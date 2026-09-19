#!/usr/bin/env python3
"""trial 결과의 감사 필드를 recovery-policy의 기존 outbox/audit 상태에서 회수·
재조정한다(2026-09-19 추가). run_once.py가 trial 종료 시 짧은 bounded wait로
이 모듈을 쓰고, 그때 못 끝낸 것(Git push 지연 등)이나 판정 필드가 기록되기
전에 만들어진 과거 trial은 이 CLI로 나중에 다시 채운다.

원칙(계약서 §5.5/§5.6):
- 판정·조치(detected/action/...)의 authoritative source는 recovery-policy의 실시간
  상태이고, 감사 필드(t_audit_write/t_audit_push/commit_sha)의 원천은 정책 결과와
  분리된 outbox/audit 상태다. Git이 늦어도 outcome이나 실제 action은 바뀌지 않는다.
- 이 도구는 감사·판정 필드와 그 provenance만 건드린다. 타임스탬프(t_slo, t_injection,
  t_detection 등)·outcome·state는 절대 수정하지 않는다 - trial 결과는 1차 증거다
  (collect_metrics.py의 "원본 수정 금지" 원칙의 유일한 공인 예외이며, 그래서 아래
  안전장치를 둔다): 첫 수정 전에 원본을 `.pre-reconcile.bak`으로 한 번만 보존하고,
  보완된 판정 필드의 원래 값을 `reconciliation.original_values`에 남기며, 다시
  돌려도 결과가 같으면(idempotent) 파일을 아예 다시 쓰지 않는다.

한 trial에 감사기록이 여러 개일 때의 primary 선택 규칙(계약서 §5.6):
  1. skipped_duplicate는 primary가 될 수 없다(duplicate로 보존만).
  2. run_id 귀속 근거가 없는 기록은 자격이 없다. 근거는 경로별로 정해져 있고 서로
     대체되지 않는다: 예측 경로(signal_source=anomaly)는 idempotency_key가 정확히
     "{run_id}:" 접두어를 가져야 하고, 반응 경로(alertmanager)는 evidence.
     experiment_run_id가 정확히 일치해야 한다(Alertmanager key(fingerprint:startsAt)에는
     run_id가 없어서 recovery-policy가 2026-09-19부터 evidence에 남김). 접두어+콜론으로
     비교해 "run-1"이 "run-11"의 기록을 잘못 가져가는 일이 없게 한다.
  3. executed_verified > executed_unverified > 최초 유효 탐지의 판정 기록 순.

detector 추론 정책(계약서 §5.5): 감사기록 evidence에 detector 태그가 없던 과거 기록(2026-09-19
이전)은 pilot(is_pilot=true)에 한해 trial의 detector_process(arm 배선값)로 채우되
reconciliation.inferred_fields에 반드시 표시한다. 본 실험(is_pilot=false) 데이터에는 추론을
허용하지 않는다 - detector는 null로 남고 collect_metrics.py가 검증 이슈로 드러낸다.
"""
import argparse
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

import requests

DEFAULT_RECOVERY_POLICY_URL = "http://localhost:8080"  # kubectl port-forward -n vllm-serving svc/recovery-policy 8080:8080
ADMIN_TIMEOUT_SEC = 10

EXECUTED_VERIFIED = "executed_verified"
EXECUTED_UNVERIFIED = "executed_unverified"
SKIPPED_DUPLICATE = "skipped_duplicate"

JUDGMENT_KEYS = ["detected", "detection_source", "detector", "action", "decision_outcome",
                 "idempotency_key", "promotion_verified"]
_TIME_KEYS = ("audit_reconciled_at",)


def record_attribution(record: dict, run_id: str) -> Optional[str]:
    """이 기록이 run_id의 것임을 증명하는 근거(없으면 None = 자격 없음). 근거는 신호 경로별로
    정해져 있고 서로 대체되지 않는다(2026-09-19 승인): 예측 경로(signal_source=anomaly)는
    idempotency_key가 정확히 "{run_id}:" 접두어를 가져야 하고, 반응 경로(alertmanager)는
    evidence.experiment_run_id가 정확히 일치해야 한다. 예측 기록이 evidence만, 반응 기록이 key
    접두어만 맞는 경우(예: run_id 없이 보낸 예측 신호가 ambient로 태깅된 경우 - 이 trial의
    detector 프로세스가 보낸 신호가 아니다)는 인정하지 않는다. 경로를 모르는 기록도 제외한다."""
    source = record.get("signal_source")
    if source == "anomaly":
        if (record.get("idempotency_key") or "").startswith(f"{run_id}:"):
            return "idempotency_key"
    elif source == "alertmanager":
        if (record.get("evidence") or {}).get("experiment_run_id") == run_id:
            return "evidence.experiment_run_id"
    return None


def select_records(records: list, run_id: str) -> dict:
    """primary/최초 유효 탐지 기록과 제외된 기록(사유 포함)을 고른다. 기록 순서는
    audit-log 파일 순서(=recovery-policy가 처리한 순서)를 그대로 쓴다."""
    eligible, excluded = [], []
    for rec in records:
        if rec.get("outcome") == SKIPPED_DUPLICATE:
            excluded.append({"record_id": rec.get("record_id"), "outcome": rec.get("outcome"),
                              "reason": "skipped_duplicate는 primary가 될 수 없음(duplicate로 보존)"})
        elif record_attribution(rec, run_id) is None:
            excluded.append({"record_id": rec.get("record_id"), "outcome": rec.get("outcome"),
                              "reason": f"run_id 귀속 근거 없음(signal_source={rec.get('signal_source')!r}: 예측 경로는 "
                                        f"idempotency_key가 '{run_id}:'로 시작, 반응 경로는 "
                                        f"evidence.experiment_run_id가 일치해야 함)"})
        else:
            eligible.append(rec)
    primary = None
    for wanted in (EXECUTED_VERIFIED, EXECUTED_UNVERIFIED):
        primary = next((r for r in eligible if r.get("outcome") == wanted), None)
        if primary is not None:
            break
    if primary is None and eligible:
        primary = eligible[0]
    return {"primary": primary, "first_detection": eligible[0] if eligible else None,
            "excluded": excluded, "eligible_count": len(eligible)}


def audit_fields(primary: Optional[dict], detection_expected: bool) -> dict:
    """primary 기록의 outbox 전송 상태에서 비동기 감사 필드와 audit_status를 계산한다.
    t_audit_write는 outbox에 동기로 남는 값이라 push 전에도 알 수 있으면 채운다.
    t_audit_push/commit_sha는 실제 push가 끝난 뒤에만 채우고 그 전엔 null 유지."""
    fields = {"t_audit_write": None, "t_audit_push": None, "commit_sha": None,
              "audit_record_id": None, "audit_status": None, "audit_status_reason": None}
    if primary is None:
        if detection_expected:
            fields.update(audit_status="pending",
                          audit_status_reason="탐지·판정은 있으나 대응하는 primary 감사기록을 아직 확인하지 못함")
        else:
            fields.update(audit_status="not_applicable", audit_status_reason="탐지·판정이 없어 감사기록 대상 아님")
        return fields
    outbox = primary.get("outbox") or {}
    status = outbox.get("status")
    fields.update(audit_record_id=primary.get("record_id"), t_audit_write=outbox.get("t_audit_write"))
    if status == "pushed" and outbox.get("commit_sha"):
        fields.update(t_audit_push=outbox.get("t_audit_push"), commit_sha=outbox["commit_sha"],
                      audit_status="complete")
    elif status == "failed":
        fields.update(audit_status="failed",
                      audit_status_reason=f"Git push 실패(attempts={outbox.get('attempts')}): {outbox.get('last_error')}")
    elif status is None:
        fields.update(audit_status="pending", audit_status_reason="outbox 엔트리 없음(전송 상태 미확인)")
    else:
        fields.update(audit_status="pending", audit_status_reason=f"Git push 대기 중(outbox status={status})")
    return fields


def judgment_fields(selection: dict, trial: dict) -> tuple:
    """live 권위 상태가 없는 과거 trial의 판정 필드를 감사기록으로 보완한다.
    반환: (fields, inferred, notes) - inferred는 감사기록 자체에 근거가 없어 추론한 값의
    출처, notes는 추론을 허용하지 않아 채우지 못한 사유. detection_source/detector는 최초
    유효 탐지 기록의, action 계열은 primary의 것이다. detector 추론은 pilot 한정이다."""
    primary, first = selection["primary"], selection["first_detection"]
    if primary is None:
        return ({"detected": False, "detection_source": None, "detector": None, "action": "none",
                 "decision_outcome": None, "idempotency_key": None, "promotion_verified": None}, {}, [])
    inferred, notes = {}, []
    source = first.get("signal_source")
    if source == "anomaly":
        detection_source = "predictive"
        detector = (first.get("evidence") or {}).get("detector")
        if detector is None and trial.get("detector_process"):
            if trial.get("is_pilot") is True:
                detector = trial["detector_process"]
                # 문구는 이미 보존된 pilot JSON의 reconciliation.inferred_fields와 글자 그대로 같아야 한다
                # (바꾸면 재실행이 provenance를 다시 쓰게 되어 idempotent가 깨진다). pilot 한정이라는 제약은
                # 이 분기의 is_pilot 조건이 강제한다.
                inferred["detector"] = ("trial.detector_process(arm 배선값) - 이 감사기록은 evidence에 detector "
                                        "태그가 없는 2026-09-19 이전 기록이라 기록 자체로는 확인 불가")
            else:
                notes.append("감사기록 evidence에 detector 태그가 없고 본 실험(is_pilot=false) 데이터에는 "
                             "detector 추론을 허용하지 않아 detector=null로 남김")
    elif source == "alertmanager":
        detection_source, detector = "reactive", "alertmanager"
    else:
        detection_source, detector = None, None
    outcome = primary.get("outcome")
    return ({
        "detected": True, "detection_source": detection_source, "detector": detector,
        "action": primary.get("action") or "none", "decision_outcome": outcome,
        "idempotency_key": primary.get("idempotency_key"),
        "promotion_verified": True if outcome == EXECUTED_VERIFIED else False if outcome == EXECUTED_UNVERIFIED else None,
    }, inferred, notes)


def fetch_audit_records(run_id: str, base_url: str = DEFAULT_RECOVERY_POLICY_URL) -> list:
    resp = requests.get(f"{base_url}/admin/audit/{quote(run_id, safe='')}", timeout=ADMIN_TIMEOUT_SEC)
    resp.raise_for_status()
    return resp.json()["records"]


def wait_for_primary_audit(
    run_id: str, expected_key: Optional[str], expected_outcome: Optional[str],
    timeout_sec: float, poll_sec: float, base_url: str = DEFAULT_RECOVERY_POLICY_URL,
    fetch_fn: Callable = fetch_audit_records,
) -> tuple:
    """trial 종료 시 짧은 bounded wait(run_once용). authoritative 판정(expected_*)과
    일치하는 primary 기록이 push까지 끝나면 즉시 반환하고, 시간 안에 못 끝나면
    마지막으로 관측한 상태(pending/failed + 사유)를 그대로 반환한다 - Git 지연이나
    조회 실패는 여기서 예외로 번지지 않고 audit_status로만 남는다(outcome 불변).
    반환: (audit_fields, selection|None)."""
    deadline = time.monotonic() + timeout_sec
    fields, selection = None, None
    while True:
        try:
            selection = select_records(fetch_fn(run_id, base_url), run_id)
            primary = selection["primary"]
            matches = primary is not None and (
                expected_key is None
                or (primary.get("idempotency_key") == expected_key and primary.get("outcome") == expected_outcome))
            fields = audit_fields(primary if matches else None, detection_expected=True)
        except Exception as e:
            fields = audit_fields(None, detection_expected=True)
            fields["audit_status_reason"] = f"audit 조회 실패: {type(e).__name__}: {e}"
        if fields["audit_status"] == "complete" or time.monotonic() >= deadline:
            return fields, selection
        time.sleep(poll_sec)


def _strip_times(trial: dict) -> dict:
    out = {k: v for k, v in trial.items() if k not in _TIME_KEYS}
    if isinstance(out.get("reconciliation"), dict):
        out["reconciliation"] = {k: v for k, v in out["reconciliation"].items() if k != "reconciled_at"}
    return out


def reconcile_trial(trial: dict, records: list, now_iso: str) -> tuple:
    """순수 함수 - (갱신된 trial, 보고)를 돌려준다. 바뀐 게 없으면 입력 trial을 그대로
    반환한다(idempotent). native는 recovery-policy 미개입이라 손대지 않는다."""
    if trial.get("arm") == "native":
        return trial, {"changed": False, "skipped": "native(recovery-policy 미개입)"}
    run_id = trial["run_id"]
    selection = select_records(records, run_id)
    updated = dict(trial)
    report = {"changed": False, "primary_record_id": (selection["primary"] or {}).get("record_id"),
              "excluded": selection["excluded"], "judgment_supplemented": False, "notes": []}

    detection_expected = trial.get("t_detection") is not None or bool(trial.get("detected"))
    if trial.get("judgment_source") != "live_state":
        if selection["primary"] is None and trial.get("t_detection") is not None:
            report["notes"].append("t_detection은 있으나 귀속 가능한 감사기록이 없어 판정 필드를 추측해 채우지 않음")
        else:
            fields, inferred, inference_notes = judgment_fields(selection, trial)
            report["notes"].extend(inference_notes)
            original = (trial.get("reconciliation") or {}).get("original_values") or {k: trial.get(k) for k in JUDGMENT_KEYS}
            updated.update(fields)
            updated["judgment_source"] = "audit_reconcile"
            updated["reconciliation"] = {
                "tool": "reconcile_audit.py",
                "source": "recovery-policy GET /admin/audit/{run_id} (audit-log + outbox)",
                "primary_record_id": (selection["primary"] or {}).get("record_id"),
                "first_detection_record_id": (selection["first_detection"] or {}).get("record_id"),
                "excluded_records": selection["excluded"],
                "supplemented_fields": [k for k in JUDGMENT_KEYS if original.get(k) != fields[k]],
                "inferred_fields": inferred,
                "original_values": original,
                "reconciled_at": (trial.get("reconciliation") or {}).get("reconciled_at"),
            }
            detection_expected = fields["detected"]
            report["judgment_supplemented"] = True
    updated.update(audit_fields(selection["primary"], detection_expected))

    if _strip_times(updated) == _strip_times(trial):
        return trial, report
    updated["audit_reconciled_at"] = now_iso
    if isinstance(updated.get("reconciliation"), dict):
        updated["reconciliation"]["reconciled_at"] = now_iso
    report["changed"] = True
    return updated, report


def reconcile_file(path: Path, fetch_fn: Callable = fetch_audit_records,
                   base_url: str = DEFAULT_RECOVERY_POLICY_URL, dry_run: bool = False,
                   now_fn: Callable = lambda: datetime.now(timezone.utc).isoformat()) -> dict:
    trial = json.loads(path.read_text(encoding="utf-8"))
    if trial.get("arm") == "native":
        return {"path": str(path), "changed": False, "skipped": "native(recovery-policy 미개입)"}
    updated, report = reconcile_trial(trial, fetch_fn(trial["run_id"], base_url), now_fn())
    report["path"] = str(path)
    if report["changed"] and not dry_run:
        backup = path.with_name(path.name + ".pre-reconcile.bak")
        if not backup.exists():
            shutil.copy2(path, backup)  # 첫 수정 전 원본을 그대로 보존(이후 실행에서는 덮어쓰지 않음)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="trial 결과의 감사 필드를 recovery-policy outbox/audit 상태로 재조정(idempotent)")
    parser.add_argument("paths", nargs="+", help="trial-*.json 경로")
    parser.add_argument("--recovery-policy-url", default=DEFAULT_RECOVERY_POLICY_URL)
    parser.add_argument("--dry-run", action="store_true", help="파일을 쓰지 않고 보고만 출력")
    args = parser.parse_args()
    failed = 0
    for p in args.paths:
        try:
            report = reconcile_file(Path(p), base_url=args.recovery_policy_url, dry_run=args.dry_run)
        except Exception as e:
            failed += 1
            print(f"[실패] {p}: {type(e).__name__}: {e}")
            continue
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
