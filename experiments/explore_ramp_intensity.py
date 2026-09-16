#!/usr/bin/env python3
"""ramp 강도 재설계용 탐색 스크립트(2026-09-16). run_once()는 t_slo/t_recovery가
둘 다 찍히면 check_slo_violation()/check_recovered() 호출을 멈추는 "위반->회복
1회 사이클"짜리 단일 trial 설계라, 여러 stage에 걸친 sweep 탐색에는 안 맞는다
(실측 확인: stage-1에서 짧게 위반+즉시 회복되자 나머지 stage 관찰이 통째로
끊김 - probe 프로세스 자체는 계속 정상 실행 중이었음, kubectl exec/네트워킹
문제 아님). 이 스크립트는 run_once() 없이 ramp.py+probe.py를 끝까지 동시
실행하고, 완주한 뒤 probe raw CSV 전체를 config의 stage 경계로 나눠 stage별
P95/위반 여부를 보고한다 - 정상 trial 판정이 아니라 순수 탐색·재설계용.
"""
import argparse
import os
import statistics
import time
import uuid
from pathlib import Path

import yaml

import slo_judge
from load_ramp_adapter import IMAGE, NAMESPACE, SETTLE_SEC, _delete_pod, _run, _wait_pod_ready

RESULTS_DIR = Path(__file__).parent / "results"
POST_RAMP_DRAIN_SEC = 60  # ramp 완료 후에도 잠깐 더 관찰 - 마지막 stage의 꼬리 회복 일부라도 확인


def main():
    parser = argparse.ArgumentParser(description="ramp 강도 재설계용 stage별 sweep 탐색")
    parser.add_argument("--ramp-config", required=True)
    parser.add_argument("--probe-config", default=str(Path(__file__).parent.parent / "chaos" / "probe-config.yaml"))
    args = parser.parse_args()

    ramp_cfg = yaml.safe_load(Path(args.ramp_config).read_text(encoding="utf-8"))
    stages = ramp_cfg["stages"]
    total_ramp_sec = sum(s["duration_sec"] for s in stages)

    run_id = f"explore-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    ramp_pod = f"explore-ramp-{uuid.uuid4().hex[:6]}"
    probe_pod = f"explore-probe-{uuid.uuid4().hex[:6]}"
    ramp_config_name = Path(args.ramp_config).name
    probe_config_name = Path(args.probe_config).name
    local_raw = RESULTS_DIR / f"probe-{run_id}-raw.csv"

    print(f"run_id: {run_id}, 총 ramp 시간: {total_ramp_sec}초")

    try:
        for pod_name in (ramp_pod, probe_pod):
            _run(["kubectl", "run", pod_name, "-n", NAMESPACE, f"--image={IMAGE}",
                  "--image-pull-policy=Never", "--restart=Never", "--", "sleep", str(int(total_ramp_sec) + 600)],
                 check=True)
        for pod_name in (ramp_pod, probe_pod):
            if not _wait_pod_ready(pod_name):
                raise RuntimeError(f"{pod_name} Ready 시간초과")

        _run(["kubectl", "cp", os.path.relpath(args.ramp_config), f"{NAMESPACE}/{ramp_pod}:/{ramp_config_name}"],
             check=True)
        _run(["kubectl", "cp", os.path.relpath(args.probe_config), f"{NAMESPACE}/{probe_pod}:/{probe_config_name}"],
             check=True)
        print(f"안정화 대기 {SETTLE_SEC}초...")
        time.sleep(SETTLE_SEC)

        ramp_inner = (f"PYTHONUNBUFFERED=1 python /ramp.py --config /{ramp_config_name} "
                      f"--run-id {run_id} --method native --rep 1 > /ramp.log 2>&1; echo $? > /ramp.exit")
        _run(["kubectl", "exec", "-n", NAMESPACE, ramp_pod, "--", "sh", "-c",
              f"nohup sh -c '{ramp_inner}' < /dev/null > /ramp-wrapper.log 2>&1 &"], check=True)

        probe_duration = total_ramp_sec + POST_RAMP_DRAIN_SEC + 30
        probe_inner = (f"PYTHONUNBUFFERED=1 python /probe.py --config /{probe_config_name} "
                       f"--run-id {run_id} --scenario explore --arm native --rep 1 "
                       f"--out /probe-raw.csv --duration-sec {probe_duration} "
                       f"> /probe.log 2>&1; echo $? > /probe.exit")
        _run(["kubectl", "exec", "-n", NAMESPACE, probe_pod, "--", "sh", "-c",
              f"nohup sh -c '{probe_inner}' < /dev/null > /probe-wrapper.log 2>&1 &"], check=True)

        t_start = time.monotonic()
        print(f"ramp+probe 시작, 완주까지 대기(~{total_ramp_sec}초 + 여유)")
        deadline = t_start + total_ramp_sec + 60
        ramp_done = False
        while time.monotonic() < deadline:
            r = _run(["kubectl", "exec", "-n", NAMESPACE, ramp_pod, "--", "test", "-f", "/ramp.exit"])
            if r.returncode == 0:
                ramp_done = True
                break
            time.sleep(5)
        if not ramp_done:
            print("경고: ramp.py가 예상 시간 내에 안 끝남 - 현재까지 데이터로 진행")

        print(f"ramp 완료(경과 {time.monotonic() - t_start:.1f}초) - post-ramp drain {POST_RAMP_DRAIN_SEC}초 대기")
        time.sleep(POST_RAMP_DRAIN_SEC)

        r = _run(["kubectl", "exec", "-n", NAMESPACE, probe_pod, "--", "cat", "/probe-raw.csv"], check=True)
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        local_raw.write_text(r.stdout, encoding="utf-8")
    finally:
        _delete_pod(ramp_pod)
        _delete_pod(probe_pod)

    rows = slo_judge.load_raw(local_raw)
    t0 = rows[0]["sent_at"]
    boundary = 0.0
    print(f"\n=== stage별 결과 (raw: {local_raw}) ===")
    print(f"{'stage':<22} {'rps':>6} {'n':>5} {'mean':>8} {'P95':>8} {'max':>8} {'위반?':>6}")
    for s in stages:
        lo, hi = boundary, boundary + s["duration_sec"]
        bucket = [r for r in rows if lo <= (r["sent_at"] - t0).total_seconds() < hi]
        if bucket:
            lat = [r["latency"] for r in bucket]
            p95 = statistics.quantiles(lat, n=100)[94] if len(lat) >= 20 else max(lat)
            violates = p95 > slo_judge.LATENCY_THRESHOLD
            print(f"{s['name']:<22} {s['rps']:>6} {len(bucket):>5} {statistics.mean(lat):>8.3f} "
                  f"{p95:>8.3f} {max(lat):>8.3f} {'예' if violates else '아니오':>6}")
        else:
            print(f"{s['name']:<22} {s['rps']:>6} {'0':>5} (데이터 없음)")
        boundary = hi

    post = [r for r in rows if (r["sent_at"] - t0).total_seconds() >= boundary]
    if post:
        lat = [r["latency"] for r in post]
        p95 = statistics.quantiles(lat, n=100)[94] if len(lat) >= 20 else max(lat)
        violates = p95 > slo_judge.LATENCY_THRESHOLD
        print(f"{'post-ramp(drain)':<22} {'-':>6} {len(post):>5} {statistics.mean(lat):>8.3f} "
              f"{p95:>8.3f} {max(lat):>8.3f} {'예' if violates else '아니오':>6}")

    print(f"\nlatency SLO threshold(v2) = {slo_judge.LATENCY_THRESHOLD}s")


if __name__ == "__main__":
    main()
