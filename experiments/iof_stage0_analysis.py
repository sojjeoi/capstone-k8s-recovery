#!/usr/bin/env python3
"""Stage 0 I/O feasibility 분석(새 파일, 2026-10-09, counted=false) - 계약: docs/design/phase8-iof-stage0-contract-20261009.md

판정은 기계 규칙(계약 §6–§8). 공식 SLO는 동결 slo_episodes r2 / slo_judge v3(v2wl stage_slo_verdict 재사용).
CPU 정상 범위 확인은 탐지기 임계값이 아니라 feasibility guard: baseline 세션(iob)의 60 s 창 CPU [min, max] envelope.
I/O 변화의 기준 지표는 노드 io PSI waiting rate(nb02·bc03b forensic에서 정지와 함께 오른 지표). 나머지 I/O 지표는 기술값.
IF·R·C2 계산 없음, 임계값 산정 없음."""
import argparse
import json
import statistics as st
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import slo_judge
import v2_workload_pilot_analysis as wa

CPU_WIN, CPU_STEP = 60.0, 15.0          # 컨테이너 CPU(cAdvisor 10 s scrape)
NODE_WIN, NODE_STEP = 120.0, 30.0       # 노드 지표(node-exporter 30 s scrape)
NOMINAL_SCRAPE = {"cpu": 10.0, "gen_tokens": 15.0, "node": 30.0}
GAP_FACTOR = 2.5                        # 최대 표본 간격 ≤ 2.5 × 명목 scrape 간격
WRITER_MIN_RAN_FRACTION = 0.95
NODE_KEYS = ("written_bytes", "read_bytes", "io_time", "io_time_weighted", "write_time", "writes_completed",
             "psi_waiting", "psi_stalled", "iowait")


def selectors(pod: str, node_instance: str) -> dict:
    n = f'instance="{node_instance}"'
    d = f'{n},device="vda"'
    return {"cpu": f'container_cpu_usage_seconds_total{{namespace="vllm-serving",pod="{pod}",container="vllm",job="kubelet"}}',
            "gen_tokens": f'vllm:generation_tokens_total{{pod="{pod}",job="vllm-active"}}',
            "written_bytes": f"node_disk_written_bytes_total{{{d}}}", "read_bytes": f"node_disk_read_bytes_total{{{d}}}",
            "io_time": f"node_disk_io_time_seconds_total{{{d}}}",
            "io_time_weighted": f"node_disk_io_time_weighted_seconds_total{{{d}}}",
            "write_time": f"node_disk_write_time_seconds_total{{{d}}}",
            "writes_completed": f"node_disk_writes_completed_total{{{d}}}",
            "psi_waiting": f"node_pressure_io_waiting_seconds_total{{{n}}}",
            "psi_stalled": f"node_pressure_io_stalled_seconds_total{{{n}}}",
            "iowait": f'node_cpu_seconds_total{{{n},mode="iowait"}}'}


def capture(pod, node_instance, start, end, prom_url, get=requests.get):
    """원본 scrape 표본(여러 줄 = cpu별 iowait 등은 같은 시각끼리 합) - v2wl prom_raw와 같은 규칙."""
    sel = selectors(pod, node_instance)
    return {"pod": pod, "node_instance": node_instance, "start_utc": start.isoformat(), "end_utc": end.isoformat(),
            "selectors": sel, "series": {k: wa.prom_raw(prom_url, s, start, end, get=get) for k, s in sel.items()}}


def _ts(x):
    return wa._ts(x)


def rate_windows(series, start, end, win, step):
    """[t-win, t] 창의 counter 증가율, t = start+win, +step, … ≤ end. 표본 2개 미만 창은 None."""
    out, t = [], start.timestamp() + win
    while t <= end.timestamp() + 1e-6:
        pts = [(a, v) for a, v in series if t - win <= a <= t]
        out.append((t, (pts[-1][1] - pts[0][1]) / (pts[-1][0] - pts[0][0]) if len(pts) >= 2 and pts[-1][0] > pts[0][0] else None))
        t += step
    return out


def max_gap(series, a, b):
    ts = [t for t, _ in series if a.timestamp() <= t <= b.timestamp()]
    edges = [a.timestamp()] + ts + [b.timestamp()]
    return max(y - x for x, y in zip(edges, edges[1:]))


def integrity(metrics, a, b):
    """구간 [a, b]의 측정 무결성: 지표별 최대 표본 간격 ≤ 2.5 × 명목 scrape."""
    gaps, bad = {}, []
    for k, s in metrics["series"].items():
        nominal = NOMINAL_SCRAPE["node"] if k in NODE_KEYS else NOMINAL_SCRAPE[k]
        g = max_gap(s, a, b)
        gaps[k] = round(g, 1)
        if g > GAP_FACTOR * nominal:
            bad.append(f"{k}_gap={g:.1f}s")
    return {"ok": not bad, "max_gap_sec": gaps, "problems": bad}


def cpu_envelope(baseline_metrics, obs_start, obs_end):
    vals = [v for t, v in rate_windows(baseline_metrics["series"]["cpu"], obs_start, obs_end, CPU_WIN, CPU_STEP) if v is not None]
    return {"min": min(vals), "max": max(vals), "n_windows": len(vals)} if vals else None


def _median(xs):
    xs = [x for x in xs if x is not None]
    return round(st.median(xs), 6) if xs else None


def node_describe(metrics, a, b):
    s = metrics["series"]
    w = {k: [v for _, v in rate_windows(s[k], a, b, NODE_WIN, NODE_STEP)] for k in NODE_KEYS}
    lat = [wt / wc if wt is not None and wc else None for wt, wc in zip(w["write_time"], w["writes_completed"])]
    return {"write_MBps": _median([x / 1e6 if x is not None else None for x in w["written_bytes"]]),
            "read_MBps": _median([x / 1e6 if x is not None else None for x in w["read_bytes"]]),
            "util": _median(w["io_time"]), "avg_queue_depth": _median(w["io_time_weighted"]),
            "write_latency_ms": _median([x * 1000 if x is not None else None for x in lat]),
            "psi_waiting": _median(w["psi_waiting"]), "psi_stalled": _median(w["psi_stalled"]),
            "iowait_cores": _median(w["iowait"])}


def stage_eval(stage, *, rows, obs0, metrics, envelope, pre_window, timing):
    """한 스트레스 단계 판정(계약 §6). stage = {name, role, target_write_mbps, start_utc, end_utc, writer:{rc, ran_sec}}."""
    s, e = _ts(stage["start_utc"]), _ts(stage["end_utc"])
    v = wa.stage_slo_verdict(rows, obs0, s, e)
    if v is None or not v["computable"]:
        return {"name": stage["name"], "role": stage.get("role"), "computable": False, "reason": v and v["reason"]}
    t_slo = min((_ts(x["t_slo"]) for x in v["episodes"] if _ts(x["t_slo"]) >= s), default=None) if v["violating"] else None
    guard_end = min(t_slo, e) if t_slo else e
    # 창 끝이 단계 시작 15 s 뒤부터(첫 창은 일부 주입 전 구간 포함 - 정상 쪽이라 guard를 느슨하게 하지 않음),
    # 빠른 위반에서도 guard 창이 0개가 되지 않게
    cpu = [(t, x) for t, x in rate_windows(metrics["series"]["cpu"], s - timedelta(seconds=CPU_WIN - CPU_STEP), guard_end,
                                           CPU_WIN, CPU_STEP) if x is not None]
    out_hi = [round(x, 4) for _, x in cpu if x > envelope["max"]]
    out_lo = [round(x, 4) for _, x in cpu if x < envelope["min"]]
    pre_s, pre_e = pre_window
    pre_psi = [x for _, x in rate_windows(metrics["series"]["psi_waiting"], pre_s, pre_e, NODE_WIN, NODE_STEP) if x is not None]
    stg_psi = [x for _, x in rate_windows(metrics["series"]["psi_waiting"], s, e, NODE_WIN, NODE_STEP) if x is not None]
    w = stage.get("writer") or {}
    writer_ok = w.get("rc") == 0 and (w.get("ran_sec") or 0) >= WRITER_MIN_RAN_FRACTION * timing["stress_sec"]
    return {"name": stage["name"], "role": stage.get("role"), "target_write_mbps": stage["target_write_mbps"],
            "computable": True, "slo_normal": not v["violating"], "t_slo": t_slo.isoformat() if t_slo else None,
            "t_slo_rel_stage_sec": round((t_slo - s).total_seconds(), 1) if t_slo else None,
            "cpu_guard_pass": bool(cpu) and not out_hi and not out_lo, "cpu_windows": len(cpu),
            "cpu_out_high": out_hi, "cpu_out_low": out_lo, "cpu_median": _median([x for _, x in cpu]),
            "io_change": bool(pre_psi) and bool(stg_psi) and st.median(stg_psi) > max(pre_psi),
            "psi_pre_max": round(max(pre_psi), 6) if pre_psi else None, "psi_stage_median": _median(stg_psi),
            "writer_ok": writer_ok, "writer": w, "integrity": integrity(metrics, s, e),
            "node": node_describe(metrics, s, e), "pre_node": node_describe(metrics, pre_s, pre_e)}


def analyze_session(summary, metrics, envelope, timing):
    """세션 유효성(계약 §8) + 단계 판정. 결과(SLO·I/O 크기)는 유효성에 쓰지 않는다."""
    reasons = []
    if summary.get("outcome_status") != "completed":
        reasons.append(f"outcome={summary.get('outcome_status')}: {summary.get('outcome_reason')}")
    if summary.get("cleanup_errors"):
        reasons.append("cleanup_errors")
    if not summary.get("observation_start_utc"):
        reasons.append("readiness_not_reached")
    if not metrics:
        reasons.append("metrics_missing")
    if (summary.get("prometheus_node") or "") == (summary.get("target_pod") or {}).get("node"):
        reasons.append("prometheus_on_target_node")
    if not summary.get("target_uid_constant"):
        reasons.append("target_identity_changed")
    if reasons:
        return {"run": summary.get("run"), "kind": summary.get("kind"), "valid": False, "invalid_reasons": reasons, "stages": []}
    rows = slo_judge.load_raw(Path(summary["probe_raw_csv"]))
    obs0, obs1 = _ts(summary["observation_start_utc"]), _ts(summary["observation_end_utc"])
    integ = integrity(metrics, obs0, obs1)
    if not integ["ok"]:
        return {"run": summary["run"], "kind": summary["kind"], "valid": False,
                "invalid_reasons": [f"integrity:{integ['problems']}"], "stages": [], "integrity": integ}
    if summary["kind"] == "baseline":
        v = wa.stage_slo_verdict(rows, obs0, obs0, obs1)
        ok = bool(v and v["computable"])
        return {"run": summary["run"], "kind": "baseline", "valid": ok, "slo_normal": ok and not v["violating"],
                "cpu_envelope": cpu_envelope(metrics, obs0, obs1), "node": node_describe(metrics, obs0, obs1),
                "integrity": integ, "invalid_reasons": [] if ok else ["probe_not_computable"]}
    pre = (_ts(summary["pre_stress"]["start_utc"]), _ts(summary["pre_stress"]["end_utc"]))
    stages = [stage_eval(x, rows=rows, obs0=obs0, metrics=metrics, envelope=envelope, pre_window=pre, timing=timing)
              for x in summary.get("stress_stages") or []]
    bad = [f"stage_not_computable:{x['name']}" for x in stages if not x["computable"]]
    bad += [f"writer_not_ok:{x['name']}" for x in stages if x.get("computable") and not x["writer_ok"]]
    pre_v = wa.stage_slo_verdict(rows, obs0, *pre)
    if not pre_v or not pre_v["computable"]:
        bad.append("pre_stress_not_computable")
    elif pre_v["violating"]:
        bad.append("pre_stress_slo_violation")   # 스트레스 전부터 위반 = 환경 문제(계약 §8)
    return {"run": summary["run"], "kind": summary["kind"], "valid": not bad, "invalid_reasons": bad, "stages": stages,
            "integrity": integ, "slo_boundary_stop": summary.get("slo_boundary_stage")}


def weak_ok(x):
    return x["computable"] and x["writer_ok"] and x["io_change"] and x["slo_normal"] and x["cpu_guard_pass"] and x["integrity"]["ok"]


def strong_ok(x):
    return x["computable"] and x["writer_ok"] and (not x["slo_normal"]) and x["cpu_guard_pass"] and x["integrity"]["ok"]


def select_dev(reps, ladder):
    """개발 선택 규칙(계약 §7): weak = 모든 유효 rep에서 weak 조건을 만족하는 가장 높은 강도,
    strong = 모든 유효 rep에서 strong 조건을 만족하는 가장 낮은 강도이면서 weak보다 높은 것. 없으면 None."""
    valid = [r for r in reps if r["valid"]]
    if len(valid) < 2:
        return {"weak": None, "strong": None, "reason": f"valid_dev_reps={len(valid)}<2"}
    order = [x["name"] for x in ladder]

    def all_reps(name, f):
        hit = [s for r in valid for s in r["stages"] if s["name"] == name]
        return len(hit) == len(valid) and all(f(s) for s in hit)
    weak = [n for n in order if all_reps(n, weak_ok)]
    strong = [n for n in order if all_reps(n, strong_ok)]
    w = weak[-1] if weak else None
    s = next((n for n in strong if w is None or order.index(n) > order.index(w)), None)
    return {"weak": w, "strong": s, "reason": None if w and s else "weak_or_strong_missing",
            "weak_candidates": weak, "strong_candidates": strong, "valid_reps": [r["run"] for r in valid]}


def confirm_verdict(reps):
    """확증 판정(계약 §6): 처음 두 유효 rep에서 조건 1–4가 모두 참이어야 feasible."""
    valid = [r for r in reps if r["valid"]][:2]
    if len(valid) < 2:
        return {"feasible": False, "reason": f"valid_confirm_reps={len(valid)}<2", "conditions": None}
    by = [{s["role"]: s for s in r["stages"]} for r in valid]
    cond = {"1_strong_slo_degradation_2of2": all(not b["strong"]["slo_normal"] for b in by),
            "2_strong_cpu_guard_2of2": all(b["strong"]["cpu_guard_pass"] for b in by),
            "3_weak_io_change_slo_normal_cpu_2of2": all(b["weak"]["io_change"] and b["weak"]["slo_normal"]
                                                        and b["weak"]["cpu_guard_pass"] for b in by),
            "4_measurement_integrity": all(r["integrity"]["ok"] and all(s["integrity"]["ok"] for s in r["stages"])
                                           for r in valid)}
    return {"feasible": all(cond.values()), "conditions": cond, "reps": [r["run"] for r in valid]}


def analyze(results_dir: Path, timing: dict, ladder: list):
    sums = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(results_dir.glob("summary-*.json"))]

    def metrics_of(s):
        p = results_dir / f"metrics-{s['run_id']}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    base = [analyze_session(s, metrics_of(s), None, timing) for s in sums if s["kind"] == "baseline"]
    base_ok = [b for b in base if b["valid"] and b.get("slo_normal")]
    env = base_ok[0]["cpu_envelope"] if base_ok else None
    out = {"analyzer": "iof_stage0_analysis", "counted": False, "baseline": base, "cpu_envelope": env}
    if env is None:
        out["verdict"] = {"feasible": False, "reason": "no_valid_baseline"}
        return out
    dev = [analyze_session(s, metrics_of(s), env, timing) for s in sums if s["kind"] == "development"]
    conf = [analyze_session(s, metrics_of(s), env, timing) for s in sums if s["kind"] == "confirmatory"]
    out.update(development=dev, development_selection=select_dev(dev, ladder) if dev else None,
               confirmatory=conf, verdict=confirm_verdict(conf) if conf else None)
    return out


def main(argv=None) -> int:  # pragma: no cover
    import yaml
    p = argparse.ArgumentParser()
    p.add_argument("--results-dir", default=str(Path(__file__).parent / "results" / "capstone_iof_stage0"))
    p.add_argument("--config", default=str(Path(__file__).resolve().parent.parent / "chaos" / "iof-stage0.yaml"))
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    cfg = yaml.safe_load(Path(a.config).read_text(encoding="utf-8"))
    res = analyze(Path(a.results_dir), cfg["timing"], cfg["io"]["development_ladder"])
    res["analyzed_at_utc"] = datetime.now(timezone.utc).isoformat()
    with open(a.out, "x", encoding="utf-8") as fh:
        fh.write(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    print(json.dumps({k: res.get(k) for k in ("cpu_envelope", "development_selection", "verdict")}, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
