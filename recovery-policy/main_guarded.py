"""guarded 정책의 별도 HTTP 앱(기존 main.py와 분리). import·create_app만으로는 네트워크·git·CLI 호출 없음.

- GET  /identity                : {"role","version","mode","actions_enabled"} - 송신 측 수신자 확인용
- GET  /time                    : 서버 UTC 시각(정책 잠금 없이) - 시계 오차 측정 도구용
- POST /signal/v2               : v2 신호 처리(스레드풀에서 실행 - 긴 승격 처리 동안 이벤트 루프를 막지 않음).
                                  정책이 거부해도 HTTP 200 + outcome(전달 성공과 정책 판단 구분)
- GET  /admin/state             : context·차단·미해결 조치
- POST /admin/context           : 실행 context 등록(record_only에서는 actions_allowed 불가)
- POST /admin/context/clear     : run_id 일치 시 해제
- POST /admin/actions/resolve   : 미해결 조치 수동 해결(근거 필수)
- POST /admin/blocked/clear     : 프로세스 차단 해제(근거 필수)
- POST /webhooks/alertmanager   : 관찰 전용 기록(승격 경로 없음)
admin_token이 주어지면 /admin/* 는 헤더 X-Admin-Token 일치가 필요하다(act 빌더는 토큰 필수).

build_app_from_env(): 기본 record_only. act는 아래가 모두 명시돼야만 시작한다(하나라도 없으면 시작 거부):
  GUARDED_MODE=act, GUARDED_MAX_SIGNAL_AGE_SEC, GUARDED_MAX_FUTURE_SKEW_SEC, GUARDED_MAX_WINDOW_TO_DECISION_SEC,
  GUARDED_SERVICE_CRITERIA_ACK=reused-timed-switch-v1(재사용 기준 확인), GUARDED_COUNTER_MAX_STALENESS_SEC,
  GUARDED_ADMIN_TOKEN, 원장·상태·감사·잠금 경로(PVC). 서비스 확인 카운터는 Pod IP /metrics 직접(2026-10-06 gap01 뒤).
  임시값(30/5/30 등)은 기본값으로 넣지 않는다 - 시간 기준은 시계 오차 측정 뒤 결정 항목이다.

잠금 수명주기(2026-10-06):
- 시작: 단일 기록자 잠금 획득(소유 토큰). 획득 뒤 초기화가 실패하면, 시작된 기록 작업이 없음이 확인될 때만 자신이 획득한
  잠금을 풀고 원래 오류를 다시 올린다(해제 실패도 오류로 올린다).
- 정상 종료(uvicorn이 SIGTERM/SIGINT/SIGBREAK로 새 연결을 멈추고 진행 중 요청을 끝낸 뒤 lifespan 종료): 정책이 새 신호·관리
  변경을 거부(shutting_down)하고, 진행 중 요청·백그라운드 서비스 확인 작업이 0이 될 때까지 기다린다(GUARDED_SHUTDOWN_WAIT_SEC).
  0이 확인되고 현재 경로의 잠금이 내 것(owns: 핸들 + 토큰 일치)일 때만 감사 기록을 남기고 잠금을 푼다.
  보고: stderr에는 항상 남긴다. 공유 저장소의 shutdown-report.json은 쓰는 순간 잠금이 내 것으로 확인될 때만 쓴다
  (작업 미완료로 잠금을 유지하는 경우). 소유 불명·다른 소유자, 그리고 잠금 삭제 실패(해제 과정에서 핸들을 이미 닫아
  소유를 다시 확인할 수 없음)는 stderr로만 보고한다 - 다른 소유자의 잠금·보고서·감사·상태를 바꾸지 않는다.
  무조건 지우지 않는다.
- 강제 종료(SIGKILL 등)는 잠금이 남는다 → 다음 시작은 LockHeld로 거부(사람이 확인하고 지움, 자동 삭제 없음).
- 잠금 해제는 상태·원장을 바꾸지 않는다: 재시작 때 기존 미해결 조치·차단·원장은 그대로 다시 읽히고 actions_allowed는 False.
"""
import json
import math
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

import guarded_ledger as gl
import guarded_state as gs
from guarded_policy import MODE_ACT, MODE_RECORD_ONLY, POLICY_VERSION, AuditLog, GuardConfig, GuardedPolicy

ROLE = "guarded-recovery-policy"
SERVICE_CRITERIA_ACK = "reused-timed-switch-v1"


DEFAULT_SHUTDOWN_WAIT_SEC = 150.0  # 서비스 확인(10+60초)·승격 처리(최대 약 60초)가 끝날 시간 + 여유. 배포 grace(180초)보다 짧게


def _emit(rec: dict) -> None:
    print(json.dumps({"guarded_lifecycle": rec}, ensure_ascii=False, default=str), file=sys.stderr, flush=True)


def _write_report(lock, rep: dict) -> None:
    """현재 경로의 잠금이 내 것으로 확인될 때만 공유 저장소에 쓴다. 아니면 쓰지 않는다(stderr 보고만)."""
    if lock is None or not lock.owns():
        rep["report_file"] = "skipped_not_owner_stderr_only"
        return
    path = lock.path.parent / "shutdown-report.json"
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        rep["report_write_error"] = f"{type(e).__name__}: {e}"


def shutdown_sequence(policy: GuardedPolicy, lock, wait_sec: float, *, monotonic=time.monotonic, sleep=time.sleep,
                      emit=_emit) -> dict:
    """정상 종료 처리. 새 작업 거부 → 남은 작업 대기 → 0이 확인될 때만 자신의 잠금 해제. 예외를 던지지 않는다."""
    rep = {"at_utc": datetime.now(timezone.utc).isoformat(), "lock_released": False}
    policy.begin_shutdown()
    w = policy.wait_idle(wait_sec, monotonic=monotonic, sleep=sleep)
    rep["idle"] = w
    if lock is None or not lock.held():
        rep["reason"] = "no_lock_held_by_this_process"
    elif not lock.owns():  # 핸들은 열려 있지만 현재 잠금 파일이 내 것이 아님(또는 확인 불가) - 공유 저장소에 쓰지 않는다
        rep["reason"] = "lock_not_owned_or_unverifiable - 감사·보고서·해제 모두 하지 않음(stderr 보고만)"
    elif not w["idle"]:
        rep["reason"] = (f"work_not_finished(active_requests={w['active_requests']}, service_workers={w['service_workers']}) "
                         f"after {wait_sec}s - 잠금 유지(사람 확인)")
    else:
        aerr = policy._audit({"event": "shutdown_release", "idle": w})
        if aerr:
            rep["audit_error"] = aerr
        try:
            rep["release"] = lock.release()
            rep["lock_released"] = True
        except (gl.LockOwnershipError, gl.LockReleaseError) as e:
            rep["reason"] = f"{type(e).__name__}: {e}"
    if not rep["lock_released"]:
        _write_report(lock, rep)
    emit(rep)
    return rep


def release_after_init_failure(lock, policy: Optional[GuardedPolicy], err: BaseException, *, emit=_emit) -> dict:
    """잠금 획득 뒤 초기화 실패. 시작된 기록 작업이 없음이 확인될 때만 자신이 획득한 잠금을 푼다."""
    idle = policy.wait_idle(0) if policy is not None else {"idle": True, "note": "policy_not_created"}
    rep = {"event": "init_failed", "error": f"{type(err).__name__}: {err}", "idle": idle, "lock_released": False}
    if not idle["idle"]:
        rep["reason"] = "기록 작업이 남아 있음 - 잠금 유지(사람 확인)"
        emit(rep)
        return rep
    try:
        rep["release"] = lock.release()
        rep["lock_released"] = True
    except (gl.LockOwnershipError, gl.LockReleaseError) as e:
        rep["reason"] = f"{type(e).__name__}: {e}"
        emit(rep)
        raise RuntimeError(f"초기화 실패({rep['error']}) 뒤 잠금 해제도 실패: {rep['reason']}") from err
    emit(rep)
    return rep


def create_app(policy: GuardedPolicy, *, admin_token: Optional[str] = None, lock=None,
               shutdown_wait_sec: float = DEFAULT_SHUTDOWN_WAIT_SEC) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        yield
        app.state.shutdown_report = await run_in_threadpool(shutdown_sequence, policy, lock, shutdown_wait_sec)

    app = FastAPI(lifespan=lifespan)

    def _admin(token):
        if admin_token is not None and token != admin_token:
            raise HTTPException(status_code=403, detail="admin token required")

    async def _json(request: Request):
        try:
            return await request.json()
        except Exception:  # noqa: BLE001 - 본문이 JSON이 아니면 스키마 거부로 기록
            return None

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/time")
    def server_time():
        return {"server_utc": datetime.now(timezone.utc).isoformat()}

    @app.get("/identity")
    def identity():
        c = policy.config  # 실행기가 act 승인 기록의 확정 시간 기준과 실제 정책 설정이 같은지 대조한다
        timing = ({"max_signal_age_sec": c.max_signal_age_sec, "max_future_skew_sec": c.max_future_skew_sec,
                   "max_window_to_decision_sec": c.max_window_to_decision_sec} if c.timing_configured() else None)
        return {"role": ROLE, "version": POLICY_VERSION, "mode": c.mode, "timing": timing,
                "actions_enabled": c.mode == MODE_ACT and policy.state is not None}

    @app.post("/signal/v2")
    async def signal_v2(request: Request):
        body = await _json(request)
        return JSONResponse(await run_in_threadpool(policy.process, body))

    @app.get("/admin/state")
    def state(x_admin_token: Optional[str] = Header(None)):
        _admin(x_admin_token)
        return policy.snapshot()

    @app.post("/admin/context")
    async def set_context(request: Request, x_admin_token: Optional[str] = Header(None)):
        _admin(x_admin_token)
        body = await _json(request)
        try:
            return await run_in_threadpool(policy.set_context, body if isinstance(body, dict) else {})
        except (ValueError, RuntimeError) as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

    @app.post("/admin/context/clear")
    async def clear_context(run_id: str, x_admin_token: Optional[str] = Header(None)):
        _admin(x_admin_token)
        try:
            return await run_in_threadpool(policy.clear_context, run_id)
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

    @app.post("/admin/actions/resolve")
    async def resolve(request: Request, x_admin_token: Optional[str] = Header(None)):
        _admin(x_admin_token)
        b = await _json(request) or {}
        try:
            return await run_in_threadpool(policy.resolve_action, b.get("target_key"), b.get("resolution"),
                                           b.get("evidence"), b.get("by"))
        except (ValueError, RuntimeError) as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

    @app.post("/admin/blocked/clear")
    async def clear_blocked(request: Request, x_admin_token: Optional[str] = Header(None)):
        _admin(x_admin_token)
        b = await _json(request) or {}
        try:
            return await run_in_threadpool(policy.clear_block, b.get("evidence"), b.get("by"))
        except (ValueError, RuntimeError) as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

    @app.post("/webhooks/alertmanager")
    async def alertmanager(request: Request):
        return await run_in_threadpool(policy.record_alertmanager, await _json(request))

    return app


ACT_REQUIRED_ENV = ("GUARDED_MAX_SIGNAL_AGE_SEC", "GUARDED_MAX_FUTURE_SKEW_SEC", "GUARDED_MAX_WINDOW_TO_DECISION_SEC",
                    "GUARDED_SERVICE_CRITERIA_ACK", "GUARDED_COUNTER_MAX_STALENESS_SEC", "GUARDED_ADMIN_TOKEN")
MAX_COUNTER_CALL_TIMEOUT_SEC = 30.0  # Pod별 호출 제한 상한(총 상한은 실행기 서비스 대기 기한)


def act_env_problems(env) -> list:
    """act 시작 전 필수 설정 점검(값 해석까지). 빈 목록이어야 act 시작."""
    probs = [k for k in ACT_REQUIRED_ENV if not env.get(k)]
    if env.get("GUARDED_COUNTER_CALL_TIMEOUT_SEC"):
        try:
            if not 0 < float(env["GUARDED_COUNTER_CALL_TIMEOUT_SEC"]) <= MAX_COUNTER_CALL_TIMEOUT_SEC:
                probs.append("GUARDED_COUNTER_CALL_TIMEOUT_SEC_invalid")
        except ValueError:
            probs.append("GUARDED_COUNTER_CALL_TIMEOUT_SEC_invalid")
    if env.get("GUARDED_SERVICE_CRITERIA_ACK") and env["GUARDED_SERVICE_CRITERIA_ACK"] != SERVICE_CRITERIA_ACK:
        probs.append("GUARDED_SERVICE_CRITERIA_ACK_mismatch")
    # 미래 표본 허용은 선택 값(없으면 0 = 시계 오차 근거 없이는 미래 표본 불통과). 주어지면 숫자 검사.
    for k in ACT_REQUIRED_ENV[:3] + ("GUARDED_COUNTER_MAX_STALENESS_SEC", "GUARDED_COUNTER_MAX_FUTURE_SEC"):
        if env.get(k):
            try:
                v = float(env[k])
                if not (v >= 0 and v != float("inf")):
                    probs.append(f"{k}_invalid")
            except ValueError:
                probs.append(f"{k}_invalid")
    return probs


TIMING_ENV = ACT_REQUIRED_ENV[:3]


def _counter_reader(env, gk, core, ns, http_get):
    """서비스 확인의 Pod별 요청 카운터 출처: Pod IP /metrics 직접(Pod별 호출 제한 GUARDED_COUNTER_CALL_TIMEOUT_SEC, 기본 5초)."""
    t = float(env.get("GUARDED_COUNTER_CALL_TIMEOUT_SEC") or 5.0)
    return gk.pod_metrics_counter_reader(core, ns, http_get=http_get, call_timeout_sec=t, api_timeout_sec=t)


def _k8s(env):
    """실제 연결부는 필요할 때만 불러온다(import만으로 클러스터 접속 없음)."""
    from kubernetes import client, config as kcfg

    import guarded_k8s as gk
    kcfg.load_incluster_config()
    core, custom = client.CoreV1Api(), client.CustomObjectsApi()
    ns = env.get("GUARDED_NAMESPACE", "vllm-serving")
    adapter = gk.K8sAdapter(core=core, custom=custom, namespace=ns, rollout_name=env.get("GUARDED_ROLLOUT", "vllm-serving"),
                            active_service=env.get("GUARDED_ACTIVE_SERVICE", "vllm-active"))
    return gk, core, ns, adapter


def build_app_from_env(env=None, *, k8s_factory=_k8s) -> FastAPI:
    """배포 진입점. 기본 record_only. 모든 모드가 단일 기록자 잠금을 잡는다(겹친 프로세스가 같은 상태를 쓰지 않게).
    상태·감사 기본 경로는 모드별로 분리(/data/guarded/<mode>/), 원장·잠금은 공유(새 상태로 이전 조치 우회 방지).
    record_only 리허설: GUARDED_REHEARSAL_CLUSTER_READ=1이면 읽기 전용 대상 조회를 붙이고, 시간 기준 env 3개가 모두 있으면
    그 값(리허설 기록용 - 확정값 아님)으로 판단까지 해 would_pass_record_only를 남긴다. 승격 함수는 붙이지 않는다.
    act는 모든 명시 설정이 있어야 시작한다."""
    env = dict(os.environ if env is None else env)
    data = Path(env.get("RECOVERY_POLICY_DATA_DIR", "/data")) / "guarded"
    mode = MODE_ACT if env.get("GUARDED_MODE") == MODE_ACT else MODE_RECORD_ONLY
    if mode == MODE_ACT and act_env_problems(env):
        raise SystemExit(f"act 시작 거부 - 설정 문제: {act_env_problems(env)}")
    try:
        wait = float(env.get("GUARDED_SHUTDOWN_WAIT_SEC") or DEFAULT_SHUTDOWN_WAIT_SEC)
    except ValueError:
        wait = float("nan")
    if not (math.isfinite(wait) and wait > 0):
        raise SystemExit(f"GUARDED_SHUTDOWN_WAIT_SEC 잘못됨: {env.get('GUARDED_SHUTDOWN_WAIT_SEC')!r}")
    state_path = Path(env.get("GUARDED_STATE_PATH", data / mode / "state.json"))
    audit = AuditLog(Path(env.get("GUARDED_AUDIT_PATH", data / mode / "audit.jsonl")))
    init = env.get("GUARDED_STATE_INITIALIZE") == "1"
    lock = gl.SingleWriterLock(Path(env.get("GUARDED_LOCK_PATH", data / "writer.lock")))
    lock.acquire(owner=f"guarded-{POLICY_VERSION}-{mode}@{env.get('HOSTNAME', 'unknown-host')}")
    holder = {"policy": None}
    try:
        return _build_locked(env, mode, data, state_path, audit, init, lock, wait, k8s_factory, holder)
    except BaseException as e:  # noqa: BLE001 - 초기화 실패: 안전할 때만 자신의 잠금 해제 후 원래 오류
        release_after_init_failure(lock, holder["policy"], e)
        raise


def _build_locked(env, mode, data, state_path, audit, init, lock, wait, k8s_factory, holder) -> FastAPI:
    if mode == MODE_RECORD_ONLY:
        timing = {}
        if all(env.get(k) for k in TIMING_ENV):
            timing = dict(max_signal_age_sec=float(env[TIMING_ENV[0]]), max_future_skew_sec=float(env[TIMING_ENV[1]]),
                          max_window_to_decision_sec=float(env[TIMING_ENV[2]]))
        reader = k8s_factory(env)[3].read_view if env.get("GUARDED_REHEARSAL_CLUSTER_READ") == "1" else None
        pol = holder["policy"] = GuardedPolicy(GuardConfig(mode=MODE_RECORD_ONLY, **timing), gs.StateStore(state_path),
                                               audit=audit, cluster_reader=reader, initialize=init)
        return create_app(pol, admin_token=env.get("GUARDED_ADMIN_TOKEN"), lock=lock, shutdown_wait_sec=wait)
    ledger = gl.Ledger(Path(env.get("GUARDED_LEDGER_PATH", data / "ledger.jsonl")),
                       create=env.get("GUARDED_LEDGER_CREATE") == "1")
    import requests
    gk, core, ns, adapter = k8s_factory(env)
    checker = gk.ServiceChecker(endpoints_reader=gk.endpoints_reader_from(core, ns, adapter.active_service),
                                counter_reader=_counter_reader(env, gk, core, ns, requests.get),
                                criteria=dict(gk.REUSED_TRAFFIC_CRITERIA),
                                max_counter_staleness_sec=float(env["GUARDED_COUNTER_MAX_STALENESS_SEC"]),
                                max_counter_future_sec=float(env.get("GUARDED_COUNTER_MAX_FUTURE_SEC") or 0.0))
    cfg = GuardConfig(mode=MODE_ACT, max_signal_age_sec=float(env["GUARDED_MAX_SIGNAL_AGE_SEC"]),
                      max_future_skew_sec=float(env["GUARDED_MAX_FUTURE_SKEW_SEC"]),
                      max_window_to_decision_sec=float(env["GUARDED_MAX_WINDOW_TO_DECISION_SEC"]))
    pol = holder["policy"] = GuardedPolicy(cfg, gs.StateStore(state_path), audit=audit, cluster_reader=adapter.read_view,
                                           promote_fn=adapter.promote_once, service_check_fn=checker, ledger=ledger,
                                           initialize=init)
    return create_app(pol, admin_token=env["GUARDED_ADMIN_TOKEN"], lock=lock, shutdown_wait_sec=wait)
