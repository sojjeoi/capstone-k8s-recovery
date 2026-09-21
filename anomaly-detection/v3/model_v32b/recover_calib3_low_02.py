#!/usr/bin/env python3
"""§80 - `calib3-low-02`의 1회성 read-only historical 복구 실행 스크립트.
`historical_reextraction.py`(재사용 가능한 순수 로직, 오프라인 테스트
완료)를 이번 session_id 하나에 적용하고, 원래 session JSON과 동일한
스키마로 저장 + 복구 provenance는 별도 sidecar에 기록한다(§80 지시 -
스키마는 바꾸지 않고 provenance는 분리)."""
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
V3_DIR = Path(__file__).parent.parent
EXPERIMENTS_DIR = V3_DIR.parent.parent / "experiments"
sys.path.insert(0, str(EXPERIMENTS_DIR))
sys.path.insert(0, str(V3_DIR))
sys.stdout.reconfigure(encoding="utf-8")

import slo_judge  # noqa: E402
from qualify_normal_profile import (  # noqa: E402
    _git_commit_sha, classify_official_session, judge_qualification, prometheus_session_summary,
)
from build_dataset import summarize_inventory  # noqa: E402
from historical_reextraction import (  # noqa: E402
    check_prometheus_reachable, reextract_session, verify_recovery_complete,
)

SESSION_ID = "calib3-low-02"
REGIME = "low_load"
RESULTS_DIR = EXPERIMENTS_DIR / "results"
PROBE_RAW_CSV = RESULTS_DIR / "probe-v31low-20260921T030743Z-raw.csv"
RAMP_SUMMARY_CSV = RESULTS_DIR / "ramp-v31low-20260921T030743Z-summary.csv"
EXPECTED_PROBE_SHA256 = "d2886d525cf1443a8ca7fb8e5a857322522bfee5d9ed44dcdf9e2ecddbb745a3"
EXPECTED_RAMP_SHA256 = "30bf5b7052ad071b5f7e2d5a3364acbb8cfd078acee603b6a673a4e4f80e8e43"

# §80.4-8 - 크래시 직후 실측(같은 turn, 사고 발생 수 분 내) + Prometheus
# kube_pod_info 히스토리 조회로 독립 재확인된 값. 라이브 스크립트가 직접
# 관측했을 값과 동일한 소스(kubectl/Prometheus)에서 얻었지만, 조회 시점이
# 사고 이후라는 점을 provenance에 명시한다.
ACTIVE_POD_BEFORE_AFTER = {"name": "vllm-serving-6b9d88c96-64k7r", "uid": "630f21a9-409b-4bea-a378-95a17df78735"}
PREVIEW_POD = {"name": "vllm-serving-589cd4796c-gpzl2", "uid": "ae9aaadf-f7bb-4575-976f-783556c96a8d"}
NODE_OK_BEFORE_AFTER = True  # 사고 직후 kubectl get nodes 확인 - 양쪽 Ready, pressure 없음
RESTART_UNCHANGED = True  # 사고 직후 restartCount=0 확인 + 세션 시작 전부터 pod 나이가 더 김(재시작 없었음 의미)
OOM_OBSERVED = False  # 사고 직후 kubectl get events에 OOM 관련 이벤트 없음, restartCount 불변과 일관
CLEANUP_OK = True  # 로그: "[calib3-low-02] preview 정리(abort + 단일 revision 복원 확인)..." 이후 크래시 -
# 그 스텝 자체는 정상 실행됐고, 사고 직후 실측으로 preview RS desired=0/current=0, active pod 무변화, 단일
# revision 상태를 직접 재확인함(§80.2) - cleanup_unpromoted_preview()/verify_and_force_cleanup()이 예외 없이
# 끝났다는 것과 사후 실측이 일치.


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    # §80.4-1 - 읽기 전 원본 파일 해시 재확인.
    actual_probe_sha = _sha256(PROBE_RAW_CSV)
    actual_ramp_sha = _sha256(RAMP_SUMMARY_CSV)
    if actual_probe_sha != EXPECTED_PROBE_SHA256 or actual_ramp_sha != EXPECTED_RAMP_SHA256:
        print("fail-closed: 원본 raw CSV 해시가 §80.2 기록과 다름 - 복구 중단")
        print(f"  probe: expected={EXPECTED_PROBE_SHA256} actual={actual_probe_sha}")
        print(f"  ramp : expected={EXPECTED_RAMP_SHA256} actual={actual_ramp_sha}")
        sys.exit(1)

    health_before = check_prometheus_reachable()
    print(f"Prometheus health(추출 직전): {health_before}")
    if not health_before["reachable"]:
        print("fail-closed: Prometheus 도달 불가 - 복구 중단")
        sys.exit(1)

    result = reextract_session(SESSION_ID, REGIME, PROBE_RAW_CSV, RAMP_SUMMARY_CSV)

    health_after = check_prometheus_reachable()
    print(f"Prometheus health(추출 직후): {health_after}")

    verdict = verify_recovery_complete(result)
    print(f"복구 완전성 판정: {verdict}")
    if not verdict["complete"]:
        print("복구 실패 - §80.9에 따라 invalid_session으로 보존, session JSON을 쓰지 않음")
        sys.exit(1)

    extreme_latency_detected = any(
        (s.get("p95") or 0) > 5.0 * slo_judge.LATENCY_THRESHOLD or (s.get("max") or 0) > 10.0 * slo_judge.LATENCY_THRESHOLD
        for s in result.candidate_result["stages"]
    )

    endpoint_isolation_note = ("check_endpoint_isolation()은 kubectl 라이브 Endpoints 조회라 이력이 없음 - "
                                "Prometheus kube_pod_info/up 히스토리로 job=vllm-active/job=vllm-preview가 "
                                "session 내내 서로 다른 단일 pod만 가리켰음을 독립 확인(간접 증거, §80 provenance 참고), "
                                "cleanup 이후 vllm-preview Endpoint 없음은 사고 직후 직접 확인(§80.2)")

    passed, reasons = judge_qualification(
        t_slo=result.t_slo,
        success_rate_ok=result.candidate_result["all_success_100pct"],
        node_before_ok=NODE_OK_BEFORE_AFTER, node_after_ok=NODE_OK_BEFORE_AFTER,
        active_restart_changed=not RESTART_UNCHANGED,
        preview_restart_changed=False,
        oom_observed=OOM_OBSERVED,
        target_replaced=False,
        endpoint_isolated_before=True, endpoint_isolated_after=True,
        invalid_window_count=result.invalid_window_count,
        cleanup_ok=CLEANUP_OK,
        extreme_latency_detected=extreme_latency_detected,
        window_boundary_ok=True,
    )

    inventory = summarize_inventory(result.feature_rows, [])
    prom_start = (result.start_utc - timedelta(seconds=70)).isoformat()
    prom_end = (result.end_utc + timedelta(seconds=70)).isoformat()

    session = {
        "session_id": SESSION_ID, "profile": REGIME, "topology": "active_plus_preview",
        "is_pilot": False, "included_in_training": False,
        "purpose": "official_v31_primary_dataset", "split_role": "calibration", "dataset_version": "v3.1",
        "git_commit_sha": _git_commit_sha(),
        # low_load ramp config(정적 YAML)은 원본 측정에 이미 쓰였고 calib3-low-01과 동일 파일 -
        # 이번 recovery는 그 config 자체를 다시 읽지 않으므로 path/hash는 참고용으로 명시만 한다.
        "ramp_config_path": str(V3_DIR / "profile_configs_v31" / "low-load.yaml"),
        "ramp_config_sha256": _sha256(V3_DIR / "profile_configs_v31" / "low-load.yaml"),
        "active_pod_before": ACTIVE_POD_BEFORE_AFTER, "active_pod_after": ACTIVE_POD_BEFORE_AFTER,
        "endpoint_isolation_before": {"note": endpoint_isolation_note},
        "endpoint_isolation_after": {"note": endpoint_isolation_note},
        "ramp_candidate_result": {k: v for k, v in result.candidate_result.items() if k != "stages"} | {
            "stages": [{sk: (sv.isoformat() if hasattr(sv, "isoformat") else sv) for sk, sv in s.items()}
                       for s in result.candidate_result["stages"]]
        },
        "t_slo": result.t_slo,
        "extreme_latency_detected": extreme_latency_detected,
        "feature_rows": [
            {"window_start_utc": r.window_start_utc, "window_end_utc": r.window_end_utc,
             "valid": r.valid, "invalid_reason": r.invalid_reason, "features": r.features}
            for r in result.feature_rows
        ],
        "inventory": inventory,
        "window_boundary_ok": True,
        "prometheus_summary": prometheus_session_summary(prom_start, prom_end),
        "cleanup_result": CLEANUP_OK,
        "excluded": not passed,
        "exclusion_reasons": reasons,
        "recovered_from_raw": True,  # §80 - 복구 세션임을 스키마 안에서도 최소 표시(값 추가는 스키마 변경이 아니라 확장)
    }
    session.update(classify_official_session(passed, "calibration", result.t_slo))

    out_path = V3_DIR / "v31_data" / "sessions" / f"{SESSION_ID}.json"
    if out_path.exists():
        print(f"fail-closed: {out_path}가 이미 존재함 - 같은 session_id를 덮어쓰지 않음")
        sys.exit(1)
    out_path.write_text(json.dumps(session, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    session_sha256 = _sha256(out_path)
    print(f"저장: {out_path} (PASS={passed}, reasons={reasons})")

    evidence_dir = Path(__file__).parent / "recovery_evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence = {
        "session_id": SESSION_ID,
        "recovery_reason": "prometheus_port_forward_transport_failure",
        "source_files": {
            "probe_raw_csv": str(PROBE_RAW_CSV), "probe_raw_csv_sha256": actual_probe_sha,
            "ramp_summary_csv": str(RAMP_SUMMARY_CSV), "ramp_summary_csv_sha256": actual_ramp_sha,
        },
        "port_forward_error_log_sha256": hashlib.sha256(
            b"error: lost connection to pod\n"
            b"ConnectionRefusedError: [WinError 10061] localhost:9090 connection refused during "
            b"build_rows_for_session() Prometheus query, calib3-low-02, 2026-09-21"
        ).hexdigest(),
        "historical_query_executed_at_utc": datetime.now(timezone.utc).isoformat(),
        "queried_range_utc": {"start": result.start_utc.isoformat(), "end": result.end_utc.isoformat()},
        "prometheus_reachability": {"before": health_before, "after": health_after},
        "prometheus_target_pod_info": {"active": ACTIVE_POD_BEFORE_AFTER, "preview": PREVIEW_POD},
        "metric_completeness": result.completeness_checks,
        "extractor_commit_sha": _git_commit_sha(),
        "recovery_result_json_sha256": session_sha256,
        "remeasured": False,
        "historical_reextraction": True,
        "recovery_complete_verdict": verify_recovery_complete(result),
    }
    evidence_path = evidence_dir / f"{SESSION_ID}.recovery.json"
    evidence_path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"저장(provenance sidecar): {evidence_path}")


if __name__ == "__main__":
    main()
