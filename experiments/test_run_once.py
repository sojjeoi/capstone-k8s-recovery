#!/usr/bin/env python3
"""run_once.py의 상태머신을 검증 - 실제 chaos 없이 가짜 Injector/Prober로
검증한다(experiment-contract.md 3단계 완료기준 + 1·2차 리뷰에서 지적된
문제들의 회귀 테스트). run_id 등록/quiescence 확인은 arm="native"일 때
건너뛰므로 대부분은 오프라인으로 돈다 - non-native arm은 실제
recovery-policy에 HTTP로 붙어서(port-forward 필요) 실제 연동을 확인한다.

모든 테스트는 run_once(results_dir=...)로 결과 기록 위치를 격리한다(2026-
09-16 수정) - 예전엔 run_once.py의 RESULTS_DIR(실제 results/)에 그대로
썼는데, pytest로 돌리면 __main__ 전용이던 _clean_previous_results()가 안
불려서 dry_run-* 산출물이 여러 날짜에 걸쳐 계속 쌓였다(collect_metrics.py
실측으로 발견). pytest로 돌리면 각 테스트가 고유한 tmp_path를 자동으로
받고, python test_run_once.py로 직접 돌리면 __main__ 블록이 매 테스트마다
tempfile.TemporaryDirectory()로 새로 만들어 넘긴다 - 어느 경로로 실행하든
실제 results/를 건드리지 않고, 정리도 OS가 보장한다."""
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")

import requests

from run_once import RECOVERY_POLICY_URL, HarnessCorrupted, Injector, Prober, run_once


def _fake_injector(is_done_after_calls=1, is_started_after_calls=1, effective=True):
    calls = {"prepare": 0, "inject": 0, "is_started": 0, "is_effective": 0, "is_done": 0, "cleanup": 0}

    def prepare():
        calls["prepare"] += 1

    def inject():
        calls["inject"] += 1

    def is_started():
        calls["is_started"] += 1
        return calls["is_started"] >= is_started_after_calls

    def is_effective():
        calls["is_effective"] += 1
        return effective

    def is_done():
        calls["is_done"] += 1
        return calls["is_done"] >= is_done_after_calls

    def cleanup():
        calls["cleanup"] += 1

    return Injector(prepare=prepare, inject=inject, is_started=is_started,
                     is_effective=is_effective, is_done=is_done, cleanup=cleanup), calls


def _fake_prober(alive=True, violates_after_calls=1, recovers_after_slo_calls=1, stops_cleanly=True):
    """violates_after_calls: check_slo_violation() 몇 번째 호출부터 위반으로
    볼지. recovers_after_slo_calls: t_slo가 찍힌 뒤(!) check_recovered() 몇
    번째 호출부터 True를 낼지 - t_slo 이전엔 애초에 안 불리는 걸 run_once()가
    보장해야 하므로, 이 카운터는 오직 t_slo 이후 호출에만 반응한다.
    stops_cleanly=True(기본)면 stop() 호출 이후 is_alive()가 False로
    바뀐다(실제 정상 종료를 흉내) - False로 주면 stop()을 불러도 안 죽는
    prober를 흉내낼 수 있다(2차 리뷰의 "stop 이후에도 살아있음" 시나리오용)."""
    calls = {"start": 0, "is_alive": 0, "check_slo_violation": 0, "check_recovered": 0, "stop": 0}
    counters = {"slo": 0, "recovered": 0}
    state = {"stopped": False}

    def start():
        calls["start"] += 1

    def is_alive():
        calls["is_alive"] += 1
        if state["stopped"]:
            return False
        return alive

    def check_slo_violation():
        calls["check_slo_violation"] += 1
        counters["slo"] += 1
        return counters["slo"] >= violates_after_calls

    def check_recovered():
        calls["check_recovered"] += 1
        counters["recovered"] += 1
        return counters["recovered"] >= recovers_after_slo_calls

    def stop():
        calls["stop"] += 1
        if stops_cleanly:
            state["stopped"] = True

    return Prober(start=start, is_alive=is_alive, check_slo_violation=check_slo_violation,
                   check_recovered=check_recovered, stop=stop), calls


def test_normal_completion(tmp_path):
    injector, icalls = _fake_injector(is_done_after_calls=1)
    prober, pcalls = _fake_prober(violates_after_calls=1, recovers_after_slo_calls=1)

    result = run_once(
        scenario="dry_run", arm="native", rep=1, sequence_index=1, order_seed=42,
        injector=injector, prober=prober, timeout_sec=10, poll_interval_sec=0.1,
        results_dir=tmp_path,
    )

    assert result.outcome == "recovered", result.outcome
    assert result.state == "completed"
    assert result.probe_valid is True
    assert result.injection_valid is True
    assert result.t_injection is not None
    assert result.t_injection_end is not None
    assert result.t_slo is not None
    assert result.t_recovery is not None
    assert result.t_run_end is not None
    assert pcalls["start"] == 1
    assert pcalls["stop"] == 1
    assert icalls["prepare"] == 1
    assert icalls["cleanup"] == 1
    print("OK - 정상 완료:", result.run_id, result.outcome, result.state)


def test_pilot_result_written_to_pilot_subdir(tmp_path):
    # 2026-09-16 지적: PILOT-EXCLUDED를 notes 자유 텍스트에만 넣으면
    # collect_metrics.py가 실수로 포함할 수 있다 - is_pilot=True는 results/
    # 바로 아래가 아니라 results/pilot/ 아래에 쓰여서 구조적으로 분리돼야 한다.
    injector, _ = _fake_injector(is_done_after_calls=1)
    prober, _ = _fake_prober(violates_after_calls=1, recovers_after_slo_calls=1)

    result = run_once(
        scenario="dry_run", arm="native", rep=1, sequence_index=1, order_seed=42,
        injector=injector, prober=prober, timeout_sec=10, poll_interval_sec=0.1,
        is_pilot=True, results_dir=tmp_path,
    )

    assert result.is_pilot is True
    pilot_path = tmp_path / "pilot" / f"trial-{result.run_id}.json"
    main_path = tmp_path / f"trial-{result.run_id}.json"
    assert pilot_path.exists(), f"파일럿 결과가 {pilot_path}에 없음"
    assert not main_path.exists(), "파일럿 결과가 본 실험 경로에도 써지면 안 됨"
    written = json.loads(pilot_path.read_text(encoding="utf-8"))
    assert written["is_pilot"] is True
    print("OK - is_pilot=True는 results/pilot/ 아래 구조적으로 분리돼 기록됨")


def test_slo_violation_gates_recovery_check(tmp_path):
    # 1차 리뷰 지적 회귀 테스트: 주입 직후 아직 멀쩡한 구간에서
    # check_recovered()가 호출되면 안 된다(t_slo 찍히기 전엔 아예 안 물어봄).
    injector, _ = _fake_injector(is_done_after_calls=10)
    prober, pcalls = _fake_prober(violates_after_calls=3, recovers_after_slo_calls=2)

    result = run_once(
        scenario="dry_run", arm="native", rep=10, sequence_index=10, order_seed=1,
        injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.02,
        results_dir=tmp_path,
    )

    assert result.outcome == "recovered", result.outcome
    assert result.t_slo is not None and result.t_recovery is not None
    assert result.t_slo <= result.t_recovery
    assert pcalls["check_recovered"] < pcalls["check_slo_violation"], \
        "check_recovered는 t_slo 찍히기 전엔 호출되면 안 됨"
    print("OK - t_slo 이전엔 check_recovered() 미호출, t_slo<=t_recovery 순서 보장")


def test_prevented_when_never_violates(tmp_path):
    # 1차 리뷰 지적 회귀 테스트: 끝까지 SLO 위반이 없으면 recovered가 아니라
    # prevented여야 하고, check_recovered()는 아예 호출되면 안 된다.
    injector, _ = _fake_injector(is_done_after_calls=2)
    prober, pcalls = _fake_prober(violates_after_calls=10_000)  # 절대 위반 안 되게

    result = run_once(
        scenario="dry_run", arm="native", rep=11, sequence_index=11, order_seed=1,
        injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.05,
        results_dir=tmp_path,
    )

    assert result.outcome == "prevented", result.outcome
    assert result.state == "completed"
    assert result.t_slo is None
    assert result.t_recovery is None
    assert pcalls["check_recovered"] == 0, "위반이 한 번도 없었으면 check_recovered는 호출되면 안 됨"
    print("OK - 끝까지 위반 없음 -> prevented, check_recovered 미호출")


def test_timeout(tmp_path):
    injector, icalls = _fake_injector(is_done_after_calls=1)
    prober, pcalls = _fake_prober(violates_after_calls=1, recovers_after_slo_calls=10_000)  # 절대 회복 안 되게

    result = run_once(
        scenario="dry_run", arm="native", rep=2, sequence_index=2, order_seed=42,
        injector=injector, prober=prober, timeout_sec=1, poll_interval_sec=0.2,
        results_dir=tmp_path,
    )

    assert result.outcome == "timeout", result.outcome
    assert result.state == "timeout"
    assert result.t_slo is not None  # 위반은 있었음(그래서 timeout이지 prevented가 아님)
    assert result.t_recovery is None
    assert pcalls["stop"] == 1
    assert icalls["cleanup"] == 1
    print("OK - timeout:", result.run_id, result.outcome, result.state)


def test_injection_not_effective_marks_invalid(tmp_path):
    injector, icalls = _fake_injector(effective=False)
    prober, pcalls = _fake_prober()

    result = run_once(
        scenario="dry_run", arm="native", rep=12, sequence_index=12, order_seed=1,
        injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.1,
        results_dir=tmp_path,
    )

    assert result.outcome == "invalid_run"
    assert result.state == "invalid"
    assert result.injection_valid is False
    assert "주입" in result.invalid_reason
    print("OK - 주입 시작됐지만 효과 없음 -> invalid_run:", result.invalid_reason)


def test_exception_still_cleans_up_and_marks_invalid(tmp_path):
    def raising_inject():
        raise RuntimeError("의도적으로 터뜨린 예외 - injector.inject() 실패 시나리오")

    icalls = {"cleanup": 0}

    def cleanup():
        icalls["cleanup"] += 1

    injector = Injector(prepare=lambda: None, inject=raising_inject, is_started=lambda: True,
                         is_effective=lambda: True, is_done=lambda: True, cleanup=cleanup)
    prober, pcalls = _fake_prober()

    result = run_once(
        scenario="dry_run", arm="native", rep=3, sequence_index=3, order_seed=42,
        injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.1,
        results_dir=tmp_path,
    )

    assert result.outcome == "invalid_run", result.outcome
    assert result.state == "invalid"
    assert "예외" in result.invalid_reason
    assert pcalls["stop"] == 1, "예외가 나도 prober.stop()은 호출돼야 함"
    assert icalls["cleanup"] == 1, "예외가 나도 injector.cleanup()은 호출돼야 함"
    print("OK - 예외 발생해도 정리 + invalid_run 기록:", result.invalid_reason)


def test_probe_never_alive_marks_invalid(tmp_path):
    injector, icalls = _fake_injector()
    prober, pcalls = _fake_prober(alive=False)

    result = run_once(
        scenario="dry_run", arm="native", rep=4, sequence_index=4, order_seed=42,
        injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.1,
        probe_ready_timeout_sec=1, results_dir=tmp_path,
    )

    assert result.outcome == "invalid_run"
    assert result.state == "invalid"
    assert result.probe_valid is False
    assert icalls["inject"] == 0, "probe가 준비 안 됐으면 주입 자체를 시도하면 안 됨"
    print("OK - probe 미준비 -> invalid_run, 주입 시도 안 함")


def test_critical_cleanup_failure_raises_and_still_writes_result(tmp_path):
    # 1차 리뷰 지적: chaos 삭제(injector.cleanup) 실패처럼 다음 trial을
    # 오염시킬 수 있는 정리 실패는 notes로 끝내지 않고 예외로 전파해야 한다.
    def raising_cleanup():
        raise RuntimeError("chaos 리소스 삭제 실패 시뮬레이션")

    injector, _ = _fake_injector()
    injector.cleanup = raising_cleanup
    prober, _ = _fake_prober()

    raised = False
    try:
        run_once(
            scenario="dry_run", arm="native", rep=13, sequence_index=13, order_seed=1,
            injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.1,
            results_dir=tmp_path,
        )
    except HarnessCorrupted as e:
        raised = True
        print("  ->", e)

    assert raised, "injector.cleanup() 실패는 HarnessCorrupted로 전파돼야 함"
    result_files = sorted(tmp_path.glob("trial-dry_run-native-13-*.json"))
    assert len(result_files) == 1, "cleanup 실패해도 결과 파일은 기록돼야 함"
    written = json.loads(result_files[0].read_text(encoding="utf-8"))
    assert "cleanup" in written["notes"], written["notes"]
    print("OK - injector.cleanup() 실패 -> HarnessCorrupted 전파 + 결과 파일은 남음")


def test_prober_still_alive_after_stop_raises_harness_corrupted(tmp_path):
    # 2차 리뷰 지적: stop()이 예외 없이 반환해도 실제로 안 멈췄을 수 있다 -
    # is_alive()로 재확인해서, 여전히 살아있으면(다음 trial 오염 위험)
    # HarnessCorrupted여야 한다.
    injector, _ = _fake_injector()
    stuck_prober, pcalls = _fake_prober(stops_cleanly=False)  # stop()을 불러도 안 죽음

    raised = False
    try:
        run_once(
            scenario="dry_run", arm="native", rep=14, sequence_index=14, order_seed=1,
            injector=injector, prober=stuck_prober, timeout_sec=5, poll_interval_sec=0.1,
            results_dir=tmp_path,
        )
    except HarnessCorrupted as e:
        raised = True
        print("  ->", e)

    assert raised, "stop() 이후에도 is_alive()==True면 HarnessCorrupted여야 함"
    result_files = sorted(tmp_path.glob("trial-dry_run-native-14-*.json"))
    written = json.loads(result_files[0].read_text(encoding="utf-8"))
    assert "여전히 살아있음" in written["notes"], written["notes"]
    print("OK - prober.stop() 이후에도 is_alive()==True -> HarnessCorrupted")


def test_real_experiment_context_registration_non_native_arm(tmp_path):
    """native가 아닌 arm은 실제 recovery-policy에 quiescence 확인 +
    등록/clear HTTP 호출이 나간다 - 로컬에서
    kubectl port-forward -n vllm-serving svc/recovery-policy 8080:8080
    켜둔 상태(및 GET /admin/quiescent가 배포된 상태)에서만 통과."""
    injector, _ = _fake_injector()
    prober, _ = _fake_prober()

    result = run_once(
        scenario="dry_run", arm="fixed_threshold", rep=1, sequence_index=5, order_seed=42,
        injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.1,
        results_dir=tmp_path,
    )
    # prober는 가짜라 기본값(즉시 위반->즉시 회복)대로 recovered가 나옴 - 이
    # 테스트가 실제로 확인하는 건 outcome 값 자체가 아니라 quiescence 확인 +
    # experiment-run 등록/clear가 실제 recovery-policy에 에러 없이 다녀왔는지.
    assert result.outcome == "recovered", result.outcome
    assert "실패" not in result.notes, result.notes
    print("OK - non-native arm의 실제 quiescence/experiment-run 등록/clear 성공:", result.run_id)


def test_active_context_blocks_new_trial_start(tmp_path):
    """다른 trial이 미리 컨텍스트를 등록해둔 상태(오케스트레이터가 정리를
    건너뛴 버그 상황을 흉내)에서 run_once()를 부르면 즉시 invalid_run이어야
    한다 - 실제 recovery-policy에 직접 등록해두고 확인."""
    leaked_ctx = {"run_id": "leaked-from-previous-trial", "scenario": "pod_kill",
                  "arm": "native", "rep": 1, "started_at": "2026-01-01T00:00:00+00:00"}
    requests.post(f"{RECOVERY_POLICY_URL}/admin/experiment-run", json=leaked_ctx, timeout=10).raise_for_status()

    try:
        injector, icalls = _fake_injector()
        prober, _ = _fake_prober()

        result = run_once(
            scenario="dry_run", arm="fixed_threshold", rep=2, sequence_index=6, order_seed=1,
            injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.1,
            results_dir=tmp_path,
        )
        assert result.outcome == "invalid_run", result.outcome
        assert "활성 실험" in result.invalid_reason, result.invalid_reason
        assert icalls["prepare"] == 0, "활성 context 감지되면 injector.prepare()까지 가면 안 됨"
        print("OK - 활성 context 있으면 새 trial은 즉시 invalid_run:", result.invalid_reason)
    finally:
        requests.post(f"{RECOVERY_POLICY_URL}/admin/experiment-run/clear",
                       params={"run_id": "leaked-from-previous-trial"}, timeout=10)


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    def _run_with_tmp_dir(test_fn):
        with tempfile.TemporaryDirectory() as d:
            test_fn(Path(d))

    for test_fn in (
        test_normal_completion,
        test_pilot_result_written_to_pilot_subdir,
        test_slo_violation_gates_recovery_check,
        test_prevented_when_never_violates,
        test_timeout,
        test_injection_not_effective_marks_invalid,
        test_exception_still_cleans_up_and_marks_invalid,
        test_probe_never_alive_marks_invalid,
        test_critical_cleanup_failure_raises_and_still_writes_result,
        test_prober_still_alive_after_stop_raises_harness_corrupted,
        test_real_experiment_context_registration_non_native_arm,
        test_active_context_blocks_new_trial_start,
    ):
        _run_with_tmp_dir(test_fn)
    print("모두 통과")
