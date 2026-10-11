#!/usr/bin/env python3
"""2026-10-04 입력 무결성 수정안(오프라인 후보, 운영 미적용 - score_server.py는 이 모듈을 쓰지 않는다).

동결 버전(features.py·score_server.py·v3/prom_health.py)에서 재현된 세 문제를 한 묶음으로 다룬다.
1. 부분 시계열 합산: 기대 active·preview 중 일부만 있는 격자도 합산되어 정상 입력으로 통과한다.
2. 시간축 압축: _query_range가 시각을 버리고 _mean_slope가 np.arange를 써서, 빠진 격자가 있으면 기울기 단위가 바뀐다
   (시각 [0,15,45,60]·값 [0,1,3,4] → 1.4, 실제 15초 칸당 기울기 1.0).
3. 신선도: check_metric_freshness가 result[0]만 보고, 인스턴트 응답의 시각(= 질의 평가 시각)을 원시 표본 시각처럼 쓴다.

수정 원칙: 빠진 값을 채우지 않는다(보간·0·과거값 금지), 불완전한 입력은 표시하고 점수에 쓰지 않는다, 완전한 정상 입력에서는
기존 계산(합산·평균·"15초 격자 칸당" 기울기)과 값이 같다, 임계값·모델·연속 조건은 그대로다. 입력 보완 후보(CPU rate[60s])는
질의 문자열만 바꾸는 별도 설정으로 둔다(CPU_RATE_WINDOW_SEC).

Pod 정체성(이름만으로 UID를 확인했다고 표시하지 않는다):
- 응답 라벨에 UID 형태 값이 있으면(cAdvisor `id` 경로 등) 기대 UID와 같아야 한다. 다르면 identity_conflict로 거부.
- UID 라벨이 없는 지표(vLLM /metrics의 queue·cache 등)는 `runtime_uid_evidence`(실행 당시 기록에서 얻은 이름→UID 매핑:
  실행기 5초 표본의 원래 Pod UID, Pod 이벤트의 preview UID)가 기대 UID와 같을 때만 runtime_mapping으로 인정한다.
  증거가 없으면 identity_unverified로 판단하지 않는다."""
import math
import re
import time
from datetime import datetime
from typing import Optional

import numpy as np

from features import METRICS as FROZEN_METRICS  # 동결 버전 지표 순서 = FEATURE_NAMES 순서(cpu, memory, queue, cache)

GRID_STEP_SEC = 15.0           # features.py의 step="15s" - 기울기 단위("15초 격자 칸당")의 기준
GRID_TOL_SEC = 0.5
CPU_RATE_WINDOW_SEC = 60       # 후보(동결 버전 30). 근거: experiments/analysis_v1/cpu-input-candidate-20261004.md
DEFAULT_MAX_SAMPLE_AGE_SEC = 120.0  # score_server.FRESHNESS_MAX_AGE_SEC와 같은 값(새로 고르지 않음)
# 미래 시각 허용 오차: 노드(cAdvisor 원시 표본 시각)와 Prometheus 서버(평가 시각) 시계 차이를 측정한 근거가 아직 없어
# 기본 0초(평가 시각보다 늦은 표본 시각은 전부 future_timestamp). 병렬 기록 진단에서 (평가 시각 − 표본 시각) 분포를 보고
# 별도로 정하기 전에는 바꾸지 않는다.
DEFAULT_FUTURE_TOLERANCE_SEC = 0.0
REQUIRED_METRICS = tuple(FROZEN_METRICS)
_UID_RE = re.compile(r"[0-9a-f]{8}[-_][0-9a-f]{4}[-_][0-9a-f]{4}[-_][0-9a-f]{4}[-_][0-9a-f]{12}")


def grid_points(start: datetime, end: datetime, step_sec: float = GRID_STEP_SEC) -> list:
    s, e = start.timestamp(), end.timestamp()
    return [s + i * step_sec for i in range(int((e - s) / step_sec + GRID_TOL_SEC) + 1)]


def label_uids(labels: dict) -> set:
    """라벨 값 안의 UID 형태 문자열(cAdvisor id 경로는 '-' 대신 '_')을 '-' 형태로."""
    return {u.replace("_", "-") for v in (labels or {}).values() for u in _UID_RE.findall(str(v))}


def pod_identity(series_for_pod: list, expected_uid: str, runtime_uid_evidence: Optional[dict], pod: str) -> str:
    """uid_label(응답 라벨 UID 일치) / runtime_mapping(라벨에 UID 없음 + 실행 당시 매핑 일치) /
    identity_conflict(라벨 UID 불일치) / identity_unverified(근거 없음)."""
    found = set().union(*(label_uids(sr.get("metric")) for sr in series_for_pod)) if series_for_pod else set()
    if found:
        return "uid_label" if found == {expected_uid} else "identity_conflict"
    return "runtime_mapping" if (runtime_uid_evidence or {}).get(pod) == expected_uid else "identity_unverified"


def assemble(raw_series: list, expected_pods: Optional[dict], grid: list, runtime_uid_evidence: Optional[dict] = None) -> dict:
    """raw_series: Prometheus matrix 결과 그대로([{"metric": {...,"pod":..}, "values": [[ts, "v"], ...]}]).
    expected_pods: {pod 이름: uid} - 실행 당시 기대 목록(없으면 판단하지 않음).
    반환 status: complete / incomplete / identity_conflict / expected_unverified. complete일 때만 points=[(ts, 합)]."""
    if not expected_pods:
        return {"status": "expected_unverified", "reason": "expected_pods_unknown", "points": None}
    by_pod = {}
    for sr in raw_series:
        by_pod.setdefault((sr.get("metric") or {}).get("pod"), []).append(sr)
    dup = sorted(p for p, v in by_pod.items() if p in expected_pods and len(v) > 1)
    if dup:
        return {"status": "expected_unverified", "reason": f"duplicate_series={dup}", "points": None}
    identity = {p: pod_identity(by_pod.get(p, []), uid, runtime_uid_evidence, p) for p, uid in expected_pods.items()
                if by_pod.get(p)}
    if "identity_conflict" in identity.values():
        return {"status": "identity_conflict", "reason": f"identity={identity}", "identity": identity, "points": None}
    if "identity_unverified" in identity.values():
        return {"status": "expected_unverified", "reason": f"identity={identity}", "identity": identity, "points": None}
    vals, missing = {p: [None] * len(grid) for p in expected_pods}, {}
    for p in expected_pods:
        for ts, v in (by_pod.get(p) or [{"values": []}])[0]["values"]:
            for i, g in enumerate(grid):
                if abs(float(ts) - g) <= GRID_TOL_SEC:
                    vals[p][i] = float(v)
        miss = [i for i, x in enumerate(vals[p]) if x is None or not math.isfinite(x)]
        if miss:
            missing[p] = miss
    if missing:
        return {"status": "incomplete", "reason": "missing_grid", "missing": missing, "identity": identity, "points": None}
    return {"status": "complete", "reason": None, "missing": {}, "identity": identity,
            "points": [(g, sum(vals[p][i] for p in expected_pods)) for i, g in enumerate(grid)]}


def mean_slope_timed(points: list, step_sec: float = GRID_STEP_SEC) -> tuple:
    """(평균, 15초 칸당 기울기). x = (시각 − 첫 시각)/15초 - 완전한 격자에서는 np.arange와 같은 x라 기존 _mean_slope와
    값이 같고, 시각이 비어 있으면 실제 시간 간격을 반영한다(칸 수를 압축하지 않음)."""
    if not points:
        raise ValueError("points 없음 - 빈 입력을 0으로 바꾸지 않는다")
    ts = np.array([float(t) for t, _ in points])
    ys = np.array([float(v) for _, v in points])
    if len(points) < 2:
        return float(ys[0]), 0.0
    x = (ts - ts[0]) / step_sec
    return float(ys.mean()), float(np.polyfit(x, ys, 1)[0])


def _finite_float(x) -> Optional[float]:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def freshness_by_pod(timestamp_query_result: list, expected_pods: Optional[dict], now_epoch: Optional[float] = None,
                     max_age_sec: float = DEFAULT_MAX_SAMPLE_AGE_SEC, runtime_uid_evidence: Optional[dict] = None,
                     future_tolerance_sec: float = DEFAULT_FUTURE_TOLERANCE_SEC) -> dict:
    """timestamp_query_result: 인스턴트 질의 `timestamp(<지표 선택자>)`의 결과. 각 원소 value = [평가 시각, 원시 표본 시각].
    - 원시 표본 시각(value[1])만 신선도의 근거다. 평가 시각(value[0])은 표본 시각이 아니다.
    - 나이 = 평가 시각 − 원시 표본 시각(같은 응답 안의 Prometheus 서버 시계 기준 - 이 PC 시계 오차가 끼지 않는다).
      now_epoch가 주어지면 (now − 평가 시각)을 pc_clock_offset_sec로 기록만 한다.
    기대 Pod마다 state: fresh / stale / missing / duplicate_series / malformed / invalid_timestamp / future_timestamp /
    identity_conflict / identity_unverified."""
    if not expected_pods:
        return {"status": "expected_unverified", "per_pod": None}
    by_pod = {}
    for r in timestamp_query_result or []:
        pod = (r.get("metric") or {}).get("pod") if isinstance(r, dict) else None
        by_pod.setdefault(pod, []).append(r)
    per_pod = {}
    for p, uid in expected_pods.items():
        rs = by_pod.get(p) or []
        if not rs:
            per_pod[p] = {"state": "missing"}
            continue
        if len(rs) > 1:
            per_pod[p] = {"state": "duplicate_series"}
            continue
        ident = pod_identity(rs, uid, runtime_uid_evidence, p)
        if ident in ("identity_conflict", "identity_unverified"):
            per_pod[p] = {"state": ident}
            continue
        value = rs[0].get("value")
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            per_pod[p] = {"state": "malformed", "raw_value": value}
            continue
        eval_ts, sample_ts = _finite_float(value[0]), _finite_float(value[1])
        if eval_ts is None:
            per_pod[p] = {"state": "malformed", "raw_value": list(value)}
            continue
        if sample_ts is None:
            per_pod[p] = {"state": "invalid_timestamp", "raw_value": list(value)}
            continue
        age = eval_ts - sample_ts
        state = ("future_timestamp" if age < -future_tolerance_sec else "fresh" if age <= max_age_sec else "stale")
        per_pod[p] = {"state": state, "identity": ident, "sample_ts": sample_ts, "query_eval_ts": eval_ts,
                      "age_sec": round(age, 3),
                      "pc_clock_offset_sec": round(now_epoch - eval_ts, 3) if now_epoch is not None else None}
    ok = all(v["state"] == "fresh" for v in per_pod.values())
    return {"status": "fresh" if ok else "not_fresh", "per_pod": per_pod}


def timestamp_query(selector: str) -> str:
    return f"timestamp({selector})"


_KIND_PRIORITY = ("identity_conflict", "malformed", "invalid_timestamp", "future_timestamp", "missing",
                  "duplicate_series", "identity_unverified", "stale")


def check_freshness_by_pod(selector: str, expected_pods: Optional[dict], *, max_age_sec: float = DEFAULT_MAX_SAMPLE_AGE_SEC,
                           runtime_uid_evidence: Optional[dict] = None,
                           future_tolerance_sec: float = DEFAULT_FUTURE_TOLERANCE_SEC,
                           prom_url: str = "http://localhost:9090", timeout: float = 5.0,
                           get_fn=None, now_fn=time.time) -> dict:
    """prom_health.check_metric_freshness의 최소 대체안(후보, 운영 미적용). `timestamp(<선택자>)` 인스턴트 질의로 시계열마다
    원시 표본 시각을 받아 기대 Pod별로 판정한다. 인프라 실패(connection_error·api_error)·응답 형식 오류(malformed_response)와
    데이터 문제(no_samples·missing·stale·future_timestamp 등)를 reason_kind로 구분하고, 기대 목록이 없으면 판단하지 않는다."""
    if not expected_pods:
        return {"fresh": False, "reason_kind": "expected_unverified", "per_pod": None}
    try:
        import requests
        r = (get_fn or requests.get)(f"{prom_url}/api/v1/query", params={"query": timestamp_query(selector)}, timeout=timeout)
        r.raise_for_status()
        body = r.json()
    except Exception as e:  # noqa: BLE001 - 기존 함수와 같은 분류
        return {"fresh": False, "reason_kind": "connection_error", "reason": str(e), "per_pod": None}
    if not isinstance(body, dict) or body.get("status") != "success":
        return {"fresh": False, "reason_kind": "api_error", "per_pod": None}
    data = body.get("data")
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, list) or (data.get("resultType") not in (None, "vector")):
        return {"fresh": False, "reason_kind": "malformed_response", "per_pod": None}
    if not result:
        return {"fresh": False, "reason_kind": "no_samples", "per_pod": None}
    f = freshness_by_pod(result, expected_pods, now_fn(), max_age_sec, runtime_uid_evidence, future_tolerance_sec)
    states = {v["state"] for v in f["per_pod"].values()}
    kind = None if f["status"] == "fresh" else next(k for k in _KIND_PRIORITY if k in states)
    return {"fresh": f["status"] == "fresh", "reason_kind": kind, "per_pod": f["per_pod"]}


def cpu_rate_query(namespace: str = "vllm-serving", window_sec: int = CPU_RATE_WINDOW_SEC) -> str:
    return f'rate(container_cpu_usage_seconds_total{{namespace="{namespace}",container="vllm"}}[{window_sec}s])'


def window_features(raw_by_metric: dict, expected_pods: Optional[dict], start: datetime, end: datetime,
                    runtime_uid_evidence: Optional[dict] = None) -> dict:
    """필수 지표(동결 FEATURE_NAMES 순서: cpu, memory, queue, cache)의 원시 응답 → (mean, slope) 8개.
    입력 딕셔너리 순서와 무관하게 이 순서로 계산한다. 필수 지표가 없거나 하나라도 complete가 아니면 features=None."""
    grid = grid_points(start, end)
    out, feats = {"per_metric": {}, "required_metrics": list(REQUIRED_METRICS)}, []
    absent = [m for m in REQUIRED_METRICS if m not in (raw_by_metric or {})]
    if absent:
        out.update(features=None, status="metric_missing", reason=f"required_metrics_absent={absent}")
        return out
    for name in REQUIRED_METRICS:
        a = assemble(raw_by_metric[name], expected_pods, grid, runtime_uid_evidence)
        out["per_metric"][name] = {"status": a["status"], "reason": a["reason"], "missing": a.get("missing"),
                                   "identity": a.get("identity")}
        if a["status"] != "complete":
            out.update(features=None, status=a["status"], reason=f"{name}:{a['reason']}")
            return out
        feats.extend(mean_slope_timed(a["points"]))
    out.update(features=feats, status="complete", reason=None)
    return out
