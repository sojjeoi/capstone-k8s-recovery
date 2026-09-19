#!/usr/bin/env python3
"""calibrate_network_tolerant_probe.py 오프라인 검증(2026-09-19, 사전 등록 §42).

- overlay 렌더 diff가 readiness/liveness timeoutSeconds 2경로만 허용하고 나머지(CPU·모델·startupProbe·이미지·
  서비스 selector 등)의 변경은 거부하는지
- calibration pod가 base template과 (nodeSelector·두 timeout 외) 같고 어떤 selector에도 안 걸리는지
- §42.6 판정 표(권고 분류)와 즉시 실패 H1~H9 평가
- 예외·중단(KeyboardInterrupt)·timeout·정리 실패·운영 상태 변경에서도 NetworkChaos와 pod가 정리되고, 도구가 pod·CR
  외에는 아무 것도 변경하지 않는지(가짜 클러스터로 - 실제 클러스터 접근 없음, conftest의 cluster_guard가 강제)
"""
import copy
import json
import shutil
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import calibrate_network_tolerant_probe as cal
import network_degrade_adapter as nda

needs_kubectl = pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl 없음(로컬 렌더링 필요)")
RUN_ID = "calib-net-tolerant-20260919t000000z"
PAYLOAD = {"model": "m", "prompt": "Hi", "max_tokens": 1}


# ---- 공통 fixture ---------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def base():
    return cal.load_base()


def fake_rendered(base, timeout=10):
    """kubectl 없이 overlay 결과를 흉내낸다 - base에 timeout 2경로만 추가."""
    rendered = copy.deepcopy(base)
    c = rendered[cal.ROLLOUT_KEY]["spec"]["template"]["spec"]["containers"][0]
    c["readinessProbe"]["timeoutSeconds"] = timeout
    c["livenessProbe"]["timeoutSeconds"] = timeout
    return rendered


def pod_entry(uid, labels, ready=True, restarts=0, phase="Running", terminated=False, deleting=False,
              node="sj-worker", ip="10.0.0.5"):
    return {"uid": uid, "labels": labels, "phase": phase, "ready": ready, "restarts": restarts,
            "terminated": terminated, "deleting": deleting, "node": node, "ip": ip}


def make_snapshot():
    sel = {"app": "vllm-serving", "rollouts-pod-template-hash": "659795b9df"}
    return {
        "nodes": {"sj-control": {"ready": True, "bad": []}, "sj-worker": {"ready": True, "bad": []}},
        "rollout": {"generation": 30, "phase": "Healthy", "abort": False, "current_hash": "659795b9df",
                    "stable_rs": "659795b9df", "active_selector": "659795b9df", "preview_selector": "659795b9df"},
        "services": {"vllm-active": dict(sel), "vllm-preview": dict(sel)},
        "pods": {"vllm-serving-659795b9df-xzmkr": pod_entry("u-vllm", {"app": "vllm-serving"}),
                 "recovery-policy-abc": pod_entry("u-rp", {"app": "recovery-policy"}, ip="10.0.0.6")},
        "chaos": [], "events": [], "context": None}


# ---- A. overlay 렌더 diff -------------------------------------------------------------------------
@needs_kubectl
def test_real_overlay_render_changes_only_the_two_timeouts(base):
    changes = cal.verify_overlay(cal.render_overlay(), base)
    assert {p for p, _, _ in changes} == cal.EXPECTED_OVERLAY_CHANGES
    assert all(new == 10 for _, _, new in changes), "이번 overlay 후보는 10초"


def test_verify_overlay_accepts_exactly_the_two_paths(base):
    assert len(cal.verify_overlay(fake_rendered(base, 12), base)) == 2


@pytest.mark.parametrize("mutate,what", [
    (lambda c: c["resources"]["limits"].__setitem__("cpu", "4"), "CPU limit"),
    (lambda c: c["args"].append("--model=other"), "모델 인자"),
    (lambda c: c["startupProbe"].__setitem__("periodSeconds", 5), "startupProbe"),
    (lambda c: c.__setitem__("image", "other:latest"), "이미지"),
    (lambda c: c["readinessProbe"].__setitem__("periodSeconds", 1), "readiness period"),
    (lambda c: c["livenessProbe"].__setitem__("failureThreshold", 9), "liveness failureThreshold"),
])
def test_verify_overlay_rejects_any_other_rollout_change(base, mutate, what):
    rendered = fake_rendered(base)
    mutate(rendered[cal.ROLLOUT_KEY]["spec"]["template"]["spec"]["containers"][0])
    with pytest.raises(cal.OverlayError):
        cal.verify_overlay(rendered, base)


def test_verify_overlay_rejects_service_selector_or_other_resource_change(base):
    rendered = fake_rendered(base)
    rendered[("Service", "vllm-active")]["spec"]["selector"]["app"] = "other"
    with pytest.raises(cal.OverlayError, match="Service"):
        cal.verify_overlay(rendered, base)
    rendered = fake_rendered(base)
    rendered.pop(("Service", "vllm-preview"))
    with pytest.raises(cal.OverlayError, match="집합"):
        cal.verify_overlay(rendered, base)


def test_verify_overlay_rejects_only_one_timeout_changed_or_nonpositive(base):
    rendered = fake_rendered(base)
    del rendered[cal.ROLLOUT_KEY]["spec"]["template"]["spec"]["containers"][0]["livenessProbe"]["timeoutSeconds"]
    with pytest.raises(cal.OverlayError, match="경로"):
        cal.verify_overlay(rendered, base)
    with pytest.raises(cal.OverlayError, match="양수"):
        cal.verify_overlay(fake_rendered(base, 0), base)


# ---- B. calibration pod --------------------------------------------------------------------------
def test_calibration_pod_equals_the_template_except_placement_and_timeouts(base):
    rollout = fake_rendered(base)[cal.ROLLOUT_KEY]
    pod = cal.build_calibration_pod(rollout, RUN_ID, "sj-worker")
    cal.verify_calibration_pod(pod, base)
    c_pod = pod["spec"]["containers"][0]
    c_base = base[cal.ROLLOUT_KEY]["spec"]["template"]["spec"]["containers"][0]
    for field in ("image", "args", "resources", "ports", "volumeMounts", "startupProbe"):
        assert c_pod[field] == c_base[field], field
    assert pod["spec"]["volumes"] == base[cal.ROLLOUT_KEY]["spec"]["template"]["spec"]["volumes"]
    assert (c_pod["readinessProbe"]["timeoutSeconds"], c_pod["livenessProbe"]["timeoutSeconds"]) == (10, 10)
    assert pod["spec"]["nodeSelector"] == {"kubernetes.io/hostname": "sj-worker"}
    assert pod["metadata"]["name"] == f"vllm-{RUN_ID}" and len(pod["metadata"]["name"]) <= 63


def test_candidate_override_changes_only_the_two_timeouts(base):
    pod = cal.build_calibration_pod(fake_rendered(base)[cal.ROLLOUT_KEY], RUN_ID, "sj-worker", candidate_sec=12)
    cal.verify_calibration_pod(pod, base)
    c = pod["spec"]["containers"][0]
    assert (c["readinessProbe"]["timeoutSeconds"], c["livenessProbe"]["timeoutSeconds"]) == (12, 12)


def test_calibration_pod_labels_match_no_service_rollout_or_monitor_selector(base):
    pod = cal.build_calibration_pod(fake_rendered(base)[cal.ROLLOUT_KEY], RUN_ID, "sj-worker")
    labels = pod["metadata"]["labels"]
    assert labels["app"] == "vllm-calibration"
    for key, doc in base.items():
        if key[0] == "Service":
            assert not cal._labels_match(doc["spec"]["selector"], labels), key
    assert not cal._labels_match(base[cal.ROLLOUT_KEY]["spec"]["selector"]["matchLabels"], labels)
    sm = base[("ServiceMonitor", "vllm-serving")]["spec"]["selector"]["matchLabels"]
    assert not cal._labels_match(sm, labels), "ServiceMonitor 셀렉터는 Service 라벨이지만 pod 라벨도 겹치면 안 됨"


def test_verify_calibration_pod_rejects_serving_labels_owner_and_extra_changes(base):
    def fresh():
        return cal.build_calibration_pod(fake_rendered(base)[cal.ROLLOUT_KEY], RUN_ID, "sj-worker")
    pod = fresh()
    pod["metadata"]["labels"]["app"] = "vllm-serving"
    with pytest.raises(cal.OverlayError, match="selector"):
        cal.verify_calibration_pod(pod, base)
    pod = fresh()
    pod["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet"}]
    with pytest.raises(cal.OverlayError, match="소속"):
        cal.verify_calibration_pod(pod, base)
    pod = fresh()
    pod["spec"]["containers"][0]["resources"]["limits"]["cpu"] = "8"
    with pytest.raises(cal.OverlayError, match="다른 필드"):
        cal.verify_calibration_pod(pod, base)


# ---- C. 분석·판정 표 ------------------------------------------------------------------------------
def test_summarize_health_and_completion():
    samples = ([{"kind": "health", "status": 200, "error": None, "latency": x} for x in (1.0, 2.0, 3.0, 4.0)]
               + [{"kind": "health", "status": None, "error": "timeout", "latency": 30.0},
                  {"kind": "completion", "status": 200, "error": None, "latency": 0.5},
                  {"kind": "completion", "status": 500, "error": None, "latency": 0.1}])
    h, c = cal.summarize_health(samples), cal.summarize_completion(samples)
    assert (h["n"], h["ok"], h["errors"], h["max"], h["p50"]) == (5, 4, 1, 4.0, 2.0)
    assert (c["n"], c["ok"], c["success_rate"]) == (2, 1, 0.5)


def window(name, ok=90, errors=0, l_max=8.0, fraction=1.0, kubelet=0):
    return {"name": name, "completed_fraction": fraction, "kubelet": {"probe_failures": kubelet},
            "health": {"n": ok + errors, "ok": ok, "errors": errors, "max": l_max}}


WORST = cal.STAGES[-1]["name"]


@pytest.mark.parametrize("l_max,expected,t_min", [
    (8.85, "RAISE", 12),       # T_req = max(11.06, 10.35) - 후보 10초는 마진 부족
    (8.0, "KEEP", 10),         # 1.25 x 8.0 = 10.0 정확히 - 경계
    (7.0, "LOWER", 9),         # T_req = max(8.75, 8.5) - 후보가 과도
    (11.0, "RAISE", 14),       # 13.75 -> 14 <= 상한 15
    (12.0, "RAISE", 15),       # 15.0 -> 15 == 상한(허용)
    (12.5, "INSUFFICIENT", 16),  # 15.625 -> 16 > 상한 15
])
def test_recommendation_table_for_a_clean_run(l_max, expected, t_min):
    a = cal.recommend([window(WORST, l_max=l_max)], 10.0, None)
    assert (a["run_outcome"], a["recommendation"], a["T_min"]) == ("PASS", expected, t_min)
    assert a["provisional"] is True and a["T_cap"] == 15.0


def test_recommendation_is_none_on_hard_fail_or_incomplete_worst_window():
    assert cal.recommend([window(WORST)], 10.0, {"code": "H1"})["recommendation"] == "NONE"
    assert cal.recommend([window(WORST)], 10.0, {"code": "H1"})["run_outcome"] == "FAIL"
    assert cal.recommend([], 10.0, None)["recommendation"] == "NONE", "stage-4 창 없음"
    assert cal.recommend([window(WORST, fraction=0.79)], 10.0, None)["recommendation"] == "NONE"
    assert cal.recommend([window(WORST, ok=29)], 10.0, None)["recommendation"] == "NONE"
    assert cal.recommend([window(WORST, fraction=0.8, ok=30)], 10.0, None)["recommendation"] == "KEEP", \
        "경계(80%·30개)는 허용 - L_max 8.0 -> T_req 10.0 == 후보"


def test_kubelet_or_client_failures_make_the_run_marginal():
    a = cal.recommend([window(WORST, l_max=10.5, kubelet=2)], 10.0, None)
    assert (a["run_outcome"], a["recommendation"], a["kubelet_probe_failures"]) == ("MARGINAL", "RAISE", 2)
    a = cal.recommend([window("baseline", errors=1), window(WORST, l_max=8.0)], 10.0, None)
    assert (a["run_outcome"], a["client_health_failures"]) == ("MARGINAL", 1)
    inconsistent = cal.recommend([window(WORST, l_max=8.0, kubelet=1)], 10.0, None)
    assert inconsistent["run_outcome"] == "MARGINAL" and inconsistent["recommendation"] == "NONE"
    assert "불일치" in inconsistent["reason"], "kubelet 실패가 있는데 L_max < 후보면 측정 불일치"


def test_required_timeout_is_the_larger_of_relative_and_absolute_margin():
    assert cal.required_timeout(8.0) == pytest.approx(10.0)
    assert cal.required_timeout(2.0) == pytest.approx(3.5), "작은 값에서는 절대 마진 1.5초가 지배"
    assert cal.required_timeout(20.0) == pytest.approx(25.0)


# ---- D. 즉시 실패 평가 ---------------------------------------------------------------------------
def state_with_pod(**kw):
    s = cal.RunState(pod_name="cal-pod", created=True, chaos_names={"cr-0"})
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def snapshot_with_cal(**over):
    snap = make_snapshot()
    snap["pods"]["cal-pod"] = pod_entry("u-cal", {"app": "vllm-calibration"}, ip="10.0.9.9")
    snap.update(over)
    return snap


def codes(baseline, snap, state):
    return [c for c, _ in cal.evaluate_violations(baseline, snap, state)]


def test_a_clean_snapshot_has_no_violations_and_tracks_uid_and_ready():
    baseline, snap, state = make_snapshot(), snapshot_with_cal(), state_with_pod()
    assert codes(baseline, snap, state) == []
    assert state.pod_uid == "u-cal" and state.ready_seen is True


def test_h1_h2_calibration_pod_failures():
    baseline = make_snapshot()
    snap = snapshot_with_cal()
    snap["pods"]["cal-pod"]["restarts"] = 1
    assert "H1" in codes(baseline, snap, state_with_pod())
    snap = snapshot_with_cal()
    snap["pods"]["cal-pod"]["terminated"] = True
    assert "H1" in codes(baseline, snap, state_with_pod())
    snap = snapshot_with_cal()
    snap["pods"]["cal-pod"]["phase"] = "Failed"
    assert "H1" in codes(baseline, snap, state_with_pod())
    snap = snapshot_with_cal()
    snap["pods"]["cal-pod"]["uid"] = "other"
    assert "H1" in codes(baseline, snap, state_with_pod(pod_uid="u-cal"))
    snap = snapshot_with_cal()
    del snap["pods"]["cal-pod"]
    assert "H1" in codes(baseline, snap, state_with_pod())
    snap = snapshot_with_cal()
    snap["pods"]["cal-pod"]["ready"] = False
    assert codes(baseline, snap, state_with_pod()) == [], "최초 Ready 전의 NotReady는 정상(기동 중)"
    assert "H2" in codes(baseline, snap, state_with_pod(ready_seen=True)), "Ready 뒤 NotReady는 H2"


def test_h3_node_conditions():
    baseline = make_snapshot()
    for mutate in (lambda n: n["sj-worker"].__setitem__("ready", False),
                   lambda n: n["sj-control"].__setitem__("bad", ["MemoryPressure"])):
        snap = snapshot_with_cal()
        mutate(snap["nodes"])
        assert "H3" in codes(baseline, snap, state_with_pod())


def test_h4_production_pods_restart_replace_notready():
    baseline = make_snapshot()
    for mutate in (lambda p: p["vllm-serving-659795b9df-xzmkr"].__setitem__("restarts", 1),
                   lambda p: p["vllm-serving-659795b9df-xzmkr"].__setitem__("uid", "new"),
                   lambda p: p["recovery-policy-abc"].__setitem__("ready", False),
                   lambda p: p.pop("recovery-policy-abc")):
        snap = snapshot_with_cal()
        mutate(snap["pods"])
        assert "H4" in codes(baseline, snap, state_with_pod())


def test_h5_unexpected_manifest_changes():
    baseline = make_snapshot()
    snap = snapshot_with_cal()
    snap["rollout"]["generation"] = 31
    assert "H5" in codes(baseline, snap, state_with_pod())
    snap = snapshot_with_cal()
    snap["rollout"]["preview_selector"] = "abc"  # preview 출현
    assert "H5" in codes(baseline, snap, state_with_pod())
    snap = snapshot_with_cal()
    snap["services"]["vllm-active"]["rollouts-pod-template-hash"] = "abc"
    assert "H5" in codes(baseline, snap, state_with_pod())
    snap = snapshot_with_cal()
    snap["pods"]["ramp-probe-x"] = pod_entry("u-x", {})
    assert "H5" in codes(baseline, snap, state_with_pod())


def test_h9_foreign_chaos_and_non_null_context():
    baseline = make_snapshot()
    snap = snapshot_with_cal(chaos=["cr-0", "someone-elses"])
    assert codes(baseline, snap, state_with_pod()) == ["H9"], "이 run 소유 CR(cr-0)은 정상, 남의 CR만 위반"
    snap = snapshot_with_cal(context={"run_id": "x"})
    assert "H9" in codes(baseline, snap, state_with_pod())


# ---- E. 스냅샷 파싱·preflight --------------------------------------------------------------------
def test_parse_snapshot_and_unhealthy_count_exclude_startup_probe_failures():
    ns_items = [
        {"kind": "Pod", "metadata": {"name": "p1", "uid": "u1", "labels": {"app": "vllm-serving"}},
         "spec": {"nodeName": "sj-worker"},
         "status": {"phase": "Running", "podIP": "10.0.0.5", "conditions": [{"type": "Ready", "status": "True"}],
                    "containerStatuses": [{"restartCount": 2, "lastState": {"terminated": {"exitCode": 1}}}]}},
        {"kind": "Service", "metadata": {"name": "vllm-active"}, "spec": {"selector": {"app": "vllm-serving"}}},
        {"kind": "Rollout", "metadata": {"name": "vllm-serving", "generation": 30},
         "status": {"phase": "Healthy", "currentPodHash": "h", "stableRS": "h",
                    "blueGreen": {"activeSelector": "h", "previewSelector": "h"}}},
        {"kind": "NetworkChaos", "metadata": {"name": "cr-1"}},
        {"kind": "Event", "involvedObject": {"kind": "Pod", "name": "p1"}, "reason": "Unhealthy", "count": 3,
         "message": 'Readiness probe failed: Get "http://x": context deadline exceeded'},
        {"kind": "Event", "involvedObject": {"kind": "Pod", "name": "p1"}, "reason": "Unhealthy", "count": 2,
         "message": "Liveness probe failed: timeout"},
        {"kind": "Event", "involvedObject": {"kind": "Pod", "name": "p1"}, "reason": "Unhealthy", "count": 40,
         "message": "Startup probe failed: 서버 연결 실패"},
        {"kind": "Event", "involvedObject": {"kind": "Pod", "name": "other"}, "reason": "Unhealthy", "count": 9,
         "message": "Readiness probe failed: x"},
    ]
    node_items = [{"metadata": {"name": "n1"}, "status": {"conditions": [
        {"type": "Ready", "status": "True"}, {"type": "MemoryPressure", "status": "True"}]}}]
    snap = cal.parse_snapshot(ns_items, node_items, None)
    assert snap["pods"]["p1"] == pod_entry("u1", {"app": "vllm-serving"}, ready=True, restarts=2, terminated=True,
                                           ip="10.0.0.5")
    assert snap["rollout"]["current_hash"] == "h" and snap["chaos"] == ["cr-1"]
    assert snap["nodes"]["n1"] == {"ready": True, "bad": ["MemoryPressure"]}
    assert cal.unhealthy_count(snap, "p1") == 5, "Readiness 3 + Liveness 2, Startup 40과 다른 pod 9는 제외"


def test_preflight_problems():
    assert cal.preflight_problems(make_snapshot()) == []

    def problem(mutate):
        snap = make_snapshot()
        mutate(snap)
        return cal.preflight_problems(snap)
    assert problem(lambda s: s["rollout"].__setitem__("preview_selector", "x"))
    assert problem(lambda s: s["rollout"].__setitem__("phase", "Progressing"))
    assert problem(lambda s: s["rollout"].__setitem__("stable_rs", "x"))
    assert problem(lambda s: s["nodes"]["sj-worker"].__setitem__("ready", False))
    assert problem(lambda s: s["pods"].__setitem__("ramp-probe-1", pod_entry("u", {})))
    assert problem(lambda s: s["pods"].pop("recovery-policy-abc"))
    assert problem(lambda s: s["pods"]["vllm-serving-659795b9df-xzmkr"].__setitem__("ready", False))
    assert problem(lambda s: s["chaos"].append("leftover"))
    assert problem(lambda s: s.__setitem__("context", {"run_id": "x"}))


def test_template_fidelity_ignores_annotations_only(base):
    template = base[cal.ROLLOUT_KEY]["spec"]["template"]
    live = copy.deepcopy(template)
    live["metadata"]["annotations"] = {"experiment-prep-ts": "2026-09-19T06:23:18Z"}
    assert cal.template_fidelity_problems(live, template) == []
    live["spec"]["containers"][0]["image"] = "other"
    assert cal.template_fidelity_problems(live, template)


def test_template_fidelity_ignores_the_defaulted_tcp_protocol_but_not_other_protocols(base):
    template = base[cal.ROLLOUT_KEY]["spec"]["template"]
    live = copy.deepcopy(template)
    live["spec"]["containers"][0]["ports"][0]["protocol"] = "TCP"  # API 서버가 채우는 기본값(실측)
    assert cal.template_fidelity_problems(live, template) == []
    live["spec"]["containers"][0]["ports"][0]["protocol"] = "UDP"
    assert cal.template_fidelity_problems(live, template)


# ---- F. KubectlCluster·SshNodeProbe ---------------------------------------------------------------
def test_kubectl_cluster_has_no_methods_that_mutate_rollout_or_services():
    public = {m for m in dir(cal.KubectlCluster) if not m.startswith("_")}
    assert public == {"snapshot", "rollout_template", "create_pod", "delete_pod", "pod_exists"}


def test_kubectl_cluster_commands_and_parsing():
    calls = []

    def run(cmd, capture_output, text, encoding, input=None):
        calls.append((cmd, input))
        joined = " ".join(cmd)
        if "get --raw" in joined:
            out = '{"current": null}'
        elif "get nodes" in joined:
            out = json.dumps({"items": []})
        elif "get pods" in joined:
            out = json.dumps({"items": []})
        elif cmd[1:3] == ["get", "pod"]:
            return SimpleNamespace(returncode=1, stdout="", stderr='Error from server (NotFound): pods "x" not found')
        else:
            out = ""
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    cluster = cal.KubectlCluster(run=run, retries=0)
    snap = cluster.snapshot()
    assert snap["context"] is None and snap["pods"] == {}
    joined = [" ".join(c) for c, _ in calls]
    assert any("get pods,services,events,rollouts.argoproj.io,networkchaos.chaos-mesh.org -n vllm-serving -o json" in j
               for j in joined)
    assert any("/api/v1/namespaces/vllm-serving/services/recovery-policy:8080/proxy/admin/experiment-run" in j
               for j in joined)
    cluster.create_pod({"kind": "Pod", "metadata": {"name": "x"}})
    assert calls[-1][0][1:4] == ["create", "-f", "-"] and json.loads(calls[-1][1])["metadata"]["name"] == "x"
    cluster.delete_pod("x")
    assert calls[-1][0][1:4] == ["delete", "pod", "x"] and "--wait=false" in calls[-1][0]
    assert cluster.pod_exists("x") is False


class _FakeProc:
    def __init__(self, lines, rc=0, stderr=""):
        import io
        self.stdin, self.stdout, self.stderr = io.StringIO(), io.StringIO("".join(lines)), io.StringIO(stderr)
        self.returncode, self.terminated = rc, False

    def wait(self):
        return self.returncode

    def terminate(self):
        self.terminated = True


def test_ssh_node_probe_builds_the_command_and_pipes_the_script():
    import base64
    captured = {}
    lines = [json.dumps({"kind": "health", "seq": 0, "t": 0.0, "latency": 0.1, "status": 200, "error": None}) + "\n",
             json.dumps({"kind": "summary", "elapsed": 1.0, "issued": {"health": 1}}) + "\n"]

    def popen(cmd, **kw):
        captured["cmd"] = cmd
        captured["proc"] = _FakeProc(lines)
        return captured["proc"]
    probe = cal.SshNodeProbe(host="worker-x", ssh="myssh", popen=popen)
    params = {"ip": "10.0.0.1", "duration_sec": 5}
    handle = probe.start(params)
    cmd = captured["cmd"]
    assert cmd[0] == "myssh" and "BatchMode=yes" in cmd and "worker-x" in cmd and cmd[-3:-1] == ["python3", "-"]
    assert json.loads(base64.b64decode(cmd[-1])) == params
    assert handle.result() and [r["kind"] for r in handle.result()] == ["health"] and handle.error() is None


def test_ssh_handle_reports_errors_but_not_after_an_abort():
    proc = _FakeProc([], rc=255, stderr="Permission denied")
    handle = cal._SshHandle(proc, "x = 1\n")
    assert "Permission denied" in handle.error() and "summary=False" in handle.error()
    proc2 = _FakeProc([], rc=255)
    handle2 = cal._SshHandle(proc2, "x = 1\n")
    handle2.abort()
    assert proc2.terminated and handle2.error() is None, "중단한 세션의 실패는 오류가 아님"


# ---- G. 가짜 클러스터로 오케스트레이션 검증 ------------------------------------------------------
class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


class FakeHandle:
    def __init__(self, world, params):
        self.w, self.p = world, params
        self.start, self.delay_ms, self.aborted, self.abort_at = world.clock(), world.active_delay_ms(), False, None

    def done(self):
        return self.aborted or self.w.clock() >= self.start + self.p["duration_sec"]

    def abort(self):
        self.aborted, self.abort_at = True, self.w.clock()

    def result(self):
        end = self.abort_at if self.aborted else self.start + self.p["duration_sec"]
        elapsed = max(0.0, min(end, self.start + self.p["duration_sec"]) - self.start)
        out = []
        for kind, key in (("health", "health_interval_sec"), ("completion", "completion_interval_sec")):
            for i in range(int(elapsed / self.p[key])):
                out.append({"kind": kind, "seq": i, "t": i * self.p[key], "status": 200, "error": None,
                            "latency": self.w.latency_fn(kind, self.delay_ms)})
        return out

    def error(self):
        return None if self.aborted else self.w.probe_error


class FakeWorld:
    """cluster/chaos/probe 세 의존성을 한 세계로 흉내낸다. calls에는 **변경 호출만** 남는다."""

    def __init__(self, clock):
        self.clock, self.state, self.calls, self.cal = clock, make_snapshot(), [], None
        self.chaos, self.hooks, self.extra_events = {}, [], []
        self.injects = self.delete_pod_works = self.chaos_gone_works = True
        self.never_ready, self.probe_error, self.chaos_create_error, self.probe_start_error = False, None, None, None
        self.latency_fn = lambda kind, ms: 0.02 + 2 * ms / 1000.0 + (0.3 if kind == "completion" else 0.0)

    def active_delay_ms(self):
        return max([c["delay_ms"] for c in self.chaos.values()
                    if self.chaos_alive(c) and self.clock() >= c["injected_at"]] or [0])

    def chaos_alive(self, c):
        return not (c["delete_at"] is not None and self.clock() >= c["delete_at"])

    def cal_alive(self):
        c = self.cal
        return c is not None and not (c["delete_at"] is not None and self.clock() >= c["delete_at"])

    # cluster
    def snapshot(self):
        for hook in self.hooks:
            hook(self)
        snap = copy.deepcopy(self.state)
        if self.cal_alive():
            c = self.cal
            snap["pods"][c["name"]] = pod_entry(
                c["uid"], {"app": "vllm-calibration"},
                ready=(not self.never_ready and self.clock() >= c["ready_at"] and not c["unready"]),
                restarts=c["restarts"], ip=c["ip"])
        snap["chaos"] = [n for n, c in self.chaos.items() if self.chaos_alive(c)]
        snap["events"] = list(self.extra_events)
        return snap

    def create_pod(self, manifest):
        self.calls.append(("create_pod", manifest["metadata"]["name"]))
        self.cal = {"name": manifest["metadata"]["name"], "uid": "u-cal", "ready_at": self.clock() + 120,
                    "restarts": 0, "ip": "10.0.9.9", "delete_at": None, "unready": False}

    def delete_pod(self, name):
        self.calls.append(("delete_pod", name))
        if self.delete_pod_works and self.cal:
            self.cal["delete_at"] = self.clock() + 5

    def pod_exists(self, name):
        return self.cal_alive()

    # chaos
    def chaos_create(self, cr, run_id, arm, pod, stage, duration=None):
        self.calls.append(("chaos_create", cr, pod, arm, duration))
        if self.chaos_create_error:
            raise self.chaos_create_error
        self.chaos[cr] = {"delay_ms": int(stage["latency"][:-2]), "injected_at": self.clock() + 2, "delete_at": None}

    def chaos_injected(self, cr):
        return self.injects and cr in self.chaos and self.clock() >= self.chaos[cr]["injected_at"]

    def chaos_delete(self, cr):
        self.calls.append(("chaos_delete", cr))
        if self.chaos_gone_works and cr in self.chaos and self.chaos[cr]["delete_at"] is None:
            self.chaos[cr]["delete_at"] = self.clock() + 3

    def chaos_exists(self, cr):
        return cr in self.chaos and self.chaos_alive(self.chaos[cr])

    # probe
    def start(self, params):
        if self.probe_start_error:
            raise self.probe_start_error
        return FakeHandle(self, params)


def build_run(base, world=None, candidate=10.0):
    clock = FakeClock()
    world = world or FakeWorld(clock)
    world.clock = clock
    pod = cal.build_calibration_pod(fake_rendered(base)[cal.ROLLOUT_KEY], RUN_ID, "sj-worker", None)
    cfg = cal.Config(RUN_ID, pod, candidate, "overlay 렌더값", PAYLOAD)
    chaos = cal.ChaosOps(create=world.chaos_create, delete=world.chaos_delete, exists=world.chaos_exists,
                         injected=world.chaos_injected)
    deps = cal.Deps(cluster=world, chaos=chaos, probe=world, clock=clock, sleep=clock.sleep,
                    now_iso=lambda: "2026-09-19T00:00:00+00:00")
    return world, deps, cfg


def assert_clean_after(world, result):
    """정리 후: CR·pod 모두 소멸, 운영 상태(Rollout·Service·운영 pod)는 시작 때와 동일."""
    assert result["cleanup"]["ok"], result["cleanup"]
    assert not any(world.chaos_exists(n) for n in world.chaos)
    assert not world.cal_alive()
    final = world.snapshot()
    assert final["rollout"] == make_snapshot()["rollout"] and final["services"] == make_snapshot()["services"]
    assert set(final["pods"]) == set(make_snapshot()["pods"]) and final["chaos"] == []


def test_a_normal_run_measures_all_windows_and_only_creates_pod_and_chaos(base):
    world, deps, cfg = build_run(base)
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert result["hard_fail"] is None
    names = [w["name"] for w in result["windows"]]
    assert names == ["baseline"] + [x for s in cal.STAGES for x in (s["name"], f"recovery-{s['name']}")]
    assert all(w["complete"] for w in result["windows"])
    worst = next(w for w in result["windows"] if w["name"] == WORST)
    assert worst["health"]["max"] == pytest.approx(8.02) and worst["health"]["ok"] >= 89
    assert worst["completion"]["success_rate"] == 1.0
    a = result["analysis"]
    assert (a["run_outcome"], a["recommendation"], a["T_min"]) == ("PASS", "RAISE", 11), "8.02s -> T_req 10.03 -> 11"
    assert_clean_after(world, result)
    mutation_kinds = {c[0] for c in world.calls}
    assert mutation_kinds == {"create_pod", "delete_pod", "chaos_create", "chaos_delete"}, \
        "pod 생성·삭제와 NetworkChaos 생성·삭제 외 어떤 변경도 없어야 함"
    creates = [c for c in world.calls if c[0] == "chaos_create"]
    assert [c[2] for c in creates] == [cfg.pod_manifest["metadata"]["name"]] * 4, "지연은 calibration pod에만"
    assert all(c[3] == "calibration" and c[4] == "240s" for c in creates), "자동 만료 안전망 90+30+60+60"


def test_restart_mid_stage_aborts_h1_keeps_partial_data_and_cleans_up(base):
    world, deps, cfg = build_run(base)
    seen = {"n": 0}

    def restart_mid_window_at_2000ms(w):
        if w.cal and w.active_delay_ms() == 2000:
            seen["n"] += 1
            if seen["n"] == 8:  # 창 시작 검사가 아니라 측정 도중(폴링 7번째쯤)에 재시작
                w.cal["restarts"] = 1
    world.hooks.append(restart_mid_window_at_2000ms)
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert result["hard_fail"]["code"] == "H1" and result["analysis"]["run_outcome"] == "FAIL"
    assert result["analysis"]["recommendation"] == "NONE"
    partial = [w for w in result["windows"] if not w["complete"]]
    assert [w["name"] for w in partial] == ["stage-3-2000ms"] and partial[0]["health"]["n"] > 0, "부분 데이터 보존"
    assert_clean_after(world, result)
    assert {c[1] for c in world.calls if c[0] == "chaos_delete"} == {c[1] for c in world.calls if c[0] == "chaos_create"}


def test_pod_never_ready_times_out_h7_without_creating_chaos(base):
    world, deps, cfg = build_run(base)
    world.never_ready = True
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert result["hard_fail"]["code"] == "H7" and result["windows"] == []
    assert not any(c[0] == "chaos_create" for c in world.calls)
    assert_clean_after(world, result)


def test_allinjected_timeout_aborts_h6_and_deletes_the_cr(base):
    world, deps, cfg = build_run(base)
    world.injects = False
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert result["hard_fail"]["code"] == "H6"
    assert [c[0] for c in world.calls].count("chaos_create") == 1
    assert_clean_after(world, result)


def test_exception_while_creating_chaos_still_deletes_pod_and_attempts_cr_cleanup(base):
    world, deps, cfg = build_run(base)
    world.chaos_create_error = RuntimeError("webhook 거부")
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert result["hard_fail"]["code"] == "H9" and "webhook" in result["hard_fail"]["detail"]
    assert any(c[0] == "chaos_delete" for c in world.calls), "생성이 예외여도 그 CR의 삭제를 시도(idempotent)"
    assert_clean_after(world, result)


def test_keyboard_interrupt_cleans_up_and_reraises_with_the_result(base):
    world, deps, cfg = build_run(base)
    calls = {"n": 0}
    real_start = world.start

    def start(params):
        calls["n"] += 1
        if calls["n"] == 4:  # stage-2 측정 창
            raise KeyboardInterrupt()
        return real_start(params)
    world.start = start
    with pytest.raises(KeyboardInterrupt) as exc:
        cal.run_calibration(deps, cfg, log=lambda *_: None)
    result = exc.value.calibration_result
    assert result["hard_fail"]["code"] == "H9" and "KeyboardInterrupt" in result["hard_fail"]["detail"]
    assert_clean_after(world, result)


def test_cleanup_failure_is_reported_as_h8_and_the_run_is_fail(base):
    world, deps, cfg = build_run(base)
    world.delete_pod_works = False
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert result["cleanup"]["ok"] is False and result["cleanup"]["pod_deleted"] is False
    assert result["hard_fail"]["code"] == "H8" and result["analysis"]["run_outcome"] == "FAIL"
    assert any("calibration pod" in p for p in result["cleanup"]["problems"])


def test_cr_that_never_disappears_is_h8_mid_run(base):
    world, deps, cfg = build_run(base)
    world.chaos_gone_works = False
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert result["hard_fail"]["code"] == "H8" and result["cleanup"]["ok"] is False


def test_external_production_change_aborts_h5_and_the_tool_never_touches_the_rollout(base):
    world, deps, cfg = build_run(base)

    def promote_someone_else(w):
        if w.active_delay_ms() == 1000:
            w.state["rollout"]["current_hash"] = "changed"
    world.hooks.append(promote_someone_else)
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert result["hard_fail"]["code"] == "H5"
    assert "also_cleanup_failed" in result["hard_fail"], "외부 변경이 남아 사후 스냅샷 불일치로도 드러남"
    assert {c[0] for c in world.calls} <= {"create_pod", "delete_pod", "chaos_create", "chaos_delete"}
    assert not world.cal_alive() and not any(world.chaos_exists(n) for n in world.chaos)


@pytest.mark.parametrize("mutate,code", [
    (lambda w: w.state["nodes"]["sj-worker"].__setitem__("bad", ["DiskPressure"]), "H3"),
    (lambda w: w.state["pods"]["vllm-serving-659795b9df-xzmkr"].__setitem__("restarts", 1), "H4"),
    (lambda w: w.chaos.setdefault("foreign", {"delay_ms": 1, "injected_at": 0, "delete_at": None}), "H9"),
    (lambda w: w.state.__setitem__("context", {"run_id": "x"}), "H9"),
])
def test_other_hard_fail_conditions_abort_and_clean_up(base, mutate, code):
    world, deps, cfg = build_run(base)
    world.hooks.append(lambda w: mutate(w) if w.active_delay_ms() == 500 else None)
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert result["hard_fail"]["code"] == code
    assert not world.cal_alive()


def test_probe_client_failure_is_h9(base):
    world, deps, cfg = build_run(base)
    world.probe_error = "rc=255 summary=False stderr=ssh: connect refused"
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert result["hard_fail"]["code"] == "H9" and "ssh" in result["hard_fail"]["detail"]
    assert_clean_after(world, result)


def test_kubelet_probe_failure_events_without_restart_make_the_run_marginal(base):
    world, deps, cfg = build_run(base)

    def add_event(w):
        if w.active_delay_ms() == 4000 and not w.extra_events:
            w.extra_events.append({"pod": w.cal["name"], "reason": "Unhealthy", "count": 2,
                                   "message": "Readiness probe failed: context deadline exceeded"})
    world.hooks.append(add_event)
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    a = result["analysis"]
    assert result["hard_fail"] is None and a["run_outcome"] == "MARGINAL" and a["kubelet_probe_failures"] == 2
    assert a["recommendation"] == "NONE" and "불일치" in a["reason"], "L_max 8.02 < 후보 10인데 kubelet이 실패 - 불일치"
    assert_clean_after(world, result)


def test_probe_failures_between_windows_are_attributed_to_the_next_window(base):
    """CR 생성~AllInjected 대기 중(창 사이 공백)에 생긴 실패가 어느 창에도 안 잡혀 과소 집계되면 안 된다."""
    world, deps, cfg = build_run(base)

    def fail_while_waiting_for_allinjected(w):
        if w.chaos and w.active_delay_ms() == 0 and not w.extra_events and w.cal:
            w.extra_events.append({"pod": w.cal["name"], "reason": "Unhealthy", "count": 1,
                                   "message": "Liveness probe failed: Get: context deadline exceeded"})
    world.hooks.append(fail_while_waiting_for_allinjected)
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    per_window = {w["name"]: w["kubelet"]["probe_failures"] for w in result["windows"]}
    assert per_window["stage-1-500ms"] == 1, "공백의 실패는 다음 창(stage-1)에 귀속"
    assert sum(per_window.values()) == 1 and result["analysis"]["run_outcome"] == "MARGINAL"


def test_ready_loss_after_first_ready_is_h2(base):
    world, deps, cfg = build_run(base)
    world.hooks.append(lambda w: w.cal.__setitem__("unready", True) if w.cal and w.active_delay_ms() == 2000 else None)
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert result["hard_fail"]["code"] == "H2"
    assert_clean_after(world, result)


def test_preflight_failure_creates_nothing(base):
    world, deps, cfg = build_run(base)
    world.state["rollout"]["preview_selector"] = "preview-exists"
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert result["hard_fail"]["code"] == "PREFLIGHT" and world.calls == []


def test_hard_time_limit_aborts_h9_and_cleans_up(base, monkeypatch):
    world, deps, cfg = build_run(base)
    monkeypatch.setattr(cal, "HARD_LIMIT_SEC", 100.0)
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert result["hard_fail"]["code"] == "H9" and "상한" in result["hard_fail"]["detail"]
    assert_clean_after(world, result)


# ---- H. CLI --------------------------------------------------------------------------------------
def boom(_args):
    raise AssertionError("이 모드는 클러스터에 접근하면 안 됨")


@needs_kubectl
def test_dry_run_validates_offline_and_touches_no_cluster(capsys):
    assert cal.main(["--dry-run"], deps_factory=boom) == 0
    out = capsys.readouterr().out
    assert "DRY-RUN OK" in out and "readinessProbe/timeoutSeconds" in out and "livenessProbe/timeoutSeconds" in out
    assert "Rollout·Service·운영 pod·recovery-policy 변경 없음" in out


def test_a_mode_flag_is_required():
    with pytest.raises(SystemExit) as exc:
        cal.main([])
    assert exc.value.code == 2


class _CliWorld(FakeWorld):
    def rollout_template(self):
        template = copy.deepcopy(cal.load_base()[cal.ROLLOUT_KEY]["spec"]["template"])
        template["metadata"]["annotations"] = {"experiment-prep-ts": "x"}
        return template


def _cli_deps(world):
    chaos = cal.ChaosOps(create=world.chaos_create, delete=world.chaos_delete, exists=world.chaos_exists,
                         injected=world.chaos_injected)
    clock = world.clock
    return lambda _args: cal.Deps(cluster=world, chaos=chaos, probe=world, clock=clock, sleep=clock.sleep,
                                  now_iso=lambda: "2026-09-19T00:00:00+00:00")


@needs_kubectl
def test_execute_refuses_to_start_when_preflight_fails(capsys, tmp_path):
    world = _CliWorld(FakeClock())
    world.state["chaos"] = ["leftover"]
    world.chaos["leftover"] = {"delay_ms": 1, "injected_at": 0, "delete_at": None}
    assert cal.main(["--execute", "--output-dir", str(tmp_path)], deps_factory=_cli_deps(world)) == 1
    assert world.calls == [] and list(tmp_path.iterdir()) == []
    assert "PREFLIGHT 실패" in capsys.readouterr().out


@needs_kubectl
def test_execute_writes_the_pilot_result_file_and_returns_by_outcome(tmp_path, capsys):
    world = _CliWorld(FakeClock())
    assert cal.main(["--execute", "--output-dir", str(tmp_path)], deps_factory=_cli_deps(world)) == 0
    (path,) = list(tmp_path.glob("calibration-network-tolerant-*.json"))
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["analysis"]["run_outcome"] == "PASS" and saved["cleanup"]["ok"] and saved["candidate_sec"] == 10
    assert saved["preregistration"] == "§42" and len(saved["windows"]) == 9

    world = _CliWorld(FakeClock())
    world.hooks.append(lambda w: w.cal.__setitem__("restarts", 1) if w.cal and w.active_delay_ms() == 4000 else None)
    assert cal.main(["--execute", "--output-dir", str(tmp_path)], deps_factory=_cli_deps(world)) == 2
    world = _CliWorld(FakeClock())
    world.delete_pod_works = False
    assert cal.main(["--execute", "--output-dir", str(tmp_path)], deps_factory=_cli_deps(world)) == 3


# ---- I. 어댑터 duration·결과 파일 구조적 제외 ----------------------------------------------------
def test_create_network_chaos_sets_spec_duration_only_when_requested():
    api = MagicMock()
    stage = {"latency": "1000ms", "jitter": "100ms"}
    with patch("network_degrade_adapter.load_kube_config"), \
            patch("network_degrade_adapter.client.CustomObjectsApi", return_value=api):
        nda.create_network_chaos("cr", "run", "calibration", "pod-1", stage)
        default_body = api.create_namespaced_custom_object.call_args.args[-1]
        nda.create_network_chaos("cr", "run", "calibration", "pod-1", stage, "160s")
        timed_body = api.create_namespaced_custom_object.call_args.args[-1]
    assert "duration" not in default_body["spec"], "기본값은 기존 trial 동작(명시적 삭제 전까지 유지) 그대로"
    assert timed_body["spec"]["duration"] == "160s"
    without_duration = copy.deepcopy(timed_body)
    del without_duration["spec"]["duration"]
    assert without_duration == default_body, "duration 외에는 본문이 동일"


def test_calibration_result_files_are_outside_the_trial_glob_used_by_collect_metrics(tmp_path):
    import collect_metrics
    (tmp_path / "pilot").mkdir()
    (tmp_path / "pilot" / f"calibration-network-tolerant-{RUN_ID}.json").write_text("{}", encoding="utf-8")
    assert collect_metrics.load_all_results(tmp_path) == [], "calibration은 pilot이며 본 분석·집계에서 구조적으로 제외"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
