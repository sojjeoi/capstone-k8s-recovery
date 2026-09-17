#!/usr/bin/env python3
"""collect_metrics.py 검증 - 정상/prevented/timeout/invalid/pilot 5가지
fixture로 분류·시간검증·파생값 계산이 맞는지 확인한다. 전부 tmp_path에
throwaway 파일로 써서 실제 results/를 건드리지 않는다(원본 read-only
원칙은 테스트 자신에도 적용)."""
import csv
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")

from collect_metrics import build_comparison, load_all_results, write_comparison_csv


def _base_row(**overrides) -> dict:
    row = {
        "run_id": "load_ramp-native-01-20260101T000000Z",
        "scenario": "load_ramp",
        "arm": "native",
        "rep": 1,
        "sequence_index": 1,
        "order_seed": 1,
        "t_run_start": "2026-01-01T00:00:00+00:00",
        "is_pilot": False,
        "probe_profile": "inference-max1-rps1",
        "slo_version": "v2",
        "latency_slo_sec": 0.512,
        "probe_rps": 1.0,
        "min_observation_sec": 60.0,
        "t_injection": "2026-01-01T00:01:00+00:00",
        "t_injection_end": "2026-01-01T00:08:00+00:00",
        "t_detection": "2026-01-01T00:01:30+00:00",
        "t_decision": "2026-01-01T00:01:31+00:00",
        "t_api_request": "2026-01-01T00:01:32+00:00",
        "t_switch": "2026-01-01T00:01:35+00:00",
        "t_slo": "2026-01-01T00:02:00+00:00",
        "t_recovery": "2026-01-01T00:03:00+00:00",
        "t_audit_write": "2026-01-01T00:03:05+00:00",
        "t_audit_push": "2026-01-01T00:03:10+00:00",
        "commit_sha": "abc123",
        "detected": True,
        "detection_source": "isolation_forest",
        "action": "promote_preview",
        "promotion_verified": True,
        "outcome": "recovered",
        "slo_evaluable_at_exit": None,  # outcome=recovered면 의미 없음(prevented에서만 씀)
        "injection_valid": True,
        "probe_valid": True,
        "invalid_reason": None,
        "p95_peak": 1.2,
        "availability_min": 0.95,
        "t_run_end": "2026-01-01T00:03:15+00:00",
        "notes": "",
        "state": "completed",
    }
    row.update(overrides)
    return row


def test_normal_recovered_row_included_with_derived_values():
    rows = [_base_row()]
    out_rows, issues = build_comparison(rows)
    assert len(out_rows) == 1
    r = out_rows[0]
    assert r["included_in_main_analysis"] is True
    assert r["exclusion_reason"] is None
    assert r["timing_anomaly"] is False
    assert r["detection_lead_sec"] == 30.0  # t_slo - t_detection = 00:02:00 - 00:01:30
    assert r["action_delay_sec"] == 5.0  # t_switch - t_detection = 00:01:35 - 00:01:30
    assert r["recovery_sec"] == 60.0  # t_recovery - t_slo = 00:03:00 - 00:02:00
    assert r["total_recovery_sec"] == 120.0  # t_recovery - t_injection = 00:03:00 - 00:01:00
    # row 1개만 넣으면 "누락된 rep(2~5)" 교차검증이 정상적으로 같이 뜬다
    # (그 자체는 test_missing_rep_detected가 따로 검증) - 여기서는 이 row
    # 자체에 timing/schema 이슈가 없는지만 확인한다.
    assert not any(i.run_id == r["run_id"] for i in issues)
    print("OK - 정상 recovered row: 포함, 파생값 정확, row 자체 이상 없음")


def test_prevented_missing_t_slo_is_not_an_anomaly():
    # arm은 non-native로, slo_evaluable_at_exit=True로 둬서 이 테스트의
    # 본래 관심사(t_slo 없음 자체는 anomaly가 아님)와 무관한 새 검증(native+
    # prevented, evaluable 미검증)이 같이 걸리지 않게 한다 - 그 둘은 별도
    # 테스트(test_native_arm_prevented_is_flagged 등)에서 확인한다.
    rows = [_base_row(arm="fixed_threshold", outcome="prevented", t_slo=None, t_recovery=None,
                       action="promote_preview", detected=True, slo_evaluable_at_exit=True)]
    out_rows, issues = build_comparison(rows)
    r = out_rows[0]
    assert r["included_in_main_analysis"] is True
    assert r["timing_anomaly"] is False, "prevented는 t_slo 없는 게 정상 - anomaly 아님"
    assert r["detection_lead_sec"] is None
    assert r["recovery_sec"] is None
    assert not any(i.field == "t_slo" for i in issues)
    assert not any(i.run_id == r["run_id"] for i in issues), \
        "arm 비-native + slo_evaluable_at_exit=True면 새 prevented 검증에도 안 걸려야 함"
    print("OK - prevented: t_slo 없음이 anomaly로 안 잡힘")


def test_native_arm_prevented_is_flagged_as_anomaly():
    # 계약서 §3: native arm은 개입이 없어 prevented가 나올 수 없다 - pilot
    # 여부와 무관하게 항상 이상으로 잡혀야 한다(2026-09-17 pod_kill 오판정
    # 사건 재발 방지).
    rows = [_base_row(arm="native", outcome="prevented", t_slo=None, t_recovery=None,
                       slo_evaluable_at_exit=True)]
    _, issues = build_comparison(rows)
    assert any(i.field == "outcome" and "native" in i.problem for i in issues)
    print("OK - arm=native + outcome=prevented가 이상으로 검출됨")


def test_main_experiment_prevented_without_evaluable_true_is_validation_error():
    # 본 실험(is_pilot=False)의 prevented는 slo_evaluable_at_exit=True가
    # 아니면 검증 오류 - probe가 실제로 판정 가능한 데이터를 확보했는지
    # 확인이 안 된 상태이기 때문.
    rows = [_base_row(arm="fixed_threshold", outcome="prevented", t_slo=None, t_recovery=None,
                       is_pilot=False, slo_evaluable_at_exit=None)]
    _, issues = build_comparison(rows)
    assert any(i.field == "slo_evaluable_at_exit" for i in issues)
    print("OK - 본 실험 prevented + slo_evaluable_at_exit!=True가 검증 오류로 잡힘")


def test_pilot_prevented_without_evaluable_true_is_exempt():
    # 파일럿은 옛 하니스로 실행됐을 수 있어(hook 미구현=None) 이 검증에서
    # 제외한다 - 본 실험만 검증 오류로 취급.
    rows = [_base_row(arm="fixed_threshold", outcome="prevented", t_slo=None, t_recovery=None,
                       is_pilot=True, slo_evaluable_at_exit=None)]
    _, issues = build_comparison(rows)
    assert not any(i.field == "slo_evaluable_at_exit" for i in issues)
    print("OK - 파일럿 prevented는 slo_evaluable_at_exit 검증에서 제외됨")


def test_timeout_missing_t_recovery():
    rows = [_base_row(outcome="timeout", t_recovery=None)]
    out_rows, issues = build_comparison(rows)
    r = out_rows[0]
    assert r["included_in_main_analysis"] is True
    assert r["recovery_sec"] is None
    assert r["total_recovery_sec"] is None
    assert r["timing_anomaly"] is False, "timeout에서 t_recovery 없음은 정상"
    print("OK - timeout: t_recovery 없어도 anomaly 아님, 파생값은 None")


def test_invalid_run_excluded_but_kept():
    rows = [_base_row(outcome="invalid_run", invalid_reason="probe 시작 실패",
                       t_slo=None, t_recovery=None)]
    out_rows, issues = build_comparison(rows)
    assert len(out_rows) == 1, "삭제하지 않고 남겨야 함"
    r = out_rows[0]
    assert r["included_in_main_analysis"] is False
    assert r["exclusion_reason"] == "invalid_run"
    assert r["invalid_reason"] == "probe 시작 실패"
    # 실측 버그(2026-09-16): invalid_run은 t_slo 찍히기 전에 조기 중단될 수
    # 있어 prevented와 마찬가지로 t_slo 없음이 정상인데, 처음엔 이걸
    # "측정 누락 의심"으로 잘못 플래그했었다 - 회귀 방지.
    assert r["timing_anomaly"] is False, "invalid_run의 t_slo 없음은 정상 - anomaly 아님"
    assert not any(i.field == "t_slo" for i in issues)
    print("OK - invalid_run: 삭제 안 되고 exclusion_reason과 함께 분류됨, t_slo 없음은 anomaly 아님")


def test_pilot_excluded_but_kept():
    rows = [_base_row(is_pilot=True, notes="ramp intensity calibration")]
    out_rows, issues = build_comparison(rows)
    r = out_rows[0]
    assert r["included_in_main_analysis"] is False
    assert r["exclusion_reason"] == "pilot"
    print("OK - is_pilot=True: exclusion_reason=pilot로 분류, 삭제 안 됨")


def test_preflight_excluded_takes_priority_over_pilot():
    rows = [_base_row(is_pilot=True, notes="PREFLIGHT-EXCLUDED: promotion test")]
    out_rows, _ = build_comparison(rows)
    assert out_rows[0]["exclusion_reason"] == "preflight_excluded"
    print("OK - PREFLIGHT-EXCLUDED가 is_pilot보다 우선 분류됨")


def test_schema_validation_catches_bad_types():
    rows = [_base_row(rep="1", is_pilot="yes")]  # 문자열로 잘못 옴
    _, issues = build_comparison(rows)
    fields_with_issues = {i.field for i in issues}
    assert "rep" in fields_with_issues
    assert "is_pilot" in fields_with_issues
    print("OK - 타입 오류(rep, is_pilot)가 issue로 잡힘")


def test_timing_order_violation_detected():
    rows = [_base_row(t_decision="2026-01-01T00:01:20+00:00")]  # t_detection(00:01:30)보다 이름
    out_rows, issues = build_comparison(rows)
    assert out_rows[0]["timing_anomaly"] is True
    assert any("t_detection" in i.field for i in issues)
    print("OK - 인과순서 위반(t_decision < t_detection)이 timing_anomaly로 잡힘")


def test_duplicate_rep_detected():
    rows = [_base_row(run_id="a"), _base_row(run_id="b")]  # 같은 scenario/arm/rep
    _, issues = build_comparison(rows)
    assert any("중복" in i.problem for i in issues)
    print("OK - 동일 scenario/arm/rep 중복이 issue로 잡힘")


def test_missing_rep_detected():
    rows = [_base_row(run_id=f"r{n}", rep=n) for n in (1, 2, 4)]  # 3, 5 누락
    _, issues = build_comparison(rows)
    missing_issue = next((i for i in issues if "누락된 rep" in i.problem), None)
    assert missing_issue is not None
    assert "3" in missing_issue.problem and "5" in missing_issue.problem
    print("OK - 누락된 rep(3, 5)이 issue로 잡힘")


def test_original_json_files_not_modified(tmp_path):
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    path = results_dir / "trial-test-01.json"
    original_text = json.dumps(_base_row(), ensure_ascii=False, indent=2)
    path.write_text(original_text, encoding="utf-8")

    rows = load_all_results(results_dir)
    out_rows, _ = build_comparison(rows)
    write_comparison_csv(out_rows, results_dir / "comparison.csv")

    assert path.read_text(encoding="utf-8") == original_text, "원본 JSON이 수정됨 - 읽기 전용 위반"
    print("OK - 원본 trial JSON 파일이 수정되지 않음")


def test_comparison_csv_written_correctly(tmp_path):
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    (results_dir / "trial-a.json").write_text(
        json.dumps(_base_row(run_id="a"), ensure_ascii=False), encoding="utf-8")
    pilot_dir = results_dir / "pilot"
    pilot_dir.mkdir()
    (pilot_dir / "trial-b.json").write_text(
        json.dumps(_base_row(run_id="b", is_pilot=True), ensure_ascii=False), encoding="utf-8")

    rows = load_all_results(results_dir)
    assert len(rows) == 2, "본 실험 + 파일럿 둘 다 읽혀야 함"
    out_rows, _ = build_comparison(rows)
    out_path = results_dir / "comparison.csv"
    write_comparison_csv(out_rows, out_path)

    with out_path.open(encoding="utf-8") as f:
        csv_rows = list(csv.DictReader(f))
    assert len(csv_rows) == 2
    included_flags = {r["run_id"]: r["included_in_main_analysis"] for r in csv_rows}
    assert included_flags["a"] == "True"
    assert included_flags["b"] == "False"
    print("OK - comparison.csv에 본 실험+파일럿 모두 기록, included 플래그 정확")


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    test_normal_recovered_row_included_with_derived_values()
    test_prevented_missing_t_slo_is_not_an_anomaly()
    test_native_arm_prevented_is_flagged_as_anomaly()
    test_main_experiment_prevented_without_evaluable_true_is_validation_error()
    test_pilot_prevented_without_evaluable_true_is_exempt()
    test_timeout_missing_t_recovery()
    test_invalid_run_excluded_but_kept()
    test_pilot_excluded_but_kept()
    test_preflight_excluded_takes_priority_over_pilot()
    test_schema_validation_catches_bad_types()
    test_timing_order_violation_detected()
    test_duplicate_rep_detected()
    test_missing_rep_detected()
    with tempfile.TemporaryDirectory() as d:
        test_original_json_files_not_modified(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_comparison_csv_written_correctly(Path(d))
    print("\n모두 통과")
