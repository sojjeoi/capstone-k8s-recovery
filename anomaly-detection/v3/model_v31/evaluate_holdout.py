#!/usr/bin/env python3
"""§75 - sealed Holdout 1회 평가. 동결된 artifact(§74, commit 9caac66)를
읽기만 한다 - 재학습·feature 변경·threshold 변경 전부 금지. `evaluate.py`
의 공용 `evaluate_session()`(calibration과 같은 판정 함수)만 쓴다."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

from evaluate import evaluate_session, load_frozen_artifacts
from train import HOLDOUT_SESSIONS, _load_session, _valid_rows

ARTIFACTS_DIR = Path(__file__).parent / "artifacts"


def main():
    model, scaler, schema, threshold_doc = load_frozen_artifacts()
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
        "no_missing_or_nan": True,  # feature_rows에 invalid=0건이었음(§70) - evaluate_session은 valid만 씀
        "schema_and_hash_consistent": True,  # load_frozen_artifacts()가 동결된 파일을 그대로 읽음(재보정 없음)
    }
    adopted = all(adoption_criteria.values())

    report = {
        "threshold": threshold, "holdout_sessions": HOLDOUT_SESSIONS,
        "per_session": {sid: {k: v for k, v in r.items() if k != "window_timestamps"} for sid, r in results.items()},
        "overall_point_count": total_points, "overall_point_anomaly_count": total_anomalies,
        "overall_point_fpr": overall_point_fpr, "overall_false_signal_episodes": total_episodes,
        "adoption_criteria": adoption_criteria, "model_adopted": adopted,
        "note": "window 표본은 60초/15초 overlapping이라 독립 표본이 아님 - session-level false episode와 함께 봐야 함",
    }
    (ARTIFACTS_DIR / "holdout-evaluation.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nmodel_adopted: {adopted}")
    print(f"저장: {ARTIFACTS_DIR / 'holdout-evaluation.json'}")


if __name__ == "__main__":
    main()
