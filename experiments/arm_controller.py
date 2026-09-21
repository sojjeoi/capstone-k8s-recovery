#!/usr/bin/env python3
"""arm(native/fixed_threshold/proposed)별 detector 생명주기 + non-native
arm의 preview 준비를 관리하는 공통 오케스트레이터(2026-09-18 추가).

지금까지 run_load_ramp_trial.py는 --arm 이름만 결과에 태깅할 뿐,
fixed_threshold.py/score_server.py의 실제 실행·종료나 preview 준비를
전혀 담당하지 않았다 - 그대로 non-native arm을 돌리면 detector가 실제로
동작하지 않은 채 "fixed_threshold"/"proposed"로 잘못 라벨링된 결과가
생길 수 있었다. 이 모듈은 그 갭을 메운다.

계약서(docs/design/experiment-contract.md) §1을 그대로 따른다 - 세 arm
중 native만 detector·preview 둘 다 없고, fixed_threshold/proposed는
(예측 모델만 다르고) 둘 다 detector 1개 + standby(preview)가 필요하다.
새로 추정하지 않는다 - 값은 전부 계약서·기존 코드(blue_green_prep.py)
그대로 재사용한다.
"""
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit, urlunsplit

import requests

from blue_green_prep import PREVIEW_PREP_TIMEOUT_SEC
from blue_green_prep import cleanup_unpromoted_preview as _real_cleanup_unpromoted_preview
from blue_green_prep import prepare_preview_with_rollback as _real_prepare_preview_with_rollback
from run_once import Detector, HarnessCorrupted, Injector, TrialInvalid

ANOMALY_DETECTION_DIR = Path(__file__).parent.parent / "anomaly-detection"

# 계약서 §1 - fixed_threshold/proposed는 예측 모델만 다르고 나머지(standby/
# promotion/공통 Alertmanager 반응형 fallback)는 동일하다. "name"은 각
# 스크립트가 post_to_recovery_policy()로 실제로 보내는 detector= 태그와
# 정확히 일치해야 한다(fixed_threshold.py: detector="fixed_threshold" 명시
# 전달, score_server.py: 인자 기본값 "isolation_forest") - 그래야 나중에
# recovery-policy가 실제로 수신한 신호와 대조해 이 trial이 의도한 detector로
# 정말 실행됐는지 사후 감사할 수 있다.
_DETECTOR_SCRIPTS = {
    "fixed_threshold": {"script": "fixed_threshold.py", "name": "fixed_threshold"},
    "proposed": {"script": "score_server.py", "name": "isolation_forest"},
}

# 계약서 §6 Phase 8 동결값(2026-09-20 정정) - fixed_threshold.py는 더 이상 CPU
# limit을 스스로 추정하지 않고(fail-closed) 매 실행마다 --cpu-limit-cores를
# 명시적으로 요구한다. 여기가 그 값의 유일한 소스 - gitops/apps/vllm-serving/
# rollout.yaml resources.limits.cpu(lab-cpu3-warm-v1, docs/design/phase8-blue-
# green-preflight-incident.md §11·§16)와 반드시 같아야 하고, 클러스터 자원이
# 다시 재구성되면 여기와 계약서 §6을 함께 갱신해야 한다(그 전까지 이 정정
# 이전에 하드코딩됐던 4.0/3.6코어와 같은 낡은 값 사고가 재발하지 않게 하는
# 유일한 안전장치는 이 상수를 실제 rollout.yaml과 맞춰 유지하는 것뿐이다).
FIXED_THRESHOLD_CPU_LIMIT_CORES = 3.0

# §86(2026-09-21) - `proposed` arm만 v3.2b 동결 artifact를 명시적으로 가리킨다
# (score_server.py는 --artifacts-dir/--model-version 없이는 fail-closed로
# 시작을 거부함, 위 fixed_threshold의 --cpu-limit-cores와 동일 원칙).
# native/fixed_threshold 배선은 이 상수와 무관 - 손대지 않는다.
PROPOSED_ARTIFACTS_DIR = ANOMALY_DETECTION_DIR / "v3" / "model_v32b" / "artifacts"
PROPOSED_MODEL_VERSION = "v3.2b"

STOP_TIMEOUT_SEC = 10.0  # terminate() 이후 정상 종료 대기 - 넘기면 kill()
# score_server.py(그리고 이를 import하는 fixed_threshold.py)는 원래
# in-cluster DNS를 기본값으로 쓴다 - 로컬 서브프로세스로 돌릴 땐 이
# 환경변수로 덮어써야 recovery-policy에 실제로 신호가 도달한다(둘 다
# kubectl port-forward -n vllm-serving svc/recovery-policy 8080:8080
# 전제 - run_once.py의 RECOVERY_POLICY_URL과 동일 전제, 계약서에 새로
# 추가하는 조건이 아니라 기존 로컬 실행 전제를 그대로 따름).
LOCAL_RECOVERY_POLICY_SIGNAL_URL = "http://localhost:8080/signal"
REACHABILITY_CHECK_TIMEOUT_SEC = 5.0


def _resolved_signal_url() -> str:
    """detector 서브프로세스가 실제로 쓸 URL과 정확히 같은 값을 계산한다
    (2026-09-19 추가) - _subprocess_detector()의 env.setdefault()와 동일한
    우선순위(환경변수 RECOVERY_POLICY_SIGNAL_URL 우선, 없으면 로컬 기본값).
    reachability 검사가 실제로 쓰일 URL과 다른 URL을 확인하면 무의미하므로
    반드시 같은 계산식을 공유해야 한다."""
    return os.environ.get("RECOVERY_POLICY_SIGNAL_URL", LOCAL_RECOVERY_POLICY_SIGNAL_URL)


def _recovery_policy_reachable(signal_url: str) -> bool:
    """RECOVERY_POLICY_SIGNAL_URL이 실제로 도달 가능한지 확인한다(2026-09-19
    추가, fail-closed 사전 확인). /signal에 직접 요청을 보내면 진짜 신호로
    처리돼 부작용이 생기므로(idempotency 소모·감사기록 오염), 같은
    host:port의 /healthz(부작용 없는 엔드포인트, main.py에 이미 존재)로
    대신 확인한다."""
    parts = urlsplit(signal_url)
    health_url = urlunsplit((parts.scheme, parts.netloc, "/healthz", "", ""))
    try:
        r = requests.get(health_url, timeout=REACHABILITY_CHECK_TIMEOUT_SEC)
        return r.status_code == 200
    except requests.exceptions.RequestException:
        return False


LOCAL_PROMETHEUS_URL = "http://localhost:9090"  # features.py의 PROM_URL과 동일 전제(로컬 port-forward)
PROMETHEUS_FRESHNESS_MAX_AGE_SEC = 120.0  # score_server.py/fixed_threshold.py의 WINDOW_SEC(60초)보다 넉넉히 큰 상한
_PROMETHEUS_PROBE_QUERY = 'container_cpu_usage_seconds_total{namespace="vllm-serving",container="vllm"}'


def _prometheus_reachable_and_fresh(prom_url: str = LOCAL_PROMETHEUS_URL,
                                     max_age_sec: float = PROMETHEUS_FRESHNESS_MAX_AGE_SEC) -> bool:
    """Prometheus가 실제로 쿼리에 응답하고, vLLM 지표가 최근에 갱신되고
    있는지 확인한다(2026-09-19 추가, fail-closed 사전 확인). 단순 포트
    연결이나 /-/healthy만으로는 Prometheus 프로세스는 떠있지만 실제
    타겟 스크랩이 죽어있는 경우(오래된 데이터만 응답)를 못 잡는다 -
    detector(score_server.py/fixed_threshold.py)가 실제로 쓰는 것과 같은
    종류의 지표를 인스턴트 쿼리로 직접 조회해 표본이 있는지, 그 표본의
    timestamp가 max_age_sec 이내로 신선한지까지 확인한다."""
    try:
        r = requests.get(f"{prom_url}/api/v1/query", params={"query": _PROMETHEUS_PROBE_QUERY},
                          timeout=REACHABILITY_CHECK_TIMEOUT_SEC)
        if r.status_code != 200:
            return False
        body = r.json()
        if body.get("status") != "success":
            return False
        result = body.get("data", {}).get("result", [])
        if not result:
            return False
        sample_ts = float(result[0]["value"][0])
        return (time.time() - sample_ts) <= max_age_sec
    except (requests.exceptions.RequestException, KeyError, IndexError, ValueError, TypeError):
        return False


def _build_detector_command(arm: str, run_id: str, evidence_log_path: Optional[str] = None) -> Optional[list]:
    """순수 함수 - 실제 프로세스를 안 띄우고 커맨드만 조립한다(오프라인
    테스트용, run_id 전파를 코드 실행 없이 검증 가능). arm이 native거나
    매핑에 없으면 None(detector 없음). fixed_threshold는 2026-09-20부터
    --cpu-limit-cores(FIXED_THRESHOLD_CPU_LIMIT_CORES, 위 §6 동결값)를 반드시
    같이 받는다 - fixed_threshold.py가 이 인자 없이는 즉시 fail-closed로
    종료하므로, 여기서 안 붙이면 detector가 아예 시작을 못 한다.

    evidence_log_path(§90 E2E pilot, 2026-09-21 추가) - 지정되면 proposed
    arm(score_server.py)에만 --evidence-log로 전달한다(기본값 None이면
    아무 인자도 안 붙어 기존 호출부·본 실험 동작이 전혀 안 바뀜, 순수
    opt-in). score_server.py의 --evidence-log는 판정 로직에 영향 없는
    관찰 전용 append 파일이다(§88.6). fixed_threshold.py는 이 옵션 자체가
    없으므로 arm에 상관없이 절대 붙이지 않는다."""
    spec = _DETECTOR_SCRIPTS.get(arm)
    if spec is None:
        return None
    cmd = [sys.executable, str(ANOMALY_DETECTION_DIR / spec["script"]), "--run-id", run_id]
    if arm == "fixed_threshold":
        cmd += ["--cpu-limit-cores", str(FIXED_THRESHOLD_CPU_LIMIT_CORES)]
    elif arm == "proposed":
        cmd += ["--artifacts-dir", str(PROPOSED_ARTIFACTS_DIR), "--model-version", PROPOSED_MODEL_VERSION]
        if evidence_log_path is not None:
            cmd += ["--evidence-log", evidence_log_path]
    return cmd


def _subprocess_detector(cmd: list, name: str, cwd: Optional[str] = None) -> Detector:
    """서브프로세스 생명주기 관리 자체를 detector 스크립트 내용과 분리한
    작은 헬퍼(2026-09-18 추가, 테스트 용이성) - make_detector_for_arm()이
    실제 detector 스크립트로 이걸 쓰고, 테스트는 어떤 명령이든(예:
    python -c "...") 넣어서 start/is_alive/stop 자체의 정확성만 검증할 수
    있다(fixed_threshold.py/score_server.py는 Prometheus·모델 파일 등
    외부 의존성이 있어 오프라인 테스트 대상이 아님)."""
    state = {"proc": None}

    def start():
        env = dict(os.environ)
        env.setdefault("RECOVERY_POLICY_SIGNAL_URL", LOCAL_RECOVERY_POLICY_SIGNAL_URL)
        state["proc"] = subprocess.Popen(
            cmd, cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        )

    def is_alive():
        proc = state["proc"]
        return proc is not None and proc.poll() is None

    def stop():
        proc = state["proc"]
        if proc is None or proc.poll() is not None:
            return  # 시작 전이거나 이미 죽어있음 - idempotent
        proc.terminate()
        try:
            proc.wait(timeout=STOP_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=STOP_TIMEOUT_SEC)

    return Detector(start=start, is_alive=is_alive, stop=stop, name=name)


def make_detector_for_arm(
    arm: str, run_id: str,
    reachability_check_fn: Optional[Callable[[str], bool]] = None,
    prometheus_check_fn: Optional[Callable[[], bool]] = None,
    evidence_log_path: Optional[str] = None,
) -> Optional[Detector]:
    """native면 None - run_once()가 detector 관련 로직을 아예 안 탄다.
    fixed_threshold/proposed면 anomaly-detection/{script} --run-id
    {run_id}를 로컬 서브프로세스로 띄우는 Detector를 반환한다.

    반환된 Detector.start()는 실제로 서브프로세스를 띄우기 전에
    RECOVERY_POLICY_SIGNAL_URL과 Prometheus 둘 다 도달 가능한지부터
    확인한다(2026-09-19 추가, fail-closed 지시) - 둘 중 하나라도 접근
    불가면 TrialInvalid를 던져 detector 자체를 시작하지 않는다.
    detector(score_server.py/fixed_threshold.py)는 Prometheus 없이는
    첫 evaluate()에서 곧바로 크래시하므로(features.py, 예외 처리 없음)
    is_alive() 크래시 감지로도 결국 invalid_run이 되긴 하지만, 그건
    "시작한 뒤에 실패"고 이 사전 확인은 "애초에 시작(=chaos 주입 직전
    단계)하지 않음"이다 - run_once()에서 detector.start()가 chaos 주입
    "직전"에 호출되므로(§32), 여기서 막히면 주입도 자동으로 안 일어난다.
    reachability_check_fn/prometheus_check_fn은 테스트에서 가짜 함수를
    주입할 수 있게 열어둔 선택 인자(기본값은 각각 실제
    _recovery_policy_reachable/_prometheus_reachable_and_fresh)."""
    spec = _DETECTOR_SCRIPTS.get(arm)
    if spec is None:
        return None
    check_fn = reachability_check_fn or _recovery_policy_reachable
    prom_fn = prometheus_check_fn or _prometheus_reachable_and_fresh
    cmd = _build_detector_command(arm, run_id, evidence_log_path=evidence_log_path)
    base = _subprocess_detector(cmd, spec["name"], cwd=str(ANOMALY_DETECTION_DIR))

    def start_with_reachability_preflight():
        signal_url = _resolved_signal_url()
        if not check_fn(signal_url):
            raise TrialInvalid(
                f"RECOVERY_POLICY_SIGNAL_URL({signal_url}) 접근 불가 - "
                f"detector 시작 안 함(fail-closed, arm={arm})"
            )
        if not prom_fn():
            raise TrialInvalid(
                f"Prometheus 접근 불가 또는 지표가 오래됨 - "
                f"detector 시작 안 함(fail-closed, arm={arm})"
            )
        base.start()

    return Detector(start=start_with_reachability_preflight, is_alive=base.is_alive, stop=base.stop, name=base.name)


def wrap_injector_with_preview_prep(
    injector: Injector, arm: str,
    rollout_name: str = "vllm-serving", namespace: str = "vllm-serving",
    prepare_preview_fn: Callable[[str, str, float], dict] = _real_prepare_preview_with_rollback,
    preview_prep_timeout_sec: float = PREVIEW_PREP_TIMEOUT_SEC,
    cleanup_preview_fn: Callable[[Optional[dict], str, str], Optional[bool]] = _real_cleanup_unpromoted_preview,
) -> Injector:
    """native는 원본 injector를 그대로 반환(계약서 §1 - standby 자체가
    없음). non-native면 injector.prepare()가 (시나리오별 기존 prepare()
    호출 앞에) prepare_preview_with_rollback()을 먼저 호출하도록 감싼다.

    2026-09-19 추가(fixed_threshold pilot 01회 사고 계기 - preview가 180초
    timeout보다 늦게 Ready된 채 방치되고 Rollout이 Paused/Degraded로 남음):
    실패를 두 층으로 구분한다.
      - preview timeout이지만 자동 rollback 성공(클러스터 정상 복원) ->
        기존과 동일하게 TrialInvalid(이 trial만 무효, 배치는 계속).
      - activeSelector가 bump 직후 이미 예상 밖이거나(다른 프로세스 개입
        가능성 - 무엇이 "우리 preview"인지 특정 불가) 자동 rollback 자체가
        실패 -> HarnessCorrupted(클러스터가 다음 trial을 오염시킬 수 있는
        상태로 남았을 가능성 - run_once()가 critical_failures에 반영해
        배치를 멈춘다). 둘 다 "이 trial은 무효"라는 점은 같지만 후자는
        수동 확인 없이 다음 trial로 넘어가면 안 된다.
    두 경우 모두 원래 prepare 실패 사유(몇 초 만에 timeout됐는지)와 rollback
    결과(성공/실패, 대상 pod_hash)를 예외 메시지 하나에 함께 남긴다.

    preview 준비 진단 정보(t_preview_ready, 소요시간, rollback 결과)는
    injector.get_preview_prep_info()로 노출한다 - run_once()가 실패 시에도
    (finally에서) TrialResult에 반영할 수 있게 하기 위함이다.

    injector.cleanup()도 함께 감싼다(2026-09-19 추가 - fixed_threshold pilot
    재실행에서 실측 발견한 별도 gap: preview 준비는 성공했는데 detector가
    끝내 promote를 안 하고 trial이 끝나면, 위 timeout-rollback 경로를 안
    타서 아무도 이 preview를 정리하지 않았다 - Rollout이 2-revision으로
    방치됨). 원본 cleanup()을 먼저 실행하고(시나리오 자체 자원 정리 -
    실패해도 아래 preview 정리는 독립적으로 계속 시도), 그 다음
    cleanup_unpromoted_preview()로 "이번 trial이 준비했지만 promote 안 된
    preview"만 골라 abort + 복원 재확인한다. 이미 promote됐으면(activeSelector가
    우리 pod_hash로 바뀜) 손대지 않는다 - 그건 이제 진짜 active고, 옛 stable은
    Rollout 컨트롤러가 알아서 정리한다. 정리 자체가 실패하면 예외를 던져
    run_once()의 기존 injector.cleanup() 실패 처리(critical_failures ->
    배치 끝에서 HarnessCorrupted)를 그대로 재사용한다 - 새 심각도 체계를
    또 만들지 않는다.

    prepare_preview_fn/cleanup_preview_fn은 기본적으로 실제 blue_green_prep의
    함수(kubernetes 클라이언트로 실클러스터에 접근)를 쓰지만, 테스트에서
    가짜 함수를 주입할 수 있게 인자로 열어뒀다."""
    if arm not in _DETECTOR_SCRIPTS:
        return injector

    original_prepare = injector.prepare
    original_cleanup = injector.cleanup
    prep_state = {"last": None}

    def prepare_with_preview():
        prep = prepare_preview_fn(rollout_name, namespace, preview_prep_timeout_sec)
        prep_state["last"] = prep
        if prep["ready"]:
            original_prepare()
            return

        if prep["external_interference"]:
            raise HarnessCorrupted(
                f"{rollout_name}의 activeSelector가 preview 준비 도중 예상 밖으로 바뀜(arm={arm}) - "
                f"다른 프로세스가 개입했을 가능성이 있어 무엇이 '이번 호출의 preview'인지 특정 불가, "
                f"fail-closed로 아무 것도 건드리지 않음 - 수동 확인 필요"
            )

        base_msg = (
            f"{rollout_name} preview가 {preview_prep_timeout_sec}초 내 Ready 안 됨(arm={arm}, "
            f"실측 대기 {prep['prep_duration_sec']:.1f}초) - 주입 시도 안 함"
        )
        if not prep["rollback_attempted"]:
            raise TrialInvalid(base_msg)
        if prep["rollback_ok"]:
            raise TrialInvalid(f"{base_msg} | 자동 rollback 성공(aborted_pod_hash={prep['aborted_pod_hash']})")
        raise HarnessCorrupted(
            f"{base_msg} | 자동 rollback 실패(aborted_pod_hash={prep['aborted_pod_hash']}) - "
            f"preview/Rollout이 방치된 상태로 남았을 수 있음, 수동 확인 필요"
        )

    def cleanup_with_preview_check():
        try:
            original_cleanup()
        finally:
            ok = cleanup_preview_fn(prep_state["last"], rollout_name, namespace)
            if ok is False:
                info = prep_state["last"] or {}
                raise RuntimeError(
                    f"trial 종료 시 미promote preview 자동 정리 실패(arm={arm}, "
                    f"pod_hash={info.get('created_pod_hash')}) - activeSelector 복원 또는 "
                    f"preview scale-down 확인 안 됨, 수동 확인 필요"
                )

    injector.prepare = prepare_with_preview
    injector.cleanup = cleanup_with_preview_check
    injector.get_preview_prep_info = lambda: prep_state["last"]
    return injector
