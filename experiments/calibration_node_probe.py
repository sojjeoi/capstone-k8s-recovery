#!/usr/bin/env python3
"""worker 노드에서 실행하는 probe 동등 요청 클라이언트(2026-09-19, calibrate_network_tolerant_probe.py 전용).

kubelet의 readiness/liveness probe와 **같은 네트워크 위치**(노드 → pod IP)에서 calibration pod에
`GET /health`와 실험과 같은 completion을 보내 지연을 잰다. port-forward·kubectl exec은 pod 안 loopback
으로 들어가 NetworkChaos의 pod 송신 지연을 받지 않으므로 쓸 수 없다(사전 등록 §42.5). 연결마다 새 TCP
연결을 맺어(kubelet probe와 같은 방식) 핸드셰이크 지연까지 포함해 재고, 느린 요청이 다음 요청 발행을
막지 않도록 요청마다 스레드로 동시에 보낸다.

worker의 Python 3.8에서도 돌아야 하고(`python3 -`로 stdin에서 실행), 클러스터·파일을 바꾸지 않는다(읽기
전용 HTTP만). 입력은 base64 JSON 하나(셸 인용 문제를 피함), 출력은 JSON 라인:
  {"kind": "health"|"completion", "seq": n, "t": 발행 시각(시작 기준 초), "latency": 초,
   "status": HTTP 코드|null, "error": 문자열|null}
  마지막에 {"kind": "summary", "elapsed": 초, "issued": {"health": n, "completion": m}}
"""
import base64
import http.client
import json
import os
import sys
import threading
import time


def encode_params(params: dict) -> str:
    return base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")


def request_once(ip, port, method, path, payload, timeout, need_choices=False):
    """(status, latency_sec, error) - 실패한 요청도 실패까지 걸린 시간을 latency로 남긴다."""
    started = time.monotonic()
    status = None
    error = None
    conn = None
    try:
        conn = http.client.HTTPConnection(ip, port, timeout=timeout)
        headers = {"Connection": "close"}
        body = None
        if payload is not None:
            body = json.dumps(payload)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        status = resp.status
        if need_choices and status == 200 and "choices" not in json.loads(data.decode("utf-8")):
            error = "응답에 choices 없음"
    except Exception as e:  # 연결 거부·timeout·파싱 실패 전부 "이 요청은 실패"로만 기록
        error = "%s: %s" % (type(e).__name__, e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return status, time.monotonic() - started, error


def run(params: dict, emit) -> None:
    ip, port = params["ip"], int(params.get("port", 8000))
    duration = float(params["duration_sec"])
    health_interval = float(params.get("health_interval_sec") or 0)
    completion_interval = float(params.get("completion_interval_sec") or 0)
    health_timeout = float(params.get("health_timeout_sec", 30))
    completion_timeout = float(params.get("completion_timeout_sec", 60))
    payload = params.get("completion_payload")

    start = time.monotonic()
    lock = threading.Lock()
    workers = []
    issued = {"health": 0, "completion": 0}

    def one(kind, seq, issued_at):
        if kind == "health":
            status, latency, error = request_once(ip, port, "GET", "/health", None, health_timeout)
        else:
            status, latency, error = request_once(
                ip, port, "POST", "/v1/completions", payload, completion_timeout, need_choices=True)
        record = {"kind": kind, "seq": seq, "t": round(issued_at, 3), "latency": round(latency, 4),
                  "status": status, "error": error}
        with lock:
            emit(record)

    def scheduler(kind, interval):
        n = 0
        while n * interval < duration:
            due = n * interval
            wait = start + due - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            worker = threading.Thread(target=one, args=(kind, n, due))
            worker.start()
            with lock:
                workers.append(worker)
                issued[kind] += 1
            n += 1

    schedulers = []
    if health_interval > 0:
        schedulers.append(threading.Thread(target=scheduler, args=("health", health_interval)))
    if completion_interval > 0:
        schedulers.append(threading.Thread(target=scheduler, args=("completion", completion_interval)))
    for s in schedulers:
        s.start()
    for s in schedulers:
        s.join()
    with lock:
        pending = list(workers)
    for worker in pending:
        worker.join()
    with lock:
        emit({"kind": "summary", "elapsed": round(time.monotonic() - start, 3), "issued": dict(issued)})


def main(argv) -> int:
    params = json.loads(base64.b64decode(argv[0]).decode("utf-8"))
    # 안전망: 요청 timeout이 다 겹쳐도 이 시간 안에는 반드시 끝난다(부모 ssh가 끊겨도 좀비로 남지 않게).
    hard_limit = float(params["duration_sec"]) + 3 * max(
        float(params.get("health_timeout_sec", 30)), float(params.get("completion_timeout_sec", 60))) + 10
    watchdog = threading.Timer(hard_limit, lambda: os._exit(3))
    watchdog.daemon = True
    watchdog.start()

    def emit(record):
        sys.stdout.write(json.dumps(record) + "\n")
        sys.stdout.flush()

    run(params, emit)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
