#!/usr/bin/env python3
"""build_dataset.py 검증 - 순수 함수만 대상(윈도우 생성·strict completeness·
중복 검출·세션 단위 split). 실제 Prometheus 접근(_query_range)은 전부 가짜
함수로 주입해서 피한다 - anomaly-detection/에는 experiments/conftest.py 같은
cluster_guard가 없으므로 이 규율을 테스트 각자가 지킨다(실제 함수를 인자
기본값 없이 호출하지 않음)."""
import sys
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8")

from build_dataset import (
    WINDOW_SEC,
    WindowRow,
    build_rows_for_session,
    extract_window_strict,
    find_duplicate_feature_vectors,
    find_duplicate_timestamps,
    iter_window_starts,
    split_sessions,
    summarize_inventory,
)
from windows import CandidateSession, validate_sessions

T0 = datetime(2026, 9, 20, 0, 0, 0, tzinfo=timezone.utc)


# --- iter_window_starts -------------------------------------------------------

def test_iter_window_starts_fits_exact_multiple():
    # 정확히 60+15*3=105초 구간 - 60초 창이 [0,60) [15,75) [30,90) [45,105) 4개
    starts = iter_window_starts(T0, T0 + timedelta(seconds=105))
    assert len(starts) == 4
    assert starts[0] == T0
    assert starts[-1] == T0 + timedelta(seconds=45)
    print("OK - 세션 길이가 window+step의 배수면 딱 맞게 창이 생성됨")


def test_iter_window_starts_never_exceeds_session_end():
    starts = iter_window_starts(T0, T0 + timedelta(seconds=70))  # 60초 창 하나 + 10초 남음(다음 창 못 만듦)
    assert len(starts) == 1
    print("OK - 세션 밖으로 새는 창은 만들지 않음(마지막 자투리 구간은 버림)")


def test_iter_window_starts_too_short_session_yields_nothing():
    starts = iter_window_starts(T0, T0 + timedelta(seconds=59))
    assert starts == []
    print("OK - 세션이 window_sec보다 짧으면 창이 0개")


# --- extract_window_strict / build_rows_for_session --------------------------

def _fake_query_range_all_present(promql, start, end):
    return [1.0, 2.0, 3.0]


def _fake_query_range_one_metric_missing(missing_promql):
    def fn(promql, start, end):
        return [] if promql == missing_promql else [1.0, 2.0, 3.0]
    return fn


def test_extract_window_strict_all_present_succeeds():
    feats, reason = extract_window_strict(T0, T0 + timedelta(seconds=60), _fake_query_range_all_present)
    assert reason is None
    assert len(feats) == 8  # 4 metrics x (mean, slope)
    print("OK - 4개 지표 전부 응답 있으면 8-feature 벡터 반환")


def test_extract_window_strict_rejects_on_any_missing_metric():
    from features import METRICS
    missing = list(METRICS.values())[2]  # queue
    feats, reason = extract_window_strict(T0, T0 + timedelta(seconds=60), _fake_query_range_one_metric_missing(missing))
    assert feats is None
    assert reason is not None and "queue" in reason
    print("OK - 지표 하나라도 빈 응답이면 0으로 채우지 않고 전체 창을 invalid 처리")


def test_build_rows_for_session_marks_each_window_valid_or_invalid():
    session = CandidateSession("s1", "probe_baseline", "active_only", T0, T0 + timedelta(seconds=90), "run-1")
    rows = build_rows_for_session(session, _fake_query_range_all_present)
    assert len(rows) == 3  # [0,60) [15,75) [30,90)
    assert all(r.valid and r.session_id == "s1" for r in rows)
    print("OK - 세션의 모든 창이 session_id/topology/regime을 그대로 물려받고 valid로 기록됨")


# --- 중복 검출 -----------------------------------------------------------------

def _row(session_id, ts, features=None, valid=True):
    return WindowRow(session_id=session_id, regime="r", topology="active_only", source_run_id="x",
                      window_start_utc=ts, window_end_utc=ts, valid=valid, features=features)


def test_find_duplicate_timestamps_detects_cross_session_overlap():
    rows = [_row("s1", "2026-01-01T00:00:00"), _row("s2", "2026-01-01T00:00:00"), _row("s1", "2026-01-01T00:00:15")]
    dups = find_duplicate_timestamps(rows)
    assert len(dups) == 1 and dups[0][0] == "2026-01-01T00:00:00"
    print("OK - 서로 다른 세션이 같은 window_start_utc를 쓰면 검출됨")


def test_find_duplicate_feature_vectors_detects_identical_vectors():
    rows = [_row("s1", "t1", features=[1.0, 2.0]), _row("s2", "t2", features=[1.0, 2.0]),
            _row("s3", "t3", features=[9.0, 9.0])]
    dups = find_duplicate_feature_vectors(rows)
    assert len(dups) == 1
    print("OK - 서로 다른 (세션,창)이 부동소수점까지 같은 벡터를 내면 검출됨")


def test_find_duplicate_feature_vectors_ignores_invalid_rows():
    rows = [_row("s1", "t1", features=None, valid=False), _row("s2", "t2", features=None, valid=False)]
    assert find_duplicate_feature_vectors(rows) == []
    print("OK - invalid(features=None) row는 중복 비교 대상이 아님")


# --- split_sessions -------------------------------------------------------------

def _sessions(regime_topology_pairs):
    return [CandidateSession(f"s{i}", regime, topo, T0, T0 + timedelta(seconds=60), f"run-{i}")
            for i, (regime, topo) in enumerate(regime_topology_pairs)]


def test_split_sessions_never_splits_same_session_across_buckets():
    sessions = _sessions([("r1", "active_only")] * 6)
    split = split_sessions(sessions, seed=1)
    all_ids = split["train"] + split["calibration"] + split["holdout"]
    assert sorted(all_ids) == sorted(s.session_id for s in sessions)
    assert len(set(all_ids)) == len(all_ids)
    print("OK - 모든 세션이 정확히 한 split에만 배정되고 하나도 안 빠짐")


def test_split_sessions_is_deterministic_given_same_seed():
    sessions = _sessions([("r1", "active_only")] * 9)
    a = split_sessions(sessions, seed=42)
    b = split_sessions(sessions, seed=42)
    assert a == b
    print("OK - 같은 seed면 항상 같은 split(재현 가능)")


def test_split_sessions_different_seed_can_differ():
    sessions = _sessions([("r1", "active_only")] * 9)
    a = split_sessions(sessions, seed=1)
    b = split_sessions(sessions, seed=2)
    assert a["train"] != b["train"] or a["holdout"] != b["holdout"]
    print("OK - seed가 다르면 배정도 달라질 수 있음(seed가 실제로 쓰이고 있다는 증거)")


def test_split_sessions_stratifies_by_regime_and_topology_independently():
    # 5개 active_plus_preview + 5개 active_only - 층화 안 하면 한 조합이
    # 통째로 한 split에 몰릴 수 있다(실측 확인된 문제, §2/§4).
    sessions = _sessions([("r1", "active_plus_preview")] * 5 + [("r1", "active_only")] * 5)
    split = split_sessions(sessions, seed=7)

    def topo_of(sid):
        return next(s.topology for s in sessions if s.session_id == sid)

    for bucket in ("train", "calibration", "holdout"):
        topologies = {topo_of(sid) for sid in split[bucket]}
        assert topologies == {"active_plus_preview", "active_only"}, (
            f"{bucket}에 두 topology가 모두 있어야 함, 실제: {topologies}")
    print("OK - (regime, topology) 조합별로 층화해 모든 split에 두 topology가 다 들어감")


def test_split_sessions_reports_shortfall_for_single_session_regime():
    sessions = _sessions([("rare_regime", "active_only")])
    split = split_sessions(sessions, seed=1)
    assert split["train"] == ["s0"]
    assert split["calibration"] == [] and split["holdout"] == []
    assert any("rare_regime" in s for s in split["shortfalls"])
    print("OK - 세션 1개뿐인 조합은 train에만 배정하고 shortfall로 명시(거짓 3분할 안 함)")


# --- validate_sessions (windows.py) ---------------------------------------------

def test_validate_sessions_rejects_pre_cutover_session():
    from windows import CORE3_CUTOVER_UTC
    bad = [CandidateSession("s1", "r", "active_only", CORE3_CUTOVER_UTC - timedelta(days=1),
                             CORE3_CUTOVER_UTC - timedelta(hours=1), "run-1")]
    problems = validate_sessions(bad)
    assert any("3코어 전환" in p for p in problems)
    print("OK - 3코어 전환 이전 세션은 validate_sessions()가 잡아냄")


def test_validate_sessions_accepts_registered_candidates():
    assert validate_sessions() == []
    print("OK - windows.py에 등록된 실제 후보 12개는 전부 검증 통과")


# --- summarize_inventory (순수 집계) --------------------------------------------

def test_summarize_inventory_separates_row_count_from_session_count():
    session = CandidateSession("s1", "probe_baseline", "active_only", T0, T0 + timedelta(seconds=90), "run-1")
    rows = build_rows_for_session(session, _fake_query_range_all_present)  # 3 rows, 1 session
    inv = summarize_inventory(rows, [session])
    assert inv["num_candidate_sessions"] == 1
    assert inv["total_rows"] == 3
    print("OK - 같은 세션의 겹치는 row가 여러 개여도 session 수와 row 수를 따로 보고")


def test_summarize_inventory_flags_queue_nonzero_fraction():
    session = CandidateSession("s1", "probe_baseline", "active_only", T0, T0 + timedelta(seconds=60), "run-1")
    rows = build_rows_for_session(session, _fake_query_range_all_present)
    inv = summarize_inventory(rows, [session])
    # _fake_query_range_all_present는 모든 지표에 [1,2,3]을 주므로 queue_mean도 0이 아님
    assert inv["queue_mean_nonzero_valid_rows"] == len(rows)
    print("OK - queue_mean이 0이 아닌 valid row 수를 별도로 집계(v1의 '항상 0' 문제 재확인용)")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        t()
    print(f"전체 통과 ({len(tests)}개)")
