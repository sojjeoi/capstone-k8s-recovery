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

# §164 - 위 상수는 이 ServiceMonitor가 스크랩하는 vLLM 앱 자신의
# /metrics에서 나오는 queue/cache에만 근거가 있다. cpu/memory는
# cAdvisor 출처(container_cpu_usage_seconds_total/
# container_memory_working_set_bytes)라 kubelet의 별도 스크레이프
# 경로를 타는데, 이 저장소 gitops/ 전체에 그 주기를 정의하는 설정이
# 없다(전수 검색으로 확인 - §163 문서 정정 phase8-v2-instrumentation-
# precision-163.md §1.1 참고). None은 "이 저장소 근거로는 확인 불가"를
# 뜻하며, _raw_sample_staleness()가 이 경우 하한을 계산하지 않는다
# (미확인 숫자를 대입하지 않기 위함).
CONFIRMED_SCRAPE_INTERVAL_SEC = {
    "cpu": None,
    "memory": None,
    "queue": PROM_SCRAPE_INTERVAL_SEC,
    "cache": PROM_SCRAPE_INTERVAL_SEC,
}


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


def _raw_sample_staleness(pairs: list, query_received_at: datetime,
                           scrape_interval_sec: float = None) -> dict:
    """§164(§163 정정) - query_range가 돌려주는 timestamp는 PromQL
    **평가 격자 시각**이지 원본 스크레이프 시각 자체가 아니다 -
    Prometheus가 lookback 구간(기본값 5분) 안의 가장 최근 표본을 찾아
    그 격자 시각에 실어 돌려주기 때문에, 실제 스크레이프가 몇 분 전에
    멈췄어도(값이 안 바뀌어도) 매 평가 시각마다 "방금 갱신된 것처럼"
    보이는 값이 나올 수 있다(이 상태는 명시적 stale marker가 찍히거나,
    marker 없이도 lookback 자체가 만료되면 끝난다 - 두 메커니즘은
    다르다). 그래서 단일 "원본 표본 시각"을 단정하지 않고 `eval_grid_
    ts_utc`(평가 격자 시각)만 남긴다. `scrape_interval_sec`이 주어지면
    (이 저장소 gitops/ 설정으로 스크레이프 주기가 확인된 지표 -
    queue/cache) 그만큼 앞선 시각을 참고용 하한으로 계산하지만, 이것도
    lookback 때문에 물리적으로 보장된 경계는 아니다. `scrape_interval_
    sec`이 None이면(cpu/memory - 이 저장소로는 주기가 확인 안 됨)
    미확인 숫자로 하한을 계산하지 않고 `None`으로 명시한다. 표본이
    아예 없으면(Prometheus가 빈 시계열을 돌려준 경우) 전부 None +
    status="no_samples"로 명시한다 - 조용히 0이나 과거값으로 채우지
    않는다."""
    if not pairs:
        return {
            "raw_sample_status": "no_samples",
            "eval_grid_ts_utc": None,
            "sample_lag_at_query_sec": None,
            "scrape_interval_used_sec": scrape_interval_sec,
            "interval_based_lower_bound_utc": None,
        }
    last_ts_epoch = max(float(ts) for ts, _v in pairs)
    last_ts = datetime.fromtimestamp(last_ts_epoch, tz=timezone.utc)
    lag_sec = (query_received_at - last_ts).total_seconds()
    lower_bound = (None if scrape_interval_sec is None
                   else (last_ts - timedelta(seconds=scrape_interval_sec)).isoformat())
    return {
        "raw_sample_status": "ok",
        "eval_grid_ts_utc": last_ts.isoformat(),
        "sample_lag_at_query_sec": round(lag_sec, 3),
        "scrape_interval_used_sec": scrape_interval_sec,
        "interval_based_lower_bound_utc": lower_bound,
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
    """§163/§164 - extract_features()와 완전히 같은 feature 벡터를
    계산하되, **지표별** 쿼리 요청/응답 시각과 신선도 참고값
    (§_raw_sample_staleness - **평가 격자 시각 기반**, 원본 스크레이프
    시각을 검증된 방식으로 확정하지 않음, 함수 docstring 참고)을
    함께 반환한다. "range 쿼리가 요청한 구간"(start~end, 호출부가
    이미 알고 있음)과 "Prometheus 응답이 실제로 담고 있던 평가 격자
    시각/그로부터 계산한 경과 시간"을 구분하는 게 목적 - 후자를
    "원본 표본이 정확히 언제 찍혔는가"와 같다고 단정하지 않는다.
    `raw_sample_status="ok"`도 원본값의 신선함이나 유효성을 보장하지
    않는다 - 단지 그 격자 시각에 빈 응답이 아니었다는 뜻이다.
    §164 - 이전엔 4개 지표 전체를 감싸는 요청/응답 시각 1쌍뿐이었는데,
    어느 지표 조회가 오래 걸렸는지 구분할 수 없었다 - 이제 지표마다
    따로 남긴다.
    extract_features()는 이 함수의 "features" 값만 뽑는 얇은 래퍼로
    재정의했다 - 반환값 100% 동일 유지(기존 호출부 전부 무변경, 회귀
    테스트로 확인)."""
    features = []
    per_metric_provenance = {}
    for name, promql in METRICS.items():
        query_sent_at = datetime.now(timezone.utc)
        pairs = _query_range_with_timestamps(promql, start, end)
        query_received_at = datetime.now(timezone.utc)
        values = [v for _ts, v in pairs]
        mean, slope = _mean_slope(values)
        features.extend([mean, slope])
        per_metric_provenance[name] = {
            **_raw_sample_staleness(pairs, query_received_at, CONFIRMED_SCRAPE_INTERVAL_SEC[name]),
            "query_sent_at_utc": query_sent_at.isoformat(),
            "query_received_at_utc": query_received_at.isoformat(),
        }
    return {
        "features": features,
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
