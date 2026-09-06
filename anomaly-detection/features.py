#!/usr/bin/env python3
"""Prometheus에서 시간창(start~end) 기준 특성 벡터를 뽑는다.

guideline.md 방향("이동평균·기울기")을 따라 지표별 평균(mean)과 구간 내
선형 기울기(slope)를 특성으로 쓴다. 최소 모델이라 지표 4개(K8s CPU/메모리,
vLLM 큐 길이/캐시 사용률)만 쓴다.
# ponytail: 재시작 횟수·OOM 등은 빠짐 - Phase 6 본 구현에서 필요해지면 추가.
"""
import argparse
from datetime import datetime

import numpy as np
import requests

PROM_URL = "http://localhost:9090"
NAMESPACE = "vllm-serving"

METRICS = {
    "cpu": f'rate(container_cpu_usage_seconds_total{{namespace="{NAMESPACE}",container="vllm"}}[30s])',
    "memory": f'container_memory_working_set_bytes{{namespace="{NAMESPACE}",container="vllm"}}',
    "queue": "vllm:num_requests_waiting",
    "cache": "vllm:kv_cache_usage_perc",
}
FEATURE_NAMES = [f"{name}_{stat}" for name in METRICS for stat in ("mean", "slope")]


def _query_range(promql: str, start: datetime, end: datetime, step: str = "15s") -> list:
    r = requests.get(
        f"{PROM_URL}/api/v1/query_range",
        params={"query": promql, "start": start.timestamp(), "end": end.timestamp(), "step": step},
        timeout=30,
    )
    r.raise_for_status()
    result = r.json()["data"]["result"]
    if not result:
        return []
    # 여러 시계열(active+preview 동시 구동 등으로 pod 여러 개)이 나오면 합산.
    by_ts = {}
    for series in result:
        for ts, val in series["values"]:
            by_ts[ts] = by_ts.get(ts, 0.0) + float(val)
    return [by_ts[ts] for ts in sorted(by_ts)]


def _mean_slope(values: list) -> tuple:
    if not values:
        return 0.0, 0.0
    if len(values) < 2:
        return values[0], 0.0
    x = np.arange(len(values))
    slope = np.polyfit(x, values, 1)[0]
    return float(np.mean(values)), float(slope)


def extract_features(start: datetime, end: datetime) -> list:
    """start~end 구간의 특성 벡터를 FEATURE_NAMES 순서로 반환."""
    features = []
    for promql in METRICS.values():
        mean, slope = _mean_slope(_query_range(promql, start, end))
        features.extend([mean, slope])
    return features


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="주어진 UTC 구간의 특성 벡터를 출력")
    parser.add_argument("start", help="ISO8601 UTC 시작 시각")
    parser.add_argument("end", help="ISO8601 UTC 종료 시각")
    args = parser.parse_args()

    feats = extract_features(datetime.fromisoformat(args.start), datetime.fromisoformat(args.end))
    for name, val in zip(FEATURE_NAMES, feats):
        print(f"{name}: {val:.4f}")
