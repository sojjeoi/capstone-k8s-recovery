#!/usr/bin/env python3
"""trial_observer.py 오프라인 검증(2026-09-20) - 가짜 kubectl 출력·합성 기록만 쓴다(클러스터 접근 없음, conftest의
cluster_guard가 강제). 계약서 5.8: 이벤트 시각만으로 teardown 단정 금지, transition_straddling 별도 집계, 단발이고 Ready·
Endpoint·restart 영향이 없으면 profile 실패가 아니며 연속 실패·Ready=False·Endpoint 제거·restart는 trial 실패."""
import io
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import trial_observer as obs

T0 = 1_789_000_000.0
OFFSET = 0.3
TARGET = "vllm-serving-a"


def iso_z(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pod(**over):
    base = {"uid": "u-a", "app": "vllm-serving", "hash": "A", "phase": "Running", "ready": True,
            "ready_since": "2026-09-19T00:00:00Z", "restarts": 0, "deleting": False, "ip": "10.0.0.5"}
    base.update(over)
    return base


def state(**over):
    st = {"pods": {TARGET: pod()}, "services": {"vllm-active": {"app": "vllm-serving", "rollouts-pod-template-hash": "A"}},
          "endpoints": {"vllm-active": {"ready": [TARGET], "not_ready": []}},
          "rollout": {"phase": "Healthy", "abort": False, "current_hash": "A", "stable_rs": "A", "active_selector": "A",
                      "preview_selector": "A"},
          "chaos": {}, "nodes": {"n1": {"ready": True, "bad": []}}, "events": [],
          "local": {"recovery_policy": 200, "prometheus": 200}}
    st.update(over)
    return st


def kubelet_event(worker_epoch, message, count=1, uid="ev-1", kind="Readiness"):
    stamp = iso_z(worker_epoch)
    return {"pod": TARGET, "reason": "Unhealthy", "message": f"{kind} probe failed: {message}", "count": count, "uid": uid,
            "first": stamp, "last": stamp}


TIMEOUT = 'Get "http://10.0.0.5:8000/health": context deadline exceeded (Client.Timeout exceeded while awaiting headers)'


def stage_times(i):
    create = T0 + 100 + 130 * i
    return {"create": create, "allinjected": create + 2, "delete_request": create + 92, "gone": create + 94.3}


def chaos_records(i, name=None):
    s, name = stage_times(i), name or f"cr-{i + 1}"
    created = f"2026-09-19T00:0{i}:00Z"
    common = {"type": "chaos", "name": name, "target": TARGET, "created": created}
    return [{**common, "t": s["create"], "event": "ADDED", "deleting": None, "injected_since": None},
            {**common, "t": s["allinjected"], "event": "MODIFIED", "deleting": None, "injected_since": created},
            {**common, "t": s["delete_request"], "event": "MODIFIED", "deleting": "x", "injected_since": created},
            {**common, "t": s["gone"], "event": "DELETED", "deleting": "x", "injected_since": created}]


def records(states, chaos_stages=(0, 1, 2, 3), offset=OFFSET):
    out = [{"type": "header", "started_at": "x", "interval": 3.0, "worker_offset": {"offset_sec": offset, "rtt_sec": 0.5}}]
    for i in chaos_stages:
        out += chaos_records(i)
    out += [{"type": "state", "t": t, "state": st} for t, st in states]
    return sorted(out, key=lambda r: r.get("t", -1.0))


D4 = stage_times(3)["delete_request"]


def quiet_states():
    return [(T0, state()), (D4 + 40, state())]


# ---- 상태 수집 ------------------------------------------------------------------------------------
def test_build_state_extracts_the_fields_the_analysis_needs():
    items = [
        {"kind": "Pod", "metadata": {"name": TARGET, "uid": "u-a", "labels": {"app": "vllm-serving", "rollouts-pod-template-hash": "A"}},
         "status": {"phase": "Running", "podIP": "10.0.0.5",
                    "conditions": [{"type": "Ready", "status": "True", "lastTransitionTime": "2026-09-19T00:00:00Z"}],
                    "containerStatuses": [{"restartCount": 2}]}},
        {"kind": "Pod", "metadata": {"name": "ramp-probe-x", "uid": "u2", "deletionTimestamp": "t"}, "status": {}},
        {"kind": "Service", "metadata": {"name": "vllm-active"}, "spec": {"selector": {"app": "vllm-serving"}}},
        {"kind": "Endpoints", "metadata": {"name": "vllm-active"}, "subsets": [
            {"addresses": [{"targetRef": {"name": TARGET}}], "notReadyAddresses": [{"targetRef": {"name": "vllm-serving-b"}}]}]},
        {"kind": "Rollout", "metadata": {"name": "vllm-serving"},
         "status": {"phase": "Healthy", "currentPodHash": "A", "stableRS": "A",
                    "blueGreen": {"activeSelector": "A", "previewSelector": "A"}}},
        {"kind": "NetworkChaos", "metadata": {"name": "cr-1", "creationTimestamp": "c", "deletionTimestamp": "d"},
         "spec": {"selector": {"pods": {"vllm-serving": [TARGET]}}},
         "status": {"conditions": [{"type": "AllInjected", "status": "True", "lastTransitionTime": "i"}]}},
        {"kind": "Node", "metadata": {"name": "n1"}, "status": {"conditions": [
            {"type": "Ready", "status": "True"}, {"type": "MemoryPressure", "status": "True"}]}},
        {"kind": "Event", "metadata": {"uid": "e1"}, "involvedObject": {"kind": "Pod", "name": TARGET}, "reason": "Unhealthy",
         "message": "Readiness probe failed: x", "count": 2, "firstTimestamp": "a", "lastTimestamp": "b"},
        {"kind": "Event", "metadata": {"uid": "e2"}, "involvedObject": {"kind": "Pod", "name": TARGET}, "reason": "Pulled"},
        {"kind": "Event", "metadata": {"uid": "e3"}, "involvedObject": {"kind": "Pod", "name": "ramp-probe-x"}, "reason": "Unhealthy"},
    ]
    st = obs.build_state(items)
    assert st["pods"][TARGET] == pod(restarts=2) and st["pods"]["ramp-probe-x"]["deleting"]
    assert st["endpoints"]["vllm-active"] == {"ready": [TARGET], "not_ready": ["vllm-serving-b"]}
    assert st["rollout"]["active_selector"] == "A" and st["services"]["vllm-active"] == {"app": "vllm-serving"}
    assert st["chaos"]["cr-1"] == {"target": TARGET, "created": "c", "deleting": "d", "injected_since": "i"}
    assert st["nodes"]["n1"] == {"ready": True, "bad": ["MemoryPressure"]}
    assert [e["uid"] for e in st["events"]] == ["e1"], "vllm pod의 Unhealthy/Killing 이벤트만 남긴다"


def test_parse_watch_stream_reads_pretty_printed_objects():
    text = json.dumps({"type": "ADDED", "object": {"metadata": {"name": "a"}}}, indent=2) + "\n" \
        + json.dumps({"type": "DELETED", "object": {"metadata": {"name": "a"}}}, indent=2) + "\n"
    events = list(obs.parse_watch_stream(io.StringIO(text)))
    assert [e["type"] for e in events] == ["ADDED", "DELETED"] and events[0]["object"]["metadata"]["name"] == "a"


def test_parse_watch_stream_reads_the_one_line_compact_objects_kubectl_really_prints():
    """실측: `kubectl get -w --output-watch-events -o json`은 이벤트마다 한 줄짜리 compact JSON을 낸다(처음 구현이 이 형식을 놓쳐
    native 파일럿의 CR 스트림이 비었다)."""
    text = "".join(json.dumps({"type": kind, "object": {"metadata": {"name": "cr", "labels": {"k": "{}"}}}},
                               separators=(",", ":")) + "\n" for kind in ("ADDED", "MODIFIED", "DELETED"))
    assert [e["type"] for e in obs.parse_watch_stream(io.StringIO(text))] == ["ADDED", "MODIFIED", "DELETED"]
    mixed = io.StringIO(text + json.dumps({"type": "ADDED", "object": {"x": {"y": 1}}}, indent=2) + "\n")
    assert len(list(obs.parse_watch_stream(mixed))) == 4


def test_chaos_summary_marks_allinjected_even_though_chaos_mesh_conditions_have_no_transition_time():
    def item(conds):
        return {"metadata": {"name": "cr", "creationTimestamp": "c"}, "spec": {"selector": {"pods": {"vllm-serving": [TARGET]}}},
                "status": {"conditions": conds}}
    assert obs.chaos_summary(item([{"type": "AllInjected", "status": "True", "reason": ""}]))["injected_since"] is True
    assert obs.chaos_summary(item([{"type": "AllInjected", "status": "False"}]))["injected_since"] is None
    with_time = [{"type": "AllInjected", "status": "True", "lastTransitionTime": "2026-09-19T00:00:00Z"}]
    assert obs.chaos_summary(item(with_time))["injected_since"] == "2026-09-19T00:00:00Z"


class _Recorder:
    def __init__(self):
        self.records = []

    def write(self, record):
        self.records.append(record)


def test_chaos_stream_records_each_watch_event_with_the_local_receive_time():
    payload = "".join(json.dumps({"type": kind, "object": {
        "metadata": {"name": "cr-1", "creationTimestamp": "c", **({"deletionTimestamp": "d"} if kind == "MODIFIED" else {})},
        "spec": {"selector": {"pods": {"vllm-serving": [TARGET]}}}}}, indent=2) + "\n" for kind in ("ADDED", "MODIFIED"))
    rec, clock = _Recorder(), iter(range(100, 200))
    proc = SimpleNamespace(stdout=io.StringIO(payload), poll=lambda: 0, terminate=lambda: None)
    holder = []

    def popen(*a, **k):
        holder[0].stop_flag.set()  # 한 번만 읽고 끝나게(재접속 루프 진입 전에 중지 표시)
        return proc
    stream = obs.ChaosStream(rec, now=lambda: float(next(clock)), popen=popen)
    holder.append(stream)
    stream.run()
    chaos = [r for r in rec.records if r["type"] == "chaos"]
    assert [(r["event"], r["target"], bool(r["deleting"])) for r in chaos] == [("ADDED", TARGET, False), ("MODIFIED", TARGET, True)]
    assert chaos[0]["t"] < chaos[1]["t"]


def test_observer_only_ever_reads_the_cluster():
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"items": []}), stderr="")
    obs.kubectl_items(run)
    assert calls == [["kubectl", "get", obs.RESOURCES, "-n", "vllm-serving", "-o", "json"]]
    assert obs.CHAOS_WATCH_CMD[:2] == ["kubectl", "get"]
    source = Path(obs.__file__).read_text(encoding="utf-8")
    verbs = re.findall(r'"kubectl",\s*"([^"]+)"', source)
    assert verbs and set(verbs) == {"get"}, f"kubectl 호출은 get뿐이어야 한다: {verbs}"


def test_state_poller_writes_only_changes_alerts_and_heartbeats():
    times = iter([0.0, 3.0, 6.0, 40.0])
    rec, alerts = _Recorder(), []
    new_pod = {"kind": "Pod", "metadata": {"name": TARGET, "uid": "u"}, "status": {
        "conditions": [{"type": "Ready", "status": "False"}], "containerStatuses": [{"restartCount": 1}]}}
    feeds = iter([[], [], [new_pod], []])
    poller = obs.StatePoller(lambda: next(feeds), rec, now=lambda: next(times), alert=alerts.append)
    for _ in range(4):
        poller.poll()
    assert [r["type"] for r in rec.records] == ["state", "state", "state"], "같은 상태(2번째)는 기록 안 함, 3번째 변경·4번째 되돌림 기록"
    assert any("pod 생성" in a for a in alerts) and any("pod 소멸" in a for a in alerts)
    times2, rec2 = iter([0.0, 3.0, 40.0]), _Recorder()
    quiet = obs.StatePoller(lambda: [], rec2, now=lambda: next(times2), alert=lambda *_: None)
    for _ in range(3):
        quiet.poll()
    assert [r["type"] for r in rec2.records] == ["state", "heartbeat"], "변화가 없어도 30초마다 heartbeat"


def test_ready_false_and_restart_raise_alerts():
    alerts, rec = [], _Recorder()
    ready = {"kind": "Pod", "metadata": {"name": TARGET, "uid": "u"}, "status": {
        "conditions": [{"type": "Ready", "status": "True"}], "containerStatuses": [{"restartCount": 0}]}}
    bad = json.loads(json.dumps(ready))
    bad["status"]["conditions"][0]["status"] = "False"
    bad["status"]["containerStatuses"][0]["restartCount"] = 1
    feeds, times = iter([[ready], [bad]]), iter([0.0, 3.0])
    poller = obs.StatePoller(lambda: next(feeds), rec, now=lambda: next(times), alert=alerts.append)
    poller.poll()
    poller.poll()
    assert any("Ready=False" in a for a in alerts) and any("restart 0->1" in a for a in alerts)


# ---- 분석: 계약서 5.8 -----------------------------------------------------------------------------
def analyze(states, **kw):
    return obs.analyze(records(states, **kw), 11, offset_sec=OFFSET)


def test_a_quiet_trial_passes_with_the_stage_timeline_from_the_chaos_stream():
    a = analyze(quiet_states())
    assert a["verdict"] == "PASS" and a["stop_conditions"] == [] and a["target"] == TARGET
    assert [s["source"] for s in a["stages"]] == ["watch"] * 4 and all(s["allinjected"] and s["gone"] for s in a["stages"])
    assert a["row"] == {"transition_straddling": 0, "ready_transition": False, "endpoint_impact": False, "restart": False,
                        "steady": 0, "pure_teardown": 0}


def test_a_teardown_time_timeout_failure_whose_probe_spans_the_deletion_is_transition_straddling_and_not_a_failure():
    """45절의 두 회차와 같은 모양 - 삭제 요청 +5.2초에 찍힌 11초 timeout 실패(추정 probe 시작 -5.8초)."""
    ev = kubelet_event(D4 + 5, TIMEOUT)
    a = analyze([(T0, state()), (D4 + 8, state(events=[ev])), (D4 + 40, state(events=[ev]))])
    (probe,) = a["probe_events"]
    assert probe["segment"] == "transition_straddling_4" and probe["ambiguous"] is False
    assert probe["probe_start_iso"] < a["stages"][3]["delete_request"] < probe["t_pc_iso"]
    assert a["verdict"] == "PASS" and a["row"]["transition_straddling"] == 1 and a["row"]["steady"] == 0 \
        and a["row"]["pure_teardown"] == 0 and a["stop_conditions"] == []


def test_a_failure_whose_probe_started_after_the_deletion_is_pure_teardown():
    ev = kubelet_event(D4 + 14, TIMEOUT)   # 이벤트 +14.2초 -> 추정 시작 +3.2초(삭제 요청 뒤)
    a = analyze([(T0, state()), (D4 + 16, state(events=[ev]))])
    assert [p["segment"] for p in a["probe_events"]] == ["teardown_4"]
    assert a["row"]["transition_straddling"] == 0 and a["row"]["pure_teardown"] == 1 and a["verdict"] == "PASS"


def test_a_steady_failure_is_a_stop_condition():
    ev = kubelet_event(D4 - 30, TIMEOUT)
    a = analyze([(T0, state()), (D4 - 28, state(events=[ev]))])
    assert a["probe_events"][0]["segment"] == "steady_4" and a["verdict"] == "FAIL"
    assert [c for c, _ in a["stop_conditions"]] == ["S1_steady_failure"] and a["row"]["steady"] == 1


def test_two_failures_in_the_same_transition_are_consecutive_and_fail_the_trial():
    first, second = kubelet_event(D4 + 5, TIMEOUT, count=1), kubelet_event(D4 + 9, TIMEOUT, count=2)
    a = analyze([(T0, state()), (D4 + 7, state(events=[first])), (D4 + 11, state(events=[second]))])
    assert a["verdict"] == "FAIL" and "S5_consecutive_transition_failures" in [c for c, _ in a["stop_conditions"]]
    assert a["row"]["transition_straddling"] == 2


def test_ready_false_a_ready_flap_endpoint_removal_and_restart_each_fail_the_trial():
    def codes(mutations):
        a = analyze([(T0, state()), (D4 - 10, state(**mutations)), (D4 + 40, state(**mutations))])
        return [c for c, _ in a["stop_conditions"]]
    assert codes({"pods": {TARGET: pod(ready=False, ready_since="2026-09-19T00:05:00Z")}}) == ["S2_ready_false"]
    assert codes({"pods": {TARGET: pod(ready_since="2026-09-19T00:05:00Z")}}) == ["S2_ready_false"], "폴링 사이 순간 전이"
    assert codes({"endpoints": {"vllm-active": {"ready": [], "not_ready": [TARGET]}}}) == ["S3_endpoint_removed"]
    assert codes({"pods": {TARGET: pod(restarts=1)}}) == ["S4_restart_or_replaced"]
    row = analyze([(T0, state()), (D4, state(pods={TARGET: pod(ready=False)}))])["row"]
    assert row["ready_transition"] is True


def test_a_promotion_switch_is_not_an_endpoint_removal_and_the_old_pod_deletion_after_it_is_legit():
    switched = state(services={"vllm-active": {"app": "vllm-serving", "rollouts-pod-template-hash": "B"}},
                     endpoints={"vllm-active": {"ready": ["vllm-serving-b"], "not_ready": []}},
                     rollout={"phase": "Healthy", "abort": False, "current_hash": "B", "stable_rs": "B", "active_selector": "B",
                              "preview_selector": "B"},
                     pods={TARGET: pod(), "vllm-serving-b": pod(uid="u-b", hash="B")})
    gone = state(services=switched["services"], endpoints=switched["endpoints"], rollout=switched["rollout"],
                 pods={"vllm-serving-b": pod(uid="u-b", hash="B")})
    a = analyze([(T0, state()), (D4 - 60, switched), (D4 - 20, gone)])
    assert a["verdict"] == "PASS" and a["promotion_observed"] is not None and a["target_lost_before_promotion"] is None


def test_probe_failures_after_the_targets_own_termination_by_scale_down_are_shutdown_artifacts():
    """fixed_threshold 실측(2026-09-20): promotion 뒤 Argo가 구 pod(=chaos target)를 scale-down했고, 그 종료 중(Killing 이벤트 뒤)에
    readiness 실패 3건이 찍혔다 - CR 삭제(같은 시각대)와 겹쳐도 순수 teardown/straddling이 아니라 종료 아티팩트다."""
    killing = {"pod": TARGET, "reason": "Killing", "message": "Stopping container vllm", "count": 1, "uid": "ev-k",
               "first": iso_z(D4 - 5), "last": iso_z(D4 - 5)}
    fails = [kubelet_event(D4 + 1, TIMEOUT, uid="ev-1", count=1), kubelet_event(D4 + 5, "dial tcp: connection refused", uid="ev-2"),
             kubelet_event(D4 + 16, TIMEOUT, uid="ev-3")]
    switched = dict(services={"vllm-active": {"app": "vllm-serving", "rollouts-pod-template-hash": "B"}},
                    endpoints={"vllm-active": {"ready": ["vllm-serving-b"], "not_ready": []}},
                    rollout={"phase": "Healthy", "abort": False, "current_hash": "B", "stable_rs": "B", "active_selector": "B",
                             "preview_selector": "B"})
    terminating = state(**switched, pods={TARGET: pod(deleting=True), "vllm-serving-b": pod(uid="u-b", hash="B")}, events=[killing])
    after = state(**switched, pods={"vllm-serving-b": pod(uid="u-b", hash="B")}, events=[killing, *fails])
    a = analyze([(T0, state()), (D4 - 30, state(**switched, pods={TARGET: pod(), "vllm-serving-b": pod(uid="u-b", hash="B")})),
                 (D4 - 4, terminating), (D4 + 20, after)])
    assert {p["segment"] for p in a["probe_events"]} == {"shutdown"} and a["counts"]["shutdown"] == 3
    assert a["verdict"] == "PASS" and a["stop_conditions"] == [] and a["row"]["transition_straddling"] == 0
    assert a["target_termination_start"] is not None and a["promotion_observed"] is not None


def test_the_target_disappearing_without_a_promotion_is_a_replacement():
    a = analyze([(T0, state()), (D4 - 60, state(pods={}))])
    assert [c for c, _ in a["stop_conditions"]] == ["S4_restart_or_replaced"] and a["row"]["restart"] is True


def test_a_node_anomaly_and_a_dead_port_forward_are_stop_conditions():
    a = analyze([(T0, state()), (D4, state(nodes={"n1": {"ready": True, "bad": ["MemoryPressure"]}},
                                            local={"recovery_policy": None, "prometheus": 200}))])
    assert {c for c, _ in a["stop_conditions"]} == {"S6_node_anomaly", "S7_local_port_forward_down"}


def test_without_a_chaos_stream_the_timeline_falls_back_to_the_state_polls():
    chaos_present = {"cr-1": {"target": TARGET, "created": "2026-09-19T00:00:00Z", "deleting": None, "injected_since": "i"}}
    recs = [{"type": "header", "worker_offset": None}, {"type": "state", "t": T0, "state": state(chaos=chaos_present)},
            {"type": "state", "t": T0 + 95, "state": state()}]
    a = obs.analyze(recs, 11, offset_sec=0.0)
    assert a["stages"][0]["source"] == "poll" and a["stages"][0]["gone"] is not None and a["offset_source"] == "given"


def test_pure_liveness_failures_outside_steady_are_recorded_as_notes_not_stop_conditions():
    ev = kubelet_event(D4 + 30, "Get x: connection refused", kind="Liveness")
    a = analyze([(T0, state()), (D4 + 32, state(events=[ev]))])
    assert a["verdict"] == "PASS" and a["notes"], "계약서 중단 목록에 없는 순수 liveness 실패는 기록만"


def test_analyze_cli_prints_the_final_row_and_returns_by_verdict(tmp_path, capsys):
    path = tmp_path / "obs.jsonl"
    ev = kubelet_event(D4 + 5, TIMEOUT)
    path.write_text("\n".join(json.dumps(r) for r in records([(T0, state()), (D4 + 8, state(events=[ev]))])), encoding="utf-8")
    assert obs.main(["analyze", str(path), "--probe-timeout", "11", "--offset-sec", "0.3", "--json-out", str(tmp_path / "a.json")]) == 0
    out = capsys.readouterr().out
    assert "transition_straddling 1건" in out and "Ready 전이 없음" in out and "Endpoint 영향 없음" in out and "restart 없음" in out
    assert json.loads((tmp_path / "a.json").read_text(encoding="utf-8"))["verdict"] == "PASS"
    bad = records([(T0, state()), (D4, state(pods={TARGET: pod(restarts=2)}))])
    path.write_text("\n".join(json.dumps(r) for r in bad), encoding="utf-8")
    assert obs.main(["analyze", str(path), "--probe-timeout", "11", "--offset-sec", "0.3"]) == 2


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
