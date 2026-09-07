#!/usr/bin/env python3
"""policy.decide()의 신호 타입 x preview 준비 여부 조합을 전부 확인.
실제 alert 이름(VLLMTargetDown/VLLMTargetMissing)과 rule-out, 미정의 신호
케이스까지 포함."""
import sys

sys.stdout.reconfigure(encoding="utf-8")

import policy
from schemas import NormalizedSignal, SignalSource

TEMPLATE = dict(source=SignalSource.ANOMALY, received_at="2026-09-07T00:00:00+00:00", raw={})


def _signal(signal_type: str, key: str) -> NormalizedSignal:
    return NormalizedSignal(signal_type=signal_type, idempotency_key=key, **TEMPLATE)


def test_anomaly_risk_promotes_when_preview_ready():
    d = policy.decide(_signal("anomaly_risk", "k1"), policy.PolicyContext(preview_ready=True))
    assert d.action == policy.ACTION_PROMOTE_PREVIEW
    print("OK - anomaly_risk + preview_ready ->", d.action)


def test_anomaly_risk_observes_when_preview_not_ready():
    d = policy.decide(_signal("anomaly_risk", "k2"), policy.PolicyContext(preview_ready=False))
    assert d.action == policy.ACTION_OBSERVE_ONLY
    print("OK - anomaly_risk + preview 없음 ->", d.action)


def test_anomaly_risk_rule_out_blocks_action():
    d = policy.decide(
        _signal("anomaly_risk", "k3"),
        policy.PolicyContext(preview_ready=True, has_contradicting_evidence=True),
    )
    assert d.action is None
    print("OK - anomaly_risk + 반대증거 -> 조치 없음")


def test_target_down_promotes_when_preview_ready():
    d = policy.decide(_signal("VLLMTargetDown", "k4"), policy.PolicyContext(preview_ready=True))
    assert d.action == policy.ACTION_PROMOTE_PREVIEW
    print("OK - VLLMTargetDown + preview_ready ->", d.action)


def test_target_down_observes_when_preview_not_ready():
    d = policy.decide(_signal("VLLMTargetDown", "k5"), policy.PolicyContext(preview_ready=False))
    assert d.action == policy.ACTION_OBSERVE_ONLY
    print("OK - VLLMTargetDown + preview 없음 ->", d.action)


def test_target_missing_promotes_when_preview_ready():
    d = policy.decide(_signal("VLLMTargetMissing", "k6"), policy.PolicyContext(preview_ready=True))
    assert d.action == policy.ACTION_PROMOTE_PREVIEW
    print("OK - VLLMTargetMissing + preview_ready ->", d.action)


def test_target_missing_observes_when_preview_not_ready():
    d = policy.decide(_signal("VLLMTargetMissing", "k7"), policy.PolicyContext(preview_ready=False))
    assert d.action == policy.ACTION_OBSERVE_ONLY
    print("OK - VLLMTargetMissing + preview 없음 ->", d.action)


def test_unknown_signal_no_action():
    d = policy.decide(_signal("SomeRandomAlert", "k8"), policy.PolicyContext())
    assert d.action is None
    print("OK - 미정의 신호 ->", d.reasoning)


if __name__ == "__main__":
    test_anomaly_risk_promotes_when_preview_ready()
    test_anomaly_risk_observes_when_preview_not_ready()
    test_anomaly_risk_rule_out_blocks_action()
    test_target_down_promotes_when_preview_ready()
    test_target_down_observes_when_preview_not_ready()
    test_target_missing_promotes_when_preview_ready()
    test_target_missing_observes_when_preview_not_ready()
    test_unknown_signal_no_action()
    print("모두 통과")
