#!/usr/bin/env python3
"""3-way 비교용 고정 임계치 baseline. 제안 방식(Isolation Forest, score_server.py)과
정확히 같은 standby·promotion 인프라·평가 주기·cooldown을 쓰고, "이상 여부를
어떻게 판단하는가"만 다르게 한다(guideline.md 9-7절: 순수 탐지방식 비교의
통제변수 - 동일 BlueGreen standby 조건에서 고정 임계치 vs Isolation Forest).

임계치는 비교 실행 전에 정해 고정한다(9-6절: 결과를 본 뒤 제안 방식에 유리하게
사후 조정 금지). guideline.md 저장소 구조 설명에 이미 적혀있던 예시
("CPU>90% -> promote_preview")를 그대로 쓴다 - 비교 결과를 보기 전부터 기록돼
있던 값이라 사후 선택이 아니다. 90%는 **비율**로 여기 코드에 고정하고(계약서
§6 동결 - 사후 조정 금지), 그 90%가 적용될 **절대 CPU limit**은 하드코딩하지
않는다 - `--cpu-limit-cores`로 호출자(정상 실행에서는 arm_controller.py)가
매번 명시적으로 전달해야 한다(2026-09-20 정정). features.py의 cpu_mean은
container_cpu_usage_seconds_total의 rate라 코어 단위 절대값이지 백분율이
아니므로, "90% 초과"를 판정하려면 실제 limit(코어)이 필요하다.

**정정 배경**: 이 값이 한때 `CPU_LIMIT_CORES = 4.0`으로 코드에 하드코딩돼
있었는데, `gitops/apps/vllm-serving/rollout.yaml`의 실제 CPU limit은
lab-cpu3-warm-v1(2026-09-18, `docs/design/phase8-blue-green-preflight-
incident.md` §11·§16) 재구성 이후 **3코어**다 - 즉 임계치(3.6코어)가
컨테이너가 구조적으로 넘을 수 없는 값이었다(할당량의 120%). Phase 8 동결값은
**CPU limit=3.0, threshold=2.7코어**(계약서 §6, `docs/design/experiment-
contract.md` 변경 이력) - 이 정정 전에 실행된 pilot 3건(load_ramp/pod_kill/
network_degrade 각 1회)은 구 3.6코어 기준으로 돌았으므로 원본은 그대로 두고
"배선 검증용 제외 pilot"으로만 취급한다(재실행하지 않음).

EVAL_INTERVAL_SEC/WINDOW_SEC/CONSECUTIVE_THRESHOLD/COOLDOWN_SEC/
post_to_recovery_policy()는 score_server.py에서 그대로 가져다 쓴다 - 값을
복붙하면 두 스크립트가 나중에 따로 바뀌어 비교 조건이 몰래 달라질 위험이
있어서, import로 묶어 절대 어긋날 수 없게 한다.
"""
import argparse
import math
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8")

from features import FEATURE_NAMES, extract_features, extract_features_with_provenance
from score_server import (
    COOLDOWN_SEC,
    CONSECUTIVE_THRESHOLD,
    EVAL_INTERVAL_SEC,
    WINDOW_SEC,
    post_to_recovery_policy,
    _write_evidence_line,
)

CPU_THRESHOLD_PCT = 0.90  # guideline.md 저장소 구조에 사전 기록된 값 - 사후 튜닝 금지(계약서 §6 동결)
CPU_MEAN_INDEX = FEATURE_NAMES.index("cpu_mean")


def compute_threshold_cores(cpu_limit_cores: float) -> float:
    """cpu_limit_cores(할당 CPU limit, 코어 단위)의 90%를 반환한다. 호출자가
    실제 클러스터 설정값을 매번 명시적으로 전달해야 하며, 기본값 추정은 없다
    (fail-closed) - None·NaN·무한대·0 이하는 전부 즉시 예외로 거부한다. 예전
    처럼 코드 내부에 4코어 같은 상수를 심어두면 클러스터 자원이 재구성돼도
    (lab-cpu3-warm-v1처럼) 아무도 모르게 낡은 값으로 계속 도는 사고가
    재발한다."""
    if cpu_limit_cores is None or not math.isfinite(cpu_limit_cores) or cpu_limit_cores <= 0:
        raise ValueError(
            f"--cpu-limit-cores 값이 유효하지 않음(받은 값: {cpu_limit_cores!r}) - "
            f"양의 유한한 코어 수를 명시적으로 전달해야 한다(기본값 추정 없음, fail-closed). "
            f"Phase 8 동결값은 3.0(계약서 §6, gitops/apps/vllm-serving/rollout.yaml "
            f"resources.limits.cpu와 일치해야 함)")
    return cpu_limit_cores * CPU_THRESHOLD_PCT


def evaluate_verbose() -> dict:
    """§160 - v2 계측 pilot용. WINDOW_SEC 구간의 feature 전체(cpu_mean 포함,
    queue/cache/memory도 그대로 반환)와 그 창의 시각을 반환한다.
    score_server.py의 evaluate_v32b()/_evaluate_v32b_verbose() 쌍과 동일한
    패턴 - 판정 로직은 건드리지 않고 evidence 로깅에 필요한 원시값만
    추가로 노출한다. Prometheus 쿼리는 이 함수에서 한 번만 한다(evaluate()
    가 이 함수를 감싸므로 중복 쿼리 없음).

    §163 - extract_features() 대신 extract_features_with_provenance()를
    쓴다("features" 값은 100% 동일함이 test_features.py로 확인됨) - 쿼리
    요청·응답 시각과 지표별 원본 표본 신선도(상한/하한 또는 no_samples)
    를 추가로 노출한다. "range 쿼리가 요청한 구간"(window_start/end_utc)
    을 "Prometheus가 실제로 갖고 있던 원본 표본의 시각"과 같다고 단정
    하지 않는다 - 후자는 per_metric_provenance에 별도로 담는다."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=WINDOW_SEC)
    provenance = extract_features_with_provenance(start, end)
    feats = provenance["features"]
    feature_computed_at = datetime.now(timezone.utc)
    return {
        "window_start_utc": start.isoformat(), "window_end_utc": end.isoformat(),
        "raw_feature_vector": feats, "cpu_mean": feats[CPU_MEAN_INDEX],
        "query_sent_at_utc": provenance["query_sent_at_utc"],
        "query_received_at_utc": provenance["query_received_at_utc"],
        "per_metric_provenance": provenance["per_metric_provenance"],
        "feature_computed_at_utc": feature_computed_at.isoformat(),
    }


def evaluate() -> float:
    """지금 시각 기준 최근 WINDOW_SEC 구간의 평균 CPU 사용량(코어)을 반환.
    evaluate_verbose()의 cpu_mean만 뽑는 얇은 래퍼 - 기존 호출부·반환값·
    판정에 쓰이는 수치는 §160 이전과 100% 동일하다."""
    return evaluate_verbose()["cpu_mean"]


def main(cpu_limit_cores: float, once: bool = False, experiment_run_id: str = None,
         evidence_log_path: str = None):
    """§160 - v2 계측 pilot 전용 evidence_log_path(선택, opt-in)를 추가했다.
    score_server.py의 evidence_log_path와 같은 형식(append-only JSONL,
    _write_evidence_line() 재사용 - 새 로그 포맷을 따로 만들지 않는다)이다.
    미지정(기본값 None)이면 기존과 100% 동일하게 동작한다 - 판정 로직
    (is_anomalous/연속판정/cooldown/신호 발행 여부·시점)은 이 인자와
    무관하게 전혀 바뀌지 않는다(아래에서 그 조건식들을 그대로 유지하고
    "언제 로깅하는지"만 추가했다). 기록 실패는 신호 발행을 막지 않는다
    - score_server.py의 §92 write-ahead fail-closed 게이트는 IF arm
    고유의 안전장치라 여기로 그대로 옮기지 않는다(관찰 전용 계측이
    baseline arm의 기존 승격 동작에 새로운 실패 경로를 추가하면 안 됨)."""
    threshold_cores = compute_threshold_cores(cpu_limit_cores)  # 유효성 검증 실패 시 여기서 즉시 예외(평가 루프 진입 전)
    print(f"[fixed_threshold] cpu_limit_cores={cpu_limit_cores:.3f} threshold_pct={CPU_THRESHOLD_PCT:.2f} "
          f"threshold_cores={threshold_cores:.3f} (시작 시 실제 적용값 - 계약서 §6 동결)")
    consecutive_anomalous = 0
    last_signal_at = None
    evaluation_seq = 0
    evidence_file = open(evidence_log_path, "a", encoding="utf-8") if evidence_log_path else None

    try:
        while True:
            evaluation_seq += 1
            correlation_id = uuid.uuid4().hex
            verbose = evaluate_verbose()
            cpu_mean = verbose["cpu_mean"]
            is_anomalous = cpu_mean > threshold_cores
            consecutive_anomalous = consecutive_anomalous + 1 if is_anomalous else 0
            now = time.monotonic()

            status = "이상" if is_anomalous else "정상"
            print(f"[{datetime.now(timezone.utc).isoformat()}] cpu_mean={cpu_mean:.3f}코어 "
                  f"(임계치 {threshold_cores:.3f}, limit {cpu_limit_cores:.3f}) ({status}), 연속={consecutive_anomalous}")

            # 아래 두 변수(cooldown_active/would_signal)는 기존 조건식을 값 그대로
            # 재현한다 - "신호를 언제 보낼지"는 한 글자도 안 바뀌었고, evidence
            # 기록을 실제 HTTP 호출 전에 남기기 위해 결정과 실행만 분리했다.
            cooldown_active = False
            would_signal = False
            if consecutive_anomalous >= CONSECUTIVE_THRESHOLD:
                in_cooldown = last_signal_at is not None and (now - last_signal_at) < COOLDOWN_SEC
                if in_cooldown:
                    cooldown_active = True
                    print(f"  -> cooldown 중 (남은 {COOLDOWN_SEC - (now - last_signal_at):.0f}초) - 신호 스킵")
                else:
                    would_signal = True

            if evidence_file is not None:
                _write_evidence_line(evidence_file, {
                    "record_type": "evaluation_decision", "detector": "fixed_threshold",
                    "wall_clock_utc": datetime.now(timezone.utc).isoformat(),
                    "run_id": experiment_run_id, "evaluation_seq": evaluation_seq,
                    "correlation_id": correlation_id,
                    "window_start_utc": verbose["window_start_utc"],
                    "window_end_utc": verbose["window_end_utc"],
                    # §163 - 쿼리 요청/응답 시각과 feature 계산 완료 시각을 구분해
                    # 기록한다. 이 넷(window_*, query_*, feature_computed_at)은
                    # 전부 "요청/처리 시각"이지 "원본 표본이 실제로 언제 참이
                    # 됐는가"가 아니다 - 후자는 per_metric_provenance에서
                    # 상한/하한(또는 no_samples)으로만 표현한다(단정 금지).
                    "query_sent_at_utc": verbose["query_sent_at_utc"],
                    "query_received_at_utc": verbose["query_received_at_utc"],
                    "feature_computed_at_utc": verbose["feature_computed_at_utc"],
                    "per_metric_provenance": verbose["per_metric_provenance"],
                    "raw_feature_vector": verbose["raw_feature_vector"],
                    "cpu_mean": cpu_mean, "threshold_cores": threshold_cores,
                    "is_anomalous": is_anomalous, "consecutive_anomalous": consecutive_anomalous,
                    "cooldown_active": cooldown_active, "would_signal": would_signal,
                })

            if would_signal:
                post_to_recovery_policy(cpu_mean, experiment_run_id, detector="fixed_threshold")
                last_signal_at = now

            if once:
                return
            time.sleep(EVAL_INTERVAL_SEC)
    finally:
        if evidence_file is not None:
            evidence_file.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="고정 임계치(CPU) baseline - 주기 평가 후 신호 발행")
    parser.add_argument("--once", action="store_true", help="한 번만 평가하고 종료(테스트용)")
    parser.add_argument("--run-id", default=None, help="Phase 8 오케스트레이터가 지정 - 미지정 시 감사기록이 adhoc으로 묶임")
    parser.add_argument("--cpu-limit-cores", type=float, required=True,
                         help="vLLM 컨테이너 CPU limit(코어 단위) - 기본값 없음, 반드시 명시적으로 전달"
                              "(fail-closed). Phase 8 동결값은 3.0(계약서 §6, "
                              "gitops/apps/vllm-serving/rollout.yaml resources.limits.cpu와 일치해야 함)")
    parser.add_argument("--evidence-log", default=None,
                         help="§160 - v2 계측 pilot 전용 append-only structured JSONL evidence 파일 경로"
                              "(선택). score_server.py --evidence-log와 같은 포맷(_write_evidence_line() "
                              "재사용). 기본값 None이면 기존과 완전히 동일하게 아무 파일도 안 씀(본 실험 "
                              "기본 동작 불변, 순수 opt-in) - 판정 로직에는 영향 없음(관찰 전용)")
    args = parser.parse_args()
    try:
        main(cpu_limit_cores=args.cpu_limit_cores, once=args.once, experiment_run_id=args.run_id,
             evidence_log_path=args.evidence_log)
    except ValueError as e:
        parser.error(str(e))
