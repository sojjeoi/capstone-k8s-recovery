#!/usr/bin/env python3
"""verify_memory_pressure_negative_control.py 검증 - 순수 함수만 대상(§94.5
반복별 PASS 판정·§94.8 3회 재현성 판정). run_round()의 라이브 오케스트레이션
자체는 explore_memory_pressure_intensity.py의 다른 테스트가 이미 다루는
범위 밖으로 취급하는 것과 같은 이유로 오프라인 테스트 대상이 아니다
(실클러스터·Prometheus·probe pod 의존). judge_direct_safety()(§56, 재사용)
자체의 10개 기준 각각에 대한 세부 테스트는 test_verify_memory_pressure_
direct_candidate.py가 이미 커버하므로 여기서 중복하지 않는다 - 이 파일은
§94에서 새로 추가된 두 판정(working set 상승·SLO 위반 부재)과 3회 종합
판정만 검증한다."""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from verify_memory_pressure_negative_control import (
    MIN_WORKING_SET_RISE_BYTES,
    judge_negative_control_repetition,
    judge_negative_control_reproducibility,
    stage_violates,
)

GIB = 1024 ** 3
MIB = 1024 ** 2
HEALTHY = {"Ready": "True", "MemoryPressure": "False", "DiskPressure": "False", "PIDPressure": "False"}


def _clean_result(t_slo=None, t_slo_within_window=False, working_set_rise_bytes=850 * MIB):
    return {
        "aborted": False,
        "all_injected_confirmed": True,
        "slo": {"success_rate": 1.0},
        "cleanup_confirmed": True,
        "target_replacement": None,
        "working_set_rise_bytes": working_set_rise_bytes,
        "ticks": [
            {"working_set_bytes": 4.7 * GIB, "node_available_bytes": 8 * GIB,
             "restart_count": 0, "oom_killed": False, "node_conditions": dict(HEALTHY)},
            {"working_set_bytes": 4.75 * GIB, "node_available_bytes": 7.8 * GIB,
             "restart_count": 0, "oom_killed": False, "node_conditions": dict(HEALTHY)},
            {"event": "cleanup_recovery_check", "recovered": True},
        ],
        "scoped_slo": {"t_slo": t_slo, "t_slo_within_window": t_slo_within_window},
    }


# --- stage_violates (§94의 독립 재정의, §56과 동일 로직) ----------------------

def test_stage_violates_requires_both_t_slo_and_within_window():
    assert stage_violates(_clean_result(t_slo=None, t_slo_within_window=False)) is False
    assert stage_violates(_clean_result(t_slo="X", t_slo_within_window=False)) is False, (
        "t_slo가 있어도 stage 경계 밖(다음 관측 구간으로 샌 사건)이면 위반으로 안 침")
    assert stage_violates(_clean_result(t_slo="X", t_slo_within_window=True)) is True
    print("OK - t_slo not None AND t_slo_within_window일 때만 위반으로 판정")


def test_stage_violates_false_on_scoped_slo_error():
    r = _clean_result()
    r["scoped_slo"] = {"error": "주입 시각 미확보"}
    assert stage_violates(r) is False
    print("OK - scoped_slo 계산 실패(중단 등)는 위반으로 세지 않음")


# --- judge_negative_control_repetition ----------------------------------------

def test_judge_negative_control_repetition_passes_clean_run():
    verdict = judge_negative_control_repetition(_clean_result())
    assert verdict == {"pass": True, "reasons": []}
    print("OK - 안전 기준 전부 충족 + working set 상승 충분 + SLO 위반 없음이면 pass=True")

def test_judge_negative_control_repetition_reuses_existing_safety_criteria():
    # judge_direct_safety()의 기존 10개 기준(§56) 중 하나라도 깨지면
    # 그대로 실패해야 한다 - 재사용 확인(중복 재구현 안 함).
    r = _clean_result()
    r["all_injected_confirmed"] = False
    verdict = judge_negative_control_repetition(r)
    assert verdict["pass"] is False
    assert any("AllInjected" in reason for reason in verdict["reasons"])
    print("OK - 기존 §56 안전 기준(judge_direct_safety)이 그대로 재사용됨")


def test_judge_negative_control_repetition_fails_on_insufficient_working_set_rise():
    r = _clean_result(working_set_rise_bytes=799 * MIB)
    verdict = judge_negative_control_repetition(r)
    assert verdict["pass"] is False
    assert any("800MiB" in reason for reason in verdict["reasons"])
    print("OK - working set 상승이 800MiB 미만이면 실패")


def test_judge_negative_control_repetition_passes_at_exactly_800mib_rise():
    r = _clean_result(working_set_rise_bytes=MIN_WORKING_SET_RISE_BYTES)
    verdict = judge_negative_control_repetition(r)
    assert verdict["pass"] is True
    print("OK - 정확히 800MiB 상승이면 경계값 통과(>=)")


def test_judge_negative_control_repetition_fails_on_missing_working_set_rise():
    r = _clean_result()
    r["working_set_rise_bytes"] = None
    verdict = judge_negative_control_repetition(r)
    assert verdict["pass"] is False
    assert any("미확보" in reason for reason in verdict["reasons"])
    print("OK - working_set_rise_bytes를 아예 못 구했으면(None) 실패(fail-closed)")


def test_judge_negative_control_repetition_fails_on_sustained_slo_violation():
    # §56과 정반대 방향 - 여기서는 위반이 "성공 신호"가 아니라 "부적격 신호".
    r = _clean_result(t_slo="2026-01-01T00:01:00+00:00", t_slo_within_window=True)
    verdict = judge_negative_control_repetition(r)
    assert verdict["pass"] is False
    assert any("negative control 부적격" in reason for reason in verdict["reasons"])
    print("OK - sustained SLO 위반이 발생하면 실패(negative control 부적격)")


def test_judge_negative_control_repetition_ignores_violation_outside_stage_window():
    # t_slo가 있어도 stage 경계 밖으로 샌 사건이면(§55.2/§56.3 원칙) 이
    # 반복의 위반으로 세지 않는다 - 다른 조건이 다 맞으면 여전히 PASS.
    r = _clean_result(t_slo="2026-01-01T00:05:00+00:00", t_slo_within_window=False)
    verdict = judge_negative_control_repetition(r)
    assert verdict["pass"] is True
    print("OK - stage 경계 밖으로 샌 t_slo는 이 반복의 위반으로 안 셈(§55.2 원칙 재사용)")


def test_judge_negative_control_repetition_momentary_p95_alone_does_not_fail():
    # p95_peak가 순간적으로 threshold를 넘어도 t_slo(sustained)가 없으면
    # PASS해야 한다(지시 그대로) - scoped_slo에 p95_peak만 높고 t_slo=None.
    r = _clean_result(t_slo=None, t_slo_within_window=False)
    r["scoped_slo"]["p95_peak"] = 999.0
    verdict = judge_negative_control_repetition(r)
    assert verdict["pass"] is True
    print("OK - 순간적 P95 초과만으로는(t_slo 미발생) 실패하지 않음(기록만)")


# --- judge_negative_control_reproducibility (§94.8) ---------------------------

def _rep(passed=True):
    return {"negative_control": {"pass": passed, "reasons": [] if passed else ["dummy"]}}


def test_judge_negative_control_reproducibility_passes_when_3_of_3_pass():
    verdict = judge_negative_control_reproducibility([_rep(), _rep(), _rep()])
    assert verdict["overall_pass"] is True
    print("OK - 3/3 전부 PASS면 종합 PASS(Freeze 가능)")


def test_judge_negative_control_reproducibility_fails_when_any_rep_fails():
    verdict = judge_negative_control_reproducibility([_rep(), _rep(passed=False), _rep()])
    assert verdict["overall_pass"] is False
    assert verdict["checks"]["all_reps_pass"] is False
    print("OK - 한 회차라도 실패(안전 실패 또는 SLO 위반)하면 종합 실패 - §56과 달리 부분 위반 허용 없음")


def test_judge_negative_control_reproducibility_fails_when_fewer_than_3_repetitions():
    verdict = judge_negative_control_reproducibility([_rep(), _rep()])
    assert verdict["overall_pass"] is False
    assert verdict["checks"]["enough_repetitions"] is False
    print("OK - 3회 미만이면(중단으로 조기 종료) 실패")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        t()
    print(f"전체 통과 ({len(tests)}개)")
