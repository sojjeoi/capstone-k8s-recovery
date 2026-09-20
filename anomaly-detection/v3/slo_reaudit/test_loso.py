#!/usr/bin/env python3
"""loso.py 검증 - 합성 데이터로 세션 단위 LOSO 로직만 확인. 클러스터
의존 없음."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

from loso import FEATURE_NAMES, leave_one_session_out


def _fake_entry(session_id: str, cpu_mean_values: list):
    """FEATURE_NAMES 8개 중 cpu_mean(index 0)만 바꾸고 나머지는 상수 -
    합성 테스트용 최소 구성."""
    rows = [{"valid": True, "features": [v, 0.0, 7e9, 0.0, 0.0, 0.0, 0.0, 0.0]} for v in cpu_mean_values]
    return {"session_id": session_id, "_full_session": {"feature_rows": rows}}


def test_outlier_session_flagged_by_others_full_range():
    normal = [_fake_entry(f"s{i}", [1.0, 1.1, 0.9, 1.05]) for i in range(3)]
    outlier = _fake_entry("outlier", [5.0, 5.2, 4.9, 5.1])
    result = leave_one_session_out(normal + [outlier])
    outlier_diag = next(d for d in result if d["session_id"] == "outlier")
    assert outlier_diag["per_feature"]["cpu_mean"]["target_min_outside_others_full_range"] is True
    print("OK - 다른 세션 범위를 완전히 벗어난 세션은 outside=True로 잡힘")


def test_overlapping_session_not_flagged():
    """§78.5에서 실측 확인한 것과 같은 패턴 - 개별 세션 범위가 넓게
    겹치면(다른 세션도 비슷한 극단값을 보이면) outside로 잡히지 않아야
    한다(풀링된 quartile로는 오도될 수 있었던 지점)."""
    wide_sessions = [_fake_entry(f"s{i}", [0.7, 1.8, 1.0, 1.5]) for i in range(3)]
    target = _fake_entry("target", [0.75, 1.2, 0.9])  # 다른 세션들 범위 [0.7, 1.8] 안에 완전히 포함
    result = leave_one_session_out(wide_sessions + [target])
    target_diag = next(d for d in result if d["session_id"] == "target")
    assert target_diag["per_feature"]["cpu_mean"]["target_min_outside_others_full_range"] is False
    assert target_diag["per_feature"]["cpu_mean"]["target_max_outside_others_full_range"] is False
    print("OK - 다른 세션들이 이미 비슷하게 넓은 범위를 보이면 outside로 안 잡힘(§78.5 실측과 일치)")


def test_all_sessions_get_a_diagnosis():
    entries = [_fake_entry(f"s{i}", [1.0, 1.1]) for i in range(4)]
    result = leave_one_session_out(entries)
    assert {d["session_id"] for d in result} == {"s0", "s1", "s2", "s3"}
    print("OK - 모든 세션이 한 번씩 target이 되어 LOSO 진단을 받음")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        t()
    print(f"전체 통과 ({len(tests)}개)")
