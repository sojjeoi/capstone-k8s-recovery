"""Stage 2 운영 도구 시험(2026-10-18). 서버를 바꾸지 않는다: kubectl·정책 서버·port-forward는 모두 가짜로 바꾼다.
동결 실행기가 승인 기록을 검증하는 함수(run_guarded_pilot.load_act_approval)를 그대로 불러 도구가 만든 기록이 그 검증을 통과하는지 확인한다."""
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import n2_stage2_ops as ops  # noqa: E402
import n2_stage2_shim as shim  # noqa: E402

import run_guarded_pilot as gp  # noqa: E402  (동결 - 승인 기록 검증을 그대로 쓴다)

IMPL = {"manifest": "experiments/phase2_network_env/phase2-network-env-implementation-freeze-r6.json", "manifest_sha256": "a" * 64,
        "selection_profile_sha256": "b" * 64}


def clock(tmp_path, *, n_ok=20, offset=0.05, unc=0.02):
    p = tmp_path / "clock-offset.json"
    p.write_text(json.dumps({"summary": {"n": n_ok, "n_ok": n_ok, "best_offset_sec": offset, "best_uncertainty_sec": unc}}), encoding="utf-8")
    return p


def approval(tmp_path, run="n2s01", **kw):
    return ops.build_approval(run, clock_file=clock(tmp_path, **kw), approved_by="user(chat 2026-10-18)", approved_at="2026-10-18T12:00:00+00:00", impl=IMPL,
                              contract_doc="docs/design/PHASE2-NETWORK-ENV-PREREG-v1-rev4.md", provenance_path=tmp_path / "prov.json", provenance_sha="c" * 64,
                              scope="s")


# ---------- 승인 기록: 동결 실행기의 검증을 통과해야 한다 ----------
def test_the_approval_passes_the_frozen_runners_own_validation_and_is_scoped_to_exactly_one_run(tmp_path):
    a = approval(tmp_path)
    p = tmp_path / "a.json"
    p.write_text(json.dumps(a), encoding="utf-8")
    loaded = gp.load_act_approval(p)                                                   # 동결 검증(status·timing·시계 파일·대기 시간·승인자)
    assert loaded["runs"] == ["n2s01"] and a["timing"] == ops.TIMING and a["service_wait_deadline_sec"] >= gp.MIN_SERVICE_WAIT_SEC
    assert a["clock_bound_sec"] == pytest.approx(0.07) and a["status"] == "final" and a["timing_source"] not in ("", "rehearsal", "provisional")
    assert tuple(a["timing"]) == gp.TIMING_KEYS


@pytest.mark.parametrize("kw", [{"n_ok": 5}, {"offset": 0.6}, {"offset": 0.45, "unc": 0.1}, {"offset": float("nan")}])
def test_a_missing_or_large_clock_error_refuses_to_make_the_approval(tmp_path, kw):
    with pytest.raises(SystemExit, match="시계"):
        approval(tmp_path, **kw)


def test_the_approval_timing_equals_the_policy_servers_environment_values():
    assert ops.TIMING == {"max_signal_age_sec": 15.0, "max_future_skew_sec": 2.0, "max_window_to_decision_sec": 10.0}      # GUARDED_MAX_* 와 같다(시작 점검이 대조)


def test_write_new_never_overwrites(tmp_path):
    p = ops.write_new(tmp_path / "x" / "a.json", {"a": 1})
    with pytest.raises(FileExistsError):
        ops.write_new(p, {"a": 2})


# ---------- 실행 출처 기록 ----------
def test_provenance_pins_code_inputs_and_the_act_switch_record(tmp_path):
    act = tmp_path / "act-switch-n2s01.json"
    act.write_text('{"ok": true}', encoding="utf-8")
    files = {k: tmp_path / k for k in ("wrapper.ps1", "runner.py", "shim.py", "active.json", "evidence.json")}
    for f in files.values():
        f.write_bytes(b"x\r\ny\r\n")
    pv = ops.build_provenance("n2s01", "rid", impl=IMPL, act_switch_file=act, active_profile=files["active.json"], selection_evidence=files["evidence.json"],
                              wrapper_script=files["wrapper.ps1"], runner_script=files["runner.py"], shim_script=files["shim.py"],
                              invocation={"Run": "n2s01", "Mode": "act"}, generated_at="t")
    assert pv["implementation_freeze"]["manifest_sha256"] == "a" * 64 and pv["act_switch"] == {"file": act.name, "sha256": ops.text_sha256(act)}
    assert pv["runner_shim_sha256"] == pv["runner_sha256_lf"] == ops.text_sha256(files["runner.py"]) and pv["wrapper_invocation"]["Mode"] == "act"
    assert ops.text_sha256(files["runner.py"]) == ops.hashlib.sha256(b"x\ny\n").hexdigest()                  # CRLF를 LF로 정규화해서 해시한다


# ---------- 정책 모드 전환 기록(서버는 가짜) ----------
class Fake:
    def __init__(self, mode="act", rc=0, rollout_rc=0, timing=None, state=None):
        self.cmds, self.mode, self.rc, self.rollout_rc = [], mode, rc, rollout_rc
        self.timing, self.state = timing or dict(ops.TIMING), state

    def run(self, cmd, **kw):
        self.cmds.append(cmd)
        rc = self.rollout_rc if cmd[1] == "rollout" else self.rc
        return {"rc": rc, "out": "ok", "err": "", "at_utc": "t"}

    def reader(self, url):
        st = self.state or {"mode": self.mode, "context": None, "blocked": None, "unresolved": {}}
        return {"role": "guarded-recovery-policy", "mode": self.mode, "timing": self.timing}, st


class Tunnel:
    def __enter__(self):
        return "http://127.0.0.1:18090"

    def __exit__(self, *e):
        return False


def sw(tmp_path, mode="act", label="act-switch-n2s01", **kw):
    f = Fake(**({"mode": mode} | kw))
    return f, ops.switch_policy(mode, label=label, out_dir=tmp_path, run=f.run, reader=f.reader, tunnel=Tunnel, sleep=lambda s: None)


def test_switching_to_act_runs_set_env_then_waits_for_rollout_then_verifies_identity_and_a_clean_state(tmp_path):
    f, rec = sw(tmp_path)
    assert [c[:3] for c in f.cmds] == [["kubectl", "set", "env"], ["kubectl", "rollout", "status"]] and "GUARDED_MODE=act" in f.cmds[0]
    assert rec["ok"] is True and rec["identity"]["mode"] == "act" and rec["problems"] == []
    assert json.loads((tmp_path / "act-switch-n2s01.json").read_text(encoding="utf-8"))["ok"] is True


def test_reverting_to_record_only_uses_the_same_steps(tmp_path):
    f, rec = sw(tmp_path, mode="record_only", label="revert-record-only-after-n2s01")
    assert "GUARDED_MODE=record_only" in f.cmds[0] and rec["identity"]["mode"] == "record_only"


@pytest.mark.parametrize("kw,needle", [
    ({"rc": 1}, "set_env_or_rollout_failed"), ({"rollout_rc": 1}, "set_env_or_rollout_failed"),
    ({"timing": {"max_signal_age_sec": 99.0, "max_future_skew_sec": 2.0, "max_window_to_decision_sec": 10.0}}, "policy_timing"),
    ({"state": {"mode": "act", "context": {"run_id": "x"}, "blocked": None, "unresolved": {}}}, "policy_state_not_clean"),
    ({"state": {"mode": "act", "context": None, "blocked": {"why": "x"}, "unresolved": {}}}, "policy_state_not_clean"),
    ({"state": {"mode": "act", "context": None, "blocked": None, "unresolved": {"r": 1}}}, "policy_state_not_clean")])
def test_a_failed_or_unclean_switch_fails_closed_but_keeps_the_record(tmp_path, kw, needle):
    with pytest.raises(SystemExit, match=needle):
        sw(tmp_path, **kw)
    rec = json.loads((tmp_path / "act-switch-n2s01.json").read_text(encoding="utf-8"))
    assert rec["ok"] is False and any(needle in p for p in rec["problems"])                                  # 실패해도 결과를 그대로 남긴다


def test_a_mode_mismatch_after_the_switch_is_refused(tmp_path):
    f = Fake(mode="record_only")
    with pytest.raises(SystemExit, match="identity_mode"):
        ops.switch_policy("act", label="x", out_dir=tmp_path, run=f.run, reader=f.reader, tunnel=Tunnel, sleep=lambda s: None)


def test_a_switch_record_label_cannot_be_reused_and_unknown_modes_are_refused(tmp_path):
    sw(tmp_path)
    with pytest.raises(FileExistsError):
        sw(tmp_path)
    with pytest.raises(SystemExit):
        ops.switch_policy("promote", label="y", out_dir=tmp_path)


# ---------- 세션 실행 연결 파일 ----------
def test_the_shim_translates_wrapper_arguments_into_the_stage2_subcommand(monkeypatch):
    monkeypatch.setenv("PHASE2_ARTIFACT_ROOT", "C:/art")
    argv = shim.translate(["--run", "n2s01", "--mode", "act", "--policy-url", "http://127.0.0.1:18090", "--session-file", "s.json", "--act-approval", "a.json"])
    assert argv[:2] == ["--policy-url", "http://127.0.0.1:18090"] and argv[2:4] == ["stage2", "--run"] and "--preflight-only" not in argv
    assert Path(argv[argv.index("--active-profile") + 1]).name == "active-profile.json"
    assert Path(argv[argv.index("--selection-evidence") + 1]).name == "phase2-network-selection-evidence.json"
    assert argv[argv.index("--artifact-root") + 1] == "C:/art" and argv[argv.index("--act-approval") + 1] == "a.json"
    assert shim.translate(["--run", "n2s01", "--mode", "act", "--policy-url", "u", "--session-file", "s", "--act-approval", "a", "--preflight-only"])[-1] == "--preflight-only"


def test_the_shim_refuses_non_act_modes_and_a_missing_artifact_root(monkeypatch):
    base = ["--run", "n2s01", "--policy-url", "u", "--session-file", "s", "--act-approval", "a"]
    monkeypatch.setenv("PHASE2_ARTIFACT_ROOT", "C:/art")
    with pytest.raises(SystemExit, match="act 모드만"):
        shim.translate(base + ["--mode", "record_only"])
    monkeypatch.delenv("PHASE2_ARTIFACT_ROOT")
    with pytest.raises(SystemExit, match="PHASE2_ARTIFACT_ROOT"):
        shim.translate(base + ["--mode", "act"])
    with pytest.raises(SystemExit):
        shim.translate(["--run", "n2s01", "--mode", "act", "--policy-url", "u", "--session-file", "s"])         # act 승인 기록 없이는 시작 안 함


# ---------- 통합: 실제 동결·선택 결과를 읽기 전용으로 쓰고, 새 파일만 만든다 ----------
def test_approve_end_to_end_makes_a_record_the_frozen_runner_accepts(tmp_path):
    import n2_freeze as fz
    try:
        fz.verify_implementation_freeze()
    except SystemExit:
        pytest.skip("구현 동결이 검증되지 않는 상태")
    if not (ops.SELECTION_DIR / "active-profile.json").is_file():
        pytest.skip("선택 결과가 없는 환경")
    (tmp_path / "act-switch-n2s01.json").write_text('{"ok": true}', encoding="utf-8")
    ns = ops.argparse.Namespace(run="n2s01", clock=clock(tmp_path), approved_by="user(chat)", out_dir=str(tmp_path))
    assert ops.cmd_approve(ns) == 0
    ap = next(tmp_path.glob("n2s01-act-approval-*.json"))
    pv = next(tmp_path.glob("execution-provenance-n2s01-*.json"))
    a = gp.load_act_approval(ap)
    assert a["runs"] == ["n2s01"] and a["execution_provenance_sha256"] == ops.text_sha256(pv)
    p = json.loads(pv.read_text(encoding="utf-8"))
    assert p["run_id"].startswith("guarded_pressure_act-n2s01-") and len(p["run_id"]) <= 63 and p["implementation_freeze"]["manifest"].endswith("-r6.json")
    assert p["active_profile_sha256"] == ops.text_sha256(ops.SELECTION_DIR / "active-profile.json")
