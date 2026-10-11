#!/usr/bin/env python3
"""2026-10-04 - 독립 검증 V1(경고 시점·무주입 경고) 실행기. 계약: docs/design/phase8-v1-detector-validation-contract-20261004.md

압박(v1s01~v1s15): V2 실행기(run_detector_switch_ab_trial)의 A(sink 전용, 승격 구조적으로 불가) 경로를 그대로 쓴다.
  바꾸는 것은 plan_id·scenario·결과 폴더·실행 ID·계약 문구뿐 - 사전검사·PC commit·preview 배치·압박 전 검사·실행 중
  감시·probe 이미지 확인·압박 전달 판정·정리 확인·오류 기록은 V2와 같은 코드다. 관찰 600초(계약 §3).
무주입(v1n01~v1n06): 기존 run_no_injection_smoke(60분, sink 전용)에 V2의 사전검사·PC commit·실행 중 감시·probe
  이미지 확인·5초 표본기·정리 확인을 훅으로 연결한다. 표본기는 정책 재생용 Rollout 일시정지 상태를 추가로 남긴다.
모델·임계값(−0.04)·3연속·쿨다운은 바꾸지 않는다. 공유 모듈은 수정하지 않는다(V2 실행기에는 V1 미해결 흔적 확인 1줄만)."""
import argparse
import json
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

import arm_controller
import node_resource_safety_gate
import run_detector_switch_ab_trial as ab
import run_timed_switch_recovery_trial as rt
from blue_green_prep import ROLLOUTS_GROUP, ROLLOUTS_PLURAL, ROLLOUTS_VERSION, _custom_api
from cpu_pressure_demo_adapter import make_cpu_pressure_injector
from load_ramp_adapter import IMAGE_FORCE_CLOSE, IMAGE_FORCE_CLOSE_ID, make_load_ramp_prober
from load_ramp_adapter import RESULTS_DIR as PROBE_RESULTS_DIR
from run_cpu_demo_trial import DEMO_STAGES
from run_if_threshold_candidate_smoke import (
    CANDIDATE_THRESHOLD, DEFAULT_PROBE_CONFIG, _capture_recent_pod_events, _check_safety_no_injection,
    _pick_free_loopback_port, _start_candidate_score_server_detector_for_sink, run_no_injection_smoke,
)
from run_if_threshold_no_injection_long_trial import PROBE_LIFETIME_MARGIN_SEC, summarize as summarize_no_injection

PLAN_ID = "capstone-v1-detector-20261004"
CONTRACT_ID = "v1-detector-contract-20261004"
CONTRACT_DOC = "docs/design/phase8-v1-detector-validation-contract-20261004.md"
RESULTS_DIR = Path(__file__).parent / "results" / "capstone_v1_detector"
V2_RESULTS_DIR = ab.RESULTS_DIR  # import 시점 값 고정 - 압박 경로가 ab.RESULTS_DIR를 V1 폴더로 바꾼 뒤에도 V2 흔적을 본다
PRESSURE_SCENARIO = "v1_detector_pressure"
NO_INJECTION_SCENARIO = "v1_detector_no_injection"
PRESSURE_RUNS = {f"v1s{i:02d}": 100 + i for i in range(1, 16)}      # 유효 10회 목표·최대 15회 시도
NO_INJECTION_RUNS = {f"v1n{i:02d}": 200 + i for i in range(1, 7)}   # 60분 유효 3회 목표·최대 6회 시도
NO_INJECTION_OBSERVATION_SEC = 3600.0
NAMESPACE, ROLLOUT_NAME = "vllm-serving", "vllm-serving"
PRESSURE_PURPOSE = ("V1 압박 - 같은 새 CPU 압박 기록에서 후보 IF·공식 IF·고정 규칙·G1이 공식 SLO 위반 전에 경고 조건을 "
                    "충족하는지(같은 평가 시점 사후 재생). 탐지기는 sink 전용 - 전환·조치 효과는 측정하지 않는다.")


def run_id_for(run: str) -> str:
    if run in PRESSURE_RUNS:
        return f"{PRESSURE_SCENARIO}-control-{run}-{PLAN_ID}"
    if run in NO_INJECTION_RUNS:
        return f"{NO_INJECTION_SCENARIO}-{run}-{PLAN_ID}"
    raise ValueError(f"알 수 없는 V1 실행 ID: {run!r} (허용: v1s01~v1s15, v1n01~v1n06)")


def preserve_existing(path: Path) -> None:
    """같은 이름의 이전 기록(사전검사 거부·dry-run 계약 등)이 있으면 덮어쓰지 않도록 .attemptN 이름으로 옮겨 보존한다."""
    if not path.exists():
        return
    n = 1
    while (dst := path.with_name(f"{path.stem}.attempt{n}{path.suffix}")).exists():
        n += 1
    path.rename(dst)


def check_no_unresolved_anywhere() -> None:
    """V1·V2·정시 전환 실험 폴더 어디든 실행 중 표시·정리 실패가 남아 있으면 시작하지 않는다(같은 클러스터)."""
    for d in (RESULTS_DIR, V2_RESULTS_DIR, rt.RESULTS_DIR, RESULTS_DIR.parent / "capstone_input_capture_diagnostic",
              RESULTS_DIR.parent / "capstone_candidate_parallel_diagnostic",
              RESULTS_DIR.parent / "capstone_c1_normal_collection", RESULTS_DIR.parent / "capstone_c1_shadow_pilot",
              RESULTS_DIR.parent / "capstone_v33c1_validation"):
        rt.check_no_unresolved_failures(d)


# ---------------- 압박: V2 실행기의 A 경로 재사용 ----------------

def configure_v2_runner_for_v1_pressure() -> None:
    """V2 실행기 모듈 상수만 V1 값으로 바꾼다(이 프로세스 안에서만). 판정·안전·정리 코드는 그대로."""
    ab.PLAN_ID, ab.SCENARIO, ab.RESULTS_DIR = PLAN_ID, PRESSURE_SCENARIO, RESULTS_DIR
    ab.PAIR_REPS = dict(PRESSURE_RUNS)
    ab.MAIN_PURPOSE = PRESSURE_PURPOSE
    v2_contract = ab._contract

    def contract(run_id: str, arm: str, pair: str) -> dict:
        c = v2_contract(run_id, arm, pair)
        c.update(main_contract=CONTRACT_ID, contract_doc=CONTRACT_DOC, v1_kind="pressure", purpose=PRESSURE_PURPOSE,
                 pair_order=None, scale_down_delay_override_sec=None, scale_down_delay_note=None,
                 analysis=("같은 평가 시점 사후 재생(detector_same_tick_replay --set v1): 후보 IF −0.04·공식 IF·고정 cpu_mean>2.7"
                           "(모두 3연속)·G1(gated_advance_streak 직접 호출). 기준 시각 = 공식 t_slo(slo_judge v3)"),
                 primary_metric=("각 규칙의 선행 시간 = 공식 t_slo − 첫 경고 조건 충족 시각(양수면 선행, "
                                 "detector_same_tick_replay의 lead_vs_t_slo_sec). 보조: 첫 나쁜 요청 시각 기준"),
                 not_claimed="전환·조치 효과(V2의 몫), 이미 본 V2 A·개발 자료로 V1 성능 주장")
        return c
    ab._contract = contract


def run_pressure(run: str, extra_args: list) -> None:
    configure_v2_runner_for_v1_pressure()
    check_no_unresolved_anywhere()
    rid = run_id_for(run)
    if (RESULTS_DIR / f"summary-{rid}.json").exists():
        raise SystemExit(f"이미 결과가 있음 - 덮어쓰지 않음: summary-{rid}.json")
    for name in (f"preflight-{rid}.json", f"contract-{rid}.json"):  # V2 실행기는 같은 이름에 다시 쓴다 - 이전 기록 보존
        preserve_existing(RESULTS_DIR / name)
    sys.argv = [sys.argv[0], "--arm", "control", "--pair", run, *extra_args]
    ab.main()


# ---------------- 무주입: 기존 run_no_injection_smoke + V2 안전장치 ----------------

def read_rollout_pause_state() -> dict:
    """정책 재생용 - recovery-policy의 preview_ready 판정(rollouts_client.is_paused_pre_promotion: pauseConditions에
    reason=BlueGreenPause가 있는가)을 오프라인으로 재현할 원자료. 판정값이 아니라 원래 필드를 남긴다."""
    obj = _custom_api().get_namespaced_custom_object(ROLLOUTS_GROUP, ROLLOUTS_VERSION, NAMESPACE, ROLLOUTS_PLURAL,
                                                     ROLLOUT_NAME)
    st = obj.get("status") or {}
    bg = st.get("blueGreen") or {}
    return {"pause_conditions": st.get("pauseConditions") or [], "phase": st.get("phase"), "abort": st.get("abort"),
            "active_selector": bg.get("activeSelector"), "preview_selector": bg.get("previewSelector"),
            "current_pod_hash": st.get("currentPodHash")}


class V1NoInjectionSampler(rt.TimedSwitch):
    """V2 5초 표본기 그대로 + rollout_pause_state 한 필드. 조회 실패는 None과 오류 문자열(소실 증거 아님)."""

    def __init__(self, *, rollout_state_fn=read_rollout_pause_state, **kw):
        super().__init__(**kw)
        self.rollout_state_fn = rollout_state_fn

    def _sample_once(self, target_name):
        s = super()._sample_once(target_name)
        try:
            s["rollout_pause_state"] = self.rollout_state_fn()
        except Exception as e:  # noqa: BLE001
            s["rollout_pause_state"] = None
            s["rollout_pause_state_error"] = f"{type(e).__name__}: {e}"
        return s


def _no_injection_contract(run_id: str, run: str) -> dict:
    return {"run_id": run_id, "plan_id": PLAN_ID, "main_contract": CONTRACT_ID, "contract_doc": CONTRACT_DOC,
            "v1_kind": "no_injection", "run": run, "recorded_at": datetime.now(timezone.utc).isoformat(),
            "detector": "score_server.py, 후보 artifacts(threshold −0.04, 3연속, 쿨다운 60초) - sink 전용(승격 구조적으로 불가)",
            "candidate_threshold": CANDIDATE_THRESHOLD, "observation_sec_requested": NO_INJECTION_OBSERVATION_SEC,
            "probe_image": IMAGE_FORCE_CLOSE, "probe_image_id": IMAGE_FORCE_CLOSE_ID,
            "safety": "V2 사전검사·PC commit·preview 배치 검증·관찰 전 preview 노드 검사·실행 중 노드 감시·probe 이미지 확인",
            "policy_replay_records": ["sink-captured(신호 전문·수신 시각)", "detector-evidence(평가·signal_result)",
                                      "samples(5초: rollout_pause_state·active endpoint·selector·Pod별 요청 카운터)"],
            "policy_replay_wording": "저장된 상태와 명시한 가정 아래 조치 자격을 통과한 신호 수 - 실제 승격 횟수가 아님"}


def run_no_injection(run: str, *, preflight_only: bool = False, dry_run: bool = False) -> int:
    run_id = run_id_for(run)
    return run_observation(run_id=run_id, rep=NO_INJECTION_RUNS[run], results_dir=RESULTS_DIR,
                           contract=_no_injection_contract(run_id, run), observation_sec=NO_INJECTION_OBSERVATION_SEC,
                           scenario=NO_INJECTION_SCENARIO,
                           summary_extra={"plan_id": PLAN_ID, "main_contract": CONTRACT_ID, "v1_kind": "no_injection", "run": run},
                           preflight_only=preflight_only, dry_run=dry_run)


_CAPTURE_METRICS = ("cpu", "memory", "queue", "cache")


def _decision_record_problems(r: dict, require_capture: bool) -> list:
    """평가 기록이 '수집·계산·기록 정상 완료'인지(점수 크기와 무관). 필요한 칸이 없으면 그 이름들을 돌려준다."""
    bad = [k for k in ("window_start_utc", "window_end_utc", "evaluation_seq") if not r.get(k)]
    s = r.get("score")
    if not isinstance(s, (int, float)) or s != s or s in (float("inf"), float("-inf")):
        bad.append("score_not_finite")
    if require_capture:
        prov = r.get("per_metric_provenance") or {}
        bad += [f"raw_query_capture:{m}" for m in _CAPTURE_METRICS if "raw_query_capture" not in (prov.get(m) or {})]
        if "parallel_candidate" not in r:
            bad.append("parallel_candidate")
    return bad


def first_complete_evaluation(evidence_path: Path, first_sample_utc, *, require_capture: bool) -> dict:
    """c1 시작 절차 v2(2026-10-05)의 준비 판정. 완결된 줄(줄바꿈으로 끝난 줄)만 읽는다 - 마지막 조각은 쓰는 중일 수 있다.
    ready = 정상 완료된 evaluation_decision 중 특징 창 시작이 첫 Pod 상태 표본 시각 이후인 것이 하나라도 있음
    (그 창부터 기대 Pod를 확인할 수 있다). 완결된 줄의 JSON 오류·평가 기록 칸 누락은 error(기록 오류)."""
    detail = {"first_sample_utc": first_sample_utc, "decisions_seen": 0, "skipped_seen": 0, "ready_seq": None}
    if first_sample_utc is None or not Path(evidence_path).exists():
        return {"ready": False, "detail": detail}
    t0 = datetime.fromisoformat(str(first_sample_utc).replace("Z", "+00:00"))
    for i, line in enumerate(Path(evidence_path).read_text(encoding="utf-8").split("\n")[:-1], 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            return {"ready": False, "error": f"evidence {i}번째 줄 JSON 파싱 실패", "detail": detail}
        if r.get("record_type") == "evaluation_skipped":
            detail["skipped_seen"] += 1
            continue
        if r.get("record_type") != "evaluation_decision":
            continue
        detail["decisions_seen"] += 1
        bad = _decision_record_problems(r, require_capture)
        if bad:
            return {"ready": False, "error": f"seq={r.get('evaluation_seq')} 평가 기록 칸 누락: {bad}", "detail": detail}
        if datetime.fromisoformat(r["window_start_utc"].replace("Z", "+00:00")) >= t0:
            detail.update(ready_seq=r["evaluation_seq"], ready_window_start_utc=r["window_start_utc"])
            return {"ready": True, "detail": detail}
    return {"ready": False, "detail": detail}


def run_observation(*, run_id: str, rep: int, results_dir: Path, contract: dict, observation_sec: float, scenario: str,
                    summary_extra: dict, preflight_only: bool = False, dry_run: bool = False,
                    detector_ready_timeout_sec: float = None, detector_ready_fn=None) -> int:
    """detector_ready_timeout_sec(선택, 기본 None = 기존 V1 동작 그대로): 주면 c1 시작 절차 v2 - 탐지기 기동 뒤
    준비 판정이 ready일 때부터 observation_sec을 잰다(대기 상한 = 이 값, probe 수명도 그만큼 늘림). 준비 판정은 기본
    first_complete_evaluation(동결 탐지기 기록 형식), detector_ready_fn(인자 없는 함수)을 주면 그것을 쓴다(c1 섀도 pilot)."""
    """무주입 관찰 1회(압박 없음, sink 전용) - V1 무주입과 c1 정상 자료 수집이 같이 쓴다(2026-10-04 분리, 동작 동일).
    기존 run_no_injection_smoke + V2 사전검사·PC commit·실행 중 감시·probe 이미지 확인·5초 표본기·정리 확인."""
    RESULTS_DIR = results_dir  # noqa: N806 - 아래 본문은 기존 V1 코드 그대로(결과 폴더만 인자로)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = RESULTS_DIR / f"summary-{run_id}.json"
    if summary_path.exists():
        raise SystemExit(f"이미 결과가 있음 - 덮어쓰지 않음: {summary_path}")
    check_no_unresolved_anywhere()
    contract_path = RESULTS_DIR / f"contract-{run_id}.json"
    preserve_existing(contract_path)
    preserve_existing(RESULTS_DIR / f"preflight-{run_id}.json")
    if dry_run:
        contract_path.write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")
        print("dry-run - 클러스터 미접속, 계약만 기록")
        return 0
    preflight = rt.run_preflight()
    preflight["local_commit"] = ab.check_local_commit()
    if not preflight["local_commit"]["ok"]:
        preflight["ok"] = False
        preflight.setdefault("reasons", []).append(preflight["local_commit"]["reason"])
    (RESULTS_DIR / f"preflight-{run_id}.json").write_text(json.dumps(preflight, ensure_ascii=False, indent=2, default=str),
                                                          encoding="utf-8")
    if not preflight["ok"]:
        raise SystemExit(f"사전검사 실패 - 시작 안 함(클러스터 변경 없음): {preflight['reasons']}")
    if preflight_only:
        print(f"사전검사 통과(읽기 전용): {preflight.get('request_counter')}")
        return 0
    contract_path.write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")

    state, probe_image_check, lifecycle_errors, cleanup_errors = {}, {}, [], []
    run_guard = {"mem_drop_abort_gib": ab.MEM_DROP_ABORT_GIB, "disk_recheck_sec": ab.DISK_RECHECK_SEC}
    injector = make_cpu_pressure_injector(run_id, ab.ARM_LABEL, rep, stages=DEMO_STAGES)  # inject()는 부르지 않는다
    injector = arm_controller.wrap_injector_with_preview_prep(injector, ab.ARM_LABEL)
    injector = node_resource_safety_gate.wrap_injector_with_no_injection_diagnostic_exception(
        injector, ab.ARM_LABEL, node_resource_safety_gate.NO_INJECTION_DIAGNOSTIC_EXPECTED_RUN_MODE)
    # V2 감시(check_run_guard)·시작 노드 기록(compose_prepare)이 읽는 이름으로 같은 배치 정보를 연결한다(형식 동일)
    injector.get_real_promotion_diagnostic_exception_info = injector.get_no_injection_diagnostic_exception_info
    rt.compose_prepare(injector, arm="control", expected_start_node=None, state=state, apply_override_fn=lambda: None)
    prober = make_load_ramp_prober(DEFAULT_PROBE_CONFIG, run_id, scenario, ab.ARM_LABEL, rep,
                                   observation_sec + PROBE_LIFETIME_MARGIN_SEC + (detector_ready_timeout_sec or 0.0),
                                   image=IMAGE_FORCE_CLOSE)
    probe_raw_csv_path = PROBE_RESULTS_DIR / f"probe-{run_id}-{ab.ARM_LABEL}-{rep}-raw.csv"
    evidence_path = RESULTS_DIR / f"detector-evidence-{run_id}.jsonl"
    sampler = V1NoInjectionSampler(injector=injector, send_signal=False, run_id=run_id,
                                   evidence_path=RESULTS_DIR / f"samples-{run_id}.jsonl", switch_at_sec=None)
    events_since = datetime.now(timezone.utc).isoformat()
    running_marker = RESULTS_DIR / f"running-{run_id}.json"
    running_marker.write_text(json.dumps({"run_id": run_id, "started_at": events_since}), encoding="utf-8")

    def _terminate(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")
    old_handlers = {getattr(signal, n): signal.signal(getattr(signal, n), _terminate)
                    for n in ("SIGTERM", "SIGBREAK") if hasattr(signal, n)}

    def safety(baseline_state, prober_):  # 기존 무주입 감시 + V2 실행 중 감시·probe 이미지 확인
        _check_safety_no_injection(baseline_state, prober_)
        ab.check_run_guard(run_guard, injector, state)
        ab.verify_probe_image(probe_image_check)

    ready_kw = {}
    if detector_ready_timeout_sec is not None:  # c1 시작 절차 v2 전용 경로(원문 기록 칸까지 확인)
        ready_kw = {"detector_ready_timeout_sec": detector_ready_timeout_sec,
                    "detector_ready_fn": detector_ready_fn or (lambda: first_complete_evaluation(
                        evidence_path, sampler.samples[0]["t"] if sampler.samples else None, require_capture=True))}
    outcome, trial_error = None, None
    try:
        outcome = run_no_injection_smoke(
            run_id, injector, prober, _pick_free_loopback_port(), evidence_path,
            RESULTS_DIR / f"sink-captured-{run_id}.jsonl", probe_raw_csv_path=probe_raw_csv_path,
            observation_sec=observation_sec, check_safety_fn=safety, **ready_kw,
            detector_start_fn=lambda rid, path, sink_url: ab.with_sampler(
                _start_candidate_score_server_detector_for_sink(rid, path, sink_url), sampler, lifecycle_errors))
    except BaseException as e:  # noqa: BLE001 - 하네스 오류·중단 신호도 요약·정리 확인을 남긴다(V2 pilot03 교훈)
        trial_error = f"{type(e).__name__}: {e}"
        outcome = {"status": "harness_error", "reason": trial_error, "elapsed_sec": None, "prober_stop_failed": None,
                   "injector_cleanup_failed": None, "cleanup_status": "unknown"}
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
    cleanup_errors.extend(lifecycle_errors)
    if outcome.get("cleanup_status") != "complete":
        cleanup_errors.append(f"trial_cleanup_incomplete: prober={outcome.get('prober_stop_failed')}, "
                              f"injector={outcome.get('injector_cleanup_failed')}")
    if trial_error:
        cleanup_errors.append(f"harness_error: {trial_error}")
        try:
            left = [p["name"] for p in ab.read_probe_pods() if not p["terminating"]]
            if left:
                cleanup_errors.append(f"probe_pod_residual: {left}")
        except Exception as e:  # noqa: BLE001
            cleanup_errors.append(f"probe_pod_check_failed: {type(e).__name__}: {e}")
    pod_events_error = None
    try:
        events = _capture_recent_pod_events(since_utc=events_since)
        (RESULTS_DIR / f"pod-events-{run_id}.json").write_text(json.dumps(events, ensure_ascii=False, indent=2, default=str),
                                                               encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        pod_events_error = f"{type(e).__name__}: {e}"
    try:
        summary = summarize_no_injection(run_id, observation_sec, outcome, evidence_path)
    except Exception as e:  # noqa: BLE001 - 평가 기록이 없을 때(조기 중단)도 요약은 남긴다
        summary = {"run_id": run_id, "outcome_status": outcome.get("status"), "outcome_reason": outcome.get("reason"),
                   "observation_sec_requested": observation_sec, "summarize_error": f"{type(e).__name__}: {e}"}
    try:  # 기대 active Pod(이름·UID) - prepare()가 고른 대상. c1 특징의 기대 Pod 목록에 쓴다(2026-10-04 추가, 기록 전용)
        active_target = (injector.get_stress_delivery_record() or {}).get("target")
    except Exception as e:  # noqa: BLE001
        active_target = {"error": f"{type(e).__name__}: {e}"}
    summary.update(**summary_extra, preflight=preflight, active_target=active_target,
                   start_active_node=state.get("start_active_node"), run_guard=run_guard,
                   probe_image=IMAGE_FORCE_CLOSE, probe_image_id=IMAGE_FORCE_CLOSE_ID,
                   probe_image_runtime_check=probe_image_check, probe_raw_csv=str(probe_raw_csv_path),
                   preview_prep=ab._preview_prep_info(injector), samples_count=len(sampler.samples),
                   cleanup_errors=cleanup_errors, harness_error=trial_error, pod_events_error=pod_events_error)
    if detector_ready_timeout_sec is not None:  # 준비 대기 기록(대기 시간·준비 판정 평가)과 관찰 시작 시각 보존
        summary.update(detector_readiness=outcome.get("detector_readiness"),
                       observation_started_at_utc=outcome.get("observation_started_at_utc"))
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if cleanup_errors:
        print(f"정리 오류 - 실행 중 표시 유지, 다음 실행 차단: {cleanup_errors}")
        return 2
    running_marker.unlink()
    print(f"결과: {summary.get('outcome_status')} / {summary_path}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="V1 독립 검증 1회 실행(압박 v1sNN 또는 무주입 v1nNN)")
    parser.add_argument("--run", required=True, choices=[*PRESSURE_RUNS, *NO_INJECTION_RUNS])
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--dry-run-contract-only", action="store_true")
    args = parser.parse_args()
    if args.run in PRESSURE_RUNS:
        extra = [*(["--preflight-only"] if args.preflight_only else []),
                 *(["--dry-run-contract-only"] if args.dry_run_contract_only else [])]
        run_pressure(args.run, extra)
        return
    sys.exit(run_no_injection(args.run, preflight_only=args.preflight_only, dry_run=args.dry_run_contract_only))


if __name__ == "__main__":
    main()
