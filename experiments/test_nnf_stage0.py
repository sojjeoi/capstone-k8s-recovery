"""noisy-neighbor CPU Stage 0 - 한 축 강도·weak/strong 판정·단일 지표 규칙 보정(누설 금지)·3-consecutive·기준 5·상태 분리·
trial 흐름·probe 배치·정리·provenance. 네트워크·클러스터 접속 없음."""
import copy
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest
import yaml

import guarded_cleanup_gate as gcg
import nnf_stage0_analysis as na
import run_guarded_pilot as gp
import run_nnf_stage0 as rn
import test_run_guarded_pilot as tgp

sys.path.insert(0, str(rn.REPO_ROOT / "chaos" / "loadgen"))
import cpu_burner  # noqa: E402

T0 = datetime(2026, 10, 10, tzinfo=timezone.utc)
CFG = yaml.safe_load(rn.CONFIG.read_text(encoding="utf-8"))
TIMING = CFG["timing"]
LAD = CFG["stress"]["development_ladder"]


def at(s):
    return T0 + timedelta(seconds=s)


# ---------------- 설정: 한 축 ----------------
def test_ladder_varies_only_workers_and_amendment_unset():
    assert [x["workers"] for x in LAD] == [2, 4, 6, 8, 12] and all(set(x) == {"name", "workers"} for x in LAD)
    assert CFG["stress"]["axis"] == "workers" and CFG["stress"]["confirmatory"] is None and CFG["single_metric_rules"] is None
    plan = rn.plan_for("development", CFG)
    assert [p["workers"] for p in plan] == [2, 4, 6, 8, 12] and all(p["role"] is None for p in plan)


def test_burner_pod_identity():
    b = rn.make_cpu_burner("rid", 7190, None, CFG)
    assert b.info["pod"] == rn.burner_pod_name("rid", 7190) and b.info["node"] == "sj-worker2"


def test_burner_result_counts_full_seconds_only():
    log = "sec,alive,wall_utc\n0,4,x\n1,4,x\n2,3,x\n3,0,x\n"
    assert rn.burner_result(log, 4, "0") == {"rc": 0, "ran_sec": 2, "log_rows": 4}
    assert rn.burner_result("", 4, "")["rc"] is None


def test_burner_script_runs_and_rejects_bad_args(tmp_path):
    log = tmp_path / "b.csv"
    assert cpu_burner.main(["--workers", "1", "--duration-sec", "1", "--log", str(log), "--stop-file", str(tmp_path / "S")]) == 0
    assert log.read_text().startswith("sec,alive,wall_utc")
    assert cpu_burner.main(["--workers", "0", "--duration-sec", "1", "--log", str(log)]) == 3


# ---------------- 규칙 후보: 누설 금지 ----------------
def test_rule_candidates_preregistered_and_no_latency_or_stress_pod_inputs():
    assert sorted(na.RULE_METRICS) == ["A_node_cpu_psi_waiting", "B_node_schedstat_waiting", "C_node_load1",
                                       "D_vllm_container_cpu", "E_vllm_cfs_throttled_ratio", "F_node_cpu_busy_cores"]
    assert {r["direction"] for k, r in na.RULE_METRICS.items() if k[0] in "DE"} == {"two_sided"}
    with pytest.raises(ValueError, match="금지"):
        na.validate_rule_metrics({"X_probe_latency_p95": {"keys": ["cpu"]}})
    with pytest.raises(ValueError, match="금지"):
        na.validate_rule_metrics({"X": {"keys": ["burner_cpu"]}})
    with pytest.raises(ValueError, match="금지"):
        na.validate_rule_metrics({"X": {"keys": ["e2e_latency"]}})   # 수집 목록에 없는 키도 거부


# ---------------- 3-consecutive·보정 ----------------
def vals(*xs):
    return [(float(i), x) for i, x in enumerate(xs)]


def test_first_trigger_needs_three_consecutive_and_none_resets():
    r = {"upper": 1.0, "lower": None}
    assert na.first_trigger(vals(2, 2, 0, 2, 2, 2), r) == 5.0
    assert na.first_trigger(vals(2, 2, None, 2, 2), r) is None
    assert na.first_trigger(vals(1.0, 1.0, 1.0), r) is None   # 같으면 발화 아님(> 엄격)
    assert na.first_trigger(vals(0.1, 0.1, 0.1), {"upper": None, "lower": 0.5}) == 2.0


def test_calibrate_most_sensitive_zero_false_trigger():
    segs = [vals(1, 5, 1, 2, 3, 2), vals(4, 4, 0)]
    c = na.calibrate_rule(segs, "upper")
    assert c["upper"] == 2 and all(na.first_trigger(s, c) is None for s in segs)
    lower_t = {"upper": c["upper"] - 1e-9, "lower": None}
    assert any(na.first_trigger(s, lower_t) is not None for s in segs)   # 더 민감하면 보정 증거에서 오탐
    two = na.calibrate_rule(segs, "two_sided")
    assert two["lower"] == 3 and all(na.first_trigger(s, two) is None for s in segs)
    assert na.calibrate_rule([vals(1, None, 1)], "upper")["upper"] is None


# ---------------- 합성 자료 ----------------
def counter(rate_fn, t0, t1, step):
    out, v = [], 0.0
    for t in range(t0, t1, step):
        out.append([at(t).timestamp(), v])
        v += rate_fn(t) * step
    return out


def metrics(psi=lambda t: 0.1, cpu=lambda t: 1.7, burner=lambda t: 0.0, gap_at=None):
    s = {"cpu": counter(cpu, -400, 3000, 10), "thr_periods": counter(lambda t: 3.0, -400, 3000, 10),
         "periods": counter(lambda t: 10.0, -400, 3000, 10), "burner_cpu": counter(burner, -400, 3000, 10),
         "gen_tokens": counter(lambda t: 1.0, -400, 3000, 15), "psi_cpu": counter(psi, -400, 3000, 30),
         "schedstat_wait": counter(lambda t: 3.0, -400, 3000, 30), "cpu_idle": counter(lambda t: 4.5, -400, 3000, 30),
         "load1": [[at(t).timestamp(), 5.0] for t in range(-400, 3000, 30)]}
    if gap_at:
        s["psi_cpu"] = [p for p in s["psi_cpu"] if not (gap_at[0] <= p[0] - T0.timestamp() <= gap_at[1])]
    return {"series": s}


def probe_rows(slow=None, until=3000):
    return [{"sent_at": at(i), "latency": 3.0 if slow and slow[0] <= i <= slow[1] else 0.25, "success": True}
            for i in range(-10, until)]


def stage(name, s, workers=4, role=None, rc=0, ran=300):
    return {"name": name, "role": role, "workers": workers, "start_utc": at(s).isoformat(), "end_utc": at(s + 300).isoformat(),
            "burner": {"rc": rc, "ran_sec": ran}}


def ev(st, m, rows):
    return na.stage_eval(st, rows=rows, obs0=at(0), metrics=m, pre_window=(at(0), at(300)), timing=TIMING,
                         applied_min_fraction=0.4)


def test_weak_stage_contention_change_slo_normal_and_applied():
    x = ev(stage("W4", 300), metrics(psi=lambda t: 0.5 if 300 <= t < 600 else 0.1, burner=lambda t: 3.5 if t >= 300 else 0),
           probe_rows())
    assert x["slo_normal"] and x["contention_change"] and x["stress_applied"] and na.weak_ok(x) and not na.strong_ok(x)


def test_strong_stage_and_stress_not_applied():
    m = metrics(psi=lambda t: 2.0 if t >= 300 else 0.1, burner=lambda t: 6.0 if t >= 300 else 0)
    x = ev(stage("W8", 300, workers=8), m, probe_rows(slow=(400, 520)))
    assert not x["slo_normal"] and x["t_slo"] and na.strong_ok(x)
    y = ev(stage("W8", 300, workers=8), metrics(psi=lambda t: 2.0 if t >= 300 else 0.1, burner=lambda t: 1.0), probe_rows())
    assert not y["stress_applied"] and not na.weak_ok(y)   # 0.4 × 8 = 3.2 core 미만이면 부하 미적용
    z = ev(stage("W8", 300, workers=8, rc=3, ran=40), m, probe_rows())
    assert not z["burner_ok"] and not z["stress_applied"]


def test_integrity_gap_detected():
    x = ev(stage("W4", 300), metrics(gap_at=(350, 450)), probe_rows())
    assert not x["integrity"]["ok"] and any(p.startswith("psi_cpu_gap") for p in x["integrity"]["problems"])


# ---------------- analyze_session ----------------
def sess(**over):
    s = {"run": "nnw90", "kind": "confirm_weak", "outcome_status": "completed", "cleanup_errors": [],
         "observation_start_utc": at(0).isoformat(), "target_pod": {"node": "sj-worker2"}, "prometheus_node": "sj-worker",
         "probe_pod": {"node": "sj-worker"}, "target_uid_constant": True}
    return {**s, **over}


def test_analyze_session_measurement_plane_invalid_reasons():
    r = na.analyze_session(sess(probe_pod={"node": "sj-worker2"}), metrics(), TIMING)
    assert "probe_on_target_node" in r["invalid_reasons"]
    assert "prometheus_on_target_node" in na.analyze_session(sess(prometheus_node="sj-worker2"), metrics(), TIMING)["invalid_reasons"]
    assert "probe_node_unknown" in na.analyze_session(sess(probe_pod=None), metrics(), TIMING)["invalid_reasons"]
    for why in ("TrialInvalid: 무주입 관찰 중 active pod OOMKilled(예상 밖) - fail-closed",
                "TrialInvalid: 무주입 관찰 중 active pod 재시작 감지(예상 밖) - fail-closed",
                "HarnessCorrupted: 무주입 관찰 중 Node 상태 이상: {}"):
        a = na.analyze_session(sess(outcome_status="aborted", outcome_reason=why), metrics(), TIMING)
        assert a["alive_violation"] and not a["valid"]
    b = na.analyze_session(sess(outcome_status="aborted", outcome_reason="TrialInvalid: 준비 조건 상한"), metrics(), TIMING)
    assert not b["alive_violation"]


# ---------------- 개발 선택·보정 구간 ----------------
STAGE_T = {"W2": 300, "W4": 720, "W6": 1140, "W8": 1560}


def srec(name, weak=False, strong=False):
    s = STAGE_T.get(name, 300)
    return {"name": name, "computable": True, "stress_applied": True, "contention_change": weak or strong,
            "slo_normal": not strong, "integrity": {"ok": True}, "_start": at(s).isoformat(), "_end": at(s + 300).isoformat()}


def dev_rep(run, spec, valid=True):
    return {"run": run, "valid": valid, "stages": [srec(n, *spec.get(n, (False, False))) for n in ("W2", "W4", "W6", "W8")],
            "pre_stress": [at(0).isoformat(), at(300).isoformat()]}


SPEC = {"W2": (True, False), "W4": (True, False), "W6": (False, True), "W8": (False, True)}


def test_dev_selection_weak_highest_strong_lowest_above_weak():
    s = na.select_dev([dev_rep("a", SPEC), dev_rep("b", SPEC)], LAD)
    assert (s["weak"], s["strong"]) == ("W4", "W6")
    b = dev_rep("b", {**SPEC, "W6": (False, False)})
    assert na.select_dev([dev_rep("a", SPEC), b], LAD)["strong"] == "W8"
    assert na.select_dev([dev_rep("a", SPEC)], LAD)["reason"].startswith("valid_dev_reps")


def test_calibration_excludes_strong_stages_so_strong_data_cannot_move_thresholds():
    base = [{"run": "nnb90", "valid": True, "slo_normal": True, "observation": [at(-300).isoformat(), at(0).isoformat()]}]
    dev = [dev_rep("nnd90", SPEC), dev_rep("nnd91", SPEC)]
    segs = na.calibration_segments(base, dev, LAD, "W4")
    labels = {x[1] for x in segs}
    assert labels == {"baseline", "pre_stress", "W2", "W4"}
    m = {r: metrics() for r in ("nnb90", "nnd90", "nnd91")}
    c1 = na.calibrate(segs, m)
    m2 = copy.deepcopy(m)
    for r in ("nnd90", "nnd91"):   # strong 단계(W6·W8, 1140 s 이후)만 아주 크게 바꿔도 임계값 불변
        m2[r]["series"]["psi_cpu"] = counter(lambda t: 9.0 if t >= 1260 else 0.1, -400, 3000, 30)
        m2[r]["series"]["cpu"] = counter(lambda t: 0.2 if t >= 1260 else 1.7, -400, 3000, 10)
    c2 = na.calibrate(segs, m2)
    for k in na.RULE_METRICS:
        assert (c2[k]["upper"], c2[k]["lower"]) == (c1[k]["upper"], c1[k]["lower"])
    assert c1["A_node_cpu_psi_waiting"]["upper"] is not None and c1["D_vllm_container_cpu"]["lower"] is not None


# ---------------- 기준 5·최종 상태 ----------------
def rules(a_upper):
    r = {k: {"direction": v["direction"], "upper": None, "lower": None, "consecutive": 3, "window_sec": v["win"],
             "step_sec": v["step"]} for k, v in na.RULE_METRICS.items()}
    r["A_node_cpu_psi_waiting"]["upper"] = a_upper
    return r


def conf(run, kind, slo_normal, t_slo_rel=None, valid=True, reasons=()):
    st = {**srec("W8" if kind == "confirm_strong" else "W4", weak=slo_normal, strong=not slo_normal),
          "_start": at(300).isoformat(), "_end": at(600).isoformat(),
          "t_slo": at(300 + t_slo_rel).isoformat() if t_slo_rel else None, "t_slo_rel_stage_sec": t_slo_rel}
    return {"run": run, "kind": kind, "valid": valid, "invalid_reasons": list(reasons), "stages": [st],
            "observation": [at(0).isoformat(), at(840).isoformat()], "pre_stress": [at(0).isoformat(), at(300).isoformat()]}


MBR = {"w1": metrics(psi=lambda t: 0.3 if 300 <= t < 600 else 0.1), "w2": metrics(psi=lambda t: 0.3 if 300 <= t < 600 else 0.1),
       "s1": metrics(psi=lambda t: 2.0 if 300 <= t < 600 else 0.1), "s2": metrics(psi=lambda t: 2.0 if 300 <= t < 600 else 0.1)}
W2 = [conf("w1", "confirm_weak", True), conf("w2", "confirm_weak", True)]
S2 = [conf("s1", "confirm_strong", False, 150), conf("s2", "confirm_strong", False, 150)]


def test_one_trivial_metric_fails_criterion5_but_scenario_feasible():
    v = na.final_verdict(W2, S2, rules(0.35), MBR)
    a = v["single_metric"]["A_node_cpu_psi_waiting"]
    assert a["trivial_separator"] and all(not x["false_trigger"] for x in a["weak"]) and all(x["pre_slo"] for x in a["strong"])
    assert all(x["lead_sec"] > 0 for x in a["strong"])
    assert v["criteria"]["C5_no_trivial_single_metric"] is False and v["criteria"]["C2_vllm_cpu_not_trivial"] is True
    assert v["SCENARIO_FEASIBLE"] is True and v["AIOPS_JUSTIFIED"] is False


def test_no_trivial_metric_passes_criterion5():
    v = na.final_verdict(W2, S2, rules(5.0), MBR)
    assert v["criteria"]["C5_no_trivial_single_metric"] is True and v["SCENARIO_FEASIBLE"] and v["AIOPS_JUSTIFIED"]


def test_weak_false_trigger_or_late_trigger_is_not_trivial():
    v = na.final_verdict(W2, S2, rules(0.2), MBR)   # weak 0.3 > 0.2 → weak 오탐 → trivial 아님
    a = v["single_metric"]["A_node_cpu_psi_waiting"]
    assert not a["trivial_separator"] and any(x["false_trigger"] for x in a["weak"])
    early = [conf("s1", "confirm_strong", False, 20), conf("s2", "confirm_strong", False, 20)]   # t_slo가 발화보다 먼저
    v2 = na.final_verdict(W2, early, rules(0.35), MBR)
    assert not v2["single_metric"]["A_node_cpu_psi_waiting"]["trivial_separator"] and v2["criteria"]["C5_no_trivial_single_metric"]


def test_criteria_1_to_4_failures_make_scenario_infeasible():
    v = na.final_verdict(W2, [S2[0], conf("s2", "confirm_strong", True)], rules(5.0), MBR)
    assert v["criteria"]["C4_strong_degradation_2of2"] is False and v["SCENARIO_FEASIBLE"] is False and not v["AIOPS_JUSTIFIED"]
    bad = conf("s0", "confirm_strong", False, valid=False, reasons=("integrity:['psi_cpu_gap=90s']",))
    v3 = na.final_verdict(W2, [bad] + S2, rules(5.0), MBR)
    assert v3["criteria"]["C3_measurement_integrity"] is False and v3["SCENARIO_FEASIBLE"] is False
    dead = {**conf("s0", "confirm_strong", False, valid=False), "alive_violation": True}
    assert na.final_verdict(W2, [dead] + S2, rules(5.0), MBR)["criteria"]["C1_strong_slow_but_alive_2of2"] is False
    v5 = na.final_verdict(W2[:1], S2, rules(5.0), MBR)
    assert v5["criteria"]["W_weak_state_2of2"] is False and v5["criteria"]["C5_no_trivial_single_metric"] is None


# ---------------- trial ----------------
class Clock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, s):
        self.t += s

    def mono(self):
        return self.t


class FakeBurner:
    def __init__(self, clock, rc=0):
        self.c, self.rc, self.calls, self.t0, self.info = clock, rc, [], {}, {"pod": "b"}

    def prepare(self):
        self.calls.append("prepare")

    def start(self, name, workers, dur):
        self.calls.append(f"start:{name}:{workers}")
        self.t0[name] = self.c.t

    def done(self, name):
        return self.c.t - self.t0[name] >= 300

    def stop(self):
        self.calls.append("stop")

    def fetch(self, name):
        return {"rc": self.rc, "ran_sec": 300}

    def cleanup(self):
        self.calls.append("cleanup")


def rows_until(clock, slow=None):
    def rows():
        return [r for r in probe_rows(slow, int(clock.t) + 1) if (r["sent_at"] - T0).total_seconds() + r["latency"] <= clock.t]
    return rows


def run_trial(kind, plan, *, slow=None, safety=None, ready_after=0, rc=0, probe_node="sj-worker"):
    c = Clock()
    burner = FakeBurner(c, rc) if plan else None
    o = rn.trial(kind, plan, TIMING, burner=burner, prober=tgp.FakeProber(),
                 safety=safety(c) if safety else (lambda s, p: None), probe_rows=rows_until(c, slow),
                 readiness_fn=lambda t: [] if c.t >= ready_after else ["scrape_incomplete"],
                 probe_place_fn=lambda: rn.probe_placement(lambda: [{"name": "ramp-probe-x", "node": probe_node}]),
                 sleep=c.sleep, monotonic=c.mono, now=lambda: at(c.t))
    return o, burner


DEV = rn.plan_for("development", CFG)


def test_trial_burner_prepared_first_and_dev_stops_at_slo_boundary():
    o, b = run_trial("development", DEV, slow=(950, 1050))
    assert o["status"] == "completed" and o["slo_boundary_stage"] == "W4" and o["stages_not_run"] == ["W6", "W8", "W12"]
    assert b.calls[0] == "prepare" and [x for x in b.calls if x.startswith("start")] == ["start:W2:2", "start:W4:4"]
    assert b.calls[-2:] == ["stop", "cleanup"] and o["cleanup_status"] == "complete" and o["probe_pod"]["node"] == "sj-worker"


def test_trial_probe_on_stress_node_is_invalid_and_cleans_up():
    o, b = run_trial("development", DEV, probe_node="sj-worker2")
    assert o["status"] == "aborted" and "부하 노드" in o["reason"] and b.calls[-1] == "cleanup"
    with pytest.raises(rn.TrialInvalid, match="probe pod 수"):
        rn.probe_placement(lambda: [{"name": "a", "node": "x"}, {"name": "b", "node": "y"}])


def test_trial_confirm_strong_single_stage_from_amendment():
    cfg = copy.deepcopy(CFG)
    cfg["stress"]["confirmatory"] = {"weak": {"name": "W4", "workers": 4}, "strong": {"name": "W8", "workers": 8}}
    o, b = run_trial("confirm_strong", rn.plan_for("confirm_strong", cfg))
    assert o["status"] == "completed" and [x["role"] for x in o["stress_stages"]] == ["strong"]
    assert [x for x in b.calls if x.startswith("start")] == ["start:W8:8"]
    assert rn.plan_for("confirm_weak", cfg)[0]["workers"] == 4


def test_trial_safety_abort_and_burner_failure_clean_up():
    def mk(c):
        def safety(s, p):
            if c.t > 900:
                raise rn.HarnessCorrupted("node NotReady")
        return safety
    o, b = run_trial("development", DEV, safety=mk)
    assert o["status"] == "aborted" and "NotReady" in o["reason"] and b.calls[-2:] == ["stop", "cleanup"]
    o2, b2 = run_trial("development", DEV, rc=3)
    assert o2["status"] == "aborted" and "rc=3" in o2["reason"] and b2.calls[-1] == "cleanup"


def test_trial_baseline_no_burner_and_readiness_timeout():
    o, b = run_trial("baseline", [])
    assert o["status"] == "completed" and b is None and o["observation_end_utc"]
    o2, b2 = run_trial("development", DEV, ready_after=10_000)
    assert o2["status"] == "aborted" and "준비 조건 상한" in o2["reason"] and b2.calls[-1] == "cleanup"


def test_wait_cpu_settled_ok_and_timeout():
    pre = (at(0).isoformat(), at(300).isoformat())
    c = Clock()
    flat = lambda sel, a, b: counter(lambda t: 4.5, -400, 3000, 30)   # noqa: E731 - busy 3.5 core
    assert rn.wait_cpu_settled(flat, pre, TIMING, sleep=c.sleep, monotonic=c.mono, now=lambda: at(900 + c.t))["ok"]
    hot = lambda sel, a, b: counter(lambda t: 4.5 if t < 300 else 0.5, -400, 3000, 30)   # noqa: E731 - 계속 7.5 core
    c2 = Clock()
    r = rn.wait_cpu_settled(hot, pre, TIMING, sleep=c2.sleep, monotonic=c2.mono, now=lambda: at(900 + c2.t))
    assert not r["ok"] and r["reason"] == "not_settled"


def test_readiness_check_keys(monkeypatch):
    snap = {"pods": [{"uid": "u1"}]}
    monkeypatch.setattr(gcg, "check_canonical", lambda s, delay: ["residual: probe=['ramp-probe-x']"])
    assert rn.readiness_check("u1", "p", lambda: snap, lambda sel, a, b: [0] * 12, at(0), "b") == []
    monkeypatch.setattr(gcg, "check_canonical", lambda s, delay: [])
    p = rn.readiness_check("u2", "p", lambda: snap, lambda sel, a, b: [0] * 2, at(0), "b")
    assert "target_uid_changed" in p and any(x.startswith("scrape_incomplete:psi_cpu") for x in p)


# ---------------- run_session(동결 World) ----------------
def _run(w, run, monkeypatch, node="sj-worker2", prom_node="sj-worker"):
    monkeypatch.setattr(rn.wa, "stage_slo_verdict",
                        lambda rows, a0, s, e: {"computable": True, "reason": None, "violating": False, "episodes": []})
    probe = w.results.parent / "probe-fake.csv"
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    probe.write_text(f"sent_at,latency,success\n{future},0.25,True\n", encoding="utf-8")
    return rn.run_session(run, deps=w.deps(probe_csv_path=lambda rid, rep: probe,
                                           read_probe_pods=lambda: [{"name": "ramp-probe-x", "node": "sj-worker"}]),
                          results_dir=w.results, make_burner=lambda *a: FakeBurner(w.clock),
                          prom_raw_fn=lambda sel, a, b: [[a.timestamp(), 0.0]] * 12,
                          node_of_fn=lambda ns, pod: None if pod == "b" else node, prometheus_node_fn=lambda: prom_node)


def test_run_session_baseline_end_to_end_no_promotion(tmp_path, monkeypatch):
    w = tgp.World(tmp_path, monkeypatch, mode=gp.MODE_RO, send_at=())
    s = _run(w, "nnb90", monkeypatch)
    assert s["kind"] == "baseline" and s["outcome_status"] == "completed" and s["cleanup_errors"] == []
    assert w.promotes == [] and w.overrides == [] and w.policy.snapshot()["context"] is None
    assert s["probe_pod"]["node"] == "sj-worker" and (w.results / f"metrics-{s['run_id']}.json").exists()


def test_run_session_placement_schedule_amendment_and_reuse(tmp_path, monkeypatch):
    w = tgp.World(tmp_path, monkeypatch, mode=gp.MODE_RO, send_at=())
    with pytest.raises(SystemExit, match="순서 밖"):
        _run(w, "nnd90", monkeypatch)
    with pytest.raises(SystemExit, match="placement"):
        _run(w, "nnb90", monkeypatch, prom_node="sj-worker2")
    w2 = tgp.World(tmp_path / "b", monkeypatch, mode=gp.MODE_RO, send_at=())
    _run(w2, "nnb90", monkeypatch)
    _run(w2, "nnd90", monkeypatch)
    with pytest.raises(SystemExit, match="amendment"):
        _run(w2, "nnw90", monkeypatch)
    with pytest.raises(SystemExit, match="재사용 금지"):
        _run(w2, "nnb90", monkeypatch)


def test_amendment_ready_requires_all_rule_metrics_non_null_and_weak_below_strong():
    cfg = copy.deepcopy(CFG)
    cfg["stress"]["confirmatory"] = {"weak": {"name": "W4", "workers": 4}, "strong": {"name": "W8", "workers": 8}}
    assert not rn.amendment_ready(cfg)
    full = {k: {"upper": 1.0, "lower": 0.5} for k in na.RULE_METRICS}
    cfg["single_metric_rules"] = {k: full[k] for k in list(na.RULE_METRICS)[:-1]}
    assert not rn.amendment_ready(cfg)
    cfg["single_metric_rules"] = full
    assert rn.amendment_ready(cfg)
    cfg["single_metric_rules"] = {**full, "A_node_cpu_psi_waiting": {"upper": None, "lower": None}}
    assert not rn.amendment_ready(cfg)   # null 임계값 = 발화 불가 = C5 공허 통과 → 거부
    cfg["single_metric_rules"] = {**full, "D_vllm_container_cpu": {"upper": 2.0, "lower": None}}
    assert not rn.amendment_ready(cfg)
    cfg["single_metric_rules"] = full
    cfg["stress"]["confirmatory"]["strong"]["workers"] = 4
    assert not rn.amendment_ready(cfg)


def test_amendment_guard_blocks_hand_edited_rules_and_config_drift(tmp_path):
    prev = {k: {"upper": 1.0, "lower": 0.5 if v["direction"] == "two_sided" else None} for k, v in na.RULE_METRICS.items()}
    rules = copy.deepcopy(prev)
    for rid, sha in (("a", "x"), ("b", "x")):
        (tmp_path / f"contract-{rid}.json").write_text(json.dumps({"config_sha256": sha}), encoding="utf-8")
    assert na.amendment_guard(rules, prev, tmp_path, ["a", "b"])["ok"]
    rules["A_node_cpu_psi_waiting"]["upper"] = 1.2   # 결과를 보고 손으로 바꾼 임계값
    assert "rule_not_from_calibration:A_node_cpu_psi_waiting" in na.amendment_guard(rules, prev, tmp_path, ["a", "b"])["problems"]
    (tmp_path / "contract-b.json").write_text(json.dumps({"config_sha256": "y"}), encoding="utf-8")
    assert not na.amendment_guard(prev, prev, tmp_path, ["a", "b"])["ok"]


def test_provenance_fail_closed_and_amendment_precedence(tmp_path):
    files = {}
    for rel in rn.PROVENANCE_REQUIRED.values():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(rel, encoding="utf-8")
        files[rel] = hashlib.sha256(rel.encode()).hexdigest()
    v1 = tmp_path / rn.FREEZE_MANIFESTS["nnf-stage0-v1"]
    v1.parent.mkdir(parents=True, exist_ok=True)
    v1.write_text(json.dumps({"status": "frozen", "version": "nnf-stage0-v1", "files": files}), encoding="utf-8")
    assert rn.verify_provenance(root=tmp_path)["freeze_version"] == "nnf-stage0-v1"
    v11 = tmp_path / rn.FREEZE_MANIFESTS["nnf-stage0-v1.1"]
    v11.write_text(json.dumps({"status": "frozen", "version": "nnf-stage0-v1.1", "files": files}), encoding="utf-8")
    assert rn.verify_provenance(root=tmp_path)["freeze_version"] == "nnf-stage0-v1.1"
    (tmp_path / rn.PROVENANCE_REQUIRED["burner"]).write_text("changed", encoding="utf-8")
    with pytest.raises(SystemExit, match="해시 불일치"):
        rn.verify_provenance(root=tmp_path)
    v11.write_text(json.dumps({"status": "candidate", "version": "nnf-stage0-v1.1", "files": files}), encoding="utf-8")
    with pytest.raises(SystemExit, match="형식 오류"):
        rn.verify_provenance(root=tmp_path)
