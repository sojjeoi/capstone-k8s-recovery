#!/usr/bin/env python3
"""guarded-v1 실행기(2026-10-06, 새 파일) - 계약 초안: docs/design/phase8-v33c1-guarded-v1-pilot-contract-draft-20261006.md

기존 실행기(동결)는 수정하지 않고 조립만 한다:
  - 압박: run_if_threshold_candidate_smoke.run_no_switch_control_trial (기존 preview 준비·기준선·탐지기 준비 확인 뒤 주입·
    DEMO_STAGES 압박·안전 감시·정리). 이 함수는 activeSelector 변화를 중단 사유로 보지 않고, 탐지기가 받는 sink 주소를
    쓰지 않는 c1_live_starter와 함께 쓴다(로컬 capture sink는 떠 있지만 아무것도 받지 않는다).
  - 무주입: run_if_threshold_candidate_smoke.run_no_injection_smoke (preview 준비 + 1RPS, 주입 없음).
  - 사전검사·정리 확인: run_timed_switch_recovery_trial(run_preflight·compose_prepare·wait_chaos_gone),
    유예 원복은 guarded_cleanup_gate.finalize_override_guarded(D6) → 기존 scale_down_delay_override.restore_override,
    run_detector_switch_ab_trial(check_local_commit·check_run_guard·verify_probe_image·read_probe_pods).
새로 연결한 것: c1_live_starter(탐지기), 섀도 준비 확인(sd.first_scored_evaluation), guarded context 등록·해제,
ActionWatcher(탐지기 송신 결과에서 조치 감지 → guard_after_action 정확히 1회), act에서만 420초 유예 적용·30초 원복 확인,
기존 recovery-policy 격리 확인(old_policy_isolation - 사전검사·preview 준비 직전·안전 감시마다, 미확인이면 시작 거부·중단),
서비스 확인 연장 구간(관찰 종료 시각은 그대로 두고, 승격 서비스 확인이 끝나거나 기한에 닿을 때까지 probe·연결·안전 감시를
유지한 뒤에야 기존 정리가 진행되게 한다 - 탐지기 정지 함수 안에서 연장한다. 연장 구간은 따로 기록하고 주 지표에 넣지 않는다:
연장 중에는 probe 로컬 원자료를 새로 읽지 않아 기존 유효성 판정·주 지표 원자료가 관찰 종료까지만 담긴다).

모드: 실행 ID로 정해지고 --mode가 같아야 한다. 기본 실행 ID는 record_only(grnNN 무주입, grpNN 압박).
act(gapNN)는 --mode act + --act-approval(status=final, 리허설 값이 아닌 확정 시간 기준·시계 측정 파일·서비스 대기 기한)
+ 실제 정책 /identity의 mode=act·timing이 승인 값과 같을 때만 시작한다.
자동 재승격·롤백은 없다. 조치 결과가 검증 성공이 아니거나 서비스 확인이 정상이 아니면 탐지기를 멈추고 기존 정리 경로로 끝내며,
정책에 미해결 조치가 남으면 정리 오류로 기록해 실행 중 표시를 보존한다(다음 실행 차단 - 새 ID·새 상태 파일로도 우회 불가:
정책 원장 + 이 폴더의 실행 중 표시·정리 오류 검사).
"""
import argparse
import ctypes
import hashlib
import json
import math
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import requests

import guarded_cleanup_gate as gcg
import guarded_live_detector_runner as gr
import old_policy_isolation as opi
import run_if_threshold_candidate_smoke as smoke
import run_timed_switch_recovery_trial as rt
import ssh_check_evidence as sce
from load_ramp_adapter import IMAGE_FORCE_CLOSE, IMAGE_FORCE_CLOSE_ID
from load_ramp_adapter import RESULTS_DIR as PROBE_RESULTS_DIR
from run_once import HarnessCorrupted, TrialInvalid

sys.path.insert(0, str(smoke.ANOMALY_DETECTION_DIR))
import c1_live_detector as ld  # noqa: E402
import c1_policy_link as link  # noqa: E402
import c1_shadow_detector as sd  # noqa: E402

PLAN_ID = "capstone-guarded-v1-pilot-20261006"
CONTRACT_DOC = "docs/design/phase8-v33c1-guarded-v1-pilot-contract-draft-20261006.md"
RESULTS_DIR = Path(__file__).parent / "results" / "capstone_guarded_v1_pilot"
MODE_RO, MODE_ACT = "record_only", "act"
POLICY_VERSION = "guarded-v1"
RUNS = {**{f"grn{i:02d}": ("no_injection", MODE_RO, 700 + i) for i in range(1, 4)},
        **{f"grp{i:02d}": ("pressure", MODE_RO, 710 + i) for i in range(1, 4)},
        **{f"gap{i:02d}": ("pressure", MODE_ACT, 720 + i) for i in range(1, 4)}}
READY_TIMEOUT_SEC = 300.0           # c1 섀도 pilot과 같은 준비 대기 상한(run_c1_shadow_pilot.READY_TIMEOUT_SEC 근거)
NO_INJECTION_OBSERVATION_SEC = 600.0  # 연결·평가·기록 확인용(탐지 성능 측정 아님)
PRESSURE_OBSERVATION_SEC = 600.0      # 기존 V2 실행기와 같은 관찰(rt.OBSERVATION_SEC)
SCALE_DOWN_DELAY_SEC = 420            # 기존 V2 B군 확정값(2026-10-02 pilot04 B 적용·원복 확인)
BASE_SCALE_DOWN_DELAY_SEC = 30        # rollout.yaml 값 - 실행 전·후 같아야 한다
MIN_SERVICE_WAIT_SEC = 70.0           # 재사용 기준 settle 10 + window 60
PROMOTION_OUTCOMES = {"promoted_verified", "promoted_unverified", "promotion_unknown"}
TIMING_KEYS = ("max_signal_age_sec", "max_future_skew_sec", "max_window_to_decision_sec")
OTHER_RESULT_DIRS = ("capstone_v1_detector", "capstone_detector_switch_ab", "capstone_timed_switch_recovery",
                     "capstone_input_capture_diagnostic", "capstone_candidate_parallel_diagnostic",
                     "capstone_c1_normal_collection", "capstone_c1_shadow_pilot", "capstone_v33c1_validation")


# 전환/무전환 비교(2026-10-07 계약, 첫 결과 전 고정): abNNa = A(record_only 무전환), abNNb = B(act IF 전환), 같은 압박·요청.
# ab00 = 개발 rehearsal(비교에 넣지 않음), ab01~ab15 = 공식. 결과 폴더·plan ID를 pilot과 분리해 집계에 섞이지 않게 한다.
AB_PLAN_ID = "capstone-guarded-ab-v1-20261007"
# r2(2026-10-07, ab01 결과 전): 모든 arm canonical Healthy 시작·미승격 정리 뒤 canonical 복원. ab00a는 r1 아래 실행(소급 없음)
AB_CONTRACT_DOC = "docs/design/phase8-v33c1-guarded-ab-comparison-contract-r2-20261007.md"
AB_RESULTS_DIR = Path(__file__).parent / "results" / "capstone_guarded_ab_v1"
AB_REHEARSAL_PAIRS = ("ab00", "ab90")  # ab00 = r1 rehearsal(보존, 재사용 금지), ab90 = r2 rehearsal - 둘 다 집계 제외
RUNS.update({f"ab{p:02d}{arm}": ("pressure", mode, 800 + 2 * p + i)
             for p in (*range(16), 90) for i, (arm, mode) in enumerate((("a", MODE_RO), ("b", MODE_ACT)))})


def is_ab(run: str) -> bool:
    return run.startswith("ab")


def run_id_for(run: str) -> str:
    kind, mode, _ = RUNS[run]
    return f"guarded_{kind}_{mode}-{run}-{AB_PLAN_ID if is_ab(run) else PLAN_ID}"


def check_ab_pair_precondition(run: str, results_dir: Path) -> Optional[str]:
    """B는 같은 쌍 A가 정상 종료(정리 오류 없음)·시작 노드 기록을 남겼을 때만, A의 시작 active 노드에서만 시작한다
    (기존 V2 check_pair_precondition과 같은 원칙). A·pilot은 None. 비교 자격은 여기서 판정하지 않는다(사전 등록 분석기)."""
    if not (is_ab(run) and run.endswith("b")):
        return None
    a_path = results_dir / f"summary-{run_id_for(run[:-1] + 'a')}.json"
    if not a_path.exists():
        raise SystemExit(f"{run} 거부: 같은 쌍 A 요약 없음({a_path.name}) - A를 먼저 실행")
    a = json.loads(a_path.read_text(encoding="utf-8"))
    if a.get("outcome_status") != "completed" or a.get("cleanup_errors") or not a.get("start_active_node"):
        raise SystemExit(f"{run} 거부: A가 정상 종료가 아님 또는 시작 노드 없음 - status={a.get('outcome_status')} "
                         f"cleanup_errors={a.get('cleanup_errors')} start={a.get('start_active_node')}")
    return a["start_active_node"]


# ---------------- 정책 클라이언트 ----------------
class PolicyClient:
    def __init__(self, base_url: str, admin_token: Optional[str], *, http=requests, timeout=(3.0, 10.0)):
        self.base, self.token, self.http, self.timeout = base_url.rstrip("/"), admin_token, http, timeout

    def _req(self, method, path, **kw):
        r = getattr(self.http, method)(f"{self.base}{path}", headers={"X-Admin-Token": self.token or ""},
                                       timeout=self.timeout, allow_redirects=False, **kw)
        if r.status_code != 200:
            raise RuntimeError(f"{method.upper()} {path} -> HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def healthz(self) -> bool:
        return self._req("get", "/healthz").get("status") == "ok"

    def identity(self) -> dict:
        return self._req("get", "/identity")

    def state(self) -> dict:
        return self._req("get", "/admin/state")

    def set_context(self, ctx: dict) -> dict:
        return self._req("post", "/admin/context", json=ctx)

    def clear_context(self, run_id: str) -> dict:
        return self._req("post", "/admin/context/clear", params={"run_id": run_id})


# ---------------- act 승인 기록 ----------------
def load_act_approval(path) -> dict:
    """act 시작 조건. 리허설 값·임시 값은 여기 들어오면 거부한다. 숫자는 유한한 0 이상(서비스 대기는 70초 이상)."""
    if not path:
        raise SystemExit("act는 --act-approval(확정 시간 기준 승인 기록)이 있어야 시작한다")
    p = Path(path)
    try:
        a = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise SystemExit(f"act 승인 기록을 읽을 수 없음: {type(e).__name__}: {e}") from e
    probs = []
    if a.get("status") != "final":
        probs.append("status != final")
    if a.get("timing_source") in (None, "", "rehearsal", "provisional"):
        probs.append(f"timing_source={a.get('timing_source')!r}(확정 근거 아님)")
    cm = a.get("clock_measurement_file")
    if not cm or not (Path(cm) if Path(cm).is_absolute() else p.parent / cm).exists():
        probs.append("clock_measurement_file 없음")
    timing = a.get("timing") or {}
    for k in TIMING_KEYS:
        v = timing.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            probs.append(f"timing.{k} 잘못됨: {v!r}")
    w = a.get("service_wait_deadline_sec")
    if isinstance(w, bool) or not isinstance(w, (int, float)) or not math.isfinite(w) or w < MIN_SERVICE_WAIT_SEC:
        probs.append(f"service_wait_deadline_sec 잘못됨(≥{MIN_SERVICE_WAIT_SEC}): {w!r}")
    if not a.get("approved_by") or not a.get("approved_at_utc"):
        probs.append("approved_by/approved_at_utc 없음")
    if probs:
        raise SystemExit(f"act 승인 기록 거부: {probs}")
    return a


def check_policy(policy, mode: str, approval: Optional[dict]) -> dict:
    """시작 전 정책 확인(읽기 전용). 문제가 있으면 problems가 비지 않는다."""
    probs = []
    try:
        ident = policy.identity()
        st = policy.state()
    except Exception as e:  # noqa: BLE001
        return {"problems": [f"policy_unreachable: {type(e).__name__}: {e}"]}
    if ident.get("role") != link.GUARDED_ROLE or ident.get("version") != POLICY_VERSION:
        probs.append(f"identity_mismatch: {ident.get('role')}/{ident.get('version')}")
    if ident.get("mode") != mode:
        probs.append(f"policy_mode={ident.get('mode')} != {mode}")
    if mode == MODE_ACT and approval is not None and ident.get("timing") != {k: approval["timing"][k] for k in TIMING_KEYS}:
        probs.append(f"policy_timing {ident.get('timing')} != approved {approval['timing']}")
    if mode == MODE_RO and ident.get("actions_enabled"):
        probs.append("record_only인데 actions_enabled")
    if not st.get("state_available"):
        probs.append(f"policy_state_unavailable: {st.get('reason')}")
    if st.get("blocked"):
        probs.append(f"policy_blocked: {st['blocked']}")
    if st.get("unresolved"):
        probs.append(f"policy_unresolved_actions: {sorted(st['unresolved'])}")
    if st.get("context") is not None:
        probs.append(f"policy_context_not_cleared: {st['context'].get('run_id')}")
    return {"identity": ident, "state": {k: st.get(k) for k in ("state_available", "mode", "blocked", "unresolved", "context")},
            "problems": probs}


def build_context(run_id: str, rollout: dict, mode: str, art: dict) -> dict:
    hashes = art["hashes"]
    return {"run_id": run_id, "detector_id": ld.DETECTOR_ID, "model_version": link.MODEL_VERSION,
            "model_hash": hashes["model.pkl"], "artifact_manifest_hash": link.artifact_manifest_hash(hashes),
            "threshold": art["threshold"], "rollout": rollout, "actions_allowed": mode == MODE_ACT}


# ---------------- 조치 감지 ----------------
class ActionWatcher:
    """탐지기 evidence의 c1_policy_send_result로 조치를 감지한다. 안전 감시 함수 안에서 매번 불린다.
    - record_only에서 승격 결과가 보이면 하네스 이상(중단).
    - act: 첫 승격 결과가 promoted_verified가 아니면 즉시 중단(재승격·롤백 없음). 검증 성공이면 정책 상태에서 그 target_key의
      조치가 이 run_id인지 확인한 뒤 guard_after_action을 **정확히 1회** 시작한다(별도 스레드 - 관찰 루프·probe 갱신 유지).
    - 같은 target_key의 중복 응답은 세기만 하고 다시 기다리지 않는다. 다른 target_key는 조치 1회 원칙 위반(중단).
    - guard 결과가 continue가 아니면(실패·불명·차단·기한 초과) 중단 - guard가 이미 탐지기를 멈췄고 정리는 기존 경로."""

    def __init__(self, evidence_path: Path, *, mode: str, run_id: str, get_state: Callable, deadline_sec: float,
                 thread_runner: Callable, guard_fn: Callable = gr.guard_after_action, wait_kw: Optional[dict] = None):
        self.path, self.mode, self.run_id, self.get_state = Path(evidence_path), mode, run_id, get_state
        self.deadline_sec, self.thread_runner, self.guard_fn, self.wait_kw = deadline_sec, thread_runner, guard_fn, wait_kw or {}
        self.handle = None
        self.lines_seen, self.action, self.guard_starts, self.duplicates = 0, None, 0, 0
        self.guard_result, self.thread, self.events, self.late_failures = None, None, [], []

    def _new_send_results(self) -> list:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").split("\n")[:-1]  # 완결된 줄만
        new, self.lines_seen = lines[self.lines_seen:], len(lines)
        out = []
        for ln in new:
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
            except ValueError as e:
                raise HarnessCorrupted("탐지기 기록 줄 파싱 실패 - 조치 감지 불가") from e
            if r.get("record_type") == "c1_policy_send_result":
                out.append(r)
        return out

    def pending(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def poll_quiet(self, source: str) -> None:
        """정리 단계용(예외를 던지지 않음): 마지막 안전검사 이후 도착한 송신 결과를 읽어 조치를 감지한다."""
        try:
            self.check(raise_on_guard=False, source=source)
        except Exception as e:  # noqa: BLE001
            self.late_failures.append(f"{source}: {type(e).__name__}: {e}")

    def adopt_from_policy_state(self) -> None:
        """탐지기 정지 뒤: 응답이 기록되지 못한 조치(송신 중 정지 등)를 정책 상태에서 찾는다. 예외를 던지지 않는다."""
        try:
            st = self.get_state()
        except Exception as e:  # noqa: BLE001 - 늦은 조치가 없는지 확인 못 함
            self.late_failures.append(f"policy_state_after_detector_stop: {type(e).__name__}: {e}")
            return
        for tk, a in sorted((st.get("actions") or {}).items()):
            status = a.get("status") or ""
            if a.get("run_id") != self.run_id or status.startswith("aborted"):
                continue
            if self.action is not None:
                if tk != self.action["target_key"]:
                    self.late_failures.append(f"두 번째 조치 대상(정책 상태): {tk}")
                continue
            self.action = {"target_key": tk, "outcome": status, "source": "policy_state_after_detector_stop"}
            self.events.append({"effective": status, "target_key": tk, "source": "policy_state_after_detector_stop",
                                "seen_at_utc": datetime.now(timezone.utc).isoformat()})
            if self.mode != MODE_ACT:
                self.late_failures.append(f"record_only인데 정책 상태에 조치({status})")
            elif status == "verified_service_pending":
                self.guard_starts += 1
                self.thread = self.thread_runner(self._guard, tk)
            elif status != "verified":
                self.late_failures.append(f"늦게 발견된 조치가 검증 성공이 아님({status}) - 재승격·롤백 없음")

    def check(self, raise_on_guard: bool = True, source: str = "safety_check") -> None:
        for r in self._new_send_results():
            out = r.get("policy_outcome")
            eff = r.get("original_outcome") if out == "duplicate_delivery" else out
            if eff not in PROMOTION_OUTCOMES:
                continue
            tk = r.get("target_key")
            self.events.append({"evaluation_seq": r.get("evaluation_seq"), "policy_outcome": out, "effective": eff,
                                "target_key": tk, "source": source, "seen_at_utc": datetime.now(timezone.utc).isoformat()})
            if self.mode != MODE_ACT:
                raise HarnessCorrupted(f"record_only인데 승격 결과 수신({eff}) - 정책 모드 이상")
            if self.action is not None:
                if tk != self.action["target_key"]:
                    raise HarnessCorrupted(f"두 번째 조치 대상 감지({tk}) - 실행당 조치 1회 원칙 위반")
                self.duplicates += 1  # 같은 대상 - 서비스 대기·승격 반복 없음
                continue
            self.action = {"target_key": tk, "outcome": eff, "evaluation_seq": r.get("evaluation_seq")}
            if eff != "promoted_verified":
                raise TrialInvalid(f"승격 결과가 검증 성공이 아님({eff}) - 재승격·롤백 없이 중단, 사람 확인 필요")
            if not tk:
                raise TrialInvalid("승격 확인 응답에 target_key 없음 - 서비스 확인 연결 불가")
            try:
                act = (self.get_state().get("actions") or {}).get(tk)
            except Exception as e:  # noqa: BLE001
                raise TrialInvalid(f"승격 뒤 정책 상태 조회 실패 - 서비스 확인 연결 불가: {type(e).__name__}: {e}") from e
            if not act or act.get("run_id") != self.run_id:
                raise HarnessCorrupted(f"정책 상태의 조치와 target_key·run_id 불일치: {tk} → {act and act.get('run_id')}")
            self.guard_starts += 1
            self.thread = self.thread_runner(self._guard, tk)
        r = self.guard_result
        if raise_on_guard and r is not None and r.get("runner_action") != "continue":
            raise TrialInvalid(f"승격 뒤 서비스 확인이 정상 아님({r.get('result')}) - 탐지기 정지, 기존 정리 경로")

    def _guard(self, tk) -> None:
        try:
            self.guard_result = self.guard_fn(self.handle, self.get_state, tk, self.deadline_sec, **self.wait_kw)
        except Exception as e:  # noqa: BLE001 - 대기 자체 오류도 정상 아님
            try:
                stop = self.handle.stop()
            except Exception as e2:  # noqa: BLE001
                stop = {"stop_error": f"{type(e2).__name__}: {e2}"}
            self.guard_result = {"result": "guard_error", "error": f"{type(e).__name__}: {e}", "service_ok": None,
                                 "runner_action": "stopped", "detector_stop": stop}

    def finish(self, timeout_sec: float) -> None:
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout_sec)

    def record(self) -> dict:
        return {"action": self.action, "guard_starts": self.guard_starts, "duplicates": self.duplicates,
                "guard_result": self.guard_result, "guard_pending_at_end": self.pending(),
                "promotion_events": self.events, "late_failures": self.late_failures}


def _thread_runner(fn, *args):
    t = threading.Thread(target=fn, args=args, daemon=True)
    t.start()
    return t


# ---------------- 보조 프로세스(포트포워드·keep_awake) ----------------
def _pid_alive(pid) -> bool:
    if os.name == "nt":  # os.kill(pid, 0)은 Windows에서 CTRL_C_EVENT라 쓰지 않는다
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def _http_ok(url: str) -> bool:
    try:
        return requests.get(url, timeout=5, allow_redirects=False).status_code == 200
    except requests.RequestException:
        return False


def check_session(session_file, *, pid_alive=_pid_alive, http_ok=_http_ok) -> dict:
    """guarded_pilot_session.ps1이 남긴 세션 기록으로 keep_awake·포트포워드 생존과 엔드포인트 도달을 확인한다."""
    if not session_file:
        return {"ok": False, "problems": ["session_file 없음 - guarded_pilot_session.ps1로 시작해야 함"]}
    try:
        s = json.loads(Path(session_file).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        return {"ok": False, "problems": [f"session_file 읽기 실패: {type(e).__name__}: {e}"]}
    probs = [f"{k}_dead({s.get(k)})" for k in ("keep_awake", "pf_prometheus", "pf_policy") if not s.get(k) or not pid_alive(s[k])]
    for name, url in (("prometheus", f"{s.get('prometheus_url', '')}/-/healthy"), ("policy", f"{s.get('policy_url', '')}/healthz")):
        if not http_ok(url):
            probs.append(f"{name}_unreachable({url})")
    return {"ok": not probs, "problems": probs, "checked_at_utc": datetime.now(timezone.utc).isoformat(), "session": s}


# ---------------- 의존성(테스트에서 가짜로 바꾼다) ----------------
@dataclass
class Deps:
    policy: object
    check_unresolved: Callable[[], None]
    preflight: Callable[..., dict]  # (pol, disk_check_fn=...) - 디스크 점검은 근거 보존 계측 연결로
    session_check: Callable[[], dict]
    rollout_identity: Callable[[], dict]
    read_delay: Callable[[], Optional[int]]
    make_injector: Callable[..., object]  # (run_id, rep, kind, cleanup_preview_fn) - preview 정리는 D5 관문을 거친다
    make_prober: Callable[[str, int, str, float], object]
    base_safety: Callable[[object, str], Callable]
    run_guard_check: Callable
    probe_image_check: Callable
    trial_pressure: Callable
    trial_no_injection: Callable
    apply_override: Callable[[int, Path], dict]
    wait_chaos_gone: Callable[[list], list]
    read_probe_pods: Callable[[], list]
    capture_events: Callable[[str], list]
    old_policy_check: Callable[[], dict]  # opi.assess 결과 {"isolated": bool, "reasons": [...]}
    starter_factory: Callable = gr.c1_live_starter
    thread_runner: Callable = _thread_runner
    sleep_fn: Callable = time.sleep
    monotonic_fn: Callable = time.monotonic
    pick_port: Callable[[], int] = smoke._pick_free_loopback_port
    artifacts: Callable[[], dict] = field(default=lambda: sd.load_artifacts(sd.DEFAULT_ARTIFACTS))
    # prober가 실제로 쓰는 원자료 경로(기존 실행기와 같은 규칙) - 관찰 종료 뒤 공백 진단에 쓴다
    probe_csv_path: Callable = field(default=lambda run_id, rep: PROBE_RESULTS_DIR / f"probe-{run_id}-{rt.ARM_LABEL}-{rep}-raw.csv")
    # SSH 디스크 점검 근거 보존(guarded 경로 전용). 판정은 기존 함수 그대로 - 테스트는 가짜 ssh 접두어로 바꾼다
    make_ssh_evidence: Callable[[Path], object] = sce.SshCheckEvidence
    run_guard_nodes: Callable[[], list] = field(default=lambda: _watch_nodes())
    preflight_nodes: Callable[[], list] = field(default=lambda: sce.g.list_cluster_nodes())
    # D5·D6(guarded 경로 전용, 읽기 + 기존 원복 함수): 승격 여부 근거와 확인된 정상 전환의 원복 대기
    rollout_view: Callable[[], dict] = gcg.read_view
    list_rs: Callable[[], dict] = gcg.list_rs
    ledger_read: Callable[[], list] = gcg.read_ledger
    restore_override: Callable = field(default=lambda *a, **k: _sdo().restore_override(*a, **k))
    patch_delay: Callable = field(default=lambda v: _sdo().patch_delay(v))  # D6 guarded_patch가 직전 재확인 뒤 부른다
    cleanup_unpromoted_preview: Callable = field(default=lambda *a: _bgp().cleanup_unpromoted_preview(*a))
    now_utc: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    # 압박 전달 근거(기록 전용, 판정은 사전 등록 분석기): 주입 대상 Pod CPU 원시 카운터 - V2와 같은 조회·구간
    cpu_raw_counter: Callable[[str, datetime], list] = field(default=lambda pod, t0: _cpu_raw_counter(pod, t0))
    # A/B 비교(계약 r2): canonical 상태 읽기(검사만)와 미승격 abort 뒤 검증된 annotation 복원
    canonical_snapshot: Callable[[], dict] = gcg.read_snapshot
    canonical_restore: Callable[[dict, Optional[tuple]], dict] = field(default=lambda prep, pod: gcg.restore_canonical(
        prep, pod, snapshot_fn=lambda: gcg.read_snapshot(residuals=False), rs_template_fn=gcg.rs_template,
        patch_prep_ts_fn=gcg.patch_prep_ts))


def _sdo():
    import scale_down_delay_override as sdo
    return sdo


def _bgp():
    import blue_green_prep
    return blue_green_prep


def _cpu_raw_counter(pod: str, t0: datetime) -> list:  # pragma: no cover - 라이브 전용(run_detector_switch_ab_trial과 같은 구간)
    import run_detector_switch_ab_trial as ab
    return ab.prom_raw_counter(ab._cpu_counter_selector(pod), t0 - timedelta(seconds=60),
                               t0 + timedelta(seconds=rt.PLANNED_STRESS_SEC + 120))


def _watch_nodes() -> list:  # ab.check_run_guard 기본값과 같은 노드 목록(무거운 모듈이라 지연 import)
    import run_detector_switch_ab_trial as ab
    return list(ab.WATCH_NODES)


def real_deps(policy_url: str, admin_token: Optional[str], session_file) -> Deps:  # pragma: no cover - 라이브 전용 조립
    import arm_controller
    import node_resource_safety_gate as nrsg
    import run_detector_switch_ab_trial as ab
    import run_v1_detector_trial as v1
    import scale_down_delay_override as sdo
    from cpu_pressure_demo_adapter import make_cpu_pressure_injector
    from load_ramp_adapter import IMAGE_FORCE_CLOSE, make_load_ramp_prober
    from run_cpu_demo_trial import DEMO_STAGES
    from run_if_threshold_no_injection_long_trial import PROBE_LIFETIME_MARGIN_SEC

    policy = PolicyClient(policy_url, admin_token)

    def check_unresolved():
        v1.check_no_unresolved_anywhere()
        for d in OTHER_RESULT_DIRS:
            rt.check_no_unresolved_failures(RESULTS_DIR.parent / d)
        rt.check_no_unresolved_failures(RESULTS_DIR)
        rt.check_no_unresolved_failures(AB_RESULTS_DIR)  # pilot·비교 어느 쪽 정리 실패든 양쪽 다음 실행을 막는다

    def preflight(pol, disk_check_fn):
        # 기존 정책(8080) 대신 guarded 정책 상태로 대체, 디스크 점검은 같은 판정의 근거 보존 계측 연결
        out = rt.run_preflight(policy_healthy_fn=pol.healthz, disk_check_fn=disk_check_fn)
        out["local_commit"] = ab.check_local_commit()
        if not out["local_commit"]["ok"]:
            out["ok"] = False
            out.setdefault("reasons", []).append(out["local_commit"]["reason"])
        return out

    def make_injector(run_id, rep, kind, cleanup_preview_fn):
        if kind == "no_injection":  # inject()는 부르지 않는다(run_v1_detector_trial.run_observation과 같은 조립)
            inj = make_cpu_pressure_injector(run_id, ab.ARM_LABEL, rep, stages=DEMO_STAGES)
            inj = arm_controller.wrap_injector_with_preview_prep(inj, ab.ARM_LABEL, cleanup_preview_fn=cleanup_preview_fn)
            inj = nrsg.wrap_injector_with_no_injection_diagnostic_exception(
                inj, ab.ARM_LABEL, nrsg.NO_INJECTION_DIAGNOSTIC_EXPECTED_RUN_MODE)
            inj.get_real_promotion_diagnostic_exception_info = inj.get_no_injection_diagnostic_exception_info
            return inj
        inj = make_cpu_pressure_injector(run_id, ab.ARM_LABEL, rep, stages=DEMO_STAGES, continue_after_target_replacement=True)
        inj = arm_controller.wrap_injector_with_preview_prep(inj, ab.ARM_LABEL, cleanup_preview_fn=cleanup_preview_fn)
        return nrsg.wrap_injector_with_real_promotion_diagnostic_exception(
            inj, ab.ARM_LABEL, nrsg.REAL_PROMOTION_DIAGNOSTIC_EXPECTED_RUN_MODE)

    def make_prober(run_id, rep, kind, lifetime_sec):
        margin = PROBE_LIFETIME_MARGIN_SEC if kind == "no_injection" else smoke.BASELINE_TIMEOUT_SEC + smoke.PROBE_INJECTION_START_MARGIN_SEC
        return make_load_ramp_prober(smoke.DEFAULT_PROBE_CONFIG, run_id, f"guarded_{kind}", ab.ARM_LABEL, rep,
                                     lifetime_sec + margin, image=IMAGE_FORCE_CLOSE)

    def base_safety(injector, kind):
        return smoke._check_safety_no_injection if kind == "no_injection" else rt.make_safety_fn(injector)

    return Deps(
        policy=policy, check_unresolved=check_unresolved, preflight=preflight,
        session_check=lambda: check_session(session_file), rollout_identity=lambda: {
            "namespace": sdo.NAMESPACE, "name": sdo.ROLLOUT_NAME, "uid": sdo.get_rollout()["metadata"]["uid"]},
        read_delay=lambda: rt._read_rollout_snapshot()["delay"], make_injector=make_injector, make_prober=make_prober,
        base_safety=base_safety, run_guard_check=ab.check_run_guard, probe_image_check=ab.verify_probe_image,
        trial_pressure=smoke.run_no_switch_control_trial, trial_no_injection=smoke.run_no_injection_smoke,
        apply_override=sdo.apply_override,
        wait_chaos_gone=rt.wait_chaos_gone, read_probe_pods=ab.read_probe_pods,
        capture_events=lambda since: smoke._capture_recent_pod_events(since_utc=since),
        old_policy_check=lambda: opi.assess(opi.read_state(**opi._clients())))


# ---------------- 실행 ----------------
def evidence_counts(path: Path) -> dict:
    c = {"scored": 0, "skipped": 0, "condition_met": 0, "send_attempts": 0, "send_results": {}, "parse_errors": 0}
    if not path.exists():
        return c
    for ln in path.read_text(encoding="utf-8").split("\n")[:-1]:
        try:
            r = json.loads(ln) if ln.strip() else {}
        except ValueError:
            c["parse_errors"] += 1
            continue
        t = r.get("record_type")
        if t == "c1_evaluation":
            c["scored" if r.get("status") == "scored" else "skipped"] += 1
            c["condition_met"] += bool(r.get("condition_met"))
        elif t == "c1_policy_send_attempt":
            c["send_attempts"] += 1
        elif t == "c1_policy_send_result":
            k = r.get("policy_outcome") or r.get("delivery")
            c["send_results"][k] = c["send_results"].get(k, 0) + 1
    return c


def run_guarded(run: str, *, mode: str, deps: Deps, policy_url: str, approval_path=None, preflight_only=False,
                dry_run=False, results_dir: Optional[Path] = None) -> dict:
    if run not in RUNS:
        raise SystemExit(f"알 수 없는 실행 ID: {run}")
    kind, run_mode, rep = RUNS[run]
    if mode != run_mode:
        raise SystemExit(f"--mode {mode}가 실행 ID {run}의 모드({run_mode})와 다름 - 시작 안 함")
    run_id = run_id_for(run)
    results_dir = results_dir or (AB_RESULTS_DIR if is_ab(run) else RESULTS_DIR)
    results_dir.mkdir(parents=True, exist_ok=True)
    summary_path = results_dir / f"summary-{run_id}.json"
    if summary_path.exists():
        raise SystemExit(f"이미 결과가 있음 - 덮어쓰지 않음: {summary_path}")
    # 실행 ID 재사용 금지(2026-10-06): 요약을 보관 폴더로 옮긴 뒤에도 탐지기 원자료가 남아 있으면 같은 ID로 다시 시작하지 않는다
    # (사전검사·계약 파일 덮어쓰기 방지). 사전검사만 한 뒤의 본 실행은 탐지기 기록이 없으므로 그대로 허용된다.
    used = gr.live_evidence_path(results_dir, run_id)
    if used.exists():
        raise SystemExit(f"실행 ID 재사용 금지 - 이미 실행된 ID(탐지기 원자료 있음): {used.name}")
    expected_start_node = check_ab_pair_precondition(run, results_dir)
    deps.check_unresolved()
    approval = load_act_approval(approval_path) if mode == MODE_ACT else None
    if is_ab(run) and approval is not None and run not in (approval.get("runs") or []):
        raise SystemExit(f"act 승인 범위 밖 - {run}이 승인 기록 runs에 없음: {approval.get('runs')}")
    art = deps.artifacts()
    obs_sec = NO_INJECTION_OBSERVATION_SEC if kind == "no_injection" else PRESSURE_OBSERVATION_SEC
    comparison = None
    if is_ab(run):  # 분석기는 이 기록과 실행 ID 규칙(ab01~ab15)으로만 공식 쌍을 고른다 - ab00은 어떤 경우에도 제외
        comparison = {"series": AB_PLAN_ID, "pair": run[:-1], "arm": "A_no_switch" if run.endswith("a") else "B_if_switch",
                      "counted_in_comparison": run[:-1] not in AB_REHEARSAL_PAIRS,
                      "expected_start_active_node": expected_start_node}
    contract = {"run_id": run_id, "run": run, "plan_id": AB_PLAN_ID if is_ab(run) else PLAN_ID,
                "contract_doc": AB_CONTRACT_DOC if is_ab(run) else CONTRACT_DOC, "comparison": comparison,
                "conditions": {"stress_stages": rt.DEMO_STAGES, "probe_config": str(smoke.DEFAULT_PROBE_CONFIG),
                               "probe_config_sha256": hashlib.sha256(smoke.DEFAULT_PROBE_CONFIG.read_bytes()).hexdigest(),
                               "probe_image": IMAGE_FORCE_CLOSE, "probe_image_id": IMAGE_FORCE_CLOSE_ID,
                               "detector_rule": {"consecutive": sd.CONSECUTIVE_THRESHOLD, "cooldown_sec": sd.COOLDOWN_SEC},
                               "start_state_rule": "canonical_healthy_r2" if is_ab(run) else None},
                "kind": kind, "mode": mode,
                "detector": ld.DETECTOR_ID, "model": link.MODEL_VERSION, "threshold": art["threshold"],
                "artifact_hashes": art["hashes"], "observation_sec": obs_sec, "ready_timeout_sec": READY_TIMEOUT_SEC,
                "scale_down_delay": {"act": SCALE_DOWN_DELAY_SEC, "base": BASE_SCALE_DOWN_DELAY_SEC,
                                     "changed": mode == MODE_ACT},
                "act_approval": approval,
                "timing_note": None if approval else "record_only - 정책 시간 값은 리허설 기록용(확정값 아님)",
                "service_check_note": "짧은 전환 직후 트래픽 확인 - 사용자 품질 개선 입증 아님",
                "no_auto_repromotion_or_rollback": True}
    if dry_run:
        (results_dir / f"contract-{run_id}.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"dry_run": True, "run_id": run_id}

    pol = deps.policy
    policy_check = check_policy(pol, mode, approval)
    session_start = deps.session_check()
    ssh_ev = deps.make_ssh_evidence(results_dir / f"ssh-evidence-{run_id}")
    run_guard_disk_fn = ssh_ev.disk_fn(deps.run_guard_nodes)
    preflight = deps.preflight(pol, disk_check_fn=ssh_ev.disk_fn(deps.preflight_nodes))
    try:
        delay_before = deps.read_delay()
    except Exception as e:  # noqa: BLE001
        delay_before = f"read_error: {type(e).__name__}: {e}"
    try:
        old_policy = deps.old_policy_check()
    except Exception as e:  # noqa: BLE001
        old_policy = {"isolated": False, "reasons": [f"check_error: {type(e).__name__}: {e}"]}
    reasons = list(preflight.get("reasons") or []) + policy_check["problems"] + list(session_start.get("problems") or [])
    if old_policy.get("isolated") is not True:  # 기존 승격 경로가 열려 있으면 preview를 만들기 전에 거부
        reasons.append(f"old_recovery_policy_not_isolated: {old_policy.get('reasons')}")
    if delay_before != BASE_SCALE_DOWN_DELAY_SEC:
        reasons.append(f"scaleDownDelaySeconds={delay_before} != {BASE_SCALE_DOWN_DELAY_SEC}")
    if is_ab(run):  # 계약 r2: 모든 arm은 canonical Healthy에서만 시작(검사만 - 고치지 않고 시작 거부)
        try:
            snap = deps.canonical_snapshot()
            cp = gcg.check_canonical(snap, delay=BASE_SCALE_DOWN_DELAY_SEC)
        except Exception as e:  # noqa: BLE001
            snap, cp = None, [f"snapshot_error: {type(e).__name__}: {e}"]
        p0 = gcg.active_pod(snap or {})
        start_pod = (p0["name"], p0["uid"]) if p0 else None
        preflight["canonical_start"] = {"problems": cp, "start_pod": start_pod, "state": {k: (snap or {}).get(k) for k in (
            "phase", "abort", "active", "stable", "current", "preview", "delay", "pods", "svc_selectors", "endpoints", "rs")}}
        reasons += [f"canonical_start: {x}" for x in cp]
    preflight.update(policy=policy_check, session=session_start, scale_down_delay_before=delay_before, old_policy=old_policy,
                     ok=not reasons, reasons=reasons, ssh_check_evidence=ssh_ev.summary())
    # 2026-10-07 ab90b 뒤: 단계(preflight-only/run)·시각별 새 파일로만 쓴다("x") - 같은 ID의 앞 사전검사 기록을 덮어쓰지 않는다.
    # 공식 실행의 사전검사 파일은 요약의 preflight_file로 찾는다(옛 실행의 preflight-<run_id>.json은 그대로 둔다).
    preflight_file = results_dir / (f"preflight-{run_id}-{'preflight-only' if preflight_only else 'run'}-"
                                    f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.json")
    with open(preflight_file, "x", encoding="utf-8") as fh:
        fh.write(json.dumps(preflight, ensure_ascii=False, indent=2, default=str))
    if reasons:
        raise SystemExit(f"사전검사 실패 - 시작 안 함(클러스터 변경 없음): {reasons}")
    if preflight_only:
        return {"preflight_only": True, "run_id": run_id, "preflight": preflight, "preflight_file": preflight_file.name}
    (results_dir / f"contract-{run_id}.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")

    events_since = datetime.now(timezone.utc).isoformat()
    running_marker = results_dir / f"running-{run_id}.json"
    running_marker.write_text(json.dumps({"run_id": run_id, "started_at": events_since}), encoding="utf-8")
    evidence_path = gr.live_evidence_path(results_dir, run_id)
    state, cleanup_errors, probe_image_check = {}, [], {}
    run_guard = {"mem_drop_abort_gib": 1.0, "disk_recheck_sec": 60.0}
    override_record_path = results_dir / f"scale-down-delay-override-{run_id}.json"
    if mode == MODE_ACT:
        state["override_record_path"] = override_record_path
    watcher = ActionWatcher(evidence_path, mode=mode, run_id=run_id, get_state=pol.state,
                            deadline_sec=approval["service_wait_deadline_sec"] if approval else MIN_SERVICE_WAIT_SEC,
                            thread_runner=deps.thread_runner,
                            wait_kw={"monotonic": deps.monotonic_fn, "sleep": deps.sleep_fn})
    outcome, trial_error, context, injector, canonical_end = None, None, None, None, None

    def assess_now(prep_info=None):  # D5·D6 - 정책 상태·원장·탐지기 기록·실제 Rollout을 함께 본다(예외 없음)
        prep_info = prep_info or gate.prep_info
        old_active = (prep_info or {}).get("pre_prepare_active_selector") or             ((state.get("override") or {}).get("before") or {}).get("active_selector")
        return gcg.assess(run_id=run_id, mode=mode, policy_state_fn=pol.state, ledger_fn=deps.ledger_read,
                          evidence_path=evidence_path, view_fn=deps.rollout_view,
                          rollout_uid=((context or {}).get("rollout") or {}).get("uid"), old_active=old_active,
                          prep_info=prep_info, detector_expected=watcher.handle is not None)
    # 계약 r2: A/B는 미승격 abort 뒤 canonical 복원(승격된 B는 D5가 정리를 건너뛰므로 여기로 오지 않고 D6로 끝난다)
    gate = gcg.PreviewCleanupGate(assess_now, deps.cleanup_unpromoted_preview,
                                  restore_fn=(lambda prep: deps.canonical_restore(prep, start_pod)) if is_ab(run) else None)
    extensions, safety_args, old_policy_checks = [], {}, {"n": 0, "last": None}

    def check_old_policy():
        try:
            r = deps.old_policy_check()
        except Exception as e:  # noqa: BLE001
            r = {"isolated": False, "reasons": [f"check_error: {type(e).__name__}: {e}"]}
        old_policy_checks["n"] += 1
        old_policy_checks["last"] = {k: r.get(k) for k in ("isolated", "reasons")}
        if r.get("isolated") is not True:
            raise HarnessCorrupted(f"기존 recovery-policy 승격 경로가 열림 - 중단: {r.get('reasons')}")

    def _terminate(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")
    old_handlers = ({getattr(signal, n): signal.signal(getattr(signal, n), _terminate)
                     for n in ("SIGTERM", "SIGBREAK") if hasattr(signal, n)}
                    if threading.current_thread() is threading.main_thread() else {})
    try:
        context = build_context(run_id, deps.rollout_identity(), mode, art)
        pol.set_context(context)
        injector = deps.make_injector(run_id, rep, kind, gate.cleanup)
        base_prepare = injector.prepare

        def prepare_isolated():  # preview를 만들기 직전에 기존 정책 격리를 다시 확인한다
            check_old_policy()
            base_prepare()
        injector.prepare = prepare_isolated
        # act에서만 preview 준비가 끝난 뒤·주입 전에 420초 유예를 적용한다. record_only는 arm=control - 유예 변경 경로 없음
        # 비교 B는 같은 쌍 A의 시작 active 노드여야 한다(기존 compose_prepare가 preview 준비 뒤·주입 전에 강제)
        rt.compose_prepare(injector, arm="switch" if mode == MODE_ACT else "control", expected_start_node=expected_start_node,
                           state=state,
                           apply_override_fn=(lambda: deps.apply_override(SCALE_DOWN_DELAY_SEC, override_record_path))
                           if mode == MODE_ACT else (lambda: None))
        prober = deps.make_prober(run_id, rep, kind, READY_TIMEOUT_SEC + obs_sec)
        base = deps.base_safety(injector, kind)

        def safety(baseline_state, prober_):
            safety_args.update(baseline_state=baseline_state, prober=prober_)
            base(baseline_state, prober_)
            deps.run_guard_check(run_guard, injector, state, disk_fn=run_guard_disk_fn)
            deps.probe_image_check(probe_image_check)
            check_old_policy()
            watcher.check()

        def extend_for_service_check(phase):
            """관찰 종료 뒤 서비스 확인이 진행 중이면 끝나거나 기한에 닿을 때까지 probe·연결·안전 감시를 유지한다.
            probe 원자료는 새로 읽지 않는다(주 지표·기존 유효성 판정은 관찰 종료까지만). 예외를 던지지 않는다."""
            if not watcher.pending():
                return
            rec = {"phase": phase, "started_at_utc": datetime.now(timezone.utc).isoformat(),
                   "started_mono": deps.monotonic_fn(), "target_key": (watcher.action or {}).get("target_key"),
                   "safety_checks": 0, "safety_errors": [], "probe_alive": [], "counted_in_main_metrics": False}
            last = None
            while watcher.pending():
                t = deps.monotonic_fn()
                if last is None or t - last >= smoke.SAFETY_POLL_INTERVAL_SEC:
                    last = t
                    try:
                        if safety_args:
                            base(safety_args["baseline_state"], safety_args["prober"])
                        deps.run_guard_check(run_guard, injector, state, disk_fn=run_guard_disk_fn)
                        check_old_policy()
                        rec["probe_alive"].append(bool(prober.is_alive()))
                        rec["safety_checks"] += 1
                    except Exception as e:  # noqa: BLE001 - 연장 중 안전 문제: 연장을 멈추고 정리로 간다
                        rec["safety_errors"].append(f"{type(e).__name__}: {e}")
                        break
                deps.sleep_fn(1.0)
            rec.update(ended_at_utc=datetime.now(timezone.utc).isoformat(), ended_mono=deps.monotonic_fn(),
                       guard_pending_after=watcher.pending(), guard_result=watcher.guard_result)
            extensions.append(rec)

        start_live = deps.starter_factory(results_dir, policy_url, POLICY_VERSION, mode)

        def detector_start(rid, path, sink_url):
            real = start_live(rid, path, sink_url)
            watcher.handle = real

            def stop_after_service_check():
                # 기존 정리의 첫 단계(관찰 종료 시각 기록 직후, probe 정지 전). 연장이 있어도 실제 정지는 반드시 한다.
                try:
                    watcher.poll_quiet("after_last_safety_check")
                    extend_for_service_check("before_detector_stop")
                finally:
                    r = real.stop()
                watcher.poll_quiet("after_detector_stop")
                watcher.adopt_from_policy_state()
                extend_for_service_check("after_detector_stop")
                return r
            return smoke.DetectorHandle(is_alive=real.is_alive, stop=stop_after_service_check, label=real.label)
        common = dict(check_safety_fn=safety, detector_start_fn=detector_start, sleep_fn=deps.sleep_fn,
                      monotonic_fn=deps.monotonic_fn, detector_ready_timeout_sec=READY_TIMEOUT_SEC,
                      detector_ready_fn=lambda: sd.first_scored_evaluation(evidence_path))
        probe_csv = deps.probe_csv_path(run_id, rep)
        sink_out = results_dir / f"sink-captured-{run_id}.jsonl"
        if kind == "pressure":
            outcome = deps.trial_pressure(run_id, injector, prober, deps.pick_port(), evidence_path, sink_out, probe_csv,
                                          post_injection_observation_sec=obs_sec, **common)
        else:
            outcome = deps.trial_no_injection(run_id, injector, prober, deps.pick_port(), evidence_path, sink_out,
                                              probe_raw_csv_path=probe_csv, observation_sec=obs_sec, **common)
    except BaseException as e:  # noqa: BLE001 - 하네스 오류·중단 신호도 요약·정리 확인을 남긴다
        trial_error = f"{type(e).__name__}: {e}"
        outcome = {"status": "harness_error", "reason": trial_error, "cleanup_status": "unknown"}
    finally:
        watcher.finish(timeout_sec=watcher.deadline_sec + 30)
        policy_after = None
        try:
            policy_after = pol.state()
        except Exception as e:  # noqa: BLE001
            cleanup_errors.append(f"policy_state_after_failed: {type(e).__name__}: {e}")
        if context is not None:
            try:
                pol.clear_context(run_id)
            except Exception as e:  # noqa: BLE001
                cleanup_errors.append(f"policy_context_clear: {type(e).__name__}: {e}")
        restore, final_assessment = None, None
        if gate.held:  # D5: preview 정리 보류 - 남은 자원과 사유를 남기고 다음 실행을 막는다
            cleanup_errors.append(f"preview_cleanup_held: reasons={gate.held['reasons']} "
                                  f"attempt_evidence={gate.held['attempt_evidence']} remaining={gate.held['remaining']}")
        if mode == MODE_ACT:
            def wait_safety():  # D6 원복 대기 중 안전 감시(격리·노드/디스크). 정책 상태·대상 조치는 finalize가 매번 확인
                check_old_policy()
                deps.run_guard_check(run_guard, injector, state, disk_fn=run_guard_disk_fn)
            # 실행기 쪽 서비스 확인: guard 결과 verified, 또는 탐지기 응답 유실로 정책 상태에서 이미 verified로 채택(guard 없음)
            adopted = (watcher.action or {}).get("source") == "policy_state_after_detector_stop"
            runner_guard_ok = (watcher.guard_result or {}).get("result") == "verified" or (
                adopted and (watcher.action or {}).get("outcome") == "verified" and watcher.guard_starts == 0)
            final_assessment = assess_now()
            restore = gcg.finalize_override_guarded(
                state, assessment=final_assessment, runner_guard_ok=runner_guard_ok, view_fn=deps.rollout_view,
                list_rs_fn=deps.list_rs, policy_state_fn=pol.state, restore_fn=deps.restore_override,
                real_patch_fn=deps.patch_delay, safety_fn=wait_safety, monotonic=deps.monotonic_fn,
                sleep=deps.sleep_fn, now_utc=deps.now_utc)
            if restore.get("restored") is False:
                cleanup_errors.append(f"scale_down_delay_restore: {restore.get('error')}")
            if state.get("override_error") and state.get("override_rollback_ok") is False:
                cleanup_errors.append(f"scale_down_delay_apply_rollback_failed: {state['override_error']}")
        try:
            delay_after = deps.read_delay()
        except Exception as e:  # noqa: BLE001
            delay_after = f"read_error: {type(e).__name__}: {e}"
        if delay_after != BASE_SCALE_DOWN_DELAY_SEC:  # 원복 확인(act)·무변경 확인(record_only)
            cleanup_errors.append(f"scale_down_delay_after={delay_after} != {BASE_SCALE_DOWN_DELAY_SEC}")
        if mode == MODE_RO and (state.get("override") or state.get("override_error")):
            cleanup_errors.append("record_only에서 유예 변경 흔적 - 수동 확인")
        if (policy_after or {}).get("unresolved"):
            cleanup_errors.append(f"policy_unresolved_actions: {sorted(policy_after['unresolved'])}")
        if (policy_after or {}).get("blocked"):
            cleanup_errors.append(f"policy_blocked: {policy_after['blocked']}")
        if watcher.record()["guard_pending_at_end"]:
            cleanup_errors.append("service_wait_still_running_at_end")
        cleanup_errors.extend(f"late: {x}" for x in watcher.late_failures)
        try:
            old_policy_end = deps.old_policy_check()
        except Exception as e:  # noqa: BLE001
            old_policy_end = {"isolated": False, "reasons": [f"check_error: {type(e).__name__}: {e}"]}
        if old_policy_end.get("isolated") is not True:
            cleanup_errors.append(f"old_recovery_policy_not_isolated_at_end: {old_policy_end.get('reasons')}")
        if outcome.get("cleanup_status") != "complete":
            cleanup_errors.append(f"trial_cleanup_incomplete: prober={outcome.get('prober_stop_failed')}, "
                                  f"injector={outcome.get('injector_cleanup_failed')}")
        if kind == "pressure" and injector is not None:
            try:
                left = deps.wait_chaos_gone(injector.get_stress_delivery_record().get("planned_cr_names") or [])
                if left:
                    cleanup_errors.append(f"stress_chaos_residual: {left}")
            except Exception as e:  # noqa: BLE001
                cleanup_errors.append(f"stress_chaos_check_failed: {type(e).__name__}: {e}")
        if gate.restore_failed is not None:  # 계약 r2: 복원 실패는 정리 실패(실행 중 표시 보존 → 다음 arm 차단, 재시도 없음)
            cleanup_errors.append(f"canonical_restore_failed: {gate.restore_failed.get('problems')}")
        if is_ab(run):  # 끝 상태도 canonical이어야 다음 arm이 같은 시작 상태를 가진다(읽기만, 고치지 않음)
            canonical_end = gcg.wait_canonical(deps.canonical_snapshot, delay=BASE_SCALE_DOWN_DELAY_SEC,
                                               sleep=deps.sleep_fn, monotonic=deps.monotonic_fn)
            if not canonical_end["ok"]:
                cleanup_errors.append(f"canonical_end_not_reached: {canonical_end['problems']}")
        if trial_error:
            cleanup_errors.append(f"harness_error: {trial_error}")
            try:
                left = [p["name"] for p in deps.read_probe_pods() if not p["terminating"]]
                if left:
                    cleanup_errors.append(f"probe_pod_residual: {left}")
            except Exception as e:  # noqa: BLE001
                cleanup_errors.append(f"probe_pod_check_failed: {type(e).__name__}: {e}")
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)

    pod_events_error = None
    try:
        (results_dir / f"pod-events-{run_id}.json").write_text(
            json.dumps(deps.capture_events(events_since), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        pod_events_error = f"{type(e).__name__}: {e}"
    session_end = deps.session_check()  # 관찰·사후 검증·정리 동안 보조 프로세스가 살아 있었는지(끝 시점 확인)
    counts = evidence_counts(evidence_path)
    readiness = outcome.get("detector_readiness") or {}
    rehearsal = None
    if mode == MODE_RO:  # 신호가 자연 발생하지 않으면 연결·평가·기록 확인까지만 성공으로 본다
        rehearsal = {"connection_evaluation_recording_ok": bool(
            outcome.get("status") == "completed" and readiness.get("ready_at_utc") and counts["scored"] > 0
            and not counts["parse_errors"] and not cleanup_errors and session_end.get("ok")),
            "signal_observed": counts["send_attempts"] > 0, "send_results": counts["send_results"],
            "note": "신호가 없으면 탐지·정책 판단 경로는 확인되지 않은 것이다(0건을 '경고 없음 검증'으로 쓰지 않음)"}
    summary = {"run_id": run_id, "run": run, "kind": kind, "mode": mode, "outcome_status": outcome.get("status"),
               "outcome_reason": outcome.get("reason"), "invalidity_reasons": outcome.get("invalidity_reasons"),
               "detector_readiness": readiness, "evidence_counts": counts, "watcher": watcher.record(), "context": context,
               "policy_state_after": policy_after, "scale_down_delay_override": state.get("override"),
               "scale_down_delay_override_error": state.get("override_error"), "scale_down_delay_restore": restore,
               "scale_down_delay_before": delay_before, "scale_down_delay_after": delay_after,
               "session_start": session_start, "session_end": session_end, "pod_events_error": pod_events_error,
               "probe_image_runtime_check": probe_image_check, "start_active_node": state.get("start_active_node"),
               "harness_error": trial_error, "cleanup_errors": cleanup_errors, "rehearsal": rehearsal,
               "service_extensions": extensions, "observation_ended_at_utc": outcome.get("observation_ended_at_utc"),
               "old_policy_isolation": {"start": old_policy, "checks": old_policy_checks,
                                        "end": {k: old_policy_end.get(k) for k in ("isolated", "reasons")}},
               "service_check_note": contract["service_check_note"]}
    # 기록 전용 보존(판정·정리에 쓰지 않음): 시행 함수 결과 전체(주입 시각·단계 구간·관찰 시작 등)와 압박 전달 기록
    summary["trial_outcome"] = outcome
    summary["preview_cleanup_gate"] = gate.log  # D5 판정·보류 근거
    summary["final_promotion_assessment"] = final_assessment  # D6 원복 경로 판정 근거(act)
    summary["ssh_check_evidence"] = ssh_ev.summary()  # 디스크 점검 원문·단계 근거 연결(판정은 run_guard의 disk_failure)
    summary["run_guard"] = {k: v for k, v in run_guard.items() if not k.startswith("_")}
    try:
        summary["stress_delivery_record"] = injector.get_stress_delivery_record() if injector is not None else None
    except Exception as e:  # noqa: BLE001
        summary["stress_delivery_record"] = {"error": f"{type(e).__name__}: {e}"}
    summary["comparison"] = comparison
    summary["preflight_file"] = preflight_file.name  # 이 실행의 사전검사 기록(분석기가 공식 run preflight로 읽는다)
    summary["canonical_end"] = canonical_end  # 계약 r2(A/B만). 미승격 복원 근거는 preview_cleanup_gate[*].canonical_restore
    summary["cpu_raw_counter_samples"], summary["cpu_raw_counter_error"] = None, None
    if kind == "pressure":  # 압박 전달 근거 수집(기록 전용 - 실패해도 정리·차단 판단에 쓰지 않고, 분석기가 판정 불가로 본다)
        try:
            tgt = (summary["stress_delivery_record"] or {}).get("target") or {}
            t0 = outcome.get("t_injection_observed_utc")
            if not tgt.get("name") or not t0:
                raise RuntimeError(f"target_or_injection_time_missing: {tgt.get('name')}/{t0}")
            summary["cpu_raw_counter_samples"] = deps.cpu_raw_counter(tgt["name"], datetime.fromisoformat(t0))
        except Exception as e:  # noqa: BLE001
            summary["cpu_raw_counter_error"] = f"{type(e).__name__}: {e}"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if not cleanup_errors:
        running_marker.unlink()
    return summary


def main(argv=None) -> int:  # pragma: no cover - 라이브 진입점(가짜 의존성 경로는 run_guarded로 시험)
    p = argparse.ArgumentParser(description="guarded-v1 실행 1회(기본 record_only). 관리자 토큰은 환경변수 GUARDED_ADMIN_TOKEN")
    p.add_argument("--run", required=True, choices=sorted(RUNS))
    p.add_argument("--mode", required=True, choices=[MODE_RO, MODE_ACT])
    p.add_argument("--policy-url", default="http://127.0.0.1:18090")
    p.add_argument("--session-file", default=None)
    p.add_argument("--act-approval", default=None)
    p.add_argument("--preflight-only", action="store_true")
    p.add_argument("--dry-run-contract-only", action="store_true")
    a = p.parse_args(argv)
    token = os.environ.get("GUARDED_ADMIN_TOKEN")
    if not token and not a.dry_run_contract_only:
        raise SystemExit("GUARDED_ADMIN_TOKEN 환경변수 필요")
    s = run_guarded(a.run, mode=a.mode, deps=real_deps(a.policy_url, token, a.session_file), policy_url=a.policy_url,
                    approval_path=a.act_approval, preflight_only=a.preflight_only, dry_run=a.dry_run_contract_only)
    if s.get("cleanup_errors"):
        print(f"[중단] 정리·원복·미해결 조치 - 다음 실행은 자동 거부, 수동 확인 필요: {s['cleanup_errors']}")
        return 2
    print(f"결과: {s.get('outcome_status') or s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
