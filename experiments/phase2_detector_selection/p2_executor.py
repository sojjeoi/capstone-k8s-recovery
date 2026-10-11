#!/usr/bin/env python3
"""Phase 2 같은-tick detector 실행기(2026-10-11) - prereg §4·§4.1·§4.3·§8.1.

평가 1회 = 동결 c1_shadow_detector.evaluate_once 1회(같은 snapshot·Prometheus 원문·무결성/신선도 검사). 그 점수는 외부 공급
c3-A scaler·모델로 계산되고(§5.1 검사 통과 뒤), 같은 기록의 features.cpu_mean으로 CPU 후보를 판정한다(same-tick parity).
두 detector 모두 rules[<detector_id>]에 value·threshold·is_anomalous·consecutive·condition_met + state·role을 남긴다.

권한(§4 1차 방어):
  - run_shadow: 송신 함수·정책 URL이 없다(ACTIVE 0개 profile 전용 - Stage 1). 어떤 detector도 신호를 보낼 수 없다.
  - run_active: ACTIVE 정확히 1개 profile 전용(Stage 2). 송신 payload는 ACTIVE의 rules 항목에서만 만든다. SHADOW 항목은
    payload 생성 함수에 넘어가지 않는다.
판정 상태기계·송신 결과 분류·정지 규칙은 동결 c1_bck_live_detector와 같다(score_server.advance_streak, ld.classify_send_result)."""
import sys
sys.dont_write_bytecode = True
import argparse  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import p2_common as pc  # noqa: E402

import c1_live_detector as ld  # noqa: E402
import c1_policy_link as link  # noqa: E402
from c1_shadow_detector import (COOLDOWN_SEC, CONSECUTIVE_THRESHOLD, EVAL_INTERVAL_SEC, WINDOW_SEC,  # noqa: E402
                                EvidenceWriteFailed, _must_write, evaluate_once, k8s_snapshot, prom_instant, prom_range)

LABEL = "phase2_same_tick_v1"
EXIT_PROFILE_INVALID = 7


def _setup(profile_path, artifact_root, stage):
    loaded = pc.load_profile(profile_path, stage=stage)
    art = pc.verify_artifacts(artifact_root)
    pc.verify_rule_spec()
    return loaded, art


def _start_record(kind, run_id, loaded, art, now_fn, **extra):
    return {"record_type": kind, "run_id": run_id, "detector": LABEL, "environment_profile_id": loaded["profile"]["profile_id"],
            "environment_profile_sha256": loaded["sha256"],
            "roles": {d["detector_id"]: d["role"] for d in loaded["profile"]["detectors"]}, "active_detector": loaded["active"],
            "thresholds": {d: pc.FROZEN_DETECTORS[d]["threshold"] for d in pc.DETECTOR_IDS},
            "rule_spec_sha256": pc.FROZEN_DETECTORS[pc.CPU_ID]["artifact"]["rule_spec_sha256"],
            "artifact_hashes": art["hashes"], "kept_features": art["schema"]["kept_feature_names"],
            "window_sec": WINDOW_SEC, "eval_interval_sec": EVAL_INTERVAL_SEC, "consecutive_threshold": CONSECUTIVE_THRESHOLD,
            "cooldown_sec": COOLDOWN_SEC, "started_at_utc": now_fn().isoformat(), **extra}


def tick(rec: dict, art: dict, states: dict, roles: dict, monotonic_now: float) -> dict:
    """같은 평가 기록으로 두 detector 판정. IF 점수 = evaluate_once가 c3-A로 계산한 rec["score"]."""
    if rec.get("status") == "scored":
        pc.check_features(rec["features"], art["schema"])
    return {d: {**pc.judge(d, rec, rec.get("score"), states[d], monotonic_now), "role": roles[d]} for d in pc.DETECTOR_IDS}


def _loop(run_id, art, roles, on_record, *, max_evals, now_fn, monotonic_fn, sleep_fn, snapshot_fn, range_fn, instant_fn):
    states = {d: {"consecutive": 0, "last_signal": None} for d in pc.DETECTOR_IDS}
    seq = 0
    while max_evals is None or seq < max_evals:
        seq += 1
        rec = evaluate_once(art, now_fn=now_fn, snapshot_fn=snapshot_fn, range_fn=range_fn, instant_fn=instant_fn)
        rules = tick(rec, art, states, roles, monotonic_fn())
        rec.update(record_type="c1_evaluation", run_id=run_id, evaluation_seq=seq, rules=rules)
        code = on_record(rec, seq)
        if code is not None:
            return code
        # 진행 표시는 순번·상태만(판정 결과를 운영자에게 보이지 않음 - 유효성 판정 전 결과 노출 방지)
        print(f"[{rec.get('score_completed_at_utc') or rec.get('skipped_at_utc')}] seq={seq} {rec['status']}", flush=True)
        sleep_fn(EVAL_INTERVAL_SEC)
    return ld.EXIT_OK


def run_shadow(run_id: str, evidence_path: Path, *, profile_path, artifact_root, max_evals=None,
               now_fn=lambda: datetime.now(timezone.utc), monotonic_fn=time.monotonic, sleep_fn=time.sleep,
               snapshot_fn=k8s_snapshot, range_fn=prom_range, instant_fn=prom_instant) -> int:
    """Stage 1 - ACTIVE 0개 profile만 받는다. 송신 경로 없음."""
    loaded, art = _setup(profile_path, artifact_root, "stage1")
    roles = {d["detector_id"]: d["role"] for d in loaded["profile"]["detectors"]}
    with open(evidence_path, "a", encoding="utf-8") as fh:
        _must_write(fh, _start_record("c1_shadow_start", run_id, loaded, art, now_fn, signal_send=False, actuating_rule=None))

        def write(rec, seq):
            rec.update(actuating_rule=None, signal_send=False)
            _must_write(fh, rec)
        return _loop(run_id, art, roles, write, max_evals=max_evals, now_fn=now_fn, monotonic_fn=monotonic_fn,
                     sleep_fn=sleep_fn, snapshot_fn=snapshot_fn, range_fn=range_fn, instant_fn=instant_fn)


def build_payload(start: dict, rec: dict, active: str) -> dict:
    """동결 build_v2_payload 검증(대상 Pod·UID·창·시각)을 거친 뒤 ACTIVE identity·값으로 바꾼다(bd.build_payload와 같은 방식)."""
    j = rec["rules"][active]
    if j["role"] != pc.ACTIVE or start.get("active_detector") != active:
        raise link.PayloadError(f"ACTIVE가 아닌 판정으로 payload 생성 시도: {active}={j['role']}")
    base = link.build_v2_payload(start, {**rec, "condition_met": j["condition_met"],
                                         "condition_met_at_utc": j["condition_met_at_utc"],
                                         "consecutive_anomalous": j["consecutive"]})
    base.update(start["signal_identity"], score=j["value"])
    return base


def run_active(run_id: str, evidence_path: Path, *, profile_path, artifact_root, policy_base_url: str,
               expected_version: str, expected_mode: str, max_evals=None, now_fn=lambda: datetime.now(timezone.utc),
               monotonic_fn=time.monotonic, sleep_fn=time.sleep, snapshot_fn=k8s_snapshot, range_fn=prom_range,
               instant_fn=prom_instant, check_fn=link.check_receiver, send_fn=link.send) -> int:
    """Stage 2 - ACTIVE 정확히 1개 profile만 받는다. ACTIVE의 condition_met에서만 송신(조치 결과 뒤 post_action)."""
    if expected_mode != "act":
        raise SystemExit("run_active는 act 전용")
    loaded, art = _setup(profile_path, artifact_root, "stage2")
    active = loaded["active"]
    roles = {d["detector_id"]: d["role"] for d in loaded["profile"]["detectors"]}
    ident = pc.signal_identity(active, art["hashes"])
    rc = check_fn(policy_base_url, "guarded", expected_version=expected_version, expected_mode=expected_mode)
    url = policy_base_url.rstrip("/") + "/signal/v2"
    state = {"post_action": False}
    with open(evidence_path, "a", encoding="utf-8") as fh:
        start = _start_record("c1_live_start", run_id, loaded, art, now_fn, actuating_rule=active, signal_identity=ident,
                              threshold=ident["threshold"], policy_url=url, expected_version=expected_version,
                              expected_mode=expected_mode, receiver_check=rc, send_timeout=list(ld.SEND_TIMEOUT))
        _must_write(fh, start)
        if not rc.get("ok"):
            return ld.EXIT_RECEIVER

        def write_and_send(rec, seq):
            j = rec["rules"][active]
            rec.update(post_action=state["post_action"], actuating_rule=active, is_anomalous=j["is_anomalous"],
                       consecutive_anomalous=j["consecutive"], condition_met=j["condition_met"],
                       condition_met_at_utc=j["condition_met_at_utc"])
            _must_write(fh, rec)
            if not (j["condition_met"] and not state["post_action"]):
                return None
            try:
                payload = build_payload(start, rec, active)
            except link.PayloadError as e:
                _must_write(fh, {"record_type": "c1_policy_payload_error", "run_id": run_id, "evaluation_seq": seq,
                                 "error": str(e), "at_utc": now_fn().isoformat()})
                return ld.EXIT_PAYLOAD
            _must_write(fh, {"record_type": "c1_policy_send_attempt", "run_id": run_id, "evaluation_seq": seq,
                             "detector_id": active, "url": url, "attempted_at_utc": now_fn().isoformat()})
            sr = send_fn(url, payload, timeout=ld.SEND_TIMEOUT)
            decision = ld.classify_send_result(sr)
            _must_write(fh, {"record_type": "c1_policy_send_result", "run_id": run_id, "evaluation_seq": seq,
                             "detector_id": active, "completed_at_utc": now_fn().isoformat(), "decision": decision, **sr})
            if decision == "stop_delivery":
                return ld.EXIT_DELIVERY
            if decision == "stop_policy":
                return ld.EXIT_POLICY
            state["post_action"] = state["post_action"] or decision == "post_action"
            return None
        return _loop(run_id, art, roles, write_and_send, max_evals=max_evals, now_fn=now_fn, monotonic_fn=monotonic_fn,
                     sleep_fn=sleep_fn, snapshot_fn=snapshot_fn, range_fn=range_fn, instant_fn=instant_fn)


def main(argv=None) -> int:  # pragma: no cover - 라이브 진입점(실행기가 subprocess로 띄운다)
    p = argparse.ArgumentParser(description="Phase 2 같은-tick 실행기. shadow = 송신 없음(Stage 1), active = ACTIVE만 송신(Stage 2)")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("shadow", "active"):
        s = sub.add_parser(name)
        s.add_argument("--run-id", required=True)
        s.add_argument("--evidence-log", required=True, type=Path)
        s.add_argument("--profile", required=True, type=Path)
        s.add_argument("--artifact-root", required=True, type=Path)
        if name == "active":
            s.add_argument("--policy-url", required=True)
            s.add_argument("--expected-version", required=True)
            s.add_argument("--expected-mode", required=True, choices=["act"])
    a = p.parse_args(argv)
    try:
        if a.cmd == "shadow":
            return run_shadow(a.run_id, a.evidence_log, profile_path=a.profile, artifact_root=a.artifact_root)
        return run_active(a.run_id, a.evidence_log, profile_path=a.profile, artifact_root=a.artifact_root,
                          policy_base_url=a.policy_url, expected_version=a.expected_version, expected_mode=a.expected_mode)
    except pc.ProfileInvalid as e:
        print(f"PROFILE_INVALID - 시작 거부·중단: {e}", file=sys.stderr, flush=True)
        return EXIT_PROFILE_INVALID
    except EvidenceWriteFailed as e:
        print(f"필수 기록 실패 - 종료: {e}", file=sys.stderr, flush=True)
        return ld.EXIT_EVIDENCE


if __name__ == "__main__":
    sys.exit(main())
