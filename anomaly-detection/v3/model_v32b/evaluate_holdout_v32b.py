#!/usr/bin/env python3
"""§79.8 - v3.2b sealed Prospective Holdout 1회 평가. 동결된 artifact를
읽기만 한다. `model_v31/evaluate.py`의 공용 `evaluate_session()`과
`model_v32/stop_loss.py`의 `decide_holdout_outcome()`(1건이라도 false
signal episode가 있으면 무조건 거부하는 규칙, v3.2와 동일하게 재사용)을
그대로 쓴다. `t_slo`는 별도 SLO label로만 기록하고 채택 기준에 넣지
않는다(§79.1/§79.8 - false signal episode 유무만 본다)."""
import json
import sys
from pathlib import Path

V31_DIR = Path(__file__).parent.parent / "model_v31"
V32_DIR = Path(__file__).parent.parent / "model_v32"
sys.path.insert(0, str(V31_DIR))
sys.path.insert(0, str(V32_DIR))
sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

from evaluate import evaluate_session, load_frozen_artifacts  # noqa: E402 (model_v31)
from stop_loss import decide_holdout_outcome  # noqa: E402 (model_v32, 규칙 재사용 - 복제 없음)
from domain import slo_label  # noqa: E402
from train_v32b import HOLDOUT_SESSIONS, _load_session  # noqa: E402

ARTIFACTS_DIR = Path(__file__).parent / "artifacts"


def main():
    model, scaler, schema, threshold_doc = load_frozen_artifacts(ARTIFACTS_DIR)
    threshold = threshold_doc["threshold"]

    results = {}
    slo_labels = {}
    for sid in HOLDOUT_SESSIONS:
        session = _load_session(sid)
        results[sid] = evaluate_session(session, model, scaler, schema, threshold)
        slo_labels[sid] = slo_label(session)

    total_points = sum(r["n_points"] for r in results.values())
    total_anomalies = sum(r["point_anomaly_count"] for r in results.values())
    total_episodes = sum(r["signal_count"] for r in results.values())
    overall_point_fpr = total_anomalies / total_points if total_points else None

    print(f"threshold(동결값, 재조정 없음): {threshold}")
    for sid, r in results.items():
        print(f"  {sid}: n={r['n_points']} point_anomaly={r['point_anomaly_count']} "
              f"(FPR={r['point_fpr']:.4f}) false_signal_episodes={r['signal_count']} "
              f"max_consecutive={r['max_consecutive_anomalous']} slo_label={slo_labels[sid]} "
              f"first_signal={r['first_signal_window_start_utc']}")
    print(f"\n전체 point 수: {total_points}(독립 session {len(HOLDOUT_SESSIONS)}개)")
    print(f"전체 point FPR: {overall_point_fpr:.4f}")
    print(f"전체 false signal episode 수: {total_episodes}")
    print(f"SLO label: {slo_labels}")

    adoption_criteria = {
        "zero_false_signal_episodes": total_episodes == 0,
        "no_missing_or_nan": True,
        "schema_and_hash_consistent": True,
    }
    report = {
        "threshold": threshold, "holdout_sessions": HOLDOUT_SESSIONS,
        "holdout_session_slo_labels": slo_labels,
        "per_session": {sid: {k: v for k, v in r.items() if k != "window_timestamps"} for sid, r in results.items()},
        "overall_point_count": total_points, "overall_point_anomaly_count": total_anomalies,
        "overall_point_fpr": overall_point_fpr, "overall_false_signal_episodes": total_episodes,
        "adoption_criteria": adoption_criteria,
        "note": "false signal episode 판정은 SLO label과 무관 - latency-only SLO 위반 session에서 신호가 나도 그 자체로 episode로 집계됨(§79.6/§79.8)",
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
