#!/usr/bin/env python3
"""§88.6 - capture_sink.py 오프라인 고정 테스트. 실제 loopback
서버(127.0.0.1)를 별도 스레드로 띄워 검증한다 - 외부 네트워크·클러스터
의존 없음."""
import json
import socket
import sys
import threading
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

import capture_sink as cs  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_server_in_thread(port: int, out_path: Path, run_id: str):
    server = cs._StrictHTTPServer(("127.0.0.1", port), cs._make_handler(out_path, out_path.with_name(out_path.name + ".rejected.jsonl"), run_id))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_bind_fails_closed_on_port_already_in_use(tmp_path):
    port = _free_port()
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", port))
    holder.listen(1)
    try:
        try:
            cs._StrictHTTPServer(("127.0.0.1", port), cs._make_handler(tmp_path / "out.jsonl", tmp_path / "rej.jsonl", "r1"))
            assert False, "이미 점유된 포트에 bind가 성공하면 안 됨"
        except OSError:
            pass
    finally:
        holder.close()
    print("OK - allow_reuse_address=False로 고정돼 있어 포트 충돌 시 bind()가 즉시 fail-closed")


def test_matching_run_id_goes_to_main_file(tmp_path):
    port = _free_port()
    out_path = tmp_path / "captured.jsonl"
    server, thread = _run_server_in_thread(port, out_path, "run-A")
    try:
        time.sleep(0.2)
        r = requests.post(f"http://127.0.0.1:{port}/signal", json={"experiment_run_id": "run-A", "score": -0.1})
        assert r.status_code == 200
        time.sleep(0.2)
        lines = out_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["run_id_match"] is True
        assert rec["payload"]["experiment_run_id"] == "run-A"
        assert "source_address" in rec
        rejected_path = out_path.with_name(out_path.name + ".rejected.jsonl")
        assert not rejected_path.exists()
    finally:
        server.shutdown()
        thread.join(timeout=5)
    print("OK - 일치하는 run_id는 메인 캡처 파일에 기록되고 source_address도 함께 남음")


def test_mismatched_run_id_isolated_to_rejected_file():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp_path = Path(d)
        port = _free_port()
        out_path = tmp_path / "captured.jsonl"
        server, thread = _run_server_in_thread(port, out_path, "run-A")
        try:
            time.sleep(0.2)
            r = requests.post(f"http://127.0.0.1:{port}/signal", json={"experiment_run_id": "run-B", "score": -0.1})
            assert r.status_code == 202  # 거부되지만 요청 자체는 받아 기록함(200이 아님을 구분)
            time.sleep(0.2)
            assert not out_path.exists() or out_path.read_text(encoding="utf-8").strip() == ""
            rejected_path = out_path.with_name(out_path.name + ".rejected.jsonl")
            rec = json.loads(rejected_path.read_text(encoding="utf-8").splitlines()[0])
            assert rec["run_id_match"] is False
            assert rec["payload"]["experiment_run_id"] == "run-B"
        finally:
            server.shutdown()
            thread.join(timeout=5)
    print("OK - 다른 run_id의 요청은 메인 파일과 격리된 별도 .rejected.jsonl에만 기록됨(오염 방지)")


def test_no_experiment_run_id_field_treated_as_mismatch():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp_path = Path(d)
        port = _free_port()
        out_path = tmp_path / "captured.jsonl"
        server, thread = _run_server_in_thread(port, out_path, "run-A")
        try:
            time.sleep(0.2)
            requests.post(f"http://127.0.0.1:{port}/signal", json={"score": -0.1, "test": True})
            time.sleep(0.2)
            assert not out_path.exists() or out_path.read_text(encoding="utf-8").strip() == ""
            rejected_path = out_path.with_name(out_path.name + ".rejected.jsonl")
            assert rejected_path.exists()
        finally:
            server.shutdown()
            thread.join(timeout=5)
    print("OK - experiment_run_id가 아예 없는 요청(예: 수동 curl 테스트)도 메인 파일을 오염시키지 않음")


def test_healthz_endpoint_ok():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp_path = Path(d)
        port = _free_port()
        out_path = tmp_path / "captured.jsonl"
        server, thread = _run_server_in_thread(port, out_path, "run-A")
        try:
            time.sleep(0.2)
            r = requests.get(f"http://127.0.0.1:{port}/healthz")
            assert r.status_code == 200
        finally:
            server.shutdown()
            thread.join(timeout=5)
    print("OK - /healthz는 정상 응답(오케스트레이터의 기동 확인용)")


if __name__ == "__main__":
    import tempfile
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        if "tmp_path" in t.__code__.co_varnames[:t.__code__.co_argcount]:
            with tempfile.TemporaryDirectory() as d:
                t(Path(d))
        else:
            t()
    print(f"전체 통과 ({len(tests)}개)")
