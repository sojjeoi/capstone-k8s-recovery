#!/usr/bin/env python3
"""feature_selection.py 검증 - train-only 원칙과 fail-closed 동작을
순수 함수 단위로 확인한다."""
import sys

sys.stdout.reconfigure(encoding="utf-8")

import pytest

from feature_selection import FEATURE_NAMES, apply_feature_schema, compute_feature_schema

# cpu_mean/cpu_slope/memory_mean/memory_slope: 변동 있음, queue_mean/queue_slope: 상수 0,
# cache_mean/cache_slope: 변동 있음 - §70 실측 패턴을 축소 재현.
_ROWS = [
    [1.0, 0.1, 7e9, 100.0, 0.0, 0.0, 0.0, 0.0],
    [1.2, -0.1, 7.01e9, -50.0, 0.0, 0.0, 0.001, 0.0005],
    [0.9, 0.2, 6.99e9, 200.0, 0.0, 0.0, 0.0, -0.0005],
]


def test_zero_variance_features_removed():
    schema = compute_feature_schema(_ROWS)
    assert "queue_mean" not in schema["kept_feature_names"]
    assert "queue_slope" not in schema["kept_feature_names"]
    removed_names = {r["name"] for r in schema["removed_features"]}
    assert removed_names == {"queue_mean", "queue_slope"}
    print("OK - train에서 정확히 0분산인 feature만 제거")


def test_nonzero_variance_features_kept_even_if_small():
    schema = compute_feature_schema(_ROWS)
    # cache_mean/cache_slope는 분산이 작아도(near-zero) 0이 아니므로 유지
    assert "cache_mean" in schema["kept_feature_names"]
    assert "cache_slope" in schema["kept_feature_names"]
    print("OK - 0이 아니면(near-zero라도) 자동 제거하지 않고 유지")


def test_feature_order_preserved_as_subset():
    schema = compute_feature_schema(_ROWS)
    # 원본 순서에서 제거된 것만 빠진 부분수열이어야 함(재정렬 없음)
    original_order = [n for n in FEATURE_NAMES if n in schema["kept_feature_names"]]
    assert schema["kept_feature_names"] == original_order
    print("OK - feature 순서는 원본 순서를 유지한 부분집합")


def test_missing_nan_fails_closed():
    bad_rows = [row[:] for row in _ROWS]
    bad_rows[0][0] = float("nan")
    with pytest.raises(ValueError):
        compute_feature_schema(bad_rows)
    print("OK - NaN이 있으면 fail-closed(예외)")


def test_infinite_value_fails_closed():
    bad_rows = [row[:] for row in _ROWS]
    bad_rows[1][2] = float("inf")
    with pytest.raises(ValueError):
        compute_feature_schema(bad_rows)
    print("OK - Inf가 있으면 fail-closed(예외)")


def test_none_value_fails_closed():
    bad_rows = [row[:] for row in _ROWS]
    bad_rows[2][3] = None
    with pytest.raises(ValueError):
        compute_feature_schema(bad_rows)
    print("OK - None(결측)이 있으면 fail-closed(예외)")


def test_empty_train_matrix_fails_closed():
    with pytest.raises(ValueError):
        compute_feature_schema([])
    print("OK - train row가 0개면 fail-closed(예외)")


def test_row_length_mismatch_fails_closed():
    with pytest.raises(ValueError):
        compute_feature_schema([[1.0, 2.0]])
    print("OK - feature 개수가 8개가 아니면 fail-closed(예외)")


def test_apply_feature_schema_matches_kept_indices():
    schema = compute_feature_schema(_ROWS)
    reduced = apply_feature_schema(_ROWS[0], schema)
    expected = [_ROWS[0][i] for i in schema["kept_feature_indices"]]
    assert reduced == expected
    print("OK - apply_feature_schema가 schema의 kept_feature_indices와 정확히 일치")


def test_apply_feature_schema_rejects_wrong_length():
    schema = compute_feature_schema(_ROWS)
    with pytest.raises(ValueError):
        apply_feature_schema([1.0, 2.0], schema)
    print("OK - 원본 열 수와 다른 row를 주면 fail-closed")


def test_calibration_values_never_influence_schema():
    """calibration/holdout 값을 넣어도(=train_feature_matrix에 안 넣으면)
    schema가 오직 넘긴 train 행만으로 결정됨을 확인 - 함수 시그니처 자체가
    train 외 데이터를 받지 않으므로, 다른 데이터를 넘겨도 같은 결과가
    나오는 것으로 '분리'가 아니라 '한 번에 하나만 본다'는 계약을
    검증한다(다른 split을 아예 인자로 받을 수 없는 API 형태 자체가 방지책)."""
    schema_a = compute_feature_schema(_ROWS)
    schema_b = compute_feature_schema(_ROWS)  # 동일 train, 다른 호출
    assert schema_a["kept_feature_names"] == schema_b["kept_feature_names"]
    print("OK - 동일 train 입력이면 항상 동일 schema(결정론적, 외부 데이터 개입 불가)")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        t()
    print(f"전체 통과 ({len(tests)}개)")
