#!/usr/bin/env python3
"""§84 - v3.2b boundary challenge set 단 한 번 평가 실행 스크립트.
동결된 model/scaler/threshold(freeze commit 57d4440, 변경 없음)로
`boundary_challenge_manifest.json`의 6개 세션을 채점한다. 완전히
read-only - 기존 raw/feature 자료를 그대로 쓰고, artifact를 전혀
다시 쓰지 않는다."""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
V3_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(V3_DIR / "model_v31"))
sys.path.insert(0, str(V3_DIR))
sys.stdout.reconfigure(encoding="utf-8")

from evaluate import evaluate_session, load_frozen_artifacts, score_session_rows  # noqa: E402 (model_v31)
from qualify_normal_profile import _git_commit_sha  # noqa: E402

from boundary_challenge_evaluate import (  # noqa: E402
    CHALLENGE_SESSIONS, ROLE_CATEGORY, aggregate_actual_violation, aggregate_safe_transient,
    classify_overall, evaluate_challenge_session,
)

ARTIFACTS_DIR = Path(__file__).parent / "artifacts"

# §84.1 - 시작 전 무결성 확인에서 기록한 원본 session 파일 SHA-256(채점 전).
EXPECTED_SESSION_SHA256 = {
    "q3c-sustained_load-20260920-r1": "50a7af97b5777958d94e84ad3552960a3b31247551bc5614bc8c0fae67299c31",
    "official-train-sustained_load-20260920": "773f61b1cc706541a9d1ee61de8fecc0ad778c01942ca625e97f631a8a837895",
    "q3c-burst-20260920-r1": "f0dccbaaeae141bd813944daba0b7a34df9b4a1b4b2381539a7226b796997c3a",
    "official-train-burst-20260920": "cf871e66b0111b2472474ab805065f1a46f8dee0ada37227929042ece132eae6",
    "official-calib-burst-20260920": "77280a8802f3ea27124de53d447b828d90e591138b9a17d6c98c1e520482c806",
    "qual-low_load-20260920-r6": "c4e7ff64538705430fea24a1859c4232df9220bb5b76c4e6266fc86cd37bb18f",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_challenge_session(session_id: str) -> dict:
    path = V3_DIR / CHALLENGE_SESSIONS[session_id]["file"]
    actual = _sha256(path)
    expected = EXPECTED_SESSION_SHA256[session_id]
    if actual != expected:
        raise RuntimeError(f"fail-closed: {session_id} 원본 hash 불일치(expected={expected}, actual={actual})")
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    model, scaler, schema, threshold_doc = load_frozen_artifacts(ARTIFACTS_DIR)
    threshold = threshold_doc["threshold"]

    all_results = {}
    for session_id, meta in CHALLENGE_SESSIONS.items():
        session = _load_challenge_session(session_id)
        result = evaluate_challenge_session(
            session, meta["role"], model, scaler, schema, threshold,
            evaluate_session_fn=evaluate_session, score_session_rows_fn=score_session_rows,
        )
        all_results[session_id] = result
        print(f"{session_id} ({meta['role']}): n={result['n_points']} point_anomaly={result['point_anomaly_count']} "
              f"(FPR={result['point_fpr']:.4f}) max_consecutive={result['max_consecutive_anomalous']} "
              f"signal_episodes={result['signal_count']} t_slo={result['t_slo']} "
              f"detection_class={result['detection_class']} lead_time_sec={result['lead_time_sec']} "
              f"unnecessary_signal={result['unnecessary_signal']}")

    safe_results = [r for r in all_results.values() if r["category"] == "safe_transient"]
    violation_results = [r for r in all_results.values() if r["category"] == "actual_violation"]
    extreme_result = next(r for r in all_results.values() if r["category"] == "extreme_non_reproduced")

    safe_agg = aggregate_safe_transient(safe_results)
    violation_agg = aggregate_actual_violation(violation_results)
    overall = classify_overall(safe_agg, violation_agg, holdout_adopted=True)

    print("\n=== safe_transient 집계 ===")
    print(json.dumps(safe_agg, indent=2, ensure_ascii=False))
    print("\n=== actual_violation 집계(표본 2개, 통계적 우월성 주장 없음) ===")
    print(json.dumps(violation_agg, indent=2, ensure_ascii=False))
    print("\n=== §60 non_reproduced_anomaly 단독 결과(다른 그룹과 합산하지 않음) ===")
    print(f"session={extreme_result['session_id']} point_anomaly={extreme_result['point_anomaly_count']}/"
          f"{extreme_result['n_points']} max_consecutive={extreme_result['max_consecutive_anomalous']} "
          f"signal_episodes={extreme_result['signal_count']}")
    print("\n=== 해석 분류 ===")
    print(json.dumps(overall, indent=2, ensure_ascii=False))

    report = {
        "threshold": threshold,
        "model_freeze_commit": "57d4440d76b9bc476942c05df61d72c77be8e31d",
        "evaluation_commit_at_run_time": _git_commit_sha(),
        "artifact_hashes": {
            "model.pkl": _sha256(ARTIFACTS_DIR / "model.pkl"),
            "scaler.pkl": _sha256(ARTIFACTS_DIR / "scaler.pkl"),
            "threshold.json": _sha256(ARTIFACTS_DIR / "threshold.json"),
            "feature-schema.json": _sha256(ARTIFACTS_DIR / "feature-schema.json"),
        },
        "challenge_session_source_sha256": EXPECTED_SESSION_SHA256,
        "per_session": {sid: {k: v for k, v in r.items() if k != "window_timestamps"}
                        for sid, r in all_results.items()},
        "safe_transient_aggregate": safe_agg,
        "actual_violation_aggregate": violation_agg,
        "non_reproduced_anomaly_standalone": {k: v for k, v in extreme_result.items() if k != "window_timestamps"},
        "overall_classification": overall,
        "not_used_for": ["threshold tuning", "feature selection", "model retraining"],
        "scored_exactly_once": True,
    }
    out_path = ARTIFACTS_DIR / "boundary-challenge-evaluation.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\n저장: {out_path}")


if __name__ == "__main__":
    main()
