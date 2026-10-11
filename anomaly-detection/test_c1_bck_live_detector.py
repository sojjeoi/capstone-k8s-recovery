"""c1_bck_live_detector 오프라인 테스트 - 공통 tick 판정 동치·독립 streak·송신은 arm만·identity가 실제 정책 검사 통과. 네트워크 없음."""
import json
import sys
from pathlib import Path

import pytest

import c1_bck_live_detector as bd
import c1_live_detector as ld
import c1_policy_link as link
import c1_shadow_detector as sd
from score_server import advance_streak
from test_c1_policy_link import EVAL, SNAP

sys.path.append(str(Path(__file__).resolve().parent.parent / "recovery-policy"))
import guarded_schema as gsch  # noqa: E402
from guarded_policy import GuardedPolicy  # noqa: E402

ART = sd.load_artifacts(sd.DEFAULT_ARTIFACTS)
SPEC = bd.load_spec()


def _rec(score, cpu, status="scored"):
    if status != "scored":
        return {"status": status, "skip_reason": "x", "skipped_at_utc": "2026-10-07T00:00:00+00:00"}
    return {"status": "scored", "score": score, "features": {"cpu_mean": cpu},
            "score_completed_at_utc": "2026-10-06T02:53:49+00:00", "window_start_utc": EVAL["window_start_utc"],
            "window_end_utc": EVAL["window_end_utc"], "expected_pods": dict(EVAL["expected_pods"]), "pod_snapshot": SNAP}


def _states():
    return {a: {"consecutive": 0, "last_signal": None} for a in bd.ARMS}


def test_frozen_thresholds_exact_and_spec_matches():
    assert bd.THRESHOLDS == {"B": -0.07667538512242333, "C": 1.6842544293089357, "K": 2.7}
    assert bd.THRESHOLDS["B"] == sd.EXPECTED_THRESHOLD == ART["threshold"]
    calib = json.loads((Path(__file__).resolve().parent.parent / "experiments" / "analysis_v1" /
                        "bck-fixed-threshold-calibration-20261007.json").read_text(encoding="utf-8"))
    assert calib["computed_threshold_exact"] == bd.THRESHOLDS["C"] and calib["calibration_failed"] is False
    assert all(r["signal_count"] == 0 for r in calib["per_session_at_exact"].values())


@pytest.mark.parametrize("arm,key,val", [("C", "threshold", 1.6843), ("K", "threshold", 2.71), ("B", "anomalous_if", "x"),
                                         ("C", "detector_id", "other")])
def test_spec_mismatch_fails_closed(tmp_path, arm, key, val):
    spec = json.loads(bd.RULE_SPEC.read_text(encoding="utf-8"))
    spec["rules"][arm][key] = val
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(RuntimeError, match="fail-closed"):
        bd.load_spec(p)


@pytest.mark.parametrize("arm,T", [("C", 1.6842544293089357), ("K", 2.7)])
def test_fixed_rule_is_strict_cpu_mean_greater_than_threshold(arm, T):
    for cpu, expect in ((T, False), (T - 1e-12, False), (T + 1e-12, True), (T + 1.0, True)):
        r = bd.judge_all(_rec(0.5, cpu), _states(), 0.0)[arm]
        assert r["is_anomalous"] is expect and r["value"] == cpu


def test_if_rule_equals_frozen_step_streak_sequence():
    scores = [0.1, -0.1, -0.1, -0.1, 0.1, -0.2, -0.2, -0.2, -0.2]
    states, frozen = _states(), {"consecutive": 0, "last_signal": None}
    for i, s in enumerate(scores):
        t = 17.0 * i
        mine = bd.judge_all(_rec(s, 1.0), states, t)["B"]
        f = sd.step_streak(_rec(s, 1.0), frozen, sd.EXPECTED_THRESHOLD, t)
        assert (mine["consecutive"], mine["condition_met"]) == (f["consecutive_anomalous"], f["condition_met"])


def test_three_rules_have_independent_streaks_and_cooldowns_from_same_record():
    seq = [(0.5, 1.70), (0.5, 1.70), (-0.2, 1.70), (-0.2, 2.80), (-0.2, 2.80), (-0.2, 2.80)]
    states, met = _states(), {a: [] for a in bd.ARMS}
    for i, (s, cpu) in enumerate(seq):
        out = bd.judge_all(_rec(s, cpu), states, 17.0 * i)
        for a in bd.ARMS:
            met[a].append(out[a]["condition_met"])
    assert met["C"] == [False, False, True, False, False, False]  # 3연속 후 쿨다운 60초 안(17초 간격)
    assert met["B"] == [False, False, False, False, True, False]
    assert met["K"] == [False, False, False, False, False, True]


def test_skip_resets_all_three_rules():
    states = _states()
    for i in range(2):
        bd.judge_all(_rec(-0.2, 3.0), states, 17.0 * i)
    out = bd.judge_all(_rec(None, None, status="skipped"), states, 34.0)
    assert all(out[a]["consecutive"] == 0 and out[a]["condition_met"] is False for a in bd.ARMS)
    assert all(states[a]["consecutive"] == 0 for a in bd.ARMS)
    assert not bd.judge_all(_rec(-0.2, 3.0), states, 51.0)["C"]["condition_met"]


def _run(tmp_path, monkeypatch, arm, recs, send=None):
    it = iter(recs)
    monkeypatch.setattr(bd, "evaluate_once", lambda art, **k: dict(next(it)))
    sends = []

    def send_fn(url, payload, timeout):
        sends.append(payload)
        return (send or (lambda: {"delivery": "delivered", "http_status": 200, "policy_outcome": "would_pass_record_only"}))()
    code = bd.run_live("rid", tmp_path / "ev.jsonl", arm=arm, policy_base_url="http://127.0.0.1:18090/",
                       expected_version="guarded-v1", expected_mode="act", max_evals=len(recs), sleep_fn=lambda s: None,
                       monotonic_fn=iter(range(0, 1000, 17)).__next__,
                       check_fn=lambda *a, **k: {"ok": True, "identity": {}}, send_fn=send_fn)
    return code, sends, [json.loads(x) for x in (tmp_path / "ev.jsonl").read_text(encoding="utf-8").splitlines()]


# B·C·K 모두 이상(점수 -0.2, cpu 3.0) - 어느 arm이든 송신은 자기 규칙 1건뿐
ALL_HOT = [_rec(-0.2, 3.0)] * 3


@pytest.mark.parametrize("arm", bd.ARMS)
def test_only_actuating_rule_is_sent_others_shadow(tmp_path, monkeypatch, arm):
    code, sends, recs = _run(tmp_path, monkeypatch, arm, ALL_HOT)
    assert code == ld.EXIT_OK and len(sends) == 1
    evals = [r for r in recs if r["record_type"] == "c1_evaluation"]
    assert all(r["actuating_rule"] == arm and set(r["rules"]) == set(bd.ARMS) for r in evals)
    assert all(evals[-1]["rules"][a]["condition_met"] for a in bd.ARMS)  # shadow 판정도 기록
    p = sends[0]
    assert p["detector_id"] == bd.IDENTITY[arm][0] and p["threshold"] == bd.THRESHOLDS[arm]
    assert p["score"] == (-0.2 if arm == "B" else 3.0) and p["consecutive_count"] == 3 and p["evaluation_seq"] == 3
    assert recs[0]["actuating_rule"] == arm and recs[0]["rule_spec_sha256"] == SPEC["sha256"]


def test_shadow_rule_firing_never_sends(tmp_path, monkeypatch):
    code, sends, recs = _run(tmp_path, monkeypatch, "K", [_rec(-0.2, 1.9)] * 4)  # B·C 발화, K(2.7) 미발화
    evals = [r for r in recs if r["record_type"] == "c1_evaluation"]
    assert sends == [] and evals[2]["rules"]["B"]["condition_met"] and evals[2]["rules"]["C"]["condition_met"]
    assert not any(r["condition_met"] for r in evals) and "c1_policy_send_attempt" not in {r["record_type"] for r in recs}


def test_post_action_stops_further_sends_but_keeps_three_rule_records(tmp_path, monkeypatch):
    code, sends, recs = _run(tmp_path, monkeypatch, "C", [_rec(-0.2, 3.0)] * 8,
                             send=lambda: {"delivery": "delivered", "policy_outcome": "promoted_verified", "target_key": "t"})
    assert code == ld.EXIT_OK and len(sends) == 1
    assert sum(r["record_type"] == "c1_evaluation" for r in recs) == 8


@pytest.mark.parametrize("sr,code", [({"delivery": "unknown_timeout"}, ld.EXIT_DELIVERY),
                                     ({"delivery": "delivered", "policy_outcome": "promotion_unknown"}, ld.EXIT_POLICY)])
def test_stop_rules_same_as_frozen_detector(tmp_path, monkeypatch, sr, code):
    got, sends, _ = _run(tmp_path, monkeypatch, "K", ALL_HOT + ALL_HOT, send=lambda: dict(sr))
    assert got == code and len(sends) == 1


@pytest.mark.parametrize("arm", bd.ARMS)
def test_payload_passes_real_schema_and_policy_context_check(tmp_path, monkeypatch, arm):
    _, sends, _ = _run(tmp_path, monkeypatch, arm, ALL_HOT)
    sig, err = gsch.parse_signal(sends[0])
    assert err is None, err
    ctx = {"run_id": "rid", **bd.signal_identity(arm, ART["hashes"], SPEC["sha256"])}
    assert GuardedPolicy._context_reasons(None, ctx, sig) == []
    for other in set(bd.ARMS) - {arm}:  # 다른 arm context로는 거부(구조적 식별)
        octx = {"run_id": "rid", **bd.signal_identity(other, ART["hashes"], SPEC["sha256"])}
        assert GuardedPolicy._context_reasons(None, octx, sig) != []


def test_identity_binding():
    b, c, k = (bd.signal_identity(a, ART["hashes"], SPEC["sha256"]) for a in bd.ARMS)
    assert b["model_hash"] == ART["hashes"]["model.pkl"] and b["artifact_manifest_hash"] == link.artifact_manifest_hash(ART["hashes"])
    assert c["model_hash"] == k["model_hash"] == SPEC["sha256"] and c["artifact_manifest_hash"] == k["artifact_manifest_hash"]
    assert len({b["detector_id"], c["detector_id"], k["detector_id"]}) == 3


def test_frozen_modules_untouched_ids():
    assert ld.DETECTOR_ID == "c1_live_v3.3" and sd.DETECTOR_ID == "c1_shadow_v3.3" and link.MODEL_VERSION == "v3.3-c1"
    assert advance_streak(-1.0, 0.0, 2, None, 0.0)["should_signal"] is True
