#!/usr/bin/env python3
"""trial 러너(run_pod_kill_trial.py / run_load_ramp_trial.py)의 arm 배선 검증(2026-09-19 추가).

non-native arm은 arm_controller(detector 기동·preview 준비+자동 rollback)를 절대 우회할 수 없어야
한다(fail-closed) - 예전 run_pod_kill_trial.py는 --arm 이름만 결과에 태깅할 뿐 detector·preview가
전혀 안 붙어서 그대로 돌리면 잘못 라벨링된 trial이 생겼고, run_load_ramp_trial.py의 같은 주장
("우회할 수 없다")도 테스트가 없었다. run_once()를 mock으로 대체해 main()이 실제로 무엇을
run_once()에 넘기는지만 본다 - 클러스터·서브프로세스·네트워크는 건드리지 않는다(Detector는 start()가
불릴 때만 프로세스를 띄우는데 여기선 run_once()가 mock이라 절대 안 불린다)."""
import importlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.stdout.reconfigure(encoding="utf-8")

from run_once import Injector

RUNNERS = [
    pytest.param("run_pod_kill_trial", "make_pod_kill_injector", "pod_kill", id="pod_kill"),
    pytest.param("run_load_ramp_trial", "make_load_ramp_injector", "load_ramp", id="load_ramp"),
]
EXPECTED_DETECTOR = {"fixed_threshold": "fixed_threshold", "proposed": "isolation_forest"}


def _run_main(module_name, injector_factory, arm, extra_argv=("--pilot",)):
    mod = importlib.import_module(module_name)
    original = Injector(prepare=lambda: None, inject=lambda: None, is_started=lambda: True,
                         is_effective=lambda: True, is_done=lambda: True, cleanup=lambda: None)
    fake_result = SimpleNamespace(outcome="recovered", state="completed", t_injection=None,
                                  injection_observation_error_sec=None, t_slo=None, t_recovery=None)
    with patch.object(mod, injector_factory, return_value=original), \
         patch.object(mod, "make_load_ramp_prober", return_value=MagicMock()), \
         patch.object(mod, "run_once", return_value=fake_result) as mock_run_once, \
         patch.object(mod.arm_controller, "make_detector_for_arm",
                      wraps=mod.arm_controller.make_detector_for_arm) as spy_detector, \
         patch.object(sys, "argv", [module_name, "--arm", arm, *extra_argv]):
        mod.main()
    assert mock_run_once.call_count == 1
    return mock_run_once.call_args.kwargs, original, spy_detector


@pytest.mark.parametrize("arm", ["fixed_threshold", "proposed"])
@pytest.mark.parametrize("module_name,injector_factory,scenario", RUNNERS)
def test_non_native_arm_always_goes_through_orchestration(module_name, injector_factory, scenario, arm):
    kwargs, original, spy_detector = _run_main(module_name, injector_factory, arm)

    detector = kwargs["detector"]
    assert detector is not None, "non-native arm은 detector가 반드시 붙어야 함(없으면 잘못 라벨링된 trial)"
    assert detector.name == EXPECTED_DETECTOR[arm], "arm별 정확한 단일 detector"
    spy_detector.assert_called_once_with(arm, kwargs["run_id"])  # detector 서브프로세스에 이 trial의 run_id가 전달됨

    injector = kwargs["injector"]
    assert injector is original, "wrap은 같은 injector 객체를 감싸 돌려줌"
    assert injector.get_preview_prep_info is not None, "preview 준비+자동 rollback 래퍼가 걸려 있어야 함"
    assert injector.prepare is not None and injector.cleanup is not None

    assert kwargs["is_pilot"] is True and kwargs["arm"] == arm and kwargs["scenario"] == scenario
    assert kwargs["run_id"].startswith(f"pilot-{scenario}-{arm}-01-"), kwargs["run_id"]


@pytest.mark.parametrize("module_name,injector_factory,scenario", RUNNERS)
def test_native_arm_has_no_detector_and_no_preview_wrapper(module_name, injector_factory, scenario):
    kwargs, original, _ = _run_main(module_name, injector_factory, "native")
    assert kwargs["detector"] is None, "native는 detector 없음(계약서 §1)"
    assert kwargs["injector"].get_preview_prep_info is None, "native는 preview 준비·정리 래퍼가 걸리면 안 됨"
    assert kwargs["arm"] == "native"


@pytest.mark.parametrize("module_name,injector_factory,scenario", RUNNERS)
def test_runner_passes_rollout_and_namespace_to_preview_prep(module_name, injector_factory, scenario):
    mod = importlib.import_module(module_name)
    with patch.object(mod.arm_controller, "wrap_injector_with_preview_prep",
                      side_effect=lambda inj, arm, rollout, namespace: inj) as spy_wrap:
        _run_main(module_name, injector_factory, "proposed", extra_argv=("--pilot", "--rollout", "r1", "--namespace", "n1"))
    assert spy_wrap.call_args.args[2:] == ("r1", "n1")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
