#!/usr/bin/env python3
"""collect_session.py 검증 - judge_session_exclusion()(순수 함수)만 대상.
세션 수집 자체의 라이브 오케스트레이션(preview 생성/promotion 감시/ramp
실행)은 explore_ramp_intensity.run_candidate()와 같은 이유로 오프라인
테스트 대상이 아니다(실클러스터·Rollout·Prometheus 의존)."""
import subprocess
import sys
from unittest.mock import patch

sys.stdout.reconfigure(encoding="utf-8")

from collect_session import judge_session_exclusion, run_candidate_with_retry, verify_and_force_cleanup

HEALTHY_NODE = {"node_ok": True, "restart_count": 0}
UNHEALTHY_NODE = {"node_ok": False, "restart_count": 0}


def _clean_result(violates=False, success_100pct=True, valid=True):
    return {
        "valid": valid,
        "stages": [{"stage": "v3-low-load", "violates": violates, "p95": 0.3}],
        "all_success_100pct": success_100pct,
    }


def test_judge_session_exclusion_all_clean_not_excluded():
    excluded, reasons = judge_session_exclusion(
        _clean_result(), HEALTHY_NODE, HEALTHY_NODE,
        oom_before=False, oom_after=False, target_replaced=False, cleanup_ok=True)
    assert excluded is False and reasons == []
    print("OK - 전부 정상이면 제외되지 않음")


def test_judge_session_exclusion_invalid_ramp_result():
    result = {"valid": False, "reason": "baseline_violating"}
    excluded, reasons = judge_session_exclusion(
        result, HEALTHY_NODE, HEALTHY_NODE,
        oom_before=False, oom_after=False, target_replaced=False, cleanup_ok=True)
    assert excluded is True
    assert any("invalid" in r for r in reasons)
    print("OK - ramp/probe 자체가 invalid(baseline_violating 등)면 제외")


def test_judge_session_exclusion_sustained_slo_violation():
    excluded, reasons = judge_session_exclusion(
        _clean_result(violates=True), HEALTHY_NODE, HEALTHY_NODE,
        oom_before=False, oom_after=False, target_replaced=False, cleanup_ok=True)
    assert excluded is True
    assert any("SLO 위반" in r for r in reasons)
    print("OK - stage에서 SLO 위반 확인되면 제외")


def test_judge_session_exclusion_incomplete_success_rate():
    excluded, reasons = judge_session_exclusion(
        _clean_result(success_100pct=False), HEALTHY_NODE, HEALTHY_NODE,
        oom_before=False, oom_after=False, target_replaced=False, cleanup_ok=True)
    assert excluded is True
    assert any("성공률" in r for r in reasons)
    print("OK - 요청 성공률이 100%가 아니면 제외")


def test_judge_session_exclusion_node_unhealthy_before_or_after():
    excluded, reasons = judge_session_exclusion(
        _clean_result(), UNHEALTHY_NODE, HEALTHY_NODE,
        oom_before=False, oom_after=False, target_replaced=False, cleanup_ok=True)
    assert excluded is True
    assert any("Node" in r for r in reasons)
    print("OK - 수집 전 Node 이상이면 제외")


def test_judge_session_exclusion_restart_count_change():
    excluded, reasons = judge_session_exclusion(
        _clean_result(), {"node_ok": True, "restart_count": 0}, {"node_ok": True, "restart_count": 1},
        oom_before=False, oom_after=False, target_replaced=False, cleanup_ok=True)
    assert excluded is True
    assert any("restartCount" in r for r in reasons)
    print("OK - restartCount가 바뀌면 제외")


def test_judge_session_exclusion_oom_killed():
    excluded, reasons = judge_session_exclusion(
        _clean_result(), HEALTHY_NODE, HEALTHY_NODE,
        oom_before=False, oom_after=True, target_replaced=False, cleanup_ok=True)
    assert excluded is True
    assert any("OOMKilled" in r for r in reasons)
    print("OK - OOMKilled 관측되면 제외")


def test_judge_session_exclusion_target_replaced():
    excluded, reasons = judge_session_exclusion(
        _clean_result(), HEALTHY_NODE, HEALTHY_NODE,
        oom_before=False, oom_after=False, target_replaced=True, cleanup_ok=True)
    assert excluded is True
    assert any("교체" in r for r in reasons)
    print("OK - target pod 교체(promotion 포함) 감지되면 제외")


def test_judge_session_exclusion_cleanup_failure():
    excluded, reasons = judge_session_exclusion(
        _clean_result(), HEALTHY_NODE, HEALTHY_NODE,
        oom_before=False, oom_after=False, target_replaced=False, cleanup_ok=False)
    assert excluded is True
    assert any("cleanup" in r for r in reasons)
    print("OK - cleanup_unpromoted_preview 실패(단일 revision 복원 실패)면 제외")


def test_judge_session_exclusion_accumulates_multiple_reasons():
    excluded, reasons = judge_session_exclusion(
        _clean_result(violates=True), HEALTHY_NODE, HEALTHY_NODE,
        oom_before=False, oom_after=True, target_replaced=False, cleanup_ok=True)
    assert excluded is True
    assert len(reasons) >= 2
    print("OK - 여러 조건이 동시에 위반되면 전부 reasons에 누적")


# --- verify_and_force_cleanup (2026-09-20 실측 버그 회귀) ---------------------

_PREP_INFO = {"ready": True, "pre_prepare_active_selector": "old-hash", "created_pod_hash": "preview-hash"}


def test_verify_and_force_cleanup_passthrough_when_already_decided():
    assert verify_and_force_cleanup(_PREP_INFO, True) is True
    assert verify_and_force_cleanup(_PREP_INFO, False) is False
    print("OK - cleanup_ok가 이미 True/False면 그대로 통과(원 함수가 실제로 시도한 경우)")


def test_verify_and_force_cleanup_noop_when_no_preview_was_prepared():
    assert verify_and_force_cleanup(None, None) is None
    assert verify_and_force_cleanup({"ready": False}, None) is None
    print("OK - 애초에 준비된 preview가 없으면(native 등) None 그대로")


def test_verify_and_force_cleanup_forces_abort_when_still_unpromoted():
    calls = {"aborted": False}
    result = verify_and_force_cleanup(
        _PREP_INFO, None,
        get_status_fn=lambda: {"active_selector": "old-hash", "current_pod_hash": "preview-hash"},
        abort_fn=lambda: calls.__setitem__("aborted", True),
        wait_rolled_back_fn=lambda pre_active, our_hash: True,
    )
    assert result is True
    assert calls["aborted"] is True
    print("OK - 원 함수가 스킵(None)했는데 실제로 아직 미승격이면 abort를 직접 재시도")


def test_verify_and_force_cleanup_does_not_touch_promoted_preview():
    calls = {"aborted": False}
    result = verify_and_force_cleanup(
        _PREP_INFO, None,
        get_status_fn=lambda: {"active_selector": "preview-hash", "current_pod_hash": "preview-hash"},
        abort_fn=lambda: calls.__setitem__("aborted", True),
        wait_rolled_back_fn=lambda pre_active, our_hash: True,
    )
    assert result is None
    assert calls["aborted"] is False
    print("OK - 실제로 이미 승격됐으면(activeSelector가 우리 hash로 바뀜) 손대지 않음")


def test_verify_and_force_cleanup_does_not_touch_unrelated_change():
    calls = {"aborted": False}
    result = verify_and_force_cleanup(
        _PREP_INFO, None,
        get_status_fn=lambda: {"active_selector": "old-hash", "current_pod_hash": "some-other-hash"},
        abort_fn=lambda: calls.__setitem__("aborted", True),
        wait_rolled_back_fn=lambda pre_active, our_hash: True,
    )
    assert result is None
    assert calls["aborted"] is False
    print("OK - 우리가 만든 preview가 아닌 다른 변경이 있으면 fail-closed로 손대지 않음")


# --- run_candidate_with_retry (2026-09-20 실측 발견 - 간헐적 kubectl 오류) ----

def test_run_candidate_with_retry_succeeds_first_try():
    with patch("collect_session.run_candidate", return_value={"valid": True}) as mock_rc, \
         patch("collect_session.time.sleep") as mock_sleep:
        result = run_candidate_with_retry("cfg.yaml", "probe.yaml", "label")
    assert result["valid"] is True
    assert result["run_candidate_attempts"] == 1
    mock_rc.assert_called_once()
    mock_sleep.assert_not_called()
    print("OK - 첫 시도가 성공하면 재시도 없이 바로 반환")


def test_run_candidate_with_retry_retries_on_transient_kubectl_error():
    call_count = {"n": 0}

    def side_effect(*_a, **_kw):
        call_count["n"] += 1
        if call_count["n"] < 2:
            raise subprocess.CalledProcessError(1, ["kubectl", "run"])
        return {"valid": True}

    with patch("collect_session.run_candidate", side_effect=side_effect), \
         patch("collect_session.time.sleep") as mock_sleep:
        result = run_candidate_with_retry("cfg.yaml", "probe.yaml", "label")
    assert result["valid"] is True
    assert result["run_candidate_attempts"] == 2
    mock_sleep.assert_called_once()
    print("OK - 일시적 CalledProcessError는 재시도해서 성공하면 시도 횟수와 함께 반환")


def test_run_candidate_with_retry_gives_up_after_max_attempts():
    with patch("collect_session.run_candidate", side_effect=subprocess.CalledProcessError(1, ["kubectl", "run"])), \
         patch("collect_session.time.sleep"):
        try:
            run_candidate_with_retry("cfg.yaml", "probe.yaml", "label")
            assert False, "최대 시도 횟수를 넘기면 예외가 다시 던져져야 함"
        except subprocess.CalledProcessError:
            pass
    print("OK - 최대 시도 횟수를 다 써도 계속 실패하면 마지막 오류를 그대로 전파")


def test_run_candidate_with_retry_does_not_retry_domain_errors():
    """valid=False(baseline_violating 등) 같은 도메인 판정은 재시도 대상이
    아니다 - 인프라 오류(CalledProcessError)만 재시도한다."""
    with patch("collect_session.run_candidate", return_value={"valid": False, "reason": "baseline_violating"}) as mock_rc, \
         patch("collect_session.time.sleep") as mock_sleep:
        result = run_candidate_with_retry("cfg.yaml", "probe.yaml", "label")
    assert result["valid"] is False
    mock_rc.assert_called_once()
    mock_sleep.assert_not_called()
    print("OK - 도메인 판정(valid=False)은 재시도하지 않고 그대로 반환")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        t()
    print(f"전체 통과 ({len(tests)}개)")
