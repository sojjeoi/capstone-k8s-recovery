#!/usr/bin/env python3
"""§80 - `calib3-low-02` port-forward transport 장애 이후의 read-only
historical 복구 전용 모듈(§80.3/§80.4/§80.5 사전등록 그대로 구현).

측정·판정 의미는 절대 바꾸지 않는다 - 8-feature 추출은 `build_dataset.
build_rows_for_session()`(model_v31 프로토콜과 동일, model_v32b가 이미
재사용 중)을, stage/latency 판정은 `explore_ramp_intensity.
classify_stages()`/`bucket_stats()`/`slo_judge`를 전부 그대로 import해서
쓴다 - 이 파일이 새로 하는 일은 (1) 이미 끝난 측정의 원본 CSV로부터
`candidate_result`를 재구성하는 것, (2) raw completeness 검사(gap/NaN/
누락) 뿐이다. Prometheus 도달성 확인·bounded retry는 `prom_health.py`
(qualify_normal_profile.py와 공용)로 옮겼다 - 여기서는 그대로 재사용만
한다(중복 구현 없음)."""
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import requests

import sys

V3_DIR = Path(__file__).parent.parent
EXPERIMENTS_DIR = V3_DIR.parent.parent / "experiments"
sys.path.insert(0, str(EXPERIMENTS_DIR))
sys.path.insert(0, str(V3_DIR))
sys.path.insert(0, str(V3_DIR.parent))  # features.py

import slo_judge  # noqa: E402
from explore_ramp_intensity import bucket_stats, classify_stages, parse_ramp_summary  # noqa: E402
from build_dataset import build_rows_for_session  # noqa: E402
from windows import CandidateSession  # noqa: E402
from features import METRICS, PROM_URL, _query_range  # noqa: E402
from prom_health import check_prometheus_reachable, query_range_with_bounded_retry  # noqa: E402,F401 - 재수출(하위 호환)

EXPECTED_STEP_SEC = 15.0
MAX_ALLOWED_GAP_SEC = 2 * EXPECTED_STEP_SEC  # §80.4-7 - step의 2배를 넘는 gap은 비정상


def verify_metric_completeness(metric_name: str, promql: str, start: datetime, end: datetime,
                                step_sec: float = EXPECTED_STEP_SEC, prom_url: str = PROM_URL,
                                timeout: float = 30.0) -> dict:
    """§80.4의 4/6/7번 조건 - `_query_range()`는 timestamp를 버리고 값만
    반환하므로(features.py, 런타임 의미 불변 원칙상 손대지 않음), 여기선
    별도로 timestamp까지 받는 raw 조회를 직접 수행해 gap/NaN/개수를
    검사한다. feature 계산 자체와는 무관한 순수 진단 조회."""
    r = requests.get(f"{prom_url}/api/v1/query_range",
                      params={"query": promql, "start": start.timestamp(), "end": end.timestamp(),
                              "step": f"{step_sec:g}s"},
                      timeout=timeout)
    r.raise_for_status()
    result = r.json()["data"]["result"]
    by_ts = {}
    for series in result:
        for ts, val in series["values"]:
            by_ts[ts] = by_ts.get(ts, 0.0) + float(val)
    timestamps = sorted(by_ts)
    values = [by_ts[t] for t in timestamps]

    duration = (end - start).total_seconds()
    expected_n = math.floor(duration / step_sec) + 1
    has_nan_or_inf = any(math.isnan(v) or math.isinf(v) for v in values)
    gaps = [b - a for a, b in zip(timestamps, timestamps[1:])]
    max_gap = max(gaps) if gaps else 0.0

    ok = (len(timestamps) >= expected_n * 0.9  # 약간의 정렬 오차 허용, 임의 보간은 없음 - 미달이면 아래서 실패
          and not has_nan_or_inf
          and max_gap <= MAX_ALLOWED_GAP_SEC
          and len(timestamps) > 0)
    return {
        "metric_name": metric_name, "n_samples": len(timestamps), "expected_n_samples": expected_n,
        "has_nan_or_inf": has_nan_or_inf, "max_gap_sec": max_gap, "ok": ok,
    }


@dataclass
class ReextractionResult:
    session_id: str
    regime: str
    run_id: str
    start_utc: datetime
    end_utc: datetime
    candidate_result: dict
    t_slo: Optional[str]
    feature_rows: list
    completeness_checks: list
    invalid_window_count: int


def reextract_session(session_id: str, regime: str, probe_raw_csv: Path, ramp_summary_csv: Path,
                       query_range_fn: Callable = _query_range, prom_url: str = PROM_URL,
                       completeness_fn: Callable = verify_metric_completeness) -> ReextractionResult:
    """§80.4 - 이미 완료된 측정의 원본 CSV 두 개만으로 `candidate_result`를
    `explore_ramp_intensity.run_candidate()`의 후반부(같은 함수, 그대로
    import)와 동일하게 재구성한 뒤, `build_dataset.build_rows_for_session()`
    (변경 없음)으로 Prometheus에서 8-feature를 다시 뽑는다. 새 측정·새
    판정 로직 없음 - 전부 기존 frozen 코드 재사용."""
    rows = slo_judge.load_raw(probe_raw_csv)
    ramp_stages = parse_ramp_summary(ramp_summary_csv.read_text(encoding="utf-8"))
    buckets = classify_stages(rows, ramp_stages)

    run_id = ramp_stages[0]["experiment_run_id"] if ramp_stages else None
    candidate_result = {
        "run_id": run_id, "valid": True, "reason": None,
        "local_raw": str(probe_raw_csv), "local_ramp_summary": str(ramp_summary_csv),
        "baseline": bucket_stats(buckets["baseline"]),
        "stages": [{**s, **bucket_stats(bucket)} for s, bucket in buckets["stages"]],
        "drain": bucket_stats(buckets["drain"]),
    }
    all_buckets = [candidate_result["baseline"]] + candidate_result["stages"] + [candidate_result["drain"]]
    candidate_result["all_success_100pct"] = all(b["n"] and b["success_rate"] == 1.0 for b in all_buckets)

    points = slo_judge.evaluate(rows)
    t_slo_dt = slo_judge.find_t_slo(points)
    t_slo = t_slo_dt.isoformat() if t_slo_dt else None

    start_utc = candidate_result["stages"][0]["stage_start_utc"]
    end_utc = candidate_result["stages"][-1]["stage_end_utc"]
    cand = CandidateSession(session_id=session_id, regime=regime, topology="active_plus_preview",
                             start_utc=start_utc, end_utc=end_utc, source_run_id=run_id)
    feature_rows = build_rows_for_session(cand, query_range_fn)
    invalid_window_count = sum(1 for r in feature_rows if not r.valid)

    completeness_checks = [
        completeness_fn(name, promql, start_utc, end_utc, prom_url=prom_url)
        for name, promql in METRICS.items()
    ]

    return ReextractionResult(
        session_id=session_id, regime=regime, run_id=run_id, start_utc=start_utc, end_utc=end_utc,
        candidate_result=candidate_result, t_slo=t_slo, feature_rows=feature_rows,
        completeness_checks=completeness_checks, invalid_window_count=invalid_window_count,
    )


def verify_recovery_complete(result: ReextractionResult) -> dict:
    """§80.4 완전/불완전 판정 - 하나라도 불만족이면 실패(보간·0 대체
    없음)."""
    failures = []
    if result.invalid_window_count > 0:
        failures.append(f"invalid feature window {result.invalid_window_count}개")
    for c in result.completeness_checks:
        if not c["ok"]:
            failures.append(f"{c['metric_name']}: n={c['n_samples']}/{c['expected_n_samples']}, "
                             f"max_gap={c['max_gap_sec']}s, nan_or_inf={c['has_nan_or_inf']}")
    if not result.feature_rows:
        failures.append("feature_rows가 비어 있음")
    return {"complete": len(failures) == 0, "failures": failures}
