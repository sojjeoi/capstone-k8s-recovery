#!/usr/bin/env python3
"""네트워크 환경 DESIGN FREEZE·IMPLEMENTATION FREEZE 작성·검증(2026-10-17, M5, DEVELOPMENT - **동결 전**, 동결을 실제로 하는 것은 사용자 승인 뒤).

Phase 2(prereg §18)의 2단계 freeze를 그대로 따른다. 달라지는 점:
  - 동결 대상에 네트워크 환경 코드·시험·원복 스크립트·selection profile·net_probe 보정 근거·지연 주입 경로(어댑터)가 들어간다.
  - 재사용하는 Phase 2 코드가 바뀌지 않았다는 것을 **Phase 2 자신의 implementation freeze**를 다시 검증해 확인한다(verify의 check_phase2).
  - 사전등록 해시(n2_common.PREREG_SHA256)가 None이면 어떤 manifest도 검증을 통과하지 못한다(fail-closed).
쓰는 파일은 모두 한 번만 쓴다(덮어쓰기 금지). 해시는 LF 정규화(= 커밋 blob과 같은 값)."""
import sys
sys.dont_write_bytecode = True
import argparse  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import platform  # noqa: E402
import subprocess  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import n2_common as nc  # noqa: E402

pc = nc.pc
NET = "experiments/phase2_network_env"
DESIGN_FREEZE = "docs/design/phase2-network-env-design-freeze.json"
IMPLEMENTATION_FREEZE = f"{NET}/phase2-network-env-implementation-freeze-r1.json"          # n2_stage1.NetConfig.implementation_freeze와 같다
SELECTION_PROFILE_REL = f"{NET}/profiles/selection-profile-network-tolerant-vllm-qwen05b-v1.json"
NET_CODE = ("n2_common.py", "n2_selection.py", "n2_executor.py", "n2_pressure.py", "n2_cleanup.py", "n2_preflight.py", "n2_session.py",
            "n2_stage1.py", "n2_stage2.py", "n2_select_offline.py", "n2_run.py", "n2_freeze.py", "restore-demo-state.ps1",
            "test_n2.py", "test_n2_executor.py", "test_n2_pressure.py", "test_n2_cleanup.py", "test_n2_preflight.py", "test_n2_session.py",
            "test_n2_stage1.py", "test_n2_stage2.py", "test_n2_select_offline.py", "test_n2_run.py", "test_n2_restore_script.py", "test_n2_prereg_doc.py",
            "test_n2_freeze.py")
# 동결 전에는 없는 파일(동결 절차가 만든다) - 존재 검사에서만 제외한다
PRODUCED_AT_FREEZE = (SELECTION_PROFILE_REL, DESIGN_FREEZE, IMPLEMENTATION_FREEZE)
INJECTION_PATH = ("experiments/network_degrade_adapter.py", "experiments/arm_controller.py", "experiments/node_resource_safety_gate.py",
                  "experiments/run_detector_switch_ab_trial.py", "experiments/blue_green_prep.py",
                  "gitops/apps/vllm-serving/rollout.yaml", "gitops/apps/vllm-serving/overlays/network-tolerant/kustomization.yaml",
                  "gitops/apps/vllm-serving/overlays/network-tolerant/probe-timeout-patch.yaml", "chaos/probe-config.yaml")
FREEZE_REQUIRED = tuple(dict.fromkeys(
    (*[f"{NET}/{f}" for f in NET_CODE], SELECTION_PROFILE_REL, nc.PREREG_DOC, DESIGN_FREEZE, nc.NET_CALIBRATION_REL, *INJECTION_PATH,
     pc.IMPLEMENTATION_FREEZE, pc.PREREG_DOC, *pc.FREEZE_REQUIRED)))


def text_sha256(p) -> str:
    return pc.text_sha256(p)


def verify_implementation_freeze(manifest_rel: str = IMPLEMENTATION_FREEZE, root: Path = nc.ROOT, required=None, *,
                                 check_phase2: bool = True) -> dict:
    """실행 전 fail-closed: manifest 형식·design freeze 참조·모든 파일 LF 정규화 해시·Phase 2 자신의 freeze. 클러스터 접근 전에 부른다."""
    required = FREEZE_REQUIRED if required is None else required
    root = Path(root)
    try:
        raw = (root / manifest_rel).read_bytes()
        m = json.loads(raw)
        files = m["files"]
        if m.get("status") != "implementation_frozen" or not isinstance(files, dict) or not files:
            raise ValueError(f"status={m.get('status')}")
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SystemExit(f"implementation freeze fail-closed: manifest 읽기·형식 오류: {type(e).__name__}: {e}") from e
    probs = []
    if nc.PREREG_SHA256 is None:
        probs.append("prereg_not_frozen(n2_common.PREREG_SHA256=None)")
    df = m.get("design_freeze") or {}
    if df.get("prereg_sha256") != nc.PREREG_SHA256 or df.get("manifest_path") != DESIGN_FREEZE \
            or not (root / DESIGN_FREEZE).is_file() or df.get("manifest_sha256") != text_sha256(root / DESIGN_FREEZE):
        probs.append("design_freeze_reference_mismatch")
    if not (root / nc.PREREG_DOC).is_file() or text_sha256(root / nc.PREREG_DOC) != nc.PREREG_SHA256:
        probs.append("prereg_changed")
    t = m.get("tests") or {}
    if t.get("network_failed") != 0 or t.get("phase2_failed") != 0 or t.get("new_regressions") != 0:
        probs.append("tests_not_100_percent")
    bad = sorted(rel for rel, want in files.items() if not (root / rel).is_file() or text_sha256(root / rel) != want)
    if manifest_rel in files:
        probs.append("self_reference")
    missing = sorted(set(required) - set(files))
    if missing:
        probs.append(f"required_files_not_frozen: {missing}")
    if check_phase2:
        try:
            pc.verify_implementation_freeze()                   # 재사용하는 Phase 2 코드가 Phase 2 동결 때와 같은지
        except SystemExit as e:
            probs.append(f"phase2_implementation_freeze: {e}")
    if bad or probs:
        raise SystemExit(f"implementation freeze fail-closed: 해시 불일치 {bad}, {probs}")
    return {"manifest": manifest_rel, "manifest_sha256": hashlib.sha256(raw).hexdigest(), "verified_files": len(files),
            "selection_profile_sha256": m.get("selection_profile_sha256"), "status": "verified"}


# ---------------- 작성 (동결 시점에 한 번, 사용자 승인 뒤) ----------------
def tracked_files(root=nc.ROOT) -> list:
    import write_implementation_freeze as p2w                  # Phase 2 작성기의 추적 패턴(읽기만) + 네트워크 환경 패턴
    patterns = [*p2w.TRACKED, f"{NET}/*.py", f"{NET}/*.ps1", f"{NET}/profiles/*.json", nc.PREREG_DOC, nc.NET_CALIBRATION_REL,
                "gitops/apps/vllm-serving/overlays/network-tolerant/*.yaml"]
    out = subprocess.run(["git", "ls-files", "--", *patterns], cwd=root, capture_output=True, text=True, check=True).stdout
    return sorted(f for f in out.splitlines() if f and f not in (IMPLEMENTATION_FREEZE, pc.IMPLEMENTATION_FREEZE))


def git_show(commit: str, rel: str, root=nc.ROOT) -> bytes:
    return subprocess.run(["git", "show", f"{commit}:{rel}"], cwd=root, capture_output=True, check=True).stdout


def blob_sha256(data: bytes) -> str:
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def build_design_freeze(prereg_commit: str, root=nc.ROOT, show=git_show) -> dict:
    """사전등록 문서를 커밋한 뒤, 그 커밋을 가리키는 설계 동결 기록. 작업 폴더의 문서가 그 커밋의 blob과 같아야 한다."""
    root = Path(root)
    on_disk = text_sha256(root / nc.PREREG_DOC)
    if blob_sha256(show(prereg_commit, nc.PREREG_DOC)) != on_disk:
        raise SystemExit("DESIGN FREEZE 거부: 작업 폴더의 사전등록 문서가 지정한 커밋의 blob과 다름(먼저 커밋)")
    if nc.PREREG_SHA256 != on_disk:
        raise SystemExit(f"DESIGN FREEZE 거부: n2_common.PREREG_SHA256({nc.PREREG_SHA256})가 문서 해시({on_disk})와 다름")
    return {"status": "design_frozen", "version": "phase2-network-env-design-v1", "plan_id": "capstone-phase2-network-env-v1",
            "prereg_path": nc.PREREG_DOC, "prereg_sha256": on_disk, "prereg_commit": prereg_commit,
            "hash_basis": "LF-normalized file content (= committed git blob)",
            "frozen_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "candidates": {d: nc.FROZEN_DETECTORS[d]["threshold"] for d in nc.DETECTOR_IDS},
            "net_probe": {"definition_sha256": nc.NET_DEFINITION_SHA256, "calibration_sha256": nc.NET_CALIBRATION_SHA256,
                          "threshold": nc.NET_THRESHOLD, "calibration_evidence": nc.NET_CALIBRATION_REL},
            "counts": {"normal": nc.N_NORMAL, "pressure": nc.N_PRESSURE, "stage2": nc.N_STAGE2, "max_attempts": dict(nc.MAX_ATTEMPTS)},
            "stages": [dict(s) for s in nc.NETWORK_STAGES], "probe_profile": dict(nc.PROBE_PROFILE),
            "phase2_not_modified": {"prereg_path": pc.PREREG_DOC, "prereg_sha256": text_sha256(root / pc.PREREG_DOC),
                                    "implementation_freeze": pc.IMPLEMENTATION_FREEZE,
                                    "implementation_freeze_sha256": text_sha256(root / pc.IMPLEMENTATION_FREEZE)},
            "supersedes": None}


def build_implementation_freeze(test_results: Path, review_summary: Path, code_commit: str, root=nc.ROOT, files_fn=tracked_files) -> dict:
    root = Path(root)
    tr = json.loads(Path(test_results).read_text(encoding="utf-8"))
    if (tr.get("network") or {}).get("failed") != 0 or (tr.get("phase2") or {}).get("failed") != 0 or tr.get("new_regressions") != []:
        raise SystemExit(f"IMPLEMENTATION FREEZE 거부: 시험 100%·새 regression 0 아님 - {tr.get('network')} {tr.get('phase2')} {tr.get('new_regressions')}")
    files = {f: text_sha256(root / f) for f in files_fn(root)}
    missing = sorted(set(FREEZE_REQUIRED) - set(files))
    if missing:
        raise SystemExit(f"IMPLEMENTATION FREEZE 거부: 필수 파일이 git 추적 대상이 아님 {missing}")
    dirty = subprocess.run(["git", "status", "--porcelain", "--", *files], cwd=root, capture_output=True, text=True).stdout
    if dirty.strip():
        raise SystemExit(f"IMPLEMENTATION FREEZE 거부: 커밋되지 않은 변경 {dirty.splitlines()[:5]}")
    return {"status": "implementation_frozen", "version": "phase2-network-env-impl-r1", "plan_id": "capstone-phase2-network-env-v1",
            "design_freeze": {"manifest_path": DESIGN_FREEZE, "manifest_sha256": text_sha256(root / DESIGN_FREEZE),
                              "prereg_path": nc.PREREG_DOC, "prereg_sha256": nc.PREREG_SHA256},
            "git_commit": code_commit, "frozen_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "hash_basis": "LF-normalized file content (= committed git blob)", "files": files,
            "selection_profile": SELECTION_PROFILE_REL, "selection_profile_sha256": text_sha256(root / SELECTION_PROFILE_REL),
            "external_artifact_expected": pc.FROZEN_DETECTORS[pc.IF_ID]["artifact"], "cpu_rule_expected": pc.FROZEN_DETECTORS[pc.CPU_ID]["artifact"],
            "net_probe_expected": {"definition_sha256": nc.NET_DEFINITION_SHA256, "calibration_sha256": nc.NET_CALIBRATION_SHA256},
            "environment_requirements": {"python": platform.python_version(), "sklearn": pc.SKLEARN_VERSION,
                                         "artifact_root": "PHASE2_ARTIFACT_ROOT 또는 --artifact-root(명시 필수, 기본 경로 없음)",
                                         "checkout": "이 git_commit의 worktree에서만 실행"},
            "tests": {"network_passed": tr["network"]["passed"], "network_failed": tr["network"]["failed"],
                      "network_skipped": tr["network"].get("skipped", 0), "phase2_passed": tr["phase2"]["passed"],
                      "phase2_failed": tr["phase2"]["failed"], "phase2_skipped": tr["phase2"].get("skipped", 0),
                      "new_regressions": len(tr["new_regressions"]),
                      "results_file": Path(test_results).resolve().relative_to(root.resolve()).as_posix(),
                      "results_sha256": text_sha256(test_results)},
            "reviews": json.loads(Path(review_summary).read_text(encoding="utf-8"))}


def write_new(path: Path, obj: dict) -> Path:
    with open(path, "x", encoding="utf-8", newline="\n") as fh:      # 덮어쓰기 금지
        fh.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    return path


def main(argv=None) -> int:  # pragma: no cover - 동결 시점의 파일 쓰기·검증 진입점(사용자 승인 뒤)
    p = argparse.ArgumentParser(description="네트워크 환경 freeze 작성·검증")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("design")
    s.add_argument("--prereg-commit", required=True)
    s = sub.add_parser("impl")
    s.add_argument("--test-results", required=True, type=Path)
    s.add_argument("--review-summary", required=True, type=Path)
    s.add_argument("--code-commit", required=True)
    sub.add_parser("verify")
    a = p.parse_args(argv)
    if a.cmd == "design":
        out = write_new(nc.ROOT / DESIGN_FREEZE, build_design_freeze(a.prereg_commit))
    elif a.cmd == "impl":
        out = write_new(nc.ROOT / IMPLEMENTATION_FREEZE, build_implementation_freeze(a.test_results, a.review_summary, a.code_commit))
    else:
        print(json.dumps(verify_implementation_freeze(), ensure_ascii=False))
        return 0
    print(json.dumps({"written": out.relative_to(nc.ROOT).as_posix(), "sha256": text_sha256(out)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
