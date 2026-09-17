#!/usr/bin/env python3
"""docs/design/slo-definition.md의 규칙을 raw 요청 로그(ramp.py 출력)에 사후
적용해서 t_SLO/t_recovery를 찾는다. calibration은 개입 없이 관찰만 하므로
실시간 스트리밍 없이 사후 분석으로 충분하다(실시간 반응은 Phase 7 정책
서비스 몫 - guideline.md 9-2절).

P95는 ramp.py 자체 요약(summary CSV)과 달리 timeout(status=None)도 포함해서
계산한다 - 완료된 요청만 보면 타임아웃이 쏟아지는 와중에도 P95가 좋아 보이는
사각지대가 생긴다(이 프로젝트에서 이미 한 번 겪은 문제).
"""
import argparse
import csv
import statistics
import sys
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8")  # Windows 기본 cp949 콘솔 대응

# SLO v2(2026-09-16, docs/design/slo-definition.md §2 변경이력) - max_tokens=1
# probe 전용 프로필로 재보정한 값. v1(2.686s, max_tokens=10)은 probe 자신이
# vLLM CPU 4코어를 거의 다 써서 observer effect를 일으키는 게 실측 확인돼
# 폐기했다(본 실험 미사용, 이력만 slo-definition.md에 보존). experiments/
# probe.py가 항상 이 프로필로 도므로, 이 상수는 probe raw 로그 판정에만
# 쓴다 - ramp.py 자체 원시 로그(max_tokens=10)에 이 상수를 적용하면 안 된다.
L_BASELINE = 0.256  # 3x300건 calibration(calibrate_probe_only.py)의 P95 중앙값
LATENCY_THRESHOLD = 2 * L_BASELINE  # §3
LATENCY_PERSIST_SEC = 30  # §3, §6
AVAILABILITY_THRESHOLD = 0.99  # §4
WINDOW_SEC = 60  # §3, §4
# evaluate()가 실제 백분위수 대신 max()로 근사하는 표본 수 경계 - 이 밑에서는
# P95가 "지금까지 제일 느린 요청 1개"와 같아서 노이즈가 크다. run_once.py의
# NOT_EVALUABLE 판정(Prober.is_slo_evaluable)도 이 상수를 그대로 참조한다 -
# "위반 없음"을 신뢰하려면 최소 이만큼의 실측 표본이 있어야 한다는 기준을
# 중복 정의하지 않기 위함(2026-09-17).
MIN_SAMPLES_FOR_RELIABLE_P95 = 20


def load_raw(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({
                "sent_at": datetime.fromisoformat(r["sent_at"]),
                "latency": float(r["latency"]),
                "success": r["success"] in ("True", "true", "1"),
            })
    rows.sort(key=lambda r: r["sent_at"])
    return rows


def _window(rows, t, seconds=WINDOW_SEC):
    lo = t - timedelta(seconds=seconds)
    return [r for r in rows if lo < r["sent_at"] <= t]


def evaluate(rows):
    """각 요청 시각을 평가 시점으로 삼아 그 시점의 직전 60초 P95/성공률과
    SLO 위반 여부를 계산한다."""
    points = []
    for r in rows:
        w = _window(rows, r["sent_at"])
        latencies = [x["latency"] for x in w]
        p95 = (statistics.quantiles(latencies, n=100)[94]
               if len(latencies) >= MIN_SAMPLES_FOR_RELIABLE_P95 else max(latencies))
        success_rate = sum(x["success"] for x in w) / len(w)
        points.append({
            "t": r["sent_at"],
            "p95": p95,
            "success_rate": success_rate,
            "latency_violating": p95 > LATENCY_THRESHOLD,
            "availability_violating": success_rate < AVAILABILITY_THRESHOLD,
        })
    return points


def find_t_slo(points):
    """latency 위반은 30초 연속 지속돼야 인정(§3), availability 위반은 즉시(§4).
    둘 중 먼저 만족되는 시각을 반환한다."""
    latency_t_slo = None
    streak_start = None
    for p in points:
        if p["latency_violating"]:
            streak_start = streak_start or p["t"]
            if (p["t"] - streak_start).total_seconds() >= LATENCY_PERSIST_SEC:
                latency_t_slo = streak_start + timedelta(seconds=LATENCY_PERSIST_SEC)
                break
        else:
            streak_start = None

    availability_t_slo = next((p["t"] for p in points if p["availability_violating"]), None)

    candidates = [t for t in (latency_t_slo, availability_t_slo) if t is not None]
    return min(candidates) if candidates else None


def find_t_recovery(points, t_slo):
    """t_SLO 이후 두 조건 모두 해소된 상태가 30초 유지된 구간의 시작 시각(§6)."""
    if t_slo is None:
        return None
    after = [p for p in points if p["t"] >= t_slo]
    streak_start = None
    for p in after:
        if not p["latency_violating"] and not p["availability_violating"]:
            streak_start = streak_start or p["t"]
            if (p["t"] - streak_start).total_seconds() >= LATENCY_PERSIST_SEC:
                return streak_start
        else:
            streak_start = None
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="raw CSV에 SLO 규칙을 적용해 t_SLO/t_recovery 산출")
    parser.add_argument("raw_csv")
    args = parser.parse_args()

    rows = load_raw(args.raw_csv)
    points = evaluate(rows)
    t_slo = find_t_slo(points)
    t_recovery = find_t_recovery(points, t_slo)

    print("t_SLO:", t_slo.isoformat() if t_slo else "위반 없음")
    if t_slo is not None:
        print("t_recovery:", t_recovery.isoformat() if t_recovery else "미회복(관측 종료 시점까지)")
