"""HPA 비교(H/W/G) 연결 모듈 공통부 (2026-10-22). 동결 소스는 수정하지 않고 불러서만 쓴다.

설계: docs/design/HPA-COMPARISON-DESIGN-DRAFT-20261022.md (§0 실행 범위 확정).
- H: 서비스 Pod 1개 + CPU HPA(최대 2), preview 없음 / W: Ready Pod 2개 상시, HPA·전환 없음 / G: 기존 동결 하네스(별도 경로).
- 이 파일은 H/W 세션과 분석이 함께 쓰는 쿠버네티스 조회·상태 복원·기록 함수를 모았다.
"""
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXP = HERE.parent
ROOT = EXP.parent
if str(EXP) not in sys.path:
    sys.path.insert(0, str(EXP))

RESULTS = EXP / "results" / "hpa_compare"
NS, ROLLOUT = "vllm-serving", "vllm-serving"
HPA_NAME = "vllm-hpa-compare"
HPA_MANIFEST = HERE / "hpa-100.yaml"
OBS_SEC = 600.0                       # 동결 하네스 상수(rt.OBSERVATION_SEC)와 같다 - 모든 군에 동일
STAND_SEC = 300.0                     # 주입 전 준비 Pod 가 모두 Ready 인 채 안정된 시간(비용 구간 시작 기준)
POD_CPU_REQ, POD_MEM_REQ_GIB = 2.0, 4.0
TOLERANT_OVERLAY = ROOT / "gitops" / "apps" / "vllm-serving" / "overlays" / "network-tolerant"
BASE_ROLLOUT = ROOT / "gitops" / "apps" / "vllm-serving" / "rollout.yaml"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def kubectl(*args, input_text=None, check=True, timeout=90):
    r = subprocess.run(["kubectl", *args], input=input_text, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} 실패(exit {r.returncode}): {r.stderr.strip()[:400]}")
    return r


def kjson(*args, timeout=90):
    return json.loads(kubectl(*args, "-o", "json", timeout=timeout).stdout)


def wait_until(fn, timeout, poll=5.0, what=""):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = fn()
            if last:
                return last
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        time.sleep(poll)
    raise TimeoutError(f"대기 시간 초과({timeout:.0f}s): {what} (마지막: {last})")


def _pod_ready(p) -> bool:
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in (p.get("status") or {}).get("conditions") or [])


def pod_info(p) -> dict:
    st = p.get("status") or {}
    ready_t = next((c.get("lastTransitionTime") for c in st.get("conditions") or [] if c.get("type") == "Ready" and c.get("status") == "True"), None)
    return {"name": p["metadata"]["name"], "uid": p["metadata"]["uid"], "phase": st.get("phase"), "ready": _pod_ready(p),
            "ready_since": ready_t, "created": p["metadata"].get("creationTimestamp"),
            "deleting": p["metadata"].get("deletionTimestamp"), "node": (p.get("spec") or {}).get("nodeName"),
            "hash": (p["metadata"].get("labels") or {}).get("rollouts-pod-template-hash"),
            "restarts": sum(c.get("restartCount", 0) for c in st.get("containerStatuses") or [])}


def vllm_pods() -> list:
    items = kjson("get", "pods", "-n", NS, "-l", "app=vllm-serving")["items"]
    return sorted((pod_info(p) for p in items), key=lambda x: x["name"])


def rollout_state() -> dict:
    ro = kjson("get", "rollout", ROLLOUT, "-n", NS)
    st, spec = ro.get("status") or {}, ro.get("spec") or {}
    c0 = (spec.get("template", {}).get("spec", {}).get("containers") or [{}])[0]
    bg = st.get("blueGreen") or {}
    return {"phase": st.get("phase"), "replicas_spec": spec.get("replicas"), "available": st.get("availableReplicas"),
            "active": bg.get("activeSelector"), "preview": bg.get("previewSelector"), "current_hash": st.get("currentPodHash"),
            "stable_rs": st.get("stableRS"), "generation": ro["metadata"].get("generation"), "observed": st.get("observedGeneration"),
            "readiness_timeout": (c0.get("readinessProbe") or {}).get("timeoutSeconds"),
            "liveness_timeout": (c0.get("livenessProbe") or {}).get("timeoutSeconds"),
            "requests": (c0.get("resources") or {}).get("requests"), "limits": (c0.get("resources") or {}).get("limits"),
            "image": c0.get("image"), "scale_down_delay": (spec.get("strategy", {}).get("blueGreen") or {}).get("scaleDownDelaySeconds"),
            "preview_replica_count": (spec.get("strategy", {}).get("blueGreen") or {}).get("previewReplicaCount"),
            "annotations": (spec.get("template", {}).get("metadata") or {}).get("annotations")}


def hpa_state():
    r = kubectl("get", "hpa", HPA_NAME, "-n", NS, "-o", "json", check=False)
    if r.returncode != 0:
        return None
    h = json.loads(r.stdout)
    st = h.get("status") or {}
    return {"current": st.get("currentReplicas"), "desired": st.get("desiredReplicas"), "last_scale": st.get("lastScaleTime"),
            "metrics": st.get("currentMetrics"), "conditions": [{k: c.get(k) for k in ("type", "status", "reason", "message")}
                                                                 for c in st.get("conditions") or []],
            "spec_min": (h.get("spec") or {}).get("minReplicas"), "spec_max": (h.get("spec") or {}).get("maxReplicas")}


def chaos_crs() -> list:
    out = []
    for kind in ("networkchaos", "stresschaos"):
        r = kubectl("get", kind, "-n", NS, "-o", "json", check=False)
        if r.returncode == 0:
            out += [{"kind": kind, "name": i["metadata"]["name"], "pods": ((i.get("spec") or {}).get("selector") or {}).get("pods"),
                     "latency": ((i.get("spec") or {}).get("delay") or {}).get("latency"),
                     "records": [x.get("id") for x in ((((i.get("status") or {}).get("experiment") or {}).get("containerRecords")) or [])],
                     "conditions": {c.get("type"): c.get("status") for c in (i.get("status") or {}).get("conditions") or []}}
                    for i in json.loads(r.stdout).get("items", [])]
    return out


def endpoints_count(svc="vllm-active") -> int:
    ep = kjson("get", "endpoints", svc, "-n", NS)
    return sum(len(s.get("addresses") or []) for s in ep.get("subsets") or [])


def top_pods() -> dict:
    """metrics API(metrics-server)의 Pod 별 CPU(milli)·메모리 사용량. 실패하면 빈 값."""
    r = kubectl("get", "--raw", f"/apis/metrics.k8s.io/v1beta1/namespaces/{NS}/pods", check=False)
    if r.returncode != 0:
        return {}
    out = {}
    for i in json.loads(r.stdout).get("items", []):
        c = (i.get("containers") or [{}])[0]
        u = c.get("usage") or {}
        out[i["metadata"]["name"]] = {"cpu": u.get("cpu"), "memory": u.get("memory"), "window": i.get("window"), "timestamp": i.get("timestamp")}
    return out


def snapshot() -> dict:
    return {"t": now_iso(), "rollout": rollout_state(), "pods": vllm_pods(), "hpa": hpa_state(), "chaos": chaos_crs(),
            "endpoints_active": endpoints_count("vllm-active")}


# ---------------- 사전 점검 ----------------
def other_experiment_problems() -> list:
    """다른 실험이 실제로 실행 중인지(오래된 변경 흔적만으로는 막지 않는다). 읽기 전용."""
    probs = []
    pods = kjson("get", "pods", "-n", NS)["items"]
    for p in pods:
        n = p["metadata"]["name"]
        if n.startswith(("ramp-probe", "ramp-inj", "probe-", "loadgen")) and (p.get("status") or {}).get("phase") in ("Running", "Pending"):
            probs.append(f"running_load_or_probe_pod:{n}")
    probs += [f"chaos_present:{c['kind']}/{c['name']}" for c in chaos_crs()]
    try:
        mode = kubectl("get", "deploy", "guarded-recovery-policy", "-n", NS, "-o",
                       "jsonpath={.spec.template.spec.containers[0].env[?(@.name=='GUARDED_MODE')].value}").stdout.strip()
        if mode != "record_only":
            probs.append(f"policy_mode={mode}")
    except Exception as e:  # noqa: BLE001
        probs.append(f"policy_mode_unreadable:{e}")
    ps = subprocess.run(["powershell", "-NoProfile", "-Command",
                         "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'n2_run\\.py|guarded_pilot_session|n2_stage|run_guarded_pilot|ramp\\.py' "
                         "-and $_.CommandLine -notmatch 'Get-CimInstance|Where-Object' -and $_.ProcessId -ne $PID } | ForEach-Object { $_.ProcessId }"],
                        capture_output=True, text=True, timeout=60)
    live = [x for x in ps.stdout.split() if x.strip().isdigit()]
    if live:
        probs.append(f"local_experiment_processes:{live}")
    return probs


# ---------------- 군 구성 / 복원 ----------------
def hpa_delete() -> None:
    kubectl("delete", "hpa", HPA_NAME, "-n", NS, "--ignore-not-found=true", "--wait=true", check=False)


def set_replicas(n: int) -> None:
    kubectl("patch", "rollout", ROLLOUT, "-n", NS, "--type", "merge", "-p", json.dumps({"spec": {"replicas": n}}))


def settled(want_pods: int, *, hash_eq=True) -> bool:
    """Rollout Healthy, active=preview(preview ReplicaSet 없음), vllm Pod 가 want_pods 개 모두 Ready, active endpoint 도 want_pods."""
    ro = rollout_state()
    pods = [p for p in vllm_pods() if not p["deleting"]]
    if ro["phase"] != "Healthy" or (hash_eq and ro["active"] != ro["preview"]):
        return False
    return len(pods) == want_pods and all(p["ready"] for p in pods) and endpoints_count("vllm-active") == want_pods


def restore_base(timeout=900.0) -> dict:
    """다음 세션의 시작 상태: HPA 없음, replicas 1, Pod 1개 Ready, Healthy, active=preview, 11초 프로필은 그대로."""
    hpa_delete()
    ro = rollout_state()
    if ro["replicas_spec"] != 1:
        set_replicas(1)
    wait_until(lambda: settled(1), timeout, 5.0, "base 복원(Pod 1개 Ready, Healthy)")
    return snapshot()


def setup_hpa(timeout=240.0) -> dict:
    kubectl("apply", "-f", str(HPA_MANIFEST))

    def active():
        h = hpa_state()
        return bool(h) and any(c["type"] == "ScalingActive" and c["status"] == "True" for c in h["conditions"]) and bool(h.get("metrics"))
    wait_until(active, timeout, 5.0, "HPA ScalingActive + 메트릭")
    return hpa_state()


def setup_w(timeout=900.0) -> dict:
    set_replicas(2)
    wait_until(lambda: settled(2), timeout, 5.0, "W: Ready Pod 2개")
    return snapshot()


def active_node():
    pods = [p for p in vllm_pods() if p["ready"] and not p["deleting"]]
    return pods[0]["node"] if len(pods) == 1 else None


def promote_via_policy_pod() -> None:
    kubectl("exec", "-n", NS, "deploy/guarded-recovery-policy", "--", "kubectl-argo-rollouts", "promote", ROLLOUT, "-n", NS, timeout=60)


def restore_delay_if_needed(want=30, timeout=300.0):
    """하네스가 정리 때 보류(restore_held)해 둔 scaleDownDelaySeconds 를 기준값으로 되돌린다. Healthy·active=preview·Pod 1개일 때만."""
    ro = rollout_state()
    if ro["scale_down_delay"] == want:
        return None
    if not (ro["phase"] == "Healthy" and ro["active"] == ro["preview"] and settled(1)):
        raise RuntimeError(f"scaleDownDelaySeconds={ro['scale_down_delay']} 인데 Rollout 이 정상 단일 상태가 아님 - 자동 복원 안 함")
    kubectl("patch", "rollout", ROLLOUT, "-n", NS, "--type", "merge", "-p", json.dumps({"spec": {"strategy": {"blueGreen": {"scaleDownDelaySeconds": want}}}}))
    wait_until(lambda: rollout_state()["scale_down_delay"] == want and settled(1), timeout, 5.0, "delay 복원")
    return {"restored_from": ro["scale_down_delay"], "to": want, "at": now_iso()}


def ensure_active_on(node="sj-worker", timeout=1500.0) -> dict:
    """서비스 Pod 를 지정 노드에 둔다(세션 시작 전 준비). 동결 G 하네스의 preview 노드 메모리 사전검사(가용 1.5GiB)는 sj-worker 가
    calico-node 메모리 사용으로 기준 아래라, 서비스 Pod 가 sj-worker 에 있어야 preview 가 sj-worker2 에 놓여 통과한다.
    이미 그 노드면 아무것도 하지 않고, 아니면 동결 헬퍼로 새 리비전을 만들어 승격한다(Pod 위치만 바뀐다)."""
    rec = {"target": node, "before": active_node()}
    if rec["before"] == node:
        rec["moved"] = False
        return rec
    import blue_green_prep as bgp
    prep = bgp.prepare_preview_with_rollback(ROLLOUT, NS)
    rec["prep"] = {k: prep.get(k) for k in ("ready", "t_prep_start", "t_preview_ready", "prep_duration_sec", "created_pod_hash", "rollback_attempted", "rollback_ok")}
    if not prep.get("ready"):
        raise RuntimeError(f"위치 이동용 preview 준비 실패: {rec['prep']}")
    promote_via_policy_pod()
    wait_until(lambda: settled(1), timeout, 5.0, "위치 이동 승격 뒤 Healthy 단일 Pod")
    rec["after"], rec["moved"] = active_node(), True
    return rec


def is_tolerant_profile(ro=None) -> bool:
    ro = ro or rollout_state()
    return ro["readiness_timeout"] == 11 and ro["liveness_timeout"] == 11


def jdump(path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8", newline="\n") as fh:      # 덮어쓰기 금지(원기록 보존)
        fh.write(json.dumps(obj, ensure_ascii=False, indent=1, default=str) + "\n")


# ---------------- 시계열 기록기 ----------------
class Sampler:
    """주기적으로 Pod·Rollout·HPA·metrics-server·Pod 별 요청 카운터·Chaos CR 을 읽어 jsonl 로 쌓는다(읽기 전용)."""

    def __init__(self, path, period=10.0, counter_every=15.0):
        self.path, self.period, self.counter_every = Path(path), period, counter_every
        self._stop = threading.Event()
        self._t = None
        self._last_counter = 0.0
        self.errors = 0

    def _counters(self):
        import run_timed_switch_recovery_trial as rt
        return rt.vllm_request_counters()

    def _loop(self):
        with open(self.path, "x", encoding="utf-8", newline="\n") as fh:
            while not self._stop.is_set():
                rec = {"t": now_iso()}
                for k, fn in (("pods", vllm_pods), ("rollout", rollout_state), ("hpa", hpa_state), ("top", top_pods), ("chaos", chaos_crs)):
                    try:
                        rec[k] = fn()
                    except Exception as e:  # noqa: BLE001
                        rec[k] = None
                        rec.setdefault("errors", []).append(f"{k}:{type(e).__name__}")
                        self.errors += 1
                if time.monotonic() - self._last_counter >= self.counter_every:
                    self._last_counter = time.monotonic()
                    try:
                        rec["counters"] = self._counters()
                    except Exception as e:  # noqa: BLE001
                        rec["errors"] = rec.get("errors", []) + [f"counters:{type(e).__name__}"]
                        self.errors += 1
                fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
                fh.flush()
                self._stop.wait(self.period)

    def start(self):
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def stop(self):
        self._stop.set()
        if self._t:
            self._t.join(timeout=60)


def read_timeline(path) -> list:
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def _dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def pod_seconds(timeline, t0: datetime, t1: datetime) -> dict:
    """공통 구간 [t0, t1] 안에서 Pod 별 유지 시간(생성~삭제 또는 마지막 관측, 구간으로 자름)과 Ready 시간(초).
    Ready 시간은 Ready 전이 시각이 기록된 Pod 만 계산한다. 10 s 주기 관측의 해상도 한계가 있다."""
    seen = {}
    for rec in timeline:
        ts = _dt(rec["t"])
        for p in rec.get("pods") or []:
            e = seen.setdefault(p["name"], {"created": _dt(p["created"]), "deleting": None, "ready_since": None, "last_seen": ts})
            e["last_seen"] = ts
            if p.get("deleting") and e["deleting"] is None:
                e["deleting"] = _dt(p["deleting"])
            if p.get("ready") and p.get("ready_since") and e["ready_since"] is None:
                e["ready_since"] = _dt(p["ready_since"])
    out = {}
    for name, e in seen.items():
        end = e["deleting"] or e["last_seen"]
        a, b = max(e["created"], t0), min(end, t1)
        alive = max(0.0, (b - a).total_seconds())
        ra = max(e["ready_since"], t0) if e["ready_since"] else None
        ready = max(0.0, (b - ra).total_seconds()) if ra else 0.0
        out[name] = {"alive_sec": round(alive, 1), "ready_sec": round(ready, 1), "created": e["created"].isoformat(),
                     "ready_since": e["ready_since"].isoformat() if e["ready_since"] else None}
    return out


def cost_from(pods: dict) -> dict:
    alive = sum(v["alive_sec"] for v in pods.values())
    return {"pod_alive_sec_total": round(alive, 1), "cpu_request_core_sec": round(alive * POD_CPU_REQ, 1),
            "memory_request_gib_sec": round(alive * POD_MEM_REQ_GIB, 1),
            "pod_ready_sec_total": round(sum(v["ready_sec"] for v in pods.values()), 1),
            "note": "예약량(요청량×유지 시간). 실제 사용량은 usage 항목에 따로 기록. 금액 환산 없음"}


def counter_delta(timeline, t_a: datetime, t_b: datetime) -> dict:
    """t_a 이전 마지막 카운터 표본과 t_b 이전 마지막 표본의 Pod 별 차이."""
    def last_before(t):
        best = None
        for rec in timeline:
            if rec.get("counters") and _dt(rec["t"]) <= t:
                best = rec
        return best
    a, b = last_before(t_a), last_before(t_b)
    if not a or not b:
        return {}
    out = {}
    for pod in sorted(set(a["counters"]) | set(b["counters"])):
        va, vb = a["counters"].get(pod), b["counters"].get(pod)
        out[pod] = {"before": va, "after": vb, "increase": (vb - va) if (va is not None and vb is not None) else (vb if va is None else None)}
    return out


def slo_cumulative(probe_csv, t_inj_iso, obs_end_iso, **kw) -> dict:
    import slo_episodes
    import slo_judge
    rows = slo_judge.load_raw(Path(probe_csv))
    return slo_episodes.violation_episodes(rows, _dt(t_inj_iso), _dt(obs_end_iso), **kw)
