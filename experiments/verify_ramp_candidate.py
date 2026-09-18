#!/usr/bin/env python3
"""load_ramp 최종 후보의 재현성을 사전 등록된 기준(docs/design/
phase8-blue-green-preflight-incident.md §25)으로 검증한다. explore_
ramp_intensity.run_candidate()를 반복 호출하고, 매 반복 사이 Node/Pod
quiescence를 확인한 뒤 cooldown을 두고, 끝나면 judge_candidate()로
기계적으로 판정한다 - 실행 중간에 판단을 바꾸지 않는다."""
import argparse
import time
from pathlib import Path

from explore_ramp_intensity import check_node_and_pods, print_result, run_candidate

COOLDOWN_SEC = 60
MAX_ATTEMPTS = 6  # 3회 유효 반복을 못 채우면(무효 baseline 반복 등) 무한루프 대신 여기서 멈춤


def judge_candidate(repetitions: list, low_rps=(0.025, 0.05), high_rps=(0.30, 0.40)) -> dict:
    """§25 사전 등록 기준을 유효한(valid=True) 반복에만 기계적으로 적용한다.
    repetitions: run_candidate() 반환값 리스트(무효 포함 가능, node_pod_clean
    키가 채워져 있어야 함)."""
    valid = [r for r in repetitions if r["valid"]]
    invalid = [r for r in repetitions if not r["valid"]]

    def stage_violates(rep, rps_target):
        for s in rep["stages"]:
            if abs(float(s.get("target_rps", -1)) - rps_target) < 1e-9:
                return s["violates"]
        return None

    checks = {"enough_valid_repetitions": len(valid) >= 3}

    for rps in low_rps:
        flags = [stage_violates(r, rps) for r in valid]
        checks[f"{rps}rps_never_violates"] = len(flags) > 0 and all(f is False for f in flags)

    for rps in high_rps:
        flags = [stage_violates(r, rps) for r in valid]
        violate_count = sum(1 for f in flags if f is True)
        checks[f"{rps}rps_violates_at_least_2_of_{len(flags) if flags else 3}"] = (
            len(flags) > 0 and violate_count >= 2)

    checks["all_runs_100pct_success"] = len(valid) > 0 and all(r["all_success_100pct"] for r in valid)
    checks["all_runs_drain_recovers"] = len(valid) > 0 and all(not r["drain"]["violates"] for r in valid)
    checks["all_runs_node_pod_clean"] = len(valid) > 0 and all(r.get("node_pod_clean") is True for r in valid)

    return {"overall_pass": all(checks.values()), "checks": checks,
            "num_valid": len(valid), "num_invalid": len(invalid)}


def wait_for_quiescence(vllm_pod: str, cooldown_sec: int):
    status = check_node_and_pods(vllm_pod=vllm_pod)
    print(f"quiescence 확인: node_ok={status['node_ok']}, restart_count={status['restart_count']}")
    print(f"cooldown {cooldown_sec}초 대기...")
    time.sleep(cooldown_sec)
    return status


def main():
    parser = argparse.ArgumentParser(description="load_ramp 후보 재현성 검증(사전 등록 기준 기계적 적용)")
    parser.add_argument("--ramp-config", required=True)
    parser.add_argument("--probe-config", default=str(Path(__file__).parent.parent / "chaos" / "probe-config.yaml"))
    parser.add_argument("--vllm-pod", required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--cooldown-sec", type=int, default=COOLDOWN_SEC)
    args = parser.parse_args()

    repetitions = []
    attempt = 0
    while sum(1 for r in repetitions if r["valid"]) < args.repetitions and attempt < MAX_ATTEMPTS:
        attempt += 1
        print(f"\n{'=' * 20} 반복 시도 {attempt}(유효 {sum(1 for r in repetitions if r['valid'])}/{args.repetitions}) {'=' * 20}")
        status_before = check_node_and_pods(vllm_pod=args.vllm_pod)
        if not status_before["node_ok"]:
            print("시작 전 Node가 정상이 아님 - 안전을 위해 중단")
            break

        result = run_candidate(args.ramp_config, args.probe_config, label="verify")
        status_after = check_node_and_pods(vllm_pod=args.vllm_pod)
        result["node_pod_clean"] = bool(
            status_after["node_ok"]
            and status_before["restart_count"] is not None
            and status_after["restart_count"] == status_before["restart_count"])
        print_result(result)
        repetitions.append(result)

        if not status_after["node_ok"]:
            print("실행 후 Node 이상 감지 - 안전을 위해 중단")
            break
        if not result["node_pod_clean"]:
            print("실행 후 pod restart 증가 감지 - 안전을 위해 중단")
            break

        if sum(1 for r in repetitions if r["valid"]) < args.repetitions and attempt < MAX_ATTEMPTS:
            wait_for_quiescence(args.vllm_pod, args.cooldown_sec)

    verdict = judge_candidate(repetitions)
    print(f"\n{'=' * 20} 최종 판정 {'=' * 20}")
    print(f"유효 반복: {verdict['num_valid']}, 무효(invalid calibration): {verdict['num_invalid']}")
    for k, v in verdict["checks"].items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    print(f"\n종합: {'PASS' if verdict['overall_pass'] else 'FAIL'}")
    return repetitions, verdict


if __name__ == "__main__":
    main()
