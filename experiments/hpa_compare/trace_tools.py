"""요청 단위 추적 도구 (2026-10-23): vLLM Pod 로그 수집, Service 선택자·엔드포인트 변경 감시, 요청의 처리 Pod 특정.

처리 Pod 특정 방법: 프로브가 요청마다 새 연결(force_close)을 쓰고 자신의 소켓 주소(ip:port)를 기록한다. vLLM(uvicorn) 접속 로그에는
`클라이언트IP:포트 - "POST /v1/completions ..."` 가 남고, kubectl logs --timestamps 가 줄마다 시각을 붙인다. 같은 ip:port 가
전송~완료 시각 근처(허용 오차)에 정확히 한 Pod 의 로그에만 있으면 그 Pod 가 처리한 것이다. 없거나 둘 이상이면 None(미확인) - 추정으로 채우지 않는다.
"""
import json
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

NS = "vllm-serving"
SERVICE = "vllm-active"
ACCESS = re.compile(r'^(\d{4}-\d\d-\d\dT[\d:.]+)Z\s.*?INFO:\s+(\d+\.\d+\.\d+\.\d+):(\d+) - "(\w+) (\S+) HTTP/[\d.]+" (\d+)')
TOL_BEFORE, TOL_AFTER = 2.0, 3.0   # 시계 차이(프로브 노드·vLLM 노드)와 로그 기록 지연 허용치(초)


def parse_ts(s):
    s = s.rstrip("Z")
    head, _, frac = s.partition(".")
    return datetime.fromisoformat(head + "." + (frac + "000000")[:6]).replace(tzinfo=timezone.utc)


def parse_access_line(line):
    m = ACCESS.match(line)
    if not m:
        return None
    ts, ip, port, method, path, status = m.groups()
    return {"ts": parse_ts(ts), "ip": ip, "port": int(port), "method": method, "path": path, "status": int(status)}


def load_access(logdir):
    """logdir/<pod>.log 들 -> {(ip,port): [{ts,pod,path,status}]} (POST /v1/ 요청만)."""
    out = {}
    for p in sorted(Path(logdir).glob("*.log")):
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            a = parse_access_line(line)
            if a and a["method"] == "POST" and a["path"].startswith("/v1/"):
                out.setdefault((a["ip"], a["port"]), []).append({"ts": a["ts"], "pod": p.stem, "path": a["path"], "status": a["status"]})
    return out


def attribute(ip, port, t_from, t_to, access):
    """(처리 Pod 또는 None, 근거). t_from/t_to: 요청의 전송·완료(또는 마지막 관측) 시각(aware datetime)."""
    if not ip or not port:
        return None, "소켓 주소 미상(응답 헤더 전 실패)"
    lo, hi = t_from - timedelta(seconds=TOL_BEFORE), t_to + timedelta(seconds=TOL_AFTER)
    hits = [h for h in access.get((ip, port), []) if lo <= h["ts"] <= hi]
    pods = sorted({h["pod"] for h in hits})
    if len(pods) == 1:
        return pods[0], f"접속 로그 {len(hits)}줄 일치"
    return None, "접속 로그 없음" if not pods else f"여러 Pod 로그와 일치: {pods}"


def read_jsonl(path):
    p = Path(path)
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


def request_table(evidence_path, logdir):
    """probe_trace evidence + Pod 접속 로그 -> 요청별 행. 완료 기록이 없는 요청은 outcome='unresolved'(성공·실패로 채우지 않는다)."""
    ev = read_jsonl(evidence_path)
    access = load_access(logdir)
    done = {e["request_id"]: e for e in ev if e["event"] == "done"}
    rows = []
    for s in (e for e in ev if e["event"] == "sent"):
        d = done.get(s["request_id"])
        t0 = datetime.fromisoformat(s["sent_at"])
        if d is None:
            rows.append({"request_id": s["request_id"], "sent_at": s["sent_at"], "outcome": "unresolved", "served_pod": None, "basis": "완료 기록 없음"})
            continue
        pod, why = attribute(d.get("client_ip"), d.get("client_port"), t0, datetime.fromisoformat(d["done_at"]), access)
        rows.append({"request_id": s["request_id"], "sent_at": s["sent_at"], "headers_at": d.get("headers_at"), "done_at": d["done_at"],
                     "latency": round(d["latency"], 4), "status": d["status"], "success": d["success"], "error": d.get("error"),
                     "outcome": "ok" if d["success"] else "failed", "client_ip": d.get("client_ip"), "client_port": d.get("client_port"),
                     "served_pod": pod, "basis": why})
    return rows


def _now():
    return datetime.now(timezone.utc).isoformat()


class LogFollowers:
    """현재·앞으로 생기는 vLLM Pod 의 로그를 <outdir>/<pod>.log 로 계속 받는다(Pod 가 사라진 뒤에도 파일은 남는다). 읽기 전용."""

    def __init__(self, outdir, list_pods):
        self.outdir, self.list_pods = Path(outdir), list_pods
        self.procs, self.files, self._stop = {}, {}, threading.Event()
        self.errors = []
        self.outdir.mkdir(parents=True, exist_ok=True)

    def _loop(self):
        while not self._stop.is_set():
            try:
                names = [p["name"] for p in self.list_pods()]
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"{_now()} list_pods: {e}")
                names = []
            for n in names:
                pr = self.procs.get(n)
                if pr is not None and pr.poll() is None:
                    continue
                path = self.outdir / f"{n}.log"
                if pr is not None and path.exists() and path.stat().st_size > 0:
                    continue                                   # 한 번 받기 시작한 뒤 끝난 것은 Pod 종료로 본다
                f = open(path, "ab")
                self.files[n] = f
                self.procs[n] = subprocess.Popen(["kubectl", "logs", "-f", "--timestamps", "-n", NS, n, "-c", "vllm"],
                                                 stdout=f, stderr=subprocess.DEVNULL)
            self._stop.wait(3.0)

    def start(self):
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()
        return self

    def stop(self):
        self._stop.set()
        for pr in self.procs.values():
            if pr.poll() is None:
                pr.terminate()
        for f in self.files.values():
            f.close()


class Watchers:
    """Service 선택자와 엔드포인트 IP 목록의 변경을 받은 즉시 (수신 시각, 값) 으로 기록한다. 시작 시 현재값도 1회 기록된다."""

    TEMPLATES = {"selector": "{.spec.selector}{\"\\n\"}",
                 "endpoints": "{range .subsets[*].addresses[*]}{.ip},{end}{\"\\n\"}"}

    def __init__(self, outdir):
        self.outdir, self.procs, self.threads = Path(outdir), [], []
        self.outdir.mkdir(parents=True, exist_ok=True)

    def _pump(self, pr, kind, fh):
        for line in pr.stdout:
            fh.write(json.dumps({"at": _now(), "kind": kind, "value": line.strip()}, ensure_ascii=False) + "\n")
            fh.flush()

    def start(self):
        for kind, res in (("selector", "svc"), ("endpoints", "endpoints")):
            fh = open(self.outdir / f"watch-{kind}.jsonl", "a", encoding="utf-8")
            pr = subprocess.Popen(["kubectl", "get", res, SERVICE, "-n", NS, "-w", "-o", f"jsonpath={self.TEMPLATES[kind]}"],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
            t = threading.Thread(target=self._pump, args=(pr, kind, fh), daemon=True)
            t.start()
            self.procs.append(pr)
            self.threads.append(t)
        return self

    def stop(self):
        for pr in self.procs:
            if pr.poll() is None:
                pr.terminate()
        time.sleep(0.5)
