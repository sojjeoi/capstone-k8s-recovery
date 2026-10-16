#!/usr/bin/env python3
"""net_mean 감지기의 정상 불필요 신호 분류(Phase 2 Gate 3와 같은 분류기, 오프라인 - 클러스터 접속 없음, 2026-10-17).

입력: network_detector_calibration.py가 저장한 calibration.json(임계값 T)과 series-*.json, 세션 유효성 파일.
분류: guarded_normal_bc_analysis.arm_outcome 그대로(판정 창 3600 s, 추적 600 s, 인프라 증거 창 규칙 포함).
  신호 시각 = 3번째 연속 이상 평가 시점(격자 시각). 기존 IF는 score_completed_at_utc를 쓴다(수 초 차이 가능).
임계값을 바꾸지 않는다. 결과는 보고용이다."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "anomaly-detection" / "v3" / "model_v31"))
import guarded_normal_bc_analysis as nab  # noqa: E402
import replay  # noqa: E402
from network_detector_calibration import C3, OUT, net_mean  # noqa: E402


def ts(s):
    return datetime.fromisoformat(s)


def context(v):
    b = v["base"]
    t0 = ts(b["t0_utc"])
    at = lambda s: t0 + timedelta(seconds=s)  # noqa: E731
    return {"t0": t0, "obs_end": ts(b["observation_end_utc"]),
            "episodes": [(at(e["t_slo_rel_sec"]), at(e["end_rel_sec"])) for e in (b.get("slo") or {}).get("episodes") or []],
            "service_events": [at(s) for s in b.get("service_events") or []],
            "infra_events": [at(e["t_rel_sec"]) for e in b.get("infra_events") or []]}


def classify_session(sid, T):
    pts = json.loads((OUT / f"series-{sid}.json").read_text(encoding="utf-8"))["points"]
    pm = net_mean(pts)
    r = replay.replay_detector([-v for _, v in pm], -T, consecutive_threshold=3, cooldown_sec=60.0, eval_interval_sec=15.0)
    sig = [datetime.fromtimestamp(pm[i][0], tz=timezone.utc) for i in r["signal_indices"]]
    ctx = context(json.loads((C3 / f"{sid}-validity.json").read_text(encoding="utf-8")))
    out = nab.arm_outcome(sig, ctx["t0"], obs_end=ctx["obs_end"], episodes=ctx["episodes"],
                          service_events=ctx["service_events"], infra_events=ctx["infra_events"])
    return {"raw_signals": len(sig), "in_decision_window": out["raw_trigger_count"], "class_counts": out["class_counts"],
            "signals": out["signals"], "t0": ctx["t0"].isoformat()}


def main():
    cal = json.loads((OUT / "calibration.json").read_text(encoding="utf-8"))
    T = cal["threshold_seconds"]
    res = {sid: classify_session(sid, T) for sid in cal["evaluation_sessions"]}
    tot = {c: sum(r["class_counts"].get(c, 0) for r in res.values()) for c in nab.CLASSES}
    out = {"threshold_seconds": T, "sessions": res, "total_class_counts": tot, "unnecessary_total": tot.get("unnecessary", 0),
           "note": "Gate 3 지표는 unnecessary 합계(동등 판정: 차이 ≤ 1). 임계값은 바꾸지 않았다."}
    (OUT / "gate3-classification.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(out, indent=2, ensure_ascii=False))


def loso():
    """개정 2 보정 방식의 정직한 오경보 추정(개발 단계): 세션을 하나씩 빼고 나머지로 임계값을 정한 뒤 빠진 세션에서 신호를 센다.
    c3 세션은 Gate 3 분류기로 분류하고, c1 세션은 분류 근거(유효성 파일)가 없어 원시 신호 수만 보고한다."""
    ids = {}
    for p in sorted(OUT.glob("series-c3*.json")):
        sid = p.stem.replace("series-", "")
        if json.loads((C3 / f"{sid}-validity.json").read_text(encoding="utf-8"))["status"] == "normal_valid":
            ids[sid] = p
    for p in sorted((OUT / "c1").glob("series-c1*.json")):
        ids[p.stem.replace("series-", "")] = p
    data = {k: net_mean(json.loads(p.read_text(encoding="utf-8"))["points"]) for k, p in ids.items()}
    rule = dict(consecutive_threshold=3, cooldown_sec=60.0, eval_interval_sec=15.0)
    rows = {}
    for held in data:
        rest = {k: [-v for _, v in m] for k, m in data.items() if k != held}
        cal = replay.calibrate_threshold(rest)
        T = -cal["threshold"]
        r = replay.replay_detector([-v for _, v in data[held]], -T, **rule)
        row = {"T_without": round(T, 4), "n_points": len(data[held]), "raw_signals": r["signal_count"],
               "max_net_mean": round(max(v for _, v in data[held]), 4)}
        if held.startswith("c3"):
            sig = [datetime.fromtimestamp(data[held][i][0], tz=timezone.utc) for i in r["signal_indices"]]
            ctx = context(json.loads((C3 / f"{held}-validity.json").read_text(encoding="utf-8")))
            o = nab.arm_outcome(sig, ctx["t0"], obs_end=ctx["obs_end"], episodes=ctx["episodes"],
                                service_events=ctx["service_events"], infra_events=ctx["infra_events"])
            row["unnecessary"] = o["class_counts"]["unnecessary"]
        rows[held] = row
    tot_raw = sum(r["raw_signals"] for r in rows.values())
    out = {"method": "leave-one-session-out(개발 단계 추정, 독립 검증 아님)", "sessions": rows,
           "sessions_with_signal": sorted(k for k, r in rows.items() if r["raw_signals"]),
           "raw_signals_total": tot_raw, "n_sessions": len(rows),
           "unnecessary_c3_total": sum(r.get("unnecessary", 0) for r in rows.values())}
    (OUT / "loso-rev2.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    loso() if "--loso" in sys.argv else main()
