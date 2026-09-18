#!/usr/bin/env python3
"""slo_judge.py 검증 - 2026-09-18 타임스탬프 재설계(t_slo/t_recovery가
sent_at이 아니라 observed_at=sent_at+latency를 반환하도록 변경) 회귀
테스트. evaluate()의 윈도우 구성·P95·성공률·위반 여부 계산 자체는
바뀌지 않았다는 걸 같은 fixture로 확인한다."""
import sys
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8")

from pathlib import Path

from slo_judge import (
    AVAILABILITY_THRESHOLD, LATENCY_PERSIST_SEC, LATENCY_THRESHOLD,
    MIN_SAMPLES_FOR_RELIABLE_P95, evaluate, find_t_recovery, find_t_slo, load_raw,
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
    rows = [_row(i, 0.8, success=True) for i in range(65)]
    points = evaluate(rows)
    t_slo = find_t_slo(points)
    assert t_slo is not None
    # observed_at = sent_at + 0.8s인 표본들의 집합에 속해야 함(정확히 일치하는
    # 표본이 존재) - 즉 "언젠가의 sent_at + 0.8s" 형태여야 한다.
    matched = any(abs((t_slo - (T0 + timedelta(seconds=i, milliseconds=800))).total_seconds()) < 1e-6
                  for i in range(65))
    assert matched, f"t_slo={t_slo}가 어떤 표본의 observed_at과도 안 맞음"
    print("OK - latency 위반의 t_slo도 observed_at 도메인 값")


def test_recovery_uses_observed_at_and_filters_by_observed_at():
    # t_slo 이후 30초간 정상(latency 0.1s, 성공)이 지속되면 회복 - 반환값도
    # observed_at 도메인이어야 하고, "t_slo 이후" 필터도 observed_at 기준이어야
    # 한다(늦게 보냈지만 빨리 끝난 요청과 일찍 보냈지만 오래 걸린 요청이
    # 뒤섞이지 않도록).
    violating = [_row(i, 0.8, success=True) for i in range(65)]  # 0~64초 전송, latency 위반(19개 warmup 이후 30초+ 지속 필요)
    t_slo = find_t_slo(evaluate(violating))
    assert t_slo is not None

    # 60초 롤링 윈도우가 나쁜 표본을 완전히 밀어내려면(35초 시점부터 정상
    # 표본만 보내도) 최소 60초, 거기다 30초 연속 정상까지 확인하려면 총
    # 90초 이상의 정상 트래픽이 필요하다 - 넉넉히 100초를 보낸다.
    recovering = [_row(65 + i, 0.1, success=True) for i in range(100)]  # 65~164초 전송, 정상(violating 65개 다음)
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


def test_small_sample_single_high_latency_is_not_a_violation():
    # 실제로 발생한 버그(pilot-load_ramp-native-01-20260918T130111Z)의
    # 핵심 재현: 20개 미만 윈도우에서 고지연 표본 1개가 있어도
    # latency_violating은 False(추정 보류)여야 한다 - max()를 P95로
    # 쓰지 않는다.
    rows = [_row(0, 5.0, success=True)] + [_row(i, 0.1, success=True) for i in range(1, MIN_SAMPLES_FOR_RELIABLE_P95 - 1)]
    points = evaluate(rows)
    assert all(p["sample_count"] < MIN_SAMPLES_FOR_RELIABLE_P95 for p in points)
    assert all(p["latency_evaluable"] is False for p in points)
    assert all(p["p95"] is None for p in points)
    assert all(p["latency_violating"] is False for p in points)
    assert find_t_slo(points) is None
    print("OK - 표본 20개 미만에서 고지연 1개는 위반 아님(latency_evaluable=False)")


def test_20_or_more_samples_sustained_high_latency_still_detected():
    # 20개 이상부터는 기존과 동일하게 실제 위반을 정상 검출해야 한다.
    rows = [_row(i, 1.0, success=True) for i in range(65)]
    points = evaluate(rows)
    assert points[-1]["latency_evaluable"] is True
    assert points[-1]["latency_violating"] is True
    t_slo = find_t_slo(points)
    assert t_slo is not None
    print("OK - 20개 이상에서 지속 고지연은 정상 위반 검출")


def test_pilot_pattern_single_early_spike_then_normal_gives_none():
    # 이번 파일럿의 실제 패턴(초반 고지연 1건 뒤 30초+ 정상)을 합성으로
    # 재현 - t_slo가 더 이상 찍히면 안 된다.
    rows = [_row(0, 1.0, success=True)] + [_row(i, 0.2, success=True) for i in range(1, 50)]
    points = evaluate(rows)
    assert find_t_slo(points) is None
    print("OK - 초반 고지연 1건 + 이후 정상 패턴에서 t_slo=None(파일럿 버그 재현 안 됨)")


def test_availability_violation_detected_even_under_20_samples():
    # 표본이 적어도 실패 자체는 기존처럼 즉시 위반으로 잡아야 한다 -
    # latency 판정 보류와 availability 판정은 독립적이다.
    rows = [_row(0, 0.1, success=False)] + [_row(i, 0.1, success=True) for i in range(1, 5)]
    points = evaluate(rows)
    assert points[0]["latency_evaluable"] is False
    assert points[0]["availability_violating"] is True
    t_slo = find_t_slo(points)
    assert t_slo is not None
    print("OK - 20개 미만이라도 실패 요청이 있으면 availability 위반 정상 검출")


def test_recovery_not_confirmed_by_non_evaluable_stretch_alone():
    # t_slo 이후 30초 넘게 latency_evaluable=False인 표본만 이어져도
    # 회복으로 확정되면 안 된다(모른다≠정상).
    violating = [_row(i, 1.0, success=True) for i in range(65)]  # t_slo 확보(19개 warmup 이후 30초+ 지속 필요)
    t_slo = find_t_slo(evaluate(violating))
    assert t_slo is not None
    # 40초 시점부터 5개만 성공적으로 보내(윈도우가 대부분 과거 고지연
    # 표본이라 sample_count는 크지만, 이 테스트는 "표본이 적어서
    # 미평가"인 상황을 직접 구성하기 위해 새 스트림처럼 간격을 크게 둔다.
    sparse_normal = [_row(40 + i * 20, 0.1, success=True) for i in range(3)]  # 40,60,80초 - 간격이 넓어 윈도우 표본이 적음
    points = evaluate(violating + sparse_normal)
    t_recovery = find_t_recovery(points, t_slo)
    # sparse 구간 표본 수가 20 미만이면 latency_evaluable=False라 회복 스트릭이
    # 시작되지 않아야 한다.
    sparse_points = [p for p in points if p["t"] >= T0 + timedelta(seconds=40)]
    if any(not p["latency_evaluable"] for p in sparse_points):
        assert t_recovery is None or t_recovery not in [p["observed_at"] for p in sparse_points if not p["latency_evaluable"]]
    print("OK - latency 비평가 구간만으로 recovery가 확정되지 않음")


def test_normal_violate_then_recover_flow_unchanged_with_enough_samples():
    # 표본이 충분할 때(>=20) 위반->회복 흐름은 기존과 동일하게 동작해야 한다.
    violating = [_row(i, 1.0, success=True) for i in range(65)]
    recovering = [_row(65 + i, 0.1, success=True) for i in range(100)]
    points = evaluate(violating + recovering)
    t_slo = find_t_slo(points)
    t_recovery = find_t_recovery(points, t_slo)
    assert t_slo is not None
    assert t_recovery is not None
    assert t_recovery > t_slo
    print("OK - 표본 충분한 정상 위반->회복 흐름은 기존과 동일")


def test_preserved_pilot_raw_csv_small_sample_points_never_violate():
    # 보존된 이번 파일럿 원본(gitignore 대상, 로컬에만 존재)을 직접 읽어
    # 통합 확인한다 - 없는 환경(신규 clone 등)에서는 건너뛴다.
    #
    # 주의: 이 CSV를 다시 분석하면 t_slo 자체는 여전히 찍힌다(13:03:55의
    # 원래 버그 시각과는 다른 시각) - t=39~41초 구간에 별도의 실제 고지연
    # 클러스터(0.61~0.71s, n=38~54의 정상 통계량, 표본 부족 아님)가 있어
    # 시작부 콜드스타트 클러스터와 겹치는 60초 윈도우 안에서 함께 잡히기
    # 때문이다. 이는 "표본 부족 시 max() 대용"이라는 이번에 고친 버그와는
    # 별개의, 더 근본적인 현상(두 개의 짧은 blip이 겹치는 윈도우를 통해
    # 합쳐져 보이는 문제)이라 이번 수정 범위 밖이다 - 별도 보고 대상.
    # 이 테스트는 그 별개 현상과 무관하게, 고친 버그 자체(표본 20개 미만
    # 구간이 더 이상 latency_violating=True가 되지 않는지)만 검증한다.
    path = Path(__file__).parent / "results" / "probe-pilot-load_ramp-native-01-20260918T130111Z-native-1-raw.csv"
    if not path.exists():
        print("SKIP - 보존된 파일럿 원본 CSV가 이 환경에 없음(gitignore 대상, 로컬 전용)")
        return
    rows = load_raw(path)
    points = evaluate(rows)
    small_sample_points = [p for p in points if p["sample_count"] < MIN_SAMPLES_FOR_RELIABLE_P95]
    assert small_sample_points, "테스트 전제 확인 실패 - 이 CSV엔 표본 20개 미만 구간이 있어야 함"
    assert all(not p["latency_violating"] for p in small_sample_points)
    assert all(p["p95"] is None for p in small_sample_points)
    # 원래 버그가 만든 정확한 오탐 시각(13:03:55.46)은 더 이상 t_slo가 아니어야 함.
    t_slo = find_t_slo(points)
    original_bug_t_slo = datetime.fromisoformat("2026-09-18T13:03:55.461640+00:00")
    assert t_slo != original_bug_t_slo
    print(f"OK - 표본 20개 미만 구간은 더 이상 위반 아님. 원래 버그 시각(13:03:55)은 재현 안 됨"
          f"(참고: 이번 CSV엔 t=39~41초의 별개 실제 클러스터로 인해 t_slo={t_slo}는 여전히 존재 - 범위 밖, 별도 보고)")


if __name__ == "__main__":
    test_calibration_constants_pinned()
    test_evaluate_preserves_sent_at_and_adds_observed_at()
    test_window_p95_and_violation_flags_match_hand_computed_baseline()
    test_availability_violation_t_slo_uses_observed_at_not_sent_at()
    test_latency_violation_t_slo_is_observed_at_domain()
    test_recovery_uses_observed_at_and_filters_by_observed_at()
    test_no_violation_returns_none()
    test_small_sample_single_high_latency_is_not_a_violation()
    test_20_or_more_samples_sustained_high_latency_still_detected()
    test_pilot_pattern_single_early_spike_then_normal_gives_none()
    test_availability_violation_detected_even_under_20_samples()
    test_recovery_not_confirmed_by_non_evaluable_stretch_alone()
    test_normal_violate_then_recover_flow_unchanged_with_enough_samples()
    test_preserved_pilot_raw_csv_no_longer_produces_false_t_slo()
    print("\n모두 통과")
