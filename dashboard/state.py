"""시연 대시보드 판정(순수 함수) - 원자료(raw)를 받아 화면용 view를 만든다. 입출력·클러스터 접근 없음.

원칙(사용자 지시 2026-10-10·10-12 + Codex 검토 반영):
- 증거가 없으면 UNKNOWN / STALE / WAITING / N/A. 0·HEALTHY·NORMAL로 채우지 않고, 마지막 정상값을 현재 값처럼 두지 않는다.
- 승격 API 성공 != SWITCHED. SWITCHED = 대시보드가 직접 읽은 vllm-active Service selector와 ready Endpoints(targetRef)가
  이번 실행 조치의 NEW hash·Pod UID와 정확히 일치(혼재·빈 목록·식별 불가 = 미충족). Service는 앞뒤 두 번 읽어 같을 때만 믿는다.
- TRAFFIC VERIFIED = SWITCHED + 같은 실행·같은 대상의 정책 조치 status=verified, service_check.ok is True, verdict.verified is True,
  new_pod_increase >= 30, 0 <= old_pod_increase <= 2(정책 guarded_k8s 기본값과 같은 기준, 숫자만 인정).
- 회복 완료(VERIFIED) = TRAFFIC VERIFIED + 60초 창 전체가 승격 이후인 신선한 사용자 probe가 SLO v3 기준 정상(표본 20개 이상).
- Prometheus Pod별 요청 그래프는 보여주기 전용이다. 검증 배지·숫자는 정책 service_check 결과만 쓴다.
- Phase 2: ACTIVE 감지기만 복구 신호 권한이 있다. SHADOW는 기록 전용 - SHADOW 전송이 하나라도 있으면 위반으로 드러낸다.
- 감지 표현: 임계값 초과 / 정상 학습 패턴 이탈. 예측·원인 판단 표현을 쓰지 않는다.
"""
import math
import re
from datetime import datetime

DETECTOR_STALE_SEC = 45.0   # 평가 주기 15초 x 3
PROBE_STALE_SEC = 20.0      # 실행기가 probe CSV를 약 5초마다 다시 받아 온다(요청 지연 포함 여유)
FUTURE_SKEW_SEC = 5.0       # 이보다 미래 시각의 증거는 시계 차이로 보고 믿지 않는다
PROBE_MIN_SAMPLES = 20      # slo_judge.MIN_SAMPLES_FOR_RELIABLE_P95 (테스트에서 일치 확인)
VERIFY_MIN_NEW, VERIFY_MAX_OLD = 30.0, 2.0  # recovery-policy/guarded_k8s.py min_new·max_old 기본값
STREAK_OF = 3
IF_RULE = "B"
TARGET_RE = re.compile(r"^(?P<ns>[^/]+)/(?P<name>[^@]+)@(?P<uid>[^:]+):(?P<old>[^-]+)->(?P<new>[^#]+)#(?P<new_uid>.+)$")
PROMOTED = ("promoted_verified", "promoted_unverified", "promotion_unknown")


def ts(s):
    """ISO 문자열 -> aware datetime. 없거나 해석 불가·시간대 없음이면 None."""
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else None


def parse_target(key):
    m = TARGET_RE.match(key or "")
    return m.groupdict() if m else None


def _age(now, at):
    a = ts(at)
    return None if a is None else (now - a).total_seconds()


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


# ---------- 탐지기 ----------

def _rule_key(start):
    """판정에 쓸 규칙. Phase 2는 ACTIVE 감지기 id, B/C/K는 rules.B(IF), 원래 c1_live는 최상위 필드."""
    roles = start.get("roles") or {}
    if roles:
        a = start.get("active_detector")
        return a if a and roles.get(a) == "ACTIVE" else None
    return IF_RULE


def _rule_view(rec, key, p2):
    rb = (rec.get("rules") or {}).get(key) if key else None
    if rb is not None:
        return {"is_anomalous": rb.get("is_anomalous"), "consecutive": rb.get("consecutive"),
                "condition_met": rb.get("condition_met"), "value": rb.get("value")}
    if p2:  # Phase 2인데 ACTIVE 규칙 값이 없으면 다른 감지기·최상위 값으로 채우지 않는다
        return {"is_anomalous": None, "consecutive": None, "condition_met": None, "value": None}
    return {"is_anomalous": rec.get("is_anomalous"), "consecutive": rec.get("consecutive_anomalous"),
            "condition_met": rec.get("condition_met"), "value": None}


def _eval_at(rec):
    return rec.get("score_completed_at_utc") or rec.get("skipped_at_utc")


def _skip_text(reason):
    r = reason or ""
    if "selector_unknown" in r:
        return "전환 후 평가 보류(active=preview)"
    if r.startswith(("not_fresh", "incomplete")):
        return "지표 수집 공백 - 평가 건너뜀"
    if r.startswith("query_failed"):
        return "Prometheus 조회 실패 - 평가 건너뜀"
    return "평가 건너뜀"


def derive_detector(det, now):
    if not det or not det.get("ok"):
        return {"value": "UNKNOWN", "reason": (det or {}).get("error") or "detector source unavailable"}
    d = det.get("data")
    if not d:
        return {"value": "NO DATA", "reason": "이번 세션에 시작된 탐지기 실행 없음"}
    start, evals = d.get("start") or {}, d.get("evaluations") or []
    # Phase 2 = 역할 표가 있거나 평가 규칙에 role이 있는 기록(역할 표가 빠져도 옛 형식으로 오인하지 않는다)
    p2 = bool(start.get("roles")) or any("role" in (v or {}) for e in evals for v in (e.get("rules") or {}).values())
    key = _rule_key(start)
    thr = ((start.get("thresholds") or {}).get(key) if p2
           else start["if_threshold"] if start.get("if_threshold") is not None else start.get("threshold"))
    view = {"run_id": start.get("run_id"), "detector_id": start.get("detector"), "p2": p2,
            "active_id": key if p2 else None, "threshold": thr,
            "streak_of": start.get("consecutive_threshold") or STREAK_OF,
            "series": [], "features": None, "score": None, "streak": None, "last_eval_at": None,
            "last_scored_at": None, "skip_reason": None, "age_sec": None}
    if p2 and key is None:
        return {**view, "value": "UNKNOWN", "reason": "ACTIVE 감지기 정보 없음"}
    for r in evals:
        if r.get("status") != "scored":
            continue
        rv = _rule_view(r, key, p2)
        y = rv["value"] if p2 else (r.get("features") or {}).get("cpu_mean")
        if _num(y):
            view["series"].append({"t": r.get("window_end_utc") or _eval_at(r), "y": y, "anomalous": rv["is_anomalous"]})
    if not evals:
        return {**view, "value": "NO DATA", "reason": "탐지기 시작됨 - 첫 평가 대기"}
    last = evals[-1]
    view["last_eval_at"] = _eval_at(last)
    scored = [r for r in evals if r.get("status") == "scored"]
    if scored:
        f = scored[-1].get("features") or {}
        view.update(features={k: f.get(k) for k in ("cpu_mean", "cpu_slope", "memory_mean", "memory_slope")},
                    score=scored[-1].get("score"), last_scored_at=_eval_at(scored[-1]))
    age = view["age_sec"] = _age(now, view["last_eval_at"])
    if age is None or age > DETECTOR_STALE_SEC:
        # 정책 동기 호출(최대 90초) 중에는 평가 기록이 잠시 멈추는 게 정상 - 이유를 구분해 보여준다
        why = "정책 응답 대기 중(평가 일시 정지)" if d.get("send_pending") else "최근 평가 없음"
        return {**view, "value": "STALE", "reason": why}
    if age < -FUTURE_SKEW_SEC:
        return {**view, "value": "UNKNOWN", "reason": "평가 시각이 현재보다 미래(시계 차이)"}
    if last.get("status") != "scored":
        return {**view, "value": "SKIPPED", "streak": 0, "skip_reason": last.get("skip_reason"),
                "reason": _skip_text(last.get("skip_reason"))}
    rv = _rule_view(last, key, p2)
    view["streak"] = rv["consecutive"]
    hit = "임계값 초과" if p2 and key.startswith("cpu") else "정상 학습 패턴 이탈"
    if rv["is_anomalous"] is True:
        return {**view, "value": "ANOMALY", "reason": hit}
    if rv["is_anomalous"] is False:
        return {**view, "value": "NORMAL", "reason": "정상 범위"}
    return {**view, "value": "UNKNOWN", "reason": "판정 값 없음"}


def derive_shadow(det, det_view):
    """Phase 2 SHADOW 감지기: 상태와 전송 횟수(시도+결과). 전송 0은 읽을 수 있는 전체 기록이 있을 때만 0이다."""
    if not det or not det.get("ok"):
        return {"id": None, "value": "UNKNOWN", "streak": None, "met": None, "sends": None}
    d = det.get("data") or {}
    roles = (d.get("start") or {}).get("roles") or {}
    sid = next((k for k, v in roles.items() if v == "SHADOW"), None)
    if sid is None:
        return None
    evals = d.get("evaluations") or []
    sends = {(x.get("detector_id"), x.get("evaluation_seq"))
             for x in (d.get("send_attempts") or []) + (d.get("send_results") or []) if x.get("detector_id") == sid}
    out = {"id": sid, "streak": None, "sends": len(sends),
           "met": any(((e.get("rules") or {}).get(sid) or {}).get("condition_met") for e in evals)}
    last = evals[-1] if evals else None
    if det_view.get("value") in ("STALE", "UNKNOWN", "NO DATA") or last is None or last.get("status") != "scored":
        return {**out, "value": "UNKNOWN"}
    r = (last.get("rules") or {}).get(sid) or {}
    out["streak"] = r.get("consecutive")
    return {**out, "value": {True: "ANOMALY", False: "NORMAL"}.get(r.get("is_anomalous"), "UNKNOWN")}


def signal_at(det, active_id, p2):
    """ACTIVE 감지기가 정책에 신호를 보낸 첫 시각(시도 기록). Phase 2가 아니면 감지기 구분 없이.
    Phase 2인데 ACTIVE를 확정하지 못하면 None(SHADOW 시도를 ACTIVE 신호로 보이지 않게)."""
    if not det or not det.get("ok") or (p2 and not active_id):
        return None
    d = det.get("data") or {}
    xs = (d.get("send_attempts") or []) + (d.get("send_results") or [])
    ats = [(ts(x.get("attempted_at_utc")), x.get("attempted_at_utc")) for x in xs
           if active_id is None or x.get("detector_id") == active_id]
    ats = [a for a in ats if a[0] is not None]
    return min(ats)[1] if ats else None


# ---------- CPU 압박 ----------

def derive_pressure(pr, seen):
    """summary injection_started_at(실행기가 CPU 상승까지 확인한 시각)이 우선. 실행 중에는 StressChaos AllInjected를
    대시보드가 처음 본 시각(seen.pressure, 관측 시각). CR이 정리된 뒤라도 이미 관측한 압박은 사건으로 남는다."""
    # value = 지금 상태(현재 CR 기준), at = 압박 시작 사건 시각(지난 사건이어도 남는다)
    hist = seen.get("pressure")
    if not pr or not pr.get("ok"):
        return {"value": "UNKNOWN", "at": hist}
    d = pr.get("data")
    if d is None:
        return {"value": "WAITING", "at": None}
    at = d["injection_started_at"] if ts(d.get("injection_started_at")) else hist
    chaos = d.get("chaos")
    if chaos is None:
        return {"value": "UNKNOWN", "at": at, "reason": d.get("chaos_error")}
    if any(c.get("all_injected") for c in chaos):
        return {"value": "INJECTED", "at": at}
    if any(c.get("all_injected") is None for c in chaos):  # 조건 정보 없음 - 압박 중이라고 하지 않는다
        return {"value": "UNKNOWN", "at": at}
    if chaos:
        return {"value": "INJECTING", "at": at}
    return {"value": "ENDED" if at else "NONE", "at": at}


# ---------- Kubernetes(서비스 경로) ----------

def derive_k8s(k8s):
    if not k8s or not k8s.get("ok"):
        return {"ok": False, "reason": (k8s or {}).get("error") or "kubernetes read failed"}
    d = k8s["data"]
    svc_a, svc_b = d.get("active_hash_before"), d.get("active_hash_after")
    pods = [p for p in d.get("pods") or [] if p.get("hash")]
    by_name = {p["name"]: p for p in pods}
    eps = d.get("endpoints") or []
    # targetRef 이름과 UID가 모두 현재 Pod와 같아야 그 Pod로 본다(같은 이름의 교체 Pod·지워진 Pod 오인 방지)
    ep_pods = [p if p is not None and e.get("uid") is not None and e.get("uid") == p.get("uid") else None
               for e in eps for p in [by_name.get(e.get("target"))]]
    consistent = svc_a is not None and svc_a == svc_b and all(p is not None for p in ep_pods)
    preview_hash = d.get("preview_hash")
    return {"ok": True, "consistent": consistent, "service_hash": svc_a if consistent else None,
            "serving": ep_pods if consistent else [], "endpoint_count": len(eps), "preview_hash": preview_hash,
            "preview": [p for p in pods if p["hash"] == preview_hash and preview_hash != svc_a],
            "pods": pods, "rollout": d.get("rollout") or {},
            "reason": None if consistent else "Service/Endpoints 읽기가 서로 맞지 않음(전환 중이거나 식별 불가)"}


def is_switched(kv, target):
    """이번 실행 조치(target)의 NEW hash·UID로 Service와 ready Endpoints가 전부 넘어갔는가."""
    if not kv.get("ok") or not kv.get("consistent") or not target:
        return False
    serving = kv["serving"]
    return (kv["service_hash"] == target["new"] and len(serving) >= 1
            and all(p["hash"] == target["new"] and p.get("uid") == target["new_uid"] and p.get("ready") is True
                    and not p.get("terminating") for p in serving))


# ---------- 사용자 probe ----------

def derive_user(probe, now, not_before=None):
    """SLO v3 창(직전 60초) 기준. not_before를 넘기면(None이 아니면) 해석 가능한 그 시각 이후에 창이 시작할 때만
    VERIFIED 근거(usable)로 쓴다 - 시각이 없거나 깨졌으면 usable=False."""
    if not probe or not probe.get("ok"):
        return {"value": "UNKNOWN", "reason": (probe or {}).get("error") or "사용자 probe 없음", "usable": False}
    d = probe.get("data")
    if not d:
        return {"value": "UNKNOWN", "reason": "이번 실행의 사용자 probe 없음", "usable": False}
    v = {k: d.get(k) for k in ("total", "success", "fail", "p95", "success_rate", "last_sent_at", "window_start")}
    age = v["age_sec"] = _age(now, d.get("last_sent_at"))
    if age is None or age > PROBE_STALE_SEC:
        return {**v, "value": "STALE", "reason": "최근 요청 기록 없음", "usable": False}
    if age < -FUTURE_SKEW_SEC:
        return {**v, "value": "UNKNOWN", "reason": "요청 시각이 현재보다 미래(시계 차이)", "usable": False}
    if d.get("availability_violating") or d.get("latency_violating"):
        return {**v, "value": "DEGRADED", "reason": "SLO v3 기준 위반(직전 60초)", "usable": False}
    if (d.get("total") or 0) < PROBE_MIN_SAMPLES or not d.get("latency_evaluable"):
        return {**v, "value": "UNKNOWN", "reason": f"표본 부족(<{PROBE_MIN_SAMPLES}) - 지연 판정 보류", "usable": False}
    nb, ws = ts(not_before), ts(d.get("window_start"))
    usable = not_before is None or (nb is not None and ws is not None and ws >= nb)
    return {**v, "value": "HEALTHY", "reason": "직전 60초 SLO v3 충족", "usable": usable}


# ---------- 복구 경로 ----------

def _policy_action(pol, run_id, target_key):
    if not pol or not pol.get("ok"):
        return None, (pol or {}).get("error") or "policy unreachable"
    data = pol.get("data") or {}
    st = data.get("state")
    if not st:
        return None, data.get("state_error") or "admin state 미조회"
    if not st.get("state_available"):
        return None, f"policy state unavailable: {st.get('reason')}"
    a = (st.get("actions") or {}).get(target_key) if target_key else None
    if a is not None and a.get("run_id") != run_id:
        return None, "조치가 이번 실행 것이 아님"
    return a, None if a is not None else "정책에 이 조치 기록 없음"


def derive_traffic(action, err):
    if action is None:
        return {"value": "UNKNOWN", "reason": err or "정책 조치 기록 없음", "old_delta": None, "new_delta": None}
    sc = action.get("service_check") or {}
    vd = sc.get("verdict") or {}
    nd, od = vd.get("new_pod_increase"), vd.get("old_pod_increase")
    base = {"old_delta": od if _num(od) else None, "new_delta": nd if _num(nd) else None,
            "window_sec": sc.get("counter_window_sec"), "samples": sc.get("endpoint_samples"),
            "reasons": vd.get("reasons") or [],
            "finished_at": action.get("service_finished_at")}
    status = action.get("status")
    if status == "verified_service_pending" or not sc:
        return {**base, "value": "MEASURING", "reason": "검증 창에서 Pod별 요청 카운터 측정 중"}
    if (status == "verified" and sc.get("ok") is True and vd.get("verified") is True
            and _num(nd) and nd >= VERIFY_MIN_NEW and _num(od) and 0 <= od <= VERIFY_MAX_OLD):
        return {**base, "value": "VERIFIED", "reason": "새 Pod 요청 증가 · 기존 Pod 요청 정지 확인"}
    if vd.get("verified") is False or sc.get("ok") is False:
        return {**base, "value": "FAILED", "reason": "트래픽 확인 실패"}
    return {**base, "value": "UNKNOWN",
            "reason": "카운터 증거 불충분: " + ",".join(sc.get("validity") or vd.get("reasons") or ["unknown"])}


def derive_recovery(raw, kv, now):
    d = (raw.get("detector") or {}).get("data") or {}
    start = d.get("start") or {}
    sends = d.get("send_results") or []
    pol = raw.get("policy") or {}
    pdata = pol.get("data") or {}
    out = {"mode": (pdata.get("identity") or {}).get("mode") if pol.get("ok") else None,
           "target": None, "action_status": None, "promote_at": None, "traffic": None, "switched": False,
           "policy_error": None,
           "policy_outcome": (sends[-1] if sends else {}).get("policy_outcome"),
           "policy_reasons": (sends[-1] if sends else {}).get("reasons"),
           "delivery": (sends[-1] if sends else {}).get("delivery"),
           "signal_sent": bool(sends) or bool(d.get("send_pending")),
           "signal_delivered": any(x.get("delivery") == "delivered" for x in sends),  # 정책이 실제로 받은 신호만
           # 승격하지 않은 결정은 정책 응답 시각. 승격 경로는 아래에서 정책 조치의 reserved_at(조건 통과·조치 예약)으로 바꾼다
           "policy_at": next((x.get("completed_at_utc") for x in sends
                              if x.get("delivery") == "delivered" and x.get("policy_outcome") not in PROMOTED), None),
           "detector_ok": bool((raw.get("detector") or {}).get("ok")),
           "policy_blocked": bool((pdata.get("state") or {}).get("blocked"))}
    if not kv.get("ok"):
        return {**out, "value": "UNKNOWN", "reason": kv.get("reason")}
    if (pdata.get("state") or {}).get("blocked"):
        return {**out, "value": "DEGRADED", "reason": "정책 차단 상태(blocked)"}
    last = sends[-1] if sends else None
    if last and last.get("decision") in ("stop_delivery", "stop_policy"):
        return {**out, "value": "DEGRADED",
                "reason": f"신호/정책 결과로 중단: {last.get('policy_outcome') or last.get('delivery')}"}
    promoted = next((s for s in sends if s.get("policy_outcome") in PROMOTED), None)
    if promoted:
        out["target"] = target = parse_target(promoted.get("target_key"))
        action, err = _policy_action(pol, start.get("run_id"), promoted.get("target_key"))
        out.update(policy_error=err, action_status=(action or {}).get("status"), policy_at=(action or {}).get("reserved_at"),
                   promote_at=(action or {}).get("promote_called_at") or (promoted.get("promotion") or {}).get("verified_at"),
                   traffic=derive_traffic(action, err), switched=is_switched(kv, target))
        if out["action_status"] in ("verified_service_failed", "unverified", "unknown"):
            return {**out, "value": "DEGRADED", "reason": f"정책 서비스 확인 결과: {out['action_status']}"}
        if not out["switched"]:
            return {**out, "value": "PROMOTING", "reason": "승격 호출됨 - Service/Endpoints 전환은 아직 미확인"}
        # 승격 이후 창 판정은 정책 조치의 promote_called_at만 쓴다(없거나 깨지면 VERIFIED 불가)
        user = derive_user(raw.get("probe"), now, not_before=(action or {}).get("promote_called_at") or "")
        if out["traffic"]["value"] == "VERIFIED" and user["value"] == "HEALTHY" and user["usable"]:
            return {**out, "value": "VERIFIED", "reason": "새 Pod 요청 증가 확인 + 전환 후 사용자 요청 정상"}
        why = ("새 Pod 트래픽 확인 대기" if out["traffic"]["value"] != "VERIFIED"
               else "전환 후 60초 사용자 요청 창 대기" if user["value"] == "HEALTHY" else f"사용자 서비스 {user['value']}")
        return {**out, "value": "SWITCHED", "reason": f"Service가 새 Pod를 가리킴 - {why}"}
    if last and last.get("policy_outcome") == "would_pass_record_only":
        return {**out, "value": "RECORD ONLY", "reason": "record_only 모드 - 전환하지 않음(설계상)"}
    if d.get("send_pending"):
        return {**out, "value": "SIGNAL SENT", "reason": "탐지 조건 충족 - 정책 응답 대기"}
    if last:
        return {**out, "value": "NO ACTION", "reason": f"정책 결과: {last.get('policy_outcome')}"}
    if kv["preview"] and all(p.get("ready") and not p.get("terminating") for p in kv["preview"]):
        return {**out, "value": "READY", "reason": "Preview Pod 대기 중(전환 가능)"}
    return {**out, "value": "IDLE", "reason": "Preview 준비 중" if kv["preview"] else "대기 Preview 없음"}


# ---------- Pod별 요청 그래프(보여주기 전용) ----------

def traffic_series(tr, old_hash, new_hash):
    """Prometheus 누적 카운터 -> 시작점 대비 증가량. 카운터가 줄면(재시작) 그 지점부터 0에서 다시 센 값을 더하고
    reset으로 표시한다(값을 깎아 숨기지 않음). Pod 이름에 hash가 정확히 하나 맞을 때만 그 Pod로 본다."""
    if not tr or not tr.get("ok"):
        return {"ok": False, "reason": (tr or {}).get("error") or "Prometheus 없음", "old": None, "new": None}
    series = ((tr.get("data") or {}).get("series")) or {}

    def one(h):
        pods = [p for p in series if h and f"-{h}-" in p]
        if len(pods) != 1:
            return None
        pts, total, prev, reset = [], 0.0, None, False
        for t, v in series[pods[0]]:
            if not (_num(t) and _num(v)):
                continue
            if prev is not None:
                if v < prev:
                    reset, total = True, total + v
                else:
                    total += v - prev
            prev = v
            pts.append([t, total])
        return {"points": pts, "reset": reset} if pts else None

    return {"ok": True, "old": one(old_hash), "new": one(new_hash)}


# ---------- 파드별 CPU · 운영 프로필(보여주기 전용) ----------

POD_HASH_RE = re.compile(r"^vllm-serving-(?P<h>[a-z0-9]+)-[a-z0-9]+$")
CPU_FRESH_SEC = 90.0


def derive_profile(pr):
    d = (pr or {}).get("data") if (pr or {}).get("ok") else None
    if not d:
        return None
    dets = d.get("detectors") or []

    def one(role):
        xs = [x for x in dets if x.get("role") == role]
        return xs[0] if len(xs) == 1 else None
    return {"active": one("ACTIVE"), "shadow": one("SHADOW")}


def role_of(h, target, kv):
    """hash의 이번 세션 역할. 이번 실행 조치(old->new, 정책 결과의 target_key)가 있으면 그것이 기준이고, 승격 결과 전에는
    현재 Service(active) = 곧 바뀔 기존 Pod, preview = 새 Pod. 대기 Pod가 없으면 그냥 현재 Pod. hash를 코드에 고정하지 않는다."""
    if not h:
        return None
    if target:
        return "new" if h == target["new"] else "old" if h == target["old"] else "other"
    act = kv.get("service_hash")
    prev = kv.get("preview_hash") if kv.get("preview_hash") != act else None
    return ("active" if prev else "current") if h == act else "preview" if h == prev else "other"


def cpu_series(cpu, kv, target, now):
    """파드별 CPU 계열과 역할(role_of)."""
    if not cpu or not cpu.get("ok"):
        return {"ok": False, "reason": (cpu or {}).get("error") or "Prometheus 없음", "series": [], "current": None}
    data = cpu.get("data") or {}
    names = {p["name"]: p.get("hash") for p in kv.get("pods") or []}
    out = []
    for pod, pts in sorted((data.get("series") or {}).items()):
        m = POD_HASH_RE.match(pod)
        h = names.get(pod) or (m.group("h") if m else None)
        pts = [[t, v] for t, v in pts if _num(t) and _num(v)]
        if pts:
            out.append({"pod": pod, "hash": h, "role": role_of(h, target, kv) or "other", "points": pts})
    serving = {p["name"] for p in kv.get("serving") or []}
    cur = next((s for s in out if s["pod"] in serving
                and -FUTURE_SKEW_SEC <= now.timestamp() - s["points"][-1][0] <= CPU_FRESH_SEC), None)
    return {"ok": True, "series": out, "current": cur["points"][-1][1] if cur else None,
            "current_pod": cur["pod"] if cur else None, "current_hash": cur["hash"] if cur else None,
            "current_role": cur["role"] if cur else None, "minutes": data.get("minutes")}


# ---------- 검증 중 Pod별 요청 카운터(보여주기 전용) ----------

COUNTER_SETTLE_SEC = 10.0  # 정책 서비스 확인과 같은 전환 뒤 안정 대기(이후 첫 읽기를 기준점으로)


def live_traffic(lc, seen, now):
    """정책이 요청 이동을 측정하는 동안 대시보드가 직접 읽은 old/new Pod 카운터의 증가량. 판정에 쓰지 않는다(공식 숫자는
    정책 service_check만). 기준점 = 대시보드가 전환을 관측하고 10초 지난 뒤 첫 읽기. 카운터가 줄면(재시작) 숫자 없음."""
    d = (lc or {}).get("data") if (lc or {}).get("ok") else None
    sw = ts(seen.get("switch"))
    if not d or sw is None or (now - sw).total_seconds() < COUNTER_SETTLE_SEC:
        return None, None
    ident = {"target_key": d["target_key"], "old_pod": d["old"]["pod"], "new_pod": d["new"]["pod"]}
    base, obs = seen.get("counter_base"), None
    if not base or any(base.get(k) != v for k, v in ident.items()):
        base = obs = {**ident, "old": d["old"]["value"], "new": d["new"]["value"], "at": now.isoformat()}
    do, dn = d["old"]["value"] - base["old"], d["new"]["value"] - base["new"]
    if do < 0 or dn < 0:
        return {"new": None, "old": None, "since": base["at"], "reset": True}, obs
    return {"new": dn, "old": do, "since": base["at"]}, obs


# ---------- 전체 ----------

def derive(raw, seen=None):
    """raw -> (view, new_observations). seen = {"switch", "verified", "pressure"}: 이번 run_id에서 대시보드가 처음 관측한 시각."""
    seen = dict(seen or {})
    now = ts(raw["now"])
    kv = derive_k8s(raw.get("k8s"))
    det_view = derive_detector(raw.get("detector"), now)
    user = derive_user(raw.get("probe"), now)
    rec = derive_recovery(raw, kv, now)
    pr_raw = raw.get("pressure")
    obs = {}
    chaos = ((pr_raw or {}).get("data") or {}).get("chaos") or []
    if (pr_raw or {}).get("ok") and any(c.get("all_injected") for c in chaos) and not seen.get("pressure"):
        obs["pressure"] = raw["now"]
    if rec["switched"] and not seen.get("switch"):
        obs["switch"] = raw["now"]
    if rec["value"] == "VERIFIED" and not seen.get("verified"):
        obs["verified"] = raw["now"]
    seen.update(obs)
    lt, base_obs = live_traffic(raw.get("live_counters"), seen, now)
    if base_obs:
        obs["counter_base"] = seen["counter_base"] = base_obs
    target = rec["target"]
    if target:
        old_hash, new_hash = target["old"], target["new"]
    else:  # 승격 결과 전에는 현재 active/preview를 임시로 쓴다. 둘이 같거나 없으면(전환 직후 등) 누구 것인지 모름
        a, p = kv.get("service_hash"), kv.get("preview_hash")
        old_hash, new_hash = (a, p) if a and p and a != p else (None, None)
    view = {
        "mode": raw.get("mode"), "generated_at": raw["now"], "run_id": det_view.get("run_id"),
        "service_ok": kv["ok"], "k8s_consistent": bool(kv.get("consistent")), "k8s_reason": kv.get("reason"),
        "pods": kv.get("pods") or [], "serving": kv.get("serving") or [],
        "rollout": kv.get("rollout"),
        "detector": det_view, "shadow": derive_shadow(raw.get("detector"), det_view),
        "signal_at": signal_at(raw.get("detector"), det_view.get("active_id"), bool(det_view.get("p2"))),
        "pressure": derive_pressure(pr_raw, seen),
        "recovery": {k: v for k, v in rec.items() if k != "traffic"},
        "traffic": rec["traffic"], "user": user, "live_traffic": lt,
        "pod_roles": {h: role_of(h, target, kv) for h in {p.get("hash") for p in kv.get("pods") or []}
                      | {(kv.get("rollout") or {}).get(k) for k in ("active_selector", "preview_selector", "stable_rs")}
                      | ({target["old"], target["new"]} if target else set()) if h},
        "traffic_series": {**traffic_series(raw.get("traffic"), old_hash, new_hash), "provisional": target is None},
        "cpu": cpu_series(raw.get("cpu"), kv, target, now), "profile": derive_profile(raw.get("profile")),
        "seen": seen,
        "sources": {k: {"ok": (raw.get(k) or {}).get("ok"), "read_at": (raw.get(k) or {}).get("read_at"),
                        "error": (raw.get(k) or {}).get("error")}
                    for k in ("k8s", "detector", "policy", "probe", "pressure", "traffic", "cpu", "profile",
                              "live_counters")},
    }
    view["ui"] = present(view)
    return view, obs


# ---------- 라이브 데모 화면(4분할) - 판정은 위와 같고, 보이는 단어·항목만 정한다 ----------

def _active_ui(det, rec, sig_at):
    v = det.get("value")
    if sig_at and v != "UNKNOWN":
        return {"value": "SIGNAL", "text": "SIGNAL SENT", "sub": "복구 신호 전송됨"}
    if v == "ANOMALY":
        of = det.get("streak_of") or STREAK_OF  # 3연속 뒤에도 연속 수는 계속 늘 수 있어 표시는 of에서 멈춘다
        return {"value": "ANOMALY", "text": f"ANOMALY {min(det.get('streak') or 0, of)}/{of}", "sub": det.get("reason")}
    if v == "NORMAL":
        return {"value": "NORMAL", "text": "NORMAL", "sub": "정상 범위"}
    if v == "SKIPPED" and "selector_unknown" in (det.get("skip_reason") or "") and rec.get("switched"):
        return {"value": "POST-SWITCH", "text": "POST-SWITCH", "sub": "전환 완료 · 평가 보류"}
    sub = {"NO DATA": "실행 대기", "STALE": "최근 평가 없음"}.get(v, det.get("reason") or "판정 불가")
    return {"value": "UNKNOWN", "text": "UNKNOWN", "sub": sub}


def _policy_ui(rec, policy_src_ok):
    """Recovery Policy = 신호를 받아 전환 가능한 운영 조건을 확인하는 계층. 정책 응답·결과 증거만 쓴다."""
    out = rec.get("policy_outcome")
    if not policy_src_ok:
        return {"value": "UNKNOWN", "sub": "Recovery Policy 응답 없음"}
    if not rec.get("detector_ok"):  # 신호 기록을 못 읽으면 '신호 없음'으로 보지 않는다
        return {"value": "UNKNOWN", "sub": "신호 기록 읽기 실패"}
    if rec.get("policy_blocked"):
        return {"value": "BLOCKED", "sub": "정책 차단 상태"}
    if out in PROMOTED:
        return {"value": "ACTION", "sub": "조건 충족 → 승격 요청"}
    if out == "would_pass_record_only":
        return {"value": "RECORD ONLY", "sub": "조건 충족 · 기록 전용(전환 안 함)"}
    if rec.get("delivery") not in (None, "delivered") or out == "duplicate_delivery":
        return {"value": "UNKNOWN", "sub": "신호 전달 결과 불명"}
    if out:
        return {"value": "BLOCKED", "sub": f"전환 안 함: {out}"}
    if rec.get("signal_sent"):
        return {"value": "CHECKING", "sub": "운영 조건 확인 중"}
    return {"value": "IDLE", "sub": "신호 대기"}


# 복구 조건 검증(guarded_policy.check_target + 신호 시각 검사)을 화면 항목으로. 정책은 통과 항목을 따로 기록하지 않고
# 거부 사유(reasons)만 돌려주므로: 사유 없음 + 통과 결과 = 전 항목 통과, 사유가 있으면 그 사유의 항목만 실패로 표시한다.
GUARD_CHECKS = (
    ("timing", "신호가 최신 (15초 이내)", ("future_signal", "stale_signal", "stale_window")),
    ("target", "대상 Pod 동일", ("rollout_mismatch", "active_changed", "preview_changed", "preview_not_paused",
                              "active_pod_count", "preview_pod_count")),
    ("uid", "Pod UID 동일", ("active_uid_changed", "preview_uid_changed")),
    ("term", "종료 중 아님", ("active_terminating", "preview_terminating")),
    ("restart", "재시작 수 동일", ("active_restarted", "preview_restarted")),
    ("ready", "새 Pod Ready", ("preview_not_ready",)),
)
# 이 결과들은 시각·대상 검사를 모두 통과한 뒤의 단계(조치 한도·상태 저장 등)에서 나온다 - guarded_policy._act_locked
AFTER_TARGET = ("blocked_state", "blocked_unresolved", "already_acted_on_target", "action_limit_reached",
                "actions_not_allowed", "state_error_no_promote", "aborted_adapter_precheck")


def _guard_ui(rec, policy_src_ok):
    out, reasons = rec.get("policy_outcome"), [str(r) for r in rec.get("policy_reasons") or []]
    hit = lambda codes: [r for r in reasons if r.startswith(codes)]  # noqa: E731
    if not policy_src_ok or not rec.get("detector_ok"):
        st, value, decision = {}, "UNKNOWN", "확인 불가"
    elif not out:
        st = {}
        value, decision = ("CHECKING", "조건 확인 중") if rec.get("signal_sent") else ("IDLE", "신호 대기")
    elif out in PROMOTED or out == "would_pass_record_only" or out in AFTER_TARGET:
        st = {k: "pass" for k, _, _ in GUARD_CHECKS}
        value, decision = (("ALLOW", "전환 허용") if out in PROMOTED else ("RECORD ONLY", "조건 충족 · 기록 전용")
                           if out == "would_pass_record_only" else ("DENY", f"전환 안 함 ({out})"))
    elif out == "rejected_timing":
        st = {"timing": "fail"}
        value, decision = "DENY", "전환 거부"
    elif out in ("rejected_target", "aborted_final_check"):
        view_err = any(r.startswith("cluster_view_error") for r in reasons)
        st = {k: "fail" if hit(codes) else "unknown" if view_err and k != "timing" else "pass" for k, _, codes in GUARD_CHECKS}
        value, decision = "DENY", "전환 거부"
    else:  # 스키마·실행 맥락·중복 신호 등에서 멈춤 - 대상 검사 전
        st, value, decision = {}, "DENY", f"전환 안 함 ({out})"
    wait = "unknown" if value == "UNKNOWN" else "wait" if value in ("IDLE", "CHECKING") else "skip"
    checks = [{"key": k, "label": label, "state": st.get(k, wait), "why": ", ".join(hit(codes)) or None}
              for k, label, codes in GUARD_CHECKS]
    return {"value": value, "decision": decision, "checks": checks, "at": rec.get("policy_at"),
            "passed": sum(c["state"] == "pass" for c in checks), "total": len(checks)}


def _rollout_ui(rec, k8s_ok, rollout):
    """PROMOTED는 대시보드가 Service/Endpoints 전환을 직접 확인했을 때만."""
    if not k8s_ok:
        return {"value": "UNKNOWN", "sub": "Rollout 읽기 실패"}
    if rec.get("switched"):
        return {"value": "PROMOTED", "sub": "Service가 새 Pod로 전환됨"}
    phase = (rollout or {}).get("phase")
    if rec.get("policy_outcome") in PROMOTED:  # 승격은 정책 결과가 있을 때만 - Progressing은 새 Pod 준비일 수 있다
        return {"value": "PROMOTING", "sub": "승격 중 · Service 전환 확인 중"}
    if phase == "Progressing":
        return {"value": "PROGRESSING", "sub": "새 Pod 준비 중"}
    if phase:
        return {"value": phase.upper(), "sub": {"Paused": "Preview 대기", "Healthy": "정상"}.get(phase, "")}
    return {"value": "UNKNOWN", "sub": "Rollout 상태 없음"}


def _traffic_ui(tr, rec, k8s_ok):
    if not k8s_ok:
        return {"value": "UNKNOWN", "sub": "Service 읽기 실패"}
    if tr is None:
        if rec.get("value") in ("UNKNOWN", "DEGRADED"):
            return {"value": "UNKNOWN", "sub": rec.get("reason") or "판정 불가"}
        return {"value": "WAITING", "sub": "전환 전"}
    v = tr["value"]
    if v == "VERIFIED":
        if rec.get("switched"):
            return {"value": "VERIFIED", "sub": "새 Pod 요청 증가 · 기존 Pod 정지"}
        return {"value": "UNKNOWN", "sub": "Service 전환 미관측"}
    if v == "MEASURING":
        return {"value": "VERIFYING", "sub": "요청 카운터 측정 중"}
    if v == "FAILED":
        return {"value": "NOT VERIFIED", "sub": "트래픽 확인 실패"}
    return {"value": "UNKNOWN", "sub": tr.get("reason") or "증거 불충분"}


def _system(view, pressure, traffic, active):
    rec, user = view["recovery"], view["user"]
    if not view["service_ok"] or not view["sources"]["detector"]["ok"]:
        return {"value": "UNKNOWN", "sub": "상태 읽기 실패"}
    if not view["run_id"]:
        return {"value": "WAITING", "sub": "데모 세션 대기"}
    if (view.get("shadow") or {}).get("sends"):
        return {"value": "DEGRADED", "sub": "SHADOW 권한 위반(전송 기록)"}
    if rec["value"] == "DEGRADED" or traffic["value"] == "NOT VERIFIED":
        return {"value": "DEGRADED", "sub": rec.get("reason") or "복구 확인 실패"}
    if rec["value"] == "VERIFIED":
        return {"value": "HEALTHY", "sub": "복구 완료 · 새 Pod 서비스 중"}
    if rec.get("switched") and user["value"] == "DEGRADED":  # 전환 뒤 품질 저하는 숨기지 않는다
        return {"value": "DEGRADED", "sub": "전환 후 사용자 요청 " + (user.get("reason") or "SLO 위반")}
    if rec.get("switched"):
        return {"value": "VERIFYING", "sub": "트래픽 확인 완료 · 전환 후 60초 사용자 요청 확인 중"
                if traffic["value"] == "VERIFIED" else "전환 완료 · 트래픽 확인 중"}
    if rec["value"] in ("PROMOTING", "SIGNAL SENT") or (active["value"] == "SIGNAL" and rec["value"] != "RECORD ONLY"):
        return {"value": "RECOVERING", "sub": "복구 진행 중"}
    if not view["k8s_consistent"]:
        return {"value": "UNKNOWN", "sub": "Service/Endpoints 읽기 불일치"}
    if view["detector"].get("value") in ("STALE", "UNKNOWN"):
        return {"value": "UNKNOWN", "sub": view["detector"].get("reason") or "감지기 평가 없음"}
    if pressure["value"] in ("INJECTED", "INJECTING"):
        return {"value": "PRESSURE", "sub": "CPU 압박 중" + (" · 사용자 SLO 위반" if user["value"] == "DEGRADED" else "")}
    if user["value"] in ("HEALTHY", "DEGRADED"):
        return {"value": user["value"], "sub": user["reason"]}
    return {"value": "UNKNOWN", "sub": user.get("reason") or "서비스 상태 불명"}


def _node_of(view, h):
    return next((p.get("node") for p in view["pods"] if p.get("hash") == h), None) if h else None


SYSTEM_KO = {"HEALTHY": "정상", "PRESSURE": "압박 중", "RECOVERING": "복구 중", "VERIFYING": "검증 중",
             "DEGRADED": "이상", "WAITING": "세션 대기", "UNKNOWN": "확인 불가"}
VERIFY_KO = {"VERIFIED": "검증 완료", "VERIFYING": "검증 중", "WAITING": "전환 대기", "NOT VERIFIED": "검증 실패",
             "UNKNOWN": "확인 불가"}
ROLLOUT_KO = {"HEALTHY": "정상 동작 중", "PAUSED": "새 Pod 대기", "PROGRESSING": "새 Pod 준비 중", "PROMOTING": "승격 중",
              "PROMOTED": "전환 완료", "DEGRADED": "이상 상태", "UNKNOWN": "확인 불가"}
PHASE_KO = {"Healthy": "정상", "Paused": "대기", "Progressing": "진행 중", "Degraded": "이상"}
DETECTOR_KO = {"cpu-threshold-c": "CPU 임계치", "c3-a-if": "c3-A IF", "c1-if": "c1 IF"}
ROLE_KO = {"new": "새 Pod", "old": "기존 Pod", "active": "기존 Pod", "preview": "새 Pod", "current": "현재 Pod",
           "other": "이전 Pod"}
STEP_KO = {"pressure": "압박 주입", "detect": "이상 감지", "policy": "복구 조건 검증", "promotion": "롤아웃 전환",
           "switch": "서비스 전환", "verify": "요청 이동 검증"}


def _pod_tip(view, h, pod=None):
    """hover 보조 정보(실제 식별자): Pod 이름·hash·Node. 메인 화면에는 역할 이름만."""
    if not h:
        return None
    ps = [p for p in view["pods"] if p.get("hash") == h and (pod is None or p.get("name") == pod)]
    p = next((x for x in ps if not x.get("terminating")), ps[0] if ps else {})
    lines = [f"Pod: {p.get('name') or pod or '확인 불가'}", f"Hash: {h}"]
    if p.get("node"):
        lines.append(f"Node: {p['node']}")
    return "\n".join(lines)


def _role_label(view, h):
    r = (view.get("pod_roles") or {}).get(h)
    return ROLE_KO.get(r) if r else None


def present(view):
    """첨부 디자인(라벤더 글래스) 화면 데이터. 값은 위 판정 결과만 쓰고, 모르면 None(화면에서 N/A)."""
    rec, det, src = view["recovery"], view["detector"], view["sources"]
    k8s_ok, det_ok, pol_ok = bool(view["service_ok"]), bool(src["detector"]["ok"]), bool(src["policy"]["ok"])
    sig_at, pressure, seen = view.get("signal_at"), view["pressure"], view.get("seen") or {}
    prof, shadow, cpu = view.get("profile") or {}, view.get("shadow"), view["cpu"]
    active = _active_ui(det, rec, sig_at)
    traffic = _traffic_ui(view.get("traffic"), rec, k8s_ok)
    tr, tgt = view.get("traffic") or {}, rec.get("target")
    done = traffic["value"] in ("VERIFIED", "NOT VERIFIED")  # 숫자는 정책 판정이 끝났을 때만(측정 중 값 없음)
    new_d, old_d = (tr.get("new_delta"), tr.get("old_delta")) if done else (None, None)
    ro = (view.get("rollout") or {}) if k8s_ok else {}
    svc_hash = (view["serving"] or [{}])[0].get("hash") if k8s_ok and view["k8s_consistent"] else None
    # 이상 감지선: 이번 세션 기록이 우선, 세션 전에는 운영 프로필(phase2_selection/active-profile.json)
    # 세션이 있으면 그 세션 기록의 기준선만 쓴다(없으면 N/A). 운영 프로필 값은 세션 전에만 보여준다
    sess_thr = det.get("threshold") if det.get("p2") and _num(det.get("threshold")) else None
    thr, thr_src = (sess_thr, "이번 세션") if sess_thr is not None else (None, None)
    if not view.get("run_id") and _num((prof.get("active") or {}).get("threshold")):
        thr, thr_src = prof["active"]["threshold"], "운영 프로필"
    outcome = rec.get("policy_outcome")
    pol_text = ("복구 정책 승인 (전환 조건 충족)" if outcome in PROMOTED else "복구 정책: 기록 전용 · 전환 안 함"
                if outcome == "would_pass_record_only" else f"복구 정책: 전환 안 함 ({outcome})" if outcome else "복구 정책 확인")
    fmt = lambda v: "N/A" if v is None else f"+{v:g}"  # noqa: E731
    events = [
        ("pressure", "압박", "CPU 압박 주입 (Chaos Mesh)", pressure.get("at"), pressure["value"] == "UNKNOWN"),
        ("detect", "이상 감지기", "ACTIVE 감지기 신호 전송" + (f" (cpu_mean > {sess_thr:.3f}, 3회 연속)" if sess_thr else ""),
         sig_at, not det_ok),
        ("policy", "복구 정책", pol_text, rec.get("policy_at"), not det_ok or not pol_ok),
        ("promotion", "롤아웃", "롤아웃 승격 시작", rec.get("promote_at"), not det_ok),
        ("switch", "쿠버네티스", "서비스 연결 대상이 새 Pod로 변경됨" if tgt else "서비스 연결 대상 변경",
         seen.get("switch"), not k8s_ok),
        ("verify", "검증", f"요청 이동 검증 완료 (새 Pod {fmt(new_d)} / 기존 Pod {fmt(old_d)})"
         if traffic["value"] == "VERIFIED" else "요청 이동 검증",
         tr.get("finished_at") if traffic["value"] == "VERIFIED" else None, not k8s_ok),
    ]
    ev = [{"key": k, "source": source, "text": text, "at": at, "state": "done" if at else "unknown" if unk else "wait",
           "label": STEP_KO[k]} for k, source, text, at, unk in events]
    # 요청 이동 검증: 정책 측정 중이면 '검증 중', 공식 판정 성공 뒤에만 '검증 완료'
    if traffic["value"] == "VERIFIED" and ev[-1]["state"] == "done":
        ev[-1]["label"] = "검증 완료"
    elif traffic["value"] == "VERIFYING":
        ev[-1]["note"] = "검증 중"
    lt = view.get("live_traffic") if traffic["value"] == "VERIFYING" else None
    ser = view["traffic_series"]
    system = _system(view, pressure, traffic, active)
    # 상단 '복구 상태' 한 단어: 검증 결과가 있으면 그것, 없으면 신호·승격·전환 증거가 있을 때만 '진행 중'
    if traffic["value"] == "VERIFIED":
        recovery = ("DONE", "검증 완료")
    elif traffic["value"] == "NOT VERIFIED" or rec.get("value") == "DEGRADED":
        recovery = ("FAILED", "검증 실패")
    elif not k8s_ok or not det_ok:
        recovery = ("UNKNOWN", "확인 불가")
    elif rec.get("value") in ("SIGNAL SENT", "PROMOTING", "SWITCHED"):  # 기록 전용 신호는 복구 진행이 아니다
        recovery = ("PROGRESS", "진행 중")
    elif system["value"] == "HEALTHY":
        recovery = ("IDLE", "정상")  # 복구가 필요 없는 정상 서비스
    else:
        recovery = ("IDLE", "대기")
    rollout = _rollout_ui(rec, k8s_ok, ro)
    # 세션이 있으면 그 세션에서 실제로 쓴 감지기(Phase 2 역할 또는 c1 IF), 세션 전에만 운영 프로필
    if not view.get("run_id"):
        act_id, sh_id = (prof.get("active") or {}).get("id"), (prof.get("shadow") or {}).get("id")
    elif det.get("p2"):
        act_id, sh_id = det.get("active_id"), (shadow or {}).get("id")
    else:
        act_id, sh_id = "c1-if", None
    return {
        "badge": "MOCK" if view.get("mode") == "mock" else "LIVE",
        "header": {
            "system": {**system, "label": SYSTEM_KO.get(system["value"], system["value"])},
            "policy": {"mode": rec.get("mode") if pol_ok else None, **_policy_ui(rec, pol_ok)},
            "active": {"id": act_id, "label": DETECTOR_KO.get(act_id, act_id), **active},
            "shadow": {"id": sh_id, "label": DETECTOR_KO.get(sh_id, sh_id) if sh_id else "없음",
                       "value": (shadow or {}).get("value", "UNKNOWN"), "met": (shadow or {}).get("met"),
                       "sends": (shadow or {}).get("sends"), "violation": bool((shadow or {}).get("sends"))},
        },
        "kpi": {
            "cpu": {"value": cpu.get("current"), "role": ROLE_KO.get(cpu.get("current_role")),
                    "hash": cpu.get("current_hash"), "tip": _pod_tip(view, cpu.get("current_hash"), cpu.get("current_pod"))},
            "pods": {"ready": ro.get("ready_replicas"), "desired": ro.get("replicas")},
            "service": {"hash": svc_hash},
            "verify": {"value": traffic["value"], "label": VERIFY_KO[traffic["value"]], "new": new_d, "old": old_d},
            "recovery": {"value": recovery[0], "label": recovery[1]},
        },
        "cpu_chart": {"ok": cpu.get("ok"), "reason": cpu.get("reason"), "minutes": cpu.get("minutes"),
                      "series": [{"label": ROLE_KO[s["role"]], "role": s["role"], "hash": s["hash"], "points": s["points"],
                                  "tip": _pod_tip(view, s["hash"], s["pod"])} for s in cpu.get("series") or []],
                      "threshold": thr, "threshold_src": thr_src,
                      "pressure_at": pressure.get("at"), "signal_at": sig_at, "switch_at": seen.get("switch")},
        "rollout": {"name": "vllm-serving" if k8s_ok else None, **rollout,
                    "label": ROLLOUT_KO.get(rollout["value"], rollout["value"]),
                    "phase": PHASE_KO.get(ro.get("phase"), ro.get("phase")), "active": ro.get("active_selector"),
                    "active_role": _role_label(view, ro.get("active_selector")),
                    "active_tip": _pod_tip(view, ro.get("active_selector")),
                    "preview": ro.get("preview_selector"), "stable": ro.get("stable_rs"),
                    "stable_state": None if not (ro.get("stable_rs") and ro.get("active_selector"))
                    else "정상" if ro["stable_rs"] == ro["active_selector"] else "확정 전"},
        "verify": {"value": traffic["value"], "label": VERIFY_KO[traffic["value"]], "sub": traffic["sub"],
                   "new": new_d, "old": old_d, "rule": f"new ≥ {VERIFY_MIN_NEW:g} · old ≤ {VERIFY_MAX_OLD:g}",
                   "from": (tgt or {}).get("old"), "to": (tgt or {}).get("new"), "service_hash": svc_hash,
                   "from_label": ROLE_KO["old"] if tgt else None, "to_label": ROLE_KO["new"] if tgt else None,
                   "new_tip": _pod_tip(view, (tgt or {}).get("new")), "old_tip": _pod_tip(view, (tgt or {}).get("old")),
                   "live": None if not lt else {"new": lt.get("new"), "old": lt.get("old"), "since": lt.get("since")},
                   "window_sec": tr.get("window_sec") if done else None, "samples": tr.get("samples") if done else None,
                   "new_spark": (ser.get("new") or {}).get("points"), "old_spark": (ser.get("old") or {}).get("points")},
        "guard": _guard_ui(rec, pol_ok),
        "events": ev, "next_event": next((i for i, e in enumerate(ev) if e["state"] != "done"), None),
    }
