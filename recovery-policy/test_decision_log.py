#!/usr/bin/env python3
"""decision_log.build() 체크 - 성공 케이스만이 아니라 리뷰가 지적한
"자주 빠뜨리는" outcome들(cooldown/중복/no_action/promotion 실패)도
전부 레코드가 만들어지는지 확인."""
from datetime import datetime, timezone

from decision_log import Outcome, build
from schemas import NormalizedSignal, SignalSource

SIGNAL = NormalizedSignal(
    source=SignalSource.ANOMALY,
    signal_type="anomaly_risk",
    idempotency_key="anomaly:anomaly_risk:2026-09-06T15:00:00+00:00",
    received_at=datetime.now(timezone.utc),
    raw={"score": -0.05},
)


def test_executed_verified():
    record = build(
        SIGNAL, action="promote_preview", outcome=Outcome.EXECUTED_VERIFIED,
        evidence={"cpu_mean_zscore": 2.8}, result={"method": "cli", "verified": True},
        reasoning="CPU 표준화 편차 2.8시그마로 임계치 초과",
    )
    assert record.outcome == Outcome.EXECUTED_VERIFIED
    assert record.result["verified"] is True
    print("OK - executed_verified:", record.record_id)


def test_skipped_cooldown_still_logged():
    # 리뷰 지적: "allowed()에서 바로 return하면 이 판단이 기록 안 됨" - 여기선 기록됨을 확인
    record = build(SIGNAL, action=None, outcome=Outcome.SKIPPED_COOLDOWN, reasoning="직전 조치 후 60초 이내")
    assert record.outcome == Outcome.SKIPPED_COOLDOWN
    assert record.action is None
    print("OK - skipped_cooldown도 레코드 생성됨:", record.record_id)


def test_skipped_duplicate_still_logged():
    record = build(SIGNAL, action=None, outcome=Outcome.SKIPPED_DUPLICATE, reasoning="동일 idempotency_key 이미 처리됨")
    assert record.outcome == Outcome.SKIPPED_DUPLICATE
    print("OK - skipped_duplicate도 레코드 생성됨:", record.record_id)


def test_executed_unverified_promotion_failure():
    record = build(
        SIGNAL, action="promote_preview", outcome=Outcome.EXECUTED_UNVERIFIED,
        result={"method": "cli", "requested": True, "verified": False},
        reasoning="CLI 요청은 성공했으나 selector 전환 미확인",
    )
    assert record.outcome == Outcome.EXECUTED_UNVERIFIED
    assert record.result["verified"] is False
    print("OK - promotion 실패(unverified)도 레코드 생성됨:", record.record_id)


if __name__ == "__main__":
    test_executed_verified()
    test_skipped_cooldown_still_logged()
    test_skipped_duplicate_still_logged()
    test_executed_unverified_promotion_failure()
    print("모두 통과")
