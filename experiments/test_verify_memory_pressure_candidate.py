#!/usr/bin/env python3
"""verify_memory_pressure_candidate.py 검증 - 순수 함수만 대상(§54.4 안전
재현성 판정·stage별 SLO 재현성 판정·tick 구간 분류). run_candidate()의 라이브
오케스트레이션 자체는 explore_ramp_intensity.run_candidate()/explore_memory_
pressure_intensity.run_round()와 같은 이유로 오프라인 테스트 대상이 아니다
(실클러스터·Prometheus·probe pod 의존)."""
import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")

from verify_memory_pressure_candidate import (
    CANDIDATE_STAGES,
    GIB,
    MAX_TARGET_WORKING_SET_BYTES,
    STAGE_500_NAME,
    STAGE_1000_NAME,
    STAGE_1500_NAME,
    bucket_ticks_by_stage,
    judge_reproducibility,
    judge_safety,
)

HEALTHY = {"Ready": "True", "MemoryPressure": "False", "DiskPressure": "False", "PIDPressure": "False"}
NOT_READY = {"Ready": "False", "MemoryPressure": "False", "DiskPressure": "False", "PIDPressure": "False"}


# --- bucket_ticks_by_stage ---------------------------------------------------

def test_bucket_ticks_by_stage_splits_by_actual_window():
    windows = [
        {"name": "s1", "start": "2026-01-01T00:00:00+00:00", "end": "2026-01-01T00:02:00+00:00"},
        {"name": "s2", "start": "2026-01-01T00:02:00+00:00", "end": "2026-01-01T00:04:00+00:00"},
    ]
    ticks = [
        {"event": "safety_tick", "ts": "2026-01-01T00:00:30+00:00", "working_set_bytes": 1},
        {"event": "safety_tick", "ts": "2026-01-01T00:02:30+00:00", "working_set_bytes": 2},
        {"event": "safety_tick", "ts": "2026-01-01T00:05:00+00:00", "working_set_bytes": 3},  # 창 밖(drain)
        {"event": "prepare_ok", "ts": "2026-01-01T00:00:10+00:00"},  # safety_tick 아님 - 무시
    ]
    buckets = bucket_ticks_by_stage(ticks, windows)
    assert [t["working_set_bytes"] for t in buckets["s1"]] == [1]
    assert [t["working_set_bytes"] for t in buckets["s2"]] == [2]
    print("OK - 실제 stage 경계로 safety_tick만 정확히 분류, 창 밖·다른 이벤트는 제외")


def test_bucket_ticks_by_stage_boundary_is_half_open():
    windows = [{"name": "s1", "start": "2026-01-01T00:00:00+00:00", "end": "2026-01-01T00:01:00+00:00"}]
    ticks = [{"event": "safety_tick", "ts": "2026-01-01T00:01:00+00:00", "working_set_bytes": 9}]
    buckets = bucket_ticks_by_stage(ticks, windows)
    assert buckets["s1"] == [], "end 시각 자체는 이 stage에 안 들어감([start, end) 반개구간)"
    print("OK - stage 경계는 [start, end) 반개구간 - end와 정확히 같은 tick은 다음 구간으로")


# --- judge_safety -------------------------------------------------------------

def _clean_stage(name, size_mb, t_slo=None):
    return {"name": name, "size_mb": size_mb, "duration_sec": 120.0, "all_injected": True,
            "slo": {"t_slo": t_slo}}


def _clean_result(stage_t_slo=(None, None, None)):
    windows = [{"name": s["name"], "all_injected": True} for s in CANDIDATE_STAGES]
    return {
        "aborted": False,
        "stage_windows": windows,
        "overall_slo": {"success_rate": 1.0},
        "cleanup_confirmed": True,
        "ticks": [
            {"working_set_bytes": 3.5 * GIB, "node_available_bytes": 8 * GIB,
             "restart_count": 0, "oom_killed": False, "node_conditions": dict(HEALTHY)},
            {"working_set_bytes": 3.6 * GIB, "node_available_bytes": 7.8 * GIB,
             "restart_count": 0, "oom_killed": False, "node_conditions": dict(HEALTHY)},
            {"event": "cleanup_recovery_check", "recovered": True},
        ],
        "stages": [_clean_stage(STAGE_500_NAME, 500.0, stage_t_slo[0]),
                   _clean_stage(STAGE_1000_NAME, 1000.0, stage_t_slo[1]),
                   _clean_stage(STAGE_1500_NAME, 1500.0, stage_t_slo[2])],
    }


def test_judge_safety_all_criteria_met():
    verdict = judge_safety(_clean_result())
    assert verdict == {"pass": True, "reasons": []}
    print("OK - 9개 안전 기준 전부 충족 시 pass=True")


def test_judge_safety_aborted_short_circuits():
    r = _clean_result()
    r["aborted"] = True
    r["abort_reason"] = "Node MemAvailable 부족"
    verdict = judge_safety(r)
    assert verdict["pass"] is False
    assert "Node MemAvailable 부족" in verdict["reasons"][0]
    print("OK - aborted면 다른 조건 계산 없이 즉시 실패")


def test_judge_safety_fails_when_stage_window_missing():
    r = _clean_result()
    r["stage_windows"] = r["stage_windows"][:2]  # 관측 데이터 손실 시뮬레이션
    verdict = judge_safety(r)
    assert verdict["pass"] is False
    assert any("stage 수 불일치" in reason for reason in verdict["reasons"])
    print("OK - stage 창이 예상 개수보다 적으면(데이터 손실) 실패")


def test_judge_safety_fails_when_a_stage_not_all_injected():
    r = _clean_result()
    r["stage_windows"][2]["all_injected"] = False
    verdict = judge_safety(r)
    assert verdict["pass"] is False
    assert any("AllInjected 미확인" in reason for reason in verdict["reasons"])
    print("OK - 어느 한 stage라도 AllInjected 미확인이면 실패")


def test_judge_safety_fails_on_incomplete_success_rate():
    r = _clean_result()
    r["overall_slo"] = {"success_rate": 0.99}
    verdict = judge_safety(r)
    assert verdict["pass"] is False
    assert any("completion 성공률" in reason for reason in verdict["reasons"])
    print("OK - completion 성공률이 100%가 아니면 실패")


def test_judge_safety_fails_on_working_set_at_or_above_ceiling():
    r = _clean_result()
    r["ticks"][0]["working_set_bytes"] = MAX_TARGET_WORKING_SET_BYTES
    verdict = judge_safety(r)
    assert verdict["pass"] is False
    assert any("5GiB 이상" in reason for reason in verdict["reasons"])
    print("OK - working set이 5GiB 이상인 tick이 있으면 실패")


def test_judge_safety_fails_on_low_node_available():
    r = _clean_result()
    r["ticks"][0]["node_available_bytes"] = 3.9 * GIB
    verdict = judge_safety(r)
    assert verdict["pass"] is False
    assert any("4GiB 미만" in reason for reason in verdict["reasons"])
    print("OK - Node MemAvailable이 4GiB 미만인 tick이 있으면 실패")


def test_judge_safety_fails_on_restart_count_change():
    r = _clean_result()
    r["ticks"][0]["restart_count"] = 1
    verdict = judge_safety(r)
    assert verdict["pass"] is False
    assert any("restartCount" in reason for reason in verdict["reasons"])
    print("OK - restartCount가 tick 사이에 바뀌면 실패")


def test_judge_safety_fails_on_oom_killed():
    r = _clean_result()
    r["ticks"][0]["oom_killed"] = True
    verdict = judge_safety(r)
    assert verdict["pass"] is False
    assert any("OOMKilled" in reason for reason in verdict["reasons"])
    print("OK - OOMKilled 관측 시 실패")


def test_judge_safety_fails_on_node_unhealthy():
    r = _clean_result()
    r["ticks"][0]["node_conditions"] = dict(NOT_READY)
    verdict = judge_safety(r)
    assert verdict["pass"] is False
    assert any("Node 상태 이상" in reason for reason in verdict["reasons"])
    print("OK - Node 상태 이상 tick이 있으면 실패")


def test_judge_safety_fails_when_final_recovery_not_confirmed():
    r = _clean_result()
    r["ticks"][-1]["recovered"] = False
    verdict = judge_safety(r)
    assert verdict["pass"] is False
    assert any("30초" in reason for reason in verdict["reasons"])
    print("OK - 최종 cleanup_recovery_check가 recovered=False면 실패")


def test_judge_safety_fails_when_cleanup_not_confirmed():
    r = _clean_result()
    r["cleanup_confirmed"] = False
    verdict = judge_safety(r)
    assert verdict["pass"] is False
    assert any("CR·observer" in reason for reason in verdict["reasons"])
    print("OK - CR·observer 정리 확인 안 되면 실패")


# --- judge_reproducibility ----------------------------------------------------

def _rep(safety_pass=True, t_slo_500=None, t_slo_1000=None, t_slo_1500=None):
    r = _clean_result((t_slo_500, t_slo_1000, t_slo_1500))
    r["safety"] = {"pass": safety_pass, "reasons": [] if safety_pass else ["dummy"]}
    return r


def test_judge_reproducibility_passes_clean_pattern():
    reps = [_rep(t_slo_1500="2026-01-01T00:03:00+00:00"),
            _rep(t_slo_1500="2026-01-01T00:03:00+00:00"),
            _rep(t_slo_1500=None)]
    verdict = judge_reproducibility(reps)
    assert verdict["overall_pass"] is True
    print("OK - 500/1000 3회 모두 미위반 + 1500 2/3 위반 + 안전 전부 PASS면 종합 PASS")


def test_judge_reproducibility_fails_when_500mb_violates_once():
    reps = [_rep(t_slo_500="X", t_slo_1500="X"), _rep(t_slo_1500="X"), _rep()]
    verdict = judge_reproducibility(reps)
    assert verdict["overall_pass"] is False
    assert verdict["checks"][f"{STAGE_500_NAME}_never_violates"] is False
    print("OK - 500MB가 단 1회라도 위반하면 실패")


def test_judge_reproducibility_fails_when_1500mb_violates_only_once():
    reps = [_rep(t_slo_1500="X"), _rep(), _rep()]
    verdict = judge_reproducibility(reps)
    assert verdict["overall_pass"] is False
    assert verdict["checks"][f"{STAGE_1500_NAME}_violates_at_least_2_of_3"] is False
    print("OK - 1500MB가 1/3회만 위반하면(기준 2/3 미달) 실패")


def test_judge_reproducibility_fails_when_any_rep_safety_fails():
    reps = [_rep(t_slo_1500="X"), _rep(t_slo_1500="X", safety_pass=False), _rep()]
    verdict = judge_reproducibility(reps)
    assert verdict["overall_pass"] is False
    assert verdict["checks"]["all_reps_safety_pass"] is False
    print("OK - 한 회차라도 안전 기준을 못 충족하면 종합 실패")


def test_judge_reproducibility_fails_when_fewer_than_3_repetitions():
    reps = [_rep(t_slo_1500="X"), _rep()]
    verdict = judge_reproducibility(reps)
    assert verdict["overall_pass"] is False
    assert verdict["checks"]["enough_repetitions"] is False
    print("OK - 3회 미만이면(중단으로 조기 종료) 실패")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        t()
    print(f"전체 통과 ({len(tests)}개)")
