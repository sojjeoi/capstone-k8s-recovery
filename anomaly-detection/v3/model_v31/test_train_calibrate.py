#!/usr/bin/env python3
"""§8 - train.py/calibrate.py/evaluate.py/integrity.py 검증. 실제
클러스터·Prometheus 의존 없음(v31_data/sessions의 이미 수집된 JSON과
합성 데이터만 사용)."""
import copy
import hashlib
import json
import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

from evaluate import evaluate_session, load_frozen_artifacts, score_session_rows
from feature_selection import apply_feature_schema, compute_feature_schema
from integrity import verify_sha256sums
from train import CALIBRATION_SESSIONS, HOLDOUT_SESSIONS, MODEL_PARAMS, TRAIN_SESSIONS

ARTIFACTS_DIR = Path(__file__).parent / "artifacts"

_SYNTH_ROWS = [
    [1.0 + 0.01 * i, 0.1 * (i % 3 - 1), 7e9 + i * 1e5, 100.0 * (i % 5 - 2), 0.0, 0.0, 0.001 * (i % 2), 0.0]
    for i in range(40)
]


def test_split_sessions_are_disjoint():
    """train/calibration/holdout에 같은 session_id가 두 번 들어가면 안
    된다(누출 방지) - §65.1/§69.4의 "역할 교환 없음" 원칙과 별개로,
    같은 세션이 두 split에 동시에 배정되는 실수 자체를 코드로 막는다."""
    train_set, calib_set, holdout_set = set(TRAIN_SESSIONS), set(CALIBRATION_SESSIONS), set(HOLDOUT_SESSIONS)
    assert not (train_set & calib_set), "train/calibration 중복"
    assert not (train_set & holdout_set), "train/holdout 중복"
    assert not (calib_set & holdout_set), "calibration/holdout 중복"
    print("OK - train/calibration/holdout session_id가 서로 겹치지 않음")


def test_replay_constants_match_score_server_py():
    """replay.py의 기본값이 anomaly-detection/score_server.py의 실제
    상수와 어긋나면(누군가 score_server.py만 고치고 replay.py를 안
    고치면) 이 테스트가 즉시 잡아낸다.

    §86(2026-09-21) v3.2b 통합 이후 SCORE_THRESHOLD는 더 이상 하드코딩된
    상수가 아니다 - `load_and_verify_artifacts()`가 동결 threshold.json
    에서 읽고, 그 값이 `evaluate_v32b()`의 `score < threshold`(엄격한
    미만, 문자 그대로 이 비교 자체는 그대로)로 쓰인다. 이 테스트는 그
    상수 자체(CONSECUTIVE_THRESHOLD/COOLDOWN_SEC/EVAL_INTERVAL_SEC)와
    엄격한 미만 비교 관례만 계속 확인한다."""
    score_server_path = Path(__file__).parent.parent.parent / "score_server.py"
    src = score_server_path.read_text(encoding="utf-8")
    assert "CONSECUTIVE_THRESHOLD = 3" in src
    assert "COOLDOWN_SEC = 60" in src
    assert "EVAL_INTERVAL_SEC = 15" in src
    assert "score < threshold" in src  # 엄격한 미만 - replay.py도 `score < threshold`
    import inspect

    from replay import replay_detector
    sig = inspect.signature(replay_detector)
    assert sig.parameters["consecutive_threshold"].default == 3
    assert sig.parameters["cooldown_sec"].default == 60.0
    assert sig.parameters["eval_interval_sec"].default == 15.0
    print("OK - replay.py 기본값이 score_server.py의 실제 상수와 일치")


def test_model_params_match_v1_explicitly():
    """v1(anomaly-detection/train.py)과 비교 가능해야 하므로 핵심
    하이퍼파라미터가 동일해야 한다 - 하이퍼파라미터 탐색 없음."""
    assert MODEL_PARAMS["n_estimators"] == 100
    assert MODEL_PARAMS["contamination"] == "auto"
    assert MODEL_PARAMS["random_state"] == 42
    print("OK - n_estimators/contamination/random_state가 v1과 동일")


def test_training_reproducible_with_same_seed():
    """동일 입력·동일 환경에서 재학습하면 score와 threshold(재현 가능한
    부분)가 완전히 같아야 한다 - 합성 데이터로 결정론성만 확인(실제
    75행 재학습은 train.py를 두 번 실행해 pkl 바이트 동일함을 이미
    수동 검증함, §74)."""
    schema = compute_feature_schema(_SYNTH_ROWS)
    X = [apply_feature_schema(r, schema) for r in _SYNTH_ROWS]

    def fit_once():
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)
        model = IsolationForest(**MODEL_PARAMS)
        model.fit(X_scaled)
        return model.decision_function(X_scaled)

    scores_a = fit_once()
    scores_b = fit_once()
    assert list(scores_a) == list(scores_b)
    print("OK - 동일 seed·동일 입력이면 decision_function 점수가 완전히 재현됨")


def test_sha256sums_detects_tampering(tmp_path):
    (tmp_path / "a.json").write_text('{"x":1}', encoding="utf-8")
    (tmp_path / "b.json").write_text('{"y":2}', encoding="utf-8")
    manifest = {name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() for name in ("a.json", "b.json")}
    (tmp_path / "SHA256SUMS.json").write_text(json.dumps(manifest), encoding="utf-8")

    assert verify_sha256sums(tmp_path) == []  # 조작 전 - 전부 일치

    (tmp_path / "a.json").write_text('{"x":999}', encoding="utf-8")  # 조작
    mismatches = verify_sha256sums(tmp_path)
    assert len(mismatches) == 1 and "a.json" in mismatches[0]
    print("OK - manifest와 실제 파일 SHA가 다르면 정확히 탐지")


def test_sha256sums_detects_missing_file(tmp_path):
    (tmp_path / "a.json").write_text('{"x":1}', encoding="utf-8")
    manifest = {"a.json": hashlib.sha256((tmp_path / "a.json").read_bytes()).hexdigest(), "missing.json": "deadbeef"}
    (tmp_path / "SHA256SUMS.json").write_text(json.dumps(manifest), encoding="utf-8")
    mismatches = verify_sha256sums(tmp_path)
    assert any("missing.json" in m for m in mismatches)
    print("OK - manifest에 있는데 파일이 없으면 탐지")


def _frozen_artifacts_exist() -> bool:
    return (ARTIFACTS_DIR / "model.pkl").exists() and (ARTIFACTS_DIR / "threshold.json").exists()


def test_evaluate_session_does_not_modify_artifacts():
    """§75/§76 - holdout/challenge 평가가 artifact 파일을 전혀 바꾸지
    않아야 한다(재학습·재보정 금지). 이미 학습된 실제 artifacts/를
    대상으로, 평가 함수 호출 전후 SHA256SUMS 무결성이 그대로 유지되는지
    확인한다."""
    if not _frozen_artifacts_exist():
        import pytest
        pytest.skip("train.py/calibrate.py를 먼저 실행해야 하는 로컬 산출물 - 오프라인 스위트 필수 대상 아님")
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in ARTIFACTS_DIR.glob("*") if p.name != "calibration-result.json"}

    model, scaler, schema, threshold_doc = load_frozen_artifacts()
    dummy_session = {
        "session_id": "dummy",
        "feature_rows": [{"valid": True, "window_start_utc": "2026-01-01T00:00:00+00:00",
                           "features": apply_feature_schema_inverse(schema)}],
    }
    evaluate_session(dummy_session, model, scaler, schema, threshold_doc["threshold"])

    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
             for p in ARTIFACTS_DIR.glob("*") if p.name != "calibration-result.json"}
    assert before == after
    print("OK - evaluate_session() 호출이 artifact 파일을 전혀 바꾸지 않음")


def apply_feature_schema_inverse(schema):
    """테스트 전용 - kept feature 개수만큼 원본 8열짜리 dummy row를 만든다
    (원본 열 순서, kept 위치만 1.0, 나머지 0.0 - schema 검증용이 아니라
    evaluate_session()이 예외 없이 돌아가기만 하면 되는 형태)."""
    row = [0.0] * len(schema["original_feature_names"])
    for idx in schema["kept_feature_indices"]:
        row[idx] = 1.0
    return row


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        try:
            t()
        except TypeError:
            pass  # tmp_path fixture 필요한 테스트는 pytest로만 실행
    print(f"전체 실행 시도 ({len(tests)}개) - tmp_path 필요한 테스트는 pytest로 실행하세요")
