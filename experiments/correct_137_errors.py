#!/usr/bin/env python3
"""§137 정정 — analyze_effect_cost_evidence.py §8의 특정 계산·표현 오류를
바로잡는다(§137 문서 §0/§2 자체를 조용히 덮어쓰지 않고, 이 스크립트+정정
섹션으로 이력을 남긴다). 읽기 전용, 원본 미변경. 새 판정 로직을 만들지
않는다 - slo_judge.find_t_slo()/find_t_recovery()를 그 기존 not_before
인자로 반복 호출해 재사용할 뿐이다.

바로잡는 항목(사용자 지시 §2 A~D):
A. viol_sec_total은 사실 recovery_sec 합계였다(t_recovery-t_slo) - 독립적인
   "SLO 위반 누적시간"이 아니었다. 여기서 (1) 점단위 rolling-window 위반
   시간(raw, persistence 미적용)과 (2) find_t_slo/find_t_recovery를 반복
   재사용해 찾은 persistence-확정 episode(들)를 분리해서 계산한다.
B. load_ramp 전환율의 분모를 "전체 유효 trial(5)" vs "SLO 위반 관측
   trial(recovery_sec 정의됨, 4)"로 분리해 다시 표로 낸다.
C. 요청 실패율을 trial별로 쪼개 보여주고, 기존 §137 표의 값은 "저장된
   요청 기준 pooled 비율"로 재명명한다.
D. 관측종료 미완료 요청 추정치의 한계 - 마지막 저장 행 시각만으로는 꼬리
   전체가 통째로 빠진 경우를 못 잡는다는 걸 t_run_end 대조로 확인한다.
"""
import json
from datetime import datetime
from pathlib import Path

import slo_judge as sj
from analyze_effect_cost_evidence import (
    RESULTS_DIR, PLAN_ID, load_raw_trial, probe_raw_path,
)
from generate_phase8_figures import load_authoritative, SCENARIOS, ARMS, cell


# ======================================================================
# A. SLO 위반 시간 - raw(rolling-window, persistence 미적용) vs
#    episode(find_t_slo/find_t_recovery 반복 재사용, persistence 확정)
# ======================================================================
def raw_violating_seconds(points):
    """evaluate()가 이미 계산한 point별 판정만으로 '위반 상태였던 시간'을
    직접 합산한다 - 30초 지속조건(persistence)은 전혀 적용하지 않는다(그래서
    'raw'). 각 point의 판정이 다음 point 직전까지 유지된다고 가정한다
    (probe 주기(~1초)보다 짧은 변동은 이 근사로 못 잡는다 - 한계로 명시).
    availability는 표본수 하한이 없어 항상 판정 가능하므로, 이 구간 전체
    (첫~마지막 sent_at)를 '판정 가능한 시간'으로 본다 - latency 컴포넌트만
    표본 부족 시 그 컴포넌트의 기여를 뺀다(availability_violating은 그대로
    유효)."""
    if len(points) < 2:
        return {"raw_violating_sec": 0.0, "eval_sec": 0.0, "n_points": len(points)}
    pts = sorted(points, key=lambda p: p["t"])
    viol_sec = 0.0
    eval_sec = 0.0
    for i in range(len(pts) - 1):
        p0, p1 = pts[i], pts[i + 1]
        dt = (p1["t"] - p0["t"]).total_seconds()
        eval_sec += dt
        violating = (p0["latency_evaluable"] and p0["latency_violating"]) or p0["availability_violating"]
        if violating:
            viol_sec += dt
    return {"raw_violating_sec": round(viol_sec, 1), "eval_sec": round(eval_sec, 1), "n_points": len(pts)}


def find_all_episodes(points, initial_not_before=None):
    """slo_judge.find_t_slo()/find_t_recovery()를 그대로, 반복 재사용한다 -
    새 판정 로직 없음. 각 episode의 t_recovery를 다음 탐색의 not_before로
    넘겨 재위반(같은 trial 안의 두 번째 이상 위반)이 있는지 확인한다.
    관측 종료까지 미회복이면(t_recovery=None) 그 episode를 '열린 상태'로
    표시하고 탐색을 멈춘다(그 뒤 구간은 판정 불가).

    initial_not_before(선택, 기본 None - 기존 호출부 전부 이 인자를 안 넘기므로
    동작 무변경): 첫 episode 탐색의 streak-시작 자격 하한을 지정한다(원본
    run_once() 판정의 find_t_slo(points, not_before=t_injection)과 정확히
    같은 방식 - 재위반 이후 탐색은 여전히 그 episode의 t_recovery를 하한으로
    쓴다). points 자체(=rolling window 계산에 쓰이는 원자료)는 이 인자로
    잘라내지 않는다 - 주입 전 이력이 초기 60초 window 계산에 남아있어야
    원본과 동일한 판정이 나온다(§138 이후 지시 §4 - 이 구분을 처음엔 놓쳐서
    cohort를 주입 시각으로 통째로 잘라 points에 넣었다가 원본 t_slo/t_recovery와
    몇 분씩 어긋나는 걸 실측으로 발견하고 고쳤다)."""
    episodes = []
    not_before = initial_not_before
    guard = 0
    while guard < 20:  # 무한루프 방지용 상한 - 실제로 이만큼 재위반할 리 없음
        guard += 1
        t_slo = sj.find_t_slo(points, not_before=not_before)
        if t_slo is None:
            break
        t_recovery = sj.find_t_recovery(points, t_slo)
        episodes.append({"t_slo": t_slo, "t_recovery": t_recovery, "open_at_observation_end": t_recovery is None})
        if t_recovery is None:
            break
        not_before = t_recovery
    return episodes


def section_a_violation_time(out_core):
    print("\n" + "=" * 70)
    print("§A 정정 - SLO 위반 시간: raw(rolling-window) vs episode(persistence 확정)")
    print("=" * 70)
    print("이전 §137의 viol_sec_total은 t_recovery-t_slo 합계였다 - 이건 '확정된")
    print("첫 episode의 길이' 합계이지, 독립적으로 계산한 위반 누적시간이 아니다.")
    print("아래는 그 둘을 분리하고, 재위반 여부도 기존 함수 재사용으로 확인한다.\n")

    results = {}
    for scenario in SCENARIOS:
        for arm in ARMS:
            rows = cell(out_core, scenario, arm)
            agg = {"raw_violating_sec": 0.0, "eval_sec": 0.0, "n_multi_episode": 0, "n_open_at_end": 0,
                   "first_episode_sec_sum": 0.0, "n_first_episode": 0}
            per_trial = {}
            for r in rows:
                p = probe_raw_path(r)
                if not p.exists():
                    continue
                prows = sj.load_raw(p)
                points = sj.evaluate(prows)
                raw = raw_violating_seconds(points)
                episodes = find_all_episodes(points)
                agg["raw_violating_sec"] += raw["raw_violating_sec"]
                agg["eval_sec"] += raw["eval_sec"]
                if len(episodes) >= 2:
                    agg["n_multi_episode"] += 1
                if episodes and episodes[-1]["open_at_observation_end"]:
                    agg["n_open_at_end"] += 1
                if episodes:
                    e0 = episodes[0]
                    if e0["t_recovery"] is not None:
                        agg["first_episode_sec_sum"] += (e0["t_recovery"] - e0["t_slo"]).total_seconds()
                        agg["n_first_episode"] += 1
                per_trial[r["run_id"]] = {
                    "raw_violating_sec": raw["raw_violating_sec"], "eval_sec": raw["eval_sec"],
                    "n_episodes_detected": len(episodes),
                    "episodes": [{"t_slo": e["t_slo"].isoformat(), "t_recovery": e["t_recovery"].isoformat() if e["t_recovery"] else None,
                                   "open_at_observation_end": e["open_at_observation_end"]} for e in episodes],
                    "recovery_sec_schema_value": r["recovery_sec"],
                }
            results[(scenario, arm)] = {"agg": agg, "per_trial": per_trial}
            ratio = round(agg["raw_violating_sec"] / agg["eval_sec"], 4) if agg["eval_sec"] else None
            print(f"[{scenario}/{arm}] raw 위반시간(persistence 미적용)={agg['raw_violating_sec']}s / "
                  f"평가가능={agg['eval_sec']}s (비율={ratio}) | 재위반(2개 이상 episode) 감지={agg['n_multi_episode']}건 "
                  f"| 관측종료 시 미회복(open) episode={agg['n_open_at_end']}건")
            for run_id, t in per_trial.items():
                if t["n_episodes_detected"] >= 2 or (t["episodes"] and t["episodes"][-1]["open_at_observation_end"]):
                    print(f"    [주목] {run_id}: {t['n_episodes_detected']}개 episode 감지 - {t['episodes']}")
    return {f"{k[0]}/{k[1]}": v["agg"] for k, v in results.items()}


# ======================================================================
# B. load_ramp 전환율 분모 분리
# ======================================================================
def section_b_conversion_denominators(out_core):
    print("\n" + "=" * 70)
    print("§B 정정 - load_ramp 전환율: '전체 유효 trial' vs 'SLO 위반 관측 trial' 분모 분리")
    print("=" * 70)
    for arm in ("fixed_threshold", "proposed"):
        rows = cell(out_core, "load_ramp", arm)
        n_total_valid = len(rows)  # 5 (prevented 포함 - 기준선의 유효한 결과로 유지)
        violation_observed = [r for r in rows if r["recovery_sec"] is not None]
        n_violation_observed = len(violation_observed)
        n_executed_total_valid = 0
        n_executed_violation_observed = 0
        for r in rows:
            audit_path = Path("../audit-log") / f"{r['run_id']}.jsonl"
            executed = False
            if audit_path.exists():
                for line in audit_path.read_text(encoding="utf-8").splitlines():
                    if line.strip() and json.loads(line).get("outcome") == "executed_verified":
                        executed = True
            if executed:
                n_executed_total_valid += 1
                if r["recovery_sec"] is not None:
                    n_executed_violation_observed += 1
        print(f"[{arm}] 전체 유효 trial 기준 실제 전환: {n_executed_total_valid}/{n_total_valid} "
              f"| SLO 위반 관측 trial 기준 실제 전환: {n_executed_violation_observed}/{n_violation_observed}")
    print("\n주의: recovery_sec가 정의되는 4건(위반 관측 trial)을 '유효 trial 전체'라고 부르지 않는다 -")
    print("무탐지·무개입 자연회복(prevented 아닌, 위반은 있었지만 개입 없이 회복)도 기준선의")
    print("유효한 결과다. 전환이 많다고 그 자체로 우수하다고 해석하지 않는다(전환 필요성의")
    print("독립적 근거 없이는 '전환 횟수'를 '성능'으로 바꾸지 않는다).")


# ======================================================================
# C. 요청 실패율 - pooled 명시 + trial별 분해
# ======================================================================
def section_c_failure_rate_breakdown(out_core):
    print("\n" + "=" * 70)
    print("§C 정정 - 요청 실패율: 'pooled(저장된 요청 기준)' 명시 + trial별 분해")
    print("=" * 70)
    for scenario in SCENARIOS:
        for arm in ("fixed_threshold", "proposed"):
            rows = cell(out_core, scenario, arm)
            print(f"\n[{scenario}/{arm}] trial별 분해(요청 수 = 독립 실험 수가 아니라 저장된 요청 로그 수):")
            pooled_n = pooled_fail = pooled_over = 0
            for r in sorted(rows, key=lambda x: x["rep"]):
                p = probe_raw_path(r)
                if not p.exists():
                    continue
                prows = sj.load_raw(p)
                n = len(prows)
                nfail = sum(1 for x in prows if not x["success"])
                nover = sum(1 for x in prows if x["latency"] > sj.LATENCY_THRESHOLD)
                span = (max(x["sent_at"] for x in prows) - min(x["sent_at"] for x in prows)).total_seconds() if prows else 0
                pooled_n += n; pooled_fail += nfail; pooled_over += nover
                print(f"    rep{r['rep']}: n={n} 관측구간={round(span,1)}s 실패={nfail}({round(nfail/n,4) if n else None}) "
                      f"지연초과={nover}({round(nover/n,4) if n else None})")
            print(f"  => pooled(저장된 요청 {pooled_n}건 기준) 실패율={round(pooled_fail/pooled_n,4) if pooled_n else None} "
                  f"지연초과율={round(pooled_over/pooled_n,4) if pooled_n else None} "
                  f"- 이 비율차를 '독립 실험 수천 회'로 해석하지 않는다(같은 5개 trial 안의 요청은 서로 독립이 아니다).")


# ======================================================================
# D. 미완료 요청 - 마지막 저장 시각 vs t_run_end 대조(꼬리 전체 소실 탐지)
# ======================================================================
def section_d_incomplete_check(out_core):
    print("\n" + "=" * 70)
    print("§D 정정 - 관측종료 미완료 요청: 마지막 저장 시각을 t_run_end와 대조(꼬리 전체 소실 탐지)")
    print("=" * 70)
    print("이전 추정(§137)은 '마지막 30초 구간의 기대표본 대비 결측'만 봤다 - 꼬리 전체가")
    print("통째로 안 써졌다면(과정 종료로 인해) 이 방법으로는 감지 불가하다. t_run_end와")
    print("마지막 저장 sent_at의 격차가 크면 '미확인'으로 표시한다(0건이라 단정하지 않는다).\n")
    flagged = []
    for scenario in SCENARIOS:
        for arm in ARMS:
            for r in cell(out_core, scenario, arm):
                p = probe_raw_path(r)
                if not p.exists():
                    continue
                raw = load_raw_trial(r["run_id"])
                prows = sj.load_raw(p)
                if not prows or not raw.get("t_run_end"):
                    continue
                last_sent = max(x["sent_at"] for x in prows)
                t_run_end = datetime.fromisoformat(raw["t_run_end"])
                gap = (t_run_end - last_sent).total_seconds()
                if gap > 15:  # probe 주기(~1초)보다 훨씬 크면 꼬리 소실 의심(임의 확정 아님, 점검 대상 표시용)
                    flagged.append((r["run_id"], round(gap, 1)))
    if flagged:
        print(f"t_run_end와 마지막 probe 기록 사이 격차 >15초인 trial(꼬리 소실 '미확인'으로 표시): {flagged}")
    else:
        print("모든 45건에서 마지막 probe 기록이 t_run_end와 15초 이내 - 꼬리 전체 소실 의심 사례 없음"
              "(단, 15초 이내 구간 자체의 미완료 개별 요청 유무는 여전히 원천적으로 기록이 없어 '미확인'임 - §137 한계 유지).")


# ======================================================================
# A-2. 탐지 시각이 실제로 '그 위반' 중이었는지 (t_slo~t_recovery 사이인지)
#      - §A에서 fixed_threshold rep2가 t_recovery보다 6분49초 늦게 탐지된
#      사실을 발견해 전수로 재확인한다. detection_source=predictive라는
#      기록이 '그 recovery_sec을 만든 사건'이라고 자동으로 가정하지 않는다.
# ======================================================================
def section_a2_detection_vs_episode_timing(out_core):
    print("\n" + "=" * 70)
    print("§A-2 정정 - load_ramp 탐지 시각이 실제로 그 위반 episode 안에 있었는지 전수 확인")
    print("=" * 70)
    print("detection_source=predictive 기록은 '최초 유효 탐지가 있었다'는 뜻일 뿐,")
    print("그 탐지가 TrialResult에 기록된 recovery_sec(첫 episode)의 원인이라는")
    print("보장이 아니다 - t_detection이 t_recovery보다 한참 뒤라면 서로 무관한")
    print("사건일 가능성이 높다(그 사이 다른 원인으로 자연 회복됐을 수 있음).\n")
    for arm in ("fixed_threshold", "proposed"):
        print(f"[load_ramp/{arm}]")
        for r in sorted(cell(out_core, "load_ramp", arm), key=lambda x: x["rep"]):
            raw = load_raw_trial(r["run_id"])
            t_det, t_slo, t_rec = raw.get("t_detection"), raw.get("t_slo"), raw.get("t_recovery")
            if not t_det or not t_slo:
                print(f"  rep{r['rep']}: 탐지없음 또는 위반없음 (recovery_sec={r['recovery_sec']})")
                continue
            t_det_dt, t_slo_dt = datetime.fromisoformat(t_det), datetime.fromisoformat(t_slo)
            t_rec_dt = datetime.fromisoformat(t_rec) if t_rec else None
            gap = round((t_det_dt - t_slo_dt).total_seconds(), 1)
            if t_rec_dt and t_slo_dt <= t_det_dt <= t_rec_dt:
                label = "DURING(위반 중 탐지 - 기여 가능성과 시간적으로 부합)"
            elif t_det_dt < t_slo_dt:
                label = "BEFORE(선제 탐지)"
            else:
                label = "AFTER-RECOVERY(위반 해소 후 탐지 - 그 recovery_sec과 무관할 가능성)"
            print(f"  rep{r['rep']}: recovery_sec={r['recovery_sec']} | t_slo~t_detection={gap}s | {label}")


def main():
    core_run_ids, out_core, aux_run_ids, out_aux = load_authoritative()
    assert len(core_run_ids) == 45 and len(aux_run_ids) == 5
    ra = section_a_violation_time(out_core)
    section_a2_detection_vs_episode_timing(out_core)
    section_b_conversion_denominators(out_core)
    section_c_failure_rate_breakdown(out_core)
    section_d_incomplete_check(out_core)
    Path("results/_correction_137.json").write_text(
        json.dumps({"violation_time": ra}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("\n\n중간 산출물 저장: results/_correction_137.json (정정 전용, 공식 결과 아님)")


if __name__ == "__main__":
    main()
