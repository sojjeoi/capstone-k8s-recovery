"""demo-only wrapper - frozen 실행기 무수정 호출, demo 폴더·ID 분리, 승인 조건, 전역 원복, ID 재사용 거부. 클러스터 접근 없음."""
import json

import pytest

import run_demo_guarded as drg
import run_guarded_pilot as gp
import test_run_guarded_pilot as tgp


def demo_approval(w, **over):
    p = w.approval()
    a = {**json.loads(p.read_text(encoding="utf-8")), "demo_only": True, "counted": False, "runs": ["demo01"], **over}
    p.write_text(json.dumps(a), encoding="utf-8")
    return p


def run(w, rid="demo01", **kw):
    return drg.run_demo(rid, deps=kw.pop("deps", None) or w.deps(), policy_url="http://p", results_dir=w.results, **kw)


def test_demo_run_uses_frozen_runner_with_demo_namespace_and_restores_globals(tmp_path, monkeypatch):
    runs0, plan0, doc0 = dict(gp.RUNS), gp.PLAN_ID, gp.CONTRACT_DOC
    w = tgp.World(tmp_path, monkeypatch, mode=gp.MODE_ACT)
    s = run(w, approval_path=demo_approval(w))
    assert s["outcome_status"] == "completed" and s["cleanup_errors"] == [] and len(w.promotes) == 1
    assert s["watcher"]["guard_result"]["result"] == "verified"            # 기존 서비스 확인 경로 그대로
    assert s["run_id"] == "guarded_pressure_act-demo01-capstone-demo-only-20261010"
    contract = json.loads(next(w.results.glob("contract-*demo01*.json")).read_text(encoding="utf-8"))
    assert contract["plan_id"] == drg.DEMO_PLAN_ID and contract["contract_doc"] == drg.DEMO_CONTRACT_DOC
    tag = json.loads(next(w.results.glob("demo-tag-*.json")).read_text(encoding="utf-8"))
    assert tag["demo_only"] is True and tag["counted"] is False and tag["official_evidence"] is False
    assert (dict(gp.RUNS), gp.PLAN_ID, gp.CONTRACT_DOC) == (runs0, plan0, doc0) and "demo01" not in gp.RUNS


def test_refuses_non_demo_ids_and_non_demo_approval(tmp_path, monkeypatch):
    w = tgp.World(tmp_path, monkeypatch, mode=gp.MODE_ACT)
    for rid in ("gap03", "ab06b", "bc07b"):
        with pytest.raises(SystemExit, match="demo 실행 ID가 아님"):
            run(w, rid, approval_path=demo_approval(w))
    for over in ({"demo_only": False}, {"counted": True}, {"runs": ["demo02"]}):
        with pytest.raises(SystemExit, match="demo 승인 기록 아님"):
            run(w, approval_path=demo_approval(w, **over))
    with pytest.raises(SystemExit, match="승인 기록"):
        run(w, approval_path=None)
    assert w.promotes == [] and "demo01" not in gp.RUNS


def test_id_reuse_refused_and_globals_restored_on_error(tmp_path, monkeypatch):
    w = tgp.World(tmp_path, monkeypatch, mode=gp.MODE_ACT)
    run(w, approval_path=demo_approval(w))
    with pytest.raises(SystemExit, match="덮어쓰지 않음|재사용 금지"):
        run(w, approval_path=demo_approval(w))
    assert "demo01" not in gp.RUNS and gp.PLAN_ID == "capstone-guarded-v1-pilot-20261006"


def test_demo_cleanup_failure_blocks_next_demo_run(tmp_path, monkeypatch):
    w = tgp.World(tmp_path, monkeypatch, mode=gp.MODE_ACT)
    w.results.mkdir(parents=True, exist_ok=True)
    (w.results / "running-guarded_pressure_act-demo01-x.json").write_text("{}", encoding="utf-8")
    base = w.deps()
    base.check_unresolved = lambda: None          # 공식 폴더 검사는 통과한다고 가정 - demo 폴더 검사만 확인
    with pytest.raises(SystemExit, match="실행 중 표시"):
        run(w, deps=base, approval_path=demo_approval(w, runs=["demo02"]), rid="demo02")
    assert w.promotes == []


def test_approval_helper_output_passes_demo_and_frozen_checks(tmp_path):
    import demo_only_approval as doa
    clock = tmp_path / "clock.json"
    clock.write_text(json.dumps({"summary": {"best_offset_sec": 0.05, "best_uncertainty_sec": 0.1, "n_ok": 20}}), encoding="utf-8")
    p = tmp_path / "approval.json"
    p.write_text(json.dumps(doa.build("demo01", clock, "user test")), encoding="utf-8")
    drg.check_demo_approval(p, "demo01")
    assert gp.load_act_approval(p)["demo_only"] is True
    clock.write_text(json.dumps({"summary": {"best_offset_sec": 0.4, "best_uncertainty_sec": 0.2, "n_ok": 20}}), encoding="utf-8")
    with pytest.raises(SystemExit, match="시계 측정 불충분"):
        doa.build("demo01", clock, "user test")
