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


# ---------- (6) selection profile: 가상 tolerant 템플릿에서 ----------
def test_virtual_tolerant_template_is_the_base_rollout_plus_only_the_two_probe_timeouts():
    import copy
    import yaml
    base = [d for d in yaml.safe_load_all((nc.ROOT / "gitops/apps/vllm-serving/rollout.yaml").read_text(encoding="utf-8")) if d and d.get("kind") == "Rollout"][0]
    before = copy.deepcopy(base["spec"]["template"])
    virt = fz.virtual_tolerant_template()
    c, b = virt["spec"]["containers"][0], before["spec"]["containers"][0]
    assert c["readinessProbe"]["timeoutSeconds"] == c["livenessProbe"]["timeoutSeconds"] == nc.PROBE_TIMEOUT_SEC
    assert "timeoutSeconds" not in b["readinessProbe"] and "timeoutSeconds" not in b["livenessProbe"]       # 기본 Rollout에는 없다
    c["readinessProbe"].pop("timeoutSeconds")
    c["livenessProbe"].pop("timeoutSeconds")
    assert virt == before                                                                  # 그 두 값 외에는 기본 Rollout과 같다
    assert fz.virtual_tolerant_template() != before                                         # 호출마다 새 복사본(원본을 바꾸지 않음)


def test_selection_profile_from_virtual_template_matches_tolerant_observation_and_flags_the_base_as_stale(monkeypatch):
    import yaml
    monkeypatch.setattr(nc, "PREREG_SHA256", "f" * 64)
    prof = fz.build_selection_profile_obj()
    sha = nc.contract_hashes()["recovery_policy_sha256"]
    assert pc.check_stale(prof, nc.observe_environment(fz.virtual_tolerant_template()), sha) == []        # 11 s 프로필을 적용하면 세션 시작 검사가 통과한다
    base = [d for d in yaml.safe_load_all((nc.ROOT / "gitops/apps/vllm-serving/rollout.yaml").read_text(encoding="utf-8")) if d and d.get("kind") == "Rollout"][0]
    assert "environment.probe_profile" in pc.check_stale(prof, nc.observe_environment(base["spec"]["template"]), sha)   # 적용 전(기본)에는 막힌다
    assert [d["role"] for d in prof["detectors"]] == [nc.SHADOW] * 3 and {d["detector_id"] for d in prof["detectors"]} == set(nc.DETECTOR_IDS)
    monkeypatch.setattr(nc, "PREREG_SHA256", None)
    with pytest.raises(SystemExit, match="설계 동결 전"):
        fz.build_selection_profile_obj()


# ---------- (7) 시험 결과 모으기 ----------
OK_OUT = "........\n273 passed in 89.69s (0:01:29)\n"
BAD_OUT = ("FAILED recovery-policy/test_a.py::test_one - AssertionError: x\nFAILED anomaly-detection/test_b.py::test_two - boom\n"
           "ERROR anomaly-detection/test_c.py - ImportError\n== 2 failed, 5 passed, 1 skipped, 1 error in 3.20s ==\n")


def test_parse_pytest_output_reads_counts_and_failed_ids():
    assert fz.parse_pytest_output(OK_OUT) == {"passed": 273, "failed": 0, "skipped": 0, "errors": 0, "failed_ids": []}
    r = fz.parse_pytest_output(BAD_OUT)
    assert (r["passed"], r["failed"], r["skipped"], r["errors"]) == (5, 2, 1, 1)
    assert r["failed_ids"] == ["anomaly-detection/test_b.py::test_two", "anomaly-detection/test_c.py", "recovery-policy/test_a.py::test_one"]
    assert fz.parse_pytest_output("106 passed, 1 skipped in 13.5s")["skipped"] == 1


def test_known_baseline_includes_listed_failures_and_the_documented_flaky_tests():
    ids = fz.known_baseline_ids()
    assert "anomaly-detection/test_c1_bck_shadow_pair.py::test_spec_mismatch_fails_closed" in ids
    assert "recovery-policy/test_git_client.py::test_restart_requeues_pending" in ids                      # 주석에 적힌 flaky


def test_collect_test_results_counts_only_failures_outside_the_known_baseline_as_regressions():
    calls = []

    def fake_run(targets):
        calls.append(tuple(targets))
        return {fz.NET: OK_OUT, "experiments/phase2_detector_selection": "106 passed, 1 skipped in 13s\n"}.get(targets[0], BAD_OUT)
    res = fz.collect_test_results(run=fake_run, baseline_ids={"anomaly-detection/test_c.py", "anomaly-detection/test_b.py::test_two"})
    assert calls == [(fz.NET,), ("experiments/phase2_detector_selection",), ("recovery-policy", "anomaly-detection")]
    assert (res["network"]["passed"], res["network"]["failed"]) == (273, 0) and (res["phase2"]["passed"], res["phase2"]["skipped"]) == (106, 1)
    assert res["new_regressions"] == ["recovery-policy/test_a.py::test_one"]                              # 기준선에 없는 실패만
    assert res["existing"]["failed_in_baseline"] == 2
    all_baseline = fz.collect_test_results(run=fake_run, baseline_ids={"anomaly-detection/test_c.py", "anomaly-detection/test_b.py::test_two",
                                                                       "recovery-policy/test_a.py::test_one"})
    assert all_baseline["new_regressions"] == []


def test_write_new_creates_missing_parent_and_never_overwrites(tmp_path):
    out = fz.write_new(tmp_path / "profiles" / "p.json", {"a": 1})
    assert json.loads(out.read_text(encoding="utf-8")) == {"a": 1}
    with pytest.raises(FileExistsError):
        fz.write_new(out, {"a": 2})


def test_real_tracked_file_list_covers_every_required_file_except_the_two_produced_by_the_freeze():
    got = set(fz.tracked_files())
    assert fz.IMPLEMENTATION_FREEZE not in got                                                     # 자기 자신은 해시하지 않는다
    missing = set(fz.FREEZE_REQUIRED) - got - {fz.IMPLEMENTATION_FREEZE}
    assert missing <= {fz.DESIGN_FREEZE, fz.SELECTION_PROFILE_REL}                                 # 이 둘은 동결 절차가 만든 뒤 추적된다
    assert fz.pc.IMPLEMENTATION_FREEZE in got                                                      # Phase 2 구현 동결 기록도 해시 대상
