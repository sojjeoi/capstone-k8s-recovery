#!/usr/bin/env python3
"""§86.6/§88.6 - no-action live smoke 전용 capture sink. `score_server.py`
가 보내는 POST를 받기만 하고 저장할 뿐, promotion이나 recovery-policy로의
전달을 절대 하지 않는다(그런 코드 자체가 없음 - 표준 라이브러리
`http.server`만 사용, 실제 signal 처리 로직 없음). `RECOVERY_POLICY_
SIGNAL_URL` 환경변수를 이 sink의 주소로 덮어써서 score_server.py를
그대로 재사용한다(score_server.py 자체는 변경하지 않음).

§88.6 - §87 사고(낡은 stray sink가 포트를 계속 점유한 채 살아있었는데
`HTTPServer.allow_reuse_address=1`(표준 라이브러리 기본값) 때문에 새
프로세스가 그 사실을 못 알아챔) 재발 방지: (1) `allow_reuse_address=False`
로 명시 고정해 포트 충돌 시 조용히 넘어가지 않고 bind()가 즉시
예외를 던지게 한다, (2) `--run-id`를 필수로 받아 payload의
`experiment_run_id`가 다르면 별도 `-rejected.jsonl`에 남기고 메인
캡처 파일에는 안 섞는다, (3) 시작 시 자기 PID를 pidfile에 남긴다."""
import argparse
import json
import os
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

_lock = threading.Lock()


class _StrictHTTPServer(HTTPServer):
    """§88.6 - `allow_reuse_address=1`(http.server 기본값)은 SO_REUSEADDR를
    켜서 이미 다른 프로세스가 점유 중인 포트에도 bind()가 조용히
    성공하는 경우가 있다(플랫폼에 따라 다름, §87 사고의 근본 원인 중
    하나) - 명시적으로 꺼서 포트 충돌 시 bind() 자체가 fail-closed로
    예외를 던지게 고정한다."""
    allow_reuse_address = False


def _flush_write(f, record: dict) -> None:
    f.write(json.dumps(record, ensure_ascii=False) + "\n")
    f.flush()
    try:
        os.fsync(f.fileno())
    except OSError:
        pass


def _make_handler(out_path: Path, rejected_path: Path, expected_run_id: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # 기본 stderr 접근 로그는 끄고 아래 커스텀 출력만 남김

        def do_GET(self):
            if self.path == "/healthz":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            received_at = datetime.now(timezone.utc).isoformat()
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {"_raw": body.decode("utf-8", errors="replace")}
            record = {
                "received_at_utc": received_at, "path": self.path,
                "source_address": f"{self.client_address[0]}:{self.client_address[1]}",
                "payload": payload,
            }
            payload_run_id = payload.get("experiment_run_id") if isinstance(payload, dict) else None
            is_expected_run = payload_run_id == expected_run_id
            record["run_id_match"] = is_expected_run

            with _lock:
                target = out_path if is_expected_run else rejected_path
                with target.open("a", encoding="utf-8") as f:
                    _flush_write(f, record)

            tag = "수신(저장만 - promotion/전달 없음)" if is_expected_run else \
                f"거부(run_id 불일치 - expected={expected_run_id!r}, got={payload_run_id!r}, 별도 파일에 기록)"
            print(f"[capture_sink] {tag}: {record}")
            self.send_response(200 if is_expected_run else 202)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"captured"}' if is_expected_run else b'{"status":"rejected_wrong_run_id"}')

    return Handler


def main():
    parser = argparse.ArgumentParser(description="§86.6/§88.6 no-action live smoke capture sink")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--out", required=True, help="수신 signal을 JSON lines로 저장할 경로")
    parser.add_argument("--run-id", required=True,
                         help="§88.6 - 이 값과 일치하는 experiment_run_id만 --out에 기록, 나머지는 "
                              "<out>.rejected.jsonl에 별도 기록(기본값 없음, fail-closed)")
    parser.add_argument("--pidfile", default=None, help="§88.6 - 자기 PID를 기록할 경로(선택)")
    args = parser.parse_args()

    out_path = Path(args.out)
    rejected_path = out_path.with_name(out_path.name + ".rejected.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        server = _StrictHTTPServer(("127.0.0.1", args.port), _make_handler(out_path, rejected_path, args.run_id))
    except OSError as e:
        print(f"[capture_sink] fail-closed: 포트 {args.port} bind 실패(이미 사용 중일 가능성) - {e}")
        sys.exit(1)

    if args.pidfile:
        Path(args.pidfile).write_text(str(os.getpid()), encoding="utf-8")

    print(f"[capture_sink] 시작 - PID={os.getpid()}, 127.0.0.1:{args.port}, run_id={args.run_id}, "
          f"저장 경로={out_path}(불일치 run_id는 {rejected_path}) "
          f"(promotion·recovery-policy 전달 코드 없음, 수신만 함)")
    server.serve_forever()


if __name__ == "__main__":
    main()
