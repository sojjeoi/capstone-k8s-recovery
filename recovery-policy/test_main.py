#!/usr/bin/env python3
"""main.py의 두 입력 어댑터 + process_signal() 파이프라인 전체를 확인.
실제 K8s 클러스터를 쓰지 않도록 is_paused_pre_promotion/promote는
monkeypatch로 대체한다 - main.py가 `from rollouts_client import ...`로
가져다 썼으므로 main 모듈 네임스페이스를 patch해야 실제로 먹는다.

git_client.enqueue/start_worker도 전체 모듈 단위로 no-op patch한다 - 실제
git clone/PVC 쓰기는 이 파일 책임이 아니라 test_git_client.py 몫(로컬 bare
저장소로 별도 검증)."""
import sys
from unittest.mock import patch

sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient

import safety
from main import app

patch("main.git_client.start_worker", lambda: None).start()
patch("main.git_client.enqueue", lambda signal, record: None).start()

client = TestClient(app)


def _reset_state():
    if safety.STATE_FILE.exists():
        safety.STATE_FILE.unlink()


def test_healthz():
    resp = client.get("/healthz")
    assert resp.json() == {"status": "ok"}
    print("OK - /healthz")


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


if __name__ == "__main__":
    test_healthz()
    test_anomaly_signal_observe_only_when_preview_not_ready()
    test_anomaly_signal_promotes_when_preview_ready()
    test_promote_unverified_logged_correctly()
    test_duplicate_signal_skipped()
    test_action_cooldown_blocks_repeat_promotion()
    test_alertmanager_webhook_end_to_end()
    test_unknown_signal_type_via_alertmanager()
    _reset_state()
    print("모두 통과")
