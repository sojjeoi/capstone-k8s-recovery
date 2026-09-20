#!/usr/bin/env python3
"""memory_pressure 최종 후보(500MB/1000MB/1500MB, 각 120초 stage, native,
worker 1개)의 3회 재현성 검증 전용 스크립트(2026-09-20). 사전 등록:
docs/design/phase8-blue-green-preflight-incident.md §54 - 이 파일의 동작이
그 절의 규칙과 어긋나면 문서가 맞다(측정 뒤 규칙을 사후 조정하지 않는다는
원칙, §42/§50/§52와 동일).

verify_ramp_candidate.py(§25)와 같은 두 계층 구조를 그대로 따른다 - "후보
1회 실행"(run_candidate)과 "N회 반복 + cooldown/quiescence + 기계적 판정"
(main())을 분리한다. explore_memory_pressure_intensity.py의 run_round()와
다른 점: run_round()는 §50/§52 calibration용 **단일** 강도 1회 라운드이고,
이 스크립트는 §50.1/§52.1의 500/1000/1500MB 최종 후보를 **하나의 progressive
시퀀스**(단일 inject() 호출, 어댑터가 이미 지원하는 다단계 stages 리스트)로
실행한 뒤 stage별로 나눠 분석한다 - `run_memory_pressure_trial.py`(smoke
전용, 1GB 이상 차단)의 제한은 건드리지 않는다(완전히 별도 경로).

`memory_pressure_adapter.make_memory_pressure_injector()`를 그대로 재사용
한다(안전 감시·headroom 게이트·duration 안전망·target replacement 규칙 전부
불변) - 이 스크립트가 얹는 것은 (1) baseline/recovery 구간의 독립 감시
(explore_memory_pressure_intensity.run_round()의 own_tick과 같은 이유 -
어댑터 스레드가 그 구간엔 안 돎), (2) 어댑터가 실제로 기록한 stage 경계
(`injector.get_stage_windows()`, §53 이후 추가된 훅)로 probe raw CSV와 안전
tick을 stage별로 나눠 판정하는 것뿐이다.

산출물은 `results/`(top-level, `results/pilot/`이 아님) 아래
`verify-memory_pressure-candidate-rep{N}-{timestamp}-summary.json`으로
저장한다 - `collect_metrics.py`는 `trial-*.json`만 glob하므로(§49.4) 이
파일은 본 실험 분석에 절대 섞이지 않는다. `is_pilot` 개념상 true와 동등
(§50.1과 동일 논리 - TrialResult를 안 쓰므로 필드 자체는 없지만 실질은
동일하게 보장)."""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from active_pod_resolver import get_active_pods
from explore_memory_pressure_intensity import (
    BASELINE_MAX_WAIT_SEC,
    BASELINE_MIN_SEC,
    DEFAULT_PROBE_CONFIG,
    GIB,
    INJECTION_STARTED_TIMEOUT_SEC,
    MAX_TARGET_WORKING_SET_BYTES,
    MIN_NODE_AVAILABLE_PASS_BYTES,
    POLL_INTERVAL_SEC,
    PROBE_STARTUP_TIMEOUT_SEC,
    PROBE_RESULTS_DIR,
    RECOVERY_OBSERVE_SEC,
    ExplorationAbort,
    analyze_slo,
    diff_unhealthy_events,
    node_healthy,
    snapshot_unhealthy_events,
)
from load_ramp_adapter import make_load_ramp_prober
from memory_pressure_adapter import (
    MIN_NODE_AVAILABLE_BYTES,
    get_node_available_bytes,
    get_node_conditions,
    get_node_ip,
    get_pod_details,
    get_pod_working_set_bytes,
    make_memory_pressure_injector,
)
from run_once import HarnessCorrupted, TrialInvalid

RESULTS_DIR = Path(__file__).parent / "results"

# 사전 등록(§54.1) - 최종 후보 구성. CLI로 바꿀 수 없다(다른 강도/시간을
# 시험하려면 §50/§52의 calibration 경로를 다시 거쳐야 한다 - 이 스크립트는
# "이미 정해진 후보의 재현성"만 검증한다).
CANDIDATE_STAGES = [
    {"name": "stage-1-500mb", "size_mb": 500.0, "workers": 1, "duration_sec": 120.0},
    {"name": "stage-2-1000mb", "size_mb": 1000.0, "workers": 1, "duration_sec": 120.0},
    {"name": "stage-3-1500mb", "size_mb": 1500.0, "workers": 1, "duration_sec": 120.0},
]
REPETITIONS = 3
COOLDOWN_SEC = 120.0  # 반복 사이 유휴 대기 - verify_ramp_candidate.py(§25)보다 길게(§54.1, progressive라 stage 3개분 회복 여유)
STAGE_TAIL_MARGIN_SEC = 30.0  # analyze_slo(upper_bound_iso=...) 여유 - LATENCY_PERSIST_SEC(30초)와 동일 크기

STAGE_500_NAME, STAGE_1000_NAME, STAGE_1500_NAME = (s["name"] for s in CANDIDATE_STAGES)
TOTAL_STAGE_SEC = sum(s["duration_sec"] for s in CANDIDATE_STAGES)
# stage 시퀀스 전체 대기 상한 - 명목 시간 + stage마다 CR 삭제·소멸 확인 여유(150초, run_round()와 동일 근거)
STAGE_SEQUENCE_TIMEOUT_SEC = TOTAL_STAGE_SEC + len(CANDIDATE_STAGES) * 150


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _wait_for(check, timeout, interval=1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(interval)
    return False


def bucket_ticks_by_stage(ticks: list, windows: list) -> dict:
    """어댑터의 safety_tick 기록(§53 이후 전부 실제 ts 보유)을 stage
    경계(get_stage_windows()가 반환한 실제 시작/종료)로 나눈다 - 순수 함수,
    명목 시각이 아니라 실제 창을 쓰므로 이전 stage의 잔여 효과가 다음
    stage로 잘못 들어가지 않는다(경계가 딱 [start, end) 반개구간)."""
    buckets = {w["name"]: [] for w in windows}
    for t in ticks:
        if t.get("event") != "safety_tick" or t.get("ts") is None:
            continue
        ts = datetime.fromisoformat(t["ts"])
        for w in windows:
            start = datetime.fromisoformat(w["start"])
            end = datetime.fromisoformat(w["end"]) if w["end"] else None
            if start <= ts and (end is None or ts < end):
                buckets[w["name"]].append(t)
                break
    return buckets


def judge_safety(result: dict) -> dict:
    """§54.4의 9개 재현성 안전 기준을 이 1회 실행에 전부 적용한다 - 순수
    함수(result dict만 읽음, 오프라인 테스트 대상). explore_memory_pressure_
    intensity.judge_pass()와 같은 스타일이지만 "AllInjected"가 stage별로
    나뉘고, 안전 tick이 own_tick(baseline/recovery)과 어댑터 자체 tick
    (injecting)을 합친 result["ticks"] 전체에서 나온다는 점이 다르다."""
    if result.get("aborted"):
        return {"pass": False, "reasons": [f"중단됨: {result.get('abort_reason')}"]}

    reasons = []
    windows = result.get("stage_windows", [])
    if len(windows) != len(CANDIDATE_STAGES):
        reasons.append(f"stage 수 불일치(관측 데이터 손실 의심): 예상 {len(CANDIDATE_STAGES)}, 실제 {len(windows)}")
    not_injected = [w["name"] for w in windows if not w.get("all_injected")]
    if not_injected:
        reasons.append(f"AllInjected 미확인 stage: {not_injected}")

    overall_slo = result.get("overall_slo", {})
    if overall_slo.get("error") or overall_slo.get("success_rate") != 1.0:
        reasons.append(f"completion 성공률 100% 아님: {overall_slo.get('success_rate')}, error={overall_slo.get('error')}")

    ticks = result.get("ticks", [])
    ws_values = [t["working_set_bytes"] for t in ticks if t.get("working_set_bytes") is not None]
    if any(ws >= MAX_TARGET_WORKING_SET_BYTES for ws in ws_values):
        reasons.append("working set이 5GiB 이상인 시점 존재")

    avail_values = [t["node_available_bytes"] for t in ticks if t.get("node_available_bytes") is not None]
    if any(a < MIN_NODE_AVAILABLE_PASS_BYTES for a in avail_values):
        reasons.append("Node MemAvailable이 4GiB 미만인 시점 존재")

    restart_values = {t["restart_count"] for t in ticks if t.get("restart_count") is not None}
    if len(restart_values) > 1:
        reasons.append(f"restartCount 변화 관측: {sorted(restart_values)}")

    if any(t.get("oom_killed") for t in ticks):
        reasons.append("OOMKilled 관측")

    node_issues = [t for t in ticks if t.get("node_conditions") and not node_healthy(t["node_conditions"])]
    if node_issues:
        reasons.append(f"Node 상태 이상 {len(node_issues)}건")

    recovery_checks = [t for t in ticks if t.get("event") == "cleanup_recovery_check"]
    if not recovery_checks or not recovery_checks[-1].get("recovered"):
        reasons.append("cleanup 후 30초 내 baseline 복귀 확인 안 됨")

    if not result.get("cleanup_confirmed", True):
        reasons.append("CR·observer 완전 정리 확인 안 됨")

    return {"pass": len(reasons) == 0, "reasons": reasons}


def judge_reproducibility(repetitions: list) -> dict:
    """§54.4 stage별 SLO 재현성 기준 - 유효(안전 PASS)한 반복에만 기계적으로
    적용한다. violates 판정은 stage의 analyze_slo()가 반환한 t_slo(30초 연속
    latency 또는 즉시 availability 위반 - slo_judge.find_t_slo()의 기존 정의
    그대로) is not None 하나로만 정한다 - p95_peak(순간 초과)는 절대 쓰지
    않는다(지시: "순간 P95 초과만으로 위반 처리하지 않음")."""
    def stage_violates(rep, stage_name):
        s = next((s for s in rep.get("stages", []) if s["name"] == stage_name), None)
        if s is None or "error" in s.get("slo", {}):
            return None
        return s["slo"]["t_slo"] is not None

    safety_pass = [r.get("safety", {}).get("pass") for r in repetitions]
    checks = {"enough_repetitions": len(repetitions) >= REPETITIONS,
              "all_reps_safety_pass": len(repetitions) >= REPETITIONS and all(safety_pass)}

    for name in (STAGE_500_NAME, STAGE_1000_NAME):
        flags = [stage_violates(r, name) for r in repetitions]
        checks[f"{name}_never_violates"] = len(flags) >= REPETITIONS and all(f is False for f in flags)

    flags_1500 = [stage_violates(r, STAGE_1500_NAME) for r in repetitions]
    violate_count = sum(1 for f in flags_1500 if f is True)
    checks[f"{STAGE_1500_NAME}_violates_at_least_2_of_{REPETITIONS}"] = (
        len(flags_1500) >= REPETITIONS and violate_count >= 2)

    return {"overall_pass": all(checks.values()), "checks": checks, "num_repetitions": len(repetitions)}


def check_cluster_quiescent(vllm_pod: str, node_name: str, baseline_restart_count) -> dict:
    """반복 사이 cooldown 전후로 클러스터가 원상복구됐는지 확인한다
    (verify_ramp_candidate.py의 check_node_and_pods와 같은 목적, 이
    어댑터의 실제 조회 함수를 그대로 재사용)."""
    conditions = get_node_conditions(node_name)
    details = get_pod_details(vllm_pod)
    return {
        "node_ok": node_healthy(conditions),
        "restart_count": details["restart_count"] if details else None,
        "restart_unchanged": (details is not None and baseline_restart_count is not None
                               and details["restart_count"] == baseline_restart_count),
        "pod_alive": details is not None,
    }


def run_candidate(rep_index: int, probe_config: str = str(DEFAULT_PROBE_CONFIG)) -> dict:
    """§54.1~54.3 절차대로 500->1000->1500MB(각 120초) progressive 시퀀스를
    1회 실행한다. 단일 inject() 호출 - 어댑터가 stages 리스트를 순차 처리
    한다(§53 이전부터 있던 기능, 지금까지는 단일 stage로만 썼을 뿐)."""
    pods = get_active_pods()
    if len(pods) != 1:
        raise ExplorationAbort(f"active pod이 정확히 1개가 아님({len(pods)}개) - fail-closed")
    target_name, target_uid = pods[0]["name"], pods[0]["uid"]

    details0 = get_pod_details(target_name)
    if details0 is None:
        raise ExplorationAbort("대상 pod 상세 조회 실패(fail-closed)")
    node_name = details0["node_name"]
    baseline_restart_count = details0["restart_count"]
    node_ip = get_node_ip(node_name)
    if node_ip is None:
        raise ExplorationAbort(f"Node({node_name}) InternalIP 조회 실패(fail-closed)")

    run_id = f"verify-memory_pressure-candidate-rep{rep_index}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    events_before = snapshot_unhealthy_events(target_name)
    ticks = []

    def own_tick(phase):
        """baseline·recovery 구간(어댑터 스레드가 안 도는 동안) 전용 감시 -
        target UID 변경까지 포함한 즉시 중단 조건 전체를 본다(explore_memory_
        pressure_intensity.run_round()의 own_tick과 동일한 이유·로직)."""
        pods_now = get_active_pods()
        if len(pods_now) != 1 or pods_now[0]["uid"] != target_uid:
            raise ExplorationAbort(
                f"target UID 변경 감지({phase} 구간): 원래 {target_uid}, 지금 "
                f"{[p['uid'] for p in pods_now]}(즉시 중단)")
        details = get_pod_details(target_name)
        available = get_node_available_bytes(node_ip)
        ws = get_pod_working_set_bytes(target_name)
        conditions = get_node_conditions(node_name)
        rec = {"ts": _now_iso(), "phase": phase, "event": "safety_tick",
               "node_available_bytes": available, "working_set_bytes": ws,
               "restart_count": details["restart_count"] if details else None,
               "oom_killed": details["oom_killed"] if details else None,
               "node_conditions": conditions}
        ticks.append(rec)
        if available is None or available < MIN_NODE_AVAILABLE_BYTES:  # 즉시중단 3GiB(어댑터 상수 그대로)
            raise ExplorationAbort(f"Node MemAvailable 부족/조회 실패({phase}): {available}")
        if ws is None or ws > MAX_TARGET_WORKING_SET_BYTES:
            raise ExplorationAbort(f"target working set 이상/조회 실패({phase}): {ws}")
        if not node_healthy(conditions):
            raise ExplorationAbort(f"Node 상태 이상({phase}): {conditions}")
        if details is None:
            raise ExplorationAbort(f"target pod 조회 실패({phase})")
        if details["oom_killed"]:
            raise ExplorationAbort(f"target OOMKilled({phase})")
        if details["restart_count"] is not None and details["restart_count"] > baseline_restart_count:
            raise ExplorationAbort(
                f"restartCount 증가 감지({phase}): {baseline_restart_count}->{details['restart_count']}")
        return rec

    def adapter_log(record: dict) -> None:
        ticks.append(record)

    probe_total_duration = (BASELINE_MAX_WAIT_SEC + STAGE_SEQUENCE_TIMEOUT_SEC + RECOVERY_OBSERVE_SEC + 60)
    prober = make_load_ramp_prober(probe_config, run_id, "memory_pressure_candidate", "native", 1,
                                    probe_total_duration)
    injector = make_memory_pressure_injector(run_id, "native", 1, stages=CANDIDATE_STAGES, log_fn=adapter_log)

    result = {
        "run_id": run_id, "rep_index": rep_index,
        "target_pod": target_name, "target_uid": target_uid, "node_name": node_name,
        "t_round_start": _now_iso(), "aborted": False, "abort_reason": None, "cleanup_confirmed": False,
    }
    prober_started = False
    baseline_ws = None
    try:
        prober.start()
        prober_started = True
        if not _wait_for(prober.is_alive, PROBE_STARTUP_TIMEOUT_SEC, 1.0):
            raise ExplorationAbort("probe pod이 시작 후 정상 상태에 도달 못 함")

        baseline_start = time.monotonic()
        baseline_status = None
        while True:
            own_tick("baseline")
            baseline_status = prober.get_baseline_status()
            elapsed = time.monotonic() - baseline_start
            if baseline_status["ready"] and elapsed >= BASELINE_MIN_SEC:
                break
            if elapsed >= BASELINE_MAX_WAIT_SEC:
                raise ExplorationAbort(
                    f"probe baseline이 {BASELINE_MAX_WAIT_SEC}초 내 안정화 안 됨(안전 실패 아님 - baseline_timeout)")
            time.sleep(POLL_INTERVAL_SEC)
        result["probe_baseline"] = baseline_status
        baseline_ws = get_pod_working_set_bytes(target_name)
        result["baseline_working_set_bytes"] = baseline_ws
        result["t_baseline_done"] = _now_iso()

        injector.prepare()
        result["t_injection_request"] = _now_iso()
        injector.inject()
        if not _wait_for(injector.is_started, INJECTION_STARTED_TIMEOUT_SEC, 1.0):
            raise ExplorationAbort(f"{INJECTION_STARTED_TIMEOUT_SEC}초 내 stage-1 AllInjected+working set 상승 확인 안 됨")
        t_injection = injector.get_actual_injection_time()
        prober.notify_injected(t_injection)
        result["t_injection"] = t_injection

        stage_deadline = time.monotonic() + STAGE_SEQUENCE_TIMEOUT_SEC
        while True:
            prober.get_baseline_status()  # raw CSV refresh 부수효과만 이용
            if injector.is_done():
                break
            if time.monotonic() >= stage_deadline:
                raise ExplorationAbort("progressive stage 시퀀스가 예상 시간 내에 끝나지 않음")
            time.sleep(POLL_INTERVAL_SEC)
        result["t_injection_end"] = _now_iso()

        windows = injector.get_stage_windows()
        if len(windows) != len(CANDIDATE_STAGES) or any(w["end"] is None for w in windows):
            raise ExplorationAbort(f"stage 시각 데이터 손실(관측 실패) - 실제: {windows}")
        result["stage_windows"] = windows

        injector.cleanup()
        result["cleanup_confirmed"] = True
        result["t_cleanup_done"] = _now_iso()

        recovery_start = time.monotonic()
        while time.monotonic() - recovery_start < RECOVERY_OBSERVE_SEC:
            own_tick("recovery")
            prober.get_baseline_status()
            time.sleep(POLL_INTERVAL_SEC)

    except (ExplorationAbort, TrialInvalid, HarnessCorrupted, RuntimeError) as e:
        result["aborted"] = True
        result["abort_reason"] = str(e)
        print(f"[즉시 중단] {e}", file=sys.stderr)
    finally:
        try:
            injector.cleanup()
            result["cleanup_confirmed"] = True
        except Exception as e:
            result["cleanup_confirmed"] = False
            print(f"[cleanup 경고 - 수동 확인 필요] {e}", file=sys.stderr)
        if prober_started:
            try:
                prober.stop()
            except Exception as e:
                print(f"[prober 정리 경고 - 수동 확인 필요] {e}", file=sys.stderr)

    result["t_round_end"] = _now_iso()
    result["ticks"] = ticks
    events_after = snapshot_unhealthy_events(target_name)
    result["unhealthy_events"] = diff_unhealthy_events(events_before, events_after)

    if result["aborted"]:
        result["safety"] = judge_safety(result)
        return result

    local_raw = PROBE_RESULTS_DIR / f"probe-{run_id}-native-1-raw.csv"
    result["overall_slo"] = analyze_slo(local_raw, result.get("t_injection"))

    windows = result["stage_windows"]
    buckets = bucket_ticks_by_stage(ticks, windows)
    stage_results = []
    for w, spec in zip(windows, CANDIDATE_STAGES):
        stage_ticks = buckets[w["name"]]
        ws_values = [t["working_set_bytes"] for t in stage_ticks if t.get("working_set_bytes") is not None]
        stage_slo = analyze_slo(local_raw, w["start"], upper_bound_iso=w["end"], upper_margin_sec=STAGE_TAIL_MARGIN_SEC)
        recovery_after_stage_end = None
        if stage_slo.get("t_recovery") and w["end"]:
            recovery_after_stage_end = datetime.fromisoformat(stage_slo["t_recovery"]) >= datetime.fromisoformat(w["end"])
        stage_results.append({
            "name": w["name"], "size_mb": spec["size_mb"], "duration_sec": spec["duration_sec"],
            "start": w["start"], "end": w["end"], "all_injected": w["all_injected"],
            "max_working_set_bytes": max(ws_values) if ws_values else None,
            "working_set_rise_bytes": (max(ws_values) - baseline_ws) if (ws_values and baseline_ws is not None) else None,
            "num_safety_ticks": len(stage_ticks),
            "slo": stage_slo,
            "recovery_after_stage_end": recovery_after_stage_end,
        })
    result["stages"] = stage_results

    stage_max_ws = [s["max_working_set_bytes"] for s in stage_results]
    result["working_set_monotonic_nondecreasing"] = (
        all(v is not None for v in stage_max_ws)
        and all(stage_max_ws[i] <= stage_max_ws[i + 1] for i in range(len(stage_max_ws) - 1)))

    result["safety"] = judge_safety(result)
    return result


def wait_for_quiescence(vllm_pod: str, node_name: str, baseline_restart_count, cooldown_sec: float) -> dict:
    status = check_cluster_quiescent(vllm_pod, node_name, baseline_restart_count)
    print(f"quiescence 확인: node_ok={status['node_ok']}, restart_unchanged={status['restart_unchanged']}")
    print(f"cooldown {cooldown_sec:.0f}초 대기...")
    time.sleep(cooldown_sec)
    return status


def print_repetition(result: dict) -> None:
    print(f"\n=== rep {result['rep_index']} 결과(run_id={result['run_id']}) ===")
    if result["aborted"]:
        print(f"중단됨: {result['abort_reason']}")
        return
    for s in result["stages"]:
        slo = s["slo"]
        print(f"  {s['name']}: all_injected={s['all_injected']} max_ws={s['max_working_set_bytes']} "
              f"rise={s['working_set_rise_bytes']} t_slo={slo.get('t_slo')} t_recovery={slo.get('t_recovery')} "
              f"within_window={slo.get('t_slo_within_window')} recovery_after_end={s['recovery_after_stage_end']}")
    print(f"  working_set_monotonic_nondecreasing={result['working_set_monotonic_nondecreasing']}")
    print(f"  overall_slo.success_rate={result['overall_slo'].get('success_rate')}")
    for reason in result["safety"]["reasons"]:
        print(f"  - SAFETY FAIL: {reason}")
    print(f"  safety.pass={result['safety']['pass']}")


def main():
    parser = argparse.ArgumentParser(
        description="memory_pressure 최종 후보(500/1000/1500MB x 120초) 3회 재현성 검증(§54)")
    parser.add_argument("--probe-config", default=str(DEFAULT_PROBE_CONFIG))
    parser.add_argument("--repetitions", type=int, default=REPETITIONS)
    parser.add_argument("--cooldown-sec", type=float, default=COOLDOWN_SEC)
    args = parser.parse_args()

    pods = get_active_pods()
    if len(pods) != 1:
        print(f"시작 전 active pod이 1개가 아님({len(pods)}개) - 중단", file=sys.stderr)
        sys.exit(2)
    vllm_pod, vllm_uid = pods[0]["name"], pods[0]["uid"]
    details0 = get_pod_details(vllm_pod)
    node_name = details0["node_name"]
    baseline_restart_count = details0["restart_count"]

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    repetitions = []
    for i in range(1, args.repetitions + 1):
        print(f"\n{'=' * 20} 반복 {i}/{args.repetitions} {'=' * 20}")
        status_before = check_cluster_quiescent(vllm_pod, node_name, baseline_restart_count)
        if not status_before["node_ok"] or not status_before["restart_unchanged"]:
            print(f"시작 전 클러스터가 정상이 아님(node_ok={status_before['node_ok']}, "
                  f"restart_unchanged={status_before['restart_unchanged']}) - 중단", file=sys.stderr)
            break

        result = run_candidate(i, args.probe_config)
        print_repetition(result)
        out_path = RESULTS_DIR / f"{result['run_id']}-summary.json"
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        print(f"요약 저장: {out_path}")
        repetitions.append(result)

        if not result["safety"]["pass"]:
            print("이 회차가 안전 기준을 충족하지 못함 - 이후 반복을 실행하지 않는다(지시)", file=sys.stderr)
            break

        if i < args.repetitions:
            wait_for_quiescence(vllm_pod, node_name, baseline_restart_count, args.cooldown_sec)

    verdict = judge_reproducibility(repetitions)
    print(f"\n{'=' * 20} 최종 판정 {'=' * 20}")
    for k, v in verdict["checks"].items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    print(f"\n종합: {'PASS' if verdict['overall_pass'] else 'FAIL'}")

    verdict_path = RESULTS_DIR / f"verify-memory_pressure-candidate-verdict-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    verdict_path.write_text(json.dumps({"verdict": verdict, "run_ids": [r["run_id"] for r in repetitions]},
                                        indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"판정 저장: {verdict_path}")
    return repetitions, verdict


if __name__ == "__main__":
    main()
