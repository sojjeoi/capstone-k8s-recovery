#!/usr/bin/env python3
"""§79.6 - v3.2b calibration threshold 결정. `replay.calibrate_threshold()`
(model_v31, 프로토콜 공용 유틸)를 새 Calibration 6세션에 그대로 적용한다.
**Calibration session에 latency-only `t_slo`가 있어도 이 함수는 t_slo를
전혀 보지 않는다** - 오직 `decision_function` score의 연속 3회 조건만
본다. 즉 "SLO 위반이 있어도 모델이 조용하면 그 자체로 유효한 calibration
결과"라는 §79.1의 새 정의가 코드 변경 없이 이미 성립한다."""
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
from domain import slo_label  # noqa: E402
from train_v32b import CALIBRATION_SESSIONS, _load_session, _valid_rows  # noqa: E402

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

    session_scores = {}
    session_slo_labels = {}
    for sid in CALIBRATION_SESSIONS:
        session = _load_session(sid)
        session_scores[sid] = _session_scores(sid, model, scaler, schema)
        session_slo_labels[sid] = slo_label(session)

    for sid, scores in session_scores.items():
        print(f"{sid}: n={len(scores)} min={min(scores):.4f} median={sorted(scores)[len(scores)//2]:.4f} "
              f"max={max(scores):.4f} slo_label={session_slo_labels[sid]}")

    result = calibrate_threshold(session_scores)

    if result["calibration_failed"]:
        print(f"\ncalibration_failed=True - {result['reason']}")
        (ARTIFACTS_DIR / "calibration-result.json").write_text(
            json.dumps({**result, "session_scores": session_scores, "session_slo_labels": session_slo_labels},
                       indent=2, ensure_ascii=False), encoding="utf-8")
        sys.exit(1)

    threshold = result["threshold"]
    print(f"\n선택된 threshold: {threshold:.6f}")
    for sid, r in result["per_session"].items():
        print(f"  {sid}: point_anomaly={r['point_anomaly_count']}/{r['n_points']} "
              f"(FPR={r['point_fpr']:.4f}) false_signal_episodes={r['signal_count']} "
              f"max_consecutive={r['max_consecutive_anomalous']} slo_label={session_slo_labels[sid]}")

    threshold_json = {
        "threshold": threshold,
        "decision_rule": "anomaly if decision_function(x) < threshold (strict, score_server.py:82와 동일)",
        "selected_from": "v3.2b calibration split only (6 new sessions)",
        "consecutive_threshold": 3, "cooldown_sec": 60.0, "eval_interval_sec": 15.0,
        "calibration_sessions": CALIBRATION_SESSIONS,
        "calibration_session_slo_labels": session_slo_labels,
        "per_session_result": result["per_session"],
        "calibration_failed": False,
        "note": ("false signal episode는 t_slo와 무관하게 순수 score 상태기계 기준(§79.6) - "
                 "latency-only SLO 위반이 있어도 모델이 연속 3회 조건을 안 만들면 episode 0으로 유효."),
    }
    (ARTIFACTS_DIR / "threshold.json").write_text(
        json.dumps(threshold_json, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n저장: {ARTIFACTS_DIR / 'threshold.json'}")


if __name__ == "__main__":
    main()
