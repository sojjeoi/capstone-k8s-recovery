#!/usr/bin/env python3
"""반복 실험마다 preview를 새로 준비 — template bump로 새 revision을 만들고
Ready까지 기다린 뒤 paused 상태로 남긴다(promotion은 하지 않음). recovery-policy의
런타임 역할이 아니라 "실험 셋업" 역할이라 별도 스크립트로 분리한다
(guideline.md 9-5절 BlueGreen 상태머신 1~4단계).

rollout.yaml의 strategy.blueGreen.autoPromotionEnabled가 이미 false라서
(이번 세션에서 template bump할 때마다 자동으로 Paused에 들어가는 걸 실측 확인)
"4단계: set_paused"는 별도 호출이 필요 없다 - Ready되면 자연히 멈춘다.
"""
import argparse
import time
from datetime import datetime, timezone

from kubernetes import client, config

ROLLOUTS_GROUP = "argoproj.io"
ROLLOUTS_VERSION = "v1alpha1"
ROLLOUTS_PLURAL = "rollouts"


def _custom_api():
    config.load_kube_config()  # 클러스터 내부 배포 시엔 config.load_incluster_config()로 교체
    return client.CustomObjectsApi()


def bump_template_annotation(name: str, namespace: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    _custom_api().patch_namespaced_custom_object(
        ROLLOUTS_GROUP, ROLLOUTS_VERSION, namespace, ROLLOUTS_PLURAL, name,
        {"spec": {"template": {"metadata": {"annotations": {"experiment-prep-ts": ts}}}}},
    )


def is_paused_pre_promotion(name: str, namespace: str) -> bool:
    obj = _custom_api().get_namespaced_custom_object(
        ROLLOUTS_GROUP, ROLLOUTS_VERSION, namespace, ROLLOUTS_PLURAL, name
    )
    conditions = obj.get("status", {}).get("pauseConditions") or []
    return any(c.get("reason") == "BlueGreenPause" for c in conditions)


def wait_until_paused(name: str, namespace: str, timeout: float = 180.0, poll_interval: float = 5.0) -> bool:
    """preview가 Ready돼서 BlueGreenPause에 들어갈 때까지 대기.
    실측(이번 세션)상 vLLM 모델 로딩에 60~110초 걸려서 기본 timeout을 넉넉히 잡는다."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_paused_pre_promotion(name, namespace):
            return True
        time.sleep(poll_interval)
    return False


def prepare_preview(name: str, namespace: str) -> bool:
    """9-5절 상태머신 1~4단계: template bump -> Ready 대기 -> paused 유지."""
    bump_template_annotation(name, namespace)
    return wait_until_paused(name, namespace)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="실험 반복마다 BlueGreen preview를 새로 준비")
    parser.add_argument("--name", default="vllm-serving")
    parser.add_argument("--namespace", default="vllm-serving")
    args = parser.parse_args()

    ok = prepare_preview(args.name, args.namespace)
    print("preview ready & paused" if ok else "TIMEOUT - preview not ready", "-", ok)
