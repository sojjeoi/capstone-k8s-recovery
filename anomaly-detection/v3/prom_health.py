#!/usr/bin/env python3
"""§80.5 하니스 안전 보완 - Prometheus port-forward transport 계층
health check + read-only 조회 전용 bounded retry. `calib3-low-02`
port-forward 단절 사고(§80.2) 재발 방지용, 부하·feature·SLO·모델 의미는
전혀 바꾸지 않는다. `qualify_normal_profile.py`(세션 시작 전/feature
extraction 직전 health 확인)와 `model_v32b/historical_reextraction.py`
(offline 복구)가 공용으로 쓴다."""
import time
from datetime import datetime
from typing import Callable, Optional

import requests

PROM_URL = "http://localhost:9090"


def check_prometheus_reachable(prom_url: str = PROM_URL, timeout: float = 5.0) -> dict:
    """TCP 연결 여부가 아니라 실제 query 호출과 status=success 응답까지
    확인한다(반쯤 끊긴 터널이 TCP는 받아줘도 응답을 못 주는 경우까지
    잡기 위함)."""
    try:
        r = requests.get(f"{prom_url}/api/v1/query", params={"query": "up"}, timeout=timeout)
        r.raise_for_status()
        body = r.json()
        return {"reachable": body.get("status") == "success", "http_status": r.status_code, "error": None}
    except Exception as e:  # noqa: BLE001 - health check는 원인 불문 reachable=False로 fail-closed
        return {"reachable": False, "http_status": None, "error": str(e)}


def query_range_with_bounded_retry(promql: str, start: datetime, end: datetime, step: str = "15s",
                                    max_retries: int = 3, retry_delay_sec: float = 5.0,
                                    query_range_fn: Optional[Callable] = None,
                                    sleep_fn: Callable = time.sleep) -> list:
    """매 retry는 동일 UTC 범위·동일 query를 다시 던질 뿐, 이전 시도의
    부분 응답과 합치거나 보간하지 않는다(한 시도는 성공 아니면 완전
    실패 중 하나). 전부 실패하면 예외(호출부가 completeness 실패로
    처리 - 0 대체·보간 없음)."""
    if query_range_fn is None:
        from features import _query_range as query_range_fn  # noqa: PLC0415 - 순환 임포트 회피, 지연 로딩
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            return query_range_fn(promql, start, end, step)
        except Exception as e:  # noqa: BLE001 - read-only 조회 재시도 대상, 원인 불문
            last_error = e
            if attempt < max_retries:
                sleep_fn(retry_delay_sec)
    raise RuntimeError(f"bounded retry {max_retries}회 모두 실패: {last_error}") from last_error
