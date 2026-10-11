#!/usr/bin/env python3
"""Stage 0 I/O feasibility - 노드 디스크 쓰기 부하(새 파일, 2026-10-09). 계약: docs/design/phase8-iof-stage0-contract-20261009.md

별도 Pod(active vLLM 노드 고정, vLLM 컨테이너 밖)에서 emptyDir(노드 루트 디스크)에 O_DIRECT 순차 쓰기만 한다.
강도 축은 하나: 목표 쓰기 속도(MB/s, 1 MB = 1e6 B). 블록 1 MiB, 쓰기 프로세스 1개, 파일은 1 GiB를 순환 덮어쓰기
(디스크를 채우지 않음 - emptyDir sizeLimit 2Gi가 추가 상한). 블록 k의 예정 시각 = t0 + k × 블록크기 / 목표속도.
디스크가 목표를 못 따라가면 쓰기가 막히는 만큼 늦어진다(실제 달성 속도는 초 단위 로그로 기록).
종료: 지속 시간 끝, 정지 파일(/iotest/STOP) 존재, 오류(O_DIRECT 미지원 등 - exit 3). 끝나면 데이터 파일 삭제."""
import argparse
import csv
import mmap
import os
import sys
import time

BLOCK = 1 << 20
FILE_BLOCKS = 1024          # 1 GiB 순환
MB = 1_000_000


def due_offsets(rate_mbps: float, duration_sec: float) -> list:
    """블록별 예정 시각(시작 기준 초) - 순수 함수(테스트용)."""
    step = BLOCK / (rate_mbps * MB)
    return [k * step for k in range(int(duration_sec / step))]


def run(rate_mbps, duration_sec, path, log_path, stop_path):  # pragma: no cover - pod 안 실행
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_DIRECT, 0o600)
    except OSError as e:
        print(f"O_DIRECT open 실패: {e}", file=sys.stderr, flush=True)
        return 3
    buf = mmap.mmap(-1, BLOCK)          # 페이지 정렬 버퍼(O_DIRECT 요건)
    buf.write(os.urandom(BLOCK))
    step = BLOCK / (rate_mbps * MB)
    t0 = time.monotonic()
    k, rows, sec, sec_bytes, rc = 0, [], 0, 0, 0
    try:
        while True:
            now = time.monotonic() - t0
            if now >= duration_sec or os.path.exists(stop_path):
                break
            if int(now) > sec:
                rows.append({"sec": sec, "bytes": sec_bytes, "wall_utc": round(time.time(), 3)})
                sec, sec_bytes = int(now), 0
            due = k * step
            if due > now:
                time.sleep(min(due - now, 0.05))
                continue
            os.pwrite(fd, buf, (k % FILE_BLOCKS) * BLOCK)
            k += 1
            sec_bytes += BLOCK
    except OSError as e:
        print(f"쓰기 오류: {e}", file=sys.stderr, flush=True)
        rc = 3
    finally:
        os.close(fd)
        rows.append({"sec": sec, "bytes": sec_bytes, "wall_utc": round(time.time(), 3)})
        with open(log_path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["sec", "bytes", "wall_utc"])
            w.writeheader()
            w.writerows(rows)
        try:
            os.remove(path)
        except OSError:
            pass
    elapsed = time.monotonic() - t0
    print(f"blocks={k} bytes={k * BLOCK} elapsed={elapsed:.1f} achieved_mbps={k * BLOCK / MB / max(elapsed, 1e-9):.3f}", flush=True)
    return rc


if __name__ == "__main__":  # pragma: no cover
    p = argparse.ArgumentParser()
    p.add_argument("--rate-mbps", type=float, required=True)
    p.add_argument("--duration-sec", type=float, required=True)
    p.add_argument("--path", default="/iotest/data.bin")
    p.add_argument("--log", required=True)
    p.add_argument("--stop-file", default="/iotest/STOP")
    a = p.parse_args()
    sys.exit(run(a.rate_mbps, a.duration_sec, a.path, a.log, a.stop_file))
