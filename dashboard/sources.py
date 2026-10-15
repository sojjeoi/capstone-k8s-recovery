"""시연 대시보드 live 수집기 - 전부 읽기 전용. 각 함수는 {"ok", "data", "error", "read_at"}를 돌려준다.

읽는 것(쓰기·실행·신호 없음):
- Kubernetes(kubeconfig): vllm-active/vllm-preview Service selector, vllm-active Endpoints, Rollout status, Pod 목록.
  read_/list_/get_ 호출만 쓴다. vllm-active Service는 Endpoints·Pod 읽기 앞뒤로 두 번 읽어 같을 때만 믿는다(state.derive_k8s).
- 탐지기: 실행기가 PC에 쓰는 experiments/results/<series>/c1-live-evidence-<run_id>.jsonl (줄바꿈으로 끝난 줄만).
- 정책: GET /identity, GET /admin/state(X-Admin-Token = 환경변수 GUARDED_ADMIN_TOKEN, 응답·오류에 토큰을 남기지 않음).
  포트포워드는 실행기 세션(guarded_pilot_session.ps1)이 연다 - 대시보드는 열거나 고치지 않는다.
- 사용자 probe: 실행기가 약 5초마다 다시 받아 쓰는 experiments/results/probe-<run_id>-*-raw.csv를 slo_judge(SLO v3)로 평가.
- CPU 압박: StressChaos 목록(chaos-mesh.org, annotation experiment-run-id-full == run_id) + evidence 옆 summary-<run_id>.json.
- Pod별 요청 그래프: Prometheus query_range(vllm:request_success_total, Pod별 합). 보여주기 전용 - 검증 판정에 쓰지 않는다.
- 검증 중 Pod별 요청 카운터: 정책이 측정 중일 때만 apiserver pod proxy GET /metrics(old/new 2개). 보여주기 전용.
"""
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO / "experiments" / "results"
sys.path.insert(0, str(REPO / "experiments"))
import slo_judge  # noqa: E402 - 동결 판정 함수 재사용(읽기 전용 import)

NAMESPACE, ROLLOUT = "vllm-serving", "vllm-serving"
ACTIVE_SVC, PREVIEW_SVC = "vllm-active", "vllm-preview"
HASH_LABEL = "rollouts-pod-template-hash"
K8S_TIMEOUT = 5
HTTP_TIMEOUT = (2.0, 4.0)
# 환경변수 프록시(HTTP_PROXY 등)를 쓰지 않는다 - admin token·조회가 loopback 밖으로 나가지 않게
_http = requests.Session()
_http.trust_env = False
PROBE_SETTLE_SEC = 0.3


def _now():
    return datetime.now(timezone.utc)


def _result(ok, data=None, error=None):
    return {"ok": ok, "data": data, "error": error, "read_at": _now().isoformat()}


def _ts(s):
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else None


# ---------- Kubernetes ----------

_k8s = {}


def _clients():
    if not _k8s:
        from kubernetes import client, config
        config.load_kube_config()
        _k8s.update(core=client.CoreV1Api(), custom=client.CustomObjectsApi())
    return _k8s["core"], _k8s["custom"]


def _pod_ready(p):
    return any(c.type == "Ready" and c.status == "True" for c in (p.status.conditions or []))


def read_k8s():
    try:
        core, custom = _clients()
        t = K8S_TIMEOUT

        def svc_hash(name):
            return (core.read_namespaced_service(name, NAMESPACE, _request_timeout=t).spec.selector or {}).get(HASH_LABEL)
        before = svc_hash(ACTIVE_SVC)
        ep = core.read_namespaced_endpoints(ACTIVE_SVC, NAMESPACE, _request_timeout=t)
        pods = core.list_namespaced_pod(NAMESPACE, label_selector=HASH_LABEL, _request_timeout=t).items
        ro = custom.get_namespaced_custom_object("argoproj.io", "v1alpha1", NAMESPACE, "rollouts", ROLLOUT,
                                                 _request_timeout=t)
        preview = svc_hash(PREVIEW_SVC)
        after = svc_hash(ACTIVE_SVC)
    except Exception as e:  # noqa: BLE001 - 어떤 실패든 UNKNOWN으로 표시
        return _result(False, error=f"kubernetes: {type(e).__name__}: {str(e)[:160]}")
    status = ro.get("status") or {}
    bg = status.get("blueGreen") or {}
    return _result(True, {
        "active_hash_before": before, "active_hash_after": after, "preview_hash": preview,
        "endpoints": [{"ip": a.ip, "target": a.target_ref.name if a.target_ref else None,
                       "uid": a.target_ref.uid if a.target_ref else None,
                       "kind": a.target_ref.kind if a.target_ref else None}
                      for s in (ep.subsets or []) for a in (s.addresses or [])],
        "pods": [{"name": p.metadata.name, "uid": p.metadata.uid, "hash": (p.metadata.labels or {}).get(HASH_LABEL),
                  "node": p.spec.node_name, "ip": p.status.pod_ip, "phase": p.status.phase, "ready": _pod_ready(p),
                  "terminating": p.metadata.deletion_timestamp is not None} for p in pods],
        "rollout": {"phase": status.get("phase"), "active_selector": bg.get("activeSelector"),
                    "preview_selector": bg.get("previewSelector"), "abort": status.get("abort"),
                    "stable_rs": status.get("stableRS"), "replicas": (ro.get("spec") or {}).get("replicas"),
                    "ready_replicas": status.get("readyReplicas")},
    })


# ---------- 탐지기 evidence ----------

def _complete_lines(path):
    """줄바꿈으로 끝난 줄만(쓰는 중인 마지막 줄 제외). BOM·CRLF 허용."""
    text = path.read_bytes().decode("utf-8-sig")
    return [ln.strip() for ln in text.split("\n")[:-1] if ln.strip()]


def _start_record(path):
    with open(path, "rb") as f:
        first = f.readline()
    if not first.endswith(b"\n"):
        return None
    r = json.loads(first.decode("utf-8-sig"))
    return r if r.get("record_type") == "c1_live_start" else None


def pick_run(results_dir, since):
    """since 이후 시작된 실행 중 가장 늦게 시작한 evidence 파일(이전 실행은 파일이 바뀌어도 무시)."""
    best = None
    for p in [*results_dir.glob("c1-live-evidence-*.jsonl"), *results_dir.glob("*/c1-live-evidence-*.jsonl")]:
        if datetime.fromtimestamp(p.stat().st_mtime, timezone.utc) < since:
            continue
        try:
            s = _start_record(p)
        except (OSError, ValueError):
            continue
        st = _ts((s or {}).get("started_at_utc"))
        if st and st >= since and (best is None or st > best[0]):
            best = (st, p)
    return best[1] if best else None


EVAL_KEYS = ("evaluation_seq", "status", "skip_reason", "score", "is_anomalous", "consecutive_anomalous",
             "condition_met", "score_completed_at_utc", "skipped_at_utc", "window_end_utc")
START_KEYS = ("run_id", "detector", "arm", "actuating_rule", "threshold", "if_threshold", "consecutive_threshold",
              "started_at_utc", "expected_mode", "kept_features", "roles", "active_detector", "thresholds")
SEND_KEYS = ("evaluation_seq", "completed_at_utc", "decision", "delivery", "policy_outcome", "promotion", "target_key",
             "detector_id")
RULE_KEYS = ("value", "threshold", "state", "is_anomalous", "consecutive", "condition_met", "role")


def parse_evidence(lines):
    start, evals, sends, attempts = None, [], [], {}
    for i, ln in enumerate(lines, 1):
        try:
            r = json.loads(ln)
        except ValueError:
            raise ValueError(f"evidence {i}번째 줄 JSON 해석 실패") from None
        rt = r.get("record_type")
        if rt == "c1_live_start" and start is None:
            start = {k: r.get(k) for k in START_KEYS}
        elif rt == "c1_evaluation":
            e = {k: r.get(k) for k in EVAL_KEYS}
            f = r.get("features") or {}
            e["features"] = {k: f.get(k) for k in ("cpu_mean", "cpu_slope", "memory_mean", "memory_slope")}
            # B/C/K 기록은 rules.B, Phase 2 기록은 rules[<detector_id>](role ACTIVE/SHADOW)
            rules = r.get("rules") or {}
            if rules:
                e["rules"] = {k: {f: (v or {}).get(f) for f in RULE_KEYS} for k, v in rules.items()}
            evals.append(e)
        elif rt == "c1_policy_send_attempt":
            # 감지기마다 평가 번호가 같을 수 있어 (detector_id, 평가 번호)로 짝짓는다
            attempts.setdefault((r.get("detector_id"), r.get("evaluation_seq")), r.get("attempted_at_utc"))
        elif rt == "c1_policy_send_result":
            key = (r.get("detector_id"), r.get("evaluation_seq"))
            sends.append({**{k: r.get(k) for k in SEND_KEYS}, "attempted_at_utc": attempts.get(key)})
    done = {(s["detector_id"], s["evaluation_seq"]) for s in sends}
    return {"start": start, "evaluations": evals, "send_results": sends, "send_pending": bool(set(attempts) - done),
            "send_attempts": [{"detector_id": d, "evaluation_seq": q, "attempted_at_utc": at}
                              for (d, q), at in attempts.items()]}


def read_detector(results_dir, since):
    try:
        p = pick_run(results_dir, since)
        if p is None:
            return _result(True, None)
        d = parse_evidence(_complete_lines(p))
    except (OSError, ValueError, UnicodeDecodeError) as e:
        return _result(False, error=f"evidence: {e}")
    d["file"] = p.name
    out = _result(True, d)
    out["path"] = p  # collect()만 쓰고 화면 JSON에는 넣지 않는다(summary는 evidence와 같은 폴더에서 찾는다)
    return out


# ---------- CPU 압박(Chaos Mesh StressChaos) ----------

CHAOS = ("chaos-mesh.org", "v1alpha1", "stresschaos")
RUN_ANNOTATION = "experiment-run-id-full"


def _chaos_view(item):
    conds = (item.get("status") or {}).get("conditions") or []
    inj = next((c for c in conds if c.get("type") == "AllInjected"), None)
    return {"name": (item.get("metadata") or {}).get("name"),
            "all_injected": None if inj is None else inj.get("status") == "True"}


def read_pressure(run_id, evidence_path):
    """이 실행의 CPU 압박 근거. summary의 injection_started_at(실행기가 CPU 상승까지 확인한 시각)이 있으면 그것을,
    없으면 StressChaos의 AllInjected 여부만 쓴다(CR 생성 시각은 압박 시작 증거가 아니라 쓰지 않는다)."""
    if not run_id:
        return _result(True, None)
    data = {"injection_started_at": None, "summary_error": None, "chaos": None, "chaos_error": None}
    if evidence_path is not None:
        p = evidence_path.parent / f"summary-{run_id}.json"
        if p.is_file():
            try:
                s = json.loads(p.read_text(encoding="utf-8-sig"))
                if s.get("run_id") not in (None, run_id):
                    raise ValueError("run_id 불일치")
                data["injection_started_at"] = s.get("injection_started_at")
            except (OSError, ValueError) as e:  # 쓰는 중이거나 깨진 summary - 이번 주기는 없는 것으로
                data["summary_error"] = f"summary: {type(e).__name__}"
    try:
        _, custom = _clients()
        items = custom.list_namespaced_custom_object(CHAOS[0], CHAOS[1], NAMESPACE, CHAOS[2],
                                                     _request_timeout=K8S_TIMEOUT).get("items") or []
        data["chaos"] = [_chaos_view(i) for i in items
                         if ((i.get("metadata") or {}).get("annotations") or {}).get(RUN_ANNOTATION) == run_id]
    except Exception as e:  # noqa: BLE001 - 권한·CRD 없음 등은 '압박 없음'이 아니라 '모름'
        data["chaos_error"] = f"stresschaos: {type(e).__name__}"
    return _result(True, data)


# ---------- Pod별 요청 수(Prometheus, 그래프 전용 - 검증 판정에는 쓰지 않는다) ----------

TRAFFIC_QUERY = 'sum by (pod) (vllm:request_success_total{namespace="vllm-serving"})'
TRAFFIC_STEP_SEC = 15
TRAFFIC_MAX_SEC = 15 * 60


# ---------- 파드별 CPU(Prometheus, 감지기와 같은 rate 창) / 운영 프로필 ----------

CPU_QUERY = 'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="vllm-serving",container="vllm"}[60s]))'
RANGES_MIN = (5, 30, 60, 360)


def read_cpu(prom_url, minutes, get=_http.get, now=None):
    if not prom_url or minutes not in RANGES_MIN:
        return _result(True, None)
    end = now or _now()
    step = max(15, minutes * 60 // 240)  # 화면 폭에 맞게 점 240개 안팎
    try:
        r = get(prom_url.rstrip("/") + "/api/v1/query_range", timeout=HTTP_TIMEOUT, allow_redirects=False,
                params={"query": CPU_QUERY, "start": (end - timedelta(minutes=minutes)).timestamp(),
                        "end": end.timestamp(), "step": step})
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise ValueError(body.get("errorType") or "status")
        series = {}
        for s in (body.get("data") or {}).get("result") or []:
            pod = (s.get("metric") or {}).get("pod")
            if pod and pod not in series:
                series[pod] = [[float(t), float(v)] for t, v in s.get("values") or []]
    except Exception as e:  # noqa: BLE001
        return _result(False, error=f"prometheus cpu: {type(e).__name__} (포트포워드 9090 확인)")
    return _result(True, {"series": series, "step_sec": step, "minutes": minutes})


def read_profile(results_dir, path=None):
    """Phase 2 선택 결과로 만든 운영 프로필(ACTIVE/SHADOW·임계값) = 실행기가 읽는 그 파일(--active-profile).
    세션 전에도 지금 설정을 보여주기 위한 것. path가 없으면 results_dir/phase2_selection/active-profile.json."""
    p = Path(path) if path else results_dir / "phase2_selection" / "active-profile.json"
    if not p.is_file():
        return _result(True, None)
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
        dets = [{"id": x.get("detector_id"), "role": x.get("role"), "threshold": x.get("threshold")}
                for x in d.get("detectors") or []]
    except (OSError, ValueError, AttributeError) as e:
        return _result(False, error=f"profile: {type(e).__name__}")
    return _result(True, {"detectors": dets})


def read_traffic(prom_url, start_iso, get=_http.get, now=None):
    st = _ts(start_iso)
    if not prom_url or st is None:
        return _result(True, None)
    end = now or _now()
    begin = max(st - timedelta(seconds=60), end - timedelta(seconds=TRAFFIC_MAX_SEC))
    try:
        r = get(prom_url.rstrip("/") + "/api/v1/query_range", timeout=HTTP_TIMEOUT, allow_redirects=False,
                params={"query": TRAFFIC_QUERY, "start": begin.timestamp(), "end": end.timestamp(),
                        "step": TRAFFIC_STEP_SEC})
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise ValueError(body.get("errorType") or "status")
        series = {}
        for s in (body.get("data") or {}).get("result") or []:
            pod = (s.get("metric") or {}).get("pod")
            if pod and pod not in series:  # 같은 pod 중복 series는 첫 것만(합치지 않는다)
                series[pod] = [[float(t), float(v)] for t, v in s.get("values") or []]
    except Exception as e:  # noqa: BLE001
        return _result(False, error=f"prometheus: {type(e).__name__} (포트포워드 9090 확인)")
    return _result(True, {"series": series, "step_sec": TRAFFIC_STEP_SEC})


# ---------- 정책 ----------

def _slim_action(a):
    sc = a.get("service_check") or {}
    vd = sc.get("verdict") or {}
    return {"status": a.get("status"), "run_id": a.get("run_id"), "reserved_at": a.get("reserved_at"),
            "promote_called_at": a.get("promote_called_at"),
            "service_finished_at": a.get("service_finished_at"),
            "service_check": None if not sc else {
                "ok": sc.get("ok"), "validity": sc.get("validity"), "counter_window_sec": sc.get("counter_window_sec"),
                "endpoint_samples": sc.get("endpoint_samples"),
                "verdict": {k: vd.get(k) for k in ("verified", "reasons", "new_pod_increase", "old_pod_increase")}}}


def read_policy(base_url, token, get=_http.get):
    base = base_url.rstrip("/")
    try:
        r = get(base + "/identity", timeout=HTTP_TIMEOUT, allow_redirects=False)
        r.raise_for_status()
        ident = {k: r.json().get(k) for k in ("role", "version", "mode")}
    except Exception as e:  # noqa: BLE001
        return _result(False, error=f"policy /identity: {type(e).__name__} (포트포워드 확인)")
    data = {"identity": ident, "state": None, "state_error": None}
    if not token:
        data["state_error"] = "GUARDED_ADMIN_TOKEN 없음 - admin state 미조회"
        return _result(True, data)
    try:
        r = get(base + "/admin/state", headers={"X-Admin-Token": token}, timeout=HTTP_TIMEOUT, allow_redirects=False)
        if r.status_code in (401, 403):
            data["state_error"] = f"admin token 거부(HTTP {r.status_code})"
            return _result(True, data)
        r.raise_for_status()
        st = r.json()
    except Exception as e:  # noqa: BLE001 - 오류 문구에 토큰이 들어가지 않도록 형식명만
        data["state_error"] = f"admin state: {type(e).__name__}"
        return _result(True, data)
    data["state"] = {"state_available": st.get("state_available"), "reason": st.get("reason"),
                     "mode": st.get("mode"), "blocked": bool(st.get("blocked")),
                     "actions": {k: _slim_action(v) for k, v in (st.get("actions") or {}).items()}}
    return _result(True, data)


# ---------- 사용자 probe ----------

def summarize_probe(rows):
    """slo_judge 창(마지막 요청 기준 직전 60초)의 현재 값."""
    if not rows:
        return None
    last = rows[-1]["sent_at"]
    win = [r for r in rows if last - timedelta(seconds=slo_judge.WINDOW_SEC) < r["sent_at"] <= last]
    pt = slo_judge.evaluate(win)[-1]
    ok = sum(1 for r in win if r["success"])
    return {"total": len(win), "success": ok, "fail": len(win) - ok, "p95": pt["p95"],
            "success_rate": pt["success_rate"], "latency_evaluable": pt["latency_evaluable"],
            "latency_violating": pt["latency_violating"], "availability_violating": pt["availability_violating"],
            "last_sent_at": last.isoformat(), "window_start": win[0]["sent_at"].isoformat()}


def read_probe(results_dir, run_id):
    if not run_id:
        return _result(True, None)
    files = sorted(results_dir.glob(f"probe-{run_id}-*-raw.csv"))
    if not files:
        return _result(True, None)
    try:
        rows = _stable_load(files[-1])
        if rows is None:
            return _result(False, error="probe csv: 실행기가 다시 쓰는 중 - 이번 주기 판정 보류")
        if any(r["sent_at"].tzinfo is None for r in rows):
            raise ValueError("sent_at 시간대 없음")
        return _result(True, summarize_probe(rows))
    except (OSError, ValueError, KeyError) as e:
        return _result(False, error=f"probe csv: {type(e).__name__}: {e}")


def _stable_load(path, settle_sec=PROBE_SETTLE_SEC, tries=3, sleep=time.sleep, wall=time.time):
    """실행기는 CSV를 write_text로 통째로 다시 쓴다(원자적이지 않음). 마지막 수정 뒤 settle_sec가 지났고 읽기 앞뒤
    크기·수정시각이 같을 때만 그 내용을 쓴다 - 쓰는 도중의 앞부분만 읽어 실패 요청을 놓치지 않도록."""
    for _ in range(tries):
        s1 = path.stat()
        if wall() - s1.st_mtime < settle_sec:
            sleep(settle_sec)
            continue
        rows = slo_judge.load_raw(path)
        s2 = path.stat()
        if (s1.st_size, s1.st_mtime_ns) == (s2.st_size, s2.st_mtime_ns):
            return rows
    return None


# ---------- 검증 중 Pod별 요청 카운터(보여주기 전용, 판정에 쓰지 않음) ----------
# 전환 직후 Prometheus는 기존 Pod를 더 이상 수집하지 않는다(ServiceMonitor가 vllm-active/preview 대상).
# 그래서 정책이 측정하는 동안만 apiserver pod proxy GET으로 두 Pod의 /metrics를 직접 읽는다(읽기 전용, 수집 주기마다 2회).

POD_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
COUNTER_METRIC = "vllm:request_success_total"


def sum_counter(text):
    """한 Pod /metrics의 요청 성공 카운터(모든 계열) 합. 계열이 없거나 값이 깨지면 None(0으로 채우지 않음)."""
    total, n = 0.0, 0
    for ln in (text or "").splitlines():
        if not ln.startswith(COUNTER_METRIC) or ln[len(COUNTER_METRIC):len(COUNTER_METRIC) + 1] not in ("{", " "):
            continue
        try:
            v = float((ln.rsplit("}", 1)[-1] if "{" in ln else ln[len(COUNTER_METRIC):]).split()[0])
        except (IndexError, ValueError):
            return None
        if not (v >= 0 and v != float("inf")):
            return None
        total, n = total + v, n + 1
    return total if n else None


def verifying_pods(k8s, policy, run_id):
    """정책이 이번 실행 조치의 요청 이동을 측정 중(verified_service_pending)일 때만 old/new Pod 이름. 아니면 None.
    old/new는 정책 조치의 target_key(<active hash>-><preview hash>#<preview pod uid>)로 정한다(hash 고정 없음)."""
    from state import parse_target
    st = ((policy or {}).get("data") or {}).get("state") or {}
    if not (k8s or {}).get("ok") or not run_id:
        return None
    for key, a in (st.get("actions") or {}).items():
        t = parse_target(key)
        if a.get("run_id") != run_id or a.get("status") != "verified_service_pending" or not t:
            continue
        pods = (k8s.get("data") or {}).get("pods") or []
        old = [p for p in pods if p.get("hash") == t["old"] and not p.get("terminating")]
        new = [p for p in pods if p.get("hash") == t["new"] and p.get("uid") == t["new_uid"]]
        if len(old) == 1 and len(new) == 1:
            return {"target_key": key, "old": old[0]["name"], "new": new[0]["name"]}
        return None
    return None


def _pod_metrics(pod):
    if not POD_NAME_RE.match(pod or ""):
        raise ValueError("pod name")
    core, _ = _clients()
    # kubernetes 36 client: response_type 인자 없음 - 본문을 그대로 받는다(_preload_content=False)
    r = core.api_client.call_api(f"/api/v1/namespaces/{NAMESPACE}/pods/{pod}:8000/proxy/metrics", "GET",
                                 auth_settings=["BearerToken"], _return_http_data_only=True,
                                 _preload_content=False, _request_timeout=K8S_TIMEOUT)
    return r.data.decode("utf-8")


def read_live_counters(k8s, policy, run_id, get_metrics=_pod_metrics):
    tg = verifying_pods(k8s, policy, run_id)
    if tg is None:
        return _result(True, None)
    try:
        vals = {r: sum_counter(get_metrics(tg[r])) for r in ("old", "new")}
    except Exception as e:  # noqa: BLE001 - 권한(pods/proxy)·연결 실패는 숫자 없음으로
        return _result(False, error=f"pod counter: {type(e).__name__}")
    if any(v is None for v in vals.values()):
        return _result(False, error="pod counter: 요청 카운터 계열 없음")
    return _result(True, {"target_key": tg["target_key"], **{r: {"pod": tg[r], "value": vals[r]} for r in vals}})


def collect(cfg, minutes=5):
    """live 원자료 한 묶음. mock 값은 절대 섞지 않는다. minutes = 파드별 CPU 그래프 범위."""
    det = read_detector(cfg["results_dir"], cfg["since"])
    path = det.pop("path", None)
    start = (det.get("data") or {}).get("start") or {}
    run_id = start.get("run_id")
    k8s, pol = read_k8s(), read_policy(cfg["policy_url"], cfg.get("token"))
    out = {"mode": "live", "k8s": k8s, "detector": det, "policy": pol,
           "probe": read_probe(cfg["results_dir"], run_id), "pressure": read_pressure(run_id, path),
           "traffic": read_traffic(cfg.get("prom_url"), start.get("started_at_utc")),
           "cpu": read_cpu(cfg.get("prom_url"), minutes),
           "profile": read_profile(cfg["results_dir"], cfg.get("active_profile")),
           "live_counters": read_live_counters(k8s, pol, run_id)}
    out["now"] = _now().isoformat()  # 판정 기준 시각은 모든 읽기가 끝난 뒤(느린 조회가 오래된 값을 신선하게 보이게 하지 않도록)
    return out
