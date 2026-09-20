#!/usr/bin/env python3
"""§72 - training-only feature 선택 규칙(사전등록). Train split의 feature
row만 입력받는다 - calibration/holdout/challenge 값은 이 함수에 절대
넣지 않는다(호출자 책임, 이 함수 자체는 무엇이 train인지 모름).

규칙(사용자 지시 그대로):
  - Train에서 정확히 zero variance인 feature는 제거
  - 결측·NaN·무한대 feature는 fail-closed(예외 발생, 계속 진행 금지)
  - near-zero variance는 자동 제거하지 않고 통계만 보고
  - 상관관계가 높다는 이유만으로는 제거하지 않음(애초에 상관관계 기반
    제거 로직 자체를 만들지 않음 - 사용자가 명시적으로 금지)
  - feature 순서는 원본 `features.FEATURE_NAMES` 순서를 그대로 유지한
    부분집합(제거된 것만 빠짐, 재정렬 없음)"""
import math

FEATURE_NAMES = ["cpu_mean", "cpu_slope", "memory_mean", "memory_slope",
                  "queue_mean", "queue_slope", "cache_mean", "cache_slope"]


def _is_bad(v) -> bool:
    return v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))


def compute_feature_schema(train_feature_matrix: list) -> dict:
    """train_feature_matrix: train split의 유효(valid=true) feature row만,
    각 원소가 `FEATURE_NAMES` 순서의 8개 float 리스트. 최소 1개 이상의
    row가 있어야 한다(빈 입력은 fail-closed)."""
    if not train_feature_matrix:
        raise ValueError("fail-closed: train feature row가 0개 - feature 선택 불가")

    n_cols = len(FEATURE_NAMES)
    for row in train_feature_matrix:
        if len(row) != n_cols:
            raise ValueError(f"fail-closed: feature row 길이가 {len(row)} - 기대값 {n_cols}과 다름")
        for v in row:
            if _is_bad(v):
                raise ValueError(f"fail-closed: train feature에 missing/NaN/Inf 값 발견: {row}")

    kept_indices, kept_names, removed = [], [], []
    stats = {}
    n = len(train_feature_matrix)
    for i, name in enumerate(FEATURE_NAMES):
        col = [row[i] for row in train_feature_matrix]
        mean = sum(col) / n
        variance = sum((v - mean) ** 2 for v in col) / n
        std = variance ** 0.5
        col_min, col_max = min(col), max(col)
        stats[name] = {"mean": mean, "std": std, "min": col_min, "max": col_max, "range": col_max - col_min}
        if std == 0.0:
            removed.append({"name": name, "reason": "zero_variance_in_train", "value": col_min})
        else:
            kept_indices.append(i)
            kept_names.append(name)

    return {
        "original_feature_names": FEATURE_NAMES,
        "kept_feature_names": kept_names,
        "kept_feature_indices": kept_indices,
        "removed_features": removed,
        "train_feature_stats": stats,  # 전체 8개 - kept/removed 모두 포함, near-zero-variance는 이 통계로 사람이 판단(자동 제거 없음)
        "n_train_rows": n,
    }


def apply_feature_schema(feature_row: list, schema: dict) -> list:
    """원본 8개 feature row에서 schema가 선택한 열만 순서 그대로 뽑는다.
    학습·calibration·holdout·challenge 전부 이 함수로 동일하게 부분집합을
    만들어야 열 순서 불일치를 방지한다."""
    if len(feature_row) != len(schema["original_feature_names"]):
        raise ValueError(f"fail-closed: feature row 길이가 schema의 원본 열 수와 다름")
    return [feature_row[i] for i in schema["kept_feature_indices"]]
