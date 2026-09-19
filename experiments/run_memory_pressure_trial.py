#!/usr/bin/env python3
"""memory_pressure 시나리오 단일 trial 수동 실행 - run_pod_kill_trial.py/
run_network_degrade_trial.py와 같은 패턴. probe는 동일하게 make_load_ramp_prober
(범용 Prober 팩토리)를 재사용한다.

memory_pressure는 network_degrade와 달리 **기본 readiness/liveness probe
설정만 쓴다** - network-tolerant 같은 overlay 개념이 없다(2026-09-20 지시).
메모리 압박 중 liveness restart가 나는 것 자체가 이 시나리오의 관찰
대상이지만(계약서 §48 안전장치와는 별개 축), OOMKilled·Node 이상은 여전히
안전 실패로 즉시 중단한다(memory_pressure_adapter.py가 담당). 낮은 강도
smoke에서는 restartCount 증가도 실패로 처리한다 - 이후 본격 calibration에서
liveness restart를 유효한 장애 효과로 허용할지는 이번 smoke 결과를 보고
별도로 결정한다(이 러너는 그 결정을 내리지 않는다).

`--size-mb`/`--workers`/`--duration-sec`는 전부 **필수 인자다(기본값 없음)** -
memory_pressure_adapter.py의 stages 필수화와 같은 이유(우발적 기본 실행
방지). 이 러너는 항상 **단일 stage**만 만든다 - 여러 강도를 순차 시도하는
ramp 실행은 아직 지시 범위 밖(`run_all_scenarios.py`처럼 향후 별도 구현)이다.

TIMEOUT_SEC=1140(19분)은 계약서 §4의 memory_pressure 예산(14분 주입 + 5분
관찰 여유)을 그대로 가져온 값이다 - 그 예산 자체가 옛 5-stage/5000MB 설계
기준이라 잠정 무효로 표시돼 있지만(§48.3), 강도 재설계가 끝나기 전까지는
이 값을 상한으로 그대로 쓴다(단일 60초 stage smoke에는 넉넉한 여유).
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path

import arm_controller
import slo_judge
from load_ramp_adapter import make_load_ramp_prober
from memory_pressure_adapter import make_memory_pressure_injector
from run_once import HarnessCorrupted, run_once

DEFAULT_PROBE_CONFIG = Path(__file__).parent.parent / "chaos" / "probe-config.yaml"
TIMEOUT_SEC = 1140
MAX_ALLOWED_SIZE_MB = 1000  # 1GB 이상 탐색 금지(계약서 §48 지시) - 이 러너 레벨에서도 fail-closed


def main():
    parser = argparse.ArgumentParser(description="memory_pressure 시나리오 단일 trial 실행(단일 stage)")
    parser.add_argument("--arm", default="native", choices=["native", "fixed_threshold", "proposed"])
    parser.add_argument("--rep", type=int, default=1)
    parser.add_argument("--probe-config", default=str(DEFAULT_PROBE_CONFIG))
    parser.add_argument("--timeout-sec", type=float, default=TIMEOUT_SEC)
    parser.add_argument("--sequence-index", type=int, default=1)
    parser.add_argument("--order-seed", type=int, default=1)
    parser.add_argument("--pilot", action="store_true",
                         help="파일럿 실행 표시 - run_id에 pilot- 접두어를 붙이고 "
                              "is_pilot=True로 기록해 results/pilot/ 아래 구조적으로 분리")
    parser.add_argument("--size-mb", type=float, required=True,
                         help="StressChaos memory stressor 크기(MB, 필수) - 기본값 없음. "
                              "1GB(1000MB) 이상은 이번 지시 범위 밖(별도 calibration 전 금지)")
    parser.add_argument("--workers", type=int, required=True, help="StressChaos memory stressor worker 수(필수)")
    parser.add_argument("--duration-sec", type=float, required=True,
                         help="이 단일 stage의 지속시간(초, 필수) - 실제 CR duration은 여기에 "
                              "안전 여유(memory_pressure_adapter.STAGE_DURATION_SAFETY_MARGIN_SEC)를 "
                              "더해 설정된다")
    parser.add_argument("--rollout", default="vllm-serving", help="non-native arm의 preview 준비 대상 Rollout 이름")
    parser.add_argument("--namespace", default="vllm-serving", help="non-native arm의 preview 준비 대상 namespace")
    args = parser.parse_args()

    if args.size_mb >= MAX_ALLOWED_SIZE_MB:
        parser.error(
            f"--size-mb {args.size_mb}는 1GB({MAX_ALLOWED_SIZE_MB}MB) 이상 - 지시 범위 밖(500MB 최소 강도 "
            f"smoke 이후 별도 calibration 전까지 1GB 이상 탐색 금지, fail-closed)")

    scenario = "memory_pressure"
    prefix = "pilot-" if args.pilot else ""
    run_id = f"{prefix}{scenario}-{args.arm}-{args.rep:02d}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    stages = [{"name": f"stage-1-{args.size_mb:.0f}mb", "size_mb": args.size_mb,
               "workers": args.workers, "duration_sec": args.duration_sec}]

    injector = make_memory_pressure_injector(run_id, args.arm, args.rep, stages=stages)
    # non-native arm은 이 배선을 절대 우회할 수 없다(fail-closed) - 나머지 세 러너와 동일한
    # arm_controller 배선(detector 기동 + preview 준비·자동 rollback). native면 두 함수 모두
    # 원본/None을 그대로 돌려준다.
    injector = arm_controller.wrap_injector_with_preview_prep(injector, args.arm, args.rollout, args.namespace)
    detector = arm_controller.make_detector_for_arm(args.arm, run_id)
    prober = make_load_ramp_prober(args.probe_config, run_id, scenario, args.arm, args.rep, args.timeout_sec)

    print(f"run_id: {run_id}" + (f" / detector: {detector.name}" if detector is not None else ""))
    print(f"stage: size_mb={args.size_mb} workers={args.workers} duration_sec={args.duration_sec}")
    try:
        result = run_once(
            scenario=scenario, arm=args.arm, rep=args.rep,
            sequence_index=args.sequence_index, order_seed=args.order_seed,
            injector=injector, prober=prober, timeout_sec=args.timeout_sec,
            detector=detector,
            run_id=run_id, is_pilot=args.pilot,
            latency_slo_sec=slo_judge.LATENCY_THRESHOLD, slo_version=slo_judge.SLO_VERSION,
            min_observation_sec=slo_judge.WINDOW_SEC,
            # 기본 30초보다 넉넉히 잡는다(2026-09-20, live smoke 실측 발견) - is_started()가
            # AllInjected뿐 아니라 실제 working set 상승까지 확인하는데, StressChaos 자체는
            # 즉시 적용돼도 kubelet->cAdvisor->Prometheus 스크레이프 경로에 지연이 있다
            # (이번 smoke 실측 약 18초). run_once.py는 injector.is_effective()를 is_started()
            # 확정 직후 단 한 번만 확인하므로, 이 재시도 예산 자체를 넉넉히 둬야 한다.
            injection_started_timeout_sec=60.0,
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
              f"invalid_run 아님, 낮은 강도 smoke에서는 그래도 실패로 취급할 것(계약서 §48 PASS 기준)")
    result_dir = "results/pilot" if args.pilot else "results"
    print(f"결과 파일: {result_dir}/trial-{run_id}.json")
    print(f"안전 로그(evidence): {result_dir}/memory-pressure-safety-{run_id}.jsonl")


if __name__ == "__main__":
    main()
