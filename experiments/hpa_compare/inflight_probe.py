#!/usr/bin/env python3
"""진행 중 요청 점검용 스트리밍 요청기 (2026-10-23, 기능 검증 전용 - SLO 0.648초 기준을 적용하지 않는다).
--plan "오프셋초:max_tokens,..." 대로 스트리밍(stream=true, ignore_eos=true) 요청을 보내고, 요청마다 다음을 evidence JSONL 에 남긴다:
sent / headers(첫 응답 헤더, 클라이언트 소켓 ip·port) / chunk 도착 시각(요청 시작 대비 오프셋 초 배열) / 끝(completed: [DONE] 수신,
interrupted: 중간에 연결 끊김·오류, timeout) 와 받은 청크 수. 청크 1개 = 토큰 1개 안팎(vLLM 스트림)이며 정확한 토큰 수라고 단정하지 않는다.
요청은 force_close=True(새 연결)로 보낸다 - 프로브와 같은 방식. 이미 연결된 요청은 Service 선택자가 바뀌어도 그 연결의 Pod 가 계속 처리한다.
"""
import argparse
import asyncio
import contextvars
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


def now():
    return datetime.now(timezone.utc).isoformat()


_CUR = contextvars.ContextVar("cur_request", default=None)


def _socket_factory(addr_info):
    """probe_trace.py 와 같은 방식: 소켓을 만드는 순간 요청별 컨텍스트에 클라이언트 포트를 적는다(요청마다 새 연결)."""
    fam, typ, proto, _, _addr = addr_info
    sk = socket.socket(fam, typ, proto)
    sk.setblocking(False)
    sk.bind(("", 0))
    cur = _CUR.get()
    if cur is not None:
        cur["client_port"] = sk.getsockname()[1]
    return sk


async def one(session, url, model, prompt, max_tokens, offset, t_start, ev, timeout_sec):
    await asyncio.sleep(max(0.0, t_start + offset - time.monotonic()))
    rid = uuid.uuid4().hex
    base = {"request_id": rid, "planned_offset_sec": offset, "max_tokens": max_tokens}
    cur = {}
    _CUR.set(cur)
    t0 = time.monotonic()
    ev({"event": "sent", "sent_at": now(), **base})
    chunks, finish, done_marker, status = [], None, False, None
    try:
        async with session.post(url, json={"model": model, "prompt": prompt, "max_tokens": max_tokens, "stream": True, "ignore_eos": True},
                                timeout=aiohttp.ClientTimeout(total=timeout_sec)) as resp:
            status = resp.status
            ev({"event": "headers", "at": now(), "status": status, "client_ip": socket.gethostbyname(socket.gethostname()),
                "client_port": cur.get("client_port"), **base})
            async for raw in resp.content:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    done_marker = True
                    break
                chunks.append(round(time.monotonic() - t0, 3))
                if len(chunks) % 10 == 0:
                    ev({"event": "progress", "at": now(), "n_chunks": len(chunks), **base})
                try:
                    finish = json.loads(data)["choices"][0].get("finish_reason") or finish
                except Exception:  # noqa: BLE001
                    pass
        # [DONE] 만 받고 finish_reason(length/stop)이 없으면 서버가 종료 중 요청을 abort 하고 스트림만 닫은 것이다 - '정상 완료'로 세지 않는다(2026-10-23 발견).
        outcome, err = (("completed" if finish in ("length", "stop") else "truncated_done") if (done_marker and status == 200) else "interrupted"), None
    except asyncio.TimeoutError:
        outcome, err = "timeout", "asyncio.TimeoutError"
    except Exception as e:  # noqa: BLE001
        outcome, err = "interrupted", type(e).__name__ + ": " + str(e)[:200]
    ev({"event": "end", "at": now(), "outcome": outcome, "error": err, "status": status, "finish_reason": finish, "done_marker": done_marker,
        "client_ip": socket.gethostbyname(socket.gethostname()), "client_port": cur.get("client_port"),
        "n_chunks": len(chunks), "first_chunk_sec": chunks[0] if chunks else None, "last_chunk_sec": chunks[-1] if chunks else None,
        "elapsed_sec": round(time.monotonic() - t0, 3), "chunk_offsets_sec": chunks, **base})


async def main(config_path, plan, out_path, timeout_sec, url=None):
    target = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))["target"]
    target["url"] = url or target["url"]
    items = [(float(a), int(b)) for a, b in (p.split(":") for p in plan.split(","))]
    f = open(out_path, "w", encoding="utf-8")

    def ev(d):
        f.write(json.dumps(d, ensure_ascii=False) + "\n")
        f.flush()

    t_start = time.monotonic()
    ev({"event": "plan_start", "at": now(), "plan": items})
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0, force_close=True, socket_factory=_socket_factory)) as s:
        await asyncio.gather(*[one(s, target["url"], target["model"], target["prompt"], mt, off, t_start, ev, timeout_sec) for off, mt in items])
    ev({"event": "plan_end", "at": now()})
    f.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--plan", required=True)
    ap.add_argument("--out", default="/inflight-evidence.jsonl")
    ap.add_argument("--timeout-sec", type=float, default=240.0)
    ap.add_argument("--url", default=None, help="config 의 url 대신 쓸 주소(예: vllm-preview 서비스)")
    a = ap.parse_args()
    asyncio.run(main(a.config, a.plan, a.out, a.timeout_sec, a.url))
