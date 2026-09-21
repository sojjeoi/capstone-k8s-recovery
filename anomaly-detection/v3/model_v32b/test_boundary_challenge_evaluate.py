#!/usr/bin/env python3
"""§84 - boundary challenge 해석 로직 오프라인 고정 테스트. 실제 score
계산(evaluate_session/score_session_rows)은 fake로 주입해 이 파일의
해석 규칙(lead time·early/late/missed·streak timeline·집계·A/B/C/D)만
검증한다. 클러스터·실제 model artifact 의존 없음."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

from boundary_challenge_evaluate import (  # noqa: E402
    aggregate_actual_violation, aggregate_safe_transient, anomaly_streak_timeline,
    classify_detection, classify_overall, compute_lead_time_sec, evaluate_challenge_session,
)


def test_classify_detection_early_late_missed():
    assert classify_detection("2026-01-01T00:01:00+00:00", "2026-01-01T00:00:30+00:00") == "early_detection"
    assert classify_detection("2026-01-01T00:01:00+00:00", "2026-01-01T00:01:30+00:00") == "late_detection"
    assert classify_detection("2026-01-01T00:01:00+00:00", None) == "missed"
    assert classify_detection("2026-01-01T00:01:00+00:00", "2026-01-01T00:01:00+00:00") == "early_detection"
    print("OK - t_detection<=t_slo=early, >t_slo=late, signal 없음=missed, 동시=early(0 lead time)")


def test_classify_detection_none_for_safe_transient():
    assert classify_detection(None, "2026-01-01T00:00:30+00:00") is None
    print("OK - t_slo가 없는 세션(safe_transient)은 분류 대상 아님")


def test_compute_lead_time_sec_sign_convention():
    assert compute_lead_time_sec("2026-01-01T00:01:00+00:00", "2026-01-01T00:00:30+00:00") == 30.0
    assert compute_lead_time_sec("2026-01-01T00:01:00+00:00", "2026-01-01T00:01:30+00:00") == -30.0
    assert compute_lead_time_sec("2026-01-01T00:01:00+00:00", "2026-01-01T00:01:00+00:00") == 0.0
    assert compute_lead_time_sec("2026-01-01T00:01:00+00:00", None) is None
    print("OK - lead_time_sec 양수=선제/음수=사후/0=동시/None=signal 없음")


def test_anomaly_streak_timeline_resets_on_normal():
    ts = ["t0", "t1", "t2", "t3", "t4"]
    scores = [-0.1, -0.1, 0.2, -0.1, -0.1]
    threshold = 0.0
    timeline = anomaly_streak_timeline(ts, scores, threshold)
    counts = [row["consecutive_count"] for row in timeline]
    assert counts == [1, 2, 0, 1, 2]
    assert timeline[2]["is_anomalous"] is False
    print("OK - streak timeline이 정상 1회에 즉시 0으로 reset되고 그 뒤 다시 누적됨")


def _fake_evaluate_session_fn(signal_count, first_signal_ts):
    def fn(session, model, scaler, schema, threshold):
        return {
            "session_id": session["session_id"], "n_points": 3, "point_anomaly_count": 1,
            "point_fpr": 0.33, "signal_indices": [0] if signal_count else [],
            "signal_count": signal_count, "max_consecutive_anomalous": 3 if signal_count else 1,
            "score_min": -0.2, "score_median": 0.0, "score_max": 0.2,
            "first_signal_window_start_utc": first_signal_ts,
            "window_timestamps": ["t0", "t1", "t2"],
        }
    return fn


def _fake_score_session_rows_fn():
    def fn(session, model, scaler, schema):
        return [("t0", -0.2), ("t1", 0.0), ("t2", 0.2)]
    return fn


def test_evaluate_challenge_session_safe_transient_no_signal():
    session = {"session_id": "q3c-burst-20260920-r1", "t_slo": None, "profile": "burst", "excluded": False}
    result = evaluate_challenge_session(
        session, "burst_safe", None, None, None, -0.05,
        evaluate_session_fn=_fake_evaluate_session_fn(0, None),
        score_session_rows_fn=_fake_score_session_rows_fn(),
    )
    assert result["category"] == "safe_transient"
    assert result["unnecessary_signal"] is False
    assert result["detection_class"] is None
    print("OK - safe_transient + signal 없음 -> unnecessary_signal=False")


def test_evaluate_challenge_session_safe_transient_with_signal_flagged_unnecessary():
    session = {"session_id": "q3c-burst-20260920-r1", "t_slo": None, "profile": "burst", "excluded": False}
    result = evaluate_challenge_session(
        session, "burst_safe", None, None, None, -0.05,
        evaluate_session_fn=_fake_evaluate_session_fn(1, "t0"),
        score_session_rows_fn=_fake_score_session_rows_fn(),
    )
    assert result["unnecessary_signal"] is True
    print("OK - safe_transient인데 signal 발생 -> unnecessary_signal=True")


def test_evaluate_challenge_session_actual_violation_early_detection():
    session = {"session_id": "official-train-sustained_load-20260920",
               "t_slo": "2026-01-01T00:05:00+00:00", "profile": "sustained_load", "excluded": True}
    result = evaluate_challenge_session(
        session, "sustained_load_violation", None, None, None, -0.05,
        evaluate_session_fn=_fake_evaluate_session_fn(1, "2026-01-01T00:04:00+00:00"),
        score_session_rows_fn=_fake_score_session_rows_fn(),
    )
    assert result["detection_class"] == "early_detection"
    assert result["lead_time_sec"] == 60.0
    assert result["unnecessary_signal"] is None
    print("OK - actual_violation + t_detection<t_slo -> early_detection, lead_time_sec 양수")


def test_evaluate_challenge_session_actual_violation_missed():
    session = {"session_id": "official-calib-burst-20260920",
               "t_slo": "2026-01-01T00:05:00+00:00", "profile": "burst", "excluded": True}
    result = evaluate_challenge_session(
        session, "burst_violation", None, None, None, -0.05,
        evaluate_session_fn=_fake_evaluate_session_fn(0, None),
        score_session_rows_fn=_fake_score_session_rows_fn(),
    )
    assert result["detection_class"] == "missed"
    assert result["lead_time_sec"] is None
    print("OK - actual_violation + signal 없음 -> missed, lead_time_sec None")


def test_aggregate_safe_transient_counts():
    results = [
        {"session_id": "a", "role": "burst_safe", "unnecessary_signal": False, "signal_count": 0},
        {"session_id": "b", "role": "burst_safe", "unnecessary_signal": True, "signal_count": 1},
        {"session_id": "c", "role": "sustained_load_pass", "unnecessary_signal": False, "signal_count": 0},
    ]
    agg = aggregate_safe_transient(results)
    assert agg["n_sessions"] == 3
    assert agg["unnecessary_signal_count"] == 1
    assert abs(agg["unnecessary_signal_fraction"] - 1 / 3) < 1e-9
    print("OK - safe transient 집계: unnecessary signal 1/3 정확히 계산")


def test_aggregate_actual_violation_two_samples_no_overreach():
    results = [
        {"detection_class": "early_detection", "lead_time_sec": 45.0},
        {"detection_class": "late_detection", "lead_time_sec": -10.0},
    ]
    agg = aggregate_actual_violation(results)
    assert agg["early_detection_count"] == 1
    assert agg["late_detection_count"] == 1
    assert agg["missed_count"] == 0
    assert agg["lead_time_range_sec"] == [-10.0, 45.0]
    assert "표본 2개" in agg["note"]
    print("OK - actual violation 2건 집계 - 통계적 우월성 주장 없이 median/range만 보고")


def test_classify_overall_promising():
    safe_agg = {"unnecessary_signal_count": 0}
    violation_agg = {"n_sessions": 2, "early_detection_count": 1, "late_detection_count": 1, "missed_count": 0}
    result = classify_overall(safe_agg, violation_agg, holdout_adopted=True)
    assert result["classification"] == "A"
    print("OK - safe 0건 + early>=1 + holdout adopted -> A(Promising)")


def test_classify_overall_over_sensitive():
    safe_agg = {"unnecessary_signal_count": 1}
    violation_agg = {"n_sessions": 2, "early_detection_count": 1, "late_detection_count": 1, "missed_count": 0}
    result = classify_overall(safe_agg, violation_agg, holdout_adopted=True)
    assert result["classification"] == "B"
    print("OK - safe transient signal>=1 -> B(Over-sensitive), early detection 있어도 우선")


def test_classify_overall_insensitive():
    safe_agg = {"unnecessary_signal_count": 0}
    violation_agg = {"n_sessions": 2, "early_detection_count": 0, "late_detection_count": 0, "missed_count": 2}
    result = classify_overall(safe_agg, violation_agg, holdout_adopted=True)
    assert result["classification"] == "C"
    print("OK - safe 0건 + violation 전부 missed -> C(Insensitive)")


def test_classify_overall_mixed_when_b_and_c_overlap():
    safe_agg = {"unnecessary_signal_count": 1}
    violation_agg = {"n_sessions": 2, "early_detection_count": 0, "late_detection_count": 1, "missed_count": 1}
    result = classify_overall(safe_agg, violation_agg, holdout_adopted=True)
    assert result["classification"] == "D"
    print("OK - B 트리거와 C 트리거가 동시에 성립하면 확대 해석하지 않고 D(Mixed)")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        t()
    print(f"전체 통과 ({len(tests)}개)")
