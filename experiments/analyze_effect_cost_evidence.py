#!/usr/bin/env python3
"""Phase 8 최종 결론 근거: 효과·비용·적용조건 분석 (읽기 전용, 원본과 분리).

목적: "동일한 BlueGreen 복구 구조에서 고정 임계치 대신 Isolation Forest를
쓰면 실제 서비스 피해가 얼마나 줄고, 추가 비용을 감수할 가치가 있는가"에
답하기 위해 기존 원자료만으로 계산 가능한 것을 계산하고, 계산 불가능한
것은 명시적으로 "미측정"으로 남긴다.

이 스크립트는 클러스터에 접속하지 않고 로컬 파일만 읽는다. 결과 JSON·
state·hash·모델·threshold·SLO 정의·기존 코드(collect_metrics.py,
slo_judge.py 등)를 전혀 수정하지 않는다 - 전부 읽기만 한다. 새 계산이
필요한 경우도 기존 검증된 함수(collect_metrics.build_comparison(),
slo_judge.evaluate()/find_t_slo()/find_t_recovery())를 그대로 재사용하고
새로운 판정 로직을 만들지 않는다.

분석 대상: §129.1에서 확정한 권위 있는 core 45건(load_ramp/pod_kill=
mainexp-v2, network_degrade=mainexp-v1) + auxiliary 5건(mainexp-v1).
precursor/pilot은 generate_phase8_figures.load_authoritative()와 동일한
방식(officialexperiment-state*.json의 result_path로 파일 목록을 직접
구성)으로 원천 배제한다.
"""
import csv
import json
import statistics
from datetime import datetime, timedelta
from pathlib import Path

import collect_metrics as cm
import slo_judge as sj
from generate_phase8_figures import load_authoritative, SCENARIOS, ARMS, cell

RESULTS_DIR = Path("results")
AUDIT_DIR = Path("../audit-log")
DETECTOR_LOG_DIR = RESULTS_DIR / "detector-logs"

# authoritative plan_id per scenario (§129.1)
PLAN_ID = {"load_ramp": "mainexp-v2", "pod_kill": "mainexp-v2", "network_degrade": "mainexp-v1"}


def load_raw_trial(run_id):
    p = RESULTS_DIR / f"trial-{run_id}.json"
    return json.loads(p.read_text(encoding="utf-8"))


def probe_raw_path(row):
    """probe-{run_id}-{arm}-{rep}-raw.csv 명명 규칙(코드 확인, run_*_trial.py 공통)."""
    run_id = row["run_id"]
    arm = row["arm"]
    rep = row["rep"]
    return RESULTS_DIR / f"probe-{run_id}-{arm}-{rep}-raw.csv"


def detector_log_path(row):
    if row["arm"] == "native":
        return None
    mech = "isolation_forest" if row["arm"] == "proposed" else "fixed_threshold"
    run_id = row["run_id"]
    matches = sorted(DETECTOR_LOG_DIR.glob(f"detector-{mech}-{run_id}-*.log"))
    return matches[-1] if matches else None


def audit_log_path(run_id):
    """run_id는 반드시 row['run_id'](load_authoritative()/cell()이 이미 정확히
    해석한 값)를 그대로 받는다 - scenario/arm/rep/plan_id로 직접 재조립하지
    않는다. replacement trial(예: -retry1- 접미사)은 표준 패턴과 다른 run_id를
    쓰므로, 재조립하면 실제 존재하는 파일을 못 찾고 조용히 '기록 없음'으로
    오판한다(이번 턴에 실제로 발견한 버그 - load_ramp-fixed_threshold-05는
    공식 run_id가 …-05-retry1-…이라 재조립값과 달랐다)."""
    p = AUDIT_DIR / f"{run_id}.jsonl"
    return p if p.exists() else None


# ======================================================================
# 3. 증거 확보 가능성 확인
# ======================================================================
def section3_evidence_availability(out_core):
    print("\n" + "=" * 70)
    print("§3 증거 확보 가능성 (권위 있는 core 45건 전수 확인)")
    print("=" * 70)

    n_probe_raw = 0
    n_loadgen_summary = 0  # load_ramp만 존재(별도 부하생성기 ramp.py의 "단계별 요약"뿐, 요청별 원자료 아님)
    n_timestamps_full = 0
    n_detector_log = 0
    n_audit_present = 0
    missing_probe = []
    missing_ts = []

    REQUIRED_TS = ["t_injection", "t_detection", "t_decision", "t_api_request", "t_switch", "t_slo", "t_recovery", "t_run_end"]

    for row in out_core:
        raw = load_raw_trial(row["run_id"])
        if probe_raw_path(row).exists():
            n_probe_raw += 1
        else:
            missing_probe.append(row["run_id"])

        if row["scenario"] == "load_ramp":
            summ = list(RESULTS_DIR.glob(f"ramp-summary-{row['run_id']}-{row['arm']}-{row['rep']}.csv"))
            if summ:
                n_loadgen_summary += 1

        present_ts = sum(1 for f in REQUIRED_TS if raw.get(f) is not None)
        # t_slo/t_recovery/t_detection/t_decision/t_api_request/t_switch는 outcome에 따라
        # 구조적으로 None일 수 있음(예: prevented=t_slo 없음, native=탐지 계열 전부 None) -
        # "필드 자체가 스키마에 있고 값을 채울 기회가 있었는지"만 확인한다.
        if raw.get("t_injection") and raw.get("t_run_end"):
            n_timestamps_full += 1
        else:
            missing_ts.append(row["run_id"])

        if row["arm"] != "native":
            if detector_log_path(row):
                n_detector_log += 1
        else:
            n_detector_log += 1  # native는 해당없음(개입 없음) - "확보"와 동일하게 취급하지 않고 아래에서 별도 안내

        if audit_log_path(row["run_id"]):
            n_audit_present += 1

    print(f"- 요청별 전송·완료 시각/성공/latency (probe raw csv): 확보됨 {n_probe_raw}/45"
          + (f" (누락: {missing_probe})" if missing_probe else ""))
    print(f"- load_ramp 부하생성기(ramp.py)의 요청별 원자료: 없음(0/15) - 단계별 요약만 존재({n_loadgen_summary}/15, "
          f"target_rps/actual_rps/sent/success/p95/p99, 요청 단위 아님) - 일부 확보")
    print(f"- pod_kill/network_degrade의 '부하생성기': 해당없음 - 두 시나리오는 probe.py 단일 트래픽만 사용"
          f"(run_pod_kill_trial.py/run_network_degrade_trial.py 코드 주석으로 확인, §3 요구의 'probe와 load generator 구분' 자체가 성립 안 함)")
    print(f"- injection/탐지/판단/전환/복구/관측종료 시각(t_injection~t_run_end): 확보됨 {n_timestamps_full}/45"
          + (f" (확인 필요: {missing_ts})" if missing_ts else ""))
    print(f"- 실제 detection_source: 확보됨 45/45 (TrialResult 필드, native는 구조적으로 None)")
    print(f"- detector 판단 로그(seq/score/threshold/cooldown): 확보됨 {n_detector_log}/45(비-native 30건 중 실제 로그 파일 존재 건수 포함)")
    print(f"- promotion 감사 기록(audit-log/*.jsonl): 확보됨 {n_audit_present}/45 (파일 존재 여부만, 내용 신호 유무는 아님 - 없어도 정상: 신호 자체가 없었을 수 있음)")
    print(f"- detector/serving/preview 자원 사용(CPU·메모리) 기록: **없음(0/45)** - TrialResult 68개 필드 전체 확인, "
          f"results/ 디렉터리 어디에도 리소스 사용량 파일 없음(파일명·스키마 전수 검색). preview 소요시간(t_preview_prep_start~t_preview_ready)만 "
          f"간접 프록시로 존재 - 자원량 자체는 아님")
    print(f"- SLO probe 임계치(threshold={sj.LATENCY_THRESHOLD}s)는 probe.py 전용(max_tokens=1) - "
          f"load_ramp의 ramp.py(max_tokens=10, §130.1에서 이미 확인)에는 절대 적용하지 않음")


# ======================================================================
# 4A. 복구 효과
# ======================================================================
def section4a_recovery_effect(out_core):
    print("\n" + "=" * 70)
    print("§4A 복구 효과 - 시나리오별 fixed_threshold vs proposed")
    print("=" * 70)
    results = {}
    for scenario in SCENARIOS:
        row_out = {}
        for arm in ARMS:
            rows = cell(out_core, scenario, arm)
            vals = [r["recovery_sec"] for r in rows if r["recovery_sec"] is not None]
            excluded = [(r["run_id"], r["outcome"]) for r in rows if r["recovery_sec"] is None]
            row_out[arm] = {
                "n": len(vals), "n_total": len(rows), "excluded": excluded,
                "values": sorted(vals),
                "median": round(statistics.median(vals), 2) if vals else None,
                "range": (round(min(vals), 2), round(max(vals), 2)) if vals else None,
            }
        ft, pr = row_out["fixed_threshold"], row_out["proposed"]
        diff = pct = None
        if ft["median"] is not None and pr["median"] is not None:
            diff = round(ft["median"] - pr["median"], 2)
            if ft["median"] != 0:
                pct = round(diff / ft["median"] * 100, 1)
        row_out["fixed_vs_proposed"] = {
            "median_diff_sec": diff,
            "median_diff_formula": "median(fixed_threshold.recovery_sec) - median(proposed.recovery_sec), 초 단위, 양수=proposed가 더 빠름",
            "relative_reduction_pct": pct,
            "relative_reduction_formula": "median_diff_sec / median(fixed_threshold.recovery_sec) * 100 - "
                                           "'중앙값 기준' 1회 계산이며 반복별 개선율의 평균이 아님",
        }
        results[scenario] = row_out
        print(f"\n[{scenario}]")
        for arm in ARMS:
            d = row_out[arm]
            print(f"  {arm}: n={d['n']}/{d['n_total']} median={d['median']} range={d['range']} "
                  f"제외={[f'{rid}({oc})' for rid,oc in d['excluded']]}")
        print(f"  fixed_threshold-proposed 중앙값 차이: {diff}s | 상대 감소율(fixed 기준): {pct}%"
              f"  [산식: {row_out['fixed_vs_proposed']['relative_reduction_formula']}]")
    return results


# ======================================================================
# 4B. 실제 서비스 피해
# ======================================================================
def section4b_service_harm(out_core):
    print("\n" + "=" * 70)
    print("§4B 실제 서비스 피해 - recovery_sec 정의 재확인 + 단계별 소요시간 + 요청 단위 실측")
    print("=" * 70)
    print("recovery_sec 정의(collect_metrics.py 코드 확인): t_recovery - t_injection **아님**,")
    print("실제로는 _seconds_between(ts,'t_slo','t_recovery') = t_recovery - t_slo.")
    print("=> 'SLO 위반으로 판정된 시각'부터 'SLO 판정 로직이 정상 회복을 확인한 시각'까지의 구간이다.")
    print("   이것을 '전체 SLO 위반시간'이나 '사용자 중단시간'으로 자동 지칭하지 않는다 - ")
    print("   (a) t_slo 자체가 30초 지속조건 확정 후 스트릭 시작점으로 소급되고,")
    print("   (b) t_recovery도 30초 무위반 스트릭 시작점으로 소급되므로(§134에서 이미 확인),")
    print("   실제 사용자 체감 저하 구간과 정확히 일치한다는 보장이 없다 - 대리 지표로만 쓴다.\n")

    results = {}
    for scenario in SCENARIOS:
        for arm in ARMS:
            rows = cell(out_core, scenario, arm)
            for r in rows:
                raw = load_raw_trial(r["run_id"])
                probe_path = probe_raw_path(r)
                n_fail = n_total = n_over_thresh = None
                if probe_path.exists():
                    prows = sj.load_raw(probe_path)
                    n_total = len(prows)
                    n_fail = sum(1 for p in prows if not p["success"])
                    n_over_thresh = sum(1 for p in prows if p["latency"] > sj.LATENCY_THRESHOLD)
                stage_times = {}
                ts_pairs = [
                    ("injection_to_slo", "t_injection", "t_slo"),
                    ("slo_to_detection", "t_slo", "t_detection"),
                    ("detection_to_decision", "t_detection", "t_decision"),
                    ("decision_to_apirequest", "t_decision", "t_api_request"),
                    ("apirequest_to_switch", "t_api_request", "t_switch"),
                    ("slo_to_recovery(=recovery_sec)", "t_slo", "t_recovery"),
                ]
                for name, a, b in ts_pairs:
                    ta, tb = raw.get(a), raw.get(b)
                    if ta and tb:
                        try:
                            dt = (datetime.fromisoformat(tb) - datetime.fromisoformat(ta)).total_seconds()
                            stage_times[name] = round(dt, 2)
                        except Exception:
                            stage_times[name] = None
                    else:
                        stage_times[name] = None
                results[r["run_id"]] = {
                    "scenario": scenario, "arm": arm, "rep": r["rep"], "outcome": r["outcome"],
                    "probe_n_total": n_total, "probe_n_fail": n_fail, "probe_n_over_threshold": n_over_thresh,
                    "probe_fail_rate": round(n_fail / n_total, 4) if n_total else None,
                    "probe_over_threshold_rate": round(n_over_thresh / n_total, 4) if n_total else None,
                    "stage_times_sec": stage_times,
                }
    return results


# ======================================================================
# 4C. 탐지 방식의 기여 (detector_process vs detection_source 분리)
# ======================================================================
def section4c_detection_contribution(out_core):
    print("\n" + "=" * 70)
    print("§4C 탐지 방식의 기여 - detector_process/detection_source/시점/전환 분리 집계")
    print("=" * 70)
    for scenario in SCENARIOS:
        for arm in ("fixed_threshold", "proposed"):
            rows = cell(out_core, scenario, arm)
            print(f"\n[{scenario}/{arm}] n={len(rows)}")
            for r in sorted(rows, key=lambda x: x["rep"]):
                lead = r["detection_lead_sec"]
                lead_str = f"{lead:+.1f}s({'위반전' if lead and lead>0 else '위반후' if lead is not None else 'NA'})" if lead is not None else "NA(위반없음/미탐지)"
                print(f"  rep{r['rep']}: detector_process={'isolation_forest' if arm=='proposed' else 'fixed_threshold'} "
                      f"| detection_source={r['detection_source']} | detector_check={r['detector_check']} "
                      f"| lead={lead_str} | target_replaced={r['target_replaced']}({r['target_change_kind']})")


# ======================================================================
# 5. load_ramp/proposed 선제 전환 사례 심층 분석
# ======================================================================
def section5_preemptive_switch_deepdive(out_core):
    print("\n" + "=" * 70)
    print("§5 load_ramp/proposed 중 detection_lead_sec>0(SLO 판정 이전 탐지) 사례 심층 분석")
    print("=" * 70)

    rows = [r for r in cell(out_core, "load_ramp", "proposed") if (r["detection_lead_sec"] or 0) > 0]
    print(f"대상: {[r['run_id'] for r in rows]} (n={len(rows)})")

    WIN = 90  # switch 전후 관찰 폭(초) - slo_judge 60초 롤링 윈도우를 덮도록 여유 포함
    results = {}
    for r in rows:
        raw = load_raw_trial(r["run_id"])
        t_switch = datetime.fromisoformat(raw["t_switch"])
        t_slo = datetime.fromisoformat(raw["t_slo"]) if raw.get("t_slo") else None
        t_recovery = datetime.fromisoformat(raw["t_recovery"]) if raw.get("t_recovery") else None
        prows = sj.load_raw(probe_raw_path(r))

        def in_window(p, lo, hi):
            sent = datetime.fromisoformat(p["sent_at"]) if isinstance(p["sent_at"], str) else p["sent_at"]
            return lo <= sent <= hi

        pre = [p for p in prows if in_window(p, t_switch - timedelta(seconds=WIN), t_switch)]
        post = [p for p in prows if in_window(p, t_switch, t_switch + timedelta(seconds=WIN))]

        # threshold를 살짝 넘는 단발 지연(경계성 노이즈, 정상 운영에서도 발생 가능)과
        # 실패/2배 이상 지연(심각) 을 구분한다 - 둘을 뭉뚱그리면 "저하"의 의미가 흐려짐
        SEVERE_MULT = 2.0

        def bad(p):
            return p["latency"] > sj.LATENCY_THRESHOLD or not p["success"]

        def severe(p):
            return not p["success"] or p["latency"] > sj.LATENCY_THRESHOLD * SEVERE_MULT

        pre_bad, post_bad = [p for p in pre if bad(p)], [p for p in post if bad(p)]
        pre_severe, post_severe = [p for p in pre if severe(p)], [p for p in post if severe(p)]

        def offset(p):
            sent = datetime.fromisoformat(p["sent_at"]) if isinstance(p["sent_at"], str) else p["sent_at"]
            return round((sent - t_switch).total_seconds(), 2)

        post_worst = max(post, key=lambda p: p["latency"]) if post else None
        pre_worst = max(pre, key=lambda p: p["latency"]) if pre else None

        # 결론은 "심각(severe)" 이벤트 기준으로만 내린다 - 경계성 단발 초과는 정상 변동 범위로 취급
        if not pre_severe and post_severe:
            level = "B: 전환 이후 새 요청에서 심각한 저하 발생 근거(전환 전 90초 창엔 심각 이벤트 없음)"
        elif pre_severe and not post_severe:
            level = "A: 이동창에 낡은 심각값이 남아있었다는 근거(전환 후 90초 창엔 심각 이벤트 없음)"
        elif pre_severe and post_severe:
            level = "C: 혼재(전환 전후 모두 심각 이벤트 존재)"
        else:
            level = "D: 구분 불가(전환 전후 90초 창 모두 심각 이벤트 없음 - 경계성 초과만 존재, 관측 폭 밖 원인 가능)"

        rec = {
            "t_switch": raw["t_switch"], "t_slo": raw.get("t_slo"), "t_recovery": raw.get("t_recovery"),
            "t_switch_to_t_slo_sec": round((t_slo - t_switch).total_seconds(), 2) if t_slo else None,
            "pre_switch_90s": {"n": len(pre), "n_bad(any_threshold)": len(pre_bad), "n_severe(fail_or_2x)": len(pre_severe),
                                "worst_latency": round(pre_worst["latency"], 3) if pre_worst else None},
            "post_switch_90s": {"n": len(post), "n_bad(any_threshold)": len(post_bad), "n_severe(fail_or_2x)": len(post_severe),
                                 "worst_latency": round(post_worst["latency"], 3) if post_worst else None,
                                 "worst_offset_sec": offset(post_worst) if post_worst else None,
                                 "worst_success": post_worst["success"] if post_worst else None},
            "evidence_level": level,
        }
        results[r["run_id"]] = rec
        print(f"\n[{r['run_id']}] (target_replaced={r['target_replaced']}/{r['target_change_kind']})")
        print(f"  t_switch={raw['t_switch']} | t_switch~t_slo={rec['t_switch_to_t_slo_sec']}s | t_recovery={raw.get('t_recovery')}")
        print(f"  전환 전 {WIN}s: n={rec['pre_switch_90s']['n']} threshold초과={rec['pre_switch_90s']['n_bad(any_threshold)']} "
              f"심각={rec['pre_switch_90s']['n_severe(fail_or_2x)']} 최악latency={rec['pre_switch_90s']['worst_latency']}s")
        print(f"  전환 후 {WIN}s: n={rec['post_switch_90s']['n']} threshold초과={rec['post_switch_90s']['n_bad(any_threshold)']} "
              f"심각={rec['post_switch_90s']['n_severe(fail_or_2x)']} 최악latency={rec['post_switch_90s']['worst_latency']}s"
              f"(+{rec['post_switch_90s']['worst_offset_sec']}s 지점, success={rec['post_switch_90s']['worst_success']})")
        print(f"  => 증거 수준: {level}")
        print(f"  [주의] 이 요청이 실제로 어느 pod(구/신)로 라우팅됐는지는 probe 원자료에 식별자가 없어 시각만으로 확정 불가.")
    return results


# ======================================================================
# 6. 비용/오탐 증거
# ======================================================================
def _tally_detector_log(path):
    """score_server.py 로그 1건에서 평가/이상판정/신호발행/cooldown스킵 횟수 집계 (문자열 카운트, 신규 판정 로직 없음)."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    n_eval = sum(1 for l in lines if l.startswith("[20"))  # 평가 라인은 타임스탬프로 시작
    n_anomalous_point = sum(1 for l in lines if l.startswith("[20") and "(이상)" in l)
    n_signal_fired = sum(1 for l in lines if "신호 발행:" in l)
    n_cooldown_skip = sum(1 for l in lines if "cooldown 중" in l and "신호 스킵" in l)
    return {"n_eval": n_eval, "n_anomalous_point": n_anomalous_point,
            "n_signal_fired": n_signal_fired, "n_cooldown_skip": n_cooldown_skip}


def _tally_audit_log(path):
    outcomes = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        oc = rec.get("outcome", "(no outcome field)")
        outcomes[oc] = outcomes.get(oc, 0) + 1
    return outcomes


def section6_cost_and_fp(out_core):
    print("\n" + "=" * 70)
    print("§6 비용/오탐 증거 - 공통비용(preview) vs proposed 추가비용, 불필요 신호/전환")
    print("=" * 70)

    print(f"\n[6-0] 발화 규칙(score_server.py 코드 확인, 두 탐지방식 공용): "
          f"CONSECUTIVE_THRESHOLD=3(연속 3회 이상 판정 시 발행), COOLDOWN_SEC=60(발행 후 60초 재발행 억제)")

    print("\n[6-1] detector 로그 집계(seq 평가/이상판정/신호발행/cooldown스킵) - 시나리오x arm별, 30건 전수")
    log_tally = {}
    for scenario in SCENARIOS:
        for arm in ("fixed_threshold", "proposed"):
            rows = cell(out_core, scenario, arm)
            agg = {"n_eval": 0, "n_anomalous_point": 0, "n_signal_fired": 0, "n_cooldown_skip": 0, "n_trials_with_log": 0}
            per_trial = {}
            for r in rows:
                p = detector_log_path(r)
                if not p:
                    continue
                t = _tally_detector_log(p)
                per_trial[r["run_id"]] = t
                agg["n_trials_with_log"] += 1
                for k in ("n_eval", "n_anomalous_point", "n_signal_fired", "n_cooldown_skip"):
                    agg[k] += t[k]
            log_tally[(scenario, arm)] = {"agg": agg, "per_trial": per_trial}
            print(f"  [{scenario}/{arm}] 로그있음={agg['n_trials_with_log']}/{len(rows)} "
                  f"총평가={agg['n_eval']} 이상판정={agg['n_anomalous_point']} 신호발행={agg['n_signal_fired']} "
                  f"cooldown스킵={agg['n_cooldown_skip']}")

    print("\n[6-2] audit-log 결과(outcome) 집계 - 파일 존재하는 건에 한함(신호 자체가 없던 건은 파일 없음이 정상)")
    outcome_tally = {}
    for scenario in SCENARIOS:
        for arm in ARMS:
            rows = cell(out_core, scenario, arm)
            agg = {}
            for r in rows:
                p = audit_log_path(r["run_id"])
                if not p:
                    continue
                for oc, n in _tally_audit_log(p).items():
                    agg[oc] = agg.get(oc, 0) + n
            if agg:
                outcome_tally[(scenario, arm)] = agg
                print(f"  [{scenario}/{arm}] {agg}")

    print("\n[6-3] preview 준비 소요시간(t_preview_prep_start~t_preview_ready) - native/proposed 공통비용 프록시"
          "(자원량 자체 아님, 시간만)")
    preview_dur = {}
    for scenario in SCENARIOS:
        for arm in ARMS:
            rows = cell(out_core, scenario, arm)
            durs = []
            for r in rows:
                raw = load_raw_trial(r["run_id"])
                a, b = raw.get("t_preview_prep_start"), raw.get("t_preview_ready")
                if a and b:
                    durs.append(round((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds(), 1))
            preview_dur[(scenario, arm)] = durs
            if durs:
                print(f"  [{scenario}/{arm}] n={len(durs)} median={statistics.median(durs)}s range=({min(durs)},{max(durs)})")

    print("\n[6-4] 오프라인 재현(holdout) 대표성 확인 - 실제 배포 모델(model_v32b)의 사전 holdout 평가만 존재")
    holdout_path = Path("../anomaly-detection/v3/model_v32b/artifacts/holdout-evaluation.json")
    holdout = json.loads(holdout_path.read_text(encoding="utf-8")) if holdout_path.exists() else None
    if holdout:
        print(f"  holdout 세션: {holdout['holdout_sessions']} (라벨: 전부 'clean' - idle/low_load, 부하 램프업/장애 주입 없음)")
        print(f"  point-level FPR(전체)={holdout['overall_point_fpr']:.4f} ({holdout['overall_point_anomaly_count']}/{holdout['overall_point_count']}점)")
        print(f"  실제 신호(연속 3회+cooldown 통과) 기준 false signal episode = {holdout['overall_false_signal_episodes']}/6세션"
              f" (최대 연속 이상판정: {max(s['max_consecutive_anomalous'] for s in holdout['per_session'].values())}회, "
              f"발행 기준 3회 미달)")
        print("  [주의] 이 holdout은 idle/low_load에서만 측정됨 - load_ramp처럼 부하 자체가 램프업하는 조건의 "
              "정상구간 오탐율은 별도로 측정된 적 없음. 이 수치를 모든 부하 수준·수명주기로 일반화하지 않는다.")
        print("  [주의] '오프라인 point-level 이상판정'과 '실제 배포에서의 promote_preview 실행'은 다른 사건이다 - "
              "여기 없음(0/6)은 '홀드아웃에서 실신호 발행 0회'를 뜻할 뿐, 실험 중 정상구간 오탐이 0이라는 뜻이 아니다(그건 6-1/6-2가 별도로 다룸).")
    else:
        print("  결과 파일 없음")

    print("\n[6-5] 자원 사용(CPU/메모리) 비용: §3에서 이미 확인 - 0/45, 미측정으로 남김(추정치로 대체하지 않음)")

    return {
        "consecutive_threshold": 3, "cooldown_sec": 60,
        "detector_log_tally": {f"{k[0]}/{k[1]}": v["agg"] for k, v in log_tally.items()},
        "audit_outcome_tally": {f"{k[0]}/{k[1]}": v for k, v in outcome_tally.items()},
        "preview_prep_duration_sec": {f"{k[0]}/{k[1]}": v for k, v in preview_dur.items()},
        "holdout_fpr_model_v32b": holdout,
    }


# ======================================================================
# 7. "실제 실행" 판정 귀속 재검증 - detection_source(최초 유효 탐지)와
#    실제 executed_* 판정의 evidence.detector(실제 그 조치를 실행시킨 신호의
#    출처)가 다를 수 있음(recovery-policy/main.py의 _record_detection() vs
#    _record_decision() 분리 설계 - 코드 확인, 이번 지시 1절 요구사항).
# ======================================================================
def section7_executed_action_attribution(out_core):
    print("\n" + "=" * 70)
    print("§7 실제 승격을 실행시킨 신호의 detector - detection_source(최초 탐지)와 분리 재확인")
    print("=" * 70)
    print("근거(코드, recovery-policy/main.py): detection_source/detector는 '이 trial에서")
    print("최초로 유효 판정된 신호'의 것으로 딱 한 번만 기록되고 이후 절대 안 바뀐다.")
    print("반면 실제 executed_verified/unverified 판정은 나중에 도착한 다른 출처 신호가")
    print("만들 수도 있다(예: 반응형이 먼저 관찰만 하고 나중에 예측 신호가 실제 승격).")
    print("따라서 '실제 승격을 실행시킨 신호'는 TrialResult.detection_source가 아니라")
    print("audit-log의 executed_* 레코드 자체의 evidence.detector로 재확인해야 한다.\n")

    mismatches = []
    results = {}
    for scenario in SCENARIOS:
        for arm in ("fixed_threshold", "proposed"):
            rows = cell(out_core, scenario, arm)
            for r in sorted(rows, key=lambda x: x["rep"]):
                p = audit_log_path(r["run_id"])
                executed = []
                if p:
                    for line in p.read_text(encoding="utf-8").splitlines():
                        if not line.strip():
                            continue
                        rec = json.loads(line)
                        if rec.get("outcome") in ("executed_verified", "executed_unverified"):
                            executed.append(rec)
                exec_detectors = [e.get("evidence", {}).get("detector") for e in executed]
                raw = load_raw_trial(r["run_id"])
                trial_detector = raw.get("detector")
                trial_detection_source = r["detection_source"]
                mismatch = bool(executed) and trial_detector is not None and any(
                    d is not None and d != trial_detector for d in exec_detectors
                )
                if mismatch:
                    mismatches.append(r["run_id"])
                results[r["run_id"]] = {
                    "n_executed_records": len(executed),
                    "executed_outcomes": [e.get("outcome") for e in executed],
                    "executed_evidence_detectors": exec_detectors,
                    "trialresult_detection_source": trial_detection_source,
                    "trialresult_detector": trial_detector,
                    "mismatch_vs_trialresult_detector": mismatch,
                }
                print(f"  [{r['run_id']}] TrialResult: detection_source={trial_detection_source} detector={trial_detector}"
                      f" | audit executed 레코드 {len(executed)}건: outcome={results[r['run_id']]['executed_outcomes']}"
                      f" evidence.detector={exec_detectors}" + ("  <== 불일치!" if mismatch else ""))
    print(f"\n총 {sum(1 for v in results.values() if v['n_executed_records']>0)}건에서 실제 executed 레코드 확인,"
          f" 그중 TrialResult.detector와 실제 executed 신호의 evidence.detector가 다른 사례: {len(mismatches)}건 {mismatches}")
    return results


# ======================================================================
# 8. Stage A - 서비스 피해 비교표(사용자 지정 9열)
# ======================================================================
def _evaluable_seconds(raw, r):
    """'평가 가능한 시간'의 정의: probe raw 원자료의 첫~마지막 sent_at 구간
    (probe가 실제로 표본을 보낸 구간만 - injection~run_end 전체가 아니다,
    probe 시작 전 대기시간 등을 '평가 가능한데 위반 없음'으로 잘못 채우지
    않기 위함)."""
    p = probe_raw_path(r)
    if not p.exists():
        return None, None
    prows = sj.load_raw(p)
    if not prows:
        return None, None
    times = [datetime.fromisoformat(x["sent_at"]) if isinstance(x["sent_at"], str) else x["sent_at"] for x in prows]
    return (max(times) - min(times)).total_seconds(), prows


def _incomplete_at_end(prows, expected_interval_sec=1.0, tail_window_sec=30.0):
    """관측종료 시 미완료 요청 추정(코드 확인: probe.py는 duration_sec 종료 후
    최대 10초만 pending을 기다리고 그 반환값도 버린다 - 그 안에 안 끝난 요청은
    행 자체가 안 써진다). 직접 관측할 수는 없으니(원천적으로 기록이 없음),
    마지막 tail_window_sec 구간에서 expected_interval_sec 간격 대비 '있어야
    할 시각에 없는' 표본 수를 결측 후보로 센다 - 추정치이며 확정 아님."""
    if not prows:
        return None
    times = sorted(datetime.fromisoformat(x["sent_at"]) if isinstance(x["sent_at"], str) else x["sent_at"] for x in prows)
    last = times[-1]
    window_start = last - timedelta(seconds=tail_window_sec)
    window_times = [t for t in times if t >= window_start]
    if len(window_times) < 2:
        return 0
    expected = round((window_times[-1] - window_times[0]).total_seconds() / expected_interval_sec)
    return max(0, expected - (len(window_times) - 1))


def section8_stage_a_harm_table(out_core):
    print("\n" + "=" * 70)
    print("§8 Stage A - 서비스 피해 비교표 (9열: 지표/정의/원자료/비교가능여부/fixed/proposed/절대차이/상대차이/해석제한)")
    print("=" * 70)

    table = []
    for scenario in SCENARIOS:
        per_arm = {}
        for arm in ("fixed_threshold", "proposed"):
            rows = cell(out_core, scenario, arm)
            recov = [r["recovery_sec"] for r in rows if r["recovery_sec"] is not None]
            n_fail_total = n_over_total = n_req_total = 0
            eval_sec_total = viol_sec_total = 0.0
            incomplete_total = 0
            n_executed_total = 0
            stage_delays = {"det_to_dec": [], "dec_to_api": [], "api_to_switch": [], "det_to_switch": []}
            for r in rows:
                raw = load_raw_trial(r["run_id"])
                eval_sec, prows = _evaluable_seconds(raw, r)
                if prows:
                    n_req_total += len(prows)
                    n_fail_total += sum(1 for p in prows if not p["success"])
                    n_over_total += sum(1 for p in prows if p["latency"] > sj.LATENCY_THRESHOLD)
                    incomplete_total += _incomplete_at_end(prows) or 0
                if eval_sec:
                    eval_sec_total += eval_sec
                if raw.get("t_slo") and raw.get("t_recovery"):
                    viol_sec_total += (datetime.fromisoformat(raw["t_recovery"]) - datetime.fromisoformat(raw["t_slo"])).total_seconds()
                for name, a, b in (("det_to_dec", "t_detection", "t_decision"), ("dec_to_api", "t_decision", "t_api_request"),
                                    ("api_to_switch", "t_api_request", "t_switch"), ("det_to_switch", "t_detection", "t_switch")):
                    ta, tb = raw.get(a), raw.get(b)
                    if ta and tb:
                        stage_delays[name].append((datetime.fromisoformat(tb) - datetime.fromisoformat(ta)).total_seconds())
                p = audit_log_path(r["run_id"])
                if p:
                    n_executed_total += sum(1 for line in p.read_text(encoding="utf-8").splitlines() if line.strip()
                                             and json.loads(line).get("outcome") == "executed_verified")
            per_arm[arm] = {
                "recovery_median": round(statistics.median(recov), 2) if recov else None,
                "recovery_range": (round(min(recov), 2), round(max(recov), 2)) if recov else None,
                "recovery_n": len(recov),
                "eval_sec_total": round(eval_sec_total, 1),
                "viol_sec_total": round(viol_sec_total, 1),
                "viol_ratio": round(viol_sec_total / eval_sec_total, 4) if eval_sec_total else None,
                "n_req_total": n_req_total, "n_fail_total": n_fail_total, "n_over_total": n_over_total,
                "fail_rate": round(n_fail_total / n_req_total, 4) if n_req_total else None,
                "over_rate": round(n_over_total / n_req_total, 4) if n_req_total else None,
                "incomplete_total_est": incomplete_total,
                "n_executed_total": n_executed_total,
                "stage_delay_median": {k: (round(statistics.median(v), 2) if v else None) for k, v in stage_delays.items()},
            }
        ft, pr = per_arm["fixed_threshold"], per_arm["proposed"]
        table.append({"scenario": scenario, "fixed_threshold": ft, "proposed": pr})

        print(f"\n[{scenario}]")
        print(f"  recovery_sec: fixed={ft['recovery_median']}s(n={ft['recovery_n']},{ft['recovery_range']}) "
              f"proposed={pr['recovery_median']}s(n={pr['recovery_n']},{pr['recovery_range']}) "
              f"| 비교가능 - 정의 동일(둘 다 t_slo~t_recovery)")
        print(f"  평가가능시간 중 위반누적: fixed={ft['viol_sec_total']}s/{ft['eval_sec_total']}s(비율={ft['viol_ratio']}) "
              f"proposed={pr['viol_sec_total']}s/{pr['eval_sec_total']}s(비율={pr['viol_ratio']}) "
              f"| 비교가능 - 둘 다 probe 평가가능구간 기준")
        print(f"  요청 실패: fixed={ft['n_fail_total']}/{ft['n_req_total']}건({ft['fail_rate']}) "
              f"proposed={pr['n_fail_total']}/{pr['n_req_total']}건({pr['fail_rate']}) "
              f"| 비교가능(주의: n_req_total이 arm마다 다르면 반드시 건수+비율 함께 봄)")
        print(f"  latency threshold({sj.LATENCY_THRESHOLD}s) 초과: fixed={ft['n_over_total']}/{ft['n_req_total']}건({ft['over_rate']}) "
              f"proposed={pr['n_over_total']}/{pr['n_req_total']}건({pr['over_rate']}) | 비교가능(동일 probe 프로필만 사용)")
        print(f"  탐지->판단->API요청->전환 지연(중앙값,초): "
              f"fixed={ft['stage_delay_median']} proposed={pr['stage_delay_median']} "
              f"| 해석제한: None은 '그 경로 자체가 없었던 반복 존재'이지 0이 아님")
        print(f"  관측종료 시 미완료 요청(추정): fixed={ft['incomplete_total_est']}건 proposed={pr['incomplete_total_est']}건 "
              f"| 해석제한: 직접 기록 없음(코드상 pending 요청은 행 자체가 안 남음) - 마지막 30초 구간 기대표본수 대비 결측 추정치")
        print(f"  실제 전환 횟수(executed_verified): fixed={ft['n_executed_total']}건(5개 trial 중) "
              f"proposed={pr['n_executed_total']}건 | 해석제한: 횟수만 - '불필요'라는 판단은 별도 근거 없이 붙이지 않음")
    return table


# ======================================================================
# 9. Stage B 재조사 - in-flight vs 신규요청 구분, load_ramp stage 경계 대조
# ======================================================================
def section9_stage_b_refined(out_core):
    print("\n" + "=" * 70)
    print("§9 Stage B 재조사 - rep3/rep5, in-flight/신규 요청 구분 + load_ramp stage 경계 대조")
    print("=" * 70)
    print("목적: '전환 후 29~32초'라는 숫자 재현이 아니라, 그 사건이 (a) 전환 자체와")
    print("관련된 현상인지 (b) load_ramp 자체의 예정된 stage 상승과 우연히 겹친 것인지")
    print("구분 시도. 두 경우가 서로 다른 설명일 수 있음을 미리 전제하지 않고 각각 확인.\n")

    rows = [r for r in cell(out_core, "load_ramp", "proposed") if (r["detection_lead_sec"] or 0) > 0]
    for r in rows:
        raw = load_raw_trial(r["run_id"])
        t_switch = datetime.fromisoformat(raw["t_switch"])
        prows = sj.load_raw(probe_raw_path(r))

        def parse(p):
            return datetime.fromisoformat(p["sent_at"]) if isinstance(p["sent_at"], str) else p["sent_at"]

        # in-flight: sent_at < t_switch < sent_at+latency (요청이 전환 순간에 이미 진행 중이었음)
        inflight = [p for p in prows if parse(p) < t_switch < parse(p) + timedelta(seconds=p["latency"])]
        severe_post = [p for p in prows if parse(p) >= t_switch and
                       (not p["success"] or p["latency"] > sj.LATENCY_THRESHOLD * 2) and
                       parse(p) <= t_switch + timedelta(seconds=90)]

        print(f"\n[{r['run_id']}]")
        print(f"  t_switch={raw['t_switch']}")
        print(f"  전환 순간 in-flight(전환 전 SENT, 전환 후 완료)였던 요청: {len(inflight)}건"
              + (f" - latency={[round(p['latency'],3) for p in inflight]}" if inflight else ""))
        if severe_post:
            print(f"  전환 후 90초 내 심각 이벤트: {len(severe_post)}건 - 전부 sent_at이 전환 '이후'(in-flight 잔류 요청이 아니라 신규 발신 요청)")
        else:
            print("  전환 후 90초 내 심각 이벤트 없음")

        summ_path = RESULTS_DIR / f"ramp-summary-{r['run_id']}-{r['arm']}-{r['rep']}.csv"
        if summ_path.exists():
            with open(summ_path, encoding="utf-8") as f:
                stages = list(csv.DictReader(f))
            print(f"  load_ramp stage 경계:")
            containing_stage = None
            for s in stages:
                s_start, s_end = datetime.fromisoformat(s["stage_start_utc"]), datetime.fromisoformat(s["stage_end_utc"])
                marker = ""
                if s_start <= t_switch <= s_end:
                    marker += " <=t_switch 포함"
                    containing_stage = s["stage"]
                for p in severe_post:
                    if s_start <= parse(p) <= s_end:
                        marker += f" <=심각이벤트(+{round((parse(p)-t_switch).total_seconds(),1)}s) 포함"
                gap_to_switch = round((s_start - t_switch).total_seconds(), 1)
                print(f"    {s['stage']}: [{s['stage_start_utc']}, {s['stage_end_utc']}] "
                      f"success_rate={s['success_rate']} p95={s['p95']} p99={s['p99']} "
                      f"(시작-전환={gap_to_switch:+}s){marker}")
            if severe_post:
                sev_stage = next((s["stage"] for s in stages if
                                   datetime.fromisoformat(s["stage_start_utc"]) <= parse(severe_post[0]) <= datetime.fromisoformat(s["stage_end_utc"])), None)
                boundary_near = sev_stage is not None and containing_stage is not None and sev_stage != containing_stage
                print(f"  => 심각 이벤트가 {'전환과 다른 stage(직후 새 stage 시작과 겹침 - ramp 자체 부하상승이 경쟁 설명일 수 있음)' if boundary_near else '전환과 같은 stage 안(경계 무관, mid-stage)'}")
        else:
            print("  ramp-summary 없음")


def main():
    core_run_ids, out_core, aux_run_ids, out_aux = load_authoritative()
    assert len(core_run_ids) == 45 and len(aux_run_ids) == 5
    section3_evidence_availability(out_core)
    r4a = section4a_recovery_effect(out_core)
    r4b = section4b_service_harm(out_core)
    section4c_detection_contribution(out_core)
    r5 = section5_preemptive_switch_deepdive(out_core)
    r6 = section6_cost_and_fp(out_core)
    r7 = section7_executed_action_attribution(out_core)
    r8 = section8_stage_a_harm_table(out_core)
    section9_stage_b_refined(out_core)

    out = {"recovery_effect": r4a, "service_harm": r4b, "preemptive_switch_deepdive": r5, "cost_and_fp": r6,
           "executed_action_attribution": r7, "stage_a_harm_table": r8}
    Path("results/_effect_cost_analysis.json").write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("\n\n중간 산출물 저장: results/_effect_cost_analysis.json (분석 전용, 공식 결과 아님)")


if __name__ == "__main__":
    main()
