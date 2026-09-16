#!/usr/bin/env python3
"""pod_kill 시나리오의 Injector 어댑터(run_once.py 계약 구현). 전조 없는
즉발 장애라 예측 경로 검증용이 아니라 Alertmanager 반응 경로·타임스탬프·
cleanup·집계 연결을 확인하는 게 목적이다(계약서 §1 - VLLMTargetDown 신호).
proposed가 native보다 반드시 빨라야 한다고 가정하면 안 된다 - K8s 기본
self-healing이 더 빠르거나 비슷한 게 정상적인 예상 결과다.

active Service selector로 대상을 동적으로 찾는다(chaos/scenario-pod-
kill.yaml의 정적 labelSelectors: {app: vllm-serving}만 쓰면 preview가
같이 떠있을 때 여러 pod가 매칭될 수 있음 - 그러면 fail-closed). 주입
직전에 정확한 pod 이름과 UID를 고정해서, 그 이후로는 절대 "새로 뜬 pod"를
다시 대상으로 잡지 않는다 - is_started()는 오직 그 고정된 이름/UID만
재확인한다.

Chaos Mesh PodChaos는 순간 액션(pod-kill)이라 CR 자체에 "종료됐는지" 상태가
없다 - 그래서 CR 생성 성공(requested)과 기존 UID가 실제로 사라졌는지
(effective)를 분리해서 확인한다. t_injection은 CR 요청 시각이 아니라
is_started()가 폴링으로 기존 UID 소멸을 처음 관측한 시각으로 기록한다 -
실제 삭제 시각 그 자체가 아니라 관측 시각이므로, 오차 상한은
poll_interval_sec이다(run_once.py가 injection_observation_error_sec으로
함께 기록 - load_ramp_adapter.py의 get_actual_injection_time()과 같은
원칙).
"""
import os
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from run_once import Injector, TrialInvalid

NAMESPACE = "vllm-serving"
ACTIVE_SERVICE = "vllm-active"
CHAOS_GROUP = "chaos-mesh.org"
CHAOS_VERSION = "v1alpha1"
CHAOS_PLURAL = "podchaos"


def _load_kube_config() -> None:
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        config.load_incluster_config()
    else:
        config.load_kube_config()


def get_active_pods() -> list:
    """vllm-active Service의 selector로 실제 매칭되는 pod들을 (name, uid)
    리스트로 돌려준다 - 정적 라벨(app=vllm-serving)만 쓰면 preview가 같이
    떠있을 때 여러 개 걸릴 수 있어서, Service selector(rollouts-pod-
    template-hash 포함)로 정확히 좁힌다."""
    _load_kube_config()
    core = client.CoreV1Api()
    svc = core.read_namespaced_service(ACTIVE_SERVICE, NAMESPACE)
    selector = svc.spec.selector or {}
    label_selector = ",".join(f"{k}={v}" for k, v in selector.items())
    pods = core.list_namespaced_pod(NAMESPACE, label_selector=label_selector)
    return [{"name": p.metadata.name, "uid": p.metadata.uid} for p in pods.items]


def get_pod(name: str) -> Optional[dict]:
    """이름으로 pod 하나를 조회 - 없으면(404) None. is_started()가 "고정해둔
    이름의 pod가 사라졌는지"를 반복 확인하는 용도라, 삭제된 상태(None)와
    다른 API 에러를 구분해야 한다(그 외 에러는 그대로 전파)."""
    _load_kube_config()
    core = client.CoreV1Api()
    try:
        p = core.read_namespaced_pod(name, NAMESPACE)
        return {"name": p.metadata.name, "uid": p.metadata.uid}
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def create_pod_chaos(cr_name: str, run_id: str, arm: str, target_pod_name: str) -> None:
    _load_kube_config()
    body = {
        "apiVersion": f"{CHAOS_GROUP}/{CHAOS_VERSION}",
        "kind": "PodChaos",
        "metadata": {
            "name": cr_name,
            "namespace": NAMESPACE,
            "labels": {"experiment-run-id": run_id, "phase8-arm": arm},
        },
        "spec": {
            "action": "pod-kill",
            "mode": "one",
            "selector": {
                "namespaces": [NAMESPACE],
                "pods": {NAMESPACE: [target_pod_name]},
            },
        },
    }
    client.CustomObjectsApi().create_namespaced_custom_object(
        CHAOS_GROUP, CHAOS_VERSION, NAMESPACE, CHAOS_PLURAL, body)


def delete_pod_chaos(cr_name: str) -> None:
    """몇 번을 불러도 안전(idempotent) - CR이 이미 없으면(404) 조용히
    넘어간다. prepare()/inject()가 전혀 안 불려 CR을 만든 적이 없어도(이름은
    이미 정해져 있으므로) 안전하게 호출 가능."""
    _load_kube_config()
    try:
        client.CustomObjectsApi().delete_namespaced_custom_object(
            CHAOS_GROUP, CHAOS_VERSION, NAMESPACE, CHAOS_PLURAL, cr_name)
    except ApiException as e:
        if e.status != 404:
            raise


def _sanitize_cr_name(run_id: str) -> str:
    """K8s 오브젝트 이름은 소문자+하이픈만 허용(DNS-1123) - run_id는
    scenario 이름에 언더스코어(pod_kill), timestamp에 대문자(T/Z)가 섞여
    있어 그대로 못 쓴다. 짧은 uuid를 붙여 충돌을 원천 방지."""
    safe = run_id.lower().replace("_", "-").replace(":", "-")
    return f"podkill-{safe}-{uuid.uuid4().hex[:6]}"[:253]


def make_pod_kill_injector(
    run_id: str, arm: str, rep: int,
    get_active_pods_fn: Callable[[], list] = get_active_pods,
    get_pod_fn: Callable[[str], Optional[dict]] = get_pod,
    create_chaos_fn: Callable[[str, str, str, str], None] = create_pod_chaos,
    delete_chaos_fn: Callable[[str], None] = delete_pod_chaos,
) -> Injector:
    """*_fn 파라미터들은 실제 K8s API 호출을 대신할 수 있는 훅 - 오프라인
    유닛테스트가 실클러스터 없이 대상없음/다중대상/정상종료 등을 검증할 수
    있게 하기 위함이다(기본값은 진짜 kubernetes 클라이언트 호출)."""
    cr_name = _sanitize_cr_name(run_id)
    target = {"name": None, "uid": None}
    injection_started_at = {"t": None}

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

    def inject():
        create_chaos_fn(cr_name, run_id, arm, target["name"])

    def is_started() -> bool:
        # 고정해둔 이름/UID만 재확인 - 새로 뜬 pod를 다시 대상으로 잡지 않는다.
        current = get_pod_fn(target["name"])
        started = current is None or current["uid"] != target["uid"]
        if started and injection_started_at["t"] is None:
            injection_started_at["t"] = datetime.now(timezone.utc).isoformat()
        return started

    def is_effective() -> bool:
        # CR 생성(requested)과 실제 기존 UID 소멸(effective)은 다른 사실이다 -
        # pod-kill은 순간 액션이라 CR 상태 필드로는 알 수 없고, 대상 자체의
        # 상태 변화로만 판단할 수 있다.
        return is_started()

    def get_actual_injection_time() -> Optional[str]:
        return injection_started_at["t"]

    def is_done() -> bool:
        # pod-kill은 순간 액션 - 기존 pod가 사라진 게 확인되면(is_started)
        # 이 injector가 할 일은 끝난다(새로 뜬 pod의 Ready 여부·SLO 정상화는
        # Prober가 별도로 판정한다).
        return injection_started_at["t"] is not None

    def cleanup():
        # cr_name은 uuid로 매 trial 고유하므로, 실제로 만든 적 없어도(prepare/
        # inject 전혀 미호출) delete_chaos_fn이 404를 조용히 삼켜서 안전하다 -
        # 별도 플래그로 "만들었는지" 추적할 필요 없음.
        delete_chaos_fn(cr_name)

    return Injector(prepare=prepare, inject=inject, is_started=is_started,
                     is_effective=is_effective, is_done=is_done, cleanup=cleanup,
                     get_actual_injection_time=get_actual_injection_time)
