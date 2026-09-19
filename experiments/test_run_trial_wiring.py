#!/usr/bin/env python3
"""trial 러너(run_pod_kill_trial.py / run_load_ramp_trial.py / run_network_degrade_trial.py)의 arm
배선 검증(2026-09-19 추가).

non-native arm은 arm_controller(detector 기동·preview 준비+자동 rollback)를 절대 우회할 수 없어야
한다(fail-closed) - 예전 run_pod_kill_trial.py는 --arm 이름만 결과에 태깅할 뿐 detector·preview가
전혀 안 붙어서 그대로 돌리면 잘못 라벨링된 trial이 생겼고, run_load_ramp_trial.py의 같은 주장
("우회할 수 없다")도 테스트가 없었다(run_network_degrade_trial.py에도 같은 결함이 뒤늦게 확인돼 같은
방식으로 고쳤다). run_once()를 mock으로 대체해 main()이 실제로 무엇을 run_once()에 넘기는지만 본다 -
클러스터·서브프로세스·네트워크는 건드리지 않는다(Detector는 start()가 불릴 때만 프로세스를 띄우는데
여기선 run_once()가 mock이라 절대 안 불린다. network_degrade 러너가 실행 직전에 실클러스터의 probe
설정을 읽는 _verify_probe_profile도 여기선 patch로 막는다)."""
import contextlib
import importlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.stdout.reconfigure(encoding="utf-8")

import arm_controller
from run_once import Injector

RUNNERS = [
    pytest.param("run_pod_kill_trial", "make_pod_kill_injector", "pod_kill", id="pod_kill"),
    pytest.param("run_load_ramp_trial", "make_load_ramp_injector", "load_ramp", id="load_ramp"),
    pytest.param("run_network_degrade_trial", "make_network_degrade_injector", "network_degrade",
                 id="network_degrade"),
    pytest.param("run_memory_pressure_trial", "make_memory_pressure_injector", "memory_pressure",
                 id="memory_pressure"),
]
EXPECTED_DETECTOR = {"fixed_threshold": "fixed_threshold", "proposed": "isolation_forest"}
# run_memory_pressure_trial.py는 --size-mb/--workers/--duration-sec이 필수라(우발적 기본 실행 방지,
# memory_pressure_adapter.py의 stages 필수화와 같은 이유) 이 값들 없이는 argparse 단계에서부터
# main()이 실패한다 - 다른 세 러너는 이런 필수 인자가 없으므로 빈 튜플(영향 없음).
REQUIRED_EXTRA_ARGV = {
    "run_memory_pressure_trial": ("--size-mb", "500", "--workers", "1", "--duration-sec", "60"),
}


def _run_main(module_name, injector_factory, arm, extra_argv=("--pilot",)):
    mod = importlib.import_module(module_name)
    original = Injector(prepare=lambda: None, inject=lambda: None, is_started=lambda: True,
                         is_effective=lambda: True, is_done=lambda: True, cleanup=lambda: None)
    fake_result = SimpleNamespace(outcome="recovered", state="completed", t_injection=None,
                                  injection_observation_error_sec=None, t_slo=None, t_recovery=None,
                                  target_replaced=False)
    # arm_controller는 러너 모듈의 속성이 아니라 직접 import한 모듈을 patch한다 - 배선이 없는 러너에서도
    # AttributeError가 아니라 "detector가 None" 같은 의도한 단언으로 실패하게 하기 위함.
    with contextlib.ExitStack() as stack:
        make_injector = stack.enter_context(patch.object(mod, injector_factory, return_value=original))
        make_prober = stack.enter_context(patch.object(mod, "make_load_ramp_prober", return_value=MagicMock()))
        mock_run_once = stack.enter_context(patch.object(mod, "run_once", return_value=fake_result))
        spy_detector = stack.enter_context(patch.object(
            arm_controller, "make_detector_for_arm", wraps=arm_controller.make_detector_for_arm))
        if hasattr(mod, "_verify_probe_profile"):  # 실클러스터 pod의 probe 설정을 읽는 사전 검증 - 차단
            stack.enter_context(patch.object(mod, "_verify_probe_profile"))
        argv = [module_name, "--arm", arm, *extra_argv, *REQUIRED_EXTRA_ARGV.get(module_name, ())]
        stack.enter_context(patch.object(sys, "argv", argv))
        mod.main()
    assert mock_run_once.call_count == 1
    kwargs = mock_run_once.call_args.kwargs
    # injector·prober도 run_once와 같은 run_id로 만들어져야 한다(서로 다른 run_id가 섞이면 산출물이 갈라진다).
    assert kwargs["run_id"] in make_injector.call_args.args, "injector가 다른 run_id로 만들어짐"
    assert kwargs["run_id"] in make_prober.call_args.args, "prober가 다른 run_id로 만들어짐"
    return kwargs, original, spy_detector


@pytest.mark.parametrize("arm", ["fixed_threshold", "proposed"])
@pytest.mark.parametrize("module_name,injector_factory,scenario", RUNNERS)
def test_non_native_arm_always_goes_through_orchestration(module_name, injector_factory, scenario, arm):
    kwargs, original, spy_detector = _run_main(module_name, injector_factory, arm)

    detector = kwargs.get("detector")  # 배선이 없는 러너는 detector 인자 자체를 안 넘긴다
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
    assert kwargs.get("detector") is None, "native는 detector 없음(계약서 §1)"
    assert kwargs["injector"].get_preview_prep_info is None, "native는 preview 준비·정리 래퍼가 걸리면 안 됨"
    assert kwargs["arm"] == "native"


@pytest.mark.parametrize("module_name,injector_factory,scenario", RUNNERS)
def test_runner_passes_rollout_and_namespace_to_preview_prep(module_name, injector_factory, scenario):
    with patch.object(arm_controller, "wrap_injector_with_preview_prep",
                      side_effect=lambda inj, arm, rollout, namespace: inj) as spy_wrap:
        _run_main(module_name, injector_factory, "proposed", extra_argv=("--pilot", "--rollout", "r1", "--namespace", "n1"))
    assert spy_wrap.call_count == 1, "러너가 preview 준비 래퍼를 배선하지 않음"
    assert spy_wrap.call_args.args[1] == "proposed", "arm이 정확히 전달돼야 함"
    assert spy_wrap.call_args.args[2:] == ("r1", "n1")


@pytest.mark.parametrize("module_name,injector_factory,scenario", RUNNERS)
def test_runner_defaults_rollout_and_namespace_to_vllm_serving(module_name, injector_factory, scenario):
    with patch.object(arm_controller, "wrap_injector_with_preview_prep",
                      side_effect=lambda inj, arm, rollout, namespace: inj) as spy_wrap:
        _run_main(module_name, injector_factory, "fixed_threshold")
    assert spy_wrap.call_count == 1, "러너가 preview 준비 래퍼를 배선하지 않음"
    assert spy_wrap.call_args.args[2:] == ("vllm-serving", "vllm-serving")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
