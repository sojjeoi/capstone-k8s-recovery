#!/usr/bin/env python3
"""§71/§73 - score_server.py의 실제 신호 상태기계를 그대로 재생하는
단일 함수. 학습(사용 안 함, feature_selection 몫)·calibration·holdout·
challenge 평가가 전부 `replay_detector()` 하나만 재사용한다 - 판정
로직을 여러 곳에 복제하지 않는다.

score_server.py(§71 감사)와의 대응:
  - EVAL_INTERVAL_SEC=15 -> eval_interval_sec
  - CONSECUTIVE_THRESHOLD=3 -> consecutive_threshold
  - COOLDOWN_SEC=60 -> cooldown_sec
  - `score < SCORE_THRESHOLD`(엄격한 미만) -> score < threshold
  - `consecutive_anomalous`는 정상 판정 1회로 즉시 0 리셋 -> 동일

recovery-policy의 `preview_ready` 게이트·`safety.py`의 추가 idempotency는
여기서 모델링하지 않는다(§71에서 명시한 범위 밖) - "score_server.py가
POST를 보내는가"만 재생한다."""
import statistics


def replay_detector(scores: list, threshold: float, *,
                     consecutive_threshold: int = 3, cooldown_sec: float = 60.0,
                     eval_interval_sec: float = 15.0) -> dict:
    """scores: 한 세션 안에서 시간 순서대로 정렬된 decision_function 값
    (각 원소 = 실제 15초 간격 평가 1회에 대응 - `build_dataset.py`의
    고정 15초 step과 일치, §71에서 gap 없음을 확인함). 세션 경계를 넘어
    이어붙이지 않는다 - 세션마다 독립적으로 호출해야 한다(실제 런타임도
    세션마다 별도 preview/트래픽 구간이라 상태가 이어지지 않음)."""
    if not scores:
        return {
            "n_points": 0, "point_anomaly_count": 0, "point_fpr": None,
            "signal_indices": [], "signal_count": 0, "max_consecutive_anomalous": 0,
            "score_min": None, "score_median": None, "score_max": None,
        }

    consecutive = 0
    max_consecutive = 0
    last_signal_index = None
    signal_indices = []
    point_anomaly_count = 0

    for i, score in enumerate(scores):
        is_anomalous = score < threshold
        if is_anomalous:
            point_anomaly_count += 1
            consecutive += 1
        else:
            consecutive = 0
        max_consecutive = max(max_consecutive, consecutive)

        if consecutive >= consecutive_threshold:
            elapsed = (i - last_signal_index) * eval_interval_sec if last_signal_index is not None else None
            in_cooldown = elapsed is not None and elapsed < cooldown_sec
            if not in_cooldown:
                signal_indices.append(i)
                last_signal_index = i

    n = len(scores)
    return {
        "n_points": n,
        "point_anomaly_count": point_anomaly_count,
        "point_fpr": point_anomaly_count / n,
        "signal_indices": signal_indices,
        "signal_count": len(signal_indices),
        "max_consecutive_anomalous": max_consecutive,
        "score_min": min(scores), "score_median": statistics.median(scores), "score_max": max(scores),
    }


def calibrate_threshold(session_scores: dict, *, consecutive_threshold: int = 3,
                         cooldown_sec: float = 60.0, eval_interval_sec: float = 15.0) -> dict:
    """§4 calibration 규칙 - `session_scores`(session_id -> 시간순 score
    리스트, calibration split만) 전체에서 false signal episode가 0인 가장
    민감한(=가장 높은) threshold를 찾는다. threshold가 낮아질수록(덜
    민감) point anomaly 수는 단조 비증가하므로 episode 수도 단조
    비증가한다 - 관측된 score를 높은 값부터 내림차순으로 훑으며 첫 번째로
    "모든 calibration session에서 episode 0"을 만족하는 값을 채택한다.

    선택된 threshold가 calibration에서 관측된 point anomaly를 단 하나도
    만들지 못하면(=calibration 전체 score 범위보다 낮아 사실상 아무것도
    탐지 못함) `calibration_failed=True`로 표시한다(사용자 지시 - 억지
    동결 금지)."""
    all_scores = sorted({s for scores in session_scores.values() for s in scores}, reverse=True)
    if not all_scores:
        return {"calibration_failed": True, "reason": "calibration score가 없음", "threshold": None}

    min_score = min(all_scores)
    # 마지막 후보 = 관측된 전체 범위보다 낮음(=아무것도 탐지 못하는
    # 자명한 해) - 이것만 통과하면 degenerate로 판정한다.
    candidates = all_scores + [min_score - 1e-9]

    for cand in candidates:
        per_session = {
            sid: replay_detector(scores, cand, consecutive_threshold=consecutive_threshold,
                                  cooldown_sec=cooldown_sec, eval_interval_sec=eval_interval_sec)
            for sid, scores in session_scores.items()
        }
        if all(r["signal_count"] == 0 for r in per_session.values()):
            total_point_anomalies = sum(r["point_anomaly_count"] for r in per_session.values())
            degenerate = total_point_anomalies == 0
            return {
                "calibration_failed": degenerate,
                "reason": ("선택된 threshold가 calibration 전체 score 범위보다 낮아 "
                           "단 하나의 point도 이상으로 잡지 못함(사실상 무의미한 동결)") if degenerate else None,
                "threshold": cand,
                "per_session": per_session,
            }

    # all_scores + [min-eps]는 항상 마지막 후보가 signal_count=0을 만족하므로
    # (전부 미탐지) 이 지점에 도달할 수 없다 - 방어적 fail-closed.
    return {"calibration_failed": True, "reason": "unreachable - 후보 전부 탐색했지만 0 episode 조건을 못 찾음", "threshold": None}
