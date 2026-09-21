#!/usr/bin/env python3
"""§96 - `memory_pressure_negative_control_v1`(§94/§95 동결, 1000MB×120초
×worker 1개, direct 단일 stage) 3-arm 파일럿 단일 arm 실행기. `run_memory_
pressure_trial.py`(smoke 전용, `--size-mb`가 1GB 이상이면 fail-closed로
거부 - "해제하지 않는다"는 기존 방침 그대로 유지)와 완전히 별도 파일이다 -
이 스크립트는 §94/§95에서 이미 3회 독립 재현성이 확인·동결된 바로 그
profile 하나만, 파라미터 조정 없이 실행한다(사전 등록값 CLI로 변경 불가 -
§56.1/§94.2와 동일 원칙).

`run_once()`/`arm_controller`/`make_memory_pressure_injector` 배선은
`run_memory_pressure_trial.py`와 동일하게 그대로 재사용한다 - native는
detector·preview 없음, fixed_threshold/proposed는 detector 기동+preview
준비+자동 rollback이 전부 그대로 붙는다. 새 TrialResult 필드는 추가하지
않는다 - promotion vs 비정상 교체 구분은 이 스크립트의 `classify_target_
replacement()`(순수 함수, 완료된 TrialResult만 읽음)가 기존 필드(action/
promotion_verified/target_replaced/t_target_replaced/t_switch)로부터
사후에 파생한다."""
import argparse
from datetime import datetime, timezone
from pathlib import Path

import arm_controller
import slo_judge
from load_ramp_adapter import make_load_ramp_prober
from memory_pressure_adapter import make_memory_pressure_injector
from run_once import HarnessCorrupted, run_once

DEFAULT_PROBE_CONFIG = Path(__file__).parent.parent / "chaos" / "probe-config.yaml"
TIMEOUT_SEC = 1140  # run_memory_pressure_trial.py와 동일(계약서 §4 예산, 단일 120초 stage에는 넉넉)

# 사전 등록(§94.2/§95.4 동결값) - CLI로 바꿀 수 없다. 다른 강도/시간을
# 시험하려면 §50/§52의 calibration 경로를 다시 거쳐야 한다.
SIZE_MB = 1000.0
WORKERS = 1
STAGE_DURATION_SEC = 120.0


def classify_target_replacement(result) -> dict:
    """§96 section 7 - TrialResult에 이미 있는 필드만으로 target replacement의
    원인을 사후에 분류한다(새 필드 없음, 순수 함수). native는 detector·
    promotion 경로 자체가 없으므로 교체가 있었다면 무조건 비정상이다.
    non-native는 promote_preview가 실행+검증됐고 그 시각(t_switch)이 교체
    시각(t_target_replaced)과 근접(300초 이내 - 이 파일럿의 stage 지속시간
    120초+drain보다 넉넉한 여유)해야만 "promotion 원인"으로 인정한다 -
    단순히 action/promotion_verified만 보고 넘겨짚지 않는다(지시:
    "Kubernetes event와 timing으로 입증")."""
    if not result.target_replaced:
        return {"category": "no_replacement", "reasons": []}

    if result.arm == "native":
        return {"category": "abnormal_replacement",
                "reasons": ["native arm은 detector·promotion 경로 자체가 없음 - 교체는 비정상(restart/eviction 등)으로만 설명 가능"]}

    if result.action != "promote_preview" or result.promotion_verified is not True:
        return {"category": "abnormal_replacement",
                "reasons": [f"action={result.action}, promotion_verified={result.promotion_verified} - "
                            f"promotion이 실행·검증되지 않았는데 target이 바뀜(비정상)"]}

    if not result.t_switch or not result.t_target_replaced:
        return {"category": "abnormal_replacement",
                "reasons": ["promotion은 검증됐지만 t_switch 또는 t_target_replaced 시각이 없어 "
                            "timing으로 인과를 입증할 수 없음(fail-closed - 근거 불충분은 비정상으로 취급)"]}

    t_switch = datetime.fromisoformat(result.t_switch)
    t_replaced = datetime.fromisoformat(result.t_target_replaced)
    delta_sec = abs((t_replaced - t_switch).total_seconds())
    if delta_sec > 300.0:
        return {"category": "abnormal_replacement",
                "reasons": [f"promotion 검증 시각과 target 교체 시각의 차이가 {delta_sec:.1f}초로 너무 큼 "
                            f"(300초 초과) - 같은 사건이라고 보기 어려움"]}

    return {"category": "promotion_caused", "reasons": [],
            "t_switch": result.t_switch, "t_target_replaced": result.t_target_replaced,
            "delta_sec": delta_sec}


def main():
    parser = argparse.ArgumentParser(
        description="memory_pressure_negative_control_v1(1000MB x 120초) 3-arm 파일럿 단일 arm 실행(§96)")
    parser.add_argument("--arm", required=True, choices=["native", "fixed_threshold", "proposed"])
    parser.add_argument("--rep", type=int, default=1)
    parser.add_argument("--probe-config", default=str(DEFAULT_PROBE_CONFIG))
    parser.add_argument("--timeout-sec", type=float, default=TIMEOUT_SEC)
    parser.add_argument("--sequence-index", type=int, default=1)
    parser.add_argument("--order-seed", type=int, default=1)
    parser.add_argument("--rollout", default="vllm-serving")
    parser.add_argument("--namespace", default="vllm-serving")
    args = parser.parse_args()

    scenario = "memory_pressure_negative_control_v1"
    run_id = f"pilot-memory-negative-{args.arm.replace('fixed_threshold', 'fixed')}-{args.rep:02d}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    stages = [{"name": f"stage-1-{SIZE_MB:.0f}mb", "size_mb": SIZE_MB, "workers": WORKERS,
               "duration_sec": STAGE_DURATION_SEC}]

    injector = make_memory_pressure_injector(run_id, args.arm, args.rep, stages=stages)
    injector = arm_controller.wrap_injector_with_preview_prep(injector, args.arm, args.rollout, args.namespace)
    detector = arm_controller.make_detector_for_arm(args.arm, run_id)
    prober = make_load_ramp_prober(args.probe_config, run_id, scenario, args.arm, args.rep, args.timeout_sec)

    print(f"run_id: {run_id}" + (f" / detector: {detector.name}" if detector is not None else ""))
    print(f"stage: size_mb={SIZE_MB} workers={WORKERS} duration_sec={STAGE_DURATION_SEC} (§94/§95 동결, 변경 불가)")
    try:
        result = run_once(
            scenario=scenario, arm=args.arm, rep=args.rep,
            sequence_index=args.sequence_index, order_seed=args.order_seed,
            injector=injector, prober=prober, timeout_sec=args.timeout_sec,
            detector=detector,
            run_id=run_id, is_pilot=True,
            latency_slo_sec=slo_judge.LATENCY_THRESHOLD, slo_version=slo_judge.SLO_VERSION,
            min_observation_sec=slo_judge.WINDOW_SEC,
            injection_started_timeout_sec=60.0,
        )
    except HarnessCorrupted as e:
        print(f"HARNESS CORRUPTED: {e}")
        raise

    print(f"outcome: {result.outcome} / state: {result.state}")
    print(f"t_injection={result.t_injection} t_slo={result.t_slo} t_recovery={result.t_recovery}")
    print(f"detected={result.detected} detection_source={result.detection_source} "
          f"action={result.action} decision_outcome={result.decision_outcome} "
          f"promotion_verified={result.promotion_verified}")
    if result.target_replaced:
        classification = classify_target_replacement(result)
        print(f"target_replaced=True at {result.t_target_replaced} "
              f"(replacement={result.target_replacement_pod_name}/{result.target_replacement_pod_uid}) "
              f"-> classification={classification['category']} ({classification['reasons']})")
    result_dir = "results/pilot"
    print(f"결과 파일: {result_dir}/trial-{run_id}.json")
    print(f"안전 로그(evidence): {result_dir}/memory-pressure-safety-{run_id}.jsonl")


if __name__ == "__main__":
    main()
