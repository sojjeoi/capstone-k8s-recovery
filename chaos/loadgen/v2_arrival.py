#!/usr/bin/env python3
"""AIOps v2 workload feasibility - 단계 하나만 실행하는 jittered-grid open-loop 부하(새 파일, 2026-10-09).

계약: docs/design/phase8-aiops-v2-workload-feasibility-contract-20261009.md §2.1
동결 ramp.py는 고정 간격(next_fire += 1/rps)이라 5·10·20·40 s 주기가 15 s Prometheus scrape와 위상 고정(aliasing)된다.
이 파일은 발사 시각 계획 하나만 바꾼다 - 요청 전송·모양(model/prompt/max_tokens)·timeout은 ramp.fire_request를 그대로 쓴다.

규칙 jittered_grid_v1: N = ceil(rps * duration), slot = duration / N, k번째 요청 = (k + U_k) * slot,
U_k = random.Random(seed).random() 순서대로. 단계마다 요청 수가 고정(달성 RPS = N/duration)이고, 각 요청의 위상은
slot 안에서 균등 → scrape 격자와 주기적으로 동기화되지 않는다. seed가 같으면 계획이 같다(재현).
pod 안에서는 /ramp.py(이미지 내장, 동결 sha256 aadab9fc…) 옆 /v2_arrival.py로 복사돼 실행된다."""
import argparse
import asyncio
import csv
import math
import random
import time
from datetime import datetime, timezone
from pathlib import Path

RULE = "jittered_grid_v1"
STRAGGLER_WAIT_SEC = 10  # ramp.py와 같은 단계 끝 대기


def schedule(rps: float, duration_sec: float, seed: int) -> list:
    """단계 시작 기준 발사 시각(초) 계획. 순수 함수 - 실행기·분석기·테스트가 같은 값을 재계산해 대조한다."""
    n = math.ceil(round(rps * duration_sec, 9))
    slot = duration_sec / n
    rng = random.Random(seed)
    return [round((k + rng.random()) * slot, 6) for k in range(n)]


def stage_seed(run_seed: int, stage_index: int) -> int:
    return run_seed * 10 + stage_index


async def run(config_path, stage_index, run_seed, summary_out, raw_out):  # pragma: no cover - pod 안 실행
    import aiohttp
    import yaml

    import ramp  # 동결 ramp.py(같은 디렉터리) - 요청 전송 함수 재사용
    cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    st, t = cfg["stages"][stage_index - 1], cfg["target"]
    payload = {"model": t["model"], "prompt": t["prompt"], "max_tokens": t["max_tokens"]}
    seed = stage_seed(run_seed, stage_index)
    plan = schedule(st["rps"], st["duration_sec"], seed)
    results, sends, tasks = [], [], []
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0)) as session:
        t0 = time.monotonic()
        start_utc = datetime.now(timezone.utc)
        print(f"=== {st['name']}: {st['rps']} RPS, {st['duration_sec']}s, seed {seed} ===", flush=True)
        for k, off in enumerate(plan):
            await asyncio.sleep(max(0.0, t0 + off - time.monotonic()))
            sent_at = datetime.now(timezone.utc).isoformat()
            sends.append({"k": k, "scheduled_offset_sec": off, "sent_at": sent_at,
                          "send_lag_sec": round(time.monotonic() - t0 - off, 6)})
            tasks.append(asyncio.create_task(ramp.fire_request(session, t["url"], payload, results, sent_at)))
        await asyncio.sleep(max(0.0, t0 + st["duration_sec"] - time.monotonic()))
        _, pending = await asyncio.wait(tasks, timeout=STRAGGLER_WAIT_SEC) if tasks else (set(), set())
        for p in pending:
            p.cancel()
    end_utc = datetime.now(timezone.utc)
    by_sent = {r["sent_at"]: r for r in results}
    for s in sends:
        r = by_sent.get(s["sent_at"], {})
        s.update(latency=r.get("latency"), status=r.get("status"), success=bool(r.get("success")))
    with open(raw_out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["k", "scheduled_offset_sec", "sent_at", "send_lag_sec", "latency", "status", "success"])
        w.writeheader()
        w.writerows(sends)
    row = {"stage": st["name"], "target_rps": st["rps"], "duration_sec": st["duration_sec"], "arrival_rule": RULE,
           "seed": seed, "planned": len(plan), "sent": len(sends), "success": sum(s["success"] for s in sends),
           "actual_rps": round(len(sends) / st["duration_sec"], 6),
           "max_send_lag_sec": max((s["send_lag_sec"] for s in sends), default=None),
           "stage_start_utc": start_utc.isoformat(), "stage_end_utc": end_utc.isoformat()}
    with open(summary_out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(row))
        w.writeheader()
        w.writerow(row)


if __name__ == "__main__":  # pragma: no cover
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--stage", type=int, required=True, help="1부터")
    p.add_argument("--run-seed", type=int, required=True)
    p.add_argument("--summary-out", required=True)
    p.add_argument("--raw-out", required=True)
    a = p.parse_args()
    asyncio.run(run(a.config, a.stage, a.run_seed, a.summary_out, a.raw_out))
