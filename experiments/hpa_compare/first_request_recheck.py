"""'조치 후 새 Pod 가 처리한 첫 요청' 측정 재계산 (2026-10-23, 분석 전용: 서버·원자료·analyze.py 는 건드리지 않는다).

analyze.first_success_after 는 조치 후 '첫 표본'을 기준값으로 삼아, 그 표본에 이미 들어 있는 증가를 건너뛴다.
여기서는 조치(promote 호출) 직전 마지막 표본의 카운터를 기준값으로 보존하고, 그보다 늘어난 최초 표본을 찾는다.
- 결과는 시각이 아니라 구간 (직전 표본, 증가 확인 표본] 이다. 카운터엔 워밍업·준비 요청이 섞일 수 있고 관측 시각 != 요청 완료 시각이다.
- 표본 사이 결측이 있으면 구간이 그만큼 길어진다(max_gap_sec 에 기록). 일률 15초 오차로 표시하지 않는다.
"""
import csv
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import g_stage2  # noqa: E402
import hc_common as hc  # noqa: E402

OUT = hc.RESULTS / "analysis"


def recheck(run, pod, called):
    tl_path = g_stage2.results_dir_for(run) / f"timeline-{run}.jsonl"
    tl = hc.read_timeline(tl_path)
    pts = [(hc._dt(r["t"]), (r.get("counters") or {}).get(pod)) for r in tl]
    pts = [(t, c) for t, c in pts if c is not None]
    before = [(t, c) for t, c in pts if t < called]
    after = [(t, c) for t, c in pts if t >= called]
    if not before or not after:
        return {"run": run, "note": "조치 전 또는 후 표본 없음", "n_before": len(before), "n_after": len(after)}
    pt, pc = before[-1]
    hit = next(((t, c) for t, c in after if c > pc), None)
    gaps = [round((b[0] - a[0]).total_seconds(), 1) for a, b in zip([(pt, pc)] + after, after)]
    out = {"run": run, "pod": pod, "called": called.isoformat(), "pre_sample": pt.isoformat(), "pre_counter": pc,
           "pre_sample_to_called_sec": round((called - pt).total_seconds(), 1), "max_gap_sec": max(gaps) if gaps else None}
    if hit:
        out.update({"first_increase_sample": hit[0].isoformat(), "counter_at_sample": hit[1], "delta": hit[1] - pc,
                    "interval_start_sec_after_called": round((pt - called).total_seconds(), 1),
                    "interval_end_sec_after_called": round((hit[0] - called).total_seconds(), 1)})
    else:
        out["note"] = "관찰 구간에서 증가 없음"
    return out


def main():
    sess = json.loads(sorted((OUT).glob("sessions-*.json"))[-1].read_text(encoding="utf-8"))
    rows = []
    for r in sess:
        pr, ta = r.get("promotion") or {}, (r.get("t_action") or {}).get("value")
        if r.get("arm") != "G" or not pr.get("new_pod") or not ta or ta[:1] not in "0123456789":
            continue
        old = ((r.get("t_first_success") or {}).get("first_confirmed"))
        x = recheck(r["run"], pr["new_pod"], hc._dt(ta))
        x.update({"fault": r["fault"], "block": r["block"], "old_first_confirmed": old})
        if old and x.get("first_increase_sample"):
            x["old_minus_new_sec"] = round((hc._dt(old) - hc._dt(x["first_increase_sample"])).total_seconds(), 1)
        rows.append(x)
    hc.jdump(OUT / "first-request-recheck-20261023.json", rows)
    cols = sorted({k for x in rows for k in x})
    with open(OUT / "first-request-recheck-20261023.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    for x in rows:
        print({k: x.get(k) for k in ("run", "fault", "pre_counter", "counter_at_sample", "interval_start_sec_after_called",
                                      "interval_end_sec_after_called", "max_gap_sec", "old_minus_new_sec", "note")})


if __name__ == "__main__":
    main()
