#!/usr/bin/env python3
"""§59 사전 등록 - `active_plus_preview` topology 정상 데이터 실제 수집
(2026-09-20). 세션 하나 = regime 하나(low_load/sustained_load/burst 중
하나만, 여러 regime을 한 세션에서 연속 측정하지 않는다 - 지시).

**새 클러스터 조작 코드를 만들지 않는다** - 전부 이미 실전 검증된 기존
경로를 그대로 재사용한다:
  - `blue_green_prep.prepare_preview_with_rollback()`/`cleanup_unpromoted_
    preview()`(모든 non-native 파일럿이 이미 쓰는 preview 준비/정리 -
    promotion은 절대 하지 않고, Ready 상태로만 유지하다가 세션이 끝나면
    무조건 abort + 단일 revision 복원을 실측 재확인한다)
  - `explore_ramp_intensity.run_candidate()`/`check_node_and_pods()`(ramp
    pod+probe pod 동시 실행, baseline precheck, stage별 SLO violates
    판정 - 전부 기존 로직 그대로, 새 SLO 판정 없음)
  - `v3/build_dataset.py`(고정 60초/15초 window, strict completeness -
    ramp.py가 실제로 기록한 stage_start_utc/stage_end_utc로만 Prometheus
    재조회, 명목 시각 안 씀)

qualification(`--pilot`, `is_pilot=true`)과 official 수집은 이 스크립트를
그대로 재사용하되 `--official` 플래그로만 구분한다 - qualification
세션은 이 플래그를 안 주므로 아무리 깨끗해도 `included_in_training`이
항상 False로 남는다(exploratory와 official 자료를 구조적으로 분리,
§59.1 지시)."""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

EXPERIMENTS_DIR = Path(__file__).parent.parent.parent / "experiments"
sys.path.insert(0, str(EXPERIMENTS_DIR))
sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

from active_pod_resolver import get_active_pods  # noqa: E402
from blue_green_prep import cleanup_unpromoted_preview, prepare_preview_with_rollback  # noqa: E402
from explore_ramp_intensity import check_node_and_pods, run_candidate  # noqa: E402
from memory_pressure_adapter import get_pod_details  # noqa: E402

from build_dataset import build_rows_for_session, summarize_inventory  # noqa: E402
from windows import CandidateSession  # noqa: E402

ROLLOUT_NAME = "vllm-serving"
NAMESPACE = "vllm-serving"
SETTLE_AFTER_PREVIEW_READY_SEC = 30.0  # windows.py의 기존 active_plus_preview 후보와 동일 여유
DEFAULT_PROBE_CONFIG = str(Path(__file__).parent.parent.parent / "chaos" / "probe-config.yaml")
REGIME_CONFIGS_DIR = Path(__file__).parent / "regime_configs"
SESSIONS_DIR = Path(__file__).parent / "data" / "sessions"

REGIME_CONFIG_PATHS = {
    "low_load": REGIME_CONFIGS_DIR / "low-load.yaml",
    "sustained_load": REGIME_CONFIGS_DIR / "sustained-load.yaml",
    "burst": REGIME_CONFIGS_DIR / "burst.yaml",
}


def judge_session_exclusion(candidate_result: dict, node_before: dict, node_after: dict,
                             oom_before: bool, oom_after: bool, target_replaced: bool,
                             cleanup_ok) -> tuple:
    """§59.1 제외 규칙을 기계적으로 적용한다(순수 함수, 오프라인 테스트
    대상) - sustained SLO 위반·restart·OOM·Node 이상·target replacement·
    cleanup 실패 중 하나라도 있으면 제외. (excluded: bool, reasons: list)."""
    reasons = []
    if not candidate_result.get("valid", True):
        reasons.append(f"ramp/probe 실행 자체가 invalid: {candidate_result.get('reason')}")
    stages = candidate_result.get("stages") or []
    if stages and stages[0].get("violates"):
        reasons.append(f"stage에서 sustained SLO 위반(P95={stages[0].get('p95')})")
    if not candidate_result.get("all_success_100pct", True):
        reasons.append("요청 성공률 100% 아님(baseline/stage/drain 중 하나라도)")
    if not node_before.get("node_ok", True) or not node_after.get("node_ok", True):
        reasons.append("Node 상태 이상(수집 전 또는 후)")
    rc_before, rc_after = node_before.get("restart_count"), node_after.get("restart_count")
    if rc_before is not None and rc_after is not None and rc_before != rc_after:
        reasons.append(f"restartCount 변화: {rc_before}->{rc_after}")
    if oom_before or oom_after:
        reasons.append("OOMKilled 관측(수집 전 또는 후)")
    if target_replaced:
        reasons.append("target pod 교체 감지(promotion 또는 예기치 않은 재시작)")
    if cleanup_ok is False:
        reasons.append("cleanup_unpromoted_preview 실패(단일 revision 복원 실패)")
    return (len(reasons) > 0, reasons)


def collect_one_session(regime: str, session_id: str, is_pilot: bool,
                         included_in_training_if_clean: bool) -> dict:
    if regime not in REGIME_CONFIG_PATHS:
        raise ValueError(f"알 수 없는 regime: {regime}(허용: {list(REGIME_CONFIG_PATHS)})")
    ramp_config = str(REGIME_CONFIG_PATHS[regime])

    session = {
        "session_id": session_id, "regime": regime, "topology": "active_plus_preview",
        "is_pilot": is_pilot, "included_in_training": False,
        "t_session_start": datetime.now(timezone.utc).isoformat(),
    }

    pods_before = get_active_pods()
    if len(pods_before) != 1:
        raise RuntimeError(f"세션 시작 전 active pod이 1개가 아님({len(pods_before)}개) - fail-closed, 세션 시작 안 함")
    active_before = pods_before[0]
    session["active_pod_before"] = {"name": active_before["name"], "uid": active_before["uid"]}

    node_before = check_node_and_pods(vllm_pod=active_before["name"])
    details_before = get_pod_details(active_before["name"]) or {}
    if not node_before["node_ok"]:
        raise RuntimeError("세션 시작 전 Node가 정상이 아님 - fail-closed, 세션 시작 안 함")

    print(f"[{session_id}] preview 준비 시작(promotion 없음, Ready까지만)...")
    prep_info = prepare_preview_with_rollback(ROLLOUT_NAME, NAMESPACE)
    session["preview_prep_info"] = prep_info

    if not prep_info["ready"]:
        session["excluded"] = True
        session["exclusion_reasons"] = ["preview 준비 실패(timeout) - prepare_preview_with_rollback 자체 rollback으로 이미 복원됨"]
        session["t_session_end"] = datetime.now(timezone.utc).isoformat()
        print(f"[{session_id}] preview 준비 실패 - 세션 제외, 이미 자동 복원됨")
        return session

    print(f"[{session_id}] preview Ready(pod_hash={prep_info['created_pod_hash']}) - "
          f"{SETTLE_AFTER_PREVIEW_READY_SEC:.0f}초 추가 안정화 대기")
    time.sleep(SETTLE_AFTER_PREVIEW_READY_SEC)

    try:
        print(f"[{session_id}] {regime} 부하 실행(run_candidate, ramp+probe 동시)...")
        candidate_result = run_candidate(ramp_config, DEFAULT_PROBE_CONFIG, label=f"v3{regime[:5]}")
    finally:
        print(f"[{session_id}] preview 정리(abort + 단일 revision 복원 확인)...")
        cleanup_ok = cleanup_unpromoted_preview(prep_info, ROLLOUT_NAME, NAMESPACE)
        session["cleanup_unpromoted_preview_result"] = cleanup_ok

    session["t_session_end"] = datetime.now(timezone.utc).isoformat()
    session["ramp_candidate_result"] = {
        k: v for k, v in candidate_result.items() if k not in ("stages",)
    }
    session["ramp_candidate_result"]["stages"] = [
        {**{sk: (sv.isoformat() if hasattr(sv, "isoformat") else sv) for sk, sv in s.items()}}
        for s in (candidate_result.get("stages") or [])
    ]

    pods_after = get_active_pods()
    target_replaced = len(pods_after) != 1 or pods_after[0]["uid"] != active_before["uid"]
    node_after = check_node_and_pods(vllm_pod=pods_after[0]["name"] if len(pods_after) == 1 else None)
    details_after = (get_pod_details(pods_after[0]["name"]) or {}) if len(pods_after) == 1 else {}
    session["active_pod_after"] = (
        {"name": pods_after[0]["name"], "uid": pods_after[0]["uid"]} if len(pods_after) == 1 else None)

    excluded, reasons = judge_session_exclusion(
        candidate_result, node_before, node_after,
        oom_before=bool(details_before.get("oom_killed")), oom_after=bool(details_after.get("oom_killed")),
        target_replaced=target_replaced, cleanup_ok=cleanup_ok,
    )
    session["excluded"] = excluded
    session["exclusion_reasons"] = reasons

    if candidate_result.get("valid") and candidate_result.get("stages"):
        stage = candidate_result["stages"][0]
        cand = CandidateSession(
            session_id=session_id, regime=regime, topology="active_plus_preview",
            start_utc=stage["stage_start_utc"], end_utc=stage["stage_end_utc"],
            source_run_id=candidate_result["run_id"],
        )
        rows = build_rows_for_session(cand)
        session["feature_rows"] = [
            {"window_start_utc": r.window_start_utc, "window_end_utc": r.window_end_utc,
             "valid": r.valid, "invalid_reason": r.invalid_reason, "features": r.features}
            for r in rows
        ]
        session["inventory"] = summarize_inventory(rows, [cand])
    else:
        session["feature_rows"] = []
        session["inventory"] = None

    if not excluded and included_in_training_if_clean:
        session["included_in_training"] = True

    status = "EXCLUDED" if excluded else ("official" if session["included_in_training"] else "qualification(제외 아님, 미포함)")
    print(f"[{session_id}] 완료 - {status}" + (f" - 사유: {reasons}" if reasons else ""))
    return session


def main():
    parser = argparse.ArgumentParser(description="§59 active_plus_preview 정상 데이터 세션 1회 수집")
    parser.add_argument("--regime", required=True, choices=list(REGIME_CONFIG_PATHS))
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--pilot", action="store_true", help="qualification 세션(is_pilot=true) - included_in_training 항상 False")
    parser.add_argument("--official", action="store_true",
                         help="깨끗하면(excluded=False) included_in_training=True로 표시(공식 수집용)")
    args = parser.parse_args()

    if args.pilot and args.official:
        parser.error("--pilot과 --official은 동시에 줄 수 없음(qualification/official 자료 분리 원칙)")

    session = collect_one_session(args.regime, args.session_id, is_pilot=args.pilot,
                                   included_in_training_if_clean=args.official)

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SESSIONS_DIR / f"{args.session_id}.json"
    out_path.write_text(json.dumps(session, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"세션 기록 저장: {out_path}")
    sys.exit(1 if session.get("excluded") else 0)


if __name__ == "__main__":
    main()
