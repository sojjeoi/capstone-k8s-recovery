#!/usr/bin/env python3
"""schemas.py 변환 함수 체크 - 실제 score_server.py/Alertmanager 페이로드
모양으로 넣어보고 NormalizedSignal이 맞게 나오는지 확인."""
from schemas import (
    AlertmanagerWebhookRequest,
    AnomalySignalRequest,
    normalize_alertmanager_webhook,
    normalize_anomaly_signal,
)


def test_anomaly_signal():
    # score_server.py의 post_to_recovery_policy()가 실제로 보내는 것과 동일한 형태
    req = AnomalySignalRequest(signal_type="anomaly_risk", score=-0.0531, timestamp="2026-09-06T15:00:00+00:00")
    sig = normalize_anomaly_signal(req)
    assert sig.source == "anomaly"
    assert sig.signal_type == "anomaly_risk"
    assert sig.idempotency_key == "anomaly:anomaly_risk:2026-09-06T15:00:00+00:00"
    # 같은 요청이 재전송돼도 key가 똑같이 나와야(retry-safe) idempotency가 의미 있음
    assert normalize_anomaly_signal(req).idempotency_key == sig.idempotency_key
    print("OK - anomaly signal:", sig.idempotency_key)


def test_alertmanager_webhook_filters_resolved():
    # 발견 5의 실제 alert 이름(VLLMTargetDown) + 하나는 firing, 하나는 resolved
    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "VLLMTargetDown", "severity": "critical"},
                "annotations": {"summary": "vLLM Serving 타겟 다운"},
                "startsAt": "2026-09-06T15:00:00Z",
                "fingerprint": "abc123",
            },
            {
                "status": "resolved",
                "labels": {"alertname": "VLLMTargetMissing"},
                "startsAt": "2026-09-06T14:00:00Z",
                "endsAt": "2026-09-06T14:05:00Z",
                "fingerprint": "def456",
            },
        ],
    }
    req = AlertmanagerWebhookRequest.model_validate(payload)
    signals = normalize_alertmanager_webhook(req)

    assert len(signals) == 1, "resolved alert가 걸러지지 않음"
    assert signals[0].signal_type == "VLLMTargetDown"
    assert signals[0].idempotency_key == "abc123:2026-09-06T15:00:00+00:00"
    print("OK - alertmanager webhook: firing 1개만 통과,", signals[0].idempotency_key)


if __name__ == "__main__":
    test_anomaly_signal()
    test_alertmanager_webhook_filters_resolved()
    print("모두 통과")
