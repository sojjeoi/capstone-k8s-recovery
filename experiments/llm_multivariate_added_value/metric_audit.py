#!/usr/bin/env python3
"""Axis 2 (LLM-serving multivariate added value) - 오프라인 지표 실현 가능성 감사 (2026-10-11, DESIGN 단계).

질문: 지금 vLLM 환경에서 CPU 말고 실제로 변하는 LLM-serving 지표가 있는가, CPU가 비슷한 구간에서도 다르게 움직이는가?
자료: 기존 공식 evidence가 정한 시간 구간만(새 workload 없음). Prometheus는 GET(query, query_range)만 - 클러스터 변경 없음.
  - normal  : c3 정상 세션(normal_valid) 관찰 구간, active Pod
  - pressure: A/B A-arm ab01a–ab05a, 주입 관측 시각 ~ +363 s(공식 압박 길이), active Pod = 구간 요청 성공 증가가 가장 큰 Pod
  - workload: v2 workload pilot v2wl90/91 baseline(L0)·ladder 단계, summary의 active Pod
vLLM 지표는 vllm-active·vllm-preview 두 job이 같은 Pod를 긁으므로 job="vllm-active"로 한 시계열만 쓴다.
request_success_total은 finished_reason 라벨별 시계열이 여러 개라 sum()으로 합친다(첫 시계열만 쓰면 0 - 2026-10-11 수정).
주의: 여기 쓰인 구간은 DEVELOPMENT evidence다. 이것으로 feature를 고르면 공식 검증은 새 독립 세션으로만 한다.
지연·SLO 계열은 detector 입력 후보에서 제외(outcome 전용) - 존재·변동만 기록한다."""
import argparse
import json
import math
import statistics
import subprocess
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "experiments" / "results"
PROM = "/api/v1/namespaces/monitoring/services/kube-prom-kube-prometheus-prometheus:9090/proxy/api/v1/"
NS = "vllm-serving"
STEP_SEC = 15
PRESSURE_SEC = 363.0
V = 'job="vllm-active",pod="{pod}"'

# name -> (PromQL({pod} 자리), detector 입력 허용 여부, 의미)
CANDIDATES = {
    "cpu_cores": (f'rate(container_cpu_usage_seconds_total{{namespace="{NS}",container="vllm",pod="{{pod}}"}}[1m])',
                  True, "vLLM 컨테이너 CPU(코어)"),
    "memory_ws_bytes": (f'container_memory_working_set_bytes{{namespace="{NS}",container="vllm",pod="{{pod}}"}}',
                        True, "working set 메모리"),
    "running": (f"vllm:num_requests_running{{{V}}}", True, "처리 중 요청 수"),
    "waiting": (f"vllm:num_requests_waiting{{{V}}}", True, "대기열 요청 수(상태값, 지연 아님)"),
    "kv_cache_usage": (f"vllm:kv_cache_usage_perc{{{V}}}", True, "KV cache 사용률 0-1"),
    "gen_tokens_per_s": (f"rate(vllm:generation_tokens_total{{{V}}}[1m])", True, "생성 토큰/초"),
    "prompt_tokens_per_s": (f"rate(vllm:prompt_tokens_total{{{V}}}[1m])", True, "prompt 토큰/초"),
    "req_success_per_s": (f"sum(rate(vllm:request_success_total{{{V}}}[1m]))", True, "완료 요청/초"),
    "engine_steps_per_s": (f"rate(vllm:iteration_tokens_total_count{{{V}}}[1m])", True, "엔진 iteration/초"),
    "preemptions_per_s": (f"rate(vllm:num_preemptions_total{{{V}}}[1m])", True, "선점/초"),
    "e2e_latency_mean_s": (f"rate(vllm:e2e_request_latency_seconds_sum{{{V}}}[1m]) / "
                           f"rate(vllm:e2e_request_latency_seconds_count{{{V}}}[1m])", False, "요청 지연 - leakage, outcome 전용"),
    "queue_time_mean_s": (f"rate(vllm:request_queue_time_seconds_sum{{{V}}}[1m]) / "
                          f"rate(vllm:request_queue_time_seconds_count{{{V}}}[1m])", False, "대기 시간 - 지연 계열 leakage"),
}
COUNTERS = {"gen_tokens": f"vllm:generation_tokens_total{{{V}}}", "req_success": f"sum(vllm:request_success_total{{{V}}})"}
CPU_EDGES = [0, 0.05, 0.25, 0.75, 1.25, 1.75, 2.0, 2.25, 2.5, 4.0, 100]


# ---------------- 순수 함수(테스트 대상) ----------------
def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def series_values(resp: dict) -> dict:
    """query_range 응답 → {unix_ts(int): float}(첫 시계열; 개수는 호출자가 series_count로 기록). NaN·Inf 제외."""
    res = (resp.get("data") or {}).get("result") or []
    out = {}
    for t, v in (res[0]["values"] if res else []):
        try:
            f = float(v)
        except ValueError:
            continue
        if math.isfinite(f):
            out[int(float(t))] = f
    return out


def counter_resets(values: list) -> int:
    return sum(1 for a, b in zip(values, values[1:]) if b < a)


def _rank(v):
    order = sorted(range(len(v)), key=lambda i: v[i])
    r, i = [0.0] * len(v), 0
    while i < len(v):
        j = i
        while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2.0
        i = j + 1
    return r


def spearman(x: list, y: list):
    """상수 열이거나 3점 미만이면 None(상관 정의 안 됨)."""
    if len(x) < 3 or len(set(x)) < 2 or len(set(y)) < 2:
        return None
    rx, ry = _rank(x), _rank(y)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else None


def describe(vals: list, expected_points: int) -> dict:
    if not vals:
        return {"n": 0, "missing_rate": 1.0}
    return {"n": len(vals), "missing_rate": round(max(0.0, 1 - len(vals) / expected_points), 4),
            "nonzero_frac": round(sum(v != 0 for v in vals) / len(vals), 4), "unique": len({round(v, 9) for v in vals}),
            "min": min(vals), "median": statistics.median(vals), "max": max(vals), "std": statistics.pstdev(vals)}


def cpu_bins(points: list, edges=CPU_EDGES) -> dict:
    """points = [(kind, cpu, metric)] → {bin: {kind: {n, median, nonzero_frac}}}. 같은 CPU 구간에서 kind별 지표 비교(서술용)."""
    out = {}
    for lo, hi in zip(edges, edges[1:]):
        by = {}
        for kind, c, m in points:
            if lo <= c < hi:
                by.setdefault(kind, []).append(m)
        if by:
            out[f"[{lo:g},{hi:g})"] = {k: {"n": len(v), "median": statistics.median(v),
                                           "nonzero_frac": round(sum(x != 0 for x in v) / len(v), 4)} for k, v in sorted(by.items())}
    return out


# ---------------- 구간(공식 evidence에서만) ----------------
def normal_windows(coll=RESULTS / "c3_normal_coverage_collection") -> list:
    out = []
    for p in sorted(coll.glob("c3*-validity.json")):
        v = json.loads(p.read_text(encoding="utf-8"))
        if v.get("status") == "normal_valid":
            b = v["base"]
            out.append({"kind": "normal", "id": v["run"], "start": b["t0_utc"], "end": b["observation_end_utc"],
                        "pod": b["nodes"]["active_pod"]})
    return out


def pressure_windows(ab=RESULTS / "capstone_guarded_ab_v1") -> list:
    out = []
    for run in ("ab01a", "ab02a", "ab03a", "ab04a", "ab05a"):
        s = json.loads(next(ab.glob(f"summary-guarded_pressure_record_only-{run}-*.json")).read_text(encoding="utf-8"))
        t = ts(s["trial_outcome"]["t_injection_observed_utc"])
        out.append({"kind": "pressure", "id": run, "start": t.isoformat(),
                    "end": (t + timedelta(seconds=PRESSURE_SEC)).isoformat(), "pod": None})
    return out


def workload_windows(v2=RESULTS / "capstone_v2_workload_pilot") -> list:
    out = []
    for p in sorted(v2.glob("summary-v2_workload_pilot-*.json")):
        s = json.loads(p.read_text(encoding="utf-8"))
        pod, bw = s["active_pod"]["name"], s["baseline_window"]
        out.append({"kind": "workload", "id": f"{s['run']}-L0", "start": bw["start_utc"], "end": bw["end_utc"], "pod": pod,
                    "slo_violating": False})
        verdict = {x["stage"]: x.get("violating") for x in s.get("stage_slo_verdicts") or []}
        out += [{"kind": "workload", "id": f"{s['run']}-{st['stage']}", "start": st["stage_start_utc"], "end": st["stage_end_utc"],
                 "pod": pod, "slo_violating": verdict.get(st["stage"])} for st in s["ramp_stages"]]
    return out


# ---------------- Prometheus(GET만) ----------------
def prom_get(path: str) -> dict:
    out = subprocess.run(["kubectl", "get", "--raw", PROM + path], capture_output=True, text=True, timeout=120)
    return json.loads(out.stdout) if out.returncode == 0 else {"status": "error", "error": out.stderr[-300:]}


def query_range(expr: str, start: datetime, end: datetime) -> dict:
    return prom_get("query_range?" + urllib.parse.urlencode(
        {"query": expr, "start": start.timestamp(), "end": end.timestamp(), "step": STEP_SEC}))


def busiest_pod(start: datetime, end: datetime):
    expr = f'sum by (pod) (increase(vllm:request_success_total{{job="vllm-active"}}[{int((end - start).total_seconds())}s]))'
    r = prom_get("query?" + urllib.parse.urlencode({"query": expr, "time": end.timestamp()}))
    res = sorted(((x["metric"].get("pod"), float(x["value"][1])) for x in (r.get("data") or {}).get("result") or []),
                 key=lambda kv: kv[1], reverse=True)
    return res[0][0] if res else None


def audit_window(w: dict) -> dict:
    start, end = ts(w["start"]), ts(w["end"])
    pod = w["pod"] or busiest_pod(start, end)
    expected = int((end - start).total_seconds() // STEP_SEC) + 1
    per, count = {}, {}
    for name, (expr, _, _) in CANDIDATES.items():
        resp = query_range(expr.replace("{pod}", pod), start, end)
        count[name] = len((resp.get("data") or {}).get("result") or [])
        per[name] = series_values(resp)
    cpu = per["cpu_cores"]
    stats = {}
    for name, vals in per.items():
        common = sorted(set(vals) & set(cpu))
        stats[name] = {**describe([v for _, v in sorted(vals.items())], expected), "series_count": count[name],
                       "spearman_vs_cpu": None if name == "cpu_cores" else spearman([cpu[t] for t in common], [vals[t] for t in common])}
    resets = {k: counter_resets([v for _, v in sorted(series_values(query_range(e.replace("{pod}", pod), start, end)).items())])
              for k, e in COUNTERS.items()}
    aligned = {n: [[t, cpu[t], v[t]] for t in sorted(set(v) & set(cpu))] for n, v in per.items() if n != "cpu_cores"}
    return {**w, "pod": pod, "expected_points": expected, "stats": stats, "counter_resets": resets, "aligned": aligned}


def summarize(rows: list) -> dict:
    by_kind = {}
    for k in sorted({r["kind"] for r in rows}):
        rs = [r for r in rows if r["kind"] == k]
        by_kind[k] = {n: {"windows": len(rs),
                          "nonzero_frac_mean": round(statistics.fmean(r["stats"][n].get("nonzero_frac", 0) for r in rs), 4),
                          "unique_max": max(r["stats"][n].get("unique", 0) for r in rs),
                          "median_of_medians": statistics.median(r["stats"][n].get("median") or 0 for r in rs),
                          "max": max(r["stats"][n].get("max") or 0 for r in rs),
                          "missing_rate_max": max(r["stats"][n].get("missing_rate") or 0 for r in rs),
                          "series_count_max": max(r["stats"][n]["series_count"] for r in rs),
                          "spearman_vs_cpu_median": (statistics.median(x) if (x := [r["stats"][n]["spearman_vs_cpu"] for r in rs
                                                                                   if r["stats"][n].get("spearman_vs_cpu") is not None])
                                                     else None)}
                      for n in CANDIDATES}
    bins = {n: cpu_bins([(r["kind"] + ("_slo_violating" if r.get("slo_violating") else ""), c, m)
                         for r in rows for _, c, m in r["aligned"][n]]) for n in CANDIDATES if n != "cpu_cores"}
    return {"by_kind": by_kind, "cpu_binned": bins,
            "counter_resets_total": {k: sum(r["counter_resets"][k] for r in rows) for k in COUNTERS}}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Axis 2 지표 감사 - Prometheus GET만, 기존 공식 구간만")
    p.add_argument("--out", required=True, help="experiments/results/llm_multivariate_added_value_* 새 폴더")
    a = p.parse_args(argv)
    out = Path(a.out).resolve()
    if out.parent != RESULTS.resolve() or not out.name.startswith("llm_multivariate_added_value"):
        raise SystemExit("출력은 experiments/results/llm_multivariate_added_value_* 새 폴더만")
    out.mkdir(exist_ok=False)
    rows = [audit_window(w) for w in normal_windows() + pressure_windows() + workload_windows()]
    res = {"meta": {"created_at_utc": datetime.now(timezone.utc).isoformat(), "step_sec": STEP_SEC,
                    "candidates": {k: {"promql": v[0], "detector_input_allowed": v[1], "meaning": v[2]} for k, v in CANDIDATES.items()},
                    "note": "DEVELOPMENT evidence only - feature 선택에 쓰면 공식 검증은 새 독립 세션으로"},
           "summary": summarize(rows), "rows": rows}
    (out / "metric-audit.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps({"windows": len(rows), "out": str(out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
