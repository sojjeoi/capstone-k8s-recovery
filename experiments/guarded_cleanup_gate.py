"""guarded 실행 경로 전용 - 승격 뒤 정리 안전장치(2026-10-06 D5·D6). 동결 코드(arm_controller·blue_green_prep·
scale_down_delay_override·run_timed_switch_recovery_trial)는 수정하지 않고 주입 지점만 쓴다.

D5 미승격 preview 자동 정리 관문:
  기존 정리(arm_controller.wrap_injector_with_preview_prep의 cleanup_preview_fn = blue_green_prep.cleanup_unpromoted_preview)는
  정책 결과를 보지 않고 "Rollout activeSelector가 준비 전 값 그대로"면 preview를 abort한다. 승격 결과 불명·부분 전환에서도
  abort될 수 있어 "미검증 롤백 없음"과 충돌한다. 그래서 관문은 아래 근거를 **모두** 읽고, 승격하지 않았음과 정리 대상의
  동일성이 확인될 때만 기존 정리를 부른다. 하나라도 읽지 못하거나 승격 시도 근거가 있으면 정리하지 않고 보류(→ 다음 실행 차단).
    1) 정책 상태(/admin/state): 이 run의 조치(예약·승격 호출·결과), 차단, 미해결
    2) 원장(act만, 공유 ledger.jsonl): 이 run의 intent와 그 뒤 aborted/result/service_result
    3) 탐지기 기록: 송신 시도·결과(결과 없는 시도·전달 불명·승격 결과)
    4) 실제 Rollout·Service: UID, activeSelector, vllm-active selector, currentPodHash
  압박 자원 제거·probe 정리는 이 관문과 독립이다(기존 wrapper가 원래 cleanup을 먼저 실행하고, 관문은 None을 돌려 예외 없이 끝낸다).

D6 확인된 정상 전환의 유예 원복:
  승격과 서비스 정상이 모두 확인된 경우에만, 실제 적용된 유예(record.applied_value)와 승격 시각으로 구 RS가 0이 될 때까지
  기다린 뒤 기존 restore_override로 30을 되돌린다. 시간이 지났다는 이유만으로 원복하지 않는다(구 RS desired·current 0을 실측).
  대기 상한 = max(승격 호출 시각 + 적용 유예 + SCALE_DOWN_MARGIN_SEC, 원복 대기 시작 + MIN_CHECK_SEC)(2026-10-07 gap02 뒤).
  대기 중에는 매 표본마다 안전 감시와 대상 동일성을 확인한다.
  결과 불명·서비스 실패·대상 변경·상태 조회 실패·상한 초과면 유예를 줄이지 않고 근거와 함께 보류(→ 다음 실행 차단).
"""
import json
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

PROMOTION_OUTCOMES = {"promoted_verified", "promoted_unverified", "promotion_unknown"}
DEFINITIVE_NOT_DELIVERED = {"not_delivered"}  # 연결 전 거부 - 정책이 처리하지 않았음이 확실한 경우만
LEDGER_EVENTS = {"ledger_created", "intent", "aborted", "result", "service_result", "resolution"}
NOT_ATTEMPTED_LEDGER = (["intent", "aborted:aborted_final_check"], ["intent", "aborted:aborted_adapter_precheck"])
TARGET_RE = re.compile(r"@(?P<uid>[^:]+):(?P<old>[^-]+)->(?P<new>[^#]+)#")
# 대기 여유 근거: pilot04 B(2026-10-02) 실측 구 Pod 종료 = 전환+420초(정확히 유예값), 30초 유예 실측 29.9~30초.
# 여유 180초 = 승격 호출→실제 전환(CLI 기한 30초 + 검증 기한 10초) + vLLM Pod 종료 유예(rollout.yaml에 없음 → 기본 30초)
#            + 컨트롤러·API 반영과 표본 간격(5초) 여유 약 110초.
SCALE_DOWN_MARGIN_SEC = 180.0
POLL_SEC = 5.0
READ_FAIL_LIMIT = 3
RESTORE_RECHECK_SEC = 30.0  # 구 RS 0 확인 뒤 restore_override 자체 재확인 대기(이미 0이면 즉시 통과)
# 최소 확인 시간(2026-10-07 gap02 뒤): 상한 직전에 시작한 정리도 '안전 감시 → 정책 → 상태 조회 → 원복 직전 재확인' 한 바퀴를
# 끝낼 수 있게 한다(gap02: 상한 4초 전에 시작, 첫 조회 4.4초 뒤 구 RS 0을 보고도 보류). 한 바퀴에서 기한이 있는 호출 상한의 합(직렬):
#   기존 정책 격리 조회 4×5(old_policy_isolation API_TIMEOUT_SEC) + 노드 메모리 Prometheus 10+10(연결·읽기)
#   + 디스크 SSH 2노드×40(DISK_RECHECK_SEC 60초가 지났을 때) + 정책 상태 3+10 + Rollout·Service 2×5 + RS 5
#   + 원복 직전 재확인 Rollout·Service 2×5 + 정책 3+10 = 171초.
# 기한이 없는 기존(동결) 호출 - 노드 상태 read_node 2회, restore_override 안 get_rollout·list_rs_replicas - 은 합에 못 넣는다.
# 이들이 늦게 끝나도 patch 직전 상한 재검사(check_promoted)가 원복을 막는다.
MIN_CHECK_SEC = 4 * 5 + (10 + 10) + 2 * 40 + (3 + 10) + 2 * 5 + 5 + 2 * 5 + (3 + 10)


def _utc():
    return datetime.now(timezone.utc)


def _ts(x) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(x).replace("Z", "+00:00")) if x else None
    except ValueError:
        return None


def parse_target(tkey: str) -> Optional[dict]:
    m = TARGET_RE.search(tkey or "")
    return m.groupdict() if m else None


def _detector_evidence(path: Path) -> dict:
    out = {"attempts": [], "results": [], "uncertain": [], "promotion_results": []}
    for ln in Path(path).read_text(encoding="utf-8").split("\n"):
        if not ln.strip():
            continue
        r = json.loads(ln)  # 손상은 호출부에서 보류로 처리
        t = r.get("record_type")
        if t == "c1_policy_send_attempt":
            out["attempts"].append(r.get("evaluation_seq"))
        elif t == "c1_policy_send_result":
            out["results"].append(r.get("evaluation_seq"))
            d, o = r.get("delivery"), r.get("policy_outcome")
            if d != "delivered" and d not in DEFINITIVE_NOT_DELIVERED:
                out["uncertain"].append(f"seq{r.get('evaluation_seq')}:delivery={d}")
            if o in PROMOTION_OUTCOMES or r.get("decision") == "post_action":
                out["promotion_results"].append({"seq": r.get("evaluation_seq"), "outcome": o,
                                                 "target_key": r.get("target_key")})
    missing = sorted(set(s for s in out["attempts"] if s not in out["results"]), key=str)
    out["uncertain"] += [f"seq{s}:attempt_without_result" for s in missing]
    return out


def _ledger_segments(recs: list, run_id: str) -> dict:
    """{target_key: [구간, ...]} - 이 run의 intent마다 새 구간(이전 구간을 지우지 않는다)."""
    if not recs or not isinstance(recs[0], dict) or recs[0].get("event") != "ledger_created":
        raise ValueError("원장 첫 기록이 ledger_created가 아님")
    seg, cur = {}, {}
    for r in recs:
        ev = r.get("event") if isinstance(r, dict) else None
        if ev not in LEDGER_EVENTS:
            raise ValueError(f"원장 사건 형식 이상: {str(r)[:120]}")
        tk = r.get("target_key")
        if ev == "intent":
            if r.get("run_id") == run_id:
                cur[tk] = ["intent"]
                seg.setdefault(tk, []).append(cur[tk])
            else:
                cur.pop(tk, None)  # 다른 run의 intent - 이후 사건은 그 run 몫
        elif tk in cur:
            cur[tk].append(f"{ev}:{r.get('status')}")
    return seg


def assess(*, run_id: str, mode: str, policy_state_fn: Callable[[], dict], ledger_fn: Optional[Callable[[], list]],
           evidence_path: Path, view_fn: Callable[[], dict], rollout_uid: Optional[str], old_active: Optional[str],
           prep_info: Optional[dict] = None, detector_expected: bool = True) -> dict:
    """반환 {"verdict": not_promoted|promoted_service_ok|hold, "reasons", "attempt_evidence", "sources", "target"}.
    예외를 던지지 않는다. 근거를 읽지 못하면 hold."""
    holds, attempts, src, target = [], [], {"assessed_at_utc": _utc().isoformat()}, None
    acts, seg, det, v = {}, None, None, None
    try:
        st = policy_state_fn()
        if not isinstance(st, dict) or st.get("state_available") is not True:
            raise ValueError(f"state_unavailable: {str(st)[:200]}")
        acts = {k: a for k, a in (st.get("actions") or {}).items() if a.get("run_id") == run_id}
        src["policy"] = {"blocked": st.get("blocked"), "unresolved": sorted(st.get("unresolved") or {}),
                         "run_actions": {k: {x: a.get(x) for x in ("status", "promote_called_at", "reserved_at")}
                                         for k, a in acts.items()}}
        if st.get("blocked"):
            holds.append(f"policy_blocked: {st['blocked']}")
        if st.get("unresolved"):
            holds.append(f"policy_unresolved: {sorted(st['unresolved'])}")
        for a in acts.values():
            s = str(a.get("status", ""))
            if a.get("promote_called_at") or not s.startswith("aborted"):
                attempts.append(f"policy_action:{s}")
    except Exception as e:  # noqa: BLE001
        holds.append(f"policy_state_unreadable: {type(e).__name__}: {e}")
    if mode == "act":
        try:
            seg = _ledger_segments(ledger_fn(), run_id)
            src["ledger"] = seg
            attempts += [f"ledger:{'>'.join(x)}" for segs in seg.values() for x in segs if x not in NOT_ATTEMPTED_LEDGER]
        except Exception as e:  # noqa: BLE001
            holds.append(f"ledger_unreadable: {type(e).__name__}: {e}")
    else:
        src["ledger"] = "not_applicable_record_only"
    try:
        if Path(evidence_path).exists():
            det = _detector_evidence(evidence_path)
        elif detector_expected:  # 탐지기가 시작됐는데 기록이 없으면 "시도 없음"이 아니라 근거 없음
            raise FileNotFoundError(f"탐지기 기록 없음: {evidence_path}")
        else:
            det = {"attempts": [], "results": [], "uncertain": [], "promotion_results": [], "note": "detector_never_started"}
        src["detector"] = det
        attempts += [f"detector:{r['outcome']}" for r in det["promotion_results"]]
    except Exception as e:  # noqa: BLE001
        holds.append(f"detector_evidence_unreadable: {type(e).__name__}: {e}")
    try:
        v = view_fn()
        src["live"] = v
        if rollout_uid and v.get("uid") != rollout_uid:
            holds.append(f"rollout_uid_changed: {v.get('uid')} != {rollout_uid}")
        if old_active is None:
            holds.append("pre_switch_active_unknown")
        elif v.get("active_selector") != old_active or v.get("service_selector") != old_active:
            attempts.append(f"live_selector_changed: rollout={v.get('active_selector')} "
                            f"service={v.get('service_selector')} (was {old_active})")
    except Exception as e:  # noqa: BLE001
        holds.append(f"rollout_view_unreadable: {type(e).__name__}: {e}")
    out = {"reasons": holds, "attempt_evidence": attempts, "sources": src, "target": None}
    if holds:
        return {**out, "verdict": "hold"}
    if not attempts:
        if det["uncertain"]:  # 정책이 처리했는지 모를 송신이 있으면 미승격이라고 단정하지 않는다
            return {**out, "verdict": "hold", "reasons": [f"send_outcome_uncertain: {det['uncertain']}"]}
        if prep_info and prep_info.get("ready") and v.get("current_pod_hash") != prep_info.get("created_pod_hash"):
            return {**out, "verdict": "hold", "reasons": [
                f"cleanup_target_identity_unconfirmed: currentPodHash={v.get('current_pod_hash')} "
                f"!= prepared={prep_info.get('created_pod_hash')}"]}
        return {**out, "verdict": "not_promoted"}
    # 승격 시도 근거가 있다 - 정상 확인이 모든 근거에서 일치할 때만 promoted_service_ok
    done = {k: a for k, a in acts.items() if a.get("promote_called_at") or not str(a.get("status", "")).startswith("aborted")}
    why = []
    if len(done) != 1:
        why.append(f"run_actions={len(done)}")
    else:
        tk, a = next(iter(done.items()))
        target = {"target_key": tk, **(parse_target(tk) or {}), "promote_called_at": a.get("promote_called_at")}
        if a.get("status") != "verified":
            why.append(f"policy_status={a.get('status')}")
        tried = [(k, x) for k, segs in (seg or {}).items() for x in segs if x not in NOT_ATTEMPTED_LEDGER]
        if tried != [(tk, ["intent", "result:verified_service_pending", "service_result:verified"])]:
            why.append(f"ledger_attempts={tried}")  # 시도 구간이 정확히 이 대상의 정상 확인 하나여야 함
        if any(r.get("target_key") not in (None, tk) or r["outcome"] in ("promoted_unverified", "promotion_unknown")
               for r in det["promotion_results"]):  # 같은 대상의 중복 응답(duplicate_delivery)은 허용
            why.append(f"detector_promotion_results={det['promotion_results']}")
        if not target.get("new") or v.get("active_selector") != target["new"] or v.get("service_selector") != target["new"]:
            why.append(f"live_not_switched_to_target: rollout={v.get('active_selector')} service={v.get('service_selector')}")
        if not _ts(a.get("promote_called_at")):
            why.append("promote_called_at_missing")
    if why:
        return {**out, "verdict": "hold", "target": target, "reasons": [f"promotion_attempted_not_confirmed_ok: {why}"]}
    return {**out, "verdict": "promoted_service_ok", "target": target}


class PreviewCleanupGate:
    """arm_controller.wrap_injector_with_preview_prep(cleanup_preview_fn=gate.cleanup)로 넘긴다."""

    def __init__(self, assess_fn: Callable[[Optional[dict]], dict], real_cleanup_fn: Callable,
                 restore_fn: Optional[Callable[[dict], dict]] = None):
        # restore_fn(A/B 비교 전용, 계약 r2): 미승격 abort가 끝난 뒤 canonical Healthy 복원. None이면 기존 동작(pilot)
        self.assess_fn, self.real_cleanup_fn, self.restore_fn = assess_fn, real_cleanup_fn, restore_fn
        self.prep_info, self.log = None, []

    def cleanup(self, prep_info, name, namespace):
        self.prep_info = prep_info
        if not prep_info or not prep_info.get("ready"):  # 준비된 preview가 없으면 기존 함수도 아무것도 하지 않는다
            self.log.append({"action": "nothing_prepared"})
            return self.real_cleanup_fn(prep_info, name, namespace)
        a = self.assess_fn(prep_info)
        rec = {"action": None, "verdict": a["verdict"], "reasons": a["reasons"], "attempt_evidence": a["attempt_evidence"],
               "assessed_at_utc": a["sources"].get("assessed_at_utc")}
        if a["verdict"] == "not_promoted":
            rec["action"] = "cleanup_allowed"
            self.log.append(rec)
            r = self.real_cleanup_fn(prep_info, name, namespace)
            rec["cleanup_result"] = r
            if r is True and self.restore_fn is not None:  # abort가 실제로 끝난 경우에만(실패·미실행이면 복원하지 않음)
                rec["canonical_restore"] = self.restore_fn(prep_info)
            return r
        if a["verdict"] == "promoted_service_ok":
            rec["action"] = "skipped_preview_is_new_active"
        else:
            rec["action"] = "held"
            rec["remaining"] = {"prepared_pod_hash": prep_info.get("created_pod_hash"),
                                "pre_prepare_active_selector": prep_info.get("pre_prepare_active_selector"),
                                "live": a["sources"].get("live")}
        self.log.append(rec)
        return None  # 예외 없이 끝내 다른 정리(probe 등)를 막지 않는다 - 보류는 실행기가 정리 오류로 남긴다

    @property
    def held(self) -> Optional[dict]:
        return next((r for r in self.log if r.get("action") == "held"), None)

    @property
    def restore_failed(self) -> Optional[dict]:
        return next((r["canonical_restore"] for r in self.log
                     if "canonical_restore" in r and r["canonical_restore"].get("ok") is not True), None)


def check_policy_ok(policy_state_fn: Callable[[], dict], target_key: Optional[str] = None) -> None:
    """원복 대기·원복 직전 정책 확인. 상태 없음·차단·미해결·대상 조치 상태 변화면 예외."""
    st = policy_state_fn()
    if not isinstance(st, dict) or st.get("state_available") is not True:
        raise RuntimeError(f"policy_state_unavailable: {str(st)[:200]}")
    if st.get("blocked") or st.get("unresolved"):
        raise RuntimeError(f"policy_blocked_or_unresolved: blocked={st.get('blocked')} "
                           f"unresolved={sorted(st.get('unresolved') or {})}")
    if target_key is not None and ((st.get("actions") or {}).get(target_key) or {}).get("status") != "verified":
        raise RuntimeError(f"policy_action_not_verified: {((st.get('actions') or {}).get(target_key) or {}).get('status')}")


def _view_matches(v: dict, record: dict, active: str, current: Optional[str] = None) -> list:
    bad = []
    if v.get("uid") != record["rollout_uid"]:
        bad.append(f"uid={v.get('uid')}")
    if v.get("delay") != record["applied_value"]:
        bad.append(f"delay={v.get('delay')}")
    if v.get("active_selector") != active or v.get("service_selector") != active:
        bad.append(f"active={v.get('active_selector')}/service={v.get('service_selector')} (want {active})")
    if current is not None and v.get("current_pod_hash") != current:
        bad.append(f"currentPodHash={v.get('current_pod_hash')} (want {current})")
    return bad


def guarded_patch(check: Callable[[], None], real_patch: Callable[[Optional[int]], None]) -> Callable:
    """기존 restore_override의 patch_fn 주입 지점에 넣는다 - 실제 patch 바로 앞에서 대상·정책을 다시 확인한다."""
    def patch(value):
        check()
        real_patch(value)
    return patch


def finalize_override_guarded(state: dict, *, assessment: dict, runner_guard_ok: bool, view_fn: Callable[[], dict],
                              list_rs_fn: Callable[[], dict], policy_state_fn: Callable[[], dict], restore_fn: Callable,
                              real_patch_fn: Callable, safety_fn: Callable[[], None],
                              monotonic: Callable = time.monotonic, sleep: Callable = time.sleep,
                              now_utc: Callable = _utc, margin_sec: float = SCALE_DOWN_MARGIN_SEC,
                              poll_sec: float = POLL_SEC) -> dict:
    """act 정리 마지막 단계. 반환에 restored(True/False/None)와 근거. 예외를 던지지 않는다(중단 신호 포함)."""
    verdict = assessment.get("verdict")
    wait = {}
    try:
        return _finalize(state, verdict, assessment, runner_guard_ok, view_fn, list_rs_fn, policy_state_fn, restore_fn,
                         real_patch_fn, safety_fn, monotonic, sleep, now_utc, margin_sec, poll_sec, wait)
    except BaseException as e:  # noqa: BLE001 - finally 안에서 불리므로 남은 정리·요약 기록을 막지 않는다
        return {"path": verdict, "restored": False, "held": True, "wait": wait,
                "error": f"finalize_interrupted_or_failed: {type(e).__name__}: {e}"}


def _finalize(state, verdict, assessment, runner_guard_ok, view_fn, list_rs_fn, policy_state_fn, restore_fn,
              real_patch_fn, safety_fn, monotonic, sleep, now_utc, margin_sec, poll_sec, wait) -> dict:
    record = state.get("override")
    if record is None:
        p = state.get("override_record_path")
        if p is not None and Path(p).exists():
            return {"path": verdict, "restored": False, "held": True,
                    "error": "override_record_only_on_disk - 적용 여부·대상 확인 전 원복하지 않음, 사람 확인"}
        return {"path": verdict, "restored": None, "skipped": "override_not_applied"}
    old_hash = record["before"]["active_selector"]

    def restore(promoted, check):
        try:
            r = restore_fn(record, promoted=promoted, old_pod_hash=old_hash, wait_timeout_sec=RESTORE_RECHECK_SEC,
                           patch_fn=guarded_patch(check, real_patch_fn))
        except Exception as e:  # noqa: BLE001
            r = {"restored": False, "error": f"{type(e).__name__}: {e}"}
        return {"path": verdict, **r, "wait": wait}

    if verdict == "not_promoted":  # 미승격 확인 - 원복 직전에 selector·정책을 다시 본다(기존 finalize 우회 경로 없음)
        def check_not_promoted():
            bad = _view_matches(view_fn(), record, old_hash)
            if bad:
                raise RuntimeError(f"not_promoted_view_changed_before_patch: {bad}")
            check_policy_ok(policy_state_fn)
        return restore(False, check_not_promoted)
    if verdict != "promoted_service_ok" or not runner_guard_ok:
        return {"path": verdict, "restored": False, "held": True,
                "error": f"restore_held: verdict={verdict} runner_guard_ok={runner_guard_ok} "
                         f"reasons={assessment.get('reasons')}"}
    tgt = assessment["target"]
    new_hash, applied, tk = tgt["new"], record["applied_value"], tgt["target_key"]
    p_called = _ts(tgt["promote_called_at"])
    if p_called is None or p_called.tzinfo is None:
        return {"path": verdict, "restored": False, "held": True,
                "error": f"promote_called_at_invalid: {tgt['promote_called_at']}"}
    if not (all(type(x) in (int, float) and 0 <= x < float("inf") for x in (applied, margin_sec, poll_sec)) and poll_sec > 0):
        return {"path": verdict, "restored": False, "held": True,
                "error": f"wait_settings_invalid: applied={applied!r} margin={margin_sec!r} poll={poll_sec!r}"}
    deadline_utc = p_called + timedelta(seconds=applied + margin_sec)
    t0 = monotonic()
    # 벽시계는 남은 시간 계산에 여기서 한 번만 쓰고 applied+margin을 넘지 않게 자른다(시계가 틀려도 대기는 유한). 이후는 단조 시계.
    remaining = min(max(0.0, (deadline_utc - now_utc()).total_seconds()), applied + margin_sec)
    deadline_mono = t0 + max(remaining, MIN_CHECK_SEC)  # 늦게 시작해도 한 바퀴 확인 시간은 준다 - 원복 조건은 그대로
    limit = f"deadline_utc={deadline_utc.isoformat()} wait_limit_sec={deadline_mono - t0:.1f}"
    wait.update(old_hash=old_hash, new_hash=new_hash, applied_delay_sec=applied, margin_sec=margin_sec,
                promote_called_at=tgt["promote_called_at"], deadline_utc=deadline_utc.isoformat(),
                min_check_sec=MIN_CHECK_SEC, wait_limit_sec=round(deadline_mono - t0, 3),
                polls=0, safety_checks=0, read_failures=0, nonzero_samples=0, last_rs=None)

    def held(err):
        wait["ended_mono_elapsed_sec"] = round(monotonic() - t0, 3)
        return {"path": verdict, "restored": False, "held": True, "error": err, "wait": wait}

    def check_promoted():
        if monotonic() > deadline_mono:
            raise RuntimeError(f"deadline_passed_before_patch: {limit}")
        bad = _view_matches(view_fn(), record, new_hash, current=new_hash)
        if bad:
            raise RuntimeError(f"target_changed: {bad}")
        check_policy_ok(policy_state_fn, tk)
        if monotonic() > deadline_mono:  # 조회가 상한을 넘겨 끝났으면 patch하지 않는다
            raise RuntimeError(f"deadline_passed_before_patch: {limit}")
    fails = 0
    while True:
        try:
            safety_fn()
            check_policy_ok(policy_state_fn, tk)
            wait["safety_checks"] += 1
        except Exception as e:  # noqa: BLE001 - 안전·정책 문제면 기다림을 멈추고 유예를 줄이지 않는다
            return held(f"safety_failed_during_wait: {type(e).__name__}: {e}")
        try:
            v, rs = view_fn(), list_rs_fn()
            fails = 0
        except Exception as e:  # noqa: BLE001
            fails += 1
            wait["read_failures"] += 1
            if fails >= READ_FAIL_LIMIT:
                return held(f"state_read_failed_during_wait x{fails}: {type(e).__name__}: {e}")
            v = rs = None
        if monotonic() > deadline_mono:  # 상한을 넘겨 읽은 표본은 0이어도 받아들이지 않는다
            return held(f"old_rs_not_zero_by_deadline: last={wait['last_rs']} {limit}")
        if v is not None:
            wait["polls"] += 1
            bad = _view_matches(v, record, new_hash, current=new_hash)
            if bad:
                return held(f"target_changed_during_wait: {bad}")
            old = rs.get(old_hash, {"desired": 0, "current": 0})
            wait["last_rs"] = old
            if old.get("desired") == 0 and old.get("current") == 0:
                wait["old_rs_zero_observed_at_utc"] = now_utc().isoformat()
                break
            wait["nonzero_samples"] += 1
        if monotonic() >= deadline_mono:
            return held(f"old_rs_not_zero_by_deadline: last={wait['last_rs']} {limit}")
        sleep(max(0.0, min(poll_sec, deadline_mono - monotonic())))  # 상한 너머로 자지 않는다
    wait["ended_mono_elapsed_sec"] = round(monotonic() - t0, 3)
    return restore(True, check_promoted)


# ---------------- canonical Healthy 시작 상태·미승격 복원(계약 r2, 2026-10-07) ----------------
# 근거: canonical-restore-check2-20261007(사전 기준 sha 3451b55a… 아래 2차 실측). 검증된 조작은 abort 뒤 Rollout
# spec.template의 experiment-prep-ts 하나를 stable RS 값으로 되돌리는 merge patch뿐이다. 다른 복원 방법은 쓰지 않는다.
PREP_KEY = "experiment-prep-ts"
HASH_LABEL = "rollouts-pod-template-hash"
GEN_RE = re.compile(r"0|[1-9][0-9]{0,18}")
INT64_MAX = 2 ** 63 - 1


def norm_generation(x) -> int:
    """generation 계열 입력 계약: int(bool 제외) 또는 ASCII 10진 문자열(앞자리 0 없음, '0'만 예외), 0 ≤ 값 ≤ int64 최대.
    Argo status.observedGeneration은 문자열, metadata.generation은 정수로 온다(2차 검증 판정기 결함). 그 밖은 ValueError."""
    if type(x) is int:
        v = x
    elif type(x) is str and GEN_RE.fullmatch(x):
        v = int(x)
    else:
        raise ValueError(f"generation_invalid: {x!r}")
    if not 0 <= v <= INT64_MAX:
        raise ValueError(f"generation_out_of_range: {x!r}")
    return v


def active_pod(s: dict) -> Optional[dict]:
    pods = s.get("pods") or []
    return pods[0] if len(pods) == 1 else None


def check_canonical(s: dict, *, delay: Optional[int] = 30, restarts: int = 0) -> list:
    """canonical Healthy 상태인지 검사만 한다(고치지 않음). 반환: 문제 목록(빈 목록 = canonical). delay=None이면 유예는 보지 않는다."""
    p = []
    try:
        if norm_generation(s.get("observed")) != norm_generation(s.get("generation")):
            p.append(f"not_reconciled: observed={s.get('observed')!r} generation={s.get('generation')!r}")
    except ValueError as e:
        p.append(str(e))
    act = s.get("active")
    if s.get("phase") != "Healthy":
        p.append(f"phase={s.get('phase')}")
    if s.get("abort"):
        p.append("abort=true")
    if not act or not (s.get("current") == s.get("stable") == s.get("preview") == act):
        p.append(f"hash_mismatch: active={act} stable={s.get('stable')} current={s.get('current')} preview={s.get('preview')}")
    sel = s.get("svc_selectors") or {}
    if (sel.get("vllm-active") or {}).get(HASH_LABEL) != act or (sel.get("vllm-preview") or {}).get(HASH_LABEL) != act:
        p.append(f"service_selector_mismatch: {sel}")
    pod = active_pod(s)
    if pod is None or pod.get("hash") != act or not pod.get("ready") or pod.get("deleting") or pod.get("restarts") != restarts:
        p.append(f"active_pod_not_canonical: {s.get('pods')}")
    if pod is None or (s.get("endpoints") or {}).get("vllm-active") != [pod.get("ip")]:
        p.append(f"active_endpoint_mismatch: {s.get('endpoints')}")
    for h, r in (s.get("rs") or {}).items():
        want = (1, 1, 1) if h == act else (0, 0, 0)
        if (r.get("desired") or 0, r.get("current") or 0, r.get("ready") or 0) != want:
            p.append(f"rs_{h}_replicas={r} (want {want})")
    if act not in (s.get("rs") or {}):
        p.append("active_rs_missing")
    if delay is not None and s.get("delay") != delay:
        p.append(f"scaleDownDelaySeconds={s.get('delay')} != {delay}")
    if s.get("health_rc") != 0:
        p.append(f"active_health_rc={s.get('health_rc')}")
    if s.get("chaos") or s.get("probe_pods"):
        p.append(f"residual: chaos={s.get('chaos')} probe={s.get('probe_pods')}")
    return p


def _norm_template(t: dict) -> dict:
    t = json.loads(json.dumps(t))
    ((t.get("metadata") or {}).get("labels") or {}).pop(HASH_LABEL, None)
    (t.get("spec") or {}).pop("affinity", None)  # Argo가 strategy.antiAffinity로 RS마다 주입(대상 hash만 다름) - 2차 실측과 같은 기준
    return t


def _diff(a, b, p=""):
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            yield from _diff(a.get(k), b.get(k), f"{p}.{k}")
    elif a != b:
        yield p


def restore_canonical(prep_info: dict, expected_pod: Optional[tuple], *, snapshot_fn: Callable[[], dict],
                      rs_template_fn: Callable[[str], dict],
                      patch_prep_ts_fn: Callable[[str], None], sleep: Callable = time.sleep, monotonic: Callable = time.monotonic,
                      settle_wait_sec: float = 120.0, converge_sec: float = 60.0, stable_sec: float = 20.0,
                      poll_sec: float = 2.0) -> dict:
    """D5 abort가 끝난 뒤에만 호출. 예외를 던지지 않는다. ok=True는 canonical Healthy 재확인까지 끝났을 때만.
    patch는 patch_prep_ts_fn(값) 1회 - 바꿀 수 있는 field는 experiment-prep-ts 하나뿐이다(함수 계약). 실패 시 재시도 없음."""
    ev = {"patched": False}
    try:
        pre_active, aborted = prep_info.get("pre_prepare_active_selector"), prep_info.get("created_pod_hash")
        t0 = monotonic()
        while True:  # 중단된 preview Pod 종료만 기다린다(읽기)
            s0 = snapshot_fn()
            if [x.get("hash") for x in s0.get("pods") or []] == [pre_active]:
                break
            if monotonic() - t0 > settle_wait_sec:
                return {"ok": False, "problems": [f"preview_pod_not_gone: {s0.get('pods')}"], "evidence": ev}
            sleep(poll_sec)
        ap = active_pod(s0)
        p = []
        if not expected_pod or (ap.get("name"), ap.get("uid")) != tuple(expected_pod):  # 실행 시작 canonical 검사의 active Pod
            p.append(f"active_pod_not_start_pod: {(ap.get('name'), ap.get('uid'))} (want {expected_pod})")
        if not (s0.get("abort") and s0.get("phase") == "Degraded" and s0.get("active") == s0.get("stable") == pre_active
                and s0.get("current") == aborted and aborted and aborted != pre_active):
            p.append(f"not_expected_aborted_state: phase={s0.get('phase')} abort={s0.get('abort')} active={s0.get('active')} "
                     f"stable={s0.get('stable')} current={s0.get('current')} (want {pre_active}/{aborted})")
        if ((s0.get("svc_selectors") or {}).get("vllm-active") or {}).get(HASH_LABEL) != pre_active or \
                (s0.get("endpoints") or {}).get("vllm-active") != [ap.get("ip")] or s0.get("health_rc") != 0:
            p.append(f"active_service_not_ok: {s0.get('svc_selectors')} {s0.get('endpoints')} health={s0.get('health_rc')}")
        t_st, t_ab = rs_template_fn(pre_active), rs_template_fn(aborted)
        d = list(_diff(_norm_template(t_ab), _norm_template(t_st)))
        stable_val = ((t_st.get("metadata") or {}).get("annotations") or {}).get(PREP_KEY)
        ab_meta = json.loads(json.dumps(t_ab.get("metadata") or {}))
        (ab_meta.get("labels") or {}).pop(HASH_LABEL, None)
        if d != [f".metadata.annotations.{PREP_KEY}"]:
            p.append(f"template_diff_not_single_prep_ts: {d}")
        if type(stable_val) is not str or not stable_val:
            p.append(f"stable_prep_ts_missing: {stable_val!r}")
        if (s0.get("template") or {}).get("metadata") != ab_meta:
            p.append("rollout_template_metadata_differs_from_aborted_rs")
        ev.update(before=s0, template_diff=d, stable_value=stable_val, previous_value=s0.get("template_prep_ts"))
        if p:
            return {"ok": False, "problems": p, "evidence": ev}
        served = {x.get("hash") for x in s0["pods"]}
        deletable = sorted(h for h, r in s0["rs"].items() if h not in (pre_active, s0.get("stable"), *served)
                           and (r.get("desired") or 0, r.get("current") or 0, r.get("ready") or 0) == (0, 0, 0))
        s1 = snapshot_fn()  # 직전 재확인
        if s1.get("generation") != s0.get("generation") or s1.get("template_prep_ts") != s0.get("template_prep_ts"):
            return {"ok": False, "problems": ["rollout_changed_before_patch"], "evidence": ev}
        ev.update(allowed_deletions=deletable, patched_at_utc=_utc().isoformat())
        patch_prep_ts_fn(stable_val)
        ev["patched"] = True

        def violations(s):
            v = check_canonical(s, delay=None, restarts=ap.get("restarts"))
            bad = []
            if set(s.get("rs") or {}) - set(s0["rs"]):
                bad.append(f"new_rs: {sorted(set(s['rs']) - set(s0['rs']))}")
            gone = set(s0["rs"]) - set(s.get("rs") or {})
            if gone - set(deletable):
                bad.append(f"disallowed_rs_deletion: {sorted(gone - set(deletable))}")
            if (s.get("rs") or {}).get(pre_active, {}).get("uid") != s0["rs"][pre_active].get("uid"):
                bad.append("active_rs_uid_changed")
            if [(x.get("name"), x.get("uid")) for x in s.get("pods") or []] != [(ap.get("name"), ap.get("uid"))]:
                bad.append(f"pods_changed: {s.get('pods')}")
            if s.get("active") != pre_active or (s.get("svc_selectors") or {}).get("vllm-active") != s0["svc_selectors"]["vllm-active"] \
                    or (s.get("endpoints") or {}).get("vllm-active") != s0["endpoints"]["vllm-active"] or s.get("health_rc") != 0:
                bad.append("active_traffic_or_health_changed")
            return bad, v
        timeline, streak, t1 = [], 0, monotonic()
        while True:
            s = snapshot_fn()
            bad, v = violations(s)
            timeline.append({"t": s.get("t"), "phase": s.get("phase"), "abort": s.get("abort"), "bad": bad, "not_canonical": v})
            if bad:
                return {"ok": False, "problems": bad, "evidence": {**ev, "timeline": timeline}}
            streak = streak + 1 if not v else 0
            if streak >= 3:
                break
            if monotonic() - t1 > converge_sec:
                return {"ok": False, "problems": [f"not_canonical_by_deadline: {v}"], "evidence": {**ev, "timeline": timeline}}
            sleep(poll_sec)
        sleep(stable_sec)
        final = snapshot_fn()
        bad, v = violations(final)
        ev.update(timeline=timeline, final=final,
                  bookkeeping={"active_rs_revision": [s0["rs"][pre_active].get("revision"), (final.get("rs") or {}).get(pre_active, {}).get("revision")],
                               "deleted_rs": sorted(set(s0["rs"]) - set(final.get("rs") or {}))})
        return {"ok": not bad and not v, "problems": bad + v, "evidence": ev}
    except BaseException as e:  # noqa: BLE001 - 정리 중 중단 신호도 실패로 기록(재시도 없음)
        return {"ok": False, "problems": [f"restore_error: {type(e).__name__}: {e}"], "evidence": ev}


def wait_canonical(snapshot_fn: Callable[[], dict], *, delay: int = 30, timeout_sec: float = 90.0, poll_sec: float = 3.0,
                   sleep: Callable = time.sleep, monotonic: Callable = time.monotonic) -> dict:
    """실행 끝 확인(읽기만): 옛 Pod 종료 등 정리 중 상태가 canonical로 수렴하는지 기한 안에서 본다. 고치지 않는다."""
    t0, last = monotonic(), None
    while True:
        try:
            s = snapshot_fn()
            last = check_canonical(s, delay=delay)
        except Exception as e:  # noqa: BLE001
            last = [f"snapshot_error: {type(e).__name__}: {e}"]
        if not last:
            return {"ok": True, "problems": []}
        if monotonic() - t0 > timeout_sec:
            return {"ok": False, "problems": last}
        sleep(poll_sec)


# ---------------- 실제 클러스터 읽기(라이브 전용, 읽기만) ----------------
NAMESPACE, ROLLOUT, ACTIVE_SVC, POLICY_DEPLOY = "vllm-serving", "vllm-serving", "vllm-active", "guarded-recovery-policy"
LEDGER_IN_POD = "/data/guarded/ledger.jsonl"
API_TIMEOUT = 5


def _apis():  # pragma: no cover - 라이브 전용
    from kubernetes import client, config
    config.load_kube_config()
    return client.CustomObjectsApi(), client.CoreV1Api(), client.AppsV1Api()


def read_view() -> dict:  # pragma: no cover - 라이브 전용
    custom, core, _ = _apis()
    ro = custom.get_namespaced_custom_object("argoproj.io", "v1alpha1", NAMESPACE, "rollouts", ROLLOUT,
                                             _request_timeout=API_TIMEOUT)
    svc = core.read_namespaced_service(ACTIVE_SVC, NAMESPACE, _request_timeout=API_TIMEOUT)
    st, bg = ro.get("status") or {}, ((ro.get("spec") or {}).get("strategy") or {}).get("blueGreen") or {}
    return {"uid": ro["metadata"]["uid"], "active_selector": (st.get("blueGreen") or {}).get("activeSelector"),
            "preview_selector": (st.get("blueGreen") or {}).get("previewSelector"),
            "current_pod_hash": st.get("currentPodHash"), "delay": bg.get("scaleDownDelaySeconds"),
            "service_selector": (svc.spec.selector or {}).get("rollouts-pod-template-hash")}


def list_rs() -> dict:  # pragma: no cover - 라이브 전용
    _, _, apps = _apis()
    out = {}
    for rs in apps.list_namespaced_replica_set(NAMESPACE, label_selector="app=vllm-serving",
                                               _request_timeout=API_TIMEOUT).items:
        h = (rs.metadata.labels or {}).get("rollouts-pod-template-hash")
        if h:
            out[h] = {"desired": rs.spec.replicas or 0, "current": (rs.status.replicas or 0) if rs.status else 0}
    return out


def read_ledger(timeout: float = 30.0) -> list:  # pragma: no cover - 라이브 전용(정책 Pod 안 원장 파일 읽기만)
    cp = subprocess.run(["kubectl", "-n", NAMESPACE, "exec", f"deploy/{POLICY_DEPLOY}", "--", "cat", LEDGER_IN_POD],
                        capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    if cp.returncode != 0:
        raise RuntimeError(f"ledger read rc={cp.returncode}: {cp.stderr[-300:]}")
    return [json.loads(x) for x in cp.stdout.splitlines() if x.strip()]


def read_snapshot(residuals: bool = True) -> dict:  # pragma: no cover - 라이브 전용(읽기만, 2차 실측 스크립트와 같은 필드)
    custom, core, apps = _apis()
    ro = custom.get_namespaced_custom_object("argoproj.io", "v1alpha1", NAMESPACE, "rollouts", ROLLOUT, _request_timeout=API_TIMEOUT)
    st = ro.get("status") or {}
    bg = st.get("blueGreen") or {}
    pods = core.list_namespaced_pod(NAMESPACE, label_selector="app=vllm-serving", _request_timeout=API_TIMEOUT).items
    out = {"t": _utc().isoformat(), "uid": ro["metadata"]["uid"], "generation": ro["metadata"]["generation"],
           "observed": st.get("observedGeneration"), "phase": st.get("phase"), "abort": st.get("abort"),
           "active": bg.get("activeSelector"), "preview": bg.get("previewSelector"), "stable": st.get("stableRS"),
           "current": st.get("currentPodHash"), "delay": ro["spec"]["strategy"]["blueGreen"].get("scaleDownDelaySeconds"),
           "template": ro["spec"]["template"],
           "template_prep_ts": ((ro["spec"]["template"].get("metadata") or {}).get("annotations") or {}).get(PREP_KEY),
           "rs": {(r.metadata.labels or {}).get(HASH_LABEL): {
               "uid": r.metadata.uid, "desired": r.spec.replicas, "current": r.status.replicas, "ready": r.status.ready_replicas,
               "revision": (r.metadata.annotations or {}).get("rollout.argoproj.io/revision")}
               for r in apps.list_namespaced_replica_set(NAMESPACE, label_selector="app=vllm-serving", _request_timeout=API_TIMEOUT).items},
           "pods": sorted(({"name": p.metadata.name, "uid": p.metadata.uid, "ip": p.status.pod_ip,
                            "hash": (p.metadata.labels or {}).get(HASH_LABEL), "deleting": bool(p.metadata.deletion_timestamp),
                            "ready": bool(p.status.container_statuses and p.status.container_statuses[0].ready),
                            "restarts": p.status.container_statuses[0].restart_count if p.status.container_statuses else None}
                           for p in pods), key=lambda x: x["name"]),
           "svc_selectors": {s: core.read_namespaced_service(s, NAMESPACE, _request_timeout=API_TIMEOUT).spec.selector
                             for s in (ACTIVE_SVC, "vllm-preview")},
           "endpoints": {s: sorted(a.ip for ss in (core.read_namespaced_endpoints(s, NAMESPACE, _request_timeout=API_TIMEOUT).subsets or [])
                                   for a in (ss.addresses or [])) for s in (ACTIVE_SVC, "vllm-preview")}}
    act = [p["name"] for p in out["pods"] if p["hash"] == out["active"]]
    out["health_rc"] = subprocess.run(
        ["kubectl", "get", "--raw", f"/api/v1/namespaces/{NAMESPACE}/pods/{act[0]}:8000/proxy/health"],
        capture_output=True, timeout=20).returncode if len(act) == 1 else None
    if residuals:
        out["chaos"] = [c["metadata"]["name"] for c in custom.list_cluster_custom_object(
            "chaos-mesh.org", "v1alpha1", "stresschaos", _request_timeout=API_TIMEOUT).get("items", [])]
        out["probe_pods"] = [p.metadata.name for p in core.list_pod_for_all_namespaces(_request_timeout=API_TIMEOUT).items
                             if "probe" in p.metadata.name or "loadgen" in p.metadata.name]
    return out


def rs_template(pod_hash: str) -> dict:  # pragma: no cover - 라이브 전용(읽기만)
    from kubernetes import client
    _, _, apps = _apis()
    rs = apps.read_namespaced_replica_set(f"{ROLLOUT}-{pod_hash}", NAMESPACE, _request_timeout=API_TIMEOUT)
    return client.ApiClient().sanitize_for_serialization(rs)["spec"]["template"]


def patch_prep_ts(value: str) -> None:  # pragma: no cover - 라이브 전용. 이 annotation 하나만 바꾼다(2차 실측과 같은 merge patch)
    custom, _, _ = _apis()
    custom.patch_namespaced_custom_object("argoproj.io", "v1alpha1", NAMESPACE, "rollouts", ROLLOUT,
                                          {"spec": {"template": {"metadata": {"annotations": {PREP_KEY: value}}}}},
                                          _request_timeout=API_TIMEOUT)
