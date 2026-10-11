#!/usr/bin/env python3
"""2026-10-02 - 정시 전환 회복 실험(B군) 전용: Argo Rollout의
spec.strategy.blueGreen.scaleDownDelaySeconds 하나만 실험 중에 늘리고 되돌린다.

왜: 기본 30초면 +240초 전환 뒤 구 Pod가 약 +270초에 내려가 계획된 CPU 압박(~+360초)이
구 Pod에서 끝까지 전달되지 않는다 - 전환 효과와 압박 종료가 다시 섞인다.

원칙(Codex 검토 반영):
- 이 필드만 JSON merge-patch로 바꾼다. rollout.yaml을 apply하지 않는다(미커밋 antiAffinity 등 섞임).
- 변경 전에 원복 기록(원래 값·필드 존재 여부·Rollout UID)을 파일로 먼저 남긴다.
- abort 상태에서는 바꾸지 않는다(컨트롤러 동작 미검증 - preview 준비가 끝난 뒤에만 호출).
- 변경 후 컨트롤러 반영(observedGeneration)과 currentPodHash·activeSelector·RS 레플리카
  불변을 확인하고, 하나라도 어긋나면 즉시 원래 값으로 되돌리고 예외.
- 원복은 승격됐으면 구 RS가 0이 된 뒤에만, 다른 주체가 값을 바꿨거나 Rollout이 재생성됐으면
  덮어쓰지 않고 실패로 보고한다(조용한 통과 없음)."""
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

ROLLOUT_NAME = "vllm-serving"
NAMESPACE = "vllm-serving"
ROLLOUTS_GROUP, ROLLOUTS_VERSION, ROLLOUTS_PLURAL = "argoproj.io", "v1alpha1", "rollouts"
POD_HASH_LABEL = "rollouts-pod-template-hash"


class DelayOverrideError(RuntimeError):
    def __init__(self, msg: str, rollback_ok: bool = True):
        super().__init__(msg)
        self.rollback_ok = rollback_ok  # False면 라이브에 변경값이 남았을 수 있음 - 다음 실행 금지 사유


def _k8s():
    from kubernetes import client, config
    config.load_kube_config()
    return client


def get_rollout(name: str = ROLLOUT_NAME, namespace: str = NAMESPACE) -> dict:
    return _k8s().CustomObjectsApi().get_namespaced_custom_object(
        ROLLOUTS_GROUP, ROLLOUTS_VERSION, namespace, ROLLOUTS_PLURAL, name)


def patch_delay(value: Optional[int], name: str = ROLLOUT_NAME, namespace: str = NAMESPACE) -> None:
    """value=None이면 merge-patch의 null로 필드 자체를 제거(원래 없던 필드 복원)."""
    _k8s().CustomObjectsApi().patch_namespaced_custom_object(
        ROLLOUTS_GROUP, ROLLOUTS_VERSION, namespace, ROLLOUTS_PLURAL, name,
        {"spec": {"strategy": {"blueGreen": {"scaleDownDelaySeconds": value}}}})


def list_rs_replicas(namespace: str = NAMESPACE, label_selector: str = "app=vllm-serving") -> dict:
    """{pod_hash: {"desired": spec.replicas, "current": status.replicas}}"""
    items = _k8s().AppsV1Api().list_namespaced_replica_set(namespace, label_selector=label_selector).items
    out = {}
    for rs in items:
        h = (rs.metadata.labels or {}).get(POD_HASH_LABEL)
        if h:
            out[h] = {"desired": rs.spec.replicas or 0, "current": (rs.status.replicas or 0) if rs.status else 0}
    return out


def snapshot(rollout: dict, rs: dict) -> dict:
    spec_bg = ((rollout.get("spec") or {}).get("strategy") or {}).get("blueGreen") or {}
    status = rollout.get("status") or {}
    return {
        "uid": (rollout.get("metadata") or {}).get("uid"),
        "generation": (rollout.get("metadata") or {}).get("generation"),
        "observed_generation": status.get("observedGeneration"),
        "delay_present": "scaleDownDelaySeconds" in spec_bg,
        "delay": spec_bg.get("scaleDownDelaySeconds"),
        "current_pod_hash": status.get("currentPodHash"),
        "active_selector": (status.get("blueGreen") or {}).get("activeSelector"),
        "abort": bool(status.get("abort")),
        "phase": status.get("phase"),
        "rs": rs,
    }


def _reconciled(s: dict) -> bool:
    og = s["observed_generation"]
    try:
        return og is not None and int(og) >= int(s["generation"])
    except (TypeError, ValueError):
        return False


def _write(record_path: Path, record: dict) -> None:
    Path(record_path).write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def apply_override(target_sec: int, record_path: Path, *,
                   get_rollout_fn: Callable[[], dict] = get_rollout,
                   patch_fn: Callable[[Optional[int]], None] = patch_delay,
                   list_rs_fn: Callable[[], dict] = list_rs_replicas,
                   settle_timeout_sec: float = 30.0, poll_sec: float = 2.0,
                   sleep_fn=time.sleep, monotonic_fn=time.monotonic) -> dict:
    before = snapshot(get_rollout_fn(), list_rs_fn())
    if before["abort"]:
        raise DelayOverrideError(f"Rollout abort 상태에서는 변경하지 않음(컨트롤러 동작 미검증): {before}")
    record = {"rollout_uid": before["uid"], "original_present": before["delay_present"],
              "original_value": before["delay"], "applied_value": target_sec,
              "written_at": datetime.now(timezone.utc).isoformat(), "before": before,
              "applied": False, "verified": False}
    _write(record_path, record)  # 변경 전에 원복 정보를 먼저 남긴다

    def _fail(msg: str, rollout_replaced: bool = False):
        rollback_ok = False
        if rollout_replaced:  # 다른 Rollout 객체 - 우리 기록으로 덮어쓰지 않는다
            restored = "Rollout UID가 바뀜 - 원복 patch 안 함(수동 확인 필요)"
        else:
            try:
                try:
                    cur = snapshot(get_rollout_fn(), list_rs_fn())
                except Exception as e:  # noqa: BLE001 - 소유권 확인 불가: 180이 남는 쪽이 더 위험하므로 원복은 시도하되 미검증 처리
                    patch_fn(before["delay"] if before["delay_present"] else None)
                    raise RuntimeError(f"조회 실패({type(e).__name__}: {e})로 확인 없이 원복 patch만 보냄 - 검증 안 됨")
                if cur["uid"] != before["uid"]:
                    restored = "Rollout UID가 바뀜 - 원복 patch 안 함(수동 확인 필요)"
                elif cur["delay_present"] == before["delay_present"] and cur["delay"] == before["delay"]:
                    restored, rollback_ok = "적용되지 않았음(현재 값 = 원래 값) 확인", True
                elif cur["delay"] != target_sec:
                    restored = f"현재 값({cur['delay']})이 우리 적용값이 아님 - 다른 주체 변경 가능성, 덮어쓰지 않음"
                else:
                    patch_fn(before["delay"] if before["delay_present"] else None)
                    back = snapshot(get_rollout_fn(), list_rs_fn())
                    rollback_ok = back["delay_present"] == before["delay_present"] and back["delay"] == before["delay"]
                    restored = "즉시 원래 값으로 되돌림(재조회 확인)" if rollback_ok else f"원복 후 재조회 불일치: {back['delay']}"
            except Exception as e:  # noqa: BLE001 - 두 실패 모두 보고
                restored = f"즉시 원복도 실패: {type(e).__name__}: {e}"
        record["failure"] = f"{msg} | {restored}"
        record["rollback_ok"] = rollback_ok
        _write(record_path, record)
        raise DelayOverrideError(record["failure"], rollback_ok=rollback_ok)

    try:
        patch_fn(target_sec)
    except Exception as e:  # noqa: BLE001 - 적용됐는데 응답만 잃었을 수도 있으므로 원복 시도
        _fail(f"patch 실패: {type(e).__name__}: {e}")
    record["applied"] = True

    deadline = monotonic_fn() + settle_timeout_sec
    while True:
        try:
            last = snapshot(get_rollout_fn(), list_rs_fn())
        except Exception as e:  # noqa: BLE001 - 확인 불가도 실패 - 적용된 채 남기지 않는다
            _fail(f"변경 후 확인 조회 실패: {type(e).__name__}: {e}")
        problems = [p for p, bad in (
            ("rollout_uid_changed", last["uid"] != before["uid"]),
            ("current_pod_hash_changed", last["current_pod_hash"] != before["current_pod_hash"]),
            ("active_selector_changed", last["active_selector"] != before["active_selector"]),
            ("replicaset_replicas_changed", last["rs"] != before["rs"]),
        ) if bad]
        if problems:  # 부작용은 기다려도 사라지지 않는다 - 즉시 실패
            _fail(f"변경 후 Rollout 상태가 바뀜 {problems}: before={before} after={last}",
                  rollout_replaced="rollout_uid_changed" in problems)
        if last["delay"] == target_sec and _reconciled(last):
            break
        if monotonic_fn() >= deadline:
            _fail(f"{settle_timeout_sec:.0f}초 안에 반영 확인 실패(delay={last['delay']}, "
                  f"generation={last['generation']}, observed={last['observed_generation']})")
        sleep_fn(poll_sec)
    record["verified"] = True
    record["after"] = last
    _write(record_path, record)
    return record


def restore_override(record: dict, *, promoted: bool, old_pod_hash: Optional[str],
                     get_rollout_fn: Callable[[], dict] = get_rollout,
                     patch_fn: Callable[[Optional[int]], None] = patch_delay,
                     list_rs_fn: Callable[[], dict] = list_rs_replicas,
                     wait_timeout_sec: float = 300.0, poll_sec: float = 5.0,
                     sleep_fn=time.sleep, monotonic_fn=time.monotonic) -> dict:
    """예외를 던지지 않고 결과를 돌려준다 - 호출부가 restored=False면 다음 실행을 멈춘다."""
    result = {"restored": False, "error": None, "waited_old_rs": promoted,
              "restored_at": None, "final": None}

    def _owned() -> Optional[str]:
        cur = snapshot(get_rollout_fn(), list_rs_fn())
        if cur["uid"] != record["rollout_uid"]:
            return f"Rollout UID가 바뀜(재생성?) - 덮어쓰지 않음: {cur['uid']} != {record['rollout_uid']}"
        if cur["delay"] != record["applied_value"]:
            return (f"현재 값({cur['delay']})이 적용값({record['applied_value']})과 다름 - "
                    f"다른 주체의 변경 가능성, 덮어쓰지 않음")
        return None

    try:
        result["error"] = _owned()
        if result["error"]:
            return result
        if promoted:
            deadline = monotonic_fn() + wait_timeout_sec
            while True:
                rs = list_rs_fn().get(old_pod_hash, {"desired": 0, "current": 0})
                if rs["desired"] == 0 and rs["current"] == 0:
                    break
                if monotonic_fn() >= deadline:
                    result["error"] = f"구 RS({old_pod_hash})가 {wait_timeout_sec:.0f}초 안에 0이 안 됨: {rs}"
                    return result
                sleep_fn(poll_sec)
            result["error"] = _owned()  # 기다리는 동안 바뀌었을 수 있으니 patch 직전에 다시 확인
            if result["error"]:
                return result
        patch_fn(record["original_value"] if record["original_present"] else None)
        final = snapshot(get_rollout_fn(), list_rs_fn())
        result["final"] = final
        if final["delay_present"] != record["original_present"] or final["delay"] != record["original_value"]:
            result["error"] = f"원복 후 값 불일치: {final['delay_present']}/{final['delay']}"
            return result
        result["restored"] = True
        result["restored_at"] = datetime.now(timezone.utc).isoformat()
    except Exception as e:  # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {e}"
    return result
