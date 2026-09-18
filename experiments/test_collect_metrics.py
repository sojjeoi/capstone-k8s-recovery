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
        "timing_schema_version": "v2",
        "t_injection_request": "2026-01-01T00:00:58+00:00",
        "t_injection_last_seen": "2026-01-01T00:00:59+00:00",
        "t_injection_observed": "2026-01-01T00:01:00+00:00",
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
    assert r["temporal_relation"] == "post_injection"
    print("OK - 정상 recovered row: 포함, 파생값 정확, row 자체 이상 없음")


def test_temporal_relation_post_injection():
    # t_slo(observed_at 기준)가 t_injection_observed 이후 -> post_injection
    # (기본 fixture와 동일 - 명시적으로 하나 더 확인).
    rows = [_base_row(t_slo="2026-01-01T00:02:00+00:00")]
    out_rows, _ = build_comparison(rows)
    assert out_rows[0]["temporal_relation"] == "post_injection"
    print("OK - t_slo >= t_injection_observed -> post_injection")


def test_temporal_relation_pre_injection():
    # t_slo가 주입 구간 하한(t_injection_last_seen)보다도 이르면 -> pre_injection.
    rows = [_base_row(t_slo="2026-01-01T00:00:00+00:00")]  # last_seen(00:00:59)보다 이름
    out_rows, _ = build_comparison(rows)
    assert out_rows[0]["temporal_relation"] == "pre_injection"
    print("OK - t_slo < 주입 구간 하한 -> pre_injection")


def test_temporal_relation_temporally_ambiguous():
    # t_slo가 주입 구간(하한~상한) 안에 있으면 -> temporally_ambiguous.
    rows = [_base_row(t_slo="2026-01-01T00:00:59.500000+00:00")]  # last_seen(00:00:59)~observed(00:01:00) 사이
    out_rows, _ = build_comparison(rows)
    assert out_rows[0]["temporal_relation"] == "temporally_ambiguous"
    print("OK - t_slo가 주입 구간 안 -> temporally_ambiguous")


def test_temporal_relation_unknown_when_injection_span_missing():
    # t_injection_last_seen/request/observed가 전부 없으면(v1 결과 등)
    # 판정 근거가 없어 unknown.
    rows = [_base_row(t_injection_request=None, t_injection_last_seen=None, t_injection_observed=None)]
    out_rows, _ = build_comparison(rows)
    assert out_rows[0]["temporal_relation"] == "unknown"
    print("OK - 주입 구간 정보가 없으면 temporal_relation=unknown")


def test_temporal_relation_uses_request_when_last_seen_missing():
    # last_seen이 없으면(첫 poll에서 이미 사라짐) request를 하한으로 쓴다.
    rows = [_base_row(t_injection_last_seen=None,
                       t_slo="2026-01-01T00:00:58.500000+00:00")]  # request(00:00:58)~observed(00:01:00) 사이
    out_rows, _ = build_comparison(rows)
    assert out_rows[0]["temporal_relation"] == "temporally_ambiguous"
    print("OK - last_seen 없으면 request를 하한으로 사용")


def test_injection_timestamps_consistency_violation_detected():
    # t_injection_last_seen이 t_injection_request보다 이르면(모순) issue로 남는다.
    rows = [_base_row(t_injection_request="2026-01-01T00:01:00+00:00",
                       t_injection_last_seen="2026-01-01T00:00:00+00:00",  # request보다 이름 - 모순
                       t_injection_observed="2026-01-01T00:01:05+00:00")]
    _, issues = build_comparison(rows)
    assert any("t_injection_last_seen" in i.field and "t_injection_request" in i.field for i in issues)
    print("OK - 주입 3시각의 순서 모순이 issue로 검출됨")


def test_v1_result_without_new_timing_fields_reads_without_error():
    # 새 필드(t_injection_request/last_seen/observed, timing_schema_version)가
    # 아예 없는 v1 결과도 오류 없이 읽혀야 한다(2026-09-18 요건).
    row = _base_row()
    for f in ("t_injection_request", "t_injection_last_seen", "t_injection_observed",
              "timing_schema_version"):
        del row[f]
    out_rows, issues = build_comparison([row])
    assert len(out_rows) == 1
    r = out_rows[0]
    assert r["timing_schema_version"] is None
    assert r["temporal_relation"] == "unknown"  # 주입 구간 정보가 없어 판정 불가
    assert not any("t_injection_request" in i.field or "t_injection_last_seen" in i.field for i in issues)
    print("OK - 신규 타임스탬프 필드 없는 v1 결과도 오류 없이 읽힘(timing_schema_version=None, temporal_relation=unknown)")


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


def test_default_profile_replaced_is_restart_chain_observed():
    rows = [_base_row(readiness_probe_profile="default", readiness_probe_timeout_sec=1.0,
                       target_replaced=True, t_target_replaced="2026-01-01T00:01:10+00:00",
                       target_replacement_pod_name="vllm-def456", target_replacement_pod_uid="uid-2")]
    out_rows, _ = build_comparison(rows)
    r = out_rows[0]
    assert r["restart_chain_observed"] is True
    assert r["probe_isolation_held"] is None, "default profile에서는 해당 없음(None)이어야 함"
    assert r["outcome"] == "recovered", "target_replaced가 outcome을 바꾸면 안 됨"
    print("OK - default profile + target_replaced=true -> restart_chain_observed=true, outcome 불변")


def test_tolerant_profile_replaced_is_probe_isolation_not_held():
    rows = [_base_row(readiness_probe_profile="network_tolerant", readiness_probe_timeout_sec=10.0,
                       target_replaced=True, t_target_replaced="2026-01-01T00:01:10+00:00",
                       target_replacement_pod_name="vllm-def456", target_replacement_pod_uid="uid-2")]
    out_rows, _ = build_comparison(rows)
    r = out_rows[0]
    assert r["probe_isolation_held"] is False
    assert r["restart_chain_observed"] is None, "network_tolerant profile에서는 해당 없음(None)이어야 함"
    print("OK - network_tolerant profile + target_replaced=true -> probe_isolation_held=false")


def test_tolerant_profile_not_replaced_is_probe_isolation_held():
    rows = [_base_row(readiness_probe_profile="network_tolerant", readiness_probe_timeout_sec=10.0,
                       target_replaced=False)]
    out_rows, _ = build_comparison(rows)
    assert out_rows[0]["probe_isolation_held"] is True
    print("OK - network_tolerant profile + target_replaced=false -> probe_isolation_held=true")


def test_tolerant_profile_replaced_prevented_flagged_as_misleading():
    # probe_isolation_held=false인데 outcome=prevented로만 남으면 "설정이
    # 열화를 견뎠다"로 오해할 위험 - 별도 issue로 남아야 한다. 이 issue는
    # 순수 분석·해석용 표시일 뿐이다 - 하네스 오류가 아니라 tolerant probe
    # 설정이 재시작을 막지 못한 유효한 실험 결과이므로, outcome을 바꾸거나
    # 본 분석에서 제외하면 안 된다(2026-09-18 재확인 - issues 리스트는
    # build_comparison() 안에서 included_in_main_analysis/exclusion_reason/
    # outcome 계산에 전혀 쓰이지 않는다는 걸 이 테스트로 고정한다).
    rows = [_base_row(arm="fixed_threshold", readiness_probe_profile="network_tolerant",
                       target_replaced=True, outcome="prevented", t_slo=None, t_recovery=None,
                       slo_evaluable_at_exit=True)]
    out_rows, issues = build_comparison(rows)
    assert any("probe_isolation_held" in i.problem for i in issues)
    r = out_rows[0]
    assert r["outcome"] == "prevented", "경고가 outcome을 바꾸면 안 됨"
    assert r["exclusion_reason"] is None, "경고가 이 trial을 제외 사유로 만들면 안 됨"
    assert r["included_in_main_analysis"] is True, "유효한 실험 결과이므로 본 분석에 포함돼야 함"
    print("OK - tolerant profile + 교체 + prevented가 오해 소지 issue로 검출되지만 "
          "outcome/포함 여부는 그대로(단순 분석용 표시)")


def test_missing_target_replacement_fields_read_without_error():
    # target_replaced 계열 필드(그리고 readiness_probe_profile)가 아예 없는
    # 기존 결과(pod_kill/load_ramp 등 network_degrade 이전 trial)도 오류
    # 없이 읽혀야 한다.
    row = _base_row()
    for f in ("readiness_probe_profile", "readiness_probe_timeout_sec", "target_replaced",
              "t_target_replaced", "target_replacement_pod_name", "target_replacement_pod_uid"):
        assert f not in row  # _base_row 자체가 이미 이 필드들 없이 v1 성격 fixture임을 확인
    out_rows, issues = build_comparison([row])
    r = out_rows[0]
    assert r["readiness_probe_profile"] is None
    assert r["target_replaced"] is None
    assert r["restart_chain_observed"] is None
    assert r["probe_isolation_held"] is None
    assert not any("probe_isolation_held" in i.problem for i in issues)
    print("OK - target_replaced 계열 필드 없는 기존 결과도 오류 없이 읽힘(전부 None)")


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


def test_network_degrade_fields_survive_json_to_csv_round_trip(tmp_path):
    # 2026-09-18 리뷰 질문에 대한 직접 증거 - adapter 내부 상태가 아니라
    # 실제 trial JSON 파일 -> comparison.csv까지 필드가 남는지 파일
    # 단위로 확인한다(다른 테스트들은 build_comparison()의 반환값만 봄).
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    row = _base_row(run_id="network_degrade-native-01-20260101T000000Z", scenario="network_degrade",
                     readiness_probe_profile="default", readiness_probe_timeout_sec=1.0,
                     target_replaced=True, t_target_replaced="2026-01-01T00:01:10+00:00",
                     target_replacement_pod_name="vllm-def456", target_replacement_pod_uid="uid-2")
    (results_dir / "trial-a.json").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    rows = load_all_results(results_dir)
    out_rows, _ = build_comparison(rows)
    out_path = results_dir / "comparison.csv"
    write_comparison_csv(out_rows, out_path)

    with out_path.open(encoding="utf-8") as f:
        csv_row = next(csv.DictReader(f))
    assert csv_row["readiness_probe_profile"] == "default"
    assert csv_row["readiness_probe_timeout_sec"] == "1.0"
    assert csv_row["target_replaced"] == "True"
    assert csv_row["t_target_replaced"] == "2026-01-01T00:01:10+00:00"
    assert csv_row["target_replacement_pod_name"] == "vllm-def456"
    assert csv_row["target_replacement_pod_uid"] == "uid-2"
    assert csv_row["restart_chain_observed"] == "True"
    assert csv_row["probe_isolation_held"] == ""  # None -> csv 모듈이 빈 문자열로 씀
    print("OK - trial JSON -> comparison.csv 파일까지 6개 원본 필드 + 2개 해석 필드 모두 남음")


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    test_normal_recovered_row_included_with_derived_values()
    test_temporal_relation_post_injection()
    test_temporal_relation_pre_injection()
    test_temporal_relation_temporally_ambiguous()
    test_temporal_relation_unknown_when_injection_span_missing()
    test_temporal_relation_uses_request_when_last_seen_missing()
    test_injection_timestamps_consistency_violation_detected()
    test_v1_result_without_new_timing_fields_reads_without_error()
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
    test_default_profile_replaced_is_restart_chain_observed()
    test_tolerant_profile_replaced_is_probe_isolation_not_held()
    test_tolerant_profile_not_replaced_is_probe_isolation_held()
    test_tolerant_profile_replaced_prevented_flagged_as_misleading()
    test_missing_target_replacement_fields_read_without_error()
    with tempfile.TemporaryDirectory() as d:
        test_original_json_files_not_modified(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_comparison_csv_written_correctly(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_network_degrade_fields_survive_json_to_csv_round_trip(Path(d))
    test_slo_version_v3_preserved_through_comparison()
    print("\n모두 통과")


def test_slo_version_v3_preserved_through_comparison():
    # slo_judge.SLO_VERSION="v3"로 기록된 trial이 build_comparison()을
    # 거쳐도 "v3"가 그대로 보존돼야 한다(2026-09-18, run_once.py 기본값이
    # 조용히 "v2"로 기록되던 문제의 회귀 방지).
    row = _base_row(slo_version="v3", latency_slo_sec=0.648)
    out_rows, issues = build_comparison([row])
    assert out_rows[0]["slo_version"] == "v3"
    print("OK - slo_version='v3'가 build_comparison()을 거쳐도 보존됨")
