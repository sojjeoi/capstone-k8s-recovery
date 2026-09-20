#!/usr/bin/env python3
"""§76.9 - v3.2 최종 stop-loss 규칙(사전등록, holdout 결과를 보기 전에
고정). Prospective Holdout 6세션 전체에서 false signal episode가
1건이라도 있으면 무조건 거부 - 추가 반복으로 통과 결과를 찾지 않는다."""


def decide_holdout_outcome(holdout_report: dict) -> dict:
    """holdout_report: `evaluate_holdout.py`가 만드는 dict(최소
    `overall_false_signal_episodes`, `adoption_criteria` 키 필요).
    반환: {"outcome", "reason", "adopt_model", "proceed_to_challenge"}."""
    episodes = holdout_report["overall_false_signal_episodes"]
    criteria = holdout_report.get("adoption_criteria", {})

    if episodes > 0:
        return {
            "outcome": "stop_loss_rejected",
            "reason": (f"prospective holdout 6세션 전체에서 false signal episode {episodes}건 발생 - "
                       "Isolation Forest가 현재 데이터·구조로는 운영 신뢰성을 확보하지 못한 것으로 기록. "
                       "threshold 재조정·holdout의 calibration/training 편입·추가 데이터 수집·"
                       "boundary challenge 평가·runtime 통합 전부 금지. 추가 반복으로 통과 결과를 찾지 않는다."),
            "adopt_model": False,
            "proceed_to_challenge": False,
        }

    if not criteria.get("no_missing_or_nan", True) or not criteria.get("schema_and_hash_consistent", True):
        return {
            "outcome": "invalid_evaluation",
            "reason": "false signal episode는 0이지만 데이터/schema/hash 정합 조건을 만족하지 못함 - 채택 보류",
            "adopt_model": False,
            "proceed_to_challenge": False,
        }

    return {
        "outcome": "adopted",
        "reason": "prospective holdout 6세션 전체 false signal episode 0, schema/hash 정합, missing/NaN 없음",
        "adopt_model": True,
        "proceed_to_challenge": True,
    }
