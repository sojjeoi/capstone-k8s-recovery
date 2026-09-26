#!/usr/bin/env python3
"""§153 - 선제 복구(조기경보) 가능성 판정을 위한 읽기 전용 재계산.

목적: 40개 non-native trial(core 45건 중 30 + 후속 5쌍 10) 전부에서
  1) t_first_bad(§152와 동일 방법 - 정렬+첫 매치, 새 판정 로직 없음)
  2) t_detection/t_switch(기존 trial JSON 필드, 그대로 읽기만)
  3) "탐지+전환" 최소 시간예산(EVAL_INTERVAL_SEC=15 x CONSECUTIVE_THRESHOLD=3,
     anomaly-detection/score_server.py:62,64에서 그대로 가져온 상수)과
     실측 t_injection->t_first_bad 간격을 trial별로 비교
  4) follow-up load_ramp 10건에 한해(cost-sampler CPU/메모리 시계열이
     존재하는 유일한 대상) 주입~첫피해 구간에 CPU 상승이 주입 전 60초
     베이스라인(같은 trial 내부, 평균+2표준편차) 대비 식별 가능한지 탐색
을 계산한다. 코드 수정 0, 새 실험 0, 기존 판정 로직(slo_judge.LATENCY_
THRESHOLD, load_authoritative(), cell())을 그대로 재사용한다.
"""
import json
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import slo_judge as sj
from generate_phase8_figures import load_authoritative, SCENARIOS, cell
from analyze_effect_cost_evidence import load_raw_trial, probe_raw_path
from correct_followup_138 import load_evidence, build_cohort, _parse_ts, FOLLOWUP_DIR

RESULTS_DIR = Path("results")

EVAL_INTERVAL_SEC = 15       # anomaly-detection/score_server.py:62 (두 탐지방식 공용)
CONSECUTIVE_THRESHOLD = 3    # anomaly-detection/score_server.py:64 (공용)
DETECT_FLOOR_BEST_SEC = CONSECUTIVE_THRESHOLD * EVAL_INTERVAL_SEC       # 45s, 위상 정렬 최선
DETECT_FLOOR_WORST_SEC = DETECT_FLOOR_BEST_SEC + EVAL_INTERVAL_SEC      # 60s, 위상 어긋남 최악


def parse_dt(s):
    return None if s is None else datetime.fromisoformat(s.replace("Z", "+00:00"))


def first_bad(rows, t_injection):
    for r in rows:
        if r["sent_at"] < t_injection:
            continue
        if (not r["success"]) or r["latency"] > sj.LATENCY_THRESHOLD:
            return r
    return None


def bad_rows_within(rows, t_from, seconds=60):
    t_to = t_from + timedelta(seconds=seconds)
    return [r for r in rows if t_from < r["sent_at"] <= t_to and ((not r["success"]) or r["latency"] > sj.LATENCY_THRESHOLD)]


def first_confirmed_bad(rows, t_injection, within_sec=60):
    """사용자 지시 §1 - 단일 요청 자연변동과 장애 관련 저하 구별. 사전에 고정한
    규칙(사후 결과 보고 정한 것 아님): '이후 60초 안에 나쁜 요청이 한 건 더
    있는' 첫 번째 나쁜 요청만 '확인된 저하 개시'로 인정한다. 고립된 단발
    위반(60초 안에 후속 없음)은 건너뛰고 다음 후보를 본다."""
    bads = [r for r in rows if r["sent_at"] >= t_injection and ((not r["success"]) or r["latency"] > sj.LATENCY_THRESHOLD)]
    for i, r in enumerate(bads):
        t_to = r["sent_at"] + timedelta(seconds=within_sec)
        if any(r2["sent_at"] <= t_to for r2 in bads[i + 1:i + 2]):
            return r, (i > 0)  # (확인된 저하 시작 요청, 그 앞에 고립 단발이 있었는가)
    return None, False


def timing_fields(t_injection, t_detection, t_switch, fb, rows):
    out = {
        "t_detection": t_detection.isoformat() if t_detection else None,
        "t_switch": t_switch.isoformat() if t_switch else None,
        "detection_to_injection_sec": round((t_detection - t_injection).total_seconds(), 3) if t_detection else None,
        "switch_to_injection_sec": round((t_switch - t_injection).total_seconds(), 3) if t_switch else None,
        "detection_to_switch_sec": round((t_switch - t_detection).total_seconds(), 3) if (t_detection and t_switch) else None,
    }
    if fb is None:
        out["t_first_bad"] = None
        return out
    out["t_first_bad"] = fb["sent_at"].isoformat()
    out["injection_to_firstbad_sec"] = round((fb["sent_at"] - t_injection).total_seconds(), 3)
    out["switch_before_firstbad"] = bool(t_switch and t_switch < fb["sent_at"])
    out["floor_exceeds_lead_best"] = out["injection_to_firstbad_sec"] < DETECT_FLOOR_BEST_SEC
    out["floor_exceeds_lead_worst"] = out["injection_to_firstbad_sec"] < DETECT_FLOOR_WORST_SEC

    followers = bad_rows_within(rows, fb["sent_at"], 60)
    out["n_bad_within_60s_after_firstbad"] = len(followers)
    out["second_bad_gap_sec"] = round((followers[0]["sent_at"] - fb["sent_at"]).total_seconds(), 3) if followers else None
    out["firstbad_was_isolated_singleton"] = len(followers) == 0

    cb, had_isolated_singleton_before = first_confirmed_bad(rows, t_injection, 60)
    if cb is None:
        out["t_first_confirmed_bad"] = None
    else:
        out["t_first_confirmed_bad"] = cb["sent_at"].isoformat()
        out["injection_to_confirmedbad_sec"] = round((cb["sent_at"] - t_injection).total_seconds(), 3)
        out["confirmedbad_differs_from_firstbad"] = cb["sent_at"] != fb["sent_at"]
        out["switch_before_confirmedbad"] = bool(t_switch and t_switch < cb["sent_at"])
        out["floor_exceeds_confirmedlead_best"] = out["injection_to_confirmedbad_sec"] < DETECT_FLOOR_BEST_SEC
        out["floor_exceeds_confirmedlead_worst"] = out["injection_to_confirmedbad_sec"] < DETECT_FLOOR_WORST_SEC
    return out


def analyze_core_trial(row):
    run_id = row["run_id"]
    try:
        raw = load_raw_trial(run_id)
        t_injection = parse_dt(raw["t_injection"])
        t_detection = parse_dt(raw.get("t_detection"))
        t_switch = parse_dt(raw.get("t_switch"))
        csv_path = probe_raw_path(row)
        result = {"run_id": run_id, "scenario": row["scenario"], "arm": row["arm"],
                  "rep": row.get("rep"), "t_injection": t_injection.isoformat(),
                  "outcome": raw.get("outcome")}
        if not csv_path.exists():
            result["error"] = f"no_probe_csv:{csv_path.name}"
            return result
        rows = sj.load_raw(csv_path)
        fb = first_bad(rows, t_injection)
        result.update(timing_fields(t_injection, t_detection, t_switch, fb, rows))
        return result
    except Exception as e:
        return {"run_id": run_id, "scenario": row.get("scenario"), "arm": row.get("arm"),
                "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()}


def analyze_followup_trial(arm, rep, run_id_override=None):
    run_id = run_id_override or f"load_ramp-{arm}-{rep:02d}-post_hoc_followup-v1"
    try:
        trial_path = FOLLOWUP_DIR / f"trial-{run_id}.json"
        raw = json.loads(trial_path.read_text(encoding="utf-8"))
        t_injection = parse_dt(raw["t_injection"])
        t_detection = parse_dt(raw.get("t_detection"))
        t_switch = parse_dt(raw.get("t_switch"))
        evidence = load_evidence(run_id, arm, rep)
        cohort = build_cohort(evidence, t_injection)
        rows = []
        for rid, e in {**cohort["in_window"], **cohort["post_window"]}.items():
            terms = cohort["terminals_by_id"].get(rid)
            if not terms:
                continue
            t0 = terms[0]
            rows.append({"sent_at": _parse_ts(e["sent_at"]), "success": bool(t0.get("success")),
                         "latency": float(t0.get("elapsed_monotonic_sec", 0))})
        rows.sort(key=lambda r: r["sent_at"])
        fb = first_bad(rows, t_injection)
        result = {"run_id": run_id, "scenario": "load_ramp", "arm": arm, "rep": rep,
                  "t_injection": t_injection.isoformat()}
        result.update(timing_fields(t_injection, t_detection, t_switch, fb, rows))
        return result
    except Exception as e:
        return {"run_id": run_id, "scenario": "load_ramp", "arm": arm, "rep": rep,
                "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()}


def cpu_precursor_check(arm, rep, run_id_override, injection_to_firstbad_sec):
    """followup load_ramp 전용 - cost-sampler cluster_pods.samples에서 vllm-serving-*
    pod의 cpu_millicores 합을 시간순으로 뽑아, 주입 전 60초 베이스라인(평균+2표준편차)
    대비 주입~첫피해 구간에서 처음 넘어서는 시각을 찾는다. 탐색용 - 사후 임계값
    피팅 아님(고정 규칙: baseline mean+2*std), cache/queue는 이 데이터에 없어 제외."""
    run_id = run_id_override or f"load_ramp-{arm}-{rep:02d}-post_hoc_followup-v1"
    cost_path = FOLLOWUP_DIR / f"cost-{run_id}.json"
    if not cost_path.exists():
        return {"run_id": run_id, "error": "no_cost_json"}
    try:
        doc = json.loads(cost_path.read_text(encoding="utf-8"))
        t_injection = parse_dt(doc["phase_marks"]["t_injection"])
        samples = doc["cluster_pods"]["samples"]
        series = []
        for s in samples:
            if not s.get("ok"):
                continue
            t = parse_dt(s["t"])
            cpu = sum(p["cpu_millicores"] for p in s.get("pods", []) if p["pod"].startswith("vllm-serving"))
            series.append((t, cpu))
        series.sort(key=lambda x: x[0])
        baseline = [cpu for t, cpu in series if t < t_injection and (t_injection - t).total_seconds() <= 60]
        if len(baseline) < 3:
            return {"run_id": run_id, "error": "insufficient_baseline_samples", "n_baseline": len(baseline)}
        mean_b = sum(baseline) / len(baseline)
        var_b = sum((x - mean_b) ** 2 for x in baseline) / len(baseline)
        std_b = var_b ** 0.5
        cutoff = mean_b + 2 * std_b
        window_end = t_injection + timedelta(seconds=max(injection_to_firstbad_sec, 0)) if injection_to_firstbad_sec else t_injection
        first_exceed = None
        for t, cpu in series:
            if t < t_injection or t > window_end:
                continue
            if cpu > cutoff:
                first_exceed = t
                break
        return {
            "run_id": run_id, "baseline_mean_cpu_millicores": round(mean_b, 1),
            "baseline_std_cpu_millicores": round(std_b, 1), "cutoff_mean_plus_2std": round(cutoff, 1),
            "injection_to_firstbad_sec": injection_to_firstbad_sec,
            "first_exceed_at_sec_after_injection": round((first_exceed - t_injection).total_seconds(), 1) if first_exceed else None,
            "lead_if_used_as_precursor_sec": round((window_end - first_exceed).total_seconds(), 1) if first_exceed else None,
        }
    except Exception as e:
        return {"run_id": run_id, "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()}


def main():
    core_run_ids, out_core, aux_run_ids, out_aux = load_authoritative()
    all_results = []
    for scenario in SCENARIOS:
        for arm in ("fixed_threshold", "proposed"):
            for row in cell(out_core, scenario, arm):
                all_results.append(analyze_core_trial(row))

    followup_results = []
    for arm in ("fixed_threshold", "proposed"):
        for rep in range(1, 6):
            override = "load_ramp-fixed_threshold-03-retry1-post_hoc_followup-v1" if (arm == "fixed_threshold" and rep == 3) else None
            r = analyze_followup_trial(arm, rep, run_id_override=override)
            followup_results.append(r)
            all_results.append(r)

    cpu_checks = []
    for r in followup_results:
        if "injection_to_firstbad_sec" not in r:
            continue
        # 고립 단발이면 확인된(지속) 저하 시각까지 창을 넓혀서 본다 - 원 첫위반
        # 시각만 쓰면 창이 너무 짧아 진짜 전조를 놓친다(사용자 지시 §1 반영).
        window_sec = r.get("injection_to_confirmedbad_sec") if r.get("firstbad_was_isolated_singleton") else r["injection_to_firstbad_sec"]
        if window_sec is None:
            continue
        override = "load_ramp-fixed_threshold-03-retry1-post_hoc_followup-v1" if (r["arm"] == "fixed_threshold" and r["rep"] == 3) else None
        c = cpu_precursor_check(r["arm"], r["rep"], override, window_sec)
        c["used_confirmed_window"] = bool(r.get("firstbad_was_isolated_singleton"))
        cpu_checks.append(c)

    out_path = RESULTS_DIR / "_precursor_timing_analysis_153.json"
    out_path.write_text(json.dumps({"trials": all_results, "cpu_precursor_checks": cpu_checks}, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"wrote {out_path} ({len(all_results)} trials, {len(cpu_checks)} cpu checks)")
    print(f"\ndetection floor = CONSECUTIVE_THRESHOLD({CONSECUTIVE_THRESHOLD}) x EVAL_INTERVAL_SEC({EVAL_INTERVAL_SEC}) = {DETECT_FLOOR_BEST_SEC}s (best) ~ {DETECT_FLOOR_WORST_SEC}s (phase-worst)\n")
    print("=== per-trial timing ===")
    for r in all_results:
        if "error" in r:
            print(f"{r.get('run_id',''):50s} ERROR: {r['error']}")
            continue
        lead = r.get("injection_to_firstbad_sec")
        if lead is None:
            print(f"{r['run_id']:50s} outcome={r.get('outcome')} no_bad_request")
            continue
        print(f"{r['run_id']:50s} lead={lead:8.2f}s det={str(r.get('detection_to_injection_sec')):>8s} "
              f"sw={str(r.get('switch_to_injection_sec')):>8s} sw<firstbad={r.get('switch_before_firstbad')} "
              f"floor>lead(best/worst)={r.get('floor_exceeds_lead_best')}/{r.get('floor_exceeds_lead_worst')} "
              f"followers60s={r.get('n_bad_within_60s_after_firstbad')} gap2nd={r.get('second_bad_gap_sec')} "
              f"isolated={r.get('firstbad_was_isolated_singleton')} confirmedlead={r.get('injection_to_confirmedbad_sec')} "
              f"floor>confirmedlead(b/w)={r.get('floor_exceeds_confirmedlead_best')}/{r.get('floor_exceeds_confirmedlead_worst')}")

    print("\n=== cpu precursor checks (follow-up load_ramp only) ===")
    for c in cpu_checks:
        print(json.dumps(c, ensure_ascii=False))


if __name__ == "__main__":
    main()
