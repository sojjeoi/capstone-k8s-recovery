#!/usr/bin/env python3
"""§182(2026-09-27) - 기존 shadow CPU 규칙(30%·2연속, demo_early_warning_
rule.py, 미변경)이 "실제 대응"(자동 전환)까지 이어질 수 있는지 확인하는,
**공식 §176과 완전히 분리된 새 시연 버전**. §176/§180의 기존 조각만
재사용하고 새 판정 로직은 추가하지 않는다.

**최소 경로(사전 조사로 확인, recovery-policy 코드 전부 미변경)**:
recovery-policy/policy.py의 policy.decide()는 signal.signal_type(=
"anomaly_risk")과 ctx.preview_ready(rollouts_client.is_paused_pre_promotion())
만 보고 조치를 정한다 - signal의 detector 문자열이나 score 값은 판정에
전혀 안 쓰인다(순수 pass-through, 감사기록용). 즉 recovery-policy는
"누가 보냈는지" 구조적으로 구분하지 않고, 기존 POST /signal에 유효한
anomaly_risk 신호가 오고 preview가 이미 준비돼 있으면 항상 promote_
preview로 판정한다 - 이것이 이 파일이 기대는 유일한 기존 확장점이다.

**공식 IsolationForest와의 분리(구조적으로 보장)**:
- arm_controller.make_detector_for_arm()을 이 파일 어디서도 import·호출
  하지 않는다 - 공식 탐지기 프로세스 자체가 뜨지 않는다.
- run_once.run_once()도 호출하지 않는다(§180과 동일 이유 + 하나 더:
  run_once()가 만드는 결과 파일은 공식 TrialResult와 완전히 같은 스키마라
  reconcile_audit.py/comparison.csv 같은 공식 집계 스크립트가 이 시연
  결과를 진짜 실험으로 착각해 섞어 셀 위험이 있다 - 이 파일은 완전히
  다른 스키마·별도 디렉터리에만 쓴다).
- POST /admin/experiment-run(공식 실험 컨텍스트 등록)도 호출하지 않는다.
  recovery-policy/main.py 확인 결과 _record_detection() 등은 전부
  "signal의 experiment_run_id == _current_experiment.run_id"를 먼저
  확인하고서만 공식 상태(t_detection 등)를 기록한다 - 이 파일은 그
  등록 자체를 안 하므로 _current_experiment를 구조적으로 절대 못
  건드린다(이 파일의 run_id는 scenario 접두어부터 공식 run_id와 겹치지
  않아 우연 일치도 없다). 결과는 POST /signal의 HTTP 응답(DecisionRecord)
  에서 직접 읽는다.
- 신호의 detector 필드를 SHADOW_DETECTOR_TAG("shadow_cpu_rule")로 고정한다
  - 코드 어디서도 "isolation_forest"를 쓰지 않는다. 승격이 일어나도 결과는
  "CPU 규칙 기반 대응"으로만 기록되고, 공식 IsolationForest의 선제 탐지
  성과로 절대 표시되지 않는다.

**안전 감시 범위가 §180/§181과 다른 이유**: §180/§181은 "아무 일도 없어야
정상"인 무주입 관찰이라 active/preview OOMKilled·재시작·SLO 위반을 전부
즉시 중단 사유로 봤다. 이 버전은 실제 CPU 압박을 주입해 "시스템이 버티는지
/회복하는지"를 보는 게 목적이므로 그 셋은 예상 가능한 결과이지 하네스
이상이 아니다(공식 run_once.py의 OBSERVING 단계도 코드 확인 결과 이 셋을
이유로 중단하지 않는다 - detector 신호나 timeout만 기다린다). 그래서 이
버전의 안전 감시는 "하네스 자체가 깨졌다"고 볼 수 있는 것만 본다: probe
생존(죽으면 이후 CPU/지연 자료 전체가 무의미해짐), active pod 식별
가능 여부(0개/2개 이상 - selector 자체가 깨짐), Node 상태.

**중복 신호 방어(2중)**: 이 러너는 발화 시 최초 1회만 보낸다(로컬
already-sent 플래그) - 그리고 설령 실수로 두 번 보내도 recovery-policy/
safety.py의 기존 idempotency_key(experiment_run_id:signal_type) 중복
차단이 그대로 적용돼 서버가 두 번째를 SKIPPED_DUPLICATE로 무시한다
(미변경 기존 동작, 코드 확인됨).

**알려진 단순화**: t_injection_observed는 run_once.py와 동일하게
injector.is_started()/get_actual_injection_time()을 그대로 재사용해
얻는다(새 근사치 아님) - 다만 injection_observation_error_sec 같은
오차 상한 필드는 이 버전엔 없다(§176 수준 정밀도는 필요시 별도 승인으로
추가 가능, 이번 범위 아님)."""
import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

import arm_controller
import node_resource_safety_gate
from active_pod_resolver import get_active_pods
from cpu_pressure_demo_adapter import make_cpu_pressure_injector
from demo_cpu_per_pod_tracer import (
    MIN_STABLE_BASELINE_TICKS, analyze_shadow_trace, wrap_prober_with_pre_injection_tick_gate,
)
from demo_early_warning_rule import CONSECUTIVE_REQUIRED, RELATIVE_RISE_FACTOR, get_threshold
from load_ramp_adapter import make_load_ramp_prober
from memory_pressure_adapter import _node_healthy, get_node_conditions
from run_cpu_demo_trial import DEMO_STAGES
from run_once import INJECTION_STARTED_TIMEOUT_SEC, HarnessCorrupted, TrialInvalid

DEFAULT_PROBE_CONFIG = Path(__file__).parent.parent / "chaos" / "probe-config.yaml"
PLAN_ID = "capstone-cpu-demo-shadow-response-182-v1"
DEMO_RESULTS_DIR = Path(__file__).parent / "results" / "capstone_cpu_demo_shadow_response"

# kubectl port-forward -n vllm-serving svc/recovery-policy 8080:8080 전제 - run_once.py의
# RECOVERY_POLICY_URL과 동일 전제(이 파일은 run_once.py를 import하지 않으므로 값을 다시 적는다).
RECOVERY_POLICY_URL = "http://localhost:8080"
SHADOW_DETECTOR_TAG = "shadow_cpu_rule"  # 절대 "isolation_forest"로 두지 않음 - §182 핵심 요구사항
# score_server.py의 SIGNAL_CONNECT_TIMEOUT_SEC/SIGNAL_READ_TIMEOUT_SEC 값을 그대로 인용
# (import는 안 함 - anomaly-detection/ 쪽 코드에 대한 의존을 구조적으로 만들지 않기 위해).
SIGNAL_CONNECT_TIMEOUT_SEC = 5.0
SIGNAL_READ_TIMEOUT_SEC = 45.0

# §176의 NEW_BASELINE_TIMEOUT_SEC(8 tick 상한 문제, 동일 원인)을 그대로 인용.
BASELINE_TIMEOUT_SEC = 240.0
# §176의 TIMEOUT_SEC(주입 후 관찰 예산)을 그대로 인용.
POST_INJECTION_OBSERVATION_SEC = 480.0
# §179의 여유값을 그대로 인용.
PROBE_INJECTION_START_MARGIN_SEC = 30.0
PROBE_LIFETIME_SEC = BASELINE_TIMEOUT_SEC + PROBE_INJECTION_START_MARGIN_SEC + POST_INJECTION_OBSERVATION_SEC
SAFETY_POLL_INTERVAL_SEC = 5.0
PROBE_READY_TIMEOUT_SEC = 30.0

_CONTRACT_FILES = [
    Path(__file__),
    Path(__file__).parent / "cpu_pressure_demo_adapter.py",
    Path(__file__).parent / "demo_cpu_per_pod_tracer.py",
    Path(__file__).parent / "demo_early_warning_rule.py",
    Path(__file__).parent / "load_ramp_adapter.py",
    Path(__file__).parent / "arm_controller.py",
    Path(__file__).parent.parent / "recovery-policy" / "main.py",
    Path(__file__).parent.parent / "recovery-policy" / "policy.py",
    Path(__file__).parent.parent / "recovery-policy" / "safety.py",
    Path(__file__).parent.parent / "recovery-policy" / "schemas.py",
    Path(__file__).parent.parent / "recovery-policy" / "rollouts_client.py",
]


def _contract_hashes() -> dict:
    out = {}
    for p in _CONTRACT_FILES:
        try:
            out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError as e:
            out[p.name] = f"미확인({e})"
    return out


def _wait_for(check_fn, timeout: float, interval: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check_fn():
            return True
        time.sleep(interval)
    return False


def _check_safety_or_raise(
    baseline_state: dict,
    prober,
    get_active_pods_fn=get_active_pods,
    get_node_conditions_fn=get_node_conditions,
) -> None:
    """실제 주입이 있는 버전의 안전 감시 - 모듈 docstring 참고. active/
    preview OOMKilled·재시작·SLO 위반은 여기서 안 본다(예상 가능한 결과 -
    공식 run_once.py도 이 셋으로 OBSERVING을 안 멈춘다). probe 생존·active
    pod 식별 가능 여부·Node 상태만 하네스 이상으로 본다."""
    if not prober.is_alive():
        raise TrialInvalid("주입 관찰 중 probe 프로세스 종료 감지(예상 밖) - 이후 CPU·지연 자료 무의미")
    active = get_active_pods_fn()
    if len(active) != 1:
        raise HarnessCorrupted(f"active pod 식별 모호/소실(주입 관찰 중 예상 밖 - selector 자체 이상): {active}")
    conditions = get_node_conditions_fn(active[0]["node_name"]) if "node_name" in active[0] else None
    if conditions is not None and not _node_healthy(conditions):
        raise HarnessCorrupted(f"주입 관찰 중 Node 상태 이상: {conditions}")


def _send_shadow_signal(shadow_result: dict, run_id: str) -> dict:
    """shadow 발화 시점 값만으로 기존 미변경 POST /signal에 신호를 보낸다.
    payload 구조·timeout 값은 score_server.py의 post_to_recovery_policy()와
    같은 서버 계약을 향해 같은 모양으로 맞추되, 그 모듈을 import하지 않는다
    (anomaly-detection/ 쪽 코드 의존을 구조적으로 만들지 않기 위해 - 모듈
    docstring 참고). detector는 항상 SHADOW_DETECTOR_TAG로 고정한다.

    반환값은 세 단계를 명확히 구분한다(2026-09-27 - HTTP 예외·실패 응답을
    "전송 완료"로 뭉뚱그리지 말라는 지시 반영):
    - attempted: 이 함수가 호출됐다는 사실 자체(항상 True - 호출 자체가
      "시도"이므로).
    - accepted_by_server: HTTP 2xx만 True. 예외·4xx/5xx는 전부 False.
    - promotion_verified: recovery-policy/main.py의 _promotion_verified()와
      정확히 같은 기준(outcome == "executed_verified")을 클라이언트 쪽에서
      그대로 재현 - 그 값이 True일 때만 실제 승격이 검증됐다는 뜻이다.
      accepted_by_server가 False면 이 값도 항상 False(서버 응답을 못
      받았는데 승격을 확인할 방법이 없음)."""
    payload = {
        "signal_type": "anomaly_risk",
        "score": shadow_result["fired_cores"],
        "timestamp": shadow_result["fired_at_utc"],
        "detector": SHADOW_DETECTOR_TAG,
        "experiment_run_id": run_id,
        "threshold": get_threshold(shadow_result["baseline_cpu_mean"]),
    }
    try:
        r = requests.post(f"{RECOVERY_POLICY_URL}/signal", json=payload,
                          timeout=(SIGNAL_CONNECT_TIMEOUT_SEC, SIGNAL_READ_TIMEOUT_SEC))
    except requests.exceptions.RequestException as e:
        return {
            "attempted": True, "accepted_by_server": False, "promotion_verified": False,
            "transport_outcome": "request_exception", "error": f"{type(e).__name__}: {e}",
            "http_status": None, "response_body": None, "payload": payload,
        }
    try:
        body = r.json()
    except ValueError:
        body = None
    accepted = 200 <= r.status_code < 300
    promotion_verified = bool(accepted and isinstance(body, dict) and body.get("outcome") == "executed_verified")
    return {
        "attempted": True, "accepted_by_server": accepted, "promotion_verified": promotion_verified,
        "transport_outcome": "sent" if accepted else "http_error",
        "http_status": r.status_code,
        "response_body": body if body is not None else r.text[:500],
        "payload": payload,
    }


def run_shadow_response_trial(
    run_id: str,
    injector,
    prober,
    gate_state: dict,
    baseline_timeout_sec: float = BASELINE_TIMEOUT_SEC,
    post_injection_observation_sec: float = POST_INJECTION_OBSERVATION_SEC,
    injection_started_timeout_sec: float = INJECTION_STARTED_TIMEOUT_SEC,
    baseline_poll_interval_sec: float = 1.0,
    safety_poll_interval_sec: float = SAFETY_POLL_INTERVAL_SEC,
    probe_ready_timeout_sec: float = PROBE_READY_TIMEOUT_SEC,
    check_safety_fn=_check_safety_or_raise,
    send_signal_fn=_send_shadow_signal,
    analyze_shadow_fn=analyze_shadow_trace,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
) -> dict:
    """preview 준비 -> 요청 부하 -> 기존 8-tick 게이트(§176/§177과 동일
    wrapped get_baseline_status 재사용) -> 실제 CPU 주입(run_once.py와
    동일한 is_started()/get_actual_injection_time() 재사용) -> 매 tick마다
    shadow 판정을 즉시 재확인해 발화 즉시(최초 1회만) 기존 미변경 /signal
    API로 신호 전송. 정상 종료·중단 양쪽에서 injector.cleanup()/
    prober.stop()이 반드시 불리도록 finally로 감싼다."""
    cleanup_state = {"done": False}
    outcome = {
        "status": None, "reason": None, "elapsed_sec": None,
        "prober_stop_failed": None, "injector_cleanup_failed": None,
        "injection_valid": None, "t_injection_requested_utc": None, "t_injection_observed_utc": None,
        "shadow_result": None, "signal_sent": False, "signal_response": None,
    }

    def do_cleanup():
        if cleanup_state["done"]:
            return
        cleanup_state["done"] = True
        try:
            prober.stop()
        except Exception as e:
            outcome["prober_stop_failed"] = f"{type(e).__name__}: {e}"
            print(f"[정리] prober.stop() 실패(계속 진행): {e}")
        try:
            injector.cleanup()
        except Exception as e:
            outcome["injector_cleanup_failed"] = f"{type(e).__name__}: {e}"
            print(f"[정리] injector.cleanup() 실패: {e}")

    baseline_state = {"active_restart_count": None}
    t_entry = monotonic_fn()
    try:
        injector.prepare()
        prober.start()
        if not _wait_for(prober.is_alive, probe_ready_timeout_sec, 1.0):
            raise TrialInvalid("probe가 시작 후 살아있음을 확인 못함")

        # 1) 주입 전 기준선 게이트(§176/§177/§180과 동일한 wrapped get_baseline_status 재사용)
        baseline_deadline = monotonic_fn() + baseline_timeout_sec
        last_safety_check = 0.0
        while True:
            status = prober.get_baseline_status()
            now = monotonic_fn()
            if now - last_safety_check >= safety_poll_interval_sec:
                check_safety_fn(baseline_state, prober)
                last_safety_check = now
            if status.get("ready"):
                break
            if now >= baseline_deadline:
                raise TrialInvalid(
                    f"주입 전 기준선 대기 상한({baseline_timeout_sec:.0f}s) 초과 - "
                    f"SLO baseline 또는 CPU tick 게이트({len(gate_state['valid_ticks'])}/"
                    f"{MIN_STABLE_BASELINE_TICKS}) 미충족"
                )
            sleep_fn(baseline_poll_interval_sec)

        stable_baseline_start_utc = gate_state["valid_ticks"][0]["query_received_at_utc"]

        # 2) 실제 CPU 압박 주입(§176과 동일 DEMO_STAGES, run_once.py와 동일한 관측 방식 재사용)
        t_injection_request = datetime.now(timezone.utc).isoformat()
        outcome["t_injection_requested_utc"] = t_injection_request
        injector.inject()
        started = _wait_for(injector.is_started, injection_started_timeout_sec, 1.0)
        injection_valid = started and injector.is_effective()
        outcome["injection_valid"] = injection_valid
        if not injection_valid:
            raise TrialInvalid(
                f"주입 시작 확인 실패({injection_started_timeout_sec:.0f}s 이내 "
                f"is_started()/is_effective() 미충족)"
            )
        t_injection_observed = None
        if injector.get_actual_injection_time is not None:
            t_injection_observed = injector.get_actual_injection_time()
        t_injection_observed = t_injection_observed or t_injection_request
        outcome["t_injection_observed_utc"] = t_injection_observed

        # 3) 주입 후 관찰 - 매 tick마다 shadow 판정을 즉시 재확인, 발화 즉시(최초 1회만) 신호 전송
        def check_shadow_and_send_if_fired():
            if outcome["signal_sent"]:
                return
            shadow_result = analyze_shadow_fn(
                gate_state["valid_ticks"],
                stable_baseline_start_utc=stable_baseline_start_utc,
                injection_boundary_utc=t_injection_observed,
            )
            outcome["shadow_result"] = shadow_result
            if shadow_result and shadow_result.get("fired"):
                outcome["signal_response"] = send_signal_fn(shadow_result, run_id)
                outcome["signal_sent"] = True

        observation_deadline = monotonic_fn() + post_injection_observation_sec
        last_safety_check = 0.0
        while monotonic_fn() < observation_deadline:
            prober.get_baseline_status()  # 게이트가 이미 감쌈 - tick_interval_sec마다만 실제 조회
            now = monotonic_fn()
            if now - last_safety_check >= safety_poll_interval_sec:
                check_safety_fn(baseline_state, prober)
                # cpu_pressure_demo_adapter._run_stages()가 매 stage마다 자체적으로
                # active pod OOMKilled·재시작·Node 상태를 감시하다 위반을 찾으면
                # TrialInvalid/HarnessCorrupted를 스레드 안에 저장해두는데(§182 이전엔
                # 아무도 이걸 다시 안 물어봐 조용히 묻힐 수 있었다), is_done()이 그
                # 저장된 예외를 여기서 다시 던진다(호출 계약, run_once.py Injector
                # 프로토콜 문서 그대로). 정상 완료(all_stages_done)나 승격 후 대상
                # 교체(target_replacement)는 예외 없이 True만 반환하므로 그냥 무시한다
                # - 반환값 자체는 안 본다, 예외 여부만 본다.
                injector.is_done()
                last_safety_check = now
            check_shadow_and_send_if_fired()
            sleep_fn(1.0)
        # 루프가 0번 돌았거나(관찰 시간이 매우 짧음) 마지막 반복 이후에 tick이 하나 더
        # 잡혔을 수 있으므로, 루프 종료 직후 한 번 더 확인한다 - 그래야 "딱 경계에서
        # 발화"가 신호 전송 없이 누락되지 않는다(처음 구현엔 이 마지막 확인이 shadow_
        # result만 갱신하고 신호는 안 보내는 버그가 있었음 - 로컬 테스트로 발견).
        check_shadow_and_send_if_fired()
        outcome["status"] = "completed"
        outcome["reason"] = (
            "shadow 발화 후 신호 전송 완료" if outcome["signal_sent"]
            else f"관찰 시간({post_injection_observation_sec:.0f}s) 동안 shadow 미발화"
        )
    except (TrialInvalid, HarnessCorrupted) as e:
        outcome["status"] = "aborted"
        outcome["reason"] = f"{type(e).__name__}: {e}"
    finally:
        do_cleanup()
        outcome["elapsed_sec"] = monotonic_fn() - t_entry
    return outcome


def main():
    parser = argparse.ArgumentParser(
        description="§182 - shadow CPU 규칙이 직접 /signal을 보내 실제 전환까지 이어지는지 확인(공식 §176과 분리)")
    parser.add_argument("--arm", default="proposed", choices=["proposed", "fixed_threshold"],
                         help="native는 preview 자체가 없어 승격 대상이 없음 - 제외")
    parser.add_argument("--rep", type=int, default=1)
    parser.add_argument("--probe-config", default=str(DEFAULT_PROBE_CONFIG))
    parser.add_argument("--post-injection-observation-sec", type=float, default=POST_INJECTION_OBSERVATION_SEC)
    parser.add_argument("--dry-run-contract-only", action="store_true",
                         help="클러스터를 건드리지 않고 계약 해시만 출력하고 종료(오프라인 점검용)")
    args = parser.parse_args()

    scenario = "cpu_pressure_demo_shadow_response"
    run_id = f"{scenario}-{args.arm}-{args.rep:02d}-{PLAN_ID}"
    summary_path = DEMO_RESULTS_DIR / f"shadow-response-summary-{run_id}.json"

    DEMO_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    existing = [p for p in (DEMO_RESULTS_DIR / f"contract-{run_id}.json", summary_path) if p.exists()]
    if existing:
        raise SystemExit(f"run_id({run_id})의 결과가 이미 존재함 - 덮어쓰지 않고 중단: {[str(p) for p in existing]}")

    contract = {
        "run_id": run_id, "plan_id": PLAN_ID, "recorded_at": datetime.now(timezone.utc).isoformat(),
        "file_sha256": _contract_hashes(),
        "contract_schema_version": PLAN_ID,
        "purpose": (
            "기존 shadow CPU 규칙(30%·2연속, demo_early_warning_rule.py 미변경)이 발화하면 기존·미변경 "
            "POST /signal API로 recovery-policy에 직접 신호를 보내, preview가 준비돼 있으면 실제 "
            "promote_preview까지 이어지는지 확인하는, §176과 완전히 분리된 시연 버전"
        ),
        "detector_attribution": {
            "signal_detector_tag": SHADOW_DETECTOR_TAG,
            "guarantee": (
                "이 값은 코드 어디서도 'isolation_forest'로 덮어써지지 않는다(SHADOW_DETECTOR_TAG 상수 "
                "고정) - 결과가 승격으로 이어져도 'CPU 규칙 기반 대응'으로만 기록되고 공식 "
                "IsolationForest의 선제 탐지 성과로 표시되지 않는다."
            ),
        },
        "official_detector_never_started_or_imported": True,
        "official_run_once_never_called": True,
        "recovery_policy_api_unchanged_files": [
            "recovery-policy/main.py", "recovery-policy/policy.py", "recovery-policy/safety.py",
            "recovery-policy/schemas.py", "recovery-policy/rollouts_client.py",
        ],
        "minimal_path_to_promotion": (
            "recovery-policy/policy.py의 policy.decide()는 signal_type('anomaly_risk')과 "
            "ctx.preview_ready(rollouts_client.is_paused_pre_promotion())만 보고 조치를 정한다 - "
            "detector 문자열·score 값은 판정에 안 쓰인다(순수 pass-through). 이 파일이 기대는 유일한 "
            "기존 확장점이며, 서버 코드는 한 줄도 안 바꾼다."
        ),
        "demo_early_warning_rule_params_unchanged": {
            "relative_rise_factor": RELATIVE_RISE_FACTOR, "consecutive_required": CONSECUTIVE_REQUIRED,
        },
        "stages": DEMO_STAGES,
        "baseline_timeout_sec": BASELINE_TIMEOUT_SEC,
        "post_injection_observation_sec": args.post_injection_observation_sec,
        "probe_lifetime_sec": PROBE_LIFETIME_SEC,
        "safety_check_scope_note": (
            "§180/§181(무주입)과 달리 이 버전은 실제 주입이 있어 active/preview OOMKilled·재시작·SLO "
            "위반을 즉시 중단 사유로 보지 않는다(공식 run_once.py의 OBSERVING 단계도 이 셋으로 중단하지 "
            "않음, 코드 확인됨) - probe 생존·active pod 식별 가능 여부·Node 상태만 하네스 이상으로 본다."
        ),
        "duplicate_signal_protection": {
            "local_guard": "이 러너는 발화 시 최초 1회만 신호를 보낸다(로컬 already-sent 플래그)",
            "server_side_existing_unchanged": (
                "recovery-policy/safety.py의 idempotency_key(experiment_run_id:signal_type) 중복 차단이 "
                "그대로 적용됨 - 실수로 두 번 보내도 서버가 두 번째를 SKIPPED_DUPLICATE로 무시함"
            ),
        },
        "experiment_run_id_isolation": (
            "POST /admin/experiment-run(공식 실험 컨텍스트 등록)을 이 러너는 절대 호출하지 않는다 - "
            "recovery-policy._current_experiment 상태를 구조적으로 전혀 건드릴 수 없다(main.py 확인: "
            "_record_detection 등은 signal의 experiment_run_id가 _current_experiment.run_id와 일치할 "
            "때만 기록하는데, 이 파일의 run_id는 scenario 접두어부터 공식 run_id와 겹치지 않는다). 결과는 "
            "POST /signal의 HTTP 응답(DecisionRecord)에서 직접 읽는다."
        ),
        "known_simplifications": [
            "t_injection_observed는 run_once.py와 동일하게 injector.is_started()/"
            "get_actual_injection_time()을 재사용해 얻는다(새 근사치 아님) - "
            "injection_observation_error_sec 같은 오차 상한 필드는 이번 버전엔 없음."
        ],
    }
    contract_path = DEMO_RESULTS_DIR / f"contract-{run_id}.json"
    contract_path.write_text(json.dumps(contract, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"계약/코드 해시 기록: {contract_path}")
    if args.dry_run_contract_only:
        print("dry-run - 클러스터 미접속, 여기서 종료")
        return

    injector = make_cpu_pressure_injector(run_id, args.arm, args.rep, stages=DEMO_STAGES)
    injector = arm_controller.wrap_injector_with_preview_prep(injector, args.arm)
    # §196 - preview 생성 "전" 워커 노드 headroom 게이트(node_resource_safety_gate.py
    # docstring 참고 - 임시값, 검증된 안전 기준 아님). arm_controller.py는 미수정.
    injector = node_resource_safety_gate.wrap_injector_with_node_resource_gate(injector, args.arm)
    prober = make_load_ramp_prober(args.probe_config, run_id, scenario, args.arm, args.rep, PROBE_LIFETIME_SEC)
    prober, gate_state = wrap_prober_with_pre_injection_tick_gate(prober)

    print(f"run_id: {run_id} / results_dir: {DEMO_RESULTS_DIR} / "
          f"주입 전 기준선 상한: {BASELINE_TIMEOUT_SEC:.0f}s / "
          f"주입 후 관찰: {args.post_injection_observation_sec:.0f}s")

    outcome = run_shadow_response_trial(
        run_id, injector, prober, gate_state,
        post_injection_observation_sec=args.post_injection_observation_sec,
    )

    summary = {
        "run_id": run_id, "outcome_status": outcome["status"], "outcome_reason": outcome["reason"],
        "elapsed_sec": outcome["elapsed_sec"],
        "prober_stop_failed": outcome["prober_stop_failed"],
        "injector_cleanup_failed": outcome["injector_cleanup_failed"],
        "injection_valid": outcome["injection_valid"],
        "t_injection_requested_utc": outcome["t_injection_requested_utc"],
        "t_injection_observed_utc": outcome["t_injection_observed_utc"],
        "shadow_result": outcome["shadow_result"],
        "signal_sent": outcome["signal_sent"],
        "signal_response": outcome["signal_response"],
        "detector_tag_used": SHADOW_DETECTOR_TAG,
        "slo_baseline_ready_at_utc": gate_state.get("slo_baseline_ready_at_utc"),
        "cpu_gate_ready_at_utc": gate_state.get("cpu_gate_ready_at_utc"),
        "valid_ticks_collected": len(gate_state["valid_ticks"]),
        "valid_ticks": gate_state["valid_ticks"],
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"outcome: {outcome['status']} ({outcome['reason']})")
    if outcome["signal_sent"]:
        sr = outcome["signal_response"] or {}
        print(f"신호(detector={SHADOW_DETECTOR_TAG}) attempted={sr.get('attempted')} "
              f"accepted_by_server={sr.get('accepted_by_server')} "
              f"promotion_verified={sr.get('promotion_verified')}")
        print(f"신호 응답 전체: {sr}")
    print(f"결과 파일: {summary_path}")


if __name__ == "__main__":
    main()
