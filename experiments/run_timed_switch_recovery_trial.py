#!/usr/bin/env python3
"""2026-10-02 - 정시 전환 회복 실험 러너(A=무전환 대조, B=+240초 정시 전환).

목적: "전환 자체가 SLO 회복에 주는 효과"만 본다(AI 탐지 효과 아님 - 전환 시각을 타이머로
고정). 두 군 모두 같은 CPU 압박(DEMO_STAGES 4x90초)을 원래 active Pod(이름·UID 고정)에
끝까지 걸고(continue_after_target_replacement), B는 scaleDownDelaySeconds를 이 실행 동안만
180초로 늘려 구 Pod가 압박 종료 뒤까지 살아있게 한다. 관찰은 주입 확인 후 600초.

순서: 쌍마다 A -> B 고정(같은 쌍은 같은 active 노드에서 시작, B는 A 요약을 확인해야 시작).
노드를 되돌리기 위한 추가 승격은 하지 않는다 - 다음 쌍은 바뀐 노드에서 시작할 수 있다.

기존 공유 코드(run_if_threshold_candidate_smoke.run_cpu_pressure_trial, arm_controller,
node_resource_safety_gate, load_ramp_adapter, slo_judge)는 수정하지 않고 조립만 한다."""
import argparse
import json
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import arm_controller
import node_disk_health_gate
import node_resource_safety_gate
import scale_down_delay_override as sdo
import timed_switch_verdict as verdict
from active_pod_resolver import get_active_pods
from blue_green_prep import get_blue_green_status
from cpu_pressure_demo_adapter import MIN_EFFECTIVE_CPU_RISE_CORES, _cpu_promql, make_cpu_pressure_injector
from load_ramp_adapter import RESULTS_DIR as PROBE_RESULTS_DIR
from load_ramp_adapter import make_load_ramp_prober
from run_cpu_demo_trial import DEMO_STAGES
from run_if_threshold_candidate_smoke import (
    BASELINE_TIMEOUT_SEC, DEFAULT_PROBE_CONFIG, PROBE_INJECTION_START_MARGIN_SEC, REAL_RECOVERY_POLICY_SIGNAL_URL,
    DetectorHandle, _capture_recent_pod_events, _check_safety_with_injection, run_cpu_pressure_trial,
)
from run_once import TrialInvalid, _clear_experiment_context, _get_experiment_state, _register_experiment_context

PLAN_ID = "capstone-timed-switch-recovery-205-v1"
SCENARIO = "timed_switch_recovery"
RESULTS_DIR = Path(__file__).parent / "results" / "capstone_timed_switch_recovery"
ARMS = ("control", "switch")
ARM_LABEL = "proposed"  # preview 준비 경로(arm_controller._DETECTOR_SCRIPTS) 이름 - 실험군 이름과 분리
NAMESPACE = "vllm-serving"
SWITCH_AT_SEC = 240.0
OBSERVATION_SEC = 600.0
SCALE_DOWN_DELAY_SEC = 180
PLANNED_STRESS_SEC = sum(s["duration_sec"] for s in DEMO_STAGES)
SAMPLE_INTERVAL_SEC = 5.0
LOCAL_PROMETHEUS_URL = "http://localhost:9090"
LOCAL_RECOVERY_POLICY_URL = "http://localhost:8080"
# Pod별 요청 수: Prometheus가 아니라 API 서버 pod proxy로 각 Pod의 /metrics를 직접 읽는다(읽기 전용 GET).
# ServiceMonitor는 vllm-active/vllm-preview Service를 통해 수집하므로, 전환 뒤 구 Pod는 어느 Service에도
# 속하지 않아 Prometheus에서 사라진다(구 Pod 요청 0건을 증명할 수 없음). 사전검사에서 active Pod에 이
# 지표가 실제로 있는지 확인하고, 없으면 A·B 모두 시작하지 않는다(쌍 전체가 검증 불가가 되므로).
REQUEST_COUNTER_METRIC = "vllm:request_success_total"
VLLM_METRICS_PORT = 8000
TRAFFIC_WINDOW_SEC = 60.0
SIGNAL_TIMEOUT = (3.0, 10.0)


# 쌍 라벨 -> 어댑터·probe·컨텍스트에 쓰는 rep 번호. pilot01은 장치·판정표 확인용 시험 쌍(본 실행과 별도 run_id).
PAIR_REPS = {"pilot01": 0, "pair01": 1, "pair02": 2, "pair03": 3}
PILOT_PURPOSE = ("시험 쌍 - 장치(지연 설정 변경·원복, 정시 전환, Pod별 카운터, 압박 지속)와 판정표 확인용. "
                 "SLO 효과 크기 주장에 쓰지 않으며 본 실행 반복 수에 포함하지 않는다.")


def pair_label(pair) -> str:
    """1/'1'/'pair01' -> 'pair01'(기존 run_id 그대로, 숫자와 라벨은 같은 쌍의 별칭), 'pilot01' -> 'pilot01'.
    그 외(pilot02, 4, 0 등)는 거부."""
    label = f"pair{int(pair):02d}" if str(pair).isdigit() else str(pair)
    if label not in PAIR_REPS:
        raise ValueError(f"알 수 없는 쌍: {pair!r} (허용: 1, 2, 3, pair01, pair02, pair03, pilot01)")
    return label


def _pair_arg(value: str) -> str:
    try:
        return pair_label(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def run_id_for(arm: str, pair) -> str:
    return f"{SCENARIO}-{arm}-{pair_label(pair)}-{PLAN_ID}"


def check_no_unresolved_failures(results_dir: Path = RESULTS_DIR) -> None:
    """이전 실행의 원복·정리 실패, 또는 실행 중 표시(running-*)만 남은(요약 저장 전 비정상 종료)
    흔적이 있으면 어떤 실행도 시작하지 않는다 - 수동 확인 후 사람이 정리해야 한다."""
    if not results_dir.exists():
        return
    for p in sorted(results_dir.glob("running-*.json")):
        raise SystemExit(f"실행 중 표시가 남아 있음(요약 없이 비정상 종료?) - 라이브 상태 수동 확인 전 실행 금지: {p.name}")
    for p in sorted(results_dir.glob("summary-*.json")):
        s = json.loads(p.read_text(encoding="utf-8"))
        if s.get("cleanup_errors"):
            raise SystemExit(f"이전 실행 정리 실패가 남아 있음 - 수동 확인 전 실행 금지: {p.name} {s['cleanup_errors']}")


def check_pair_precondition(arm: str, pair, results_dir: Path = RESULTS_DIR) -> Optional[str]:
    """B는 같은 쌍의 A가 완료·비교 자격·무전환으로 끝났을 때만 시작한다. 반환: A의 시작 active 노드."""
    if arm == "control":
        return None
    label = pair_label(pair)
    path = results_dir / f"summary-{run_id_for('control', label)}.json"
    if not path.exists():
        raise SystemExit(f"{label} B 거부: 같은 쌍 A 요약이 없음({path.name}) - A를 먼저 실행")
    a = json.loads(path.read_text(encoding="utf-8"))
    if not (a.get("comparison") or {}).get("eligible"):
        raise SystemExit(f"{label} B 거부: A가 비교 자격 없음 {(a.get('comparison') or {}).get('reasons')}")
    if not a.get("start_active_node"):
        raise SystemExit(f"{label} B 거부: A의 시작 active 노드 기록 없음")
    return a["start_active_node"]


# ---- 실측 표본(정시 신호 + 5초 간격 구 Pod 생존·endpoint·selector) ----

def _k8s_core():
    from kubernetes import client, config
    config.load_kube_config()
    return client.CoreV1Api()


def pod_state(name: str) -> dict:
    from kubernetes.client.rest import ApiException
    try:
        p = _k8s_core().read_namespaced_pod(name, NAMESPACE)
    except ApiException as e:
        if e.status == 404:
            return {"exists": False}
        raise
    cs = next((c for c in (p.status.container_statuses or []) if c.name == "vllm"), None)
    return {"exists": True, "uid": p.metadata.uid, "ip": p.status.pod_ip, "node": p.spec.node_name,
            "running": bool(cs and cs.state and cs.state.running),
            "terminating": p.metadata.deletion_timestamp is not None,
            "restart_count": cs.restart_count if cs else None}


def active_endpoint_ips() -> list:
    ep = _k8s_core().read_namespaced_endpoints("vllm-active", NAMESPACE)
    return sorted(a.ip for s in (ep.subsets or []) for a in (s.addresses or []))


def active_selector() -> Optional[str]:
    return get_blue_green_status("vllm-serving", NAMESPACE).get("active_selector")


def parse_counter(metrics_text: str, metric: str = REQUEST_COUNTER_METRIC) -> Optional[float]:
    """Prometheus 텍스트 노출 형식에서 metric의 모든 라벨 조합 값을 합한다. 줄이 하나도 없으면 None."""
    total, found = 0.0, False
    for line in (metrics_text or "").splitlines():
        if line.startswith(metric + "{") or line.startswith(metric + " "):
            try:
                total += float(line.rsplit(" ", 1)[1])
                found = True
            except ValueError:
                return None
    return total if found else None


def fetch_pod_metrics_text(pod: str) -> str:
    # 2026-10-02 - 기본 호출은 bytes의 repr("b'...\\n...'")을 str로 돌려줘 줄바꿈이 없다(카운터 항상 None).
    # 원본 응답을 받아 직접 디코드한다(라이브 읽기 전용 확인: 592줄, request_success_total=2.0).
    r = _k8s_core().connect_get_namespaced_pod_proxy_with_path(f"{pod}:{VLLM_METRICS_PORT}", NAMESPACE, "metrics",
                                                               _preload_content=False)
    return r.data.decode("utf-8")


def vllm_request_counters() -> dict:
    """namespace의 모든 vLLM Pod(active·preview·구 Pod) 카운터. Pod별 조회 실패는 None."""
    pods = _k8s_core().list_namespaced_pod(NAMESPACE, label_selector="app=vllm-serving").items
    out = {}
    for p in pods:
        try:
            out[p.metadata.name] = parse_counter(fetch_pod_metrics_text(p.metadata.name))
        except Exception:  # noqa: BLE001 - 조회 실패는 값 없음(판정 불가)으로 남는다
            out[p.metadata.name] = None
    return out


def _policy_healthy() -> bool:
    import requests
    return requests.get(f"{LOCAL_RECOVERY_POLICY_URL}/healthz", timeout=5).status_code == 200


def run_preflight(*, disk_check_fn: Callable = lambda: node_disk_health_gate.check_all_nodes(
                      node_disk_health_gate.list_cluster_nodes),
                  active_pods_fn: Callable = get_active_pods, metrics_text_fn: Callable = fetch_pod_metrics_text,
                  policy_healthy_fn: Callable = _policy_healthy) -> dict:
    """읽기 전용 사전검사. 하나라도 실패하면 ok=False(실행 시작 안 함). 예외를 던지지 않는다."""
    out = {"checked_at": datetime.now(timezone.utc).isoformat(), "reasons": []}
    out["disk"] = disk_check_fn()
    if not out["disk"].get("ok"):
        out["reasons"].append(f"disk_health: {out['disk'].get('reasons')}")
    try:
        pods = active_pods_fn()
        if len(pods) != 1:
            raise RuntimeError(f"active pod {len(pods)}개")
        value = parse_counter(metrics_text_fn(pods[0]["name"]))
        out["request_counter"] = {"pod": pods[0]["name"], "metric": REQUEST_COUNTER_METRIC, "value": value}
        if value is None:
            out["reasons"].append(f"request_counter_missing: {REQUEST_COUNTER_METRIC} on {pods[0]['name']}")
    except Exception as e:  # noqa: BLE001
        out["reasons"].append(f"request_counter_check_failed: {type(e).__name__}: {e}")
    try:
        if not policy_healthy_fn():
            out["reasons"].append("recovery_policy_unhealthy")
    except Exception as e:  # noqa: BLE001
        out["reasons"].append(f"recovery_policy_unreachable: {type(e).__name__}: {e}")
    out["ok"] = not out["reasons"]
    return out


def _post_signal(url: str, payload: dict):
    import requests
    return requests.post(url, json=payload, timeout=SIGNAL_TIMEOUT)


class TimedSwitch:
    """detector 자리에 들어가는 핸들. 주입 확인 시각(injector 래치)부터 switch_at_sec 뒤에
    send_signal=True(B)면 recovery-policy로 anomaly_risk 1회(detector='timed_switch', score는
    합성 자리표시값), False(A)면 계획 시각만 기록. 별도 스레드가 5초마다 표본을 남긴다.
    stop()은 예외를 던지지 않는다(정리 경로를 막지 않음)."""

    def __init__(self, *, injector, send_signal: bool, run_id: str, evidence_path: Path,
                 switch_at_sec: float = SWITCH_AT_SEC, sample_interval_sec: float = SAMPLE_INTERVAL_SEC,
                 signal_url: str = REAL_RECOVERY_POLICY_SIGNAL_URL,
                 pod_state_fn: Callable = pod_state, endpoint_ips_fn: Callable = active_endpoint_ips,
                 active_selector_fn: Callable = active_selector, post_fn: Callable = _post_signal,
                 pod_counters_fn: Callable = vllm_request_counters,
                 now_fn: Callable = lambda: datetime.now(timezone.utc)):
        self.injector, self.send_signal, self.run_id = injector, send_signal, run_id
        self.evidence_path, self.switch_at_sec, self.interval = Path(evidence_path), switch_at_sec, sample_interval_sec
        self.signal_url, self.pod_state_fn, self.endpoint_ips_fn = signal_url, pod_state_fn, endpoint_ips_fn
        self.active_selector_fn, self.post_fn, self.now = active_selector_fn, post_fn, now_fn
        self.pod_counters_fn = pod_counters_fn
        self.stop_event = threading.Event()
        self.samples = []
        self.send = {"status": "pending", "planned_send_utc": None, "attempted_utc": None,
                     "http_status": None, "response": None, "error": None}
        self._threads = []

    def start(self) -> DetectorHandle:
        # switch_at_sec=None이면 타이머 없이 표본 수집만(탐지기 전환 A/B 러너용) - 기존 호출은 그대로
        self._sampler_thread = threading.Thread(target=self._sampler, daemon=True)
        self._threads = [self._sampler_thread]
        if self.switch_at_sec is not None:
            self._threads.insert(0, threading.Thread(target=self._sender, daemon=True))
            label = f"timed_switch({'B 신호' if self.send_signal else 'A 무신호'}, +{self.switch_at_sec:.0f}s)"
        else:
            self.send["status"] = "no_timer"
            label = "sampler_only"
        for t in self._threads:
            t.start()
        return DetectorHandle(is_alive=lambda: self._sampler_thread.is_alive(), stop=self.stop, label=label)

    def _sender(self):
        t_obs = None
        while t_obs is None:  # 카운트다운은 스레드 생성이 아니라 주입 확인 래치부터
            t_obs = self.injector.get_actual_injection_time()
            if t_obs is None and self.stop_event.wait(0.5):
                self.send["status"] = "cancelled_before_injection"
                return
        planned = datetime.fromisoformat(t_obs) + timedelta(seconds=self.switch_at_sec)
        self.send["planned_send_utc"] = planned.isoformat()
        if self.stop_event.wait(max(0.0, (planned - self.now()).total_seconds())):
            self.send["status"] = "cancelled_before_send"
            return
        if not self.send_signal:
            self.send["status"] = "not_sent_control"
            return
        payload = {"signal_type": "anomaly_risk", "score": 0.0, "timestamp": self.now().isoformat(),
                   "detector": "timed_switch", "experiment_run_id": self.run_id}
        self.send["attempted_utc"] = payload["timestamp"]
        try:
            r = self.post_fn(self.signal_url, payload)
            self.send.update(status="sent", http_status=r.status_code, response=r.text[:8000])  # 결정 기록 전체(출처 판정용)
        except Exception as e:  # noqa: BLE001 - 재시도 없음, B 비교 자격에서 걸러진다
            self.send.update(status="error", error=f"{type(e).__name__}: {e}")

    def _sample_once(self, target_name: Optional[str]) -> dict:
        s = {"t": self.now().isoformat()}
        for key, fn in (("old_pod", lambda: self.pod_state_fn(target_name) if target_name else None),
                        ("endpoint_ips", self.endpoint_ips_fn), ("active_selector", self.active_selector_fn),
                        ("pod_counters", self.pod_counters_fn)):
            try:
                s[key] = fn()
            except Exception as e:  # noqa: BLE001 - API 오류는 소실 증거가 아님(판정에서 미확인 처리)
                s[key] = None
                s[f"{key}_error"] = f"{type(e).__name__}: {e}"
        return s

    def _sampler(self):
        with open(self.evidence_path, "a", encoding="utf-8") as f:
            while True:
                rec = self.injector.get_stress_delivery_record() or {}
                s = self._sample_once((rec.get("target") or {}).get("name"))
                self.samples.append(s)
                f.write(json.dumps(s, ensure_ascii=False, default=str) + "\n")
                f.flush()
                if self.stop_event.wait(self.interval):
                    return

    def stop(self) -> dict:
        self.stop_event.set()
        for t in self._threads:
            t.join(timeout=15.0)
        # 종료 대기 후에도 살아 있는 스레드는 호출부가 정리 실패로 다룰 수 있게 알린다
        alive = [t.name for t in self._threads if t.is_alive()]
        return {"send": dict(self.send), "samples": len(self.samples), "threads_alive": alive}


# ---- prepare 합성(쌍 노드 확인 + B 지연 설정) / 안전검사 / 원복 ----

def compose_prepare(injector, *, arm: str, expected_start_node: Optional[str],
                    apply_override_fn: Callable[[], dict], state: dict) -> None:
    base_prepare = injector.prepare

    def prepare():
        base_prepare()  # preview 준비 + 배치 예측/검증 + 어댑터 prepare(대상 pod 고정)
        info = getattr(injector, "get_real_promotion_diagnostic_exception_info", lambda: None)() or {}
        start = (info.get("prediction") or {}).get("active_node")
        state["start_active_node"] = start
        if start is None:
            raise TrialInvalid("시작 active 노드를 확인 못함 - 쌍 비교 불가, fail-closed")
        if expected_start_node is not None and start != expected_start_node:
            raise TrialInvalid(f"쌍 노드 불일치: A 시작={expected_start_node}, 지금={start} - B 시작 안 함")
        if arm == "switch":  # preview 준비가 끝난(abort 해제된) 뒤, 주입 전에만 바꾼다
            try:
                state["override"] = apply_override_fn()
            except sdo.DelayOverrideError as e:
                state["override_error"] = str(e)
                state["override_rollback_ok"] = e.rollback_ok
                raise TrialInvalid(f"scaleDownDelaySeconds 변경 실패 - 주입 안 함: {e}") from e

    injector.prepare = prepare


def make_safety_fn(injector, base_fn: Callable = _check_safety_with_injection) -> Callable:
    def check(baseline_state, prober):
        base_fn(baseline_state, prober)
        injector.is_done()  # 압박 스레드 예외를 관찰 중 즉시 전파(반환값은 무시 - 관찰 600초 유지)
    return check


def selector_changed_from(samples: list, exp_state: Optional[dict]) -> Optional[bool]:
    """True=관찰 중 activeSelector가 바뀜. False는 모든 표본이 selector를 읽었고 실험 상태 조회도
    성공했을 때만 - 하나라도 빠지면 '무전환 확인'이 아니라 미확인(None)."""
    selectors = [s.get("active_selector") for s in samples]
    if selectors and all(selectors) and len(set(selectors)) > 1:
        return True
    if not selectors or not all(selectors) or not exp_state or "fetch_error" in exp_state:
        return None
    return False


def _read_rollout_snapshot() -> dict:
    return sdo.snapshot(sdo.get_rollout(), {})


def finalize_override(state: dict, *, current_active_selector_fn: Callable = active_selector,
                      restore_fn: Callable = sdo.restore_override,
                      read_rollout_fn: Callable = _read_rollout_snapshot) -> dict:
    record = state.get("override")
    if record is None:
        # 변경 확인 도중 중단(KeyboardInterrupt 등)되면 메모리 기록이 없다 - 변경 전에 남긴 파일로 라이브 값을 대조
        path = state.get("override_record_path")
        if path is None or not Path(path).exists():
            return {"restored": None, "skipped": "override_not_applied"}
        try:
            record = json.loads(Path(path).read_text(encoding="utf-8"))
            cur = read_rollout_fn()
            if cur["delay_present"] == record["original_present"] and cur["delay"] == record["original_value"]:
                return {"restored": None, "skipped": "not_applied_confirmed_by_live_read"}
            if cur["delay"] != record["applied_value"]:
                return {"restored": False, "error": f"라이브 값({cur['delay']})이 원래 값도 적용값도 아님 - 수동 확인"}
            record["before"]["active_selector"]  # 원복에 필요한 필드 확인(없으면 아래 except)
        except Exception as e:  # noqa: BLE001 - 기록 손상·조회 실패는 원복 실패로 남긴다(다음 실행 차단)
            return {"restored": False, "error": f"원복 기록/현재 값 확인 실패: {type(e).__name__}: {e}"}
    old_hash = record["before"]["active_selector"]
    try:
        promoted = current_active_selector_fn() != old_hash
    except Exception as e:  # noqa: BLE001 - 모르면 승격으로 보고 구 RS 0을 기다린다(덮어쓰기보다 안전)
        promoted = True
        state["promoted_check_error"] = f"{type(e).__name__}: {e}"
    return restore_fn(record, promoted=promoted, old_pod_hash=old_hash)


# ---- 사후 실측 수집(Prometheus) ----

def _stress_chaos_exists(name: str) -> bool:
    from kubernetes import client, config
    from kubernetes.client.rest import ApiException
    config.load_kube_config()
    try:
        client.CustomObjectsApi().get_namespaced_custom_object("chaos-mesh.org", "v1alpha1", NAMESPACE,
                                                               "stresschaos", name)
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        raise


def wait_chaos_gone(cr_names: list, exists_fn: Callable = _stress_chaos_exists, timeout_sec: float = 60.0,
                    poll_sec: float = 5.0, sleep_fn=time.sleep, monotonic_fn=time.monotonic) -> list:
    """정리 후 이번 실행의 StressChaos가 실제로 사라졌는지(삭제 요청 수락 ≠ 제거). 남은 이름 목록을 반환.
    조회 오류는 예외로 전파 - 호출부가 정리 확인 실패로 기록한다."""
    deadline = monotonic_fn() + timeout_sec
    while True:
        left = [n for n in cr_names if exists_fn(n)]
        if not left or monotonic_fn() >= deadline:
            return left
        sleep_fn(poll_sec)


def _prom_range(promql: str, start: datetime, end: datetime, step: float) -> list:
    import requests
    r = requests.get(f"{LOCAL_PROMETHEUS_URL}/api/v1/query_range", timeout=15,
                     params={"query": promql, "start": start.timestamp(), "end": end.timestamp(), "step": step})
    r.raise_for_status()
    result = r.json()["data"]["result"]
    if not result:
        return []
    return [(datetime.fromtimestamp(float(t), timezone.utc).isoformat(), float(v)) for t, v in result[0]["values"]]


MAIN_PURPOSE = ("본 실행 - 이미 SLO 위반 중일 때 전환이 회복 시간을 줄이는지(A/B). 최종 반복 수는 시험 결과·일정에 "
                "따라 본 실행 전에 고정한다.")


def purpose_for(pair: str) -> str:
    return PILOT_PURPOSE if pair.startswith("pilot") else MAIN_PURPOSE


def _contract(run_id: str, arm: str, pair: str) -> dict:
    return {
        "run_id": run_id, "plan_id": PLAN_ID, "arm": arm, "pair": pair,
        "is_pilot": pair.startswith("pilot"), "purpose": purpose_for(pair),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "stages": DEMO_STAGES, "planned_stress_sec": PLANNED_STRESS_SEC,
        "stress_target": "주입 시점 active Pod(이름·UID 고정), 전환 뒤에도 계획대로 계속(continue_after_target_replacement)",
        "switch_at_sec_after_injection_observed": SWITCH_AT_SEC if arm == "switch" else None,
        "observation_sec_after_injection_observed": OBSERVATION_SEC,
        "scale_down_delay_override_sec": SCALE_DOWN_DELAY_SEC if arm == "switch" else None,
        "signal_note": "B만 recovery-policy /signal 1회(detector='timed_switch', score=0.0은 합성 자리표시값 - 탐지 결과 아님)",
        "pair_order": "쌍마다 A(무전환) -> B(정시 전환) 고정. 같은 쌍은 같은 active 노드에서 시작.",
        "order_limitation": ("A->B 순서가 고정이라 시간대·캐시·노드 상태 변화가 B에 체계적으로 섞일 수 있다 - "
                             "무작위화하지 않았다. 노드 복귀용 추가 승격은 하지 않는다."),
        "scope": "전환 자체의 효과만(전환 시각 고정). AI 탐지의 시점·정확도·불필요 신호는 이 실험으로 입증되지 않는다.",
        "primary_metric": "SLO 위반 지속시간 = slo_judge(v3, 미수정) t_recovery - t_slo, 미회복은 관찰 종료까지 하한",
        "eligibility": ("실행 완료·무효 사유 없음 + 구 Pod 실측 CPU·생존으로 압박 전달 확인(CR 기록과 별도) + "
                        "A: 승격 없음 / B: 승격 확인·지연 설정 적용 확인·새 Pod 요청 수신 확인. 하나라도 미확인이면 비교 제외."),
        "request_counter_metric": (f"{REQUEST_COUNTER_METRIC} - 각 Pod /metrics를 API 서버 pod proxy로 5초마다 직접 "
                                   f"읽음(Prometheus는 전환 뒤 구 Pod를 수집하지 않음). 사전검사에서 존재 확인."),
        "preflight": ("읽기 전용: 전 노드 최근 30분 dmesg 디스크 오류 0건·루트 rw·dmesg 창 커버(접근 실패=차단), "
                      "active Pod 요청 카운터 존재, recovery-policy /healthz. 하나라도 실패하면 시작 안 함."),
    }


def main():
    parser = argparse.ArgumentParser(description="정시 전환 회복 실험(A 무전환 / B +240초 전환)")
    parser.add_argument("--arm", required=True, choices=ARMS)
    parser.add_argument("--pair", required=True, type=_pair_arg, help="본 실행 1·2·3(= pair01·pair02·pair03, 두 표기 같은 run_id) 또는 시험 쌍 pilot01")
    parser.add_argument("--dry-run-contract-only", action="store_true", help="클러스터 미접속, 계약만 기록")
    parser.add_argument("--preflight-only", action="store_true",
                        help="읽기 전용 사전검사(디스크·요청 카운터·recovery-policy)만 하고 종료 - 클러스터 변경 없음")
    args = parser.parse_args()

    run_id = run_id_for(args.arm, args.pair)
    rep = PAIR_REPS[args.pair]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = RESULTS_DIR / f"summary-{run_id}.json"
    if summary_path.exists():
        raise SystemExit(f"이미 결과가 있음 - 덮어쓰지 않음: {summary_path}")
    check_no_unresolved_failures(RESULTS_DIR)
    expected_node = check_pair_precondition(args.arm, args.pair, RESULTS_DIR)
    contract_path = RESULTS_DIR / f"contract-{run_id}.json"
    if args.dry_run_contract_only:
        contract_path.write_text(json.dumps(_contract(run_id, args.arm, args.pair), ensure_ascii=False, indent=2,
                                            default=str), encoding="utf-8")
        print("dry-run - 클러스터 미접속, 계약만 기록")
        return

    preflight = run_preflight()
    (RESULTS_DIR / f"preflight-{run_id}.json").write_text(
        json.dumps(preflight, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if not preflight["ok"]:
        raise SystemExit(f"사전검사 실패 - 시작 안 함(클러스터 변경 없음): {preflight['reasons']}")
    if args.preflight_only:
        print(f"사전검사 통과(읽기 전용, 클러스터 변경 없음): {preflight.get('request_counter')}")
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
    compose_prepare(injector, arm=args.arm, expected_start_node=expected_node, state=state,
                    apply_override_fn=lambda: sdo.apply_override(SCALE_DOWN_DELAY_SEC, override_record_path))

    prober = make_load_ramp_prober(DEFAULT_PROBE_CONFIG, run_id, SCENARIO, ARM_LABEL, rep,
                                   BASELINE_TIMEOUT_SEC + PROBE_INJECTION_START_MARGIN_SEC + OBSERVATION_SEC)
    probe_raw_csv_path = PROBE_RESULTS_DIR / f"probe-{run_id}-{ARM_LABEL}-{rep}-raw.csv"
    evidence_path = RESULTS_DIR / f"samples-{run_id}.jsonl"
    switch = TimedSwitch(injector=injector, send_signal=(args.arm == "switch"), run_id=run_id,
                         evidence_path=evidence_path)
    events_since = datetime.now(timezone.utc).isoformat()

    # 라이브 작업 직전에 남기고, 요약을 저장한 뒤에만 지운다 - 예외로 끝나면 남아서 다음 실행을 막는다
    running_marker = RESULTS_DIR / f"running-{run_id}.json"
    running_marker.write_text(json.dumps({"run_id": run_id, "started_at": events_since}), encoding="utf-8")

    def _terminate(signum, _frame):  # SIGTERM·Ctrl+Break도 예외로 바꿔 finally 정리를 타게 한다(kill -9는 불가)
        raise KeyboardInterrupt(f"signal {signum}")
    old_handlers = {getattr(signal, n): signal.signal(getattr(signal, n), _terminate)
                    for n in ("SIGTERM", "SIGBREAK") if hasattr(signal, n)}

    outcome, exp_state, restore, cleanup_errors = None, None, None, []
    try:
        _register_experiment_context(run_id, SCENARIO, ARM_LABEL, rep, events_since)
        outcome = run_cpu_pressure_trial(run_id, injector, prober, evidence_path, probe_raw_csv_path,
                                         post_injection_observation_sec=OBSERVATION_SEC,
                                         check_safety_fn=make_safety_fn(injector),
                                         detector_start_fn=lambda _rid, _path: switch.start())
    finally:
        try:
            exp_state = _get_experiment_state(ARM_LABEL)
        except Exception as e:  # noqa: BLE001
            exp_state = {"fetch_error": f"{type(e).__name__}: {e}"}
        try:
            _clear_experiment_context(run_id, ARM_LABEL)
        except Exception as e:  # noqa: BLE001
            cleanup_errors.append(f"experiment_context_clear: {type(e).__name__}: {e}")
            print(f"[정리] experiment-run context 정리 실패: {e}")
        # 원복 함수는 호출 시점에 찾는다(모듈 속성 - 테스트에서 교체 가능)
        restore = finalize_override(state, current_active_selector_fn=active_selector,
                                    restore_fn=sdo.restore_override)
        if restore.get("restored") is False:
            cleanup_errors.append(f"scale_down_delay_restore: {restore.get('error')}")
        if state.get("override_error") and state.get("override_rollback_ok") is False:
            cleanup_errors.append(f"scale_down_delay_apply_rollback_failed: {state['override_error']}")
        if outcome is not None and outcome.get("cleanup_status") != "complete":  # probe·Chaos·preview 잔여 가능
            cleanup_errors.append(f"trial_cleanup_incomplete: prober={outcome.get('prober_stop_failed')}, "
                                  f"injector={outcome.get('injector_cleanup_failed')}")
        try:
            left = wait_chaos_gone(injector.get_stress_delivery_record().get("planned_cr_names") or [])
            if left:
                cleanup_errors.append(f"stress_chaos_residual: {left}")
        except Exception as e:  # noqa: BLE001
            cleanup_errors.append(f"stress_chaos_check_failed: {type(e).__name__}: {e}")
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)

    # ---- 실측 수집과 판정 - 여기서 오류가 나도 요약은 남긴다(비교 제외로) ----
    delivery, cpu_error, evidence_error = {}, None, None
    stress, traffic, provenance, selector_changed = {"verified": None}, None, None, None
    try:
        delivery = injector.get_stress_delivery_record()
        target = delivery.get("target") or {}
        t0 = datetime.fromisoformat(outcome["t_injection_observed_utc"]) if outcome.get("t_injection_observed_utc") else None
        cpu = []
        if t0 and target.get("name"):
            try:
                cpu = _prom_range(_cpu_promql(target["name"]), t0 - timedelta(seconds=60),
                                  t0 + timedelta(seconds=PLANNED_STRESS_SEC + 120), SAMPLE_INTERVAL_SEC)
            except Exception as e:  # noqa: BLE001 - 수집 실패는 판정 불가(None)로 남는다
                cpu_error = f"{type(e).__name__}: {e}"
        selector_changed = selector_changed_from(switch.samples, exp_state)
        t_switch = None
        if args.arm == "switch":
            provenance = verdict.evaluate_switch_provenance(send=switch.send, exp_state=exp_state, run_id=run_id)
            t_switch = provenance.get("t_switch") or (exp_state or {}).get("t_switch")
        stress = verdict.evaluate_stress_delivery(
            delivery_record=delivery, planned_stage_count=len(DEMO_STAGES), cpu_samples=cpu,
            liveness_samples=switch.samples, baseline_cpu=target.get("baseline_cpu_cores"),
            min_rise=MIN_EFFECTIVE_CPU_RISE_CORES, sample_interval_sec=SAMPLE_INTERVAL_SEC,
            planned_total_sec=PLANNED_STRESS_SEC, continuity_from=t_switch)
        if args.arm == "switch":
            new_pod = ((delivery.get("target_replacement") or {}).get("replacement_pod") or {}).get("name")
            new_ip = pod_state(new_pod).get("ip") if new_pod else None
            new_inc = old_inc = None
            if t_switch:
                lo = datetime.fromisoformat(t_switch) + timedelta(seconds=10)
                hi = lo + timedelta(seconds=TRAFFIC_WINDOW_SEC)
                edge = verdict.MAX_LIVENESS_GAP_FACTOR * SAMPLE_INTERVAL_SEC
                new_inc = verdict.counter_delta(switch.samples, new_pod, lo, hi, edge)
                old_inc = verdict.counter_delta(switch.samples, target.get("name"), lo, hi, edge)
            traffic = verdict.evaluate_new_pod_traffic(t_switch=t_switch, new_pod_ip=new_ip,
                                                       endpoint_samples=switch.samples,
                                                       new_pod_increase=new_inc, old_pod_increase=old_inc)
            traffic.update(new_pod=new_pod, new_pod_ip=new_ip)
    except Exception as e:  # noqa: BLE001
        evidence_error = f"{type(e).__name__}: {e}"
        stress = {"verified": None, "reasons": [f"evidence_error: {evidence_error}"]}

    comparison = verdict.comparison_eligibility(
        arm=args.arm, outcome_status=outcome["status"], invalidity_reasons=outcome.get("invalidity_reasons"),
        stress=stress, traffic=traffic, promotion_verified=(exp_state or {}).get("promotion_verified"),
        active_selector_changed=selector_changed,
        override_verified=(state.get("override") or {}).get("verified") if args.arm == "switch" else None,
        switch_provenance=provenance, cleanup_errors=cleanup_errors)

    pod_events_error = None  # 이벤트는 참고 자료(비교 자격 근거 아님) - 실패해도 요약은 남긴다
    try:
        events = _capture_recent_pod_events(since_utc=events_since)
        (RESULTS_DIR / f"pod-events-{run_id}.json").write_text(
            json.dumps(events, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        pod_events_error = next((e["capture_error"] for e in events if "capture_error" in e), None)
    except Exception as e:  # noqa: BLE001
        pod_events_error = f"{type(e).__name__}: {e}"
    summary = {
        "run_id": run_id, "arm": args.arm, "pair": args.pair, "is_pilot": args.pair.startswith("pilot"),
        "purpose": purpose_for(args.pair),
        "outcome_status": outcome["status"], "outcome_reason": outcome["reason"],
        "invalidity_reasons": outcome.get("invalidity_reasons"), "cleanup_status": outcome.get("cleanup_status"),
        "t_injection_requested_utc": outcome.get("t_injection_requested_utc"),
        "t_injection_observed_utc": outcome.get("t_injection_observed_utc"),
        "observation_ended_at_utc": outcome.get("observation_ended_at_utc"),
        "probe_raw_csv": str(probe_raw_csv_path), "start_active_node": state.get("start_active_node"),
        "timed_switch": outcome.get("score_server_exit"), "experiment_run_audit_timing": exp_state,
        "stress_delivery": stress, "stress_delivery_record": delivery, "cpu_collection_error": cpu_error,
        "evidence_error": evidence_error, "preflight": preflight, "pod_events_error": pod_events_error,
        "active_selector_changed": selector_changed, "new_pod_traffic": traffic,
        "timed_switch_provenance": provenance, "cleanup_errors": cleanup_errors,
        "scale_down_delay_override": state.get("override"), "scale_down_delay_override_error": state.get("override_error"),
        "scale_down_delay_restore": restore, "comparison": comparison,
        "slo_note": "t_slo/t_recovery는 probe 원자료에 slo_judge(v3, 미수정)를 사후 적용해 계산한다(여기서 계산하지 않음).",
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    running_marker.unlink()  # 정리 실패는 summary.cleanup_errors가 계속 다음 실행을 막는다
    print(f"결과: {outcome['status']} / 비교 자격={comparison['eligible']} {comparison['reasons']} / {summary_path}")
    if cleanup_errors:
        print(f"[중단] 정리·원복 실패 - 다음 실행은 자동으로 거부됨, 수동 확인 필요: {cleanup_errors}")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
