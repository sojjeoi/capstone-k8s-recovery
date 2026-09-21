#!/usr/bin/env python3
"""run_memory_pressure_negative_control_pilot.py 검증 - 순수 함수
classify_target_replacement()만 대상(§96 section 7). run_once()의 라이브
오케스트레이션 자체는 이 프로젝트의 다른 트라이얼 러너들과 같은 이유로
오프라인 테스트 대상이 아니다(실클러스터·kubectl exec·probe pod 의존)."""
import sys
from types import SimpleNamespace

sys.stdout.reconfigure(encoding="utf-8")

from run_memory_pressure_negative_control_pilot import classify_target_replacement


def _result(**overrides):
    base = dict(
        arm="proposed", target_replaced=False, t_target_replaced=None,
        action="none", promotion_verified=None, t_switch=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_no_replacement_is_reported_plainly():
    verdict = classify_target_replacement(_result(target_replaced=False))
    assert verdict["category"] == "no_replacement"
    print("OK - target_replaced=False면 no_replacement")


def test_native_replacement_is_always_abnormal():
    verdict = classify_target_replacement(_result(
        arm="native", target_replaced=True, t_target_replaced="2026-01-01T00:00:10+00:00"))
    assert verdict["category"] == "abnormal_replacement"
    print("OK - native arm은 promotion 경로 자체가 없으므로 교체가 있으면 항상 비정상")


def test_replacement_without_verified_promotion_is_abnormal():
    verdict = classify_target_replacement(_result(
        arm="proposed", target_replaced=True, t_target_replaced="2026-01-01T00:00:10+00:00",
        action="none", promotion_verified=None))
    assert verdict["category"] == "abnormal_replacement"
    print("OK - action이 promote_preview가 아니거나 promotion_verified가 True가 아니면 비정상")


def test_replacement_with_promotion_but_missing_timestamps_is_abnormal_fail_closed():
    verdict = classify_target_replacement(_result(
        arm="proposed", target_replaced=True, t_target_replaced=None,
        action="promote_preview", promotion_verified=True, t_switch=None))
    assert verdict["category"] == "abnormal_replacement"
    assert any("근거 불충분" in r or "시각이 없어" in r for r in verdict["reasons"])
    print("OK - promotion은 검증됐지만 timing 근거(t_switch/t_target_replaced)가 없으면 fail-closed로 비정상 취급")


def test_replacement_with_promotion_and_close_timing_is_promotion_caused():
    verdict = classify_target_replacement(_result(
        arm="proposed", target_replaced=True, t_target_replaced="2026-01-01T00:00:15+00:00",
        action="promote_preview", promotion_verified=True, t_switch="2026-01-01T00:00:10+00:00"))
    assert verdict["category"] == "promotion_caused"
    assert verdict["delta_sec"] == 5.0
    print("OK - promotion 검증 + timing 근접(5초)이면 promotion_caused로 분류")


def test_replacement_with_promotion_but_distant_timing_is_abnormal():
    verdict = classify_target_replacement(_result(
        arm="fixed_threshold", target_replaced=True, t_target_replaced="2026-01-01T01:00:00+00:00",
        action="promote_preview", promotion_verified=True, t_switch="2026-01-01T00:00:10+00:00"))
    assert verdict["category"] == "abnormal_replacement"
    assert any("차이가" in r for r in verdict["reasons"])
    print("OK - promotion은 검증됐지만 교체 시각이 300초 넘게 떨어져 있으면(별개 사건 의심) 비정상 취급")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        t()
    print(f"전체 통과 ({len(tests)}개)")
