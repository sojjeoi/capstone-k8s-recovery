#!/usr/bin/env python3
"""§92.7 - 실클러스터 없이 §91에서 실제로 관측된 race(신호 직후 orchestrator가
detector cleanup을 시작 -> 진행 중이던 cycle의 evidence가 유실됨)를 로컬에서
재현하는 synthetic E2E 테스트. 구성 요소:

  - `_FakePrometheusServer`: 실제 HTTP 서버(loopback) - score_server.py가 쓰는
    두 엔드포인트(`/api/v1/query_range`, `/api/v1/query`)에 결정적 canned
    데이터로 응답한다. §91 실제 파일럿 evidence에서 그대로 가져온 raw
    feature vector(§91 evidence, is_anomalous=True 실측값)를 재현하도록
    구성해 "고정 feature sequence"가 실제로 anomalous score를 낸다는 것
    자체도 frozen v3.2b 모델로 검증한다(가짜 스코어를 주입하지 않음).
  - `_FakeRecoveryPolicyServer`: 실제 HTTP 서버 - POST /signal을 받으면 먼저
    수신 사실을 `threading.Event`로 즉시 알리고(테스트가 "지금 orchestrator가
    cleanup을 시작해야 하는 순간"을 감지), 그 다음 `response_delay_sec`만큼
    지연한 뒤에야 응답한다(§91 실측 - 실제 recovery-policy의 promote()가
    수 초 걸림).
  - 실제 `score_server.py`를 real subprocess로 띄우고(frozen v3.2b artifact
    그대로, `PROMETHEUS_URL`/`RECOVERY_POLICY_SIGNAL_URL` 환경변수로 두 fake
    서버를 가리킴) `--evidence-log`를 지정한다.
  - 테스트(오케스트레이터 역할)는 recovery-policy가 신호를 "받은" 즉시(아직
    응답 전) subprocess를 강제종료한다 - §91에서 실제로 벌어졌을 것으로
    의심되는 최악의 타이밍(진행 중인 신호에 대한 처리가 전혀 안 끝난 시점의
    강제종료)을 의도적으로 만든다.

핵심 확인: §92의 write-ahead 덕분에, 이 최악의 타이밍에서도 신호를 촉발한
cycle의 `evaluation_decision` record는 이미 파일에 flush+fsync돼 있어야
한다(§91 갭의 직접 재현·수정 확인)."""
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
V3_DIR = Path(__file__).parent / "v3"
sys.path.insert(0, str(V3_DIR))
sys.path.insert(0, str(V3_DIR / "model_v31"))
sys.stdout.reconfigure(encoding="utf-8")

from evaluate import load_frozen_artifacts  # noqa: E402 (model_v31)
from features import METRICS  # noqa: E402

V32B_ARTIFACTS_DIR = V3_DIR / "model_v32b" / "artifacts"
SCORE_SERVER_PY = Path(__file__).parent / "score_server.py"

# §91 실제 파일럿 evidence(smoke-v32b가 아니라 §91 pilot 자체, run_id
# pilot-load_ramp-proposed-01-20260921T123428Z)의 실측 anomalous raw
# feature vector 그대로 재사용 - 임의로 지어낸 값이 아니라 frozen v3.2b가
# 실제로 anomalous로 판정했던 값이다(순서: cpu_mean,cpu_slope,memory_mean,
# memory_slope,queue_mean,queue_slope,cache_mean,cache_slope).
_ANOMALOUS_RAW_FEATURES = [
    1.8483533840408632, 0.05818901716766916,
    6996905984.0, -9.104459286699055e-07,
    0.0, 0.0,
    0.00161290322580645, -0.0016129032258064514,
]
_METRIC_ORDER = list(METRICS.keys())  # ["cpu", "memory", "queue", "cache"]
_POINTS_PER_WINDOW = 5  # 60초 window / 15초 step + 1


def _linear_points(mean: float, slope: float, n: int = _POINTS_PER_WINDOW) -> list:
    """평균·기울기를 정확히 재현하는 n개 점 - np.polyfit이 잡음 없는 완전한
    직선에서는 slope를 오차 없이 복원하고, 대칭 등차수열의 평균은 항상
    가운데 항(=mean)이다."""
    return [mean - slope * (n - 1) / 2 + slope * i for i in range(n)]


def _canned_values_by_metric() -> dict:
    values = {}
    for idx, name in enumerate(_METRIC_ORDER):
        mean, slope = _ANOMALOUS_RAW_FEATURES[idx * 2], _ANOMALOUS_RAW_FEATURES[idx * 2 + 1]
        values[name] = _linear_points(mean, slope)
    return values


def _promql_to_metric(promql: str) -> str:
    for name, q in METRICS.items():
        if q == promql:
            return name
    raise KeyError(promql)


class _FakePrometheusServer(BaseHTTPRequestHandler):
    canned = _canned_values_by_metric()

    def log_message(self, *a):
        pass  # 테스트 출력 소음 방지

    def do_GET(self):
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        query = qs.get("query", [""])[0]

        if parsed.path == "/api/v1/query_range":
            metric = _promql_to_metric(query)
            points = self.canned[metric]
            now = time.time()
            values = [[now - (len(points) - 1 - i) * 15, str(v)] for i, v in enumerate(points)]
            body = {"status": "success", "data": {"resultType": "matrix",
                                                     "result": [{"metric": {}, "values": values}]}}
        elif parsed.path == "/api/v1/query":
            # freshness probe(up{...}) - 항상 신선한 표본 1개.
            body = {"status": "success", "data": {"resultType": "vector",
                                                     "result": [{"metric": {}, "value": [time.time(), "1"]}]}}
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


class _FakeRecoveryPolicyServer(BaseHTTPRequestHandler):
    received_event: threading.Event = None
    response_delay_sec: float = 0.0
    request_count = {"n": 0}
    received_payloads = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).request_count["n"] += 1
        try:
            type(self).received_payloads.append(json.loads(body))
        except Exception:
            pass
        if type(self).received_event is not None:
            type(self).received_event.set()  # "신호를 받았다" - 테스트가 이 순간 cleanup을 시작
        time.sleep(type(self).response_delay_sec)  # §91 실측 - promote()가 수 초 걸림
        resp = json.dumps({"action": "promote_preview", "outcome": "executed_verified"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


def _start_server(handler_cls, port=0):
    httpd = HTTPServer(("127.0.0.1", port), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread


def _read_jsonl(path):
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def test_synthetic_e2e_race_write_ahead_survives_kill_immediately_after_signal_received(tmp_path):
    """§92.7 핵심 테스트 - §91과 동일한 최악의 타이밍(신호 수신 직후, 응답
    받기 전에 detector 강제종료)에서도 write-ahead 덕분에 그 cycle의
    evaluation_decision이 evidence-log에 남아있어야 한다."""
    prom_httpd, prom_thread = _start_server(_FakePrometheusServer)
    rp_received = threading.Event()
    _FakeRecoveryPolicyServer.received_event = rp_received
    _FakeRecoveryPolicyServer.response_delay_sec = 3.0  # §91 실측(7.5초)보다 짧게 - 테스트 속도
    _FakeRecoveryPolicyServer.request_count = {"n": 0}
    _FakeRecoveryPolicyServer.received_payloads = []
    rp_httpd, rp_thread = _start_server(_FakeRecoveryPolicyServer)

    evidence_path = tmp_path / "evidence.jsonl"
    run_id = f"synthetic-race-{uuid.uuid4().hex[:8]}"
    env = dict(os.environ)
    env["PROMETHEUS_URL"] = f"http://127.0.0.1:{prom_httpd.server_port}"
    env["RECOVERY_POLICY_SIGNAL_URL"] = f"http://127.0.0.1:{rp_httpd.server_port}/signal"
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        [sys.executable, "-u", str(SCORE_SERVER_PY),
         "--artifacts-dir", str(V32B_ARTIFACTS_DIR), "--model-version", "v3.2b",
         "--run-id", run_id, "--evidence-log", str(evidence_path)],
        cwd=str(Path(__file__).parent), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
    )
    try:
        # 3회 연속 anomalous -> 3번째 cycle(EVAL_INTERVAL_SEC=15초 간격)에서
        # 신호가 나갈 때까지 최대 90초 대기(느린 CI 여유 포함).
        got_signal = rp_received.wait(timeout=90)
        assert got_signal, "fake recovery-policy가 신호를 못 받음(anomalous 조건이 재현 안 됐을 가능성)"

        # §91과 동일한 최악의 타이밍 - 신호 응답을 기다리지 않고 즉시 강제종료.
        proc.kill()
        proc.wait(timeout=10)
    finally:
        prom_httpd.shutdown()
        rp_httpd.shutdown()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    assert _FakeRecoveryPolicyServer.request_count["n"] == 1, "신호는 정확히 1회만 도달해야 함"

    recs = _read_jsonl(evidence_path)
    decisions = [r for r in recs if r.get("record_type") == "evaluation_decision"]
    assert len(decisions) >= 3, f"최소 3개 cycle(3회 연속 anomalous)이 기록돼야 하는데 {len(decisions)}개만 있음: {recs}"
    triggering = [d for d in decisions if d["would_signal"] is True]
    assert len(triggering) == 1, f"신호를 촉발한 cycle이 정확히 1개 있어야 함: {triggering}"
    trig = triggering[0]
    assert trig["consecutive_anomalous"] >= 3
    assert trig["is_anomalous"] is True

    # evaluation_seq gap 없음, correlation_id 모두 고유.
    seqs = sorted(d["evaluation_seq"] for d in decisions)
    assert seqs == list(range(1, len(seqs) + 1)), seqs
    corr_ids = [d["correlation_id"] for d in decisions]
    assert len(set(corr_ids)) == len(corr_ids)

    # correlation_id가 fake recovery-policy가 실제로 받은 payload와 일치.
    assert len(_FakeRecoveryPolicyServer.received_payloads) == 1
    received_payload = _FakeRecoveryPolicyServer.received_payloads[0]
    assert received_payload.get("correlation_id") == trig["correlation_id"], \
        "signal payload의 correlation_id가 evidence의 evaluation_decision과 일치해야 함"
    assert received_payload.get("evaluation_seq") == trig["evaluation_seq"]
    assert received_payload.get("model_version") == "v3.2b"
    assert received_payload.get("experiment_run_id") == run_id

    # runtime/offline parity - 이 cycle의 원본 feature로 offline evaluator를 재생.
    model, scaler, schema, threshold_doc = load_frozen_artifacts(V32B_ARTIFACTS_DIR)
    x_scaled = scaler.transform([trig["ordered_feature_vector"]])
    offline_score = float(model.decision_function(x_scaled)[0])
    assert abs(offline_score - trig["score"]) < 1e-9, (offline_score, trig["score"])
    assert (offline_score < threshold_doc["threshold"]) == trig["is_anomalous"]

    # 강제종료(kill) 직후라 signal_result가 완결됐을 수도 안 됐을 수도 있다(정상 -
    # 즉시 kill은 진행 중이던 완료-후-기록을 못 끝냈을 가능성이 있음) - 있어도
    # 없어도 되지만, 있다면 반드시 같은 correlation_id로 연결돼야 한다(덮어쓰기 없음).
    results = [r for r in recs if r.get("record_type") == "signal_result"]
    for r in results:
        assert r["correlation_id"] in corr_ids

    print(f"OK - §91과 동일한 race(신호 수신 직후 강제종료)에서도 write-ahead evaluation_decision이 "
          f"{len(decisions)}개 전부 보존됨(신호 촉발 cycle 포함), correlation_id로 signal payload와 연결 "
          f"확인, runtime/offline parity 일치")


def test_pre_fix_write_after_signal_ordering_loses_decision_on_kill_control():
    """§92.7 - "가능하면 재현" 요구사항 대응(대조군). 실제 score_server.py는
    이미 write-ahead로 고쳐졌으므로 그 파일을 되돌리지 않고, §91 이전의
    정확한 순서(신호 HTTP 요청 -> 응답 대기 -> 그 다음에만 evidence 기록)를
    이 테스트 안에 독립적으로 재현해, 같은 강제종료 타이밍에서 정말로
    레코드가 유실되는지 직접 보여준다(수정이 실제로 막는 실패 모드를
    명시적으로 대조)."""
    order_of_events = []
    write_completed = threading.Event()
    killed = threading.Event()

    def old_order_cycle():
        # §91 이전 main()의 정확한 순서: HTTP 요청부터 보내고, 그 응답을
        # "기다린 뒤에만" evidence를 쓴다.
        order_of_events.append("http_request_sent")
        if killed.wait(timeout=0.05):  # 강제종료가 먼저 도착 - 응답을 절대 못 받음
            return  # 실제 프로세스라면 여기서 그냥 죽는다 - 아래 write는 영원히 실행 안 됨
        order_of_events.append("evidence_written")  # 이 줄에 절대 도달하지 않아야 함(구조 자체가 증명 대상)
        write_completed.set()

    t = threading.Thread(target=old_order_cycle)
    t.start()
    time.sleep(0.01)  # http_request_sent가 찍히도록 잠깐 양보
    killed.set()  # §91과 동일한 순간 - "신호는 나갔다, 이제 강제종료"
    t.join(timeout=1.0)

    assert order_of_events == ["http_request_sent"], order_of_events
    assert not write_completed.is_set(), \
        "구버전 순서(신호 후 기록)에서는 신호 직후 강제종료되면 evidence가 절대 안 쓰임 - 이것이 §91 갭의 정확한 메커니즘"
    print("OK - 대조군: '신호 후 기록' 순서에서는 신호 직후 강제종료 시 evidence가 구조적으로 유실됨"
          "(§92의 write-ahead가 막는 정확한 실패 모드)")


if __name__ == "__main__":
    import tempfile
    test_pre_fix_write_after_signal_ordering_loses_decision_on_kill_control()
    with tempfile.TemporaryDirectory() as d:
        test_synthetic_e2e_race_write_ahead_survives_kill_immediately_after_signal_received(Path(d))
    print("전체 통과 (2개)")
