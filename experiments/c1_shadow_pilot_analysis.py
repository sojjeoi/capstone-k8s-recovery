#!/usr/bin/env python3
"""c1 섀도 개발 pilot 오프라인 분석(2026-10-05) - 계약 초안 §4 측정 항목만 계산한다. 원자료만 읽고 새 파일에만 쓴다.

자료 충분성은 기존 검증 기준만 재사용한다(새 수치 기준 없음):
  - 평가 기록(detector_same_tick_replay·V1 계약 §5): 평가 번호 1부터·누락/중복/역행 없음, 평가 시각 역행 없음,
    평가 사이 공백 ≤ MAX_TICK_GAP_SEC(30초), 관찰 구간 앞·뒤 ±COVERAGE_TOLERANCE_SEC(30초)(coverage_reasons),
    실행 결과·정리·probe 이미지(_v1_common_reasons), 압박 전달·실행기 자격·주입 시각·probe 완전성(_v1_pressure_item2).
  - SLO(slo_episodes r2, V2 계약): 관찰 구간 요청 ≥ 570·공백 ≤ 30초, 관찰 종료까지 응답이 확인된 요청만 근거.
  - 무주입 probe: 실행기의 probe_data_gap_detected(_v1_common_reasons 9번).
섀도 기록 자체: 시작 기록·run_id 일치, 잘린 마지막 줄·깨진 줄은 조용히 버리지 않고 자료 결함으로 남긴다. 정렬하지 않는다.
원칙: 건너뛴 평가는 정상 판정으로 세지 않는다. 기록이 없는 시간 공백과 실제 건너뜀(status=skipped)을 따로 센다. 경고·SLO 근거는
관찰 종료까지만. 경고 시각은 판단 완료 시각(condition_met_at). 자료 결함·중단 실행은 insufficient_data / partial_run으로
표시하고 "경고 없음"·"SLO 위반 없음"으로 바꾸지 않는다. 동결 탐지기와 비교하지 않는다(평가 시각이 다름)."""
import argparse
import json
import statistics
from datetime import datetime
from pathlib import Path

import detector_same_tick_replay as dstr
import slo_episodes
import slo_judge

HERE = Path(__file__).resolve().parent
RESULTS_DIR = HERE / "results" / "capstone_c1_shadow_pilot"
ARTIFACTS = HERE.parent / "anomaly-detection" / "v3" / "model_v33_c1" / "artifacts"
CONTRACT_FLAT_CPU_MIN, CONTRACT_FLAT_SLOPE_MAX = 1.702, 0.0371  # 계약 §4(학습 cpu_mean 최댓값, cpu_slope 표준편차)
# 계약 §5.1(2026-10-05): 무주입 1800초·1RPS 요청 수 하한 = 기존 600초·570건과 같은 95% 원칙(1800×0.95). 공백 ≤ 30초와
# 관찰 종료까지 응답 확인 원칙은 slo_episodes(r2) 그대로.
NO_INJECTION_MIN_REQUESTS = 1710


def min_requests_for(observation_sec) -> int:
    """95% 원칙(600초·570건과 같음): 1800초 → 1710, 3600초 → 3420(독립 검증 계약 §5)."""
    return int(round(0.95 * float(observation_sec)))


def _ts(x):
    return datetime.fromisoformat(str(x).replace("Z", "+00:00")) if x else None


def _stats(v):
    return {"n": len(v), "mean": round(statistics.mean(v), 3), "max": round(max(v), 3)} if v else {"n": 0, "mean": None, "max": None}


def flat_high_cpu_bounds(artifacts: Path = ARTIFACTS) -> tuple:
    """학습 통계에서 읽고 계약 값과 맞는지 확인 - 다르면 중단(결과를 보고 기준을 바꾸지 않게)."""
    st = json.loads((artifacts / "feature-schema.json").read_text(encoding="utf-8"))["train_feature_stats"]
    cpu_max, slope_std = st["cpu_mean"]["max"], st["cpu_slope"]["std"]
    if round(cpu_max, 3) != CONTRACT_FLAT_CPU_MIN or round(slope_std, 4) != CONTRACT_FLAT_SLOPE_MAX:
        raise RuntimeError(f"fail-closed: 학습 통계({cpu_max}, {slope_std})가 계약 값과 다름")
    return cpu_max, slope_std


def load_shadow(path: Path, run_id: str) -> tuple:
    """(기록 순서 그대로의 c1_evaluation 목록, 기록 결함 사유). 정렬하지 않는다 - 순서 이상은 결함으로 남긴다."""
    if not path.exists():
        return [], ["3:shadow_evidence_missing"]
    parts = path.read_text(encoding="utf-8").split("\n")
    reasons, recs = [], []
    if parts[-1].strip():
        reasons.append(f"3:truncated_last_line(len={len(parts[-1])})")
    for i, line in enumerate(parts[:-1], 1):
        if not line.strip():
            continue
        try:
            recs.append(json.loads(line))
        except ValueError:
            reasons.append(f"3:malformed_line_{i}")
    starts = [r for r in recs if r.get("record_type") == "c1_shadow_start"]
    if len(starts) != 1:
        reasons.append(f"3:shadow_start_records={len(starts)}")
    if any(r.get("run_id") != run_id for r in recs):
        reasons.append("3:run_id_mismatch")
    evals = [r for r in recs if r.get("record_type") == "c1_evaluation"]
    if not evals:
        return evals, reasons + ["3:no_ticks_or_missing_time"]
    seqs = [r.get("evaluation_seq") for r in evals]
    if any(not isinstance(q, int) or isinstance(q, bool) for q in seqs):
        return evals, reasons + ["3:evaluation_seq_missing"]
    if seqs[0] != 1:
        reasons.append(f"3:evaluation_seq_starts_at_{seqs[0]}")
    if any(b != a + 1 for a, b in zip(seqs, seqs[1:])):
        reasons.append("3:evaluation_seq_gap_duplicate_or_reversal")
    times = [_ts(r.get("eval_started_at_utc")) for r in evals]
    if any(t is None for t in times):
        return evals, reasons + ["3:no_ticks_or_missing_time"]
    if any((b - a).total_seconds() <= 0 for a, b in zip(times, times[1:])):
        reasons.append("3:non_increasing_tick_time")
    return evals, reasons


def time_gaps(evals: list) -> dict:
    """기록이 없는 시간 공백(평가 사이 > MAX_TICK_GAP_SEC) - 실제 건너뜀(기록 있음, status=skipped)과 구분."""
    t = [_ts(r["eval_started_at_utc"]) for r in evals]
    gaps = [{"after_seq": a["evaluation_seq"], "sec": round((tb - ta).total_seconds(), 1)}
            for a, ta, tb in zip(evals, t, t[1:]) if (tb - ta).total_seconds() > dstr.MAX_TICK_GAP_SEC]
    return {"max_gap_sec": dstr.MAX_TICK_GAP_SEC, "gaps": gaps,
            "reasons": [f"3:max_tick_gap={max(g['sec'] for g in gaps):.1f}s"] if gaps else []}


def phase_counts(evals: list) -> dict:
    sk = {}
    for r in evals:
        if r["status"] != "scored":
            k = (r.get("skip_reason") or "unknown").split(":")[0]
            sk[k] = sk.get(k, 0) + 1
    n, ok = len(evals), sum(r["status"] == "scored" for r in evals)
    return {"evaluations": n, "scored": ok, "skipped_records": n - ok, "skip_reasons": sk,
            "usable_ratio": round(ok / n, 3) if n else None,
            "anomalous_scored": sum(bool(r.get("is_anomalous")) for r in evals if r["status"] == "scored")}


def timing(evals: list) -> dict:
    t = [_ts(r["eval_started_at_utc"]) for r in evals]
    gaps = [(b - a).total_seconds() for a, b in zip(t, t[1:])]
    q = [(_ts(r["query_completed_at_utc"]) - _ts(r["window_end_utc"])).total_seconds()
         for r in evals if r.get("query_completed_at_utc")]
    s = [(_ts(r["score_completed_at_utc"]) - _ts(r["window_end_utc"])).total_seconds()
         for r in evals if r["status"] == "scored"]
    return {"eval_interval_sec": _stats(gaps), "query_latency_sec": _stats(q), "score_latency_sec": _stats(s)}


def flat_high_cpu(evals: list, cpu_min: float, slope_max: float) -> dict:
    rows = [r for r in evals if r["status"] == "scored" and r["features"]["cpu_mean"] > cpu_min
            and abs(r["features"]["cpu_slope"]) <= slope_max]
    an = sum(bool(r["is_anomalous"]) for r in rows)
    return {"definition": f"점수 계산된 평가 중 cpu_mean > {cpu_min:.4f} 그리고 |cpu_slope| <= {slope_max:.4f}",
            "n": len(rows), "anomalous": an, "anomalous_ratio": round(an / len(rows), 3) if rows else None,
            "status": "measured" if rows else "no_flat_high_cpu_evaluations"}


def _within(t, a, b):
    return t is not None and a is not None and b is not None and a <= t <= b


def _validity(kind: str, summary: dict, evals: list, integrity: list, probe_rows) -> list:
    ticks = [{"t": _ts(r.get("eval_started_at_utc"))} for r in evals]
    reasons = list(integrity)
    if all(t["t"] for t in ticks) and ticks:  # 시각을 읽을 수 있을 때만 기록 없는 공백 검사
        reasons += time_gaps(evals)["reasons"]
    if summary.get("outcome_status") != "completed":
        reasons.append(f"1:outcome_status={summary.get('outcome_status')}")
    if not summary.get("observation_ended_at_utc"):
        reasons.append("8:observation_end_missing")
    if kind == "no_injection" and not summary.get("observation_started_at_utc"):
        reasons.append("8:observation_start_missing")
    if ticks and all(t["t"] for t in ticks):
        reasons += dstr.coverage_reasons(kind, summary, ticks)
    if kind == "pressure":
        reasons += dstr._v1_pressure_item2(summary, probe_rows)
        if not probe_rows:
            reasons.append("7:probe_csv_missing")
    reasons += dstr._v1_common_reasons(kind, summary)
    return reasons


def _probe_rows(summary: dict):
    csv = summary.get("probe_raw_csv")
    return slo_judge.load_raw(Path(csv)) if csv and Path(csv).exists() else None


PILOT_NOTE = "개발 pilot 자료 - 동결 탐지기와 평가 시각이 달라 같은 시점 비교가 아니며, 선제 대응 성능 주장 아님"
PILOT_NO_INJECTION_NOTE = "무주입 경고 수는 보정 자료의 경고 0건(임계값 산정 결과)과 다른 새 측정"


def analyze_pressure(results_dir: Path, run_id: str, artifacts: Path = ARTIFACTS, note: str = PILOT_NOTE) -> dict:
    s = json.loads((results_dir / f"summary-{run_id}.json").read_text(encoding="utf-8"))
    evals, integrity = load_shadow(results_dir / f"c1-shadow-evidence-{run_id}.jsonl", run_id)
    rp = results_dir / f"shadow-readiness-{run_id}.json"
    ready = json.loads(rp.read_text(encoding="utf-8")) if rp.exists() else None
    t_ready, t_inj, t_end = _ts((ready or {}).get("ready_at_utc")), _ts(s.get("t_injection_observed_utc")), \
        _ts(s.get("observation_ended_at_utc"))
    stages = (s.get("stress_delivery_record") or {}).get("stages") or []
    t_stress_end = max((_ts(st["deleted_at"]) for st in stages if st.get("deleted_at")), default=None)
    probe_rows = _probe_rows(s)
    reasons = _validity("pressure", s, evals, integrity, probe_rows)
    if t_ready is None:
        reasons.append("2.1:shadow_readiness_record_missing")

    def started(r):
        return _ts(r.get("eval_started_at_utc"))
    readiness = [r for r in evals if t_ready is None or (started(r) and started(r) < t_ready)]
    pre_inj = [r for r in evals if t_ready and started(r) and started(r) >= t_ready and (t_inj is None or started(r) < t_inj)]
    main = [r for r in evals if _within(started(r), t_inj, t_end)]
    if not any(r["status"] == "scored" for r in main):  # 준비 구간 점수는 본 관찰 점수를 대신하지 않는다
        reasons.append("3:no_scored_evaluations_in_main_observation")
    sufficient = not reasons
    warn = [_ts(r["condition_met_at_utc"]) for r in evals if r.get("condition_met")]
    w_phase = {"before_injection": sum(1 for w in warn if t_inj and w < t_inj),
               "during_stress": sum(1 for w in warn if _within(w, t_inj, min(t_stress_end, t_end) if t_stress_end and t_end else None)),
               "after_stress_until_observation_end": sum(1 for w in warn if t_stress_end and t_end and t_stress_end < w <= t_end),
               "after_observation_end_excluded": sum(1 for w in warn if t_end and w > t_end)}
    cpu_min, slope_max = flat_high_cpu_bounds(artifacts)
    fw = first_warning_vs_t_slo(sufficient, reasons, probe_rows, warn, t_inj, t_end)
    return {"run_id": run_id, "kind": "pressure", "data_status": "complete" if sufficient else "insufficient_data",
            "statistics_scope": "complete_run" if sufficient else "partial_or_invalid_run_not_comparable",
            "insufficient_reasons": reasons, "readiness_record": ready,
            "t_injection_observed_utc": s.get("t_injection_observed_utc"), "stress_end_utc": t_stress_end,
            "observation_end_utc": s.get("observation_ended_at_utc"),
            "all": phase_counts(evals), "readiness_phase": phase_counts(readiness),
            "pre_injection_after_ready": phase_counts(pre_inj), "main_observation": phase_counts(main),
            "time_gaps_without_records": time_gaps(evals)["gaps"] if evals and all(started(r) for r in evals) else None,
            "timing_all": timing(evals) if evals and all(started(r) for r in evals) else None,
            "warnings_by_phase_judgement_time": w_phase,
            "first_warning_vs_t_slo": fw,
            "flat_high_cpu_main": flat_high_cpu(main, cpu_min, slope_max),
            "auxiliary_streaks": auxiliary_streaks(evals, t_inj, t_end),
            "warning_counts": warning_counts(evals, {
                "before_injection": (min((_ts(r["score_completed_at_utc"]) for r in evals if r.get("score_completed_at_utc")),
                                         default=None), t_inj),
                "during_stress": (t_inj, min(t_stress_end, t_end) if t_stress_end and t_end else None),
                "after_stress_until_observation_end": (t_stress_end, t_end)}),
            "if_vs_fixed_same_ticks": if_vs_fixed_same_ticks(evals, t_inj, t_end, _ts(fw.get("t_slo"))),
            "note": note}


def _brief(r, t_inj):
    t, ws = _ts(r.get("score_completed_at_utc")), _ts(r.get("window_start_utc"))
    return {"seq": r["evaluation_seq"], "score_completed_at_utc": r.get("score_completed_at_utc"),
            "window_start_utc": r.get("window_start_utc"), "window_end_utc": r.get("window_end_utc"),
            "judged_after_injection": bool(t and t_inj and t >= t_inj),
            "window_includes_pre_injection_data": bool(ws and t_inj and ws < t_inj)}


def auxiliary_streaks(evals: list, t_inj, t_end, n: int = 3) -> dict:
    """계약 §4.2 보조 분석(2026-10-05, c1sp02 실행 전 고정). 실제 경고(탐지기 상태·쿨다운)와 별개이며 탐지기 상태를 바꾸지 않는다.
    (1) 첫 실제 경고(주입~관찰 종료의 첫 condition_met)를 만든 연속 평가들: 기록 순서에서 그 평가로 끝나는
        consecutive_anomalous개 평가의 번호·판정 시각·주입 전후·창 시작/끝.
    (2) 점수 계산 완료 시각이 모두 주입 이후인 n(=3)개 연속 이상 평가(기록 순서 연속, 건너뜀·정상에서 끊김)의 첫 충족 시각
        (= 세 번째 평가의 점수 계산 완료 시각, 관찰 종료까지). 창 시작이 주입 전이면 그 사실도 함께 남긴다."""
    idx = next((i for i, r in enumerate(evals) if r.get("condition_met")
                and _within(_ts(r.get("condition_met_at_utc")), t_inj, t_end)), None)
    first = None
    if idx is not None:
        k = int(evals[idx].get("consecutive_anomalous") or 0)
        first = {"warning_judgement_utc": evals[idx].get("condition_met_at_utc"),
                 "streak": [_brief(r, t_inj) for r in evals[max(0, idx - k + 1):idx + 1]]}
    post, run = None, []
    for r in evals:
        t = _ts(r.get("score_completed_at_utc"))
        ok = r.get("status") == "scored" and r.get("is_anomalous") and t and t_inj and t >= t_inj and (t_end is None or t <= t_end)
        run = run + [r] if ok else []
        if len(run) >= n:
            post = {"met_at_utc": run[n - 1]["score_completed_at_utc"],
                    "met_rel_inj_sec": dstr._rel(_ts(run[n - 1]["score_completed_at_utc"]), t_inj),
                    "streak": [_brief(x, t_inj) for x in run[:n]],
                    "any_window_includes_pre_injection_data": any(_brief(x, t_inj)["window_includes_pre_injection_data"]
                                                                  for x in run[:n])}
            break
    return {"definition": "계약 §4.2 - 보조 시각, 실제 경고 전송 시각 아님", "first_actual_warning_streak": first,
            "first_all_post_injection_3_consecutive": post}


def warning_counts(evals: list, bounds: dict) -> dict:
    """구간별(판단 완료 시각 기준) 세 가지를 따로 센다:
    condition_satisfied_evaluations = 연속 이상 >= 3인 평가 수(쿨다운 무관),
    streaks_reaching_3 = 연속이 정확히 3에 도달한 평가 수(연속 구간 수),
    send_condition_met_cooldown_applied = 쿨다운(60초)을 적용한 IF 송신 조건 충족 건수(condition_met). sink 기록·송신 결과와
    대조하지 않았으므로 실제 송신·수신 성공 건수가 아니다."""
    out = {}
    for name, (a, b) in bounds.items():
        rs = [r for r in evals if r.get("status") == "scored" and _within(_ts(r.get("score_completed_at_utc")), a, b)]
        out[name] = {"condition_satisfied_evaluations": sum(int(r.get("consecutive_anomalous") or 0) >= 3 for r in rs),
                     "streaks_reaching_3": sum(int(r.get("consecutive_anomalous") or 0) == 3 for r in rs),
                     "send_condition_met_cooldown_applied": sum(bool(r.get("condition_met")) for r in rs)}
    return out


def first_condition(evals: list, pred, t_a, t_b, n: int = 3) -> dict:
    """계약 §7 공통 정의 - "첫 3연속 조건 충족 시각": 섀도 평가 기록 순서대로 점수 계산된 평가에 pred를 적용해 연속 횟수를 센다
    (건너뜀·pred 거짓에서 0으로, 주입 시점에 리셋하지 않음 = 실제 탐지기와 같음). 구간 [t_a, t_b] 안에서(점수 계산 완료 시각 기준)
    연속 >= n인 첫 평가의 시각. 쿨다운은 적용하지 않는다. 그 연속이 구간 전에 시작했는지(streak_started_before_window),
    구간 전에 이미 n에 도달했는지(condition_first_reached_before_window)를 함께 보고한다 - IF·고정 규칙에 똑같이 적용."""
    cons, start = 0, None
    reach_in = reach_before = 0
    first = None
    for i, r in enumerate(evals):
        if r.get("status") != "scored":
            cons, start = 0, None
            continue
        if pred(r):
            cons += 1
            start = i if cons == 1 else start
        else:
            cons, start = 0, None
        t = _ts(r.get("score_completed_at_utc"))
        if cons == n:
            reach_in += _within(t, t_a, t_b)
            reach_before += bool(t and t_a and t < t_a)
        if first is None and cons >= n and _within(t, t_a, t_b):
            s0, sn = evals[start], evals[start + n - 1]
            first = {"met_at_utc": t, "seq": r.get("evaluation_seq"), "streak_start_seq": s0.get("evaluation_seq"),
                     "streak_started_before_window": _ts(s0.get("score_completed_at_utc")) < t_a,
                     "condition_first_reached_before_window": _ts(sn.get("score_completed_at_utc")) < t_a,
                     "rel_window_start_sec": dstr._rel(t, t_a)}
    return {"first": first, "streaks_reaching_n_in_window": reach_in, "streaks_reaching_n_before_window": reach_before}


def if_vs_fixed_same_ticks(evals: list, t_a, t_b, t_slo=None) -> dict:
    """IF(기록된 is_anomalous = 점수 < 임계값)와 고정 규칙(c1 cpu_mean > 2.7 코어)을 같은 평가 시점·같은 정의로 비교.
    고정 규칙 임계값은 기존 detector_same_tick_replay.FIXED_THRESHOLD_CORES(단일 Pod 한도 3.0 × 0.9) 그대로 - cpu_mean은
    active+preview 합산이라는 한계(계약 §7). 별도 동결 탐지기 프로세스의 신호 시각과 비교하지 않는다.
    쿨다운을 적용한 IF 송신 조건 시각은 if_send_condition_cooldown_applied로 따로 둔다."""
    thr = dstr.FIXED_THRESHOLD_CORES
    out = {"definition": "계약 §7 - 같은 평가 시점, 첫 3연속 조건 충족(쿨다운 미적용, 주입 시점 리셋 없음)",
           "if": first_condition(evals, lambda r: bool(r.get("is_anomalous")), t_a, t_b),
           "fixed_rule": {**first_condition(evals, lambda r: r["features"]["cpu_mean"] > thr, t_a, t_b),
                          "threshold_cores": thr, "feature": "c1 cpu_mean(active+preview 합산)"}}
    sent = next((r for r in evals if r.get("condition_met") and _within(_ts(r.get("condition_met_at_utc")), t_a, t_b)), None)
    out["if_send_condition_cooldown_applied"] = {"first_utc": _ts(sent["condition_met_at_utc"]) if sent else None,
                                                 "seq": sent.get("evaluation_seq") if sent else None,
                                                 "note": "송신 조건 충족 시각(sink 수신과 대조하지 않음)"}
    for k in ("if", "fixed_rule"):
        f = out[k]["first"]
        out[k]["lead_vs_t_slo_sec"] = dstr._rel(t_slo, f["met_at_utc"]) if f and t_slo else None
    return out


def _category(status, lead):
    if status != "both":
        return status
    return "warned_before_t_slo" if lead > 0 else "late_warning"


def first_warning_vs_t_slo(sufficient, reasons, probe_rows, warnings, t_inj, t_end) -> dict:
    """status: both / no_warning / no_slo_violation / neither / insufficient_data. SLO는 slo_episodes r2(관찰 종료까지
    응답이 확인된 요청만), 경고는 [주입, 관찰 종료]의 판단 완료 시각만."""
    if not sufficient:
        return {"status": "insufficient_data", "category": "insufficient_data", "reasons": reasons}
    ep = slo_episodes.violation_episodes(probe_rows, dstr._align(t_inj, probe_rows), dstr._align(t_end, probe_rows))
    if not ep["computable"]:
        return {"status": "insufficient_data", "category": "insufficient_data", "reasons": [f"slo:{ep['reason']}"]}
    t_slo = _ts(ep["episodes"][0]["t_slo"]) if ep["episodes"] else None
    first = next((w for w in warnings if _within(w, t_inj, t_end)), None)
    out = {"slo_analyzer": ep["analyzer_revision"], "t_slo": t_slo, "first_warning_judgement_utc": first,
           "t_slo_rel_inj_sec": dstr._rel(t_slo, t_inj), "first_warning_rel_inj_sec": dstr._rel(first, t_inj)}
    if first and t_slo:
        out.update(status="both", lead_vs_t_slo_sec=dstr._rel(t_slo, first))
    else:
        out["status"] = "no_warning" if t_slo else ("no_slo_violation" if first else "neither")
    out["category"] = _category(out["status"], out.get("lead_vs_t_slo_sec"))
    return out


def no_injection_requests(probe_rows, t0, t1, min_requests: int = NO_INJECTION_MIN_REQUESTS) -> dict:
    """무주입 요청 충분성·품질 - slo_episodes(r2)를 요청 수 하한 1710으로 호출(공백 ≤ 30초·종료까지 응답 확인 그대로).
    준비 구간(관찰 시작 전) 요청은 따로 센다."""
    if not probe_rows:
        return {"computable": False, "reason": "probe_csv_missing", "reasons": ["7:probe_csv_missing"]}
    if not t0 or not t1:
        return {"computable": False, "reason": "observation_window_unknown", "reasons": []}
    a0, a1 = dstr._align(t0, probe_rows), dstr._align(t1, probe_rows)
    ep = slo_episodes.violation_episodes(probe_rows, a0, a1, min_requests=min_requests)
    pre = [r for r in probe_rows if r["sent_at"] < a0]
    main = [r for r in probe_rows if a0 <= r["sent_at"] <= a1]
    return {"computable": ep["computable"], "reason": ep["reason"], "min_requests": min_requests,
            "requests_readiness_phase": len(pre), "requests_main_observation": ep["requests_in_window"],
            "max_gap_main_sec": ep["max_gap_in_window_sec"],
            "failures_main": sum(not r["success"] for r in main),
            "slo_episodes_main": ep["episodes"], "slo_cumulative_sec_main": ep["cumulative_sec"],
            "reasons": [] if ep["computable"] else [f"2:probe_{ep['reason']}"]}


def analyze_no_injection(results_dir: Path, run_id: str, note: str = PILOT_NO_INJECTION_NOTE) -> dict:
    s = json.loads((results_dir / f"summary-{run_id}.json").read_text(encoding="utf-8"))
    evals, integrity = load_shadow(results_dir / f"c1-shadow-evidence-{run_id}.jsonl", run_id)
    t0, t1 = _ts(s.get("observation_started_at_utc")), _ts(s.get("observation_ended_at_utc"))
    reasons = _validity("no_injection", s, evals, integrity, None)
    req = no_injection_requests(_probe_rows(s), t0, t1, min_requests_for(s.get("observation_sec_requested") or 1800.0))
    reasons += req["reasons"]

    def started(r):
        return _ts(r.get("eval_started_at_utc"))
    readiness = [r for r in evals if t0 is None or (started(r) and started(r) < t0)]
    main = [r for r in evals if _within(started(r), t0, t1)]
    if not any(r["status"] == "scored" for r in main):  # 준비 구간 점수는 본 관찰 점수를 대신하지 않는다
        reasons.append("3:no_scored_evaluations_in_main_observation")
    sufficient = not reasons
    warn = [_ts(r["condition_met_at_utc"]) for r in evals if r.get("condition_met")]
    main_warn = sum(1 for w in warn if _within(w, t0, t1))
    return {"run_id": run_id, "kind": "no_injection", "data_status": "complete" if sufficient else "insufficient_data",
            "statistics_scope": "complete_run" if sufficient else "partial_or_invalid_run_not_comparable",
            "insufficient_reasons": reasons, "readiness": s.get("detector_readiness"), "requests": req,
            "observation_sec_measured": (t1 - t0).total_seconds() if t0 and t1 else None,
            "all": phase_counts(evals), "readiness_phase": phase_counts(readiness), "main_observation": phase_counts(main),
            "time_gaps_without_records": time_gaps(evals)["gaps"] if evals and all(started(r) for r in evals) else None,
            "timing_all": timing(evals) if evals and all(started(r) for r in evals) else None,
            "warnings_main_observation": main_warn if sufficient else None,
            "warnings_main_observation_partial": None if sufficient else main_warn,
            "warnings_status": "measured" if sufficient else "insufficient_data",
            "warnings_readiness_phase": sum(1 for w in warn if t0 and w < t0),
            "warning_counts": warning_counts(evals, {"main_observation": (t0, t1)}),
            "if_vs_fixed_same_ticks": if_vs_fixed_same_ticks(evals, t0, t1),
            "note": note}


VALIDATION_NOTE = ("v3.3-c1 독립 검증 기록(기록 전용 섀도, 계약 phase8-v33c1-independent-validation-contract-20261005) - "
                   "동결 탐지기와 평가 시각이 달라 그 신호와 직접 비교하지 않으며, 실제 전환 효과를 주장하지 않음")


def main():
    p = argparse.ArgumentParser(description="c1 섀도 기록 분석 - 개발 pilot 또는 독립 검증, 새 파일에만 저장")
    p.add_argument("--set", choices=["pilot", "validation"], default="pilot")
    p.add_argument("--run", required=True)
    p.add_argument("--out", required=True, type=Path)
    a = p.parse_args()
    if a.set == "validation":
        import run_v33c1_validation as val
        if a.run not in val.ORDER:
            raise SystemExit(f"독립 검증 ID가 아님: {a.run}")
        d, rid, pressure = val.RESULTS_DIR, val.run_id_for(a.run), val.kind(a.run) == "pressure"
        res = analyze_pressure(d, rid, note=VALIDATION_NOTE) if pressure else analyze_no_injection(d, rid, note=VALIDATION_NOTE)
    else:
        import run_c1_shadow_pilot as pilot
        if a.run not in (*pilot.PRESSURE_RUNS, *pilot.NO_INJECTION_RUNS):
            raise SystemExit(f"개발 pilot ID가 아님: {a.run}")
        rid = pilot.run_id_for(a.run)
        res = analyze_pressure(RESULTS_DIR, rid) if a.run in pilot.PRESSURE_RUNS else analyze_no_injection(RESULTS_DIR, rid)
    with open(a.out, "x", encoding="utf-8") as fh:
        fh.write(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    print(json.dumps({k: v for k, v in res.items() if k != "readiness_record"}, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
