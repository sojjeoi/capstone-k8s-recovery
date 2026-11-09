"""SLO 기준(정상 P95 0.324 s 의 2배, 30초 지속) 타당성 확인과 오프라인 세 정책 비교 (2026-10-23). 기존 기록만 읽는다.

- 동결 SLO 함수(experiments/slo_judge.py, slo_episodes.py)는 수정하지 않고 불러서 쓴다. 민감도 분석은 이 프로세스 안에서 slo_judge.LATENCY_THRESHOLD 값만
  바꿔 같은 함수를 다시 부른다(파일은 그대로).
- 사전 고정(결과를 보기 전에 정한 값): 비교 배수 1.8 / 2.0 / 2.2, 나머지(60초 창, 30초 지속, 가용성 99%)는 그대로.
  정책 재생의 신호 보류 유효기간 HOLD_SEC = 60 s(= SLO 창 길이), 재평가는 SLO 확정 시각마다 한 번.
- 정상 대비 위반 평가는 평가 가능(표본 20개 이상) 지점부터만 쓴다. 자료 누락·초기 구간을 정상으로 채우지 않는다.
- 실시간 확정 시각 = 동결 find_t_slo 가 돌려주는 확정 표본의 observed_at, 사후 구간 시작 = 위반 스트릭 첫 표본의 sent_at. 둘을 구분해서 기록한다.
"""
import csv
import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXP = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(EXP))
import hc_common as hc  # noqa: E402
import slo_episodes  # noqa: E402
import slo_judge  # noqa: E402

OUT = hc.RESULTS / "slo_basis"
RESULTS_ROOT = EXP / "results"
L0 = slo_judge.L_BASELINE
FACTORS = (1.8, 2.0, 2.2)
HOLD_SEC = 60.0
PERSIST = slo_judge.LATENCY_PERSIST_SEC


def set_factor(f):
    slo_judge.LATENCY_THRESHOLD = f * L0


def dts(x):
    return hc._dt(x) if isinstance(x, str) else x


# ---------------- 정상 구간 평가 ----------------
def latency_streaks(points):
    """latency 위반 연속 구간(사후): 시작 sent_at, 끝 sent_at, 30초 지속 충족 시 확정 시각(observed_at)."""
    out, cur = [], None
    for p in points:
        if p["latency_violating"]:
            if cur is None:
                cur = {"start": p["t"], "end": p["t"], "confirm": None}
            cur["end"] = p["t"]
            if cur["confirm"] is None and (p["t"] - cur["start"]).total_seconds() >= PERSIST:
                cur["confirm"] = p["observed_at"]
        elif cur is not None:
            out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    return out


def avail_groups(points):
    out, cur = [], None
    for p in points:
        if p["availability_violating"]:
            if cur is None:
                cur = {"start": p["t"], "end": p["t"]}
            cur["end"] = p["t"]
        elif cur is not None:
            out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    return out


def normal_stats(rows, label, group, factor):
    set_factor(factor)
    if len(rows) < 30:
        return {"label": label, "group": group, "factor": factor, "usable": False, "reason": "표본 부족"}
    pts = slo_judge.evaluate(rows)
    ev = [p for p in pts if p["latency_evaluable"]]
    if not ev:
        return {"label": label, "group": group, "factor": factor, "usable": False, "reason": "평가 가능 지점 없음"}
    dur = (ev[-1]["t"] - ev[0]["t"]).total_seconds()
    thr = factor * L0
    ls = [s for s in latency_streaks(ev) if s["confirm"] is not None]
    ag = avail_groups(pts)
    epi = slo_episodes.violation_episodes(rows, ev[0]["t"], ev[-1]["observed_at"], min_requests=1, max_gap_sec=1e9)
    p95s = [p["p95"] for p in ev]
    n_ep = len(epi.get("episodes") or []) if epi.get("computable") else None
    return {"label": label, "group": group, "factor": factor, "usable": True, "n_requests": len(rows), "n_failed": sum(not r["success"] for r in rows),
            "evaluable_sec": round(dur, 1), "window_p95_max": round(max(p95s), 3), "window_p95_median": round(statistics.median(p95s), 3),
            "window_p95_max_over_L0": round(max(p95s) / L0, 2), "frac_points_over_threshold": round(sum(p["latency_violating"] for p in ev) / len(ev), 4),
            "frac_single_requests_over_threshold": round(sum(r["latency"] > thr for r in rows) / len(rows), 4),
            "latency_episodes_30s": len(ls), "latency_episode_sec": round(sum((s["end"] - s["start"]).total_seconds() for s in ls), 1),
            "availability_episodes": len(ag), "final_slo_episodes": n_ep,
            "final_slo_cumulative_sec": epi.get("cumulative_sec") if epi.get("computable") else None,
            "episodes_per_hour": round(n_ep / (dur / 3600.0), 3) if (n_ep is not None and dur > 0) else None}


# ---------------- 저하 세션 ----------------
def confirmed_intervals(points, not_before=None):
    """실시간으로 '위반 확정' 상태였던 구간 [확정 observed_at, 해소 observed_at]. latency 는 30초 지속 뒤, availability 는 즉시."""
    ivs, streak, conf = [], None, None
    for p in points:
        if not_before is not None and p["t"] < not_before:
            continue
        if p["latency_violating"]:
            streak = streak or p["t"]
            if conf is None and (p["t"] - streak).total_seconds() >= PERSIST:
                conf = p["observed_at"]
        else:
            if conf is not None:
                ivs.append([conf, p["observed_at"]])
            streak, conf = None, None
        if p["availability_violating"]:
            ivs.append([p["observed_at"], p["observed_at"]])
    if conf is not None:
        ivs.append([conf, points[-1]["observed_at"]])
    return sorted(ivs)


def first_confirmation(points, t_inj):
    ivs = confirmed_intervals(points, t_inj)
    return ivs[0][0] if ivs else None


def session_csv(r):
    import run_timed_switch_recovery_trial as rt
    if r["arm"] == "G":
        return RESULTS_ROOT / f"probe-{r['run_id']}-{rt.ARM_LABEL}-{7300 + int(r['run'][3:])}-raw.csv"
    cand = list((hc.RESULTS / "main" / r["run_id"]).glob("probe-*raw.csv"))
    return cand[0] if cand else None


def stamp():
    return hc.now_iso()[:19].replace(":", "")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    R = RESULTS_ROOT
    normal_groups = {"기준 산정 자료(보정, in-sample)": sorted(R.glob("probe-calib-probe-only-20260918T09*-raw.csv")),
                     "정상 검증(네트워크 환경 n2n, 11초 프로필)": [R / f"probe-guarded_normal_record_only-n2n0{i}-capstone-phase2-network-env-v1-proposed-600{i}-raw.csv"
                                                           for i in (3, 5, 6, 7)]}
    rows_all = []
    for g, files in normal_groups.items():
        for f in files:
            if f.exists():
                rows = slo_judge.load_raw(f)
                for fac in FACTORS:
                    rows_all.append(normal_stats(rows, f.name[:70], g, fac))
    sess = json.loads(sorted((hc.RESULTS / "analysis").glob("sessions-*.json"))[-1].read_text(encoding="utf-8"))
    usable = [r for r in sess if r.get("classification") != "harness_error" and r.get("t0") and (r.get("slo") or {}).get("computable")]
    for r in usable:
        f = session_csv(r)
        if f and f.exists():
            pre = [x for x in slo_judge.load_raw(f) if x["sent_at"] < dts(r["t0"])]
            ready = slo_judge.find_baseline_ready(slo_judge.evaluate(pre)) if len(pre) >= 30 else None   # 하네스의 '안정 확인'(30초 무위반) 이후만: 기동 직후 과도 구간은 정상 집단이 아니다
            rows = [x for x in pre if ready and x["sent_at"] >= ready["ready_at"]]
            for fac in FACTORS:
                rows_all.append(normal_stats(rows, r["run_id"], "비교 세션 주입 전(안정 확인 이후, 짧음)", fac))
    set_factor(2.0)
    hc.jdump(OUT / f"normal-eval-{stamp()}.json", rows_all)
    cols = ["group", "label", "factor", "usable", "n_requests", "n_failed", "evaluable_sec", "window_p95_max", "window_p95_max_over_L0", "frac_points_over_threshold",
            "frac_single_requests_over_threshold", "latency_episodes_30s", "latency_episode_sec", "availability_episodes", "final_slo_episodes",
            "final_slo_cumulative_sec", "episodes_per_hour", "reason"]
    with open(OUT / "normal_false_positive_table.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows_all)
    sens = []
    for r in usable:
        f = session_csv(r)
        if not (f and f.exists()):
            continue
        rows = slo_judge.load_raw(f)
        t0, te = dts(r["t0"]), dts(r["observation_end"])
        for fac in FACTORS:
            set_factor(fac)
            pts = slo_judge.evaluate(rows)
            ep = slo_episodes.violation_episodes(rows, t0, te)
            fc = first_confirmation(pts, t0)
            sens.append({"fault": r["fault"], "block": r["block"], "arm": r["arm"], "run_id": r["run_id"], "factor": fac,
                         "cumulative_sec": ep.get("cumulative_sec"), "computable": ep.get("computable"),
                         "realtime_first_confirmation_sec_after_t0": round((fc - t0).total_seconds(), 1) if fc else None,
                         "posthoc_first_episode_start_sec_after_t0": round((dts(ep["episodes"][0]["t_slo"]) - t0).total_seconds(), 1) if ep.get("episodes") else None})
    set_factor(2.0)
    hc.jdump(OUT / f"sensitivity-{stamp()}.json", sens)
    with open(OUT / "sensitivity_table.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(sens[0].keys()))
        w.writeheader()
        w.writerows(sens)
    print(len(rows_all), "normal rows;", len(sens), "sensitivity rows")


if __name__ == "__main__":
    main()
