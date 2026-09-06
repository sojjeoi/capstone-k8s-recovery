#!/usr/bin/env python3
"""정상 regime을 여러 번 반복 수집 - Isolation Forest 학습 표본을 늘리기
위함(guideline.md 9-4절: 정상 상태 다양성·개수). Pod 재시작이 들어가는
warmup/post_startup/active_preview_concurrent는 매 반복이 실제 서비스에
영향을 주므로 여기서는 안 다룬다 - 부하 기반(idle/load 계열)만 반복.

Phase 8의 "3-way x 4종 x 5회 반복"(9-6절)에도 이 반복 패턴을 그대로
재사용할 수 있게 일반적으로 짰다.
"""
import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "experiments"))

from collect_regime import record
from run_ramp_in_cluster import run_in_cluster

CONFIGS_DIR = Path(__file__).parent / "regime_configs"
RESULTS_DIR = Path(__file__).parent.parent / "chaos" / "loadgen" / "results"

# (regime 이름, config 파일 - None이면 순수 대기) 목록
LOAD_REGIMES = {
    "idle": None,
    "low_load": CONFIGS_DIR / "low-load.yaml",
    "sustained_load": CONFIGS_DIR / "sustained-load.yaml",
    "burst": CONFIGS_DIR / "burst.yaml",
}
IDLE_DURATION_SEC = 60


def collect_one(regime: str, rep: int) -> None:
    config = LOAD_REGIMES[regime]
    start = datetime.now(timezone.utc).isoformat()
    if config is None:
        print(f"[{regime} rep{rep}] {IDLE_DURATION_SEC}초 대기...")
        time.sleep(IDLE_DURATION_SEC)
    else:
        print(f"[{regime} rep{rep}] in-cluster 실행...")
        run_in_cluster(str(config), None, "manual", str(rep), RESULTS_DIR)
    end = datetime.now(timezone.utc).isoformat()
    record(regime, start, end, f"추가 수집 rep{rep}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="정상 regime을 여러 번 반복 수집")
    parser.add_argument("--regimes", nargs="+", default=list(LOAD_REGIMES), choices=list(LOAD_REGIMES))
    parser.add_argument("--reps", type=int, default=3)
    args = parser.parse_args()

    for regime in args.regimes:
        for rep in range(1, args.reps + 1):
            collect_one(regime, rep)

    print(f"\n완료 - {args.regimes} 각 {args.reps}회 추가 수집됨")
