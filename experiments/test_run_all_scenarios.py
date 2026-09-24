"""§98 section 11 - run_all_scenarios.py 오프라인 테스트. 실 클러스터/
subprocess 없이 fake Hooks만 주입해 매트릭스 생성과 순차 실행 로직을
검증한다. 전체가 KUBECONFIG 없이 통과해야 한다(다른 experiments/ 테스트와
동일한 관례)."""
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import run_all_scenarios as ras


def _make_hooks(call_log, run_trial_fn=None, preflight_ok=True, cleanup_ok=True,
                 git_drift_ok=True, verify_hash_fn=None):
    def run_trial(trial):
        call_log.append(("run_trial", trial["run_id"]))
        if run_trial_fn:
            return run_trial_fn(trial)
        return {"status": "completed", "result_path": f"fake/{trial['run_id']}.json", "result_hash": "abc"}

    def preflight(trial, state):
        call_log.append(("preflight", trial["run_id"]))
        return {"ok": preflight_ok, "reason": None if preflight_ok else "fake preflight fail"}

    def postflight_cleanup_check(trial):
        call_log.append(("cleanup_check", trial["run_id"]))
        return {"ok": cleanup_ok, "reason": None if cleanup_ok else "fake cleanup fail"}

    def apply_profile(profile):
        call_log.append(("apply_profile", profile))

    def restore_profile(profile):
        call_log.append(("restore_profile", profile))

    def check_git_drift():
        call_log.append(("check_git_drift",))
        return {"ok": git_drift_ok, "reason": None if git_drift_ok else "fake drift outside audit-log/"}

    def verify_result_hash(entry):
        call_log.append(("verify_result_hash", entry["run_id"]))
        return verify_hash_fn(entry) if verify_hash_fn else True

    return ras.Hooks(run_trial=run_trial, preflight=preflight,
                      postflight_cleanup_check=postflight_cleanup_check,
                      apply_profile=apply_profile, restore_profile=restore_profile,
                      check_git_drift=check_git_drift, verify_result_hash=verify_result_hash)


def _fresh_state(trials, plan_id="TESTPLAN"):
    return ras.build_initial_state(trials, plan_id, "fixed-v1")


# ---- build_matrix ----

def test_build_matrix_exactly_50_trials():
    trials = ras.build_matrix("P")
    assert len(trials) == 50


def test_build_matrix_five_per_core_cell():
    trials = ras.build_matrix("P")
    core = [t for t in trials if t.analysis_group == ras.CORE_GROUP]
    assert len(core) == 45
    for scenario in ras.CORE_SCENARIOS:
        for arm in ras.ARMS:
            count = sum(1 for t in core if t.scenario == scenario and t.arm == arm)
            assert count == 5, f"{scenario}/{arm} expected 5, got {count}"


def test_build_matrix_exactly_five_memory_native_zero_others():
    trials = ras.build_matrix("P")
    aux = [t for t in trials if t.analysis_group == ras.AUX_GROUP]
    assert len(aux) == 5
    assert all(t.scenario == ras.AUX_SCENARIO and t.arm == "native" for t in aux)
    non_native_aux = [t for t in trials if t.scenario == ras.AUX_SCENARIO and t.arm != "native"]
    assert non_native_aux == []


def test_build_matrix_fixed_execution_order():
    trials = ras.build_matrix("P")
    scenario_blocks = [t.scenario for t in trials]
    assert scenario_blocks == (["load_ramp"] * 15 + ["pod_kill"] * 15 +
                                ["network_degrade"] * 15 + [ras.AUX_SCENARIO] * 5)
    for scenario in ras.CORE_SCENARIOS:
        block = [t for t in trials if t.scenario == scenario]
        for rep in range(1, 6):
            rep_arms = tuple(t.arm for t in block if t.repetition == rep)
            assert rep_arms == ras.ARM_ORDER_BY_REP[rep]


def test_build_matrix_no_duplicate_run_ids():
    trials = ras.build_matrix("P")
    run_ids = [t.run_id for t in trials]
    assert len(run_ids) == len(set(run_ids))


def test_build_matrix_deterministic_for_same_plan_id():
    a = ras.build_matrix("SAME")
    b = ras.build_matrix("SAME")
    assert [t.run_id for t in a] == [t.run_id for t in b]


def test_arm_order_satisfies_contract_section7_balance_property():
    """experiment-contract.md §7(2026-09-20 동결) - 5개 묶음에 걸쳐 각 arm이
    각 위치(1·2·3번째)에 오는 횟수의 최댓값-최솟값이 1 이하여야 한다.
    §98에서 사용자가 직접 지정한 고정 순서(ARM_ORDER_BY_REP)가 이 조건을
    실제로 만족하는지 검증한다(눈으로 확인한 것을 코드로도 고정)."""
    for arm in ras.ARMS:
        position_counts = [0, 0, 0]
        for rep in range(1, 6):
            position_counts[ras.ARM_ORDER_BY_REP[rep].index(arm)] += 1
        assert max(position_counts) - min(position_counts) <= 1, (arm, position_counts)


# ---- run_sequence: happy path / ordering ----

def test_run_sequence_strictly_sequential(tmp_path):
    trials = ras.build_matrix("P")[:4]
    state = _fresh_state(trials)
    call_log = []
    ras.run_sequence(trials, state, _make_hooks(call_log), tmp_path / "state.json")

    run_trial_calls = [c[1] for c in call_log if c[0] == "run_trial"]
    assert run_trial_calls == [t.run_id for t in trials]
    for t in trials:
        pre_i = call_log.index(("preflight", t.run_id))
        run_i = call_log.index(("run_trial", t.run_id))
        clean_i = call_log.index(("cleanup_check", t.run_id))
        assert pre_i < run_i < clean_i


def test_run_sequence_all_completed(tmp_path):
    trials = ras.build_matrix("P")[:3]
    state = _fresh_state(trials)
    ras.run_sequence(trials, state, _make_hooks([]), tmp_path / "state.json")
    assert all(e["status"] == "completed" for e in state["trials"].values())
    assert all(e["cleanup_status"] == "ok" for e in state["trials"].values())


# ---- resume semantics ----

def test_run_sequence_skips_completed_on_resume(tmp_path):
    trials = ras.build_matrix("P")[:3]
    state = _fresh_state(trials)
    state["trials"][trials[0].run_id]["status"] = "completed"
    state["trials"][trials[0].run_id]["cleanup_status"] = "ok"
    call_log = []
    ras.run_sequence(trials, state, _make_hooks(call_log), tmp_path / "state.json")
    assert all(c[1] != trials[0].run_id for c in call_log if c[0] == "run_trial")
    assert trials[1].run_id in [c[1] for c in call_log if c[0] == "run_trial"]


def test_interrupted_running_becomes_needs_attention_not_rerun(tmp_path):
    trials = ras.build_matrix("P")[:2]
    state = _fresh_state(trials)
    state["trials"][trials[0].run_id]["status"] = "running"
    call_log = []
    with pytest.raises(ras.SequenceAborted, match="needs_attention"):
        ras.run_sequence(trials, state, _make_hooks(call_log), tmp_path / "state.json")
    assert state["trials"][trials[0].run_id]["status"] == "needs_attention"
    assert call_log == []  # 자동 재실행 금지 - run_trial이 호출되면 안 됨


@pytest.mark.parametrize("stuck_status", ["invalid", "failed", "needs_attention"])
def test_terminal_bad_status_not_auto_skipped_on_resume(tmp_path, stuck_status):
    trials = ras.build_matrix("P")[:2]
    state = _fresh_state(trials)
    state["trials"][trials[0].run_id]["status"] = stuck_status
    call_log = []
    with pytest.raises(ras.SequenceAborted, match=stuck_status):
        ras.run_sequence(trials, state, _make_hooks(call_log), tmp_path / "state.json")
    assert call_log == []


# ---- fail-closed abort conditions ----

def test_invalid_trial_result_aborts_sequence(tmp_path):
    trials = ras.build_matrix("P")[:3]
    state = _fresh_state(trials)

    def run_trial_fn(trial):
        if trial["run_id"] == trials[1].run_id:
            return {"status": "invalid", "failure_reason": "insufficient_headroom_with_preview"}
        return {"status": "completed", "result_path": "x", "result_hash": "h"}

    call_log = []
    with pytest.raises(ras.SequenceAborted):
        ras.run_sequence(trials, state, _make_hooks(call_log, run_trial_fn=run_trial_fn),
                          tmp_path / "state.json")
    assert state["trials"][trials[1].run_id]["status"] == "invalid"
    # 세 번째 trial은 절대 시작되면 안 됨(즉시 전체 중단)
    assert trials[2].run_id not in [c[1] for c in call_log if len(c) > 1]


def test_port_forward_or_detector_crash_reported_as_failed_aborts(tmp_path):
    trials = ras.build_matrix("P")[:2]
    state = _fresh_state(trials)

    def run_trial_fn(trial):
        return {"status": "failed", "failure_reason": "port-forward 연결 끊김(감지기 프로세스 비정상 종료)"}

    call_log = []
    with pytest.raises(ras.SequenceAborted):
        ras.run_sequence(trials, state, _make_hooks(call_log, run_trial_fn=run_trial_fn),
                          tmp_path / "state.json")
    assert state["trials"][trials[0].run_id]["status"] == "failed"


def test_cleanup_failure_aborts_sequence(tmp_path):
    trials = ras.build_matrix("P")[:2]
    state = _fresh_state(trials)
    call_log = []
    with pytest.raises(ras.SequenceAborted, match="cleanup"):
        ras.run_sequence(trials, state, _make_hooks(call_log, cleanup_ok=False),
                          tmp_path / "state.json")
    assert state["trials"][trials[0].run_id]["status"] == "completed"
    assert state["trials"][trials[0].run_id]["cleanup_status"] == "failed"


# ---- §109(2026-09-24): postflight는 completed/invalid/failed/러너 예외 네 종료
# 경로 모두에서 시도돼야 하고, 어느 경로든 다음 trial로 넘어가면 안 된다 ----

def test_postflight_attempted_after_completed_outcome_records_cleanup(tmp_path):
    trials = ras.build_matrix("P")[:1]
    state = _fresh_state(trials)
    call_log = []
    ras.run_sequence(trials, state, _make_hooks(call_log), tmp_path / "state.json")
    assert ("cleanup_check", trials[0].run_id) in call_log
    assert state["trials"][trials[0].run_id]["cleanup_status"] == "ok"
    print("OK - 정상 완료 경로도 postflight를 시도하고 결과를 state에 기록함(기존과 동일하게 유지)")


def test_postflight_attempted_after_invalid_outcome_and_still_aborts(tmp_path):
    """run_trial()이 반환한 status='invalid'(예: run_once.py의 TrialInvalid
    경로 - preflight 실패로 인한 invalid와는 다름)도 postflight를 시도해야
    한다 - §108까지는 여기서 postflight 호출 자체를 건너뛰었다."""
    trials = ras.build_matrix("P")[:2]
    state = _fresh_state(trials)

    def run_trial_fn(trial):
        return {"status": "invalid", "failure_reason": "fail-closed: metric stale - cache(...)"}

    call_log = []
    with pytest.raises(ras.SequenceAborted, match="invalid"):
        ras.run_sequence(trials, state, _make_hooks(call_log, run_trial_fn=run_trial_fn),
                          tmp_path / "state.json")
    assert ("cleanup_check", trials[0].run_id) in call_log, "invalid 종료에서도 postflight가 시도돼야 함"
    assert state["trials"][trials[0].run_id]["status"] == "invalid"
    assert state["trials"][trials[0].run_id]["failure_reason"] == "fail-closed: metric stale - cache(...)"
    assert state["trials"][trials[0].run_id]["cleanup_status"] == "ok"
    assert trials[1].run_id not in [c[1] for c in call_log if c[0] == "run_trial"], "다음 trial로 넘어가면 안 됨"
    print("OK - run_trial()의 invalid 종료도 postflight를 시도하고 cleanup 결과를 기록, 다음 trial은 시작 안 함")


def test_postflight_attempted_after_failed_outcome_and_still_aborts(tmp_path):
    trials = ras.build_matrix("P")[:2]
    state = _fresh_state(trials)

    def run_trial_fn(trial):
        return {"status": "failed", "failure_reason": "HarnessCorrupted: detector 크래시"}

    call_log = []
    with pytest.raises(ras.SequenceAborted, match="failed"):
        ras.run_sequence(trials, state, _make_hooks(call_log, run_trial_fn=run_trial_fn),
                          tmp_path / "state.json")
    assert ("cleanup_check", trials[0].run_id) in call_log, "failed 종료에서도 postflight가 시도돼야 함"
    assert state["trials"][trials[0].run_id]["status"] == "failed"
    assert state["trials"][trials[0].run_id]["failure_reason"] == "HarnessCorrupted: detector 크래시"
    assert state["trials"][trials[0].run_id]["cleanup_status"] == "ok"
    assert trials[1].run_id not in [c[1] for c in call_log if c[0] == "run_trial"], "다음 trial로 넘어가면 안 됨"
    print("OK - run_trial()의 failed 종료(예: 크래시)도 postflight를 시도하고 cleanup 결과를 기록, 다음 trial은 시작 안 함")


def test_postflight_attempted_after_runner_exception_and_still_aborts(tmp_path):
    """run_trial()이 dict를 반환하지 않고 예외 자체를 던지는 네 번째
    종료 경로(예: real_run_trial()의 subprocess.run()이 FileNotFoundError
    등) - §108까지는 이 예외가 run_sequence() 밖으로 그대로 새 나가
    postflight/state 기록을 전혀 안 거치고 오케스트레이터 자체가 죽었다."""
    trials = ras.build_matrix("P")[:2]
    state = _fresh_state(trials)

    def run_trial_fn(trial):
        raise FileNotFoundError("runner 스크립트를 찾을 수 없음")

    call_log = []
    with pytest.raises(ras.SequenceAborted, match="run_trial 예외") as exc_info:
        ras.run_sequence(trials, state, _make_hooks(call_log, run_trial_fn=run_trial_fn),
                          tmp_path / "state.json")
    assert "FileNotFoundError" in str(exc_info.value)
    assert ("cleanup_check", trials[0].run_id) in call_log, "러너 예외에서도 postflight가 시도돼야 함"
    assert state["trials"][trials[0].run_id]["status"] == "failed"
    assert "runner 스크립트를 찾을 수 없음" in state["trials"][trials[0].run_id]["failure_reason"]
    assert state["trials"][trials[0].run_id]["cleanup_status"] == "ok"
    assert trials[1].run_id not in [c[1] for c in call_log if c[0] == "run_trial"], "다음 trial로 넘어가면 안 됨"
    print("OK - run_trial()이 예외 자체를 던져도(네 번째 종료 경로) failed로 흡수돼 postflight가 시도되고 "
          "SequenceAborted로 승격됨(과거엔 오케스트레이터 프로세스 자체가 죽었음), 다음 trial은 시작 안 함")


def test_both_original_and_cleanup_failure_reasons_shown_together(tmp_path):
    """원본 trial 실패 사유와 postflight cleanup 실패 사유가 둘 다 나면
    둘 다 드러나야 한다(어느 한쪽도 조용히 덮이면 안 됨)."""
    trials = ras.build_matrix("P")[:1]
    state = _fresh_state(trials)

    def run_trial_fn(trial):
        return {"status": "failed", "failure_reason": "원본 사유: HarnessCorrupted"}

    call_log = []
    with pytest.raises(ras.SequenceAborted) as exc_info:
        ras.run_sequence(trials, state, _make_hooks(call_log, run_trial_fn=run_trial_fn, cleanup_ok=False),
                          tmp_path / "state.json")
    msg = str(exc_info.value)
    assert "원본 사유: HarnessCorrupted" in msg
    assert "fake cleanup fail" in msg  # _make_hooks()의 cleanup_ok=False 기본 사유 문구
    entry = state["trials"][trials[0].run_id]
    assert entry["failure_reason"] == "원본 사유: HarnessCorrupted", "원본 실패 사유는 그대로 보존돼야 함(cleanup 사유로 덮이면 안 됨)"
    assert entry["cleanup_status"] == "failed"
    assert entry["cleanup_reason"] == "fake cleanup fail"
    print("OK - 원본 trial 실패 사유와 postflight cleanup 실패 사유가 둘 다 SequenceAborted 메시지와 state에 남음(어느 쪽도 안 덮임)")


def test_postflight_exception_itself_is_absorbed_as_cleanup_failure(tmp_path):
    """postflight_cleanup_check() 자신이 예외를 던져도(예상 밖 K8s API
    오류 등) 삼켜지지 않고 cleanup 실패로 기록돼야 한다."""
    trials = ras.build_matrix("P")[:1]
    state = _fresh_state(trials)

    def postflight_raises(trial):
        raise RuntimeError("예상 밖 K8s API 오류")

    hooks = _make_hooks([])
    hooks = ras.Hooks(run_trial=hooks.run_trial, preflight=hooks.preflight,
                       postflight_cleanup_check=postflight_raises,
                       apply_profile=hooks.apply_profile, restore_profile=hooks.restore_profile,
                       check_git_drift=hooks.check_git_drift, verify_result_hash=hooks.verify_result_hash)
    with pytest.raises(ras.SequenceAborted, match="예상 밖 K8s API 오류"):
        ras.run_sequence(trials, state, hooks, tmp_path / "state.json")
    entry = state["trials"][trials[0].run_id]
    assert entry["cleanup_status"] == "failed"
    assert "postflight_cleanup_check 예외" in entry["cleanup_reason"]
    print("OK - postflight_cleanup_check() 자신이 예외를 던져도 삼켜지지 않고 cleanup 실패로 state에 기록됨")


# ---------------------------------------------------------------------------
# §111(2026-09-24, load_ramp-fixed_threshold-01-mainexp-v2 사고 계기) -
# status=completed + cleanup_status=failed는 명시적 adjudication
# (verdict=resolved_false_positive) 없이는 자동으로 건너뛰면 안 된다.
# 원본 실패 기록은 절대 덮어쓰지 않는다.
# ---------------------------------------------------------------------------

def test_resume_does_not_auto_skip_completed_with_failed_cleanup_without_adjudication(tmp_path):
    trials = ras.build_matrix("P")[:2]
    state = _fresh_state(trials)
    state["trials"][trials[0].run_id]["status"] = "completed"
    state["trials"][trials[0].run_id]["cleanup_status"] = "failed"
    state["trials"][trials[0].run_id]["cleanup_reason"] = "[rollout_healthy_single_revision] Rollout phase='Degraded'"
    state["trials"][trials[0].run_id]["result_hash"] = "abc"
    call_log = []
    with pytest.raises(ras.SequenceAborted, match="adjudication"):
        ras.run_sequence(trials, state, _make_hooks(call_log, verify_hash_fn=lambda e: True),
                          tmp_path / "state.json")
    assert trials[1].run_id not in [c[1] for c in call_log if c[0] == "run_trial"], "다음 trial로 넘어가면 안 됨"
    # 원본 실패 기록은 손대지 않아야 함.
    assert state["trials"][trials[0].run_id]["cleanup_status"] == "failed"
    assert state["trials"][trials[0].run_id]["cleanup_reason"] == "[rollout_healthy_single_revision] Rollout phase='Degraded'"
    print("OK - completed+cleanup_status=failed는 명시적 adjudication 없이는 자동으로 건너뛰지 않고 중단, 원본 기록 보존")


def test_resume_skips_with_explicit_resolved_false_positive_adjudication(tmp_path):
    trials = ras.build_matrix("P")[:2]
    state = _fresh_state(trials)
    state["trials"][trials[0].run_id]["status"] = "completed"
    state["trials"][trials[0].run_id]["cleanup_status"] = "failed"
    state["trials"][trials[0].run_id]["cleanup_reason"] = "[rollout_healthy_single_revision] Rollout phase='Degraded'"
    state["trials"][trials[0].run_id]["result_hash"] = "abc"
    ras.adjudicate_cleanup_failure(state, trials[0].run_id, "resolved_false_positive",
                                    "재검증 결과 Argo Rollouts의 정상적인 abort-후 잔류 상태였음(§110)",
                                    evidence={"pauseConditions": None})
    call_log = []
    ras.run_sequence(trials, state, _make_hooks(call_log, verify_hash_fn=lambda e: True), tmp_path / "state.json")
    assert trials[1].run_id in [c[1] for c in call_log if c[0] == "run_trial"], "adjudication 후에는 다음 trial로 진행해야 함"
    # 원본 실패 기록은 여전히 그대로 - adjudication이 덮어쓰지 않음.
    assert state["trials"][trials[0].run_id]["cleanup_status"] == "failed"
    assert state["trials"][trials[0].run_id]["cleanup_adjudication"]["verdict"] == "resolved_false_positive"
    print("OK - 명시적 adjudication(verdict=resolved_false_positive)이 있으면 다음 trial로 진행, "
          "원본 cleanup_status/cleanup_reason은 여전히 'failed'로 보존됨(소급 수정 없음)")


def test_resume_still_blocks_with_confirmed_problem_adjudication(tmp_path):
    """verdict=confirmed_problem은 판정 자체는 기록되지만 여전히 자동으로
    건너뛰지 않는다 - 진짜 문제로 확정됐다는 뜻이라 재실행 여부는 별도 결정."""
    trials = ras.build_matrix("P")[:2]
    state = _fresh_state(trials)
    state["trials"][trials[0].run_id]["status"] = "completed"
    state["trials"][trials[0].run_id]["cleanup_status"] = "failed"
    state["trials"][trials[0].run_id]["cleanup_reason"] = "[all_nodes_healthy] Node sj-worker NotReady"
    state["trials"][trials[0].run_id]["result_hash"] = "abc"
    ras.adjudicate_cleanup_failure(state, trials[0].run_id, "confirmed_problem",
                                    "재검증 결과 실제로 Node가 NotReady 상태였음이 확인됨")
    call_log = []
    with pytest.raises(ras.SequenceAborted, match="adjudication"):
        ras.run_sequence(trials, state, _make_hooks(call_log, verify_hash_fn=lambda e: True),
                          tmp_path / "state.json")
    assert trials[1].run_id not in [c[1] for c in call_log if c[0] == "run_trial"]
    print("OK - verdict=confirmed_problem은 판정은 기록되지만 여전히 자동으로 건너뛰지 않음(재실행은 별도 결정)")


def test_resume_blocks_on_hash_mismatch_even_when_cleanup_status_ok(tmp_path):
    """§111 - completed+cleanup_status=ok라도 결과 파일 hash가 저장값과
    다르면(사후 손상·변조 가능성) 자동으로 건너뛰면 안 된다."""
    trials = ras.build_matrix("P")[:2]
    state = _fresh_state(trials)
    state["trials"][trials[0].run_id]["status"] = "completed"
    state["trials"][trials[0].run_id]["cleanup_status"] = "ok"
    state["trials"][trials[0].run_id]["result_hash"] = "original-hash"
    call_log = []
    with pytest.raises(ras.SequenceAborted, match="hash"):
        ras.run_sequence(trials, state, _make_hooks(call_log, verify_hash_fn=lambda e: False),
                          tmp_path / "state.json")
    assert trials[1].run_id not in [c[1] for c in call_log if c[0] == "run_trial"]
    print("OK - completed+cleanup_status=ok라도 결과 파일 hash가 저장값과 다르면 자동으로 건너뛰지 않고 중단")


def test_adjudicate_cleanup_failure_rejects_non_completed_status():
    trials = ras.build_matrix("P")[:1]
    state = _fresh_state(trials)
    state["trials"][trials[0].run_id]["status"] = "failed"
    state["trials"][trials[0].run_id]["cleanup_status"] = "failed"
    with pytest.raises(ValueError, match="completed"):
        ras.adjudicate_cleanup_failure(state, trials[0].run_id, "resolved_false_positive", "사유")
    print("OK - status가 completed가 아니면 adjudication 거부")


def test_adjudicate_cleanup_failure_rejects_already_ok_cleanup():
    trials = ras.build_matrix("P")[:1]
    state = _fresh_state(trials)
    state["trials"][trials[0].run_id]["status"] = "completed"
    state["trials"][trials[0].run_id]["cleanup_status"] = "ok"
    with pytest.raises(ValueError, match="불필요"):
        ras.adjudicate_cleanup_failure(state, trials[0].run_id, "resolved_false_positive", "사유")
    print("OK - cleanup_status가 이미 ok면 adjudication 자체가 불필요하다고 거부")


def test_adjudicate_cleanup_failure_rejects_unknown_verdict():
    trials = ras.build_matrix("P")[:1]
    state = _fresh_state(trials)
    state["trials"][trials[0].run_id]["status"] = "completed"
    state["trials"][trials[0].run_id]["cleanup_status"] = "failed"
    with pytest.raises(ValueError, match="verdict"):
        ras.adjudicate_cleanup_failure(state, trials[0].run_id, "looks_fine_i_guess", "사유")
    print("OK - 알 수 없는 verdict는 거부(resolved_false_positive|confirmed_problem만 허용)")


def test_adjudicate_cleanup_failure_never_touches_original_cleanup_fields():
    trials = ras.build_matrix("P")[:1]
    state = _fresh_state(trials)
    state["trials"][trials[0].run_id]["status"] = "completed"
    state["trials"][trials[0].run_id]["cleanup_status"] = "failed"
    state["trials"][trials[0].run_id]["cleanup_reason"] = "원본 실패 사유 그대로"
    ras.adjudicate_cleanup_failure(state, trials[0].run_id, "resolved_false_positive", "판정 사유",
                                    evidence={"k": "v"})
    entry = state["trials"][trials[0].run_id]
    assert entry["cleanup_status"] == "failed", "원본 cleanup_status를 절대 덮어쓰면 안 됨"
    assert entry["cleanup_reason"] == "원본 실패 사유 그대로"
    assert entry["cleanup_adjudication"] == {
        "verdict": "resolved_false_positive", "reason": "판정 사유", "evidence": {"k": "v"},
        "adjudicated_at_utc": entry["cleanup_adjudication"]["adjudicated_at_utc"],
    }
    print("OK - adjudication은 원본 cleanup_status/cleanup_reason을 절대 안 건드리고 별도 필드에만 기록")


def test_main_cli_adjudicate_cleanup_records_verdict_without_running_trial(tmp_path):
    trials = ras.build_matrix("P")[:1]
    state = _fresh_state(trials)
    state["trials"][trials[0].run_id]["status"] = "completed"
    state["trials"][trials[0].run_id]["cleanup_status"] = "failed"
    state["trials"][trials[0].run_id]["cleanup_reason"] = "[rollout_healthy_single_revision] Rollout phase='Degraded'"
    state_path = tmp_path / "state.json"
    ras.save_state_atomic(state_path, state)
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "run_all_scenarios.py"),
         "--adjudicate-cleanup", trials[0].run_id, "resolved_false_positive",
         "--adjudicate-reason", "테스트 - CLI 경로 확인",
         "--state-file", str(state_path)],
        capture_output=True, text=True, cwd=Path(__file__).parent)
    assert proc.returncode == 0, proc.stderr
    reloaded = ras.load_state(state_path)
    entry = reloaded["trials"][trials[0].run_id]
    assert entry["cleanup_adjudication"]["verdict"] == "resolved_false_positive"
    assert entry["cleanup_status"] == "failed", "CLI 경로도 원본 cleanup_status를 안 건드려야 함"
    print("OK - CLI --adjudicate-cleanup이 판정만 기록하고 종료(trial 실행 없음), 원본 cleanup_status 보존")


def test_preflight_failure_marks_invalid_and_aborts(tmp_path):
    trials = ras.build_matrix("P")[:2]
    state = _fresh_state(trials)
    call_log = []
    with pytest.raises(ras.SequenceAborted, match="preflight"):
        ras.run_sequence(trials, state, _make_hooks(call_log, preflight_ok=False),
                          tmp_path / "state.json")
    assert state["trials"][trials[0].run_id]["status"] == "invalid"


def test_git_drift_blocks_trial_start(tmp_path):
    trials = ras.build_matrix("P")[:2]
    state = _fresh_state(trials)
    call_log = []
    with pytest.raises(ras.SequenceAborted, match="drift"):
        ras.run_sequence(trials, state, _make_hooks(call_log, git_drift_ok=False),
                          tmp_path / "state.json")
    assert ("run_trial", trials[0].run_id) not in call_log


# ---- network_degrade profile lifecycle ----

def test_profile_applied_before_and_restored_after_network_degrade_block(tmp_path):
    trials = ras.build_matrix("P")  # 전체 매트릭스 - load_ramp, pod_kill, network_degrade, aux
    state = _fresh_state(trials)
    call_log = []
    ras.run_sequence(trials, state, _make_hooks(call_log), tmp_path / "state.json")

    profile_events = [c for c in call_log if c[0] in ("apply_profile", "restore_profile")]
    assert profile_events == [("apply_profile", "network_tolerant"), ("restore_profile", "default")]

    apply_i = call_log.index(("apply_profile", "network_tolerant"))
    restore_i = call_log.index(("restore_profile", "default"))
    first_network_degrade_run = min(i for i, c in enumerate(call_log)
                                     if c[0] == "run_trial" and c[1].startswith("network_degrade-"))
    last_network_degrade_run = max(i for i, c in enumerate(call_log)
                                    if c[0] == "run_trial" and c[1].startswith("network_degrade-"))
    assert apply_i < first_network_degrade_run
    assert restore_i > last_network_degrade_run


def test_exception_during_network_degrade_block_still_restores_profile(tmp_path):
    """§109부터 run_trial()이 던지는 예외는 run_sequence() 안에서
    "failed" 종료 경로로 흡수돼 SequenceAborted로 승격된다(과거엔 이
    RuntimeError가 그대로 밖으로 새 나갔다) - profile 복원은 그 승격과
    무관하게 여전히 finally에서 보장돼야 한다."""
    trials = ras.build_matrix("P")
    state = _fresh_state(trials)
    call_log = []

    def run_trial_fn(trial):
        if trial["scenario"] == "network_degrade":
            raise RuntimeError("시뮬레이션된 Ctrl+C/예외")
        return {"status": "completed", "result_path": "x", "result_hash": "h"}

    with pytest.raises(ras.SequenceAborted, match="run_trial 예외"):
        ras.run_sequence(trials, state, _make_hooks(call_log, run_trial_fn=run_trial_fn),
                          tmp_path / "state.json")

    assert ("restore_profile", "default") in call_log


# ---- dry-run ----

def test_dry_run_makes_zero_cluster_calls(tmp_path):
    trials = ras.build_matrix("P")  # network_degrade 블록 포함 - profile 전환도 스킵돼야 함
    state = _fresh_state(trials)
    call_log = []
    ras.run_sequence(trials, state, _make_hooks(call_log), tmp_path / "state.json", dry_run=True)

    assert [c for c in call_log if c[0] == "run_trial"] == []
    assert [c for c in call_log if c[0] in ("apply_profile", "restore_profile")] == []
    assert all(e["status"] == "planned" for e in state["trials"].values())


def test_plan_cli_prints_exactly_50_trials():
    proc = subprocess.run([sys.executable, str(Path(__file__).parent / "run_all_scenarios.py"), "--plan"],
                           capture_output=True, text=True, cwd=Path(__file__).parent)
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.rsplit("\n\n", 1)[0])
    assert len(payload) == 50


def test_plan_cli_with_scenario_filter_prints_exactly_15_load_ramp_trials():
    proc = subprocess.run([sys.executable, str(Path(__file__).parent / "run_all_scenarios.py"),
                            "--plan", "--scenario", "load_ramp"],
                           capture_output=True, text=True, cwd=Path(__file__).parent)
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.rsplit("\n\n", 1)[0])
    assert len(payload) == 15
    assert all(t["scenario"] == "load_ramp" for t in payload)
    assert all(t["analysis_group"] == "core_fault_comparison" for t in payload)


def test_scenario_filter_shares_state_leaving_other_scenarios_planned(tmp_path):
    trials = ras.build_matrix("P")
    state = _fresh_state(trials)
    load_ramp_trials = [t for t in trials if t.scenario == "load_ramp"]
    call_log = []
    ras.run_sequence(load_ramp_trials, state, _make_hooks(call_log), tmp_path / "state.json")

    assert all(state["trials"][t.run_id]["status"] == "completed" for t in load_ramp_trials)
    other_trials = [t for t in trials if t.scenario != "load_ramp"]
    assert all(state["trials"][t.run_id]["status"] == "planned" for t in other_trials)


# ---- --from-run-id / result-hash verification ----

def test_from_run_id_skips_prior_completed_with_matching_hash(tmp_path):
    trials = ras.build_matrix("P")[:3]
    state = _fresh_state(trials)
    for t in trials[:2]:
        state["trials"][t.run_id]["status"] = "completed"
        state["trials"][t.run_id]["result_hash"] = "matches"
        state["trials"][t.run_id]["cleanup_status"] = "ok"
    call_log = []
    ras.run_sequence(trials, state, _make_hooks(call_log), tmp_path / "state.json",
                      from_run_id=trials[2].run_id)
    assert [c[1] for c in call_log if c[0] == "run_trial"] == [trials[2].run_id]


def test_from_run_id_aborts_on_hash_mismatch(tmp_path):
    trials = ras.build_matrix("P")[:3]
    state = _fresh_state(trials)
    state["trials"][trials[0].run_id]["status"] = "completed"
    state["trials"][trials[0].run_id]["result_hash"] = "stale"
    state["trials"][trials[0].run_id]["cleanup_status"] = "ok"
    call_log = []
    with pytest.raises(ras.SequenceAborted, match="hash"):
        ras.run_sequence(trials, state, _make_hooks(call_log, verify_hash_fn=lambda e: False),
                          tmp_path / "state.json", from_run_id=trials[2].run_id)
    assert call_log == [("verify_result_hash", trials[0].run_id)]


def test_from_run_id_aborts_if_prior_not_completed(tmp_path):
    trials = ras.build_matrix("P")[:3]
    state = _fresh_state(trials)  # 전부 planned
    call_log = []
    with pytest.raises(ras.SequenceAborted, match="completed"):
        ras.run_sequence(trials, state, _make_hooks(call_log), tmp_path / "state.json",
                          from_run_id=trials[2].run_id)


# ---- §114(2026-09-24): --to-run-id - 지정한 run_id까지만(포함) 실행하고
# 정상 종료(중단 아님), 그 뒤는 planned로 그대로 남음 ----

def test_to_run_id_stops_after_inclusive_point_without_aborting(tmp_path):
    trials = ras.build_matrix("P")[:5]
    state = _fresh_state(trials)
    call_log = []
    result = ras.run_sequence(trials, state, _make_hooks(call_log), tmp_path / "state.json",
                               to_run_id=trials[2].run_id)
    run_trial_calls = [c[1] for c in call_log if c[0] == "run_trial"]
    assert run_trial_calls == [trials[0].run_id, trials[1].run_id, trials[2].run_id], (
        "정확히 to_run_id까지(포함) 3건만 실행돼야 함")
    assert state["trials"][trials[2].run_id]["status"] == "completed"
    assert state["trials"][trials[3].run_id]["status"] == "planned", "to_run_id 이후는 손대지 않아야 함"
    assert state["trials"][trials[4].run_id]["status"] == "planned"
    assert result is state, "정상 종료(SequenceAborted 아님) - 그냥 state를 반환해야 함"
    print("OK - to_run_id까지(포함)만 실행하고 정상 반환, 그 뒤 trial은 planned로 그대로 남음(중단 아님)")


def test_to_run_id_raises_if_not_in_matrix(tmp_path):
    trials = ras.build_matrix("P")[:3]
    state = _fresh_state(trials)
    call_log = []
    with pytest.raises(ras.SequenceAborted, match="to-run-id"):
        ras.run_sequence(trials, state, _make_hooks(call_log), tmp_path / "state.json",
                          to_run_id="존재하지-않는-run-id")
    assert call_log == [], "매트릭스에 없는 to_run_id면 아무 trial도 시작하면 안 됨"
    print("OK - 매트릭스에 없는 --to-run-id는 즉시 SequenceAborted, 아무 trial도 실행 안 됨")


def test_to_run_id_combined_with_from_run_id(tmp_path):
    """§114 - 둘 다 지정하면 [from_run_id, to_run_id] 구간(양쪽 inclusive)만
    실행한다 - from_run_id 이전 trial의 completed+hash 조건은 그대로 적용."""
    trials = ras.build_matrix("P")[:5]
    state = _fresh_state(trials)
    state["trials"][trials[0].run_id]["status"] = "completed"
    state["trials"][trials[0].run_id]["cleanup_status"] = "ok"
    state["trials"][trials[0].run_id]["result_hash"] = "h0"
    call_log = []
    ras.run_sequence(trials, state, _make_hooks(call_log, verify_hash_fn=lambda e: True),
                      tmp_path / "state.json", from_run_id=trials[1].run_id, to_run_id=trials[3].run_id)
    run_trial_calls = [c[1] for c in call_log if c[0] == "run_trial"]
    assert run_trial_calls == [trials[1].run_id, trials[2].run_id, trials[3].run_id]
    assert state["trials"][trials[4].run_id]["status"] == "planned"
    print("OK - --from-run-id와 --to-run-id를 함께 쓰면 그 구간만 실행(양쪽 inclusive)")


def test_preview_trial_range_matches_run_sequence_slicing():
    """§114 - --plan 미리보기(_preview_trial_range)가 run_sequence()의
    실제 슬라이싱과 동일한 결과를 내는지 직접 대조."""
    trials = ras.build_matrix("P")[:5]
    preview = ras._preview_trial_range(trials, to_run_id=trials[2].run_id)
    assert [t.run_id for t in preview] == [trials[0].run_id, trials[1].run_id, trials[2].run_id]

    preview2 = ras._preview_trial_range(trials, from_run_id=trials[1].run_id, to_run_id=trials[3].run_id)
    assert [t.run_id for t in preview2] == [trials[1].run_id, trials[2].run_id, trials[3].run_id]
    print("OK - _preview_trial_range()가 run_sequence()와 동일한 경계 규칙(양쪽 inclusive)으로 슬라이싱함")


def test_preview_trial_range_raises_on_unknown_to_run_id():
    trials = ras.build_matrix("P")[:3]
    with pytest.raises(ValueError, match="to-run-id"):
        ras._preview_trial_range(trials, to_run_id="존재하지-않는-run-id")
    print("OK - --plan 미리보기도 매트릭스에 없는 to_run_id를 거부(클러스터 호출 없이 즉시)")


def test_main_cli_plan_reflects_to_run_id_scope(tmp_path):
    """§114 - `--plan --to-run-id`가 실제로 이번 실행 범위(3건)만 출력하는지
    CLI 경로로 직접 확인(요청 원문 - "--plan/--dry-run으로... 이번 실행
    범위... 확인")."""
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "run_all_scenarios.py"),
         "--plan", "--scenario", "pod_kill", "--plan-id", "cli-to-run-test",
         "--to-run-id", "pod_kill-proposed-01-cli-to-run-test"],
        capture_output=True, text=True, cwd=Path(__file__).parent)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout[:proc.stdout.rindex("]") + 1])
    assert [t["run_id"] for t in data] == [
        "pod_kill-native-01-cli-to-run-test",
        "pod_kill-fixed_threshold-01-cli-to-run-test",
        "pod_kill-proposed-01-cli-to-run-test",
    ]
    assert "총 3 trial" in proc.stdout
    print("OK - CLI --plan --to-run-id가 정확히 첫 3건(rep1 native→fixed_threshold→proposed)만 미리보기로 보여줌")


# ---- atomic state persistence ----

def test_save_and_load_state_atomic_roundtrip(tmp_path):
    trials = ras.build_matrix("P")[:2]
    state = _fresh_state(trials)
    state_path = tmp_path / "state.json"
    ras.save_state_atomic(state_path, state)
    reloaded = ras.load_state(state_path)
    assert reloaded == state
    assert not state_path.with_name(state_path.name + ".tmp").exists()


def test_load_state_missing_file_returns_none(tmp_path):
    assert ras.load_state(tmp_path / "does_not_exist.json") is None


def test_real_check_git_drift_tolerates_audit_log_only(monkeypatch):
    def fake_run(cmd, cwd, capture_output, text, check):
        class R:
            stdout = " M audit-log/adhoc.jsonl\n"
        return R()
    monkeypatch.setattr(ras.subprocess, "run", fake_run)
    result = ras.real_check_git_drift()
    assert result["ok"] is True


def test_real_check_git_drift_blocks_on_code_change(monkeypatch):
    def fake_run(cmd, cwd, capture_output, text, check):
        class R:
            stdout = " M experiments/arm_controller.py\n"
        return R()
    monkeypatch.setattr(ras.subprocess, "run", fake_run)
    result = ras.real_check_git_drift()
    assert result["ok"] is False


# ---- §103: 기술적 invalid 슬롯 대체 연결 (원본 보존, 자동 건너뛰기/재시도 금지, 순서 불변) ----

def _state_with_failed_pod_kill_proposed():
    trials = ras.build_matrix("mainexp-v1")
    pod_kill = [t for t in trials if t.scenario == "pod_kill"]
    state = _fresh_state(pod_kill, plan_id="mainexp-v1")
    state["trials"]["pod_kill-native-01-mainexp-v1"]["status"] = "completed"
    state["trials"]["pod_kill-native-01-mainexp-v1"]["result_hash"] = "native-hash"
    state["trials"]["pod_kill-native-01-mainexp-v1"]["cleanup_status"] = "ok"
    state["trials"]["pod_kill-fixed_threshold-01-mainexp-v1"]["status"] = "completed"
    state["trials"]["pod_kill-fixed_threshold-01-mainexp-v1"]["result_hash"] = "ft-hash"
    state["trials"]["pod_kill-fixed_threshold-01-mainexp-v1"]["cleanup_status"] = "ok"
    state["trials"]["pod_kill-proposed-01-mainexp-v1"]["status"] = "failed"
    state["trials"]["pod_kill-proposed-01-mainexp-v1"]["failure_reason"] = "실행기 종료 코드 1"
    return state, pod_kill


def test_link_replacement_rejects_non_technical_invalid_status():
    """§103 - status=invalid(outcome=invalid_run, 유효한 실험 결과)나
    completed/planned는 대체 연결 대상이 아니다 - 'failed'(하니스 자체가
    깨진 기술적 invalid)만 허용한다. 잘못된 상태를 대체하려 하면 거부하고
    원본은 손대지 않는다."""
    state, _ = _state_with_failed_pod_kill_proposed()
    state["trials"]["pod_kill-native-02-mainexp-v1"]["status"] = "invalid"
    before = dict(state["trials"]["pod_kill-native-02-mainexp-v1"])
    with pytest.raises(ValueError, match="기술적 invalid"):
        ras.link_technical_invalid_replacement(state, "pod_kill-native-02-mainexp-v1",
                                                "pod_kill-native-02-retry-mainexp-v1", "테스트 사유")
    assert state["trials"]["pod_kill-native-02-mainexp-v1"] == before
    assert "replacements" not in state or "pod_kill-native-02-mainexp-v1" not in state.get("replacements", {})


def test_link_replacement_never_modifies_original_entry():
    state, _ = _state_with_failed_pod_kill_proposed()
    original_before = dict(state["trials"]["pod_kill-proposed-01-mainexp-v1"])
    ras.link_technical_invalid_replacement(
        state, "pod_kill-proposed-01-mainexp-v1", "pod_kill-proposed-01-retry1-mainexp-v1",
        "detector crash root-cause 불명, cleanup timeout 수정 후 재시도")
    assert state["trials"]["pod_kill-proposed-01-mainexp-v1"] == original_before, \
        "원본 슬롯은 link 이후에도 절대 바뀌면 안 됨(원본 덮어쓰기 금지)"
    print("OK - 대체 연결 후에도 원본 trial 항목은 완전히 그대로")


def test_link_replacement_creates_replacement_slot_with_same_position_fields():
    state, _ = _state_with_failed_pod_kill_proposed()
    original = state["trials"]["pod_kill-proposed-01-mainexp-v1"]
    ras.link_technical_invalid_replacement(
        state, "pod_kill-proposed-01-mainexp-v1", "pod_kill-proposed-01-retry1-mainexp-v1", "재시도")
    replacement = state["trials"]["pod_kill-proposed-01-retry1-mainexp-v1"]
    assert replacement["scenario"] == original["scenario"]
    assert replacement["arm"] == original["arm"]
    assert replacement["repetition"] == original["repetition"]
    assert replacement["analysis_group"] == original["analysis_group"]
    assert replacement["sequence_index"] == original["sequence_index"]
    assert replacement["status"] == "planned"
    assert replacement["result_hash"] is None
    link = state["replacements"]["pod_kill-proposed-01-mainexp-v1"]
    assert link["replacement_run_id"] == "pod_kill-proposed-01-retry1-mainexp-v1"
    assert link["reason"] == "재시도"
    assert link["original_result_hash"] == original.get("result_hash")
    assert link["replacement_result_hash"] is None
    print("OK - 대체 슬롯이 원본과 동일한 위치 정보로 생성되고 연결 기록이 남음")


def test_link_replacement_rejects_duplicate_link_no_arbitrary_retry():
    """§103 - 같은 원본을 두 번째로 다시 연결하려 하면 거부한다(임의 반복
    재시도 금지) - 첫 연결이 그대로 유지돼야 한다."""
    state, _ = _state_with_failed_pod_kill_proposed()
    ras.link_technical_invalid_replacement(
        state, "pod_kill-proposed-01-mainexp-v1", "pod_kill-proposed-01-retry1-mainexp-v1", "1차 재시도")
    with pytest.raises(ValueError, match="이미.*대체 연결됨"):
        ras.link_technical_invalid_replacement(
            state, "pod_kill-proposed-01-mainexp-v1", "pod_kill-proposed-01-retry2-mainexp-v1", "2차 재시도")
    assert state["replacements"]["pod_kill-proposed-01-mainexp-v1"]["replacement_run_id"] == \
        "pod_kill-proposed-01-retry1-mainexp-v1", "두 번째 연결 시도가 첫 연결을 덮어쓰면 안 됨"
    assert "pod_kill-proposed-01-retry2-mainexp-v1" not in state["trials"]
    print("OK - 같은 원본을 두 번 연결할 수 없음(첫 연결 유지, 임의 재시도 금지)")


def test_link_replacement_rejects_non_unique_replacement_run_id():
    state, _ = _state_with_failed_pod_kill_proposed()
    with pytest.raises(ValueError, match="고유"):
        ras.link_technical_invalid_replacement(
            state, "pod_kill-proposed-01-mainexp-v1", "pod_kill-native-01-mainexp-v1", "잘못된 사유")
    print("OK - 대체 run_id가 매트릭스에 이미 있으면 거부(고유해야 함)")


def test_link_replacement_rejects_missing_reason_via_cli(tmp_path):
    """--link-replacement는 --link-reason 없이 쓸 수 없다(CLI 레벨 강제)."""
    state, _ = _state_with_failed_pod_kill_proposed()
    state_path = tmp_path / "state.json"
    ras.save_state_atomic(state_path, state)
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "run_all_scenarios.py"),
         "--link-replacement", "pod_kill-proposed-01-mainexp-v1", "pod_kill-proposed-01-retry1-mainexp-v1",
         "--state-file", str(state_path)],
        capture_output=True, text=True, cwd=Path(__file__).parent)
    assert proc.returncode != 0
    assert "--link-reason" in proc.stderr
    reloaded = ras.load_state(state_path)
    assert "replacements" not in reloaded or not reloaded["replacements"]
    print("OK - --link-reason 없이는 CLI가 즉시 거부하고 state를 바꾸지 않음")


def test_apply_replacements_preserves_order_and_position():
    """§103 - 대체가 적용돼도 실행 순서 자체는 절대 안 바뀐다: 원본이 있던
    바로 그 자리에 대체가 들어가고, 그 앞뒤 trial(예: fixed_threshold-02)의
    위치는 그대로다."""
    state, pod_kill_trials = _state_with_failed_pod_kill_proposed()
    ras.link_technical_invalid_replacement(
        state, "pod_kill-proposed-01-mainexp-v1", "pod_kill-proposed-01-retry1-mainexp-v1", "재시도")
    result = ras.apply_replacements(pod_kill_trials, state["replacements"])
    result_run_ids = [t.run_id for t in result]

    original_run_ids = [t.run_id for t in pod_kill_trials]
    proposed_idx = original_run_ids.index("pod_kill-proposed-01-mainexp-v1")
    assert result_run_ids[proposed_idx] == "pod_kill-proposed-01-retry1-mainexp-v1", \
        "대체는 원본과 정확히 같은 위치(순서)에 들어가야 함"
    assert result_run_ids[proposed_idx + 1] == "pod_kill-fixed_threshold-02-mainexp-v1", \
        "대체 다음 trial(fixed_threshold-02)의 위치가 바뀌면 안 됨"
    # 나머지는 전부 원본 그대로(개수·순서 불변, 대체된 한 자리만 바뀜)
    expected = list(original_run_ids)
    expected[proposed_idx] = "pod_kill-proposed-01-retry1-mainexp-v1"
    assert result_run_ids == expected
    print("OK - 대체가 원본 위치를 정확히 대체하고 나머지 순서는 전혀 안 바뀜(fixed_threshold-02가 바로 다음)")


def test_apply_replacements_leaves_unlinked_trials_untouched():
    state, pod_kill_trials = _state_with_failed_pod_kill_proposed()
    result = ras.apply_replacements(pod_kill_trials, state.get("replacements", {}))
    assert [t.run_id for t in result] == [t.run_id for t in pod_kill_trials]
    print("OK - 연결된 대체가 없으면 apply_replacements가 아무것도 안 바꿈")


def test_run_sequence_does_not_auto_run_original_failed_slot_even_with_replacement_linked(tmp_path):
    """§103 - 대체가 연결돼 있어도 원본 run_id 자체는 run_sequence()에
    넘기는 실행 목록에 다시 등장하면 안 된다(apply_replacements가 이미
    바꿔치기했으므로) - 원본이 실수로 다시 실행 시도되면 안 됨을
    end-to-end로 확인."""
    state, pod_kill_trials = _state_with_failed_pod_kill_proposed()
    ras.link_technical_invalid_replacement(
        state, "pod_kill-proposed-01-mainexp-v1", "pod_kill-proposed-01-retry1-mainexp-v1", "재시도")
    trials_to_run = ras.apply_replacements(pod_kill_trials, state["replacements"])
    # native-01/fixed_threshold-01은 이미 completed로 표시해뒀으니 run_sequence가 건너뛰고,
    # 대체(retry1)만 새로 실행돼야 한다(원본 proposed-01은 목록 자체에 없음).
    call_log = []
    ras.run_sequence(trials_to_run, state, _make_hooks(call_log), tmp_path / "state.json")
    run_trial_calls = [c[1] for c in call_log if c[0] == "run_trial"]
    assert "pod_kill-proposed-01-mainexp-v1" not in run_trial_calls, "원본 failed 슬롯이 다시 실행되면 안 됨"
    assert "pod_kill-proposed-01-retry1-mainexp-v1" in run_trial_calls
    assert state["trials"]["pod_kill-proposed-01-mainexp-v1"]["status"] == "failed", "원본 status는 그대로 failed"
    print("OK - 대체 연결 후에도 원본은 절대 재실행되지 않고 대체만 실행됨")


def test_sync_replacement_results_updates_link_after_execution(tmp_path):
    state, pod_kill_trials = _state_with_failed_pod_kill_proposed()
    ras.link_technical_invalid_replacement(
        state, "pod_kill-proposed-01-mainexp-v1", "pod_kill-proposed-01-retry1-mainexp-v1", "재시도")
    trials_to_run = ras.apply_replacements(pod_kill_trials, state["replacements"])
    ras.run_sequence(trials_to_run, state, _make_hooks([]), tmp_path / "state.json")
    ras.sync_replacement_results(state)
    link = state["replacements"]["pod_kill-proposed-01-mainexp-v1"]
    assert link["replacement_status"] == "completed"
    assert link["replacement_result_hash"] == "abc"  # _make_hooks 기본 run_trial의 fake result_hash
    print("OK - 대체 trial 실행 후 연결 기록에 최신 status/result_hash가 반영됨")


# ---- §105: real_run_trial() 실패 시 결과 파일 보존 + 대체 링크 원본 hash 재검증 ----

def _fake_completed_process(returncode):
    proc = MagicMock()
    proc.returncode = returncode
    return proc


def test_real_run_trial_failure_with_existing_result_file_preserves_path_and_hash(tmp_path, monkeypatch):
    """§105(pod_kill-proposed-01-mainexp-v1 계기) - 실행기가 비정상 종료해도
    (예: HarnessCorrupted) run_once.py가 그 전에 이미 결과 JSON을 써뒀을 수
    있다 - 그 파일이 있으면 result_path/result_hash를 반드시 같이 보존해야
    나중에 대체 연결이 원본 hash를 검증할 근거가 생긴다."""
    monkeypatch.setattr(ras, "RESULTS_DIR", tmp_path)
    result_path = tmp_path / "trial-pod_kill-proposed-99-mainexp-v1.json"
    result_path.write_text(json.dumps({"run_id": "pod_kill-proposed-99-mainexp-v1", "outcome": "invalid_run"}),
                            encoding="utf-8")
    with patch.object(ras.subprocess, "run", return_value=_fake_completed_process(1)):
        outcome = ras.real_run_trial({"scenario": "pod_kill", "arm": "proposed", "repetition": 99,
                                       "run_id": "pod_kill-proposed-99-mainexp-v1", "sequence_index": 1})
    assert outcome["status"] == "failed"
    assert outcome["result_path"] == str(result_path)
    assert outcome["result_hash"] == ras.compute_file_sha256(result_path)
    print("OK - 실행기 비정상 종료여도 결과 파일이 있으면 경로/hash가 보존됨")


def test_real_run_trial_failure_without_result_file_has_no_path_or_hash(tmp_path, monkeypatch):
    """§105 - 결과 파일이 정말 없는 경우(더 이른 단계에서 죽음)는 위 케이스와
    구분돼야 한다 - result_path/result_hash가 없어야(또는 None) 한다."""
    monkeypatch.setattr(ras, "RESULTS_DIR", tmp_path)
    with patch.object(ras.subprocess, "run", return_value=_fake_completed_process(1)):
        outcome = ras.real_run_trial({"scenario": "pod_kill", "arm": "proposed", "repetition": 98,
                                       "run_id": "pod_kill-proposed-98-mainexp-v1", "sequence_index": 1})
    assert outcome["status"] == "failed"
    assert outcome.get("result_path") is None
    assert outcome.get("result_hash") is None
    print("OK - 결과 파일이 없으면 result_path/result_hash가 채워지지 않음(파일 있는 경우와 구분됨)")


def _state_with_linked_replacement_and_result_file(tmp_path, original_hash_in_link=None):
    state, _ = _state_with_failed_pod_kill_proposed()
    ras.link_technical_invalid_replacement(
        state, "pod_kill-proposed-01-mainexp-v1", "pod_kill-proposed-01-retry1-mainexp-v1", "재시도")
    if original_hash_in_link is not None:
        state["replacements"]["pod_kill-proposed-01-mainexp-v1"]["original_result_hash"] = original_hash_in_link
    result_path = tmp_path / "trial-pod_kill-proposed-01-mainexp-v1.json"
    result_path.write_text(json.dumps({"run_id": "pod_kill-proposed-01-mainexp-v1", "outcome": "invalid_run"}),
                            encoding="utf-8")
    return state, result_path


def test_verify_and_backfill_original_hash_fills_null_from_actual_file(tmp_path):
    state, result_path = _state_with_linked_replacement_and_result_file(tmp_path)
    assert state["replacements"]["pod_kill-proposed-01-mainexp-v1"]["original_result_hash"] is None
    ras.verify_and_backfill_original_hash(state, "pod_kill-proposed-01-mainexp-v1", results_dir=tmp_path)
    expected = ras.compute_file_sha256(result_path)
    assert state["replacements"]["pod_kill-proposed-01-mainexp-v1"]["original_result_hash"] == expected
    print("OK - null이던 original_result_hash가 실제 파일 기준으로 채워짐")


def test_verify_and_backfill_original_hash_never_touches_original_trial_slot(tmp_path):
    state, _ = _state_with_linked_replacement_and_result_file(tmp_path)
    original_before = dict(state["trials"]["pod_kill-proposed-01-mainexp-v1"])
    ras.verify_and_backfill_original_hash(state, "pod_kill-proposed-01-mainexp-v1", results_dir=tmp_path)
    assert state["trials"]["pod_kill-proposed-01-mainexp-v1"] == original_before, \
        "원본 failed 슬롯은 이 함수가 절대 건드리면 안 됨"
    print("OK - 원본 JSON/슬롯은 backfill 이후에도 완전히 그대로")


def test_verify_and_backfill_original_hash_noop_when_already_matching(tmp_path):
    state, result_path = _state_with_linked_replacement_and_result_file(tmp_path)
    correct_hash = ras.compute_file_sha256(result_path)
    state["replacements"]["pod_kill-proposed-01-mainexp-v1"]["original_result_hash"] = correct_hash
    ras.verify_and_backfill_original_hash(state, "pod_kill-proposed-01-mainexp-v1", results_dir=tmp_path)
    assert state["replacements"]["pod_kill-proposed-01-mainexp-v1"]["original_result_hash"] == correct_hash
    print("OK - 이미 일치하는 hash는 그대로 유지(재검증만 하고 통과)")


def test_verify_and_backfill_original_hash_fails_closed_on_mismatch(tmp_path):
    """§105 - '이후 재개 때도 불일치하면 fail-closed' 요구사항의 핵심 -
    저장된 hash와 실제 파일의 현재 hash가 다르면(파일이 사후 변조/손상됐을
    가능성) 즉시 거부해야 한다."""
    state, _ = _state_with_linked_replacement_and_result_file(tmp_path, original_hash_in_link="stale-wrong-hash")
    with pytest.raises(ValueError, match="불일치"):
        ras.verify_and_backfill_original_hash(state, "pod_kill-proposed-01-mainexp-v1", results_dir=tmp_path)
    assert state["replacements"]["pod_kill-proposed-01-mainexp-v1"]["original_result_hash"] == "stale-wrong-hash", \
        "검증 실패 시 기존 값을 조용히 덮어쓰면 안 됨"
    print("OK - 저장된 hash와 실제 파일이 다르면 fail-closed(조용히 덮어쓰지 않음)")


def test_verify_and_backfill_original_hash_fails_closed_on_missing_file(tmp_path):
    state, _ = _state_with_failed_pod_kill_proposed()
    ras.link_technical_invalid_replacement(
        state, "pod_kill-proposed-01-mainexp-v1", "pod_kill-proposed-01-retry1-mainexp-v1", "재시도")
    # 원본 결과 파일을 tmp_path에 만들지 않음 - 파일이 정말 없는 경우
    with pytest.raises(ValueError, match="없음"):
        ras.verify_and_backfill_original_hash(state, "pod_kill-proposed-01-mainexp-v1", results_dir=tmp_path)
    print("OK - 원본 결과 파일이 없으면 fail-closed(hash 검증 자체가 불가하므로)")


def test_verify_and_backfill_original_hash_fails_closed_on_run_id_mismatch(tmp_path):
    state, _ = _state_with_failed_pod_kill_proposed()
    ras.link_technical_invalid_replacement(
        state, "pod_kill-proposed-01-mainexp-v1", "pod_kill-proposed-01-retry1-mainexp-v1", "재시도")
    result_path = tmp_path / "trial-pod_kill-proposed-01-mainexp-v1.json"
    result_path.write_text(json.dumps({"run_id": "some-other-run-id", "outcome": "invalid_run"}), encoding="utf-8")
    with pytest.raises(ValueError, match="run_id"):
        ras.verify_and_backfill_original_hash(state, "pod_kill-proposed-01-mainexp-v1", results_dir=tmp_path)
    print("OK - 파일 안 run_id가 기대값과 다르면 fail-closed(잘못된 파일을 가리킬 위험)")


def test_verify_and_backfill_original_hash_requires_existing_link(tmp_path):
    state, _ = _state_with_failed_pod_kill_proposed()
    with pytest.raises(ValueError, match="연결된 대체가 없음"):
        ras.verify_and_backfill_original_hash(state, "pod_kill-proposed-01-mainexp-v1", results_dir=tmp_path)
    print("OK - 대체가 연결 안 된 원본에는 호출 자체가 거부됨")


def test_main_resume_fails_closed_on_replacement_hash_mismatch(tmp_path):
    """§105 - CLI --resume 경로에서도(단순 함수 호출이 아니라) 대체가 연결된
    원본의 hash 불일치가 있으면 실행 전에 즉시 거부해야 한다."""
    state, _ = _state_with_failed_pod_kill_proposed()
    ras.link_technical_invalid_replacement(
        state, "pod_kill-proposed-01-mainexp-v1", "pod_kill-proposed-01-retry1-mainexp-v1", "재시도")
    state["replacements"]["pod_kill-proposed-01-mainexp-v1"]["original_result_hash"] = "definitely-wrong"
    # 원본 결과 파일은 상태 파일과 같은 디렉터리(results_dir 기본값)에 없으므로
    # RESULTS_DIR 자체가 아니라 CLI의 기본 검증 대상이 tmp_path가 되도록 결과 파일을 만든다.
    result_path = ras.RESULTS_DIR / "trial-pod_kill-proposed-01-mainexp-v1.json"
    state_path = tmp_path / "state.json"
    ras.save_state_atomic(state_path, state)
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "run_all_scenarios.py"),
         "--resume", "--dry-run", "--scenario", "pod_kill", "--state-file", str(state_path)],
        capture_output=True, text=True, cwd=Path(__file__).parent)
    assert proc.returncode != 0
    assert "HASH VERIFICATION FAILED" in proc.stderr or "없음" in proc.stderr
    print("OK - CLI --resume도 대체 원본 hash 불일치/파일없음 시 즉시 거부")


# ---- §107: real_preflight()/real_postflight_cleanup_check() 실제 안전 검사 ----

def _ok(**extra):
    return {"ok": True, "reason": None, **extra}


def _fail(reason, **extra):
    return {"ok": False, "reason": reason, **extra}


def test_real_safety_checks_all_pass_returns_ok():
    funcs = (("a", lambda: _ok()), ("b", lambda: _ok()))
    result = ras.real_safety_checks(check_funcs=funcs)
    assert result["ok"] is True
    assert set(result["checks"]) == {"a", "b"}
    print("OK - 모든 개별 검사가 통과하면 real_safety_checks 전체도 ok=True")


def test_real_safety_checks_stops_at_first_failure_and_names_it():
    calls = []

    def a():
        calls.append("a")
        return _fail("a 실패 사유")

    def b():
        calls.append("b")
        return _ok()

    result = ras.real_safety_checks(check_funcs=(("a", a), ("b", b)))
    assert result["ok"] is False
    assert "[a] a 실패 사유" == result["reason"]
    assert calls == ["a"], "첫 실패 이후 뒤 검사는 실행되면 안 됨(불필요한 클러스터 호출 방지)"
    print("OK - 첫 실패 항목에서 멈추고 그 이름을 reason에 남김, 뒤 검사는 실행 안 함")


def test_real_preflight_and_postflight_delegate_to_real_safety_checks(monkeypatch):
    calls = []
    monkeypatch.setattr(ras, "real_safety_checks", lambda trial, phase: calls.append((trial["run_id"], phase)) or _ok())
    assert ras.real_preflight({"run_id": "x"}, {}) == _ok()
    assert ras.real_postflight_cleanup_check({"run_id": "x"}) == _ok()
    assert calls == [("x", "pre"), ("x", "post")]
    print("OK - real_preflight/real_postflight_cleanup_check가 real_safety_checks(trial, phase)를 "
          "'pre'/'post'로 구분해 호출(같은 검사 스위트 공유, restart-baseline 비교에 phase가 필요)")


def test_poll_until_ok_retries_within_timeout_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr(ras.time, "sleep", lambda s: sleeps.append(s))
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        return _ok() if attempts["n"] >= 3 else _fail("아직 전파 중")

    result = ras._poll_until_ok(flaky, timeout_sec=100.0, poll_interval_sec=3.0)
    assert result["ok"] is True
    assert attempts["n"] == 3
    assert sleeps == [3.0, 3.0]
    print("OK - 일시적 실패는 poll_interval마다 재시도해 timeout 안에 성공하면 ok=True로 수렴")


def test_poll_until_ok_gives_up_after_timeout_fail_closed(monkeypatch):
    real_monotonic = ras.time.monotonic()
    clock = {"t": real_monotonic}
    monkeypatch.setattr(ras.time, "monotonic", lambda: clock["t"])

    def fake_sleep(s):
        clock["t"] += s
    monkeypatch.setattr(ras.time, "sleep", fake_sleep)

    result = ras._poll_until_ok(lambda: _fail("영구 잔여"), timeout_sec=10.0, poll_interval_sec=3.0)
    assert result["ok"] is False
    assert result["reason"] == "영구 잔여"
    print("OK - timeout을 넘기도록 계속 실패하면 무한정 기다리지 않고 마지막 결과 그대로 fail-closed 반환")


def _fake_node(name, ready="True", mem="False", disk="False", pid="False"):
    class C:
        def __init__(self, type_, status):
            self.type, self.status = type_, status
    class Status:
        conditions = [C("Ready", ready), C("MemoryPressure", mem), C("DiskPressure", disk), C("PIDPressure", pid)]
    class Node:
        metadata = type("M", (), {"name": name})()
        status = Status()
    return Node()


def test_check_all_nodes_healthy_ok_when_both_nodes_healthy(monkeypatch):
    from kubernetes import client
    import active_pod_resolver
    monkeypatch.setattr(active_pod_resolver, "load_kube_config", lambda: None)

    class FakeCoreApi:
        def list_node(self):
            return type("L", (), {"items": [_fake_node("sj-control"), _fake_node("sj-worker")]})()

    monkeypatch.setattr(client, "CoreV1Api", FakeCoreApi)
    result = ras._check_all_nodes_healthy()
    assert result["ok"] is True
    print("OK - 클러스터의 모든 Node(§108 - control-plane 포함, active pod 노드 하나만이 아님)가 정상이면 ok=True")


def test_check_all_nodes_healthy_fails_when_control_plane_node_unhealthy(monkeypatch):
    """§108 핵심 확인 - active pod은 sj-worker에만 뜨지만, sj-control(active
    pod과 무관한 control-plane 노드)이 나빠져도 잡혀야 한다(§107은 active
    pod의 노드만 봐서 이걸 놓쳤다)."""
    from kubernetes import client
    import active_pod_resolver
    monkeypatch.setattr(active_pod_resolver, "load_kube_config", lambda: None)

    class FakeCoreApi:
        def list_node(self):
            return type("L", (), {"items": [_fake_node("sj-control", ready="False"), _fake_node("sj-worker")]})()

    monkeypatch.setattr(client, "CoreV1Api", FakeCoreApi)
    result = ras._check_all_nodes_healthy()
    assert result["ok"] is False
    assert "sj-control" in result["reason"]
    print("OK - active pod과 무관한 control-plane 노드(sj-control)가 NotReady여도 잡힘(§107은 놓쳤던 항목)")


def test_check_all_nodes_healthy_fails_on_pressure(monkeypatch):
    from kubernetes import client
    import active_pod_resolver
    monkeypatch.setattr(active_pod_resolver, "load_kube_config", lambda: None)

    class FakeCoreApi:
        def list_node(self):
            return type("L", (), {"items": [_fake_node("sj-worker", mem="True")]})()

    monkeypatch.setattr(client, "CoreV1Api", FakeCoreApi)
    result = ras._check_all_nodes_healthy()
    assert result["ok"] is False
    print("OK - MemoryPressure 등 다른 압박 조건도 잡힘")


def _patch_active_pod(monkeypatch, uid="u1", restart_count=0, oom_killed=False, name="vllm-x"):
    import active_pod_resolver
    import memory_pressure_adapter as mpa
    monkeypatch.setattr(active_pod_resolver, "get_active_pods", lambda: [{"name": name, "uid": uid}])
    monkeypatch.setattr(mpa, "get_pod_details", lambda n: {
        "name": n, "uid": uid, "node_name": "sj-worker", "oom_killed": oom_killed, "restart_count": restart_count})


def test_restart_baseline_pre_phase_records_baseline_and_passes(monkeypatch):
    ras._PREFLIGHT_POD_BASELINE.clear()
    _patch_active_pod(monkeypatch, uid="u1", restart_count=0)
    result = ras._check_active_pod_restart_baseline({"run_id": "run-a"}, "pre")
    assert result["ok"] is True
    assert ras._PREFLIGHT_POD_BASELINE["run-a"] == {"pod_uid": "u1", "restart_count": 0}
    print("OK - phase='pre'는 현재 pod UID/restart_count를 baseline으로 기록만 하고 통과")


def test_restart_baseline_fails_on_wrong_pod_count(monkeypatch):
    import active_pod_resolver
    monkeypatch.setattr(active_pod_resolver, "get_active_pods", lambda: [])
    result = ras._check_active_pod_restart_baseline({"run_id": "run-x"}, "pre")
    assert result["ok"] is False
    assert "개수 이상" in result["reason"]
    print("OK - active pod이 정확히 1개가 아니면(0개/2개 이상) 실패")


def test_restart_baseline_fails_on_oom_killed(monkeypatch):
    _patch_active_pod(monkeypatch, oom_killed=True)
    result = ras._check_active_pod_restart_baseline({"run_id": "run-x"}, "pre")
    assert result["ok"] is False
    assert "OOMKilled" in result["reason"]
    print("OK - active pod이 OOMKilled 상태면 phase 무관하게 실패")


def test_restart_baseline_same_uid_restart_increase_fails():
    """pod_kill이 아닌 시나리오(load_ramp/network_degrade 등, pod 교체 없음)
    에서 같은 pod이 재시작하면(UID 불변, restart_count 증가) 예상 밖으로
    잡아야 한다."""
    ras._PREFLIGHT_POD_BASELINE.clear()
    with pytest.MonkeyPatch.context() as mp:
        _patch_active_pod(mp, uid="u1", restart_count=0)
        pre = ras._check_active_pod_restart_baseline({"run_id": "run-b"}, "pre")
        assert pre["ok"] is True
    with pytest.MonkeyPatch.context() as mp:
        _patch_active_pod(mp, uid="u1", restart_count=1)  # 같은 UID, restart_count만 증가
        post = ras._check_active_pod_restart_baseline({"run_id": "run-b"}, "post")
    assert post["ok"] is False
    assert "예상 밖 restart 증가" in post["reason"]
    print("OK - 동일 pod(UID 불변)에서 restart_count가 늘면 예상 밖 재시작으로 실패(pod_kill의 의도된 교체와 구분)")


def test_restart_baseline_uid_change_from_pod_kill_is_not_flagged():
    """pod_kill이 대상을 delete하고 새 UID의 pod이 뜨는 건 의도된 교체이므로
    그 자체로는 실패시키지 않는다 - 새 pod의 restart_count가 0(정상적인
    첫 기동)이면 통과해야 한다."""
    ras._PREFLIGHT_POD_BASELINE.clear()
    with pytest.MonkeyPatch.context() as mp:
        _patch_active_pod(mp, uid="u1", restart_count=0)
        pre = ras._check_active_pod_restart_baseline({"run_id": "run-c"}, "pre")
        assert pre["ok"] is True
    with pytest.MonkeyPatch.context() as mp:
        _patch_active_pod(mp, uid="u2", restart_count=0, name="vllm-y")  # pod_kill이 만든 새 pod
        post = ras._check_active_pod_restart_baseline({"run_id": "run-c"}, "post")
    assert post["ok"] is True
    print("OK - pod_kill이 의도적으로 삭제한 대상의 UID 교체는 그 자체로 실패시키지 않음(새 pod의 restart_count=0이면 통과)")


def test_restart_baseline_uid_change_but_new_pod_already_restarted_fails():
    """UID가 바뀌었어도(교체는 정상) 그 새 pod 자체가 이미 재시작 이력이
    있으면(정상적인 첫 기동이면 0이어야 함) 예상 밖으로 잡아야 한다."""
    ras._PREFLIGHT_POD_BASELINE.clear()
    with pytest.MonkeyPatch.context() as mp:
        _patch_active_pod(mp, uid="u1", restart_count=0)
        ras._check_active_pod_restart_baseline({"run_id": "run-d"}, "pre")
    with pytest.MonkeyPatch.context() as mp:
        _patch_active_pod(mp, uid="u2", restart_count=2, name="vllm-y")
        post = ras._check_active_pod_restart_baseline({"run_id": "run-d"}, "post")
    assert post["ok"] is False
    assert "예상 밖" in post["reason"]
    print("OK - 교체된 새 pod 자체가 이미 재시작 이력이 있으면(restart_count>0) 예상 밖으로 실패")


def test_restart_baseline_without_preflight_falls_back_to_absolute_check():
    """preflight 없이 postflight만 단독 호출되면(baseline 없음) delta 비교
    없이 현재 절대값(restart_count>0)만 fail-closed로 확인한다 - 조용히
    통과시키지 않는다."""
    ras._PREFLIGHT_POD_BASELINE.clear()
    with pytest.MonkeyPatch.context() as mp:
        _patch_active_pod(mp, uid="u9", restart_count=1)
        result = ras._check_active_pod_restart_baseline({"run_id": "never-preflighted"}, "post")
    assert result["ok"] is False
    assert "baseline 없어" in result["reason"]
    print("OK - baseline이 없으면(preflight 미실행) delta 비교 없이 현재 restart_count>0만으로 fail-closed")


def test_check_active_endpoint_ok_when_one_to_one(monkeypatch):
    import active_pod_resolver
    monkeypatch.setattr(active_pod_resolver, "get_active_pods", lambda: [{"name": "vllm-x", "uid": "u1"}])
    monkeypatch.setattr(ras, "_endpointslice_addresses", lambda ns, svc: ["10.0.0.1"])
    result = ras._check_active_endpoint()
    assert result["ok"] is True
    print("OK - active pod 1개 + endpoint IP 1개면 ok=True")


def test_check_active_endpoint_fails_when_endpoint_empty():
    """pod_kill이 만드는 엔드포인트 공백 구간을 그대로 재현 - active pod은
    있지만(교체 대기 중이라 0개일 수도 있음) EndpointSlice 주소가 없음."""
    import active_pod_resolver

    result = None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(active_pod_resolver, "get_active_pods", lambda: [{"name": "vllm-x", "uid": "u1"}])
        mp.setattr(ras, "_endpointslice_addresses", lambda ns, svc: [])
        result = ras._check_active_endpoint()
    assert result["ok"] is False
    assert "endpoint" in result["reason"]
    print("OK - endpoint IP가 0개면(§106 조사의 pod_kill 갭 상황) 실패")


def _fake_rs(name, replicas, pod_hash, current=None, ready=None):
    """current/ready 생략 시 desired(replicas)와 같은 값 - 기존 호출부(전부
    "desired와 실제가 같은" 정상/이상 케이스)는 그대로 동작하고, §111의
    "desired=0인데 current/ready가 아직 안 줄어든 Terminating 중" 케이스만
    명시적으로 다르게 지정한다."""
    current = replicas if current is None else current
    ready = replicas if ready is None else ready
    class RS:
        metadata = type("M", (), {"name": name, "labels": {"rollouts-pod-template-hash": pod_hash}})()
        spec = type("S", (), {"replicas": replicas})()
        status = type("St", (), {"replicas": current, "ready_replicas": ready})()
    return RS()


_ROLLOUT_ABORTED_CONDITIONS = [
    {"type": "Progressing", "status": "False", "reason": "RolloutAborted", "message": "Rollout aborted update"},
]


def _patch_rollout(monkeypatch, phase="Healthy", active="abc123", preview=None, rs_items=None,
                    conditions=None, pause_conditions=None):
    import blue_green_prep as bgp

    class FakeCustomApi:
        def get_namespaced_custom_object(self, group, version, ns, plural, name):
            return {"status": {"phase": phase, "pauseConditions": pause_conditions,
                                "conditions": conditions or [],
                                "blueGreen": {"activeSelector": active,
                                              "previewSelector": preview if preview is not None else active}}}

    class FakeAppsApi:
        def list_namespaced_replica_set(self, ns, label_selector):
            return type("L", (), {"items": rs_items or [_fake_rs("vllm-serving-" + active, 1, active)]})()

    monkeypatch.setattr(bgp, "_custom_api", lambda: FakeCustomApi())
    monkeypatch.setattr(bgp, "_apps_api", lambda: FakeAppsApi())


def _patch_active_pod_and_endpoint_for_rollout(monkeypatch, active_hash="abc123", pod_name=None, endpoint_ips=None):
    """§111 - Degraded+RolloutAborted 예외 경로가 추가로 확인하는 active
    Service selector/endpoint 증거를 채운다."""
    import active_pod_resolver
    pod_name = pod_name if pod_name is not None else f"vllm-serving-{active_hash}-xyz"
    monkeypatch.setattr(active_pod_resolver, "get_active_pods", lambda: [{"name": pod_name, "uid": "u1"}])
    monkeypatch.setattr(ras, "_endpointslice_addresses",
                         lambda ns, svc: endpoint_ips if endpoint_ips is not None else ["10.0.0.1"])


def test_check_rollout_healthy_single_revision_ok_at_rest(monkeypatch):
    _patch_rollout(monkeypatch, phase="Healthy", active="abc123")
    result = ras._check_rollout_healthy_single_revision()
    assert result["ok"] is True
    assert result["classification"] == "healthy"
    print("OK - phase=Healthy + activeSelector==previewSelector(preview 없음) + 단일 revision이면 "
          "ok=True, classification='healthy'(§111 - aborted_preview_rolled_back와 구별)")


# ---------------------------------------------------------------------------
# §111(2026-09-24, load_ramp-fixed_threshold-01-mainexp-v2 사고 계기) -
# phase=Degraded+RolloutAborted는 미승격 preview가 정상적으로 abort+복원된
# 뒤 Argo Rollouts가 다음 업데이트 전까지 남겨두는 정상 상태다(§110 조사) -
# 근거가 전부 확인될 때만 예외로 허용하고 "healthy"와 구별해 기록한다.
# ---------------------------------------------------------------------------

def test_check_rollout_aborted_preview_fully_restored_is_allowed_and_classified_distinctly(monkeypatch):
    """정확한 경로 - phase=Degraded, reason=RolloutAborted, pauseConditions
    없음, active endpoint 있음, active pod이 activeSelector와 일치, 다른
    모든 revision의 desired/current/ready 전부 0."""
    _patch_rollout(monkeypatch, phase="Degraded", active="abc123",
                    conditions=_ROLLOUT_ABORTED_CONDITIONS, pause_conditions=None,
                    rs_items=[
                        _fake_rs("vllm-serving-abc123", 1, "abc123"),
                        _fake_rs("vllm-serving-oldpreview", 0, "def456"),  # abort된 preview, 완전히 scale-down됨
                    ])
    _patch_active_pod_and_endpoint_for_rollout(monkeypatch, active_hash="abc123")
    result = ras._check_rollout_healthy_single_revision()
    assert result["ok"] is True
    assert result["classification"] == "aborted_preview_rolled_back", (
        "이 예외 경로는 'healthy'가 아니라 구별되는 값으로 기록돼야 함(요청 원문)")
    print("OK - phase=Degraded+RolloutAborted이고 복원 근거가 전부 있으면 예외로 허용되지만 "
          "classification='aborted_preview_rolled_back'로 'healthy'와 구별해 기록됨")


def test_check_rollout_degraded_other_reason_still_blocks(monkeypatch):
    """Degraded인데 reason이 RolloutAborted가 아니면(진짜 문제일 수 있음)
    예외를 적용하지 않고 그대로 실패시켜야 한다."""
    other_conditions = [{"type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded",
                          "message": "timed out waiting for rollout to finish"}]
    _patch_rollout(monkeypatch, phase="Degraded", active="abc123", conditions=other_conditions)
    result = ras._check_rollout_healthy_single_revision()
    assert result["ok"] is False
    assert result["classification"] == "unhealthy"
    assert "RolloutAborted" in result["reason"]
    print("OK - phase=Degraded인데 reason이 RolloutAborted가 아니면(예: ProgressDeadlineExceeded) 예외 없이 실패")


def test_check_rollout_aborted_preview_fails_if_pause_conditions_remain(monkeypatch):
    _patch_rollout(monkeypatch, phase="Degraded", active="abc123",
                    conditions=_ROLLOUT_ABORTED_CONDITIONS,
                    pause_conditions=[{"reason": "BlueGreenPause"}])
    _patch_active_pod_and_endpoint_for_rollout(monkeypatch, active_hash="abc123")
    result = ras._check_rollout_healthy_single_revision()
    assert result["ok"] is False
    assert result["classification"] == "unhealthy"
    assert "pauseConditions" in result["reason"]
    print("OK - Degraded+RolloutAborted라도 pauseConditions가 남아있으면(복원 미완료 의심) 예외 미적용, 실패")


def test_check_rollout_aborted_preview_fails_if_endpoint_empty(monkeypatch):
    _patch_rollout(monkeypatch, phase="Degraded", active="abc123", conditions=_ROLLOUT_ABORTED_CONDITIONS)
    _patch_active_pod_and_endpoint_for_rollout(monkeypatch, active_hash="abc123", endpoint_ips=[])
    result = ras._check_rollout_healthy_single_revision()
    assert result["ok"] is False
    assert result["classification"] == "unhealthy"
    assert "endpoint" in result["reason"]
    print("OK - Degraded+RolloutAborted인데 active Service endpoint가 비어있으면(안정 revision 서빙 미확인) 실패")


def test_check_rollout_aborted_preview_fails_if_active_pod_mismatch(monkeypatch):
    _patch_rollout(monkeypatch, phase="Degraded", active="abc123", conditions=_ROLLOUT_ABORTED_CONDITIONS)
    _patch_active_pod_and_endpoint_for_rollout(monkeypatch, active_hash="abc123",
                                                pod_name="vllm-serving-def456-zzz")  # activeSelector와 다른 pod
    result = ras._check_rollout_healthy_single_revision()
    assert result["ok"] is False
    assert result["classification"] == "unhealthy"
    print("OK - Degraded+RolloutAborted인데 active pod이 activeSelector와 안 맞으면 실패")


def test_check_rollout_aborted_preview_fails_if_stray_replicas_remain(monkeypatch):
    """§111 - 원인이 정확히 RolloutAborted여도 잔여 replica(desired/current/
    ready 중 하나라도 0이 아님)가 있으면 예외를 적용하지 않는다."""
    _patch_rollout(monkeypatch, phase="Degraded", active="abc123",
                    conditions=_ROLLOUT_ABORTED_CONDITIONS,
                    rs_items=[
                        _fake_rs("vllm-serving-abc123", 1, "abc123"),
                        _fake_rs("vllm-serving-oldpreview", 0, "def456", current=1, ready=0),  # 아직 Terminating 중
                    ])
    _patch_active_pod_and_endpoint_for_rollout(monkeypatch, active_hash="abc123")
    result = ras._check_rollout_healthy_single_revision()
    assert result["ok"] is False
    assert "잔존 replica" in result["reason"]
    print("OK - RolloutAborted 예외 조건이어도 preview RS가 아직 Terminating 중(current>0)이면 실패(desired만 보던 §108 강화)")


def test_check_rollout_healthy_path_fails_on_current_or_ready_residue_even_if_desired_zero(monkeypatch):
    """§111 - phase=Healthy 경로에서도 desired=0인데 current/ready가 아직
    안 줄어든 경우를 §108(desired만 확인)보다 엄격하게 잡는다."""
    _patch_rollout(monkeypatch, phase="Healthy", active="abc123", rs_items=[
        _fake_rs("vllm-serving-abc123", 1, "abc123"),
        _fake_rs("vllm-serving-terminating", 0, "oldrev", current=1, ready=1),
    ])
    result = ras._check_rollout_healthy_single_revision()
    assert result["ok"] is False
    assert "잔존 replica" in result["reason"]
    print("OK - phase=Healthy여도 desired=0/current>0(아직 Terminating 중)인 잔여 revision이 있으면 실패")


def test_check_rollout_healthy_single_revision_fails_when_not_healthy_phase(monkeypatch):
    _patch_rollout(monkeypatch, phase="Progressing", active="abc123")
    result = ras._check_rollout_healthy_single_revision()
    assert result["ok"] is False
    assert "Progressing" in result["reason"]
    print("OK - status.phase가 Healthy가 아니면(§107의 is_paused_pre_promotion만으로는 못 잡던 Degraded/Progressing 등) 실패")


def test_check_rollout_healthy_single_revision_fails_when_preview_residual(monkeypatch):
    _patch_rollout(monkeypatch, phase="Healthy", active="abc123", preview="def456")
    result = ras._check_rollout_healthy_single_revision()
    assert result["ok"] is False
    assert "preview 잔여" in result["reason"]
    print("OK - activeSelector != previewSelector면(preview 잔여 의심) 실패")


def test_check_rollout_healthy_single_revision_fails_when_old_revision_has_replicas(monkeypatch):
    _patch_rollout(monkeypatch, phase="Healthy", active="abc123", rs_items=[
        _fake_rs("vllm-serving-abc123", 1, "abc123"),
        _fake_rs("vllm-serving-oldrev", 1, "oldrev"),  # active가 아닌데 desired=1로 남음
    ])
    result = ras._check_rollout_healthy_single_revision()
    assert result["ok"] is False
    assert "vllm-serving-oldrev" in result["reason"]
    print("OK - active가 아닌 다른 revision의 ReplicaSet에 desired>0이 남아있으면(단일 revision 아님) 실패")


def test_check_experiment_context_clear_fails_when_context_active(monkeypatch):
    import requests

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"current": {"run_id": "pod_kill-proposed-99-x", "scenario": "pod_kill"}}

    monkeypatch.setattr(requests, "get", lambda url, timeout: FakeResp())
    result = ras._check_experiment_context_clear()
    assert result["ok"] is False
    assert "정리되지 않음" in result["reason"]
    print("OK - §102/§103 사고(orphaned experiment-run context)와 동일 상황을 orchestrator 층에서 독립적으로 탐지")


def test_check_experiment_context_clear_ok_when_null(monkeypatch):
    import requests

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"current": None}

    monkeypatch.setattr(requests, "get", lambda url, timeout: FakeResp())
    result = ras._check_experiment_context_clear()
    assert result["ok"] is True
    print("OK - context가 null이면 ok=True")


def test_check_experiment_context_clear_fails_closed_on_connection_error(monkeypatch):
    import requests

    def raise_conn_error(url, timeout):
        raise requests.exceptions.ConnectionError("연결 거부")
    monkeypatch.setattr(requests, "get", raise_conn_error)
    result = ras._check_experiment_context_clear()
    assert result["ok"] is False
    assert "연결 실패" in result["reason"]
    print("OK - recovery-policy 연결 자체가 안 되면(포트포워드 끊김 등) fail-closed(ok=False), 조용히 통과 안 시킴")


def test_check_quiescent_fails_when_not_quiescent(monkeypatch):
    import requests

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"quiescent": False, "active_count": 2}

    monkeypatch.setattr(requests, "get", lambda url, timeout: FakeResp())
    result = ras._check_quiescent()
    assert result["ok"] is False
    assert "quiescent 아님" in result["reason"]
    print("OK - 이전 trial의 critical alert가 아직 안 풀렸으면(quiescent=False) 실패")


def test_check_no_leftover_chaos_crs_ok_when_empty(monkeypatch):
    from kubernetes import client
    import active_pod_resolver

    monkeypatch.setattr(active_pod_resolver, "load_kube_config", lambda: None)

    class FakeCustomApi:
        def list_namespaced_custom_object(self, group, version, ns, plural):
            return {"items": []}

    monkeypatch.setattr(client, "CustomObjectsApi", FakeCustomApi)
    result = ras._check_no_leftover_chaos_crs()
    assert result["ok"] is True
    print("OK - 3종류(podchaos/networkchaos/stresschaos) 전부 비어있으면 ok=True")


def test_check_no_leftover_chaos_crs_fails_when_leftover_found(monkeypatch):
    from kubernetes import client
    import active_pod_resolver

    monkeypatch.setattr(active_pod_resolver, "load_kube_config", lambda: None)

    class FakeCustomApi:
        def list_namespaced_custom_object(self, group, version, ns, plural):
            if plural == "podchaos":
                return {"items": [{"metadata": {"name": "pod-kill-abc123"}}]}
            return {"items": []}

    monkeypatch.setattr(client, "CustomObjectsApi", FakeCustomApi)
    result = ras._check_no_leftover_chaos_crs()
    assert result["ok"] is False
    assert "podchaos" in result["reason"] and "pod-kill-abc123" in result["reason"]
    print("OK - 어느 하나라도 CR이 남아있으면(기존엔 이런 전체 나열 검사 자체가 없었음, §107 조사) 실패하고 구체적 이름까지 남김")


def test_check_no_leftover_experiment_pods_ok_when_none_match(monkeypatch):
    from kubernetes import client
    import active_pod_resolver

    monkeypatch.setattr(active_pod_resolver, "load_kube_config", lambda: None)

    class FakePod:
        def __init__(self, name):
            self.metadata = type("M", (), {"name": name})()

    class FakeCoreApi:
        def list_namespaced_pod(self, ns):
            return type("L", (), {"items": [FakePod("vllm-serving-abc123")]})()

    monkeypatch.setattr(client, "CoreV1Api", FakeCoreApi)
    result = ras._check_no_leftover_experiment_pods()
    assert result["ok"] is True
    print("OK - ramp-inj-*/ramp-probe-* 접두사에 안 걸리는 정상 pod만 있으면 ok=True")


def test_check_no_leftover_experiment_pods_fails_on_ramp_prefix(monkeypatch):
    """load_ramp_adapter.py의 ramp-inj-*/ramp-probe-* pod은 label이 없어
    (§107 조사) 이름 접두사로만 걸러낼 수 있다는 걸 그대로 검증한다."""
    from kubernetes import client
    import active_pod_resolver

    monkeypatch.setattr(active_pod_resolver, "load_kube_config", lambda: None)

    class FakePod:
        def __init__(self, name):
            self.metadata = type("M", (), {"name": name})()

    class FakeCoreApi:
        def list_namespaced_pod(self, ns):
            return type("L", (), {"items": [FakePod("ramp-inj-deadbeef")]})()

    monkeypatch.setattr(client, "CoreV1Api", FakeCoreApi)
    result = ras._check_no_leftover_experiment_pods()
    assert result["ok"] is False
    assert "ramp-inj-deadbeef" in result["reason"]
    print("OK - 잔여 ramp-inj-*/ramp-probe-* pod을 이름 접두사로 탐지")
