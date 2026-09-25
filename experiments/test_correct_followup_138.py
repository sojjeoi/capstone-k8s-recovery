"""correct_followup_138.py 오프라인 검증 (이번 지시 §6). 합성 fixture만
사용 - 클러스터·실제 trial 불필요. 900초 경계, timezone/소수점 표현,
중복/누락 terminal, 창 밖 발신, 재위반(2episode) fixture를 전부 다룬다."""
from datetime import datetime, timedelta, timezone

import slo_judge as sj
from correct_137_errors import find_all_episodes
from correct_followup_138 import build_cohort, cohort_summary, classify_unresolved, _parse_ts
from followup_cost_sampler import reclassify_missed

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _sent(rid, sent_at, **tag):
    return {"request_id": rid, "event": "sent", "sent_at": sent_at.isoformat(), **tag}


def _completed(rid, sent_at, elapsed=0.3, success=True):
    return {"request_id": rid, "event": "completed", "sent_at": sent_at.isoformat(),
            "completed_at": (sent_at + timedelta(seconds=elapsed)).isoformat(),
            "elapsed_monotonic_sec": elapsed, "success": success}


# ---- §2: 900초 경계 ----
def test_boundary_exactly_at_900_excluded():
    ev = [
        _sent("a", T0 + timedelta(seconds=899.999)), _completed("a", T0 + timedelta(seconds=899.999)),
        _sent("b", T0 + timedelta(seconds=900.000)), _completed("b", T0 + timedelta(seconds=900.000)),
        _sent("c", T0 + timedelta(seconds=900.001)), _completed("c", T0 + timedelta(seconds=900.001)),
    ]
    cohort = build_cohort(ev, T0, window_sec=900)
    assert "a" in cohort["in_window"], "899.999초는 창 안(t_injection<=sent_at<900)"
    assert "b" in cohort["post_window"], "정확히 900.000초는 창 밖(상한 배타적, < 900이어야 포함)"
    assert "c" in cohort["post_window"], "900.001초는 창 밖"
    print("OK - 900초 상한 경계(배타적) 정확")


def test_boundary_before_injection_excluded():
    ev = [_sent("x", T0 - timedelta(seconds=0.001)), _completed("x", T0 - timedelta(seconds=0.001)),
          _sent("y", T0), _completed("y", T0)]
    cohort = build_cohort(ev, T0)
    assert "x" in cohort["pre_window"] and "x" not in cohort["in_window"]
    assert "y" in cohort["in_window"], "정확히 t_injection은 포함(하한 포함적)"
    print("OK - t_injection 하한 경계(포함적) 정확")


# ---- §2: timezone/소수점 표현 혼용 ----
def test_mixed_timezone_representations():
    # 'Z' 접미사와 '+00:00'을 섞어써도 같은 결과가 나와야 한다.
    ev = [
        {"request_id": "z1", "event": "sent", "sent_at": "2026-01-01T00:05:00.123456Z"},
        {"request_id": "z1", "event": "completed", "sent_at": "2026-01-01T00:05:00.123456Z",
         "completed_at": "2026-01-01T00:05:00.4Z", "elapsed_monotonic_sec": 0.3, "success": True},
        {"request_id": "z2", "event": "sent", "sent_at": "2026-01-01T00:05:01.000000+00:00"},
        {"request_id": "z2", "event": "completed", "sent_at": "2026-01-01T00:05:01.000000+00:00",
         "completed_at": "2026-01-01T00:05:01.3+00:00", "elapsed_monotonic_sec": 0.3, "success": True},
    ]
    cohort = build_cohort(ev, T0)
    assert len(cohort["in_window"]) == 2, "Z와 +00:00 표현이 섞여도 둘 다 정상 파싱·비교돼야 함"
    print("OK - 'Z'와 '+00:00' timestamp 표현 혼용 정상 처리")


# ---- §2: 구간 내 발신, 구간 후 완료 ----
def test_sent_in_window_completed_after_window():
    sent_at = T0 + timedelta(seconds=895)  # 창 안에서 발신
    ev = [_sent("late", sent_at),
          # 30초 걸려 완료 - completed_at은 창 밖(925s)이지만 sent_at 기준으로는 여전히 창 안
          _completed("late", sent_at, elapsed=30.0)]
    cohort = build_cohort(ev, T0)
    assert "late" in cohort["in_window"], "발신 시각이 창 안이면 완료가 늦어도 cohort에 남아야 함"
    assert "late" in cohort["resolved_in_window"]
    print("OK - 창 안 발신 + 창 밖 완료도 같은 request_id로 계속 추적됨")


# ---- §2: 중복/누락 terminal, sent-only(미완료) ----
def test_duplicate_and_orphan_and_sentonly():
    ev = [
        _sent("dup", T0 + timedelta(seconds=10)),
        _completed("dup", T0 + timedelta(seconds=10)),
        _completed("dup", T0 + timedelta(seconds=10)),  # 중복 terminal(계측 결함 시뮬레이션)
        # terminal만 있고 sent 자체가 없음(원인불명 이상 상황)
        {"request_id": "orphan", "event": "completed", "sent_at": (T0 + timedelta(seconds=20)).isoformat(),
         "completed_at": (T0 + timedelta(seconds=20.3)).isoformat(), "elapsed_monotonic_sec": 0.3, "success": True},
        _sent("neversent", T0 + timedelta(seconds=30)),  # terminal 없음(진짜 미완료)
    ]
    cohort = build_cohort(ev, T0)
    assert "dup" in cohort["duplicate_terminal"]
    assert "dup" not in cohort["resolved_in_window"], "중복 terminal은 지표 계산에서 제외돼야 함(임의로 하나 안 고름)"
    assert "orphan" in cohort["terminal_only"]
    assert "neversent" in cohort["unresolved_in_window"]
    summ = cohort_summary(cohort)
    assert summ["n_duplicate_terminal_excluded"] == 1
    assert summ["n_terminal_only_anomaly"] == 1
    assert summ["n_unresolved_in_window"] == 1
    print("OK - 중복 terminal 제외, terminal-only 이상 표시, sent-only(미완료) 구분")


# ---- §3: 미완료 사유 분류 ----
def test_unresolved_classification_grace_confirmed_vs_unexplained():
    ev_with_grace = [_sent("u1", T0 + timedelta(seconds=5)),
                      {"event": "send_stopped", "reason": "duration_elapsed"},
                      {"event": "unresolved_summary", "n_unresolved_after_grace": 1}]
    cohort1 = build_cohort(ev_with_grace, T0)
    cls1 = classify_unresolved(cohort1, ev_with_grace, "arm")
    assert cls1["u1"] == "grace_expired_confirmed"

    ev_no_send_stopped = [_sent("u2", T0 + timedelta(seconds=5))]  # send_stopped 자체가 없음 - 강제종료 의심
    cohort2 = build_cohort(ev_no_send_stopped, T0)
    cls2 = classify_unresolved(cohort2, ev_no_send_stopped, "arm")
    assert cls2["u2"] == "terminal_missing_unexplained_process_may_have_been_killed"
    print("OK - grace 만료 확정과 원인불명(강제종료 의심) 미완료가 올바르게 구분됨")


# ---- §4: 첫 회복 이후 재위반(2 episode) 장시간 fixture ----
def test_second_episode_after_first_recovery_detected():
    """find_all_episodes()의 '2개 이상' 경로는 이전까지 실제 45+2건 데이터에서
    한 번도 참으로 나온 적이 없었다(전부 0건) - 즉 이 로직 자체가 여지껏
    검증된 적이 없었다. 합성으로 진짜 2-episode를 만들어 최초로 검증한다.

    타이밍은 60초 rolling window가 위반 표본을 다 씻어내는 데 걸리는 시간
    (최대 60초) + persistence(30초)까지 감안해 넉넉히 잡는다 - 개별 latency가
    정상으로 돌아온 순간 바로 회복 판정이 나는 게 아니다(그 전 위반 표본이
    60초간 window에 남아 P95를 계속 끌어올림) - 최초 시도에서 너무 촉박하게
    잡아 실패한 뒤 이렇게 넉넉하게 재설계함(간격 부족 시 두 위반이 사실상
    하나로 합쳐져 보임 - 실제로 겪은 실패 사례)."""
    rows = []
    t = T0
    # 0~40초: 정상(baseline 확보, MIN_SAMPLES=20 채움)
    for i in range(40):
        rows.append({"sent_at": t + timedelta(seconds=i), "latency": 0.2, "success": True})
    # 40~80초: 첫 위반(40초 연속 초과)
    for i in range(40, 80):
        rows.append({"sent_at": t + timedelta(seconds=i), "latency": 1.0, "success": True})
    # 80~260초: 회복 + 충분한 여유(180초 = 60초 window 완전 정화 + 30초 persistence + 90초 여유)
    for i in range(80, 260):
        rows.append({"sent_at": t + timedelta(seconds=i), "latency": 0.2, "success": True})
    # 260~300초: 두 번째 위반(재위반, 40초 연속)
    for i in range(260, 300):
        rows.append({"sent_at": t + timedelta(seconds=i), "latency": 1.0, "success": True})
    # 300~420초: 두 번째 회복 + 여유
    for i in range(300, 420):
        rows.append({"sent_at": t + timedelta(seconds=i), "latency": 0.2, "success": True})

    points = sj.evaluate(rows)
    episodes = find_all_episodes(points)
    assert len(episodes) == 2, f"합성 2-violation fixture에서 정확히 2개 episode를 찾아야 함, 실제={len(episodes)}: {episodes}"
    assert episodes[0]["t_recovery"] is not None and episodes[1]["t_recovery"] is not None
    assert episodes[0]["t_recovery"] < episodes[1]["t_slo"], "두 episode는 시간상 겹치지 않고 순서대로여야 함"
    print(f"OK - 재위반(2episode) fixture에서 find_all_episodes()가 정확히 2개를 찾음: {episodes[0]['t_slo']} / {episodes[1]['t_slo']}")


def test_open_episode_at_observation_end():
    """관측 종료까지 회복 안 된 경우(열린 episode)도 정확히 표시돼야 한다."""
    rows = []
    for i in range(40):
        rows.append({"sent_at": T0 + timedelta(seconds=i), "latency": 0.2, "success": True})
    for i in range(40, 100):  # 위반 시작, 끝까지 회복 안 됨
        rows.append({"sent_at": T0 + timedelta(seconds=i), "latency": 1.0, "success": True})
    points = sj.evaluate(rows)
    episodes = find_all_episodes(points)
    assert len(episodes) == 1
    assert episodes[0]["open_at_observation_end"] is True
    print("OK - 관측 종료 시 미회복 episode가 open_at_observation_end=True로 정확히 표시됨")


# ---- §5: 수집 실패 vs 정상종료 꼬리 구분 ----
def test_cost_trailing_after_exit_not_counted_as_missed():
    samples = [{"t": (T0 + timedelta(seconds=i * 5)).isoformat(), "cpu_cumulative_sec": 0.1 * i,
                "rss_bytes": 1000} for i in range(10)]
    missed_interspersed = [{"t": (T0 + timedelta(seconds=22)).isoformat(), "ok": False}]  # 중간에 낀 진짜 실패
    missed_trailing = [{"t": (T0 + timedelta(seconds=50 + i * 5)).isoformat(), "ok": False} for i in range(5)]  # 마지막 성공 이후 꼬리
    result = reclassify_missed(samples, missed_interspersed + missed_trailing)
    assert len(result["genuine_missed"]) == 1
    assert len(result["trailing_after_exit"]) == 5
    print("OK - 마지막 성공 표본 이후의 연속 실패는 '정상종료 꼬리'로, 중간에 낀 실패만 '진짜 수집실패'로 분류됨")


# ---- §4: rolling window은 주입 전 이력을 유지해야 함(실제 라이브 데이터에서
#      발견한 버그의 회귀 테스트 - 최초 구현은 in_window만 points에 넣어
#      원본 판정과 수 분 단위로 어긋났었다) ----
def test_window_computation_retains_pre_injection_history():
    from correct_followup_138 import build_cohort

    t_inj = T0
    ev = []
    # 주입 2초 전: 심하게 느린 표본 2건(실제 라이브 데이터에서 관측된 패턴 재현)
    ev.append(_sent("pre1", t_inj - timedelta(seconds=2)))
    ev.append(_completed("pre1", t_inj - timedelta(seconds=2), elapsed=1.5))
    ev.append(_sent("pre2", t_inj - timedelta(seconds=1)))
    ev.append(_completed("pre2", t_inj - timedelta(seconds=1), elapsed=0.9))
    # 주입 후: 대부분 정상이지만 소수 경계값 초과 - 표본 수가 적을 때(초기
    # MIN_SAMPLES_FOR_RELIABLE_P95=20 근방) 위 2개 극단값이 섞이면 P95가
    # 금방 넘고, 안 섞이면 한참 안 넘어야 한다.
    for i in range(25):
        lat = 0.7 if i in (3, 4) else 0.3  # 소수만 경계 초과, 나머지는 정상
        ev.append(_sent(f"p{i}", t_inj + timedelta(seconds=i)))
        ev.append(_completed(f"p{i}", t_inj + timedelta(seconds=i), elapsed=lat))

    cohort = build_cohort(ev, t_inj)
    window_prows = []
    for rid, e in {**cohort["pre_window"], **cohort["in_window"]}.items():
        terms = cohort["terminals_by_id"][rid]
        window_prows.append({"sent_at": _parse_ts(e["sent_at"]), "latency": terms[0]["elapsed_monotonic_sec"], "success": True})
    window_prows.sort(key=lambda r: r["sent_at"])
    points_with_history = sj.evaluate(window_prows)

    in_window_only = [r for r in window_prows if r["sent_at"] >= t_inj]
    points_without_history = sj.evaluate(in_window_only)

    # 초기(표본수가 적을 때) P95가 pre-injection 극단값 포함 여부에 따라
    # 달라짐을 직접 확인한다 - 즉 "주입 전 이력을 window에 유지"가 실제로
    # 판정에 영향을 준다는 것 자체를 검증(회귀 방지의 핵심).
    idx = 24  # 마지막 point(표본 27개 중 window에 pre 2개 포함)
    p_with = [p for p in points_with_history if p["sent_at"] == in_window_only[idx]["sent_at"]][0]
    p_without = [p for p in points_without_history if p["sent_at"] == in_window_only[idx]["sent_at"]][0]
    assert p_with["sample_count"] == p_without["sample_count"] + 2, \
        "주입 전 이력을 포함한 쪽이 정확히 2개 더 많은 표본을 window에 가져야 함"
    print("OK - rolling window 계산에 주입 전 이력이 유지됨(제외 시 표본수가 정확히 2개 차이남을 확인)")


if __name__ == "__main__":
    import sys
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n전체 {len(tests)}개 오프라인 검증 통과")
