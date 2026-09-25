#!/usr/bin/env python3
"""load_ramp 후속 고정관측구간 프로토콜 전용 Prober (§137 이후 지시 §3/§4).
load_ramp_adapter.py의 make_load_ramp_prober()를 그대로 베이스로 하되(같은
pinned 이미지, 같은 pod 생명주기 헬퍼 _run/_wait_pod_ready/_delete_pod를
그대로 import해 재사용 - 새로 만들지 않는다), 두 가지만 다르다:
  1. probe.py 대신 probe_followup.py를 pod에 kubectl cp로 추가 반입해 실행
     (이미지 자체는 무변경 - pinned tag·해시 그대로).
  2. stop() 시점에 evidence JSONL(요청별 sent/completed/timeout/unresolved)도
     함께 회수한다.
기존 load_ramp_adapter.py는 전혀 수정하지 않는다(공식 하니스 무변경) -
이 파일은 완전히 별도이고 후속 비교에서만 쓴다.
"""
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

from run_once import Prober, TrialInvalid
import slo_judge
from load_ramp_adapter import (
    NAMESPACE, IMAGE, SETTLE_SEC, POD_READY_TIMEOUT_SEC,
    PROBE_REFETCH_INTERVAL_SEC, PROBE_DURATION_MARGIN_SEC, PROBE_FETCH_FAILURE_THRESHOLD,
    RESULTS_DIR, _run, _wait_pod_ready, _delete_pod, _is_post_injection_window_evaluable,
)

_PROBE_FOLLOWUP_LOCAL = Path(__file__).parent / "loadgen-runner" / "probe_followup.py"


def make_load_ramp_prober_followup(config_path: str, run_id: str, scenario: str, arm: str, rep: int,
                                    duration_sec: float, evidence_out_dir: Path) -> Prober:
    """make_load_ramp_prober()와 동일 계약(run_once.Prober) - SLO 판정
    로직(slo_judge 호출부)은 한 글자도 바꾸지 않았다. 다른 점은 위 모듈
    docstring 참고."""
    pod_name = f"ramp-probe-fu-{uuid.uuid4().hex[:8]}"
    config_name = Path(config_path).name
    raw_remote = "/probe-raw.csv"
    evidence_remote = "/probe-evidence.jsonl"
    stop_file_remote = "/probe.stopfile"
    local_raw = RESULTS_DIR / f"probe-{run_id}-{arm}-{rep}-raw.csv"
    evidence_out_dir.mkdir(parents=True, exist_ok=True)
    local_evidence = evidence_out_dir / f"probe-{run_id}-{arm}-{rep}-evidence.jsonl"
    probe_duration = duration_sec + PROBE_DURATION_MARGIN_SEC
    cache = {"points": [], "rows": [], "fetched_at": 0.0}
    warmup = {"started_at": None}
    injection_ref = {"t": None}

    def start():
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        _run(["kubectl", "run", pod_name, "-n", NAMESPACE, f"--image={IMAGE}",
              "--image-pull-policy=Never", "--restart=Never", "--", "sleep", str(int(probe_duration) + 300)],
             check=True)
        if not _wait_pod_ready(pod_name):
            raise TrialInvalid(f"{pod_name} Ready 시간초과")
        _run(["kubectl", "cp", os.path.relpath(config_path), f"{NAMESPACE}/{pod_name}:/{config_name}"], check=True)
        _run(["kubectl", "cp", os.path.relpath(str(_PROBE_FOLLOWUP_LOCAL)),
              f"{NAMESPACE}/{pod_name}:/probe_followup.py"], check=True)
        time.sleep(SETTLE_SEC)
        inner = (f"PYTHONUNBUFFERED=1 python /probe_followup.py --config /{config_name} "
                 f"--run-id {run_id} --scenario {scenario} --arm {arm} --rep {rep} "
                 f"--out {raw_remote} --evidence-out {evidence_remote} --duration-sec {probe_duration} "
                 f"--stop-file {stop_file_remote} "
                 f"> /probe.log 2>&1; echo $? > /probe.exit")
        cmd = f"nohup sh -c '{inner}' < /dev/null > /probe-wrapper.log 2>&1 &"
        _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "sh", "-c", cmd], check=True)
        warmup["started_at"] = time.monotonic()

    def is_alive():
        r = _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "test", "!", "-f", "/probe.exit"])
        return r.returncode == 0

    def _refresh():
        now = time.monotonic()
        if now - cache["fetched_at"] < PROBE_REFETCH_INTERVAL_SEC:
            return cache["points"]
        r = _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "cat", raw_remote])
        cache["fetched_at"] = now
        if r.returncode != 0 or not r.stdout.strip():
            cache["consecutive_failures"] = cache.get("consecutive_failures", 0) + 1
            if cache["consecutive_failures"] >= PROBE_FETCH_FAILURE_THRESHOLD:
                raise TrialInvalid(
                    f"probe raw CSV를 {pod_name}에서 {cache['consecutive_failures']}회 연속 못 읽음"
                    f"(kubectl exec 실패 또는 빈 응답) - stale 데이터로 계속 판정할 수 없음"
                )
            return cache["points"]
        cache["consecutive_failures"] = 0
        try:
            local_raw.write_text(r.stdout, encoding="utf-8")
            rows = slo_judge.load_raw(local_raw)
            cache["points"] = slo_judge.evaluate(rows) if rows else []
            cache["rows"] = rows
        except Exception:
            pass
        return cache["points"]

    def notify_injected(t_injection: str):
        injection_ref["t"] = datetime.fromisoformat(t_injection)

    def is_slo_evaluable():
        return _is_post_injection_window_evaluable(cache["rows"], injection_ref["t"])

    def _warmed_up():
        return warmup["started_at"] is not None and time.monotonic() - warmup["started_at"] >= slo_judge.WINDOW_SEC

    def check_slo_violation():
        if not _warmed_up():
            return False
        return slo_judge.find_t_slo(_refresh(), not_before=injection_ref["t"]) is not None

    def check_recovered():
        points = _refresh()
        t_slo = slo_judge.find_t_slo(points, not_before=injection_ref["t"])
        return t_slo is not None and slo_judge.find_t_recovery(points, t_slo) is not None

    def get_actual_slo_time():
        t = slo_judge.find_t_slo(cache["points"], not_before=injection_ref["t"])
        return t.isoformat() if t else None

    def get_actual_recovery_time():
        points = cache["points"]
        t_slo = slo_judge.find_t_slo(points, not_before=injection_ref["t"])
        t_rec = slo_judge.find_t_recovery(points, t_slo) if t_slo else None
        return t_rec.isoformat() if t_rec else None

    def stop():
        """§138 이후 지시 §3에서 발견한 경합 수정 - 이전엔 evidence만 즉시
        회수하고 바로 pkill+삭제해서, probe_followup.py가 자기 grace(60초)
        드레인을 끝내기 전에 강제 종료될 수 있었다(실측: proposed 1건
        unresolved가 진짜 timeout인지 이 경합 때문인지 구분 불가했음).
        이제 순서를 보장한다: 발신 중단 신호(stop-file) -> 진행 중 요청의
        제한된 drain 대기(최대 DRAIN_WAIT_TIMEOUT_SEC, is_alive()로 확인) ->
        raw CSV·evidence 둘 다 최종본 회수 -> pkill(안전망, 이미 끝났으면
        무해)+pod 삭제. drain이 시간 내 안 끝나도 회수·정리는 계속하되,
        그 사실을 숨기지 않고 결과에 명시한다."""
        try:
            _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "touch", stop_file_remote])
        except Exception:
            pass  # stop-file을 못 만들어도 아래 bounded wait가 자연 시간초과로 이어짐

        DRAIN_WAIT_TIMEOUT_SEC = 90.0
        DRAIN_POLL_SEC = 3.0
        drained = False
        waited = 0.0
        while waited < DRAIN_WAIT_TIMEOUT_SEC:
            if not is_alive():  # /probe.exit 존재 = probe_followup.py가 자기 drain을 끝내고 정상 종료함
                drained = True
                break
            time.sleep(DRAIN_POLL_SEC)
            waited += DRAIN_POLL_SEC

        fetch_errs = []
        try:
            r = _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "cat", raw_remote])
            if r.returncode == 0:
                local_raw.write_text(r.stdout, encoding="utf-8")
            else:
                fetch_errs.append(f"raw CSV: {r.stderr}")
        except Exception as e:
            fetch_errs.append(f"raw CSV: {e}")
        try:
            r = _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "cat", evidence_remote])
            if r.returncode == 0:
                local_evidence.write_text(r.stdout, encoding="utf-8")
            else:
                fetch_errs.append(f"evidence JSONL: {r.stderr}")
        except Exception as e:
            fetch_errs.append(f"evidence JSONL: {e}")

        _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "sh", "-c", "pkill -f probe_followup.py || true"])
        _delete_pod(pod_name)

        if not drained:
            raise RuntimeError(
                f"probe_followup.py가 {DRAIN_WAIT_TIMEOUT_SEC}초 안에 자체 drain을 끝내지 못해 강제 종료함"
                f"({pod_name}) - 회수된 raw CSV/evidence의 마지막 몇 건은 강제종료로 인한 것일 수 있어"
                f"'grace 만료로 인한 정상 unresolved'와 구분할 수 없다(완결성 실패, 숨기지 않고 보고)"
            )
        if fetch_errs:
            raise RuntimeError(f"파일 회수 실패({pod_name}): {'; '.join(fetch_errs)} - "
                                f"pod는 정리됐으나 이 trial의 원자료 일부가 없다(별도 확인 필요)")

    return Prober(start=start, is_alive=is_alive, check_slo_violation=check_slo_violation,
                  check_recovered=check_recovered, stop=stop,
                  get_actual_slo_time=get_actual_slo_time, get_actual_recovery_time=get_actual_recovery_time,
                  is_slo_evaluable=is_slo_evaluable, notify_injected=notify_injected)
