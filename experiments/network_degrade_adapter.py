#!/usr/bin/env python3
"""network_degrade 시나리오의 Injector 어댑터(run_once.py 계약 구현).
chaos/scenario-network-degrade.yaml과 같은 4단계(500ms/1000ms/2000ms/4000ms,
90초씩)를 재현하되 두 가지를 다르게 한다.

1. 정적 labelSelectors 대신 active_pod_resolver로 실제 active pod을 동적으로
   고정한다(pod_kill_adapter.py와 동일한 이유 - preview가 같이 떠있으면
   대상이 모호해짐).
2. Workflow CRD 대신 NetworkChaos 4개를 이 어댑터가 직접 순차 생성/삭제한다.
   Workflow의 status 스키마는 문서로 확인하지 못했고(Serial 하위 단계별
   상태 표현이 다를 수 있음), 단일 NetworkChaos의 status.conditions는
   Chaos Mesh 공식 문서로 확인했다(Selected/AllInjected/AllRecovered,
   K8s 표준 conditions 형식) - 검증 가능한 메커니즘만 쓴다.

주입 효과 확인(is_effective)은 pod_kill과 달리 "사라짐"처럼 간단한 이분법
신호가 없다 - CR이 accepted된 것(requested)과 실제로 대상 pod의 netns에 tc
규칙이 적용된 것(effective)은 다른 사실이므로, status.conditions의
AllInjected=True를 폴링해 확인한다. 이 필드 경로는 Chaos Mesh 공식 문서
(chaos-mesh.org/docs/next/inspect-chaos-experiments)로 확인했지만 실클러스터
kubectl get networkchaos -o yaml로 직접 본 적은 없다 - 첫 실클러스터 trial
전에 반드시 raw 출력으로 대조 확인할 것.

"연쇄장애 실험(default) vs 순수 열화 실험(network_tolerant)" 분리는 이
어댑터의 책임이 아니다 - readinessProbe/livenessProbe.timeoutSeconds를 K8s
기본값(1초)에서 올리는 건 Rollout 자체의 설정이고(gitops/overlays/
vllm-serving-network-tolerant/), 그걸 언제 적용할지는 run_network_degrade_
trial.py가 --probe-profile로 받아 TrialResult에 기록만 한다(9-x절, 발견 5).
"""
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from active_pod_resolver import NAMESPACE, get_active_pods, get_pod, load_kube_config
from run_once import Injector, TrialInvalid

CHAOS_GROUP = "chaos-mesh.org"
CHAOS_VERSION = "v1alpha1"
CHAOS_PLURAL = "networkchaos"

# chaos/scenario-network-degrade.yaml과 동일한 4단계(1차 실행 20/50/100ms가
# baseline(~2.6초) 대비 노이즈에 묻혀 재설계된 값 - 그 YAML의 주석 참고).
STAGES = [
    {"name": "stage-1-500ms", "latency": "500ms", "jitter": "50ms", "duration_sec": 90},
    {"name": "stage-2-1000ms", "latency": "1000ms", "jitter": "100ms", "duration_sec": 90},
    {"name": "stage-3-2000ms", "latency": "2000ms", "jitter": "200ms", "duration_sec": 90},
    {"name": "stage-4-4000ms", "latency": "4000ms", "jitter": "400ms", "duration_sec": 90},
]
CLEANUP_JOIN_TIMEOUT_SEC = 30
STAGE_RECOVERY_TIMEOUT_SEC = 30  # 삭제 요청 후 실제 소멸(recover) 확인 대기 상한
CLEANUP_VERIFY_TIMEOUT_SEC = 30


def create_network_chaos(cr_name: str, run_id: str, arm: str, target_pod_name: str, stage: dict) -> None:
    load_kube_config()
    body = {
        "apiVersion": f"{CHAOS_GROUP}/{CHAOS_VERSION}",
        "kind": "NetworkChaos",
        "metadata": {
            "name": cr_name,
            "namespace": NAMESPACE,
            "labels": {"experiment-run-id": run_id, "phase8-arm": arm},
        },
        "spec": {
            "action": "delay",
            "mode": "one",
            "selector": {
                "namespaces": [NAMESPACE],
                "pods": {NAMESPACE: [target_pod_name]},
            },
            "delay": {"latency": stage["latency"], "jitter": stage["jitter"]},
        },
    }
    client.CustomObjectsApi().create_namespaced_custom_object(
        CHAOS_GROUP, CHAOS_VERSION, NAMESPACE, CHAOS_PLURAL, body)


def delete_network_chaos(cr_name: str) -> None:
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
    Chaos Mesh 공식 문서 기반(모듈 docstring 참고), 실클러스터 미검증."""
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
    """CR이 실제로 아직 존재하는지(404가 아니면 True) - is_stage_injected와
    다른 질문이다(그건 "적용됐는지", 이건 "아직 있는지"). delete 요청 후
    실제 소멸(Chaos Mesh finalizer가 tc 규칙을 먼저 걷어낸 뒤에야 오브젝트가
    진짜 사라짐)을 확인하는 용도로만 쓴다."""
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
    """K8s 오브젝트 이름은 소문자+하이픈만 허용(DNS-1123) - pod_kill_adapter.py의
    _sanitize_cr_name과 동일 이유. stage_index를 넣어 4단계가 서로 다른
    이름을 갖게 한다(순차 생성/삭제라 동시에 존재하진 않지만, cleanup()이
    4개 이름을 전부 안전하게 지울 수 있게 미리 다 계산해둔다)."""
    safe = run_id.lower().replace("_", "-").replace(":", "-")
    return f"netdelay-{safe}-s{stage_index}-{uuid.uuid4().hex[:6]}"[:253]


def make_network_degrade_injector(
    run_id: str, arm: str, rep: int,
    get_active_pods_fn: Callable[[], list] = get_active_pods,
    get_pod_fn: Callable[[str], Optional[dict]] = get_pod,
    is_stage_injected_fn: Callable[[str], bool] = is_stage_injected,
    does_chaos_exist_fn: Callable[[str], bool] = does_chaos_exist,
    create_chaos_fn: Callable[[str, str, str, str, dict], None] = create_network_chaos,
    delete_chaos_fn: Callable[[str], None] = delete_network_chaos,
    stages: list = STAGES,
    stage_delete_poll_interval_sec: float = 1.0,
    stage_recovery_timeout_sec: float = STAGE_RECOVERY_TIMEOUT_SEC,
    cleanup_verify_timeout_sec: float = CLEANUP_VERIFY_TIMEOUT_SEC,
) -> Injector:
    """*_fn 파라미터는 pod_kill_adapter.py와 같은 이유의 오프라인 테스트용
    의존성 주입 지점. get_pod_fn은 매 단계 전환 직전 target 이 여전히
    prepare()에서 고정한 그 pod(UID로 확인)인지 재확인하는 용도 - 대상
    pod이 "사라지는" 게 아니라 계속 살아있는 채로 네트워크만 열화되므로
    이름이 같아도 다른 pod일 수 있다(예: 주입 도중 BlueGreen 전환으로
    active가 바뀜 - 처음 review에서 놓친 부분, 2026-09-18 정정)."""
    cr_names = [_sanitize_cr_name(run_id, i) for i in range(len(stages))]
    target = {"name": None, "uid": None}
    injection_started_at = {"t": None}  # 첫 단계가 실제 적용됐음을 처음 관측한 시각
    last_seen_not_injected_at = {"t": None}  # 아직 적용 전이었음을 마지막으로 관측한 시각
    current_stage_index = {"i": None}  # 지금 떠 있는 단계(백그라운드 스레드가 갱신)
    all_stages_done = {"v": False}
    stop_event = threading.Event()
    thread_ref = {"t": None}
    thread_exception = {"e": None}  # 백그라운드 스레드 예외를 메인 스레드(is_done 폴링)로 전달

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

    def _assert_target_unchanged():
        current = get_pod_fn(target["name"])
        if current is None or current["uid"] != target["uid"]:
            raise TrialInvalid(
                f"주입 도중 active pod이 바뀜(원래 uid={target['uid']}, 지금 "
                f"{current['uid'] if current else '없음'}) - BlueGreen 전환 등으로 "
                f"대상이 더 이상 prepare() 시점의 그 pod이 아님, invalid_run 처리")

    def _wait_for_stage_gone(cr_name: str):
        deadline = time.monotonic() + stage_recovery_timeout_sec
        while time.monotonic() < deadline:
            if not does_chaos_exist_fn(cr_name):
                return
            if stop_event.wait(stage_delete_poll_interval_sec):
                return  # cleanup()이 중단시킴 - 최종 확인은 cleanup() 자신이 함
        raise TrialInvalid(
            f"{cr_name} 삭제 요청 후 {stage_recovery_timeout_sec}초 내 실제 소멸(복구) 미확인")

    def _run_stages():
        try:
            for i, stage in enumerate(stages):
                if stop_event.is_set():
                    return
                _assert_target_unchanged()
                create_chaos_fn(cr_names[i], run_id, arm, target["name"], stage)
                current_stage_index["i"] = i
                interrupted = stop_event.wait(stage["duration_sec"])
                delete_chaos_fn(cr_names[i])
                if interrupted:
                    return
                # 다음 단계 CR을 만들기 전에 이 단계가 실제로 소멸(복구)했는지
                # 확인한다 - delete 요청 성공과 실제 tc 규칙 해제 완료는
                # 다른 사실이다(이 세션 전체의 requested-vs-effective 원칙과
                # 동일). 확인 없이 바로 다음 단계를 만들면 같은 대상 pod에
                # 두 NetworkChaos가 순간적으로 겹칠 위험이 있다.
                _wait_for_stage_gone(cr_names[i])
            all_stages_done["v"] = True
        except Exception as e:
            thread_exception["e"] = e

    def inject():
        thread_ref["t"] = threading.Thread(target=_run_stages, daemon=True)
        thread_ref["t"].start()

    def is_started() -> bool:
        # 한번 True가 되면 이후 단계 전환과 무관하게 계속 True(latch) -
        # pod_kill_adapter.py의 injection_started_at 패턴과 동일.
        if injection_started_at["t"] is not None:
            return True
        # is_done()과 같은 이유 - stage-1 자체에서 스레드가 예외로 죽으면
        # (예: 첫 단계부터 UID 불일치) current_stage_index가 끝내 None으로
        # 남아 아래 idx is None 분기만 반복돼 run_once.py의
        # injection_started_timeout_sec(기본 30초) 시간초과로만 끝나고,
        # 이 구체적 사유(TrialInvalid 메시지)가 사라진다 - 여기서도 확인해서
        # 더 이른 시점에 정확한 사유로 전달되게 한다.
        if thread_exception["e"] is not None:
            raise thread_exception["e"]
        idx = current_stage_index["i"]
        if idx is None:
            return False  # inject()가 아직 첫 단계 CR을 못 만듦(스레드 시작 직후)
        now = datetime.now(timezone.utc)
        if is_stage_injected_fn(cr_names[idx]):
            injection_started_at["t"] = now
            return True
        last_seen_not_injected_at["t"] = now
        return False

    def is_effective() -> bool:
        # CR accepted(requested)와 실제 tc 규칙 적용(effective)은 다른
        # 사실이다 - pod_kill의 is_effective와 같은 이유로 is_started와
        # 동일 신호를 쓴다(여기선 애초에 is_started 자체가 이미 "실제
        # 적용 관측"을 의미하므로 requested만으로 True가 되는 경로가 없음).
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

    def is_done() -> bool:
        # 백그라운드 스레드에서 난 예외(UID 변경 감지, 단계 소멸 확인 시간초과
        # 등)를 여기서 다시 던져 run_once.py의 OBSERVING 루프가 catch하게
        # 한다 - is_done()은 그 루프가 매 poll_interval_sec마다 부르는
        # 유일한 injector 메서드라 예외 전달의 자연스러운 지점이다(스레드
        # 자체 예외는 Python이 메인 스레드로 자동 전파해주지 않는다).
        if thread_exception["e"] is not None:
            raise thread_exception["e"]
        return all_stages_done["v"]

    def cleanup():
        stop_event.set()
        t = thread_ref["t"]
        if t is not None:
            t.join(timeout=CLEANUP_JOIN_TIMEOUT_SEC)
        for name in cr_names:
            delete_chaos_fn(name)
        # delete 호출이 성공(또는 404로 조용히 넘어감)했다고 해서 실제로
        # 다 지워졌다는 보장은 없다 - 여기서 실측 확인해서, 남아있으면
        # 예외를 던진다(run_once.py가 injector.cleanup()의 예외를
        # critical_failures에 담아 HarnessCorrupted로 승격시킨다 - 잔존
        # NetworkChaos를 "성공"으로 조용히 넘기지 않기 위함).
        deadline = time.monotonic() + cleanup_verify_timeout_sec
        remaining = list(cr_names)
        while remaining and time.monotonic() < deadline:
            remaining = [name for name in remaining if does_chaos_exist_fn(name)]
            if remaining:
                time.sleep(min(stage_delete_poll_interval_sec, 1.0))
        if remaining:
            raise RuntimeError(
                f"cleanup 후에도 남아있는 NetworkChaos CR: {remaining} - "
                f"수동 확인 필요(잔존 시 다음 trial 네트워크 상태를 오염시킬 수 있음)")

    return Injector(prepare=prepare, inject=inject, is_started=is_started,
                     is_effective=is_effective, is_done=is_done, cleanup=cleanup,
                     get_actual_injection_time=get_actual_injection_time,
                     get_injection_observation_error_sec=get_injection_observation_error_sec,
                     get_last_seen_present_time=get_last_seen_present_time)
