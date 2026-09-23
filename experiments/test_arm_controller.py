#!/usr/bin/env python3
"""arm_controller.py 검증(2026-09-18 추가, arm orchestration 보완) - 실제
fixed_threshold.py/score_server.py(Prometheus·모델 파일 의존)나 실클러스터
없이 오프라인으로 돈다. 서브프로세스 생명주기 자체는 _subprocess_detector()에
trivial한 python -c 커맨드를 직접 넣어 검증하고, preview 준비는
prepare_preview_fn을, RECOVERY_POLICY_SIGNAL_URL reachability(2026-09-19
추가)는 reachability_check_fn을 가짜로 주입해 검증한다."""
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.stdout.reconfigure(encoding="utf-8")

import arm_controller
from arm_controller import (
    _DETECTOR_SCRIPTS,
    FIXED_THRESHOLD_CPU_LIMIT_CORES,
    PROPOSED_ARTIFACTS_DIR,
    PROPOSED_MODEL_VERSION,
    _build_detector_command,
    _prometheus_reachable_and_fresh,
    _resolved_signal_url,
    _stop_file_for,
    _subprocess_detector,
    make_detector_for_arm,
    wrap_injector_with_preview_prep,
)
from run_once import HarnessCorrupted, Injector, TrialInvalid


def test_make_detector_for_arm_native_returns_none():
    assert make_detector_for_arm("native", "run-1") is None
    print("OK - native는 detector가 없음(None)")


def test_make_detector_for_arm_fixed_threshold_dispatches_correct_script():
    cmd = _build_detector_command("fixed_threshold", "run-1")
    assert cmd is not None
    # cmd[-3]은 --cpu-limit-cores 추가(2026-09-20) 이후 인자 개수가 arm마다
    # 달라져 더 이상 안전하지 않다 - 스크립트 경로는 항상 cmd[1](sys.executable
    # 다음 위치)에 고정이다.
    assert cmd[1].endswith("fixed_threshold.py"), cmd
    assert "score_server.py" not in " ".join(cmd), "fixed_threshold arm인데 score_server.py가 섞이면 안 됨"
    detector = make_detector_for_arm("fixed_threshold", "run-1")
    assert detector.name == "fixed_threshold"
    print("OK - fixed_threshold arm은 fixed_threshold.py + detector.name='fixed_threshold'만 반환")


def test_make_detector_for_arm_proposed_dispatches_correct_script():
    cmd = _build_detector_command("proposed", "run-1")
    assert cmd is not None
    assert cmd[1].endswith("score_server.py"), cmd
    assert "fixed_threshold.py" not in " ".join(cmd), "proposed arm인데 fixed_threshold.py가 섞이면 안 됨"
    detector = make_detector_for_arm("proposed", "run-1")
    assert detector.name == "isolation_forest"
    print("OK - proposed arm은 score_server.py + detector.name='isolation_forest'만 반환")


def test_fixed_threshold_command_carries_frozen_cpu_limit():
    # 계약서 §6 동결값(2026-09-20 정정) - arm_controller가 fixed_threshold.py에게
    # CPU limit을 암묵적 기본값 없이 명시적으로 전달해야 한다.
    cmd = _build_detector_command("fixed_threshold", "run-1")
    assert "--cpu-limit-cores" in cmd, cmd
    idx = cmd.index("--cpu-limit-cores")
    assert float(cmd[idx + 1]) == FIXED_THRESHOLD_CPU_LIMIT_CORES == 3.0, cmd
    print("OK - fixed_threshold detector 커맨드에 동결값 3.0이 --cpu-limit-cores로 명시 전달됨")


def test_proposed_command_has_no_cpu_limit_arg():
    # score_server.py(Isolation Forest)는 CPU 임계치 개념이 없다 - 엉뚱하게
    # 섞여 들어가면 안 된다.
    cmd = _build_detector_command("proposed", "run-1")
    assert "--cpu-limit-cores" not in cmd, cmd
    print("OK - proposed(score_server.py) 커맨드에는 --cpu-limit-cores가 없음")


def test_proposed_command_carries_v32b_artifacts_dir_and_model_version():
    # §86 - proposed arm만 v3.2b 동결 artifact 경로·버전을 명시적으로
    # 전달해야 한다(score_server.py의 --artifacts-dir/--model-version은
    # 기본값 없음, fail-closed).
    cmd = _build_detector_command("proposed", "run-1")
    assert "--artifacts-dir" in cmd, cmd
    idx = cmd.index("--artifacts-dir")
    assert cmd[idx + 1] == str(PROPOSED_ARTIFACTS_DIR), cmd
    assert "--model-version" in cmd, cmd
    idx2 = cmd.index("--model-version")
    assert cmd[idx2 + 1] == PROPOSED_MODEL_VERSION == "v3.2b", cmd
    print("OK - proposed detector 커맨드에 v3.2b artifact 경로·model_version이 명시 전달됨")


def test_fixed_threshold_command_has_no_v32b_artifact_args():
    # native/fixed_threshold 배선은 §86 변경과 무관해야 한다(불변 확인).
    cmd = _build_detector_command("fixed_threshold", "run-1")
    assert "--artifacts-dir" not in cmd, cmd
    assert "--model-version" not in cmd, cmd
    assert make_detector_for_arm("native", "run-1") is None
    print("OK - fixed_threshold 커맨드에 v3.2b 관련 인자 없음, native는 여전히 detector 없음(불변)")


def test_evidence_log_path_default_none_leaves_command_unchanged():
    # §90 E2E pilot - evidence_log_path 미지정(기존 모든 호출부의 기본값)이면
    # 커맨드가 이전과 정확히 동일해야 한다(본 실험 기존 동작 불변 확인).
    cmd_without_param = _build_detector_command("proposed", "run-1")
    cmd_with_default = _build_detector_command("proposed", "run-1", evidence_log_path=None)
    assert cmd_without_param == cmd_with_default
    assert "--evidence-log" not in cmd_without_param, cmd_without_param
    print("OK - evidence_log_path 기본값(None)은 커맨드를 전혀 바꾸지 않음(opt-in)")


def test_evidence_log_path_appended_only_for_proposed():
    cmd = _build_detector_command("proposed", "run-1", evidence_log_path="/tmp/evidence.jsonl")
    assert "--evidence-log" in cmd, cmd
    idx = cmd.index("--evidence-log")
    assert cmd[idx + 1] == "/tmp/evidence.jsonl", cmd
    print("OK - evidence_log_path 지정 시 proposed 커맨드에 --evidence-log로 정확히 전달됨")


def test_evidence_log_path_ignored_for_fixed_threshold():
    # fixed_threshold.py는 --evidence-log 옵션 자체가 없다 - 실수로 붙으면
    # 즉시 argparse 오류로 detector가 시작조차 못 한다(fail-closed 회귀 방지).
    cmd = _build_detector_command("fixed_threshold", "run-1", evidence_log_path="/tmp/evidence.jsonl")
    assert "--evidence-log" not in cmd, cmd
    print("OK - evidence_log_path가 지정돼도 fixed_threshold 커맨드에는 절대 안 붙음")


def test_detector_script_dispatch_table_has_exactly_two_non_native_arms():
    # native를 제외한 두 arm만 detector를 가지며, 서로 다른 스크립트를
    # 가리켜야 한다(구조적으로 arm과 detector가 어긋날 수 없음을 보장).
    assert set(_DETECTOR_SCRIPTS.keys()) == {"fixed_threshold", "proposed"}
    assert _DETECTOR_SCRIPTS["fixed_threshold"]["script"] != _DETECTOR_SCRIPTS["proposed"]["script"]
    assert _DETECTOR_SCRIPTS["fixed_threshold"]["name"] != _DETECTOR_SCRIPTS["proposed"]["name"]
    print("OK - detector 매핑 테이블이 native 제외 2개 arm만 담고, 서로 다른 스크립트/이름을 가짐")


def test_run_id_propagated_into_detector_command():
    cmd = _build_detector_command("fixed_threshold", "load_ramp-fixed_threshold-01-20260101T000000Z")
    assert "--run-id" in cmd
    idx = cmd.index("--run-id")
    assert cmd[idx + 1] == "load_ramp-fixed_threshold-01-20260101T000000Z"
    print("OK - run_id가 --run-id 인자로 정확히 전파됨")


def test_subprocess_detector_lifecycle_start_alive_stop():
    # trivial한 오래 도는 프로세스로 start/is_alive/stop 자체의 정확성만 검증.
    detector = _subprocess_detector([sys.executable, "-c", "import time; time.sleep(30)"], "trivial")
    assert detector.is_alive() is False, "start() 전에는 살아있으면 안 됨"
    detector.start()
    time.sleep(0.3)
    assert detector.is_alive() is True, "start() 직후에는 살아있어야 함"
    detector.stop()
    assert detector.is_alive() is False, "stop() 이후에는 죽어있어야 함"
    detector.stop()  # idempotent 확인 - 두 번째 호출도 예외 없이 안전해야 함
    print("OK - 서브프로세스 detector의 start/is_alive/stop 생명주기 정확")


def test_subprocess_detector_crash_is_observed_as_not_alive():
    # 즉시 0이 아닌 코드로 죽는 프로세스 - is_alive()가 곧바로 False를 내야 한다.
    detector = _subprocess_detector([sys.executable, "-c", "import sys; sys.exit(1)"], "trivial")
    detector.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and detector.is_alive():
        time.sleep(0.05)
    assert detector.is_alive() is False, "즉시 종료되는 프로세스는 곧 is_alive()=False여야 함"
    print("OK - 서브프로세스가 크래시하면 is_alive()가 False로 관측됨")


def test_subprocess_detector_crash_preserves_stdout_to_log_file(tmp_path):
    """§101(2026-09-23, pod_kill-proposed-01-mainexp-v1 실사고 계기) - 크래시
    전에 찍은 출력이 stdout=PIPE(아무도 안 읽음)처럼 유실되지 않고 파일에
    남아야 한다. PYTHONUNBUFFERED=1 없이도 print 직후 sys.exit()이면 정상
    인터프리터 종료 경로를 타 flush되지만, 이 테스트는 "파일에 실제로
    남는지" 자체를 확인한다(파이프였다면 아무도 안 읽어 검증 자체가 불가)."""
    with patch.object(arm_controller, "DETECTOR_LOG_DIR", tmp_path):
        detector = _subprocess_detector(
            [sys.executable, "-c", "print('hello from crashing detector'); import sys; sys.exit(1)"],
            "trivial", run_id="test-run-crash-log")
        detector.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and detector.is_alive():
            time.sleep(0.05)
        assert detector.is_alive() is False
        crash_info = detector.get_crash_info()
    assert crash_info["exit_code"] == 1, crash_info
    assert crash_info["run_id"] == "test-run-crash-log", crash_info
    log_path = Path(crash_info["log_path"])
    assert log_path.exists(), f"로그 파일이 생성되지 않음: {log_path}"
    assert "hello from crashing detector" in log_path.read_text(encoding="utf-8")
    print("OK - 크래시해도 exit_code/run_id/log_path가 남고 로그 파일에 실제 출력이 보존됨")


def test_subprocess_detector_log_dir_created_under_results():
    assert arm_controller.DETECTOR_LOG_DIR.name == "detector-logs"
    assert arm_controller.DETECTOR_LOG_DIR.parent.name == "results"
    print("OK - detector 로그는 experiments/results/detector-logs/ 아래에 남음(기존 결과물과 같은 관례)")


def test_subprocess_detector_no_pipe_used_for_stdout(tmp_path):
    """§101 - stdout이 subprocess.PIPE가 아니라 실제 파일 객체여야 한다 -
    PIPE면 아무도 안 읽을 때 OS 파이프 버퍼가 차서 자식 프로세스의 쓰기가
    블로킹될 위험이 있다(대량 출력을 내는 detector일수록 실제로 닿을 수
    있음). 대량 출력을 내는 명령으로 짧은 시간 안에 정상 종료되는지 확인해
    이 위험이 없음을 실측으로 증명한다."""
    with patch.object(arm_controller, "DETECTOR_LOG_DIR", tmp_path):
        # 파이프 기본 버퍼(보통 64KB)를 확실히 넘기는 양을 짧은 시간에 출력
        detector = _subprocess_detector(
            [sys.executable, "-c", "print('x' * 200000)"], "trivial", run_id="test-large-output")
        detector.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and detector.is_alive():
            time.sleep(0.02)
        elapsed = time.monotonic() - deadline + 5
        crash_info = detector.get_crash_info()
    assert crash_info["exit_code"] == 0, "대량 출력 후 정상 종료해야 함(파이프 블로킹 없음)"
    assert elapsed < 5, "5초 안에 끝나야 함 - 안 끝나면 파이프 버퍼 블로킹 의심"
    log_path = Path(crash_info["log_path"])
    assert log_path.stat().st_size >= 200000, "대량 출력이 전부 파일에 남아야 함"
    print("OK - 대량 출력에도 블로킹 없이 정상 종료하고 전부 파일에 남음(파이프 미사용 확인)")


def test_stop_file_for_derives_deterministic_path_from_evidence_log():
    assert _stop_file_for("/a/b/evidence.jsonl") == "/a/b/evidence.jsonl.stopfile"
    print("OK - stop-file 경로가 evidence_log_path에서 결정적으로 유도됨")


def test_build_detector_command_includes_stop_file_only_with_evidence_log():
    cmd_with = _build_detector_command("proposed", "run-1", evidence_log_path="/tmp/e.jsonl")
    assert "--stop-file" in cmd_with, cmd_with
    idx = cmd_with.index("--stop-file")
    assert cmd_with[idx + 1] == "/tmp/e.jsonl.stopfile", cmd_with
    cmd_without = _build_detector_command("proposed", "run-1")
    assert "--stop-file" not in cmd_without, cmd_without
    print("OK - --stop-file은 evidence_log_path가 있을 때만 proposed 커맨드에 붙음")


def test_subprocess_detector_stop_default_unchanged_immediate_terminate():
    # §92 - stop_file_path 미지정(기본값, 기존 모든 호출부)이면 grace 대기
    # 없이 기존과 동일하게 즉시 terminate()부터 시작해야 한다.
    detector = _subprocess_detector([sys.executable, "-c", "import time; time.sleep(30)"], "trivial")
    detector.start()
    time.sleep(0.3)
    t0 = time.monotonic()
    result = detector.stop()
    elapsed = time.monotonic() - t0
    assert detector.is_alive() is False
    assert result["graceful"] is False, result
    assert elapsed < 5.0, f"stop_file_path 없으면 즉시 종료돼야 하는데 {elapsed:.1f}초 걸림"
    print("OK - stop_file_path 미지정 시 기존과 동일하게 즉시 terminate(grace 대기 없음, 회귀 방지)")


def test_subprocess_detector_stop_with_stop_file_graceful_exit():
    # §92 - stop-file을 스스로 확인해 정상 종료하는 프로세스는 강제종료 없이
    # graceful=True로 관측돼야 한다.
    stop_file = os.path.join(tempfile.gettempdir(), f"test-stopfile-{uuid.uuid4().hex}.stopfile")
    script = (
        "import os, sys, time\n"
        f"stop_file = {stop_file!r}\n"
        "for _ in range(200):\n"
        "    if os.path.exists(stop_file):\n"
        "        sys.exit(0)\n"
        "    time.sleep(0.05)\n"
        "sys.exit(1)\n"
    )
    detector = _subprocess_detector([sys.executable, "-c", script], "trivial", stop_file_path=stop_file)
    try:
        detector.start()
        time.sleep(0.3)
        assert detector.is_alive() is True
        t0 = time.monotonic()
        result = detector.stop()
        elapsed = time.monotonic() - t0
        assert result["graceful"] is True, result
        assert result["exit_code"] == 0, result
        assert elapsed < 5.0, f"stop-file을 즉시 확인하는 프로세스인데 {elapsed:.1f}초나 걸림"
        assert detector.is_alive() is False
    finally:
        if os.path.exists(stop_file):
            os.remove(stop_file)
    print("OK - stop-file을 스스로 확인해 정상 종료하는 프로세스는 강제종료 없이 graceful=True로 관측됨")


def test_subprocess_detector_stop_falls_back_to_terminate_after_grace_timeout():
    # §92 - stop-file을 절대 확인하지 않는 프로세스는 grace timeout 뒤
    # 강제종료(terminate/kill)로 폴백해야 한다(무한 대기 금지).
    stop_file = os.path.join(tempfile.gettempdir(), f"test-stopfile-{uuid.uuid4().hex}.stopfile")
    detector = _subprocess_detector([sys.executable, "-c", "import time; time.sleep(30)"], "trivial",
                                     stop_file_path=stop_file)
    try:
        detector.start()
        time.sleep(0.3)
        with patch.object(arm_controller, "GRACEFUL_STOP_TIMEOUT_SEC", 0.3):
            result = detector.stop()
        assert result["graceful"] is False, result
        assert detector.is_alive() is False
    finally:
        if os.path.exists(stop_file):
            os.remove(stop_file)
    print("OK - grace 기간 안에 stop-file을 확인 안 하는 프로세스는 강제종료로 폴백(무한 대기 없음)")


def test_wrap_injector_with_preview_prep_native_passthrough():
    calls = {"prepare": 0}
    injector = Injector(prepare=lambda: calls.__setitem__("prepare", calls["prepare"] + 1),
                         inject=lambda: None, is_started=lambda: True, is_effective=lambda: True,
                         is_done=lambda: True, cleanup=lambda: None)
    wrapped = wrap_injector_with_preview_prep(injector, "native")
    wrapped.prepare()
    assert calls["prepare"] == 1
    print("OK - native는 preview 준비 없이 원본 prepare() 그대로 호출됨")


def test_wrap_injector_with_preview_prep_success_calls_original_prepare():
    calls = {"prepare": 0, "preview_fn_args": None}

    def fake_prepare_preview(name, namespace, timeout):
        calls["preview_fn_args"] = (name, namespace, timeout)
        return {"ready": True, "t_prep_start": "t0", "t_preview_ready": "t1",
                "prep_duration_sec": 12.3, "external_interference": False,
                "rollback_attempted": False, "rollback_ok": None, "aborted_pod_hash": None}

    injector = Injector(prepare=lambda: calls.__setitem__("prepare", calls["prepare"] + 1),
                         inject=lambda: None, is_started=lambda: True, is_effective=lambda: True,
                         is_done=lambda: True, cleanup=lambda: None)
    wrapped = wrap_injector_with_preview_prep(
        injector, "fixed_threshold", rollout_name="vllm-serving", namespace="vllm-serving",
        prepare_preview_fn=fake_prepare_preview, preview_prep_timeout_sec=480.0,
    )
    wrapped.prepare()
    assert calls["preview_fn_args"] == ("vllm-serving", "vllm-serving", 480.0)
    assert calls["prepare"] == 1, "preview 준비 성공 후에는 원본 prepare()도 호출돼야 함"
    info = wrapped.get_preview_prep_info()
    assert info["t_preview_ready"] == "t1" and info["prep_duration_sec"] == 12.3
    print("OK - preview 준비 성공 시 원본 prepare() 호출 + 진단 정보(get_preview_prep_info) 노출")


def test_wrap_injector_with_preview_prep_failure_blocks_original_prepare():
    # 핵심 회귀 테스트(방식 8) - preview 준비 실패 시 injector.inject()로
    # 이어질 원본 prepare() 자체가 호출되면 안 된다. rollback은 성공했다고
    # 가정(클러스터는 정상 복원) - 이 경우는 여전히 TrialInvalid여야 한다.
    calls = {"prepare": 0}

    def failing_prepare_preview(name, namespace, timeout):
        return {"ready": False, "t_prep_start": "t0", "t_preview_ready": None,
                "prep_duration_sec": 480.4, "external_interference": False,
                "rollback_attempted": True, "rollback_ok": True, "aborted_pod_hash": "previewXYZ"}

    injector = Injector(prepare=lambda: calls.__setitem__("prepare", calls["prepare"] + 1),
                         inject=lambda: None, is_started=lambda: True, is_effective=lambda: True,
                         is_done=lambda: True, cleanup=lambda: None)
    wrapped = wrap_injector_with_preview_prep(
        injector, "proposed", prepare_preview_fn=failing_prepare_preview,
    )

    raised = False
    try:
        wrapped.prepare()
    except TrialInvalid as e:
        raised = True
        assert "previewXYZ" in str(e) and "rollback 성공" in str(e)
    assert raised, "preview 준비 실패 + rollback 성공은 TrialInvalid여야 함(이 trial만 무효, 클러스터는 정상)"
    assert calls["prepare"] == 0, "preview 준비 실패 시 원본 prepare()(따라서 이후 inject())가 호출되면 안 됨"
    print("OK - preview 준비 실패+rollback 성공 시 TrialInvalid, 원본 prepare()/이후 injection 미실행")


def test_wrap_injector_with_preview_prep_rollback_failure_raises_harness_corrupted():
    # 2026-09-19 추가 - fixed_threshold pilot 01회 사고(preview 방치, Rollout
    # Paused/Degraded로 남음) 재발 방지. rollback 자체가 실패하면 이 trial만
    # 무효 처리하고 다음 trial로 넘어가면 안 된다 - HarnessCorrupted로 승격.
    def failing_rollback(name, namespace, timeout):
        return {"ready": False, "t_prep_start": "t0", "t_preview_ready": None,
                "prep_duration_sec": 480.2, "external_interference": False,
                "rollback_attempted": True, "rollback_ok": False, "aborted_pod_hash": "previewXYZ"}

    injector = Injector(prepare=lambda: None, inject=lambda: None, is_started=lambda: True,
                         is_effective=lambda: True, is_done=lambda: True, cleanup=lambda: None)
    wrapped = wrap_injector_with_preview_prep(injector, "fixed_threshold", prepare_preview_fn=failing_rollback)

    raised = False
    try:
        wrapped.prepare()
    except HarnessCorrupted as e:
        raised = True
        assert "previewXYZ" in str(e) and "rollback 실패" in str(e)
    except TrialInvalid:
        assert False, "rollback 실패는 TrialInvalid가 아니라 HarnessCorrupted여야 함"
    assert raised, "자동 rollback 자체가 실패하면 HarnessCorrupted를 던져야 함"
    print("OK - preview 준비 실패 + 자동 rollback도 실패 시 HarnessCorrupted로 승격")


def test_wrap_injector_with_preview_prep_external_interference_raises_harness_corrupted_without_rollback():
    # 2026-09-19 추가 - bump 직후 activeSelector가 예상 밖이면(다른 프로세스
    # 개입 가능성) 무엇이 "우리 preview"인지 특정 불가 - abort를 시도하지
    # 않고(이미 존재하던 preview를 함부로 제거하지 않음) HarnessCorrupted.
    calls = {"abort_would_be_called": False}

    def interfered_prepare(name, namespace, timeout):
        return {"ready": False, "t_prep_start": "t0", "t_preview_ready": None,
                "prep_duration_sec": 1.0, "external_interference": True,
                "rollback_attempted": False, "rollback_ok": None, "aborted_pod_hash": None}

    injector = Injector(prepare=lambda: calls.__setitem__("abort_would_be_called", True),
                         inject=lambda: None, is_started=lambda: True, is_effective=lambda: True,
                         is_done=lambda: True, cleanup=lambda: None)
    wrapped = wrap_injector_with_preview_prep(injector, "proposed", prepare_preview_fn=interfered_prepare)

    raised = False
    try:
        wrapped.prepare()
    except HarnessCorrupted:
        raised = True
    assert raised, "activeSelector 예상 밖 변경은 HarnessCorrupted여야 함"
    assert calls["abort_would_be_called"] is False, "원본 prepare()(따라서 injection)가 호출되면 안 됨"
    print("OK - 외부 개입 감지 시 rollback 시도 없이 HarnessCorrupted(fail-closed)")


def test_wrap_injector_cleanup_aborts_unpromoted_preview_after_original_cleanup():
    # 2026-09-19 추가 - fixed_threshold pilot 재실행에서 실측 발견한 gap:
    # preview 준비는 성공했는데 detector가 promote를 안 하면 trial 종료
    # 시 아무도 그 preview를 안 치웠다. 원본 cleanup()이 먼저 실행되고,
    # 그 다음 미promote preview 정리가 시도돼야 한다(순서 확인).
    calls = {"order": []}

    def fake_prepare_preview(name, namespace, timeout):
        return {"ready": True, "t_prep_start": "t0", "t_preview_ready": "t1", "prep_duration_sec": 200.0,
                "external_interference": False, "rollback_attempted": False, "rollback_ok": None,
                "aborted_pod_hash": None, "pre_prepare_active_selector": "stableA", "created_pod_hash": "previewB"}

    def fake_cleanup_preview(prep_info, name, namespace):
        calls["order"].append("cleanup_preview")
        assert prep_info["created_pod_hash"] == "previewB"
        return True

    injector = Injector(prepare=lambda: None, inject=lambda: None, is_started=lambda: True,
                         is_effective=lambda: True, is_done=lambda: True,
                         cleanup=lambda: calls["order"].append("original_cleanup"))
    wrapped = wrap_injector_with_preview_prep(
        injector, "fixed_threshold", prepare_preview_fn=fake_prepare_preview, cleanup_preview_fn=fake_cleanup_preview,
    )
    wrapped.prepare()
    wrapped.cleanup()
    assert calls["order"] == ["original_cleanup", "cleanup_preview"], calls["order"]
    print("OK - trial 종료 시 원본 cleanup() 후 미promote preview 정리가 이어서 실행됨(순서 확인)")


def test_wrap_injector_cleanup_skips_preview_check_when_no_prep_happened():
    # prepare()가 아직 호출 안 됐으면(예: prepare() 자체가 실패해서 quiescence
    # 단계에서 일찍 끝난 경우) prep_state가 비어있다 - cleanup_preview_fn에
    # None이 그대로 전달돼 정리할 게 없음을 스스로 판단하게 한다.
    calls = {"prep_info_seen": "not-called"}

    def fake_cleanup_preview(prep_info, name, namespace):
        calls["prep_info_seen"] = prep_info
        return None

    injector = Injector(prepare=lambda: None, inject=lambda: None, is_started=lambda: True,
                         is_effective=lambda: True, is_done=lambda: True, cleanup=lambda: None)
    wrapped = wrap_injector_with_preview_prep(injector, "proposed", cleanup_preview_fn=fake_cleanup_preview)
    wrapped.cleanup()  # prepare() 없이 바로 cleanup()만 호출
    assert calls["prep_info_seen"] is None
    print("OK - prepare()가 실행된 적 없으면 cleanup_preview_fn에 None이 전달됨")


def test_wrap_injector_cleanup_raises_when_preview_cleanup_fails():
    # 미promote preview 정리 자체가 실패하면(activeSelector 복원 또는
    # scale-down 확인 안 됨) run_once()의 기존 injector.cleanup() 실패
    # 처리(critical_failures -> HarnessCorrupted)를 타도록 예외를 던져야 한다.
    def fake_prepare_preview(name, namespace, timeout):
        return {"ready": True, "t_prep_start": "t0", "t_preview_ready": "t1", "prep_duration_sec": 200.0,
                "external_interference": False, "rollback_attempted": False, "rollback_ok": None,
                "aborted_pod_hash": None, "pre_prepare_active_selector": "stableA", "created_pod_hash": "previewB"}

    injector = Injector(prepare=lambda: None, inject=lambda: None, is_started=lambda: True,
                         is_effective=lambda: True, is_done=lambda: True, cleanup=lambda: None)
    wrapped = wrap_injector_with_preview_prep(
        injector, "fixed_threshold", prepare_preview_fn=fake_prepare_preview,
        cleanup_preview_fn=lambda prep_info, name, namespace: False,  # 정리 실패 시뮬레이션
    )
    wrapped.prepare()
    raised = False
    try:
        wrapped.cleanup()
    except RuntimeError as e:
        raised = True
        assert "previewB" in str(e)
    assert raised, "미promote preview 정리 실패는 예외로 전파돼야 함(run_once가 HarnessCorrupted로 승격)"
    print("OK - 미promote preview 정리 실패 시 예외 전파(기존 injector.cleanup() 실패 경로 재사용)")


def test_wrap_injector_cleanup_runs_preview_check_even_if_original_cleanup_raises():
    # 시나리오 자체 cleanup()이 실패해도 preview 정리는 독립적으로 시도돼야
    # 한다(run_once.py의 기존 "각 정리 단계는 서로 독립" 원칙과 동일).
    calls = {"cleanup_preview_called": False}

    def raising_original_cleanup():
        raise RuntimeError("시나리오 자체 cleanup 실패 시뮬레이션")

    def fake_cleanup_preview(prep_info, name, namespace):
        calls["cleanup_preview_called"] = True
        return None

    injector = Injector(prepare=lambda: None, inject=lambda: None, is_started=lambda: True,
                         is_effective=lambda: True, is_done=lambda: True, cleanup=raising_original_cleanup)
    wrapped = wrap_injector_with_preview_prep(injector, "proposed", cleanup_preview_fn=fake_cleanup_preview)

    raised = False
    try:
        wrapped.cleanup()
    except RuntimeError as e:
        raised = True
        assert "시나리오 자체" in str(e)
    assert raised, "원본 cleanup() 예외는 그대로 전파돼야 함"
    assert calls["cleanup_preview_called"], "원본 cleanup()이 실패해도 preview 정리는 독립적으로 시도돼야 함"
    print("OK - 원본 cleanup() 실패해도 미promote preview 정리는 독립적으로 실행됨")


def test_make_detector_for_arm_reachability_check_blocks_start_when_unreachable():
    detector = make_detector_for_arm("fixed_threshold", "run-1", reachability_check_fn=lambda url: False)
    raised = False
    try:
        detector.start()
    except TrialInvalid:
        raised = True
    assert raised, "RECOVERY_POLICY_SIGNAL_URL이 접근 불가능하면 TrialInvalid를 던져야 함"
    assert detector.is_alive() is False, "reachability 확인에 실패하면 detector 프로세스 자체를 띄우면 안 됨"
    print("OK - RECOVERY_POLICY_SIGNAL_URL 접근 불가 시 detector.start()가 TrialInvalid로 fail-closed(주입도 자동 차단)")


def test_make_detector_for_arm_reachability_check_passes_allows_start():
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None  # 계속 살아있는 것처럼
    with patch("arm_controller.subprocess.Popen", return_value=fake_proc) as mock_popen:
        detector = make_detector_for_arm("proposed", "run-1", reachability_check_fn=lambda url: True,
                                          prometheus_check_fn=lambda: True)
        detector.start()
        assert mock_popen.called, "reachability 통과 시 실제 서브프로세스 시작 시도까지 이어져야 함"
    print("OK - RECOVERY_POLICY_SIGNAL_URL 접근 가능하면 detector.start()가 정상적으로 서브프로세스를 시작함")


def test_prometheus_check_blocks_start_when_unreachable():
    # RECOVERY_POLICY 쪽은 통과시키고 Prometheus만 막아, 두 검사가 서로
    # 독립적으로 게이트한다는 걸 확인(2026-09-19 추가).
    detector = make_detector_for_arm("fixed_threshold", "run-1",
                                      reachability_check_fn=lambda url: True,
                                      prometheus_check_fn=lambda: False)
    raised = False
    try:
        detector.start()
    except TrialInvalid:
        raised = True
    assert raised, "Prometheus가 접근 불가/오래됐으면 TrialInvalid를 던져야 함"
    assert detector.is_alive() is False, "Prometheus 확인 실패 시 detector 프로세스를 띄우면 안 됨"
    print("OK - Prometheus 접근 불가 시 detector.start()가 TrialInvalid로 fail-closed(recovery-policy는 정상이어도)")


def test_prometheus_reachable_and_fresh_rejects_stale_or_missing_data():
    # 실제 네트워크 없이 requests.get 자체를 mocking해 "쿼리는 성공하지만
    # 표본이 오래됨"과 "표본 자체가 없음" 두 경우 모두 False가 나오는지 확인.
    import time as _time

    stale_resp = MagicMock()
    stale_resp.status_code = 200
    stale_resp.json.return_value = {
        "status": "success",
        "data": {"result": [{"value": [_time.time() - 999, "1.0"]}]},
    }
    with patch("arm_controller.requests.get", return_value=stale_resp):
        assert _prometheus_reachable_and_fresh(max_age_sec=120.0) is False

    empty_resp = MagicMock()
    empty_resp.status_code = 200
    empty_resp.json.return_value = {"status": "success", "data": {"result": []}}
    with patch("arm_controller.requests.get", return_value=empty_resp):
        assert _prometheus_reachable_and_fresh() is False

    fresh_resp = MagicMock()
    fresh_resp.status_code = 200
    fresh_resp.json.return_value = {
        "status": "success",
        "data": {"result": [{"value": [_time.time(), "1.0"]}]},
    }
    with patch("arm_controller.requests.get", return_value=fresh_resp):
        assert _prometheus_reachable_and_fresh() is True
    print("OK - Prometheus 응답의 표본 신선도(오래됨/없음/신선함)를 정확히 판별")


def test_resolved_signal_url_prefers_env_override():
    import os
    assert _resolved_signal_url() == "http://localhost:8080/signal"
    os.environ["RECOVERY_POLICY_SIGNAL_URL"] = "http://localhost:9999/signal"
    try:
        assert _resolved_signal_url() == "http://localhost:9999/signal"
    finally:
        del os.environ["RECOVERY_POLICY_SIGNAL_URL"]
    print("OK - RECOVERY_POLICY_SIGNAL_URL 환경변수가 있으면 그 값을, 없으면 로컬 기본값을 씀")


if __name__ == "__main__":
    test_make_detector_for_arm_native_returns_none()
    test_make_detector_for_arm_fixed_threshold_dispatches_correct_script()
    test_make_detector_for_arm_proposed_dispatches_correct_script()
    test_fixed_threshold_command_carries_frozen_cpu_limit()
    test_proposed_command_has_no_cpu_limit_arg()
    test_detector_script_dispatch_table_has_exactly_two_non_native_arms()
    test_run_id_propagated_into_detector_command()
    test_subprocess_detector_lifecycle_start_alive_stop()
    test_subprocess_detector_crash_is_observed_as_not_alive()
    test_stop_file_for_derives_deterministic_path_from_evidence_log()
    test_build_detector_command_includes_stop_file_only_with_evidence_log()
    test_subprocess_detector_stop_default_unchanged_immediate_terminate()
    test_subprocess_detector_stop_with_stop_file_graceful_exit()
    test_subprocess_detector_stop_falls_back_to_terminate_after_grace_timeout()
    test_wrap_injector_with_preview_prep_native_passthrough()
    test_wrap_injector_with_preview_prep_success_calls_original_prepare()
    test_wrap_injector_with_preview_prep_failure_blocks_original_prepare()
    test_wrap_injector_with_preview_prep_rollback_failure_raises_harness_corrupted()
    test_wrap_injector_with_preview_prep_external_interference_raises_harness_corrupted_without_rollback()
    test_wrap_injector_cleanup_aborts_unpromoted_preview_after_original_cleanup()
    test_wrap_injector_cleanup_skips_preview_check_when_no_prep_happened()
    test_wrap_injector_cleanup_raises_when_preview_cleanup_fails()
    test_wrap_injector_cleanup_runs_preview_check_even_if_original_cleanup_raises()
    test_make_detector_for_arm_reachability_check_blocks_start_when_unreachable()
    test_make_detector_for_arm_reachability_check_passes_allows_start()
    test_prometheus_check_blocks_start_when_unreachable()
    test_prometheus_reachable_and_fresh_rejects_stale_or_missing_data()
    test_resolved_signal_url_prefers_env_override()
    test_evidence_log_path_default_none_leaves_command_unchanged()
    test_evidence_log_path_appended_only_for_proposed()
    test_evidence_log_path_ignored_for_fixed_threshold()
    print("\n모두 통과")
