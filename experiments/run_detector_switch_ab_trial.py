#!/usr/bin/env python3
"""2026-10-02 - 독립 검증 V2: 압박이 계속되는 동안 IF 자동 전환의 효과(A/B 짝).

A(control): 후보 IF(score_server.py, 후보 artifacts −0.04)를 capture sink에만 연결 - 승격 구조적으로 불가.
B(switch):  같은 후보 IF를 실제 recovery-policy에 연결 - IF가 판단하면 실제 승격.
두 군 모두 DEMO_STAGES 4×90초를 주입 시점 active Pod(이름·UID 고정)에 끝까지 건다
(continue_after_target_replacement). B는 구 Pod가 압박 종료까지 살도록 scaleDownDelaySeconds를
**제안값 420초**로 이 실행 동안만 바꾸고 원복한다 - 실제 Argo 동작은 아직 확인되지 않았다.

계획: docs/design/phase8-detector-independent-validation-plan-20261002.md §3.
기존 공유 코드(run_if_threshold_candidate_smoke의 run_cpu_pressure_trial·run_no_switch_control_trial,
arm_controller, node_resource_safety_gate, slo_judge)는 수정하지 않고 조립만 한다. 정시 전환 러너의
사전검사·지연 변경/원복·판정·정리 확인을 재사용한다."""
import argparse
import json
import signal
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import arm_controller
import node_disk_health_gate
import node_resource_safety_gate
import run_timed_switch_recovery_trial as rt
import slo_judge
import scale_down_delay_override as sdo
import timed_switch_verdict as verdict
from cpu_pressure_demo_adapter import MIN_EFFECTIVE_CPU_RISE_CORES, _cpu_promql, make_cpu_pressure_injector
from load_ramp_adapter import RESULTS_DIR as PROBE_RESULTS_DIR
from load_ramp_adapter import IMAGE_FORCE_CLOSE, IMAGE_FORCE_CLOSE_ID, make_load_ramp_prober
from run_cpu_demo_trial import DEMO_STAGES
from run_if_threshold_candidate_smoke import (
    BASELINE_TIMEOUT_SEC, CANDIDATE_THRESHOLD, DEFAULT_PROBE_CONFIG, PROBE_INJECTION_START_MARGIN_SEC,
    DetectorHandle, _capture_recent_pod_events, _pick_free_loopback_port,
    _start_candidate_score_server_detector, _start_candidate_score_server_detector_for_sink,
    run_cpu_pressure_trial, run_no_switch_control_trial,
)
from run_once import (HarnessCorrupted, TrialInvalid, _clear_experiment_context, _get_experiment_state,
                      _register_experiment_context)

PLAN_ID = "capstone-detector-switch-ab-206-v1"
SCENARIO = "detector_switch_ab"
RESULTS_DIR = Path(__file__).parent / "results" / "capstone_detector_switch_ab"
ARMS = ("control", "switch")
ARM_LABEL = rt.ARM_LABEL  # preview 준비 경로 이름("proposed") - 실험군 이름과 분리
OBSERVATION_SEC = rt.OBSERVATION_SEC  # 600
SCALE_DOWN_DELAY_SEC = 420  # 본 실행 확정(2026-10-02 pilot04 B: 적용 확인, 구 Pod Killing=전환+420초로 압박 종료 134초 뒤, 30 원복 라이브 확인)
# 시험 쌍 pilot01과 본 실행 pair01..pair10 - run_id가 서로·정시 전환 실험과 겹치지 않는다(plan_id·scenario가 다름)
# pilot02(2026-10-02): pilot01 A 시도 1이 주입 전 실행기 결함(NameError)으로 무효 - 새 시험 ID, rep은 겹치지 않는 20
# pilot03(2026-10-02): pilot02 A는 기준 v1(rate[30s] 5초 격자)로 비교 자격 없음 보존 - 기준 v2 첫 시험 ID
# pilot04(2026-10-02): pilot03 A는 이 PC 메모리 예약 부족으로 실행기 중단 - 무효 보존
# 본 실행 계약(2026-10-02): 유효 10쌍·최대 18쌍 시도 → pair01~pair18
PAIR_REPS = {"pilot01": 0, "pilot02": 20, "pilot03": 21, "pilot04": 22, **{f"pair{i:02d}": i for i in range(1, 19)}}
# 2026-10-03 지표 개정(pair02 A 후·B 전): 주 지표 = 누적 위반시간 - docs/design/phase8-v2-main-experiment-contract-v2-20261003.md
MAIN_CONTRACT_ID = "v2-main-contract-20261003"
PILOT_PURPOSE = ("시험 쌍 - 장치(IF 실제 전환 경로, 압박 지속, 구 Pod 유지 420초 제안값의 적용·원복, Pod별 카운터)와 "
                 "판정표 확인용. 효과 주장에 쓰지 않고 본 실행 반복 수에 포함하지 않는다.")
MAIN_PURPOSE = ("본 실행 - 같은 CPU 압박이 계속되는 동안 IF 자동 전환(B)과 무전환(A)의 사용자 요청 품질 비교. "
                "B는 배정 기준 분석(IF가 전환하지 않은 B도 포함). 고정 규칙의 실제 조치와의 우열은 이 비교로 주장하지 않는다.")
AUDIT_POLL_SEC, AUDIT_POLL_TIMEOUT_SEC = 5.0, 60.0
PROBE_IMAGE = IMAGE_FORCE_CLOSE  # V2 새 실행 전용(과거 실행 기본 이미지와 분리)
CANDIDATE_MODEL_SHA256 = "2102e4f5f0d06809402f6f11c0396f0249bda3864bf6eeb18c52ac626e1a9243"  # 후보 artifacts model.pkl


def pair_label(pair) -> str:
    label = f"pair{int(pair):02d}" if str(pair).isdigit() else str(pair)
    if label not in PAIR_REPS:
        raise ValueError(f"알 수 없는 쌍: {pair!r} (허용: pilot01, 1~10 또는 pair01~pair10)")
    return label


def _pair_arg(value: str) -> str:
    try:
        return pair_label(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def run_id_for(arm: str, pair) -> str:
    return f"{SCENARIO}-{arm}-{pair_label(pair)}-{PLAN_ID}"


MIN_POST_INJECTION_REQUESTS = 570  # 계획 §3.6: 600초×1RPS의 95%


def pilot_a_slo_gate(a_summary: dict) -> dict:
    """계획 §3.6 - pilot B 전에 A의 probe 원자료를 기존 slo_judge(v3, 수정 없음)로 판정한다.
    A에 공식 t_slo가 있어야(압박이 이 환경에서도 사용자 피해를 만든다는 대조 전제) B를 시작한다.
    계산 불가(CSV 없음·주입 시각 없음·주입 후 요청 부족·파싱 실패) 또는 위반 없음이면 ok=False."""
    out = {"ok": False, "status": None, "probe_raw_csv": a_summary.get("probe_raw_csv"),
           "t_injection_observed_utc": a_summary.get("t_injection_observed_utc"),
           "post_injection_requests": None, "t_slo": None, "t_slo_rel_sec": None,
           "judged_at": datetime.now(timezone.utc).isoformat(), "slo_version": slo_judge.SLO_VERSION}
    csv, t_inj = a_summary.get("probe_raw_csv"), a_summary.get("t_injection_observed_utc")
    if not csv or not Path(csv).exists() or not t_inj:
        out["status"] = "not_computable(probe_csv_or_injection_time_missing)"
        return out
    try:
        rows = slo_judge.load_raw(Path(csv))
        t0 = datetime.fromisoformat(t_inj)
        nb = t0 if rows and rows[0]["sent_at"].tzinfo is not None else t0.replace(tzinfo=None)
        out["post_injection_requests"] = sum(1 for r in rows if r["sent_at"] >= nb)
        if out["post_injection_requests"] < MIN_POST_INJECTION_REQUESTS:
            out["status"] = f"not_computable(post_injection_requests<{MIN_POST_INJECTION_REQUESTS})"
            return out
        t_slo = slo_judge.find_t_slo(slo_judge.evaluate(rows), not_before=nb)
    except Exception as e:  # noqa: BLE001 - 파싱 실패도 계산 불가
        out["status"] = f"not_computable({type(e).__name__}: {e})"
        return out
    if t_slo is None:
        out["status"] = "no_violation"
        return out
    out.update(ok=True, status="violation", t_slo=t_slo.isoformat(), t_slo_rel_sec=round((t_slo - nb).total_seconds(), 1))
    return out


def check_pair_precondition(arm: str, pair, results_dir: Path = RESULTS_DIR) -> Optional[str]:
    """B는 같은 쌍 A가 비교 자격을 얻었을 때만. 시험 쌍(pilot*)은 추가로 A의 공식 t_slo 발생을 확인하고
    판정을 pilot-slo-gate-<B run_id>.json에 남긴다(A 요약·원자료는 수정하지 않음). 본 실행(pair*)은
    배정 기준 원칙대로 이 확인을 하지 않는다. 반환: A의 시작 active 노드."""
    if arm == "control":
        return None
    label = pair_label(pair)
    path = results_dir / f"summary-{run_id_for('control', label)}.json"
    if not path.exists():
        raise SystemExit(f"{label} B 거부: 같은 쌍 A 요약이 없음({path.name}) - A를 먼저 실행")
    a = json.loads(path.read_text(encoding="utf-8"))
    if not (a.get("comparison") or {}).get("eligible") or not a.get("start_active_node"):
        raise SystemExit(f"{label} B 거부: A가 비교 자격 없음 또는 시작 노드 기록 없음 "
                         f"{(a.get('comparison') or {}).get('reasons')}")
    if label.startswith("pilot"):
        gate = pilot_a_slo_gate(a)
        (results_dir / f"pilot-slo-gate-{run_id_for('switch', label)}.json").write_text(
            json.dumps(gate, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        if not gate["ok"]:
            raise SystemExit(f"{label} B 거부: A에서 공식 t_slo 확인 안 됨({gate['status']}) - 계획 §3.6, "
                             f"압박 설계 재검토 필요(강도를 결과 보고 조정하지 않음)")
    return a["start_active_node"]


def stop_and_confirm(name: str, handle: DetectorHandle, errors: list) -> dict:
    """정지하고 실제로 멈췄는지 확인. 실패는 errors(= 다음 실행을 막는 정리 오류)에 남긴다. 예외 없음."""
    try:
        result = handle.stop() or {}
    except Exception as e:  # noqa: BLE001
        result = {"stop_error": f"{type(e).__name__}: {e}"}
        errors.append(f"{name}_stop_error: {result['stop_error']}")
    if isinstance(result, dict):
        if result.get("exited_cleanly") is False:  # 탐지기 프로세스(_stop_subprocess) 종료 미확인
            errors.append(f"{name}_not_exited_cleanly: {result}")
        if result.get("threads_alive"):  # 표본 수집기 스레드가 종료 대기 후에도 살아 있음
            errors.append(f"{name}_threads_still_alive: {result['threads_alive']}")
    try:
        if handle.is_alive():
            errors.append(f"{name}_still_alive_after_stop")
    except Exception as e:  # noqa: BLE001
        errors.append(f"{name}_alive_check_failed: {type(e).__name__}: {e}")
    return result


def with_sampler(detector: DetectorHandle, sampler, errors: list) -> DetectorHandle:
    """탐지기와 5초 표본 수집기를 함께 켜고 끈다. 표본기 시작이 실패하면 이미 켠 탐지기(B는 실제 정책 연결)를
    즉시 끄고 종료를 확인한 뒤 HarnessCorrupted로 실행을 중단한다 - 핸들이 부모 러너에 전달되지 않아
    탐지기가 남는 것을 막기 위함. 종료 확인 실패는 errors에 남아 다음 실행을 막는다."""
    try:
        sampler_handle = sampler.start()
    except Exception as e:  # noqa: BLE001
        stop_and_confirm("detector", detector, errors)
        raise HarnessCorrupted(f"표본 수집기 시작 실패 - 탐지기 종료 후 중단: {type(e).__name__}: {e}") from e

    def stop():
        return {"detector": stop_and_confirm("detector", detector, errors),
                "sampler": stop_and_confirm("sampler", sampler_handle, errors)}
    return DetectorHandle(is_alive=lambda: detector.is_alive() and sampler_handle.is_alive(), stop=stop,
                          label=f"{detector.label} + sampler")


def fetch_raw_audit(run_id: str) -> dict:
    """recovery-policy 원본 감사 기록(읽기 전용 GET). 공용 _fetch_audit_verified_at과 달리 evidence를 보존한다."""
    import requests
    try:
        r = requests.get(f"{rt.LOCAL_RECOVERY_POLICY_URL}/admin/audit/{run_id}", timeout=10)
        r.raise_for_status()
        return {"fetch_error": None, "records": r.json().get("records", [])}
    except Exception as e:  # noqa: BLE001
        return {"fetch_error": f"{type(e).__name__}: {e}", "records": []}


def load_signal_results(evidence_path: Path) -> list:
    """이 실행 탐지기 평가 기록의 signal_result(전송 시도 결과). 파일이 없거나 깨진 줄은 건너뛴다."""
    out = []
    if not Path(evidence_path).exists():
        return out
    for line in Path(evidence_path).read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("record_type") == "signal_result":
            out.append(r)
    return out


def fetch_audit_until_settled(run_id: str, *, expect_promotion: bool, fetch_fn: Callable = None,
                              sleep_fn=time.sleep, monotonic_fn=time.monotonic) -> dict:
    """감사 기록(git outbox)은 비동기라 늦을 수 있다 - 승격이 있었으면 그 기록이 보일 때까지 짧게 재조회."""
    fetch_fn = fetch_fn or fetch_raw_audit
    deadline = monotonic_fn() + AUDIT_POLL_TIMEOUT_SEC
    while True:
        audit = fetch_fn(run_id)
        has_promote = any(r.get("action") == "promote_preview" for r in audit.get("records") or [])
        if audit.get("fetch_error") is None and (has_promote or not expect_promotion):
            return audit
        if monotonic_fn() >= deadline:
            return audit
        sleep_fn(AUDIT_POLL_SEC)


def read_probe_pods() -> list:
    """vllm-serving의 ramp-probe-* Pod와 kubelet이 보고한 실제 이미지 ID(읽기 전용)."""
    r = subprocess.run(["kubectl", "get", "pods", "-n", "vllm-serving", "-o", "json"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"kubectl get pods 실패: {r.stderr.strip()[:300]}")
    out = []
    for p in json.loads(r.stdout).get("items", []):
        if not p["metadata"]["name"].startswith("ramp-probe-"):
            continue
        cs = (p.get("status", {}).get("containerStatuses") or [{}])[0]
        out.append({"name": p["metadata"]["name"], "node": p.get("spec", {}).get("nodeName"),
                    "terminating": bool(p["metadata"].get("deletionTimestamp")),
                    "image": cs.get("image"), "image_id": cs.get("imageID")})
    return out


def verify_probe_image(record: dict, expected_id: str = IMAGE_FORCE_CLOSE_ID, read_fn: Callable = None) -> None:
    """실행 중 probe Pod가 실제로 쓴 이미지 ID를 확인해 record에 남긴다(첫 성공 뒤에는 생략).
    Pod가 정확히 1개가 아니거나, 이미지 ID를 읽지 못하거나, 예상과 다르면 TrialInvalid(정리 경로로 중단)."""
    if record.get("verified"):
        return
    record.update(expected_image_id=expected_id, checked_at=datetime.now(timezone.utc).isoformat())
    try:
        live = [p for p in (read_fn or read_probe_pods)() if not p["terminating"]]
    except Exception as e:  # noqa: BLE001 - 확인 불가도 실패(fail-closed)
        record.update(verified=False, error=f"{type(e).__name__}: {e}")
        raise TrialInvalid(f"probe 이미지 확인 불가: {record['error']}") from e
    record["pods"] = live
    if len(live) != 1:
        record["verified"] = False
        raise TrialInvalid(f"probe Pod가 {len(live)}개 - 실제 이미지 확인 불가: {[p['name'] for p in live]}")
    record["observed_image_id"] = live[0]["image_id"]
    if live[0]["image_id"] != expected_id:
        record["verified"] = False
        raise TrialInvalid(f"probe 이미지 불일치(무효): 실제 {live[0]['image_id']!r} != 예상 {expected_id}")
    record["verified"] = True


WATCH_NODES = {"sj-worker": "192.168.30.76:9100", "sj-worker2": "192.168.30.117:9100"}  # node-exporter instance
MEM_DROP_ABORT_GIB = 1.0  # 2026-10-02 실행 전 고정: 시작 시 MemAvailable보다 이만큼 줄면 중단(결과 보고 바꾸지 않음)
MEM_READ_FAIL_LIMIT = 3   # 메모리·노드 상태 연속 조회 실패 허용 횟수(초과 시 중단)
DISK_RECHECK_SEC = 60.0
MEM_SAMPLE_MAX_AGE_SEC = 60.0


def read_worker_mem_available() -> dict:
    """worker별 MemAvailable(bytes) - 오래된 표본(>60초)은 없는 것으로 친다."""
    import requests
    r = requests.get(f"{rt.LOCAL_PROMETHEUS_URL}/api/v1/query", params={"query": "node_memory_MemAvailable_bytes"},
                     timeout=10)
    r.raise_for_status()
    now, by_inst = time.time(), {}
    for s in r.json()["data"]["result"]:
        ts, val = s["value"]
        if now - float(ts) <= MEM_SAMPLE_MAX_AGE_SEC:
            by_inst[s["metric"].get("instance")] = float(val)
    return {n: by_inst[i] for n, i in WATCH_NODES.items() if i in by_inst}


def _read_node_conditions(node: str) -> dict:
    from memory_pressure_adapter import get_node_conditions
    return get_node_conditions(node)


# 2026-10-02 pair01 A 사용자 지시 - preview가 준비된 뒤·압박 전에 preview 노드 여유·Calico·Pressure·서비스 재확인.
# 1.5GiB = 실행 중 감시가 중단하는 감소폭(MEM_DROP_ABORT_GIB 1.0) + 그때도 남길 0.5GiB. 운영 여유이지 검증된 안전 기준 아님.
PRE_STRESS_PREVIEW_NODE_MIN_AVAILABLE_GIB = 1.5


def _kubectl_json(args: list) -> dict:
    r = subprocess.run(["kubectl", *args, "-o", "json"], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} 실패: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout)


def read_calico_node_state(node: str) -> dict:
    """그 노드의 calico-node Pod 상태(읽기 전용) + Prometheus working set(GiB)."""
    items = _kubectl_json(["get", "pods", "-n", "kube-system", "-l", "k8s-app=calico-node",
                           "--field-selector", f"spec.nodeName={node}"]).get("items", [])
    if len(items) != 1:
        raise RuntimeError(f"{node}의 calico-node Pod {len(items)}개")
    st = items[0].get("status", {})
    cs = st.get("containerStatuses") or []
    import requests
    q = (f'sum(container_memory_working_set_bytes{{namespace="kube-system",pod=~"calico-node.*",'
         f'container="",node="{node}"}})')
    res = requests.get(f"{rt.LOCAL_PROMETHEUS_URL}/api/v1/query", params={"query": q}, timeout=10).json()["data"]["result"]
    return {"pod": items[0]["metadata"]["name"], "ready": st.get("phase") == "Running" and bool(cs)
            and all(c.get("ready") for c in cs), "restarts": sum(c.get("restartCount", 0) for c in cs),
            "working_set_gib": round(float(res[0]["value"][1]) / 2 ** 30, 3) if res else None}


def read_active_service() -> dict:
    """vllm-active endpoint와 그 Pod의 /health(API 서버 pod proxy, 읽기 전용)."""
    subsets = _kubectl_json(["get", "endpoints", "vllm-active", "-n", "vllm-serving"]).get("subsets") or []
    addrs = [a for s in subsets for a in (s.get("addresses") or [])]
    out = {"endpoint_count": len(addrs), "pod": None, "health_ok": False}
    if len(addrs) == 1 and (addrs[0].get("targetRef") or {}).get("name"):
        out["pod"] = addrs[0]["targetRef"]["name"]
        r = subprocess.run(["kubectl", "get", "--raw",
                            f"/api/v1/namespaces/vllm-serving/pods/{out['pod']}:8000/proxy/health"],
                           capture_output=True, text=True, timeout=30)
        out["health_ok"] = r.returncode == 0
    return out


def check_pre_stress_gate(record: dict, preview_node: str, *, mem_fn: Callable = None, cond_fn: Callable = None,
                          calico_fn: Callable = None, service_fn: Callable = None) -> None:
    """한 번만, 압박 전에(첫 안전검사 = 기준선 대기 중). 실패·조회 불가면 TrialInvalid → 정리, 압박 안 함."""
    from memory_pressure_adapter import _node_healthy
    g = record.setdefault("pre_stress", {"preview_node": preview_node,
                                          "min_available_gib": PRE_STRESS_PREVIEW_NODE_MIN_AVAILABLE_GIB,
                                          "checked_at": datetime.now(timezone.utc).isoformat()})
    try:
        mem = (mem_fn or read_worker_mem_available)()
        g["node_conditions"] = (cond_fn or _read_node_conditions)(preview_node)
        g["calico"] = (calico_fn or read_calico_node_state)(preview_node)
        g["active_service"] = (service_fn or read_active_service)()
        g["available_gib"] = round(mem[preview_node] / 2 ** 30, 3) if preview_node in mem else None
    except Exception as e:  # noqa: BLE001 - 확인 불가 = 불확실 → 시작 안 함
        g.update(ok=False, error=f"{type(e).__name__}: {e}")
        raise TrialInvalid(f"압박 전 확인 불가: {g['error']}") from e
    bad = []
    if not _node_healthy(g["node_conditions"]):
        bad.append(f"node_conditions={g['node_conditions']}")
    if g["available_gib"] is None or mem[preview_node] / 2 ** 30 < PRE_STRESS_PREVIEW_NODE_MIN_AVAILABLE_GIB:
        bad.append(f"available_gib={g['available_gib']}<{PRE_STRESS_PREVIEW_NODE_MIN_AVAILABLE_GIB}")
    if not g["calico"].get("ready"):
        bad.append(f"calico_not_ready={g['calico']}")
    if g["active_service"].get("endpoint_count") != 1 or not g["active_service"].get("health_ok"):
        bad.append(f"active_service={g['active_service']}")
    g["ok"], g["failures"] = not bad, bad
    if bad:
        raise TrialInvalid(f"압박 전 확인 실패(preview 노드 {preview_node}): {bad}")


def check_run_guard(record: dict, injector, state: dict, *, mem_fn: Callable = read_worker_mem_available,
                    cond_fn: Callable = _read_node_conditions, disk_fn: Callable = None,
                    monotonic_fn=time.monotonic, pre_stress_fn: Callable = None) -> None:
    """2026-10-02 사용자 지시 - 압박 시작 전 preview 실제 배치(다른 worker) 확인, 실행 내내 두 worker의
    상태·MemAvailable 감소·디스크 오류를 감시. 이상이면 TrialInvalid/HarnessCorrupted(정리 경로로 중단)."""
    from memory_pressure_adapter import _node_healthy
    if not record.get("preview_verified"):
        info = getattr(injector, "get_real_promotion_diagnostic_exception_info", lambda: None)() or {}
        actual, start = info.get("actual_node"), state.get("start_active_node")
        record.update(preview_actual_node=actual, start_active_node=start)
        if actual not in WATCH_NODES or start not in WATCH_NODES or actual == start:
            raise TrialInvalid(f"preview 실제 배치 노드 이상: preview={actual}, active={start} - 압박 시작 안 함")
        record["preview_verified"] = True
    if not (record.get("pre_stress") or {}).get("ok"):
        (pre_stress_fn or check_pre_stress_gate)(record, record["preview_actual_node"])
    try:
        conds = {n: cond_fn(n) for n in WATCH_NODES}
        mem = mem_fn()
        if set(mem) != set(WATCH_NODES):
            raise RuntimeError(f"MemAvailable 표본 누락: {sorted(mem)}")
        record["read_failures"] = 0
    except Exception as e:  # noqa: BLE001
        record["read_failures"] = record.get("read_failures", 0) + 1
        record["last_read_error"] = f"{type(e).__name__}: {e}"
        if record["read_failures"] >= MEM_READ_FAIL_LIMIT:
            raise HarnessCorrupted(f"노드 상태/메모리 {record['read_failures']}회 연속 조회 실패: {record['last_read_error']}")
        return
    bad = {n: c for n, c in conds.items() if not _node_healthy(c)}
    if bad:
        record["node_condition_failure"] = bad
        raise HarnessCorrupted(f"worker 노드 상태 이상: {bad}")
    gib = {n: round(v / 2 ** 30, 3) for n, v in mem.items()}
    base = record.setdefault("mem_baseline_gib", dict(gib))
    mins = record.setdefault("mem_min_gib", dict(gib))
    for n, v in gib.items():
        mins[n] = min(mins[n], v)
        if base[n] - v >= MEM_DROP_ABORT_GIB:
            record["mem_abort"] = {"node": n, "baseline_gib": base[n], "now_gib": v}
            raise HarnessCorrupted(f"{n} MemAvailable 감소 {base[n] - v:.2f}GiB ≥ {MEM_DROP_ABORT_GIB}GiB - 중단")
    now = monotonic_fn()
    if now - record.get("_last_disk_check", float("-inf")) >= DISK_RECHECK_SEC:
        record["_last_disk_check"] = now
        disk = (disk_fn or (lambda: node_disk_health_gate.check_all_nodes(lambda: list(WATCH_NODES))))()
        record["disk_checks"] = record.get("disk_checks", 0) + 1
        if not disk.get("ok"):
            record["disk_failure"] = disk.get("reasons")
            raise HarnessCorrupted(f"실행 중 디스크 검사 실패: {disk.get('reasons')}")


def _cpu_counter_selector(pod: str) -> str:
    """_cpu_promql(pod) = rate(<이 선택자>[30s]) - 탐지기·기존 판정과 같은 시계열의 원시 누적 카운터."""
    return f'container_cpu_usage_seconds_total{{namespace="vllm-serving",pod="{pod}",container="vllm"}}'


def prom_raw_counter(selector: str, start: datetime, end: datetime) -> list:
    """Prometheus에 실제 저장된 원시 표본 [(ISO 시각, 값)] - 조회 격자·rate 창을 거치지 않는다.
    시계열이 정확히 1개가 아니면(컨테이너 교체 등) 모호하므로 예외(판정 불가로 기록)."""
    import requests
    r = requests.get(f"{rt.LOCAL_PROMETHEUS_URL}/api/v1/query", timeout=15,
                     params={"query": f"{selector}[{int((end - start).total_seconds()) + 1}s]", "time": end.timestamp()})
    r.raise_for_status()
    result = r.json()["data"]["result"]
    if len(result) != 1:
        raise RuntimeError(f"CPU 카운터 시계열 {len(result)}개 - 1개여야 함")
    return [(datetime.fromtimestamp(float(t), timezone.utc).isoformat(), float(v)) for t, v in result[0]["values"]]


# 2026-10-02 pilot03 무효(이 PC commit 여유 약 1.1GiB에서 kubectl 프로세스 생성 실패). 실행 중 이 PC에 실행기·score_server·
# capture_sink·kubectl 프로세스가 함께 뜨며 Python 프로세스 하나가 약 0.5GiB를 예약한다(같은 날 관측) → 약 2GiB, 2배 여유.
MIN_LOCAL_COMMIT_AVAILABLE_GIB = 4.0


def read_local_commit() -> dict:
    """이 PC(Windows)의 메모리 예약(commit) 한도·여유 - GlobalMemoryStatusEx(ullTotalPageFile/ullAvailPageFile)."""
    import ctypes

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    s = MemoryStatusEx()
    s.dwLength = ctypes.sizeof(s)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s)):
        raise OSError("GlobalMemoryStatusEx 실패")
    gib = 2 ** 30
    return {"commit_limit_gib": s.ullTotalPageFile / gib, "commit_available_gib": s.ullAvailPageFile / gib,
            "ram_available_gib": s.ullAvailPhys / gib}


def check_local_commit(read_fn: Callable = None) -> dict:
    """여유가 MIN_LOCAL_COMMIT_AVAILABLE_GIB 미만이거나 읽지 못하면 ok=False(시작 안 함)."""
    try:
        raw = (read_fn or read_local_commit)()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "min_required_gib": MIN_LOCAL_COMMIT_AVAILABLE_GIB,
                "reason": f"local_commit_unreadable: {type(e).__name__}: {e}"}
    ok = raw["commit_available_gib"] >= MIN_LOCAL_COMMIT_AVAILABLE_GIB  # 판정은 반올림 전 원래 값으로
    m = {k: round(v, 2) for k, v in raw.items()}  # 반올림은 기록 표시용
    return {"ok": ok, **m, "commit_available_gib_raw": raw["commit_available_gib"],
            "min_required_gib": MIN_LOCAL_COMMIT_AVAILABLE_GIB,
            "reason": None if ok else (f"local_commit_available {m['commit_available_gib']}GiB < "
                                       f"{MIN_LOCAL_COMMIT_AVAILABLE_GIB}GiB - 이 PC 메모리 예약 여유 부족")}


def _harness_error_outcome(injector, error: str) -> dict:
    """시험 함수가 하네스 오류로 끝났을 때의 결과 - 주입 시각은 injector의 실측 기록에서 되살린다."""
    try:
        t_inj = (injector.get_stress_delivery_record() or {}).get("injection_started_at")
    except Exception:  # noqa: BLE001
        t_inj = None
    return {"status": "harness_error", "reason": error, "invalidity_reasons": ["harness_error"],
            "cleanup_status": "unknown(trial_result_lost)", "t_injection_requested_utc": None,
            "t_injection_observed_utc": t_inj, "observation_ended_at_utc": None,
            "injection_time_source": "stress_delivery_record.injection_started_at" if t_inj else None}


def _preview_prep_info(injector) -> Optional[dict]:
    """preview 준비 실측(prep_duration_sec 등). 조회 실패는 오류 문자열로 남긴다."""
    try:
        return getattr(injector, "get_preview_prep_info", lambda: None)()
    except Exception as e:  # noqa: BLE001
        return {"fetch_error": f"{type(e).__name__}: {e}"}


def _contract(run_id: str, arm: str, pair: str) -> dict:
    pilot = pair.startswith("pilot")
    return {
        "run_id": run_id, "plan_id": PLAN_ID, "arm": arm, "pair": pair, "is_pilot": pilot,
        "purpose": PILOT_PURPOSE if pilot else MAIN_PURPOSE,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "detector": "score_server.py, 후보 artifacts(threshold −0.04, 3연속)",
        "probe_image": PROBE_IMAGE, "probe_image_id": IMAGE_FORCE_CLOSE_ID,
        "probe_image_note": "force_close=True probe(요청마다 새 연결) - 두 worker에 같은 tar로 반입, /probe.py 내용 확인됨(2026-10-02)",
        "policy_connection": ("A: capture sink(승격 불가)" if arm == "control" else "B: 실제 recovery-policy(실제 승격 가능)"),
        "stages": DEMO_STAGES, "stress_target": "주입 시점 active Pod(이름·UID 고정), 전환 뒤에도 4단계 끝까지",
        "observation_sec_after_injection_observed": OBSERVATION_SEC,
        "scale_down_delay_override_sec": SCALE_DOWN_DELAY_SEC if arm == "switch" else None,
        "scale_down_delay_note": ("420초 - pilot04 B(2026-10-02)에서 적용 확인, 구 Pod Killing이 전환+420초(압박 종료 134초 뒤), "
                                  "30 원복 라이브 확인. IF 미전환 B의 원복 경로는 오프라인 테스트만"),
        "main_contract": None if pilot else MAIN_CONTRACT_ID,
        "pair_order": "쌍마다 A -> B 고정, 같은 active 노드에서 시작. 순서 무작위화 없음(시간대·캐시·노드 상태가 B에 섞일 수 있음)",
        "analysis": "B는 배정 기준(IF 미전환 B도 포함). 주입 전 승격은 비교 제외 + 불필요 실제 조치로 별도 기록",
        "not_claimed": "고정 규칙의 실제 조치보다 IF가 낫다는 주장(이 A/B에 고정 규칙 군이 없음)",
        "primary_metric": ("누적 SLO 위반시간(관찰 구간의 모든 위반 구간 합계, slo_judge v3 미수정 + slo_episodes.py) - "
                           "쌍별 A−B, 사후 계산. 보조: 첫 위반~첫 회복(v2-main-contract-20261003)"),
        "stress_delivery_criterion": verdict.STRESS_CRITERION_ID,
        "stress_delivery_criterion_doc": "docs/design/phase8-v2-stress-delivery-criterion-20261002.md",
    }


def main():
    parser = argparse.ArgumentParser(description="V2 A/B: 압박 지속 중 IF 자동 전환(B) vs 무전환(A)")
    parser.add_argument("--arm", required=True, choices=ARMS)
    parser.add_argument("--pair", required=True, type=_pair_arg, help="pilot01~pilot04(시험) 또는 1~18(= pair01~pair18)")
    parser.add_argument("--dry-run-contract-only", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()

    run_id, rep = run_id_for(args.arm, args.pair), PAIR_REPS[args.pair]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = RESULTS_DIR / f"summary-{run_id}.json"
    if summary_path.exists():
        raise SystemExit(f"이미 결과가 있음 - 덮어쓰지 않음: {summary_path}")
    # 같은 클러스터를 쓰는 정시 전환 실험의 미해결 흔적도 막는다
    rt.check_no_unresolved_failures(RESULTS_DIR)
    rt.check_no_unresolved_failures(rt.RESULTS_DIR)
    for other in ("capstone_v1_detector", "capstone_input_capture_diagnostic", "capstone_candidate_parallel_diagnostic",
                  "capstone_c1_normal_collection", "capstone_c1_shadow_pilot",
                  "capstone_v33c1_validation"):  # 2026-10-04 같은 클러스터의 V1·진단 흔적
        rt.check_no_unresolved_failures(RESULTS_DIR.parent / other)
    expected_node = check_pair_precondition(args.arm, args.pair, RESULTS_DIR)
    contract_path = RESULTS_DIR / f"contract-{run_id}.json"
    if args.dry_run_contract_only:
        contract_path.write_text(json.dumps(_contract(run_id, args.arm, args.pair), ensure_ascii=False, indent=2,
                                            default=str), encoding="utf-8")
        print("dry-run - 클러스터 미접속, 계약만 기록")
        return

    preflight = rt.run_preflight()
    preflight["local_commit"] = check_local_commit()  # 2026-10-02 pilot03: 이 PC 메모리 예약 부족으로 실행기 중단
    if not preflight["local_commit"]["ok"]:
        preflight["ok"] = False
        preflight.setdefault("reasons", []).append(preflight["local_commit"]["reason"])
    (RESULTS_DIR / f"preflight-{run_id}.json").write_text(
        json.dumps(preflight, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if not preflight["ok"]:
        raise SystemExit(f"사전검사 실패 - 시작 안 함(클러스터 변경 없음): {preflight['reasons']}")
    if args.preflight_only:
        print(f"사전검사 통과(읽기 전용): {preflight.get('request_counter')}")
        return
    contract_path.write_text(json.dumps(_contract(run_id, args.arm, args.pair), ensure_ascii=False, indent=2,
                                        default=str), encoding="utf-8")

    state = {}
    injector = make_cpu_pressure_injector(run_id, ARM_LABEL, rep, stages=DEMO_STAGES,
                                          continue_after_target_replacement=True)
    injector = arm_controller.wrap_injector_with_preview_prep(injector, ARM_LABEL)
    injector = node_resource_safety_gate.wrap_injector_with_real_promotion_diagnostic_exception(
        injector, ARM_LABEL, node_resource_safety_gate.REAL_PROMOTION_DIAGNOSTIC_EXPECTED_RUN_MODE)
    override_record_path = RESULTS_DIR / f"scale-down-delay-override-{run_id}.json"
    if args.arm == "switch":
        state["override_record_path"] = override_record_path
    rt.compose_prepare(injector, arm=args.arm, expected_start_node=expected_node, state=state,
                       apply_override_fn=lambda: sdo.apply_override(SCALE_DOWN_DELAY_SEC, override_record_path))
    prober = make_load_ramp_prober(DEFAULT_PROBE_CONFIG, run_id, SCENARIO, ARM_LABEL, rep,
                                   BASELINE_TIMEOUT_SEC + PROBE_INJECTION_START_MARGIN_SEC + OBSERVATION_SEC,
                                   image=PROBE_IMAGE)
    probe_raw_csv_path = PROBE_RESULTS_DIR / f"probe-{run_id}-{ARM_LABEL}-{rep}-raw.csv"
    evidence_path = RESULTS_DIR / f"detector-evidence-{run_id}.jsonl"
    sampler = rt.TimedSwitch(injector=injector, send_signal=False, run_id=run_id,
                             evidence_path=RESULTS_DIR / f"samples-{run_id}.jsonl", switch_at_sec=None)
    events_since = datetime.now(timezone.utc).isoformat()

    running_marker = RESULTS_DIR / f"running-{run_id}.json"
    running_marker.write_text(json.dumps({"run_id": run_id, "started_at": events_since}), encoding="utf-8")

    def _terminate(signum, _frame):  # SIGTERM·Ctrl+Break도 finally 정리를 타게 한다(kill -9는 불가)
        raise KeyboardInterrupt(f"signal {signum}")
    old_handlers = {getattr(signal, n): signal.signal(getattr(signal, n), _terminate)
                    for n in ("SIGTERM", "SIGBREAK") if hasattr(signal, n)}

    outcome, exp_state, restore, cleanup_errors = None, None, None, []
    trial_error, post_run_probe_pods = None, None
    probe_image_check = {}  # 실행 중 실제 probe 이미지 ID 확인 기록
    run_guard = {"mem_drop_abort_gib": MEM_DROP_ABORT_GIB, "disk_recheck_sec": DISK_RECHECK_SEC}
    lifecycle_errors = []  # 탐지기·표본기 종료 실패(정리 오류로 합쳐 다음 실행을 막는다)
    try:
        _register_experiment_context(run_id, SCENARIO, ARM_LABEL, rep, events_since)
        base_safety = rt.make_safety_fn(injector)

        def safety(baseline_state, prober_):  # 첫 호출은 기준선 대기 중(탐지기·주입 전)
            base_safety(baseline_state, prober_)
            check_run_guard(run_guard, injector, state)
            verify_probe_image(probe_image_check)
        try:
            if args.arm == "control":
                outcome = run_no_switch_control_trial(
                    run_id, injector, prober, _pick_free_loopback_port(), evidence_path,
                    RESULTS_DIR / f"sink-captured-{run_id}.jsonl", probe_raw_csv_path,
                    post_injection_observation_sec=OBSERVATION_SEC, check_safety_fn=safety,
                    detector_start_fn=lambda rid, path, sink_url: with_sampler(
                        _start_candidate_score_server_detector_for_sink(rid, path, sink_url), sampler, lifecycle_errors))
            else:
                outcome = run_cpu_pressure_trial(
                    run_id, injector, prober, evidence_path, probe_raw_csv_path,
                    post_injection_observation_sec=OBSERVATION_SEC, check_safety_fn=safety,
                    detector_start_fn=lambda rid, path: with_sampler(
                        _start_candidate_score_server_detector(rid, path), sampler, lifecycle_errors))
        except Exception as e:  # noqa: BLE001 - 2026-10-02 pilot03: 하네스 오류(WinError 1455)로 요약·단계 기록이 유실됨
            # 시험 함수 내부 정리(finally)는 이미 실행됐다. 결과를 잃지 않도록 주입 시각·단계 기록을 injector에서 되살려
            # 요약을 남기고, 정리 결과는 아래 사후 확인과 함께 cleanup_errors로 남겨 다음 실행을 막는다.
            trial_error = f"{type(e).__name__}: {e}"
            outcome = _harness_error_outcome(injector, trial_error)
    finally:
        try:
            exp_state = _get_experiment_state(ARM_LABEL)
        except Exception as e:  # noqa: BLE001
            exp_state = {"fetch_error": f"{type(e).__name__}: {e}"}
        try:
            _clear_experiment_context(run_id, ARM_LABEL)
        except Exception as e:  # noqa: BLE001
            cleanup_errors.append(f"experiment_context_clear: {type(e).__name__}: {e}")
        restore = rt.finalize_override(state, current_active_selector_fn=rt.active_selector,
                                       restore_fn=sdo.restore_override)
        if restore.get("restored") is False:
            cleanup_errors.append(f"scale_down_delay_restore: {restore.get('error')}")
        if state.get("override_error") and state.get("override_rollback_ok") is False:
            cleanup_errors.append(f"scale_down_delay_apply_rollback_failed: {state['override_error']}")
        cleanup_errors.extend(lifecycle_errors)
        if outcome is not None and outcome.get("cleanup_status") != "complete":
            cleanup_errors.append(f"trial_cleanup_incomplete: prober={outcome.get('prober_stop_failed')}, "
                                  f"injector={outcome.get('injector_cleanup_failed')}")
        try:
            left = rt.wait_chaos_gone(injector.get_stress_delivery_record().get("planned_cr_names") or [])
            if left:
                cleanup_errors.append(f"stress_chaos_residual: {left}")
        except Exception as e:  # noqa: BLE001
            cleanup_errors.append(f"stress_chaos_check_failed: {type(e).__name__}: {e}")
        if trial_error:
            cleanup_errors.append(f"harness_error: {trial_error}")
            try:  # 시험 함수의 정리 결과는 예외와 함께 사라졌으므로 probe Pod 잔여를 직접 확인해 남긴다
                post_run_probe_pods = [p for p in read_probe_pods() if not p["terminating"]]
                if post_run_probe_pods:
                    cleanup_errors.append(f"probe_pod_residual: {[p['name'] for p in post_run_probe_pods]}")
            except Exception as e:  # noqa: BLE001
                cleanup_errors.append(f"probe_pod_check_failed: {type(e).__name__}: {e}")
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)

    # 계약 v2 §7(2026-10-04): 새 Pod UID 확인에 쓰므로 판정 전에 수집한다(파일은 기존과 같은 이름).
    events, pod_events_error = [], None
    try:
        captured = _capture_recent_pod_events(since_utc=events_since)
        (RESULTS_DIR / f"pod-events-{run_id}.json").write_text(
            json.dumps(captured, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        events = captured  # 파일로 남은 증거만 판정에 쓴다(재판정 재현성)
        pod_events_error = next((e["capture_error"] for e in events if "capture_error" in e), None)
    except Exception as e:  # noqa: BLE001
        pod_events_error = f"{type(e).__name__}: {e}"

    # ---- 실측 수집과 판정 - 오류가 나도 요약은 남긴다(비교 제외로) ----
    delivery, cpu_error, evidence_error, cpu_raw = {}, None, None, []
    stress, traffic, provenance, selector_changed, audit = {"verified": None}, None, {"verified": None}, None, None
    try:
        delivery = injector.get_stress_delivery_record()
        target = delivery.get("target") or {}
        t0 = datetime.fromisoformat(outcome["t_injection_observed_utc"]) if outcome.get("t_injection_observed_utc") else None
        selector_changed = rt.selector_changed_from(sampler.samples, exp_state)
        audit = fetch_audit_until_settled(run_id, expect_promotion=bool(selector_changed))
        provenance = verdict.evaluate_detector_switch_provenance(
            audit=audit, exp_state=exp_state, run_id=run_id, t_injection=outcome.get("t_injection_observed_utc"),
            signal_results=load_signal_results(evidence_path), expected_threshold=CANDIDATE_THRESHOLD,
            expected_model_hash=CANDIDATE_MODEL_SHA256)
        t_switch = provenance.get("t_switch") if provenance.get("switched") else None
        if t0 and target.get("name"):
            try:
                cpu_raw = prom_raw_counter(_cpu_counter_selector(target["name"]), t0 - timedelta(seconds=60),
                                           t0 + timedelta(seconds=rt.PLANNED_STRESS_SEC + 120))
            except Exception as e:  # noqa: BLE001
                cpu_error = f"{type(e).__name__}: {e}"
        stress = verdict.evaluate_stress_delivery_raw(  # 기준 v2(2026-10-02) - A·B 동일
            delivery_record=delivery, planned_stage_count=len(DEMO_STAGES), cpu_raw_samples=cpu_raw,
            liveness_samples=sampler.samples, baseline_cpu=target.get("baseline_cpu_cores"),
            min_rise=MIN_EFFECTIVE_CPU_RISE_CORES, sample_interval_sec=rt.SAMPLE_INTERVAL_SEC,
            planned_total_sec=rt.PLANNED_STRESS_SEC, continuity_from=t_switch)
        if t_switch:
            # 계약 v2 §7: 저장된 표본·Pod 이벤트·preview 해시로 식별(압박 도구 교체 기록·현재 상태에 기대지 않음)
            ident = verdict.identify_new_pod(
                t_switch=t_switch, created_pod_hash=(_preview_prep_info(injector) or {}).get("created_pod_hash"),
                samples=sampler.samples, pod_events=events, target_replacement=delivery.get("target_replacement"))
            new_pod, new_ip = ident["name"], ident["ip"]
            lo = datetime.fromisoformat(t_switch) + timedelta(seconds=10)
            hi = lo + timedelta(seconds=rt.TRAFFIC_WINDOW_SEC)
            edge = verdict.MAX_LIVENESS_GAP_FACTOR * rt.SAMPLE_INTERVAL_SEC
            traffic = verdict.evaluate_new_pod_traffic(
                t_switch=t_switch, new_pod_ip=new_ip, endpoint_samples=sampler.samples,
                new_pod_increase=verdict.counter_delta(sampler.samples, new_pod, lo, hi, edge),
                old_pod_increase=verdict.counter_delta(sampler.samples, target.get("name"), lo, hi, edge))
            traffic.update(new_pod=new_pod, new_pod_ip=new_ip, new_pod_identification=ident)
    except Exception as e:  # noqa: BLE001
        evidence_error = f"{type(e).__name__}: {e}"
        stress = {"verified": None, "reasons": [f"evidence_error: {evidence_error}"]}

    comparison = verdict.detector_ab_eligibility(
        arm=args.arm, outcome_status=outcome["status"], invalidity_reasons=outcome.get("invalidity_reasons"),
        stress=stress, traffic=traffic, provenance=provenance, active_selector_changed=selector_changed,
        override_verified=(state.get("override") or {}).get("verified") if args.arm == "switch" else None,
        cleanup_errors=cleanup_errors)
    if not probe_image_check.get("verified"):  # 실제 이미지 미확인 실행은 비교에서 제외
        comparison = {**comparison, "eligible": False,
                      "reasons": [*(comparison.get("reasons") or []), "probe_image_not_verified"]}

    summary = {
        "run_id": run_id, "arm": args.arm, "pair": args.pair, "is_pilot": args.pair.startswith("pilot"),
        "purpose": PILOT_PURPOSE if args.pair.startswith("pilot") else MAIN_PURPOSE,
        "outcome_status": outcome["status"], "outcome_reason": outcome["reason"],
        "invalidity_reasons": outcome.get("invalidity_reasons"), "cleanup_status": outcome.get("cleanup_status"),
        "t_injection_requested_utc": outcome.get("t_injection_requested_utc"),
        "t_injection_observed_utc": outcome.get("t_injection_observed_utc"),
        "observation_ended_at_utc": outcome.get("observation_ended_at_utc"),
        "probe_raw_csv": str(probe_raw_csv_path), "detector_evidence": str(evidence_path),
        "probe_image": PROBE_IMAGE, "probe_image_id": IMAGE_FORCE_CLOSE_ID,
        "harness_error": trial_error, "post_run_probe_pods": post_run_probe_pods,
        "probe_image_runtime_check": probe_image_check,
        "run_guard": {k: v for k, v in run_guard.items() if not k.startswith("_")},
        "preview_prep": _preview_prep_info(injector), "preview_prep_timeout_sec": arm_controller.PREVIEW_PREP_TIMEOUT_SEC,
        "start_active_node": state.get("start_active_node"), "preflight": preflight,
        "experiment_run_audit_timing": exp_state, "audit": audit, "switch_provenance": provenance,
        "stress_delivery": stress, "stress_delivery_record": delivery, "cpu_collection_error": cpu_error,
        "stress_delivery_criterion": verdict.STRESS_CRITERION_ID, "cpu_raw_counter_samples": cpu_raw,
        "evidence_error": evidence_error, "active_selector_changed": selector_changed, "new_pod_traffic": traffic,
        "scale_down_delay_override": state.get("override"), "scale_down_delay_override_error": state.get("override_error"),
        "scale_down_delay_restore": restore, "cleanup_errors": cleanup_errors, "pod_events_error": pod_events_error,
        "comparison": comparison,
        "pilot_a_slo_gate_file": (str(RESULTS_DIR / f"pilot-slo-gate-{run_id}.json")
                                  if args.arm == "switch" and args.pair.startswith("pilot") else None),
        "slo_note": "t_slo/t_recovery는 probe 원자료에 slo_judge(v3, 미수정)를 사후 적용해 계산한다(여기서 계산하지 않음).",
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if not cleanup_errors:  # 2026-10-02: 정리·원복이 불완전하면 실행 중 표시를 보존(요약의 cleanup_errors와 함께 다음 실행 차단)
        running_marker.unlink()
    print(f"결과: {outcome['status']} / 비교 자격={comparison['eligible']} b_status={comparison['b_status']} "
          f"{comparison['reasons']} / {summary_path}")
    if cleanup_errors:
        print(f"[중단] 정리·원복 실패 - 다음 실행은 자동으로 거부됨, 수동 확인 필요: {cleanup_errors}")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
