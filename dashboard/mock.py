"""MOCK / DEMO REPLAY - 시연 연습용 가짜 원자료. live 수집기와 같은 모양(Phase 2 ACTIVE/SHADOW 기록)을 만들어 같은
state.derive를 거친다. 화면 상단에 MOCK이 항상 표시된다(mode="mock"). live 모드는 이 파일을 쓰지 않는다.

각본(LOOP_SEC마다 반복, 평가 간격은 연습용으로 5초로 줄임):
 0s 정상 → 20s CPU 압박 주입 → 30s SHADOW(IF) 이상(조건 충족해도 전송 없음) → 40s ACTIVE 1/3 → 50s 3/3·신호
 → 52s 정책 승인·승격 → 54s Service 전환 → 84s 트래픽 확인 → 115s 전환 후 60초 사용자 요청 창 충족 = 복구 완료.
"""
from datetime import datetime, timedelta, timezone

from sources import summarize_probe

LOOP_SEC = 150
EVAL_EVERY = 5
ACTIVE, SHADOW = "cpu-threshold-c", "c3-a-if"
CPU_THR, IF_THR = 1.6842544293089357, -0.1948341270493127
OLD, NEW = "6df78d474c", "7d4fdfd957"
OLD_POD, NEW_POD = f"vllm-serving-{OLD}-mock1", f"vllm-serving-{NEW}-mock2"
OLD_UID, NEW_UID = "00000000-mock-0000-0000-000000000001", "00000000-mock-0000-0000-000000000002"
TARGET = f"vllm-serving/vllm-serving@00000000-mock-rollout:{OLD}->{NEW}#{NEW_UID}"
T_PRESS, T_SIGNAL, T_SEND, T_SWITCH, T_VERIFY = 20, 50, 52, 54, 84
T_CLEAN = T_SWITCH + 4  # 데모 harness가 전환 확인 뒤 이번 실행의 StressChaos를 정리


def _rule(value, thr, streak, role, anomalous):
    return {"value": value, "threshold": thr, "is_anomalous": anomalous, "consecutive": streak,
            "condition_met": streak == 3, "role": role,
            "state": {True: "ANOMALY", False: "NORMAL"}.get(anomalous, "UNKNOWN")}


def _evals(anchor, e):
    out, a_k, s_k = [], 0, 0
    for t in range(0, int(e) + 1, EVAL_EVERY):
        at = (anchor + timedelta(seconds=t)).isoformat()
        if t > T_SWITCH:
            out.append({"evaluation_seq": t // EVAL_EVERY, "status": "skipped", "skipped_at_utc": at, "window_end_utc": at,
                        "skip_reason": f"expected_unverified:selector_unknown(active={NEW}, preview={NEW})",
                        "rules": {ACTIVE: _rule(None, CPU_THR, 0, "ACTIVE", None),
                                  SHADOW: _rule(None, IF_THR, 0, "SHADOW", None)}})
            continue
        cpu = 1.2 + 0.02 * (t % 3) + (0.12 * (t - T_PRESS) / EVAL_EVERY if t >= T_PRESS else 0)
        a_hot = cpu > CPU_THR
        a_k = a_k + 1 if a_hot else 0
        s_hot = 30 <= t <= 45  # SHADOW는 ACTIVE보다 먼저 조건을 채워도 권한이 없다
        s_k = s_k + 1 if s_hot else 0
        out.append({"evaluation_seq": t // EVAL_EVERY, "status": "scored", "score_completed_at_utc": at,
                    "window_end_utc": at,
                    "rules": {ACTIVE: _rule(cpu, CPU_THR, a_k, "ACTIVE", a_hot),
                              SHADOW: _rule(-0.25 if s_hot else -0.12, IF_THR, s_k, "SHADOW", s_hot)}})
    return out


def _cpu(s):
    """파드별 CPU(코어): 기존 Pod는 압박으로 오르고 압박 정리(T_CLEAN) 뒤 내려간다. 새 Pod는 전환 뒤 요청만 받는다."""
    # 실제 그래프는 rate[60s]라 바뀐 뒤 약 60초에 걸쳐 서서히 움직인다(모의 데이터는 잡음 없이)
    def press(t):
        return 1.2 + min(1.55, 0.03 * max(0, t - T_PRESS))
    if s < T_CLEAN:
        old = press(s)
    else:
        old = press(T_CLEAN) + (0.15 - press(T_CLEAN)) * min(1.0, (s - T_CLEAN) / 60)
    new = 0.1 + 0.52 * min(1.0, max(0, s - T_SWITCH) / 60)
    return old, new


def build(elapsed, now=None, minutes=5):
    now = now or datetime.now(timezone.utc)
    e = elapsed % LOOP_SEC
    run_id = f"mock-demo-replay-{int(elapsed // LOOP_SEC) + 1}"  # 반복마다 새 실행으로 취급(타임라인 초기화)
    anchor = now - timedelta(seconds=e)

    def iso(s):
        return (anchor + timedelta(seconds=s)).isoformat()
    attempts, sends = [], []
    if e >= T_SIGNAL + 0.5:
        attempts.append({"detector_id": ACTIVE, "evaluation_seq": T_SIGNAL // EVAL_EVERY,
                         "attempted_at_utc": iso(T_SIGNAL + 0.5)})
    if e >= T_SEND:
        sends.append({"evaluation_seq": T_SIGNAL // EVAL_EVERY, "detector_id": ACTIVE, "decision": "post_action",
                      "delivery": "delivered", "attempted_at_utc": iso(T_SIGNAL + 0.5), "completed_at_utc": iso(T_SEND),
                      "policy_outcome": "promoted_verified", "policy_passed": True, "reasons": [], "target_key": TARGET,
                      "promotion": {"attempted": True, "verified": True, "verified_at": iso(T_SEND)}})
    switched = e >= T_SWITCH
    active = NEW if switched else OLD
    pods = [{"name": OLD_POD, "uid": OLD_UID, "hash": OLD, "node": "sj-worker",
             "ip": "10.0.0.1", "phase": "Running", "ready": True, "terminating": e > 140},
            {"name": NEW_POD, "uid": NEW_UID, "hash": NEW, "node": "sj-worker2", "ip": "10.0.0.2",
             "phase": "Running", "ready": True, "terminating": False}]
    k8s = {"active_hash_before": active, "active_hash_after": active, "preview_hash": NEW,
           "endpoints": [{"ip": "10.0.0.2" if switched else "10.0.0.1", "target": NEW_POD if switched else OLD_POD,
                          "uid": NEW_UID if switched else OLD_UID, "kind": "Pod"}],
           "pods": pods, "rollout": {"phase": "Healthy" if switched else "Paused", "active_selector": active,
                                     "preview_selector": NEW, "abort": False, "stable_rs": active,
                                     "replicas": 1, "ready_replicas": 1}}
    actions = {}
    if e >= T_SEND:
        done = e >= T_VERIFY
        actions[TARGET] = {"status": "verified" if done else "verified_service_pending", "run_id": run_id,
                           "reserved_at": iso(T_SEND - 1.5), "promote_called_at": iso(T_SEND - 1),
                           "service_finished_at": iso(T_VERIFY) if done else None,
                           "service_check": None if not done else {
                               "ok": True, "validity": [], "counter_window_sec": 60.222, "endpoint_samples": 12,
                               "verdict": {"verified": True, "reasons": [], "new_pod_increase": 60.0,
                                           "old_pod_increase": 0.0}}}
    rows = [{"sent_at": anchor + timedelta(seconds=s), "success": True,
             "latency": 0.24 + (0.3 if T_PRESS + 10 <= s <= T_SWITCH else 0.0) + 0.01 * (s % 5)}
            for s in range(-60, int(e) + 1)]
    # Pod별 누적 요청 카운터(1초에 1건): 전환 전에는 기존 Pod, 전환 뒤에는 새 Pod만 는다
    steps = range(-60, int(e) + 1, 5)
    series = {OLD_POD: [[(anchor + timedelta(seconds=s)).timestamp(), 560.0 + min(s, T_SWITCH)] for s in steps],
              NEW_POD: [[(anchor + timedelta(seconds=s)).timestamp(), float(max(0, s - T_SWITCH))] for s in steps]}
    chaos = [{"name": f"{run_id}-cpu-0", "all_injected": True}] if T_PRESS <= e < T_CLEAN else []
    # 정책이 측정하는 동안 Pod별 카운터(실시간 표시용): 새 Pod는 전환 뒤 초당 3건, 기존 Pod는 그대로
    pending = T_SEND <= e < T_VERIFY
    live = {"target_key": TARGET, "old": {"pod": OLD_POD, "value": 560.0 + T_SWITCH},
            "new": {"pod": NEW_POD, "value": 3.0 * max(0, int(e) - T_SWITCH)}} if pending else None
    step = max(5, minutes * 60 // 240)
    ts_ = [s for s in range(int(e) - minutes * 60, int(e) + 1, step)]
    cpu = {OLD_POD: [[(anchor + timedelta(seconds=s)).timestamp(), _cpu(s)[0]] for s in ts_],
           NEW_POD: [[(anchor + timedelta(seconds=s)).timestamp(), _cpu(s)[1]] for s in ts_]}
    ok = {"error": None, "read_at": now.isoformat(), "ok": True}
    return {
        "mode": "mock", "now": now.isoformat(),
        "k8s": {**ok, "data": k8s},
        "detector": {**ok, "data": {"file": "MOCK", "send_pending": bool(attempts) and not sends,
                                    "send_results": sends, "send_attempts": attempts, "evaluations": _evals(anchor, e),
                                    "start": {"run_id": run_id, "detector": "phase2_same_tick_v1 (mock)",
                                              "roles": {ACTIVE: "ACTIVE", SHADOW: "SHADOW"}, "active_detector": ACTIVE,
                                              "thresholds": {ACTIVE: CPU_THR, SHADOW: IF_THR},
                                              "consecutive_threshold": 3, "expected_mode": "act",
                                              "started_at_utc": anchor.isoformat()}}},
        "policy": {**ok, "data": {"identity": {"role": "guarded-recovery-policy", "version": "guarded-v1", "mode": "act"},
                                  "state_error": None,
                                  "state": {"state_available": True, "blocked": False, "mode": "act", "actions": actions}}},
        "probe": {**ok, "data": summarize_probe(rows)},
        "pressure": {**ok, "data": {"injection_started_at": None, "summary_error": None, "chaos": chaos,
                                    "chaos_error": None}},
        "traffic": {**ok, "data": {"series": series, "step_sec": 5}},
        "cpu": {**ok, "data": {"series": cpu, "step_sec": step, "minutes": minutes}},
        "live_counters": {**ok, "data": live},
        "profile": {**ok, "data": {"detectors": [{"id": ACTIVE, "role": "ACTIVE", "threshold": CPU_THR},
                                                 {"id": SHADOW, "role": "SHADOW", "threshold": IF_THR}]}},
    }
