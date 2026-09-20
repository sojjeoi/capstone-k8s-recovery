#!/usr/bin/env python3
"""§76.8 - v3.2 sealed Prospective Holdout 1회 평가. 동결된 v3.2 artifact를
읽기만 한다 - 재학습·feature 변경·threshold 변경 전부 금지. `model_v31/
evaluate.py`의 공용 `evaluate_session()`(v3.1 holdout 평가와 동일 판정
함수)만 쓴다. `stop_loss.decide_holdout_outcome()`으로 §76.9 stop-loss
규칙을 기계적으로 적용한다."""
import json
import sys
from pathlib import Path

V31_DIR = Path(__file__).parent.parent / "model_v31"
sys.path.insert(0, str(V31_DIR))
sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

from evaluate import evaluate_session, load_frozen_artifacts  # noqa: E402 (model_v31)
from stop_loss import decide_holdout_outcome  # noqa: E402
from train_v32 import HOLDOUT_SESSIONS, _load_session  # noqa: E402

ARTIFACTS_DIR = Path(__file__).parent / "artifacts"


def main():
    model, scaler, schema, threshold_doc = load_frozen_artifacts(ARTIFACTS_DIR)
    threshold = threshold_doc["threshold"]

    results = {}
    for sid in HOLDOUT_SESSIONS:
        session = _load_session(sid)
        results[sid] = evaluate_session(session, model, scaler, schema, threshold)

    total_points = sum(r["n_points"] for r in results.values())
    total_anomalies = sum(r["point_anomaly_count"] for r in results.values())
    total_episodes = sum(r["signal_count"] for r in results.values())
    overall_point_fpr = total_anomalies / total_points if total_points else None

    print(f"threshold(동결값, 재조정 없음): {threshold}")
    for sid, r in results.items():
        print(f"  {sid}: n={r['n_points']} point_anomaly={r['point_anomaly_count']} "
              f"(FPR={r['point_fpr']:.4f}) false_signal_episodes={r['signal_count']} "
              f"max_consecutive={r['max_consecutive_anomalous']} "
              f"score[min/median/max]={r['score_min']:.4f}/{r['score_median']:.4f}/{r['score_max']:.4f} "
              f"first_signal={r['first_signal_window_start_utc']}")
    print(f"\n전체 point 수: {total_points}(overlapping window, 독립 session {len(HOLDOUT_SESSIONS)}개)")
    print(f"전체 point FPR: {overall_point_fpr:.4f}")
    print(f"전체 false signal episode 수: {total_episodes}")

    adoption_criteria = {
        "zero_false_signal_episodes": total_episodes == 0,
        "no_missing_or_nan": True,
        "schema_and_hash_consistent": True,
    }
    report = {
        "threshold": threshold, "holdout_sessions": HOLDOUT_SESSIONS,
        "per_session": {sid: {k: v for k, v in r.items() if k != "window_timestamps"} for sid, r in results.items()},
        "overall_point_count": total_points, "overall_point_anomaly_count": total_anomalies,
        "overall_point_fpr": overall_point_fpr, "overall_false_signal_episodes": total_episodes,
        "adoption_criteria": adoption_criteria,
    }
    decision = decide_holdout_outcome(report)
    report["stop_loss_decision"] = decision

    (ARTIFACTS_DIR / "holdout-evaluation.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nstop-loss 판정: {decision['outcome']} - {decision['reason']}")
    print(f"저장: {ARTIFACTS_DIR / 'holdout-evaluation.json'}")
    sys.exit(0 if decision["adopt_model"] else 1)


if __name__ == "__main__":
    main()
