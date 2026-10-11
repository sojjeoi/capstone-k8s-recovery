#!/usr/bin/env python3
"""Phase 2 IMPLEMENTATION FREEZE manifest 작성(prereg §18-B, 2026-10-11) - 오프라인, 1회.

동결 대상 = Phase 2 코드·테스트·runbook·selection profile + 실행 경로가 import·호출하는 기존 코드·계약 파일(git 추적 파일만).
해시는 LF 정규화(p2_common.text_sha256 - 커밋 blob과 같은 값). §11 테스트 100%·새 regression 0이 기록돼 있어야만 쓴다.
실행기(run_stage1·run_stage2·selection)는 시작 전 p2_common.verify_implementation_freeze로 이 manifest를 다시 해시한다."""
import sys
sys.dont_write_bytecode = True
import argparse  # noqa: E402
import json  # noqa: E402
import platform  # noqa: E402
import subprocess  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import p2_common as pc  # noqa: E402

DESIGN_FREEZE_COMMIT = "4a867eb762ebc78b37f5295baa9d2a48e2973027"   # rev 6 교체 manifest 커밋(rev 5 6019341을 supersede)
TRACKED = ["experiments/phase2_detector_selection/*.py", "experiments/phase2_detector_selection/*.md",
           "experiments/phase2_detector_selection/*.txt", "experiments/phase2_detector_selection/*.json",
           "experiments/phase2_detector_selection/profiles/*.json", pc.PREREG_DOC, pc.DESIGN_FREEZE, *pc.SUPERSEDED,
           "experiments/*.py", "experiments/guarded_pilot_session.ps1", "experiments/c3_normal_coverage/*.py",
           "anomaly-detection/*.py", "anomaly-detection/v3/model_v31/*.py", pc.RULE_SPEC_REL,
           "anomaly-detection/v3/model_c3_normal_coverage/artifacts/*.json", "recovery-policy/*.py",
           "chaos/probe-config.yaml", "gitops/apps/vllm-serving/rollout.yaml"]


def tracked_files(root=pc.ROOT) -> list:
    out = subprocess.run(["git", "ls-files", "--", *TRACKED], cwd=root, capture_output=True, text=True, check=True).stdout
    return sorted(f for f in out.splitlines() if f and f != pc.IMPLEMENTATION_FREEZE)


def build(test_results: Path, review_summary: Path, code_commit: str, root=pc.ROOT) -> dict:
    tr = json.loads(Path(test_results).read_text(encoding="utf-8"))
    if tr.get("phase2", {}).get("failed") != 0 or tr.get("new_regressions") != []:
        raise SystemExit(f"IMPLEMENTATION FREEZE 거부: §11 100%·새 regression 0 아님 - {tr.get('phase2')} {tr.get('new_regressions')}")
    files = {f: pc.text_sha256(Path(root) / f) for f in tracked_files(root)}
    missing = sorted(set(pc.FREEZE_REQUIRED) - set(files))
    if missing:
        raise SystemExit(f"IMPLEMENTATION FREEZE 거부: 필수 파일이 git 추적 대상이 아님 {missing}")
    dirty = subprocess.run(["git", "status", "--porcelain", "--", *files], cwd=root, capture_output=True, text=True).stdout
    if dirty.strip():
        raise SystemExit(f"IMPLEMENTATION FREEZE 거부: 커밋되지 않은 변경 {dirty.splitlines()[:5]}")
    return {"status": "implementation_frozen", "version": "phase2-impl-v1", "plan_id": pc.PLAN_ID,
            "design_freeze": {"manifest_path": pc.DESIGN_FREEZE, "manifest_sha256": pc.text_sha256(Path(root) / pc.DESIGN_FREEZE),
                              "prereg_path": pc.PREREG_DOC, "prereg_sha256": pc.PREREG_SHA256, "commit": DESIGN_FREEZE_COMMIT},
            "git_commit": code_commit, "frozen_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "hash_basis": "LF-normalized file content (= committed git blob)", "files": files,
            "selection_profile": pc.SELECTION_PROFILE.relative_to(root).as_posix(),
            "selection_profile_sha256": pc.text_sha256(pc.SELECTION_PROFILE),
            "external_artifact_expected": pc.FROZEN_DETECTORS[pc.IF_ID]["artifact"],
            "cpu_rule_expected": pc.FROZEN_DETECTORS[pc.CPU_ID]["artifact"],
            "environment_requirements": {"python": platform.python_version(), "sklearn": pc.SKLEARN_VERSION,
                                         "artifact_root": "PHASE2_ARTIFACT_ROOT 또는 --artifact-root(명시 필수, 기본 경로 없음)",
                                         "checkout": "이 git_commit의 followup-detector-selection worktree에서만 실행"},
            "tests": {"phase2_passed": tr["phase2"]["passed"], "phase2_failed": tr["phase2"]["failed"],
                      "phase2_skipped": tr["phase2"]["skipped"], "new_regressions": len(tr["new_regressions"]),
                      "results_file": Path(test_results).resolve().relative_to(Path(root).resolve()).as_posix(),
                      "results_sha256": pc.text_sha256(test_results)},
            "reviews": json.loads(Path(review_summary).read_text(encoding="utf-8"))}


def main(argv=None) -> int:  # pragma: no cover - 1회 파일 쓰기
    p = argparse.ArgumentParser(description="Phase 2 IMPLEMENTATION FREEZE manifest 작성(오프라인)")
    p.add_argument("--test-results", required=True, type=Path)
    p.add_argument("--review-summary", required=True, type=Path)
    p.add_argument("--code-commit", required=True)
    a = p.parse_args(argv)
    m = build(a.test_results, a.review_summary, a.code_commit)
    out = pc.ROOT / pc.IMPLEMENTATION_FREEZE
    with open(out, "x", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(m, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"manifest": pc.IMPLEMENTATION_FREEZE, "files": len(m["files"]), "sha256": pc.text_sha256(out)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
