#!/usr/bin/env python3
"""probe.py 단독(ramp.py 주입 없이) 실행 시 vLLM 서버에 주는 부하 자체를
측정한다. load_ramp 강도를 probe 포함 조건으로 재보정하기 전에, probe의
상시 1RPS 자체가 이미 유의미한 처리용량을 차지하는지 먼저 확인해야 한다는
지적(2026-09-16, 계약서 변경이력)에 따른 사전 calibration - 이 스크립트는
run_once() 상태머신을 쓰지 않는 단순 1회성 측정이라 load_ramp_adapter.py의
Injector/Prober 대신 필요한 부분만 직접 구현한다.

5분(기본값) 동안 probe만 돌리고 latency P95/성공률(slo_judge.evaluate())과
vLLM pod CPU·메모리(kubectl top, 전/중/후)를 비교해서 baseline 대비 부하
증가를 보여준다.
"""
import argparse
import os
import subprocess
import time
import uuid
from pathlib import Path

import slo_judge

NAMESPACE = "vllm-serving"
IMAGE = "loadgen-runner:local"
SETTLE_SEC = 60
POD_READY_TIMEOUT_SEC = 60
TOP_SAMPLE_INTERVAL_SEC = 60


def _run(cmd, check=False):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=check)


def _wait_pod_ready(pod_name: str, timeout=POD_READY_TIMEOUT_SEC) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = _run(["kubectl", "get", "pod", pod_name, "-n", NAMESPACE,
                   "-o", "jsonpath={.status.containerStatuses[0].ready}"])
        if r.stdout.strip() == "true":
            return True
        time.sleep(2)
    return False


def _top_snapshot(label: str) -> None:
    r = _run(["kubectl", "top", "pod", "-n", NAMESPACE, "--no-headers"])
    print(f"[kubectl top - {label}]\n{r.stdout.strip()}")


def main():
    parser = argparse.ArgumentParser(description="probe.py 단독 실행 calibration - baseline 대비 부하 증가 측정")
    parser.add_argument("--config", default=str(Path(__file__).parent.parent / "chaos" / "scenario-load-ramp.yaml"))
    parser.add_argument("--duration-sec", type=float, default=300)
    args = parser.parse_args()

    pod_name = f"probe-calib-{uuid.uuid4().hex[:8]}"
    config_name = Path(args.config).name
    run_id = f"calib-probe-only-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    local_raw = Path(__file__).parent / "results" / f"probe-{run_id}-raw.csv"

    print(f"run_id: {run_id}")
    _top_snapshot("probe 시작 전 (baseline)")

    try:
        _run(["kubectl", "run", pod_name, "-n", NAMESPACE, f"--image={IMAGE}",
              "--image-pull-policy=Never", "--restart=Never", "--", "sleep", str(int(args.duration_sec) + 600)],
             check=True)
        if not _wait_pod_ready(pod_name):
            raise RuntimeError(f"{pod_name} Ready 시간초과")
        _run(["kubectl", "cp", os.path.relpath(args.config), f"{NAMESPACE}/{pod_name}:/{config_name}"], check=True)
        time.sleep(SETTLE_SEC)

        inner = (f"PYTHONUNBUFFERED=1 python /probe.py --config /{config_name} "
                 f"--run-id {run_id} --scenario calibration --arm none --rep 0 "
                 f"--out /probe-raw.csv --duration-sec {args.duration_sec} "
                 f"> /probe.log 2>&1; echo $? > /probe.exit")
        cmd = f"nohup sh -c '{inner}' < /dev/null > /probe-wrapper.log 2>&1 &"
        _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "sh", "-c", cmd], check=True)

        start = time.monotonic()
        deadline = start + args.duration_sec
        next_sample = start + TOP_SAMPLE_INTERVAL_SEC
        while time.monotonic() < deadline:
            if time.monotonic() >= next_sample:
                _top_snapshot(f"진행 중 (t+{int(time.monotonic() - start)}s)")
                next_sample += TOP_SAMPLE_INTERVAL_SEC
            time.sleep(2)

        # probe.py 자체 종료(exitfile) 대기 - duration_sec은 probe 내부 루프
        # 기준이라 aiohttp 마무리 대기(최대 10초) 등으로 조금 더 걸릴 수 있음
        for _ in range(30):
            if _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "test", "-f", "/probe.exit"]).returncode == 0:
                break
            time.sleep(2)

        _top_snapshot("probe 종료 직후")

        r = _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "cat", "/probe-raw.csv"], check=True)
        local_raw.parent.mkdir(parents=True, exist_ok=True)
        local_raw.write_text(r.stdout, encoding="utf-8")
    finally:
        _run(["kubectl", "delete", "pod", pod_name, "-n", NAMESPACE, "--wait=true", "--timeout=60s"])

    rows = slo_judge.load_raw(local_raw)
    points = slo_judge.evaluate(rows)
    p95_values = [p["p95"] for p in points]
    success_rates = [p["success_rate"] for p in points]
    violations = sum(1 for p in points if p["latency_violating"] or p["availability_violating"])

    print(f"\n=== probe-only calibration 결과 ({len(rows)}건 요청, {args.duration_sec:.0f}초) ===")
    print(f"raw CSV: {local_raw}")
    print(f"L_baseline(slo_judge.py) = {slo_judge.L_BASELINE}s, latency threshold = {slo_judge.LATENCY_THRESHOLD}s")
    print(f"관측 P95 범위: {min(p95_values):.3f}s ~ {max(p95_values):.3f}s (마지막: {p95_values[-1]:.3f}s)")
    print(f"성공률 범위: {min(success_rates):.1%} ~ {max(success_rates):.1%}")
    print(f"SLO 판정 기준 위반 포인트: {violations}/{len(points)}건 "
          f"({'주의: probe 단독으로도 위반 발생' if violations else '위반 없음 - probe 단독 부하는 안전'})")


if __name__ == "__main__":
    main()
