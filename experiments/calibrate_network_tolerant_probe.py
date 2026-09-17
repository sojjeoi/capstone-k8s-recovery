#!/usr/bin/env python3
"""network_tolerant probe profile(gitops/apps/vllm-serving/overlays/
network-tolerant/)의 readinessProbe/livenessProbe.timeoutSeconds 후보값이
실제로 안전한지 확인하는 1회성 calibration - calibrate_probe_only.py와 같은
"run_once() 상태머신 없이 필요한 부분만 직접 구현"패턴.

전제조건(이 스크립트가 대신 해주지 않음 - run_id 등 실험 태깅과 무관한
클러스터 설정 변경이라 network_degrade_adapter.py의 책임 밖):
  1. kubectl apply -k gitops/apps/vllm-serving/overlays/network-tolerant/
     --load-restrictor=LoadRestrictionsNone 로 overlay 적용
  2. preview Ready 확인 후 promote로 active 전환
(순서는 gitops/apps/vllm-serving/overlays/network-tolerant/kustomization.yaml
상단 주석 참고)

측정 방법: NetworkChaos 최악 조건(기본 4000ms/400ms jitter, chaos/scenario-
network-degrade.yaml stage-4와 동일)을 active pod에 주입하고, 주입 구간
내내(끝에서 한 번만이 아니라) restartCount와 vllm-active Endpoints 소속
여부를 폴링해서 "probe 자체가 false-positive로 재시작·제외를 유발하는지"를
직접 관측한다(발견 5 재현 여부 확인 - gitops/apps/vllm-serving/overlays/
network-tolerant/probe-timeout-patch.yaml의 TODO 참고, 임의값 확정 금지).
CR은 항상 삭제한다(try/finally, network_degrade_adapter.py의 cleanup()과
같은 이유).
"""
import argparse
import subprocess
import time
import uuid

from active_pod_resolver import NAMESPACE, get_active_pods
from network_degrade_adapter import create_network_chaos, delete_network_chaos

WATCH_POLL_INTERVAL_SEC = 3


def _run(cmd, check=False):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=check)


def _restart_count(pod_name: str) -> int:
    r = _run(["kubectl", "get", "pod", pod_name, "-n", NAMESPACE,
              "-o", "jsonpath={.status.containerStatuses[0].restartCount}"])
    return int(r.stdout.strip() or "0")


def _readiness_timeout_sec(pod_name: str) -> str:
    r = _run(["kubectl", "get", "pod", pod_name, "-n", NAMESPACE,
              "-o", "jsonpath={.spec.containers[0].readinessProbe.timeoutSeconds}"])
    return r.stdout.strip() or "(미설정 - K8s 기본값 1초)"


def _is_in_active_endpoints(pod_name: str) -> bool:
    r = _run(["kubectl", "get", "endpoints", "vllm-active", "-n", NAMESPACE,
              "-o", "jsonpath={.subsets[*].addresses[*].targetRef.name}"])
    return pod_name in r.stdout.split()


def main():
    parser = argparse.ArgumentParser(
        description="network_tolerant probe timeoutSeconds 후보값이 NetworkChaos 최악조건에서 "
                     "false-positive 재시작/endpoint 제외를 유발하지 않는지 확인")
    parser.add_argument("--latency-ms", type=int, default=4000, help="chaos/scenario-network-degrade.yaml stage-4와 동일 기본값")
    parser.add_argument("--jitter-ms", type=int, default=400)
    parser.add_argument("--duration-sec", type=float, default=90, help="stage-4의 duration_sec과 동일 기본값")
    args = parser.parse_args()

    pods = get_active_pods()
    if len(pods) != 1:
        raise RuntimeError(f"active pod이 정확히 1개가 아님({len(pods)}개) - calibration 중단")
    pod_name = pods[0]["name"]

    run_id = f"calib-net-tolerant-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    cr_name = f"netdelay-calib-{uuid.uuid4().hex[:6]}"
    stage = {"latency": f"{args.latency_ms}ms", "jitter": f"{args.jitter_ms}ms"}

    print(f"run_id: {run_id}")
    print(f"대상 pod: {pod_name}")
    print(f"현재 readinessProbe.timeoutSeconds: {_readiness_timeout_sec(pod_name)}")
    print(f"주입: latency={stage['latency']} jitter={stage['jitter']} duration={args.duration_sec}s")

    restart_before = _restart_count(pod_name)
    in_endpoints_before = _is_in_active_endpoints(pod_name)
    print(f"주입 전: restartCount={restart_before}, vllm-active 소속={in_endpoints_before}")
    if not in_endpoints_before:
        raise RuntimeError("주입 전부터 이미 active pod이 vllm-active endpoints에 없음 - calibration 무의미, 중단")

    max_restart_seen = restart_before
    endpoint_dropped_at = None

    try:
        create_network_chaos(cr_name, run_id, "calibration", pod_name, stage)
        deadline = time.monotonic() + args.duration_sec
        while time.monotonic() < deadline:
            time.sleep(WATCH_POLL_INTERVAL_SEC)
            current_restart = _restart_count(pod_name)
            max_restart_seen = max(max_restart_seen, current_restart)
            if not _is_in_active_endpoints(pod_name) and endpoint_dropped_at is None:
                endpoint_dropped_at = time.monotonic() - (deadline - args.duration_sec)
    finally:
        delete_network_chaos(cr_name)

    time.sleep(5)  # NetworkChaos 삭제(복구) 직후 안정화 여유
    restart_after = _restart_count(pod_name)
    in_endpoints_after = _is_in_active_endpoints(pod_name)

    restarted = max_restart_seen > restart_before
    dropped = endpoint_dropped_at is not None

    print(f"\n=== calibration 결과 ===")
    print(f"restartCount: {restart_before} -> 주입중 최대 {max_restart_seen} -> 종료후 {restart_after}")
    print(f"vllm-active 소속: 종료후 {in_endpoints_after}"
          + (f" (주입 시작 후 {endpoint_dropped_at:.0f}초 시점에 한번 이상 이탈 관측)" if dropped else ", 주입 내내 유지"))
    if restarted or dropped:
        print("FAIL - false-positive 재시작 또는 endpoint 이탈 관측됨. "
              "readinessProbe/livenessProbe.timeoutSeconds 후보값을 더 올려서 재시도할 것"
              "(gitops/apps/vllm-serving/overlays/network-tolerant/probe-timeout-patch.yaml).")
    else:
        print("PASS - 이 조건(최대 지연 기준)에서는 false-positive 재시작/endpoint 이탈 없음. "
              "다만 이것으로 값을 '확정'하려면 최소 반복 실행으로 재현성도 확인할 것 "
              "(이 스크립트는 1회 실행만 수행함).")


if __name__ == "__main__":
    main()
