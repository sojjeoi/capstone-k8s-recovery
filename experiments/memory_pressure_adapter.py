#!/usr/bin/env python3
"""memory_pressure 시나리오의 Injector 어댑터(run_once.py 계약 구현).
network_degrade_adapter.py와 같은 lifecycle 패턴을 따른다 - active pod을
동적으로 고정하고, 단계마다 StressChaos CR을 어댑터가 직접 순차 생성/삭제한다
(Chaos Mesh Workflow 대신 - 이유는 network_degrade_adapter.py 모듈 docstring과
동일: 검증 가능한 단일 CR의 status.conditions만 신뢰한다). `chaos/scenario-
progressive-memory-pressure.yaml`(Workflow, 5000MB 마지막 단계 포함)은 이력으로
그대로 두고 이 어댑터가 대체한다 - 새 강도 후보는 `chaos/scenario-memory-
pressure-explore.yaml` 참고(2500MB·5000MB 제외, 근거는 phase5-memory-pressure-
investigation.md §2 + phase8-blue-green-preflight-incident.md §48.3).

**pod_kill/network_degrade와 다른 점 - 안전 감시가 훨씬 무겁다.** 메모리
압박은 노드 전체의 가용 메모리를 실제로 소모하고(Phase 5 §2 - 커널 cgroup
OOM 메커니즘), 임계치를 넘기면 대상 자신이 아니라 다른 워크로드나 노드
자체가 위험해질 수 있다. 그래서 이 어댑터는 각 stage 생성 전 + 대기 중
주기적으로(SAFETY_POLL_INTERVAL_SEC마다) Node MemAvailable·target working
set·restartCount·OOMKilled·Node conditions를 직접 조회해 즉시 중단 조건을
검사한다 - network_degrade_adapter.py의 `_check_target()`(대상 교체 재확인)
보다 훨씬 자주, 훨씬 많은 지표를 본다.

**duration 안전망**: 매 StressChaos CR 생성 시 `spec.duration`을 stage
지속시간 + 여유(STAGE_DURATION_SAFETY_MARGIN_SEC)로 명시한다 - 하니스
프로세스 자체가 죽어도 Chaos Mesh가 그 시간 뒤 스스로 회수하게 하는 안전망
(calibrate_network_tolerant_probe.py가 NetworkChaos에 쓰는 것과 같은 기법).

**안전 관측값(evidence)은 TrialResult 스키마에 없다** - `results/memory-
pressure-safety-{run_id}.jsonl`에 매 안전 확인 tick과 주요 이벤트를 JSON
라인으로 남긴다(지시: "새 TrialResult 필드는 만들지 말고 안전 관측값은
evidence와 notes에 남기세요"). 이 로그 기록 자체가 실패해도(디스크 문제 등)
trial 판정에 영향을 주지 않는다(읽기 전용 관측이 판정 경로를 오염시키면
안 된다는 원칙 - trial_observer.py와 동일).

target replacement(효과가 난 뒤 대상이 바뀜)는 network_degrade_adapter.py와
완전히 같은 규칙을 쓴다 - 효과를 내기 전 교체는 TrialInvalid(외부 오염
가능성), 효과를 낸 뒤 교체는 기록만 하고 injector는 "주입 끝남"으로 본다.
"""
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import requests
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from active_pod_resolver import NAMESPACE, get_active_pods, load_kube_config
from run_once import HarnessCorrupted, Injector, TrialInvalid

CHAOS_GROUP = "chaos-mesh.org"
CHAOS_VERSION = "v1alpha1"
CHAOS_PLURAL = "stresschaos"

RESULTS_DIR = Path(__file__).parent / "results"  # run_once.py의 RESULTS_DIR와 동일 디렉터리 관례
LOCAL_PROMETHEUS_URL = "http://localhost:9090"  # arm_controller.py의 LOCAL_PROMETHEUS_URL과 동일 전제(로컬 port-forward)
PROM_QUERY_TIMEOUT_SEC = 10.0
# §96 - anomaly-detection/v3/prom_health.py의 DEFAULT_FRESHNESS_MAX_AGE_SEC/
# arm_controller.py의 PROMETHEUS_FRESHNESS_MAX_AGE_SEC과 동일한 값(120초) -
# 이 파일은 그 두 모듈을 import하지 않고(기존 관례 - arm_controller.py도
# prom_health.py를 안 쓰고 독립 구현을 둠, 무거운 의존성을 지역화하기 위함)
# 같은 상수만 재사용한다.
FRESHNESS_MAX_AGE_SEC = 120.0

GIB = 1024 ** 3
MIB = 1024 ** 2

# 즉시 중단 조건(지시 원문) - 이 두 상수가 안전 감시의 핵심 임계치다.
MIN_NODE_AVAILABLE_BYTES = 3 * GIB
MAX_TARGET_WORKING_SET_BYTES = 5 * GIB
# §96 - non-native arm(preview 공존) 전용 사전 headroom 게이트의 PASS 바.
# explore_memory_pressure_intensity.py의 MIN_NODE_AVAILABLE_PASS_BYTES와
# 같은 값(4GiB) - 그 모듈을 import하지 않고(이 파일이 더 하위 계층이라
# 역방향 의존을 피함) 값만 재사용한다.
MIN_NODE_AVAILABLE_WITH_PROJECTED_STRESS_BYTES = 4 * GIB

SAFETY_POLL_INTERVAL_SEC = 5.0  # 주입 중 안전 지표 확인 주기
STAGE_DURATION_SAFETY_MARGIN_SEC = 60.0  # 각 CR의 duration 안전망 = stage 지속시간 + 이 값
EFFECTIVE_WORKING_SET_RISE_BYTES = 50 * MIB  # is_effective() 판정 - 최소 이만큼은 올라야 "효과 확인"
CLEANUP_JOIN_TIMEOUT_SEC = 30
STAGE_RECOVERY_TIMEOUT_SEC = 30  # 삭제 요청 후 실제 소멸(recover) 확인 대기 상한
CLEANUP_VERIFY_TIMEOUT_SEC = 30
CLEANUP_RECOVERY_TIMEOUT_SEC = 30.0  # cleanup 후 working set이 baseline 근처로 돌아오는지 확인(비차단 관측)
CLEANUP_RECOVERY_TOLERANCE_BYTES = 150 * MIB  # baseline ±150MiB


def _node_healthy(conditions: dict) -> bool:
    """explore_ramp_intensity.py의 check_node_and_pods()와 동일한 4-condition 판정."""
    return (conditions.get("Ready") == "True"
            and conditions.get("MemoryPressure") == "False"
            and conditions.get("DiskPressure") == "False"
            and conditions.get("PIDPressure") == "False")


_QUANTITY_SUFFIXES = {"Ki": 2 ** 10, "Mi": 2 ** 20, "Gi": 2 ** 30, "Ti": 2 ** 40}


def _parse_k8s_quantity_bytes(raw: str) -> Optional[float]:
    """"6Gi" 같은 K8s 리소스 quantity 문자열을 바이트로 변환한다. 파싱 실패는
    None(호출자는 컨테이너 limit을 모르는 것으로 취급하고 MAX_TARGET_WORKING_
    SET_BYTES 안전 상한만으로 계속 진행한다 - 이 값이 항상 지켜야 할 하한이라
    파싱 실패로 안전성이 낮아지지 않는다)."""
    raw = raw.strip()
    for suffix, mult in _QUANTITY_SUFFIXES.items():
        if raw.endswith(suffix):
            try:
                return float(raw[:-len(suffix)]) * mult
            except ValueError:
                return None
    try:
        return float(raw)  # 접미사 없으면 바이트 그대로(K8s 관례)
    except ValueError:
        return None


def get_pod_details(name: str) -> Optional[dict]:
    """이름으로 pod 상세(노드 이름, vllm 컨테이너 memory limit, restartCount,
    OOMKilled 여부)를 조회한다 - active_pod_resolver.get_pod()보다 필드이
    많아 이 어댑터 전용으로 둔다(다른 어댑터는 이 정보가 필요 없음). 없으면
    (404) None."""
    load_kube_config()
    core = client.CoreV1Api()
    try:
        p = core.read_namespaced_pod(name, NAMESPACE)
    except ApiException as e:
        if e.status == 404:
            return None
        raise
    container = next((c for c in (p.spec.containers or []) if c.name == "vllm"), None)
    memory_limit_bytes = None
    if container is not None and container.resources and container.resources.limits:
        raw = container.resources.limits.get("memory")
        if raw is not None:
            memory_limit_bytes = _parse_k8s_quantity_bytes(raw)
    statuses = p.status.container_statuses or []
    cs = next((c for c in statuses if c.name == "vllm"), None)
    restart_count = cs.restart_count if cs is not None else None
    oom_killed = False
    if cs is not None:
        for state in (cs.last_state, cs.state):
            if state is not None and state.terminated is not None and state.terminated.reason == "OOMKilled":
                oom_killed = True
    return {
        "name": p.metadata.name, "uid": p.metadata.uid, "node_name": p.spec.node_name,
        "memory_limit_bytes": memory_limit_bytes, "restart_count": restart_count, "oom_killed": oom_killed,
    }


def get_node_ip(node_name: str) -> Optional[str]:
    load_kube_config()
    core = client.CoreV1Api()
    node = core.read_node(node_name)
    for addr in node.status.addresses or []:
        if addr.type == "InternalIP":
            return addr.address
    return None


def get_node_conditions(node_name: str) -> dict:
    load_kube_config()
    core = client.CoreV1Api()
    node = core.read_node(node_name)
    return {c.type: c.status for c in (node.status.conditions or [])}


def _prom_instant_query(promql: str, prom_url: str) -> Optional[float]:
    """단일 스칼라 값을 기대하는 Prometheus 인스턴트 쿼리 - 실패(접근 불가,
    표본 없음, 파싱 실패, §96부터는 **오래된 표본**도 포함)는 전부 None.
    호출자가 None을 '조회 실패'로 취급해 fail-closed해야 한다(안전 지표를
    못 읽었다고 계속 진행하면 안 됨).

    §96 - 이전에는 응답이 성공(200, status=success, 표본 있음)이기만 하면
    표본 시각(freshness)을 전혀 확인하지 않았다 - Prometheus가 살아있어도
    특정 스크레이프 타겟이 멈춰서 오래된 값만 계속 나오는 경우(prom_health.
    check_metric_freshness()가 score_server.py 쪽에서 이미 방어하는 것과
    같은 실패 모드)를 이 안전 감시 경로는 못 잡았다. 표본이 FRESHNESS_MAX_
    AGE_SEC보다 오래됐으면 그 값을 신뢰하지 않고 None으로 취급한다(판정
    로직 자체는 변경 없음 - 기존 호출부는 이미 None을 fail-closed로
    처리하고 있었으므로 이 tightening은 그 경로를 그대로 탄다)."""
    try:
        r = requests.get(f"{prom_url}/api/v1/query", params={"query": promql}, timeout=PROM_QUERY_TIMEOUT_SEC)
        if r.status_code != 200:
            return None
        body = r.json()
        if body.get("status") != "success":
            return None
        result = body.get("data", {}).get("result", [])
        if not result:
            return None
        sample_ts, sample_value = result[0]["value"]
        if (time.time() - float(sample_ts)) > FRESHNESS_MAX_AGE_SEC:
            return None
        return float(sample_value)
    except (requests.exceptions.RequestException, KeyError, IndexError, ValueError, TypeError):
        return None


def get_node_available_bytes(node_ip: str, prom_url: str = LOCAL_PROMETHEUS_URL) -> Optional[float]:
    """node-exporter의 node_memory_MemAvailable_bytes - instance 라벨이
    "{node_ip}:9100" 형태(kube-prometheus-stack 기본 배선)라 node 이름이
    아니라 IP로 조인해야 한다."""
    return _prom_instant_query(f'node_memory_MemAvailable_bytes{{instance="{node_ip}:9100"}}', prom_url)


def get_pod_working_set_bytes(pod_name: str, prom_url: str = LOCAL_PROMETHEUS_URL) -> Optional[float]:
    return _prom_instant_query(
        f'container_memory_working_set_bytes{{namespace="{NAMESPACE}",pod="{pod_name}",container="vllm"}}',
        prom_url)


def check_prometheus_health_for_injection(node_ip: str, pod_name: str,
                                           prom_url: str = LOCAL_PROMETHEUS_URL) -> dict:
    """§96 - injection 시작 "직전"에 부르는 명시적 preflight(지시: "실행 전
    port-forward health fail-closed", "session 시작 전 최신 metric 조회").
    안전 감시가 실제로 읽을 두 지표(Node MemAvailable·target pod working
    set)를 지금 이 순간 조회해 둘 다 fresh하게 나오는지 확인한다 - 새
    Prometheus 호출 경로를 만들지 않고 get_node_available_bytes()/
    get_pod_working_set_bytes()(이미 §96에서 staleness 검사가 들어감)를
    그대로 재사용한다. 실패하면 호출자가 injection을 아예 시작하지 않고
    fail-closed(TrialInvalid/invalid_run)로 처리해야 한다."""
    node_available = get_node_available_bytes(node_ip, prom_url)
    working_set = get_pod_working_set_bytes(pod_name, prom_url)
    reasons = []
    if node_available is None:
        reasons.append("Node MemAvailable 조회 실패 또는 표본 오래됨(port-forward 또는 스크레이프 문제)")
    if working_set is None:
        reasons.append("target pod working set 조회 실패 또는 표본 오래됨(port-forward 또는 스크레이프 문제)")
    return {"healthy": len(reasons) == 0, "reasons": reasons,
            "node_available_bytes": node_available, "working_set_bytes": working_set}


def create_memory_stress_chaos(cr_name: str, run_id: str, arm: str, target_pod_name: str, stage: dict,
                                duration: Optional[str] = None) -> None:
    """duration을 주면 Chaos Mesh가 그 시간 뒤 스스로 복구한다 - 하니스가
    죽어도 압박이 무한정 남지 않게 하는 안전망(이 어댑터는 항상 명시 전달,
    calibrate_network_tolerant_probe.py가 NetworkChaos에 쓰는 것과 동일 패턴).
    selector는 network_degrade_adapter.py와 동일하게 selector.pods로 특정
    pod에 고정한다(labelSelectors만 쓰면 preview가 같이 떠있을 때 모호해짐)."""
    load_kube_config()
    body = {
        "apiVersion": f"{CHAOS_GROUP}/{CHAOS_VERSION}",
        "kind": "StressChaos",
        "metadata": {
            "name": cr_name,
            "namespace": NAMESPACE,
            "labels": {"experiment-run-id": run_id, "phase8-arm": arm},
        },
        "spec": {
            "mode": "one",
            "selector": {
                "namespaces": [NAMESPACE],
                "pods": {NAMESPACE: [target_pod_name]},
            },
            "stressors": {
                "memory": {"workers": stage["workers"], "size": f"{stage['size_mb']}MB"},
            },
        },
    }
    if duration is not None:
        body["spec"]["duration"] = duration
    client.CustomObjectsApi().create_namespaced_custom_object(
        CHAOS_GROUP, CHAOS_VERSION, NAMESPACE, CHAOS_PLURAL, body)


def delete_memory_stress_chaos(cr_name: str) -> None:
    """idempotent - CR이 없으면(404) 조용히 넘어간다(pod_kill_adapter.py의
    delete_pod_chaos와 동일 패턴)."""
    load_kube_config()
    try:
        client.CustomObjectsApi().delete_namespaced_custom_object(
            CHAOS_GROUP, CHAOS_VERSION, NAMESPACE, CHAOS_PLURAL, cr_name)
    except ApiException as e:
        if e.status != 404:
            raise


def is_stage_injected(cr_name: str) -> bool:
    """status.conditions에서 type=AllInjected, status="True"를 찾는다 -
    Chaos Mesh 공식 문서 기반(network_degrade_adapter.py와 동일). 방치된
    StressChaos CR 실측 덤프(phase8-blue-green-preflight-incident.md §9.3)로
    이 필드 경로가 StressChaos에도 그대로 존재함을 이미 확인했다."""
    load_kube_config()
    try:
        obj = client.CustomObjectsApi().get_namespaced_custom_object(
            CHAOS_GROUP, CHAOS_VERSION, NAMESPACE, CHAOS_PLURAL, cr_name)
    except ApiException as e:
        if e.status == 404:
            return False
        raise
    conditions = (obj.get("status") or {}).get("conditions") or []
    return any(c.get("type") == "AllInjected" and c.get("status") == "True" for c in conditions)


def does_chaos_exist(cr_name: str) -> bool:
    load_kube_config()
    try:
        client.CustomObjectsApi().get_namespaced_custom_object(
            CHAOS_GROUP, CHAOS_VERSION, NAMESPACE, CHAOS_PLURAL, cr_name)
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        raise


def _sanitize_cr_name(run_id: str, stage_index: int) -> str:
    import uuid
    safe = run_id.lower().replace("_", "-").replace(":", "-")
    return f"memstress-{safe}-s{stage_index}-{uuid.uuid4().hex[:6]}"[:253]


def _classify_timestamp_against_windows(timestamp_iso, windows) -> str:
    """network_degrade_adapter.py의 동명 함수와 완전히 같은 순수 로직(의도적
    복제 - 두 어댑터 모두 자기 완결적이라는 이 저장소의 기존 관례를 따름,
    pod_kill_adapter.py/network_degrade_adapter.py도 서로 UID 추적 로직을
    공유하지 않는다). 근거 없으면 추정하지 않고 "unknown"."""
    try:
        t = datetime.fromisoformat(timestamp_iso)
        if not windows:
            return "unknown"
        for w in windows:
            if w["start"] <= t and (w["end"] is None or t <= w["end"]):
                return w["name"]
        if t < windows[0]["start"]:
            return "baseline"
        for a, b in zip(windows, windows[1:]):
            if a["end"] is not None and a["end"] < t < b["start"]:
                return "inter_stage_tail"
        if windows[-1]["end"] is not None and t > windows[-1]["end"]:
            return "drain"
    except Exception:
        return "unknown"
    return "unknown"


def _safety_log_path(run_id: str) -> Path:
    # run_once.py._write_result()와 같은 관례 - run_id의 "pilot-" 접두어(모든
    # 러너가 --pilot일 때 붙이는 것)로 pilot 여부를 판단해 results/pilot/
    # 아래 별도 경로에 쓴다. 이 어댑터는 is_pilot을 별도 인자로 받지 않으므로
    # (생성자 시그니처를 늘리지 않기 위해) run_id 문자열 관례를 그대로 재사용한다.
    base = (RESULTS_DIR / "pilot") if run_id.startswith("pilot-") else RESULTS_DIR
    return base / f"memory-pressure-safety-{run_id}.jsonl"


def _append_safety_log(run_id: str, record: dict) -> None:
    """안전 관측값(evidence)을 JSONL로 남긴다 - TrialResult 스키마에는 없는
    보조 기록. record는 _log()가 이미 "ts"를 찍어서 넘긴다(2026-09-20부터).
    기록 실패(디스크 문제 등)는 trial 판정에 영향을 주면 안 되므로 예외를
    삼키고 stderr에만 남긴다(읽기 전용 관측이 판정 경로를 오염시키지 않는다는
    원칙, trial_observer.py와 동일)."""
    try:
        path = _safety_log_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except OSError as e:
        print(f"[memory_pressure_adapter] 안전 로그 기록 실패(무시하고 계속): {e}", file=sys.stderr)


def make_memory_pressure_injector(
    run_id: str, arm: str, rep: int,
    stages: list,
    get_active_pods_fn: Callable[[], list] = get_active_pods,
    get_pod_details_fn: Callable[[str], Optional[dict]] = get_pod_details,
    get_node_ip_fn: Callable[[str], Optional[str]] = get_node_ip,
    get_node_conditions_fn: Callable[[str], dict] = get_node_conditions,
    get_node_available_fn: Callable[[str], Optional[float]] = get_node_available_bytes,
    get_working_set_fn: Callable[[str], Optional[float]] = get_pod_working_set_bytes,
    is_stage_injected_fn: Callable[[str], bool] = is_stage_injected,
    does_chaos_exist_fn: Callable[[str], bool] = does_chaos_exist,
    create_chaos_fn: Callable = create_memory_stress_chaos,
    delete_chaos_fn: Callable[[str], None] = delete_memory_stress_chaos,
    min_node_available_bytes: float = MIN_NODE_AVAILABLE_BYTES,
    max_target_working_set_bytes: float = MAX_TARGET_WORKING_SET_BYTES,
    safety_poll_interval_sec: float = SAFETY_POLL_INTERVAL_SEC,
    stage_duration_safety_margin_sec: float = STAGE_DURATION_SAFETY_MARGIN_SEC,
    effective_rise_bytes: float = EFFECTIVE_WORKING_SET_RISE_BYTES,
    stage_delete_poll_interval_sec: float = 1.0,
    stage_recovery_timeout_sec: float = STAGE_RECOVERY_TIMEOUT_SEC,
    cleanup_verify_timeout_sec: float = CLEANUP_VERIFY_TIMEOUT_SEC,
    cleanup_recovery_timeout_sec: float = CLEANUP_RECOVERY_TIMEOUT_SEC,
    cleanup_recovery_tolerance_bytes: float = CLEANUP_RECOVERY_TOLERANCE_BYTES,
    log_fn: Callable[[dict], None] = None,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> Injector:
    """*_fn 파라미터는 pod_kill_adapter.py/network_degrade_adapter.py와 같은
    이유의 오프라인 테스트용 의존성 주입 지점.

    `stages`는 **필수 인자다(기본값 없음)** - 계약서·incident 문서가 아직
    강도를 확정하지 않았고(§48.3, `chaos/scenario-memory-pressure-explore.yaml`
    참고) 1GB 이상 탐색·5단계 전체 ramp가 이번 지시 범위 밖이므로, 호출자가
    실행할 단계를 매번 명시하지 않으면 아무 것도 실행되지 않게 한다(우발적
    기본 실행 방지 - fail-closed와 같은 정신).

    log_fn은 안전 로그 기록 대상을 테스트에서 가로챌 수 있게 하는 선택
    인자(기본값은 실제 파일에 append하는 _append_safety_log(run_id, ...))."""
    if not stages:
        raise ValueError("stages는 비어있지 않은 리스트여야 함(기본값 없음 - 명시적으로 전달할 것)")

    def _log(record: dict) -> None:
        """모든 안전 로그 기록에 시각(ts)을 여기서 한 번만 찍는다(2026-09-20
        추가 - 후보 재현성 검증 도구 지원). 이전에는 log_fn 경로(테스트·
        explore/verify 도구)로 가는 기록에는 ts가 안 붙고 파일 기록
        경로(_append_safety_log)에만 붙어 비대칭이었다 - 여러 stage에 걸친
        기록을 시각으로 구간 분류하려면 log_fn 쪽도 ts가 있어야 한다. 실제
        벽시계(now_fn 아님)를 쓴다 - now_fn은 stage 경계 기록 전용 주입
        지점(_StepClock 등 결정론적 테스트 시계)이라, 여기서 같이 소비하면
        매 안전 tick마다 그 시계가 추가로 진행해 stage 경계 타이밍이
        틀어진다(실측 확인됨 - classify_stage 테스트가 깨짐)."""
        stamped = {"ts": datetime.now(timezone.utc).isoformat(), **record}
        if log_fn is not None:
            log_fn(stamped)
        else:
            _append_safety_log(run_id, stamped)

    cr_names = [_sanitize_cr_name(run_id, i) for i in range(len(stages))]
    target = {"name": None, "uid": None, "node_name": None, "node_ip": None, "memory_limit_bytes": None}
    baseline_restart_count = {"v": None}
    baseline_working_set = {"v": None}
    injection_started_at = {"t": None}
    last_seen_not_injected_at = {"t": None}
    current_stage_index = {"i": None}
    all_stages_done = {"v": False}
    stop_event = threading.Event()
    thread_ref = {"t": None}
    thread_exception = {"e": None}
    target_replacement = {"v": None}
    stage_windows = []  # classify_stage()가 읽는 실제 stage 창(스레드만 append/갱신)

    def _check_target():
        """단계 전환 직전 active pod 재확인(network_degrade_adapter.py의
        _check_target()과 완전히 같은 규칙) - 효과를 내기 전 교체는 외부
        오염 가능성으로 TrialInvalid, 효과를 낸 뒤 교체는 정상 실험 결과로
        기록만 한다(연쇄장애 자체가 관찰 대상일 수 있음)."""
        pods = get_active_pods_fn()
        if len(pods) == 1 and pods[0]["uid"] == target["uid"]:
            return
        if injection_started_at["t"] is None:
            raise TrialInvalid(
                f"주입이 효과를 내기 전에 active pod 구성이 바뀜(원래 uid="
                f"{target['uid']}, 지금 {len(pods)}개: {[p['uid'] for p in pods]}) - "
                f"외부 오염 가능성, invalid_run 처리")
        if target_replacement["v"] is None:
            replacement_pod = pods[0] if len(pods) == 1 else None
            target_replacement["v"] = {
                "replaced_at": datetime.now(timezone.utc).isoformat(),
                "pod": replacement_pod,
            }

    def prepare():
        pods = get_active_pods_fn()
        if len(pods) == 0:
            raise TrialInvalid("active selector에 매칭되는 pod가 없음(fail-closed)")
        if len(pods) > 1:
            raise TrialInvalid(
                f"active selector에 pod가 {len(pods)}개 매칭됨(1개여야 함) - fail-closed: "
                f"{[p['name'] for p in pods]}")
        target["name"] = pods[0]["name"]
        target["uid"] = pods[0]["uid"]

        details = get_pod_details_fn(target["name"])
        if details is None:
            raise TrialInvalid("prepare 직후 대상 pod 상세 조회 실패(사라짐?) - fail-closed")
        target["node_name"] = details["node_name"]
        target["memory_limit_bytes"] = details["memory_limit_bytes"]
        baseline_restart_count["v"] = details["restart_count"]

        node_ip = get_node_ip_fn(target["node_name"])
        if node_ip is None:
            raise TrialInvalid(f"Node({target['node_name']}) InternalIP 조회 실패 - fail-closed")
        target["node_ip"] = node_ip

        conditions = get_node_conditions_fn(target["node_name"])
        if not _node_healthy(conditions):
            raise TrialInvalid(f"주입 전 Node({target['node_name']}) 상태 이상(fail-closed): {conditions}")

        available = get_node_available_fn(node_ip)
        if available is None:
            raise TrialInvalid("주입 전 Node MemAvailable 조회 실패 - 안전 지표 확인 불가로 fail-closed")
        if available < min_node_available_bytes:
            raise TrialInvalid(
                f"주입 전 Node MemAvailable 부족: {available / GIB:.2f}GiB < "
                f"{min_node_available_bytes / GIB:.2f}GiB - headroom 부족(fail-closed)")

        # §96 - non-native arm preview headroom 게이트(지시): preview가 이미
        #떠 있는 상태(arm_controller.wrap_injector_with_preview_prep()이 이
        # prepare() 호출 "전"에 미리 준비해둠)에서 Node MemAvailable을 다시
        # 재는 것이므로, 이 `available` 값 자체가 이미 preview pod의 현재
        # 메모리 사용량을 반영한 노드 레벨 지표다(별도로 preview working
        # set을 따로 조회할 필요 없음 - MemAvailable은 노드 전체 값이라
        # 자동으로 포함됨). 예상 stress 증분을 뺀 뒤에도 4GiB 이상이어야
        # 한다: `pre_injection_memavailable - expected_stress_delta >= 4GiB`.
        # native는 이 게이트를 걸어도 무해하다(§95 실측 - MemAvailable
        # 6.29~7.22GiB에서 953MiB를 빼도 4GiB를 한참 넘음) - arm 조건 분기
        # 없이 항상 확인한다.
        expected_stress_delta = max(s["size_mb"] for s in stages) * MIB
        projected_available = available - expected_stress_delta
        if projected_available < MIN_NODE_AVAILABLE_WITH_PROJECTED_STRESS_BYTES:
            raise TrialInvalid(
                f"insufficient_headroom_with_preview: Node MemAvailable {available / GIB:.2f}GiB - "
                f"예상 stress 증분 {expected_stress_delta / GIB:.2f}GiB = {projected_available / GIB:.2f}GiB, "
                f"{MIN_NODE_AVAILABLE_WITH_PROJECTED_STRESS_BYTES / GIB:.2f}GiB 미만(fail-closed, "
                f"preview 공존 상태의 headroom 부족)")

        baseline_ws = get_working_set_fn(target["name"])
        if baseline_ws is None:
            raise TrialInvalid("주입 전 target working set 조회 실패 - 안전 지표 확인 불가로 fail-closed")
        baseline_working_set["v"] = baseline_ws

        ceiling = max_target_working_set_bytes
        if target["memory_limit_bytes"] is not None:
            ceiling = min(ceiling, target["memory_limit_bytes"])
        max_stage_bytes = max(s["size_mb"] for s in stages) * MIB
        projected = baseline_ws + max_stage_bytes
        if projected > ceiling:
            raise TrialInvalid(
                f"headroom 부족(fail-closed) - baseline {baseline_ws / GIB:.2f}GiB + 최대 stage "
                f"{max_stage_bytes / GIB:.2f}GiB 투영치 {projected / GIB:.2f}GiB가 안전 상한 "
                f"{ceiling / GIB:.2f}GiB 초과(컨테이너 limit={target['memory_limit_bytes']}, "
                f"MAX_TARGET_WORKING_SET_BYTES=5GiB 중 더 작은 값)")

        _log({
            "event": "prepare_ok", "run_id": run_id, "arm": arm, "rep": rep,
            "target_pod": target["name"], "target_uid": target["uid"], "node_name": target["node_name"],
            "memory_limit_bytes": target["memory_limit_bytes"],
            "baseline_restart_count": baseline_restart_count["v"],
            "baseline_working_set_bytes": baseline_ws, "node_available_bytes": available,
        })

    def _raise_for_violation(violation: dict):
        msg = f"[{violation['reason']}] {violation['detail']}"
        if violation["severity"] == "harness_corrupted":
            raise HarnessCorrupted(msg)
        raise TrialInvalid(msg)

    def _check_safety_once() -> Optional[dict]:
        """즉시 중단 조건 전부를 한 번 확인한다 - 안전 관측값은 위반 여부와
        무관하게 매번 evidence 로그에 남긴다(지시: "observer는 읽기 전용이며
        항상 종료 확인" - 여기서는 "항상 관측 기록")."""
        available = get_node_available_fn(target["node_ip"])
        ws = get_working_set_fn(target["name"])
        details = get_pod_details_fn(target["name"])
        conditions = get_node_conditions_fn(target["node_name"])
        _log({
            "event": "safety_tick", "run_id": run_id, "node_available_bytes": available,
            "working_set_bytes": ws,
            "restart_count": details.get("restart_count") if details else None,
            "oom_killed": details.get("oom_killed") if details else None,
            "node_conditions": conditions,
        })

        if available is None:
            return {"reason": "node_memavailable_unreadable", "severity": "invalid",
                    "detail": "Node MemAvailable 조회 실패 - 안전 지표 확인 불가로 fail-closed"}
        if available < min_node_available_bytes:
            return {"reason": "node_memavailable_low", "severity": "invalid",
                    "detail": f"Node MemAvailable {available / GIB:.2f}GiB < "
                              f"{min_node_available_bytes / GIB:.2f}GiB - 즉시 중단"}
        if not _node_healthy(conditions):
            return {"reason": "node_unhealthy", "severity": "harness_corrupted",
                    "detail": f"Node({target['node_name']}) NotReady 또는 pressure 발생: {conditions}"}
        if ws is None:
            return {"reason": "working_set_unreadable", "severity": "invalid",
                    "detail": "target working set 조회 실패 - 안전 지표 확인 불가로 fail-closed"}
        if ws > max_target_working_set_bytes:
            return {"reason": "working_set_high", "severity": "invalid",
                    "detail": f"target working set {ws / GIB:.2f}GiB > "
                              f"{max_target_working_set_bytes / GIB:.2f}GiB - 즉시 중단"}
        if details is None:
            return {"reason": "target_unreadable", "severity": "invalid",
                    "detail": "target pod 상세 조회 실패(사라짐?) - fail-closed"}
        restart_increased = (details["restart_count"] is not None and baseline_restart_count["v"] is not None
                              and details["restart_count"] > baseline_restart_count["v"])
        if details["oom_killed"] or restart_increased:
            if details["oom_killed"]:
                return {"reason": "oom_killed", "severity": "invalid",
                        "detail": f"target OOMKilled(restartCount "
                                  f"{baseline_restart_count['v']}->{details['restart_count']})"}
            return {"reason": "restart_increased", "severity": "invalid",
                    "detail": f"restartCount 증가 감지({baseline_restart_count['v']}->"
                              f"{details['restart_count']}, 원인=기타(liveness 등으로 추정, OOMKilled 아님)"}
        return None

    def _stage_wait_with_safety(stage_duration_sec: float, stop_reason: dict,
                                 window: Optional[dict] = None, stage_idx: Optional[int] = None) -> bool:
        """stage_duration_sec 동안 안전 감시를 하며 대기한다. cleanup()이
        stop_event를 세우면 stop_reason을 건드리지 않고 즉시 True(정상
        중단)를 반환한다. 안전 위반이 감지되면 stop_reason["v"]를 채우고
        True를 반환한다. 정상 만료면 False.

        window/stage_idx(선택, 2026-09-20 추가 - 후보 재현성 검증 도구 지원):
        주어지면 이 stage의 AllInjected를 이미 도는 안전 tick 주기에 얹어서
        확인만 하고 window["all_injected"]에 기록한다(판정에 관여하지 않는
        순수 관측 - 기존 안전/중단 로직은 전혀 안 바뀐다, 추가 대기시간도
        없다). 못 확인해도 stage 진행 자체는 막지 않는다."""
        deadline = time.monotonic() + stage_duration_sec
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if stop_event.wait(min(safety_poll_interval_sec, remaining)):
                return True  # cleanup()이 요청한 정상 중단
            # §96 - stage 진행 중(단일 장시간 stage일수록 중요, 예: negative-
            # control의 120초 stage)에도 target replacement를 매 tick 확인한다.
            # 기존에는 _run_stages()가 stage 시작 "직전"에만 _check_target()을
            # 불렀고, 이 대기 루프 안에서는 안 불렀다 - fixed_threshold/proposed
            # 에서 detector가 stage 도중에 실제로 promotion을 일으키면(negative
            # control에서 관찰 대상인 unnecessary_action 그 자체), 원래
            # target(교체 전 active pod)이 정상적으로 scale-down되어 사라지고,
            # 그 다음 tick의 _check_safety_once()가 `get_pod_details_fn(target
            # ["name"])`의 404를 "target_unreadable"(안전 위반)로 오판해
            # 정상적인 promotion 결과를 harness 실패로 잘못 분류했을 것이다
            # (§96 forensic, 라이브 실행 전 정적 감사로 발견 - 아직 실제로
            # 겪은 사고는 아님). _check_target()은 이미 "효과가 난 뒤의 교체는
            # 기록만 하고 정상 종료로 본다"는 규칙을 갖고 있으므로(그대로
            # 재사용, 새 로직 없음) 여기서 먼저 확인해 안전 위반으로 새기 전에
            # 걸러낸다 - is_done()도 이미 target_replacement를 정상 완료
            # 신호로 취급하므로(기존 설계와 일치) 이 결과를 "cleanup()이
            # 요청한 정상 중단"과 동일하게 처리한다(stop_reason 안 건드림).
            _check_target()
            if target_replacement["v"] is not None:
                return True
            if window is not None and not window.get("all_injected") and stage_idx is not None:
                if is_stage_injected_fn(cr_names[stage_idx]):
                    window["all_injected"] = True
            violation = _check_safety_once()
            if violation is not None:
                stop_reason["v"] = violation
                return True

    def _run_stages():
        try:
            stop_reason = {"v": None}
            for i, stage in enumerate(stages):
                if stop_event.is_set():
                    return
                _check_target()
                if target_replacement["v"] is not None:
                    return  # 이미 효과를 낸 뒤 대상이 바뀜 - 더 새 단계를 만들지 않는다

                pre_violation = _check_safety_once()
                if pre_violation is not None:
                    _raise_for_violation(pre_violation)

                cr_duration = f"{stage['duration_sec'] + stage_duration_safety_margin_sec:.0f}s"
                create_chaos_fn(cr_names[i], run_id, arm, target["name"], stage, duration=cr_duration)
                window = {"name": stage["name"], "start": now_fn(), "end": None, "all_injected": False}
                stage_windows.append(window)
                current_stage_index["i"] = i

                stopped = _stage_wait_with_safety(stage["duration_sec"], stop_reason, window=window, stage_idx=i)
                delete_chaos_fn(cr_names[i])
                if stopped:
                    window["end"] = now_fn()
                    if stop_reason["v"] is not None:
                        _wait_for_stage_gone(cr_names[i])
                        window["end"] = now_fn()
                        _raise_for_violation(stop_reason["v"])
                    return  # cleanup()이 stop_event로 중단시킨 정상 경로
                _wait_for_stage_gone(cr_names[i])
                window["end"] = now_fn()
            all_stages_done["v"] = True
        except Exception as e:
            thread_exception["e"] = e

    def _wait_for_stage_gone(cr_name: str):
        deadline = time.monotonic() + stage_recovery_timeout_sec
        while time.monotonic() < deadline:
            if not does_chaos_exist_fn(cr_name):
                return
            if stop_event.wait(stage_delete_poll_interval_sec):
                return
        raise TrialInvalid(
            f"{cr_name} 삭제 요청 후 {stage_recovery_timeout_sec}초 내 실제 소멸(복구) 미확인 - "
            f"CR 삭제·소멸 실패(즉시 중단 조건)")

    def inject():
        thread_ref["t"] = threading.Thread(target=_run_stages, daemon=True)
        thread_ref["t"].start()

    def is_started() -> bool:
        """AllInjected(requested 성공)와 실제 working set 상승을 함께 확인해야
        latch한다(명시 요구사항 - "AllInjected와 working set 상승을 함께
        확인"). **2026-09-20 정정(smoke 실측 발견)**: 이 둘을 is_effective()
        에서만 확인하고 is_started()는 AllInjected만으로 latch하면, run_once.py
        는 is_started()를 재시도 루프(`_wait_for`, 최대 `injection_started_
        timeout_sec`)로 기다리지만 `is_effective()`는 그 직후 **단 한 번만**
        확인한다(run_once.py의 `started and injector.is_effective()`) -
        Prometheus cAdvisor 지표 반영 지연(이번 smoke 실측 약 18초 - StressChaos
        자체는 즉시 점프해도 kubelet->cAdvisor->Prometheus 스크레이프 경로는
        지연이 있음)을 흡수하지 못해 실제로는 정상 적용된 압박이 `injection_
        valid=False`(invalid_run)로 오판정됐다. working set 상승 확인 자체를
        재시도되는 이 함수 쪽으로 옮겨 고쳤다 - `is_effective()`는 이제 이
        상태를 그대로 재사용한다."""
        if injection_started_at["t"] is not None:
            return True
        if thread_exception["e"] is not None:
            raise thread_exception["e"]
        idx = current_stage_index["i"]
        if idx is None:
            return False
        now = datetime.now(timezone.utc)
        if not is_stage_injected_fn(cr_names[idx]):
            last_seen_not_injected_at["t"] = now
            return False
        ws = get_working_set_fn(target["name"])
        baseline_ws = baseline_working_set["v"]
        if ws is None or baseline_ws is None or (ws - baseline_ws) < effective_rise_bytes:
            last_seen_not_injected_at["t"] = now  # AllInjected뿐 - 아직 "시작 확정"은 아님, 계속 재시도
            return False
        injection_started_at["t"] = now
        return True

    def is_effective() -> bool:
        return is_started()

    def get_actual_injection_time() -> Optional[str]:
        t = injection_started_at["t"]
        return t.isoformat() if t is not None else None

    def get_injection_observation_error_sec() -> Optional[float]:
        injected_at, not_yet_at = injection_started_at["t"], last_seen_not_injected_at["t"]
        if injected_at is None or not_yet_at is None:
            return None
        return (injected_at - not_yet_at).total_seconds()

    def get_last_seen_present_time() -> Optional[str]:
        t = last_seen_not_injected_at["t"]
        return t.isoformat() if t is not None else None

    def get_target_replacement() -> Optional[dict]:
        v = target_replacement["v"]
        if v is None:
            return None
        return {"replaced_at": v["replaced_at"], "replacement_pod": v["pod"]}

    def classify_stage(timestamp_iso: str) -> str:
        return _classify_timestamp_against_windows(timestamp_iso, [dict(w) for w in list(stage_windows)])

    def get_stage_windows() -> list:
        """어댑터가 실제로 기록한 stage 경계를 그대로 반환한다(명목 계산
        아님) - run_once.Injector.get_stage_windows 참고. 스냅샷(list(...))
        후 변환해 classify_stage()와 동일한 스레드 안전 패턴을 쓴다."""
        return [{"name": w["name"], "start": w["start"].isoformat(),
                 "end": w["end"].isoformat() if w["end"] is not None else None,
                 "all_injected": w.get("all_injected", False)}
                for w in list(stage_windows)]

    def is_done() -> bool:
        if thread_exception["e"] is not None:
            raise thread_exception["e"]
        return all_stages_done["v"] or target_replacement["v"] is not None

    def _wait_for_working_set_recovery(baseline_ws: float):
        """cleanup 후 working set이 baseline 근처로 돌아오는지 확인한다(명시
        요구사항) - bounded, 비차단(non-raising): 느린 페이지 캐시 회수 등
        정상적인 지연일 수 있어 실패해도 예외를 던지지 않고 evidence 로그에만
        기록한다."""
        deadline = time.monotonic() + cleanup_recovery_timeout_sec
        last_ws = None
        while True:
            last_ws = get_working_set_fn(target["name"])
            if last_ws is not None and abs(last_ws - baseline_ws) <= cleanup_recovery_tolerance_bytes:
                return True, last_ws
            if time.monotonic() >= deadline:
                return False, last_ws
            time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))

    def cleanup():
        stop_event.set()
        t = thread_ref["t"]
        if t is not None:
            t.join(timeout=CLEANUP_JOIN_TIMEOUT_SEC)
        for name in cr_names:
            delete_chaos_fn(name)
        deadline = time.monotonic() + cleanup_verify_timeout_sec
        remaining = list(cr_names)
        while remaining and time.monotonic() < deadline:
            remaining = [name for name in remaining if does_chaos_exist_fn(name)]
            if remaining:
                time.sleep(min(stage_delete_poll_interval_sec, 1.0))
        if remaining:
            raise RuntimeError(
                f"cleanup 후에도 남아있는 StressChaos CR: {remaining} - "
                f"수동 확인 필요(잔존 시 다음 trial 메모리 상태를 오염시킬 수 있음, 즉시 중단 조건)")

        baseline_ws = baseline_working_set["v"]
        if baseline_ws is not None and target["name"] is not None:
            recovered, final_ws = _wait_for_working_set_recovery(baseline_ws)
            _log({
                "event": "cleanup_recovery_check", "run_id": run_id, "recovered": recovered,
                "baseline_working_set_bytes": baseline_ws, "final_working_set_bytes": final_ws,
                "tolerance_bytes": cleanup_recovery_tolerance_bytes,
                "timeout_sec": cleanup_recovery_timeout_sec,
            })

    return Injector(prepare=prepare, inject=inject, is_started=is_started,
                     is_effective=is_effective, is_done=is_done, cleanup=cleanup,
                     get_actual_injection_time=get_actual_injection_time,
                     get_injection_observation_error_sec=get_injection_observation_error_sec,
                     get_last_seen_present_time=get_last_seen_present_time,
                     get_target_replacement=get_target_replacement,
                     classify_stage=classify_stage,
                     get_stage_windows=get_stage_windows)
