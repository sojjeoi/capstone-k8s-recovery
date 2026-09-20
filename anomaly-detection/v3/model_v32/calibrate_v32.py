#!/usr/bin/env python3
"""§76.6 - v3.2 calibration threshold 결정. v3.1(model_v31/calibrate.py)과
완전히 동일한 규칙 - `replay.calibrate_threshold()`(model_v31에서 import,
프로토콜 공용 유틸)를 새 Calibration 6세션에만 적용한다. Train/Calibration
feature나 model을 다시 건드리지 않는다."""
import json
import pickle
import sys
from pathlib import Path

V31_DIR = Path(__file__).parent.parent / "model_v31"
sys.path.insert(0, str(V31_DIR))
sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

from feature_selection import apply_feature_schema  # noqa: E402 (model_v31)
from replay import calibrate_threshold  # noqa: E402 (model_v31)
from train_v32 import CALIBRATION_SESSIONS, _load_session, _valid_rows  # noqa: E402

ARTIFACTS_DIR = Path(__file__).parent / "artifacts"


def _session_scores(session_id: str, model, scaler, schema) -> list:
    session = _load_session(session_id)
    rows = _valid_rows(session)
    X = [apply_feature_schema(row, schema) for row in rows]
    if not X:
        return []
    X_scaled = scaler.transform(X)
    return [float(s) for s in model.decision_function(X_scaled)]


def main():
    model = pickle.loads((ARTIFACTS_DIR / "model.pkl").read_bytes())
    scaler = pickle.loads((ARTIFACTS_DIR / "scaler.pkl").read_bytes())
    schema = json.loads((ARTIFACTS_DIR / "feature-schema.json").read_text(encoding="utf-8"))

    session_scores = {sid: _session_scores(sid, model, scaler, schema) for sid in CALIBRATION_SESSIONS}
    for sid, scores in session_scores.items():
        print(f"{sid}: n={len(scores)} min={min(scores):.4f} median={sorted(scores)[len(scores)//2]:.4f} max={max(scores):.4f}")

    result = calibrate_threshold(session_scores)

    if result["calibration_failed"]:
        print(f"\ncalibration_failed=True - {result['reason']}")
        print("threshold를 동결하지 않는다. 이후 단계(artifact freeze, holdout 개봉)를 진행하지 않는다.")
        (ARTIFACTS_DIR / "calibration-result.json").write_text(
            json.dumps({**result, "session_scores": session_scores}, indent=2, ensure_ascii=False), encoding="utf-8")
        sys.exit(1)

    threshold = result["threshold"]
    print(f"\n선택된 threshold: {threshold:.6f}")
    for sid, r in result["per_session"].items():
        print(f"  {sid}: point_anomaly={r['point_anomaly_count']}/{r['n_points']} "
              f"(FPR={r['point_fpr']:.4f}) false_signal_episodes={r['signal_count']} "
              f"max_consecutive={r['max_consecutive_anomalous']} "
              f"score[min/median/max]={r['score_min']:.4f}/{r['score_median']:.4f}/{r['score_max']:.4f}")

    threshold_json = {
        "threshold": threshold,
        "decision_rule": "anomaly if decision_function(x) < threshold (strict, score_server.py:82와 동일)",
        "selected_from": "v3.2 calibration split only (6 new sessions)",
        "consecutive_threshold": 3, "cooldown_sec": 60.0, "eval_interval_sec": 15.0,
        "calibration_sessions": CALIBRATION_SESSIONS,
        "per_session_result": result["per_session"],
        "calibration_failed": False,
        "diff_from_v31_threshold": "v3.1 threshold was 0.013299(2 calibration sessions) - see this value's own number for v3.2(6 calibration sessions)",
    }
    (ARTIFACTS_DIR / "threshold.json").write_text(
        json.dumps(threshold_json, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n저장: {ARTIFACTS_DIR / 'threshold.json'}")


if __name__ == "__main__":
    main()
