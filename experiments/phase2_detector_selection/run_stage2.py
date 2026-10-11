#!/usr/bin/env python3
"""Phase 2 Stage 2 - Recovery Integration Validation 실행기(2026-10-11) - prereg §8.3·§8.4·§13. live 실행은 별도 승인 사항.

진입(§8.3, entry_check): Stage 1 결과 SELECTED + active profile(ACTIVE 정확히 1개)이 동결 selection profile에서 role·selection만
바뀐 기계적 파생 + 증거·profile 해시 사슬 일치 + implementation freeze 일치(§11 100% 기록) + PROFILE_STALE/INVALID 없음.
하나라도 어긋나면 SystemExit(조치 없음). 다른 detector로 자동 전환하지 않는다.

세션: 동결 B/C/K 실행기 run_guarded_bck.run_bck(act·압박·canonical r2·D5·D6·기존 정책 격리 확인)를 수정 없이 호출하고,
계열 전역(RUNS·순서·plan·결과 폴더·arm 이름·detector identity)만 호출 동안 Phase 2 값으로 바꾼다(c3 수집과 같은 방식).
탐지기 = p2_executor active(ACTIVE 판정에서만 c1-signal-v2 송신), 정책 context = 같은 signal_identity(ACTIVE). Recovery Policy
코드는 바꾸지 않는다 - 정책 entrypoint·guarded 검사·승격·트래픽 검증은 detector 종류와 무관하게 같은 경로다.
판정(§8.4·§13): stage2_session_result·phase2_verdict(순수 함수)."""
import sys
sys.dont_write_bytecode = True
import argparse  # noqa: E402
import contextlib  # noqa: E402
import copy  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import p2_common as pc  # noqa: E402
import run_stage1 as s1  # noqa: E402

import guarded_live_detector_runner as gr  # noqa: E402
import run_guarded_bck as gb  # noqa: E402
import run_guarded_pilot as gp  # noqa: E402
import run_if_threshold_candidate_smoke as smoke  # noqa: E402
import run_timed_switch_recovery_trial as rt  # noqa: E402

RESULTS_DIR = pc.EXP / "results" / "phase2_stage2"
EXECUTOR = pc.HERE / "p2_executor.py"
STAGE2_PLAN_ID = f"{pc.PLAN_ID}-stage2"
ARM = "P2"                                   # run_bck의 arm 자리(이름만) - 실제 identity는 profile ACTIVE
RUNS = {f"p2s{i:02d}": (i, ARM, 4200 + i) for i in range(1, pc.MAX_ATTEMPTS["stage2"] + 1)}
ARTIFACT_ENV = "PHASE2_ARTIFACT_ROOT"


_read, _lines = s1._read, s1._lines


# ---------------- 진입 조건(§8.3) ----------------
def check_outcome(evidence_path) -> str:
    """오프라인 선검사 - SELECTED가 아니면 클러스터 연결 조립 전에 멈춘다."""
    out = _read(evidence_path).get("outcome") or ""
    if not out.startswith("SELECTED:"):
        raise SystemExit(f"Stage 2 금지: Stage 1 결과 {out}(SELECTED만 허용)")
    return out


def entry_check(*, selection_profile_path, active_profile_path, evidence_path, artifact_root, snapshot_fn, impl: dict,
                stage1_dir=None) -> dict:
    """stage1_dir가 주어지면 Stage 1 기록에서 선택 입력을 다시 만들어 증거 파일과 비교한다(실행 진입점은 항상 준다)."""
    import selection as sel
    probs = []
    ev = _read(evidence_path)
    ev_sha = pc.sha256_bytes(evidence_path)
    out = check_outcome(evidence_path)
    recomputed = sel.select(ev.get("inputs") or {})
    if ev.get("rule") != pc.PREREG_SHA256 or ev.get("evidence_id") != f"p2sel-{pc.obj_sha256(ev.get('inputs') or {})[:16]}" \
            or {k: ev.get(k) for k in ("outcome", "reason", "gate_results")} != recomputed:
        probs.append("selection_evidence_not_reproducible")
    if stage1_dir is not None:
        again = sel.collect(stage1_dir, profile_path=selection_profile_path, artifact_root=artifact_root,
                            verify_freeze=lambda: impl)
        if pc.canonical(again["inputs"]) != pc.canonical(ev.get("inputs") or {}):
            probs.append("selection_inputs_differ_from_stage1_records")
    sel = pc.load_profile(selection_profile_path, stage="stage1")
    if sel["sha256"] != impl.get("selection_profile_sha256"):
        probs.append("selection_profile_not_implementation_frozen")
    try:
        act = pc.load_profile(active_profile_path, stage="stage2")
    except pc.ProfileInvalid as e:
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
    except pc.ProfileInvalid as e:
        raise SystemExit(f"Stage 2 금지: {e}") from e
    stale = pc.check_stale(act["profile"], pc.observe_environment((snapshot_fn() or {}).get("template")),
                           pc.contract_hashes()["recovery_policy_sha256"])
    if stale:
        probs.append(f"PROFILE_STALE: {stale}")
    if probs:
        raise SystemExit(f"Stage 2 금지(조치 없음): {probs}")
    return {"loaded": act, "art": art, "evidence_sha256": ev_sha, "selected": act["active"]}


# ---------------- 순서·상한(§8.4·§9) ----------------
def _session_path(d, run):
    return Path(d) / f"p2-s2-session-{run}.json"


def schedule(run, d) -> dict:
    if run not in RUNS:
        raise SystemExit(f"알 수 없는 Stage 2 실행 ID: {run}")
    d = Path(d)
    if _session_path(d, run).exists():
        raise SystemExit(f"{run} 거부: 이미 시도한 ID")
    done = sorted((_read(p) for p in d.glob("p2-s2-session-*.json")), key=lambda r: r["attempt_number"])
    missing = [r["run"] for r in done if not (d / f"p2-s2-validity-{r['run']}.json").exists()]
    if missing:
        raise SystemExit(f"{run} 거부: 이전 시도의 유효성 기록 없음 {missing}")
    if any(r.get("cleanup_errors") for r in done):
        raise SystemExit("중단 규칙: 정리 오류 - 사용자 확인 필요")
    if any((r.get("result") or {}).get("authority_leak") for r in done):
        raise SystemExit("중단: 권한 누수 기록 - PHASE2_NOT_VALIDATED")
    if len(done) >= pc.MAX_ATTEMPTS["stage2"]:
        raise SystemExit(f"중단 규칙: Stage 2 시도 상한 {pc.MAX_ATTEMPTS['stage2']} 도달")
    n_valid = sum(_read(d / f"p2-s2-validity-{r['run']}.json")["valid"] for r in done)
    if n_valid >= pc.N_STAGE2:
        raise SystemExit(f"{run} 거부: 유효 {n_valid}/{pc.N_STAGE2} 이미 채움")
    before = [r for r in RUNS if RUNS[r][0] < RUNS[run][0] and not _session_path(d, r).exists()]
    if before:
        raise SystemExit(f"{run} 거부: 순서 밖 - 먼저 시도할 ID {before}")
    return {"block": RUNS[run][0], "order": ARM, "position": 1, "required_before": [], "attempt_number": len(done) + 1}


def refuse(run, d, reason):
    """Stage 2 진입 거부 - 시도로 기록하고(유효성 = 실행 안 됨) 조치 없이 멈춘다."""
    sched = schedule(run, d)
    rec = {"run": run, "run_id": run_id_for(run), "attempt_number": sched["attempt_number"], "status": "refused_entry",
           "reason": reason, "result": None, "cleanup_errors": [], "recorded_at_utc": datetime.now(timezone.utc).isoformat()}
    pc.write_json_new(_session_path(d, run), rec)
    pc.write_json_new(Path(d) / f"p2-s2-validity-{run}.json", {"run": run, "valid": False, "invalid_reasons": [reason],
                                                                "status": "not_run_refused_entry"})
    raise SystemExit(f"Stage 2 진입 거부(조치 없음): {reason}")


# ---------------- 동결 B/C/K 실행기 재사용 ----------------
def active_starter(profile_path, artifact_root, *, start_subprocess=smoke._start_subprocess,
                   stop_subprocess=smoke._stop_subprocess):
    """gb.bck_starter와 같은 모양 - 스크립트만 p2_executor active(ACTIVE만 송신)."""
    def factory(results_dir, policy_url, expected_version, expected_mode, arm=None):
        def start(run_id, evidence_log_path=None, sink_url=None):
            h = start_subprocess(
                [sys.executable, "-u", str(EXECUTOR), "active", "--run-id", run_id,
                 "--evidence-log", str(gr.live_evidence_path(results_dir, run_id)), "--profile", str(profile_path),
                 "--artifact-root", str(artifact_root), "--policy-url", policy_url,
                 "--expected-version", expected_version, "--expected-mode", expected_mode],
                cwd=pc.HERE, stdout_path=Path(results_dir) / f"p2-active-stdout-{run_id}.log",
                stderr_path=Path(results_dir) / f"p2-active-stderr-{run_id}.log")
            h["ignored_sink_url"] = sink_url

            def stop():
                r = stop_subprocess(h, "p2_executor.py")
                return {**r, "exit_meaning": gr.EXIT_MEANING.get(r.get("returncode"), "terminated_or_unknown")}
            return smoke.DetectorHandle(is_alive=lambda: h["proc"].poll() is None, stop=stop,
                                        label=f"p2_executor active -> {policy_url} ({expected_mode})")
        return start
    return factory


@contextlib.contextmanager
def patched_bck(results_dir, sched, ident):
    """run_guarded_bck·c1_bck_live_detector(파일 수정 없음)의 계열 전역과 identity 함수를 호출 동안만 Phase 2 값으로 바꾼다."""
    names = ("RUNS", "check_schedule", "BCK_PLAN_ID", "BCK_CONTRACT_DOC", "BCK_RESULTS_DIR", "ARM_LABEL")
    saved, saved_ident = {n: getattr(gb, n) for n in names}, gb.bd.signal_identity
    try:
        gb.RUNS, gb.check_schedule = RUNS, (lambda r, _d: sched)
        gb.BCK_PLAN_ID, gb.BCK_CONTRACT_DOC, gb.BCK_RESULTS_DIR = STAGE2_PLAN_ID, pc.PREREG_DOC, Path(results_dir)
        gb.ARM_LABEL = {ARM: f"phase2_active:{ident['detector_id']}"}
        gb.bd.signal_identity = lambda arm, hashes, spec_sha: dict(ident)
        yield gb
    finally:
        for n, v in saved.items():
            setattr(gb, n, v)
        gb.bd.signal_identity = saved_ident


def run_id_for(run: str) -> str:
    return f"guarded_pressure_{gp.MODE_ACT}-{run}-{STAGE2_PLAN_ID}"


def run_session(run, *, deps, entry: dict, active_profile_path, artifact_root, approval_path, policy_url, audit_reader,
                results_dir=None, preflight_only=False, code_provenance=None, starter_factory=None) -> dict:
    """[USER APPROVAL REQUIRED] 실제 deps면 act 압박·실제 승격. entry = entry_check 결과."""
    d = Path(results_dir or RESULTS_DIR)
    d.mkdir(parents=True, exist_ok=True)
    sched = schedule(run, d)
    started = datetime.now(timezone.utc).isoformat()
    ident = pc.signal_identity(entry["selected"], entry["art"]["hashes"])
    deps = copy.copy(deps)
    deps.artifacts = lambda: entry["art"]
    deps.starter_factory = starter_factory or active_starter(active_profile_path, artifact_root)
    with patched_bck(d, sched, ident):
        summary = gb.run_bck(run, deps=deps, policy_url=policy_url, approval_path=approval_path,
                             preflight_only=preflight_only, results_dir=d, code_provenance=code_provenance)
    if preflight_only:
        return {"preflight_only": True, "run_id": run_id_for(run), "schedule": sched}
    ep = gr.live_evidence_path(d, run_id_for(run))
    try:  # 정책이 이 세션 창에 받은 신호 - 감사 기록의 detector 식별(delivery_key 2번째 칸)이 ACTIVE뿐이어야 한다(§8.4)
        summary["policy_decisions"] = [{"delivery_key": x.get("delivery_key"), "outcome": x.get("outcome"),
                                        "detector_id": ((x.get("delivery_key") or "").split(":") + ["", ""])[1]}
                                       for x in s1.policy_decisions(audit_reader(), started, datetime.now(timezone.utc).isoformat())]
    except Exception as e:  # noqa: BLE001 - 읽지 못하면 성공으로 세지 않는다
        summary["policy_decisions_error"] = f"{type(e).__name__}: {e}"
    result = stage2_session_result(summary, _lines(ep), entry["selected"])
    rec = {"run": run, "run_id": run_id_for(run), "attempt_number": sched["attempt_number"], "selected": entry["selected"],
           "environment_profile_sha256": entry["loaded"]["sha256"], "selection_evidence_sha256": entry["evidence_sha256"],
           "runner_started_at_utc": started, "runner_ended_at_utc": datetime.now(timezone.utc).isoformat(),
           "evidence_file": ep.name, "evidence_sha256": pc.sha256_bytes(ep) if ep.exists() else None,
           "outcome_status": summary.get("outcome_status"), "result": result, "code_provenance": code_provenance,
           "cleanup_errors": list(summary.get("cleanup_errors") or [])}
    pc.write_json_new(_session_path(d, run), rec)
    return {"summary": summary, "session_record": rec}


# ---------------- 판정(§8.4·§13, 순수 함수) ----------------
def stage2_session_result(summary: dict, evidence_recs: list, active: str) -> dict:
    """성공 = promoted_verified + 새/이전 Pod 요청 카운터 확인(guard verified) + SHADOW 송신 0 + 정책 context identity가 ACTIVE.
    신호 없음·첫 ACTIVE 송신이 주입 관측 전이면 실패(세션 유효성과 별개). 권한 누수 = ACTIVE 외 identity의 송신·context."""
    import c3_common as cc
    sends = [r for r in evidence_recs if r.get("record_type") == "c1_policy_send_attempt"]
    starts = [r for r in evidence_recs if r.get("record_type") == "c1_live_start"]
    w = summary.get("watcher") or {}
    t_inj = (summary.get("trial_outcome") or {}).get("t_injection_observed_utc")
    ctx = summary.get("context")
    leak = any(r.get("detector_id") != active for r in sends) or (ctx is not None and ctx.get("detector_id") != active) \
        or any((s.get("signal_identity") or {}).get("detector_id") != active for s in starts) \
        or any(d.get("detector_id") != active for d in summary.get("policy_decisions") or [])
    pre_inj = bool(sends and t_inj and cc.ts(sends[0]["attempted_at_utc"]) < cc.ts(t_inj))
    checks = {"signal_sent": bool(sends), "context_set": ctx is not None,
              "policy_audit_read": not summary.get("policy_decisions_error"),
              "promoted_verified": (w.get("action") or {}).get("outcome") == "promoted_verified",
              "traffic_verified": (w.get("guard_result") or {}).get("result") == "verified",
              "no_shadow_send": not leak, "first_send_after_injection": bool(sends) and not pre_inj}
    return {"success": all(checks.values()), "authority_leak": leak, "checks": checks}


def phase2_verdict(stage1_outcome: str, stage2_valid_results: list, *, any_authority_leak: bool = False,
                   preregistration_violation: bool = False) -> dict:
    """§13 판정(어느 detector가 선택되는지와 무관). 순서: NOT_VALIDATED 조건 → VALIDATED → MIXED → PENDING.
    stage2_valid_results = 유효 Stage 2 세션의 stage2_session_result 목록(실행 안 했으면 []). IMPLEMENTATION FREEZE가
    §11 100%를 강제하므로 테스트 조건은 여기서 따로 받지 않는다. any_authority_leak = 유효·무효를 가리지 않은 모든 Stage 2
    시도에서의 권한 누수(§13: 어느 세션이든 누수면 NOT_VALIDATED)."""
    ok = sum(r["success"] for r in stage2_valid_results)
    fail = len(stage2_valid_results) - ok
    selected = stage1_outcome.startswith("SELECTED:")
    if preregistration_violation or any_authority_leak or stage1_outcome in ("SELECTION_INCOMPLETE", "INTEGRITY_FAILURE") \
            or any(r["authority_leak"] for r in stage2_valid_results) or (fail >= 1 and ok == 0) \
            or (not selected and stage2_valid_results):
        v = "PHASE2_NOT_VALIDATED"
    elif selected and len(stage2_valid_results) >= pc.N_STAGE2 and fail == 0:
        v = "PHASE2_ARCHITECTURE_VALIDATED"
    elif selected and ok >= 1 and fail >= 1:
        v = "PHASE2_MIXED"
    elif selected or stage1_outcome == "NO_ELIGIBLE_DETECTOR":
        v = "PHASE2_SELECTION_VALIDATED_RECOVERY_PENDING"
    else:
        v = "PHASE2_NOT_VALIDATED"
    return {"verdict": v, "stage1_outcome": stage1_outcome, "stage2_valid": len(stage2_valid_results), "success": ok, "fail": fail}


def validate(run, d, *, operator: str, probe_dir=None) -> dict:
    """Stage 2 세션 유효성(§8.4) - §8.2 B 항목(정책 결과를 전제하지 않음)만: A/B run_validity에서 V5(전환 결과 의존) 제외."""
    import guarded_ab_analysis as gab
    d = Path(d)
    rec = _read(_session_path(d, run))
    rid = rec["run_id"]
    dd, probe_csv = s1.pressure_run_dict(d, run, probe_dir or gp.PROBE_RESULTS_DIR, rid=rid, rep=RUNS[run][2])
    frozen = {**s1.pressure_frozen(pc.expected_if_hashes()), "threshold": pc.FROZEN_DETECTORS[rec["selected"]]["threshold"]}
    v = gab.run_validity(dd, frozen, gab.primary(dd, probe_csv))
    reasons = [r for r in v["reasons"] if not r.startswith("V5_") or r.startswith("V5_old_policy")]
    reasons += [f"operator_intervention: {operator}"] if operator != "none" else []
    return {"run": run, "run_id": rid, "valid": not reasons, "invalid_reasons": reasons, "operator_intervention": operator,
            "rule": "prereg §8.4 - §8.2 B 항목만(V5 전환 결과 의존 항목 제외), 성공 판정 전에 확정"}


def main(argv=None) -> int:  # pragma: no cover - 라이브 진입점(별도 승인)
    p = argparse.ArgumentParser(description="Phase 2 Stage 2 세션 1회(act, 실제 승격 - 사용자 승인 필요)")
    p.add_argument("--run")
    p.add_argument("--mode", choices=[gp.MODE_ACT], default=gp.MODE_ACT)
    p.add_argument("--policy-url", default="http://127.0.0.1:18090")
    p.add_argument("--session-file", default=None)
    p.add_argument("--act-approval")
    p.add_argument("--preflight-only", action="store_true")
    p.add_argument("--selection-profile", type=Path, default=pc.SELECTION_PROFILE)
    p.add_argument("--active-profile", type=Path, default=os.environ.get("PHASE2_ACTIVE_PROFILE") or None)
    p.add_argument("--selection-evidence", type=Path, default=os.environ.get("PHASE2_SELECTION_EVIDENCE") or None)
    p.add_argument("--artifact-root", type=Path, default=os.environ.get(ARTIFACT_ENV) or None)
    p.add_argument("--record-validity", default=None, metavar="RUN")
    p.add_argument("--operator-intervention", default=None)
    a = p.parse_args(argv)
    if a.record_validity:
        if not a.operator_intervention:
            raise SystemExit("--operator-intervention 필요('none' 또는 개입 내용)")
        v = validate(a.record_validity, RESULTS_DIR, operator=a.operator_intervention)
        pc.write_json_new(RESULTS_DIR / f"p2-s2-validity-{a.record_validity}.json", v)
        print(json.dumps(v, ensure_ascii=False))
        return 0
    if not (a.run and a.active_profile and a.selection_evidence and a.artifact_root):
        raise SystemExit("--run, --active-profile, --selection-evidence, --artifact-root 필요")
    check_outcome(a.selection_evidence)             # SELECTED가 아니면 연결 조립 전에 멈춤
    impl = pc.verify_implementation_freeze()
    token = os.environ.get("GUARDED_ADMIN_TOKEN")
    if not token:
        raise SystemExit("GUARDED_ADMIN_TOKEN 환경변수 필요")
    deps = gb.real_deps(a.policy_url, token, a.session_file)
    base = deps.check_unresolved

    def check_unresolved():
        base()
        rt.check_no_unresolved_failures(RESULTS_DIR)
    deps.check_unresolved = check_unresolved
    try:
        entry = entry_check(selection_profile_path=a.selection_profile, active_profile_path=a.active_profile,
                            evidence_path=a.selection_evidence, artifact_root=a.artifact_root,
                            snapshot_fn=deps.canonical_snapshot, impl=impl, stage1_dir=s1.RESULTS_DIR)
    except SystemExit as e:                         # 진입 거부도 시도로 센다(§9 Stage 2 상한)
        refuse(a.run, RESULTS_DIR, str(e))
    r = run_session(a.run, deps=deps, entry=entry, active_profile_path=a.active_profile, artifact_root=a.artifact_root,
                    approval_path=a.act_approval, policy_url=a.policy_url, preflight_only=a.preflight_only, code_provenance=impl,
                    audit_reader=lambda: s1.read_policy_audit(gp.MODE_ACT))
    s = r.get("summary") or {}
    print(json.dumps({"run": a.run, "outcome": s.get("outcome_status"), "cleanup_errors": s.get("cleanup_errors"),
                      "result": (r.get("session_record") or {}).get("result")}, ensure_ascii=False, default=str))
    return 2 if s.get("cleanup_errors") else 0


if __name__ == "__main__":
    sys.exit(main())
