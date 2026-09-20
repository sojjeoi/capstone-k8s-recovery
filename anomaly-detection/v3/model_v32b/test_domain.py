#!/usr/bin/env python3
"""domain.py 검증 - infrastructure-normal/SLO label 분리가 §79 정의대로
동작하는지 확인. 클러스터 의존 없음."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

from domain import classify_exclusion_reasons, slo_label


def test_pure_slo_violation_is_still_infrastructure_normal():
    """§79 핵심 - calib2-low-02와 같은 실측 사례: 사유가 sustained SLO
    위반 하나뿐이면 infrastructure_normal=True여야 한다(더는 제외 사유가
    아님)."""
    result = classify_exclusion_reasons(["sustained SLO 위반(t_slo=2026-09-20T17:00:58.640856+00:00)"])
    assert result["infrastructure_normal"] is True
    assert len(result["slo_violation_reasons"]) == 1
    assert result["infrastructure_issues"] == []
    print("OK - 순수 SLO 위반 사유만 있으면 infrastructure_normal=True")


def test_no_reasons_is_infrastructure_normal_and_no_slo():
    result = classify_exclusion_reasons([])
    assert result["infrastructure_normal"] is True
    assert result["slo_violation_reasons"] == []
    print("OK - 사유가 아예 없으면 infra 정상, SLO 위반도 없음")


def test_restart_reason_breaks_infrastructure_normal():
    result = classify_exclusion_reasons(["active restartCount 변화"])
    assert result["infrastructure_normal"] is False
    print("OK - restartCount 변화는 infrastructure_normal을 깨뜨림")


def test_oom_reason_breaks_infrastructure_normal():
    result = classify_exclusion_reasons(["OOMKilled 관측"])
    assert result["infrastructure_normal"] is False
    print("OK - OOMKilled는 infrastructure_normal을 깨뜨림")


def test_cleanup_failure_breaks_infrastructure_normal():
    result = classify_exclusion_reasons(["cleanup 또는 단일 revision 복원 실패"])
    assert result["infrastructure_normal"] is False
    print("OK - cleanup 실패는 infrastructure_normal을 깨뜨림")


def test_slo_and_infrastructure_reasons_both_recorded_independently():
    result = classify_exclusion_reasons(["sustained SLO 위반(t_slo=X)", "Node 상태 이상(측정 전 또는 후)"])
    assert result["infrastructure_normal"] is False  # infra 사유가 있으므로 여전히 깨짐
    assert len(result["slo_violation_reasons"]) == 1
    assert len(result["infrastructure_issues"]) == 1
    print("OK - SLO 사유와 infra 사유가 같이 있어도 각자 독립적으로 분류됨")


def test_no_unclassified_reasons_for_known_vocabulary():
    """judge_qualification()이 실제로 만드는 모든 문자열이 이 함수의
    마커에 빠짐없이 걸리는지 확인 - 새 사유가 생기면 이 테스트가 실패해
    마커 목록을 갱신하라고 알려준다."""
    all_known_reasons = [
        "sustained SLO 위반(t_slo=X)", "요청 성공률 100% 아님", "Node 상태 이상(측정 전 또는 후)",
        "active restartCount 변화", "preview restartCount 변화", "OOMKilled 관측",
        "예기치 않은 target 교체/promotion 감지", "active/preview Endpoint 격리 실패",
        "metric 결측/무효 window 3개", "cleanup 또는 단일 revision 복원 실패",
        "§60 수준의 비정상적인 다초 단위 latency 재발 의심", "feature window가 session 경계를 벗어남",
    ]
    result = classify_exclusion_reasons(all_known_reasons)
    assert result["unclassified_reasons"] == [], f"분류 안 된 사유 발견: {result['unclassified_reasons']}"
    print("OK - judge_qualification()의 알려진 모든 사유가 빠짐없이 분류됨")


def test_slo_label_clean_vs_violation():
    assert slo_label({"t_slo": None}) == "clean"
    assert slo_label({"t_slo": "2026-09-20T17:00:58.640856+00:00"}) == "sustained_violation"
    print("OK - slo_label이 t_slo 존재 여부만으로 clean/sustained_violation을 정확히 구분")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        t()
    print(f"전체 통과 ({len(tests)}개)")
