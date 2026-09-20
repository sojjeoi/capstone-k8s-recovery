#!/usr/bin/env python3
"""analyze.py 검증 - registry_entry()/reverify_slo()가 실제 보존된
session JSON·raw CSV(§66/§77 등 이미 커밋된 고정 기록)에 대해 올바른
값을 뽑아내는지 확인하는 회귀 테스트. 이 파일들은 정책상 다시 수정되지
않는 동결 기록이라 안정적인 fixture로 쓸 수 있다."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

from analyze import registry_entry, reverify_slo


def test_registry_entry_extracts_known_violation_session():
    entry = registry_entry("anomaly-detection/v3/v31_data/sessions/calib2-low-02.json", "test")
    assert entry["session_id"] == "calib2-low-02"
    assert entry["profile"] == "low_load"
    assert entry["topology"] == "active_plus_preview"
    assert entry["stored_excluded"] is True
    assert entry["stored_t_slo"] == "2026-09-20T17:00:58.640856+00:00"
    print("OK - 알려진 위반 세션(calib2-low-02)의 registry 필드가 정확히 추출됨")


def test_registry_entry_extracts_known_pass_session():
    entry = registry_entry("anomaly-detection/v3/v31_data/sessions/v31-train-idle-20260920.json", "test")
    assert entry["profile"] == "idle"
    assert entry["stored_excluded"] is False
    assert entry["stored_t_slo"] is None
    print("OK - 알려진 PASS 세션(v31-train-idle)의 registry 필드가 정확히 추출됨")


def test_reverify_reproduces_known_violation_exactly():
    """§78.2 핵심 - 동결된 slo_judge를 원본 raw CSV에 독립 재적용해도
    §77에서 기록한 t_slo와 정확히 일치해야 한다(과거 timestamp ordering/
    small-sample 버그가 재발하지 않았음을 확인)."""
    entry = registry_entry("anomaly-detection/v3/v31_data/sessions/calib2-low-02.json", "test")
    rv = reverify_slo(entry)
    if rv["reverify_status"] != "ok":
        import pytest
        pytest.skip("raw CSV가 로컬에 없음(gitignore 대상) - 커밋된 세션 JSON만으로는 재검증 불가한 환경")
    assert rv["matches_stored"] is True
    assert rv["recomputed_t_slo"] == entry["stored_t_slo"]
    print("OK - 독립 재검증한 t_slo가 원본 기록과 정확히 일치")


def test_reverify_reproduces_known_pass_exactly():
    entry = registry_entry("anomaly-detection/v3/v31_data/sessions/v31-train-idle-20260920.json", "test")
    rv = reverify_slo(entry)
    if rv["reverify_status"] != "ok":
        import pytest
        pytest.skip("raw CSV가 로컬에 없음(gitignore 대상)")
    assert rv["matches_stored"] is True
    assert rv["recomputed_t_slo"] is None
    print("OK - 독립 재검증에서도 위반 없음(None)으로 원본과 일치")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        t()
    print(f"전체 통과 ({len(tests)}개)")
