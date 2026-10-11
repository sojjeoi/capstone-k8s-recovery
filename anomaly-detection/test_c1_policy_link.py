"""c1_policy_link 오프라인 테스트 - payload 생성·수신자 확인·전송 결과 구분. 네트워크 호출 없음(가짜 get/post)."""
import sys
from pathlib import Path

import pytest
import requests

import c1_policy_link as link

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "recovery-policy"))
from guarded_schema import parse_signal  # noqa: E402

START = {"record_type": "c1_shadow_start", "run_id": "r1", "detector": "c1_shadow_v3.3", "threshold": -0.0767,
         "artifact_hashes": {"model.pkl": "a" * 64, "scaler.pkl": "b" * 64}, "started_at_utc": "2026-10-06T02:53:14+00:00"}
SNAP = {"active_selector": "aaa111", "preview_selector": "bbb222",
        "pods": [{"name": "vllm-serving-aaa111-x", "uid": "ua", "hash": "aaa111", "restart_count": 0},
                 {"name": "vllm-serving-bbb222-y", "uid": "up", "hash": "bbb222", "restart_count": 0},
                 {"name": "recovery-policy-z", "uid": "uz", "hash": None}]}
EVAL = {"record_type": "c1_evaluation", "run_id": "r1", "evaluation_seq": 3, "status": "scored", "condition_met": True,
        "condition_met_at_utc": "2026-10-06T02:53:49+00:00", "window_start_utc": "2026-10-06T02:52:48+00:00",
        "window_end_utc": "2026-10-06T02:53:48+00:00", "score": -0.21, "consecutive_anomalous": 3,
        "expected_pods": {"vllm-serving-aaa111-x": "ua", "vllm-serving-bbb222-y": "up"}, "pod_snapshot": SNAP}


def test_payload_built_from_start_and_eval_validates_in_guarded_schema():
    p = link.build_v2_payload(START, EVAL)
    sig, err = parse_signal(p)
    assert err is None and sig.evaluation_seq == 3 and sig.decided_at_utc.isoformat().startswith("2026-10-06T02:53:49")
    assert p["detector_session_id"] == START["started_at_utc"] and p["observed"]["preview_pod_uid"] == "up"
    assert p == link.build_v2_payload(START, EVAL)  # 재생성해도 같은 본문(재전송 시 시각·seq 유지)


@pytest.mark.parametrize("mut", [lambda e: e.update(condition_met=False), lambda e: e.update(status="skipped"),
                                 lambda e: e.update(run_id="r2"), lambda e: e.pop("window_end_utc"),
                                 lambda e: e.update(expected_pods={"vllm-serving-aaa111-x": "other", "vllm-serving-bbb222-y": "up"}),
                                 lambda e: e.update(pod_snapshot={**SNAP, "preview_selector": "aaa111"})])
def test_payload_errors_instead_of_filling_defaults(mut):
    e = {**EVAL}
    mut(e)
    with pytest.raises(link.PayloadError):
        link.build_v2_payload(START, e)


class R:
    def __init__(self, code, body=None, text=""):
        self.status_code, self._body, self.text = code, body, text

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _get(routes):
    def get(url, timeout, allow_redirects):
        assert allow_redirects is False
        for suffix, resp in routes.items():
            if url.endswith(suffix):
                return resp
        return R(404)
    return get


def test_check_receiver_guarded():
    ok = _get({"/identity": R(200, {"role": link.GUARDED_ROLE, "version": "guarded-v1", "mode": "record_only"})})
    assert link.check_receiver("http://x", "guarded", expected_version="guarded-v1", expected_mode="record_only", get=ok)["ok"]
    assert not link.check_receiver("http://x", "guarded", expected_mode="act", get=ok)["ok"]
    kw = dict(expected_version="guarded-v1", expected_mode="record_only")
    assert not link.check_receiver("http://x", "guarded", get=_get({"/identity": R(200, {"role": "capture_sink"})}), **kw)["ok"]
    assert link.check_receiver("http://x", "guarded", get=_get({"/identity": R(302)}), **kw)["reason"] == "redirect_refused"
    assert not link.check_receiver("http://x", "guarded", get=_get({}), **kw)["ok"]  # 기존 정책·sink는 /identity 404
    assert link.check_receiver("http://x", "guarded", get=ok)["reason"] == "expected_version_and_mode_required"
    assert not link.check_receiver("http://x", "guarded", expected_version="guarded-v2", expected_mode="record_only", get=ok)["ok"]


def test_check_receiver_capture_sink_limited_and_rejects_old_policy():
    sink = _get({"/healthz": R(200)})
    res = link.check_receiver("http://x", "capture_sink", get=sink)
    assert res["ok"] and "신원 증명 아님" in res["note"]
    old_policy = _get({"/healthz": R(200), "/admin/experiment-run": R(200, {"current": None})})
    assert not link.check_receiver("http://x", "capture_sink", get=old_policy)["ok"]


def _post(resp=None, exc=None):
    def post(url, json, timeout, allow_redirects):
        assert allow_redirects is False
        if exc:
            raise exc
        return resp
    return post


@pytest.mark.parametrize("post,expect", [
    (_post(R(200, {"outcome": "rejected_context", "policy_passed": False})), "delivered"),
    (_post(R(200, ValueError("bad json"))), "delivered_unparsable"),
    (_post(R(200, {"no": "outcome"})), "delivered_unparsable"),
    (_post(R(500, None, "err")), "http_error"),
    (_post(R(404, None, "nf")), "http_error"),
    (_post(R(307)), "redirect_refused"),
    (_post(exc=requests.exceptions.ReadTimeout("t")), "unknown_timeout"),
    (_post(exc=requests.exceptions.ConnectTimeout("ct")), "not_delivered"),
    (_post(exc=requests.exceptions.ConnectionError("Failed to establish a new connection: refused")), "not_delivered"),
    (_post(exc=requests.exceptions.ConnectionError("RemoteDisconnected('closed without response')")), "unknown_connection_error"),
    (_post(exc=requests.exceptions.InvalidURL("u")), "error_unknown")])
def test_send_classifies_results(post, expect):
    r = link.send("http://x/signal/v2", {"a": 1}, post=post)
    assert r["delivery"] == expect
    if expect == "delivered":
        assert r["policy_outcome"] == "rejected_context" and r["policy_passed"] is False
