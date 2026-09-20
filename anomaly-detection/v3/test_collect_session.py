#!/usr/bin/env python3
"""collect_session.py 검증 - judge_session_exclusion()(순수 함수)만 대상.
세션 수집 자체의 라이브 오케스트레이션(preview 생성/promotion 감시/ramp
실행)은 explore_ramp_intensity.run_candidate()와 같은 이유로 오프라인
테스트 대상이 아니다(실클러스터·Rollout·Prometheus 의존)."""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from collect_session import judge_session_exclusion

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


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        t()
    print(f"전체 통과 ({len(tests)}개)")
