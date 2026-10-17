"""호스트 중단(PC 절전) 뒤 재개 규칙 (사전등록 rev2 §9.1, 2026-10-18).

동결된 Phase 2 resume_check는 "탐지기 시작 전 하네스 오류"만 재개할 수 있다. n2n01은 61분 관찰 중 PC가 절전에 들어가
통로(port-forward)가 끊겨 중단됐다(탐지기는 이미 돌고 있었고 점수·신호는 보지 않았다). 이 모듈은 그 사고를 위한 **별도 조건**이다:
탐지기 결과를 보지 않고 확인 가능한 사실(전원 이벤트, 정리 오류 종류, 송신 0, 무결성, 지금의 서버 상태)만 쓴다.

재개해도 그 시도는 무효·시도 1회로 남고(ID 재사용 금지), running 표시·원자료는 지우지 않는다. 기록은 시도 기록·요약·유효성·표시 파일의
해시와 현재 구현 동결에 묶여, 하나라도 바뀌면 효력을 잃는다(원본 resume_authorized를 그대로 쓴다).
원본 모듈은 바꾸지 않는다: n2_stage1.load_stage1이 로드한 인스턴스의 resume_check만 이 구현으로 교체한다."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

HOST_SLEEP_EVENT_IDS = (42, 506)          # Kernel-Power: 42 = 시스템이 절전에 들어감, 506 = 연결 대기(Modern Standby) 진입
# 절전으로 정책 서버·Prometheus 통로가 끊기면 생기는 정리 오류만. 잔여 자원·복원 실패·유예 변경 등은 재개 불가.
RESUMABLE_PREFIXES = ("policy_state_after_failed", "preview_cleanup_held", "canonical_end_not_reached")
CLASSIFICATION = "host_sleep_interruption_after_detector_start"


def _event_utc(e) -> datetime:
    ms = int(re.search(r"\((-?\d+)\)", str(e.get("TimeCreated"))).group(1))
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


def _parse(t):
    return datetime.fromisoformat(t) if t else None


def sleep_events_in_window(rec: dict, summary: dict) -> list:
    """관찰 구간 [시작, 끝] 안에 든 절전 진입 이벤트 id 목록(구간 정보가 없으면 실행기 시작~종료)."""
    lo = _parse(summary.get("observation_started_at_utc")) or _parse(rec.get("runner_started_at_utc"))
    hi = _parse(summary.get("observation_ended_at_utc")) or _parse(rec.get("runner_ended_at_utc"))
    out = []
    for e in rec.get("power_events") or []:
        try:
            if e.get("Id") in HOST_SLEEP_EVENT_IDS and lo and hi and lo <= _event_utc(e) <= hi:
                out.append(e["Id"])
        except (AttributeError, TypeError, ValueError):
            continue
    return out


def make_resume_check(m, live_fn):
    """m = n2_stage1.load_stage1이 돌려준 run_stage1 인스턴스. live_fn() -> {"policy": {mode, context, ...}, "problems": [...]}
    (지금의 서버 상태, 읽기 전용). live_fn이 없으면 재개 조건을 확인할 수 없으므로 거부(fail-closed)."""
    def resume_check(d, run) -> dict:
        d = Path(d)
        rec = m._read(m._session_path(d, run))
        rid = rec["run_id"]
        s = m._read(d / f"summary-{rid}.json")
        v = m._read(d / f"p2-validity-{run}.json")
        marker = d / f"running-{rid}.json"
        ev = m._lines(d / rec["evidence_file"])
        iso = s.get("old_policy_isolation") or {}
        to = s.get("trial_outcome") or {}
        live = live_fn() if live_fn else None
        marker_only = f"running_marker_left:{[marker.name]}"
        live_problems = [p for p in (live or {}).get("problems", []) if p != marker_only]       # 이 시도의 running 표시만 남은 것은 허용
        policy = (live or {}).get("policy") or {}
        sleeps = sleep_events_in_window(rec, s)
        facts = {"classification": CLASSIFICATION, "host_sleep_event_ids": sleeps, "detector_started": s.get("detector_started"),
                 "cleanup_errors": list(rec.get("cleanup_errors") or []), "validity_status": v.get("status"),
                 "send_attempts": sum(r.get("record_type") == "c1_policy_send_attempt" for r in ev)
                 + int((s.get("evidence_counts") or {}).get("send_attempts") or 0),
                 "prober_stop_failed": to.get("prober_stop_failed"), "injector_cleanup_failed": to.get("injector_cleanup_failed"),
                 "postflight_invalid_reasons": list(rec.get("postflight_invalid_reasons") or []), "integrity": rec.get("integrity"),
                 "old_policy_isolated": {"start": iso.get("start"), "end": iso.get("end")},
                 "live_policy_mode": policy.get("mode"), "live_policy_context": policy.get("context"), "live_problems": live_problems}
        probs = []
        if rec.get("status") != "run" or v.get("valid") is not False:
            probs.append("not_a_terminal_invalid_attempt")
        if not sleeps:
            probs.append("no_host_sleep_event_in_observation_window")
        errs = facts["cleanup_errors"]
        if not errs or any(not str(e).startswith(RESUMABLE_PREFIXES) for e in errs):
            probs.append(f"cleanup_errors_not_interruption_class: {errs}")
        if facts["prober_stop_failed"] or facts["injector_cleanup_failed"]:
            probs.append("prober_or_injector_cleanup_failed")
        if facts["postflight_invalid_reasons"]:
            probs.append(f"postflight_invalid: {facts['postflight_invalid_reasons']}")
        if facts["send_attempts"]:
            probs.append("signal_send_attempt_present")
        if not (rec.get("integrity") or {}).get("ok") or (d / f"p2-integrity-failure-{run}.json").exists():
            probs.append("integrity_failure")
        if any(facts["old_policy_isolated"].get(k) is not True for k in ("start", "end")):
            probs.append(f"old_policy_isolation_not_verified: {facts['old_policy_isolated']}")
        if live is None:
            probs.append("live_state_not_provided")
        else:
            if facts["live_policy_mode"] != m.MODE or facts["live_policy_context"] is not None:
                probs.append("live_policy_not_clean_record_only")
            if live_problems:
                probs.append(f"live_state_not_canonical: {live_problems}")
        return {"run": run, "run_id": rid, "facts": facts, "problems": probs,
                "session_record_sha256": m.pc.sha256_bytes(m._session_path(d, run)),
                "summary_sha256": m.pc.sha256_bytes(d / f"summary-{rid}.json"),
                "validity_sha256": m.pc.sha256_bytes(d / f"p2-validity-{run}.json"),
                "running_marker": marker.name if marker.exists() else None,
                "running_marker_sha256": m.pc.sha256_bytes(marker) if marker.exists() else None}
    return resume_check


def profiles_equal_except_prereg(old_path, new_path) -> bool:
    """rev2 selection profile이 이전 profile과 사전등록 해시 필드만 다른지(환경·후보·임계값은 같은지) 확인한다."""
    def strip(o):
        if isinstance(o, dict):
            return {k: strip(v) for k, v in o.items() if "prereg" not in k}
        return [strip(x) for x in o] if isinstance(o, list) else o
    load = lambda p: json.loads(Path(p).read_text(encoding="utf-8"))          # noqa: E731
    return strip(load(old_path)) == strip(load(new_path))


def make_authorize(m, live_fn, *, previous_profile: Path = None):
    """원본 authorize_cleanup_resume과 같은 절차이되 (1) 위의 재개 조건을 쓰고 (2) 실패한 시도의 profile 해시가 현재와 달라도
    사전등록 해시 필드만 다르면(rev2 교체) 허용한다."""
    check = make_resume_check(m, live_fn)

    def authorize(d, run, *, authorized_by: str, review_note: str, impl: dict, artifact_root):
        c = check(d, run)
        if c["problems"]:
            raise SystemExit(f"재개 기록 거부: {c['problems']}")
        if m.pc.text_sha256(m.pc.SELECTION_PROFILE) != impl.get("selection_profile_sha256"):
            raise SystemExit("재개 기록 거부: selection profile이 implementation freeze와 다름")
        m.pc.load_profile(m.pc.SELECTION_PROFILE, stage="stage1")
        art = m.pc.verify_artifacts(artifact_root)
        rec = m._read(m._session_path(d, run))
        old_sha = rec.get("environment_profile_sha256")
        if old_sha != impl.get("selection_profile_sha256"):
            if previous_profile is None or m.pc.text_sha256(previous_profile) != old_sha \
                    or not profiles_equal_except_prereg(previous_profile, m.pc.SELECTION_PROFILE):
                raise SystemExit("재개 기록 거부: 실패한 시도의 profile이 현재 동결값과 다르고 사전등록 해시만 다른 교체본도 아님")
        if rec.get("artifact_hashes") != art["hashes"]:
            raise SystemExit("재개 기록 거부: 실패한 시도의 artifact가 현재 동결값과 다름")
        return m.pc.write_json_new(m._auth_path(d, run), {
            "record_type": "p2_resume_authorization", "run": run, "run_id": c["run_id"], "attempt_number": rec["attempt_number"],
            "failure_classification": CLASSIFICATION, "failure_recorded_at_utc": rec.get("runner_ended_at_utc"), **c,
            "attempt_remains_invalid": True, "attempt_consumed": True,
            "detector_results_observed": False, "detector_ran_before_interruption": bool(c["facts"]["detector_started"]),
            "attempt_profile_sha256": old_sha, "current_profile_sha256": impl.get("selection_profile_sha256"),
            "user_authorized_resume": True, "authorized_by": authorized_by, "review_note": review_note,
            "authorized_at_utc": datetime.now(timezone.utc).isoformat(),
            "implementation_freeze": {"manifest": impl.get("manifest"), "manifest_sha256": impl.get("manifest_sha256")}})
    return authorize
