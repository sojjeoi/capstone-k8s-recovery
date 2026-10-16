#!/usr/bin/env python3
"""네트워크 지연 환경 같은-tick detector 실행기(2026-10-17, DEVELOPMENT - 동결 전, 라이브 실행 없음).

Phase 2 p2_executor와 같은 구조에 net_probe를 더한 3개 후보 버전. 동결 코드는 고치지 않고 import해서 쓴다:
평가 1회 = 동결 c1_shadow_detector.evaluate_once 1회(CPU·IF 특징), 그 직후 같은 tick에 net_probe 질의 1회(kubelet readiness probe
소요 시간의 60초 비율, active Pod 정확히 1개일 때만 값). 세 후보 모두 rules[<detector_id>]에 value·threshold·is_anomalous·
consecutive·condition_met + state·role을 남긴다.

권한(§4 1차 방어)은 Phase 2와 같다:
  - run_shadow: 송신 함수·정책 URL이 없다(ACTIVE 0개 profile 전용 - Stage 1). 어떤 detector도 신호를 보낼 수 없다.
  - run_active: ACTIVE 정확히 1개 profile 전용(Stage 2). 송신 payload는 ACTIVE의 rules 항목에서만 만든다(p2_executor.build_payload 재사용).
진행 표시는 순번·상태만 찍는다(판정 결과를 운영자에게 보이지 않음)."""
import sys
sys.dont_write_bytecode = True
import argparse  # noqa: E402
import math  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import n2_common as nc  # noqa: E402

pc = nc.pc
import p2_executor as p2ex  # noqa: E402  (동결 - build_payload·상수만 재사용)
import c1_live_detector as ld  # noqa: E402
import c1_policy_link as link  # noqa: E402
from c1_shadow_detector import (COOLDOWN_SEC, CONSECUTIVE_THRESHOLD, EVAL_INTERVAL_SEC, WINDOW_SEC,  # noqa: E402
                                EvidenceWriteFailed, _must_write, evaluate_once, k8s_snapshot, prom_instant, prom_range)

LABEL = "phase2_network_same_tick_v1"
EXIT_PROFILE_INVALID = p2ex.EXIT_PROFILE_INVALID
build_payload = p2ex.build_payload


def read_net_probe(instant_fn, now_fn) -> dict:
    """한 tick의 net_probe 값. active Pod의 시계열이 정확히 1개이고 유한할 때만 value, 그 밖(0개·2개 이상·NaN·오류)은 None(→ UNKNOWN)."""
    sent = now_fn()
    body, err = None, None
    try:
        body = instant_fn(nc.NET_EXPR)
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    received = now_fn()
    value, n = None, None
    if err is None:
        try:
            res = body["data"]["result"]
            n = len(res)
            if n == 1:
                v = float(res[0]["value"][1])
                value = v if math.isfinite(v) else None
                err = None if value is not None else "non_finite_value"
            else:
                err = f"series_count={n}"
        except (KeyError, IndexError, TypeError, ValueError) as e:
            err = f"parse_error: {type(e).__name__}: {e}"
    return {"value": value, "n_series": n, "error": err, "promql": nc.NET_EXPR,
            "sent_at_utc": sent.isoformat(), "received_at_utc": received.isoformat()}


def _setup(profile_path, artifact_root, stage):
    loaded = nc.load_profile(profile_path, stage=stage)
    art = pc.verify_artifacts(artifact_root)
    pc.verify_rule_spec()
    nc.verify_net_calibration()
    return loaded, art


def _start_record(kind, run_id, loaded, art, now_fn, **extra):
    return {"record_type": kind, "run_id": run_id, "detector": LABEL, "environment_profile_id": loaded["profile"]["profile_id"],
            "environment_profile_sha256": loaded["sha256"],
            "roles": {d["detector_id"]: d["role"] for d in loaded["profile"]["detectors"]}, "active_detector": loaded["active"],
            "thresholds": {d: nc.FROZEN_DETECTORS[d]["threshold"] for d in nc.DETECTOR_IDS},
            "rule_spec_sha256": pc.FROZEN_DETECTORS[nc.CPU_ID]["artifact"]["rule_spec_sha256"],
            "net_probe_definition_sha256": nc.NET_DEFINITION_SHA256, "net_probe_calibration_sha256": nc.NET_CALIBRATION_SHA256,
            "artifact_hashes": art["hashes"], "kept_features": art["schema"]["kept_feature_names"],
            "window_sec": WINDOW_SEC, "eval_interval_sec": EVAL_INTERVAL_SEC, "consecutive_threshold": CONSECUTIVE_THRESHOLD,
            "cooldown_sec": COOLDOWN_SEC, "started_at_utc": now_fn().isoformat(), **extra}


def tick(rec: dict, art: dict, states: dict, roles: dict, monotonic_now: float) -> dict:
    """같은 평가 기록으로 세 detector 판정. IF 점수 = evaluate_once가 c3-A로 계산한 rec["score"], net_probe = rec["net_probe"]."""
    if rec.get("status") == "scored":
        pc.check_features(rec["features"], art["schema"])
    return {d: {**nc.judge(d, rec, rec.get("score"), states[d], monotonic_now), "role": roles[d]} for d in nc.DETECTOR_IDS}


def _loop(run_id, art, roles, on_record, *, max_evals, now_fn, monotonic_fn, sleep_fn, snapshot_fn, range_fn, instant_fn):
    states = {d: {"consecutive": 0, "last_signal": None} for d in nc.DETECTOR_IDS}
    seq = 0
    while max_evals is None or seq < max_evals:
        seq += 1
        rec = evaluate_once(art, now_fn=now_fn, snapshot_fn=snapshot_fn, range_fn=range_fn, instant_fn=instant_fn)
        rec["net_probe"] = read_net_probe(instant_fn, now_fn) if rec.get("status") == "scored" else None
        rules = tick(rec, art, states, roles, monotonic_fn())
        rec.update(record_type="c1_evaluation", run_id=run_id, evaluation_seq=seq, rules=rules)
        code = on_record(rec, seq)
        if code is not None:
            return code
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
    ident = nc.signal_identity(active, art["hashes"])
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


def main(argv=None) -> int:  # pragma: no cover - 라이브 진입점(세션 실행기가 subprocess로 띄운다, 아직 사용하지 않음)
    p = argparse.ArgumentParser(description="네트워크 환경 같은-tick 실행기. shadow = 송신 없음(Stage 1), active = ACTIVE만 송신(Stage 2)")
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
