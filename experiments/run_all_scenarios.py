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
        # §109(2026-09-24) - cleanup_status(ok/failed)만으로는 실패 사유가 안 남는다.
        # postflight가 이제 completed/invalid/failed(러너 예외 포함) 4개 종료 경로
        # 전부에서 시도되므로(run_sequence() 참고), 그 실패 사유를 원래 trial
        # 실패 사유와 별도로 보존한다(둘 다 필요하면 SequenceAborted 메시지에서
        # " | 추가로 postflight cleanup도 실패: ..."로 합쳐서 드러남).
        "cleanup_reason": None,
        # §111(2026-09-24) - real_safety_checks()가 이미 계산해두고도 버려지던
        # 항목별 세부 결과(checks dict)를 이제 durable하게 남긴다 - "Healthy"와
        # "aborted_preview_rolled_back"(§111 Rollout 예외)처럼 같은 ok=True라도
        # 서로 다른 근거로 통과했는지를 사후에 구분하기 위함(§110 사고 - 이
        # 구분이 없어서 처음엔 이걸 진짜 문제로 오인할 뻔했다).
        "preflight_checks": None,
        "postflight_checks": None,
        # §111 - completed인데 cleanup_status=failed인 trial을 재개 흐름에
        # 다시 포함시키려면, 재실행이 아니라 사람이 현재 클러스터 상태·당시
        # 이벤트 근거를 직접 대조해 내린 판정을 여기 별도로 기록해야 한다
        # (adjudicate_cleanup_failure() 참고) - 원본 cleanup_status/
        # cleanup_reason은 이 판정이 있어도 절대 덮어쓰지 않는다.
        "cleanup_adjudication": None,
    }


def build_initial_state(trials: list, plan_id: str, order_seed: str,
                         code_freeze_commit: Optional[str] = None) -> dict:
    return {
        "plan_id": plan_id, "order_seed": order_seed,
        "code_freeze_commit": code_freeze_commit,
        "trials": {t.run_id: new_state_entry(t) for t in trials},
    }


# §103(2026-09-24) - pod_kill-proposed-01-mainexp-v1이 HarnessCorrupted(subprocess
# 비정상 종료)로 "failed"가 된 것과, TrialResult.outcome="invalid_run"(예: SLO
# 평가 불가·탐지 실패)이 정상적으로 기록된 "invalid"는 서로 다르다 - 후자는
# 계약서·지시 전체에서 반복적으로 "유효한 실험 결과이니 보존·포함"하라고 못박은
# 것이고, 전자만 "기술적 invalid"(하니스/인프라 결함으로 결과 자체가 안 나온 것)
# 다. 대체 연결은 오직 "failed"에만 허용한다 - "invalid"를 대체하면 정당한
# 데이터 포인트를 몰래 지우는 것이 된다.
TECHNICAL_INVALID_STATUSES = ("failed",)


def link_technical_invalid_replacement(state: dict, original_run_id: str,
                                        replacement_run_id: str, reason: str) -> dict:
    """§103 - 기술적 invalid(TECHNICAL_INVALID_STATUSES) 슬롯에만, 사용자가
    명시적으로 지정한 새 고유 run_id를 연결한다. 자동 생성·자동 재시도가
    아니다 - 호출자(CLI)가 매번 사람이 직접 고른 값을 넘겨야 한다.

    원본 슬롯(state["trials"][original_run_id])은 이 함수가 절대 건드리지
    않는다 - 원본 덮어쓰기 방지. 관계·사유·각 결과 hash는 별도의
    state["replacements"][original_run_id]에만 기록한다(TrialResult
    스키마 무관 - 오케스트레이터 자체의 state 구조 확장일 뿐).

    이미 연결된 원본을 다시 연결하려 하면 거부한다(임의 반복 재시도
    방지 - 링크는 평생 한 번만). 대체 run_id가 이미 매트릭스/state에
    있으면(고유해야 함) 거부한다. 대체 trial은 원본과 완전히 같은
    scenario/arm/repetition/analysis_group/sequence_index로 state에
    새로 추가되므로, 나중에 build_matrix()+apply_replacements()가
    원본의 정확히 그 위치에서 실행 순서를 그대로 유지한다."""
    if original_run_id not in state["trials"]:
        raise ValueError(f"{original_run_id}이 state에 없음 - 매트릭스에 없는 run_id는 연결 불가")
    original_entry = state["trials"][original_run_id]
    if original_entry["status"] not in TECHNICAL_INVALID_STATUSES:
        raise ValueError(
            f"{original_run_id}은 status={original_entry['status']!r} - "
            f"기술적 invalid({TECHNICAL_INVALID_STATUSES})만 대체 연결 가능. "
            f"'invalid'(outcome=invalid_run)는 유효한 실험 결과이므로 절대 대체하지 않음.")
    if replacement_run_id == original_run_id:
        raise ValueError("대체 run_id는 원본과 달라야 함(고유해야 함)")
    if replacement_run_id in state["trials"]:
        raise ValueError(f"대체 run_id({replacement_run_id})가 이미 매트릭스/state에 존재함 - 고유한 새 값이어야 함")

    replacements = state.setdefault("replacements", {})
    if original_run_id in replacements:
        existing = replacements[original_run_id]["replacement_run_id"]
        raise ValueError(
            f"{original_run_id}은 이미 {existing}로 대체 연결됨 - 임의 반복 재시도 금지. "
            f"기존 연결을 그대로 쓰거나(이미 실행됐다면 그 결과를 확인), 그 대체 자체가 또 "
            f"기술적 invalid가 된 경우에만 그 대체 run_id를 원본으로 삼아 새로 연결할 것.")

    replacements[original_run_id] = {
        "replacement_run_id": replacement_run_id,
        "reason": reason,
        "linked_at_utc": _now_iso(),
        "original_result_hash": original_entry.get("result_hash"),
        "replacement_result_hash": None,  # 대체 trial 실행 후 run_sequence()가 채움
        "replacement_status": "planned",
    }
    replacement_entry = dict(original_entry)
    replacement_entry.update({
        "run_id": replacement_run_id, "status": "planned",
        "start_timestamp": None, "end_timestamp": None, "result_path": None,
        "audit_status": None, "cleanup_status": None, "failure_reason": None,
        "result_hash": None,
    })
    state["trials"][replacement_run_id] = replacement_entry
    return state


def sync_replacement_results(state: dict) -> None:
    """§103 - 대체 trial이 실행된 뒤(성공/중단 무관), state["replacements"]의
    각 연결 기록에 대체 trial의 최신 status/result_hash를 반영한다. 원본
    기록(state["trials"][original_run_id])은 절대 건드리지 않는다 -
    이 함수는 replacements 딕셔너리만 갱신한다."""
    for link in state.get("replacements", {}).values():
        replacement_entry = state["trials"].get(link["replacement_run_id"])
        if replacement_entry is not None:
            link["replacement_result_hash"] = replacement_entry.get("result_hash")
            link["replacement_status"] = replacement_entry.get("status")


def adjudicate_cleanup_failure(state: dict, run_id: str, verdict: str, reason: str,
                                evidence: Optional[dict] = None) -> None:
    """§111(2026-09-24, load_ramp-fixed_threshold-01-mainexp-v2 계기) -
    `status=completed`인데 `cleanup_status=failed`인 trial을 재개 흐름에서
    다시 진행시키려면(그 trial 자체를 재실행하는 게 아니다 - trial 결과는
    이미 유효할 수 있음), run_sequence()가 자동으로 판단하지 않고 **사람이
    현재 클러스터 상태와 당시 이벤트 근거를 직접 대조해 내린 판정**을 이
    함수로 별도 기록해야 한다. 원본 `cleanup_status`/`cleanup_reason`은
    이 판정이 생겨도 절대 덮어쓰지 않는다(원래 실패 기록 그대로 보존 -
    link_technical_invalid_replacement()가 원본 trial slot을 절대 안
    건드리는 것과 같은 원칙).

    verdict:
      "resolved_false_positive" - 재검증 결과 실제 문제가 아니었음이
        확인됨(예: §111의 Rollout phase=Degraded+RolloutAborted 오탐).
        이 값만 run_sequence()의 재개 skip-check를 통과시킨다.
      "confirmed_problem" - 재검증 결과 진짜 문제로 확정됨. 판정 자체는
        기록되지만 run_sequence()는 여전히 자동으로 건너뛰지 않는다 -
        재실행 여부·방법은 이 함수의 책임 밖(완전히 별도 결정 필요)."""
    if run_id not in state["trials"]:
        raise ValueError(f"{run_id}이 state에 없음")
    entry = state["trials"][run_id]
    if entry["status"] != "completed":
        raise ValueError(f"{run_id}의 status가 completed가 아님({entry['status']!r}) - adjudication 대상이 아님")
    if entry.get("cleanup_status") == "ok":
        raise ValueError(f"{run_id}은 이미 cleanup_status=ok - adjudication 불필요")
    if verdict not in ("resolved_false_positive", "confirmed_problem"):
        raise ValueError(f"알 수 없는 verdict: {verdict!r}(resolved_false_positive|confirmed_problem만 허용)")
    entry["cleanup_adjudication"] = {
        "verdict": verdict, "reason": reason, "evidence": evidence or {},
        "adjudicated_at_utc": _now_iso(),
    }


def apply_replacements(trials: list, replacements: dict) -> list:
    """§103 - 실행 목록(순서 그대로)에서, 연결된 대체가 있는 원본 자리를
    정확히 같은 위치의 대체 Trial로 바꿔치기한다. 연결 안 된 trial은
    그대로 둔다. 원본 리스트를 변형하지 않고 새 리스트를 반환한다 -
    호출자가 이 결과를 run_sequence()에 넘기면 원본 run_id는 다시 실행
    시도되지 않고(있는 그대로 state에 남음), 대체 run_id만 그 슬롯에서
    정상적으로 preflight/실행/cleanup을 거친다."""
    replaced = []
    for t in trials:
        link = replacements.get(t.run_id)
        if link is not None:
            replaced.append(Trial(run_id=link["replacement_run_id"], scenario=t.scenario, arm=t.arm,
                                   repetition=t.repetition, analysis_group=t.analysis_group,
                                   sequence_index=t.sequence_index))
        else:
            replaced.append(t)
    return replaced


def verify_and_backfill_original_hash(state: dict, original_run_id: str,
                                       results_dir: Path = RESULTS_DIR) -> None:
    """§105(2026-09-24, pod_kill-proposed-01-mainexp-v1 계기) - 대체가
    연결된 원본의 `state["replacements"][original]["original_result_hash"]`
    가 null인 경우(예: link 당시 원본 slot 자체의 result_hash가 §105
    수정 이전 버전 real_run_trial()로 인해 비어 있었던 경우)만 실제 파일
    기준으로 채운다. **원본 trial slot(state["trials"][original_run_id])
    은 절대 건드리지 않는다** - 이 함수가 쓰는 건 replacements 링크
    기록뿐이다.

    이미 채워져 있으면(null이 아니면) 그 값을 신뢰하지 않고 매번 실제
    파일의 현재 hash와 재대조한다 - 재개 때마다 호출되므로 이게 바로
    "이후 재개 때도 불일치하면 fail-closed"에 해당한다(파일이 사후에
    손상·변조됐을 가능성을 매번 다시 확인). 파일이 없거나, 파일 안의
    run_id가 원본과 다르면(잘못된 파일을 가리키고 있을 위험) 즉시
    ValueError로 fail-closed한다."""
    if original_run_id not in state.get("replacements", {}):
        raise ValueError(f"{original_run_id}에 연결된 대체가 없음 - link_technical_invalid_replacement()를 먼저 호출할 것")
    link = state["replacements"][original_run_id]
    result_path = results_dir / f"trial-{original_run_id}.json"
    if not result_path.exists():
        raise ValueError(f"원본 결과 파일이 없음: {result_path} - hash 검증 불가(fail-closed)")
    data = json.loads(result_path.read_text(encoding="utf-8"))
    if data.get("run_id") != original_run_id:
        raise ValueError(
            f"원본 결과 파일({result_path})의 run_id({data.get('run_id')!r})가 "
            f"기대값({original_run_id!r})과 다름 - fail-closed")
    actual_hash = compute_file_sha256(result_path)
    stored = link.get("original_result_hash")
    if stored is not None and stored != actual_hash:
        raise ValueError(
            f"{original_run_id}의 저장된 original_result_hash({stored})가 "
            f"실제 파일 hash({actual_hash})와 불일치 - fail-closed")
    link["original_result_hash"] = actual_hash


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


def _completed_trial_resumable_reason(entry: dict, hooks: "Hooks") -> Optional[str]:
    """§111(2026-09-24) - "completed니까 건너뛰어도 된다"고 판단하기 전에
    반드시 통과해야 하는 두 조건을 한곳에 모은다(메인 루프의 매 trial
    skip-check와 `--from-run-id`의 사전 검증 둘 다 이 함수를 쓴다 - 규칙이
    두 곳에서 갈라지지 않게). 통과하면 None, 실패하면 중단 사유 문자열을
    반환한다(호출부가 `SequenceAborted`로 승격).

    1) `cleanup_status != "ok"`인데 사람이 명시적으로 `verdict=
       resolved_false_positive`로 adjudicate하지 않았으면 건너뛰지 않는다
       (load_ramp-fixed_threshold-01-mainexp-v2 사고 - 예전엔 status만 보고
       조용히 건너뛰었다). `adjudicate_cleanup_failure()`가 남기는 판정만
       인정하고, 원본 `cleanup_status`/`cleanup_reason`은 이 함수가 절대
       참조해 덮어쓰지 않는다.
    2) hash가 실제 파일과 다르면(사후 손상·변조 가능성) 건너뛰지 않는다 -
       `cleanup_status`가 이미 ok였어도 매번 재확인한다."""
    if entry.get("cleanup_status") != "ok":
        adjudication = entry.get("cleanup_adjudication")
        if not adjudication or adjudication.get("verdict") != "resolved_false_positive":
            return (f"{entry['run_id']}이 completed지만 cleanup_status={entry.get('cleanup_status')!r} - "
                    f"명시적 adjudication(verdict=resolved_false_positive) 없이는 자동으로 건너뛰지 않음")
    if not hooks.verify_result_hash(entry):
        return f"{entry['run_id']}의 저장된 result hash가 실제 결과 파일과 다름 - 중단"
    return None


def run_sequence(trials: list, state: dict, hooks: Hooks, state_path: Path,
                  dry_run: bool = False, from_run_id: Optional[str] = None) -> dict:
    """§98 섹션2/6/8 - 결정론적 순서로 순차 실행. 중단 조건을 만나면
    `SequenceAborted`를 던지고 그 시점까지의 state는 이미 저장돼 있다(호출부가
    잡아서 보고만 하면 됨). dry_run=True면 클러스터에 닿는 어떤 hook도 부르지
    않고(run_trial 호출 자체를 생략) preflight/drift 체크까지만 수행한다.

    §109(2026-09-24) - `run_trial()`의 네 가지 종료 경로(정상 완료/invalid/
    failed/러너 자체의 예외) 전부에서 `postflight_cleanup_check()`를 반드시
    시도하고 그 결과를 state(`cleanup_status`/`cleanup_reason`)에 기록한다 -
    §108까지는 invalid/failed면 postflight 호출 전에 즉시 중단해 클러스터가
    깨끗한지 전혀 확인하지 않았다(크래시·무효 상태일 때야말로 확인이 가장
    필요한 순간이었는데 정작 건너뛰고 있었음). 네 경로 중 어느 것도 다음
    trial로 자동으로 넘어가지 않는다 - trial 자체 실패 사유는 그대로 보존되고,
    postflight까지 실패하면 두 사유가 `SequenceAborted` 메시지에 모두
    드러난다."""
    if from_run_id:
        idx = next((i for i, t in enumerate(trials) if t.run_id == from_run_id), None)
        if idx is None:
            raise SequenceAborted(f"--from-run-id {from_run_id}이 매트릭스에 없음")
        for prior in trials[:idx]:
            entry = state["trials"].get(prior.run_id)
            if not entry or entry["status"] != "completed":
                raise SequenceAborted(
                    f"--from-run-id 이전 trial {prior.run_id}이 completed 상태가 아님({entry and entry['status']}) - 중단")
            reason = _completed_trial_resumable_reason(entry, hooks)
            if reason:
                raise SequenceAborted(reason)
        trials = trials[idx:]

    current_scenario = None
    try:
        for trial in trials:
            entry = state["trials"][trial.run_id]

            if entry["status"] == "completed":
                # §111(2026-09-24, load_ramp-fixed_threshold-01-mainexp-v2 계기) -
                # status만 보고 건너뛰면 안 된다. cleanup_status=failed인데
                # 명시적 adjudication(verdict=resolved_false_positive) 없이
                # 자동으로 넘어가던 gap이 있었다 - 그리고 completed+cleanup=ok라도
                # 결과 파일이 사후에 손상/변조됐을 수 있으니 매번 hash를 재확인한다.
                reason = _completed_trial_resumable_reason(entry, hooks)
                if reason:
                    raise SequenceAborted(reason)
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
            # §111 - real_safety_checks()가 이미 계산한 항목별 세부 결과를
            # durable하게 남긴다("Healthy" vs "aborted_preview_rolled_back"처럼
            # 같은 ok=True도 서로 다른 근거로 통과했는지 사후 구분 가능해야 함).
            entry["preflight_checks"] = pre.get("checks")
            if not pre["ok"]:
                entry.update(status="invalid", failure_reason=pre["reason"], end_timestamp=_now_iso())
                save_state_atomic(state_path, state)
                raise SequenceAborted(f"{trial.run_id} preflight 실패 - {pre['reason']}")

            if dry_run:
                continue

            entry.update(status="running", start_timestamp=_now_iso())
            save_state_atomic(state_path, state)

            # §109(2026-09-24) - run_trial()이 dict를 반환하지 않고 예외 자체를
            # 던지는 경우(예: real_run_trial()의 subprocess.run()이 FileNotFoundError
            # 등을 던짐)도 "failed" 종료 경로로 통일한다 - 이전엔 이 경우 run_sequence()
            # 밖으로 그대로 새 나가 postflight/state 기록을 전혀 안 거치고
            # 오케스트레이터 프로세스 자체가 죽었다(§109 이전 test_exception_during_
            # network_degrade_block_still_restores_profile이 바로 이 옛 동작을
            # 검증하던 테스트 - 이제 SequenceAborted로 바뀐 것을 검증하도록 갱신).
            try:
                outcome = hooks.run_trial(trial.__dict__)
            except SequenceAborted:
                raise
            except Exception as e:
                outcome = {"status": "failed", "failure_reason": f"run_trial 예외: {type(e).__name__}: {e}"}

            entry.update(status=outcome["status"], end_timestamp=_now_iso(),
                         result_path=outcome.get("result_path"),
                         failure_reason=outcome.get("failure_reason"),
                         audit_status=outcome.get("audit_status"),
                         result_hash=outcome.get("result_hash"))
            save_state_atomic(state_path, state)

            # §109 - completed/invalid/failed(러너 예외 포함) **네 경로 전부**에서
            # postflight를 항상 시도한다. §108까지는 outcome이 ABORT_STATUSES면
            # 여기 도달 전에 즉시 중단해 postflight 자체를 건너뛰었다 - 크래시·무효
            # 상태일 때야말로 클러스터가 깨끗한지 확인이 가장 필요한데 정작 확인을
            # 안 하고 있었다(사용자 지적). postflight 자신이 예외를 던져도(예상 밖
            # K8s API 오류 등) 같은 방식으로 "실패"로 흡수해 반드시 결과를 state에
            # 남긴다.
            try:
                cleanup = hooks.postflight_cleanup_check(trial.__dict__)
            except SequenceAborted:
                raise
            except Exception as e:
                cleanup = {"ok": False, "reason": f"postflight_cleanup_check 예외: {type(e).__name__}: {e}"}

            entry["cleanup_status"] = "ok" if cleanup["ok"] else "failed"
            entry["cleanup_reason"] = cleanup.get("reason")
            entry["postflight_checks"] = cleanup.get("checks")
            save_state_atomic(state_path, state)

            # 어느 실패에서도(원본 trial 실패든 cleanup 실패든) 다음 trial로 넘어가지
            # 않는다 - 원본 실패 사유는 보존하고, cleanup까지 실패하면 두 사유를 모두
            # 드러낸다.
            if outcome["status"] in ABORT_STATUSES:
                reason = f"{trial.run_id} -> {outcome['status']}: {outcome.get('failure_reason')}"
                if not cleanup["ok"]:
                    reason += f" | 추가로 postflight cleanup도 실패: {cleanup['reason']}"
                raise SequenceAborted(reason)

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
        # §105(2026-09-24, pod_kill-proposed-01-mainexp-v1 계기) - 실행기가
        # 비정상 종료해도(예: HarnessCorrupted) run_once.py는 그 전에 이미
        # 결과 JSON을 써둔 상태일 수 있다(예외 던지기 전에 _write_result가
        # 먼저 끝남). 그 파일이 실제로 있으면 경로·hash를 같이 보존한다 -
        # 파일이 정말 없는 경우(더 이른 단계에서 죽음)와는 구분해야 나중에
        # 대체 연결(link_technical_invalid_replacement)이 원본 hash를
        # 검증할 근거가 생긴다.
        if result_path.exists():
            return {"status": "failed", "failure_reason": f"실행기 종료 코드 {proc.returncode}",
                    "result_path": str(result_path), "result_hash": compute_file_sha256(result_path)}
        return {"status": "failed", "failure_reason": f"실행기 종료 코드 {proc.returncode}"}
    if not result_path.exists():
        return {"status": "failed", "failure_reason": "결과 파일이 생성되지 않음"}

    result = json.loads(result_path.read_text(encoding="utf-8"))
    status = "invalid" if result.get("outcome") == "invalid_run" else "completed"
    return {"status": status, "failure_reason": result.get("invalid_reason"),
            "result_path": str(result_path), "result_hash": compute_file_sha256(result_path)}


LOCAL_RECOVERY_POLICY_URL = "http://localhost:8080"  # run_once.py의 RECOVERY_POLICY_URL과 동일 전제(port-forward)
CHAOS_GROUP = "chaos-mesh.org"
CHAOS_VERSION = "v1alpha1"
CHAOS_PLURALS = ("podchaos", "networkchaos", "stresschaos")  # pod_kill_adapter/network_degrade_adapter/memory_pressure_adapter가 각각 쓰는 CR 종류
# load_ramp_adapter.py의 ramp-inj-*/ramp-probe-* pod은 label이 없다(실측
# 확인, §107 조사) - 이름 접두사로만 걸러낼 수 있다.
RESIDUAL_POD_NAME_PREFIXES = ("ramp-inj-", "ramp-probe-")
# §107(2026-09-23) - chaos CR 삭제(finalizer 처리)·Alertmanager 알림 해소는
# 즉시 반영되지 않을 수 있다(자연스러운 전파 지연) - §102의 wait_until_
# rolled_back() 교훈과 동일하게, 무기한 대기가 아니라 근거 있는 유한 시간
# 동안 재확인한다. memory_pressure_adapter.CLEANUP_VERIFY_TIMEOUT_SEC(30초)와
# 같은 값을 그대로 재사용(같은 종류의 K8s 컨트롤러 전파 지연이라 같은 여유가
# 근거 있음).
SAFETY_CHECK_POLL_TIMEOUT_SEC = 30.0
SAFETY_CHECK_POLL_INTERVAL_SEC = 3.0


def _node_conditions_healthy(conditions: dict) -> bool:
    return (conditions.get("Ready") == "True" and conditions.get("MemoryPressure") == "False"
            and conditions.get("DiskPressure") == "False" and conditions.get("PIDPressure") == "False")


def _check_all_nodes_healthy() -> dict:
    """§108(2026-09-24) - §107의 _check_active_pod_and_node()는 active pod이
    떠 있는 노드 하나만 확인했다(이 클러스터는 sj-control/sj-worker 2개
    노드 - 실측 확인, `kubectl get nodes`). control-plane 노드가 나빠지면
    Rollout controller·Chaos Mesh 등 클러스터 전체 조율이 흔들릴 수 있는데
    그건 active pod 쪽에서는 절대 안 보인다 - 그래서 클러스터의 **모든**
    노드를 확인한다(workload가 어디서 뜨는지와 무관)."""
    from kubernetes import client
    import active_pod_resolver
    active_pod_resolver.load_kube_config()
    nodes = client.CoreV1Api().list_node().items
    unhealthy = {}
    for node in nodes:
        conditions = {c.type: c.status for c in (node.status.conditions or [])}
        if not _node_conditions_healthy(conditions):
            unhealthy[node.metadata.name] = conditions
    if unhealthy:
        return {"ok": False, "reason": f"비정상 Node 발견: {unhealthy}"}
    return {"ok": True, "reason": None}


# §108(2026-09-24) - preflight가 기록한 active pod의 UID/restart_count를
# postflight가 비교할 수 있게 하는 프로세스 내부 캐시(run_id로 키). Hooks.
# postflight_cleanup_check의 기존 시그니처(Callable[[dict], dict], trial만
# 받음)를 바꾸지 않기 위한 최소한의 선택 - run_sequence()가 preflight/
# postflight를 같은 프로세스 안에서 순차 호출하는 실제 사용 패턴에서만
# 유효하면 충분하다(오케스트레이터 프로세스가 그 사이에 재시작되면 baseline
# 없이 fallback - 아래 함수 참고, 조용히 통과시키지 않고 절대값만 재확인).
_PREFLIGHT_POD_BASELINE: dict = {}


def _check_active_pod_restart_baseline(trial: dict, phase: str) -> dict:
    """§108 - "pod_kill이 의도적으로 삭제한 대상 UID"와 "예상 밖 재시작"을
    구분한다. pod_kill은 대상 pod을 완전히 delete하고 컨트롤러가 새 UID의
    pod을 만든다(재시작이 아니라 신원 교체) - 그래서 UID가 바뀐 것 자체는
    실패로 보지 않는다. 반면 같은 UID인데 restart_count가 늘었으면(예:
    load_ramp/network_degrade처럼 pod을 안 죽이는 시나리오에서) 그건 pod_kill
    메커니즘과 무관한 진짜 예상 밖 재시작이다(memory_pressure_adapter.py의
    기존 baseline-restart_count-delta 관례와 같은 원리).

    phase="pre": 현재 값을 baseline으로 기록만 하고 통과(비교 대상이 아직
    없음). phase="post": 기록된 baseline과 비교 - UID 교체는 새 pod
    자체의 restart_count>0/OOMKilled만 보고, UID 불변이면 restart_count
    증가 여부를 본다. baseline이 없으면(preflight 없이 단독 호출 등)
    delta 비교 없이 현재 절대값(OOMKilled/restart_count>0)만 fail-closed로
    확인한다 - 조용히 통과시키지 않는다."""
    import active_pod_resolver
    import memory_pressure_adapter as mpa
    pods = active_pod_resolver.get_active_pods()
    if len(pods) != 1:
        return {"ok": False, "reason": f"active pod 개수 이상(기대 1, 실제 {len(pods)}): {pods}"}
    details = mpa.get_pod_details(pods[0]["name"])
    if details is None:
        return {"ok": False, "reason": f"active pod {pods[0]['name']} 상세 조회 실패(404)"}
    if details["oom_killed"]:
        return {"ok": False, "reason": f"active pod {details['name']}이 OOMKilled 상태"}

    run_id = trial.get("run_id")
    current_restart = details["restart_count"] or 0
    if phase == "pre":
        if run_id:
            _PREFLIGHT_POD_BASELINE[run_id] = {"pod_uid": details["uid"], "restart_count": current_restart}
        return {"ok": True, "reason": None}

    baseline = _PREFLIGHT_POD_BASELINE.get(run_id)
    if baseline is None:
        if current_restart > 0:
            return {"ok": False, "reason": f"active pod {details['name']}의 restart_count={current_restart} "
                                            f"(preflight baseline 없어 절대값만 확인)"}
        return {"ok": True, "reason": None}

    if details["uid"] != baseline["pod_uid"]:
        # 의도된 신원 교체(pod_kill 등) - 새 pod 자체가 이미 재시작 이력을
        # 가지고 있으면(정상적인 첫 기동이라면 0이어야 함) 그것만 이상 신호.
        if current_restart > 0:
            return {"ok": False, "reason": f"교체된 새 pod {details['name']}의 restart_count={current_restart} "
                                            f"(새로 생성된 pod은 0이어야 함 - 예상 밖)"}
        return {"ok": True, "reason": None}

    if current_restart > (baseline["restart_count"] or 0):
        return {"ok": False, "reason": f"동일 pod({details['name']}, UID 불변)에서 예상 밖 restart 증가: "
                                        f"baseline={baseline['restart_count']} -> 현재={current_restart}"}
    return {"ok": True, "reason": None}


def _check_active_endpoint(namespace: str = "vllm-serving") -> dict:
    """active pod 수와 vllm-active EndpointSlice의 주소 수가 정확히
    1:1로 맞는지 확인한다(_verify_active_selector_and_endpoints()는
    promote 직후의 특정 hash를 검증하는 용도라 여기선 안 씀 - 이건
    "지금 이 순간 정상인가"만 본다)."""
    import active_pod_resolver
    pods = active_pod_resolver.get_active_pods()
    ips = _endpointslice_addresses(namespace, "vllm-active")
    if len(pods) != 1 or len(ips) != 1:
        return {"ok": False, "reason": f"active endpoint 이상(active pod={len(pods)}개, endpoint IP={len(ips)}개)"}
    return {"ok": True, "reason": None}


def _check_rollout_healthy_single_revision(name: str = "vllm-serving", namespace: str = "vllm-serving") -> dict:
    """§107/§108의 절대조건("phase==Healthy만 통과")을 §111에서 다듬었다 -
    실사고 계기(load_ramp-fixed_threshold-01-mainexp-v2, docs/design/
    phase8-blue-green-preflight-incident.md §110): detector가 SLO 위반을
    감지 못해 promote 없이 끝난 trial은 `cleanup_unpromoted_preview()`가
    미승격 preview를 abort하는데, Argo Rollouts는 abort된 Rollout의
    `status.phase`를 다음 업데이트 시도 전까지 `"Degraded"`(reason
    `RolloutAborted`)로 남겨둔다 - 이건 Argo Rollouts 자체의 정상 동작이지
    클러스터 이상이 아니다(§110 - `kubectl get events` 실측으로 확인, 실제
    서빙 pod은 전혀 영향 없었음). 하지만 이 예외를 함부로 다 허용하면 안
    되므로, **원인이 정확히 RolloutAborted이고 미승격 preview의 복원이
    실제로 완료됐다는 근거가 전부 있을 때만** 예외로 허용하고, 그 결과를
    `"healthy"`와 구분되는 `"aborted_preview_rolled_back"`로 분류해
    반환한다(호출부가 `Healthy`로 오인해 기록하지 않도록 - `run_sequence()`
    가 `checks`를 `preflight_checks`/`postflight_checks`에 그대로 저장).
    phase 검사 자체를 없애지 않았다 - Degraded의 다른 원인(reason이
    RolloutAborted가 아님)은 여전히 그대로 실패 처리한다.

    검사 내용:
      1) `phase == "Healthy"` -> `classification="healthy"`, 정상 경로.
      2) `phase == "Degraded"`이고 `conditions`에 `type=Progressing,
         status=False, reason=RolloutAborted`가 있으면 ->
         `classification="aborted_preview_rolled_back"` 후보 - 아래 근거를
         전부 확인해야 통과: `pauseConditions`가 비어있음(잔여 pause
         없음), `activeSelector`가 있고 active Service EndpointSlice에
         주소가 있음(안정 revision이 실제로 서빙 중), active pod 이름이
         `activeSelector` 해시를 포함(선택 재확인). 그 외 Degraded
         원인(예: 실제 실패로 인한 Degraded)은 그대로 실패.
      3) 단일 revision 확인(1·2 공통) - `app={name}` 라벨의 ReplicaSet 중
         active가 아닌 것들의 desired **AND current AND ready** 전부 0이어야
         함(§108까지는 desired만 봄 - preview pod이 Terminating 중이라
         desired는 이미 0인데 current/ready가 아직 안 줄어든 경우까지
         잡기 위해 강화, §102 wait_until_rolled_back()과 같은 이유).
      4) `classification=="healthy"`일 때만 `activeSelector==
         previewSelector`(아무 preview도 진행 중이 아님)도 확인한다 -
         `aborted_preview_rolled_back` 상태에서는 `previewSelector`가
         이미 지워진 preview의 옛 해시를 계속 들고 있는 게 정상이라
         (실측 확인) 이 비교 자체가 무의미하다."""
    import active_pod_resolver
    import blue_green_prep as bgp
    obj = bgp._custom_api().get_namespaced_custom_object(
        bgp.ROLLOUTS_GROUP, bgp.ROLLOUTS_VERSION, namespace, bgp.ROLLOUTS_PLURAL, name)
    status = obj.get("status", {})
    phase = status.get("phase")
    bg = status.get("blueGreen") or {}
    active_selector, preview_selector = bg.get("activeSelector"), bg.get("previewSelector")
    conditions = status.get("conditions") or []

    if phase == "Healthy":
        classification = "healthy"
    elif phase == "Degraded" and any(
            c.get("type") == "Progressing" and c.get("status") == "False" and c.get("reason") == "RolloutAborted"
            for c in conditions):
        classification = "aborted_preview_rolled_back"
    else:
        return {"ok": False, "reason": f"Rollout phase={phase!r}(Healthy 아님, RolloutAborted 예외 조건도 아님)",
                "classification": "unhealthy"}

    if classification == "aborted_preview_rolled_back":
        if status.get("pauseConditions"):
            return {"ok": False, "reason": f"Degraded+RolloutAborted인데 pauseConditions가 남아있음(복원 미완료): "
                                            f"{status.get('pauseConditions')}", "classification": "unhealthy"}
        if not active_selector:
            return {"ok": False, "reason": "Degraded+RolloutAborted인데 activeSelector를 확인할 수 없음",
                    "classification": "unhealthy"}
        endpoint_ips = _endpointslice_addresses(namespace, "vllm-active")
        if not endpoint_ips:
            return {"ok": False, "reason": "Degraded+RolloutAborted인데 active Service endpoint가 비어있음"
                                            "(안정 revision 서빙 확인 불가)", "classification": "unhealthy"}
        active_pods = active_pod_resolver.get_active_pods()
        if len(active_pods) != 1 or active_selector not in active_pods[0]["name"]:
            return {"ok": False, "reason": f"Degraded+RolloutAborted인데 active pod이 activeSelector"
                                            f"({active_selector})와 일치하지 않음: {active_pods}",
                    "classification": "unhealthy"}

    apps = bgp._apps_api()
    rs_list = apps.list_namespaced_replica_set(namespace, label_selector=f"app={name}").items
    stray = []
    for rs in rs_list:
        if rs.metadata.labels.get("rollouts-pod-template-hash") == active_selector:
            continue
        desired = rs.spec.replicas or 0
        current = rs.status.replicas or 0
        ready = rs.status.ready_replicas or 0
        if desired > 0 or current > 0 or ready > 0:
            stray.append({"name": rs.metadata.name, "desired": desired, "current": current, "ready": ready})
    if stray:
        return {"ok": False, "reason": f"active 외 다른 revision에 잔존 replica 있음(단일 revision 아님): {stray}",
                "classification": "unhealthy"}

    if classification == "healthy" and active_selector != preview_selector:
        return {"ok": False, "reason": f"preview 잔여 의심 - activeSelector({active_selector}) "
                                        f"!= previewSelector({preview_selector})", "classification": "unhealthy"}

    return {"ok": True, "reason": None, "classification": classification}


def _check_experiment_context_clear(url: str = LOCAL_RECOVERY_POLICY_URL) -> dict:
    """§102/§103 사고(orphaned experiment-run context)의 재발을 orchestrator
    층에서도 독립적으로 확인한다 - run_once.py 자신도 trial 시작 전 이
    엔드포인트를 확인하지만(계약서 §6), 그건 "정상 종료한" 하니스에만
    해당한다. subprocess가 크래시로 중간에 죽으면 run_once.py의 자체
    정리가 아예 실행되지 못하므로, 그 경우를 잡아내는 건 이 orchestrator
    층의 책임이다."""
    import requests
    try:
        resp = requests.get(f"{url}/admin/experiment-run", timeout=10)
        resp.raise_for_status()
    except Exception as e:
        return {"ok": False, "reason": f"recovery-policy 연결 실패(포트포워드 확인 필요): {e}"}
    current = resp.json().get("current")
    if current is not None:
        return {"ok": False, "reason": f"이전 trial의 experiment-run context가 정리되지 않음: {current}"}
    return {"ok": True, "reason": None}


def _check_quiescent(url: str = LOCAL_RECOVERY_POLICY_URL) -> dict:
    """recovery-policy 자신의 /admin/quiescent(계약서 §6)를 orchestrator
    층에서도 재확인 - 이유는 _check_experiment_context_clear()와 동일
    (크래시한 subprocess는 자체 확인을 못 함)."""
    import requests
    try:
        resp = requests.get(f"{url}/admin/quiescent", timeout=10)
        resp.raise_for_status()
    except Exception as e:
        return {"ok": False, "reason": f"recovery-policy quiescent 확인 연결 실패: {e}"}
    data = resp.json()
    if not data.get("quiescent"):
        return {"ok": False, "reason": f"quiescent 아님(active_count={data.get('active_count')})"}
    return {"ok": True, "reason": None}


def _list_leftover_chaos_crs(namespace: str = "vllm-serving") -> dict:
    from kubernetes import client
    import active_pod_resolver
    active_pod_resolver.load_kube_config()
    api = client.CustomObjectsApi()
    leftover = {}
    for plural in CHAOS_PLURALS:
        items = api.list_namespaced_custom_object(CHAOS_GROUP, CHAOS_VERSION, namespace, plural).get("items", [])
        if items:
            leftover[plural] = [i["metadata"]["name"] for i in items]
    return leftover


def _check_no_leftover_chaos_crs(namespace: str = "vllm-serving") -> dict:
    """3개 시나리오(pod_kill/network_degrade/memory_pressure)가 쓰는
    CHAOS_PLURALS 전체에 남은 CR이 있는지 확인한다 - 기존엔 각 어댑터가
    "자기가 만든 CR 하나"의 존재 여부만 확인했을 뿐, 이렇게 전체를
    나열해 잔여를 잡는 헬퍼는 전혀 없었다(§107 조사에서 확인된 gap)."""
    leftover = _list_leftover_chaos_crs(namespace)
    if leftover:
        return {"ok": False, "reason": f"잔여 Chaos CR 발견: {leftover}"}
    return {"ok": True, "reason": None}


def _list_leftover_experiment_pods(namespace: str = "vllm-serving") -> list:
    from kubernetes import client
    import active_pod_resolver
    active_pod_resolver.load_kube_config()
    core = client.CoreV1Api()
    pods = core.list_namespaced_pod(namespace).items
    return [p.metadata.name for p in pods if p.metadata.name.startswith(RESIDUAL_POD_NAME_PREFIXES)]


def _check_no_leftover_experiment_pods(namespace: str = "vllm-serving") -> dict:
    leftover = _list_leftover_experiment_pods(namespace)
    if leftover:
        return {"ok": False, "reason": f"잔여 실험 pod 발견: {leftover}"}
    return {"ok": True, "reason": None}


def _poll_until_ok(check_fn: Callable[[], dict], timeout_sec: float = SAFETY_CHECK_POLL_TIMEOUT_SEC,
                    poll_interval_sec: float = SAFETY_CHECK_POLL_INTERVAL_SEC) -> dict:
    """일부 확인은 즉시 확정되지 않는 자연스러운 전파 지연이 있다(chaos
    CR의 finalizer 삭제 처리) - §102의 wait_until_rolled_back() 교훈(무기한
    연장이 아니라 근거 있는 유한 대기+재확인)을 그대로 적용한다. timeout
    안에 한 번도 ok가 안 되면 마지막 결과를 그대로 반환한다(무한정
    기다리지 않고 fail-closed)."""
    deadline = time.monotonic() + timeout_sec
    result = check_fn()
    while not result["ok"] and time.monotonic() < deadline:
        time.sleep(poll_interval_sec)
        result = check_fn()
    return result


# §107/§108(2026-09-23~24, phase8-blue-green-preflight-incident.md §106/§107
# 조사 계기) - §98/§100이 약속한 8개 검사(Node·Rollout·active endpoint·
# restart/OOM·context·Chaos CR·실험 pod·detector 잔여) 중 7개를 여기서
# 실제로 수행한다(restart/OOM은 _check_active_pod_restart_baseline에
# 통합). **detector 잔여는 이 계층에서 직접 확인할 방법이 없다**(§107
# 조사 결론 - Detector.is_alive()는 그 Popen을 쥔 프로세스 안에서만
# 유효하고, run_all_scenarios.py는 각 trial을 별도 subprocess로 띄우므로
# 그 grandchild 프로세스를 외부에서 스캔할 인프라가 이 코드베이스에 전혀
# 없음, psutil 등 새 의존성도 없음 - 이 한계를 과장하지 않고 문서에도
# 그대로 남긴다, §108.3). 자동 PASS 항목으로 세지 않는다 - 이 검사
# 스위트에 아예 없다.
def _default_safety_check_funcs(trial: dict, phase: str) -> tuple:
    """§108 - restart-baseline 검사만 trial/phase(preflight="pre"/
    postflight="post")를 필요로 하므로, 나머지 무인자 함수와 섞어 하나의
    (이름, 무인자 콜러블) 목록으로 만든다. 함수 목록 자체를 주입 가능하게
    두는 건(real_safety_checks의 check_funcs 인자) 테스트에서 실 클러스터
    없이 fake 목록으로 완전히 대체하기 위함(memory_pressure_adapter.py의
    기존 *_fn 주입 관례와 동일한 목적)."""
    return (
        ("all_nodes_healthy", _check_all_nodes_healthy),
        ("active_endpoint", _check_active_endpoint),
        ("rollout_healthy_single_revision", _check_rollout_healthy_single_revision),
        ("active_pod_restart_baseline", lambda: _check_active_pod_restart_baseline(trial, phase)),
        ("experiment_context_clear", _check_experiment_context_clear),
        ("quiescent", lambda: _poll_until_ok(_check_quiescent)),
        ("no_leftover_chaos_crs", lambda: _poll_until_ok(_check_no_leftover_chaos_crs)),
        ("no_leftover_experiment_pods", _check_no_leftover_experiment_pods),
    )


def real_safety_checks(trial: dict = None, phase: str = "pre", check_funcs=None) -> dict:
    """preflight/postflight 공용 검사 스위트 - 첫 실패 항목에서 멈추고
    그 이름·사유를 반환한다(run_sequence()가 이 reason을 그대로
    SequenceAborted 메시지에 싣는다). check_funcs를 명시하면(테스트 전용)
    trial/phase는 무시되고 그 목록만 그대로 돈다."""
    checks = {}
    funcs = check_funcs if check_funcs is not None else _default_safety_check_funcs(trial or {}, phase)
    for name, fn in funcs:
        result = fn()
        checks[name] = result
        if not result["ok"]:
            return {"ok": False, "reason": f"[{name}] {result['reason']}", "checks": checks}
    return {"ok": True, "reason": None, "checks": checks}


def real_preflight(trial: dict, state: dict) -> dict:
    return real_safety_checks(trial, "pre")


def real_postflight_cleanup_check(trial: dict) -> dict:
    return real_safety_checks(trial, "post")


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
    parser.add_argument("--link-replacement", nargs=2, default=None, metavar=("ORIGINAL_RUN_ID", "NEW_RUN_ID"),
                         help="§103 - 기술적 invalid(status=failed) 슬롯 ORIGINAL_RUN_ID에, 사용자가 직접 고른 "
                              "고유한 NEW_RUN_ID를 대체로 연결만 하고 종료한다(trial 실행은 이 호출에서 하지 "
                              "않음 - 이후 --resume으로 별도 실행). --link-reason과 함께 줘야 하고, 기존 "
                              "state 파일이 있어야 한다(새 state 생성 안 함). 원본은 절대 수정하지 않는다.")
    parser.add_argument("--link-reason", default=None,
                         help="--link-replacement와 함께 필수 - 왜 이 원본이 기술적 invalid이고 왜 이 대체가 "
                              "필요한지 사람이 읽을 수 있는 사유(state/manifest에 그대로 보존됨).")
    parser.add_argument("--adjudicate-cleanup", nargs=2, default=None, metavar=("RUN_ID", "VERDICT"),
                         help="§111 - status=completed인데 cleanup_status=failed인 RUN_ID에 대해 사람이 현재 "
                              "클러스터 상태·당시 이벤트 근거를 직접 대조해 내린 판정을 기록만 하고 종료한다"
                              "(trial 실행 없음 - 그 trial 자체는 재실행하지 않음). VERDICT는 "
                              "resolved_false_positive|confirmed_problem만 허용. --adjudicate-reason과 함께 "
                              "줘야 함. 원본 cleanup_status/cleanup_reason은 절대 안 건드림 - "
                              "verdict=resolved_false_positive만 run_sequence()의 재개 skip을 허용시킨다.")
    parser.add_argument("--adjudicate-reason", default=None,
                         help="--adjudicate-cleanup과 함께 필수 - 판정 근거(사람이 읽을 수 있는 사유, state에 "
                              "그대로 보존됨).")
    parser.add_argument("--adjudicate-evidence-file", default=None,
                         help="--adjudicate-cleanup과 함께 선택 - 현재 클러스터 상태/K8s 이벤트 등 근거를 담은 "
                              "JSON 파일 경로(state에 그대로 보존됨).")
    args = parser.parse_args()

    if args.link_replacement is not None and args.link_reason is None:
        parser.error("--link-replacement는 --link-reason과 함께 줘야 함")
    if args.adjudicate_cleanup is not None and args.adjudicate_reason is None:
        parser.error("--adjudicate-cleanup은 --adjudicate-reason과 함께 줘야 함")

    state_path = Path(args.state_file)
    existing = load_state(state_path)

    if args.link_replacement is not None:
        if existing is None:
            print(f"--link-replacement는 기존 state 파일이 있어야 함: {state_path}", file=sys.stderr)
            sys.exit(1)
        original_run_id, new_run_id = args.link_replacement
        try:
            link_technical_invalid_replacement(existing, original_run_id, new_run_id, args.link_reason)
            verify_and_backfill_original_hash(existing, original_run_id)
        except ValueError as e:
            print(f"LINK REJECTED: {e}", file=sys.stderr)
            sys.exit(1)
        save_state_atomic(state_path, existing)
        print(f"연결 완료: {original_run_id} -> {new_run_id} (state 파일: {state_path}). "
              f"trial은 아직 실행되지 않음 - --resume으로 별도 실행할 것.")
        return

    if args.adjudicate_cleanup is not None:
        if existing is None:
            print(f"--adjudicate-cleanup은 기존 state 파일이 있어야 함: {state_path}", file=sys.stderr)
            sys.exit(1)
        run_id, verdict = args.adjudicate_cleanup
        evidence = None
        if args.adjudicate_evidence_file:
            evidence = json.loads(Path(args.adjudicate_evidence_file).read_text(encoding="utf-8"))
        try:
            adjudicate_cleanup_failure(existing, run_id, verdict, args.adjudicate_reason, evidence)
        except ValueError as e:
            print(f"ADJUDICATION REJECTED: {e}", file=sys.stderr)
            sys.exit(1)
        save_state_atomic(state_path, existing)
        print(f"판정 기록 완료: {run_id} -> {verdict} (state 파일: {state_path}). "
              f"trial은 재실행되지 않음 - 원본 cleanup_status/cleanup_reason은 그대로 보존됨.")
        return

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

    # §105 - 재개할 때마다 연결된 모든 대체의 원본 hash를 재검증(불일치 시
    # fail-closed) - 링크 생성 시점 1회가 아니라 매번, "파일이 그 사이
    # 손상·변조되지 않았는가"를 다시 확인하기 위함.
    for original_run_id in list(state.get("replacements", {}).keys()):
        try:
            verify_and_backfill_original_hash(state, original_run_id)
        except ValueError as e:
            print(f"REPLACEMENT ORIGINAL HASH VERIFICATION FAILED: {e}", file=sys.stderr)
            sys.exit(1)

    trials = build_matrix(plan_id)
    trials_to_run = [t for t in trials if t.scenario == args.scenario] if args.scenario else trials
    trials_to_run = apply_replacements(trials_to_run, state.get("replacements", {}))

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
        sync_replacement_results(state)
        save_state_atomic(state_path, state)
        print(f"SEQUENCE ABORTED: {e}", file=sys.stderr)
        sys.exit(1)

    sync_replacement_results(state)
    save_state_atomic(state_path, state)
    print(f"완료. state 파일: {state_path}")


if __name__ == "__main__":
    main()
