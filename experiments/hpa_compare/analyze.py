"""HPA 비교 분석 (H/W/G). 기존 기록만 읽고 새 종합점수·금액 환산·유의성 검정은 만들지 않는다. 결과는 analysis/ 폴더에 새 파일로 쓴다.

세션별 값:
- SLO 위반 누적(기존 정의: slo_episodes.violation_episodes r2 의 cumulative_sec), 미회복 여부
- Pod 별 요청 증가량, 예약 자원 유지량(요청량 × Pod 유지 시간), 실제 사용량(metrics-server)
- 시각: t0(실제 장애·부하 시작), t_action(G 승격 요청 / H HPA 확장 결정), t_first_success(전환 후 새 Pod·추가 Pod 가 처리한 첫 요청),
  t_slo_recovered(기존 정의에 따른 SLO 지속 회복 구간 시작). 정확한 시각이 없으면 관측 구간·최초 확인 시각으로 표시하고 종류를 적는다.
- 구간: t0→t_first_success, t_action→t_first_success, t0→t_slo_recovered
확보 못 한 값은 "미확인", 관찰 종료까지 일어나지 않은 사건은 "미발생", W 의 확장·전환은 "해당 없음".
"""
import csv
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hc_common as hc  # noqa: E402

OUT = hc.RESULTS / "analysis"
NA, UNK, NEVER = "해당 없음", "미확인", "미발생"


def dt(s):
    return hc._dt(s) if s else None


def secs(a, b):
    return round((b - a).total_seconds(), 1) if a and b else None


def first_success_after(timeline, pod, t_after, ready_since=None):
    """pod 의 vLLM 요청 카운터가 t_after(이후) 처음 늘어난 표본. 카운터는 약 15초 간격이라 정확한 첫 요청 시각이 아니라
    (직전 표본, 이 표본] 구간이며, 준비·워밍업 요청과 구분하려고 Ready 이후 첫 표본을 기준값으로 쓴다."""
    base, prev_t = None, None
    start = max([x for x in (t_after, ready_since) if x])
    for rec in timeline:
        ts = dt(rec["t"])
        c = (rec.get("counters") or {}).get(pod)
        if c is None:
            continue
        if ts < start:
            prev_t = ts
            continue
        if base is None:
            base, prev_t = c, ts
            continue
        if c > base:
            return {"interval_start": prev_t.isoformat(), "first_confirmed": ts.isoformat(), "kind": "pod_request_counter_interval"}
        prev_t = ts
    return None


def slo_times(slo):
    eps = (slo or {}).get("episodes") or []
    if not (slo or {}).get("computable"):
        return {"status": UNK}
    if not eps:
        return {"status": "위반 없음", "cumulative_sec": slo.get("cumulative_sec")}
    last = eps[-1]
    unrecovered = bool(slo.get("cumulative_lower_bound")) or last.get("t_recovery") is None
    return {"status": "미회복(관찰 종료까지)" if unrecovered else "회복", "first_violation": eps[0].get("t_slo"),
            "first_recovery_start": eps[0].get("t_recovery"), "final_recovery_start": None if unrecovered else last.get("t_recovery"),
            "episodes": len(eps), "cumulative_sec": slo.get("cumulative_sec")}


def row_hw(summary_path):
    s = json.loads(Path(summary_path).read_text(encoding="utf-8"))
    d = Path(summary_path).parent
    tl = hc.read_timeline(d / "timeline.jsonl")
    ev = s.get("events") or {}
    t0 = dt(ev.get("t_injection"))
    r = {"arm": s["arm"], "fault": s["fault"], "block": s["block"], "run_id": s["run_id"], "classification": s["classification"],
         "harness_problems": s.get("harness_problems"), "t0": ev.get("t_injection"), "observation_end": ev.get("observation_end"),
         "slo": s.get("slo"), "cost": s.get("cost"), "usage": s.get("usage"), "request_increase": s.get("request_increase"), "events": ev,
         "target": (s.get("target") or {}).get("name")}
    if s["arm"] == "W":
        r["t_action"] = {"value": NA}
        r["t_first_success"] = {"value": NA}
        return r, tl
    act, kind = None, None
    for rec in tl:
        h = rec.get("hpa") or {}
        if t0 and dt(rec["t"]) >= t0 and h.get("last_scale") and dt(h["last_scale"]) >= t0:
            act, kind = h["last_scale"], "HPA 상태의 lastScaleTime"
            break
    if act is None and ev.get("hpa_scale_decision"):
        act, kind = ev["hpa_scale_decision"], "replicas 변경 최초 관측(10초 주기 표본)"
    r["t_action"] = {"value": act, "kind": kind} if act else {"value": NEVER}
    extra, rs = ev.get("extra_pod"), ev.get("extra_pod_ready")
    fs = first_success_after(tl, extra, t0, dt(rs)) if extra else None
    r["extra_pod"] = {"name": extra, "created": ev.get("extra_pod_created"), "ready": rs}
    r["t_first_success"] = fs if fs else {"value": NEVER if not extra else UNK}
    return r, tl


def row_g(run, fault, block):
    import run_guarded_pilot as gp
    import run_timed_switch_recovery_trial as rt
    import g_stage2
    d = g_stage2.results_dir_for(run)
    sess = d / f"p2-s2-session-{run}.json"
    if not sess.exists():
        return None, []
    rec = json.loads(sess.read_text(encoding="utf-8"))
    rid = rec["run_id"]
    sm = json.loads((d / f"summary-{rid}.json").read_text(encoding="utf-8"))
    tl_path = d / f"timeline-{run}.jsonl"
    tl = hc.read_timeline(tl_path) if tl_path.exists() else []
    to = sm.get("trial_outcome") or {}
    t0, te = to.get("t_injection_observed_utc"), to.get("observation_ended_at_utc")
    csvp = Path(gp.PROBE_RESULTS_DIR) / f"probe-{rid}-{rt.ARM_LABEL}-{7300 + int(run[3:])}-raw.csv"
    slo = hc.slo_cumulative(csvp, t0, te) if (csvp.exists() and t0 and te) else None
    act = None
    for a in ((sm.get("policy_state_after") or {}).get("actions") or {}).values():
        if a.get("run_id") == rid:
            act = a
    r = {"arm": "G", "fault": fault, "block": block, "run_id": rid, "run": run, "t0": t0, "observation_end": te, "slo": slo,
         "session_result": rec.get("result"), "cleanup_errors": rec.get("cleanup_errors"), "outcome_status": rec.get("outcome_status")}
    if act:
        sj = act.get("service_job") or {}
        called = act.get("promote_called_at")
        r["t_action"] = {"value": called, "kind": "정책 promote 호출 시각(promote_called_at)"} if called else {"value": NEVER}
        r["promotion"] = {"status": act.get("status"), "verified_at": sj.get("verified_at"), "new_pod": sj.get("new_pod"), "old_pod": sj.get("old_pod")}
        newp = sj.get("new_pod")
        fs = first_success_after(tl, newp, dt(called)) if (newp and called) else None
        r["t_first_success"] = fs if fs else {"value": UNK}
    else:
        r["t_action"] = {"value": NEVER}
        r["t_first_success"] = {"value": NEVER}
        r["promotion"] = None
    if tl and t0 and te:
        from run_hw import usage_stats
        ti, tend = dt(t0), dt(te)
        w0 = ti - timedelta(seconds=hc.STAND_SEC)
        pods = hc.pod_seconds(tl, w0, tend)
        r["cost"] = {"window": [w0.isoformat(), tend.isoformat()], "pods": pods, **hc.cost_from(pods)}
        r["usage"] = usage_stats(tl, w0, tend)
        r["request_increase"] = hc.counter_delta(tl, ti, tend)
    return r, tl


def add_durations(r):
    t0 = dt(r.get("t0"))
    av = (r.get("t_action") or {}).get("value")
    ta = dt(av) if isinstance(av, str) and av not in (NA, NEVER, UNK) else None
    fs = r.get("t_first_success") or {}
    tf = dt(fs.get("first_confirmed")) if fs.get("first_confirmed") else None
    st = slo_times(r.get("slo"))
    r["slo_recovery"] = st
    rec_t = dt(st.get("final_recovery_start")) if st.get("final_recovery_start") else None
    to_rec = secs(t0, rec_t) if rec_t else st["status"]
    if r["arm"] == "W":
        r["durations"] = {"t0_to_first_success": NA, "action_to_first_success": NA, "t0_to_slo_recovered": to_rec}
        return
    miss = NEVER if fs.get("value") == NEVER else UNK
    r["durations"] = {"t0_to_first_success": secs(t0, tf) if tf else miss,
                      "action_to_first_success": secs(ta, tf) if (ta and tf) else miss,
                      "t0_to_slo_recovered": to_rec,
                      "note": "first_success 는 (직전 표본, 최초 확인] 구간의 끝 시각이라 최대 약 15초의 오차가 있다",
                      "t0_to_first_success_lower_bound": secs(t0, dt(fs["interval_start"])) if fs.get("interval_start") else None}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for sp in sorted((hc.RESULTS / "main").glob("*/summary.json")):
        r, _ = row_hw(sp)
        rows.append(r)
    import orchestrate as oc
    for (fault, block, arm) in oc.SEQ:
        if arm != "G":
            continue
        for run in (oc.G_RUN[(fault, block)], oc.G_RERUN[(fault, block)]):
            r, _ = row_g(run, fault, block)
            if r:
                rows.append(r)
    for r in rows:
        add_durations(r)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (OUT / f"sessions-{stamp}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    cols = ["fault", "block", "arm", "run_id", "classification", "slo_cumulative_sec", "slo_status", "pod_alive_sec", "cpu_req_core_sec", "mem_req_gib_sec",
            "t0", "t_action", "t_first_success_confirmed", "t0_to_first_success", "action_to_first_success", "t0_to_slo_recovered"]
    with open(OUT / f"sessions-{stamp}.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in sorted(rows, key=lambda x: (x["fault"], x["block"], x["arm"], x["run_id"])):
            c, d = r.get("cost") or {}, r.get("durations") or {}
            cls = r.get("classification") or ("valid" if (r.get("session_result") or {}).get("success") is not None and not r.get("cleanup_errors") else "check")
            w.writerow([r["fault"], r["block"], r["arm"], r["run_id"], cls, (r.get("slo") or {}).get("cumulative_sec"),
                        (r.get("slo_recovery") or {}).get("status"), c.get("pod_alive_sec_total"), c.get("cpu_request_core_sec"),
                        c.get("memory_request_gib_sec"), r.get("t0"), (r.get("t_action") or {}).get("value"),
                        (r.get("t_first_success") or {}).get("first_confirmed"), d.get("t0_to_first_success"), d.get("action_to_first_success"),
                        d.get("t0_to_slo_recovered")])
    try:
        figures(rows, stamp)
    except Exception as e:  # noqa: BLE001
        print("그림 생성 실패:", type(e).__name__, e)
    print(f"{len(rows)} 세션 분석 -> {OUT}")


def figures(rows, stamp):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.family"] = "Malgun Gothic"
    plt.rcParams["axes.unicode_minus"] = False
    colors = {"H": "#d95f02", "W": "#7570b3", "G": "#1b9e77"}
    for fault, arms in (("f2", ["H", "W", "G"]), ("f3", ["H", "G"])):
        sel = [r for r in rows if r["fault"] == fault and r.get("slo") and r.get("classification") != "harness_error"]
        if not sel:
            continue
        blocks = sorted({r["block"] for r in sel})
        fig, axes = plt.subplots(1, 3, figsize=(15, 4.2))
        w = 0.8 / len(arms)
        for ai, arm in enumerate(arms):
            xs = [b + (ai - (len(arms) - 1) / 2) * w for b in blocks]
            for ax, key, label in ((axes[0], lambda r: (r.get("slo") or {}).get("cumulative_sec"), "SLO 위반 누적(초)"),
                                   (axes[1], lambda r: (r.get("cost") or {}).get("cpu_request_core_sec"), "CPU 요청량 x Pod 유지 시간(코어·초)"),
                                   (axes[2], lambda r: (r.get("durations") or {}).get("t0_to_slo_recovered"), "t0 -> SLO 지속 회복(초)")):
                ys = []
                for b in blocks:
                    m = [r for r in sel if r["arm"] == arm and r["block"] == b]
                    v = key(m[0]) if m else None
                    ys.append(v if isinstance(v, (int, float)) else 0)
                    if not m:
                        ax.text(b + (ai - (len(arms) - 1) / 2) * w, 0, "미완료(환경)", ha="center", va="bottom", fontsize=8, color="#555")
                ax.bar(xs, ys, width=w * 0.9, color=colors[arm], label=arm)
                ax.set_title(label, fontsize=10)
                ax.set_xticks(blocks)
                ax.set_xlabel("block")
        axes[0].legend()
        fig.suptitle(f"{'F2 네트워크 지연: H(HPA) / W(상시 2개) / G(예비 Pod 전환)' if fault == 'f2' else 'F3 요청 폭주: H(HPA) / G(예비 Pod 전환)'}  (블록당 1세션, 미완료는 표시)")
        fig.tight_layout()
        fig.savefig(OUT / f"compare-{fault}-{stamp}.png", dpi=130)
        plt.close(fig)


if __name__ == "__main__":
    main()
