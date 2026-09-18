#!/usr/bin/env python3
"""arm_controller.py 검증(2026-09-18 추가, arm orchestration 보완) - 실제
fixed_threshold.py/score_server.py(Prometheus·모델 파일 의존)나 실클러스터
없이 오프라인으로 돈다. 서브프로세스 생명주기 자체는 _subprocess_detector()에
trivial한 python -c 커맨드를 직접 넣어 검증하고, preview 준비는
prepare_preview_fn을 가짜로 주입해 검증한다."""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from arm_controller import (
    _DETECTOR_SCRIPTS,
    _build_detector_command,
    _subprocess_detector,
    make_detector_for_arm,
    wrap_injector_with_preview_prep,
)
from run_once import Injector, TrialInvalid


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

    def fake_prepare_preview(name, namespace):
        calls["preview_fn_args"] = (name, namespace)
        return True

    injector = Injector(prepare=lambda: calls.__setitem__("prepare", calls["prepare"] + 1),
                         inject=lambda: None, is_started=lambda: True, is_effective=lambda: True,
                         is_done=lambda: True, cleanup=lambda: None)
    wrapped = wrap_injector_with_preview_prep(
        injector, "fixed_threshold", rollout_name="vllm-serving", namespace="vllm-serving",
        prepare_preview_fn=fake_prepare_preview,
    )
    wrapped.prepare()
    assert calls["preview_fn_args"] == ("vllm-serving", "vllm-serving")
    assert calls["prepare"] == 1, "preview 준비 성공 후에는 원본 prepare()도 호출돼야 함"
    print("OK - preview 준비 성공 시 원본 prepare()까지 정상 호출됨")


def test_wrap_injector_with_preview_prep_failure_blocks_original_prepare():
    # 핵심 회귀 테스트(방식 8) - preview 준비 실패 시 injector.inject()로
    # 이어질 원본 prepare() 자체가 호출되면 안 된다.
    calls = {"prepare": 0}

    def failing_prepare_preview(name, namespace):
        return False

    injector = Injector(prepare=lambda: calls.__setitem__("prepare", calls["prepare"] + 1),
                         inject=lambda: None, is_started=lambda: True, is_effective=lambda: True,
                         is_done=lambda: True, cleanup=lambda: None)
    wrapped = wrap_injector_with_preview_prep(
        injector, "proposed", prepare_preview_fn=failing_prepare_preview,
    )

    raised = False
    try:
        wrapped.prepare()
    except TrialInvalid:
        raised = True
    assert raised, "preview 준비 실패는 TrialInvalid를 던져야 함"
    assert calls["prepare"] == 0, "preview 준비 실패 시 원본 prepare()(따라서 이후 inject())가 호출되면 안 됨"
    print("OK - preview 준비 실패 시 TrialInvalid, 원본 prepare()/이후 injection 미실행")


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
    print("\n모두 통과")
