#!/usr/bin/env python3
"""qualify_normal_profile.py 검증 - judge_qualification()/check_endpoint_
isolation()의 순수 판정 로직만 대상(라이브 오케스트레이션은 collect_session.py/
aba_diagnostic.py와 같은 이유로 오프라인 테스트 대상 아님 - 실클러스터·
Rollout·Prometheus 의존)."""
import sys
from unittest.mock import patch

sys.stdout.reconfigure(encoding="utf-8")

from qualify_normal_profile import (
    PROFILE_CONFIGS,
    PROFILE_RUN_LABELS,
    check_endpoint_isolation,
    judge_qualification,
)

_CLEAN = dict(
    t_slo=None, success_rate_ok=True, node_before_ok=True, node_after_ok=True,
    active_restart_changed=False, preview_restart_changed=False, oom_observed=False,
    target_replaced=False, endpoint_isolated_before=True, endpoint_isolated_after=True,
    invalid_window_count=0, cleanup_ok=True, extreme_latency_detected=False,
)


def test_judge_qualification_all_clean_passes():
    passed, reasons = judge_qualification(**_CLEAN)
    assert passed is True and reasons == []
    print("OK - 전부 정상이면 PASS")


def test_judge_qualification_sustained_slo_violation_fails():
    passed, reasons = judge_qualification(**{**_CLEAN, "t_slo": "2026-09-20T00:00:00+00:00"})
    assert passed is False
    assert any("sustained SLO 위반" in r for r in reasons)
    print("OK - t_slo가 있으면(진짜 30초 sustained) FAIL")


def test_judge_qualification_incomplete_success_rate_fails():
    passed, reasons = judge_qualification(**{**_CLEAN, "success_rate_ok": False})
    assert passed is False
    assert any("성공률" in r for r in reasons)
    print("OK - 요청 성공률 100% 아니면 FAIL")


def test_judge_qualification_node_unhealthy_fails():
    passed, reasons = judge_qualification(**{**_CLEAN, "node_before_ok": False})
    assert passed is False
    assert any("Node" in r for r in reasons)
    print("OK - Node 이상이면 FAIL")


def test_judge_qualification_restart_change_fails():
    passed, reasons = judge_qualification(**{**_CLEAN, "active_restart_changed": True})
    assert passed is False
    assert any("restartCount" in r for r in reasons)
    print("OK - active restartCount 변화면 FAIL")


def test_judge_qualification_oom_fails():
    passed, reasons = judge_qualification(**{**_CLEAN, "oom_observed": True})
    assert passed is False
    assert any("OOMKilled" in r for r in reasons)
    print("OK - OOMKilled 관측되면 FAIL")


def test_judge_qualification_target_replaced_fails():
    passed, reasons = judge_qualification(**{**_CLEAN, "target_replaced": True})
    assert passed is False
    assert any("promotion" in r for r in reasons)
    print("OK - 예기치 않은 target 교체면 FAIL")


def test_judge_qualification_endpoint_isolation_fails():
    passed, reasons = judge_qualification(**{**_CLEAN, "endpoint_isolated_before": False})
    assert passed is False
    assert any("Endpoint" in r for r in reasons)
    print("OK - Endpoint 격리 실패면 FAIL")


def test_judge_qualification_invalid_window_fails():
    passed, reasons = judge_qualification(**{**_CLEAN, "invalid_window_count": 2})
    assert passed is False
    assert any("무효 window" in r for r in reasons)
    print("OK - 무효 window가 있으면 FAIL")


def test_judge_qualification_cleanup_failure_fails():
    passed, reasons = judge_qualification(**{**_CLEAN, "cleanup_ok": False})
    assert passed is False
    assert any("cleanup" in r for r in reasons)
    print("OK - cleanup/단일 revision 복원 실패면 FAIL")


def test_judge_qualification_extreme_latency_fails():
    passed, reasons = judge_qualification(**{**_CLEAN, "extreme_latency_detected": True})
    assert passed is False
    assert any("비정상적인" in r for r in reasons)
    print("OK - §60 수준 비정상 latency 재발 의심이면 FAIL")


def test_judge_qualification_accumulates_multiple_reasons():
    passed, reasons = judge_qualification(**{**_CLEAN, "oom_observed": True, "target_replaced": True})
    assert passed is False and len(reasons) >= 2
    print("OK - 여러 조건이 동시에 위반되면 전부 reasons에 누적")


def test_check_endpoint_isolation_isolated_with_preview():
    def fake_run(cmd):
        svc = cmd[3]
        class R:
            returncode = 0
            stdout = ('{"subsets":[{"addresses":[{"targetRef":{"name":"active-pod"}}]}]}' if svc == "vllm-active"
                       else '{"subsets":[{"addresses":[{"targetRef":{"name":"preview-pod"}}]}]}')
        return R()

    with patch("qualify_normal_profile._run", side_effect=fake_run):
        result = check_endpoint_isolation("active-pod", "preview-pod")
    assert result["isolated"] is True
    print("OK - active/preview Endpoint가 각자 자기 pod 하나씩만 가리키면 isolated=True")


def test_check_endpoint_isolation_leak_detected():
    def fake_run(cmd):
        svc = cmd[3]
        class R:
            returncode = 0
            stdout = ('{"subsets":[{"addresses":[{"targetRef":{"name":"active-pod"}},'
                       '{"targetRef":{"name":"preview-pod"}}]}]}' if svc == "vllm-active"
                       else '{"subsets":[{"addresses":[{"targetRef":{"name":"preview-pod"}}]}]}')
        return R()

    with patch("qualify_normal_profile._run", side_effect=fake_run):
        result = check_endpoint_isolation("active-pod", "preview-pod")
    assert result["isolated"] is False
    print("OK - active Endpoint에 preview pod까지 섞여 있으면 isolated=False")


def test_check_endpoint_isolation_no_preview_expected_empty():
    def fake_run(cmd):
        svc = cmd[3]
        class R:
            returncode = 0
            stdout = ('{"subsets":[{"addresses":[{"targetRef":{"name":"active-pod"}}]}]}' if svc == "vllm-active"
                       else '{"subsets":[]}')
        return R()

    with patch("qualify_normal_profile._run", side_effect=fake_run):
        result = check_endpoint_isolation("active-pod", None)
    assert result["isolated"] is True
    print("OK - preview가 없어야 하는 상태(cleanup 후)에서 vllm-preview Endpoint가 비어 있으면 isolated=True")


def test_profile_run_labels_are_valid_k8s_names():
    """§60의 밑줄 버그(pod 이름 규칙 위반) 재발 방지 - 모든 profile label이
    RFC 1123 안전(밑줄 없음, label[:7]이 그대로 pod 이름 접두어가 됨)."""
    import re
    k8s_name_safe = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    assert set(PROFILE_RUN_LABELS) == set(PROFILE_CONFIGS)
    for profile, label in PROFILE_RUN_LABELS.items():
        assert "_" not in label, f"{profile} -> {label}: 밑줄이 남아있으면 kubectl run이 매번 실패함(§60 실측 확인)"
        assert k8s_name_safe.match(label), f"{profile} -> {label}: K8s 리소스 이름 규칙 위반"
    print("OK - 모든 profile의 run label이 밑줄 없이 K8s 이름 규칙을 지킴")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        t()
    print(f"전체 통과 ({len(tests)}개)")
