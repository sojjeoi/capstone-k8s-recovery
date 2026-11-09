"""G 세션 실행 연결 파일 (2026-10-22). guarded_pilot_session.ps1 이 `--run R --mode act --policy-url U --session-file F --act-approval A
[--preflight-only]` 로 부르는 실행기이다. 네트워크 검증의 Stage 2 경로(n2_run.cmd_stage2)를 같은 순서로 따르되, **프로세스 안에서만**
다음을 바꾼다(동결 소스 파일은 수정하지 않는다):
  - 결과 폴더: experiments/results/hpa_compare/{main-g|check-g}  (네트워크 검증 결과와 분리)
  - 실행 ID: n2s11~n2s15(본), n2s91·n2s92(기능 점검) 만 허용, 시도 상한·유효 개수 상한 해제
  - F3: ACTIVE 가 cpu-threshold-c 인 비교 전용 profile 과 부하(load_ramp) 주입기로 교체, 선택 증거 사슬 검사는 생략
기록 전용 보조: `--validity RUN` 은 세션 유효성 기록을 새 파일로 남긴다(다음 G 세션 진입 조건).
"""
import argparse
import copy
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXP = HERE.parent
ROOT = EXP.parent
NET = EXP / "phase2_network_env"
for p in (str(NET), str(EXP), str(EXP / "phase2_detector_selection")):
    if p not in sys.path:
        sys.path.insert(0, p)

import n2_common as nc  # noqa: E402
import n2_run  # noqa: E402
import n2_select_offline as so  # noqa: E402
import n2_session as ns2  # noqa: E402
import n2_stage1 as ns1  # noqa: E402
import n2_stage2 as n2s2  # noqa: E402

pc = nc.pc
RES = EXP / "results" / "hpa_compare"
SEL = EXP / "results" / "phase2_network_env_selection"
RUNS_G = {"n2s11": ("f2", 1), "n2s12": ("f2", 2), "n2s13": ("f2", 3), "n2s14": ("f3", 1), "n2s15": ("f3", 2),
          # 명백한 하네스 오류로 무효가 된 세션을 1회 다시 할 때만 쓰는 예비 ID(원기록은 그대로 보존)
          "n2s21": ("f2", 1), "n2s22": ("f2", 2), "n2s23": ("f2", 3), "n2s24": ("f3", 1), "n2s25": ("f3", 2),
          "n2s91": ("f2", 91), "n2s92": ("f3", 92),
          # 요청 단위 보호 검증(2026-10-23, 별도 계측 판 probe) 전용 ID - 위 HPA 비교 13세션과 섞지 않는다
          "n2s31": ("f2", 31), "n2s32": ("f2", 32),
          # F2 블록 2 G 보충 실행(2026-10-29 사용자 승인): n2s12·n2s22 가 노드 메모리 사전검사로 무효 → 3번째 시도, 같은 블록 H·W 보다 늦게 실행
          "n2s26": ("f2", 2),
          # F2 G 추가 반복 2회(2026-10-30 사용자 승인 "우리 시스템만 2회"): H·W 짝 없음, 기존 H·W 3회와 비교하는 추가 반복
          "n2s27": ("f2", 4), "n2s28": ("f2", 5),
          # 통일 비교(2026-11-04, docs/design/UNIFIED-COMPARISON-PREREG-20261104.md): F1 CPU 압박 n2s51~60, F2 n2s61~66, F3 n2s71~78,
          # F1 기능 점검 n2s93(check-g 폴더, 본 비교에 쓰지 않음). 두 번째 숫자는 블록 번호다.
          **{f"n2s{50 + i}": ("f1", i) for i in range(1, 11)},
          # 11/05: n2s64 가 진입 전 사전검사 거부(주입 없음)로 ID 를 하나 더 써서 모자람 - 확장(칸당 10회)용 예비 F2 n2s67~70, F3 n2s79~82 추가
          **{f"n2s{60 + i}": ("f2", 10 + i) for i in range(1, 11)},
          **{f"n2s{70 + i}": ("f3", 10 + i) for i in range(1, 13)},
          "n2s93": ("f1", 93),
          # n2s93 은 9090 port-forward 충돌로 시작 직후 중단(11/04 01:38, 주입 없음) - 기록 보존, 점검은 n2s94 로 다시 한다
          "n2s94": ("f1", 94)}
F3_PROFILE = HERE / "profiles" / "f3-active-cpu.json"


# 다른 작업(시연)이 작업 폴더에서 수정 중인 시연 전용 파일 2개: 동결 manifest 에는 들어 있으나 G 실행 경로에서 쓰이지 않는다.
# 작업 폴더 대신 커밋(HEAD) 버전이 manifest 해시와 같을 때만 그 파일의 해시를 커밋 버전으로 대조한다(그 밖의 불일치는 그대로 실패).
FREEZE_DEMO_ONLY = ("experiments/run_demo_phase2.py", "experiments/test_run_demo_phase2.py")


def tolerant_verify():
    import hashlib
    import subprocess
    import n2_freeze as fz
    orig = fz.text_sha256
    manifest = json.loads((ROOT / fz.IMPLEMENTATION_FREEZE).read_text(encoding="utf-8"))["files"]

    def patched(path):
        h = orig(path)
        try:
            rel = Path(path).resolve().relative_to(ROOT.resolve()).as_posix()
        except ValueError:
            return h
        if rel in FREEZE_DEMO_ONLY and h != manifest.get(rel):
            blob = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=ROOT, capture_output=True, check=True).stdout
            head = hashlib.sha256(blob.replace(bytes([13, 10]), bytes([10]))).hexdigest()
            if head == manifest.get(rel):
                return head
        return h
    fz.text_sha256 = patched
    try:
        return fz.verify_implementation_freeze()
    finally:
        fz.text_sha256 = orig


def results_dir_for(run):
    n = int(run[3:])
    # 동결 규칙: 같은 결과 폴더에 정리 오류가 있는 이전 G 세션이 있으면 다음 세션 진입을 막는다("중단 규칙: 정리 오류 - 사용자 확인 필요").
    # 그래서 G 세션마다 자기 폴더를 쓴다(이미 쓴 n2s11·n2s12 는 main-g, n2s22 는 main-g-r 에 그대로 보존).
    legacy = {"n2s11": "main-g", "n2s12": "main-g", "n2s22": "main-g-r"}
    if n >= 90:
        return RES / "check-g"
    return RES / legacy[run] if run in legacy else RES / "g-runs" / run


def build_f3_profile():
    """비교 전용 profile: 네트워크 검증의 active profile 에서 ACTIVE 만 cpu-threshold-c 로 바꾼다(기계적 파생; 선택 결과가 아니다)."""
    if F3_PROFILE.exists():
        return F3_PROFILE
    p = copy.deepcopy(json.loads((SEL / "active-profile.json").read_text(encoding="utf-8")))
    for d in p["detectors"]:
        d["role"] = nc.ACTIVE if d["detector_id"] == nc.CPU_ID else nc.SHADOW
    p["profile_id"] = "hpa-compare-f3-active-cpu-threshold-c"
    p["selection"] = {**p["selection"], "outcome": f"COMPARISON_ONLY:{nc.CPU_ID}",
                      "reason": "HPA 비교 F3 전용: 기존 동결 CPU 감지기를 ACTIVE 로 둔다. 감지기 선택 결과가 아니다(2026-10-22)."}
    nc.validate_profile(p, stage="stage2")
    F3_PROFILE.parent.mkdir(parents=True, exist_ok=True)
    with open(F3_PROFILE, "x", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(p, ensure_ascii=False, indent=1) + "\n")
    return F3_PROFILE


def load(run):
    fault, idx = RUNS_G[run]
    d = results_dir_for(run)
    d.mkdir(parents=True, exist_ok=True)
    s1 = ns1.load_stage1(ns1.NetConfig(), tolerant_verify)
    s2 = n2s2.load_stage2(s1, n2s2.Net2Config(results_dir=d))
    s2.RUNS = {run: (int(run[3:]), s2.ARM, 7300 + int(run[3:]))}                  # 이 프로세스에서는 이 실행 하나만 존재
    s1.pc.N_STAGE2 = 99                                          # 유효 3개 상한은 네트워크 검증의 것 - 이 비교에는 적용하지 않는다
    nc.MAX_ATTEMPTS["stage2"] = 99
    return s1, s2, fault, d


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--mode")
    ap.add_argument("--policy-url")
    ap.add_argument("--session-file")
    ap.add_argument("--act-approval")
    ap.add_argument("--preflight-only", action="store_true")
    ap.add_argument("--validity", action="store_true")
    a = ap.parse_args(argv)
    if a.run not in RUNS_G:
        raise SystemExit(f"이 비교에서 허용하는 G 실행 ID 가 아님: {a.run}")
    os.chdir(ROOT)
    if os.environ.get("HC_TRACE_DIR"):                       # 요청 단위 계측 판 probe (2026-10-23). 환경변수가 없으면 기존과 완전히 같다.
        import trace_adapter
        trace_adapter.install(os.environ["HC_TRACE_DIR"])
    s1, s2, fault, d = load(a.run)
    if a.validity:
        ns2.install(s1, None)
        n2s2.install(s1, s2, None)
        if fault in ("f1", "f3"):
            # 네트워크 전달 확인(N3)은 NetworkChaos 용이라 F1·F3 에는 해당 없음 - 유효성 기록은 형식상 하네스 오류 여부만 남긴다
            rec = s2._read(s2._session_path(d, a.run))
            v = {"run": a.run, "run_id": rec["run_id"], "valid": not rec.get("cleanup_errors"),
                 "note": f"{fault.upper()} 비교 세션: 비교 분석은 hpa_compare 분석 스크립트가 한다", "invalid_reasons": list(rec.get("cleanup_errors") or [])}
        else:
            v = s2.validate(a.run, d, operator="none")
        pc.write_json_new(d / f"p2-s2-validity-{a.run}.json", v)
        print(json.dumps({k: v.get(k) for k in ("run", "valid", "invalid_reasons")}, ensure_ascii=False))
        return 0
    if a.mode != "act" or not (a.policy_url and a.session_file and a.act_approval):
        raise SystemExit("--mode act, --policy-url, --session-file, --act-approval 필요")
    root = os.environ.get("PHASE2_ARTIFACT_ROOT")
    token = os.environ.get("GUARDED_ADMIN_TOKEN")
    if not root or not token:
        raise SystemExit("PHASE2_ARTIFACT_ROOT, GUARDED_ADMIN_TOKEN 환경변수 필요")
    import run_guarded_bck as gb
    import run_guarded_pilot as gp
    import run_timed_switch_recovery_trial as rt
    impl = s1.pc.verify_implementation_freeze()
    deps = gb.real_deps(a.policy_url, token, a.session_file)
    base = deps.check_unresolved

    def check_unresolved():
        base()
        rt.check_no_unresolved_failures(s2.RESULTS_DIR)
        s1.check_unresolved_stage1(s1.RESULTS_DIR)
    deps.check_unresolved = check_unresolved
    profile_path = SEL / "active-profile.json" if fault == "f2" else build_f3_profile()
    net = ns2.RealNetLive(deps.canonical_snapshot, ns1.NetConfig().selection_profile, policy_state_fn=deps.policy.state)
    ns2.install(s1, net)
    hooks = None
    if fault == "f3":
        import load_ramp_adapter as lra
        cfg = ROOT / "chaos" / "scenario-load-ramp.yaml"
        hooks = {"base_injector": lambda run_id, rep, rec: lra.make_load_ramp_injector(str(cfg), run_id, rt.ARM_LABEL, rep)}
        ns2.collect_delivery_evidence = lambda rec, watch, net_, recovery_verified_at=None: {
            "network_delivery_check": {"problems": [], "container_records_present": None, "note": "F3 부하 주입 - NetworkChaos 없음"}}
    elif fault == "f1":
        # CPU 압박: 시작 서비스 Pod 하나에 1~4 workers × 90 s. 전환 뒤에도 원래 Pod 에 남은 단계를 계속 건다(Phase 2 Stage 2 act 와 같음)
        import cpu_pressure_demo_adapter as cpa
        from run_cpu_demo_trial import DEMO_STAGES
        hooks = {"base_injector": lambda run_id, rep, rec: cpa.make_cpu_pressure_injector(
            run_id, rt.ARM_LABEL, rep, stages=DEMO_STAGES, continue_after_target_replacement=True)}
        ns2.collect_delivery_evidence = lambda rec, watch, net_, recovery_verified_at=None: {
            "network_delivery_check": {"problems": [], "container_records_present": None, "note": "F1 CPU 압박(StressChaos) - NetworkChaos 없음"}}
    n2s2.install(s1, s2, net, hooks=hooks, collect_fn=lambda dd, **kw: so.collect(s1, dd, **kw))
    try:
        if fault == "f2":
            s2.check_outcome(SEL / "phase2-network-selection-evidence.json")
            entry = s2.entry_check(selection_profile_path=ns1.NetConfig().selection_profile, active_profile_path=profile_path,
                                   evidence_path=SEL / "phase2-network-selection-evidence.json", artifact_root=root,
                                   snapshot_fn=deps.canonical_snapshot, impl=impl, stage1_dir=s1.RESULTS_DIR)
        else:                                                      # 비교 전용 ACTIVE(cpu-threshold-c): 선택 증거 사슬 검사는 해당 없음
            loaded = nc.load_profile(profile_path, stage="stage2")
            art = pc.verify_artifacts(root)
            pc.verify_rule_spec()
            entry = {"loaded": loaded, "art": art, "evidence_sha256": loaded["sha256"], "selected": loaded["active"]}
    except SystemExit as e:
        s2.refuse(a.run, d, str(e))
    r = s2.run_session(a.run, deps=deps, entry=entry, active_profile_path=profile_path, artifact_root=root,
                       approval_path=a.act_approval, policy_url=a.policy_url, preflight_only=a.preflight_only, code_provenance=impl,
                       audit_reader=lambda: s1.read_policy_audit(gp.MODE_ACT))
    s = r.get("summary") or {}
    print(json.dumps({"run": a.run, "fault": fault, "outcome": s.get("outcome_status"), "cleanup_errors": s.get("cleanup_errors"),
                      "result": (r.get("session_record") or {}).get("result"),
                      "network_delivery_check": r.get("network_delivery_check")}, ensure_ascii=False, default=str))
    return 2 if s.get("cleanup_errors") else 0


if __name__ == "__main__":
    sys.exit(main())
