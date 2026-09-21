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
    assert timing == {
        "run_id": None, "t_detection": None, "t_decision": None, "t_api_request": None, "t_switch": None,
        "detected": None, "detection_source": None, "detector": None, "action": None,
        "decision_outcome": None, "idempotency_key": None, "promotion_verified": None,
    }
    print("OK - 활성 실험이 없으면 상태 엔드포인트가 전부 null(2026-09-19 확장 필드 포함)")


def _register_run(run_id, arm="proposed", started_at="2026-09-19T10:00:00+00:00", extra=None):
    ctx = {"run_id": run_id, "scenario": "load_ramp", "arm": arm, "rep": 1, "started_at": started_at}
    ctx.update(extra or {})
    assert client.post("/admin/experiment-run", json=ctx).status_code == 200


def _state():
    return client.get("/admin/experiment-run/timing").json()


def _clear_run(run_id):
    client.post("/admin/experiment-run/clear", params={"run_id": run_id})


def _predictive_payload(run_id, ts="2026-09-19T10:00:05+00:00", detector="isolation_forest",
                         signal_type="anomaly_risk"):
    return {"signal_type": signal_type, "score": -0.05, "timestamp": ts,
            "experiment_run_id": run_id, "detector": detector}


def _alert_payload(fingerprint, alertname="VLLMTargetDown", starts_at="2026-09-19T10:00:07Z"):
    return {"status": "firing", "alerts": [{
        "status": "firing", "labels": {"alertname": alertname}, "annotations": {},
        "startsAt": starts_at, "fingerprint": fingerprint,
    }]}


_PROMOTE_OK = {"method": "cli", "requested": True, "verified": True}


def test_state_predictive_promotion_success():
    _reset_state()
    _register_run("state-pred-promote-01")
    before = _state()
    assert before["run_id"] == "state-pred-promote-01"
    assert before["detected"] is False
    assert before["detection_source"] is None and before["action"] is None and before["promotion_verified"] is None

    with patch("main.is_paused_pre_promotion", return_value=True), patch("main.promote", return_value=_PROMOTE_OK):
        client.post("/signal", json=_predictive_payload("state-pred-promote-01"))

    state = _state()
    assert state["detected"] is True
    assert state["detection_source"] == "predictive"
    assert state["detector"] == "isolation_forest"
    assert state["action"] == "promote_preview"
    assert state["decision_outcome"] == "executed_verified"
    assert state["idempotency_key"] == "state-pred-promote-01:anomaly_risk"
    assert state["promotion_verified"] is True
    assert state["t_detection"] is not None and state["t_api_request"] is not None

    record = mock_enqueue.call_args.args[1]
    assert record.evidence == {"experiment_run_id": "state-pred-promote-01", "detector": "isolation_forest"}, \
        "감사기록 evidence에 run 귀속 근거와 detector가 남아야 함"
    print("OK - 예측 경로 promotion 성공 시 판정·조치 필드가 authoritative 상태에 기록됨")
    _clear_run("state-pred-promote-01")


def test_state_predictive_promotion_carries_provenance_fields_into_audit_evidence():
    # §92 - score_server.py가 evaluation_seq/correlation_id/model_version/
    # model_hash/feature_schema_hash/threshold/consecutive_count를 함께
    # 보내면(전부 선택 필드) 정책 결정에는 영향 없이 감사기록 evidence에만
    # 그대로 반영돼야 한다.
    _reset_state()
    _register_run("state-pred-provenance-01")
    payload = _predictive_payload("state-pred-provenance-01")
    payload.update({
        "correlation_id": "corr-xyz", "evaluation_seq": 26, "model_version": "v3.2b",
        "model_hash": "deadbeef", "feature_schema_hash": "cafef00d",
        "threshold": -0.0742929709960305, "consecutive_count": 3,
    })
    with patch("main.is_paused_pre_promotion", return_value=True), patch("main.promote", return_value=_PROMOTE_OK):
        resp = client.post("/signal", json=payload)
    assert resp.json()["outcome"] == "executed_verified"

    record = mock_enqueue.call_args.args[1]
    assert record.evidence == {
        "experiment_run_id": "state-pred-provenance-01", "detector": "isolation_forest",
        "correlation_id": "corr-xyz", "evaluation_seq": 26, "model_version": "v3.2b",
        "model_hash": "deadbeef", "feature_schema_hash": "cafef00d",
        "threshold": -0.0742929709960305, "consecutive_count": 3,
    }, "provenance 필드가 감사기록 evidence에 그대로 반영돼야 함"
    print("OK - signal payload provenance가 정책 결정 변경 없이 감사기록 evidence에 그대로 전달됨")
    _clear_run("state-pred-provenance-01")


def test_state_reactive_fallback_promotion():
    _reset_state()
    _register_run("state-reactive-promote-01")
    with patch("main.is_paused_pre_promotion", return_value=True), patch("main.promote", return_value=_PROMOTE_OK):
        client.post("/webhooks/alertmanager", json=_alert_payload("fp-reactive-promote"))

    state = _state()
    assert state["detected"] is True
    assert state["detection_source"] == "reactive"
    assert state["detector"] == "alertmanager"
    assert state["action"] == "promote_preview"
    assert state["decision_outcome"] == "executed_verified"
    assert state["idempotency_key"].startswith("fp-reactive-promote:"), \
        "반응형 alert의 idempotency_key는 fingerprint:startsAt 형식(run_id 없음)"
    assert state["promotion_verified"] is True

    record = mock_enqueue.call_args.args[1]
    assert record.evidence == {"experiment_run_id": "state-reactive-promote-01"}, \
        "run_id를 못 담는 반응형 key 대신 evidence로 귀속 근거를 남겨야 함(detector 태그는 없음)"
    print("OK - 반응형 fallback promotion도 detection_source=reactive/detector=alertmanager로 기록됨")
    _clear_run("state-reactive-promote-01")


def test_state_observe_only():
    _reset_state()
    _register_run("state-observe-01")
    with patch("main.is_paused_pre_promotion", return_value=False):
        client.post("/signal", json=_predictive_payload("state-observe-01", detector="fixed_threshold"))
    state = _state()
    assert state["detected"] is True
    assert state["detector"] == "fixed_threshold"
    assert state["action"] == "observe_only"
    assert state["decision_outcome"] == "no_action"
    assert state["promotion_verified"] is None, "조치를 실행하지 않았으면 promotion_verified는 null"
    assert state["t_api_request"] is None
    print("OK - observe-only는 탐지·판정만 기록하고 promotion 필드는 null")
    _clear_run("state-observe-01")


def test_state_no_detection_stays_default():
    _reset_state()
    _register_run("state-nodetect-01")
    state = _state()
    assert state["run_id"] == "state-nodetect-01"
    assert state["detected"] is False, "신호가 없으면 detected=false(기본값이 아니라 authoritative 상태)"
    for key in ("detection_source", "detector", "action", "decision_outcome", "idempotency_key",
                "promotion_verified", "t_detection", "t_api_request"):
        assert state[key] is None, key
    print("OK - 미탐지: detected=false, 나머지 판정 필드는 전부 null")
    _clear_run("state-nodetect-01")


def test_state_duplicate_preserves_first_values():
    _reset_state()
    _register_run("state-dup-01")
    payload = _predictive_payload("state-dup-01")
    with patch("main.is_paused_pre_promotion", return_value=False):
        client.post("/signal", json=payload)
        first = _state()
        dup = client.post("/signal", json={**payload, "detector": "fixed_threshold"})  # 같은 key, 다른 detector 태그
    assert dup.json()["outcome"] == "skipped_duplicate"
    assert _state() == first, "중복 신호가 최초 탐지·판정 정보(detector 포함)를 덮어쓰면 안 됨"
    print("OK - 중복 신호 후에도 최초 값 전부 보존(skipped_duplicate는 primary 아님)")
    _clear_run("state-dup-01")


def test_state_executed_action_takes_priority_over_observe_only_but_keeps_first_detection():
    _reset_state()
    _register_run("state-priority-01")
    with patch("main.is_paused_pre_promotion", return_value=False):
        client.post("/signal", json=_predictive_payload("state-priority-01"))  # 1) preview 없음 -> observe_only
    first = _state()
    assert first["action"] == "observe_only"

    with patch("main.is_paused_pre_promotion", return_value=True), patch("main.promote", return_value=_PROMOTE_OK):
        client.post("/webhooks/alertmanager", json=_alert_payload("fp-priority"))  # 2) 이번엔 실제 promotion
    second = _state()
    assert second["action"] == "promote_preview"
    assert second["decision_outcome"] == "executed_verified"
    assert second["promotion_verified"] is True
    assert second["idempotency_key"].startswith("fp-priority:")
    # 최초 유효 탐지 정보는 그대로(먼저 온 건 예측 경로였다)
    assert second["detection_source"] == "predictive" and second["detector"] == "isolation_forest"
    assert second["t_detection"] == first["t_detection"]

    # 3) 이후 observe-only/cooldown-skip 기록이 와도 실행된 조치 정보를 내리지 않는다
    with patch("main.is_paused_pre_promotion", return_value=True), patch("main.promote", return_value=_PROMOTE_OK):
        later = client.post("/webhooks/alertmanager", json=_alert_payload("fp-priority-later"))
    assert later.json()["processed"][0]["outcome"] == "skipped_cooldown"
    assert _state() == second, "실행된 조치 뒤의 skipped_cooldown 기록이 primary를 대체하면 안 됨"
    print("OK - 실행된 action이 observe-only보다 우선하고, 최초 탐지 정보는 유지됨")
    _clear_run("state-priority-01")


def test_state_unverified_promotion_reported_as_false():
    _reset_state()
    _register_run("state-unverified-01")
    with patch("main.is_paused_pre_promotion", return_value=True), \
         patch("main.promote", return_value={"method": "cli", "requested": True, "verified": False}):
        client.post("/signal", json=_predictive_payload("state-unverified-01"))
    state = _state()
    assert state["action"] == "promote_preview"
    assert state["decision_outcome"] == "executed_unverified"
    assert state["promotion_verified"] is False, "실행했지만 selector 검증 실패는 null이 아니라 false"
    print("OK - promotion 실행 + selector 검증 실패는 promotion_verified=false")
    _clear_run("state-unverified-01")


def test_state_excludes_other_run_and_stale_signals():
    _reset_state()
    _register_run("state-isolation-01", started_at="2026-09-19T11:00:00+00:00")
    with patch("main.is_paused_pre_promotion", return_value=True), patch("main.promote", return_value=_PROMOTE_OK):
        client.post("/signal", json=_predictive_payload("some-other-run", ts="2026-09-19T11:00:05+00:00"))
        client.post("/webhooks/alertmanager", json=_alert_payload("fp-stale", starts_at="2026-09-19T10:59:00Z"))
    state = _state()
    assert state["detected"] is False
    assert state["detector"] is None and state["action"] is None and state["decision_outcome"] is None
    assert state["t_decision"] is None and state["t_switch"] is None, "다른 run·stale 신호는 t_decision/t_switch도 안 채움"
    print("OK - 다른 run_id·stale alert는 판정·조치·timing 필드에 전혀 반영되지 않음")
    _clear_run("state-isolation-01")


def test_state_not_carried_into_next_trial_after_clear():
    _reset_state()
    _register_run("state-carry-01")
    with patch("main.is_paused_pre_promotion", return_value=True), patch("main.promote", return_value=_PROMOTE_OK):
        client.post("/signal", json=_predictive_payload("state-carry-01"))
    assert _state()["action"] == "promote_preview"
    _clear_run("state-carry-01")

    _register_run("state-carry-02", started_at="2026-09-19T12:00:00+00:00")
    state = _state()
    assert state["run_id"] == "state-carry-02"
    assert state["detected"] is False
    for key in ("detection_source", "detector", "action", "decision_outcome", "idempotency_key",
                "promotion_verified", "t_detection", "t_decision", "t_api_request", "t_switch"):
        assert state[key] is None, f"이전 trial의 {key}가 다음 trial로 새면 안 됨"
    print("OK - context clear 뒤 다음 trial엔 이전 판정·조치 상태가 남지 않음")
    _clear_run("state-carry-02")


def test_reregister_same_run_id_preserves_detection_state_and_ignores_forged_fields():
    _reset_state()
    _register_run("state-rereg-01", extra={"detector": "forged", "action": "promote_preview",
                                             "decision_outcome": "executed_verified"})
    forged = _state()
    assert forged["detector"] is None and forged["action"] is None and forged["decision_outcome"] is None, \
        "등록 요청 본문의 판정·조치 필드는 무시돼야 함(process_signal만이 채울 수 있는 authoritative 값)"

    with patch("main.is_paused_pre_promotion", return_value=False):
        client.post("/signal", json=_predictive_payload("state-rereg-01"))
    recorded = _state()
    _register_run("state-rereg-01")  # 같은 run_id 재등록(idempotent)
    assert _state() == recorded, "재등록이 이미 기록된 첫 탐지·판정 상태를 지우면 안 됨"
    print("OK - 같은 run_id 재등록은 상태를 보존하고, 클라이언트가 보낸 판정 필드는 무시됨")
    _clear_run("state-rereg-01")


def test_audit_endpoint_joins_outbox_and_rejects_bad_run_id(tmp_path):
    import json as _json
    import git_client

    audit_dir = tmp_path / "audit-log"
    audit_dir.mkdir()
    record = {"record_id": "rec-1", "decided_at": "2026-09-19T10:00:06+00:00", "signal_source": "anomaly",
              "signal_type": "anomaly_risk", "idempotency_key": "audit-run-01:anomaly_risk",
              "evidence": {}, "action": "promote_preview", "outcome": "executed_verified",
              "result": {"verified": True}, "reasoning": "x"}
    (audit_dir / "audit-run-01.jsonl").write_text(_json.dumps(record) + "\n", encoding="utf-8")
    (tmp_path / "outbox.json").write_text(_json.dumps({"rec-1": {
        "status": "pushed", "run_id": "audit-run-01", "t_audit_write": "2026-09-19T10:00:06+00:00",
        "commit_sha": "abc123", "t_audit_push": "2026-09-19T10:00:09+00:00", "attempts": 0,
        "last_error": None,
    }}), encoding="utf-8")

    with patch.object(git_client, "AUDIT_LOG_DIR", audit_dir), patch.object(git_client, "OUTBOX_PATH", tmp_path / "outbox.json"):
        body = client.get("/admin/audit/audit-run-01").json()
        assert body["run_id"] == "audit-run-01" and len(body["records"]) == 1
        outbox = body["records"][0]["outbox"]
        assert outbox["status"] == "pushed" and outbox["commit_sha"] == "abc123"
        assert outbox["t_audit_push"] == "2026-09-19T10:00:09+00:00"
        assert client.get("/admin/audit/no-such-run").json()["records"] == [], "기록 없는 run은 빈 목록"
        assert client.get("/admin/audit/bad%20id").status_code == 400, "허용되지 않는 형식은 거부"
        try:
            git_client.read_audit("../escape")
            raise AssertionError("audit-log 밖을 가리키는 run_id는 거부돼야 함")
        except ValueError:
            pass
    print("OK - 감사 조회 엔드포인트: outbox 상태 조인, 빈 결과, 형식 검증, 경로 이탈 차단")


def _promote_verified_now(*args, **kwargs):
    """promote()가 selector 검증에 성공한 "그 순간"의 서버 시각을 verified_at으로 돌려주는 가짜 -
    호출 시점에 찍어야 t_api_request <= t_switch 순서가 실제 promote() 호출 뒤가 된다."""
    from datetime import datetime, timezone
    return {"method": "cli", "requested": True, "verified": True,
            "verified_at": datetime.now(timezone.utc).isoformat()}


def _ts(value):
    from datetime import datetime
    return datetime.fromisoformat(value)


def test_state_decision_time_recorded_for_observe_only_without_api_request_or_switch():
    _reset_state()
    _register_run("state-tdecision-observe-01")
    assert _state()["t_decision"] is None
    with patch("main.is_paused_pre_promotion", return_value=False):
        client.post("/signal", json=_predictive_payload("state-tdecision-observe-01"))
    state = _state()
    assert state["action"] == "observe_only"
    assert state["t_decision"] is not None, "observe-only여도 정책이 action을 확정한 시각을 기록해야 함"
    assert _ts(state["t_detection"]) <= _ts(state["t_decision"])
    assert state["t_api_request"] is None and state["t_switch"] is None, "promotion이 없으면 둘 다 null"
    print("OK - observe-only: t_decision 기록, t_api_request/t_switch는 null")
    _clear_run("state-tdecision-observe-01")


def test_state_decision_time_recorded_even_when_policy_takes_no_action():
    _reset_state()
    _register_run("state-tdecision-unknown-01")
    with patch("main.is_paused_pre_promotion", return_value=False):
        client.post("/signal", json=_predictive_payload("state-tdecision-unknown-01", signal_type="not_a_known_signal"))
    state = _state()
    assert state["decision_outcome"] == "skipped_unknown_signal" and state["action"] is None
    assert state["t_decision"] is not None, "조치 자체가 없는 판정(unknown/rule-out)도 정책이 확정한 사건"
    assert state["t_api_request"] is None and state["t_switch"] is None
    print("OK - 조치 없는 판정(unknown)도 t_decision 기록, t_api_request/t_switch는 null")
    _clear_run("state-tdecision-unknown-01")


def test_state_promotion_path_timing_order_and_switch_time_from_verification():
    _reset_state()
    _register_run("state-tswitch-01")
    with patch("main.is_paused_pre_promotion", return_value=True), patch("main.promote", side_effect=_promote_verified_now):
        client.post("/signal", json=_predictive_payload("state-tswitch-01"))
    state = _state()
    assert state["decision_outcome"] == "executed_verified" and state["promotion_verified"] is True
    for key in ("t_detection", "t_decision", "t_api_request", "t_switch"):
        assert state[key] is not None, key
    assert _ts(state["t_detection"]) <= _ts(state["t_decision"]) <= _ts(state["t_api_request"]) <= _ts(state["t_switch"]), state
    record = mock_enqueue.call_args.args[1]
    assert record.result["verified_at"], "감사기록의 promotion 결과에도 검증 시각이 남아야 함"
    assert _ts(record.result["verified_at"]) == _ts(state["t_switch"]), \
        "t_switch는 promote()가 검증에 성공한 순간에 찍은 시각을 그대로 쓴 것(반환 뒤의 시각이 아님)"
    print("OK - promotion 경로: t_detection <= t_decision <= t_api_request <= t_switch, t_switch=verified_at")
    _clear_run("state-tswitch-01")


def test_state_unverified_promotion_has_no_switch_time():
    _reset_state()
    _register_run("state-tswitch-unverified-01")
    with patch("main.is_paused_pre_promotion", return_value=True), \
         patch("main.promote", return_value={"method": "cli", "requested": True, "verified": False}):
        client.post("/signal", json=_predictive_payload("state-tswitch-unverified-01"))
    state = _state()
    assert state["decision_outcome"] == "executed_unverified" and state["promotion_verified"] is False
    assert state["t_api_request"] is not None and state["t_decision"] is not None
    assert state["t_switch"] is None, "selector 검증이 끝내 실패한 promotion에는 전환 시각을 기록하지 않음"
    print("OK - 검증 실패 promotion: t_api_request는 있고 t_switch는 null")
    _clear_run("state-tswitch-unverified-01")


def test_state_first_timing_values_not_overwritten_by_duplicate_or_later_signals():
    _reset_state()
    _register_run("state-timing-first-01")
    payload = _predictive_payload("state-timing-first-01")
    with patch("main.is_paused_pre_promotion", return_value=True), patch("main.promote", side_effect=_promote_verified_now):
        client.post("/signal", json=payload)
        first = _state()
        dup = client.post("/signal", json=payload)  # 같은 key -> 중복
        later = client.post("/webhooks/alertmanager", json=_alert_payload("fp-timing-later"))  # 다른 신호 -> cooldown-skip
    assert dup.json()["outcome"] == "skipped_duplicate"
    assert later.json()["processed"][0]["outcome"] == "skipped_cooldown"
    after = _state()
    for key in ("t_detection", "t_decision", "t_api_request", "t_switch"):
        assert after[key] == first[key], f"중복·후속 신호가 최초 {key}를 덮어쓰면 안 됨"
    print("OK - 중복·후속 신호가 t_decision/t_api_request/t_switch 최초 값을 덮어쓰지 않음")
    _clear_run("state-timing-first-01")


def test_state_decision_time_is_first_valid_decision_when_later_signal_promotes():
    # 예측 신호가 먼저 observe-only로 판정된 뒤 반응 신호가 promotion을 실행하는 경우: t_decision은
    # 첫 유효 신호의 판정 시각으로 유지되고(최초 값 규칙), t_api_request/t_switch는 실행된 promotion의
    # 것이라 뒤이며, 그래도 promotion 경로의 순서는 성립한다.
    _reset_state()
    _register_run("state-timing-multi-01")
    with patch("main.is_paused_pre_promotion", return_value=False):
        client.post("/signal", json=_predictive_payload("state-timing-multi-01"))
    first = _state()
    assert first["t_decision"] is not None and first["t_api_request"] is None
    with patch("main.is_paused_pre_promotion", return_value=True), patch("main.promote", side_effect=_promote_verified_now):
        client.post("/webhooks/alertmanager", json=_alert_payload("fp-timing-multi"))
    state = _state()
    assert state["t_decision"] == first["t_decision"], "t_decision은 첫 유효 판정의 시각으로 유지"
    assert state["action"] == "promote_preview" and state["promotion_verified"] is True
    assert _ts(state["t_detection"]) <= _ts(state["t_decision"]) <= _ts(state["t_api_request"]) <= _ts(state["t_switch"])
    print("OK - 첫 판정이 observe-only여도 t_decision 유지, 뒤이은 promotion의 t_api_request/t_switch와 순서 성립")
    _clear_run("state-timing-multi-01")


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
        test_state_predictive_promotion_success()
        test_state_reactive_fallback_promotion()
        test_state_observe_only()
        test_state_no_detection_stays_default()
        test_state_duplicate_preserves_first_values()
        test_state_executed_action_takes_priority_over_observe_only_but_keeps_first_detection()
        test_state_unverified_promotion_reported_as_false()
        test_state_excludes_other_run_and_stale_signals()
        test_state_not_carried_into_next_trial_after_clear()
        test_reregister_same_run_id_preserves_detection_state_and_ignores_forged_fields()
        test_state_decision_time_recorded_for_observe_only_without_api_request_or_switch()
        test_state_decision_time_recorded_even_when_policy_takes_no_action()
        test_state_promotion_path_timing_order_and_switch_time_from_verification()
        test_state_unverified_promotion_has_no_switch_time()
        test_state_first_timing_values_not_overwritten_by_duplicate_or_later_signals()
        test_state_decision_time_is_first_valid_decision_when_later_signal_promotes()
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as _d:
            test_audit_endpoint_joins_outbox_and_rejects_bad_run_id(Path(_d))
        _reset_state()
        print("모두 통과")
