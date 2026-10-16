#!/usr/bin/env python3
"""kubelet readiness probe 소요 시간 지표(net_probe) 평가 - 개정 2와 같은 절차(보정 → leave-one-out 오경보 → 파일럿 반응), 읽기 전용(2026-10-17).

지표   rate(prober_probe_duration_seconds_sum)/rate(..._count) [60s], probe_type=Readiness, container=vllm,
       scrape_duration_seconds{job="vllm-active"}가 있는 Pod(= active Pod)만. vLLM의 /metrics 처리와 무관한 kubelet→Pod 경로.
tick 값 = 15초 격자 시점의 위 60초 비율(이미 60초 창 평균이라 추가 평균 없음). 값이 없는 tick(창 안 probe 0회)은 0.0으로 채워 연속을 끊는다(보수적).
규칙   값 > T 가 3회 연속이면 신호, 쿨다운 60초, 간격 15초. 보정 = replay.calibrate_threshold(점수 = -값), 정상 세션만.
산출   results/network_detector_dev_20261017/probe-metric/ (시계열 보존 + result.json)."""
import glob
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import network_detector_calibration as nc  # noqa: E402
import network_detector_gate3 as g3  # noqa: E402

replay, nab = nc.replay, g3.nab
EXPR = ('(sum by (namespace,pod)(rate(prober_probe_duration_seconds_sum{namespace="vllm-serving",container="vllm",probe_type="Readiness"}[60s]))'
        '/sum by (namespace,pod)(rate(prober_probe_duration_seconds_count{namespace="vllm-serving",container="vllm",probe_type="Readiness"}[60s])))'
        ' and on(namespace,pod) scrape_duration_seconds{namespace="vllm-serving",job="vllm-active"}')
OUT = nc.OUT / "probe-metric"
ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")
RULE = dict(consecutive_threshold=3, cooldown_sec=60.0, eval_interval_sec=15.0)


def window_generic(path):
    ts = []
    for line in open(path, encoding="utf-8"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        for k, v in d.items():
            if isinstance(v, str) and ISO.match(v) and (k.endswith("_utc") or k.endswith("_at")):
                try:
                    ts.append(datetime.fromisoformat(v).timestamp())
                except ValueError:
                    pass
    return min(ts), max(ts)


def ticks(a, b):
    """[a,b] 15초 격자 tick 값. 누락/NaN은 0.0(연속 끊김). 반환: ([(t, v)], n_series, n_missing)"""
    res = nc.query_range(EXPR, a, b)
    by_i = {}  # 부동소수점 시각 비교 대신 격자 번호로 맞춘다(세션 시작 시각에 소수점 초가 있음)
    for s in res:
        for t, v in s["values"]:
            if v not in ("NaN", "+Inf", "-Inf"):
                i = round((float(t) - a) / nc.STEP)
                by_i[i] = max(by_i.get(i, 0.0), float(v))
    n = int((b - a) // nc.STEP) + 1
    miss = sum(1 for i in range(n) if i not in by_i)
    return [(a + nc.STEP * i, by_i.get(i, 0.0)) for i in range(n)], len(res), miss


def all_sessions():
    """세션 -> (시작, 끝). c3 normal_valid 10 + c1 10(개정 2와 같은 20개)."""
    out = {k: s["window"] for k, s in nc.sessions().items() if s["status"] == "normal_valid"}
    for f in sorted(glob.glob(str(nc.ROOT / "experiments" / "results" / "capstone_c1_normal_collection" /
                                  "detector-evidence-c1_normal-c1*-capstone-c1-normal-20261004.jsonl"))):
        out[re.search(r"(c1(?:tr|ca)\d+)", f).group(1)] = window_generic(f)
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    sess = all_sessions()
    data, meta = {}, {}
    for sid, (a, b) in sess.items():
        tk, n, miss = ticks(a, b)
        tk = tk[nc.WINDOW_PTS - 1:]  # 개정 2와 같은 평가 시점(시작 + 60초부터)
        (OUT / f"series-{sid}.json").write_text(json.dumps({"session": sid, "expr": EXPR, "points": tk}), encoding="utf-8")
        data[sid], meta[sid] = tk, {"n_series": n, "n_ticks": len(tk), "n_missing_filled_zero": miss,
                                    "max": max(v for _, v in tk)}
    cal = replay.calibrate_threshold({k: [-v for _, v in m] for k, m in data.items()})
    T = -cal["threshold"]
    loso = {}
    for held in data:
        c = replay.calibrate_threshold({k: [-v for _, v in m] for k, m in data.items() if k != held})
        Th = -c["threshold"]
        r = replay.replay_detector([-v for _, v in data[held]], -Th, **RULE)
        row = {"T_without": round(Th, 4), "raw_signals": r["signal_count"], "max": round(meta[held]["max"], 4)}
        if held.startswith("c3"):
            sig = [datetime.fromtimestamp(data[held][i][0], tz=timezone.utc) for i in r["signal_indices"]]
            ctx = g3.context(json.loads((nc.C3 / f"{held}-validity.json").read_text(encoding="utf-8")))
            o = nab.arm_outcome(sig, ctx["t0"], obs_end=ctx["obs_end"], episodes=ctx["episodes"],
                                service_events=ctx["service_events"], infra_events=ctx["infra_events"])
            row["unnecessary"] = o["class_counts"]["unnecessary"]
        loso[held] = row
    # 파일럿(개발 자료): 같은 지표로 반응 확인
    pa = datetime.fromisoformat(nc.PILOT["start"]).timestamp()
    pb = datetime.fromisoformat(nc.PILOT["end"]).timestamp()
    ptk, pn, pmiss = ticks(pa, pb)
    ptk = ptk[nc.WINDOW_PTS - 1:]
    (OUT / "series-pilot-20260919-native.json").write_text(json.dumps({"expr": EXPR, "points": ptk}), encoding="utf-8")
    pr = replay.replay_detector([-v for _, v in ptk], -T, **RULE)
    t_inj = datetime.fromisoformat(nc.PILOT["t_injection"]).timestamp()
    t_slo = datetime.fromisoformat(nc.PILOT["t_slo"]).timestamp()
    sig = [ptk[i][0] for i in pr["signal_indices"]]
    first = next((t for t in sig if t >= t_inj), None)
    res = {"revision": "probe-metric-1", "expr": EXPR, "n_sessions": len(data), "sessions_meta": meta,
           "threshold_seconds": T, "calibration_failed": cal["calibration_failed"],
           "loso": loso, "loso_sessions_with_signal": sorted(k for k, r in loso.items() if r["raw_signals"]),
           "loso_raw_signals_total": sum(r["raw_signals"] for r in loso.values()),
           "loso_unnecessary_c3_total": sum(r.get("unnecessary", 0) for r in loso.values()),
           "pilot_20260919_native_dev_only": {
               "n_series": pn, "n_missing_filled_zero": pmiss, "n_signals": pr["signal_count"],
               "first_signal_after_injection_sec": None if first is None else round(first - t_inj, 1),
               "first_signal_vs_t_slo_sec": None if first is None else round(first - t_slo, 1),
               "max_before_injection": max((v for t, v in ptk if t < t_inj), default=None),
               "values_after_injection_max": max((v for t, v in ptk if t >= t_inj), default=None)},
           "note": "모든 세션이 개발 자료. 압박·SLO 결과는 임계값 산정에 쓰지 않았다."}
    (OUT / "result.json").write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: res[k] for k in ("n_sessions", "threshold_seconds", "calibration_failed", "loso_sessions_with_signal",
                                          "loso_raw_signals_total", "loso_unnecessary_c3_total",
                                          "pilot_20260919_native_dev_only")}, indent=2, ensure_ascii=False))
    print("누락 tick(0으로 채움) 많은 세션:", {k: v["n_missing_filled_zero"] for k, v in meta.items() if v["n_missing_filled_zero"] > 5})
    print("세션별 최대:", {k: round(v["max"], 4) for k, v in meta.items()})


if __name__ == "__main__":
    main()
