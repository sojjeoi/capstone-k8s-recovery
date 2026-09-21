#!/usr/bin/env python3
"""§89 - v3.2b lifecycle-aligned diagnostic smoke. §87(원본, misaligned)을
대체하거나 지우지 않는다 - 별도 run_id·별도 evidence 파일로 완전히
독립된 1회 실행이다.

목적: 실제 `experiments/run_once.py`의 detector lifecycle(baseline
확보 후에만 시작, trial cleanup 직전에 정지 - §88.2에서 code-cited로
확정한 순서)과 최대한 정확히 일치하는 조건에서 §87의 residual 2건
(signal 4/5, stage 구간·경계)이 재현되는지 확인한다.

`qualify_normal_profile.collect_qualification_session()`은 **전혀
수정하지 않고 그대로 재사용**한다(측정 파이프라인 자체는 건드리지
않음, §88.9 계획대로). detector 시작/정지 시점만 정확한 지점에
꽂아 넣기 위해 그 함수가 내부적으로 쓰는 두 함수(`run_candidate_
with_retry`/`cleanup_unpromoted_preview`)를 실행 도중에만 일시적으로
감싸고(monkey-patch), 함수가 반환하기 전에 원래대로 복원한다 - 이
프로젝트 테스트 스위트 전반에서 이미 쓰는 의존성 주입 패턴과 같은
성격이다. `run_once()` 자체는 호출하지 않는다 - `_wait_for_
quiescence()`/`_register_experiment_context()` 등이 실제
recovery-policy admin API에 무조건 연결하므로(§88.2 §552-661 code
확인), 이번 턴의 "recovery-policy 연결 금지" 범위 제한과 직접
충돌한다. 대신 §89.1에서 `run_once()`의 정확한 phase 순서를 문서화한
뒤, 이 스크립트의 순서가 그것과 동일함을 §89 테스트로 고정한다."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

MODEL_V32B_DIR = Path(__file__).parent
V3_DIR = MODEL_V32B_DIR.parent
ANOMALY_DETECTION_DIR = V3_DIR.parent
EXPERIMENTS_DIR = ANOMALY_DETECTION_DIR.parent / "experiments"
sys.path.insert(0, str(V3_DIR))
sys.path.insert(0, str(EXPERIMENTS_DIR))
sys.stdout.reconfigure(encoding="utf-8")

import qualify_normal_profile as qnp  # noqa: E402

# run_once.py(실제 trial)의 baseline 구성과 동일한 값 - explore_ramp_intensity.py에서
# 그대로 가져온다(새 상수 추정 없음, §89.2).
SETTLE_SEC_INNER = 60  # run_candidate() 자체 내부 settle(explore_ramp_intensity.py 확인)
BASELINE_SEC = 60  # run_candidate()의 baseline 수집 구간(explore_ramp_intensity.py 확인)
DETECTOR_START_DELAY_SEC = SETTLE_SEC_INNER + BASELINE_SEC  # 120초 - "baseline 확보 직후"의 근사치

REAL_RECOVERY_POLICY_URL = "http://localhost:8080/signal"
ARTIFACTS_DIR = MODEL_V32B_DIR / "artifacts"
SMOKE_EVIDENCE_DIR = MODEL_V32B_DIR / "smoke_evidence"
RUN_ID = "smoke-v32b-lifecycle-aligned-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
SESSION_ID = "smoke-v32b-lifecycle-aligned-01"
MIN_STEADY_POINTS = 38


def _pick_free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _assert_port_free(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError as e:
            raise RuntimeError(f"fail-closed: 포트 {port}가 이미 점유돼 있음 - sink를 시작하지 않음: {e}") from e


def _assert_sink_distinct_from_real_url(sink_url: str) -> None:
    if sink_url == REAL_RECOVERY_POLICY_URL or urlsplit(sink_url).port == urlsplit(REAL_RECOVERY_POLICY_URL).port:
        raise RuntimeError("fail-closed: capture sink URL/포트가 실제 recovery-policy와 같음 - 시작 안 함")
    print(f"확인: capture sink({sink_url}) != 실제 recovery-policy({REAL_RECOVERY_POLICY_URL})")


def _assert_no_stray_processes() -> None:
    """§89.4 - 기존 score_server/capture_sink 프로세스가 0개인지 확인
    (best-effort, Windows `wmic`/`tasklist` 기반 - 실패해도 예외로
    막지 않고 경고만 남긴다. 주 안전장치는 각 sink의 임의 전용 포트 +
    `_assert_port_free()`)."""
    try:
        out = subprocess.run(
            ["wmic", "process", "where",
             "CommandLine like '%score_server.py%' or CommandLine like '%capture_sink.py%'",
             "get", "ProcessId,CommandLine"],
            capture_output=True, text=True, timeout=10.0).stdout
        lines = [l for l in out.splitlines() if l.strip() and "CommandLine" not in l]
        if lines:
            raise RuntimeError(f"fail-closed: 기존 score_server.py/capture_sink.py 프로세스가 이미 실행 중: {lines}")
    except FileNotFoundError:
        print("경고: wmic을 사용할 수 없어 stray process 사전 확인을 건너뜀(참고 정보 부족, fail-closed 주 안전장치는 포트 확인)")


def _wait_http_ok(url: str, timeout_sec: float = 15.0) -> bool:
    import requests
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            r = requests.get(url, timeout=2.0)
            if r.status_code == 200:
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(0.5)
    return False


def _start_subprocess(cmd: list, cwd: Path, stdout_path: Path, stderr_path: Path) -> dict:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    stdout_f = open(stdout_path, "w", encoding="utf-8")
    stderr_f = open(stderr_path, "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=stdout_f, stderr=stderr_f, text=True, encoding="utf-8")
    t_started = datetime.now(timezone.utc).isoformat()
    time.sleep(1.0)
    return {"proc": proc, "cmd": cmd, "pid": proc.pid, "t_started_utc": t_started,
            "alive_after_start": proc.poll() is None, "stdout_path": str(stdout_path), "stderr_path": str(stderr_path),
            "_stdout_f": stdout_f, "_stderr_f": stderr_f}


def _stop_subprocess(handle: dict, name: str, timeout_sec: float = 10.0) -> dict:
    proc = handle["proc"]
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=timeout_sec)
    handle["_stdout_f"].close()
    handle["_stderr_f"].close()
    exited_cleanly = proc.poll() is not None
    result = {"pid": handle["pid"], "returncode": proc.returncode,
              "t_stopped_utc": datetime.now(timezone.utc).isoformat(), "exited_cleanly": exited_cleanly}
    print(f"{name} 정리 완료(exit={proc.returncode}, exited_cleanly={exited_cleanly})")
    return result


class DetectorLifecycleController:
    """§89.2 - `run_once()`의 순서(baseline 확보 -> detector.start() ->
    injection -> 관찰 -> detector.stop() -> cleanup)를 `qualify_normal_
    profile.collect_qualification_session()`(변경 없음)의 두 내부 호출
    지점(`run_candidate_with_retry`/`cleanup_unpromoted_preview`)에
    맞춰 재현한다. 측정 파이프라인 코드는 전혀 수정하지 않는다 - 실행
    도중에만 그 두 이름을 감쌌다가 끝나면 원래대로 되돌린다."""

    def __init__(self, artifacts_dir: Path, model_version: str, run_id: str, sink_url: str,
                 evidence_path: Path, stdout_path: Path, stderr_path: Path,
                 start_delay_sec: float = DETECTOR_START_DELAY_SEC, start_detector_fn=None):
        self.artifacts_dir = artifacts_dir
        self.model_version = model_version
        self.run_id = run_id
        self.sink_url = sink_url
        self.evidence_path = evidence_path
        self.stdout_path = stdout_path
        self.stderr_path = stderr_path
        self.start_delay_sec = start_delay_sec
        self._start_detector_override = start_detector_fn  # 테스트 전용 - 실제 subprocess 없이 타이밍/순서만 검증
        self.handle = None
        self._detector_started = False
        self.t_detector_start_utc = None
        self.t_detector_stop_utc = None
        self._timer = None
        self._original_run_candidate_with_retry = None
        self._original_cleanup_unpromoted_preview = None

    def _start_detector(self):
        self._detector_started = True
        if self._start_detector_override is not None:
            self.t_detector_start_utc = datetime.now(timezone.utc).isoformat()
            self._start_detector_override()
            return
        env = dict(os.environ)
        env["RECOVERY_POLICY_SIGNAL_URL"] = self.sink_url
        env["PYTHONUNBUFFERED"] = "1"
        stdout_f = open(self.stdout_path, "w", encoding="utf-8")
        stderr_f = open(self.stderr_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-u", str(ANOMALY_DETECTION_DIR / "score_server.py"),
             "--artifacts-dir", str(self.artifacts_dir), "--model-version", self.model_version,
             "--run-id", self.run_id, "--evidence-log", str(self.evidence_path)],
            cwd=str(ANOMALY_DETECTION_DIR), env=env, stdout=stdout_f, stderr=stderr_f, text=True, encoding="utf-8")
        self.handle = {"proc": proc, "pid": proc.pid, "_stdout_f": stdout_f, "_stderr_f": stderr_f,
                        "cmd": proc.args, "stdout_path": str(self.stdout_path), "stderr_path": str(self.stderr_path)}
        self.t_detector_start_utc = datetime.now(timezone.utc).isoformat()
        print(f"[lifecycle] detector 시작(baseline 확보 근사 시점, pid={proc.pid}): {self.t_detector_start_utc}")

    def _wrapped_run_candidate_with_retry(self, *args, **kwargs):
        # §89.2 - "baseline 확보 후에만 detector 시작"의 근사 - run_candidate()
        # 자체가 SETTLE_SEC_INNER+BASELINE_SEC초 뒤에 baseline 게이트를 통과하므로,
        # 이 함수 진입과 동시에 같은 지연으로 타이머를 건다(같은 프로세스, 같은
        # 시각 기준이라 드리프트가 사실상 없음).
        self._timer = threading.Timer(self.start_delay_sec, self._start_detector)
        self._timer.start()
        try:
            return self._original_run_candidate_with_retry(*args, **kwargs)
        finally:
            self._timer.cancel()  # 이미 발사됐으면 no-op, 발사 전에 예외가 났으면 detector 시작 자체를 취소

    def _wrapped_cleanup_unpromoted_preview(self, *args, **kwargs):
        # §89.2 - "detector.stop()이 injector.cleanup()보다 먼저" -
        # collect_qualification_session()의 finally 블록이 정확히 이 함수를
        # cleanup의 첫 호출로 쓰므로, 그 직전에 detector를 정지한다.
        if self._detector_started and self.t_detector_stop_utc is None:
            self.t_detector_stop_utc = datetime.now(timezone.utc).isoformat()
            print(f"[lifecycle] detector 정지(cleanup 직전, 실제 run_once() 순서와 동일): {self.t_detector_stop_utc}")
            if self._start_detector_override is not None:
                self.stop_result = {"stopped_via_override": True}
            elif self.handle is not None:
                self.stop_result = _stop_subprocess(self.handle, "score_server.py(lifecycle-aligned)")
        return self._original_cleanup_unpromoted_preview(*args, **kwargs)

    def __enter__(self):
        self._original_run_candidate_with_retry = qnp.run_candidate_with_retry
        self._original_cleanup_unpromoted_preview = qnp.cleanup_unpromoted_preview
        qnp.run_candidate_with_retry = self._wrapped_run_candidate_with_retry
        qnp.cleanup_unpromoted_preview = self._wrapped_cleanup_unpromoted_preview
        self.stop_result = None
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        qnp.run_candidate_with_retry = self._original_run_candidate_with_retry
        qnp.cleanup_unpromoted_preview = self._original_cleanup_unpromoted_preview
        if self._timer is not None:
            self._timer.cancel()
        # detector가 아직 살아있다면(예: run_candidate가 baseline_violating으로
        # 일찍 끝나 cleanup 훅을 못 태운 극단적 타이밍) 안전망으로 여기서도 정지.
        if self._start_detector_override is None and self.handle is not None and self.handle["proc"].poll() is None:
            self.stop_result = _stop_subprocess(self.handle, "score_server.py(lifecycle-aligned, safety-net)")
        return False


def _classify_lifecycle_phase_precise(ts: datetime, t_detector_start: "datetime | None",
                                       t_detector_stop: "datetime | None", session: dict) -> str:
    """§89.2 - 실제 detector on/off 시각(위 controller가 기록한 값)을
    최우선으로 쓴다 - session 자체 타임스탬프(§88의 근사 방식)는 detector
    on/off 정보가 없을 때만 보조로 쓴다."""
    if t_detector_start and ts < t_detector_start:
        return "pre_detector_start(baseline_or_earlier)"
    if t_detector_stop and ts >= t_detector_stop:
        return "post_detector_stop(cleanup_or_later)"
    stages = session.get("ramp_candidate_result", {}).get("stages", [])
    stage_start = datetime.fromisoformat(stages[0]["stage_start_utc"]) if stages else None
    stage_end = datetime.fromisoformat(stages[-1]["stage_end_utc"]) if stages else None
    if stage_start and ts < stage_start:
        return "baseline_window(detector_active)"
    if stage_end and ts < stage_end:
        return "steady(stage)"
    return "drain(detector_active_post_injection)"


def main():
    _assert_no_stray_processes()
    port = _pick_free_loopback_port()
    _assert_port_free(port)
    sink_url = f"http://127.0.0.1:{port}/signal"
    _assert_sink_distinct_from_real_url(sink_url)

    SMOKE_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    sink_out_path = SMOKE_EVIDENCE_DIR / f"{RUN_ID}-sink-captured.jsonl"
    sink_pidfile = SMOKE_EVIDENCE_DIR / f"{RUN_ID}-sink.pid"
    score_evidence_path = SMOKE_EVIDENCE_DIR / f"{RUN_ID}-score-server-evidence.jsonl"
    score_stdout = SMOKE_EVIDENCE_DIR / f"{RUN_ID}-score-server.stdout.log"
    score_stderr = SMOKE_EVIDENCE_DIR / f"{RUN_ID}-score-server.stderr.log"

    sink_handle = _start_subprocess(
        [sys.executable, "-u", str(MODEL_V32B_DIR / "capture_sink.py"),
         "--port", str(port), "--out", str(sink_out_path), "--run-id", RUN_ID, "--pidfile", str(sink_pidfile)],
        cwd=MODEL_V32B_DIR,
        stdout_path=SMOKE_EVIDENCE_DIR / f"{RUN_ID}-sink.stdout.log",
        stderr_path=SMOKE_EVIDENCE_DIR / f"{RUN_ID}-sink.stderr.log")
    if not sink_handle["alive_after_start"] or not _wait_http_ok(f"http://127.0.0.1:{port}/healthz"):
        raise RuntimeError(f"fail-closed: capture sink가 정상 기동하지 않음(pid={sink_handle['pid']})")
    print(f"capture sink 기동 확인: {sink_url}(pid={sink_handle['pid']})")

    controller = DetectorLifecycleController(ARTIFACTS_DIR, "v3.2b", RUN_ID, sink_url,
                                              score_evidence_path, score_stdout, score_stderr)
    try:
        with controller:
            print(f"[{SESSION_ID}] low_load 0.025 RPS 세션 시작 - detector는 baseline 확보(약 {DETECTOR_START_DELAY_SEC}초 후) "
                  f"이후에만 활성화되도록 lifecycle-aligned로 배선됨")
            session = qnp.collect_qualification_session(
                "low_load", SESSION_ID, official=False, split_role="calibration", dataset_version="v3.1")
    finally:
        sink_exit = _stop_subprocess(sink_handle, "capture_sink")

    score_server_exit = controller.stop_result or {"pid": None, "returncode": None, "exited_cleanly": False}

    session["_smoke_note"] = ("§89 lifecycle-aligned diagnostic smoke evidence - §87(misaligned) 대체 아님, "
                               "별도 run_id·별도 evidence. Training/Calibration/Holdout registry에 미포함")
    session_out_path = SMOKE_EVIDENCE_DIR / f"{SESSION_ID}.json"
    session_out_path.write_text(json.dumps(session, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    t_start = datetime.fromisoformat(controller.t_detector_start_utc) if controller.t_detector_start_utc else None
    t_stop = datetime.fromisoformat(controller.t_detector_stop_utc) if controller.t_detector_stop_utc else None

    annotated = []
    if score_evidence_path.exists():
        for line in score_evidence_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            ts = datetime.fromisoformat(rec["wall_clock_before_utc"])
            rec["lifecycle_phase"] = _classify_lifecycle_phase_precise(ts, t_start, t_stop, session)
            annotated.append(rec)
    annotated_path = SMOKE_EVIDENCE_DIR / f"{RUN_ID}-score-server-evidence-annotated.jsonl"
    with annotated_path.open("w", encoding="utf-8") as f:
        for rec in annotated:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    sink_lines = sink_out_path.read_text(encoding="utf-8").splitlines() if sink_out_path.exists() else []
    rejected_path = sink_out_path.with_name(sink_out_path.name + ".rejected.jsonl")
    rejected_lines = rejected_path.read_text(encoding="utf-8").splitlines() if rejected_path.exists() else []

    phase_counts = {}
    for rec in annotated:
        phase_counts[rec["lifecycle_phase"]] = phase_counts.get(rec["lifecycle_phase"], 0) + 1
    steady_points = phase_counts.get("steady(stage)", 0)

    cleanup_ok = score_server_exit["exited_cleanly"] and sink_exit["exited_cleanly"]
    report = {
        "run_id": RUN_ID, "session_id": SESSION_ID, "sink_url": sink_url, "sink_port": port,
        "real_recovery_policy_url": REAL_RECOVERY_POLICY_URL,
        "t_detector_start_utc": controller.t_detector_start_utc, "t_detector_stop_utc": controller.t_detector_stop_utc,
        "detector_start_delay_sec_used": DETECTOR_START_DELAY_SEC,
        "run_candidate_attempts": session.get("ramp_candidate_result", {}).get("run_candidate_attempts"),
        "cleanup_ok": cleanup_ok,
        "session_excluded": session.get("excluded"), "session_exclusion_reasons": session.get("exclusion_reasons"),
        "session_t_slo": session.get("t_slo"), "session_cleanup_result": session.get("cleanup_result"),
        "session_active_pod_before": session.get("active_pod_before"),
        "session_active_pod_after": session.get("active_pod_after"),
        "session_endpoint_isolation_before": session.get("endpoint_isolation_before"),
        "session_endpoint_isolation_after": session.get("endpoint_isolation_after"),
        "score_server_evidence_cycles": len(annotated),
        "score_server_evidence_phase_breakdown": phase_counts,
        "steady_observation_points": steady_points,
        "steady_points_meets_minimum_38": steady_points >= MIN_STEADY_POINTS,
        "capture_sink_signal_count": len(sink_lines),
        "capture_sink_signals": [json.loads(line) for line in sink_lines],
        "capture_sink_rejected_count": len(rejected_lines),
        "score_server_evidence_path": str(score_evidence_path),
        "score_server_evidence_annotated_path": str(annotated_path),
    }
    report_path = SMOKE_EVIDENCE_DIR / f"{RUN_ID}-report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\n저장: {session_out_path}")
    print(f"저장: {report_path}")
    print(f"\n요약: phase_breakdown={phase_counts}, steady_points={steady_points}(최소 {MIN_STEADY_POINTS} 필요), "
          f"capture_sink_signal_count={report['capture_sink_signal_count']}, cleanup_ok={cleanup_ok}, "
          f"session_excluded={report['session_excluded']}, run_candidate_attempts={report['run_candidate_attempts']}")
    if not cleanup_ok:
        print("경고: cleanup_ok=False - 이 run은 INVALID로 취급해야 함")


if __name__ == "__main__":
    main()
