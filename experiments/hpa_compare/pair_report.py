"""요청 단위 보호 검증 C(무전환) 대 G(기존 자동 전환) 정리 (2026-10-23). 실행: python pair_report.py <C 폴더> <G 실행 ID>
출력: analysis/trace-pair-20261023/{request-table-C.csv, request-table-G.csv, pair-report.json, pair-timeline.png}
판정 규칙은 docs/design/PER-REQUEST-PROTECTION-CHECK-DRAFT-20261023.md §4 (결과를 보기 전에 고정).
"""
import csv
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import g_stage2  # noqa: E402
import hc_common as hc  # noqa: E402
import slo_policy_replay as spr  # noqa: E402
import trace_tools as tt  # noqa: E402

C_DIR, G_RUN = Path(sys.argv[1]), sys.argv[2]
OUT = hc.RESULTS / "analysis" / "trace-pair-20261023"
OUT.mkdir(parents=True, exist_ok=True)
dt = datetime.fromisoformat
THR = 0.648


def pct(v, p):
    v = sorted(v)
    return round(v[min(len(v) - 1, int(round(p * (len(v) - 1))))], 3) if v else None


def stats(rows):
    lat = [r["latency"] for r in rows if r.get("latency") is not None]
    return {"n": len(rows), "failed": sum(r["outcome"] != "ok" for r in rows), "median": round(statistics.median(lat), 3) if lat else None, "p95": pct(lat, 0.95),
            "over_0.648": sum(1 for x in lat if x > THR)}


# ---------- C ----------
cs = json.loads((C_DIR / "summary.json").read_text(encoding="utf-8"))
c_t0, c_te = dt(cs["outcome"]["t_injection_observed_utc"]), dt(cs["outcome"]["observation_ended_at_utc"])
c_ev = next((C_DIR / "trace").glob("probe-*-evidence.jsonl"))
c_rows = tt.request_table(c_ev, C_DIR / "trace" / "vllm-logs")
c_pod = cs["target"]["name"]
for r in c_rows:
    r["served_by"] = "target" if r.get("served_pod") == c_pod else ("unknown" if r.get("served_pod") is None else "other")
    r["t_rel"] = round((dt(r["sent_at"]) - c_t0).total_seconds(), 2)

# ---------- G ----------
gd = g_stage2.results_dir_for(G_RUN)
gs = next(gd.glob("summary-*.json"))
gsum = json.loads(gs.read_text(encoding="utf-8"))
to = gsum["trial_outcome"]
g_t0, g_te = dt(to["t_injection_observed_utc"]), dt(to["observation_ended_at_utc"])
act = next(a for a in ((gsum.get("policy_state_after") or {}).get("actions") or {}).values() if a.get("run_id") == gsum["run_id"])
sj = act.get("service_job") or {}
old_pod, new_pod = sj.get("old_pod"), sj.get("new_pod")
t_call = dt(act["promote_called_at"]) if act.get("promote_called_at") else None
gt = hc.RESULTS / "trace-pair" / f"g-{G_RUN}"
sel = [json.loads(x) for x in (gt / "watch-selector.jsonl").read_text(encoding="utf-8").splitlines()]
new_hash = new_pod.split("-")[-2] if new_pod else None
t_sel = next((dt(r["at"]) for r in sel if new_hash and new_hash in r["value"]), None)
g_ev = next(gt.glob("probe-*-evidence.jsonl"))
g_rows = tt.request_table(g_ev, gt / "vllm-logs")
for r in g_rows:
    p = r.get("served_pod")
    r["served_by"] = "unknown" if p is None else ("old" if p == old_pod else ("new" if p == new_pod else "other"))
    r["t_rel"] = round((dt(r["sent_at"]) - g_t0).total_seconds(), 2)
ev_path = next(gd.glob("c1-live-evidence-*.jsonl"))
sig = [t for t in spr.signals_of(ev_path, gsum["run_id"], "net-probe-readiness") if t >= g_t0]
t_sig = sig[0] if sig else None

# ---------- 시간축·비교 ----------
rel = lambda t: round((t - g_t0).total_seconds(), 1) if t else None  # noqa: E731
first_new = min((r for r in g_rows if r["served_by"] == "new"), key=lambda r: r["sent_at"], default=None)
last_old_after = max((r for r in g_rows if r["served_by"] == "old" and t_sel and dt(r["sent_at"]) >= t_sel), key=lambda r: r["sent_at"], default=None)
res = {"G_run": G_RUN, "G_run_id": gsum["run_id"], "old_pod": old_pod, "new_pod": new_pod,
       "timeline_sec_after_injection": {"injection": 0.0, "detector_signal(net-probe, first condition_met)": rel(t_sig), "promote_called": rel(t_call),
                                      "service_selector_changed(observed)": rel(t_sel),
                                      "first_request_served_by_new_pod(sent)": rel(dt(first_new["sent_at"])) if first_new else None,
                                      "last_request_sent_after_selector_served_by_old_pod": rel(dt(last_old_after["sent_at"])) if last_old_after else None},
       "selector_to_first_new_pod_request_sec": round((dt(first_new["sent_at"]) - t_sel).total_seconds(), 2) if (first_new and t_sel) else None}
res["whole_window"] = {"C": stats(c_rows), "G": stats(g_rows)}
if t_sel:
    cut = (t_sel - g_t0).total_seconds()
    g_before = [r for r in g_rows if 0 <= r["t_rel"] < cut]
    g_after = [r for r in g_rows if r["t_rel"] >= cut]
    res["split_at_selector_change"] = {"cut_sec_after_injection": round(cut, 1), "G_before": stats(g_before), "G_after": stats(g_after),
                                       "C_same_elapsed_before": stats([r for r in c_rows if 0 <= r["t_rel"] < cut]),
                                       "C_same_elapsed_after": stats([r for r in c_rows if r["t_rel"] >= cut]),
                                       "G_after_served_by": {k: sum(r["served_by"] == k for r in g_after) for k in ("old", "new", "other", "unknown")}}
try:
    csvp = next((HERE.parent / "results").glob(f"probe-{gsum['run_id']}-*-raw.csv"))
    slo_g = hc.slo_cumulative(csvp, to["t_injection_observed_utc"], to["observation_ended_at_utc"])
except Exception as e:  # noqa: BLE001
    slo_g = {"error": str(e)}
res["slo_cumulative_sec"] = {"C": cs["slo"].get("cumulative_sec"), "G": slo_g.get("cumulative_sec"),
                             "C_first_t_slo_rel": cs["slo"]["episodes"][0]["t_slo_rel_sec"],
                             "G_first_t_slo_rel": (slo_g.get("episodes") or [{}])[0].get("t_slo_rel_sec")}
res["classification_note"] = "G 에서 선택자 변경 전에 0.648초를 넘은 요청이 있으면 '피해 감소'로만 표기 (사전 등록 §4)"
(OUT / "pair-report.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
for nm, rows in (("C", c_rows), ("G", g_rows)):
    with open(OUT / f"request-table-{nm}.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=sorted({k for r in rows for k in r}))
        w.writeheader()
        w.writerows(rows)
print(json.dumps(res, ensure_ascii=False, indent=1, default=str))

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
matplotlib.rcParams["font.family"] = "Malgun Gothic"
matplotlib.rcParams["axes.unicode_minus"] = False
col = {"old": "tab:red", "new": "tab:green", "target": "tab:red", "unknown": "tab:gray", "other": "tab:purple"}
fig, axs = plt.subplots(2, 1, figsize=(11, 7), sharex=True, sharey=True)
for ax, rows, title in ((axs[0], c_rows, "C 무전환 — 장애를 받는 Pod 하나가 계속 처리"), (axs[1], g_rows, "G 기존 자동 전환 — 빨강=장애 Pod, 초록=전환 후 새 Pod")):
    for r in rows:
        if r.get("latency") is None:
            ax.scatter(r["t_rel"], 0.1, c="black", marker="x", s=25)
        else:
            ax.scatter(r["t_rel"], max(r["latency"], 0.1), c=col[r["served_by"]], s=9, alpha=.8)
    ax.axhline(THR, color="gray", ls=":", lw=1)
    ax.set_yscale("log")
    ax.set_title(title)
    ax.set_ylabel("요청 지연(초)")
for (t, lab, c), yy in zip(((t_sig, "①감지 신호", "tab:orange"), (t_call, "②승격 요청", "k"), (t_sel, "③Service 선택자 변경", "tab:blue")), (25, 9, 3.4)):
    if t:
        axs[1].axvline((t - g_t0).total_seconds(), color=c, ls="--", lw=1)
        axs[1].text((t - g_t0).total_seconds() + 3, yy, lab, fontsize=8, va="center", color=c)
axs[0].set_ylim(0.15, 40)
axs[1].set_xlabel("장애 주입 이후 경과 시간(초)")
fig.tight_layout()
fig.savefig(OUT / "pair-timeline.png", dpi=130)
