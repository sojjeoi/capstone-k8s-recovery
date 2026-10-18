"""호스트 중단(PC 절전) 뒤 재개 규칙 (사전등록 rev2 §9.1, 2026-10-18).

동결된 Phase 2 resume_check는 "탐지기 시작 전 하네스 오류"만 재개할 수 있다. n2n01은 61분 관찰 중 PC가 절전에 들어가
통로(port-forward)가 끊겨 중단됐다(탐지기는 이미 돌고 있었고 점수·신호는 보지 않았다). 이 모듈은 그 사고를 위한 **별도 조건**이다:
탐지기 결과를 보지 않고 확인 가능한 사실(전원 이벤트, 정리 오류 종류, 송신 0, 무결성, 지금의 서버 상태)만 쓴다.

재개해도 그 시도는 무효·시도 1회로 남고(ID 재사용 금지), running 표시·원자료는 지우지 않는다. 기록은 시도 기록·요약·유효성·표시 파일의
해시와 현재 구현 동결에 묶여, 하나라도 바뀌면 효력을 잃는다(원본 resume_authorized를 그대로 쓴다).
원본 모듈은 바꾸지 않는다: n2_stage1.load_stage1이 로드한 인스턴스의 resume_check만 이 구현으로 교체한다."""
import ast
import json
import re
from datetime import datetime, timezone
from pathlib import Path

HOST_SLEEP_EVENT_IDS = (42, 506)          # Kernel-Power: 42 = 시스템이 절전에 들어감, 506 = 연결 대기(Modern Standby) 진입
# 절전으로 정책 서버·Prometheus 통로가 끊기면 생기는 정리 오류만. 잔여 자원·복원 실패·유예 변경 등은 재개 불가.
RESUMABLE_PREFIXES = ("policy_state_after_failed", "preview_cleanup_held", "canonical_end_not_reached")
CLASSIFICATION = "host_sleep_interruption_after_detector_start"
# rev4 §9.4: 실제 압박 주입 이전의 구현 결함으로 중단된 압박 시도. 정리 오류는 이 종류뿐이어야 하고 harness_error가 하나 이상 있어야 한다
# (주입 전 NetworkChaos 생성이 거부된 n2p01 형태). 잔여 자원·복원 실패·유예 변경 등은 불가.
CLASSIFICATION_DEFECT = "pre_injection_implementation_defect"
DEFECT_PREFIXES = ("trial_cleanup_incomplete", "harness_error", "network_session_end: running_marker_left")


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


MARKER_PROBLEM = "running_marker_left:"


def _without_recorded_markers(problems, recorded) -> list:
    """지금 서버 상태 확인(live)의 문제 목록에서, 시도 기록이 있는 시도의 'running 표시'만 뺀다. 그런 표시는 증거 파일로 남아 있는 것이 정상이고(재개 승인이
    없으면 다음 세션 시작 관문이 따로 막는다), 두 시도가 동시에 재개를 기다릴 때 서로의 표시 때문에 둘 다 거부되는 순환을 막는다.
    시도 기록이 없는 표시(알 수 없는 실행이 남긴 것)는 그대로 문제로 남긴다."""
    out = []
    for p in problems:
        if p.startswith(MARKER_PROBLEM):
            try:
                left = [x for x in ast.literal_eval(p[len(MARKER_PROBLEM):]) if x not in recorded]
            except (ValueError, SyntaxError):
                out.append(p)
                continue
            if left:
                out.append(f"{MARKER_PROBLEM}{left}")
        else:
            out.append(p)
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
        recorded = {f"running-{a['run_id']}.json" for a in m.attempts(d)}                       # 시도 기록(p2-session)이 있는 시도의 running 표시
        live_problems = _without_recorded_markers((live or {}).get("problems", []), recorded)
        policy = (live or {}).get("policy") or {}
        sleeps = sleep_events_in_window(rec, s)
        facts = {"classification": CLASSIFICATION, "host_sleep_event_ids": sleeps, "detector_started": s.get("detector_started"),
                 "cleanup_errors": list(rec.get("cleanup_errors") or []), "validity_status": v.get("status"),
                 "send_attempts": sum(r.get("record_type") == "c1_policy_send_attempt" for r in ev)
                 + int((s.get("evidence_counts") or {}).get("send_attempts") or 0),
                 "prober_stop_failed": to.get("prober_stop_failed"), "injector_cleanup_failed": to.get("injector_cleanup_failed"),
                 "postflight_invalid_reasons": list(rec.get("postflight_invalid_reasons") or []), "integrity": rec.get("integrity"),
                 "old_policy_isolated": {k: (iso.get(k) or {}).get("isolated") for k in ("start", "end")},   # 실제 기록: {"isolated": bool, "reasons": [...], ...}
                 "live_policy_mode": policy.get("mode"), "live_policy_context": policy.get("context"), "live_problems": live_problems}
        # ---- 두 종류가 모두 요구하는 공통 조건 ----
        probs = []
        if rec.get("status") != "run" or v.get("valid") is not False:
            probs.append("not_a_terminal_invalid_attempt")
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
        errs = facts["cleanup_errors"]
        # ---- 종류 1(§9.1): 호스트 절전 ----
        host = []
        if not sleeps:
            host.append("no_host_sleep_event_in_observation_window")
        if not errs or any(not str(e).startswith(RESUMABLE_PREFIXES) for e in errs):
            host.append(f"cleanup_errors_not_interruption_class: {errs}")
        # ---- 종류 2(§9.4, rev4): 실제 압박 주입 이전의 구현 결함 - 압박 시도에만 ----
        nd = s.get("network_delivery")
        try:
            cur_freeze = m.pc.verify_implementation_freeze().get("manifest_sha256")
        except SystemExit:
            cur_freeze = None
        attempt_freeze = ((rec.get("code_provenance") or s.get("code_provenance") or {}).get("manifest_sha256"))
        facts.update({"kind": rec.get("kind"), "network_delivery": nd, "t_injection_observed_utc": to.get("t_injection_observed_utc"),
                      "canonical_end_ok": (s.get("canonical_end") or {}).get("ok"), "attempt_implementation_freeze_sha256": attempt_freeze,
                      "current_implementation_freeze_sha256": cur_freeze})
        defect = []
        if rec.get("kind") != "pressure":
            defect.append("defect_class_applies_to_pressure_attempts_only")
        if not isinstance(nd, dict):
            defect.append("network_delivery_record_missing(주입 0회를 확인할 수 없음)")
        elif nd.get("crs") or nd.get("first_created_at") or nd.get("last_deleted_at") or to.get("t_injection_observed_utc") or s.get("stress_delivery_record"):
            defect.append("injection_may_have_happened(NetworkChaos 0회가 아님)")
        if not errs or any(not str(e).startswith(DEFECT_PREFIXES) for e in errs) or not any(str(e).startswith("harness_error") for e in errs):
            defect.append(f"cleanup_errors_not_pre_injection_defect_class: {errs}")
        if facts["canonical_end_ok"] is not True:
            defect.append("canonical_end_not_ok_in_the_attempt_record")
        if not cur_freeze or not attempt_freeze or cur_freeze == attempt_freeze:
            defect.append("no_new_implementation_freeze_since_the_attempt(수정 뒤 새 구현 동결이 없음)")
        if not host:
            facts["classification"] = CLASSIFICATION
        elif not defect:
            facts["classification"] = CLASSIFICATION_DEFECT
        else:
            facts["classification"] = "none"
            probs += host + [f"pre_injection_defect_class: {x}" for x in defect]
        return {"run": run, "run_id": rid, "facts": facts, "problems": probs,
                "session_record_sha256": m.pc.sha256_bytes(m._session_path(d, run)),
                "summary_sha256": m.pc.sha256_bytes(d / f"summary-{rid}.json"),
                "validity_sha256": m.pc.sha256_bytes(d / f"p2-validity-{run}.json"),
                "running_marker": marker.name if marker.exists() else None,
                "running_marker_sha256": m.pc.sha256_bytes(marker) if marker.exists() else None}
    return resume_check


def profiles_equal_except_prereg(old_path, new_path) -> bool:
    """rev2 selection profile이 이전 profile과 사전등록 해시 필드만 다른지(환경·후보·임계값은 같은지) 확인한다."""
    def strip(o, parent=None):                    # 사전등록 해시가 들어가는 자리: 이름에 prereg가 든 필드와 selection.rule(= 문서 해시)
        if isinstance(o, dict):
            return {k: strip(v, k) for k, v in o.items() if "prereg" not in k and not (parent == "selection" and k == "rule")}
        return [strip(x, parent) for x in o] if isinstance(o, list) else o
    load = lambda p: json.loads(Path(p).read_text(encoding="utf-8"))          # noqa: E731
    return strip(load(old_path)) == strip(load(new_path))


def make_authorize(m, live_fn, *, previous_profiles=()):
    """원본 authorize_cleanup_resume과 같은 절차이되 (1) 위의 재개 조건을 쓰고 (2) 실패한 시도의 profile 해시가 현재와 달라도
    이전 revision profile들 중 하나이고 사전등록 해시 필드만 다르면 허용한다."""
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
            lineage = {m.pc.text_sha256(p) for p in previous_profiles if Path(p).is_file() and profiles_equal_except_prereg(p, m.pc.SELECTION_PROFILE)}
            if old_sha not in lineage:
                raise SystemExit("재개 기록 거부: 실패한 시도의 profile이 현재 동결값과 다르고 사전등록 해시만 다른 이전 revision profile도 아님")
        if rec.get("artifact_hashes") != art["hashes"]:
            raise SystemExit("재개 기록 거부: 실패한 시도의 artifact가 현재 동결값과 다름")
        return m.pc.write_json_new(m._auth_path(d, run), {
            "record_type": "p2_resume_authorization", "run": run, "run_id": c["run_id"], "attempt_number": rec["attempt_number"],
            "failure_classification": c["facts"]["classification"], "failure_recorded_at_utc": rec.get("runner_ended_at_utc"), **c,
            "attempt_remains_invalid": True, "attempt_consumed": True,
            "detector_results_observed": False, "detector_ran_before_interruption": bool(c["facts"]["detector_started"]),
            "no_injection_confirmed": c["facts"]["classification"] == CLASSIFICATION_DEFECT or None,
            "attempt_profile_sha256": old_sha, "current_profile_sha256": impl.get("selection_profile_sha256"),
            "user_authorized_resume": True, "authorized_by": authorized_by, "review_note": review_note,
            "authorized_at_utc": datetime.now(timezone.utc).isoformat(),
            "implementation_freeze": {"manifest": impl.get("manifest"), "manifest_sha256": impl.get("manifest_sha256")}})
    return authorize
