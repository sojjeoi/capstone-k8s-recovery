"""guarded_bck_analysis 테스트 - 실제 ab90b 원자료(r2 rehearsal, 실제 승격·D6 성공)를 bcNN{b,c,k} 이름·B/C/K 계약 필드로 바꾼
개발용 fixture(공식 자료 아님)와 합성 SLO 값. 원본 ab90 파일은 읽기만 한다."""
import json
import shutil
from datetime import datetime, timedelta

import pytest

import guarded_bck_analysis as an
import run_guarded_bck as gb
import run_guarded_pilot as gp
from test_guarded_ab_analysis import _write_rows

SRC = gp.AB_RESULTS_DIR
pytestmark = pytest.mark.skipif(not (SRC / f"summary-{gp.run_id_for('ab90b')}.json").exists(), reason="ab90 원자료 없음")
FROZEN = an.frozen_values()


def _load():
    rid = gp.run_id_for("ab90b")
    s = json.loads((SRC / f"summary-{rid}.json").read_text(encoding="utf-8"))
    c = json.loads((SRC / f"contract-{rid}.json").read_text(encoding="utf-8"))
    p = json.loads((SRC / f"preflight-{rid}.json").read_text(encoding="utf-8"))
    csvp = gp.PROBE_RESULTS_DIR / f"probe-{rid}-{gp.rt.ARM_LABEL}-{gp.RUNS['ab90b'][2]}-raw.csv"
    return rid, s, c, p, csvp


def _evidence(arm, t_inj, pre_injection=False):
    t = datetime.fromisoformat(t_inj)
    met = (t - timedelta(seconds=5) if pre_injection else t + timedelta(seconds=30)).isoformat()
    rules = {a: {"value": 1.0, "threshold": gb.bd.THRESHOLDS[a], "is_anomalous": True, "consecutive": 3,
                 "condition_met": True, "condition_met_at_utc": met} for a in gb.bd.ARMS}
    return [{"record_type": "c1_live_start", "actuating_rule": arm, "rule_spec_sha256": FROZEN["rule_spec_sha256"]},
            {"record_type": "c1_evaluation", "status": "scored", "actuating_rule": arm, "rules": rules, "condition_met": True}]


class Fix:
    def __init__(self, tmp):
        self.R, self.P = tmp / "results", tmp / "probe"
        self.R.mkdir(parents=True)
        self.P.mkdir()

    def write(self, block="bc01", arms="bck", edit=None, counted=True, rows=None, pre_injection=()):
        for a in arms:
            src_rid, s, c, p, csvp = _load()
            run, arm = block + a, a.upper()
            rid = gb.run_id_for(run)
            ident = FROZEN["identity"][arm]
            comp = {"series": gb.BCK_PLAN_ID, "block": block, "arm": arm, "counted_in_comparison": counted}
            for x in (s, c):
                x.update(run_id=rid, run=run, arm=arm, comparison=dict(comp))
            c.update(plan_id=gb.BCK_PLAN_ID, contract_doc=gb.BCK_CONTRACT_DOC, threshold=ident["threshold"],
                     signal_identity=ident)
            c["conditions"].update(rules={x: {"threshold": gb.bd.THRESHOLDS[x]} for x in gb.bd.ARMS},
                                   rule_spec_sha256=FROZEN["rule_spec_sha256"], if_artifact_hashes=FROZEN["artifact_hashes"],
                                   if_threshold=FROZEN["if_threshold"])
            s["context"] = {**(s.get("context") or {}), **ident, "run_id": rid}
            for act in ((s.get("policy_state_after") or {}).get("actions") or {}).values():
                if act.get("run_id") == src_rid:
                    act["run_id"] = rid
            s["preflight_file"] = f"preflight-{rid}-run-20261007T000000000000Z.json"
            ev = _evidence(arm, s["trial_outcome"]["t_injection_observed_utc"], pre_injection=a in pre_injection)
            if edit:
                edit(a, s, c, p, ev)
            (self.R / s["preflight_file"]).write_text(json.dumps(p), encoding="utf-8")
            (self.R / f"summary-{rid}.json").write_text(json.dumps(s), encoding="utf-8")
            (self.R / f"contract-{rid}.json").write_text(json.dumps(c), encoding="utf-8")
            (gb.gr.live_evidence_path(self.R, rid)).write_text("".join(json.dumps(x) + "\n" for x in ev), encoding="utf-8")
            dst = self.P / f"probe-{rid}-{gp.rt.ARM_LABEL}-{gb.RUNS[run][2]}-raw.csv"
            if rows is not None and a in rows:
                _write_rows(dst, s, rows[a])
            else:
                shutil.copy(csvp, dst)
        return self

    def analyze(self):
        return an.analyze(self.R, self.P)


def _reasons(out):
    return {k: v["validity"]["reasons"] for k, v in out["runs"].items()}


# ---------------- 포함·제외 ----------------
def test_valid_block_all_three_runs_and_pairs(tmp_path):
    out = Fix(tmp_path).write().analyze()["result"]
    assert all(v["validity"]["valid"] for v in out["runs"].values()), _reasons(out)
    assert out["block_status"]["bc01"] == {k: {"status": "valid"} for k in an.COMPARISONS}
    for name in an.COMPARISONS:
        assert out["analysis"][name]["n_valid_pairs"] == 1
    assert out["analysis"]["primary"]["stage"] == "descriptive_only_n_lt_6"
    assert out["analysis"]["exploratory"]["stage"] == "descriptive_only_exploratory"
    v = out["runs"]["bc01c"]["validity"]
    assert v["action_outcome"]["promoted"] is True and v["timing"]["injection_to_trigger_sec"] == 30.0
    assert v["rule_timeline"]["K"]["pre_injection_condition_met"] == 0


@pytest.mark.parametrize("run", ["bc90b", "bc90c", "bc90k", "bc00b", "bc19b", "bc01a", "ab01b", "BC01b", "bc1b"])
def test_non_official_ids_excluded_by_name(run):
    assert an.exclusion_reasons(run, {}) == ["not_official_id"]


def test_pilot_folder_runs_are_excluded(tmp_path):
    out = Fix(tmp_path).write(block="bc90", counted=False).analyze()["result"]
    assert out["runs"] == {} and {x["label"] for x in out["excluded_runs"]} == {"pilot excluded"}
    assert out["attempted_blocks"] == 0


def test_counted_false_excluded(tmp_path):
    out = Fix(tmp_path).write(counted=False).analyze()["result"]
    assert out["runs"] == {} and len(out["excluded_runs"]) == 3


@pytest.mark.parametrize("field", ["summary_arm", "contract_comparison_arm", "mode"])
def test_structured_arm_mismatch_excluded(tmp_path, field):
    def ed(a, s, c, p, ev):
        if a == "c":
            if field == "summary_arm":
                s["arm"] = "B"
            elif field == "contract_comparison_arm":
                c["comparison"]["arm"] = "K"
            else:
                s["mode"] = gp.MODE_RO
    out = Fix(tmp_path).write(edit=ed).analyze()["result"]
    assert any(x["run"] == "bc01c" for x in out["excluded_runs"])
    assert out["block_status"]["bc01"]["primary"]["status"] == "incomplete"
    assert out["block_status"]["bc01"]["secondary"]["status"] == "valid"  # B·K 쌍은 영향 없음


# ---------------- run 유효성 ----------------
@pytest.mark.parametrize("what", ["threshold", "identity", "rules", "spec_sha", "context"])
def test_arm_identity_or_threshold_mismatch_invalid(tmp_path, what):
    def ed(a, s, c, p, ev):
        if a != "k":
            return
        if what == "threshold":
            c["threshold"] = 2.71
        elif what == "identity":
            c["signal_identity"] = FROZEN["identity"]["C"]
        elif what == "rules":
            c["conditions"]["rules"]["C"]["threshold"] = 1.6843
        elif what == "spec_sha":
            c["conditions"]["rule_spec_sha256"] = "0" * 64
        else:
            s["context"]["detector_id"] = "c1_bck_v1_fixed_cal"
    out = Fix(tmp_path).write(edit=ed).analyze()["result"]
    assert out["runs"]["bc01k"]["validity"]["valid"] is False
    assert out["block_status"]["bc01"]["secondary"]["status"] == "invalid" and out["block_status"]["bc01"]["primary"]["status"] == "valid"


@pytest.mark.parametrize("what", ["no_rules", "wrong_actuating", "start_arm", "no_evidence"])
def test_common_tick_evidence_required(tmp_path, what):
    def ed(a, s, c, p, ev):
        if a != "b":
            return
        if what == "no_rules":
            del ev[1]["rules"]
        elif what == "wrong_actuating":
            ev[1]["actuating_rule"] = "C"
        elif what == "start_arm":
            ev[0]["actuating_rule"] = "K"
    f = Fix(tmp_path).write(edit=ed)
    if what == "no_evidence":
        gb.gr.live_evidence_path(f.R, gb.run_id_for("bc01b")).unlink()
    out = f.analyze()["result"]
    assert out["runs"]["bc01b"]["validity"]["valid"] is False, _reasons(out)
    assert any(r.startswith(("V8", "missing:evidence")) for r in out["runs"]["bc01b"]["validity"]["reasons"])


def test_no_promotion_arm_stays_valid_itt(tmp_path):
    """K가 발화하지 않음 - 승격 없음·selector 불변이면 유효(배정 기준 그대로 포함)."""
    def ed(a, s, c, p, ev):
        if a == "k":
            s["watcher"]["action"] = None
            s["policy_state_after"]["actions"] = {}
            s["evidence_counts"]["send_attempts"] = 0
            start = p["canonical_start"]["state"]["active"]
            s["final_promotion_assessment"]["sources"]["live"]["active_selector"] = start
            for j in ev[1]["rules"].values():
                j["condition_met"] = False
    out = Fix(tmp_path).write(edit=ed).analyze()["result"]
    v = out["runs"]["bc01k"]["validity"]
    assert v["valid"] is True, v["reasons"]
    assert v["action_outcome"]["promoted"] is False and v["timing"]["injection_to_trigger_sec"] is None


def test_pre_injection_promotion_excludes_only_pairs_with_that_run_and_is_counted(tmp_path):
    def ed(a, s, c, p, ev):
        if a == "c":
            t = datetime.fromisoformat(s["trial_outcome"]["t_injection_observed_utc"])
            for act in s["policy_state_after"]["actions"].values():
                act["promote_called_at"] = (t - timedelta(seconds=10)).isoformat()
    out = Fix(tmp_path).write(edit=ed, pre_injection="c").analyze()["result"]
    st = out["block_status"]["bc01"]
    assert st["primary"]["status"] == "invalid" and any("pre_injection" in r for r in st["primary"]["reasons"])
    assert st["secondary"]["status"] == "valid" and out["counts"]["pre_injection_promotion_by_arm"]["C"] == 1
    assert out["runs"]["bc01c"]["validity"]["rule_timeline"]["C"]["pre_injection_condition_met"] == 1


def test_observation_problem_invalidates_run(tmp_path):
    def ed(a, s, c, p, ev):
        if a == "b":
            s["cleanup_errors"] = ["harness_error: x"]
    out = Fix(tmp_path).write(edit=ed).analyze()["result"]
    assert out["block_status"]["bc01"]["primary"]["status"] == "invalid"
    assert out["block_status"]["bc01"]["exploratory"]["status"] == "valid"


# ---------------- 효과 방향·통계 ----------------
def _fake_primary(monkeypatch, sec_by_run, lb=()):
    def prim(d, csv):
        return {"computable": True, "reason": None, "cumulative_sec": sec_by_run[d["run"]], "lower_bound": d["run"] in lb,
                "t_slo_first": None, "episodes": [], "recovered_by_end": True, "requests_in_window": 600,
                "max_gap_in_window_sec": 1.0}
    monkeypatch.setattr(an.ab, "primary", prim)


def test_effect_directions(tmp_path, monkeypatch):
    _fake_primary(monkeypatch, {"bc01b": 10.0, "bc01c": 50.0, "bc01k": 200.0})
    a = Fix(tmp_path).write().analyze()["result"]["analysis"]
    assert a["primary"]["pairs"][0]["D_sec"] == 40.0 and a["primary"]["pairs"][0]["C"] == 50.0
    assert a["secondary"]["pairs"][0]["D_sec"] == 190.0 and a["exploratory"]["pairs"][0]["D_sec"] == 150.0
    assert "C − B" in a["primary"]["D"] and "K − B" in a["secondary"]["D"]


def test_lower_bound_drives_sensitivity(tmp_path, monkeypatch):
    _fake_primary(monkeypatch, {"bc01b": 0.0, "bc01c": 0.0, "bc01k": 200.0}, lb=("bc01k",))
    out = Fix(tmp_path).write().analyze()["result"]
    assert out["analysis"]["primary"]["zero"] == 1 and out["sensitivity_without_lower_bound_pairs"]["secondary"]["n_valid_pairs"] == 0
    assert out["sensitivity_without_lower_bound_pairs"]["primary"]["n_valid_pairs"] == 1


def _pairs(ds):
    return [{"block": f"bc{i + 1:02d}", "x_sec": d, "y_sec": 0.0, "D_sec": d, "lower_bound": {}} for i, d in enumerate(ds)]


def test_stages_and_roles():
    five = an.describe(_pairs([1, 2, 3, 4, 5]), "C", "B", False, "primary")
    assert five["stage"] == "descriptive_only_n_lt_6"
    six = an.describe(_pairs([1, 2, 3, 4, 5, 0]), "C", "B", False, "primary")
    assert six["stage"] == "stage1_descriptive" and six["sign_test_reference"]["n_nonzero"] == 5 and six["zero"] == 1
    fin = an.describe(_pairs([1, -2, 3, 4, 5, 6, 0, 8, 9, 10, 11, 12]), "C", "B", True, "primary")
    assert fin["stage"] == "final" and fin["confirmatory"] is True and fin["wilcoxon"]["zeros_dropped"] == 1
    sec = an.describe(_pairs([1] * 12), "K", "B", True, "secondary")
    assert sec["confirmatory"] is False and "확인적 유의성 주장 없음" in sec["note"]
    assert an.describe(_pairs([1] * 12), "K", "C", True, "exploratory")["stage"] == "descriptive_only_exploratory"


def test_final_and_stop_rule_thresholds():
    assert (an.STAGE1_VALID, an.STAGE1_MAX_ATTEMPTS, an.FINAL_VALID, an.FINAL_MAX_ATTEMPTS) == (6, 9, 12, 18)
    assert an.COMPARISONS == {"primary": ("C", "B"), "secondary": ("K", "B"), "exploratory": ("K", "C")}


def test_pair_condition_mismatch_invalidates_pair(tmp_path):
    def ed(a, s, c, p, ev):
        if a == "c":
            c["conditions"]["probe_image_id"] = "sha256:other"
    st = Fix(tmp_path).write(edit=ed).analyze()["result"]["block_status"]["bc01"]
    assert st["primary"]["status"] == "invalid" and st["secondary"]["status"] == "valid"


def test_rerun_same_digest(tmp_path):
    f = Fix(tmp_path).write()
    assert f.analyze()["result_digest"] == f.analyze()["result_digest"]


def test_real_bck_folder_absent_or_pilot_only_has_no_official_blocks():
    out = an.analyze(gb.BCK_RESULTS_DIR, gp.PROBE_RESULTS_DIR)["result"] if gb.BCK_RESULTS_DIR.exists() else None
    assert out is None or out["attempted_blocks"] == 0
