#!/usr/bin/env python3
"""BlueGreen 아래서 실제 active pod을 동적으로 찾는 공용 헬퍼 -
pod_kill_adapter.py에서 추출(2026-09-18). network_degrade_adapter.py·
memory_pressure_adapter.py도 동일 패턴이 필요하다: 정적 라벨(app=vllm-serving)
selector만 쓰면 preview가 같이 떠있을 때 여러 pod가 매칭돼 대상이 모호해지므로,
vllm-active Service의 selector(rollouts-pod-template-hash 포함)로 정확히
좁힌다."""
import os
from typing import Optional

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

NAMESPACE = "vllm-serving"
ACTIVE_SERVICE = "vllm-active"


def load_kube_config() -> None:
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        config.load_incluster_config()
    else:
        config.load_kube_config()


def get_active_pods() -> list:
    """vllm-active Service의 selector로 실제 매칭되는 pod들을 (name, uid)
    리스트로 돌려준다."""
    load_kube_config()
    core = client.CoreV1Api()
    svc = core.read_namespaced_service(ACTIVE_SERVICE, NAMESPACE)
    selector = svc.spec.selector or {}
    label_selector = ",".join(f"{k}={v}" for k, v in selector.items())
    pods = core.list_namespaced_pod(NAMESPACE, label_selector=label_selector)
    return [{"name": p.metadata.name, "uid": p.metadata.uid} for p in pods.items]


def get_pod(name: str) -> Optional[dict]:
    """이름으로 pod 하나를 조회 - 없으면(404) None, 그 외 API 에러는 전파."""
    load_kube_config()
    core = client.CoreV1Api()
    try:
        p = core.read_namespaced_pod(name, NAMESPACE)
        return {"name": p.metadata.name, "uid": p.metadata.uid}
    except ApiException as e:
        if e.status == 404:
            return None
        raise
