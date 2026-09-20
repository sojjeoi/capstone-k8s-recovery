#!/usr/bin/env python3
"""qualify_normal_profile.py 검증 - judge_qualification()/check_endpoint_
isolation()의 순수 판정 로직만 대상(라이브 오케스트레이션은 collect_session.py/
aba_diagnostic.py와 같은 이유로 오프라인 테스트 대상 아님 - 실클러스터·
Rollout·Prometheus 의존)."""
import sys
from unittest.mock import patch

sys.stdout.reconfigure(encoding="utf-8")

import pytest

from qualify_normal_profile import (
    PROFILE_CONFIGS,
    PROFILE_RUN_LABELS,
    check_endpoint_isolation,
    classify_official_session,
    collect_qualification_session,
    judge_qualification,
)

_CLEAN = dict(
    t_slo=None, success_rate_ok=True, node_before_ok=True, node_after_ok=True,
    active_restart_changed=False, preview_restart_changed=False, oom_observed=False,
    target_replaced=False, endpoint_isolated_before=True, endpoint_isolated_after=True,
    invalid_window_count=0, cleanup_ok=True, extreme_latency_detected=False,
    window_boundary_ok=True,
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


def test_judge_qualification_window_boundary_violation_fails():
    passed, reasons = judge_qualification(**{**_CLEAN, "window_boundary_ok": False})
    assert passed is False
    assert any("session 경계" in r for r in reasons)
    print("OK - feature window가 session 경계를 벗어나면(§65.3) FAIL")


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


def test_classify_official_session_passed_train():
    result = classify_official_session(True, "train", None)
    assert result == {"included_in_training": True, "included_in_calibration": False,
                       "included_in_holdout": False, "classification": "normal_valid"}
    print("OK - PASS+train이면 included_in_training만 True, normal_valid")


def test_classify_official_session_passed_calibration():
    result = classify_official_session(True, "calibration", None)
    assert result["included_in_calibration"] is True
    assert result["included_in_training"] is False and result["included_in_holdout"] is False
    print("OK - PASS+calibration이면 included_in_calibration만 True")


def test_classify_official_session_passed_holdout():
    result = classify_official_session(True, "holdout", None)
    assert result["included_in_holdout"] is True
    assert result["included_in_training"] is False and result["included_in_calibration"] is False
    print("OK - PASS+holdout이면 included_in_holdout만 True")


def test_classify_official_session_failed_with_slo_violation():
    """§67.2 핵심 - 진짜 sustained SLO 위반으로 FAIL하면 어떤 split에도
    포함되지 않고 unexpected_slo_violation으로 영구 분류된다(평균 P95가
    threshold 아래였다는 이유로 정상 재분류하지 않음 - official-train-
    sustained_load-20260920 실측 사례)."""
    result = classify_official_session(False, "train", "2026-09-20T10:54:20.545650+00:00")
    assert result == {"included_in_training": False, "included_in_calibration": False,
                       "included_in_holdout": False, "classification": "unexpected_slo_violation"}
    print("OK - t_slo 존재+FAIL이면 세 split 전부 False, unexpected_slo_violation")


def test_classify_official_session_failed_without_slo_violation():
    """t_slo가 없는데(=진짜 sustained 위반은 아님) FAIL한 경우(restart/OOM/
    Node/harness 오류 등)는 invalid_session으로 구분한다 - §65.4의 기존
    "기술적 오류는 새 ID로 재실행" 경로 대상."""
    result = classify_official_session(False, "calibration", None)
    assert result["classification"] == "invalid_session"
    assert not any([result["included_in_training"], result["included_in_calibration"], result["included_in_holdout"]])
    print("OK - t_slo 없이 FAIL(restart/OOM/Node/harness 등)이면 invalid_session")


def test_collect_official_session_requires_valid_split_role():
    """official=True인데 split_role이 없거나 잘못됐으면 클러스터를 건드리기
    전에(get_active_pods() 호출 전) fail-closed로 거부한다(§65.1 - 역할은
    측정 전에 고정돼야 하므로, 빠뜨린 채로 세션이 시작되면 안 됨)."""
    with pytest.raises(ValueError):
        collect_qualification_session("low_load", "x", official=True, split_role=None)
    with pytest.raises(ValueError):
        collect_qualification_session("low_load", "x", official=True, split_role="not-a-real-role")
    print("OK - official 세션은 유효한 split_role 없이 시작되지 않음(클러스터 호출 전 차단)")


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
