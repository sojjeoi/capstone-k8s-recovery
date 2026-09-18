#!/usr/bin/env python3
"""verify_ramp_candidate.judge_candidate() 검증 - §25 사전 등록 기준을
기계적으로 올바르게 적용하는지 확인한다(이 함수가 실제 후보 동결 여부를
결정하므로 실행 전에 반드시 통과해야 함)."""
from verify_ramp_candidate import judge_candidate


def _stage(rps, violates):
    return {"stage": f"explore-{rps}rps", "target_rps": rps, "violates": violates,
            "n": 90, "success_rate": 1.0}


def _rep(low_violates=(False, False), high_violates=(True, True), success_100=True,
          drain_violates=False, node_clean=True, valid=True):
    if not valid:
        return {"valid": False, "reason": "baseline_violating"}
    stages = [_stage(0.025, low_violates[0]), _stage(0.05, low_violates[1]),
              _stage(0.20, False), _stage(0.30, high_violates[0]), _stage(0.40, high_violates[1])]
    return {"valid": True, "stages": stages, "all_success_100pct": success_100,
            "drain": {"violates": drain_violates}, "node_pod_clean": node_clean}


def test_clean_pass_all_criteria_met():
    reps = [_rep(), _rep(), _rep()]
    verdict = judge_candidate(reps)
    assert verdict["overall_pass"] is True
    assert verdict["num_valid"] == 3
    assert verdict["num_invalid"] == 0


def test_fails_when_low_rps_violates_even_once():
    reps = [_rep(), _rep(low_violates=(True, False)), _rep()]
    verdict = judge_candidate(reps)
    assert verdict["overall_pass"] is False
    assert verdict["checks"]["0.025rps_never_violates"] is False


def test_fails_when_high_rps_violates_only_1_of_3():
    reps = [_rep(high_violates=(True, True)), _rep(high_violates=(False, True)),
            _rep(high_violates=(False, True))]
    verdict = judge_candidate(reps)
    assert verdict["overall_pass"] is False
    # 0.30rps: 1/3만 위반 -> 기준(2/3) 미충족
    assert any(not v for k, v in verdict["checks"].items() if k.startswith("0.3rps"))


def test_passes_when_high_rps_violates_exactly_2_of_3():
    reps = [_rep(high_violates=(True, True)), _rep(high_violates=(True, True)),
            _rep(high_violates=(False, False))]
    verdict = judge_candidate(reps)
    assert verdict["overall_pass"] is True


def test_fails_when_not_enough_valid_repetitions():
    reps = [_rep(), _rep(), _rep(valid=False)]
    verdict = judge_candidate(reps)
    assert verdict["overall_pass"] is False
    assert verdict["checks"]["enough_valid_repetitions"] is False
    assert verdict["num_valid"] == 2
    assert verdict["num_invalid"] == 1


def test_invalid_reps_excluded_but_dont_block_pass_if_3_valid_remain():
    # 무효 반복 1개 + 유효하고 기준 통과하는 3개 = 종합 PASS여야 함(무효는
    # 그냥 제외되지, 그 자체로 실패 사유가 아니다).
    reps = [_rep(valid=False), _rep(), _rep(), _rep()]
    verdict = judge_candidate(reps)
    assert verdict["num_valid"] == 3
    assert verdict["num_invalid"] == 1
    assert verdict["overall_pass"] is True


def test_fails_when_any_run_not_100pct_success():
    reps = [_rep(), _rep(success_100=False), _rep()]
    verdict = judge_candidate(reps)
    assert verdict["overall_pass"] is False
    assert verdict["checks"]["all_runs_100pct_success"] is False


def test_fails_when_any_run_drain_does_not_recover():
    reps = [_rep(), _rep(drain_violates=True), _rep()]
    verdict = judge_candidate(reps)
    assert verdict["overall_pass"] is False
    assert verdict["checks"]["all_runs_drain_recovers"] is False


def test_fails_when_any_run_node_pod_not_clean():
    reps = [_rep(), _rep(node_clean=False), _rep()]
    verdict = judge_candidate(reps)
    assert verdict["overall_pass"] is False
    assert verdict["checks"]["all_runs_node_pod_clean"] is False
