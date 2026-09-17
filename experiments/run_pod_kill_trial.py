#!/usr/bin/env python3
"""pod_kill 시나리오 단일 trial 수동 실행 - run_load_ramp_trial.py와 같은
패턴("native x 1회로 주입/probe/SLO 기록만 확인" 검증용). pod_kill은 순간
액션이라 injector 쪽에 별도 scenario config가 필요 없다(pod_kill_adapter.py가
active Service selector로 대상을 동적으로 찾음) - probe는 load_ramp와 동일한
chaos/probe-config.yaml·make_load_ramp_prober()를 그대로 재사용한다(그 함수는
이름과 달리 load_ramp 고유 로직이 없고, 어떤 시나리오든 "probe.py 띄우고
slo_judge로 판정"만 하는 범용 Prober 팩토리다 - scenario/arm/rep은 태깅에만
쓰임).

TIMEOUT_SEC=600은 첫 실측 실행을 위한 잠정값이다(load_ramp의 900초처럼
반복 보정을 거친 값이 아님) - 대상 pod 소멸 후 replacement pod 스케줄링+
모델 로딩+Ready+60초 롤링 P95 정상화 확인까지 감안한 여유. 이번 실행에서
실제로 걸리는 시간을 보고 3-arm 파일럿/전체 배치 전에 조정할지 판단한다.

min_observation_sec=slo_judge.WINDOW_SEC를 넘긴다(2026-09-17 정정) - pod_kill
은 즉발 injector라 injector.is_done()이 주입 직후 바로 True가 되는데, 이
값 없이는 probe가 표본을 하나도 못 읽은 채로 "prevented"가 확정돼버린다
(첫 native 파일럿에서 실측 발견 - t_injection_end가 주입 0.7초 뒤에 찍혀서
관측이 37초 만에 끝났고, 그 시점 replacement pod는 여전히 Not Ready였다).
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path

import slo_judge
from load_ramp_adapter import make_load_ramp_prober
from pod_kill_adapter import make_pod_kill_injector
from run_once import HarnessCorrupted, run_once

DEFAULT_PROBE_CONFIG = Path(__file__).parent.parent / "chaos" / "probe-config.yaml"
TIMEOUT_SEC = 600


def main():
    parser = argparse.ArgumentParser(description="pod_kill 시나리오 단일 trial 실행")
    parser.add_argument("--arm", default="native", choices=["native", "fixed_threshold", "proposed"])
    parser.add_argument("--rep", type=int, default=1)
    parser.add_argument("--probe-config", default=str(DEFAULT_PROBE_CONFIG))
    parser.add_argument("--timeout-sec", type=float, default=TIMEOUT_SEC)
    parser.add_argument("--sequence-index", type=int, default=1)
    parser.add_argument("--order-seed", type=int, default=1)
    parser.add_argument("--pilot", action="store_true",
                         help="파일럿 실행 표시 - run_id에 pilot- 접두어를 붙이고 "
                              "is_pilot=True로 기록해 results/pilot/ 아래 구조적으로 분리")
    args = parser.parse_args()

    scenario = "pod_kill"
    prefix = "pilot-" if args.pilot else ""
    run_id = f"{prefix}{scenario}-{args.arm}-{args.rep:02d}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    injector = make_pod_kill_injector(run_id, args.arm, args.rep)
    prober = make_load_ramp_prober(args.probe_config, run_id, scenario, args.arm, args.rep, args.timeout_sec)

    print(f"run_id: {run_id}")
    try:
        result = run_once(
            scenario=scenario, arm=args.arm, rep=args.rep,
            sequence_index=args.sequence_index, order_seed=args.order_seed,
            injector=injector, prober=prober, timeout_sec=args.timeout_sec,
            run_id=run_id, is_pilot=args.pilot,
            latency_slo_sec=slo_judge.LATENCY_THRESHOLD,
            min_observation_sec=slo_judge.WINDOW_SEC,
        )
    except HarnessCorrupted as e:
        print(f"HARNESS CORRUPTED: {e}")
        raise

    print(f"outcome: {result.outcome} / state: {result.state}")
    print(f"t_injection={result.t_injection} "
          f"injection_observation_error_sec={result.injection_observation_error_sec}")
    print(f"t_slo={result.t_slo} t_recovery={result.t_recovery}")
    result_dir = "results/pilot" if args.pilot else "results"
    print(f"결과 파일: {result_dir}/trial-{run_id}.json")


if __name__ == "__main__":
    main()
