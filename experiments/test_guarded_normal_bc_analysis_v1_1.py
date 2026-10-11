"""N1 v1.1 amendment - 신호 귀속 인프라 창 [max(t0, t−60), t+600] 반례 7개 + 진단 보존 + 재-pilot ID. no-grace 유지 확인."""
import guarded_normal_bc_analysis as na
import run_guarded_normal_bc as rn
from test_guarded_normal_bc_analysis import at, arm, cls, _write_session


def test_1_pre_t0_warning_makes_neither_early_arm_indeterminate():
    """v1 재현 반례: Warning t0−20, B t0+30, C t0+80 → v1에서는 B만 indeterminate(비대칭). v1.1은 둘 다 unnecessary."""
    assert cls(30, infra=[-20]) == "unnecessary" and cls(80, infra=[-20]) == "unnecessary"
    b, c = arm([30], infra_events=[at(-20)]), arm([80], infra_events=[at(-20)])
    assert (b["t_safe_sec"], c["t_safe_sec"]) == (30, 80)


def test_2_post_t0_warning_inside_lookback_is_indeterminate():
    assert cls(30, infra=[10]) == "indeterminate"


def test_3_old_pre_t0_warning_no_effect():
    assert cls(30, infra=[-100]) == "unnecessary"


def test_4_post_t0_warning_outside_60s_lookback_no_effect():
    assert cls(80, infra=[10]) == "unnecessary" and cls(80, infra=[20]) == "indeterminate"  # 경계 t−60 포함


def test_5_input_skip_only_before_t0_does_not_contaminate(tmp_path):
    """첫 평가 skip(t0 전) → t0 = 두 번째 평가. t0 직후 B 신호는 t0 전 skip 때문에 indeterminate가 되지 않는다(진단엔 보존)."""
    _write_session(tmp_path, "nb01", b_sig=(34,), skip_at=(0,))
    s = na.analyze(tmp_path)["sessions"][0]
    assert s["status"] == "normal" and s["t0_utc"] == at(17).isoformat()
    assert s["arms"]["B"]["signals"][0]["class"] == "unnecessary" and s["arms"]["B"]["t_safe_sec"] == 17
    pre = [e for e in s["infra_events"] if e["pre_t0_diagnostic_only"]]
    assert len(pre) == 1 and pre[0]["what"].startswith("input_skip")


def test_6_input_skip_after_t0_inside_window_stays_indeterminate(tmp_path):
    _write_session(tmp_path, "nb01", b_sig=(1000,), skip_at=(1100,))
    s = na.analyze(tmp_path)["sessions"][0]
    assert s["arms"]["B"]["signals"][0]["class"] == "indeterminate" and s["arms"]["B"]["t_safe_sec"] == 3600
    assert not any(e["pre_t0_diagnostic_only"] for e in s["infra_events"])


def test_7_signal_right_after_t0_counts_without_grace():
    o = arm([0, 1])
    assert o["raw_trigger_count"] == 2 and o["t_safe_sec"] == 0 and o["first_raw_trigger_sec"] == 0
    assert cls(0) == "unnecessary"


def test_pre_t0_warning_is_kept_as_diagnostic(tmp_path):
    _write_session(tmp_path, "nb01", b_sig=(30,), c_sig=(80,), warn_at=(-20,))
    r = na.analyze(tmp_path)
    s = r["sessions"][0]
    assert [x["class"] for x in s["arms"]["B"]["signals"] + s["arms"]["C"]["signals"]] == ["unnecessary", "unnecessary"]
    assert {"t_rel_sec": -20, "what": "k8s_warning:Unhealthy", "pre_t0_diagnostic_only": True} in s["infra_events"]
    assert r["analyzer_revision"] == "n1-v1.1" and r["windows"]["infra_window"] == "[max(t0, t-60s), t+600s]"
    assert any("evidence_counts.condition_met" in x for x in r["known_limitations"])


def test_repilot_id_nb91_is_pilot_and_excluded(tmp_path):
    assert rn.RUNS["nb91"] == (91, 2091) and rn.check_schedule("nb91", tmp_path) == {
        "session": 91, "required_before": [], "counted": False}
    _write_session(tmp_path, "nb91", obs=600)
    r = na.analyze(tmp_path)
    assert r["attempted"] == 0 and [x["run"] for x in r["excluded"]] == ["nb91"]
