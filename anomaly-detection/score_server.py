#!/usr/bin/env python3
"""실시간 위험도 판단 로직 — 주기적으로 최근 구간 특성을 평가해 Isolation
Forest 점수를 매기고, 연속판정+cooldown을 거쳐 recovery-policy에 신호를
발행한다(guideline.md Phase 6 pseudocode 그대로 구현).

recovery-policy/main.py는 아직 없어서(Phase 7 미착수) POST는 지금 당연히
연결 실패한다 - 그건 이 스크립트의 버그가 아니라 아직 받을 곳이 없는 것뿐이라
로그만 남기고 계속 돈다.
"""
import argparse
import pickle
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from features import extract_features

sys.stdout.reconfigure(encoding="utf-8")  # Windows 기본 cp949 콘솔 대응

ARTIFACTS_DIR = Path(__file__).parent / "artifacts"
RECOVERY_POLICY_URL = "http://localhost:8080/signal"

EVAL_INTERVAL_SEC = 15  # 평가 주기 (features.py의 Prometheus query step과 동일)
WINDOW_SEC = 60  # 평가 대상 trailing window (slo-definition.md와 동일 관례)
CONSECUTIVE_THRESHOLD = 3  # 이 횟수만큼 연속으로 이상이어야 신호 발행 (단발 노이즈 방지)
COOLDOWN_SEC = 60  # 신호 발행 후 이 시간 동안은 재발행 안 함
SCORE_THRESHOLD = 0.0  # decision_function 기준 - sklearn 관례상 0 미만이 predict()==-1(이상)과 동일


def load_model():
    with (ARTIFACTS_DIR / "model.pkl").open("rb") as f:
        model = pickle.load(f)
    with (ARTIFACTS_DIR / "scaler.pkl").open("rb") as f:
        scaler = pickle.load(f)
    return model, scaler


def evaluate(model, scaler) -> float:
    """지금 시각 기준 최근 WINDOW_SEC 구간의 이상 점수를 반환."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=WINDOW_SEC)
    feats = extract_features(start, end)
    X = scaler.transform([feats])
    return float(model.decision_function(X)[0])


def post_to_recovery_policy(score: float) -> None:
    payload = {"signal_type": "anomaly_risk", "score": score, "timestamp": datetime.now(timezone.utc).isoformat()}
    try:
        requests.post(RECOVERY_POLICY_URL, json=payload, timeout=5)
        print(f"  -> 신호 발행: {payload}")
    except requests.exceptions.ConnectionError:
        print(f"  -> recovery-policy 서비스 없음(Phase 7 미구현) - 신호 발행 스킵: {payload}")


def main(once: bool = False):
    model, scaler = load_model()
    consecutive_anomalous = 0
    last_signal_at = None

    while True:
        score = evaluate(model, scaler)
        is_anomalous = score < SCORE_THRESHOLD
        consecutive_anomalous = consecutive_anomalous + 1 if is_anomalous else 0
        now = time.monotonic()

        status = "이상" if is_anomalous else "정상"
        print(f"[{datetime.now(timezone.utc).isoformat()}] score={score:.4f} ({status}), 연속={consecutive_anomalous}")

        if consecutive_anomalous >= CONSECUTIVE_THRESHOLD:
            in_cooldown = last_signal_at is not None and (now - last_signal_at) < COOLDOWN_SEC
            if in_cooldown:
                print(f"  -> cooldown 중 (남은 {COOLDOWN_SEC - (now - last_signal_at):.0f}초) - 신호 스킵")
            else:
                post_to_recovery_policy(score)
                last_signal_at = now

        if once:
            return
        time.sleep(EVAL_INTERVAL_SEC)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="실시간 위험도 판단 - 주기 평가 후 신호 발행")
    parser.add_argument("--once", action="store_true", help="한 번만 평가하고 종료(테스트용)")
    args = parser.parse_args()
    main(once=args.once)
