#!/usr/bin/env python3
"""BlueGreen 콜드스타트(preview 생성 -> Ready -> 안정성 관찰) 판정에 쓰이는
순수 로직만 모은다. 실클러스터를 직접 두드리는 폴링 루프(1회성 관찰
스크립트)는 이 모듈의 함수만 불러 쓰고, 오프라인 테스트 대상은 여기
함수들로 한정한다.

2026-09-18 HEADROOM-COLDSTART-01(재분류 후 HEADROOM-MIGRATION-PILOT-01)
1차 시도에서 실제로 난 버그를 계기로 분리했다 - 정상적인 'Unhealthy:
Startup probe failed'(모델 로딩 중 반복 실패가 startupProbe
failureThreshold 설계 의도다, gitops/apps/vllm-serving/rollout.yaml 참고)
를 위험 이벤트로 오판해 관찰 시작 1.3초 만에 조기 abort했다 - 실제
클러스터는 전혀 이상 없었다(직접 재확인함). 진짜 재시작/스케줄 실패를
유발하는 신호만 위험으로 본다."""
from datetime import datetime

RISKY_EVENT_KEYWORDS = (
    "oomkill", "evicted", "failedmount", "failedscheduling",
    "crashloopbackoff", "back-off restarting failed container",
)


def is_risky_event(reason: str, message: str) -> bool:
    """진짜 재시작/스케줄 실패를 유발하는 이벤트만 위험으로 본다.
    'Unhealthy: Startup probe failed'류는 절대 여기 안 걸려야 한다 -
    startupProbe가 failureThreshold(예: 90)까지 반복 실패하는 건 모델
    로딩 시간을 벌기 위한 정상 설계이고, 애초에 startupProbe가 통과하기
    전에는 liveness/readinessProbe 자체가 실행되지 않아 이 실패가 재시작을
    유발하지도 않는다(K8s 자체 동작) - 진짜 위험 신호는 restart_increased()
    로 따로 잡는다."""
    blob = f"{reason} {message}".lower()
    return any(k in blob for k in RISKY_EVENT_KEYWORDS)


def restart_increased(initial_restart_count: int, current_restart_count: int) -> bool:
    """실제 컨테이너 재시작(재시작 카운트 증가)만 위험으로 본다."""
    return current_restart_count > initial_restart_count


def compute_effective_start_mono(applied_at: datetime, now_utc: datetime, now_mono: float) -> float:
    """모니터링 도중 관찰이 끊겼다가 재개돼도(예: 버그 수정 후 재실행) 총
    시작시간을 이 프로세스가 재실행된 시점이 아니라 원래 apply 시각 기준
    으로 계산하기 위한 monotonic 기준점을 만든다. 실제 콜드스타트는 관찰이
    끊긴 동안에도 계속 진행 중이었으므로, 재실행 시점을 0으로 잡으면 총
    시작시간이 실제보다 짧게 기록된다."""
    already_elapsed_sec = (now_utc - applied_at).total_seconds()
    return now_mono - already_elapsed_sec
