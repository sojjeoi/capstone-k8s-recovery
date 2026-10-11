"""Stage 0 I/O feasibility - 쓰기 속도 계획·분석 규칙(SLO·CPU guard·I/O 변화·무결성·선택·확증)·trial 흐름·배치 거부·provenance.
네트워크·클러스터 접속 없음."""
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest
import yaml

import guarded_cleanup_gate as gcg
import iof_stage0_analysis as ia
import run_guarded_pilot as gp
import run_iof_stage0 as ri
import test_run_guarded_pilot as tgp

sys.path.insert(0, str(ri.REPO_ROOT / "chaos" / "loadgen"))
import io_writer  # noqa: E402

T0 = datetime(2026, 10, 10, tzinfo=timezone.utc)
CFG = yaml.safe_load(ri.CONFIG.read_text(encoding="utf-8"))
TIMING = CFG["timing"]
ENV = {"min": 1.5, "max": 1.9}


def at(s):
    return T0 + timedelta(seconds=s)


# ---------------- 쓰기 계획·설정 ----------------
def test_writer_pacing_matches_target_rate():
    for rate in (2, 6, 18):
        offs = io_writer.due_offsets(rate, 300)
        assert abs(len(offs) * io_writer.BLOCK / 1e6 / 300 - rate) <= io_writer.BLOCK / 1e6 / 300
        assert offs == sorted(offs) and offs[0] == 0.0


def test_config_single_axis_ladder_and_confirm_unset():
    lad = CFG["io"]["development_ladder"]
    assert [x["target_write_mbps"] for x in lad] == [2, 6, 18] and CFG["io"]["confirmatory"] is None
    assert CFG["target"]["max_tokens"] == 10 and CFG["stages"][0]["rps"] == 0.025
    assert set(CFG["arrival"]["seeds"]) == set(ri.RUNS)


# ---------------- 합성 자료 ----------------
def counter(rate_fn, t0, t1, step):
    out, v = [], 0.0
    for t in range(t0, t1, step):
        out.append([at(t).timestamp(), v])
        v += rate_fn(t) * step
    return out


def metrics(cpu=lambda t: 1.7, psi=lambda t: 0.01, gap_at=None):
    s = {"cpu": counter(cpu, 0, 3000, 10), "gen_tokens": counter(lambda t: 1.0, 0, 3000, 15)}
    for k in ia.NODE_KEYS:
        s[k] = counter(psi if k == "psi_waiting" else (lambda t: 0.1), 0, 3000, 30)
    if gap_at:
        s["cpu"] = [p for p in s["cpu"] if not (gap_at[0] <= p[0] - T0.timestamp() <= gap_at[1])]
    return {"series": s}


def probe_rows(slow=None, until=3000):
    return [{"sent_at": at(i), "latency": 3.0 if slow and slow[0] <= i <= slow[1] else 0.25, "success": True}
            for i in range(-10, until)]


def stage(name, s, role=None, rc=0, ran=300):
    return {"name": name, "role": role, "target_write_mbps": 6, "start_utc": at(s).isoformat(),
            "end_utc": at(s + 300).isoformat(), "writer": {"rc": rc, "ran_sec": ran}}


def ev(st, m, rows, env=ENV):
    return ia.stage_eval(st, rows=rows, obs0=at(0), metrics=m, envelope=env, pre_window=(at(0), at(300)), timing=TIMING)


# ---------------- stage_eval ----------------
def test_weak_stage_io_change_slo_normal_cpu_in_envelope():
    x = ev(stage("I1", 300), metrics(psi=lambda t: 0.2 if 300 <= t < 600 else 0.01), probe_rows())
    assert x["slo_normal"] and x["io_change"] and x["cpu_guard_pass"] and x["writer_ok"] and x["integrity"]["ok"]
    assert ia.weak_ok(x) and not ia.strong_ok(x)


def test_strong_stage_slo_violation_with_cpu_in_envelope():
    x = ev(stage("I3", 300), metrics(psi=lambda t: 0.5 if 300 <= t < 600 else 0.01), probe_rows(slow=(400, 520)))
    assert not x["slo_normal"] and x["t_slo"] and x["cpu_guard_pass"] and ia.strong_ok(x)


def test_cpu_guard_two_sided_until_t_slo():
    hi = ev(stage("I3", 300), metrics(cpu=lambda t: 2.4 if t >= 300 else 1.7), probe_rows())
    lo = ev(stage("I3", 300), metrics(cpu=lambda t: 1.0 if t >= 300 else 1.7), probe_rows())
    assert not hi["cpu_guard_pass"] and hi["cpu_out_high"] and not lo["cpu_guard_pass"] and lo["cpu_out_low"]
    late = ev(stage("I3", 300), metrics(cpu=lambda t: 2.4 if t >= 560 else 1.7), probe_rows(slow=(400, 520)))
    assert late["cpu_guard_pass"]   # t_slo 뒤 CPU 변화는 guard 대상 아님


def test_no_io_change_when_psi_flat_and_writer_not_ok():
    x = ev(stage("I1", 300, rc=3, ran=40), metrics(), probe_rows())
    assert not x["io_change"] and not x["writer_ok"] and not ia.weak_ok(x)


def test_integrity_gap_detected():
    x = ev(stage("I1", 300), metrics(gap_at=(350, 420)), probe_rows())
    assert not x["integrity"]["ok"] and any(p.startswith("cpu_gap") for p in x["integrity"]["problems"])


# ---------------- 선택·확증 ----------------
def srec(name, weak=False, strong=False):
    return {"name": name, "computable": True, "writer_ok": True, "io_change": weak or strong, "slo_normal": not strong,
            "cpu_guard_pass": True, "integrity": {"ok": True}}


def dev_rep(spec, valid=True):
    return {"run": "d", "valid": valid, "stages": [srec(n, *spec.get(n, (False, False))) for n in ("I1", "I2", "I3")]}


LAD = CFG["io"]["development_ladder"]


def test_dev_selection_weak_highest_strong_lowest_above_weak():
    spec = {"I1": (True, False), "I2": (True, False), "I3": (False, True)}
    s = ia.select_dev([dev_rep(spec), dev_rep(spec)], LAD)
    assert (s["weak"], s["strong"]) == ("I2", "I3")


def test_dev_selection_requires_every_valid_rep_and_two_reps():
    a = dev_rep({"I1": (True, False), "I3": (False, True)})
    b = dev_rep({"I1": (True, False), "I3": (False, False)})
    s = ia.select_dev([a, b, dev_rep({}, valid=False)], LAD)
    assert s["weak"] == "I1" and s["strong"] is None and s["reason"] == "weak_or_strong_missing"
    assert ia.select_dev([a], LAD)["reason"].startswith("valid_dev_reps")


def conf_rep(strong_slo_normal=False, weak_io=True, cpu_ok=True, integ=True, valid=True):
    w = {**srec("W", weak=True), "role": "weak", "io_change": weak_io}
    s = {**srec("S", strong=True), "role": "strong", "slo_normal": strong_slo_normal, "cpu_guard_pass": cpu_ok}
    return {"run": "c", "valid": valid, "stages": [w, s], "integrity": {"ok": integ}}


def test_confirm_verdict_all_four_conditions():
    assert ia.confirm_verdict([conf_rep(), conf_rep()])["feasible"] is True
    v = ia.confirm_verdict([conf_rep(), conf_rep(strong_slo_normal=True)])
    assert v["feasible"] is False and v["conditions"]["1_strong_slo_degradation_2of2"] is False
    assert ia.confirm_verdict([conf_rep(), conf_rep(cpu_ok=False)])["conditions"]["2_strong_cpu_guard_2of2"] is False
    assert ia.confirm_verdict([conf_rep(), conf_rep(weak_io=False)])["feasible"] is False
    assert ia.confirm_verdict([conf_rep(), conf_rep(integ=False)])["conditions"]["4_measurement_integrity"] is False
    assert ia.confirm_verdict([conf_rep(), conf_rep(valid=False)])["reason"].startswith("valid_confirm_reps")


def test_analyze_session_invalid_reasons_are_not_outcome_based():
    s = {"run": "iod90", "kind": "development", "outcome_status": "completed", "cleanup_errors": [],
         "observation_start_utc": at(0).isoformat(), "target_pod": {"node": "sj-worker2"}, "prometheus_node": "sj-worker2",
         "target_uid_constant": True}
    assert "prometheus_on_target_node" in ia.analyze_session(s, metrics(), ENV, TIMING)["invalid_reasons"]
    r = ia.analyze_session({**s, "prometheus_node": "sj-worker", "target_uid_constant": False}, metrics(), ENV, TIMING)
    assert "target_identity_changed" in r["invalid_reasons"]


# ---------------- trial ----------------
class Clock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, s):
        self.t += s

    def mono(self):
        return self.t


class FakeRamp:
    def __init__(self):
        self.calls = []

    def prepare(self):
        self.calls.append("prepare")

    def start(self, i):
        self.calls.append(f"start{i}")

    def cleanup(self):
        self.calls.append("cleanup")


class FakeWriter:
    def __init__(self, clock, rc=0):
        self.c, self.rc, self.calls, self.t0, self.info = clock, rc, [], {}, {"pod": "w"}

    def prepare(self):
        self.calls.append("prepare")

    def start(self, name, rate, dur):
        self.calls.append(f"start:{name}:{rate}")
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


def run_trial(kind, plan, *, slow=None, safety=None, ready_after=0, rc=0):
    c = Clock()
    ramp, writer = FakeRamp(), (FakeWriter(c, rc) if plan else None)
    o = ri.trial(kind, plan, TIMING, ramp=ramp, writer=writer, prober=tgp.FakeProber(),
                 safety=safety(c) if safety else (lambda s, p: None), probe_rows=rows_until(c, slow),
                 readiness_fn=lambda t: [] if c.t >= ready_after else ["scrape_incomplete"],
                 disk_ok_fn=lambda: True, sleep=c.sleep, monotonic=c.mono, now=lambda: at(c.t))
    return o, ramp, writer


DEV = ri.plan_for("development", CFG)


def test_trial_development_stops_after_slo_boundary_stage():
    o, ramp, writer = run_trial("development", DEV, slow=(1000, 1100))
    assert o["status"] == "completed" and o["slo_boundary_stage"] == "I2" and o["stages_not_run"] == ["I3"]
    assert [x for x in writer.calls if x.startswith("start")] == ["start:I1:2", "start:I2:6"]
    assert writer.calls[-1] == "cleanup" and ramp.calls[-1] == "cleanup" and o["cleanup_status"] == "complete"


def test_trial_confirmatory_runs_weak_then_strong():
    cfg = {**CFG, "io": {**CFG["io"], "confirmatory": {"weak": {"name": "I1", "target_write_mbps": 2},
                                                         "strong": {"name": "I3", "target_write_mbps": 18}}}}
    o, _, writer = run_trial("confirmatory", ri.plan_for("confirmatory", cfg))
    assert o["status"] == "completed" and [x["role"] for x in o["stress_stages"]] == ["weak", "strong"]
    assert [x for x in writer.calls if x.startswith("start")] == ["start:I1:2", "start:I3:18"]


def test_trial_readiness_timeout_is_invalid_and_cleans_up():
    o, ramp, writer = run_trial("development", DEV, ready_after=10_000)
    assert o["status"] == "aborted" and "준비 조건 상한" in o["reason"] and writer.calls[-1] == "cleanup"
    assert not o["observation_start_utc"]


def test_trial_safety_abort_stops_writer_and_cleans_up():
    def mk(c):
        def safety(s, p):
            if c.t > 600:
                raise ri.HarnessCorrupted("node NotReady")
        return safety
    o, ramp, writer = run_trial("development", DEV, safety=mk)
    assert o["status"] == "aborted" and "NotReady" in o["reason"] and writer.calls[-2:] == ["stop", "cleanup"]


def test_trial_writer_failure_is_invalid():
    o, _, writer = run_trial("development", DEV, rc=3)
    assert o["status"] == "aborted" and "rc=3" in o["reason"] and writer.calls[-1] == "cleanup"


def test_trial_baseline_has_no_writer():
    o, _, writer = run_trial("baseline", [])
    assert o["status"] == "completed" and writer is None and o["observation_end_utc"]


def test_readiness_check_ignores_session_pods_but_not_canonical_problems(monkeypatch):
    snap = {"pods": [{"uid": "u1"}]}
    monkeypatch.setattr(gcg, "check_canonical", lambda s, delay: ["residual: probe=['ramp-probe-x']"])
    assert ri.readiness_check("u1", "p", lambda: snap, lambda sel, a, b: [0] * 12, at(0)) == []
    monkeypatch.setattr(gcg, "check_canonical", lambda s, delay: ["phase=Degraded"])
    assert ri.readiness_check("u1", "p", lambda: snap, lambda sel, a, b: [0] * 12, at(0))[0].startswith("canonical")
    monkeypatch.setattr(gcg, "check_canonical", lambda s, delay: [])
    p = ri.readiness_check("u2", "p", lambda: snap, lambda sel, a, b: [0] * 2, at(0))
    assert "target_uid_changed" in p and any(x.startswith("scrape_incomplete:cpu") for x in p)


# ---------------- run_session(동결 World) ----------------
def _run(w, run, monkeypatch, node="sj-worker2", prom_node="sj-worker"):
    monkeypatch.setattr(ri.wa, "stage_slo_verdict",
                        lambda rows, a0, s, e: {"computable": True, "reason": None, "violating": False, "episodes": []})
    probe = w.results.parent / "probe-fake.csv"   # 관찰 끝 뒤까지 응답이 확인된 probe 행(실시각 기준 미래 1행)
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    probe.write_text(f"sent_at,latency,success\n{future},0.25,True\n", encoding="utf-8")
    return ri.run_session(run, deps=w.deps(probe_csv_path=lambda rid, rep: probe), results_dir=w.results,
                          make_ramp=lambda *a: FakeRamp(),
                          make_writer=lambda *a: FakeWriter(w.clock), prom_raw_fn=lambda sel, a, b: [[a.timestamp(), 0.0]] * 12,
                          node_of_fn=lambda ns, pod: None if pod == "w" else node, prometheus_node_fn=lambda: prom_node,
                          disk_avail_fn=lambda: 100e9)


def test_run_session_baseline_end_to_end_no_promotion(tmp_path, monkeypatch):
    w = tgp.World(tmp_path, monkeypatch, mode=gp.MODE_RO, send_at=())
    s = _run(w, "iob90", monkeypatch)
    assert s["kind"] == "baseline" and s["outcome_status"] == "completed" and s["cleanup_errors"] == []
    assert w.promotes == [] and w.overrides == [] and w.policy.snapshot()["context"] is None
    assert (w.results / f"metrics-{s['run_id']}.json").exists() and s["target_pod"]["node"] == "sj-worker2"


def test_run_session_placement_and_schedule_refusals(tmp_path, monkeypatch):
    w = tgp.World(tmp_path, monkeypatch, mode=gp.MODE_RO, send_at=())
    with pytest.raises(SystemExit, match="순서 밖"):
        _run(w, "iod90", monkeypatch)
    with pytest.raises(SystemExit, match="placement"):
        _run(w, "iob90", monkeypatch, node="sj-worker")
    w2 = tgp.World(tmp_path / "b", monkeypatch, mode=gp.MODE_RO, send_at=())
    with pytest.raises(SystemExit, match="placement"):
        _run(w2, "iob90", monkeypatch, prom_node="sj-worker2")


def test_confirmatory_refused_without_amendment_and_id_reuse(tmp_path, monkeypatch):
    w = tgp.World(tmp_path, monkeypatch, mode=gp.MODE_RO, send_at=())
    _run(w, "iob90", monkeypatch)
    _run(w, "iod90", monkeypatch)
    with pytest.raises(SystemExit, match="amendment"):
        _run(w, "iof90", monkeypatch)
    with pytest.raises(SystemExit, match="재사용 금지"):
        _run(w, "iod90", monkeypatch)


def test_provenance_fail_closed(tmp_path):
    files = {}
    for rel in ri.PROVENANCE_REQUIRED.values():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(rel, encoding="utf-8")
        files[rel] = hashlib.sha256(rel.encode()).hexdigest()
    mp = tmp_path / ri.FREEZE_MANIFEST
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps({"status": "frozen", "version": ri.FREEZE_VERSION, "files": files}), encoding="utf-8")
    assert ri.verify_provenance(root=tmp_path)["verification_status"] == "verified"
    (tmp_path / ri.PROVENANCE_REQUIRED["writer"]).write_text("changed", encoding="utf-8")
    with pytest.raises(SystemExit, match="해시 불일치"):
        ri.verify_provenance(root=tmp_path)
    mp.write_text(json.dumps({"status": "candidate", "version": ri.FREEZE_VERSION, "files": files}), encoding="utf-8")
    with pytest.raises(SystemExit, match="형식 오류"):
        ri.verify_provenance(root=tmp_path)


def test_cpu_guard_has_windows_even_for_fast_violation():
    x = ev(stage("I3", 300), metrics(psi=lambda t: 0.5 if t >= 300 else 0.01), probe_rows(slow=(305, 420)))
    assert not x["slo_normal"] and x["cpu_windows"] >= 1 and x["cpu_guard_pass"]


def test_amendment_manifest_takes_precedence(tmp_path):
    files = {}
    for rel in ri.PROVENANCE_REQUIRED.values():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(rel, encoding="utf-8")
        files[rel] = hashlib.sha256(rel.encode()).hexdigest()
    for ver, path in ri.FREEZE_MANIFESTS.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(json.dumps({"status": "frozen", "version": ver, "files": files}), encoding="utf-8")
    assert ri.verify_provenance(root=tmp_path)["freeze_version"] == "iof-stage0-v1.1"
    (tmp_path / ri.FREEZE_MANIFESTS["iof-stage0-v1.1"]).write_text(json.dumps({"status": "frozen", "version": "iof-stage0-v1", "files": files}), encoding="utf-8")
    with pytest.raises(SystemExit, match="형식 오류"):
        ri.verify_provenance(root=tmp_path)   # v1.1 파일에 다른 버전 기록 = 거부


def test_trial_collects_probe_evidence_to_observation_end_even_without_stress():
    """iob90 결함 회귀: 스트레스 단계가 없는 기준선도 관찰 끝 뒤 probe 원자료를 회수해야 한다(회수 못 하면 aborted)."""
    c = Clock()
    calls = []

    def rows():
        calls.append(c.t)
        return rows_until(c)()
    o = ri.trial("baseline", [], TIMING, ramp=FakeRamp(), writer=None, prober=tgp.FakeProber(), safety=lambda s, p: None,
                 probe_rows=rows, readiness_fn=lambda t: [], disk_ok_fn=lambda: True, sleep=c.sleep, monotonic=c.mono,
                 now=lambda: at(c.t))
    end = (ri.wa._ts(o["observation_end_utc"]) - T0).total_seconds()
    assert o["status"] == "completed" and calls and max(calls) > end
    c2 = Clock()
    o2 = ri.trial("baseline", [], TIMING, ramp=FakeRamp(), writer=None, prober=tgp.FakeProber(), safety=lambda s, p: None,
                  probe_rows=lambda: [], readiness_fn=lambda t: [], disk_ok_fn=lambda: True, sleep=c2.sleep,
                  monotonic=c2.mono, now=lambda: at(c2.t))
    assert o2["status"] == "aborted" and "probe 증거 미회수" in o2["reason"]
