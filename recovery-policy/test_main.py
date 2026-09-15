#!/usr/bin/env python3
"""main.py의 두 입력 어댑터 + process_signal() 파이프라인 전체를 확인.
실제 K8s 클러스터를 쓰지 않도록 is_paused_pre_promotion/promote는
monkeypatch로 대체한다 - main.py가 `from rollouts_client import ...`로
가져다 썼으므로 main 모듈 네임스페이스를 patch해야 실제로 먹는다.

git_client.enqueue/start_worker도 전체 모듈 단위로 no-op patch한다 - 실제
git clone/PVC 쓰기는 이 파일 책임이 아니라 test_git_client.py 몫(로컬 bare
저장소로 별도 검증)."""
import sys
from unittest.mock import MagicMock, patch

sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient

import safety
from main import app

patch("main.git_client.start_worker", lambda: None).start()
mock_enqueue = MagicMock()
patch("main.git_client.enqueue", mock_enqueue).start()

client = TestClient(app)


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


if __name__ == "__main__":
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
    _reset_state()
    print("모두 통과")
