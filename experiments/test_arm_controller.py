#!/usr/bin/env python3
"""arm_controller.py 검증(2026-09-18 추가, arm orchestration 보완) - 실제
fixed_threshold.py/score_server.py(Prometheus·모델 파일 의존)나 실클러스터
없이 오프라인으로 돈다. 서브프로세스 생명주기 자체는 _subprocess_detector()에
trivial한 python -c 커맨드를 직접 넣어 검증하고, preview 준비는
prepare_preview_fn을, RECOVERY_POLICY_SIGNAL_URL reachability(2026-09-19
추가)는 reachability_check_fn을 가짜로 주입해 검증한다."""
import sys
import time
from unittest.mock import MagicMock, patch

sys.stdout.reconfigure(encoding="utf-8")

from arm_controller import (
    _DETECTOR_SCRIPTS,
    _build_detector_command,
    _prometheus_reachable_and_fresh,
    _resolved_signal_url,
    _subprocess_detector,
    make_detector_for_arm,
    wrap_injector_with_preview_prep,
)
from run_once import HarnessCorrupted, Injector, TrialInvalid


def test_make_detector_for_arm_native_returns_none():
    assert make_detector_for_arm("native", "run-1") is None
    print("OK - native는 detector가 없음(None)")


def test_make_detector_for_arm_fixed_threshold_dispatches_correct_script():
    cmd = _build_detector_command("fixed_threshold", "run-1")
    assert cmd is not None
    assert cmd[-3] == "fixed_threshold.py" or cmd[-3].endswith("fixed_threshold.py"), cmd
    assert "score_server.py" not in " ".join(cmd), "fixed_threshold arm인데 score_server.py가 섞이면 안 됨"
    detector = make_detector_for_arm("fixed_threshold", "run-1")
    assert detector.name == "fixed_threshold"
    print("OK - fixed_threshold arm은 fixed_threshold.py + detector.name='fixed_threshold'만 반환")


def test_make_detector_for_arm_proposed_dispatches_correct_script():
    cmd = _build_detector_command("proposed", "run-1")
    assert cmd is not None
    assert cmd[-3] == "score_server.py" or cmd[-3].endswith("score_server.py"), cmd
    assert "fixed_threshold.py" not in " ".join(cmd), "proposed arm인데 fixed_threshold.py가 섞이면 안 됨"
    detector = make_detector_for_arm("proposed", "run-1")
    assert detector.name == "isolation_forest"
    print("OK - proposed arm은 score_server.py + detector.name='isolation_forest'만 반환")


def test_detector_script_dispatch_table_has_exactly_two_non_native_arms():
    # native를 제외한 두 arm만 detector를 가지며, 서로 다른 스크립트를
    # 가리켜야 한다(구조적으로 arm과 detector가 어긋날 수 없음을 보장).
    assert set(_DETECTOR_SCRIPTS.keys()) == {"fixed_threshold", "proposed"}
    assert _DETECTOR_SCRIPTS["fixed_threshold"]["script"] != _DETECTOR_SCRIPTS["proposed"]["script"]
    assert _DETECTOR_SCRIPTS["fixed_threshold"]["name"] != _DETECTOR_SCRIPTS["proposed"]["name"]
    print("OK - detector 매핑 테이블이 native 제외 2개 arm만 담고, 서로 다른 스크립트/이름을 가짐")


def test_run_id_propagated_into_detector_command():
    cmd = _build_detector_command("fixed_threshold", "load_ramp-fixed_threshold-01-20260101T000000Z")
    assert "--run-id" in cmd
    idx = cmd.index("--run-id")
    assert cmd[idx + 1] == "load_ramp-fixed_threshold-01-20260101T000000Z"
    print("OK - run_id가 --run-id 인자로 정확히 전파됨")


def test_subprocess_detector_lifecycle_start_alive_stop():
    # trivial한 오래 도는 프로세스로 start/is_alive/stop 자체의 정확성만 검증.
    detector = _subprocess_detector([sys.executable, "-c", "import time; time.sleep(30)"], "trivial")
    assert detector.is_alive() is False, "start() 전에는 살아있으면 안 됨"
    detector.start()
    time.sleep(0.3)
    assert detector.is_alive() is True, "start() 직후에는 살아있어야 함"
    detector.stop()
    assert detector.is_alive() is False, "stop() 이후에는 죽어있어야 함"
    detector.stop()  # idempotent 확인 - 두 번째 호출도 예외 없이 안전해야 함
    print("OK - 서브프로세스 detector의 start/is_alive/stop 생명주기 정확")


def test_subprocess_detector_crash_is_observed_as_not_alive():
    # 즉시 0이 아닌 코드로 죽는 프로세스 - is_alive()가 곧바로 False를 내야 한다.
    detector = _subprocess_detector([sys.executable, "-c", "import sys; sys.exit(1)"], "trivial")
    detector.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and detector.is_alive():
        time.sleep(0.05)
    assert detector.is_alive() is False, "즉시 종료되는 프로세스는 곧 is_alive()=False여야 함"
    print("OK - 서브프로세스가 크래시하면 is_alive()가 False로 관측됨")


def test_wrap_injector_with_preview_prep_native_passthrough():
    calls = {"prepare": 0}
    injector = Injector(prepare=lambda: calls.__setitem__("prepare", calls["prepare"] + 1),
                         inject=lambda: None, is_started=lambda: True, is_effective=lambda: True,
                         is_done=lambda: True, cleanup=lambda: None)
    wrapped = wrap_injector_with_preview_prep(injector, "native")
    wrapped.prepare()
    assert calls["prepare"] == 1
    print("OK - native는 preview 준비 없이 원본 prepare() 그대로 호출됨")


def test_wrap_injector_with_preview_prep_success_calls_original_prepare():
    calls = {"prepare": 0, "preview_fn_args": None}

    def fake_prepare_preview(name, namespace, timeout):
        calls["preview_fn_args"] = (name, namespace, timeout)
        return {"ready": True, "t_prep_start": "t0", "t_preview_ready": "t1",
                "prep_duration_sec": 12.3, "external_interference": False,
                "rollback_attempted": False, "rollback_ok": None, "aborted_pod_hash": None}

    injector = Injector(prepare=lambda: calls.__setitem__("prepare", calls["prepare"] + 1),
                         inject=lambda: None, is_started=lambda: True, is_effective=lambda: True,
                         is_done=lambda: True, cleanup=lambda: None)
    wrapped = wrap_injector_with_preview_prep(
        injector, "fixed_threshold", rollout_name="vllm-serving", namespace="vllm-serving",
        prepare_preview_fn=fake_prepare_preview, preview_prep_timeout_sec=480.0,
    )
    wrapped.prepare()
    assert calls["preview_fn_args"] == ("vllm-serving", "vllm-serving", 480.0)
    assert calls["prepare"] == 1, "preview 준비 성공 후에는 원본 prepare()도 호출돼야 함"
    info = wrapped.get_preview_prep_info()
    assert info["t_preview_ready"] == "t1" and info["prep_duration_sec"] == 12.3
    print("OK - preview 준비 성공 시 원본 prepare() 호출 + 진단 정보(get_preview_prep_info) 노출")


def test_wrap_injector_with_preview_prep_failure_blocks_original_prepare():
    # 핵심 회귀 테스트(방식 8) - preview 준비 실패 시 injector.inject()로
    # 이어질 원본 prepare() 자체가 호출되면 안 된다. rollback은 성공했다고
    # 가정(클러스터는 정상 복원) - 이 경우는 여전히 TrialInvalid여야 한다.
    calls = {"prepare": 0}

    def failing_prepare_preview(name, namespace, timeout):
        return {"ready": False, "t_prep_start": "t0", "t_preview_ready": None,
                "prep_duration_sec": 480.4, "external_interference": False,
                "rollback_attempted": True, "rollback_ok": True, "aborted_pod_hash": "previewXYZ"}

    injector = Injector(prepare=lambda: calls.__setitem__("prepare", calls["prepare"] + 1),
                         inject=lambda: None, is_started=lambda: True, is_effective=lambda: True,
                         is_done=lambda: True, cleanup=lambda: None)
    wrapped = wrap_injector_with_preview_prep(
        injector, "proposed", prepare_preview_fn=failing_prepare_preview,
    )

    raised = False
    try:
        wrapped.prepare()
    except TrialInvalid as e:
        raised = True
        assert "previewXYZ" in str(e) and "rollback 성공" in str(e)
    assert raised, "preview 준비 실패 + rollback 성공은 TrialInvalid여야 함(이 trial만 무효, 클러스터는 정상)"
    assert calls["prepare"] == 0, "preview 준비 실패 시 원본 prepare()(따라서 이후 inject())가 호출되면 안 됨"
    print("OK - preview 준비 실패+rollback 성공 시 TrialInvalid, 원본 prepare()/이후 injection 미실행")


def test_wrap_injector_with_preview_prep_rollback_failure_raises_harness_corrupted():
    # 2026-09-19 추가 - fixed_threshold pilot 01회 사고(preview 방치, Rollout
    # Paused/Degraded로 남음) 재발 방지. rollback 자체가 실패하면 이 trial만
    # 무효 처리하고 다음 trial로 넘어가면 안 된다 - HarnessCorrupted로 승격.
    def failing_rollback(name, namespace, timeout):
        return {"ready": False, "t_prep_start": "t0", "t_preview_ready": None,
                "prep_duration_sec": 480.2, "external_interference": False,
                "rollback_attempted": True, "rollback_ok": False, "aborted_pod_hash": "previewXYZ"}

    injector = Injector(prepare=lambda: None, inject=lambda: None, is_started=lambda: True,
                         is_effective=lambda: True, is_done=lambda: True, cleanup=lambda: None)
    wrapped = wrap_injector_with_preview_prep(injector, "fixed_threshold", prepare_preview_fn=failing_rollback)

    raised = False
    try:
        wrapped.prepare()
    except HarnessCorrupted as e:
        raised = True
        assert "previewXYZ" in str(e) and "rollback 실패" in str(e)
    except TrialInvalid:
        assert False, "rollback 실패는 TrialInvalid가 아니라 HarnessCorrupted여야 함"
    assert raised, "자동 rollback 자체가 실패하면 HarnessCorrupted를 던져야 함"
    print("OK - preview 준비 실패 + 자동 rollback도 실패 시 HarnessCorrupted로 승격")


def test_wrap_injector_with_preview_prep_external_interference_raises_harness_corrupted_without_rollback():
    # 2026-09-19 추가 - bump 직후 activeSelector가 예상 밖이면(다른 프로세스
    # 개입 가능성) 무엇이 "우리 preview"인지 특정 불가 - abort를 시도하지
    # 않고(이미 존재하던 preview를 함부로 제거하지 않음) HarnessCorrupted.
    calls = {"abort_would_be_called": False}

    def interfered_prepare(name, namespace, timeout):
        return {"ready": False, "t_prep_start": "t0", "t_preview_ready": None,
                "prep_duration_sec": 1.0, "external_interference": True,
                "rollback_attempted": False, "rollback_ok": None, "aborted_pod_hash": None}

    injector = Injector(prepare=lambda: calls.__setitem__("abort_would_be_called", True),
                         inject=lambda: None, is_started=lambda: True, is_effective=lambda: True,
                         is_done=lambda: True, cleanup=lambda: None)
    wrapped = wrap_injector_with_preview_prep(injector, "proposed", prepare_preview_fn=interfered_prepare)

    raised = False
    try:
        wrapped.prepare()
    except HarnessCorrupted:
        raised = True
    assert raised, "activeSelector 예상 밖 변경은 HarnessCorrupted여야 함"
    assert calls["abort_would_be_called"] is False, "원본 prepare()(따라서 injection)가 호출되면 안 됨"
    print("OK - 외부 개입 감지 시 rollback 시도 없이 HarnessCorrupted(fail-closed)")


def test_make_detector_for_arm_reachability_check_blocks_start_when_unreachable():
    detector = make_detector_for_arm("fixed_threshold", "run-1", reachability_check_fn=lambda url: False)
    raised = False
    try:
        detector.start()
    except TrialInvalid:
        raised = True
    assert raised, "RECOVERY_POLICY_SIGNAL_URL이 접근 불가능하면 TrialInvalid를 던져야 함"
    assert detector.is_alive() is False, "reachability 확인에 실패하면 detector 프로세스 자체를 띄우면 안 됨"
    print("OK - RECOVERY_POLICY_SIGNAL_URL 접근 불가 시 detector.start()가 TrialInvalid로 fail-closed(주입도 자동 차단)")


def test_make_detector_for_arm_reachability_check_passes_allows_start():
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None  # 계속 살아있는 것처럼
    with patch("arm_controller.subprocess.Popen", return_value=fake_proc) as mock_popen:
        detector = make_detector_for_arm("proposed", "run-1", reachability_check_fn=lambda url: True,
                                          prometheus_check_fn=lambda: True)
        detector.start()
        assert mock_popen.called, "reachability 통과 시 실제 서브프로세스 시작 시도까지 이어져야 함"
    print("OK - RECOVERY_POLICY_SIGNAL_URL 접근 가능하면 detector.start()가 정상적으로 서브프로세스를 시작함")


def test_prometheus_check_blocks_start_when_unreachable():
    # RECOVERY_POLICY 쪽은 통과시키고 Prometheus만 막아, 두 검사가 서로
    # 독립적으로 게이트한다는 걸 확인(2026-09-19 추가).
    detector = make_detector_for_arm("fixed_threshold", "run-1",
                                      reachability_check_fn=lambda url: True,
                                      prometheus_check_fn=lambda: False)
    raised = False
    try:
        detector.start()
    except TrialInvalid:
        raised = True
    assert raised, "Prometheus가 접근 불가/오래됐으면 TrialInvalid를 던져야 함"
    assert detector.is_alive() is False, "Prometheus 확인 실패 시 detector 프로세스를 띄우면 안 됨"
    print("OK - Prometheus 접근 불가 시 detector.start()가 TrialInvalid로 fail-closed(recovery-policy는 정상이어도)")


def test_prometheus_reachable_and_fresh_rejects_stale_or_missing_data():
    # 실제 네트워크 없이 requests.get 자체를 mocking해 "쿼리는 성공하지만
    # 표본이 오래됨"과 "표본 자체가 없음" 두 경우 모두 False가 나오는지 확인.
    import time as _time

    stale_resp = MagicMock()
    stale_resp.status_code = 200
    stale_resp.json.return_value = {
        "status": "success",
        "data": {"result": [{"value": [_time.time() - 999, "1.0"]}]},
    }
    with patch("arm_controller.requests.get", return_value=stale_resp):
        assert _prometheus_reachable_and_fresh(max_age_sec=120.0) is False

    empty_resp = MagicMock()
    empty_resp.status_code = 200
    empty_resp.json.return_value = {"status": "success", "data": {"result": []}}
    with patch("arm_controller.requests.get", return_value=empty_resp):
        assert _prometheus_reachable_and_fresh() is False

    fresh_resp = MagicMock()
    fresh_resp.status_code = 200
    fresh_resp.json.return_value = {
        "status": "success",
        "data": {"result": [{"value": [_time.time(), "1.0"]}]},
    }
    with patch("arm_controller.requests.get", return_value=fresh_resp):
        assert _prometheus_reachable_and_fresh() is True
    print("OK - Prometheus 응답의 표본 신선도(오래됨/없음/신선함)를 정확히 판별")


def test_resolved_signal_url_prefers_env_override():
    import os
    assert _resolved_signal_url() == "http://localhost:8080/signal"
    os.environ["RECOVERY_POLICY_SIGNAL_URL"] = "http://localhost:9999/signal"
    try:
        assert _resolved_signal_url() == "http://localhost:9999/signal"
    finally:
        del os.environ["RECOVERY_POLICY_SIGNAL_URL"]
    print("OK - RECOVERY_POLICY_SIGNAL_URL 환경변수가 있으면 그 값을, 없으면 로컬 기본값을 씀")


if __name__ == "__main__":
    test_make_detector_for_arm_native_returns_none()
    test_make_detector_for_arm_fixed_threshold_dispatches_correct_script()
    test_make_detector_for_arm_proposed_dispatches_correct_script()
    test_detector_script_dispatch_table_has_exactly_two_non_native_arms()
    test_run_id_propagated_into_detector_command()
    test_subprocess_detector_lifecycle_start_alive_stop()
    test_subprocess_detector_crash_is_observed_as_not_alive()
    test_wrap_injector_with_preview_prep_native_passthrough()
    test_wrap_injector_with_preview_prep_success_calls_original_prepare()
    test_wrap_injector_with_preview_prep_failure_blocks_original_prepare()
    test_wrap_injector_with_preview_prep_rollback_failure_raises_harness_corrupted()
    test_wrap_injector_with_preview_prep_external_interference_raises_harness_corrupted_without_rollback()
    test_make_detector_for_arm_reachability_check_blocks_start_when_unreachable()
    test_make_detector_for_arm_reachability_check_passes_allows_start()
    test_prometheus_check_blocks_start_when_unreachable()
    test_prometheus_reachable_and_fresh_rejects_stale_or_missing_data()
    test_resolved_signal_url_prefers_env_override()
    print("\n모두 통과")
