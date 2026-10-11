#!/usr/bin/env python3
"""졸업작품 시연 전용 - 점진적 CPU 압박(활성 Pod 한정) -> 자동 탐지·정책
결정 -> 준비된 정상 Pod로 전환 -> 요청 품질 확인. 메모리 압박 데모
(run_capstone_demo_trial.py)가 이 2-VM 클러스터의 노드 메모리 헤드룸
부족(active+preview 공존만으로 이미 기존 안전 하한 3GiB 미달, 2026-09-26
확인)으로 보류된 뒤의 대안 경로. run_capstone_demo_trial.py/
memory_pressure_adapter.py/arm_controller.py/run_once.py 어느 것도 안
건드리고, 새로 만든 cpu_pressure_demo_adapter.make_cpu_pressure_injector()
(활성 Pod에만 selector.pods로 고정, CPU cgroup 하드캡이라 메모리와 달리
노드 차원 폭주 경로가 구조적으로 없음 - 그 파일 docstring 참고)만 새로
사용한다.

안전 여유(2026-09-26 확인, 새 숫자 발명 아님): active+preview 두 Pod가
동시에 각자 컨테이너 CPU 한도(3코어, 계약서 §6 동결)를 전부 써도
6코어/8코어(75%), 기타 pod(~0.5코어) 더해도 82% - 여유 있음. 메모리와
달리 노드 가용 메모리 사전 투영 게이트가 필요 없다(구조상 못 넘음)."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import arm_controller
import node_resource_safety_gate
import slo_judge
from cpu_pressure_demo_adapter import make_cpu_pressure_injector
from load_ramp_adapter import make_load_ramp_prober
from run_once import HarnessCorrupted, TrialInvalid, run_once

DEFAULT_PROBE_CONFIG = Path(__file__).parent.parent / "chaos" / "probe-config.yaml"
TIMEOUT_SEC = 480  # run_capstone_demo_trial.py(메모리 데모)와 동일 근거 - 4 stage x 90s + 여유
PLAN_ID = "capstone-cpu-demo-166-v1"
DEMO_RESULTS_DIR = Path(__file__).parent / "results" / "capstone_cpu_demo"

# 시연용 4단계 - 컨테이너 CPU 한도(3코어) 안에서 vLLM 자신의 요청처리
# 스레드와 경쟁하는 stress worker 수를 점진적으로 늘린다(workers>코어수여도
# cgroup이 하드캡이라 노드 차원 위험 없음 - cpu_pressure_demo_adapter.py
# 모듈 docstring 참고).
DEMO_STAGES = [
    {"name": "stage-1-1worker", "workers": 1, "load": 100, "duration_sec": 90},
    {"name": "stage-2-2worker", "workers": 2, "load": 100, "duration_sec": 90},
    {"name": "stage-3-3worker", "workers": 3, "load": 100, "duration_sec": 90},
    {"name": "stage-4-4worker", "workers": 4, "load": 100, "duration_sec": 90},
]

_CONTRACT_FILES = [
    Path(__file__),
    Path(__file__).parent / "cpu_pressure_demo_adapter.py",
    Path(__file__).parent / "memory_pressure_adapter.py",
    Path(__file__).parent / "load_ramp_adapter.py",
    Path(__file__).parent / "run_once.py",
    Path(__file__).parent / "arm_controller.py",
    Path(__file__).parent / "slo_judge.py",
]


def _contract_hashes() -> dict:
    out = {}
    for p in _CONTRACT_FILES:
        try:
            out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError as e:
            out[p.name] = f"미확인({e})"
    return out


def _default_evidence_log_path(run_id: str) -> Path:
    """§167 교훈 재사용 - 처음부터 탐지기 evidence-log를 켜 둔다."""
    return DEMO_RESULTS_DIR / f"detector-evidence-{run_id}.jsonl"


def main():
    parser = argparse.ArgumentParser(description="졸업작품 시연 - 점진적 CPU 압박(활성 Pod 한정) -> 선제 전환")
    parser.add_argument("--arm", default="proposed", choices=["native", "fixed_threshold", "proposed"])
    parser.add_argument("--rep", type=int, default=1)
    parser.add_argument("--probe-config", default=str(DEFAULT_PROBE_CONFIG))
    parser.add_argument("--timeout-sec", type=float, default=TIMEOUT_SEC)
    parser.add_argument("--sequence-index", type=int, default=1)
    parser.add_argument("--order-seed", type=int, default=1)
    parser.add_argument("--evidence-log", default=None)
    parser.add_argument("--dry-run-contract-only", action="store_true",
                         help="클러스터를 건드리지 않고 계약 해시·stage 설정만 출력하고 종료(오프라인 점검용)")
    parser.add_argument("--attempt-of", default=None,
                         help="이 실행이 기술적으로 무효화된 원본 run_id의 대체 시도임을 표시"
                              "(run_precursor_pilot_165_trial.py의 동일 기능을 이 스크립트에도 그대로 이식 - "
                              "새 메커니즘을 만들지 않음). 지정하면 --attempt-suffix/--replacement-reason도 필수.")
    parser.add_argument("--attempt-suffix", default=None,
                         help="예: retry1 - run_id에 삽입해 원본과 구분")
    parser.add_argument("--replacement-reason", default=None, help="기술적 대체 사유(사람이 읽을 설명)")
    args = parser.parse_args()

    scenario = "cpu_pressure_demo"
    if args.attempt_of:
        if not args.attempt_suffix or not args.replacement_reason:
            parser.error("--attempt-of는 --attempt-suffix와 --replacement-reason을 모두 함께 지정해야 합니다")
        run_id = f"{scenario}-{args.arm}-{args.rep:02d}-{args.attempt_suffix}-{PLAN_ID}"
    else:
        run_id = f"{scenario}-{args.arm}-{args.rep:02d}-{PLAN_ID}"
    evidence_log_path = args.evidence_log or str(_default_evidence_log_path(run_id))

    DEMO_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    existing = [p for p in (
        DEMO_RESULTS_DIR / f"contract-{run_id}.json",
        DEMO_RESULTS_DIR / f"trial-{run_id}.json",
    ) if p.exists()]
    if existing:
        raise SystemExit(f"run_id({run_id})의 결과가 이미 존재함 - 덮어쓰지 않고 중단: {[str(p) for p in existing]}")

    contract = {
        "run_id": run_id, "plan_id": PLAN_ID, "recorded_at": datetime.now(timezone.utc).isoformat(),
        "timeout_sec": args.timeout_sec, "stages": DEMO_STAGES,
        "file_sha256": _contract_hashes(),
        "detector_evidence_log_path": evidence_log_path,
        "contract_schema_version": "capstone-cpu-demo-166-v1-retry" if args.attempt_of else "capstone-cpu-demo-166-v1",
    }
    if args.attempt_of:
        original_result_path = DEMO_RESULTS_DIR / f"trial-{args.attempt_of}.json"
        original_contract_path = DEMO_RESULTS_DIR / f"contract-{args.attempt_of}.json"
        if not original_result_path.exists():
            raise SystemExit(f"--attempt-of로 지정한 원본 결과 파일이 없음: {original_result_path}")
        if not original_contract_path.exists():
            raise SystemExit(f"--attempt-of로 지정한 원본 계약 파일이 없음: {original_contract_path}")
        original_contract = json.loads(original_contract_path.read_text(encoding="utf-8"))
        current_hashes = contract["file_sha256"]
        # §168 - run_precursor_pilot_165_trial.py의 §166 patttern과 동일:
        # "변경된 하니스 해시"를 원본 계약의 file_sha256과 이번 실행의
        # file_sha256을 대조해 자동으로 남긴다(새 메커니즘 아님).
        harness_diff = {
            name: {"original_sha256": orig_hash, "current_sha256": current_hashes.get(name)}
            for name, orig_hash in original_contract.get("file_sha256", {}).items()
            if current_hashes.get(name) != orig_hash
        }
        contract["replacement"] = {
            "attempt_of_run_id": args.attempt_of,
            "replacement_reason": args.replacement_reason,
            "original_result_path": str(original_result_path),
            "original_result_sha256": hashlib.sha256(original_result_path.read_bytes()).hexdigest(),
            "original_contract_path": str(original_contract_path),
            "harness_file_sha256_diff_vs_original": harness_diff,
            "same_logical_rep": {"scenario": scenario, "arm": args.arm, "rep": args.rep, "plan_id": PLAN_ID},
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
    detector = arm_controller.make_detector_for_arm(args.arm, run_id, evidence_log_path=evidence_log_path)
    prober = make_load_ramp_prober(args.probe_config, run_id, scenario, args.arm, args.rep, args.timeout_sec)

    print(f"run_id: {run_id} / detector: {detector.name if detector else None} / "
          f"results_dir: {DEMO_RESULTS_DIR} / stages: {[s['name'] for s in DEMO_STAGES]} / "
          f"detector_evidence_log: {evidence_log_path}")
    try:
        result = run_once(
            scenario=scenario, arm=args.arm, rep=args.rep,
            sequence_index=args.sequence_index, order_seed=args.order_seed,
            injector=injector, prober=prober, timeout_sec=args.timeout_sec,
            detector=detector, run_id=run_id, is_pilot=False,
            latency_slo_sec=slo_judge.LATENCY_THRESHOLD, slo_version=slo_judge.SLO_VERSION,
            min_observation_sec=slo_judge.WINDOW_SEC,
            results_dir=DEMO_RESULTS_DIR,
        )
    except (HarnessCorrupted, TrialInvalid) as e:
        print(f"중단됨({type(e).__name__}): {e}")
        raise

    print(f"outcome: {result.outcome} / state: {result.state}")
    print(f"t_injection={result.t_injection} t_slo={result.t_slo} t_recovery={result.t_recovery}")
    print(f"결과 파일: {DEMO_RESULTS_DIR}/trial-{run_id}.json")


if __name__ == "__main__":
    main()
