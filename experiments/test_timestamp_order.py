#!/usr/bin/env python3
"""timestamp_order.py 검증 - HEADROOM-COLDSTART-03-ATTEMPT1의 실제 오탐 버그
(warmup completion이 Ready보다 먼저였는데 문자열 비교 때문에 반대로
판정됨)를 회귀 고정한다."""
from timestamp_order import compare_before, parse_rfc3339


def test_normal_order_different_seconds():
    assert compare_before("2026-01-01T00:00:01Z", "2026-01-01T00:00:05Z") is True


def test_same_second_different_microseconds_both_precise():
    assert compare_before("2026-01-01T00:00:01.100000Z", "2026-01-01T00:00:01.900000Z") is True
    assert compare_before("2026-01-01T00:00:01.900000Z", "2026-01-01T00:00:01.100000Z") is False


def test_z_and_plus00_00_same_instant_are_equal_not_ordered():
    a = "2026-01-01T00:00:01.500000Z"
    b = "2026-01-01T00:00:01.500000+00:00"
    assert parse_rfc3339(a) == parse_rfc3339(b)
    assert compare_before(a, b) is False
    assert compare_before(b, a) is False


def test_real_failure_ready_actually_first():
    warmup = "2026-01-01T00:00:05Z"
    ready = "2026-01-01T00:00:01+00:00"
    assert compare_before(warmup, ready) is False


def test_missing_timestamp_raises():
    for bad in ("", None):
        try:
            parse_rfc3339(bad)
            assert False, f"{bad!r}는 예외를 던져야 함"
        except ValueError:
            pass


def test_attempt1_regression_second_truncation_returns_unknown_not_false():
    """실제로 발생한 버그의 정확한 재현: warmup은 소수초까지 있고 ready는
    K8s lastTransitionTime처럼 초 단위로 절삭돼 있다. 같은 초 안에서는
    실제 순서를 알 수 없으므로 잘못된 False가 아니라 None(확인 불가)을
    반환해야 한다."""
    warmup = "2026-09-18T08:12:51.314199658Z"
    ready = "2026-09-18T08:12:51+00:00"
    assert compare_before(warmup, ready) is None


def test_no_silent_second_truncation_when_both_sides_precise():
    """양쪽 다 소수초 정보가 있으면(=절삭된 게 아니면) 같은 초 안에서도
    명확한 True/False를 반환해야 한다 - None으로 뭉개지 않는다."""
    warmup = "2026-09-18T08:12:51.100000Z"
    ready = "2026-09-18T08:12:51.900000+00:00"
    assert compare_before(warmup, ready) is True
