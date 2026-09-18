#!/usr/bin/env python3
"""run_once.py의 상태머신을 검증 - 실제 chaos 없이 가짜 Injector/Prober로
검증한다(experiment-contract.md 3단계 완료기준 + 1·2차 리뷰에서 지적된
문제들의 회귀 테스트). run_id 등록/quiescence 확인은 arm="native"일 때
건너뛰므로 대부분은 오프라인으로 돈다 - non-native arm은 실제
recovery-policy에 HTTP로 붙어서(port-forward 필요) 실제 연동을 확인하며,
`@pytest.mark.live_cluster`로 표시해 기본 `pytest` 실행에서는 건너뛴다
(conftest.py, RUN_LIVE_TESTS=1로만 실행 - experiments/README.md "테스트" 절).

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
import time
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")

import pytest
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


def _fake_prober(alive=True, violates_after_calls=1, recovers_after_slo_calls=1, stops_cleanly=True,
                  baseline_ready_after_calls=None):
    """violates_after_calls: check_slo_violation() 몇 번째 호출부터 위반으로
    볼지. recovers_after_slo_calls: t_slo가 찍힌 뒤(!) check_recovered() 몇
    번째 호출부터 True를 낼지 - t_slo 이전엔 애초에 안 불리는 걸 run_once()가
    보장해야 하므로, 이 카운터는 오직 t_slo 이후 호출에만 반응한다.
    stops_cleanly=True(기본)면 stop() 호출 이후 is_alive()가 False로
    바뀐다(실제 정상 종료를 흉내) - False로 주면 stop()을 불러도 안 죽는
    prober를 흉내낼 수 있다(2차 리뷰의 "stop 이후에도 살아있음" 시나리오용).
    baseline_ready_after_calls(2026-09-18 추가, BASELINE 단계 회귀 테스트용):
    None(기본)이면 get_baseline_status 자체를 구현 안 함(하위호환 - pod_kill/
    network_degrade처럼 이 훅이 없는 어댑터를 흉내) - BASELINE 단계 전체가
    건너뛰어진다. 정수를 주면 그 호출 횟수부터 ready=True를 낸다(그 전까지는
    ready=False + 진행 중 표본수/p95/가용성 스냅샷을 흉내낸 값을 반환)."""
    calls = {"start": 0, "is_alive": 0, "check_slo_violation": 0, "check_recovered": 0, "stop": 0,
             "get_baseline_status": 0}
    counters = {"slo": 0, "recovered": 0, "baseline": 0}
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

    prober_kwargs = dict(start=start, is_alive=is_alive, check_slo_violation=check_slo_violation,
                          check_recovered=check_recovered, stop=stop)

    if baseline_ready_after_calls is not None:
        def get_baseline_status():
            calls["get_baseline_status"] += 1
            counters["baseline"] += 1
            ready = counters["baseline"] >= baseline_ready_after_calls
            return {
                "ready": ready,
                "sample_count": min(20 + counters["baseline"], 60),
                "p95": 0.3,
                "availability": 1.0,
                "ready_at": "2026-01-01T00:00:05.300000+00:00" if ready else None,
            }
        prober_kwargs["get_baseline_status"] = get_baseline_status

    return Prober(**prober_kwargs), calls


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
    assert result.t_injection_request is not None
    assert result.t_injection_observed == result.t_injection
    assert result.t_injection_last_seen is None, "get_last_seen_present_time() 미구현 어댑터는 None"
    # get_actual_injection_time() 미구현이어도 이제 injection_observation_error_sec은
    # None으로 남지 않는다 - t_injection_request~observed 구간으로 항상 대체
    # 계산되기 때문(2026-09-18 정정, request/observed가 거의 동시에 찍히는
    # 가짜 injector라 0에 가까운 작은 값).
    assert result.injection_observation_error_sec is not None
    assert 0 <= result.injection_observation_error_sec < 1.0
    assert result.timing_schema_version == "v2"
    assert result.t_injection_end is not None
    assert result.t_slo is not None
    assert result.t_recovery is not None
    assert result.t_run_end is not None
    assert pcalls["start"] == 1
    assert pcalls["stop"] == 1
    assert icalls["prepare"] == 1
    assert icalls["cleanup"] == 1
    print("OK - 정상 완료:", result.run_id, result.outcome, result.state)


def test_precise_injection_time_overrides_and_records_observation_error(tmp_path):
    # get_actual_injection_time()을 구현한 어댑터(pod_kill/load_ramp)는
    # t_injection이 그 값으로 덮어써진다. 관측 오차는 어댑터가
    # get_injection_observation_error_sec()으로 실측해 넘긴 값을 그대로 쓴다 -
    # poll_interval_sec 같은 설정값을 run_once()가 대신 채우지 않는다
    # (2026-09-16 정정: 설정값은 조회 자체의 실행시간·스케줄링 지연을 반영
    # 못 해 진짜 상한이 아닐 수 있음).
    precise_time = "2026-01-01T00:00:00.123456+00:00"
    injector, _ = _fake_injector(is_done_after_calls=1)
    injector.get_actual_injection_time = lambda: precise_time
    injector.get_injection_observation_error_sec = lambda: 0.37  # 어댑터가 실측한 값(설정 poll_interval_sec=0.1과 다름을 의도적으로 확인)
    prober, _ = _fake_prober(violates_after_calls=1, recovers_after_slo_calls=1)

    result = run_once(
        scenario="dry_run", arm="native", rep=15, sequence_index=15, order_seed=1,
        injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.1,
        results_dir=tmp_path,
    )

    assert result.t_injection == precise_time
    assert result.injection_observation_error_sec == 0.37, \
        "poll_interval_sec(0.1)이 아니라 어댑터가 실측한 값을 그대로 써야 함"
    print("OK - t_injection 덮어쓰기 + 어댑터 실측 관측 오차 기록(설정 poll_interval_sec과 무관)")


def test_precise_injection_time_without_error_hook_falls_back_to_request_span(tmp_path):
    # get_actual_injection_time()만 구현하고 get_injection_observation_error_sec()은
    # 없는 어댑터 - run_once()는 이제 poll_interval_sec(임의 설정값)이 아니라
    # t_injection_request~observed 실측 구간으로 대체 계산한다(2026-09-18
    # 정정 - 이전엔 이 경우 None으로 남겼지만, request~observed도 엄연한
    # 실측값이라 "근거 없는 값"이 아니다). precise_time은 실제 시계와
    # 무관한 값을 쓰면 t_injection_request(run_once() 자신의 실제 현재
    # 시각)와의 차가 무의미해지므로, 실제 injector처럼 "지금"에 가까운
    # 값을 흉내낸다.
    injector, _ = _fake_injector(is_done_after_calls=1)
    # 실제 injector처럼 "호출되는 시점"의 현재 시각을 반환해야 한다 - 미리
    # 계산해두면 PREPARING/PROBING/READY를 거치는 동안 시간이 흘러
    # t_injection_request(더 나중에 찍힘)보다 앞선 값이 돼버린다.
    injector.get_actual_injection_time = lambda: datetime.now(timezone.utc).isoformat()
    prober, _ = _fake_prober(violates_after_calls=1, recovers_after_slo_calls=1)

    result = run_once(
        scenario="dry_run", arm="native", rep=16, sequence_index=16, order_seed=1,
        injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.1,
        results_dir=tmp_path,
    )

    assert result.t_injection is not None
    assert result.t_injection_last_seen is None, "get_last_seen_present_time() 미구현이면 None"
    assert result.injection_observation_error_sec is not None
    assert 0 <= result.injection_observation_error_sec < 1.0, \
        f"request~observed 구간이 비정상적으로 큼: {result.injection_observation_error_sec}"
    print("OK - 관측 오차 hook 미구현 시 t_injection_request~observed 구간으로 대체 계산")


def test_first_poll_already_gone_records_request_to_observed_span(tmp_path):
    # 즉발 injector(pod_kill 등)에서 첫 poll에 이미 대상이 사라진 경우 -
    # get_last_seen_present_time()도 None을 반환한다(관측 기준점 자체가
    # 없음). t_injection_last_seen은 null로 남고, 실제 주입 구간은
    # t_injection_request~t_injection_observed로 기록돼야 한다.
    injector, _ = _fake_injector(is_done_after_calls=1)
    injector.get_actual_injection_time = lambda: datetime.now(timezone.utc).isoformat()
    injector.get_last_seen_present_time = lambda: None  # 첫 poll에 이미 사라짐
    prober, _ = _fake_prober(violates_after_calls=1, recovers_after_slo_calls=1)

    result = run_once(
        scenario="dry_run", arm="native", rep=17, sequence_index=17, order_seed=1,
        injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.1,
        results_dir=tmp_path,
    )

    assert result.t_injection_request is not None
    assert result.t_injection_last_seen is None
    assert result.t_injection_observed is not None
    req_dt = datetime.fromisoformat(result.t_injection_request)
    obs_dt = datetime.fromisoformat(result.t_injection_observed)
    assert abs(result.injection_observation_error_sec - (obs_dt - req_dt).total_seconds()) < 1e-6
    print("OK - last_seen 없으면 request~observed 구간이 injection_observation_error_sec으로 기록됨")


def test_last_seen_present_uses_tighter_span_than_request(tmp_path):
    # get_last_seen_present_time()이 구현돼 있으면 t_injection_last_seen이
    # 채워지고, injection_observation_error_sec은(어댑터가 직접
    # get_injection_observation_error_sec()을 안 줘도) request~observed가
    # 아니라 더 좁은 last_seen~observed 구간을 사용해야 한다.
    injector, _ = _fake_injector(is_done_after_calls=1)
    last_seen_iso = "2026-01-01T00:00:00.500000+00:00"
    observed_iso = "2026-01-01T00:00:00.900000+00:00"
    injector.get_actual_injection_time = lambda: observed_iso
    injector.get_last_seen_present_time = lambda: last_seen_iso
    prober, _ = _fake_prober(violates_after_calls=1, recovers_after_slo_calls=1)

    result = run_once(
        scenario="dry_run", arm="native", rep=18, sequence_index=18, order_seed=1,
        injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.1,
        results_dir=tmp_path,
    )

    assert result.t_injection_last_seen == last_seen_iso
    assert result.t_injection_observed == observed_iso
    assert result.injection_observation_error_sec == pytest.approx(0.4)  # 0.9-0.5, request(현재 시각) 기준 아님
    print("OK - last_seen 있으면 request~observed 대신 last_seen~observed(더 좁은 구간) 사용")


def test_not_evaluable_blocks_premature_prevented(tmp_path):
    # 실측 버그 회귀 테스트(2026-09-17): pod_kill native 파일럿에서 즉발
    # injector(injector.is_done()이 주입 직후 바로 True)가 t_injection_end를
    # 곧장 찍는 바람에, probe가 표본을 하나도 못 읽은 채 prevented로
    # 오판정됐다. prober.is_slo_evaluable()이 False(NOT_EVALUABLE)인 동안은
    # t_slo가 계속 None이어도 즉시 prevented로 끝나면 안 된다.
    injector, _ = _fake_injector(is_done_after_calls=1)  # 즉발 injector 흉내
    evaluable_after_calls = 3
    calls = {"is_slo_evaluable": 0}

    def is_slo_evaluable():
        calls["is_slo_evaluable"] += 1
        return calls["is_slo_evaluable"] >= evaluable_after_calls

    prober, _ = _fake_prober(violates_after_calls=10_000)  # 절대 위반 안 함
    prober.is_slo_evaluable = is_slo_evaluable

    result = run_once(
        scenario="dry_run", arm="native", rep=20, sequence_index=20, order_seed=1,
        injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.05,
        results_dir=tmp_path,
    )

    assert result.outcome == "prevented", result.outcome
    assert result.slo_evaluable_at_exit is True
    assert calls["is_slo_evaluable"] >= evaluable_after_calls, \
        "evaluable=False인 동안은 prevented로 조기 종료되면 안 됨(반복 확인돼야 함)"
    print("OK - NOT_EVALUABLE인 동안은 prevented 조기 종료 차단, evaluable된 뒤에만 확정")


def test_min_observation_sec_blocks_premature_prevented(tmp_path):
    # min_observation_sec은 is_slo_evaluable 미구현(None) 어댑터에도 최소
    # 관측시간 바닥을 강제한다 - 즉발 injector가 곧장 t_injection_end를 찍어도
    # min_observation_sec이 지나기 전에는 prevented로 끝나면 안 된다.
    injector, _ = _fake_injector(is_done_after_calls=1)
    prober, _ = _fake_prober(violates_after_calls=10_000)  # is_slo_evaluable 미구현

    start = time.monotonic()
    result = run_once(
        scenario="dry_run", arm="native", rep=21, sequence_index=21, order_seed=1,
        injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.05,
        min_observation_sec=0.3, results_dir=tmp_path,
    )
    elapsed = time.monotonic() - start

    assert result.outcome == "prevented", result.outcome
    assert elapsed >= 0.3, f"min_observation_sec(0.3s) 전에 끝남: {elapsed:.3f}s"
    assert result.min_observation_sec == 0.3, "전달한 min_observation_sec이 결과에 그대로 기록돼야 함"
    # is_slo_evaluable 미구현(None)이면 게이트는 통과해도(하위호환) 기록은
    # "검증됨"이 아니라 "검증 안 함"을 뜻하는 None이어야 한다(2026-09-17 정정).
    assert result.slo_evaluable_at_exit is None
    print(f"OK - min_observation_sec 바닥 적용 확인({elapsed:.3f}s >= 0.3s), 미구현 hook은 None 기록")


def test_min_observation_sec_anchored_after_effectiveness_confirmed(tmp_path):
    # 실측 지적 회귀 테스트(2026-09-17): min_observation_sec의 기준점이
    # injector.inject() 호출 "전"으로 잡혀 있으면, 실제 효과 확인
    # (is_effective())까지 걸린 대기시간이 최소 관찰시간 바닥을 그만큼
    # 갉아먹는다. is_started_after_calls로 효과 확인 지연을 흉내낸다 -
    # poll_interval_sec=0.05 * 10회 ≈ 0.45초 지연. min_observation_sec=0.2초를
    # 그 "이후"부터 다시 세면 총 최소 0.65초, 기준점이 inject() 전이면
    # 지연 자체(0.45초)만으로 이미 바닥을 넘겨 거의 즉시 끝난다.
    injector, _ = _fake_injector(is_started_after_calls=10, is_done_after_calls=1)
    prober, _ = _fake_prober(violates_after_calls=10_000)

    start = time.monotonic()
    result = run_once(
        scenario="dry_run", arm="native", rep=23, sequence_index=23, order_seed=1,
        injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.05,
        min_observation_sec=0.2, results_dir=tmp_path,
    )
    elapsed = time.monotonic() - start

    assert result.outcome == "prevented", result.outcome
    assert elapsed >= 0.6, (
        f"효과 확인 지연(~0.45s)이 min_observation_sec(0.2s)에 흡수됨: {elapsed:.3f}s "
        f"(기준점이 is_effective() 확인 이후가 아니라 inject() 호출 전으로 잡혔을 가능성)"
    )
    print(f"OK - min_observation_sec은 is_effective() 확인 이후부터 계산됨({elapsed:.3f}s >= 0.6s)")


def test_slo_evaluable_at_exit_none_when_hook_unimplemented(tmp_path):
    # hook 미구현(None)이면 게이트는 기존처럼 evaluable=True로 간주해 동작은
    # 그대로지만, 기록되는 slo_evaluable_at_exit은 "검증됨"이 아니라 "검증
    # 안 함"을 뜻하는 None이어야 한다(2026-09-17 정정) - min_observation_sec
    # 관련 테스트와 별개로, 이 필드 하나만 단독으로 확인.
    injector, _ = _fake_injector(is_done_after_calls=1)
    prober, _ = _fake_prober(violates_after_calls=10_000)  # is_slo_evaluable 미구현

    result = run_once(
        scenario="dry_run", arm="native", rep=24, sequence_index=24, order_seed=1,
        injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.05,
        results_dir=tmp_path,
    )

    assert result.outcome == "prevented", result.outcome
    assert result.slo_evaluable_at_exit is None, \
        "hook 미구현이면 게이트는 통과해도 기록은 None(검증 안 함)이어야 함"
    print("OK - evaluability hook 미구현 시 slo_evaluable_at_exit=None 기록")


def test_violation_detected_even_while_not_evaluable(tmp_path):
    # NOT_EVALUABLE 게이트는 "위반 없음을 믿어도 되는가"만 막는다 - 실제
    # 위반(t_slo)은 evaluable 여부와 무관하게 항상 그대로 신뢰해야 한다.
    injector, _ = _fake_injector(is_done_after_calls=5)
    prober, _ = _fake_prober(violates_after_calls=1, recovers_after_slo_calls=1)
    prober.is_slo_evaluable = lambda: False  # 절대 evaluable 안 됨

    result = run_once(
        scenario="dry_run", arm="native", rep=22, sequence_index=22, order_seed=1,
        injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.05,
        results_dir=tmp_path,
    )

    assert result.t_slo is not None, "evaluable=False여도 실제 위반은 감지돼야 함"
    assert result.outcome == "recovered", result.outcome
    assert result.slo_evaluable_at_exit is None, "recovered는 prevented가 아니므로 채우지 않음"
    print("OK - NOT_EVALUABLE 상태에서도 실제 SLO 위반 감지는 그대로 신뢰됨")


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


def test_baseline_gate_waits_until_ready_then_injects(tmp_path):
    # 주입 전 baseline 미확보 문제 수정(2026-09-18) - get_baseline_status가
    # 처음엔 ready=False를 내다가 3번째 호출부터 ready=True를 내면, run_once()는
    # 그동안 주입을 미루고 폴링만 하다가 ready된 뒤에야 정상적으로 주입을
    # 진행해야 한다. 결과에는 baseline 스냅샷이 그대로 기록돼야 한다.
    injector, icalls = _fake_injector(is_done_after_calls=1)
    prober, pcalls = _fake_prober(violates_after_calls=10_000, baseline_ready_after_calls=3)

    result = run_once(
        scenario="dry_run", arm="native", rep=30, sequence_index=30, order_seed=1,
        injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.02,
        baseline_timeout_sec=5, results_dir=tmp_path,
    )

    assert pcalls["get_baseline_status"] >= 3
    assert icalls["inject"] == 1, "baseline ready 이후에는 정상적으로 주입이 진행돼야 함"
    assert result.baseline_valid is True
    assert result.t_baseline_ready is not None
    assert result.baseline_sample_count is not None
    assert result.baseline_p95 is not None
    assert result.baseline_availability == 1.0
    print("OK - baseline ready가 될 때까지 대기한 뒤 정상적으로 주입 진행, 결과에 baseline 필드 기록")


def test_baseline_gate_never_ready_blocks_injection_and_invalidates(tmp_path):
    # 방식 3 회귀 테스트: baseline 조건이 제한시간(여기선 테스트 속도를 위해
    # baseline_timeout_sec을 짧게 줌) 안에 충족되지 않으면 injector.inject()
    # 자체가 호출되면 안 되고, invalid_run으로 끝나야 한다.
    injector, icalls = _fake_injector(is_done_after_calls=1)
    prober, pcalls = _fake_prober(violates_after_calls=10_000, baseline_ready_after_calls=10_000)  # 절대 ready 안 됨

    result = run_once(
        scenario="dry_run", arm="native", rep=31, sequence_index=31, order_seed=1,
        injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.02,
        baseline_timeout_sec=0.2, results_dir=tmp_path,
    )

    assert icalls["inject"] == 0, "baseline이 확보되지 않았으면 주입 자체를 시도하면 안 됨"
    assert result.outcome == "invalid_run", result.outcome
    assert result.baseline_valid is False
    assert result.t_baseline_ready is None
    print("OK - baseline이 시간 내 확보되지 않으면 주입 안 하고 invalid_run:", result.invalid_reason)


def test_baseline_gate_skipped_when_hook_unimplemented(tmp_path):
    # pod_kill/network_degrade처럼(둘 다 load_ramp_adapter.make_load_ramp_prober를
    # 공용 Prober로 재사용하므로 실제로는 이 훅도 같이 갖지만, 훅 자체가 없는
    # 어댑터에 대한 하위호환을 직접 확인) get_baseline_status 훅이 없으면 이
    # 단계 자체가 건너뛰어져 기존과 동일하게 바로 주입돼야 한다(회귀 없음).
    injector, icalls = _fake_injector(is_done_after_calls=1)
    prober, pcalls = _fake_prober(violates_after_calls=1, recovers_after_slo_calls=1)  # get_baseline_status 미구현

    result = run_once(
        scenario="dry_run", arm="native", rep=32, sequence_index=32, order_seed=1,
        injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.1,
        results_dir=tmp_path,
    )

    assert pcalls["get_baseline_status"] == 0
    assert icalls["inject"] == 1
    assert result.outcome == "recovered", result.outcome
    assert result.baseline_valid is None, "hook 미구현이면 검증 안 함을 뜻하는 None이어야 함(실패 아님)"
    assert result.t_baseline_ready is None
    print("OK - get_baseline_status 미구현 어댑터는 BASELINE 단계를 건너뛰고 기존과 동일하게 동작(회귀 없음)")


@pytest.mark.live_cluster
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


@pytest.mark.live_cluster
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
    import os
    import tempfile
    from pathlib import Path

    def _run_with_tmp_dir(test_fn):
        with tempfile.TemporaryDirectory() as d:
            test_fn(Path(d))

    offline_tests = (
        test_normal_completion,
        test_precise_injection_time_overrides_and_records_observation_error,
        test_precise_injection_time_without_error_hook_falls_back_to_request_span,
        test_first_poll_already_gone_records_request_to_observed_span,
        test_last_seen_present_uses_tighter_span_than_request,
        test_not_evaluable_blocks_premature_prevented,
        test_min_observation_sec_blocks_premature_prevented,
        test_min_observation_sec_anchored_after_effectiveness_confirmed,
        test_slo_evaluable_at_exit_none_when_hook_unimplemented,
        test_violation_detected_even_while_not_evaluable,
        test_pilot_result_written_to_pilot_subdir,
        test_slo_violation_gates_recovery_check,
        test_prevented_when_never_violates,
        test_timeout,
        test_injection_not_effective_marks_invalid,
        test_exception_still_cleans_up_and_marks_invalid,
        test_probe_never_alive_marks_invalid,
        test_critical_cleanup_failure_raises_and_still_writes_result,
        test_prober_still_alive_after_stop_raises_harness_corrupted,
        test_baseline_gate_waits_until_ready_then_injects,
        test_baseline_gate_never_ready_blocks_injection_and_invalidates,
        test_baseline_gate_skipped_when_hook_unimplemented,
    )
    live_cluster_tests = (
        test_real_experiment_context_registration_non_native_arm,
        test_active_context_blocks_new_trial_start,
    )

    for test_fn in offline_tests:
        _run_with_tmp_dir(test_fn)

    if os.environ.get("RUN_LIVE_TESTS") == "1":
        for test_fn in live_cluster_tests:
            _run_with_tmp_dir(test_fn)
    else:
        print(f"건너뜀({len(live_cluster_tests)}개) - live_cluster 테스트는 RUN_LIVE_TESTS=1로만 실행")
    print("모두 통과")
