#!/usr/bin/env python3
"""Pod 별 처리 중 요청 수 기록기 (2026-10-23, 관측 전용 - 제어에 쓰지 않는다). 프로브 파드에서 실행.
각 대상 Pod IP 의 /metrics 를 interval 초마다 읽어 vllm:num_requests_running / waiting 을 JSONL 로 남긴다. 읽기 실패는 err 로 남기고 값을 채우지 않는다.
사용: python pod_poll.py --targets old=10.0.0.1:8000,new=10.0.0.2:8000 --interval 2 --duration 300 --out /poll.jsonl
"""
import argparse
import json
import re
import time
import urllib.request
from datetime import datetime, timezone

PAT = {"running": re.compile(r"^vllm:num_requests_running\{[^}]*\}\s+([0-9.eE+-]+)", re.M),
       "waiting": re.compile(r"^vllm:num_requests_waiting\{[^}]*\}\s+([0-9.eE+-]+)", re.M)}


def read(addr):
    with urllib.request.urlopen(f"http://{addr}/metrics", timeout=3) as r:
        txt = r.read().decode("utf-8", "replace")
    out = {}
    for k, p in PAT.items():
        m = p.search(txt)
        out[k] = float(m.group(1)) if m else None
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", required=True)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--duration", type=float, default=300.0)
    ap.add_argument("--out", default="/poll.jsonl")
    a = ap.parse_args()
    targets = dict(t.split("=") for t in a.targets.split(","))
    end = time.monotonic() + a.duration
    with open(a.out, "w", encoding="utf-8") as f:
        while time.monotonic() < end:
            t0 = time.monotonic()
            for name, addr in targets.items():
                rec = {"at": datetime.now(timezone.utc).isoformat(), "target": name}
                try:
                    rec.update(read(addr))
                except Exception as e:  # noqa: BLE001
                    rec["err"] = type(e).__name__
                f.write(json.dumps(rec) + "\n")
                f.flush()
            time.sleep(max(0.0, a.interval - (time.monotonic() - t0)))


if __name__ == "__main__":
    main()
