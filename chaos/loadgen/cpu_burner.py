#!/usr/bin/env python3
"""noisy-neighbor CPU 부하(nnf Stage 0, counted=false). vLLM 밖 별도 pod 안에서 busy 프로세스 N개를 duration 동안 돌린다.
강도 축은 --workers 하나(pod CPU request·limit은 실행기가 고정). STOP 파일이 생기면 일찍 끝난다.
로그 CSV: sec, alive(살아 있는 worker 수), wall_utc. 종료 코드 0 = 모든 worker 정상 종료, 3 = 오류."""
import argparse
import csv
import multiprocessing as mp
import os
import sys
import time
from datetime import datetime, timezone


def spin(stop_at: float, stop_file: str) -> None:
    n = 0
    while True:
        n += 1
        if n % 200000 == 0 and (time.monotonic() >= stop_at or os.path.exists(stop_file)):
            return


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--workers", type=int, required=True)
    p.add_argument("--duration-sec", type=float, required=True)
    p.add_argument("--log", required=True)
    p.add_argument("--stop-file", default="/tmp/STOP")
    a = p.parse_args(argv)
    if a.workers < 1 or a.duration_sec <= 0:
        print("bad args", file=sys.stderr)
        return 3
    t0 = time.monotonic()
    procs = [mp.Process(target=spin, args=(t0 + a.duration_sec, a.stop_file)) for _ in range(a.workers)]
    for x in procs:
        x.start()
    with open(a.log, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["sec", "alive", "wall_utc"])
        sec = 0
        while any(x.is_alive() for x in procs):
            time.sleep(max(0.0, t0 + sec + 1 - time.monotonic()))
            w.writerow([sec, sum(x.is_alive() for x in procs), datetime.now(timezone.utc).isoformat()])
            fh.flush()
            sec += 1
    for x in procs:
        x.join()
    return 0 if all(x.exitcode == 0 for x in procs) else 3


if __name__ == "__main__":
    sys.exit(main())
