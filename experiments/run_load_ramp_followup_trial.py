#!/usr/bin/env python3
"""load_ramp 후속 고정관측구간 비교 실행기 (§137 이후 지시 §4/§5/§7 - 별도
runner로 격리, 기존 run_load_ramp_trial.py는 전혀 수정하지 않았다).

기존 공식 실행기와의 차이(전부 명시적):
  - probe.py 대신 probe_followup.py(load_ramp_followup_adapter 경유) - 요청
    단위 sent/complete evidence까지 남김.
  - run_once(fixed_duration_observation=True) - recovered/prevented가
    확정돼도 조기종료하지 않고 timeout_sec(기본 900s, 공식과 동일값 - 임의로
    줄이거나 늘리지 않음) 전체를 채운다.
  - results_dir=results/followup(별도 - 기존 45건과 절대 합산 안 됨),
    run_id에 plan_id=post_hoc_followup-v1 접미사.
  - detector 로컬 PID·클러스터 pod 자원을 독립 주기로 함께 샘플링해
    results/followup/cost-{run_id}.json에 저장.
  - native arm은 지원하지 않는다(--arm choices에서 제외 - 이번 후속 실행에
    native를 임의 추가하지 말라는 지시).

안전 중단 우선순위는 그대로 유지한다 - run_once()가 TrialInvalid/
HarnessCorrupted를 던지면 이 스크립트도 그대로 전파한다(자동 재시도 없음,
원본 보존을 위해 여기서 무엇도 조용히 삼키지 않는다).
"""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import arm_controller
import slo_judge
from load_ramp_adapter import make_load_ramp_injector
from load_ramp_followup_adapter import make_load_ramp_prober_followup
from followup_cost_sampler import LocalProcessSampler, ClusterPodSampler, dump as dump_cost
from run_once import HarnessCorrupted, TrialInvalid, run_once

DEFAULT_CONFIG = Path(__file__).parent.parent / "chaos" / "scenario-load-ramp.yaml"
DEFAULT_PROBE_CONFIG = Path(__file__).parent.parent / "chaos" / "probe-config.yaml"
TIMEOUT_SEC = 900  # 공식 run_load_ramp_trial.py와 동일값(§5 비교 계약 - 동결된 전체 부하
                    # 스케줄(~450s) + 사후 관측(~450s), 기존 실측 근거 그대로 재사용, 임의 변경 아님)
PLAN_ID = "post_hoc_followup-v1"
FOLLOWUP_RESULTS_DIR = Path(__file__).parent / "results" / "followup"

_CONTRACT_FILES = [
    Path(__file__),
    Path(__file__).parent / "load_ramp_followup_adapter.py",
    Path(__file__).parent / "loadgen-runner" / "probe_followup.py",
    Path(__file__).parent / "followup_cost_sampler.py",
    Path(__file__).parent / "run_once.py",
    Path(__file__).parent / "arm_controller.py",
    Path(__file__).parent / "load_ramp_adapter.py",
    Path(__file__).parent / "slo_judge.py",
]


def _contract_hashes() -> dict:
    """§7 - '코드·계약·artifact hash를 기록'을 위한 최소 구현. 이 실행에
    실제로 관여하는 코드 파일들의 SHA-256을 남긴다 - 사후에 "그때 정말
    이 코드로 돌렸는가"를 검증할 수 있게 한다."""
    out = {}
    for p in _CONTRACT_FILES:
        try:
            out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError as e:
            out[p.name] = f"미확인({e})"
    return out


def main():
    parser = argparse.ArgumentParser(description="load_ramp 후속 고정관측구간 비교 - fixed_threshold/proposed 전용")
    parser.add_argument("--arm", required=True, choices=["fixed_threshold", "proposed"])
    parser.add_argument("--rep", type=int, default=1)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--probe-config", default=str(DEFAULT_PROBE_CONFIG))
    parser.add_argument("--timeout-sec", type=float, default=TIMEOUT_SEC)
    parser.add_argument("--sequence-index", type=int, default=1)
    parser.add_argument("--order-seed", type=int, default=1)
    parser.add_argument("--cost-sample-interval-sec", type=float, default=5.0)
    parser.add_argument("--dry-run-contract-only", action="store_true",
                         help="클러스터를 건드리지 않고 계약 해시·경로만 출력하고 종료(오프라인 점검용)")
    args = parser.parse_args()

    scenario = "load_ramp"
    run_id = f"{scenario}-{args.arm}-{args.rep:02d}-{PLAN_ID}"
    contract = {
        "run_id": run_id, "plan_id": PLAN_ID, "recorded_at": datetime.now(timezone.utc).isoformat(),
        "timeout_sec": args.timeout_sec, "fixed_duration_observation": True,
        "file_sha256": _contract_hashes(),
    }
    FOLLOWUP_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    contract_path = FOLLOWUP_RESULTS_DIR / f"contract-{run_id}.json"
    contract_path.write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"계약/코드 해시 기록: {contract_path}")
    if args.dry_run_contract_only:
        print("dry-run - 클러스터 미접속, 여기서 종료")
        return

    injector = make_load_ramp_injector(args.config, run_id, args.arm, args.rep)
    injector = arm_controller.wrap_injector_with_preview_prep(injector, args.arm)
    detector = arm_controller.make_detector_for_arm(args.arm, run_id)
    prober = make_load_ramp_prober_followup(args.probe_config, run_id, scenario, args.arm, args.rep,
                                             args.timeout_sec, evidence_out_dir=FOLLOWUP_RESULTS_DIR)

    local_sampler = LocalProcessSampler(detector.get_pid if detector is not None else (lambda: None),
                                         interval_sec=args.cost_sample_interval_sec)
    cluster_sampler = ClusterPodSampler("vllm-serving", interval_sec=args.cost_sample_interval_sec)

    print(f"run_id: {run_id} / detector: {detector.name if detector else None} / "
          f"results_dir: {FOLLOWUP_RESULTS_DIR} / timeout_sec: {args.timeout_sec} (fixed_duration_observation=True)")
    local_sampler.start()
    cluster_sampler.start()
    t_run_start = datetime.now(timezone.utc).isoformat()
    result = None
    try:
        result = run_once(
            scenario=scenario, arm=args.arm, rep=args.rep,
            sequence_index=args.sequence_index, order_seed=args.order_seed,
            injector=injector, prober=prober, timeout_sec=args.timeout_sec,
            detector=detector, run_id=run_id, is_pilot=False,
            latency_slo_sec=slo_judge.LATENCY_THRESHOLD, slo_version=slo_judge.SLO_VERSION,
            min_observation_sec=slo_judge.WINDOW_SEC,
            results_dir=FOLLOWUP_RESULTS_DIR,
            fixed_duration_observation=True,
        )
    except (HarnessCorrupted, TrialInvalid) as e:
        # 안전 중단 우선 - 원본(지금까지 쓰인 부분 결과 파일) 그대로 두고
        # 여기서 자동 재시도·대체 실행을 하지 않는다(지시 그대로).
        print(f"중단됨({type(e).__name__}): {e}")
        raise
    finally:
        local_sampler.stop()
        cluster_sampler.stop()
        cost_path = FOLLOWUP_RESULTS_DIR / f"cost-{run_id}.json"
        dump_cost(cost_path, local_sampler, cluster_sampler, phase_marks={
            "run_started_at": t_run_start,
            "t_injection": getattr(result, "t_injection", None),
            "t_slo": getattr(result, "t_slo", None),
            "t_recovery": getattr(result, "t_recovery", None),
            "t_run_end": getattr(result, "t_run_end", None),
        })
        print(f"비용 계측 저장: {cost_path}")

    print(f"outcome: {result.outcome} / state: {result.state}")
    print(f"t_injection={result.t_injection} t_slo={result.t_slo} t_recovery={result.t_recovery}")
    print(f"결과 파일: {FOLLOWUP_RESULTS_DIR}/trial-{run_id}.json")


if __name__ == "__main__":
    main()
