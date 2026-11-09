"""무조치(W) F2 세션에서 '지연 기준을 넘기 전 시간 여유'가 있는지 보는 읽기 전용 분석 (2026-10-23).

사용자가 보는 프로브 요청 기록만 쓴다(W 타임라인에는 네트워크 readiness 지표가 없다). 주입 시각 t_inj 기준으로:
- 주입 직전 60초 요청 지연 중앙값 vs 주입 후 첫 요청들 (주입 직후부터 이미 느린가, 점진적인가)
- 첫 기준 초과 요청(지연 > 2*L0) 의 도착 시각(sent+latency), 동결 SLO 의 실시간 위반 확정 시각
- 그 사이에 이미 기준을 넘은 요청 수 (= 'SLO 확정 전에도 느린 요청은 발생한다')
임계 배수는 2.0 하나만 쓴다(새 임계 탐색 없음). 서버·원자료·동결 소스는 건드리지 않는다.
"""
import csv
import datetime
import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import hc_common as hc  # noqa: E402
import slo_basis as sb  # noqa: E402
import slo_judge  # noqa: E402

OUT = hc.RESULTS / "analysis"


def obs(r):
    return r["sent_at"] + datetime.timedelta(seconds=r["latency"])


def one(d):
    s = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    ev = s.get("events") or {}
    t0 = sb.dts(ev.get("t_injection"))
    rows = slo_judge.load_raw(next(d.glob("probe-*raw.csv")))
    thr = 2.0 * slo_judge.L_BASELINE
    sb.set_factor(2.0)
    pre = [r["latency"] for r in rows if 0 <= (t0 - r["sent_at"]).total_seconds() <= 60]
    post = [r for r in rows if r["sent_at"] >= t0]
    over = [r for r in post if r["latency"] > thr or not r["success"]]
    pts = slo_judge.evaluate(rows)
    conf = sb.first_confirmation(pts, t0)
    first_win = next((p for p in pts if p["t"] >= t0 and p["latency_evaluable"] and p["latency_violating"]), None)
    rel = lambda t: round((t - t0).total_seconds(), 1) if t else None  # noqa: E731
    return {"run_id": s["run_id"], "arm": s["arm"], "fault": s["fault"], "block": s["block"], "classification": s.get("classification"),
            "pre60_median_latency": round(statistics.median(pre), 3) if pre else None, "pre60_n": len(pre),
            "first_post_latencies": [round(r["latency"], 2) for r in post[:5]],
            "first_over_request_arrival_sec": rel(obs(over[0])) if over else None,
            "first_over_request_sent_sec": rel(over[0]["sent_at"]) if over else None,
            "first_window_p95_over_sec": rel(first_win["observed_at"]) if first_win else None,
            "slo_confirm_sec": rel(conf),
            "over_requests_before_slo_confirm": len([r for r in over if conf and obs(r) <= conf]),
            "requests_completed_before_slo_confirm": len([r for r in post if conf and obs(r) <= conf]),
            "threshold_sec": round(thr, 3)}


def main():
    rows = []
    for d in sorted((hc.RESULTS / "main").glob("hc-f2-[wh]-b*")):
        try:
            rows.append(one(d))
        except Exception as e:  # noqa: BLE001
            rows.append({"run_id": d.name, "error": str(e)})
    hc.jdump(OUT / "precursor-margin-20261023.json", rows)
    cols = sorted({k for x in rows for k in x})
    with open(OUT / "precursor-margin-20261023.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    for x in rows:
        print(x)


if __name__ == "__main__":
    main()
