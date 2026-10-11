#!/usr/bin/env python3
"""AIOps v2 workload feasibility pilot 분석(새 파일, 2026-10-09) - 계약: docs/design/phase8-aiops-v2-workload-feasibility-contract-20261009.md

pilot(counted=false)의 목적은 하나: 정상(SLO·안전 정상)이면서 vLLM service-state metric이 실제로 움직이는 부하 수준이 있는지.
IF score·CPU 임계값 발화는 계산하지도, 선택 기준으로 쓰지도 않는다. 판정은 기계 규칙(사람이 그래프를 보고 고르지 않음).
공식 SLO는 동결 slo_episodes(r2)·slo_judge v3 그대로. 통계 없음(기술값만).
r2(freeze 후보 v2): scrape 원본 표본(시각 그대로) 저장, job="vllm-active" 한 줄만(같은 pod를 active·preview job이 이중 수집),
단계 SLO 판정 함수 하나를 실행기(단계 직후 경계 종료)와 분석기가 같이 씀, 부하 적합성(계획 재계산·발사 수·지연) 확인."""
import argparse
import csv
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import c1_shadow_pilot_analysis as cspa
import slo_episodes
import slo_judge

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "chaos" / "loadgen"))
import v2_arrival  # noqa: E402 - 발사 계획 재계산(순수 함수)

ACTIVATION_MIN_FRACTION = 0.20   # running≥1 표본 비율 하한 - c1 60 s 창(15 s 격자 5점)에 평균 1점 이상 비zero. 결과를 보고 바꾸지 않음
SAMPLE_COVERAGE_MIN = 0.80       # 단계별 기대 15 s 표본의 80% 이상 있어야 판정 가능
SCRAPE_SEC = 15
MAX_SEND_LAG_SEC = 1.0           # 계획 대비 실제 발사 지연 상한(ladder arrival.max_send_lag_sec와 같아야 함 - 테스트가 대조)
# 원본 scrape 표본 selector(집계 없음). 같은 pod를 vllm-active·vllm-preview job이 이중 수집하므로 job="vllm-active"만.
# 한 selector가 여러 줄(예: finished_reason별)이면 같은 scrape 시각끼리 합한다(prom_raw).
_V = '{{pod="{pod}",job="vllm-active"}}'
METRICS = {
    "running": "vllm:num_requests_running" + _V,
    "waiting": "vllm:num_requests_waiting" + _V,
    "kv_cache_perc": "vllm:kv_cache_usage_perc" + _V,
    "queue_time_sum": "vllm:request_queue_time_seconds_sum" + _V,
    "queue_time_count": "vllm:request_queue_time_seconds_count" + _V,
    # 끝난 요청 수(엔진이 요청을 끝낼 때 증가, finished_reason=stop|length|abort|error|repetition 합) - 도착률이 아니라 처리량
    "finished_requests_total": "vllm:request_success_total" + _V,
    "finished_abort_error_total": 'vllm:request_success_total{{pod="{pod}",job="vllm-active",finished_reason=~"abort|error"}}',
    "generation_tokens_total": "vllm:generation_tokens_total" + _V,
    "iteration_count": "vllm:iteration_tokens_total_count" + _V,
    "cpu_seconds_total": 'container_cpu_usage_seconds_total{{namespace="vllm-serving",pod="{pod}",container="vllm",job="kubelet"}}',
}
# feature 의미 분류(설계 r3). 실제 도착률(offered load)은 Prometheus에 없다 - harness 목표·실제 발사 RPS는 라벨/진단으로만.
FEATURE_ROLE = {"finished_requests_total": "service_throughput", "running": "service_state", "waiting": "service_state",
                "queue_time_sum": "service_state", "queue_time_count": "service_state", "cpu_seconds_total": "resource",
                "harness_target_rps": "workload_label_only", "harness_achieved_rps": "diagnostic_only"}


def _ts(x):
    t = x if isinstance(x, datetime) else datetime.fromisoformat(str(x).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def prom_raw(prom_url, selector, start, end, get=requests.get):
    """[start, end]의 원본 scrape 표본(평가 격자가 아니라 실제 scrape 시각). 여러 줄이면 같은 시각끼리 합."""
    dur = int((end - start).total_seconds()) + 1
    r = get(f"{prom_url}/api/v1/query", params={"query": f"{selector}[{dur}s]", "time": end.timestamp()}, timeout=30)
    r.raise_for_status()
    acc = {}
    for ser in r.json()["data"]["result"]:
        for t, v in ser["values"]:
            acc[float(t)] = acc.get(float(t), 0.0) + float(v)
    return [[t, acc[t]] for t in sorted(acc)]


def capture_metrics(pod, start, end, range_fn):
    """실행 끝에 한 번 원본 표본을 저장한다(분석기는 이 파일만 읽음 - 재현 가능)."""
    return {"pod": pod, "start_utc": start.isoformat(), "end_utc": end.isoformat(), "sample_kind": "raw_scrape",
            "selectors": {k: e.format(pod=pod) for k, e in METRICS.items()},
            "series": {k: range_fn(e.format(pod=pod), start, end) for k, e in METRICS.items()}}


def _in(series, a, b):
    return [(t, v) for t, v in series if a.timestamp() <= t < b.timestamp()]   # 반열림 - 경계 표본이 두 단계에 중복되지 않게


def _rate(series, a, b):
    pts = _in(series, a, b)
    return (pts[-1][1] - pts[0][1]) / (pts[-1][0] - pts[0][0]) if len(pts) >= 2 and pts[-1][0] > pts[0][0] else None


def _sample_note(n, expected):
    if n == expected:
        return "as_expected"
    if abs(n - expected) == 1:
        return "scrape_grid_alignment(+-1: 단계 경계와 15 s scrape 위상)"
    return "scrape_missing_or_extra(scrape 누락·중복 - coverage로 판정)" if n else "no_samples"


def stage_metrics(metrics, a, b):
    s = metrics["series"]
    run = [v for _, v in _in(s["running"], a, b)]
    wait = [v for _, v in _in(s["waiting"], a, b)]
    expected = max(1, int((b - a).total_seconds() // SCRAPE_SEC))
    qs, qc = _in(s["queue_time_sum"], a, b), _in(s["queue_time_count"], a, b)
    dq = (qc[-1][1] - qc[0][1]) if len(qc) >= 2 else 0
    n_run, n_wait = sum(v >= 1 for v in run), sum(v >= 1 for v in wait)
    return {"samples": len(run), "expected_samples": expected, "coverage": round(len(run) / expected, 3),
            "sample_count_note": _sample_note(len(run), expected),
            "running_nonzero_samples": n_run,
            "running_nonzero_fraction": round(n_run / len(run), 3) if run else None,
            "running_max": max(run) if run else None, "running_mean": round(statistics.mean(run), 3) if run else None,
            "waiting_samples": len(wait), "waiting_nonzero_samples": n_wait,
            "waiting_nonzero_fraction": round(n_wait / len(wait), 3) if wait else None,
            "waiting_max": max(wait) if wait else None,
            "queue_time_requests": dq, "queue_time_mean_sec": round((qs[-1][1] - qs[0][1]) / dq, 6) if dq > 0 else None,
            "kv_cache_perc_max": max((v for _, v in _in(s["kv_cache_perc"], a, b)), default=None),
            "finished_request_rate": _rate(s["finished_requests_total"], a, b),
            "finished_abort_error_rate": _rate(s["finished_abort_error_total"], a, b),
            "generation_token_rate": _rate(s["generation_tokens_total"], a, b),
            "iteration_rate": _rate(s["iteration_count"], a, b),
            "cpu_cores_mean": _rate(s["cpu_seconds_total"], a, b)}


def stage_slo_verdict(rows, a0, start, end):
    """계약 §2.3 - 단계 [start, end]의 공식 SLO 판정. [a0, end] 관찰을 동결 slo_episodes r2(응답이 end 이전에 확인된
    요청만 증거)로 계산해 위반 구간이 단계와 겹치면 violating. 실행기(단계 직후)와 분석기가 이 함수 하나를 같은 입력으로
    부르므로 판정이 같다. None = probe 증거가 아직 end를 덮지 않음(실행기는 기다림)."""
    if not any(r["sent_at"] + timedelta(seconds=r["latency"]) > end for r in rows):
        return None
    ep = slo_episodes.violation_episodes(rows, a0, end, min_requests=cspa.min_requests_for((end - a0).total_seconds()))
    if not ep["computable"]:
        return {"computable": False, "reason": ep["reason"], "violating": None, "episodes": []}
    eps = [(_ts(e["t_slo"]), _ts(e["end_counted"])) for e in ep["episodes"]]
    return {"computable": True, "reason": None, "violating": any(s <= end and e >= start for s, e in eps),
            "episodes": [{"t_slo": s.isoformat(), "end": e.isoformat()} for s, e in eps]}


def load_conformance(row, raw_rows, run_seed, stage_index):
    """계약 §2.1 허용 오차: seed·계획 재계산 일치, 발사 수 == 계획 수, |발사 수/길이 - 목표| <= 1/길이, 요청별 발사 지연 <= 1 s."""
    rps, dur = float(row["target_rps"]), float(row["duration_sec"])
    seed = v2_arrival.stage_seed(run_seed, stage_index)
    plan = v2_arrival.schedule(rps, dur, seed)
    sent = int(row["sent"])
    lags = [float(r["send_lag_sec"]) for r in raw_rows]
    problems = []
    if int(row["seed"]) != seed:
        problems.append("seed_mismatch")
    if [float(r["scheduled_offset_sec"]) for r in raw_rows] != plan:
        problems.append("schedule_mismatch")
    if sent != len(plan) or len(raw_rows) != sent:
        problems.append(f"sent={sent} planned={len(plan)} raw={len(raw_rows)}")
    if abs(sent / dur - rps) > 1 / dur:
        problems.append("achieved_rps_out_of_tolerance")
    if not lags or max(lags) > MAX_SEND_LAG_SEC:
        problems.append(f"send_lag>{MAX_SEND_LAG_SEC}")
    return {"load_conformant": not problems, "load_problems": problems, "planned": len(plan), "sent": sent,
            "achieved_rps": round(sent / dur, 6), "max_send_lag_sec": max(lags) if lags else None}


def stage_windows(summary):
    """L0 = ramp 전 기준선 구간, L1.. = 단계별 v2_arrival summary의 실제 단계 시각."""
    w = [{"level": "L0", "rps": 0.0, "start": _ts(summary["baseline_window"]["start_utc"]),
          "end": _ts(summary["baseline_window"]["end_utc"]), "ramp_sent": 0, "ramp_success": 0, "row": None}]
    for r in summary.get("ramp_stages") or []:
        w.append({"level": r["stage"].split("-")[0], "rps": float(r["target_rps"]), "start": _ts(r["stage_start_utc"]),
                  "end": _ts(r["stage_end_utc"]), "ramp_sent": int(r["sent"]), "ramp_success": int(r["success"]), "row": r})
    return w


def _raw_rows(path):
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def analyze_rep(summary, metrics, probe_csv):
    """한 반복(rep)의 단계별 판정. valid=False면 그 rep의 단계는 선택에 쓰지 않는다(fail-closed).
    SLO 경계 종료(ramp_terminated_after_slo_boundary)는 invalid 사유가 아니다 - 실행된 단계까지만 판정한다."""
    reasons = []
    if summary.get("outcome_status") != "completed":
        reasons.append(f"outcome={summary.get('outcome_status')}: {summary.get('outcome_reason')}")
    if summary.get("cleanup_errors"):
        reasons.append("cleanup_errors")
    if not summary.get("ramp_stages"):
        reasons.append("ramp_summary_missing")
    if not probe_csv or not Path(probe_csv).exists():
        reasons.append("probe_csv_missing")
    if reasons:
        return {"run": summary.get("run"), "valid": False, "invalid_reasons": reasons, "stages": []}
    rows = slo_judge.load_raw(Path(probe_csv))
    wins = stage_windows(summary)
    a0 = wins[0]["start"]
    out, bad = [], []
    for i, w in enumerate(wins):
        v = stage_slo_verdict(rows, a0, w["start"], w["end"])
        if not v or not v["computable"]:
            bad.append(f"probe_not_computable:{w['level']}:{v and v['reason']}")
            continue
        m = stage_metrics(metrics, w["start"], w["end"])
        lc = (load_conformance(w["row"], _raw_rows(w["row"]["raw_csv"]), int(summary["run_seed"]), i)
              if w["row"] is not None else {"load_conformant": True, "load_problems": [], "achieved_rps": 0.0})
        probe_fail = sum(1 for r in rows if w["start"] <= r["sent_at"] <= w["end"] and not r["success"])
        ramp_fail = w["ramp_sent"] - w["ramp_success"]
        out.append({"level": w["level"], "rps": w["rps"], "start_utc": w["start"].isoformat(), "end_utc": w["end"].isoformat(),
                    "slo_normal": not v["violating"], "slo_episodes": v["episodes"],
                    "probe_failures": probe_fail, "ramp_failures": ramp_fail,
                    "evaluable": m["coverage"] >= SAMPLE_COVERAGE_MIN,
                    "normal_and_safe": (not v["violating"]) and probe_fail == 0 and ramp_fail == 0,
                    "activated": (m["running_nonzero_fraction"] or 0) >= ACTIVATION_MIN_FRACTION, **lc, **m})
    if bad:
        return {"run": summary.get("run"), "valid": False, "invalid_reasons": bad, "stages": []}
    return {"run": summary.get("run"), "valid": True, "invalid_reasons": [], "stages": out,
            "ramp_terminated_after_slo_boundary": bool(summary.get("ramp_terminated_after_slo_boundary")),
            "slo_boundary_stage": summary.get("slo_boundary_stage")}


def select_levels(reps):
    """사전 고정 선택 규칙(계약 §6). reps = analyze_rep 결과(유효 rep만 사용).
    high = 모든 유효 rep에서 normal_and_safe·evaluable·load_conformant·activated인 가장 높은 단계(L1 이상).
    medium = 모든 유효 rep에서 normal_and_safe·evaluable·load_conformant인 단계 중 rps가 high의 절반에 가장 가까운 것
    (동률이면 낮은 쪽), high·low와 달라야 함. low = L1이 같은 조건이면 L1. 보간 없음."""
    valid = [r for r in reps if r["valid"]]
    if not valid:
        return {"feasible": False, "reason": "no_valid_rep", "low": None, "medium": None, "high": None}
    # 단계 합집합 - SLO 경계 종료로 어떤 rep에서 실행되지 않은 단계는 "모든 유효 rep" 조건을 못 채워 선택 불가
    levels = sorted({(s["level"], s["rps"]) for r in valid for s in r["stages"] if s["level"] != "L0"}, key=lambda x: x[1])

    def all_reps(level, *keys):
        hit = [s for r in valid for s in r["stages"] if s["level"] == level]
        return len(hit) == len(valid) and all(all(s[k] for k in keys) for s in hit)
    ok = [(lv, rps) for lv, rps in levels if all_reps(lv, "normal_and_safe", "evaluable", "load_conformant")]
    act = [(lv, rps) for lv, rps in ok if all_reps(lv, "activated")]
    if not act:
        return {"feasible": False, "reason": "no_level_normal_safe_and_activated", "low": None, "medium": None, "high": None,
                "normal_safe_levels": [lv for lv, _ in ok]}
    high = max(act, key=lambda x: x[1])
    low = levels[0] if levels and levels[0] in ok else None
    mids = [x for x in ok if x not in (high, low)]
    medium = min(mids, key=lambda x: (abs(x[1] - high[1] / 2), x[1])) if mids else None
    return {"feasible": bool(low and medium), "reason": None if (low and medium) else "low_or_medium_missing",
            "low": low and low[0], "medium": medium and medium[0], "high": high[0],
            "normal_safe_levels": [lv for lv, _ in ok], "activated_levels": [lv for lv, _ in act]}


def analyze(results_dir: Path):
    reps = []
    for sp in sorted(results_dir.glob("summary-*.json")):
        s = json.loads(sp.read_text(encoding="utf-8"))
        mp = results_dir / f"metrics-{s['run_id']}.json"
        metrics = json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else None
        reps.append(analyze_rep(s, metrics, s.get("probe_raw_csv")) if metrics else
                    {"run": s.get("run"), "valid": False, "invalid_reasons": ["metrics_missing"], "stages": []})
    return {"analyzer": "v2_workload_pilot_analysis", "counted": False,
            "note": "feasibility pilot - 학습·보정·검증·우월성 근거 아님. IF·CPU 임계값 발화는 계산하지 않음",
            "rules": {"activation_min_fraction": ACTIVATION_MIN_FRACTION, "sample_coverage_min": SAMPLE_COVERAGE_MIN,
                      "max_send_lag_sec": MAX_SEND_LAG_SEC, "arrival_rule": v2_arrival.RULE, "feature_role": FEATURE_ROLE},
            "reps": reps, "selection": select_levels(reps)}


def main(argv=None) -> int:  # pragma: no cover
    p = argparse.ArgumentParser()
    p.add_argument("--results-dir", default=str(Path(__file__).parent / "results" / "capstone_v2_workload_pilot"))
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    res = analyze(Path(a.results_dir))
    with open(a.out, "x", encoding="utf-8") as fh:
        fh.write(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    print(json.dumps(res["selection"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
