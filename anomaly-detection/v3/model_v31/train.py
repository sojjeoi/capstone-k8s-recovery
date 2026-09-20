#!/usr/bin/env python3
"""§73.3 - v3.1 Isolation Forest 학습. v1(anomaly-detection/train.py)과
비교 가능하도록 하이퍼파라미터 탐색을 하지 않고 v1과 동일한 값을 그대로
쓴다(`n_estimators=100, contamination="auto", random_state=42`, 나머지는
sklearn 기본값 - `training-metadata.json`에 명시 기록). scaler·model
전부 **Train split에만** fit한다. `contamination="auto"`가 내부적으로
만드는 `model.offset_`은 운영 threshold로 쓰지 않는다 - 그건 `calibrate.py`
가 calibration split에서 별도로 정해 `threshold.json`에 저장한다."""
import hashlib
import json
import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))  # v3/ (windows.py는 안 씀, 참고용)
sys.stdout.reconfigure(encoding="utf-8")

from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

from feature_selection import apply_feature_schema, compute_feature_schema
from build_dataset import _git_commit_sha  # noqa: E402

V3_DIR = Path(__file__).parent.parent
V31_SESSIONS_DIR = V3_DIR / "v31_data" / "sessions"
ARTIFACTS_DIR = Path(__file__).parent / "artifacts"

TRAIN_SESSIONS = ["v31-train-idle-20260920", "v31-train-low_load-20260920"]
CALIBRATION_SESSIONS = ["v31-calib-low_load-20260920", "v31-calib-idle-20260920"]
HOLDOUT_SESSIONS = ["v31-holdout-idle-20260920", "v31-holdout-low_load-20260920"]

MODEL_PARAMS = {
    "n_estimators": 100, "contamination": "auto", "random_state": 42,
    # 나머지는 v1과 동일하게 sklearn 기본값을 그대로 씀(explicit) - 하이퍼파라미터 탐색 안 함.
    "max_samples": "auto", "max_features": 1.0, "bootstrap": False, "n_jobs": None,
    "verbose": 0, "warm_start": False,
}


def _load_session(session_id: str) -> dict:
    return json.loads((V31_SESSIONS_DIR / f"{session_id}.json").read_text(encoding="utf-8"))


def _valid_rows(session: dict) -> list:
    return [r["features"] for r in session["feature_rows"] if r["valid"]]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

    train_sessions = [_load_session(sid) for sid in TRAIN_SESSIONS]
    train_matrix_full = []
    per_session_row_counts = {}
    for sid, session in zip(TRAIN_SESSIONS, train_sessions):
        rows = _valid_rows(session)
        per_session_row_counts[sid] = len(rows)
        train_matrix_full.extend(rows)

    print(f"독립 Train session 수: {len(TRAIN_SESSIONS)}개 - {TRAIN_SESSIONS}")
    print(f"Train 행 수(overlapping window 포함, session별): {per_session_row_counts}")
    print(f"Train 총 행 수: {len(train_matrix_full)}개 (독립 session {len(TRAIN_SESSIONS)}개의 window들 - "
          f"{len(train_matrix_full)}개의 독립 표본이 아님, 시간축으로 겹치는 window)")

    schema = compute_feature_schema(train_matrix_full)
    print(f"\nkept features: {schema['kept_feature_names']}")
    print(f"removed features: {schema['removed_features']}")

    X = [apply_feature_schema(row, schema) for row in train_matrix_full]

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    model = IsolationForest(**MODEL_PARAMS)
    model.fit(X_scaled)

    model_path = ARTIFACTS_DIR / "model.pkl"
    scaler_path = ARTIFACTS_DIR / "scaler.pkl"
    with model_path.open("wb") as f:
        pickle.dump(model, f)
    with scaler_path.open("wb") as f:
        pickle.dump(scaler, f)

    (ARTIFACTS_DIR / "feature-schema.json").write_text(
        json.dumps(schema, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    split_manifest = {
        "train_sessions": TRAIN_SESSIONS, "calibration_sessions": CALIBRATION_SESSIONS,
        "holdout_sessions": HOLDOUT_SESSIONS,
        "train_row_counts_by_session": per_session_row_counts,
        "train_independent_session_count": len(TRAIN_SESSIONS),
        "train_total_rows": len(train_matrix_full),
        "note": ("row 수는 60초 window/15초 step으로 겹치는 시계열 표본이다 - "
                 "독립 표본 수는 session 수(위 train_independent_session_count)를 봐야 한다."),
    }
    (ARTIFACTS_DIR / "split-manifest.json").write_text(
        json.dumps(split_manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    dataset_manifest = {
        "train_sessions": [{"session_id": s["session_id"], "regime": s["profile"], "split_role": s["split_role"],
                             "valid_rows": len(_valid_rows(s)), "git_commit_sha": s.get("git_commit_sha"),
                             "ramp_config_sha256": s.get("ramp_config_sha256")}
                            for s in train_sessions],
    }
    (ARTIFACTS_DIR / "dataset-manifest.json").write_text(
        json.dumps(dataset_manifest, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    import sklearn
    import numpy
    training_metadata = {
        "model_params": MODEL_PARAMS,
        "scaler_class": "sklearn.preprocessing.StandardScaler (defaults)",
        "python_version": sys.version,
        "sklearn_version": sklearn.__version__,
        "numpy_version": numpy.__version__,
        "training_commit_sha": _git_commit_sha(),
        "note_on_contamination_auto": ("model.offset_(contamination='auto'가 내부적으로 만드는 sklearn 자체 "
                                        "임계값)는 운영 threshold로 쓰지 않는다 - calibrate.py가 별도로 정함"),
        "model_offset_sklearn_internal_not_used_as_threshold": float(model.offset_),
    }
    (ARTIFACTS_DIR / "training-metadata.json").write_text(
        json.dumps(training_metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    sha_manifest = {p.name: _sha256_file(p) for p in sorted(ARTIFACTS_DIR.glob("*"))}
    (ARTIFACTS_DIR / "SHA256SUMS.json").write_text(
        json.dumps(sha_manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n저장: {model_path}, {scaler_path}, feature-schema.json, split-manifest.json, "
          f"dataset-manifest.json, training-metadata.json, SHA256SUMS.json")
    print(f"model.offset_(참고용, 운영 threshold 아님): {model.offset_:.6f}")


if __name__ == "__main__":
    main()
