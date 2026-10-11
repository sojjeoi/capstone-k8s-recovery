#!/usr/bin/env python3
"""guarded A/B 공식 분석기 - 계약 r2 + docs/design/phase8-v33c1-guarded-ab-analysis-rules-20261007.md를 그대로 코드화.

원자료는 읽기만 한다. 공식 집계는 ab01~ab15 + 계약·요약 메타데이터 + arm 구조 정보가 모두 맞는 실행만.
쌍 효과 D = A 누적 − B 누적(초) - 양수면 B의 공식 SLO 위반시간이 더 적다. 자연어 outcome_reason은 판정에 쓰지 않는다.
결과는 새 파일로만 쓴다(덮어쓰기 없음). 같은 원자료 → 같은 result_digest."""
import argparse
import csv
import hashlib
import json
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import guarded_cleanup_gate as gcg
import run_guarded_pilot as gp
import slo_episodes
import slo_judge
import timed_switch_verdict as verdict
from cpu_pressure_demo_adapter import MIN_EFFECTIVE_CPU_RISE_CORES
from scipy.stats import binomtest, rankdata

HERE = Path(__file__).parent
RULES_DOC = HERE.parent / "docs" / "design" / "phase8-v33c1-guarded-ab-analysis-rules-20261007.md"
CONTRACT_R1 = HERE.parent / "docs" / "design" / "phase8-v33c1-guarded-ab-comparison-contract-20261007.md"
OFFICIAL_RE = re.compile(r"ab(0[1-9]|1[0-5])[ab]")
FROZEN_THRESHOLD = -0.07667538512242333
STRESS_CRITERION = "raw-counter-v2.3-guarded-ab-20261007"
ARMS = {"a": ("A_no_switch", gp.MODE_RO), "b": ("B_if_switch", gp.MODE_ACT)}
OBSERVATION_ERRORS = ("harness_error", "trial_cleanup_incomplete", "old_recovery_policy_not_isolated_at_end")
B_MECH_OUTCOMES = {"promoted_unverified", "promotion_unknown"}
SLOW_SEC = 2 * slo_judge.L_BASELINE


def _sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _ts(x):
    return datetime.fromisoformat(str(x).replace("Z", "+00:00")) if x else None


def _json(p: Path):
    return json.loads(Path(p).read_text(encoding="utf-8-sig"))


def frozen_values() -> dict:
    art = gp.sd.load_artifacts(gp.sd.DEFAULT_ARTIFACTS)
    return {"threshold": FROZEN_THRESHOLD, "artifact_hashes": art["hashes"], "observation_sec": gp.PRESSURE_OBSERVATION_SEC,
            "stress_stages": gp.rt.DEMO_STAGES, "probe_config_sha256": _sha(gp.smoke.DEFAULT_PROBE_CONFIG),
            "probe_image": gp.IMAGE_FORCE_CLOSE, "probe_image_id": gp.IMAGE_FORCE_CLOSE_ID,
            "detector_rule": {"consecutive": 3, "cooldown_sec": 60}, "start_state_rule": "canonical_healthy_r2"}


# ---------------- 실행 읽기·포함 ----------------
def read_run(results_dir: Path, probe_dir: Path, run: str) -> dict:
    rid = gp.run_id_for(run)
    files = {"summary": results_dir / f"summary-{rid}.json", "contract": results_dir / f"contract-{rid}.json",
             "probe_csv": probe_dir / f"probe-{rid}-{gp.rt.ARM_LABEL}-{gp.RUNS[run][2]}-raw.csv"}
    d = {"run": run, "run_id": rid, "missing": []}
    for k in ("summary", "contract"):
        if files[k].exists():
            d[k] = _json(files[k])
        else:
            d["missing"].append(k)
    pf = (d.get("summary") or {}).get("preflight_file")  # 이 실행의 run-preflight(단계별 파일, 2026-10-07 수정)
    if pf:
        files["preflight"] = results_dir / pf
    if pf and files["preflight"].exists():
        d["preflight"] = _json(files["preflight"])
    else:
        d["missing"].append("run_preflight")
    if not files["probe_csv"].exists():
        d["missing"].append("probe_csv")
    d["files"] = {k: {"path": str(p), "sha256": _sha(p) if p.exists() else None} for k, p in sorted(files.items())}
    return d


def exclusion_reasons(run: str, d: dict) -> list:
    """공식 집계 제외 사유(빈 목록 = 포함 후보). ID·계약·요약 메타데이터·arm 구조를 모두 본다."""
    if not OFFICIAL_RE.fullmatch(run) or run[:4] in gp.AB_REHEARSAL_PAIRS:
        return ["not_official_id"]
    s, c = d.get("summary") or {}, d.get("contract") or {}
    r = []
    if c.get("plan_id") != gp.AB_PLAN_ID or c.get("contract_doc") != gp.AB_CONTRACT_DOC:
        r.append(f"contract_plan_or_doc_mismatch: {c.get('plan_id')} {c.get('contract_doc')}")
    for name, comp in (("contract", c.get("comparison")), ("summary", s.get("comparison"))):
        if not isinstance(comp, dict) or comp.get("counted_in_comparison") is not True or comp.get("pair") != run[:4]:
            r.append(f"{name}_comparison_not_counted_or_pair_mismatch: {comp}")
    arm, mode = ARMS[run[-1]]
    if (s.get("comparison") or {}).get("arm") != arm or (c.get("comparison") or {}).get("arm") != arm \
            or s.get("mode") != mode or c.get("mode") != mode or gp.RUNS[run][1] != mode:
        r.append(f"analysis_unknown_arm: id={run[-1]} summary={(s.get('comparison') or {}).get('arm')}/{s.get('mode')} "
                 f"contract={(c.get('comparison') or {}).get('arm')}/{c.get('mode')}")
    return r


# ---------------- 지표 ----------------
def _window(d):
    o = (d.get("summary") or {}).get("trial_outcome") or {}
    return _ts(o.get("t_injection_observed_utc")), _ts(o.get("observation_ended_at_utc"))


def primary(d: dict, probe_csv: Path) -> dict:
    t0, t1 = _window(d)
    rows = slo_judge.load_raw(probe_csv) if probe_csv.exists() else []
    ep = slo_episodes.violation_episodes(rows, t0, t1)
    return {"computable": ep["computable"], "reason": ep["reason"], "cumulative_sec": ep["cumulative_sec"],
            "lower_bound": ep["cumulative_lower_bound"], "t_slo_first": ep["episodes"][0]["t_slo"] if ep["episodes"] else None,
            "episodes": ep["episodes"], "recovered_by_end": None if not ep["computable"] else not ep["cumulative_lower_bound"],
            "requests_in_window": ep["requests_in_window"], "max_gap_in_window_sec": ep["max_gap_in_window_sec"]}


def secondary(d: dict, probe_csv: Path) -> dict:
    t0, t1 = _window(d)
    if not probe_csv.exists() or not t0 or not t1:
        return {"computable": False}
    with open(probe_csv, encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if t0 <= _ts(r["sent_at"]) <= t1]
    lat = [float(r["latency"]) for r in rows]
    ok = [r["success"] in ("True", "true", "1") for r in rows]
    edges = [t0] + sorted(_ts(r["sent_at"]) for r in rows) + [t1]
    q = statistics.quantiles(lat, n=100) if len(lat) >= 2 else None
    return {"computable": True, "requests": len(rows), "success": sum(ok), "failure": len(rows) - sum(ok),
            "timeout": sum(1 for r, s in zip(rows, ok) if not s and not str(r.get("status") or "").isdigit()),
            "p50": q[49] if q else None, "p95": q[94] if q else None, "p99": q[98] if q else None,
            "max_latency": max(lat) if lat else None, "slow_requests": sum(x > SLOW_SEC for x in lat),
            "max_gap_sec": round(max((b - a).total_seconds() for a, b in zip(edges, edges[1:])), 3)}


def _run_action(s: dict) -> dict:
    acts = [a for a in ((s.get("policy_state_after") or {}).get("actions") or {}).values() if a.get("run_id") == s.get("run_id")]
    return acts[0] if len(acts) == 1 else ({"ambiguous": len(acts)} if acts else {})


def b_outcome(s: dict) -> dict:
    act, w = _run_action(s), s.get("watcher") or {}
    pr = act.get("promote_result") or {}
    promoted = (w.get("action") or {}).get("outcome") in gp.PROMOTION_OUTCOMES
    return {"signal_sent": (s.get("evidence_counts") or {}).get("send_attempts", 0) > 0,
            "policy_outcomes": (s.get("evidence_counts") or {}).get("send_results"),
            "promoted": promoted, "action_status": act.get("status"), "promote_called_at": act.get("promote_called_at"),
            "promote_rc": (pr.get("cli") or {}).get("returncode"), "switched_at": (pr.get("verify") or {}).get("switched_at"),
            "service_verified": (w.get("guard_result") or {}).get("result") == "verified" if promoted else None,
            "service_finished_at": act.get("service_finished_at"),
            "d6": {k: (s.get("scale_down_delay_restore") or {}).get(k) for k in ("path", "restored", "held", "restored_at")}}


def stress(d: dict, t_switch) -> dict:
    s = d["summary"]
    rec = s.get("stress_delivery_record") or {}
    raw = [(t, v) for t, v in (s.get("cpu_raw_counter_samples") or [])]
    if not raw or not rec.get("stages"):
        return {"verified": None, "reasons": ["stress_evidence_missing"], "criterion": STRESS_CRITERION}
    r = verdict.evaluate_stress_delivery_raw(
        delivery_record=rec, planned_stage_count=len(gp.rt.DEMO_STAGES), cpu_raw_samples=raw, liveness_samples=[],
        baseline_cpu=(rec.get("target") or {}).get("baseline_cpu_cores"), min_rise=MIN_EFFECTIVE_CPU_RISE_CORES,
        sample_interval_sec=gp.rt.SAMPLE_INTERVAL_SEC, planned_total_sec=gp.rt.PLANNED_STRESS_SEC, continuity_from=t_switch)
    reasons = [x for x in r["reasons"] if x != "no_liveness_samples"]  # 생존은 압박 도구 기록으로 대체(규칙 §2 V3)
    lost = bool(rec.get("target_lost")) or rec.get("end_reason") != "completed"
    below = lost or any(p.get("ok") is False for p in r.get("per_stage") or []) or (r["verified"] is False and not r.get("per_stage"))
    unverifiable = [x for x in reasons if "below_baseline" not in x and not x.startswith("stage_not_completed")]
    v = False if below else (None if unverifiable else True)
    return {**r, "verified": v, "reasons": reasons + (["target_lost_or_not_completed"] if lost else []), "criterion": STRESS_CRITERION}


# ---------------- 실행 유효성 ----------------
def run_validity(d: dict, frozen: dict, prim: dict) -> dict:
    s, c, pre = d.get("summary") or {}, d.get("contract") or {}, d.get("preflight") or {}
    r, notes = [f"missing:{m}" for m in d["missing"]], {}
    o = s.get("trial_outcome") or {}
    if s.get("outcome_status") != "completed" or o.get("injection_valid") is not True:
        r.append(f"V1_not_completed: {s.get('outcome_status')} injection_valid={o.get('injection_valid')}")
    if not (s.get("detector_readiness") or {}).get("ready_at_utc"):
        r.append("V1_detector_not_ready")
    if not prim["computable"]:
        r.append(f"V2_primary_not_computable: {prim['reason']}")
    is_b = d["run"].endswith("b")
    bo = b_outcome(s) if is_b else None
    t_switch = bo["switched_at"] if is_b and bo["promoted"] else None
    st = stress(d, t_switch) if s else {"verified": None, "reasons": ["no_summary"]}
    notes["stress"] = {k: st.get(k) for k in ("verified", "reasons", "criterion")}
    if st["verified"] is not True:
        r.append(f"V3_stress_not_verified: {st['verified']} {st['reasons']}")
    cond = c.get("conditions") or {}
    for k in ("stress_stages", "probe_config_sha256", "probe_image", "probe_image_id", "detector_rule", "start_state_rule"):
        if cond.get(k) != frozen[k]:
            r.append(f"V4_condition_mismatch:{k}")
    if c.get("threshold") != frozen["threshold"] or c.get("artifact_hashes") != frozen["artifact_hashes"] \
            or c.get("observation_sec") != frozen["observation_sec"]:
        r.append("V4_model_threshold_or_observation_mismatch")
    if (s.get("probe_image_runtime_check") or {}).get("verified") is not True:
        r.append("V4_probe_image_not_verified")
    iso = s.get("old_policy_isolation") or {}
    if (iso.get("start") or {}).get("isolated") is not True or (iso.get("end") or {}).get("isolated") is not True:
        r.append("V5_old_policy_not_isolated")
    start_active = ((pre.get("canonical_start") or {}).get("state") or {}).get("active")
    start_pod = (pre.get("canonical_start") or {}).get("start_pod")
    if is_b:
        fa = s.get("final_promotion_assessment") or {}
        end_active = ((fa.get("sources") or {}).get("live") or {}).get("active_selector")
        new = (fa.get("target") or {}).get("new")
        if end_active != start_active and not (bo["promoted"] and bo["action_status"] == "verified" and new == end_active):
            r.append(f"V5_unexplained_selector_change: {start_active}->{end_active}")
        if end_active == start_active and bo["promoted"]:
            r.append("V5_promoted_but_selector_unchanged")
    else:
        rest = [g.get("canonical_restore") for g in s.get("preview_cleanup_gate") or [] if g.get("canonical_restore")]
        fin = (((rest[-1] if rest else {}).get("evidence") or {}).get("final")) or {}
        pod = gcg.active_pod(fin) if fin else None
        try:
            reconciled = gcg.norm_generation(fin.get("observed")) == gcg.norm_generation(fin.get("generation"))
        except ValueError:
            reconciled = False
        if (s.get("watcher") or {}).get("action") is not None or _run_action(s):
            r.append("V5_A_has_policy_action")
        if not (fin and reconciled and fin.get("active") == start_active and pod
                and [pod["name"], pod["uid"]] == list(start_pod or [])):
            r.append("V5_A_end_active_not_start_canonical")
    if pre.get("ok") is not True or (pre.get("canonical_start") or {}).get("problems") != []:
        r.append("V6_start_not_canonical")
    errs = s.get("cleanup_errors") or []
    obs_errs = [e for e in errs if e.startswith(OBSERVATION_ERRORS)]
    if s.get("harness_error") or obs_errs:
        r.append(f"V7_observation_problem: {s.get('harness_error')} {obs_errs}")
    post = [e for e in errs if e not in obs_errs]
    t_inj = _window(d)[0]
    pre_inj = bool(is_b and bo["promoted"] and t_inj and _ts(bo["promote_called_at"]) and _ts(bo["promote_called_at"]) < t_inj)
    mech = bool(is_b and (((s.get("watcher") or {}).get("action") or {}).get("outcome") in B_MECH_OUTCOMES
                          or (bo["promoted"] and not bo["service_verified"]) or bo["d6"].get("held")))
    return {"valid": not r, "reasons": r, "notes": notes, "b_outcome": bo, "pre_injection_promotion": pre_inj,
            "b_mechanism_failure": mech, "post_observation_cleanup_errors": post,
            "next_run_independence_risk": bool(post), "start_active_node": s.get("start_active_node")}


# ---------------- 통계 ----------------
def wilcoxon_exact(diffs: list) -> dict:
    nz = [x for x in diffs if x != 0]
    m = len(nz)
    if m == 0:
        return {"n_nonzero": 0, "zeros_dropped": len(diffs), "t_plus": None, "p_two_sided": None}
    ranks = [float(x) for x in rankdata([abs(x) for x in nz])]  # 동점 = 평균 순위
    t_obs = sum(rk for rk, x in zip(ranks, nz) if x > 0)
    mean = sum(ranks) / 2
    hit = sum(1 for mask in range(2 ** m)
              if abs(sum(ranks[i] for i in range(m) if mask >> i & 1) - mean) >= abs(t_obs - mean) - 1e-9)
    return {"n_nonzero": m, "zeros_dropped": len(diffs) - m, "t_plus": t_obs, "p_two_sided": hit / 2 ** m}


def sign_test_exact(diffs: list) -> dict:
    nz = [x for x in diffs if x != 0]
    m, k = len(nz), sum(x > 0 for x in nz)
    if m == 0:
        return {"n_nonzero": 0, "positive": 0, "p_two_sided": None}
    return {"n_nonzero": m, "positive": k, "p_two_sided": float(binomtest(k, m).pvalue)}


def hodges_lehmann(diffs: list):
    w = [(diffs[i] + diffs[j]) / 2 for i in range(len(diffs)) for j in range(i, len(diffs))]
    return statistics.median(w) if w else None


def describe(pairs: list, final: bool) -> dict:
    ds = [p["D_sec"] for p in pairs]
    n = len(ds)
    out = {"n_valid_pairs": n, "direction_D_positive_means": "B의 공식 SLO 누적 위반시간이 A보다 적음 (D = A − B)",
           "pairs": [{k: p[k] for k in ("pair", "A_sec", "B_sec", "D_sec", "lower_bound")} for p in pairs],
           "D_median": statistics.median(ds) if ds else None, "D_range": [min(ds), max(ds)] if ds else None,
           "B_less": sum(d > 0 for d in ds), "equal": sum(d == 0 for d in ds), "B_more": sum(d < 0 for d in ds)}
    if n < 5:
        out["stage"] = "descriptive_only_n_lt_5"
    elif not final:
        out.update(stage="stage1_descriptive", sign_test_reference=sign_test_exact(ds), note="1단계 기술 통계 - 유의성 주장 아님")
    else:
        out.update(stage="final", wilcoxon=wilcoxon_exact(ds), hodges_lehmann=hodges_lehmann(ds), alpha=0.05)
    return out


# ---------------- 전체 ----------------
CORRECTION_FIELDS = ("original_verdict_path", "original_verdict_sha256", "error_type", "contract_basis",
                     "corrected_interpretation", "applies_to")


def load_corrections(results_dir: Path) -> dict:
    acc, rej = [], []
    folder = results_dir / "corrections"
    for f in sorted(folder.glob("*.json")) if folder.exists() else []:
        try:
            c = _json(f)
            p = Path(c["original_verdict_path"])
            p = p if p.is_absolute() else results_dir / p
            ok = all(c.get(k) for k in CORRECTION_FIELDS) and c["applies_to"] == "all_runs" and p.exists() \
                and _sha(p) == c["original_verdict_sha256"]
        except Exception as e:  # noqa: BLE001 - 형식 오류 기록은 받아들이지 않는다
            ok, c = False, {"error": f"{type(e).__name__}: {e}"}
        (acc if ok else rej).append({"file": f.name, "sha256": _sha(f), "record": {k: c.get(k) for k in CORRECTION_FIELDS} if ok else c})
    return {"accepted": acc, "rejected": rej,
            "use": "기록 연결만 - 판정은 분석기 코드의 결정적 수정(예: norm_generation)으로 모든 실행에 동일 적용, 원본 판정 파일은 읽기만"}


def analyze(results_dir: Path, probe_dir: Path, frozen: dict = None) -> dict:
    frozen = frozen or frozen_values()
    runs = sorted(r for r in gp.RUNS if r.startswith("ab") and (results_dir / f"summary-{gp.run_id_for(r)}.json").exists())
    per, excluded = {}, []
    for run in runs:
        d = read_run(results_dir, probe_dir, run)
        why = exclusion_reasons(run, d)
        if why:
            excluded.append({"run": run, "reasons": why,
                             "label": "development/rehearsal excluded" if run[:4] in gp.AB_REHEARSAL_PAIRS else "excluded"})
            continue
        csv_path = Path(d["files"]["probe_csv"]["path"])
        prim = primary(d, csv_path)
        per[run] = {"files": d["files"], "primary": prim, "secondary": secondary(d, csv_path),
                    "validity": run_validity(d, frozen, prim), "_d": d}
    pairs, pair_status = [], {}
    for p in sorted({r[:4] for r in runs if OFFICIAL_RE.fullmatch(r) and r[:4] not in gp.AB_REHEARSAL_PAIRS}):
        a, b = per.get(p + "a"), per.get(p + "b")
        if not a or not b:
            pair_status[p] = {"status": "incomplete", "present_included": [x for x in (p + "a", p + "b") if x in per]}
            continue
        ca, cb = a["_d"].get("contract") or {}, b["_d"].get("contract") or {}
        sa, sb = a["_d"].get("summary") or {}, b["_d"].get("summary") or {}
        why = [f"run_invalid:{x}" for x in (p + "a", p + "b") if not per[x]["validity"]["valid"]]
        for k in ("contract_doc", "threshold", "artifact_hashes", "conditions", "observation_sec"):
            if ca.get(k) != cb.get(k):
                why.append(f"pair_mismatch:{k}")
        if not sa.get("start_active_node") or sb.get("start_active_node") != sa.get("start_active_node") \
                or (sb.get("comparison") or {}).get("expected_start_active_node") != sa.get("start_active_node"):
            why.append(f"pair_start_node_mismatch: A={sa.get('start_active_node')} B={sb.get('start_active_node')}")
        if b["validity"]["pre_injection_promotion"]:
            why.append("pre_injection_promotion(계약 r1 §3 예외 - 비교 제외, 따로 집계)")
        if why:
            pair_status[p] = {"status": "invalid", "reasons": why}
            continue
        A, B = a["primary"]["cumulative_sec"], b["primary"]["cumulative_sec"]
        sec = {k: (a["secondary"][k] - b["secondary"][k]) if isinstance(a["secondary"].get(k), (int, float))
               and isinstance(b["secondary"].get(k), (int, float)) else None
               for k in ("failure", "timeout", "p95", "p99", "slow_requests", "max_gap_sec")}
        pairs.append({"pair": p, "A_sec": A, "B_sec": B, "D_sec": round(A - B, 3),
                      "lower_bound": {"A": a["primary"]["lower_bound"], "B": b["primary"]["lower_bound"]},
                      "secondary_A_minus_B": sec})
        pair_status[p] = {"status": "valid"}
    attempts = sum(1 for r in runs if OFFICIAL_RE.fullmatch(r) and r.endswith("a") and r[:4] not in gp.AB_REHEARSAL_PAIRS)
    final = len(pairs) >= 10 or attempts >= 15
    sens = [p for p in pairs if not p["lower_bound"]["A"] and not p["lower_bound"]["B"]]
    result = {"rules_doc": RULES_DOC.name, "official_id_pattern": OFFICIAL_RE.pattern, "attempted_pairs": attempts, "final": final,
              "excluded_runs": excluded, "pair_status": pair_status,
              "runs": {k: {x: v[x] for x in ("files", "primary", "secondary", "validity")} for k, v in sorted(per.items())},
              "primary_analysis": describe(pairs, final), "sensitivity_without_lower_bound_pairs": describe(sens, final),
              "counts": {"pre_injection_promotion": sum(1 for v in per.values() if v["validity"]["pre_injection_promotion"]),
                         "b_mechanism_failure": sum(1 for v in per.values() if v["validity"]["b_mechanism_failure"])},
              "frozen_expectations": frozen, "corrections": load_corrections(results_dir)}
    digest = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()
    deps = {p.name: _sha(p) for p in (Path(__file__), Path(slo_judge.__file__), Path(slo_episodes.__file__), Path(verdict.__file__),
                                      Path(gcg.__file__), RULES_DOC, CONTRACT_R1, HERE.parent / gp.AB_CONTRACT_DOC)}
    return {"result": result, "result_digest": digest,
            "provenance": {"analyzed_at_utc": datetime.now(timezone.utc).isoformat(), "code_and_docs_sha256": deps,
                           "slo_version": slo_judge.SLO_VERSION, "slo_episodes_revision": slo_episodes.ANALYZER_REVISION}}


def main(argv=None) -> int:  # pragma: no cover - 파일 쓰기 진입점
    p = argparse.ArgumentParser(description="guarded A/B 공식 분석(새 파일에만 저장)")
    p.add_argument("--results-dir", type=Path, default=gp.AB_RESULTS_DIR)
    p.add_argument("--probe-dir", type=Path, default=gp.PROBE_RESULTS_DIR)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args(argv)
    out = analyze(a.results_dir, a.probe_dir)
    with open(a.out, "x", encoding="utf-8") as fh:
        fh.write(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    r = out["result"]["primary_analysis"]
    print(f"valid pairs={r['n_valid_pairs']} stage={r['stage']} excluded={len(out['result']['excluded_runs'])} "
          f"digest={out['result_digest'][:16]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
