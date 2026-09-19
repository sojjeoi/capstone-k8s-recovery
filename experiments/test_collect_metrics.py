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
        "detection_source": "predictive",  # 2026-09-19부터 경로(predictive/reactive) - 예전 값("isolation_forest")은 detector 필드로 분리됨
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
                       action="promote_preview", detected=True, slo_evaluable_at_exit=True,
                       detector="fixed_threshold")]
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
    # 2026-09-20: 교체 관측이 promotion 요청(00:01:32)보다 앞서야 promotion과 별개인 unplanned 교체다 - 시각이 없으면 이제
    # indeterminate(추정 안 함)라 이 issue가 아니라 별도 issue가 나온다.
    rows = [_base_row(arm="fixed_threshold", readiness_probe_profile="network_tolerant",
                       target_replaced=True, t_target_replaced="2026-01-01T00:01:10+00:00",
                       target_replacement_pod_name="vllm-def456", target_replacement_pod_uid="uid-2",
                       outcome="prevented", t_slo=None, t_recovery=None, slo_evaluable_at_exit=True)]
    out_rows, issues = build_comparison(rows)
    assert any("promotion으로 설명되지 않는" in i.problem and "probe_isolation_held" in i.problem for i in issues)
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


def test_slo_version_v3_preserved_through_comparison():
    # slo_judge.SLO_VERSION="v3"로 기록된 trial이 build_comparison()을
    # 거쳐도 "v3"가 그대로 보존돼야 한다(2026-09-18, run_once.py 기본값이
    # 조용히 "v2"로 기록되던 문제의 회귀 방지).
    row = _base_row(slo_version="v3", latency_slo_sec=0.648)
    out_rows, issues = build_comparison([row])
    assert out_rows[0]["slo_version"] == "v3"
    print("OK - slo_version='v3'가 build_comparison()을 거쳐도 보존됨")


def test_baseline_fields_preserved_through_comparison():
    # 주입 전 baseline 미확보 문제 수정(2026-09-18)으로 TrialResult에 추가된
    # 5개 baseline 필드가 build_comparison()을 거쳐 comparison.csv 행까지
    # 그대로 남아야 한다 - 미구현 어댑터(pod_kill/network_degrade의 옛
    # 결과 등)로 기록된, 이 필드들이 아예 없는 row도 오류 없이 None으로
    # 읽혀야 한다.
    row = _base_row(baseline_valid=True, t_baseline_ready="2026-01-01T00:00:57+00:00",
                     baseline_sample_count=42, baseline_p95=0.31, baseline_availability=1.0)
    out_rows, issues = build_comparison([row])
    assert out_rows[0]["baseline_valid"] is True
    assert out_rows[0]["t_baseline_ready"] == "2026-01-01T00:00:57+00:00"
    assert out_rows[0]["baseline_sample_count"] == 42
    assert out_rows[0]["baseline_p95"] == 0.31
    assert out_rows[0]["baseline_availability"] == 1.0

    old_row = _base_row()  # baseline 필드 자체가 없는 기존 결과(하위호환)
    old_out_rows, _ = build_comparison([old_row])
    assert old_out_rows[0]["baseline_valid"] is None
    assert old_out_rows[0]["t_baseline_ready"] is None
    print("OK - baseline 5개 필드가 comparison.csv 행까지 보존됨, 미구현 결과는 None으로 안전하게 읽힘")


def test_stage_fields_preserved_through_comparison():
    # stage 관측성 보완(2026-09-18)으로 추가된 slo_stage/detection_stage/
    # action_stage가 build_comparison()을 거쳐 comparison.csv 행까지
    # 그대로 남아야 한다 - 이 필드가 없는 과거 결과도 오류 없이 None으로
    # 읽혀야 한다.
    row = _base_row(slo_stage="stage-3-0.20rps", detection_stage=None, action_stage=None)
    out_rows, issues = build_comparison([row])
    assert out_rows[0]["slo_stage"] == "stage-3-0.20rps"
    assert out_rows[0]["detection_stage"] is None
    assert out_rows[0]["action_stage"] is None

    old_row = _base_row()  # stage 필드 자체가 없는 기존 결과(하위호환)
    old_out_rows, _ = build_comparison([old_row])
    assert old_out_rows[0]["slo_stage"] is None
    print("OK - stage 3개 필드가 comparison.csv 행까지 보존됨, 미구현 결과는 None으로 안전하게 읽힘")


def _fields(issues, run_id=None):
    return [(i.run_id, i.field) for i in issues if run_id is None or i.run_id == run_id]


def _judged_row(**overrides):
    """non-native trial의 정상적으로 채워진(live_state) 판정·감사 필드 fixture."""
    fields = dict(
        arm="proposed", run_id="load_ramp-proposed-01-20260101T000000Z", detected=True,
        detection_source="predictive", detector="isolation_forest", action="promote_preview",
        decision_outcome="executed_verified", idempotency_key="load_ramp-proposed-01-20260101T000000Z:anomaly_risk",
        promotion_verified=True, judgment_source="live_state", audit_status="complete",
        audit_record_id="rec-1", audit_reconciled_at="2026-01-01T00:03:20+00:00")
    fields.update(overrides)
    return _base_row(**fields)


def test_judgment_and_audit_fields_preserved_through_comparison():
    out_rows, issues = build_comparison([_judged_row()])
    row = out_rows[0]
    assert row["detector"] == "isolation_forest" and row["detection_source"] == "predictive"
    assert row["decision_outcome"] == "executed_verified" and row["promotion_verified"] is True
    assert row["idempotency_key"].endswith(":anomaly_risk") and row["judgment_source"] == "live_state"
    assert row["audit_status"] == "complete" and row["audit_record_id"] == "rec-1"
    assert row["t_audit_write"] and row["t_audit_push"] and row["commit_sha"] == "abc123"
    assert row["audit_pending"] is False and row["timing_anomaly"] is False
    assert issues == [] or all(i.field not in ("detected", "audit_status") for i in issues), issues

    legacy, _ = build_comparison([_base_row()])  # 새 필드가 아예 없는 과거 결과도 오류 없이 None
    assert legacy[0]["detector"] is None and legacy[0]["audit_status"] is None
    print("OK - 새 판정·감사 필드가 comparison 행까지 보존되고 과거 결과도 안전하게 읽힘")


def test_t_detection_with_detected_false_is_flagged_for_non_native():
    row = _judged_row(detected=False)  # 이번에 발견된 실제 회귀: t_detection은 있는데 detected가 기본값
    _, issues = build_comparison([row])
    assert (row["run_id"], "detected") in _fields(issues)
    print("OK - non-native에서 t_detection이 있는데 detected=false면 모순으로 검출")


def test_detected_true_without_t_detection_is_flagged():
    row = _judged_row(t_detection=None)
    _, issues = build_comparison([row])
    assert (row["run_id"], "t_detection") in _fields(issues)
    print("OK - detected=true인데 t_detection이 없으면 모순으로 검출")


def test_promote_action_without_t_api_request_or_verification_is_flagged():
    row = _judged_row(t_api_request=None, promotion_verified=None)
    _, issues = build_comparison([row])
    flagged = _fields(issues, row["run_id"])
    assert (row["run_id"], "t_api_request") in flagged and (row["run_id"], "promotion_verified") in flagged
    print("OK - action=promote_preview인데 t_api_request/promotion 검증 결과가 없으면 모순으로 검출")


def test_unverified_promotion_is_not_a_contradiction():
    # 실행했지만 selector 검증이 실패한 것(promotion_verified=false)은 "검증 결과 없음"이 아니다
    row = _judged_row(promotion_verified=False, decision_outcome="executed_unverified", t_switch=None)
    _, issues = build_comparison([row])
    assert (row["run_id"], "promotion_verified") not in _fields(issues)
    assert (row["run_id"], "t_switch") not in _fields(issues), "검증 실패 promotion은 t_switch가 null인 게 정상"
    print("OK - promotion_verified=false는 결과가 있는 것이라 모순이 아님")


def test_native_arm_is_not_subject_to_judgment_contradiction_checks():
    row = _base_row(arm="native", detected=False, t_detection="2026-01-01T00:01:30+00:00")
    _, issues = build_comparison([row])
    assert (row["run_id"], "detected") not in _fields(issues)
    print("OK - native는 recovery-policy 미개입이라 판정 모순 검사 대상이 아님")


def test_audit_pending_is_separate_from_timing_anomaly():
    row = _judged_row(audit_status="pending", audit_status_reason="Git push 대기 중(outbox status=pushing)",
                      t_audit_push=None, commit_sha=None)
    out_rows, issues = build_comparison([row])
    assert out_rows[0]["audit_pending"] is True
    assert out_rows[0]["timing_anomaly"] is False, "promotion_verified=true + audit pending은 timing anomaly가 아님"
    audit_issues = [i for i in issues if i.field == "audit_status"]
    assert len(audit_issues) == 1 and "timing anomaly 아님" in audit_issues[0].problem
    assert "promotion 자체는 검증됨" in audit_issues[0].problem
    assert not [i for i in issues if i.field in ("t_audit_push", "commit_sha")]
    print("OK - promotion 검증 + audit pending은 timing anomaly가 아니라 audit pending으로 별도 표시")


def test_audit_failed_is_flagged_as_pending_category_with_reason():
    row = _judged_row(audit_status="failed", audit_status_reason="Git push 실패: 403", commit_sha=None, t_audit_push=None)
    out_rows, issues = build_comparison([row])
    assert out_rows[0]["audit_pending"] is True
    assert any("403" in i.problem for i in issues if i.field == "audit_status")
    print("OK - audit failed도 사유와 함께 audit pending 범주로 표시")


def test_unreconciled_legacy_promotion_is_flagged_but_reconciled_one_is_not():
    legacy = _base_row(arm="proposed", run_id="legacy-proposed-01", detected=False, action="none",
                       t_audit_write=None, t_audit_push=None, commit_sha=None)
    # 과거 proposed 파일럿과 같은 상태: 실제 promotion(t_api_request 있음)이 있었는데 판정 필드는 기본값
    _, issues = build_comparison([legacy])
    assert ("legacy-proposed-01", "detected") in _fields(issues), "reconcile 전 과거 trial은 모순으로 드러나야 함"

    fixed = _judged_row(run_id="legacy-proposed-01", judgment_source="audit_reconcile",
                        audit_reconciled_at="2026-09-19T12:00:00+00:00")
    out_rows, issues = build_comparison([fixed])
    assert ("legacy-proposed-01", "detected") not in _fields(issues)
    assert out_rows[0]["judgment_source"] == "audit_reconcile"
    assert out_rows[0]["audit_reconciled_at"] == "2026-09-19T12:00:00+00:00"
    print("OK - reconcile 전 과거 trial은 모순으로 검출, 보완 후엔 provenance와 함께 정상")


def test_promotion_path_timing_columns_order_and_derived_delay():
    row = _judged_row()  # base row의 t_detection < t_decision < t_api_request < t_switch (00:01:30/31/32/35)
    out_rows, issues = build_comparison([row])
    out = out_rows[0]
    assert out["t_decision"] == "2026-01-01T00:01:31+00:00" and out["t_api_request"] == "2026-01-01T00:01:32+00:00"
    assert out["t_switch"] == "2026-01-01T00:01:35+00:00", "새 timestamp가 comparison 행까지 도달해야 함"
    assert out["timing_anomaly"] is False
    assert out["action_delay_sec"] == 5.0, "t_detection -> t_switch(이제 실제 값이 있어 계산됨)"
    assert not [i for i in issues if i.field in ("t_decision", "t_api_request", "t_switch", "t_detection/t_decision")]
    print("OK - promotion 경로 4개 timestamp가 컬럼으로 노출되고 순서 정상, action_delay_sec 계산됨")


def test_promotion_path_order_violations_are_timing_anomalies():
    early_decision = _judged_row(t_decision="2026-01-01T00:01:00+00:00")  # t_detection(00:01:30)보다 앞
    out, issues = build_comparison([early_decision])
    assert out[0]["timing_anomaly"] is True
    assert any("t_detection/t_decision" in i.field for i in issues)

    early_switch = _judged_row(t_switch="2026-01-01T00:01:31+00:00")  # t_api_request(00:01:32)보다 앞
    out, issues = build_comparison([early_switch])
    assert out[0]["timing_anomaly"] is True
    assert any("t_api_request/t_switch" in i.field for i in issues)

    late_decision = _judged_row(t_decision="2026-01-01T00:01:33+00:00")  # t_api_request보다 뒤
    out, issues = build_comparison([late_decision])
    assert out[0]["timing_anomaly"] is True and any("t_decision/t_api_request" in i.field for i in issues)
    print("OK - t_detection <= t_decision <= t_api_request <= t_switch 순서 위반은 timing anomaly로 검출")


def test_t_switch_requires_verified_promotion():
    row = _judged_row(promotion_verified=False, decision_outcome="executed_unverified")  # base의 t_switch가 남아 있음
    _, issues = build_comparison([row])
    assert (row["run_id"], "t_switch") in _fields(issues)
    print("OK - 검증되지 않은 promotion에 t_switch가 있으면 모순으로 검출")


def test_no_promotion_requires_null_api_request_and_switch():
    row = _judged_row(action="observe_only", decision_outcome="no_action", promotion_verified=None)
    _, issues = build_comparison([row])
    flagged = _fields(issues, row["run_id"])
    assert (row["run_id"], "t_api_request") in flagged and (row["run_id"], "t_switch") in flagged

    clean = _judged_row(action="observe_only", decision_outcome="no_action", promotion_verified=None,
                        t_api_request=None, t_switch=None)
    _, issues = build_comparison([clean])
    assert (clean["run_id"], "t_api_request") not in _fields(issues) and (clean["run_id"], "t_switch") not in _fields(issues)
    print("OK - promotion이 없으면 t_api_request/t_switch는 null이어야 함(observe-only의 t_decision은 허용)")


def test_live_state_row_requires_decision_and_switch_but_legacy_reconciled_does_not():
    missing = _judged_row(t_decision=None, t_switch=None)  # live_state인데 promotion 검증 trial에 t_decision/t_switch 없음
    _, issues = build_comparison([missing])
    flagged = _fields(issues, missing["run_id"])
    assert (missing["run_id"], "t_decision") in flagged and (missing["run_id"], "t_switch") in flagged

    # 판정 필드가 기록되기 전 과거 pilot을 audit_reconcile로 보완한 경우: t_decision/t_switch는 추정 없이 null 보존
    legacy = _judged_row(run_id="legacy-proposed-01", judgment_source="audit_reconcile", t_decision=None, t_switch=None)
    out_rows, issues = build_comparison([legacy])
    assert (legacy["run_id"], "t_decision") not in _fields(issues) and (legacy["run_id"], "t_switch") not in _fields(issues)
    assert out_rows[0]["t_decision"] is None and out_rows[0]["t_switch"] is None and out_rows[0]["timing_anomaly"] is False
    print("OK - live_state는 t_decision/t_switch 필수, 보완된 과거 pilot의 null은 오류가 아님")


def _detector_issue(issues, run_id):
    return [i for i in issues if i.run_id == run_id and i.field == "detector"]


def test_detector_matching_arm_is_ok_and_mismatch_is_flagged():
    ok_proposed = _judged_row()  # proposed + predictive + isolation_forest
    out, issues = build_comparison([ok_proposed])
    assert out[0]["detector_check"] == "ok" and not _detector_issue(issues, ok_proposed["run_id"])

    ok_fixed = _judged_row(arm="fixed_threshold", run_id="load_ramp-fixed_threshold-01-20260101T000000Z",
                           detector="fixed_threshold")
    out, issues = build_comparison([ok_fixed])
    assert out[0]["detector_check"] == "ok" and not _detector_issue(issues, ok_fixed["run_id"])

    wrong_proposed = _judged_row(detector="fixed_threshold")  # proposed arm인데 fixed_threshold가 신호를 냄
    out, issues = build_comparison([wrong_proposed])
    assert out[0]["detector_check"] == "mismatch"
    assert any("isolation_forest" in i.problem for i in _detector_issue(issues, wrong_proposed["run_id"]))

    wrong_fixed = _judged_row(arm="fixed_threshold", run_id="load_ramp-fixed_threshold-02-20260101T000000Z",
                              detector="isolation_forest")
    out, issues = build_comparison([wrong_fixed])
    assert out[0]["detector_check"] == "mismatch" and _detector_issue(issues, wrong_fixed["run_id"])
    print("OK - arm과 detector 일치는 ok, 불일치(proposed<-fixed_threshold, fixed_threshold<-isolation_forest)는 issue")


def test_native_must_have_null_detector():
    ok = _base_row(arm="native", detector=None)
    out, issues = build_comparison([ok])
    assert out[0]["detector_check"] == "ok" and not _detector_issue(issues, ok["run_id"])

    bad = _base_row(arm="native", detector="isolation_forest")
    out, issues = build_comparison([bad])
    assert out[0]["detector_check"] == "mismatch" and _detector_issue(issues, bad["run_id"])
    print("OK - native는 detector가 null이어야 함")


def test_alertmanager_fallback_is_the_defined_exception_for_non_native_arms():
    for arm in ("fixed_threshold", "proposed"):
        row = _judged_row(arm=arm, run_id=f"load_ramp-{arm}-03-20260101T000000Z", detection_source="reactive",
                          detector="alertmanager", idempotency_key="fp123:2026-01-01T00:01:30+00:00")
        out, issues = build_comparison([row])
        assert out[0]["detector_check"] == "reactive_fallback", arm
        assert not _detector_issue(issues, row["run_id"]), "Alertmanager fallback은 오류가 아니라 정의된 예외"

    wrong_name = _judged_row(detection_source="reactive", detector="isolation_forest")
    out, issues = build_comparison([wrong_name])
    assert out[0]["detector_check"] == "mismatch" and _detector_issue(issues, wrong_name["run_id"])

    wrong_path = _judged_row(detection_source="predictive", detector="alertmanager")
    out, issues = build_comparison([wrong_path])
    assert out[0]["detector_check"] == "mismatch" and _detector_issue(issues, wrong_path["run_id"]),         "예측 경로 탐지인데 detector가 alertmanager면 예외가 아니라 오류"
    print("OK - reactive+alertmanager만 fallback 예외로 허용, 그 외 조합은 issue")


def test_predictive_detection_without_detector_and_detector_without_detection_are_flagged():
    unknown = _judged_row(detector=None)
    out, issues = build_comparison([unknown])
    assert out[0]["detector_check"] == "missing" and _detector_issue(issues, unknown["run_id"])

    no_source = _judged_row(detection_source=None)
    out, issues = build_comparison([no_source])
    assert out[0]["detector_check"] == "missing" and _detector_issue(issues, no_source["run_id"])

    phantom = _judged_row(detected=False, t_detection=None, t_decision=None, t_api_request=None, t_switch=None,
                          action="none", decision_outcome=None, promotion_verified=None, detection_source=None)
    out, issues = build_comparison([phantom])  # 탐지가 없는데 detector가 남아 있음
    assert out[0]["detector_check"] == "mismatch" and _detector_issue(issues, phantom["run_id"])

    clean = _judged_row(detected=False, t_detection=None, t_decision=None, t_api_request=None, t_switch=None,
                        action="none", decision_outcome=None, promotion_verified=None, detection_source=None,
                        detector=None)
    out, issues = build_comparison([clean])
    assert out[0]["detector_check"] == "not_applicable" and not _detector_issue(issues, clean["run_id"])
    print("OK - 예측 탐지인데 detector 불명/탐지 없는데 detector 있음은 issue, 미탐지+null은 not_applicable")


def test_inferred_detector_is_marked_for_pilot_not_an_error_but_refused_for_main_data():
    inferred = {"detector": "trial.detector_process(arm 배선값) - 2026-09-19 이전 기록이라 확인 불가"}
    pilot = _judged_row(is_pilot=True, judgment_source="audit_reconcile",
                        reconciliation={"inferred_fields": inferred})
    out, issues = build_comparison([pilot])
    assert out[0]["detector_check"] == "inferred_pilot", "provenance가 있는 과거 pilot은 오류가 아니라 별도 표시"
    assert not _detector_issue(issues, pilot["run_id"])

    main_data = _judged_row(is_pilot=False, judgment_source="audit_reconcile",
                            reconciliation={"inferred_fields": inferred})
    out, issues = build_comparison([main_data])
    assert out[0]["detector_check"] == "mismatch"
    assert any("추론" in i.problem for i in _detector_issue(issues, main_data["run_id"])), "본 실험 데이터의 detector 추론은 오류"

    wrong_inferred = _judged_row(is_pilot=True, detector="fixed_threshold", judgment_source="audit_reconcile",
                                 reconciliation={"inferred_fields": inferred})  # 추론값이 arm과 불일치
    out, issues = build_comparison([wrong_inferred])
    assert out[0]["detector_check"] == "mismatch" and _detector_issue(issues, wrong_inferred["run_id"])
    print("OK - 과거 inferred pilot은 별도 표시(오류 아님), 본 실험 추론·arm 불일치 추론값은 오류")


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
    test_baseline_fields_preserved_through_comparison()
    test_stage_fields_preserved_through_comparison()
    test_judgment_and_audit_fields_preserved_through_comparison()
    test_t_detection_with_detected_false_is_flagged_for_non_native()
    test_detected_true_without_t_detection_is_flagged()
    test_promote_action_without_t_api_request_or_verification_is_flagged()
    test_unverified_promotion_is_not_a_contradiction()
    test_native_arm_is_not_subject_to_judgment_contradiction_checks()
    test_audit_pending_is_separate_from_timing_anomaly()
    test_audit_failed_is_flagged_as_pending_category_with_reason()
    test_unreconciled_legacy_promotion_is_flagged_but_reconciled_one_is_not()
    test_promotion_path_timing_columns_order_and_derived_delay()
    test_promotion_path_order_violations_are_timing_anomalies()
    test_t_switch_requires_verified_promotion()
    test_no_promotion_requires_null_api_request_and_switch()
    test_live_state_row_requires_decision_and_switch_but_legacy_reconciled_does_not()
    test_detector_matching_arm_is_ok_and_mismatch_is_flagged()
    test_native_must_have_null_detector()
    test_alertmanager_fallback_is_the_defined_exception_for_non_native_arms()
    test_predictive_detection_without_detector_and_detector_without_detection_are_flagged()
    test_inferred_detector_is_marked_for_pilot_not_an_error_but_refused_for_main_data()
    print("\n모두 통과")
