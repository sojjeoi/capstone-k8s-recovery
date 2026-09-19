#!/usr/bin/env python3
"""reconcile_audit.py 검증(2026-09-19 추가) - recovery-policy·실클러스터 없이
합성 감사기록(audit-log 레코드 + outbox 상태를 조인한 형태)으로 primary 선택
규칙, 감사 필드 계산, 과거 trial 보완(provenance·원본 보존), idempotency,
bounded wait를 검증한다."""
import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from reconcile_audit import (
    audit_fields,
    reconcile_file,
    reconcile_trial,
    select_records,
    wait_for_primary_audit,
)

RUN = "pilot-load_ramp-proposed-01-20260918T181524Z"


def _rec(record_id, outcome, key=None, source="anomaly", action="promote_preview", evidence=None,
         status="pushed", sha="16c8f08aaa", t_push="2026-09-18T18:22:40+00:00", last_error=None, attempts=0):
    return {
        "record_id": record_id, "decided_at": "2026-09-18T18:22:33.375182Z", "signal_source": source,
        "signal_type": "anomaly_risk", "idempotency_key": key if key is not None else f"{RUN}:anomaly_risk",
        "evidence": evidence if evidence is not None else {}, "action": action, "outcome": outcome,
        "result": None, "reasoning": "",
        "outbox": {"status": status, "t_audit_write": "2026-09-18T18:22:33.400000+00:00",
                   "t_audit_push": t_push if status == "pushed" else None,
                   "commit_sha": sha if status == "pushed" else None, "attempts": attempts,
                   "last_error": last_error},
    }


def _legacy_trial(**over):
    trial = {
        "run_id": RUN, "arm": "proposed", "scenario": "load_ramp", "rep": 1, "is_pilot": True,
        "detector_process": "isolation_forest", "outcome": "recovered", "state": "completed",
        "t_injection": "2026-09-18T18:21:57.162939+00:00", "t_detection": "2026-09-18T18:22:29.206642+00:00",
        "t_api_request": "2026-09-18T18:22:29.241981+00:00", "t_slo": "2026-09-18T18:23:32.678416+00:00",
        "t_recovery": "2026-09-18T18:24:19.760227+00:00", "t_audit_write": None, "t_audit_push": None,
        "commit_sha": None, "detected": False, "detection_source": None, "action": "none",
        "promotion_verified": None, "notes": "",
    }
    trial.update(over)
    return trial


def test_primary_selection_prefers_executed_verified_and_excludes_duplicate():
    records = [
        _rec("r-observe", "no_action", action="observe_only", key=f"{RUN}:other"),
        _rec("r-exec", "executed_verified"),
        _rec("r-dup", "skipped_duplicate", action=None),
    ]
    sel = select_records(records, RUN)
    assert sel["primary"]["record_id"] == "r-exec"
    assert sel["first_detection"]["record_id"] == "r-observe", "최초 유효 탐지는 primary와 다를 수 있음"
    assert [e["record_id"] for e in sel["excluded"]] == ["r-dup"]
    assert "skipped_duplicate" in sel["excluded"][0]["reason"]
    print("OK - executed_verified가 primary, skipped_duplicate는 제외(사유 기록)")


def test_primary_selection_without_action_uses_first_valid_detection():
    records = [
        _rec("r-dup0", "skipped_duplicate", action=None),
        _rec("r-first", "no_action", action="observe_only"),
        _rec("r-second", "skipped_rule_out", action=None, key=f"{RUN}:other"),
    ]
    sel = select_records(records, RUN)
    assert sel["primary"]["record_id"] == "r-first"
    print("OK - action이 없으면 최초 유효 탐지의 decision 기록이 primary")


def test_unverified_execution_beats_observe_only_but_not_verified():
    sel = select_records([_rec("r-obs", "no_action", action="observe_only"),
                          _rec("r-unv", "executed_unverified", key=f"{RUN}:x")], RUN)
    assert sel["primary"]["record_id"] == "r-unv"
    sel = select_records([_rec("r-unv", "executed_unverified", key=f"{RUN}:x"),
                          _rec("r-ver", "executed_verified", key=f"{RUN}:y")], RUN)
    assert sel["primary"]["record_id"] == "r-ver"
    print("OK - executed_verified > executed_unverified > 그 외")


def test_other_run_and_unattributed_records_are_excluded():
    records = [
        _rec("r-other-run", "executed_verified", key="some-other-run:anomaly_risk"),
        _rec("r-prefix-trap", "executed_verified", key=f"{RUN}Z:anomaly_risk"),  # run_id가 접두어일 뿐 다른 run
        _rec("r-legacy-reactive", "no_action", source="alertmanager", key="fp123:2026-09-18T18:00:00+00:00",
             action="observe_only"),
        _rec("r-reactive-attributed", "no_action", source="alertmanager", key="fp124:2026-09-18T18:00:01+00:00",
             action="observe_only", evidence={"experiment_run_id": RUN}),
    ]
    sel = select_records(records, RUN)
    assert sel["primary"]["record_id"] == "r-reactive-attributed"
    assert {e["record_id"] for e in sel["excluded"]} == {"r-other-run", "r-prefix-trap", "r-legacy-reactive"}
    print("OK - 다른 run/접두어만 같은 run/귀속 근거 없는 반응 기록은 제외, evidence로 귀속된 반응 기록은 허용")


def test_attribution_is_strict_per_signal_path():
    # 승인된 규칙(2026-09-19): 예측 경로는 key 접두어, 반응 경로는 evidence - 서로 대체되지 않는다.
    records = [
        # run_id 없이 보낸 예측 신호가 ambient로 태깅된 경우: evidence는 맞지만 key엔 run_id가 없다
        # (이 trial의 detector 프로세스가 보낸 신호가 아님) -> 예측 기록은 evidence만으로 인정 안 함
        _rec("r-pred-evidence-only", "no_action", key="anomaly:anomaly_risk:2026-09-18T18:00:00+00:00",
             action="observe_only", evidence={"experiment_run_id": RUN}),
        # 반응 기록이 key 접두어만 맞는 경우(우연/조작) -> 반응 기록은 evidence로만 인정
        _rec("r-react-key-only", "no_action", source="alertmanager", key=f"{RUN}:fake-fingerprint",
             action="observe_only", evidence={}),
        _rec("r-unknown-source", "no_action", source="manual", key=f"{RUN}:x", action="observe_only",
             evidence={"experiment_run_id": RUN}),
        _rec("r-pred-ok", "no_action", action="observe_only"),
    ]
    sel = select_records(records, RUN)
    assert sel["primary"]["record_id"] == "r-pred-ok"
    assert {e["record_id"] for e in sel["excluded"]} == {"r-pred-evidence-only", "r-react-key-only", "r-unknown-source"}
    assert all("귀속 근거 없음" in e["reason"] for e in sel["excluded"])
    none = select_records(records[:3], RUN)
    assert none["primary"] is None, "어느 근거도 없으면 primary 후보가 될 수 없음"
    print("OK - 예측=key 접두어, 반응=evidence 정확 일치, 경로별 근거는 서로 대체 안 됨, 근거 없으면 후보 제외")


def test_audit_fields_complete_pending_failed_and_not_applicable():
    done = audit_fields(_rec("r", "executed_verified"), True)
    assert done["audit_status"] == "complete" and done["commit_sha"] == "16c8f08aaa"
    assert done["t_audit_push"] and done["t_audit_write"] and done["audit_record_id"] == "r"

    pending = audit_fields(_rec("r", "executed_verified", status="pushing"), True)
    assert pending["audit_status"] == "pending" and "pushing" in pending["audit_status_reason"]
    assert pending["t_audit_write"], "t_audit_write는 push 전에도 알 수 있으면 채움"
    assert pending["t_audit_push"] is None and pending["commit_sha"] is None, "미완료는 null 유지"

    failed = audit_fields(_rec("r", "executed_verified", status="failed", last_error="non-fast-forward", attempts=3), True)
    assert failed["audit_status"] == "failed" and "non-fast-forward" in failed["audit_status_reason"]
    assert failed["commit_sha"] is None

    assert audit_fields(None, False)["audit_status"] == "not_applicable"
    assert audit_fields(None, True)["audit_status"] == "pending"
    print("OK - audit_status: complete / pending / failed(사유) / not_applicable")


def test_reconcile_supplements_legacy_trial_with_provenance_and_preserves_evidence(tmp_path):
    path = tmp_path / f"trial-{RUN}.json"
    original = _legacy_trial()
    path.write_text(json.dumps(original, ensure_ascii=False, indent=2), encoding="utf-8")
    records = [_rec("ec8d6b5d", "executed_verified"),
               _rec("418c665a", "skipped_duplicate", action=None)]

    report = reconcile_file(path, fetch_fn=lambda run_id, url: records, now_fn=lambda: "2026-09-19T12:00:00+00:00")
    assert report["changed"] is True and report["primary_record_id"] == "ec8d6b5d"
    assert [e["record_id"] for e in report["excluded"]] == ["418c665a"]

    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["detected"] is True and after["detection_source"] == "predictive"
    assert after["detector"] == "isolation_forest"
    assert after["action"] == "promote_preview" and after["decision_outcome"] == "executed_verified"
    assert after["idempotency_key"] == f"{RUN}:anomaly_risk" and after["promotion_verified"] is True
    assert after["commit_sha"] == "16c8f08aaa" and after["audit_status"] == "complete"
    assert after["judgment_source"] == "audit_reconcile"
    assert after["audit_reconciled_at"] == "2026-09-19T12:00:00+00:00"
    prov = after["reconciliation"]
    assert prov["reconciled_at"] == "2026-09-19T12:00:00+00:00"
    assert prov["primary_record_id"] == "ec8d6b5d" and prov["excluded_records"][0]["record_id"] == "418c665a"
    assert "detector" in prov["inferred_fields"], "evidence에 detector가 없던 과거 기록이라 추론 출처를 남겨야 함"
    assert prov["original_values"]["detected"] is False and prov["original_values"]["action"] == "none"
    assert set(prov["supplemented_fields"]) >= {"detected", "action", "promotion_verified", "detector"}

    # 원본 증거 보존: 타임스탬프·outcome은 그대로, 원본 파일은 바이트 그대로 백업
    for key in ("t_injection", "t_detection", "t_api_request", "t_slo", "t_recovery", "outcome", "state"):
        assert after[key] == original[key], key
    backup = path.with_name(path.name + ".pre-reconcile.bak")
    assert json.loads(backup.read_text(encoding="utf-8")) == original
    print("OK - 과거 trial을 primary 기록으로 보완, provenance·원본 값 보존, 타임스탬프/outcome 불변, 원본 백업")


def test_detector_inference_allowed_only_for_pilot_and_marked(tmp_path):
    records = [_rec("ec8d6b5d", "executed_verified")]  # evidence에 detector 없음(2026-09-19 이전 기록)

    pilot = tmp_path / "trial-pilot.json"
    pilot.write_text(json.dumps(_legacy_trial(is_pilot=True)), encoding="utf-8")
    reconcile_file(pilot, fetch_fn=lambda r, u: records, now_fn=lambda: "2026-09-19T12:00:00+00:00")
    after = json.loads(pilot.read_text(encoding="utf-8"))
    assert after["detector"] == "isolation_forest"
    assert "detector" in after["reconciliation"]["inferred_fields"], "pilot의 추론은 반드시 표시돼야 함"

    main_data = tmp_path / "trial-main.json"
    main_data.write_text(json.dumps(_legacy_trial(is_pilot=False)), encoding="utf-8")
    report = reconcile_file(main_data, fetch_fn=lambda r, u: records, now_fn=lambda: "2026-09-19T12:00:00+00:00")
    after = json.loads(main_data.read_text(encoding="utf-8"))
    assert after["detector"] is None, "본 실험 데이터에는 detector 추론을 허용하지 않음"
    assert after["reconciliation"]["inferred_fields"] == {}
    assert any("추론을 허용하지 않아" in n for n in report["notes"])
    assert after["detection_source"] == "predictive" and after["action"] == "promote_preview", \
        "detector만 비워두고 나머지 감사기록 기반 판정 필드는 정상 보완"

    # 감사기록 evidence에 detector가 있으면(추론이 아니라 기록 자체) 본 실험 데이터도 그대로 채운다
    with_tag = tmp_path / "trial-main-tagged.json"
    with_tag.write_text(json.dumps(_legacy_trial(is_pilot=False)), encoding="utf-8")
    tagged = [_rec("ec8d6b5d", "executed_verified", evidence={"experiment_run_id": RUN, "detector": "isolation_forest"})]
    reconcile_file(with_tag, fetch_fn=lambda r, u: tagged, now_fn=lambda: "2026-09-19T12:00:00+00:00")
    after = json.loads(with_tag.read_text(encoding="utf-8"))
    assert after["detector"] == "isolation_forest" and after["reconciliation"]["inferred_fields"] == {}
    print("OK - detector 추론은 pilot 한정+표시 유지, 본 실험 데이터는 추론 불가(null), evidence 기록이 있으면 그대로 사용")


def test_reconcile_is_idempotent_and_never_rewrites_unchanged_file(tmp_path):
    path = tmp_path / f"trial-{RUN}.json"
    path.write_text(json.dumps(_legacy_trial()), encoding="utf-8")
    records = [_rec("ec8d6b5d", "executed_verified")]
    reconcile_file(path, fetch_fn=lambda r, u: records, now_fn=lambda: "2026-09-19T12:00:00+00:00")
    first_bytes = path.read_bytes()
    backup_bytes = path.with_name(path.name + ".pre-reconcile.bak").read_bytes()

    report = reconcile_file(path, fetch_fn=lambda r, u: records, now_fn=lambda: "2026-09-19T13:00:00+00:00")
    assert report["changed"] is False
    assert path.read_bytes() == first_bytes, "같은 입력으로 다시 돌려도 파일이 바뀌면 안 됨(reconciled_at 포함)"
    assert path.with_name(path.name + ".pre-reconcile.bak").read_bytes() == backup_bytes, "백업은 최초 원본 유지"
    print("OK - 재실행해도 결과 동일(idempotent), 파일·백업 불변")


def test_audit_pending_then_reconciled_later_without_touching_outcome_or_judgment(tmp_path):
    # live 권위 상태로 이미 판정이 기록된 trial - Git push가 늦어 audit_status=pending으로 끝났다
    path = tmp_path / f"trial-{RUN}.json"
    live = _legacy_trial(
        judgment_source="live_state", detected=True, detection_source="predictive", detector="isolation_forest",
        action="promote_preview", decision_outcome="executed_verified", idempotency_key=f"{RUN}:anomaly_risk",
        promotion_verified=True, audit_status="pending", audit_status_reason="Git push 대기 중(outbox status=pushing)",
        t_audit_write="2026-09-18T18:22:33.400000+00:00")
    path.write_text(json.dumps(live), encoding="utf-8")

    pushed = [_rec("ec8d6b5d", "executed_verified")]
    report = reconcile_file(path, fetch_fn=lambda r, u: pushed, now_fn=lambda: "2026-09-19T12:00:00+00:00")
    after = json.loads(path.read_text(encoding="utf-8"))
    assert report["changed"] is True and report["judgment_supplemented"] is False
    assert after["audit_status"] == "complete" and after["commit_sha"] == "16c8f08aaa" and after["t_audit_push"]
    assert after["audit_status_reason"] is None
    assert after["judgment_source"] == "live_state" and "reconciliation" not in after, \
        "live 권위 상태의 판정 필드는 audit 재조정이 덮어쓰지 않음"
    for key in ("outcome", "detected", "action", "t_detection", "t_slo"):
        assert after[key] == live[key], key
    print("OK - audit push pending -> 재조정으로 complete, 판정 필드·outcome·timestamp는 불변")


def test_audit_failure_is_recorded_without_changing_outcome(tmp_path):
    path = tmp_path / f"trial-{RUN}.json"
    path.write_text(json.dumps(_legacy_trial(judgment_source="live_state", detected=True, action="promote_preview")),
                    encoding="utf-8")
    failed = [_rec("ec8d6b5d", "executed_verified", status="failed", last_error="git push 실패: 403", attempts=6)]
    reconcile_file(path, fetch_fn=lambda r, u: failed, now_fn=lambda: "2026-09-19T12:00:00+00:00")
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["audit_status"] == "failed" and "403" in after["audit_status_reason"]
    assert after["commit_sha"] is None and after["t_audit_push"] is None
    assert after["outcome"] == "recovered" and after["action"] == "promote_preview"
    print("OK - audit 실패는 audit_status=failed+사유로만 남고 outcome/action은 그대로")


def test_t_detection_without_attributable_record_is_not_guessed():
    trial = _legacy_trial()
    updated, report = reconcile_trial(trial, [_rec("r-legacy", "no_action", source="alertmanager",
                                                   key="fp:2026-09-18T18:00:00+00:00", action="observe_only")],
                                      "2026-09-19T12:00:00+00:00")
    assert updated["detected"] is False, "귀속 가능한 기록이 없으면 판정 필드를 추측해 바꾸지 않음"
    assert updated["audit_status"] == "pending"
    assert report["notes"] and "추측" in report["notes"][0]
    print("OK - t_detection은 있는데 귀속 가능한 감사기록이 없으면 판정을 추측하지 않고 pending")


def test_native_trial_is_never_touched(tmp_path):
    path = tmp_path / "trial-native.json"
    native = _legacy_trial(arm="native", run_id="pilot-load_ramp-native-01", t_detection=None)
    path.write_text(json.dumps(native), encoding="utf-8")
    called = {"fetch": 0}

    def fetch(run_id, url):
        called["fetch"] += 1
        return []

    report = reconcile_file(path, fetch_fn=fetch)
    assert report["changed"] is False and "native" in report["skipped"]
    assert called["fetch"] == 0, "native는 recovery-policy를 조회하지도 않음"
    assert json.loads(path.read_text(encoding="utf-8")) == native
    print("OK - native는 조회·수정 모두 안 함")


def test_wait_for_primary_audit_completes_when_push_finishes_during_wait():
    pending = [_rec("r", "executed_verified", status="pushing")]
    done = [_rec("r", "executed_verified")]
    seq = iter([pending, pending, done])
    fields, sel = wait_for_primary_audit(RUN, f"{RUN}:anomaly_risk", "executed_verified",
                                         timeout_sec=2.0, poll_sec=0.01, fetch_fn=lambda r, u: next(seq))
    assert fields["audit_status"] == "complete" and fields["commit_sha"] == "16c8f08aaa"
    print("OK - bounded wait 도중 push가 끝나면 complete로 반환")


def test_wait_for_primary_audit_returns_pending_on_timeout_without_raising():
    started = time.monotonic()
    fields, _ = wait_for_primary_audit(RUN, f"{RUN}:anomaly_risk", "executed_verified", timeout_sec=0.1,
                                       poll_sec=0.02, fetch_fn=lambda r, u: [_rec("r", "executed_verified", status="pending")])
    assert fields["audit_status"] == "pending" and fields["commit_sha"] is None
    assert time.monotonic() - started < 2.0, "bounded wait는 timeout을 넘겨 오래 기다리지 않음"
    print("OK - push가 안 끝나면 timeout 후 pending(예외 없음)")


def test_wait_for_primary_audit_requires_record_matching_authoritative_decision():
    # 이미 push된 기록이 있어도 authoritative 상태의 판정(key/outcome)과 다르면 그 기록은 이 판정의 감사기록이 아님
    stale = [_rec("r-old", "no_action", action="observe_only", key=f"{RUN}:observe")]
    fields, _ = wait_for_primary_audit(RUN, f"{RUN}:anomaly_risk", "executed_verified", timeout_sec=0.1,
                                       poll_sec=0.02, fetch_fn=lambda r, u: stale)
    assert fields["audit_status"] == "pending" and fields["commit_sha"] is None
    print("OK - authoritative 판정과 일치하지 않는 기록은 완료로 취급하지 않음")


def test_wait_for_primary_audit_query_failure_becomes_pending_with_reason():
    def boom(run_id, url):
        raise ConnectionError("port-forward 끊김 시뮬레이션")

    fields, sel = wait_for_primary_audit(RUN, None, None, timeout_sec=0.05, poll_sec=0.01, fetch_fn=boom)
    assert fields["audit_status"] == "pending" and "조회 실패" in fields["audit_status_reason"]
    assert sel is None
    print("OK - 감사 조회 실패는 예외가 아니라 audit_status=pending+사유")


if __name__ == "__main__":
    import tempfile

    for fn in (
        test_primary_selection_prefers_executed_verified_and_excludes_duplicate,
        test_primary_selection_without_action_uses_first_valid_detection,
        test_unverified_execution_beats_observe_only_but_not_verified,
        test_other_run_and_unattributed_records_are_excluded,
        test_attribution_is_strict_per_signal_path,
        test_audit_fields_complete_pending_failed_and_not_applicable,
        test_t_detection_without_attributable_record_is_not_guessed,
        test_wait_for_primary_audit_completes_when_push_finishes_during_wait,
        test_wait_for_primary_audit_returns_pending_on_timeout_without_raising,
        test_wait_for_primary_audit_requires_record_matching_authoritative_decision,
        test_wait_for_primary_audit_query_failure_becomes_pending_with_reason,
    ):
        fn()
    for fn in (
        test_reconcile_supplements_legacy_trial_with_provenance_and_preserves_evidence,
        test_detector_inference_allowed_only_for_pilot_and_marked,
        test_reconcile_is_idempotent_and_never_rewrites_unchanged_file,
        test_audit_pending_then_reconciled_later_without_touching_outcome_or_judgment,
        test_audit_failure_is_recorded_without_changing_outcome,
        test_native_trial_is_never_touched,
    ):
        with tempfile.TemporaryDirectory() as d:
            fn(Path(d))
    print("모두 통과")
