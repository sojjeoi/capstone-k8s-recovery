"""오프라인 세 정책 재생 (2026-10-23, 신호 단계 비교). 실제 조치를 바꾸는 재생이 아니다 - 같은 기록에서 '조치 후보'가 언제 생기는지만 본다.

정책(사전 고정):
- P0 기존: 내부 감지기 신호(동결 규칙: 3연속 이상·60 s 쿨다운) + 기존 Guard
- P1 SLO 확인: 내부 신호 + 실시간 SLO 위반 확정 + 기존 Guard. 신호는 이미 위반 확정 상태이면 즉시, 아니면 HOLD_SEC(60 s) 안에 위반이 확정될 때 그 확정 시각에 후보가 된다.
  유효기간이 지나면 폐기(재평가는 SLO 확정 시각에 한 번).
- P2 SLO 단독: 실시간 SLO 위반 확정 시각 + 기존 Guard
Guard(신호 신선도 15 s 등)와 정책 내부 상태는 재구성하지 않았으므로 '조치 가능'이 아니라 **조치 후보(신호 단계)** 로만 표시한다.
SLO 확정 시각은 동결 함수가 돌려주는 확정 표본의 observed_at(그 사실을 알 수 있었던 시각)이며, 위반 구간의 사후 시작 시각은 쓰지 않는다.
"""
import csv
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXP = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(EXP))
sys.path.insert(0, str(EXP / "phase2_network_env"))
sys.path.insert(0, str(EXP / "c3_normal_coverage"))
sys.path.insert(0, str(EXP.parent / "anomaly-detection"))
import hc_common as hc  # noqa: E402
import slo_basis as sb  # noqa: E402
import slo_judge  # noqa: E402

NET = EXP / "results" / "phase2_network_env"
DETS = {"cpu-threshold-c": "CPU", "net-probe-readiness": "net_probe"}


def policies(signals, ivs, t_inj=None):
    p0 = sorted(s for s in signals if t_inj is None or s >= t_inj)
    p1 = []
    for s in p0:
        hit = None
        for a, b in ivs:
            if a <= s <= b:
                hit = s
                break
            if 0 < (a - s).total_seconds() <= sb.HOLD_SEC:
                hit = a
                break
        if hit is not None:
            p1.append(hit)
    p2 = sorted(a for a, _ in ivs if t_inj is None or a >= t_inj)
    return p0, sorted(set(p1)), p2


def rel(t, t0):
    return round((t - t0).total_seconds(), 1) if (t is not None and t0 is not None) else None


def find_evidence(run, kind):
    pats = [NET.glob(f"c1-live-evidence-guarded_{kind}_record_only-{run}-*.jsonl")]
    for d in (hc.RESULTS / "main-g", hc.RESULTS / "main-g-r", *(hc.RESULTS / "g-runs").glob("*")):
        pats.append(d.glob(f"c1-live-evidence-guarded_pressure_act-{run}-*.jsonl"))
    for g in pats:
        for p in g:
            return p
    return None


def probe_csv_net(run, kind, rep):
    c = sorted((EXP / "results").glob(f"probe-guarded_{kind}_record_only-{run}-*-proposed-{rep}-raw.csv"))
    return c[0] if c else EXP / "results" / f"probe-missing-{run}.csv"


def signals_of(ev, run_id, det):
    import c3_common as cc
    import n2_selection as nsel
    evals = cc.read_evals(ev, run_id)
    return [x["t"] for x in nsel.replay_detector(det, evals, {}) if x["condition_met"]]


def main():
    sb.set_factor(2.0)
    out = []
    for i in (3, 5, 6, 7):                                          # 정상 세션: 신호가 SLO 확인으로 얼마나 걸러지는지
        run = f"n2n0{i}"
        v = json.loads((NET / f"p2-validity-{run}.json").read_text(encoding="utf-8"))
        ev = find_evidence(run, "normal")
        rows = slo_judge.load_raw(probe_csv_net(run, "normal", 6000 + i))
        pts = [p for p in slo_judge.evaluate(rows) if p["latency_evaluable"]]
        ivs = sb.confirmed_intervals(pts)
        for det, nm in DETS.items():
            p0, p1, p2 = policies(signals_of(ev, v["run_id"], det), ivs)
            out.append({"kind": "normal", "session": run, "detector": nm, "n_signals_P0": len(p0), "n_candidates_P1": len(p1),
                        "n_candidates_P2_slo_only": len(p2), "suppressed_by_slo_confirmation": len(p0) - len(p1),
                        "evaluable_min": round(len(pts) / 60.0, 1)})
    degr = []                                                        # 서비스 저하 세션
    for i in range(2, 7):
        run = f"n2p0{i}"
        v = json.loads((NET / f"p2-validity-{run}.json").read_text(encoding="utf-8"))
        degr.append((run, "F2류(Stage1 지연 주입)", v["run_id"], find_evidence(run, "pressure"), probe_csv_net(run, "pressure", 6100 + i),
                     sb.dts(v["t_injection_observed_utc"])))
    sess = json.loads(sorted((hc.RESULTS / "analysis").glob("sessions-*.json"))[-1].read_text(encoding="utf-8"))
    for r in sess:
        if r["arm"] == "G" and r.get("t0") and r.get("classification") != "harness_error" and (r.get("session_result") or {}).get("success") is not None:
            degr.append((r["run"], "F2" if r["fault"] == "f2" else "F3", r["run_id"], find_evidence(r["run"], "pressure"), sb.session_csv(r), sb.dts(r["t0"])))
    for run, kind, rid, ev, csvp, t0 in degr:
        if ev is None or not Path(csvp).exists():
            out.append({"kind": kind, "session": run, "note": "증거 또는 프로브 없음"})
            continue
        pts = [p for p in slo_judge.evaluate(slo_judge.load_raw(csvp)) if p["latency_evaluable"]]
        ivs = sb.confirmed_intervals(pts, t0)
        for det, nm in DETS.items():
            p0, p1, p2 = policies(signals_of(ev, rid, det), ivs, t0)
            out.append({"kind": kind, "session": run, "detector": nm, "t0": t0.isoformat(),
                        "P0_first_sec": rel(p0[0], t0) if p0 else None, "P1_first_sec": rel(p1[0], t0) if p1 else None,
                        "P2_slo_only_first_sec": rel(p2[0], t0) if p2 else None,
                        "P1_extra_delay_vs_P0_sec": round(rel(p1[0], t0) - rel(p0[0], t0), 1) if (p0 and p1) else None,
                        "P0_candidates": len(p0), "P1_candidates": len(p1), "P2_candidates": len(p2)})
    hc.jdump(sb.OUT / f"policy-replay-{sb.stamp()}.json", out)
    cols = sorted({k for r in out for k in r}, key=lambda k: (k not in ("kind", "session", "detector"), k))
    with open(sb.OUT / "policy_replay_table.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(out)
    print(len(out), "rows")


if __name__ == "__main__":
    main()
