#!/usr/bin/env python3
"""후속 고정관측구간 프로토콜 전용 probe (§137 이후 지시 §3). probe.py를
그대로 베이스로 하되(부하량·payload·request timeout(30s)·connector 재사용
정책(TCPConnector(limit=0)) 전부 동일 - 절대 변경하지 않는다), "요청이
완료돼야만 로그에 나타나는" 문제를 고친다: 각 요청에 request_id를 부여하고
(1) 발신 즉시 sent 이벤트, (2) 완료 시 completed/timeout/cancelled 이벤트를
같은 request_id로 남긴다.

기존 probe.py는 건드리지 않는다(공식 하니스 기본 동작 무변경) - 이 파일은
완전히 별도이고, 후속 비교에서만 쓴다.

산출물 2종:
  --out: 기존 probe.py와 100% 호환되는 CSV(completed_at 없이 sent_at/
         latency/status/success만 - slo_judge.load_raw()가 그대로 읽음).
         SLO 판정에 unresolved 요청을 성공/실패/가짜 latency로 채워 넣지
         않는다 - 완료된 요청만 이 파일에 한 행씩 남는다(기존과 동일 의미).
  --evidence-out: request_id 기준 전체 이벤트(sent/completed/timeout/
         cancelled/unresolved) JSONL - 관측 cohort·미완료 처리 판단은
         이 파일로 한다.
"""
import argparse
import asyncio
import csv
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import yaml

sys.stdout.reconfigure(encoding="utf-8")

FIELDNAMES = ["experiment_run_id", "scenario", "arm", "repetition", "sent_at", "latency", "status", "success"]

# probe.py와 동일값 - 후속 실험이라고 임의로 바꾸지 않는다(비교 계약 §5).
REQUEST_TIMEOUT_SEC = 30
TCP_CONNECTOR_LIMIT = 0  # probe.py와 동일: 무제한, keepalive는 aiohttp 기본값 그대로(변경 없음)

# 관측 종료 후 미완료 요청을 기다리는 grace(기존 probe.py의 10초보다 넉넉하게 -
# "미완료"를 더 정확히 구분하기 위함이지, SLO 판정 자체를 바꾸는 게 아니다).
GRACE_SEC = 60.0


def _evidence_writer(path):
    f = open(path, "w", newline="", encoding="utf-8")

    def write(event: dict):
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
        f.flush()

    return write, f


async def _fire_and_log(session, url, payload, csv_writer, csv_f, evidence_write, tag, seen_ids: set):
    request_id = uuid.uuid4().hex
    if request_id in seen_ids:
        # 이론상 uuid4 충돌은 무시할 수준이지만, 중복 탐지 규칙 자체는
        # 명시적으로 존재해야 한다(사용자 지시 §7 - request_id 중복 탐지).
        raise RuntimeError(f"request_id 중복 생성: {request_id}")
    seen_ids.add(request_id)

    sent_wall = datetime.now(timezone.utc).isoformat()
    sent_mono = time.monotonic()
    evidence_write({"request_id": request_id, "event": "sent", "sent_at": sent_wall, **tag})

    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SEC)) as resp:
            await resp.read()
            elapsed = time.monotonic() - sent_mono
            row = {"sent_at": sent_wall, "latency": elapsed, "status": resp.status, "success": resp.status == 200}
            evidence_write({"request_id": request_id, "event": "completed", "sent_at": sent_wall,
                             "completed_at": datetime.now(timezone.utc).isoformat(),
                             "elapsed_monotonic_sec": elapsed, "http_status": resp.status,
                             "success": resp.status == 200, **tag})
    except asyncio.CancelledError:
        elapsed = time.monotonic() - sent_mono
        evidence_write({"request_id": request_id, "event": "cancelled", "sent_at": sent_wall,
                         "elapsed_monotonic_sec": elapsed, **tag})
        raise
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - sent_mono
        row = {"sent_at": sent_wall, "latency": elapsed, "status": None, "success": False}
        evidence_write({"request_id": request_id, "event": "timeout", "sent_at": sent_wall,
                         "completed_at": datetime.now(timezone.utc).isoformat(),
                         "elapsed_monotonic_sec": elapsed, "http_status": None, "success": False, **tag})
    except Exception as e:
        elapsed = time.monotonic() - sent_mono
        row = {"sent_at": sent_wall, "latency": elapsed, "status": None, "success": False}
        evidence_write({"request_id": request_id, "event": "error", "sent_at": sent_wall,
                         "completed_at": datetime.now(timezone.utc).isoformat(),
                         "elapsed_monotonic_sec": elapsed, "http_status": None, "success": False,
                         "error": str(e), **tag})

    row.update(tag)
    csv_writer.writerow(row)
    csv_f.flush()


async def main(config_path, run_id, scenario, arm, rep, out_path, evidence_out_path, duration_sec,
                stop_file_path=None):
    """stop_file_path(선택, 기본 None - 미지정 시 기존과 100% 동일 동작):
    호출자(load_ramp_followup_adapter.py)가 관측을 끝내고 싶을 때 이 경로에
    파일을 만들면, 다음 발신 시각 확인 시점(최대 약 1초 이내)에 감지해 새
    요청 발신을 즉시 멈추고 그 아래의 동일한 grace-drain 절차로 넘어간다
    (score_server.py의 --stop-file과 같은 패턴 재사용 - 새로 만들지 않음).
    이게 없으면(원래 설계) run_once()가 관측을 끝내는 순간 호출자가 pod를
    강제 종료해, 아직 진행 중이던 요청이 grace 없이 그대로 끊길 위험이 있다
    (실측으로 발견 - §138 이후 지시 §3)."""
    config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    target = config["target"]
    payload = {"model": target["model"], "prompt": target["prompt"], "max_tokens": target["max_tokens"]}
    rps = config.get("rps", 1)
    tag = {"experiment_run_id": run_id, "scenario": scenario, "arm": arm, "repetition": rep}

    out_f = open(out_path, "w", newline="", encoding="utf-8")
    writer = csv.DictWriter(out_f, fieldnames=FIELDNAMES, extrasaction="ignore")
    writer.writeheader()
    out_f.flush()
    evidence_write, evidence_f = _evidence_writer(evidence_out_path)
    seen_ids = set()

    connector = aiohttp.TCPConnector(limit=TCP_CONNECTOR_LIMIT)
    interval = 1.0 / rps
    start_time = time.monotonic()
    next_fire = start_time
    tasks = []
    stop_reason = "duration_elapsed"
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            while time.monotonic() - start_time < duration_sec:
                now = time.monotonic()
                if now >= next_fire:
                    if stop_file_path is not None and os.path.exists(stop_file_path):
                        stop_reason = "stop_file"
                        break
                    tasks.append(asyncio.create_task(
                        _fire_and_log(session, target["url"], payload, writer, out_f, evidence_write, tag, seen_ids)))
                    next_fire += interval
                else:
                    await asyncio.sleep(min(0.01, next_fire - now))
            evidence_write({"event": "send_stopped", "reason": stop_reason,
                            "elapsed_sec": round(time.monotonic() - start_time, 3), **tag})
            if tasks:
                done, pending = await asyncio.wait(tasks, timeout=GRACE_SEC)
                for t in pending:
                    # grace 안에도 안 끝난 요청 - 강제 취소하고 "unresolved"로
                    # 명시 기록한다(성공/실패/가짜 latency로 채우지 않음).
                    t.cancel()
                if pending:
                    await asyncio.wait(pending, timeout=5)
                    evidence_write({"event": "unresolved_summary", "n_unresolved_after_grace": len(pending), **tag})
    finally:
        out_f.close()
        evidence_f.close()
    print(f"probe_followup 종료({stop_reason}): {out_path} (evidence: {evidence_out_path})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="후속 고정관측구간 프로토콜용 SLO probe (request_id 기반 sent/complete 분리)")
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--arm", required=True)
    parser.add_argument("--rep", type=int, required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--evidence-out", required=True)
    parser.add_argument("--duration-sec", type=float, required=True)
    parser.add_argument("--stop-file", default=None,
                         help="지정하면 이 파일이 생기는 즉시(최대 약 1초 지연) 새 요청 발신을 멈추고 "
                              "grace-drain으로 넘어간다. 기본값 None이면 기존과 동일 동작.")
    args = parser.parse_args()
    asyncio.run(main(args.config, args.run_id, args.scenario, args.arm, args.rep,
                      args.out, args.evidence_out, args.duration_sec, stop_file_path=args.stop_file))
