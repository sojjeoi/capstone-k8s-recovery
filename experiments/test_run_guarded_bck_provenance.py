"""run_guarded_bck v1.2 실행 provenance fail-closed 테스트. 단위(verify_provenance)는 tmp 저장소 사본으로, CLI는
guarded_pilot_session.ps1과 같은 인자 형식의 subprocess로 확인한다. 모두 클러스터 접속 전 단계. 실제 결과 폴더를 만들지 않는다."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import run_guarded_bck as gb

EXP = Path(__file__).resolve().parent
EXTRA = ("experiments/guarded_bck_analysis.py",)  # 필수 외 항목도 전부 검증되는지 확인용
DRIVER = ("import sys, pathlib; sys.path.insert(0, sys.argv.pop(1)); import run_guarded_bck as gb; "
          "root = sys.argv.pop(1); gb.REPO_ROOT = pathlib.Path(root) if root != '-' else gb.REPO_ROOT; "
          "gb.BCK_RESULTS_DIR = pathlib.Path(sys.argv.pop(1)); sys.exit(gb.main(sys.argv[1:]))")


def _sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _repo(tmp, *, manifest=None, drop=(), extra=EXTRA):
    """필수 파일 사본(v1.2 개정 문서는 아직 없으면 테스트용 내용) + manifest를 tmp에 만든다."""
    root = tmp / "repo"
    for rel in [*gb.PROVENANCE_REQUIRED.values(), *extra]:
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        src = gb.REPO_ROOT / rel
        if src.is_file():
            shutil.copy(src, dst)
        else:
            dst.write_text(f"test placeholder {rel}\n", encoding="utf-8")
    files = {rel: _sha(root / rel) for rel in [*gb.PROVENANCE_REQUIRED.values(), *extra] if rel not in drop}
    m = manifest if manifest is not None else {"status": "frozen", "version": "v1.2", "files": files}
    (root / gb.FREEZE_MANIFEST).parent.mkdir(parents=True, exist_ok=True)
    (root / gb.FREEZE_MANIFEST).write_text(m if isinstance(m, str) else json.dumps(m), encoding="utf-8")
    return root


# ---------------- 정상 ----------------
def test_all_hashes_match_records_exact_provenance(tmp_path):
    root = _repo(tmp_path)
    p = gb.verify_provenance(root=root)
    assert p["verification_status"] == "verified" and p["freeze_version"] == "v1.2"
    assert p["freeze_manifest_path"] == gb.FREEZE_MANIFEST and p["freeze_manifest_sha256"] == _sha(root / gb.FREEZE_MANIFEST)
    for k, rel in gb.PROVENANCE_REQUIRED.items():
        assert p[f"{k}_path"] == rel and p[f"{k}_sha256"] == _sha(root / rel)
    assert p["runner_sha256"] == _sha(gb.REPO_ROOT / "experiments/run_guarded_bck.py")  # 사본 = 실제 runner
    assert set(p["verified_files"]) == {*gb.PROVENANCE_REQUIRED.values(), *EXTRA}


def test_manifest_hash_is_not_hardcoded_in_runner():
    src = (EXP / "run_guarded_bck.py").read_text(encoding="utf-8")
    assert gb.FREEZE_MANIFEST in src and "freeze_manifest_sha256" in src
    real = gb.REPO_ROOT / gb.FREEZE_MANIFEST
    assert not real.exists() or _sha(real) not in src


# ---------------- fail-closed ----------------
@pytest.mark.parametrize("key", sorted(gb.PROVENANCE_REQUIRED))
def test_each_required_artifact_mismatch_fails_closed(tmp_path, key):
    root = _repo(tmp_path)
    with open(root / gb.PROVENANCE_REQUIRED[key], "a", encoding="utf-8") as fh:
        fh.write("\n# changed\n")
    with pytest.raises(SystemExit, match="해시 불일치.*" + gb.PROVENANCE_REQUIRED[key].split("/")[-1]):
        gb.verify_provenance(root=root)


def test_extra_listed_artifact_mismatch_or_missing_fails_closed(tmp_path):
    root = _repo(tmp_path)
    (root / EXTRA[0]).unlink()
    with pytest.raises(SystemExit, match="해시 불일치"):
        gb.verify_provenance(root=root)


@pytest.mark.parametrize("key", sorted(gb.PROVENANCE_REQUIRED))
def test_required_entry_missing_fails_closed(tmp_path, key):
    root = _repo(tmp_path, drop=(gb.PROVENANCE_REQUIRED[key],))
    with pytest.raises(SystemExit, match=f"필수 항목 누락 \\['{key}'\\]"):
        gb.verify_provenance(root=root)


def test_manifest_missing_fails_closed(tmp_path):
    root = _repo(tmp_path)
    (root / gb.FREEZE_MANIFEST).unlink()
    with pytest.raises(SystemExit, match="manifest 읽기·형식 오류"):
        gb.verify_provenance(root=root)


@pytest.mark.parametrize("manifest", ["{not json", "[]", {"status": "frozen", "version": "v1.2"},
                                      {"status": "draft", "version": "v1.2", "files": {}},
                                      {"status": "frozen", "version": "v1.1", "files": {}},
                                      {"status": "frozen", "version": "v1.2", "files": []}])
def test_manifest_malformed_fails_closed(tmp_path, manifest):
    root = _repo(tmp_path, manifest=manifest)
    with pytest.raises(SystemExit, match="manifest 읽기·형식 오류"):
        gb.verify_provenance(root=root)


def test_manifest_self_reference_fails_closed(tmp_path):
    root = _repo(tmp_path)
    m = json.loads((root / gb.FREEZE_MANIFEST).read_text(encoding="utf-8"))
    m["files"][gb.FREEZE_MANIFEST] = "0" * 64
    (root / gb.FREEZE_MANIFEST).write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(SystemExit, match="provenance fail-closed"):
        gb.verify_provenance(root=root)


# ---------------- 실제 CLI 경로(ps1 형식) ----------------
def _cli(tmp, root, extra=(), token=False, run="bc90c"):
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    env.pop("GUARDED_ADMIN_TOKEN", None)
    if token:
        env["GUARDED_ADMIN_TOKEN"] = "test-token-not-real"
    (tmp / "clock.json").write_text("{}", encoding="utf-8")
    (tmp / "approval.json").write_text(json.dumps({
        "status": "final", "timing_source": "clock-measurement-test", "clock_measurement_file": "clock.json",
        "timing": {"max_signal_age_sec": 30.0, "max_future_skew_sec": 5.0, "max_window_to_decision_sec": 30.0},
        "service_wait_deadline_sec": 80.0, "approved_by": "test", "approved_at_utc": "2026-10-08T00:00:00+00:00",
        "runs": [run]}), encoding="utf-8")
    args = ["--run", run, "--mode", "act", "--policy-url", "http://127.0.0.1:18090",
            "--session-file", str(tmp / "session.json"), "--act-approval", str(tmp / "approval.json"), *extra]
    return subprocess.run([sys.executable, "-c", DRIVER, str(EXP), str(root), str(tmp / "results"), *args], cwd=EXP,
                          env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)


@pytest.fixture(autouse=True)
def _real_results_untouched():
    before = gb.BCK_RESULTS_DIR.exists()
    yield
    assert gb.BCK_RESULTS_DIR.exists() == before


@pytest.mark.parametrize("token", [False, True])
def test_cli_provenance_failure_stops_before_runner_and_cluster(tmp_path, token):
    root = _repo(tmp_path)
    with open(root / gb.PROVENANCE_REQUIRED["detector"], "a", encoding="utf-8") as fh:
        fh.write("\n# changed\n")
    r = _cli(tmp_path, root, token=token)
    assert r.returncode == 1 and "provenance fail-closed" in r.stderr, r.stderr[-600:]
    assert not (tmp_path / "results").exists()  # run_bck(결과 폴더 생성)·real_deps 전에 멈춤


def test_cli_verified_provenance_is_recorded_in_contract(tmp_path):
    root = _repo(tmp_path)
    r = _cli(tmp_path, root, extra=["--dry-run-contract-only"], run="bc90b")  # 순서상 bc90 첫 run
    assert r.returncode == 0, r.stderr[-600:]
    c = json.loads((tmp_path / "results" / f"contract-{gb.run_id_for('bc90b')}.json").read_text(encoding="utf-8"))
    p = c["code_provenance"]
    assert p["verification_status"] == "verified" and p["freeze_manifest_sha256"] == _sha(root / gb.FREEZE_MANIFEST)
    assert p["base_contract_path"] == gb.BCK_CONTRACT_DOC == c["contract_doc"] and c["arm"] == "B"


REAL = gb.REPO_ROOT / gb.FREEZE_MANIFEST


@pytest.mark.skipif(not REAL.exists(), reason="v1.2 manifest 동결 전")
def test_cli_real_v12_manifest_verifies_current_tree(tmp_path):
    r = _cli(tmp_path, "-", extra=["--dry-run-contract-only"], run="bc90b")
    assert r.returncode == 0, r.stderr[-600:]
    c = json.loads((tmp_path / "results" / f"contract-{gb.run_id_for('bc90b')}.json").read_text(encoding="utf-8"))
    p = c["code_provenance"]
    assert p["freeze_manifest_sha256"] == _sha(REAL) and p["runner_sha256"] == _sha(EXP / "run_guarded_bck.py")
    assert p["v1_2_amendment_sha256"] == _sha(gb.REPO_ROOT / gb.PROVENANCE_REQUIRED["v1_2_amendment"])
