#!/usr/bin/env python3
"""§88.6 - run_v32b_no_action_smoke.py 오케스트레이션 로직 오프라인 고정
테스트. 실제 서브프로세스·클러스터·live Prometheus 의존 없음 - 순수
함수(lifecycle 분류, 포트 확인, PID 격리)만 검증한다."""
import socket
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

import run_v32b_no_action_smoke as smoke  # noqa: E402


def _t(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


FAKE_SESSION = {
    "preview_prep_info": {
        "t_prep_start": "2026-01-01T00:00:00+00:00",
        "t_preview_ready": "2026-01-01T00:03:00+00:00",
    },
    "ramp_candidate_result": {
        "stages": [
            {"stage_start_utc": "2026-01-01T00:06:00+00:00", "stage_end_utc": "2026-01-01T00:16:00+00:00"},
        ],
    },
    "t_session_end": "2026-01-01T00:18:00+00:00",
}


def test_classify_lifecycle_phase_all_six_buckets():
    cases = [
        ("2025-12-31T23:59:00+00:00", "pre_prep"),
        ("2026-01-01T00:01:00+00:00", "preview_prep"),
        ("2026-01-01T00:04:00+00:00", "settle"),
        ("2026-01-01T00:10:00+00:00", "stage"),
        ("2026-01-01T00:17:00+00:00", "drain"),
        ("2026-01-01T00:20:00+00:00", "post_session"),
    ]
    for iso, expected in cases:
        result = smoke._classify_lifecycle_phase(_t(iso), FAKE_SESSION)
        assert result == expected, (iso, result, expected)
    print("OK - preview_prep/settle/stage/drain/post_session 6개 구간이 session 자체 타임스탬프로 정확히 분류됨")


def test_classify_lifecycle_phase_stage_window_matches_feature_rows_scope():
    """§88 Task 4 - Training/Calibration/Holdout의 feature_rows는 stage
    구간만 커버한다(qualify_normal_profile.collect_qualification_session
    의 CandidateSession 구성과 동일 경계) - 'stage'로 분류되는 구간이
    정확히 그 경계와 일치해야, "steady measurement window만 학습에
    쓰였다"는 §87.4/§88 주장을 코드로 재확인할 수 있다."""
    stage_start = _t(FAKE_SESSION["ramp_candidate_result"]["stages"][0]["stage_start_utc"])
    stage_end = _t(FAKE_SESSION["ramp_candidate_result"]["stages"][0]["stage_end_utc"])
    assert smoke._classify_lifecycle_phase(stage_start, FAKE_SESSION) == "stage"
    assert smoke._classify_lifecycle_phase(stage_end - timedelta(seconds=1), FAKE_SESSION) == "stage"
    assert smoke._classify_lifecycle_phase(stage_end, FAKE_SESSION) == "drain"
    print("OK - 'stage' 분류 경계가 feature_rows 생성에 쓰이는 CandidateSession 경계와 정확히 일치")


def test_assert_port_free_raises_when_bound():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        port = holder.getsockname()[1]
        holder.listen(1)
        try:
            smoke._assert_port_free(port)
            assert False, "이미 점유된 포트인데 통과하면 안 됨"
        except RuntimeError as e:
            assert "이미 점유" in str(e)
    print("OK - 이미 점유된 포트는 sink 기동 전에 fail-closed로 걸러짐")


def test_assert_port_free_passes_when_free():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    smoke._assert_port_free(port)  # 예외 없이 통과해야 함
    print("OK - 실제로 비어 있는 포트는 정상 통과")


def test_assert_sink_distinct_from_real_url_rejects_same_port():
    try:
        smoke._assert_sink_distinct_from_real_url("http://localhost:8080/signal")
        assert False, "실제 recovery-policy와 같은 포트인데 통과하면 안 됨"
    except RuntimeError:
        pass
    print("OK - sink URL이 실제 recovery-policy와 같은 포트면 fail-closed")


def test_stop_subprocess_only_touches_its_own_handle():
    """§88 Task 6 - cleanup이 자기 PID만 종료하는지: `_stop_subprocess()`가
    전달받은 handle의 proc 객체에만 terminate()/wait()를 호출하고, 다른
    handle에는 전혀 손대지 않아야 한다."""
    proc_a = MagicMock()
    proc_a.poll.return_value = None  # 아직 살아있음
    proc_a.wait.return_value = None
    proc_a.returncode = 0
    handle_a = {"proc": proc_a, "pid": 111, "_stdout_f": MagicMock(), "_stderr_f": MagicMock()}

    proc_b = MagicMock()
    handle_b = {"proc": proc_b, "pid": 222, "_stdout_f": MagicMock(), "_stderr_f": MagicMock()}

    smoke._stop_subprocess(handle_a, "proc_a")

    proc_a.terminate.assert_called_once()
    proc_b.terminate.assert_not_called()
    proc_b.poll.assert_not_called()
    print("OK - _stop_subprocess()는 전달받은 handle의 프로세스만 건드리고 다른 handle은 전혀 건드리지 않음")


def test_stop_subprocess_reports_exit_status():
    proc = MagicMock()
    # 첫 poll (진입 시): 살아있음 -> terminate 호출됨 -> wait 이후 poll: 종료됨
    proc.poll.side_effect = [None, 0]
    proc.returncode = 0
    handle = {"proc": proc, "pid": 333, "_stdout_f": MagicMock(), "_stderr_f": MagicMock()}
    result = smoke._stop_subprocess(handle, "proc")
    assert result["exited_cleanly"] is True
    assert result["pid"] == 333
    print("OK - 정상 종료 시 exited_cleanly=True와 pid가 report에 기록됨")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        t()
    print(f"전체 통과 ({len(tests)}개)")
