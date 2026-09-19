#!/usr/bin/env python3
"""calibration_node_probe.py 검증(2026-09-19) - 로컬 가짜 HTTP 서버(지연 주입)에 대고 실제 스크립트를 ssh와
같은 방식(`python -` + stdin)으로 실행해, 지연 측정·발행 스케줄 유지·느린 응답이 다음 요청을 막지 않는지·
실패/timeout 기록을 고정한다. 클러스터·외부 네트워크에 접근하지 않는다(127.0.0.1만)."""
import http.server
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import calibration_node_probe as cnp

SCRIPT_TEXT = Path(cnp.__file__).read_text(encoding="utf-8")


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        time.sleep(self.server.delay)
        self._reply(200, {"status": "ok"})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        time.sleep(self.server.delay)
        self._reply(200, self.server.post_body)


@pytest.fixture
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.delay = 0.0
    srv.post_body = {"choices": [{"text": "x"}]}
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _run(port, **overrides):
    params = {"ip": "127.0.0.1", "port": port, "duration_sec": 2.0, "health_interval_sec": 0.5,
              "completion_interval_sec": 1.0, "health_timeout_sec": 5, "completion_timeout_sec": 5,
              "completion_payload": {"model": "m", "prompt": "Hi", "max_tokens": 1}}
    params.update(overrides)
    proc = subprocess.run([sys.executable, "-", cnp.encode_params(params)], input=SCRIPT_TEXT,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    records = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    summary = [r for r in records if r["kind"] == "summary"]
    assert len(summary) == 1, "마지막에 summary가 정확히 한 줄 있어야 함"
    by_kind = {k: sorted((r for r in records if r["kind"] == k), key=lambda r: r["seq"])
               for k in ("health", "completion")}
    return by_kind, summary[0]


def test_measures_latency_and_keeps_the_issue_schedule(server):
    server.delay = 0.3
    by_kind, summary = _run(server.server_port, duration_sec=3.0)
    health, completion = by_kind["health"], by_kind["completion"]
    assert len(health) == 6 and len(completion) == 3, "0.5초 간격 3초 = 6개, 1초 간격 = 3개"
    assert summary["issued"] == {"health": 6, "completion": 3}
    for r in health + completion:
        assert r["status"] == 200 and r["error"] is None, r
        assert 0.3 <= r["latency"] < 1.0, f"주입한 지연 0.3초가 측정에 반영돼야 함: {r}"
    assert [r["t"] for r in health] == [0.0, 0.5, 1.0, 1.5, 2.0, 2.5], "발행 시각은 응답과 무관하게 일정"


def test_slow_responses_do_not_block_later_requests(server):
    server.delay = 1.2  # 발행 간격(0.3초)보다 훨씬 느린 응답
    by_kind, summary = _run(server.server_port, duration_sec=1.5, health_interval_sec=0.3,
                            completion_interval_sec=0)
    health = by_kind["health"]
    assert len(health) == 5, "느린 응답을 기다리느라 발행이 밀리면 안 됨(0, 0.3, ..., 1.2)"
    assert all(r["latency"] >= 1.2 for r in health)
    assert summary["elapsed"] >= 1.2 + 1.2 - 0.05, "마지막 요청의 응답까지 기다린 뒤 끝나야 함"
    assert by_kind["completion"] == [] and summary["issued"]["completion"] == 0, "간격 0이면 completion 끔"


def test_connection_failures_are_recorded_with_error_and_no_status():
    with socket.socket() as s:  # 아무도 듣지 않는 포트
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
    by_kind, _ = _run(closed_port, duration_sec=1.0)
    for r in by_kind["health"] + by_kind["completion"]:
        assert r["status"] is None and r["error"], r
        assert r["latency"] < 10.0  # Windows는 닫힌 포트 연결 거부에도 재시도로 약 2초가 걸린다


def test_timeout_is_recorded_as_error_at_the_timeout_latency(server):
    server.delay = 2.0
    by_kind, _ = _run(server.server_port, duration_sec=1.0, health_interval_sec=1.0,
                      completion_interval_sec=0, health_timeout_sec=0.5)
    (r,) = by_kind["health"]
    assert r["status"] is None and "timed out" in r["error"], r
    assert 0.5 <= r["latency"] < 1.5


def test_completion_without_choices_is_an_error(server):
    server.post_body = {"error": "x"}
    by_kind, _ = _run(server.server_port, duration_sec=1.0, health_interval_sec=0)
    (r,) = by_kind["completion"]
    assert r["status"] == 200 and "choices" in r["error"], "200이어도 choices가 없으면 성공이 아님"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
