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
DESIGN_FREEZE = "docs/design/phase2-network-env-design-freeze-rev5.json"
IMPLEMENTATION_FREEZE = f"{NET}/phase2-network-env-implementation-freeze-r9.json"          # n2_stage1.NetConfig.implementation_freeze와 같다
SELECTION_PROFILE_REL = f"{NET}/profiles/selection-profile-network-tolerant-vllm-qwen05b-v5.json"
NET_CODE = ("n2_common.py", "n2_selection.py", "n2_executor.py", "n2_pressure.py", "n2_cleanup.py", "n2_preflight.py", "n2_session.py",
            "n2_stage1.py", "n2_stage2.py", "n2_select_offline.py", "n2_run.py", "n2_freeze.py", "n2_resume.py", "restore-demo-state.ps1",
            "test_n2.py", "test_n2_executor.py", "test_n2_pressure.py", "test_n2_cleanup.py", "test_n2_preflight.py", "test_n2_session.py",
            "test_n2_stage1.py", "test_n2_stage2.py", "test_n2_select_offline.py", "test_n2_run.py", "test_n2_restore_script.py", "test_n2_prereg_doc.py",
            "test_n2_freeze.py", "test_n2_resume.py")
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
                "gitops/apps/vllm-serving/overlays/network-tolerant/*.yaml", DESIGN_FREEZE, pc.IMPLEMENTATION_FREEZE]
    out = subprocess.run(["git", "ls-files", "--", *patterns], cwd=root, capture_output=True, text=True, check=True).stdout
    return sorted(f for f in out.splitlines() if f and f != IMPLEMENTATION_FREEZE)


def git_show(commit: str, rel: str, root=nc.ROOT) -> bytes:
    return subprocess.run(["git", "show", f"{commit}:{rel}"], cwd=root, capture_output=True, check=True).stdout


def blob_sha256(data: bytes) -> str:
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def _supersedes(root, old, reason):
    """교체하는 이전 동결 기록(경로·해시·사유). 사유 없는 교체는 거부한다."""
    if old is None:
        return None
    if not reason:
        raise SystemExit("교체 manifest는 사유(--reason)가 필요")
    return {"manifest_path": Path(old).resolve().relative_to(Path(root).resolve()).as_posix(), "manifest_sha256": text_sha256(old), "reason": reason}


def build_design_freeze(prereg_commit: str, root=nc.ROOT, show=git_show, supersedes: Path = None, reason: str = None) -> dict:
    """사전등록 문서를 커밋한 뒤, 그 커밋을 가리키는 설계 동결 기록. 작업 폴더의 문서가 그 커밋의 blob과 같아야 한다."""
    root = Path(root)
    on_disk = text_sha256(root / nc.PREREG_DOC)
    if blob_sha256(show(prereg_commit, nc.PREREG_DOC)) != on_disk:
        raise SystemExit("DESIGN FREEZE 거부: 작업 폴더의 사전등록 문서가 지정한 커밋의 blob과 다름(먼저 커밋)")
    if nc.PREREG_SHA256 != on_disk:
        raise SystemExit(f"DESIGN FREEZE 거부: n2_common.PREREG_SHA256({nc.PREREG_SHA256})가 문서 해시({on_disk})와 다름")
    return {"status": "design_frozen", "version": "phase2-network-env-design-v1-rev5", "plan_id": "capstone-phase2-network-env-v1",
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
            "supersedes": _supersedes(root, supersedes, reason)}


def build_implementation_freeze(test_results: Path, review_summary: Path, code_commit: str, root=nc.ROOT, files_fn=tracked_files,
                                supersedes: Path = None, reason: str = None) -> dict:
    root = Path(root)
    old = _supersedes(root, supersedes, reason)
    tr =json.loads(Path(test_results).read_text(encoding="utf-8"))
    if (tr.get("network") or {}).get("failed") != 0 or (tr.get("phase2") or {}).get("failed") != 0 or tr.get("new_regressions") != []:
        raise SystemExit(f"IMPLEMENTATION FREEZE 거부: 시험 100%·새 regression 0 아님 - {tr.get('network')} {tr.get('phase2')} {tr.get('new_regressions')}")
    files = {f: text_sha256(root / f) for f in files_fn(root)}
    missing = sorted(set(FREEZE_REQUIRED) - set(files))
    if missing:
        raise SystemExit(f"IMPLEMENTATION FREEZE 거부: 필수 파일이 git 추적 대상이 아님 {missing}")
    dirty = subprocess.run(["git", "status", "--porcelain", "--", *files], cwd=root, capture_output=True, text=True).stdout
    if dirty.strip():
        raise SystemExit(f"IMPLEMENTATION FREEZE 거부: 커밋되지 않은 변경 {dirty.splitlines()[:5]}")
    return {"status": "implementation_frozen", "version": "phase2-network-env-impl-r9", "plan_id": "capstone-phase2-network-env-v1",
            "design_freeze": {"manifest_path": DESIGN_FREEZE, "manifest_sha256": text_sha256(root / DESIGN_FREEZE),
                              "prereg_path": nc.PREREG_DOC, "prereg_sha256": nc.PREREG_SHA256},
            "supersedes": old, "git_commit": code_commit, "frozen_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
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


def virtual_tolerant_template(root=nc.ROOT) -> dict:
    """기본 Rollout(rollout.yaml)의 spec.template에 network-tolerant overlay의 probe 패치를 적용한 **가상** 템플릿(클러스터·kubectl 없이).
    selection profile의 환경 정의를 live에서 11 s 프로필을 적용하기 전에 만들기 위한 것이다. 적용 뒤 실제 관측과 같아야 하며 PROFILE_STALE 검사가 그것을 확인한다."""
    import copy
    import yaml
    root = Path(root)
    docs = [d for d in yaml.safe_load_all((root / "gitops/apps/vllm-serving/rollout.yaml").read_text(encoding="utf-8")) if d]
    rollouts = [d for d in docs if d.get("kind") == "Rollout"]
    if len(rollouts) != 1:
        raise SystemExit(f"rollout.yaml에 Rollout이 정확히 1개가 아님: {len(rollouts)}")
    out = copy.deepcopy(rollouts[0])
    patch = yaml.safe_load((root / "gitops/apps/vllm-serving/overlays/network-tolerant/probe-timeout-patch.yaml").read_text(encoding="utf-8"))
    for op in patch:
        if op.get("op") != "add":
            raise SystemExit(f"지원하지 않는 패치 연산: {op.get('op')}")
        parts = [p for p in op["path"].split("/") if p]
        node = out
        for p in parts[:-1]:
            node = node[int(p)] if isinstance(node, list) else node[p]
        node[int(parts[-1]) if isinstance(node, list) else parts[-1]] = op["value"]
    return out["spec"]["template"]


def build_selection_profile_obj(root=nc.ROOT) -> dict:
    """가상 tolerant 템플릿에서 selection profile(ACTIVE 0개)을 만든다. 사전등록 해시가 정해진 뒤(동결 시점)에만 만들 수 있다."""
    if nc.PREREG_SHA256 is None:
        raise SystemExit("selection profile 생성 거부: n2_common.PREREG_SHA256=None (설계 동결 전)")
    obs = nc.observe_environment(virtual_tolerant_template(root))
    if obs["probe_profile"] != nc.PROBE_PROFILE:
        raise SystemExit(f"가상 템플릿이 network_tolerant가 아님: {obs['probe_profile']}")
    env = {k: v for k, v in obs.items() if k not in ("probe_profile", "pressure")}
    return nc.build_selection_profile(env, nc.contract_hashes()["recovery_policy_sha256"], nc.PREREG_SHA256)


# ---------------- 시험 결과 모으기 (구현 동결의 입력) ----------------
_SUMMARY = {"passed": r"(\d+) passed", "failed": r"(\d+) failed", "skipped": r"(\d+) skipped", "errors": r"(\d+) errors?"}


def parse_pytest_output(text: str) -> dict:
    """pytest -q -rfE 출력 -> {"passed","failed","skipped","errors","failed_ids"}. 마지막 요약 줄과 FAILED/ERROR 줄만 읽는다."""
    import re
    lines = [ln.strip() for ln in text.splitlines()]
    last = next((ln for ln in reversed(lines) if re.search(r"\b(passed|failed|error|errors|skipped|no tests ran)\b", ln) and " in " in ln), "")
    counts = {k: int((re.search(rx, last) or [0, 0])[1]) for k, rx in _SUMMARY.items()}
    ids = sorted({re.split(r"\s+-\s+", ln.split(None, 1)[1])[0] for ln in lines if re.match(r"(FAILED|ERROR)\s+\S", ln)})
    return {**counts, "failed_ids": ids}


def known_baseline_ids(path=None) -> set:
    """Phase 2가 기록한 기준선 실패 목록(KNOWN-BASELINE-FAILURES.txt)의 test id - 줄 형식 '결과 | id | …'과 주석 속 flaky id."""
    import re
    p = Path(path or nc.P2DIR / "KNOWN-BASELINE-FAILURES.txt")
    ids = set()
    for ln in p.read_text(encoding="utf-8").splitlines():
        if ln.startswith("#"):
            ids |= set(re.findall(r"([\w./-]+\.py::\w+)", ln))
        elif "|" in ln:
            ids.add(ln.split("|")[1].strip())
    return ids


def collect_test_results(root=nc.ROOT, run=None, baseline_ids=None) -> dict:
    """시험을 실제로 돌려 구현 동결 입력(JSON)을 만든다. 이 환경 폴더와 Phase 2 폴더는 0 실패여야 하고, 기존 recovery-policy·anomaly-detection의 실패는
    기준선 목록에 없는 것만 새 regression이다. run(targets) -> pytest 출력 문자열 (시험에서 가짜로 바꾼다)."""
    root = Path(root)

    def real_run(targets):
        cp = subprocess.run([sys.executable, "-m", "pytest", *targets, "-q", "-p", "no:cacheprovider", "-rfE", "--continue-on-collection-errors"],
                            cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
        return cp.stdout + "\n" + cp.stderr
    run = run or real_run
    net = parse_pytest_output(run([NET]))
    p2 = parse_pytest_output(run(["experiments/phase2_detector_selection"]))
    old = parse_pytest_output(run(["recovery-policy", "anomaly-detection"]))
    base = known_baseline_ids() if baseline_ids is None else set(baseline_ids)
    regress = sorted(i for i in old["failed_ids"] if i not in base)
    slim = lambda r: {k: r[k] for k in ("passed", "failed", "skipped", "errors")}                    # noqa: E731
    return {"recorded_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "network": {"suite": NET, **slim(net)},
            "phase2": {"suite": "experiments/phase2_detector_selection", **slim(p2)},
            "existing": {"suite": "recovery-policy + anomaly-detection", **slim(old), "failed_in_baseline": len(old["failed_ids"]) - len(regress)},
            "new_regressions": regress}


def write_new(path: Path, obj: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8", newline="\n") as fh:      # 덮어쓰기 금지
        fh.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    return path


def main(argv=None) -> int:  # pragma: no cover - 동결 시점의 파일 쓰기·검증 진입점(사용자 승인 뒤)
    p = argparse.ArgumentParser(description="네트워크 환경 freeze 작성·검증")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("design")
    s.add_argument("--prereg-commit", required=True)
    s.add_argument("--supersedes", type=Path, default=None)
    s.add_argument("--reason", default=None)
    s = sub.add_parser("impl")
    s.add_argument("--test-results", required=True, type=Path)
    s.add_argument("--review-summary", required=True, type=Path)
    s.add_argument("--code-commit", required=True)
    s.add_argument("--supersedes", type=Path, default=None)
    s.add_argument("--reason", default=None)
    sub.add_parser("verify")
    sub.add_parser("profile")
    s = sub.add_parser("tests")
    s.add_argument("--out", required=True, type=Path)
    a = p.parse_args(argv)
    if a.cmd == "profile":
        out = write_new(nc.ROOT / SELECTION_PROFILE_REL, build_selection_profile_obj())
    elif a.cmd == "tests":
        res = collect_test_results()
        out = write_new(a.out, res)
        print(json.dumps({k: res[k] for k in ("network", "phase2", "new_regressions")}, ensure_ascii=False))
    elif a.cmd == "design":
        out = write_new(nc.ROOT / DESIGN_FREEZE, build_design_freeze(a.prereg_commit, supersedes=a.supersedes, reason=a.reason))
    elif a.cmd == "impl":
        out = write_new(nc.ROOT / IMPLEMENTATION_FREEZE, build_implementation_freeze(a.test_results, a.review_summary, a.code_commit, supersedes=a.supersedes, reason=a.reason))
    else:
        print(json.dumps(verify_implementation_freeze(), ensure_ascii=False))
        return 0
    print(json.dumps({"written": out.relative_to(nc.ROOT).as_posix(), "sha256": text_sha256(out)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
