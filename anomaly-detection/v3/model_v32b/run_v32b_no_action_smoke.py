#!/usr/bin/env python3
"""§86.6/§88.6 - v3.2b no-action live smoke 오케스트레이션. `score_server.
py`를 실제로 기동해 offline evaluator와 완전히 같은 규칙을 쓰는지
확인하는 것이 유일한 목적이다 - recovery-policy promotion 경로에는
연결하지 않는다(RECOVERY_POLICY_SIGNAL_URL을 로컬 capture_sink.py로
덮어씀).

§88.6 - §87 사고(stdout 버퍼링으로 로그 유실, stray 프로세스의 포트
점유를 못 알아채 신호가 엉뚱한 파일로 샘) 재발 방지로 다음을 추가했다
(판정 로직·의사결정 코드는 전혀 건드리지 않음, 순수 관찰·프로세스
관리 개선):
  - 매 실행마다 임의의 전용 loopback 포트(OS가 배정) 사용 - 고정
    8765 재사용 안 함.
  - sink 기동 전 그 포트가 이미 점유돼 있지 않은지 직접 bind 시도로
    선확인(fail-closed) + `capture_sink.py` 자체도
    `allow_reuse_address=False`로 고정(§88.6 capture_sink.py 참고).
  - 두 서브프로세스 모두 `-u`(unbuffered)로 실행, stdout/stderr를
    각각 별도 파일로 분리 기록.
  - PID·전체 커맨드라인·시작/종료 시각·종료 코드를 evidence에 기록.
  - `score_server.py --evidence-log`로 판정 세부값을 구조화 JSONL로
    직접 flush - stdout 파싱에 의존하지 않음.
  - `capture_sink.py --run-id`로 다른 run_id 요청은 별도 파일로
    격리(오염 방지).
  - session 자체의 lifecycle 타임스탬프(preview 준비/Ready/settle/
    stage/drain/session 종료)로 evidence의 각 evaluation을 사후
    분류(외부 timeline 결합 방식, score_server.py에 phase를 주입하지
    않음).
  - cleanup 확인 - 두 서브프로세스가 실제로 종료됐는지 재확인해
    실패 시 report의 `cleanup_ok`를 False로 남긴다(전체 판정에 반영,
    별도 FAIL 처리는 호출부가 report를 보고 판단)."""
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

MODEL_V32B_DIR = Path(__file__).parent
V3_DIR = MODEL_V32B_DIR.parent
ANOMALY_DETECTION_DIR = V3_DIR.parent
EXPERIMENTS_DIR = ANOMALY_DETECTION_DIR.parent / "experiments"
sys.path.insert(0, str(V3_DIR))
sys.path.insert(0, str(EXPERIMENTS_DIR))
sys.stdout.reconfigure(encoding="utf-8")

from qualify_normal_profile import collect_qualification_session  # noqa: E402

REAL_RECOVERY_POLICY_URL = "http://localhost:8080/signal"
ARTIFACTS_DIR = MODEL_V32B_DIR / "artifacts"
SMOKE_EVIDENCE_DIR = MODEL_V32B_DIR / "smoke_evidence"
RUN_ID = "smoke-v32b-no-action-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
SESSION_ID = "smoke-v32b-runtime-01"


def _pick_free_loopback_port() -> int:
    """§88.6 - 매 실행마다 새 임의 포트를 OS에게 배정받는다(고정 포트
    재사용 안 함 - §87 사고의 재발 여지를 구조적으로 줄임)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _assert_port_free(port: int) -> None:
    """§88.6 - sink를 띄우기 직전, 그 포트에 아직 아무도 없는지 직접
    bind 시도로 재확인한다(fail-closed) - `_pick_free_loopback_port()`와
    실제 sink 기동 사이의 좁은 race는 남지만, §87처럼 "이미 낡은
    프로세스가 오래 점유 중"인 경우는 이 시점에 확실히 잡는다."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError as e:
            raise RuntimeError(f"fail-closed: 포트 {port}가 이미 점유돼 있음 - sink를 시작하지 않음: {e}") from e


def _assert_sink_distinct_from_real_url(sink_url: str) -> None:
    if sink_url == REAL_RECOVERY_POLICY_URL:
        raise RuntimeError("fail-closed: capture sink URL이 실제 recovery-policy URL과 같음 - 시작 안 함")
    if urlsplit(sink_url).port == urlsplit(REAL_RECOVERY_POLICY_URL).port:
        raise RuntimeError("fail-closed: capture sink 포트가 실제 recovery-policy 포트와 같음 - 시작 안 함")
    print(f"확인: capture sink({sink_url}) != 실제 recovery-policy({REAL_RECOVERY_POLICY_URL})")


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


def _windows_listener_pid(port: int) -> "int | None":
    """§88.6 - best-effort 포트 소유 PID 확인(Windows `netstat -ano`
    파싱) - 실패해도 스모크 자체를 막지 않는다(참고 evidence일 뿐,
    fail-closed 판단의 유일한 근거로 쓰지 않음 - 주 안전장치는
    `_assert_port_free()`의 직접 bind 시도)."""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=5.0).stdout
        for line in out.splitlines():
            if f":{port} " in line and "LISTENING" in line:
                return int(line.split()[-1])
    except Exception:  # noqa: BLE001 - 참고 정보, 실패해도 무시
        return None
    return None


def _start_subprocess(cmd: list, cwd: Path, stdout_path: Path, stderr_path: Path) -> dict:
    """§88.6 - unbuffered(-u는 호출부가 cmd에 이미 포함), stdout/stderr
    분리, PID·커맨드라인 기록, 시작 직후 생존 확인까지 한 번에."""
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    stdout_f = open(stdout_path, "w", encoding="utf-8")
    stderr_f = open(stderr_path, "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=stdout_f, stderr=stderr_f,
                             text=True, encoding="utf-8")
    t_started = datetime.now(timezone.utc).isoformat()
    time.sleep(1.0)
    alive_after_start = proc.poll() is None
    return {"proc": proc, "cmd": cmd, "pid": proc.pid, "t_started_utc": t_started,
            "alive_after_start": alive_after_start, "stdout_path": str(stdout_path), "stderr_path": str(stderr_path),
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


def _classify_lifecycle_phase(ts: datetime, session: dict) -> str:
    """§88.6 - session 자체가 기록한 lifecycle 타임스탬프로 evidence의
    각 evaluation을 사후 분류한다(외부 timeline 결합 - score_server.py
    에는 phase 개념을 주입하지 않음). 정의:
      preview_prep: t_prep_start ~ t_preview_ready
      settle: t_preview_ready ~ stage_start(§79 SETTLE_AFTER_PREVIEW_
        READY_SEC=60s + run_candidate 자체 SETTLE_SEC + baseline 60초가
        전부 여기 뭉뚱그려 들어감 - qualify_normal_profile.py가 이
        하위 구간을 별도로 기록하지 않으므로 이 구간 전체를 'settle'로
        묶는다, 과소 세분화이지만 거짓 세분화보다 낫다)
      stage: stage_start ~ stage_end(ramp/idle 측정 구간, feature_rows
        커버 범위와 정확히 일치)
      drain: stage_end ~ t_session_end
      post_session: t_session_end 이후(cleanup 이후 dead time)
      pre_prep: t_prep_start 이전(있을 수 없지만 방어적으로 포함)
    """
    prep = session.get("preview_prep_info", {})
    t_prep_start = prep.get("t_prep_start")
    t_preview_ready = prep.get("t_preview_ready")
    stages = session.get("ramp_candidate_result", {}).get("stages", [])
    stage_start = stages[0]["stage_start_utc"] if stages else None
    stage_end = stages[-1]["stage_end_utc"] if stages else None
    t_session_end = session.get("t_session_end")

    def _p(x):
        return datetime.fromisoformat(x) if isinstance(x, str) else x

    t_prep_start, t_preview_ready = _p(t_prep_start), _p(t_preview_ready)
    stage_start, stage_end, t_session_end = _p(stage_start), _p(stage_end), _p(t_session_end)

    if t_prep_start and ts < t_prep_start:
        return "pre_prep"
    if t_preview_ready and ts < t_preview_ready:
        return "preview_prep"
    if stage_start and ts < stage_start:
        return "settle"
    if stage_end and ts < stage_end:
        return "stage"
    if t_session_end and ts < t_session_end:
        return "drain"
    return "post_session"


def _annotate_evidence_with_lifecycle(evidence_log_path: Path, session: dict) -> list:
    if not evidence_log_path.exists():
        return []
    rows = []
    for line in evidence_log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        ts = datetime.fromisoformat(rec["wall_clock_before_utc"])
        rec["lifecycle_phase"] = _classify_lifecycle_phase(ts, session)
        rows.append(rec)
    return rows


def main():
    port = _pick_free_loopback_port()
    _assert_port_free(port)
    sink_url = f"http://127.0.0.1:{port}/signal"
    _assert_sink_distinct_from_real_url(sink_url)

    SMOKE_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    sink_out_path = SMOKE_EVIDENCE_DIR / f"{RUN_ID}-sink-captured.jsonl"
    sink_pidfile = SMOKE_EVIDENCE_DIR / f"{RUN_ID}-sink.pid"
    score_evidence_path = SMOKE_EVIDENCE_DIR / f"{RUN_ID}-score-server-evidence.jsonl"

    sink_handle = _start_subprocess(
        [sys.executable, "-u", str(MODEL_V32B_DIR / "capture_sink.py"),
         "--port", str(port), "--out", str(sink_out_path), "--run-id", RUN_ID, "--pidfile", str(sink_pidfile)],
        cwd=MODEL_V32B_DIR,
        stdout_path=SMOKE_EVIDENCE_DIR / f"{RUN_ID}-sink.stdout.log",
        stderr_path=SMOKE_EVIDENCE_DIR / f"{RUN_ID}-sink.stderr.log")
    if not sink_handle["alive_after_start"] or not _wait_http_ok(f"http://127.0.0.1:{port}/healthz"):
        raise RuntimeError(f"fail-closed: capture sink가 정상 기동하지 않음(pid={sink_handle['pid']}, "
                            f"stderr={sink_handle['stderr_path']} 확인)")
    listener_pid = _windows_listener_pid(port)
    if listener_pid is not None and listener_pid != sink_handle["pid"]:
        _stop_subprocess(sink_handle, "capture_sink")
        raise RuntimeError(f"fail-closed: 포트 {port}의 실제 리스너 PID({listener_pid})가 "
                            f"우리가 띄운 sink PID({sink_handle['pid']})와 다름 - stray 프로세스 의심")
    print(f"capture sink 기동 확인: {sink_url}(pid={sink_handle['pid']}, listener_pid_check={listener_pid})")

    env_override = {"RECOVERY_POLICY_SIGNAL_URL": sink_url}
    score_env = dict(os.environ)
    score_env.update(env_override)
    score_env["PYTHONUNBUFFERED"] = "1"
    score_stdout = SMOKE_EVIDENCE_DIR / f"{RUN_ID}-score-server.stdout.log"
    score_stderr = SMOKE_EVIDENCE_DIR / f"{RUN_ID}-score-server.stderr.log"
    score_proc_handle = None
    try:
        stdout_f = open(score_stdout, "w", encoding="utf-8")
        stderr_f = open(score_stderr, "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-u", str(ANOMALY_DETECTION_DIR / "score_server.py"),
             "--artifacts-dir", str(ARTIFACTS_DIR), "--model-version", "v3.2b", "--run-id", RUN_ID,
             "--evidence-log", str(score_evidence_path)],
            cwd=str(ANOMALY_DETECTION_DIR), env=score_env, stdout=stdout_f, stderr=stderr_f,
            text=True, encoding="utf-8")
        score_proc_handle = {"proc": proc, "pid": proc.pid, "_stdout_f": stdout_f, "_stderr_f": stderr_f,
                              "t_started_utc": datetime.now(timezone.utc).isoformat(),
                              "cmd": proc.args, "stdout_path": str(score_stdout), "stderr_path": str(score_stderr)}
        time.sleep(2.0)
        if proc.poll() is not None:
            raise RuntimeError(f"fail-closed: score_server.py가 기동 직후 종료됨(exit={proc.returncode}) - "
                                f"stderr: {score_stderr}")
        print(f"score_server.py(v3.2b) 기동 확인(pid={proc.pid}), sink로 신호 전달 설정: {sink_url}")

        print(f"[{SESSION_ID}] low_load 0.025 RPS 세션 시작(600초 관찰, active_plus_preview) - score_server 병행 실행 중")
        session = collect_qualification_session(
            "low_load", SESSION_ID, official=False, split_role="calibration", dataset_version="v3.1")
    finally:
        score_server_exit = _stop_subprocess(score_proc_handle, "score_server.py") if score_proc_handle else \
            {"pid": None, "returncode": None, "exited_cleanly": False}
        sink_exit = _stop_subprocess(sink_handle, "capture_sink")

    session["_smoke_note"] = ("§86/§88 no-action runtime smoke 전용 evidence - Training/Calibration/Holdout "
                               "registry에 포함되지 않음, v31_data/sessions/에 저장하지 않음")
    session_out_path = SMOKE_EVIDENCE_DIR / f"{SESSION_ID}.json"
    session_out_path.write_text(json.dumps(session, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    annotated = _annotate_evidence_with_lifecycle(score_evidence_path, session)
    annotated_path = SMOKE_EVIDENCE_DIR / f"{RUN_ID}-score-server-evidence-annotated.jsonl"
    with annotated_path.open("w", encoding="utf-8") as f:
        for rec in annotated:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    sink_lines = sink_out_path.read_text(encoding="utf-8").splitlines() if sink_out_path.exists() else []
    rejected_path = sink_out_path.with_name(sink_out_path.name + ".rejected.jsonl")
    rejected_lines = rejected_path.read_text(encoding="utf-8").splitlines() if rejected_path.exists() else []

    cleanup_ok = score_server_exit["exited_cleanly"] and sink_exit["exited_cleanly"]
    report = {
        "run_id": RUN_ID, "session_id": SESSION_ID, "sink_url": sink_url, "sink_port": port,
        "real_recovery_policy_url": REAL_RECOVERY_POLICY_URL,
        "sink_process": {"pid": sink_handle["pid"], "cmd": sink_handle["cmd"],
                          "t_started_utc": sink_handle["t_started_utc"], **sink_exit},
        "score_server_process": {"pid": score_proc_handle["pid"] if score_proc_handle else None,
                                  "cmd": score_proc_handle["cmd"] if score_proc_handle else None,
                                  "t_started_utc": score_proc_handle["t_started_utc"] if score_proc_handle else None,
                                  **score_server_exit},
        "cleanup_ok": cleanup_ok,
        "session_excluded": session.get("excluded"), "session_exclusion_reasons": session.get("exclusion_reasons"),
        "session_t_slo": session.get("t_slo"), "session_cleanup_result": session.get("cleanup_result"),
        "session_active_pod_before": session.get("active_pod_before"),
        "session_active_pod_after": session.get("active_pod_after"),
        "session_endpoint_isolation_before": session.get("endpoint_isolation_before"),
        "session_endpoint_isolation_after": session.get("endpoint_isolation_after"),
        "session_valid_feature_rows": sum(1 for r in session.get("feature_rows", []) if r["valid"]),
        "session_total_feature_rows": len(session.get("feature_rows", [])),
        "score_server_evidence_cycles": len(annotated),
        "score_server_evidence_lifecycle_breakdown": {
            phase: sum(1 for r in annotated if r["lifecycle_phase"] == phase)
            for phase in ("pre_prep", "preview_prep", "settle", "stage", "drain", "post_session")
        },
        "capture_sink_signal_count": len(sink_lines),
        "capture_sink_signals": [json.loads(line) for line in sink_lines],
        "capture_sink_rejected_count": len(rejected_lines),
        "capture_sink_rejected": [json.loads(line) for line in rejected_lines],
        "score_server_evidence_path": str(score_evidence_path),
        "score_server_evidence_annotated_path": str(annotated_path),
    }
    report_path = SMOKE_EVIDENCE_DIR / f"{RUN_ID}-report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\n저장: {session_out_path}")
    print(f"저장: {report_path}")
    print(f"\n요약: valid_rows={report['session_valid_feature_rows']}/{report['session_total_feature_rows']}, "
          f"evidence_cycles={report['score_server_evidence_cycles']}, "
          f"lifecycle_breakdown={report['score_server_evidence_lifecycle_breakdown']}, "
          f"capture_sink_signal_count={report['capture_sink_signal_count']}, "
          f"rejected_count={report['capture_sink_rejected_count']}, "
          f"cleanup_ok={cleanup_ok}, session_excluded={report['session_excluded']}")
    if not cleanup_ok:
        print("경고: cleanup_ok=False - 서브프로세스가 정상 종료되지 않음, 이 run은 FAIL로 취급해야 함")


if __name__ == "__main__":
    main()
