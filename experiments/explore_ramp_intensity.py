#!/usr/bin/env python3
"""ramp 강도 재설계용 탐색 스크립트(2026-09-16, 2026-09-18 stage 경계 버그
수정). run_once()는 t_slo/t_recovery가 둘 다 찍히면 check_slo_violation()/
check_recovered() 호출을 멈추는 "위반->회복 1회 사이클"짜리 단일 trial
설계라, 여러 stage에 걸친 sweep 탐색에는 안 맞는다(실측 확인: stage-1에서
짧게 위반+즉시 회복되자 나머지 stage 관찰이 통째로 끊김 - probe 프로세스
자체는 계속 정상 실행 중이었음, kubectl exec/네트워킹 문제 아님). 이
스크립트는 run_once() 없이 ramp.py+probe.py를 끝까지 동시 실행하고,
완주한 뒤 probe raw CSV 전체를 stage 경계로 나눠 stage별 P95/위반 여부를
보고한다 - 정상 trial 판정이 아니라 순수 탐색·재설계용.

2026-09-18 버그 수정: 이전 버전은 probe의 첫 sent_at을 t0로 삼고 stage
"명목" duration_sec만으로 시간 구간을 잘랐다. 하지만 ramp.py는 각 stage
종료 시 미완료 요청을 최대 10초까지 기다리므로(stragglers), 실제 stage
경계는 stage마다 조금씩 밀리고 이게 누적된다 - 7단계 실행에서 명목
630초짜리 ramp가 실제로는 668.4초 걸렸다(explore-20260918T095809Z).
그 결과 "post-ramp drain" 버킷이 아직 안 끝난 마지막 stage의 트래픽을
그대로 포함해 drain이 회복 안 하는 것처럼 잘못 보였다. 이제 ramp.py가
직접 기록하는 실제 stage_start_utc/stage_end_utc(--summary-out)를 읽어
그 시각으로 나눈다 - 명목 계산을 전혀 쓰지 않는다. probe도 ramp보다
먼저 시작해 SLO 판정 가능한 baseline(BASELINE_SEC)을 확보한 뒤에만
ramp를 시작한다."""
import argparse
import csv
import io
import os
import statistics
import time
import uuid
from datetime import datetime
from pathlib import Path

import yaml

import slo_judge
from load_ramp_adapter import NAMESPACE, SETTLE_SEC, _delete_pod, _run, _wait_pod_ready

# 탐색 전용 이미지 - load_ramp_adapter.IMAGE("loadgen-runner:local", 본
# 실험/실제 trial harness가 쓰는 태그)는 그대로 두고 이 스크립트만 새
# 태그를 쓴다. stage 경계 버그 수정(ramp.py --summary-out 등, §23)이
# 반영된 이미지로, sj-worker에서 chaos/loadgen/ramp.py·probe.py·
# requirements.txt·Dockerfile만 격리된 임시 디렉터리로 복사해
# `docker build` 후 `docker save | ctr -n k8s.io images import`로
# 주입했다(§23.6). 기존 loadgen-runner:local은 덮어쓰지 않음.
# docker image ID(config digest): sha256:e58a37b2d1c5903d1ce50474fd00c7d3a39cb300549408c0e0c2305482db897a
# containerd k8s.io manifest digest: sha256:21d6b8ef8bcb1804a28359b2db7a64faae19853493bddb52202b72ac6e9b7aaf
# 이미지 내부 /ramp.py SHA-256(로컬 chaos/loadgen/ramp.py와 smoke pod에서 직접 대조 확인):
#   aadab9fc7f2a5a51cfee4e666ba7872c8e8fa378389d47a0e68e501f39153a82
IMAGE = "loadgen-runner:phase8-v3-boundaries"

RESULTS_DIR = Path(__file__).parent / "results"
POST_RAMP_DRAIN_SEC = 60  # ramp 완료 후에도 잠깐 더 관찰 - 마지막 stage의 꼬리 회복 확인
BASELINE_SEC = 60  # probe만 먼저 돌려 SLO 판정 가능한 최소 표본(1RPS*60=60건)과
                    # 60초 안정 구간을 확보한 뒤에만 ramp를 시작한다


def classify_stages(rows, ramp_stages):
    """rows: slo_judge.load_raw()가 반환한 probe 표본(sent_at=datetime, 정렬됨).
    ramp_stages: ramp.py가 실제로 기록한 stage별
    {"stage", "stage_start_utc": datetime, "stage_end_utc": datetime, ...}
    리스트, 실행 순서대로. 명목 duration_sec 계산이 아니라 이 실제 시각을
    경계로 쓴다 - stage 종료 시 최대 10초 straggler 대기 때문에 실제
    경계가 명목 경계보다 계속 밀릴 수 있어서다.
    반환: {"baseline": [...], "stages": [(stage_dict, [...]), ...], "drain": [...]}
    """
    if not ramp_stages:
        return {"baseline": rows, "stages": [], "drain": []}
    first_start = ramp_stages[0]["stage_start_utc"]
    last_end = ramp_stages[-1]["stage_end_utc"]
    baseline = [r for r in rows if r["sent_at"] < first_start]
    stages = []
    for s in ramp_stages:
        lo, hi = s["stage_start_utc"], s["stage_end_utc"]
        bucket = [r for r in rows if lo <= r["sent_at"] < hi]
        stages.append((s, bucket))
    drain = [r for r in rows if r["sent_at"] >= last_end]
    return {"baseline": baseline, "stages": stages, "drain": drain}


def bucket_stats(bucket):
    if not bucket:
        return {"n": 0, "success_rate": None, "mean": None, "p95": None, "max": None, "violates": None}
    lat = [r["latency"] for r in bucket]
    succ = sum(1 for r in bucket if r["success"])
    p95 = statistics.quantiles(lat, n=100)[94] if len(lat) >= 20 else max(lat)
    return {"n": len(bucket), "success_rate": succ / len(bucket), "mean": statistics.mean(lat),
            "p95": p95, "max": max(lat), "violates": p95 > slo_judge.LATENCY_THRESHOLD}


def parse_ramp_summary(text: str) -> list:
    rows = list(csv.DictReader(io.StringIO(text)))
    for r in rows:
        r["stage_start_utc"] = datetime.fromisoformat(r["stage_start_utc"])
        r["stage_end_utc"] = datetime.fromisoformat(r["stage_end_utc"])
    return rows


def _print_bucket(label, stats, extra=""):
    if not stats["n"]:
        print(f"{label:<22} {0:>5}  (데이터 없음) {extra}")
        return
    print(f"{label:<22} {stats['n']:>5} {stats['success_rate']:>7.1%} {stats['mean']:>8.3f} "
          f"{stats['p95']:>8.3f} {stats['max']:>8.3f} {'예' if stats['violates'] else '아니오':>6}  {extra}")


def check_node_and_pods(node_name="sj-worker", vllm_pod=None):
    """Node Ready·pressure, vLLM pod restart 여부를 확인한다. 재현성
    검증(반복 사이 quiescence·cooldown, §25)과 단발 탐색 양쪽에서 쓴다."""
    r = _run(["kubectl", "get", "node", node_name, "-o", "json"], check=True)
    import json as _json
    node = _json.loads(r.stdout)
    conditions = {c["type"]: c["status"] for c in node["status"]["conditions"]}
    node_ok = (conditions.get("Ready") == "True"
               and conditions.get("MemoryPressure") == "False"
               and conditions.get("DiskPressure") == "False"
               and conditions.get("PIDPressure") == "False")
    restart_count = None
    if vllm_pod:
        r2 = _run(["kubectl", "get", "pod", vllm_pod, "-n", NAMESPACE, "-o", "json"])
        if r2.returncode == 0:
            pod = _json.loads(r2.stdout)
            statuses = pod.get("status", {}).get("containerStatuses", [])
            restart_count = sum(c.get("restartCount", 0) for c in statuses)
    return {"node_ok": node_ok, "conditions": conditions, "restart_count": restart_count}


def run_candidate(ramp_config_path: str, probe_config_path: str, label: str = "explore") -> dict:
    """ramp+probe를 1회 실행하고 stage별 결과를 반환한다. baseline이 이미
    threshold를 넘으면(=진짜 정상 상태에서 시작한 게 아니면) ramp를 아예
    시작하지 않고 {"valid": False, "reason": "baseline_violating", ...}를
    반환한다(§25 재현성 기준 7번 - invalid calibration으로 분리)."""
    ramp_cfg = yaml.safe_load(Path(ramp_config_path).read_text(encoding="utf-8"))
    stages = ramp_cfg["stages"]
    total_ramp_sec = sum(s["duration_sec"] for s in stages)
    num_stages = len(stages)
    straggler_margin_sec = num_stages * 10

    run_id = f"{label}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    ramp_pod = f"{label[:7]}-ramp-{uuid.uuid4().hex[:6]}"
    probe_pod = f"{label[:7]}-probe-{uuid.uuid4().hex[:6]}"
    ramp_config_name = Path(ramp_config_path).name
    probe_config_name = Path(probe_config_path).name
    local_raw = RESULTS_DIR / f"probe-{run_id}-raw.csv"
    local_ramp_summary = RESULTS_DIR / f"ramp-{run_id}-summary.csv"

    probe_duration = BASELINE_SEC + total_ramp_sec + straggler_margin_sec + POST_RAMP_DRAIN_SEC + 30
    pod_sleep_sec = int(probe_duration) + 600

    print(f"run_id: {run_id}, baseline: {BASELINE_SEC}초, 총 ramp 시간(명목): {total_ramp_sec}초")

    result = {"run_id": run_id, "valid": True, "reason": None}
    try:
        for pod_name in (ramp_pod, probe_pod):
            _run(["kubectl", "run", pod_name, "-n", NAMESPACE, f"--image={IMAGE}",
                  "--image-pull-policy=Never", "--restart=Never", "--", "sleep", str(pod_sleep_sec)],
                 check=True)
        for pod_name in (ramp_pod, probe_pod):
            if not _wait_pod_ready(pod_name):
                raise RuntimeError(f"{pod_name} Ready 시간초과")

        _run(["kubectl", "cp", os.path.relpath(ramp_config_path), f"{NAMESPACE}/{ramp_pod}:/{ramp_config_name}"],
             check=True)
        _run(["kubectl", "cp", os.path.relpath(probe_config_path), f"{NAMESPACE}/{probe_pod}:/{probe_config_name}"],
             check=True)
        print(f"안정화 대기 {SETTLE_SEC}초...")
        time.sleep(SETTLE_SEC)

        # 1) probe를 ramp보다 먼저 시작 - SLO 판정 가능한 baseline을 먼저
        #    확보해야 ramp 시작 전 구간이 진짜 "정상 상태"였는지 사후에 볼 수 있다.
        probe_inner = (f"PYTHONUNBUFFERED=1 python /probe.py --config /{probe_config_name} "
                       f"--run-id {run_id} --scenario {label} --arm native --rep 1 "
                       f"--out /probe-raw.csv --duration-sec {probe_duration} "
                       f"> /probe.log 2>&1; echo $? > /probe.exit")
        _run(["kubectl", "exec", "-n", NAMESPACE, probe_pod, "--", "sh", "-c",
              f"nohup sh -c '{probe_inner}' < /dev/null > /probe-wrapper.log 2>&1 &"], check=True)

        print(f"probe 시작 - SLO 판정 가능한 baseline 확보 위해 {BASELINE_SEC}초 대기 후 ramp 시작")
        time.sleep(BASELINE_SEC)

        # baseline gate - ramp 시작 전, probe가 지금까지 쌓은 표본(60개
        # 가량, 아직 flush된 부분만)을 미리 읽어 이미 threshold를 넘고
        # 있으면 이 시도 자체를 무효로 치고 ramp를 시작하지 않는다.
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        r_baseline = _run(["kubectl", "exec", "-n", NAMESPACE, probe_pod, "--", "cat", "/probe-raw.csv"])
        local_raw.write_text(r_baseline.stdout, encoding="utf-8")
        baseline_rows = slo_judge.load_raw(local_raw) if r_baseline.stdout.strip() else []
        baseline_stats = bucket_stats(baseline_rows)
        result["baseline_precheck"] = baseline_stats
        if baseline_stats["violates"]:
            result["valid"] = False
            result["reason"] = "baseline_violating"
            print(f"baseline이 이미 threshold를 초과(P95={baseline_stats['p95']:.3f}s) - "
                  f"ramp를 시작하지 않고 invalid calibration으로 분리")
            return result

        # 2) baseline이 정상일 때만 ramp 시작. --summary-out으로 고정
        #    경로에 stage별 실제 시작/종료 UTC를 기록하게 한다.
        ramp_inner = (f"PYTHONUNBUFFERED=1 python /ramp.py --config /{ramp_config_name} "
                      f"--run-id {run_id} --method native --rep 1 "
                      f"--summary-out /ramp-summary.csv > /ramp.log 2>&1; echo $? > /ramp.exit")
        _run(["kubectl", "exec", "-n", NAMESPACE, ramp_pod, "--", "sh", "-c",
              f"nohup sh -c '{ramp_inner}' < /dev/null > /ramp-wrapper.log 2>&1 &"], check=True)

        t_start = time.monotonic()
        print(f"ramp 시작, 완주까지 대기(~{total_ramp_sec}초 + straggler 여유 {straggler_margin_sec}초)")
        deadline = t_start + total_ramp_sec + straggler_margin_sec + 60
        ramp_done = False
        while time.monotonic() < deadline:
            r = _run(["kubectl", "exec", "-n", NAMESPACE, ramp_pod, "--", "test", "-f", "/ramp.exit"])
            if r.returncode == 0:
                ramp_done = True
                break
            time.sleep(5)
        if not ramp_done:
            print("경고: ramp.py가 예상 시간 내에 안 끝남 - 현재까지 데이터로 진행")
        result["ramp_completed_in_time"] = ramp_done

        print(f"ramp 완료(경과 {time.monotonic() - t_start:.1f}초, 명목 {total_ramp_sec}초와의 차이가 "
              f"straggler 누적 지연) - post-ramp drain {POST_RAMP_DRAIN_SEC}초 대기")
        time.sleep(POST_RAMP_DRAIN_SEC)

        r = _run(["kubectl", "exec", "-n", NAMESPACE, probe_pod, "--", "cat", "/probe-raw.csv"], check=True)
        local_raw.write_text(r.stdout, encoding="utf-8")

        r2 = _run(["kubectl", "exec", "-n", NAMESPACE, ramp_pod, "--", "cat", "/ramp-summary.csv"], check=True)
        local_ramp_summary.write_text(r2.stdout, encoding="utf-8")
    finally:
        _delete_pod(ramp_pod)
        _delete_pod(probe_pod)

    if not result["valid"]:
        return result

    rows = slo_judge.load_raw(local_raw)
    ramp_stages = parse_ramp_summary(local_ramp_summary.read_text(encoding="utf-8"))
    buckets = classify_stages(rows, ramp_stages)

    result["local_raw"] = str(local_raw)
    result["local_ramp_summary"] = str(local_ramp_summary)
    result["baseline"] = bucket_stats(buckets["baseline"])
    result["stages"] = [{"stage": s["stage"], "stage_start_utc": s["stage_start_utc"],
                          "stage_end_utc": s["stage_end_utc"], **bucket_stats(bucket)}
                         for s, bucket in buckets["stages"]]
    result["drain"] = bucket_stats(buckets["drain"])
    all_buckets = [result["baseline"]] + result["stages"] + [result["drain"]]
    result["all_success_100pct"] = all(b["n"] and b["success_rate"] == 1.0 for b in all_buckets)
    return result


def print_result(result: dict):
    if not result["valid"]:
        print(f"\n=== INVALID ({result['reason']}) - run_id: {result['run_id']} ===")
        if result.get("baseline_precheck"):
            _print_bucket("baseline(무효)", result["baseline_precheck"])
        return
    print(f"\n=== 결과 (probe raw: {result['local_raw']}, ramp summary: {result['local_ramp_summary']}) ===")
    print("경계는 ramp.py가 기록한 실제 stage_start_utc/stage_end_utc 기준 - 명목 duration 아님\n")
    print(f"{'bucket':<22} {'n':>5} {'성공률':>8} {'mean':>8} {'P95':>8} {'max':>8} {'위반?':>6}  구간(UTC)")
    _print_bucket("baseline(ramp 전)", result["baseline"])
    for s in result["stages"]:
        window = f"{s['stage_start_utc'].isoformat()} ~ {s['stage_end_utc'].isoformat()}"
        _print_bucket(s["stage"], s, window)
    drain_from = result["stages"][-1]["stage_end_utc"].isoformat() if result["stages"] else "-"
    _print_bucket("post-ramp(drain)", result["drain"], f"{drain_from} 이후")
    print(f"\nlatency SLO threshold = {slo_judge.LATENCY_THRESHOLD}s")


def main():
    parser = argparse.ArgumentParser(description="ramp 강도 재설계용 stage별 sweep 탐색")
    parser.add_argument("--ramp-config", required=True)
    parser.add_argument("--probe-config", default=str(Path(__file__).parent.parent / "chaos" / "probe-config.yaml"))
    args = parser.parse_args()
    result = run_candidate(args.ramp_config, args.probe_config)
    print_result(result)


if __name__ == "__main__":
    main()
