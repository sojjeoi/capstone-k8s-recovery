import json

import metric_audit as ma


def test_series_values_first_series_drops_nan_and_junk():
    resp = {"data": {"result": [{"values": [[1.0, "0.5"], [16.0, "NaN"], [31.0, "x"], [46.0, "2"]]},
                                {"values": [[1.0, "9"]]}]}}
    assert ma.series_values(resp) == {1: 0.5, 46: 2.0}
    assert ma.series_values({"status": "error"}) == {}


def test_counter_resets_and_spearman():
    assert ma.counter_resets([1, 2, 5, 0, 3, 1]) == 2
    assert ma.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert ma.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0
    assert ma.spearman([1, 2, 3], [0, 0, 0]) is None   # 상수 열 → 상관 정의 안 됨


def test_describe_constant_zero_metric_reports_degenerate():
    d = ma.describe([0.0] * 10, expected_points=20)
    assert d["nonzero_frac"] == 0 and d["unique"] == 1 and d["std"] == 0 and d["missing_rate"] == 0.5
    assert ma.describe([], 5) == {"n": 0, "missing_rate": 1.0}


def test_cpu_bins_groups_by_kind_within_same_cpu_band():
    pts = [("normal", 1.6, 1.0), ("normal", 1.7, 1.0), ("pressure", 1.65, 0.0), ("pressure", 2.3, 0.0)]
    b = ma.cpu_bins(pts, edges=[0, 1.5, 2.0, 3.0])
    assert b["[1.5,2)"]["normal"] == {"n": 2, "median": 1.0, "nonzero_frac": 1.0}
    assert b["[1.5,2)"]["pressure"]["n"] == 1 and "[0,1.5)" not in b


def test_windows_from_evidence_files(tmp_path):
    (tmp_path / "c3n01-validity.json").write_text(json.dumps({"run": "c3n01", "status": "normal_valid", "base": {
        "t0_utc": "2026-10-10T08:00:00+00:00", "observation_end_utc": "2026-10-10T09:10:00+00:00",
        "nodes": {"active_pod": "vllm-a"}}}), encoding="utf-8")
    (tmp_path / "c3n05-validity.json").write_text(json.dumps({"run": "c3n05", "status": "unexpected_degradation"}),
                                                  encoding="utf-8")
    w = ma.normal_windows(tmp_path)
    assert [x["id"] for x in w] == ["c3n01"] and w[0]["pod"] == "vllm-a"   # 무효 세션은 쓰지 않음


def test_latency_candidates_marked_not_allowed_as_detector_input():
    banned = [k for k, (_, allowed, _) in ma.CANDIDATES.items() if not allowed]
    assert set(banned) == {"e2e_latency_mean_s", "queue_time_mean_s"}
    assert all('job="vllm-active"' in e for k, (e, _, _) in ma.CANDIDATES.items() if "vllm:" in e)
    # finished_reason 라벨별 다중 시계열 → 합산하지 않으면 첫 시계열(0)만 읽힌다
    assert ma.CANDIDATES["req_success_per_s"][0].startswith("sum(") and ma.COUNTERS["req_success"].startswith("sum(")
