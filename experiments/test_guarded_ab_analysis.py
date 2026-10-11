"""guarded_ab_analysis 테스트 - 실제 ab90 원자료를 ab01 이름으로 복사한 개발용 fixture(공식 자료 아님)와 합성 값.
원본 ab90 파일은 읽기만 한다."""
import csv
import hashlib
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import guarded_ab_analysis as an
import run_guarded_pilot as gp

SRC = gp.AB_RESULTS_DIR
pytestmark = pytest.mark.skipif(not (SRC / f"summary-{gp.run_id_for('ab90b')}.json").exists(), reason="ab90 원자료 없음")


def _load(run):
    rid = gp.run_id_for(run)
    s = json.loads((SRC / f"summary-{rid}.json").read_text(encoding="utf-8"))
    c = json.loads((SRC / f"contract-{rid}.json").read_text(encoding="utf-8"))
    p = json.loads((SRC / f"preflight-{rid}.json").read_text(encoding="utf-8"))  # ab90은 수정 전 legacy 이름
    csvp = gp.PROBE_RESULTS_DIR / f"probe-{rid}-{gp.rt.ARM_LABEL}-{gp.RUNS[run][2]}-raw.csv"
    return s, c, p, csvp


def _obs(s):
    o = s["trial_outcome"]
    return (datetime.fromisoformat(o["observation_ended_at_utc"]) - datetime.fromisoformat(o["t_injection_observed_utc"])).total_seconds()


def _write_rows(dst, s, spec):
    """spec: 주입 관측 시각 기준 전송 시각(초) 목록. 지연 0.1초 성공."""
    t0 = datetime.fromisoformat(s["trial_outcome"]["t_injection_observed_utc"])
    with open(dst, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["experiment_run_id", "scenario", "arm", "repetition", "sent_at", "latency", "status", "success"])
        for x in spec:
            w.writerow(["r", "s", "proposed", 1, (t0 + timedelta(seconds=x)).isoformat(), 0.1, 200, True])


class Fix:
    """ab90a/b 원자료를 ab<NN>a/b로 바꿔 tmp에 쓴다. edit(arm, s, c, p)로 반례를 만든다."""

    def __init__(self, tmp: Path):
        self.R, self.P = tmp / "results", tmp / "probe"
        self.R.mkdir(parents=True)
        self.P.mkdir()

    def write(self, pair="ab01", arms="ab", edit=None, counted=True, rows=None):
        for arm in arms:
            s, c, p, csvp = _load("ab90" + arm)
            src_rid, run = gp.run_id_for("ab90" + arm), pair + arm
            rid = gp.run_id_for(run)
            for x in (s, c):
                x.update(run_id=rid, run=run)
                x["comparison"] = {**x["comparison"], "pair": pair, "counted_in_comparison": counted}
            for a in ((s.get("policy_state_after") or {}).get("actions") or {}).values():
                if a.get("run_id") == src_rid:
                    a["run_id"] = rid
            s["preflight_file"] = f"preflight-{rid}-run-20261007T000000000000Z.json"
            if edit:
                edit(arm, s, c, p)
            (self.R / s["preflight_file"]).write_text(json.dumps(p), encoding="utf-8")
            (self.R / f"summary-{rid}.json").write_text(json.dumps(s), encoding="utf-8")
            (self.R / f"contract-{rid}.json").write_text(json.dumps(c), encoding="utf-8")
            dst = self.P / f"probe-{rid}-{gp.rt.ARM_LABEL}-{gp.RUNS[run][2]}-raw.csv"
            if rows is not None and arm in rows:
                _write_rows(dst, s, rows[arm])
            else:
                shutil.copy(csvp, dst)
        return self

    def analyze(self):
        return an.analyze(self.R, self.P)


# ---------------- 포함·제외 ----------------
def test_real_folder_dry_run_excludes_rehearsals_and_has_no_official_pairs():
    r = an.analyze(gp.AB_RESULTS_DIR, gp.PROBE_RESULTS_DIR)["result"]
    ex = {x["run"]: x for x in r["excluded_runs"]}
    assert {"ab90a", "ab90b"} <= set(ex) and all(ex[k]["label"] == "development/rehearsal excluded" for k in ("ab90a", "ab90b"))
    assert r["runs"] == {} and r["primary_analysis"]["n_valid_pairs"] == 0 and r["attempted_pairs"] == 0


def test_valid_fixture_pair_is_included_and_valid(tmp_path):
    out = Fix(tmp_path).write().analyze()["result"]
    assert out["pair_status"] == {"ab01": {"status": "valid"}}, (out["pair_status"], {k: v["validity"]["reasons"] for k, v in out["runs"].items()})
    for run in ("ab01a", "ab01b"):
        assert out["runs"][run]["validity"]["valid"] is True, out["runs"][run]["validity"]["reasons"]
    b = out["runs"]["ab01b"]["validity"]["b_outcome"]
    assert b["promoted"] is True and b["service_verified"] is True and b["d6"]["restored"] is True
    p = out["primary_analysis"]["pairs"][0]
    assert p["D_sec"] == round(p["A_sec"] - p["B_sec"], 3) and out["primary_analysis"]["stage"] == "descriptive_only_n_lt_5"


@pytest.mark.parametrize("run", ["ab90a", "ab90b", "ab00a", "ab00b", "gap02", "grp02", "ab16a", "ab1a", "AB01a"])
def test_non_official_ids_are_excluded_by_name(run):
    assert an.exclusion_reasons(run, {}) == ["not_official_id"]


def test_counted_false_is_excluded_even_with_official_id(tmp_path):
    out = Fix(tmp_path).write(counted=False).analyze()["result"]
    assert out["runs"] == {} and {x["run"] for x in out["excluded_runs"]} == {"ab01a", "ab01b"}


def test_gap_summary_in_folder_is_never_scanned(tmp_path):
    f = Fix(tmp_path).write()
    (f.R / f"summary-{gp.run_id_for('gap02')}.json").write_text("{}", encoding="utf-8")
    assert set(f.analyze()["result"]["runs"]) == {"ab01a", "ab01b"}


@pytest.mark.parametrize("arms", ["a", "b"])
def test_single_arm_is_incomplete_pair(tmp_path, arms):
    out = Fix(tmp_path).write(arms=arms).analyze()["result"]
    assert out["pair_status"]["ab01"]["status"] == "incomplete" and out["primary_analysis"]["n_valid_pairs"] == 0


# ---------------- arm·메타데이터 ----------------
def test_outcome_reason_text_does_not_affect_arm(tmp_path):
    def ed(arm, s, c, p):
        s["outcome_reason"] = "전환 없음 대조 관찰 600초 완료" if arm == "b" else "IF 전환 완료"
    assert Fix(tmp_path).write(edit=ed).analyze()["result"]["pair_status"]["ab01"]["status"] == "valid"


@pytest.mark.parametrize("field", ["summary_arm", "contract_arm", "mode"])
def test_structured_arm_mismatch_is_analysis_unknown(tmp_path, field):
    def ed(arm, s, c, p):
        if arm == "b":
            if field == "summary_arm":
                s["comparison"]["arm"] = "A_no_switch"
            elif field == "contract_arm":
                c["comparison"]["arm"] = "A_no_switch"
            else:
                s["mode"] = gp.MODE_RO
    out = Fix(tmp_path).write(edit=ed).analyze()["result"]
    ex = {x["run"]: x["reasons"] for x in out["excluded_runs"]}
    assert "ab01b" in ex and any("analysis_unknown_arm" in r for r in ex["ab01b"])
    assert out["pair_status"]["ab01"]["status"] == "incomplete"


@pytest.mark.parametrize("what,where", [("threshold", "V4_model"), ("artifact", "V4_model"), ("contract_doc", "contract_plan_or_doc")])
def test_model_threshold_contract_mismatch_rejected(tmp_path, what, where):
    def ed(arm, s, c, p):
        if arm == "b":
            if what == "threshold":
                c["threshold"] = -0.05
            elif what == "artifact":
                c["artifact_hashes"] = {**c["artifact_hashes"], "model.pkl": "0" * 64}
            else:
                c["contract_doc"] = "docs/design/phase8-v33c1-guarded-ab-comparison-contract-20261007.md"
    out = Fix(tmp_path).write(edit=ed).analyze()["result"]
    txt = json.dumps(out["pair_status"]["ab01"]) + json.dumps(out["excluded_runs"]) + json.dumps(out["runs"].get("ab01b", {}), default=str)
    assert out["pair_status"]["ab01"]["status"] != "valid" and where in txt


def test_b_start_node_mismatch_invalid_pair(tmp_path):
    def ed(arm, s, c, p):
        if arm == "b":
            s["start_active_node"] = "sj-worker2"
    st = Fix(tmp_path).write(edit=ed).analyze()["result"]["pair_status"]["ab01"]
    assert st["status"] == "invalid" and any("start_node" in r for r in st["reasons"])


# ---------------- 유효성 ----------------
@pytest.mark.parametrize("n,ok", [(569, False), (570, True)])
def test_request_count_boundary(tmp_path, n, ok):
    obs = _obs(_load("ab90a")[0])
    spec = [0.5 + i * (obs - 1) / (n - 1) for i in range(n)]
    v = Fix(tmp_path).write(rows={"a": spec}).analyze()["result"]["runs"]["ab01a"]["validity"]
    assert (not any(r.startswith("V2_") for r in v["reasons"])) is ok, v["reasons"]


@pytest.mark.parametrize("gap,ok", [(30.0, True), (30.5, False)])
def test_gap_boundary(tmp_path, gap, ok):
    obs = _obs(_load("ab90a")[0])
    spec = [x * 0.5 for x in range(1, int(obs / 2 / 0.5))]  # 앞 절반 0.5초 간격(요청 수 충분)
    last = spec[-1]
    spec += [last + gap + i * 0.5 for i in range(int((obs - last - gap) / 0.5))]
    v = Fix(tmp_path).write(rows={"a": spec}).analyze()["result"]["runs"]["ab01a"]["validity"]
    assert (not any(r.startswith("V2_") for r in v["reasons"])) is ok, v["reasons"]


def test_missing_stress_evidence_invalid(tmp_path):
    def ed(arm, s, c, p):
        if arm == "a":
            s["cpu_raw_counter_samples"] = None
    v = Fix(tmp_path).write(edit=ed).analyze()["result"]["runs"]["ab01a"]["validity"]
    assert v["valid"] is False and any(r.startswith("V3_") for r in v["reasons"])


def test_b_without_signal_or_promotion_stays_valid(tmp_path):
    def ed(arm, s, c, p):
        if arm == "b":  # 신호·승격 없는 B(정책 거부 포함) - 무효 사유 아님(ITT)
            s["watcher"]["action"], s["watcher"]["guard_result"] = None, None
            s["evidence_counts"]["send_attempts"], s["evidence_counts"]["send_results"] = 0, {}
            s["policy_state_after"]["actions"] = {}
            s["final_promotion_assessment"]["sources"]["live"]["active_selector"] = p["canonical_start"]["state"]["active"]
            s["final_promotion_assessment"]["verdict"] = "not_promoted"
    out = Fix(tmp_path).write(edit=ed).analyze()["result"]
    v = out["runs"]["ab01b"]["validity"]
    assert v["valid"] is True and v["b_outcome"]["promoted"] is False and v["b_outcome"]["signal_sent"] is False, v["reasons"]
    assert out["pair_status"]["ab01"]["status"] == "valid"


def test_unexplained_active_change_in_a_invalid(tmp_path):
    def ed(arm, s, c, p):
        if arm == "a":
            [g for g in s["preview_cleanup_gate"] if g.get("canonical_restore")][-1]["canonical_restore"]["evidence"]["final"]["active"] = "zzz"
    v = Fix(tmp_path).write(edit=ed).analyze()["result"]["runs"]["ab01a"]["validity"]
    assert any(r.startswith("V5_A_end_active") for r in v["reasons"])


def test_unexplained_selector_change_in_b_invalid(tmp_path):
    def ed(arm, s, c, p):
        if arm == "b":
            s["final_promotion_assessment"]["sources"]["live"]["active_selector"] = "zzz"
    v = Fix(tmp_path).write(edit=ed).analyze()["result"]["runs"]["ab01b"]["validity"]
    assert any(r.startswith("V5_unexplained_selector_change") for r in v["reasons"])


def test_malformed_generation_in_a_restore_evidence_is_not_accepted(tmp_path):
    def ed(arm, s, c, p):
        if arm == "a":
            fin = [g for g in s["preview_cleanup_gate"] if g.get("canonical_restore")][-1]["canonical_restore"]["evidence"]["final"]
            fin["observed"] = "0" + str(fin["generation"])
    v = Fix(tmp_path).write(edit=ed).analyze()["result"]["runs"]["ab01a"]["validity"]
    assert any(r.startswith("V5_A_end_active") for r in v["reasons"])


def test_post_observation_cleanup_error_does_not_invalidate_but_is_flagged(tmp_path):
    def ed(arm, s, c, p):
        if arm == "b":
            s["cleanup_errors"] = ["scale_down_delay_restore: old_rs_not_zero_by_deadline"]
            s["scale_down_delay_restore"]["held"] = True
    v = Fix(tmp_path).write(edit=ed).analyze()["result"]["runs"]["ab01b"]["validity"]
    assert v["valid"] is True and v["next_run_independence_risk"] is True and v["b_mechanism_failure"] is True


def test_observation_problem_invalidates(tmp_path):
    def ed(arm, s, c, p):
        if arm == "a":
            s["cleanup_errors"] = ["trial_cleanup_incomplete: prober=x"]
    v = Fix(tmp_path).write(edit=ed).analyze()["result"]["runs"]["ab01a"]["validity"]
    assert v["valid"] is False and any(r.startswith("V7_") for r in v["reasons"])


def test_pre_injection_promotion_excludes_pair(tmp_path):
    def ed(arm, s, c, p):
        if arm == "b":  # 이 실행의 조치만 바꾼다(정책 상태에는 다른 실행의 조치도 있다)
            next(a for a in s["policy_state_after"]["actions"].values() if a.get("run_id") == s["run_id"])["promote_called_at"] = \
                "2000-01-01T00:00:00+00:00"
    out = Fix(tmp_path).write(edit=ed).analyze()["result"]
    assert out["pair_status"]["ab01"]["status"] == "invalid" and out["counts"]["pre_injection_promotion"] == 1


# ---------------- 주 지표·효과 ----------------
@pytest.mark.parametrize("a,b,d", [(100.0, 20.0, 80.0), (0.0, 0.0, 0.0), (20.0, 100.0, -80.0)])
def test_effect_direction_is_a_minus_b(tmp_path, monkeypatch, a, b, d):
    real = an.primary
    monkeypatch.setattr(an, "primary", lambda dd, path: {**real(dd, path), "cumulative_sec": a if dd["run"].endswith("a") else b})
    p = Fix(tmp_path).write().analyze()["result"]["primary_analysis"]["pairs"][0]
    assert p["D_sec"] == d and (p["D_sec"] > 0) == (b < a)


def test_bad_b_result_stays_valid(tmp_path, monkeypatch):
    real = an.primary
    monkeypatch.setattr(an, "primary", lambda dd, path: {**real(dd, path), "cumulative_sec": 500.0 if dd["run"].endswith("b") else 0.0})
    out = Fix(tmp_path).write().analyze()["result"]
    assert out["pair_status"]["ab01"]["status"] == "valid" and out["primary_analysis"]["pairs"][0]["D_sec"] == -500.0


def test_no_slo_violation_is_zero_and_censored_flag_drives_sensitivity(tmp_path, monkeypatch):
    real = an.primary
    monkeypatch.setattr(an, "primary", lambda dd, path: {**real(dd, path), "cumulative_sec": 0.0, "lower_bound": dd["run"].endswith("a")})
    r = Fix(tmp_path).write().analyze()["result"]
    assert r["primary_analysis"]["pairs"][0]["lower_bound"] == {"A": True, "B": False} and r["primary_analysis"]["pairs"][0]["D_sec"] == 0
    assert r["sensitivity_without_lower_bound_pairs"]["n_valid_pairs"] == 0


def test_real_primary_uses_slo_episodes_and_zero_when_no_violation(tmp_path):
    obs = _obs(_load("ab90a")[0])
    spec = [0.5 + i for i in range(int(obs) - 1)]  # 1 RPS, 지연 0.1초 - 위반 없음
    pa = Fix(tmp_path).write(rows={"a": spec}).analyze()["result"]["runs"]["ab01a"]["primary"]
    assert pa["computable"] and pa["cumulative_sec"] == 0.0 and pa["episodes"] == [] and pa["recovered_by_end"] is True


# ---------------- 통계 ----------------
def test_wilcoxon_exact_known_values():
    assert an.wilcoxon_exact(list(range(1, 11)))["p_two_sided"] == pytest.approx(2 / 1024)
    w = an.wilcoxon_exact([0, 0, 3, -3, 5])  # 0 제외, 동점 평균 순위
    assert w["n_nonzero"] == 3 and w["zeros_dropped"] == 2 and w["t_plus"] == 4.5
    assert an.wilcoxon_exact([0, 0])["p_two_sided"] is None


def test_sign_test_hl_and_stages():
    assert an.sign_test_exact([1, 2, 3, 4, 5])["p_two_sided"] == pytest.approx(0.0625)
    assert an.hodges_lehmann([1, 2, 3]) == 2
    mk = lambda ds: [{"pair": f"ab{i:02d}", "A_sec": x, "B_sec": 0.0, "D_sec": x, "lower_bound": {}} for i, x in enumerate(ds, 1)]  # noqa: E731
    assert an.describe(mk([1, 2, 3, 4]), final=True)["stage"] == "descriptive_only_n_lt_5"
    s1 = an.describe(mk([1, 2, 3, 4, 5]), final=False)
    assert s1["stage"] == "stage1_descriptive" and "wilcoxon" not in s1 and s1["B_less"] == 5
    f = an.describe(mk(list(range(1, 11))), final=True)
    assert f["stage"] == "final" and f["wilcoxon"]["p_two_sided"] == pytest.approx(2 / 1024)


# ---------------- 재현·정정 ----------------
def test_rerun_same_digest(tmp_path):
    f = Fix(tmp_path).write()
    assert f.analyze()["result_digest"] == f.analyze()["result_digest"]


def test_corrections_accept_reject_and_never_modify_original(tmp_path):
    f = Fix(tmp_path).write()
    orig = f.R / "verdict.json"
    orig.write_text('{"verdict": "x"}', encoding="utf-8")
    h = hashlib.sha256(orig.read_bytes()).hexdigest()
    (f.R / "corrections").mkdir()
    base = {"original_verdict_path": "verdict.json", "original_verdict_sha256": h, "error_type": "generation_type_comparison",
            "contract_basis": "r2 §5·§6", "corrected_interpretation": "norm_generation", "applies_to": "all_runs"}
    (f.R / "corrections" / "c1.json").write_text(json.dumps(base), encoding="utf-8")
    (f.R / "corrections" / "c2.json").write_text(json.dumps({**base, "original_verdict_sha256": "0" * 64}), encoding="utf-8")
    (f.R / "corrections" / "c3.json").write_text(json.dumps({**base, "applies_to": "ab01b"}), encoding="utf-8")
    (f.R / "corrections" / "c4.json").write_text("{not json", encoding="utf-8")
    c = f.analyze()["result"]["corrections"]
    assert [x["file"] for x in c["accepted"]] == ["c1.json"] and [x["file"] for x in c["rejected"]] == ["c2.json", "c3.json", "c4.json"]
    assert hashlib.sha256(orig.read_bytes()).hexdigest() == h
