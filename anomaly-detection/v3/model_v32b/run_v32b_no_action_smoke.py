#!/usr/bin/env python3
"""§86.6 - v3.2b no-action live smoke 오케스트레이션. `score_server.py`를
실제로 기동해 offline evaluator와 완전히 같은 규칙을 쓰는지 확인하는
것이 유일한 목적이다 - recovery-policy promotion 경로에는 연결하지
않는다(RECOVERY_POLICY_SIGNAL_URL을 로컬 capture_sink.py로 덮어씀).

부하 생성·preview lifecycle·cleanup은 `qualify_normal_profile.
collect_qualification_session()`(변경 없음, calib3-*/holdout3-* 세션
16회에 이미 쓰인 동일 경로)을 그대로 재사용한다 - 이 smoke 전용 새
클러스터 조작 코드를 만들지 않는다. 이 smoke의 session 기록은
`v31_data/sessions/`가 아니라 `smoke_evidence/`에 별도 저장해
Training/Calibration/Holdout registry와 절대 섞이지 않게 한다."""
import json
import os
import re
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

SINK_PORT = 8765
REAL_RECOVERY_POLICY_URL = "http://localhost:8080/signal"
SINK_URL = f"http://127.0.0.1:{SINK_PORT}/signal"
ARTIFACTS_DIR = MODEL_V32B_DIR / "artifacts"
SMOKE_EVIDENCE_DIR = MODEL_V32B_DIR / "smoke_evidence"
RUN_ID = "smoke-v32b-no-action-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
SESSION_ID = "smoke-v32b-runtime-01"

_LOG_LINE_RE = re.compile(r"^\[(?P<ts>[^\]]+)\] score=(?P<score>-?\d+\.\d+) \((?P<status>[^)]+)\), 연속=(?P<consecutive>\d+)$")


def _assert_sink_distinct_from_real_url():
    if SINK_URL == REAL_RECOVERY_POLICY_URL:
        raise RuntimeError("fail-closed: capture sink URL이 실제 recovery-policy URL과 같음 - 시작 안 함")
    real_port = urlsplit(REAL_RECOVERY_POLICY_URL).port
    if SINK_PORT == real_port:
        raise RuntimeError("fail-closed: capture sink 포트가 실제 recovery-policy 포트와 같음 - 시작 안 함")
    print(f"확인: capture sink({SINK_URL}) != 실제 recovery-policy({REAL_RECOVERY_POLICY_URL})")


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


def _stop(proc: subprocess.Popen, name: str, timeout_sec: float = 10.0):
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=timeout_sec)
    print(f"{name} 정리 완료(exit={proc.returncode})")


def _replay_verify(score_server_log_path: Path) -> dict:
    """§86.7 - score_server.py가 실제로 찍은 (timestamp, score) 각각에
    대해 정확히 같은 [timestamp-60s, timestamp) 구간을 offline evaluator
    경로(build_dataset.extract_window_strict + apply_feature_schema +
    frozen model/scaler, 전부 변경 없음)로 다시 조회해 재현되는지
    확인한다 - score_server.py가 실제로 평가에 쓴 window를 그대로
    재현하는 사후 검증(qualify_normal_profile 세션의 별도 window와는
    무관)."""
    sys.path.insert(0, str(V3_DIR / "model_v31"))
    from build_dataset import extract_window_strict
    from feature_selection import apply_feature_schema
    from evaluate import load_frozen_artifacts

    model, scaler, schema, threshold_doc = load_frozen_artifacts(ARTIFACTS_DIR)

    cycles = []
    for line in score_server_log_path.read_text(encoding="utf-8").splitlines():
        m = _LOG_LINE_RE.match(line.strip())
        if m:
            cycles.append((datetime.fromisoformat(m.group("ts")), float(m.group("score"))))

    results = []
    for end, runtime_score in cycles:
        start = end - timedelta(seconds=60)
        feats, reason = extract_window_strict(start, end)
        if feats is None:
            results.append({"end": end.isoformat(), "runtime_score": runtime_score,
                             "offline_score": None, "match": False, "reason": reason})
            continue
        x6 = apply_feature_schema(feats, schema)
        offline_score = float(model.decision_function(scaler.transform([x6]))[0])
        results.append({"end": end.isoformat(), "runtime_score": runtime_score,
                         "offline_score": offline_score,
                         "abs_diff": abs(offline_score - runtime_score),
                         "match": abs(offline_score - runtime_score) <= 1e-6})
    return {"n_cycles_logged": len(cycles), "n_cycles_replayed": len(results), "per_cycle": results,
            "all_match": all(r["match"] for r in results) if results else False,
            "float_tolerance": 1e-6}


def main():
    _assert_sink_distinct_from_real_url()
    SMOKE_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    sink_out_path = SMOKE_EVIDENCE_DIR / f"{RUN_ID}-sink-captured.jsonl"
    score_server_log_path = SMOKE_EVIDENCE_DIR / f"{RUN_ID}-score-server.log"

    sink_proc = subprocess.Popen(
        [sys.executable, str(MODEL_V32B_DIR / "capture_sink.py"), "--port", str(SINK_PORT), "--out", str(sink_out_path)],
        cwd=str(MODEL_V32B_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    if not _wait_http_ok(f"http://127.0.0.1:{SINK_PORT}/healthz"):
        _stop(sink_proc, "capture_sink")
        raise RuntimeError("fail-closed: capture sink가 정상 기동하지 않음")
    print(f"capture sink 기동 확인: {SINK_URL}")

    env = dict(os.environ)
    env["RECOVERY_POLICY_SIGNAL_URL"] = SINK_URL
    score_server_log = open(score_server_log_path, "w", encoding="utf-8")
    score_server_proc = subprocess.Popen(
        [sys.executable, str(ANOMALY_DETECTION_DIR / "score_server.py"),
         "--artifacts-dir", str(ARTIFACTS_DIR), "--model-version", "v3.2b", "--run-id", RUN_ID],
        cwd=str(ANOMALY_DETECTION_DIR), env=env, stdout=score_server_log, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8")
    time.sleep(2.0)
    if score_server_proc.poll() is not None:
        score_server_log.close()
        _stop(sink_proc, "capture_sink")
        raise RuntimeError(f"fail-closed: score_server.py가 기동 직후 종료됨(exit={score_server_proc.returncode}) - "
                            f"로그: {score_server_log_path}")
    print(f"score_server.py(v3.2b) 기동 확인, sink로 신호 전달 설정: {SINK_URL}")

    try:
        print(f"[{SESSION_ID}] low_load 0.025 RPS 세션 시작(600초 관찰, active_plus_preview) - score_server 병행 실행 중")
        session = collect_qualification_session(
            "low_load", SESSION_ID, official=False, split_role="calibration", dataset_version="v3.1")
    finally:
        _stop(score_server_proc, "score_server.py")
        score_server_log.close()
        _stop(sink_proc, "capture_sink")

    session["_smoke_note"] = ("§86 no-action runtime smoke 전용 evidence - Training/Calibration/Holdout "
                               "registry에 포함되지 않음, v31_data/sessions/에 저장하지 않음")
    session_out_path = SMOKE_EVIDENCE_DIR / f"{SESSION_ID}.json"
    session_out_path.write_text(json.dumps(session, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    replay = _replay_verify(score_server_log_path)
    sink_lines = sink_out_path.read_text(encoding="utf-8").splitlines() if sink_out_path.exists() else []

    report = {
        "run_id": RUN_ID, "session_id": SESSION_ID,
        "sink_url": SINK_URL, "real_recovery_policy_url": REAL_RECOVERY_POLICY_URL,
        "session_excluded": session.get("excluded"), "session_exclusion_reasons": session.get("exclusion_reasons"),
        "session_t_slo": session.get("t_slo"), "session_cleanup_result": session.get("cleanup_result"),
        "session_active_pod_before": session.get("active_pod_before"),
        "session_active_pod_after": session.get("active_pod_after"),
        "session_endpoint_isolation_before": session.get("endpoint_isolation_before"),
        "session_endpoint_isolation_after": session.get("endpoint_isolation_after"),
        "session_valid_feature_rows": sum(1 for r in session.get("feature_rows", []) if r["valid"]),
        "session_total_feature_rows": len(session.get("feature_rows", [])),
        "replay_verification": replay,
        "capture_sink_signal_count": len(sink_lines),
        "capture_sink_signals": [json.loads(line) for line in sink_lines],
        "score_server_log_path": str(score_server_log_path),
    }
    report_path = SMOKE_EVIDENCE_DIR / f"{RUN_ID}-report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\n저장: {session_out_path}")
    print(f"저장: {report_path}")
    print(f"\n요약: valid_rows={report['session_valid_feature_rows']}/{report['session_total_feature_rows']}, "
          f"replay_all_match={replay['all_match']}({replay['n_cycles_replayed']}개 cycle), "
          f"capture_sink_signal_count={report['capture_sink_signal_count']}, "
          f"session_excluded={report['session_excluded']}, cleanup={report['session_cleanup_result']}")


if __name__ == "__main__":
    main()
