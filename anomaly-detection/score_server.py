#!/usr/bin/env python3
"""실시간 위험도 판단 로직 — 주기적으로 최근 구간 특성을 평가해 Isolation
Forest 점수를 매기고, 연속판정+cooldown을 거쳐 recovery-policy에 신호를
발행한다(guideline.md Phase 6 pseudocode 그대로 구현).

recovery-policy/main.py는 아직 없어서(Phase 7 미착수) POST는 지금 당연히
연결 실패한다 - 그건 이 스크립트의 버그가 아니라 아직 받을 곳이 없는 것뿐이라
로그만 남기고 계속 돈다.

**§86(2026-09-21) v3.2b 통합** - `--artifacts-dir`/`--model-version`을
필수로 받는다(암묵적 기본 artifact 없음, `fixed_threshold.py`의
`--cpu-limit-cores` fail-closed 선례와 동일 원칙). 기존 v1 artifact
(`anomaly-detection/artifacts/`)와 rejected v3.1 artifact는 이 변경으로
전혀 수정·삭제되지 않는다 - 그저 더 이상 "인자 없을 때의 암묵적 기본값"이
아니게 됐을 뿐이다. feature 추출·scaling·판정은 offline evaluator
(`model_v31/evaluate.py`)와 완전히 같은 함수(`build_dataset.
extract_window_strict()`·`feature_selection.apply_feature_schema()`)를
그대로 재사용해 parity를 코드 수준에서 보장한다."""
import argparse
import hashlib
import json
import math
import os
import pickle
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import requests

sys.stdout.reconfigure(encoding="utf-8")  # Windows 기본 cp949 콘솔 대응

V3_DIR = Path(__file__).parent / "v3"
sys.path.insert(0, str(V3_DIR))
sys.path.insert(0, str(V3_DIR / "model_v31"))

from features import _query_range  # noqa: E402 (anomaly-detection/features.py, 같은 디렉터리)
from build_dataset import extract_window_strict  # noqa: E402 (v3/build_dataset.py, 변경 없음)
from feature_selection import apply_feature_schema  # noqa: E402 (model_v31, 변경 없음)
from integrity import verify_sha256sums  # noqa: E402 (model_v31, 변경 없음)
from prom_health import check_metric_freshness, query_range_with_bounded_retry  # noqa: E402 (v3/prom_health.py)

# recovery-policy가 in-cluster Deployment/Service로 배포되므로(1단계) 그
# in-cluster DNS를 기본값으로 쓴다. Phase 8 arm_controller.py가 로컬에서
# 이 스크립트를 서브프로세스로 띄울 때는(kubectl port-forward -n
# vllm-serving svc/recovery-policy 8080:8080 전제 - run_once.py의
# RECOVERY_POLICY_URL과 동일 전제) RECOVERY_POLICY_SIGNAL_URL 환경변수로
# http://localhost:8080/signal을 넘긴다(2026-09-18 추가) - 소스를 고쳐야
# 했던 기존 수동 절차를 없앤다. 환경변수 미지정 시 동작은 기존과 동일.
# §86 no-action smoke는 이 같은 환경변수를 real recovery-policy가 아닌
# local capture sink URL로 덮어써서 재사용한다(score_server.py 자체
# 변경 없음 - 이미 있던 확장점).
RECOVERY_POLICY_URL = os.environ.get(
    "RECOVERY_POLICY_SIGNAL_URL",
    "http://recovery-policy.vllm-serving.svc.cluster.local:8080/signal",
)

EVAL_INTERVAL_SEC = 15  # 평가 주기 (features.py의 Prometheus query step과 동일)
WINDOW_SEC = 60  # 평가 대상 trailing window (slo-definition.md와 동일 관례)
CONSECUTIVE_THRESHOLD = 3  # 이 횟수만큼 연속으로 이상이어야 신호 발행 (단발 노이즈 방지)
COOLDOWN_SEC = 60  # 신호 발행 후 이 시간 동안은 재발행 안 함
FRESHNESS_MAX_AGE_SEC = 120.0  # WINDOW_SEC(60초)보다 넉넉히 큰 상한 - arm_controller.py의 동일 상수와 같은 값
# §107(2026-09-23, docs/design/phase8-blue-green-preflight-incident.md §106
# - pod_kill-proposed-03-mainexp-v1 사후조사 계기) - 예전엔 up{job=
# "vllm-active"} 단일 canary로 6개 feature 전체의 신선도를 대신 판단했다.
# 이 canary는 vllm-active Service의 EndpointSlice 존재 여부에 구조적으로
# 묶여 있다(gitops/apps/vllm-serving/servicemonitor.yaml - 같은
# ServiceMonitor가 vllm-active/vllm-preview 두 Service를 모두 스크랩).
# pod_kill이 만드는 엔드포인트 공백 구간에는 cpu/memory(kubelet/cAdvisor
# 기반, Service 엔드포인트와 무관)는 여전히 계산 가능한데 canary만 비어
# "fail-closed: metric stale"이 걸릴 수 있다(§106 조사 결론 - "가능성
# 높음, 100% 확정 아님"). queue/cache는 vLLM 자체 /metrics로 같은
# ServiceMonitor를 타므로 canary와 같은 구조적 취약점을 공유한다("6개
# feature가 계산 가능했다는 사실만으로 신선함까지 증명되지 않는다").
#
# 그래서 하나의 대리 지표 대신 4개 feature 원천 각각의 실제 표본 시각을
# 개별 확인한다. cpu만 METRICS의 rate(...[30s]) 대신 raw counter를
# 쓴다 - rate()를 신선도 판정에 직접 쓰면 안 되는 이유는 아래 원래 있던
# 설명 그대로다(30초 구간 안에 표본이 2개 이상 있어야 계산되는 rate()라
# 스크레이프 타이밍에 따라 간헐적으로 빈 응답이 나올 수 있음을 실측으로
# 확인함, 2026-09-21, 8회 중 1회 재현) - feature 계산 자체(METRICS)는
# 전혀 안 바꾸고 이 판정에서만 다른 쿼리를 쓴다.
FRESHNESS_PROBE_QUERIES = {
    "cpu": 'container_cpu_usage_seconds_total{namespace="vllm-serving",container="vllm"}',
    "memory": 'container_memory_working_set_bytes{namespace="vllm-serving",container="vllm"}',
    "queue": "vllm:num_requests_waiting",
    "cache": "vllm:kv_cache_usage_perc",
}
# §107 - "장기간 입력이 없는 경우를 단순한 미탐지와 구별"하는 기준. WINDOW_SEC
# (60초) 자체가 feature 계산에 쓰는 trailing window 길이이므로, 연속
# 스킵이 WINDOW_SEC를 넘기면(=window 전체가 이미 공백 구간 안에 들어간
# 상태) "잠깐 꼬리만 걸친" 상황을 넘어 질적으로 다른 상태로 본다.
PROLONGED_DATA_GAP_CYCLES = int(WINDOW_SEC // EVAL_INTERVAL_SEC)  # 60/15 = 4

_DEPENDENCY_PIN_PATTERN = re.compile(r"^(scikit-learn|numpy)==([^\s#]+)")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _check_dependency_versions(requirements_lock_path: Path) -> None:
    """§86.2 - artifact와 함께 동결된 정확한 의존성 버전과 실제 설치된
    버전을 대조한다. 하드코딩된 버전 문자열을 여기 또 심지 않고
    `requirements-lock.txt`(동결 artifact 자체)를 유일한 소스로 쓴다 -
    버전 하나만 갱신하면 여기와 어긋날 일이 없다."""
    import numpy
    import sklearn

    installed = {"scikit-learn": sklearn.__version__, "numpy": numpy.__version__}
    pinned = {}
    for line in requirements_lock_path.read_text(encoding="utf-8").splitlines():
        m = _DEPENDENCY_PIN_PATTERN.match(line.strip())
        if m:
            pinned[m.group(1)] = m.group(2)

    mismatches = [f"{name}: pinned={ver}, installed={installed.get(name)}"
                  for name, ver in pinned.items() if installed.get(name) != ver]
    if mismatches:
        raise RuntimeError(f"fail-closed: 의존성 버전 불일치 - {mismatches}")
    if not pinned:
        raise RuntimeError(f"fail-closed: {requirements_lock_path}에서 scikit-learn/numpy 고정 버전을 못 찾음")


def load_and_verify_artifacts(artifacts_dir: Path, expected_model_version: str) -> dict:
    """§86.2 필수 조건 전부 이 함수 하나에서 fail-closed로 확인한다 -
    암묵적 latest/default 없음(호출부가 `--artifacts-dir`를 명시해야만
    호출됨), SHA-256 전수 검증, 의존성 버전 검증, model_version 교차
    확인, runtime replay 상수(threshold.json에 기록된 값)와 이 파일의
    상수(EVAL_INTERVAL_SEC 등)가 어긋나지 않는지까지 확인한다."""
    if not artifacts_dir.is_dir():
        raise RuntimeError(f"fail-closed: artifact 디렉터리 없음: {artifacts_dir}")

    mismatches = verify_sha256sums(artifacts_dir)
    if mismatches:
        raise RuntimeError(f"fail-closed: SHA256SUMS 불일치 {len(mismatches)}건: {mismatches}")

    _check_dependency_versions(artifacts_dir / "requirements-lock.txt")

    model = pickle.loads((artifacts_dir / "model.pkl").read_bytes())
    scaler = pickle.loads((artifacts_dir / "scaler.pkl").read_bytes())
    schema = json.loads((artifacts_dir / "feature-schema.json").read_text(encoding="utf-8"))
    threshold_doc = json.loads((artifacts_dir / "threshold.json").read_text(encoding="utf-8"))
    training_metadata = json.loads((artifacts_dir / "training-metadata.json").read_text(encoding="utf-8"))

    actual_version = training_metadata.get("model_version")
    if actual_version != expected_model_version:
        raise RuntimeError(
            f"fail-closed: --model-version({expected_model_version!r})이 training-metadata.json의 "
            f"model_version({actual_version!r})과 다름 - 잘못된 artifact 디렉터리일 수 있음")

    runtime_checks = {
        "consecutive_threshold": (threshold_doc.get("consecutive_threshold"), CONSECUTIVE_THRESHOLD),
        "cooldown_sec": (threshold_doc.get("cooldown_sec"), COOLDOWN_SEC),
        "eval_interval_sec": (threshold_doc.get("eval_interval_sec"), EVAL_INTERVAL_SEC),
    }
    rule_mismatches = [f"{k}: artifact={a}, runtime={r}" for k, (a, r) in runtime_checks.items() if a != r]
    if rule_mismatches:
        raise RuntimeError(f"fail-closed: threshold.json의 runtime replay 규칙이 score_server.py 상수와 다름 - "
                            f"{rule_mismatches}")

    return {
        "model": model, "scaler": scaler, "schema": schema,
        "threshold": threshold_doc["threshold"],
        "artifacts_dir": artifacts_dir,
        "artifact_hashes": {
            "model.pkl": _sha256_file(artifacts_dir / "model.pkl"),
            "scaler.pkl": _sha256_file(artifacts_dir / "scaler.pkl"),
            "threshold.json": _sha256_file(artifacts_dir / "threshold.json"),
            "feature-schema.json": _sha256_file(artifacts_dir / "feature-schema.json"),
        },
    }


def _evaluate_v32b_verbose(model, scaler, schema, *,
                            query_range_fn: Callable = _query_range,
                            freshness_check_fn: Callable = check_metric_freshness,
                            now_fn: Callable = lambda: datetime.now(timezone.utc)) -> dict:
    """§86.2/§88.6 - offline evaluator(`model_v31/evaluate.py`)와 정확히
    같은 2단계(8-feature 추출 -> `apply_feature_schema()`로 6개만
    순서대로 선택 -> scaler.transform -> decision_function)를 실시간
    window에 적용한다. missing/NaN/Inf/stale 중 하나라도 있으면 score를
    만들지 않고 예외를 던진다(fail-closed) - 호출부가 이 예외를 삼키지
    않고 그대로 올려 signal도 안 보내지게 한다.

    §88(2026-09-21) - evidence 로깅에 raw/ordered/scaled feature vector가
    필요해져 중간값을 전부 담은 dict를 반환하도록 분리했다(판정 로직
    자체는 전혀 바꾸지 않음 - `evaluate_v32b()`가 이 함수를 감싸 기존과
    동일하게 `score` float만 반환)."""
    end = now_fn()
    start = end - timedelta(seconds=WINDOW_SEC)

    def bounded_query_range_fn(promql, s, e):
        return query_range_with_bounded_retry(promql, s, e, query_range_fn=query_range_fn)

    feats, reason = extract_window_strict(start, end, bounded_query_range_fn)
    if feats is None:
        raise RuntimeError(f"fail-closed: feature 결측 - {reason}")
    if any(v is None or math.isnan(v) or math.isinf(v) for v in feats):
        raise RuntimeError(f"fail-closed: feature에 NaN/Inf 포함: {feats}")

    # §107 - 4개 feature 원천을 개별 확인(위 FRESHNESS_PROBE_QUERIES 주석
    # 참고) - 첫 stale/결측 지점에서 바로 raise(기존과 동일하게 "하나라도
    # 문제면 이 cycle은 score를 만들지 않는다"는 fail-closed 원칙 자체는
    # 안 바뀜, 어떤 원천이 문제인지만 더 정확해짐).
    freshness_by_metric = {}
    for metric_name, probe_promql in FRESHNESS_PROBE_QUERIES.items():
        freshness = freshness_check_fn(probe_promql, FRESHNESS_MAX_AGE_SEC)
        if not freshness["fresh"]:
            raise RuntimeError(f"fail-closed: metric stale - {metric_name}({probe_promql}): {freshness.get('reason')}")
        freshness_by_metric[metric_name] = freshness

    x6 = apply_feature_schema(feats, schema)
    x_scaled = scaler.transform([x6])
    score = float(model.decision_function(x_scaled)[0])
    return {
        "window_start_utc": start.isoformat(), "window_end_utc": end.isoformat(),
        "raw_feature_vector": feats, "ordered_feature_vector": x6,
        "scaled_feature_vector": [float(v) for v in x_scaled[0]],
        "score": score, "freshness_by_metric": freshness_by_metric,
    }


def evaluate_v32b(model, scaler, schema, *,
                   query_range_fn: Callable = _query_range,
                   freshness_check_fn: Callable = check_metric_freshness,
                   now_fn: Callable = lambda: datetime.now(timezone.utc)) -> float:
    """기존 호출부(§86.2/§86.3 테스트 포함) 하위호환 - score만 반환.
    판정 로직은 `_evaluate_v32b_verbose()`와 완전히 동일(그 함수를
    그대로 호출할 뿐)."""
    return _evaluate_v32b_verbose(model, scaler, schema, query_range_fn=query_range_fn,
                                   freshness_check_fn=freshness_check_fn, now_fn=now_fn)["score"]


def advance_streak(score: float, threshold: float, consecutive_anomalous: int, last_signal_at,
                    now: float, *, cooldown_sec: float = COOLDOWN_SEC,
                    consecutive_threshold: int = CONSECUTIVE_THRESHOLD) -> dict:
    """§86.3 - `main()`의 상태기계를 순수 함수로 추출한 것(연속 3회+정상
    1회 즉시 reset+cooldown 60초, `model_v31/replay.py`의
    `replay_detector()`와 정확히 같은 규칙을 한 스텝씩 진행하는 버전).
    이렇게 분리해야 offline evaluator의 `replay_detector()`와 동일한
    score 시퀀스를 넣었을 때 신호 타이밍이 완전히 일치하는지 직접
    검증할 수 있다(오프라인 테스트 전용 분리가 아니라 `main()`도 이
    함수를 그대로 씀 - 로직이 두 곳에 복제되지 않는다)."""
    is_anomalous = score < threshold
    new_consecutive = consecutive_anomalous + 1 if is_anomalous else 0
    should_signal = False
    new_last_signal_at = last_signal_at
    if new_consecutive >= consecutive_threshold:
        in_cooldown = last_signal_at is not None and (now - last_signal_at) < cooldown_sec
        if not in_cooldown:
            should_signal = True
            new_last_signal_at = now
    return {
        "is_anomalous": is_anomalous, "consecutive_anomalous": new_consecutive,
        "should_signal": should_signal, "last_signal_at": new_last_signal_at,
    }


# §92 - promote()의 실측 지연(§91: t_api_request~t_switch 7.543초, 서버측
# rollouts_client.promote()의 CLI_TIMEOUT_SEC=30 + verify_timeout=5.0이
# 이론적 상한)이 기존 5초 read timeout보다 길 수 있음이 §91 실제 파일럿
# 데이터로 확인됐다 - 서버 처리가 5초를 넘기면 클라이언트가 응답을 못 받고
# ReadTimeout을 던지는데, 이 예외가 서버측 실제 조치(승격)를 막지는 않지만
# (서버는 독립적으로 계속 처리) 클라이언트 쪽 관찰(evidence)이 이 신호의
# 결과를 놓치는 원인이 될 수 있다(§92 forensic 참고). 서버측 문서화된
# 상한(30+5=35초)보다 넉넉한 여유를 둔다 - 판정 로직(언제 신호를 보낼지)은
# 전혀 안 바꾸고, 신호를 보낸 뒤 응답을 기다리는 시간만 늘린다.
SIGNAL_CONNECT_TIMEOUT_SEC = 5.0
SIGNAL_READ_TIMEOUT_SEC = 45.0


def post_to_recovery_policy(score: float, experiment_run_id: str = None, detector: str = "isolation_forest", *,
                             correlation_id: str = None, evaluation_seq: int = None,
                             model_version: str = None, artifact_hashes: dict = None,
                             threshold: float = None, consecutive_anomalous: int = None) -> dict:
    """§88.6/§92 - 반환값(성공/실패 종류 + payload + 진단 정보)을 추가했다
    (기존 호출부인 `fixed_threshold.py`는 이 함수를 쓰지 않으므로 영향
    없음) - evidence 로그에 signal_result를 남기기 위함, 실제 신호
    판정(언제 보낼지)은 전혀 안 바뀌었다.

    §92 provenance 필드(전부 선택 인자, 기본값 None이면 payload에 아예
    안 붙음 - 기존 payload와 100% 동일) - recovery-policy의
    `AnomalySignalRequest`가 `Optional[...] = None`으로 선언한 필드만
    실제로 감사기록에 반영되고, 그 외 필드는 Pydantic이 조용히 무시한다
    (요청 자체는 항상 성공 - 서버 결정 로직·기존 필드 의미 무변경).

    §92 예외 처리 - 기존엔 `ConnectionError`만 잡아서 `ReadTimeout`류가
    새 나가 while 루프 전체가 죽을 위험이 있었다(§92 forensic). 이제
    `RequestException`(그 상위 클래스, `ConnectionError` 포함) 전체를
    잡아 어떤 실패든 while 루프를 절대 죽이지 않는다."""
    timestamp = datetime.now(timezone.utc).isoformat()
    payload = {
        "signal_type": "anomaly_risk", "score": score,
        "timestamp": timestamp, "detector": detector,
    }
    if experiment_run_id:
        payload["experiment_run_id"] = experiment_run_id
    if correlation_id is not None:
        payload["correlation_id"] = correlation_id
    if evaluation_seq is not None:
        payload["evaluation_seq"] = evaluation_seq
    if model_version is not None:
        payload["model_version"] = model_version
    if artifact_hashes is not None:
        payload["model_hash"] = artifact_hashes.get("model.pkl")
        payload["feature_schema_hash"] = artifact_hashes.get("feature-schema.json")
    if threshold is not None:
        payload["threshold"] = threshold
    if consecutive_anomalous is not None:
        payload["consecutive_count"] = consecutive_anomalous

    idempotency_key_hint = f"{experiment_run_id}:{payload['signal_type']}" if experiment_run_id else None
    try:
        r = requests.post(RECOVERY_POLICY_URL, json=payload,
                           timeout=(SIGNAL_CONNECT_TIMEOUT_SEC, SIGNAL_READ_TIMEOUT_SEC))
        print(f"  -> 신호 발행: {payload}")
        return {
            "outcome": "sent", "http_status": r.status_code,
            "response_body_summary": r.text[:500],
            "idempotency_key_hint": idempotency_key_hint, "payload": payload,
        }
    except requests.exceptions.ConnectionError as e:
        print(f"  -> recovery-policy 서비스 없음(Phase 7 미구현) - 신호 발행 스킵: {payload}")
        return {"outcome": "connection_error", "error": str(e),
                "idempotency_key_hint": idempotency_key_hint, "payload": payload}
    except requests.exceptions.RequestException as e:
        print(f"  -> 신호 발행 실패({type(e).__name__}): {payload}")
        return {"outcome": "request_exception", "error": f"{type(e).__name__}: {e}",
                "idempotency_key_hint": idempotency_key_hint, "payload": payload}


def _write_evidence_line(evidence_file, record: dict) -> bool:
    """§88.6/§92 - append-only JSONL, 매 evaluation 직후 flush+fsync(가능하면).
    반환값(성공 여부)을 추가했다(§92) - evaluation 자체를 막지는 않지만
    (기존과 동일하게 예외를 삼키고 계속 진행), `main()`이 이 반환값을 보고
    "이 cycle의 write-ahead가 실패했으면 신호를 보내지 않는다"는 별도의
    fail-closed 게이트를 걸 수 있게 한다(§92.3 - 신호만 조건부로 막지,
    evaluation 자체를 막지는 않음)."""
    try:
        evidence_file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        evidence_file.flush()
        try:
            os.fsync(evidence_file.fileno())
        except OSError:
            pass  # 일부 파일시스템/스트림은 fsync 미지원 - 관찰 전용이므로 무시
        return True
    except Exception as e:  # noqa: BLE001 - evidence 기록 실패가 evaluation을 막으면 안 됨
        print(f"  -> [evidence] 기록 실패(무시하고 계속): {e}")
        return False


def main(artifacts_dir: str, model_version: str, once: bool = False, experiment_run_id: str = None,
         evidence_log_path: str = None, stop_file_path: str = None):
    """§92 - main loop을 write-ahead evidence 순서로 재구성했다(판정
    로직은 전혀 안 바꿈, IO 순서만 강화):
      0) §107 - `_evaluate_v32b_verbose()`가 fail-closed RuntimeError(결측/
         NaN/stale)를 던지면 이 cycle만 스킵하고(`record_type:
         "evaluation_skipped"`로 evidence에 남김) loop는 계속 돈다 -
         프로세스 자체를 종료시키지 않는다(과거엔 이 예외가 그대로 새
         나가 detector 전체가 죽었다, phase8-blue-green-preflight-incident.md
         §106/§107 참고). 그 외 예상 밖 예외는 그대로 전파돼 프로세스를
         종료시킨다(기존과 동일).
      1) feature/score/streak 계산(변경 없음)
      2) `evaluation_decision` record를 **외부 HTTP 신호 전에** 먼저
         flush+fsync(§92 forensic이 지목한 §91의 근본 취약점 - 기존엔
         신호 HTTP 호출이 끝난 "뒤에만" 기록해서, 그 호출이 멈추거나
         (예외 처리 밖으로 새 나가는 경우) 프로세스가 죽으면 그 cycle의
         결정 자체가 통째로 유실됐다).
      3) `would_signal`이면 신호를 보내고, 성공/실패/예외 무엇이든
         별도의 `signal_result` record로(먼저 쓴 `evaluation_decision`을
         절대 덮어쓰지 않음) correlation_id로 연결해 flush+fsync.
      4) 매 cycle 끝에 `stop_file_path`(선택)가 존재하는지 확인 - §92
         graceful shutdown: 오케스트레이터가 이 파일을 만들면(정지 요청)
         "이번 cycle을 끝까지 완료한 뒤" 스스로 종료한다(OS 시그널
         강제종료와 달리 진행 중이던 evidence 기록이 항상 완결된 채로
         남는다) - `detector_shutdown` record를 남기고 정상 반환한다.
         강제종료(terminate/kill)는 여전히 오케스트레이터 쪽의 최후
         수단으로 남아있다(이 파일이 없거나 grace 기간을 넘기면)."""
    artifacts = load_and_verify_artifacts(Path(artifacts_dir), model_version)
    model, scaler, schema, threshold = artifacts["model"], artifacts["scaler"], artifacts["schema"], artifacts["threshold"]
    print(f"[score_server v3.2b] model_version={model_version} artifacts_dir={artifacts['artifacts_dir']} "
          f"features({len(schema['kept_feature_names'])})={schema['kept_feature_names']} threshold={threshold} "
          f"recovery_policy_url={RECOVERY_POLICY_URL} artifact_hashes={artifacts['artifact_hashes']}")

    consecutive_anomalous = 0
    last_signal_at = None
    evaluation_seq = 0
    # §107(2026-09-23) - pod_kill-proposed-03-mainexp-v1 사후조사(§106) 결론:
    # _evaluate_v32b_verbose()의 fail-closed RuntimeError(결측/NaN/stale)가
    # 이 while 루프를 통째로 죽여 detector 프로세스 자체가 종료됐다(run_once.py
    # 쪽에서는 "detector가 관찰 도중 비정상 종료"로 관측됨). pod_kill의
    # 엔드포인트 공백처럼 15~25초짜리 일시적 갭 한 번 때문에 남은 trial
    # 전체의 detector가 사라지는 건 과도한 fail-closed다 - "이 cycle의
    # score/신호를 내지 않는다"와 "detector 프로세스 자체가 죽는다"는
    # 서로 다른 요구이므로 분리한다. consecutive_data_gap_cycles는 "정상
    # 평가가 몇 cycle째 연속으로 안 되고 있는가"만 추적한다(판정 로직인
    # advance_streak()는 전혀 안 건드림).
    consecutive_data_gap_cycles = 0
    evidence_file = open(evidence_log_path, "a", encoding="utf-8") if evidence_log_path else None

    try:
        while True:
            evaluation_seq += 1
            correlation_id = uuid.uuid4().hex
            wall_clock_before = datetime.now(timezone.utc).isoformat()
            try:
                verbose = _evaluate_v32b_verbose(model, scaler, schema)
            except RuntimeError as e:
                # §107 - fail-closed 조건(결측/NaN/stale)에 한해서만 잡는다.
                # 이 3개는 _evaluate_v32b_verbose()가 명시적으로 RuntimeError로
                # 표현하는, 이미 알려진("fail-closed: ...") 데이터 품질 문제뿐이다.
                # 그 외의 진짜 예상 밖 예외(다른 예외 타입)는 여기서 안 잡고
                # 그대로 위로 새 나가 프로세스를 종료시킨다(기존과 동일 -
                # 진짜 버그를 조용히 삼키면 안 됨).
                consecutive_data_gap_cycles += 1
                gap_classification = ("prolonged" if consecutive_data_gap_cycles >= PROLONGED_DATA_GAP_CYCLES
                                       else "transient")
                # 미확정 cycle이다 - anomalous로도 정상으로도 간주하지 않고
                # 스트릭을 리셋한다(advance_streak() 자체는 호출하지 않음 -
                # 판정 함수는 안 바꾸고 이 loop의 호출 여부만 조절).
                consecutive_anomalous = 0
                print(f"[{datetime.now(timezone.utc).isoformat()}] 평가 스킵(seq={evaluation_seq}, "
                      f"연속 {consecutive_data_gap_cycles}회 데이터 갭, {gap_classification}) - {e}")
                if evidence_file is not None:
                    _write_evidence_line(evidence_file, {
                        "record_type": "evaluation_skipped",
                        "wall_clock_before_utc": wall_clock_before,
                        "wall_clock_after_utc": datetime.now(timezone.utc).isoformat(),
                        "run_id": experiment_run_id, "model_version": model_version,
                        "evaluation_seq": evaluation_seq, "correlation_id": correlation_id,
                        "reason": str(e),
                        "consecutive_data_gap_cycles": consecutive_data_gap_cycles,
                        "gap_classification": gap_classification,
                    })
                if once:
                    return
                if stop_file_path is not None and os.path.exists(stop_file_path):
                    print(f"  -> [shutdown] stop-file 감지({stop_file_path}) - 정상 종료(seq={evaluation_seq})")
                    if evidence_file is not None:
                        _write_evidence_line(evidence_file, {
                            "record_type": "detector_shutdown",
                            "run_id": experiment_run_id,
                            "requested_via": "stop_file",
                            "exited_at_utc": datetime.now(timezone.utc).isoformat(),
                            "last_evaluation_seq": evaluation_seq,
                            "exit_reason": "graceful_stop_file",
                        })
                    return
                time.sleep(EVAL_INTERVAL_SEC)
                continue

            consecutive_data_gap_cycles = 0  # 정상 평가 성공 - 갭 카운터 리셋
            score = verbose["score"]
            now = time.monotonic()
            step = advance_streak(score, threshold, consecutive_anomalous, last_signal_at, now)
            consecutive_anomalous, last_signal_at = step["consecutive_anomalous"], step["last_signal_at"]

            status = "이상" if step["is_anomalous"] else "정상"
            print(f"[{datetime.now(timezone.utc).isoformat()}] score={score:.4f} ({status}), 연속={consecutive_anomalous}"
                  f" seq={evaluation_seq} corr={correlation_id}")

            cooldown_active = bool(consecutive_anomalous >= CONSECUTIVE_THRESHOLD and not step["should_signal"])
            if cooldown_active:
                remaining = COOLDOWN_SEC - (now - last_signal_at)
                print(f"  -> cooldown 중 (남은 {remaining:.0f}초) - 신호 스킵")

            # §92 - write-ahead: 신호를 보내기 전에 이 cycle의 결정을 먼저
            # 영구 기록한다. would_signal=True인 cycle도 예외 없이 여기서
            # 먼저 flush+fsync된 뒤에만 아래에서 실제 HTTP 요청을 보낸다.
            # evidence_log_path 미지정(기존 대부분의 로컬/테스트 호출)이면
            # decision_write_ok=True(기록할 것 자체가 없음 - 기존과 동일하게
            # 신호는 evidence와 무관하게 그대로 나간다).
            decision_write_ok = True
            if evidence_file is not None:
                decision_write_ok = _write_evidence_line(evidence_file, {
                    "record_type": "evaluation_decision",
                    "wall_clock_before_utc": wall_clock_before,
                    "wall_clock_after_utc": datetime.now(timezone.utc).isoformat(),
                    "run_id": experiment_run_id, "model_version": model_version,
                    "evaluation_seq": evaluation_seq, "correlation_id": correlation_id,
                    "artifact_hashes": artifacts["artifact_hashes"],
                    "window_start_utc": verbose["window_start_utc"], "window_end_utc": verbose["window_end_utc"],
                    "raw_feature_vector": verbose["raw_feature_vector"],
                    "ordered_feature_vector": verbose["ordered_feature_vector"],
                    "scaled_feature_vector": verbose["scaled_feature_vector"],
                    "score": score, "threshold": threshold, "is_anomalous": step["is_anomalous"],
                    "consecutive_anomalous": consecutive_anomalous,
                    "cooldown_active": cooldown_active,
                    "would_signal": step["should_signal"],
                    "target_signal_url": RECOVERY_POLICY_URL,
                    "lifecycle_phase": None,  # 오케스트레이터가 세션 자체 타임스탬프로 사후 결합(§88.6 - 외부 timeline 방식)
                })

            signal_response = None
            if step["should_signal"]:
                if not decision_write_ok:
                    # §92.3 - fail-closed: evidence-log가 설정돼 있는데(opt-in)
                    # 이 cycle의 write-ahead 기록 자체가 실패했으면, 그 결정의
                    # 근거를 영구히 남길 수 없는 채로 실제 조치(recovery-policy
                    # promotion)를 유발하지 않는다. 판정 로직(연속 3회 이상)은
                    # 그대로 유지되고, 다음 cycle에서 여전히 anomalous면 그때
                    # 다시 시도한다(cooldown/consecutive 상태는 이미 위에서
                    # advance_streak()가 갱신했으므로 영향 없음).
                    print(f"  -> [fail-closed] evidence write-ahead 실패 - 신호를 보내지 않음(seq={evaluation_seq})")
                    signal_response = {"outcome": "skipped_evidence_write_failed", "payload": None}
                else:
                    attempted_at = datetime.now(timezone.utc).isoformat()
                    signal_response = post_to_recovery_policy(
                        score, experiment_run_id,
                        correlation_id=correlation_id, evaluation_seq=evaluation_seq,
                        model_version=model_version, artifact_hashes=artifacts["artifact_hashes"],
                        threshold=threshold, consecutive_anomalous=consecutive_anomalous,
                    )
                    completed_at = datetime.now(timezone.utc).isoformat()
                    if evidence_file is not None:
                        _write_evidence_line(evidence_file, {
                            "record_type": "signal_result",
                            "correlation_id": correlation_id, "evaluation_seq": evaluation_seq,
                            "run_id": experiment_run_id,
                            "attempted_at_utc": attempted_at, "completed_at_utc": completed_at,
                            "outcome": signal_response.get("outcome"),
                            "http_status": signal_response.get("http_status"),
                            "error": signal_response.get("error"),
                            "response_body_summary": signal_response.get("response_body_summary"),
                            "idempotency_key_hint": signal_response.get("idempotency_key_hint"),
                        })

            if once:
                return

            if stop_file_path is not None and os.path.exists(stop_file_path):
                print(f"  -> [shutdown] stop-file 감지({stop_file_path}) - 정상 종료(seq={evaluation_seq})")
                if evidence_file is not None:
                    _write_evidence_line(evidence_file, {
                        "record_type": "detector_shutdown",
                        "run_id": experiment_run_id,
                        "requested_via": "stop_file",
                        "exited_at_utc": datetime.now(timezone.utc).isoformat(),
                        "last_evaluation_seq": evaluation_seq,
                        "exit_reason": "graceful_stop_file",
                    })
                return

            time.sleep(EVAL_INTERVAL_SEC)
    finally:
        if evidence_file is not None:
            evidence_file.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="실시간 위험도 판단 - 주기 평가 후 신호 발행")
    parser.add_argument("--once", action="store_true", help="한 번만 평가하고 종료(테스트용)")
    parser.add_argument("--run-id", default=None, help="Phase 8 오케스트레이터가 지정 - 미지정 시 감사기록이 adhoc으로 묶임")
    parser.add_argument("--artifacts-dir", required=True,
                         help="동결된 model.pkl/scaler.pkl/threshold.json/feature-schema.json/"
                              "requirements-lock.txt/SHA256SUMS.json/training-metadata.json이 있는 디렉터리 - "
                              "기본값 없음(fail-closed, §86). 예: anomaly-detection/v3/model_v32b/artifacts")
    parser.add_argument("--model-version", required=True,
                         help="training-metadata.json의 model_version과 정확히 일치해야 함(예: v3.2b) - "
                              "기본값 없음(fail-closed)")
    parser.add_argument("--evidence-log", default=None,
                         help="§88.6/§92 - append-only structured JSONL evidence 파일 경로(선택). stdout 로그가 "
                              "리다이렉트·버퍼링으로 유실돼도(§87.1) 매 evaluation 직후 flush+fsync되는 이 파일로 "
                              "판정 세부값을 남긴다 - 판정 로직에는 영향 없음(관찰 전용). §92부터 신호 전 "
                              "evaluation_decision을 먼저 쓰고 신호 후 signal_result를 별도로 남긴다(write-ahead)")
    parser.add_argument("--stop-file", default=None,
                         help="§92 - graceful shutdown 요청 파일 경로(선택). 이 파일이 생기면 진행 중이던 "
                              "cycle을 끝까지 완료(신호·evidence 기록 포함)한 뒤 스스로 정상 종료한다. "
                              "미지정 시 기존과 동일하게 오케스트레이터의 terminate/kill만으로 종료된다")
    args = parser.parse_args()
    try:
        main(artifacts_dir=args.artifacts_dir, model_version=args.model_version,
             once=args.once, experiment_run_id=args.run_id, evidence_log_path=args.evidence_log,
             stop_file_path=args.stop_file)
    except RuntimeError as e:
        parser.error(str(e))
