#!/usr/bin/env python3
"""B/C/K 탐지 방식 비교 실행기(새 파일, 2026-10-07) - 계약: docs/design/phase8-v33c1-guarded-bck-comparison-contract-draft-20261007.md

동결 A/B 실행기(run_guarded_pilot.py)는 수정하지 않는다. 그 부품(PolicyClient·load_act_approval·check_policy·ActionWatcher·Deps·
real_deps·evidence_counts·상수)과 기존 시행 함수·cleanup gate(D5·D6·canonical)를 import해 그대로 쓰고, 동결 run_guarded 안에
A/B ID·탐지기 ID·context가 고정돼 재사용할 수 없는 orchestration 본문만 옮겨 와 다음만 바꿨다(사용자 결정 D3):
  - 실행 ID bcNN{b,c,k}(공식 01-18, pilot 90), plan·결과 폴더·계약 문서 분리, probe 반복 번호 1003-1058 / 1990-1992
  - 세 arm 모두 act·압박. 탐지기 = c1_bck_live_detector(공통 평가 tick, 실제 arm 규칙만 송신), context = 같은 모듈의
    signal_identity(정책은 정확 일치만 본다)
  - 사전 등록 Latin-square 순서 밖 run 거부(시도 = 요약 있음), 승인 기록 runs는 정확히 [이 run] 하나(D4)
  - 시작 노드는 기록만(A/B의 같은 노드 요건 없음 - 계약 §8). canonical 시작·끝 검사, 미승격 D5 abort→canonical 복원,
    승격 D6(420→30)은 A/B r2와 같은 함수.
guarded 모드 전환(record_only↔act)은 A/B와 같이 실행기 밖 운영 절차로 run마다 한다(D4: run 하나씩 act → 정리 → record_only)."""
import argparse
import hashlib
import json
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import guarded_cleanup_gate as gcg
import guarded_live_detector_runner as gr
import run_guarded_pilot as gp
import run_if_threshold_candidate_smoke as smoke
import run_timed_switch_recovery_trial as rt
from run_guarded_pilot import (BASE_SCALE_DOWN_DELAY_SEC, MODE_ACT, POLICY_VERSION, PRESSURE_OBSERVATION_SEC,
                               READY_TIMEOUT_SEC, SCALE_DOWN_DELAY_SEC, ActionWatcher, Deps, check_policy,
                               evidence_counts, load_act_approval)
from load_ramp_adapter import IMAGE_FORCE_CLOSE, IMAGE_FORCE_CLOSE_ID
from run_once import HarnessCorrupted

import c1_bck_live_detector as bd  # noqa: E402  (gp가 anomaly-detection 경로를 잡는다)
import c1_shadow_detector as sd  # noqa: E402

BCK_PLAN_ID = "capstone-guarded-bck-v1-20261007"
BCK_CONTRACT_DOC = "docs/design/phase8-v33c1-guarded-bck-comparison-contract-draft-20261007.md"
BCK_RESULTS_DIR = Path(__file__).parent / "results" / "capstone_guarded_bck_v1"
BCK_SCRIPT = smoke.ANOMALY_DETECTION_DIR / "c1_bck_live_detector.py"
PILOT_BLOCK, OFFICIAL_BLOCKS = 90, range(1, 19)  # 최종 상한 18 attempted blocks(계약 §11)
LATIN = ("BCK", "CKB", "KBC", "KCB", "CBK", "BKC")  # 사전 등록 순서(계약 §7) - 결과를 보고 바꾸지 않는다
ARM_LABEL = {"B": "B_if_switch", "C": "C_calibrated_fixed_switch", "K": "K_legacy_fixed_switch"}
RUNS = {f"bc{n:02d}{a.lower()}": (n, a, (1990 if n == PILOT_BLOCK else 1000 + 3 * n) + "BCK".index(a))
        for n in (*OFFICIAL_BLOCKS, PILOT_BLOCK) for a in "BCK"}
# v1.2(2026-10-08, bc90 전): 실행 provenance fail-closed. manifest 자체 해시는 코드에 넣지 않는다(manifest가 이 파일 해시를 담음)
REPO_ROOT = Path(__file__).resolve().parent.parent
FREEZE_VERSION = "v1.2"
FREEZE_MANIFEST = "docs/design/phase8-v33c1-guarded-bck-freeze-manifest-v1.2-20261008.json"
PROVENANCE_REQUIRED = {"base_contract": BCK_CONTRACT_DOC,
                       "v1_1_amendment": "docs/design/phase8-v33c1-guarded-bck-contract-amendment-v1.1-20261007.md",
                       "v1_2_amendment": "docs/design/phase8-v33c1-guarded-bck-contract-amendment-v1.2-20261008.md",
                       "runner": "experiments/run_guarded_bck.py", "detector": "anomaly-detection/c1_bck_live_detector.py",
                       "rule_spec": "anomaly-detection/bck_rules/rule-spec-v1.json"}


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def verify_provenance(manifest: Optional[str] = None, root: Optional[Path] = None) -> dict:
    """manifest의 모든 파일을 다시 해시해 비교한다. 하나라도 다르거나 manifest가 없거나 형식이 틀리거나 필수 항목이 빠지면
    SystemExit(클러스터 접근 전). 반환값은 run contract·summary의 code_provenance."""
    manifest, root = manifest or FREEZE_MANIFEST, root or REPO_ROOT
    try:
        raw = (root / manifest).read_bytes()
        m = json.loads(raw)
        files = m["files"]
        if m.get("status") != "frozen" or m.get("version") != FREEZE_VERSION or not isinstance(files, dict):
            raise ValueError(f"status={m.get('status')} version={m.get('version')} files={type(files).__name__}")
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SystemExit(f"provenance fail-closed: freeze manifest 읽기·형식 오류 {manifest}: {type(e).__name__}: {e}") from e
    missing = sorted(k for k, rel in PROVENANCE_REQUIRED.items() if rel not in files)
    actual = {rel: _sha256(root / rel) if (root / rel).is_file() else None for rel in files}
    bad = sorted(rel for rel, want in files.items() if actual[rel] != want)
    if missing or bad or manifest in files:
        raise SystemExit(f"provenance fail-closed: 필수 항목 누락 {missing}, 해시 불일치 {bad}, 자기 참조 {manifest in files}")
    return {"freeze_version": FREEZE_VERSION, "freeze_manifest_path": manifest,
            "freeze_manifest_sha256": hashlib.sha256(raw).hexdigest(),
            **{f"{k}_{x}": (rel if x == "path" else actual[rel]) for k, rel in PROVENANCE_REQUIRED.items() for x in ("path", "sha256")},
            "verified_files": actual, "verification_status": "verified",
            "verified_at_utc": datetime.now(timezone.utc).isoformat()}


def block_order(n: int) -> str:
    return LATIN[0] if n == PILOT_BLOCK else LATIN[(n - 1) % len(LATIN)]


def run_name(n: int, arm: str) -> str:
    return f"bc{n:02d}{arm.lower()}"


def run_id_for(run: str) -> str:
    return f"guarded_pressure_{MODE_ACT}-{run}-{BCK_PLAN_ID}"


def check_schedule(run: str, results_dir: Path) -> dict:
    """사전 등록 순서 강제: 같은 block의 앞 arm, block 첫 arm이면 앞 block(공식 02 이상) 세 run이 모두 시도(요약 있음)됐어야 한다.
    무효·정리 오류 run도 시도로 센다(순서표 한 줄 소모 - 결과로 순서를 고르지 않음). 정리 오류 차단은 check_unresolved가 따로 한다."""
    n, arm, _ = RUNS[run]
    order = block_order(n)
    pos = order.index(arm)
    prev = [run_name(n, a) for a in order[:pos]]
    if pos == 0 and n not in (1, PILOT_BLOCK):
        prev = [run_name(n - 1, a) for a in block_order(n - 1)]
    missing = [r for r in prev if not (results_dir / f"summary-{run_id_for(r)}.json").exists()]
    if missing:
        raise SystemExit(f"{run} 거부: 사전 등록 순서 밖 - 먼저 시도할 run 요약 없음 {missing} (block {n:02d} 순서 {order})")
    return {"block": n, "order": order, "position": pos + 1, "required_before": prev}


def build_context(run_id: str, rollout: dict, arm: str, art: dict, spec_sha256: str) -> dict:
    return {"run_id": run_id, **bd.signal_identity(arm, art["hashes"], spec_sha256), "rollout": rollout,
            "actions_allowed": True}


def bck_starter(results_dir: Path, policy_url: str, expected_version: str, expected_mode: str, arm: str, *,
                start_subprocess=smoke._start_subprocess, stop_subprocess=smoke._stop_subprocess):
    """gr.c1_live_starter와 같은 모양(스크립트·--arm만 다름). sink_url은 쓰지 않는다(무시한 값을 기록)."""
    def start(run_id, evidence_log_path=None, sink_url=None):
        h = start_subprocess(
            [sys.executable, "-u", str(BCK_SCRIPT), "--run-id", run_id, "--arm", arm,
             "--evidence-log", str(gr.live_evidence_path(results_dir, run_id)), "--policy-url", policy_url,
             "--expected-version", expected_version, "--expected-mode", expected_mode],
            cwd=smoke.ANOMALY_DETECTION_DIR, stdout_path=results_dir / f"c1-live-stdout-{run_id}.log",
            stderr_path=results_dir / f"c1-live-stderr-{run_id}.log")
        h["ignored_sink_url"] = sink_url

        def stop():
            r = stop_subprocess(h, "c1_bck_live_detector.py")
            return {**r, "exit_meaning": gr.EXIT_MEANING.get(r.get("returncode"), "terminated_or_unknown")}
        return smoke.DetectorHandle(is_alive=lambda: h["proc"].poll() is None, stop=stop,
                                    label=f"c1_bck_v1 arm={arm} -> {policy_url} ({expected_mode})")
    return start


def real_deps(policy_url: str, admin_token: Optional[str], session_file) -> Deps:  # pragma: no cover - 라이브 전용 조립
    d = gp.real_deps(policy_url, admin_token, session_file)
    base = d.check_unresolved

    def check_unresolved():  # 기존 폴더 전부 + 이 시리즈 폴더
        base()
        rt.check_no_unresolved_failures(BCK_RESULTS_DIR)
    d.check_unresolved = check_unresolved
    d.starter_factory = bck_starter
    return d


def run_bck(run: str, *, deps: Deps, policy_url: str, approval_path=None, preflight_only=False, dry_run=False,
            results_dir: Optional[Path] = None, code_provenance: Optional[dict] = None) -> dict:
    if run not in RUNS:
        raise SystemExit(f"알 수 없는 실행 ID: {run}")
    block, arm, rep = RUNS[run]
    kind, mode = "pressure", MODE_ACT
    run_id = run_id_for(run)
    results_dir = results_dir or BCK_RESULTS_DIR
    results_dir.mkdir(parents=True, exist_ok=True)
    summary_path = results_dir / f"summary-{run_id}.json"
    if summary_path.exists():
        raise SystemExit(f"이미 결과가 있음 - 덮어쓰지 않음: {summary_path}")
    used = gr.live_evidence_path(results_dir, run_id)
    if used.exists():
        raise SystemExit(f"실행 ID 재사용 금지 - 이미 실행된 ID(탐지기 원자료 있음): {used.name}")
    schedule = check_schedule(run, results_dir)
    deps.check_unresolved()
    approval = load_act_approval(approval_path)
    if approval.get("runs") != [run]:  # D4: 승인 범위는 정확히 이 run 하나
        raise SystemExit(f"act 승인 범위 밖 - runs는 정확히 [{run!r}]여야 함: {approval.get('runs')}")
    art = deps.artifacts()
    spec = bd.load_spec()
    ident = bd.signal_identity(arm, art["hashes"], spec["sha256"])
    obs_sec = PRESSURE_OBSERVATION_SEC
    comparison = {"series": BCK_PLAN_ID, "block": f"bc{block:02d}", "block_number": block, "arm": arm,
                  "arm_label": ARM_LABEL[arm], "schedule": schedule, "counted_in_comparison": block != PILOT_BLOCK,
                  "start_node_rule": "recorded_only"}
    rules = {a: {k: spec["spec"]["rules"][a][k] for k in ("detector_id", "model_version", "anomalous_if", "threshold")}
             for a in bd.ARMS}
    contract = {"run_id": run_id, "run": run, "plan_id": BCK_PLAN_ID, "contract_doc": BCK_CONTRACT_DOC,
                "comparison": comparison,
                "conditions": {"stress_stages": rt.DEMO_STAGES, "probe_config": str(smoke.DEFAULT_PROBE_CONFIG),
                               "probe_config_sha256": hashlib.sha256(smoke.DEFAULT_PROBE_CONFIG.read_bytes()).hexdigest(),
                               "probe_image": IMAGE_FORCE_CLOSE, "probe_image_id": IMAGE_FORCE_CLOSE_ID,
                               "detector_rule": {"consecutive": sd.CONSECUTIVE_THRESHOLD, "cooldown_sec": sd.COOLDOWN_SEC},
                               "start_state_rule": "canonical_healthy_r2",
                               "common_evaluation": "c1_bck_live_detector: evaluate_once 1회/tick, 세 규칙 동시 판정·기록",
                               "rules": rules, "rule_spec_sha256": spec["sha256"],
                               "if_artifact_hashes": art["hashes"], "if_threshold": art["threshold"]},
                "kind": kind, "mode": mode, "arm": arm, "actuating_rule": arm, "signal_identity": ident,
                "detector": ident["detector_id"], "model": ident["model_version"], "threshold": ident["threshold"],
                "artifact_hashes": art["hashes"], "observation_sec": obs_sec, "ready_timeout_sec": READY_TIMEOUT_SEC,
                "scale_down_delay": {"act": SCALE_DOWN_DELAY_SEC, "base": BASE_SCALE_DOWN_DELAY_SEC, "changed": True},
                "act_approval": approval,
                "service_check_note": "짧은 전환 직후 트래픽 확인 - 사용자 품질 개선 입증 아님",
                "no_auto_repromotion_or_rollback": True}
    if code_provenance is not None:  # CLI(main)는 항상 검증된 값을 넘긴다. 함수 직접 호출(테스트)은 기존 기록 형식 그대로
        contract["code_provenance"] = code_provenance
    if dry_run:
        (results_dir / f"contract-{run_id}.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"dry_run": True, "run_id": run_id}

    # ---- 이하 동결 run_guarded 본문과 같은 순서·같은 함수(차이: is_ab 분기 고정 True, record_only 분기 없음, 탐지기·context) ----
    pol = deps.policy
    policy_check = check_policy(pol, mode, approval)
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
    if delay_before != BASE_SCALE_DOWN_DELAY_SEC:
        reasons.append(f"scaleDownDelaySeconds={delay_before} != {BASE_SCALE_DOWN_DELAY_SEC}")
    try:
        snap = deps.canonical_snapshot()
        cp = gcg.check_canonical(snap, delay=BASE_SCALE_DOWN_DELAY_SEC)
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
    cleanup_errors, probe_image_check = [], {}
    run_guard = {"mem_drop_abort_gib": 1.0, "disk_recheck_sec": 60.0}
    override_record_path = results_dir / f"scale-down-delay-override-{run_id}.json"
    state = {"override_record_path": override_record_path}
    watcher = ActionWatcher(evidence_path, mode=mode, run_id=run_id, get_state=pol.state,
                            deadline_sec=approval["service_wait_deadline_sec"], thread_runner=deps.thread_runner,
                            wait_kw={"monotonic": deps.monotonic_fn, "sleep": deps.sleep_fn})
    outcome, trial_error, context, injector, canonical_end = None, None, None, None, None

    def assess_now(prep_info=None):  # D5·D6 - 정책 상태·원장·탐지기 기록·실제 Rollout을 함께 본다(예외 없음)
        prep_info = prep_info or gate.prep_info
        old_active = (prep_info or {}).get("pre_prepare_active_selector") or \
            ((state.get("override") or {}).get("before") or {}).get("active_selector")
        return gcg.assess(run_id=run_id, mode=mode, policy_state_fn=pol.state, ledger_fn=deps.ledger_read,
                          evidence_path=evidence_path, view_fn=deps.rollout_view,
                          rollout_uid=((context or {}).get("rollout") or {}).get("uid"), old_active=old_active,
                          prep_info=prep_info, detector_expected=watcher.handle is not None)
    gate = gcg.PreviewCleanupGate(assess_now, deps.cleanup_unpromoted_preview,
                                  restore_fn=lambda prep: deps.canonical_restore(prep, start_pod))
    extensions, safety_args, old_policy_checks = [], {}, {"n": 0, "last": None}

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
    old_handlers = ({getattr(signal, n): signal.signal(getattr(signal, n), _terminate)
                     for n in ("SIGTERM", "SIGBREAK") if hasattr(signal, n)}
                    if threading.current_thread() is threading.main_thread() else {})
    try:
        context = build_context(run_id, deps.rollout_identity(), arm, art, spec["sha256"])
        pol.set_context(context)
        injector = deps.make_injector(run_id, rep, kind, gate.cleanup)
        base_prepare = injector.prepare

        def prepare_isolated():
            check_old_policy()
            base_prepare()
        injector.prepare = prepare_isolated
        # 세 arm 모두 preview 준비 뒤·주입 전에 420초 유예. 시작 노드는 기록만(expected_start_node=None)
        rt.compose_prepare(injector, arm="switch", expected_start_node=None, state=state,
                           apply_override_fn=lambda: deps.apply_override(SCALE_DOWN_DELAY_SEC, override_record_path))
        prober = deps.make_prober(run_id, rep, kind, READY_TIMEOUT_SEC + obs_sec)
        base = deps.base_safety(injector, kind)

        def safety(baseline_state, prober_):
            safety_args.update(baseline_state=baseline_state, prober=prober_)
            base(baseline_state, prober_)
            deps.run_guard_check(run_guard, injector, state, disk_fn=run_guard_disk_fn)
            deps.probe_image_check(probe_image_check)
            check_old_policy()
            watcher.check()

        def extend_for_service_check(phase):
            if not watcher.pending():
                return
            rec = {"phase": phase, "started_at_utc": datetime.now(timezone.utc).isoformat(),
                   "started_mono": deps.monotonic_fn(), "target_key": (watcher.action or {}).get("target_key"),
                   "safety_checks": 0, "safety_errors": [], "probe_alive": [], "counted_in_main_metrics": False}
            last = None
            while watcher.pending():
                t = deps.monotonic_fn()
                if last is None or t - last >= smoke.SAFETY_POLL_INTERVAL_SEC:
                    last = t
                    try:
                        if safety_args:
                            base(safety_args["baseline_state"], safety_args["prober"])
                        deps.run_guard_check(run_guard, injector, state, disk_fn=run_guard_disk_fn)
                        check_old_policy()
                        rec["probe_alive"].append(bool(prober.is_alive()))
                        rec["safety_checks"] += 1
                    except Exception as e:  # noqa: BLE001
                        rec["safety_errors"].append(f"{type(e).__name__}: {e}")
                        break
                deps.sleep_fn(1.0)
            rec.update(ended_at_utc=datetime.now(timezone.utc).isoformat(), ended_mono=deps.monotonic_fn(),
                       guard_pending_after=watcher.pending(), guard_result=watcher.guard_result)
            extensions.append(rec)

        start_live = deps.starter_factory(results_dir, policy_url, POLICY_VERSION, mode, arm)

        def detector_start(rid, path, sink_url):
            real = start_live(rid, path, sink_url)
            watcher.handle = real

            def stop_after_service_check():
                try:
                    watcher.poll_quiet("after_last_safety_check")
                    extend_for_service_check("before_detector_stop")
                finally:
                    r = real.stop()
                watcher.poll_quiet("after_detector_stop")
                watcher.adopt_from_policy_state()
                extend_for_service_check("after_detector_stop")
                return r
            return smoke.DetectorHandle(is_alive=real.is_alive, stop=stop_after_service_check, label=real.label)
        common = dict(check_safety_fn=safety, detector_start_fn=detector_start, sleep_fn=deps.sleep_fn,
                      monotonic_fn=deps.monotonic_fn, detector_ready_timeout_sec=READY_TIMEOUT_SEC,
                      detector_ready_fn=lambda: sd.first_scored_evaluation(evidence_path))
        probe_csv = deps.probe_csv_path(run_id, rep)
        sink_out = results_dir / f"sink-captured-{run_id}.jsonl"
        outcome = deps.trial_pressure(run_id, injector, prober, deps.pick_port(), evidence_path, sink_out, probe_csv,
                                      post_injection_observation_sec=obs_sec, **common)
    except BaseException as e:  # noqa: BLE001 - 하네스 오류·중단 신호도 요약·정리 확인을 남긴다
        trial_error = f"{type(e).__name__}: {e}"
        outcome = {"status": "harness_error", "reason": trial_error, "cleanup_status": "unknown"}
    finally:
        watcher.finish(timeout_sec=watcher.deadline_sec + 30)
        policy_after = None
        try:
            policy_after = pol.state()
        except Exception as e:  # noqa: BLE001
            cleanup_errors.append(f"policy_state_after_failed: {type(e).__name__}: {e}")
        if context is not None:
            try:
                pol.clear_context(run_id)
            except Exception as e:  # noqa: BLE001
                cleanup_errors.append(f"policy_context_clear: {type(e).__name__}: {e}")
        if gate.held:
            cleanup_errors.append(f"preview_cleanup_held: reasons={gate.held['reasons']} "
                                  f"attempt_evidence={gate.held['attempt_evidence']} remaining={gate.held['remaining']}")

        def wait_safety():
            check_old_policy()
            deps.run_guard_check(run_guard, injector, state, disk_fn=run_guard_disk_fn)
        adopted = (watcher.action or {}).get("source") == "policy_state_after_detector_stop"
        runner_guard_ok = (watcher.guard_result or {}).get("result") == "verified" or (
            adopted and (watcher.action or {}).get("outcome") == "verified" and watcher.guard_starts == 0)
        final_assessment = assess_now()
        restore = gcg.finalize_override_guarded(
            state, assessment=final_assessment, runner_guard_ok=runner_guard_ok, view_fn=deps.rollout_view,
            list_rs_fn=deps.list_rs, policy_state_fn=pol.state, restore_fn=deps.restore_override,
            real_patch_fn=deps.patch_delay, safety_fn=wait_safety, monotonic=deps.monotonic_fn,
            sleep=deps.sleep_fn, now_utc=deps.now_utc)
        if restore.get("restored") is False:
            cleanup_errors.append(f"scale_down_delay_restore: {restore.get('error')}")
        if state.get("override_error") and state.get("override_rollback_ok") is False:
            cleanup_errors.append(f"scale_down_delay_apply_rollback_failed: {state['override_error']}")
        try:
            delay_after = deps.read_delay()
        except Exception as e:  # noqa: BLE001
            delay_after = f"read_error: {type(e).__name__}: {e}"
        if delay_after != BASE_SCALE_DOWN_DELAY_SEC:
            cleanup_errors.append(f"scale_down_delay_after={delay_after} != {BASE_SCALE_DOWN_DELAY_SEC}")
        if (policy_after or {}).get("unresolved"):
            cleanup_errors.append(f"policy_unresolved_actions: {sorted(policy_after['unresolved'])}")
        if (policy_after or {}).get("blocked"):
            cleanup_errors.append(f"policy_blocked: {policy_after['blocked']}")
        if watcher.record()["guard_pending_at_end"]:
            cleanup_errors.append("service_wait_still_running_at_end")
        cleanup_errors.extend(f"late: {x}" for x in watcher.late_failures)
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
        canonical_end = gcg.wait_canonical(deps.canonical_snapshot, delay=BASE_SCALE_DOWN_DELAY_SEC,
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
    session_end = deps.session_check()
    counts = evidence_counts(evidence_path)
    summary = {"run_id": run_id, "run": run, "kind": kind, "mode": mode, "arm": arm, "outcome_status": outcome.get("status"),
               "outcome_reason": outcome.get("reason"), "invalidity_reasons": outcome.get("invalidity_reasons"),
               "detector_readiness": outcome.get("detector_readiness") or {}, "evidence_counts": counts,
               "watcher": watcher.record(), "context": context, "signal_identity": ident,
               "rule_spec_sha256": spec["sha256"], "policy_state_after": policy_after,
               "scale_down_delay_override": state.get("override"),
               "scale_down_delay_override_error": state.get("override_error"), "scale_down_delay_restore": restore,
               "scale_down_delay_before": delay_before, "scale_down_delay_after": delay_after,
               "session_start": session_start, "session_end": session_end, "pod_events_error": pod_events_error,
               "probe_image_runtime_check": probe_image_check, "start_active_node": state.get("start_active_node"),
               "harness_error": trial_error, "cleanup_errors": cleanup_errors, "rehearsal": None,
               "service_extensions": extensions, "observation_ended_at_utc": outcome.get("observation_ended_at_utc"),
               "old_policy_isolation": {"start": old_policy, "checks": old_policy_checks,
                                        "end": {k: old_policy_end.get(k) for k in ("isolated", "reasons")}},
               "service_check_note": contract["service_check_note"]}
    summary["trial_outcome"] = outcome
    summary["preview_cleanup_gate"] = gate.log
    summary["final_promotion_assessment"] = final_assessment
    summary["ssh_check_evidence"] = ssh_ev.summary()
    summary["run_guard"] = {k: v for k, v in run_guard.items() if not k.startswith("_")}
    try:
        summary["stress_delivery_record"] = injector.get_stress_delivery_record() if injector is not None else None
    except Exception as e:  # noqa: BLE001
        summary["stress_delivery_record"] = {"error": f"{type(e).__name__}: {e}"}
    summary["comparison"] = comparison
    if code_provenance is not None:
        summary["code_provenance"] = code_provenance
    summary["preflight_file"] = preflight_file.name
    summary["canonical_end"] = canonical_end
    summary["cpu_raw_counter_samples"], summary["cpu_raw_counter_error"] = None, None
    try:
        tgt = (summary["stress_delivery_record"] or {}).get("target") or {}
        t0 = outcome.get("t_injection_observed_utc")
        if not tgt.get("name") or not t0:
            raise RuntimeError(f"target_or_injection_time_missing: {tgt.get('name')}/{t0}")
        summary["cpu_raw_counter_samples"] = deps.cpu_raw_counter(tgt["name"], datetime.fromisoformat(t0))
    except Exception as e:  # noqa: BLE001
        summary["cpu_raw_counter_error"] = f"{type(e).__name__}: {e}"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if not cleanup_errors:
        running_marker.unlink()
    return summary


def main(argv=None) -> int:  # pragma: no cover - 라이브 진입점(가짜 의존성 경로는 run_bck로 시험)
    p = argparse.ArgumentParser(description="B/C/K 비교 run 1회(act 전용, run별 승인). 관리자 토큰은 환경변수 GUARDED_ADMIN_TOKEN")
    p.add_argument("--run", required=True, choices=sorted(RUNS))
    # v1.1(2026-10-07, bc90 시작 전): guarded_pilot_session.ps1이 항상 --mode를 넘긴다. B/C/K는 모두 실제 전환 arm - act만 허용
    p.add_argument("--mode", required=True, choices=[MODE_ACT])
    p.add_argument("--policy-url", default="http://127.0.0.1:18090")
    p.add_argument("--session-file", default=None)
    p.add_argument("--act-approval", required=True)
    p.add_argument("--preflight-only", action="store_true")
    p.add_argument("--dry-run-contract-only", action="store_true")
    a = p.parse_args(argv)
    prov = verify_provenance()  # v1.2: 클러스터 연결 조립(real_deps)·실행 전에 fail-closed
    token = os.environ.get("GUARDED_ADMIN_TOKEN")
    if not token and not a.dry_run_contract_only:
        raise SystemExit("GUARDED_ADMIN_TOKEN 환경변수 필요")
    s = run_bck(a.run, deps=real_deps(a.policy_url, token, a.session_file), policy_url=a.policy_url,
                approval_path=a.act_approval, preflight_only=a.preflight_only, dry_run=a.dry_run_contract_only,
                code_provenance=prov)
    if s.get("cleanup_errors"):
        print(f"[중단] 정리·원복·미해결 조치 - 다음 실행은 자동 거부, 수동 확인 필요: {s['cleanup_errors']}")
        return 2
    print(f"결과: {s.get('outcome_status') or s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
