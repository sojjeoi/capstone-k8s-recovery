#!/usr/bin/env python3
"""slo_judge.py 검증 - 2026-09-18 타임스탬프 재설계(t_slo/t_recovery가
sent_at이 아니라 observed_at=sent_at+latency를 반환하도록 변경) 회귀
테스트. evaluate()의 윈도우 구성·P95·성공률·위반 여부 계산 자체는
바뀌지 않았다는 걸 같은 fixture로 확인한다."""
import sys
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8")

from slo_judge import (
    AVAILABILITY_THRESHOLD, LATENCY_PERSIST_SEC, LATENCY_THRESHOLD,
    evaluate, find_t_recovery, find_t_slo,
)

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _row(offset_sec, latency, success=True):
    return {"sent_at": T0 + timedelta(seconds=offset_sec), "latency": latency, "success": success}


def test_calibration_constants_pinned():
    # 현재 활성 SLO 값을 고정한다 - 의도적 재보정(예: 자원 구성 변경 후
    # L_baseline 재측정) 때만 이 값을 함께 갱신할 것. SLO v2(0.512s)는
    # lab-cpu3-v1 4코어 시절 값이었고, SLO v3(2026-09-18, 0.648s =
    # 2*0.324s)는 lab-cpu3-warm-v1 3코어 재측정값이다 -
    # slo-definition.md 변경이력 참고.
    assert LATENCY_THRESHOLD == 0.648
    assert LATENCY_PERSIST_SEC == 30
    assert AVAILABILITY_THRESHOLD == 0.99
    print("OK - SLO 상수가 v3(0.648s)로 고정됨")


def test_evaluate_preserves_sent_at_and_adds_observed_at():
    rows = [_row(0, 0.1), _row(1, 2.5)]
    points = evaluate(rows)
    assert points[0]["t"] == points[0]["sent_at"] == T0
    assert points[0]["observed_at"] == T0 + timedelta(seconds=0.1)
    assert points[1]["sent_at"] == T0 + timedelta(seconds=1)
    assert points[1]["observed_at"] == T0 + timedelta(seconds=1) + timedelta(seconds=2.5)
    print("OK - point에 sent_at(=t)과 observed_at(=sent_at+latency) 함께 보존")


def test_window_p95_and_violation_flags_match_hand_computed_baseline():
    # 윈도우 구성·P95·성공률·위반 여부 계산 로직은 이번 수정으로 바뀌지
    # 않았다 - 손으로 계산한 기대값과 정확히 일치하는지 확인(회귀 가드).
    # 25개 요청, 1초 간격, 전부 latency=0.1s(현재 임계값 미만)·성공.
    rows = [_row(i, 0.1, success=True) for i in range(25)]
    points = evaluate(rows)
    last = points[-1]
    assert last["success_rate"] == 1.0
    assert last["p95"] == 0.1  # 전부 동일 latency라 percentile도 0.1
    assert last["latency_violating"] is False
    assert last["availability_violating"] is False
    print("OK - P95/성공률/위반 플래그가 손 계산 기대값과 일치(회귀 없음)")


def test_availability_violation_t_slo_uses_observed_at_not_sent_at():
    # 실측 사건 재현(2026-09-17 pod_kill 파일럿): 전송 후 한참 뒤에야
    # 실패가 확정된 요청의 t_slo는 sent_at이 아니라 sent_at+latency여야
    # 한다.
    rows = [_row(0, 2.539, success=False)]  # 단일 요청 - success_rate=0 즉시 위반
    points = evaluate(rows)
    t_slo = find_t_slo(points)
    assert t_slo == T0 + timedelta(seconds=2.539), t_slo
    assert t_slo != T0, "t_slo가 sent_at을 그대로 쓰면 안 됨"
    print("OK - availability 위반의 t_slo = sent_at+latency(observed_at), sent_at 아님")


def test_latency_violation_t_slo_is_observed_at_domain():
    # 0.8초(현재 임계값 0.648s 초과) 요청이 35초간 연속(30초 지속 요건 초과)
    # -> latency 위반. 반환값이 sent_at 도메인(streak_start+30s처럼 sent_at만
    # 으로 계산한 값)이 아니라 실제 표본의 observed_at이어야 한다.
    rows = [_row(i, 0.8, success=True) for i in range(35)]
    points = evaluate(rows)
    t_slo = find_t_slo(points)
    assert t_slo is not None
    # observed_at = sent_at + 0.8s인 표본들의 집합에 속해야 함(정확히 일치하는
    # 표본이 존재) - 즉 "언젠가의 sent_at + 0.8s" 형태여야 한다.
    matched = any(abs((t_slo - (T0 + timedelta(seconds=i, milliseconds=800))).total_seconds()) < 1e-6
                  for i in range(35))
    assert matched, f"t_slo={t_slo}가 어떤 표본의 observed_at과도 안 맞음"
    print("OK - latency 위반의 t_slo도 observed_at 도메인 값")


def test_recovery_uses_observed_at_and_filters_by_observed_at():
    # t_slo 이후 30초간 정상(latency 0.1s, 성공)이 지속되면 회복 - 반환값도
    # observed_at 도메인이어야 하고, "t_slo 이후" 필터도 observed_at 기준이어야
    # 한다(늦게 보냈지만 빨리 끝난 요청과 일찍 보냈지만 오래 걸린 요청이
    # 뒤섞이지 않도록).
    violating = [_row(i, 0.8, success=True) for i in range(35)]  # 0~34초 전송, latency 위반(현재 임계값 0.648s 초과)
    t_slo = find_t_slo(evaluate(violating))
    assert t_slo is not None

    # 60초 롤링 윈도우가 나쁜 표본을 완전히 밀어내려면(35초 시점부터 정상
    # 표본만 보내도) 최소 60초, 거기다 30초 연속 정상까지 확인하려면 총
    # 90초 이상의 정상 트래픽이 필요하다 - 넉넉히 100초를 보낸다.
    recovering = [_row(35 + i, 0.1, success=True) for i in range(100)]  # 35~134초 전송, 정상
    points = evaluate(violating + recovering)
    t_recovery = find_t_recovery(points, t_slo)
    assert t_recovery is not None
    assert t_recovery > t_slo
    # 회복 스트릭 시작 표본의 observed_at(=sent_at+0.1s) 중 하나와 일치해야 함
    matched = any(abs((t_recovery - (T0 + timedelta(seconds=35 + i, milliseconds=100))).total_seconds()) < 1e-6
                  for i in range(100))
    assert matched, f"t_recovery={t_recovery}가 회복 구간 표본의 observed_at과 안 맞음"
    print("OK - t_recovery도 observed_at 도메인, t_slo 이후 필터도 observed_at 기준")


def test_no_violation_returns_none():
    rows = [_row(i, 0.1, success=True) for i in range(25)]
    points = evaluate(rows)
    assert find_t_slo(points) is None
    print("OK - 위반 없으면 t_slo=None(기존과 동일)")


if __name__ == "__main__":
    test_calibration_constants_pinned()
    test_evaluate_preserves_sent_at_and_adds_observed_at()
    test_window_p95_and_violation_flags_match_hand_computed_baseline()
    test_availability_violation_t_slo_uses_observed_at_not_sent_at()
    test_latency_violation_t_slo_is_observed_at_domain()
    test_recovery_uses_observed_at_and_filters_by_observed_at()
    test_no_violation_returns_none()
    print("\n모두 통과")
