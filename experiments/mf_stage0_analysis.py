#!/usr/bin/env python3
"""메모리 압박 Pilot 사후 분석(읽기 전용, 2026-10-16) - 새 SLO 판정 로직 없음(slo_judge 재사용).

사용자 승인 원인 귀속 규칙(실행 전 고정):
  공식 SLO v3(slo_judge.evaluate/find_t_slo)를 수정 없이 쓰고, 위반을 "메모리 관련 저하"로 인정하려면 셋 다 만족:
  R1 위반 구간 시작 S >= 주입 관측 시각(주입 전부터 이어진 위반은 불인정)
  R2 주입 후 요청만으로 같은 판정을 다시 돌려도 지속 위반 재현
  R3 그 재현 위반이 stress가 걸려 있는 동안(S <= 주입 종료) 시작
분류(탐지기 결과와 무관):
  D 어댑터 PASS 조건 실패(중단·OOM·재시작·노드 이상·cleanup 미복귀 등)  C 인정된 위반 없음
  A 인정된 위반 + 주입 구간 cpu_mean <= 1.6842544293089357   B 인정된 위반 + cpu_mean > 임계값(CPU 혼입)
CPU 임계값 초과 여부는 SLO와 별개로 항상 기록한다. CPU·PSI·스로틀링은 사후 Prometheus 범위 조회(어댑터 무수정).
"""
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

import slo_judge

CPU_THRESHOLD = 1.6842544293089357


def first_violation(points):
    """공식 판정이 처음 확정한 위반의 (t_slo, 위반 구간 시작 S). 위반 없으면 None."""
    t_slo = slo_judge.find_t_slo(points)
    if t_slo is None:
        return None
    start, S = None, None
    for p in points:
        if p["latency_violating"]:
            start = start or p["t"]
            if (p["t"] - start).total_seconds() >= slo_judge.LATENCY_PERSIST_SEC and p["observed_at"] == t_slo:
                S = start
                break
        else:
            start = None
    if S is None:
        S = next((p["t"] for p in points if p["availability_violating"] and p["observed_at"] == t_slo), None)
    return {"t_slo": t_slo, "S": S}


def attribute(rows, t_inj, t_end):
    full = first_violation(slo_judge.evaluate(rows))
    post = [r for r in rows if r["sent_at"] >= t_inj]
    v2 = first_violation(slo_judge.evaluate(post)) if post else None
    r1 = full is not None and full["S"] is not None and full["S"] >= t_inj
    r2 = v2 is not None
    r3 = r2 and v2["S"] is not None and v2["S"] <= t_end
    return {"official_t_slo": full and full["t_slo"].isoformat(), "S_full": full and full["S"] and full["S"].isoformat(),
            "post_only_t_slo": v2 and v2["t_slo"].isoformat(), "S_post": v2 and v2["S"] and v2["S"].isoformat(),
            "R1": r1, "R2": r2, "R3": r3, "accepted": bool(r1 and r2 and r3)}


def classify(passed, accepted, cpu_exceeded):
    if not passed:
        return "D"
    if not accepted:
        return "C"
    return "B" if cpu_exceeded else "A"


def prom_range(prom, expr, t0, t1, step=15):
    r = requests.get(f"{prom}/api/v1/query_range", timeout=30,
                     params={"query": expr, "start": t0.timestamp(), "end": t1.timestamp(), "step": step})
    res = r.json()["data"]["result"]
    return [(float(v[0]), float(v[1])) for v in res[0]["values"]] if res else []


def stats(vals):
    xs = [v for _, v in vals if v == v]
    return {"n": len(xs), "min": min(xs), "mean": sum(xs) / len(xs), "max": max(xs)} if xs else {"n": 0}


def telemetry(prom, pod, node_ip, t0, t1, tb0, tb1):
    sel = f'namespace="vllm-serving",container="vllm",pod="{pod}"'
    cpu = f"sum(rate(container_cpu_usage_seconds_total{{{sel}}}[60s]))"
    thr = (f"sum(rate(container_cpu_cfs_throttled_periods_total{{{sel}}}[60s]))"
           f"/sum(rate(container_cpu_cfs_periods_total{{{sel}}}[60s]))")
    psi_w = f'rate(node_pressure_memory_waiting_seconds_total{{instance="{node_ip}:9100"}}[60s])'
    psi_s = f'rate(node_pressure_memory_stalled_seconds_total{{instance="{node_ip}:9100"}}[60s])'
    out = {}
    for name, expr in (("cpu_cores", cpu), ("cpu_throttle_ratio", thr), ("node_psi_memory_some", psi_w),
                       ("node_psi_memory_full", psi_s)):
        out[name] = {"injection": stats(prom_range(prom, expr, t0, t1)), "baseline": stats(prom_range(prom, expr, tb0, tb1))}
    return out


def analyze(summary_path, prom, node_ip=""):
    s = json.loads(Path(summary_path).read_text(encoding="utf-8"))
    iso = datetime.fromisoformat
    raw = Path(s["slo"]["local_raw_csv"]) if s.get("slo", {}).get("local_raw_csv") else None
    out = {"run_id": s["run_id"], "size_mb": s["size_mb"], "aborted": s["aborted"], "abort_reason": s["abort_reason"],
           "adapter_pass": s["pass"]["pass"], "adapter_fail_reasons": s["pass"]["reasons"],
           "baseline_working_set_bytes": s.get("baseline_working_set_bytes"),
           "max_working_set_bytes": s.get("max_working_set_bytes"), "official_slo": s.get("slo")}
    if s.get("t_injection") and raw and raw.exists():
        t_inj, t_end = iso(s["t_injection"]), iso(s["t_injection_end"])
        out["attribution"] = attribute(slo_judge.load_raw(raw), t_inj, t_end)
        tb1 = iso(s["t_baseline_done"])
        out["telemetry"] = telemetry(prom, s["target_pod"], s.get("node_ip") or node_ip, t_inj + timedelta(seconds=60), t_end,
                                      tb1 - timedelta(seconds=45), tb1)
        cpu_max = out["telemetry"]["cpu_cores"]["injection"].get("max")
        out["cpu_exceeded_threshold"] = cpu_max is not None and cpu_max > CPU_THRESHOLD
        out["class"] = classify(out["adapter_pass"], out["attribution"]["accepted"], out["cpu_exceeded_threshold"])
    else:
        out["class"] = "D"
    return out


def selftest():
    t0 = datetime(2026, 10, 16, 0, 0, 0)
    ok = lambda i, lat=0.2, succ=True: {"sent_at": t0 + timedelta(seconds=i), "latency": lat, "success": succ}  # noqa: E731
    calm = [ok(i) for i in range(300)]
    inj, end = t0 + timedelta(seconds=100), t0 + timedelta(seconds=220)
    assert not attribute(calm, inj, end)["accepted"] and classify(True, False, False) == "C"
    slow_in = [ok(i, 1.0 if 110 <= i < 200 else 0.2) for i in range(300)]  # 주입 중 느려짐 -> 인정
    a = attribute(slow_in, inj, end)
    assert a["accepted"] and classify(True, a["accepted"], False) == "A" and classify(True, True, True) == "B"
    slow_pre = [ok(i, 1.0 if 40 <= i < 160 else 0.2) for i in range(300)]  # 주입 전부터 이어진 위반 -> R1 불인정
    b = attribute(slow_pre, inj, end)
    assert not b["R1"] and not b["accepted"]
    slow_after = [ok(i, 1.0 if 230 <= i < 300 else 0.2) for i in range(300)]  # 정리 뒤 시작 -> R3 불인정
    c = attribute(slow_after, inj, end)
    assert not c["R3"] and not c["accepted"] and classify(False, True, False) == "D"
    print("selftest ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("summary", nargs="?")
    ap.add_argument("--prom", default="http://localhost:9090")
    ap.add_argument("--node-ip", default="192.168.30.117")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        sys.exit(0)
    print(json.dumps(analyze(a.summary, a.prom, a.node_ip), indent=2, ensure_ascii=False, default=str))
