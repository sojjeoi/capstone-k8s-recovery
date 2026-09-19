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
기본값(1초)에서 올리는 건 Rollout 자체의 설정이고(gitops/apps/vllm-serving/
overlays/network-tolerant/), 그걸 언제 적용할지는 run_network_degrade_
trial.py가 --probe-profile로 받아 TrialResult에 기록만 한다(9-x절, 발견 5).

3. 대상 pod 재확인(매 단계 전환 직전)에서 UID가 바뀐 걸 발견하면 무조건
   TrialInvalid로 처리하지 않는다(2026-09-18 2차 정정 - 리뷰 지적). "주입이
   한 번도 효과를 내기 전"과 "이미 효과를 낸 뒤"를 구분한다 - 전자는 실험이
   시작되기도 전에 대상이 바뀐 것이므로 외부 오염 가능성이 높아 여전히
   TrialInvalid(invalid_run)다. 후자는 네트워크 열화 자체가 probe 실패->
   재시작(발견 5)이나 tolerant profile의 calibration 실패로 이어진 것일 수
   있는 정상적인 실험 결과이므로, invalid로 버리지 않고 target_replaced
   계열 필드로 기록만 하고 더 이상 새 단계를 만들지 않는다(injector 입장의
   "주입은 끝났다"로 취급 - 이후 outcome은 평소대로 prober의 SLO 판정만으로
   결정된다).
"""
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from active_pod_resolver import NAMESPACE, get_active_pods, load_kube_config
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


def create_network_chaos(cr_name: str, run_id: str, arm: str, target_pod_name: str, stage: dict,
                         duration: Optional[str] = None) -> None:
    """duration(예: "160s")을 주면 Chaos Mesh가 그 시간 뒤 스스로 복구한다 - 하니스가 죽어도 지연이 남지
    않게 하는 안전망(calibrate_network_tolerant_probe.py가 씀). 기본 None이면 본문이 그대로라서 기존
    trial 동작(명시적 삭제 전까지 유지)은 바뀌지 않는다."""
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
    if duration is not None:
        body["spec"]["duration"] = duration
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


def _classify_timestamp_against_windows(timestamp_iso, windows) -> str:
    """timestamp_iso가 실제 stage 창 어디에 속하는지 분류하는 순수 함수(2026-09-20 추가 - network_degrade는
    classify_stage가 없어 slo_stage/detection_stage/action_stage가 늘 None이었다. 계약서 §5.10의 주 비교 지표에
    action stage가 들어 있어 채워야 한다. 어휘·경계 규칙(start <= t <= end)은 load_ramp의 classify_stage와 같다).
    windows = 시간순 [{"name", "start", "end"}](aware datetime, 실제 CR 생성 호출이 돌아온 시각 ~ 소멸을 확인한
    시각, end=None이면 아직 열려 있음). 반환: stage 이름 | "baseline"(첫 stage 시작 전) | "inter_stage_tail"(두
    stage 창 사이) | "drain"(마지막 stage 창이 끝난 뒤 - promotion으로 injector가 남은 stage를 만들지 않은 뒤(
    treatment-induced truncation)도 여기다) | "unknown". 절대 예외를 던지지 않고(run_once의 보조 정보),
    근거가 없으면(창이 없음, timestamp 파싱 불가, naive/aware 혼용) 추정하지 않고 "unknown"이다."""
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


def make_network_degrade_injector(
    run_id: str, arm: str, rep: int,
    get_active_pods_fn: Callable[[], list] = get_active_pods,
    is_stage_injected_fn: Callable[[str], bool] = is_stage_injected,
    does_chaos_exist_fn: Callable[[str], bool] = does_chaos_exist,
    create_chaos_fn: Callable[[str, str, str, str, dict], None] = create_network_chaos,
    delete_chaos_fn: Callable[[str], None] = delete_network_chaos,
    stages: list = STAGES,
    stage_delete_poll_interval_sec: float = 1.0,
    stage_recovery_timeout_sec: float = STAGE_RECOVERY_TIMEOUT_SEC,
    cleanup_verify_timeout_sec: float = CLEANUP_VERIFY_TIMEOUT_SEC,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> Injector:
    """*_fn 파라미터는 pod_kill_adapter.py와 같은 이유의 오프라인 테스트용
    의존성 주입 지점. get_active_pods_fn을 prepare() 때뿐 아니라 매 단계
    전환 직전에도 다시 불러 target이 여전히 그 pod인지 재확인한다(이름
    하나만으로는 "이름은 같지만 다른 pod"를 구분 못 하고, vllm-active
    Service가 지금 실제로 가리키는 pod을 다시 물어봐야 교체 시 새 pod의
    이름/UID도 자연히 얻을 수 있다 - prepare()와 같은 메커니즘 재사용).
    now_fn은 stage 창(classify_stage용) 시각 기록에만 쓰는 시계 주입 지점이다."""
    cr_names = [_sanitize_cr_name(run_id, i) for i in range(len(stages))]
    target = {"name": None, "uid": None}
    injection_started_at = {"t": None}  # 첫 단계가 실제 적용됐음을 처음 관측한 시각
    last_seen_not_injected_at = {"t": None}  # 아직 적용 전이었음을 마지막으로 관측한 시각
    current_stage_index = {"i": None}  # 지금 떠 있는 단계(백그라운드 스레드가 갱신)
    all_stages_done = {"v": False}
    stop_event = threading.Event()
    thread_ref = {"t": None}
    thread_exception = {"e": None}  # 백그라운드 스레드 예외를 메인 스레드(is_done 폴링)로 전달
    target_replacement = {"v": None}  # None 또는 {"replaced_at": iso, "pod": {"name","uid"}|None}
    # 실제로 만들어진 stage의 창(CR 생성 호출이 돌아온 시각 ~ 소멸 확인 시각) - classify_stage()가 씀.
    # _run_stages 스레드만 append/갱신하고 classify_stage는 스냅샷만 읽는다. promotion 등으로 남은 stage를
    # 만들지 않으면 그 stage는 창 자체가 없다(명목 일정이 아니라 실제로 일어난 것만 기록).
    stage_windows = []

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

    def _check_target():
        # 매 단계 전환 직전 active pod을 다시 조회한다. 한 개가 매칭되고
        # UID가 그대로면 아무 일도 안 한다.
        pods = get_active_pods_fn()
        if len(pods) == 1 and pods[0]["uid"] == target["uid"]:
            return
        # 여기 도달하면 뭔가 달라졌다 - active로 잡히는 pod이 0개/2개 이상
        # 이거나(전환 중 등), 1개지만 UID가 다르다(교체됨).
        if injection_started_at["t"] is None:
            # 주입이 아직 한 번도 효과를 내기 전 - 실험 자체의 결과일 수
            # 없다(아직 아무 효과도 없었으므로). 외부 오염 가능성이 높다고
            # 보고 invalid_run으로 처리한다.
            raise TrialInvalid(
                f"주입이 효과를 내기 전에 active pod 구성이 바뀜(원래 uid="
                f"{target['uid']}, 지금 {len(pods)}개: {[p['uid'] for p in pods]}) - "
                f"외부 오염 가능성, invalid_run 처리")
        # 이미 최소 한 번 효과가 확인된 뒤의 변화 - 네트워크 열화가 probe를
        # 실패시켜 재시작/교체로 이어진 것일 수 있는 정상적인 실험 결과다
        # (발견 5류 연쇄장애, 또는 tolerant profile의 calibration 실패).
        # invalid로 버리지 않고 사실만 기록한다 - 최초 1회만(이후 poll에서
        # 또 바뀌어도 "최초 교체 시각"을 덮어쓰지 않음).
        if target_replacement["v"] is None:
            replacement_pod = pods[0] if len(pods) == 1 else None
            target_replacement["v"] = {
                "replaced_at": datetime.now(timezone.utc).isoformat(),
                "pod": replacement_pod,
            }

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
                _check_target()
                if target_replacement["v"] is not None:
                    # 이미 효과를 낸 뒤 대상이 바뀜 - 더 새 단계를 만들지
                    # 않는다(새 pod은 애초에 이 trial이 주입해온 대상이
                    # 아니다). is_done()이 이 상태를 "주입 끝남"으로 본다.
                    return
                create_chaos_fn(cr_names[i], run_id, arm, target["name"], stage)
                window = {"name": stage["name"], "start": now_fn(), "end": None}
                stage_windows.append(window)
                current_stage_index["i"] = i
                interrupted = stop_event.wait(stage["duration_sec"])
                delete_chaos_fn(cr_names[i])
                if interrupted:
                    window["end"] = now_fn()
                    return
                # 다음 단계 CR을 만들기 전에 이 단계가 실제로 소멸(복구)했는지
                # 확인한다 - delete 요청 성공과 실제 tc 규칙 해제 완료는
                # 다른 사실이다(이 세션 전체의 requested-vs-effective 원칙과
                # 동일). 확인 없이 바로 다음 단계를 만들면 같은 대상 pod에
                # 두 NetworkChaos가 순간적으로 겹칠 위험이 있다.
                _wait_for_stage_gone(cr_names[i])
                window["end"] = now_fn()  # 소멸 미확인 시간초과면 여기 못 오고 창은 열린 채(end=None)로 남는다
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

    def get_target_replacement() -> Optional[dict]:
        v = target_replacement["v"]
        if v is None:
            return None
        return {"replaced_at": v["replaced_at"], "replacement_pod": v["pod"]}

    def classify_stage(timestamp_iso: str) -> str:
        return _classify_timestamp_against_windows(timestamp_iso, [dict(w) for w in list(stage_windows)])

    def is_done() -> bool:
        # 백그라운드 스레드에서 난 예외(단계 소멸 확인 시간초과, 효과를 내기
        # 전 대상 교체 등)를 여기서 다시 던져 run_once.py의 OBSERVING 루프가
        # catch하게 한다 - is_done()은 그 루프가 매 poll_interval_sec마다
        # 부르는 유일한 injector 메서드라 예외 전달의 자연스러운 지점이다
        # (스레드 자체 예외는 Python이 메인 스레드로 자동 전파해주지 않는다).
        if thread_exception["e"] is not None:
            raise thread_exception["e"]
        # 4단계를 다 돌았거나, 효과를 낸 뒤 대상이 바뀌어 더 진행하지 않기로
        # 했거나 - 어느 쪽이든 이 injector가 할 일은 끝났다(target_replacement가
        # 있으면 invalid가 아니라 정상 결과이므로 예외를 던지지 않는다 -
        # run_once.py는 이후 평소대로 prober의 SLO 판정으로 outcome을 정한다).
        return all_stages_done["v"] or target_replacement["v"] is not None

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
                     get_last_seen_present_time=get_last_seen_present_time,
                     get_target_replacement=get_target_replacement,
                     classify_stage=classify_stage)
