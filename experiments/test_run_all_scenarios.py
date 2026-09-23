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


# ---- §103: 기술적 invalid 슬롯 대체 연결 (원본 보존, 자동 건너뛰기/재시도 금지, 순서 불변) ----

def _state_with_failed_pod_kill_proposed():
    trials = ras.build_matrix("mainexp-v1")
    pod_kill = [t for t in trials if t.scenario == "pod_kill"]
    state = _fresh_state(pod_kill, plan_id="mainexp-v1")
    state["trials"]["pod_kill-native-01-mainexp-v1"]["status"] = "completed"
    state["trials"]["pod_kill-native-01-mainexp-v1"]["result_hash"] = "native-hash"
    state["trials"]["pod_kill-fixed_threshold-01-mainexp-v1"]["status"] = "completed"
    state["trials"]["pod_kill-fixed_threshold-01-mainexp-v1"]["result_hash"] = "ft-hash"
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
