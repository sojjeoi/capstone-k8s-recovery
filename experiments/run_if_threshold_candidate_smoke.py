#!/usr/bin/env python3
"""§183(2026-09-27) - 공식 IsolationForest 경로에 대한 최소 변경 후보(임계값
-0.0743 -> -0.04, 모델·특징·3연속규칙·CPU 압박 강도 전부 불변) 검증.

**후보 artifacts 분리**: anomaly-detection/v3/model_v32b/artifacts_candidate_
thr_neg0p04/ - model.pkl/scaler.pkl/feature-schema.json/requirements-lock.txt/
training-metadata.json/dataset-manifest.json/split-manifest.json은 공식
artifacts/와 바이트 동일(생성 시 직접 복사+SHA256 재확인 완료), threshold.json만
threshold 필드가 다르고 consecutive_threshold(3)·cooldown_sec(60)·eval_
interval_sec(15)는 공식과 동일 - score_server.py 자신의 runtime_checks가 이
셋의 불일치를 fail-closed로 막으므로 3연속 규칙이 실수로도 안 바뀐다. 공식
artifacts/·threshold.json은 이 파일이 전혀 안 건드린다.

**Phase 1(no_injection)**: preview 준비(injector.prepare()만, inject() 없음)
+ 1 RPS 요청 부하 상태에서, score_server.py(공식 코드 그대로)를 후보
artifacts로 직접 subprocess로 띄우되 RECOVERY_POLICY_SIGNAL_URL을 로컬
capture_sink.py(anomaly-detection/v3/model_v32b/capture_sink.py, §86 no-action
smoke가 이미 쓰는 것 그대로 재사용 - 새 코드 없음)로 덮어쓴다. capture_sink는
수신·저장만 하고 promotion·recovery-policy 전달 코드 자체가 없으므로, 이
단계는 구조적으로 실제 정책·승격에 연결될 수 없다. arm_controller.
make_detector_for_arm()은 안 쓴다(PROPOSED_ARTIFACTS_DIR이 공식 경로로
하드코딩돼 있어 커스텀 artifacts_dir을 못 받는다 - 그 파일을 안 건드리기
위해 이 파일이 score_server.py를 직접 띄운다).

관찰이 끝나면 evidence-log에 기록된 원시 score만으로 공식(-0.0743)·후보(-0.04)
두 임계값을 각각 오프라인 재적용(3연속·하드리셋, score_server.advance_streak과
동일 로직)해 발화 여부를 비교한다 - 라이브 중에는 후보 임계값만 실사용됐고
(sink로만 감), 공식 임계값 쪽은 이 사후 재적용으로만 확인한다.

**Phase 2(cpu_pressure)**: Phase 1이 통과했을 때만(호출자가 별도로 확인 후
--mode cpu_pressure로 재실행) 진행. §176/§182와 동일한 DEMO_STAGES로 실제
CPU 압박을 주입하되, score_server.py는 후보 artifacts로, RECOVERY_POLICY_
SIGNAL_URL은 실제 recovery-policy(http://localhost:8080/signal, 로컬
port-forward 전제)로 연결해 실제 판정·전환이 일어나는지 관찰한다.

**§184(2026-09-27) 실행 순서 정정** - 최초 버전(§183)은 두 Phase 모두
prober.start() 직후 곧바로 score_server.py를 띄웠다. 공식 run_once.py는
"요청 부하 시작(prober.start()) -> SLO 기준선 안정 확인(_wait_for_baseline())
-> IF 시작(detector.start())" 순서를 지킨다(run_once.py:711/731/745-746,
코드로 확인됨) - detector가 "요청이 아직 안정되기 전" 구간을 절대 못 보게
하는 구조다. §183 rep1의 CPU 압박 trial에서 실제로 확인된 문제: score_
server.py가 요청 램프업 과도기(cpu_mean이 feature-schema.json의 훈련
데이터 최솟값 0.4616보다도 낮았음)를 그대로 보고 3연속 이상 판정 -> 실제
승격까지 일으켰다 - 이건 후보 임계값의 결함이 아니라 이 테스트 스크립트
자신의 시퀀싱 버그였다(§183 rep1의 t_injection_request가 그 세 평가보다
전부 늦다는 사실만으로 이미 확인됨 - 올바른 순서였다면 그 평가들 자체가
없었을 것). 이제 두 Phase 모두 SLO 기준선이 안정된 뒤에만 score_server.py를
띄운다 - 모델·특징·3연속규칙·CPU강도·후보 임계값(-0.04)은 전부 미변경,
이 파일 자신의 실행 순서만 고쳤다. §183의 이전 결과 파일들은 보존하되
(삭제하지 않음), 이제 이 정정된 순서로 만든 새 run_id(다른 --rep)의
결과만 후보 검증의 유효한 근거로 쓴다 - §183 rep1의 무효 결과는 여전히
'테스트 스크립트 버그였다'는 근거로만 참고하고 후보의 오탐 증거로 재사용
하지 않는다.

**§185(2026-09-28) probe 원자료 수집 공백 수정** - §184로 고친 §183 rep2
(유효한 CPU 압박 trial)에서 새로 발견된 문제: run_cpu_pressure_trial()의
주입 후 관찰 루프가 prober.is_alive()(check_safety_fn을 통해)만 반복 호출
하고 prober.check_slo_violation()/get_baseline_status()는 한 번도 다시 안
불렀다. load_ramp_adapter.py(공유·동결, 안 건드림) 내부의 raw CSV 로컬
캐시는 이 세 메서드 중 하나가 불릴 때만 kubectl exec으로 새로 고쳐지는데,
공식 run_once.py의 OBSERVING 루프는 매 반복 prober.check_slo_violation()을
부른다(run_once.py:824, 코드 확인됨) - 이 호출이 판정뿐 아니라 캐시 갱신도
겸하고 있었다. 이 함수가 그 호출을 안 해서, 캐시가 기준선 대기 종료
시점에서 멈췄다(§183 rep2의 probe raw CSV가 주입 요청보다도 이른 시각에서
끊긴 것으로 실측 확인됨) - load_ramp_adapter.py의 stop()도 마지막 갱신을
안 하므로 pod 삭제 전 최신 데이터가 확보 안 됐다.

이제 관찰 루프가 매 반복 check_slo_violation()을 반환값은 안 보고(§182
철학 유지 - 실제 압박 중 SLO 위반은 예상된 결과) 호출해 캐시를 계속
새로 고치고, 정리 직전에 한 번 더 불러 pod 삭제 전 마지막 데이터를
확보한다. 그 뒤 이 파일 자신이 로컬 raw CSV 파일을 직접 읽어(공유 파일
내부 캐시에는 접근할 방법이 없음) 마지막 요청 시각(probe_last_request_utc)
과 실제 관찰 종료 시각(observation_ended_at_utc)의 간격(probe_data_gap_sec)
을 결과에 남긴다 - 간격이 30초(PROBE_DATA_GAP_THRESHOLD_SEC)를 넘으면
probe_data_gap_detected=True, preemption_timing_evaluable=False로 표시해
그 결과를 "첫 느린 요청 대비 선제 탐지 여부" 판정 근거로 오용하지 못하게
막는다. §183 rep1·rep2의 기존 원자료·판정은 이 수정으로 재계산하지 않고
그대로 둔다(그 결과들은 이미 수집된 그대로가 사실이다).

**§186(2026-09-28) "전환 없음" 대조 관찰 추가(--mode no_switch_control)** -
rep=3·4·5(§185 수정 후 run_cpu_pressure_trial())에서 확인된 "IF 선제 전환이
첫 SLO초과보다 먼저"라는 결과가 실제 피해 감소로 이어지는지 보려면, 같은
조건에서 전환만 없었던 대조가 필요하다. 기존 native arm 결과는 preview
자체가 없어(arm_controller.wrap_injector_with_preview_prep의 _DETECTOR_
SCRIPTS 분기 - native는 그대로 반환) 이 대조로 못 쓴다. 이 모드는 rep=3·4·5와
preview 준비·SLO 기준선 확인·CPU 압박 강도(DEMO_STAGES)·요청 부하·실행
순서·probe 원자료 수집 방식(§185)까지 전부 동일하게 재현하되, score_
server.py(후보 threshold, 동일)의 RECOVERY_POLICY_SIGNAL_URL만 capture_
sink.py(§183 Phase 1과 동일 재사용)로 돌려 IF의 판정·평가는 evidence-log에
그대로 기록되면서도 실제 /signal 전송·정책 결정·승격은 구조적으로 불가능
하게 만든다 - "판정 로직은 동일한데 승격만 없다"는 순수 대조가 성립하는
유일한 방법이다. 주입 장치(cpu_pressure_demo_adapter.py) 자체의 기존 안전
중단 조건(OOMKilled·재시작·Node 이상 시 stage 중단)은 이 모드가 손대지
않는 _run_stages() 내부 로직이라 그대로 적용된다."""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import arm_controller
import node_resource_safety_gate
from active_pod_resolver import get_active_pods
from cpu_pressure_demo_adapter import make_cpu_pressure_injector
from load_ramp_adapter import RESULTS_DIR as PROBE_RESULTS_DIR
from load_ramp_adapter import IMAGE as LR_IMAGE, IMAGE_FORCE_CLOSE, make_load_ramp_prober
from run_cpu_demo_trial import DEMO_STAGES
from run_cpu_demo_shadow_no_injection_trial import _check_safety_or_raise as _check_safety_no_injection
from run_cpu_demo_shadow_response_trial import _check_safety_or_raise as _check_safety_with_injection
from run_once import (
    INJECTION_STARTED_TIMEOUT_SEC, HarnessCorrupted, TrialInvalid,
    _clear_experiment_context, _get_experiment_state, _register_experiment_context,
)

DEFAULT_PROBE_CONFIG = Path(__file__).parent.parent / "chaos" / "probe-config.yaml"
PLAN_ID = "capstone-if-threshold-candidate-183-v1"
# §187(2026-09-28) - fixed_threshold 비교 실행은 후보 IF와 별도 plan_id를 써서
# run_id·contract·summary·detector-evidence 파일이 전부 다른 이름으로 분리된다
# (지시: "결과는 후보 IF 원자료와 다른 run_id·파일에 기록"). PLAN_ID는 안 건드림.
FIXED_THRESHOLD_PLAN_ID = "capstone-fixed-threshold-comparison-187-v1"
DEMO_RESULTS_DIR = Path(__file__).parent / "results" / "capstone_if_threshold_candidate"

# §187 - features.py의 cpu_mean 집계 범위와 fixed_threshold 임계값의 의미가
# 정확히 다르다는 사실을 계약·결과 양쪽에 그대로 남긴다(지시 - 질의·임계값
# 자체는 바꾸지 않음). features.py:69-73(자체 주석 "여러 시계열... 합산") 확인:
# namespace=vllm-serving, container=vllm 라벨에 매칭되는 모든 시계열(주입 중엔
# active+preview 두 Pod)을 초 단위로 SUM한 값이 cpu_mean이다 - 특정 active Pod
# 하나만 골라내지 않는다. 반면 임계값 2.7코어는 fixed_threshold.py.
# compute_threshold_cores(3.0)이 산출한, 단일 컨테이너 3코어 한도(rollout.yaml
# resources.limits.cpu)의 90%다. 즉 실제 판정은 "합산 CPU가 단일 컨테이너 기준
# 90% 바를 넘는가"이며, preview pod의 유휴 CPU(통상 0.02코어 내외, 주입 대상이
# 아님)도 그 합에 포함된다.
CPU_AGGREGATION_NOTE = (
    "features.py의 cpu_mean은 namespace=vllm-serving,container=vllm에 매칭되는 "
    "모든 시계열(주입 중엔 active+preview 두 Pod)을 합산한 값이다(features.py 자체 "
    "주석 '여러 시계열... 합산' 확인, 특정 active Pod만 필터링하지 않음) - 질의는 "
    "변경하지 않았다. fixed_threshold의 적용 임계값(2.7코어)은 fixed_threshold.py."
    "compute_threshold_cores(3.0)이 산출한 값으로, 단일 컨테이너의 3코어 한도(90%)에서 "
    "나온 것이다 - 임계값 자체도 변경하지 않았다. 따라서 실제 판정은 '합산 CPU가 "
    "단일 컨테이너 기준 90% 바를 넘는가'이며, preview pod의 유휴 CPU(통상 0.02코어 "
    "내외)도 합산에 포함된다는 의미 차이를 그대로 기록한다."
)

ANOMALY_DETECTION_DIR = Path(__file__).parent.parent / "anomaly-detection"
MODEL_V32B_DIR = ANOMALY_DETECTION_DIR / "v3" / "model_v32b"
OFFICIAL_ARTIFACTS_DIR = MODEL_V32B_DIR / "artifacts"
CANDIDATE_ARTIFACTS_DIR = MODEL_V32B_DIR / "artifacts_candidate_thr_neg0p04"
MODEL_VERSION = "v3.2b"
OFFICIAL_THRESHOLD = -0.0742929709960305
CANDIDATE_THRESHOLD = -0.04
CONSECUTIVE_THRESHOLD = 3  # score_server.py CONSECUTIVE_THRESHOLD와 동일 - 재확인용 상수(변경 안 함)

REAL_RECOVERY_POLICY_SIGNAL_URL = "http://localhost:8080/signal"  # kubectl port-forward 전제(run_once.py와 동일)

BASELINE_TIMEOUT_SEC = 240.0  # §176/§182와 동일 값 인용
POST_INJECTION_OBSERVATION_SEC = 480.0  # §176/§182와 동일 값 인용
PROBE_INJECTION_START_MARGIN_SEC = 30.0
NO_INJECTION_OBSERVATION_SEC = 600.0  # 공식 calibration 세션 규모(37~38평가, 약 555초)와 비슷한 "짧은" 관찰
PROBE_LIFETIME_NO_INJECTION_SEC = NO_INJECTION_OBSERVATION_SEC + 360.0
PROBE_LIFETIME_CPU_PRESSURE_SEC = BASELINE_TIMEOUT_SEC + PROBE_INJECTION_START_MARGIN_SEC + POST_INJECTION_OBSERVATION_SEC
SAFETY_POLL_INTERVAL_SEC = 5.0
PROBE_READY_TIMEOUT_SEC = 30.0


def _pick_free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http_ok(url: str, timeout_sec: float = 15.0) -> bool:
    import requests
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            if requests.get(url, timeout=2.0).status_code == 200:
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(0.5)
    return False


def _start_subprocess(cmd: list, cwd: Path, stdout_path: Path, stderr_path: Path, env_override: dict = None) -> dict:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    if env_override:
        env.update(env_override)
    stdout_f = open(stdout_path, "w", encoding="utf-8")
    stderr_f = open(stderr_path, "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=stdout_f, stderr=stderr_f, text=True, encoding="utf-8")
    time.sleep(1.5)
    return {"proc": proc, "cmd": cmd, "pid": proc.pid, "alive_after_start": proc.poll() is None,
            "stdout_path": str(stdout_path), "stderr_path": str(stderr_path), "_out": stdout_f, "_err": stderr_f}


def _stop_subprocess(handle: dict, name: str, timeout_sec: float = 10.0) -> dict:
    if handle is None:
        return {"pid": None, "returncode": None, "exited_cleanly": False}
    proc = handle["proc"]
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=timeout_sec)
    handle["_out"].close()
    handle["_err"].close()
    exited_cleanly = proc.poll() is not None
    print(f"[정리] {name} 종료(exit={proc.returncode}, exited_cleanly={exited_cleanly})")
    return {"pid": handle["pid"], "returncode": proc.returncode, "exited_cleanly": exited_cleanly}


class DetectorHandle:
    """run_cpu_pressure_trial()이 탐지기 종류와 무관하게 쓸 수 있는 최소
    어댑터 - is_alive()/stop()/label 세 가지만 있으면 된다(§187, 2026-09-28).
    기존 score_server.py 직접 기동 경로도, 신규 arm_controller 기반
    fixed_threshold.py 경로도 이 셋만 채워 반환한다 - run_cpu_pressure_trial()
    자신의 흐름(preview 준비/기준선/CPU stage/요청부하/관찰/안전/정리)은
    한 줄도 안 바꾼다."""
    def __init__(self, is_alive, stop, label):
        self.is_alive = is_alive
        self.stop = stop
        self.label = label


def _start_candidate_score_server_detector(run_id: str, evidence_log_path: Path) -> DetectorHandle:
    """기존 동작 그대로(§184/§185 이후 변경 없음) - 후보 threshold artifacts로
    score_server.py를 직접 subprocess 기동, 실제 recovery-policy에 연결.
    run_cpu_pressure_trial()의 기본 detector_start_fn - 기존 호출부(rep=3·4·5
    등)는 이 함수를 안 넘겨도 정확히 같은 동작을 그대로 받는다."""
    handle = _start_subprocess(
        [sys.executable, "-u", str(ANOMALY_DETECTION_DIR / "score_server.py"),
         "--artifacts-dir", str(CANDIDATE_ARTIFACTS_DIR), "--model-version", MODEL_VERSION,
         "--run-id", run_id, "--evidence-log", str(evidence_log_path)],
        cwd=ANOMALY_DETECTION_DIR,
        stdout_path=DEMO_RESULTS_DIR / f"score-server-stdout-{run_id}.log",
        stderr_path=DEMO_RESULTS_DIR / f"score-server-stderr-{run_id}.log",
        env_override={"RECOVERY_POLICY_SIGNAL_URL": REAL_RECOVERY_POLICY_SIGNAL_URL})
    label = f"score_server.py(후보 threshold={CANDIDATE_THRESHOLD})"

    def is_alive():
        # 실제 subprocess.Popen()은 절대 None이 아니다(_start_subprocess()가 항상
        # 진짜 Popen을 반환) - proc가 None인 건 기존 테스트가 _start_subprocess를
        # monkeypatch해 실제 프로세스를 안 띄우는 경우뿐이다. 그 경우 기존
        # 테스트들이 이미 제어하던 alive_after_start로 그대로 폴백해 기존 동작을
        # 100% 보존한다(실제 실행 경로는 proc.poll() 실측 그대로, 변경 없음).
        proc = handle["proc"]
        return proc.poll() is None if proc is not None else handle["alive_after_start"]

    return DetectorHandle(is_alive=is_alive, stop=lambda: _stop_subprocess(handle, "score_server.py"), label=label)


def _start_fixed_threshold_detector(run_id: str, evidence_log_path: Path) -> DetectorHandle:
    """§187(2026-09-28) - 기존 arm_controller의 고정 임계값 명령 생성 경로를
    그대로 재사용한다(_build_detector_command/make_detector_for_arm, 새 명령
    조립 로직 없음). fixed_threshold.py는 score_server.py에서 WINDOW_SEC/
    CONSECUTIVE_THRESHOLD/COOLDOWN_SEC/EVAL_INTERVAL_SEC를 import로 그대로
    가져다 쓰므로(anomaly-detection/fixed_threshold.py 자체 docstring) 집계창·
    연속조건·cooldown이 후보 IF와 구조적으로 동일하다 - 여기서 별도로 지정하지
    않는다(지정할 값 자체가 없음, import가 곧 동일성 보장).

    임계값(cpu_limit_cores)은 arm_controller.FIXED_THRESHOLD_CPU_LIMIT_CORES
    (=3.0, 계약서 §6 동결값)를 그대로 쓴다 - 이 파일에서 새 상수를 만들지
    않는다. RECOVERY_POLICY_SIGNAL_URL 환경변수도 따로 안 건드린다 -
    arm_controller._resolved_signal_url()의 기본값(LOCAL_RECOVERY_POLICY_
    SIGNAL_URL="http://localhost:8080/signal")이 이미 REAL_RECOVERY_POLICY_
    SIGNAL_URL과 정확히 같은 값이라 별도 override가 불필요하다(코드 확인 완료).

    detector.start()는 arm_controller 자신의 reachability preflight(recovery-
    policy·Prometheus 둘 다 확인, 실패 시 TrialInvalid)를 이미 포함한다 -
    여기서 그 예외를 삼키지 않고 호출자에게 그대로 전파한다."""
    detector = arm_controller.make_detector_for_arm(
        "fixed_threshold", run_id, evidence_log_path=str(evidence_log_path))
    detector.start()
    label = (f"fixed_threshold.py(cpu_limit_cores={arm_controller.FIXED_THRESHOLD_CPU_LIMIT_CORES}, "
             f"threshold={arm_controller.FIXED_THRESHOLD_CPU_LIMIT_CORES * 0.90}코어)")
    return DetectorHandle(is_alive=detector.is_alive, stop=detector.stop, label=label)


def _start_sink_only_detector(run_id: str, evidence_log_path: Path, sink_url: str, *, script_name: str,
                               extra_args: list = None) -> DetectorHandle:
    """§189 - run_no_injection_smoke()/run_no_switch_control_trial() 전용
    공통 헬퍼(둘 다 RECOVERY_POLICY_SIGNAL_URL을 실제 정책이 아니라 그때그때
    새로 고른 sink_url로만 덮어써야 한다 - _start_candidate_score_server_
    detector()는 REAL_RECOVERY_POLICY_SIGNAL_URL을 쓰므로 재사용 불가).
    script_name만 "score_server.py"(공식) 또는 "gated_signal_detector.py"
    (§189 실험용)로 바꿔 같은 sink-only 기동 절차를 공유한다.

    §204(2026-10-01) - extra_args(기본 None=기존 호출부 전부 동작 불변):
    gated_signal_detector.py 전용 --consecutive-threshold-override 같은
    1회 진단용 인자를 명령줄 끝에 그대로 덧붙이기 위함 - score_server.py는
    이 인자를 모르므로 공식 탐지기 호출부에는 절대 넘기지 않는다."""
    cmd = [sys.executable, "-u", str(ANOMALY_DETECTION_DIR / script_name),
           "--artifacts-dir", str(CANDIDATE_ARTIFACTS_DIR), "--model-version", MODEL_VERSION,
           "--run-id", run_id, "--evidence-log", str(evidence_log_path)]
    if extra_args:
        cmd.extend(extra_args)
    handle = _start_subprocess(
        cmd,
        cwd=ANOMALY_DETECTION_DIR,
        stdout_path=DEMO_RESULTS_DIR / f"{script_name.replace('.py', '')}-stdout-{run_id}.log",
        stderr_path=DEMO_RESULTS_DIR / f"{script_name.replace('.py', '')}-stderr-{run_id}.log",
        env_override={"RECOVERY_POLICY_SIGNAL_URL": sink_url})
    label = f"{script_name}(후보 threshold={CANDIDATE_THRESHOLD}, sink 전용={sink_url})"

    def is_alive():
        proc = handle["proc"]
        return proc.poll() is None if proc is not None else handle["alive_after_start"]

    return DetectorHandle(is_alive=is_alive, stop=lambda: _stop_subprocess(handle, script_name), label=label)


def _start_candidate_score_server_detector_for_sink(run_id: str, evidence_log_path: Path, sink_url: str) -> DetectorHandle:
    """run_no_injection_smoke()/run_no_switch_control_trial()의 기존
    기본값(§184/§185/§186 이후 변경 없음) - 공식 score_server.py를 sink
    전용으로 기동. 기존 호출부는 이 함수를 안 넘겨도 정확히 같은 동작을
    그대로 받는다(§189 - 새 detector_start_fn 매개변수의 기본값)."""
    return _start_sink_only_detector(run_id, evidence_log_path, sink_url, script_name="score_server.py")


def _start_gated_score_server_detector_for_sink(run_id: str, evidence_log_path: Path, sink_url: str) -> DetectorHandle:
    """§189 - mem_slope>0 게이트 실험용 탐지기(anomaly-detection/gated_
    signal_detector.py, 공식 score_server.py 무수정)를 sink 전용으로 기동.
    이 함수를 쓰는 호출부는 실제 recovery-policy에 절대 연결되지 않는다 -
    sink_url을 호출자가 넘기고, 그 값만 환경변수로 주입한다(REAL_RECOVERY_
    POLICY_SIGNAL_URL은 이 함수 어디에도 등장하지 않음 - 구조적으로 실제
    정책 경로가 없다는 것을 코드로 보장한다)."""
    return _start_sink_only_detector(run_id, evidence_log_path, sink_url, script_name="gated_signal_detector.py")


def make_gated_detector_with_consecutive_override(consecutive_threshold: int):
    """§204(2026-10-01) - "2연속 + mem_slope>0 게이트" 후보를 공유
    threshold.json·score_server.py의 CONSECUTIVE_THRESHOLD를 전혀 안
    건드리고 진단하기 위한 팩토리. gated_signal_detector.py가 이미 가진
    --consecutive-threshold-override(§204)만 명령줄에 덧붙인다 - sink 전용
    연결(_start_gated_score_server_detector_for_sink와 완전히 동일, 실제
    recovery-policy 경로 없음)은 그대로 유지한다. 반환값은 detector_start_fn
    (run_id, evidence_log_path, sink_url) 계약을 그대로 따른다."""
    def _start(run_id: str, evidence_log_path: Path, sink_url: str) -> DetectorHandle:
        return _start_sink_only_detector(
            run_id, evidence_log_path, sink_url, script_name="gated_signal_detector.py",
            extra_args=["--consecutive-threshold-override", str(consecutive_threshold)])
    return _start


def _advance_streak_offline(scores: list, threshold: float, n: int = CONSECUTIVE_THRESHOLD) -> dict:
    """score_server.advance_streak()의 판정식(is_anomalous = score < threshold,
    연속 미달 시 즉시 0으로 리셋)을 그대로 재현한 순수 오프라인 재적용 - 그
    함수를 import하지 않는 이유는 cooldown/신호전송 부수효과 없이 "몇 번째에서
    3연속이 되는가"만 필요하기 때문(재구현이 아니라 동일 결정식의 부분 재사용)."""
    streak = 0
    for i, s in enumerate(scores):
        streak = streak + 1 if s < threshold else 0
        if streak >= n:
            return {"fired": True, "fire_index": i, "fire_count": 1}
    return {"fired": False, "fire_index": None, "fire_count": 0}


def _load_evidence_scores(evidence_log_path: Path) -> list:
    if not evidence_log_path.exists():
        return []
    out = []
    with open(evidence_log_path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            d = json.loads(line)
            if d.get("record_type") == "evaluation_decision":
                out.append(d)
    return out


def run_no_injection_smoke(
    run_id: str, injector, prober, sink_port: int, evidence_log_path: Path, sink_out_path: Path,
    probe_raw_csv_path: Path = None,
    observation_sec: float = NO_INJECTION_OBSERVATION_SEC,
    baseline_timeout_sec: float = BASELINE_TIMEOUT_SEC,
    probe_ready_timeout_sec: float = PROBE_READY_TIMEOUT_SEC,
    safety_poll_interval_sec: float = SAFETY_POLL_INTERVAL_SEC,
    check_safety_fn=_check_safety_no_injection,
    detector_start_fn=_start_candidate_score_server_detector_for_sink,
    sleep_fn=time.sleep, monotonic_fn=time.monotonic,
    detector_ready_fn=None, detector_ready_timeout_sec: float = None,
) -> dict:
    """2026-10-05 - detector_ready_fn(선택, 기본 None = 기존 동작 그대로): 주면 탐지기 기동 뒤 이 함수가
    {"ready": True}를 돌려줄 때까지(최대 detector_ready_timeout_sec) 기다린 뒤에야 observation_sec을 재기
    시작한다. 대기 중에도 안전 감시·탐지기/sink 생존 확인을 그대로 하고, 시간 초과는 TrialInvalid,
    {"error": ...}(기록 오류)·탐지기/sink 종료는 HarnessCorrupted로 기존 정리 경로를 탄다.

    preview+1RPS만 켜두고(주입 없음) 후보 artifacts로 탐지기를
    capture_sink에 연결해 관찰한다. §180과 같은 엄격한 안전 감시(SLO 위반
    포함 전부 즉시 중단 - '아무 일도 없어야 정상'인 관찰이므로)를 그대로
    재사용한다.

    §184 정정 - 실행 순서를 공식 run_once.py와 동일하게 "요청 부하 시작 ->
    SLO 기준선 안정 확인 -> IF 시작"으로 고정한다(모델·특징·3연속규칙·
    CPU강도·후보 임계값 전부 미변경 - 이 파일 자신의 시퀀싱만 수정).

    §189 - detector_start_fn(run_id, evidence_log_path, sink_url)만 바꾸면
    공식 score_server.py 대신 실험용 gated_signal_detector.py를 쓸 수
    있다(run_cpu_pressure_trial()의 detector_start_fn과 같은 원칙, sink_url
    인자만 추가 - 이 함수는 항상 sink에만 연결하므로 실제 정책 URL을 아예
    모른다). 기본값은 기존과 100% 동일한 공식 score_server.py 경로."""
    cleanup_state = {"done": False}
    outcome = {"status": None, "reason": None, "elapsed_sec": None,
               "prober_stop_failed": None, "injector_cleanup_failed": None,
               "sink_exit": None, "score_server_exit": None,
               "observation_ended_at_utc": None, "probe_last_request_utc": None,
               "probe_data_gap_sec": None, "probe_data_gap_detected": True,
               "probe_max_internal_gap_sec": None, "cleanup_status": "incomplete",
               "invalidity_reasons": None}
    sink_handle = {"h": None}
    detector_ref = {"d": DetectorHandle(is_alive=lambda: False,
                                         stop=lambda: _stop_subprocess(None, "score_server.py"),
                                         label="detector(시작 전)")}

    def do_cleanup():
        if cleanup_state["done"]:
            return
        cleanup_state["done"] = True
        # §190 - 여기서 prober.check_slo_violation() 등 추가 네트워크 호출을
        # 새로 부르지 않는다(run_cpu_pressure_trial/run_no_switch_control_trial의
        # "정리 직전 최종 갱신"과 다른 점, 의도적) - 그 호출 자체가 kubectl exec을
        # 타임아웃 없이 부르는 공유·동결 어댑터(load_ramp_adapter._run())를 거치므로
        # 무기한 대기할 수 있다(§189 게이트 라이브에서 실제로 의심된 원인). 이
        # 함수는 관찰 루프가 이미 주기적으로 채워둔 로컬 CSV 캐시를 그대로 읽기만
        # 한다 - 새 hang 유발 지점을 추가하지 않는다.
        outcome["observation_ended_at_utc"] = datetime.now(timezone.utc).isoformat()
        outcome["score_server_exit"] = detector_ref["d"].stop()
        outcome["sink_exit"] = _stop_subprocess(sink_handle["h"], "capture_sink.py")
        # §191 - CalledProcessError(§190에서 timeout=이 이 타입으로도 실패를
        # 던지게 됨)는 str(e)만으로는 "exit status 124"만 보이고 실제 원인
        # (타임아웃 여부·대상 명령)이 담긴 .stderr가 빠진다 - 아래 두
        # except에서만 그 상세를 덧붙인다(TrialInvalid 등 기존 예외엔
        # .stderr가 없어 getattr 기본값 None → 기존 형식 그대로 불변).
        try:
            prober.stop()
        except Exception as e:
            detail = getattr(e, "stderr", None)
            outcome["prober_stop_failed"] = f"{type(e).__name__}: {e}" + (f" / {detail}" if detail else "")
            print(f"[정리] prober.stop() 실패(계속 진행): {e}")
        try:
            injector.cleanup()
        except Exception as e:
            detail = getattr(e, "stderr", None)
            outcome["injector_cleanup_failed"] = f"{type(e).__name__}: {e}" + (f" / {detail}" if detail else "")
            print(f"[정리] injector.cleanup() 실패: {e}")
        try:
            _finalize_trial_outcome(outcome, probe_raw_csv_path, outcome["observation_ended_at_utc"])
        except Exception as e:
            print(f"[정리] 결과 유효성 최종 판정 실패(계속 진행): {e}")

    baseline_state = {"active_restart_count": None}
    t_entry = monotonic_fn()
    try:
        injector.prepare()
        prober.start()
        if not _wait_for(prober.is_alive, probe_ready_timeout_sec, 1.0):
            raise TrialInvalid("probe가 시작 후 살아있음을 확인 못함")

        # §184 정정 - 공식 run_once.py 순서(요청 부하 시작 -> SLO 기준선 안정 확인 -> IF 시작)를
        # 그대로 재현한다(run_once.py:711 prober.start() -> 731 _wait_for_baseline() -> 745-746
        # detector.start(), 코드 확인됨). 이전 버전은 이 대기 없이 곧장 detector를 띄워, 공식
        # 배포가 절대 노출 안 시키는 요청 램프업 과도 구간을 그대로 노출시켰다(§183 rep1에서
        # 실제로 이 구간의 cpu_mean이 훈련 데이터 최솟값보다도 낮았음 - 확인됨).
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
                raise TrialInvalid(f"SLO 기준선 대기 상한({baseline_timeout_sec:.0f}s) 초과")
            sleep_fn(1.0)

        sink_url = f"http://127.0.0.1:{sink_port}/signal"
        sink_handle["h"] = _start_subprocess(
            [sys.executable, "-u", str(MODEL_V32B_DIR / "capture_sink.py"),
             "--port", str(sink_port), "--out", str(sink_out_path), "--run-id", run_id],
            cwd=MODEL_V32B_DIR,
            stdout_path=DEMO_RESULTS_DIR / f"sink-stdout-{run_id}.log",
            stderr_path=DEMO_RESULTS_DIR / f"sink-stderr-{run_id}.log")
        if not sink_handle["h"]["alive_after_start"] or not _wait_http_ok(f"http://127.0.0.1:{sink_port}/healthz"):
            raise HarnessCorrupted("capture_sink.py가 정상 기동하지 않음 - fail-closed(정책 오염 위험 회피)")

        detector_ref["d"] = detector_start_fn(run_id, evidence_log_path, sink_url)
        if not detector_ref["d"].is_alive():
            raise HarnessCorrupted(f"{detector_ref['d'].label}가 기동 직후 종료됨")
        print(f"{detector_ref['d'].label} 기동 확인, sink={sink_url}로만 연결(정책 미연결)")

        if detector_ready_fn is not None:  # 2026-10-05 c1 시작 절차 v2 - 첫 정상 평가 기록 확인 뒤 관찰 시작
            _wait_detector_ready(outcome, detector_ref["d"], sink_handle["h"], detector_ready_fn, detector_ready_timeout_sec,
                                 check_safety_fn, baseline_state, prober, safety_poll_interval_sec, monotonic_fn, sleep_fn)
            outcome["observation_started_at_utc"] = datetime.now(timezone.utc).isoformat()

        deadline = monotonic_fn() + observation_sec
        last_safety_check = 0.0
        while monotonic_fn() < deadline:
            now = monotonic_fn()
            if now - last_safety_check >= safety_poll_interval_sec:
                check_safety_fn(baseline_state, prober)
                if not detector_ref["d"].is_alive():
                    raise HarnessCorrupted(f"{detector_ref['d'].label}가 관찰 중 예상 밖 종료됨")
                if sink_handle["h"]["proc"].poll() is not None:
                    raise HarnessCorrupted("capture_sink.py가 관찰 중 예상 밖 종료됨 - 신호 유실 위험")
                last_safety_check = now
            sleep_fn(1.0)
        outcome["status"] = "completed"
        outcome["reason"] = f"무주입 관찰 {observation_sec:.0f}초 완료"
    except (TrialInvalid, HarnessCorrupted, subprocess.CalledProcessError) as e:
        # §191(2026-09-29) - injector.prepare()/prober.start()는 kubectl
        # run·cp·exec를 check=True로 부른다(load_ramp_adapter.py, §190에서
        # timeout=까지 추가됨) - 이 명령이 실패하거나 타임아웃되면
        # subprocess.CalledProcessError가 나는데, 이전엔 이 except가
        # TrialInvalid/HarnessCorrupted만 잡아서 이 예외는 그대로
        # 전파됐다. finally의 do_cleanup()은 Python 자체 규칙상 그래도
        # 실행되지만(정리 자체는 시도됨), 함수가 outcome을 반환하지 못한
        # 채 스크립트 전체가 죽어 그 정리 결과(cleanup_status 등)가 파일에
        # 남지 못했다 - 이미 pod가 생성된 뒤(예: kubectl run은 성공했는데
        # 그 다음 kubectl cp가 타임아웃) 실패해도 마찬가지였다. 이제
        # 이 예외도 여기서 잡아 정상적으로 outcome을 반환하고, finally의
        # do_cleanup()이 이미 계산해 둔 cleanup_status/invalidity_reasons가
        # 파일에 그대로 남는다(코드 변경 없이 기존 §189/§190 경로 재사용).
        outcome["status"] = "aborted"
        detail = getattr(e, "stderr", None)  # TrialInvalid/HarnessCorrupted엔 없음 - 기존 두 예외의 reason 형식은 불변
        outcome["reason"] = f"{type(e).__name__}: {e}" + (f" / {detail}" if detail else "")
    finally:
        do_cleanup()
        outcome["elapsed_sec"] = monotonic_fn() - t_entry
    return outcome


def _wait_detector_ready(outcome, detector, sink_h, ready_fn, timeout_sec, check_safety_fn, baseline_state, prober,
                         safety_poll_interval_sec, monotonic_fn, sleep_fn) -> None:
    """2026-10-05 - 탐지기 기동 뒤 ready_fn()이 {"ready": True}를 줄 때까지 대기(상한 timeout_sec). 대기 중에도 안전 감시와
    탐지기·sink 생존 확인을 한다. 시간 초과는 TrialInvalid, 기록 오류({"error"})·탐지기/sink 종료는 HarnessCorrupted -
    호출부의 기존 정리 경로를 탄다. 경계: 안전검사·판정이 끝난 뒤 시각으로 경과를 다시 재서, 경과 == 상한이면 ready 수용,
    경과 > 상한이면 ready여도 거부, 준비 안 됨 + 경과 >= 상한이면 거부. 결과는 outcome["detector_readiness"]."""
    ready = {"timeout_sec": timeout_sec, "wait_started_at_utc": datetime.now(timezone.utc).isoformat(),
             "ready_at_utc": None, "waited_sec": None, "detail": None}
    outcome["detector_readiness"] = ready
    t_wait, last_safety_check = monotonic_fn(), None
    while True:
        now = monotonic_fn()
        if last_safety_check is None or now - last_safety_check >= safety_poll_interval_sec:
            check_safety_fn(baseline_state, prober)
            last_safety_check = now
        if not detector.is_alive():
            raise HarnessCorrupted(f"{detector.label}가 첫 평가 대기 중 종료됨 - 다음 단계 시작 안 함")
        if sink_h["proc"].poll() is not None:
            raise HarnessCorrupted("capture_sink.py가 첫 평가 대기 중 종료됨 - 다음 단계 시작 안 함")
        r = ready_fn()
        waited = monotonic_fn() - t_wait
        ready.update(detail=r.get("detail"), waited_sec=round(waited, 1))
        if r.get("error"):
            raise HarnessCorrupted(f"탐지기 평가 기록 오류 - 다음 단계 시작 안 함: {r['error']}")
        if waited > timeout_sec or (not r.get("ready") and waited >= timeout_sec):
            raise TrialInvalid(f"탐지기 첫 정상 평가 대기 상한({timeout_sec:.0f}s) 초과 - 다음 단계 시작 안 함")
        if r.get("ready"):
            ready["ready_at_utc"] = datetime.now(timezone.utc).isoformat()
            return
        sleep_fn(1.0)


def _wait_for(check_fn, timeout: float, interval: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check_fn():
            return True
        time.sleep(interval)
    return False


PROBE_DATA_GAP_THRESHOLD_SEC = 30.0  # 이 이상 뒤처지면 "요청 원자료 수집 공백"으로 표시


def _last_probe_request_utc(probe_raw_csv_path: Path):
    """probe raw CSV(loca_ramp_adapter.py가 이미 로컬에 쓰는 그 파일)를 직접
    읽어 마지막 sent_at을 구한다 - load_ramp_adapter.py의 캐시 내부 상태에는
    Prober 인터페이스 밖에서 접근할 방법이 없고, 그 파일 자체(공유·동결)를
    건드리지 않기 위해 이미 디스크에 쓰인 결과만 이 파일에서 직접 읽는다."""
    if probe_raw_csv_path is None or not probe_raw_csv_path.exists():
        return None
    import csv as _csv
    last = None
    with open(probe_raw_csv_path, encoding="utf-8") as f:
        for row in _csv.DictReader(f):
            sent_at = row.get("sent_at")
            if sent_at and (last is None or sent_at > last):
                last = sent_at
    return last


def _probe_data_gap_diagnostics(probe_raw_csv_path: Path, observation_ended_at_utc: str) -> dict:
    """§185(2026-09-28) - §183 rep2에서 실제로 발견된 문제(주입 후 관찰 중
    prober를 한 번도 다시 안 불러 로컬 CSV 캐시가 기준선 대기 종료 시점
    이후로 멈춤)의 재발을 결과에서 바로 알아볼 수 있게 한다. last_request_utc
    와 observation_ended_at_utc의 간격이 크면(기본 30초) probe_data_gap_
    detected=True로 남기고, 호출자가 이 값을 보고 "선제성 평가 가능"으로
    표시하지 않게 한다(이 함수 자신은 그 표시를 하지 않는다 - 진단만 제공)."""
    last = _last_probe_request_utc(probe_raw_csv_path)
    result = {
        "probe_last_request_utc": last, "observation_ended_at_utc": observation_ended_at_utc,
        "gap_sec": None, "probe_data_gap_detected": True,
    }
    if last is None or observation_ended_at_utc is None:
        return result
    gap = (datetime.fromisoformat(observation_ended_at_utc) - datetime.fromisoformat(last)).total_seconds()
    result["gap_sec"] = gap
    result["probe_data_gap_detected"] = gap > PROBE_DATA_GAP_THRESHOLD_SEC
    return result


def _max_internal_probe_gap_sec(probe_raw_csv_path: Path):
    """§190(2026-09-29) - probe raw CSV 안에서 연속 요청 사이 최대 간격(초).
    끝부분만 보는 _probe_data_gap_diagnostics()와 달리, 관찰 도중 있었다가
    끝나기 전에 회복된 공백(예: 일시적 네트워크 장애)도 잡는다 - 지시
    "장시간 공백이 있으면 completed가 아니라 무효로 기록"의 "끝까지
    이어지지 않음"과 별개인 조건. 표본이 2개 미만이거나 파일을 못
    읽으면(누락·파싱 실패) None을 반환한다 - 그 경우는 이미
    _probe_data_gap_diagnostics()의 fail-closed 판정(파일 없음/최신 요청
    없음 -> probe_data_gap_detected=True)이 따로 잡으므로, 여기서 표본
    부족을 "나쁜 값"으로 가정하면 정상적으로 짧게 끝난 실행(예: 안전위반
    직후 중단)까지 오염된 것처럼 보이게 된다."""
    if probe_raw_csv_path is None or not probe_raw_csv_path.exists():
        return None
    import csv as _csv
    times = []
    try:
        with open(probe_raw_csv_path, encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                sent_at = row.get("sent_at")
                if sent_at:
                    times.append(datetime.fromisoformat(sent_at))
        if len(times) < 2:
            return None
        # sort()/max()도 try 안에 둔다(2026-09-29 코드리뷰 지적) - 일부
        # 행만 tz-aware/naive가 섞이면 fromisoformat() 자체는 성공해도
        # 비교 시 TypeError가 나고, csv.Error(행 구조 손상)도 열거 도중
        # 날 수 있어 둘 다 여기서 잡아야 "파싱 실패 시 None"이라는
        # 이 함수의 계약이 모든 실패 유형에서 실제로 지켜진다.
        times.sort()
        return max((b - a).total_seconds() for a, b in zip(times, times[1:]))
    except (OSError, ValueError, TypeError, _csv.Error):
        return None


def _finalize_trial_outcome(outcome: dict, probe_raw_csv_path: Path = None,
                             observation_ended_at_utc: str = None) -> None:
    """§190(2026-09-29) - §189 게이트 무주입 60분 라이브에서 실제로 발생한
    문제(probe 원자료가 관찰 종료보다 한참 전에 멈췄는데도, 그리고
    prober.stop()/injector.cleanup()이 네트워크 단절로 실패했는데도
    outcome_status가 "completed"로 남아 하네스가 직접 무효를 표시하지
    못함 - 사람이 원자료를 사후에 직접 분석해야 발견됐다)의 재발을 막는다.
    do_cleanup() 끝에서 딱 한 번 호출한다.

    지시("요청 원자료의 마지막 시각이 관찰 종료까지 이어지지 않거나 장시간
    공백이 있으면 completed가 아니라 무효로 기록", "prober.stop() 또는
    injector.cleanup() 실패도 최종 결과를 무효로 만들고, 원인과 정리
    미완료 여부를 파일에 남길 것")를 그대로 구현한다 - status는
    "completed"였을 때만 "invalid"로 바꾼다. "aborted"(안전위반으로 이미
    중단됨)는 그 자체로 이미 의미 있는 신호라 문자열은 안 건드리되,
    invalidity_reasons/cleanup_status는 aborted에도 동일하게 남긴다(정리
    완료 여부는 실행 결과와 별개로 항상 파일에 있어야 함 - 지시 그대로).
    probe_raw_csv_path가 없으면(그 축을 안 쓰는 호출자) gap 관련 검사는
    건너뛴다 - 새 판정 축을 강제로 추가하지 않는다.

    §190 코드리뷰 수정(2026-09-29, 블로킹 지적) - probe 진단 두 호출을 각각
    자기 try/except로 감싼다. 원래는 감싸지 않아, 손상된 CSV(예: 최신
    시각이 파싱 불가능한 문자열)가 _probe_data_gap_diagnostics()에서
    예외를 내면 그 아래의 cleanup_status 계산 자체가 전혀 실행되지 않아
    - "정리 실패는 probe 상태와 무관하게 항상 무효로 만들어야 한다"는
    지시를 정확히 어기는 조합(손상된 CSV + 정리 실패)이 completed로
    남을 수 있었다. 이제 진단 실패 자체도 fail-closed로 하나의 무효
    사유(probe_data_diagnostic_error)로 남기고, cleanup_status 계산은
    그 성공 여부와 무관하게 항상 실행된다."""
    reasons = []
    if probe_raw_csv_path is not None and observation_ended_at_utc is not None:
        try:
            gap_diag = _probe_data_gap_diagnostics(probe_raw_csv_path, observation_ended_at_utc)
            outcome["probe_last_request_utc"] = gap_diag["probe_last_request_utc"]
            outcome["probe_data_gap_sec"] = gap_diag["gap_sec"]
            outcome["probe_data_gap_detected"] = gap_diag["probe_data_gap_detected"]
            if gap_diag["probe_data_gap_detected"]:
                reasons.append("probe_data_gap")
            if gap_diag["gap_sec"] is not None and gap_diag["gap_sec"] < 0:
                reasons.append("probe_clock_inconsistent")  # last_request가 observation_ended보다 나중 - 시계 불일치
        except Exception as e:
            outcome["probe_data_gap_detected"] = True  # fail-closed - 진단 자체가 실패하면 신뢰 못 함
            reasons.append("probe_data_diagnostic_error")
            print(f"[정리] probe 공백 진단 실패(fail-closed으로 무효 사유에 반영): {e}")
        try:
            max_internal_gap = _max_internal_probe_gap_sec(probe_raw_csv_path)
            outcome["probe_max_internal_gap_sec"] = max_internal_gap
            if max_internal_gap is not None and max_internal_gap > PROBE_DATA_GAP_THRESHOLD_SEC:
                reasons.append("probe_internal_gap")
        except Exception as e:
            if "probe_data_diagnostic_error" not in reasons:
                reasons.append("probe_data_diagnostic_error")
            print(f"[정리] probe 내부 공백 스캔 실패(fail-closed으로 무효 사유에 반영): {e}")
    cleanup_ok = outcome["prober_stop_failed"] is None and outcome["injector_cleanup_failed"] is None
    outcome["cleanup_status"] = "complete" if cleanup_ok else "incomplete"
    if not cleanup_ok:
        reasons.append("cleanup_incomplete")
    outcome["invalidity_reasons"] = reasons
    if outcome["status"] == "completed" and reasons:
        outcome["status"] = "invalid"
        outcome["reason"] = outcome["reason"] + f" - 무효 사유: {', '.join(reasons)}"


def _capture_recent_pod_events(namespace: str = "vllm-serving", since_utc: str = None) -> list:
    """§187(2026-09-28) - 읽기 전용 관찰 전용. 구 Pod 종료 이벤트·Pod
    식별자를 보존하는 것이 유일한 목적이고, 여기서는 수집·저장만 한다 -
    이 이벤트 시각만으로 요청의 연결 대상이나 실패 원인을 확정하지
    말라는 지시는 이 함수의 호출부(main())가 지킨다(이 함수 자신은 해석을
    전혀 안 하므로 자동으로 지켜짐). `kubectl get events`는 읽기 전용
    조회 - 클러스터 상태를 바꾸지 않는다. 실패해도 trial 판정에 영향을
    주면 안 되므로 예외를 삼키고 진단 문자열만 남긴다(memory-pressure-
    safety 로그와 같은 원칙)."""
    try:
        result = subprocess.run(
            ["kubectl", "get", "events", "-n", namespace, "-o", "json", "--sort-by=.lastTimestamp"],
            capture_output=True, text=True, encoding="utf-8", timeout=30)
        if result.returncode != 0:
            return [{"capture_error": result.stderr.strip()}]
        items = json.loads(result.stdout).get("items", [])
        events = [{
            "type": e.get("type"), "reason": e.get("reason"),
            "involved_object": {"kind": e.get("involvedObject", {}).get("kind"),
                                 "name": e.get("involvedObject", {}).get("name"),
                                 "uid": e.get("involvedObject", {}).get("uid")},
            "message": e.get("message"),
            "first_timestamp": e.get("firstTimestamp"), "last_timestamp": e.get("lastTimestamp"),
            "count": e.get("count"),
        } for e in items]
        if since_utc:
            events = [e for e in events if e["last_timestamp"] and e["last_timestamp"] >= since_utc]
        return events
    except Exception as e:
        return [{"capture_error": f"{type(e).__name__}: {e}"}]


def _fetch_audit_verified_at(run_id: str, admin_base_url: str = "http://localhost:8080") -> dict:
    """§187(2026-09-28) - recovery-policy의 promote 결과에 이미 있는
    verified_at을 그대로 회수한다(재계산하지 않음, 지시 그대로).
    /admin/audit/{run_id}는 기존 git 기반 감사 outbox를 읽기만 하는 GET
    엔드포인트(recovery-policy/main.py의 git_client.read_audit() 그대로,
    새 경로 아님). 이 값은 recovery-policy 자신이 activeSelector 변경을
    처음 검증한 시각이다 - 개별 요청이 새 Pod에 도달한 시각으로 절대
    표현하지 않는다(호출부가 그 의미로만 쓴다). 실패해도 trial 판정에
    영향을 주면 안 되므로 예외를 삼키고 진단만 남긴다."""
    import requests
    try:
        r = requests.get(f"{admin_base_url}/admin/audit/{run_id}", timeout=10)
        r.raise_for_status()
        records = r.json().get("records", [])
    except Exception as e:
        return {"fetch_error": f"{type(e).__name__}: {e}", "records": []}
    out = []
    for rec in records:
        result = rec.get("result") or {}
        out.append({
            "record_id": rec.get("record_id"), "decided_at": rec.get("decided_at"),
            "signal_source": rec.get("signal_source"), "action": rec.get("action"),
            "outcome": rec.get("outcome"), "verified": result.get("verified"),
            "verified_at": result.get("verified_at"),
        })
    return {"fetch_error": None, "records": out}


def run_cpu_pressure_trial(
    run_id: str, injector, prober, evidence_log_path: Path, probe_raw_csv_path: Path = None,
    baseline_timeout_sec: float = BASELINE_TIMEOUT_SEC,
    post_injection_observation_sec: float = POST_INJECTION_OBSERVATION_SEC,
    injection_started_timeout_sec: float = INJECTION_STARTED_TIMEOUT_SEC,
    probe_ready_timeout_sec: float = PROBE_READY_TIMEOUT_SEC,
    safety_poll_interval_sec: float = SAFETY_POLL_INTERVAL_SEC,
    check_safety_fn=_check_safety_with_injection,
    sleep_fn=time.sleep, monotonic_fn=time.monotonic,
    detector_start_fn=_start_candidate_score_server_detector,
) -> dict:
    """Phase 2 - 실제 CPU 주입 + 탐지기를 실제 recovery-policy에 연결. §182와
    동일하게 안전 감시는 하네스 이상(probe 생존·active pod 식별·Node)만
    본다(실제 압박 결과인 OOMKilled·재시작·SLO 위반은 중단 사유 아님).

    §187(2026-09-28) - detector_start_fn 매개변수 추가. preview 준비·기준선
    대기·CPU stage 주입·요청 부하·관찰·안전 감시·정리 흐름은 전부 그대로고,
    "어떤 탐지기를 어떻게 띄우는가"만 이 함수로 분리했다 - 기본값
    (_start_candidate_score_server_detector)은 기존 동작과 100% 동일해
    기존 호출부(rep=3·4·5 등)는 아무 것도 안 바뀐다. fixed_threshold.py
    비교 실행은 _start_fixed_threshold_detector를 넘겨 같은 흐름을 그대로
    재사용한다(이 함수를 통째로 복제하지 않음).

    §185(2026-09-28) 정정 - §183 rep2에서 발견된 문제: 이 함수의 주입 후
    관찰 루프가 prober.check_slo_violation()/get_baseline_status()를 한 번도
    다시 안 불러(check_safety_fn은 prober.is_alive()만 봄) load_ramp_adapter.py
    내부의 raw CSV 캐시가 기준선 대기 종료 시점 이후로 멈췄다(공식 run_once.py
    OBSERVING 루프는 매 반복 prober.check_slo_violation()을 부른다 - run_once.py:824
    확인됨 - 그게 캐시를 계속 새로 고치는 역할도 겸한다). 이제 이 루프도 매
    반복 check_slo_violation()을 불러 캐시를 계속 새로 고치되, 그 반환값으로
    중단하지는 않는다(§182 철학 유지 - 실제 압박 중 SLO 위반은 예상된 결과).
    정리 직전에도 한 번 더 불러 pod 삭제 전 가능한 한 최신 데이터를 남긴다.
    load_ramp_adapter.py(공유·동결) 자체는 안 건드린다."""
    cleanup_state = {"done": False}
    outcome = {"status": None, "reason": None, "elapsed_sec": None,
               "prober_stop_failed": None, "injector_cleanup_failed": None, "score_server_exit": None,
               "injection_valid": None, "t_injection_requested_utc": None, "t_injection_observed_utc": None,
               "observation_ended_at_utc": None, "probe_last_request_utc": None,
               "probe_data_gap_sec": None, "probe_data_gap_detected": True, "stage_windows": None,
               "probe_max_internal_gap_sec": None, "cleanup_status": "incomplete",
               "invalidity_reasons": None}
    # 탐지기가 아직 시작되기 전에(예: 기준선 대기 중 안전검사 실패) 예외가 나도
    # 기존 동작(_stop_subprocess(None, ...)이 조용히 무해한 dict를 반환)을 그대로
    # 보존한다 - do_cleanup()이 이 default를 무조건 호출해도 안전하다.
    detector_ref = {"d": DetectorHandle(is_alive=lambda: False,
                                         stop=lambda: _stop_subprocess(None, "score_server.py"),
                                         label="detector(시작 전)")}

    def do_cleanup():
        if cleanup_state["done"]:
            return
        cleanup_state["done"] = True
        outcome["observation_ended_at_utc"] = datetime.now(timezone.utc).isoformat()
        try:
            prober.check_slo_violation()  # 반환값 무시 - pod 삭제 전 마지막으로 캐시를 최대한 새로 고침
        except Exception as e:
            print(f"[정리] 정리 직전 최종 probe 데이터 갱신 실패(계속 진행): {e}")
        # §190 - gap 진단·무효 판정은 _finalize_trial_outcome()으로 옮겨 do_cleanup()
        # 맨 끝(prober.stop()/injector.cleanup() 이후)에서 한 번에 계산한다 - 위
        # check_slo_violation()이 이미 로컬 CSV 캐시를 최신화해뒀으므로, 여기서
        # 그 결과를 바로 읽어도 pod 삭제 전과 동일한 데이터를 얻는다(순서상
        # 뒤로 옮겨도 값 손실 없음). 진단 파싱이 실패해도 위 정리 호출들을
        # 막지 않도록 분리한다.
        # §187 - 실제 적용된 CPU stage 기록(주입기 자신의 기존 get_stage_windows(),
        # 새 계측 없음) - 정리 전에 남겨야 target 교체/cleanup으로 사라지기 전 값을 잡는다.
        try:
            outcome["stage_windows"] = injector.get_stage_windows()
        except Exception as e:
            print(f"[정리] stage_windows 조회 실패(계속 진행): {e}")
        outcome["score_server_exit"] = detector_ref["d"].stop()
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
        try:
            _finalize_trial_outcome(outcome, probe_raw_csv_path, outcome["observation_ended_at_utc"])
        except Exception as e:
            print(f"[정리] 결과 유효성 최종 판정 실패(계속 진행): {e}")

    baseline_state = {"active_restart_count": None}
    t_entry = monotonic_fn()
    try:
        injector.prepare()
        prober.start()
        if not _wait_for(prober.is_alive, probe_ready_timeout_sec, 1.0):
            raise TrialInvalid("probe가 시작 후 살아있음을 확인 못함")

        # §184 정정 - 공식 run_once.py 순서(요청 부하 시작 -> SLO 기준선 안정 확인 -> IF
        # 시작 -> 주입)를 그대로 재현한다(run_once.py:711/731/745-746 확인됨). 이전
        # 버전은 이 대기 전에 score_server.py를 먼저 띄워, 공식 배포가 절대 노출 안
        # 시키는 요청 램프업 과도 구간(§183 rep1에서 cpu_mean이 훈련 데이터 최솟값보다도
        # 낮았음 - 확인됨)을 그대로 노출시켰다. 이 대기 중엔 탐지기가 아직 없으므로
        # 그 존재를 확인하는 분기 없이 하네스 안전 감시만 한다.
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
                raise TrialInvalid(f"주입 전 SLO 기준선 대기 상한({baseline_timeout_sec:.0f}s) 초과")
            sleep_fn(1.0)

        detector_ref["d"] = detector_start_fn(run_id, evidence_log_path)
        if not detector_ref["d"].is_alive():
            raise HarnessCorrupted(f"{detector_ref['d'].label}가 기동 직후 종료됨")
        print(f"{detector_ref['d'].label} 기동 확인(SLO 기준선 안정 확인 후), "
              f"실제 recovery-policy({REAL_RECOVERY_POLICY_SIGNAL_URL})에 연결됨")

        t_injection_request = datetime.now(timezone.utc).isoformat()
        outcome["t_injection_requested_utc"] = t_injection_request
        injector.inject()
        started = _wait_for(injector.is_started, injection_started_timeout_sec, 1.0)
        injection_valid = started and injector.is_effective()
        outcome["injection_valid"] = injection_valid
        if not injection_valid:
            raise TrialInvalid(f"주입 시작 확인 실패({injection_started_timeout_sec:.0f}s 이내)")
        t_injection_observed = None
        if injector.get_actual_injection_time is not None:
            t_injection_observed = injector.get_actual_injection_time()
        outcome["t_injection_observed_utc"] = t_injection_observed or t_injection_request

        observation_deadline = monotonic_fn() + post_injection_observation_sec
        last_safety_check = 0.0
        while monotonic_fn() < observation_deadline:
            now = monotonic_fn()
            if now - last_safety_check >= safety_poll_interval_sec:
                check_safety_fn(baseline_state, prober)
                # §185 - 공식 run_once.py의 OBSERVING 루프(run_once.py:824)와 동일하게 매
                # 반복 다시 부른다 - 반환값은 안 본다(§182 철학: 실제 압박 중 SLO 위반은
                # 예상된 결과, 중단 사유 아님), 이 호출의 실제 역할은 load_ramp_adapter.py
                # 내부 raw CSV 캐시를 계속 새로 고치는 것(§183 rep2에서 이걸 한 번도 안
                # 불러 캐시가 멈췄던 문제의 재발 방지). _refresh()가 연속 실패 상한을
                # 넘기면 TrialInvalid를 던지는데, 그건 여기서 삼키지 않고 그대로 전파해
                # 기존 except 절이 trial을 정상적으로 무효 처리하게 둔다.
                prober.check_slo_violation()
                if not detector_ref["d"].is_alive():
                    raise HarnessCorrupted(f"{detector_ref['d'].label}가 관찰 중 예상 밖 종료됨")
                last_safety_check = now
            sleep_fn(1.0)
        outcome["status"] = "completed"
        outcome["reason"] = f"관찰 {post_injection_observation_sec:.0f}초 완료"
    except (TrialInvalid, HarnessCorrupted) as e:
        outcome["status"] = "aborted"
        outcome["reason"] = f"{type(e).__name__}: {e}"
    finally:
        do_cleanup()
        outcome["elapsed_sec"] = monotonic_fn() - t_entry
    return outcome


def run_no_switch_control_trial(
    run_id: str, injector, prober, sink_port: int, evidence_log_path: Path, sink_out_path: Path,
    probe_raw_csv_path: Path = None,
    baseline_timeout_sec: float = BASELINE_TIMEOUT_SEC,
    post_injection_observation_sec: float = POST_INJECTION_OBSERVATION_SEC,
    injection_started_timeout_sec: float = INJECTION_STARTED_TIMEOUT_SEC,
    probe_ready_timeout_sec: float = PROBE_READY_TIMEOUT_SEC,
    safety_poll_interval_sec: float = SAFETY_POLL_INTERVAL_SEC,
    check_safety_fn=_check_safety_with_injection,
    detector_start_fn=_start_candidate_score_server_detector_for_sink,
    sleep_fn=time.sleep, monotonic_fn=time.monotonic,
    detector_ready_fn=None, detector_ready_timeout_sec: float = None,
) -> dict:
    """2026-10-05 - detector_ready_fn(선택, 기본 None = 기존 동작 그대로): 주면 탐지기 기동 뒤 _wait_detector_ready로
    준비를 확인한 뒤에만 압박을 주입한다(시간 초과·기록 오류·종료면 주입 없이 기존 정리 경로).

    §186(2026-09-28) - "전환 없음" 대조 관찰. rep=3·4·5(run_cpu_pressure_
    trial())와 완전히 동일하게 preview 준비 -> SLO 기준선 안정 확인 -> 실제
    CPU 압박 주입(DEMO_STAGES, 동일 강도)까지 그대로 재현하되, 딱 하나만
    다르다 - score_server.py(후보 threshold, 동일)의 RECOVERY_POLICY_
    SIGNAL_URL을 실제 recovery-policy 대신 capture_sink.py(run_no_injection_
    smoke()·§86 no-action smoke가 이미 쓰는 것 그대로 재사용)로 돌려, IF의
    평가·판정 자체는 evidence-log에 그대로 기록되면서도 실제 /signal 전송·
    정책 결정·승격은 구조적으로 불가능하게 만든다. 이렇게 해야 "판정 로직은
    완전히 동일한데 승격만 없었다"는 순수한 대조가 성립한다(예: 다른 임계값·
    다른 안전검사를 쓰면 그 차이가 결과에 섞여 대조가 아니게 된다).

    안전 감시는 run_cpu_pressure_trial()과 동일(§182 철학 - 하네스 이상만
    본다, 실제 압박 결과인 OOMKilled·재시작·SLO 위반은 중단 사유 아님).
    주입 장치(cpu_pressure_demo_adapter.py) 자체의 기존 안전 중단 조건
    (OOMKilled·재시작·Node 이상 시 stage 중단)은 이 함수가 손대지 않는
    _run_stages() 내부 로직이라 항상 그대로 적용된다. probe raw CSV
    수집 공백 방지(§185)도 동일하게 적용한다.

    §189 - detector_start_fn(run_id, evidence_log_path, sink_url)로 공식
    score_server.py 대신 실험용 gated_signal_detector.py를 쓸 수 있다
    (run_no_injection_smoke()와 동일 원칙). 기본값은 기존과 100% 동일한
    공식 score_server.py 경로 - 이 함수는 항상 sink에만 연결하므로(§186
    설계 그대로) 어느 탐지기를 쓰든 실제 정책 URL은 등장하지 않는다."""
    cleanup_state = {"done": False}
    outcome = {"status": None, "reason": None, "elapsed_sec": None,
               "prober_stop_failed": None, "injector_cleanup_failed": None,
               "sink_exit": None, "score_server_exit": None,
               "injection_valid": None, "t_injection_requested_utc": None, "t_injection_observed_utc": None,
               "observation_ended_at_utc": None, "probe_last_request_utc": None,
               "probe_data_gap_sec": None, "probe_data_gap_detected": True,
               "probe_max_internal_gap_sec": None, "cleanup_status": "incomplete",
               "invalidity_reasons": None}
    sink_handle = {"h": None}
    detector_ref = {"d": DetectorHandle(is_alive=lambda: False,
                                         stop=lambda: _stop_subprocess(None, "score_server.py"),
                                         label="detector(시작 전)")}

    def do_cleanup():
        if cleanup_state["done"]:
            return
        cleanup_state["done"] = True
        outcome["observation_ended_at_utc"] = datetime.now(timezone.utc).isoformat()
        try:
            prober.check_slo_violation()
        except Exception as e:
            print(f"[정리] 정리 직전 최종 probe 데이터 갱신 실패(계속 진행): {e}")
        # §190 - gap 진단·무효 판정은 _finalize_trial_outcome()으로 옮겨 do_cleanup()
        # 맨 끝(prober.stop()/injector.cleanup() 이후)에서 한 번에 계산한다(run_cpu_
        # pressure_trial()과 동일 이유 - 위 최종 갱신이 이미 로컬 CSV를 새로고쳤으므로
        # 순서를 옮겨도 값 손실 없음, 진단 파싱 실패가 정리 호출을 막지 않게 분리).
        outcome["score_server_exit"] = detector_ref["d"].stop()
        outcome["sink_exit"] = _stop_subprocess(sink_handle["h"], "capture_sink.py")
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
        try:
            _finalize_trial_outcome(outcome, probe_raw_csv_path, outcome["observation_ended_at_utc"])
        except Exception as e:
            print(f"[정리] 결과 유효성 최종 판정 실패(계속 진행): {e}")

    baseline_state = {"active_restart_count": None}
    t_entry = monotonic_fn()
    try:
        injector.prepare()
        prober.start()
        if not _wait_for(prober.is_alive, probe_ready_timeout_sec, 1.0):
            raise TrialInvalid("probe가 시작 후 살아있음을 확인 못함")

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
                raise TrialInvalid(f"주입 전 SLO 기준선 대기 상한({baseline_timeout_sec:.0f}s) 초과")
            sleep_fn(1.0)

        sink_url = f"http://127.0.0.1:{sink_port}/signal"
        sink_handle["h"] = _start_subprocess(
            [sys.executable, "-u", str(MODEL_V32B_DIR / "capture_sink.py"),
             "--port", str(sink_port), "--out", str(sink_out_path), "--run-id", run_id],
            cwd=MODEL_V32B_DIR,
            stdout_path=DEMO_RESULTS_DIR / f"sink-stdout-{run_id}.log",
            stderr_path=DEMO_RESULTS_DIR / f"sink-stderr-{run_id}.log")
        if not sink_handle["h"]["alive_after_start"] or not _wait_http_ok(f"http://127.0.0.1:{sink_port}/healthz"):
            raise HarnessCorrupted("capture_sink.py가 정상 기동하지 않음 - fail-closed(정책 오염 위험 회피)")

        detector_ref["d"] = detector_start_fn(run_id, evidence_log_path, sink_url)
        if not detector_ref["d"].is_alive():
            raise HarnessCorrupted(f"{detector_ref['d'].label}가 기동 직후 종료됨")
        print(f"{detector_ref['d'].label} 기동 확인(SLO 기준선 안정 확인 후), "
              f"sink={sink_url}로만 연결(실제 정책·승격 구조적으로 불가능)")
        if detector_ready_fn is not None:  # 2026-10-05 c1 섀도 pilot - 첫 정상 계산 기록 확인 뒤에만 주입
            _wait_detector_ready(outcome, detector_ref["d"], sink_handle["h"], detector_ready_fn, detector_ready_timeout_sec,
                                 check_safety_fn, baseline_state, prober, safety_poll_interval_sec, monotonic_fn, sleep_fn)

        t_injection_request = datetime.now(timezone.utc).isoformat()
        outcome["t_injection_requested_utc"] = t_injection_request
        injector.inject()
        started = _wait_for(injector.is_started, injection_started_timeout_sec, 1.0)
        injection_valid = started and injector.is_effective()
        outcome["injection_valid"] = injection_valid
        if not injection_valid:
            raise TrialInvalid(f"주입 시작 확인 실패({injection_started_timeout_sec:.0f}s 이내)")
        t_injection_observed = None
        if injector.get_actual_injection_time is not None:
            t_injection_observed = injector.get_actual_injection_time()
        outcome["t_injection_observed_utc"] = t_injection_observed or t_injection_request

        observation_deadline = monotonic_fn() + post_injection_observation_sec
        last_safety_check = 0.0
        while monotonic_fn() < observation_deadline:
            now = monotonic_fn()
            if now - last_safety_check >= safety_poll_interval_sec:
                check_safety_fn(baseline_state, prober)
                prober.check_slo_violation()  # §185와 동일 - 반환값 무시, probe CSV 캐시만 계속 새로 고침
                if not detector_ref["d"].is_alive():
                    raise HarnessCorrupted(f"{detector_ref['d'].label}가 관찰 중 예상 밖 종료됨")
                if sink_handle["h"]["proc"].poll() is not None:
                    raise HarnessCorrupted("capture_sink.py가 관찰 중 예상 밖 종료됨 - 신호 유실 위험")
                last_safety_check = now
            sleep_fn(1.0)
        outcome["status"] = "completed"
        outcome["reason"] = f"전환 없음 대조 관찰 {post_injection_observation_sec:.0f}초 완료"
    except (TrialInvalid, HarnessCorrupted) as e:
        outcome["status"] = "aborted"
        outcome["reason"] = f"{type(e).__name__}: {e}"
    finally:
        do_cleanup()
        outcome["elapsed_sec"] = monotonic_fn() - t_entry
    return outcome


def main():
    parser = argparse.ArgumentParser(description="§183 - 공식 IF 임계값 후보(-0.04) 검증")
    parser.add_argument("--mode", required=True,
                         choices=["no_injection", "cpu_pressure", "no_switch_control", "fixed_threshold_pressure"])
    parser.add_argument("--rep", type=int, default=1)
    parser.add_argument("--probe-config", default=str(DEFAULT_PROBE_CONFIG))
    parser.add_argument("--probe-image", choices=[LR_IMAGE, IMAGE_FORCE_CLOSE], default=LR_IMAGE,
                        help="2026-10-02 - 기본값은 과거 실행과 같은 이미지. V1 새 실행은 force_close 이미지를 명시")
    parser.add_argument("--detector", choices=["candidate", "gated"], default="candidate",
                         help="§189 - --mode no_switch_control 전용. candidate=공식 score_server.py(기존 "
                              "기본값) / gated=mem_slope>0 게이트 실험용(사후선택). 다른 mode에서는 무시됨 "
                              "(cpu_pressure/fixed_threshold_pressure는 각자의 기존 탐지기 선택 로직을 그대로 씀).")
    parser.add_argument("--allow-real-promotion-diagnostic-exception", action="store_true",
                         help="§202 - --mode cpu_pressure/fixed_threshold_pressure 전용. 필수 명시 시에만 "
                              "워커 2개 상황의 표준 fail-closed 배치 게이트 대신 목적지 사전예측+사후검증 "
                              "예외를 쓴다(§200/§201과 동일 메커니즘). 기본(미지정)이면 두 모드 모두 이제 "
                              "표준 wrap_injector_with_node_resource_gate(다른 5개 CPU 주입 러너와 동일)로 "
                              "막힌다 - 이 파일만 그 보호가 없었던 것을 §202에서 맞춘다.")
    parser.add_argument("--dry-run-contract-only", action="store_true")
    args = parser.parse_args()

    is_fixed_threshold = args.mode == "fixed_threshold_pressure"
    use_gated_detector = args.mode == "no_switch_control" and args.detector == "gated"
    plan_id = FIXED_THRESHOLD_PLAN_ID if is_fixed_threshold else PLAN_ID
    arm_label = "fixed_threshold" if is_fixed_threshold else "proposed"
    scenario = "fixed_threshold_comparison" if is_fixed_threshold else f"if_threshold_candidate_{args.mode}"
    run_id_suffix = "-gated" if use_gated_detector else ""
    run_id = f"{scenario}-{arm_label}{run_id_suffix}-{args.rep:02d}-{plan_id}"
    evidence_log_path = DEMO_RESULTS_DIR / f"detector-evidence-{run_id}.jsonl"
    summary_path = DEMO_RESULTS_DIR / f"summary-{run_id}.json"

    DEMO_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    if summary_path.exists():
        raise SystemExit(f"run_id({run_id})의 결과가 이미 존재함 - 중단: {summary_path}")

    contract = {
        "run_id": run_id, "plan_id": plan_id, "mode": args.mode, "probe_image": args.probe_image,
        "detector": "gated" if use_gated_detector else arm_label,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "official_threshold": OFFICIAL_THRESHOLD, "candidate_threshold": CANDIDATE_THRESHOLD,
        "official_artifacts_dir": str(OFFICIAL_ARTIFACTS_DIR), "candidate_artifacts_dir": str(CANDIDATE_ARTIFACTS_DIR),
        "model_features_consecutive_rule_unchanged": True,
        "cpu_stage_intensity_unchanged": DEMO_STAGES if args.mode in ("cpu_pressure", "no_switch_control", "fixed_threshold_pressure") else None,
        "execution_order": "§184 정정 - 요청 부하 시작 -> SLO 기준선 안정 확인 -> 탐지기 시작"
                           + (" -> 주입" if args.mode in ("cpu_pressure", "no_switch_control", "fixed_threshold_pressure") else ""),
        "supersedes_invalid_result_note": (
            "§183 rep1(동일 PLAN_ID, 다른 rep)은 이 정정 전 시퀀싱 버그로 무효 - "
            "그 결과는 후보 임계값의 오탐 증거로 재사용하지 않음(보존만 함)"
        ) if not is_fixed_threshold else None,
        "policy_connection": (
            "capture_sink.py(promotion/전달 코드 없음)로만 연결 - 실제 정책·승격 구조적으로 불가능"
            if args.mode in ("no_injection", "no_switch_control") else
            "실제 recovery-policy(localhost:8080)에 연결 - 실제 판정·전환 가능"
        ),
        "no_switch_control_note": (
            "§186 - rep=3·4·5(run_cpu_pressure_trial)와 preview·SLO기준선·CPU압박강도·요청부하·"
            "실행순서·후보threshold 전부 동일, IF 신호가 capture_sink로만 가서 실제 승격이 구조적으로 "
            "불가능한 점만 다른 대조 관찰"
            + (" §189 - detector=gated(mem_slope>0 게이트 실험용, 사후선택·아직 안전장치 아님)로 실행 - "
               "sink 전용이라 이 경우도 실제 정책·승격 구조적으로 불가능." if use_gated_detector else "")
        ) if args.mode == "no_switch_control" else None,
        "fixed_threshold_comparison_note": (
            "§187 - 후보 IF(rep=3·4·5, run_cpu_pressure_trial)와 preview 준비·SLO기준선·CPU압박강도·"
            "요청부하·관찰시간·안전/정리 절차 전부 동일, 탐지기만 arm_controller의 기존 fixed_threshold "
            "명령 생성 경로(make_detector_for_arm)로 교체 - 실제 recovery-policy 연결, 실제 자동 전환 가능. "
            "결과는 후보 IF 원자료와 다른 plan_id/run_id/파일에 기록된다(섞이지 않음)."
        ) if is_fixed_threshold else None,
        "fixed_threshold_cpu_limit_cores": arm_controller.FIXED_THRESHOLD_CPU_LIMIT_CORES if is_fixed_threshold else None,
        "fixed_threshold_applied_threshold_cores": (
            arm_controller.FIXED_THRESHOLD_CPU_LIMIT_CORES * 0.90) if is_fixed_threshold else None,
        "cpu_aggregation_note": CPU_AGGREGATION_NOTE if is_fixed_threshold else None,
        "real_promotion_diagnostic_exception_requested": (
            args.allow_real_promotion_diagnostic_exception
            if args.mode in ("cpu_pressure", "fixed_threshold_pressure") else None
        ),
        "two_worker_environment_note": (
            "§202(2026-10-01) - sj-worker2 합류 이후 2-워커 환경. 기존 rep=01~05(단일 워커, "
            "2026-09-27/28)와 환경이 달라 별도 계열 - 전환 성공 횟수를 합산하지 않는다."
        ) if (args.mode in ("cpu_pressure", "fixed_threshold_pressure")
              and args.allow_real_promotion_diagnostic_exception) else None,
    }
    contract_path = DEMO_RESULTS_DIR / f"contract-{run_id}.json"
    contract_path.write_text(json.dumps(contract, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"계약 기록: {contract_path}")
    if args.dry_run_contract_only:
        print("dry-run - 클러스터 미접속, 여기서 종료")
        return

    injector = make_cpu_pressure_injector(run_id, arm_label, args.rep, stages=DEMO_STAGES)
    injector = arm_controller.wrap_injector_with_preview_prep(injector, arm_label)

    real_promotion_diagnostic_used = False
    if args.mode in ("cpu_pressure", "fixed_threshold_pressure"):
        if args.allow_real_promotion_diagnostic_exception:
            real_promotion_diagnostic_used = True
            print("[§202] 실제 승격 동반 CPU 진단 예외 활성화 - 워커 2개 상황의 표준 배치 "
                  "안전 차단을 이번 1회 실행에 한해 의도적으로 우회함")
            injector = node_resource_safety_gate.wrap_injector_with_real_promotion_diagnostic_exception(
                injector, arm_label, node_resource_safety_gate.REAL_PROMOTION_DIAGNOSTIC_EXPECTED_RUN_MODE)
        else:
            # §202 - 다른 5개 CPU 주입 러너와 동일한 표준 fail-closed 게이트로 맞춘다(§197).
            # 이 두 모드만 지금까지 이 게이트가 아예 없었다 - 판정 임계값은 전혀 안 바꾼다.
            injector = node_resource_safety_gate.wrap_injector_with_node_resource_gate(injector, arm_label)

    events_before_utc = datetime.now(timezone.utc).isoformat() if is_fixed_threshold else None

    if args.mode == "no_injection":
        prober = make_load_ramp_prober(args.probe_config, run_id, scenario, "proposed", args.rep,
                                       PROBE_LIFETIME_NO_INJECTION_SEC, image=args.probe_image)
        probe_raw_csv_path = PROBE_RESULTS_DIR / f"probe-{run_id}-proposed-{args.rep}-raw.csv"
        sink_port = _pick_free_loopback_port()
        sink_out_path = DEMO_RESULTS_DIR / f"sink-captured-{run_id}.jsonl"
        print(f"run_id: {run_id} / mode=no_injection / 관찰: {NO_INJECTION_OBSERVATION_SEC:.0f}s / sink_port={sink_port}")
        outcome = run_no_injection_smoke(run_id, injector, prober, sink_port, evidence_log_path, sink_out_path,
                                          probe_raw_csv_path=probe_raw_csv_path)
    elif args.mode == "no_switch_control":
        prober = make_load_ramp_prober(args.probe_config, run_id, scenario, "proposed", args.rep,
                                       PROBE_LIFETIME_CPU_PRESSURE_SEC, image=args.probe_image)
        probe_raw_csv_path = PROBE_RESULTS_DIR / f"probe-{run_id}-proposed-{args.rep}-raw.csv"
        sink_port = _pick_free_loopback_port()
        sink_out_path = DEMO_RESULTS_DIR / f"sink-captured-{run_id}.jsonl"
        no_switch_detector_start_fn = (_start_gated_score_server_detector_for_sink if use_gated_detector
                                        else _start_candidate_score_server_detector_for_sink)
        print(f"run_id: {run_id} / mode=no_switch_control / detector={args.detector} / "
              f"후보 threshold={CANDIDATE_THRESHOLD}(신호는 sink로만) / "
              f"주입전 상한: {BASELINE_TIMEOUT_SEC:.0f}s / 관찰: {POST_INJECTION_OBSERVATION_SEC:.0f}s / sink_port={sink_port}")
        outcome = run_no_switch_control_trial(run_id, injector, prober, sink_port, evidence_log_path, sink_out_path,
                                              probe_raw_csv_path, detector_start_fn=no_switch_detector_start_fn)
    else:  # cpu_pressure(후보 IF) 또는 fixed_threshold_pressure(§187) - run_cpu_pressure_trial() 공통 흐름 재사용
        prober = make_load_ramp_prober(args.probe_config, run_id, scenario, "proposed", args.rep,
                                       PROBE_LIFETIME_CPU_PRESSURE_SEC, image=args.probe_image)
        probe_raw_csv_path = PROBE_RESULTS_DIR / f"probe-{run_id}-proposed-{args.rep}-raw.csv"
        detector_start_fn = _start_fixed_threshold_detector if is_fixed_threshold else _start_candidate_score_server_detector
        label = (f"fixed_threshold(cpu_limit_cores={arm_controller.FIXED_THRESHOLD_CPU_LIMIT_CORES})"
                 if is_fixed_threshold else f"후보 threshold={CANDIDATE_THRESHOLD}")
        print(f"run_id: {run_id} / mode={args.mode} / {label} / "
              f"주입전 상한: {BASELINE_TIMEOUT_SEC:.0f}s / 관찰: {POST_INJECTION_OBSERVATION_SEC:.0f}s")
        # §202 - t_switch(감사 로그가 확인한 실제 전환 시각, rollouts_client.promote()의
        # verified_at 원천)·t_decision·action·decision_outcome은 recovery-policy가
        # experiment-run context가 등록돼 있을 때만 기록한다(main.py:_signal_belongs_to_
        # current_experiment 확인). run_cpu_pressure_trial() 자체는 이 등록을 안 하므로
        # (기존 rep=3~5 호출부와 동일하게 변경 없음), 이 함수를 감싸는 형태로만 추가한다 -
        # run_cpu_pressure_trial()과 그 기존 안전 중단 조건은 한 줄도 안 바꾼다.
        if real_promotion_diagnostic_used:
            _register_experiment_context(run_id, scenario, arm_label, args.rep,
                                          datetime.now(timezone.utc).isoformat())
        outcome = None
        try:
            outcome = run_cpu_pressure_trial(run_id, injector, prober, evidence_log_path, probe_raw_csv_path,
                                              detector_start_fn=detector_start_fn)
        finally:
            if real_promotion_diagnostic_used:
                if outcome is not None:
                    try:
                        outcome["experiment_run_audit_timing"] = _get_experiment_state(arm_label)
                    except Exception as e:
                        print(f"[§202] experiment-run timing 조회 실패(계속 진행, 신호·게이트 자체 결과에는 "
                              f"영향 없음): {e}")
                        outcome.setdefault("experiment_run_audit_timing", None)
                # context 정리는 outcome 존재 여부와 무관하게 항상 시도한다(run_cpu_pressure_trial()
                # 자체가 예상 밖으로 예외를 내더라도 등록된 experiment-run context를 남겨두지 않기 위함).
                try:
                    _clear_experiment_context(run_id, arm_label)
                except Exception as e:
                    print(f"[§202] experiment-run context 정리 실패: {e}")

    evaluations = _load_evidence_scores(evidence_log_path)

    summary = {
        "run_id": run_id, "mode": args.mode, "detector": arm_label,
        "outcome_status": outcome["status"], "outcome_reason": outcome["reason"],
        "elapsed_sec": outcome["elapsed_sec"],
        "prober_stop_failed": outcome["prober_stop_failed"], "injector_cleanup_failed": outcome["injector_cleanup_failed"],
        "n_evaluations": len(evaluations),
        "injection_valid": outcome.get("injection_valid"),
        "t_injection_requested_utc": outcome.get("t_injection_requested_utc"),
        "t_injection_observed_utc": outcome.get("t_injection_observed_utc"),
        "observation_ended_at_utc": outcome.get("observation_ended_at_utc"),
        "probe_last_request_utc": outcome.get("probe_last_request_utc"),
        "probe_data_gap_sec": outcome.get("probe_data_gap_sec"),
        "probe_data_gap_detected": outcome.get("probe_data_gap_detected"),
        "probe_max_internal_gap_sec": outcome.get("probe_max_internal_gap_sec"),
        "cleanup_status": outcome.get("cleanup_status"),
        "invalidity_reasons": outcome.get("invalidity_reasons"),
        "stage_windows": outcome.get("stage_windows"),
        # §185 - probe_data_gap_detected가 True이거나 trial이 무효면, 이 결과를
        # "첫 느린 요청 대비 선제 탐지 여부"를 판정하는 근거로 쓰면 안 된다
        # (§183 rep2가 정확히 이 상태였는데도 그 사실이 결과에 안 남아있었다).
        "preemption_timing_evaluable": (
            outcome.get("status") == "completed" and outcome.get("probe_data_gap_detected") is False
        ) if args.mode in ("cpu_pressure", "no_switch_control", "fixed_threshold_pressure") else None,
        "real_promotion_diagnostic_exception_used": real_promotion_diagnostic_used,
        # §202 - t_switch(promote()의 verified_at 원천, 실제 전환을 "처음 확인한" 서버 시각)를
        # 포함한 recovery-policy 자신의 감사 타이밍 기록 - run_cpu_pressure_trial()이 보는
        # "신호 전송 성공(http 200)"과는 별개의, 더 권위 있는 소스(main.py:_record_switch).
        "experiment_run_audit_timing": outcome.get("experiment_run_audit_timing"),
        "node_placement_info": (
            getattr(injector, "get_real_promotion_diagnostic_exception_info", lambda: None)()
            if real_promotion_diagnostic_used else None
        ),
        "two_worker_environment_note": (
            "§202(2026-10-01) - sj-worker2 합류 이후 2-워커 환경에서 실행됨. 기존 "
            "rep=01~05(단일 워커, capstone-if-threshold-candidate-183-v1, 2026-09-27/28)와는 "
            "환경이 달라 결과를 같은 계열로 합산하지 않는다 - rep 번호·plan_id는 동일 규칙을 "
            "재사용하되, 집계 시 이 필드로 구분할 것."
        ) if real_promotion_diagnostic_used else None,
    }

    if is_fixed_threshold:
        # §187 - fixed_threshold 평가 레코드엔 score/threshold(IF 고유값)가 없다 -
        # _advance_streak_offline() 재적용은 억지로 하지 않고, 자기 자신의
        # would_signal 기록만 그대로 읽는다(지시: "입력 집계 범위나 시각 의미가
        # 맞지 않으면 억지로 비교하지 말고 한계를 설명").
        first_signal = next((e for e in evaluations if e.get("would_signal")), None)
        summary["fixed_threshold_signal"] = {
            "would_signal_ever": first_signal is not None,
            "first_signal_window_end_utc": first_signal.get("window_end_utc") if first_signal else None,
            "first_signal_cpu_mean": first_signal.get("cpu_mean") if first_signal else None,
            "first_signal_evaluation_seq": first_signal.get("evaluation_seq") if first_signal else None,
            "threshold_cores_applied": arm_controller.FIXED_THRESHOLD_CPU_LIMIT_CORES * 0.90,
        }
        summary["cpu_aggregation_note"] = CPU_AGGREGATION_NOTE
        # §187 - verified_at은 recovery-policy가 이미 계산해 둔 값을 그대로 회수한다
        # (재계산 없음, 지시 그대로) - 신호가 한 번이라도 갔을 때만 조회한다.
        if first_signal is not None:
            audit = _fetch_audit_verified_at(run_id)
            summary["audit_verified_at"] = audit
        else:
            summary["audit_verified_at"] = {"fetch_error": "would_signal 기록 없음 - 조회 생략", "records": []}
        # §187 - 구 Pod 종료 이벤트·Pod 식별자 보존(읽기 전용, 해석 안 함).
        events_path = DEMO_RESULTS_DIR / f"pod-events-{run_id}.json"
        events = _capture_recent_pod_events(since_utc=events_before_utc)
        events_path.write_text(json.dumps(events, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        summary["pod_events_file"] = str(events_path)
        summary["pod_events_note"] = (
            "이벤트 시각만으로 요청의 연결 대상이나 실패 원인을 확정하지 않는다 - 참고용 보존만."
        )
    else:
        scores = [e["score"] for e in evaluations]
        summary["official_threshold_offline_replay"] = _advance_streak_offline(scores, OFFICIAL_THRESHOLD)
        summary["candidate_threshold_offline_replay"] = _advance_streak_offline(scores, CANDIDATE_THRESHOLD)

    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"outcome: {outcome['status']} ({outcome['reason']})")
    if is_fixed_threshold:
        fts = summary["fixed_threshold_signal"]
        print(f"평가 수={len(evaluations)} / fixed_threshold 발화={fts['would_signal_ever']}"
              f"(t={fts['first_signal_window_end_utc']}, cpu_mean={fts['first_signal_cpu_mean']})")
        print(f"audit verified_at 조회: {summary['audit_verified_at']}")
    else:
        official_sim = summary["official_threshold_offline_replay"]
        candidate_sim = summary["candidate_threshold_offline_replay"]
        print(f"평가 수={len(evaluations)} / 공식임계값 재적용 발화={official_sim['fired']}"
              f"(idx={official_sim['fire_index']}) / 후보임계값 재적용 발화={candidate_sim['fired']}(idx={candidate_sim['fire_index']})")
    if args.mode in ("cpu_pressure", "no_switch_control", "fixed_threshold_pressure"):
        print(f"probe 데이터 공백 진단: last_request={summary['probe_last_request_utc']} / "
              f"observation_ended={summary['observation_ended_at_utc']} / gap_sec={summary['probe_data_gap_sec']} / "
              f"gap_detected={summary['probe_data_gap_detected']} / "
              f"preemption_timing_evaluable={summary['preemption_timing_evaluable']}")
    print(f"결과 파일: {summary_path}")


if __name__ == "__main__":
    main()
