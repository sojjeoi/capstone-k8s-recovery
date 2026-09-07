"""두 입력 경로(예측/반응)의 서로 다른 payload 형식을 공통 NormalizedSignal로
변환한다. policy.py/safety.py/decision_log.py는 전부 NormalizedSignal만 보고
원본 포맷(anomaly json vs Alertmanager webhook)은 몰라도 되게 하기 위함
(피드백 지적 ②).

main.py의 실제 라우트 연결은 5단계에서 - 여기서는 스키마와 순수 변환 함수만.
"""
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class SignalSource(str, Enum):
    ANOMALY = "anomaly"
    ALERTMANAGER = "alertmanager"


class NormalizedSignal(BaseModel):
    source: SignalSource
    signal_type: str  # "anomaly_risk" | "VLLMTargetDown" | "VLLMTargetMissing" | 그 외(모르는 값도 통과시킴 - policy.py가 판단)
    idempotency_key: str
    received_at: datetime
    raw: dict  # 원본 payload 그대로 보존 - 감사 기록(decision_log.py)이 참고


# ---- 예측 경로: score_server.py가 보내는 형식 ----

class AnomalySignalRequest(BaseModel):
    signal_type: str
    score: float
    timestamp: datetime
    experiment_run_id: Optional[str] = None  # Phase 8 오케스트레이터가 넣어줄 수 있게 옵션 (지금 score_server.py는 안 보냄)


def normalize_anomaly_signal(req: AnomalySignalRequest) -> NormalizedSignal:
    """experiment_run_id가 있으면 그걸로, 없으면 signal_type+timestamp로 idempotency
    key를 만든다. timestamp는 score_server.py가 신호 생성 시점에 찍는 값이라, 같은
    요청이 네트워크 문제로 재전송돼도 값이 그대로라 결정적(retry-safe)이다 - 매번
    새 UUID를 만들면 재전송을 다른 신호로 착각하게 되므로 그렇게 하지 않는다."""
    key = (
        f"{req.experiment_run_id}:{req.signal_type}"
        if req.experiment_run_id
        else f"anomaly:{req.signal_type}:{req.timestamp.isoformat()}"
    )
    return NormalizedSignal(
        source=SignalSource.ANOMALY,
        signal_type=req.signal_type,
        idempotency_key=key,
        received_at=req.timestamp,
        raw=req.model_dump(mode="json"),
    )


# ---- 반응 경로: Alertmanager 표준 webhook 형식 ----
# https://prometheus.io/docs/alerting/latest/configuration/#webhook_config 의
# 표준 페이로드 - 한 요청에 alerts가 여러 개 배치로 올 수 있다.

class AlertmanagerAlert(BaseModel):
    status: str  # "firing" | "resolved"
    labels: dict
    annotations: dict = {}
    startsAt: datetime
    endsAt: Optional[datetime] = None
    fingerprint: str


class AlertmanagerWebhookRequest(BaseModel):
    status: str
    alerts: list[AlertmanagerAlert]


def normalize_alertmanager_alert(alert: AlertmanagerAlert) -> NormalizedSignal:
    """fingerprint+startsAt으로 idempotency key 구성(피드백이 제안한 방식) -
    Alertmanager 자체가 알림 하나하나에 fingerprint를 고정으로 부여하므로 결정적."""
    return NormalizedSignal(
        source=SignalSource.ALERTMANAGER,
        signal_type=alert.labels.get("alertname", "unknown"),
        idempotency_key=f"{alert.fingerprint}:{alert.startsAt.isoformat()}",
        received_at=alert.startsAt,
        raw=alert.model_dump(mode="json"),
    )


def normalize_alertmanager_webhook(req: AlertmanagerWebhookRequest) -> list[NormalizedSignal]:
    """firing 상태인 alert만 NormalizedSignal로 변환한다. resolved는 "장애가
    해소됐다"는 정보 알림이지 처치 대상이 아니므로 여기서 걸러낸다(피드백 지적 ②)."""
    return [normalize_alertmanager_alert(a) for a in req.alerts if a.status == "firing"]
