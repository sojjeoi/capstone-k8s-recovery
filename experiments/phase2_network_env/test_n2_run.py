"""n2_run 시험(2026-10-17, M4, DEVELOPMENT). 클러스터 접근 없음 - 가짜 조회기·가짜 deps로 진입점의 동작을 검증한다.
핵심: (1) 읽기 전용 하위 명령이 원래 상태를 보존하고 원복을 정확히 판정한다, (2) 라이브 하위 명령은 구현 동결 전에는 클러스터 연결을 조립하기 전에 멈춘다,
(3) 관리자 토큰은 출력·저장되지 않는다, (4) 인자 연결."""
import json
import sys
from pathlib import Path

import copy

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import n2_common as nc  # noqa: E402
import n2_run as run  # noqa: E402
import n2_stage1 as ns1  # noqa: E402
import n2_stage2 as n2s2  # noqa: E402
import test_run_guarded_pilot as tgp  # noqa: E402
from test_n2_session import CLEAN_POLICY, base_template, template  # noqa: E402

pc = nc.pc


class FakeReader:
    """RealNetLive 자리 - 클러스터 없음. 스냅샷은 실제 canonical Healthy 모양(tgp.CANON_SNAP)이고 snap_over로 일부를 바꾼다."""

    def __init__(self, tpl=None, chaos=(), policy=None, delay=30, snap_over=None):
        self.tpl, self.chaos, self.policy, self.delay = tpl or base_template(), list(chaos), dict(policy or CLEAN_POLICY), delay
        self.snap_over = snap_over or {}

    def snapshot_fn(self):
        return {**copy.deepcopy(tgp.CANON_SNAP), "template": self.tpl, "delay": self.delay, **copy.deepcopy(self.snap_over)}

    def read_state(self, snap=None):
        snap = snap if snap is not None else self.snapshot_fn()
        return {"template": snap["template"], "chaos_names": list(self.chaos), "policy": dict(self.policy), "delay": snap["delay"]}


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """결과 폴더를 임시 폴더로 바꾼다(저장소 results 폴더를 건드리지 않는다)."""
    orig1, orig2 = ns1.NetConfig, n2s2.Net2Config
    monkeypatch.setattr(ns1, "NetConfig", lambda: orig1(results_dir=tmp_path / "s1"))
    monkeypatch.setattr(n2s2, "Net2Config", lambda: orig2(results_dir=tmp_path / "s2"))
    (tmp_path / "s1").mkdir()
    (tmp_path / "s2").mkdir()
    return tmp_path


def call(argv, reader, **kw):
    return run.main(argv, reader_factory=lambda url, profile: reader, **kw)


# ---------- (1) 원래 상태 보존·원복 확인 ----------
def test_baseline_is_written_once_for_a_clean_start_state(dirs, capsys):
    out = dirs / "baseline.json"
    assert call(["capture-baseline", "--out", str(out)], FakeReader()) == 0
    b = json.loads(out.read_text(encoding="utf-8"))
    assert b["state"]["probe_profile"] == {"readiness_timeout_sec": None, "liveness_timeout_sec": None}     # 실제 기본 Rollout: 적혀 있지 않음
    assert b["state"]["policy"]["mode"] == "record_only"
    assert json.loads(capsys.readouterr().out)["ok"] is True
    with pytest.raises(FileExistsError):
        call(["capture-baseline", "--out", str(out)], FakeReader())                 # 덮어쓰기 금지


@pytest.mark.parametrize("reader,needle", [
    (FakeReader(tpl=template(11, 11)), "already_tolerant"),                          # 이미 11 s - 원복 기준이 될 수 없다
    (FakeReader(chaos=["netdelay-old"]), "network_chaos_present"),
    (FakeReader(policy={**CLEAN_POLICY, "mode": "act"}), "policy_mode"),
    (FakeReader(policy={**CLEAN_POLICY, "context": {"run_id": "r"}}), "policy_context_set"),
])
def test_baseline_refuses_a_dirty_or_already_changed_start_state_and_writes_nothing(dirs, capsys, reader, needle):
    out = dirs / "baseline.json"
    assert call(["capture-baseline", "--out", str(out)], reader) == 2
    assert not out.exists() and needle in capsys.readouterr().out


def test_restore_check_detects_each_way_the_demo_state_can_be_left_changed(dirs, capsys):
    out = dirs / "baseline.json"
    call(["capture-baseline", "--out", str(out)], FakeReader())
    capsys.readouterr()
    assert call(["restore-check", "--baseline", str(out)], FakeReader()) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "problems": []}
    cases = [(FakeReader(tpl=template(11, 11)), "probe_profile_not_restored"), (FakeReader(chaos=["netdelay-x"]), "network_chaos_residual"),
             (FakeReader(delay=420), "scale_down_delay"), (FakeReader(policy={**CLEAN_POLICY, "mode": "act"}), "policy_mode"),
             (FakeReader(policy={**CLEAN_POLICY, "context": {"run_id": "r"}}), "policy_context_set")]
    for reader, needle in cases:
        assert call(["restore-check", "--baseline", str(out)], reader) == 2
        assert needle in capsys.readouterr().out, needle
    (dirs / "s1" / "running-guarded_pressure_record_only-n2p01-x.json").write_text("{}", encoding="utf-8")     # 정리 안 된 세션 표식
    assert call(["restore-check", "--baseline", str(out)], FakeReader()) == 2
    assert "running_marker_left" in capsys.readouterr().out


def test_restore_check_with_a_changed_template_beyond_probe_timeouts_is_not_restored(dirs, capsys):
    out = dirs / "baseline.json"
    call(["capture-baseline", "--out", str(out)], FakeReader())
    capsys.readouterr()
    changed = template(1, 1)
    changed["spec"]["containers"][0]["args"].append("--extra")                         # probe가 아닌 필드가 바뀐 채로 남음
    assert call(["restore-check", "--baseline", str(out)], FakeReader(tpl=changed)) == 2
    assert "rollout_template_not_restored" in capsys.readouterr().out


def test_baseline_requires_a_canonical_healthy_state_too(dirs, capsys):
    out = dirs / "baseline.json"
    paused = {"phase": "Paused", "preview": "bbb222"}                                       # 대기 중인 preview 가 있는 상태
    assert call(["capture-baseline", "--out", str(out)], FakeReader(snap_over=paused)) == 2
    assert not out.exists() and "canonical" in capsys.readouterr().out


def test_restore_check_fails_while_the_template_is_back_but_traffic_is_not(dirs, capsys):
    """Codex 검토 blocker: 템플릿 해시가 원래와 같아도 승격이 끝나지 않았거나 이전(tolerant) 리비전이 서비스 중이면 원복이 아니다."""
    out = dirs / "baseline.json"
    call(["capture-baseline", "--out", str(out)], FakeReader())
    capsys.readouterr()
    states = [({"phase": "Paused", "preview": "bbb222"}, "phase=Paused"),                       # 복구 리비전이 preview 로 대기 중
              ({"active": "old111", "stable": "old111", "current": "aaa111", "preview": "aaa111"}, "hash_mismatch"),   # 이전 리비전이 active
              ({"observed": "0"}, "not_reconciled"),                                          # 컨트롤러가 아직 반영 전
              ({"endpoints": {"vllm-active": ["10.9.9.9"], "vllm-preview": ["10.0.0.1"]}}, "active_endpoint_mismatch"),
              ({"rs": {"aaa111": {"uid": "r1", "desired": 1, "current": 1, "ready": 1},
                       "old111": {"uid": "r0", "desired": 1, "current": 1, "ready": 1}}}, "rs_old111"),   # 이전 ReplicaSet 이 아직 남음
              ({"health_rc": 7}, "active_health_rc"), ({"chaos": ["stress"]}, "residual")]
    for over, needle in states:
        assert call(["restore-check", "--baseline", str(out)], FakeReader(snap_over=over)) == 2
        assert needle in capsys.readouterr().out, needle
    assert call(["restore-check", "--baseline", str(out)], FakeReader()) == 0


def test_restore_check_wait_polls_until_the_state_is_canonical_but_not_forever(dirs, capsys, monkeypatch):
    out = dirs / "baseline.json"
    call(["capture-baseline", "--out", str(out)], FakeReader())
    capsys.readouterr()
    clock = {"t": 0.0, "sleeps": 0}
    monkeypatch.setattr(run, "_mono", lambda: clock["t"])
    monkeypatch.setattr(run, "_sleep", lambda s: clock.update(t=clock["t"] + s, sleeps=clock["sleeps"] + 1))

    class Settling(FakeReader):                                                    # 승격 직후: 처음 두 번은 이전 ReplicaSet 이 남아 있다가 세 번째에 사라진다
        reads = 0

        def snapshot_fn(self):
            Settling.reads += 1
            self.snap_over = {"rs": {"aaa111": {"uid": "r1", "desired": 1, "current": 1, "ready": 1},
                                     "old111": {"uid": "r0", "desired": 1, "current": 1, "ready": 1}}} if Settling.reads <= 2 else {}
            return super().snapshot_fn()
    assert call(["restore-check", "--baseline", str(out), "--wait-sec", "120"], Settling()) == 0
    assert clock["sleeps"] == 2 and json.loads(capsys.readouterr().out)["ok"] is True
    clock.update(t=0.0, sleeps=0)
    never = FakeReader(snap_over={"phase": "Paused"})
    assert call(["restore-check", "--baseline", str(out), "--wait-sec", "30"], never) == 2                 # 상한을 넘으면 멈추고 실패를 보고한다
    assert clock["sleeps"] == 3 and "phase=Paused" in capsys.readouterr().out


def test_stage1_records_a_refused_attempt_when_an_existing_freeze_manifest_does_not_match(dirs, tmp_path, monkeypatch):
    """Codex 검토: 원본 run_stage1.main 은 동결 manifest 가 있는데 불일치하면 시도로 기록한다(동결 전이면 기록 없이 종료)."""
    orig = type(ns1.NetConfig())                                                          # dirs 가 바꾼 람다가 아닌 실제 설정 클래스
    monkeypatch.setattr(ns1, "NetConfig", lambda: orig(results_dir=tmp_path / "s1", implementation_freeze="docs/design/PHASE2-NETWORK-ENV-PREREG-v1.md"))
    monkeypatch.setenv("GUARDED_ADMIN_TOKEN", "dummy-token-xyz")

    def mismatch():
        raise SystemExit("implementation freeze fail-closed: 해시 불일치 ['x']")
    with pytest.raises(SystemExit, match="세션 시작 안 함"):
        run.main(["stage1", "--run", "n2n01", "--artifact-root", str(tmp_path)], verify_freeze=mismatch, real_deps=boom)
    rec = json.loads((tmp_path / "s1" / "p2-session-n2n01.json").read_text(encoding="utf-8"))
    assert rec["status"] == "refused_profile_invalid" and rec["cause"] == "implementation_freeze" and rec["attempt_number"] == 1


def test_session_end_check_keeps_the_tolerant_profile_and_is_not_a_full_restore(dirs, capsys):
    """사용자 결정(2026-10-17): 세션 정리(11 s 프로필 유지)와 전체 원복(기본 프로필로 복원)을 구분한다 - 같은 상태가 두 확인에서 반대로 판정된다."""
    tolerant = FakeReader(tpl=template(11, 11))                                           # 실험 block 진행 중: 11 s 프로필
    assert call(["session-end-check"], tolerant) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["scope"].startswith("session_end") and "not a full restore" in out["scope"]
    base = dirs / "baseline.json"
    call(["capture-baseline", "--out", str(base)], FakeReader())                              # 시작 전 기본 상태(timeoutSeconds 없음)
    capsys.readouterr()
    assert call(["restore-check", "--baseline", str(base)], tolerant) == 2                   # 같은 상태는 전체 원복으로는 통과하지 못한다
    assert "probe_profile_not_restored" in capsys.readouterr().out
    assert call(["session-end-check"], FakeReader()) == 2                                    # block 중에 11 s 프로필이 풀려 있으면 문제
    assert "probe_profile_changed_during_collection" in capsys.readouterr().out


@pytest.mark.parametrize("reader,args,needle", [
    (FakeReader(tpl=template(11, 11), chaos=["netdelay-s3"]), [], "network_chaos_residual"),                           # 이 세션의 지연 설정이 남음
    (FakeReader(tpl=template(11, 11), policy={**CLEAN_POLICY, "context": {"run_id": "r"}}), [], "policy_context_set"),
    (FakeReader(tpl=template(11, 11), policy={**CLEAN_POLICY, "blocked": True}), [], "policy_blocked"),
    (FakeReader(tpl=template(11, 11), policy={**CLEAN_POLICY, "unresolved": ["t"]}), [], "policy_unresolved"),
    (FakeReader(tpl=template(11, 11), policy={**CLEAN_POLICY, "mode": "act"}), [], "policy_mode"),                      # record_only를 기대했는데 act
    (FakeReader(tpl=template(11, 11), snap_over={"phase": "Paused", "preview": "bbb222"}), [], "canonical"),           # 정리 안 된 preview
])
def test_session_end_check_fails_on_each_leftover(dirs, capsys, reader, args, needle):
    assert call(["session-end-check", *args], reader) == 2
    assert needle in capsys.readouterr().out


def test_session_end_check_marker_left_and_act_mode_expectation(dirs, capsys):
    (dirs / "s2" / "running-guarded_pressure_act-n2s01-x.json").write_text("{}", encoding="utf-8")
    assert call(["session-end-check", "--policy-mode", "act"], FakeReader(tpl=template(11, 11), policy={**CLEAN_POLICY, "mode": "act"})) == 2
    assert "running_marker_left" in capsys.readouterr().out
    (dirs / "s2" / "running-guarded_pressure_act-n2s01-x.json").unlink()
    assert call(["session-end-check", "--policy-mode", "act"], FakeReader(tpl=template(11, 11), policy={**CLEAN_POLICY, "mode": "act"})) == 0


# ---------- (2) 라이브 하위 명령은 동결 전에는 멈춘다 ----------
def boom(*a, **k):
    raise AssertionError("클러스터 연결 조립이 호출됨")


def test_live_stage1_stops_before_assembling_cluster_connections_while_unfrozen(dirs, tmp_path, monkeypatch):
    monkeypatch.setenv("GUARDED_ADMIN_TOKEN", "dummy-token-xyz")
    with pytest.raises(SystemExit, match="fail-closed"):
        run.main(["stage1", "--run", "n2n01", "--artifact-root", str(tmp_path)], real_deps=boom)


def test_live_stage2_requires_a_selected_outcome_and_the_freeze_before_deps(dirs, tmp_path, monkeypatch):
    monkeypatch.setenv("GUARDED_ADMIN_TOKEN", "dummy-token-xyz")
    ev = tmp_path / "ev.json"
    ev.write_text(json.dumps({"outcome": "NO_ELIGIBLE_DETECTOR"}), encoding="utf-8")
    base = ["stage2", "--run", "n2s01", "--active-profile", str(tmp_path / "a.json"), "--selection-evidence", str(ev),
            "--artifact-root", str(tmp_path)]
    with pytest.raises(SystemExit, match="SELECTED만 허용"):
        run.main(base, real_deps=boom)
    ev.write_text(json.dumps({"outcome": f"SELECTED:{nc.NET_ID}"}), encoding="utf-8")
    with pytest.raises(SystemExit, match="fail-closed"):
        run.main(base, real_deps=boom)


def test_live_commands_need_the_token_and_arguments(dirs, tmp_path, monkeypatch):
    monkeypatch.delenv("GUARDED_ADMIN_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="--run"):
        run.main(["stage1"], verify_freeze=lambda: {}, real_deps=boom)
    with pytest.raises(SystemExit, match="GUARDED_ADMIN_TOKEN"):
        run.main(["stage1", "--run", "n2n01", "--artifact-root", str(tmp_path)], verify_freeze=lambda: {}, real_deps=boom)


def test_stage1_assembly_uses_read_only_reader_and_frozen_flow(dirs, tmp_path, monkeypatch, capsys):
    """동결 검증을 통과했다고 가정하고(가짜) 조립이 맞는지 본다: deps.canonical_snapshot·policy.state가 읽기 전용 조회기로 연결되고
    세션은 record_only로 한 번 호출된다. 토큰은 출력에 나오지 않는다."""
    monkeypatch.setenv("GUARDED_ADMIN_TOKEN", "dummy-token-xyz")
    seen = {}

    class Deps:
        def __init__(self):
            self.canonical_snapshot = lambda: {"template": template()}
            self.policy = type("P", (), {"state": staticmethod(lambda: dict(CLEAN_POLICY))})()
            self.check_unresolved = lambda: None

    class Stub:
        MODE = "record_only"
        RESULTS_DIR = tmp_path / "s1"
        N1_RESULTS_DIR = tmp_path / "n1"
        pc = type("PC", (), {"verify_implementation_freeze": staticmethod(lambda: {"selection_profile_sha256": "s"})})()

        def read_policy_audit(self, mode):
            return []

        def check_unresolved_stage1(self, d):
            return None

        def run_session(self, run, **kw):
            seen["kw"] = kw
            seen["run"] = run
            return {"summary": {"outcome_status": "completed", "cleanup_errors": []}, "session_record": {"integrity": {"ok": True, "problems": []}}}

    stub = Stub()
    monkeypatch.setattr(ns1, "load_stage1", lambda cfg=None, verify_freeze=None: stub)
    monkeypatch.setattr(run.ns2, "install", lambda inst, net, hooks=None: seen.update(net=net, inst=inst))
    code = run.main(["stage1", "--run", "n2n01", "--artifact-root", str(tmp_path)], verify_freeze=lambda: {}, real_deps=lambda *a: Deps())
    out = capsys.readouterr().out
    assert code == 0 and seen["run"] == "n2n01" and seen["kw"]["mode"] == "record_only" and seen["kw"]["impl"] == {"selection_profile_sha256": "s"}
    assert seen["inst"] is stub and seen["net"].snapshot_fn().get("template") and "dummy-token-xyz" not in out
    assert seen["net"]._policy_state() == CLEAN_POLICY                                   # 정책 상태는 deps.policy.state(읽기)로 연결


# ---------- (3) 토큰 ----------
def test_live_reader_needs_the_token_and_never_exposes_it(monkeypatch, capsys):
    import guarded_cleanup_gate as gcg
    import run_guarded_pilot as gp
    monkeypatch.delenv("GUARDED_ADMIN_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="GUARDED_ADMIN_TOKEN"):
        run.live_reader("http://x", "p.json")
    monkeypatch.setenv("GUARDED_ADMIN_TOKEN", "dummy-token-xyz")
    got = {}

    class FakePolicy:
        def __init__(self, url, token):
            got.update(url=url, token=token)

        def state(self):
            return dict(CLEAN_POLICY)
    monkeypatch.setattr(gp, "PolicyClient", FakePolicy)
    monkeypatch.setattr(gcg, "read_snapshot", lambda residuals=True: {"template": template(1, 1), "delay": 30, "pods": []})
    r = run.live_reader("http://127.0.0.1:18090", "p.json")
    assert got == {"url": "http://127.0.0.1:18090", "token": "dummy-token-xyz"}
    assert "dummy-token-xyz" not in repr(vars(r)) and "dummy-token-xyz" not in capsys.readouterr().out


# ---------- (4) 인자 연결 ----------
def test_select_passes_arguments_through_and_requires_artifact_root(dirs, tmp_path, monkeypatch):
    got = {}
    monkeypatch.setattr(run.so, "main", lambda argv, s1=None: got.update(argv=argv, s1=s1) or 0)
    monkeypatch.delenv(run.ARTIFACT_ENV, raising=False)
    with pytest.raises(SystemExit, match="--artifact-root"):
        run.main(["select", "--out-dir", str(tmp_path)], verify_freeze=lambda: {})
    assert run.main(["select", "--artifact-root", str(tmp_path), "--out-dir", str(tmp_path / "o")], verify_freeze=lambda: {}) == 0
    assert got["argv"][:2] == ["--artifact-root", str(tmp_path.resolve())] and got["s1"].CONFIG.plan_id == "capstone-phase2-network-env-v1"


def test_record_validity_writes_once_and_needs_the_operator_note(dirs, tmp_path, monkeypatch, capsys):
    stub = type("S", (), {"RESULTS_DIR": tmp_path / "s1", "validate": staticmethod(
        lambda r, d, *, operator, probe_dir=None: {"run": r, "valid": True, "invalid_reasons": [], "operator": operator})})()
    monkeypatch.setattr(ns1, "load_stage1", lambda cfg=None, verify_freeze=None: stub)
    monkeypatch.setattr(run.ns2, "install", lambda *a, **k: None)
    with pytest.raises(SystemExit, match="--operator-intervention"):
        run.main(["record-validity", "--stage", "1", "--record-validity", "n2p01"], verify_freeze=lambda: {})
    assert run.main(["record-validity", "--stage", "1", "--record-validity", "n2p01", "--operator-intervention", "none"],
                    verify_freeze=lambda: {}) == 0
    assert json.loads((tmp_path / "s1" / "p2-validity-n2p01.json").read_text(encoding="utf-8"))["operator"] == "none"
    assert json.loads(capsys.readouterr().out)["valid"] is True
    with pytest.raises(FileExistsError):
        run.main(["record-validity", "--stage", "1", "--record-validity", "n2p01", "--operator-intervention", "none"],
                 verify_freeze=lambda: {})                                              # 유효성 기록도 덮어쓰지 않는다


def test_parser_rejects_unknown_commands_and_stage2_preflight_needs_a_profile(dirs):
    with pytest.raises(SystemExit):
        run.main(["unknown"])
    with pytest.raises(SystemExit, match="--profile"):
        call(["preflight", "--stage", "2"], FakeReader())
