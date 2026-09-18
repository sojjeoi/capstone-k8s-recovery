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
from pathlib import Path
from typing import Callable, Optional

from blue_green_prep import prepare_preview as _real_prepare_preview
from run_once import Detector, Injector, TrialInvalid

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

STOP_TIMEOUT_SEC = 10.0  # terminate() 이후 정상 종료 대기 - 넘기면 kill()
# score_server.py(그리고 이를 import하는 fixed_threshold.py)는 원래
# in-cluster DNS를 기본값으로 쓴다 - 로컬 서브프로세스로 돌릴 땐 이
# 환경변수로 덮어써야 recovery-policy에 실제로 신호가 도달한다(둘 다
# kubectl port-forward -n vllm-serving svc/recovery-policy 8080:8080
# 전제 - run_once.py의 RECOVERY_POLICY_URL과 동일 전제, 계약서에 새로
# 추가하는 조건이 아니라 기존 로컬 실행 전제를 그대로 따름).
LOCAL_RECOVERY_POLICY_SIGNAL_URL = "http://localhost:8080/signal"


def _build_detector_command(arm: str, run_id: str) -> Optional[list]:
    """순수 함수 - 실제 프로세스를 안 띄우고 커맨드만 조립한다(오프라인
    테스트용, run_id 전파를 코드 실행 없이 검증 가능). arm이 native거나
    매핑에 없으면 None(detector 없음)."""
    spec = _DETECTOR_SCRIPTS.get(arm)
    if spec is None:
        return None
    return [sys.executable, str(ANOMALY_DETECTION_DIR / spec["script"]), "--run-id", run_id]


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


def make_detector_for_arm(arm: str, run_id: str) -> Optional[Detector]:
    """native면 None - run_once()가 detector 관련 로직을 아예 안 탄다.
    fixed_threshold/proposed면 anomaly-detection/{script} --run-id
    {run_id}를 로컬 서브프로세스로 띄우는 Detector를 반환한다."""
    spec = _DETECTOR_SCRIPTS.get(arm)
    if spec is None:
        return None
    cmd = _build_detector_command(arm, run_id)
    return _subprocess_detector(cmd, spec["name"], cwd=str(ANOMALY_DETECTION_DIR))


def wrap_injector_with_preview_prep(
    injector: Injector, arm: str,
    rollout_name: str = "vllm-serving", namespace: str = "vllm-serving",
    prepare_preview_fn: Callable[[str, str], bool] = _real_prepare_preview,
) -> Injector:
    """native는 원본 injector를 그대로 반환(계약서 §1 - standby 자체가
    없음). non-native면 injector.prepare()가 (시나리오별 기존 prepare()
    호출 앞에) prepare_preview()를 먼저 호출하도록 감싼다 - 실패하면(시간
    내 Ready 안 됨) TrialInvalid를 던진다. run_once()의 PREPARING 단계는
    이 예외를 그대로 invalid_run으로 처리하고 injector.inject()를 아예
    호출하지 않는다(기존 "probe 미준비면 주입 안 함"과 동일한 안전장치를
    재사용 - 새로 만들지 않음).

    prepare_preview_fn은 기본적으로 실제 blue_green_prep.prepare_preview
    (kubernetes 클라이언트로 실클러스터에 접근)를 쓰지만, 테스트에서
    가짜 함수를 주입할 수 있게 인자로 열어뒀다."""
    if arm not in _DETECTOR_SCRIPTS:
        return injector

    original_prepare = injector.prepare

    def prepare_with_preview():
        if not prepare_preview_fn(rollout_name, namespace):
            raise TrialInvalid(
                f"{rollout_name} preview가 시간 내 Ready 안 됨(arm={arm}) - 주입 시도 안 함"
            )
        original_prepare()

    injector.prepare = prepare_with_preview
    return injector
