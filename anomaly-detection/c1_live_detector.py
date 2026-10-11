"""v3.3-c1 실전 탐지기 변형(새 파일) - 동결 c1_shadow_detector의 계산 경로를 그대로 가져다 쓰고, 송신만 guarded 정책으로 한다.
동결 파일은 수정하지 않는다. 모델·입력·임계값·3연속·쿨다운은 동결 경로 그대로(load_artifacts·evaluate_once·step_streak).

동작:
1) 시작 시 수신자 확인(c1_policy_link.check_receiver: role·version·mode 필수 일치). 실패하면 시작 기록만 남기고 종료(EXIT_RECEIVER).
2) 평가 기록은 동결 경로와 같은 c1_evaluation 레코드(필수 기록 - 실패면 EvidenceWriteFailed → EXIT_EVIDENCE).
3) 송신 조건이면: v2 payload 생성 → 시도 기록(필수, 실패면 송신하지 않음) → 송신 → 결과 기록(필수, 실패면 재송신 없이 종료).
4) 종료 조건(실행기의 기존 생존 감시가 탐지기 종료를 보고 중단·정리한다):
   - 전달 성공(delivered) 외 전부(결과 불명·연결 실패·HTTP 오류·해석 불가) → EXIT_DELIVERY
   - 정책 결과가 결과 불명·미검증·내부 오류·차단·상태 오류·충돌, 또는 응답에 blocked/state_error/audit_error/ledger_error 표시,
     또는 위험 결과의 중복 응답 → EXIT_POLICY
5) promoted_verified 이후에는 추가 송신을 하지 않는다(post_action) - 서비스 확인 중 신호로 차단·정리가 일어나지 않게.
   평가·기록은 계속한다(전환 뒤에는 동결 입력 규칙상 건너뜀이 기록된다). 서비스 확인 결과를 보고 중단·정리하는 일은
   실행기 쪽(experiments/guarded_live_detector_runner.guard_after_action)이 맡는다.
송신 읽기 시간 상한(SEND_TIMEOUT) 근거: 정책 동기 처리 = 대상 조회 2회(guarded_k8s read_deadline 10초씩) + CLI 30초 +
전환 검증 10초 + 상태·원장·감사 저장(수 초) ≈ 60초 → 여유 30초를 더해 90초. guarded_k8s 기본 기한이 바뀌면 같이 바꾼다.
"""
import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import c1_policy_link as link
from c1_shadow_detector import (COOLDOWN_SEC, CONSECUTIVE_THRESHOLD, DEFAULT_ARTIFACTS, EVAL_INTERVAL_SEC, WINDOW_SEC,
                                EvidenceWriteFailed, _must_write, evaluate_once, k8s_snapshot, load_artifacts,
                                prom_instant, prom_range, step_streak)

DETECTOR_ID = "c1_live_v3.3"
EXIT_OK, EXIT_EVIDENCE, EXIT_RECEIVER, EXIT_PAYLOAD, EXIT_DELIVERY, EXIT_POLICY = 0, 2, 3, 4, 5, 6
SEND_TIMEOUT = (3.0, 90.0)
STOP_OUTCOMES = {"promotion_unknown", "promoted_unverified", "internal_error", "blocked_state", "blocked_unresolved",
                 "state_unavailable", "state_error_no_promote", "delivery_conflict", "aborted_ledger_failed",
                 "aborted_audit_failed", "rejected_schema"}
FLAG_FIELDS = ("blocked", "state_error", "audit_error", "ledger_error")


def classify_send_result(sr: dict) -> str:
    """continue / post_action / stop_delivery / stop_policy"""
    if sr.get("delivery") != "delivered":
        return "stop_delivery"
    if any(sr.get(f) for f in FLAG_FIELDS):
        return "stop_policy"
    outcome = sr.get("policy_outcome")
    if outcome == "duplicate_delivery":
        outcome = sr.get("original_outcome")
    if outcome in STOP_OUTCOMES:
        return "stop_policy"
    return "post_action" if outcome == "promoted_verified" else "continue"


def run_live(run_id: str, evidence_path: Path, *, policy_base_url: str, expected_version: str, expected_mode: str,
             artifacts=DEFAULT_ARTIFACTS, max_evals=None, now_fn=lambda: datetime.now(timezone.utc),
             monotonic_fn=time.monotonic, sleep_fn=time.sleep, snapshot_fn=k8s_snapshot, range_fn=prom_range,
             instant_fn=prom_instant, check_fn=link.check_receiver, send_fn=link.send) -> int:
    art = load_artifacts(Path(artifacts))
    rc = check_fn(policy_base_url, "guarded", expected_version=expected_version, expected_mode=expected_mode)
    state, seq, post_action = {"consecutive": 0, "last_signal": None}, 0, False
    url = policy_base_url.rstrip("/") + "/signal/v2"
    with open(evidence_path, "a", encoding="utf-8") as fh:
        start = {"record_type": "c1_live_start", "run_id": run_id, "detector": DETECTOR_ID, "artifacts": str(artifacts),
                 "artifact_hashes": art["hashes"], "threshold": art["threshold"],
                 "kept_features": art["schema"]["kept_feature_names"], "policy_url": url,
                 "expected_version": expected_version, "expected_mode": expected_mode, "receiver_check": rc,
                 "send_timeout": list(SEND_TIMEOUT), "window_sec": WINDOW_SEC, "eval_interval_sec": EVAL_INTERVAL_SEC,
                 "consecutive_threshold": CONSECUTIVE_THRESHOLD, "cooldown_sec": COOLDOWN_SEC,
                 "started_at_utc": now_fn().isoformat()}
        _must_write(fh, start)
        if not rc.get("ok"):
            return EXIT_RECEIVER
        while max_evals is None or seq < max_evals:
            seq += 1
            rec = evaluate_once(art, now_fn=now_fn, snapshot_fn=snapshot_fn, range_fn=range_fn, instant_fn=instant_fn)
            rec.update(record_type="c1_evaluation", run_id=run_id, evaluation_seq=seq, post_action=post_action)
            step_streak(rec, state, art["threshold"], monotonic_fn())
            _must_write(fh, rec)
            if rec["condition_met"] and not post_action:
                try:
                    payload = link.build_v2_payload(start, rec)
                except link.PayloadError as e:
                    _must_write(fh, {"record_type": "c1_policy_payload_error", "run_id": run_id, "evaluation_seq": seq,
                                     "error": str(e), "at_utc": now_fn().isoformat()})
                    return EXIT_PAYLOAD
                _must_write(fh, {"record_type": "c1_policy_send_attempt", "run_id": run_id, "evaluation_seq": seq,
                                 "url": url, "attempted_at_utc": now_fn().isoformat()})
                sr = send_fn(url, payload, timeout=SEND_TIMEOUT)
                decision = classify_send_result(sr)
                _must_write(fh, {"record_type": "c1_policy_send_result", "run_id": run_id, "evaluation_seq": seq,
                                 "completed_at_utc": now_fn().isoformat(), "decision": decision, **sr})
                if decision == "stop_delivery":
                    return EXIT_DELIVERY
                if decision == "stop_policy":
                    return EXIT_POLICY
                post_action = post_action or decision == "post_action"
            print(f"[{rec.get('score_completed_at_utc') or rec.get('skipped_at_utc')}] seq={seq} {rec['status']} "
                  f"score={rec.get('score')} 연속={rec['consecutive_anomalous']} post_action={post_action}", flush=True)
            sleep_fn(EVAL_INTERVAL_SEC)
    return EXIT_OK


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="v3.3-c1 실전 탐지기 변형 - guarded 정책 전용(수신자·버전·모드 확인 필수)")
    p.add_argument("--run-id", required=True)
    p.add_argument("--evidence-log", required=True, type=Path)
    p.add_argument("--policy-url", required=True)
    p.add_argument("--expected-version", required=True)
    p.add_argument("--expected-mode", required=True, choices=["record_only", "act"])
    p.add_argument("--artifacts-dir", default=str(DEFAULT_ARTIFACTS))
    a = p.parse_args(argv)
    try:
        return run_live(a.run_id, a.evidence_log, policy_base_url=a.policy_url, expected_version=a.expected_version,
                        expected_mode=a.expected_mode, artifacts=a.artifacts_dir)
    except EvidenceWriteFailed as e:
        print(f"필수 기록 실패 - 종료: {e}", file=sys.stderr, flush=True)
        return EXIT_EVIDENCE


if __name__ == "__main__":
    sys.exit(main())
