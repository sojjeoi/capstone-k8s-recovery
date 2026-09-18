#!/usr/bin/env python3
"""main.py의 두 입력 어댑터 + process_signal() 파이프라인 전체를 확인.
실제 K8s 클러스터를 쓰지 않도록 is_paused_pre_promotion/promote는
monkeypatch로 대체한다 - main.py가 `from rollouts_client import ...`로
가져다 썼으므로 main 모듈 네임스페이스를 patch해야 실제로 먹는다.

git_client.enqueue/start_worker도 전체 모듈 단위로 no-op patch한다 - 실제
git clone/PVC 쓰기는 이 파일 책임이 아니라 test_git_client.py 몫(로컬 bare
저장소로 별도 검증).

2026-09-19 수정: 이 patch를 예전엔 `patch(...).start()`만 호출하고
`.stop()`이 없어 프로세스 전역에 영구히 남았다 - 이 파일 자신의 테스트는
전부 통과하지만(재현 스크립트 참고), 같은 pytest 세션에서 test_main.py
"다음"에 수집되는 다른 파일(예: test_git_client.py)이 실제 git_client
함수 대신 이 mock을 계속 보게 되는 문제가 있었다(`git stash`로 이번 세션
변경분을 전부 제거한 원본 코드에서도 동일 재현 확인 - 기존부터 있던
결함). 이제 `_patch_git_client` autouse fixture가 매 테스트 함수 실행
직전에 patch를 걸고 직후에 반드시 원복한다 - `mock_enqueue`는 기존
테스트들이 전역 이름으로 그대로 참조할 수 있게 fixture가 매번 새로
할당한다(테스트 간 호출 이력도 자연히 격리됨, 부수 효과로 얻는 개선)."""
import sys
from unittest.mock import MagicMock, patch

sys.stdout.reconfigure(encoding="utf-8")

import pytest
from fastapi.testclient import TestClient

import safety
from main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _patch_git_client():
    global mock_enqueue
    with patch("main.git_client.start_worker", lambda: None), \
         patch("main.git_client.enqueue") as mock_enqueue:
        yield


def _reset_state():
    if safety.STATE_FILE.exists():
        safety.STATE_FILE.unlink()


def test_healthz():
    resp = client.get("/healthz")
    assert resp.json() == {"status": "ok"}
    print("OK - /healthz")


def test_quiescent_true_when_no_active_alerts():
    mock_resp = MagicMock()
    mock_resp.json.return_value = []
    mock_resp.raise_for_status.return_value = None
    with patch("main.requests.get", return_value=mock_resp):
        resp = client.get("/admin/quiescent")
    assert resp.json() == {"quiescent": True, "active_count": 0}
    print("OK - 활성 alert 없음 -> quiescent=True")


def test_quiescent_false_when_alertmanager_unreachable():
    # 연결 자체가 안 되면 "조용하다"로 오판하지 않고 안전 쪽(False)으로 응답해야 함
    with patch("main.requests.get", side_effect=ConnectionError("연결 실패 시뮬레이션")):
        resp = client.get("/admin/quiescent")
    body = resp.json()
    assert body["quiescent"] is False
    assert body["active_count"] is None
    print("OK - Alertmanager 연결 실패 -> quiescent=False(안전 쪽)")


def test_anomaly_signal_observe_only_when_preview_not_ready():
    _reset_state()
    with patch("main.is_paused_pre_promotion", return_value=False):
        resp = client.post("/signal", json={
            "signal_type": "anomaly_risk", "score": -0.05,
            "timestamp": "2026-09-07T00:00:00+00:00",
        })
    body = resp.json()
    assert resp.status_code == 200
    assert body["action"] == "observe_only"
    assert body["outcome"] == "no_action"
    print("OK - anomaly_risk + preview 없음 ->", body["outcome"])


def test_anomaly_signal_promotes_when_preview_ready():
    _reset_state()
    with patch("main.is_paused_pre_promotion", return_value=True), \
         patch("main.promote", return_value={"method": "cli", "requested": True, "verified": True}):
        resp = client.post("/signal", json={
            "signal_type": "anomaly_risk", "score": -0.06,
            "timestamp": "2026-09-07T00:01:00+00:00",
        })
    body = resp.json()
    assert body["action"] == "promote_preview"
    assert body["outcome"] == "executed_verified"
    print("OK - anomaly_risk + preview 준비 ->", body["outcome"])


def test_promote_unverified_logged_correctly():
    _reset_state()
    with patch("main.is_paused_pre_promotion", return_value=True), \
         patch("main.promote", return_value={"method": "cli", "requested": True, "verified": False}):
        resp = client.post("/signal", json={
            "signal_type": "anomaly_risk", "score": -0.07,
            "timestamp": "2026-09-07T00:02:00+00:00",
        })
    body = resp.json()
    assert body["outcome"] == "executed_unverified"
    print("OK - promote 요청은 갔으나 검증 실패 ->", body["outcome"])


def test_duplicate_signal_skipped():
    _reset_state()
    payload = {"signal_type": "anomaly_risk", "score": -0.05, "timestamp": "2026-09-07T00:03:00+00:00"}
    with patch("main.is_paused_pre_promotion", return_value=False):
        first = client.post("/signal", json=payload)
        second = client.post("/signal", json=payload)
    assert first.json()["outcome"] == "no_action"
    assert second.json()["outcome"] == "skipped_duplicate"
    print("OK - 동일 신호 재전송 -> 두 번째는", second.json()["outcome"])


def test_action_cooldown_blocks_repeat_promotion():
    _reset_state()
    with patch("main.is_paused_pre_promotion", return_value=True), \
         patch("main.promote", return_value={"method": "cli", "requested": True, "verified": True}):
        first = client.post("/signal", json={
            "signal_type": "anomaly_risk", "score": -0.08,
            "timestamp": "2026-09-07T00:04:00+00:00",
        })
        second = client.post("/signal", json={
            "signal_type": "anomaly_risk", "score": -0.09,
            "timestamp": "2026-09-07T00:05:00+00:00",  # idempotency_key는 다름 - 중복 차단이 아니라 쿨다운으로 막혀야 함
        })
    assert first.json()["outcome"] == "executed_verified"
    assert second.json()["outcome"] == "skipped_cooldown"
    print("OK - 조치 직후 다른 신호가 와도 쿨다운으로 재promote 차단 ->", second.json()["outcome"])


def test_alertmanager_webhook_end_to_end():
    _reset_state()
    payload = {
        "status": "firing",
        "alerts": [{
            "status": "firing",
            "labels": {"alertname": "VLLMTargetDown"},
            "annotations": {},
            "startsAt": "2026-09-07T00:06:00Z",
            "fingerprint": "e2e-test-fp",
        }],
    }
    with patch("main.is_paused_pre_promotion", return_value=False):
        resp = client.post("/webhooks/alertmanager", json=payload)
    processed = resp.json()["processed"]
    assert len(processed) == 1
    assert processed[0]["signal_type"] == "VLLMTargetDown"
    assert processed[0]["outcome"] == "no_action"
    print("OK - alertmanager webhook end-to-end ->", processed[0]["outcome"])


def test_unknown_signal_type_via_alertmanager():
    _reset_state()
    payload = {
        "status": "firing",
        "alerts": [{
            "status": "firing",
            "labels": {"alertname": "SomeOtherAlert"},
            "annotations": {},
            "startsAt": "2026-09-07T00:07:00Z",
            "fingerprint": "unknown-fp",
        }],
    }
    with patch("main.is_paused_pre_promotion", return_value=False):
        resp = client.post("/webhooks/alertmanager", json=payload)
    processed = resp.json()["processed"]
    assert processed[0]["outcome"] == "skipped_unknown_signal"
    print("OK - 미정의 alert ->", processed[0]["outcome"])


def test_experiment_run_id_injected_into_alertmanager_path():
    # Alertmanager alert 자체엔 experiment_run_id를 실을 자리가 없어서 ambient
    # 등록값이 대신 쓰여야 한다 - 등록된 started_at보다 나중 alert만 대상.
    _reset_state()
    mock_enqueue.reset_mock()
    ctx = {"run_id": "pilot-run-001", "scenario": "load_ramp", "arm": "proposed",
           "rep": 1, "started_at": "2026-09-16T00:00:00+00:00"}
    set_resp = client.post("/admin/experiment-run", json=ctx)
    assert set_resp.json()["status"] == "active"

    payload = {
        "status": "firing",
        "alerts": [{
            "status": "firing",
            "labels": {"alertname": "VLLMTargetDown"},
            "annotations": {},
            "startsAt": "2026-09-16T00:05:00Z",  # 등록 시각보다 나중
            "fingerprint": "run-id-test-fp",
        }],
    }
    with patch("main.is_paused_pre_promotion", return_value=False):
        client.post("/webhooks/alertmanager", json=payload)

    signal_arg = mock_enqueue.call_args.args[0]
    assert signal_arg.raw["experiment_run_id"] == "pilot-run-001"
    print("OK - ambient run_id가 Alertmanager 경로 signal.raw에 주입됨")

    clear_resp = client.post("/admin/experiment-run/clear", params={"run_id": "pilot-run-001"})
    assert clear_resp.json()["status"] == "cleared"
    print("OK - 일치하는 run_id로 clear 성공")


def test_experiment_run_idempotent_reregister_and_conflict():
    ctx1 = {"run_id": "run-a", "scenario": "pod_kill", "arm": "native",
            "rep": 1, "started_at": "2026-09-16T00:00:00+00:00"}
    assert client.post("/admin/experiment-run", json=ctx1).status_code == 200
    assert client.post("/admin/experiment-run", json=ctx1).status_code == 200
    print("OK - 같은 run_id 재등록은 idempotent(200)")

    ctx2 = {**ctx1, "run_id": "run-b"}
    assert client.post("/admin/experiment-run", json=ctx2).status_code == 409
    print("OK - 다른 run_id가 활성 중일 때 새 등록은 409")

    assert client.post("/admin/experiment-run/clear", params={"run_id": "run-b"}).status_code == 409
    print("OK - 불일치 run_id의 clear는 409(현재 활성값 안 지워짐)")

    assert client.post("/admin/experiment-run/clear", params={"run_id": "run-a"}).json()["status"] == "cleared"


def test_reset_cooldown_requires_quiescent_and_no_active_context():
    _reset_state()
    quiet = MagicMock()
    quiet.json.return_value = []
    quiet.raise_for_status.return_value = None

    ctx = {"run_id": "cooldown-test", "scenario": "pod_kill", "arm": "native",
           "rep": 1, "started_at": "2026-09-16T00:00:00+00:00"}
    client.post("/admin/experiment-run", json=ctx)
    with patch("main.requests.get", return_value=quiet):
        resp = client.post("/admin/reset-cooldown")
    assert resp.status_code == 409
    print("OK - 활성 context 있으면 cooldown 초기화 거부(409)")
    client.post("/admin/experiment-run/clear", params={"run_id": "cooldown-test"})

    busy = MagicMock()
    busy.json.return_value = [{"labels": {"alertname": "VLLMTargetDown"}}]
    busy.raise_for_status.return_value = None
    with patch("main.requests.get", return_value=busy):
        resp = client.post("/admin/reset-cooldown")
    assert resp.status_code == 409
    print("OK - quiescent 아니면 cooldown 초기화 거부(409)")

    safety.mark_action_taken()
    assert safety.in_action_cooldown() is True
    with patch("main.requests.get", return_value=quiet):
        resp = client.post("/admin/reset-cooldown")
    assert resp.json()["status"] == "cooldown_reset"
    assert safety.in_action_cooldown() is False
    print("OK - 활성 context 없고 quiescent하면 cooldown 초기화 성공")


def test_get_experiment_run_reflects_current_state():
    _reset_state()
    assert client.get("/admin/experiment-run").json()["current"] is None
    ctx = {"run_id": "check-current", "scenario": "load_ramp", "arm": "fixed_threshold",
           "rep": 1, "started_at": "2026-09-16T00:00:00+00:00"}
    client.post("/admin/experiment-run", json=ctx)
    current = client.get("/admin/experiment-run").json()["current"]
    assert current["run_id"] == "check-current"
    client.post("/admin/experiment-run/clear", params={"run_id": "check-current"})
    assert client.get("/admin/experiment-run").json()["current"] is None
    print("OK - GET /admin/experiment-run이 현재 상태를 정확히 반영")


def test_stale_alert_not_tagged_with_current_run():
    _reset_state()
    mock_enqueue.reset_mock()
    ctx = {"run_id": "run-c", "scenario": "network_degrade", "arm": "fixed_threshold",
           "rep": 2, "started_at": "2026-09-16T10:00:00+00:00"}
    client.post("/admin/experiment-run", json=ctx)

    payload = {
        "status": "firing",
        "alerts": [{
            "status": "firing",
            "labels": {"alertname": "VLLMTargetMissing"},
            "annotations": {},
            "startsAt": "2026-09-16T09:00:00Z",  # 등록 시각보다 이전 - stale
            "fingerprint": "stale-fp",
        }],
    }
    with patch("main.is_paused_pre_promotion", return_value=False):
        client.post("/webhooks/alertmanager", json=payload)

    signal_arg = mock_enqueue.call_args.args[0]
    assert signal_arg.raw.get("experiment_run_id") is None
    print("OK - trial 등록 이전 startsAt을 가진 stale alert는 run_id 태깅 안 됨")

    client.post("/admin/experiment-run/clear", params={"run_id": "run-c"})


def test_predictive_signal_sets_t_detection():
    _reset_state()
    ctx = {"run_id": "timing-predictive-01", "scenario": "load_ramp", "arm": "proposed",
           "rep": 1, "started_at": "2026-09-19T00:00:00+00:00"}
    client.post("/admin/experiment-run", json=ctx)
    assert client.get("/admin/experiment-run/timing").json()["t_detection"] is None

    with patch("main.is_paused_pre_promotion", return_value=False):
        client.post("/signal", json={
            "signal_type": "anomaly_risk", "score": -0.05,
            "timestamp": "2026-09-19T00:00:05+00:00",
            "experiment_run_id": "timing-predictive-01", "detector": "isolation_forest",
        })

    timing = client.get("/admin/experiment-run/timing").json()
    assert timing["run_id"] == "timing-predictive-01"
    assert timing["t_detection"] is not None
    print("OK - 예측 신호(/signal)가 t_detection을 채움:", timing["t_detection"])
    client.post("/admin/experiment-run/clear", params={"run_id": "timing-predictive-01"})


def test_reactive_alert_sets_t_detection():
    _reset_state()
    ctx = {"run_id": "timing-reactive-01", "scenario": "pod_kill", "arm": "fixed_threshold",
           "rep": 1, "started_at": "2026-09-19T01:00:00+00:00"}
    client.post("/admin/experiment-run", json=ctx)

    payload = {
        "status": "firing",
        "alerts": [{
            "status": "firing", "labels": {"alertname": "VLLMTargetDown"}, "annotations": {},
            "startsAt": "2026-09-19T01:00:05Z", "fingerprint": "timing-reactive-fp",
        }],
    }
    with patch("main.is_paused_pre_promotion", return_value=False):
        client.post("/webhooks/alertmanager", json=payload)

    timing = client.get("/admin/experiment-run/timing").json()
    assert timing["run_id"] == "timing-reactive-01"
    assert timing["t_detection"] is not None
    print("OK - 반응형 alert(/webhooks/alertmanager)가 t_detection을 채움")
    client.post("/admin/experiment-run/clear", params={"run_id": "timing-reactive-01"})


def test_duplicate_signal_does_not_overwrite_t_detection():
    _reset_state()
    ctx = {"run_id": "timing-dup-01", "scenario": "load_ramp", "arm": "proposed",
           "rep": 1, "started_at": "2026-09-19T02:00:00+00:00"}
    client.post("/admin/experiment-run", json=ctx)

    payload = {"signal_type": "anomaly_risk", "score": -0.05, "timestamp": "2026-09-19T02:00:05+00:00",
               "experiment_run_id": "timing-dup-01"}
    with patch("main.is_paused_pre_promotion", return_value=False):
        client.post("/signal", json=payload)
        first_timing = client.get("/admin/experiment-run/timing").json()
        second_resp = client.post("/signal", json=payload)  # 동일 payload -> 동일 idempotency_key -> 중복
    assert second_resp.json()["outcome"] == "skipped_duplicate"
    second_timing = client.get("/admin/experiment-run/timing").json()
    assert second_timing["t_detection"] == first_timing["t_detection"], "중복 신호가 t_detection을 갱신하면 안 됨"
    print("OK - 중복(재전송) 신호는 t_detection을 덮어쓰지 않음")
    client.post("/admin/experiment-run/clear", params={"run_id": "timing-dup-01"})


def test_stale_and_different_run_id_signals_do_not_set_t_detection():
    _reset_state()
    ctx = {"run_id": "timing-isolation-01", "scenario": "network_degrade", "arm": "fixed_threshold",
           "rep": 1, "started_at": "2026-09-19T03:00:00+00:00"}
    client.post("/admin/experiment-run", json=ctx)

    # 1) 다른 run_id를 직접 실은 예측 신호(예: 정리 안 된 이전 trial의 detector가
    # 계속 보내는 신호를 흉내)
    with patch("main.is_paused_pre_promotion", return_value=False):
        client.post("/signal", json={
            "signal_type": "anomaly_risk", "score": -0.05, "timestamp": "2026-09-19T03:00:05+00:00",
            "experiment_run_id": "some-other-run-id",
        })
    assert client.get("/admin/experiment-run/timing").json()["t_detection"] is None, \
        "다른 run_id를 실은 신호가 t_detection을 채우면 안 됨"

    # 2) stale alert(등록 시각보다 이전 startsAt)
    payload = {
        "status": "firing",
        "alerts": [{
            "status": "firing", "labels": {"alertname": "VLLMTargetMissing"}, "annotations": {},
            "startsAt": "2026-09-19T02:59:00Z", "fingerprint": "timing-stale-fp",  # 등록(03:00:00)보다 이전
        }],
    }
    with patch("main.is_paused_pre_promotion", return_value=False):
        client.post("/webhooks/alertmanager", json=payload)
    assert client.get("/admin/experiment-run/timing").json()["t_detection"] is None, \
        "stale alert가 t_detection을 채우면 안 됨"
    print("OK - 다른 run_id·stale alert 둘 다 t_detection을 채우지 않음")
    client.post("/admin/experiment-run/clear", params={"run_id": "timing-isolation-01"})


def test_no_action_leaves_t_api_request_null():
    _reset_state()
    ctx = {"run_id": "timing-noaction-01", "scenario": "load_ramp", "arm": "proposed",
           "rep": 1, "started_at": "2026-09-19T04:00:00+00:00"}
    client.post("/admin/experiment-run", json=ctx)
    with patch("main.is_paused_pre_promotion", return_value=False):  # preview 없음 -> observe_only
        client.post("/signal", json={
            "signal_type": "anomaly_risk", "score": -0.05, "timestamp": "2026-09-19T04:00:05+00:00",
            "experiment_run_id": "timing-noaction-01",
        })
    timing = client.get("/admin/experiment-run/timing").json()
    assert timing["t_detection"] is not None
    assert timing["t_api_request"] is None
    print("OK - 조치가 없으면(observe_only) t_api_request는 null로 남음")
    client.post("/admin/experiment-run/clear", params={"run_id": "timing-noaction-01"})


def test_promotion_sets_t_api_request_after_t_detection():
    _reset_state()
    ctx = {"run_id": "timing-promote-01", "scenario": "load_ramp", "arm": "fixed_threshold",
           "rep": 1, "started_at": "2026-09-19T05:00:00+00:00"}
    client.post("/admin/experiment-run", json=ctx)
    with patch("main.is_paused_pre_promotion", return_value=True), \
         patch("main.promote", return_value={"method": "cli", "requested": True, "verified": True}):
        client.post("/signal", json={
            "signal_type": "anomaly_risk", "score": -0.05, "timestamp": "2026-09-19T05:00:05+00:00",
            "experiment_run_id": "timing-promote-01",
        })
    timing = client.get("/admin/experiment-run/timing").json()
    assert timing["t_detection"] is not None
    assert timing["t_api_request"] is not None
    from datetime import datetime as _dt
    assert _dt.fromisoformat(timing["t_detection"]) <= _dt.fromisoformat(timing["t_api_request"])
    print("OK - 실제 promotion 시 t_api_request가 채워지고 t_detection <= t_api_request")
    client.post("/admin/experiment-run/clear", params={"run_id": "timing-promote-01"})


def test_context_clear_removes_timing_for_next_trial():
    _reset_state()
    ctx1 = {"run_id": "timing-clear-01", "scenario": "load_ramp", "arm": "proposed",
            "rep": 1, "started_at": "2026-09-19T06:00:00+00:00"}
    client.post("/admin/experiment-run", json=ctx1)
    with patch("main.is_paused_pre_promotion", return_value=False):
        client.post("/signal", json={
            "signal_type": "anomaly_risk", "score": -0.05, "timestamp": "2026-09-19T06:00:05+00:00",
            "experiment_run_id": "timing-clear-01",
        })
    assert client.get("/admin/experiment-run/timing").json()["t_detection"] is not None
    client.post("/admin/experiment-run/clear", params={"run_id": "timing-clear-01"})

    ctx2 = {"run_id": "timing-clear-02", "scenario": "load_ramp", "arm": "proposed",
            "rep": 2, "started_at": "2026-09-19T06:10:00+00:00"}
    client.post("/admin/experiment-run", json=ctx2)
    timing2 = client.get("/admin/experiment-run/timing").json()
    assert timing2["run_id"] == "timing-clear-02"
    assert timing2["t_detection"] is None
    assert timing2["t_api_request"] is None
    print("OK - context clear 후 다음 trial 등록엔 이전 timing이 남지 않음")
    client.post("/admin/experiment-run/clear", params={"run_id": "timing-clear-02"})


def test_timing_endpoint_null_when_no_active_experiment():
    current = client.get("/admin/experiment-run").json()["current"]
    if current is not None:
        client.post("/admin/experiment-run/clear", params={"run_id": current["run_id"]})
    timing = client.get("/admin/experiment-run/timing").json()
    assert timing == {"run_id": None, "t_detection": None, "t_api_request": None}
    print("OK - 활성 실험이 없으면 timing 엔드포인트가 전부 null")


if __name__ == "__main__":
    # pytest면 위 _patch_git_client autouse fixture가 매 테스트마다 자동으로
    # 걸어주지만, 직접 실행(python test_main.py)에선 fixture가 안 돌므로
    # 전체를 감싸는 이 with 블록이 동일한 역할을 한다(2026-09-19 수정).
    with patch("main.git_client.start_worker", lambda: None), \
         patch("main.git_client.enqueue") as mock_enqueue:
        test_healthz()
        test_quiescent_true_when_no_active_alerts()
        test_quiescent_false_when_alertmanager_unreachable()
        test_anomaly_signal_observe_only_when_preview_not_ready()
        test_anomaly_signal_promotes_when_preview_ready()
        test_promote_unverified_logged_correctly()
        test_duplicate_signal_skipped()
        test_action_cooldown_blocks_repeat_promotion()
        test_alertmanager_webhook_end_to_end()
        test_unknown_signal_type_via_alertmanager()
        test_experiment_run_id_injected_into_alertmanager_path()
        test_experiment_run_idempotent_reregister_and_conflict()
        test_reset_cooldown_requires_quiescent_and_no_active_context()
        test_get_experiment_run_reflects_current_state()
        test_stale_alert_not_tagged_with_current_run()
        test_predictive_signal_sets_t_detection()
        test_reactive_alert_sets_t_detection()
        test_duplicate_signal_does_not_overwrite_t_detection()
        test_stale_and_different_run_id_signals_do_not_set_t_detection()
        test_no_action_leaves_t_api_request_null()
        test_promotion_sets_t_api_request_after_t_detection()
        test_context_clear_removes_timing_for_next_trial()
        test_timing_endpoint_null_when_no_active_experiment()
        _reset_state()
        print("모두 통과")
