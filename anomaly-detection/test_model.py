#!/usr/bin/env python3
"""최소 모델 체크 — train.py 산출물이 실제로 정상/이상을 갈라내는지 확인.

Phase 5 발견 5(네트워크 열화 붕괴, 2026-09-05 22:35~22:41 KST, 성공률 11%)
구간을 알려진 이상 사례로 써서 검증한다. guideline.md Phase 6 산출물
기준("정상/이상 상황에서 이상 점수가 유의미하게 갈리는 것 확인")의 최소
재현 체크.
"""
import pickle
import sys
from datetime import datetime
from pathlib import Path

from features import extract_features

sys.stdout.reconfigure(encoding="utf-8")  # Windows 기본 cp949 콘솔 대응

ARTIFACTS_DIR = Path(__file__).parent / "artifacts"
# 이 raw CSV는 UTC 적용 전(구버전 ramp.py)이라 타임스탬프가 KST(UTC+9) -
# UTC로 환산한 값.
KNOWN_ANOMALY_START = "2026-09-05T13:35:02+00:00"
KNOWN_ANOMALY_END = "2026-09-05T13:41:31+00:00"


def main():
    with (ARTIFACTS_DIR / "model.pkl").open("rb") as f:
        model = pickle.load(f)
    with (ARTIFACTS_DIR / "scaler.pkl").open("rb") as f:
        scaler = pickle.load(f)

    feats = extract_features(
        datetime.fromisoformat(KNOWN_ANOMALY_START), datetime.fromisoformat(KNOWN_ANOMALY_END)
    )
    X = scaler.transform([feats])
    score = model.decision_function(X)[0]
    pred = model.predict(X)[0]

    print(f"known-anomaly score: {score:.4f}, predict: {pred} (-1=이상, 1=정상)")
    assert pred == -1, "알려진 이상 구간(네트워크 열화 붕괴)을 정상으로 오판함"
    print("OK - 알려진 이상 구간을 정상 학습 데이터와 구분해냄")


if __name__ == "__main__":
    main()
