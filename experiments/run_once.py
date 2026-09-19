#!/usr/bin/env python3
"""Phase 8 trial 공통 하네스. experiment-contract.md의 상태머신·TrialResult
스키마를 그대로 구현한다.

상태: PREPARING -> READY -> PROBING -> BASELINE -> INJECTING -> OBSERVING
      -> CLEANING -> COMPLETED / INVALID / TIMEOUT

시나리오별 실제 로직(chaos 주입 방법, probe 구현)은 이 파일이 모른다 - Injector/
Prober 어댑터(둘 다 작은 콜백 묶음)를 인자로 받는다. 여기서는 순서·타이밍·
정리·결과기록만 표준화한다(4단계에서 시나리오별 어댑터를 연결).

1차 리뷰(5개)에 이은 2차 리뷰 반영(2026-09-16):
6. safety.py의 action cooldown을 매 trial 시작 전 초기화한다(idempotency는
   유지 - run_id가 키에 포함돼 trial마다 자연히 달라짐). 각 trial은 독립
   표본이어야 하는데, 이전 trial(다른 arm일 수 있음)의 promotion이 남긴
   cooldown이 이번 trial의 조치를 막으면 비교가 왜곡된다. 순서는
   quiescence 확인 -> 활성 context 없음 확인 -> cooldown 초기화 -> 준비 ->
   context 등록 -> 주입(recovery-policy의 /admin/reset-cooldown이 서버
   쪽에서도 이 순서를 재확인). 초기화 자체가 실패하면 invalid_run이 아니라
   HarnessCorrupted(다음 trial까지 오염시킬 수 있는 문제라 배치를 멈춰야 함).
7. prober.stop()을 "실패하면 비치명적"으로 단순 처리하지 않는다 - stop()이
   예외 없이 반환해도 실제로 안 멈췄을 수 있고, 반대로 예외가 나도 이미
   죽어있을 수 있다. 그래서 stop() 시도 후 반드시 is_alive()로 실제 상태를
   재확인한다: 여전히 살아있으면(=다음 trial의 부하·지표를 오염시킬 수 있음)
   HarnessCorrupted, 죽어있으면(stop()이 예외를 냈어도) notes만 남긴다.

2차 리뷰 테스트 중 직접 발견한 버그: 활성 context가 이미 있어서(다른 trial
소유) PREPARING에서 TrialInvalid로 일찍 끝난 trial이, cleanup에서 "자기"
run_id로 clear를 무조건 시도해서 409(그 run_id로 등록한 적이 없으니 당연히
실패)를 critical_failures로 오인하던 것 - context_registered 플래그로
"이 trial이 실제로 자기 context를 등록했는가"를 추적해서, 등록 성공한
경우에만 clear를 시도하도록 수정.

quiescence(이전 trial의 firing 알림이 남아있지 않은지)는 recovery-policy의
GET /admin/quiescent로 확인한다 - PREPARING 진입 시 한 번, CLEANING에서
context clear 직전에 한 번(§6).

정리(prober 종료, chaos 리소스 삭제, experiment context clear)는 전부
finally에서 실행한다. 치명적 실패(action cooldown 초기화 실패, prober가
stop() 이후에도 안 죽음, injector.cleanup() 실패, experiment context clear
실패)는 critical_failures 리스트에 모아뒀다가 함수 끝에서 한 번에
HarnessCorrupted로 던진다 - 결과 파일은 그 전에 이미 기록돼 있으므로
호출자(run_all_scenarios.py, 8단계)가 어떤 trial에서 뭐가 문제였는지 알고
배치를 멈출 수 있다.

결과는 trial 시작 시점부터 매 상태 전이마다 임시파일+rename으로 덮어써서,
도중에 프로세스가 죽어도 마지막으로 기록된 상태가 파일에 남는다.
"""
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

import requests

import reconcile_audit

RESULTS_DIR = Path(__file__).parent / "results"
RECOVERY_POLICY_URL = "http://localhost:8080"  # 로컬 실행 전제 - kubectl port-forward -n vllm-serving svc/recovery-policy 8080:8080
ADMIN_TIMEOUT_SEC = 10
PROBE_READY_TIMEOUT_SEC = 30
INJECTION_STARTED_TIMEOUT_SEC = 30
QUIESCENCE_TIMEOUT_SEC = 60
QUIESCENCE_POLL_SEC = 3
BASELINE_TIMEOUT_SEC = 120  # 주입 전 baseline 관찰 단계 상한(2026-09-18 추가) - 이 안에 조건이 안 채워지면 invalid_run
# trial 종료 시 감사 필드(Git push 완료) 회수의 bounded wait(2026-09-19 추가) - 넘기면
# audit_status=pending/failed로 남기고 그대로 진행한다(Git 지연이 outcome/action을 바꾸지 않음).
# 나중에 reconcile_audit.py로 다시 채울 수 있다.
AUDIT_WAIT_SEC = 20
AUDIT_POLL_SEC = 2
# 탐지는 됐는데 판정이 아직 기록 전(recovery-policy가 process_signal 진행 중 - 예: promote()의
# CLI 호출·selector 검증)인 순간에 trial이 끝난 경우 판정이 확정되길 잠시 기다린다.
STATE_SETTLE_SEC = 10
STATE_SETTLE_POLL_SEC = 1


class TrialState(str, Enum):
    PREPARING = "preparing"
    READY = "ready"
    PROBING = "probing"
    BASELINE = "baseline"
    INJECTING = "injecting"
    OBSERVING = "observing"
    CLEANING = "cleaning"
    COMPLETED = "completed"
    INVALID = "invalid"
    TIMEOUT = "timeout"


_FINAL_STATE_BY_OUTCOME = {
    "recovered": TrialState.COMPLETED,
    "prevented": TrialState.COMPLETED,
    "timeout": TrialState.TIMEOUT,
    "invalid_run": TrialState.INVALID,
}


class TrialInvalid(Exception):
    """probe·주입·사전조건 문제로 이 trial만 무효 처리하면 되는 경우."""


class HarnessCorrupted(Exception):
    """클러스터가 다음 trial을 오염시킬 수 있는 상태로 남았을 가능성 - 이
    trial의 결과는 이미 파일에 기록됐지만, 호출자는 전체 배치를 멈추고
    수동 확인 후 재개해야 한다."""


@dataclass
class Injector:
    """시나리오별 chaos 주입 어댑터(4단계에서 실제 구현 연결).
    prepare()는 느릴 수 있는 준비(pod 생성, 네트워크 안정화 등)를 t_injection
    이전에 끝내기 위한 것 - inject()는 "지금 당장 주입 시작"만 한다.
    is_started()/is_effective()로 실제 효과가 났는지까지 확인해야
    injection_valid=True로 인정한다. is_done()은 정상 종료뿐 아니라 비정상
    종료(예: 프로세스 exit code != 0)도 구분해야 하면 TrialInvalid를 직접
    던져도 된다(run_once()의 OBSERVING 루프가 그대로 상위로 전파해
    outcome=invalid_run으로 처리한다). cleanup()은 prepare()/inject()가
    전혀 안 불렸어도 안전하게 호출 가능해야 한다(idempotent).
    get_actual_injection_time(): 선택 구현 - inject() 호출 시각과 "실제 주입
    효과가 시작된 시각"이 다를 수 있는 어댑터(예: pod 안에서 프로세스를
    백그라운드로 띄우는 경우, 또는 대상의 소멸을 폴링으로 확인하는 경우)를
    위한 것. 이 값은 어댑터가 실측한 정확한 사건 발생 시각이 아니라,
    is_started()가 폴링으로 그 변화를 "처음 관측한" 시각이다. None이 아닌
    값을 반환하면 run_once()가 t_injection을 이 값으로 덮어쓴다 - 미구현
    (None 필드)이면 inject() 호출 시각을 그대로 쓴다.
    get_injection_observation_error_sec(): 선택 구현 - 위 관측 오차의 상한을
    초 단위로 반환한다. poll_interval_sec 같은 설정값을 그대로 쓰면 안 된다
    (2026-09-16 정정 - is_started() 호출 자체의 실행시간·스케줄링 지연이
    설정값을 넘을 수 있어 진짜 상한이 아닐 수 있음). 어댑터가 "대상이
    살아있음을 마지막으로 관측한 시각"과 "처음 사라졌음을 관측한 시각"의
    실측 차이로 계산해야 한다(pod_kill_adapter.py/load_ramp_adapter.py 참고).
    None이면(예: 관측 없이 첫 poll에서 이미 바뀐 상태여서 기준점이 없음)
    run_once()가 t_injection_request~t_injection_observed 구간으로 대신
    계산한다(2026-09-18 추가 - 이 구간은 항상 실측값이라 임의 설정값을
    쓰는 것과 다르다).
    get_last_seen_present_time(): 선택 구현 - "대상이 살아있음을 마지막으로
    관측한 시각"을 그대로 ISO 문자열로 반환한다(2026-09-18 추가). 위
    get_injection_observation_error_sec()이 내부적으로 계산에 쓰는 것과
    같은 값이지만, 결과 스키마의 t_injection_last_seen 필드를 채우려면
    시각 자체가 필요하다 - 없으면(첫 poll에서 이미 사라짐) None.
    get_target_replacement(): 선택 구현 - 주입이 실제 효과를 낸 뒤(is_effective
    확인 후) 대상 자체가 바뀐 것을 어댑터가 관측했으면 {"replaced_at": ISO
    문자열, "replacement_pod": {"name":..., "uid":...} | None}을 반환한다
    (2026-09-18 추가 - network_degrade 리뷰). 이건 invalid_run이 아니다 -
    주입이 이미 효과를 낸 뒤의 대상 교체는 그 자체로 실험 결과일 수 있다
    (예: 네트워크 열화가 probe를 실패시켜 재시작을 유발하는 연쇄장애 자체가
    관찰 대상). 반대로 주입이 아직 한 번도 효과를 내기 전의 대상 교체는
    여전히 어댑터가 TrialInvalid로 직접 처리해야 한다(외부 오염과 실험
    결과를 구분하는 경계가 "효과를 낸 적이 있는가"). None이면 교체 없음.
    classify_stage(timestamp_iso): 선택 구현(2026-09-18 추가 - stage
    관측성 보완, §30에서 명목 stage 경계만 참고할 수 있었던 문제 수정).
    ISO8601 타임스탬프 문자열을 받아 그 시각이 실제 어느 실험 단계에
    속했는지 문자열로 분류해 반환한다(주입이 여러 "stage"로 나뉘는
    시나리오, 지금은 load_ramp만 구현) - stage 이름, 또는 "baseline"/
    "inter_stage_tail"/"drain"/"unknown" 중 하나. 절대 예외를 던지면
    안 되고(run_once()가 t_slo/t_detection/t_api_request 각각에 대해
    호출해 slo_stage/detection_stage/action_stage를 채우는 보조 정보라
    핵심 판정에 영향을 주면 안 됨), 분류할 근거가 없으면(요약 fetch
    실패 등) "unknown"을 반환해야지 임의로 추정하면 안 된다. 미구현
    (None 필드)이면 run_once()가 아예 호출하지 않고 관련 필드는 None으로
    남는다(pod_kill/network_degrade 등 stage 개념이 없는 시나리오)."""
    prepare: Callable[[], None]
    inject: Callable[[], None]
    is_started: Callable[[], bool]
    is_effective: Callable[[], bool]
    is_done: Callable[[], bool]
    cleanup: Callable[[], None]
    get_actual_injection_time: Optional[Callable[[], Optional[str]]] = None
    get_last_seen_present_time: Optional[Callable[[], Optional[str]]] = None
    get_injection_observation_error_sec: Optional[Callable[[], Optional[float]]] = None
    get_target_replacement: Optional[Callable[[], Optional[dict]]] = None
    classify_stage: Optional[Callable[[str], str]] = None
    # 선택 구현(2026-09-19 추가) - preview 준비 진단 정보(실제 Ready 시각,
    # 소요시간, timeout 시 자동 rollback 결과)를 노출한다. prepare()가
    # TrialInvalid/HarnessCorrupted를 던져도(=preview 준비 실패) 이 값은
    # 여전히 조회 가능해야 한다 - arm_controller.wrap_injector_with_preview_prep()가
    # prepare() 실행마다 최신 결과를 갱신해두므로, run_once()는 finally에서
    # (prepare 성공/실패 무관하게) 한 번 호출해 TrialResult에 반영한다.
    # 미구현(None 필드)이면 native 등 preview 자체가 없는 arm이라 스킵.
    get_preview_prep_info: Optional[Callable[[], Optional[dict]]] = None


@dataclass
class Prober:
    """합성 부하/헬스체크 어댑터(4단계에서 실제 구현 연결). load generator와
    같은 프로세스를 재사용하지 않는다 - arm마다 실제 주입 부하 자체가 다르면
    SLO 판정 표본이 arm 간에 달라져 비교가 왜곡된다(리뷰 지적).
    is_alive(): probe 프로세스 자체가 정상 실행 중인가(측정 대상 서비스의
    상태와 무관 - 서비스가 장애나는 건 관찰 대상이지 probe 고장이 아니다).
    stop() 이후에도 이 함수가 계속 True를 내면 다음 trial이 오염된다.
    check_slo_violation()/check_recovered(): slo-definition.md 기준 판정을
    어댑터가 직접 구현 - run_once()는 언제 물어볼지, t_slo보다 먼저는 안
    묻는다는 순서만 안다. get_actual_slo_time()/get_actual_recovery_time():
    선택 구현 - check_*()가 True를 반환한 poll 시각이 아니라, probe 자체가
    계산한(예: 요청 로그 기반) 더 정밀한 실제 판정 시각을 쓰고 싶을 때.
    None이면 poll 시각(호출 시점)을 그대로 쓴다.
    is_slo_evaluable(): 선택 구현 - check_slo_violation()의 False가 "정말
    위반이 없다"(COMPLIANT)인지 "아직 판정할 표본이 부족하다"(NOT_EVALUABLE)
    인지 run_once()가 구분하게 해준다(2026-09-17 정정 - pod_kill native
    파일럿에서 관측 창이 warmup보다 먼저 끝나 probe 데이터를 한 번도 못
    읽은 채 "prevented"로 오판정된 사례 발견). True를 반환해야만 "여태
    위반이 안 보였다"를 진짜 COMPLIANT로 인정한다 - False를 반환하면
    check_slo_violation()이 계속 False였어도 run_once()는 "prevented"로
    조기 종료하지 않는다(POSITIVE 위반 감지 자체는 evaluable 여부와 무관하게
    항상 신뢰한다 - 이건 "위반 없음을 믿어도 되는가"만 게이트한다). None
    필드(미구현)는 게이트 통과 여부는 기존처럼 항상 evaluable로 간주해
    동작은 그대로 유지하지만(하위호환), 결과에 남는 slo_evaluable_at_exit은
    "검증됨(True/False)"이 아니라 "검증 안 함"을 뜻하는 None으로 기록된다.
    notify_injected(t_injection): 선택 구현 - is_effective()로 주입이 실제
    적용됐음을 확인한 직후 run_once()가 정확히 한 번 호출한다(2026-09-17
    추가). is_slo_evaluable()이 "주입 이후" 표본만으로 유효한 관측 창이
    쌓였는지 판단하려면 주입 기준 시각을 알아야 하는데, Prober는 자기가
    언제 시작됐는지만 알고 주입이 언제 일어났는지는 모른다 - 이 훅이 그
    기준점을 넘겨준다. 미구현이면 안 불린다(무해).
    get_baseline_status(): 선택 구현(2026-09-18 추가 - 주입 전 baseline
    미확보 문제 수정). run_once()가 주입 전 BASELINE 단계에서 최대
    BASELINE_TIMEOUT_SEC(120초) 동안 poll_interval_sec 간격으로 반복 호출한다.
    매번 {"ready": bool, "sample_count": int|None, "p95": float|None,
    "availability": float|None, "ready_at": str(선택)}를 반환해야 한다 -
    "ready"가 True인 첫 호출에서 그 시점의 값들을 결과에 기록하고 즉시 주입을
    진행, 120초 안에 한 번도 True가 안 나오면 주입 자체를 하지 않고
    invalid_run으로 끝낸다. None(미구현)이면 이 단계 전체를 건너뛴다(하위호환
    - pod_kill/network_degrade 등 아직 이 훅이 없는 어댑터는 기존과 동일하게
    probe 생존 확인 직후 바로 주입, baseline_valid는 검증 안 함을 뜻하는
    None으로 기록)."""
    start: Callable[[], None]
    is_alive: Callable[[], bool]
    check_slo_violation: Callable[[], bool]
    check_recovered: Callable[[], bool]
    stop: Callable[[], None]
    get_actual_slo_time: Optional[Callable[[], Optional[str]]] = None
    get_actual_recovery_time: Optional[Callable[[], Optional[str]]] = None
    is_slo_evaluable: Optional[Callable[[], bool]] = None
    notify_injected: Optional[Callable[[str], None]] = None
    get_baseline_status: Optional[Callable[[], dict]] = None


@dataclass
class Detector:
    """arm별 이상탐지 프로세스 생명주기(2026-09-18 추가 - 3-arm 파일럿 전
    orchestration 보완). run_load_ramp_trial.py가 지금까지 --arm 이름만
    결과에 태깅할 뿐 실제 detector 프로세스나 preview 준비를 전혀 담당하지
    않아서, non-native arm을 실행하면 detector가 실제로는 동작하지 않은 채
    잘못 라벨링된 결과가 생길 수 있었던 문제를 고친다. native는 이 객체
    자체가 없다(run_once(detector=None), 기본값) - arm_controller.py의
    make_detector_for_arm()이 fixed_threshold/proposed에만 실제로 만들어
    넘긴다.
    start(): baseline 확보(BASELINE 단계 통과) + context 등록이 모두 끝난
    뒤, chaos 주입 직전에 run_once()가 정확히 한 번 호출한다 - baseline
    관찰 도중에는 detector 프로세스 자체가 아예 존재하지 않아야 그 구간의
    신호·조치가 원천 차단된다.
    is_alive(): OBSERVING 루프에서 prober.is_alive()와 나란히 반복
    확인한다 - False면 TrialInvalid로 처리돼 invalid_run이 된다.
    stop(): idempotent해야 한다(start() 전에 불려도 안전). finally에서
    prober.stop()과 같은 자리에서 호출되고, 그 직후 is_alive()로 실제
    종료 여부를 재확인한다 - 여전히 살아있으면(다음 trial의 신호·조치
    오염 위험) HarnessCorrupted(prober의 기존 leak-check와 동일한
    심각도로 처리).
    name: 이 arm에 대응하는 detector 식별자(예: "fixed_threshold"/
    "isolation_forest" - 각 스크립트가 post_to_recovery_policy()에 실제로
    보내는 detector= 태그와 정확히 일치) - TrialResult.detector_process에
    기록돼, 이 trial이 어떤 detector로 실행되려 했는지 사후 감사할 수
    있게 한다."""
    start: Callable[[], None]
    is_alive: Callable[[], bool]
    stop: Callable[[], None]
    name: str


@dataclass
class TrialResult:
    run_id: str
    scenario: str
    arm: str
    rep: int
    sequence_index: int
    order_seed: int
    t_run_start: str
    is_pilot: bool = False
    # 재현성 메타데이터(2026-09-16, SLO v2 도입) - probe payload/SLO 산정
    # 기준이 나중에 또 바뀔 수 있으므로 각 trial 결과 JSON 자체에 "이 trial이
    # 어떤 probe·SLO 조건으로 판정됐는지"를 같이 남긴다.
    probe_profile: str = "inference-max1-rps1"
    slo_version: str = "unspecified"
    latency_slo_sec: Optional[float] = None
    probe_rps: float = 1.0
    # 이 trial에 실제로 배선된 detector 식별자(2026-09-18 추가 - arm
    # orchestration 보완). arm_controller.make_detector_for_arm()이 만든
    # Detector.name을 그대로 기록 - native거나 detector=None으로 호출됐으면
    # null. 이 trial이 "어떤 detector로 실행되려 했는지"의 감사 기록이라
    # detector.start()가 실제로 성공했는지와 무관하게(예: 시작하자마자
    # 크래시해 invalid_run이 됐어도) 채워진다.
    detector_process: Optional[str] = None
    # network_degrade 전용(2026-09-18 추가) - "default"/"network_tolerant" 중
    # 실제로 어떤 K8s readiness/livenessProbe.timeoutSeconds 설정으로 돌았는지
    # 기록한다. 위 probe_profile(SLO 측정용 HTTP probe 설정)과는 다른 축이다 -
    # 이건 K8s 자체 헬스체크 probe를 가리킨다. "default"는 K8s 기본값(1초,
    # 발견 5의 재시작 연쇄장애를 그대로 재현), "network_tolerant"는
    # gitops/overlays/vllm-serving-network-tolerant/ overlay 적용 상태.
    # network_degrade 외 시나리오·미적용 trial은 None.
    readiness_probe_profile: Optional[str] = None
    readiness_probe_timeout_sec: Optional[float] = None
    # 주입이 실제 효과를 낸 뒤 대상 자체가 바뀐 것을 어댑터가 관측했는지
    # (2026-09-18 추가, network_degrade 리뷰 - UID 재확인을 모든 경우에
    # TrialInvalid로 처리하면 안 된다는 지적에 따른 정정). True면 invalid_run
    # 이 아니라 정상 실험 결과다 - default profile에서는 연쇄장애(재시작)
    # 자체가 예상 가능한 결과, network_tolerant profile에서는 calibration
    # 실패 또는 예상 밖 재시작을 뜻할 수 있다(해석은 readiness_probe_profile과
    # 함께 봐야 함, 이 필드 자체는 profile을 모른 채 사실만 기록).
    target_replaced: bool = False
    t_target_replaced: Optional[str] = None
    target_replacement_pod_name: Optional[str] = None
    target_replacement_pod_uid: Optional[str] = None
    # "prevented" 조기 종료를 막는 최소 관찰시간(초, run_once()의 동명
    # 파라미터 값을 그대로 기록 - 2026-09-17 추가). 이 값이 trial마다
    # 달랐는지 사후에 재현성 검증하려면 결과 자체에 남아야 한다.
    min_observation_sec: float = 0.0
    # 이 trial이 어떤 타임스탬프 체계로 기록됐는지(2026-09-18 추가) - "v2"는
    # t_injection_request/last_seen/observed 3분할 + t_slo/t_recovery가
    # observed_at(완료 시각) 기준인 새 방식. 이 필드가 없거나 "v1"이면 옛
    # 방식(t_injection 단일 필드, t_slo/t_recovery가 sent_at 기준)으로 기록된
    # trial이다 - 기존 파일 재수정 없이 구분하기 위한 것.
    timing_schema_version: str = "v2"
    # 주입 전 baseline 관찰 단계 결과(2026-09-18 추가 - 주입 전 baseline
    # 미확보 문제 수정). prober.get_baseline_status()가 "ready"를 처음 True로
    # 낸 시점의 값들, 또는(120초 안에 못 채우면) 마지막으로 관측된 값들을
    # 기록한다. baseline_valid: True=조건 충족 후 주입 진행, False=120초 안에
    # 못 채워 invalid_run, None=어댑터가 get_baseline_status 미구현이라 이
    # 단계 자체를 건너뜀(검증 안 함 - is_slo_evaluable=None과 동일 관례).
    t_baseline_ready: Optional[str] = None
    baseline_sample_count: Optional[int] = None
    baseline_p95: Optional[float] = None
    baseline_availability: Optional[float] = None
    baseline_valid: Optional[bool] = None
    # BlueGreen preview 준비 진단(2026-09-19 추가 - fixed_threshold pilot 01회,
    # preview가 180초 timeout보다 늦게(235초) Ready된 채 방치된 사고 계기).
    # injector.get_preview_prep_info() 구현 시(non-native arm)만 채워짐 -
    # native나 preview 개념이 없는 시나리오는 전부 None. prepare()가
    # TrialInvalid/HarnessCorrupted로 실패해도(=preview 준비 자체가 실패)
    # finally에서 무관하게 회수하므로, invalid_run 결과에도 진단 목적으로 남는다.
    t_preview_prep_start: Optional[str] = None
    t_preview_ready: Optional[str] = None  # 시간 내 도달 못 했으면 None(실패 확정 - 늦게라도 됐는지는 별도 추적 안 함)
    preview_prep_duration_sec: Optional[float] = None  # 성공/실패(timeout) 모두 기록
    preview_rollback_attempted: Optional[bool] = None
    preview_rollback_ok: Optional[bool] = None
    # injector.inject() 호출 직전 시각(2026-09-18 추가) - 실제 주입 구간의
    # 하한. 첫 poll에서 이미 대상이 사라져 t_injection_last_seen이 없을 때
    # injection_observation_error_sec 계산의 대체 기준점으로도 쓰인다.
    t_injection_request: Optional[str] = None
    # 하위 호환을 위해 유지 - t_injection_observed와 같은 값(대표값)이다.
    t_injection: Optional[str] = None
    # 기존 대상이 살아있음을 마지막으로 관측한 시각(2026-09-18 추가) -
    # 없으면(첫 poll에서 이미 사라짐) null. injector.get_last_seen_present_time()
    # 구현 시에만 채움.
    t_injection_last_seen: Optional[str] = None
    # 주입 효과를 처음 관측한 시각(2026-09-18 추가) - t_injection과 항상
    # 같은 값. get_actual_injection_time() 구현 시 그 정밀값, 미구현이면
    # inject() 호출 시각.
    t_injection_observed: Optional[str] = None
    # 3단계 우선순위로 계산(2026-09-18 정정): 1) 어댑터의
    # get_injection_observation_error_sec(), 2) 없으면
    # t_injection_last_seen~observed, 3) last_seen도 없으면(첫 poll에서
    # 이미 사라짐) t_injection_request~observed. 전부 실측 구간이지
    # poll_interval_sec 같은 임의값은 없다(이전엔 3번 상황에서 None으로
    # 남겨 "근거 없음"을 표현했지만, request~observed도 엄연한 실측
    # 구간이다). injection_valid=True
    # 인 trial은 이제 이 필드가 항상 채워진다(둘 중 하나는 항상 있으므로).
    injection_observation_error_sec: Optional[float] = None
    t_injection_end: Optional[str] = None
    # t_detection/t_decision/t_api_request/t_switch는 전부 recovery-policy 서버 시각(authoritative,
    # 계약서 §5.2/§5.5) - t_detection: 유효 신호 최초 수락, t_decision: 정책이 action을 확정한 직후
    # (observe-only 포함), t_api_request: 실제 promotion 호출 직전, t_switch: promotion 후 active
    # selector 검증이 처음 성공한 시각. promotion이 없으면 t_api_request/t_switch는 null. 전부 첫 값만
    # 유지한다. 2026-09-19 이전 trial의 t_decision/t_switch는 항상 null이고 추정으로 채우지 않는다.
    t_detection: Optional[str] = None
    t_decision: Optional[str] = None
    t_api_request: Optional[str] = None
    t_switch: Optional[str] = None
    t_slo: Optional[str] = None
    # injector.classify_stage()로 계산한 stage 분류(2026-09-18 추가 - stage
    # 관측성 보완). 대응하는 timestamp(t_slo/t_detection/t_api_request)가
    # None이면 이 필드도 None(사건 자체가 없었음 - "unknown"과는 다르다).
    # 어댑터가 classify_stage 미구현이면 셋 다 None(하위호환, pod_kill/
    # network_degrade 등). action_stage는 t_api_request(정책이 K8s API를
    # 실제로 호출해 조치를 실행한 시각) 기준으로 분류한다 - action 필드
    # 자체는 문자열(예: "promote_preview")이라 대응하는 타임스탬프가 없어,
    # 셋 중 "조치가 취해진 시각"에 가장 가까운 t_api_request를 썼다.
    slo_stage: Optional[str] = None
    detection_stage: Optional[str] = None
    action_stage: Optional[str] = None
    # outcome=prevented로 결론 낸 시점에 prober.is_slo_evaluable()이 True였는지
    # (2026-09-17 추가 - pod_kill native 파일럿에서 warmup 전에 관측이 끝나
    # probe 데이터를 한 번도 못 읽은 채 prevented로 오판정된 사례를 계기로,
    # 이후 이 필드 없이 기록된(또는 어댑터가 is_slo_evaluable 미구현인) 과거
    # prevented 결과와 새로 검증된 결과를 구분하기 위함). prevented가 아닌
    # outcome이거나 어댑터가 is_slo_evaluable을 구현 안 했으면 None.
    slo_evaluable_at_exit: Optional[bool] = None
    t_recovery: Optional[str] = None
    t_audit_write: Optional[str] = None
    t_audit_push: Optional[str] = None
    commit_sha: Optional[str] = None
    # 판정·조치 필드(2026-09-19 정정) - 예전엔 run_once.py 어디서도 대입하지 않아 항상
    # 기본값이었다(proposed 파일럿에서 실제 promotion이 검증까지 됐는데도
    # detected=false/action="none"으로 남아 발견). 이제 non-native trial은 trial 종료 시
    # (context clear 전) recovery-policy의 authoritative 상태에서 회수해 채운다 -
    # judgment_source="live_state". native는 recovery-policy 미개입이라 기본값 그대로
    # (detected=false, action="none", 나머지 null). 과거 trial은 reconcile_audit.py가
    # 감사기록으로 보완하고 judgment_source="audit_reconcile" + reconciliation에
    # provenance(원래 값 포함)를 남긴다.
    #   detected: 현재 run의 유효 신호가 idempotency/stale/run_id 검사를 통과했는가
    #   detection_source: 최초 유효 탐지의 경로("predictive"/"reactive")
    #   detector: 그 탐지의 실제 source("isolation_forest"/"fixed_threshold"/"alertmanager")
    #   action/decision_outcome/idempotency_key: primary 판정(실행된 조치 > 최초 유효 탐지의
    #     판정, skipped_duplicate 제외 - 계약서 §5.6)
    #   promotion_verified: promotion을 실행했을 때의 selector 검증 결과(실행 안 했으면 null)
    detected: bool = False
    detection_source: Optional[str] = None
    detector: Optional[str] = None
    action: str = "none"
    decision_outcome: Optional[str] = None
    idempotency_key: Optional[str] = None
    promotion_verified: Optional[bool] = None
    judgment_source: Optional[str] = None
    # 비동기 감사 필드는 정책 결과와 분리한다(2026-09-19) - t_audit_write/t_audit_push/
    # commit_sha의 원천은 recovery-policy의 outbox/audit 상태이고, Git 지연·실패는
    # outcome이나 실제 action을 바꾸지 않고 audit_status로만 남는다.
    #   audit_status: "complete" | "pending" | "failed" | "not_applicable"(탐지·판정이
    #     없어 감사기록 대상 아님) | null(native 또는 아직 시도 안 함)
    #   미완료(pending/failed)면 t_audit_push/commit_sha는 null 유지 + audit_status_reason.
    audit_status: Optional[str] = None
    audit_status_reason: Optional[str] = None
    audit_record_id: Optional[str] = None  # 선택된 primary 감사기록의 record_id
    audit_reconciled_at: Optional[str] = None
    reconciliation: Optional[dict] = None  # reconcile_audit.py가 과거 trial을 보완할 때만 채움(provenance)
    outcome: Optional[str] = None
    injection_valid: bool = False
    probe_valid: bool = False
    invalid_reason: Optional[str] = None
    p95_peak: Optional[float] = None
    availability_min: Optional[float] = None
    t_run_end: Optional[str] = None
    notes: str = ""
    state: str = field(default=TrialState.PREPARING.value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _wait_for(check: Callable[[], bool], timeout: float, interval: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(interval)
    return False


def _wait_for_baseline(prober: "Prober", timeout: float, interval: float) -> dict:
    """주입 전 BASELINE 단계 폴링(2026-09-18 추가). prober.get_baseline_status
    미구현이면 이 단계 자체를 건너뛴다(하위호환) - valid=None은 "검증 안 함"
    이지 실패가 아니다(is_slo_evaluable=None 관례와 동일). 구현돼 있으면
    "ready"가 True가 될 때까지 최대 timeout초 동안 interval 간격으로 poll하고,
    실패해도(timeout 소진) 마지막으로 관측된 sample_count/p95/availability는
    그대로 반환해 사후에 "얼마나 가까웠는지" 알 수 있게 한다."""
    if prober.get_baseline_status is None:
        return {"valid": None, "t_ready": None, "sample_count": None, "p95": None, "availability": None}
    deadline = time.monotonic() + timeout
    status = {"sample_count": None, "p95": None, "availability": None}
    while time.monotonic() < deadline:
        status = prober.get_baseline_status()
        if status.get("ready"):
            return {
                "valid": True,
                "t_ready": status.get("ready_at") or _now(),
                "sample_count": status.get("sample_count"),
                "p95": status.get("p95"),
                "availability": status.get("availability"),
            }
        time.sleep(interval)
    return {
        "valid": False,
        "t_ready": None,
        "sample_count": status.get("sample_count"),
        "p95": status.get("p95"),
        "availability": status.get("availability"),
    }


def _write_result(result: TrialResult, results_dir: Path) -> None:
    # is_pilot=True는 results_dir/pilot/ 아래 별도 경로에 쓴다 - collect_metrics.py가
    # 본 실험 집계에서 파일럿을 note 텍스트 파싱 없이 구조적으로 제외할 수
    # 있게 하기 위함(2026-09-16 지적: 자유 텍스트 notes만으로는 실수로
    # 포함될 위험). results_dir은 run_once() 호출자가 넘긴 값(기본값은
    # 모듈 상수 RESULTS_DIR) - 테스트가 pytest tmp_path로 격리할 수 있게
    # 하기 위해 인자로 뺐다(2026-09-16, dry_run 테스트 산출물이 실제
    # results/에 누적되던 문제 수정).
    out_dir = (results_dir / "pilot") if result.is_pilot else results_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"trial-{result.run_id}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)  # 원자적 교체


def _is_quiescent() -> bool:
    try:
        resp = requests.get(f"{RECOVERY_POLICY_URL}/admin/quiescent", timeout=ADMIN_TIMEOUT_SEC)
        resp.raise_for_status()
        return bool(resp.json().get("quiescent"))
    except Exception:
        return False  # 확인 자체가 안 되면 조용하다고 우기지 않는다


def _wait_for_quiescence(arm: str) -> bool:
    if arm == "native":
        return True  # native는 recovery-policy가 안 떠있어 확인 대상 자체가 없음
    return _wait_for(_is_quiescent, QUIESCENCE_TIMEOUT_SEC, QUIESCENCE_POLL_SEC)


def _get_active_experiment_context(arm: str) -> Optional[dict]:
    if arm == "native":
        return None
    resp = requests.get(f"{RECOVERY_POLICY_URL}/admin/experiment-run", timeout=ADMIN_TIMEOUT_SEC)
    resp.raise_for_status()
    return resp.json().get("current")


def _reset_action_cooldown(arm: str) -> None:
    if arm == "native":
        return
    resp = requests.post(f"{RECOVERY_POLICY_URL}/admin/reset-cooldown", timeout=ADMIN_TIMEOUT_SEC)
    resp.raise_for_status()


def _register_experiment_context(run_id: str, scenario: str, arm: str, rep: int, started_at: str) -> None:
    if arm == "native":
        return  # native는 recovery-policy 자체가 안 떠있음(계약서 §1)
    resp = requests.post(
        f"{RECOVERY_POLICY_URL}/admin/experiment-run",
        json={"run_id": run_id, "scenario": scenario, "arm": arm, "rep": rep, "started_at": started_at},
        timeout=ADMIN_TIMEOUT_SEC,
    )
    resp.raise_for_status()


def _clear_experiment_context(run_id: str, arm: str) -> None:
    if arm == "native":
        return
    resp = requests.post(
        f"{RECOVERY_POLICY_URL}/admin/experiment-run/clear",
        params={"run_id": run_id}, timeout=ADMIN_TIMEOUT_SEC,
    )
    resp.raise_for_status()


def _get_experiment_state(arm: str, settle_sec: float = STATE_SETTLE_SEC,
                          poll_sec: float = STATE_SETTLE_POLL_SEC) -> Optional[dict]:
    """t_detection/t_api_request와 판정·조치 필드(detected/detection_source/detector/
    action/decision_outcome/idempotency_key/promotion_verified)의 authoritative
    source(2026-09-19 추가, 판정 필드는 같은 날 확장) - recovery-policy가 신호를 수락하거나
    promotion을 시도한 실제 사건을 process_signal() 내부에서 동기적으로 기록해둔 admin
    엔드포인트를 읽는다(경로는 처음 만든 /timing 그대로). detector 프로세스 stdout이나
    비동기 Git 감사기록(git_client.py)은 지연·실패가 있어도 그 함수의 반환을 막지 않게
    설계돼 있어 원천으로 쓸 수 없다(지시) - 이 엔드포인트 값만 신뢰한다. native는
    recovery-policy 자체가 안 떠있으므로(계약서 §1) 호출하지 않고 None - 다른 admin
    헬퍼들과 동일한 관례.

    탐지는 됐는데 판정(decision_outcome)이 아직 없는 상태는 recovery-policy가 그 신호를
    처리하는 도중(promote() 진행 중 등)이라는 뜻이다 - 이 순간을 그대로 기록하면 실제로는
    조치가 나가는 중인데 action="none"으로 남으므로 settle_sec 안에서 확정될 때까지
    기다린다. 그래도 안 끝나면 마지막 값을 그대로 돌려주고 호출자가 notes에 남긴다."""
    if arm == "native":
        return None
    deadline = time.monotonic() + settle_sec
    while True:
        resp = requests.get(f"{RECOVERY_POLICY_URL}/admin/experiment-run/timing", timeout=ADMIN_TIMEOUT_SEC)
        resp.raise_for_status()
        state = resp.json()
        in_flight = bool(state.get("detected")) and state.get("decision_outcome") is None
        if not in_flight or time.monotonic() >= deadline:
            return state
        time.sleep(poll_sec)


def run_once(
    scenario: str, arm: str, rep: int, sequence_index: int, order_seed: int,
    injector: Injector, prober: Prober, timeout_sec: float, poll_interval_sec: float = 1.0,
    probe_ready_timeout_sec: float = PROBE_READY_TIMEOUT_SEC,
    injection_started_timeout_sec: float = INJECTION_STARTED_TIMEOUT_SEC,
    baseline_timeout_sec: float = BASELINE_TIMEOUT_SEC,
    audit_wait_sec: float = AUDIT_WAIT_SEC,
    audit_poll_sec: float = AUDIT_POLL_SEC,
    state_settle_sec: float = STATE_SETTLE_SEC,
    detector: Optional[Detector] = None,
    run_id: Optional[str] = None,
    is_pilot: bool = False,
    probe_profile: str = "inference-max1-rps1",
    slo_version: str = "unspecified",
    latency_slo_sec: Optional[float] = None,
    probe_rps: float = 1.0,
    results_dir: Optional[Path] = None,
    min_observation_sec: float = 0.0,
    readiness_probe_profile: Optional[str] = None,
    readiness_probe_timeout_sec: Optional[float] = None,
) -> TrialResult:
    results_dir = results_dir or RESULTS_DIR
    # run_id를 밖에서 넘길 수 있게 한 이유: injector/prober는 run_once() 호출
    # *전에* 이미 만들어져 있어야 하는데(인자로 받으므로), 그 어댑터들이
    # ramp.py/probe.py의 raw 로그에 태깅하는 run_id와 여기서 쓰는 run_id가
    # 어긋나면 trial 결과 JSON과 raw 로그를 나중에 못 join한다. 호출자가 먼저
    # run_id를 만들어 어댑터 생성과 이 호출에 동일하게 넘기면 된다. 안 넘기면
    # (기존 테스트들처럼) 이전과 동일하게 자동 생성.
    run_id = run_id or f"{scenario}-{arm}-{rep:02d}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    result = TrialResult(
        run_id=run_id, scenario=scenario, arm=arm, rep=rep,
        sequence_index=sequence_index, order_seed=order_seed, t_run_start=_now(),
        is_pilot=is_pilot, probe_profile=probe_profile, slo_version=slo_version,
        latency_slo_sec=latency_slo_sec, probe_rps=probe_rps,
        min_observation_sec=min_observation_sec,
        readiness_probe_profile=readiness_probe_profile,
        readiness_probe_timeout_sec=readiness_probe_timeout_sec,
        detector_process=detector.name if detector is not None else None,
    )
    _write_result(result, results_dir)
    critical_failures: list = []
    context_registered = False  # 이 trial이 실제로 자기 context를 등록했는지 - cleanup에서
    # 등록도 안 한 context를 clear하려다 409(다른 trial 소유)로 오탐되는 걸 막기 위함

    try:
        # PREPARING - 순서 고정: quiescence -> 활성 context 없음 확인 ->
        # cooldown 초기화 -> (느릴 수 있는) 주입 준비.
        if not _wait_for_quiescence(arm):
            raise TrialInvalid("trial 시작 전 quiescence(이전 alert 해소) 확인 실패")

        active_ctx = _get_active_experiment_context(arm)
        if active_ctx is not None:
            raise TrialInvalid(f"trial 시작 전인데 이미 활성 실험 있음: {active_ctx.get('run_id')}")

        try:
            _reset_action_cooldown(arm)
        except Exception as e:
            msg = f"action cooldown 초기화 실패: {e}"
            critical_failures.append(msg)
            raise TrialInvalid(msg)

        injector.prepare()
        _write_result(result, results_dir)

        result.state = TrialState.PROBING.value
        prober.start()
        result.probe_valid = _wait_for(prober.is_alive, probe_ready_timeout_sec, poll_interval_sec)
        _write_result(result, results_dir)
        if not result.probe_valid:
            raise TrialInvalid("probe가 시작 후 정상 상태에 도달 못 함")

        result.state = TrialState.READY.value
        _register_experiment_context(run_id, scenario, arm, rep, result.t_run_start)
        context_registered = True
        _write_result(result, results_dir)

        # BASELINE(2026-09-18 추가) - probe가 살아있다는 것만 확인하고 바로
        # 주입하면, 콜드스타트 잔재나 초기 불안정 상태가 "주입 전 정상 상태"
        # 로 오인될 수 있다(pilot-load_ramp-native-01-20260918T130111Z에서
        # 실측 확인). get_baseline_status 미구현 어댑터(pod_kill/network_degrade
        # 등, 아직 이 훅 없음)는 baseline_valid=None으로 이 단계를 건너뛰고
        # 기존과 동일하게 바로 INJECTING으로 진행 - is False로만 게이트해야
        # None(미검증)을 실패로 오판하지 않는다.
        result.state = TrialState.BASELINE.value
        _write_result(result, results_dir)
        baseline = _wait_for_baseline(prober, baseline_timeout_sec, poll_interval_sec)
        result.baseline_valid = baseline["valid"]
        result.t_baseline_ready = baseline["t_ready"]
        result.baseline_sample_count = baseline["sample_count"]
        result.baseline_p95 = baseline["p95"]
        result.baseline_availability = baseline["availability"]
        _write_result(result, results_dir)
        if result.baseline_valid is False:
            raise TrialInvalid(f"주입 전 baseline 관찰 조건을 {baseline_timeout_sec}초 내에 충족 못 함")

        # detector 시작(2026-09-18 추가) - baseline 확보 + context 등록(둘 다
        # 위에서 이미 끝남)이 모두 끝난 뒤, chaos 주입 직전에 정확히 한 번
        # 호출한다(지시). baseline 관찰 도중에는 detector 프로세스 자체가
        # 존재하지 않아야 그 구간의 신호·조치가 원천 차단된다.
        if detector is not None:
            detector.start()

        result.state = TrialState.INJECTING.value
        result.t_injection_request = _now()  # injector.inject() 호출 직전 - 실제 주입 구간의 하한
        result.t_injection = result.t_injection_request  # 폴백값(아래서 observed로 덮어씀)
        injector.inject()
        started = _wait_for(injector.is_started, injection_started_timeout_sec, poll_interval_sec)
        result.injection_valid = started and injector.is_effective()
        if result.injection_valid:
            observed = None
            if injector.get_actual_injection_time is not None:
                precise = injector.get_actual_injection_time()
                if precise:
                    observed = precise
            result.t_injection_observed = observed or result.t_injection_request
            result.t_injection = result.t_injection_observed  # 하위 호환 대표값

            if injector.get_last_seen_present_time is not None:
                result.t_injection_last_seen = injector.get_last_seen_present_time()

            # injection_observation_error_sec: 3단계 우선순위로 계산한다
            # (2026-09-18 정정). 전부 실측 구간이지 poll_interval_sec 같은
            # 임의 설정값은 없다.
            #   1) 어댑터의 get_injection_observation_error_sec() - 어댑터
            #      내부 상태로 직접 계산한 값이라 가장 정밀할 수 있음(기존
            #      pod_kill_adapter.py/load_ramp_adapter.py 구현 그대로 유지).
            #   2) t_injection_last_seen ~ t_injection_observed - 어댑터가
            #      위 훅은 없어도 get_last_seen_present_time()만 구현했다면
            #      run_once() 자신이 계산.
            #   3) t_injection_request ~ t_injection_observed - 살아있음을
            #      한 번도 못 봤을 때(첫 poll에서 이미 사라짐)의 최후 폴백.
            # injection_valid=True인 trial은 이제 이 필드가 절대 None으로
            # 남지 않는다.
            err = (injector.get_injection_observation_error_sec()
                   if injector.get_injection_observation_error_sec is not None else None)
            if err is None:
                obs_dt = datetime.fromisoformat(result.t_injection_observed)
                lower_iso = result.t_injection_last_seen or result.t_injection_request
                lower_dt = datetime.fromisoformat(lower_iso)
                err = (obs_dt - lower_dt).total_seconds()
            result.injection_observation_error_sec = err

            # min_observation_sec 기준점은 반드시 is_effective() 확인 "직후"부터
            # 잡는다(2026-09-17 정정, t_injection_request 도입 후에도 유지) -
            # inject() 호출 전(=t_injection_request)에 잡으면, 실제 효과
            # 확인까지 걸린 대기시간이 최소 관찰시간 바닥을 그만큼 갉아먹는다
            # (효과 확인이 느린 injector일수록 실관찰 시간이 줄어드는 역설).
            injection_monotonic = time.monotonic()
            if prober.notify_injected is not None:
                prober.notify_injected(result.t_injection)
        _write_result(result, results_dir)
        if not result.injection_valid:
            raise TrialInvalid("주입이 시작됐는지/효과가 있었는지 확인 안 됨")

        result.state = TrialState.OBSERVING.value
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if not prober.is_alive():
                result.probe_valid = False
                raise TrialInvalid("probe가 관찰 도중 비정상 종료")
            if detector is not None and not detector.is_alive():
                raise TrialInvalid("detector가 관찰 도중 비정상 종료")
            if injector.is_done() and result.t_injection_end is None:
                result.t_injection_end = _now()
            if not result.target_replaced and injector.get_target_replacement is not None:
                replacement = injector.get_target_replacement()
                if replacement is not None:
                    result.target_replaced = True
                    result.t_target_replaced = replacement.get("replaced_at")
                    pod = replacement.get("replacement_pod")
                    if pod is not None:
                        result.target_replacement_pod_name = pod.get("name")
                        result.target_replacement_pod_uid = pod.get("uid")
            if result.t_slo is None and prober.check_slo_violation():
                precise = prober.get_actual_slo_time() if prober.get_actual_slo_time else None
                result.t_slo = precise or _now()
            # t_slo가 찍히기 전에는 check_recovered()를 묻지 않는다 - 주입
            # 직후 아직 멀쩡한 순간을 "회복됨"으로 오판정하는 걸 막기 위함.
            if result.t_slo is not None and result.t_recovery is None and prober.check_recovered():
                precise = prober.get_actual_recovery_time() if prober.get_actual_recovery_time else None
                result.t_recovery = precise or _now()
            # "위반 없음"(t_slo is None)을 prevented로 조기 종료해도 되는지는
            # evaluable(NOT_EVALUABLE이 아님) + 최소 관측시간 둘 다 필요하다
            # (2026-09-17 정정). 즉발 injector(pod_kill)는 t_injection_end가
            # 주입 직후 바로 찍혀서, 이 게이트가 없으면 probe가 표본을 하나도
            # 못 읽은 채로 prevented가 확정돼버린다 - load_ramp처럼 injector
            # 자체 지속시간이 긴 시나리오는 원래도 이 시점엔 이미 두 조건이
            # 자연히 만족돼 있어 동작이 바뀌지 않는다. POSITIVE 위반 감지
            # (t_slo가 실제로 찍히는 것)는 evaluable 여부와 무관하게 항상
            # 그대로 신뢰한다 - 이 게이트는 "위반이 없었다"는 결론에만 적용.
            evaluable_hook_present = prober.is_slo_evaluable is not None
            evaluable = prober.is_slo_evaluable() if evaluable_hook_present else True
            observed_long_enough = (time.monotonic() - injection_monotonic) >= min_observation_sec
            prevented_confirmed = result.t_slo is None and evaluable and observed_long_enough
            if result.t_injection_end is not None and (prevented_confirmed or result.t_recovery is not None):
                break
            time.sleep(poll_interval_sec)
        else:
            result.outcome = "timeout"

        if result.outcome is None:
            # t_slo가 끝까지 안 찍혔으면 prevented 후보(최종 판정은
            # collect_metrics.py가 같은 시나리오 native 반복과 교차검증해서
            # 확정 - 계약서 §3의 3조건 중 하나는 여기서 알 수 없음). evaluable/
            # evaluable_hook_present는 루프의 마지막 iteration에서 계산된 값
            # 그대로 - t_slo가 None이면(=이 분기로 들어왔으면) 위 루프가 최소
            # 한 번은 돌았으므로 항상 정의돼 있다.
            if result.t_slo is None:
                result.outcome = "prevented"
                # hook 미구현이면 게이트는 통과했어도(하위호환) "검증됨"이
                # 아니라 "검증 안 함"이므로 None - True/False는 어댑터가
                # 실제로 판정한 경우에만 기록한다(2026-09-17 정정).
                result.slo_evaluable_at_exit = evaluable if evaluable_hook_present else None
            else:
                result.outcome = "recovered"

    except HarnessCorrupted as e:
        # injector.prepare()(예: preview 준비 실패 후 자동 rollback까지 실패)
        # 등 try 블록 내부에서 직접 HarnessCorrupted를 던진 경우(2026-09-19
        # 추가) - 이 trial 자체는 invalid_run으로 기록하되, critical_failures에도
        # 반영해 함수 끝(§830 부근)에서 실제로 HarnessCorrupted가 재발생하도록
        # 한다 - _reset_action_cooldown 실패와 동일한 패턴(§6) 재사용.
        result.outcome = "invalid_run"
        result.invalid_reason = str(e)
        critical_failures.append(str(e))
    except TrialInvalid as e:
        result.outcome = "invalid_run"
        result.invalid_reason = str(e)
    except Exception as e:
        result.outcome = "invalid_run"
        result.invalid_reason = f"예외: {type(e).__name__}: {e}"
    finally:
        # preview 준비 진단 정보 회수(2026-09-19 추가) - prepare()가 실패해도
        # (TrialInvalid/HarnessCorrupted) 진단 데이터는 남겨야 하므로 성공/실패
        # 무관하게 여기서 수행한다. 이 정보 하나 때문에 trial 전체가 오염되면
        # 안 되므로(기존 classify_stage와 동일한 이유) 별도 try/except로 감싼다.
        if injector.get_preview_prep_info is not None:
            try:
                prep_info = injector.get_preview_prep_info()
                if prep_info is not None:
                    result.t_preview_prep_start = prep_info.get("t_prep_start")
                    result.t_preview_ready = prep_info.get("t_preview_ready")
                    result.preview_prep_duration_sec = prep_info.get("prep_duration_sec")
                    result.preview_rollback_attempted = prep_info.get("rollback_attempted")
                    result.preview_rollback_ok = prep_info.get("rollback_ok")
            except Exception as e:
                result.notes += f"preview prep 진단 정보 회수 실패(핵심 결과에는 영향 없음): {e} | "

        # t_detection/t_api_request 회수(2026-09-19 추가) - context가
        # clear되기 전에(아래에서 더 나중에 일어남) 반드시 먼저 읽어야 한다.
        # native는 조회 대상 자체가 없어 스킵(계약서 §1 재확인 - recovery-
        # policy·detector·preview 전부 비활성). context_registered=False면
        # (이 arm이 non-native인데도) 애초에 이 trial 몫의 experiment context가
        # recovery-policy에 등록된 적이 없다는 뜻이라 조회 자체를 시도하지
        # 않는다(트래픽 낭비 + 남의 context를 잘못 읽을 위험 방지) - 이미
        # 다른 이유로 invalid_run이 확정돼 있을 것이므로 별도 처리 불필요.
        # "값이 없다"(정말 무탐지)와 "확인 자체가 안 됐다"를 구분해야
        # 하므로(지시), 엔드포인트 응답이 없거나 run_id가 안 맞으면 조용히
        # null로 남기지 않고 이 trial을 명시적으로 invalid_run 처리한다 -
        # 단, 이미 다른 사유로 invalid_run이 확정된 trial의 기존 사유는
        # 덮어쓰지 않는다(더 구체적인 원인을 보존).
        #
        # 같은 응답에서 판정·조치 필드(detected/detection_source/detector/action/
        # decision_outcome/idempotency_key/promotion_verified)도 함께 회수한다
        # (2026-09-19) - 예전엔 이 필드들을 채우는 코드가 없어 항상 기본값이었다.
        # detected는 기본값을 유지하지 않고 권위 상태에서 채운다. 조회 실패·run_id
        # 불일치는 위 timing과 같은 규칙(invalid_run)이다 - 이때 judgment_source는
        # null로 남아 "권위 있는 값이 아님"을 표시한다.
        authoritative_state = None
        if arm != "native" and context_registered:
            try:
                state = _get_experiment_state(arm, settle_sec=state_settle_sec)
                if state is not None and state.get("run_id") == run_id:
                    authoritative_state = state
                    result.t_detection = state.get("t_detection")
                    result.t_decision = state.get("t_decision")  # 정책이 action을 확정한 서버 시각(observe-only 포함)
                    result.t_api_request = state.get("t_api_request")
                    result.t_switch = state.get("t_switch")  # promotion 후 selector 검증이 처음 성공한 서버 시각
                    result.detected = bool(state.get("detected"))
                    result.detection_source = state.get("detection_source")
                    result.detector = state.get("detector")
                    result.action = state.get("action") or "none"
                    result.decision_outcome = state.get("decision_outcome")
                    result.idempotency_key = state.get("idempotency_key")
                    result.promotion_verified = state.get("promotion_verified")
                    result.judgment_source = "live_state"
                    if result.detected and result.decision_outcome is None:
                        result.notes += (
                            f"trial 종료 시점에 판정이 {state_settle_sec}초 안에 확정되지 않음"
                            f"(recovery-policy가 신호를 처리하는 도중) - action/decision_outcome은 미확정 | "
                        )
                elif result.outcome != "invalid_run":
                    result.outcome = "invalid_run"
                    result.invalid_reason = (
                        "recovery-policy에 이 run_id의 experiment context가 없음 - "
                        "t_detection/t_api_request 및 판정·조치 필드 회수 불가"
                    )
                    result.notes += "상태 조회 결과 run_id 불일치 또는 context 없음 | "
            except Exception as e:
                if result.outcome != "invalid_run":
                    result.outcome = "invalid_run"
                    result.invalid_reason = f"recovery-policy 상태(timing) 엔드포인트 조회 실패: {e}"
                result.notes += f"t_detection/t_api_request/판정 필드 회수 실패 - {e} | "

        # stage 분류(2026-09-18 추가) - cleanup()으로 pod가 삭제되기 전,
        # injector가 아직 살아있는 이 시점에 수행한다. classify_stage()
        # 자체가 "예외를 던지면 안 된다"는 계약이지만, 이 보조 정보 하나
        # 때문에 trial 전체가 HarnessCorrupted로 번지면 안 되므로 한 번 더
        # try/except로 감싼다 - 실패해도 result.outcome/t_slo 등 핵심
        # 판정은 이미 위에서 전부 확정된 뒤라 영향 없다.
        if injector.classify_stage is not None:
            try:
                if result.t_slo is not None:
                    result.slo_stage = injector.classify_stage(result.t_slo)
                if result.t_detection is not None:
                    result.detection_stage = injector.classify_stage(result.t_detection)
                if result.t_api_request is not None:
                    result.action_stage = injector.classify_stage(result.t_api_request)
            except Exception as e:
                result.notes += f"stage 분류 실패(핵심 SLO 결과에는 영향 없음): {e} | "

        result.state = TrialState.CLEANING.value
        _write_result(result, results_dir)

        # detector 정리(2026-09-18 추가) - prober/injector보다 먼저 멈춘다.
        # 계속 살아있으면 이후 정리 단계가 진행되는 동안에도 신호를 계속
        # 낼 수 있어(예: 대상이 아직 완전히 안 죽은 상태에서 또 한 번
        # anomaly로 잡힘), 정리 과정 자체를 관찰 대상으로 오염시킬 위험이
        # 가장 크다.
        if detector is not None:
            try:
                detector.stop()
            except Exception as e:
                result.notes += f"detector.stop() 예외(아래 is_alive 재확인으로 최종 판단): {e} | "
            if detector.is_alive():
                msg = "detector.stop() 이후에도 detector가 여전히 살아있음 - 다음 trial의 신호·조치 오염 위험"
                critical_failures.append(msg)
                result.notes += f"CRITICAL: {msg} | "

        try:
            prober.stop()
        except Exception as e:
            result.notes += f"prober.stop() 예외(아래 is_alive 재확인으로 최종 판단): {e} | "
        # stop()이 예외 없이 반환해도 실제로 안 멈췄을 수 있고, 반대로
        # 예외가 나도 이미 죽어있을 수 있다 - is_alive()로 실측한다.
        if prober.is_alive():
            msg = "prober.stop() 이후에도 probe가 여전히 살아있음 - 다음 trial의 부하·지표 오염 위험"
            critical_failures.append(msg)
            result.notes += f"CRITICAL: {msg} | "

        try:
            injector.cleanup()
        except Exception as e:
            msg = f"injector.cleanup() 실패: {e}"
            critical_failures.append(msg)
            result.notes += f"{msg} | "

        if context_registered and not critical_failures:
            _wait_for_quiescence(arm)  # 정리 후 잔여 alert도 가능하면 해소되길 기다림(최선 노력)
            try:
                _clear_experiment_context(run_id, arm)
            except Exception as e:
                msg = f"experiment-run clear 실패: {e}"
                critical_failures.append(msg)
                result.notes += f"{msg} | "

        result.t_run_end = _now()
        result.state = _FINAL_STATE_BY_OUTCOME.get(result.outcome, result.state).value
        _write_result(result, results_dir)

        # 비동기 감사 필드 회수(2026-09-19 추가) - 정책 결과(위에서 이미 확정·기록됨)와
        # 분리된 단계다. t_audit_write/t_audit_push/commit_sha의 원천은 recovery-policy의
        # 기존 outbox/audit 상태이고, Git push 완료를 기다리도록 recovery 실행 경로를
        # 바꾸지 않았다(git_client.enqueue는 파일 기록+큐잉만). 여기서는 cleanup이
        # 끝난 뒤 짧은 bounded wait만 하고, 못 끝나면 audit_status=pending/failed와
        # 사유를 남기고 null을 유지한다 - 어떤 경우에도 outcome/action을 바꾸거나
        # invalid_run/HarnessCorrupted로 번지지 않는다(예외도 삼킴). 못 끝낸 건 나중에
        # reconcile_audit.py로 다시 채울 수 있다. 판정이 있었을 때만(실제로 감사기록이
        # 생겼어야 할 때만) 기다린다 - 없으면 not_applicable로 바로 끝.
        if authoritative_state is not None:
            try:
                if not result.detected:
                    result.audit_status = "not_applicable"
                    result.audit_status_reason = "탐지·판정이 없어 감사기록 대상 아님"
                else:
                    audit, _ = reconcile_audit.wait_for_primary_audit(
                        run_id, result.idempotency_key, result.decision_outcome,
                        timeout_sec=audit_wait_sec, poll_sec=audit_poll_sec, base_url=RECOVERY_POLICY_URL,
                    )
                    result.t_audit_write = audit["t_audit_write"]
                    result.t_audit_push = audit["t_audit_push"]
                    result.commit_sha = audit["commit_sha"]
                    result.audit_status = audit["audit_status"]
                    result.audit_status_reason = audit["audit_status_reason"]
                    result.audit_record_id = audit["audit_record_id"]
                    result.audit_reconciled_at = _now()
            except Exception as e:
                result.audit_status = "pending"
                result.audit_status_reason = f"감사 필드 회수 중 예외(outcome에는 영향 없음): {type(e).__name__}: {e}"
            _write_result(result, results_dir)

    if critical_failures:
        result_path = (results_dir / "pilot" if is_pilot else results_dir) / f"trial-{run_id}.json"
        raise HarnessCorrupted(
            f"{run_id}: {'; '.join(critical_failures)} - 클러스터가 다음 trial을 오염시켰을 수 있음, "
            f"수동 확인 필요. 결과는 {result_path}에 기록됨"
        )
    return result
