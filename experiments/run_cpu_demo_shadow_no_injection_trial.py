#!/usr/bin/env python3
"""§180(2026-09-27) - shadow CPU 규칙(30%·2연속, demo_early_warning_rule.py,
미변경)을 정책에 연결하기 전에, "불필요한 경보(오탐) 가능성"만 오프라인
확인하기 위한 **무주입 정상 관찰** 전용 러너. CPU 압박 주입·공식
IsolationForest 탐지기·정책 신호·Pod 승격을 전혀 호출하지 않는다.

**run_once.py는 전혀 안 쓴다** - 그 함수의 OBSERVING 단계는
injector.inject()를 무조건 호출하므로(run_once.py:751, 직접 확인 -
이를 건너뛸 파라미터 없음), 대신 §176~§179가 이미 쓰는 조각들만 다른
순서로 조합한다: arm_controller.wrap_injector_with_preview_prep()로
preview만 준비하고(inject()는 절대 호출 안 함), demo_cpu_per_pod_tracer.
wrap_prober_with_pre_injection_tick_gate()(§177/§178/§179, 미변경)를
그대로 재사용해 "요청 부하 시작 후 CPU rate() 30초 웜업 + 동일 active
Pod의 유효 tick 8개 확보 + 첫 tick 고정 기준선"을 얻는다 - 이 게이트가
원래 run_once._wait_for_baseline()이 ready=True에서 멈추는 지점을,
여기서는 계속 폴링해(같은 함수를 더 오래 부를 뿐, 새 게이트 로직 없음)
관찰을 연장한다. arm_controller.make_detector_for_arm()은 이 파일
어디서도 import·호출하지 않는다 - 공식 탐지기·정책 신호·승격 경로
자체가 구조적으로 존재하지 않는다.

**제한된 관찰 시간(MAX_OBSERVATION_TICKS=40) 근거**: §176~§179(rep=1~6,
native rep=1)의 주입 전 관찰 구간은 전부 정확히 8 tick(실측 약
105~290초)뿐이었다 - 정상 상태에서의 shadow 오탐 여부를 판단하기엔
너무 짧다. 이번 관찰은 그 최장 구간의 2배 이상인 40 tick(기준선 8 +
추가 32, 약 10.5분: 웜업 30초 + 40 x EVAL_INTERVAL_SEC(15초, §176 값
그대로 인용))까지만 본다. run_cpu_demo_shadow_gated_trial.py의
SHADOW_MAX_TICKS(200, 약 50분)를 다 채우지 않는 것은, 이번이 이
관찰 방식 자체의 첫 시도라 과도하게 길게 하지 않으려는 의도적
축소다(필요하면 이후 별도 승인으로 늘릴 수 있음 - 이번에 그 결정을
내리지 않는다).

**안전 감시**: 주입이 없어 cpu_pressure_demo_adapter.py의 스테이지
내부 안전 감시(OOMKilled·재시작·Node)는 애초에 실행되지 않으므로,
이 스크립트가 별도로 매 SAFETY_POLL_INTERVAL_SEC마다 active/preview
pod 존재·재시작·OOMKilled, Node 상태, probe SLO 위반 여부를 직접
확인한다(전부 기존 memory_pressure_adapter.py/active_pod_resolver.py
헬퍼 재사용, 새 판정 로직 없음) - 이상 시 즉시 TrialInvalid/
HarnessCorrupted를 던지고 finally에서 정리한다.

공식 IsolationForest 모델·임계값·정책·SLO·CPU stage 구성(DEMO_STAGES,
그대로 인용만 함 - inject()를 안 부르니 실제로는 안 쓰임)·기존 결과는
이 파일에서 전혀 안 건드린다."""
import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import arm_controller
import node_resource_safety_gate
from active_pod_resolver import get_active_pods
from cpu_pressure_demo_adapter import make_cpu_pressure_injector
from demo_cpu_per_pod_tracer import (
    EVAL_INTERVAL_SEC, MIN_STABLE_BASELINE_TICKS, RATE_WINDOW_WARMUP_SEC,
    analyze_shadow_trace, get_preview_pods, wrap_prober_with_pre_injection_tick_gate,
)
from demo_early_warning_rule import CONSECUTIVE_REQUIRED, RELATIVE_RISE_FACTOR
from load_ramp_adapter import make_load_ramp_prober
from memory_pressure_adapter import _node_healthy, get_node_conditions, get_pod_details
from run_cpu_demo_trial import DEMO_STAGES
from run_once import HarnessCorrupted, TrialInvalid

DEFAULT_PROBE_CONFIG = Path(__file__).parent.parent / "chaos" / "probe-config.yaml"
PLAN_ID = "capstone-cpu-demo-shadow-no-injection-180-v1"
DEMO_RESULTS_DIR = Path(__file__).parent / "results" / "capstone_cpu_demo_shadow_no_injection"

# §180 - 계산 근거는 위 모듈 docstring 참고.
MAX_OBSERVATION_TICKS = 40
# 웜업(30s) + 40 tick x 15s = 630s의 약 1.4배 여유 - empty_result 재조회로
# tick 수집이 예상보다 느려져도 무한정 안 늘어나게 하는 별도 안전 상한
# (tick 수 조건과 별개로 둘 중 먼저 오는 쪽에서 멈춤).
MAX_WALL_CLOCK_SEC = 900.0
# preview 준비(실측 약 260~340초, §172/§175/§176 문서 그대로 인용) + 웜업
# 30초 + tick 수집 상한 900초 + 여유- probe가 관찰 예산보다 먼저 안 죽게
# (§179와 같은 목적, 같은 계산 방식).
PROBE_LIFETIME_SEC = 900.0 + 360.0
SAFETY_POLL_INTERVAL_SEC = 5.0
PROBE_READY_TIMEOUT_SEC = 30.0

_CONTRACT_FILES = [
    Path(__file__),
    Path(__file__).parent / "cpu_pressure_demo_adapter.py",
    Path(__file__).parent / "demo_cpu_per_pod_tracer.py",
    Path(__file__).parent / "demo_early_warning_rule.py",
    Path(__file__).parent / "load_ramp_adapter.py",
    Path(__file__).parent / "run_once.py",
    Path(__file__).parent / "arm_controller.py",
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
    get_preview_pods_fn=get_preview_pods,
    get_pod_details_fn=get_pod_details,
    get_node_conditions_fn=get_node_conditions,
) -> None:
    """무주입 관찰 중 probe 생존·active/preview pod·Node·요청 품질을
    확인한다 - 전부 기존 헬퍼 재사용(새 판정 로직 없음). 이상 시 즉시
    예외를 던져 상위 finally에서 정리하게 한다.

    §181 - probe(요청 부하 생성기)가 죽어도 CPU tick 수집 자체는 계속
    "성공"할 수 있다(demo_cpu_per_pod_tracer의 CPU 조회가 Prometheus를
    직접 보고 probe 프로세스와 무관하기 때문) - 그러면 "요청 부하가
    흐르는 정상 상태"가 아니라 유휴 상태의 CPU 값을 정상 자료로 계속
    쌓이게 된다. 그래서 이 확인을 다른 안전 검사보다 먼저, 매번 한다."""
    if not prober.is_alive():
        raise TrialInvalid(
            "무주입 관찰 중 probe 프로세스 종료 감지(예상 밖) - "
            "요청 부하 없는 상태의 CPU 값을 정상 자료로 계속 모으지 않도록 중단"
        )
    active = get_active_pods_fn()
    if len(active) != 1:
        raise HarnessCorrupted(f"active pod 식별 모호/소실(무주입 관찰 중 예상 밖): {active}")
    details = get_pod_details_fn(active[0]["name"])
    if details is None:
        raise HarnessCorrupted("active pod 상세 조회 실패(무주입 관찰 중 예상 밖)")
    if details.get("oom_killed"):
        raise TrialInvalid("무주입 관찰 중 active pod OOMKilled(예상 밖) - fail-closed")
    if baseline_state["active_restart_count"] is None:
        baseline_state["active_restart_count"] = details.get("restart_count") or 0
    elif (details.get("restart_count") or 0) > baseline_state["active_restart_count"]:
        raise TrialInvalid("무주입 관찰 중 active pod 재시작 감지(예상 밖) - fail-closed")

    conditions = get_node_conditions_fn(details["node_name"])
    if not _node_healthy(conditions):
        raise HarnessCorrupted(f"무주입 관찰 중 Node 상태 이상: {conditions}")

    preview = get_preview_pods_fn()
    if len(preview) == 1:
        pdetails = get_pod_details_fn(preview[0]["name"])
        if pdetails is not None and pdetails.get("oom_killed"):
            raise TrialInvalid("무주입 관찰 중 preview pod OOMKilled(예상 밖) - fail-closed")

    if prober.check_slo_violation():
        raise TrialInvalid("무주입인데 SLO 위반 감지(예상 밖 상태) - fail-closed")


def run_no_injection_observation(
    run_id: str,
    injector,
    prober,
    max_observation_ticks: int = MAX_OBSERVATION_TICKS,
    max_wall_clock_sec: float = MAX_WALL_CLOCK_SEC,
    safety_poll_interval_sec: float = SAFETY_POLL_INTERVAL_SEC,
    probe_ready_timeout_sec: float = PROBE_READY_TIMEOUT_SEC,
    check_safety_fn=_check_safety_or_raise,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
) -> dict:
    """preview만 준비하고(inject() 절대 호출 안 함) 요청 부하를 흘리며,
    기존 §177/§178/§179 게이트를 연장 폴링해 유효 CPU tick을
    max_observation_ticks개까지 모은다. 정상 종료·중단 양쪽에서
    injector.cleanup()/prober.stop()이 반드시 불리도록 finally로 감싼다
    (idempotent cleanup()을 전제로 하는 run_once.py의 기존 관례와 동일).

    §181 정정 3가지(공유 코드·30%/2연속 규칙 미변경, 이 함수 국소 수정):
    1) **관찰시간 시작점 분리**: max_wall_clock_sec는 이전엔 이 함수
       진입 시각(=preview 준비 이전)부터 셌다 - preview 준비(실측 약
       260~340초)가 그 상한을 미리 갉아먹어, tick 수집 자체는 거의
       못 하고도 시간 상한에 걸릴 수 있었다. 이제 "실제 probe 관찰
       시작"(is_alive 확인 직후)부터 별도로 잰다.
    2) **tick 미달 시간 상한 = 불완전 관찰**: 목표 tick 수를 못 채운 채
       시간 상한에 도달하면 이전엔 "completed"로 기록됐다(정상 관찰
       성공처럼 보임). 이제 "incomplete_observation"으로 구분한다.
    3) **정리 실패를 결과에 남김**: prober.stop()/injector.cleanup()이
       예외를 던지면 이전엔 콘솔에 출력만 하고 넘어갔다. 이제
       outcome["prober_stop_failed"]/["injector_cleanup_failed"]에
       그대로 남긴다(성공 시 None)."""
    cleanup_state = {"done": False}
    outcome = {
        "status": None, "reason": None, "elapsed_sec": None,
        "prober_stop_failed": None, "injector_cleanup_failed": None,
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
    t_entry = monotonic_fn()  # 함수 진입 시각(경과시간 보고용) - 관찰시간 상한 기준과는 분리
    t_observation_start = None  # §181 - probe 생존 확인 직후에만 채움(관찰시간 상한의 실제 기준점)
    try:
        injector.prepare()  # preview 준비만 - inject()는 이 함수 전체에서 절대 호출 안 함
        prober.start()
        if not _wait_for(prober.is_alive, probe_ready_timeout_sec, 1.0):
            raise TrialInvalid("probe가 시작 후 살아있음을 확인 못함")
        t_observation_start = monotonic_fn()

        last_safety_check = 0.0
        while True:
            status = prober.get_baseline_status()  # §177/§178/§179 게이트가 이미 감쌈
            now = monotonic_fn()
            if now - last_safety_check >= safety_poll_interval_sec:
                check_safety_fn(baseline_state, prober)  # §181 - probe 생존도 매번 여기서 확인
                last_safety_check = now
            n_valid = status.get("cpu_valid_ticks_collected", 0)
            if n_valid >= max_observation_ticks:
                outcome["status"] = "completed"
                outcome["reason"] = f"목표 tick 수({max_observation_ticks}) 도달"
                break
            if now - t_observation_start >= max_wall_clock_sec:
                outcome["status"] = "incomplete_observation"
                outcome["reason"] = (
                    f"목표 tick 수({max_observation_ticks}) 미달 상태로 최대 관찰 시간"
                    f"({max_wall_clock_sec:.0f}s, probe 생존 확인 시점부터 기산) 도달 - "
                    f"tick {n_valid}/{max_observation_ticks}"
                )
                break
            sleep_fn(1.0)
    except (TrialInvalid, HarnessCorrupted) as e:
        outcome["status"] = "aborted"
        outcome["reason"] = f"{type(e).__name__}: {e}"
    finally:
        do_cleanup()
        outcome["elapsed_sec"] = monotonic_fn() - t_entry
    return outcome


def main():
    parser = argparse.ArgumentParser(
        description="§180 shadow CPU 규칙 오탐 확인용 무주입 정상 관찰(주입·탐지기·정책신호·승격 없음)")
    parser.add_argument("--arm", default="proposed", choices=["proposed", "fixed_threshold"],
                         help="native는 preview 자체가 없어 이 관찰 목적과 안 맞음 - 제외")
    parser.add_argument("--rep", type=int, default=1)
    parser.add_argument("--probe-config", default=str(DEFAULT_PROBE_CONFIG))
    parser.add_argument("--max-observation-ticks", type=int, default=MAX_OBSERVATION_TICKS)
    parser.add_argument("--dry-run-contract-only", action="store_true",
                         help="클러스터를 건드리지 않고 계약 해시만 출력하고 종료(오프라인 점검용)")
    args = parser.parse_args()

    scenario = "cpu_pressure_demo_shadow_no_injection"
    run_id = f"{scenario}-{args.arm}-{args.rep:02d}-{PLAN_ID}"
    summary_path = DEMO_RESULTS_DIR / f"normal-observation-summary-{run_id}.json"

    DEMO_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    existing = [p for p in (
        DEMO_RESULTS_DIR / f"contract-{run_id}.json", summary_path,
    ) if p.exists()]
    if existing:
        raise SystemExit(f"run_id({run_id})의 결과가 이미 존재함 - 덮어쓰지 않고 중단: {[str(p) for p in existing]}")

    contract = {
        "run_id": run_id, "plan_id": PLAN_ID, "recorded_at": datetime.now(timezone.utc).isoformat(),
        "file_sha256": _contract_hashes(),
        "contract_schema_version": PLAN_ID,
        "is_reproduction_of_171": False, "is_reproduction_of_172_175": False, "is_reproduction_of_176_179": False,
        "injection_never_called": True,
        "detector_never_started_or_imported": True,
        "policy_signal_never_sent": True,
        "promotion_structurally_impossible": True,
        "purpose": "shadow CPU 규칙(30%·2연속)을 정책에 연결하기 전, 무주입 정상 상태에서 오탐 가능성만 관찰",
        "demo_early_warning_rule_params_unchanged": {
            "relative_rise_factor": RELATIVE_RISE_FACTOR, "consecutive_required": CONSECUTIVE_REQUIRED,
        },
        "min_pre_injection_valid_ticks_required": MIN_STABLE_BASELINE_TICKS,
        "rate_window_warmup_sec_cited_from": "demo_cpu_per_pod_tracer.RATE_WINDOW_WARMUP_SEC(§177, 새 값 아님)",
        "warmup_sec": RATE_WINDOW_WARMUP_SEC,
        "eval_interval_sec_cited_from": "demo_cpu_per_pod_tracer.EVAL_INTERVAL_SEC(§172, 새 값 아님)",
        "eval_interval_sec": EVAL_INTERVAL_SEC,
        "max_observation_ticks": args.max_observation_ticks,
        "max_observation_ticks_rationale": (
            "§176~§179(rep=1~6, native rep=1)의 주입 전 관찰이 전부 정확히 8 tick(약 105~290초)뿐이었다 - "
            "그 최장 구간의 2배 이상인 40 tick(웜업 30초 + 40*15초=630초)까지만 본다. "
            "run_cpu_demo_shadow_gated_trial.SHADOW_MAX_TICKS(200, 약 50분)를 다 채우지 않는 의도적 축소 - "
            "첫 시도를 과도하게 늘리지 않기 위함."
        ),
        "max_wall_clock_sec": MAX_WALL_CLOCK_SEC,
        "probe_lifetime_sec": PROBE_LIFETIME_SEC,
        "expected_live_duration_estimate_sec": {
            "preview_prep": "약 260~340초(§172/§175/§176 실측 인용)",
            "tick_collection_nominal": 630.0,
            "tick_collection_ceiling": MAX_WALL_CLOCK_SEC,
            "cleanup": "약 60~120초",
        },
    }
    contract_path = DEMO_RESULTS_DIR / f"contract-{run_id}.json"
    contract_path.write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")
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
    prober, gate_state = wrap_prober_with_pre_injection_tick_gate(
        prober, min_valid_ticks=args.max_observation_ticks, tick_interval_sec=EVAL_INTERVAL_SEC,
        warmup_sec=RATE_WINDOW_WARMUP_SEC,
    )
    # min_valid_ticks를 목표 tick 수 자체로 넘겨, status["cpu_valid_ticks_collected"]가
    # 곧 "지금까지 모은 tick 수"로 직접 쓰이게 한다(게이트 자체 로직은 미변경 -
    # 이 파라미터는 §177에서 이미 노출돼 있던 값을 그대로 씀).

    print(f"run_id: {run_id} / results_dir: {DEMO_RESULTS_DIR} / "
          f"목표 tick: {args.max_observation_ticks} / 최대 관찰시간: {MAX_WALL_CLOCK_SEC:.0f}s")

    outcome = run_no_injection_observation(run_id, injector, prober, args.max_observation_ticks)

    shadow_result = analyze_shadow_trace(gate_state["valid_ticks"]) if gate_state["valid_ticks"] else None
    summary = {
        "run_id": run_id, "outcome_status": outcome["status"], "outcome_reason": outcome["reason"],
        "elapsed_sec": outcome["elapsed_sec"],
        "prober_stop_failed": outcome["prober_stop_failed"],
        "injector_cleanup_failed": outcome["injector_cleanup_failed"],
        "valid_ticks_collected": len(gate_state["valid_ticks"]),
        "slo_baseline_ready_at_utc": gate_state.get("slo_baseline_ready_at_utc"),
        "cpu_gate_ready_at_utc": gate_state.get("cpu_gate_ready_at_utc"),
        "shadow_result": shadow_result,
        "valid_ticks": gate_state["valid_ticks"],
        "is_reproduction_of_171": False, "is_reproduction_of_172_175": False, "is_reproduction_of_176_179": False,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"outcome: {outcome['status']} ({outcome['reason']}) / 유효 tick {len(gate_state['valid_ticks'])}개 / "
          f"경과 {outcome['elapsed_sec']:.1f}초")
    if shadow_result:
        print(f"shadow 판정: fired={shadow_result.get('fired')}")
    print(f"결과 파일: {summary_path}")


if __name__ == "__main__":
    main()
