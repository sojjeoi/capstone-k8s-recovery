#!/usr/bin/env python3
"""고정 도착률(open-loop) 부하 생성기 — Phase 5 요청 폭주 시나리오.

closed-loop(동시사용자 수 고정, 응답 기다렸다 다음 요청)이 아니라
정해진 시각마다 이전 요청의 완료 여부와 무관하게 새 요청을 쏜다.
서버가 느려져도 유입 요청률이 줄지 않아야 실제 포화 상태를 관측할 수 있다.

TTFT/TPOT/KV cache 등 서버 쪽 상세 지표는 이미 Prometheus가 스크랩 중이므로
여기서는 클라이언트가 실제로 관측한 성공률/지연시간/실제 도달 RPS만 기록하고,
같은 시간대 그래프는 Grafana에서 대조한다.
"""
import argparse
import asyncio
import csv
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import yaml


async def fire_request(session, url, payload, results, sent_at):
    start = time.monotonic()
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            await resp.read()
            results.append({
                "sent_at": sent_at,
                "latency": time.monotonic() - start,
                "status": resp.status,
                "success": resp.status == 200,
            })
    except Exception as e:
        results.append({
            "sent_at": sent_at,
            "latency": time.monotonic() - start,
            "status": None,
            "success": False,
            "error": str(e),
        })


async def run_stage(session, url, payload, rps, duration_sec, stage_name):
    interval = 1.0 / rps
    results = []
    tasks = []
    stage_start = time.monotonic()
    next_fire = stage_start
    print(f"\n=== {stage_name}: {rps} RPS, {duration_sec}s ===")

    while time.monotonic() - stage_start < duration_sec:
        now = time.monotonic()
        if now >= next_fire:
            sent_at = datetime.now(timezone.utc).isoformat()
            tasks.append(asyncio.create_task(fire_request(session, url, payload, results, sent_at)))
            next_fire += interval
        else:
            await asyncio.sleep(min(0.01, next_fire - now))

    # 단계 끝난 뒤 아직 안 끝난 요청은 최대 10초만 더 기다리고, 그래도 안 끝나면
    # 다음 단계로 안 새어들어가게 취소한다.
    done, pending = await asyncio.wait(tasks, timeout=10) if tasks else (set(), set())
    for t in pending:
        t.cancel()

    sent = len(tasks)
    completed = [r for r in results if r["status"] is not None]
    success = [r for r in results if r["success"]]
    latencies = [r["latency"] for r in completed]

    actual_rps = sent / duration_sec
    success_rate = len(success) / sent if sent else 0.0
    if len(latencies) >= 20:
        p95, p99 = statistics.quantiles(latencies, n=100)[94], statistics.quantiles(latencies, n=100)[98]
    else:
        p95 = p99 = max(latencies) if latencies else 0.0

    print(f"  목표 RPS: {rps} / 실제 발사 RPS: {actual_rps:.2f}")
    print(f"  전송: {sent}건 / 성공: {len(success)}건 / 성공률: {success_rate:.1%}")
    print(f"  클라이언트 관측 latency P95: {p95:.2f}s, P99: {p99:.2f}s")
    print("  (TTFT/TPOT/KV cache 등 서버 지표는 Grafana에서 같은 시간대로 확인)")

    for r in results:
        r["stage"] = stage_name

    summary = {
        "stage": stage_name, "target_rps": rps, "actual_rps": round(actual_rps, 2),
        "sent": sent, "success": len(success), "success_rate": round(success_rate, 4),
        "p95": round(p95, 3), "p99": round(p99, 3),
    }
    return summary, results


async def main(config_path, run_id=None, method="manual", repetition=1):
    # Phase 8에서 Prometheus/policy 로그와 조인하려면 동일 experiment_run_id가
    # 필요하다(guideline.md 9-2절) — 오케스트레이터가 나중에 --run-id로 주입할
    # 수 있게 옵션으로 열어두고, 지금처럼 단독 실행할 땐 자동 생성한다.
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    scenario = Path(config_path).stem

    config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    target = config["target"]
    payload = {"model": target["model"], "prompt": target["prompt"], "max_tokens": target["max_tokens"]}

    # limit=0(무제한)으로 안 하면 aiohttp 커넥터 자체의 기본 동시연결 한도(100)에
    # 걸려서 클라이언트 쪽이 먼저 병목이 되고, 그러면 이것도 결국 closed-loop가 된다.
    connector = aiohttp.TCPConnector(limit=0)
    summary = []
    all_raw = []
    async with aiohttp.ClientSession(connector=connector) as session:
        for stage in config["stages"]:
            result, raw = await run_stage(session, target["url"], payload, stage["rps"], stage["duration_sec"], stage["name"])
            summary.append(result)
            all_raw.extend(raw)

    # 모든 행에 동일 run_id/scenario/method/repetition을 찍어야 나중에 Prometheus·
    # policy 로그와 조인하거나 3-way(self-healing/고정임계치/제안방식) 비교를 위해
    # 반복(rep) 단위로 필터링할 수 있다(guideline.md 9-2절, 9-6절).
    tag = {"experiment_run_id": run_id, "scenario": scenario, "method": method, "repetition": repetition}
    for row in summary:
        row.update(tag)
    for row in all_raw:
        row.update(tag)

    out_dir = Path(__file__).parent / "results"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    tag_fields = ["experiment_run_id", "scenario", "method", "repetition"]

    summary_file = out_dir / f"load-ramp-{ts}.csv"
    with summary_file.open("w", newline="", encoding="utf-8") as f:
        fieldnames = tag_fields + [k for k in summary[0].keys() if k not in tag_fields]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(summary)

    # 요청 단위 원시 로그 — stage 안에서 시간 흐름에 따라 성공률이 서서히
    # 나빠지는지(점진적 열화) 순간적으로 무너지는지(절벽)를 나중에 구분하려면
    # stage 요약만으론 안 되고 이 타임라인이 있어야 한다.
    raw_file = out_dir / f"load-ramp-{ts}-raw.csv"
    fieldnames = tag_fields + ["stage", "sent_at", "latency", "status", "success"]
    with raw_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_raw)

    print(f"run_id: {run_id} / scenario: {scenario} / method: {method} / rep: {repetition}")

    print(f"\n결과 저장: {summary_file}")
    print(f"원시 로그 저장: {raw_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).parent.parent / "scenario-load-ramp.yaml"))
    parser.add_argument("--run-id", default=None, help="미지정 시 UTC 타임스탬프로 자동 생성 (오케스트레이터가 여러 로그를 조인할 때 지정)")
    parser.add_argument("--method", default="manual", help="3-way 비교 축: self-healing / fixed-threshold / proposed 등 (기본: manual 단독 실행)")
    parser.add_argument("--rep", type=int, default=1, help="반복 실행 번호 (기본: 1)")
    args = parser.parse_args()
    asyncio.run(main(args.config, run_id=args.run_id, method=args.method, repetition=args.rep))
