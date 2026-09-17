#!/usr/bin/env python3
"""load_ramp 시나리오 단일 trial 수동 실행 - "native x 1회로 주입/probe/SLO
기록만 확인" 검증용(4단계 완료 기준의 첫 단계, 사용자 지시). 이후 3-arm
파일럿(7단계)/전체 배치(8단계 run_all_scenarios.py)는 이 스크립트가 검증된
뒤 별도로 반복 호출한다.
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path

import slo_judge
from load_ramp_adapter import make_load_ramp_injector, make_load_ramp_prober
from run_once import HarnessCorrupted, run_once

DEFAULT_CONFIG = Path(__file__).parent.parent / "chaos" / "scenario-load-ramp.yaml"
DEFAULT_PROBE_CONFIG = Path(__file__).parent.parent / "chaos" / "probe-config.yaml"
# 계약서 §4(2026-09-16 개정): 450초 chaos + 최대 450초 후속 관찰 = 900초.
# 원래 600초(2.5분 버퍼)로는 60초 롤링 P95가 다 해소되기 전에 관찰이 끝나서
# recovered/timeout을 구분 못 하는 우측 검열(right-censoring)이 실측으로
# 확인됐다(파일럿 run_id=load_ramp-native-01-20260916T033817Z).
TIMEOUT_SEC = 900


def main():
    parser = argparse.ArgumentParser(description="load_ramp 시나리오 단일 trial 실행")
    parser.add_argument("--arm", default="native", choices=["native", "fixed_threshold", "proposed"])
    parser.add_argument("--rep", type=int, default=1)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--probe-config", default=str(DEFAULT_PROBE_CONFIG))
    parser.add_argument("--timeout-sec", type=float, default=TIMEOUT_SEC)
    parser.add_argument("--sequence-index", type=int, default=1)
    parser.add_argument("--order-seed", type=int, default=1)
    parser.add_argument("--pilot", action="store_true",
                         help="파일럿 실행 표시 - run_id에 pilot- 접두어를 붙이고 "
                              "is_pilot=True로 기록해 results/pilot/ 아래 구조적으로 "
                              "분리한다(collect_metrics.py 8단계가 본 실험 5회 반복 "
                              "집계에서 구조적으로 제외할 수 있게)")
    args = parser.parse_args()

    scenario = "load_ramp"
    prefix = "pilot-" if args.pilot else ""
    run_id = f"{prefix}{scenario}-{args.arm}-{args.rep:02d}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    injector = make_load_ramp_injector(args.config, run_id, args.arm, args.rep)
    prober = make_load_ramp_prober(args.probe_config, run_id, scenario, args.arm, args.rep, args.timeout_sec)

    print(f"run_id: {run_id}")
    try:
        result = run_once(
            scenario=scenario, arm=args.arm, rep=args.rep,
            sequence_index=args.sequence_index, order_seed=args.order_seed,
            injector=injector, prober=prober, timeout_sec=args.timeout_sec,
            run_id=run_id, is_pilot=args.pilot,
            latency_slo_sec=slo_judge.LATENCY_THRESHOLD,
            # 2026-09-17 추가 - pod_kill 회귀와 동일 기준으로 명시적 통일.
            # load_ramp는 injector.is_done()이 450초 램프 완료 후에만 True가
            # 되므로 이 값(60초)은 이미 한참 지나 있어 실제 동작은 그대로다.
            min_observation_sec=slo_judge.WINDOW_SEC,
        )
    except HarnessCorrupted as e:
        print(f"HARNESS CORRUPTED: {e}")
        raise

    print(f"outcome: {result.outcome} / state: {result.state}")
    print(f"t_injection={result.t_injection} t_slo={result.t_slo} t_recovery={result.t_recovery}")
    result_dir = "results/pilot" if args.pilot else "results"
    print(f"결과 파일: {result_dir}/trial-{run_id}.json")


if __name__ == "__main__":
    main()
