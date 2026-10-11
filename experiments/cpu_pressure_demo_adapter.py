#!/usr/bin/env python3
"""졸업작품 시연 전용 - 점진적 CPU 압박(활성 Pod 한정) Injector 어댑터.
memory_pressure_adapter.py를 전혀 수정하지 않고, 그 파일의 pod/노드
헬퍼(get_pod_details/get_node_ip/get_node_conditions/_node_healthy/
_prom_instant_query/_sanitize_cr_name/is_stage_injected/
delete_memory_stress_chaos)를 그대로 import해 재사용한다 - 새로 만든 건
CPU stressor CR 생성 함수와 CPU 전용 안전 판단 하나뿐이다.

**메모리와 다른 점(왜 더 단순한 안전 감시로 충분한가)**: 메모리 압박은
커널 cgroup OOM 메커니즘 때문에 노드 전체 가용 메모리를 실제로 소모하고
한도를 넘기면 강제 종료(OOMKilled)로 이어진다 - 그래서
memory_pressure_adapter.py는 Node MemAvailable을 촘촘히 감시하고 사전
투영 게이트까지 둔다. CPU는 cgroup CPU quota가 **하드 캡**이라 stress
worker를 몇 개 띄우든 대상 Pod 자신의 컨테이너 CPU 한도(3코어, 계약서
§6 동결값)를 절대 못 넘는다 - vLLM 자신의 요청처리 스레드와 그 3코어
예산을 두고 경쟁(=지연 유발)할 뿐, 노드 차원 폭주로 번질 구조적 경로가
없다(실측: 8코어 노드에서 active+preview 두 Pod가 동시에 각자 3코어
한도를 전부 써도 6코어(75%), 기타 pod(~0.5코어) 더해도 82% - 여유 있음,
2026-09-26 확인). 그래서 이 어댑터는 memory_pressure_adapter.py의 '노드
가용 메모리 사전 투영'류 게이트 없이, Node 상태(NotReady/pressure)와
대상 Pod의 OOMKilled·재시작만 감시한다 - 위험 수준에 맞춘 의도적
단순화다(ponytail: 이 두 항목만 감시, 노드 CPU 전체가 실제로 고갈되는
사고가 확인되면 그때 memory_pressure_adapter.py 수준의 사전 투영 게이트를
추가).

선점(활성 Pod에만 고정): create_cpu_stress_chaos()가 memory_pressure_
adapter.create_memory_stress_chaos()와 완전히 같은 구조로 selector.pods를
쓴다(labelSelectors만 쓰면 preview가 같이 떠 있을 때 모호해짐 - 이미
확인된 문제, 같은 해법 재사용) - 그래서 준비된(preview) Pod는 이 압박의
영향을 전혀 받지 않는다.

is_started()/is_effective() 분리는 memory_pressure_adapter.py와 동일한
이유(2026-09-20 정정 이력) - AllInjected와 실제 지표 상승을 함께, run_once.py
가 재시도하는 is_started() 쪽에서 확인해야 Prometheus 스크레이프 지연을
흡수한다.

§168(2026-09-27, 첫 라이브 리허설 proposed-01에서 실측 확인): 정책이
promote_preview에 성공하면 BlueGreen 컨트롤러가 구 ReplicaSet을 정상
scale-down해 이 어댑터의 원래 target pod를 지운다 - 그런데 안전 감시가
이걸 "대상이 사라짐"으로만 보고 pod_details_unreadable(invalid)로
fail-closed해, 정상적인 선제 전환 결과를 harness 실패로 오판했다.
memory_pressure_adapter.py가 §96에서 이미 겪은 것과 완전히 같은 문제라,
그 파일의 _check_target()/target_replacement 패턴을 새로 설계하지 않고
그대로 이식했다: 주입 효과 확정(is_started) 전 대상 교체는 여전히
fail-closed(TrialInvalid, 외부 오염 가능성), 효과 확정 후 교체만 정상
결과로 기록(get_target_replacement())한다."""
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

import requests
from kubernetes import client

from active_pod_resolver import NAMESPACE, get_active_pods, load_kube_config
from blue_green_prep import get_blue_green_status
from memory_pressure_adapter import (
    CHAOS_GROUP, CHAOS_VERSION, CHAOS_PLURAL, LOCAL_PROMETHEUS_URL,
    SAFETY_POLL_INTERVAL_SEC, STAGE_DURATION_SAFETY_MARGIN_SEC,
    PROM_QUERY_TIMEOUT_SEC, FRESHNESS_MAX_AGE_SEC,
    _node_healthy, _prom_instant_query,
    get_pod_details, get_node_ip, get_node_conditions, is_stage_injected,
    delete_memory_stress_chaos as _delete_stress_chaos,
)
from run_once import HarnessCorrupted, Injector, TrialInvalid


def _sanitize_cpu_cr_name(run_id: str, stage_index: int) -> str:
    """memory_pressure_adapter._sanitize_cr_name()과 같은 방식이지만 접두어를
    이 어댑터 것으로 쓴다 - 그 함수는 "memstress-"를 하드코딩해서 그대로
    재사용하면 CPU CR이 kubectl 목록에 메모리 압박으로 오인 표시된다.
    저장소 기존 관례(pod_kill/network_degrade가 서로 이름 함수를 공유하지
    않음, memory_pressure_adapter.py:313-317 주석)와도 맞다 - 각 어댑터가
    자기 이름 함수를 갖는다."""
    import uuid
    safe = run_id.lower().replace("_", "-").replace(":", "-")
    return f"cpustress-{safe}-s{stage_index}-{uuid.uuid4().hex[:6]}"[:253]


def _sanitize_cpu_label_value(run_id: str) -> str:
    """§176 rep=2 실측(2026-09-27, HTTP 422 "must be no more than 63
    characters")으로 확인됨 - StressChaos metadata.labels 값은 CR 이름
    (metadata.name, 253자 한도)과 달리 Kubernetes label-value 규칙(최대
    63자)을 따라야 한다. 전체 run_id를 그대로 쓰면 이 시나리오처럼 긴
    run_id에서 API가 객체 생성 자체를 거부한다. 같은 run_id는 항상 같은
    값이 나와야(uuid4처럼 매번 무작위면 안 됨) 한 run의 4개 stage CR이
    서로 같은 라벨로 상관관계를 유지한다 - sha256 앞 16자(63자 한도 안,
    이 실험 규모에서 충돌 가능성 없음)만 쓴다. 전체 run_id는 잘리지 않고
    annotations에 그대로 남겨 추적 가능하게 한다(길이 제한 없음) -
    contract·trial·evidence 로그는 이 함수와 무관하게 원본 run_id를
    그대로 쓴다(안 바뀜)."""
    import hashlib
    return hashlib.sha256(run_id.encode()).hexdigest()[:16]

# 계약서 §6 동결값(arm_controller.FIXED_THRESHOLD_CPU_LIMIT_CORES와 동일 - 새 숫자 아님).
# CPU stressor가 이 값을 절대 못 넘는 이유(cgroup 하드캡)는 위 모듈 docstring 참고.
ACTIVE_POD_CPU_LIMIT_CORES = 3.0
# 기준선 대비 이만큼 오르면 "효과가 실제로 났다"로 latch(임의 값 아님 - idle
# 시 실측 변동폭(0.03~0.04코어대, 본 세션 여러 trial에서 반복 확인)보다
# 충분히 크게 잡아 노이즈와 구분).
MIN_EFFECTIVE_CPU_RISE_CORES = 0.2


def _cpu_promql(pod_name: str) -> str:
    """탐지기(anomaly-detection/features.py METRICS['cpu'])와 정확히 같은
    PromQL(rate(container_cpu_usage_seconds_total[30s]))을 특정 pod 하나로
    좁힌 문자열 - get_pod_cpu_usage_cores()/get_pod_cpu_usage_cores_diagnostic()
    양쪽이 이 함수 하나로만 만들어 두 값이 절대 어긋나지 않게 한다(§169)."""
    return f'rate(container_cpu_usage_seconds_total{{namespace="{NAMESPACE}",pod="{pod_name}",container="vllm"}}[30s])'


def get_pod_cpu_usage_cores(pod_name: str, prom_url: str = LOCAL_PROMETHEUS_URL) -> Optional[float]:
    """탐지기와 정확히 같은 PromQL을 재사용한다 - 새 지표를 만들지 않았다."""
    return _prom_instant_query(_cpu_promql(pod_name), prom_url)


# §170(2026-09-26) - §169의 3회x3초(~9초) 짧은 재시도가 두 번 라이브에서
# 다 소진됨을 확인(두 번째는 3회 내내 empty_result만 나옴 - 질의 타이밍
# 미스 한 번이 아니라 preview-ready 직후 최소 10초+ 지속되는 공백). preview
# ready와 "CPU 지표 조회 준비 완료"를 별도 조건으로 다룬다 - 판정 기준(같은
# PromQL·30초 창·CPU 값 산식)은 그대로, 대기 자체에만 시간 제한을 둔 여유를
# 준다. 상한 산정 근거(발명한 숫자 아님): 같은 세션에서 원본 카운터를 직접
# 대조해 실측한 스크레이프 간격 ~15초(OBSERVED_CPU_SCRAPE_INTERVAL_SEC) x
# 스크레이프 4회 누락까지 허용 = 60초. 폴링 간격은 새 숫자를 만들지 않고
# 기존 SAFETY_POLL_INTERVAL_SEC(5초, memory_pressure_adapter.py 안전 감시
# 루프와 동일)를 그대로 재사용한다.
OBSERVED_CPU_SCRAPE_INTERVAL_SEC = 15.0  # 2026-09-26 range-query 실측(§168/§169 조사)
TELEMETRY_READY_TIMEOUT_SEC = OBSERVED_CPU_SCRAPE_INTERVAL_SEC * 4  # 60.0초


def get_pod_cpu_usage_cores_diagnostic(pod_name: str, prom_url: str = LOCAL_PROMETHEUS_URL) -> dict:
    """get_pod_cpu_usage_cores()와 완전히 같은 PromQL(_cpu_promql() 공유)로
    같은 값을 계산하지만, memory_pressure_adapter._prom_instant_query()가
    모든 실패를 None 하나로 뭉개는 것과 달리 '빈 응답'(질의는 성공했지만
    표본 없음/오래된 표본)과 '실제 조회 오류'(HTTP/연결 실패, 파싱 실패)를
    구분해 반환한다 - 이 어댑터의 주입 전 단발 기준선 조회 진단 전용이고,
    _prom_instant_query()를 쓰는 다른 안전 경로(memory_pressure_adapter.py
    자신, is_started()의 재시도 가능한 상승 확인)는 그대로 둔다(계약 변경
    없음). 반환값 {"cores": float|None, "reason": str} - reason은
    "ok"/"empty_result"/f"stale_sample(age=...)"/"non_success_status"/
    f"http_status_{code}"/f"request_exception:{type}"/f"parse_error:{type}"."""
    promql = _cpu_promql(pod_name)
    try:
        r = requests.get(f"{prom_url}/api/v1/query", params={"query": promql}, timeout=PROM_QUERY_TIMEOUT_SEC)
    except requests.exceptions.RequestException as e:
        return {"cores": None, "reason": f"request_exception:{type(e).__name__}"}
    if r.status_code != 200:
        return {"cores": None, "reason": f"http_status_{r.status_code}"}
    try:
        body = r.json()
        if body.get("status") != "success":
            return {"cores": None, "reason": "non_success_status"}
        result = body.get("data", {}).get("result", [])
        if not result:
            return {"cores": None, "reason": "empty_result"}
        sample_ts, sample_value = result[0]["value"]
        age_sec = time.time() - float(sample_ts)
        if age_sec > FRESHNESS_MAX_AGE_SEC:
            return {"cores": None, "reason": f"stale_sample(age={age_sec:.1f}s)"}
        return {"cores": float(sample_value), "reason": "ok"}
    except (ValueError, KeyError, IndexError, TypeError) as e:
        return {"cores": None, "reason": f"parse_error:{type(e).__name__}"}


def create_cpu_stress_chaos(cr_name: str, run_id: str, arm: str, target_pod_name: str, stage: dict,
                             duration: Optional[str] = None) -> None:
    """memory_pressure_adapter.create_memory_stress_chaos()와 완전히 같은
    구조 - stressors만 memory->cpu로 바꿨다(Chaos Mesh StressChaos가 이미
    지원하는 표준 stressor 종류, 새 CRD 아님)."""
    load_kube_config()
    body = {
        "apiVersion": f"{CHAOS_GROUP}/{CHAOS_VERSION}",
        "kind": "StressChaos",
        "metadata": {
            "name": cr_name,
            "namespace": NAMESPACE,
            "labels": {"experiment-run-id": _sanitize_cpu_label_value(run_id), "phase8-arm": arm},
            "annotations": {"experiment-run-id-full": run_id},
        },
        "spec": {
            "mode": "one",
            "selector": {
                "namespaces": [NAMESPACE],
                "pods": {NAMESPACE: [target_pod_name]},
            },
            "stressors": {
                "cpu": {"workers": stage["workers"], "load": stage.get("load", 100)},
            },
        },
    }
    if duration is not None:
        body["spec"]["duration"] = duration
    client.CustomObjectsApi().create_namespaced_custom_object(
        CHAOS_GROUP, CHAOS_VERSION, NAMESPACE, CHAOS_PLURAL, body)


def make_cpu_pressure_injector(
    run_id: str, arm: str, rep: int,
    stages: list,
    get_active_pods_fn: Callable[[], list] = get_active_pods,
    get_pod_details_fn: Callable[[str], Optional[dict]] = get_pod_details,
    get_node_conditions_fn: Callable[[str], dict] = get_node_conditions,
    get_pod_cpu_usage_fn: Callable[[str], Optional[float]] = get_pod_cpu_usage_cores,
    get_pod_cpu_usage_diagnostic_fn: Callable[[str], dict] = get_pod_cpu_usage_cores_diagnostic,
    is_stage_injected_fn: Callable[[str], bool] = is_stage_injected,
    create_chaos_fn: Callable = create_cpu_stress_chaos,
    delete_chaos_fn: Callable[[str], None] = _delete_stress_chaos,
    safety_poll_interval_sec: float = SAFETY_POLL_INTERVAL_SEC,
    stage_duration_safety_margin_sec: float = STAGE_DURATION_SAFETY_MARGIN_SEC,
    min_effective_rise_cores: float = MIN_EFFECTIVE_CPU_RISE_CORES,
    get_blue_green_status_fn: Callable[[str, str], dict] = get_blue_green_status,
    rollout_name: str = "vllm-serving",
    telemetry_ready_timeout_sec: float = TELEMETRY_READY_TIMEOUT_SEC,
    telemetry_ready_poll_interval_sec: float = SAFETY_POLL_INTERVAL_SEC,
    continue_after_target_replacement: bool = False,
) -> Injector:
    """stages: [{"name","workers","load"(선택,기본100),"duration_sec"}, ...].
    *_fn은 memory_pressure_adapter.py와 같은 이유의 오프라인 테스트 주입점.

    continue_after_target_replacement(2026-10-02, 정시 전환 회복 실험 전용, 기본 False -
    기존 동작 불변): True면 승격으로 active pod가 바뀐 뒤에도 원래 대상 pod(이름·UID
    고정)에 남은 stage를 계획대로 계속 건다 - 전환과 압박 종료를 분리하기 위함. 교체 후
    원래 pod가 사라지거나(404)·UID가 바뀌거나·OOM/재시작하면 예외 없이 target_lost로
    기록하고 stage를 멈춘다(그 실행은 압박 전달 미확인). Node 이상은 항상 먼저 보고
    HarnessCorrupted. 교체 전 실패는 기존과 동일하게 예외."""
    if not stages:
        raise ValueError("stages는 비어있지 않은 리스트여야 함(기본값 없음 - 명시적으로 전달할 것)")

    cr_names = [_sanitize_cpu_cr_name(run_id, i) for i in range(len(stages))]
    target = {"name": None, "uid": None, "node_name": None, "baseline_cpu_cores": None}
    baseline_restart_count = {"v": None}
    injection_started_at = {"t": None}
    stop_event = threading.Event()
    thread_ref = {"t": None}
    thread_exception = {"e": None}
    all_stages_done = {"v": False}
    target_replacement = {"v": None}
    # CR 생성·삭제 시각 기록(관측용 - 실제 전달 판정은 호출부가 CPU·생존 실측으로 따로 한다)
    delivery = {"stages": [], "end_reason": None, "target_lost": None}

    def prepare():
        pods = get_active_pods_fn()
        if len(pods) != 1:
            raise TrialInvalid(f"active selector에 매칭되는 pod가 {len(pods)}개(1개여야 함) - fail-closed")
        target["name"] = pods[0]["name"]
        target["uid"] = pods[0]["uid"]

        details = get_pod_details_fn(target["name"])
        if details is None:
            raise TrialInvalid("prepare 직후 대상 pod 상세 조회 실패(사라짐?) - fail-closed")
        target["node_name"] = details["node_name"]
        baseline_restart_count["v"] = details["restart_count"]

        conditions = get_node_conditions_fn(target["node_name"])
        if not _node_healthy(conditions):
            raise TrialInvalid(f"주입 전 Node({target['node_name']}) 상태 이상(fail-closed): {conditions}")

        # §170 - preview ready와 "CPU 지표 조회 준비 완료"를 별도 조건으로
        # 다룬다(지시 - §169의 3회x3초 짧은 재시도가 라이브에서 두 번 다
        # 소진됨을 실측 확인). 판정 기준(같은 PromQL·30초 창·CPU 값 산식)은
        # 전혀 안 바꾸고, 대기 자체에만 시간 제한(TELEMETRY_READY_TIMEOUT_SEC,
        # 산정 근거는 모듈 상단 주석)을 둔 여유를 준다 - 무한 재시도도, 빈
        # 값을 0으로 대체하지도 않는다. 대기 중 active pod/Node/Rollout이
        # 나빠지면 상한까지 기다리지 않고 즉시 중단한다.
        expected_active_selector = get_blue_green_status_fn(rollout_name, NAMESPACE).get("active_selector")
        wait_start = time.monotonic()
        deadline = wait_start + telemetry_ready_timeout_sec
        attempts = []
        baseline_cpu = None
        attempt = 0
        while True:
            attempt += 1
            diag = get_pod_cpu_usage_diagnostic_fn(target["name"])
            attempts.append({"attempt": attempt, "elapsed_sec": round(time.monotonic() - wait_start, 1),
                              "reason": diag["reason"]})
            print(f"[cpu_pressure_demo_adapter] telemetry-ready 대기 시도 {attempt}"
                  f"(+{attempts[-1]['elapsed_sec']}s): {diag['reason']}")
            if diag["cores"] is not None:
                baseline_cpu = diag["cores"]
                break
            if time.monotonic() >= deadline:
                break

            # 대기 중 즉시 중단 조건 확인(끝까지 기다리지 않음) - 기존
            # _immediate_stop_reason()과 같은 판정 등급을 재사용한다.
            recheck = get_pod_details_fn(target["name"])
            if recheck is None:
                raise TrialInvalid(f"telemetry-ready 대기 중 대상 pod 상세 조회 실패(사라짐?) - "
                                    f"fail-closed: {attempts}")
            if recheck.get("oom_killed"):
                raise TrialInvalid(f"telemetry-ready 대기 중 대상 pod OOMKilled - fail-closed: {attempts}")
            if (recheck.get("restart_count") or 0) > (baseline_restart_count["v"] or 0):
                raise TrialInvalid(f"telemetry-ready 대기 중 대상 pod 재시작 감지 - fail-closed: {attempts}")
            recheck_conditions = get_node_conditions_fn(target["node_name"])
            if not _node_healthy(recheck_conditions):
                raise HarnessCorrupted(f"telemetry-ready 대기 중 Node({target['node_name']}) "
                                        f"상태 이상: {recheck_conditions}")
            current_bg = get_blue_green_status_fn(rollout_name, NAMESPACE)
            if current_bg.get("active_selector") != expected_active_selector:
                raise TrialInvalid(
                    f"telemetry-ready 대기 중 Rollout activeSelector가 예상 밖으로 바뀜(원래="
                    f"{expected_active_selector}, 지금={current_bg.get('active_selector')}) - "
                    f"외부 개입 가능성, fail-closed: {attempts}")

            time.sleep(min(telemetry_ready_poll_interval_sec, max(0.0, deadline - time.monotonic())))

        if baseline_cpu is None:
            raise TrialInvalid(
                f"telemetry-ready 대기 상한({telemetry_ready_timeout_sec:.0f}초) 안에 유효한 CPU "
                f"기준선을 못 얻음 - fail-closed. 시도별 사유: {attempts}")
        target["baseline_cpu_cores"] = baseline_cpu

    def _immediate_stop_reason() -> Optional[dict]:
        details = get_pod_details_fn(target["name"])
        if details is None:
            return {"reason": "pod_details_unreadable", "severity": "invalid"}
        if details.get("oom_killed"):
            return {"reason": "target_oom_killed", "severity": "invalid"}
        if (details.get("restart_count") or 0) > (baseline_restart_count["v"] or 0):
            return {"reason": "target_restarted", "severity": "invalid"}
        conditions = get_node_conditions_fn(target["node_name"])
        if not _node_healthy(conditions):
            return {"reason": "node_unhealthy", "severity": "harness_corrupted", "detail": conditions}
        return None

    def _check_target():
        """memory_pressure_adapter.py의 _check_target()(§96)과 완전히 같은
        규칙을 그대로 재사용(§168) - 주입 효과 확정(is_started) 전 active
        pod 구성이 바뀌면 외부 오염 가능성으로 fail-closed(TrialInvalid),
        확정 후 바뀌면(BlueGreen 승격이 원인일 수 있음) 정상 결과로
        기록만 하고 예외를 던지지 않는다."""
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

    def _raise_for_violation(violation: dict):
        msg = f"[{violation['reason']}] {violation.get('detail', violation)}"
        if violation["severity"] == "harness_corrupted":
            raise HarnessCorrupted(msg)
        raise TrialInvalid(msg)

    def _stop_reason() -> Optional[dict]:
        if not continue_after_target_replacement:
            return _immediate_stop_reason()
        # 계속 모드: Node 이상이 pod 이상에 가려지지 않도록 Node부터 본다.
        conditions = get_node_conditions_fn(target["node_name"])
        if not _node_healthy(conditions):
            return {"reason": "node_unhealthy", "severity": "harness_corrupted", "detail": conditions}
        details = get_pod_details_fn(target["name"])  # 404면 None, 그 외 API 오류는 예외(소실 증거 아님)
        if details is None:
            return {"reason": "pod_details_unreadable", "severity": "invalid"}
        if details.get("uid") is not None and details["uid"] != target["uid"]:
            return {"reason": "target_uid_changed", "severity": "invalid"}
        if details.get("oom_killed"):
            return {"reason": "target_oom_killed", "severity": "invalid"}
        if (details.get("restart_count") or 0) > (baseline_restart_count["v"] or 0):
            return {"reason": "target_restarted", "severity": "invalid"}
        return None

    def _stop_on_violation(violation: dict) -> None:
        """교체 후(계속 모드)의 대상 pod 이상은 예외 없이 target_lost로 끝낸다 - 그 외는 기존대로 예외."""
        if (continue_after_target_replacement and target_replacement["v"] is not None
                and violation["severity"] == "invalid"):
            delivery["target_lost"] = {"at": datetime.now(timezone.utc).isoformat(),
                                       "reason": violation["reason"]}
            delivery["end_reason"] = "target_lost"
            return
        _raise_for_violation(violation)

    def _end_stage(i: int, end: str) -> None:
        delete_chaos_fn(cr_names[i])
        delivery["stages"][-1].update(deleted_at=datetime.now(timezone.utc).isoformat(), end=end)

    def _stop_for_replacement() -> bool:
        return target_replacement["v"] is not None and not continue_after_target_replacement

    def _run_stages():
        try:
            for i, stage in enumerate(stages):
                if stop_event.is_set():
                    delivery["end_reason"] = "stopped"
                    return
                _check_target()
                if _stop_for_replacement():
                    delivery["end_reason"] = "target_replaced"
                    return  # 이미 효과를 낸 뒤 대상이 바뀜(§168) - 더 새 단계를 만들지 않는다
                pre_stop = _stop_reason()
                if pre_stop is not None:
                    _stop_on_violation(pre_stop)
                    return

                cr_duration = f"{stage['duration_sec'] + stage_duration_safety_margin_sec:.0f}s"
                create_chaos_fn(cr_names[i], run_id, arm, target["name"], stage, duration=cr_duration)
                delivery["stages"].append({"index": i, "name": stage.get("name"), "cr_name": cr_names[i],
                                           "workers": stage.get("workers"), "duration_sec": stage["duration_sec"],
                                           "created_at": datetime.now(timezone.utc).isoformat(),
                                           "deleted_at": None, "end": None})
                deadline = time.monotonic() + stage["duration_sec"]
                while time.monotonic() < deadline:
                    remaining = max(0.0, deadline - time.monotonic())
                    if stop_event.wait(timeout=min(safety_poll_interval_sec, remaining)):
                        _end_stage(i, "stopped")
                        delivery["end_reason"] = "stopped"
                        return
                    _check_target()
                    if _stop_for_replacement():
                        _end_stage(i, "target_replaced")
                        delivery["end_reason"] = "target_replaced"
                        return
                    stop = _stop_reason()
                    if stop is not None:
                        _end_stage(i, stop["reason"])
                        _stop_on_violation(stop)
                        return
                _end_stage(i, "completed")
            all_stages_done["v"] = True
            delivery["end_reason"] = "completed"
        except Exception as e:  # noqa: BLE001 - 스레드 예외는 여기서만 관측 가능, is_done()/is_started()가 재확인
            thread_exception["e"] = e
            delivery["end_reason"] = delivery["end_reason"] or f"error: {type(e).__name__}"
            stop_event.set()

    def inject():
        thread_ref["t"] = threading.Thread(target=_run_stages, daemon=True)
        thread_ref["t"].start()

    def is_started() -> bool:
        """§96(memory_pressure_adapter.py) 정정 이력과 동일한 이유로,
        AllInjected와 실제 CPU 상승을 함께 확인해야 latch한다 - run_once.py가
        이 함수를 재시도(_wait_for)하므로 Prometheus 스크레이프 지연을
        여기서 흡수한다(is_effective()는 그 뒤 단 한 번만 불림)."""
        if injection_started_at["t"] is not None:
            return True
        if thread_exception["e"] is not None:
            raise thread_exception["e"]
        if not is_stage_injected_fn(cr_names[0]):
            return False
        cpu_now = get_pod_cpu_usage_fn(target["name"])
        if cpu_now is None:
            return False
        if cpu_now >= target["baseline_cpu_cores"] + min_effective_rise_cores:
            injection_started_at["t"] = datetime.now(timezone.utc)
            return True
        return False

    def is_effective() -> bool:
        return is_started()

    def is_done() -> bool:
        if thread_exception["e"] is not None:
            raise thread_exception["e"]
        if continue_after_target_replacement:  # 교체만으로는 끝이 아님 - 계획 완료 또는 대상 소실까지
            return all_stages_done["v"] or delivery["target_lost"] is not None
        return all_stages_done["v"] or target_replacement["v"] is not None

    def get_actual_injection_time() -> Optional[str]:
        t = injection_started_at["t"]
        return t.isoformat() if t is not None else None

    def get_target_replacement() -> Optional[dict]:
        v = target_replacement["v"]
        if v is None:
            return None
        return {"replaced_at": v["replaced_at"], "replacement_pod": v["pod"]}

    def cleanup():
        stop_event.set()
        t = thread_ref["t"]
        if t is not None:
            t.join(timeout=30.0)
        for name in cr_names:
            delete_chaos_fn(name)

    def get_stress_delivery_record() -> dict:
        return {"continue_after_target_replacement": continue_after_target_replacement,
                "planned_cr_names": list(cr_names),  # 정리 후 잔여 CR 확인용(생성 중 막힌 stage 포함)
                "target": dict(target), "baseline_restart_count": baseline_restart_count["v"],
                "injection_started_at": get_actual_injection_time(),
                "target_replacement": get_target_replacement(),
                "stages": [dict(s) for s in delivery["stages"]],
                "end_reason": delivery["end_reason"], "target_lost": delivery["target_lost"]}

    injector = Injector(prepare=prepare, inject=inject, is_started=is_started,
                        is_effective=is_effective, is_done=is_done, cleanup=cleanup,
                        get_actual_injection_time=get_actual_injection_time,
                        get_target_replacement=get_target_replacement)
    injector.get_stress_delivery_record = get_stress_delivery_record
    return injector
