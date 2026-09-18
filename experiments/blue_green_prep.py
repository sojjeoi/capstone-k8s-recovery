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
from typing import Optional

from kubernetes import client, config

ROLLOUTS_GROUP = "argoproj.io"
ROLLOUTS_VERSION = "v1alpha1"
ROLLOUTS_PLURAL = "rollouts"

# 2026-09-19 - fixed_threshold pilot 01회에서 preview가 235초 만에(180초
# timeout보다 늦게) Ready된 채 방치된 사고 계기. 기존 공식 실측(전부 3코어+
# 3코어 active+preview 동시구동 조건): HEADROOM-COLDSTART 01회차 176.1초,
# 02회차 350.3초, 03회차 163.7초 + 이번 235초 - 전부 163.7~350.3초 범위.
# SLO나 복구시간 판정 기준이 아니라 "실험 준비 단계"의 최대 대기시간일 뿐이라,
# 관측된 최댓값(350.3초)에도 137초 여유를 두는 480초로 상향한다.
PREVIEW_PREP_TIMEOUT_SEC = 480.0
ROLLBACK_VERIFY_TIMEOUT_SEC = 60.0
ROLLBACK_VERIFY_POLL_SEC = 3.0


def _custom_api():
    config.load_kube_config()  # 클러스터 내부 배포 시엔 config.load_incluster_config()로 교체
    return client.CustomObjectsApi()


def _apps_api():
    config.load_kube_config()
    return client.AppsV1Api()


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


def get_blue_green_status(name: str, namespace: str) -> dict:
    """activeSelector/currentPodHash 스냅샷(2026-09-19 추가, rollback 대상 특정용)."""
    obj = _custom_api().get_namespaced_custom_object(
        ROLLOUTS_GROUP, ROLLOUTS_VERSION, namespace, ROLLOUTS_PLURAL, name
    )
    status = obj.get("status", {})
    bg = status.get("blueGreen") or {}
    return {"active_selector": bg.get("activeSelector"), "current_pod_hash": status.get("currentPodHash")}


def abort_preview(name: str, namespace: str) -> None:
    """`kubectl argo rollouts abort`와 동일한 효과(status.abort=true) - 진행 중인
    업데이트를 취소하고 preview ReplicaSet을 0으로 scale-down시킨다(실측 확인,
    2026-09-19: fixed_threshold pilot 01회 방치된 preview 수동 정리 시 검증됨).
    activeSelector는 건드리지 않는다 - undo/promotion이 아니다."""
    _custom_api().patch_namespaced_custom_object_status(
        ROLLOUTS_GROUP, ROLLOUTS_VERSION, namespace, ROLLOUTS_PLURAL, name,
        {"status": {"abort": True}},
    )


def _replicaset_desired(namespace: str, pod_hash: str) -> Optional[int]:
    items = _apps_api().list_namespaced_replica_set(
        namespace, label_selector=f"rollouts-pod-template-hash={pod_hash}"
    ).items
    return items[0].spec.replicas if items else None


def wait_until_rolled_back(name: str, namespace: str, expected_active_hash: Optional[str], aborted_hash: str,
                            timeout: float = ROLLBACK_VERIFY_TIMEOUT_SEC,
                            poll_interval: float = ROLLBACK_VERIFY_POLL_SEC) -> bool:
    """abort 이후 activeSelector가 원래대로고 우리가 만든 preview RS가 실제로
    0으로 줄었는지 실측 확인한다(2026-09-19 추가) - abort() 호출 자체가 성공해도
    controller reconcile은 비동기라 즉시 반영을 보장하지 않는다. 이미 조건이
    충족된 상태에서 호출해도 첫 poll에서 바로 True를 반환하므로 idempotent."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = get_blue_green_status(name, namespace)
        rs_desired = _replicaset_desired(namespace, aborted_hash) or 0
        if status["active_selector"] == expected_active_hash and rs_desired == 0:
            return True
        time.sleep(poll_interval)
    return False


def prepare_preview_with_rollback(name: str, namespace: str,
                                   timeout: float = PREVIEW_PREP_TIMEOUT_SEC,
                                   poll_interval: float = 5.0) -> dict:
    """prepare_preview()에 실패 시 자동 rollback을 더한 버전(2026-09-19 추가) -
    fixed_threshold pilot 01회에서 preview가 timeout보다 늦게 Ready된 채
    방치되고 Rollout이 Paused/Degraded로 남은 사고가 계기. run_calibration.py는
    기존 prepare_preview()(bool 반환)를 그대로 쓰므로 건드리지 않는다 - 이
    함수는 arm_controller.py 전용 신규 경로.

    반환 dict:
      ready: 시간 내 BlueGreenPause 도달했는지
      t_prep_start / t_preview_ready: ISO 시각(준비 시작 / 실제 Ready 관측 시각,
        시간 내 도달 못 했으면 t_preview_ready=None)
      prep_duration_sec: 실측 소요시간(성공/실패 모두 기록 - 실패 시 timeout을
        살짝 넘는 실제 경과시간)
      external_interference: bump 직후인데 activeSelector가 이미 우리가 알던
        값과 다름 - 다른 프로세스가 개입했을 가능성. 이 경우 무엇이 "우리
        preview"인지 특정할 수 없어 rollback을 시도하지 않는다(fail-closed).
      rollback_attempted / rollback_ok / aborted_pod_hash: timeout이고
        external_interference가 아닐 때만 채워짐 - abort_preview()를 이번
        호출이 만든 preview(bump 직후 읽은 current_pod_hash)에만 수행하고,
        wait_until_rolled_back()으로 실제 복원을 재확인한 결과.
      pre_prepare_active_selector / created_pod_hash: bump 전 activeSelector와
        이번 호출이 만든 pod_hash(성공/실패/외부개입 전부 - external_interference면
        created_pod_hash=None, 우리가 뭘 만들었는지 자체가 불확실하므로) - trial이
        끝날 때 "이 preview가 promote됐는지"를 판단하는 데 쓰인다(2026-09-19
        추가, cleanup_unpromoted_preview() 참고 - 성공적으로 준비됐지만 끝내
        promote 안 된 preview를 trial 종료 시 정리하기 위함).
    """
    before = get_blue_green_status(name, namespace)
    t0 = time.monotonic()
    t_prep_start = datetime.now(timezone.utc).isoformat()
    bump_template_annotation(name, namespace)
    after_bump = get_blue_green_status(name, namespace)

    result = {
        "ready": False, "t_prep_start": t_prep_start, "t_preview_ready": None,
        "prep_duration_sec": None, "external_interference": False,
        "rollback_attempted": False, "rollback_ok": None, "aborted_pod_hash": None,
        "pre_prepare_active_selector": before["active_selector"], "created_pod_hash": None,
    }

    if after_bump["active_selector"] != before["active_selector"]:
        result["external_interference"] = True
        return result

    result["created_pod_hash"] = after_bump["current_pod_hash"]

    deadline = t0 + timeout
    while time.monotonic() < deadline:
        if is_paused_pre_promotion(name, namespace):
            result["ready"] = True
            result["t_preview_ready"] = datetime.now(timezone.utc).isoformat()
            result["prep_duration_sec"] = time.monotonic() - t0
            return result
        time.sleep(poll_interval)

    result["prep_duration_sec"] = time.monotonic() - t0
    our_hash = after_bump["current_pod_hash"]
    result["rollback_attempted"] = True
    result["aborted_pod_hash"] = our_hash
    abort_preview(name, namespace)
    result["rollback_ok"] = wait_until_rolled_back(name, namespace, before["active_selector"], our_hash)
    return result


def cleanup_unpromoted_preview(prep_info: Optional[dict], name: str, namespace: str) -> Optional[bool]:
    """trial 종료 시(2026-09-19 추가) - preview 준비는 성공했지만(ready=True)
    detector가 끝내 promote를 안 한 채 trial이 끝나면, prepare_preview_with_
    rollback()의 timeout-rollback 경로를 안 타서 아무도 정리하지 않는 gap이
    있었다(fixed_threshold pilot 재실행에서 preview가 정상인데도 방치된 채
    Rollout이 2-revision으로 남는 것을 실측으로 발견 - detector 미탐지/
    prevented/timeout처럼 promote 없이 끝나는 trial에서 반복될 수 있음).

    prep_info가 None이거나 ready=False면 정리할 게 없다(timeout이면 이미
    prepare_preview_with_rollback 자신이 처리했고, 애초에 preview가 없었으면
    (native 등) 볼 것도 없음). ready=True인데 activeSelector가 여전히 준비
    전 값 그대로면(=promote 안 됨) 우리가 만든 pod_hash만 abort하고 복원을
    실측 재확인한다. activeSelector가 우리 pod_hash로 이미 바뀌어 있으면
    (=실제로 promote됨) 그건 이제 새 active이므로 손대지 않는다 - 옛 stable은
    Rollout 컨트롤러 자신의 scaleDownDelaySeconds 로직이 알아서 정리한다.

    반환: 정리 안 함(None) / 정리 성공(True) / 정리 시도했으나 실패(False -
    호출자가 예외로 승격해야 함, run_once.py의 기존 injector.cleanup() 실패
    처리(critical_failures->HarnessCorrupted)를 그대로 재사용하기 위함)."""
    if prep_info is None or not prep_info.get("ready"):
        return None
    pre_active = prep_info.get("pre_prepare_active_selector")
    our_hash = prep_info.get("created_pod_hash")
    if our_hash is None:
        return None
    current = get_blue_green_status(name, namespace)
    if current["active_selector"] != pre_active:
        return None  # promote됨 - 이제 active, 손대지 않음
    if current["current_pod_hash"] != our_hash:
        return None  # 우리 이후 다른 변경이 있었을 가능성 - fail-closed로 손대지 않음
    abort_preview(name, namespace)
    return wait_until_rolled_back(name, namespace, pre_active, our_hash)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="실험 반복마다 BlueGreen preview를 새로 준비")
    parser.add_argument("--name", default="vllm-serving")
    parser.add_argument("--namespace", default="vllm-serving")
    args = parser.parse_args()

    ok = prepare_preview(args.name, args.namespace)
    print("preview ready & paused" if ok else "TIMEOUT - preview not ready", "-", ok)
