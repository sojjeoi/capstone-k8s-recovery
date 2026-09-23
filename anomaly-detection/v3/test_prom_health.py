#!/usr/bin/env python3
"""§80.5 - `prom_health.py` 오프라인 고정 테스트. 클러스터 의존 없음."""
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

import prom_health as ph  # noqa: E402


def _resp(json_body):
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = json_body
    m.raise_for_status = MagicMock()
    return m


def test_check_prometheus_reachable_success():
    with patch.object(ph.requests, "get", return_value=_resp({"status": "success"})):
        result = ph.check_prometheus_reachable()
    assert result["reachable"] is True
    print("OK - 정상 응답이면 reachable=True")


def test_check_prometheus_reachable_connection_refused():
    with patch.object(ph.requests, "get", side_effect=ConnectionError("refused")):
        result = ph.check_prometheus_reachable()
    assert result["reachable"] is False
    assert "refused" in result["error"]
    print("OK - 연결 거부는 reachable=False + 원인 기록(TCP 여부가 아니라 실제 호출 기준)")


def test_check_prometheus_reachable_bad_status_body():
    with patch.object(ph.requests, "get", return_value=_resp({"status": "error"})):
        result = ph.check_prometheus_reachable()
    assert result["reachable"] is False
    print("OK - HTTP 200이어도 body status!=success면 reachable=False(반쯤 끊긴 터널 대비)")


def test_bounded_retry_succeeds_after_transient_failure_no_merge():
    calls = []

    def fn(promql, start, end, step):
        calls.append((promql, start, end, step))
        if len(calls) < 3:
            raise ConnectionError("transient")
        return [1.0, 2.0, 3.0]

    start, end = datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
    result = ph.query_range_with_bounded_retry("up", start, end, max_retries=3, retry_delay_sec=0,
                                                query_range_fn=fn, sleep_fn=lambda s: None)
    assert result == [1.0, 2.0, 3.0]
    assert len(calls) == 3
    assert all(c == (("up", start, end, "15s")) for c in calls)
    print("OK - 매 retry가 동일 query·동일 범위로만 재시도되고, 최종 성공 결과는 이전 부분 실패와 섞이지 않음")


def test_bounded_retry_exhausts_and_raises():
    def always_fails(promql, start, end, step):
        raise ConnectionError("dead tunnel")

    start, end = datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
    try:
        ph.query_range_with_bounded_retry("up", start, end, max_retries=3, retry_delay_sec=0,
                                           query_range_fn=always_fails, sleep_fn=lambda s: None)
        assert False, "예외가 발생해야 함"
    except RuntimeError as e:
        assert "dead tunnel" in str(e)
    print("OK - retry를 모두 소진하면 보간·0 대체 없이 예외로 실패 처리")


def test_bounded_retry_single_success_needs_no_retry():
    calls = []

    def fn(promql, start, end, step):
        calls.append(1)
        return [9.0]

    start, end = datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
    result = ph.query_range_with_bounded_retry("up", start, end, query_range_fn=fn, sleep_fn=lambda s: None)
    assert result == [9.0]
    assert len(calls) == 1
    print("OK - 첫 시도가 성공하면 retry·sleep 없이 바로 반환")


# ---------------------------------------------------------------------------
# §108(2026-09-24) - check_metric_freshness()의 reason_kind: "진짜 데이터 공백"
# (no_samples/stale)과 "Prometheus 자체에 못 닿음"(connection_error/api_error)을
# 구분해야 score_server.py가 데이터 공백으로 오인해 삼키지 않는다.
# ---------------------------------------------------------------------------

def test_freshness_fresh_sample_has_no_reason_kind():
    now = __import__("time").time()
    with patch.object(ph.requests, "get", return_value=_resp(
            {"status": "success", "data": {"result": [{"value": [now, "1"]}]}})):
        result = ph.check_metric_freshness("up")
    assert result["fresh"] is True
    assert result["reason_kind"] is None
    print("OK - 신선한 표본이면 fresh=True, reason_kind=None")


def test_freshness_no_samples_reason_kind():
    with patch.object(ph.requests, "get", return_value=_resp(
            {"status": "success", "data": {"result": []}})):
        result = ph.check_metric_freshness("up")
    assert result["fresh"] is False
    assert result["reason_kind"] == "no_samples"
    print("OK - 정상 응답인데 표본이 0개면 reason_kind='no_samples'(진짜 데이터 공백)")


def test_freshness_stale_sample_reason_kind():
    old_ts = __import__("time").time() - 999.0
    with patch.object(ph.requests, "get", return_value=_resp(
            {"status": "success", "data": {"result": [{"value": [old_ts, "1"]}]}})):
        result = ph.check_metric_freshness("up", max_age_sec=120.0)
    assert result["fresh"] is False
    assert result["reason_kind"] == "stale"
    print("OK - 표본은 있지만 max_age_sec보다 오래됐으면 reason_kind='stale'(진짜 데이터 공백)")


def test_freshness_api_error_reason_kind():
    with patch.object(ph.requests, "get", return_value=_resp({"status": "error"})):
        result = ph.check_metric_freshness("up")
    assert result["fresh"] is False
    assert result["reason_kind"] == "api_error"
    print("OK - HTTP 200이어도 body status!=success면 reason_kind='api_error'(데이터 공백 아님 - 인프라 문제)")


def test_freshness_connection_error_reason_kind():
    with patch.object(ph.requests, "get", side_effect=ConnectionError("연결 거부")):
        result = ph.check_metric_freshness("up")
    assert result["fresh"] is False
    assert result["reason_kind"] == "connection_error"
    print("OK - 요청 자체가 실패하면 reason_kind='connection_error'(데이터 공백 아님 - 인프라 문제)")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        t()
    print(f"전체 통과 ({len(tests)}개)")
