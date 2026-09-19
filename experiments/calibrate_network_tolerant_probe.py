#!/usr/bin/env python3
"""network_tolerant probe timeout calibration - 격리 calibration pod 방식 (2026-09-19 재작성).

사전 등록: docs/design/phase8-blue-green-preflight-incident.md §42 - 판정 규칙·절차·정리 검증은 측정 전에
고정됐고 이 파일의 상수가 그 값이다(측정 뒤 조정 금지).

이전 버전은 overlay 적용·promote를 사람이 먼저 하고 **active pod**에 지연을 걸었다. 지금 상태(active =
timeout 1초 probe)에서 그대로 실행하면 probe가 실패해 active pod가 재시작된다. Rollout preview 방식은
live Rollout spec을 바꿔야 하고, 대기 preview가 있는 동안 지연이 만든 VLLMTargetDown이 recovery-policy의
promote_preview를 유발할 수 있다(policy.py). 그래서 Rollout과 무관한 격리 pod(`vllm-calib-*`)를 쓴다:

- overlay가 렌더한 Rollout의 spec.template으로 Pod를 만든다(후보 timeout 포함, 나머지 동일). 라벨은 어떤
  Service·Rollout selector에도 걸리지 않는다 -> Prometheus가 스크랩하지 않아 VLLMTargetDown이 없다.
- 이 pod에만 NetworkChaos를 건다(이름 지정). kubelet의 readiness/liveness probe는 pod spec대로 이 pod에서
  돈다. probe 동등 요청은 worker 노드에서 ssh로 보낸다(calibration_node_probe.py).
- Rollout·Service·운영 vLLM pod·recovery-policy를 바꾸는 호출이 이 파일에 없다(테스트가 고정).

사용법(정확히 하나):
  --dry-run         오프라인 - overlay 렌더 diff·pod 매니페스트 검증·계획 출력. 클러스터 접근 없음.
  --preflight-only  읽기 전용 클러스터 확인(Node·Rollout·pod·Chaos CR·context·라이브 template·worker ssh 체인).
  --execute         실제 calibration 1회(이 pod·CR만 만들고 반드시 지운다).
종료 코드: 0 = 측정 완료(PASS/MARGINAL), 1 = 사전 확인·사용법 문제, 2 = FAIL(하드 실패 - 부분 데이터는 저장),
3 = 정리 실패(H8), 130 = 중단(정리 후).
"""
import argparse
import copy
import json
import math
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import yaml

import calibration_node_probe
from active_pod_resolver import NAMESPACE
from network_degrade_adapter import (
    STAGES, create_network_chaos, delete_network_chaos, does_chaos_exist, is_stage_injected)

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_DIR = REPO_ROOT / "gitops" / "apps" / "vllm-serving"
OVERLAY_DIR = BASE_DIR / "overlays" / "network-tolerant"
BASE_RESOURCE_FILES = ["rollout.yaml", "service.yaml", "rbac.yaml", "servicemonitor.yaml",
                       "prometheusrule.yaml", "alertmanagerconfig.yaml"]
PROBE_CONFIG = REPO_ROOT / "chaos" / "probe-config.yaml"
RESULTS_DIR = Path(__file__).resolve().parent / "results" / "pilot"
WORKER_SSH_HOST = "capstone-worker"

# ---- 사전 등록 상수(§42.4~§42.6) - 측정 뒤 조정 금지 ------------------------------------------------
POLL_INTERVAL_SEC = 3.0
POD_READY_TIMEOUT_SEC = 600.0
WARMUP_SETTLE_SEC = 30.0
BASELINE_SEC = 60.0
RECOVERY_SEC = 30.0
INJECTED_TIMEOUT_SEC = 30.0
CHAOS_GONE_TIMEOUT_SEC = 60.0
POD_GONE_TIMEOUT_SEC = 120.0
HARD_LIMIT_SEC = 40 * 60.0
HEALTH_INTERVAL_SEC = 1.0
COMPLETION_INTERVAL_SEC = 1.0
HEALTH_TIMEOUT_SEC = 30.0
COMPLETION_TIMEOUT_SEC = 60.0
CR_EXPIRY_SLACK_SEC = 60.0
MARGIN_RATIO = 1.25
MARGIN_ABS_SEC = 1.5
T_CAP_SEC = 15.0
MIN_WORST_COMPLETED_FRACTION = 0.8
MIN_WORST_OK_SAMPLES = 30
NODE_BAD_CONDITIONS = ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable")
ROLLOUT_KEY = ("Rollout", "vllm-serving")
EXPECTED_OVERLAY_CHANGES = {
    "/spec/template/spec/containers/0/readinessProbe/timeoutSeconds",
    "/spec/template/spec/containers/0/livenessProbe/timeoutSeconds",
}
MISSING = "<없음>"


class CalibrationAbort(Exception):
    """하드 실패(H1~H9) - 측정을 중단하고 정리한다."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class OverlayError(Exception):
    """overlay 렌더가 기대(timeout 2경로만 변경)와 다름."""


# ---- overlay 렌더 diff ----------------------------------------------------------------------------
class _YamlLoader(yaml.SafeLoader):
    """kustomize 출력의 따옴표 없는 `=`(matchType: =)를 PyYAML이 YAML 1.1 value 태그로 읽어 실패하는 것을 막는다."""


_YamlLoader.add_constructor("tag:yaml.org,2002:value", lambda loader, node: loader.construct_scalar(node))


def load_yaml_docs(text: str) -> list:
    return [d for d in yaml.load_all(text, Loader=_YamlLoader) if d]


def _index(docs: list) -> dict:
    return {(d["kind"], d["metadata"]["name"]): d for d in docs}


def diff_paths(a, b, path: str = "") -> list:
    """(path, old, new) 목록 - 리스트는 같은 길이일 때 원소별로, 다르면 통째로 비교한다."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for key in sorted(set(a) | set(b)):
            if key not in a:
                out.append((f"{path}/{key}", MISSING, b[key]))
            elif key not in b:
                out.append((f"{path}/{key}", a[key], MISSING))
            else:
                out += diff_paths(a[key], b[key], f"{path}/{key}")
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += diff_paths(x, y, f"{path}/{i}")
        return out
    return [] if a == b else [(path, a, b)]


def render_overlay(run: Callable = subprocess.run, overlay_dir: Path = OVERLAY_DIR) -> dict:
    """`kubectl kustomize`로 overlay를 로컬 렌더링한다(클러스터 접근 없음)."""
    r = run(["kubectl", "kustomize", "--load-restrictor=LoadRestrictionsNone", str(overlay_dir)],
            capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise OverlayError(f"kubectl kustomize 실패: {r.stderr.strip()[:300]}")
    return _index(load_yaml_docs(r.stdout))


def load_base(base_dir: Path = BASE_DIR) -> dict:
    docs = []
    for name in BASE_RESOURCE_FILES:
        docs += load_yaml_docs((base_dir / name).read_text(encoding="utf-8"))
    return _index(docs)


def verify_overlay(rendered: dict, base: dict) -> list:
    """렌더 결과가 raw base와 **정확히** Rollout의 readiness/liveness timeoutSeconds 2경로만 다른지 검증한다.
    다른 리소스·다른 필드(CPU·모델·startupProbe·이미지·서비스 selector 등)가 바뀌면 OverlayError."""
    if set(rendered) != set(base):
        raise OverlayError(f"리소스 집합이 다름: 렌더에만 {sorted(set(rendered) - set(base))}, "
                           f"base에만 {sorted(set(base) - set(rendered))}")
    rollout_changes = []
    for key in sorted(base):
        changes = diff_paths(base[key], rendered[key])
        if key == ROLLOUT_KEY:
            rollout_changes = changes
        elif changes:
            raise OverlayError(f"{key}이(가) 바뀜(허용 안 됨): {changes[:3]}")
    paths = {p for p, _, _ in rollout_changes}
    if paths != EXPECTED_OVERLAY_CHANGES:
        raise OverlayError(f"Rollout 변경 경로가 기대와 다름: 예상 {sorted(EXPECTED_OVERLAY_CHANGES)}, "
                           f"실제 {sorted(paths)}")
    for path, _old, new in rollout_changes:
        if not isinstance(new, (int, float)) or new <= 0:
            raise OverlayError(f"{path} 새 값이 양수 숫자가 아님: {new!r}")
    return rollout_changes


def overlay_candidate_timeouts(rendered_rollout: dict) -> dict:
    container = rendered_rollout["spec"]["template"]["spec"]["containers"][0]
    return {"readiness": container["readinessProbe"]["timeoutSeconds"],
            "liveness": container["livenessProbe"]["timeoutSeconds"]}


# ---- calibration pod ------------------------------------------------------------------------------
def calibration_run_id(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return "calib-net-tolerant-" + now.strftime("%Y%m%dt%H%M%Sz")


def build_calibration_pod(rollout: dict, run_id: str, node_name: str,
                          candidate_sec: Optional[float] = None) -> dict:
    """overlay가 렌더한 Rollout의 spec.template으로 격리 Pod를 만든다. metadata(이름·라벨)와 같은 노드 고정
    nodeSelector만 다르고, candidate_sec을 주면 readiness/liveness timeoutSeconds만 덮어쓴다."""
    template = copy.deepcopy(rollout["spec"]["template"])
    spec = template["spec"]
    spec["nodeSelector"] = {"kubernetes.io/hostname": node_name}
    if candidate_sec is not None:
        value = int(candidate_sec) if float(candidate_sec).is_integer() else float(candidate_sec)
        for probe in ("readinessProbe", "livenessProbe"):
            spec["containers"][0][probe]["timeoutSeconds"] = value
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": f"vllm-{run_id}", "namespace": NAMESPACE,
                         "labels": {"app": "vllm-calibration", "experiment-run-id": run_id,
                                    "phase8-role": "calibration"}},
            "spec": spec}


def _labels_match(selector: dict, labels: dict) -> bool:
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


def verify_calibration_pod(pod: dict, base: dict) -> None:
    """Pod가 base Rollout template과 (nodeSelector·두 timeout 외에) 같고, 어떤 Service·Rollout selector에도
    걸리지 않으며 소유자가 없는지 검증한다. 어긋나면 OverlayError."""
    base_spec = base[ROLLOUT_KEY]["spec"]["template"]["spec"]
    allowed = {"/nodeSelector", "/containers/0/readinessProbe/timeoutSeconds",
               "/containers/0/livenessProbe/timeoutSeconds"}
    extra = [(p, o, n) for p, o, n in diff_paths(base_spec, pod["spec"]) if p not in allowed]
    if extra:
        raise OverlayError(f"calibration pod가 base template과 다른 필드가 있음: {extra[:3]}")
    labels = pod["metadata"]["labels"]
    if "rollouts-pod-template-hash" in labels or pod["metadata"].get("ownerReferences"):
        raise OverlayError("calibration pod는 Rollout/RS 소속이 될 수 없음")
    selectors = [d["spec"].get("selector") for k, d in base.items() if k[0] == "Service"]
    selectors.append(base[ROLLOUT_KEY]["spec"]["selector"].get("matchLabels"))
    for selector in selectors:
        if _labels_match(selector, labels):
            raise OverlayError(f"calibration pod 라벨 {labels}이(가) selector {selector}에 걸림")


# ---- 분석(순수) -----------------------------------------------------------------------------------
def percentile(values: list, q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


def _r(x):
    return None if x is None else round(x, 4)


def summarize_health(samples: list) -> dict:
    health = [s for s in samples if s.get("kind") == "health"]
    ok = [s["latency"] for s in health if s.get("status") == 200 and not s.get("error")]
    return {"n": len(health), "ok": len(ok), "errors": len(health) - len(ok), "min": _r(min(ok) if ok else None),
            "p50": _r(percentile(ok, 0.5)), "p95": _r(percentile(ok, 0.95)), "max": _r(max(ok) if ok else None)}


def summarize_completion(samples: list) -> dict:
    comp = [s for s in samples if s.get("kind") == "completion"]
    ok = [s["latency"] for s in comp if s.get("status") == 200 and not s.get("error")]
    return {"n": len(comp), "ok": len(ok), "success_rate": _r(len(ok) / len(comp)) if comp else None,
            "p50": _r(percentile(ok, 0.5)), "p95": _r(percentile(ok, 0.95)), "max": _r(max(ok) if ok else None)}


def required_timeout(l_max: float) -> float:
    return max(MARGIN_RATIO * l_max, l_max + MARGIN_ABS_SEC)


def recommend(windows: list, candidate_sec: float, hard_fail: Optional[dict]) -> dict:
    """§42.6 판정 규칙 그대로. n=1이므로 모든 권고는 잠정(provisional)이다."""
    worst_name = STAGES[-1]["name"]
    worst = next((w for w in windows if w["name"] == worst_name), None)
    kubelet_failures = sum(w["kubelet"]["probe_failures"] for w in windows)
    client_failures = sum(w["health"]["errors"] for w in windows)
    if hard_fail:
        outcome = "FAIL"
    elif kubelet_failures or client_failures:
        outcome = "MARGINAL"
    else:
        outcome = "PASS"
    out = {"run_outcome": outcome, "candidate_sec": candidate_sec, "worst_stage": worst_name,
           "kubelet_probe_failures": kubelet_failures, "client_health_failures": client_failures,
           "L_max": None, "T_required": None, "T_min": None, "T_cap": T_CAP_SEC,
           "recommendation": "NONE", "reason": "", "provisional": True}
    if hard_fail:
        out["reason"] = f"하드 실패({hard_fail.get('code')}) - 권고 없음, 원인 보고"
        return out
    if worst is None or worst["completed_fraction"] < MIN_WORST_COMPLETED_FRACTION \
            or worst["health"]["ok"] < MIN_WORST_OK_SAMPLES:
        out["reason"] = "stage-4 창이 80% 미만 완료이거나 성공 /health가 30개 미만 - 권고 없음"
        return out
    l_max = worst["health"]["max"]
    out["L_max"] = l_max
    if kubelet_failures and l_max < candidate_sec:
        out["reason"] = (f"kubelet probe 실패가 있는데 probe 동등 요청의 L_max({l_max}s)가 후보({candidate_sec}s) "
                         "미만 - 측정 불일치, 권고 없음")
        return out
    t_req = required_timeout(l_max)
    t_min = math.ceil(t_req - 1e-9)
    out["T_required"], out["T_min"] = _r(t_req), t_min
    if t_min > T_CAP_SEC:
        out["recommendation"] = "INSUFFICIENT"
        out["reason"] = f"T_min {t_min}s > 상한 {T_CAP_SEC}s - timeout만으로는 불가(failureThreshold/period 재설계 필요)"
    elif t_min > candidate_sec:
        out["recommendation"] = "RAISE"
        out["reason"] = f"후보 {candidate_sec}s는 마진 부족 - 권고 {t_min}s"
    elif t_min == candidate_sec:
        out["recommendation"] = "KEEP"
        out["reason"] = f"후보 {candidate_sec}s가 규칙의 최소값과 같음"
    else:
        out["recommendation"] = "LOWER"
        out["reason"] = f"후보 {candidate_sec}s는 과도 - 권고 {t_min}s"
    return out


# ---- 클러스터 스냅샷·하드 실패 평가 ---------------------------------------------------------------
def parse_snapshot(ns_items: list, node_items: list, context) -> dict:
    """kubectl get -o json 결과(List.items)를 이 도구가 쓰는 단순 dict로 줄인다."""
    snap = {"nodes": {}, "rollout": None, "services": {}, "pods": {}, "chaos": [], "events": [],
            "context": context}
    for node in node_items:
        conds = {c["type"]: c["status"] for c in node.get("status", {}).get("conditions", [])}
        snap["nodes"][node["metadata"]["name"]] = {
            "ready": conds.get("Ready") == "True",
            "bad": [t for t in NODE_BAD_CONDITIONS if conds.get(t) == "True"]}
    for item in ns_items:
        kind, md = item.get("kind"), item.get("metadata", {})
        if kind == "Pod":
            st = item.get("status", {})
            cs = (st.get("containerStatuses") or [{}])[0]
            snap["pods"][md["name"]] = {
                "uid": md["uid"], "labels": md.get("labels", {}), "phase": st.get("phase"),
                "ready": any(c.get("type") == "Ready" and c.get("status") == "True" for c in st.get("conditions", [])),
                "restarts": cs.get("restartCount", 0), "terminated": bool((cs.get("lastState") or {}).get("terminated")),
                "deleting": "deletionTimestamp" in md, "node": item.get("spec", {}).get("nodeName"),
                "ip": st.get("podIP")}
        elif kind == "Service":
            snap["services"][md["name"]] = item.get("spec", {}).get("selector")
        elif kind == "Rollout" and md.get("name") == ROLLOUT_KEY[1]:
            st, bg = item.get("status", {}), item.get("status", {}).get("blueGreen", {})
            snap["rollout"] = {"generation": md.get("generation"), "phase": st.get("phase"),
                               "abort": bool(st.get("abort")), "current_hash": st.get("currentPodHash"),
                               "stable_rs": st.get("stableRS"), "active_selector": bg.get("activeSelector"),
                               "preview_selector": bg.get("previewSelector")}
        elif kind == "NetworkChaos":
            snap["chaos"].append(md["name"])
        elif kind == "Event" and item.get("involvedObject", {}).get("kind") == "Pod":
            snap["events"].append({"pod": item["involvedObject"]["name"], "reason": item.get("reason"),
                                   "message": item.get("message", ""), "count": item.get("count", 1)})
    return snap


def unhealthy_count(snap: dict, pod_name: str) -> int:
    """Readiness/Liveness probe 실패 이벤트 누적 횟수(Startup probe 실패는 제외)."""
    return sum(e["count"] for e in snap["events"]
               if e["pod"] == pod_name and e["reason"] == "Unhealthy"
               and e["message"].startswith(("Readiness probe failed", "Liveness probe failed")))


@dataclass
class RunState:
    pod_name: str
    created: bool = False
    pod_uid: Optional[str] = None
    ready_seen: bool = False
    chaos_names: set = field(default_factory=set)


def evaluate_violations(baseline: dict, snap: dict, state: RunState) -> list:
    """§42.6 즉시 실패 조건 H1~H5·H9를 평가한다 -> [(코드, 설명)]. (H6~H8은 흐름에서 직접 발생.)"""
    v = []
    for name, node in snap["nodes"].items():
        if not node["ready"] or node["bad"]:
            v.append(("H3", f"Node {name}: Ready={node['ready']} 이상 조건={node['bad']}"))
    for name, before in baseline["pods"].items():
        now = snap["pods"].get(name)
        if now is None or now["uid"] != before["uid"]:
            v.append(("H4", f"운영 pod {name} 삭제/교체"))
            continue
        if now["restarts"] > before["restarts"]:
            v.append(("H4", f"운영 pod {name} 재시작 {before['restarts']}->{now['restarts']}"))
        if not now["ready"] or now["deleting"]:
            v.append(("H4", f"운영 pod {name} NotReady/삭제 중"))
    if snap["rollout"] != baseline["rollout"]:
        old, new = baseline["rollout"] or {}, snap["rollout"] or {}
        changed = {k: old.get(k, MISSING) for k in set(old) | set(new) if old.get(k) != new.get(k)}
        v.append(("H5", f"Rollout 변경(이전 값): {changed}"))
    if snap["services"] != baseline["services"]:
        v.append(("H5", f"Service selector 변경: {snap['services']} (이전 {baseline['services']})"))
    unexpected = sorted(p for p in snap["pods"] if p not in baseline["pods"] and p != state.pod_name)
    if unexpected:
        v.append(("H5", f"예상 밖 신규 pod: {unexpected}"))
    foreign = sorted(c for c in snap["chaos"] if c not in state.chaos_names)
    if foreign:
        v.append(("H9", f"이 run 소유가 아닌 Chaos CR: {foreign}"))
    if snap["context"] is not None:
        v.append(("H9", f"recovery-policy context가 null이 아님: {snap['context']}"))
    if state.created:
        pod = snap["pods"].get(state.pod_name)
        if pod is None:
            v.append(("H1", "calibration pod가 사라짐"))
        else:
            if state.pod_uid is None:
                state.pod_uid = pod["uid"]
            elif pod["uid"] != state.pod_uid:
                v.append(("H1", f"calibration pod UID 변경 {state.pod_uid}->{pod['uid']}"))
            if pod["restarts"] > 0 or pod["terminated"] or pod["phase"] in ("Failed", "Unknown"):
                v.append(("H1", f"calibration pod 재시작/종료: restarts={pod['restarts']} "
                                f"terminated={pod['terminated']} phase={pod['phase']}"))
            if pod["ready"]:
                state.ready_seen = True
            elif state.ready_seen:
                v.append(("H2", "calibration pod가 Ready를 잃음(readiness probe 실패)"))
    return v


def preflight_problems(snap: dict) -> list:
    """§42.1 시작 조건 - 하나라도 어긋나면 아무 것도 만들지 않는다."""
    p = []
    for name, node in snap["nodes"].items():
        if not node["ready"] or node["bad"]:
            p.append(f"Node {name} 이상: Ready={node['ready']} {node['bad']}")
    r = snap["rollout"]
    if r is None:
        p.append("Rollout vllm-serving 없음")
    else:
        if r["phase"] != "Healthy" or r["abort"]:
            p.append(f"Rollout이 Healthy가 아님: phase={r['phase']} abort={r['abort']}")
        if r["active_selector"] != r["preview_selector"]:
            p.append(f"preview가 존재함: active={r['active_selector']} preview={r['preview_selector']}")
        if r["current_hash"] != r["stable_rs"]:
            p.append(f"단일 revision이 아님: current={r['current_hash']} stable={r['stable_rs']}")
    vllm = {n: x for n, x in snap["pods"].items() if x["labels"].get("app") == "vllm-serving"}
    if len(vllm) != 1 or not all(x["ready"] for x in vllm.values()):
        p.append(f"vLLM pod가 정확히 1개·Ready가 아님: {sorted(vllm)}")
    others = sorted(n for n, x in snap["pods"].items()
                    if x["labels"].get("app") not in ("vllm-serving", "recovery-policy"))
    if others:
        p.append(f"실험용/예상 밖 pod가 있음: {others}")
    rp = {n: x for n, x in snap["pods"].items() if x["labels"].get("app") == "recovery-policy"}
    if len(rp) != 1 or not all(x["ready"] for x in rp.values()):
        p.append(f"recovery-policy pod가 정확히 1개·Ready가 아님: {sorted(rp)}")
    if snap["chaos"]:
        p.append(f"Chaos CR이 남아 있음: {snap['chaos']}")
    if snap["context"] is not None:
        p.append(f"recovery-policy context가 null이 아님: {snap['context']}")
    return p


def template_fidelity_problems(live_template: dict, base_template: dict) -> list:
    """라이브 Rollout template이 base와 (annotation·K8s 기본값 외에) 같은지 - calibration pod가 실제 운영 pod와
    같은 spec으로 측정하는지 확인한다. API 서버가 채우는 `ports[].protocol: TCP`는 의미가 같은 기본값이라
    양쪽에서 지운다(2026-09-19 실측: 라이브 template의 유일한 차이였음)."""
    def norm(t):
        t = copy.deepcopy(t)
        t.get("metadata", {}).pop("annotations", None)
        for container in t.get("spec", {}).get("containers", []):
            for port in container.get("ports", []):
                if port.get("protocol") == "TCP":
                    del port["protocol"]
        return t
    return [f"{p}: {o!r} -> {n!r}" for p, o, n in diff_paths(norm(base_template), norm(live_template))]


# ---- 클러스터 접근 --------------------------------------------------------------------------------
class KubectlCluster:
    """kubectl subprocess로 읽는다. 쓰기는 calibration pod 생성·삭제뿐이다(Rollout·Service 변경 메서드 없음)."""

    def __init__(self, run: Callable = subprocess.run, retries: int = 2):
        self._run, self._retries = run, retries

    def _kubectl(self, args: list, input_text: Optional[str] = None):
        return self._run(["kubectl", *args], capture_output=True, text=True, encoding="utf-8",
                         input=input_text)

    def _json(self, args: list):
        last = None
        for _ in range(self._retries + 1):
            r = self._kubectl(args)
            if r.returncode == 0:
                try:
                    return json.loads(r.stdout)
                except ValueError as e:
                    last = str(e)
            else:
                last = r.stderr.strip()[:200]
            time.sleep(1.0)
        raise CalibrationAbort("H9", f"kubectl {' '.join(args[:3])} 실패: {last}")

    def snapshot(self) -> dict:
        ns = self._json(["get", "pods,services,events,rollouts.argoproj.io,networkchaos.chaos-mesh.org",
                         "-n", NAMESPACE, "-o", "json"])
        nodes = self._json(["get", "nodes", "-o", "json"])
        ctx = self._json(["get", "--raw", f"/api/v1/namespaces/{NAMESPACE}/services/recovery-policy:8080"
                                         "/proxy/admin/experiment-run"])
        return parse_snapshot(ns.get("items", []), nodes.get("items", []), ctx.get("current"))

    def rollout_template(self) -> dict:
        return self._json(["get", "rollout", ROLLOUT_KEY[1], "-n", NAMESPACE, "-o", "json"])["spec"]["template"]

    def create_pod(self, manifest: dict) -> None:
        r = self._kubectl(["create", "-f", "-"], input_text=json.dumps(manifest))
        if r.returncode != 0:
            raise CalibrationAbort("H7", f"calibration pod 생성 실패: {r.stderr.strip()[:300]}")

    def delete_pod(self, name: str) -> None:
        r = self._kubectl(["delete", "pod", name, "-n", NAMESPACE, "--wait=false", "--ignore-not-found"])
        if r.returncode != 0:
            raise RuntimeError(f"pod 삭제 호출 실패: {r.stderr.strip()[:300]}")

    def pod_exists(self, name: str) -> bool:
        r = self._kubectl(["get", "pod", name, "-n", NAMESPACE, "-o", "name"])
        if r.returncode == 0:
            return True
        if "NotFound" in r.stderr or "not found" in r.stderr:
            return False
        raise RuntimeError(f"pod 조회 실패: {r.stderr.strip()[:200]}")


@dataclass
class ChaosOps:
    create: Callable = create_network_chaos
    delete: Callable = delete_network_chaos
    exists: Callable = does_chaos_exist
    injected: Callable = is_stage_injected


class _SshHandle:
    def __init__(self, proc, script_text: str):
        self._proc, self._records, self._aborted, self._stderr = proc, [], False, ""
        self._summary_seen = False
        proc.stdin.write(script_text)
        proc.stdin.close()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._err_reader = threading.Thread(target=self._read_err, daemon=True)
        self._reader.start()
        self._err_reader.start()

    def _read(self):
        for line in self._proc.stdout:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind") == "summary":
                self._summary_seen = True
            else:
                self._records.append(rec)
        self._proc.wait()

    def _read_err(self):
        self._stderr = self._proc.stderr.read()

    def done(self) -> bool:
        return not self._reader.is_alive()

    def abort(self):
        self._aborted = True
        try:
            self._proc.terminate()
        except Exception:
            pass

    def result(self) -> list:
        self._reader.join(timeout=10)
        return list(self._records)

    def error(self) -> Optional[str]:
        if self._aborted:
            return None
        self._reader.join(timeout=10)
        self._err_reader.join(timeout=5)
        if self._proc.returncode not in (0, None) or not self._summary_seen:
            return f"rc={self._proc.returncode} summary={self._summary_seen} stderr={self._stderr.strip()[:300]}"
        return None


class SshNodeProbe:
    """worker 노드에서 calibration_node_probe.py를 `python3 -`로 실행한다(스크립트는 stdin, 인자는 base64 JSON)."""

    def __init__(self, host: str = WORKER_SSH_HOST, ssh: str = "ssh", popen: Callable = subprocess.Popen):
        self.host, self.ssh, self._popen = host, ssh, popen

    def command(self, params: dict) -> list:
        return [self.ssh, "-o", "BatchMode=yes", "-o", "LogLevel=ERROR", "-o", "ServerAliveInterval=15",
                self.host, "python3", "-", calibration_node_probe.encode_params(params)]

    def start(self, params: dict) -> _SshHandle:
        script = "# -*- coding: utf-8 -*-\n" + Path(calibration_node_probe.__file__).read_text(encoding="utf-8")
        proc = self._popen(self.command(params), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True, encoding="utf-8")
        return _SshHandle(proc, script)


@dataclass
class Deps:
    cluster: object
    chaos: ChaosOps
    probe: object
    clock: Callable = time.monotonic
    sleep: Callable = time.sleep
    now_iso: Callable = lambda: datetime.now(timezone.utc).isoformat()


@dataclass
class Config:
    run_id: str
    pod_manifest: dict
    candidate_sec: float
    candidate_source: str
    probe_payload: dict


# ---- 실행 ----------------------------------------------------------------------------------------
def _wait(d: Deps, predicate: Callable, timeout: float, poll: float = 2.0) -> bool:
    deadline = d.clock() + timeout
    while True:
        try:
            if predicate():
                return True
        except Exception:
            pass
        if d.clock() >= deadline:
            return False
        d.sleep(poll)


def _cleanup(d: Deps, cfg: Config, state: RunState, baseline: dict) -> dict:
    out = {"chaos_deleted": {}, "pod_deleted": None, "problems": [], "ok": False}
    for name in sorted(state.chaos_names):
        try:
            d.chaos.delete(name)
        except Exception as e:
            out["problems"].append(f"CR {name} 삭제 호출 실패: {type(e).__name__}: {e}")
        gone = _wait(d, lambda n=name: not d.chaos.exists(n), CHAOS_GONE_TIMEOUT_SEC)
        out["chaos_deleted"][name] = gone
        if not gone:
            out["problems"].append(f"CR {name}이(가) {CHAOS_GONE_TIMEOUT_SEC:.0f}초 내 소멸하지 않음")
    if state.created:
        try:
            d.cluster.delete_pod(state.pod_name)
        except Exception as e:
            out["problems"].append(f"calibration pod 삭제 호출 실패: {type(e).__name__}: {e}")
        gone = _wait(d, lambda: not d.cluster.pod_exists(state.pod_name), POD_GONE_TIMEOUT_SEC)
        out["pod_deleted"] = gone
        if not gone:
            out["problems"].append(f"calibration pod가 {POD_GONE_TIMEOUT_SEC:.0f}초 내 소멸하지 않음")
    try:
        final = d.cluster.snapshot()
        residual = evaluate_violations(baseline, final, RunState(pod_name=state.pod_name))
        out["problems"] += [f"사후 스냅샷 불일치 {c}: {m}" for c, m in residual]
        if state.pod_name in final["pods"]:
            out["problems"].append("사후 스냅샷에 calibration pod가 남아 있음")
    except Exception as e:
        out["problems"].append(f"사후 스냅샷 실패: {type(e).__name__}: {e}")
    out["ok"] = not out["problems"]
    return out


def run_calibration(deps: Deps, cfg: Config, log: Callable = print) -> dict:
    d = deps
    started = d.clock()
    deadline = started + HARD_LIMIT_SEC
    name = cfg.pod_manifest["metadata"]["name"]
    result = {"run_id": cfg.run_id, "tool": "calibrate_network_tolerant_probe", "preregistration": "§42",
              "started_at": d.now_iso(), "candidate_sec": cfg.candidate_sec,
              "candidate_source": cfg.candidate_source, "stages": [dict(s) for s in STAGES],
              "windows": [], "hard_fail": None, "cleanup": None, "analysis": None}
    baseline = d.cluster.snapshot()
    problems = preflight_problems(baseline)
    if problems:
        result["hard_fail"] = {"code": "PREFLIGHT", "detail": "; ".join(problems)}
        result["analysis"] = recommend([], cfg.candidate_sec, result["hard_fail"])
        result["finished_at"] = d.now_iso()
        return result
    result["baseline"] = {"pods": {n: {k: p[k] for k in ("uid", "restarts", "ready")}
                                   for n, p in baseline["pods"].items()},
                          "rollout": baseline["rollout"], "services": baseline["services"]}
    state = RunState(pod_name=name)
    interrupted = None
    # kubelet probe 실패의 누적 기준선 - 창 "시작 시점 대비"가 아니라 "이전 창이 끝난 시점 대비"로 세서, 창 사이 공백
    # (CR 생성·AllInjected 대기 중)에 생긴 실패도 다음 창에 귀속시킨다(허용 실패 0인데 과소 집계되면 위험).
    unhealthy = {"last": 0}

    def check() -> dict:
        snap = d.cluster.snapshot()
        if d.clock() > deadline:
            raise CalibrationAbort("H9", f"하드 상한 {HARD_LIMIT_SEC:.0f}초 초과")
        violations = evaluate_violations(baseline, snap, state)
        if violations:
            raise CalibrationAbort(violations[0][0], "; ".join(f"{c}: {m}" for c, m in violations))
        return snap

    def hold(seconds: float):
        end = d.clock() + seconds
        while d.clock() < end:
            check()
            d.sleep(min(POLL_INTERVAL_SEC, max(0.0, end - d.clock())))

    def run_window(window_name: str, duration: float, stage: Optional[dict]):
        snap0 = check()
        ip = snap0["pods"][name]["ip"]
        before = unhealthy["last"]
        params = {"ip": ip, "port": 8000, "duration_sec": duration, "health_interval_sec": HEALTH_INTERVAL_SEC,
                  "completion_interval_sec": COMPLETION_INTERVAL_SEC, "health_timeout_sec": HEALTH_TIMEOUT_SEC,
                  "completion_timeout_sec": COMPLETION_TIMEOUT_SEC, "completion_payload": cfg.probe_payload}
        t0 = d.clock()
        log(f"[window] {window_name} {duration:.0f}s 시작 (pod ip {ip})")
        handle = d.probe.start(params)
        complete = False
        try:
            while not handle.done():
                d.sleep(POLL_INTERVAL_SEC)
                check()
                if d.clock() - t0 > duration + COMPLETION_TIMEOUT_SEC + 30:
                    raise CalibrationAbort("H9", "probe 클라이언트가 제한시간 내 끝나지 않음")
            complete = True
        finally:
            if not complete:
                handle.abort()
            samples = handle.result()
            try:
                snap1 = d.cluster.snapshot()
            except Exception:
                snap1 = None
            pod1 = (snap1 or {}).get("pods", {}).get(name, {})
            after = unhealthy_count(snap1, name) if snap1 else before
            unhealthy["last"] = after
            result["windows"].append({
                "name": window_name, "stage": stage, "duration_sec": duration, "complete": complete,
                "completed_fraction": round(min(1.0, (d.clock() - t0) / duration), 3) if duration else 1.0,
                "health": summarize_health(samples), "completion": summarize_completion(samples),
                "kubelet": {"probe_failures": after - before,
                            "restarts": pod1.get("restarts"), "uid": pod1.get("uid"), "ready": pod1.get("ready"),
                            "phase": pod1.get("phase")},
                "nodes": (snap1 or {}).get("nodes")})
        err = handle.error()
        if err:
            raise CalibrationAbort("H9", f"probe 클라이언트 오류: {err}")

    try:
        d.cluster.create_pod(cfg.pod_manifest)
        state.created = True
        log(f"[pod] {name} 생성 - Ready 대기(<= {POD_READY_TIMEOUT_SEC:.0f}s)")
        ready_deadline = d.clock() + POD_READY_TIMEOUT_SEC
        while True:
            snap = check()
            if snap["pods"].get(name, {}).get("ready"):
                unhealthy["last"] = unhealthy_count(snap, name)  # Ready 시점 기준선
                break
            if d.clock() > ready_deadline:
                raise CalibrationAbort("H7", f"calibration pod가 {POD_READY_TIMEOUT_SEC:.0f}초 내 Ready 안 됨")
            d.sleep(POLL_INTERVAL_SEC)
        result["t_ready"] = d.now_iso()
        log(f"[pod] Ready - warmup settle {WARMUP_SETTLE_SEC:.0f}s")
        hold(WARMUP_SETTLE_SEC)
        run_window("baseline", BASELINE_SEC, None)
        for i, stage in enumerate(STAGES):
            cr = f"netdelay-calib-{cfg.run_id.rsplit('-', 1)[-1]}-s{i}"
            state.chaos_names.add(cr)  # 생성 호출이 중간에 실패해도 정리 대상에 들어가게 먼저 등록
            expiry = int(stage["duration_sec"] + INJECTED_TIMEOUT_SEC + COMPLETION_TIMEOUT_SEC + CR_EXPIRY_SLACK_SEC)
            d.chaos.create(cr, cfg.run_id, "calibration", name, stage, f"{expiry}s")
            wait_end = d.clock() + INJECTED_TIMEOUT_SEC
            while not d.chaos.injected(cr):
                if d.clock() > wait_end:
                    raise CalibrationAbort("H6", f"{cr}이(가) {INJECTED_TIMEOUT_SEC:.0f}초 내 AllInjected 안 됨")
                check()
                d.sleep(1.0)
            log(f"[chaos] {stage['name']} AllInjected")
            run_window(stage["name"], float(stage["duration_sec"]), stage)
            d.chaos.delete(cr)
            if not _wait(d, lambda c=cr: not d.chaos.exists(c), CHAOS_GONE_TIMEOUT_SEC):
                raise CalibrationAbort("H8", f"{cr}이(가) {CHAOS_GONE_TIMEOUT_SEC:.0f}초 내 소멸하지 않음")
            run_window(f"recovery-{stage['name']}", RECOVERY_SEC, None)
    except CalibrationAbort as e:
        result["hard_fail"] = {"code": e.code, "detail": e.detail}
    except BaseException as e:  # KeyboardInterrupt 포함 - 정리는 반드시 한다
        result["hard_fail"] = {"code": "H9", "detail": f"{type(e).__name__}: {e}"}
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            interrupted = e
    finally:
        result["cleanup"] = _cleanup(d, cfg, state, baseline)
    if not result["cleanup"]["ok"]:
        if result["hard_fail"] is None:
            result["hard_fail"] = {"code": "H8", "detail": "; ".join(result["cleanup"]["problems"])}
        else:
            result["hard_fail"]["also_cleanup_failed"] = result["cleanup"]["problems"]
    result["analysis"] = recommend(result["windows"], cfg.candidate_sec, result["hard_fail"])
    result["finished_at"] = d.now_iso()
    if interrupted is not None:
        interrupted.calibration_result = result
        raise interrupted
    return result


# ---- CLI ------------------------------------------------------------------------------------------
def load_probe_payload(path: Path = PROBE_CONFIG) -> dict:
    target = yaml.safe_load(path.read_text(encoding="utf-8"))["target"]
    return {"model": target["model"], "prompt": target["prompt"], "max_tokens": target["max_tokens"]}


def print_plan(out, rollout_changes: list, candidate: dict, cfg: Config, source: str) -> None:
    def p(s=""):
        print(s, file=out)

    p("=== calibration 계획 (dry-run - 클러스터 접근 없음) ===")
    p(f"사전 등록: docs/design/phase8-blue-green-preflight-incident.md §42 | run_id 예시: {cfg.run_id}")
    p("\n[overlay 렌더 diff] 렌더된 9개 리소스 중 Rollout만, 정확히 아래 2경로만 base와 다름 (나머지는 동일):")
    for path, old, new in rollout_changes:
        p(f"  {path}: {old} -> {new}")
    p(f"\n[후보 timeout] readiness={candidate['readiness']}s liveness={candidate['liveness']}s "
      f"(출처: {source}; 현재 운영 pod는 기본 1초)")
    spec = cfg.pod_manifest["spec"]
    c0 = spec["containers"][0]
    p(f"\n[calibration pod] {cfg.pod_manifest['metadata']['name']} labels={cfg.pod_manifest['metadata']['labels']}")
    p(f"  image={c0['image']} resources={c0['resources']} nodeSelector={spec['nodeSelector']}")
    p("  Rollout·Service selector·ServiceMonitor에 걸리지 않음(검증됨), base template과 nodeSelector·두 timeout 외 동일(검증됨)")
    p("\n[단계]")
    plan = [("warmup settle", WARMUP_SETTLE_SEC), ("baseline(지연 없음)", BASELINE_SEC)]
    for s in STAGES:
        plan += [(f"{s['name']} {s['latency']}±{s['jitter']} (AllInjected 확인 후 측정)", s["duration_sec"]),
                 ("  -> CR 삭제·소멸 확인 후 회복 측정", RECOVERY_SEC)]
    for label, sec in plan:
        p(f"  {sec:>5.0f}s  {label}")
    p(f"  (pod Ready 대기 <= {POD_READY_TIMEOUT_SEC:.0f}s, 하드 상한 {HARD_LIMIT_SEC / 60:.0f}분, 폴링 {POLL_INTERVAL_SEC:.0f}s)")
    p(f"\n[측정] /health {HEALTH_INTERVAL_SEC}s 간격 + completion {COMPLETION_INTERVAL_SEC}s 간격(payload {cfg.probe_payload}) - worker ssh에서")
    p("\n[판정] 즉시 실패 H1~H9 / 허용 probe 실패 0 / T_req=max(1.25*L_max, L_max+1.5), T_min=올림, 상한 "
      f"{T_CAP_SEC:.0f}s / 권고 KEEP·LOWER·RAISE·INSUFFICIENT·NONE, n=1이므로 잠정")
    p("\n[클러스터 변경(실행 시)] calibration pod 1개 생성·삭제, NetworkChaos CR 4개 생성·삭제(spec.duration 자동 만료 안전망). "
      "Rollout·Service·운영 pod·recovery-policy 변경 없음.")


def default_deps(args) -> Deps:
    return Deps(cluster=KubectlCluster(), chaos=ChaosOps(), probe=SshNodeProbe(host=args.node_ssh, ssh=args.ssh))


def main(argv=None, deps_factory: Callable = default_deps) -> int:
    parser = argparse.ArgumentParser(description="network_tolerant probe timeout calibration(격리 pod) - 사전 등록 §42")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="오프라인 계획·검증(클러스터 접근 없음)")
    mode.add_argument("--preflight-only", action="store_true", help="읽기 전용 클러스터 확인")
    mode.add_argument("--execute", action="store_true", help="실제 calibration 1회")
    parser.add_argument("--candidate-timeout-sec", type=float, default=None,
                        help="후속 측정용 - 기본은 overlay가 렌더한 값(10초)을 그대로 쓴다")
    parser.add_argument("--node-ssh", default=WORKER_SSH_HOST)
    parser.add_argument("--ssh", default="ssh")
    parser.add_argument("--output-dir", default=str(RESULTS_DIR))
    args = parser.parse_args(argv)

    try:
        rendered, base = render_overlay(), load_base()
        rollout_changes = verify_overlay(rendered, base)
    except OverlayError as e:
        print(f"OVERLAY 검증 실패: {e}")
        return 1
    candidate = overlay_candidate_timeouts(rendered[ROLLOUT_KEY])
    if candidate["readiness"] != candidate["liveness"]:
        print(f"OVERLAY 후보가 readiness/liveness에서 다름: {candidate}")
        return 1
    candidate_sec = args.candidate_timeout_sec if args.candidate_timeout_sec is not None else candidate["readiness"]
    source = "override(--candidate-timeout-sec)" if args.candidate_timeout_sec is not None else "overlay 렌더값"
    run_id = calibration_run_id()
    payload = load_probe_payload()

    if args.dry_run:
        pod = build_calibration_pod(rendered[ROLLOUT_KEY], run_id, "<active pod의 노드>", args.candidate_timeout_sec)
        verify_calibration_pod(pod, base)
        cfg = Config(run_id, pod, candidate_sec, source, payload)
        print_plan(sys.stdout, rollout_changes, {"readiness": candidate_sec, "liveness": candidate_sec}, cfg, source)
        print("\nDRY-RUN OK - 오프라인 검증 통과")
        return 0

    deps = deps_factory(args)
    snap = deps.cluster.snapshot()
    problems = preflight_problems(snap)
    live_problems = template_fidelity_problems(deps.cluster.rollout_template(), base[ROLLOUT_KEY]["spec"]["template"])
    if live_problems:
        problems.append(f"라이브 Rollout template이 base와 다름: {live_problems[:5]}")
    vllm = [(n, p) for n, p in snap["pods"].items() if p["labels"].get("app") == "vllm-serving"]
    node = vllm[0][1]["node"] if vllm else "unknown"
    pod = build_calibration_pod(rendered[ROLLOUT_KEY], run_id, node, args.candidate_timeout_sec)
    verify_calibration_pod(pod, base)
    cfg = Config(run_id, pod, candidate_sec, source, payload)

    if args.preflight_only:
        print(f"[preflight] Node={ {n: v['ready'] for n, v in snap['nodes'].items()} } Rollout={snap['rollout']}")
        print(f"[preflight] pods={ {n: (p['ready'], p['restarts']) for n, p in snap['pods'].items()} } "
              f"chaos={snap['chaos']} context={snap['context']}")
        if vllm:
            handle = deps.probe.start({"ip": vllm[0][1]["ip"], "port": 8000, "duration_sec": 3, "health_interval_sec": 1,
                                       "completion_interval_sec": 0, "health_timeout_sec": 10,
                                       "completion_timeout_sec": 10})
            while not handle.done():
                time.sleep(0.5)
            samples, err = handle.result(), handle.error()
            ok = [s for s in samples if s.get("status") == 200]
            print(f"[preflight] worker ssh 프로브 체인: /health {len(ok)}/{len(samples)} 성공 err={err}")
            if err or not ok:
                problems.append(f"worker ssh 프로브 체인 실패: {err} samples={samples[:2]}")
        for problem in problems:
            print(f"[preflight] 문제: {problem}")
        print("PREFLIGHT " + ("OK" if not problems else "FAIL"))
        return 0 if not problems else 1

    if problems:
        print("PREFLIGHT 실패 - 아무 것도 만들지 않고 중단:\n  " + "\n  ".join(problems))
        return 1
    print(f"calibration 시작: run_id={run_id} pod={pod['metadata']['name']} 후보 timeout={candidate_sec}s ({source})")
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"calibration-network-tolerant-{run_id}.json"

    def save(res):
        path.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        a = res["analysis"]
        print(f"\n결과 파일: {path}")
        print(f"run_outcome={a['run_outcome']} recommendation={a['recommendation']} L_max={a['L_max']} "
              f"T_required={a['T_required']} T_min={a['T_min']} (후보 {a['candidate_sec']}s, 상한 {a['T_cap']}s, 잠정)")
        print(f"이유: {a['reason']}")
        if res["hard_fail"]:
            print(f"하드 실패: {res['hard_fail']}")
        if res["cleanup"]:
            print(f"정리: ok={res['cleanup']['ok']} problems={res['cleanup']['problems']}")

    try:
        res = run_calibration(deps, cfg)
    except KeyboardInterrupt as e:
        save(e.calibration_result)
        return 130
    save(res)
    if res["cleanup"] and not res["cleanup"]["ok"]:
        return 3
    return 2 if res["analysis"]["run_outcome"] == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
