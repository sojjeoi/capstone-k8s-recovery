"""n2_select_offline 시험(2026-10-17, M3, DEVELOPMENT). 클러스터 접근 없음 - 합성 Stage 1 기록(정상 4·압박 5)에서 후보 3개를 같은 tick으로 재생한다.
핵심: (1) 어느 후보가 Gate 2를 통과하느냐에 따라 사전 고정 규칙대로 선택된다(net_probe가 억지로 뽑히지 않는다), (2) 결과가 결정적이다,
(3) 무결성 문제는 INTEGRITY_FAILURE, (4) 선택 증거 -> active profile -> Stage 2 진입 검사가 이어진다."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import n2_common as nc  # noqa: E402
import n2_select_offline as so  # noqa: E402
import n2_selection as nsel  # noqa: E402
import n2_stage1 as ns1  # noqa: E402
import n2_stage2 as n2s2  # noqa: E402
from test_n2_executor import env  # noqa: E402,F401  (합성 외부 artifact + 동결 해시 교체 fixture)
from test_n2_session import template, write_profile  # noqa: E402

pc = nc.pc
from feature_selection import FEATURE_NAMES  # noqa: E402

T0 = datetime(2026, 10, 20, 1, 0, tzinfo=timezone.utc)
CPU, NET, IF = nc.CPU_ID, nc.NET_ID, nc.IF_ID
HOT = {CPU: 3.0, NET: 1.0, IF: -0.5}            # 임계값을 넘는(이상) 값
COLD = {CPU: 1.0, NET: 0.004, IF: 0.2}          # 정상 값
SNAP = {"active_selector": "aaa111", "preview_selector": "bbb222",
        "pods": [{"name": "vllm-serving-aaa111-x", "uid": "ua", "hash": "aaa111", "restart_count": 0}]}


def row(hot):
    """hot = 이 tick에서 이상 값을 낼 후보 집합 -> (cpu_mean, if_score, net_probe)."""
    return tuple((HOT if d in hot else COLD)[d] for d in (CPU, IF, NET))


def evals(rows):
    out = []
    for i, p in enumerate(rows, start=1):
        t = T0 + timedelta(seconds=15 * i)
        r = {"record_type": "c1_evaluation", "evaluation_seq": i, "eval_started_at_utc": t.isoformat(),
             "window_start_utc": (t - timedelta(seconds=60)).isoformat(), "window_end_utc": t.isoformat(),
             "status": "scored", "pod_snapshot": SNAP, "expected_pods": {"vllm-serving-aaa111-x": "ua"},
             "features": {**{n: 0.0 for n in FEATURE_NAMES}, "cpu_mean": p[0]}, "score": p[1], "net_probe": {"value": p[2]},
             "score_completed_at_utc": (t + timedelta(seconds=1)).isoformat()}
        out.append(r)
    return out


def build_stage1(tmp_path, *, pressure_hot, normal_hot_by_session=None, start_over=None, invalid_pressure=None, old_profile_for=None,
                 previous_profiles=()):
    """합성 Stage 1 폴더. pressure_hot = 압박 세션에서 주입 뒤 이상 값을 내는 후보 집합. normal_hot_by_session = {n(1~4): 후보 집합}(정상 세션 중간 3연속).
    old_profile_for = {run: 그 세션이 실행될 때의 이전 revision profile 해시}, previous_profiles = 선택 분석이 같은 계열로 인정할 이전 profile 파일들."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    s1 = ns1.load_stage1(ns1.NetConfig(results_dir=tmp_path / "stage1", previous_selection_profiles=tuple(previous_profiles)))
    d = tmp_path / "stage1"
    d.mkdir(parents=True)
    profile = write_profile(tmp_path, template())
    sha = nc.load_profile(profile, stage="stage1")["sha256"]
    base = {"t0_utc": T0.isoformat(), "observation_end_utc": (T0 + timedelta(seconds=4200)).isoformat(),
            "slo": {"episodes": []}, "service_events": [], "infra_events": []}
    plan = []
    for n in range(1, 5):
        hot = (normal_hot_by_session or {}).get(n, set())
        plan.append((f"n2n0{n}", [row(set())] * 3 + [row(hot)] * 3 + [row(set())] * 2, {"base": base}, "normal_valid"))
    inj = {"t_injection_observed_utc": (T0 + timedelta(seconds=60)).isoformat()}
    first_valid = 1
    if invalid_pressure is not None:          # 새 Pod 격리를 확립하지 못해 무효가 된 세션 1개를 먼저 시도했고, 유효 5개는 그 뒤(n2p02~06)다
        plan.append(("n2p01", [row(set())] * 4 + [row(invalid_pressure)] * 8,
                     {**inj, "valid": False, "invalid_reasons": ["SESSION INVALID — target isolation / new-Pod leakage exclusion not established (1건)"]}, "invalid"))
        first_valid = 2
    for n in range(first_valid, first_valid + 5):
        plan.append((f"n2p0{n}", [row(set())] * 4 + [row(pressure_hot)] * 8, inj, "valid"))
    for n, (run, rows, extra, status) in enumerate(plan, 1):
        rid = s1.run_id_for(run)
        ep = d / f"c1-live-evidence-{rid}.jsonl"
        psha = (old_profile_for or {}).get(run, sha)
        start = {"record_type": "c1_shadow_start", "run_id": rid, "environment_profile_sha256": psha, "active_detector": None,
                 "net_probe_definition_sha256": nc.NET_DEFINITION_SHA256, "net_probe_calibration_sha256": nc.NET_CALIBRATION_SHA256,
                 **(start_over or {}).get(run, {})}
        ep.write_text("".join(json.dumps({**r, "run_id": rid}) + "\n" for r in [start, *evals(rows)]), encoding="utf-8")
        (d / f"p2-session-{run}.json").write_text(json.dumps(
            {"run": run, "run_id": rid, "kind": s1.kind_of(run), "attempt_number": n, "status": "run", "evidence_file": ep.name,
             "evidence_sha256": pc.sha256_bytes(ep), "environment_profile_sha256": psha,
             "runner_ended_at_utc": f"2026-10-2{n % 10}T00:00:00+00:00", "cleanup_errors": []}), encoding="utf-8")
        (d / f"p2-validity-{run}.json").write_text(json.dumps({"status": status, "valid": True, **extra}), encoding="utf-8")
    return s1, d, profile, sha


@pytest.fixture
def fake_if_scores(monkeypatch):
    import selection as p2sel
    monkeypatch.setattr(p2sel, "if_scores_for", lambda ev, art: {r["evaluation_seq"]: r["score"] for r in ev if r.get("status") == "scored"})


def collect(s1, d, profile, root, sha):
    return so.collect(s1, d, profile_path=profile, artifact_root=root, verify_freeze=lambda: {"selection_profile_sha256": sha})


# ---------- (1) 사전 고정 규칙대로 선택 ----------
@pytest.mark.parametrize("hot,want", [
    ({NET}, f"SELECTED:{NET}"),                  # net_probe만 압박을 잡는다 -> net_probe (억지로 뽑지 않았고, 유일한 통과자라서 뽑힌다)
    ({CPU, NET, IF}, f"SELECTED:{CPU}"),         # 모두 통과하고 불필요 신호·시각이 동등 -> 단순성 순서(CPU)
    ({IF}, f"SELECTED:{IF}"),
    ({CPU}, f"SELECTED:{CPU}"),
    (set(), nsel.NO_ELIGIBLE),
])
def test_selection_follows_the_preregistered_rule(tmp_path, env, fake_if_scores, hot, want):
    s1, d, profile, sha = build_stage1(tmp_path, pressure_hot=hot)
    ev = so.evidence_from(collect(s1, d, profile, env[1], sha))
    assert ev["outcome"] == want and ev["gate_results"]["session_validity"] == {"normal_valid": 4, "pressure_valid": 5,
                                                                              "required": {"normal": 4, "pressure": 5}}
    assert ev["gate_results"]["gate2"]["pass_count"] == {CPU: 5 if CPU in hot else 0, NET: 5 if NET in hot else 0, IF: 5 if IF in hot else 0}
    assert ev["gate_results"]["gate3"]["unnecessary_sum"] == {CPU: 0, NET: 0, IF: 0}
    assert set(ev["candidates"]) == set(nc.DETECTOR_IDS) and ev["decided_at_utc"] == "2026-10-29T00:00:00+00:00"
    assert ev["evidence_id"].startswith(so.EVIDENCE_PREFIX) and ev["rule"] == nc.PREREG_SHA256


def test_unnecessary_signals_in_normal_sessions_decide_gate3(tmp_path, env, fake_if_scores):
    """net_probe와 IF가 모두 압박을 잡아도, 정상 운전에서 net_probe가 불필요 신호를 2번 내면(차이 >= 2) IF가 뽑힌다."""
    s1, d, profile, sha = build_stage1(tmp_path, pressure_hot={NET, IF}, normal_hot_by_session={1: {NET}, 2: {NET}})
    ev = so.evidence_from(collect(s1, d, profile, env[1], sha))
    assert ev["gate_results"]["gate3"]["unnecessary_sum"] == {CPU: 0, NET: 2, IF: 0}
    assert ev["outcome"] == f"SELECTED:{IF}" and ev["gate_results"]["gate3"]["used"] is True


def test_gate2_failure_is_never_compensated_by_quietness(tmp_path, env, fake_if_scores):
    s1, d, profile, sha = build_stage1(tmp_path, pressure_hot={CPU}, normal_hot_by_session={1: {CPU}, 2: {CPU}, 3: {CPU}})
    ev = so.evidence_from(collect(s1, d, profile, env[1], sha))                       # CPU만 통과(불필요 신호 3) - 조용한 net/IF는 Gate 2 탈락
    assert ev["outcome"] == f"SELECTED:{CPU}" and ev["gate_results"]["gate2"]["eligible"] == [CPU]


def test_result_is_byte_deterministic(tmp_path, env, fake_if_scores):
    s1, d, profile, sha = build_stage1(tmp_path, pressure_hot={NET})
    a = so.evidence_from(collect(s1, d, profile, env[1], sha))
    b = so.evidence_from(collect(s1, d, profile, env[1], sha))                       # 두 번째는 수집 닫힘 표식이 이미 있는 상태
    assert pc.canonical(a) == pc.canonical(b) and (d / s1.CLOSED_MARKER).exists()


# ---------- (3) 무결성 ----------
def _older_profile(tmp_path, profile, *, edit=None):
    """현재 profile에서 사전등록 해시 필드(selection.rule)만 바꾼(edit가 있으면 다른 값도 바꾼) 이전 revision profile 파일."""
    d = json.loads(Path(profile).read_text(encoding="utf-8"))
    d["selection"]["rule"] = "0" * 64
    if edit:
        edit(d)
    p = tmp_path / ("older-" + ("changed" if edit else "same") + ".json")
    p.write_text(json.dumps(d), encoding="utf-8")
    return p, pc.text_sha256(p)


def test_a_valid_session_run_under_an_earlier_profile_revision_is_reused_but_a_changed_profile_is_not(tmp_path, env, fake_if_scores):
    """rev3 §9.3: n2n03 같은 이전 revision(사전등록 해시 필드만 다른 profile)의 유효 세션은 같은 기준으로 재사용하고, 내용이 다른 profile은 거부한다."""
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    s1, d, profile, sha = build_stage1(probe_dir, pressure_hot={NET})
    older, older_sha = _older_profile(probe_dir, profile)
    changed, changed_sha = _older_profile(probe_dir, profile, edit=lambda x: x["detectors"][1].__setitem__("threshold", 0.123))
    ok_dir = tmp_path / "ok"
    ok_dir.mkdir()
    s1, d, profile, sha = build_stage1(ok_dir, pressure_hot={NET}, old_profile_for={"n2n01": older_sha}, previous_profiles=(older,))
    c = collect(s1, d, profile, env[1], sha)
    assert not [p for p in c["inputs"]["integrity_problems"] if "profile_changed" in p] and len(c["inputs"]["normal"]) == 4     # n2n01 포함
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    s1, d, profile, sha = build_stage1(bad_dir, pressure_hot={NET}, old_profile_for={"n2n01": changed_sha}, previous_profiles=(changed,))
    c = collect(s1, d, profile, env[1], sha)
    assert any("profile_changed_or_not_shadow:n2n01" in p for p in c["inputs"]["integrity_problems"])                          # 내용이 다른 profile
    s1, d, profile, sha = build_stage1(tmp_path / "unlisted", pressure_hot={NET}, old_profile_for={"n2n01": older_sha})          # 계열로 인정하지 않은 profile
    c = collect(s1, d, profile, env[1], sha)
    assert any("profile_changed_or_not_shadow:n2n01" in p for p in c["inputs"]["integrity_problems"])


def test_other_net_probe_definition_or_calibration_in_a_session_is_integrity_failure(tmp_path, env, fake_if_scores):
    s1, d, profile, sha = build_stage1(tmp_path, pressure_hot={NET}, start_over={"n2p02": {"net_probe_calibration_sha256": "0" * 64}})
    c = collect(s1, d, profile, env[1], sha)
    assert "net_probe_definition_changed:n2p02" in c["inputs"]["integrity_problems"]
    assert nsel.select(c["inputs"])["outcome"] == nsel.INTEGRITY


def test_evidence_changed_after_the_session_record_is_integrity_failure(tmp_path, env, fake_if_scores):
    s1, d, profile, sha = build_stage1(tmp_path, pressure_hot={NET})
    ep = d / f"c1-live-evidence-{s1.run_id_for('n2p01')}.jsonl"
    ep.write_text(ep.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    c = collect(s1, d, profile, env[1], sha)
    assert "evidence_hash_mismatch:n2p01" in c["inputs"]["integrity_problems"] and nsel.select(c["inputs"])["outcome"] == nsel.INTEGRITY


def test_unfrozen_implementation_or_other_profile_is_integrity_failure(tmp_path, env, fake_if_scores):
    s1, d, profile, sha = build_stage1(tmp_path, pressure_hot={NET})
    c = so.collect(s1, d, profile_path=profile, artifact_root=env[1], verify_freeze=lambda: {"selection_profile_sha256": "0" * 64})
    assert "selection_profile_not_implementation_frozen" in c["inputs"]["integrity_problems"]
    c = so.collect(s1, d, profile_path=profile, artifact_root=env[1])                    # 동결 검증기가 연결되지 않은 기본 상태 = fail-closed
    assert any(p.startswith("implementation_freeze:") for p in c["inputs"]["integrity_problems"])


def test_tampered_calibration_evidence_makes_net_probe_gate1_fail(tmp_path, env, fake_if_scores, monkeypatch):
    s1, d, profile, sha = build_stage1(tmp_path, pressure_hot={NET})
    monkeypatch.setattr(nc, "NET_CALIBRATION_SHA256", "0" * 64)
    c = collect(s1, d, profile, env[1], sha)
    assert c["inputs"]["gate1"][NET] and not c["inputs"]["gate1"][CPU]
    assert nsel.select(c["inputs"])["outcome"] == nsel.INTEGRITY


def test_refuses_before_collection_is_finished_or_validity_is_missing(tmp_path, env, fake_if_scores):
    s1, d, profile, sha = build_stage1(tmp_path, pressure_hot={NET})
    (d / "p2-validity-n2p05.json").unlink()
    with pytest.raises(SystemExit, match="유효성 기록 없는"):
        collect(s1, d, profile, env[1], sha)
    assert not (d / s1.CLOSED_MARKER).exists()                                          # 실수로 수집이 닫히지 않는다
    s1b, db, profileb, shab = build_stage1(tmp_path / "b", pressure_hot={NET})
    (db / "p2-session-n2p05.json").unlink()
    (db / "p2-validity-n2p05.json").unlink()
    with pytest.raises(SystemExit, match="수집이 끝나지 않음"):
        collect(s1b, db, profileb, env[1], shab)


# ---------- (4) 증거 -> active profile -> Stage 2 진입 ----------
def test_selected_net_probe_flows_into_active_profile_and_stage2_entry(tmp_path, env, fake_if_scores):
    s1, d, profile, sha = build_stage1(tmp_path, pressure_hot={NET})
    ev = so.evidence_from(collect(s1, d, profile, env[1], sha))
    out = tmp_path / "out"
    written = so.write_outputs(ev, profile, out)
    assert written == {"evidence": so.EVIDENCE_NAME, "active_profile": "active-profile.json"}
    act = nc.load_profile(out / "active-profile.json", stage="stage2")
    assert act["active"] == NET and [x["role"] for x in act["profile"]["detectors"] if x["detector_id"] == NET] == [nc.ACTIVE]
    prov = json.loads((out / "active-profile-provenance.json").read_text(encoding="utf-8"))
    assert prov["selected_detector"] == NET and prov["generated_profile_sha256"] == pc.sha256_bytes(out / "active-profile.json")
    s2 = n2s2.load_stage2(s1, n2s2.Net2Config(results_dir=tmp_path / "s2"))
    entry = n2s2.entry_check_net(s2, selection_profile_path=profile, active_profile_path=out / "active-profile.json",
                                 evidence_path=out / so.EVIDENCE_NAME, artifact_root=env[1],
                                 snapshot_fn=lambda: {"template": template()}, impl={"selection_profile_sha256": sha},
                                 stage1_dir=d, collect_fn=lambda *a, **k: so.collect(s1, *a, **k))
    assert entry["selected"] == NET and entry["loaded"]["active"] == NET
    tampered = json.loads((out / so.EVIDENCE_NAME).read_text(encoding="utf-8"))          # 증거를 바꾸면 Stage 2 진입이 거부된다
    tampered["reason"] = "x"
    (out / "tampered.json").write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(SystemExit, match="selection_evidence_not_reproducible"):
        n2s2.entry_check_net(s2, selection_profile_path=profile, active_profile_path=out / "active-profile.json",
                             evidence_path=out / "tampered.json", artifact_root=env[1], snapshot_fn=lambda: {"template": template()},
                             impl={"selection_profile_sha256": sha}, stage1_dir=d, collect_fn=lambda *a, **k: so.collect(s1, *a, **k))


def _rev4_chain(tmp_path, env, *, rule4=nc.STAGE1_SELECTION_PREREG_REV4, evidence_rule=None, active_rule=None, profile_edit=None):
    """Stage 1 선택이 rev4 사전등록 아래에서 끝난 상황(n2s02 preflight 거부의 원인)을 만든다: 현재(rev5) 선택 profile과 사전등록 해시 필드만 다른 이전 profile,
    그 profile에서 파생된 선택 증거와 active profile. 반환: Stage 2 진입 검사 호출 인자."""
    old_path = tmp_path / "rev4-profile.json"
    s1, d, profile, sha = build_stage1(tmp_path, pressure_hot={NET}, previous_profiles=(old_path,))
    ev = so.evidence_from(collect(s1, d, profile, env[1], sha))
    out = tmp_path / "out"
    so.write_outputs(ev, profile, out)
    old = json.loads(Path(profile).read_text(encoding="utf-8"))
    old["selection"]["rule"] = rule4
    if profile_edit:
        profile_edit(old)
    old_path.write_text(json.dumps(old), encoding="utf-8")
    old_sha = pc.text_sha256(old_path)
    ev2 = {**json.loads((out / so.EVIDENCE_NAME).read_text(encoding="utf-8")), "rule": evidence_rule or rule4, "selection_profile_sha256": old_sha}
    ev_path = tmp_path / "evidence-rev4.json"
    ev_path.write_text(json.dumps(ev2), encoding="utf-8")
    act = json.loads((out / "active-profile.json").read_text(encoding="utf-8"))
    act["selection"].update(rule=active_rule or rule4, source_selection_profile_sha256=old_sha, selection_evidence_sha256=pc.sha256_bytes(ev_path))
    act_path = tmp_path / "active-rev4.json"
    act_path.write_text(json.dumps(act), encoding="utf-8")
    s2 = n2s2.load_stage2(s1, n2s2.Net2Config(results_dir=tmp_path / "s2"))
    return s2, dict(selection_profile_path=profile, active_profile_path=act_path, evidence_path=ev_path, artifact_root=env[1],
                    snapshot_fn=lambda: {"template": template()}, impl={"selection_profile_sha256": sha}, stage1_dir=d,
                    collect_fn=lambda *a, **k: so.collect(s1, *a, **k))


def test_stage2_entry_accepts_a_selection_made_under_the_rev4_preregistration_and_nothing_else(tmp_path, env, fake_if_scores):
    """n2s02 preflight 거부 뒤 연결 수정: Stage 1 선택을 실제 만든 rev4 해시(증거·active profile이 같은 해시, 같은 selection profile에서 파생)만 인정한다."""
    s2, kw = _rev4_chain(tmp_path / "ok", env, )
    entry = n2s2.entry_check_net(s2, **kw)
    assert entry["selected"] == NET
    s2, kw = _rev4_chain(tmp_path / "evmix", env, evidence_rule=nc.PREREG_SHA256)                       # 증거와 active profile이 같은 해시에서 파생되지 않음
    with pytest.raises(SystemExit, match="selection_profile_chain_mismatch"):
        n2s2.entry_check_net(s2, **kw)
    s2, kw = _rev4_chain(tmp_path / "actmix", env, active_rule=nc.PREREG_SHA256)
    with pytest.raises(SystemExit, match="selection_profile_chain_mismatch"):
        n2s2.entry_check_net(s2, **kw)
    older = "0" * 64                                                                                    # 임의의 과거 revision 해시는 인정하지 않는다
    s2, kw = _rev4_chain(tmp_path / "other", env, rule4=older)
    with pytest.raises(SystemExit, match="selection_evidence_not_reproducible|PROFILE_INVALID"):
        n2s2.entry_check_net(s2, **kw)
    s2, kw = _rev4_chain(tmp_path / "changed", env, profile_edit=lambda x: x["detectors"][1].__setitem__("threshold", 0.123))   # 내용이 다른 이전 profile
    with pytest.raises(SystemExit, match="selection_profile_chain_mismatch"):
        n2s2.entry_check_net(s2, **kw)


def test_only_the_rev4_hash_is_added_for_stage2_profiles_and_stage1_stays_strict(tmp_path, env, fake_if_scores):
    s1, d, profile, sha = build_stage1(tmp_path, pressure_hot={NET})
    ev = so.evidence_from(collect(s1, d, profile, env[1], sha))
    out = tmp_path / "out"
    so.write_outputs(ev, profile, out)
    act = json.loads((out / "active-profile.json").read_text(encoding="utf-8"))
    for rule, ok in ((nc.PREREG_SHA256, True), (nc.STAGE1_SELECTION_PREREG_REV4, True), ("e34233be638d591564684cc911ffaa8b2ee15d41ecac5a610def6edf71b32804", False),
                     ("1e80017b57183b15e089825b9fe5419fd91510e961035223caf83e2eac469b97", False), ("ff835c1810b1bf37057b8851e03b65230afb5738cc05e24d6b6ca29328f7848d", False)):
        act["selection"]["rule"] = rule
        if ok:
            nc.validate_profile(act, stage="stage2")
        else:
            with pytest.raises(nc.ProfileInvalid, match="selection_rule_not_design_freeze"):
                nc.validate_profile(act, stage="stage2")
    sel = json.loads(Path(profile).read_text(encoding="utf-8"))
    sel["selection"]["rule"] = nc.STAGE1_SELECTION_PREREG_REV4
    with pytest.raises(nc.ProfileInvalid, match="selection_rule_not_design_freeze"):                    # Stage 1 profile은 현재 해시만
        nc.validate_profile(sel, stage="stage1")


def test_the_rev4_hash_constant_matches_the_real_rev4_profile_and_document(tmp_path):
    prof = nc.HERE / "profiles" / "selection-profile-network-tolerant-vllm-qwen05b-v4.json"
    assert json.loads(prof.read_text(encoding="utf-8"))["selection"]["rule"] == nc.STAGE1_SELECTION_PREREG_REV4
    doc4 = nc.ROOT / "docs" / "design" / "PHASE2-NETWORK-ENV-PREREG-v1-rev4.md"
    assert doc4.is_file() and pc.text_sha256(doc4) == nc.STAGE1_SELECTION_PREREG_REV4


@pytest.mark.parametrize("invalid_hot,valid_hot,want", [
    (set(), {NET}, f"SELECTED:{NET}"),                  # 무효 세션에서 net_probe가 못 잡았어도 net_probe의 실패로 세지 않는다(5/5 통과)
    ({NET}, {CPU}, f"SELECTED:{CPU}"),                  # 무효 세션에서 net_probe가 잡았어도 net_probe의 성공으로 세지 않는다(0/5)
])
def test_leakage_invalid_session_is_excluded_from_every_detectors_gate_results(tmp_path, env, fake_if_scores, invalid_hot, valid_hot, want):
    """새 Pod 누출 배제를 확립하지 못한 세션은 유효성 기록에서 무효이고 선택 입력(Gate 2·3)에 들어가지 않는다 - 어느 감지기의 실패나 성공으로도 세지 않는다."""
    s1, d, profile, sha = build_stage1(tmp_path, pressure_hot=valid_hot, invalid_pressure=invalid_hot)
    c = collect(s1, d, profile, env[1], sha)
    sessions = [p["session"] for p in c["inputs"]["pressure"]]
    assert sessions == ["n2p02", "n2p03", "n2p04", "n2p05", "n2p06"] and c["inputs"]["integrity_problems"] == []
    ev = so.evidence_from(c)
    assert ev["outcome"] == want
    assert ev["gate_results"]["gate2"]["pass_count"][NET] == (5 if NET in valid_hot else 0)
    assert all(s["session"] != "n2p01" for per in ev["gate_results"]["gate2"]["sessions"].values() for s in per)
    kept = json.loads((d / "p2-validity-n2p01.json").read_text(encoding="utf-8"))                                           # 무효 기록은 보고용으로 그대로 남는다
    assert kept["valid"] is False and kept["invalid_reasons"][0].startswith("SESSION INVALID — target isolation")


def test_no_active_profile_unless_selected(tmp_path, env, fake_if_scores):
    s1, d, profile, sha = build_stage1(tmp_path, pressure_hot=set())
    ev = so.evidence_from(collect(s1, d, profile, env[1], sha))
    written = so.write_outputs(ev, profile, tmp_path / "out")
    assert ev["outcome"] == nsel.NO_ELIGIBLE and written == {"evidence": so.EVIDENCE_NAME}
    assert not (tmp_path / "out" / "active-profile.json").exists()
