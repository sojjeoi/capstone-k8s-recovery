#!/usr/bin/env python3
"""rollouts_client.promote()의 verified_at(2026-09-19 추가) 검증 - Phase 8 t_switch의 원천이라
"active selector 검증이 처음 성공한 시각"이 정확히 그 순간에 찍히고, 검증에 실패하거나 승격할 게
없었으면 찍히지 않는지 본다. 실제 K8s 없이 selector 조회·CLI 호출·sleep을 전부 가짜로 대체한다."""
import sys
from datetime import datetime, timezone
from unittest.mock import patch

sys.stdout.reconfigure(encoding="utf-8")

import rollouts_client

PREVIEW = {"app": "vllm-serving", "rollouts-pod-template-hash": "newhash"}
OLD_ACTIVE = {"app": "vllm-serving", "rollouts-pod-template-hash": "oldhash"}


def _selector_sequence(active_after_polls):
    """get_service_selector 가짜: 처음 두 호출은 preview/active(승격 전), 이후 verify 루프의 active
    조회 결과를 active_after_polls 순서대로 돌려준다(마지막 값 반복)."""
    calls = {"n": 0}
    later = list(active_after_polls)

    def fake(service_name, namespace):
        calls["n"] += 1
        if calls["n"] == 1:
            return PREVIEW
        if calls["n"] == 2:
            return OLD_ACTIVE
        return later.pop(0) if len(later) > 1 else later[0]

    return fake


def test_verified_at_is_stamped_when_selector_first_matches_preview():
    before = datetime.now(timezone.utc)
    with patch("rollouts_client.get_service_selector", side_effect=_selector_sequence([OLD_ACTIVE, PREVIEW])), \
         patch("rollouts_client.promote_via_cli", return_value={"method": "cli", "requested": True}), \
         patch("rollouts_client.time.sleep"):
        result = rollouts_client.promote("vllm-serving", "vllm-serving", verify_timeout=5.0, poll_interval=0.0)
    after = datetime.now(timezone.utc)
    assert result["verified"] is True
    verified_at = datetime.fromisoformat(result["verified_at"])
    assert verified_at.tzinfo is not None and before <= verified_at <= after
    print("OK - 검증 성공 시 verified_at(UTC)이 promote() 호출 구간 안에서 찍힘")


def test_verified_at_absent_when_verification_fails():
    with patch("rollouts_client.get_service_selector", side_effect=_selector_sequence([OLD_ACTIVE])), \
         patch("rollouts_client.promote_via_cli", return_value={"method": "cli", "requested": True}), \
         patch("rollouts_client.time.sleep"):
        result = rollouts_client.promote("vllm-serving", "vllm-serving", verify_timeout=0.05, poll_interval=0.0)
    assert result["verified"] is False
    assert "verified_at" not in result, "검증에 실패했는데 전환 시각이 있으면 안 됨"
    print("OK - 검증 실패 시 verified_at 없음")


def test_verified_at_absent_when_nothing_to_promote():
    with patch("rollouts_client.get_service_selector", return_value=PREVIEW), \
         patch("rollouts_client.promote_via_cli") as mock_cli:
        result = rollouts_client.promote("vllm-serving", "vllm-serving")
    assert result["verified"] is False and result["method"] == "none"
    assert "verified_at" not in result
    mock_cli.assert_not_called()
    print("OK - active==preview(승격할 게 없음)이면 promote 시도·verified_at 모두 없음")


if __name__ == "__main__":
    test_verified_at_is_stamped_when_selector_first_matches_preview()
    test_verified_at_absent_when_verification_fails()
    test_verified_at_absent_when_nothing_to_promote()
    print("모두 통과")
