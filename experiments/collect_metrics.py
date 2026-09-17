#!/usr/bin/env python3
"""Phase 8 trial 결과 JSON을 모아 run-level comparison.csv를 만든다
(experiment-contract.md §5 스키마 검증 포함, 8단계 최소 버전). 파일럿
(is_pilot)·PREFLIGHT-EXCLUDED·invalid_run은 삭제하지 않고 exclusion_reason과
함께 결과에 남긴다 - "왜 뺐는지"를 추적 가능해야 하고, 제외 기준 자체도
사후 편향 없이 재현 가능해야 한다(guideline.md 9-6절과 같은 이유).

t_SLO가 없는 것과 "측정을 못 한 것"은 다르다: outcome=prevented는 원래
t_SLO가 없는 게 정상(계약서 §3 3조건)이고, 그 외 outcome에서 t_SLO가
비어있으면 timing anomaly로 따로 표시한다. t_detection과 t_SLO의 선후
관계는 고정돼 있지 않다(예측 경로가 성공하면 detection이 SLO 위반보다
먼저 온다 - "prevented"의 핵심 전제) - 그래서 이 관계는 순서 위반으로
플래그하지 않고 detection_lead_sec라는 부호 있는 값으로만 남긴다. 반면
t_detection -> t_decision -> t_api_request -> t_switch, t_slo -> t_recovery는
항상 고정된 인과 순서라 위반 시 timing anomaly로 남긴다.

`t_action`이라는 별도 필드는 없다 - t_switch(실제 selector 전환 완료
시각)를 그 자리에 쓴다.

원본 JSON은 절대 수정하지 않는다(읽기 전용) - trial 결과는 실험의 1차
증거라 사후 수정 흔적이 남으면 안 된다.

outcome=prevented는 그 자체로 신뢰하지 않는다(2026-09-17 pod_kill 오판정
사건 이후 추가) - native arm은 계약서 §3상 애초에 prevented가 나올 수 없고,
본 실험(is_pilot=False)의 prevented는 slo_evaluable_at_exit=True(probe가
실제로 판정 가능한 데이터를 확보했다는 run_once.py의 확인)가 아니면
검증 오류로 취급한다.

network_degrade의 target_replaced(2026-09-18 추가)도 같은 이유로 outcome만
보면 안 된다 - readiness_probe_profile과 묶어 restart_chain_observed
(default profile)/probe_isolation_held(network_tolerant profile) 두 분석
필드로 해석해 CSV에 같이 남긴다. tolerant profile에서 대상이 교체됐는데
outcome=prevented로만 남으면 "설정이 열화를 견뎠다"로 오해할 위험이 있어
_check_tolerant_profile_prevented_misleading()으로 별도 issue도 남긴다 -
어느 쪽도 outcome 자체를 바꾸지는 않는다(SLO 판정과 별개의 분석 필드).
"""
import argparse
import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

RESULTS_DIR = Path(__file__).parent / "results"
EXPECTED_REPS = 5  # experiment-contract.md: 시나리오별 arm당 5회 반복

REQUIRED_STR_FIELDS = ["run_id", "scenario", "arm", "t_run_start", "state"]
REQUIRED_INT_FIELDS = ["rep", "sequence_index", "order_seed"]
REQUIRED_BOOL_FIELDS = ["is_pilot", "detected", "injection_valid", "probe_valid"]
OPTIONAL_TS_FIELDS = [
    "t_injection", "t_injection_request", "t_injection_last_seen", "t_injection_observed",
    "t_injection_end", "t_detection", "t_decision", "t_api_request",
    "t_switch", "t_slo", "t_recovery", "t_audit_write", "t_audit_push", "t_run_end",
]
# t_slo는 의도적으로 제외 - t_detection과의 선후관계가 arm/outcome에 따라
# 뒤집히는 게 정상이라(prevented) 고정 순서 검증 대상이 아니다.
CAUSAL_CHAIN = ["t_injection", "t_detection", "t_decision", "t_api_request", "t_switch"]
VALID_OUTCOMES = {"prevented", "recovered", "timeout", "invalid_run", None}


@dataclass
class ValidationIssue:
    run_id: str
    field: str
    problem: str


def _parse_ts(value, field_name: str, run_id: str, issues: list) -> Optional[datetime]:
    """None은 그대로 None - 임의값으로 채우지 않는다. 문자열인데 파싱 실패하면
    이상 기록하고 None 취급(집계에서는 "없음"과 동일하게 다루되, issue로 남겨서
    원본 데이터 문제를 놓치지 않는다)."""
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        issues.append(ValidationIssue(run_id, field_name, f"ISO8601 파싱 실패: {value!r}"))
        return None


def _validate_schema(row: dict, issues: list) -> None:
    run_id = row.get("run_id", "?")
    for f in REQUIRED_STR_FIELDS:
        if not isinstance(row.get(f), str):
            issues.append(ValidationIssue(run_id, f, f"필수 문자열 필드 누락/타입오류: {row.get(f)!r}"))
    for f in REQUIRED_INT_FIELDS:
        if not isinstance(row.get(f), int) or isinstance(row.get(f), bool):
            issues.append(ValidationIssue(run_id, f, f"필수 정수 필드 누락/타입오류: {row.get(f)!r}"))
    for f in REQUIRED_BOOL_FIELDS:
        if not isinstance(row.get(f), bool):
            issues.append(ValidationIssue(run_id, f, f"필수 불리언 필드 누락/타입오류: {row.get(f)!r}"))
    if row.get("outcome") not in VALID_OUTCOMES:
        issues.append(ValidationIssue(run_id, "outcome", f"알 수 없는 값: {row.get('outcome')!r}"))


def _classify_exclusion(row: dict) -> Optional[str]:
    """PREFLIGHT-EXCLUDED > is_pilot > invalid_run 순으로 하나만 기록한다 -
    여러 사유가 겹쳐도 가장 구체적인/의도적인 사유를 우선한다."""
    notes = row.get("notes") or ""
    if notes.startswith("PREFLIGHT-EXCLUDED"):
        return "preflight_excluded"
    if row.get("is_pilot"):
        return "pilot"
    if row.get("outcome") == "invalid_run":
        return "invalid_run"
    return None


def _check_timing(row: dict, ts: dict, issues: list) -> bool:
    """고정 인과순서(CAUSAL_CHAIN, t_slo->t_recovery)만 위반으로 본다.
    outcome=prevented가 아닌데 t_slo가 없으면 측정 누락 의심으로 별도 표시.
    반환값은 "이 row에 timing anomaly가 있었는지"."""
    run_id = row.get("run_id", "?")
    anomaly = False

    present = [(name, ts[name]) for name in CAUSAL_CHAIN if ts.get(name) is not None]
    for (name_a, t_a), (name_b, t_b) in zip(present, present[1:]):
        if t_a > t_b:
            issues.append(ValidationIssue(
                run_id, f"{name_a}/{name_b}",
                f"순서 위반: {name_a}={t_a.isoformat()} > {name_b}={t_b.isoformat()}"))
            anomaly = True

    if ts.get("t_slo") is not None and ts.get("t_recovery") is not None and ts["t_slo"] > ts["t_recovery"]:
        issues.append(ValidationIssue(run_id, "t_slo/t_recovery", "순서 위반: t_slo > t_recovery"))
        anomaly = True

    # invalid_run은 t_slo가 찍히기 전(PREPARING/INJECTING 단계)에 조기 중단될
    # 수 있어 prevented와 마찬가지로 t_slo 없음이 정상이다 - "recovered"/
    # "timeout"만 t_slo가 반드시 있어야 한다(run_once()가 그 두 outcome을
    # t_slo 확정 이후에만 부여하므로).
    if ts.get("t_slo") is None and row.get("outcome") in ("recovered", "timeout"):
        issues.append(ValidationIssue(
            run_id, "t_slo", f"outcome={row.get('outcome')}인데 t_slo 없음(측정 누락 의심)"))
        anomaly = True

    return anomaly


def _check_prevented_validity(row: dict, issues: list) -> None:
    """계약서 §3: `prevented`는 조건부로만 인정된다(2026-09-17 pod_kill
    오판정 사건 재발 방지 - run_once.py의 NOT_EVALUABLE 게이트 추가와 짝).
    (1) native arm은 개입이 없어 원천적으로 prevented가 나올 수 없다
    (계약서 §3 마지막 줄) - pilot 여부와 무관하게 항상 이상. (2) 본 실험
    (is_pilot=False)의 prevented는 slo_evaluable_at_exit=True가 아니면
    "probe가 실제로 판정 가능한 데이터를 확보했는지"를 검증 못 한 것이므로
    검증 오류로 처리한다 - 파일럿은 옛 하니스로 실행됐을 수 있어(hook
    미구현=None) 제외."""
    if row.get("outcome") != "prevented":
        return
    run_id = row.get("run_id", "?")
    if row.get("arm") == "native":
        issues.append(ValidationIssue(
            run_id, "outcome",
            "arm=native인데 outcome=prevented - 계약서 §3상 native는 prevented가 나올 수 없음"))
    if not row.get("is_pilot") and row.get("slo_evaluable_at_exit") is not True:
        issues.append(ValidationIssue(
            run_id, "slo_evaluable_at_exit",
            f"outcome=prevented인데 slo_evaluable_at_exit={row.get('slo_evaluable_at_exit')!r}"
            f"(True 아님) - probe가 실제로 판정 가능한 데이터를 확보했는지 검증 안 됨"))


def _check_injection_timestamps_consistency(row: dict, ts: dict, issues: list) -> None:
    """t_injection_request <= t_injection_last_seen <= t_injection_observed
    순서가 깨지면(2026-09-18 타임스탬프 재설계 - v2 스키마에만 해당) issue로
    남긴다. 세 필드 다 없는 v1 결과는 조용히 통과(계산할 게 없음)."""
    run_id = row.get("run_id", "?")
    req, last_seen, obs = (ts.get("t_injection_request"), ts.get("t_injection_last_seen"),
                            ts.get("t_injection_observed"))
    if req is not None and obs is not None and req > obs:
        issues.append(ValidationIssue(
            run_id, "t_injection_request/t_injection_observed",
            f"순서 위반: t_injection_request={req.isoformat()} > t_injection_observed={obs.isoformat()}"))
    if last_seen is not None:
        if req is not None and last_seen < req:
            issues.append(ValidationIssue(
                run_id, "t_injection_request/t_injection_last_seen",
                f"순서 위반: t_injection_last_seen={last_seen.isoformat()} < t_injection_request={req.isoformat()}"))
        if obs is not None and last_seen > obs:
            issues.append(ValidationIssue(
                run_id, "t_injection_last_seen/t_injection_observed",
                f"순서 위반: t_injection_last_seen={last_seen.isoformat()} > t_injection_observed={obs.isoformat()}"))


def _compute_profile_interpretation(row: dict) -> tuple:
    """network_degrade의 readiness_probe_profile + target_replaced 조합을
    해석한다(2026-09-18 추가 - 리뷰: tolerant profile에서 파드가 교체됐는데
    SLO 위반이 안 잡혀 outcome=prevented만 남으면 "설정이 열화를 견뎠다"로
    오해할 수 있다는 지적). outcome은 절대 바꾸지 않는다 - SLO 판정과
    별개의 분석 필드다.
    - default profile: target_replaced 그대로가 restart_chain_observed(연쇄
      장애 자체가 관찰됐는지).
    - network_tolerant profile: target_replaced의 반대가 probe_isolation_held
      (그 설정이 열화로부터 probe를 실제로 격리했는지).
    - profile이 둘 중 하나가 아니면(다른 시나리오, 미적용) 둘 다 None.
    restart_chain_observed=True는 이 통제된 실험(네트워크 열화 주입과 같은
    trial 안에서의 시간적 연관) 안에서의 관찰을 뜻할 뿐이다 - 단일 실행
    하나로 인과관계를 확정한다는 뜻이 아니다(반복·통계적 근거는 여러 rep을
    모은 뒤 별도로 봐야 한다)."""
    profile = row.get("readiness_probe_profile")
    replaced = bool(row.get("target_replaced"))
    restart_chain_observed = replaced if profile == "default" else None
    probe_isolation_held = (not replaced) if profile == "network_tolerant" else None
    return restart_chain_observed, probe_isolation_held


def _check_tolerant_profile_prevented_misleading(row: dict, issues: list) -> None:
    """network_tolerant profile에서 대상이 교체됐는데(probe_isolation_held=
    False) outcome=prevented로만 남으면, "위반이 안 잡혔다"만 보고 그 설정이
    열화를 견뎠다고 오해할 위험이 있다(2026-09-18 추가, 리뷰 지적) -
    _check_prevented_validity와 같은 이유로 별도 issue를 남긴다(단독 CSV
    컬럼만으로는 놓치기 쉬움)."""
    if row.get("readiness_probe_profile") != "network_tolerant":
        return
    if row.get("target_replaced") and row.get("outcome") == "prevented":
        issues.append(ValidationIssue(
            row.get("run_id", "?"), "outcome",
            "network_tolerant profile에서 대상 교체(target_replaced=true)가 있었는데 "
            "outcome=prevented - probe_isolation_held=false를 함께 보지 않으면 "
            "설정이 열화를 견딘 것으로 오해할 수 있음"))


def _compute_temporal_relation(ts: dict) -> str:
    """t_slo가 실제 주입 구간에 비해 언제 일어났다고 볼 수 있는지 분류한다
    (2026-09-18 추가 - t_slo가 이제 observed_at 기준이라도, 주입 구간 자체가
    폭을 가지므로 "주입 전/중/후"를 명확히 나누는 게 좋다). 하한은
    t_injection_last_seen이 있으면 그 값, 없으면 t_injection_request -
    t_injection_observed는 상한. 필요한 시각이 하나라도 없으면 unknown."""
    t_slo = ts.get("t_slo")
    upper = ts.get("t_injection_observed")
    lower = ts.get("t_injection_last_seen") or ts.get("t_injection_request")
    if t_slo is None or upper is None or lower is None:
        return "unknown"
    if t_slo < lower:
        return "pre_injection"
    if t_slo < upper:
        return "temporally_ambiguous"
    return "post_injection"


def _seconds_between(ts: dict, start: str, end: str) -> Optional[float]:
    if ts.get(start) is None or ts.get(end) is None:
        return None
    return (ts[end] - ts[start]).total_seconds()


def load_all_results(results_dir: Path) -> list:
    """results/*.json(본 실험) + results/pilot/*.json(파일럿)을 전부 읽는다.
    원본 파일은 절대 안 건드린다(읽기 전용)."""
    rows = []
    for path in sorted(results_dir.glob("trial-*.json")):
        rows.append(json.loads(path.read_text(encoding="utf-8")))
    pilot_dir = results_dir / "pilot"
    if pilot_dir.exists():
        for path in sorted(pilot_dir.glob("trial-*.json")):
            rows.append(json.loads(path.read_text(encoding="utf-8")))
    return rows


def build_comparison(rows: list) -> tuple:
    issues: list = []
    out_rows = []
    seen_keys: dict = {}  # (scenario, arm, rep) -> [run_id, ...] - 제외 안 된 것만 대상

    for row in rows:
        _validate_schema(row, issues)
        ts = {f: _parse_ts(row.get(f), f, row.get("run_id", "?"), issues) for f in OPTIONAL_TS_FIELDS}

        exclusion_reason = _classify_exclusion(row)
        included = exclusion_reason is None
        timing_anomaly = _check_timing(row, ts, issues)
        _check_prevented_validity(row, issues)
        _check_injection_timestamps_consistency(row, ts, issues)
        _check_tolerant_profile_prevented_misleading(row, issues)
        temporal_relation = _compute_temporal_relation(ts)
        restart_chain_observed, probe_isolation_held = _compute_profile_interpretation(row)

        if included:
            key = (row.get("scenario"), row.get("arm"), row.get("rep"))
            seen_keys.setdefault(key, []).append(row.get("run_id"))

        out_rows.append({
            "run_id": row.get("run_id"),
            "scenario": row.get("scenario"),
            "arm": row.get("arm"),
            "rep": row.get("rep"),
            "sequence_index": row.get("sequence_index"),
            "order_seed": row.get("order_seed"),
            "outcome": row.get("outcome"),
            "slo_evaluable_at_exit": row.get("slo_evaluable_at_exit"),
            "min_observation_sec": row.get("min_observation_sec"),
            "readiness_probe_profile": row.get("readiness_probe_profile"),
            "readiness_probe_timeout_sec": row.get("readiness_probe_timeout_sec"),
            "target_replaced": row.get("target_replaced"),
            "t_target_replaced": row.get("t_target_replaced"),
            "target_replacement_pod_name": row.get("target_replacement_pod_name"),
            "target_replacement_pod_uid": row.get("target_replacement_pod_uid"),
            "restart_chain_observed": restart_chain_observed,
            "probe_isolation_held": probe_isolation_held,
            "state": row.get("state"),
            "detected": row.get("detected"),
            "detection_source": row.get("detection_source"),
            "action": row.get("action"),
            "promotion_verified": row.get("promotion_verified"),
            "injection_valid": row.get("injection_valid"),
            "probe_valid": row.get("probe_valid"),
            "invalid_reason": row.get("invalid_reason"),
            "slo_version": row.get("slo_version"),
            "latency_slo_sec": row.get("latency_slo_sec"),
            "p95_peak": row.get("p95_peak"),
            "availability_min": row.get("availability_min"),
            "t_injection": row.get("t_injection"),
            "t_injection_request": row.get("t_injection_request"),
            "t_injection_last_seen": row.get("t_injection_last_seen"),
            "t_injection_observed": row.get("t_injection_observed"),
            "timing_schema_version": row.get("timing_schema_version"),
            "temporal_relation": temporal_relation,
            "t_detection": row.get("t_detection"),
            "t_slo": row.get("t_slo"),
            "t_recovery": row.get("t_recovery"),
            "commit_sha": row.get("commit_sha"),
            "detection_lead_sec": _seconds_between(ts, "t_detection", "t_slo"),
            "action_delay_sec": _seconds_between(ts, "t_detection", "t_switch"),
            "recovery_sec": _seconds_between(ts, "t_slo", "t_recovery"),
            "total_recovery_sec": _seconds_between(ts, "t_injection", "t_recovery"),
            "timing_anomaly": timing_anomaly,
            "included_in_main_analysis": included,
            "exclusion_reason": exclusion_reason,
            "notes": row.get("notes", ""),
        })

    for (scenario, arm, rep), run_ids in seen_keys.items():
        if len(run_ids) > 1:
            issues.append(ValidationIssue(
                ",".join(run_ids), f"{scenario}/{arm}/rep={rep}", f"중복 {len(run_ids)}건"))

    by_scenario_arm: dict = {}
    for (scenario, arm, rep) in seen_keys:
        by_scenario_arm.setdefault((scenario, arm), set()).add(rep)
    for (scenario, arm), reps in by_scenario_arm.items():
        missing = set(range(1, EXPECTED_REPS + 1)) - reps
        if missing:
            issues.append(ValidationIssue(
                "-", f"{scenario}/{arm}", f"누락된 rep: {sorted(missing)} (기대 {EXPECTED_REPS}회)"))

    return out_rows, issues


def write_comparison_csv(out_rows: list, out_path: Path) -> None:
    if not out_rows:
        return
    fieldnames = list(out_rows[0].keys())
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(out_rows)


def main():
    parser = argparse.ArgumentParser(description="Phase 8 trial 결과를 run-level comparison.csv로 집계")
    parser.add_argument("--results-dir", default=str(RESULTS_DIR))
    parser.add_argument("--out", default=str(RESULTS_DIR / "comparison.csv"))
    args = parser.parse_args()

    rows = load_all_results(Path(args.results_dir))
    out_rows, issues = build_comparison(rows)
    write_comparison_csv(out_rows, Path(args.out))

    included = sum(1 for r in out_rows if r["included_in_main_analysis"])
    print(f"총 {len(out_rows)}건 중 본 분석 포함 {included}건, 제외 {len(out_rows) - included}건")
    if issues:
        print(f"\n검증 이슈 {len(issues)}건:")
        for issue in issues:
            print(f"  [{issue.run_id}] {issue.field}: {issue.problem}")
    print(f"\n결과: {args.out}")


if __name__ == "__main__":
    main()
