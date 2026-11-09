#!/usr/bin/env python3
"""계측 probe (2026-10-23). 이미지 loadgen-runner:phase8-v4-forceclose-20261002 의 /probe.py(sha256 6d610450…, 사본:
results/hpa_compare/notes/image-probe-forceclose-20261002.py)와 요청 방식이 같다 - 1 RPS open-loop, force_close=True(요청마다 새 연결),
total timeout 30 s, payload·CSV 8열 동일. 파드에서 /probe.py 자리에 올려 실행되므로 SLO 판정 경로(CSV 읽기)는 변하지 않는다.
추가분(--evidence-out): 요청마다 request_id, 전송 시각, 응답 헤더 수신 시각, 완료/실패 시각, 클라이언트 소켓 주소(ip, port), 응답 id,
오류 종류. 소켓 주소는 vLLM 접속 로그의 '클라이언트IP:포트'와 대조해 실제 처리 Pod 를 특정하는 데 쓴다(응답 헤더를 못 받은 요청은 주소 미상).
"""
import argparse
import asyncio
import contextvars
import csv
import json
import socket
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import yaml

sys.stdout.reconfigure(encoding="utf-8")

FIELDNAMES = ["experiment_run_id", "scenario", "arm", "repetition", "sent_at", "latency", "status", "success"]


def now():
    return datetime.now(timezone.utc).isoformat()


# 짧은 응답은 헤더를 받는 시점에 연결이 이미 반환돼 resp.connection 이 None 이다. 그래서 소켓을 만드는 순간(socket_factory)에
# 요청별 태스크의 컨텍스트에 클라이언트 포트를 적는다(요청마다 새 연결이므로 포트가 요청을 구별한다). 클라이언트 IP 는 파드 IP 하나다.
_CUR = contextvars.ContextVar("cur_request", default=None)
POD_IP = None
KEEPALIVE = False          # --keepalive: 연결 재사용 모드(2026-10-23). 재사용한 요청은 새 연결을 만들지 않아 포트가 기록되지 않으므로 직전 새 연결의 포트로 가정한다.
_LAST = {"port": None}


def _socket_factory(addr_info):
    fam, typ, proto, _, _addr = addr_info
    sk = socket.socket(fam, typ, proto)
    sk.setblocking(False)
    sk.bind(("", 0))
    _LAST["port"] = sk.getsockname()[1]
    cur = _CUR.get()
    if cur is not None:
        cur["client_port"] = _LAST["port"]
    return sk


async def _fire_and_log(session, url, payload, writer, f, tag, ev):
    rid = uuid.uuid4().hex
    sent_at = now()
    start = time.monotonic()
    ev({"event": "sent", "request_id": rid, "sent_at": sent_at})
    cur = {}
    _CUR.set(cur)
    info = {}
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            info = {"headers_at": now()}
            body = await resp.read()
            try:
                info["response_id"] = json.loads(body).get("id")
            except Exception:  # noqa: BLE001
                info["response_id"] = None
            row = {"sent_at": sent_at, "latency": time.monotonic() - start,
                   "status": resp.status, "success": resp.status == 200}
    except Exception as e:  # noqa: BLE001
        row = {"sent_at": sent_at, "latency": time.monotonic() - start,
               "status": None, "success": False}
        info["error"] = type(e).__name__ + ": " + str(e)[:200]
    ev({"event": "done", "request_id": rid, "sent_at": sent_at, "done_at": now(), "latency": row["latency"],
        "status": row["status"], "success": row["success"], "client_ip": POD_IP,
        "client_port": cur.get("client_port") or (_LAST["port"] if KEEPALIVE else None),
        "port_source": "new_connection" if cur.get("client_port") else ("reused_assumed_last_connection" if KEEPALIVE else None), **info})
    row.update(tag)
    writer.writerow(row)
    f.flush()


async def main(config_path, run_id, scenario, arm, rep, out_path, duration_sec, evidence_path, keepalive=False):
    config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    target = config["target"]
    payload = {"model": target["model"], "prompt": target["prompt"], "max_tokens": target["max_tokens"]}
    rps = config.get("rps", 1)
    tag = {"experiment_run_id": run_id, "scenario": scenario, "arm": arm, "repetition": rep}

    out_f = open(out_path, "w", newline="", encoding="utf-8")
    writer = csv.DictWriter(out_f, fieldnames=FIELDNAMES, extrasaction="ignore")
    writer.writeheader()
    out_f.flush()
    ev_f = open(evidence_path, "w", encoding="utf-8")

    def ev(d):
        ev_f.write(json.dumps(d, ensure_ascii=False) + "\n")
        ev_f.flush()

    global POD_IP, KEEPALIVE
    POD_IP = socket.gethostbyname(socket.gethostname())
    KEEPALIVE = keepalive
    connector = aiohttp.TCPConnector(limit=0, force_close=not keepalive, socket_factory=_socket_factory)
    interval = 1.0 / rps
    start_time = time.monotonic()
    next_fire = start_time
    tasks = []
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            while time.monotonic() - start_time < duration_sec:
                now_m = time.monotonic()
                if now_m >= next_fire:
                    tasks.append(asyncio.create_task(
                        _fire_and_log(session, target["url"], payload, writer, out_f, tag, ev)))
                    next_fire += interval
                else:
                    await asyncio.sleep(min(0.01, next_fire - now_m))
            if tasks:
                await asyncio.wait(tasks, timeout=10)
    finally:
        out_f.close()
        ev_f.close()
    print(f"probe 종료: {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SLO 판정용 저율 open-loop probe (계측 판)")
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--arm", required=True)
    parser.add_argument("--rep", type=int, required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--duration-sec", type=float, required=True)
    parser.add_argument("--evidence-out", default="/probe-evidence.jsonl")
    parser.add_argument("--keepalive", action="store_true", help="연결 재사용(기본은 요청마다 새 연결 = 기존 force_close 와 동일)")
    args = parser.parse_args()
    asyncio.run(main(args.config, args.run_id, args.scenario, args.arm, args.rep, args.out, args.duration_sec, args.evidence_out, args.keepalive))
