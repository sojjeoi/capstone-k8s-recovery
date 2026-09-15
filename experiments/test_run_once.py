#!/usr/bin/env python3
"""run_once.py의 상태머신을 검증 - 실제 chaos 없이 가짜 Injector/Prober로
정상종료/timeout/예외/probe미준비 네 경로를 확인한다(experiment-contract.md
3단계 완료기준 1~7). run_id 등록은 arm="native"일 때 건너뛰므로 대부분은
오프라인으로 돈다 - non-native arm 등록 하나만 실제 recovery-policy에 HTTP로
붙어서(port-forward 필요) 실제 연동을 확인한다."""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from run_once import Injector, Prober, run_once


def _fake_injector(is_done_after_calls=1):
    calls = {"is_done": 0, "cleanup": 0, "inject": 0}

    def inject():
        calls["inject"] += 1

    def is_done():
        calls["is_done"] += 1
        return calls["is_done"] >= is_done_after_calls

    def cleanup():
        calls["cleanup"] += 1

    return Injector(inject=inject, is_done=is_done, cleanup=cleanup), calls


def _fake_prober(healthy=True, recovers_after_calls=1):
    calls = {"start": 0, "is_healthy": 0, "check_recovered": 0, "stop": 0}

    def start():
        calls["start"] += 1

    def is_healthy():
        calls["is_healthy"] += 1
        return healthy

    def check_recovered():
        calls["check_recovered"] += 1
        return calls["check_recovered"] >= recovers_after_calls

    def stop():
        calls["stop"] += 1

    return Prober(start=start, is_healthy=is_healthy, check_recovered=check_recovered, stop=stop), calls


def test_normal_completion():
    injector, icalls = _fake_injector(is_done_after_calls=1)
    prober, pcalls = _fake_prober(recovers_after_calls=1)

    result = run_once(
        scenario="dry_run", arm="native", rep=1, sequence_index=1, order_seed=42,
        injector=injector, prober=prober, timeout_sec=10, poll_interval_sec=0.1,
    )

    assert result.outcome == "recovered", result.outcome
    assert result.probe_valid is True
    assert result.injection_valid is True
    assert result.t_injection is not None
    assert result.t_injection_end is not None
    assert result.t_recovery is not None
    assert result.t_run_end is not None
    assert pcalls["start"] == 1
    assert pcalls["stop"] == 1
    assert icalls["cleanup"] == 1
    print("OK - 정상 완료:", result.run_id, result.outcome)


def test_timeout():
    injector, icalls = _fake_injector(is_done_after_calls=1)
    prober, pcalls = _fake_prober(recovers_after_calls=10_000)  # 절대 회복 안 되게

    result = run_once(
        scenario="dry_run", arm="native", rep=2, sequence_index=2, order_seed=42,
        injector=injector, prober=prober, timeout_sec=1, poll_interval_sec=0.2,
    )

    assert result.outcome == "timeout", result.outcome
    assert result.t_recovery is None
    assert pcalls["stop"] == 1
    assert icalls["cleanup"] == 1
    print("OK - timeout:", result.run_id, result.outcome)


def test_exception_still_cleans_up_and_marks_invalid():
    def raising_inject():
        raise RuntimeError("의도적으로 터뜨린 예외 - injector.inject() 실패 시나리오")

    icalls = {"cleanup": 0}

    def cleanup():
        icalls["cleanup"] += 1

    injector = Injector(inject=raising_inject, is_done=lambda: True, cleanup=cleanup)
    prober, pcalls = _fake_prober()

    result = run_once(
        scenario="dry_run", arm="native", rep=3, sequence_index=3, order_seed=42,
        injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.1,
    )

    assert result.outcome == "invalid_run", result.outcome
    assert "예외" in result.invalid_reason
    assert pcalls["stop"] == 1, "예외가 나도 prober.stop()은 호출돼야 함"
    assert icalls["cleanup"] == 1, "예외가 나도 injector.cleanup()은 호출돼야 함"
    print("OK - 예외 발생해도 정리 + invalid_run 기록:", result.invalid_reason)


def test_probe_never_ready_marks_invalid():
    injector, icalls = _fake_injector()
    prober, pcalls = _fake_prober(healthy=False)

    result = run_once(
        scenario="dry_run", arm="native", rep=4, sequence_index=4, order_seed=42,
        injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.1,
        probe_ready_timeout_sec=1,
    )

    assert result.outcome == "invalid_run"
    assert result.probe_valid is False
    assert icalls["inject"] == 0, "probe가 준비 안 됐으면 주입 자체를 시도하면 안 됨"
    print("OK - probe 미준비 -> invalid_run, 주입 시도 안 함")


def test_real_experiment_context_registration_non_native_arm():
    """native가 아닌 arm은 실제 recovery-policy에 등록/clear HTTP 호출이
    나간다 - 로컬에서 kubectl port-forward -n vllm-serving svc/recovery-policy
    8080:8080 켜둔 상태에서만 통과."""
    injector, _ = _fake_injector()
    prober, _ = _fake_prober()

    result = run_once(
        scenario="dry_run", arm="fixed_threshold", rep=1, sequence_index=5, order_seed=42,
        injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.1,
    )
    assert result.outcome == "recovered"
    assert "clear 실패" not in result.notes, result.notes
    print("OK - non-native arm의 실제 experiment-run 등록/clear 성공:", result.run_id)


if __name__ == "__main__":
    test_normal_completion()
    test_timeout()
    test_exception_still_cleans_up_and_marks_invalid()
    test_probe_never_ready_marks_invalid()
    test_real_experiment_context_registration_non_native_arm()
    print("모두 통과")
