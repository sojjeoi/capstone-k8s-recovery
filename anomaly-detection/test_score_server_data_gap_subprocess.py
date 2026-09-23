#!/usr/bin/env python3
"""§108(2026-09-24) - 실 클러스터에서 pod_kill 엔드포인트 공백이 우연히
재현될 때까지 pilot을 반복하는 대신(§107.6에서 확인됨 - 4회 중 1회만
재현됨, 확률적 이벤트), fake Prometheus HTTP 서버로 데이터 갭·장기 갭·
연결 실패 3가지 경로를 결정론적으로 재현한다. `test_score_server_e2e_race.py`
와 같은 패턴(실제 score_server.py subprocess + loopback HTTP 서버,
PROMETHEUS_URL 환경변수로 가리킴)을 그대로 따른다.

세 시나리오:
  1) 일시 공백(1~3cycle) - 데이터가 다시 돌아오면 정상 평가가 재개돼야
     한다(transient, evaluation_skipped만 남고 프로세스는 안 죽음).
  2) 장기 공백(PROLONGED_DATA_GAP_CYCLES=4cycle 이상) - detector가 스스로
     PROLONGED_DATA_GAP_EXIT_CODE로 종료해야 한다(§108 신규 동작).
  3) 연결 실패(Prometheus 자체가 응답 안 함) - DataGapFailClosed가 아닌
     평범한 예외로 즉시 실패해야 한다(§108 - 데이터 공백으로 삼키면 안 됨).

각 시나리오 모두 feature 추출(query_range)은 항상 정상 값을 준다 - §106/§107
조사에서 확인된 proposed-03의 실제 구조(6-feature는 계산 가능, freshness
canary만 실패)를 그대로 재현하기 위함. 시나리오 3만 예외 - query_range
자체가 실패해야 하므로 서버를 아예 띄우지 않는다."""
import json
import os
import subprocess
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
V3_DIR = Path(__file__).parent / "v3"
sys.stdout.reconfigure(encoding="utf-8")

V32B_ARTIFACTS_DIR = V3_DIR / "model_v32b" / "artifacts"
SCORE_SERVER_PY = Path(__file__).parent / "score_server.py"

NORMAL_VALUES = {"cpu": 1.0, "memory": 7.0e9, "queue": 0.0, "cache": 0.001}
_METRIC_RAW_NAME = {
    "cpu": "container_cpu_usage_seconds_total",
    "memory": "container_memory_working_set_bytes",
    "queue": "vllm:num_requests_waiting",
    "cache": "vllm:kv_cache_usage_perc",
}


def _metric_from_promql(promql: str):
    for name, needle in _METRIC_RAW_NAME.items():
        if needle in promql:
            return name
    return None


class _GapControllableFakePrometheus(BaseHTTPRequestHandler):
    """클래스 상태(`gap_cycles`)로 몇 번째 평가 cycle에서 freshness가
    공백이어야 하는지 제어한다. cycle 번호는 query_range 요청 중
    METRICS의 첫 항목("cpu")이 올 때마다 1씩 늘어난다(extract_window_
    strict()가 매 cycle 항상 METRICS 순서대로 전부 조회하므로 - §107
    구현에서 feature 자체는 정상 값을 유지하도록 설계) - freshness 호출
    (query, 순서상 feature 추출 뒤)은 그 시점의 cycle 번호로 gap 여부를
    판단한다."""
    state = {"cycle": 0, "gap_cycles": frozenset()}

    def log_message(self, *a):
        pass

    def do_GET(self):
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        query = qs.get("query", [""])[0]

        if parsed.path == "/api/v1/query_range":
            name = _metric_from_promql(query)
            if name == "cpu":
                type(self).state["cycle"] += 1
            body = self._range_body(name)
        elif parsed.path == "/api/v1/query":
            body = self._instant_body()
        else:
            self.send_response(404)
            self.end_headers()
            return

        payload = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _range_body(self, metric_name):
        value = NORMAL_VALUES.get(metric_name, 1.0)
        now = time.time()
        values = [[now - (4 - i) * 15, str(value)] for i in range(5)]
        return {"status": "success", "data": {"resultType": "matrix", "result": [{"metric": {}, "values": values}]}}

    def _instant_body(self):
        cycle = type(self).state["cycle"]
        if cycle in type(self).state["gap_cycles"]:
            return {"status": "success", "data": {"resultType": "vector", "result": []}}  # 표본 없음(진짜 데이터 공백)
        return {"status": "success", "data": {"resultType": "vector",
                                               "result": [{"metric": {}, "value": [time.time(), "1"]}]}}


def _start_server(handler_cls, port=0):
    httpd = HTTPServer(("127.0.0.1", port), handler_cls)
    import threading
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread


def _read_jsonl(path):
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def _run_score_server(tmp_path, prom_url, *, run_id=None):
    evidence_path = tmp_path / "evidence.jsonl"
    run_id = run_id or f"gap-test-{uuid.uuid4().hex[:8]}"
    env = dict(os.environ)
    env["PROMETHEUS_URL"] = prom_url
    env["RECOVERY_POLICY_SIGNAL_URL"] = "http://127.0.0.1:1/signal"  # 아무것도 안 듣는 포트 - 신호가 나가면 안 되므로 안전망
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-u", str(SCORE_SERVER_PY),
         "--artifacts-dir", str(V32B_ARTIFACTS_DIR), "--model-version", "v3.2b",
         "--run-id", run_id, "--evidence-log", str(evidence_path)],
        cwd=str(Path(__file__).parent), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
    )
    return proc, evidence_path


def test_transient_gap_then_recovery_deterministic(tmp_path):
    """1cycle짜리 일시 공백(cycle 2만 gap) - 그 cycle만 evaluation_skipped로
    기록되고, 프로세스는 안 죽고, 입력 복귀 후(cycle 3) 평가가 정상
    재개돼야 한다."""
    _GapControllableFakePrometheus.state = {"cycle": 0, "gap_cycles": frozenset({2})}
    prom_httpd, prom_thread = _start_server(_GapControllableFakePrometheus)
    proc, evidence_path = _run_score_server(tmp_path, f"http://127.0.0.1:{prom_httpd.server_port}")
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            recs = _read_jsonl(evidence_path)
            decisions = [r for r in recs if r.get("record_type") == "evaluation_decision"]
            if len(decisions) >= 3:
                break
            time.sleep(1.0)
        assert proc.poll() is None, f"프로세스가 예기치 않게 종료됨(returncode={proc.returncode}): {proc.stdout.read() if proc.stdout else ''}"
    finally:
        proc.kill()
        proc.wait(timeout=10)
        prom_httpd.shutdown()

    recs = _read_jsonl(evidence_path)
    decisions = [r for r in recs if r.get("record_type") == "evaluation_decision"]
    skipped = [r for r in recs if r.get("record_type") == "evaluation_skipped"]
    assert len(skipped) == 1, f"cycle 2만 스킵돼야 함: {skipped}"
    assert skipped[0]["evaluation_seq"] == 2
    assert skipped[0]["gap_classification"] == "transient"
    assert [d["evaluation_seq"] for d in decisions] == [1, 3, 4][:len(decisions)]
    print(f"OK - fake Prometheus로 결정론적으로 재현한 1cycle 일시 공백: cycle 2만 스킵, "
          f"프로세스 생존, cycle 3부터 평가 재개(실 클러스터 우연 재현에 의존하지 않음)")


def test_prolonged_gap_invalidates_and_exits_deterministic(tmp_path):
    """PROLONGED_DATA_GAP_CYCLES(4) 이상 연속 공백 - detector가 스스로
    PROLONGED_DATA_GAP_EXIT_CODE로 종료해야 한다."""
    import score_server as ss  # noqa: E402 - PROLONGED_DATA_GAP_CYCLES/EXIT_CODE 상수만 읽음(subprocess와 별개)
    always_gap = frozenset(range(1, 20))
    _GapControllableFakePrometheus.state = {"cycle": 0, "gap_cycles": always_gap}
    prom_httpd, prom_thread = _start_server(_GapControllableFakePrometheus)
    proc, evidence_path = _run_score_server(tmp_path, f"http://127.0.0.1:{prom_httpd.server_port}")
    try:
        returncode = proc.wait(timeout=120)
    finally:
        prom_httpd.shutdown()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    assert returncode == ss.PROLONGED_DATA_GAP_EXIT_CODE, (
        f"장기 공백이면 exit_code={ss.PROLONGED_DATA_GAP_EXIT_CODE}로 스스로 종료해야 함(실제={returncode})")
    recs = _read_jsonl(evidence_path)
    skipped = [r for r in recs if r.get("record_type") == "evaluation_skipped"]
    invalidated = [r for r in recs if r.get("record_type") == "prolonged_data_gap_invalidated"]
    assert len(skipped) == ss.PROLONGED_DATA_GAP_CYCLES, skipped
    assert len(invalidated) == 1, invalidated
    assert invalidated[0]["exit_code"] == ss.PROLONGED_DATA_GAP_EXIT_CODE
    print(f"OK - fake Prometheus로 결정론적으로 재현한 {ss.PROLONGED_DATA_GAP_CYCLES}cycle 이상 장기 공백: "
          f"detector가 exit_code={returncode}로 스스로 종료(정상 미탐지와 섞이지 않음, "
          f"실 클러스터 우연 재현에 의존하지 않음)")


def test_prometheus_connection_failure_is_not_treated_as_gap_deterministic(tmp_path):
    """Prometheus 자체가 응답하지 않으면(포트에 아무 것도 안 듣고 있음)
    DataGapFailClosed로 삼켜지지 않고 프로세스가 실패로 즉시 종료돼야
    한다 - evaluation_skipped가 단 1건도 남으면 안 된다."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()  # 바인드만 확인하고 즉시 닫음 - 이 포트엔 아무 것도 안 듣는 상태로 남음

    proc, evidence_path = _run_score_server(tmp_path, f"http://127.0.0.1:{dead_port}")
    try:
        returncode = proc.wait(timeout=60)
        stdout = proc.stdout.read() if proc.stdout else ""
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    assert returncode != 0, "Prometheus 연결 실패는 정상 종료(0)가 아니라 실패로 끝나야 함"
    # score_server.py의 __main__ 블록(기존, 이번 턴 무변경)이 RuntimeError를
    # parser.error()로 감싸 깔끔한 CLI 오류로 바꾼다(raw traceback이 아님) -
    # 이 메시지가 query_range_with_bounded_retry()가 연결 완전 두절 시
    # 던지는 것이라는 증거로 ASCII 전용 마커만 확인한다(이 Windows 환경의
    # 콘솔 codepage가 cp949라 stderr의 한글 부분은 subprocess 파이프를
    # utf-8로 읽을 때 깨질 수 있음 - score_server.py는 sys.stdout만
    # reconfigure하고 stderr는 안 함, 이건 기존 상태라 이번 턴에서
    # 건드리지 않음. "bounded retry"/"HTTPConnectionPool" 등은 순수
    # ASCII라 이 문제와 무관하게 항상 안전하게 매치된다).
    assert "bounded retry" in stdout, (
        f"연결 실패가 bounded retry 소진으로 전파됐는지 stdout에서 확인 안 됨: {stdout[-2000:]}")
    assert "HTTPConnectionPool" in stdout or "ConnectionError" in stdout, (
        f"연결 실패 자체의 원인이 stdout에 안 남음: {stdout[-2000:]}")
    assert "fail-closed" not in stdout, (
        "연결 실패가 DataGapFailClosed의 메시지 형태(fail-closed: ...)로 나타나면 안 됨(데이터 공백으로 오인된 것)")
    recs = _read_jsonl(evidence_path)
    skipped = [r for r in recs if r.get("record_type") == "evaluation_skipped"]
    assert skipped == [], f"Prometheus 연결 실패가 evaluation_skipped로 기록되면 안 됨(데이터 공백이 아니므로): {skipped}"
    print(f"OK - fake Prometheus 없이(연결 자체가 실패) score_server.py가 returncode={returncode}로 실패 종료, "
          f"evaluation_skipped 0건(데이터 공백으로 오인해 삼키지 않음 확인)")


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        test_transient_gap_then_recovery_deterministic(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_prolonged_gap_invalidates_and_exits_deterministic(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_prometheus_connection_failure_is_not_treated_as_gap_deterministic(Path(d))
    print("전체 통과 (3개)")
