"""기능 점검 1 (2026-10-23): 계측 probe 가 실제 서비스에서 동작하는지, 요청의 처리 Pod 를 접속 로그와 대조해 특정할 수 있는지 본다.
장애 주입·전환 없음. 클러스터 변경은 짧은 프로브 파드 1개뿐(종료 시 삭제). 실행: python trace_check.py [--sec 45]
"""
import argparse
import csv
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import hc_common as hc  # noqa: E402
import trace_adapter  # noqa: E402
import trace_tools as tt  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sec", type=float, default=45.0)
    a = ap.parse_args()
    os.chdir(hc.ROOT)
    probs = hc.other_experiment_problems()
    if probs:
        raise SystemExit(f"다른 작업과 충돌 - 시작 안 함: {probs}")
    import load_ramp_adapter as lra
    import run_if_threshold_candidate_smoke as smoke
    out = hc.RESULTS / "trace" / f"check1-{hc.now_iso()[:19].replace(':', '')}"
    out.mkdir(parents=True)
    logs = tt.LogFollowers(out / "vllm-logs", hc.vllm_pods).start()
    watch = tt.Watchers(out).start()
    trace_adapter.install(out)
    run_id = "trace-check1"
    prober = lra.make_load_ramp_prober(smoke.DEFAULT_PROBE_CONFIG, run_id, "trace_check", "proposed", 9001, a.sec, settle_sec=5, image=lra.IMAGE_FORCE_CLOSE)
    try:
        prober.start()
        time.sleep(a.sec + 15)
    finally:
        prober.stop()
        time.sleep(3)
        logs.stop()
        watch.stop()
    evid = out / f"probe-{run_id}-proposed-9001-evidence.jsonl"
    rows = tt.request_table(evid, out / "vllm-logs")
    with open(out / "request-table.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=sorted({k for r in rows for k in r}))
        w.writeheader()
        w.writerows(rows)
    pod_csv = list(csv.DictReader(open(out / f"probe-{run_id}-proposed-9001-pod-raw.csv", encoding="utf-8")))
    attributed = [r for r in rows if r.get("served_pod")]
    print("requests", len(rows), "attributed", len(attributed), "unresolved", sum(r["outcome"] == "unresolved" for r in rows),
          "failed", sum(r["outcome"] == "failed" for r in rows), "pod_csv_rows", len(pod_csv))
    print("pods", sorted({str(r.get("served_pod")) for r in rows}), "basis", sorted({r["basis"] for r in rows})[:4])
    done = sorted([r for r in rows if r["outcome"] != "unresolved"], key=lambda x: x["sent_at"])
    print("max |csv latency - evidence latency|:", max((abs(float(c["latency"]) - r["latency"]) for c, r in zip(pod_csv, done)), default=None))
    print("out:", out)


if __name__ == "__main__":
    main()
