"""guarded-v1 실전 탐지기(c1_live_detector.py) 실행기 쪽 부품 - 오프라인 준비용, 아직 어떤 시행 하네스에도 연결하지 않았다.

- c1_live_starter(...): 기존 하네스의 탐지기 기동 함수 모양 start(run_id, evidence_log_path, sink_url) -> DetectorHandle.
  sink_url은 쓰지 않는다(신호는 guarded 정책으로만 간다 - 무시한 값을 기록에 남긴다). 탐지기는 결과 불명·필수 기록 실패·위험한
  정책 결과에서 스스로 종료하고, 기존 생존 감시(is_alive False → 중단·정리)가 그 종료를 잡는다. 종료 코드 뜻은 EXIT_MEANING.
- wait_for_service_result(...): 승격 뒤 서비스 확인 결과를 /admin/state로 기다린다(읽기 전용). 기한 안에 pending이 끝나지 않으면
  timeout_pending(결과 불명 취급). 상태 읽기 실패는 성공으로 치지 않는다. 서비스 확인 통과는 짧은 트래픽 확인이지
  사용자 품질 개선 입증이 아니다.
"""
import sys
import time
from pathlib import Path

import run_if_threshold_candidate_smoke as smoke

LIVE_SCRIPT = smoke.ANOMALY_DETECTION_DIR / "c1_live_detector.py"
EXIT_MEANING = {0: "ok", 2: "evidence_write_failed", 3: "receiver_check_failed", 4: "payload_error",
                5: "delivery_not_confirmed", 6: "policy_result_requires_stop"}
SERVICE_OK = {"verified": True, "verified_service_failed": False, "verified_service_unknown": None, "resolved": None}


def live_evidence_path(results_dir: Path, run_id: str) -> Path:
    return results_dir / f"c1-live-evidence-{run_id}.jsonl"


def c1_live_starter(results_dir: Path, policy_url: str, expected_version: str, expected_mode: str, *,
                    start_subprocess=smoke._start_subprocess, stop_subprocess=smoke._stop_subprocess):
    def start(run_id, evidence_log_path=None, sink_url=None):
        h = start_subprocess(
            [sys.executable, "-u", str(LIVE_SCRIPT), "--run-id", run_id,
             "--evidence-log", str(live_evidence_path(results_dir, run_id)), "--policy-url", policy_url,
             "--expected-version", expected_version, "--expected-mode", expected_mode],
            cwd=smoke.ANOMALY_DETECTION_DIR, stdout_path=results_dir / f"c1-live-stdout-{run_id}.log",
            stderr_path=results_dir / f"c1-live-stderr-{run_id}.log")
        h["ignored_sink_url"] = sink_url

        def stop():
            r = stop_subprocess(h, "c1_live_detector.py")
            return {**r, "exit_meaning": EXIT_MEANING.get(r.get("returncode"), "terminated_or_unknown")}
        return smoke.DetectorHandle(is_alive=lambda: h["proc"].poll() is None, stop=stop,
                                    label=f"c1_live_v3.3 -> {policy_url} ({expected_mode})")
    return start


def admin_state_reader(get, base_url: str, admin_token: str, timeout=(3.0, 10.0)):
    def read():
        r = get(base_url.rstrip("/") + "/admin/state", headers={"X-Admin-Token": admin_token},
                timeout=timeout, allow_redirects=False)
        r.raise_for_status()
        return r.json()
    return read


def wait_for_service_result(get_state, target_key: str, deadline_sec: float, *, poll_sec: float = 5.0,
                            monotonic=time.monotonic, sleep=time.sleep) -> dict:
    """전체 대기 기한(deadline_sec)과 개별 상태 조회 제한(get_state 쪽 HTTP timeout, admin_state_reader 기본 (3,10)초)은 별개다.
    경계 규칙: 상태 조회가 **끝난 직후** 단조 시계를 다시 읽어 기한 이하(≤)일 때만 그 조회 결과를 판정에 쓴다. 기한을 넘겨(>)
    끝난 조회의 관측(verified 포함)은 late_observation으로 보존만 하고 result=deadline_exceeded, service_ok=None으로 끝낸다."""
    start = monotonic()
    end, errors, last = start + deadline_sec, [], None
    while True:
        blocked, status = None, None
        try:
            st = get_state()
            if st.get("state_available"):
                status = ((st.get("actions") or {}).get(target_key) or {}).get("status")
                blocked = st.get("blocked")
            else:
                errors.append(f"state_unavailable: {st.get('reason')}")
        except Exception as e:  # noqa: BLE001 - 읽기 실패는 기록만 하고 기한까지 다시 읽는다
            errors.append(f"{type(e).__name__}: {e}")
        done = monotonic()
        if done > end:
            return {"result": "deadline_exceeded", "service_ok": None, "last_status": last, "read_errors": errors,
                    "late_observation": {"status": status, "blocked": blocked, "after_deadline_sec": done - end},
                    "elapsed_sec": done - start}
        last = status if status is not None else last
        if blocked:  # 서비스 결과 저장·원장·감사 실패 등 - 결과가 verified여도 성공으로 치지 않는다
            return {"result": "blocked", "service_ok": None, "last_status": last, "blocked": blocked, "read_errors": errors}
        if last in SERVICE_OK:
            return {"result": last, "service_ok": SERVICE_OK[last], "read_errors": errors}
        if done >= end:
            return {"result": "timeout_pending" if last else "state_unreadable", "service_ok": None,
                    "last_status": last, "read_errors": errors}
        sleep(poll_sec)


def guard_after_action(handle, get_state, target_key: str, deadline_sec: float, **wait_kw) -> dict:
    """승격 확인(promoted_verified) 뒤 실행기 쪽 연결: 서비스 결과를 기다리고, 확실한 정상(service_ok True)이 아니면
    (실패·불명·차단·기한 초과·상태 읽기 불가) 탐지기를 멈추고 정리 결과를 함께 돌려준다. 호출부는 이 결과로 실행을 중단 처리한다."""
    w = wait_for_service_result(get_state, target_key, deadline_sec, **wait_kw)
    if w["service_ok"] is True:
        return {**w, "runner_action": "continue"}
    return {**w, "runner_action": "stopped", "detector_stop": handle.stop()}
