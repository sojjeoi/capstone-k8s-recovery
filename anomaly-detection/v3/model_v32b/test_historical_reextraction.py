#!/usr/bin/env python3
"""§80.5 하니스 안전 보완의 오프라인 고정 테스트 - 클러스터 의존 없음.
port-forward health check·bounded retry(보간 없음)·raw completeness
검사(gap/NaN/누락)가 부하·feature·SLO 의미를 바꾸지 않고 정확히
동작하는지만 확인한다."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

import historical_reextraction as hr  # noqa: E402


def _resp(json_body, status_ok=True):
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = json_body
    m.raise_for_status = MagicMock() if status_ok else MagicMock(side_effect=Exception("http error"))
    return m


def test_check_prometheus_reachable_success():
    with patch.object(hr.requests, "get", return_value=_resp({"status": "success"})):
        result = hr.check_prometheus_reachable()
    assert result["reachable"] is True
    print("OK - 정상 응답이면 reachable=True")


def test_check_prometheus_reachable_connection_refused():
    with patch.object(hr.requests, "get", side_effect=ConnectionError("refused")):
        result = hr.check_prometheus_reachable()
    assert result["reachable"] is False
    assert "refused" in result["error"]
    print("OK - 연결 거부는 reachable=False + 원인 기록(TCP 여부가 아니라 실제 호출 기준)")


def test_bounded_retry_succeeds_after_transient_failure_no_merge():
    calls = []

    def fn(promql, start, end, step):
        calls.append((promql, start, end, step))
        if len(calls) < 3:
            raise ConnectionError("transient")
        return [1.0, 2.0, 3.0]

    start, end = datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
    result = hr.query_range_with_bounded_retry("up", start, end, max_retries=3, retry_delay_sec=0,
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
        hr.query_range_with_bounded_retry("up", start, end, max_retries=3, retry_delay_sec=0,
                                           query_range_fn=always_fails, sleep_fn=lambda s: None)
        assert False, "예외가 발생해야 함"
    except RuntimeError as e:
        assert "dead tunnel" in str(e)
    print("OK - retry를 모두 소진하면 보간·0 대체 없이 예외로 실패 처리")


def _matrix_response(start_ts, n, step_sec, value=1.0, inject_nan_at=None):
    values = []
    for i in range(n):
        ts = start_ts + i * step_sec
        v = "NaN" if inject_nan_at == i else value
        values.append([ts, str(v)])
    return {"data": {"result": [{"metric": {}, "values": values}]}}


def test_verify_metric_completeness_passes_clean_grid():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(seconds=60)
    n = int(60 / 15) + 1
    body = _matrix_response(start.timestamp(), n, 15.0)
    with patch.object(hr.requests, "get", return_value=_resp(body)):
        result = hr.verify_metric_completeness("cpu", "up", start, end)
    assert result["ok"] is True
    assert result["has_nan_or_inf"] is False
    print("OK - 15초 간격으로 빈틈없는 grid는 completeness 통과")


def test_verify_metric_completeness_detects_gap():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(seconds=120)
    # 15초 간격이어야 할 것을 처음 절반만 채우고 나머지를 건너뛴다(45초+ gap 발생).
    values = [[start.timestamp() + i * 15.0, "1.0"] for i in range(3)]
    values.append([start.timestamp() + 110.0, "1.0"])
    body = {"data": {"result": [{"metric": {}, "values": values}]}}
    with patch.object(hr.requests, "get", return_value=_resp(body)):
        result = hr.verify_metric_completeness("cpu", "up", start, end)
    assert result["ok"] is False
    assert result["max_gap_sec"] > hr.MAX_ALLOWED_GAP_SEC
    print("OK - step의 2배를 넘는 gap은 completeness 실패로 판정")


def test_verify_metric_completeness_detects_nan():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(seconds=60)
    n = int(60 / 15) + 1
    body = _matrix_response(start.timestamp(), n, 15.0, inject_nan_at=1)
    with patch.object(hr.requests, "get", return_value=_resp(body)):
        result = hr.verify_metric_completeness("cpu", "up", start, end)
    assert result["ok"] is False
    assert result["has_nan_or_inf"] is True
    print("OK - NaN 샘플이 하나라도 있으면 completeness 실패로 판정(0 대체 없음)")


def test_verify_metric_completeness_detects_missing_response():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(seconds=60)
    body = {"data": {"result": []}}
    with patch.object(hr.requests, "get", return_value=_resp(body)):
        result = hr.verify_metric_completeness("cpu", "up", start, end)
    assert result["ok"] is False
    assert result["n_samples"] == 0
    print("OK - 응답 자체가 없으면(target 없음 등) completeness 실패")


def test_reextract_session_reconstructs_bounds_from_ramp_summary(tmp_path):
    run_id = "synthtest-20260101T000000Z"
    probe_csv = tmp_path / f"probe-{run_id}-raw.csv"
    ramp_csv = tmp_path / f"ramp-{run_id}-summary.csv"

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    stage_start = base + timedelta(seconds=10)
    stage_end = base + timedelta(seconds=70)

    probe_rows = ["experiment_run_id,scenario,arm,repetition,sent_at,latency,status,success"]
    t = base
    while t < base + timedelta(seconds=80):
        probe_rows.append(f"{run_id},synth,native,1,{t.isoformat()},0.2,200,True")
        t += timedelta(seconds=1)
    probe_csv.write_text("\n".join(probe_rows), encoding="utf-8")

    ramp_csv.write_text(
        "experiment_run_id,scenario,method,repetition,stage,target_rps,actual_rps,sent,success,success_rate,"
        "p95,p99,stage_start_utc,stage_end_utc\n"
        f"{run_id},synth,native,1,synth-stage,0.025,0.03,2,2,1.0,0.2,0.2,"
        f"{stage_start.isoformat()},{stage_end.isoformat()}\n",
        encoding="utf-8",
    )

    def fake_query_range_fn(promql, start, end, step="15s"):
        return [1.0, 1.0, 1.0]

    result = hr.reextract_session("synth-session", "low_load", probe_csv, ramp_csv,
                                   query_range_fn=fake_query_range_fn)

    assert result.start_utc == stage_start
    assert result.end_utc == stage_end
    assert result.run_id == run_id
    assert result.candidate_result["all_success_100pct"] is True
    assert result.t_slo is None  # latency 0.2s는 SLO 위반 아님
    assert len(result.feature_rows) > 0
    assert all(r.valid for r in result.feature_rows)
    print("OK - 원본 raw CSV 두 개만으로 session 경계·candidate_result·feature_rows가 정확히 재구성됨")


def test_verify_recovery_complete_flags_incomplete_metric():
    from historical_reextraction import ReextractionResult
    fake = ReextractionResult(
        session_id="x", regime="low_load", run_id="r", start_utc=None, end_utc=None,
        candidate_result={}, t_slo=None, feature_rows=[MagicMock(valid=True)],
        completeness_checks=[{"metric_name": "cpu", "n_samples": 1, "expected_n_samples": 5,
                               "max_gap_sec": 100.0, "has_nan_or_inf": False, "ok": False}],
        invalid_window_count=0,
    )
    verdict = hr.verify_recovery_complete(fake)
    assert verdict["complete"] is False
    assert "cpu" in verdict["failures"][0]
    print("OK - completeness 실패가 하나라도 있으면 전체 복구를 불완전으로 판정")


if __name__ == "__main__":
    import tempfile
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        if "tmp_path" in t.__code__.co_varnames[:t.__code__.co_argcount]:
            with tempfile.TemporaryDirectory() as d:
                t(Path(d))
        else:
            t()
    print(f"전체 통과 ({len(tests)}개)")
