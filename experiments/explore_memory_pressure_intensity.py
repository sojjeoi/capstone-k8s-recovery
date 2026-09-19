#!/usr/bin/env python3
"""memory_pressure 강도 calibration 탐색 전용 스크립트(2026-09-20). 사전
등록: docs/design/phase8-blue-green-preflight-incident.md §50 - 이 파일의
동작이 그 절의 규칙과 어긋나면 §50이 맞다(측정 뒤 규칙을 사후 조정하지
않는다는 원칙, §42와 동일).

run_once()/TrialResult를 쓰지 않는다(explore_ramp_intensity.py와 같은 이유
- 정상 trial 판정이 아니라 순수 탐색용). §50.1의 커스텀 타이밍(baseline
60초 이상·strength 유지 90초·cleanup 후 회복 관찰 60초 이상)이 run_once()의
고정 상수(slo_judge.LATENCY_PERSIST_SEC=30초 등 SLO 정의 자체)와 다른
예산을 요구하기 때문 - SLO 판정 상수는 손대지 않고 그 위에 더 긴 관찰
시간만 얹는다.

`memory_pressure_adapter.make_memory_pressure_injector()`는 그대로
재사용한다(안전 감시·headroom 게이트·duration 안전망·target replacement
규칙 전부 불변) - 이 스크립트가 얹는 것은 §50.1 타이밍 프로토콜과 §50.4~
50.6의 라운드 판정뿐이다. `run_memory_pressure_trial.py`(smoke 전용)의
1GB 이상 차단은 건드리지 않는다 - 이 스크립트는 완전히 별도 경로다.

산출물은 `results/`(top-level, `results/pilot/`이 아님) 아래
`explore-memory_pressure-native-{size_mb}mb-{timestamp}-summary.json`으로
저장한다 - `collect_metrics.py`는 `trial-*.json`만 glob하므로(§49.4에서
코드로 확인) 이 파일은 본 실험 분석에 절대 섞이지 않는다.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from kubernetes import client

import slo_judge
from active_pod_resolver import NAMESPACE, get_active_pods, load_kube_config
from load_ramp_adapter import RESULTS_DIR as PROBE_RESULTS_DIR
from load_ramp_adapter import make_load_ramp_prober
from memory_pressure_adapter import (
    GIB,
    MAX_TARGET_WORKING_SET_BYTES,
    MIB,
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
DEFAULT_PROBE_CONFIG = Path(__file__).parent.parent / "chaos" / "probe-config.yaml"

# 사전 등록(§50.1) - 이 값들 밖은 전부 거부한다. 2000MB는 baseline 실측
# (~3.4~3.6GiB, §48.3) + 2000MB가 5GiB 안전 상한을 이미 넘어 영구 금지.
ALLOWED_SIZES_MB = (1000.0, 1500.0)

BASELINE_MIN_SEC = 60.0        # §50.1 - probe baseline 최소 관찰시간
BASELINE_MAX_WAIT_SEC = 180.0  # 그 안에 안정 안 되면 이 라운드는 baseline_timeout으로 중단(안전 실패 아님)
STAGE_DURATION_SEC = 90.0      # §50.1 - 각 강도 유지시간
RECOVERY_OBSERVE_SEC = 60.0    # §50.1 - cleanup 후 최소 회복 관찰시간(어댑터 자신의 30초 판정을 포함)
POLL_INTERVAL_SEC = 5.0
INJECTION_STARTED_TIMEOUT_SEC = 60.0  # run_memory_pressure_trial.py와 동일 근거(§49.1 - Prometheus 반영 지연)
PROBE_STARTUP_TIMEOUT_SEC = 60.0

MIN_NODE_AVAILABLE_PASS_BYTES = 4 * GIB  # §50.4 PASS 기준(즉시 중단 3GiB보다 엄격)
REQUIRED_RISE_FRACTION = 0.80             # §50.4 - 요청량의 최소 80%


class ExplorationAbort(Exception):
    """§50.5 즉시 중단 조건 - 이 라운드를 즉시 끝내고 CR을 정리한다."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _wait_for(check, timeout, interval=1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(interval)
    return False


def node_healthy(conditions: dict) -> bool:
    """memory_pressure_adapter._node_healthy()와 동일한 4-condition 판정 -
    별도 모듈이라 의도적으로 복제한다(이 저장소의 기존 관례, §50.3 참고)."""
    return (conditions.get("Ready") == "True"
            and conditions.get("MemoryPressure") == "False"
            and conditions.get("DiskPressure") == "False"
            and conditions.get("PIDPressure") == "False")


def snapshot_unhealthy_events(pod_name: str) -> dict:
    """대상 pod의 kubelet Unhealthy(Readiness/Liveness probe failed) 이벤트
    스냅샷 - {event_uid: {"kind": ..., "count": ...}}. Startup probe 실패는
    제외(정상 콜드스타트 설계 - coldstart_monitor.py와 같은 원칙)."""
    load_kube_config()
    core = client.CoreV1Api()
    events = core.list_namespaced_event(NAMESPACE, field_selector=f"involvedObject.name={pod_name}").items
    snap = {}
    for e in events:
        if e.reason != "Unhealthy":
            continue
        msg = e.message or ""
        if msg.startswith("Readiness probe failed"):
            kind = "Readiness"
        elif msg.startswith("Liveness probe failed"):
            kind = "Liveness"
        else:
            continue
        snap[e.metadata.uid] = {"kind": kind, "count": e.count or 1}
    return snap


def diff_unhealthy_events(before: dict, after: dict) -> dict:
    """두 스냅샷 사이의 실제 증가분만 kind별로 합산한다(같은 메시지는 K8s가
    count로 합치므로 delta가 이 라운드 동안 새로 발생한 횟수)."""
    result = {"Readiness": 0, "Liveness": 0}
    for key, info in after.items():
        prev = before.get(key, {"count": 0})["count"]
        result[info["kind"]] += max(0, info["count"] - prev)
    return result


def analyze_slo(local_raw: Path, t_injection_iso) -> dict:
    """§50.6 SLO 분석 - probe raw CSV 완주 후 사후분석(slo_judge.py 재사용,
    새 판정 로직 없음)."""
    if not local_raw.exists():
        return {"error": "probe raw CSV 없음(probe 시작 실패 등)"}
    rows = slo_judge.load_raw(local_raw)
    if not rows:
        return {"error": "probe raw CSV가 비어있음"}
    points = slo_judge.evaluate(rows)
    not_before = datetime.fromisoformat(t_injection_iso) if t_injection_iso else None
    t_slo = slo_judge.find_t_slo(points, not_before=not_before)
    t_recovery = slo_judge.find_t_recovery(points, t_slo) if t_slo else None
    post_injection = [p for p in points if not_before is None or p["t"] >= not_before]
    evaluable_count = sum(1 for p in post_injection if p["latency_evaluable"])
    success_count = sum(1 for r in rows if r["success"])
    p95_values = [p["p95"] for p in points if p["p95"] is not None]
    availability_values = [p["success_rate"] for p in points]
    return {
        "local_raw_csv": str(local_raw),
        "total_samples": len(rows),
        "success_rate": success_count / len(rows),
        "t_slo": t_slo.isoformat() if t_slo else None,
        "t_recovery": t_recovery.isoformat() if t_recovery else None,
        "violation_duration_sec": (t_recovery - t_slo).total_seconds() if (t_slo and t_recovery) else None,
        "post_injection_evaluable_samples": evaluable_count,
        "p95_peak": max(p95_values) if p95_values else None,
        "availability_min": min(availability_values) if availability_values else None,
    }


def judge_pass(result: dict) -> dict:
    """§50.4의 9개 PASS 조건을 전부 확인한다 - 하나라도 어긋나면 그 사유를
    reasons에 남기고 pass=False. 순수 함수(result dict만 읽음, 오프라인
    테스트 대상)."""
    reasons = []
    if result.get("aborted"):
        return {"pass": False, "reasons": [f"중단됨: {result.get('abort_reason')}"]}

    if not result.get("all_injected_confirmed"):
        reasons.append("AllInjected 확인 안 됨")

    size_mb = result["size_mb"]
    rise = result.get("working_set_rise_bytes")
    required_rise = size_mb * MIB * REQUIRED_RISE_FRACTION
    if rise is None or rise < required_rise:
        reasons.append(f"working set 상승 부족: {rise} < 요청량의 80%({required_rise:.0f}B)")

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


def run_round(size_mb: float, workers: int = 1, probe_config: str = str(DEFAULT_PROBE_CONFIG)) -> dict:
    """§50.1~50.3 절차대로 단일 강도 1라운드를 실행한다. StressChaos 1개만
    쓰고(단일 stage), 예외·중단 시 finally에서 즉시 정리한다."""
    if size_mb not in ALLOWED_SIZES_MB:
        raise ValueError(
            f"size_mb={size_mb}는 사전 등록된 값이 아님(허용: {ALLOWED_SIZES_MB}) - "
            f"fail-closed(§50.1, 2000MB는 5GiB 안전 상한과 충돌해 영구 금지)")

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

    run_id = f"explore-memory_pressure-native-{size_mb:.0f}mb-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    events_before = snapshot_unhealthy_events(target_name)
    ticks = []

    def own_tick(phase):
        """baseline·recovery 구간(어댑터 내부 스레드가 안 도는 동안) 전용
        안전 확인 - target UID 변경까지 포함한 §50.5 즉시 중단 조건 전체를
        본다. 어댑터 자체 스레드가 도는 injecting 구간은 어댑터의 안전
        tick(log_fn으로 ticks에 이미 합류)이 이미 담당한다."""
        pods_now = get_active_pods()
        if len(pods_now) != 1 or pods_now[0]["uid"] != target_uid:
            raise ExplorationAbort(
                f"target UID 변경 감지({phase} 구간): 원래 {target_uid}, 지금 "
                f"{[p['uid'] for p in pods_now]}(즉시 중단)")
        details = get_pod_details(target_name)
        available = get_node_available_bytes(node_ip)
        ws = get_pod_working_set_bytes(target_name)
        conditions = get_node_conditions(node_name)
        rec = {"ts": _now_iso(), "phase": phase, "node_available_bytes": available,
               "working_set_bytes": ws,
               "restart_count": details["restart_count"] if details else None,
               "oom_killed": details["oom_killed"] if details else None,
               "node_conditions": conditions}
        ticks.append(rec)
        if available is None or available < MIN_NODE_AVAILABLE_BYTES:
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

    probe_total_duration = (BASELINE_MAX_WAIT_SEC + STAGE_DURATION_SEC + 180 + RECOVERY_OBSERVE_SEC + 60)
    prober = make_load_ramp_prober(probe_config, run_id, "memory_pressure_explore", "native", 1,
                                    probe_total_duration)
    stages = [{"name": f"stage-1-{size_mb:.0f}mb", "size_mb": size_mb, "workers": workers,
               "duration_sec": STAGE_DURATION_SEC}]
    injector = make_memory_pressure_injector(run_id, "native", 1, stages=stages, log_fn=adapter_log)

    result = {
        "run_id": run_id, "size_mb": size_mb, "workers": workers,
        "target_pod": target_name, "target_uid": target_uid, "node_name": node_name,
        "t_round_start": _now_iso(), "aborted": False, "abort_reason": None,
        "all_injected_confirmed": False, "cleanup_confirmed": False,
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
            raise ExplorationAbort(f"{INJECTION_STARTED_TIMEOUT_SEC}초 내 AllInjected+working set 상승 확인 안 됨")
        result["all_injected_confirmed"] = True
        t_injection = injector.get_actual_injection_time()
        prober.notify_injected(t_injection)
        result["t_injection"] = t_injection

        stage_deadline = time.monotonic() + STAGE_DURATION_SEC + 150
        while True:
            prober.get_baseline_status()  # raw CSV refresh 부수효과만 이용(baseline 판정 자체는 이제 안 씀)
            if injector.is_done():
                break
            if time.monotonic() >= stage_deadline:
                raise ExplorationAbort("stage가 예상 시간 내에 끝나지 않음(CR 삭제·소멸 확인 실패 의심)")
            time.sleep(POLL_INTERVAL_SEC)
        result["t_injection_end"] = _now_iso()

        injector.cleanup()
        result["cleanup_confirmed"] = True
        result["t_cleanup_done"] = _now_iso()

        recovery_start = time.monotonic()
        while time.monotonic() - recovery_start < RECOVERY_OBSERVE_SEC:
            own_tick("recovery")
            prober.get_baseline_status()
            time.sleep(POLL_INTERVAL_SEC)

    except (ExplorationAbort, TrialInvalid, HarnessCorrupted) as e:
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

    ws_values = [t["working_set_bytes"] for t in ticks if t.get("working_set_bytes") is not None]
    result["max_working_set_bytes"] = max(ws_values) if ws_values else None
    result["working_set_rise_bytes"] = (
        (result["max_working_set_bytes"] - baseline_ws)
        if (result["max_working_set_bytes"] is not None and baseline_ws is not None) else None)

    local_raw = PROBE_RESULTS_DIR / f"probe-{run_id}-native-1-raw.csv"
    result["slo"] = analyze_slo(local_raw, result.get("t_injection"))
    result["pass"] = judge_pass(result)
    return result


def main():
    parser = argparse.ArgumentParser(
        description="memory_pressure 강도 calibration 탐색(사전 등록 §50) - native 1라운드")
    parser.add_argument("--size-mb", type=float, required=True, choices=ALLOWED_SIZES_MB,
                         help="사전 등록된 값만 허용(1000/1500) - 2000MB는 영구 금지(§50.1)")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--probe-config", default=str(DEFAULT_PROBE_CONFIG))
    args = parser.parse_args()

    print(f"=== memory_pressure 강도 탐색: {args.size_mb}MB, workers={args.workers} ===")
    result = run_round(args.size_mb, args.workers, args.probe_config)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"{result['run_id']}-summary.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    print(f"\n=== 결과: aborted={result['aborted']} pass={result['pass']['pass']} ===")
    if result["aborted"]:
        print(f"중단 사유: {result['abort_reason']}")
    for reason in result["pass"]["reasons"]:
        print(f"  - FAIL: {reason}")
    print(f"baseline_working_set_bytes={result.get('baseline_working_set_bytes')}")
    print(f"max_working_set_bytes={result.get('max_working_set_bytes')} "
          f"rise_bytes={result.get('working_set_rise_bytes')}")
    print(f"unhealthy_events={result.get('unhealthy_events')}")
    print(f"slo={result.get('slo')}")
    print(f"요약 저장: {out_path}")
    sys.exit(0 if (not result["aborted"] and result["pass"]["pass"]) else 1)


if __name__ == "__main__":
    main()
