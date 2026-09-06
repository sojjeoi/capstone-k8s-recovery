"""K8s API 얇은 래퍼 — Argo Rollouts promote·상태조회만 담당.
안전장치(idempotency/cooldown/검증)는 safety.py, 판단 로직은 policy.py 몫.

Phase 2.5 스파이크 결론 (실측 완료, 컨트롤러 v1.7.2 기준):
status.pauseConditions를 CustomObjectsApi로 직접 patch하면 API 호출은 예외
없이 끝나지만 실제로 selector가 안 바뀐다(컨트롤러가 재계산해 덮어쓰는 것으로
추정 — 공식 CLI는 /status 서브리소스 patch + spec.paused 해제를 함께 하는데
이 직접 patch는 그중 일부만 함). 반면 공식 kubectl-argo-rollouts CLI(동일
버전)는 실측으로 promote 성공을 확인함. 그래서 CLI를 1차 시도로 쓰고, direct
API patch는 CLI 자체가 없는 환경 대비 2차 시도로만 남긴다. "완성도 우선"으로
갈 경우 이 direct patch 부분을 공식 구현과 동일하게(/status 서브리소스 +
spec.paused 해제) 다시 짜는 게 다음 단계 — 그 전까지는 requested가 아니라
verified 여부로만 최종 성공을 판단할 것 (guideline.md 9-5절).
"""
import subprocess
import time

from kubernetes import client, config

ROLLOUTS_GROUP = "argoproj.io"
ROLLOUTS_VERSION = "v1alpha1"
ROLLOUTS_PLURAL = "rollouts"


def _custom_api():
    config.load_kube_config()  # 클러스터 내부 배포 시엔 config.load_incluster_config()로 교체
    return client.CustomObjectsApi()


def get_rollout_phase(name: str, namespace: str) -> str:
    obj = _custom_api().get_namespaced_custom_object(
        ROLLOUTS_GROUP, ROLLOUTS_VERSION, namespace, ROLLOUTS_PLURAL, name
    )
    return obj.get("status", {}).get("phase", "Unknown")


def is_paused_pre_promotion(name: str, namespace: str) -> bool:
    """safety.py가 promote 전에 호출 — pre-promotion paused 상태인지 확인."""
    obj = _custom_api().get_namespaced_custom_object(
        ROLLOUTS_GROUP, ROLLOUTS_VERSION, namespace, ROLLOUTS_PLURAL, name
    )
    conditions = obj.get("status", {}).get("pauseConditions") or []
    return any(c.get("reason") == "BlueGreenPause" for c in conditions)


def get_service_selector(service_name: str, namespace: str) -> dict:
    config.load_kube_config()  # 클러스터 내부 배포 시엔 config.load_incluster_config()로 교체
    svc = client.CoreV1Api().read_namespaced_service(service_name, namespace)
    return svc.spec.selector or {}


def promote_via_cli(name: str, namespace: str) -> dict:
    """1차 시도 — 공식 kubectl-argo-rollouts 플러그인(실측 검증 완료).

    `kubectl argo rollouts ...`(kubectl 플러그인 디스패치)가 아니라 바이너리를
    직접 호출한다 — 플러그인으로 인식되려면 파일명이 `kubectl-argo_rollouts`
    (언더스코어)여야 하는데 공식 설치 가이드는 `kubectl-argo-rollouts`(하이픈)
    이름으로 PATH에 두고 직접 실행하는 방식을 쓰므로 그에 맞춘다.
    """
    result = subprocess.run(
        ["kubectl-argo-rollouts", "promote", name, "-n", namespace],
        capture_output=True,
        text=True,
    )
    return {
        "method": "cli",
        "requested": result.returncode == 0,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def _promote_via_api_patch(name: str, namespace: str) -> dict:
    """2차 시도 — direct API patch. 실측상 selector를 안 바꾸는 걸로 확인됐지만
    (모듈 docstring 참고) CLI 플러그인이 없는 환경 대비 남겨둔다. 신뢰하지 말 것."""
    try:
        _custom_api().patch_namespaced_custom_object(
            ROLLOUTS_GROUP, ROLLOUTS_VERSION, namespace, ROLLOUTS_PLURAL, name,
            {"status": {"pauseConditions": None}},
        )
        return {"method": "api", "requested": True}
    except Exception as e:
        return {"method": "api", "requested": False, "error": str(e)}


def promote(name: str, namespace: str, verify_timeout: float = 5.0, poll_interval: float = 0.5) -> dict:
    """paused BlueGreen Rollout을 promote. CLI를 1차로, 실패 시 direct API
    patch를 2차로 시도한다. 어느 경로든 "requested"만으로 성공을 판단하지
    않고 active selector가 실제로 preview와 일치하는지(verified)까지 확인한
    뒤 반환한다."""
    preview_selector = get_service_selector("vllm-preview", namespace)

    result = promote_via_cli(name, namespace)
    if not result["requested"]:
        result = _promote_via_api_patch(name, namespace)

    deadline = time.monotonic() + verify_timeout
    while time.monotonic() < deadline:
        if get_service_selector("vllm-active", namespace) == preview_selector:
            result["verified"] = True
            return result
        time.sleep(poll_interval)
    result["verified"] = False
    return result


if __name__ == "__main__":
    # Phase 2.5 스파이크 데모: python rollouts_client.py <rollout-name>
    import sys

    name = sys.argv[1] if len(sys.argv) > 1 else "vllm-serving"
    ns = "vllm-serving"

    print("phase:", get_rollout_phase(name, ns))
    print("paused pre-promotion:", is_paused_pre_promotion(name, ns))
    print("active selector:", get_service_selector("vllm-active", ns))
    print("preview selector:", get_service_selector("vllm-preview", ns))

    print("promote:", promote(name, ns))
