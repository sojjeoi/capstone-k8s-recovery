"""B/C/K 공통 평가 tick 탐지기(새 파일, 2026-10-07) - 계약: docs/design/phase8-v33c1-guarded-bck-comparison-contract-draft-20261007.md

동결 경로를 import만 한다(c1_shadow_detector·c1_live_detector·c1_policy_link·score_server 수정 없음):
1) 평가 1회 = 동결 evaluate_once 1회. 같은 snapshot·같은 Prometheus 원문·같은 무결성/신선도 검사에서 IF score와
   features.cpu_mean을 함께 얻는다(규칙별 추가 조회 없음).
2) 같은 레코드로 세 규칙을 판정한다. 연속·쿨다운은 동결 advance_streak(value < threshold = 이상)를 규칙마다 독립 상태로 부른다.
   B: (score, -0.07667538512242333) / C: (-cpu_mean, -1.6842544293089357) / K: (-cpu_mean, -2.7) - 즉 cpu_mean > T (strict).
   건너뛴 평가는 세 상태 모두 연속 0(동결 step_streak과 같은 처리).
3) 평가 기록 1줄에 세 판정(rules)과 actuating_rule을 남긴다. 최상위 condition_met·consecutive_anomalous는 실제 arm 규칙 값
   (기존 실행기·D5 관문·준비 확인이 읽는 형식 그대로). 최상위 score는 IF score(기존 형식).
4) 실제 arm 규칙만 /signal/v2로 송신한다(나머지 두 규칙은 기록만). 송신·결과 분류·종료·post_action은 동결 c1_live_detector와 같다.
신호 identity(detector_id·model_version·model_hash·artifact_manifest_hash·threshold)는 signal_identity() 하나에서만 만든다 -
실행기가 같은 함수로 guarded context를 설정한다(정책은 이 값들의 정확 일치만 본다)."""
import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import c1_live_detector as ld
import c1_policy_link as link
from c1_shadow_detector import (COOLDOWN_SEC, CONSECUTIVE_THRESHOLD, DEFAULT_ARTIFACTS, EVAL_INTERVAL_SEC,
                                EXPECTED_THRESHOLD, WINDOW_SEC, EvidenceWriteFailed, _must_write, evaluate_once,
                                k8s_snapshot, load_artifacts, prom_instant, prom_range)
from score_server import advance_streak

RULE_SPEC = Path(__file__).resolve().parent / "bck_rules" / "rule-spec-v1.json"
ARMS = ("B", "C", "K")
THRESHOLDS = {"B": EXPECTED_THRESHOLD, "C": 1.6842544293089357, "K": 2.7}  # 동결 - 규칙 명세와 정확히 같아야 시작
IDENTITY = {"B": ("c1_bck_v1_if", "v3.3-c1"), "C": ("c1_bck_v1_fixed_cal", "fixed-cpu-gt-1.6842544293089357"),
            "K": ("c1_bck_v1_fixed_legacy", "fixed-cpu-gt-2.7")}
ANOMALOUS_IF = {"B": "if_score < threshold", "C": "cpu_mean > threshold", "K": "cpu_mean > threshold"}


def load_spec(path: Path = RULE_SPEC) -> dict:
    """규칙 명세를 읽고 코드 상수와 대조한다(fail-closed). 반환 {"spec", "sha256", "path"}."""
    raw = Path(path).read_bytes()
    spec = json.loads(raw)
    rules, common, bad = spec.get("rules") or {}, spec.get("common") or {}, []
    for a in ARMS:
        r = rules.get(a) or {}
        if (r.get("threshold"), r.get("detector_id"), r.get("model_version"), r.get("anomalous_if")) != (
                THRESHOLDS[a], *IDENTITY[a], ANOMALOUS_IF[a]):
            bad.append(a)
    if common.get("consecutive") != CONSECUTIVE_THRESHOLD or common.get("cooldown_sec") != COOLDOWN_SEC:
        bad.append("common")
    if bad:
        raise RuntimeError(f"fail-closed: 규칙 명세가 동결 상수와 다름: {bad}")
    return {"spec": spec, "sha256": hashlib.sha256(raw).hexdigest(), "path": str(path)}


def signal_identity(arm: str, art_hashes: dict, spec_sha256: str) -> dict:
    """guarded context·v2 payload 공통 identity. B는 모델 해시, C/K는 규칙 명세 해시로 결속한다."""
    if arm == "B":
        model_hash, manifest = art_hashes["model.pkl"], link.artifact_manifest_hash(art_hashes)
    else:
        model_hash, manifest = spec_sha256, link.artifact_manifest_hash({RULE_SPEC.name: spec_sha256})
    return {"detector_id": IDENTITY[arm][0], "model_version": IDENTITY[arm][1], "model_hash": model_hash,
            "artifact_manifest_hash": manifest, "threshold": THRESHOLDS[arm]}


def rule_value(arm: str, rec: dict) -> float:
    return rec["score"] if arm == "B" else rec["features"]["cpu_mean"]


def judge_all(rec: dict, states: dict, monotonic_now: float) -> dict:
    """같은 평가 레코드에서 세 규칙 판정. states[arm] = {"consecutive", "last_signal"} (규칙별 독립)."""
    out = {}
    for a in ARMS:
        st = states[a]
        if rec["status"] != "scored":
            st["consecutive"] = 0
            out[a] = {"value": None, "threshold": THRESHOLDS[a], "is_anomalous": None, "consecutive": 0,
                      "condition_met": False, "condition_met_at_utc": None}
            continue
        v = rule_value(a, rec)
        s = advance_streak(v if a == "B" else -v, THRESHOLDS[a] if a == "B" else -THRESHOLDS[a], st["consecutive"],
                           st["last_signal"], monotonic_now, cooldown_sec=COOLDOWN_SEC,
                           consecutive_threshold=CONSECUTIVE_THRESHOLD)
        st["consecutive"], st["last_signal"] = s["consecutive_anomalous"], s["last_signal_at"]
        out[a] = {"value": v, "threshold": THRESHOLDS[a], "is_anomalous": s["is_anomalous"],
                  "consecutive": s["consecutive_anomalous"], "condition_met": s["should_signal"],
                  "condition_met_at_utc": rec["score_completed_at_utc"] if s["should_signal"] else None}
    return out


def build_payload(arm: str, start: dict, rec: dict) -> dict:
    """동결 build_v2_payload의 검증(대상 Pod·UID·창·시각)을 그대로 거친 뒤 arm identity와 규칙 값으로 바꾼다."""
    j = rec["rules"][arm]
    base = link.build_v2_payload(start, {**rec, "condition_met": j["condition_met"],
                                         "condition_met_at_utc": j["condition_met_at_utc"],
                                         "consecutive_anomalous": j["consecutive"]})
    base.update(start["signal_identity"], score=j["value"])
    return base


def run_live(run_id: str, evidence_path: Path, *, arm: str, policy_base_url: str, expected_version: str,
             expected_mode: str, artifacts=DEFAULT_ARTIFACTS, spec_path: Path = RULE_SPEC, max_evals=None,
             now_fn=lambda: datetime.now(timezone.utc), monotonic_fn=time.monotonic, sleep_fn=time.sleep,
             snapshot_fn=k8s_snapshot, range_fn=prom_range, instant_fn=prom_instant, check_fn=link.check_receiver,
             send_fn=link.send) -> int:
    if arm not in ARMS:
        raise ValueError(f"arm은 {ARMS} 중 하나: {arm}")
    art = load_artifacts(Path(artifacts))
    spec = load_spec(spec_path)
    ident = signal_identity(arm, art["hashes"], spec["sha256"])
    rc = check_fn(policy_base_url, "guarded", expected_version=expected_version, expected_mode=expected_mode)
    states = {a: {"consecutive": 0, "last_signal": None} for a in ARMS}
    seq, post_action = 0, False
    url = policy_base_url.rstrip("/") + "/signal/v2"
    with open(evidence_path, "a", encoding="utf-8") as fh:
        start = {"record_type": "c1_live_start", "run_id": run_id, "detector": ident["detector_id"], "arm": arm,
                 "actuating_rule": arm, "signal_identity": ident, "rule_spec": spec["path"],
                 "rule_spec_sha256": spec["sha256"], "rules": spec["spec"]["rules"], "artifacts": str(artifacts),
                 "artifact_hashes": art["hashes"], "threshold": ident["threshold"], "if_threshold": art["threshold"],
                 "kept_features": art["schema"]["kept_feature_names"], "policy_url": url,
                 "expected_version": expected_version, "expected_mode": expected_mode, "receiver_check": rc,
                 "send_timeout": list(ld.SEND_TIMEOUT), "window_sec": WINDOW_SEC, "eval_interval_sec": EVAL_INTERVAL_SEC,
                 "consecutive_threshold": CONSECUTIVE_THRESHOLD, "cooldown_sec": COOLDOWN_SEC,
                 "started_at_utc": now_fn().isoformat()}
        _must_write(fh, start)
        if not rc.get("ok"):
            return ld.EXIT_RECEIVER
        while max_evals is None or seq < max_evals:
            seq += 1
            rec = evaluate_once(art, now_fn=now_fn, snapshot_fn=snapshot_fn, range_fn=range_fn, instant_fn=instant_fn)
            rules = judge_all(rec, states, monotonic_fn())
            j = rules[arm]
            rec.update(record_type="c1_evaluation", run_id=run_id, evaluation_seq=seq, post_action=post_action,
                       rules=rules, actuating_rule=arm, is_anomalous=j["is_anomalous"],
                       consecutive_anomalous=j["consecutive"], condition_met=j["condition_met"],
                       condition_met_at_utc=j["condition_met_at_utc"])
            _must_write(fh, rec)
            if rec["condition_met"] and not post_action:
                try:
                    payload = build_payload(arm, start, rec)
                except link.PayloadError as e:
                    _must_write(fh, {"record_type": "c1_policy_payload_error", "run_id": run_id, "evaluation_seq": seq,
                                     "error": str(e), "at_utc": now_fn().isoformat()})
                    return ld.EXIT_PAYLOAD
                _must_write(fh, {"record_type": "c1_policy_send_attempt", "run_id": run_id, "evaluation_seq": seq,
                                 "url": url, "attempted_at_utc": now_fn().isoformat()})
                sr = send_fn(url, payload, timeout=ld.SEND_TIMEOUT)
                decision = ld.classify_send_result(sr)
                _must_write(fh, {"record_type": "c1_policy_send_result", "run_id": run_id, "evaluation_seq": seq,
                                 "completed_at_utc": now_fn().isoformat(), "decision": decision, **sr})
                if decision == "stop_delivery":
                    return ld.EXIT_DELIVERY
                if decision == "stop_policy":
                    return ld.EXIT_POLICY
                post_action = post_action or decision == "post_action"
            flags = " ".join(f"{a}={rules[a]['consecutive']}{'!' if rules[a]['condition_met'] else ''}" for a in ARMS)
            print(f"[{rec.get('score_completed_at_utc') or rec.get('skipped_at_utc')}] seq={seq} {rec['status']} "
                  f"arm={arm} {flags} post_action={post_action}", flush=True)
            sleep_fn(EVAL_INTERVAL_SEC)
    return ld.EXIT_OK


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="B/C/K 공통 평가 tick 탐지기 - 실제 arm 규칙만 guarded 정책으로 송신")
    p.add_argument("--run-id", required=True)
    p.add_argument("--arm", required=True, choices=ARMS)
    p.add_argument("--evidence-log", required=True, type=Path)
    p.add_argument("--policy-url", required=True)
    p.add_argument("--expected-version", required=True)
    p.add_argument("--expected-mode", required=True, choices=["act"])
    p.add_argument("--artifacts-dir", default=str(DEFAULT_ARTIFACTS))
    a = p.parse_args(argv)
    try:
        return run_live(a.run_id, a.evidence_log, arm=a.arm, policy_base_url=a.policy_url,
                        expected_version=a.expected_version, expected_mode=a.expected_mode, artifacts=a.artifacts_dir)
    except EvidenceWriteFailed as e:
        print(f"필수 기록 실패 - 종료: {e}", file=sys.stderr, flush=True)
        return ld.EXIT_EVIDENCE


if __name__ == "__main__":
    sys.exit(main())
