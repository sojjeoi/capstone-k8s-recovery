#!/usr/bin/env python3
"""explore_ramp_intensity.py 검증 - 2026-09-18 실제로 발생한 stage 경계
오분류 버그(explore-20260918T095809Z: 명목 duration으로 구간을 잘라
아직 진행 중인 마지막 stage 트래픽을 drain으로 잘못 분류, 결과적으로
drain이 회복 안 하는 것처럼 보임)를 회귀 고정한다."""
from datetime import datetime, timedelta, timezone

from explore_ramp_intensity import bucket_stats, classify_stages, parse_ramp_summary

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _row(offset_sec, latency=0.1, success=True):
    return {"sent_at": T0 + timedelta(seconds=offset_sec), "latency": latency, "success": success}


def _stage(name, start_sec, end_sec):
    return {"stage": name, "stage_start_utc": T0 + timedelta(seconds=start_sec),
            "stage_end_utc": T0 + timedelta(seconds=end_sec)}


def test_clean_boundaries_no_drift():
    # stage 경계가 명목과 정확히 일치하는 이상적인 경우.
    stages = [_stage("s1", 60, 150), _stage("s2", 150, 240)]
    rows = ([_row(30)]  # baseline
            + [_row(i) for i in (60, 100, 149)]  # s1
            + [_row(i) for i in (150, 200, 239)]  # s2
            + [_row(250)])  # drain
    result = classify_stages(rows, stages)
    assert len(result["baseline"]) == 1
    assert len(result["stages"]) == 2
    assert len(result["stages"][0][1]) == 3
    assert len(result["stages"][1][1]) == 3
    assert len(result["drain"]) == 1


def test_delayed_stage_end_reclassifies_overhang_to_that_stage():
    # 실제로 발생한 버그의 재현: stage-1이 명목상 60~150초에 끝나야 하지만
    # straggler 대기 때문에 실제로는 165초까지 밀렸다(ramp.py가 기록한
    # stage_end_utc가 그 사실을 그대로 반영). 150~165초 사이에 도착한
    # probe 표본은 "stage-2가 이미 시작됐다"고 명목상 가정하면 안 되고,
    # 아직 안 끝난 stage-1에 속해야 한다.
    stages = [_stage("s1", 60, 165), _stage("s2", 165, 240)]
    overhang_row = _row(160)  # 명목상 stage-2 구간(150~)이지만 실제로는 s1이 아직 안 끝남
    rows = [_row(70), overhang_row, _row(200)]
    result = classify_stages(rows, stages)
    s1_bucket = result["stages"][0][1]
    s2_bucket = result["stages"][1][1]
    assert overhang_row in s1_bucket, "실제 stage_end_utc 이전 표본은 지연됐어도 그 stage에 속해야 함"
    assert overhang_row not in s2_bucket


def test_drain_only_includes_samples_after_real_last_stage_end():
    # 실제로 발생한 버그의 핵심: 명목 합계(90초)가 아니라 마지막 stage의
    # 실제 stage_end_utc(105초, straggler 대기로 15초 밀림) 이후만 drain.
    stages = [_stage("s1", 0, 105)]
    still_running = _row(95)  # 명목 90초 이후지만 실제로는 아직 stage-1 진행 중
    truly_drain = _row(110)   # 실제 stage_end_utc(105) 이후
    result = classify_stages([still_running, truly_drain], stages)
    assert still_running in result["stages"][0][1]
    assert still_running not in result["drain"]
    assert truly_drain in result["drain"]


def test_no_ramp_stages_everything_is_baseline():
    rows = [_row(0), _row(10)]
    result = classify_stages(rows, [])
    assert result["baseline"] == rows
    assert result["stages"] == []
    assert result["drain"] == []


def test_bucket_stats_empty_bucket_reports_no_data_sentinel():
    stats = bucket_stats([])
    assert stats == {"n": 0, "success_rate": None, "mean": None, "p95": None, "max": None, "violates": None}


def test_bucket_stats_computes_success_rate_and_violation():
    bucket = [_row(0, latency=0.1, success=True), _row(1, latency=10.0, success=False)]
    stats = bucket_stats(bucket)
    assert stats["n"] == 2
    assert stats["success_rate"] == 0.5
    assert stats["max"] == 10.0
    assert stats["violates"] is True  # max(10.0) used as p95 proxy when n<20, well above threshold


def test_parse_ramp_summary_parses_timestamps_to_datetime():
    text = ("stage,stage_start_utc,stage_end_utc\n"
            "s1,2026-01-01T00:00:00+00:00,2026-01-01T00:01:45+00:00\n")
    rows = parse_ramp_summary(text)
    assert len(rows) == 1
    assert rows[0]["stage"] == "s1"
    assert rows[0]["stage_start_utc"] == T0
    assert rows[0]["stage_end_utc"] == T0 + timedelta(seconds=105)


def test_parse_ramp_summary_preserves_target_rps():
    # 실제로 발생한 버그의 재현: run_candidate()가 stage 결과 dict를 만들 때
    # {"stage":..., "stage_start_utc":..., "stage_end_utc":..., **bucket_stats(bucket)}
    # 처럼 필드를 골라 담으면 target_rps가 빠져 judge_candidate()가 RPS로
    # stage를 못 찾고 전부 None 취급했다(§25 재현성 검증 1차 시도에서 실측
    # 확인). classify_stages()가 반환하는 원본 stage dict에 target_rps가
    # 남아있는지, 그리고 {**s, **bucket_stats(bucket)} 병합 패턴이 그걸
    # 보존하는지 확인한다.
    text = ("stage,target_rps,stage_start_utc,stage_end_utc\n"
            "explore-0.3rps,0.3,2026-01-01T00:00:00+00:00,2026-01-01T00:01:30+00:00\n")
    ramp_stages = parse_ramp_summary(text)
    assert ramp_stages[0]["target_rps"] == "0.3"

    rows = [_row(10)]
    result = classify_stages(rows, ramp_stages)
    s, bucket = result["stages"][0]
    merged = {**s, **bucket_stats(bucket)}
    assert merged["target_rps"] == "0.3", "target_rps가 병합 후에도 남아있어야 함(버그: 누락되면 RPS 판정 불가)"
