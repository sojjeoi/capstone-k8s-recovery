"""c1_bck_shadow_pair - 동결 judge_all·load_spec를 그대로 쓰고, 신호를 어디에도 보내지 않으며, B·C·K를 같은 레코드로 기록한다."""
import json
from datetime import datetime, timedelta, timezone

import pytest

import c1_bck_live_detector as bd
import c1_bck_shadow_pair as sp

T = datetime(2026, 10, 9, tzinfo=timezone.utc)


def _fake_eval(seq_values):
    """seq_values: [(status, score, cpu_mean)] - 동결 evaluate_once 대신 레코드만 돌려준다."""
    it = iter(seq_values)

    def ev(art, **_):
        st, sc, cpu = next(it)
        n = ev.n = getattr(ev, "n", 0) + 1
        t = T + timedelta(seconds=17 * n)
        if st != "scored":
            return {"status": "skipped", "skip_reason": "input_incomplete", "eval_started_at_utc": t.isoformat(),
                    "skipped_at_utc": t.isoformat()}
        return {"status": "scored", "score": sc, "features": {"cpu_mean": cpu}, "eval_started_at_utc": t.isoformat(),
                "score_completed_at_utc": t.isoformat()}
    return ev


def _run(tmp_path, values, **kw):
    p = tmp_path / "ev.jsonl"
    mono = iter(range(0, 10_000, 17))
    rc = sp.run_shadow("rid", p, max_evals=len(values), evaluate_fn=_fake_eval(values), sleep_fn=lambda s: None,
                       monotonic_fn=lambda: next(mono), now_fn=lambda: T, **kw)
    return rc, [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]


def test_records_three_rules_same_tick_without_any_send(tmp_path, monkeypatch):
    monkeypatch.setattr(bd.link, "send", lambda *a, **k: pytest.fail("송신 경로가 불렸다"))
    hi, lo = 1.70, 1.60  # C 임계값 1.6842544293089357 위·아래
    rc, recs = _run(tmp_path, [("scored", -0.2, hi)] * 3 + [("scored", 0.1, lo)])
    assert rc == sp.EXIT_OK
    start, evals = recs[0], recs[1:]
    assert start["record_type"] == "c1_shadow_start" and start["signal_send"] is False and start["actuating_rule"] is None
    assert start["thresholds"] == {"B": -0.07667538512242333, "C": 1.6842544293089357, "K": 2.7}
    assert start["rule_spec_sha256"] == bd.load_spec()["sha256"]
    assert [r["evaluation_seq"] for r in evals] == [1, 2, 3, 4]
    assert all(set(r["rules"]) == {"B", "C", "K"} and r["signal_send"] is False for r in evals)
    assert [r["rules"]["B"]["condition_met"] for r in evals] == [False, False, True, False]
    assert [r["rules"]["C"]["condition_met"] for r in evals] == [False, False, True, False]
    assert not any(r["rules"]["K"]["condition_met"] for r in evals)
    assert not any(r.get("record_type", "").startswith("c1_policy") for r in recs)


def test_skip_resets_every_rule_streak(tmp_path):
    rc, recs = _run(tmp_path, [("scored", -0.2, 1.7)] * 2 + [("skipped", None, None)] + [("scored", -0.2, 1.7)] * 3)
    met = [(r["rules"]["B"]["condition_met"], r["rules"]["C"]["condition_met"]) for r in recs[1:]]
    assert met == [(False, False)] * 5 + [(True, True)]


def test_cooldown_and_independent_states_match_frozen_judge_all(tmp_path):
    vals = [("scored", -0.2, 1.7)] * 8
    _, recs = _run(tmp_path, vals)
    states = {a: {"consecutive": 0, "last_signal": None} for a in bd.ARMS}
    expect = [bd.judge_all({"status": "scored", "score": -0.2, "features": {"cpu_mean": 1.7},
                            "score_completed_at_utc": "x"}, states, 17 * i)["C"]["condition_met"] for i in range(8)]
    assert [r["rules"]["C"]["condition_met"] for r in recs[1:]] == expect


def test_spec_mismatch_fails_closed(tmp_path):
    bad = tmp_path / "spec.json"
    spec = json.loads(bd.RULE_SPEC.read_text(encoding="utf-8"))
    spec["rules"]["C"]["threshold"] = 1.7
    bad.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(RuntimeError, match="fail-closed"):
        _run(tmp_path, [("scored", 0.1, 1.6)], spec_path=bad)
