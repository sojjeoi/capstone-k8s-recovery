#!/usr/bin/env python3
"""blue_green_prep.prepare_preview_with_rollback()/wait_until_rolled_back() 검증
(2026-09-19 추가 - fixed_threshold pilot 01회에서 preview가 180초 timeout보다
늦게 Ready된 채 방치되고 Rollout이 Paused/Degraded로 남은 사고가 계기).
실클러스터 없이 오프라인으로 돈다 - k8s를 직접 건드리는 함수(bump_template_
annotation/is_paused_pre_promotion/get_blue_green_status/abort_preview/
_replicaset_desired)는 전부 모킹하고 오케스트레이션 로직(언제 rollback을
시도하는지/시도하지 않는지, 어떤 preview를 대상으로 삼는지)만 검증한다."""
import sys
from unittest.mock import patch

sys.stdout.reconfigure(encoding="utf-8")

from blue_green_prep import prepare_preview_with_rollback, wait_until_rolled_back


def test_ready_within_timeout_no_rollback():
    with patch("blue_green_prep.get_blue_green_status", return_value={"active_selector": "stableA", "current_pod_hash": "previewB"}), \
         patch("blue_green_prep.bump_template_annotation"), \
         patch("blue_green_prep.is_paused_pre_promotion", return_value=True), \
         patch("blue_green_prep.abort_preview") as mock_abort:
        r = prepare_preview_with_rollback("vllm-serving", "vllm-serving", timeout=1.0, poll_interval=0.01)
    assert r["ready"] is True
    assert r["t_preview_ready"] is not None
    assert r["prep_duration_sec"] is not None
    assert r["rollback_attempted"] is False
    assert r["external_interference"] is False
    mock_abort.assert_not_called()
    print("OK - 시간 내 Ready면 rollback 시도 안 함")


def test_timeout_triggers_rollback_and_succeeds():
    with patch("blue_green_prep.get_blue_green_status", return_value={"active_selector": "stableA", "current_pod_hash": "previewB"}), \
         patch("blue_green_prep.bump_template_annotation"), \
         patch("blue_green_prep.is_paused_pre_promotion", return_value=False), \
         patch("blue_green_prep.abort_preview") as mock_abort, \
         patch("blue_green_prep.wait_until_rolled_back", return_value=True) as mock_verify:
        r = prepare_preview_with_rollback("vllm-serving", "vllm-serving", timeout=0.05, poll_interval=0.01)
    assert r["ready"] is False
    assert r["external_interference"] is False
    assert r["rollback_attempted"] is True
    assert r["rollback_ok"] is True
    assert r["aborted_pod_hash"] == "previewB"  # bump 직후 읽은 값 - "이번 호출이 만든" preview만 대상
    mock_abort.assert_called_once_with("vllm-serving", "vllm-serving")
    mock_verify.assert_called_once_with("vllm-serving", "vllm-serving", "stableA", "previewB")
    print("OK - timeout이면 이번 호출이 만든 preview(previewB)만 abort 대상으로 삼고 rollback 성공 기록")


def test_timeout_rollback_fails_is_reported():
    with patch("blue_green_prep.get_blue_green_status", return_value={"active_selector": "stableA", "current_pod_hash": "previewB"}), \
         patch("blue_green_prep.bump_template_annotation"), \
         patch("blue_green_prep.is_paused_pre_promotion", return_value=False), \
         patch("blue_green_prep.abort_preview"), \
         patch("blue_green_prep.wait_until_rolled_back", return_value=False):
        r = prepare_preview_with_rollback("vllm-serving", "vllm-serving", timeout=0.05, poll_interval=0.01)
    assert r["ready"] is False
    assert r["rollback_attempted"] is True
    assert r["rollback_ok"] is False
    print("OK - rollback 자체가 실패하면 rollback_ok=False로 명시 기록(호출자가 HarnessCorrupted로 승격할 근거)")


def test_external_interference_skips_rollback_fail_closed():
    """bump 직후 activeSelector가 이미 우리가 알던 값과 다르면(다른 프로세스
    개입 가능성) 무엇이 "우리 preview"인지 특정할 수 없다 - abort를 아예
    시도하지 않아야 한다(이미 존재하던 preview를 함부로 건드리지 않음)."""
    calls = [
        {"active_selector": "stableA", "current_pod_hash": None},  # bump 이전
        {"active_selector": "stableX", "current_pod_hash": "someoneElsePreview"},  # bump 직후 - 예상 밖
    ]
    with patch("blue_green_prep.get_blue_green_status", side_effect=calls), \
         patch("blue_green_prep.bump_template_annotation"), \
         patch("blue_green_prep.is_paused_pre_promotion", return_value=False), \
         patch("blue_green_prep.abort_preview") as mock_abort:
        r = prepare_preview_with_rollback("vllm-serving", "vllm-serving", timeout=0.05, poll_interval=0.01)
    assert r["external_interference"] is True
    assert r["rollback_attempted"] is False
    assert r["rollback_ok"] is None
    mock_abort.assert_not_called()
    print("OK - bump 직후 activeSelector가 예상 밖이면 fail-closed로 abort 시도 안 함")


def test_wait_until_rolled_back_polls_until_converged():
    """timeout 직후 preview가 뒤늦게 Ready/삭제되는 경합 상황 - 첫 poll에서는
    아직 안 끝났어도(RS replica 잔존) 이후 poll에서 수렴하면 True를 반환해야
    한다(한 번만 확인하고 포기하면 안 됨)."""
    status_calls = [
        {"active_selector": "stableA", "current_pod_hash": "previewB"},  # 1차: 아직 selector 원복 확인은 됨
        {"active_selector": "stableA", "current_pod_hash": "previewB"},  # 2차
    ]
    rs_desired_calls = [1, 0]  # 1차: 아직 안 줄어듦(경합), 2차: 수렴
    with patch("blue_green_prep.get_blue_green_status", side_effect=status_calls), \
         patch("blue_green_prep._replicaset_desired", side_effect=rs_desired_calls):
        ok = wait_until_rolled_back("vllm-serving", "vllm-serving", "stableA", "previewB",
                                     timeout=1.0, poll_interval=0.01)
    assert ok is True
    print("OK - 첫 poll에서 안 끝나도 이후 수렴하면 rollback 검증 성공(경합 상황 허용)")


def test_wait_until_rolled_back_gives_up_after_timeout():
    with patch("blue_green_prep.get_blue_green_status", return_value={"active_selector": "stableA", "current_pod_hash": "previewB"}), \
         patch("blue_green_prep._replicaset_desired", return_value=1):  # 계속 안 줄어듦
        ok = wait_until_rolled_back("vllm-serving", "vllm-serving", "stableA", "previewB",
                                     timeout=0.03, poll_interval=0.01)
    assert ok is False
    print("OK - timeout 내내 수렴 안 되면 False(호출자가 HarnessCorrupted로 승격)")


if __name__ == "__main__":
    test_ready_within_timeout_no_rollback()
    test_timeout_triggers_rollback_and_succeeds()
    test_timeout_rollback_fails_is_reported()
    test_external_interference_skips_rollback_fail_closed()
    test_wait_until_rolled_back_polls_until_converged()
    test_wait_until_rolled_back_gives_up_after_timeout()
    print("전체 통과")
