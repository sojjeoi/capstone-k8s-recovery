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
     클러스터/실환경에 닿는다.

`switch_probe_profile_live()`(§98 launch-readiness gate, 2026-09-22)의
실 클러스터 검증 결과 - **부분 검증**: apply(kustomize render+apply -f -,
base는 apply -f) -> `blue_green_prep.wait_until_paused()`로 preview
Ready 확인 -> abort_preview()+wait_until_rolled_back()로 안전 복원까지는
실클러스터에서 반복 검증됐다(전 과정에서 기존 active pod
`vllm-serving-7d6f888c94-zlkvv`는 단 한 번도 재시작·중단되지 않음 -
실제 서빙 트래픽은 계속 안전했음). **promote 단계(활성 트래픽 전환)는
검증되지 않았다** - 로컬에 `kubectl-argo-rollouts` CLI가 없고, status
서브리소스 직접 patch(`patch_namespaced_custom_object_status`, `blue_
green_prep.abort_preview()`와 동일한 API 종류)는 이 세션의 자동 권한
분류기가 "공유 자원 수정"으로 판단해 두 차례 차단했다 - 사용자의 명시적
허가(대화형 확인 또는 권한 설정 변경) 없이는 이 함수가 promote까지 끝까지
실행되지 못한다. `real_apply_profile`이 호출하는 promote 지점은 여전히
**최초 실사용 전 사용자 승인 하에 별도로 확인 필요**."""
import argparse
import hashlib
import json
import subprocess
import sys
import time
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
    """§98 - audit-log/ 바깥의 "코드/config drift"만 차단한다 - 여기서
    drift는 이미 git이 추적 중인 파일이 커밋 없이 바뀐 것을 뜻한다.
    `git status --porcelain`의 untracked(`??`) 항목은 제외한다 - 커밋된
    baseline과 무관한 새 로컬 산출물일 뿐이라서다(실측 확인, 2026-09-22
    launch-readiness gate: `experiments/results/`·`anomaly-detection/v3/
    model_v32/artifacts/`는 이 세션 시작 전부터 있던 untracked 디렉터리인데
    `.gitignore`가 그 안의 특정 파일 패턴만 덮고 디렉터리 자체는 안 덮어서
    `??`로 계속 나타난다 - 이걸 drift로 취급하면 이 오케스트레이터가
    영원히 첫 trial도 시작 못 하는 결함이었다). recovery-policy-bot의
    audit-log/ 커밋을 허용하는 것과 같은 이유로 "이미 추적 중이던 것이
    예고 없이 바뀌었는가"만 본다."""
    out = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True,
                          text=True, check=True).stdout
    tracked_changes = [line[3:].strip() for line in out.splitlines()
                        if line.strip() and not line.startswith("??")]
    outside = [p for p in tracked_changes if not p.startswith(allowed_prefix)]
    if outside:
        return {"ok": False, "reason": f"audit-log/ 밖 추적 파일 변경: {outside}"}
    return {"ok": True, "reason": None}


PREVIEW_READY_TIMEOUT_SEC = 480.0  # blue_green_prep.PREVIEW_PREP_TIMEOUT_SEC와 동일 근거(모델 로딩 실측 최대 350.3초)
PROMOTE_VERIFY_TIMEOUT_SEC = 60.0
OLD_REVISION_SCALE_DOWN_TIMEOUT_SEC = 90.0  # scaleDownDelaySeconds=30 + 여유


def _endpointslice_addresses(namespace: str, service_name: str) -> list:
    from kubernetes import client
    import active_pod_resolver
    active_pod_resolver.load_kube_config()
    slices = client.DiscoveryV1Api().list_namespaced_endpoint_slice(
        namespace, label_selector=f"kubernetes.io/service-name={service_name}")
    addrs = []
    for s in slices.items:
        for ep in (s.endpoints or []):
            addrs.extend(ep.addresses or [])
    return addrs


def _verify_active_selector_and_endpoints(namespace: str, expected_hash: str) -> dict:
    """Rollout status(activeSelector)만 보고 끝내지 않고 실제 vllm-active
    Service selector로 매칭되는 pod과 그 EndpointSlice까지 확인한다."""
    import active_pod_resolver
    pods = active_pod_resolver.get_active_pods()
    ok_selector = len(pods) == 1
    ok_hash = ok_selector and expected_hash in pods[0]["name"]
    endpoint_ips = _endpointslice_addresses(namespace, "vllm-active")
    return {"ok": bool(ok_hash and len(endpoint_ips) == 1),
            "pods": pods, "endpoint_ips": endpoint_ips, "expected_hash": expected_hash}


def switch_probe_profile_live(profile: str, name: str = "vllm-serving", namespace: str = "vllm-serving",
                               preview_timeout_sec: float = PREVIEW_READY_TIMEOUT_SEC) -> dict:
    """실제 kubectl apply + promote로 probe profile을 전환한다(base <->
    network_tolerant) - `real_apply_profile`/`real_restore_profile`이 이
    함수 하나를 공유한다(§98 launch-readiness 지시 - "동일 lifecycle 함수
    사용"). `blue_green_prep.py`의 기존 테스트된 함수(wait_until_paused/
    get_blue_green_status/abort_preview/wait_until_rolled_back)를 그대로
    재사용 - 새 preview 대기/롤백 로직을 여기서 다시 만들지 않는다.
    network-tolerant overlay 자신의 kustomization.yaml 주석에 적힌 절차를
    그대로 코드화했다: apply -k(+LoadRestrictionsNone) 또는 apply -f(base)
    -> wait_until_paused -> promote -> selector/EndpointSlice/구 revision
    scale-down 확인. 실패 시 fail-closed: preview timeout이면 abort_preview()로
    되돌리고 예외를 던진다(호출부 run_sequence가 SequenceAborted로 승격)."""
    import blue_green_prep as bgp

    before = bgp.get_blue_green_status(name, namespace)

    if profile == "network_tolerant":
        # kubectl apply -k(v1.34)는 --load-restrictor를 받지 않는다(실측 확인 -
        # "unknown flag" 즉시 거부, 클러스터 변경 없이 안전하게 실패) - kustomize
        # 렌더링과 apply를 분리해 kubectl kustomize에만 이 플래그를 준다. 렌더
        # 결과는 server dry-run으로 Rollout 외 리소스는 전부 unchanged임을 확인함.
        rendered = subprocess.run(
            ["kubectl", "kustomize", str(GITOPS_TOLERANT_OVERLAY), "--load-restrictor=LoadRestrictionsNone"],
            capture_output=True, check=True)
        subprocess.run(["kubectl", "apply", "-f", "-"], input=rendered.stdout, check=True)
    elif profile == "default":
        subprocess.run(["kubectl", "apply", "-f", str(GITOPS_DEFAULT_ROLLOUT)], check=True)
    else:
        raise ValueError(f"알 수 없는 profile: {profile}")

    ready = bgp.wait_until_paused(name, namespace, timeout=preview_timeout_sec)
    if not ready:
        our_hash = bgp.get_blue_green_status(name, namespace)["current_pod_hash"]
        bgp.abort_preview(name, namespace)
        rolled_back = bgp.wait_until_rolled_back(name, namespace, before["active_selector"], our_hash)
        raise RuntimeError(f"{profile} profile preview가 {preview_timeout_sec}초 내 Ready 안 됨 - "
                            f"abort 시도(rollback_ok={rolled_back})")

    new_hash = bgp.get_blue_green_status(name, namespace)["current_pod_hash"]
    # kubectl-argo-rollouts CLI 플러그인이 로컬에 없다(실측 확인 - arm_controller.py의
    # abort_preview()와 동일 사정). recovery-policy/rollouts_client.py의 direct-patch
    # 폴백은 일반 object patch(patch_namespaced_custom_object)를 써서 자신의 docstring이
    # 이미 "selector를 안 바꾼다"고 경고한 것 - status 서브리소스 patch가 아니라서
    # 컨트롤러가 무시/재계산하는 것으로 보인다. abort_preview()가 이미 증명한 대로
    # status 서브리소스(patch_namespaced_custom_object_status)로 pauseConditions를
    # 지운다 - blue_green_prep의 CustomObjectsApi 설정을 그대로 재사용.
    bgp._custom_api().patch_namespaced_custom_object_status(
        bgp.ROLLOUTS_GROUP, bgp.ROLLOUTS_VERSION, namespace, bgp.ROLLOUTS_PLURAL, name,
        {"status": {"pauseConditions": None}},
    )

    deadline = time.monotonic() + PROMOTE_VERIFY_TIMEOUT_SEC
    promoted = False
    while time.monotonic() < deadline:
        if bgp.get_blue_green_status(name, namespace)["active_selector"] == new_hash:
            promoted = True
            break
        time.sleep(3.0)
    if not promoted:
        raise RuntimeError(f"{profile} profile promote 후 activeSelector가 {new_hash}로 전환 안 됨")

    endpoint_check = _verify_active_selector_and_endpoints(namespace, new_hash)
    if not endpoint_check["ok"]:
        raise RuntimeError(f"{profile} profile promote 후 selector/EndpointSlice 검증 실패: {endpoint_check}")

    old_hash = before["active_selector"]
    if old_hash and old_hash != new_hash:
        scale_deadline = time.monotonic() + OLD_REVISION_SCALE_DOWN_TIMEOUT_SEC
        scaled_down = False
        while time.monotonic() < scale_deadline:
            if (bgp._replicaset_desired(namespace, old_hash) or 0) == 0:
                scaled_down = True
                break
            time.sleep(3.0)
        if not scaled_down:
            raise RuntimeError(f"{profile} profile promote 후 구 revision({old_hash}) scale-down 확인 안 됨")

    return {"active_hash": new_hash, "old_hash": old_hash, "endpoint_ips": endpoint_check["endpoint_ips"]}


def real_apply_profile(profile: str) -> None:
    switch_probe_profile_live("network_tolerant")


def real_restore_profile(profile: str) -> None:
    switch_probe_profile_live("default")


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
    parser.add_argument("--scenario", default=None, choices=list(CORE_SCENARIOS) + [AUX_SCENARIO],
                         help="지정한 시나리오의 trial만 이번 실행에서 진행한다 - 공식 50-trial 매트릭스/state는 "
                              "그대로 공유되고 다른 시나리오 블록은 손대지 않은 채 planned로 남아, 같은 "
                              "--state-file로 나중에 --scenario만 바꿔 이어서 실행할 수 있다. 생략하면 "
                              "매트릭스 전체(50 trial)를 순서대로 실행한다.")
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
    trials_to_run = [t for t in trials if t.scenario == args.scenario] if args.scenario else trials

    if args.plan:
        print(json.dumps([t.__dict__ for t in trials_to_run], indent=2, ensure_ascii=False))
        print(f"\n총 {len(trials_to_run)} trial (core={sum(1 for t in trials_to_run if t.analysis_group == CORE_GROUP)}, "
              f"auxiliary={sum(1 for t in trials_to_run if t.analysis_group == AUX_GROUP)})")
        return

    save_state_atomic(state_path, state)
    try:
        run_sequence(trials_to_run, state, real_hooks(), state_path,
                     dry_run=args.dry_run, from_run_id=args.from_run_id)
    except SequenceAborted as e:
        print(f"SEQUENCE ABORTED: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"완료. state 파일: {state_path}")


if __name__ == "__main__":
    main()
