"""keepalive 오류 경로 집계 (2026-10-23, 로그 분석 전용). 실행: python ka_report.py "라벨=<trace 실행 폴더>" ...
구 Pod 의 접속 로그(vllm-logs, --timestamps)에서 프로브 파드 IP 의 연결(소스 포트)별로 종료 신호 전 사용 횟수·마지막 사용 시각과 종료 신호 뒤 500 응답을 모은다.
연결을 2번 이상 쓴 포트 = 재사용(keepalive 풀) 연결. 종료 신호 뒤 500 을 받은 연결이 전환 전부터 열려 있던 풀 연결인지, 연결당 몇 번 500 을 받았는지를 보인다.
출력: analysis/ka-path-20261023/ka-path.json
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hc_common as hc  # noqa: E402
import trace_tools as tt  # noqa: E402

OUT = hc.RESULTS / "analysis" / "ka-path-20261023"
OUT.mkdir(parents=True, exist_ok=True)
ACC = re.compile(r'^(\S+Z)\s.*?INFO:\s+(\d+\.\d+\.\d+\.\d+):(\d+) - "POST /v1/\S+ HTTP/[\d.]+" (\d+)')
res = {}
for arg in sys.argv[1:]:
    label, d = arg.split("=", 1)
    d = Path(d)
    rep = json.loads((d / "drain-report.json").read_text(encoding="utf-8"))
    sig = datetime.fromisoformat(rep["sigterm_at"])
    S = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    old = S["hashes"]["old_active"]
    log = next(d.glob(f"vllm-logs/*{old}*.log"))
    conns = {}
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        m = ACC.match(line)
        if not m:
            continue
        t = tt.parse_ts(m.group(1))
        c = conns.setdefault((m.group(2), int(m.group(3))), {"before": [], "after": []})
        c["before" if t < sig else "after"].append((round((t - sig).total_seconds(), 2), int(m.group(4))))
    # 종료 신호 전에 2번 이상 쓰였고 종료 신호 직전 10초 안에도 쓰인 연결 = 종료 시점에 붙어 있던 재사용(keepalive) 연결(모든 실행에 같은 규칙)
    pool = {k: v for k, v in conns.items() if len(v["before"]) >= 2 and max(t for t, _ in v["before"]) >= -10}
    rows = []
    for (ip, port), v in sorted(pool.items(), key=lambda kv: -len(kv[1]["before"])):
        rows.append({"port": port, "uses_before_sigterm": len(v["before"]), "last_use_before_sigterm_sec": max(t for t, _ in v["before"]),
                     "after_sigterm": [{"t": t, "status": s} for t, s in v["after"]]})
    got500 = [r for r in rows if any(a["status"] == 500 for a in r["after_sigterm"])]
    res[label] = {"sigterm_at": rep["sigterm_at"], "reused_connections_at_sigterm": len(rows), "reused_connections_that_got_500": len(got500),
                  "total_500_after_sigterm": sum(1 for r in rows for a in r["after_sigterm"] if a["status"] == 500),
                  "max_500_per_connection": max([sum(a["status"] == 500 for a in r["after_sigterm"]) for r in rows], default=0),
                  "connections": rows}
    print(label, {k: v for k, v in res[label].items() if k != "connections"})
(OUT / "ka-path.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
