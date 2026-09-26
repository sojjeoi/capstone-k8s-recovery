#!/usr/bin/env python3
"""features.py 회귀 테스트(§163 신규 - 이전엔 이 모듈 직접 테스트가 없었음).
Prometheus 접근(_query_range_with_timestamps 내부 requests.get)은 전부
monkeypatch로 대체한다 - 실제 클러스터 접근 없이 오프라인으로 돈다.

핵심 검증: (1) 시각 정보를 추가해도 _query_range()/extract_features()의
기존 반환값이 한 글자도 안 바뀐다(판정 로직이 보는 값 불변), (2) 원본
표본 신선도가 있음/없음/오래됨 상황에서 정직하게(단정하지 않고 상·하한
또는 확인불가로) 기록된다."""
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.stdout.reconfigure(encoding="utf-8")

import features as feat

T0 = datetime(2026, 9, 20, 0, 0, 0, tzinfo=timezone.utc)


class _FakeResponse:
    def __init__(self, result):
        self._result = result

    def raise_for_status(self):
        pass

    def json(self):
        return {"data": {"result": self._result}}


def _series(values_with_ts):
    return [{"metric": {}, "values": [[ts, str(v)] for ts, v in values_with_ts]}]


def test_query_range_with_timestamps_preserves_and_sorts_pairs():
    # 순서를 일부러 뒤섞어 반환해도(Prometheus가 원래 정렬해 주지만
    # 방어적으로) sorted() 결과가 나오는지 확인.
    fake_result = _series([(100.0, 1.0), (130.0, 3.0), (115.0, 2.0)])
    with patch.object(feat.requests, "get", return_value=_FakeResponse(fake_result)):
        pairs = feat._query_range_with_timestamps("dummy_promql", T0, T0 + timedelta(seconds=60))
    assert pairs == [(100.0, 1.0), (115.0, 2.0), (130.0, 3.0)], pairs
    print("OK - (timestamp, value) 쌍이 시각순 정렬로 보존됨")


def test_query_range_with_timestamps_empty_result_returns_empty_list():
    with patch.object(feat.requests, "get", return_value=_FakeResponse([])):
        pairs = feat._query_range_with_timestamps("dummy_promql", T0, T0 + timedelta(seconds=60))
    assert pairs == []
    print("OK - 빈 result는 빈 리스트(시계열 자체가 없음을 그대로 보존)")


def test_query_range_unchanged_after_refactor():
    # §163 이전 _query_range()의 계약(값만 있는 flat 리스트, 시각순)이
    # 새 구현(값만 뽑는 얇은 래퍼)에서도 100% 동일한지 확인.
    fake_result = _series([(100.0, 1.0), (115.0, 2.0), (130.0, 3.0)])
    with patch.object(feat.requests, "get", return_value=_FakeResponse(fake_result)):
        values = feat._query_range("dummy_promql", T0, T0 + timedelta(seconds=60))
    assert values == [1.0, 2.0, 3.0], values
    print("OK - _query_range()의 반환값이 리팩터링 전과 100% 동일(값만, 시각순)")


def test_query_range_sums_multiple_series_unchanged():
    # 기존 동작(여러 시계열 합산) 보존 확인 - active+preview 동시 구동 케이스.
    fake_result = [
        {"metric": {"job": "a"}, "values": [[100.0, "1.0"], [115.0, "2.0"]]},
        {"metric": {"job": "b"}, "values": [[100.0, "10.0"], [115.0, "20.0"]]},
    ]
    with patch.object(feat.requests, "get", return_value=_FakeResponse(fake_result)):
        values = feat._query_range("dummy_promql", T0, T0 + timedelta(seconds=60))
    assert values == [11.0, 22.0], values
    print("OK - 여러 시계열 합산 동작이 리팩터링 후에도 동일")


def test_raw_sample_staleness_no_samples_reports_explicit_gap_not_silent_zero():
    result = feat._raw_sample_staleness([], datetime(2026, 9, 20, 0, 5, 0, tzinfo=timezone.utc),
                                         feat.PROM_SCRAPE_INTERVAL_SEC)
    assert result["raw_sample_status"] == "no_samples"
    assert result["eval_grid_ts_utc"] is None
    assert result["sample_lag_at_query_sec"] is None
    assert result["interval_based_lower_bound_utc"] is None
    print("OK - 표본 없음은 status='no_samples'+전부 None으로 명시(조용히 0으로 안 채움)")


def test_raw_sample_staleness_computes_bounds_not_a_single_point():
    query_received_at = datetime(2026, 9, 20, 0, 5, 0, tzinfo=timezone.utc)
    last_ts_epoch = query_received_at.timestamp() - 3.0  # 3초 전 표본
    result = feat._raw_sample_staleness([(last_ts_epoch, 1.0)], query_received_at,
                                         feat.PROM_SCRAPE_INTERVAL_SEC)
    assert result["raw_sample_status"] == "ok"
    assert result["sample_lag_at_query_sec"] == 3.0
    grid_ts = datetime.fromisoformat(result["eval_grid_ts_utc"])
    lower = datetime.fromisoformat(result["interval_based_lower_bound_utc"])
    assert grid_ts - lower == timedelta(seconds=feat.PROM_SCRAPE_INTERVAL_SEC), (grid_ts, lower)
    assert grid_ts == query_received_at - timedelta(seconds=3.0)
    print("OK - 단일 시각 단정 없이 평가 격자 시각·주기 기반 참고 하한만 반환")


def test_raw_sample_staleness_unconfirmed_interval_yields_no_lower_bound():
    # §164 - cpu/memory처럼 scrape_interval_sec이 None(이 저장소로는
    # 확인 불가)이면 미확인 숫자로 하한을 계산하지 말고 None이어야 한다.
    query_received_at = datetime(2026, 9, 20, 0, 5, 0, tzinfo=timezone.utc)
    result = feat._raw_sample_staleness([(query_received_at.timestamp() - 3.0, 1.0)],
                                         query_received_at, None)
    assert result["raw_sample_status"] == "ok"
    assert result["eval_grid_ts_utc"] is not None  # 격자 시각 자체는 여전히 보고
    assert result["scrape_interval_used_sec"] is None
    assert result["interval_based_lower_bound_utc"] is None, \
        "주기 미확인인데 하한이 계산됨(미확인 숫자 대입 금지 위반)"
    print("OK - scrape_interval_sec=None(cpu/memory)이면 하한을 만들어내지 않음")


def test_raw_sample_staleness_stale_sample_shows_large_lag_honestly():
    # 지연/stale 상황 - 표본이 실제로는 5분 전인데 조용히 "방금"처럼 보이면 안 됨.
    query_received_at = datetime(2026, 9, 20, 0, 5, 0, tzinfo=timezone.utc)
    stale_ts_epoch = query_received_at.timestamp() - 300.0
    result = feat._raw_sample_staleness([(stale_ts_epoch, 1.0)], query_received_at,
                                         feat.PROM_SCRAPE_INTERVAL_SEC)
    assert result["raw_sample_status"] == "ok"
    assert result["sample_lag_at_query_sec"] == 300.0, result
    print("OK - 오래된(stale) 표본의 지연이 축소·은폐 없이 그대로 기록됨(300초)")


def test_extract_features_with_provenance_features_match_plain_extract_features():
    fake_result = _series([(100.0, 1.0), (115.0, 2.0)])
    with patch.object(feat.requests, "get", return_value=_FakeResponse(fake_result)):
        plain = feat.extract_features(T0, T0 + timedelta(seconds=60))
    with patch.object(feat.requests, "get", return_value=_FakeResponse(fake_result)):
        verbose = feat.extract_features_with_provenance(T0, T0 + timedelta(seconds=60))
    assert plain == verbose["features"], (plain, verbose["features"])
    assert len(verbose["features"]) == len(feat.FEATURE_NAMES) == 8
    assert set(verbose["per_metric_provenance"].keys()) == set(feat.METRICS.keys())
    for name in feat.METRICS:
        prov = verbose["per_metric_provenance"][name]
        assert prov["query_sent_at_utc"] is not None and prov["query_received_at_utc"] is not None
    print("OK - extract_features_with_provenance()['features']가 extract_features()와 100% 동일")


def test_extract_features_with_provenance_never_asserts_interval_for_cpu_memory():
    # §164 - cpu/memory는 이 저장소 근거로 스크레이프 주기가 확인 안
    # 되므로, 지표별 provenance에서 이 둘만 scrape_interval_used_sec=
    # None(따라서 interval_based_lower_bound_utc도 None)이어야 하고
    # queue/cache는 15초 근거가 있어야 한다.
    fake_result = _series([(100.0, 1.0), (115.0, 2.0)])
    with patch.object(feat.requests, "get", return_value=_FakeResponse(fake_result)):
        verbose = feat.extract_features_with_provenance(T0, T0 + timedelta(seconds=60))
    for name in ("cpu", "memory"):
        prov = verbose["per_metric_provenance"][name]
        assert prov["scrape_interval_used_sec"] is None, f"{name}에 미확인 주기가 대입됨: {prov}"
        assert prov["interval_based_lower_bound_utc"] is None
    for name in ("queue", "cache"):
        prov = verbose["per_metric_provenance"][name]
        assert prov["scrape_interval_used_sec"] == feat.PROM_SCRAPE_INTERVAL_SEC
        assert prov["interval_based_lower_bound_utc"] is not None
    print("OK - cpu/memory에는 미확인 15초를 대입하지 않고, queue/cache에만 근거 있는 15초 적용")


def test_extract_features_with_provenance_reports_no_samples_per_metric_honestly():
    with patch.object(feat.requests, "get", return_value=_FakeResponse([])):
        verbose = feat.extract_features_with_provenance(T0, T0 + timedelta(seconds=60))
    for name in feat.METRICS:
        assert verbose["per_metric_provenance"][name]["raw_sample_status"] == "no_samples"
    # 기존 _mean_slope([]) 완충(0.0, 0.0)은 실시간 경로 그대로 유지 - feature 자체는 안 바뀜.
    assert verbose["features"] == [0.0, 0.0] * len(feat.METRICS)
    print("OK - 지표 전부 무응답이어도 feature 완충(0,0)은 유지하되 provenance는 정직하게 no_samples")


def main():
    test_query_range_with_timestamps_preserves_and_sorts_pairs()
    test_query_range_with_timestamps_empty_result_returns_empty_list()
    test_query_range_unchanged_after_refactor()
    test_query_range_sums_multiple_series_unchanged()
    test_raw_sample_staleness_no_samples_reports_explicit_gap_not_silent_zero()
    test_raw_sample_staleness_computes_bounds_not_a_single_point()
    test_raw_sample_staleness_unconfirmed_interval_yields_no_lower_bound()
    test_raw_sample_staleness_stale_sample_shows_large_lag_honestly()
    test_extract_features_with_provenance_features_match_plain_extract_features()
    test_extract_features_with_provenance_never_asserts_interval_for_cpu_memory()
    test_extract_features_with_provenance_reports_no_samples_per_metric_honestly()
    print("전체 통과")


if __name__ == "__main__":
    main()
