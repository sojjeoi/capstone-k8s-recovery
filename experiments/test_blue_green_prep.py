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

from blue_green_prep import cleanup_unpromoted_preview, prepare_preview_with_rollback, wait_until_rolled_back


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


def test_prepare_preview_stale_hash_after_bump_is_not_trusted():
    """2026-09-20 실측 발견(anomaly-detection/v3/collect_session.py
    qualification 1~2차 시도에서 재현, kubectl로 직접 대조 확인) - bump
    직후 곧바로 읽은 current_pod_hash가 직전 시도의 잔여값(컨트롤러
    reconcile 미완료)일 수 있다. Ready 확정 시점에 다시 읽은 값만
    created_pod_hash로 써야 한다."""
    calls = [
        {"active_selector": "stableA", "current_pod_hash": "leftoverFromPriorAttempt"},  # before
        {"active_selector": "stableA", "current_pod_hash": "leftoverFromPriorAttempt"},  # bump 직후(reconcile 전 - stale)
        {"active_selector": "stableA", "current_pod_hash": "realNewPreview"},  # Ready 확정 시점(reconcile 끝남)
    ]
    with patch("blue_green_prep.get_blue_green_status", side_effect=calls), \
         patch("blue_green_prep.bump_template_annotation"), \
         patch("blue_green_prep.is_paused_pre_promotion", return_value=True), \
         patch("blue_green_prep.abort_preview") as mock_abort:
        r = prepare_preview_with_rollback("vllm-serving", "vllm-serving", timeout=1.0, poll_interval=0.01)
    assert r["ready"] is True
    assert r["created_pod_hash"] == "realNewPreview", "bump 직후 값(stale) 말고 Ready 확정 시점 값을 써야 함"
    mock_abort.assert_not_called()
    print("OK - bump 직후 stale current_pod_hash를 신뢰하지 않고 Ready 확정 시점에 다시 읽음")


def test_prepare_preview_timeout_rereads_hash_before_abort():
    """timeout 경로도 마찬가지 - abort 직전에 다시 읽은 hash를 aborted_pod_hash로
    써야 한다(stale 값으로 엉뚱한 revision을 대상으로 삼으면 안 됨)."""
    calls_iter = iter([
        {"active_selector": "stableA", "current_pod_hash": "leftover"},  # before
        {"active_selector": "stableA", "current_pod_hash": "leftover"},  # bump 직후
    ])

    def side_effect(*_a, **_kw):
        try:
            return next(calls_iter)
        except StopIteration:
            return {"active_selector": "stableA", "current_pod_hash": "reconciledLater"}

    with patch("blue_green_prep.get_blue_green_status", side_effect=side_effect), \
         patch("blue_green_prep.bump_template_annotation"), \
         patch("blue_green_prep.is_paused_pre_promotion", return_value=False), \
         patch("blue_green_prep.abort_preview") as mock_abort, \
         patch("blue_green_prep.wait_until_rolled_back", return_value=True) as mock_verify:
        r = prepare_preview_with_rollback("vllm-serving", "vllm-serving", timeout=0.03, poll_interval=0.01)
    assert r["aborted_pod_hash"] == "reconciledLater"
    assert r["created_pod_hash"] == "reconciledLater"
    mock_verify.assert_called_once_with("vllm-serving", "vllm-serving", "stableA", "reconciledLater")
    mock_abort.assert_called_once_with("vllm-serving", "vllm-serving")
    print("OK - timeout 시에도 abort 직전 다시 읽은 hash를 씀(stale 값으로 잘못된 preview를 기록 안 함)")


def test_wait_until_rolled_back_polls_until_converged():
    """timeout 직후 preview가 뒤늦게 Ready/삭제되는 경합 상황 - 첫 poll에서는
    아직 안 끝났어도(RS replica 잔존) 이후 poll에서 수렴하면 True를 반환해야
    한다(한 번만 확인하고 포기하면 안 됨)."""
    status_calls = [
        {"active_selector": "stableA", "current_pod_hash": "previewB"},  # 1차: 아직 selector 원복 확인은 됨
        {"active_selector": "stableA", "current_pod_hash": "previewB"},  # 2차
    ]
    rs_desired_calls = [1, 0]  # 1차: 아직 안 줄어듦(경합), 2차: 수렴
    rs_current_calls = [1, 0]  # §101 - desired와 함께 current(실제 떠 있는 pod 수)도 0이어야 완료
    with patch("blue_green_prep.get_blue_green_status", side_effect=status_calls), \
         patch("blue_green_prep._replicaset_desired", side_effect=rs_desired_calls), \
         patch("blue_green_prep._replicaset_current", side_effect=rs_current_calls):
        ok = wait_until_rolled_back("vllm-serving", "vllm-serving", "stableA", "previewB",
                                     timeout=1.0, poll_interval=0.01)
    assert ok is True
    print("OK - 첫 poll에서 안 끝나도 이후 수렴하면 rollback 검증 성공(경합 상황 허용)")


def test_wait_until_rolled_back_gives_up_after_timeout():
    with patch("blue_green_prep.get_blue_green_status", return_value={"active_selector": "stableA", "current_pod_hash": "previewB"}), \
         patch("blue_green_prep._replicaset_desired", return_value=1), \
         patch("blue_green_prep._replicaset_current", return_value=1):  # 계속 안 줄어듦
        ok = wait_until_rolled_back("vllm-serving", "vllm-serving", "stableA", "previewB",
                                     timeout=0.03, poll_interval=0.01)
    assert ok is False
    print("OK - timeout 내내 수렴 안 되면 False(호출자가 HarnessCorrupted로 승격)")


def test_wait_until_rolled_back_completion_requires_current_zero_too():
    """§101(2026-09-23, pod_kill-proposed-01-mainexp-v1 실사고 계기) - desired
    (spec.replicas)만 0이고 current(status.replicas, 실제 아직 떠 있는 pod
    수)는 아직 1이면(pod가 Terminating 중) 완료로 오판하면 안 된다 - 예전
    코드는 desired만 봐서 이 상태를 "rollback 완료"로 잘못 판정할 수 있었다."""
    with patch("blue_green_prep.get_blue_green_status", return_value={"active_selector": "stableA", "current_pod_hash": "previewB"}), \
         patch("blue_green_prep._replicaset_desired", return_value=0), \
         patch("blue_green_prep._replicaset_current", return_value=1):  # desired는 내려갔지만 pod가 아직 안 없어짐
        ok = wait_until_rolled_back("vllm-serving", "vllm-serving", "stableA", "previewB",
                                     timeout=0.03, poll_interval=0.01)
    assert ok is False
    print("OK - desired=0이어도 current(실제 pod 수)가 아직 0이 아니면 완료로 오판하지 않음")


def test_wait_until_rolled_back_classifies_converging_vs_stuck():
    """§101 - 지연된 정상 수렴(converging: desired가 최소 한 번은 0으로
    내려가는 진행을 관측)과 영구 잔여(stuck: 그런 진행이 전혀 없음)를
    구분한다 - 둘 다 timeout 안엔 rollback을 완료 못 했다는 점은 같지만
    심각도가 다르다(전자는 "조금 더 걸릴 뿐", 후자는 "abort 자체가
    반영조차 안 됐을 수 있음")."""
    with patch("blue_green_prep.get_blue_green_status", return_value={"active_selector": "stableA", "current_pod_hash": "previewB"}), \
         patch("blue_green_prep._replicaset_desired", side_effect=[0, 0]), \
         patch("blue_green_prep._replicaset_current", side_effect=[1, 1]):  # desired는 내려갔지만(진행 중) current가 안 따라옴
        converging = wait_until_rolled_back("vllm-serving", "vllm-serving", "stableA", "previewB",
                                             timeout=0.02, poll_interval=0.01, return_details=True)
    assert converging["rolled_back"] is False
    assert converging["classification"] == "converging"

    with patch("blue_green_prep.get_blue_green_status", return_value={"active_selector": "stableA", "current_pod_hash": "previewB"}), \
         patch("blue_green_prep._replicaset_desired", return_value=1), \
         patch("blue_green_prep._replicaset_current", return_value=1):  # desired조차 한 번도 안 내려감
        stuck = wait_until_rolled_back("vllm-serving", "vllm-serving", "stableA", "previewB",
                                        timeout=0.02, poll_interval=0.01, return_details=True)
    assert stuck["rolled_back"] is False
    assert stuck["classification"] == "stuck"
    print("OK - desired가 한 번이라도 0을 찍었으면 converging, 전혀 안 그랬으면 stuck으로 구분")


def test_wait_until_rolled_back_default_return_type_unchanged():
    """return_details 기본값(False)에서는 기존 호출부(blue_green_prep.py
    2곳, run_all_scenarios.py, collect_session.py, aba_diagnostic.py)가
    전부 그대로 기대하는 순수 bool을 반환해야 한다 - dict가 섞여 나오면
    `if wait_until_rolled_back(...):` 같은 기존 호출부가 전부 잘못된
    truthy 판정(빈 dict가 아닌 이상 항상 True)을 하게 된다."""
    with patch("blue_green_prep.get_blue_green_status", return_value={"active_selector": "stableA", "current_pod_hash": "previewB"}), \
         patch("blue_green_prep._replicaset_desired", return_value=0), \
         patch("blue_green_prep._replicaset_current", return_value=0):
        ok = wait_until_rolled_back("vllm-serving", "vllm-serving", "stableA", "previewB",
                                     timeout=1.0, poll_interval=0.01)
    assert ok is True and isinstance(ok, bool)
    print("OK - return_details 생략 시 기존과 동일하게 순수 bool 반환")


def test_cleanup_unpromoted_preview_aborts_when_not_promoted():
    # 2026-09-19 추가 - fixed_threshold pilot 재실행에서 실측 발견: preview
    # 준비는 성공했는데 detector가 promote를 안 하면 아무도 안 치워서
    # Rollout이 2-revision으로 방치됐다. activeSelector가 여전히 준비 전
    # 값이면(=promote 안 됨) 우리가 만든 pod_hash만 abort 대상으로 삼는다.
    prep_info = {"ready": True, "pre_prepare_active_selector": "stableA", "created_pod_hash": "previewB"}
    with patch("blue_green_prep.get_blue_green_status", return_value={"active_selector": "stableA", "current_pod_hash": "previewB"}), \
         patch("blue_green_prep.abort_preview") as mock_abort, \
         patch("blue_green_prep.wait_until_rolled_back", return_value=True) as mock_verify:
        result = cleanup_unpromoted_preview(prep_info, "vllm-serving", "vllm-serving")
    assert result is True
    mock_abort.assert_called_once_with("vllm-serving", "vllm-serving")
    mock_verify.assert_called_once_with("vllm-serving", "vllm-serving", "stableA", "previewB")
    print("OK - promote 안 된 preview는 trial 종료 시 자동 abort됨")


def test_cleanup_unpromoted_preview_skips_when_promoted():
    # activeSelector가 우리 pod_hash로 바뀌어 있으면(=실제로 promote됨)
    # 그건 이제 active이므로 절대 건드리면 안 된다.
    prep_info = {"ready": True, "pre_prepare_active_selector": "stableA", "created_pod_hash": "previewB"}
    with patch("blue_green_prep.get_blue_green_status", return_value={"active_selector": "previewB", "current_pod_hash": "previewB"}), \
         patch("blue_green_prep.abort_preview") as mock_abort:
        result = cleanup_unpromoted_preview(prep_info, "vllm-serving", "vllm-serving")
    assert result is None
    mock_abort.assert_not_called()
    print("OK - promote된 preview(이제 active)는 손대지 않음")


def test_cleanup_unpromoted_preview_noop_when_prep_none_or_not_ready():
    with patch("blue_green_prep.abort_preview") as mock_abort:
        assert cleanup_unpromoted_preview(None, "vllm-serving", "vllm-serving") is None
        assert cleanup_unpromoted_preview({"ready": False}, "vllm-serving", "vllm-serving") is None
    mock_abort.assert_not_called()
    print("OK - preview 자체가 없거나(native) 준비 실패였으면(이미 timeout-rollback이 처리) 정리 스킵")


def test_cleanup_unpromoted_preview_fail_closed_on_unexpected_pod_hash():
    # 우리가 만든 pod_hash와 현재 currentPodHash가 다르면(우리 이후 다른
    # 변경이 있었을 가능성) 무엇을 지울지 확신할 수 없으므로 건드리지 않는다.
    prep_info = {"ready": True, "pre_prepare_active_selector": "stableA", "created_pod_hash": "previewB"}
    with patch("blue_green_prep.get_blue_green_status", return_value={"active_selector": "stableA", "current_pod_hash": "someoneElse"}), \
         patch("blue_green_prep.abort_preview") as mock_abort:
        result = cleanup_unpromoted_preview(prep_info, "vllm-serving", "vllm-serving")
    assert result is None
    mock_abort.assert_not_called()
    print("OK - currentPodHash가 예상과 다르면 fail-closed로 정리 스킵")


if __name__ == "__main__":
    test_ready_within_timeout_no_rollback()
    test_timeout_triggers_rollback_and_succeeds()
    test_timeout_rollback_fails_is_reported()
    test_external_interference_skips_rollback_fail_closed()
    test_prepare_preview_stale_hash_after_bump_is_not_trusted()
    test_prepare_preview_timeout_rereads_hash_before_abort()
    test_wait_until_rolled_back_polls_until_converged()
    test_wait_until_rolled_back_gives_up_after_timeout()
    test_cleanup_unpromoted_preview_aborts_when_not_promoted()
    test_cleanup_unpromoted_preview_skips_when_promoted()
    test_cleanup_unpromoted_preview_noop_when_prep_none_or_not_ready()
    test_cleanup_unpromoted_preview_fail_closed_on_unexpected_pod_hash()
    print("전체 통과")
