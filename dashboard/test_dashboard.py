"""시연 대시보드 테스트 - 클러스터 접속 없음(가짜 원자료·가짜 클라이언트). 실행: python -m pytest dashboard -q"""
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mock  # noqa: E402
import server  # noqa: E402
import sources  # noqa: E402
import state  # noqa: E402

NOW = datetime(2026, 10, 15, 5, 0, 0, tzinfo=timezone.utc)
OLD, NEW, NEW_UID = "aaaa111111", "bbbb222222", "uid-new"
TARGET = f"vllm-serving/vllm-serving@ro-uid:{OLD}->{NEW}#{NEW_UID}"
RUN = "guarded_pressure_act-demo01"


def iso(sec):
    return (NOW + timedelta(seconds=sec)).isoformat()


def pod(name, h, uid, node, ready=True):
    return {"name": name, "uid": uid, "hash": h, "node": node, "ip": "10.0.0." + name[-1], "phase": "Running",
            "ready": ready, "terminating": False}


PODS = [pod("p-old1", OLD, "uid-old", "sj-worker"), pod("p-new2", NEW, NEW_UID, "sj-worker2")]


def k8s(active=OLD, eps=("p-old1",), before=None, after=None, preview=NEW, pods=PODS, ep_uids=None):
    uids = ep_uids or {p["name"]: p["uid"] for p in pods}
    return {"ok": True, "read_at": iso(0), "error": None, "data": {
        "active_hash_before": before or active, "active_hash_after": after or active, "preview_hash": preview,
        "endpoints": [{"ip": "x", "target": e, "uid": uids.get(e), "kind": "Pod"} for e in eps], "pods": pods,
        "rollout": {"phase": "Paused", "active_selector": active, "preview_selector": preview}}}


def ev(sec, k, anomalous=True, score=-0.2, cond=False):
    return {"status": "scored", "score": score, "score_completed_at_utc": iso(sec), "is_anomalous": anomalous,
            "consecutive_anomalous": k, "condition_met": cond,
            "features": {"cpu_mean": 1.2, "cpu_slope": 0.3, "memory_mean": 7e9, "memory_slope": 1e5}}


def detector(evals, sends=(), pending=False, start=None):
    return {"ok": True, "read_at": iso(0), "error": None, "data": {
        "start": start or {"run_id": RUN, "detector": "c1_live_v3.3", "threshold": -0.0767, "started_at_utc": iso(-60)},
        "evaluations": list(evals), "send_results": list(sends), "send_pending": pending}}


PROMOTED = {"evaluation_seq": 3, "completed_at_utc": iso(-40), "decision": "post_action", "delivery": "delivered",
            "policy_outcome": "promoted_verified", "target_key": TARGET,
            "promotion": {"attempted": True, "verified": True, "verified_at": iso(-41)}}


def policy(status="verified", ok=True, verified=True, new=60.0, old=0.0, run=RUN, blocked=False, available=True):
    sc = None if status == "verified_service_pending" else {
        "ok": ok, "validity": [], "counter_window_sec": 60.0,
        "verdict": {"verified": verified, "reasons": [], "new_pod_increase": new, "old_pod_increase": old}}
    return {"ok": True, "read_at": iso(0), "error": None, "data": {
        "identity": {"mode": "act"}, "state_error": None,
        "state": {"state_available": available, "blocked": blocked, "actions": {TARGET: {
            "status": status, "run_id": run, "reserved_at": iso(-101), "promote_called_at": iso(-100),
            "service_finished_at": iso(-30),
            "service_check": sc}}}}}


def probe(last=-1, total=60, fail=0, window_start=-60, viol=False, evaluable=True):
    return {"ok": True, "read_at": iso(0), "error": None, "data": {
        "total": total, "success": total - fail, "fail": fail, "p95": 0.3, "success_rate": (total - fail) / total,
        "latency_evaluable": evaluable, "latency_violating": viol, "availability_violating": fail > 0,
        "last_sent_at": iso(last), "window_start": iso(window_start)}}


def raw(**kw):
    base = {"mode": "live", "now": iso(0), "k8s": k8s(), "detector": detector([ev(-5, 0, False, 0.05)]),
            "policy": policy(), "probe": probe()}
    base.update(kw)
    return base


SWITCHED_K8S = dict(active=NEW, eps=("p-new2",), preview=NEW)


# 1. active/preview 파싱
def test_active_and_preview_from_service_and_endpoints():
    kv = state.derive_k8s(k8s())
    assert [p["name"] for p in kv["serving"]] == ["p-old1"] and kv["serving"][0]["node"] == "sj-worker"
    assert [p["name"] for p in kv["preview"]] == ["p-new2"]
    assert state.derive_k8s(k8s(**SWITCHED_K8S))["preview"] == []  # 전환 후 preview==active - 대기 Pod 없음


# 2. 탐지기 기록 파싱(부분 줄·깨진 줄·두 형식)
def test_parse_evidence_partial_and_malformed(tmp_path):
    p = tmp_path / "c1-live-evidence-x.jsonl"
    start = {"record_type": "c1_live_start", "run_id": "x", "started_at_utc": iso(0), "threshold": -0.07}
    good = {"record_type": "c1_evaluation", "status": "scored", "score": -0.1, "features": {"cpu_mean": 1.0}}
    p.write_text(json.dumps(start) + "\n" + json.dumps(good) + "\n" + '{"record_type": "c1_eval', encoding="utf-8")
    d = sources.parse_evidence(sources._complete_lines(p))
    assert d["start"]["run_id"] == "x" and len(d["evaluations"]) == 1  # 쓰는 중인 마지막 줄은 제외
    p.write_text(json.dumps(start) + "\n{broken\n", encoding="utf-8")
    r = sources.read_detector(tmp_path, NOW - timedelta(days=3650))
    assert r["ok"] is False and "JSON" in r["error"]


def test_bck_record_uses_if_rule_not_actuating_rule():
    rec = {**ev(-5, 2, anomalous=False, score=-0.2), "rules": {"B": {"is_anomalous": True, "consecutive": 2,
                                                                     "condition_met": False}}}
    start = {"run_id": RUN, "detector": "c1_bck_v1_fixed_cal", "arm": "C", "actuating_rule": "C", "threshold": 1.68,
             "if_threshold": -0.0767, "started_at_utc": iso(-60)}
    d = state.derive_detector(detector([rec], start=start), NOW)
    assert d["value"] == "ANOMALY" and d["streak"] == 2 and d["threshold"] == -0.0767


def test_real_frozen_evidence_parses_if_present():
    f = (HERE.parent / "experiments/results/capstone_guarded_bck_v1/"
         "c1-live-evidence-guarded_pressure_act-bc01b-capstone-guarded-bck-v1-20261007.jsonl")
    if not f.exists():
        pytest.skip("frozen evidence not present")
    d = sources.parse_evidence(sources._complete_lines(f))  # 읽기 전용
    assert d["start"]["actuating_rule"] == "B" and d["send_results"][0]["policy_outcome"] == "promoted_verified"
    assert state.parse_target(d["send_results"][0]["target_key"])["new"] == "5f7d7bbbd8"


# 3. IF score / threshold / 3연속 표시
def test_detector_streak_and_threshold():
    evals = [ev(-35, 1), ev(-20, 2), ev(-5, 3, cond=True)]
    d = state.derive_detector(detector(evals), NOW)
    assert (d["value"], d["streak"], d["score"], d["threshold"]) == ("ANOMALY", 3, -0.2, -0.0767)
    assert len(d["series"]) == 3


# 4. selector / endpoints
def test_inconsistent_or_mixed_reads_are_not_switched():
    target = state.parse_target(TARGET)
    assert state.is_switched(state.derive_k8s(k8s(**SWITCHED_K8S)), target)
    assert not state.is_switched(state.derive_k8s(k8s(active=NEW, eps=("p-new2",), before=OLD, after=NEW)), target)
    assert not state.is_switched(state.derive_k8s(k8s(active=NEW, eps=("p-new2", "p-old1"))), target)
    assert not state.is_switched(state.derive_k8s(k8s(active=NEW, eps=())), target)
    assert not state.is_switched(state.derive_k8s(k8s(active=NEW, eps=("ghost",))), target)
    swapped = [pod("p-new2", NEW, "other-uid", "sj-worker2"), PODS[0]]
    assert not state.is_switched(state.derive_k8s(k8s(active=NEW, eps=("p-new2",), pods=swapped)), target)
    # Endpoint targetRef UID가 현재 Pod UID와 다르면(같은 이름의 교체 Pod) 식별 불가
    stale = state.derive_k8s(k8s(active=NEW, eps=("p-new2",), ep_uids={"p-new2": "deleted-uid"}))
    assert stale["consistent"] is False and not state.is_switched(stale, target)


# 5. traffic verification 파싱
def test_traffic_parsing():
    assert state.derive_traffic(policy()["data"]["state"]["actions"][TARGET], None)["value"] == "VERIFIED"
    a = policy(ok=None)["data"]["state"]["actions"][TARGET]  # verdict true여도 ok None이면 불명
    assert state.derive_traffic(a, None)["value"] == "UNKNOWN"
    a = policy(status="verified_service_pending")["data"]["state"]["actions"][TARGET]
    assert state.derive_traffic(a, None)["value"] == "MEASURING"
    t = state.derive_traffic(None, "policy unreachable")
    assert t["value"] == "UNKNOWN" and t["new_delta"] is None and t["old_delta"] is None  # 0으로 채우지 않음
    a = policy(verified=False, ok=False, new=3.0)["data"]["state"]["actions"][TARGET]
    assert state.derive_traffic(a, None)["value"] == "FAILED"


# 6. unknown / stale
def test_unknown_and_stale_never_show_healthy_or_zero():
    v, _ = state.derive(raw(k8s={"ok": False, "error": "kubernetes: timeout", "read_at": iso(0)}))
    assert v["ui"]["header"]["system"]["value"] == "UNKNOWN" and v["recovery"]["value"] == "UNKNOWN"
    assert state.derive_detector(detector([ev(-60, 0, False, 0.05)]), NOW)["value"] == "STALE"
    assert "정책" in state.derive_detector(detector([ev(-60, 1)], pending=True), NOW)["reason"]
    assert state.derive_detector({"ok": True, "data": None}, NOW)["value"] == "NO DATA"
    assert state.derive_user(probe(last=-30), NOW)["value"] == "STALE"
    assert state.derive_user(probe(total=10), NOW)["value"] == "UNKNOWN"
    assert state.derive_user(probe(evaluable=False), NOW)["value"] == "UNKNOWN"
    assert state.derive_user({"ok": False, "error": "probe csv: race"}, NOW)["value"] == "UNKNOWN"
    assert state.derive_user(probe(fail=2), NOW)["value"] == "DEGRADED"
    v, _ = state.derive(raw(detector=detector([{"status": "skipped", "skipped_at_utc": iso(-5), "skip_reason": "x"}])))
    assert v["ui"]["header"]["active"]["value"] == "UNKNOWN"  # 건너뜀은 정상 근거가 아니다


# 7. 승격 API 성공만으로 SWITCHED가 되지 않는다
def test_promotion_success_without_service_switch_is_promoting():
    v, obs = state.derive(raw(detector=detector([ev(-50, 3, cond=True)], [PROMOTED])))
    assert v["recovery"]["value"] == "PROMOTING" and v["ui"]["header"]["system"]["value"] == "RECOVERING" and "switch" not in obs


def test_record_only_is_not_promoting():
    s = {**PROMOTED, "policy_outcome": "would_pass_record_only", "decision": "continue", "target_key": None}
    v, _ = state.derive(raw(detector=detector([ev(-5, 3, cond=True)], [s])))
    u = v["ui"]
    assert v["recovery"]["value"] == "RECORD ONLY" and u["header"]["policy"]["value"] == "RECORD ONLY"
    assert u["header"]["system"]["value"] != "RECOVERING" and u["rollout"]["value"] != "PROMOTING"


# 8. 새 Pod 트래픽 증거 없으면 VERIFIED가 아니다
@pytest.mark.parametrize("pol,prb,why", [
    (policy(status="verified_service_pending"), probe(window_start=-50), "traffic pending"),
    ({"ok": False, "error": "policy /identity: ConnectionError", "read_at": iso(0)}, probe(window_start=-50), "policy down"),
    (policy(run="other-run"), probe(window_start=-50), "wrong run"),
    (policy(new=0.0, verified=True), probe(window_start=-50), "zero new"),
    (policy(), probe(window_start=-120), "probe window before promotion"),
    (policy(), probe(last=-40, window_start=-50), "probe stale"),
])
def test_not_verified_without_full_evidence(pol, prb, why):
    v, obs = state.derive(raw(k8s=k8s(**SWITCHED_K8S), detector=detector([ev(-50, 3, cond=True)], [PROMOTED]),
                              policy=pol, probe=prb))
    assert v["recovery"]["value"] == "SWITCHED", why
    assert "verified" not in obs and obs.get("switch") == iso(0)


def test_verified_with_full_evidence():
    args = dict(k8s=k8s(**SWITCHED_K8S), detector=detector([ev(-50, 3, cond=True)], [PROMOTED]))
    v, obs = state.derive(raw(probe=probe(window_start=-50), **args))
    assert v["recovery"]["value"] == "VERIFIED" and v["ui"]["header"]["system"]["value"] == "HEALTHY"
    assert obs["switch"] == iso(0) and obs["verified"] == iso(0)
    assert (v["ui"]["verify"]["from"], v["ui"]["verify"]["to"]) == (OLD, NEW)
    v2, obs2 = state.derive(raw(probe=probe(window_start=-50, fail=3), **args), seen=obs)
    assert v2["ui"]["header"]["system"]["value"] == "DEGRADED" and obs2 == {}  # 이후 품질 저하는 숨기지 않는다
    assert v2["ui"]["events"][-1]["state"] == "done"  # 이미 확인된 트래픽 검증 사건은 남는다


@pytest.mark.parametrize("pc", [None, "not-a-time", "2026-10-15T04:58:20"])
def test_verified_requires_valid_promote_called_at(pc):
    pol = policy()
    pol["data"]["state"]["actions"][TARGET]["promote_called_at"] = pc
    v, obs = state.derive(raw(k8s=k8s(**SWITCHED_K8S), detector=detector([ev(-50, 3, cond=True)], [PROMOTED]),
                              policy=pol, probe=probe(window_start=-50)))
    assert v["recovery"]["value"] == "SWITCHED" and "verified" not in obs


def test_future_probe_is_not_trusted():
    u = state.derive_user(probe(last=30, window_start=-20), NOW, not_before=iso(-100))
    assert u["value"] == "UNKNOWN" and u["usable"] is False


def test_original_c1_threshold_survives_parse(tmp_path):
    start = {"record_type": "c1_live_start", "run_id": "x", "detector": "c1_live_v3.3", "threshold": -0.0766,
             "started_at_utc": iso(-30)}
    d = sources.parse_evidence([json.dumps(start), json.dumps({"record_type": "c1_evaluation", **ev(-5, 1)})])
    view = state.derive_detector({"ok": True, "data": d}, NOW)
    assert view["threshold"] == -0.0766


def test_stable_load_rejects_rewrite_in_progress(tmp_path):
    f = tmp_path / "probe.csv"
    f.write_text("experiment_run_id,sent_at,latency,status,success\nx,2026-10-15T05:00:00+00:00,0.2,200,True\n",
                 encoding="utf-8")
    slept = []
    mt = f.stat().st_mtime
    assert sources._stable_load(f, wall=lambda: mt + 0.05, sleep=slept.append) is None and len(slept) == 3
    assert len(sources._stable_load(f, wall=lambda: mt + 5, sleep=slept.append)) == 1


def test_blocked_policy_is_degraded():
    v, _ = state.derive(raw(k8s=k8s(**SWITCHED_K8S), detector=detector([ev(-50, 3, cond=True)], [PROMOTED]),
                            policy=policy(blocked=True)))
    assert v["recovery"]["value"] == "DEGRADED"


# 9. 클러스터 변경 호출 없음
FORBIDDEN = [r"\bcreate_", r"\bpatch_", r"\bdelete_", r"\breplace_", r"\bconnect_", r"\bstream\(", r"subprocess",
             r"os\.system", r"kubectl", r"requests\.(post|put|patch|delete)", r"\.post\(", r"method=\"(POST|PUT)",
             r"/signal", r"/admin/(context|actions|blocked)", r"open\([^)]*['\"][wa]"]


def test_static_scan_no_mutation_calls():
    for f in ("state.py", "sources.py", "server.py", "mock.py", "index.html"):
        text = (HERE / f).read_text(encoding="utf-8")
        for pat in FORBIDDEN:
            assert not re.search(pat, text), f"{f}: {pat}"


def test_k8s_reader_uses_only_read_calls(monkeypatch):
    calls = []

    class Obj:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class Fake:
        def __getattr__(self, name):
            calls.append(name)
            if not name.startswith(("read_", "list_", "get_")):
                raise AssertionError(f"mutating call {name}")
            if name == "read_namespaced_service":
                return lambda *a, **k: Obj(spec=Obj(selector={sources.HASH_LABEL: OLD}))
            if name == "read_namespaced_endpoints":
                return lambda *a, **k: Obj(subsets=[])
            if name == "list_namespaced_pod":
                return lambda *a, **k: Obj(items=[])
            return lambda *a, **k: {"status": {"phase": "Healthy", "blueGreen": {}}}
    monkeypatch.setattr(sources, "_clients", lambda: (Fake(), Fake()))
    r = sources.read_k8s()
    assert r["ok"] and r["data"]["active_hash_before"] == OLD == r["data"]["active_hash_after"]
    assert all(c.startswith(("read_", "list_", "get_")) for c in calls)


def test_policy_reader_get_only_and_token_not_leaked():
    seen = []

    class Resp:
        def __init__(self, code, body):
            self.status_code, self.body = code, body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code} secret-token")

        def json(self):
            return self.body

    def get(url, headers=None, **kw):
        seen.append((url, kw.get("allow_redirects")))
        if url.endswith("/identity"):
            return Resp(200, {"mode": "act", "role": "guarded-recovery-policy", "version": "guarded-v1"})
        return Resp(401, {})
    r = sources.read_policy("http://127.0.0.1:18090", "secret-token", get=get)
    assert r["ok"] and "401" in r["data"]["state_error"] and "secret-token" not in json.dumps(r)
    assert all(redir is False for _, redir in seen)
    r = sources.read_policy("http://127.0.0.1:18090", None, get=get)
    assert r["data"]["state"] is None and "TOKEN" in r["data"]["state_error"]


def test_server_rejects_non_get_methods():
    hub = server.Hub(lambda m=5: mock.build(5, minutes=m))
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(hub))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        body = json.loads(urllib.request.urlopen(base + "/api/state", timeout=5).read())
        assert body["mode"] == "mock"
        for m in ("POST", "PUT", "PATCH", "DELETE"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(urllib.request.Request(base + "/api/state", data=b"{}", method=m), timeout=5)
            assert e.value.code == 405
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/promote", timeout=5)
        assert e.value.code == 404
    finally:
        srv.shutdown()


def test_hub_collects_once_per_interval():
    n = []
    hub = server.Hub(lambda m=5: n.append(1) or mock.build(5, minutes=m))
    for _ in range(5):
        hub.view()
    assert len(n) == 1


# 세션 범위: 대시보드 시작 전 실행은 무시
def test_pick_run_ignores_runs_started_before_session(tmp_path):
    for name, sec in (("old", -600), ("new", 30)):
        p = tmp_path / f"c1-live-evidence-{name}.jsonl"
        p.write_text(json.dumps({"record_type": "c1_live_start", "run_id": name, "started_at_utc": iso(sec)}) + "\n",
                     encoding="utf-8")
        t = (NOW + timedelta(seconds=60)).timestamp()  # 이전 실행 파일도 방금 수정됐다고 가정(그래도 무시해야 한다)
        os.utime(p, (t, t))
    assert sources.pick_run(tmp_path, NOW).name == "c1-live-evidence-new.jsonl"
    assert sources.pick_run(tmp_path, NOW + timedelta(hours=1)) is None


def test_probe_summary_matches_slo_judge_window():
    rows = [{"sent_at": NOW + timedelta(seconds=s), "latency": 0.2, "success": s != 90} for s in range(0, 100)]
    s = sources.summarize_probe(rows)
    assert s["total"] == 60 and s["fail"] == 1 and s["availability_violating"] is True
    assert state.PROBE_MIN_SAMPLES == sources.slo_judge.MIN_SAMPLES_FOR_RELIABLE_P95


# 10. mock 흐름 + live 격리
def _mock_hub():
    hub = server.Hub(lambda m=5: mock.build(hub.t, minutes=m))
    return hub


def test_mock_demo_flow():
    hub, states = _mock_hub(), []
    for t in (5, 25, 42, 51, 53, 60, 90, 120):
        hub.t, hub.cache = t, {}
        v = hub.view()
        assert v["mode"] == "mock" and v["ui"]["badge"] == "MOCK"
        states.append(v["ui"]["header"]["system"]["value"])
    assert states == ["HEALTHY", "PRESSURE", "PRESSURE", "RECOVERING", "RECOVERING", "VERIFYING", "VERIFYING", "HEALTHY"]
    u = v["ui"]
    assert u["verify"]["value"] == "VERIFIED" and (u["verify"]["new"], u["verify"]["old"]) == (60.0, 0.0)
    assert all(e["state"] == "done" for e in u["events"])
    assert u["header"]["shadow"]["met"] is True and u["header"]["shadow"]["sends"] == 0  # 조건 충족해도 전송 없음


def test_mock_next_cycle_starts_fresh_timeline():
    hub = _mock_hub()
    hub.t = 120
    assert hub.view()["ui"]["header"]["system"]["value"] == "HEALTHY"
    hub.cache, hub.t = {}, mock.LOOP_SEC + 5
    u = hub.view()["ui"]
    assert u["header"]["system"]["value"] == "HEALTHY" and not any(e["state"] == "done" for e in u["events"])


def test_mock_mode_never_calls_live_collectors(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("live collector called in mock mode")
    for f in ("read_k8s", "read_detector", "read_policy", "read_probe", "read_pressure", "read_traffic", "collect"):
        monkeypatch.setattr(sources, f, boom)
    collect = server.build_collector(type("A", (), {"mock": True})())
    assert server.Hub(collect).view()["mode"] == "mock"


def test_live_collect_is_marked_live(monkeypatch, tmp_path):
    monkeypatch.setattr(sources, "read_k8s", lambda: {"ok": False, "error": "x", "read_at": iso(0)})
    monkeypatch.setattr(sources, "read_policy", lambda *a: {"ok": False, "error": "x", "read_at": iso(0)})
    r = sources.collect({"results_dir": tmp_path, "since": NOW, "policy_url": "http://127.0.0.1:1"})
    assert r["mode"] == "live" and r["detector"]["data"] is None and "path" not in r["detector"]
    v, _ = state.derive(r)
    assert v["ui"]["header"]["system"]["value"] == "UNKNOWN" and v["detector"]["value"] == "NO DATA"


def test_live_urls_must_be_loopback():
    args = type("A", (), {"mock": False, "policy_url": "http://10.0.0.5:18090", "prom_url": "http://127.0.0.1:9090",
                          "since_minutes": 0, "results_dir": "."})()
    with pytest.raises(SystemExit):
        server.build_collector(args)


# ---------- Phase 2(ACTIVE/SHADOW) 기록 ----------
A_ID, S_ID = "cpu-threshold-c", "c3-a-if"
P2_START = {"run_id": RUN, "detector": "phase2_same_tick_v1", "roles": {A_ID: "ACTIVE", S_ID: "SHADOW"},
            "active_detector": A_ID, "thresholds": {A_ID: 1.684, S_ID: -0.195}, "started_at_utc": iso(-120)}


def p2ev(sec, cpu, k, s_k=0):
    def rule(v, kk, role, thr):
        return {"value": v, "threshold": thr, "state": "ANOMALY" if kk else "NORMAL", "is_anomalous": kk > 0,
                "consecutive": kk, "condition_met": kk == 3, "role": role}
    return {"status": "scored", "score_completed_at_utc": iso(sec), "window_end_utc": iso(sec - 2),
            "rules": {A_ID: rule(cpu, k, "ACTIVE", 1.684), S_ID: rule(-0.25 if s_k else -0.1, s_k, "SHADOW", -0.195)}}


def p2det(evals, sends=(), attempts=(), start=P2_START):
    return {"ok": True, "read_at": iso(0), "error": None, "data": {
        "start": start, "evaluations": list(evals), "send_results": list(sends), "send_attempts": list(attempts),
        "send_pending": False}}


P2_SEND = {**PROMOTED, "detector_id": A_ID, "attempted_at_utc": iso(-45)}


def test_p2_parse_keeps_rules_and_pairs_attempts_by_detector():
    lines = [json.dumps({"record_type": "c1_live_start", **P2_START}),
             json.dumps({"record_type": "c1_evaluation", "evaluation_seq": 3, **p2ev(-5, 1.9, 1, 3)}),
             json.dumps({"record_type": "c1_policy_send_attempt", "detector_id": A_ID, "evaluation_seq": 3,
                         "attempted_at_utc": iso(-4)}),
             json.dumps({"record_type": "c1_policy_send_attempt", "detector_id": S_ID, "evaluation_seq": 3,
                         "attempted_at_utc": iso(-3)}),
             json.dumps({"record_type": "c1_policy_send_result", "detector_id": A_ID, "evaluation_seq": 3,
                         "delivery": "delivered"})]
    d = sources.parse_evidence(lines)
    assert d["start"]["roles"] == P2_START["roles"] and d["start"]["active_detector"] == A_ID
    assert d["evaluations"][0]["rules"][S_ID]["condition_met"] is True
    assert d["send_results"][0]["attempted_at_utc"] == iso(-4)  # SHADOW 시도와 섞이지 않음
    assert d["send_pending"] is True and len(d["send_attempts"]) == 2


def test_p2_active_series_threshold_and_missing_active():
    d = state.derive_detector(p2det([p2ev(-20, 1.5, 0), p2ev(-5, 1.9, 1)]), NOW)
    assert (d["value"], d["active_id"], d["threshold"], d["streak"]) == ("ANOMALY", A_ID, 1.684, 1)
    assert [p["y"] for p in d["series"]] == [1.5, 1.9] and d["reason"] == "임계값 초과"
    bad = {**P2_START, "active_detector": S_ID}  # 역할 표와 맞지 않으면 SHADOW로 채우지 않는다
    assert state.derive_detector(p2det([p2ev(-5, 1.9, 1)], start=bad), NOW)["value"] == "UNKNOWN"


def test_p2_shadow_has_no_authority_and_violation_is_shown():
    v, _ = state.derive(raw(detector=p2det([p2ev(-20, 1.2, 0, 2), p2ev(-5, 1.3, 0, 3)])))
    sh = v["ui"]["header"]["shadow"]
    assert (sh["id"], sh["value"], sh["met"], sh["sends"], sh["violation"]) == (S_ID, "ANOMALY", True, 0, False)
    assert v["ui"]["header"]["active"]["value"] == "NORMAL" and v["ui"]["cpu_chart"]["signal_at"] is None
    leak = {"detector_id": S_ID, "evaluation_seq": 3, "attempted_at_utc": iso(-4)}
    v, _ = state.derive(raw(detector=p2det([p2ev(-5, 1.3, 0, 3)], attempts=[leak])))
    assert v["ui"]["header"]["shadow"]["violation"] is True and v["ui"]["cpu_chart"]["signal_at"] is None
    v, _ = state.derive(raw(detector={"ok": False, "error": "evidence: x", "read_at": iso(0)}))
    assert v["ui"]["header"]["shadow"]["sends"] is None  # 못 읽으면 0이라고 하지 않는다


def test_p2_signal_at_is_active_attempt():
    v, _ = state.derive(raw(detector=p2det([p2ev(-50, 2.0, 3)], [P2_SEND])))
    assert v["ui"]["cpu_chart"]["signal_at"] == iso(-45) and v["ui"]["header"]["active"]["text"] == "SIGNAL SENT"
    assert v["ui"]["events"][1]["at"] == iso(-45) and v["ui"]["events"][2]["at"] == iso(-101)  # 정책 조치 예약 시각
    bad = {**P2_START, "active_detector": None}  # ACTIVE를 확정 못 하면 어떤 시도도 ACTIVE 신호로 보이지 않는다
    v, _ = state.derive(raw(detector=p2det([p2ev(-50, 2.0, 3)], [P2_SEND], start=bad)))
    assert v["ui"]["cpu_chart"]["signal_at"] is None


# ---------- 트래픽 검증 경계 ----------
@pytest.mark.parametrize("new,old,ok", [(30.0, 2.0, True), (60.0, 0.0, True), (29.99, 0.0, False), (60.0, 2.01, False),
                                        (60.0, -1.0, False), (True, 0.0, False), (60.0, None, False),
                                        (float("nan"), 0.0, False), (float("inf"), 0.0, False)])
def test_traffic_verified_boundaries(new, old, ok):
    a = policy(new=new, old=old)["data"]["state"]["actions"][TARGET]
    assert (state.derive_traffic(a, None)["value"] == "VERIFIED") is ok


def test_traffic_badge_requires_observed_switch():
    det = detector([ev(-50, 3, cond=True)], [PROMOTED])
    assert _ui(k8s=k8s(**SWITCHED_K8S), detector=det)["verify"]["value"] == "VERIFIED"
    u = _ui(detector=det)  # 정책은 verified지만 대시보드가 Service 전환을 보지 못함
    assert u["verify"]["value"] != "VERIFIED" and u["verify"]["new"] is None


def test_traffic_numbers_only_from_policy_verdict():
    args = dict(k8s=k8s(**SWITCHED_K8S), detector=detector([ev(-50, 3, cond=True)], [PROMOTED]))
    u = _ui(policy=policy(status="verified_service_pending"), **args)
    assert u["verify"]["value"] == "VERIFYING" and (u["verify"]["new"], u["verify"]["old"]) == (None, None)
    u = _ui(policy=policy(verified=False, ok=False, new=3.0, old=5.0), **args)
    assert u["verify"]["value"] == "NOT VERIFIED" and u["header"]["system"]["value"] == "DEGRADED"


# ---------- CPU 압박 ----------
def _pr(**data):
    return {"ok": True, "read_at": iso(0), "error": None,
            "data": {"injection_started_at": None, "summary_error": None, "chaos": [], "chaos_error": None, **data}}


def test_pressure_sources_and_latch():
    assert state.derive_pressure(_pr(injection_started_at=iso(-30)), {})["at"] == iso(-30)  # summary 우선
    v, obs = state.derive(raw(pressure=_pr(chaos=[{"name": "c", "all_injected": True}])))
    assert obs["pressure"] == iso(0) and v["ui"]["cpu_chart"]["pressure_at"] == iso(0)
    assert state.derive_pressure(_pr(chaos=[{"name": "c", "all_injected": False}]), {})["value"] == "INJECTING"
    assert state.derive_pressure(_pr(), {})["value"] == "NONE"
    assert state.derive_pressure(_pr(chaos=None, chaos_error="stresschaos: ApiException"), {})["value"] == "UNKNOWN"
    ended = state.derive_pressure(_pr(), {"pressure": iso(-10)})  # CR 정리 뒤: 사건 시각은 남고 '지금 압박 중'은 아님
    assert (ended["value"], ended["at"]) == ("ENDED", iso(-10))
    assert state.derive_pressure(_pr(chaos=None, chaos_error="x"), {"pressure": iso(-10)})["value"] == "UNKNOWN"
    v, _ = state.derive(raw(pressure=_pr()), seen={"pressure": iso(-10)})
    assert v["ui"]["header"]["system"]["value"] != "PRESSURE" and v["ui"]["events"][0]["at"] == iso(-10)
    assert state.derive_pressure(None, {})["value"] == "UNKNOWN"


def test_read_pressure_filters_by_exact_annotation(monkeypatch, tmp_path):
    calls = []

    def item(rid, inj):
        return {"metadata": {"name": rid, "annotations": {"experiment-run-id-full": rid}},
                "status": {"conditions": [{"type": "AllInjected", "status": inj}]}}

    class Custom:
        def list_namespaced_custom_object(self, *a, **k):
            calls.append(a)
            return {"items": [item(RUN, "True"), item(RUN + "-other", "True")]}
    monkeypatch.setattr(sources, "_clients", lambda: (None, Custom()))
    ev_path = tmp_path / f"c1-live-evidence-{RUN}.jsonl"
    (tmp_path / f"summary-{RUN}.json").write_text(json.dumps({"run_id": RUN, "injection_started_at": iso(-9)}),
                                                  encoding="utf-8")
    d = sources.read_pressure(RUN, ev_path)["data"]
    assert d["chaos"] == [{"name": RUN, "all_injected": True}] and d["injection_started_at"] == iso(-9)
    assert calls == [("chaos-mesh.org", "v1alpha1", "vllm-serving", "stresschaos")]


# ---------- Pod별 요청 그래프 ----------
def test_traffic_series_reset_and_identity():
    old_pod, new_pod = f"vllm-serving-{OLD}-x1", f"vllm-serving-{NEW}-x2"
    tr = {"ok": True, "data": {"series": {old_pod: [[1, 100.0], [2, 110.0], [3, 4.0], [4, 6.0], [5, float("nan")]],
                                          new_pod: [[1, 0.0], [2, 0.0]]}}}
    s = state.traffic_series(tr, OLD, NEW)
    assert s["old"]["points"] == [[1, 0.0], [2, 10.0], [3, 14.0], [4, 16.0]] and s["old"]["reset"] is True
    tr["data"]["series"][f"vllm-serving-{OLD}-x3"] = [[1, 1.0]]  # 같은 hash Pod가 둘 - 누구 것인지 모름
    assert state.traffic_series(tr, OLD, NEW)["old"] is None
    assert state.traffic_series({"ok": False, "error": "prometheus: x"}, OLD, NEW)["ok"] is False


def test_read_traffic_is_get_only_and_bounded():
    seen = {}

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "success", "data": {"result": [{"metric": {"pod": "p1"}, "values": [[1, "2"]]}]}}

    def get(url, **kw):
        seen.update(url=url, **kw)
        return R()
    r = sources.read_traffic("http://127.0.0.1:9090", iso(-3600), get=get, now=NOW)
    assert r["ok"] and r["data"]["series"] == {"p1": [[1.0, 2.0]]} and seen["url"].endswith("/api/v1/query_range")
    assert seen["allow_redirects"] is False and NOW.timestamp() - seen["params"]["start"] <= sources.TRAFFIC_MAX_SEC
    assert sources.read_traffic("http://127.0.0.1:9090", None, get=get)["data"] is None


# ---------- 화면(4분할) 매핑 ----------
def _ui(**kw):
    return state.derive(raw(**kw))[0]["ui"]


def test_ui_top_bar_reflects_live_state():
    u = _ui()
    assert u["badge"] == "LIVE" and u["header"]["system"]["value"] == "HEALTHY"
    assert u["header"]["policy"]["mode"] == "act" and u["header"]["policy"]["value"] == "IDLE"
    assert u["rollout"]["value"] == "PAUSED"
    u = _ui(detector=detector([ev(-50, 3, cond=True)], [PROMOTED]))
    assert (u["header"]["system"]["value"], u["header"]["policy"]["value"], u["rollout"]["value"]) == \
        ("RECOVERING", "ACTION", "PROMOTING")
    u = _ui(k8s=k8s(**SWITCHED_K8S), detector=detector([ev(-50, 3, cond=True)], [PROMOTED]), probe=probe(window_start=-50))
    assert (u["header"]["system"]["value"], u["rollout"]["value"]) == ("HEALTHY", "PROMOTED")


def test_ui_failsafe_unknown_not_last_good():
    u = _ui(k8s={"ok": False, "error": "x", "read_at": iso(0)}, detector=detector([ev(-50, 3, cond=True)], [PROMOTED]))
    assert u["header"]["system"]["value"] == "UNKNOWN" and u["rollout"]["value"] == "UNKNOWN"
    assert u["verify"]["value"] == "UNKNOWN" and {e["key"]: e["state"] for e in u["events"]}["switch"] == "unknown"
    u = _ui(policy={"ok": False, "error": "x", "read_at": iso(0)})
    assert u["header"]["policy"]["value"] == "UNKNOWN" and u["header"]["policy"]["mode"] is None
    u = _ui(detector={"ok": False, "error": "evidence: broken", "read_at": iso(0)})
    assert u["header"]["active"]["value"] == "UNKNOWN" and u["header"]["system"]["value"] == "UNKNOWN"
    assert _ui(detector={"ok": True, "data": None, "read_at": iso(0)})["header"]["system"]["value"] == "WAITING"


def test_ui_active_streak_caps_and_post_switch():
    assert _ui(detector=detector([ev(-5, 5)]))["header"]["active"]["text"] == "ANOMALY 3/3"
    skip = {"status": "skipped", "skipped_at_utc": iso(-5), "skip_reason": "expected_unverified:selector_unknown(a=b)"}
    assert _ui(detector=detector([skip]))["header"]["active"]["value"] == "UNKNOWN"  # 전환 증거 없이 건너뜀


def test_ui_shows_versions_but_not_uids_or_scores():  # 새 디자인은 버전(hash)을 보여주되 UID·IF 점수는 숨긴다
    u = _ui(k8s=k8s(**SWITCHED_K8S), detector=detector([ev(-50, 3, cond=True)], [PROMOTED]), probe=probe(window_start=-50))
    text = json.dumps(u)
    assert NEW_UID not in text and "uid-old" not in text and "score" not in text
    assert u["kpi"]["service"]["hash"] == NEW and u["rollout"]["active"] == NEW


def test_ui_text_has_no_forbidden_claims():
    text = (HERE / "index.html").read_text(encoding="utf-8") + (HERE / "state.py").read_text(encoding="utf-8")
    for bad in ("장애 예측", "장애 확정", "AI 자동 전환", "AI가 복구", "AI가 전환", "MTTR", "RTO", "ollback", "SYNCED",
                "Webhook", "GitOps", "92.5", "Latency Threshold", "AI Inference"):
        assert bad not in text, bad


# ---------- Codex 리뷰 반영 ----------
def test_switch_requires_ready_and_not_terminating_pod():
    target = state.parse_target(TARGET)
    unready = [PODS[0], pod("p-new2", NEW, NEW_UID, "sj-worker2", ready=False)]
    assert not state.is_switched(state.derive_k8s(k8s(**SWITCHED_K8S, pods=unready)), target)
    term = [PODS[0], {**PODS[1], "terminating": True}]
    assert not state.is_switched(state.derive_k8s(k8s(**SWITCHED_K8S, pods=term)), target)


def test_system_precedence_for_violation_and_inconsistent_reads():
    leak = {"detector_id": S_ID, "evaluation_seq": 3, "attempted_at_utc": iso(-4)}
    assert _ui(detector=p2det([p2ev(-5, 1.3, 0, 3)], attempts=[leak]))["header"]["system"]["value"] == "DEGRADED"
    mixed = k8s(active=NEW, eps=("p-new2",), before=OLD, after=NEW)  # 앞뒤 selector 다름
    assert _ui(k8s=mixed)["header"]["system"]["value"] == "UNKNOWN"
    assert _ui(detector=detector([ev(-60, 0, False, 0.05)]))["header"]["system"]["value"] == "UNKNOWN"  # STALE


def test_traffic_identity_unknown_when_active_equals_preview_before_result():
    old_pod, new_pod = f"vllm-serving-{OLD}-x1", f"vllm-serving-{NEW}-x2"
    tr = {"ok": True, "read_at": iso(0), "error": None,
          "data": {"series": {old_pod: [[1, 1.0]], new_pod: [[1, 2.0]]}}}
    u = _ui(k8s=k8s(**SWITCHED_K8S), traffic=tr)  # 전환은 됐지만 승격 결과(대상)가 아직 없음
    assert u["verify"]["old_spark"] is None and u["verify"]["new_spark"] is None


def test_http_session_ignores_env_proxies():
    assert sources._http.trust_env is False
    assert sources.read_policy.__defaults__[0] == sources._http.get


def test_review2_unknown_injection_terminating_preview_and_p2_without_roles():
    assert state.derive_pressure(_pr(chaos=[{"name": "c", "all_injected": None}]), {})["value"] == "UNKNOWN"
    term = [PODS[0], {**PODS[1], "terminating": True}]
    assert state.derive(raw(k8s=k8s(pods=term)))[0]["recovery"]["value"] != "READY"
    no_roles = {k: v for k, v in P2_START.items() if k != "roles"}
    leak = {"detector_id": S_ID, "evaluation_seq": 3, "attempted_at_utc": iso(-4)}
    v, _ = state.derive(raw(detector=p2det([p2ev(-5, 1.3, 0, 3)], attempts=[leak], start=no_roles)))
    assert v["detector"]["p2"] is True and v["ui"]["cpu_chart"]["signal_at"] is None


# ---------- 새 디자인(KPI·파드별 CPU·롤아웃) ----------
def test_kpis_cpu_series_and_profile_threshold():
    cpu = {"ok": True, "read_at": iso(0), "error": None,
           "data": {"minutes": 5, "series": {"p-old1": [[NOW.timestamp() - 30, 1.1], [NOW.timestamp() - 15, 1.3]],
                                             "p-new2": [[NOW.timestamp() - 15, 0.1]]}}}
    prof = {"ok": True, "read_at": iso(0), "error": None,
            "data": {"detectors": [{"id": A_ID, "role": "ACTIVE", "threshold": 1.684}, {"id": S_ID, "role": "SHADOW", "threshold": -0.19}]}}
    u = _ui(cpu=cpu, profile=prof, detector={"ok": True, "data": None, "read_at": iso(0)})  # 세션 없음
    c = u["kpi"]["cpu"]
    assert (c["value"], c["role"], c["hash"]) == (1.3, "기존 Pod", OLD) and u["kpi"]["service"]["hash"] == OLD
    assert c["tip"] == f"Pod: p-old1\nHash: {OLD}\nNode: sj-worker" and "pod" not in c  # 전체 이름은 hover로만
    assert [(s["role"], s["hash"]) for s in u["cpu_chart"]["series"]] == [("preview", NEW), ("active", OLD)]
    assert [s["label"] for s in u["cpu_chart"]["series"]] == ["새 Pod", "기존 Pod"]
    assert (u["cpu_chart"]["threshold"], u["cpu_chart"]["threshold_src"]) == (1.684, "운영 프로필")
    assert u["header"]["active"]["id"] == A_ID and u["header"]["shadow"]["id"] == S_ID
    stale = {**cpu, "data": {**cpu["data"], "series": {"p-old1": [[NOW.timestamp() - 300, 1.1]]}}}
    assert _ui(cpu=stale)["kpi"]["cpu"]["value"] is None  # 오래된 값은 현재 값이 아니다
    assert _ui(cpu={"ok": False, "error": "prometheus cpu: x", "read_at": iso(0)})["cpu_chart"]["ok"] is False


def test_rollout_table_and_unknown():
    r = k8s()
    r["data"]["rollout"].update(stable_rs=OLD, replicas=1, ready_replicas=1)
    u = _ui(k8s=r)
    assert (u["rollout"]["active"], u["rollout"]["preview"], u["rollout"]["stable"]) == (OLD, NEW, OLD)
    assert u["kpi"]["pods"] == {"ready": 1, "desired": 1}
    u = _ui(k8s={"ok": False, "error": "x", "read_at": iso(0)})
    assert u["rollout"]["value"] == "UNKNOWN" and u["kpi"]["pods"] == {"ready": None, "desired": None}


def test_read_cpu_uses_detector_rate_window_and_valid_ranges():
    seen = {}

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "success", "data": {"result": [{"metric": {"pod": "p1"}, "values": [[1, "0.5"]]}]}}

    def get(url, **kw):
        seen.update(kw)
        return R()
    r = sources.read_cpu("http://127.0.0.1:9090", 30, get=get, now=NOW)
    assert r["data"]["series"] == {"p1": [[1.0, 0.5]]} and "[60s]" in seen["params"]["query"]
    assert sources.read_cpu("http://127.0.0.1:9090", 7, get=get)["data"] is None  # 허용 범위 밖


def test_review3_future_cpu_and_session_threshold_not_borrowed():
    fut = {"ok": True, "read_at": iso(0), "error": None,
           "data": {"minutes": 5, "series": {"p-old1": [[NOW.timestamp() + 60, 9.9]]}}}
    assert _ui(cpu=fut)["kpi"]["cpu"]["value"] is None  # 미래 시각 샘플은 현재 값이 아님
    prof = {"ok": True, "read_at": iso(0), "error": None,
            "data": {"detectors": [{"id": A_ID, "role": "ACTIVE", "threshold": 1.684}]}}
    start = {**P2_START, "thresholds": {}}  # 세션 기록에 기준선 없음
    u = _ui(profile=prof, detector=p2det([p2ev(-50, 2.0, 3)], [P2_SEND], start=start))
    assert u["cpu_chart"]["threshold"] is None and "1.684" not in u["events"][1]["text"]


def test_top_recovery_state_word():
    assert _ui()["kpi"]["recovery"]["label"] == "정상"  # 서비스 정상 + 복구 없음
    assert _ui(detector=detector([ev(-50, 3, cond=True)], [PROMOTED]))["kpi"]["recovery"]["label"] == "진행 중"
    args = dict(k8s=k8s(**SWITCHED_K8S), detector=detector([ev(-50, 3, cond=True)], [PROMOTED]))
    assert _ui(policy=policy(status="verified_service_pending"), **args)["kpi"]["recovery"]["label"] == "진행 중"
    assert _ui(**args)["kpi"]["recovery"]["label"] == "검증 완료"
    assert _ui(policy=policy(verified=False, ok=False, new=3.0), **args)["kpi"]["recovery"]["label"] == "검증 실패"
    assert _ui(k8s={"ok": False, "error": "x", "read_at": iso(0)})["kpi"]["recovery"]["label"] == "확인 불가"
    s = {**PROMOTED, "policy_outcome": "would_pass_record_only", "decision": "continue", "target_key": None}
    assert _ui(detector=detector([ev(-5, 3, cond=True)], [s]))["kpi"]["recovery"]["value"] == "IDLE"  # 기록 전용은 복구 아님


def test_header_shows_detectors_actually_used():
    prof = {"ok": True, "read_at": iso(0), "error": None,
            "data": {"detectors": [{"id": A_ID, "role": "ACTIVE", "threshold": 1.684}, {"id": S_ID, "role": "SHADOW", "threshold": -0.19}]}}
    h = _ui(profile=prof, detector={"ok": True, "data": None, "read_at": iso(0)})["header"]  # 세션 전: 운영 프로필
    assert (h["active"]["label"], h["shadow"]["label"]) == ("CPU 임계치", "c3-A IF")
    h = _ui(profile=prof)["header"]  # c1 형식 세션: 실제로 쓴 감지기는 c1 IF(프로필로 덮지 않음)
    assert (h["active"]["label"], h["shadow"]["label"]) == ("c1 IF", "없음")
    h = _ui(profile=prof, detector=p2det([p2ev(-5, 1.3, 0)]))["header"]  # Phase 2 세션: 기록의 역할
    assert (h["active"]["label"], h["shadow"]["label"]) == ("CPU 임계치", "c3-A IF")


# ---------- Pod 표시 이름(기존 Pod / 새 Pod)·실시간 카운터·단계 이름 (2026-10-12) ----------
def test_pod_roles_follow_session_mapping_not_fixed_hashes():
    # 승격 결과 전: active = 기존 Pod, preview = 새 Pod
    u = _ui()
    assert (u["rollout"]["active_role"], u["verify"]["from_label"]) == ("기존 Pod", None)
    # 승격 결과 후: target_key(old->new)가 기준 - hash 값이 바뀌어도 역할은 결과에서 정해진다
    for old, new in ((OLD, NEW), ("cccc333333", "dddd444444")):
        tk = f"vllm-serving/vllm-serving@ro-uid:{old}->{new}#{NEW_UID}"
        pods = [pod("p-old1", old, "uid-old", "sj-worker"), pod("p-new2", new, NEW_UID, "sj-worker2")]
        pol = policy()
        pol["data"]["state"]["actions"] = {tk: pol["data"]["state"]["actions"][TARGET]}
        u = _ui(k8s=k8s(active=new, eps=("p-new2",), preview=new, pods=pods),
                detector=detector([ev(-50, 3, cond=True)], [{**PROMOTED, "target_key": tk}]), policy=pol)
        v = u["verify"]
        assert (v["from_label"], v["to_label"], v["from"], v["to"]) == ("기존 Pod", "새 Pod", old, new)
        assert v["new_tip"].startswith("Pod: p-new2") and "Node: sj-worker2" in v["new_tip"]
        assert u["rollout"]["active_role"] == "새 Pod" and u["events"][4]["text"] == "서비스 연결 대상이 새 Pod로 변경됨"


def test_ui_has_no_hardcoded_hash_and_no_full_pod_name_in_main_text():
    html = (HERE / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"\b[0-9a-f]{10}\b", html)            # 특정 hash 고정 없음
    assert "파드" not in html and "기존 Pod" in html and "새 Pod" in html


def test_rollout_progressing_is_not_promoting():
    r = k8s()
    r["data"]["rollout"]["phase"] = "Progressing"
    u = _ui(k8s=r)
    assert (u["rollout"]["value"], u["rollout"]["label"]) == ("PROGRESSING", "새 Pod 준비 중")
    r["data"]["rollout"].update(stable_rs=OLD)
    assert _ui(k8s=r)["rollout"]["stable_state"] == "정상"


def test_step_labels_and_verifying_note():
    args = dict(k8s=k8s(**SWITCHED_K8S), detector=detector([ev(-50, 3, cond=True)], [PROMOTED]))
    ev_ = _ui(policy=policy(status="verified_service_pending"), **args)["events"]
    assert [e["label"] for e in ev_] == ["압박 주입", "이상 감지", "정책 확인", "롤아웃 전환", "서비스 전환", "요청 이동 검증"]
    assert ev_[-1]["state"] == "wait" and ev_[-1]["note"] == "검증 중"
    ev_ = _ui(**args)["events"]
    assert ev_[-1]["state"] == "done" and ev_[-1]["label"] == "검증 완료"


METRICS = ('# HELP vllm:request_success_total x\n'
           'vllm:request_success_total{finished_reason="stop",model_name="m"} 40.0\n'
           'vllm:request_success_total{finished_reason="length",model_name="m"} 2.0\n'
           'vllm:request_success_total_created{model_name="m"} 1.7e9\n')


def test_sum_counter_and_live_counter_reader_only_while_policy_measures():
    assert sources.sum_counter(METRICS) == 42.0
    assert sources.sum_counter("other 1\n") is None and sources.sum_counter('vllm:request_success_total{a="b"} NaN\n') is None
    pend = policy(status="verified_service_pending")
    sw = k8s(**SWITCHED_K8S)
    calls = []

    def get(name):
        calls.append(name)
        return METRICS
    r = sources.read_live_counters(sw, pend, RUN, get_metrics=get)
    assert r["ok"] and r["data"]["old"] == {"pod": "p-old1", "value": 42.0} and r["data"]["new"]["pod"] == "p-new2"
    calls.clear()
    assert sources.read_live_counters(sw, policy(), RUN, get_metrics=get)["data"] is None and calls == []  # 판정 뒤엔 안 읽음
    assert sources.read_live_counters(sw, pend, "other-run", get_metrics=get)["data"] is None and calls == []

    def boom(name):
        raise RuntimeError("403 pods/proxy")
    assert sources.read_live_counters(sw, pend, RUN, get_metrics=boom)["ok"] is False


def _lc(old, new):
    return {"ok": True, "read_at": iso(0), "error": None,
            "data": {"target_key": TARGET, "old": {"pod": "p-old1", "value": old}, "new": {"pod": "p-new2", "value": new}}}


def test_live_delta_baseline_after_switch_settle_and_official_after_verdict():
    args = dict(k8s=k8s(**SWITCHED_K8S), detector=detector([ev(-50, 3, cond=True)], [PROMOTED]),
                policy=policy(status="verified_service_pending"))
    v, obs = state.derive(raw(live_counters=_lc(500, 10), **args), {"switch": iso(-5)})  # 전환 5초 뒤: 기준점 전
    assert v["ui"]["verify"]["live"] is None and "counter_base" not in obs
    v, obs = state.derive(raw(live_counters=_lc(500, 10), **args), {"switch": iso(-12)})
    assert v["ui"]["verify"]["live"]["new"] == 0 and obs["counter_base"]["new"] == 10
    seen = {"switch": iso(-12), "counter_base": obs["counter_base"]}
    v, _ = state.derive(raw(live_counters=_lc(500, 37), **args), seen)
    assert (v["ui"]["verify"]["live"]["new"], v["ui"]["verify"]["live"]["old"]) == (27, 0)
    assert v["ui"]["verify"]["value"] == "VERIFYING" and v["ui"]["verify"]["new"] is None  # 공식 숫자는 아직 없음
    v, _ = state.derive(raw(live_counters=_lc(499, 37), **args), seen)  # 카운터 감소 = 숫자 없음
    assert v["ui"]["verify"]["live"]["new"] is None
    done = {**args, "policy": policy()}
    u = state.derive(raw(live_counters={"ok": True, "data": None, "read_at": iso(0)}, **done), seen)[0]["ui"]
    assert u["verify"]["value"] == "VERIFIED" and (u["verify"]["new"], u["verify"]["old"], u["verify"]["live"]) == (60.0, 0.0, None)


def test_read_profile_follows_explicit_active_profile(tmp_path):
    f = tmp_path / "ap.json"
    f.write_text(json.dumps({"detectors": [{"detector_id": A_ID, "role": "ACTIVE", "threshold": 1.684}]}), encoding="utf-8")
    r = sources.read_profile(tmp_path / "nowhere", f)
    assert r["data"]["detectors"] == [{"id": A_ID, "role": "ACTIVE", "threshold": 1.684}]


def test_mock_cleans_pressure_after_switch_and_shows_live_counter():
    seen = {}
    for t in (30, 60, 70, 80):
        v, obs = state.derive(mock.build(t, now=NOW + timedelta(seconds=t)), seen)
        seen.update(obs)
    assert v["pressure"]["value"] == "ENDED" and v["ui"]["verify"]["live"]["new"] > 0
