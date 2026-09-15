#!/usr/bin/env python3
"""SLO 판정용 저율 open-loop probe. load generator(ramp.py)와 별도 프로세스로
띄워서, 모든 arm에서 동일한 요청·주기를 유지한다 - load generator를 SLO
판정에도 같이 쓰면 arm마다 실제 주입 부하 자체가 달라 판정 표본이 arm 간에
달라진다(Phase 8 2차 리뷰 지적).

ramp.py와 차이점: ramp.py는 결과를 메모리에 모았다가 종료 시점에 한 번에
CSV로 쓴다(사후분석 전제). 이 probe는 각 요청마다 즉시 append+flush한다 -
run_once()의 Prober 어댑터가 이 프로세스가 아직 돌고 있는 중에도 주기적으로
raw CSV를 읽어 slo_judge.py(load_raw/evaluate/find_t_slo/find_t_recovery)로
실시간에 가깝게 판정해야 하기 때문이다. sent_at/latency/success 세 컬럼만
slo_judge.load_raw()가 실제로 읽으므로(다른 컬럼은 DictReader가 무시)
나머지(experiment_run_id 등)는 raw 로그 자체의 태깅용으로 얹는다.

1 RPS 고정이면 60초 슬라이딩 윈도우(slo-definition.md)에 약 60개 표본이
쌓여 P95·가용성 계산이 비교적 안정적이다(2차 리뷰).
"""
import argparse
import asyncio
import csv
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import yaml

sys.stdout.reconfigure(encoding="utf-8")

FIELDNAMES = ["experiment_run_id", "scenario", "arm", "repetition", "sent_at", "latency", "status", "success"]


async def _fire_and_log(session, url, payload, writer, f, tag):
    sent_at = datetime.now(timezone.utc).isoformat()
    start = time.monotonic()
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            await resp.read()
            row = {"sent_at": sent_at, "latency": time.monotonic() - start,
                   "status": resp.status, "success": resp.status == 200}
    except Exception:
        row = {"sent_at": sent_at, "latency": time.monotonic() - start,
               "status": None, "success": False}
    row.update(tag)
    writer.writerow(row)
    f.flush()


async def main(config_path, run_id, scenario, arm, rep, out_path, duration_sec):
    config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    target = config["target"]
    payload = {"model": target["model"], "prompt": target["prompt"], "max_tokens": target["max_tokens"]}
    rps = config.get("rps", 1)
    tag = {"experiment_run_id": run_id, "scenario": scenario, "arm": arm, "repetition": rep}

    out_f = open(out_path, "w", newline="", encoding="utf-8")
    writer = csv.DictWriter(out_f, fieldnames=FIELDNAMES, extrasaction="ignore")
    writer.writeheader()
    out_f.flush()

    connector = aiohttp.TCPConnector(limit=0)
    interval = 1.0 / rps
    start_time = time.monotonic()
    next_fire = start_time
    tasks = []
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            while time.monotonic() - start_time < duration_sec:
                now = time.monotonic()
                if now >= next_fire:
                    tasks.append(asyncio.create_task(
                        _fire_and_log(session, target["url"], payload, writer, out_f, tag)))
                    next_fire += interval
                else:
                    await asyncio.sleep(min(0.01, next_fire - now))
            if tasks:
                await asyncio.wait(tasks, timeout=10)
    finally:
        out_f.close()
    print(f"probe 종료: {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SLO 판정용 저율 open-loop probe")
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--arm", required=True)
    parser.add_argument("--rep", type=int, required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--duration-sec", type=float, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.config, args.run_id, args.scenario, args.arm, args.rep, args.out, args.duration_sec))
