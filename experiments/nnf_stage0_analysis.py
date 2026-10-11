#!/usr/bin/env python3
"""noisy-neighbor CPU Stage 0 분석(새 파일, 2026-10-10, counted=false) - 계약: docs/design/phase8-nnf-stage0-contract-20261010.md

판정은 기계 규칙. 공식 SLO는 동결 slo_episodes r2 / slo_judge v3(v2wl stage_slo_verdict 재사용).
지연·SLO 값은 결과(t_slo·라벨)로만 쓰고 단일 지표 규칙에는 넣지 않는다. 이웃 pod 자신의 CPU는 '부하 적용 확인'에만 쓴다.
단일 지표 규칙의 임계값은 기준선·개발(사전 구간과 weak 이하 정상 단계) 증거로만 계산하고, 확증 자료로는 다시 계산하지 않는다.
IF2·R 학습·보정 없음."""
import argparse
import json
import statistics as st
from datetime import timedelta
from pathlib import Path

import iof_stage0_analysis as ia
import slo_judge
import v2_workload_pilot_analysis as wa

NODE_CPUS = 8
CONSECUTIVE = 3
GAP_FACTOR = 2.5
NOMINAL_SCRAPE = {"cpu": 10.0, "thr_periods": 10.0, "periods": 10.0, "burner_cpu": 10.0, "gen_tokens": 15.0,
                  "psi_cpu": 30.0, "schedstat_wait": 30.0, "load1": 30.0, "cpu_idle": 30.0}
# 사전 등록 단일 지표 후보(결과를 보고 추가·삭제·방향 변경 금지). kind: rate = counter 증가율, gauge = 창 안 마지막 표본,
# ratio = 두 counter 증가율의 비, busy = 노드 CPU 수 − idle 증가율. direction: upper = 값↑이 경합↑, two_sided = 양쪽.
RULE_METRICS = {
    "A_node_cpu_psi_waiting": {"kind": "rate", "keys": ["psi_cpu"], "win": 120.0, "step": 30.0, "direction": "upper"},
    "B_node_schedstat_waiting": {"kind": "rate", "keys": ["schedstat_wait"], "win": 120.0, "step": 30.0, "direction": "upper"},
    "C_node_load1": {"kind": "gauge", "keys": ["load1"], "win": 60.0, "step": 30.0, "direction": "upper"},
    "D_vllm_container_cpu": {"kind": "rate", "keys": ["cpu"], "win": 60.0, "step": 15.0, "direction": "two_sided"},
    "E_vllm_cfs_throttled_ratio": {"kind": "ratio", "keys": ["thr_periods", "periods"], "win": 60.0, "step": 15.0,
                                   "direction": "two_sided"},
    "F_node_cpu_busy_cores": {"kind": "busy", "keys": ["cpu_idle"], "win": 120.0, "step": 30.0, "direction": "upper"},
}
FORBIDDEN_RULE_KEYS = ("burner_cpu", "gen_tokens")       # 주입 표식·진행 지표는 규칙 입력 아님
FORBIDDEN_WORDS = ("latency", "slo", "probe", "ttft", "e2e", "burner", "neighbor")
ALIVE_MARKERS = ("active pod OOMKilled", "active pod 재시작", "active pod 식별 모호/소실", "Node 상태 이상", "NotReady")
MEASUREMENT_REASONS = ("integrity", "probe_on_target", "probe_node_unknown", "prometheus_on_target", "target_identity_changed")


def selectors(pod: str, node_instance: str, burner_pod: str) -> dict:
    n = f'instance="{node_instance}"'
    c = f'namespace="vllm-serving",pod="{pod}",container="vllm",job="kubelet"'
    return {"cpu": f"container_cpu_usage_seconds_total{{{c}}}",
            "thr_periods": f"container_cpu_cfs_throttled_periods_total{{{c}}}",
            "periods": f"container_cpu_cfs_periods_total{{{c}}}",
            "gen_tokens": f'vllm:generation_tokens_total{{pod="{pod}",job="vllm-active"}}',
            "burner_cpu": f'container_cpu_usage_seconds_total{{namespace="vllm-serving",pod="{burner_pod}",container="{burner_pod}",job="kubelet"}}',
            "psi_cpu": f"node_pressure_cpu_waiting_seconds_total{{{n}}}",
            "schedstat_wait": f"node_schedstat_waiting_seconds_total{{{n}}}",
            "load1": f"node_load1{{{n}}}",
            "cpu_idle": f'node_cpu_seconds_total{{{n},mode="idle"}}'}


def validate_rule_metrics(rules: dict) -> None:
    """규칙 후보가 지연·SLO·probe·이웃 pod CPU를 입력으로 쓰지 않는지(계약 §7). 어기면 ValueError."""
    for name, r in rules.items():
        bad = [k for k in r["keys"] if k in FORBIDDEN_RULE_KEYS or k not in NOMINAL_SCRAPE]
        bad += [w for w in FORBIDDEN_WORDS if w in name.lower() or any(w in k.lower() for k in r["keys"])]
        if bad:
            raise ValueError(f"금지된 규칙 입력 {name}: {bad}")


validate_rule_metrics(RULE_METRICS)


def _ts(x):
    return wa._ts(x)


def metric_values(metrics, name, a, b):
    """규칙 지표 값 [(창 끝 시각 t, 값|None)], t = a, a+step, … ≤ b. 창은 [t−win, t]."""
    r, s = RULE_METRICS[name], metrics["series"]
    start = a - timedelta(seconds=r["win"])
    if r["kind"] in ("rate", "busy"):
        out = ia.rate_windows(s[r["keys"][0]], start, b, r["win"], r["step"])
        if r["kind"] == "busy":
            out = [(t, NODE_CPUS - v if v is not None else None) for t, v in out]
        return out
    if r["kind"] == "ratio":
        num = ia.rate_windows(s[r["keys"][0]], start, b, r["win"], r["step"])
        den = ia.rate_windows(s[r["keys"][1]], start, b, r["win"], r["step"])
        return [(t, x / y if x is not None and y else None) for (t, x), (_, y) in zip(num, den)]
    out, t = [], a.timestamp()
    while t <= b.timestamp() + 1e-6:
        pts = [v for u, v in s[r["keys"][0]] if t - r["win"] < u <= t]
        out.append((t, pts[-1] if pts else None))
        t += r["step"]
    return out


def describe(values):
    xs = sorted(v for _, v in values if v is not None)
    if not xs:
        return {"n": 0}

    def q(p):
        return round(xs[min(len(xs) - 1, int(p * (len(xs) - 1) + 0.5))], 6)
    return {"n": len(xs), "median": round(st.median(xs), 6), "min": round(xs[0], 6), "p10": q(0.1), "p90": q(0.9),
            "max": round(xs[-1], 6), "trajectory": [[round(t, 1), None if v is None else round(v, 6)] for t, v in values]}


def _triples(values):
    """연속 non-None 3개 묶음(None은 연속을 끊는다)."""
    run = []
    for _, v in values:
        if v is None:
            run = []
            continue
        run.append(v)
        if len(run) >= CONSECUTIVE:
            yield run[-CONSECUTIVE:]


def calibrate_rule(segments_values, direction):
    """정상·weak 보정 증거에서 3-consecutive 오탐 0회인 가장 민감한 임계값(계약 §7).
    upper: t = max(각 연속 3창의 최솟값), 발화 = 3창 연속 값 > t. lower: t = min(각 연속 3창의 최댓값), 발화 = 3창 연속 값 < t."""
    trip = [x for seg in segments_values for x in _triples(seg)]
    if not trip:
        return {"upper": None, "lower": None, "n_triples": 0}
    return {"upper": max(min(x) for x in trip) if direction in ("upper", "two_sided") else None,
            "lower": min(max(x) for x in trip) if direction == "two_sided" else None, "n_triples": len(trip)}


def first_trigger(values, rule):
    """3창 연속 임계 초과(upper: > upper, lower: < lower)의 세 번째 창 끝 시각(epoch s). None 창은 연속을 끊는다."""
    hi = lo = 0
    for t, v in values:
        if v is None:
            hi = lo = 0
            continue
        hi = hi + 1 if rule.get("upper") is not None and v > rule["upper"] else 0
        lo = lo + 1 if rule.get("lower") is not None and v < rule["lower"] else 0
        if hi >= CONSECUTIVE or lo >= CONSECUTIVE:
            return t
    return None


def integrity(metrics, a, b, skip=()):
    gaps, bad = {}, []
    for k, s in metrics["series"].items():
        if k in skip:
            continue
        g = ia.max_gap(s, a, b)
        gaps[k] = round(g, 1)
        if g > GAP_FACTOR * NOMINAL_SCRAPE[k]:
            bad.append(f"{k}_gap={g:.1f}s")
    return {"ok": not bad, "max_gap_sec": gaps, "problems": bad}


def stage_eval(stage, *, rows, obs0, metrics, pre_window, timing, applied_min_fraction):
    s, e = _ts(stage["start_utc"]), _ts(stage["end_utc"])
    base = {"name": stage["name"], "role": stage.get("role"), "workers": stage["workers"],
            "_start": stage["start_utc"], "_end": stage["end_utc"]}
    v = wa.stage_slo_verdict(rows, obs0, s, e)
    if v is None or not v["computable"]:
        return {**base, "computable": False, "reason": v and v["reason"]}
    t_slo = min((_ts(x["t_slo"]) for x in v["episodes"] if _ts(x["t_slo"]) >= s), default=None) if v["violating"] else None
    pre_s, pre_e = pre_window
    pre_psi = [x for _, x in metric_values(metrics, "A_node_cpu_psi_waiting", pre_s, pre_e) if x is not None]
    stg_psi = [x for _, x in metric_values(metrics, "A_node_cpu_psi_waiting", s, e) if x is not None]
    burn = [x for _, x in ia.rate_windows(metrics["series"]["burner_cpu"], s, e, 60.0, 15.0) if x is not None]
    need = applied_min_fraction * min(stage["workers"], NODE_CPUS)
    b = stage.get("burner") or {}
    burner_ok = b.get("rc") == 0 and (b.get("ran_sec") or 0) >= 0.95 * timing["stress_sec"]
    return {**base, "computable": True, "slo_normal": not v["violating"], "t_slo": t_slo.isoformat() if t_slo else None,
            "t_slo_rel_stage_sec": round((t_slo - s).total_seconds(), 1) if t_slo else None,
            "contention_change": bool(pre_psi) and bool(stg_psi) and st.median(stg_psi) > max(pre_psi),
            "psi_pre_max": round(max(pre_psi), 6) if pre_psi else None, "psi_stage_median": ia._median(stg_psi),
            "stress_applied": burner_ok and bool(burn) and st.median(burn) >= need,
            "burner_cpu_median": ia._median(burn), "burner_cpu_needed": need, "burner_ok": burner_ok, "burner": b,
            "integrity": integrity(metrics, s, e),
            "metrics": {k: describe(metric_values(metrics, k, s, e)) for k in RULE_METRICS},
            "pre_metrics": {k: describe(metric_values(metrics, k, pre_s, pre_e)) for k in RULE_METRICS}}


def analyze_session(summary, metrics, timing, applied_min_fraction=0.4):
    reasons = []
    if summary.get("outcome_status") != "completed":
        reasons.append(f"outcome={summary.get('outcome_status')}: {summary.get('outcome_reason')}")
    if summary.get("cleanup_errors"):
        reasons.append("cleanup_errors")
    if not summary.get("observation_start_utc"):
        reasons.append("readiness_not_reached")
    if not metrics:
        reasons.append("metrics_missing")
    target = (summary.get("target_pod") or {}).get("node")
    if (summary.get("prometheus_node") or "") == target:
        reasons.append("prometheus_on_target_node")
    pn = (summary.get("probe_pod") or {}).get("node")
    if not pn:
        reasons.append("probe_node_unknown")
    elif pn == target:
        reasons.append("probe_on_target_node")
    if not summary.get("target_uid_constant"):
        reasons.append("target_identity_changed")
    # 안전 감시 중단(active OOM·재시작·소실·노드 이상) = slow-but-alive 위반으로 기록(기준 1). 동결 안전 감시 문구로 판별
    reason = str(summary.get("outcome_reason") or "")
    alive_violation = summary.get("outcome_status") in ("aborted", "harness_error") and any(m in reason for m in ALIVE_MARKERS)
    base = {"run": summary.get("run"), "kind": summary.get("kind"), "alive_violation": alive_violation}
    if reasons:
        return {**base, "valid": False, "invalid_reasons": reasons, "stages": []}
    rows = slo_judge.load_raw(Path(summary["probe_raw_csv"]))
    obs0, obs1 = _ts(summary["observation_start_utc"]), _ts(summary["observation_end_utc"])
    integ = integrity(metrics, obs0, obs1, skip=("burner_cpu",) if summary["kind"] == "baseline" else ())   # 기준선엔 부하 pod 없음
    if not integ["ok"]:
        return {**base, "valid": False, "invalid_reasons": [f"integrity:{integ['problems']}"], "stages": [], "integrity": integ}
    if summary["kind"] == "baseline":
        v = wa.stage_slo_verdict(rows, obs0, obs0, obs1)
        ok = bool(v and v["computable"])
        return {**base, "valid": ok, "slo_normal": ok and not v["violating"], "integrity": integ,
                "observation": [obs0.isoformat(), obs1.isoformat()],
                "metrics": {k: describe(metric_values(metrics, k, obs0, obs1)) for k in RULE_METRICS},
                "invalid_reasons": [] if ok else ["probe_not_computable"]}
    pre = (_ts(summary["pre_stress"]["start_utc"]), _ts(summary["pre_stress"]["end_utc"]))
    stages = [stage_eval(x, rows=rows, obs0=obs0, metrics=metrics, pre_window=pre, timing=timing,
                         applied_min_fraction=applied_min_fraction) for x in summary.get("stress_stages") or []]
    bad = [f"stage_not_computable:{x['name']}" for x in stages if not x["computable"]]
    bad += [f"burner_not_ok:{x['name']}" for x in stages if x.get("computable") and not x["burner_ok"]]
    pre_v = wa.stage_slo_verdict(rows, obs0, *pre)
    if not pre_v or not pre_v["computable"]:
        bad.append("pre_stress_not_computable")
    elif pre_v["violating"]:
        bad.append("pre_stress_slo_violation")
    if summary["kind"] != "development" and len(stages) != 1:
        bad.append(f"confirm_stage_count={len(stages)}")
    return {**base, "valid": not bad, "invalid_reasons": bad, "stages": stages, "integrity": integ,
            "observation": [obs0.isoformat(), obs1.isoformat()], "pre_stress": [pre[0].isoformat(), pre[1].isoformat()],
            "slo_boundary_stop": summary.get("slo_boundary_stage")}


def weak_ok(x):
    return x["computable"] and x["stress_applied"] and x["contention_change"] and x["slo_normal"] and x["integrity"]["ok"]


def strong_ok(x):
    return x["computable"] and x["stress_applied"] and (not x["slo_normal"]) and x["integrity"]["ok"]


def select_dev(reps, ladder):
    """개발 선택 규칙(계약 §6): weak = 모든 유효 rep에서 weak인 가장 높은 강도, strong = 모든 유효 rep에서 strong인 가장
    낮은 강도이면서 weak보다 높은 것. 유효 rep 2개 필요. 없으면 None."""
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


def calibration_segments(baseline, dev, ladder, weak):
    """보정 증거 구간(계약 §7): 유효 기준선 관찰 + 유효 개발 rep의 사전 구간 + weak 이하 강도 중 SLO 정상 단계.
    weak보다 높은 단계(strong 후보 포함)·회복 구간·확증 자료는 넣지 않는다. 반환 [(run, label, a_iso, b_iso)]."""
    order = [x["name"] for x in ladder]
    wi = order.index(weak)
    segs = [(b["run"], "baseline", *b["observation"]) for b in baseline if b["valid"] and b.get("slo_normal")]
    for r in dev:
        if not r["valid"]:
            continue
        segs.append((r["run"], "pre_stress", *r["pre_stress"]))
        segs += [(r["run"], s["name"], s["_start"], s["_end"]) for s in r["stages"]
                 if s["name"] in order and order.index(s["name"]) <= wi and s["computable"] and s["slo_normal"]]
    return segs


def calibrate(segments, metrics_by_run):
    """지표별 임계값(계약 §7). segments의 구간 값만 쓴다."""
    out = {}
    for name, r in RULE_METRICS.items():
        vals = [metric_values(metrics_by_run[run], name, _ts(a), _ts(b)) for run, _, a, b in segments]
        c = calibrate_rule(vals, r["direction"])
        out[name] = {"direction": r["direction"], "kind": r["kind"], "keys": r["keys"], "window_sec": r["win"],
                     "step_sec": r["step"], "consecutive": CONSECUTIVE, "missing": "None 창은 연속을 끊고 발화하지 않음",
                     "upper": c["upper"], "lower": c["lower"], "n_triples": c["n_triples"],
                     "calibration_segments": [list(x) for x in segments]}
    return out


def rule_eval(conf_weak, conf_strong, rules, metrics_by_run):
    """기준 5(계약 §8): 지표마다 weak 2/2 무발화(관찰 전체) AND strong 2/2 t_slo 전 발화(단계 시작부터)면 trivial."""
    res = {}
    for name in RULE_METRICS:
        rule = rules[name]
        weak_rows, strong_rows = [], []
        for r in conf_weak:
            a = _ts(r["observation"][0])
            t = first_trigger(metric_values(metrics_by_run[r["run"]], name, a, _ts(r["observation"][1])), rule)
            weak_rows.append({"run": r["run"], "false_trigger": t is not None,
                              "first_trigger_rel_obs_sec": None if t is None else round(t - a.timestamp(), 1)})
        for r in conf_strong:
            s = r["stages"][0]
            a, b = _ts(s["_start"]), _ts(s["_end"])
            t = first_trigger(metric_values(metrics_by_run[r["run"]], name, a, b), rule)
            tslo = _ts(s["t_slo"]).timestamp() if s.get("t_slo") else None
            pre_t = first_trigger(metric_values(metrics_by_run[r["run"]], name, _ts(r["pre_stress"][0]),
                                                _ts(r["pre_stress"][1])), rule)
            strong_rows.append({"run": r["run"], "trigger_rel_stage_sec": None if t is None else round(t - a.timestamp(), 1),
                                "t_slo_rel_stage_sec": s.get("t_slo_rel_stage_sec"),
                                "pre_slo": t is not None and tslo is not None and t < tslo,
                                "lead_sec": round(tslo - t, 1) if t is not None and tslo is not None else None,
                                "pre_stress_trigger": pre_t is not None})
        trivial = len(weak_rows) >= 2 and len(strong_rows) >= 2 and not any(x["false_trigger"] for x in weak_rows) \
            and all(x["pre_slo"] for x in strong_rows)
        res[name] = {"rule": {k: rule[k] for k in ("direction", "upper", "lower", "consecutive", "window_sec", "step_sec")},
                     "weak": weak_rows, "strong": strong_rows, "trivial_separator": trivial}
    return res


def final_verdict(weak_reps, strong_reps, rules, metrics_by_run):
    """계약 §8. 각 종류 처음 두 유효 rep. 상태 = SCENARIO_FEASIBLE·AIOPS_JUSTIFIED."""
    vw = [r for r in weak_reps if r["valid"]][:2]
    vs = [r for r in strong_reps if r["valid"]][:2]
    attempts = weak_reps + strong_reps
    weak_exists = len(vw) == 2 and all(weak_ok(r["stages"][0]) for r in vw)
    alive = not any(r.get("alive_violation") for r in strong_reps)
    c1 = len(vs) == 2 and alive and all(strong_ok(r["stages"][0]) for r in vs)
    c3 = len(vw) == 2 and len(vs) == 2 and not any(
        x.startswith(MEASUREMENT_REASONS) for r in attempts for x in r["invalid_reasons"])
    c4 = len(vs) == 2 and all(not r["stages"][0]["slo_normal"] for r in vs)
    rule = rule_eval(vw, vs, rules, metrics_by_run) if len(vw) == 2 and len(vs) == 2 else None
    c2 = None if rule is None else not rule["D_vllm_container_cpu"]["trivial_separator"]
    c5 = None if rule is None else not any(x["trivial_separator"] for x in rule.values())
    feasible = bool(weak_exists and c1 and c3 and c4)
    return {"weak_reps": [r["run"] for r in vw], "strong_reps": [r["run"] for r in vs],
            "criteria": {"W_weak_state_2of2": weak_exists, "C1_strong_slow_but_alive_2of2": c1,
                         "C2_vllm_cpu_not_trivial": c2, "C3_measurement_integrity": c3, "C4_strong_degradation_2of2": c4,
                         "C5_no_trivial_single_metric": c5},
            "single_metric": rule, "SCENARIO_FEASIBLE": feasible, "AIOPS_JUSTIFIED": bool(feasible and c5)}


def analyze(results_dir: Path, cfg: dict):
    timing, ladder = cfg["timing"], cfg["stress"]["development_ladder"]
    frac = cfg["stress"]["applied_min_fraction"]
    sums = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(results_dir.glob("summary-*.json"))]
    mets = {}
    for s in sums:
        p = results_dir / f"metrics-{s['run_id']}.json"
        mets[s["run"]] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def an(kind):
        return [analyze_session(s, mets[s["run"]], timing, frac) for s in sums if s["kind"] == kind]
    base, dev = an("baseline"), an("development")
    out = {"analyzer": "nnf_stage0_analysis", "counted": False, "baseline": base, "development": dev,
           "development_selection": select_dev(dev, ladder) if dev else None}
    sel = out["development_selection"] or {}
    if sel.get("weak") and sel.get("strong"):
        out["calibration_preview"] = calibrate(calibration_segments(base, dev, ladder, sel["weak"]), mets)
    rules = cfg.get("single_metric_rules")
    weak, strong = an("confirm_weak"), an("confirm_strong")
    out.update(confirm_weak=weak, confirm_strong=strong)
    if rules and (weak or strong):
        guard = amendment_guard(rules, out.get("calibration_preview"), results_dir,
                                [s["run_id"] for s in sums if s["kind"] in ("confirm_weak", "confirm_strong")])
        out["amendment_guard"] = guard
        out["verdict"] = final_verdict(weak, strong, rules, mets) if guard["ok"] else {"blocked": guard["problems"]}
    return out


def amendment_guard(rules, preview, results_dir: Path, confirm_run_ids):
    """고정 규칙이 개발 자료 보정값과 같은지, 모든 확증 run이 같은 config 해시로 돌았는지(결과 뒤 임계값 수정 차단)."""
    probs = []
    if not preview:
        probs.append("calibration_preview_unavailable")
    else:
        probs += [f"rule_not_from_calibration:{k}" for k in RULE_METRICS
                  if (rules[k].get("upper"), rules[k].get("lower")) != (preview[k]["upper"], preview[k]["lower"])]
    shas = set()
    for rid in confirm_run_ids:
        p = results_dir / f"contract-{rid}.json"
        shas.add(json.loads(p.read_text(encoding="utf-8")).get("config_sha256") if p.exists() else None)
    if len(shas) > 1 or None in shas:
        probs.append(f"confirm_config_mismatch:{sorted(map(str, shas))}")
    return {"ok": not probs, "problems": probs}


def main(argv=None) -> int:  # pragma: no cover
    import yaml
    p = argparse.ArgumentParser()
    p.add_argument("--results-dir", default=str(Path(__file__).parent / "results" / "capstone_nnf_stage0"))
    p.add_argument("--config", default=str(Path(__file__).resolve().parent.parent / "chaos" / "nnf-stage0.yaml"))
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    cfg = yaml.safe_load(Path(a.config).read_text(encoding="utf-8"))
    res = analyze(Path(a.results_dir), cfg)
    with open(a.out, "x", encoding="utf-8") as fh:
        fh.write(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    print(json.dumps({"development_selection": res.get("development_selection")}, ensure_ascii=False, default=str))
    if res.get("verdict"):
        v = res["verdict"]
        print(json.dumps({k: v[k] for k in ("criteria", "SCENARIO_FEASIBLE", "AIOPS_JUSTIFIED")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
