#!/usr/bin/env python3
"""verify_memory_pressure_direct_candidate.py 검증 - 순수 함수만 대상(§56.2
안전 판정·§56.4 SLO 재현성 판정). run_round()의 라이브 오케스트레이션 자체는
explore_memory_pressure_intensity.py의 다른 테스트가 이미 커버 범위 밖으로
다루는 것과 같은 이유로 오프라인 테스트 대상이 아니다(실클러스터·
Prometheus·probe pod 의존)."""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from verify_memory_pressure_direct_candidate import (
    MAX_TARGET_WORKING_SET_BYTES,
    judge_direct_reproducibility,
    judge_direct_safety,
    stage_violates,
)

GIB = 1024 ** 3
HEALTHY = {"Ready": "True", "MemoryPressure": "False", "DiskPressure": "False", "PIDPressure": "False"}
NOT_READY = {"Ready": "False", "MemoryPressure": "False", "DiskPressure": "False", "PIDPressure": "False"}


def _clean_result(t_slo=None, t_slo_within_window=False):
    return {
        "aborted": False,
        "all_injected_confirmed": True,
        "slo": {"success_rate": 1.0},
        "cleanup_confirmed": True,
        "target_replacement": None,
        "ticks": [
            {"working_set_bytes": 4.7 * GIB, "node_available_bytes": 8 * GIB,
             "restart_count": 0, "oom_killed": False, "node_conditions": dict(HEALTHY)},
            {"working_set_bytes": 4.75 * GIB, "node_available_bytes": 7.8 * GIB,
             "restart_count": 0, "oom_killed": False, "node_conditions": dict(HEALTHY)},
            {"event": "cleanup_recovery_check", "recovered": True},
        ],
        "scoped_slo": {"t_slo": t_slo, "t_slo_within_window": t_slo_within_window},
    }


# --- judge_direct_safety ------------------------------------------------------

def test_judge_direct_safety_all_criteria_met():
    verdict = judge_direct_safety(_clean_result())
    assert verdict == {"pass": True, "reasons": []}
    print("OK - 10개 안전 기준 전부 충족 시 pass=True")


def test_judge_direct_safety_aborted_short_circuits():
    r = _clean_result()
    r["aborted"] = True
    r["abort_reason"] = "Node MemAvailable 부족"
    verdict = judge_direct_safety(r)
    assert verdict["pass"] is False
    assert "Node MemAvailable 부족" in verdict["reasons"][0]
    print("OK - aborted면 다른 조건 계산 없이 즉시 실패")


def test_judge_direct_safety_fails_on_allinjected_not_confirmed():
    r = _clean_result()
    r["all_injected_confirmed"] = False
    verdict = judge_direct_safety(r)
    assert verdict["pass"] is False
    assert any("AllInjected" in reason for reason in verdict["reasons"])
    print("OK - AllInjected 미확인이면 실패")


def test_judge_direct_safety_fails_on_incomplete_success_rate():
    r = _clean_result()
    r["slo"] = {"success_rate": 0.99}
    verdict = judge_direct_safety(r)
    assert verdict["pass"] is False
    assert any("completion 성공률" in reason for reason in verdict["reasons"])
    print("OK - completion 성공률이 100%가 아니면 실패")


def test_judge_direct_safety_fails_on_working_set_at_or_above_ceiling():
    r = _clean_result()
    r["ticks"][0]["working_set_bytes"] = MAX_TARGET_WORKING_SET_BYTES
    verdict = judge_direct_safety(r)
    assert verdict["pass"] is False
    assert any("5GiB 이상" in reason for reason in verdict["reasons"])
    print("OK - working set이 5GiB 이상인 tick이 있으면 실패")


def test_judge_direct_safety_fails_on_low_node_available():
    r = _clean_result()
    r["ticks"][0]["node_available_bytes"] = 3.9 * GIB
    verdict = judge_direct_safety(r)
    assert verdict["pass"] is False
    assert any("4GiB 미만" in reason for reason in verdict["reasons"])
    print("OK - Node MemAvailable이 4GiB 미만인 tick이 있으면 실패")


def test_judge_direct_safety_fails_on_restart_count_change():
    r = _clean_result()
    r["ticks"][0]["restart_count"] = 1
    verdict = judge_direct_safety(r)
    assert verdict["pass"] is False
    assert any("restartCount" in reason for reason in verdict["reasons"])
    print("OK - restartCount가 tick 사이에 바뀌면 실패")


def test_judge_direct_safety_fails_on_oom_killed():
    r = _clean_result()
    r["ticks"][0]["oom_killed"] = True
    verdict = judge_direct_safety(r)
    assert verdict["pass"] is False
    assert any("OOMKilled" in reason for reason in verdict["reasons"])
    print("OK - OOMKilled 관측 시 실패")


def test_judge_direct_safety_fails_on_node_unhealthy():
    r = _clean_result()
    r["ticks"][0]["node_conditions"] = dict(NOT_READY)
    verdict = judge_direct_safety(r)
    assert verdict["pass"] is False
    assert any("Node 상태 이상" in reason for reason in verdict["reasons"])
    print("OK - Node 상태 이상 tick이 있으면 실패")


def test_judge_direct_safety_fails_on_target_uid_change():
    r = _clean_result()
    r["target_replacement"] = {"replaced_at": "2026-01-01T00:00:00+00:00", "replacement_pod": {"uid": "new"}}
    verdict = judge_direct_safety(r)
    assert verdict["pass"] is False
    assert any("target UID 변경" in reason for reason in verdict["reasons"])
    print("OK - target UID가 바뀌면(효과 후 교체 포함) 실패")


def test_judge_direct_safety_fails_when_final_recovery_not_confirmed():
    r = _clean_result()
    r["ticks"][-1]["recovered"] = False
    verdict = judge_direct_safety(r)
    assert verdict["pass"] is False
    assert any("30초" in reason for reason in verdict["reasons"])
    print("OK - cleanup_recovery_check가 recovered=False면 실패")


def test_judge_direct_safety_fails_when_cleanup_not_confirmed():
    r = _clean_result()
    r["cleanup_confirmed"] = False
    verdict = judge_direct_safety(r)
    assert verdict["pass"] is False
    assert any("CR·observer·context" in reason for reason in verdict["reasons"])
    print("OK - CR·observer·context 정리 확인 안 되면 실패")


# --- stage_violates / judge_direct_reproducibility ---------------------------

def test_stage_violates_requires_both_t_slo_and_within_window():
    assert stage_violates(_clean_result(t_slo=None, t_slo_within_window=False)) is False
    assert stage_violates(_clean_result(t_slo="X", t_slo_within_window=False)) is False, (
        "t_slo가 있어도 stage 경계 밖(다음 관측 구간으로 샌 사건)이면 위반으로 안 침")
    assert stage_violates(_clean_result(t_slo="X", t_slo_within_window=True)) is True
    print("OK - t_slo not None AND t_slo_within_window일 때만 위반으로 판정(§55.2 원칙 재사용)")


def test_stage_violates_false_on_scoped_slo_error():
    r = _clean_result()
    r["scoped_slo"] = {"error": "주입 시각 미확보"}
    assert stage_violates(r) is False
    print("OK - scoped_slo 계산 실패(중단 등)는 위반으로 세지 않음")


def _rep(safety_pass=True, t_slo=None, t_slo_within_window=False):
    r = _clean_result(t_slo, t_slo_within_window)
    r["safety"] = {"pass": safety_pass, "reasons": [] if safety_pass else ["dummy"]}
    return r


def test_judge_direct_reproducibility_passes_when_2_of_3_violate():
    reps = [_rep(t_slo="X", t_slo_within_window=True), _rep(t_slo="X", t_slo_within_window=True), _rep()]
    verdict = judge_direct_reproducibility(reps)
    assert verdict["overall_pass"] is True
    assert verdict["violate_count"] == 2
    print("OK - 3회 중 2회 sustained 위반 + 안전 전부 PASS면 종합 PASS")


def test_judge_direct_reproducibility_fails_when_only_1_of_3_violate():
    reps = [_rep(t_slo="X", t_slo_within_window=True), _rep(), _rep()]
    verdict = judge_direct_reproducibility(reps)
    assert verdict["overall_pass"] is False
    assert verdict["violate_count"] == 1
    print("OK - 1/3회만 위반하면(기준 2/3 미달) 실패")


def test_judge_direct_reproducibility_fails_when_any_rep_safety_fails():
    reps = [_rep(t_slo="X", t_slo_within_window=True), _rep(safety_pass=False), _rep()]
    verdict = judge_direct_reproducibility(reps)
    assert verdict["overall_pass"] is False
    assert verdict["checks"]["all_reps_safety_pass"] is False
    print("OK - 한 회차라도 안전 기준을 못 충족하면 종합 실패(위반 횟수와 무관)")


def test_judge_direct_reproducibility_fails_when_fewer_than_3_repetitions():
    reps = [_rep(t_slo="X", t_slo_within_window=True), _rep(t_slo="X", t_slo_within_window=True)]
    verdict = judge_direct_reproducibility(reps)
    assert verdict["overall_pass"] is False
    assert verdict["checks"]["enough_repetitions"] is False
    print("OK - 3회 미만이면(중단으로 조기 종료) 실패")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        t()
    print(f"전체 통과 ({len(tests)}개)")
