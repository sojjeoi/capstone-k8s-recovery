#!/usr/bin/env python3
"""Prometheus에서 시간창(start~end) 기준 특성 벡터를 뽑는다.

guideline.md 방향("이동평균·기울기")을 따라 지표별 평균(mean)과 구간 내
선형 기울기(slope)를 특성으로 쓴다. 최소 모델이라 지표 4개(K8s CPU/메모리,
vLLM 큐 길이/캐시 사용률)만 쓴다.
# ponytail: 재시작 횟수·OOM 등은 빠짐 - Phase 6 본 구현에서 필요해지면 추가.
"""
import argparse
import os
from datetime import datetime, timedelta, timezone

import numpy as np
import requests

# §92 - 환경변수 override 추가(기본값 불변) - RECOVERY_POLICY_SIGNAL_URL과
# 동일 패턴. 실클러스터 없이 score_server.py를 실제 subprocess로 띄워
# deterministic fake Prometheus에 붙이는 로컬 synthetic E2E 테스트에 필요.
PROM_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
NAMESPACE = "vllm-serving"

METRICS = {
    "cpu": f'rate(container_cpu_usage_seconds_total{{namespace="{NAMESPACE}",container="vllm"}}[30s])',
    "memory": f'container_memory_working_set_bytes{{namespace="{NAMESPACE}",container="vllm"}}',
    "queue": "vllm:num_requests_waiting",
    "cache": "vllm:kv_cache_usage_perc",
}
FEATURE_NAMES = [f"{name}_{stat}" for name in METRICS for stat in ("mean", "slope")]


# §163 - Prometheus의 실제 스크레이프 주기(gitops/apps/vllm-serving/
# servicemonitor.yaml: interval: 15s, 코드로 재조회 불가한 K8s 리소스라
# 상수로 인용) - "원본 표본이 언제 참이 됐는가"의 하한을 계산하는 데만
# 쓴다(§163 _raw_sample_staleness() 참고). 실시간 판정 로직에는 안 쓰임.
PROM_SCRAPE_INTERVAL_SEC = 15


def _query_range_with_timestamps(promql: str, start: datetime, end: datetime, step: str = "15s") -> list:
    """§163 - _query_range()와 완전히 같은 쿼리·다중 시계열 합산을 하되,
    Prometheus가 실제로 돌려준 (timestamp, value) 쌍을 시각까지 보존해
    정렬된 리스트로 반환한다(timestamp를 버리지 않는 버전). _query_range()
    는 이 함수의 값만 뽑는 얇은 래퍼로 재정의했다 - 반환값·순서 100% 동일
    유지(기존 호출부 전부 무변경, 회귀 테스트로 확인)."""
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
    return sorted(by_ts.items())


def _query_range(promql: str, start: datetime, end: datetime, step: str = "15s") -> list:
    return [v for _ts, v in _query_range_with_timestamps(promql, start, end, step)]


def _raw_sample_staleness(pairs: list, query_received_at: datetime) -> dict:
    """§163 - "원본 표본이 정확히 언제 참이 됐는가"는 원리상 복원할 수
    없다: Prometheus 표본의 timestamp는 "언제 스크레이프됐는가"이지
    "언더라잉 값이 언제 바뀌었는가"가 아니다(스크레이프 사이 어느
    시점에 바뀌었어도 다음 스크레이프까지는 반영이 안 보인다). 그래서
    단일 시각을 단정하지 않고 상한(관측된 마지막 표본 시각)·하한(그
    보다 한 스크레이프 주기 앞)으로만 표현한다. 표본이 아예 없으면
    (Prometheus가 빈 시계열을 돌려준 경우) 전부 None + status=
    "no_samples"로 명시한다 - 조용히 0이나 과거값으로 채우지 않는다."""
    if not pairs:
        return {
            "raw_sample_status": "no_samples",
            "last_sample_ts_utc": None,
            "sample_lag_at_query_sec": None,
            "underlying_value_true_upper_bound_utc": None,
            "underlying_value_true_lower_bound_utc": None,
        }
    last_ts_epoch = max(float(ts) for ts, _v in pairs)
    last_ts = datetime.fromtimestamp(last_ts_epoch, tz=timezone.utc)
    lag_sec = (query_received_at - last_ts).total_seconds()
    return {
        "raw_sample_status": "ok",
        "last_sample_ts_utc": last_ts.isoformat(),
        "sample_lag_at_query_sec": round(lag_sec, 3),
        "underlying_value_true_upper_bound_utc": last_ts.isoformat(),
        "underlying_value_true_lower_bound_utc":
            (last_ts - timedelta(seconds=PROM_SCRAPE_INTERVAL_SEC)).isoformat(),
    }


def _mean_slope(values: list) -> tuple:
    if not values:
        return 0.0, 0.0
    if len(values) < 2:
        return values[0], 0.0
    x = np.arange(len(values))
    slope = np.polyfit(x, values, 1)[0]
    return float(np.mean(values)), float(slope)


def extract_features_with_provenance(start: datetime, end: datetime) -> dict:
    """§163 - extract_features()와 완전히 같은 feature 벡터를 계산하되,
    쿼리 요청·응답 시각과 지표별 원본 표본 신선도(§_raw_sample_staleness)
    를 함께 반환한다. "range 쿼리가 요청한 구간"(start~end, 호출부가
    이미 알고 있음)과 "Prometheus가 실제로 갖고 있던 원본 표본의 시각/
    나이"를 구분하는 게 목적 - 후자를 전자와 같다고 단정하지 않는다.
    extract_features()는 이 함수의 "features" 값만 뽑는 얇은 래퍼로
    재정의했다 - 반환값 100% 동일 유지(기존 호출부 전부 무변경, 회귀
    테스트로 확인)."""
    query_sent_at = datetime.now(timezone.utc)
    features = []
    per_metric_provenance = {}
    for name, promql in METRICS.items():
        pairs = _query_range_with_timestamps(promql, start, end)
        values = [v for _ts, v in pairs]
        mean, slope = _mean_slope(values)
        features.extend([mean, slope])
        per_metric_provenance[name] = pairs  # 아래에서 staleness로 변환(수신 시각 확정 후)
    query_received_at = datetime.now(timezone.utc)
    per_metric_provenance = {
        name: _raw_sample_staleness(pairs, query_received_at)
        for name, pairs in per_metric_provenance.items()
    }
    return {
        "features": features,
        "query_sent_at_utc": query_sent_at.isoformat(),
        "query_received_at_utc": query_received_at.isoformat(),
        "per_metric_provenance": per_metric_provenance,
    }


def extract_features(start: datetime, end: datetime) -> list:
    """start~end 구간의 특성 벡터를 FEATURE_NAMES 순서로 반환."""
    return extract_features_with_provenance(start, end)["features"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="주어진 UTC 구간의 특성 벡터를 출력")
    parser.add_argument("start", help="ISO8601 UTC 시작 시각")
    parser.add_argument("end", help="ISO8601 UTC 종료 시각")
    args = parser.parse_args()

    feats = extract_features(datetime.fromisoformat(args.start), datetime.fromisoformat(args.end))
    for name, val in zip(FEATURE_NAMES, feats):
        print(f"{name}: {val:.4f}")
