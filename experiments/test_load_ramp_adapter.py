#!/usr/bin/env python3
"""load_ramp_adapter._is_post_injection_window_evaluable() /
_classify_timestamp_against_stages() 검증 - 둘 다 순수 함수라 kubectl/
실클러스터 없이 오프라인으로 돈다.

_is_post_injection_window_evaluable(2026-09-17 정정): 전체 누적 표본
수만 보던 이전 버전은 주입 전 표본이 섞이거나 주입 후 갱신이 멈춰도
잘못 evaluable=True를 낼 수 있었음.

_classify_timestamp_against_stages(2026-09-18 추가, stage 관측성 보완):
§30에서 stage 경계를 명목값(주입 시각+90초 단위)으로만 추정해야 했던
문제 - ramp.py --summary-out이 기록하는 실제 stage_start_utc/
stage_end_utc를 써서 t_slo 등이 실제로 어느 stage에 속했는지 판정한다."""
import sys
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8")

from load_ramp_adapter import _classify_timestamp_against_stages, _is_post_injection_window_evaluable

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _rows_at(offsets_sec):
    return [{"sent_at": T0 + timedelta(seconds=s)} for s in offsets_sec]


def _stage(name, start_offset_sec, end_offset_sec):
    # ramp.py --summary-out CSV를 csv.DictReader로 읽으면 전부 문자열이므로
    # fixture도 문자열로 만든다(실제 입력 형태와 동일하게).
    return {
        "stage": name,
        "stage_start_utc": (T0 + timedelta(seconds=start_offset_sec)).isoformat(),
        "stage_end_utc": (T0 + timedelta(seconds=end_offset_sec)).isoformat(),
    }


def _iso(offset_sec):
    return (T0 + timedelta(seconds=offset_sec)).isoformat()


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


def test_classify_stage_within_stage_boundary():
    stages = [_stage("stage-1-0.025rps", 0, 90), _stage("stage-2-0.05rps", 90, 180)]
    assert _classify_timestamp_against_stages(_iso(45), stages) == "stage-1-0.025rps"
    assert _classify_timestamp_against_stages(_iso(135), stages) == "stage-2-0.05rps"
    print("OK - 실제 stage 구간 안 timestamp는 해당 stage 이름으로 분류됨")


def test_classify_stage_before_first_stage_is_baseline():
    stages = [_stage("stage-1-0.025rps", 0, 90)]
    assert _classify_timestamp_against_stages(_iso(-5), stages) == "baseline"
    print("OK - 첫 stage 시작 전은 baseline으로 분류됨")


def test_classify_stage_after_last_stage_is_drain():
    stages = [_stage("stage-1-0.025rps", 0, 90)]
    assert _classify_timestamp_against_stages(_iso(120), stages) == "drain"
    print("OK - 마지막 stage 종료 후는 drain으로 분류됨")


def test_classify_stage_uses_real_boundaries_not_nominal():
    # 핵심 회귀 테스트(§23에서 확인된 문제 재발 방지) - stage-1이 straggler
    # 대기 때문에 명목 종료(90초)보다 8초 늦게(98초) 실제로 끝났다고 가정.
    # 명목 경계로 계산하면 92초 시점은 이미 stage-2(90초부터)로 잘못
    # 분류되지만, 실제 stage_end_utc(98초)를 쓰면 아직 stage-1이어야 한다.
    stages = [_stage("stage-1-0.025rps", 0, 98), _stage("stage-2-0.05rps", 98, 188)]
    assert _classify_timestamp_against_stages(_iso(92), stages) == "stage-1-0.025rps", \
        "명목 경계(90초)가 아니라 실제 지연된 경계(98초)를 써야 함"
    print("OK - 명목 90초 경계가 아니라 실제 stage_end_utc(지연 반영)로 분류됨")


def test_classify_stage_gap_between_stages_is_inter_stage_tail():
    # straggler 취소 처리 등으로 두 stage의 실제 경계 사이에 틈이 생기는
    # 경우(현재 ramp.py는 다음 stage를 곧바로 시작하지만, 요약 자체가
    # 손상되거나 향후 구현이 바뀌어 틈이 생겨도 임의로 양쪽 stage에 붙이지
    # 않고 별도로 구분해야 한다).
    stages = [_stage("stage-1-0.025rps", 0, 90), _stage("stage-2-0.05rps", 95, 185)]
    assert _classify_timestamp_against_stages(_iso(92), stages) == "inter_stage_tail"
    print("OK - 두 stage 실제 경계 사이 틈은 inter_stage_tail로 분류됨")


def test_classify_stage_missing_stages_is_unknown():
    assert _classify_timestamp_against_stages(_iso(50), None) == "unknown"
    assert _classify_timestamp_against_stages(_iso(50), []) == "unknown"
    print("OK - summary 자체가 없으면(fetch 실패) unknown - 임의 추정 안 함")


def test_classify_stage_malformed_stage_row_is_unknown():
    broken = [{"stage": "stage-1", "stage_start_utc": _iso(0)}]  # stage_end_utc 누락
    assert _classify_timestamp_against_stages(_iso(50), broken) == "unknown"
    print("OK - stage 행이 손상돼 있으면(필드 누락) unknown - 임의 추정 안 함")


if __name__ == "__main__":
    test_pre_injection_samples_alone_do_not_count()
    test_stale_samples_after_injection_do_not_count()
    test_enough_fresh_post_injection_samples_are_evaluable()
    test_post_injection_samples_below_min_count_not_evaluable()
    test_no_injection_time_is_not_evaluable()
    test_empty_rows_is_not_evaluable()
    test_classify_stage_within_stage_boundary()
    test_classify_stage_before_first_stage_is_baseline()
    test_classify_stage_after_last_stage_is_drain()
    test_classify_stage_uses_real_boundaries_not_nominal()
    test_classify_stage_gap_between_stages_is_inter_stage_tail()
    test_classify_stage_missing_stages_is_unknown()
    test_classify_stage_malformed_stage_row_is_unknown()
    print("\n모두 통과")
