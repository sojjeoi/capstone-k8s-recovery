"""호스트 중단(PC 절전) 뒤 재개 규칙 시험(사전등록 rev2 §9.1, 2026-10-18, DEVELOPMENT). 합성 결과 폴더만 쓴다(실제 n2n01 기록·클러스터 없음)."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import n2_resume as rn  # noqa: E402
import n2_run as run  # noqa: E402
import n2_stage1 as ns1  # noqa: E402

RUN, RID = "n2n01", "guarded_normal_record_only-n2n01-test"
T0, T1 = "2026-10-17T14:20:15+00:00", "2026-10-17T15:21:49+00:00"
SLEEP_MS = int(datetime(2026, 10, 17, 15, 19, 50, tzinfo=timezone.utc).timestamp() * 1000)
ERRS = ["policy_state_after_failed: ConnectionError: x", "preview_cleanup_held: reasons=['policy_state_unreadable']",
        "canonical_end_not_reached: ['phase=Paused']"]
ARTS = {"model.pkl": "a" * 64}


def ev(i, ms=SLEEP_MS):
    return {"Id": i, "TimeCreated": f"/Date({ms})/"}


def setup(tmp_path, *, errors=ERRS, sleeps=(42, 506, 507), send_attempts=0, integrity=True, iso_end=True, valid=False, status="run"):
    d = tmp_path / "res"
    d.mkdir()
    (d / "ev.jsonl").write_text("", encoding="utf-8")
    rec = {"run": RUN, "run_id": RID, "kind": "normal", "attempt_number": 1, "status": status, "cleanup_errors": list(errors),
           "integrity": {"ok": integrity, "problems": []}, "power_events": [ev(i) for i in sleeps], "postflight_invalid_reasons": [],
           "runner_started_at_utc": "2026-10-17T14:14:05+00:00", "runner_ended_at_utc": "2026-10-17T15:24:05+00:00",
           "evidence_file": "ev.jsonl", "environment_profile_sha256": "p" * 64, "artifact_hashes": ARTS}
    summ = {"detector_started": True, "observation_started_at_utc": T0, "observation_ended_at_utc": T1,
            "evidence_counts": {"send_attempts": send_attempts}, "old_policy_isolation": {"start": True, "end": iso_end},
            "trial_outcome": {"prober_stop_failed": None, "injector_cleanup_failed": None}}
    (d / f"p2-session-{RUN}.json").write_text(json.dumps(rec), encoding="utf-8")
    (d / f"summary-{RID}.json").write_text(json.dumps(summ), encoding="utf-8")
    (d / f"p2-validity-{RUN}.json").write_text(json.dumps({"run": RUN, "status": "invalid", "valid": valid}), encoding="utf-8")
    (d / f"running-{RID}.json").write_text("{}", encoding="utf-8")
    return d


def live_ok(**over):
    base = {"policy": {"mode": "record_only", "context": None}, "problems": [f"running_marker_left:['running-{RID}.json']"]}
    base.update(over)
    return lambda: base


def load(d, live_fn=None, **cfg):
    return ns1.load_stage1(ns1.NetConfig(results_dir=d, **cfg), verify_freeze=lambda: {"manifest_sha256": "m" * 64}, live_fn=live_fn)


def check(tmp_path, live_fn=live_ok(), **kw):
    d = setup(tmp_path, **kw)
    return rn.make_resume_check(load(d, live_fn), live_fn)(d, RUN)


def test_a_documented_host_sleep_interruption_is_resumable(tmp_path):
    c = check(tmp_path)
    assert c["problems"] == [] and c["facts"]["host_sleep_event_ids"] == [42, 506]            # 507(대기 복귀 계열)은 절전 진입으로 세지 않는다 and c["facts"]["send_attempts"] == 0


@pytest.mark.parametrize("kw,needle", [
    ({"sleeps": ()}, "no_host_sleep_event"),
    ({"sleeps": (1, 507)}, "no_host_sleep_event"),                      # 절전 진입(42·506)이 아닌 이벤트만 있으면 안 된다
    ({"errors": ERRS + ["stress_chaos_residual: ['x']"]}, "cleanup_errors_not_interruption_class"),
    ({"errors": []}, "cleanup_errors_not_interruption_class"),
    ({"errors": ["canonical_restore_failed: x"]}, "cleanup_errors_not_interruption_class"),
    ({"send_attempts": 1}, "signal_send_attempt_present"),
    ({"integrity": False}, "integrity_failure"),
    ({"iso_end": False}, "old_policy_isolation_not_verified"),
    ({"valid": True}, "not_a_terminal_invalid_attempt"),
    ({"status": "refused_profile_invalid"}, "not_a_terminal_invalid_attempt")])
def test_every_condition_is_required(tmp_path, kw, needle):
    c = check(tmp_path, **kw)
    assert any(needle in p for p in c["problems"]), c["problems"]


def test_sleep_outside_the_observation_window_does_not_count(tmp_path):
    d = setup(tmp_path, sleeps=())
    rec = json.loads((d / f"p2-session-{RUN}.json").read_text(encoding="utf-8"))
    early = int(datetime(2026, 10, 17, 14, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)         # 관찰 시작(14:20) 전
    rec["power_events"] = [ev(42, early)]
    (d / f"p2-session-{RUN}.json").write_text(json.dumps(rec), encoding="utf-8")
    assert any("no_host_sleep_event" in p for p in rn.make_resume_check(load(d, live_ok()), live_ok())(d, RUN)["problems"])


def test_live_state_must_be_provided(tmp_path):
    assert any("live_state_not_provided" in p for p in check(tmp_path, live_fn=None)["problems"])


@pytest.mark.parametrize("policy", [{"mode": "act", "context": None}, {"mode": "record_only", "context": {"run": "x"}}])
def test_live_policy_in_act_mode_or_with_context_blocks_the_resume(tmp_path, policy):
    c = check(tmp_path, live_fn=live_ok(policy=policy))
    assert any("live_policy_not_clean_record_only" in p for p in c["problems"])


def test_live_canonical_problems_block_but_this_attempts_own_marker_is_forgiven(tmp_path):
    c = check(tmp_path, live_fn=live_ok(problems=[f"running_marker_left:['running-{RID}.json']", "canonical: phase=Degraded"]))
    assert any("live_state_not_canonical" in p and "Degraded" in p for p in c["problems"])


def test_another_leftover_marker_is_not_forgiven(tmp_path):
    c = check(tmp_path, live_fn=live_ok(problems=["running_marker_left:['running-a.json', 'running-b.json']"]))
    assert any("live_state_not_canonical" in p for p in c["problems"])


def test_profiles_may_differ_only_in_the_preregistration_hash(tmp_path):
    a, b, c = (tmp_path / n for n in "abc")
    a.write_text(json.dumps({"x": {"prereg_sha256": "1", "thr": 0.5}, "l": [1, {"prereg_sha256": "q"}]}), encoding="utf-8")
    b.write_text(json.dumps({"x": {"prereg_sha256": "2", "thr": 0.5}, "l": [1, {"prereg_sha256": "z"}]}), encoding="utf-8")
    c.write_text(json.dumps({"x": {"prereg_sha256": "2", "thr": 0.6}, "l": [1, {"prereg_sha256": "z"}]}), encoding="utf-8")
    assert rn.profiles_equal_except_prereg(a, b) and not rn.profiles_equal_except_prereg(a, c)


def test_the_real_rev1_and_rev2_profiles_differ_only_in_the_document_hash(tmp_path):
    base = HERE / "profiles" / "selection-profile-network-tolerant-vllm-qwen05b-"
    assert rn.profiles_equal_except_prereg(str(base) + "v1.json", str(base) + "v2.json")
    d = json.loads(Path(str(base) + "v2.json").read_text(encoding="utf-8"))
    d["selection"]["rule"] = "x"                                       # selection.rule(문서 해시)은 무시하지만 다른 값 변경은 잡는다
    d["detectors"][1]["threshold"] = 0.123
    tmp = tmp_path / "changed.json"
    tmp.write_text(json.dumps(d), encoding="utf-8")
    assert not rn.profiles_equal_except_prereg(str(base) + "v1.json", tmp)


def authorize_setup(tmp_path, *, thr=0.5):
    d = setup(tmp_path)
    prev, cur = tmp_path / "prev.json", tmp_path / "cur.json"
    prev.write_text(json.dumps({"prereg_sha256": "1", "thr": 0.5}), encoding="utf-8")
    cur.write_text(json.dumps({"prereg_sha256": "2", "thr": thr}), encoding="utf-8")
    m = load(d, live_ok(), selection_profile=cur, previous_selection_profile=prev)
    rec = json.loads((d / f"p2-session-{RUN}.json").read_text(encoding="utf-8"))
    rec["environment_profile_sha256"] = m.pc.text_sha256(prev)
    (d / f"p2-session-{RUN}.json").write_text(json.dumps(rec), encoding="utf-8")
    m.pc.load_profile = lambda *a, **k: None                                  # 프로필 형식 검증은 다른 시험이 한다
    m.pc.verify_artifacts = lambda root: {"hashes": ARTS}
    impl = {"manifest": "m.json", "manifest_sha256": "m" * 64, "selection_profile_sha256": m.pc.text_sha256(cur)}
    return d, m, impl


def test_authorization_unblocks_only_that_attempt_and_is_invalidated_by_any_change(tmp_path):
    d, m, impl = authorize_setup(tmp_path)
    assert "정리 오류" in m.stop_reason(d)                                     # 재개 기록 전: 수집 중단
    out = m.authorize_cleanup_resume(d, RUN, authorized_by="tester", review_note="절전 확인", impl=impl, artifact_root=tmp_path)
    a = json.loads(Path(out).read_text(encoding="utf-8"))
    assert a["user_authorized_resume"] is True and a["attempt_remains_invalid"] and a["attempt_consumed"]
    assert a["detector_results_observed"] is False and a["detector_ran_before_interruption"] is True
    assert a["failure_classification"] == rn.CLASSIFICATION and a["attempt_profile_sha256"] != a["current_profile_sha256"]
    assert m.stop_reason(d) is None                                           # 이 시도의 정리 오류는 풀렸다
    m.check_unresolved_stage1(d)                                              # running 표시도 이 시도만 해소로 본다
    with pytest.raises(FileExistsError):
        m.authorize_cleanup_resume(d, RUN, authorized_by="tester", review_note="again", impl=impl, artifact_root=tmp_path)   # 덮어쓰기 금지
    (d / f"running-{RID}.json").write_text('{"changed": true}', encoding="utf-8")                                              # 표시가 바뀌면 효력 상실
    assert "정리 오류" in m.stop_reason(d)


def test_authorization_is_refused_when_conditions_fail_and_writes_nothing(tmp_path):
    d, m, impl = authorize_setup(tmp_path)
    (d / f"p2-validity-{RUN}.json").write_text(json.dumps({"run": RUN, "status": "valid", "valid": True}), encoding="utf-8")
    with pytest.raises(SystemExit, match="재개 기록 거부"):
        m.authorize_cleanup_resume(d, RUN, authorized_by="t", review_note="n", impl=impl, artifact_root=tmp_path)
    assert not (d / f"p2-resume-authorization-{RUN}.json").exists()


def test_a_replaced_profile_that_differs_beyond_the_prereg_hash_is_refused(tmp_path):
    d, m, impl = authorize_setup(tmp_path, thr=0.9)
    with pytest.raises(SystemExit, match="사전등록 해시만 다른 교체본도 아님"):
        m.authorize_cleanup_resume(d, RUN, authorized_by="t", review_note="n", impl=impl, artifact_root=tmp_path)


def test_artifact_mismatch_is_refused(tmp_path):
    d, m, impl = authorize_setup(tmp_path)
    m.pc.verify_artifacts = lambda root: {"hashes": {"model.pkl": "b" * 64}}
    with pytest.raises(SystemExit, match="artifact"):
        m.authorize_cleanup_resume(d, RUN, authorized_by="t", review_note="n", impl=impl, artifact_root=tmp_path)


def test_cli_requires_every_argument_and_is_registered():
    a = run.build_parser().parse_args(["authorize-resume", "--authorize-resume", RUN, "--authorized-by", "x", "--review-note", "y"])
    assert a.cmd == "authorize-resume" and a.authorize_resume == RUN
    a.authorized_by = ""
    with pytest.raises(SystemExit, match="필요"):
        run.cmd_authorize_resume(a, None, [], lambda: {})
    with pytest.raises(SystemExit):
        run.build_parser().parse_args(["authorize-resume"])


def test_live_facts_reports_policy_and_problems_read_only(monkeypatch):
    monkeypatch.setattr(run, "current_state", lambda reader, dirs: ({"policy": {"mode": "record_only", "context": None}, "template": {}}, {"x": 1}))
    monkeypatch.setattr(run.cl, "session_end_problems", lambda *a, **k: ["s1"])
    monkeypatch.setattr(run, "canonical_problems", lambda snap, d: ["c1"])
    assert run.live_facts(None, []) == {"policy": {"mode": "record_only", "context": None}, "problems": ["s1", "c1"]}
