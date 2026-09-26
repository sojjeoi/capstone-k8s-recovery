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
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import fixed_threshold

SCRIPT_PATH = Path(__file__).parent / "fixed_threshold.py"

_FAKE_VERBOSE = {
    "window_start_utc": "2026-01-01T00:00:00+00:00",
    "window_end_utc": "2026-01-01T00:01:00+00:00",
    "raw_feature_vector": [0.1, 0.0, 1e9, 0.0, 0.0, 0.0, 0.0, 0.0],
    "cpu_mean": 0.1,  # 임계치(2.7코어)보다 한참 낮음 - 신호 발행 경로를 안 타게 해 테스트를 단순하게 유지
}


def _read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


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
    # evaluate_verbose()를 대체해 Prometheus 없이도 시작 로그(실제 적용값)가
    # 정확히 찍히는지 확인한다 - "시작 로그와 결과 근거에 실제 적용값 출력"
    # 요구사항. §160부터 main()이 evaluate() 대신 evaluate_verbose()를
    # 직접 호출하므로(evaluate()는 그 cpu_mean만 뽑는 얇은 래퍼) 패치
    # 대상도 그에 맞춰 옮겼다 - 판정에 쓰는 값(cpu_mean=0.0)은 그대로다.
    buf = io.StringIO()
    with patch.object(fixed_threshold, "evaluate_verbose", return_value={
             "window_start_utc": "2026-01-01T00:00:00+00:00",
             "window_end_utc": "2026-01-01T00:01:00+00:00",
             "raw_feature_vector": [0.0] * 8, "cpu_mean": 0.0}), \
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


def _run_main_n_cycles(evidence_path, n):
    """§160 - main()을 once=False로 n cycle만 돌리고 멈추게 하는 테스트 헬퍼
    (test_score_server_v32b.py의 _run_main_n_cycles와 동일 패턴 - sleep
    무력화, n번째 cycle 뒤 StopIteration으로 빠져나옴). 판정 로직은 건드리지
    않고 IO 계층(evaluate_verbose/time.sleep)만 가짜로 대체한다."""
    calls = {"n": 0}

    def fake_sleep(_):
        calls["n"] += 1
        if calls["n"] >= n:
            raise StopIteration("test: n cycles reached")

    with patch.object(fixed_threshold, "evaluate_verbose", return_value=_FAKE_VERBOSE), \
         patch.object(fixed_threshold.time, "sleep", side_effect=fake_sleep):
        try:
            fixed_threshold.main(cpu_limit_cores=3.0, once=False, experiment_run_id="test-run",
                                  evidence_log_path=str(evidence_path))
        except StopIteration:
            pass


def test_evidence_log_records_every_cycle_no_drops():
    # 지시 - "로깅 누락을 잡는 회귀 테스트". n cycle을 돌리면 evaluation_decision도
    # 정확히 n건, evaluation_seq도 빠짐없이 연속이어야 한다.
    with tempfile.TemporaryDirectory() as d:
        evidence_path = Path(d) / "evidence.jsonl"
        _run_main_n_cycles(evidence_path, 5)
        decisions = [r for r in _read_jsonl(evidence_path) if r["record_type"] == "evaluation_decision"]
        assert len(decisions) == 5, f"5 cycle을 돌렸는데 evaluation_decision {len(decisions)}건 - 누락 있음"
        assert [rec["evaluation_seq"] for rec in decisions] == [1, 2, 3, 4, 5], \
            "evaluation_seq가 연속이 아님 - 일부 cycle이 기록 안 되고 스킵됐을 가능성"
        assert len({rec["correlation_id"] for rec in decisions}) == 5, "correlation_id가 중복됨"
    print("OK - 5 cycle 전부 누락 없이 기록, evaluation_seq 연속·correlation_id 고유")


def test_evidence_log_window_matches_evaluate_verbose_exactly():
    # 지시 - "타임스탬프 오류를 잡는 회귀 테스트". 기록된 window 시각이
    # evaluate_verbose()가 실제로 반환한 값과 한 글자도 다르면 안 된다
    # (다른 경로에서 별도로 datetime.now()를 다시 불러 재계산했다는 뜻).
    with tempfile.TemporaryDirectory() as d:
        evidence_path = Path(d) / "evidence.jsonl"
        with patch.object(fixed_threshold, "evaluate_verbose", return_value=_FAKE_VERBOSE):
            fixed_threshold.main(cpu_limit_cores=3.0, once=True, experiment_run_id="test-run",
                                  evidence_log_path=str(evidence_path))
        records = _read_jsonl(evidence_path)
        assert len(records) == 1
        assert records[0]["window_start_utc"] == _FAKE_VERBOSE["window_start_utc"]
        assert records[0]["window_end_utc"] == _FAKE_VERBOSE["window_end_utc"]
    print("OK - 기록된 window 시각이 evaluate_verbose() 반환값과 정확히 일치(재계산 없음)")


def test_evaluate_verbose_window_length_matches_window_sec():
    # extract_features()만 가짜로 대체하고(실제 evaluate_verbose() 로직은
    # 그대로 실행) window_end-window_start가 WINDOW_SEC와 정확히 같은지
    # 확인한다 - off-by-one류 시각 계산 오류를 잡는다.
    with patch.object(fixed_threshold, "extract_features", return_value=[0.0] * 8):
        verbose = fixed_threshold.evaluate_verbose()
    start = datetime.fromisoformat(verbose["window_start_utc"])
    end = datetime.fromisoformat(verbose["window_end_utc"])
    assert (end - start) == timedelta(seconds=fixed_threshold.WINDOW_SEC), \
        f"window 길이가 WINDOW_SEC({fixed_threshold.WINDOW_SEC}초)와 다름: {end - start}"
    print(f"OK - evaluate_verbose() window 길이가 WINDOW_SEC({fixed_threshold.WINDOW_SEC}초)와 정확히 일치")


def test_evidence_log_schema_is_fixed_and_has_no_env_or_secret_leakage():
    # 지시 - "환경변수·자격증명·요청 본문이 기록되지 않는지 확인". 가짜
    # 비밀값을 환경변수에 심고 로그 텍스트 전체 어디에도 없는지, 그리고
    # 기록된 키가 의도한 고정 집합과 정확히 같은지(os.environ 등을 통째로
    # 덤프하는 사고를 키 집합 불일치로 잡는다) 확인한다.
    fake_secret = "sk-should-never-leak-9f3ac21b"
    with tempfile.TemporaryDirectory() as d, \
         patch.dict(os.environ, {"FAKE_TEST_SECRET_TOKEN": fake_secret}), \
         patch.object(fixed_threshold, "evaluate_verbose", return_value=_FAKE_VERBOSE):
        evidence_path = Path(d) / "evidence.jsonl"
        fixed_threshold.main(cpu_limit_cores=3.0, once=True, experiment_run_id="test-run",
                              evidence_log_path=str(evidence_path))
        raw_text = evidence_path.read_text(encoding="utf-8")
        assert fake_secret not in raw_text, "환경변수 값(가짜 비밀)이 evidence 로그에 그대로 노출됨"
        records = _read_jsonl(evidence_path)
        assert len(records) == 1
        expected_keys = {
            "record_type", "detector", "wall_clock_utc", "run_id", "evaluation_seq",
            "correlation_id", "window_start_utc", "window_end_utc", "raw_feature_vector",
            "cpu_mean", "threshold_cores", "is_anomalous", "consecutive_anomalous",
            "cooldown_active", "would_signal",
        }
        assert set(records[0].keys()) == expected_keys, \
            f"evidence 레코드 키가 고정 스키마와 다름(뜻밖의 필드 유입 의심): {set(records[0].keys())}"
    print("OK - 환경변수 유출 없음, 레코드 스키마가 고정 키 집합과 정확히 일치(요청 본문·자격증명 필드 없음)")


def test_evidence_logging_does_not_change_signal_decision():
    # 핵심 회귀 - evidence_log_path 유무가 "언제 신호를 보낼지"에 영향을
    # 주면 안 된다(지시: 기존 탐지·정책·승격 동작 변경 금지). 3회 연속
    # 이상(cpu_mean이 임계치를 넘음)일 때 evidence_log_path가 있을 때와
    # 없을 때 post_to_recovery_policy 호출 횟수·시점이 동일해야 한다.
    anomalous_verbose = {**_FAKE_VERBOSE, "cpu_mean": 2.8}  # 임계치(2.7코어) 초과
    calls_without_log = []
    calls_with_log = []

    def make_fake_sleep():
        calls = {"n": 0}

        def fake_sleep(_):
            calls["n"] += 1
            if calls["n"] >= 3:
                raise StopIteration("test: 3 cycles reached")
        return fake_sleep

    with patch.object(fixed_threshold, "evaluate_verbose", return_value=anomalous_verbose), \
         patch.object(fixed_threshold, "post_to_recovery_policy",
                       side_effect=lambda *a, **k: calls_without_log.append((a, k))), \
         patch.object(fixed_threshold.time, "sleep", side_effect=make_fake_sleep()):
        try:
            fixed_threshold.main(cpu_limit_cores=3.0, once=False, experiment_run_id="test-run")
        except StopIteration:
            pass

    with tempfile.TemporaryDirectory() as d:
        evidence_path = Path(d) / "evidence.jsonl"
        with patch.object(fixed_threshold, "evaluate_verbose", return_value=anomalous_verbose), \
             patch.object(fixed_threshold, "post_to_recovery_policy",
                           side_effect=lambda *a, **k: calls_with_log.append((a, k))), \
             patch.object(fixed_threshold.time, "sleep", side_effect=make_fake_sleep()):
            try:
                fixed_threshold.main(cpu_limit_cores=3.0, once=False, experiment_run_id="test-run",
                                      evidence_log_path=str(evidence_path))
            except StopIteration:
                pass

    assert len(calls_without_log) == len(calls_with_log) == 1, \
        f"evidence_log_path 유무에 따라 신호 발행 횟수가 다름: 없음={len(calls_without_log)}, 있음={len(calls_with_log)}"
    assert calls_without_log[0][0] == calls_with_log[0][0], "evidence_log_path 유무에 따라 신호 발행 인자가 다름"
    print("OK - evidence_log_path 유무와 무관하게 신호 발행 횟수·인자가 완전히 동일(판정 로직 불변 확인)")


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
    test_evidence_log_records_every_cycle_no_drops()
    test_evidence_log_window_matches_evaluate_verbose_exactly()
    test_evaluate_verbose_window_length_matches_window_sec()
    test_evidence_log_schema_is_fixed_and_has_no_env_or_secret_leakage()
    test_evidence_logging_does_not_change_signal_decision()
    print("전체 통과")


if __name__ == "__main__":
    main()
