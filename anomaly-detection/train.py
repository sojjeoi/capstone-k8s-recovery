#!/usr/bin/env python3
"""정상 상태 구간(data/regimes.jsonl)의 특성 벡터로 Isolation Forest를
학습한다. scaler는 이 학습 데이터에만 fit한다(평가 데이터 유출 방지 -
guideline.md Phase 6 원칙).
# ponytail: 정상 구간 표본이 7개뿐 - Phase 6 본 구현에서 정상 데이터
# 다양성/개수를 늘려야 한다(guideline.md 9-4절). 지금은 "정상/이상 점수가
# 갈리는지" 최소 확인이 목적.
"""
import json
import pickle
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # Windows 기본 cp949 콘솔 대응

import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

from features import FEATURE_NAMES, extract_features

REGIMES_FILE = Path(__file__).parent / "data" / "regimes.jsonl"
ARTIFACTS_DIR = Path(__file__).parent / "artifacts"


def load_regimes() -> list:
    with REGIMES_FILE.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def build_training_matrix() -> tuple:
    rows, labels = [], []
    for r in load_regimes():
        start = datetime.fromisoformat(r["start_utc"])
        end = datetime.fromisoformat(r["end_utc"])
        feats = extract_features(start, end)
        print(f"{r['regime']}: {dict(zip(FEATURE_NAMES, (round(v, 4) for v in feats)))}")
        rows.append(feats)
        labels.append(r["regime"])
    return np.array(rows), labels


def main():
    X, labels = build_training_matrix()

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    model = IsolationForest(n_estimators=100, contamination="auto", random_state=42)
    model.fit(X_scaled)

    ARTIFACTS_DIR.mkdir(exist_ok=True)
    with (ARTIFACTS_DIR / "model.pkl").open("wb") as f:
        pickle.dump(model, f)
    with (ARTIFACTS_DIR / "scaler.pkl").open("wb") as f:
        pickle.dump(scaler, f)

    scores = model.decision_function(X_scaled)
    print("\n=== 학습 데이터 자체 점수 (전부 정상이니 참고용) ===")
    for label, score in zip(labels, scores):
        print(f"  {label}: {score:.4f}")

    print(f"\n저장: {ARTIFACTS_DIR / 'model.pkl'}, {ARTIFACTS_DIR / 'scaler.pkl'}")


if __name__ == "__main__":
    main()
