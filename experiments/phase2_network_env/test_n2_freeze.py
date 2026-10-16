"""n2_freeze 시험(2026-10-17, M5, DEVELOPMENT). 합성 폴더로 동결 작성·검증 규칙을 시험한다 - 실제 동결은 하지 않는다.
핵심: (1) 동결 전에는 모든 실행이 막힌다(fail-closed), (2) 파일 하나만 바뀌어도 검증이 실패한다, (3) 재사용하는 Phase 2 동결이 그대로 유효하다,
(4) 네트워크 환경 파일이 하나도 동결 목록에서 빠지지 않는다, (5) 작성은 한 번만 되고 시험이 100%가 아니면 거부한다."""
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import n2_common as nc  # noqa: E402
import n2_freeze as fz  # noqa: E402
import n2_run as run  # noqa: E402

pc = nc.pc


# ---------- (1) 동결 전 ----------
def test_unfrozen_state_refuses_everything_and_the_live_gate_uses_this_verifier():
    assert nc.PREREG_SHA256 is None or (nc.ROOT / fz.IMPLEMENTATION_FREEZE).is_file()
    if not (nc.ROOT / fz.IMPLEMENTATION_FREEZE).is_file():
        with pytest.raises(SystemExit, match="fail-closed"):
            fz.verify_implementation_freeze()
    assert run.default_verify_freeze() == fz.verify_implementation_freeze                  # 라이브 하위 명령의 동결 관문이 이 검증기에 연결돼 있다
    assert fz.IMPLEMENTATION_FREEZE == nc.HERE.relative_to(nc.ROOT).as_posix() + "/phase2-network-env-implementation-freeze-r1.json"
    import n2_stage1 as ns1
    assert ns1.NetConfig().implementation_freeze == fz.IMPLEMENTATION_FREEZE               # 설정과 검증기가 같은 manifest 경로를 본다


# ---------- (2) 합성 폴더에서의 검증 ----------
@pytest.fixture
def root(tmp_path, monkeypatch):
    (tmp_path / "docs" / "design").mkdir(parents=True)
    prereg = tmp_path / nc.PREREG_DOC
    prereg.write_text("prereg v1\n", encoding="utf-8")
    monkeypatch.setattr(nc, "PREREG_SHA256", fz.text_sha256(prereg))
    design = tmp_path / fz.DESIGN_FREEZE
    design.write_text(json.dumps({"status": "design_frozen", "prereg_sha256": nc.PREREG_SHA256}), encoding="utf-8")
    (tmp_path / "x.py").write_text("a = 1\n", encoding="utf-8")
    return tmp_path


def manifest(root, **over):
    m = {"status": "implementation_frozen", "files": {"x.py": fz.text_sha256(root / "x.py")},
         "design_freeze": {"prereg_sha256": nc.PREREG_SHA256, "manifest_path": fz.DESIGN_FREEZE,
                           "manifest_sha256": fz.text_sha256(root / fz.DESIGN_FREEZE)},
         "tests": {"network_failed": 0, "phase2_failed": 0, "new_regressions": 0}, "selection_profile_sha256": "s", **over}
    (root / "m.json").write_text(json.dumps(m), encoding="utf-8")


def verify(root):
    return fz.verify_implementation_freeze("m.json", root, required=("x.py",), check_phase2=False)


def test_valid_manifest_passes_and_every_tampering_fails_closed(root, monkeypatch):
    manifest(root)
    assert verify(root)["status"] == "verified"
    for over in ({"tests": {"network_failed": 1, "phase2_failed": 0, "new_regressions": 0}},
                 {"tests": {"network_failed": 0, "phase2_failed": 2, "new_regressions": 0}},
                 {"tests": {"network_failed": 0, "phase2_failed": 0, "new_regressions": 1}},
                 {"status": "draft"}, {"files": {"x.py": "0" * 64}}, {"files": {}},
                 {"design_freeze": {"prereg_sha256": "0" * 64, "manifest_path": fz.DESIGN_FREEZE}},
                 {"design_freeze": {"prereg_sha256": nc.PREREG_SHA256, "manifest_path": "other.json"}}):
        manifest(root, **over)
        with pytest.raises(SystemExit, match="fail-closed"):
            verify(root)
    manifest(root)
    (root / "x.py").write_text("a = 2\n", encoding="utf-8")                              # 코드 한 줄이 바뀜
    with pytest.raises(SystemExit, match="x.py"):
        verify(root)
    (root / "x.py").write_text("a = 1\n", encoding="utf-8")
    (root / nc.PREREG_DOC).write_text("prereg v1 edited\n", encoding="utf-8")            # 사전등록 문서가 바뀜
    with pytest.raises(SystemExit, match="prereg_changed"):
        verify(root)
    (root / nc.PREREG_DOC).write_text("prereg v1\n", encoding="utf-8")
    (root / fz.DESIGN_FREEZE).write_text("{}", encoding="utf-8")                          # 설계 동결 기록이 바뀜
    with pytest.raises(SystemExit, match="design_freeze_reference_mismatch"):
        verify(root)
    with pytest.raises(SystemExit, match="fail-closed"):
        fz.verify_implementation_freeze("missing.json", root, required=("x.py",), check_phase2=False)


def test_prereg_constant_none_blocks_even_a_perfectly_consistent_manifest(root, monkeypatch):
    manifest(root)
    monkeypatch.setattr(nc, "PREREG_SHA256", None)
    with pytest.raises(SystemExit, match="prereg_not_frozen"):
        verify(root)


def test_required_files_missing_from_the_manifest_and_self_reference_are_rejected(root):
    manifest(root)
    with pytest.raises(SystemExit, match="required_files_not_frozen"):
        fz.verify_implementation_freeze("m.json", root, required=("x.py", "y.py"), check_phase2=False)
    manifest(root, files={"x.py": fz.text_sha256(root / "x.py"), "m.json": "0" * 64})
    with pytest.raises(SystemExit, match="self_reference"):
        verify(root)


# ---------- (3) 재사용하는 Phase 2 동결이 그대로 유효 ----------
def test_phase2_own_implementation_freeze_still_verifies_so_reused_code_is_unchanged():
    if not (nc.ROOT / pc.IMPLEMENTATION_FREEZE).is_file():
        pytest.skip("Phase 2 implementation freeze manifest 없음")
    assert pc.verify_implementation_freeze()["status"] == "verified"


def test_phase2_failure_is_reported_by_the_network_verifier(root, monkeypatch):
    manifest(root)

    def bad():
        raise SystemExit("implementation freeze fail-closed: 해시 불일치 ['experiments/x']")
    monkeypatch.setattr(pc, "verify_implementation_freeze", bad)
    with pytest.raises(SystemExit, match="phase2_implementation_freeze"):
        fz.verify_implementation_freeze("m.json", root, required=("x.py",), check_phase2=True)


# ---------- (4) 동결 목록 ----------
def test_every_network_env_file_is_on_the_freeze_list_and_the_list_points_to_real_files():
    on_disk = {p.name for p in HERE.iterdir() if p.is_file() and (p.name.startswith(("n2_", "test_n2")) or p.suffix == ".ps1")}
    assert on_disk <= set(fz.NET_CODE), sorted(on_disk - set(fz.NET_CODE))                # 새 파일을 동결 목록에 넣는 것을 잊으면 여기서 걸린다
    for rel in fz.FREEZE_REQUIRED:
        if rel in fz.PRODUCED_AT_FREEZE:
            continue
        assert (nc.ROOT / rel).is_file(), rel
    assert set(pc.FREEZE_REQUIRED) <= set(fz.FREEZE_REQUIRED) and nc.NET_CALIBRATION_REL in fz.FREEZE_REQUIRED
    assert "experiments/network_degrade_adapter.py" in fz.FREEZE_REQUIRED and nc.PREREG_DOC in fz.FREEZE_REQUIRED


# ---------- (5) 작성 ----------
def test_design_freeze_requires_the_committed_blob_to_match_and_the_constant_to_be_set(root, monkeypatch):
    blob = (root / nc.PREREG_DOC).read_bytes()
    show = lambda commit, rel: blob.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")                                # 커밋 blob이 CRLF여도 LF 정규화로 같다  # noqa: E731
    monkeypatch.setattr(pc, "PREREG_DOC", nc.PREREG_DOC)
    (root / pc.IMPLEMENTATION_FREEZE).parent.mkdir(parents=True, exist_ok=True)
    (root / pc.IMPLEMENTATION_FREEZE).write_text("{}", encoding="utf-8")
    d = fz.build_design_freeze("abc123", root, show=show)
    assert d["status"] == "design_frozen" and d["prereg_sha256"] == nc.PREREG_SHA256 and d["prereg_commit"] == "abc123"
    assert d["candidates"].keys() == set(nc.DETECTOR_IDS) and d["counts"]["normal"] == 4 and d["counts"]["pressure"] == 5
    assert d["net_probe"]["calibration_sha256"] == nc.NET_CALIBRATION_SHA256 and d["supersedes"] is None
    with pytest.raises(SystemExit, match="blob과 다름"):
        fz.build_design_freeze("abc123", root, show=lambda c, r: b"something else\n")
    monkeypatch.setattr(nc, "PREREG_SHA256", "0" * 64)
    with pytest.raises(SystemExit, match="PREREG_SHA256"):
        fz.build_design_freeze("abc123", root, show=show)


def test_implementation_freeze_is_refused_unless_all_tests_pass_and_written_once(root, tmp_path, monkeypatch):
    monkeypatch.setattr(fz, "FREEZE_REQUIRED", ("x.py", fz.SELECTION_PROFILE_REL))
    (root / fz.SELECTION_PROFILE_REL).parent.mkdir(parents=True, exist_ok=True)
    (root / fz.SELECTION_PROFILE_REL).write_text("{}", encoding="utf-8")
    review = root / "review.json"
    review.write_text(json.dumps({"codex_rounds": 3}), encoding="utf-8")

    def results(**over):
        r = {"network": {"passed": 200, "failed": 0}, "phase2": {"passed": 106, "failed": 0, "skipped": 1}, "new_regressions": [], **over}
        p = root / "tests.json"
        p.write_text(json.dumps(r), encoding="utf-8")
        return p
    files_fn = lambda r: ["x.py", fz.SELECTION_PROFILE_REL]                                  # noqa: E731
    for bad in ({"network": {"passed": 1, "failed": 1}}, {"phase2": {"passed": 1, "failed": 3}}, {"new_regressions": ["t"]}):
        with pytest.raises(SystemExit, match="거부"):
            fz.build_implementation_freeze(results(**bad), review, "c0ffee", root, files_fn=files_fn)
    m = fz.build_implementation_freeze(results(), review, "c0ffee", root, files_fn=files_fn)
    assert m["status"] == "implementation_frozen" and m["tests"]["network_failed"] == 0 and m["tests"]["new_regressions"] == 0
    assert m["files"]["x.py"] == fz.text_sha256(root / "x.py") and m["git_commit"] == "c0ffee" and m["reviews"] == {"codex_rounds": 3}
    out = fz.write_new(root / "impl.json", m)
    with pytest.raises(FileExistsError):
        fz.write_new(out, m)                                                                  # 덮어쓰기 금지
    monkeypatch.setattr(fz, "FREEZE_REQUIRED", ("x.py", "missing.py"))
    with pytest.raises(SystemExit, match="필수 파일"):
        fz.build_implementation_freeze(results(), review, "c0ffee", root, files_fn=files_fn)
