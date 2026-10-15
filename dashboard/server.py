"""AIOps 시연 대시보드 서버 - 관찰 전용(READ-ONLY).

- GET /           : index.html
- GET /api/state  : 판정된 화면 상태(JSON)
- 그 외 경로 404, GET 외 메서드(POST/PUT/PATCH/DELETE 등) 405. 클러스터·정책을 바꾸는 엔드포인트 자체가 없다.
- 127.0.0.1에만 묶는다. 수집은 single-flight + 최소 간격(MIN_COLLECT_SEC)으로 브라우저 수와 무관하게 부하가 일정하다.

실행:
  live : python dashboard/server.py                 (실행기 세션의 포트포워드·GUARDED_ADMIN_TOKEN 환경변수 사용)
  mock : python dashboard/server.py --mock          (화면 상단 MOCK DATA, live 수집기 호출 없음)
"""
import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import state  # noqa: E402

MIN_COLLECT_SEC = 3.0
RANGES_MIN = (5, 30, 60, 360)  # sources.RANGES_MIN과 같다(mock 모드는 sources를 import하지 않아도 되게 여기 둔다)


def sources_ranges():
    return RANGES_MIN


class Hub:
    """원자료 수집 + 판정 + 첫 관측 시각 보관(run_id별). 수집은 한 번에 하나만."""

    def __init__(self, collect_fn):
        self.collect_fn = collect_fn  # collect_fn(minutes) -> raw
        self.lock = threading.Lock()
        self.cache = {}  # 그래프 범위(분)별 (view, 수집 시각)
        self.seen = {}

    def view(self, minutes=5):
        with self.lock:
            hit = self.cache.get(minutes)
            if hit is None or time.monotonic() - hit[1] >= MIN_COLLECT_SEC:
                raw = self.collect_fn(minutes)
                key = (((raw.get("detector") or {}).get("data") or {}).get("start") or {}).get("run_id")
                v, obs = state.derive(raw, self.seen.get(key))
                if key:
                    self.seen.setdefault(key, {}).update(obs)
                hit = self.cache[minutes] = (v, time.monotonic())
            return hit[0]


def make_handler(hub):
    page = (HERE / "index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            path, _, query = self.path.partition("?")
            if path in ("/", "/index.html"):
                return self._send(200, page, "text/html; charset=utf-8")
            if path == "/api/state":
                try:
                    q = parse_qs(query).get("range", ["5"])[0]
                    minutes = int(q) if q.isdigit() and int(q) in sources_ranges() else 5
                    body = json.dumps(hub.view(minutes), ensure_ascii=False, default=str).encode("utf-8")
                except Exception as e:  # noqa: BLE001 - 화면은 연결 실패(UNKNOWN)로 그린다
                    return self._send(500, json.dumps({"error": type(e).__name__}).encode(), "application/json")
                return self._send(200, body, "application/json; charset=utf-8")
            return self._send(404, b"not found", "text/plain")

        def _reject(self):
            # 본문을 버리고 읽은 뒤 응답한다 - 안 읽은 채 닫으면 Windows가 연결을 강제 종료(RST)해 405가 전달되지 않는다
            self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 65536))
            self._send(405, b"read-only dashboard", "text/plain")

        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _reject  # noqa: N815

        def log_message(self, fmt, *args):  # 요청 로그 생략(토큰은 애초에 URL·응답에 없음)
            pass

    return Handler


def build_collector(args):
    if args.mock:
        import mock
        t0 = time.monotonic()
        return lambda minutes=5: mock.build(time.monotonic() - t0, minutes=minutes)
    import sources
    for u in (args.policy_url, args.prom_url):  # 포트포워드(로컬)만 읽는다 - admin token이 다른 곳으로 가지 않게
        if urlparse(u).hostname not in ("127.0.0.1", "localhost"):
            raise SystemExit(f"loopback 주소만 허용: {u}")
    since = datetime.now(timezone.utc) - timedelta(minutes=args.since_minutes)
    cfg = {"results_dir": Path(args.results_dir), "since": since, "policy_url": args.policy_url,
           "prom_url": args.prom_url, "token": os.environ.get("GUARDED_ADMIN_TOKEN"), "active_profile": args.active_profile}
    print(f"[live] since={since.isoformat()} results={cfg['results_dir']} policy={cfg['policy_url']} "
          f"prometheus={cfg['prom_url']} profile={cfg['active_profile'] or 'results-dir 기본'} "
          f"admin_token={'set' if cfg['token'] else 'NOT SET'}", flush=True)
    return lambda minutes=5: sources.collect(cfg, minutes)


def main(argv=None):
    p = argparse.ArgumentParser(description="AIOps 시연 대시보드(읽기 전용)")
    p.add_argument("--mock", action="store_true", help="MOCK DATA 각본 재생(연습용)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--results-dir", default=str(HERE.parent / "experiments" / "results"))
    p.add_argument("--policy-url", default="http://127.0.0.1:18090")
    p.add_argument("--prom-url", default="http://127.0.0.1:9090", help="Prometheus 포트포워드(Pod별 요청 그래프용)")
    p.add_argument("--active-profile", default=None,
                   help="실행기와 같은 Active Profile 파일(ACTIVE/SHADOW 표시의 원본). 없으면 <results-dir>/phase2_selection/active-profile.json")
    p.add_argument("--since-minutes", type=float, default=0.0,
                   help="이 시간(분)만큼 이전에 시작된 실행까지 이번 세션으로 본다(기본 0 = 대시보드 시작 이후만)")
    a = p.parse_args(argv)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(Hub(build_collector(a))))
    print(f"{'MOCK' if a.mock else 'LIVE'} dashboard: http://127.0.0.1:{a.port}/", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
