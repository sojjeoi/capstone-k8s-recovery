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
# METRICS["cpu"]는 rate(...[30s]) - 인스턴트 쿼리로 신선도만 확인하기엔
# 30초 구간 안에 표본이 2개 이상 있어야 계산되는 rate()라 스크레이프
# 타이밍에 따라 간헐적으로 빈 응답이 나올 수 있음을 실측으로 확인했다
# (2026-09-21, 8회 중 1회 재현). `up`(스크레이프마다 항상 1개씩 찍히는
# 순수 gauge, 윈도우 계산 자체가 없음)으로 신선도를 확인해 이 문제를
# 피한다 - feature 추출 자체(METRICS)는 그대로 둔다.
FRESHNESS_PROBE_QUERY = 'up{namespace="vllm-serving",job="vllm-active"}'

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


def evaluate_v32b(model, scaler, schema, *,
                   query_range_fn: Callable = _query_range,
                   freshness_check_fn: Callable = check_metric_freshness,
                   now_fn: Callable = lambda: datetime.now(timezone.utc)) -> float:
    """§86.2 - offline evaluator(`model_v31/evaluate.py`)와 정확히 같은
    2단계(8-feature 추출 -> `apply_feature_schema()`로 6개만 순서대로
    선택 -> scaler.transform -> decision_function)를 실시간 window에
    적용한다. missing/NaN/Inf/stale 중 하나라도 있으면 score를 만들지
    않고 예외를 던진다(fail-closed) - 호출부가 이 예외를 삼키지 않고
    그대로 올려 signal도 안 보내지게 한다."""
    end = now_fn()
    start = end - timedelta(seconds=WINDOW_SEC)

    def bounded_query_range_fn(promql, s, e):
        return query_range_with_bounded_retry(promql, s, e, query_range_fn=query_range_fn)

    feats, reason = extract_window_strict(start, end, bounded_query_range_fn)
    if feats is None:
        raise RuntimeError(f"fail-closed: feature 결측 - {reason}")
    if any(v is None or math.isnan(v) or math.isinf(v) for v in feats):
        raise RuntimeError(f"fail-closed: feature에 NaN/Inf 포함: {feats}")

    freshness = freshness_check_fn(FRESHNESS_PROBE_QUERY, FRESHNESS_MAX_AGE_SEC)
    if not freshness["fresh"]:
        raise RuntimeError(f"fail-closed: metric stale - {freshness.get('reason')}")

    x6 = apply_feature_schema(feats, schema)
    x_scaled = scaler.transform([x6])
    return float(model.decision_function(x_scaled)[0])


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


def post_to_recovery_policy(score: float, experiment_run_id: str = None, detector: str = "isolation_forest") -> None:
    payload = {
        "signal_type": "anomaly_risk", "score": score,
        "timestamp": datetime.now(timezone.utc).isoformat(), "detector": detector,
    }
    if experiment_run_id:
        payload["experiment_run_id"] = experiment_run_id
    try:
        requests.post(RECOVERY_POLICY_URL, json=payload, timeout=5)
        print(f"  -> 신호 발행: {payload}")
    except requests.exceptions.ConnectionError:
        print(f"  -> recovery-policy 서비스 없음(Phase 7 미구현) - 신호 발행 스킵: {payload}")


def main(artifacts_dir: str, model_version: str, once: bool = False, experiment_run_id: str = None):
    artifacts = load_and_verify_artifacts(Path(artifacts_dir), model_version)
    model, scaler, schema, threshold = artifacts["model"], artifacts["scaler"], artifacts["schema"], artifacts["threshold"]
    print(f"[score_server v3.2b] model_version={model_version} artifacts_dir={artifacts['artifacts_dir']} "
          f"features({len(schema['kept_feature_names'])})={schema['kept_feature_names']} threshold={threshold} "
          f"recovery_policy_url={RECOVERY_POLICY_URL} artifact_hashes={artifacts['artifact_hashes']}")

    consecutive_anomalous = 0
    last_signal_at = None

    while True:
        score = evaluate_v32b(model, scaler, schema)
        now = time.monotonic()
        step = advance_streak(score, threshold, consecutive_anomalous, last_signal_at, now)
        consecutive_anomalous, last_signal_at = step["consecutive_anomalous"], step["last_signal_at"]

        status = "이상" if step["is_anomalous"] else "정상"
        print(f"[{datetime.now(timezone.utc).isoformat()}] score={score:.4f} ({status}), 연속={consecutive_anomalous}")

        if consecutive_anomalous >= CONSECUTIVE_THRESHOLD and not step["should_signal"]:
            remaining = COOLDOWN_SEC - (now - last_signal_at)
            print(f"  -> cooldown 중 (남은 {remaining:.0f}초) - 신호 스킵")
        elif step["should_signal"]:
            post_to_recovery_policy(score, experiment_run_id)

        if once:
            return
        time.sleep(EVAL_INTERVAL_SEC)


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
    args = parser.parse_args()
    try:
        main(artifacts_dir=args.artifacts_dir, model_version=args.model_version,
             once=args.once, experiment_run_id=args.run_id)
    except RuntimeError as e:
        parser.error(str(e))
