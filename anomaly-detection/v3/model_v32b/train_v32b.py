#!/usr/bin/env python3
"""§79 - v3.2b Isolation Forest 학습. v3.1/v3.2와 완전히 동일한
하이퍼파라미터·절차를 쓰되, Training 세션 구성 규칙이 다르다: **infra-
structure-normal이면 `t_slo` 존재 여부와 무관하게 Training 후보**(§78의
survivorship bias 발견에 대한 조치, §79.1). `replay.py`/`feature_
selection.py`는 model_v31에서 그대로 import한다(프로토콜 공용 유틸)."""
import hashlib
import json
import pickle
import sys
from pathlib import Path

V31_DIR = Path(__file__).parent.parent / "model_v31"
V3_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(V31_DIR))
sys.path.insert(0, str(V3_DIR))
sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

from domain import classify_exclusion_reasons, slo_label  # noqa: E402
from feature_selection import apply_feature_schema, compute_feature_schema  # noqa: E402 (model_v31)
from build_dataset import _git_commit_sha  # noqa: E402

V31_SESSIONS_DIR = V3_DIR / "v31_data" / "sessions"
ARTIFACTS_DIR = Path(__file__).parent / "artifacts"

# §79.2 Training registry - protocol(active_plus_preview, 600초, 0.025 RPS
# 또는 idle)과 정확히 일치하고 infrastructure-normal인 세션 전부. t_slo
# 존재 여부는 포함 기준이 아니다(§79.1) - calib2-low-02도 포함된다.
TRAIN_SESSIONS = [
    "v31-train-idle-20260920", "v31-calib-idle-20260920", "v31-holdout-idle-20260920", "calib2-idle-01",
    "v31-train-low_load-20260920", "v31-calib-low_load-20260920", "v31-holdout-low_load-20260920",
    "calib2-low-01", "calib2-low-02",
]
# 짧은(180초) 프로토콜이라 제외 - §79.2 사유 기록.
TRAIN_EXCLUDED_PROTOCOL_MISMATCH = {
    "q3c-low_load-20260920-r2": "180초 세션(§64 qualification) - v3.1/v3.2b 600초 프로토콜과 다름",
    "official-train-low_load-20260920": "180초 세션(§66) - 프로토콜 다름",
    "official-calib-low_load-20260920": "180초 세션(§68) - 프로토콜 다름",
}
CALIBRATION_SESSIONS = [
    "calib3-idle-01", "calib3-low-01", "calib3-low-02", "calib3-idle-02", "calib3-idle-03", "calib3-low-03",
]
HOLDOUT_SESSIONS = [
    "holdout3-low-01", "holdout3-idle-01", "holdout3-idle-02", "holdout3-low-02", "holdout3-low-03", "holdout3-idle-03",
]

MODEL_PARAMS = {
    "n_estimators": 100, "contamination": "auto", "random_state": 42,
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
    # fail-closed 검증 - 전부 실제로 infrastructure-normal이어야 한다(사람이 목록을 손으로 관리하므로 코드로 재확인).
    for s in train_sessions:
        cls = classify_exclusion_reasons(s.get("exclusion_reasons"))
        if not cls["infrastructure_normal"]:
            raise RuntimeError(f"fail-closed: {s['session_id']}이 infrastructure-normal이 아님: {cls['infrastructure_issues']}")

    train_matrix_full = []
    per_session_row_counts = {}
    per_session_slo_labels = {}
    for sid, session in zip(TRAIN_SESSIONS, train_sessions):
        rows = _valid_rows(session)
        per_session_row_counts[sid] = len(rows)
        per_session_slo_labels[sid] = slo_label(session)
        train_matrix_full.extend(rows)

    idle_count = sum(1 for s in train_sessions if s["profile"] == "idle")
    low_load_count = sum(1 for s in train_sessions if s["profile"] == "low_load")
    print(f"독립 Train session 수: {len(TRAIN_SESSIONS)}개(idle {idle_count} + low_load {low_load_count})")
    print(f"Train 행 수(session별): {per_session_row_counts}")
    print(f"SLO label(session별): {per_session_slo_labels}")
    print(f"Train 총 행 수: {len(train_matrix_full)}개(독립 session {len(TRAIN_SESSIONS)}개의 겹치는 window)")
    print(f"제외된 protocol-mismatch 세션: {TRAIN_EXCLUDED_PROTOCOL_MISMATCH}")

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
        "train_slo_labels_by_session": per_session_slo_labels,
        "train_independent_session_count": len(TRAIN_SESSIONS),
        "train_idle_session_count": idle_count, "train_low_load_session_count": low_load_count,
        "train_total_rows": len(train_matrix_full),
        "train_excluded_protocol_mismatch": TRAIN_EXCLUDED_PROTOCOL_MISMATCH,
        "note": ("t_slo 존재 여부는 Training 포함 기준이 아니다(§79.1) - infrastructure-normal이면 포함. "
                 "row 수는 겹치는 window라 독립 표본 수는 session 수를 봐야 한다."),
    }
    (ARTIFACTS_DIR / "split-manifest.json").write_text(
        json.dumps(split_manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    dataset_manifest = {
        "train_sessions": [{"session_id": s["session_id"], "regime": s["profile"],
                             "slo_label": slo_label(s), "valid_rows": len(_valid_rows(s)),
                             "session_json_sha256": _sha256_file(V31_SESSIONS_DIR / f"{s['session_id']}.json"),
                             "git_commit_sha": s.get("git_commit_sha"), "ramp_config_sha256": s.get("ramp_config_sha256")}
                            for s in train_sessions],
    }
    (ARTIFACTS_DIR / "dataset-manifest.json").write_text(
        json.dumps(dataset_manifest, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    import numpy
    import sklearn
    training_metadata = {
        "model_version": "v3.2b",
        "normal_domain_definition": "infrastructure-normal (Chaos/fault/restart/OOM/Node/Endpoint/promotion/cleanup/success-rate/metric-completeness) - t_slo excluded from eligibility, tracked as separate SLO label",
        "model_params": MODEL_PARAMS,
        "scaler_class": "sklearn.preprocessing.StandardScaler (defaults)",
        "python_version": sys.version,
        "sklearn_version": sklearn.__version__,
        "numpy_version": numpy.__version__,
        "training_commit_sha": _git_commit_sha(),
        "model_offset_sklearn_internal_not_used_as_threshold": float(model.offset_),
    }
    (ARTIFACTS_DIR / "training-metadata.json").write_text(
        json.dumps(training_metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    sha_manifest = {p.name: _sha256_file(p) for p in sorted(ARTIFACTS_DIR.glob("*")) if p.is_file()}
    (ARTIFACTS_DIR / "SHA256SUMS.json").write_text(
        json.dumps(sha_manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n저장: {model_path}, {scaler_path}, feature-schema.json, split-manifest.json, "
          f"dataset-manifest.json, training-metadata.json, SHA256SUMS.json")


if __name__ == "__main__":
    main()
