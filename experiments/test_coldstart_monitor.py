#!/usr/bin/env python3
"""coldstart_monitor.py 검증 - 2026-09-18 HEADROOM-COLDSTART-01 1차 시도의
실제 오탐 버그(정상 startupProbe 실패를 위험으로 오판해 1.3초 만에 조기
abort)를 계기로 4가지를 회귀 고정한다: 정상 startupProbe 실패 무시,
restart count 증가 탐지, OOM·eviction·CrashLoop 탐지, 원래 apply 시각을
유지한 resume 동작."""
import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")

from coldstart_monitor import compute_effective_start_mono, is_risky_event, restart_increased


def test_normal_startup_probe_failure_is_not_risky():
    # 1차 시도에서 실제로 오판했던 두 메시지 그대로 - context deadline
    # exceeded(모델이 아직 포트를 안 열었을 때)와 connection refused
    # (그다음 단계) 둘 다 startupProbe가 반복 실패하는 정상 패턴이다.
    assert is_risky_event(
        "Unhealthy",
        'Startup probe failed: Get "http://10.244.36.42:8000/health": '
        'context deadline exceeded (Client.Timeout exceeded while awaiting headers)'
    ) is False
    assert is_risky_event(
        "Unhealthy",
        'Startup probe failed: Get "http://10.244.36.42:8000/health": '
        'dial tcp 10.244.36.42:8000: connect: connection refused'
    ) is False
    print("OK - 정상 startupProbe 실패(Unhealthy)는 위험으로 안 잡힘")


def test_restart_count_increase_detected():
    assert restart_increased(0, 0) is False
    assert restart_increased(0, 1) is True
    assert restart_increased(2, 3) is True
    assert restart_increased(3, 3) is False
    print("OK - restart count 증가만 감지, 유지는 감지 안 됨")


def test_oom_eviction_crashloop_detected():
    assert is_risky_event("OOMKilling", "Memory cgroup out of memory: Killed process") is True
    assert is_risky_event("Evicted", "The node was low on resource: memory") is True
    assert is_risky_event("BackOff", "Back-off restarting failed container") is True
    assert is_risky_event("FailedScheduling", "0/2 nodes are available: insufficient cpu") is True
    assert is_risky_event("FailedMount", "Unable to attach or mount volumes") is True
    print("OK - OOM·eviction·CrashLoop·스케줄/마운트 실패가 위험으로 잡힘")


def test_resume_preserves_original_apply_time():
    # 2026-09-18 실측 사례 그대로 - apply 05:16:50, 재개 시점(now_utc)까지
    # 125초 경과. compute_effective_start_mono가 돌려주는 기준점 기준으로
    # "지금(now_mono)까지의 경과시간"을 계산하면 125초가 나와야 한다 -
    # 재개 시점을 0초로 잘못 잡으면 안 됨.
    applied_at = datetime(2026, 9, 18, 5, 16, 50, tzinfo=timezone.utc)
    resumed_at_utc = datetime(2026, 9, 18, 5, 18, 55, tzinfo=timezone.utc)  # 125초 후
    now_mono = 1000.0  # 임의 monotonic 값(resume 프로세스 시작 시점)

    start_mono = compute_effective_start_mono(applied_at, resumed_at_utc, now_mono)
    elapsed_since_apply = now_mono - start_mono
    assert abs(elapsed_since_apply - 125.0) < 0.01

    # 재개 시점 그대로 30초가 더 흘렀다고 가정하면(같은 monotonic 시계 위에서)
    # apply 시각 기준 총 경과시간은 155초여야 한다.
    later_mono = now_mono + 30.0
    assert abs((later_mono - start_mono) - 155.0) < 0.01
    print("OK - resume 후에도 총 경과시간이 재실행 시점이 아니라 원래 apply 시각 기준으로 계산됨")


def test_no_gap_case_matches_simple_elapsed():
    # 관찰이 끊긴 적 없는 정상 케이스(resume 아님)에서는 그냥
    # "지금까지 걸린 시간"과 동일해야 한다 - already_elapsed_sec=0.
    applied_at = datetime(2026, 9, 18, 5, 16, 50, tzinfo=timezone.utc)
    now_utc = applied_at  # apply 직후 바로 시작(끊김 없음)
    start_mono = compute_effective_start_mono(applied_at, now_utc, now_mono=500.0)
    assert start_mono == 500.0
    print("OK - 끊김 없는 정상 케이스는 단순 경과시간 계산과 동일")


if __name__ == "__main__":
    test_normal_startup_probe_failure_is_not_risky()
    test_restart_count_increase_detected()
    test_oom_eviction_crashloop_detected()
    test_resume_preserves_original_apply_time()
    test_no_gap_case_matches_simple_elapsed()
    print("\n모두 통과")
