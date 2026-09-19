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
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8")

from features import FEATURE_NAMES, extract_features
from score_server import (
    COOLDOWN_SEC,
    CONSECUTIVE_THRESHOLD,
    EVAL_INTERVAL_SEC,
    WINDOW_SEC,
    post_to_recovery_policy,
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


def evaluate() -> float:
    """지금 시각 기준 최근 WINDOW_SEC 구간의 평균 CPU 사용량(코어)을 반환."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=WINDOW_SEC)
    feats = extract_features(start, end)
    return feats[CPU_MEAN_INDEX]


def main(cpu_limit_cores: float, once: bool = False, experiment_run_id: str = None):
    threshold_cores = compute_threshold_cores(cpu_limit_cores)  # 유효성 검증 실패 시 여기서 즉시 예외(평가 루프 진입 전)
    print(f"[fixed_threshold] cpu_limit_cores={cpu_limit_cores:.3f} threshold_pct={CPU_THRESHOLD_PCT:.2f} "
          f"threshold_cores={threshold_cores:.3f} (시작 시 실제 적용값 - 계약서 §6 동결)")
    consecutive_anomalous = 0
    last_signal_at = None

    while True:
        cpu_mean = evaluate()
        is_anomalous = cpu_mean > threshold_cores
        consecutive_anomalous = consecutive_anomalous + 1 if is_anomalous else 0
        now = time.monotonic()

        status = "이상" if is_anomalous else "정상"
        print(f"[{datetime.now(timezone.utc).isoformat()}] cpu_mean={cpu_mean:.3f}코어 "
              f"(임계치 {threshold_cores:.3f}, limit {cpu_limit_cores:.3f}) ({status}), 연속={consecutive_anomalous}")

        if consecutive_anomalous >= CONSECUTIVE_THRESHOLD:
            in_cooldown = last_signal_at is not None and (now - last_signal_at) < COOLDOWN_SEC
            if in_cooldown:
                print(f"  -> cooldown 중 (남은 {COOLDOWN_SEC - (now - last_signal_at):.0f}초) - 신호 스킵")
            else:
                post_to_recovery_policy(cpu_mean, experiment_run_id, detector="fixed_threshold")
                last_signal_at = now

        if once:
            return
        time.sleep(EVAL_INTERVAL_SEC)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="고정 임계치(CPU) baseline - 주기 평가 후 신호 발행")
    parser.add_argument("--once", action="store_true", help="한 번만 평가하고 종료(테스트용)")
    parser.add_argument("--run-id", default=None, help="Phase 8 오케스트레이터가 지정 - 미지정 시 감사기록이 adhoc으로 묶임")
    parser.add_argument("--cpu-limit-cores", type=float, required=True,
                         help="vLLM 컨테이너 CPU limit(코어 단위) - 기본값 없음, 반드시 명시적으로 전달"
                              "(fail-closed). Phase 8 동결값은 3.0(계약서 §6, "
                              "gitops/apps/vllm-serving/rollout.yaml resources.limits.cpu와 일치해야 함)")
    args = parser.parse_args()
    try:
        main(cpu_limit_cores=args.cpu_limit_cores, once=args.once, experiment_run_id=args.run_id)
    except ValueError as e:
        parser.error(str(e))
