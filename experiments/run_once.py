#!/usr/bin/env python3
"""Phase 8 trial 공통 하네스. experiment-contract.md의 상태머신·TrialResult
스키마를 그대로 구현한다.

상태: PREPARING -> READY -> PROBING -> INJECTING -> OBSERVING -> CLEANING
      -> COMPLETED / INVALID / TIMEOUT

시나리오별 실제 로직(chaos 주입 방법, probe 구현)은 이 파일이 모른다 - Injector/
Prober 어댑터(둘 다 작은 콜백 묶음)를 인자로 받는다. 여기서는 순서·타이밍·
정리·결과기록만 표준화한다(4단계에서 시나리오별 어댑터를 연결).

1차 리뷰(5개)에 이은 2차 리뷰 반영(2026-09-16):
6. safety.py의 action cooldown을 매 trial 시작 전 초기화한다(idempotency는
   유지 - run_id가 키에 포함돼 trial마다 자연히 달라짐). 각 trial은 독립
   표본이어야 하는데, 이전 trial(다른 arm일 수 있음)의 promotion이 남긴
   cooldown이 이번 trial의 조치를 막으면 비교가 왜곡된다. 순서는
   quiescence 확인 -> 활성 context 없음 확인 -> cooldown 초기화 -> 준비 ->
   context 등록 -> 주입(recovery-policy의 /admin/reset-cooldown이 서버
   쪽에서도 이 순서를 재확인). 초기화 자체가 실패하면 invalid_run이 아니라
   HarnessCorrupted(다음 trial까지 오염시킬 수 있는 문제라 배치를 멈춰야 함).
7. prober.stop()을 "실패하면 비치명적"으로 단순 처리하지 않는다 - stop()이
   예외 없이 반환해도 실제로 안 멈췄을 수 있고, 반대로 예외가 나도 이미
   죽어있을 수 있다. 그래서 stop() 시도 후 반드시 is_alive()로 실제 상태를
   재확인한다: 여전히 살아있으면(=다음 trial의 부하·지표를 오염시킬 수 있음)
   HarnessCorrupted, 죽어있으면(stop()이 예외를 냈어도) notes만 남긴다.

2차 리뷰 테스트 중 직접 발견한 버그: 활성 context가 이미 있어서(다른 trial
소유) PREPARING에서 TrialInvalid로 일찍 끝난 trial이, cleanup에서 "자기"
run_id로 clear를 무조건 시도해서 409(그 run_id로 등록한 적이 없으니 당연히
실패)를 critical_failures로 오인하던 것 - context_registered 플래그로
"이 trial이 실제로 자기 context를 등록했는가"를 추적해서, 등록 성공한
경우에만 clear를 시도하도록 수정.

quiescence(이전 trial의 firing 알림이 남아있지 않은지)는 recovery-policy의
GET /admin/quiescent로 확인한다 - PREPARING 진입 시 한 번, CLEANING에서
context clear 직전에 한 번(§6).

정리(prober 종료, chaos 리소스 삭제, experiment context clear)는 전부
finally에서 실행한다. 치명적 실패(action cooldown 초기화 실패, prober가
stop() 이후에도 안 죽음, injector.cleanup() 실패, experiment context clear
실패)는 critical_failures 리스트에 모아뒀다가 함수 끝에서 한 번에
HarnessCorrupted로 던진다 - 결과 파일은 그 전에 이미 기록돼 있으므로
호출자(run_all_scenarios.py, 8단계)가 어떤 trial에서 뭐가 문제였는지 알고
배치를 멈출 수 있다.

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
    """클러스터가 다음 trial을 오염시킬 수 있는 상태로 남았을 가능성 - 이
    trial의 결과는 이미 파일에 기록됐지만, 호출자는 전체 배치를 멈추고
    수동 확인 후 재개해야 한다."""


@dataclass
class Injector:
    """시나리오별 chaos 주입 어댑터(4단계에서 실제 구현 연결).
    prepare()는 느릴 수 있는 준비(pod 생성, 네트워크 안정화 등)를 t_injection
    이전에 끝내기 위한 것 - inject()는 "지금 당장 주입 시작"만 한다.
    is_started()/is_effective()로 실제 효과가 났는지까지 확인해야
    injection_valid=True로 인정한다. is_done()은 정상 종료뿐 아니라 비정상
    종료(예: 프로세스 exit code != 0)도 구분해야 하면 TrialInvalid를 직접
    던져도 된다(run_once()의 OBSERVING 루프가 그대로 상위로 전파해
    outcome=invalid_run으로 처리한다). cleanup()은 prepare()/inject()가
    전혀 안 불렸어도 안전하게 호출 가능해야 한다(idempotent).
    get_actual_injection_time(): 선택 구현 - inject() 호출 시각과 "실제 주입
    효과가 시작된 시각"이 다를 수 있는 어댑터(예: pod 안에서 프로세스를
    백그라운드로 띄우는 경우, 또는 대상의 소멸을 폴링으로 확인하는 경우)를
    위한 것. 이 값은 어댑터가 실측한 정확한 사건 발생 시각이 아니라,
    is_started()가 폴링으로 그 변화를 "처음 관측한" 시각이다. None이 아닌
    값을 반환하면 run_once()가 t_injection을 이 값으로 덮어쓴다 - 미구현
    (None 필드)이면 inject() 호출 시각을 그대로 쓴다.
    get_injection_observation_error_sec(): 선택 구현 - 위 관측 오차의 상한을
    초 단위로 반환한다. poll_interval_sec 같은 설정값을 그대로 쓰면 안 된다
    (2026-09-16 정정 - is_started() 호출 자체의 실행시간·스케줄링 지연이
    설정값을 넘을 수 있어 진짜 상한이 아닐 수 있음). 어댑터가 "대상이
    살아있음을 마지막으로 관측한 시각"과 "처음 사라졌음을 관측한 시각"의
    실측 차이로 계산해야 한다(pod_kill_adapter.py/load_ramp_adapter.py 참고).
    None이면(예: 관측 없이 첫 poll에서 이미 바뀐 상태여서 기준점이 없음)
    run_once()는 injection_observation_error_sec을 기록하지 않는다 - 근거
    없는 상한을 만들어내지 않는다."""
    prepare: Callable[[], None]
    inject: Callable[[], None]
    is_started: Callable[[], bool]
    is_effective: Callable[[], bool]
    is_done: Callable[[], bool]
    cleanup: Callable[[], None]
    get_actual_injection_time: Optional[Callable[[], Optional[str]]] = None
    get_injection_observation_error_sec: Optional[Callable[[], Optional[float]]] = None


@dataclass
class Prober:
    """합성 부하/헬스체크 어댑터(4단계에서 실제 구현 연결). load generator와
    같은 프로세스를 재사용하지 않는다 - arm마다 실제 주입 부하 자체가 다르면
    SLO 판정 표본이 arm 간에 달라져 비교가 왜곡된다(리뷰 지적).
    is_alive(): probe 프로세스 자체가 정상 실행 중인가(측정 대상 서비스의
    상태와 무관 - 서비스가 장애나는 건 관찰 대상이지 probe 고장이 아니다).
    stop() 이후에도 이 함수가 계속 True를 내면 다음 trial이 오염된다.
    check_slo_violation()/check_recovered(): slo-definition.md 기준 판정을
    어댑터가 직접 구현 - run_once()는 언제 물어볼지, t_slo보다 먼저는 안
    묻는다는 순서만 안다. get_actual_slo_time()/get_actual_recovery_time():
    선택 구현 - check_*()가 True를 반환한 poll 시각이 아니라, probe 자체가
    계산한(예: 요청 로그 기반) 더 정밀한 실제 판정 시각을 쓰고 싶을 때.
    None이면 poll 시각(호출 시점)을 그대로 쓴다."""
    start: Callable[[], None]
    is_alive: Callable[[], bool]
    check_slo_violation: Callable[[], bool]
    check_recovered: Callable[[], bool]
    stop: Callable[[], None]
    get_actual_slo_time: Optional[Callable[[], Optional[str]]] = None
    get_actual_recovery_time: Optional[Callable[[], Optional[str]]] = None


@dataclass
class TrialResult:
    run_id: str
    scenario: str
    arm: str
    rep: int
    sequence_index: int
    order_seed: int
    t_run_start: str
    is_pilot: bool = False
    # 재현성 메타데이터(2026-09-16, SLO v2 도입) - probe payload/SLO 산정
    # 기준이 나중에 또 바뀔 수 있으므로 각 trial 결과 JSON 자체에 "이 trial이
    # 어떤 probe·SLO 조건으로 판정됐는지"를 같이 남긴다.
    probe_profile: str = "inference-max1-rps1"
    slo_version: str = "v2"
    latency_slo_sec: Optional[float] = None
    probe_rps: float = 1.0
    t_injection: Optional[str] = None
    # get_injection_observation_error_sec()이 구현+계산 가능했을 때만 채움 -
    # "대상이 살아있음을 마지막으로 관측한 시각"과 "처음 사라졌음을 관측한
    # 시각"의 실측 차이(상한, 정확한 오차 아님). poll_interval_sec 같은
    # 설정값이 아니다 - 어댑터의 조회 자체도 시간이 걸려 설정값만으론 상한을
    # 보장 못 한다(2026-09-16 정정). None이면 관측 기반 값이 아니라는 뜻
    # (어댑터 미구현이거나 기준점이 없음).
    injection_observation_error_sec: Optional[float] = None
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


def _write_result(result: TrialResult, results_dir: Path) -> None:
    # is_pilot=True는 results_dir/pilot/ 아래 별도 경로에 쓴다 - collect_metrics.py가
    # 본 실험 집계에서 파일럿을 note 텍스트 파싱 없이 구조적으로 제외할 수
    # 있게 하기 위함(2026-09-16 지적: 자유 텍스트 notes만으로는 실수로
    # 포함될 위험). results_dir은 run_once() 호출자가 넘긴 값(기본값은
    # 모듈 상수 RESULTS_DIR) - 테스트가 pytest tmp_path로 격리할 수 있게
    # 하기 위해 인자로 뺐다(2026-09-16, dry_run 테스트 산출물이 실제
    # results/에 누적되던 문제 수정).
    out_dir = (results_dir / "pilot") if result.is_pilot else results_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"trial-{result.run_id}.json"
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


def _get_active_experiment_context(arm: str) -> Optional[dict]:
    if arm == "native":
        return None
    resp = requests.get(f"{RECOVERY_POLICY_URL}/admin/experiment-run", timeout=ADMIN_TIMEOUT_SEC)
    resp.raise_for_status()
    return resp.json().get("current")


def _reset_action_cooldown(arm: str) -> None:
    if arm == "native":
        return
    resp = requests.post(f"{RECOVERY_POLICY_URL}/admin/reset-cooldown", timeout=ADMIN_TIMEOUT_SEC)
    resp.raise_for_status()


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
    run_id: Optional[str] = None,
    is_pilot: bool = False,
    probe_profile: str = "inference-max1-rps1",
    slo_version: str = "v2",
    latency_slo_sec: Optional[float] = None,
    probe_rps: float = 1.0,
    results_dir: Optional[Path] = None,
) -> TrialResult:
    results_dir = results_dir or RESULTS_DIR
    # run_id를 밖에서 넘길 수 있게 한 이유: injector/prober는 run_once() 호출
    # *전에* 이미 만들어져 있어야 하는데(인자로 받으므로), 그 어댑터들이
    # ramp.py/probe.py의 raw 로그에 태깅하는 run_id와 여기서 쓰는 run_id가
    # 어긋나면 trial 결과 JSON과 raw 로그를 나중에 못 join한다. 호출자가 먼저
    # run_id를 만들어 어댑터 생성과 이 호출에 동일하게 넘기면 된다. 안 넘기면
    # (기존 테스트들처럼) 이전과 동일하게 자동 생성.
    run_id = run_id or f"{scenario}-{arm}-{rep:02d}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    result = TrialResult(
        run_id=run_id, scenario=scenario, arm=arm, rep=rep,
        sequence_index=sequence_index, order_seed=order_seed, t_run_start=_now(),
        is_pilot=is_pilot, probe_profile=probe_profile, slo_version=slo_version,
        latency_slo_sec=latency_slo_sec, probe_rps=probe_rps,
    )
    _write_result(result, results_dir)
    critical_failures: list = []
    context_registered = False  # 이 trial이 실제로 자기 context를 등록했는지 - cleanup에서
    # 등록도 안 한 context를 clear하려다 409(다른 trial 소유)로 오탐되는 걸 막기 위함

    try:
        # PREPARING - 순서 고정: quiescence -> 활성 context 없음 확인 ->
        # cooldown 초기화 -> (느릴 수 있는) 주입 준비.
        if not _wait_for_quiescence(arm):
            raise TrialInvalid("trial 시작 전 quiescence(이전 alert 해소) 확인 실패")

        active_ctx = _get_active_experiment_context(arm)
        if active_ctx is not None:
            raise TrialInvalid(f"trial 시작 전인데 이미 활성 실험 있음: {active_ctx.get('run_id')}")

        try:
            _reset_action_cooldown(arm)
        except Exception as e:
            msg = f"action cooldown 초기화 실패: {e}"
            critical_failures.append(msg)
            raise TrialInvalid(msg)

        injector.prepare()
        _write_result(result, results_dir)

        result.state = TrialState.PROBING.value
        prober.start()
        result.probe_valid = _wait_for(prober.is_alive, probe_ready_timeout_sec, poll_interval_sec)
        _write_result(result, results_dir)
        if not result.probe_valid:
            raise TrialInvalid("probe가 시작 후 정상 상태에 도달 못 함")

        result.state = TrialState.READY.value
        _register_experiment_context(run_id, scenario, arm, rep, result.t_run_start)
        context_registered = True
        _write_result(result, results_dir)

        result.state = TrialState.INJECTING.value
        result.t_injection = _now()
        injector.inject()
        started = _wait_for(injector.is_started, injection_started_timeout_sec, poll_interval_sec)
        result.injection_valid = started and injector.is_effective()
        if result.injection_valid and injector.get_actual_injection_time is not None:
            precise = injector.get_actual_injection_time()
            if precise:
                result.t_injection = precise  # inject() 호출 시각보다 정확 - 다만 폴링 관측값
                if injector.get_injection_observation_error_sec is not None:
                    result.injection_observation_error_sec = injector.get_injection_observation_error_sec()
        _write_result(result, results_dir)
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
                precise = prober.get_actual_slo_time() if prober.get_actual_slo_time else None
                result.t_slo = precise or _now()
            # t_slo가 찍히기 전에는 check_recovered()를 묻지 않는다 - 주입
            # 직후 아직 멀쩡한 순간을 "회복됨"으로 오판정하는 걸 막기 위함.
            if result.t_slo is not None and result.t_recovery is None and prober.check_recovered():
                precise = prober.get_actual_recovery_time() if prober.get_actual_recovery_time else None
                result.t_recovery = precise or _now()
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
        _write_result(result, results_dir)

        try:
            prober.stop()
        except Exception as e:
            result.notes += f"prober.stop() 예외(아래 is_alive 재확인으로 최종 판단): {e} | "
        # stop()이 예외 없이 반환해도 실제로 안 멈췄을 수 있고, 반대로
        # 예외가 나도 이미 죽어있을 수 있다 - is_alive()로 실측한다.
        if prober.is_alive():
            msg = "prober.stop() 이후에도 probe가 여전히 살아있음 - 다음 trial의 부하·지표 오염 위험"
            critical_failures.append(msg)
            result.notes += f"CRITICAL: {msg} | "

        try:
            injector.cleanup()
        except Exception as e:
            msg = f"injector.cleanup() 실패: {e}"
            critical_failures.append(msg)
            result.notes += f"{msg} | "

        if context_registered and not critical_failures:
            _wait_for_quiescence(arm)  # 정리 후 잔여 alert도 가능하면 해소되길 기다림(최선 노력)
            try:
                _clear_experiment_context(run_id, arm)
            except Exception as e:
                msg = f"experiment-run clear 실패: {e}"
                critical_failures.append(msg)
                result.notes += f"{msg} | "

        result.t_run_end = _now()
        result.state = _FINAL_STATE_BY_OUTCOME.get(result.outcome, result.state).value
        _write_result(result, results_dir)

    if critical_failures:
        result_path = (results_dir / "pilot" if is_pilot else results_dir) / f"trial-{run_id}.json"
        raise HarnessCorrupted(
            f"{run_id}: {'; '.join(critical_failures)} - 클러스터가 다음 trial을 오염시켰을 수 있음, "
            f"수동 확인 필요. 결과는 {result_path}에 기록됨"
        )
    return result
