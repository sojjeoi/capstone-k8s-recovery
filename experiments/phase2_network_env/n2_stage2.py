#!/usr/bin/env python3
"""네트워크 환경 Stage 2(act 복구) 세션 실행기의 설정과 교체 지점(2026-10-17, M2, DEVELOPMENT - 동결 전, 이 파일은 클러스터를 바꾸지 않는다).

방식은 Stage 1과 같다(n2_stage1): 동결 phase2_detector_selection/run_stage2.py의 **같은 소스**를 별도 모듈 인스턴스로 읽어 쓰고
(원본 모듈·run_guarded_bck·Recovery Policy 코드는 수정하지 않는다), 네트워크 환경에서 달라지는 부분만 인스턴스에서 바꾼다:
  - entry_check: 후보 3개 선택 규칙(n2_selection)으로 증거를 다시 계산해 대조한다(원본은 후보 2개 selection 모듈을 쓴다).
  - run_session: (1) 네트워크 사전 점검(읽기 전용, 실패하면 시도로 기록하고 클러스터 변경 없이 멈춤) (2) 지연 주입기와 증거 기록기로 deps 교체
    (CPU 스트레스 주입기·CPU 카운터 대신) (3) 동결 run_bck 호출 동안만 계약의 압박 단계를 네트워크 단계로 기록(원본 코드는 CPU 상수를 계약에 적는다)
    (4) 끝난 뒤 전달 증거(net-delivery-<run_id>.json)를 따로 남긴다.
  - validate: 원본 run_validity에서 CPU 전용 V3만 네트워크 전달 확인(N3)으로 바꾼다. 성공 판정(stage2_session_result)은 후보와 무관해 원본 그대로 쓴다.
복구 경로(act 승격·서비스 확인·정책 identity 검사)는 CPU 환경과 같은 코드다."""
import sys
sys.dont_write_bytecode = True
import contextlib  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from pathlib import Path  # noqa: E402

import n2_common as nc  # noqa: E402
import n2_selection as nsel  # noqa: E402
import n2_session as ns2  # noqa: E402

pc = nc.pc
STAGE2_SOURCE = nc.P2DIR / "run_stage2.py"
EVIDENCE_PREFIX = "n2sel-"          # 선택 증거 id 접두어(Phase 2는 p2sel-) - 선택 증거를 만드는 쪽(n2_selection.evidence_from)과 같아야 한다


@dataclass(frozen=True)
class Net2Config:
    results_dir: Path = nc.EXP / "results" / "phase2_network_env_stage2"
    executor: Path = nc.HERE / "n2_executor.py"
    rep_base: int = 4300            # Phase 2 Stage 2는 4200 대 - 겹치지 않게


def load_stage2(stage1, cfg: Net2Config = None):
    """run_stage2.py 소스를 별도 모듈 인스턴스로 실행하고 네트워크 설정을 끼운 인스턴스를 돌려준다. stage1 = n2_stage1.load_stage1 결과."""
    cfg = cfg or Net2Config()
    spec = importlib.util.spec_from_file_location("n2_stage2_instance", STAGE2_SOURCE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)                                   # 최상위는 원본 pc·s1로 실행된다(값은 아래에서 교체)
    m.pc, m.s1 = stage1.pc, stage1
    m.RESULTS_DIR, m.EXECUTOR = Path(cfg.results_dir), Path(cfg.executor)
    m.STAGE2_PLAN_ID = f"{stage1.CONFIG.short_plan_tag}-stage2"   # 라벨 한도(63자): 원본 plan id로는 Stage 2 실행 ID가 64자(n2p01에서 확인). 다른 동결 모듈도 이 값에서 ID를 만들므로 plan id 자체를 줄인다
    m.RUNS = {f"n2s{i:02d}": (i, m.ARM, cfg.rep_base + i) for i in range(1, pc.MAX_ATTEMPTS["stage2"] + 1)}
    m._read, m._lines = stage1._read, stage1._lines
    m.CONFIG = cfg
    return m


# ---------------- 진입 조건 ----------------
# ponytail: 원본 run_stage2.entry_check 복사 + 선택 규칙·profile 로더만 후보 3개용으로 교체 - 원본은 함수 안에서 후보 2개 selection 모듈을
# import하므로 인자로 바꿀 수 없고 동결 파일 수정은 금지. 원본이 selection 모듈을 인자로 받게 되면 그것으로 대체.
def entry_check_net(s2, *, selection_profile_path, active_profile_path, evidence_path, artifact_root, snapshot_fn, impl: dict,
                    stage1_dir=None, collect_fn=None) -> dict:
    probs = []
    ev = s2._read(evidence_path)
    ev_sha = pc.sha256_bytes(evidence_path)
    out = s2.check_outcome(evidence_path)
    recomputed = nsel.select(ev.get("inputs") or {})
    if ev.get("rule") != s2.pc.PREREG_SHA256 or ev.get("evidence_id") != f"{EVIDENCE_PREFIX}{pc.obj_sha256(ev.get('inputs') or {})[:16]}" \
            or {k: ev.get(k) for k in ("outcome", "reason", "gate_results")} != recomputed:
        probs.append("selection_evidence_not_reproducible")
    if stage1_dir is not None:
        if collect_fn is None:
            raise SystemExit("Stage 2 금지: Stage 1 기록으로 선택 입력을 다시 만들 함수(collect_fn)가 없음")
        again = collect_fn(stage1_dir, profile_path=selection_profile_path, artifact_root=artifact_root, verify_freeze=lambda: impl)
        if pc.canonical(again["inputs"]) != pc.canonical(ev.get("inputs") or {}):
            probs.append("selection_inputs_differ_from_stage1_records")
    sel = nc.load_profile(selection_profile_path, stage="stage1")
    if sel["sha256"] != impl.get("selection_profile_sha256"):
        probs.append("selection_profile_not_implementation_frozen")
    try:
        act = nc.load_profile(active_profile_path, stage="stage2")
    except nc.ProfileInvalid as e:
        raise SystemExit(f"Stage 2 금지: active profile PROFILE_INVALID: {e}") from e
    diff = pc.active_profile_diff(sel["profile"], act["profile"])
    if diff:
        probs.append(f"active_profile_not_mechanical_derivation: {diff}")
    s = act["profile"]["selection"]
    if s.get("source_selection_profile_sha256") != sel["sha256"] or ev.get("selection_profile_sha256") != sel["sha256"]:
        probs.append("selection_profile_chain_mismatch")
    if s.get("selection_evidence_sha256") != ev_sha or s.get("outcome") != out or s.get("gate_results") != ev.get("gate_results"):
        probs.append("selection_evidence_chain_mismatch")
    if act["active"] != out.split(":", 1)[1]:
        probs.append(f"active_detector {act['active']} != selected {out}")
    try:
        art = pc.verify_artifacts(artifact_root)
        pc.verify_rule_spec()
    except nc.ProfileInvalid as e:
        raise SystemExit(f"Stage 2 금지: {e}") from e
    stale = pc.check_stale(act["profile"], nc.observe_environment((snapshot_fn() or {}).get("template")),
                           nc.contract_hashes()["recovery_policy_sha256"])
    if stale:
        probs.append(f"PROFILE_STALE: {stale}")
    if probs:
        raise SystemExit(f"Stage 2 금지(조치 없음): {probs}")
    return {"loaded": act, "art": art, "evidence_sha256": ev_sha, "selected": act["active"]}


# ---------------- 계약의 압박 단계 ----------------
@contextlib.contextmanager
def network_stage_contract():
    """동결 run_bck가 계약에 적는 압박 단계 상수(rt.DEMO_STAGES = CPU 4단계)를 호출 동안만 네트워크 단계로 바꾼다.
    run_bck 안에서 이 상수를 읽는 곳은 계약 기록 한 곳뿐이다(주입기는 deps.make_injector가 만든다). 예외가 나도 복원한다."""
    import run_timed_switch_recovery_trial as rt
    saved = rt.DEMO_STAGES
    rt.DEMO_STAGES = [dict(s) for s in nc.NETWORK_STAGES]
    try:
        yield
    finally:
        rt.DEMO_STAGES = saved


# ---------------- 세션 ----------------
def sidecar_path(results_dir, run_id) -> Path:
    return Path(results_dir) / f"net-delivery-{run_id}.json"


def run_session_net(s2, run, *, orig, deps, entry: dict, active_profile_path, artifact_root, approval_path, policy_url, audit_reader,
                    net, hooks=None, results_dir=None, preflight_only=False, code_provenance=None, starter_factory=None) -> dict:
    """[USER APPROVAL REQUIRED] 실제 deps면 act 압박·실제 승격. entry = entry_check_net 결과. orig = 설치 전 원본 run_session. 테스트는 가짜 deps·net."""
    d = Path(results_dir or s2.RESULTS_DIR)
    d.mkdir(parents=True, exist_ok=True)
    pf = ns2.network_preflight(net, entry["loaded"]["profile"], policy_mode="act")        # 읽기 전용 - 실패하면 클러스터를 바꾸기 전에 멈춘다
    if not pf["ok"]:
        s2.refuse(run, d, f"network_preflight: {pf['problems']}")
    box = {}
    watch = ns2.ProtectedWatch(net, mono_fn=deps.monotonic_fn)
    d2 = ns2.net_deps(deps, ns2.make_injector_factory(box, net, hooks), net)
    d2.cpu_raw_counter = lambda pod, t0: []                                                 # CPU 카운터는 네트워크 환경에 해당 없음(기록 전용 자리)
    base_safety = d2.base_safety

    def safety_with_protected_watch(inj, kind):
        fn = base_safety(inj, kind)

        def check(baseline, prober):
            fn(baseline, prober)
            watch.check(inj.get_preview_prep_info() if hasattr(inj, "get_preview_prep_info") else None)
        return check
    d2.base_safety = safety_with_protected_watch
    with network_stage_contract():
        r = orig(run, deps=d2, entry=entry, active_profile_path=active_profile_path, artifact_root=artifact_root,
                 approval_path=approval_path, policy_url=policy_url, audit_reader=audit_reader, results_dir=d,
                 preflight_only=preflight_only, code_provenance=code_provenance, starter_factory=starter_factory)
    if preflight_only:
        return r
    ev = ns2.collect_delivery_evidence(box.get("rec"), watch, net)
    ev["network_preflight"] = {k: pf.get(k) for k in ("target", "net_probe")}
    pc.write_json_new(sidecar_path(d, s2.run_id_for(run)), ev)
    r["network_delivery_check"] = ev["network_delivery_check"]
    return r


# ---------------- 유효성(오프라인, 탐지기·정책 결과 미사용) ----------------
def validate_stage2_net(s1, s2, run, d, *, operator: str, probe_dir=None) -> dict:
    """원본 Stage 2 유효성(§8.4: V5 전환 결과 의존 항목 제외)에서 CPU 전용 V3만 네트워크 전달 확인(N3)으로 바꾼다."""
    import guarded_ab_analysis as gab
    d = Path(d)
    rec = s2._read(s2._session_path(d, run))
    rid = rec["run_id"]
    dd, probe_csv = s1.pressure_run_dict(d, run, probe_dir or s1.gp.PROBE_RESULTS_DIR, rid=rid, rep=s2.RUNS[run][2])
    frozen = {**ns2.pressure_frozen(s1, pc.expected_if_hashes()), "threshold": nc.FROZEN_DETECTORS[rec["selected"]]["threshold"]}
    v = gab.run_validity(dd, frozen, gab.primary(dd, probe_csv))
    reasons = [r for r in v["reasons"] if not r.startswith("V3_") and (not r.startswith("V5_") or r.startswith("V5_old_policy"))]
    side = sidecar_path(d, rid)
    chk = (json.loads(side.read_text(encoding="utf-8")).get("network_delivery_check") if side.exists() else None) \
        or {"problems": ["network_delivery_check_missing"]}
    reasons += ns2.isolation_reasons(chk["problems"]) + [f"N3_{x}" for x in chk["problems"]]
    reasons += [f"operator_intervention: {operator}"] if operator != "none" else []
    return {"run": run, "run_id": rid, "valid": not reasons, "invalid_reasons": reasons, "operator_intervention": operator,
            **ns2.validity_flags(reasons),
            "network_delivery": {"container_records_present": chk.get("container_records_present"), "n_problems": len(chk["problems"])},
            "rule": "prereg §8.4 - §8.2 B 항목만(V5 전환 결과 의존 항목 제외) + 네트워크 전달 확인(N3), 성공 판정 전에 확정"}


def install(s1, s2, net, hooks=None, collect_fn=None):
    """Stage 2 인스턴스의 CPU 전용 자리를 네트워크 것으로 교체한다(인스턴스에만 - 원본 모듈은 건드리지 않는다)."""
    orig_run_session = s2.run_session
    s2.entry_check = lambda **kw: entry_check_net(s2, collect_fn=collect_fn, **kw)
    s2.run_session = lambda run, **kw: run_session_net(s2, run, orig=orig_run_session, net=net, hooks=hooks, **kw)
    s2.validate = lambda run, d, *, operator, probe_dir=None: validate_stage2_net(s1, s2, run, d, operator=operator, probe_dir=probe_dir)
    s2.NET_LIVE = net
    return s2
