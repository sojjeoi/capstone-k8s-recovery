#!/usr/bin/env python3
"""정상 운전 N1 분석기(새 파일, 2026-10-08) - 계약: docs/design/phase8-v33c1-guarded-normal-bc-n1-contract-20261008.md

판정은 모두 기계 규칙이다(사람이 세션 상태·신호 분류를 바꾸는 입력 없음). 동결 부품 재사용:
  - 공식 SLO 판정: slo_episodes(r2)·slo_judge v3를 c1_shadow_pilot_analysis.no_injection_requests로 호출(새 SLO 정의 없음)
  - 기록 완전성: c1_shadow_pilot_analysis.load_shadow·time_gaps(기록 공백 ≤ 30 s, 독립 검증 계약 §5 상속)
  - 실행 상태: detector_same_tick_replay._v1_common_reasons(정리·하네스 오류, probe 이미지, probe 공백)
  - 통계: guarded_ab_analysis.wilcoxon_exact·sign_test_exact·hodges_lehmann
창: t0 = 첫 scored 평가 완료 시각. 판정 창 [t0, t0+3600) - 이 안의 신호만 primary·secondary에 쓴다. 신호 뒤 추적 W = 600 s
(관찰 4200 s의 마지막 600 s는 추적 전용 - 그 안에서 새로 난 신호는 집계하지 않는다).
v1.1(2026-10-08, amendment phase8-v33c1-guarded-normal-bc-n1-amendment-v1.1-20261008.md): 신호 귀속용 인프라 증거 창을
[max(t0, t−60 s), t+600 s]로 자른다 - t0 전 증거가 이른 신호만 indeterminate로 만드는 비대칭 차단(grace 아님, 판정 버림 없음)."""
import argparse
import json
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

import c1_shadow_pilot_analysis as cspa
import detector_same_tick_replay as dstr
import guarded_ab_analysis as ab
import guarded_live_detector_runner as gr
import run_guarded_normal_bc as rn
import slo_judge

DECISION_SEC, FOLLOW_UP_SEC = rn.DECISION_SEC, rn.FOLLOW_UP_SEC
INFRA_LOOKBACK_SEC = 60.0          # 탐지기 입력 창(c1 WINDOW_SEC) - 신호를 만든 입력 구간
COVERAGE_TOL_SEC = dstr.COVERAGE_TOLERANCE_SEC  # 30 s
STAGE_TARGET = 6
OFFICIAL_RE = re.compile(r"nb0[1-9]")
CLASSES = ("unnecessary", "true_warning_candidate", "during_degradation", "indeterminate")
ANALYZER_REVISION = "n1-v1.1"
KNOWN_LIMITATIONS = ["summary.evidence_counts.condition_met(동결 evidence_counts)는 최상위 condition_met만 세므로 N1 섀도 기록에서 항상 0 - 실제 B·C·K 조건 수가 아니며 근거로 쓰지 않는다. 공식 판정·보고는 이 분석기가 rules에서 직접 센 값과 원자료 기준."]


def _ts(x):
    if not x:
        return None
    t = x if isinstance(x, datetime) else datetime.fromisoformat(str(x).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _rel(t, t0):
    return round((t - t0).total_seconds(), 3)


# ---------------- 신호 분류 (순수 함수) ----------------
def classify_signal(t, *, t0, obs_end, episodes, service_events, infra_events, w=FOLLOW_UP_SEC):
    """episodes = [(시작, 끝)] 공식 SLO 위반 구간, service_events·infra_events = [시각]. 반환 (분류, 근거).
    true_warning_candidate는 시간 선행 관계만 뜻한다(예측 성공 확정 아님). 인프라 증거 창 시작은 t0에서 자른다(v1.1)."""
    for a, b in episodes:
        if a <= t <= b:
            return "during_degradation", "official_slo_episode_ongoing"
    end = t + timedelta(seconds=w)
    if obs_end is None or obs_end < end:
        return "indeterminate", "follow_up_incomplete"
    if any(max(t0, t - timedelta(seconds=INFRA_LOOKBACK_SEC)) <= x <= end for x in infra_events):
        return "indeterminate", "infra_abnormality_in_window"
    if any(t < a <= end for a, _ in episodes):
        return "true_warning_candidate", "official_slo_episode_started_in_follow_up"
    if any(t < x <= end for x in service_events):
        return "true_warning_candidate", "service_event_in_follow_up"
    return "unnecessary", "no_degradation_service_event_or_infra_in_follow_up"


def in_decision(t, t0, h=DECISION_SEC):
    return t0 <= t < t0 + timedelta(seconds=h)


def arm_outcome(signal_times, t0, *, obs_end, episodes, service_events, infra_events, h=DECISION_SEC, w=FOLLOW_UP_SEC):
    """판정 창 안 신호만 분류·집계. T_safe = min(첫 unnecessary − t0, h)."""
    sig = sorted(t for t in signal_times if in_decision(t, t0, h))
    classified = [{"t_rel_sec": _rel(t, t0), "class": c, "basis": why} for t in sig for c, why in [
        classify_signal(t, t0=t0, obs_end=obs_end, episodes=episodes, service_events=service_events, infra_events=infra_events, w=w)]]
    first_unn = next((x["t_rel_sec"] for x in classified if x["class"] == "unnecessary"), None)
    return {"signals": classified, "t_safe_sec": min(first_unn, h) if first_unn is not None else h,
            "unnecessary_any": first_unn is not None,
            "first_raw_trigger_sec": classified[0]["t_rel_sec"] if classified else None,
            "raw_trigger_count": len(classified), "raw_trigger_per_hour": round(len(classified) / (h / 3600.0), 3),
            "class_counts": {c: sum(x["class"] == c for x in classified) for c in CLASSES},
            "excluded_after_decision_window": sum(1 for t in signal_times if t >= t0 + timedelta(seconds=h))}


def timing_bins(classified):
    """진단 전용 - 5분 구간별 신호 수(판정 창 3600 s → 12칸)."""
    bins = [0] * int(DECISION_SEC // 300)
    for x in classified:
        bins[min(int(x["t_rel_sec"] // 300), len(bins) - 1)] += 1
    return bins


# ---------------- 세션 읽기 ----------------
def read_session(results_dir: Path, run: str) -> dict:
    rid = rn.run_id_for(run)
    files = {"summary": results_dir / f"summary-{rid}.json", "contract": results_dir / f"contract-{rid}.json",
             "evidence": gr.live_evidence_path(results_dir, rid), "pod_events": results_dir / f"pod-events-{rid}.json"}
    d = {"run": run, "run_id": rid, "missing": [k for k, p in files.items() if not p.exists()]}
    for k in ("summary", "contract", "pod_events"):
        if files[k].exists():
            d[k] = json.loads(files[k].read_text(encoding="utf-8-sig"))
    csv = (d.get("summary") or {}).get("probe_raw_csv")
    if csv:
        files["probe_csv"] = Path(csv)
    if not csv or not Path(csv).exists():
        d["missing"].append("probe_csv")
    d["files"] = {k: {"path": str(p), "sha256": ab._sha(p) if p.exists() else None} for k, p in sorted(files.items())}
    d["evidence_path"] = files["evidence"]
    return d


def _probe_rows(path):
    rows = slo_judge.load_raw(Path(path))
    for r in rows:
        r["sent_at"] = _ts(r["sent_at"])
    return rows


def analyze_session(d: dict) -> dict:
    """세션 상태(invalid / unexpected_degradation / normal)와 B·C(·K) 결과. 결과(누가 먼저 발화)는 상태 판정에 쓰지 않는다."""
    s, c, run = d.get("summary") or {}, d.get("contract") or {}, d["run"]
    out = {"run": run, "run_id": d["run_id"], "files": d["files"], "status": None, "invalid_reasons": []}
    inv = out["invalid_reasons"]
    inv += [f"missing:{m}" for m in d["missing"]]
    if inv:
        out["status"] = "invalid"
        return out
    evals, integrity = cspa.load_shadow(d["evidence_path"], d["run_id"])
    inv += integrity
    if evals and all(r.get("eval_started_at_utc") for r in evals):
        inv += cspa.time_gaps(evals)["reasons"]
    inv += dstr._v1_common_reasons("no_injection", {**s, "probe_data_gap_detected": False})  # 공백은 아래에서 상태별로
    scored = [r for r in evals if r.get("status") == "scored"]
    t0 = _ts(scored[0]["score_completed_at_utc"]) if scored else None
    obs_start, obs_end = _ts(s.get("observation_started_at_utc")), _ts(s.get("observation_ended_at_utc"))
    if not t0 or not obs_start or not obs_end or obs_end <= t0:
        inv.append(f"window_missing: t0={t0} start={obs_start} end={obs_end}")
    if not s.get("detector_started"):
        inv.append("detector_not_started")
    if c.get("plan_id") != rn.PLAN_ID or c.get("contract_doc") != rn.CONTRACT_DOC:
        inv.append(f"contract_plan_or_doc_mismatch: {c.get('plan_id')}")
    if inv:
        out["status"] = "invalid"
        return out

    rows = _probe_rows(d["files"]["probe_csv"]["path"])
    req = cspa.no_injection_requests(rows, t0, obs_end, min_requests=cspa.min_requests_for((obs_end - t0).total_seconds()))
    episodes = [(_ts(e["t_slo"]), _ts(e["t_recovery"]) or obs_end) for e in (req.get("slo_episodes_main") or [])]
    status = s.get("outcome_status")
    if status == "aborted":
        if not episodes:  # SLO 위반 외 사유의 중단(인프라·탐지기·probe 등) = 무효
            inv.append(f"aborted_without_official_slo_degradation: {s.get('outcome_reason')}")
    elif status != "completed":
        inv.append(f"outcome_status={status}")
    else:
        if not req.get("computable"):
            inv += req.get("reasons") or [f"probe_not_computable: {req.get('reason')}"]
        if s.get("probe_data_gap_detected") is not False:
            inv.append(f"probe_data_gap_detected={s.get('probe_data_gap_detected')}")
        obs_sec = float(c.get("observation_sec") or 0)
        if (obs_end - obs_start).total_seconds() < obs_sec - COVERAGE_TOL_SEC:
            inv.append(f"observation_short: {(obs_end - obs_start).total_seconds():.0f}s < {obs_sec}")
        last_eval = _ts(evals[-1].get("eval_started_at_utc"))
        if last_eval < obs_end - timedelta(seconds=COVERAGE_TOL_SEC):
            inv.append(f"evaluation_coverage_end: last={last_eval.isoformat()}")
    if inv:
        out["status"] = "invalid"
        return out

    service_events = [r["sent_at"] for r in rows if t0 <= r["sent_at"] <= obs_end and not r["success"]]
    infra = [(_ts(r.get("eval_started_at_utc")), f"input_skip:{r.get('skip_reason')}") for r in evals
             if r.get("status") != "scored" and _ts(r.get("eval_started_at_utc"))]  # t0 전 skip도 진단 보존(분류는 t0에서 자름)
    infra += [(_ts(e.get("last_timestamp") or e.get("first_timestamp")), f"k8s_warning:{e.get('reason')}")
              for e in (d.get("pod_events") or []) if e.get("type") == "Warning"
              and _ts(e.get("last_timestamp") or e.get("first_timestamp"))]
    kw = dict(obs_end=obs_end, episodes=episodes, service_events=service_events, infra_events=[t for t, _ in infra])
    arms = {a: arm_outcome([_ts(r["rules"][a]["condition_met_at_utc"]) for r in scored if r["rules"][a]["condition_met"]],
                           t0, **kw) for a in ("B", "C", "K")}
    for a in arms:
        arms[a]["timing_bins_5min"] = timing_bins(arms[a]["signals"])
    snap = scored[0].get("pod_snapshot") or {}
    pods = {p.get("hash"): p for p in snap.get("pods") or [] if p.get("hash")}
    act, prev = pods.get(snap.get("active_selector")) or {}, pods.get(snap.get("preview_selector")) or {}
    preview_ready = _ts(prev.get("vllm_started_at"))
    out.update(
        status="unexpected_degradation" if episodes else "normal",
        t0_utc=t0.isoformat(), observation_end_utc=obs_end.isoformat(), observed_after_t0_sec=_rel(obs_end, t0),
        nodes={"start_active_node": s.get("start_active_node"), "active_pod": act.get("name"), "active_uid": act.get("uid"),
               "active_node": act.get("node"), "preview_pod": prev.get("name"), "preview_uid": prev.get("uid"),
               "preview_node": prev.get("node")},
        preview_ready_rel_t0_sec=_rel(preview_ready, t0) if preview_ready else None,
        input_grid={**cspa.phase_counts(evals), "time_gaps": cspa.time_gaps(evals)["gaps"]},
        slo={"episodes": [{"t_slo_rel_sec": _rel(a, t0), "end_rel_sec": _rel(b, t0)} for a, b in episodes],
             "requests_main": req.get("requests_main_observation"), "max_gap_sec": req.get("max_gap_main_sec"),
             "computable": req.get("computable"), "reason": req.get("reason")},
        service_events=[_rel(t, t0) for t in service_events],
        infra_events=[{"t_rel_sec": _rel(t, t0), "what": why, "pre_t0_diagnostic_only": t < t0} for t, why in infra],
        arms=arms)
    return out


# ---------------- 통계 ----------------
def paired_stats(sessions: list) -> dict:
    """normal 세션의 짝 T_safe. D = B − C(양수 = B가 불필요 신호 없이 더 오래 유지)."""
    pairs = [{"run": x["run"], "t_safe_B": x["arms"]["B"]["t_safe_sec"], "t_safe_C": x["arms"]["C"]["t_safe_sec"],
              "D_sec": round(x["arms"]["B"]["t_safe_sec"] - x["arms"]["C"]["t_safe_sec"], 3)} for x in sessions]
    d = [p["D_sec"] for p in pairs]
    yes = [int(x["arms"]["C"]["unnecessary_any"]) - int(x["arms"]["B"]["unnecessary_any"]) for x in sessions]  # 양수 = B 유리
    out = {"n_normal_sessions": len(pairs), "pairs": pairs, "D": "T_safe(B) − T_safe(C), 초",
           "D_mean": round(statistics.mean(d), 3) if d else None, "D_median": statistics.median(d) if d else None,
           "D_range": [min(d), max(d)] if d else None, "B_longer": sum(x > 0 for x in d), "tie": sum(x == 0 for x in d),
           "C_longer": sum(x < 0 for x in d),
           "secondary_yes_no": {"B_only": sum(x < 0 for x in yes), "C_only": sum(x > 0 for x in yes),
                                "both": sum(bool(x["arms"]["B"]["unnecessary_any"] and x["arms"]["C"]["unnecessary_any"])
                                            for x in sessions),
                                "neither": sum(not x["arms"]["B"]["unnecessary_any"] and not x["arms"]["C"]["unnecessary_any"]
                                               for x in sessions)},
           "small_n_note": "0이 아닌 쌍 m개의 양측 최소 p = 2/2^m - m ≤ 5면 0.0625 이상(α 0.05 도달 불가). 동률 있으면 방향·효과크기만"}
    if len(pairs) < STAGE_TARGET:
        out["stage"] = "descriptive_only_n_lt_6"
        return out
    out.update(stage="n1_planned_analysis", wilcoxon_exact=ab.wilcoxon_exact(d), hodges_lehmann_D=ab.hodges_lehmann(d),
               mcnemar_exact_reference=ab.sign_test_exact(yes),
               note="사전 등록 분석(정확 Wilcoxon 양측 α 0.05 + HL). 일반화·비율 추정 금지")
    return out


def stop_rules(sessions: list) -> dict:
    st = [x["status"] for x in sessions]
    return {"invalid_two_in_a_row": any(a == b == "invalid" for a, b in zip(st, st[1:])),
            "unexpected_degradation_ge_2": st.count("unexpected_degradation") >= 2,
            "cleanup_error_any": any(any("cleanup_errors" in r for r in x["invalid_reasons"]) for x in sessions),
            "note": "보고만 한다(자동 중단 아님) - 사람 확인"}


def analyze(results_dir: Path) -> dict:
    prefix = f"summary-guarded_normal_{rn.MODE_RO}-"
    names = sorted({p.name[len(prefix):].split("-")[0] for p in results_dir.glob(f"{prefix}*.json")})
    sessions, excluded = [], []
    for run in names:
        if run not in rn.RUNS:
            excluded.append({"run": run, "reason": "unknown_id"})
            continue
        res = analyze_session(read_session(results_dir, run))
        if not OFFICIAL_RE.fullmatch(run):
            excluded.append({"run": run, "reason": "pilot_counted_false", "session": res})
            continue
        sessions.append(res)
    normal = [x for x in sessions if x["status"] == "normal"]
    return {"analyzer": "guarded_normal_bc_analysis", "analyzer_revision": ANALYZER_REVISION,
            "known_limitations": KNOWN_LIMITATIONS, "contract_doc": rn.CONTRACT_DOC, "plan_id": rn.PLAN_ID,
            "windows": {"decision_sec": DECISION_SEC, "follow_up_sec": FOLLOW_UP_SEC, "grace_sec": 0,
                        "infra_lookback_sec": INFRA_LOOKBACK_SEC, "infra_window": "[max(t0, t-60s), t+600s]"},
            "attempted": len(sessions), "max_attempts": len(rn.OFFICIAL), "target_normal": STAGE_TARGET,
            "status_counts": {k: sum(x["status"] == k for x in sessions) for k in ("normal", "unexpected_degradation", "invalid")},
            "stop_rules": stop_rules(sessions), "sessions": sessions, "excluded": excluded,
            "primary": paired_stats(normal)}


def main(argv=None) -> int:  # pragma: no cover - 파일 쓰기 진입점
    p = argparse.ArgumentParser(description="정상 운전 N1 분석(동결 규칙, 결과 파일은 새로만 씀)")
    p.add_argument("--results-dir", default=str(rn.RESULTS_DIR))
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    res = analyze(Path(a.results_dir))
    with open(a.out, "x", encoding="utf-8") as fh:
        fh.write(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    print(f"stage={res['primary']['stage']} attempted={res['attempted']} counts={res['status_counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
