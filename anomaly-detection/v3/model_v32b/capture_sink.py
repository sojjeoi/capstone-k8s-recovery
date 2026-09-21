#!/usr/bin/env python3
"""§86.6 - no-action live smoke 전용 capture sink. `score_server.py`가
보내는 POST를 받기만 하고 저장할 뿐, promotion이나 recovery-policy로의
전달을 절대 하지 않는다(그런 코드 자체가 없음 - 표준 라이브러리
`http.server`만 사용, 실제 signal 처리 로직 없음). `RECOVERY_POLICY_
SIGNAL_URL` 환경변수를 이 sink의 주소로 덮어써서 score_server.py를
그대로 재사용한다(score_server.py 자체는 변경하지 않음)."""
import argparse
import json
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

_lock = threading.Lock()


def _make_handler(out_path: Path):
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
            record = {"received_at_utc": received_at, "path": self.path, "payload": payload}
            with _lock:
                with out_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(f"[capture_sink] 수신(저장만 - promotion/전달 없음): {record}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"captured"}')

    return Handler


def main():
    parser = argparse.ArgumentParser(description="§86.6 no-action live smoke capture sink")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--out", required=True, help="수신 signal을 JSON lines로 저장할 경로")
    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    server = HTTPServer(("127.0.0.1", args.port), _make_handler(out_path))
    print(f"[capture_sink] 시작 - 127.0.0.1:{args.port}, 저장 경로={out_path} "
          f"(promotion·recovery-policy 전달 코드 없음, 수신만 함)")
    server.serve_forever()


if __name__ == "__main__":
    main()
