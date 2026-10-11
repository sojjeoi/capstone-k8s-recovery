#!/usr/bin/env python3
"""B/C/K 공식 분석기(새 파일, 2026-10-07) - 계약 docs/design/phase8-v33c1-guarded-bck-comparison-contract-draft-20261007.md §9·§12.

원자료는 읽기만 하고 결과는 새 파일로만 쓴다(덮어쓰기 없음). 같은 원자료 → 같은 result_digest.
재사용(동결 guarded_ab_analysis, 수정 없음): primary(공식 누적 SLO 위반 초, slo_episodes) · secondary · stress(raw-counter-v2.3) ·
b_outcome(승격·서비스·D6 근거) · wilcoxon_exact · sign_test_exact · hodges_lehmann · load_corrections.
새로 구현: 실행 ID·계약 메타데이터 포함 규칙, arm별 run 유효성(B 분기 규칙을 세 arm에 같게 적용 + 세 규칙 기록 확인),
같은 block 안 쌍 구성: primary D = C − B, secondary D = K − B (양수 = IF 유리), 탐색 K − C.
D=0 쌍은 검정에서 제외하고 수를 보고. 확인 검정은 primary 하나(최종 단계), secondary는 같은 계산을 보조로만 보고."""
import argparse
import hashlib
import json
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import guarded_ab_analysis as ab
import run_guarded_bck as gb
import slo_episodes
import slo_judge

HERE = Path(__file__).parent
OFFICIAL_RE = re.compile(r"bc(0[1-9]|1[0-8])[bck]")
STAGE1_VALID, STAGE1_MAX_ATTEMPTS, FINAL_VALID, FINAL_MAX_ATTEMPTS = 6, 9, 12, 18  # 계약 §11(사용자 결정 D1)
COMPARISONS = {"primary": ("C", "B"), "secondary": ("K", "B"), "exploratory": ("K", "C")}  # D = 앞 − 뒤
SHARED_CONDITIONS = ("stress_stages", "probe_config_sha256", "probe_image", "probe_image_id", "detector_rule",
                     "start_state_rule", "rules", "rule_spec_sha256", "if_artifact_hashes", "if_threshold")


def frozen_values() -> dict:
    f = ab.frozen_values()
    spec = gb.bd.load_spec()
    return {**{k: f[k] for k in ("artifact_hashes", "observation_sec", "stress_stages", "probe_config_sha256", "probe_image",
                                 "probe_image_id", "detector_rule", "start_state_rule")},
            "if_threshold": ab.FROZEN_THRESHOLD, "thresholds": dict(gb.bd.THRESHOLDS), "rule_spec_sha256": spec["sha256"],
            "identity": {a: gb.bd.signal_identity(a, f["artifact_hashes"], spec["sha256"]) for a in gb.bd.ARMS}}


def _evidence(path) -> list:
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").split("\n") if x.strip()]


def read_run(results_dir: Path, probe_dir: Path, run: str) -> dict:
    rid = gb.run_id_for(run)
    files = {"summary": results_dir / f"summary-{rid}.json", "contract": results_dir / f"contract-{rid}.json",
             "evidence": gb.gr.live_evidence_path(results_dir, rid),
             "probe_csv": probe_dir / f"probe-{rid}-{gb.rt.ARM_LABEL}-{gb.RUNS[run][2]}-raw.csv"}
    d = {"run": run, "run_id": rid, "missing": []}
    for k in ("summary", "contract"):
        if files[k].exists():
            d[k] = ab._json(files[k])
        else:
            d["missing"].append(k)
    pf = (d.get("summary") or {}).get("preflight_file")
    if pf:
        files["preflight"] = results_dir / pf
    if pf and files["preflight"].exists():
        d["preflight"] = ab._json(files["preflight"])
    else:
        d["missing"].append("run_preflight")
    for k in ("probe_csv", "evidence"):
        if not files[k].exists():
            d["missing"].append(k)
    d["files"] = {k: {"path": str(p), "sha256": ab._sha(p) if p.exists() else None} for k, p in sorted(files.items())}
    return d


def exclusion_reasons(run: str, d: dict) -> list:
    if not OFFICIAL_RE.fullmatch(run):
        return ["not_official_id"]
    s, c, arm = d.get("summary") or {}, d.get("contract") or {}, run[-1].upper()
    r = []
    if c.get("plan_id") != gb.BCK_PLAN_ID or c.get("contract_doc") != gb.BCK_CONTRACT_DOC:
        r.append(f"contract_plan_or_doc_mismatch: {c.get('plan_id')} {c.get('contract_doc')}")
    for name, comp in (("contract", c.get("comparison")), ("summary", s.get("comparison"))):
        if not isinstance(comp, dict) or comp.get("counted_in_comparison") is not True or comp.get("block") != run[:4] \
                or comp.get("arm") != arm:
            r.append(f"{name}_comparison_not_counted_or_block_arm_mismatch: {comp}")
    if s.get("arm") != arm or c.get("arm") != arm or s.get("mode") != gb.MODE_ACT or c.get("mode") != gb.MODE_ACT:
        r.append(f"analysis_unknown_arm: id={arm} summary={s.get('arm')}/{s.get('mode')} contract={c.get('arm')}/{c.get('mode')}")
    return r


def rule_timeline(recs: list, t_inj) -> dict:
    """세 규칙의 같은 tick 판정 시각(기록 전용 보조 지표). 주입 전 발화 = false-positive evidence."""
    out = {a: {"first_condition_met_utc": None, "first_after_injection_utc": None, "pre_injection_condition_met": 0}
           for a in gb.bd.ARMS}
    for r in recs:
        if r.get("record_type") != "c1_evaluation":
            continue
        for a, j in (r.get("rules") or {}).items():
            t = ab._ts(j.get("condition_met_at_utc")) if j.get("condition_met") else None
            if a not in out or t is None:
                continue
            o = out[a]
            o["first_condition_met_utc"] = o["first_condition_met_utc"] or t.isoformat()
            if t_inj and t < t_inj:
                o["pre_injection_condition_met"] += 1
            elif t_inj and not o["first_after_injection_utc"]:
                o["first_after_injection_utc"] = t.isoformat()
    return out


def _sec(a, b):
    a, b = ab._ts(a), ab._ts(b)
    return round((b - a).total_seconds(), 3) if a and b else None


def run_validity(d: dict, frozen: dict, prim: dict) -> dict:
    """동결 분석기 B 분기 규칙(V1-V7)을 세 arm에 똑같이 적용 + V8 세 규칙 공통 tick 기록 확인 + arm 동결 identity."""
    s, c, pre, arm = d.get("summary") or {}, d.get("contract") or {}, d.get("preflight") or {}, d["run"][-1].upper()
    r, notes = [f"missing:{m}" for m in d["missing"]], {}
    o = s.get("trial_outcome") or {}
    if s.get("outcome_status") != "completed" or o.get("injection_valid") is not True:
        r.append(f"V1_not_completed: {s.get('outcome_status')} injection_valid={o.get('injection_valid')}")
    if not (s.get("detector_readiness") or {}).get("ready_at_utc"):
        r.append("V1_detector_not_ready")
    if not prim["computable"]:
        r.append(f"V2_primary_not_computable: {prim['reason']}")
    bo = ab.b_outcome(s)
    t_switch = bo["switched_at"] if bo["promoted"] else None
    st = ab.stress(d, t_switch) if s else {"verified": None, "reasons": ["no_summary"]}
    notes["stress"] = {k: st.get(k) for k in ("verified", "reasons", "criterion")}
    if st["verified"] is not True:
        r.append(f"V3_stress_not_verified: {st['verified']} {st['reasons']}")
    cond = c.get("conditions") or {}
    exp = {**{k: frozen[k] for k in ("stress_stages", "probe_config_sha256", "probe_image", "probe_image_id", "detector_rule",
                                     "start_state_rule", "rule_spec_sha256", "if_threshold")},
           "if_artifact_hashes": frozen["artifact_hashes"]}
    for k, v in exp.items():
        if cond.get(k) != v:
            r.append(f"V4_condition_mismatch:{k}")
    if {a: ((cond.get("rules") or {}).get(a) or {}).get("threshold") for a in gb.bd.ARMS} != frozen["thresholds"]:
        r.append("V4_rule_thresholds_mismatch")
    if c.get("threshold") != frozen["thresholds"][arm] or c.get("signal_identity") != frozen["identity"][arm] \
            or c.get("artifact_hashes") != frozen["artifact_hashes"] or c.get("observation_sec") != frozen["observation_sec"]:
        r.append("V4_arm_identity_threshold_or_observation_mismatch")
    ctx = s.get("context") or {}
    if {k: ctx.get(k) for k in frozen["identity"][arm]} != frozen["identity"][arm]:
        r.append("V4_policy_context_identity_mismatch")
    if (s.get("probe_image_runtime_check") or {}).get("verified") is not True:
        r.append("V4_probe_image_not_verified")
    iso = s.get("old_policy_isolation") or {}
    if (iso.get("start") or {}).get("isolated") is not True or (iso.get("end") or {}).get("isolated") is not True:
        r.append("V5_old_policy_not_isolated")
    start_active = ((pre.get("canonical_start") or {}).get("state") or {}).get("active")
    fa = s.get("final_promotion_assessment") or {}
    end_active = ((fa.get("sources") or {}).get("live") or {}).get("active_selector")
    new = (fa.get("target") or {}).get("new")
    if end_active != start_active and not (bo["promoted"] and bo["action_status"] == "verified" and new == end_active):
        r.append(f"V5_unexplained_selector_change: {start_active}->{end_active}")
    if end_active == start_active and bo["promoted"]:
        r.append("V5_promoted_but_selector_unchanged")
    if pre.get("ok") is not True or (pre.get("canonical_start") or {}).get("problems") != []:
        r.append("V6_start_not_canonical")
    errs = s.get("cleanup_errors") or []
    obs_errs = [e for e in errs if e.startswith(ab.OBSERVATION_ERRORS)]
    if s.get("harness_error") or obs_errs:
        r.append(f"V7_observation_problem: {s.get('harness_error')} {obs_errs}")
    recs = _evidence(d["files"]["evidence"]["path"]) if d["files"]["evidence"]["sha256"] else []
    starts = [x for x in recs if x.get("record_type") == "c1_live_start"]
    scored = [x for x in recs if x.get("record_type") == "c1_evaluation" and x.get("status") == "scored"]
    if len(starts) != 1 or starts[0].get("actuating_rule") != arm or starts[0].get("rule_spec_sha256") != frozen["rule_spec_sha256"]:
        r.append("V8_detector_start_arm_or_spec_mismatch")
    if not scored or any(x.get("actuating_rule") != arm or set(x.get("rules") or {}) != set(gb.bd.ARMS) for x in scored):
        r.append("V8_three_rule_common_tick_evidence_missing")
    post = [e for e in errs if e not in obs_errs]
    t_inj = ab._window(d)[0]
    pre_inj = bool(bo["promoted"] and t_inj and ab._ts(bo["promote_called_at"]) and ab._ts(bo["promote_called_at"]) < t_inj)
    mech = bool(((s.get("watcher") or {}).get("action") or {}).get("outcome") in ab.B_MECH_OUTCOMES
                or (bo["promoted"] and not bo["service_verified"]) or bo["d6"].get("held"))
    tl = rule_timeline(recs, t_inj)
    trig = tl[arm]["first_after_injection_utc"]
    timing = {"injection_to_trigger_sec": _sec(t_inj.isoformat() if t_inj else None, trig),
              "trigger_to_promotion_sec": _sec(trig, bo["promote_called_at"]) if bo["promoted"] else None,
              "promotion_to_selector_sec": _sec(bo["promote_called_at"], bo["switched_at"]) if bo["promoted"] else None,
              "selector_to_service_verified_sec": _sec(bo["switched_at"], bo["service_finished_at"]) if bo["promoted"] else None}
    return {"valid": not r, "reasons": r, "notes": notes, "arm": arm, "action_outcome": bo, "pre_injection_promotion": pre_inj,
            "mechanism_failure": mech, "post_observation_cleanup_errors": post, "next_run_independence_risk": bool(post),
            "start_active_node": s.get("start_active_node"), "rule_timeline": tl, "timing": timing}


def describe(pairs: list, minuend: str, subtrahend: str, final: bool, role: str) -> dict:
    ds = [p["D_sec"] for p in pairs]
    n = len(ds)
    out = {"role": role, "n_valid_pairs": n, "D": f"{minuend} − {subtrahend} (초, 양수 = {subtrahend}의 공식 SLO 누적 위반시간이 적음)",
           "pairs": [{"block": p["block"], minuend: p["x_sec"], subtrahend: p["y_sec"], "D_sec": p["D_sec"],
                      "lower_bound": p["lower_bound"]} for p in pairs],
           "D_median": statistics.median(ds) if ds else None, "D_range": [min(ds), max(ds)] if ds else None,
           "positive": sum(d > 0 for d in ds), "zero": sum(d == 0 for d in ds), "negative": sum(d < 0 for d in ds)}
    if role == "exploratory":
        out["stage"] = "descriptive_only_exploratory"
    elif final:
        out.update(stage="final", wilcoxon=ab.wilcoxon_exact(ds), hodges_lehmann=ab.hodges_lehmann(ds), alpha=0.05,
                   confirmatory=role == "primary",
                   note=None if role == "primary" else "secondary - 효과·방향 보고, 확인적 유의성 주장 없음")
    elif n >= STAGE1_VALID:
        out.update(stage="stage1_descriptive", sign_test_reference=ab.sign_test_exact(ds), note="1단계 기술 통계 - 유의성 주장 아님")
    else:
        out["stage"] = f"descriptive_only_n_lt_{STAGE1_VALID}"
    return out


def analyze(results_dir: Path, probe_dir: Path, frozen: dict = None) -> dict:
    frozen = frozen or frozen_values()
    runs = sorted(r for r in gb.RUNS if (results_dir / f"summary-{gb.run_id_for(r)}.json").exists())
    per, excluded = {}, []
    for run in runs:
        d = read_run(results_dir, probe_dir, run)
        why = exclusion_reasons(run, d)
        if why:
            excluded.append({"run": run, "reasons": why,
                             "label": "pilot excluded" if run.startswith(f"bc{gb.PILOT_BLOCK}") else "excluded"})
            continue
        csv_path = Path(d["files"]["probe_csv"]["path"])
        prim = ab.primary(d, csv_path)
        per[run] = {"files": d["files"], "primary": prim, "secondary": ab.secondary(d, csv_path),
                    "validity": run_validity(d, frozen, prim), "_d": d}
    blocks = sorted({r[:4] for r in runs if OFFICIAL_RE.fullmatch(r)})
    pairs, status = {k: [] for k in COMPARISONS}, {}
    for blk in blocks:
        status[blk] = {}
        for name, (x, y) in COMPARISONS.items():
            px, py = per.get(blk + x.lower()), per.get(blk + y.lower())
            if not px or not py:
                status[blk][name] = {"status": "incomplete"}
                continue
            why = [f"run_invalid:{blk + a.lower()}" for a, p in ((x, px), (y, py)) if not p["validity"]["valid"]]
            why += [f"pre_injection_promotion:{blk + a.lower()}" for a, p in ((x, px), (y, py))
                    if p["validity"]["pre_injection_promotion"]]
            cx, cy = px["_d"].get("contract") or {}, py["_d"].get("contract") or {}
            why += [f"pair_mismatch:{k}" for k in ("contract_doc", "observation_sec", "artifact_hashes")
                    if cx.get(k) != cy.get(k)]
            why += [f"pair_condition_mismatch:{k}" for k in SHARED_CONDITIONS
                    if (cx.get("conditions") or {}).get(k) != (cy.get("conditions") or {}).get(k)]
            if why:
                status[blk][name] = {"status": "invalid", "reasons": why}
                continue
            X, Y = px["primary"]["cumulative_sec"], py["primary"]["cumulative_sec"]
            pairs[name].append({"block": blk, "x_sec": X, "y_sec": Y, "D_sec": round(X - Y, 3),
                                "lower_bound": {x: px["primary"]["lower_bound"], y: py["primary"]["lower_bound"]}})
            status[blk][name] = {"status": "valid"}
    attempted = len(blocks)
    final = len(pairs["primary"]) >= FINAL_VALID or attempted >= FINAL_MAX_ATTEMPTS
    analysis, sensitivity = {}, {}
    for name, (x, y) in COMPARISONS.items():
        analysis[name] = describe(pairs[name], x, y, final, name)
        sensitivity[name] = describe([p for p in pairs[name] if not any(p["lower_bound"].values())], x, y, final, name)
    pre_by_arm = {a: sum(1 for k, v in per.items() if k.endswith(a.lower()) and v["validity"]["pre_injection_promotion"])
                  for a in gb.bd.ARMS}
    mech = sum(1 for v in per.values() if v["validity"]["mechanism_failure"])
    first4 = blocks[:4]
    stop = {"first4_blocks_valid_primary_lt_2": len(first4) == 4 and sum(
                1 for p in pairs["primary"] if p["block"] in first4) < 2,
            "mechanism_failures_ge_2": mech >= 2, "same_arm_pre_injection_promotion_ge_2": any(v >= 2 for v in pre_by_arm.values()),
            "note": "보고만 한다(자동 중단 아님) - 사람 확인"}
    stage = "final" if final else ("stage1_reached" if len(pairs["primary"]) >= STAGE1_VALID else
                                   ("stage1_max_attempts_reached" if attempted >= STAGE1_MAX_ATTEMPTS else "collecting"))
    nodes = {a: sorted(str(v["validity"]["start_active_node"]) for k, v in per.items() if k.endswith(a.lower()))
             for a in gb.bd.ARMS}
    result = {"contract_doc": gb.BCK_CONTRACT_DOC, "official_id_pattern": OFFICIAL_RE.pattern, "attempted_blocks": attempted,
              "stage": stage, "final": final, "excluded_runs": excluded, "block_status": status,
              "runs": {k: {x: v[x] for x in ("files", "primary", "secondary", "validity")} for k, v in sorted(per.items())},
              "analysis": analysis, "sensitivity_without_lower_bound_pairs": sensitivity,
              "counts": {"pre_injection_promotion_by_arm": pre_by_arm, "mechanism_failure": mech,
                         "start_node_by_arm": nodes},
              "stop_rules": stop, "frozen_expectations": frozen, "corrections": ab.load_corrections(results_dir)}
    digest = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()
    deps = {p.name: ab._sha(p) for p in (Path(__file__), Path(ab.__file__), Path(gb.__file__), Path(gb.bd.__file__),
                                          gb.bd.RULE_SPEC, Path(slo_judge.__file__), Path(slo_episodes.__file__),
                                          HERE.parent / gb.BCK_CONTRACT_DOC)}
    return {"result": result, "result_digest": digest,
            "provenance": {"analyzed_at_utc": datetime.now(timezone.utc).isoformat(), "code_and_docs_sha256": deps,
                           "slo_version": slo_judge.SLO_VERSION, "slo_episodes_revision": slo_episodes.ANALYZER_REVISION}}


def main(argv=None) -> int:  # pragma: no cover - 파일 쓰기 진입점
    p = argparse.ArgumentParser(description="B/C/K 공식 분석(새 파일에만 저장)")
    p.add_argument("--results-dir", type=Path, default=gb.BCK_RESULTS_DIR)
    p.add_argument("--probe-dir", type=Path, default=gb.gp.PROBE_RESULTS_DIR)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args(argv)
    out = analyze(a.results_dir, a.probe_dir)
    with open(a.out, "x", encoding="utf-8") as fh:
        fh.write(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    r = out["result"]
    print(f"stage={r['stage']} attempted_blocks={r['attempted_blocks']} primary_n={r['analysis']['primary']['n_valid_pairs']} "
          f"excluded={len(r['excluded_runs'])} digest={out['result_digest'][:16]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
