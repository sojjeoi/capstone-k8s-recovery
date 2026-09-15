#!/usr/bin/env python3
"""3-way 비교용 고정 임계치 baseline. 제안 방식(Isolation Forest, score_server.py)과
정확히 같은 standby·promotion 인프라·평가 주기·cooldown을 쓰고, "이상 여부를
어떻게 판단하는가"만 다르게 한다(guideline.md 9-7절: 순수 탐지방식 비교의
통제변수 - 동일 BlueGreen standby 조건에서 고정 임계치 vs Isolation Forest).

임계치는 비교 실행 전에 정해 고정한다(9-6절: 결과를 본 뒤 제안 방식에 유리하게
사후 조정 금지). guideline.md 저장소 구조 설명에 이미 적혀있던 예시
("CPU>90% -> promote_preview")를 그대로 쓴다 - 비교 결과를 보기 전부터 기록돼
있던 값이라 사후 선택이 아니다. CPU 90%는 rollout.yaml의 실제 container CPU
limit(4코어, gitops/apps/vllm-serving/rollout.yaml)을 기준으로 환산한 절대값
(3.6코어)이다 - features.py의 cpu_mean은 container_cpu_usage_seconds_total의
rate라 코어 단위 절대값이지 백분율이 아니기 때문.

EVAL_INTERVAL_SEC/WINDOW_SEC/CONSECUTIVE_THRESHOLD/COOLDOWN_SEC/
post_to_recovery_policy()는 score_server.py에서 그대로 가져다 쓴다 - 값을
복붙하면 두 스크립트가 나중에 따로 바뀌어 비교 조건이 몰래 달라질 위험이
있어서, import로 묶어 절대 어긋날 수 없게 한다.
"""
import argparse
import sys
import time
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8")

from features import FEATURE_NAMES, extract_features
from score_server import (
    COOLDOWN_SEC,
    CONSECUTIVE_THRESHOLD,
    EVAL_INTERVAL_SEC,
    WINDOW_SEC,
    post_to_recovery_policy,
)

CPU_LIMIT_CORES = 4.0  # gitops/apps/vllm-serving/rollout.yaml resources.limits.cpu
CPU_THRESHOLD_PCT = 0.90  # guideline.md 저장소 구조에 사전 기록된 값 - 사후 튜닝 금지
CPU_THRESHOLD_CORES = CPU_LIMIT_CORES * CPU_THRESHOLD_PCT
CPU_MEAN_INDEX = FEATURE_NAMES.index("cpu_mean")


def evaluate() -> float:
    """지금 시각 기준 최근 WINDOW_SEC 구간의 평균 CPU 사용량(코어)을 반환."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=WINDOW_SEC)
    feats = extract_features(start, end)
    return feats[CPU_MEAN_INDEX]


def main(once: bool = False, experiment_run_id: str = None):
    consecutive_anomalous = 0
    last_signal_at = None

    while True:
        cpu_mean = evaluate()
        is_anomalous = cpu_mean > CPU_THRESHOLD_CORES
        consecutive_anomalous = consecutive_anomalous + 1 if is_anomalous else 0
        now = time.monotonic()

        status = "이상" if is_anomalous else "정상"
        print(f"[{datetime.now(timezone.utc).isoformat()}] cpu_mean={cpu_mean:.3f}코어 "
              f"(임계치 {CPU_THRESHOLD_CORES:.3f}) ({status}), 연속={consecutive_anomalous}")

        if consecutive_anomalous >= CONSECUTIVE_THRESHOLD:
            in_cooldown = last_signal_at is not None and (now - last_signal_at) < COOLDOWN_SEC
            if in_cooldown:
                print(f"  -> cooldown 중 (남은 {COOLDOWN_SEC - (now - last_signal_at):.0f}초) - 신호 스킵")
            else:
                post_to_recovery_policy(cpu_mean, experiment_run_id, detector="fixed_threshold")
                last_signal_at = now

        if once:
            return
        time.sleep(EVAL_INTERVAL_SEC)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="고정 임계치(CPU) baseline - 주기 평가 후 신호 발행")
    parser.add_argument("--once", action="store_true", help="한 번만 평가하고 종료(테스트용)")
    parser.add_argument("--run-id", default=None, help="Phase 8 오케스트레이터가 지정 - 미지정 시 감사기록이 adhoc으로 묶임")
    args = parser.parse_args()
    main(once=args.once, experiment_run_id=args.run_id)
