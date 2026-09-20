#!/usr/bin/env python3
"""memory_pressure 강도 calibration 탐색 전용 스크립트(2026-09-20, 2차
사전 등록 §52 추가). 사전 등록: docs/design/phase8-blue-green-preflight-
incident.md §50(1000MB/1500MB, stage 90초) + §52(1500MB/1600MB, stage
120초) - 이 파일의 동작이 그 절들의 규칙과 어긋나면 문서가 맞다(측정 뒤
규칙을 사후 조정하지 않는다는 원칙, §42와 동일).

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
`explore-memory_pressure-native-{size_mb}mb-{stage_duration_sec}s-{timestamp}-
summary.json`으로 저장한다(stage_duration_sec 세그먼트는 같은 크기의 90초/
120초 라운드가 파일명에서 섞이지 않도록 §52에서 추가) - `collect_metrics.py`는
`trial-*.json`만 glob하므로(§49.4에서 코드로 확인) 이 파일은 본 실험 분석에
절대 섞이지 않는다.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
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

# 사전 등록(§50.1, 1600.0은 §52 - 2차 calibration) - 이 값들 밖은 전부 거부한다.
# 1650.0/2000.0은 5GiB 안전 상한과 충돌해 영구 금지(§52 지시 - 자동 실행 금지).
ALLOWED_SIZES_MB = (1000.0, 1500.0, 1600.0)

BASELINE_MIN_SEC = 60.0        # §50.1/§52 - probe baseline 최소 관찰시간(공통, 불변)
BASELINE_MAX_WAIT_SEC = 180.0  # 그 안에 안정 안 되면 이 라운드는 baseline_timeout으로 중단(안전 실패 아님)
STAGE_DURATION_SEC = 90.0      # §50.1 원래 값(1000MB/1500MB 90초 라운드는 완료·불변) - §52 라운드는
                                # run_round()의 stage_duration_sec 인자로 120.0을 명시 전달한다
RECOVERY_OBSERVE_SEC = 60.0    # §50.1/§52 - cleanup 후 최소 회복 관찰시간(어댑터 자신의 30초 판정을 포함)
POLL_INTERVAL_SEC = 5.0
INJECTION_STARTED_TIMEOUT_SEC = 60.0  # run_memory_pressure_trial.py와 동일 근거(§49.1 - Prometheus 반영 지연)
PROBE_STARTUP_TIMEOUT_SEC = 60.0

MIN_NODE_AVAILABLE_PASS_BYTES = 4 * GIB  # §50.4 PASS 기준(즉시 중단 3GiB보다 엄격, 불변)
REQUIRED_RISE_FRACTION = 0.80             # §50.4 - 요청량의 최소 80%(불변)


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


def analyze_slo(local_raw: Path, t_injection_iso, upper_bound_iso=None, upper_margin_sec: float = 30.0) -> dict:
    """§50.6 SLO 분석 - probe raw CSV 완주 후 사후분석(slo_judge.py 재사용,
    새 판정 로직 없음).

    upper_bound_iso(선택, §54 - 후보 재현성 검증의 stage별 분석 지원): 주어지면
    (1) `t_slo`가 이 시각(+upper_margin_sec 여유) 이내인지 `t_slo_within_window`에
    담고, (2) `p95_peak`/`availability_min`/`post_injection_evaluable_samples`도
    [not_before, 이 시각+여유) 구간으로 좁혀서 계산한다(2026-09-20 수정 - §54
    최초 라이브 실행에서 실측 발견: 이 인자 없이는 이 세 값이 raw CSV 전체
    기준이라 같은 라운드의 서로 다른 stage에 항상 같은 값이 찍혀 stage별
    비교가 무의미했다). 하한(not_before)은 `find_t_slo(not_before=t_injection_
    iso)`가 이미 보장하지만(그 이전 시각은 스트릭 시작점으로도 t_slo 후보로도
    못 쓰임), p95_peak 등은 원래 이 하한조차 안 걸려 있었다. 기존 호출부
    (§50~§53, 단일 라운드 전체 분석)는 upper_bound_iso를 안 넘기므로 이
    세 값의 계산 범위(raw CSV 전체)가 그대로 유지된다 - 이미 §50~§53 문서에
    적힌 수치는 이 수정으로 달라지지 않는다(사후 재정의 금지 원칙)."""
    if not local_raw.exists():
        return {"error": "probe raw CSV 없음(probe 시작 실패 등)"}
    rows = slo_judge.load_raw(local_raw)
    if not rows:
        return {"error": "probe raw CSV가 비어있음"}
    points = slo_judge.evaluate(rows)
    not_before = datetime.fromisoformat(t_injection_iso) if t_injection_iso else None
    t_slo = slo_judge.find_t_slo(points, not_before=not_before)
    t_recovery = slo_judge.find_t_recovery(points, t_slo) if t_slo else None
    upper = datetime.fromisoformat(upper_bound_iso) + timedelta(seconds=upper_margin_sec) if upper_bound_iso else None
    windowed = [p for p in points
                if (not_before is None or p["t"] >= not_before) and (upper is None or p["t"] < upper)]
    evaluable_count = sum(1 for p in windowed if p["latency_evaluable"])
    success_count = sum(1 for r in rows if r["success"])
    # upper_bound_iso가 없으면(기존 §50~§53 호출부) p95_peak/availability_min은
    # raw CSV 전체 기준을 그대로 유지 - 있으면(§54 stage별 호출) windowed로 좁힌다.
    p95_source = windowed if upper is not None else points
    p95_values = [p["p95"] for p in p95_source if p["p95"] is not None]
    availability_values = [p["success_rate"] for p in p95_source]
    result = {
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
    if upper_bound_iso is not None:
        result["t_slo_within_window"] = t_slo is not None and t_slo <= upper
    return result


def sufficient_headroom_for_injection(baseline_ws_bytes, size_mb: float, min_headroom_bytes: float) -> bool:
    """§52 1600MB 전용 조건부 사전 조건 - baseline + 요청량을 더했을 때
    5GiB 안전 상한까지 min_headroom_bytes 이상 여유가 남아야 주입한다.
    min_headroom_bytes=0(1000MB/1500MB 라운드 기본값)이면 이 게이트는
    사실상 없음(§50.1에는 이 조건이 없었으므로 기존 라운드 동작 불변).
    순수 함수 - baseline 조회 실패(None)는 항상 거부(fail-closed)."""
    if baseline_ws_bytes is None:
        return False
    projected = baseline_ws_bytes + size_mb * MIB
    return (MAX_TARGET_WORKING_SET_BYTES - projected) >= min_headroom_bytes


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


def run_round(size_mb: float, workers: int = 1, probe_config: str = str(DEFAULT_PROBE_CONFIG),
              stage_duration_sec: float = STAGE_DURATION_SEC, min_headroom_bytes: float = 0.0) -> dict:
    """§50.1~50.3/§52 절차대로 단일 강도 1라운드를 실행한다. StressChaos 1개만
    쓰고(단일 stage), 예외·중단 시 finally에서 즉시 정리한다.

    stage_duration_sec: §50.1 원래 라운드는 90.0(1000MB/1500MB, 완료·불변),
    §52 2차 calibration(1500MB/1600MB)은 120.0을 명시 전달한다.
    min_headroom_bytes: §52 1600MB 전용 조건 - 0(기본값)이면 게이트 없음."""
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

    run_id = (f"explore-memory_pressure-native-{size_mb:.0f}mb-{stage_duration_sec:.0f}s-"
              f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
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

    probe_total_duration = (BASELINE_MAX_WAIT_SEC + stage_duration_sec + 180 + RECOVERY_OBSERVE_SEC + 60)
    prober = make_load_ramp_prober(probe_config, run_id, "memory_pressure_explore", "native", 1,
                                    probe_total_duration)
    stages = [{"name": f"stage-1-{size_mb:.0f}mb", "size_mb": size_mb, "workers": workers,
               "duration_sec": stage_duration_sec}]
    injector = make_memory_pressure_injector(run_id, "native", 1, stages=stages, log_fn=adapter_log)

    result = {
        "run_id": run_id, "size_mb": size_mb, "workers": workers,
        "stage_duration_sec": stage_duration_sec, "min_headroom_bytes": min_headroom_bytes,
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

        if not sufficient_headroom_for_injection(baseline_ws, size_mb, min_headroom_bytes):
            raise TrialInvalid(
                f"baseline working set({baseline_ws}B) + 요청량({size_mb}MB)을 더하면 5GiB 안전 상한까지 "
                f"{min_headroom_bytes:.0f}B 이상 여유가 없음(§52 사전 조건) - 주입하지 않음(fail-closed)")

        injector.prepare()
        result["t_injection_request"] = _now_iso()
        injector.inject()
        if not _wait_for(injector.is_started, INJECTION_STARTED_TIMEOUT_SEC, 1.0):
            raise ExplorationAbort(f"{INJECTION_STARTED_TIMEOUT_SEC}초 내 AllInjected+working set 상승 확인 안 됨")
        result["all_injected_confirmed"] = True
        t_injection = injector.get_actual_injection_time()
        prober.notify_injected(t_injection)
        result["t_injection"] = t_injection

        stage_deadline = time.monotonic() + stage_duration_sec + 150
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
    # §56(direct 후보 재현성 검증) 지원용 - own_tick()은 효과 전 UID 변경만
    # 중단시키고(TrialInvalid 아님, ExplorationAbort), 효과 후 변경은 어댑터가
    # target_replacement에 기록만 하고 계속 진행한다(§48 설계 그대로) - 이
    # 필드가 없으면 "aborted=False였으니 UID도 안 바뀌었다"고만 짐작해야
    # 했다. 기존 호출부(§50~§53)는 이 키를 안 읽으므로 동작 불변.
    result["target_replacement"] = injector.get_target_replacement()
    result["pass"] = judge_pass(result)
    return result


def main():
    parser = argparse.ArgumentParser(
        description="memory_pressure 강도 calibration 탐색(사전 등록 §50) - native 1라운드")
    parser.add_argument("--size-mb", type=float, required=True, choices=ALLOWED_SIZES_MB,
                         help="사전 등록된 값만 허용(1000/1500/1600) - 1650/2000MB는 영구 금지(§50.1/§52)")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--stage-duration-sec", type=float, default=STAGE_DURATION_SEC,
                         help="강도 유지시간(초) - §50.1 기본 90.0, §52 2차 calibration은 120.0을 명시 전달")
    parser.add_argument("--min-headroom-mib", type=float, default=0.0,
                         help="§52 1600MB 전용 사전 조건(MiB) - 0(기본값)이면 게이트 없음(1000/1500MB와 동일)")
    parser.add_argument("--probe-config", default=str(DEFAULT_PROBE_CONFIG))
    args = parser.parse_args()

    print(f"=== memory_pressure 강도 탐색: {args.size_mb}MB, workers={args.workers}, "
          f"stage_duration_sec={args.stage_duration_sec}, min_headroom_mib={args.min_headroom_mib} ===")
    result = run_round(args.size_mb, args.workers, args.probe_config,
                        stage_duration_sec=args.stage_duration_sec,
                        min_headroom_bytes=args.min_headroom_mib * MIB)

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
