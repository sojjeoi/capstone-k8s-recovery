#!/usr/bin/env python3
"""§61 A-B-A topology 진단(2026-09-20) - `active_plus_preview`에서 관측된
qualification 지연 급증(§60)이 topology 자체 때문인지, CPU 3-core cutover
(§58, calibration은 4-core 시절 값)나 다른 클러스터/하니스 변동 때문인지
분리한다.

collect_session.py와 동일하게 **새 클러스터 조작 코드를 만들지 않는다** -
prepare_preview_with_rollback/abort_preview/wait_until_rolled_back/
run_candidate/check_node_and_pods/get_pod_details 전부 기존 실전 검증
경로 그대로 재사용. 이 스크립트가 새로 하는 일은 그 호출들을
A1(active-only)→B(active+preview)→A2(abort 후 active-only) 순서로
묶는 것뿐이다.

전량 diagnostic pilot - 학습·threshold 결정에서 제외(§61.1)."""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

EXPERIMENTS_DIR = Path(__file__).parent.parent.parent.parent / "experiments"
V3_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(EXPERIMENTS_DIR))
sys.path.insert(0, str(V3_DIR))
sys.stdout.reconfigure(encoding="utf-8")

from active_pod_resolver import get_active_pods  # noqa: E402
from blue_green_prep import (  # noqa: E402
    abort_preview,
    get_blue_green_status,
    prepare_preview_with_rollback,
    wait_until_rolled_back,
)
from explore_ramp_intensity import check_node_and_pods  # noqa: E402
from memory_pressure_adapter import get_pod_details  # noqa: E402

from collect_session import run_candidate_with_retry  # noqa: E402

ROLLOUT_NAME = "vllm-serving"
NAMESPACE = "vllm-serving"
DIAG_DIR = Path(__file__).parent
RAMP_CONFIG = str(DIAG_DIR / "aba-low-load.yaml")
PROBE_CONFIG = str(Path(__file__).parent.parent.parent.parent / "chaos" / "probe-config.yaml")
STATE_PATH = DIAG_DIR / "aba_state.json"

# 사용자 지시 §61.2 - "B의 preview는 Ready 후 최소 60초 settle"(공식 수집의
# 30초 SETTLE_AFTER_PREVIEW_READY_SEC보다 김 - 진단 전용 값이라 별도 상수).
SETTLE_AFTER_PREVIEW_READY_SEC = 60.0

LEG_TOPOLOGY = {"a1": "active_only", "b": "active_plus_preview", "a2": "active_only_post_abort"}
# label[:7]이 그대로 pod 이름 접두어가 된다(run_candidate) - 전부 밑줄 없이
# RFC 1123 안전(§60에서 밑줄 때문에 100% 재현 실패했던 버그의 재발 방지).
LEG_LABELS = {"a1": "aba-a1", "b": "aba-b", "a2": "aba-a2"}


def _pod_snapshot():
    pods = get_active_pods()
    if len(pods) != 1:
        raise RuntimeError(f"active pod이 1개가 아님({len(pods)}개) - fail-closed")
    return pods[0]


def _pre_post_checks(pod_name):
    node = check_node_and_pods(vllm_pod=pod_name)
    details = get_pod_details(pod_name) or {}
    return node, details


def run_leg(leg: str) -> dict:
    if leg not in LEG_TOPOLOGY:
        raise ValueError(f"알 수 없는 leg: {leg}(허용: {list(LEG_TOPOLOGY)})")

    result = {
        "leg": leg, "topology": LEG_TOPOLOGY[leg], "is_pilot": True, "included_in_training": False,
        "diagnostic_only": True, "t_leg_start": datetime.now(timezone.utc).isoformat(),
    }

    if leg == "a2":
        if not STATE_PATH.exists():
            raise RuntimeError("a2는 b가 먼저 실행돼 저장한 상태가 필요함 - aba_state.json 없음")
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        prep_info = state["preview_prep_info"]
        print("[a2] 이전(b) 단계에서 준비된 preview를 abort하고 단일 revision 복원 확인...")
        status_before_abort = get_blue_green_status(ROLLOUT_NAME, NAMESPACE)
        result["blue_green_status_before_abort"] = status_before_abort
        pre_active = prep_info["pre_prepare_active_selector"]
        our_hash = prep_info["created_pod_hash"]
        if status_before_abort["active_selector"] != pre_active or status_before_abort["current_pod_hash"] != our_hash:
            raise RuntimeError(
                "fail-closed: abort 직전 상태가 b가 기록한 준비 직후 상태와 다름"
                f"(pre_active={pre_active}, our_hash={our_hash}, 현재={status_before_abort}) - "
                "이미 승격됐거나 다른 변경이 있었을 수 있어 a2를 진행하지 않음")
        abort_preview(ROLLOUT_NAME, NAMESPACE)
        rolled_back = wait_until_rolled_back(ROLLOUT_NAME, NAMESPACE, pre_active, our_hash)
        result["abort_and_rollback_ok"] = rolled_back
        if not rolled_back:
            raise RuntimeError("fail-closed: abort 후 단일 revision 복원 확인 실패 - a2 측정 중단")
        print("[a2] 단일 revision 복원 확인됨 - active-only 상태에서 측정 시작")

    active_pod = _pod_snapshot()
    result["active_pod_before"] = {"name": active_pod["name"], "uid": active_pod["uid"]}
    node_before, details_before = _pre_post_checks(active_pod["name"])
    result["node_before"] = node_before
    result["oom_before"] = bool(details_before.get("oom_killed"))
    if not node_before["node_ok"]:
        raise RuntimeError(f"[{leg}] fail-closed: 측정 시작 전 Node가 정상이 아님 - 중단")

    if leg == "b":
        print("[b] preview 준비 시작(promotion 없음, Ready까지만)...")
        prep_info = prepare_preview_with_rollback(ROLLOUT_NAME, NAMESPACE)
        result["preview_prep_info"] = prep_info
        if not prep_info["ready"]:
            result["excluded"] = True
            result["exclusion_reasons"] = ["preview 준비 실패(timeout) - 자체 rollback으로 이미 복원됨 - stop 조건(§61.4)"]
            result["t_leg_end"] = datetime.now(timezone.utc).isoformat()
            STATE_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            print("[b] preview 준비 실패 - stop 조건 - a2 진행하지 않음")
            return result
        print(f"[b] preview Ready(pod_hash={prep_info['created_pod_hash']}) - "
              f"{SETTLE_AFTER_PREVIEW_READY_SEC:.0f}초 settle(§61.2 - preview에는 트래픽 보내지 않음, active Service만 사용)")
        time.sleep(SETTLE_AFTER_PREVIEW_READY_SEC)
        STATE_PATH.write_text(json.dumps({"preview_prep_info": prep_info}, indent=2, ensure_ascii=False, default=str),
                               encoding="utf-8")

    print(f"[{leg}] run_candidate 실행({LEG_TOPOLOGY[leg]}, RPS=0.10, 90초 stage)...")
    candidate_result = run_candidate_with_retry(RAMP_CONFIG, PROBE_CONFIG, label=LEG_LABELS[leg])
    result["ramp_candidate_result"] = {k: v for k, v in candidate_result.items() if k != "stages"}
    result["ramp_candidate_result"]["stages"] = [
        {sk: (sv.isoformat() if hasattr(sv, "isoformat") else sv) for sk, sv in s.items()}
        for s in (candidate_result.get("stages") or [])
    ]

    pods_after = get_active_pods()
    target_replaced = len(pods_after) != 1 or pods_after[0]["uid"] != active_pod["uid"]
    result["target_replaced"] = target_replaced
    result["active_pod_after"] = (
        {"name": pods_after[0]["name"], "uid": pods_after[0]["uid"]} if len(pods_after) == 1 else None)
    node_after, details_after = _pre_post_checks(pods_after[0]["name"] if len(pods_after) == 1 else None)
    result["node_after"] = node_after
    result["oom_after"] = bool(details_after.get("oom_killed"))

    stop_reasons = []
    if not node_after.get("node_ok", True):
        stop_reasons.append("Node 이상(측정 후)")
    if result["oom_after"]:
        stop_reasons.append("OOMKilled 관측")
    if target_replaced:
        stop_reasons.append("target pod 교체(예기치 않은 promotion/재시작) 감지")
    result["stop_condition_triggered"] = len(stop_reasons) > 0
    result["stop_reasons"] = stop_reasons

    result["t_leg_end"] = datetime.now(timezone.utc).isoformat()
    return result


def main():
    parser = argparse.ArgumentParser(description="§61 A-B-A topology 진단 - 1개 leg 실행")
    parser.add_argument("--leg", required=True, choices=["a1", "b", "a2"])
    args = parser.parse_args()

    result = run_leg(args.leg)
    out_path = DIAG_DIR / f"aba-{args.leg}-result.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"[{args.leg}] 결과 저장: {out_path}")
    if result.get("stop_condition_triggered") or result.get("excluded"):
        print(f"[{args.leg}] stop 조건 발생(§61.4) - 이후 leg 진행 금지")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
