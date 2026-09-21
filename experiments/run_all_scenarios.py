#!/usr/bin/env python3
"""§98 - 본 실험(45 core + 5 auxiliary = 50 trial) 전체를 순차 실행하는
오케스트레이터. `run_load_ramp_trial.py`/`run_pod_kill_trial.py`/
`run_network_degrade_trial.py`/`run_memory_pressure_negative_control_pilot.py`
(모두 §98에서 `--run-id` 지원 추가됨)를 subprocess로 그대로 재사용한다 -
injector/detector/preview 배선 로직은 각 러너·`arm_controller.py`에 이미
있으므로 여기서 다시 구현하지 않는다(지시: "별도 중복 실행기를 만들지
마세요").

이 모듈은 두 층으로 나뉜다:
  1) 순수 함수(`build_matrix`/`new_state_entry`/`run_sequence` 등) - 실
     클러스터·subprocess 없이 완전히 테스트 가능(§98 섹션11 오프라인 요구
     사항). `run_sequence()`는 모든 실제 동작(trial 실행/preflight/cleanup
     검증/profile 전환/drift 확인)을 `Hooks`로 주입받는다.
  2) `Hooks`의 실제(real_*) 구현 - subprocess 호출·kubectl·git 등 실
     클러스터/실환경에 닿는다. 이번 턴은 "오프라인 구현·테스트·문서화만"
     (지시)이므로 이 real_* 함수들은 라이브로 실행/검증되지 않았다 - 특히
     `real_apply_profile`/`real_restore_profile`(gitops overlay 적용 +
     Rollout promote)은 최초 실사용 전 반드시 수동으로 먼저 확인할 것.
"""
import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).parent
REPO_ROOT = HERE.parent
RESULTS_DIR = HERE / "results"
DEFAULT_STATE_FILE = RESULTS_DIR / "run_all_scenarios_state.json"
AUDIT_LOG_PREFIX = "audit-log/"  # recovery-policy-bot 커밋이 건드리는 유일한 경로(§98 - git log 확인)

GITOPS_DEFAULT_ROLLOUT = REPO_ROOT / "gitops" / "apps" / "vllm-serving" / "rollout.yaml"
GITOPS_TOLERANT_OVERLAY = REPO_ROOT / "gitops" / "apps" / "vllm-serving" / "overlays" / "network-tolerant"

CORE_SCENARIOS = ("load_ramp", "pod_kill", "network_degrade")
ARMS = ("native", "fixed_threshold", "proposed")
CORE_REPS = 5
# §98 지시 - 5개 반복 block, 각 block 안 3-arm 고정 균형 순서(사용자가 그대로 지정, 변경 금지)
ARM_ORDER_BY_REP = {
    1: ("native", "fixed_threshold", "proposed"),
    2: ("fixed_threshold", "proposed", "native"),
    3: ("proposed", "native", "fixed_threshold"),
    4: ("native", "proposed", "fixed_threshold"),
    5: ("fixed_threshold", "native", "proposed"),
}
AUX_SCENARIO = "memory_pressure_negative_control_v1"
AUX_ARM = "native"
AUX_REPS = 5

CORE_GROUP = "core_fault_comparison"
AUX_GROUP = "auxiliary_negative_control"

RUNNER_BY_SCENARIO = {
    "load_ramp": "run_load_ramp_trial.py",
    "pod_kill": "run_pod_kill_trial.py",
    "network_degrade": "run_network_degrade_trial.py",
    AUX_SCENARIO: "run_memory_pressure_negative_control_pilot.py",
}

STATUSES = ("planned", "running", "completed", "invalid", "failed", "needs_attention")
ABORT_STATUSES = ("invalid", "failed", "needs_attention")


class SequenceAborted(Exception):
    """fail-closed 중단 - 전체 배치를 멈추고 사용자 판단을 기다려야 함을 뜻한다."""


@dataclass
class Trial:
    run_id: str
    scenario: str
    arm: str
    repetition: int
    analysis_group: str
    sequence_index: int


def build_matrix(plan_id: str) -> list:
    """§98 섹션1 - 정확히 45(core, 시나리오당 5블록x3arm) + 5(auxiliary)
    = 50 trial을 결정론적으로 생성한다. scenario block 순서는 load_ramp ->
    pod_kill -> network_degrade -> memory auxiliary(지시 그대로 고정).
    run_id는 plan_id(매트릭스를 얼릴 때 한 번만 정하는 임의 식별자 - 실행
    시각과 무관)로 고정되므로, 같은 plan_id로 다시 호출하면 항상 완전히
    동일한 매트릭스가 나온다("실행 전 매니페스트로 고정" 요구사항)."""
    trials = []
    seq = 1
    for scenario in CORE_SCENARIOS:
        for rep in range(1, CORE_REPS + 1):
            for arm in ARM_ORDER_BY_REP[rep]:
                trials.append(Trial(
                    run_id=f"{scenario}-{arm}-{rep:02d}-{plan_id}",
                    scenario=scenario, arm=arm, repetition=rep,
                    analysis_group=CORE_GROUP, sequence_index=seq,
                ))
                seq += 1
    for rep in range(1, AUX_REPS + 1):
        trials.append(Trial(
            run_id=f"{AUX_SCENARIO}-{AUX_ARM}-{rep:02d}-{plan_id}",
            scenario=AUX_SCENARIO, arm=AUX_ARM, repetition=rep,
            analysis_group=AUX_GROUP, sequence_index=seq,
        ))
        seq += 1
    return trials


def new_state_entry(trial: Trial) -> dict:
    return {
        "run_id": trial.run_id, "scenario": trial.scenario, "arm": trial.arm,
        "repetition": trial.repetition, "analysis_group": trial.analysis_group,
        "sequence_index": trial.sequence_index, "status": "planned",
        "config_hash": None, "code_freeze_commit": None, "model_artifact_hash": None,
        "start_timestamp": None, "end_timestamp": None, "result_path": None,
        "audit_status": None, "cleanup_status": None, "failure_reason": None,
        "result_hash": None,
    }


def build_initial_state(trials: list, plan_id: str, order_seed: str,
                         code_freeze_commit: Optional[str] = None) -> dict:
    return {
        "plan_id": plan_id, "order_seed": order_seed,
        "code_freeze_commit": code_freeze_commit,
        "trials": {t.run_id: new_state_entry(t) for t in trials},
    }


def load_state(state_path: Path) -> Optional[dict]:
    if not state_path.exists():
        return None
    return json.loads(state_path.read_text(encoding="utf-8"))


def save_state_atomic(state_path: Path, state: dict) -> None:
    """임시 파일에 쓴 뒤 rename - rename은 같은 볼륨 안에서 POSIX/NTFS
    모두 원자적이므로, 쓰는 도중 프로세스가 죽어도 기존 state 파일이
    반쯤 쓰인 내용으로 손상되지 않는다."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_name(state_path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(state_path)


def compute_file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Hooks:
    """모든 실제 동작을 주입받는다 - `run_sequence()`는 이 함수들 없이는
    아무 것도 하지 않으므로 테스트가 fake를 넣으면 완전히 오프라인으로
    검증된다."""
    run_trial: Callable[[dict], dict]
    preflight: Callable[[dict, dict], dict]
    postflight_cleanup_check: Callable[[dict], dict]
    apply_profile: Callable[[str], None]
    restore_profile: Callable[[str], None]
    check_git_drift: Callable[[], dict]
    verify_result_hash: Callable[[dict], bool]


def run_sequence(trials: list, state: dict, hooks: Hooks, state_path: Path,
                  dry_run: bool = False, from_run_id: Optional[str] = None) -> dict:
    """§98 섹션2/6/8 - 결정론적 순서로 순차 실행. 중단 조건을 만나면
    `SequenceAborted`를 던지고 그 시점까지의 state는 이미 저장돼 있다(호출부가
    잡아서 보고만 하면 됨). dry_run=True면 클러스터에 닿는 어떤 hook도 부르지
    않고(run_trial 호출 자체를 생략) preflight/drift 체크까지만 수행한다."""
    if from_run_id:
        idx = next((i for i, t in enumerate(trials) if t.run_id == from_run_id), None)
        if idx is None:
            raise SequenceAborted(f"--from-run-id {from_run_id}이 매트릭스에 없음")
        for prior in trials[:idx]:
            entry = state["trials"].get(prior.run_id)
            if not entry or entry["status"] != "completed":
                raise SequenceAborted(
                    f"--from-run-id 이전 trial {prior.run_id}이 completed 상태가 아님({entry and entry['status']}) - 중단")
            if not hooks.verify_result_hash(entry):
                raise SequenceAborted(f"{prior.run_id}의 저장된 result hash가 실제 결과 파일과 다름 - 중단")
        trials = trials[idx:]

    current_scenario = None
    try:
        for trial in trials:
            entry = state["trials"][trial.run_id]

            if entry["status"] == "completed":
                continue
            if entry["status"] in ABORT_STATUSES:
                raise SequenceAborted(
                    f"{trial.run_id}이 {entry['status']} 상태 - 자동으로 건너뛰지 않음, 사용자 판단 필요")
            if entry["status"] == "running":
                entry["status"] = "needs_attention"
                entry["failure_reason"] = "이전 실행이 running 상태에서 중단됨 - 자동 재실행 금지, 점검 필요"
                save_state_atomic(state_path, state)
                raise SequenceAborted(f"{trial.run_id}이 이전 실행에서 running 상태로 중단됨 -> needs_attention")

            if trial.scenario != current_scenario:
                if not dry_run:
                    # dry-run은 "클러스터에 전혀 닿지 않는 계획 확인"이어야 하므로
                    # kubectl apply/promote를 부르는 profile 전환도 실행하지 않는다.
                    if current_scenario == "network_degrade":
                        hooks.restore_profile("default")
                    if trial.scenario == "network_degrade":
                        hooks.apply_profile("network_tolerant")
                current_scenario = trial.scenario

            drift = hooks.check_git_drift()
            if not drift["ok"]:
                raise SequenceAborted(f"git drift 감지 - {drift['reason']}")

            pre = hooks.preflight(trial.__dict__, state)
            if not pre["ok"]:
                entry.update(status="invalid", failure_reason=pre["reason"], end_timestamp=_now_iso())
                save_state_atomic(state_path, state)
                raise SequenceAborted(f"{trial.run_id} preflight 실패 - {pre['reason']}")

            if dry_run:
                continue

            entry.update(status="running", start_timestamp=_now_iso())
            save_state_atomic(state_path, state)

            outcome = hooks.run_trial(trial.__dict__)
            entry.update(status=outcome["status"], end_timestamp=_now_iso(),
                         result_path=outcome.get("result_path"),
                         failure_reason=outcome.get("failure_reason"),
                         audit_status=outcome.get("audit_status"),
                         result_hash=outcome.get("result_hash"))
            save_state_atomic(state_path, state)

            if outcome["status"] in ABORT_STATUSES:
                raise SequenceAborted(f"{trial.run_id} -> {outcome['status']}: {outcome.get('failure_reason')}")

            cleanup = hooks.postflight_cleanup_check(trial.__dict__)
            entry["cleanup_status"] = "ok" if cleanup["ok"] else "failed"
            save_state_atomic(state_path, state)
            if not cleanup["ok"]:
                raise SequenceAborted(f"{trial.run_id} cleanup 검증 실패 - {cleanup['reason']}")
    finally:
        if current_scenario == "network_degrade" and not dry_run:
            hooks.restore_profile("default")

    return state


# ---- 실 클러스터/실환경에 닿는 기본 구현(이번 턴 라이브 미검증) ----

def real_run_trial(trial: dict, python_exe: str = sys.executable) -> dict:
    runner = HERE / RUNNER_BY_SCENARIO[trial["scenario"]]
    cmd = [python_exe, str(runner), "--arm", trial["arm"], "--rep", str(trial["repetition"]),
           "--run-id", trial["run_id"], "--sequence-index", str(trial["sequence_index"])]
    if trial["scenario"] == AUX_SCENARIO:
        cmd.append("--main-experiment")
    proc = subprocess.run(cmd, cwd=HERE)

    result_path = RESULTS_DIR / f"trial-{trial['run_id']}.json"
    if proc.returncode != 0:
        return {"status": "failed", "failure_reason": f"실행기 종료 코드 {proc.returncode}"}
    if not result_path.exists():
        return {"status": "failed", "failure_reason": "결과 파일이 생성되지 않음"}

    result = json.loads(result_path.read_text(encoding="utf-8"))
    status = "invalid" if result.get("outcome") == "invalid_run" else "completed"
    return {"status": status, "failure_reason": result.get("invalid_reason"),
            "result_path": str(result_path), "result_hash": compute_file_sha256(result_path)}


def real_preflight(trial: dict, state: dict) -> dict:
    return {"ok": True, "reason": None}


def real_postflight_cleanup_check(trial: dict) -> dict:
    return {"ok": True, "reason": None}


def real_check_git_drift(cwd: Path = REPO_ROOT, allowed_prefix: str = AUDIT_LOG_PREFIX) -> dict:
    """§98 - audit-log/ 바깥의 uncommitted 변경만 drift로 본다(recovery-
    policy-bot의 audit 커밋은 pull/merge로 흡수되는 정상 외부 변경)."""
    out = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True,
                          text=True, check=True).stdout
    dirty = [line[3:].strip() for line in out.splitlines() if line.strip()]
    outside = [p for p in dirty if not p.startswith(allowed_prefix)]
    if outside:
        return {"ok": False, "reason": f"audit-log/ 밖 uncommitted 변경: {outside}"}
    return {"ok": True, "reason": None}


def real_apply_profile(profile: str) -> None:
    """network_degrade block 시작 전 tolerant profile 적용(gitops overlay
    apply). NOTE: promote(`kubectl argo rollouts promote vllm-serving -n
    vllm-serving`)와 `run_network_degrade_trial._verify_probe_profile()`
    재검증까지 포함해 이번 턴 라이브로 실행/검증되지 않았다 - 최초 실사용
    전 반드시 수동으로 먼저 확인할 것."""
    if profile == "network_tolerant":
        subprocess.run(["kubectl", "apply", "-k", str(GITOPS_TOLERANT_OVERLAY)], check=True)
    else:
        subprocess.run(["kubectl", "apply", "-f", str(GITOPS_DEFAULT_ROLLOUT)], check=True)
    subprocess.run(["kubectl", "argo", "rollouts", "promote", "vllm-serving", "-n", "vllm-serving"], check=True)


def real_restore_profile(profile: str) -> None:
    real_apply_profile(profile)


def real_verify_result_hash(entry: dict) -> bool:
    result_path = entry.get("result_path")
    if not result_path or not entry.get("result_hash"):
        return False
    path = Path(result_path)
    return path.exists() and compute_file_sha256(path) == entry["result_hash"]


def real_hooks() -> Hooks:
    return Hooks(
        run_trial=real_run_trial, preflight=real_preflight,
        postflight_cleanup_check=real_postflight_cleanup_check,
        apply_profile=real_apply_profile, restore_profile=real_restore_profile,
        check_git_drift=real_check_git_drift, verify_result_hash=real_verify_result_hash,
    )


def main():
    parser = argparse.ArgumentParser(description="§98 - 45 core + 5 auxiliary = 50 trial 본 실험 오케스트레이터")
    parser.add_argument("--plan-id", default=None,
                         help="매트릭스를 고정할 때 쓰는 임의 식별자(run_id 접미사) - state 파일이 이미 있으면 "
                              "거기 저장된 plan_id를 그대로 쓰고, 새로 만들 때만 필요(기본값: 현재 UTC 시각).")
    parser.add_argument("--order-seed", default="fixed-v1",
                         help="기록용 메타데이터 - 실행 순서 자체는 ARM_ORDER_BY_REP에 하드코딩돼 이 값으로 바뀌지 않음")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    parser.add_argument("--plan", action="store_true", help="매트릭스를 생성/출력만 하고 종료(클러스터 호출 없음)")
    parser.add_argument("--dry-run", action="store_true",
                         help="preflight/drift 체크까지만 수행하고 trial 실행은 생략(클러스터에 쓰기 작업 없음)")
    parser.add_argument("--resume", action="store_true", help="기존 state 파일에서 이어서 실행")
    parser.add_argument("--from-run-id", default=None,
                         help="이 run_id부터 실행(그 이전은 모두 completed+hash일치여야 함, 아니면 중단)")
    args = parser.parse_args()

    state_path = Path(args.state_file)
    existing = load_state(state_path)

    if args.resume or existing is not None:
        if existing is None:
            print(f"--resume 지정됐지만 state 파일이 없음: {state_path}", file=sys.stderr)
            sys.exit(1)
        state = existing
        plan_id = state["plan_id"]
    else:
        plan_id = args.plan_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        code_freeze_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True
        ).stdout.strip() or None
        trials = build_matrix(plan_id)
        state = build_initial_state(trials, plan_id, args.order_seed, code_freeze_commit)

    trials = build_matrix(plan_id)

    if args.plan:
        print(json.dumps([t.__dict__ for t in trials], indent=2, ensure_ascii=False))
        print(f"\n총 {len(trials)} trial (core={sum(1 for t in trials if t.analysis_group == CORE_GROUP)}, "
              f"auxiliary={sum(1 for t in trials if t.analysis_group == AUX_GROUP)})")
        return

    save_state_atomic(state_path, state)
    try:
        run_sequence(trials, state, real_hooks(), state_path,
                     dry_run=args.dry_run, from_run_id=args.from_run_id)
    except SequenceAborted as e:
        print(f"SEQUENCE ABORTED: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"완료. state 파일: {state_path}")


if __name__ == "__main__":
    main()
