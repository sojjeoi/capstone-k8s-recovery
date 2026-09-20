#!/usr/bin/env python3
"""§73.4 - calibration threshold 결정. Calibration split에서만 threshold를
고른다(§4 지시 - `replay.calibrate_threshold()` 재사용, 새 판정 로직
없음). Train/Calibration feature나 model을 다시 건드리지 않는다 -
`train.py`가 이미 저장한 model.pkl/scaler.pkl/feature-schema.json을
읽기만 한다. Holdout은 이 스크립트에서 절대 열지 않는다."""
import json
import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

from feature_selection import apply_feature_schema
from replay import calibrate_threshold
from train import CALIBRATION_SESSIONS, V31_SESSIONS_DIR, _load_session, _valid_rows

ARTIFACTS_DIR = Path(__file__).parent / "artifacts"


def _session_scores(session_id: str, model, scaler, schema) -> list:
    session = _load_session(session_id)
    rows = _valid_rows(session)  # feature_rows는 build_rows_for_session이 시간순으로 생성 - 이미 정렬됨
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
        "selected_from": "calibration split only",
        "consecutive_threshold": 3, "cooldown_sec": 60.0, "eval_interval_sec": 15.0,
        "calibration_sessions": CALIBRATION_SESSIONS,
        "per_session_result": result["per_session"],
        "calibration_failed": False,
        "note": ("가장 민감한(가장 높은) threshold 중 두 calibration session 모두에서 "
                 "false signal episode(score_server.py 상태기계 기준)가 0인 값. "
                 "이후 holdout/train/calibration feature를 다시 보고 이 값을 바꾸지 않는다."),
    }
    (ARTIFACTS_DIR / "threshold.json").write_text(
        json.dumps(threshold_json, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n저장: {ARTIFACTS_DIR / 'threshold.json'}")


if __name__ == "__main__":
    main()
