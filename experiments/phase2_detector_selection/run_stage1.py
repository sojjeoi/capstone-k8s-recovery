#!/usr/bin/env python3
"""Phase 2 Stage 1 - Selection Validation 세션 실행기(2026-10-11) - prereg §8.1·§8.2·§9·§4.4.

record_only 전용이다. act는 인자 단계(--mode choices)와 함수 단계(run_session mode 검사)에서 거부한다.
정책 context를 설정하지 않고, 실행기(p2_executor shadow)에는 송신 경로가 없다(selection profile ACTIVE 0개).
  - 정상(p2nNN): 동결 N1 실행기 run_guarded_normal_bc.run_normal을 수정 없이 호출(계열 전역만 호출 동안 Phase 2 값으로 바꿈 -
    c3 수집과 같은 방식). probe 1 RPS·관찰 4200 s·canonical 시작/끝·미승격 preview 정리 + c1 5 s Pod 표본기(run_c3_collection.c3_starter).
  - 압박(p2pNN): A/B A arm(record_only 압박)과 같은 조건·같은 동결 부품(시행 함수·D5 관문·canonical 복원·정리 검사).
    차이: 정책 context·신호·조치 감시·유예 변경 경로가 없다(기존 압박 실행기는 act 전용이거나 context를 설정한다).
세션마다(클러스터 변경 전) Phase 2 preflight: selection profile(ACTIVE 0)·외부 artifact·rule spec·PROFILE_STALE.
세션 뒤 postflight(§8.1-6): 송신 0·SHADOW 외 역할 0·정책 조치/context 0·active selector 불변·profile 해시 불변 →
하나라도 어긋나면 p2-integrity-failure-<run>.json(INTEGRITY_FAILURE) 기록, 이후 세션 거부.
유효성(--record-validity, 오프라인)은 탐지기 결과를 보지 않고 확정한다 - 정상: c3 정의(N1 analyze_session + c3_reasons),
압박: A/B 공식 run_validity(정책 결과를 전제하지 않는 항목). 선택 재생(selection.py)은 유효성 기록이 있는 세션만 읽는다.
[USER APPROVAL REQUIRED] --plan·--record-validity 외 모드는 클러스터에 접속한다(세션마다 사용자 승인)."""
import sys
sys.dont_write_bytecode = True
import argparse  # noqa: E402
import contextlib  # noqa: E402
import copy  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import signal  # noqa: E402
import threading  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import p2_common as pc  # noqa: E402

import guarded_cleanup_gate as gcg  # noqa: E402
import guarded_live_detector_runner as gr  # noqa: E402
import run_guarded_normal_bc as rn  # noqa: E402
import run_guarded_pilot as gp  # noqa: E402
import run_if_threshold_candidate_smoke as smoke  # noqa: E402
import run_timed_switch_recovery_trial as rt  # noqa: E402
from load_ramp_adapter import IMAGE_FORCE_CLOSE, IMAGE_FORCE_CLOSE_ID  # noqa: E402
from run_once import HarnessCorrupted  # noqa: E402

import c1_shadow_detector as sd  # noqa: E402

MODE = gp.MODE_RO
RESULTS_DIR = pc.EXP / "results" / "phase2_stage1"
EXECUTOR = pc.HERE / "p2_executor.py"
NORMAL_RUNS = {f"p2n{i:02d}": (i, 4000 + i) for i in range(1, pc.MAX_ATTEMPTS["normal"] + 1)}
PRESSURE_RUNS = {f"p2p{i:02d}": (i, 4100 + i) for i in range(1, pc.MAX_ATTEMPTS["pressure"] + 1)}
TARGET = {"normal": pc.N_NORMAL, "pressure": pc.N_PRESSURE}
VALID_STATUS = {"normal": "normal_valid", "pressure": "valid"}
CLOSED_MARKER = "p2-collection-closed.json"   # selection.collect가 처음 실행될 때 쓴다 - 이후 schedule은 영구 거부
ARTIFACT_ENV = "PHASE2_ARTIFACT_ROOT"


def kind_of(run: str) -> str:
    if run in NORMAL_RUNS:
        return "normal"
    if run in PRESSURE_RUNS:
        return "pressure"
    raise SystemExit(f"알 수 없는 Stage 1 실행 ID: {run}")


def run_id_for(run: str) -> str:
    return f"guarded_{kind_of(run)}_{MODE}-{run}-{pc.PLAN_ID}"


def _read(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def _lines(p):
    p = Path(p)
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


def _session_path(d, run):
    return Path(d) / f"p2-session-{run}.json"


def attempts(d) -> list:
    return sorted((_read(p) for p in Path(d).glob("p2-session-*.json")), key=lambda r: r["attempt_number"])


# ---------------- 순서·중단 규칙(§9) - 탐지기 결과는 보지 않는다 ----------------
def stop_reason(d, resume_ack=None, include_closed=True):
    """수집 중단 사유(없으면 None). schedule(다음 세션)과 collection_state(선택 분석 허용)가 같은 규칙을 쓴다."""
    d = Path(d)
    if include_closed and (d / CLOSED_MARKER).exists():
        return "선택 분석이 이미 실행됨(수집 종료) - 결과를 본 뒤 추가 수집 불가(§17)"
    if list(d.glob("p2-integrity-failure-*.json")):
        return "INTEGRITY_FAILURE 기록 있음 - Stage 1 재개 불가(사람 확인)"
    done = attempts(d)
    no_validity = [r["run"] for r in done if not (d / f"p2-validity-{r['run']}.json").exists()]
    if no_validity:
        return f"유효성 기록 없는 시도 {no_validity}"
    if done and str(done[-1].get("status", "")).startswith("refused_") and resume_ack != done[-1]["run"]:
        return (f"{done[-1]['run']} {done[-1]['status']} - 사용자 승인 뒤 --resume-after-refusal "
                f"{done[-1]['run']}(profile·artifact·freeze 변경 없이)")
    if any(r.get("cleanup_errors") for r in done):
        return "중단 규칙: 정리 오류 1회 - 사용자 확인 필요"
    status = [_read(d / f"p2-validity-{r['run']}.json")["status"] for r in done]
    if len(status) >= 2 and status[-1] == status[-2] == "invalid":
        return "중단 규칙: invalid 2회 연속 - 사용자 확인 필요"
    if status.count("unexpected_degradation") >= 2:
        return "중단 규칙: 예상 밖 저하 2회 - 사용자 확인 필요"
    return None


def kind_progress(d, kind) -> dict:
    same = [r for r in attempts(d) if r["kind"] == kind]
    n_valid = sum(_read(Path(d) / f"p2-validity-{r['run']}.json")["status"] == VALID_STATUS[kind] for r in same
                  if (Path(d) / f"p2-validity-{r['run']}.json").exists())
    return {"attempts": len(same), "valid": n_valid,
            "done": n_valid >= TARGET[kind] or len(same) >= pc.MAX_ATTEMPTS[kind]}


def collection_state(d) -> dict:
    """선택 분석 허용 조건(§7.2-0·§17): 수집이 끝났을 때만 - 두 종류 모두 목표 충족 또는 시도 상한, 또는 중단 규칙."""
    stop = stop_reason(d, include_closed=False)     # 닫힘 표시 자체는 상태에 넣지 않음(같은 기록 → 같은 근거)
    prog = {k: kind_progress(d, k) for k in TARGET}
    return {"finished": bool(stop) or all(v["done"] for v in prog.values()), "stop_reason": stop, "progress": prog}


def schedule(run: str, d, resume_ack=None) -> dict:
    kind = kind_of(run)
    d = Path(d)
    if _session_path(d, run).exists():
        raise SystemExit(f"{run} 거부: 이미 시도한 ID(재사용 금지)")
    stop = stop_reason(d, resume_ack)
    if stop:
        raise SystemExit(f"중단: {stop}")
    done = attempts(d)
    same = [r for r in done if r["kind"] == kind]
    prog = kind_progress(d, kind)
    if prog["attempts"] >= pc.MAX_ATTEMPTS[kind]:
        raise SystemExit(f"중단 규칙: {kind} 시도 상한 {pc.MAX_ATTEMPTS[kind]} 도달 → SELECTION_INCOMPLETE")
    if prog["valid"] >= TARGET[kind]:
        raise SystemExit(f"{run} 거부: {kind} 유효 {prog['valid']}/{TARGET[kind]} 이미 채움 - 추가 수집 없음")
    runs = NORMAL_RUNS if kind == "normal" else PRESSURE_RUNS
    before = [r for r in runs if runs[r][0] < runs[run][0] and not _session_path(d, r).exists()]
    if before:
        raise SystemExit(f"{run} 거부: 순서 밖 - 먼저 시도할 ID {before}")
    return {"session": run, "kind": kind, "attempt_number": len(done) + 1, "kind_attempt": len(same) + 1,
            "resume_ack": resume_ack, "required_before": [], "counted": True}


INTEGRITY_CAUSES = ("artifact", "implementation_freeze", "profile_not_frozen")   # §4.4: 재개 못 하면 INTEGRITY_FAILURE


def refuse(run, d, sched, status, reason, started, cause):
    """PROFILE_INVALID/STALE·freeze 불일치 - 시도로 기록(§4.4)하고 non-actionable로 멈춘다. 유효성은 not_run으로 바로 확정.
    cause: artifact / implementation_freeze / profile_not_frozen(재개 못 하면 INTEGRITY_FAILURE) 또는 stale / profile(그 밖)."""
    rec = {"run": run, "run_id": run_id_for(run), "kind": sched["kind"], "attempt_number": sched["attempt_number"],
           "status": status, "reason": reason, "cause": cause, "runner_started_at_utc": started,
           "runner_ended_at_utc": datetime.now(timezone.utc).isoformat(), "cleanup_errors": []}
    pc.write_json_new(_session_path(d, run), rec)
    pc.write_json_new(Path(d) / f"p2-validity-{run}.json",
                      {"run": run, "kind": sched["kind"], "status": f"not_run_{status}", "valid": False, "invalid_reasons": [reason]})
    raise SystemExit(f"{status.upper()} - 세션 시작 안 함(클러스터 변경 없음): {reason}")


def phase2_preflight(snapshot_fn, profile_path, artifact_root) -> dict:
    """읽기 전용: profile(ACTIVE 0)·artifact·rule spec·PROFILE_STALE. ProfileInvalid는 호출부가 refused로 기록."""
    loaded = pc.load_profile(profile_path, stage="stage1")
    art = pc.verify_artifacts(artifact_root)
    pc.verify_rule_spec()
    observed = pc.observe_environment((snapshot_fn() or {}).get("template"))
    stale = pc.check_stale(loaded["profile"], observed, pc.contract_hashes()["recovery_policy_sha256"])
    return {"loaded": loaded, "artifact_hashes": art["hashes"], "observed_environment": observed, "stale": stale}


# ---------------- 실행기 시작(송신 경로 없음) ----------------
def shadow_starter(results_dir, profile_path, artifact_root, *, start_subprocess=smoke._start_subprocess,
                   stop_subprocess=smoke._stop_subprocess):
    def start(run_id, evidence_log_path=None, sink_url=None):
        h = start_subprocess(
            [sys.executable, "-u", str(EXECUTOR), "shadow", "--run-id", run_id,
             "--evidence-log", str(gr.live_evidence_path(results_dir, run_id)), "--profile", str(profile_path),
             "--artifact-root", str(artifact_root)],
            cwd=pc.HERE, stdout_path=Path(results_dir) / f"p2-shadow-stdout-{run_id}.log",
            stderr_path=Path(results_dir) / f"p2-shadow-stderr-{run_id}.log")
        h["ignored_sink_url"] = sink_url

        def stop():
            r = stop_subprocess(h, "p2_executor.py")
            return {**r, "exit_meaning": {0: "ok", 2: "evidence_write_failed", 7: "profile_invalid"}.get(
                r.get("returncode"), "terminated_or_unknown")}
        return smoke.DetectorHandle(is_alive=lambda: h["proc"].poll() is None, stop=stop, label="p2_executor shadow (송신 없음)")
    return start


def logging_snapshot(fn, path):
    """deps.canonical_snapshot(읽기만)을 감싸 매 호출 결과를 JSONL로 남긴다(template 제외) - c3 수집과 같은 기록."""
    def wrapped():
        s = fn()
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({k: v for k, v in (s or {}).items() if k != "template"}, ensure_ascii=False, default=str) + "\n")
        return s
    return wrapped


@contextlib.contextmanager
def patched_n1(results_dir, sched):
    """run_guarded_normal_bc(파일 수정 없음)의 계열 전역을 Phase 2 값으로 바꾸고 finally에서 되돌린다(c3_common.patched_n1과 같은 방식)."""
    names = ("PLAN_ID", "CONTRACT_DOC", "RESULTS_DIR", "RUNS", "check_schedule")
    saved = {n: getattr(rn, n) for n in names}
    try:
        rn.PLAN_ID, rn.CONTRACT_DOC, rn.RESULTS_DIR = pc.PLAN_ID, pc.PREREG_DOC, Path(results_dir)
        rn.RUNS, rn.check_schedule = NORMAL_RUNS, (lambda r, _d: sched)
        yield rn
    finally:
        for n, v in saved.items():
            setattr(rn, n, v)


# ---------------- 압박 세션(A/B A arm 조건, context·신호·조치 경로 없음) ----------------
def pressure_conditions() -> dict:
    return {"stress_stages": rt.DEMO_STAGES, "probe_config": str(smoke.DEFAULT_PROBE_CONFIG),
            "probe_config_sha256": hashlib.sha256(smoke.DEFAULT_PROBE_CONFIG.read_bytes()).hexdigest(),
            "probe_image": IMAGE_FORCE_CLOSE, "probe_image_id": IMAGE_FORCE_CLOSE_ID,
            "detector_rule": {"consecutive": sd.CONSECUTIVE_THRESHOLD, "cooldown_sec": sd.COOLDOWN_SEC},
            "start_state_rule": "canonical_healthy_r2"}


def pressure_frozen(art_hashes: dict) -> dict:
    """A/B run_validity V4 비교값(Phase 2 계약값) - 실행기가 계약에 쓰는 값과 같은 함수에서 만든다."""
    return {**pressure_conditions(), "threshold": {d: pc.FROZEN_DETECTORS[d]["threshold"] for d in pc.DETECTOR_IDS},
            "artifact_hashes": art_hashes, "observation_sec": gp.PRESSURE_OBSERVATION_SEC}


# ponytail: 동결 run_guarded의 record_only 압박 경로 순서 복사(~180줄) - 동결 실행기 중 압박+record_only+context 없음 경로가
# 없고 동결 파일 수정은 금지. 동결 실행기에 같은 경로가 생기면 그것으로 대체.
def run_pressure(run, *, deps, starter, art_hashes, profile_sha, results_dir, preflight_only=False, code_provenance=None):
    rep = PRESSURE_RUNS[run][1]
    run_id = run_id_for(run)
    summary_path = results_dir / f"summary-{run_id}.json"
    if summary_path.exists() or gr.live_evidence_path(results_dir, run_id).exists():
        raise SystemExit(f"실행 ID 재사용 금지: {run_id}")
    deps.check_unresolved()
    obs_sec = gp.PRESSURE_OBSERVATION_SEC
    fz = pressure_frozen(art_hashes)
    contract = {"run_id": run_id, "run": run, "plan_id": pc.PLAN_ID, "contract_doc": pc.PREREG_DOC,
                "comparison": {"series": pc.PLAN_ID, "stage": "stage1", "session": run, "counted_in_comparison": True},
                "conditions": pressure_conditions(), "kind": "pressure", "mode": MODE, "actuating_rule": None,
                "signal_send": False, "detector": "phase2_same_tick_v1", "threshold": fz["threshold"],
                "artifact_hashes": art_hashes, "observation_sec": obs_sec, "ready_timeout_sec": gp.READY_TIMEOUT_SEC,
                "scale_down_delay": {"base": gp.BASE_SCALE_DOWN_DELAY_SEC, "changed": False}, "act_approval": None,
                "no_promotion_path": True, "environment_profile_sha256": profile_sha}
    if code_provenance is not None:
        contract["code_provenance"] = code_provenance
    # ---- 이하 동결 run_guarded(pressure·record_only·canonical r2) 순서와 같은 함수. context·조치 감시·유예 변경 없음 ----
    pol = deps.policy
    policy_check = gp.check_policy(pol, MODE, None)
    session_start = deps.session_check()
    ssh_ev = deps.make_ssh_evidence(results_dir / f"ssh-evidence-{run_id}")
    run_guard_disk_fn = ssh_ev.disk_fn(deps.run_guard_nodes)
    preflight = deps.preflight(pol, disk_check_fn=ssh_ev.disk_fn(deps.preflight_nodes))
    try:
        delay_before = deps.read_delay()
    except Exception as e:  # noqa: BLE001
        delay_before = f"read_error: {type(e).__name__}: {e}"
    try:
        old_policy = deps.old_policy_check()
    except Exception as e:  # noqa: BLE001
        old_policy = {"isolated": False, "reasons": [f"check_error: {type(e).__name__}: {e}"]}
    reasons = list(preflight.get("reasons") or []) + policy_check["problems"] + list(session_start.get("problems") or [])
    if old_policy.get("isolated") is not True:
        reasons.append(f"old_recovery_policy_not_isolated: {old_policy.get('reasons')}")
    if delay_before != gp.BASE_SCALE_DOWN_DELAY_SEC:
        reasons.append(f"scaleDownDelaySeconds={delay_before} != {gp.BASE_SCALE_DOWN_DELAY_SEC}")
    try:
        snap = deps.canonical_snapshot()
        cp = gcg.check_canonical(snap, delay=gp.BASE_SCALE_DOWN_DELAY_SEC)
    except Exception as e:  # noqa: BLE001
        snap, cp = None, [f"snapshot_error: {type(e).__name__}: {e}"]
    p0 = gcg.active_pod(snap or {})
    start_pod = (p0["name"], p0["uid"]) if p0 else None
    preflight["canonical_start"] = {"problems": cp, "start_pod": start_pod, "state": {k: (snap or {}).get(k) for k in (
        "phase", "abort", "active", "stable", "current", "preview", "delay", "pods", "svc_selectors", "endpoints", "rs")}}
    reasons += [f"canonical_start: {x}" for x in cp]
    preflight.update(policy=policy_check, session=session_start, scale_down_delay_before=delay_before, old_policy=old_policy,
                     ok=not reasons, reasons=reasons, ssh_check_evidence=ssh_ev.summary())
    preflight_file = results_dir / (f"preflight-{run_id}-{'preflight-only' if preflight_only else 'run'}-"
                                    f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.json")
    with open(preflight_file, "x", encoding="utf-8") as fh:
        fh.write(json.dumps(preflight, ensure_ascii=False, indent=2, default=str))
    if reasons:
        raise SystemExit(f"사전검사 실패 - 시작 안 함(클러스터 변경 없음): {reasons}")
    if preflight_only:
        return {"preflight_only": True, "run_id": run_id, "preflight": preflight, "preflight_file": preflight_file.name}
    (results_dir / f"contract-{run_id}.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")

    events_since = datetime.now(timezone.utc).isoformat()
    running_marker = results_dir / f"running-{run_id}.json"
    running_marker.write_text(json.dumps({"run_id": run_id, "started_at": events_since}), encoding="utf-8")
    evidence_path = gr.live_evidence_path(results_dir, run_id)
    state, cleanup_errors, probe_image_check = {}, [], {}
    run_guard = {"mem_drop_abort_gib": 1.0, "disk_recheck_sec": 60.0}
    outcome, trial_error, injector, canonical_end, rollout, detector = None, None, None, None, None, {"started": False}

    def assess_now(prep_info=None):  # D5 - 승격 근거가 없어야 미승격 preview를 정리한다
        prep_info = prep_info or gate.prep_info
        return gcg.assess(run_id=run_id, mode=MODE, policy_state_fn=pol.state, ledger_fn=deps.ledger_read,
                          evidence_path=evidence_path, view_fn=deps.rollout_view, rollout_uid=(rollout or {}).get("uid"),
                          old_active=(prep_info or {}).get("pre_prepare_active_selector"), prep_info=prep_info,
                          detector_expected=detector["started"])
    gate = gcg.PreviewCleanupGate(assess_now, deps.cleanup_unpromoted_preview,
                                  restore_fn=lambda prep: deps.canonical_restore(prep, start_pod))
    old_policy_checks = {"n": 0, "last": None}

    def check_old_policy():
        try:
            r = deps.old_policy_check()
        except Exception as e:  # noqa: BLE001
            r = {"isolated": False, "reasons": [f"check_error: {type(e).__name__}: {e}"]}
        old_policy_checks["n"] += 1
        old_policy_checks["last"] = {k: r.get(k) for k in ("isolated", "reasons")}
        if r.get("isolated") is not True:
            raise HarnessCorrupted(f"기존 recovery-policy 승격 경로가 열림 - 중단: {r.get('reasons')}")

    def _terminate(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")
    old_handlers = ({getattr(signal, s): signal.signal(getattr(signal, s), _terminate)
                     for s in ("SIGTERM", "SIGBREAK") if hasattr(signal, s)}
                    if threading.current_thread() is threading.main_thread() else {})
    try:
        rollout = deps.rollout_identity()
        injector = deps.make_injector(run_id, rep, "pressure", gate.cleanup)
        base_prepare = injector.prepare

        def prepare_isolated():
            check_old_policy()
            base_prepare()
        injector.prepare = prepare_isolated
        rt.compose_prepare(injector, arm="control", expected_start_node=None, state=state, apply_override_fn=lambda: None)
        prober = deps.make_prober(run_id, rep, "pressure", gp.READY_TIMEOUT_SEC + obs_sec)
        base = deps.base_safety(injector, "pressure")

        def safety(baseline_state, prober_):
            base(baseline_state, prober_)
            deps.run_guard_check(run_guard, injector, state, disk_fn=run_guard_disk_fn)
            deps.probe_image_check(probe_image_check)
            check_old_policy()
        start_shadow = starter(results_dir)

        def detector_start(rid, path, sink_url):
            detector["started"] = True
            return start_shadow(rid, path, sink_url)
        outcome = deps.trial_pressure(
            run_id, injector, prober, deps.pick_port(), evidence_path, results_dir / f"sink-captured-{run_id}.jsonl",
            deps.probe_csv_path(run_id, rep), post_injection_observation_sec=obs_sec, check_safety_fn=safety,
            detector_start_fn=detector_start, sleep_fn=deps.sleep_fn, monotonic_fn=deps.monotonic_fn,
            detector_ready_timeout_sec=gp.READY_TIMEOUT_SEC, detector_ready_fn=lambda: sd.first_scored_evaluation(evidence_path))
    except BaseException as e:  # noqa: BLE001 - 하네스 오류·중단 신호도 요약·정리 확인을 남긴다
        trial_error = f"{type(e).__name__}: {e}"
        outcome = {"status": "harness_error", "reason": trial_error, "cleanup_status": "unknown"}
    finally:
        policy_after = None
        try:
            policy_after = pol.state()
        except Exception as e:  # noqa: BLE001
            cleanup_errors.append(f"policy_state_after_failed: {type(e).__name__}: {e}")
        if gate.held:
            cleanup_errors.append(f"preview_cleanup_held: reasons={gate.held['reasons']} "
                                  f"attempt_evidence={gate.held['attempt_evidence']} remaining={gate.held['remaining']}")
        try:
            delay_after = deps.read_delay()
        except Exception as e:  # noqa: BLE001
            delay_after = f"read_error: {type(e).__name__}: {e}"
        if delay_after != gp.BASE_SCALE_DOWN_DELAY_SEC:
            cleanup_errors.append(f"scale_down_delay_after={delay_after} != {gp.BASE_SCALE_DOWN_DELAY_SEC}")
        if state.get("override") or state.get("override_error"):
            cleanup_errors.append("record_only에서 유예 변경 흔적 - 수동 확인")
        for k in ("unresolved", "blocked", "context"):
            if (policy_after or {}).get(k):
                cleanup_errors.append(f"policy_{k}_after: {policy_after[k]}")
        try:
            old_policy_end = deps.old_policy_check()
        except Exception as e:  # noqa: BLE001
            old_policy_end = {"isolated": False, "reasons": [f"check_error: {type(e).__name__}: {e}"]}
        if old_policy_end.get("isolated") is not True:
            cleanup_errors.append(f"old_recovery_policy_not_isolated_at_end: {old_policy_end.get('reasons')}")
        if outcome.get("cleanup_status") != "complete":
            cleanup_errors.append(f"trial_cleanup_incomplete: prober={outcome.get('prober_stop_failed')}, "
                                  f"injector={outcome.get('injector_cleanup_failed')}")
        if injector is not None:
            try:
                left = deps.wait_chaos_gone(injector.get_stress_delivery_record().get("planned_cr_names") or [])
                if left:
                    cleanup_errors.append(f"stress_chaos_residual: {left}")
            except Exception as e:  # noqa: BLE001
                cleanup_errors.append(f"stress_chaos_check_failed: {type(e).__name__}: {e}")
        if gate.restore_failed is not None:
            cleanup_errors.append(f"canonical_restore_failed: {gate.restore_failed.get('problems')}")
        canonical_end = gcg.wait_canonical(deps.canonical_snapshot, delay=gp.BASE_SCALE_DOWN_DELAY_SEC,
                                           sleep=deps.sleep_fn, monotonic=deps.monotonic_fn)
        if not canonical_end["ok"]:
            cleanup_errors.append(f"canonical_end_not_reached: {canonical_end['problems']}")
        if trial_error:
            cleanup_errors.append(f"harness_error: {trial_error}")
            try:
                left = [p["name"] for p in deps.read_probe_pods() if not p["terminating"]]
                if left:
                    cleanup_errors.append(f"probe_pod_residual: {left}")
            except Exception as e:  # noqa: BLE001
                cleanup_errors.append(f"probe_pod_check_failed: {type(e).__name__}: {e}")
        for sig_, handler in old_handlers.items():
            signal.signal(sig_, handler)

    pod_events_error = None
    try:
        (results_dir / f"pod-events-{run_id}.json").write_text(
            json.dumps(deps.capture_events(events_since), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        pod_events_error = f"{type(e).__name__}: {e}"
    summary = {"run_id": run_id, "run": run, "kind": "pressure", "mode": MODE, "outcome_status": outcome.get("status"),
               "outcome_reason": outcome.get("reason"), "invalidity_reasons": outcome.get("invalidity_reasons"),
               "detector_readiness": outcome.get("detector_readiness") or {}, "evidence_counts": gp.evidence_counts(evidence_path),
               "watcher": {"action": None}, "context": None, "rollout": rollout, "policy_state_after": policy_after,
               "scale_down_delay_before": delay_before, "scale_down_delay_after": delay_after,
               "session_start": session_start, "session_end": deps.session_check(), "pod_events_error": pod_events_error,
               "probe_image_runtime_check": probe_image_check, "start_active_node": state.get("start_active_node"),
               "harness_error": trial_error, "cleanup_errors": cleanup_errors,
               "observation_ended_at_utc": outcome.get("observation_ended_at_utc"),
               "old_policy_isolation": {"start": old_policy, "checks": old_policy_checks,
                                        "end": {k: old_policy_end.get(k) for k in ("isolated", "reasons")}},
               "trial_outcome": outcome, "preview_cleanup_gate": gate.log, "ssh_check_evidence": ssh_ev.summary(),
               "run_guard": {k: v for k, v in run_guard.items() if not k.startswith("_")},
               "comparison": contract["comparison"], "preflight_file": preflight_file.name, "canonical_end": canonical_end,
               "detector_started": detector["started"], "cpu_raw_counter_samples": None, "cpu_raw_counter_error": None}
    try:
        summary["stress_delivery_record"] = injector.get_stress_delivery_record() if injector is not None else None
    except Exception as e:  # noqa: BLE001
        summary["stress_delivery_record"] = {"error": f"{type(e).__name__}: {e}"}
    try:  # 압박 전달 근거(기록 전용 - 판정은 유효성 단계 run_validity V3)
        tgt = (summary["stress_delivery_record"] or {}).get("target") or {}
        t0 = outcome.get("t_injection_observed_utc")
        if not tgt.get("name") or not t0:
            raise RuntimeError(f"target_or_injection_time_missing: {tgt.get('name')}/{t0}")
        summary["cpu_raw_counter_samples"] = deps.cpu_raw_counter(tgt["name"], datetime.fromisoformat(t0))
    except Exception as e:  # noqa: BLE001
        summary["cpu_raw_counter_error"] = f"{type(e).__name__}: {e}"
    if code_provenance is not None:
        summary["code_provenance"] = code_provenance
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if not cleanup_errors:
        running_marker.unlink()
    return summary


# ---------------- 세션 뒤 무결성(§8.1-6) ----------------
def policy_decisions(audit: list, start: str, end: str, *, any_decision: bool = False) -> list:
    """정책 감사 기록 중 세션 창 안에 받은 신호(전달 키가 있는 판단 - decision·promotion_result 등, 전달 키당 1개)
    - 정책이 실제로 받은 신호(§8.1-6 '정책 수신 신호', §8.4 감사 기록 identity)."""
    import c3_common as cc
    t0, t1 = cc.ts(start), cc.ts(end)
    out = {}
    for i, r in enumerate(audit):
        keyed = bool(r.get("delivery_key")) or (any_decision and r.get("event") == "decision")
        if keyed and r.get("received_at") and t0 <= cc.ts(r["received_at"]) <= t1:
            out.setdefault(r.get("delivery_key") or f"unkeyed:{i}", r)   # any_decision: 형식 거부 등 키 없는 판단도 수신
    return list(out.values())


def postflight(summary: dict, evidence_path, snapshots: list, profile_path, start_profile_sha: str,
               decisions=None) -> tuple:
    """(무결성 위반, 세션 무효 사유). 위반 = 승격·조치·actionable signal·정책 수신·SHADOW 외 역할·selector/profile 변경
    (→ INTEGRITY_FAILURE). 근거 없음(탐지기 시작 전 하네스 오류로 evidence·스냅샷 없음, 감사 기록 못 읽음)은 무효 사유."""
    p, invalid = [], []
    recs = _lines(evidence_path)
    if decisions is None:
        invalid.append("policy_audit_unreadable")
    elif decisions:
        p.append(f"policy_received_signals: {len(decisions)}")
    if any(r.get("record_type") in ("c1_policy_send_attempt", "c1_policy_send_result", "c1_policy_payload_error") for r in recs) \
            or (summary.get("evidence_counts") or {}).get("send_attempts"):
        p.append("actionable_signal_sent")
    starts = [r for r in recs if str(r.get("record_type", "")).endswith("_start")]
    if not recs:
        invalid.append("executor_evidence_missing")
    elif not starts or any(r.get("record_type") != "c1_shadow_start" or r.get("active_detector") is not None
                           or set((r.get("roles") or {}).values()) != {pc.SHADOW} for r in starts):
        p.append("executor_not_shadow_only")
    if any(j.get("role") != pc.SHADOW for r in recs if r.get("record_type") == "c1_evaluation"
           for j in (r.get("rules") or {}).values()):
        p.append("non_shadow_rule_record")
    acts = (summary.get("policy_state_after") or {}).get("actions") or {}
    acts = acts.values() if isinstance(acts, dict) else acts
    if any((a or {}).get("run_id") == summary.get("run_id") for a in acts):
        p.append("policy_action_for_this_run")
    if (summary.get("policy_state_after") or {}).get("context") or summary.get("context"):
        p.append("policy_context_set")
    actives = {s.get("active") for s in snapshots if s.get("active")}
    if not snapshots:
        invalid.append("k8s_snapshots_missing")
    elif len(actives) != 1:
        p.append(f"active_selector_changed: {sorted(actives)}")
    if pc.text_sha256(profile_path) != start_profile_sha:
        p.append("profile_changed_during_session")
    return p, invalid


# ---------------- 세션 1회 ----------------
def run_session(run, *, deps, profile_path, artifact_root, impl: dict, audit_reader, mode=MODE, results_dir=None,
                preflight_only=False, resume_ack=None, power_events_fn=None, normal_fn=None, starter_fn=None,
                sampler_cls=None) -> dict:
    """[USER APPROVAL REQUIRED] 실제 deps면 클러스터 접속·workload. 테스트는 가짜 deps·starter_fn.
    impl = verify_implementation_freeze() 결과(selection profile 해시 고정), audit_reader = 정책 감사 기록 읽기(읽기 전용)."""
    code_provenance = impl
    if mode != MODE:
        raise SystemExit(f"Stage 1은 record_only 전용 - mode={mode!r} 거부(조치 경로 없음)")
    import run_c3_collection as rc
    d = Path(results_dir or RESULTS_DIR)
    d.mkdir(parents=True, exist_ok=True)
    sched = schedule(run, d, resume_ack)
    started = datetime.now(timezone.utc).isoformat()
    run_id = run_id_for(run)
    if not Path(profile_path).is_file() or pc.text_sha256(profile_path) != (impl or {}).get("selection_profile_sha256"):
        refuse(run, d, sched, "refused_profile_invalid", "selection profile이 implementation freeze 값과 다름", started,
               "profile_not_frozen")
    try:
        pf = phase2_preflight(deps.canonical_snapshot, profile_path, artifact_root)
    except pc.ArtifactMismatch as e:
        refuse(run, d, sched, "refused_profile_invalid", str(e), started, "artifact")
    except pc.ProfileInvalid as e:
        refuse(run, d, sched, "refused_profile_invalid", str(e), started, "profile")
    if pf["stale"]:
        refuse(run, d, sched, "refused_profile_stale", f"PROFILE_STALE: {pf['stale']}", started, "stale")
    profile_sha = pf["loaded"]["sha256"]
    deps = copy.copy(deps)                      # 호출자 객체는 바꾸지 않음
    snaps_path = d / f"p2-k8s-snapshots-{run}.jsonl"
    deps.canonical_snapshot = logging_snapshot(deps.canonical_snapshot, snaps_path)
    deps.artifacts = lambda: {"hashes": pf["artifact_hashes"], "threshold": pc.IF_THRESHOLD}  # 계약 기록 = 실제 IF 후보(c3-A)
    starter = starter_fn or (lambda rd: shadow_starter(rd, profile_path, artifact_root))
    box, lifecycle_errors = {"injector": None}, []
    if sched["kind"] == "normal":
        base_make_injector = deps.make_injector

        def make_injector(*a, **k):             # 표본기가 공식 c1처럼 injector 대상(active target)을 읽도록 보관
            box["injector"] = base_make_injector(*a, **k)
            return box["injector"]
        deps.make_injector = make_injector
        with patched_n1(d, sched):
            summary = (normal_fn or rn.run_normal)(
                run, deps=deps, preflight_only=preflight_only, results_dir=d, code_provenance=code_provenance,
                starter=lambda rd: rc.c3_starter(starter(rd), rd, box, lifecycle_errors, sampler_cls=sampler_cls))
    else:
        summary = run_pressure(run, deps=deps, starter=starter, art_hashes=pf["artifact_hashes"], profile_sha=profile_sha,
                               results_dir=d, preflight_only=preflight_only, code_provenance=code_provenance)
    if preflight_only:
        return {"preflight_only": True, "schedule": sched, "run_id": run_id, "phase2_preflight": {
            "profile_sha256": profile_sha, "stale": pf["stale"], "artifact_hashes": pf["artifact_hashes"]}}
    ended = datetime.now(timezone.utc).isoformat()
    try:
        power, power_err = (power_events_fn or rc.windows_power_events)(started, ended), None
    except Exception as e:  # noqa: BLE001
        power, power_err = None, f"{type(e).__name__}: {e}"
    evidence_path = gr.live_evidence_path(d, run_id)
    try:
        decisions = policy_decisions(audit_reader(), started, ended, any_decision=True)
    except Exception:  # noqa: BLE001 - 읽지 못하면 확인 불가(세션 무효 사유)
        decisions = None
    problems, post_invalid = postflight(summary, evidence_path, _lines(snaps_path), profile_path, profile_sha, decisions)
    rec = {"run": run, "run_id": run_id, "kind": sched["kind"], "attempt_number": sched["attempt_number"], "status": "run",
           "plan_id": pc.PLAN_ID, "environment_profile_id": pf["loaded"]["profile"]["profile_id"],
           "environment_profile_sha256": profile_sha, "observed_environment": pf["observed_environment"],
           "artifact_hashes": pf["artifact_hashes"], "runner_started_at_utc": started, "runner_ended_at_utc": ended,
           "evidence_file": evidence_path.name,
           "evidence_sha256": pc.sha256_bytes(evidence_path) if evidence_path.exists() else None,
           "k8s_snapshots_file": snaps_path.name, "power_events": power, "power_events_error": power_err,
           "outcome_status": summary.get("outcome_status"), "integrity": {"ok": not problems, "problems": problems},
           "postflight_invalid_reasons": post_invalid,
           "code_provenance": code_provenance, "resume_ack": resume_ack,
           "cleanup_errors": list(summary.get("cleanup_errors") or []) + lifecycle_errors}
    if sched["kind"] == "normal":
        rec.update(rc.identity_metadata(box["injector"], d / f"samples-{run_id}.jsonl"), sampler_lifecycle_errors=lifecycle_errors)
    pc.write_json_new(_session_path(d, run), rec)
    if problems:
        pc.write_json_new(d / f"p2-integrity-failure-{run}.json", {"run": run, "run_id": run_id, "problems": problems,
                                                                    "recorded_at_utc": ended, "outcome": "INTEGRITY_FAILURE"})
    return {"summary": summary, "session_record": rec}


# ---------------- 유효성(오프라인, 탐지기 결과 미사용) ----------------
def pressure_run_dict(d, run, probe_dir, rid=None, rep=None):
    """A/B 분석기 read_run과 같은 모양(Phase 2 실행 ID·반복 번호 - Stage 2는 rid·rep를 넘긴다)."""
    rid, rep = rid or run_id_for(run), rep or PRESSURE_RUNS[run][1]
    files = {"summary": d / f"summary-{rid}.json", "contract": d / f"contract-{rid}.json",
             "probe_csv": Path(probe_dir) / f"probe-{rid}-{rt.ARM_LABEL}-{rep}-raw.csv"}
    out = {"run": run, "run_id": rid, "missing": []}
    for k in ("summary", "contract"):
        if files[k].exists():
            out[k] = _read(files[k])
        else:
            out["missing"].append(k)
    pf = (out.get("summary") or {}).get("preflight_file")
    if pf and (d / pf).exists():
        out["preflight"] = _read(d / pf)
    else:
        out["missing"].append("run_preflight")
    if not files["probe_csv"].exists():
        out["missing"].append("probe_csv")
    return out, files["probe_csv"]


@contextlib.contextmanager
def blinded_n1_loader(nab):
    """N1 analyze_session은 유효성 판정 뒤 B·C·K 결과(rules)를 계산한다(결과는 여기서 버림). Phase 2 기록에는 B/C/K가 없으므로
    읽은 평가의 rules를 중립 값(발화 없음)으로 바꿔 넘긴다 - 유효성 판정은 탐지기 결과를 보지 않는다(§7.2-0)."""
    orig = nab.cspa.load_shadow
    neutral = {"condition_met": False, "condition_met_at_utc": None}

    def load(path, run_id):
        evals, reasons = orig(path, run_id)
        return [{**r, "rules": {a: dict(neutral) for a in ("B", "C", "K")}} for r in evals], reasons
    nab.cspa.load_shadow = load
    try:
        yield
    finally:
        nab.cspa.load_shadow = orig


def validate(run, d, *, operator: str, probe_dir=None) -> dict:
    """세션 운영 유효성(§9) - 점수·이상·신호는 읽지 않는다."""
    import c3_common as cc
    import c3_session_validity as c3v
    import guarded_ab_analysis as gab
    import guarded_normal_bc_analysis as nab
    d = Path(d)
    rec = _read(_session_path(d, run))
    if rec.get("status") != "run":
        raise SystemExit(f"{run}: 실행되지 않은 시도(유효성은 거부 시점에 확정됨)")
    post = list(rec.get("postflight_invalid_reasons") or [])
    if rec["kind"] == "normal":
        with patched_n1(d, None), blinded_n1_loader(nab):
            sess = nab.read_session(d, run)
            base = nab.analyze_session(sess)
        base = {k: base.get(k) for k in c3v.OUT_KEYS}
        extra = [f"missing:{sess['missing']}"] if sess.get("missing") else []
        if not extra:
            summary, contract = sess.get("summary") or {}, sess.get("contract") or {}
            obs_s, obs_e = summary.get("observation_started_at_utc"), summary.get("observation_ended_at_utc")
            if not obs_s or not obs_e:
                extra.append("observation_window_missing")
            else:
                extra += c3v.c3_reasons(summary=summary, contract=contract, evals=cc.read_evals(sess["evidence_path"], rec["run_id"]),
                                        snapshots=_lines(d / rec["k8s_snapshots_file"]), c3rec=rec, operator=operator,
                                        obs_start=cc.ts(obs_s), obs_end=cc.ts(obs_e))
        extra += post
        status = c3v.classify(base, extra)
        return {"run": run, "run_id": rec["run_id"], "kind": "normal", "status": status, "valid": status == VALID_STATUS["normal"],
                "invalid_reasons": (base.get("invalid_reasons") or []) + extra, "t0_utc": base.get("t0_utc"), "base": base,
                "evidence_file": rec["evidence_file"], "operator_intervention": operator,
                "rule": "prereg §8.2 A·§9 - c3 정상 정의, 탐지기 결과 미사용"}
    dd, probe_csv = pressure_run_dict(d, run, probe_dir or gp.PROBE_RESULTS_DIR)
    v = gab.run_validity(dd, pressure_frozen(pc.expected_if_hashes()), gab.primary(dd, probe_csv))
    reasons = list(v["reasons"]) + post + ([f"operator_intervention: {operator}"] if operator != "none" else [])
    return {"run": run, "run_id": rec["run_id"], "kind": "pressure", "status": "invalid" if reasons else "valid",
            "valid": not reasons, "invalid_reasons": reasons,
            "t_injection_observed_utc": ((dd.get("summary") or {}).get("trial_outcome") or {}).get("t_injection_observed_utc"),
            "stress": v["notes"].get("stress"), "evidence_file": rec["evidence_file"], "operator_intervention": operator,
            "rule": "prereg §8.2 B·§9 - A/B 공식 run_validity(A arm 경로), 정책 결과 미전제"}


def read_policy_audit(mode: str) -> list:  # pragma: no cover - 라이브 전용(정책 Pod 감사 파일 읽기만 - gcg.read_ledger와 같은 방식)
    import subprocess
    cp = subprocess.run(["kubectl", "-n", "vllm-serving", "exec", "deploy/guarded-recovery-policy", "--", "cat",
                         f"/data/guarded/{mode}/audit.jsonl"], capture_output=True, text=True, encoding="utf-8", timeout=60)
    if cp.returncode != 0:
        raise RuntimeError(f"audit read rc={cp.returncode}: {cp.stderr[-300:]}")
    return [json.loads(x) for x in cp.stdout.splitlines() if x.strip()]


def plan_text(d) -> str:
    d = Path(d)
    done = {r["run"] for r in attempts(d)} if d.exists() else set()
    lines = []
    for kind, runs in (("normal", NORMAL_RUNS), ("pressure", PRESSURE_RUNS)):
        for r in runs:
            vp = d / f"p2-validity-{r}.json"
            lines.append(f"{r} {kind:<8} {'시도함' if r in done else '-'} {_read(vp)['status'] if vp.exists() else ''}")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Phase 2 Stage 1 세션 1회(record_only 전용). [USER APPROVAL REQUIRED] "
                                            "--plan·--record-validity 외 모드는 클러스터 접속")
    p.add_argument("--run")
    p.add_argument("--mode", choices=[MODE], default=MODE)   # act는 여기서 거부(argparse 오류 종료)
    p.add_argument("--policy-url", default="http://127.0.0.1:18090")
    p.add_argument("--session-file", default=None)
    p.add_argument("--preflight-only", action="store_true")
    p.add_argument("--profile", type=Path, default=pc.SELECTION_PROFILE)
    p.add_argument("--artifact-root", type=Path, default=os.environ.get(ARTIFACT_ENV) or None,
                   help=f"c3-A 외부 artifact 폴더(또는 환경 변수 {ARTIFACT_ENV}) - 기본 경로 없음")
    p.add_argument("--resume-after-refusal", default=None)
    p.add_argument("--plan", action="store_true")
    p.add_argument("--record-validity", default=None, metavar="RUN")
    p.add_argument("--operator-intervention", default=None)
    a = p.parse_args(argv)
    if a.plan:
        print(plan_text(RESULTS_DIR))
        return 0
    if a.record_validity:  # pragma: no cover - 파일 쓰기 진입점(오프라인)
        if not a.operator_intervention:
            raise SystemExit("--operator-intervention 필요('none' 또는 개입 내용)")
        v = validate(a.record_validity, RESULTS_DIR, operator=a.operator_intervention)
        pc.write_json_new(RESULTS_DIR / f"p2-validity-{a.record_validity}.json", v)
        print(json.dumps({k: v[k] for k in ("run", "status", "invalid_reasons")}, ensure_ascii=False))
        return 0
    if not a.run or a.artifact_root is None:
        raise SystemExit(f"--run과 --artifact-root(또는 {ARTIFACT_ENV}) 필요")
    try:
        prov = pc.verify_implementation_freeze()    # 클러스터 연결 조립 전 fail-closed
    except SystemExit as e:                         # 동결 manifest가 있는데 불일치면 시도로 기록(§4.4), 동결 전이면 기록 없이 종료
        if not (pc.ROOT / pc.IMPLEMENTATION_FREEZE).is_file():
            raise
        refuse(a.run, RESULTS_DIR, schedule(a.run, RESULTS_DIR, a.resume_after_refusal), "refused_profile_invalid",
               str(e), datetime.now(timezone.utc).isoformat(), "implementation_freeze")
    token = os.environ.get("GUARDED_ADMIN_TOKEN")  # pragma: no cover - 이하 라이브 전용
    if not token:
        raise SystemExit("GUARDED_ADMIN_TOKEN 환경변수 필요")
    deps = rn.real_deps(a.policy_url, token, a.session_file)
    base = deps.check_unresolved

    def check_unresolved():
        base()
        rt.check_no_unresolved_failures(RESULTS_DIR)
    deps.check_unresolved = check_unresolved
    r = run_session(a.run, deps=deps, profile_path=a.profile, artifact_root=a.artifact_root, impl=prov,
                    audit_reader=lambda: read_policy_audit(MODE), mode=a.mode, preflight_only=a.preflight_only,
                    resume_ack=a.resume_after_refusal)
    s = r.get("summary") or {}
    print(json.dumps({"run": a.run, "outcome": s.get("outcome_status"), "cleanup_errors": s.get("cleanup_errors"),
                      "integrity": (r.get("session_record") or {}).get("integrity"),
                      "preflight_only": r.get("preflight_only", False)}, ensure_ascii=False, default=str))
    return 2 if s.get("cleanup_errors") or not ((r.get("session_record") or {}).get("integrity") or {"ok": True})["ok"] else 0


if __name__ == "__main__":
    sys.exit(main())
