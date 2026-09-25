#!/usr/bin/env python3
"""§138 후속 비교 결과 정정 (이번 지시 §1~§5) - 읽기 전용, 기존 core 45건과
후속 첫 한 쌍의 원본 결과·state·CSV·evidence·cost·contract 파일은 전혀
건드리지 않는다. 새 판정 로직은 만들지 않는다 - slo_judge.evaluate()/
find_t_slo()/find_t_recovery()와 correct_137_errors.py의 find_all_episodes()/
raw_violating_seconds()를 그대로 재사용한다.

정정 대상(§138의 오류):
  1. detection_source=predictive가 recovery_sec의 원인이라고 가정한 것 -
     실제로는 두 trial 모두 t_detection이 t_recovery보다 한참(각각 약
     329.8초/176.1초) 뒤였다(이번 지시로 지적받아 재확인, §1).
  2. analyze_followup_results.py가 evidence 전체(pre-injection baseline
     포함)를 집계해 "900초 cohort"라고 잘못 부른 것 - 사전등록된
     t_injection<=sent_at<t_injection+900 경계를 명시적으로 적용한다(§2).
  3. stop() 경합으로 생긴 미완료 1건의 성격이 불분명했던 것 - 코드
     자체는 이미 별도로 고쳤고(probe_followup.py/load_ramp_followup_
     adapter.py의 stop-file+drain-wait), 여기서는 기존 evidence로 그
     1건이 실제로 무엇이었는지(회수 순간 진행중/취소확인/정상timeout/
     grace만료/원인불명 등) 판별한다(§3).
  4. 전체 900초 구간의 재위반 여부를 확인 안 한 것 - cohort 재구성 후
     find_all_episodes()를 그대로 재적용한다(§4).
  5. 비용 구간 경계(준비/관측/정리)를 구분 안 하고 "첫~마지막 표본 차이"
     로만 냈던 것 - phase_marks 기준으로 재분리한다(§5).
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import slo_judge as sj
from correct_137_errors import find_all_episodes, raw_violating_seconds
from followup_cost_sampler import reclassify_missed

FOLLOWUP_DIR = Path(__file__).parent / "results" / "followup"
COHORT_WINDOW_SEC = 900  # 사전등록 값(run_load_ramp_followup_trial.py TIMEOUT_SEC)과 동일, 독립 재확인용으로 여기 별도 상수로 고정


def _parse_ts(s):
    """ISO8601 timestamp 파싱 - 'Z' 접미사와 '+00:00' 오프셋을 모두 허용한다
    (사용자 지시 §2 - timezone 표현이 섞여도 안전해야 함). 전부 aware
    datetime으로 통일해 비교 시 naive/aware 혼용 오류를 원천 차단한다."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_evidence(run_id, arm, rep):
    p = FOLLOWUP_DIR / f"probe-{run_id}-{arm}-{rep}-evidence.jsonl"
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()] if p.exists() else []


TERMINAL_EVENTS = ("completed", "timeout", "error")


def build_cohort(evidence, t_injection, window_sec=COHORT_WINDOW_SEC):
    """사전등록된 t_injection<=sent_at<t_injection+window_sec 경계로 cohort를
    재구성한다. request_id 기준 - 완료 시각이 아니라 발신 시각으로 소속을
    정한다(구간 종료 뒤 완료돼도 같은 request_id로 계속 추적).

    반환 dict:
      in_window: {request_id: sent_event} - cohort 본체
      pre_window / post_window: 그 밖의 sent 이벤트(별도 집계, 버리지 않음)
      terminals_by_id: {request_id: [terminal_event, ...]} - 전체 evidence 기준
        (2개 이상이면 duplicate_terminal 후보)
      duplicate_sent / duplicate_terminal: 중복 발견된 request_id 집합
      terminal_only: sent 이벤트가 evidence 전체 어디에도 없는 terminal request_id
        (원인미상 - 정상이라면 존재할 수 없음, 있으면 계측 결함)
      unresolved_in_window: cohort 안인데 terminal이 하나도 없는 request_id
      resolved_in_window: cohort 안이고 terminal이 정확히 1개 이상 있는 request_id
        (판정 지표 계산엔 각 request_id의 "첫" terminal만 쓴다 - duplicate가
        있으면 별도로 경고하고, 그 request_id는 지표 계산에서 제외한다 -
        중복 자체가 계측 결함이므로 임의로 하나를 골라 통계에 섞지 않는다)
    """
    t_end = t_injection + timedelta(seconds=window_sec)
    sent_by_id = {}
    duplicate_sent = set()
    for e in evidence:
        if e.get("event") != "sent":
            continue
        rid = e["request_id"]
        if rid in sent_by_id:
            duplicate_sent.add(rid)
        else:
            sent_by_id[rid] = e

    terminals_by_id = {}
    for e in evidence:
        if e.get("event") in TERMINAL_EVENTS:
            terminals_by_id.setdefault(e["request_id"], []).append(e)
    duplicate_terminal = {rid for rid, evs in terminals_by_id.items() if len(evs) > 1}
    terminal_only = {rid for rid in terminals_by_id if rid not in sent_by_id}

    in_window, pre_window, post_window = {}, {}, {}
    for rid, e in sent_by_id.items():
        sent_at = _parse_ts(e["sent_at"])
        if sent_at < t_injection:
            pre_window[rid] = e
        elif sent_at >= t_end:
            post_window[rid] = e
        else:
            in_window[rid] = e

    unresolved_in_window = {rid for rid in in_window if rid not in terminals_by_id}
    resolved_in_window = {rid for rid in in_window if rid in terminals_by_id and rid not in duplicate_terminal}

    return {
        "t_injection": t_injection, "t_end": t_end, "window_sec": window_sec,
        "in_window": in_window, "pre_window": pre_window, "post_window": post_window,
        "terminals_by_id": terminals_by_id,
        "duplicate_sent": duplicate_sent, "duplicate_terminal": duplicate_terminal,
        "terminal_only": terminal_only,
        "unresolved_in_window": unresolved_in_window, "resolved_in_window": resolved_in_window,
    }


def cohort_summary(cohort):
    n_sent = len(cohort["in_window"])
    resolved = cohort["resolved_in_window"]
    n_resolved = len(resolved)
    fail = sum(1 for rid in resolved if not cohort["terminals_by_id"][rid][0].get("success"))
    over = sum(1 for rid in resolved
               if cohort["terminals_by_id"][rid][0].get("elapsed_monotonic_sec", 0) > sj.LATENCY_THRESHOLD)
    return {
        "n_sent_in_window": n_sent,
        "n_resolved_in_window": n_resolved,
        "n_unresolved_in_window": len(cohort["unresolved_in_window"]),
        "n_duplicate_terminal_excluded": len(cohort["duplicate_terminal"] & set(cohort["in_window"])),
        "n_pre_window_sent": len(cohort["pre_window"]),
        "n_post_window_sent": len(cohort["post_window"]),
        "n_terminal_only_anomaly": len(cohort["terminal_only"]),
        "n_duplicate_sent_anomaly": len(cohort["duplicate_sent"]),
        "failure_count": fail, "failure_rate": round(fail / n_resolved, 4) if n_resolved else None,
        "over_threshold_count": over, "over_threshold_rate": round(over / n_resolved, 4) if n_resolved else None,
        # 분모는 항상 발신 전체(n_sent_in_window) 기준으로도 같이 낸다 - 완료된 것만으로
        # 조용히 분모를 바꾸지 않는다(사용자 지시 §2).
        "failure_rate_of_all_sent": round(fail / n_sent, 4) if n_sent else None,
        "over_threshold_rate_of_all_sent": round(over / n_sent, 4) if n_sent else None,
    }


def classify_unresolved(cohort, evidence, arm):
    """§3 - cohort 안 unresolved 요청 각각을 분류한다:
    grace_expired_confirmed / terminal_missing_unexplained_process_may_have_been_killed /
    in_flight_at_fetch_no_grace_summary."""
    send_stopped = next((e for e in evidence if e.get("event") == "send_stopped"), None)
    unresolved_summary = next((e for e in evidence if e.get("event") == "unresolved_summary"), None)
    results = {}
    for rid in cohort["unresolved_in_window"]:
        if unresolved_summary is not None:
            # probe_followup.py 자신이 grace(60초) 만료 후 취소했다고 명시 기록한 경우만
            # "grace_expired_confirmed"로 확정한다 - 이 요약 이벤트가 없으면 함부로
            # grace 만료라고 단정하지 않는다(사용자 지시 §3).
            results[rid] = "grace_expired_confirmed"
        elif send_stopped is None:
            # probe_followup.py 자체가 정상 종료 로그(send_stopped)조차 안 남겼다 -
            # 프로세스가 강제 종료됐을 가능성이 높다(§3의 stop() 경합과 정확히 일치).
            results[rid] = "terminal_missing_unexplained_process_may_have_been_killed"
        else:
            results[rid] = "in_flight_at_fetch_no_grace_summary"
    return results


def analyze_arm_corrected(arm, rep=1):
    run_id = f"load_ramp-{arm}-{rep:02d}-post_hoc_followup-v1"
    trial_path = FOLLOWUP_DIR / f"trial-{run_id}.json"
    raw = json.loads(trial_path.read_text(encoding="utf-8"))
    t_injection = _parse_ts(raw["t_injection"])
    t_slo, t_recovery = _parse_ts(raw["t_slo"]), _parse_ts(raw["t_recovery"])
    t_detection = _parse_ts(raw["t_detection"]) if raw.get("t_detection") else None
    t_switch = _parse_ts(raw["t_switch"]) if raw.get("t_switch") else None

    evidence = load_evidence(run_id, arm, rep)
    cohort = build_cohort(evidence, t_injection)
    summ = cohort_summary(cohort)
    unresolved_class = classify_unresolved(cohort, evidence, arm)

    # §1 - 탐지/전환이 실제로 어디에 있었는지, 인과 주장 없이 시간관계만 기술.
    # 첫 episode(원본 TrialResult)만 보면 안 된다 - §4에서 재위반(2번째 episode)이
    # 실제로 있는지 먼저 확인한 뒤, 탐지·전환이 그 안에 있는지까지 봐야 한다.
    det_vs_recovery = (t_detection - t_recovery).total_seconds() if t_detection else None
    switch_vs_recovery = (t_switch - t_recovery).total_seconds() if t_switch else None

    # §4 - 재위반 확인. rolling 60초 P95 계산에는 주입 전 이력이 필요하므로
    # (원본 판정도 not_before를 streak-시작 자격에만 적용하고 window 내용
    # 자체는 안 자른다) pre_window+in_window를 함께 points 계산에 넣는다 -
    # 단 "판정 시작 자격"은 initial_not_before=t_injection으로 원본과 동일하게
    # 제한한다. post_window(주입+900초 이후 발신)는 사전등록 관측 기간 밖이라
    # 애초에 원본 판정이 본 적 없는 데이터이므로 넣지 않는다.
    # (최초 구현은 in_window만 넣었다가 원본 t_slo/t_recovery와 수 분 단위로
    # 어긋나는 걸 실측으로 발견 - 주입 직전 두 표본이 각각 1.52s/0.90s로
    # 심하게 느려서 초기 60초 window의 P95를 원본에서는 실제로 끌어올렸었다.)
    window_prows = []
    for rid, e in {**cohort["pre_window"], **cohort["in_window"]}.items():
        terms = cohort["terminals_by_id"].get(rid)
        if not terms or rid in cohort["duplicate_terminal"]:
            continue
        t = terms[0]
        window_prows.append({
            "sent_at": _parse_ts(e["sent_at"]),
            "latency": t.get("elapsed_monotonic_sec", 0.0),
            "success": bool(t.get("success")),
        })
    window_prows.sort(key=lambda r: r["sent_at"])
    points = sj.evaluate(window_prows) if window_prows else []
    episodes = find_all_episodes(points, initial_not_before=t_injection) if points else []
    # raw 위반시간은 사전등록 900초 cohort 안 point만으로 낸다(비교 계약의
    # 집계 구간 - pre_window의 2건은 window 계산엔 썼지만 '위반시간 합계'
    # 자체에는 포함하지 않는다, in_window인 point만 골라 다시 합산).
    in_window_points = [p for p in points if t_injection <= p["t"] < t_injection + timedelta(seconds=COHORT_WINDOW_SEC)]
    raw_viol = raw_violating_seconds(in_window_points) if in_window_points else {"raw_violating_sec": None, "eval_sec": None}

    # 첫 episode 재확인 - cohort로 다시 계산한 t_slo/t_recovery가 원본 TrialResult와 일치하는지도 확인
    first_ep_matches_original = None
    if episodes:
        first_ep_matches_original = (
            abs((episodes[0]["t_slo"] - t_slo).total_seconds()) < 2.0 and
            episodes[0]["t_recovery"] is not None and
            abs((episodes[0]["t_recovery"] - t_recovery).total_seconds()) < 2.0
        )

    # 탐지/전환이 '어느 episode에' 속하는지 - 첫 episode만 보고 "무관한 사후
    # 사건"이라 단정하면 안 된다(사용자 지시 §1) - 재위반 episode가 있으면
    # 그 안에도 들어가는지 반드시 확인한다.
    if t_detection is None:
        timing_relation = "탐지 없음"
    else:
        matched = None
        for i, ep in enumerate(episodes):
            ep_end = ep["t_recovery"] or datetime.max.replace(tzinfo=timezone.utc)
            if ep["t_slo"] <= t_detection <= ep_end:
                matched = i
                break
        if matched == 0:
            timing_relation = "DURING 첫 episode(위반~회복 사이 탐지 - 기여 가능성과 시간적으로 부합, 인과 미확정)"
        elif matched is not None:
            timing_relation = (f"DURING {matched+1}번째 episode(재위반 구간 안에서 탐지 - 그 재위반 해소에 대한 "
                                f"기여 가능성과 시간적으로 부합, 인과 미확정 - 원본 TrialResult의 recovery_sec은 "
                                f"이 재위반을 반영하지 않음에 유의)")
        elif t_detection < t_slo:
            timing_relation = "BEFORE 첫 episode(선제 - 기여 가능성과 시간적으로 부합, 인과 미확정)"
        else:
            timing_relation = (f"어느 episode에도 속하지 않음(회복 {det_vs_recovery:.1f}초 뒤 탐지, 확인된 "
                                f"{len(episodes)}개 episode 구간 밖 - 원인 불명, 인과 주장 안 함)")

    return {
        "run_id": run_id,
        "timing": {
            "t_injection": raw["t_injection"], "t_slo": raw["t_slo"], "t_recovery": raw["t_recovery"],
            "recovery_sec_first_episode": (t_recovery - t_slo).total_seconds(),
            "t_detection": raw.get("t_detection"), "detection_vs_recovery_sec": det_vs_recovery,
            "t_switch": raw.get("t_switch"), "switch_vs_recovery_sec": switch_vs_recovery,
            "timing_relation": timing_relation,
        },
        "cohort_900s": summ,
        "unresolved_classification": unresolved_class,
        "episodes_within_cohort": [
            {"t_slo": e["t_slo"].isoformat(), "t_recovery": e["t_recovery"].isoformat() if e["t_recovery"] else None,
             "open_at_observation_end": e["open_at_observation_end"]} for e in episodes
        ],
        "n_episodes_in_full_900s": len(episodes),
        "first_episode_matches_original_trialresult": first_ep_matches_original,
        "raw_violating_sec_in_cohort": raw_viol["raw_violating_sec"],
        "eval_sec_in_cohort": raw_viol["eval_sec"],
    }


# ======================================================================
# §5 - 비용 구간 재분류: 준비/관측/정리 경계 분리 + 수집실패 vs 정상종료 구분
# ======================================================================
def recompute_cost(run_id):
    """저장된 cost-{run_id}.json의 원본 samples/missed를 그대로 읽어(재수집
    없음) phase_marks 기준으로 준비/관측(injection~injection+900s)/정리
    구간을 나누고, reclassify_missed()로 '진짜 수집 실패'와 '프로세스 정상
    종료 후 꼬리'를 구분해 각 구간별 CPU·메모리를 다시 낸다. 경계 표본이
    없어 정확한 구간 비용을 못 내면(예: 그 구간에 표본이 0개) None으로
    표시하고 '실제 확보된 표본 구간 비용'만 별도로 보고한다 - 추정으로
    채우지 않는다."""
    cost = json.loads((FOLLOWUP_DIR / f"cost-{run_id}.json").read_text(encoding="utf-8"))
    marks = {k: _parse_ts(v) for k, v in cost["phase_marks"].items() if v}
    t_injection, t_run_end = marks["t_injection"], marks["t_run_end"]
    t_obs_end = t_injection + timedelta(seconds=COHORT_WINDOW_SEC)

    local = cost.get("local_detector")
    if not local or not local.get("samples"):
        return {"detector": None, "note": "detector 표본 없음"}

    samples = local["samples"]
    reclass = reclassify_missed(samples, local.get("missed", []))

    def phase_slice(lo, hi):
        return [s for s in samples if lo <= _parse_ts(s["t"]) < hi]

    phases = {
        "prep": phase_slice(marks.get("run_started_at", t_injection - timedelta(seconds=1)), t_injection),
        "observe_900s": phase_slice(t_injection, t_obs_end),
        "cleanup_after_900s": phase_slice(t_obs_end, t_run_end + timedelta(seconds=1)),
    }

    def phase_cost(pts):
        if len(pts) < 2:
            return {"n_samples": len(pts), "cpu_used_sec": None, "note": "경계 표본 부족(<2) - 구간 비용 계산 불가, 추정 안 함"}
        cpu_vals = [p["cpu_cumulative_sec"] for p in pts]
        rss_vals = [p["rss_bytes"] for p in pts]
        return {
            "n_samples": len(pts),
            "first_sample_at": pts[0]["t"], "last_sample_at": pts[-1]["t"],
            "cpu_used_sec": round(cpu_vals[-1] - cpu_vals[0], 3),
            "rss_bytes_mean": round(sum(rss_vals) / len(rss_vals)), "rss_bytes_max": max(rss_vals),
        }

    return {
        "detector": {
            "n_samples_total": len(samples),
            "n_genuine_missed(mid-run)": len(reclass["genuine_missed"]),
            "n_trailing_after_exit(정상종료 추정, 수집실패 아님)": len(reclass["trailing_after_exit"]),
            "phases": {k: phase_cost(v) for k, v in phases.items()},
            "full_span_cpu_used_sec(참고, 구간 미분리)": round(samples[-1]["cpu_cumulative_sec"] - samples[0]["cpu_cumulative_sec"], 3),
        }
    }


def main():
    print("=" * 70)
    print("§138 정정 - 900초 injection-anchored cohort 재구성 + 전체 구간 재위반 확인")
    print("=" * 70)
    out = {}
    for arm in ("fixed_threshold", "proposed"):
        r = analyze_arm_corrected(arm)
        out[arm] = r
        print(f"\n[{arm}] {r['run_id']}")
        t = r["timing"]
        print(f"  t_injection={t['t_injection']}")
        print(f"  첫 episode: t_slo={t['t_slo']} recovery_sec={t['recovery_sec_first_episode']:.3f}s")
        print(f"  탐지: t_detection={t['t_detection']} (회복 대비 {t['detection_vs_recovery_sec']}s) -> {t['timing_relation']}")
        print(f"  전환: t_switch={t['t_switch']} (회복 대비 {t['switch_vs_recovery_sec']}s)")
        c = r["cohort_900s"]
        print(f"  [900초 cohort] 발신={c['n_sent_in_window']} 완료={c['n_resolved_in_window']} "
              f"미완료={c['n_unresolved_in_window']} 중복terminal제외={c['n_duplicate_terminal_excluded']}")
        print(f"    창 밖: 사전={c['n_pre_window_sent']}건 사후={c['n_post_window_sent']}건 "
              f"| 이상: terminal-only={c['n_terminal_only_anomaly']} duplicate-sent={c['n_duplicate_sent_anomaly']}")
        print(f"    실패={c['failure_count']}/{c['n_resolved_in_window']}(완료기준 {c['failure_rate']}, "
              f"전체발신기준 {c['failure_rate_of_all_sent']}) "
              f"지연초과={c['over_threshold_count']}/{c['n_resolved_in_window']}(완료기준 {c['over_threshold_rate']}, "
              f"전체발신기준 {c['over_threshold_rate_of_all_sent']})")
        if r["unresolved_classification"]:
            print(f"  미완료 상세분류: {r['unresolved_classification']}")
        print(f"  전체 900초 안 episode 수: {r['n_episodes_in_full_900s']} "
              f"(첫 episode가 원본 TrialResult와 일치: {r['first_episode_matches_original_trialresult']})")
        print(f"  raw 위반시간(cohort 안): {r['raw_violating_sec_in_cohort']}s / 평가가능 {r['eval_sec_in_cohort']}s")

        cost = recompute_cost(r["run_id"])
        out[arm]["cost_corrected"] = cost
        if cost.get("detector"):
            d = cost["detector"]
            print(f"  [비용 재분류] 총표본={d['n_samples_total']} 진짜수집실패={d['n_genuine_missed(mid-run)']} "
                  f"정상종료후꼬리={d['n_trailing_after_exit(정상종료 추정, 수집실패 아님)']}")
            for phase, pc in d["phases"].items():
                print(f"    {phase}: {pc}")

    print("\n\n[비용 절대·상대 차이 - 관측(900s) 구간만]")
    for phase in ("observe_900s",):
        a = out["fixed_threshold"]["cost_corrected"]["detector"]["phases"][phase]
        b = out["proposed"]["cost_corrected"]["detector"]["phases"][phase]
        if a.get("cpu_used_sec") is not None and b.get("cpu_used_sec") is not None:
            diff = b["cpu_used_sec"] - a["cpu_used_sec"]
            ratio = round(b["cpu_used_sec"] / a["cpu_used_sec"], 2) if a["cpu_used_sec"] else None
            print(f"  CPU: fixed={a['cpu_used_sec']}s proposed={b['cpu_used_sec']}s "
                  f"절대차이={round(diff,3)}s 상대배율={ratio}x")
            mdiff = b["rss_bytes_mean"] - a["rss_bytes_mean"]
            mratio = round(b["rss_bytes_mean"] / a["rss_bytes_mean"], 2) if a["rss_bytes_mean"] else None
            print(f"  메모리 평균: fixed={a['rss_bytes_mean']/1e6:.1f}MB proposed={b['rss_bytes_mean']/1e6:.1f}MB "
                  f"절대차이={mdiff/1e6:.1f}MB 상대배율={mratio}x")

    (FOLLOWUP_DIR / "_correction_138.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n\n중간 산출물 저장: {FOLLOWUP_DIR}/_correction_138.json (정정 전용, 원본 미변경)")


if __name__ == "__main__":
    main()
