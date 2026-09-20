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
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

EXPERIMENTS_DIR = Path(__file__).parent.parent.parent / "experiments"
sys.path.insert(0, str(EXPERIMENTS_DIR))
sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

from active_pod_resolver import get_active_pods  # noqa: E402
from blue_green_prep import (  # noqa: E402
    abort_preview,
    cleanup_unpromoted_preview,
    get_blue_green_status,
    prepare_preview_with_rollback,
    wait_until_rolled_back,
)
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

# 2026-09-20 실측 발견(6회 연속 재현, stderr 확보 후 확정) - explore_ramp_
# intensity.run_candidate()는 label[:7]을 그대로 pod 이름에 쓰는데
# (f"{label[:7]}-ramp-{uuid}"), regime 이름의 "_"가 label에 그대로
# 들어가면(f"v3{regime[:5]}") K8s 리소스 이름 규칙(RFC 1123, 소문자
# 영숫자·하이픈만 허용)을 어겨 kubectl run이 매번 실패한다 - "low_load"는
# 앞 5글자("low_l")에 밑줄이 걸려 있어 100% 재현됐고, "sustained_load"/
# "burst"는 우연히 앞 5글자에 밑줄이 없어 안 걸렸을 뿐이다. 결측/일시
# 오류가 아니라 이름 규칙 위반이었다 - 밑줄을 하이픈으로 바꿔 규칙을
# 지킨다(모든 regime에 안전하게 적용).
REGIME_RUN_LABELS = {regime: f"v3{regime[:5]}".replace("_", "-") for regime in REGIME_CONFIG_PATHS}

RUN_CANDIDATE_MAX_ATTEMPTS = 3
RUN_CANDIDATE_RETRY_BACKOFF_SEC = 15.0


def run_candidate_with_retry(ramp_config: str, probe_config: str, label: str) -> dict:
    """`explore_ramp_intensity.run_candidate()`를 감싸 일시적 kubectl 오류
    (`subprocess.CalledProcessError`)에 한해 재시도한다(2026-09-20 실측
    발견 - qualification 1~3차 시도가 전부 preview settle 직후 ramp pod
    생성 단계에서 kubectl run이 이유 불명의 일시 오류로 실패했다가, 별도
    진단 재현에서는 완전히 같은 순서로 정상 성공함 - 클러스터 쪽의
    간헐적 문제로 판단). `run_candidate()`는 매 호출마다 uuid로 새 pod
    이름을 만들고 자기 finally에서 자기 pod를 정리하므로 재시도가
    안전하다(이전 시도의 잔여물과 충돌하지 않음). 도메인 판정(baseline_
    violating 등 valid=False)은 재시도 대상이 아니다 - 인프라 오류만
    재시도한다, 새 SLO/도메인 판정 로직을 만들지 않는다."""
    last_error = None
    for attempt in range(1, RUN_CANDIDATE_MAX_ATTEMPTS + 1):
        try:
            result = run_candidate(ramp_config, probe_config, label=label)
            result["run_candidate_attempts"] = attempt
            return result
        except subprocess.CalledProcessError as e:
            last_error = e
            print(f"run_candidate() 시도 {attempt}/{RUN_CANDIDATE_MAX_ATTEMPTS} 실패(일시 kubectl 오류로 판단): {e}")
            # 2026-09-20 추가 - CalledProcessError.__str__()은 stdout/stderr를
            # 안 보여준다(반복 재현 중 실제 원인을 못 봐서 진단이 막혔음).
            # 재시도 여부 판단과 무관하게 항상 실제 stderr/stdout을 남긴다.
            print(f"  stdout={e.stdout!r}")
            print(f"  stderr={e.stderr!r}")
            if attempt < RUN_CANDIDATE_MAX_ATTEMPTS:
                time.sleep(RUN_CANDIDATE_RETRY_BACKOFF_SEC)
    raise last_error


def verify_and_force_cleanup(prep_info, cleanup_ok, get_status_fn=None, abort_fn=None, wait_rolled_back_fn=None):
    """2026-09-20 실측 발견(qual-low_load 1차 시도) - `cleanup_unpromoted_
    preview()`가 이유가 뚜렷하지 않은 채 정리를 건너뛰어(반환값 None)
    preview가 방치된 사례가 있었다(Rollout이 Paused로 남고 `status.abort`가
    끝내 세팅 안 됨 - kubectl로 직접 확인). `cleanup_ok`가 이미 True/False로
    확정된 경우(원 함수가 실제로 시도한 경우)는 그대로 반환한다 - 이 함수는
    **None(스킵) 케이스만** 독립적으로 재확인해 방어한다: 실제로 여전히
    미승격 상태(activeSelector·pod_hash가 준비 직후 그대로)면 abort를 직접
    재시도하고, 이미 승격됐거나(정상) 다른 변경이 있었으면(fail-closed
    원칙 유지) 그대로 손대지 않는다. 순수 로직만 여기 두고 실제 클러스터
    호출은 주입된 함수로 분리해 오프라인 테스트 가능하게 한다."""
    if cleanup_ok is not None:
        return cleanup_ok
    if prep_info is None or not prep_info.get("ready"):
        return None
    get_status_fn = get_status_fn or (lambda: get_blue_green_status(ROLLOUT_NAME, NAMESPACE))
    abort_fn = abort_fn or (lambda: abort_preview(ROLLOUT_NAME, NAMESPACE))
    wait_rolled_back_fn = wait_rolled_back_fn or (
        lambda pre_active, our_hash: wait_until_rolled_back(ROLLOUT_NAME, NAMESPACE, pre_active, our_hash))
    current = get_status_fn()
    pre_active = prep_info.get("pre_prepare_active_selector")
    our_hash = prep_info.get("created_pod_hash")
    if current["active_selector"] != pre_active or current["current_pod_hash"] != our_hash:
        return None  # 이미 승격됐거나 다른 변경 - 원 함수와 동일한 fail-closed 판단 유지
    abort_fn()
    return wait_rolled_back_fn(pre_active, our_hash)


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
        print(f"[{session_id}] {regime} 부하 실행(run_candidate, ramp+probe 동시, 일시 오류 시 최대 "
              f"{RUN_CANDIDATE_MAX_ATTEMPTS}회 재시도)...")
        candidate_result = run_candidate_with_retry(ramp_config, DEFAULT_PROBE_CONFIG, label=REGIME_RUN_LABELS[regime])
    finally:
        print(f"[{session_id}] preview 정리(abort + 단일 revision 복원 확인)...")
        cleanup_ok = cleanup_unpromoted_preview(prep_info, ROLLOUT_NAME, NAMESPACE)
        forced_cleanup_ok = verify_and_force_cleanup(prep_info, cleanup_ok)
        if forced_cleanup_ok != cleanup_ok:
            print(f"[{session_id}] cleanup_unpromoted_preview가 스킵(None)했지만 독립 재확인 결과 "
                  f"미승격 상태로 남아있어 abort 강제 재시도 - 결과: {forced_cleanup_ok}")
        cleanup_ok = forced_cleanup_ok
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
