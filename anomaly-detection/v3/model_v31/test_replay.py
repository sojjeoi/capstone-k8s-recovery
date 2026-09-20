#!/usr/bin/env python3
"""replay.py 검증 - score_server.py의 실제 상태기계(§71 감사)와 일치하는지
순수 함수 단위로 확인한다. 클러스터·Prometheus 의존 없음."""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from replay import calibrate_threshold, replay_detector


def test_all_normal_scores_no_signal():
    scores = [1.0, 0.5, 0.8, 0.3, 0.9]
    r = replay_detector(scores, threshold=0.0)
    assert r["signal_count"] == 0 and r["point_anomaly_count"] == 0
    print("OK - 전부 threshold 이상이면 신호 없음")


def test_single_anomalous_window_no_signal():
    """score_server.py:39 - CONSECUTIVE_THRESHOLD=3, 단일 window는 신호를
    보내지 않는다."""
    scores = [1.0, -1.0, 1.0, 1.0]
    r = replay_detector(scores, threshold=0.0)
    assert r["signal_count"] == 0
    assert r["point_anomaly_count"] == 1
    print("OK - 단일 이상 window만으로는 신호 없음(연속 3회 미달)")


def test_three_consecutive_triggers_signal():
    scores = [1.0, -1.0, -1.0, -1.0, 1.0]
    r = replay_detector(scores, threshold=0.0)
    assert r["signal_count"] == 1
    assert r["signal_indices"] == [3]  # 3번째 연속(index 3)에서 발화
    print("OK - 정확히 3회 연속이면 그 시점에 신호 1회")


def test_reset_on_single_normal_reading():
    """score_server.py:83 - 정상 판정 1회로 즉시 0 리셋(누적 아님)."""
    scores = [-1.0, -1.0, 1.0, -1.0, -1.0]
    r = replay_detector(scores, threshold=0.0)
    assert r["signal_count"] == 0  # 2연속 -> 리셋 -> 2연속, 3회 연속 없음
    assert r["max_consecutive_anomalous"] == 2
    print("OK - 정상 판정 1회가 연속 카운터를 리셋함")


def test_cooldown_suppresses_repeat_signal():
    """score_server.py:40/90 - 신호 발행 후 60초(=4 tick @15s) 안에는
    연속 조건이 계속 유지돼도 재발행하지 않는다."""
    scores = [-1.0] * 10
    r = replay_detector(scores, threshold=0.0, cooldown_sec=60.0, eval_interval_sec=15.0)
    # index 2에서 첫 신호, 이후 60초(4 tick) 안에는 재발행 금지 -> 다음 가능 시점은 index 6
    assert r["signal_indices"][0] == 2
    assert 3 not in r["signal_indices"] and 4 not in r["signal_indices"] and 5 not in r["signal_indices"]
    assert 6 in r["signal_indices"]
    print("OK - cooldown 동안 연속 조건이 유지돼도 재발행하지 않음, cooldown 끝나면 재발행")


def test_strict_less_than_threshold():
    """score_server.py:82 - `score < threshold`(엄격한 미만). score==threshold는 이상이 아님."""
    scores = [0.0, 0.0, 0.0]
    r = replay_detector(scores, threshold=0.0)
    assert r["point_anomaly_count"] == 0
    print("OK - score==threshold는 이상으로 치지 않음(엄격한 미만)")


def test_empty_scores_returns_none_stats():
    r = replay_detector([], threshold=0.0)
    assert r["n_points"] == 0 and r["signal_count"] == 0 and r["point_fpr"] is None
    print("OK - 빈 입력은 0/None으로 안전하게 반환")


def test_calibrate_threshold_finds_most_sensitive_zero_episode_value():
    # 세션 A: 전부 양수(정상), 세션 B: 딱 2개만 음수(3연속 미달, episode 0)
    session_scores = {"A": [1.0, 2.0, 1.5, 0.5], "B": [0.9, -0.1, -0.2, 0.8]}
    result = calibrate_threshold(session_scores)
    assert result["calibration_failed"] is False
    # 가장 민감한(가장 높은) threshold를 골라야 하므로 threshold는 관측된 값 중 하나
    assert result["threshold"] in {s for v in session_scores.values() for s in v}
    for sid in session_scores:
        assert result["per_session"][sid]["signal_count"] == 0
    print("OK - 두 calibration session 모두 episode 0인 가장 민감한 threshold 선택")


def test_calibrate_threshold_degenerate_when_nothing_detectable_without_episodes():
    # 모든 threshold 후보에서 3연속이 반드시 나오는 극단 사례 -> 결국 "아무것도 안 잡는" 선택만 0 episode
    session_scores = {"A": [-1.0, -1.0, -1.0, -1.0]}
    result = calibrate_threshold(session_scores)
    assert result["calibration_failed"] is True
    print("OK - 0 episode를 만족하는 유일한 해가 '아무것도 안 잡음'이면 calibration_failed=True")


def test_calibrate_threshold_no_scores_fails_closed():
    result = calibrate_threshold({})
    assert result["calibration_failed"] is True and result["threshold"] is None
    print("OK - calibration score가 아예 없으면 fail-closed")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        t()
    print(f"전체 통과 ({len(tests)}개)")
