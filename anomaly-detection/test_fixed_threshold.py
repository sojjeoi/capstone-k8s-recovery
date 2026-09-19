#!/usr/bin/env python3
"""fixed_threshold.py의 CPU 임계치 계산·CLI 검증(2026-09-20 추가) - 코드
내부에 하드코딩돼 있던 CPU_LIMIT_CORES=4.0 상수를 제거하고 --cpu-limit-cores
필수 인자(fail-closed)로 바꾼 정정(계약서 §6, 실제 rollout.yaml CPU limit은
3코어인데 구 상수(3.6코어 임계치)로는 구조적으로 발화 불가능했던 문제)의
회귀 테스트. Prometheus·recovery-policy 없이 오프라인으로 돈다(evaluate()는
monkeypatch, CLI 테스트는 argparse 검증 단계에서 끝나 실제 평가 루프에
진입하지 않음) - test_model.py와 같은 스타일(pytest 아닌 assert 기반
self-check, `python test_fixed_threshold.py`로 직접 실행)."""
import io
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import fixed_threshold

SCRIPT_PATH = Path(__file__).parent / "fixed_threshold.py"


def test_frozen_ratio_applied_to_explicit_limit():
    # 계약서 §6 Phase 8 동결값 - CPU limit=3.0이면 threshold는 정확히 2.7이어야 한다.
    assert fixed_threshold.compute_threshold_cores(3.0) == 2.7
    print("OK - CPU limit 3.0코어 -> threshold 2.7코어(90%, 계약서 §6 동결)")


def test_rejects_zero():
    try:
        fixed_threshold.compute_threshold_cores(0)
        assert False, "0은 거부돼야 함(fail-closed)"
    except ValueError:
        pass
    print("OK - cpu_limit_cores=0 거부(fail-closed)")


def test_rejects_negative():
    try:
        fixed_threshold.compute_threshold_cores(-3.0)
        assert False, "음수는 거부돼야 함(fail-closed)"
    except ValueError:
        pass
    print("OK - 음수 cpu_limit_cores 거부(fail-closed)")


def test_rejects_none():
    try:
        fixed_threshold.compute_threshold_cores(None)
        assert False, "None은 거부돼야 함(fail-closed)"
    except ValueError:
        pass
    print("OK - cpu_limit_cores=None 거부(fail-closed)")


def test_rejects_nan_and_inf():
    for bad in (float("nan"), float("inf"), float("-inf")):
        try:
            fixed_threshold.compute_threshold_cores(bad)
            assert False, f"{bad}는 거부돼야 함(fail-closed)"
        except ValueError:
            pass
    print("OK - NaN/무한대 cpu_limit_cores 거부(fail-closed)")


def test_no_hardcoded_cpu_limit_constant_remains():
    # 정정 전 존재했던 암묵적 4코어 상수가 모듈에 남아있으면 회귀다.
    assert not hasattr(fixed_threshold, "CPU_LIMIT_CORES"), \
        "CPU_LIMIT_CORES 상수가 아직 남아있음 - 암묵적 4코어 하드코딩 제거가 안 됨"
    assert not hasattr(fixed_threshold, "CPU_THRESHOLD_CORES"), \
        "CPU_THRESHOLD_CORES 상수가 아직 남아있음 - 런타임 계산으로 바뀌어야 함"
    print("OK - 암묵적 CPU_LIMIT_CORES/CPU_THRESHOLD_CORES 상수 제거 확인")


def test_main_logs_actual_applied_values_before_looping():
    # evaluate()를 대체해 Prometheus 없이도 시작 로그(실제 적용값)가 정확히
    # 찍히는지 확인한다 - "시작 로그와 결과 근거에 실제 적용값 출력" 요구사항.
    buf = io.StringIO()
    with patch.object(fixed_threshold, "evaluate", return_value=0.0), \
         redirect_stdout(buf):
        fixed_threshold.main(cpu_limit_cores=3.0, once=True)
    out = buf.getvalue()
    assert "cpu_limit_cores=3.000" in out, out
    assert "threshold_cores=2.700" in out, out
    print("OK - 시작 로그에 실제 적용값(limit·threshold) 출력 확인")


def test_cli_missing_required_arg_fails_closed():
    r = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--once", "--run-id", "test"],
        capture_output=True, text=True, timeout=15)
    assert r.returncode != 0, "필수 인자 누락인데 정상 종료됨"
    assert "--cpu-limit-cores" in r.stderr, r.stderr
    print("OK - --cpu-limit-cores 누락 시 argparse가 즉시 종료(fail-closed, 평가 루프 진입 없음)")


def test_cli_zero_limit_fails_closed_before_evaluate():
    r = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--once", "--run-id", "test", "--cpu-limit-cores", "0"],
        capture_output=True, text=True, timeout=15)
    assert r.returncode != 0, "0 코어인데 정상 종료됨"
    assert "fail-closed" in r.stderr, r.stderr
    print("OK - --cpu-limit-cores 0 전달 시 evaluate() 호출(Prometheus 접근) 전에 fail-closed 종료")


def test_cli_negative_limit_fails_closed():
    r = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--once", "--run-id", "test", "--cpu-limit-cores", "-3.0"],
        capture_output=True, text=True, timeout=15)
    assert r.returncode != 0, "음수 코어인데 정상 종료됨"
    assert "fail-closed" in r.stderr, r.stderr
    print("OK - --cpu-limit-cores 음수 전달 시 fail-closed 종료")


def test_cli_non_numeric_limit_fails_closed():
    r = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--once", "--run-id", "test", "--cpu-limit-cores", "abc"],
        capture_output=True, text=True, timeout=15)
    assert r.returncode != 0, "숫자가 아닌데 정상 종료됨"
    print("OK - 숫자가 아닌 --cpu-limit-cores 전달 시 argparse가 즉시 종료")


def main():
    test_frozen_ratio_applied_to_explicit_limit()
    test_rejects_zero()
    test_rejects_negative()
    test_rejects_none()
    test_rejects_nan_and_inf()
    test_no_hardcoded_cpu_limit_constant_remains()
    test_main_logs_actual_applied_values_before_looping()
    test_cli_missing_required_arg_fails_closed()
    test_cli_zero_limit_fails_closed_before_evaluate()
    test_cli_negative_limit_fails_closed()
    test_cli_non_numeric_limit_fails_closed()
    print("전체 통과")


if __name__ == "__main__":
    main()
