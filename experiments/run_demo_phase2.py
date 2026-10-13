#!/usr/bin/env python3
"""Phase 2 시연 전용(demo-only, counted=false) 실행기 - 2026-10-12.

공식 Phase 2 Stage 2 실행기(run_stage2.run_session)와 같은 방식으로 동결 B/C/K 실행기 run_guarded_bck.run_bck를
**수정 없이** 호출한다. 바꾸는 것은 이 프로세스 안의 실행 ID 표·plan ID·결과 폴더뿐이고(호출 뒤 원래 값으로 복원),
감지기 = p2_executor active(Active Profile의 ACTIVE만 송신), 정책·승격·트래픽 검증 = 공식과 같은 경로다.

공식과 다른 데모 전용 동작 두 가지(공식 실행기·Recovery Policy·Argo·감지기·임계값은 그대로):
1) 압박 대기: 새 Pod·요청 발생기·감지기가 준비된 뒤 압박 직전에 멈추고, 발표자가 시작 신호 파일
   (demo-go-<run>.flag, start-pressure.ps1이 만든다)을 만들 때까지 기다린다. TRIGGER_WAIT_MAX_SEC 안에 없으면
   압박 없이 끝낸다. 대기 시간만큼 요청 발생기 수명을 늘린다.
2) 데모 harness의 압박 정리: 아래가 모두 확인된 뒤에만 이번 실행이 만든 StressChaos만 지운다
   (run_id annotation이 정확히 같고 이름이 이번 실행 계획 목록에 있는 것). 기존 Pod·scaleDownDelay는 건드리지 않는다.
   ① ACTIVE 신호의 정책 결과 promoted_verified ② Rollout activeSelector = new hash
   ③ vllm-active Service selector = new hash(앞뒤 두 번 읽어 같을 때) ④ old hash != new hash ⑤ new Pod Ready
   이것은 Recovery Policy의 자동 치료가 아니라, 데모 harness가 자신이 주입한 인공 압박을 정리하는 것이다.

실행 ID p2demo01–p2demo09, 결과 experiments/results/demo_only_phase2/, run_id에 plan capstone-phase2-demo-only-20261012.
세션 래퍼: guarded_pilot_session.ps1 -RunnerScript experiments\\run_demo_phase2.py -SessionRoot <결과 폴더>
(Active Profile·선택 증거·아티팩트 경로는 공식과 같은 환경변수 PHASE2_ACTIVE_PROFILE / PHASE2_SELECTION_EVIDENCE /
PHASE2_ARTIFACT_ROOT)."""
import sys
sys.dont_write_bytecode = True
import argparse  # noqa: E402
import contextlib  # noqa: E402
import copy  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent / "phase2_detector_selection"))
import p2_common as pc  # noqa: E402  (실험·감지기·정책 경로를 sys.path에 넣는다)
import run_stage1 as s1  # noqa: E402
import run_stage2 as s2  # noqa: E402

import guarded_live_detector_runner as gr  # noqa: E402
import run_guarded_bck as gb  # noqa: E402
import run_guarded_pilot as gp  # noqa: E402
import run_timed_switch_recovery_trial as rt  # noqa: E402

DEMO_PLAN_ID = "capstone-phase2-demo-only-20261012"
DEMO_DIR = pc.EXP / "results" / "demo_only_phase2"
DEMO_DOC = "docs/demo/PHASE2-DEMO-RUNBOOK-20261012.md"
ARM = s2.ARM
RUNS = {f"p2demo{i:02d}": (i, ARM, 4300 + i) for i in range(1, 10)}
TRIGGER_WAIT_MAX_SEC = 15 * 60       # 준비 완료 뒤 발표자가 압박을 시작할 때까지 기다리는 상한
MTIME_SLACK_SEC = 2.0               # Windows 파일 시각 해상도 여유(지난 신호 파일은 세션 시작 때 지운다)
POLL_SEC = 2.0                       # 시작 신호·전환 조건 확인 주기(읽기 전용, 부담 낮음)
CLEANUP_WATCH_SEC = gp.PRESSURE_OBSERVATION_SEC + 120  # 전환 조건을 기다리는 상한(관찰 구간 + 여유)
NAMESPACE, ROLLOUT, ACTIVE_SVC, HASH_LABEL = "vllm-serving", "vllm-serving", "vllm-active", "rollouts-pod-template-hash"
CHAOS = ("chaos-mesh.org", "v1alpha1", "stresschaos")
RUN_ANNOTATION = "experiment-run-id-full"
# guarded_policy.target_key: <ns>/<rollout>@<rollout uid>:<active hash>-><preview hash>#<preview pod uid>
TARGET_RE = re.compile(r"^[^/]+/[^@]+@[^:]+:(?P<old>[^-]+)->(?P<new>[^#]+)#(?P<new_uid>.+)$")


class DemoTriggerTimeout(Exception):
    """시작 신호가 상한 안에 오지 않음 - 압박 없이 끝낸다(공식 정리 경로를 탄다)."""


def run_id_for(run: str) -> str:
    return f"guarded_pressure_{gp.MODE_ACT}-{run}-{DEMO_PLAN_ID}"


def trigger_path(d: Path, run: str) -> Path:
    return Path(d) / f"demo-go-{run}.flag"


def state_path(d: Path, run_id: str) -> Path:
    return Path(d) / f"demo-state-{run_id}.json"


def cleanup_record_path(d: Path, run_id: str) -> Path:
    return Path(d) / f"demo-stress-cleanup-{run_id}.json"


def session_path(d: Path, run: str) -> Path:
    return Path(d) / f"p2demo-session-{run}.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json_atomic(path: Path, obj: dict) -> None:
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def check_demo_approval(path, run: str) -> dict:
    if not path:
        raise SystemExit("demo act는 승인 기록(--act-approval)이 있어야 시작한다")
    a = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if a.get("demo_only") is not True or a.get("counted") is not False or a.get("runs") != [run]:
        raise SystemExit(f"demo 승인 기록 아님 - demo_only=true, counted=false, runs=[{run!r}] 필요")
    return a


# ---------------- 1) 압박 대기 ----------------
def wait_for_trigger(flag: Path, armed_at: float, *, max_wait_sec: float = TRIGGER_WAIT_MAX_SEC,
                     sleep=time.sleep, monotonic=time.monotonic) -> float:
    """armed_at(epoch 초) 이후에 만들어진 시작 신호 파일을 기다린다. 그 전부터 있던 파일은 무시(지난 실행 잔재)."""
    deadline = monotonic() + max_wait_sec
    while True:
        try:
            st = flag.stat()
            if st.st_mtime >= armed_at - MTIME_SLACK_SEC:
                return st.st_mtime
        except FileNotFoundError:
            pass
        if monotonic() >= deadline:
            raise DemoTriggerTimeout(f"시작 신호 {flag.name}가 {max_wait_sec:.0f}초 안에 없음 - 압박 없이 종료")
        sleep(POLL_SEC)


# ---------------- 2) 전환 확인 뒤 데모 압박 정리 ----------------
def target_from_evidence(lines, active_id: str):
    """ACTIVE 신호의 promoted_verified 결과에서 old/new hash와 new Pod UID. 없으면 None."""
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get("record_type") != "c1_policy_send_result" or r.get("policy_outcome") != "promoted_verified":
            continue
        if r.get("detector_id") != active_id:  # 식별 없음·SHADOW 결과로는 정리하지 않는다
            continue
        m = TARGET_RE.match(r.get("target_key") or "")
        if m:
            return {**m.groupdict(), "decided_at": r.get("completed_at_utc"), "target_key": r.get("target_key")}
    return None


def switch_ready(view: dict, target: dict):
    """조건 ②–⑤. (통과 여부, 이유 목록)."""
    why = []
    if target["old"] == target["new"]:
        why.append("old_hash_equals_new_hash")
    if view.get("active_selector") != target["new"]:
        why.append(f"rollout_active_selector={view.get('active_selector')}")
    sb, sa = view.get("service_before"), view.get("service_after")
    if sb != sa:
        why.append("service_selector_changed_during_read")
    elif sa != target["new"]:
        why.append(f"service_selector={sa}")
    new_pods = [p for p in view.get("pods") or [] if p.get("hash") == target["new"] and p.get("uid") == target["new_uid"]]
    if not new_pods or not all(p.get("ready") is True and not p.get("terminating") for p in new_pods):
        why.append("new_pod_not_ready")
    return not why, why


def _read_lines(path: Path) -> list:
    try:
        text = Path(path).read_bytes().decode("utf-8-sig")
    except FileNotFoundError:
        return []
    return [ln for ln in text.split("\n")[:-1] if ln.strip()]  # 쓰는 중인 마지막 줄 제외


class StressCleanupWatcher:
    """조건 ①–⑤가 모두 확인되면 cleanup_fn(압박 장치 자체의 cleanup - preview 감싸개가 덮어쓰기 전에 잡아 둔 것)으로
    이번 실행의 StressChaos만 정리한다. 결과는 별도 기록 파일에 남기고 트래픽 검증 결과와 섞지 않는다.
    예외를 밖으로 던지지 않는다(시행을 깨지 않음). 끝나면 done이 켜진다."""

    def __init__(self, *, run_id, active_id, evidence_path, cleanup_fn, delivery_fn, read_view, list_chaos, record_path,
                 sleep=time.sleep, monotonic=time.monotonic, watch_sec=CLEANUP_WATCH_SEC):
        self.run_id, self.active_id, self.evidence_path = run_id, active_id, Path(evidence_path)
        self.cleanup_fn, self.delivery_fn = cleanup_fn, delivery_fn
        self.done = threading.Event()
        self.read_view, self.list_chaos, self.record_path = read_view, list_chaos, Path(record_path)
        self.sleep, self.monotonic, self.watch_sec = sleep, monotonic, watch_sec
        self.stop_event = threading.Event()
        self.record = {"run_id": run_id, "demo_only": True, "counted": False, "status": "watching",
                       "meaning": "데모 harness가 자신이 주입한 인공 CPU 압박을 서비스 전환 확인 뒤 정리(Recovery Policy 기능 아님)",
                       "started_at_utc": _now_iso(), "last_reasons": None, "target": None}

    def _write(self):
        try:
            write_json_atomic(self.record_path, self.record)
        except OSError as e:  # 기록 실패는 결과에 남길 곳이 없으므로 표준 오류로만
            print(f"[demo] 정리 기록 실패: {e}", file=sys.stderr)

    def run(self):
        try:
            self._run()
        except Exception as e:  # noqa: BLE001
            self.record.update(status="failed", error=f"{type(e).__name__}: {e}", ended_at_utc=_now_iso())
            self._write()
        finally:
            self.done.set()

    def finish(self, timeout=15.0):
        """실행기 종료 때: 아직 조건을 기다리는 중이면 멈추고, 기록이 끝날 때까지 잠깐 기다린다."""
        self.stop_event.set()
        return self.done.wait(timeout)

    def _run(self):
        self._write()
        deadline = self.monotonic() + self.watch_sec
        while not self.stop_event.is_set():
            target = target_from_evidence(_read_lines(self.evidence_path), self.active_id)
            if target is None:
                self.record["last_reasons"] = ["no_promoted_verified_active_signal"]
            else:
                self.record["target"] = target
                try:
                    ok, why = switch_ready(self.read_view(), target)
                except Exception as e:  # noqa: BLE001 - 읽기 실패는 '조건 미확인'
                    ok, why = False, [f"view_read_failed: {type(e).__name__}"]
                self.record["last_reasons"] = why
                if ok:
                    self.record["conditions_met_at_utc"] = _now_iso()
                    self._cleanup()
                    return
            if self.monotonic() >= deadline:
                self.record.update(status="not_triggered", ended_at_utc=_now_iso())
                self._write()
                return
            self.sleep(POLL_SEC)
        self.record.update(status="stopped_before_conditions", ended_at_utc=_now_iso())
        self._write()

    def _cleanup(self):
        names = list((self.delivery_fn() or {}).get("planned_cr_names") or [])
        items = self.list_chaos()
        mine = [i for i in items if i["name"] in names and i.get("annotation") == self.run_id]
        foreign = [i for i in items if i["name"] in names and i.get("annotation") != self.run_id]
        if foreign:  # 이름만 같고 이번 실행 것이 아니면 지우지 않는다
            self.record.update(status="failed", error="annotation_mismatch", foreign=[i["name"] for i in foreign],
                               ended_at_utc=_now_iso())
            self._write()
            return
        self.cleanup_fn()  # 압박 단계 스레드 정지 + 이번 실행 계획 목록의 CR만 삭제(없는 CR은 404 무시). preview는 안 건드림
        left = [i["name"] for i in self.list_chaos() if i["name"] in names]
        self.record.update(status="failed" if left else "cleaned",
                           deleted=[{"name": i["name"], "uid": i.get("uid")} for i in mine],
                           left=left, cleaned_at_utc=_now_iso(), ended_at_utc=_now_iso())
        if left:
            self.record["error"] = "stress_chaos_left_after_cleanup"
        self._write()


# ---------------- 데모 압박 장치 조립 ----------------
def make_demo_injector_factory(*, d: Path, run: str, active_id: str, build_inner, wrap_outer, read_view, list_chaos,
                               max_wait_sec=TRIGGER_WAIT_MAX_SEC, sleep=time.sleep, monotonic=time.monotonic,
                               wall=time.time, start_thread=None, watchers: list,
                               watcher_sleep=time.sleep, watcher_monotonic=time.monotonic):
    """공식 make_injector와 같은 조립(압박 장치 → preview 준비 감싸개 → 노드 진단 감싸개)인데, 감싸기 전에 압박 장치의
    inject만 '시작 신호 대기 → 원래 inject → 정리 감시 시작'으로 바꾼다. kind는 pressure만."""
    start_thread = start_thread or (lambda fn: threading.Thread(target=fn, name="demo-stress-cleanup", daemon=True).start())

    def make_injector(run_id, rep, kind, cleanup_preview_fn):
        if kind != "pressure":
            raise SystemExit(f"demo 실행기는 pressure만: {kind}")
        inner = build_inner(run_id, rep)
        original_inject = inner.inject
        pressure_cleanup, delivery = inner.cleanup, inner.get_stress_delivery_record  # wrap_outer가 cleanup을 덮어쓰기 전
        sp = state_path(d, run_id)

        def gated_inject():
            armed_at = wall()
            write_json_atomic(sp, {"run": run, "run_id": run_id, "phase": "waiting_for_trigger", "armed_at_utc": _now_iso(),
                                   "trigger_file": trigger_path(d, run).name, "max_wait_sec": max_wait_sec})
            try:
                wait_for_trigger(trigger_path(d, run), armed_at, max_wait_sec=max_wait_sec, sleep=sleep, monotonic=monotonic)
            except DemoTriggerTimeout:
                write_json_atomic(sp, {"run": run, "run_id": run_id, "phase": "trigger_timeout", "at_utc": _now_iso()})
                raise
            write_json_atomic(sp, {"run": run, "run_id": run_id, "phase": "pressure_started", "triggered_at_utc": _now_iso()})
            original_inject()
            w = StressCleanupWatcher(run_id=run_id, active_id=active_id, evidence_path=gr.live_evidence_path(d, run_id),
                                     cleanup_fn=pressure_cleanup, delivery_fn=delivery,
                                     read_view=read_view, list_chaos=list_chaos,
                                     record_path=cleanup_record_path(d, run_id), sleep=watcher_sleep,
                                     monotonic=watcher_monotonic)  # 별도 스레드 - 시행 시계와 분리(실제 시간)
            watchers.append(w)
            start_thread(w.run)

        inner.inject = gated_inject
        return wrap_outer(inner, cleanup_preview_fn)
    return make_injector


def real_injector_parts():  # pragma: no cover - 라이브 조립(공식 run_guarded_pilot.real_deps.make_injector와 같은 부품·인자)
    import arm_controller
    import node_resource_safety_gate as nrsg
    import run_detector_switch_ab_trial as ab
    from cpu_pressure_demo_adapter import make_cpu_pressure_injector
    from run_cpu_demo_trial import DEMO_STAGES

    def build_inner(run_id, rep):
        return make_cpu_pressure_injector(run_id, ab.ARM_LABEL, rep, stages=DEMO_STAGES, continue_after_target_replacement=True)

    def wrap_outer(inner, cleanup_preview_fn):
        inj = arm_controller.wrap_injector_with_preview_prep(inner, ab.ARM_LABEL, cleanup_preview_fn=cleanup_preview_fn)
        return nrsg.wrap_injector_with_real_promotion_diagnostic_exception(
            inj, ab.ARM_LABEL, nrsg.REAL_PROMOTION_DIAGNOSTIC_EXPECTED_RUN_MODE)
    return build_inner, wrap_outer


def real_read_view():  # pragma: no cover - 라이브 읽기 전용(read_/list_/get_)
    from kubernetes import client, config
    config.load_kube_config()
    core, custom = client.CoreV1Api(), client.CustomObjectsApi()

    def svc():
        return (core.read_namespaced_service(ACTIVE_SVC, NAMESPACE, _request_timeout=5).spec.selector or {}).get(HASH_LABEL)

    def read():
        before = svc()
        ro = custom.get_namespaced_custom_object("argoproj.io", "v1alpha1", NAMESPACE, "rollouts", ROLLOUT, _request_timeout=5)
        pods = core.list_namespaced_pod(NAMESPACE, label_selector=HASH_LABEL, _request_timeout=5).items
        after = svc()
        return {"active_selector": ((ro.get("status") or {}).get("blueGreen") or {}).get("activeSelector"),
                "service_before": before, "service_after": after,
                "pods": [{"hash": (p.metadata.labels or {}).get(HASH_LABEL), "uid": p.metadata.uid,
                          "ready": any(c.type == "Ready" and c.status == "True" for c in (p.status.conditions or [])),
                          "terminating": p.metadata.deletion_timestamp is not None} for p in pods]}
    return read


def real_list_chaos():  # pragma: no cover - 라이브 읽기 전용
    from kubernetes import client, config
    config.load_kube_config()
    custom = client.CustomObjectsApi()

    def read():
        items = custom.list_namespaced_custom_object(*CHAOS[:2], NAMESPACE, CHAOS[2], _request_timeout=5).get("items") or []
        return [{"name": (i.get("metadata") or {}).get("name"), "uid": (i.get("metadata") or {}).get("uid"),
                 "annotation": ((i.get("metadata") or {}).get("annotations") or {}).get(RUN_ANNOTATION)} for i in items]
    return read


# ---------------- 세션 ----------------
@contextlib.contextmanager
def patched_demo(results_dir, sched, ident):
    """run_stage2.patched_bck와 같은 방식 - 계열 전역만 호출 동안 demo 값으로 바꾸고 반드시 복원한다(파일 수정 없음)."""
    names = ("RUNS", "check_schedule", "BCK_PLAN_ID", "BCK_CONTRACT_DOC", "BCK_RESULTS_DIR", "ARM_LABEL")
    saved, saved_ident = {n: getattr(gb, n) for n in names}, gb.bd.signal_identity
    try:
        gb.RUNS, gb.check_schedule = RUNS, (lambda r, _d: sched)
        gb.BCK_PLAN_ID, gb.BCK_CONTRACT_DOC, gb.BCK_RESULTS_DIR = DEMO_PLAN_ID, DEMO_DOC, Path(results_dir)
        gb.ARM_LABEL = {ARM: f"phase2_active:{ident['detector_id']}"}
        gb.bd.signal_identity = lambda arm, hashes, spec_sha: dict(ident)
        yield gb
    finally:
        for n, v in saved.items():
            setattr(gb, n, v)
        gb.bd.signal_identity = saved_ident


def run_demo_session(run, *, deps, entry: dict, active_profile_path, artifact_root, approval_path, policy_url,
                     results_dir=None, preflight_only=False, code_provenance=None, starter_factory=None,
                     injector_parts=None, read_view=None, list_chaos=None, max_wait_sec=TRIGGER_WAIT_MAX_SEC,
                     watchers: list = None, audit_reader=None, start_thread=None) -> dict:
    """[USER APPROVAL REQUIRED] 실제 deps면 act 압박·실제 승격(데모 1회). entry = run_stage2.entry_check 결과."""
    if run not in RUNS:
        raise SystemExit(f"demo 실행 ID가 아님: {run} (허용 {sorted(RUNS)})")
    check_demo_approval(approval_path, run)
    d = Path(results_dir or DEMO_DIR)
    d.mkdir(parents=True, exist_ok=True)
    if session_path(d, run).exists():
        raise SystemExit(f"{run} 거부: 이미 쓴 demo 실행 ID")
    run_id = run_id_for(run)
    tag = d / f"demo-tag-{run_id}{'-preflight-only' if preflight_only else ''}.json"
    if not tag.exists():  # 실행 전에 남긴다 - 중단돼도 이 폴더 자료가 demo 전용임이 남도록
        write_json_atomic(tag, {"run": run, "run_id": run_id, "demo_only": True, "counted": False,
                                "official_evidence": False, "selected_active": entry["selected"], "written_at_utc": _now_iso()})
    if not preflight_only:  # 지난 시도의 시작 신호 파일이 남아 있으면 바로 압박이 걸리지 않도록 지운다(이 run 것만)
        try:
            trigger_path(d, run).unlink()
        except FileNotFoundError:
            pass
    sched = {"block": RUNS[run][0], "order": ARM, "position": 1, "required_before": [], "attempt_number": RUNS[run][0]}
    ident = pc.signal_identity(entry["selected"], entry["art"]["hashes"])
    deps = copy.copy(deps)
    deps.artifacts = lambda: entry["art"]
    deps.starter_factory = starter_factory or s2.active_starter(active_profile_path, artifact_root)
    deps.probe_csv_path = lambda rid, rep: d / f"probe-{rid}-{rt.ARM_LABEL}-{rep}-raw.csv"  # 공식 결과 폴더에 쓰지 않음
    base_prober = deps.make_prober
    deps.make_prober = lambda rid, rep, kind, lifetime: base_prober(rid, rep, kind, lifetime + max_wait_sec)  # 대기만큼 수명 연장
    watchers = [] if watchers is None else watchers
    build_inner, wrap_outer = injector_parts or real_injector_parts()
    deps.make_injector = make_demo_injector_factory(
        d=d, run=run, active_id=entry["selected"], build_inner=build_inner, wrap_outer=wrap_outer,
        read_view=read_view or real_read_view(), list_chaos=list_chaos or real_list_chaos(),
        max_wait_sec=max_wait_sec, sleep=deps.sleep_fn, monotonic=deps.monotonic_fn, watchers=watchers,
        start_thread=start_thread)
    started = _now_iso()
    try:
        with patched_demo(d, sched, ident):
            summary = gb.run_bck(run, deps=deps, policy_url=policy_url, approval_path=approval_path,
                                 preflight_only=preflight_only, results_dir=d, code_provenance=code_provenance)
    finally:
        # 정리 감시를 멈추고 별도 기록을 확정한 뒤에 결과를 읽는다(제때 안 끝나면 그 사실을 기록에 남긴다)
        watchers_finished = all([w.finish(timeout=30.0) for w in watchers])
        sp = state_path(d, run_id)
        if not preflight_only:  # 지난 '대기' 상태가 남아 다음 압박 신호를 받지 않도록(직전 단계는 남긴다)
            try:
                prev = json.loads(sp.read_text(encoding="utf-8")).get("phase")
            except (OSError, ValueError, AttributeError):
                prev = None
            write_json_atomic(sp, {"run": run, "run_id": run_id, "phase": "session_ended", "previous_phase": prev,
                                   "at_utc": _now_iso()})
    if preflight_only:
        return {"preflight_only": True, "run_id": run_id}
    ep = gr.live_evidence_path(d, run_id)
    if audit_reader is not None:  # 공식 Stage 2와 같은 확인 - 정책이 이 창에 받은 신호가 ACTIVE뿐인지(읽기 전용)
        try:
            summary["policy_decisions"] = [{"delivery_key": x.get("delivery_key"), "outcome": x.get("outcome"),
                                            "detector_id": ((x.get("delivery_key") or "").split(":") + ["", ""])[1]}
                                           for x in s1.policy_decisions(audit_reader(), started, _now_iso())]
        except Exception as e:  # noqa: BLE001
            summary["policy_decisions_error"] = f"{type(e).__name__}: {e}"
    result = s2.stage2_session_result(summary, s2._lines(ep), entry["selected"])
    cp = cleanup_record_path(d, run_id)
    cleanup = json.loads(cp.read_text(encoding="utf-8")) if cp.exists() else None
    if cleanup is not None and not watchers_finished:  # 감시가 아직 API 호출 중 - 결과를 확정하지 않는다
        cleanup = {**cleanup, "status": "watcher_unfinished", "error": "watcher did not finish before session end"}
    rec = {"run": run, "run_id": run_id, "demo_only": True, "counted": False, "selected": entry["selected"],
           "environment_profile_sha256": entry["loaded"]["sha256"], "selection_evidence_sha256": entry["evidence_sha256"],
           "runner_started_at_utc": started, "runner_ended_at_utc": _now_iso(), "evidence_file": ep.name,
           "outcome_status": summary.get("outcome_status"), "result": result,
           "demo_stress_cleanup": None if cleanup is None else {k: cleanup.get(k) for k in ("status", "cleaned_at_utc", "error", "left")},
           "code_provenance": code_provenance, "cleanup_errors": list(summary.get("cleanup_errors") or [])}
    pc.write_json_new(session_path(d, run), rec)
    return {"summary": summary, "session_record": rec}


def main(argv=None) -> int:  # pragma: no cover - 라이브 진입점(세션 래퍼가 부른다, 사용자 승인 필요)
    p = argparse.ArgumentParser(description="Phase 2 demo-only 세션 1회(act, 실제 승격 - 사용자 승인 필요)")
    p.add_argument("--run", required=True, choices=sorted(RUNS))
    p.add_argument("--mode", choices=[gp.MODE_ACT], default=gp.MODE_ACT)
    p.add_argument("--policy-url", default="http://127.0.0.1:18090")
    p.add_argument("--session-file", default=None)
    p.add_argument("--act-approval", required=True)
    p.add_argument("--preflight-only", action="store_true")
    p.add_argument("--selection-profile", type=Path, default=pc.SELECTION_PROFILE)
    p.add_argument("--active-profile", type=Path, default=os.environ.get("PHASE2_ACTIVE_PROFILE") or None)
    p.add_argument("--selection-evidence", type=Path, default=os.environ.get("PHASE2_SELECTION_EVIDENCE") or None)
    p.add_argument("--artifact-root", type=Path, default=os.environ.get(s2.ARTIFACT_ENV) or None)
    a = p.parse_args(argv)
    if not (a.active_profile and a.selection_evidence and a.artifact_root):
        raise SystemExit("PHASE2_ACTIVE_PROFILE / PHASE2_SELECTION_EVIDENCE / PHASE2_ARTIFACT_ROOT 필요")
    s2.check_outcome(a.selection_evidence)
    impl = pc.verify_implementation_freeze()
    token = os.environ.get("GUARDED_ADMIN_TOKEN")
    if not token:
        raise SystemExit("GUARDED_ADMIN_TOKEN 환경변수 필요")
    deps = gb.real_deps(a.policy_url, token, a.session_file)
    base = deps.check_unresolved
    deps.check_unresolved = lambda: (base(), rt.check_no_unresolved_failures(DEMO_DIR))  # demo 정리 실패도 다음 실행을 막는다
    # 공식과 같은 진입 검사(읽기 전용) - ACTIVE = Stage 1 선택 결과 = Active Profile, 다르면 시작하지 않는다
    entry = s2.entry_check(selection_profile_path=a.selection_profile, active_profile_path=a.active_profile,
                           evidence_path=a.selection_evidence, artifact_root=a.artifact_root,
                           snapshot_fn=deps.canonical_snapshot, impl=impl, stage1_dir=s1.RESULTS_DIR)
    r = run_demo_session(a.run, deps=deps, entry=entry, active_profile_path=a.active_profile, artifact_root=a.artifact_root,
                         approval_path=a.act_approval, policy_url=a.policy_url, preflight_only=a.preflight_only,
                         code_provenance=impl, audit_reader=lambda: s1.read_policy_audit(gp.MODE_ACT))
    s = r.get("summary") or {}
    rec = r.get("session_record") or {}
    print(json.dumps({"run": a.run, "outcome": s.get("outcome_status"), "cleanup_errors": s.get("cleanup_errors"),
                      "result": rec.get("result"), "demo_stress_cleanup": rec.get("demo_stress_cleanup")},
                     ensure_ascii=False, default=str))
    return 2 if s.get("cleanup_errors") else 0


if __name__ == "__main__":
    sys.exit(main())
