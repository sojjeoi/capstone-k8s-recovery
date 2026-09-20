#!/usr/bin/env python3
"""stats_utils.py 검증 - 순수 통계 함수만 대상, 클러스터 의존 없음."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

from stats_utils import exact_binomial_ci, outside_robust_range, robust_range


def test_exact_binomial_ci_one_of_eight():
    lo, hi = exact_binomial_ci(1, 8)
    assert 0.0 <= lo < 0.125 < hi <= 1.0
    print(f"OK - 1/8 위반의 Clopper-Pearson 95% CI = [{lo:.4f}, {hi:.4f}]")


def test_exact_binomial_ci_zero_of_four():
    lo, hi = exact_binomial_ci(0, 4)
    assert lo == 0.0 and hi > 0.0
    print(f"OK - 0/4 위반(idle)의 CI 하한은 0, 상한은 0보다 큼(경험적 비율만으로 '절대 0%' 단정 안 함) = [{lo:.4f}, {hi:.4f}]")


def test_exact_binomial_ci_no_sessions():
    assert exact_binomial_ci(0, 0) == (None, None)
    print("OK - 세션이 0개면 CI 계산 안 함(None)")


def test_robust_range_basic():
    r = robust_range([1.0, 2.0, 3.0, 4.0, 5.0])
    assert r["median"] == 3.0 and r["n"] == 5
    print("OK - robust_range가 median/IQR/MAD를 정상 계산")


def test_robust_range_empty():
    r = robust_range([])
    assert r["n"] == 0 and r["median"] is None
    print("OK - 빈 입력은 n=0으로 안전 반환")


def test_outside_robust_range_detects_outlier():
    ref = robust_range([1.0, 1.1, 0.9, 1.05, 0.95])
    assert outside_robust_range(10.0, ref) is True
    assert outside_robust_range(1.0, ref) is False
    print("OK - Tukey fence 밖 값은 True, 범위 안 값은 False")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        t()
    print(f"전체 통과 ({len(tests)}개)")
