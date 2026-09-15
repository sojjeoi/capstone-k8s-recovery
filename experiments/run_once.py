#!/usr/bin/env python3
"""Phase 8 trial 공통 하네스. experiment-contract.md의 상태머신·TrialResult
스키마를 그대로 구현한다.

상태: PREPARING -> READY -> PROBING -> INJECTING -> OBSERVING -> CLEANING
      -> COMPLETED / INVALID / TIMEOUT

시나리오별 실제 로직(chaos 주입 방법, probe 구현)은 이 파일이 모른다 - Injector/
Prober 어댑터(둘 다 작은 콜백 묶음)를 인자로 받는다. 여기서는 순서·타이밍·
정리·결과기록만 표준화한다(4단계에서 시나리오별 어댑터를 연결).

정리(prober 종료, chaos 리소스 삭제, experiment context clear)는 전부
finally에서 실행 - 중간에 어떤 예외가 나도 다음 trial이 오염된 상태를
물려받지 않는다. t_recovery가 찍혀도 즉시 안 끝난다 - chaos 자체가 끝나고
(t_injection_end) 정상 상태까지 확인된 뒤에야 관찰을 마친다(계약서 §4).

결과는 trial 시작 시점부터 매 상태 전이마다 임시파일+rename으로 덮어써서,
도중에 프로세스가 죽어도 마지막으로 기록된 상태가 파일에 남는다.
"""
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

import requests

RESULTS_DIR = Path(__file__).parent / "results"
RECOVERY_POLICY_URL = "http://localhost:8080"  # 로컬 실행 전제 - kubectl port-forward -n vllm-serving svc/recovery-policy 8080:8080
ADMIN_TIMEOUT_SEC = 10
PROBE_READY_TIMEOUT_SEC = 30


class TrialState(str, Enum):
    PREPARING = "preparing"
    READY = "ready"
    PROBING = "probing"
    INJECTING = "injecting"
    OBSERVING = "observing"
    CLEANING = "cleaning"
    COMPLETED = "completed"
    INVALID = "invalid"
    TIMEOUT = "timeout"


class TrialInvalid(Exception):
    """probe·주입·사전조건 문제로 이 trial을 무효 처리해야 할 때만 올린다."""


@dataclass
class Injector:
    """시나리오별 chaos 주입 어댑터(4단계에서 실제 구현 연결).
    inject()는 즉시 반환(비동기 시작) - chaos 자체가 끝났는지는 is_done()으로
    폴링한다. cleanup()은 반드시 idempotent해야 함(finally에서 여러 상황에
    호출될 수 있음)."""
    inject: Callable[[], None]
    is_done: Callable[[], bool]
    cleanup: Callable[[], None]


@dataclass
class Prober:
    """합성 부하/헬스체크 어댑터(4단계에서 실제 구현 연결).
    check_recovered()는 slo-definition.md 기준 판정(위반 해소 30초 연속)을
    어댑터가 직접 구현 - run_once()는 언제 물어볼지만 안다."""
    start: Callable[[], None]
    is_healthy: Callable[[], bool]
    check_recovered: Callable[[], bool]
    stop: Callable[[], None]


@dataclass
class TrialResult:
    run_id: str
    scenario: str
    arm: str
    rep: int
    sequence_index: int
    order_seed: int
    t_run_start: str
    t_injection: Optional[str] = None
    t_injection_end: Optional[str] = None
    t_detection: Optional[str] = None
    t_decision: Optional[str] = None
    t_api_request: Optional[str] = None
    t_switch: Optional[str] = None
    t_slo: Optional[str] = None
    t_recovery: Optional[str] = None
    t_audit_write: Optional[str] = None
    t_audit_push: Optional[str] = None
    commit_sha: Optional[str] = None
    detected: bool = False
    detection_source: Optional[str] = None
    action: str = "none"
    promotion_verified: Optional[bool] = None
    outcome: Optional[str] = None
    injection_valid: bool = False
    probe_valid: bool = False
    invalid_reason: Optional[str] = None
    p95_peak: Optional[float] = None
    availability_min: Optional[float] = None
    t_run_end: Optional[str] = None
    notes: str = ""
    state: str = field(default=TrialState.PREPARING.value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _wait_for(check: Callable[[], bool], timeout: float, interval: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(interval)
    return False


def _write_result(result: TrialResult) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"trial-{result.run_id}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)  # 원자적 교체


def _register_experiment_context(run_id: str, scenario: str, arm: str, rep: int, started_at: str) -> None:
    if arm == "native":
        return  # native는 recovery-policy 자체가 안 떠있음(계약서 §1)
    resp = requests.post(
        f"{RECOVERY_POLICY_URL}/admin/experiment-run",
        json={"run_id": run_id, "scenario": scenario, "arm": arm, "rep": rep, "started_at": started_at},
        timeout=ADMIN_TIMEOUT_SEC,
    )
    resp.raise_for_status()


def _clear_experiment_context(run_id: str, arm: str, notes: list) -> None:
    if arm == "native":
        return
    try:
        resp = requests.post(
            f"{RECOVERY_POLICY_URL}/admin/experiment-run/clear",
            params={"run_id": run_id}, timeout=ADMIN_TIMEOUT_SEC,
        )
        resp.raise_for_status()
    except Exception as e:
        notes.append(f"experiment-run clear 실패: {e}")


def run_once(
    scenario: str, arm: str, rep: int, sequence_index: int, order_seed: int,
    injector: Injector, prober: Prober, timeout_sec: float, poll_interval_sec: float = 1.0,
    probe_ready_timeout_sec: float = PROBE_READY_TIMEOUT_SEC,
) -> TrialResult:
    run_id = f"{scenario}-{arm}-{rep:02d}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    result = TrialResult(
        run_id=run_id, scenario=scenario, arm=arm, rep=rep,
        sequence_index=sequence_index, order_seed=order_seed, t_run_start=_now(),
    )
    _write_result(result)
    cleanup_notes: list = []

    try:
        result.state = TrialState.READY.value
        _register_experiment_context(run_id, scenario, arm, rep, result.t_run_start)
        _write_result(result)

        result.state = TrialState.PROBING.value
        prober.start()
        result.probe_valid = _wait_for(prober.is_healthy, probe_ready_timeout_sec, poll_interval_sec)
        _write_result(result)
        if not result.probe_valid:
            raise TrialInvalid("probe가 시작 후 healthy 상태에 도달 못 함")

        result.state = TrialState.INJECTING.value
        result.t_injection = _now()
        injector.inject()
        result.injection_valid = True
        _write_result(result)

        result.state = TrialState.OBSERVING.value
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if not prober.is_healthy():
                result.probe_valid = False
                raise TrialInvalid("probe가 관찰 도중 비정상 종료")
            if injector.is_done() and result.t_injection_end is None:
                result.t_injection_end = _now()
            if result.t_recovery is None and prober.check_recovered():
                result.t_recovery = _now()
            if result.t_recovery is not None and result.t_injection_end is not None:
                break
            time.sleep(poll_interval_sec)
        else:
            result.outcome = "timeout"
            result.state = TrialState.TIMEOUT.value

        if result.outcome is None:
            result.outcome = "recovered"

    except TrialInvalid as e:
        result.outcome = "invalid_run"
        result.invalid_reason = str(e)
        result.state = TrialState.INVALID.value
    except Exception as e:
        result.outcome = "invalid_run"
        result.invalid_reason = f"예외: {type(e).__name__}: {e}"
        result.state = TrialState.INVALID.value
    finally:
        result.state = TrialState.CLEANING.value
        _write_result(result)
        try:
            prober.stop()
        except Exception as e:
            cleanup_notes.append(f"prober.stop() 실패: {e}")
        try:
            injector.cleanup()
        except Exception as e:
            cleanup_notes.append(f"injector.cleanup() 실패: {e}")
        _clear_experiment_context(run_id, arm, cleanup_notes)

        if cleanup_notes:
            result.notes = (result.notes + " | " if result.notes else "") + " | ".join(cleanup_notes)
        result.t_run_end = _now()
        if result.state == TrialState.CLEANING.value:
            # 정상 관찰이 outcome까지 정했으면 마지막 상태는 COMPLETED로 마무리
            result.state = TrialState.COMPLETED.value if result.outcome == "recovered" else result.state
        _write_result(result)

    return result
