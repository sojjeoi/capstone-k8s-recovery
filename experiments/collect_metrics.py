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
증거라 사후 수정 흔적이 남으면 안 된다. (유일한 공인 예외는 reconcile_audit.py -
감사·판정 필드와 provenance만 보완하고 원본을 .pre-reconcile.bak으로 보존한다.
그렇게 보완된 결과는 judgment_source/audit_reconciled_at 컬럼으로 구분된다.)

판정·조치 필드(2026-09-19): detected/detection_source/detector/action/
decision_outcome/promotion_verified는 recovery-policy 권위 상태에서 채워진 값이어야
하고, 그렇지 않은 흔적(t_detection은 있는데 detected=false 등)은
_check_judgment_consistency()가 모순으로 남긴다. 비동기 감사 미완료(audit_status=
pending/failed)는 timing anomaly와 분리해 audit_pending 컬럼과 별도 issue로 표시한다.

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

계획된 promotion은 비정상 교체가 아니다(2026-09-20, 계약서 §5.9): promotion은 장애 pod에서 준비된
정상 pod로 트래픽을 옮기는 실험 처치 자체라 active target이 바뀌는 것이 정상이다. target_replaced=true
만으로 restart_chain_observed=true / probe_isolation_held=false라고 판정하면 proposed 파일럿처럼 promotion이
만든 교체를 "probe 격리 실패"로 오해한다. _classify_target_change()가 교체를 none / planned_promotion /
unplanned / indeterminate / not_applicable로 나누고(comparison의 target_change_kind), 두 분석 필드는 unplanned
여부만으로 정한다 - promotion 정보가 불완전하거나 모순되면 추정하지 않고 None + validation issue다. TrialResult
스키마와 원본 JSON은 건드리지 않는다(파생 해석만 바뀐다).
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
    "t_target_replaced",
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


def _pod_evidence_of_unplanned_change(evidence: Optional[dict]) -> Optional[str]:
    """pod 수준 증거(선택 입력 - 관찰기 분석 등)에서 promotion과 별개인 restart·UID 교체 흔적을 찾는다. 없으면 None."""
    if not evidence:
        return None
    found = []
    if (evidence.get("restarts") or 0) > 0:
        found.append(f"restart +{evidence['restarts']}")
    if evidence.get("uid_replaced"):
        found.append("UID 교체")
    if evidence.get("target_lost_before_promotion"):
        found.append(f"promotion 전 target 소멸({evidence['target_lost_before_promotion']})")
    return ", ".join(found) or None


def _classify_target_change(row: dict, ts: dict, evidence: Optional[dict] = None) -> tuple:
    """active target 변경을 해석한다 -> (kind, reason)  (계약서 §5.9, 2026-09-20).
    kind: none(교체 관측 없음) | planned_promotion(검증된 promotion이 만든 변경) | unplanned(promotion으로 설명되지 않는
    변경 = 재시작 연쇄·교체 후보) | indeterminate(promotion 정보가 불완전·모순 - 추정하지 않는다) | not_applicable
    (profile이 default/network_tolerant가 아니거나 target_replaced 필드 없음).

    - promotion과 별개인 pod 수준 증거(restart·UID 교체·promotion 전 target 소멸 - 선택 입력 `evidence`)가 있으면
      target_replaced 값과 무관하게 unplanned - promotion으로 가리지 않는다.
    - target_replaced=true인데 promotion 활동(action=promote_preview, promotion_verified, t_api_request, t_switch)이 전혀
      없으면 unplanned. 활동이 있으면 promotion_verified=true + action=promote_preview + t_api_request·t_switch 존재 +
      인과 순서(t_api_request <= t_switch)가 모두 갖춰져야만 판단한다 - 하나라도 빠지거나 어긋나면 indeterminate.
    - 검증된 promotion이 있을 때: 교체 관측(t_target_replaced)이 promotion 요청(t_api_request)보다 앞서면 promotion과 별개라
      unplanned, 그 이후이고 교체 pod가 식별되면 planned_promotion, 시각이나 교체 pod 식별이 없으면 indeterminate.
      (어댑터는 stage 경계에서만 교체를 확인하므로 t_target_replaced는 관측 시각이다 - promotion 요청 이후에 관측됐다는 사실은
      "promotion이 만들 수 있는 변경"이라는 뜻이지 그 이전 재시작이 없었다는 증명이 아니다 - 그래서 pod 증거가 있으면 우선한다.)"""
    profile = row.get("readiness_probe_profile")
    if profile not in ("default", "network_tolerant"):
        return "not_applicable", "readiness_probe_profile이 default/network_tolerant가 아님"
    separate = _pod_evidence_of_unplanned_change(evidence)
    if separate:
        return "unplanned", f"promotion과 별개의 pod 증거: {separate}"
    replaced = row.get("target_replaced")
    if replaced is None:
        return "not_applicable", "target_replaced 필드 없음"
    if not replaced:
        return "none", "target 교체 관측 없음"
    activity = (row.get("action") == "promote_preview" or row.get("promotion_verified") is not None
                or row.get("t_api_request") is not None or row.get("t_switch") is not None)
    if not activity:
        return "unplanned", "promotion 없음(action·promotion_verified·t_api_request·t_switch 모두 없음)"
    problems = []
    if row.get("promotion_verified") is not True:
        problems.append(f"promotion_verified={row.get('promotion_verified')!r}(True 아님 - promotion이 active를 바꿨는지 알 수 없음)")
    if row.get("action") != "promote_preview":
        problems.append(f"action={row.get('action')!r}(promote_preview 아님)")
    for name in ("t_api_request", "t_switch"):
        if ts.get(name) is None:
            problems.append(f"{name} 없음/파싱 불가")
    if ts.get("t_api_request") is not None and ts.get("t_switch") is not None and ts["t_switch"] < ts["t_api_request"]:
        problems.append("t_switch < t_api_request(인과 순서 모순)")
    if problems:
        return "indeterminate", "promotion 정보 불완전/모순: " + "; ".join(problems)
    if ts.get("t_target_replaced") is None:
        return "indeterminate", "t_target_replaced 없음/파싱 불가 - 교체와 promotion 요청의 선후를 알 수 없음"
    if ts["t_target_replaced"] < ts["t_api_request"]:
        return "unplanned", (f"교체 관측 {row['t_target_replaced']}이 promotion 요청 {row['t_api_request']}보다 앞섬 - "
                             f"promotion과 별개")
    if not row.get("target_replacement_pod_name") or not row.get("target_replacement_pod_uid"):
        return "indeterminate", "교체 pod 식별 불가(target_replacement_pod_name/uid 없음) - 검증된 promotion의 pod인지 확인 못 함"
    return "planned_promotion", (f"검증된 promotion(t_api_request {row['t_api_request']}, t_switch {row['t_switch']}) 뒤 교체 관측 "
                                 f"{row['t_target_replaced']}(pod {row['target_replacement_pod_name']})")


def _compute_profile_interpretation(row: dict, kind: str) -> tuple:
    """network_degrade의 readiness_probe_profile + target 변경 해석(kind)으로 두 분석 필드를 정한다(2026-09-18 추가 -
    리뷰: tolerant profile에서 파드가 교체됐는데 SLO 위반이 안 잡혀 outcome=prevented만 남으면 "설정이 열화를 견뎠다"로
    오해할 수 있다는 지적; 2026-09-20 정정 - 계획된 promotion은 비정상 교체가 아니다). outcome은 절대 바꾸지 않는다 -
    SLO 판정과 별개의 분석 필드다.
    - default profile: unplanned 교체가 있을 때만 restart_chain_observed=True(연쇄장애 자체가 관찰됐는지).
    - network_tolerant profile: unplanned 교체가 없으면 probe_isolation_held=True(그 설정이 열화로부터 probe를 실제로
      격리했는지), 있으면 False.
    - planned_promotion(검증된 promotion이 만든 변경)은 unplanned가 아니다. indeterminate(promotion 정보 불완전·모순)와
      not_applicable(profile이 둘 중 하나가 아님·필드 없음)은 둘 다 None - True/False를 추정하지 않는다.
    restart_chain_observed=True는 이 통제된 실험(네트워크 열화 주입과 같은 trial 안에서의 시간적 연관) 안에서의
    관찰을 뜻할 뿐이다 - 단일 실행 하나로 인과관계를 확정한다는 뜻이 아니다(반복·통계적 근거는 여러 rep을 모은 뒤
    별도로 봐야 한다)."""
    profile = row.get("readiness_probe_profile")
    if profile not in ("default", "network_tolerant") or kind in ("indeterminate", "not_applicable"):
        return None, None
    unplanned = kind == "unplanned"
    return (unplanned, None) if profile == "default" else (None, not unplanned)


def _check_tolerant_profile_prevented_misleading(row: dict, kind: str, issues: list) -> None:
    """network_tolerant profile에서 promotion으로 설명되지 않는 대상 교체(unplanned, probe_isolation_held=
    False)가 있는데 outcome=prevented로만 남으면, "위반이 안 잡혔다"만 보고 그 설정이 열화를 견뎠다고 오해할 위험이
    있다(2026-09-18 추가, 리뷰 지적) - _check_prevented_validity와 같은 이유로 별도 issue를 남긴다(단독 CSV 컬럼만으로는
    놓치기 쉬움). 계획된 promotion(planned_promotion)에 의한 교체는 해당하지 않는다(2026-09-20)."""
    if row.get("readiness_probe_profile") != "network_tolerant":
        return
    if kind == "unplanned" and row.get("outcome") == "prevented":
        issues.append(ValidationIssue(
            row.get("run_id", "?"), "outcome",
            "network_tolerant profile에서 promotion으로 설명되지 않는 대상 교체(unplanned)가 있었는데 "
            "outcome=prevented - probe_isolation_held=false를 함께 보지 않으면 "
            "설정이 열화를 견딘 것으로 오해할 수 있음"))


def _check_target_change_consistency(row: dict, kind: str, reason: str, evidence: Optional[dict], issues: list) -> None:
    """target 변경 해석의 validation issue(2026-09-20, 계약서 §5.9): promotion 정보가 불완전하거나 모순이라 교체를 promotion과
    구분할 수 없으면(indeterminate) 두 분석 필드를 None으로 남기고 그 사실을 issue로 드러낸다. 반대로 어댑터는 교체를
    관측하지 못했는데(target_replaced=false) pod 증거는 restart·교체를 가리키면 두 출처의 불일치도 issue다."""
    run_id = row.get("run_id", "?")
    if kind == "indeterminate":
        issues.append(ValidationIssue(
            run_id, "target_replaced",
            f"target_replaced=true를 promotion과 구분할 수 없음 - {reason} -> "
            f"restart_chain_observed/probe_isolation_held를 추정하지 않고 None으로 남김"))
    if kind == "unplanned" and not row.get("target_replaced") and _pod_evidence_of_unplanned_change(evidence):
        issues.append(ValidationIssue(
            run_id, "target_replaced",
            f"target_replaced={row.get('target_replaced')!r}인데 pod 증거는 restart·교체를 가리킴({reason}) - "
            f"어댑터가 관측하지 못한 교체(stage 경계 밖·마지막 stage 도중)"))


def _check_judgment_consistency(row: dict, issues: list) -> None:
    """판정·조치 필드끼리의 모순을 검출한다(2026-09-19 추가). 이 필드들은 예전에 어디서도
    채워지지 않아 실제 탐지·promotion과 무관하게 기본값이었다 - 그 회귀(또는 아직
    reconcile 안 된 과거 trial)를 조용히 지나치지 않기 위한 검사다. native는
    recovery-policy 미개입이라 대상이 아니다."""
    if row.get("arm") == "native":
        return
    run_id = row.get("run_id", "?")
    if row.get("t_detection") is not None and row.get("detected") is not True:
        issues.append(ValidationIssue(
            run_id, "detected",
            f"t_detection이 있는데 detected={row.get('detected')!r} - 판정 필드가 권위 상태에서 채워지지 않음"
            f"(judgment_source={row.get('judgment_source')!r}, reconcile_audit.py 필요 여부 확인)"))
    if row.get("detected") is True and row.get("t_detection") is None:
        issues.append(ValidationIssue(run_id, "t_detection", "detected=true인데 t_detection 없음"))
    if row.get("action") == "promote_preview":
        if row.get("t_api_request") is None:
            issues.append(ValidationIssue(run_id, "t_api_request", "action=promote_preview인데 t_api_request 없음"))
        if row.get("promotion_verified") is None:
            issues.append(ValidationIssue(
                run_id, "promotion_verified", "action=promote_preview인데 promotion 검증 결과(promotion_verified) 없음"))


# arm이 "실제로 띄우는" 예측 detector(계약서 §1) - native는 recovery-policy·detector 둘 다 없다.
# fixed_threshold/proposed는 예측 모델만 다르고 공통 Alertmanager 반응형 fallback을 함께 가진다.
EXPECTED_DETECTOR_BY_ARM = {"native": None, "fixed_threshold": "fixed_threshold", "proposed": "isolation_forest"}
REACTIVE_FALLBACK_DETECTOR = "alertmanager"


def _check_detector_consistency(row: dict, issues: list) -> str:
    """arm과 실제 detector의 일치를 검증한다(2026-09-19 추가, 계약서 §5.7). 반환값은 comparison의
    detector_check 컬럼: ok | reactive_fallback | inferred_pilot | not_applicable | mismatch | missing.
    mismatch/missing만 validation issue이고 reactive_fallback/inferred_pilot은 오류가 아니라 별도 표시다.

    규칙: native는 detector null. 예측 경로 탐지(detection_source=predictive)는 detector가 arm의 예측
    detector와 같아야 한다(fixed_threshold->fixed_threshold, proposed->isolation_forest). 최초 유효 탐지가
    Alertmanager fallback(detection_source=reactive)이면 detector=alertmanager가 정의된 예외로 허용된다
    (두 non-native arm이 공통으로 가진 fallback - 다른 detector 이름이거나 predictive인데 alertmanager면
    오류). 탐지가 없으면 detector도 null. 과거 pilot의 추론된 detector(reconciliation.inferred_fields.
    detector)는 arm 기대와 일치하면 오류가 아니라 inferred_pilot으로 따로 표시하되, 본 실험(is_pilot=
    false) 데이터의 추론은 허용되지 않아 오류이고 arm과 불일치하는 추론값도 오류다."""
    arm = row.get("arm")
    if arm not in EXPECTED_DETECTOR_BY_ARM:
        return "not_applicable"
    run_id = row.get("run_id", "?")
    detector, source = row.get("detector"), row.get("detection_source")
    expected = EXPECTED_DETECTOR_BY_ARM[arm]

    def flag(problem: str, label: str) -> str:
        issues.append(ValidationIssue(run_id, "detector", problem))
        return label

    if arm == "native":
        if detector is not None:
            return flag(f"arm=native인데 detector={detector!r} - native는 recovery-policy·detector 미개입(계약서 §1)", "mismatch")
        return "ok"
    if row.get("detected") is not True:
        if detector is not None:
            return flag(f"detected=false인데 detector={detector!r}", "mismatch")
        return "not_applicable"
    inferred = "detector" in ((row.get("reconciliation") or {}).get("inferred_fields") or {})
    if inferred and row.get("is_pilot") is not True:
        return flag("본 실험(is_pilot=false) 데이터에 추론된 detector가 있음 - 본 실험 데이터의 detector 추론은 허용되지 않음(계약서 §5.5)", "mismatch")
    if source == "reactive":
        if detector == REACTIVE_FALLBACK_DETECTOR:
            return "reactive_fallback"
        return flag(f"detection_source=reactive인데 detector={detector!r} - 반응형 fallback의 detector는 {REACTIVE_FALLBACK_DETECTOR!r}", "mismatch")
    if source == "predictive":
        if detector is None:
            return flag(f"arm={arm}의 예측 탐지(detection_source=predictive)인데 detector를 알 수 없음(null)", "missing")
        if detector != expected:
            return flag(f"arm={arm}의 예측 detector는 {expected!r}이어야 하는데 detector={detector!r}", "mismatch")
        return "inferred_pilot" if inferred else "ok"
    return flag(f"detected=true인데 detection_source={source!r}(predictive/reactive 아님) - detector를 arm과 대조할 수 없음", "missing")


def _check_decision_switch_consistency(row: dict, issues: list) -> None:
    """t_decision/t_switch의 존재 규칙(2026-09-19 추가, 계약서 §5.2/§5.5). 순서(t_detection <=
    t_decision <= t_api_request <= t_switch)는 CAUSAL_CHAIN이 timing anomaly로 이미 검증한다 - 여기서는
    "있어야 할 때 있고 없어야 할 때 없는가"만 본다:
    - promotion이 없으면(action이 promote_preview가 아니면) t_api_request/t_switch는 null.
    - t_switch는 selector 검증이 성공한 promotion(promotion_verified=true)에서만 있다.
    - recovery-policy 실시간 상태(judgment_source=live_state)에서 회수한 trial은 탐지했으면 t_decision이,
      검증된 promotion이면 t_api_request/t_switch도 반드시 있다. 이 필드들이 채워지기 시작하기 전의
      trial(audit_reconcile로 보완된 과거 pilot 등)의 t_decision/t_switch는 추정으로 채우지 않고 null로
      보존하므로 이 존재 요구를 적용하지 않는다."""
    if row.get("arm") == "native":
        return
    run_id = row.get("run_id", "?")
    verified = row.get("promotion_verified") is True
    if row.get("t_switch") is not None and not verified:
        issues.append(ValidationIssue(
            run_id, "t_switch", f"t_switch가 있는데 promotion_verified={row.get('promotion_verified')!r} - "
                                f"selector 검증에 성공한 promotion에서만 기록돼야 함"))
    if row.get("action") != "promote_preview":
        for field in ("t_api_request", "t_switch"):
            if row.get(field) is not None:
                issues.append(ValidationIssue(
                    run_id, field, f"action={row.get('action')!r}(promotion 없음)인데 {field}가 있음 - "
                                   f"promotion이 없으면 null이어야 함"))
    if row.get("judgment_source") == "live_state":
        if row.get("detected") is True and row.get("t_decision") is None:
            issues.append(ValidationIssue(run_id, "t_decision", "detected=true인데 t_decision 없음(live_state)"))
        if verified:
            for field in ("t_api_request", "t_switch"):
                if row.get(field) is None:
                    issues.append(ValidationIssue(
                        run_id, field, f"promotion_verified=true인데 {field} 없음(live_state)"))


def _check_audit_status(row: dict, issues: list) -> bool:
    """비동기 감사 미완료(pending/failed)를 timing anomaly와 분리해 표시한다(2026-09-19
    추가) - Git push가 늦은 것은 실험 측정의 결함이 아니라 감사기록 후처리가 안 끝난 것이다
    (promotion 자체가 검증됐어도 마찬가지). 반환값은 audit_pending 컬럼. audit_status가
    아예 없는 과거 trial도 non-native에서 실제 조치가 있었는데 commit_sha가 없으면
    reconcile 전(pending)으로 본다."""
    run_id = row.get("run_id", "?")
    status = row.get("audit_status")
    unreconciled_legacy = (
        status is None and row.get("arm") != "native"
        and row.get("action") == "promote_preview" and row.get("commit_sha") is None
    )
    if status not in ("pending", "failed") and not unreconciled_legacy:
        return False
    verified = " - promotion 자체는 검증됨" if row.get("promotion_verified") is True else ""
    reason = row.get("audit_status_reason") or "audit_status 미기록(reconcile 전 과거 trial)"
    issues.append(ValidationIssue(
        run_id, "audit_status",
        f"audit {status or 'unreconciled'}{verified} - timing anomaly 아님, reconcile_audit.py로 재조정 필요: {reason}"))
    return True


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


def build_comparison(rows: list, pod_evidence: Optional[dict] = None) -> tuple:
    """pod_evidence(선택, 2026-09-20): {run_id: {"restarts": int, "uid_replaced": bool, "target_lost_before_promotion":
    시각|bool}} - 관찰기 등 pod 수준 증거. TrialResult에는 restart·UID 교체 증거가 없어(스키마 동결) 이 입력이 있을 때만
    promotion과 별개인 교체를 pod 증거로 확정할 수 있다(계약서 §5.9)."""
    issues: list = []
    out_rows = []
    seen_keys: dict = {}  # (scenario, arm, rep) -> [run_id, ...] - 제외 안 된 것만 대상
    pod_evidence = pod_evidence or {}

    for row in rows:
        _validate_schema(row, issues)
        ts = {f: _parse_ts(row.get(f), f, row.get("run_id", "?"), issues) for f in OPTIONAL_TS_FIELDS}

        exclusion_reason = _classify_exclusion(row)
        included = exclusion_reason is None
        timing_anomaly = _check_timing(row, ts, issues)
        _check_prevented_validity(row, issues)
        _check_injection_timestamps_consistency(row, ts, issues)
        evidence = pod_evidence.get(row.get("run_id"))
        target_change_kind, target_change_reason = _classify_target_change(row, ts, evidence)
        _check_tolerant_profile_prevented_misleading(row, target_change_kind, issues)
        _check_target_change_consistency(row, target_change_kind, target_change_reason, evidence, issues)
        _check_judgment_consistency(row, issues)
        _check_decision_switch_consistency(row, issues)
        detector_check = _check_detector_consistency(row, issues)
        audit_pending = _check_audit_status(row, issues)
        temporal_relation = _compute_temporal_relation(ts)
        restart_chain_observed, probe_isolation_held = _compute_profile_interpretation(row, target_change_kind)

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
            "baseline_valid": row.get("baseline_valid"),
            "t_baseline_ready": row.get("t_baseline_ready"),
            "baseline_sample_count": row.get("baseline_sample_count"),
            "baseline_p95": row.get("baseline_p95"),
            "baseline_availability": row.get("baseline_availability"),
            "readiness_probe_profile": row.get("readiness_probe_profile"),
            "readiness_probe_timeout_sec": row.get("readiness_probe_timeout_sec"),
            "target_replaced": row.get("target_replaced"),
            "t_target_replaced": row.get("t_target_replaced"),
            "target_replacement_pod_name": row.get("target_replacement_pod_name"),
            "target_replacement_pod_uid": row.get("target_replacement_pod_uid"),
            "target_change_kind": target_change_kind,
            "target_change_reason": target_change_reason,
            "restart_chain_observed": restart_chain_observed,
            "probe_isolation_held": probe_isolation_held,
            "state": row.get("state"),
            "detected": row.get("detected"),
            "detection_source": row.get("detection_source"),
            "detector": row.get("detector"),
            "detector_check": detector_check,
            "action": row.get("action"),
            "decision_outcome": row.get("decision_outcome"),
            "idempotency_key": row.get("idempotency_key"),
            "promotion_verified": row.get("promotion_verified"),
            "judgment_source": row.get("judgment_source"),
            "audit_status": row.get("audit_status"),
            "audit_status_reason": row.get("audit_status_reason"),
            "audit_record_id": row.get("audit_record_id"),
            "audit_pending": audit_pending,
            "audit_reconciled_at": row.get("audit_reconciled_at"),
            "t_audit_write": row.get("t_audit_write"),
            "t_audit_push": row.get("t_audit_push"),
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
            "t_decision": row.get("t_decision"),
            "t_api_request": row.get("t_api_request"),
            "t_switch": row.get("t_switch"),
            "detection_stage": row.get("detection_stage"),
            "t_slo": row.get("t_slo"),
            "slo_stage": row.get("slo_stage"),
            "action_stage": row.get("action_stage"),
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
    parser.add_argument("--pod-evidence", default=None,
                        help="선택 - {run_id: {restarts, uid_replaced, target_lost_before_promotion}} JSON(관찰기 분석 등 "
                             "pod 수준 증거). 있으면 promotion과 별개인 restart·교체를 unplanned로 판정한다(계약서 §5.9)")
    args = parser.parse_args()

    rows = load_all_results(Path(args.results_dir))
    pod_evidence = json.loads(Path(args.pod_evidence).read_text(encoding="utf-8")) if args.pod_evidence else None
    out_rows, issues = build_comparison(rows, pod_evidence)
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
