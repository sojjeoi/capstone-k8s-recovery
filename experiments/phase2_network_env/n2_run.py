#!/usr/bin/env python3
"""네트워크 환경 검증 실행 진입점(2026-10-17, M4, DEVELOPMENT - 동결 전).

하위 명령은 세 종류다.
  읽기 전용(클러스터는 kubectl get·정책 /admin/state 조회만, 승인 불필요):
    capture-baseline  수집 시작 전 원래(시연) 상태를 파일로 보존한다(덮어쓰기 금지, 이미 tolerant거나 오염돼 있으면 쓰지 않는다).
    restore-check     지금 상태가 보존한 원래 상태와 같은지 확인한다(시연 환경 원복 관문 - 세션·수집을 끝났다고 보려면 이것이 통과해야 한다).
    preflight         세션 시작 전 점검(network_preflight)만 한다.
  오프라인(파일만):
    select            Stage 1 기록 -> 선택 증거·active profile (수집이 끝난 뒤에만).
    record-validity   세션 유효성 기록(탐지기·정책 결과를 읽지 않는다).
  라이브 [USER APPROVAL REQUIRED - 세션마다 별도 승인, 이 파일을 만든 것은 승인이 아니다]:
    stage1 / stage2   세션 1회. 구현 동결 검증(n2_freeze)이 통과하지 않으면 클러스터 연결을 조립하기 전에 멈춘다.
관리자 토큰은 환경 변수 GUARDED_ADMIN_TOKEN에서만 읽고 출력·저장하지 않는다."""
import sys
sys.dont_write_bytecode = True
import argparse  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import n2_cleanup as cl  # noqa: E402
import n2_common as nc  # noqa: E402
import n2_select_offline as so  # noqa: E402
import n2_session as ns2  # noqa: E402
import n2_stage1 as ns1  # noqa: E402
import n2_stage2 as n2s2  # noqa: E402

pc = nc.pc
ARTIFACT_ENV = "PHASE2_ARTIFACT_ROOT"
DEFAULT_POLICY_URL = "http://127.0.0.1:18090"
BASE_DELAY_SEC = 30          # 시연(canonical) 상태의 scaleDownDelaySeconds (run_guarded_pilot.BASE_SCALE_DOWN_DELAY_SEC와 같다)
POLL_SEC = 10.0
_sleep, _mono = time.sleep, time.monotonic        # 시험에서 바꾼다


def _now():
    return datetime.now(timezone.utc).isoformat()


def default_verify_freeze():
    """구현 동결 검증기. n2_freeze(동결 때 만든다)가 없으면 연결되지 않은 검증기(호출하면 fail-closed로 종료)를 돌려준다."""
    try:
        import n2_freeze
    except ImportError:
        return ns1._unfrozen
    return n2_freeze.verify_implementation_freeze


def live_reader(policy_url: str, profile_path):
    """읽기 전용 조회기(RealNetLive). 정책 상태 조회에 관리자 토큰이 필요하다 - 환경 변수에서만 읽는다."""
    import guarded_cleanup_gate as gcg
    import run_guarded_pilot as gp
    token = os.environ.get("GUARDED_ADMIN_TOKEN")
    if not token:
        raise SystemExit("GUARDED_ADMIN_TOKEN 환경변수 필요(정책 상태 읽기 전용 조회)")
    return ns2.RealNetLive(gcg.read_snapshot, profile_path, policy_state_fn=gp.PolicyClient(policy_url, token).state)


def current_state(reader, result_dirs):
    """지금 상태를 보존 형식(n2_cleanup.capture_state)으로 읽는다(읽기 전용). 실행 중 표식은 결과 폴더들에서 모은다.
    반환: (상태 기록, 같은 시점의 스냅샷) - 스냅샷은 canonical Healthy 검사에도 쓴다."""
    snap = reader.snapshot_fn() or {}
    s = reader.read_state(snap)
    markers = sorted(p.name for d in result_dirs if Path(d).exists() for p in Path(d).glob("running-*.json"))
    return cl.capture_state(s["template"], s["delay"], s["policy"] or {}, s["chaos_names"], markers), snap


def canonical_problems(snap, delay) -> list:
    """프로젝트의 기존 canonical Healthy 검사(guarded_cleanup_gate.check_canonical)를 그대로 쓴다: 컨트롤러 반영 완료·Healthy·active=current=stable·
    Service 선택자·active Pod Ready/재시작 0·endpoint·ReplicaSet 수(이전 리비전 0)·유예 시간·health·잔여 chaos/probe Pod.
    템플릿이 원래와 같아도 승격이 끝나지 않았거나 이전(tolerant) 리비전이 아직 서비스 중이면 여기서 걸린다."""
    import guarded_cleanup_gate as gcg
    return [f"canonical: {p}" for p in gcg.check_canonical(snap, delay=delay)]


def cmd_capture_baseline(a, reader, dirs) -> int:
    state, snap = current_state(reader, dirs)
    probs = cl.start_state_problems(state) + canonical_problems(snap, BASE_DELAY_SEC)             # 원복 기준은 정상(canonical) 상태여야 한다
    if probs:                                                   # 이미 tolerant거나 오염된 상태는 원복 기준으로 쓸 수 없다 - 쓰지 않는다
        print(json.dumps({"ok": False, "written": None, "problems": probs}, ensure_ascii=False))
        return 2
    pc.write_json_new(a.out, {"captured_at_utc": _now(), "state": state})
    print(json.dumps({"ok": True, "written": str(a.out), "probe_profile": state["probe_profile"]}, ensure_ascii=False))
    return 0


def cmd_restore_check(a, reader, dirs) -> int:
    """원복 확인(읽기 전용): 보존한 원래 상태와 같고 + canonical Healthy. --wait-sec > 0이면 통과할 때까지(승격 뒤 이전 리비전이 줄어드는 시간 등) 그 시간 안에서 다시 읽는다."""
    base = json.loads(Path(a.baseline).read_text(encoding="utf-8"))["state"]
    deadline = _mono() + max(0.0, float(getattr(a, "wait_sec", 0) or 0))
    while True:
        state, snap = current_state(reader, dirs)
        probs = cl.restore_problems(base, state) + canonical_problems(snap, base["scale_down_delay"])
        if not probs or _mono() >= deadline:
            break
        _sleep(POLL_SEC)
    print(json.dumps({"ok": not probs, "problems": probs}, ensure_ascii=False))
    return 0 if not probs else 2


def cmd_session_end_check(a, reader, dirs) -> int:
    """세션 하나가 끝난 직후의 정리 확인(읽기 전용). **전체 원복 확인(restore-check)과 다르다**: 실험 block이 계속되는 동안에는 11 s 프로필을
    유지해야 하므로 원래 상태와 같기를 요구하지 않고 오히려 11 s 프로필이 **유지**돼 있기를 요구한다. 요구하는 것: 이 세션의 지연 설정·표식이 남지 않았고,
    정책 context/blocked/unresolved가 깨끗하며, 정책 mode가 지정값이고, canonical Healthy. 이것이 통과해야 다음 세션 시작 전 preflight 하위 명령으로 넘어간다."""
    state, snap = current_state(reader, dirs)
    probs = cl.session_end_problems(state, [], expect_tolerant=True, policy_mode=a.policy_mode) + canonical_problems(snap, BASE_DELAY_SEC)
    print(json.dumps({"ok": not probs, "problems": probs, "scope": "session_end (not a full restore)"}, ensure_ascii=False))
    return 0 if not probs else 2


def cmd_preflight(a, reader, dirs) -> int:
    if a.stage == "1":
        loaded, mode = nc.load_profile(a.profile or ns1.NetConfig().selection_profile, stage="stage1"), "record_only"
    else:
        if not a.profile:
            raise SystemExit("--stage 2는 --profile(active profile)이 필요하다")
        loaded, mode = nc.load_profile(a.profile, stage="stage2"), "act"
    r = ns2.network_preflight(reader, loaded["profile"], policy_mode=mode)
    print(json.dumps({"ok": r["ok"], "problems": r["problems"], "target": r.get("target")}, ensure_ascii=False))
    return 0 if r["ok"] else 2


def stage_dirs():
    return [ns1.NetConfig().results_dir, n2s2.Net2Config().results_dir]


def cmd_record_validity(a, verify_freeze) -> int:
    if not a.operator_intervention:
        raise SystemExit("--operator-intervention 필요('none' 또는 개입 내용)")
    s1 = ns1.load_stage1(ns1.NetConfig(), verify_freeze)
    ns2.install(s1, None)
    if a.stage == "1":
        v = s1.validate(a.record_validity, s1.RESULTS_DIR, operator=a.operator_intervention)
        pc.write_json_new(s1.RESULTS_DIR / f"p2-validity-{a.record_validity}.json", v)
    else:
        s2 = n2s2.load_stage2(s1)
        n2s2.install(s1, s2, None)
        v = s2.validate(a.record_validity, s2.RESULTS_DIR, operator=a.operator_intervention)
        pc.write_json_new(s2.RESULTS_DIR / f"p2-s2-validity-{a.record_validity}.json", v)
    print(json.dumps({k: v.get(k) for k in ("run", "valid", "invalid_reasons")}, ensure_ascii=False))
    return 0


def cmd_stage1(a, verify_freeze, real_deps=None) -> int:
    """세션 1회(정상 또는 압박, record_only). [USER APPROVAL REQUIRED] 라이브 조립 - 조립 경로는 test_n2_run이 가짜 deps로 시험한다."""
    import run_guarded_pilot as gp
    import run_timed_switch_recovery_trial as rt
    if not a.run or not a.artifact_root:
        raise SystemExit(f"--run과 --artifact-root(또는 {ARTIFACT_ENV}) 필요")
    s1 = ns1.load_stage1(ns1.NetConfig(), verify_freeze)
    try:
        prov = s1.pc.verify_implementation_freeze()              # 클러스터 연결을 조립하기 전에 fail-closed
    except SystemExit as e:                                      # 동결 manifest가 있는데 불일치면 시도로 기록한다(원본 run_stage1.main과 같다 - 동결 전이면 기록 없이 종료)
        if not (nc.ROOT / s1.pc.IMPLEMENTATION_FREEZE).is_file():
            raise
        s1.refuse(a.run, s1.RESULTS_DIR, s1.schedule(a.run, s1.RESULTS_DIR, a.resume_after_refusal), "refused_profile_invalid", str(e),
                  _now(), "implementation_freeze")
    token = os.environ.get("GUARDED_ADMIN_TOKEN")
    if not token:
        raise SystemExit("GUARDED_ADMIN_TOKEN 환경변수 필요")
    if real_deps is None:
        import run_guarded_normal_bc as rn
        real_deps = rn.real_deps
    deps = real_deps(a.policy_url, token, a.session_file)

    def check_unresolved():                                      # Phase 2와 같은 폴더 확인 + 네트워크 결과 폴더
        import run_guarded_bck as gb
        import run_v1_detector_trial as v1
        v1.check_no_unresolved_anywhere()
        for x in gp.OTHER_RESULT_DIRS:
            rt.check_no_unresolved_failures(gp.RESULTS_DIR.parent / x)
        for x in (gp.RESULTS_DIR, gp.AB_RESULTS_DIR, gb.BCK_RESULTS_DIR, s1.N1_RESULTS_DIR):
            rt.check_no_unresolved_failures(x)
        s1.check_unresolved_stage1(s1.RESULTS_DIR)
    deps.check_unresolved = check_unresolved
    net = ns2.RealNetLive(deps.canonical_snapshot, a.profile, policy_state_fn=deps.policy.state)
    ns2.install(s1, net)
    r = s1.run_session(a.run, deps=deps, profile_path=a.profile, artifact_root=a.artifact_root, impl=prov,
                       audit_reader=lambda: s1.read_policy_audit(s1.MODE), mode=s1.MODE, preflight_only=a.preflight_only,
                       resume_ack=a.resume_after_refusal)
    s = r.get("summary") or {}
    print(json.dumps({"run": a.run, "outcome": s.get("outcome_status"), "cleanup_errors": s.get("cleanup_errors"),
                      "integrity": (r.get("session_record") or {}).get("integrity"), "preflight_only": r.get("preflight_only", False),
                      "network_delivery_check": s.get("network_delivery_check")}, ensure_ascii=False, default=str))
    return 2 if s.get("cleanup_errors") or not ((r.get("session_record") or {}).get("integrity") or {"ok": True})["ok"] else 0


def cmd_stage2(a, verify_freeze, real_deps=None) -> int:
    """Stage 2 세션 1회(act, 실제 승격). [USER APPROVAL REQUIRED] 라이브 조립."""
    import run_guarded_pilot as gp
    import run_timed_switch_recovery_trial as rt
    if not (a.run and a.active_profile and a.selection_evidence and a.artifact_root):
        raise SystemExit("--run, --active-profile, --selection-evidence, --artifact-root 필요")
    s1 = ns1.load_stage1(ns1.NetConfig(), verify_freeze)
    s2 = n2s2.load_stage2(s1)
    s2.check_outcome(a.selection_evidence)                       # SELECTED가 아니면 연결 조립 전에 멈춤
    impl = s1.pc.verify_implementation_freeze()
    token = os.environ.get("GUARDED_ADMIN_TOKEN")
    if not token:
        raise SystemExit("GUARDED_ADMIN_TOKEN 환경변수 필요")
    if real_deps is None:
        import run_guarded_bck as gb
        real_deps = gb.real_deps
    deps = real_deps(a.policy_url, token, a.session_file)
    base = deps.check_unresolved

    def check_unresolved():
        base()
        rt.check_no_unresolved_failures(s2.RESULTS_DIR)
        s1.check_unresolved_stage1(s1.RESULTS_DIR)               # Stage 1 미해결 시도도 Stage 2를 막는다
    deps.check_unresolved = check_unresolved
    net = ns2.RealNetLive(deps.canonical_snapshot, a.selection_profile, policy_state_fn=deps.policy.state)
    ns2.install(s1, net)
    n2s2.install(s1, s2, net, collect_fn=lambda d, **kw: so.collect(s1, d, **kw))
    try:
        entry = s2.entry_check(selection_profile_path=a.selection_profile, active_profile_path=a.active_profile,
                               evidence_path=a.selection_evidence, artifact_root=a.artifact_root,
                               snapshot_fn=deps.canonical_snapshot, impl=impl, stage1_dir=s1.RESULTS_DIR)
    except SystemExit as e:                                      # 진입 거부도 시도로 센다(§9 Stage 2 상한)
        s2.refuse(a.run, s2.RESULTS_DIR, str(e))
    r = s2.run_session(a.run, deps=deps, entry=entry, active_profile_path=a.active_profile, artifact_root=a.artifact_root,
                       approval_path=a.act_approval, policy_url=a.policy_url, preflight_only=a.preflight_only, code_provenance=impl,
                       audit_reader=lambda: s1.read_policy_audit(gp.MODE_ACT))
    s = r.get("summary") or {}
    print(json.dumps({"run": a.run, "outcome": s.get("outcome_status"), "cleanup_errors": s.get("cleanup_errors"),
                      "result": (r.get("session_record") or {}).get("result"),
                      "network_delivery_check": r.get("network_delivery_check")}, ensure_ascii=False, default=str))
    return 2 if s.get("cleanup_errors") else 0


def build_parser():
    p = argparse.ArgumentParser(description="네트워크 환경 검증 실행 진입점(DEVELOPMENT). 라이브 하위 명령은 세션마다 별도 승인이 필요하다.")
    p.add_argument("--policy-url", default=DEFAULT_POLICY_URL)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("capture-baseline")
    s.add_argument("--out", required=True, type=Path)
    s = sub.add_parser("restore-check")
    s.add_argument("--baseline", required=True, type=Path)
    s.add_argument("--wait-sec", type=float, default=0.0)
    s = sub.add_parser("session-end-check")
    s.add_argument("--policy-mode", choices=["record_only", "act"], default="record_only")
    s = sub.add_parser("preflight")
    s.add_argument("--stage", choices=["1", "2"], default="1")
    s.add_argument("--profile", type=Path, default=None)
    s = sub.add_parser("select")
    s.add_argument("--results-dir", type=Path, default=None)
    s.add_argument("--artifact-root", type=Path, default=os.environ.get(ARTIFACT_ENV) or None)
    s.add_argument("--profile", type=Path, default=None)
    s.add_argument("--out-dir", required=True, type=Path)
    s = sub.add_parser("record-validity")
    s.add_argument("--stage", choices=["1", "2"], required=True)
    s.add_argument("--record-validity", required=True, metavar="RUN")
    s.add_argument("--operator-intervention", default=None)
    s = sub.add_parser("stage1")
    s.add_argument("--run")
    s.add_argument("--profile", type=Path, default=ns1.NetConfig().selection_profile)
    s.add_argument("--artifact-root", type=Path, default=os.environ.get(ARTIFACT_ENV) or None)
    s.add_argument("--session-file", default=None)
    s.add_argument("--preflight-only", action="store_true")
    s.add_argument("--resume-after-refusal", default=None)
    s = sub.add_parser("stage2")
    s.add_argument("--run")
    s.add_argument("--selection-profile", type=Path, default=ns1.NetConfig().selection_profile)
    s.add_argument("--active-profile", type=Path, default=None)
    s.add_argument("--selection-evidence", type=Path, default=None)
    s.add_argument("--artifact-root", type=Path, default=os.environ.get(ARTIFACT_ENV) or None)
    s.add_argument("--act-approval", default=None)
    s.add_argument("--session-file", default=None)
    s.add_argument("--preflight-only", action="store_true")
    return p


def main(argv=None, *, verify_freeze=None, reader_factory=None, real_deps=None) -> int:
    a = build_parser().parse_args(argv)
    verify_freeze = verify_freeze or default_verify_freeze()
    reader_factory = reader_factory or live_reader
    for name in ("profile", "selection_profile", "active_profile", "artifact_root", "selection_evidence"):
        if getattr(a, name, None):
            setattr(a, name, Path(getattr(a, name)).resolve())         # 실행기 subprocess(cwd 다름)에 같은 경로가 가도록
    if a.cmd in ("capture-baseline", "restore-check", "session-end-check", "preflight"):
        reader = reader_factory(a.policy_url, getattr(a, "profile", None) or ns1.NetConfig().selection_profile)
        fn = {"capture-baseline": cmd_capture_baseline, "restore-check": cmd_restore_check, "session-end-check": cmd_session_end_check,
              "preflight": cmd_preflight}[a.cmd]
        return fn(a, reader, stage_dirs())
    if a.cmd == "select":
        if a.artifact_root is None:
            raise SystemExit(f"--artifact-root(또는 {ARTIFACT_ENV}) 필요")
        s1 = ns1.load_stage1(ns1.NetConfig(), verify_freeze)
        argv2 = ["--artifact-root", str(a.artifact_root), "--out-dir", str(a.out_dir)]
        argv2 += ["--results-dir", str(a.results_dir)] if a.results_dir else []
        argv2 += ["--profile", str(a.profile)] if a.profile else []
        return so.main(argv2, s1=s1)
    if a.cmd == "record-validity":
        return cmd_record_validity(a, verify_freeze)
    return {"stage1": cmd_stage1, "stage2": cmd_stage2}[a.cmd](a, verify_freeze, real_deps)


if __name__ == "__main__":
    sys.exit(main())
