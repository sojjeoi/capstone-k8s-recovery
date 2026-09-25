#!/usr/bin/env python3
"""run_once.py의 상태머신을 검증 - 실제 chaos 없이 가짜 Injector/Prober로
검증한다(experiment-contract.md 3단계 완료기준 + 1·2차 리뷰에서 지적된
문제들의 회귀 테스트). run_id 등록/quiescence 확인은 arm="native"일 때
건너뛰므로 대부분은 오프라인으로 돈다 - non-native arm의 "실제 클러스터
연동 자체가 되는가"는 `@pytest.mark.live_cluster`로 표시해 기본 `pytest`
실행에서는 건너뛴다(conftest.py, RUN_LIVE_TESTS=1로만 실행 -
experiments/README.md "테스트" 절).

t_detection/t_api_request 회수 로직(2026-09-19 추가)만은 예외 - non-native
arm이어야 그 코드 경로를 타는데, 관리자 엔드포인트 장애 같은 시나리오는
실클러스터에서 안정적으로 재현할 수 없어(포트포워딩 끊기 흉내가 어려움)
`requests.get`/`requests.post`를 직접 mocking해 오프라인으로 검증한다
(`_mock_admin_endpoints()` 참고) - quiescence/context 등록/cooldown 등
"실제로 그 프로토콜을 지키는가"는 여전히 live_cluster 테스트 몫이고, 여기서는
"timing 엔드포인트 응답에 따라 run_once()가 올바르게 반응하는가"만 본다.

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
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

sys.stdout.reconfigure(encoding="utf-8")

import pytest
import requests

from run_once import RECOVERY_POLICY_URL, Detector, HarnessCorrupted, Injector, Prober, TrialInvalid, run_once


def _fake_injector(is_done_after_calls=1, is_started_after_calls=1, effective=True, classify_stage_fn=None,
                    event_log=None):
    """classify_stage_fn(2026-09-18 추가, stage 관측성 보완 회귀 테스트용):
    None(기본)이면 Injector.classify_stage 자체를 구현 안 함(하위호환 -
    pod_kill/network_degrade처럼 stage 개념이 없는 어댑터를 흉내). 함수를
    주면 그대로 classify_stage로 노출한다(테스트가 원하는 값을 반환하거나
    예외를 던지게 할 수 있음). event_log(2026-09-18 추가, arm orchestration
    보완 회귀 테스트용): 주어지면 inject() 호출 시각을 그 리스트에
    append - _fake_detector()의 event_log와 공유해서 "detector 시작 후에만
    injection"의 실제 호출 순서를 검증한다."""
    calls = {"prepare": 0, "inject": 0, "is_started": 0, "is_effective": 0, "is_done": 0, "cleanup": 0}

    def prepare():
        calls["prepare"] += 1

    def inject():
        calls["inject"] += 1
        if event_log is not None:
            event_log.append("inject")

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

    injector_kwargs = dict(prepare=prepare, inject=inject, is_started=is_started,
                            is_effective=is_effective, is_done=is_done, cleanup=cleanup)
    if classify_stage_fn is not None:
        injector_kwargs["classify_stage"] = classify_stage_fn
    return Injector(**injector_kwargs), calls


def _fake_prober(alive=True, violates_after_calls=1, recovers_after_slo_calls=1, stops_cleanly=True,
                  baseline_ready_after_calls=None, event_log=None):
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
            if ready and event_log is not None and "baseline_ready" not in event_log:
                event_log.append("baseline_ready")
            return {
                "ready": ready,
                "sample_count": min(20 + counters["baseline"], 60),
                "p95": 0.3,
                "availability": 1.0,
                "ready_at": "2026-01-01T00:00:05.300000+00:00" if ready else None,
            }
        prober_kwargs["get_baseline_status"] = get_baseline_status

    return Prober(**prober_kwargs), calls


def _fake_detector(name="fake_detector", dies_after_calls=None, event_log=None):
    """arm orchestration 보완(2026-09-18) 회귀 테스트용 가짜 Detector.
    dies_after_calls: None(기본)이면 항상 살아있음 - 정수를 주면 그
    호출 횟수부터 is_alive()가 False(크래시 흉내). event_log: 주어지면
    start()/stop() 호출 시각을 그 리스트에 append(다른 컴포넌트의 호출
    순서와 비교하기 위한 공유 로그 - baseline/injection보다 먼저/나중에
    호출되는지 검증)."""
    calls = {"start": 0, "is_alive": 0, "stop": 0}
    state = {"started": False, "stopped": False}

    def start():
        calls["start"] += 1
        state["started"] = True
        if event_log is not None:
            event_log.append("detector_start")

    def is_alive():
        calls["is_alive"] += 1
        if not state["started"] or state["stopped"]:
            return False
        if dies_after_calls is not None and calls["is_alive"] >= dies_after_calls:
            return False
        return True

    def stop():
        calls["stop"] += 1
        state["stopped"] = True
        if event_log is not None:
            event_log.append("detector_stop")

    return Detector(start=start, is_alive=is_alive, stop=stop, name=name), calls


@contextmanager
def _mock_admin_endpoints(timing_response=None, timing_raises=None, audit_records=None, audit_raises=None,
                          call_log=None):
    """t_detection/t_api_request 회수 로직(2026-09-19 추가) 검증용 - non-native
    arm의 run_once() 전체 흐름이 recovery-policy 없이도 오프라인으로 돌게
    quiescence/active-context/cooldown/register/clear는 전부 "정상 진행"
    고정 응답을 주고, 상태(timing) 엔드포인트와 감사 조회 엔드포인트만 테스트가
    원하는 응답(또는 예외)을 내도록 열어둔다. timing_response는 이제 판정·조치
    필드까지 담는 "현재 실험 상태" 응답이다(_state_response() 참고).
    audit_records: GET /admin/audit/{run_id}의 records - 리스트면 항상 그 값,
    호출 횟수별로 다른 응답을 주려면 "리스트의 리스트"를 넘긴다(순서대로 소비).
    call_log: 주어지면 ("GET"|"POST", 경로)를 호출 순서대로 append한다."""
    audit_iter = iter(audit_records) if (audit_records and isinstance(audit_records[0], list)) else None
    # timing_response가 리스트면 호출마다 순서대로 소비하고 마지막 값을 반복한다("판정 진행 중 -> 확정" 시나리오용)
    timing_seq = list(timing_response) if isinstance(timing_response, list) else None

    def fake_get(url, timeout=None, **kwargs):
        if call_log is not None:
            call_log.append(("GET", url.split("localhost:8080")[-1]))
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        if url.endswith("/admin/quiescent"):
            resp.json.return_value = {"quiescent": True, "active_count": 0}
        elif url.endswith("/admin/experiment-run/timing"):
            if timing_raises is not None:
                raise timing_raises
            if timing_seq is not None:
                resp.json.return_value = timing_seq.pop(0) if len(timing_seq) > 1 else timing_seq[0]
            else:
                resp.json.return_value = timing_response
        elif "/admin/audit/" in url:
            if audit_raises is not None:
                raise audit_raises
            records = next(audit_iter, []) if audit_iter is not None else (audit_records or [])
            resp.json.return_value = {"run_id": url.rsplit("/", 1)[-1], "records": records}
        elif url.endswith("/admin/experiment-run"):
            resp.json.return_value = {"current": None}
        else:
            raise AssertionError(f"예상 못 한 GET: {url}")
        return resp

    def fake_post(url, timeout=None, **kwargs):
        if call_log is not None:
            call_log.append(("POST", url.split("localhost:8080")[-1]))
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        if url.endswith("/admin/reset-cooldown"):
            resp.json.return_value = {"status": "cooldown_reset"}
        elif url.endswith("/admin/experiment-run/clear"):
            resp.json.return_value = {"status": "cleared"}
        elif url.endswith("/admin/experiment-run"):
            resp.json.return_value = {"status": "active"}
        else:
            raise AssertionError(f"예상 못 한 POST: {url}")
        return resp

    with patch("run_once.requests.get", side_effect=fake_get), \
         patch("run_once.requests.post", side_effect=fake_post):
        yield


def _state_response(run_id, **over):
    """recovery-policy GET /admin/experiment-run/timing의 "미탐지" 기본 응답 - over로 덮어쓴다."""
    state = {
        "run_id": run_id, "t_detection": None, "t_decision": None, "t_api_request": None, "t_switch": None,
        "detected": False, "detection_source": None, "detector": None, "action": None,
        "decision_outcome": None, "idempotency_key": None, "promotion_verified": None,
    }
    state.update(over)
    return state


def _promoted_state(run_id, **over):
    """예측 경로 promotion 성공(executed_verified)한 trial의 상태 응답."""
    fields = dict(
        t_detection="2026-09-19T00:00:05+00:00", t_decision="2026-09-19T00:00:05.010000+00:00",
        t_api_request="2026-09-19T00:00:05.030000+00:00", t_switch="2026-09-19T00:00:05.400000+00:00",
        detected=True, detection_source="predictive", detector="isolation_forest", action="promote_preview",
        decision_outcome="executed_verified", idempotency_key=f"{run_id}:anomaly_risk", promotion_verified=True)
    fields.update(over)
    return _state_response(run_id, **fields)


def _audit_record(run_id, outcome="executed_verified", status="pushed", key=None, action="promote_preview",
                  record_id="rec-primary", **outbox_over):
    outbox = {"status": status, "t_audit_write": "2026-09-19T00:00:06+00:00",
              "t_audit_push": "2026-09-19T00:00:09+00:00" if status == "pushed" else None,
              "commit_sha": "abc123def" if status == "pushed" else None, "attempts": 0, "last_error": None}
    outbox.update(outbox_over)
    return {"record_id": record_id, "decided_at": "2026-09-19T00:00:06+00:00", "signal_source": "anomaly",
            "signal_type": "anomaly_risk", "idempotency_key": key or f"{run_id}:anomaly_risk",
            "evidence": {"experiment_run_id": run_id, "detector": "isolation_forest"},
            "action": action, "outcome": outcome, "result": None, "reasoning": "", "outbox": outbox}


_FAST_AUDIT = dict(audit_wait_sec=0.3, audit_poll_sec=0.02, state_settle_sec=0.3)


def _run_non_native(tmp_path, run_id, arm="proposed", **admin):
    injector, _ = _fake_injector(is_done_after_calls=1)
    prober, _ = _fake_prober(violates_after_calls=1, recovers_after_slo_calls=1)
    with _mock_admin_endpoints(**admin):
        return run_once(
            scenario="dry_run", arm=arm, rep=1, sequence_index=1, order_seed=1,
            injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.05,
            run_id=run_id, results_dir=tmp_path, **_FAST_AUDIT,
        )


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


def test_fixed_duration_observation_runs_full_window_but_classifies_correctly(tmp_path):
    # 후속 고정관측구간 프로토콜용 회귀 테스트 - fixed_duration_observation=False
    # (기본값, 미지정)일 때의 기존 동작은 완전히 그대로여야 하고, True일 때는
    # recovered가 이미 확정돼도 조기 종료(break)하지 않아야 한다.
    injector_early, _ = _fake_injector(is_done_after_calls=10)
    prober_early, pcalls_early = _fake_prober(violates_after_calls=2, recovers_after_slo_calls=2)
    result_early = run_once(
        scenario="dry_run", arm="native", rep=20, sequence_index=20, order_seed=1,
        injector=injector_early, prober=prober_early, timeout_sec=5, poll_interval_sec=0.02,
        results_dir=tmp_path,
    )
    assert result_early.outcome == "recovered"
    early_exit_calls = pcalls_early["is_alive"]  # is_alive()는 매 iteration 무조건 호출됨(t_slo 확정과 무관)

    injector_fixed, _ = _fake_injector(is_done_after_calls=10)
    prober_fixed, pcalls_fixed = _fake_prober(violates_after_calls=2, recovers_after_slo_calls=2)
    result_fixed = run_once(
        scenario="dry_run", arm="native", rep=21, sequence_index=21, order_seed=1,
        injector=injector_fixed, prober=prober_fixed, timeout_sec=1.0, poll_interval_sec=0.02,
        results_dir=tmp_path, fixed_duration_observation=True,
    )
    assert result_fixed.outcome == "recovered", result_fixed.outcome
    assert result_fixed.t_slo is not None and result_fixed.t_recovery is not None
    assert result_fixed.t_slo <= result_fixed.t_recovery
    # 같은 조건(빠른 회복)인데 fixed_duration_observation=True 쪽이 조기종료 없이
    # 더 오래(더 많이 poll) 도는지 확인 - 이게 핵심 회귀 방지 포인트다.
    assert pcalls_fixed["is_alive"] > early_exit_calls, (
        "fixed_duration_observation=True인데 조기 종료된 것처럼 poll 횟수가 적음"
    )
    print("OK - fixed_duration_observation=False(기본)는 조기종료 유지, "
          "True는 recovered 확정 후에도 전체 timeout_sec을 채우고 outcome은 정확히 분류")


def test_fixed_duration_observation_prevented_when_never_violates(tmp_path):
    # 위반이 끝까지 없으면 fixed_duration_observation=True에서도 prevented여야
    # 한다(반드시 recovered로 오분류되면 안 됨 - else절 재분류 로직의 회귀 테스트).
    injector, _ = _fake_injector(is_done_after_calls=1)
    prober, pcalls = _fake_prober(violates_after_calls=10_000)
    result = run_once(
        scenario="dry_run", arm="native", rep=22, sequence_index=22, order_seed=1,
        injector=injector, prober=prober, timeout_sec=0.3, poll_interval_sec=0.02,
        results_dir=tmp_path, fixed_duration_observation=True,
    )
    assert result.outcome == "prevented", result.outcome
    assert result.t_slo is None and result.t_recovery is None
    print("OK - fixed_duration_observation=True + 끝까지 위반 없음 -> prevented(재분류 정확)")


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


def test_prepare_harness_corrupted_propagates_and_marks_invalid_run(tmp_path):
    # 2026-09-19 추가 - arm_controller.wrap_injector_with_preview_prep()가
    # preview 준비 실패 후 자동 rollback까지 실패하면 injector.prepare()
    # 자체가 HarnessCorrupted를 던진다(fixed_threshold pilot 01회, preview가
    # 방치되고 Rollout이 Paused/Degraded로 남은 사고 계기). 이 trial은
    # invalid_run으로 기록되면서도 배치 자체는 멈춰야 한다(critical_failures
    # 경유로 함수 끝에서 HarnessCorrupted 재발생 - _reset_action_cooldown
    # 실패와 동일한 기존 패턴 재사용).
    def raising_prepare():
        raise HarnessCorrupted("preview 준비 실패 + 자동 rollback도 실패(시뮬레이션)")

    injector, icalls = _fake_injector()
    injector.prepare = raising_prepare
    prober, pcalls = _fake_prober()

    raised = False
    try:
        run_once(
            scenario="dry_run", arm="native", rep=20, sequence_index=20, order_seed=1,
            injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.1,
            results_dir=tmp_path,
        )
    except HarnessCorrupted as e:
        raised = True
        print("  ->", e)

    assert raised, "prepare()의 HarnessCorrupted는 run_once() 밖으로 전파돼야 함(배치 중단 신호)"
    result_files = sorted(tmp_path.glob("trial-dry_run-native-20-*.json"))
    assert len(result_files) == 1, "HarnessCorrupted로 끝나도 결과 파일은 기록돼야 함"
    written = json.loads(result_files[0].read_text(encoding="utf-8"))
    assert written["outcome"] == "invalid_run"
    assert "rollback" in written["invalid_reason"]
    assert icalls["inject"] == 0, "prepare() 실패 시 주입 자체를 시도하면 안 됨"
    print("OK - prepare()의 HarnessCorrupted -> invalid_run 기록 + 배치 중단용 예외 전파")


def test_preview_prep_info_populates_result_even_on_prepare_failure(tmp_path):
    # 2026-09-19 추가 - preview 준비 진단(t_preview_ready/소요시간/rollback
    # 결과)은 prepare()가 실패해도(=invalid_run이 돼도) 남아야 진단이 가능하다.
    def raising_prepare():
        raise TrialInvalid("preview가 timeout 내 Ready 안 됨(시뮬레이션)")

    injector, icalls = _fake_injector()
    injector.prepare = raising_prepare
    injector.get_preview_prep_info = lambda: {
        "t_prep_start": "2026-09-19T00:00:00+00:00", "t_preview_ready": None,
        "prep_duration_sec": 480.3, "rollback_attempted": True, "rollback_ok": True,
    }
    prober, _ = _fake_prober()

    result = run_once(
        scenario="dry_run", arm="native", rep=21, sequence_index=21, order_seed=1,
        injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.1,
        results_dir=tmp_path,
    )

    assert result.outcome == "invalid_run"
    assert result.t_preview_prep_start == "2026-09-19T00:00:00+00:00"
    assert result.t_preview_ready is None
    assert result.preview_prep_duration_sec == 480.3
    assert result.preview_rollback_attempted is True
    assert result.preview_rollback_ok is True
    print("OK - preview 준비 진단 정보가 prepare() 실패(invalid_run) 이후에도 결과에 반영됨")


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


def test_slo_stage_populated_when_classify_stage_implemented(tmp_path):
    # stage 관측성 보완(2026-09-18) - injector.classify_stage가 구현돼
    # 있고 t_slo가 실제로 찍히면, run_once()는 그 값으로 classify_stage를
    # 호출해 slo_stage를 채워야 한다.
    calls = {"classify_stage": []}

    def classify_stage_fn(timestamp_iso):
        calls["classify_stage"].append(timestamp_iso)
        return "stage-3-0.20rps"

    injector, _ = _fake_injector(is_done_after_calls=5, classify_stage_fn=classify_stage_fn)
    prober, _ = _fake_prober(violates_after_calls=1, recovers_after_slo_calls=1)

    result = run_once(
        scenario="dry_run", arm="native", rep=40, sequence_index=40, order_seed=1,
        injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.02,
        results_dir=tmp_path,
    )

    assert result.t_slo is not None
    assert result.slo_stage == "stage-3-0.20rps"
    assert result.t_slo in calls["classify_stage"], "classify_stage가 실제 t_slo 값으로 호출돼야 함"
    print("OK - classify_stage 구현 시 t_slo 기준으로 slo_stage가 채워짐")


def test_detection_and_action_stage_stay_none_without_source_timestamp(tmp_path):
    # native arm은 t_detection/t_api_request 자체가 항상 None(정책 엔진
    # 개입 없음) - classify_stage가 구현돼 있어도 대응하는 timestamp가
    # 없으면 detection_stage/action_stage는 "unknown"이 아니라 None이어야
    # 한다("사건이 없었음"과 "사건은 있었는데 분류 못 함"을 구분).
    injector, _ = _fake_injector(is_done_after_calls=5, classify_stage_fn=lambda t: "stage-1-0.025rps")
    prober, _ = _fake_prober(violates_after_calls=1, recovers_after_slo_calls=1)

    result = run_once(
        scenario="dry_run", arm="native", rep=41, sequence_index=41, order_seed=1,
        injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.02,
        results_dir=tmp_path,
    )

    assert result.t_detection is None and result.t_api_request is None
    assert result.detection_stage is None
    assert result.action_stage is None
    print("OK - t_detection/t_api_request가 None이면 대응 stage 필드도 None(unknown 아님)")


def test_stage_fields_stay_none_when_classify_stage_unimplemented(tmp_path):
    # pod_kill/network_degrade처럼 classify_stage 훅이 없는 어댑터는
    # 3개 필드 전부 None으로 남아야 한다(회귀 없음).
    injector, _ = _fake_injector(is_done_after_calls=5)  # classify_stage_fn 미지정
    prober, _ = _fake_prober(violates_after_calls=1, recovers_after_slo_calls=1)

    result = run_once(
        scenario="dry_run", arm="native", rep=42, sequence_index=42, order_seed=1,
        injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.02,
        results_dir=tmp_path,
    )

    assert result.t_slo is not None  # 전제 확인 - 위반 자체는 있었음
    assert result.slo_stage is None
    assert result.detection_stage is None
    assert result.action_stage is None
    print("OK - classify_stage 훅 미구현이면 3개 필드 전부 None(회귀 없음)")


def test_classify_stage_exception_does_not_break_trial(tmp_path):
    # classify_stage 자체가 버그로 예외를 던져도, 이미 확정된 핵심 판정
    # (outcome/t_slo 등)이 오염되거나 HarnessCorrupted로 번지면 안 된다 -
    # 보조 정보 하나의 결함이 trial 전체를 망가뜨리지 않아야 함.
    def raising_classify_stage(timestamp_iso):
        raise RuntimeError("stage 분류 도중 의도적으로 터뜨린 예외")

    injector, _ = _fake_injector(is_done_after_calls=5, classify_stage_fn=raising_classify_stage)
    prober, _ = _fake_prober(violates_after_calls=1, recovers_after_slo_calls=1)

    result = run_once(
        scenario="dry_run", arm="native", rep=43, sequence_index=43, order_seed=1,
        injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.02,
        results_dir=tmp_path,
    )

    assert result.outcome == "recovered", result.outcome
    assert result.t_slo is not None
    assert result.slo_stage is None
    assert "stage 분류 실패" in result.notes
    print("OK - classify_stage 예외는 notes에만 남고 핵심 판정(outcome/t_slo)은 정상 유지")


def test_detector_starts_only_after_baseline_ready(tmp_path):
    # arm orchestration 보완(2026-09-18) - detector.start()는 baseline이
    # 확보된 뒤에만 호출돼야 한다(baseline 관찰 도중에는 detector 자체가
    # 존재하면 안 됨 - 지시).
    event_log = []
    injector, icalls = _fake_injector(is_done_after_calls=5, event_log=event_log)
    prober, _ = _fake_prober(violates_after_calls=10_000, baseline_ready_after_calls=3, event_log=event_log)
    detector, dcalls = _fake_detector(event_log=event_log)

    run_once(
        scenario="dry_run", arm="native", rep=50, sequence_index=50, order_seed=1,
        injector=injector, prober=prober, detector=detector,
        timeout_sec=5, poll_interval_sec=0.02, baseline_timeout_sec=5, results_dir=tmp_path,
    )

    assert "baseline_ready" in event_log and "detector_start" in event_log
    assert event_log.index("baseline_ready") < event_log.index("detector_start"), \
        f"detector.start()는 baseline_ready 이후여야 함: {event_log}"
    assert dcalls["start"] == 1
    print("OK - detector는 baseline이 확보된 뒤에만 시작됨:", event_log)


def test_injection_starts_only_after_detector_start(tmp_path):
    event_log = []
    injector, icalls = _fake_injector(is_done_after_calls=5, event_log=event_log)
    prober, _ = _fake_prober(violates_after_calls=10_000, baseline_ready_after_calls=3, event_log=event_log)
    detector, dcalls = _fake_detector(event_log=event_log)

    run_once(
        scenario="dry_run", arm="native", rep=51, sequence_index=51, order_seed=1,
        injector=injector, prober=prober, detector=detector,
        timeout_sec=5, poll_interval_sec=0.02, baseline_timeout_sec=5, results_dir=tmp_path,
    )

    assert "detector_start" in event_log and "inject" in event_log
    assert event_log.index("detector_start") < event_log.index("inject"), \
        f"injector.inject()는 detector.start() 이후여야 함: {event_log}"
    print("OK - injection은 detector가 시작된 뒤에만 실행됨:", event_log)


def test_detector_crash_marks_invalid_run(tmp_path):
    injector, icalls = _fake_injector(is_done_after_calls=10_000)  # 관찰 도중 안 끝남
    prober, _ = _fake_prober(violates_after_calls=10_000)  # 절대 위반 안 됨
    detector, dcalls = _fake_detector(dies_after_calls=2)  # is_alive() 2번째 호출부터 크래시

    result = run_once(
        scenario="dry_run", arm="native", rep=52, sequence_index=52, order_seed=1,
        injector=injector, prober=prober, detector=detector,
        timeout_sec=5, poll_interval_sec=0.02, results_dir=tmp_path,
    )

    assert result.outcome == "invalid_run", result.outcome
    assert "detector" in result.invalid_reason
    print("OK - detector가 관찰 도중 크래시하면 invalid_run:", result.invalid_reason)


def test_detector_stopped_even_when_injector_raises(tmp_path):
    def raising_inject():
        raise RuntimeError("의도적으로 터뜨린 예외 - injector.inject() 실패 시나리오")

    injector = Injector(prepare=lambda: None, inject=raising_inject, is_started=lambda: True,
                         is_effective=lambda: True, is_done=lambda: True, cleanup=lambda: None)
    prober, _ = _fake_prober()
    detector, dcalls = _fake_detector()

    result = run_once(
        scenario="dry_run", arm="native", rep=53, sequence_index=53, order_seed=1,
        injector=injector, prober=prober, detector=detector,
        timeout_sec=5, poll_interval_sec=0.1, results_dir=tmp_path,
    )

    assert result.outcome == "invalid_run", result.outcome
    assert dcalls["start"] == 1, "detector는 injector.inject() 예외 전에 이미 시작됐어야 함"
    assert dcalls["stop"] == 1, "injector.inject()가 예외를 던져도 detector.stop()은 호출돼야 함"
    print("OK - injector.inject() 예외가 나도 detector는 정리됨")


def test_detector_stopped_on_timeout(tmp_path):
    injector, icalls = _fake_injector(is_done_after_calls=1)
    prober, _ = _fake_prober(violates_after_calls=1, recovers_after_slo_calls=10_000)  # 절대 회복 안 되게
    detector, dcalls = _fake_detector()

    result = run_once(
        scenario="dry_run", arm="native", rep=54, sequence_index=54, order_seed=1,
        injector=injector, prober=prober, detector=detector,
        timeout_sec=1, poll_interval_sec=0.2, results_dir=tmp_path,
    )

    assert result.outcome == "timeout", result.outcome
    assert dcalls["stop"] == 1, "timeout이어도 detector.stop()은 호출돼야 함"
    print("OK - timeout이어도 detector는 정리됨")


def test_detector_process_field_records_name(tmp_path):
    injector, icalls = _fake_injector(is_done_after_calls=1)
    prober, _ = _fake_prober(violates_after_calls=1, recovers_after_slo_calls=1)
    detector, dcalls = _fake_detector(name="isolation_forest")

    result = run_once(
        scenario="dry_run", arm="native", rep=55, sequence_index=55, order_seed=1,
        injector=injector, prober=prober, detector=detector,
        timeout_sec=5, poll_interval_sec=0.1, results_dir=tmp_path,
    )

    assert result.detector_process == "isolation_forest"
    print("OK - detector.name이 TrialResult.detector_process에 정확히 기록됨:", result.detector_process)


def test_detector_none_leaves_detector_process_null(tmp_path):
    # 하위호환 - detector=None(기본값, native)이면 detector_process도 null.
    injector, icalls = _fake_injector(is_done_after_calls=1)
    prober, _ = _fake_prober(violates_after_calls=1, recovers_after_slo_calls=1)

    result = run_once(
        scenario="dry_run", arm="native", rep=56, sequence_index=56, order_seed=1,
        injector=injector, prober=prober,
        timeout_sec=5, poll_interval_sec=0.1, results_dir=tmp_path,
    )

    assert result.detector_process is None
    print("OK - detector 미지정 시 detector_process는 null(회귀 없음)")


def test_non_native_timing_fetch_populates_fields(tmp_path):
    run_id = "test-timing-fixed_threshold-01"
    state = _state_response(
        run_id, t_detection="2026-09-19T00:00:05+00:00", t_api_request="2026-09-19T00:00:06+00:00",
        detected=True, detection_source="predictive", detector="fixed_threshold", action="promote_preview",
        decision_outcome="executed_verified", idempotency_key=f"{run_id}:anomaly_risk", promotion_verified=True)
    result = _run_non_native(tmp_path, run_id, arm="fixed_threshold", timing_response=state,
                             audit_records=[_audit_record(run_id)])
    assert result.t_detection == "2026-09-19T00:00:05+00:00"
    assert result.t_api_request == "2026-09-19T00:00:06+00:00"
    assert result.outcome != "invalid_run", result.invalid_reason
    print("OK - non-native trial이 recovery-policy 상태 엔드포인트에서 t_detection/t_api_request를 회수함")


def test_non_native_timing_fetch_failure_marks_invalid_run(tmp_path):
    run_id = "test-timing-fail-01"
    injector, icalls = _fake_injector(is_done_after_calls=1)
    prober, _ = _fake_prober(violates_after_calls=1, recovers_after_slo_calls=1)
    with _mock_admin_endpoints(timing_raises=ConnectionError("timing 엔드포인트 접속 실패 시뮬레이션")):
        result = run_once(
            scenario="dry_run", arm="fixed_threshold", rep=1, sequence_index=1, order_seed=1,
            injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.05,
            run_id=run_id, results_dir=tmp_path,
        )
    assert result.outcome == "invalid_run", result.outcome
    assert "timing" in result.invalid_reason
    assert result.t_detection is None and result.t_api_request is None
    print("OK - timing 엔드포인트 조회 실패 -> 명시적으로 invalid_run(무탐지와 구분됨):", result.invalid_reason)


def test_non_native_timing_run_id_mismatch_marks_invalid_run(tmp_path):
    run_id = "test-timing-mismatch-01"
    injector, icalls = _fake_injector(is_done_after_calls=1)
    prober, _ = _fake_prober(violates_after_calls=1, recovers_after_slo_calls=1)
    # 서버가 다른 run_id(레이스나 등록 유실 흉내)를 반환
    timing_response = {"run_id": "some-other-run-id", "t_detection": "2026-09-19T00:00:05+00:00", "t_api_request": None}
    with _mock_admin_endpoints(timing_response=timing_response):
        result = run_once(
            scenario="dry_run", arm="proposed", rep=1, sequence_index=1, order_seed=1,
            injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.05,
            run_id=run_id, results_dir=tmp_path,
        )
    assert result.outcome == "invalid_run", result.outcome
    assert result.t_detection is None
    print("OK - timing 응답의 run_id가 안 맞으면 invalid_run:", result.invalid_reason)


def test_timing_fetch_failure_does_not_override_existing_invalid_reason(tmp_path):
    # 이미 다른 이유(예: injector.inject() 예외)로 invalid_run이 확정된
    # trial은, timing 조회까지 실패해도 원래 사유를 덮어쓰면 안 된다(더
    # 구체적인 원인 보존).
    def raising_inject():
        raise RuntimeError("의도적으로 터뜨린 예외")

    injector = Injector(prepare=lambda: None, inject=raising_inject, is_started=lambda: True,
                         is_effective=lambda: True, is_done=lambda: True, cleanup=lambda: None)
    prober, _ = _fake_prober()
    run_id = "test-timing-preserve-reason-01"
    with _mock_admin_endpoints(timing_raises=ConnectionError("timing도 실패")):
        result = run_once(
            scenario="dry_run", arm="fixed_threshold", rep=1, sequence_index=1, order_seed=1,
            injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.05,
            run_id=run_id, results_dir=tmp_path,
        )
    assert result.outcome == "invalid_run"
    assert "의도적으로 터뜨린 예외" in result.invalid_reason, \
        f"원래 invalid_reason이 timing 실패 사유로 덮어써짐: {result.invalid_reason}"
    print("OK - 이미 확정된 invalid_run 사유는 timing 조회 실패로 덮어써지지 않음:", result.invalid_reason)


def test_native_arm_never_calls_recovery_policy(tmp_path):
    injector, icalls = _fake_injector(is_done_after_calls=1)
    prober, _ = _fake_prober(violates_after_calls=1, recovers_after_slo_calls=1)

    def fail_if_called(*a, **kw):
        raise AssertionError("native arm이 recovery-policy에 HTTP 요청을 보내면 안 됨(계약서 §1)")

    with patch("run_once.requests.get", side_effect=fail_if_called), \
         patch("run_once.requests.post", side_effect=fail_if_called):
        result = run_once(
            scenario="dry_run", arm="native", rep=60, sequence_index=60, order_seed=1,
            injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.05,
            results_dir=tmp_path,
        )
    assert result.t_detection is None and result.t_api_request is None
    assert result.outcome != "invalid_run"
    # 2026-09-19 - 판정·조치 필드도 native는 계약서 §1대로 기본값(detected=false, action=none)이고
    # 나머지는 전부 null이다(recovery-policy를 조회하지도 감사 조회를 하지도 않음).
    assert result.detected is False and result.action == "none"
    for field in ("detection_source", "detector", "decision_outcome", "idempotency_key", "promotion_verified",
                  "judgment_source", "audit_status", "audit_status_reason", "audit_record_id", "audit_reconciled_at",
                  "reconciliation", "t_audit_write", "t_audit_push", "commit_sha", "t_decision", "t_switch"):
        assert getattr(result, field) is None, field
    print("OK - native arm은 recovery-policy·detector·preview 전부 비활성, 판정 필드는 기본값(계약서 §1 재확인)")


def test_detection_and_action_stage_computed_from_recovered_timing(tmp_path):
    # 방식 8 - t_detection/t_api_request가 실제로 채워지면, 기존 §31 stage
    # 분류 메커니즘(classify_stage)이 이 값들로 detection_stage/action_stage를
    # 정상 계산해야 한다(두 메커니즘의 통합 확인).
    run_id = "test-timing-stage-01"
    t_detection_iso = "2026-09-19T00:01:00+00:00"
    t_api_request_iso = "2026-09-19T00:01:30+00:00"

    def classify_stage_fn(timestamp_iso):
        return {t_detection_iso: "stage-2-0.05rps", t_api_request_iso: "stage-3-0.20rps"}.get(timestamp_iso, "unknown")

    injector, icalls = _fake_injector(is_done_after_calls=1, classify_stage_fn=classify_stage_fn)
    prober, _ = _fake_prober(violates_after_calls=1, recovers_after_slo_calls=1)
    state = _promoted_state(run_id, t_detection=t_detection_iso, t_api_request=t_api_request_iso)

    with _mock_admin_endpoints(timing_response=state, audit_records=[_audit_record(run_id)]):
        result = run_once(
            scenario="dry_run", arm="fixed_threshold", rep=1, sequence_index=1, order_seed=1,
            injector=injector, prober=prober, timeout_sec=5, poll_interval_sec=0.05,
            run_id=run_id, results_dir=tmp_path, **_FAST_AUDIT,
        )

    assert result.detection_stage == "stage-2-0.05rps"
    assert result.action_stage == "stage-3-0.20rps"
    print("OK - 회수된 t_detection/t_api_request로 detection_stage/action_stage가 정상 계산됨")


def test_predictive_promotion_fields_populated_from_authoritative_state(tmp_path):
    run_id = "test-judgment-predictive-01"
    result = _run_non_native(tmp_path, run_id, timing_response=_promoted_state(run_id),
                             audit_records=[_audit_record(run_id)])
    assert result.detected is True
    assert result.detection_source == "predictive" and result.detector == "isolation_forest"
    assert result.action == "promote_preview" and result.decision_outcome == "executed_verified"
    assert result.idempotency_key == f"{run_id}:anomaly_risk" and result.promotion_verified is True
    assert result.judgment_source == "live_state"
    assert result.audit_status == "complete" and result.commit_sha == "abc123def"
    assert result.t_audit_write and result.t_audit_push and result.audit_record_id == "rec-primary"
    assert result.audit_reconciled_at is not None
    assert (result.t_detection, result.t_decision, result.t_api_request, result.t_switch) == (
        "2026-09-19T00:00:05+00:00", "2026-09-19T00:00:05.010000+00:00",
        "2026-09-19T00:00:05.030000+00:00", "2026-09-19T00:00:05.400000+00:00"), "서버 시각 4종이 그대로 기록돼야 함"
    assert result.outcome == "recovered", "판정·감사 필드 회수가 outcome을 바꾸면 안 됨"
    written = json.loads(next(tmp_path.rglob(f"trial-{run_id}.json")).read_text(encoding="utf-8"))
    assert written["detected"] is True and written["commit_sha"] == "abc123def"
    print("OK - 예측 경로 promotion 성공: 판정·조치·감사 필드가 authoritative 상태에서 채워지고 파일에 기록됨")


def test_reactive_fallback_fields_populated(tmp_path):
    run_id = "test-judgment-reactive-01"
    key = "fp999:2026-09-19T00:00:05+00:00"
    state = _promoted_state(run_id, detection_source="reactive", detector="alertmanager", idempotency_key=key)
    record = _audit_record(run_id, key=key)
    record.update(signal_source="alertmanager", evidence={"experiment_run_id": run_id})
    result = _run_non_native(tmp_path, run_id, arm="fixed_threshold", timing_response=state, audit_records=[record])
    assert result.detection_source == "reactive" and result.detector == "alertmanager"
    assert result.idempotency_key == key
    assert result.audit_status == "complete", \
        "run_id를 못 담는 반응형 key여도 evidence.experiment_run_id로 귀속된 기록은 primary가 될 수 있어야 함"
    print("OK - 반응형 fallback: detection_source=reactive/detector=alertmanager, evidence로 귀속된 감사기록 연결")


def test_observe_only_fields_populated(tmp_path):
    run_id = "test-judgment-observe-01"
    state = _state_response(
        run_id, t_detection="2026-09-19T00:00:05+00:00", t_decision="2026-09-19T00:00:05.010000+00:00",
        detected=True, detection_source="predictive", detector="fixed_threshold", action="observe_only",
        decision_outcome="no_action", idempotency_key=f"{run_id}:anomaly_risk")
    result = _run_non_native(tmp_path, run_id, arm="fixed_threshold", timing_response=state,
                             audit_records=[_audit_record(run_id, outcome="no_action", action="observe_only")])
    assert result.detected is True and result.action == "observe_only" and result.decision_outcome == "no_action"
    assert result.promotion_verified is None and result.t_api_request is None and result.t_switch is None
    assert result.t_decision == "2026-09-19T00:00:05.010000+00:00", "observe-only여도 t_decision은 기록"
    assert result.audit_status == "complete"
    print("OK - observe-only: 탐지·판정·t_decision은 기록되고 promotion_verified/t_api_request/t_switch는 null")


def test_no_detection_is_recorded_as_authoritative_default_and_needs_no_audit(tmp_path):
    run_id = "test-judgment-nodetect-01"
    call_log = []
    result = _run_non_native(tmp_path, run_id, timing_response=_state_response(run_id), call_log=call_log)
    assert result.detected is False and result.action == "none"
    assert result.t_decision is None and result.t_api_request is None and result.t_switch is None
    assert result.judgment_source == "live_state", "미탐지도 기본값이 아니라 authoritative 상태에서 확인된 값"
    assert result.audit_status == "not_applicable" and result.commit_sha is None
    assert result.outcome == "recovered", "무탐지·무조치여도 SLO 궤적에 따라 outcome을 판정(임의 실패 처리 안 함)"
    assert not any("/admin/audit/" in path for _, path in call_log), "판정이 없으면 감사 조회를 하지 않음"
    print("OK - 미탐지: detected=false를 authoritative로 기록, 감사 조회 없이 not_applicable, outcome 불변")


def test_audit_push_pending_keeps_outcome_and_action_and_leaves_nulls(tmp_path):
    run_id = "test-audit-pending-01"
    result = _run_non_native(tmp_path, run_id, timing_response=_promoted_state(run_id),
                             audit_records=[_audit_record(run_id, status="pushing")])
    assert result.audit_status == "pending" and "pushing" in result.audit_status_reason
    assert result.t_audit_write is not None
    assert result.t_audit_push is None and result.commit_sha is None, "미완료는 null 유지"
    assert result.action == "promote_preview" and result.promotion_verified is True
    assert result.outcome == "recovered", "Git 지연이 outcome/action을 바꾸면 안 됨"
    print("OK - Git push 지연: audit_status=pending+사유, t_audit_push/commit_sha는 null, outcome/action 불변")


def test_audit_push_completes_during_bounded_wait(tmp_path):
    run_id = "test-audit-late-01"
    pending, pushed = [_audit_record(run_id, status="pushing")], [_audit_record(run_id)]
    result = _run_non_native(tmp_path, run_id, timing_response=_promoted_state(run_id),
                             audit_records=[pending, pending, pushed])
    assert result.audit_status == "complete" and result.commit_sha == "abc123def"
    print("OK - bounded wait 도중 push가 끝나면 complete로 기록")


def test_audit_failed_recorded_with_reason_without_invalidating_trial(tmp_path):
    run_id = "test-audit-failed-01"
    result = _run_non_native(
        tmp_path, run_id, timing_response=_promoted_state(run_id),
        audit_records=[_audit_record(run_id, status="failed", last_error="non-fast-forward 재조정 실패", attempts=6)])
    assert result.audit_status == "failed" and "non-fast-forward" in result.audit_status_reason
    assert result.commit_sha is None
    assert result.outcome == "recovered" and result.action == "promote_preview"
    print("OK - audit 실패: audit_status=failed+사유만 남고 trial은 유효 그대로")


def test_audit_query_failure_is_pending_not_invalid_run(tmp_path):
    run_id = "test-audit-unreachable-01"
    result = _run_non_native(tmp_path, run_id, timing_response=_promoted_state(run_id),
                             audit_raises=ConnectionError("감사 조회 접속 실패 시뮬레이션"))
    assert result.audit_status == "pending" and "조회 실패" in result.audit_status_reason
    assert result.outcome == "recovered", "감사 조회 실패는 상태(timing) 조회 실패와 달리 invalid_run이 아님"
    print("OK - 감사 조회 실패는 pending+사유(상태 조회 실패와 달리 invalid_run 아님)")


def test_state_fetched_before_context_clear(tmp_path):
    run_id = "test-order-01"
    call_log = []
    _run_non_native(tmp_path, run_id, timing_response=_promoted_state(run_id),
                    audit_records=[_audit_record(run_id)], call_log=call_log)
    paths = [f"{method} {path}" for method, path in call_log]
    state_idx = paths.index("GET /admin/experiment-run/timing")
    clear_idx = next(i for i, p in enumerate(paths) if p.startswith("POST /admin/experiment-run/clear"))
    assert state_idx < clear_idx, f"상태 회수는 context clear 전이어야 함: {paths}"
    print("OK - 판정·조치 상태는 context clear 전에 회수됨")


def test_in_flight_decision_settles_before_being_recorded(tmp_path):
    run_id = "test-inflight-01"
    in_flight = _state_response(run_id, t_detection="2026-09-19T00:00:05+00:00", detected=True,
                                detection_source="predictive", detector="isolation_forest",
                                t_api_request="2026-09-19T00:00:05.030000+00:00")
    result = _run_non_native(tmp_path, run_id, timing_response=[in_flight, _promoted_state(run_id)],
                             audit_records=[_audit_record(run_id)])
    assert result.action == "promote_preview" and result.decision_outcome == "executed_verified"
    assert result.promotion_verified is True, "promote() 진행 중에 trial이 끝나도 확정될 때까지 기다려 기록"
    print("OK - 판정이 처리 중일 때 trial이 끝나도 확정 후 기록(action=none으로 오기록 안 함)")


def test_in_flight_decision_never_settling_is_noted_not_guessed(tmp_path):
    run_id = "test-inflight-stuck-01"
    stuck = _state_response(run_id, t_detection="2026-09-19T00:00:05+00:00", detected=True,
                            detection_source="predictive", detector="isolation_forest")
    result = _run_non_native(tmp_path, run_id, timing_response=stuck)
    assert result.detected is True and result.decision_outcome is None
    assert "판정이" in result.notes and "확정되지 않음" in result.notes
    assert result.audit_status == "pending"
    print("OK - 끝내 확정 안 되면 추측하지 않고 notes에 남김")


def test_state_query_failure_leaves_judgment_non_authoritative(tmp_path):
    run_id = "test-judgment-unavailable-01"
    result = _run_non_native(tmp_path, run_id, timing_raises=ConnectionError("상태 엔드포인트 접속 실패"))
    assert result.outcome == "invalid_run"
    assert result.judgment_source is None, "조회 실패 시 detected=false는 authoritative가 아님을 표시"
    assert result.audit_status is None
    print("OK - 상태 조회 실패: invalid_run + judgment_source=null(권위 없는 기본값 표시)")


def test_next_trial_does_not_inherit_previous_judgment(tmp_path):
    first = _run_non_native(tmp_path, "test-iso-01", timing_response=_promoted_state("test-iso-01"),
                            audit_records=[_audit_record("test-iso-01")])
    second = _run_non_native(tmp_path, "test-iso-02", arm="fixed_threshold",
                             timing_response=_state_response("test-iso-02"))
    assert first.action == "promote_preview" and first.commit_sha == "abc123def"
    assert second.detected is False and second.action == "none"
    assert second.detector is None and second.commit_sha is None and second.audit_status == "not_applicable"
    assert first.t_switch == "2026-09-19T00:00:05.400000+00:00" and second.t_decision is None and second.t_switch is None
    print("OK - 다음 trial은 이전 trial의 판정·감사 필드를 물려받지 않음")


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
def test_live_no_action_judgment_and_audit_fields_end_to_end(tmp_path):
    """실제 배포된 recovery-policy를 상대로 run_once() 전체 경로(등록 -> 신호 -> 상태 회수 ->
    감사 회수 -> clear)를 검증하는 no-action smoke(2026-09-19). **preview가 없는(Rollout이
    단일 revision, pauseConditions 비어 있음) 상태에서만 실행할 것** - 그래야
    is_paused_pre_promotion()이 False라 anomaly_risk/VLLMTargetDown 신호가 observe_only만
    낼 수 있고 promotion이 절대 나가지 않는다. injector/prober는 가짜라 클러스터에 chaos·부하는
    없고, 실제로 일어나는 부작용은 recovery-policy의 설계된 비동기 감사 커밋(audit-log/
    {run_id}.jsonl)뿐이다. 로컬에서 kubectl port-forward -n vllm-serving svc/recovery-policy
    8080:8080이 켜져 있어야 한다.

    한 trial 안에서 신호 4개를 보낸다: (1) 첫 예측 신호(isolation_forest) (2) 같은 key의 중복
    신호(다른 detector 태그 - 첫 값이 보존돼야 함) (3) 다른 run_id의 예측 신호(배제돼야 함)
    (4) run 시작 이후의 반응형 alert(첫 탐지를 덮어쓰면 안 됨)."""
    from datetime import datetime, timezone

    run_id = "smoke-judgment-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    other_run_id = run_id + "-other"

    def _now_iso():
        return datetime.now(timezone.utc).isoformat()

    def inject():
        def signal(rid, detector):
            requests.post(f"{RECOVERY_POLICY_URL}/signal", timeout=10, json={
                "signal_type": "anomaly_risk", "score": -0.05, "timestamp": _now_iso(),
                "experiment_run_id": rid, "detector": detector}).raise_for_status()
        signal(run_id, "isolation_forest")
        signal(run_id, "fixed_threshold")  # 중복(같은 idempotency_key) - 첫 값 보존 확인용
        signal(other_run_id, "isolation_forest")  # 다른 run - 상태에 반영되면 안 됨
        requests.post(f"{RECOVERY_POLICY_URL}/webhooks/alertmanager", timeout=10, json={
            "status": "firing", "alerts": [{
                "status": "firing", "labels": {"alertname": "VLLMTargetDown"}, "annotations": {},
                "startsAt": _now_iso(), "fingerprint": f"smoke-fp-{run_id}"}]}).raise_for_status()

    injector = Injector(prepare=lambda: None, inject=inject, is_started=lambda: True,
                         is_effective=lambda: True, is_done=lambda: True, cleanup=lambda: None)
    prober, _ = _fake_prober()

    result = run_once(
        scenario="dry_run", arm="proposed", rep=1, sequence_index=1, order_seed=1,
        injector=injector, prober=prober, timeout_sec=15, poll_interval_sec=0.2,
        run_id=run_id, is_pilot=True, results_dir=tmp_path, audit_wait_sec=90, audit_poll_sec=2,
    )

    assert result.outcome == "recovered", (result.outcome, result.invalid_reason, result.notes)
    assert result.detected is True and result.judgment_source == "live_state"
    assert result.detection_source == "predictive" and result.detector == "isolation_forest", \
        "중복 신호(fixed_threshold 태그)나 뒤이은 반응형 alert가 첫 탐지 정보를 덮어쓰면 안 됨"
    assert result.action == "observe_only" and result.decision_outcome == "no_action"
    assert result.promotion_verified is None and result.t_api_request is None
    # t_decision/t_switch(2026-09-19 추가)를 지원하는 recovery-policy 이미지가 배포된 뒤에만 통과한다 -
    # observe-only여도 t_decision은 기록되고, promotion이 없으니 t_switch는 null이다.
    assert result.t_decision is not None and result.t_switch is None
    assert result.idempotency_key == f"{run_id}:anomaly_risk"
    assert result.audit_status == "complete", (result.audit_status, result.audit_status_reason)
    assert result.commit_sha and result.t_audit_push and result.t_audit_write and result.audit_record_id
    state = requests.get(f"{RECOVERY_POLICY_URL}/admin/experiment-run/timing", timeout=10).json()
    assert state["run_id"] is None and state["detected"] is None, "clear 뒤 다음 trial엔 상태가 남지 않아야 함"

    records = requests.get(f"{RECOVERY_POLICY_URL}/admin/audit/{run_id}", timeout=10).json()["records"]
    assert [r["outcome"] for r in records] == ["no_action", "skipped_duplicate", "no_action"], records
    assert records[0]["record_id"] == result.audit_record_id, "primary는 skipped_duplicate가 아니라 첫 판정 기록"
    assert records[0]["evidence"] == {"experiment_run_id": run_id, "detector": "isolation_forest"}
    assert records[2]["evidence"] == {"experiment_run_id": run_id}, "반응형 기록은 evidence로 run에 귀속"
    other = requests.get(f"{RECOVERY_POLICY_URL}/admin/audit/{other_run_id}", timeout=10).json()["records"]
    assert len(other) == 1 and other[0]["evidence"]["experiment_run_id"] == other_run_id
    print("OK - live no-action smoke:", run_id, "commit", result.commit_sha[:8])


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
        test_prepare_harness_corrupted_propagates_and_marks_invalid_run,
        test_preview_prep_info_populates_result_even_on_prepare_failure,
        test_prober_still_alive_after_stop_raises_harness_corrupted,
        test_baseline_gate_waits_until_ready_then_injects,
        test_baseline_gate_never_ready_blocks_injection_and_invalidates,
        test_baseline_gate_skipped_when_hook_unimplemented,
        test_slo_stage_populated_when_classify_stage_implemented,
        test_detection_and_action_stage_stay_none_without_source_timestamp,
        test_stage_fields_stay_none_when_classify_stage_unimplemented,
        test_classify_stage_exception_does_not_break_trial,
        test_detector_starts_only_after_baseline_ready,
        test_injection_starts_only_after_detector_start,
        test_detector_crash_marks_invalid_run,
        test_detector_stopped_even_when_injector_raises,
        test_detector_stopped_on_timeout,
        test_detector_process_field_records_name,
        test_detector_none_leaves_detector_process_null,
        test_non_native_timing_fetch_populates_fields,
        test_non_native_timing_fetch_failure_marks_invalid_run,
        test_non_native_timing_run_id_mismatch_marks_invalid_run,
        test_timing_fetch_failure_does_not_override_existing_invalid_reason,
        test_native_arm_never_calls_recovery_policy,
        test_detection_and_action_stage_computed_from_recovered_timing,
        test_predictive_promotion_fields_populated_from_authoritative_state,
        test_reactive_fallback_fields_populated,
        test_observe_only_fields_populated,
        test_no_detection_is_recorded_as_authoritative_default_and_needs_no_audit,
        test_audit_push_pending_keeps_outcome_and_action_and_leaves_nulls,
        test_audit_push_completes_during_bounded_wait,
        test_audit_failed_recorded_with_reason_without_invalidating_trial,
        test_audit_query_failure_is_pending_not_invalid_run,
        test_state_fetched_before_context_clear,
        test_in_flight_decision_settles_before_being_recorded,
        test_in_flight_decision_never_settling_is_noted_not_guessed,
        test_state_query_failure_leaves_judgment_non_authoritative,
        test_next_trial_does_not_inherit_previous_judgment,
    )
    live_cluster_tests = (
        test_real_experiment_context_registration_non_native_arm,
        test_live_no_action_judgment_and_audit_fields_end_to_end,
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
