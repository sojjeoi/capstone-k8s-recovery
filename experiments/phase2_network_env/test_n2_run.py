"""n2_run 시험(2026-10-17, M4, DEVELOPMENT). 클러스터 접근 없음 - 가짜 조회기·가짜 deps로 진입점의 동작을 검증한다.
핵심: (1) 읽기 전용 하위 명령이 원래 상태를 보존하고 원복을 정확히 판정한다, (2) 라이브 하위 명령은 구현 동결 전에는 클러스터 연결을 조립하기 전에 멈춘다,
(3) 관리자 토큰은 출력·저장되지 않는다, (4) 인자 연결."""
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import n2_common as nc  # noqa: E402
import n2_run as run  # noqa: E402
import n2_stage1 as ns1  # noqa: E402
import n2_stage2 as n2s2  # noqa: E402
from test_n2_session import CLEAN_POLICY, template  # noqa: E402

pc = nc.pc


class FakeReader:
    """RealNetLive.read_state 자리 - 클러스터 없음."""

    def __init__(self, tpl=None, chaos=(), policy=None, delay=30):
        self.tpl, self.chaos, self.policy, self.delay = tpl or template(1, 1), list(chaos), dict(policy or CLEAN_POLICY), delay

    def read_state(self):
        return {"template": self.tpl, "chaos_names": list(self.chaos), "policy": dict(self.policy), "delay": self.delay}


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
    assert b["state"]["probe_profile"] == {"readiness_timeout_sec": 1, "liveness_timeout_sec": 1} and b["state"]["policy"]["mode"] == "record_only"
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
