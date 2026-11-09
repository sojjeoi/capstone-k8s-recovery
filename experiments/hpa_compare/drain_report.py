"""드레인 검증 결과 정리 (2026-10-23): 한 실행 폴더(trace_inflight.py --probes 결과)에서 요청별 표·종료 시간축·그림을 만든다.
실행: python drain_report.py <폴더>   (출력: stream-table.csv, probe-table.csv, drain-report.json, drain-timeline.png)
"""
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import trace_tools as tt  # noqa: E402

D = Path(sys.argv[1])
S = json.loads((D / "summary.json").read_text(encoding="utf-8"))
dt = datetime.fromisoformat
iso_z = lambda s: dt(s.replace("Z", "+00:00"))  # noqa: E731
OLD, NEW = S["hashes"]["old_active"], S["hashes"]["new_preview"]
access = tt.load_access(D / "vllm-logs")
t_prom, t_ret = dt(S["events"]["promote_call_at"]), dt(S["events"]["promote_returned_at"])
sel = [json.loads(x) for x in (D / "watch-selector.jsonl").read_text(encoding="utf-8").splitlines()]
t_sel = next((dt(r["at"]) for r in sel if NEW in r["value"]), None)
role = lambda pod: "unknown" if pod is None else ("old" if OLD in pod else "new")  # noqa: E731

# --- 구 Pod 종료 시간축 (로그 + 시계열) ---
old_log = next(iter(D.glob(f"vllm-logs/*{OLD}*.log")), None)
lines = old_log.read_text(encoding="utf-8", errors="replace").splitlines() if old_log else []
sigterm = next((iso_z(l.split(" ")[0][:26] + "Z") for l in lines if "[shutdown] API server: shutdown triggered" in l), None)
shutdown_lines = [l[:230] for l in lines if "[shutdown]" in l or "Shutting down" in l or "Waiting for" in l][:14]
last_log = iso_z(lines[-1].split(" ")[0][:26] + "Z") if lines else None
gone_at, del_ts, present = None, None, False
for line in (D / "timeline.jsonl").read_text(encoding="utf-8").splitlines():
    r = json.loads(line)
    here = [p for p in (r.get("pods") or []) if p["hash"] == OLD]
    if here:
        present = True
        if here[0].get("deleting") and del_ts is None:
            del_ts = here[0]["deleting"]
    elif present and gone_at is None:
        gone_at = dt(r["t"])
res = {"promote_call_at": str(t_prom), "selector_changed_at": str(t_sel), "sigterm_at": str(sigterm), "old_pod_deletion_timestamp": del_ts,
       "old_pod_last_log_at": str(last_log), "old_pod_gone_observed_at": str(gone_at),
       "shutdown_log_lines": shutdown_lines}
t_inj = dt(S["events"]["injection_started_at"]) if S["events"].get("injection_started_at") else None
res["injection_started_at"] = str(t_inj)
res["injector_cleanup"] = S["events"].get("injector_cleanup")
if t_sel and gone_at:
    res["old_pod_retention_after_selector_sec"] = round((gone_at - t_sel).total_seconds(), 1)
if sigterm and gone_at:
    res["sigterm_to_gone_sec"] = round((gone_at - sigterm).total_seconds(), 1)


# --- 스트리밍 요청 ---
def stream_rows():
    ev = S["main_events"]
    sent = {e["request_id"]: e for e in ev if e["event"] == "sent"}
    out = []
    for e in (x for x in ev if x["event"] == "end"):
        pod, why = tt.attribute(e.get("client_ip"), e.get("client_port"), dt(sent[e["request_id"]]["sent_at"]), dt(e["at"]), access)
        # 저장된 outcome 대신 증거로 다시 분류: finish_reason(length/stop)이 있어야 정상 완료, [DONE] 만 있으면 절단, 그 밖은 중단
        cls = ("completed" if e["finish_reason"] in ("length", "stop") else "truncated_done") if (e["done_marker"] and e["status"] == 200) else e["outcome"]
        out.append({"max_tokens": e["max_tokens"], "sent_at": sent[e["request_id"]]["sent_at"], "end_at": e["at"], "outcome": cls,
                    "done_marker": e["done_marker"], "finish_reason": e["finish_reason"], "n_chunks": e["n_chunks"], "elapsed_sec": e["elapsed_sec"],
                    "error": (e["error"] or "")[:50], "served_by": role(pod), "basis": why})
    return sorted(out, key=lambda r: r["sent_at"])


streams = stream_rows()
res["streams"] = {"completed(finish_reason 있음)": sum(r["outcome"] == "completed" for r in streams),
                  "truncated_done([DONE]만 있고 절단)": sum(r["outcome"] == "truncated_done" for r in streams),
                  "interrupted(연결 끊김·오류·시간초과)": sum(r["outcome"] in ("interrupted", "timeout") for r in streams), "n": len(streams),
                  "per_request": [{"max_tokens": r["max_tokens"], "outcome": r["outcome"], "n_chunks": r["n_chunks"], "elapsed_sec": r["elapsed_sec"]} for r in streams]}
if gone_at is None and S["events"].get("old_pod_gone_observed_at"):     # 시계열 표본(10초)이 놓친 경우 시험 스크립트가 직접 관측한 시각을 쓴다
    gone_at = dt(S["events"]["old_pod_gone_observed_at"])
    res["old_pod_gone_observed_at"] = str(gone_at)
    if t_sel:
        res["old_pod_retention_after_selector_sec"] = round((gone_at - t_sel).total_seconds(), 1)
    if sigterm:
        res["sigterm_to_gone_sec"] = round((gone_at - sigterm).total_seconds(), 1)


# --- 고정/keepalive probe ---
def probe_rows(name, kind):
    p = D / f"{name}-evidence.jsonl"
    if not p.exists() or not p.read_text(encoding="utf-8").strip():
        return []
    rows = tt.request_table(p, D / "vllm-logs")
    for r in rows:
        r["kind"], r["served_by"] = kind, role(r.get("served_pod"))
        r["after_selector"] = bool(t_sel and dt(r["sent_at"]) >= t_sel)
    return rows


fc, ka = probe_rows("fc", "새 연결 probe"), probe_rows("ka", "keepalive probe")
for nm, rows in (("fixed_new_connection", fc), ("keepalive", ka)):
    after = [r for r in rows if r["after_selector"]]
    cnt = lambda sub, k: sum(1 for r in sub if r["served_by"] == k)  # noqa: E731
    res[nm] = {"n": len(rows), "after_selector_n": len(after), "after_selector_served_by": {k: cnt(after, k) for k in ("old", "new", "unknown")},
               "after_selector_failed": sum(r["outcome"] == "failed" for r in after), "failed_total": sum(r["outcome"] == "failed" for r in rows),
               "unresolved_total(증거 회수 시 진행 중이던 요청, 실패 아님)": sum(r["outcome"] == "unresolved" for r in rows),
               "over_0.648s_after_selector": sum(1 for r in after if r.get("latency") and r["latency"] > 0.648),
               "last_old_pod_request_after_selector_sent_at": max([r["sent_at"] for r in after if r["served_by"] == "old"], default=None),
               "first_failure": next(({"sent_at": r["sent_at"], "status": r.get("status"), "error": r.get("error")} for r in sorted(rows, key=lambda x: x["sent_at"]) if r["outcome"] == "failed"), None)}

# --- 처리 중 요청 수 기록 ---
pollp = D / "poll.jsonl"
poll = [json.loads(x) for x in pollp.read_text(encoding="utf-8").splitlines() if x.strip()] if pollp.exists() else []
old_poll = [p for p in poll if p["target"] == "old"]
res["poll"] = {"samples_old": len(old_poll), "errors_old": sum("err" in p for p in old_poll),
               "old_first_zero_running_after_sigterm": next((p["at"] for p in old_poll if sigterm and dt(p["at"]) >= sigterm and p.get("running") == 0), None)}
(D / "drain-report.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
for name, rows in (("stream-table.csv", streams), ("probe-table.csv", fc + ka)):
    if rows:
        with open(D / name, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=sorted({k for r in rows for k in r}))
            w.writeheader()
            w.writerows(rows)
print(json.dumps({k: v for k, v in res.items() if k != "shutdown_log_lines"}, ensure_ascii=False, indent=1, default=str))

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
matplotlib.rcParams["font.family"] = "Malgun Gothic"
matplotlib.rcParams["axes.unicode_minus"] = False
x = lambda t: (t - t_prom).total_seconds()  # noqa: E731
fig, (a1, a2) = plt.subplots(2, 1, figsize=(11, 7), sharex=True, gridspec_kw={"height_ratios": [1, 1]})
for i, r in enumerate(streams):
    ok = r["outcome"] == "completed" and r["done_marker"]
    a1.plot([x(dt(r["sent_at"])), x(dt(r["end_at"]))], [i, i], lw=7, color="tab:green" if ok else "tab:red")
    lab = "정상 완료" if ok else ("절단([DONE]만 받음)" if r["outcome"] == "truncated_done" else "중단")
    a1.text(x(dt(r["end_at"])) + 1, i, f"{r['max_tokens']}토큰 · {lab} ({r['n_chunks']}청크)", va="center", fontsize=8)
a1.set_yticks([])
a1.set_title(f"{D.name} — 긴 스트리밍 답변(초록=끝까지 도착, 빨강=중단)")
cmap = {"old": "tab:blue", "new": "tab:green", "unknown": "tab:gray"}
for rows, marker in ((fc, "o"), (ka, "^")):
    for r in rows:
        if r.get("latency") is not None:
            a2.scatter(x(dt(r["sent_at"])), r["latency"], c=cmap[r["served_by"]], marker=marker, s=14, alpha=.8)
        else:
            a2.scatter(x(dt(r["sent_at"])), 0.05, c="red", marker="x", s=30)
a2.axhline(0.648, color="gray", ls=":", lw=1)
a2.set_ylabel("고정 요청 지연(초)  ○새 연결 △keepalive\n파랑=구 Pod 초록=새 Pod 빨강x=실패")
a2.set_xlabel("승격 호출 기준 경과 시간(초)")
for ax in (a1, a2):
    for t, lab, c in ((t_inj, "장애 주입", "tab:purple"), (t_prom, "승격 호출", "k"), (t_sel, "선택자 변경", "tab:blue"), (sigterm, "구 Pod 종료 신호", "tab:orange"), (gone_at, "구 Pod 사라짐", "tab:red")):
        if t:
            ax.axvline(x(t), color=c, ls="--", lw=1)
            if ax is a1:
                ax.text(x(t), len(streams) - 0.4, lab, rotation=90, fontsize=8, va="top")
fig.tight_layout()
fig.savefig(D / "drain-timeline.png", dpi=130)
