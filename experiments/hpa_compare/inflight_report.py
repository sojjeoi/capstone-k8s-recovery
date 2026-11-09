"""진행 중 요청 점검 결과 정리 (2026-10-23): 요청별 표(CSV)와 시간축 그림(PNG). 실행: python inflight_report.py <trace/inflight-... 폴더>"""
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
access = tt.load_access(D / "vllm-logs")
t_prom, t_ret = dt(S["events"]["promote_call_at"]), dt(S["events"]["promote_returned_at"])
sel = [dt(json.loads(x)["at"]) for x in (D / "watch-selector.jsonl").read_text(encoding="utf-8").splitlines()]
t_sel = sel[-1] if len(sel) > 1 else None
old_del = None
for line in (D / "timeline.jsonl").read_text(encoding="utf-8").splitlines():
    r = json.loads(line)
    for p in r.get("pods") or []:
        if p["hash"] == S["hashes"]["old_active"] and p.get("deleting") and old_del is None:
            old_del = p["deleting"]
old_log_end = None
for pth in D.glob("vllm-logs/*" + S["hashes"]["old_active"] + "*.log"):
    last = pth.read_text(encoding="utf-8", errors="replace").splitlines()[-1]
    old_log_end = last.split(" ")[0]


def role(pod):
    return None if pod is None else ("구 Pod(전환 전 활성)" if S["hashes"]["old_active"] in pod else "새 Pod(예비였다가 승격)")


def rows_of(ev, kind):
    sent = {e["request_id"]: e for e in ev if e["event"] == "sent"}
    out = []
    for e in (x for x in ev if x["event"] == "end"):
        rid = e["request_id"]
        pod, why = tt.attribute(e.get("client_ip"), e.get("client_port"), dt(sent[rid]["sent_at"]), dt(e["at"]), access)
        out.append({"kind": kind, "request_id": rid, "max_tokens": e["max_tokens"], "sent_at": sent[rid]["sent_at"], "end_at": e["at"], "outcome": e["outcome"],
                    "error": (e["error"] or "")[:60], "n_chunks_received": e["n_chunks"], "elapsed_sec": e["elapsed_sec"], "served_pod": pod, "served_by": role(pod), "basis": why})
    return out


rows = rows_of(S["pre"]["preview"], "승격 전 예비 서비스") + rows_of(S["pre"]["active"], "승격 전 활성 서비스") + rows_of(S["main_events"], "긴 스트리밍")
with open(D / "request-table.csv", "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
ev = {"promote_call_at": str(t_prom), "promote_returned_at": str(t_ret), "service_selector_changed_observed_at": str(t_sel),
      "old_pod_deletion_timestamp": old_del, "old_pod_last_log_line_at": old_log_end,
      "progress_before_promote_chunks": S["events"]["progress_before_promote"]}
(D / "events.json").write_text(json.dumps(ev, ensure_ascii=False, indent=1), encoding="utf-8")
for r in rows:
    print(r["kind"], r["max_tokens"], r["outcome"], r["n_chunks_received"], r["elapsed_sec"], r["served_by"], "|", r["basis"])
print(json.dumps(ev, ensure_ascii=False, indent=1))

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
matplotlib.rcParams["font.family"] = "Malgun Gothic"
matplotlib.rcParams["axes.unicode_minus"] = False
fig, ax = plt.subplots(figsize=(10, 4))
main = [r for r in rows if r["kind"] == "긴 스트리밍"]
for i, r in enumerate(main):
    a, b = (dt(r["sent_at"]) - t_prom).total_seconds(), (dt(r["end_at"]) - t_prom).total_seconds()
    ax.plot([a, b], [i, i], lw=6, color="tab:red" if r["outcome"] != "completed" else "tab:green")
    ax.text(b + 1, i, f"{r['outcome']} ({r['n_chunks_received']}청크, {r['served_by']})", va="center", fontsize=8)
marks = [(t_prom, "승격 호출", "k"), (t_sel, "Service 선택자 변경 관측", "tab:blue")]
if old_del:
    marks.append((dt(old_del.replace("Z", "+00:00")), "구 Pod 종료 시작", "tab:orange"))
for t, lab, c in marks:
    if t:
        x = (t - t_prom).total_seconds()
        ax.axvline(x, color=c, ls="--")
        ax.text(x, len(main) - 0.4, lab, rotation=90, fontsize=8, va="top")
ax.set_yticks(range(len(main)))
ax.set_yticklabels([f"요청 {r['max_tokens']}토큰" for r in main])
ax.set_xlabel("승격 호출 기준 경과 시간(초)")
ax.set_title("전환 시점에 생성 중이던 요청의 결과 (장애 없음, 기능 검증)")
fig.tight_layout()
fig.savefig(D / "inflight-timeline.png", dpi=130)
