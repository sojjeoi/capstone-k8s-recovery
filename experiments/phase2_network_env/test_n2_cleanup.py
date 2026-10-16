"""n2_cleanup 시험(2026-10-17, DEVELOPMENT). 클러스터 접근 없음 - 시연 환경 원복 확인 관문의 동작을 합성 상태로 검증한다."""
import copy
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import n2_cleanup as cl  # noqa: E402
import n2_common as nc  # noqa: E402


def tpl(ready=1, live=1, image="vllm-cpu-env:latest", extra=None):
    c = {"name": "vllm", "image": image, "readinessProbe": {"timeoutSeconds": ready, "periodSeconds": 10},
         "livenessProbe": {"timeoutSeconds": live, "periodSeconds": 10}}
    c.update(extra or {})
    return {"spec": {"containers": [c]}}


CLEAN_POLICY = {"mode": "record_only", "context": None, "blocked": False, "unresolved": []}


def state(template=None, delay=30, policy=None, crs=(), markers=()):
    return cl.capture_state(template or tpl(), delay, policy or CLEAN_POLICY, crs, markers)


# ---------- capture ----------
def test_capture_records_probe_profile_hash_policy_and_is_deterministic():
    s = state()
    assert set(s) == set(cl.STATE_KEYS)
    assert s["probe_profile"] == {"readiness_timeout_sec": 1, "liveness_timeout_sec": 1}
    assert s == state() and s["rollout_template_sha256"] != state(tpl(11, 11))["rollout_template_sha256"]
    assert state(crs=["b", "a"])["network_chaos_names"] == ["a", "b"]
    with pytest.raises(ValueError):
        cl.capture_state({}, 30, CLEAN_POLICY, [], [])


# ---------- 수집 시작 전 ----------
def test_start_state_must_be_clean_and_not_already_tolerant():
    assert cl.start_state_problems(state()) == []
    assert "already_tolerant" in cl.start_state_problems(state(tpl(11, 11)))
    assert any("network_chaos_present" in p for p in cl.start_state_problems(state(crs=["x"])))
    assert any("running_marker_present" in p for p in cl.start_state_problems(state(markers=["running-a.json"])))
    assert any("policy_context_set" in p for p in cl.start_state_problems(state(policy={**CLEAN_POLICY, "context": {"run_id": "r"}})))
    assert any("policy_mode" in p for p in cl.start_state_problems(state(policy={**CLEAN_POLICY, "mode": "act"})))
    no_timeouts = {"spec": {"containers": [{"name": "vllm"}]}}                                # 실제 기본 Rollout: timeoutSeconds 없음 = 정상 시작 상태
    assert cl.start_state_problems(state(no_timeouts)) == []
    assert state(no_timeouts)["probe_profile"] == {"readiness_timeout_sec": None, "liveness_timeout_sec": None}
    assert any("probe_timeout_invalid" in p for p in cl.start_state_problems(state(tpl("1s", 1))))            # 이상한 값만 거부
    assert any("probe_timeout_invalid" in p for p in cl.start_state_problems(state(tpl(0, 1))))
    assert cl.start_state_problems({"probe_profile": {}})            # 기록 불완전 → 거부


# ---------- 세션 끝 ----------
def test_session_end_clean_while_collection_continues():
    end = state(tpl(11, 11))
    assert cl.session_end_problems(end, ["cr-s0", "cr-s1"]) == []


@pytest.mark.parametrize("end_kwargs,needle", [
    (dict(crs=["cr-s2"]), "network_chaos_residual"),
    (dict(markers=["running-guarded_pressure-n2p01.json"]), "running_marker_left"),
    (dict(policy={**CLEAN_POLICY, "mode": "act"}), "policy_mode"),
    (dict(policy={**CLEAN_POLICY, "context": {"run_id": "r"}}), "policy_context_set"),
    (dict(policy={**CLEAN_POLICY, "unresolved": ["t"]}), "policy_unresolved"),
    (dict(policy={**CLEAN_POLICY, "blocked": True}), "policy_blocked"),
])
def test_session_end_detects_residue_and_policy_drift(end_kwargs, needle):
    end = state(tpl(11, 11), **end_kwargs)
    probs = cl.session_end_problems(end, ["cr-s2"], run_id_marker="n2p01")
    assert any(needle in p for p in probs), probs


def test_session_end_flags_planned_cr_not_deleted_and_probe_drift():
    probs = cl.session_end_problems(state(tpl(11, 11), crs=["cr-s1"]), ["cr-s0", "cr-s1"])
    assert any("planned_cr_not_deleted:['cr-s1']" in p for p in probs)
    assert any("probe_profile_changed" in p for p in cl.session_end_problems(state(tpl(1, 1)), []))   # 수집 중 기본값으로 되돌아감
    assert cl.session_end_problems(state(tpl(1, 1)), [], expect_tolerant=False) == []


def test_session_end_other_runs_markers_ignored_only_when_marker_filter_given():
    end = state(tpl(11, 11), markers=["running-other-run.json"])
    assert cl.session_end_problems(end, [], run_id_marker="n2p01") == []
    assert cl.session_end_problems(end, [])                       # 필터 없으면 모든 표식을 잔여물로 봄


def test_session_end_incomplete_record_is_refused():
    assert cl.session_end_problems({"probe_profile": {}}, []) != []


# ---------- 수집 끝(원복) ----------
def test_restore_ok_when_everything_matches_start():
    start = state()
    assert cl.restore_problems(start, copy.deepcopy(start)) == []


def test_restore_detects_tolerant_probe_left_behind():
    start, end = state(), state(tpl(11, 11))
    probs = cl.restore_problems(start, end)
    assert any("probe_profile_not_restored" in p for p in probs) and any("rollout_template_not_restored" in p for p in probs)


def test_restore_detects_any_template_change_even_with_probe_restored():
    start = state()
    end = state(tpl(1, 1, image="vllm-cpu-env:other"))             # probe는 원복, 이미지 필드가 달라짐
    probs = cl.restore_problems(start, end)
    assert probs == ["rollout_template_not_restored"]


@pytest.mark.parametrize("end_kwargs,needle", [
    (dict(delay=0), "scale_down_delay"),
    (dict(policy={**CLEAN_POLICY, "mode": "act"}), "policy_mode"),
    (dict(policy={**CLEAN_POLICY, "context": {"run_id": "r"}}), "policy_context_set"),
    (dict(crs=["left"]), "network_chaos_residual"),
    (dict(markers=["running-x.json"]), "running_marker_left"),
])
def test_restore_detects_each_kind_of_residue(end_kwargs, needle):
    start = state()
    probs = cl.restore_problems(start, state(**end_kwargs))
    assert any(needle in p for p in probs), probs


def test_restore_refuses_start_that_was_already_tolerant():
    """이미 바뀐 상태를 시작으로 기록했다면 '원복'의 기준이 될 수 없다."""
    start = state(tpl(11, 11))
    assert any("start_state_already_tolerant" in p for p in cl.restore_problems(start, copy.deepcopy(start)))


def test_restore_ignores_preexisting_residue_present_at_start():
    start = state(crs=["preexisting"], markers=["running-old.json"])
    assert cl.restore_problems(start, copy.deepcopy(start)) == []     # 시작 때 있던 것은 이 수집의 잔여물이 아님(시작 점검이 따로 막는다)


def test_restore_incomplete_records_refused():
    assert cl.restore_problems({"probe_profile": {}}, state())
    assert cl.restore_problems(state(), {"probe_profile": {}})
