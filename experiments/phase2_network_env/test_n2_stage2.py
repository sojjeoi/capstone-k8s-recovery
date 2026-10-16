"""n2_stage2 시험(2026-10-17, M2, DEVELOPMENT). 클러스터 접근 없음 - 실제 guarded 정책 앱(act) + 가짜 클러스터로 Stage 2 흐름을 검증한다.
핵심: (1) CPU·net_probe·IF 어느 후보가 선택돼도 같은 복구 경로(승격 1회, 서비스 확인, 정책 identity)가 돈다, (2) 위험한 시작 상태는 기록 후 거부,
(3) 동결 run_bck가 계약에 적는 압박 단계가 네트워크 단계이고 끝나면 원래 값으로 복원된다, (4) 유효성은 원본 규칙 + 네트워크 전달 확인, (5) 원본 불변."""
import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import n2_common as nc  # noqa: E402
import n2_executor as ex  # noqa: E402
import n2_stage1 as ns1  # noqa: E402
import n2_stage2 as n2s2  # noqa: E402
from test_n2_executor import active_of, env  # noqa: E402,F401  (합성 외부 artifact + 동결 해시 교체 fixture)
from test_n2_session import ACTIVE_POD, CLEAN_POLICY, FakeNet, fast_stages, hooks_for, template, write_profile  # noqa: E402

pc = nc.pc
import guarded_live_detector_runner as gr  # noqa: E402
import run_guarded_pilot as gp  # noqa: E402
import run_stage2 as orig_s2  # noqa: E402  (Phase 2 동결 원본 - 읽기 전용 대조)
import test_run_guarded_pilot as tgp  # noqa: E402

ACT_POLICY = {**CLEAN_POLICY, "mode": "act"}
VALUE = {nc.CPU_ID: 3.0, nc.IF_ID: -0.5, nc.NET_ID: 1.0}


class NetActiveDetector(tgp.FakeDetector):
    """동결 FakeDetector 시계·송신 규칙 그대로 + payload는 실제 n2_executor.build_payload(ACTIVE rules)로 만든다(세 후보 판정 기록)."""

    def __init__(self, world, send_at, loaded, art):
        super().__init__(world, send_at)
        self.loaded, self.art, self.active = loaded, art, loaded["active"]

    def starter_factory(self, results_dir, policy_url, version, mode, arm=None):
        self.factory_args = (policy_url, version, mode, arm)

        def start(rid, path, sink_url):
            self.rid, self.path, self.alive = rid, gr.live_evidence_path(results_dir, rid), True
            ident = nc.signal_identity(self.active, self.art["hashes"])
            self.start_rec = ex._start_record("c1_live_start", rid, self.loaded, self.art, tgp._now, actuating_rule=self.active,
                                              signal_identity=ident, threshold=ident["threshold"])
            self._w(self.start_rec)
            self._w({"record_type": "c1_evaluation", "run_id": rid, "evaluation_seq": 0, "status": "scored", "score": 0.1})
            self.t0 = self.world.clock.t
            self.world.clock.listeners.append(self.tick)
            return tgp.DetectorHandle(is_alive=lambda: self.alive, stop=self.stop, label="n2-active-fake")
        return start

    def _send(self, payload):
        self._w({"record_type": "c1_policy_send_attempt", "run_id": self.rid, "evaluation_seq": self.seq,
                 "detector_id": payload["detector_id"], "attempted_at_utc": tgp._now().isoformat()})
        sr = gp.link.send("http://p/signal/v2", payload, post=self._post)
        dec = gp.ld.classify_send_result(sr)
        self._w({"record_type": "c1_policy_send_result", "run_id": self.rid, "evaluation_seq": self.seq, "decision": dec, **sr})
        self.post_action = self.post_action or dec == "post_action"

    def tick(self, t):
        if not self.alive or self.post_action or not self.send_at or t - self.t0 < self.send_at[0]:
            return
        self.send_at.pop(0)
        self.seq += 1
        d = tgp._now()
        rules = {x: {"value": VALUE[x], "threshold": nc.FROZEN_DETECTORS[x]["threshold"], "state": pc.ANOMALY,
                     "is_anomalous": True, "consecutive": 3, "condition_met": True, "condition_met_at_utc": d.isoformat(),
                     "role": nc.ACTIVE if x == self.active else nc.SHADOW} for x in nc.DETECTOR_IDS}
        rec = {"record_type": "c1_evaluation", "run_id": self.rid, "evaluation_seq": self.seq, "status": "scored",
               "score": -0.5, "window_start_utc": (d - timedelta(seconds=62)).isoformat(),
               "window_end_utc": (d - timedelta(seconds=2)).isoformat(), "rules": rules,
               "expected_pods": {p["name"]: p["uid"] for p in tgp.PODS},
               "pod_snapshot": {"active_selector": "aaa111", "preview_selector": "bbb222", "pods": tgp.PODS}}
        self._w(rec)
        self._send(ex.build_payload(self.start_rec, rec, self.active))


def stage2(tmp_path, monkeypatch, env, det, net_over=None, tpl=None):
    """act 모드 가짜 클러스터 + 설치된 Stage 2 인스턴스. 반환: (world, net, s1, s2, kwargs)."""
    fast_stages(monkeypatch, 0)
    _, root = env
    sel_path = write_profile(tmp_path, template())                       # 관측 환경에서 만든 selection profile(전부 SHADOW)
    ap = tmp_path / f"active-{det}.json"
    ap.write_text(json.dumps(active_of(sel_path, det)), encoding="utf-8")
    loaded, art = nc.load_profile(ap, stage="stage2"), pc.verify_artifacts(root)
    w = tgp.World(tmp_path / det, monkeypatch, mode=gp.MODE_ACT, send_at=(50.0,), wrap_preview=True)
    w.results = tmp_path / f"res-{det}"
    w.detector = NetActiveDetector(w, (50.0,), loaded, art)
    net = FakeNet(sel_path, tpl, **{"policy": dict(ACT_POLICY), **(net_over or {})})
    s1 = ns1.load_stage1(ns1.NetConfig(results_dir=tmp_path / "stage1"))
    s2 = n2s2.load_stage2(s1, n2s2.Net2Config(results_dir=w.results))
    n2s2.install(s1, s2, net, hooks=hooks_for(net))
    audit = tmp_path / det / "policy" / "audit.jsonl"
    entry = {"selected": det, "art": art, "loaded": loaded, "evidence_sha256": "e" * 64}
    kw = dict(deps=w.deps(), entry=entry, active_profile_path=ap, artifact_root=root, policy_url="http://p",
              audit_reader=lambda: [json.loads(x) for x in audit.read_text(encoding="utf-8").splitlines() if x],
              approval_path=w.approval(runs=["n2s01"]), results_dir=w.results, starter_factory=w.detector.starter_factory)
    return w, net, s1, s2, kw


# ---------- (1) 같은 복구 경로 ----------
@pytest.mark.parametrize("det", list(nc.DETECTOR_IDS))
def test_every_candidate_uses_the_same_unmodified_recovery_path(tmp_path, monkeypatch, env, det):
    import run_guarded_bck as gb
    import run_timed_switch_recovery_trial as rt
    before = (gb.RUNS, gb.BCK_PLAN_ID, gb.ARM_LABEL, gb.bd.signal_identity, rt.DEMO_STAGES)
    w, net, s1, s2, kw = stage2(tmp_path, monkeypatch, env, det)
    r = s2.run_session("n2s01", **kw)
    s, res = r["summary"], r["session_record"]["result"]
    assert len(w.promotes) == 1 and s["cleanup_errors"] == [], s["cleanup_errors"]
    assert s["context"]["detector_id"] == det and s["context"]["threshold"] == nc.FROZEN_DETECTORS[det]["threshold"]
    assert res["checks"]["promoted_verified"] and res["checks"]["traffic_verified"] and res["authority_leak"] is False
    assert [x["detector_id"] for x in s["policy_decisions"]] == [det] and res["success"] is True        # 정책 감사 기록 identity = 선택된 후보뿐
    assert (gb.RUNS, gb.BCK_PLAN_ID, gb.ARM_LABEL, gb.bd.signal_identity, rt.DEMO_STAGES) == before     # 동결 실행기 전역·압박 단계 상수 복원
    assert w.delay == 30 and w.policy.snapshot()["context"] is None
    c = json.loads(next(w.results.glob("contract-*.json")).read_text(encoding="utf-8"))
    assert c["conditions"]["stress_stages"] == list(nc.NETWORK_STAGES) and c["mode"] == "act"            # 계약에는 네트워크 단계가 적힌다
    assert s["cpu_raw_counter_samples"] == [] and s["stress_delivery_record"]["kind"] == "network_delay"   # CPU 카운터 자리는 비어 있음
    side = json.loads(n2s2.sidecar_path(w.results, s2.run_id_for("n2s01")).read_text(encoding="utf-8"))
    assert side["network_delivery_check"]["problems"] == [] and side["network_preflight"]["target"] == ACTIVE_POD
    assert r["network_delivery_check"]["problems"] == [] and not list(w.results.glob("running-*.json"))


# ---------- (2) 위험한 시작 상태는 기록 후 거부 ----------
@pytest.mark.parametrize("net_over,needle", [
    ({"chaos_listed": ["netdelay-old"]}, "network_chaos_present"),
    ({"policy": {**ACT_POLICY, "mode": "record_only"}}, "policy_mode"),                 # Stage 2는 act 모드 정책이 필요하다
    ({"policy": {**ACT_POLICY, "context": {"run_id": "r"}}}, "policy_context_set"),
])
def test_unsafe_start_is_refused_and_recorded_as_an_attempt_before_any_injection(tmp_path, monkeypatch, env, net_over, needle):
    w, net, s1, s2, kw = stage2(tmp_path, monkeypatch, env, nc.CPU_ID, net_over=net_over)
    with pytest.raises(SystemExit, match="진입 거부") as e:
        s2.run_session("n2s01", **kw)
    assert needle in str(e.value)
    rec = json.loads((w.results / "p2-s2-session-n2s01.json").read_text(encoding="utf-8"))
    val = json.loads((w.results / "p2-s2-validity-n2s01.json").read_text(encoding="utf-8"))
    assert rec["status"] == "refused_entry" and val["valid"] is False                  # 거부도 시도로 센다(상한 계산)
    assert w.injector.calls == [] and net.crs == {} and w.promotes == []


def test_preflight_only_changes_nothing(tmp_path, monkeypatch, env):
    w, net, s1, s2, kw = stage2(tmp_path, monkeypatch, env, nc.IF_ID)
    r = s2.run_session("n2s01", preflight_only=True, **kw)
    assert r["preflight_only"] is True and w.promotes == [] and net.crs == {}
    assert not list(w.results.glob("net-delivery-*")) and not list(w.results.glob("running-*"))


# ---------- (3) 계약의 압박 단계 ----------
def test_network_stage_contract_restores_the_constant_even_on_error():
    import run_timed_switch_recovery_trial as rt
    keep = rt.DEMO_STAGES
    with pytest.raises(RuntimeError):
        with n2s2.network_stage_contract():
            assert rt.DEMO_STAGES == [dict(s) for s in nc.NETWORK_STAGES] != keep
            raise RuntimeError("boom")
    assert rt.DEMO_STAGES is keep


# ---------- (5) 원본 불변·설정 ----------
def test_instance_has_network_ids_and_original_stage2_is_untouched(tmp_path):
    before = (dict(orig_s2.RUNS), orig_s2.RESULTS_DIR, orig_s2.EXECUTOR, orig_s2.STAGE2_PLAN_ID, orig_s2.entry_check, orig_s2.run_session,
              orig_s2.validate, orig_s2.s1, orig_s2.pc)
    s1 = ns1.load_stage1(ns1.NetConfig(results_dir=tmp_path / "s1"))
    s2 = n2s2.load_stage2(s1, n2s2.Net2Config(results_dir=tmp_path / "s2"))
    n2s2.install(s1, s2, object())
    after = (dict(orig_s2.RUNS), orig_s2.RESULTS_DIR, orig_s2.EXECUTOR, orig_s2.STAGE2_PLAN_ID, orig_s2.entry_check, orig_s2.run_session,
             orig_s2.validate, orig_s2.s1, orig_s2.pc)
    assert before == after and all(k.startswith("p2s") for k in orig_s2.RUNS)
    assert list(s2.RUNS)[:2] == ["n2s01", "n2s02"] and len(s2.RUNS) == pc.MAX_ATTEMPTS["stage2"]
    assert s2.run_id_for("n2s01") == "guarded_pressure_act-n2s01-capstone-phase2-network-env-v1-stage2"
    assert s2.EXECUTOR.name == "n2_executor.py" and s2.RESULTS_DIR.name == "s2" and s2.s1 is s1 and s2.pc is s1.pc
    with pytest.raises(SystemExit, match="알 수 없는"):
        s2.schedule("p2s01", tmp_path / "s2")                                      # Phase 2 실행 ID는 이 인스턴스에서 거부
    assert s2.schedule("n2s01", tmp_path / "s2")["attempt_number"] == 1


def test_original_run_stage2_source_hash_matches_phase2_implementation_freeze():
    m = nc.ROOT / pc.IMPLEMENTATION_FREEZE
    if not m.is_file():
        pytest.skip("Phase 2 implementation freeze manifest 없음")
    want = json.loads(m.read_text(encoding="utf-8"))["files"]["experiments/phase2_detector_selection/run_stage2.py"]
    assert pc.text_sha256(n2s2.STAGE2_SOURCE) == want


def test_entry_requires_a_selected_outcome_before_anything_else(tmp_path, env):
    s1 = ns1.load_stage1(ns1.NetConfig(results_dir=tmp_path / "s1"))
    s2 = n2s2.load_stage2(s1, n2s2.Net2Config(results_dir=tmp_path / "s2"))
    n2s2.install(s1, s2, object())
    ev = tmp_path / "ev.json"
    ev.write_text(json.dumps({"outcome": "NO_ELIGIBLE_DETECTOR"}), encoding="utf-8")
    with pytest.raises(SystemExit, match="SELECTED만 허용"):
        s2.entry_check(selection_profile_path=tmp_path / "x", active_profile_path=tmp_path / "y", evidence_path=ev,
                       artifact_root=tmp_path, snapshot_fn=lambda: {}, impl={})
    ev.write_text(json.dumps({"outcome": f"SELECTED:{nc.NET_ID}", "inputs": {}}), encoding="utf-8")     # 증거가 선택 규칙으로 재현되지 않으면 거부
    sel = write_profile(tmp_path, template())
    ap = tmp_path / "active.json"
    ap.write_text(json.dumps(active_of(sel, nc.NET_ID)), encoding="utf-8")
    with pytest.raises(SystemExit, match="selection_evidence_not_reproducible"):
        s2.entry_check(selection_profile_path=sel, active_profile_path=ap, evidence_path=ev, artifact_root=env[1],
                       snapshot_fn=lambda: {"template": template()}, impl={"selection_profile_sha256": "x"})


# ---------- (4) 유효성 ----------
def _validity_setup(tmp_path, monkeypatch, sidecar, base_reasons):
    import guarded_ab_analysis as gab
    s1 = ns1.load_stage1(ns1.NetConfig(results_dir=tmp_path))
    s2 = n2s2.load_stage2(s1, n2s2.Net2Config(results_dir=tmp_path))
    pc.write_json_new(tmp_path / "p2-s2-session-n2s01.json", {"run": "n2s01", "run_id": "rid", "selected": nc.NET_ID, "attempt_number": 1})
    if sidecar is not None:
        pc.write_json_new(n2s2.sidecar_path(tmp_path, "rid"), sidecar)
    seen = {}
    monkeypatch.setattr(s1, "pressure_run_dict", lambda d, run, pdir, rid=None, rep=None: ({"summary": {}, "run": run}, tmp_path / "x.csv"))
    monkeypatch.setattr(pc, "expected_if_hashes", lambda: {"h": 1})
    monkeypatch.setattr(gab, "primary", lambda dd, csv: {"computable": True})
    monkeypatch.setattr(gab, "run_validity", lambda dd, frozen, prim: seen.update(frozen=frozen) or {"reasons": list(base_reasons), "notes": {}})
    return s1, s2, seen


def test_validity_drops_cpu_v3_and_transition_dependent_v5_but_keeps_isolation_and_adds_network(tmp_path, monkeypatch):
    s1, s2, seen = _validity_setup(tmp_path, monkeypatch, {"network_delivery_check": {"problems": [], "container_records_present": True}},
                                   ["V3_stress_not_verified: None", "V5_unexplained_selector_change: a->b", "V5_old_policy_not_isolated",
                                    "V1_not_completed: aborted"])
    v = n2s2.validate_stage2_net(s1, s2, "n2s01", tmp_path, operator="none")
    assert v["invalid_reasons"] == ["V5_old_policy_not_isolated", "V1_not_completed: aborted"] and v["valid"] is False
    assert seen["frozen"]["threshold"] == nc.FROZEN_DETECTORS[nc.NET_ID]["threshold"]            # 선택된 후보의 임계값이 계약값
    assert seen["frozen"]["stress_stages"] == list(nc.NETWORK_STAGES)


def test_validity_is_valid_with_clean_network_evidence_and_invalid_without_or_with_problems(tmp_path, monkeypatch):
    s1, s2, _ = _validity_setup(tmp_path, monkeypatch, {"network_delivery_check": {"problems": [], "container_records_present": False}},
                                ["V3_stress_not_verified: None"])
    assert n2s2.validate_stage2_net(s1, s2, "n2s01", tmp_path, operator="none")["valid"] is True
    assert "operator_intervention: manual" in n2s2.validate_stage2_net(s1, s2, "n2s01", tmp_path, operator="manual")["invalid_reasons"]
    bad = tmp_path / "bad"
    bad.mkdir()
    s1b, s2b, _ = _validity_setup(bad, monkeypatch, {"network_delivery_check": {"problems": ["x: leak"], "container_records_present": True}}, [])
    assert n2s2.validate_stage2_net(s1b, s2b, "n2s01", bad, operator="none")["invalid_reasons"] == ["N3_x: leak"]
    none_ = tmp_path / "none"
    none_.mkdir()
    s1c, s2c, _ = _validity_setup(none_, monkeypatch, None, [])
    assert n2s2.validate_stage2_net(s1c, s2c, "n2s01", none_, operator="none")["invalid_reasons"] == ["N3_network_delivery_check_missing"]
