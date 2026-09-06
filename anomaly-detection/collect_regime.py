#!/usr/bin/env python3
"""정상 상태 구간에 regime 라벨을 붙여 기록 — Isolation Forest 학습 데이터를
Prometheus에서 나중에 뽑아낼 때 어느 구간이 무슨 정상 상태였는지 표시해두기 위함.

실제 지표 값은 여기서 뽑지 않는다(Phase 6의 features.py가 이 시간창을 기준으로
Prometheus에 쿼리한다) — 이 스크립트는 "언제 어떤 정상 상태였는지"만 UTC RFC3339로
기록한다. 부하를 발생시키는 등 실제 동작은 이 스크립트가 대기하는 동안 별도로
(예: ramp.py, kubectl) 병행 실행한다.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # Windows 기본 cp949 콘솔이 "—" 등에서 죽는 것 방지

MANIFEST = Path(__file__).parent / "data" / "regimes.jsonl"


def record(regime: str, start: str, end: str, notes: str = "") -> None:
    MANIFEST.parent.mkdir(exist_ok=True)
    with MANIFEST.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"regime": regime, "start_utc": start, "end_utc": end, "notes": notes}, ensure_ascii=False) + "\n")
    print(f"기록: {regime} [{start} ~ {end}] {notes}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="정상 상태 구간을 라벨링해서 data/regimes.jsonl에 기록")
    parser.add_argument("regime", help="예: idle / warmup / low_load / sustained_load / burst / active_preview_concurrent / post_startup / gitops_sync")
    parser.add_argument("--duration", type=float, required=True, help="이 구간 길이(초) — 그동안 대기만 한다")
    parser.add_argument("--notes", default="")
    args = parser.parse_args()

    start = datetime.now(timezone.utc).isoformat()
    print(f"{args.regime} 구간 시작 — {args.duration}초 대기")
    time.sleep(args.duration)
    end = datetime.now(timezone.utc).isoformat()
    record(args.regime, start, end, args.notes)
