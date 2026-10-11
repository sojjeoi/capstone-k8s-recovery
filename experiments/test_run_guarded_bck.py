"""run_guarded_bck 연결·characterization 테스트 - 동결 test_run_guarded_pilot의 World(실제 guarded 정책 앱 + 가짜 클러스터·시계)를
그대로 써서 새 실행기를 진입점부터 끝까지 돌리고, 동결 A/B 실행기(run_guarded)와 같은 조건에서 공통 동작이 같은지 비교한다.
네트워크·클러스터 접속 없음."""
import json

import pytest

import run_guarded_bck as gb
import run_guarded_pilot as gp
import test_run_guarded_pilot as tgp

link = gp.link

ART = gp.sd.load_artifacts(gp.sd.DEFAULT_ARTIFACTS)
SPEC = gb.bd.load_spec()


class BckDetector(tgp.FakeDetector):
    """동결 FakeDetector 규칙 그대로 + 시작 기록에 arm identity(실제 탐지기 start 기록과 같은 키)."""

    def starter_factory(self, results_dir, policy_url, version, mode, arm=None):
        self.arm = arm
        start = super().starter_factory(results_dir, policy_url, version, mode)

        def wrapped(rid, path, sink_url):
            h = start(rid, path, sink_url)
            self.start_rec["signal_identity"] = gb.bd.signal_identity(arm, ART["hashes"], SPEC["sha256"])
            return h
        return wrapped


@pytest.fixture(autouse=True)
def _identity_payload(monkeypatch):
    """가짜 탐지기 payload에 실제 탐지기와 같은 방식(bd.build_payload: 동결 build_v2_payload 뒤 identity 덮어쓰기)으로 arm identity를 넣는다."""
    orig = link.build_v2_payload

    def build(start, rec, **k):
        p = orig(start, rec, **k)
        if "signal_identity" in start:
            p.update(start["signal_identity"])
        return p
    monkeypatch.setattr(link, "build_v2_payload", build)


TARGET = {"name": "vllm-serving-aaa111-x", "uid": "ua"}


def _world(tmp_path, monkeypatch, *, send_at=(50.0,), results=None, node="sj-worker"):
    w = tgp.World(tmp_path, monkeypatch, mode=gp.MODE_ACT, send_at=send_at, wrap_preview=True)
    w.detector = BckDetector(w, send_at)
    w.injector.get_stress_delivery_record = lambda: {"planned_cr_names": [], "target": dict(TARGET)}
    w.injector.get_real_promotion_diagnostic_exception_info = lambda: {"prediction": {"active_node": node}}
    if results is not None:
        w.results = results
    return w


def _run(w, run, **kw):
    if "approval_path" not in kw:  # setdefault는 기본값을 먼저 만들어 같은 승인 파일을 덮어쓴다
        kw["approval_path"] = w.approval(runs=[run])
    return gb.run_bck(run, deps=kw.pop("deps", None) or w.deps(), policy_url="http://p", results_dir=w.results, **kw)


# ---------------- ID·순서·승인 ----------------
def test_ids_plan_folder_and_probe_reps_separate_from_ab_and_pilot():
    assert gb.run_id_for("bc01b") == "guarded_pressure_act-bc01b-capstone-guarded-bck-v1-20261007"
    assert gb.BCK_RESULTS_DIR not in (gp.AB_RESULTS_DIR, gp.RESULTS_DIR) and gb.BCK_PLAN_ID != gp.AB_PLAN_ID
    reps = [v[2] for v in gb.RUNS.values()]
    assert len(set(reps)) == len(reps) and not set(reps) & {v[2] for v in gp.RUNS.values()}
    assert {r for r in gb.RUNS if r.startswith("bc90")} == {"bc90b", "bc90c", "bc90k"}
    assert len([r for r in gb.RUNS if not r.startswith("bc90")]) == 18 * 3


def test_latin_square_schedule_is_preregistered():
    assert [gb.block_order(n) for n in range(1, 13)] == list(gb.LATIN) * 2
    assert gb.block_order(90) == "BCK" and gb.LATIN == ("BCK", "CKB", "KBC", "KCB", "CBK", "BKC")
    for arm in "BCK":  # 한 바퀴(6 block)에서 각 arm이 각 위치에 정확히 2번
        assert sorted(o.index(arm) for o in gb.LATIN) == [0, 0, 1, 1, 2, 2]


def _fake_summary(results, run):
    (results / f"summary-{gb.run_id_for(run)}.json").write_text("{}", encoding="utf-8")


def test_schedule_refuses_out_of_order_runs(tmp_path):
    r = tmp_path / "res"
    r.mkdir()
    for bad in ("bc90c", "bc90k", "bc01c", "bc02c"):
        with pytest.raises(SystemExit, match="사전 등록 순서 밖"):
            gb.check_schedule(bad, r)
    assert gb.check_schedule("bc90b", r)["position"] == 1 and gb.check_schedule("bc01b", r)["position"] == 1
    _fake_summary(r, "bc01b")
    assert gb.check_schedule("bc01c", r)["required_before"] == ["bc01b"]
    with pytest.raises(SystemExit, match=r"bc01k"):  # bc02 첫 arm(C)은 bc01 세 run 시도 뒤
        gb.check_schedule("bc02c", r)
    for x in ("bc01c", "bc01k"):
        _fake_summary(r, x)
    assert gb.check_schedule("bc02c", r)["order"] == "CKB"
    with pytest.raises(SystemExit):
        gb.check_schedule("bc02b", r)  # bc02 순서 C,K,B - B는 마지막


@pytest.mark.parametrize("runs", [None, [], ["bc90b", "bc90c"], ["bc90c"], "bc90b"])
def test_approval_scope_must_be_exactly_this_run(tmp_path, monkeypatch, runs):
    w = _world(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="승인 범위 밖"):
        _run(w, "bc90b", approval_path=w.approval(**({"runs": runs} if runs is not None else {})))
    assert w.promotes == [] and w.overrides == [] and not list(w.results.glob("running-*"))


def test_act_requires_final_approval(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="act 승인 기록 거부"):
        _run(w, "bc90b", approval_path=w.approval(runs=["bc90b"], status="draft"))


# ---------------- 세 arm 수명주기 ----------------
def test_pilot_block_each_arm_promotes_once_with_its_own_identity_and_same_conditions(tmp_path, monkeypatch):
    res, out = tmp_path / "res", {}
    for i, arm in enumerate("BCK"):
        w = _world(tmp_path / arm, monkeypatch, results=res, node=("sj-worker", "sj-worker2")[i % 2])
        s = _run(w, f"bc90{arm.lower()}")
        out[arm] = (w, s)
        assert len(w.promotes) == 1 and w.delay == 30 and s["cleanup_errors"] == [], s["cleanup_errors"]
        assert s["context"]["detector_id"] == gb.bd.IDENTITY[arm][0] and s["context"]["threshold"] == gb.bd.THRESHOLDS[arm]
        assert {k: s["context"][k] for k in s["signal_identity"]} == gb.bd.signal_identity(arm, ART["hashes"], SPEC["sha256"])
        assert s["comparison"]["arm"] == arm and s["comparison"]["counted_in_comparison"] is False
        assert s["scale_down_delay_restore"]["path"] == "promoted_service_ok" and s["canonical_end"]["ok"] is True
        assert w.detector.arm == arm and w.restore_calls == []  # 승격 arm은 D6로만 끝남
    contracts = [json.loads((res / f"contract-{out[a][1]['run_id']}.json").read_text(encoding="utf-8")) for a in "BCK"]
    assert contracts[0]["conditions"] == contracts[1]["conditions"] == contracts[2]["conditions"]
    assert contracts[0]["conditions"]["rules"]["C"]["threshold"] == 1.6842544293089357
    assert {c["detector"] for c in contracts} == {"c1_bck_v1_if", "c1_bck_v1_fixed_cal", "c1_bck_v1_fixed_legacy"}
    assert {out[a][1]["start_active_node"] for a in "BCK"} == {"sj-worker", "sj-worker2"}  # 노드는 기록만(요건 아님)


def test_arm_without_signal_ends_unpromoted_with_canonical_restore(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch, send_at=())  # 예: K가 관찰 중 발화하지 않음(ITT - 결과 그대로 포함)
    s = _run(w, "bc90b")
    assert w.promotes == [] and w.restore_calls == [("bbb222", tgp.START_POD)] and w.delay == 30
    assert s["scale_down_delay_restore"]["path"] == "not_promoted" and s["cleanup_errors"] == []


def test_official_block_is_counted(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    s = _run(w, "bc01b")
    assert s["comparison"]["counted_in_comparison"] is True and s["comparison"]["schedule"]["order"] == "BCK"


def test_restore_failure_keeps_marker_and_blocks_next_arm(tmp_path, monkeypatch):
    res = tmp_path / "res"
    w = _world(tmp_path / "b", monkeypatch, send_at=(), results=res)
    w.restore_result = {"ok": False, "problems": ["new_rs: ['zzz']"]}
    s = _run(w, "bc90b")
    assert any(e.startswith("canonical_restore_failed") for e in s["cleanup_errors"]) and list(res.glob("running-*bc90b*"))
    w2 = _world(tmp_path / "c", monkeypatch, results=res)
    with pytest.raises(SystemExit):
        _run(w2, "bc90c")
    assert w2.promotes == [] and "inject" not in w2.injector.calls


@pytest.mark.parametrize("change", [{"phase": "Degraded"}, {"abort": True}, {"delay": 420}])
def test_non_canonical_start_refused_without_fixing(tmp_path, monkeypatch, change):
    w = _world(tmp_path, monkeypatch)
    w.canon.update(change)
    with pytest.raises(SystemExit, match="canonical_start"):
        _run(w, "bc90b")
    assert w.injector.calls == [] and not list(w.results.glob("running-*"))


def test_run_id_reuse_refused(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    _run(w, "bc90b")
    with pytest.raises(SystemExit, match="이미 결과가 있음"):
        _run(w, "bc90b")


def test_promotion_unknown_stops_without_repromotion_and_blocks(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    w.promote_verified = None
    s = _run(w, "bc90b")
    assert len(w.promotes) == 1 and s["cleanup_errors"] and list(w.results.glob("running-*"))


# ---------------- characterization: 동결 A/B 실행기(ab90b)와 같은 조건에서 공통 동작 비교 ----------------
def _ab90b(tmp_path, monkeypatch, send_at):
    wa = tgp.World(tmp_path / "a", monkeypatch, mode=gp.MODE_RO, send_at=(), wrap_preview=True)
    wb = tgp.World(tmp_path / "b", monkeypatch, mode=gp.MODE_ACT, send_at=send_at, wrap_preview=True)
    wb.results = wa.results
    for w in (wa, wb):
        w.injector.get_stress_delivery_record = lambda: {"planned_cr_names": [], "target": dict(TARGET)}
    wa.run("ab90a")
    return wb, wb.run("ab90b", approval_path=wb.approval(runs=["ab90b"]))


SAME = ("kind", "mode", "outcome_status", "scale_down_delay_before", "scale_down_delay_after", "start_active_node",
        "cleanup_errors", "harness_error", "rehearsal", "evidence_counts")


@pytest.mark.parametrize("send_at", [(50.0,), ()])
def test_common_behaviour_matches_frozen_ab_runner(tmp_path, monkeypatch, send_at):
    wf, sf = _ab90b(tmp_path / "frozen", monkeypatch, send_at)
    wn = _world(tmp_path / "new", monkeypatch, send_at=send_at)
    sn = _run(wn, "bc90b")
    assert set(sn) - set(sf) == {"arm", "signal_identity", "rule_spec_sha256"} and not set(sf) - set(sn)
    assert {k: sn[k] for k in SAME} == {k: sf[k] for k in SAME}
    assert len(wn.promotes) == len(wf.promotes) and wn.restore_calls == wf.restore_calls and wn.delay == wf.delay == 30
    assert wn.overrides == wf.overrides == [gp.SCALE_DOWN_DELAY_SEC]
    assert sn["scale_down_delay_restore"]["path"] == sf["scale_down_delay_restore"]["path"]
    assert sn["canonical_end"]["ok"] == sf["canonical_end"]["ok"] is True
    wk = ("guard_starts", "duplicates", "guard_pending_at_end", "late_failures")
    assert {k: sn["watcher"][k] for k in wk} == {k: sf["watcher"][k] for k in wk}
    assert [e["effective"] for e in sn["watcher"]["promotion_events"]] == [e["effective"] for e in sf["watcher"]["promotion_events"]]
    cf = json.loads((wf.results / f"contract-{sf['run_id']}.json").read_text(encoding="utf-8"))
    cn = json.loads((wn.results / f"contract-{sn['run_id']}.json").read_text(encoding="utf-8"))
    shared = ("stress_stages", "probe_config", "probe_config_sha256", "probe_image", "probe_image_id", "detector_rule",
              "start_state_rule")
    assert {k: cn["conditions"][k] for k in shared} == {k: cf["conditions"][k] for k in shared}
    assert (cn["observation_sec"], cn["ready_timeout_sec"], cn["scale_down_delay"]) == \
        (cf["observation_sec"], cf["ready_timeout_sec"], cf["scale_down_delay"])
    pf = json.loads((wf.results / sf["preflight_file"]).read_text(encoding="utf-8"))
    pn = json.loads((wn.results / sn["preflight_file"]).read_text(encoding="utf-8"))
    assert set(pf) == set(pn) and pn["canonical_start"]["problems"] == pf["canonical_start"]["problems"] == []


def test_b_context_differs_from_ab_only_in_detector_id(tmp_path, monkeypatch):
    _, sf = _ab90b(tmp_path / "frozen", monkeypatch, (50.0,))
    sn = _run(_world(tmp_path / "new", monkeypatch), "bc90b")
    diff = {k for k in sf["context"] if k != "run_id" and sf["context"][k] != sn["context"].get(k)}
    assert diff == {"detector_id"}, diff


def test_frozen_runner_constants_unchanged():
    assert gp.AB_PLAN_ID == "capstone-guarded-ab-v1-20261007" and gp.ld.DETECTOR_ID == "c1_live_v3.3"
    assert gp.SCALE_DOWN_DELAY_SEC == 420 and gp.BASE_SCALE_DOWN_DELAY_SEC == 30 and gp.PRESSURE_OBSERVATION_SEC == 600.0


def test_bck_starter_launches_new_detector_with_arm(tmp_path):
    seen = {}

    def start(cmd, cwd, stdout_path, stderr_path):
        seen["cmd"] = cmd
        return {"proc": tgp.FakeProc()}
    h = gb.bck_starter(tmp_path, "http://p", "guarded-v1", "act", "K", start_subprocess=start,
                       stop_subprocess=lambda h, n: {"returncode": 5})("rid", None, "http://sink")
    cmd = seen["cmd"]
    assert cmd[2].endswith("c1_bck_live_detector.py") and cmd[cmd.index("--arm") + 1] == "K"
    assert cmd[cmd.index("--expected-mode") + 1] == "act" and h.stop()["exit_meaning"] == "delivery_not_confirmed"
