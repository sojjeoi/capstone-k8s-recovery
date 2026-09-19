#!/usr/bin/env python3
"""collect_metrics.py의 계획된 promotion 해석(계약서 §5.9) 회귀 테스트 - 2026-09-20 network_degrade 3-arm 파일럿의 **실제 결과 JSON**
(docs/design/evidence/network-degrade-pilot/)을 fixture로 쓴다. 원본 파일은 읽기 전용이고(변형은 메모리 복사본에만 한다) 클러스터에
접근하지 않는다.

배경: proposed 파일럿은 stage 1 도중 promotion이 검증됐고(`t_switch` 16:29:51) 어댑터가 다음 stage 경계에서 active pod가 바뀐 것을 관측해
`target_replaced=true`가 됐다. 예전 파생 해석은 그 값만으로 `probe_isolation_held=False`를 냈다 - 재시작 연쇄가 아니라 실험 처치(promotion)가
만든 교체인데도. 이제 promotion으로 설명되는 교체는 unplanned가 아니다."""
import csv
import hashlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import collect_metrics
from collect_metrics import build_comparison, write_comparison_csv

EVIDENCE = Path(__file__).resolve().parent.parent / "docs" / "design" / "evidence" / "network-degrade-pilot"
ARMS = ("native", "fixed_threshold", "proposed")


def pilot_path(arm):
    (path,) = EVIDENCE.glob(f"trial-pilot-network_degrade-{arm}-*.json")
    return path


def pilot(arm, **overrides):
    row = json.loads(pilot_path(arm).read_text(encoding="utf-8"))
    row.update(overrides)
    return row


def derive(row, evidence=None):
    out_rows, issues = build_comparison([row], evidence)
    return out_rows[0], issues


def iso_shift(value, seconds):
    return (datetime.fromisoformat(value) + timedelta(seconds=seconds)).isoformat()


def target_issues(issues):
    return [i for i in issues if i.field == "target_replaced"]


# ---- 실제 파일럿 세 행 -----------------------------------------------------------------------------
def test_the_real_pilot_rows_have_the_recorded_shape():
    """fixture가 기대한 그 파일럿인지 고정한다 - 이 테스트가 깨지면 아래 해석 테스트의 전제가 바뀐 것이다."""
    native, fixed, proposed = (pilot(a) for a in ARMS)
    assert [r["arm"] for r in (native, fixed, proposed)] == list(ARMS)
    assert all(r["is_pilot"] and r["readiness_probe_profile"] == "network_tolerant" and r["readiness_probe_timeout_sec"] == 11.0
               for r in (native, fixed, proposed))
    assert (native["target_replaced"], fixed["target_replaced"], proposed["target_replaced"]) == (False, False, True)
    assert native["promotion_verified"] is None and native["action"] == "none" and native["t_switch"] is None
    assert fixed["promotion_verified"] is True and fixed["t_switch"] and proposed["promotion_verified"] is True
    order = [datetime.fromisoformat(proposed[k]) for k in ("t_api_request", "t_switch", "t_target_replaced")]
    assert order == sorted(order), "promotion 요청 < 전환 < 교체 관측(어댑터가 stage 경계에서 확인) 순"


def test_the_three_real_pilot_rows_do_not_report_a_false_probe_isolation_failure():
    out_rows, issues = build_comparison([pilot(a) for a in ARMS])
    by_arm = {r["arm"]: r for r in out_rows}
    assert [by_arm[a]["target_change_kind"] for a in ARMS] == ["none", "none", "planned_promotion"]
    assert all(by_arm[a]["probe_isolation_held"] is True for a in ARMS), "예전 해석은 proposed에서 False였다"
    assert all(by_arm[a]["restart_chain_observed"] is None for a in ARMS), "network_tolerant profile에서는 해당 없음"
    assert by_arm["proposed"]["target_replaced"] is True, "원본 필드는 그대로 - 해석만 바뀐다"
    assert issues == [], "세 파일럿 행은 validation issue가 없다"


def test_the_proposed_replacement_is_explained_by_the_verified_promotion():
    out, issues = derive(pilot("proposed"))
    assert out["target_change_kind"] == "planned_promotion"
    assert "vllm-serving-579d5d6dfb-wc6v4" in out["target_change_reason"] and "t_api_request" in out["target_change_reason"]
    assert out["outcome"] == "recovered" and not target_issues(issues), "해석은 outcome을 바꾸지 않는다"


def test_the_same_planned_promotion_is_not_a_restart_chain_in_the_default_profile_either():
    out, _ = derive(pilot("proposed", readiness_probe_profile="default", readiness_probe_timeout_sec=1.0))
    assert out["target_change_kind"] == "planned_promotion"
    assert out["restart_chain_observed"] is False and out["probe_isolation_held"] is None


def test_a_replacement_observed_before_the_promotion_request_is_unplanned():
    """promotion 요청보다 앞서 관측된 교체는 promotion으로 설명되지 않는다 - 재시작 연쇄·교체 후보다."""
    row = pilot("proposed")
    early = iso_shift(row["t_api_request"], -10)
    out, issues = derive({**row, "t_target_replaced": early})
    assert out["target_change_kind"] == "unplanned" and "promotion과 별개" in out["target_change_reason"]
    assert out["probe_isolation_held"] is False and not target_issues(issues)
    out, _ = derive({**row, "t_target_replaced": early, "readiness_probe_profile": "default", "readiness_probe_timeout_sec": 1.0})
    assert out["restart_chain_observed"] is True and out["probe_isolation_held"] is None


def test_a_replacement_without_any_promotion_is_still_unplanned():
    """promotion이 없는 run(native 등)의 교체는 예전과 같이 unplanned다."""
    changed = {"target_replaced": True, "t_target_replaced": "2026-09-19T15:50:00+00:00",
               "target_replacement_pod_name": "vllm-serving-x-y", "target_replacement_pod_uid": "u"}
    out, issues = derive(pilot("native", **changed))
    assert out["target_change_kind"] == "unplanned" and "promotion 없음" in out["target_change_reason"]
    assert out["probe_isolation_held"] is False and not target_issues(issues)
    out, _ = derive(pilot("native", **changed, readiness_probe_profile="default", readiness_probe_timeout_sec=1.0))
    assert out["restart_chain_observed"] is True


@pytest.mark.parametrize("field,value", [
    ("promotion_verified", None), ("promotion_verified", False),   # 요청은 됐는데 검증 결과가 없거나 실패
    ("t_switch", None), ("t_api_request", None),                    # 불완전
    ("action", "none"),                                             # t_switch는 있는데 promote 조치가 아니라는 모순
    ("t_target_replaced", None),                                    # 교체와 promotion의 선후를 모름
    ("target_replacement_pod_name", None), ("target_replacement_pod_uid", None),   # 교체 pod 식별 불가
])
def test_incomplete_or_contradictory_promotion_info_is_never_guessed(field, value):
    for profile, timeout in (("network_tolerant", 11.0), ("default", 1.0)):
        out, issues = derive(pilot("proposed", **{field: value}, readiness_probe_profile=profile,
                                   readiness_probe_timeout_sec=timeout))
        assert out["target_change_kind"] == "indeterminate", (field, value)
        assert out["probe_isolation_held"] is None and out["restart_chain_observed"] is None, "True/False를 추정하지 않는다"
        assert any("구분할 수 없음" in i.problem for i in target_issues(issues)), "validation issue를 남긴다"


def test_a_causal_order_contradiction_between_request_and_switch_is_not_guessed():
    row = pilot("proposed")
    out, issues = derive({**row, "t_switch": iso_shift(row["t_api_request"], -5)})
    assert out["target_change_kind"] == "indeterminate" and "t_switch < t_api_request" in out["target_change_reason"]
    assert any(i.field == "t_api_request/t_switch" for i in issues), "기존 timing anomaly 검사도 그대로 잡는다"


def test_without_an_observed_replacement_contradictory_promotion_info_does_not_matter():
    """교체가 없으면 설명할 것이 없다 - promotion 정보가 어긋나도 해석은 none(이 어긋남은 기존 검사가 따로 드러낸다)."""
    out, issues = derive(pilot("fixed_threshold", promotion_verified=None))
    assert out["target_change_kind"] == "none" and out["probe_isolation_held"] is True and not target_issues(issues)


def test_other_profiles_and_old_results_are_not_applicable():
    out, issues = derive(pilot("proposed", readiness_probe_profile=None, readiness_probe_timeout_sec=None))
    assert out["target_change_kind"] == "not_applicable"
    assert out["probe_isolation_held"] is None and out["restart_chain_observed"] is None and not target_issues(issues)
    old = pilot("native")
    for field in ("readiness_probe_profile", "target_replaced", "t_target_replaced"):
        old.pop(field)
    out, _ = derive(old)
    assert out["target_change_kind"] == "not_applicable" and out["probe_isolation_held"] is None


# ---- pod 수준 증거(promotion과 별개의 restart·UID 교체) -----------------------------------------------
@pytest.mark.parametrize("evidence", [{"restarts": 2}, {"uid_replaced": True},
                                      {"target_lost_before_promotion": "2026-09-19T16:29:00+00:00"}])
def test_separate_pod_evidence_of_restart_or_replacement_overrides_a_planned_promotion(evidence):
    row = pilot("proposed")
    out, issues = derive(row, {row["run_id"]: evidence})
    assert out["target_change_kind"] == "unplanned" and "promotion과 별개의 pod 증거" in out["target_change_reason"]
    assert out["probe_isolation_held"] is False, "promotion으로 가리지 않는다"
    assert not target_issues(issues)


def test_pod_evidence_also_catches_a_replacement_the_adapter_never_observed():
    row = pilot("fixed_threshold")    # 마지막 stage 도중 promotion - 어댑터는 교체를 관측하지 못해 target_replaced=false
    out, issues = derive(row, {row["run_id"]: {"restarts": 1}})
    assert out["target_replaced"] is False and out["target_change_kind"] == "unplanned" and out["probe_isolation_held"] is False
    assert any("관측하지 못한 교체" in i.problem for i in target_issues(issues)), "두 출처의 불일치를 issue로 드러낸다"


def test_pod_evidence_without_restart_or_replacement_changes_nothing():
    row = pilot("proposed")
    out, issues = derive(row, {row["run_id"]: {"restarts": 0, "uid_replaced": False, "target_lost_before_promotion": None}})
    assert out["target_change_kind"] == "planned_promotion" and out["probe_isolation_held"] is True and not target_issues(issues)
    out, _ = derive(row, {"some-other-run": {"restarts": 5}})
    assert out["target_change_kind"] == "planned_promotion", "다른 run의 증거는 무관"


# ---- prevented 오해 방지 issue와의 관계 --------------------------------------------------------------
def test_the_prevented_misleading_issue_fires_only_for_an_unplanned_replacement():
    planned, issues = derive(pilot("proposed", outcome="prevented", slo_evaluable_at_exit=True))
    assert planned["target_change_kind"] == "planned_promotion"
    assert not any("promotion으로 설명되지 않는" in i.problem for i in issues), "계획된 promotion 뒤의 prevented는 오해 소지가 아니다"
    row = pilot("proposed", outcome="prevented", slo_evaluable_at_exit=True)
    unplanned, issues = derive({**row, "t_target_replaced": iso_shift(row["t_api_request"], -10)})
    assert unplanned["target_change_kind"] == "unplanned"
    assert any("promotion으로 설명되지 않는" in i.problem and "probe_isolation_held" in i.problem for i in issues)
    assert unplanned["outcome"] == "prevented" and unplanned["included_in_main_analysis"] is False, "outcome·포함 여부는 그대로"


# ---- 원본 불변과 CSV ----------------------------------------------------------------------------------
def test_the_original_pilot_json_files_are_never_modified_and_the_csv_carries_the_new_columns(tmp_path):
    before = {a: hashlib.sha256(pilot_path(a).read_bytes()).hexdigest() for a in ARMS}
    out_rows, _ = build_comparison([pilot(a) for a in ARMS])
    write_comparison_csv(out_rows, tmp_path / "comparison.csv")
    assert {a: hashlib.sha256(pilot_path(a).read_bytes()).hexdigest() for a in ARMS} == before, "원본 JSON은 읽기 전용"
    header, *lines = (tmp_path / "comparison.csv").read_text(encoding="utf-8").splitlines()
    columns = header.split(",")
    assert "target_change_kind" in columns and "target_change_reason" in columns
    assert columns.index("target_change_kind") < columns.index("restart_chain_observed") < columns.index("probe_isolation_held")
    assert len(lines) >= 3
    trial_fields = {"run_id", "scenario", "arm", "target_replaced", "promotion_verified", "t_switch"}
    assert trial_fields <= set(json.loads(pilot_path("proposed").read_text(encoding="utf-8"))), "TrialResult 필드는 손대지 않았다"


def test_cli_reads_optional_pod_evidence_and_writes_the_interpretation(tmp_path, monkeypatch):
    results = tmp_path / "results"
    (results / "pilot").mkdir(parents=True)
    row = pilot("proposed")
    (results / "pilot" / "trial-proposed.json").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({row["run_id"]: {"restarts": 1}}), encoding="utf-8")
    out = tmp_path / "comparison.csv"
    monkeypatch.setattr(sys, "argv", ["collect_metrics.py", "--results-dir", str(results), "--out", str(out),
                                      "--pod-evidence", str(evidence)])
    collect_metrics.main()
    (csv_row,) = list(csv.DictReader(out.open(encoding="utf-8")))
    assert csv_row["target_change_kind"] == "unplanned" and csv_row["probe_isolation_held"] == "False"
    monkeypatch.setattr(sys, "argv", ["collect_metrics.py", "--results-dir", str(results), "--out", str(out)])
    collect_metrics.main()
    (csv_row,) = list(csv.DictReader(out.open(encoding="utf-8")))
    assert csv_row["target_change_kind"] == "planned_promotion" and csv_row["probe_isolation_held"] == "True"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
