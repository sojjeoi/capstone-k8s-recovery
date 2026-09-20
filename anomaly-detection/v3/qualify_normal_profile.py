#!/usr/bin/env python3
"""§63 3-core 정상 부하 profile qualification(2026-09-20) - §59의 0.10 RPS
low_load는 4-core calibration 값이라 v3 학습 profile에서 제외하고
(§62 A-B-A에서 3-core 하에 A1/B/A2 전부 SLO 경계였음), 더 낮은 강도
(low_load=0.025, sustained_load=0.05, burst=0.025 base+0.10 pulse)를
`active_plus_preview` topology에서 qualification(공식 수집 아님)한다.

collect_session.py/aba_diagnostic.py와 같은 원칙 - **새 클러스터 조작
코드를 만들지 않는다**: preview 준비/abort/복원은 `blue_green_prep`,
부하 실행은 `explore_ramp_intensity.run_candidate()`(멀티스테이지 YAML도
그대로 지원 - burst.yaml처럼 stage가 여러 개면 순서대로 실행), feature
window는 `build_dataset.build_rows_for_session()` 그대로 재사용. 이
스크립트가 새로 하는 일은 (1) 순서 오케스트레이션, (2) `slo_judge`를 이용한
진짜 30초 sustained SLO 판정(런타임 `run_candidate()`의 stage별 `violates`는
"이 stage 순간 P95 여부"일 뿐이라 그대로 PASS/FAIL 기준으로 쓰면 안 됨),
(3) Endpoint 격리 확인, (4) Prometheus 사후 조회로 CPU/throttle/Node 요약
기록뿐이다.

§65(2026-09-20) 확장 - 같은 세션 로직을 공식 v3 정상 데이터 9세션 수집에도
그대로 재사용한다(`--official --split-role {train,calibration,holdout}`).
qualification과 다른 점은 `purpose`/`is_pilot`/`included_in_training`
태그와 결과 저장 경로(`official_data/sessions/`)뿐 - PASS 조건·측정
절차·재사용 코드는 완전히 동일하다(중복 스크립트를 만들지 않음)."""
import argparse
import hashlib
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

EXPERIMENTS_DIR = Path(__file__).parent.parent.parent / "experiments"
V3_DIR = Path(__file__).parent
sys.path.insert(0, str(EXPERIMENTS_DIR))
sys.path.insert(0, str(V3_DIR))
sys.stdout.reconfigure(encoding="utf-8")

import slo_judge  # noqa: E402
from active_pod_resolver import get_active_pods  # noqa: E402
from blue_green_prep import cleanup_unpromoted_preview, prepare_preview_with_rollback  # noqa: E402
from explore_ramp_intensity import IMAGE, RESULTS_DIR, bucket_stats, check_node_and_pods  # noqa: E402
from load_ramp_adapter import SETTLE_SEC, _delete_pod, _run, _wait_pod_ready  # noqa: E402
from memory_pressure_adapter import get_pod_details  # noqa: E402

from build_dataset import _git_commit_sha, build_rows_for_session, summarize_inventory  # noqa: E402
from collect_session import run_candidate_with_retry, verify_and_force_cleanup  # noqa: E402
from windows import CandidateSession  # noqa: E402

ROLLOUT_NAME = "vllm-serving"
NAMESPACE = "vllm-serving"
PROM_URL = "http://localhost:9090"  # features.py와 동일 관례 - 읽기 전용 이력 조회, port-forward 필요

# 사용자 지시("최소 60초 settle") - 공식 수집(§59)의 30초보다 길게 고정.
SETTLE_AFTER_PREVIEW_READY_SEC = 60.0
# §60 수준(P95=12.654s)의 재발을 formal 30초-sustained 기준과 별개로도
# 감지하기 위한 여유 배수 - SLO threshold(0.648s)의 5배(P95)/10배(max).
EXTREME_LATENCY_P95_MULT = 5.0
EXTREME_LATENCY_MAX_MULT = 10.0

PROFILE_CONFIGS_DIR = V3_DIR / "profile_configs_3core"
PROFILE_CONFIGS = {
    "low_load": PROFILE_CONFIGS_DIR / "low-load.yaml",
    "sustained_load": PROFILE_CONFIGS_DIR / "sustained-load.yaml",
    "burst": PROFILE_CONFIGS_DIR / "burst.yaml",
}
# 전부 밑줄 없이 RFC 1123 안전, label[:7] 그대로 pod 이름 접두어가 됨(§60의
# 밑줄 버그 재발 방지 - 규제 테스트로 고정).
PROFILE_RUN_LABELS = {"low_load": "q3clow", "sustained_load": "q3csus", "burst": "q3cbrst"}
DEFAULT_PROBE_CONFIG = str(Path(__file__).parent.parent.parent / "chaos" / "probe-config.yaml")
QUALIFICATION_SESSIONS_DIR = V3_DIR / "qualification_data" / "sessions"
OFFICIAL_SESSIONS_DIR = V3_DIR / "official_data" / "sessions"
PURPOSE_QUALIFICATION = "normal_profile_qualification"
PURPOSE_OFFICIAL = "official_v3_collection"
SPLIT_ROLES = ("train", "calibration", "holdout")

# §69(2026-09-20) v3.1 - sustained_load/burst 전부 boundary_challenge_set으로
# 재분류된 뒤, 최종 정상 domain을 idle/low_load 두 regime·600초 통일
# duration으로 재설계. `idle`은 ramp pod을 아예 안 만들고 probe만
# 돌린다(SLO probe 외 별도 부하 없음) - run_candidate_with_retry() 재사용
# 불가(ramp+probe 쌍 전제)라 `run_idle_session()`을 새로 추가했다.
V31_CONFIGS_DIR = V3_DIR / "profile_configs_v31"
V31_RAMP_CONFIGS = {"low_load": V31_CONFIGS_DIR / "low-load.yaml"}  # idle은 ramp config 없음
V31_REGIMES = ("idle", "low_load")
V31_RUN_LABELS = {"idle": "v31idle", "low_load": "v31low"}  # 밑줄 없음, RFC 1123 안전
V31_SESSIONS_DIR = V3_DIR / "v31_data" / "sessions"
PURPOSE_V31 = "official_v31_primary_dataset"
V31_SESSION_DURATION_SEC = 600.0


def _pod_name_for_hash(pod_hash: str):
    r = _run(["kubectl", "get", "pods", "-n", NAMESPACE, "-l", f"rollouts-pod-template-hash={pod_hash}", "-o", "json"])
    if r.returncode != 0:
        return None
    items = json.loads(r.stdout).get("items") or []
    return items[0]["metadata"]["name"] if items else None


def check_endpoint_isolation(active_pod_name, preview_pod_name=None) -> dict:
    """Service의 selector가 아니라 실제 Endpoints 객체를 읽어 라우팅 대상을
    확인한다(§63.3 PASS 조건 - active/preview Endpoint 격리 유지)."""
    def _addrs(svc_name):
        r = _run(["kubectl", "get", "endpoints", svc_name, "-n", NAMESPACE, "-o", "json"])
        if r.returncode != 0:
            return None
        subsets = json.loads(r.stdout).get("subsets") or []
        return sorted(a.get("targetRef", {}).get("name") for s in subsets for a in (s.get("addresses") or []))

    active_eps = _addrs("vllm-active")
    preview_eps = _addrs("vllm-preview")
    expected_preview = sorted([preview_pod_name]) if preview_pod_name else []
    isolated = active_eps == [active_pod_name] and preview_eps == expected_preview
    return {"active_endpoints": active_eps, "preview_endpoints": preview_eps, "isolated": isolated}


def _prom_range(query, start_iso, end_iso, step="15s"):
    r = requests.get(f"{PROM_URL}/api/v1/query_range",
                      params={"query": query, "start": start_iso, "end": end_iso, "step": step}, timeout=15)
    r.raise_for_status()
    return r.json()["data"]["result"]


def prometheus_session_summary(start_iso: str, end_iso: str) -> dict:
    """읽기 전용 이력 조회(§61/§62와 동일 방식) - 세션 PASS/FAIL 판정에는
    쓰지 않고 보고용으로만 기록한다. Prometheus에 연결 못 하면(port-forward
    미기동 등) 세션 자체를 막지 않고 error만 남긴다."""
    def series_stats(query):
        try:
            result = _prom_range(query, start_iso, end_iso)
        except Exception as e:  # noqa: BLE001 - 보고용 best-effort, 세션을 막지 않음
            return {"error": str(e)}
        out = {}
        for r in result:
            pod_or_instance = r["metric"].get("pod") or r["metric"].get("instance") or "?"
            vals = [float(v[1]) for v in r["values"] if v[1] != "NaN"]
            if vals:
                out[pod_or_instance] = {"min": min(vals), "max": max(vals), "avg": sum(vals) / len(vals), "n": len(vals)}
        return out

    return {
        "cpu_cores": series_stats('sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="vllm-serving",container="vllm"}[1m]))'),
        "cfs_throttle_ratio": series_stats(
            'sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{namespace="vllm-serving",container="vllm"}[1m])) '
            '/ sum by (pod) (rate(container_cpu_cfs_periods_total{namespace="vllm-serving",container="vllm"}[1m]))'),
        "working_set_bytes": series_stats('sum by (pod) (container_memory_working_set_bytes{namespace="vllm-serving",container="vllm"})'),
        "node_cpu_utilization": series_stats('1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[1m]))'),
        "node_load1": series_stats('node_load1'),
        "node_iowait_ratio": series_stats('avg by (instance) (rate(node_cpu_seconds_total{mode="iowait"}[1m]))'),
        "node_mem_available_bytes": series_stats('node_memory_MemAvailable_bytes'),
        "node_psi_cpu_some_seconds": series_stats('node_pressure_cpu_waiting_seconds_total'),
    }


def judge_qualification(*, t_slo, success_rate_ok, node_before_ok, node_after_ok,
                         active_restart_changed, preview_restart_changed,
                         oom_observed, target_replaced, endpoint_isolated_before,
                         endpoint_isolated_after, invalid_window_count, cleanup_ok,
                         extreme_latency_detected, window_boundary_ok=True) -> tuple:
    """§63.3/§65.3 PASS 조건 + 중단 규칙을 기계적으로 적용하는 순수 함수
    (judge_session_exclusion과 동일한 스타일 - 오프라인 테스트 대상).
    `window_boundary_ok`는 §65.3 "feature timestamp와 session 경계 정합"
    조건 - `build_dataset.iter_window_starts()`가 애초에 세션 밖으로 새는
    창을 만들지 않아 항상 참이어야 하지만, 회귀 방지를 위해 실제로
    확인한 결과를 그대로 받는다(기본값 True는 이 검사를 하지 않는 기존
    qualification 호출부와의 하위호환용)."""
    reasons = []
    if t_slo is not None:
        reasons.append(f"sustained SLO 위반(t_slo={t_slo})")
    if not success_rate_ok:
        reasons.append("요청 성공률 100% 아님")
    if not node_before_ok or not node_after_ok:
        reasons.append("Node 상태 이상(측정 전 또는 후)")
    if active_restart_changed:
        reasons.append("active restartCount 변화")
    if preview_restart_changed:
        reasons.append("preview restartCount 변화")
    if oom_observed:
        reasons.append("OOMKilled 관측")
    if target_replaced:
        reasons.append("예기치 않은 target 교체/promotion 감지")
    if not endpoint_isolated_before or not endpoint_isolated_after:
        reasons.append("active/preview Endpoint 격리 실패")
    if invalid_window_count > 0:
        reasons.append(f"metric 결측/무효 window {invalid_window_count}개")
    if cleanup_ok is False:
        reasons.append("cleanup 또는 단일 revision 복원 실패")
    if extreme_latency_detected:
        reasons.append("§60 수준의 비정상적인 다초 단위 latency 재발 의심")
    if not window_boundary_ok:
        reasons.append("feature window가 session 경계를 벗어남")
    return (len(reasons) == 0, reasons)


def _iso(dt):
    return dt.isoformat() if hasattr(dt, "isoformat") else dt


def classify_official_session(passed: bool, split_role: str, t_slo) -> dict:
    """§67.2 - official 세션의 3개 split 소속 플래그와 `classification`을
    한 곳에서 계산하는 순수 함수(오프라인 테스트 대상). 진짜 sustained SLO
    위반으로 FAIL한 세션(`official-train-sustained_load-20260920` 등)은
    `included_in_{training,calibration,holdout}` 전부 False,
    `classification="unexpected_slo_violation"`으로 영구 분류되며, 어떤
    split의 정상 데이터 수를 채우는 대체 session으로도 계산되지 않는다.
    (t_slo가 아닌 다른 사유로 FAIL하면 - restart/OOM/Node/harness 오류 등 -
    `invalid_session`으로 분류해 §65.4의 기존 "기술적 오류는 새 ID로
    재실행 가능" 경로와 구분한다.)"""
    if passed:
        return {
            "included_in_training": split_role == "train",
            "included_in_calibration": split_role == "calibration",
            "included_in_holdout": split_role == "holdout",
            "classification": "normal_valid",
        }
    return {
        "included_in_training": False,
        "included_in_calibration": False,
        "included_in_holdout": False,
        "classification": "unexpected_slo_violation" if t_slo is not None else "invalid_session",
    }


def run_idle_session(probe_config_path: str, label: str, duration_sec: float = V31_SESSION_DURATION_SEC) -> dict:
    """§69 idle regime - ramp pod을 아예 만들지 않고 probe만 duration_sec
    동안 실행한다(SLO probe 외 별도 부하 없음). `run_candidate()`와 호환되는
    결과 구조(전체 구간을 표현하는 단일 합성 stage)를 반환해 나머지
    파이프라인(slo_judge/build_dataset)을 그대로 재사용한다."""
    run_id = f"{label}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    probe_pod = f"{label[:7]}-probe-{uuid.uuid4().hex[:6]}"
    probe_config_name = Path(probe_config_path).name
    local_raw = RESULTS_DIR / f"probe-{run_id}-raw.csv"
    pod_sleep_sec = int(duration_sec) + 600

    try:
        _run(["kubectl", "run", probe_pod, "-n", NAMESPACE, f"--image={IMAGE}",
              "--image-pull-policy=Never", "--restart=Never", "--", "sleep", str(pod_sleep_sec)], check=True)
        if not _wait_pod_ready(probe_pod):
            raise RuntimeError(f"{probe_pod} Ready 시간초과")
        _run(["kubectl", "cp", str(Path(probe_config_path)), f"{NAMESPACE}/{probe_pod}:/{probe_config_name}"], check=True)
        print(f"안정화 대기 {SETTLE_SEC}초...")
        time.sleep(SETTLE_SEC)

        stage_start_utc = datetime.now(timezone.utc)
        probe_inner = (f"PYTHONUNBUFFERED=1 python /probe.py --config /{probe_config_name} "
                       f"--run-id {run_id} --scenario {label} --arm native --rep 1 "
                       f"--out /probe-raw.csv --duration-sec {duration_sec} "
                       f"> /probe.log 2>&1; echo $? > /probe.exit")
        _run(["kubectl", "exec", "-n", NAMESPACE, probe_pod, "--", "sh", "-c",
              f"nohup sh -c '{probe_inner}' < /dev/null > /probe-wrapper.log 2>&1 &"], check=True)

        print(f"probe {duration_sec:.0f}초 동안 단독 실행 중(ramp 없음 - idle regime)...")
        time.sleep(duration_sec + 20)
        stage_end_utc = datetime.now(timezone.utc)

        r = _run(["kubectl", "exec", "-n", NAMESPACE, probe_pod, "--", "cat", "/probe-raw.csv"], check=True)
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        local_raw.write_text(r.stdout, encoding="utf-8")
    finally:
        _delete_pod(probe_pod)

    rows = slo_judge.load_raw(local_raw)
    stats = bucket_stats(rows)
    stage = {"stage": f"{label}-idle", "scenario": "idle", "method": "native", "repetition": "1",
              "target_rps": "0(no ramp - probe only)", "actual_rps": None,
              "stage_start_utc": stage_start_utc, "stage_end_utc": stage_end_utc, **stats}
    empty_bucket = {"n": 0, "success_rate": None, "mean": None, "p95": None, "max": None, "violates": None}
    return {
        "run_id": run_id, "valid": True, "reason": None, "local_raw": str(local_raw),
        "baseline": empty_bucket, "stages": [stage], "drain": empty_bucket,
        "all_success_100pct": bool(stats["n"] and stats["success_rate"] == 1.0),
        "run_candidate_attempts": 1,
    }


def collect_qualification_session(profile: str, session_id: str, *,
                                   official: bool = False, split_role: str = None,
                                   dataset_version: str = "v3") -> dict:
    is_v31 = dataset_version == "v3.1"
    if is_v31:
        if profile not in V31_REGIMES:
            raise ValueError(f"v3.1 regime이 아님: {profile}(허용: {V31_REGIMES})")
        if split_role not in SPLIT_ROLES:
            raise ValueError(f"v3.1 세션은 split_role이 {SPLIT_ROLES} 중 하나여야 함: {split_role!r}")
        ramp_config = str(V31_RAMP_CONFIGS[profile]) if profile != "idle" else None
        ramp_config_sha256 = hashlib.sha256(Path(ramp_config).read_bytes()).hexdigest() if ramp_config else None
        run_label = V31_RUN_LABELS[profile]
    else:
        if profile not in PROFILE_CONFIGS:
            raise ValueError(f"알 수 없는 profile: {profile}(허용: {list(PROFILE_CONFIGS)})")
        if official and split_role not in SPLIT_ROLES:
            raise ValueError(f"official 세션은 split_role이 {SPLIT_ROLES} 중 하나여야 함: {split_role!r}")
        ramp_config = str(PROFILE_CONFIGS[profile])
        ramp_config_sha256 = hashlib.sha256(Path(ramp_config).read_bytes()).hexdigest()
        run_label = PROFILE_RUN_LABELS[profile]

    session = {
        "session_id": session_id, "profile": profile, "topology": "active_plus_preview",
        "is_pilot": False if is_v31 else (not official),
        # official/v3.1 세션은 PASS해야만(judge_qualification 결과) True로 확정한다(아래에서 갱신) -
        # qualification은 §63 지시대로 항상 False로 고정.
        "included_in_training": False,
        "purpose": PURPOSE_V31 if is_v31 else (PURPOSE_OFFICIAL if official else PURPOSE_QUALIFICATION),
        "split_role": split_role if (is_v31 or official) else None,  # §65.1/§69 - 측정 전 고정, 결과 보고 안 바꿈
        "dataset_version": "v3.1" if is_v31 else "v3",
        "git_commit_sha": _git_commit_sha(),
        "ramp_config_path": ramp_config, "ramp_config_sha256": ramp_config_sha256,
        "t_session_start": datetime.now(timezone.utc).isoformat(),
    }

    pods_before = get_active_pods()
    if len(pods_before) != 1:
        raise RuntimeError(f"세션 시작 전 active pod이 1개가 아님({len(pods_before)}개) - fail-closed")
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
        session["exclusion_reasons"] = ["preview 준비 실패(timeout) - 자체 rollback으로 이미 복원됨 - 중단 규칙(§63.5)"]
        session["t_session_end"] = datetime.now(timezone.utc).isoformat()
        print(f"[{session_id}] preview 준비 실패 - 중단, 이후 profile 진행하지 않음")
        return session

    preview_pod_name = _pod_name_for_hash(prep_info["created_pod_hash"])
    details_before_preview = (get_pod_details(preview_pod_name) or {}) if preview_pod_name else {}
    print(f"[{session_id}] preview Ready(pod={preview_pod_name}) - {SETTLE_AFTER_PREVIEW_READY_SEC:.0f}초 settle")
    time.sleep(SETTLE_AFTER_PREVIEW_READY_SEC)

    endpoint_isolation_before = check_endpoint_isolation(active_before["name"], preview_pod_name)
    session["endpoint_isolation_before"] = endpoint_isolation_before

    try:
        if is_v31 and profile == "idle":
            print(f"[{session_id}] idle regime 실행(ramp 없음, probe {V31_SESSION_DURATION_SEC:.0f}초 단독)...")
            candidate_result = run_idle_session(DEFAULT_PROBE_CONFIG, run_label)
        else:
            print(f"[{session_id}] {profile} 부하 실행(run_candidate)...")
            candidate_result = run_candidate_with_retry(ramp_config, DEFAULT_PROBE_CONFIG, label=run_label)
    finally:
        print(f"[{session_id}] preview 정리(abort + 단일 revision 복원 확인)...")
        cleanup_ok = cleanup_unpromoted_preview(prep_info, ROLLOUT_NAME, NAMESPACE)
        cleanup_ok = verify_and_force_cleanup(prep_info, cleanup_ok)
        session["cleanup_result"] = cleanup_ok

    session["t_session_end"] = datetime.now(timezone.utc).isoformat()
    session["ramp_candidate_result"] = {k: v for k, v in candidate_result.items() if k != "stages"}
    stages = [{sk: _iso(sv) for sk, sv in s.items()} for s in (candidate_result.get("stages") or [])]
    session["ramp_candidate_result"]["stages"] = stages

    pods_after = get_active_pods()
    target_replaced = len(pods_after) != 1 or pods_after[0]["uid"] != active_before["uid"]
    active_after_name = pods_after[0]["name"] if len(pods_after) == 1 else None
    session["active_pod_after"] = ({"name": pods_after[0]["name"], "uid": pods_after[0]["uid"]}
                                    if len(pods_after) == 1 else None)
    node_after = check_node_and_pods(vllm_pod=active_after_name)
    details_after = (get_pod_details(active_after_name) or {}) if active_after_name else {}
    endpoint_isolation_after = check_endpoint_isolation(active_after_name, None)  # cleanup 이후엔 preview가 없어야 정상
    session["endpoint_isolation_after"] = endpoint_isolation_after

    # §63.3 - 진짜 30초 sustained SLO(런타임 run_candidate()의 stage.violates는
    # "이 stage 구간 순간 P95>threshold"일 뿐이라 그대로 PASS/FAIL 기준으로
    # 안 쓴다 - slo_judge.find_t_slo()로 probe raw 전체(baseline+load+drain)에
    # 걸쳐 진짜 30초 연속 위반/즉시 availability 위반만 판정한다.
    t_slo = None
    if candidate_result.get("valid") and candidate_result.get("local_raw"):
        raw_rows = slo_judge.load_raw(candidate_result["local_raw"])
        points = slo_judge.evaluate(raw_rows)
        t_slo_dt = slo_judge.find_t_slo(points)
        t_slo = t_slo_dt.isoformat() if t_slo_dt else None
    session["t_slo"] = t_slo

    extreme_latency_detected = any(
        (s.get("p95") or 0) > EXTREME_LATENCY_P95_MULT * slo_judge.LATENCY_THRESHOLD
        or (s.get("max") or 0) > EXTREME_LATENCY_MAX_MULT * slo_judge.LATENCY_THRESHOLD
        for s in (candidate_result.get("stages") or [])
    )
    session["extreme_latency_detected"] = extreme_latency_detected

    if candidate_result.get("valid") and candidate_result.get("stages"):
        cand = CandidateSession(
            session_id=session_id, regime=profile, topology="active_plus_preview",
            start_utc=candidate_result["stages"][0]["stage_start_utc"],
            end_utc=candidate_result["stages"][-1]["stage_end_utc"],
            source_run_id=candidate_result["run_id"],
        )
        rows_feat = build_rows_for_session(cand)
        session["feature_rows"] = [
            {"window_start_utc": r.window_start_utc, "window_end_utc": r.window_end_utc,
             "valid": r.valid, "invalid_reason": r.invalid_reason, "features": r.features}
            for r in rows_feat
        ]
        inventory = summarize_inventory(rows_feat, [cand])
        session["inventory"] = inventory
        invalid_window_count = inventory["invalid_rows"]
        # §65.3 "feature timestamp와 session 경계 정합" - iter_window_starts()가
        # 애초에 세션 밖으로 새는 창을 안 만들지만(build_dataset.py 구조상
        # 보장됨), 회귀 방지로 실제 값을 직접 확인한다.
        window_boundary_ok = all(
            cand.start_utc <= datetime.fromisoformat(r.window_start_utc)
            and datetime.fromisoformat(r.window_end_utc) <= cand.end_utc
            for r in rows_feat
        )
        session["window_boundary_ok"] = window_boundary_ok
        stats = inventory.get("feature_stats") or {}
        session["observed_constant_zero"] = {
            "queue": (stats.get("queue_mean", {}).get("max") == 0) if inventory["valid_rows"] else None,
            "cache": (stats.get("cache_mean", {}).get("max") == 0) if inventory["valid_rows"] else None,
        }

        # candidate_result["stages"]는 run_candidate()가 만든 원본(datetime
        # 객체 그대로) - session["ramp_candidate_result"]["stages"]에 넣은
        # _iso() 직렬화 버전과 다르다. 여기선 원본을 그대로 산술에 쓴다.
        prom_start = (candidate_result["stages"][0]["stage_start_utc"] - timedelta(seconds=70)).isoformat()
        prom_end = (candidate_result["stages"][-1]["stage_end_utc"] + timedelta(seconds=70)).isoformat()
        session["prometheus_summary"] = prometheus_session_summary(prom_start, prom_end)
    else:
        session["feature_rows"] = []
        session["inventory"] = None
        session["observed_constant_zero"] = None
        session["prometheus_summary"] = None
        session["window_boundary_ok"] = True  # 창 자체가 없으므로 위반도 없음
        invalid_window_count = 0

    passed, reasons = judge_qualification(
        t_slo=t_slo,
        success_rate_ok=candidate_result.get("all_success_100pct", False),
        node_before_ok=node_before.get("node_ok", False), node_after_ok=node_after.get("node_ok", False),
        active_restart_changed=(node_before.get("restart_count") != node_after.get("restart_count")),
        preview_restart_changed=False,  # preview는 세션 끝에 이미 정리됨 - restartCount 비교 대상 없음(별도 관찰 없음, 정직하게 미평가)
        oom_observed=bool(details_before.get("oom_killed") or details_after.get("oom_killed")
                           or details_before_preview.get("oom_killed")),
        target_replaced=target_replaced,
        endpoint_isolated_before=endpoint_isolation_before["isolated"],
        endpoint_isolated_after=endpoint_isolation_after["isolated"],
        invalid_window_count=invalid_window_count,
        cleanup_ok=cleanup_ok,
        extreme_latency_detected=extreme_latency_detected,
        window_boundary_ok=session.get("window_boundary_ok", True),
    )
    session["excluded"] = not passed
    session["exclusion_reasons"] = reasons
    # official/v3.1 세션만 split 소속 판정을 받는다(§67.2/§69 - qualification은
    # included_in_training이 항상 False로 고정돼 있었음, 위에서 이미 세팅).
    if official or is_v31:
        session.update(classify_official_session(passed, split_role, t_slo))

    print(f"[{session_id}] 완료 - {'PASS' if passed else 'FAIL'}" + (f" - 사유: {reasons}" if reasons else ""))
    return session


def main():
    parser = argparse.ArgumentParser(description="§63/§65/§69 정상 부하 profile qualification·공식 수집·v3.1 수집 - 세션 1개 실행")
    parser.add_argument("--profile", required=True, choices=sorted(set(PROFILE_CONFIGS) | set(V31_REGIMES)))
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--official", action="store_true", help="공식 v3 수집 세션(§65) - qualification이 아님")
    parser.add_argument("--v31", action="store_true", help="v3.1 primary dataset 세션(§69) - idle/low_load 전용")
    parser.add_argument("--split-role", choices=list(SPLIT_ROLES), help="--official/--v31일 때 필수 - 측정 전 고정된 역할")
    args = parser.parse_args()
    if args.official and args.v31:
        parser.error("--official과 --v31은 동시에 줄 수 없음")
    if (args.official or args.v31) and not args.split_role:
        parser.error("--official/--v31에는 --split-role이 필요함")
    if args.v31 and args.profile not in V31_REGIMES:
        parser.error(f"--v31의 --profile은 {V31_REGIMES} 중 하나여야 함")
    if not args.v31 and args.profile not in PROFILE_CONFIGS:
        parser.error(f"--v31 없이는 --profile이 {list(PROFILE_CONFIGS)} 중 하나여야 함")

    session = collect_qualification_session(args.profile, args.session_id,
                                             official=args.official, split_role=args.split_role,
                                             dataset_version="v3.1" if args.v31 else "v3")

    if args.v31:
        sessions_dir = V31_SESSIONS_DIR
    elif args.official:
        sessions_dir = OFFICIAL_SESSIONS_DIR
    else:
        sessions_dir = QUALIFICATION_SESSIONS_DIR
    sessions_dir.mkdir(parents=True, exist_ok=True)
    out_path = sessions_dir / f"{args.session_id}.json"
    out_path.write_text(json.dumps(session, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"[{args.session_id}] 결과 저장: {out_path}")
    sys.exit(1 if session.get("excluded") else 0)


if __name__ == "__main__":
    main()
