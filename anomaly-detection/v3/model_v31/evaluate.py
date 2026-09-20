#!/usr/bin/env python3
"""§75/§76 - sealed holdout·boundary challenge 평가 공용 유틸. **읽기
전용**이다 - model.pkl/scaler.pkl/threshold.json/feature-schema.json을
불러오기만 하고 절대 다시 쓰지 않는다(재학습·재보정 없음, artifact
freeze 이후에는 이 파일이 그 무엇도 변경하지 않는다는 게 계약)."""
import json
import pickle
from pathlib import Path

from feature_selection import apply_feature_schema
from replay import replay_detector

ARTIFACTS_DIR = Path(__file__).parent / "artifacts"


def load_frozen_artifacts(artifacts_dir: Path = ARTIFACTS_DIR) -> tuple:
    model = pickle.loads((artifacts_dir / "model.pkl").read_bytes())
    scaler = pickle.loads((artifacts_dir / "scaler.pkl").read_bytes())
    schema = json.loads((artifacts_dir / "feature-schema.json").read_text(encoding="utf-8"))
    threshold_doc = json.loads((artifacts_dir / "threshold.json").read_text(encoding="utf-8"))
    return model, scaler, schema, threshold_doc


def score_session_rows(session: dict, model, scaler, schema) -> list:
    """반환: [(window_start_utc, score), ...] - `feature_rows`가 이미
    시간순으로 저장돼 있으므로(§70 실측 확인) 재정렬하지 않는다."""
    valid = [r for r in session["feature_rows"] if r["valid"]]
    if not valid:
        return []
    X = [apply_feature_schema(r["features"], schema) for r in valid]
    X_scaled = scaler.transform(X)
    scores = model.decision_function(X_scaled)
    return list(zip((r["window_start_utc"] for r in valid), (float(s) for s in scores)))


def evaluate_session(session: dict, model, scaler, schema, threshold: float, *,
                      consecutive_threshold: int = 3, cooldown_sec: float = 60.0,
                      eval_interval_sec: float = 15.0) -> dict:
    """session dict(이미 로드된 세션 JSON) 하나를 채점한다 - 파일을 쓰지
    않는다(호출자가 원하면 저장)."""
    timed_scores = score_session_rows(session, model, scaler, schema)
    scores = [s for _, s in timed_scores]
    replay = replay_detector(scores, threshold, consecutive_threshold=consecutive_threshold,
                              cooldown_sec=cooldown_sec, eval_interval_sec=eval_interval_sec)
    first_signal_ts = timed_scores[replay["signal_indices"][0]][0] if replay["signal_indices"] else None
    return {
        "session_id": session.get("session_id"),
        **replay,
        "first_signal_window_start_utc": first_signal_ts,
        "window_timestamps": [ts for ts, _ in timed_scores],
    }
