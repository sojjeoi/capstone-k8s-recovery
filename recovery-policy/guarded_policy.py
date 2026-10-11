"""guarded 복구 정책(별도 버전, 기존 main.py/policy.py/safety.py 무수정).

기본 방향: 첫 송신 조건(유효한 v2 신호 1개)에 반응한다 - 재확인 대기 없음(120초 후보는 미채택, 개발 분석으로만 보존).
기본 모드는 record_only(조치 없음). act 모드는 시간 기준·클러스터 조회·승격·감사 함수가 모두 주어져야만 만들 수 있다.

처리 순서(프로세스 잠금 하나 안에서 전체를 직렬화 - 단일 프로세스 전제):
 1) 서버 수신 시각을 입구에서 고정
 2) 상태 사용 가능 여부(손상·유실이면 모든 신호에 조치 불가)
 3) 스키마 검증 → 전달 중복(같은 키·같은 본문=원래 결과 반환, 같은 키·다른 본문=충돌)
 4) 실행 context: 존재·run_id·탐지기·모델·아티팩트 일치, 탐지기 세션 고정, evaluation_seq 증가(역순·재생 거부)
 5) 시각: 미래(판정 시각이 수신 시각보다 허용 오차 이상 앞섬)·나이 초과·평가 창 끝→판정 지연 초과 거부.
    기준 값은 GuardConfig로 명시해야 한다(기본값 없음) - 없으면 record_only에서는 not_evaluated, act는 생성 거부.
 6) 대상: Rollout(이름·UID)·active/preview selector·BlueGreenPause·Pod(UID·재시작 수·preview Ready·비종료·각 1개)
 7) record_only면 여기서 판단만 기록(would_pass 또는 거부 사유) - 조치 상태를 소비하지 않는다
 8) act: 미해결 조치(전역 차단)·같은 대상 기존 조치·context당 조치 한도·context 조치 허용 확인
 9) 조치 예약(in_progress) 영속화 → 실패면 승격 안 함
10) 시도 감사 기록 → 실패면 예약을 aborted로 바꾸고 승격 안 함
11) 최종 재확인(현재 시각 기준 나이·context·대상 재조회) → 실패면 aborted_final_check(비차단), 승격 안 함
12) 승격 1회(자동 재시도 없음): 예외=unknown, 미검증=unverified(둘 다 전역 차단, 사람 확인 필요)
13) 결과 영속화(실패면 프로세스 차단)·결과 감사(실패면 차단)
14) 검증된 경우에만 서비스 확인(예외=unknown) - 승격 결과를 덮어쓰지 않고 별도 필드·상태로 남긴다
결과 구분: http_delivered(요청 수신) / accepted(스키마·중복 통과) / policy_passed / promotion_verified / service_ok.
"""
import contextlib
import functools
import json
import math
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import guarded_ledger as gl
import guarded_state as gs
from guarded_schema import SCHEMA_VERSION, parse_signal, payload_digest

MODE_RECORD_ONLY = "record_only"
MODE_ACT = "act"
POLICY_VERSION = "guarded-v1"


class AuditError(Exception):
    pass


class AuditLog:
    """추가 전용 JSONL 감사 기록(flush+fsync). 실패는 AuditError."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, record: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as e:
            raise AuditError(f"{type(e).__name__}: {e}") from e


@dataclass
class GuardConfig:
    mode: str = MODE_RECORD_ONLY
    max_signal_age_sec: Optional[float] = None
    max_future_skew_sec: Optional[float] = None
    max_window_to_decision_sec: Optional[float] = None
    max_actions_per_context: int = 1

    def __post_init__(self):
        for name in ("max_signal_age_sec", "max_future_skew_sec", "max_window_to_decision_sec"):
            v = getattr(self, name)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0):
                raise ValueError(f"{name}는 유한한 0 이상 숫자여야 함: {v!r}")
        if self.max_actions_per_context != 1 or isinstance(self.max_actions_per_context, bool):
            raise ValueError(f"max_actions_per_context는 1이어야 함(실행당 실제 조치 1회): {self.max_actions_per_context!r}")

    def timing_configured(self) -> bool:
        return None not in (self.max_signal_age_sec, self.max_future_skew_sec, self.max_window_to_decision_sec)


def _iso(t):
    return t.isoformat() if isinstance(t, datetime) else t


def target_key(rollout: dict, obs) -> str:
    return (f"{rollout['namespace']}/{rollout['name']}@{rollout['uid']}:"
            f"{obs.active_hash}->{obs.preview_hash}#{obs.preview_pod_uid}")


def check_target(view: dict, ctx: dict, obs) -> list:
    """현재 클러스터 상태가 신호가 관측한 대상과 같은지. 거부 사유 목록(빈 목록 = 일치)."""
    reasons = []
    r = view.get("rollout") or {}
    want = ctx["rollout"]
    if (r.get("namespace"), r.get("name"), r.get("uid")) != (want["namespace"], want["name"], want["uid"]):
        reasons.append("rollout_mismatch")
    if r.get("active_selector") != obs.active_hash:
        reasons.append("active_changed")
    if r.get("preview_selector") != obs.preview_hash:
        reasons.append("preview_changed")
    if not r.get("paused_bluegreen"):
        reasons.append("preview_not_paused")
    pods = view.get("pods") or []
    for role, h, uid, rc, need_ready in (("active", obs.active_hash, obs.active_pod_uid, obs.active_restart_count, False),
                                         ("preview", obs.preview_hash, obs.preview_pod_uid, obs.preview_restart_count, True)):
        cand = [p for p in pods if p.get("hash") == h]
        if len(cand) != 1:
            reasons.append(f"{role}_pod_count={len(cand)}")
            continue
        p = cand[0]
        if p.get("uid") != uid:
            reasons.append(f"{role}_uid_changed")
        if p.get("terminating"):
            reasons.append(f"{role}_terminating")
        if rc is not None and p.get("restart_count") != rc:
            reasons.append(f"{role}_restarted")
        if need_ready and not p.get("ready"):
            reasons.append("preview_not_ready")
    return reasons


class ShuttingDown(RuntimeError):
    """정상 종료 중 - 새 작업을 받지 않는다."""


def _admitted(fn):
    """관리 변경 작업: 종료 중이면 거부하고, 진행 중 작업 수에 포함한다(종료 시 끝날 때까지 기다리는 대상)."""
    @functools.wraps(fn)
    def wrapper(self, *a, **k):
        with self._admit():
            return fn(self, *a, **k)
    return wrapper


class GuardedPolicy:
    def __init__(self, config: GuardConfig, store: gs.StateStore, *, audit: Optional[AuditLog] = None,
                 cluster_reader: Optional[Callable[[], dict]] = None, promote_fn: Optional[Callable[[dict], dict]] = None,
                 service_check_fn: Optional[Callable[[dict], dict]] = None, ledger: "Optional[gl.Ledger]" = None,
                 service_runner: Optional[Callable[[Callable], None]] = None,
                 now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc), initialize: bool = False):
        if config.mode not in (MODE_RECORD_ONLY, MODE_ACT):
            raise ValueError(f"알 수 없는 mode: {config.mode}")
        if config.mode == MODE_ACT:
            missing = [n for n, v in (("timing", config.timing_configured() or None), ("audit", audit),
                                      ("cluster_reader", cluster_reader), ("promote_fn", promote_fn),
                                      ("service_check_fn", service_check_fn), ("ledger", ledger)) if not v]
            if missing:
                raise ValueError(f"act 모드 거부 - 필수 설정 없음: {missing}")
        self.config, self.store, self.audit, self.ledger = config, store, audit, ledger
        self.cluster_reader, self.promote_fn, self.service_check_fn = cluster_reader, promote_fn, service_check_fn
        self.service_runner = service_runner or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self.now_fn = now_fn
        self._lock = threading.RLock()  # 서비스 확인 작업이 같은 스레드에서 실행돼도(테스트) 교착되지 않게
        # 수명주기: 진행 중 요청 수·백그라운드 서비스 확인 작업 수·종료 중 표시(정책 잠금과 분리 - 대기 중에도 읽을 수 있게)
        self._work_lock = threading.Lock()
        self._active, self._workers, self._closing = 0, 0, False
        self._inflight = None
        self.unavailable = None
        try:
            self.state = store.load_for_restart(initialize=initialize)
            if ledger is not None:
                self._merge_ledger()
        except (gs.StateUnavailable, gs.StateSaveError, gl.LedgerUnavailable) as e:
            self.state, self.unavailable = None, str(e)

    def _merge_ledger(self) -> None:
        """원장이 우선 - 새 상태 파일·새 run_id로 이전 조치(미해결·완료)를 우회하지 못하게 한다."""
        changed = False
        for tk, a in self.ledger.reconstruct().items():
            cur = self.state["actions"].get(tk)
            if cur is not None and cur.get("status") == a["status"]:
                continue
            self.state["actions"][tk] = {**(cur or {}), "status": a["status"], "run_id": a["run_id"],
                                         "source": "ledger", "ledger_events": a["events"]}
            changed = True
        if changed:
            self.state["history"].append({"at": _iso(self.now_fn()), "event": "ledger_merged"})
            self.store.save(self.state)

    # ---------- 공통 ----------
    def _audit(self, rec: dict) -> Optional[str]:
        if self.audit is None:
            return "audit_not_configured"
        try:
            self.audit.append({"policy_version": POLICY_VERSION, "mode": self.config.mode, **rec})
            return None
        except AuditError as e:
            return f"audit_error: {e}"

    def _save(self) -> Optional[str]:
        try:
            self.store.save(self.state)
            return None
        except gs.StateSaveError as e:
            return f"state_save_error: {e}"

    def _result(self, received_at, **kw) -> dict:
        base = {"policy_version": POLICY_VERSION, "mode": self.config.mode, "received_at": _iso(received_at),
                "http_delivered": True, "accepted": False, "duplicate": False, "policy_passed": None,
                "promotion": {"attempted": False, "status": None, "verified": None}, "service_ok": None, "reasons": []}
        base.update(kw)
        return base

    def _finish(self, res: dict, sig=None, digest=None, persist_delivery=True) -> dict:
        """판단 결과를 전달 기록·감사에 남긴다. 저장·감사 실패는 결과에 표시한다(조치 전 단계라 안전)."""
        if sig is not None and persist_delivery and self.state is not None:
            self.state["deliveries"][sig.delivery_key] = {"digest": digest, "outcome": res["outcome"],
                                                           "at": res["received_at"], "target_key": res.get("target_key")}
            err = self._save()
            if err:
                res["state_error"] = err
        aerr = self._audit({"event": "decision", "delivery_key": sig.delivery_key if sig else None, **res})
        if aerr:
            res["audit_error"] = aerr
        return res

    # ---------- 수명주기 ----------
    @contextlib.contextmanager
    def _admit(self):
        with self._work_lock:
            if self._closing:
                raise ShuttingDown("policy_shutting_down")
            self._active += 1
        try:
            yield
        finally:
            with self._work_lock:
                self._active -= 1

    def _start_worker(self, fn) -> None:
        """서비스 확인 등 백그라운드 기록 작업 - 끝날 때까지 종료 대기 대상으로 센다."""
        with self._work_lock:
            self._workers += 1

        def run():
            try:
                fn()
            finally:
                with self._work_lock:
                    self._workers -= 1
        try:
            self.service_runner(run)
        except BaseException:
            with self._work_lock:
                self._workers -= 1
            raise

    def busy(self) -> dict:
        with self._work_lock:
            return {"active_requests": self._active, "service_workers": self._workers, "closing": self._closing}

    def begin_shutdown(self) -> None:
        """새 신호·관리 변경을 받지 않는다. 진행 중 작업·백그라운드 작업은 그대로 끝까지 진행된다."""
        with self._work_lock:
            self._closing = True

    def wait_idle(self, timeout_sec: float, *, poll_sec: float = 0.1, monotonic=time.monotonic, sleep=time.sleep) -> dict:
        """진행 중 요청·백그라운드 작업이 0이 될 때까지 기다린다(상한 timeout_sec). idle=False면 남은 작업이 있다."""
        start = monotonic()
        while True:
            b = self.busy()
            if b["active_requests"] == 0 and b["service_workers"] == 0:
                return {"idle": True, "waited_sec": monotonic() - start, **b}
            if monotonic() - start >= timeout_sec:
                return {"idle": False, "waited_sec": monotonic() - start, **b}
            sleep(poll_sec)

    # ---------- 신호 처리 ----------
    def process(self, raw, received_at: Optional[datetime] = None) -> dict:
        received_at = received_at or self.now_fn()
        try:
            with self._admit():
                return self._process_admitted(raw, received_at)
        except ShuttingDown:  # 기록하지 않는다(종료 중 - 잠금 해제 전에 새 쓰기가 생기지 않게)
            return self._result(received_at, outcome="shutting_down", accepted=False, reasons=["policy_shutting_down"])

    def _process_admitted(self, raw, received_at) -> dict:
        with self._lock:
            self._inflight = None
            try:
                return self._process_locked(raw, received_at)
            except Exception as e:  # noqa: BLE001 - 예상 못 한 내부 오류: 진행 중 조치는 unknown(차단), 감사는 남긴다
                err = f"{type(e).__name__}: {e}"
                if self.state is not None:
                    for act in self.state["actions"].values():
                        if act.get("status") == "in_progress":
                            act["previous_status"], act["status"] = "in_progress", "unknown"
                            act["internal_error"] = err
                    self.state["blocked"] = {"reason": f"internal_error: {err}", "at": _iso(self.now_fn())}
                    self._save()
                res = self._result(received_at, outcome="internal_error", reasons=[err], blocked=True)
                prev = self._inflight or {}  # 승격 뒤 오류여도 실제 시도·검증 결과와 신호 식별자를 보존한다
                for k in ("delivery_key", "evaluation_seq", "decided_at", "target_key", "policy_passed", "promotion",
                          "service_ok", "action_status"):
                    if k in prev:
                        res[k] = prev[k]
                res["outcome_before_error"] = prev.get("outcome")
                aerr = self._audit({"event": "internal_error", **res})
                if aerr:
                    res["audit_error"] = aerr
                return res

    def _process_locked(self, raw, received_at) -> dict:
        if self.state is None:
            return self._finish(self._result(received_at, outcome="state_unavailable", reasons=[self.unavailable]))
        sig, err = parse_signal(raw)
        if sig is None:
            return self._finish(self._result(received_at, outcome="rejected_schema", reasons=[err]))
        digest = payload_digest(raw)
        prev = self.state["deliveries"].get(sig.delivery_key)
        if prev is not None:
            if prev["digest"] == digest:
                return self._finish(self._result(received_at, outcome="duplicate_delivery", duplicate=True, accepted=True,
                                                 original_outcome=prev["outcome"], target_key=prev.get("target_key")),
                                sig, digest, persist_delivery=False)  # 원래 조치 대상을 함께 돌려준다(실행기가 같은 조치로 묶음)
            return self._finish(self._result(received_at, outcome="delivery_conflict", accepted=False,
                                             reasons=["same_key_different_payload"]), sig, digest, persist_delivery=False)
        res = self._result(received_at, accepted=True, delivery_key=sig.delivery_key, evaluation_seq=sig.evaluation_seq,
                           decided_at=_iso(sig.decided_at_utc))

        # 4) context·식별·세션·순번
        ctx = self.state.get("context")
        reasons = self._context_reasons(ctx, sig)
        if reasons:
            res.update(outcome="rejected_context", policy_passed=False, reasons=reasons)
            return self._finish(res, sig, digest)
        sess_key = sig.experiment_run_id
        seen = self.state["sessions"].get(sess_key)
        if seen is not None and seen != sig.detector_session_id:
            res.update(outcome="rejected_session_changed", policy_passed=False,
                       reasons=[f"session {seen} != {sig.detector_session_id}"])
            return self._finish(res, sig, digest)
        seq_key = f"{sig.experiment_run_id}:{sig.detector_session_id}"
        last = self.state["last_seq"].get(seq_key)
        if last is not None and sig.evaluation_seq <= last:
            res.update(outcome="rejected_out_of_order", policy_passed=False,
                       reasons=[f"seq {sig.evaluation_seq} <= last {last}"])
            return self._finish(res, sig, digest)
        self.state["sessions"][sess_key] = sig.detector_session_id
        self.state["last_seq"][seq_key] = sig.evaluation_seq

        # 5) 시각
        if not self.config.timing_configured():
            res.update(outcome="not_evaluated_timing_config_missing", policy_passed=None, reasons=["timing_config_missing"])
            return self._finish(res, sig, digest)
        reasons = self._timing_reasons(sig, received_at)
        if reasons:
            res.update(outcome="rejected_timing", policy_passed=False, reasons=reasons)
            return self._finish(res, sig, digest)

        # 6) 대상
        if self.cluster_reader is None:
            res.update(outcome="not_evaluated_no_cluster_reader", policy_passed=None, reasons=["cluster_reader_missing"])
            return self._finish(res, sig, digest)
        try:
            reasons = check_target(self.cluster_reader(), ctx, sig.observed)
        except Exception as e:  # noqa: BLE001 - 조회 실패·비정상 응답 구조 모두 거부로 기록(감사·전달 기록 유지)
            res.update(outcome="rejected_target", policy_passed=False, reasons=[f"cluster_view_error: {type(e).__name__}"])
            return self._finish(res, sig, digest)
        if reasons:
            res.update(outcome="rejected_target", policy_passed=False, reasons=reasons)
            return self._finish(res, sig, digest)
        tkey = target_key(ctx["rollout"], sig.observed)
        res["target_key"] = tkey

        # 7) record_only
        if self.config.mode == MODE_RECORD_ONLY:
            res.update(outcome="would_pass_record_only", policy_passed=True)
            return self._finish(res, sig, digest)
        return self._act_locked(res, sig, digest, ctx, tkey)

    def _context_reasons(self, ctx, sig) -> list:
        if not ctx:
            return ["no_context"]
        out = []
        for field_name, want in (("experiment_run_id", ctx.get("run_id")), ("detector_id", ctx.get("detector_id")),
                                 ("model_version", ctx.get("model_version")), ("model_hash", ctx.get("model_hash")),
                                 ("artifact_manifest_hash", ctx.get("artifact_manifest_hash"))):
            if getattr(sig, field_name) != want:
                out.append(f"{field_name}_mismatch")
        if ctx.get("threshold") is not None and sig.threshold != ctx["threshold"]:
            out.append("threshold_mismatch")
        return out

    def _timing_reasons(self, sig, now) -> list:
        c = self.config
        out = []
        ahead = (sig.decided_at_utc - now).total_seconds()
        if ahead > c.max_future_skew_sec:
            out.append(f"future_signal: decided_at {ahead:.3f}s ahead of server time")
        age = (now - sig.decided_at_utc).total_seconds()
        if age > c.max_signal_age_sec:
            out.append(f"stale_signal: age {age:.3f}s")
        lag = (sig.decided_at_utc - sig.window_end_utc).total_seconds()
        if lag > c.max_window_to_decision_sec:
            out.append(f"stale_window: decided {lag:.3f}s after window end")
        return out

    def _act_locked(self, res, sig, digest, ctx, tkey) -> dict:
        self._inflight = res  # 안전망이 진행 중 결과(승격 시도·검증)를 보존하도록 같은 객체를 참조
        st = self.state
        if st.get("blocked"):
            res.update(outcome="blocked_state", policy_passed=False, reasons=[st["blocked"]["reason"]])
            return self._finish(res, sig, digest)
        unresolved = gs.unresolved_actions(st)
        if unresolved:
            res.update(outcome="blocked_unresolved", policy_passed=False, reasons=sorted(unresolved))
            return self._finish(res, sig, digest)
        prior = st["actions"].get(tkey)
        if prior is not None and not str(prior.get("status", "")).startswith("aborted"):
            res.update(outcome="already_acted_on_target", policy_passed=False, reasons=[prior["status"]])
            return self._finish(res, sig, digest)
        # 승격을 부르지 않은 중단(aborted_*)은 한도에 세지 않고 같은 대상의 새 신호도 다시 판단한다
        done = [a for a in st["actions"].values()
                if a.get("run_id") == ctx["run_id"] and not str(a.get("status", "")).startswith("aborted")]
        if len(done) >= self.config.max_actions_per_context:
            res.update(outcome="action_limit_reached", policy_passed=False, reasons=[f"actions_in_context={len(done)}"])
            return self._finish(res, sig, digest)
        if ctx.get("actions_allowed") is not True:
            res.update(outcome="actions_not_allowed", policy_passed=False,
                       reasons=[ctx.get("actions_disabled_reason") or "context"])
            return self._finish(res, sig, digest)

        res["policy_passed"] = True
        # 9) 예약 영속화(전달 기록 포함)
        st["actions"][tkey] = {"status": "in_progress", "run_id": ctx["run_id"], "delivery_key": sig.delivery_key,
                               "reserved_at": _iso(self.now_fn()),
                               "prior_aborts": ([*prior.get("prior_aborts", []), {k: v for k, v in prior.items()
                                                                                  if k != "prior_aborts"}] if prior else [])}
        st["deliveries"][sig.delivery_key] = {"digest": digest, "outcome": "reserved", "at": res["received_at"],
                                              "target_key": tkey}
        err = self._save()
        if err:
            if prior is not None:
                st["actions"][tkey] = prior
            else:
                del st["actions"][tkey]
            st["deliveries"].pop(sig.delivery_key, None)
            res.update(outcome="state_error_no_promote", reasons=[err])
            aerr = self._audit({"event": "decision", "delivery_key": sig.delivery_key, **res})
            if aerr:
                res["audit_error"] = aerr
            return res
        # 10) 시도 감사
        aerr = self._audit({"event": "promotion_attempt", "delivery_key": sig.delivery_key, "target_key": tkey})
        if aerr:  # 필수 기록 실패 - 승격하지 않고 프로세스를 차단한다(다음 신호가 기록 없이 진행하지 않게)
            st["blocked"] = {"reason": f"attempt_audit_failed: {aerr}", "at": _iso(self.now_fn())}
            res["blocked"] = True
            return self._abort(res, sig, tkey, "aborted_audit_failed", [aerr])
        # 11) 최종 재확인 - 대상 재조회를 먼저 하고, 조회가 끝난 뒤(승격 직전) 시각·context를 검사한다
        reasons = []
        try:
            reasons += check_target(self.cluster_reader(), ctx, sig.observed)
        except Exception as e:  # noqa: BLE001
            reasons.append(f"cluster_view_error: {type(e).__name__}")
        cur = self.state.get("context") or {}
        if cur.get("run_id") != ctx["run_id"] or cur.get("actions_allowed") is not True:
            reasons.append("context_changed")
        reasons += self._timing_reasons(sig, self.now_fn())
        if reasons:
            return self._abort(res, sig, tkey, "aborted_final_check", reasons)
        # 12-a) 원장 intent(fsync) - 실패면 승격 명령을 부르지 않는다
        if self.ledger is not None:
            try:
                self.ledger.append({"event": "intent", "target_key": tkey, "run_id": ctx["run_id"],
                                    "delivery_key": sig.delivery_key})
            except gl.LedgerWriteError as e:
                return self._abort(res, sig, tkey, "aborted_ledger_failed", [str(e)])
            # intent fsync가 오래 걸렸으면(저장소 지연) 신호가 낡았을 수 있다 - 시각 기준만 다시 본다(대상 재조회 경쟁은 §2 문서)
            late = self._timing_reasons(sig, self.now_fn())
            if late:
                try:
                    self.ledger.append({"event": "aborted", "target_key": tkey, "status": "aborted_final_check"})
                except gl.LedgerWriteError:
                    pass  # intent만 남으면 재시작 때 unknown으로 차단된다(안전 쪽)
                return self._abort(res, sig, tkey, "aborted_final_check", ["after_intent_write", *late])
        # 12) 승격 1회 - 결과 계약: called False(사전 확인 불일치, 명령 미호출) / verified True·False·None
        act = st["actions"][tkey]
        act["promote_called_at"] = _iso(self.now_fn())
        target = {"target_key": tkey, "rollout": ctx["rollout"], "observed": sig.observed.model_dump()}
        try:
            pr = self.promote_fn(target)
            if not isinstance(pr, dict) or not isinstance(pr.get("called"), bool) or \
                    pr.get("verified") not in (True, False, None):
                raise ValueError(f"승격 결과 계약 위반: {pr!r}"[:300])
        except Exception as e:  # noqa: BLE001 - 예외·계약 위반은 결과 불명(전역 차단)
            pr = {"called": True, "verified": None, "error": f"{type(e).__name__}: {e}"}
        act["promote_result"] = pr
        if pr["called"] is False:
            res["promotion"]["attempted"] = False
            if self.ledger is not None:
                try:
                    self.ledger.append({"event": "aborted", "target_key": tkey, "status": "aborted_adapter_precheck"})
                except gl.LedgerWriteError:
                    pass  # intent만 남으면 재시작 때 unknown으로 차단된다(안전 쪽)
            return self._abort(res, sig, tkey, "aborted_adapter_precheck", [pr.get("reason") or "adapter_precheck"])
        res["promotion"]["attempted"] = True
        verified = pr.get("verified")
        status = {True: "verified", False: "unverified", None: "unknown"}[verified]
        act["status"], act["finished_at"] = status, _iso(self.now_fn())
        res["promotion"].update(status=status, verified=verified, verified_at=pr.get("verified_at"))
        res["outcome"] = {"verified": "promoted_verified", "unverified": "promoted_unverified",
                          "unknown": "promotion_unknown"}[status]
        job = None
        if verified is True:  # 14) 서비스 확인은 비동기 - pending(차단)을 먼저 영속화한 뒤 시작한다
            job = self._service_job(tkey, target, pr)
            act["status"], act["service_job"] = "verified_service_pending", job
            res["service_ok"], res["service_check"] = None, "pending"
        res["action_status"] = act["status"]
        st["deliveries"][sig.delivery_key]["outcome"] = res["outcome"]
        # 13) 결과 영속화·원장·감사 - 실패면 프로세스 차단(서비스 확인도 시작하지 않음)
        err = self._save()
        if err:
            st["blocked"] = {"reason": f"state_error_after_promote: {err}", "at": _iso(self.now_fn())}
            res["state_error"], res["blocked"] = err, True
        if self.ledger is not None:
            try:
                self.ledger.append({"event": "result", "target_key": tkey, "status": act["status"], "verified": verified})
            except gl.LedgerWriteError as e:
                st["blocked"] = {"reason": f"ledger_error_after_promote: {e}", "at": _iso(self.now_fn())}
                res["ledger_error"], res["blocked"] = str(e), True
                self._save()
        aerr = self._audit({"event": "promotion_result", "delivery_key": sig.delivery_key, "target_key": tkey, **res})
        if aerr:
            st["blocked"] = {"reason": f"audit_error_after_promote: {aerr}", "at": _iso(self.now_fn())}
            res["audit_error"], res["blocked"] = aerr, True
            self._save()
        if job is not None and not res.get("blocked"):
            self._start_worker(lambda: self._service_worker(tkey, job))
        elif job is not None:
            res["service_check"] = "not_started_blocked"
        return res

    def _service_job(self, tkey, target, pr) -> dict:
        """서비스 확인 작업 입력을 승격 시점 값으로 고정한다(조치 키·Rollout UID·새/옛 Pod·전환 관측 시각)."""
        obs = target["observed"]
        view = {}
        try:
            view = self.cluster_reader() or {}
        except Exception:  # noqa: BLE001 - IP를 못 읽으면 판정 함수가 근거 부족(None)으로 처리
            pass
        pods = {p.get("uid"): p for p in (view.get("pods") or []) if isinstance(p, dict)}
        newp, oldp = pods.get(obs["preview_pod_uid"], {}), pods.get(obs["active_pod_uid"], {})
        return {"target_key": tkey, "rollout_uid": target["rollout"]["uid"], "verified_at": pr.get("verified_at"),
                "new_pod": newp.get("name"), "new_pod_uid": obs["preview_pod_uid"], "new_pod_ip": newp.get("ip"),
                "old_pod": oldp.get("name"), "old_pod_uid": obs["active_pod_uid"], "old_pod_ip": oldp.get("ip")}

    def _service_worker(self, tkey, job) -> None:
        try:
            sc = self.service_check_fn(job)
            ok = sc.get("ok") if isinstance(sc, dict) else None
            ok = ok if ok in (True, False, None) else None
        except Exception as e:  # noqa: BLE001
            sc, ok = {"error": f"{type(e).__name__}: {e}"}, None
        status = {True: "verified", False: "verified_service_failed", None: "verified_service_unknown"}[ok]
        with self._lock:
            act = (self.state or {}).get("actions", {}).get(tkey)
            if act is None or act.get("status") != "verified_service_pending":
                # 수동 해결 등으로 이미 바뀌었으면 덮어쓰지 않고 이력만 남긴다
                if act is not None:
                    act.setdefault("late_service_results", []).append({"at": _iso(self.now_fn()), "ok": ok})
                    self._save()
                self._audit({"event": "service_result_ignored", "target_key": tkey, "ok": ok})
                return
            act["status"], act["service_check"], act["service_finished_at"] = status, sc, _iso(self.now_fn())
            err = self._save()
            if err:
                self.state["blocked"] = {"reason": f"state_error_after_service_check: {err}", "at": _iso(self.now_fn())}
            if self.ledger is not None:
                try:
                    self.ledger.append({"event": "service_result", "target_key": tkey, "status": status, "ok": ok})
                except gl.LedgerWriteError as e:
                    self.state["blocked"] = {"reason": f"ledger_error_after_service_check: {e}", "at": _iso(self.now_fn())}
                    self._save()
            aerr = self._audit({"event": "service_result", "target_key": tkey, "status": status, "service_check": sc})
            if aerr:
                self.state["blocked"] = {"reason": f"audit_error_after_service_check: {aerr}", "at": _iso(self.now_fn())}
                self._save()

    def _abort(self, res, sig, tkey, status, reasons) -> dict:
        """승격을 부르지 않은 중단 - 비차단 상태로 남긴다(같은 대상 재시도는 막는다)."""
        self.state["actions"][tkey].update(status=status, reasons=reasons, finished_at=_iso(self.now_fn()))
        res.update(outcome=status, policy_passed=False, reasons=reasons)
        self.state["deliveries"][sig.delivery_key]["outcome"] = status
        err = self._save()
        if err:
            self.state["blocked"] = {"reason": f"state_error_on_abort: {err}", "at": _iso(self.now_fn())}
            res["state_error"] = err
        aerr = self._audit({"event": "decision", "delivery_key": sig.delivery_key, **res})
        if aerr:
            res["audit_error"] = aerr
        return res

    # ---------- 관리 ----------
    @_admitted
    def set_context(self, ctx: dict) -> dict:
        need = ("run_id", "detector_id", "model_version", "model_hash", "artifact_manifest_hash")
        if not isinstance(ctx, dict):
            raise ValueError("context는 객체여야 함")
        bad = [k for k in need if not isinstance(ctx.get(k), str) or not ctx.get(k)]
        roll = ctx.get("rollout")
        if not isinstance(roll, dict) or not all(isinstance(roll.get(k), str) and roll.get(k) for k in ("namespace", "name", "uid")):
            bad.append("rollout.namespace/name/uid")
        allowed = ctx.get("actions_allowed", False)
        if not isinstance(allowed, bool):  # "false" 같은 문자열을 True로 해석하지 않는다
            bad.append("actions_allowed(boolean)")
        thr = ctx.get("threshold")
        if thr is not None and (isinstance(thr, bool) or not isinstance(thr, (int, float)) or not math.isfinite(thr)):
            bad.append("threshold(number)")
        if bad:
            raise ValueError(f"context 항목 오류: {bad}")
        with self._lock:
            if self.state is None:
                raise RuntimeError(f"state_unavailable: {self.unavailable}")
            cur = self.state.get("context")
            if cur and cur["run_id"] != ctx["run_id"]:
                raise RuntimeError(f"다른 context 활성: {cur['run_id']}")
            if self.config.mode != MODE_ACT and allowed:
                raise RuntimeError("record_only 모드에서는 actions_allowed를 켤 수 없음")
            new = {**{k: ctx[k] for k in need}, "rollout": {k: roll[k] for k in ("namespace", "name", "uid")},
                   "threshold": thr, "actions_allowed": allowed, "set_at": _iso(self.now_fn())}
            self.state["context"] = new
            self.state["history"].append({"at": new["set_at"], "event": "context_set", "run_id": new["run_id"],
                                          "actions_allowed": new["actions_allowed"]})
            err = self._save()
            if err:  # 저장 실패면 메모리 context도 이전 값으로 되돌린다(저장 안 된 허용으로 승격 금지)
                self.state["context"] = cur
                self.state["history"].pop()
                raise RuntimeError(err)
            self._audit({"event": "context_set", "context": new})
            return new

    @_admitted
    def clear_context(self, run_id: str) -> dict:
        with self._lock:
            if self.state is None:
                raise RuntimeError(f"state_unavailable: {self.unavailable}")
            cur = self.state.get("context")
            if cur is None:
                return {"status": "already_clear"}
            if cur["run_id"] != run_id:
                raise RuntimeError(f"run_id 불일치: {cur['run_id']} != {run_id}")
            self.state["context"] = None
            self.state["history"].append({"at": _iso(self.now_fn()), "event": "context_cleared", "run_id": run_id})
            err = self._save()
            if err:  # 저장 실패면 메모리도 그대로 둔다(파일과 메모리 불일치 방지)
                self.state["context"] = cur
                self.state["history"].pop()
                raise RuntimeError(err)
            self._audit({"event": "context_cleared", "run_id": run_id})
            return {"status": "cleared"}

    @_admitted
    def resolve_action(self, tkey: str, resolution: str, evidence: dict, by: str) -> dict:
        """미해결 조치의 수동 해결 - 기록을 지우지 않고 이전 상태·근거를 남기는 전이."""
        if not resolution or not evidence or not by:
            raise ValueError("resolution·evidence·by 필수")
        with self._lock:
            act = (self.state or {}).get("actions", {}).get(tkey)
            if act is None or act.get("status") not in gs.UNRESOLVED:
                raise RuntimeError(f"해결할 미해결 조치 없음: {tkey}")
            prev_status = act["status"]
            if self.ledger is not None:  # 원장 기록이 실패하면 해결하지 않는다
                try:
                    self.ledger.append({"event": "resolution", "target_key": tkey, "status": "resolved",
                                        "previous_status": prev_status, "resolution": resolution, "by": by})
                except gl.LedgerWriteError as e:
                    raise RuntimeError(f"ledger_error: {e}") from e
            act.setdefault("resolutions", []).append({"previous_status": prev_status, "resolution": resolution,
                                                      "evidence": evidence, "by": by, "at": _iso(self.now_fn())})
            act["status"] = "resolved"
            err = self._save()
            if err:
                act["status"] = prev_status
                act["resolutions"].pop()
                raise RuntimeError(err)
            self._audit({"event": "action_resolved", "target_key": tkey, "resolution": resolution, "by": by})
            return act

    @_admitted
    def clear_block(self, evidence: dict, by: str) -> dict:
        if not evidence or not by:
            raise ValueError("evidence·by 필수")
        with self._lock:
            if self.state is None or not self.state.get("blocked"):
                raise RuntimeError("차단 상태 아님")
            prev = self.state["blocked"]
            self.state["history"].append({"at": _iso(self.now_fn()), "event": "block_cleared", "previous": prev,
                                          "evidence": evidence, "by": by})
            self.state["blocked"] = None
            err = self._save()
            if err:
                self.state["blocked"] = prev
                raise RuntimeError(err)
            self._audit({"event": "block_cleared", "previous": prev, "by": by})
            return {"status": "cleared", "previous": prev}

    def record_alertmanager(self, raw) -> dict:
        """반응 경로는 이 버전에서 관찰 전용 - 승격 코드 경로가 없다."""
        alerts = raw.get("alerts") if isinstance(raw, dict) else None
        rec = {"event": "alertmanager_observed", "received_at": _iso(self.now_fn()), "payload_digest": payload_digest(raw),
               "alerts": [{"status": a.get("status"), "alertname": (a.get("labels") or {}).get("alertname")}
                          for a in (alerts or []) if isinstance(a, dict)]}
        try:
            with self._admit(), self._lock:
                aerr = self._audit(rec)
        except ShuttingDown:
            return {"outcome": "shutting_down", "promotion": {"attempted": False}, "recorded": False}
        return {"outcome": "observed_only", "promotion": {"attempted": False}, "audit_error": aerr}

    def snapshot(self) -> dict:
        with self._lock:
            if self.state is None:
                return {"state_available": False, "reason": self.unavailable}
            return {"state_available": True, "mode": self.config.mode, "schema_version": SCHEMA_VERSION,
                    "context": self.state.get("context"), "blocked": self.state.get("blocked"),
                    "unresolved": gs.unresolved_actions(self.state), "actions": self.state["actions"]}
