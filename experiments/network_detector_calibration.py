#!/usr/bin/env python3
"""Pod 통신 소요 시간 감지기(net_mean) 보정 - 정상 세션만 사용(2026-10-17, 읽기 전용).
사전등록 초안: docs/design/NETWORK-DETECTOR-PREREG-DRAFT-20261017.md

정의(결과를 보기 전에 고정):
  지표   Prometheus scrape_duration_seconds{namespace="vllm-serving",job="vllm-active"}
  net_mean  15초 격자 평가 시점마다 직전 60초 창(격자 5점)의 평균(cpu_mean과 같은 창)
  규칙   net_mean > T 가 3회 연속이면 신호, 쿨다운 60초, 평가 간격 15초(기존 B/C/K 공통 규칙)
  보정   기존 replay.calibrate_threshold(모든 보정 세션에서 신호 0인 가장 민감한 값), 점수 = -net_mean(CPU와 같은 부호 처리)
  세션   c3 정상 세션 중 normal_valid만. 역할은 기존 c3 계획 그대로: 보정 = TRAIN+CALIBRATION, 평가 = VALIDATION. 압박 데이터 미사용.
산출: experiments/results/network_detector_dev_20261017/ (격자 원자료 JSON + calibration.json). Prometheus 보존 30일이라 원자료를 함께 저장한다.
"""
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

os.environ["MSYS_NO_PATHCONV"] = "1"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "anomaly-detection" / "v3" / "model_v31"))
import replay  # noqa: E402

BASE = "/api/v1/namespaces/monitoring/services/kube-prom-kube-prometheus-prometheus:9090/proxy/api/v1/"
METRIC = 'scrape_duration_seconds{namespace="vllm-serving",job="vllm-active"}'
STEP, WINDOW_PTS = 15, 5
OUT = Path(__file__).parent / "results" / "network_detector_dev_20261017"
C3 = Path(__file__).parent / "results" / "c3_normal_coverage_collection"
TS_KEYS = ("started_at_utc", "score_completed_at_utc", "skipped_at_utc", "completed_at_utc", "window_end_utc")
PILOT = {"start": "2026-09-19T15:45:30+00:00", "end": "2026-09-19T15:56:00+00:00", "t_injection": "2026-09-19T15:48:47.499+00:00",
         "t_slo": "2026-09-19T15:49:20.434+00:00"}


def query_range(expr, a, b):
    p = {"query": expr, "start": a, "end": b, "step": STEP}
    out = subprocess.run(["kubectl", "get", "--raw", BASE + "query_range?" + urllib.parse.urlencode(p)],
                         capture_output=True, text=True).stdout
    return json.loads(out)["data"]["result"]


def grid(a, b, expr=METRIC):
    """15초 격자 값. 시계열이 여러 개면 시점별 최대(보수적), 개수를 함께 돌려준다."""
    res = query_range(expr, a, b)
    by_t = {}
    for s in res:
        for t, v in s["values"]:
            by_t[float(t)] = max(by_t.get(float(t), 0.0), float(v))
    ts = sorted(by_t)
    return [[t, by_t[t]] for t in ts], len(res)


def net_mean(series):
    """격자 5점 평균. 격자 간격이 어긋난 창은 만들지 않는다(누락 시 해당 평가 제외). 반환: [(t_end, value)]"""
    out = []
    for i in range(WINDOW_PTS - 1, len(series)):
        w = series[i - WINDOW_PTS + 1:i + 1]
        if w[-1][0] - w[0][0] == STEP * (WINDOW_PTS - 1):
            out.append((w[-1][0], sum(x[1] for x in w) / WINDOW_PTS))
    return out


def evidence_window(path):
    ts = []
    for line in open(path, encoding="utf-8"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        for k in TS_KEYS:
            v = d.get(k)
            if isinstance(v, str) and re.match(r"\d{4}-\d\d-\d\dT", v):
                ts.append(datetime.fromisoformat(v).timestamp())
    return min(ts), max(ts)


def sessions():
    out = {}
    for f in sorted(glob.glob(str(C3 / "c3-session-c3*.json"))):
        sid = re.search(r"(c3[nr]\d+)", f).group(1)
        role = json.load(open(f, encoding="utf-8"))["role"]
        status = json.load(open(C3 / f"{sid}-validity.json", encoding="utf-8"))["status"]
        ev = glob.glob(str(C3 / f"c1-live-evidence-*-{sid}-capstone-c3-normal-coverage-20261010.jsonl"))[0]
        out[sid] = {"role": role, "status": status, "evidence": Path(ev).name, "window": evidence_window(ev)}
    return out


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    ss = sessions()
    data, files = {}, {}
    for sid, s in ss.items():
        series, n = grid(s["window"][0] - STEP * (WINDOW_PTS - 1), s["window"][1])
        f = OUT / f"series-{sid}.json"
        f.write_text(json.dumps({"session": sid, "metric": METRIC, "n_series": n, "points": series}), encoding="utf-8")
        data[sid], files[sid] = net_mean(series), f.name
        s["n_series"], s["n_eval_points"] = n, len(data[sid])
    cal_ids = [k for k, s in ss.items() if s["status"] == "normal_valid" and s["role"] in ("TRAIN", "CALIBRATION")]
    val_ids = [k for k, s in ss.items() if s["status"] == "normal_valid" and s["role"] == "VALIDATION"]
    cal = replay.calibrate_threshold({k: [-v for _, v in data[k]] for k in cal_ids})
    T = -cal["threshold"]
    rule = dict(consecutive_threshold=3, cooldown_sec=60.0, eval_interval_sec=15.0)

    def run(ids, thr):
        out = {}
        for k in ids:
            r = replay.replay_detector([-v for _, v in data[k]], -thr, **rule)
            out[k] = {kk: r[kk] for kk in ("n_points", "point_anomaly_count", "signal_count", "max_consecutive_anomalous")}
            out[k]["net_mean_max"] = max(v for _, v in data[k])
        return out
    # 개발 자료(9/19 네트워크 파일럿, 조치 없음 arm) - 임계값을 정한 뒤 적용만 한다. 독립 검증 아님.
    a, b = datetime.fromisoformat(PILOT["start"]).timestamp(), datetime.fromisoformat(PILOT["end"]).timestamp()
    ps, pn = grid(a, b)
    (OUT / "series-pilot-20260919-native.json").write_text(
        json.dumps({"metric": METRIC, "n_series": pn, "points": ps}), encoding="utf-8")
    pm = net_mean(ps)
    r = replay.replay_detector([-v for _, v in pm], -T, **rule)
    t_inj = datetime.fromisoformat(PILOT["t_injection"]).timestamp()
    t_slo = datetime.fromisoformat(PILOT["t_slo"]).timestamp()
    sig = [pm[i][0] for i in r["signal_indices"]]
    first = next((t for t in sig if t >= t_inj), None)
    result = {
        "definition": {"metric": METRIC, "net_mean": "직전 60초 창(15초 격자 5점) 평균", **rule, "anomalous_if": "net_mean > T",
                       "calibration": "replay.calibrate_threshold(score=-net_mean), 보정 세션 전부에서 신호 0인 가장 민감한 값"},
        "sessions": ss, "series_files": files, "series_sha256": {k: sha(OUT / v) for k, v in files.items()},
        "calibration_sessions": cal_ids, "evaluation_sessions": val_ids,
        "excluded": {k: s["status"] for k, s in ss.items() if s["status"] != "normal_valid"},
        "threshold_seconds": T, "calibration_failed": cal["calibration_failed"],
        "calibration_result": run(cal_ids, T), "evaluation_result": run(val_ids, T),
        "excluded_informational": run([k for k in ss if k not in cal_ids + val_ids], T),
        "pilot_20260919_native_dev_only": {
            "n_series": pn, "n_signals": r["signal_count"],
            "signal_times_utc": [datetime.utcfromtimestamp(t).isoformat() for t in sig],
            "t_injection": PILOT["t_injection"], "t_slo": PILOT["t_slo"],
            "first_signal_after_injection_sec": None if first is None else round(first - t_inj, 1),
            "first_signal_vs_t_slo_sec": None if first is None else round(first - t_slo, 1),
            "net_mean_before_injection_max": max((v for t, v in pm if t < t_inj), default=None)},
        "note": "압박·SLO 결과는 임계값 산정에 쓰지 않았다. 파일럿 값은 지표 선택(개발)에 참고된 자료라 독립 검증이 아니다.",
    }
    (OUT / "calibration.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("calibration_sessions", "evaluation_sessions", "excluded", "threshold_seconds",
                                              "calibration_failed", "evaluation_result", "excluded_informational",
                                              "pilot_20260919_native_dev_only")}, indent=2, ensure_ascii=False, default=str))


def main_rev2():
    """개정 2(2026-10-17): 가용한 유효 정상 세션 전체(c1 정상 10 + c3 normal_valid 10)로 보정. 저장된 시계열만 읽는다(클러스터 불필요).
    공식 평가는 동결 뒤 새로 수집하는 정상 세션이 한다. 여기 세션은 전부 개발 자료(세션 안 신호 0은 정의상 당연)."""
    ss = {k: s for k, s in sessions().items() if s["status"] == "normal_valid"}
    data, hashes = {}, {}
    for sid in ss:
        f = OUT / f"series-{sid}.json"
        data[sid], hashes[sid] = net_mean(json.loads(f.read_text(encoding="utf-8"))["points"]), sha(f)
    for f in sorted((OUT / "c1").glob("series-c1*.json")):
        sid = f.stem.replace("series-", "")
        data[sid], hashes[sid] = net_mean(json.loads(f.read_text(encoding="utf-8"))["points"]), sha(f)
    cal = replay.calibrate_threshold({k: [-v for _, v in m] for k, m in data.items()})
    T = -cal["threshold"]
    rule = dict(consecutive_threshold=3, cooldown_sec=60.0, eval_interval_sec=15.0)
    per = {k: {"n_points": len(m), "signals": replay.replay_detector([-v for _, v in m], -T, **rule)["signal_count"],
               "net_mean_max": max(v for _, v in m)} for k, m in data.items()}
    pil = json.loads((OUT / "series-pilot-20260919-native.json").read_text(encoding="utf-8"))["points"]
    pm = net_mean(pil)
    r = replay.replay_detector([-v for _, v in pm], -T, **rule)
    t_inj = datetime.fromisoformat(PILOT["t_injection"]).timestamp()
    t_slo = datetime.fromisoformat(PILOT["t_slo"]).timestamp()
    sig = [pm[i][0] for i in r["signal_indices"]]
    first = next((t for t in sig if t >= t_inj), None)
    out = {"revision": 2, "definition": "개정 1과 같음(지표·net_mean·3연속·쿨다운 60·간격 15). 보정 세션만 다름: 유효 정상 세션 전체",
           "calibration_sessions": sorted(data), "n_sessions": len(data), "n_eval_points": sum(len(m) for m in data.values()),
           "threshold_seconds": T, "calibration_failed": cal["calibration_failed"], "series_sha256": hashes,
           "in_sample_per_session": per, "net_mean_max_all": max(v for m in data.values() for _, v in m),
           "pilot_20260919_native_dev_only": {"first_signal_after_injection_sec": None if first is None else round(first - t_inj, 1),
                                              "first_signal_vs_t_slo_sec": None if first is None else round(first - t_slo, 1),
                                              "n_signals": r["signal_count"]},
           "note": "모든 세션이 개발 자료. 공식 평가는 동결 뒤 새 정상 세션. 압박·SLO 결과는 임계값 산정에 쓰지 않았다."}
    (OUT / "calibration-rev2.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("n_sessions", "n_eval_points", "threshold_seconds", "calibration_failed",
                                          "net_mean_max_all", "pilot_20260919_native_dev_only")}, indent=2, ensure_ascii=False))


def selftest():
    s = [[i * STEP, float(i)] for i in range(8)]
    m = net_mean(s)
    assert [round(v, 1) for _, v in m] == [2.0, 3.0, 4.0, 5.0] and len(m) == 4  # 5점 평균
    gap = s[:3] + s[4:]  # 누락 있으면 그 창은 제외
    assert len(net_mean(gap)) < len(m)
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main_rev2() if "--rev2" in sys.argv else main()
