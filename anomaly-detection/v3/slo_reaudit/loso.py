#!/usr/bin/env python3
"""§78.5 - Leave-one-session-out 진단. 세션을 통계 단위로 삼는다(지시) -
row를 풀링해 quartile을 구하면 세션 간 이질성이 큰 경우 오도될 수 있음을
`analyze.py`의 1차 결과에서 실제로 확인했다(풀링된 사분위수 기준으로는
calib2-low-02가 "범위 밖"으로 보였지만, 세션별 전체 min/max로 다시 보면
여러 PASS 세션도 동일하게 넓은 범위를 보임 - 이 파일이 그 교정판)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from stats_utils import outside_robust_range, robust_range

FEATURE_NAMES = ["cpu_mean", "cpu_slope", "memory_mean", "memory_slope", "queue_mean", "queue_slope", "cache_mean", "cache_slope"]


def per_session_summary(entry: dict) -> dict:
    """세션 하나의 각 feature에 대해 (min, max, mean)을 계산 - LOSO에서
    "이 세션이 이 feature에서 보인 값의 범위"를 세션 하나짜리 통계량으로
    요약한다."""
    rows = [r["features"] for r in entry["_full_session"]["feature_rows"] if r["valid"]]
    summary = {}
    for i, name in enumerate(FEATURE_NAMES):
        vals = [row[i] for row in rows]
        summary[name] = {"min": min(vals), "max": max(vals), "mean": sum(vals) / len(vals)}
    return summary


def leave_one_session_out(entries: list) -> list:
    """entries: registry_entry() 결과 리스트(low_load 8세션 전부, PASS+FAIL
    섞여 있어도 됨 - 각 세션을 한 번씩 빼고 나머지의 robust range로 그
    세션 자신의 min/max가 벗어나는지 본다). 반환: 세션별 진단 리스트."""
    summaries = {e["session_id"]: per_session_summary(e) for e in entries}
    out = []
    for target_id, target_summary in summaries.items():
        others = {sid: s for sid, s in summaries.items() if sid != target_id}
        per_feature = {}
        for name in FEATURE_NAMES:
            other_mins = [s[name]["min"] for s in others.values()]
            other_maxs = [s[name]["max"] for s in others.values()]
            other_means = [s[name]["mean"] for s in others.values()]
            # "다른 세션들이 실제로 도달한 값의 전체 범위" = 그들 각자의 min의 최솟값 ~ max의 최댓값.
            other_full_range = {"min": min(other_mins), "max": max(other_maxs)}
            mean_ref_range = robust_range(other_means)
            per_feature[name] = {
                "target_min": target_summary[name]["min"], "target_max": target_summary[name]["max"],
                "target_mean": target_summary[name]["mean"],
                "other_sessions_full_range": other_full_range,
                "target_min_outside_others_full_range": not (other_full_range["min"] <= target_summary[name]["min"] <= other_full_range["max"]),
                "target_max_outside_others_full_range": not (other_full_range["min"] <= target_summary[name]["max"] <= other_full_range["max"]),
                "target_mean_outside_others_mean_robust_range": outside_robust_range(target_summary[name]["mean"], mean_ref_range),
            }
        out.append({"session_id": target_id, "per_feature": per_feature})
    return out
