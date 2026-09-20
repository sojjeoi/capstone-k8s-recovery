#!/usr/bin/env python3
"""§78 - 세션 단위 자연 SLO 위반율 계산에 쓰는 순수 통계 유틸. Overlapping
window가 아니라 독립 session을 표본 단위로 삼는다(지시)."""
import statistics

from scipy import stats as _scipy_stats


def exact_binomial_ci(successes: int, n: int, confidence: float = 0.95) -> tuple:
    """Clopper-Pearson 정확 이항 신뢰구간. `successes`=위반 session 수,
    `n`=독립 session 총수. n=0이면 계산 불가(None, None)."""
    if n == 0:
        return (None, None)
    alpha = 1 - confidence
    lo = 0.0 if successes == 0 else _scipy_stats.beta.ppf(alpha / 2, successes, n - successes + 1)
    hi = 1.0 if successes == n else _scipy_stats.beta.ppf(1 - alpha / 2, successes + 1, n - successes)
    return (float(lo), float(hi))


def robust_range(values: list) -> dict:
    """median/IQR/MAD - LOSO 진단에서 "나머지 무장애 세션의 정상 범위"를
    표현하는 데 쓴다. 표준화(z-score 등)는 하지 않는다(지시 - 결과에
    맞춘 재조정 금지 원칙과 같은 맥락으로, 여기서도 최소한의 가공만)."""
    if not values:
        return {"median": None, "q1": None, "q3": None, "iqr": None, "mad": None, "n": 0}
    sorted_v = sorted(values)
    n = len(sorted_v)
    median = statistics.median(sorted_v)
    q1 = statistics.median(sorted_v[: n // 2]) if n >= 2 else sorted_v[0]
    q3 = statistics.median(sorted_v[(n + 1) // 2:]) if n >= 2 else sorted_v[0]
    mad = statistics.median([abs(v - median) for v in sorted_v])
    return {"median": median, "q1": q1, "q3": q3, "iqr": q3 - q1, "mad": mad,
            "min": min(sorted_v), "max": max(sorted_v), "n": n}


def outside_robust_range(value: float, ref_range: dict, iqr_multiplier: float = 1.5) -> bool:
    """value가 ref_range(다른 세션들의 robust_range 출력)의 [q1-k*iqr,
    q3+k*iqr] 밖에 있는지 - LOSO 진단의 "범위를 벗어났는가" 판정에 쓰는
    탐색적 기준(표준 Tukey fence, k=1.5 기본값). 이 자체가 최종 판정은
    아니고 사람이 볼 표를 만드는 용도."""
    if ref_range["n"] == 0 or ref_range["iqr"] is None:
        return False
    lo = ref_range["q1"] - iqr_multiplier * ref_range["iqr"]
    hi = ref_range["q3"] + iqr_multiplier * ref_range["iqr"]
    return value < lo or value > hi
