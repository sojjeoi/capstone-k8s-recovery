#!/usr/bin/env python3
"""load_ramp_adapter._is_post_injection_window_evaluable() 검증 - 순수
함수라 kubectl/실클러스터 없이 오프라인으로 돈다(2026-09-17 정정: 전체
누적 표본 수만 보던 이전 버전은 주입 전 표본이 섞이거나 주입 후 갱신이
멈춰도 잘못 evaluable=True를 낼 수 있었음)."""
import sys
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8")

from load_ramp_adapter import _is_post_injection_window_evaluable

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _rows_at(offsets_sec):
    return [{"sent_at": T0 + timedelta(seconds=s)} for s in offsets_sec]


def test_pre_injection_samples_alone_do_not_count():
    # 주입 전 표본이 20개 넘게 쌓여 있어도(예: probe가 일찍 시작), 주입 후
    # 표본이 하나도 없으면 evaluable이면 안 된다.
    rows = _rows_at(range(-60, 0))  # 전부 주입 이전(T0 이전) 표본
    assert _is_post_injection_window_evaluable(rows, T0) is False
    print("OK - 주입 전 표본만 있으면(주입 후 표본 0개) NOT_EVALUABLE 유지")


def test_stale_samples_after_injection_do_not_count():
    # 주입 후 표본이 몇 개 들어오다가 갱신이 멈춘 경우(예: probe fetch 지연) -
    # 최신 표본 시각이 주입 후 한 창(WINDOW_SEC) 분량에 못 미치면 시간이
    # 아무리 지나도(호출 시점 기준이 아니라 "표본 자체의" 최신 시각 기준)
    # NOT_EVALUABLE이어야 한다.
    rows = _rows_at([-5, -2, 1, 3, 5])  # 주입 후 표본이 5초 시점에서 멈춤(60초 미달)
    assert _is_post_injection_window_evaluable(rows, T0) is False
    print("OK - 주입 후 표본이 있어도 최신 표본이 window_sec 미만이면 NOT_EVALUABLE 유지")


def test_enough_fresh_post_injection_samples_are_evaluable():
    # 주입 후 60초 이상 경과 + 그 구간에 표본 20개 이상 -> evaluable.
    rows = _rows_at(range(-10, 65))  # 주입 전 10개 + 주입 후 65개(0~64초)
    assert _is_post_injection_window_evaluable(rows, T0) is True
    print("OK - 주입 후 충분한 최신 표본이 쌓이면 evaluable=True")


def test_post_injection_samples_below_min_count_not_evaluable():
    # 최신 표본은 주입 후 60초를 넘겼지만, 그 사이 표본 수 자체가 적으면
    # (예: probe rps가 낮거나 간헐적 fetch 실패) 여전히 NOT_EVALUABLE.
    rows = _rows_at([0, 20, 40, 65])  # 주입 후 표본 4개뿐(최소 20개 미달)
    assert _is_post_injection_window_evaluable(rows, T0) is False
    print("OK - 주입 후 최신 표본이 window_sec을 넘겨도 표본 수가 적으면 NOT_EVALUABLE 유지")


def test_no_injection_time_is_not_evaluable():
    rows = _rows_at(range(0, 65))
    assert _is_post_injection_window_evaluable(rows, None) is False
    print("OK - 주입 시각을 아직 통보받지 못했으면(None) NOT_EVALUABLE")


def test_empty_rows_is_not_evaluable():
    assert _is_post_injection_window_evaluable([], T0) is False
    print("OK - 표본이 아예 없으면 NOT_EVALUABLE")


if __name__ == "__main__":
    test_pre_injection_samples_alone_do_not_count()
    test_stale_samples_after_injection_do_not_count()
    test_enough_fresh_post_injection_samples_are_evaluable()
    test_post_injection_samples_below_min_count_not_evaluable()
    test_no_injection_time_is_not_evaluable()
    test_empty_rows_is_not_evaluable()
    print("\n모두 통과")
