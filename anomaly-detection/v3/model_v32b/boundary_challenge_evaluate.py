#!/usr/bin/env python3
"""§79.7/§84 - v3.2b boundary challenge set의 단 한 번 탐색적 평가 전용
해석 로직. **읽기 전용**, threshold 재조정·재학습에 쓰지 않는다
(사용자 지시 - `not_used_for` in boundary_challenge_manifest.json).

score 계산 자체는 `model_v31/evaluate.py`의 `evaluate_session()`(변경
없음, sealed holdout 평가와 완전히 같은 함수)을 그대로 재사용한다 -
이 파일이 새로 하는 일은 (1) role별(safe_transient/actual_violation/
extreme_non_reproduced) 분류, (2) lead time·early/late/missed 판정,
(3) anomaly streak timeline 재구성, (4) 세 그룹을 절대 합쳐 평균 내지
않는 집계, (5) A/B/C/D 해석 분류뿐이다."""
from datetime import datetime
from typing import Optional

CHALLENGE_SESSIONS = {
    "q3c-sustained_load-20260920-r1": {
        "file": "qualification_data/sessions/q3c-sustained_load-20260920-r1.json",
        "role": "sustained_load_pass",
    },
    "official-train-sustained_load-20260920": {
        "file": "official_data/sessions/official-train-sustained_load-20260920.json",
        "role": "sustained_load_violation",
    },
    "q3c-burst-20260920-r1": {
        "file": "qualification_data/sessions/q3c-burst-20260920-r1.json",
        "role": "burst_safe",
    },
    "official-train-burst-20260920": {
        "file": "official_data/sessions/official-train-burst-20260920.json",
        "role": "burst_safe",
    },
    "official-calib-burst-20260920": {
        "file": "official_data/sessions/official-calib-burst-20260920.json",
        "role": "burst_violation",
    },
    "qual-low_load-20260920-r6": {
        "file": "data/sessions/qual-low_load-20260920-r6.json",
        "role": "non_reproduced_anomaly",
    },
}

ROLE_CATEGORY = {
    "sustained_load_pass": "safe_transient",
    "burst_safe": "safe_transient",
    "sustained_load_violation": "actual_violation",
    "burst_violation": "actual_violation",
    "non_reproduced_anomaly": "extreme_non_reproduced",
}


def classify_detection(t_slo: Optional[str], first_signal_window_start_utc: Optional[str]) -> Optional[str]:
    """actual_violation 세션 전용 - t_slo가 없는 세션(safe_transient)에는
    쓰지 않는다(호출부가 category로 분기)."""
    if t_slo is None:
        return None
    if first_signal_window_start_utc is None:
        return "missed"
    t_slo_dt = datetime.fromisoformat(t_slo)
    t_detect_dt = datetime.fromisoformat(first_signal_window_start_utc)
    return "early_detection" if t_detect_dt <= t_slo_dt else "late_detection"


def compute_lead_time_sec(t_slo: Optional[str], first_signal_window_start_utc: Optional[str]) -> Optional[float]:
    """§4 정의: lead_time_sec = t_slo - t_detection. 양수=선제, 0=동시,
    음수=사후. signal이 없으면 None(= §4의 "null")."""
    if t_slo is None or first_signal_window_start_utc is None:
        return None
    t_slo_dt = datetime.fromisoformat(t_slo)
    t_detect_dt = datetime.fromisoformat(first_signal_window_start_utc)
    return (t_slo_dt - t_detect_dt).total_seconds()


def anomaly_streak_timeline(window_timestamps: list, scores: list, threshold: float) -> list:
    """session별 anomaly streak timeline - window마다 score·이상 여부·
    그 시점까지의 연속 카운트를 기록한다(연속 3회 조건이 실제로 어디서
    끊기는지 감사 가능하게)."""
    timeline = []
    consecutive = 0
    for ts, score in zip(window_timestamps, scores):
        is_anomalous = score < threshold
        consecutive = consecutive + 1 if is_anomalous else 0
        timeline.append({
            "window_start_utc": ts, "score": score,
            "is_anomalous": is_anomalous, "consecutive_count": consecutive,
        })
    return timeline


def evaluate_challenge_session(session: dict, role: str, model, scaler, schema, threshold: float, *,
                                evaluate_session_fn, score_session_rows_fn) -> dict:
    """`evaluate_session_fn`/`score_session_rows_fn`은 호출부가
    `model_v31.evaluate`의 동명 함수를 그대로 주입한다(변경 없음, sealed
    holdout과 동일 함수) - 이 함수는 그 결과에 role별 해석만 얹는다."""
    category = ROLE_CATEGORY[role]
    result = evaluate_session_fn(session, model, scaler, schema, threshold)
    t_slo = session.get("t_slo")
    first_signal = result.get("first_signal_window_start_utc")

    timed = score_session_rows_fn(session, model, scaler, schema)
    window_timestamps = [ts for ts, _ in timed]
    scores = [s for _, s in timed]

    interpretation = {
        "category": category, "role": role,
        "t_slo": t_slo,
        "lead_time_sec": None, "detection_class": None, "unnecessary_signal": None,
    }
    if category == "safe_transient":
        interpretation["unnecessary_signal"] = result["signal_count"] > 0
    elif category == "actual_violation":
        interpretation["detection_class"] = classify_detection(t_slo, first_signal)
        interpretation["lead_time_sec"] = compute_lead_time_sec(t_slo, first_signal)
    # extreme_non_reproduced(§60)는 별도 분류 없이 원시 결과만 기록(§6 지시 - 다른 그룹과 합산 금지)

    return {
        **result,
        **interpretation,
        "original_profile": session.get("profile") or session.get("regime"),
        "original_verdict_excluded": session.get("excluded"),
        "anomaly_streak_timeline": anomaly_streak_timeline(window_timestamps, scores, threshold),
    }


def aggregate_safe_transient(results: list) -> dict:
    n = len(results)
    unnecessary = sum(1 for r in results if r["unnecessary_signal"])
    return {
        "n_sessions": n,
        "unnecessary_signal_count": unnecessary,
        "unnecessary_signal_fraction": unnecessary / n if n else None,
        "total_signal_episodes": sum(r["signal_count"] for r in results),
        "by_role": {r["session_id"]: {"role": r["role"], "unnecessary_signal": r["unnecessary_signal"]}
                    for r in results},
    }


def aggregate_actual_violation(results: list) -> dict:
    lead_times = [r["lead_time_sec"] for r in results if r["lead_time_sec"] is not None]
    classes = [r["detection_class"] for r in results]
    return {
        "n_sessions": len(results),
        "early_detection_count": classes.count("early_detection"),
        "late_detection_count": classes.count("late_detection"),
        "missed_count": classes.count("missed"),
        "lead_times_sec": lead_times,
        "lead_time_median_sec": sorted(lead_times)[len(lead_times) // 2] if lead_times else None,
        "lead_time_range_sec": [min(lead_times), max(lead_times)] if lead_times else None,
        "note": "표본 2개뿐 - 탐지율의 통계적 우월성을 주장하지 않는다(§6 지시)",
    }


def classify_overall(safe_agg: dict, violation_agg: dict, holdout_adopted: bool = True) -> dict:
    """§7 A/B/C/D 해석 분류 - artifact 채택 상태를 소급 변경하지 않는
    외부 검증 목적. B(safe transient signal 1건 이상)와 C(violation 전부
    missed/late)는 서로 독립된 트리거라 동시에 성립할 수 있다 - 그 경우
    사용자 지시대로 확대 해석하지 않고 D(Mixed)로 보고한다."""
    b_trigger = safe_agg["unnecessary_signal_count"] >= 1
    c_trigger = (violation_agg["n_sessions"] > 0
                 and violation_agg["early_detection_count"] == 0
                 and violation_agg["n_sessions"] == (violation_agg["missed_count"]
                                                      + violation_agg["late_detection_count"]))
    a_condition = (not b_trigger) and violation_agg["early_detection_count"] >= 1 and holdout_adopted

    if b_trigger and c_trigger:
        return {"classification": "D", "label": "Mixed",
                "reason": "safe transient signal과 actual violation 전부 missed/late 조건이 동시에 성립 - "
                          "B/C가 겹쳐 확대 해석하지 않음"}
    if a_condition:
        return {"classification": "A", "label": "Promising",
                "reason": "safe transient unnecessary signal 0/3, actual violation 중 최소 1건 조기 탐지, "
                          "Holdout 채택 결과와 모순 없음"}
    if b_trigger:
        return {"classification": "B", "label": "Over-sensitive",
                "reason": f"safe transient에서 signal {safe_agg['unnecessary_signal_count']}건 발생 - "
                          "Holdout 정상 domain은 통과했지만 boundary load에서 불필요한 promotion 위험 있음"}
    if c_trigger:
        return {"classification": "C", "label": "Insensitive",
                "reason": "actual violation 전부 missed 또는 late - 정상-domain FPR은 통과했지만 "
                          "SLO 예방 효용 근거 부족"}
    return {"classification": "D", "label": "Mixed",
            "reason": "위 A/B/C 조건 중 어느 것도 명확히 성립하지 않음 - 확대 해석하지 않음"}
