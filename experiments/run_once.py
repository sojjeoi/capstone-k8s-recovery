#!/usr/bin/env python3
"""Phase 8 trial 공통 하네스. experiment-contract.md의 상태머신·TrialResult
스키마를 그대로 구현한다.

상태: PREPARING -> READY -> PROBING -> INJECTING -> OBSERVING -> CLEANING
      -> COMPLETED / INVALID / TIMEOUT

시나리오별 실제 로직(chaos 주입 방법, probe 구현)은 이 파일이 모른다 - Injector/
Prober 어댑터(둘 다 작은 콜백 묶음)를 인자로 받는다. 여기서는 순서·타이밍·
정리·결과기록만 표준화한다(4단계에서 시나리오별 어댑터를 연결).

1차 구현 리뷰에서 발견된 5개 문제를 반영(2026-09-16):
1. Prober의 "생존"과 "SLO 위반"을 분리 - 관찰 도중 서비스가 실제로 장애나는
   건 측정 대상이지 probe 고장이 아니다. is_alive()(probe 프로세스 생존)와
   check_slo_violation()(서비스가 지금 SLO 위반 중인지)을 나눴다.
2. t_slo가 찍히기 전에는 check_recovered()를 아예 안 물어본다 - 안 그러면
   주입 직후 아직 멀쩡한 순간에 "회복됨"으로 오판정될 수 있다. 끝까지
   t_slo가 안 찍히면 outcome은 recovered가 아니라 prevented 후보(최종 판정은
   collect_metrics.py가 native 반복과 교차검증 - run_once()는 provisional만).
3. Injector에 prepare()/is_started()/is_effective()를 추가 - inject()가
   예외 없이 반환한 것만으로 "주입 성공"을 인정하지 않는다.
4. finally에서 모든 state를 CLEANING으로 찍은 뒤 recovered만 복원하던 버그
   수정 - outcome->최종 state 매핑을 명시적으로 둔다.
5. Injector.prepare()를 t_injection 이전(PREPARING)으로 분리 - load_ramp
   어댑터(4단계)가 pod 생성·60초 안정화를 prepare()에 넣으면 t_injection이
   실제 부하 시작 시각과 일치하게 된다(이 파일 자체는 시나리오를 모르므로
   인터페이스만 제공).

quiescence(이전 trial의 firing 알림이 남아있지 않은지)는 recovery-policy의
GET /admin/quiescent로 확인한다 - PREPARING 진입 시 한 번, CLEANING에서
context clear 직전에 한 번(§6).

정리(prober 종료, chaos 리소스 삭제, experiment context clear)는 전부
finally에서 실행한다. 그 중 injector.cleanup()과 experiment context clear는
실패하면 클러스터가 다음 trial을 오염시킬 수 있는 치명적 상황이라, notes에만
남기지 않고 HarnessCorrupted를 던져서 호출자(run_all_scenarios.py, 8단계)가
전체 배치를 멈출 수 있게 한다. prober.stop() 실패는 상대적으로 덜 위험하다고
판단해(잔여 load가 측정 노이즈는 될 수 있어도 다음 trial의 주입 자체를
막지는 않음) notes만 남긴다 - 이 비대칭은 판단이 갈릴 수 있는 지점이라 명시.

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
INJECTION_STARTED_TIMEOUT_SEC = 30
QUIESCENCE_TIMEOUT_SEC = 60
QUIESCENCE_POLL_SEC = 3


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


_FINAL_STATE_BY_OUTCOME = {
    "recovered": TrialState.COMPLETED,
    "prevented": TrialState.COMPLETED,
    "timeout": TrialState.TIMEOUT,
    "invalid_run": TrialState.INVALID,
}


class TrialInvalid(Exception):
    """probe·주입·사전조건 문제로 이 trial만 무효 처리하면 되는 경우."""


class HarnessCorrupted(Exception):
    """정리(특히 chaos 리소스 삭제, experiment context clear)가 실패해서
    클러스터가 다음 trial을 오염시킬 수 있는 상태로 남았을 가능성 - 이 trial의
    결과는 이미 파일에 기록됐지만, 호출자는 전체 배치를 멈추고 수동 확인 후
    재개해야 한다."""


@dataclass
class Injector:
    """시나리오별 chaos 주입 어댑터(4단계에서 실제 구현 연결).
    prepare()는 느릴 수 있는 준비(pod 생성, 네트워크 안정화 등)를 t_injection
    이전에 끝내기 위한 것 - inject()는 "지금 당장 주입 시작"만 한다.
    is_started()/is_effective()로 실제 효과가 났는지까지 확인해야
    injection_valid=True로 인정한다. cleanup()은 idempotent해야 함(finally에서
    여러 상황에 호출될 수 있음)."""
    prepare: Callable[[], None]
    inject: Callable[[], None]
    is_started: Callable[[], bool]
    is_effective: Callable[[], bool]
    is_done: Callable[[], bool]
    cleanup: Callable[[], None]


@dataclass
class Prober:
    """합성 부하/헬스체크 어댑터(4단계에서 실제 구현 연결).
    is_alive(): probe 프로세스 자체가 정상 실행 중인가(측정 대상 서비스의
    상태와 무관 - 서비스가 장애나는 건 관찰 대상이지 probe 고장이 아니다).
    check_slo_violation()/check_recovered(): slo-definition.md 기준 판정을
    어댑터가 직접 구현 - run_once()는 언제 물어볼지, t_slo보다 먼저는 안
    묻는다는 순서만 안다."""
    start: Callable[[], None]
    is_alive: Callable[[], bool]
    check_slo_violation: Callable[[], bool]
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


def _is_quiescent() -> bool:
    try:
        resp = requests.get(f"{RECOVERY_POLICY_URL}/admin/quiescent", timeout=ADMIN_TIMEOUT_SEC)
        resp.raise_for_status()
        return bool(resp.json().get("quiescent"))
    except Exception:
        return False  # 확인 자체가 안 되면 조용하다고 우기지 않는다


def _wait_for_quiescence(arm: str) -> bool:
    if arm == "native":
        return True  # native는 recovery-policy가 안 떠있어 확인 대상 자체가 없음
    return _wait_for(_is_quiescent, QUIESCENCE_TIMEOUT_SEC, QUIESCENCE_POLL_SEC)


def _register_experiment_context(run_id: str, scenario: str, arm: str, rep: int, started_at: str) -> None:
    if arm == "native":
        return  # native는 recovery-policy 자체가 안 떠있음(계약서 §1)
    resp = requests.post(
        f"{RECOVERY_POLICY_URL}/admin/experiment-run",
        json={"run_id": run_id, "scenario": scenario, "arm": arm, "rep": rep, "started_at": started_at},
        timeout=ADMIN_TIMEOUT_SEC,
    )
    resp.raise_for_status()


def _clear_experiment_context(run_id: str, arm: str) -> None:
    if arm == "native":
        return
    resp = requests.post(
        f"{RECOVERY_POLICY_URL}/admin/experiment-run/clear",
        params={"run_id": run_id}, timeout=ADMIN_TIMEOUT_SEC,
    )
    resp.raise_for_status()


def run_once(
    scenario: str, arm: str, rep: int, sequence_index: int, order_seed: int,
    injector: Injector, prober: Prober, timeout_sec: float, poll_interval_sec: float = 1.0,
    probe_ready_timeout_sec: float = PROBE_READY_TIMEOUT_SEC,
    injection_started_timeout_sec: float = INJECTION_STARTED_TIMEOUT_SEC,
) -> TrialResult:
    run_id = f"{scenario}-{arm}-{rep:02d}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    result = TrialResult(
        run_id=run_id, scenario=scenario, arm=arm, rep=rep,
        sequence_index=sequence_index, order_seed=order_seed, t_run_start=_now(),
    )
    _write_result(result)
    critical_failure: Optional[str] = None

    try:
        # PREPARING - quiescence 확인 + 느릴 수 있는 주입 준비(pod 생성 등)를
        # t_injection 이전에 끝낸다.
        if not _wait_for_quiescence(arm):
            raise TrialInvalid("trial 시작 전 quiescence(이전 alert 해소) 확인 실패")
        injector.prepare()
        _write_result(result)

        result.state = TrialState.PROBING.value
        prober.start()
        result.probe_valid = _wait_for(prober.is_alive, probe_ready_timeout_sec, poll_interval_sec)
        _write_result(result)
        if not result.probe_valid:
            raise TrialInvalid("probe가 시작 후 정상 상태에 도달 못 함")

        result.state = TrialState.READY.value
        _register_experiment_context(run_id, scenario, arm, rep, result.t_run_start)
        _write_result(result)

        result.state = TrialState.INJECTING.value
        result.t_injection = _now()
        injector.inject()
        started = _wait_for(injector.is_started, injection_started_timeout_sec, poll_interval_sec)
        result.injection_valid = started and injector.is_effective()
        _write_result(result)
        if not result.injection_valid:
            raise TrialInvalid("주입이 시작됐는지/효과가 있었는지 확인 안 됨")

        result.state = TrialState.OBSERVING.value
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if not prober.is_alive():
                result.probe_valid = False
                raise TrialInvalid("probe가 관찰 도중 비정상 종료")
            if injector.is_done() and result.t_injection_end is None:
                result.t_injection_end = _now()
            if result.t_slo is None and prober.check_slo_violation():
                result.t_slo = _now()
            # t_slo가 찍히기 전에는 check_recovered()를 묻지 않는다 - 주입
            # 직후 아직 멀쩡한 순간을 "회복됨"으로 오판정하는 걸 막기 위함.
            if result.t_slo is not None and result.t_recovery is None and prober.check_recovered():
                result.t_recovery = _now()
            if result.t_injection_end is not None and (
                result.t_slo is None or result.t_recovery is not None
            ):
                break
            time.sleep(poll_interval_sec)
        else:
            result.outcome = "timeout"

        if result.outcome is None:
            # t_slo가 끝까지 안 찍혔으면 prevented 후보(최종 판정은
            # collect_metrics.py가 같은 시나리오 native 반복과 교차검증해서
            # 확정 - 계약서 §3의 3조건 중 하나는 여기서 알 수 없음).
            result.outcome = "prevented" if result.t_slo is None else "recovered"

    except TrialInvalid as e:
        result.outcome = "invalid_run"
        result.invalid_reason = str(e)
    except Exception as e:
        result.outcome = "invalid_run"
        result.invalid_reason = f"예외: {type(e).__name__}: {e}"
    finally:
        result.state = TrialState.CLEANING.value
        _write_result(result)

        try:
            prober.stop()
        except Exception as e:
            result.notes += f"prober.stop() 실패(비치명적): {e} | "

        try:
            injector.cleanup()
        except Exception as e:
            critical_failure = f"injector.cleanup() 실패: {e}"
            result.notes += f"{critical_failure} | "

        if critical_failure is None:
            _wait_for_quiescence(arm)  # 정리 후 잔여 alert도 가능하면 해소되길 기다림(최선 노력)
            try:
                _clear_experiment_context(run_id, arm)
            except Exception as e:
                critical_failure = f"experiment-run clear 실패: {e}"
                result.notes += f"{critical_failure} | "

        result.t_run_end = _now()
        result.state = _FINAL_STATE_BY_OUTCOME.get(result.outcome, result.state).value
        _write_result(result)

    if critical_failure:
        raise HarnessCorrupted(
            f"{run_id}: {critical_failure} - 클러스터가 다음 trial을 오염시켰을 수 있음, "
            f"수동 확인 필요. 결과는 {RESULTS_DIR / f'trial-{run_id}.json'}에 기록됨"
        )
    return result
