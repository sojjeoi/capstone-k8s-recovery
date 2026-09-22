"""§98 section 11 - run_all_scenarios.py 오프라인 테스트. 실 클러스터/
subprocess 없이 fake Hooks만 주입해 매트릭스 생성과 순차 실행 로직을
검증한다. 전체가 KUBECONFIG 없이 통과해야 한다(다른 experiments/ 테스트와
동일한 관례)."""
import json
import subprocess
import sys
from pathlib import Path

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
    assert trials[1].run_id not in [c[1] for c in call_log if c[0] == "run_trial"]


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
    trials = ras.build_matrix("P")
    state = _fresh_state(trials)
    call_log = []

    def run_trial_fn(trial):
        if trial["scenario"] == "network_degrade":
            raise RuntimeError("시뮬레이션된 Ctrl+C/예외")
        return {"status": "completed", "result_path": "x", "result_hash": "h"}

    with pytest.raises(RuntimeError):
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
    call_log = []
    ras.run_sequence(trials, state, _make_hooks(call_log), tmp_path / "state.json",
                      from_run_id=trials[2].run_id)
    assert [c[1] for c in call_log if c[0] == "run_trial"] == [trials[2].run_id]


def test_from_run_id_aborts_on_hash_mismatch(tmp_path):
    trials = ras.build_matrix("P")[:3]
    state = _fresh_state(trials)
    state["trials"][trials[0].run_id]["status"] = "completed"
    state["trials"][trials[0].run_id]["result_hash"] = "stale"
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
