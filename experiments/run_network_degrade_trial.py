#!/usr/bin/env python3
"""network_degrade 시나리오 단일 trial 수동 실행 - run_pod_kill_trial.py와
같은 패턴. probe는 동일하게 make_load_ramp_prober(범용 Prober 팩토리)를
재사용한다.

--probe-profile로 "연쇄장애(default) vs 순수 열화(network_tolerant)" 두
실험을 분리한다(발견 5 - K8s 기본 timeoutSeconds=1초로는 NetworkChaos
지연이 liveness/readinessProbe 자체를 실패시켜 false-positive 재시작이
난다, gitops/apps/recovery-policy/deployment.yaml:5-6에 이미 기록된 교훈이
vllm-serving Rollout엔 아직 반영 안 됨). 이 스크립트는 overlay를 직접
적용/승격하지 않는다 - 그건 별도의 명시적 실클러스터 작업이다(gitops/
overlays/vllm-serving-network-tolerant/ 참고). 대신 실행 직전에 현재 active
pod의 실제 probe timeoutSeconds를 읽어 요청한 profile과 일치하는지
검증한다 - 안 맞으면 즉시 중단한다(라벨과 실제 설정이 어긋난 채로 trial이
기록되는 걸 막기 위함, 3-tier fallback 없이 fail-closed).

TIMEOUT_SEC=900은 load_ramp의 실측 보정값(900초)을 그대로 가져온 잠정값이다
- network_degrade 자체 주입이 4단계x90초=360초라 pod_kill의 600초보다
더 큰 여유가 필요하지만, 실제로 필요한 값은 이번 첫 실측 실행 결과를 보고
조정해야 한다(pod_kill_trial.py의 600초와 같은 성격의 잠정값).

non-native arm은 arm_controller 배선(detector 기동 + preview 준비·자동 rollback)을 절대 우회할 수
없다(fail-closed, 2026-09-19 추가) - 예전엔 --arm 이름만 결과에 태깅될 뿐 detector·preview가 전혀
안 붙어서 run_pod_kill_trial.py가 고친 것과 같은 결함이 이 러너에도 있었다(test_run_trial_wiring.py가
세 러너 모두를 고정). native는 원본 injector와 detector 없음을 그대로 유지한다.
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path

from kubernetes.client.exceptions import ApiException

import arm_controller
import slo_judge
from active_pod_resolver import get_active_pods, load_kube_config
from load_ramp_adapter import make_load_ramp_prober
from network_degrade_adapter import make_network_degrade_injector
from run_once import HarnessCorrupted, run_once

DEFAULT_PROBE_CONFIG = Path(__file__).parent.parent / "chaos" / "probe-config.yaml"
TIMEOUT_SEC = 900
DEFAULT_PROFILE_TIMEOUT_SEC = 1.0  # K8s가 admission 시 채우는 기본값(발견 5)


class ProbeProfileMismatch(Exception):
    """요청한 probe profile과 실클러스터 active pod의 실제 probe
    timeoutSeconds가 다를 때 - 라벨이 실제 설정과 어긋난 채 trial이
    기록되는 걸 막기 위해 fail-closed."""


def _verify_probe_profile(expected_timeout_sec: float) -> None:
    pods = get_active_pods()
    if len(pods) != 1:
        raise ProbeProfileMismatch(
            f"active pod이 정확히 1개가 아님({len(pods)}개) - profile 검증 불가")
    from kubernetes import client
    load_kube_config()
    core = client.CoreV1Api()
    try:
        pod = core.read_namespaced_pod(pods[0]["name"], "vllm-serving")
    except ApiException as e:
        raise ProbeProfileMismatch(f"active pod 조회 실패: {e}")
    container = pod.spec.containers[0]
    actual = {
        "readinessProbe": getattr(container.readiness_probe, "timeout_seconds", None),
        "livenessProbe": getattr(container.liveness_probe, "timeout_seconds", None),
    }
    for probe_name, actual_timeout in actual.items():
        if actual_timeout != expected_timeout_sec:
            raise ProbeProfileMismatch(
                f"{probe_name}.timeoutSeconds 실측값({actual_timeout})이 요청한 "
                f"profile의 기대값({expected_timeout_sec})과 다름 - overlay 적용/승격이 "
                f"안 됐거나 잘못된 profile을 지정했을 수 있음. 먼저 gitops/overlays/"
                f"vllm-serving-network-tolerant/를 적용·승격했는지 확인할 것.")


def main():
    parser = argparse.ArgumentParser(description="network_degrade 시나리오 단일 trial 실행")
    parser.add_argument("--arm", default="native", choices=["native", "fixed_threshold", "proposed"])
    parser.add_argument("--rep", type=int, default=1)
    parser.add_argument("--probe-config", default=str(DEFAULT_PROBE_CONFIG))
    parser.add_argument("--timeout-sec", type=float, default=TIMEOUT_SEC)
    parser.add_argument("--sequence-index", type=int, default=1)
    parser.add_argument("--order-seed", type=int, default=1)
    parser.add_argument("--pilot", action="store_true",
                         help="파일럿 실행 표시 - run_id에 pilot- 접두어를 붙이고 "
                              "is_pilot=True로 기록해 results/pilot/ 아래 구조적으로 분리")
    parser.add_argument("--probe-profile", default="default", choices=["default", "network_tolerant"],
                         help="K8s readiness/livenessProbe.timeoutSeconds 설정 - "
                              "default=재시작 포함 연쇄장애 실험, "
                              "network_tolerant=순수 네트워크 열화 실험(calibration된 값 필요)")
    parser.add_argument("--readiness-probe-timeout-sec", type=float, default=None,
                         help="network_tolerant profile일 때 필수 - 실클러스터 calibration으로 "
                              "확정한 값(임의값 금지). overlay가 실제로 이 값을 적용했는지는 "
                              "이 스크립트가 실행 직전에 직접 검증한다.")
    parser.add_argument("--skip-profile-verification", action="store_true",
                         help="위험 - active pod의 실제 probe timeoutSeconds 검증을 건너뛴다. "
                              "오프라인 계약 확인 등 클러스터 없이 --help 이외의 목적으로 "
                              "쓸 이유가 없음(정상 실행에서는 절대 켜지 말 것).")
    parser.add_argument("--rollout", default="vllm-serving", help="non-native arm의 preview 준비 대상 Rollout 이름")
    parser.add_argument("--namespace", default="vllm-serving", help="non-native arm의 preview 준비 대상 namespace")
    args = parser.parse_args()

    if args.probe_profile == "network_tolerant" and args.readiness_probe_timeout_sec is None:
        parser.error("--probe-profile network_tolerant는 --readiness-probe-timeout-sec 필수")

    expected_timeout = (DEFAULT_PROFILE_TIMEOUT_SEC if args.probe_profile == "default"
                         else args.readiness_probe_timeout_sec)
    if not args.skip_profile_verification:
        _verify_probe_profile(expected_timeout)

    scenario = "network_degrade"
    prefix = "pilot-" if args.pilot else ""
    run_id = f"{prefix}{scenario}-{args.arm}-{args.rep:02d}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    injector = make_network_degrade_injector(run_id, args.arm, args.rep)
    # non-native arm은 이 배선을 절대 우회할 수 없다(fail-closed, 2026-09-19 추가) - run_pod_kill_trial.py·
    # run_load_ramp_trial.py와 같은 결함(--arm 이름만 결과에 태깅될 뿐 detector 기동·preview 준비·자동
    # rollback이 안 붙음)이 이 러너에도 있었다. native면 두 함수 모두 원본/None을 그대로 돌려준다.
    injector = arm_controller.wrap_injector_with_preview_prep(injector, args.arm, args.rollout, args.namespace)
    detector = arm_controller.make_detector_for_arm(args.arm, run_id)
    prober = make_load_ramp_prober(args.probe_config, run_id, scenario, args.arm, args.rep, args.timeout_sec)

    print(f"run_id: {run_id}" + (f" / detector: {detector.name}" if detector is not None else ""))
    print(f"probe_profile: {args.probe_profile} (readiness_probe_timeout_sec={expected_timeout})")
    try:
        result = run_once(
            scenario=scenario, arm=args.arm, rep=args.rep,
            sequence_index=args.sequence_index, order_seed=args.order_seed,
            injector=injector, prober=prober, timeout_sec=args.timeout_sec,
            detector=detector,
            run_id=run_id, is_pilot=args.pilot,
            latency_slo_sec=slo_judge.LATENCY_THRESHOLD, slo_version=slo_judge.SLO_VERSION,
            min_observation_sec=slo_judge.WINDOW_SEC,
            readiness_probe_profile=args.probe_profile,
            readiness_probe_timeout_sec=expected_timeout,
        )
    except HarnessCorrupted as e:
        print(f"HARNESS CORRUPTED: {e}")
        raise

    print(f"outcome: {result.outcome} / state: {result.state}")
    print(f"t_injection={result.t_injection} "
          f"injection_observation_error_sec={result.injection_observation_error_sec}")
    print(f"t_slo={result.t_slo} t_recovery={result.t_recovery}")
    if result.target_replaced:
        print(f"target_replaced=True at {result.t_target_replaced} "
              f"(replacement={result.target_replacement_pod_name}/{result.target_replacement_pod_uid}) - "
              f"invalid_run 아님, {args.probe_profile} profile 맥락에서 해석할 것")
    result_dir = "results/pilot" if args.pilot else "results"
    print(f"결과 파일: {result_dir}/trial-{run_id}.json")


if __name__ == "__main__":
    main()
