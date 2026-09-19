#!/usr/bin/env python3
"""calibrate_network_tolerant_probe.py 오프라인 검증(2026-09-19, 사전 등록 §42 절차·정리 + §44 v2 판정).

- overlay 렌더 diff가 readiness/liveness timeoutSeconds 2경로만 허용하고 나머지(CPU·모델·startupProbe·이미지·
  서비스 selector 등)의 변경은 거부하는지
- calibration pod가 base template과 (nodeSelector·두 timeout 외) 같고 어떤 selector에도 안 걸리는지
- §44 v2: kubelet probe 실패 이벤트의 실제 timestamp 구간 분류(steady 경계 +-1초 보수 분류·오프셋 보정), 회차별 PASS 조건
  판정(FAIL/INVALID), 즉시 중단 H1~H12, Prometheus 카운터 교차검증
- 예외·중단(KeyboardInterrupt)·timeout·정리 실패·운영 상태 변경에서도 NetworkChaos와 pod가 정리되고, 도구가 pod·CR
  외에는 아무 것도 변경하지 않는지(가짜 클러스터로 - 실제 클러스터 접근 없음, conftest의 cluster_guard가 강제)
"""
import copy
import json
import math
import shutil
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
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
              node="sj-worker", ip="10.0.0.5", ready_since="2026-09-19T00:00:00Z", terminated_reason=None,
              evicted=False):
    return {"uid": uid, "labels": labels, "phase": phase, "ready": ready, "ready_since": ready_since,
            "restarts": restarts, "terminated": terminated, "terminated_reason": terminated_reason,
            "evicted": evicted, "deleting": deleting, "node": node, "ip": ip}


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
    assert all(new == 11 for _, _, new in changes), "calibration 독립 2회 PASS로 확정된 값(§45) - 임의로 바꾸지 말 것"


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


def window(name, ok=90, errors=0, l_max=8.0, complete=True, comp_n=30, comp_ok=None):
    return {"name": name, "complete": complete,
            "health": {"n": ok + errors, "ok": ok, "errors": errors, "max": l_max},
            "completion": {"n": comp_n, "ok": comp_n if comp_ok is None else comp_ok}}


WORST = cal.STAGES[-1]["name"]
WINDOW_NAMES = ["baseline"] + [n for s in cal.STAGES for n in (s["name"], f"recovery-{s['name']}")]


@pytest.mark.parametrize("l_max,t_min", [(7.0, 9), (8.0, 10), (8.8, 11), (8.85, 12), (11.0, 14), (12.0, 15), (12.5, 16)])
def test_stage4_t_min_follows_the_preregistered_margin_formula(l_max, t_min):
    tm = cal.stage4_t_min([window(WORST, l_max=l_max)], 11.0)
    assert tm["usable"] is True and tm["T_min"] == t_min and tm["T_cap"] == 15.0


def test_stage4_t_min_is_unusable_without_a_trustworthy_stage4_window():
    assert cal.stage4_t_min([], 11.0)["usable"] is False
    assert cal.stage4_t_min([window(WORST, complete=False)], 11.0)["T_min"] is None
    assert cal.stage4_t_min([window(WORST, ok=29)], 11.0)["usable"] is False
    assert cal.stage4_t_min([window(WORST, ok=30)], 11.0)["usable"] is True, "경계(30개)는 허용"
    tm = cal.stage4_t_min([window("stage-1-500ms", errors=1), window(WORST, l_max=8.0)], 11.0)
    assert tm["usable"] is False and tm["T_min"] == 10 and "1건" in tm["note"], \
        "stage 창에 실패한 probe 동등 요청이 있으면 L_max를 믿지 않는다(fail-closed)"


def test_required_timeout_is_the_larger_of_relative_and_absolute_margin():
    assert cal.required_timeout(8.0) == pytest.approx(10.0)
    assert cal.required_timeout(2.0) == pytest.approx(3.5), "작은 값에서는 절대 마진 1.5초가 지배"
    assert cal.required_timeout(20.0) == pytest.approx(25.0)


def ev(kind, segment, t=0.0, ambiguous=False):
    """분류가 끝난 probe 실패 발생 하나."""
    return {"kind": kind, "segment": segment, "t_pc": t, "t_pc_iso": f"t={t}", "ambiguous": ambiguous,
            "message": f"{kind} probe failed", "worker_ts": None, "polled_at": t, "approx": False}


def clean_result(candidate=11.0):
    windows = [window(n, l_max=8.02 if n == WORST else 4.0) for n in WINDOW_NAMES]
    return {"candidate_sec": candidate, "windows": windows,
            "timeline": {"stages": [{"allinjected": "x"} for _ in cal.STAGES]}, "probe_events": [],
            "hard_fail": None, "cleanup": {"ok": True, "problems": []},
            "crosscheck": {"ok": True, "problems": [], "detail": {"Readiness": {"events_before": 1, "counter_failed": 1,
                                                                                 "events_after": 1, "successful_series": True}}},
            "clock_offset": {"used_sec": 0.285}}


def test_a_clean_run_passes_every_condition():
    a = cal.judge_v2(clean_result())
    assert a["run_outcome"] == "PASS" and a["failed_conditions"] == [] and a["reasons"] == []
    assert (a["L_max"], a["T_min"]) == (8.02, 11)
    assert a["conditions"]["V3_probe_counter_crosscheck"]["detail"]["Readiness"]["counter_failed"] == 1,         "교차검증이 통과했을 때도 실제 값을 detail에 남긴다(통과했는데 '미수행'으로 표시되던 문구 버그 - 판정에는 영향 없었음)"
    result = clean_result()
    result["crosscheck"] = None
    assert cal.judge_v2(result)["conditions"]["V3_probe_counter_crosscheck"]["detail"] == "교차검증 미수행"


def test_the_same_data_fails_a_lower_candidate():
    a = cal.judge_v2(clean_result(candidate=10.0))
    assert a["run_outcome"] == "FAIL" and a["failed_conditions"] == ["C7_t_min_le_candidate"]


@pytest.mark.parametrize("mutate,outcome,failed", [
    (lambda r: r["timeline"]["stages"].pop(), "FAIL", "C1_all_stages_allinjected"),
    (lambda r: r["windows"][3]["completion"].update(ok=29), "FAIL", "C2_completion_success_100pct"),
    (lambda r: r["windows"][3]["completion"].update(n=0, ok=0), "FAIL", "C2_completion_success_100pct"),
    (lambda r: r["probe_events"].append(ev("Readiness", "steady_2", 5.0)), "FAIL", "C3_no_steady_probe_failure"),
    (lambda r: r["probe_events"].append(ev("Liveness", "steady_1", 5.0)), "FAIL", "C3_no_steady_probe_failure"),
    (lambda r: r["probe_events"].append(ev("Liveness", "baseline", 5.0)), "FAIL", "C4_no_liveness_failure"),
    (lambda r: r["probe_events"].append(ev("Liveness", "teardown_3", 5.0)), "FAIL", "C4_no_liveness_failure"),
    (lambda r: r.update(hard_fail={"code": "H2", "detail": "x"}), "FAIL", "C5_no_ready_restart_uid_oom_evict_node"),
    (lambda r: r.update(hard_fail={"code": "H1", "detail": "x"}), "FAIL", "C5_no_ready_restart_uid_oom_evict_node"),
    (lambda r: r.update(hard_fail={"code": "H3", "detail": "x"}), "FAIL", "C5_no_ready_restart_uid_oom_evict_node"),
    (lambda r: r["probe_events"].extend([ev("Readiness", "teardown_1", 5.0), ev("Readiness", "teardown_1", 6.0)]),
     "FAIL", "C6_teardown_readiness_not_consecutive"),
    (lambda r: r["windows"][-2]["health"].update(max=8.85), "FAIL", "C7_t_min_le_candidate"),
    (lambda r: r["cleanup"].update(ok=False, problems=["pod 잔존"]), "FAIL", "C8_cleanup_ok"),
    (lambda r: r.update(hard_fail={"code": "H9", "detail": "x"}), "FAIL", "C9_no_hard_fail"),
    (lambda r: r.update(crosscheck={"ok": False, "problems": ["불일치"]}), "INVALID", "V3_probe_counter_crosscheck"),
    (lambda r: r.update(crosscheck=None), "INVALID", "V3_probe_counter_crosscheck"),
    (lambda r: r.update(clock_offset=None), "INVALID", "V1_clock_offset"),
    (lambda r: r["windows"].pop(), "INVALID", "V2_all_windows_complete"),
    (lambda r: r["windows"][4].update(complete=False), "INVALID", "V2_all_windows_complete"),
    (lambda r: r["windows"][-2]["health"].update(ok=29), "INVALID", "V4_l_max_usable"),
    (lambda r: r["windows"][1]["health"].update(errors=1), "INVALID", "V4_l_max_usable"),
])
def test_each_condition_alone_flips_the_verdict(mutate, outcome, failed):
    result = clean_result()
    mutate(result)
    a = cal.judge_v2(result)
    assert a["run_outcome"] == outcome and failed in a["failed_conditions"], a["reasons"]


def test_a_single_teardown_readiness_failure_is_recorded_but_allowed():
    result = clean_result()
    result["probe_events"].append(ev("Readiness", "teardown_1", 5.0))
    a = cal.judge_v2(result)
    assert a["run_outcome"] == "PASS" and a["probe_event_counts"] == {"teardown_1/Readiness": 1}
    assert "teardown_1" in a["conditions"]["C6_teardown_readiness_not_consecutive"]["detail"]


def test_readiness_failures_outside_steady_and_teardown_are_recorded_but_do_not_fail_the_run():
    """§44.2 조건 3은 steady, 조건 6은 teardown만 다룬다 - 그 밖 구간(기동·baseline·ramp·공백)의 readiness 실패는 기록만 한다."""
    result = clean_result()
    result["probe_events"] += [ev("Readiness", "baseline", 5.0), ev("Readiness", "injection_ramp_2", 50.0),
                               ev("Readiness", "between_stage_1", 90.0), ev("Startup", "startup", 1.0)]
    a = cal.judge_v2(result)
    assert a["run_outcome"] == "PASS" and a["probe_event_counts"]["baseline/Readiness"] == 1


def test_shutdown_artifacts_are_recorded_but_excluded_from_the_verdict():
    result = clean_result()
    result["probe_events"] += [ev("Liveness", "shutdown", 900.0), ev("Readiness", "shutdown", 901.0)]
    a = cal.judge_v2(result)
    assert a["run_outcome"] == "PASS" and a["probe_event_counts"]["shutdown/Liveness"] == 1


def test_a_condition_violation_outranks_an_invalid_measurement():
    result = clean_result()
    result["crosscheck"] = None
    result["probe_events"].append(ev("Readiness", "steady_1", 1.0))
    assert cal.judge_v2(result)["run_outcome"] == "FAIL"


def test_a_preflight_only_result_can_be_judged_without_data():
    a = cal.judge_v2({"candidate_sec": 11.0, "hard_fail": {"code": "PREFLIGHT", "detail": "x"}})
    assert a["run_outcome"] == "FAIL" and "C9_no_hard_fail" in a["failed_conditions"]


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
def _event(pod, message, count=1, uid=None, first=None, last=None, reason="Unhealthy"):
    return {"pod": pod, "reason": reason, "message": message, "count": count, "uid": uid or message,
            "first": first, "last": last, "type": "Warning"}


def test_parse_snapshot_extracts_pod_conditions_and_event_timestamps():
    ns_items = [
        {"kind": "Pod", "metadata": {"name": "p1", "uid": "u1", "labels": {"app": "vllm-serving"}},
         "spec": {"nodeName": "sj-worker"},
         "status": {"phase": "Running", "podIP": "10.0.0.5",
                    "conditions": [{"type": "Ready", "status": "True", "lastTransitionTime": "2026-09-19T10:00:00Z"}],
                    "containerStatuses": [{"restartCount": 2, "lastState": {
                        "terminated": {"exitCode": 137, "reason": "OOMKilled"}}}]}},
        {"kind": "Pod", "metadata": {"name": "p2", "uid": "u2", "labels": {}}, "spec": {},
         "status": {"phase": "Failed", "reason": "Evicted", "conditions": []}},
        {"kind": "Service", "metadata": {"name": "vllm-active"}, "spec": {"selector": {"app": "vllm-serving"}}},
        {"kind": "Rollout", "metadata": {"name": "vllm-serving", "generation": 30},
         "status": {"phase": "Healthy", "currentPodHash": "h", "stableRS": "h",
                    "blueGreen": {"activeSelector": "h", "previewSelector": "h"}}},
        {"kind": "NetworkChaos", "metadata": {"name": "cr-1"}},
        {"kind": "Event", "metadata": {"uid": "ev-1"}, "involvedObject": {"kind": "Pod", "name": "p1"},
         "reason": "Unhealthy", "count": 3, "type": "Warning", "firstTimestamp": "2026-09-19T10:00:05Z",
         "lastTimestamp": "2026-09-19T10:00:15Z",
         "message": 'Readiness probe failed: Get "http://x": context deadline exceeded'},
        {"kind": "Event", "metadata": {"uid": "ev-2"}, "involvedObject": {"kind": "Service", "name": "s"},
         "reason": "Unhealthy", "count": 9, "message": "x"},
    ]
    node_items = [{"metadata": {"name": "n1"}, "status": {"conditions": [
        {"type": "Ready", "status": "True"}, {"type": "MemoryPressure", "status": "True"}]}}]
    snap = cal.parse_snapshot(ns_items, node_items, None)
    assert snap["pods"]["p1"] == pod_entry("u1", {"app": "vllm-serving"}, ready=True, restarts=2, terminated=True,
                                           ip="10.0.0.5", ready_since="2026-09-19T10:00:00Z",
                                           terminated_reason="OOMKilled")
    assert snap["pods"]["p2"]["evicted"] is True and snap["pods"]["p2"]["ready"] is False
    assert snap["rollout"]["current_hash"] == "h" and snap["chaos"] == ["cr-1"]
    assert snap["nodes"]["n1"] == {"ready": True, "bad": ["MemoryPressure"]}
    assert snap["events"] == [{"pod": "p1", "reason": "Unhealthy", "count": 3, "uid": "ev-1", "type": "Warning",
                               "first": "2026-09-19T10:00:05Z", "last": "2026-09-19T10:00:15Z",
                               "message": 'Readiness probe failed: Get "http://x": context deadline exceeded'}]


def test_probe_kind_recognises_only_the_three_probe_failure_messages():
    assert cal.probe_kind('Readiness probe failed: Get "http://x": timeout') == "Readiness"
    assert cal.probe_kind("Liveness probe failed: x") == "Liveness"
    assert cal.probe_kind("Startup probe failed: 서버 연결 실패") == "Startup"
    assert cal.probe_kind("Readiness probe warning: x") is None and cal.probe_kind("Killing container") is None


def test_event_tracker_turns_count_increments_into_occurrences():
    tracker = cal.EventTracker("p1")
    msg = 'Readiness probe failed: Get "http://x": context deadline exceeded'
    snap = {"events": [_event("p1", msg, 1, last="2026-09-19T10:00:05Z"),
                       _event("p1", "Startup probe failed: x", 10, last="2026-09-19T09:50:00Z"),
                       _event("other", msg, 9, last="2026-09-19T10:00:05Z"),
                       _event("p1", "Killing container", 4, reason="Killing"),
                       _event("p1", "Back-off restarting failed container", 4)]}
    new = tracker.update(snap, 1000.0)
    assert sorted(o["kind"] for o in new) == ["Readiness"] + ["Startup"] * 10
    assert tracker.update(snap, 1003.0) == [], "count가 그대로면 새 발생이 없다"
    snap["events"][0].update(count=3, last="2026-09-19T10:00:15Z")
    new = tracker.update(snap, 1010.0)
    assert len(new) == 2 and all(o["approx"] and o["worker_ts"] == "2026-09-19T10:00:15Z" for o in new), \
        "한 폴링에서 2건 늘면 같은 lastTimestamp + approx"
    assert tracker.totals() == {"Readiness": 3, "Liveness": 0, "Startup": 10}


def test_event_tracker_counts_a_replaced_event_object_from_zero():
    tracker = cal.EventTracker("p1")
    msg = "Liveness probe failed: x"
    tracker.update({"events": [_event("p1", msg, 2, uid="a", last="2026-09-19T10:00:05Z")]}, 1.0)
    new = tracker.update({"events": [_event("p1", msg, 1, uid="b", last="2026-09-19T10:00:25Z")]}, 2.0)
    assert len(new) == 1 and tracker.totals()["Liveness"] == 3


def iso_z(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_tl():
    """가짜 구간 경계(epoch 초) - stage i(0부터): create=300+150i, allinjected=+2, delete_request=+100, gone=+103, 끝=+118."""
    stages = []
    for i in range(4):
        base = 300.0 + 150.0 * i
        stages.append({"create": base, "allinjected": base + 2, "delete_request": base + 100,
                       "gone": base + 103, "teardown_end": base + 118})
    return {"pod_created": 100.0, "ready": 200.0, "stages": stages, "pod_delete_request": 950.0}


def occ_at(t, kind="Readiness", offset=0.0):
    """분류 결과가 정확히 t(k+0.5 꼴)가 되도록 worker 타임스탬프를 만든 발생."""
    return {"kind": kind, "message": f"{kind} probe failed", "polled_at": t + 3.0, "approx": False,
            "worker_ts": iso_z(math.floor(t + offset))}


@pytest.mark.parametrize("t,segment,ambiguous", [
    (50.5, "pre_start", False), (150.5, "startup", False), (250.5, "baseline", False),
    (300.5, "injection_ramp_1", False), (301.5, "steady_1", True), (303.5, "steady_1", False),
    (398.5, "steady_1", False), (399.5, "steady_1", True), (400.5, "steady_1", True),
    (401.5, "teardown_1", False), (417.5, "teardown_1", False), (418.5, "between_stage_1", False),
    (449.5, "between_stage_1", False), (450.5, "injection_ramp_2", False),
    (868.5, "post_teardown", False), (949.5, "post_teardown", False), (950.5, "shutdown", False),
])
def test_events_are_classified_by_their_actual_timestamp_with_a_conservative_steady_boundary(t, segment, ambiguous):
    c = cal.classify_occurrence(occ_at(t), make_tl(), offset_sec=0.0)
    assert (c["segment"], c["ambiguous"]) == (segment, ambiguous) and c["t_pc"] == pytest.approx(t)


def test_classification_shifts_worker_time_by_the_offset_and_the_half_second_midpoint():
    o = {"kind": "Readiness", "message": "m", "worker_ts": iso_z(402), "polled_at": 500.0, "approx": False}
    c = cal.classify_occurrence(o, make_tl(), offset_sec=0.285)
    assert c["t_pc"] == pytest.approx(402 - 0.285 + 0.5) and c["segment"] == "teardown_1"
    unknown = {"kind": "Readiness", "message": "m", "worker_ts": None, "polled_at": 350.5, "approx": False}
    assert cal.classify_occurrence(unknown, make_tl(), 0.285)["t_pc"] == 350.5, "타임스탬프가 없으면 폴링 시각"


def test_classification_follows_the_recorded_boundaries_not_the_nominal_stage_length():
    """명목 stage 시간(90초)으로 추정하지 않는다 - 같은 이벤트도 기록된 CR 삭제 요청 시각에 따라 구간이 달라진다."""
    late = make_tl()   # steady가 98초 지속: 삭제 요청 400
    early = make_tl()
    early["stages"][0]["delete_request"] = 380.0   # steady가 78초만 지속
    o = occ_at(392.5)
    assert cal.classify_occurrence(o, late, 0.0)["segment"] == "steady_1"
    assert cal.classify_occurrence(o, early, 0.0)["segment"] == "teardown_1"


def test_classification_before_the_boundaries_are_known_treats_the_open_window_as_steady():
    tl = {"pod_created": 100.0, "ready": 200.0, "stages": [{"create": 300.0, "allinjected": 302.0}]}
    assert cal.classify_occurrence(occ_at(350.5), tl, 0.0)["segment"] == "steady_1"
    assert cal.classify_all([occ_at(350.5), occ_at(310.5)], tl, 0.0)[1]["segment"] == "steady_1"


def test_findings_steady_and_liveness_stop_immediately_but_startup_and_shutdown_do_not():
    def codes(*events):
        return sorted({c for c, _ in cal.probe_event_findings(list(events))})
    assert codes(ev("Readiness", "steady_1", 10.0)) == ["H10"]
    assert codes(ev("Liveness", "steady_1", 10.0)) == ["H10", "H11"]
    assert codes(ev("Liveness", "between_stage_1", 10.0)) == ["H11"]
    assert codes(ev("Liveness", "startup", 10.0)) == ["H11"], "liveness는 전체 실행(기동 포함)에서 0건"
    assert codes(ev("Startup", "steady_1", 10.0), ev("Startup", "startup", 5.0)) == []
    assert codes(ev("Liveness", "shutdown", 10.0), ev("Readiness", "shutdown", 11.0)) == []
    assert codes(ev("Readiness", "baseline", 10.0), ev("Readiness", "injection_ramp_1", 12.0)) == [], \
        "steady·teardown 밖의 readiness 실패는 기록만"


def test_findings_a_single_teardown_readiness_failure_is_fine_but_consecutive_ones_are_h12():
    def codes(*events):
        return {c for c, _ in cal.probe_event_findings(list(events))}
    assert codes(ev("Readiness", "teardown_1", 405.5)) == set()
    assert codes(ev("Readiness", "teardown_1", 405.5), ev("Readiness", "teardown_2", 555.5)) == set(), \
        "서로 다른 전이 구간에 각 1건 - 비연속"
    assert codes(ev("Readiness", "teardown_1", 405.5), ev("Readiness", "teardown_1", 430.0)) == {"H12"}, \
        "같은 전이 구간에 2건"
    assert codes(ev("Readiness", "teardown_1", 417.0), ev("Readiness", "between_stage_1", 425.0)) == {"H12"}, \
        "teardown 실패와 15초 이내의 다른 readiness 실패"
    assert codes(ev("Readiness", "teardown_1", 405.0), ev("Readiness", "between_stage_1", 425.0)) == set(), \
        "20초 간격은 연속이 아님"


def _metrics(**failed):
    counters = {"Readiness/successful": 116.0, "Liveness/successful": 20.0}
    counters.update({k.replace("_", "/"): v for k, v in failed.items()})
    return {"counters": counters}


def _totals(readiness=0, liveness=0):
    return {"Readiness": readiness, "Liveness": liveness, "Startup": 10}


def test_crosscheck_requires_the_events_to_bracket_the_kubelet_counter():
    assert cal.crosscheck_probe_counters(_totals(1), _totals(1), _metrics(Readiness_failed=1.0))["ok"]
    assert cal.crosscheck_probe_counters(_totals(), _totals(), _metrics())["ok"], "failed series가 없으면 0"
    assert cal.crosscheck_probe_counters(_totals(0), _totals(1), _metrics(Readiness_failed=1.0))["ok"], \
        "E1 <= C <= E2 - 이벤트가 늦게 도착해도 허용"
    lost = cal.crosscheck_probe_counters(_totals(1), _totals(1), _metrics(Readiness_failed=3.0))
    assert not lost["ok"] and "유실" in lost["problems"][0], "카운터가 이벤트보다 많으면 이벤트 유실(스팸 필터)"
    assert not cal.crosscheck_probe_counters(_totals(2), _totals(2), _metrics(Readiness_failed=1.0))["ok"]
    assert not cal.crosscheck_probe_counters(_totals(0, 1), _totals(0, 1), _metrics())["ok"], "Liveness도 같은 규칙"
    missing = cal.crosscheck_probe_counters(_totals(), _totals(), {"counters": {}})
    assert not missing["ok"] and "successful series 없음" in missing["problems"][0]


def test_evaluate_violations_detects_an_instant_ready_transition_oom_and_eviction():
    baseline = make_snapshot()
    state = state_with_pod()
    assert codes(baseline, snapshot_with_cal(), state) == [] and state.ready_since == "2026-09-19T00:00:00Z"
    snap = snapshot_with_cal()
    snap["pods"]["cal-pod"]["ready_since"] = "2026-09-19T00:03:10Z"   # 폴링 사이에 NotReady->Ready를 거침
    found = cal.evaluate_violations(baseline, snap, state)
    assert [c for c, _ in found] == ["H2"] and "lastTransitionTime" in found[0][1]
    snap = snapshot_with_cal()
    snap["pods"]["cal-pod"].update(terminated_reason="OOMKilled", terminated=True)
    assert "OOMKilled" in cal.evaluate_violations(baseline, snap, state_with_pod())[0][1]
    snap = snapshot_with_cal()
    snap["pods"]["cal-pod"]["evicted"] = True
    assert "H1" in codes(baseline, snap, state_with_pod())


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
    assert public == {"snapshot", "rollout_template", "prober_metrics", "create_pod", "delete_pod", "pod_exists"}


def test_kubectl_cluster_prober_metrics_queries_prometheus_through_the_api_server_proxy():
    calls = []

    def run(cmd, capture_output, text, encoding, input=None):
        calls.append(cmd)
        if "prober_probe_total" in urllib.parse.unquote(cmd[-1]):
            result = [{"metric": {"probe_type": "Readiness", "result": "failed"}, "value": [1.0, "1"]},
                      {"metric": {"probe_type": "Readiness", "result": "successful"}, "value": [1.0, "116"]},
                      {"metric": {"probe_type": "Liveness", "result": "successful"}, "value": [1.0, "20"]}]
        else:
            result = [{"metric": {"probe_type": "Readiness", "result": "successful", "le": "10"},
                       "value": [1.0, "116"]}]
        return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "success", "data": {"result": result}}),
                               stderr="")
    metrics = cal.KubectlCluster(run=run, retries=0).prober_metrics("vllm-x")
    assert metrics["counters"] == {"Readiness/failed": 1.0, "Readiness/successful": 116.0, "Liveness/successful": 20.0}
    assert metrics["duration_buckets"] == [{"probe_type": "Readiness", "result": "successful", "le": "10",
                                            "value": 116.0}]
    path = calls[0][-1]
    assert calls[0][1:3] == ["get", "--raw"]
    assert path.startswith("/api/v1/namespaces/monitoring/services/kube-prom-kube-prometheus-prometheus:9090"
                           "/proxy/api/v1/query?query=")
    assert 'pod="vllm-x"' in urllib.parse.unquote(path) and 'namespace="vllm-serving"' in urllib.parse.unquote(path)


def test_kubectl_cluster_prober_metrics_rejects_a_failed_prometheus_status():
    def run(cmd, capture_output, text, encoding, input=None):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "error", "error": "x"}), stderr="")
    with pytest.raises(RuntimeError, match="Prometheus"):
        cal.KubectlCluster(run=run, retries=0).prober_metrics("vllm-x")


def test_ssh_clock_picks_the_lowest_rtt_sample_and_skips_failed_ones():
    times = iter([100.0, 101.0, 200.0, 200.2, 300.0, 300.1, 400.0, 400.1])
    outputs = iter([SimpleNamespace(returncode=0, stdout="100.9\n"), SimpleNamespace(returncode=0, stdout="200.35\n"),
                    SimpleNamespace(returncode=255, stdout=""), SimpleNamespace(returncode=0, stdout="garbage")])
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return next(outputs)
    clock = cal.SshClock(host="w", ssh="myssh", run=run, now=lambda: next(times), samples=4)
    best = clock.measure()
    assert best["offset_sec"] == pytest.approx(200.35 - 200.1) and best["rtt_sec"] == pytest.approx(0.2)
    assert seen[0][0] == "myssh" and "BatchMode=yes" in seen[0] and seen[0][-3:] == ["w", "date", "+%s.%N"][-3:]


def test_ssh_clock_returns_none_when_every_sample_fails():
    def run(cmd, **kw):
        raise OSError("ssh 없음")
    assert cal.SshClock(run=run, now=lambda: 0.0, samples=2).measure() is None


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
T0 = datetime(2026, 9, 19, 10, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, dt):
        self.t += dt

    def now(self):
        return T0 + timedelta(seconds=self.t)


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
    """cluster/chaos/probe/clock 의존성을 한 세계로 흉내낸다. calls에는 **변경 호출만** 남는다."""

    def __init__(self, clock):
        self.clock, self.state, self.calls, self.cal = clock, make_snapshot(), [], None
        self.chaos, self.hooks, self.extra_events, self.manifest = {}, [], [], None
        self.injects = self.delete_pod_works = self.chaos_gone_works = True
        self.never_ready, self.probe_error, self.chaos_create_error, self.probe_start_error = False, None, None, None
        self.metrics_override, self.metrics_error, self.offset_missing_calls, self.offset_calls = {}, None, set(), 0
        self.latency_fn = lambda kind, ms: 0.02 + 2 * ms / 1000.0 + (0.3 if kind == "completion" else 0.0)

    def active_delay_ms(self):
        return max([c["delay_ms"] for c in self.chaos.values()
                    if self.chaos_alive(c) and self.clock() >= c["injected_at"]] or [0])

    def chaos_alive(self, c):
        return not (c["delete_at"] is not None and self.clock() >= c["delete_at"])

    def cal_alive(self):
        c = self.cal
        return c is not None and not (c["delete_at"] is not None and self.clock() >= c["delete_at"])

    def now(self):
        return self.clock.now()

    def clock_offset(self):
        self.offset_calls += 1
        if self.offset_calls in self.offset_missing_calls:
            return None
        return {"offset_sec": 0.0, "rtt_sec": 0.02, "measured_at": "fake"}

    def probe_failure(self, kind, count=1, ago=0, error="context deadline exceeded"):
        """kubelet `Unhealthy` 이벤트를 지금(`ago`초 전) 시각(worker 시계 = PC 시계, 초 단위)으로 늘린다 - 같은 메시지는 count로
        합쳐진다. 실제로는 이벤트가 폴링에 잡히기 전에 발생하므로, 시간이 흐르지 않는 가짜 시계에서 발견 직후 정리로 넘어가는
        시나리오는 `ago`로 발생 시각을 앞당겨 표현한다."""
        text = f'{kind} probe failed: Get "http://10.0.9.9:8000/health": {error}'
        stamp = (self.now() - timedelta(seconds=ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for event in self.extra_events:
            if event["message"] == text:
                event["count"] += count
                event["last"] = stamp
                return
        self.extra_events.append({"pod": self.cal["name"], "reason": "Unhealthy", "count": count, "message": text,
                                  "uid": f"ev-{kind}", "first": stamp, "last": stamp, "type": "Warning"})

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
                restarts=c["restarts"], ip=c["ip"], ready_since=c["ready_since"])
        snap["chaos"] = [n for n, c in self.chaos.items() if self.chaos_alive(c)]
        snap["events"] = copy.deepcopy(self.extra_events)
        return snap

    def create_pod(self, manifest):
        self.calls.append(("create_pod", manifest["metadata"]["name"]))
        self.manifest = manifest
        self.cal = {"name": manifest["metadata"]["name"], "uid": "u-cal", "ready_at": self.clock() + 120,
                    "restarts": 0, "ip": "10.0.9.9", "delete_at": None, "unready": False,
                    "ready_since": "2026-09-19T10:02:00Z"}

    def delete_pod(self, name):
        self.calls.append(("delete_pod", name))
        if self.delete_pod_works and self.cal:
            self.cal["delete_at"] = self.clock() + 5

    def pod_exists(self, name):
        return self.cal_alive()

    def prober_metrics(self, pod_name):
        if self.metrics_error:
            raise self.metrics_error
        counters = {"Readiness/successful": 200.0, "Liveness/successful": 40.0}
        for event in self.extra_events:
            kind = cal.probe_kind(event["message"])
            if kind in ("Readiness", "Liveness"):
                counters[f"{kind}/failed"] = counters.get(f"{kind}/failed", 0.0) + event["count"]
        counters.update(self.metrics_override)
        return {"counters": counters, "duration_buckets": []}

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


def make_deps(world, clock):
    chaos = cal.ChaosOps(create=world.chaos_create, delete=world.chaos_delete, exists=world.chaos_exists,
                         injected=world.chaos_injected)
    return cal.Deps(cluster=world, chaos=chaos, probe=world, clock=clock, sleep=clock.sleep, now=clock.now,
                    clock_offset=world.clock_offset)


def build_run(base, world=None, candidate=11.0):
    clock = FakeClock()
    world = world or FakeWorld(clock)
    world.clock = clock
    pod = cal.build_calibration_pod(fake_rendered(base)[cal.ROLLOUT_KEY], RUN_ID, "sj-worker", None)
    cfg = cal.Config(RUN_ID, pod, candidate, "override(--candidate-timeout-sec)", PAYLOAD)
    return world, make_deps(world, clock), cfg


def assert_clean_after(world, result):
    """정리 후: CR·pod 모두 소멸, 운영 상태(Rollout·Service·운영 pod)는 시작 때와 동일."""
    assert result["cleanup"]["ok"], result["cleanup"]
    assert not any(world.chaos_exists(n) for n in world.chaos)
    assert not world.cal_alive()
    final = world.snapshot()
    assert final["rollout"] == make_snapshot()["rollout"] and final["services"] == make_snapshot()["services"]
    assert set(final["pods"]) == set(make_snapshot()["pods"]) and final["chaos"] == []


def counter_hook(fires_when, action, at=1):
    """조건을 만족하는 스냅샷이 `at`번째일 때 한 번만 action을 실행하는 hook."""
    seen = {"n": 0}

    def hook(w):
        if w.cal and fires_when(w):
            seen["n"] += 1
            if seen["n"] == at:
                action(w)
    return hook


def test_a_normal_run_at_candidate_11_passes_and_only_creates_pod_and_chaos(base):
    world, deps, cfg = build_run(base)
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert result["hard_fail"] is None and result["preregistration"] == "§44"
    names = [w["name"] for w in result["windows"]]
    assert names == WINDOW_NAMES and all(w["complete"] for w in result["windows"])
    worst = next(w for w in result["windows"] if w["name"] == WORST)
    assert worst["health"]["max"] == pytest.approx(8.02) and worst["health"]["ok"] >= 89
    assert worst["completion"]["success_rate"] == 1.0
    assert worst["samples"]["health"][0][:4] == [0, 0.0, pytest.approx(8.02), 200], "원본 표본 보존"
    a = result["analysis"]
    assert a["run_outcome"] == "PASS" and a["failed_conditions"] == [] and a["T_min"] == 11, a["reasons"]
    assert result["crosscheck"]["ok"] and result["prometheus"]["counters"]["Readiness/successful"] == 200.0
    assert result["clock_offset"]["used_sec"] == 0.0 and result["probe_events"] == []
    tl = result["timeline"]
    assert len(tl["stages"]) == 4 and all(set(s) == {"create", "allinjected", "delete_request", "gone", "teardown_end"}
                                          for s in tl["stages"])
    assert tl["stages"][0]["create"] < tl["stages"][0]["allinjected"] < tl["stages"][0]["delete_request"] \
        < tl["stages"][0]["gone"] < tl["stages"][0]["teardown_end"] < tl["stages"][1]["create"]
    assert tl["stages"][-1]["teardown_end"] < tl["pod_delete_request"] and tl["ready"] < tl["stages"][0]["create"]
    assert_clean_after(world, result)
    mutation_kinds = {c[0] for c in world.calls}
    assert mutation_kinds == {"create_pod", "delete_pod", "chaos_create", "chaos_delete"}, \
        "pod 생성·삭제와 NetworkChaos 생성·삭제 외 어떤 변경도 없어야 함"
    creates = [c for c in world.calls if c[0] == "chaos_create"]
    assert [c[2] for c in creates] == [cfg.pod_manifest["metadata"]["name"]] * 4, "지연은 calibration pod에만"
    assert all(c[3] == "calibration" and c[4] == "240s" for c in creates), "자동 만료 안전망 90+30+60+60"


def test_the_same_run_fails_when_the_candidate_is_only_10_seconds(base):
    world, deps, cfg = build_run(base, candidate=10.0)
    a = cal.run_calibration(deps, cfg, log=lambda *_: None)["analysis"]
    assert a["run_outcome"] == "FAIL" and a["failed_conditions"] == ["C7_t_min_le_candidate"] and a["T_min"] == 11


def test_restart_mid_stage_aborts_h1_keeps_partial_data_and_cleans_up(base):
    world, deps, cfg = build_run(base)
    world.hooks.append(counter_hook(lambda w: w.active_delay_ms() == 2000,
                                    lambda w: w.cal.__setitem__("restarts", 1), at=8))
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert result["hard_fail"]["code"] == "H1" and result["analysis"]["run_outcome"] == "FAIL"
    assert "C5_no_ready_restart_uid_oom_evict_node" in result["analysis"]["failed_conditions"]
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
    assert "C1_all_stages_allinjected" in result["analysis"]["failed_conditions"]
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
    assert result["analysis"]["run_outcome"] == "FAIL"
    assert_clean_after(world, result)


def test_cleanup_failure_is_reported_as_h8_and_the_run_is_fail(base):
    world, deps, cfg = build_run(base)
    world.delete_pod_works = False
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert result["cleanup"]["ok"] is False and result["cleanup"]["pod_deleted"] is False
    assert result["hard_fail"]["code"] == "H8" and result["analysis"]["run_outcome"] == "FAIL"
    assert "C8_cleanup_ok" in result["analysis"]["failed_conditions"]
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


def test_a_steady_window_probe_failure_stops_the_run_immediately_h10(base):
    world, deps, cfg = build_run(base)
    world.hooks.append(counter_hook(lambda w: w.active_delay_ms() == 4000,
                                    lambda w: w.probe_failure("Readiness"), at=8))
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert result["hard_fail"]["code"] == "H10" and "steady_4" in result["hard_fail"]["detail"]
    (event,) = result["probe_events"]
    assert (event["kind"], event["segment"], event["ambiguous"]) == ("Readiness", "steady_4", False)
    a = result["analysis"]
    assert a["run_outcome"] == "FAIL" and "C3_no_steady_probe_failure" in a["failed_conditions"]
    assert [w["name"] for w in result["windows"]][-1] == WORST and not result["windows"][-1]["complete"], \
        "그 자리에서 측정을 멈춘다 - stage-4 창은 미완료, 회복 창·교차검증 없음"
    assert result["crosscheck"] is None
    assert_clean_after(world, result)


def test_a_liveness_failure_anywhere_stops_the_run_immediately_h11(base):
    world, deps, cfg = build_run(base)
    world.hooks.append(counter_hook(lambda w: not w.chaos and w.clock() > w.cal["ready_at"] + 50,
                                    lambda w: w.probe_failure("Liveness", ago=2)))
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert result["hard_fail"]["code"] == "H11"
    assert [e["segment"] for e in result["probe_events"]] == ["baseline"]
    assert not any(c[0] == "chaos_create" for c in world.calls), "baseline에서 멈췄으므로 지연은 걸리지 않았다"
    assert result["analysis"]["run_outcome"] == "FAIL" and "C4_no_liveness_failure" in result["analysis"]["failed_conditions"]
    assert_clean_after(world, result)


def teardown_started(w):
    return any(c["delete_at"] is not None for c in w.chaos.values())


def test_a_timeout_failure_whose_probe_straddles_the_cr_deletion_is_recorded_separately_and_the_run_passes(base):
    """계약서 5.8 - 삭제 직후에 찍힌 timeout 유형 실패는 11초 timeout으로 역산한 probe 시작이 삭제 요청보다 앞이므로 이벤트 시각만으로
    teardown으로 단정하지 않고 transition_straddling으로 따로 센다. 단발이고 Ready·restart 영향이 없으면 실패가 아니다."""
    world, deps, cfg = build_run(base)
    world.hooks.append(counter_hook(teardown_started, lambda w: w.probe_failure("Readiness")))
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert result["hard_fail"] is None
    (event,) = result["probe_events"]
    assert (event["kind"], event["segment"]) == ("Readiness", "transition_straddling_1")
    delete_request = result["timeline"]["stages"][0]["delete_request"]
    assert event["probe_start_iso"] < delete_request < event["t_pc_iso"], "추정 실행 구간이 삭제 요청 시각을 가로지른다"
    a = result["analysis"]
    assert a["run_outcome"] == "PASS" and a["probe_event_counts"] == {"transition_straddling_1/Readiness": 1}, a["reasons"]
    assert a["transition_straddling"]["count"] == 1 and a["transition_straddling"]["not_a_profile_failure"] is True
    assert (a["transition_straddling"]["ready_transition"], a["transition_straddling"]["restart"]) == (False, False)
    assert result["crosscheck"]["ok"] and result["crosscheck"]["detail"]["Readiness"]["counter_failed"] == 1
    assert_clean_after(world, result)


def test_a_non_timeout_failure_after_the_deletion_is_a_pure_teardown_failure(base):
    world, deps, cfg = build_run(base)
    world.hooks.append(counter_hook(teardown_started,
                                    lambda w: w.probe_failure("Readiness", error="dial tcp 10.0.9.9:8000: connection refused")))
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    (event,) = result["probe_events"]
    assert event["segment"] == "teardown_1" and event["probe_start_iso"] == event["t_pc_iso"],         "timeout 유형이 아니면 실행 구간이 이벤트 시각 한 점이라 삭제 뒤 실패는 순수 teardown"
    assert result["analysis"]["run_outcome"] == "PASS" and result["analysis"]["transition_straddling"]["count"] == 0
    assert result["analysis"]["probe_event_counts"] == {"teardown_1/Readiness": 1}


def test_consecutive_teardown_readiness_failures_stop_the_run_h12(base):
    world, deps, cfg = build_run(base)
    world.hooks.append(counter_hook(teardown_started, lambda w: w.probe_failure("Readiness"), at=1))
    world.hooks.append(counter_hook(teardown_started, lambda w: w.probe_failure("Readiness", ago=2), at=2))
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert result["hard_fail"]["code"] == "H12"
    assert [e["segment"] for e in result["probe_events"]] == ["transition_straddling_1", "transition_straddling_1"]
    assert result["analysis"]["run_outcome"] == "FAIL"
    assert "C6_teardown_readiness_not_consecutive" in result["analysis"]["failed_conditions"]
    assert_clean_after(world, result)


def test_a_readiness_failure_outside_steady_and_teardown_is_recorded_only(base):
    """CR 생성~AllInjected 사이(ramp)의 실패는 어느 창에도 안 잡혀 사라지면 안 되고, 규칙상 판정에는 영향이 없다."""
    world, deps, cfg = build_run(base)
    world.hooks.append(counter_hook(lambda w: w.chaos and w.active_delay_ms() == 0 and not w.extra_events,
                                    lambda w: w.probe_failure("Readiness")))
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert result["hard_fail"] is None
    assert [e["segment"] for e in result["probe_events"]] == ["injection_ramp_1"]
    assert result["analysis"]["run_outcome"] == "PASS"


def test_shutdown_artifacts_after_the_pod_delete_request_are_recorded_and_excluded(base):
    world, deps, cfg = build_run(base)
    world.hooks.append(counter_hook(lambda w: w.cal["delete_at"] is not None, lambda w: w.probe_failure("Liveness")))
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert [(e["kind"], e["segment"]) for e in result["probe_events"]] == [("Liveness", "shutdown")]
    assert result["hard_fail"] is None and result["analysis"]["run_outcome"] == "PASS"
    assert result["pod_events"] and result["pod_events"][0]["reason"] == "Unhealthy", "원본 이벤트 보존"


def test_ready_loss_after_first_ready_is_h2(base):
    world, deps, cfg = build_run(base)
    world.hooks.append(lambda w: w.cal.__setitem__("unready", True) if w.cal and w.active_delay_ms() == 2000 else None)
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert result["hard_fail"]["code"] == "H2"
    assert_clean_after(world, result)


def test_an_instant_ready_transition_between_polls_is_h2(base):
    world, deps, cfg = build_run(base)
    world.hooks.append(lambda w: w.cal.__setitem__("ready_since", "2026-09-19T10:09:00Z")
                       if w.cal and w.active_delay_ms() == 2000 else None)
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert result["hard_fail"]["code"] == "H2" and "lastTransitionTime" in result["hard_fail"]["detail"]
    assert result["ready_condition_since"] == "2026-09-19T10:02:00Z"
    assert_clean_after(world, result)


def test_event_counter_mismatch_makes_the_run_invalid_not_pass(base):
    world, deps, cfg = build_run(base)
    world.metrics_override = {"Readiness/failed": 2.0}   # kubelet은 2번 실패라는데 이벤트는 0건 - 이벤트 유실
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    a = result["analysis"]
    assert result["hard_fail"] is None and a["run_outcome"] == "INVALID"
    assert a["failed_conditions"] == ["V3_probe_counter_crosscheck"] and "유실" in a["reasons"][0]
    assert_clean_after(world, result)


def test_prometheus_query_failure_makes_the_run_invalid(base):
    world, deps, cfg = build_run(base)
    world.metrics_error = RuntimeError("proxy 오류")
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert result["analysis"]["run_outcome"] == "INVALID" and "조회 실패" in result["crosscheck"]["problems"][0]
    assert_clean_after(world, result)


def test_clock_offset_missing_at_start_refuses_to_start_and_creates_nothing(base):
    world, deps, cfg = build_run(base)
    world.offset_missing_calls = {1}
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert result["hard_fail"]["code"] == "PREFLIGHT" and "오프셋" in result["hard_fail"]["detail"]
    assert world.calls == []


def test_a_missing_end_offset_falls_back_to_the_start_offset(base):
    world, deps, cfg = build_run(base)
    world.offset_missing_calls = {2}
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert result["clock_offset"]["end"] is None and result["clock_offset"]["used_sec"] == 0.0
    assert result["analysis"]["run_outcome"] == "PASS"


def test_preflight_failure_creates_nothing(base):
    world, deps, cfg = build_run(base)
    world.state["rollout"]["preview_selector"] = "preview-exists"
    result = cal.run_calibration(deps, cfg, log=lambda *_: None)
    assert result["hard_fail"]["code"] == "PREFLIGHT" and world.calls == [] and world.offset_calls == 0


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
    assert "§44.1" in out and "§44.2" in out and "H12" in out


@needs_kubectl
def test_dry_run_reports_the_candidate_override_without_touching_the_cluster(capsys):
    assert cal.main(["--dry-run", "--candidate-timeout-sec", "11"], deps_factory=boom) == 0
    out = capsys.readouterr().out
    assert "readiness=11.0s liveness=11.0s" in out and "T_min<=11" in out


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
    deps = make_deps(world, world.clock)
    return lambda _args: deps


ARGV11 = ["--execute", "--candidate-timeout-sec", "11"]


@needs_kubectl
def test_execute_refuses_to_start_when_preflight_fails(capsys, tmp_path):
    world = _CliWorld(FakeClock())
    world.state["chaos"] = ["leftover"]
    world.chaos["leftover"] = {"delay_ms": 1, "injected_at": 0, "delete_at": None}
    assert cal.main([*ARGV11, "--output-dir", str(tmp_path)], deps_factory=_cli_deps(world)) == 1
    assert world.calls == [] and list(tmp_path.iterdir()) == []
    assert "PREFLIGHT 실패" in capsys.readouterr().out


@needs_kubectl
def test_execute_writes_the_pilot_result_file_and_returns_by_outcome(tmp_path, capsys):
    world = _CliWorld(FakeClock())
    assert cal.main([*ARGV11, "--output-dir", str(tmp_path)], deps_factory=_cli_deps(world)) == 0
    (path,) = list(tmp_path.glob("calibration-network-tolerant-*.json"))
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["analysis"]["run_outcome"] == "PASS" and saved["cleanup"]["ok"] and saved["candidate_sec"] == 11
    assert saved["preregistration"] == "§44" and len(saved["windows"]) == 9 and saved["clock_offset"]["used_sec"] == 0.0
    assert saved["timeline"]["stages"][0]["allinjected"] and saved["crosscheck"]["ok"]
    c0 = world.manifest["spec"]["containers"][0]
    assert (c0["readinessProbe"]["timeoutSeconds"], c0["livenessProbe"]["timeoutSeconds"]) == (11, 11), \
        "후보는 calibration pod에만 적용된다"
    out = capsys.readouterr().out
    assert "run_outcome=PASS" in out and "C7_t_min_le_candidate" in out

    world = _CliWorld(FakeClock())
    world.hooks.append(counter_hook(lambda w: w.active_delay_ms() == 4000, lambda w: w.probe_failure("Liveness"), at=8))
    assert cal.main([*ARGV11, "--output-dir", str(tmp_path)], deps_factory=_cli_deps(world)) == 2
    world = _CliWorld(FakeClock())
    world.metrics_override = {"Readiness/failed": 5.0}
    assert cal.main([*ARGV11, "--output-dir", str(tmp_path)], deps_factory=_cli_deps(world)) == 2, "INVALID도 PASS가 아님"
    world = _CliWorld(FakeClock())
    world.delete_pod_works = False
    assert cal.main([*ARGV11, "--output-dir", str(tmp_path)], deps_factory=_cli_deps(world)) == 3
    world = _CliWorld(FakeClock())
    world.offset_missing_calls = {1}
    assert cal.main([*ARGV11, "--output-dir", str(tmp_path)], deps_factory=_cli_deps(world)) == 1, "시계 오프셋 없음 = 시작 안 함"
    assert world.calls == []


class _DoneHandle:
    """--preflight-only는 실제 시계로 handle.done()을 기다리므로(가짜 시계는 안 흐름) 즉시 끝나는 핸들을 쓴다."""

    def done(self):
        return True

    def result(self):
        return [{"kind": "health", "seq": i, "t": float(i), "latency": 0.004, "status": 200, "error": None}
                for i in range(3)]

    def error(self):
        return None


@needs_kubectl
def test_preflight_only_checks_the_offset_and_the_prometheus_chain_without_changing_anything(capsys):
    world = _CliWorld(FakeClock())
    world.start = lambda params: _DoneHandle()
    assert cal.main(["--preflight-only"], deps_factory=_cli_deps(world)) == 0
    out = capsys.readouterr().out
    assert "시계 오프셋" in out and "Prometheus prober_probe_total" in out and "PREFLIGHT OK" in out
    assert world.calls == []
    world = _CliWorld(FakeClock())
    world.start = lambda params: _DoneHandle()
    world.metrics_error = RuntimeError("proxy 오류")
    assert cal.main(["--preflight-only"], deps_factory=_cli_deps(world)) == 1
    assert "Prometheus probe 카운터 조회 실패" in capsys.readouterr().out
    world = _CliWorld(FakeClock())
    world.start = lambda params: _DoneHandle()
    world.offset_missing_calls = {1}
    assert cal.main(["--preflight-only"], deps_factory=_cli_deps(world)) == 1


# ---- J. 계약서 5.8 - transition_straddling 분류 ----------------------------------------------------
TIMEOUT_MSG = 'probe failed: Get "http://x:8000/health": context deadline exceeded (Client.Timeout exceeded while awaiting headers)'


def timeout_occ(t, kind="Readiness"):
    return {"kind": kind, "message": f"{kind} {TIMEOUT_MSG}", "polled_at": t + 3.0, "approx": False,
            "worker_ts": iso_z(math.floor(t))}


@pytest.mark.parametrize("t,segment", [
    (398.5, "steady_1"),                    # 삭제 요청(400) 전에 찍힌 실패는 steady
    (399.5, "steady_1"),                    # 경계 +-1초는 보수적으로 steady
    (405.5, "transition_straddling_1"),     # 추정 시작 394.5 < 삭제 요청 + 1 -> 가로지름
    (411.5, "transition_straddling_1"),     # 추정 시작 400.5 - 경계 안쪽
    (412.5, "teardown_1"),                  # 추정 시작 401.5 - 삭제 요청 뒤에 시작한 probe = 순수 teardown
    (417.5, "teardown_1"),
    (418.5, "between_stage_1"),             # teardown 구간 밖
])
def test_timeout_failures_split_into_steady_straddling_and_pure_teardown_by_the_estimated_probe_interval(t, segment):
    c = cal.classify_occurrence(timeout_occ(t), make_tl(), 0.0, probe_timeout_sec=11)
    assert c["segment"] == segment
    assert c["probe_start_est"] == pytest.approx(c["t_pc"] - 11)


def test_without_a_probe_timeout_the_old_event_time_classification_is_unchanged():
    assert cal.classify_occurrence(timeout_occ(405.5), make_tl(), 0.0)["segment"] == "teardown_1"
    assert cal.classify_occurrence(timeout_occ(405.5), make_tl(), 0.0)["probe_start_est"] is None


def test_non_timeout_and_startup_failures_are_not_treated_as_straddling():
    refused = {"kind": "Readiness", "message": "Readiness probe failed: dial tcp 10.0.0.5:8000: connect: connection refused",
               "polled_at": 410.0, "approx": False, "worker_ts": iso_z(405)}
    c = cal.classify_occurrence(refused, make_tl(), 0.0, probe_timeout_sec=11)
    assert c["segment"] == "teardown_1" and c["probe_start_est"] == c["t_pc"], "timeout 유형이 아니면 실행 구간 = 이벤트 시각 한 점"
    startup = {**refused, "kind": "Startup", "message": f"Startup {TIMEOUT_MSG}"}
    assert cal.classify_occurrence(startup, make_tl(), 0.0, probe_timeout_sec=11)["probe_start_est"] is None


def test_a_liveness_timeout_failure_is_classified_like_readiness():
    assert cal.classify_occurrence(timeout_occ(405.5, "Liveness"), make_tl(), 0.0, probe_timeout_sec=11)["segment"] == "transition_straddling_1"


def test_findings_allow_a_single_straddling_failure_but_not_consecutive_ones_or_pure_liveness_failures():
    def codes(*events):
        return {c for c, _ in cal.probe_event_findings(list(events))}
    assert codes(ev("Readiness", "transition_straddling_4", 850.0)) == set()
    assert codes(ev("Liveness", "transition_straddling_4", 850.0)) == set(), "단발 straddling liveness도 별도 집계 - H11 아님"
    assert codes(ev("Liveness", "teardown_4", 855.0)) == {"H11"}, "순수 teardown liveness 실패는 여전히 실패"
    assert codes(ev("Readiness", "transition_straddling_4", 850.0), ev("Readiness", "transition_straddling_4", 856.0)) == {"H12"}
    assert codes(ev("Readiness", "transition_straddling_4", 850.0), ev("Readiness", "teardown_4", 858.0)) == {"H12"}
    assert codes(ev("Liveness", "transition_straddling_4", 850.0), ev("Liveness", "transition_straddling_4", 860.0)) == {"H12"}
    assert codes(ev("Readiness", "transition_straddling_1", 405.0), ev("Readiness", "transition_straddling_2", 555.0)) == set(),         "서로 다른 전이 구간에 각 1건은 비연속"
    assert codes(ev("Readiness", "transition_straddling_4", 850.0), ev("Readiness", "steady_4", 845.0)) == {"H10", "H12"},         "steady 실패는 straddling으로 흡수되지 않는다"


def test_judge_reports_the_straddling_row_and_a_ready_transition_makes_the_trial_fail():
    result = clean_result()
    result["probe_events"].append(ev("Readiness", "transition_straddling_4", 850.0))
    a = cal.judge_v2(result)
    ts = a["transition_straddling"]
    assert a["run_outcome"] == "PASS" and (ts["count"], ts["ready_transition"], ts["restart"]) == (1, False, False)
    assert ts["not_a_profile_failure"] is True and "Endpoint 유지" in ts["endpoint_impact"]
    assert "steady" not in json.dumps(a["conditions"]["C3_no_steady_probe_failure"], ensure_ascii=False).replace("steady 구간", "")
    result["hard_fail"] = {"code": "H2", "detail": "Ready 전이"}
    a = cal.judge_v2(result)
    assert a["run_outcome"] == "FAIL" and a["transition_straddling"]["ready_transition"] is True
    assert a["transition_straddling"]["not_a_profile_failure"] is False and "있음" in a["transition_straddling"]["endpoint_impact"]


EVIDENCE = cal.REPO_ROOT / "docs" / "design" / "evidence" / "network-tolerant-calibration"
V2_RUNS = ["calibration-network-tolerant-calib-net-tolerant-20260919t135919z.json",
           "calibration-network-tolerant-calib-net-tolerant-20260919t142045z.json"]


@pytest.mark.parametrize("name", V2_RUNS)
def test_reanalysis_of_the_two_v2_runs_classifies_their_stage4_readiness_failure_as_transition_straddling(name):
    result = json.loads((EVIDENCE / name).read_text(encoding="utf-8"))
    original = [e["segment"] for e in result["probe_events"] if e["kind"] == "Readiness" and e["segment"] != "shutdown"]
    assert original == ["teardown_4"], "원본 분류는 이벤트 시각만 본 결과다(원본 JSON은 수정하지 않는다)"
    again = cal.reanalyze(result)
    kept = [e for e in again["probe_events"] if e["kind"] == "Readiness" and e["segment"] != "shutdown"]
    assert [e["segment"] for e in kept] == ["transition_straddling_4"] and kept[0]["ambiguous"] is False
    deleted = cal.timeline_from_iso(result["timeline"])["stages"][3]["delete_request"]
    assert kept[0]["probe_start_est"] < deleted < kept[0]["t_pc"], "추정 probe 실행 구간이 CR 삭제 요청 시각을 가로지른다"
    a = again["analysis"]
    assert a["run_outcome"] == "PASS" and a["failed_conditions"] == []
    ts = a["transition_straddling"]
    assert (ts["count"], ts["ready_transition"], ts["restart"], ts["not_a_profile_failure"]) == (1, False, False, True)
    assert a["probe_event_counts"]["transition_straddling_4/Readiness"] == 1 and not any(
        k.startswith(("steady_", "teardown_")) for k in a["probe_event_counts"])
    assert result["analysis"]["run_outcome"] == "PASS" and result["probe_events"][-4]["segment"] != "transition_straddling_4"


def test_reanalyze_cli_reads_the_saved_json_only_and_writes_a_separate_derived_file(tmp_path, capsys):
    source = EVIDENCE / V2_RUNS[0]
    before = source.read_bytes()
    assert cal.main(["--reanalyze", str(source), "--output-dir", str(tmp_path)], deps_factory=boom) == 0
    out = capsys.readouterr().out
    assert "transition_straddling: 1건" in out and "Ready 전이 없음" in out and "restart 없음" in out
    (derived,) = list(tmp_path.glob("reanalysis-transition-straddling-*.json"))
    assert json.loads(derived.read_text(encoding="utf-8"))["analysis"]["transition_straddling"]["count"] == 1
    assert source.read_bytes() == before, "원본 증거 JSON은 그대로"


def test_events_after_the_pods_own_termination_are_shutdown_even_when_later_stage_boundaries_follow():
    """trial에서는 promotion 뒤 Argo scale-down이 stage 도중에 target을 종료시킨다 - 그 뒤 실패는 다음 stage 경계가 있어도 종료 아티팩트."""
    tl = make_tl()
    tl["pod_delete_request"] = 430.0   # stage 1 teardown(418) 뒤, stage 2 시작(450) 전에 종료 시작
    assert cal.classify_occurrence(timeout_occ(455.5), tl, 0.0, probe_timeout_sec=11)["segment"] == "shutdown"
    assert cal.classify_occurrence(timeout_occ(425.5), tl, 0.0, probe_timeout_sec=11)["segment"] == "between_stage_1"
    assert cal.classify_occurrence(timeout_occ(405.5), tl, 0.0, probe_timeout_sec=11)["segment"] == "transition_straddling_1"


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
