#!/usr/bin/env python3
"""§89.7 - run_v32b_lifecycle_aligned_smoke.py 오프라인 고정 테스트.
실제 서브프로세스·클러스터·live Prometheus 의존 없음 - `qualify_
normal_profile.run_candidate_with_retry`/`cleanup_unpromoted_preview`를
가짜로 대체해 DetectorLifecycleController의 순서·타이밍·복원 로직만
검증한다. §88.2에서 code-cited로 확정한 `run_once()`의 실제 순서
(baseline 확보 후에만 detector 시작, cleanup 직전에 정지)와 동일한지
확인하는 것이 이 파일의 핵심 목적이다."""
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))  # qualify_normal_profile.py는 v3/에 있음
sys.stdout.reconfigure(encoding="utf-8")

import qualify_normal_profile as qnp  # noqa: E402
import run_v32b_lifecycle_aligned_smoke as lifecycle  # noqa: E402


def test_detector_does_not_start_before_baseline_delay_elapses():
    """§88.2 code-cited 순서 - run_once()는 baseline_valid 확인 전에는
    detector.start()를 절대 호출하지 않는다(701-702행). 여기서는
    run_candidate_with_retry 진입 직후(=baseline 시작 시점 근사)부터
    0.3초 지연으로 흉내 내고, 그 전에는 detector가 시작되지 않았음을
    확인한다."""
    events = []

    def fake_run_candidate_with_retry(*args, **kwargs):
        events.append(("run_candidate_entered", time.monotonic()))
        time.sleep(0.5)
        events.append(("run_candidate_returned", time.monotonic()))
        return {"valid": True}

    def fake_cleanup(*args, **kwargs):
        events.append(("cleanup_called", time.monotonic()))
        return True

    def fake_start_detector():
        events.append(("detector_started", time.monotonic()))

    with patch.object(qnp, "run_candidate_with_retry", fake_run_candidate_with_retry), \
         patch.object(qnp, "cleanup_unpromoted_preview", fake_cleanup):
        controller = lifecycle.DetectorLifecycleController(
            Path("."), "v3.2b", "test-run", "http://127.0.0.1:1/signal",
            Path("/dev/null"), Path("/dev/null"), Path("/dev/null"),
            start_delay_sec=0.3, start_detector_fn=fake_start_detector)
        with controller:
            qnp.run_candidate_with_retry()
            qnp.cleanup_unpromoted_preview()

    names = [e[0] for e in events]
    assert names == ["run_candidate_entered", "detector_started", "run_candidate_returned", "cleanup_called"], names
    entered_t = dict(events)["run_candidate_entered"]
    started_t = dict(events)["detector_started"]
    # threading.Timer는 요청한 지연보다 아주 약간 일찍 콜백을 스케줄링할 수 있음(OS 스케줄러
    # 오차, 문서화된 특성) - 핵심은 "즉시 시작 안 함"이지 밀리초 단위 정밀도가 아니므로
    # 약간의 여유(10%)를 둔다.
    assert started_t - entered_t >= 0.3 * 0.9, "detector가 지연 시간보다 일찍 시작되면 안 됨"
    print("OK - detector가 run_candidate 진입 후 정확한 지연(baseline 확보 근사) 이후에만 시작됨 - "
          "run_once()의 'baseline 확보 전 detector 없음' 규율과 동일")


def test_detector_stops_before_cleanup_runs():
    """§88.2 code-cited 순서 - run_once()는 detector.stop()(931-933행)을
    injector.cleanup()(953행)보다 먼저 호출한다. 여기서는 cleanup_
    unpromoted_preview 자체가 collect_qualification_session()의 첫
    cleanup 호출이므로, 그 감싸진 버전이 실제 cleanup 로직을 부르기
    전에 detector 정지가 먼저 기록되는지 확인한다."""
    events = []

    def fake_run_candidate_with_retry(*args, **kwargs):
        time.sleep(0.3)  # 지연(0.05초)보다 충분히 길게 - 실제로는 120초 vs 600초+로 여유가 큼
        return {"valid": True}

    def fake_cleanup(*args, **kwargs):
        events.append("original_cleanup_called")
        return True

    def fake_start_detector():
        events.append("detector_started")

    with patch.object(qnp, "run_candidate_with_retry", fake_run_candidate_with_retry), \
         patch.object(qnp, "cleanup_unpromoted_preview", fake_cleanup):
        controller = lifecycle.DetectorLifecycleController(
            Path("."), "v3.2b", "test-run", "http://127.0.0.1:1/signal",
            Path("/dev/null"), Path("/dev/null"), Path("/dev/null"),
            start_delay_sec=0.05, start_detector_fn=fake_start_detector)
        with controller:
            qnp.run_candidate_with_retry()
            qnp.cleanup_unpromoted_preview()
        assert controller.t_detector_stop_utc is not None

    assert events == ["detector_started", "original_cleanup_called"], events
    print("OK - detector 정지가 실제 cleanup 로직 호출보다 먼저 기록됨 - "
          "run_once()의 'detector.stop()이 injector.cleanup()보다 먼저' 규율과 동일")


def test_functions_restored_after_context_exit_even_on_exception():
    original_cleanup = qnp.cleanup_unpromoted_preview

    def fake_run_candidate_with_retry(*args, **kwargs):
        raise RuntimeError("simulated failure")

    with patch.object(qnp, "run_candidate_with_retry", fake_run_candidate_with_retry):
        controller = lifecycle.DetectorLifecycleController(
            Path("."), "v3.2b", "test-run", "http://127.0.0.1:1/signal",
            Path("/dev/null"), Path("/dev/null"), Path("/dev/null"),
            start_delay_sec=10.0, start_detector_fn=lambda: None)
        try:
            with controller:
                qnp.run_candidate_with_retry()
        except RuntimeError:
            pass

    assert qnp.cleanup_unpromoted_preview == original_cleanup
    print("OK - 예외가 나도 __exit__에서 원래 함수로 복원됨(측정 파이프라인 영구 변경 없음)")


def test_timer_cancelled_if_run_candidate_fails_before_delay_elapses():
    """지연 시간이 되기 전에 run_candidate_with_retry 자체가 실패하면
    detector가 아예 시작되지 않아야 한다(신호 없는 세션에 detector를
    억지로 붙이지 않음)."""
    started = {"flag": False}

    def fake_run_candidate_with_retry(*args, **kwargs):
        raise RuntimeError("baseline_violating 등으로 즉시 실패")

    def fake_start_detector():
        started["flag"] = True

    with patch.object(qnp, "run_candidate_with_retry", fake_run_candidate_with_retry):
        controller = lifecycle.DetectorLifecycleController(
            Path("."), "v3.2b", "test-run", "http://127.0.0.1:1/signal",
            Path("/dev/null"), Path("/dev/null"), Path("/dev/null"),
            start_delay_sec=5.0, start_detector_fn=fake_start_detector)
        try:
            with controller:
                qnp.run_candidate_with_retry()
        except RuntimeError:
            pass
        time.sleep(0.1)

    assert started["flag"] is False
    print("OK - 지연 시간 전에 run_candidate가 실패하면 detector가 시작되지 않음(취소됨)")


def test_classify_lifecycle_phase_precise_uses_actual_detector_times():
    session = {
        "ramp_candidate_result": {
            "stages": [{"stage_start_utc": "2026-01-01T00:02:00+00:00", "stage_end_utc": "2026-01-01T00:12:00+00:00"}],
        },
    }
    t_start = datetime.fromisoformat("2026-01-01T00:02:00+00:00")
    t_stop = datetime.fromisoformat("2026-01-01T00:13:00+00:00")

    cases = [
        (t_start - timedelta(seconds=30), "pre_detector_start(baseline_or_earlier)"),
        (t_start + timedelta(seconds=1), "steady(stage)"),
        (datetime.fromisoformat("2026-01-01T00:12:30+00:00"), "drain(detector_active_post_injection)"),
        (t_stop + timedelta(seconds=1), "post_detector_stop(cleanup_or_later)"),
    ]
    for ts, expected in cases:
        result = lifecycle._classify_lifecycle_phase_precise(ts, t_start, t_stop, session)
        assert result == expected, (ts, result, expected)
    print("OK - 실제 detector on/off 시각을 최우선으로 쓰는 정밀 lifecycle 분류가 정확함")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        t()
    print(f"전체 통과 ({len(tests)}개)")
