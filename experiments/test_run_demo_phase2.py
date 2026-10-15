"""run_demo_phase2(시연 전용 Phase 2 실행기) 시험 - 클러스터 접근 없음(conftest cluster_guard).
실행: 저장소 루트에서 python -m pytest experiments/test_run_demo_phase2.py -q -p no:cacheprovider"""
import json
import os
import time

import pytest

import run_demo_phase2 as dm
import p2_common as pc
import run_guarded_bck as gb
import test_run_guarded_pilot as tgp
import run_guarded_pilot as gp
from test_phase2_detector_selection import P2ActiveDetector, _active, _write_profile, art_root, profile  # noqa: F401

RID = dm.run_id_for("p2demo01")
TK = "vllm-serving/vllm-serving@ru1:aaa111->bbb222#up"


def _send_result(outcome="promoted_verified", det=pc.CPU_ID, tk=TK):
    return json.dumps({"record_type": "c1_policy_send_result", "policy_outcome": outcome, "detector_id": det,
                       "target_key": tk, "completed_at_utc": "2026-10-12T00:00:00+00:00"})


def _view(active="bbb222", svc=("bbb222", "bbb222"), ready=True, uid="up", terminating=False):
    return {"active_selector": active, "service_before": svc[0], "service_after": svc[1],
            "pods": [{"hash": "aaa111", "uid": "ua", "ready": True, "terminating": False},
                     {"hash": "bbb222", "uid": uid, "ready": ready, "terminating": terminating}]}


class Clock:
    def __init__(self):
        self.t = 0.0

    def mono(self):
        return self.t

    def sleep(self, s):
        self.t += s


# ---------------- 시작 신호 ----------------
def test_trigger_waits_for_fresh_flag_and_ignores_stale(tmp_path):
    flag, c = tmp_path / "demo-go-p2demo01.flag", Clock()
    flag.write_text("old")
    os.utime(flag, (time.time() - 3600,) * 2)                     # 지난 실행의 잔재
    armed = time.time()

    def sleep(s):
        c.sleep(s)
        if c.t >= 10:
            flag.write_text("go")
    assert dm.wait_for_trigger(flag, armed, max_wait_sec=60, sleep=sleep, monotonic=c.mono) >= armed - dm.MTIME_SLACK_SEC
    assert 10 <= c.t < 60


def test_trigger_timeout_means_no_pressure(tmp_path):
    c = Clock()
    with pytest.raises(dm.DemoTriggerTimeout):
        dm.wait_for_trigger(tmp_path / "none.flag", time.time(), max_wait_sec=30, sleep=c.sleep, monotonic=c.mono)


# ---------------- 전환 조건 ----------------
def test_target_from_evidence_only_active_promoted_verified():
    lines = ["not json", _send_result("rejected"), _send_result(det=pc.IF_ID), _send_result(det=None), _send_result()]
    t = dm.target_from_evidence(lines, pc.CPU_ID)
    assert (t["old"], t["new"], t["new_uid"]) == ("aaa111", "bbb222", "up")
    assert dm.target_from_evidence(lines[:4], pc.CPU_ID) is None     # SHADOW·거부·식별 없음 결과로는 정리하지 않는다


@pytest.mark.parametrize("view,reason", [
    (_view(active="aaa111"), "rollout_active_selector=aaa111"),
    (_view(svc=("aaa111", "aaa111")), "service_selector=aaa111"),
    (_view(svc=("aaa111", "bbb222")), "service_selector_changed_during_read"),
    (_view(ready=False), "new_pod_not_ready"),
    (_view(terminating=True), "new_pod_not_ready"),
    (_view(uid="other"), "new_pod_not_ready"),
])
def test_switch_ready_requires_every_condition(view, reason):
    t = dm.target_from_evidence([_send_result()], pc.CPU_ID)
    ok, why = dm.switch_ready(view, t)
    assert not ok and reason in why
    assert dm.switch_ready(_view(), t) == (True, [])
    same = {**t, "old": "bbb222"}
    assert "old_hash_equals_new_hash" in dm.switch_ready(_view(), same)[1]


# ---------------- 정리 감시 ----------------
class Inner:
    def __init__(self, chaos, names=("c-0", "c-1")):
        self.chaos, self.names, self.cleaned = chaos, list(names), 0

    def get_stress_delivery_record(self):
        return {"planned_cr_names": list(self.names)}

    def cleanup(self):
        self.cleaned += 1
        self.chaos[:] = [c for c in self.chaos if c["name"] not in self.names]


def _chaos(ann=RID):
    return [{"name": "c-0", "uid": "u0", "annotation": ann}, {"name": "c-1", "uid": "u1", "annotation": ann},
            {"name": "someone-else", "uid": "z", "annotation": "other-run"}]


def _watcher(tmp_path, inner, chaos, views, lines, watch_sec=100):
    ev = tmp_path / "ev.jsonl"
    c = Clock()
    seq = iter(views)

    def sleep(s):
        c.sleep(s)
        if lines:
            ev.write_text("".join(x + "\n" for x in lines.pop(0)), encoding="utf-8")
    w = dm.StressCleanupWatcher(run_id=RID, active_id=pc.CPU_ID, evidence_path=ev, cleanup_fn=inner.cleanup,
                                delivery_fn=inner.get_stress_delivery_record, read_view=lambda: next(seq), list_chaos=lambda: [dict(x) for x in chaos],
                                record_path=tmp_path / "rec.json", sleep=sleep, monotonic=c.mono, watch_sec=watch_sec)
    w.run()
    return json.loads((tmp_path / "rec.json").read_text(encoding="utf-8"))


def test_watcher_cleans_only_after_all_conditions_and_only_this_run(tmp_path):
    chaos = _chaos()
    inner = Inner(chaos)
    # 1회차: 아직 정책 결과 없음 → 2회차: 결과는 있으나 서비스 미전환 → 3회차: 전부 충족
    rec = _watcher(tmp_path, inner, chaos, [_view(svc=("aaa111", "aaa111")), _view()], [[_send_result()]])
    assert rec["status"] == "cleaned" and inner.cleaned == 1 and rec["left"] == []
    assert {d["name"] for d in rec["deleted"]} == {"c-0", "c-1"}
    assert [c["name"] for c in chaos] == ["someone-else"]             # 다른 실행 CR은 그대로
    assert rec["counted"] is False and "Recovery Policy 기능 아님" in rec["meaning"]


def test_watcher_refuses_on_annotation_mismatch(tmp_path):
    chaos = _chaos(ann="another-run-id")
    inner = Inner(chaos)
    rec = _watcher(tmp_path, inner, chaos, [_view()], [[_send_result()]])
    assert rec["status"] == "failed" and rec["error"] == "annotation_mismatch" and inner.cleaned == 0


def test_watcher_reports_left_crs_separately(tmp_path):
    chaos = _chaos()
    inner = Inner(chaos)
    inner.cleanup = lambda: None                                      # 삭제가 반영되지 않음
    rec = _watcher(tmp_path, inner, chaos, [_view()], [[_send_result()]])
    assert rec["status"] == "failed" and rec["error"] == "stress_chaos_left_after_cleanup" and rec["left"] == ["c-0", "c-1"]


def test_watcher_never_cleans_without_promotion_and_survives_read_errors(tmp_path):
    chaos = _chaos()
    inner = Inner(chaos)

    def bad():
        raise RuntimeError("api down")
    ev = tmp_path / "ev.jsonl"
    ev.write_text(_send_result() + "\n", encoding="utf-8")
    c = Clock()
    w = dm.StressCleanupWatcher(run_id=RID, active_id=pc.CPU_ID, evidence_path=ev, cleanup_fn=inner.cleanup,
                                delivery_fn=inner.get_stress_delivery_record, read_view=bad,
                                list_chaos=lambda: chaos, record_path=tmp_path / "r.json", sleep=c.sleep,
                                monotonic=c.mono, watch_sec=20)
    w.run()
    rec = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert rec["status"] == "not_triggered" and inner.cleaned == 0 and rec["last_reasons"][0].startswith("view_read_failed")
    (tmp_path / "b").mkdir()
    rec2 = _watcher(tmp_path / "b", inner, chaos, [], [], watch_sec=10)     # 정책 결과 없음
    assert rec2["status"] == "not_triggered" and rec2["last_reasons"] == ["no_promoted_verified_active_signal"]


# ---------------- 세션(동결 실행기 연결, 가짜 클러스터) ----------------
def _demo_world(tmp_path, monkeypatch, profile_path, root, det=pc.CPU_ID, send_at=(50.0,)):
    ap = _write_profile(tmp_path, _active(profile_path, det), "active.json")
    loaded, art = pc.load_profile(ap, stage="stage2"), pc.verify_artifacts(root)
    w = tgp.World(tmp_path / "w", monkeypatch, mode=gp.MODE_ACT, send_at=send_at, wrap_preview=True)
    w.results = tmp_path / "demo"
    w.detector = fake = P2ActiveDetector(w, send_at, loaded, art)
    base_w = fake._w  # 실제 p2_executor처럼 정책 결과 기록에도 detector_id를 남긴다(공유 fake는 시도 기록에만 남김)
    fake._w = lambda rec: base_w({**rec, "detector_id": fake.active} if rec.get("record_type") == "c1_policy_send_result" else rec)
    entry = {"selected": det, "art": art, "loaded": loaded, "evidence_sha256": "e" * 64}
    return w, ap, entry


def _parts(w):
    return (lambda rid, rep: w.injector, lambda inner, cpf: w.make_injector(None, None, "pressure", cpf))


def test_demo_session_waits_for_trigger_then_runs_frozen_path_and_cleans(tmp_path, monkeypatch, profile, art_root):
    w, ap, entry = _demo_world(tmp_path, monkeypatch, profile, art_root)
    chaos = [{"name": "c-0", "uid": "u0", "annotation": RID}]
    w.injector.get_stress_delivery_record = lambda: {"planned_cr_names": ["c-0"], "target": {"name": "vllm-serving-aaa111-x"}}
    w.injector.cleanup = lambda: (w.injector.calls.append("pressure_cleanup"), chaos.clear())
    calls_at_go, ran, gate = [], [], []   # gate = preview 정리 관문 호출 시점(len(ran)==1이면 감시 도중)
    ev_path = dm.gr.live_evidence_path(w.results, RID)

    def press_enter(t):                                               # 발표자가 start-pressure.ps1을 누르는 순간
        if t > 30 and not calls_at_go and dm.state_path(w.results, RID).exists():
            calls_at_go.append(list(w.injector.calls))
            dm.trigger_path(w.results, "p2demo01").write_text("go")
        # 승격 결과가 기록된 직후(시행 도중, 실행기 정리 전)에 정리 감시를 돌린다
        if pending and not ran and dm.target_from_evidence(dm._read_lines(ev_path), pc.CPU_ID):
            ran.append(list(w.injector.calls))
            pending[0]()
            ran.append(list(w.injector.calls))
    w.clock.listeners.append(press_enter)
    pending, before = [], (gb.RUNS, gb.BCK_PLAN_ID, gb.ARM_LABEL, gb.BCK_RESULTS_DIR, gb.bd.signal_identity)
    probes = []
    base_prober = w.deps().make_prober
    audit = tmp_path / "w" / "policy" / "audit.jsonl"
    r = dm.run_demo_session(
        "p2demo01", deps=w.deps(make_prober=lambda *a: (probes.append(a), base_prober(*a))[1]), entry=entry,
        active_profile_path=ap, artifact_root=art_root,
        approval_path=w.approval(runs=["p2demo01"], demo_only=True, counted=False),
        policy_url="http://p", results_dir=w.results, starter_factory=w.detector.starter_factory,
        injector_parts=(lambda rid, rep: w.injector,
                        lambda inner, cpf: w.make_injector(None, None, "pressure", lambda *a: (gate.append(len(ran)), cpf(*a))[1])),
        read_view=lambda: _view(), list_chaos=lambda: [dict(x) for x in chaos],
        max_wait_sec=600, start_thread=pending.append,
        audit_reader=lambda: [json.loads(x) for x in audit.read_text(encoding="utf-8").splitlines() if x])
    s, rec = r["summary"], r["session_record"]
    assert calls_at_go and "inject" not in calls_at_go[0]            # 시작 신호 전에는 압박 없음
    assert w.injector.calls.count("inject") == 1 and len(w.promotes) == 1 and s["cleanup_errors"] == []
    assert rec["demo_only"] is True and rec["counted"] is False and rec["result"]["success"] is True, rec["result"]
    assert s["context"]["detector_id"] == pc.CPU_ID                  # ACTIVE = Active Profile
    assert probes[0][3] >= 600                                        # 대기만큼 요청 발생기 수명 연장
    assert (gb.RUNS, gb.BCK_PLAN_ID, gb.ARM_LABEL, gb.BCK_RESULTS_DIR, gb.bd.signal_identity) == before
    assert json.loads((w.results / f"demo-tag-{RID}.json").read_text(encoding="utf-8"))["counted"] is False
    assert len(pending) == 1 and ran                                  # 정리 감시는 압박 시작 뒤 1개, 시행 도중 실행
    assert "pressure_cleanup" not in ran[0] and ran[1][-1] == "pressure_cleanup"  # 감시는 압박 정리만 호출
    assert 1 not in gate and gate == [2]                              # preview 정리 관문은 실행기 종료 때 한 번만
    cl = json.loads(dm.cleanup_record_path(w.results, RID).read_text(encoding="utf-8"))
    assert cl["status"] == "cleaned" and chaos == [] and cl["target"]["new"] == "bbb222"
    assert rec["demo_stress_cleanup"]["status"] == "cleaned"          # 세션 기록 전에 확정된 별도 기록
    assert json.loads(dm.state_path(w.results, RID).read_text(encoding="utf-8"))["phase"] == "session_ended"


def test_demo_session_trigger_timeout_injects_nothing(tmp_path, monkeypatch, profile, art_root):
    w, ap, entry = _demo_world(tmp_path, monkeypatch, profile, art_root, send_at=())  # 압박 없음 → 신호 없음
    pending = []
    r = dm.run_demo_session(
        "p2demo02", deps=w.deps(), entry=entry, active_profile_path=ap, artifact_root=art_root,
        approval_path=w.approval(runs=["p2demo02"], demo_only=True, counted=False), policy_url="http://p",
        results_dir=w.results, starter_factory=w.detector.starter_factory, injector_parts=_parts(w),
        read_view=lambda: _view(), list_chaos=lambda: [], max_wait_sec=60, start_thread=pending.append)
    assert "inject" not in w.injector.calls and w.promotes == [] and pending == []
    assert "DemoTriggerTimeout" in json.dumps(r["summary"], ensure_ascii=False, default=str)
    st = json.loads(dm.state_path(w.results, dm.run_id_for("p2demo02")).read_text(encoding="utf-8"))
    assert (st["phase"], st["previous_phase"]) == ("session_ended", "trigger_timeout")


def test_demo_session_refusals(tmp_path):
    entry = {"selected": pc.CPU_ID, "art": {"hashes": {}}, "loaded": {"sha256": "x"}, "evidence_sha256": "e"}
    kw = dict(deps=None, entry=entry, active_profile_path=None, artifact_root=None, policy_url="http://p", results_dir=tmp_path)
    ap = tmp_path / "a.json"
    with pytest.raises(SystemExit, match="demo 실행 ID가 아님"):
        dm.run_demo_session("p2s01", approval_path=ap, **kw)                       # 공식 Stage 2 ID 사용 불가
    ap.write_text(json.dumps({"runs": ["p2demo01"]}))
    with pytest.raises(SystemExit, match="demo 승인 기록 아님"):
        dm.run_demo_session("p2demo01", approval_path=ap, **kw)                    # 공식 승인 기록 사용 불가
    ap.write_text(json.dumps({"runs": ["p2demo01"], "demo_only": True, "counted": False}))
    dm.session_path(tmp_path, "p2demo01").write_text("{}")
    with pytest.raises(SystemExit, match="이미 쓴"):
        dm.run_demo_session("p2demo01", approval_path=ap, **kw)


def test_demo_ids_and_dir_are_separate_from_official():
    import run_stage2 as s2
    assert not set(dm.RUNS) & set(s2.RUNS) and dm.DEMO_PLAN_ID != s2.STAGE2_PLAN_ID
    assert dm.DEMO_DIR != s2.RESULTS_DIR and dm.DEMO_DIR.name == "demo_only_phase2"
    assert {r[2] for r in dm.RUNS.values()}.isdisjoint({r[2] for r in s2.RUNS.values()})


def test_merged_stage1_dir_copies_both_and_leaves_originals(tmp_path):
    raw, repo = tmp_path / "raw", tmp_path / "repo"
    raw.mkdir(); repo.mkdir()
    (raw / "c1-live-evidence-x.jsonl").write_text("raw")
    (repo / "p2-session-x.json").write_text("{}")
    (raw / "same.json").write_text("raw-version"); (repo / "same.json").write_text("repo-version")
    out = dm.merged_stage1_dir(raw, repo, into=tmp_path / "merged")
    assert sorted(f.name for f in out.iterdir()) == ["c1-live-evidence-x.jsonl", "p2-session-x.json", "same.json"]
    assert (out / "same.json").read_text() == "repo-version"                    # 같은 이름은 저장소(커밋) 쪽
    assert sorted(f.name for f in raw.iterdir()) == ["c1-live-evidence-x.jsonl", "same.json"]  # 원본 그대로
    assert sorted(f.name for f in repo.iterdir()) == ["p2-session-x.json", "same.json"]
