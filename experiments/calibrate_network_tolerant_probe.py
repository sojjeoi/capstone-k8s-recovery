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

v2(2026-09-19, 사전 등록 §44): 후보 timeout(`--candidate-timeout-sec 11`) 독립 2회 측정용 판정으로 바뀌었다. kubelet probe 실패
이벤트를 **실제 event timestamp**로 구간(steady injection / teardown transition / 그 밖)에 분류하고, 회차별 PASS 조건
(§44.2)을 `judge_v2`로 판정하며, steady·liveness·연속 teardown 실패는 측정 도중 즉시 중단한다(§44.3). 이벤트 유실은
Prometheus `prober_probe_total`과 교차검증한다. v1(§42.6 KEEP/LOWER/RAISE) 판정은 §43의 기록으로만 남는다.

사용법(정확히 하나):
  --dry-run         오프라인 - overlay 렌더 diff·pod 매니페스트 검증·계획 출력. 클러스터 접근 없음.
  --preflight-only  읽기 전용 클러스터 확인(Node·Rollout·pod·Chaos CR·context·라이브 template·worker ssh 체인·시계 오프셋·
                    Prometheus probe 카운터).
  --execute         실제 calibration 1회(이 pod·CR만 만들고 반드시 지운다).
종료 코드: 0 = PASS, 1 = 사전 확인·사용법 문제, 2 = FAIL/INVALID(판정 불가 - 부분 데이터는 저장), 3 = 정리 실패(H8),
130 = 중단(정리 후).
"""
import argparse
import copy
import json
import math
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import Counter
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
MIN_WORST_OK_SAMPLES = 30
# ---- v2(§44) 상수 ---------------------------------------------------------------------------------
TEARDOWN_TAIL_SEC = 15.0        # teardown transition window = CR 삭제 요청 ~ 삭제 완료(첫 소멸 확인) + 15초
AMBIGUITY_SEC = 1.0             # steady 경계 ±1초 안의 이벤트는 보수적으로 steady + ambiguous
CONSECUTIVE_SEC = 15.0          # readiness 실패 두 건이 이 안이면 "연속"
CROSSCHECK_WAIT_SEC = 45.0      # 마지막 창 뒤 Prometheus 스크랩(30초 간격) 한 번 + 여유
CROSSCHECK_ATTEMPTS = 3
CLOCK_SAMPLES = 5
PROM_NAMESPACE = "monitoring"
PROM_SERVICE = "kube-prom-kube-prometheus-prometheus:9090"
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


def stage4_t_min(windows: list, candidate_sec: float) -> dict:
    """§44.2 조건 8 - stage-4 steady 측정 창의 성공 /health 최대 지연 L_max로 T_req=max(1.25 x L_max, L_max+1.5), T_min=올림.
    데이터가 없거나(창 없음·미완료·성공 표본 30개 미만) 창에 실패한 probe 동등 요청이 있으면 L_max를 믿을 수 없어 `usable=False`
    (fail-closed: 조건 8을 충족한 것으로 보지 않는다)."""
    worst_name = STAGES[-1]["name"]
    worst = next((w for w in windows if w["name"] == worst_name), None)
    out = {"usable": False, "L_max": None, "T_required": None, "T_min": None, "T_cap": T_CAP_SEC, "note": ""}
    if worst is None or not worst["complete"]:
        out["note"] = "stage-4 창이 없거나 끝까지 측정되지 않음"
        return out
    if worst["health"]["ok"] < MIN_WORST_OK_SAMPLES:
        out["note"] = f"stage-4 창의 성공 /health가 {MIN_WORST_OK_SAMPLES}개 미만({worst['health']['ok']})"
        return out
    l_max = worst["health"]["max"]
    t_req = required_timeout(l_max)
    out.update(L_max=l_max, T_required=_r(t_req), T_min=math.ceil(t_req - 1e-9))
    stage_errors = sum(w["health"]["errors"] for w in windows if w["name"] in {s["name"] for s in STAGES})
    if stage_errors:
        out["note"] = f"stage 창에 실패한 probe 동등 /health 요청 {stage_errors}건 - L_max를 신뢰할 수 없음"
        return out
    out["usable"] = True
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
            ready = next((c for c in st.get("conditions", []) if c.get("type") == "Ready"), {})
            last_terminated = (cs.get("lastState") or {}).get("terminated")
            terminated = last_terminated or (cs.get("state") or {}).get("terminated") or {}
            snap["pods"][md["name"]] = {
                "uid": md["uid"], "labels": md.get("labels", {}), "phase": st.get("phase"),
                "ready": ready.get("status") == "True", "ready_since": ready.get("lastTransitionTime"),
                "restarts": cs.get("restartCount", 0), "terminated": bool(last_terminated),
                "terminated_reason": terminated.get("reason"), "evicted": st.get("reason") == "Evicted",
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
                                   "message": item.get("message", ""), "count": item.get("count", 1),
                                   "uid": md.get("uid"), "first": item.get("firstTimestamp"),
                                   "last": item.get("lastTimestamp"), "type": item.get("type")})
    return snap


@dataclass
class RunState:
    pod_name: str
    created: bool = False
    pod_uid: Optional[str] = None
    ready_seen: bool = False
    ready_since: Optional[str] = None   # Ready 조건 lastTransitionTime(첫 Ready 관측 시점 값) - 바뀌면 순간 전이
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
            if pod["restarts"] > 0 or pod["terminated"] or pod["evicted"] or pod["phase"] in ("Failed", "Unknown"):
                v.append(("H1", f"calibration pod 재시작/종료/OOM/eviction: restarts={pod['restarts']} "
                                f"terminated={pod['terminated']} reason={pod['terminated_reason']} "
                                f"evicted={pod['evicted']} phase={pod['phase']}"))
            if pod["ready"]:
                state.ready_seen = True
                if state.ready_since is None:
                    state.ready_since = pod["ready_since"]
                elif pod["ready_since"] != state.ready_since:
                    v.append(("H2", f"Ready 조건 lastTransitionTime 변경 {state.ready_since} -> {pod['ready_since']}"
                                    " (폴링 사이의 순간 Ready 전이 - Endpoint 제거 포함)"))
            elif state.ready_seen:
                v.append(("H2", "calibration pod가 Ready를 잃음(readiness probe 실패)"))
    return v


# ---- probe 이벤트 추적·구간 분류(§44.1)·즉시 중단(§44.3)·카운터 교차검증(§44.2)·회차 판정 -----------------------
PROBE_KINDS = (("Readiness probe failed", "Readiness"), ("Liveness probe failed", "Liveness"),
               ("Startup probe failed", "Startup"))


def probe_kind(message: str) -> Optional[str]:
    for prefix, kind in PROBE_KINDS:
        if message.startswith(prefix):
            return kind
    return None


def parse_k8s_time(text: str) -> float:
    """K8s 타임스탬프(초 해상도 `...Z`) -> epoch 초."""
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def iso(t: Optional[float]) -> Optional[str]:
    return None if t is None else datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds")


class EventTracker:
    """kubelet `Unhealthy`(Readiness/Liveness/Startup probe failed) 이벤트를 폴링 사이 `count` 증가분 하나하나의 발생으로 푼다.
    같은 메시지는 `count`로 합쳐지고 `lastTimestamp`만 갱신되므로 증가분마다 그 시점의 lastTimestamp를 부여한다(한 폴링에서 2건
    이상 늘면 모두 같은 시각 + approx 표시 - §44.1)."""

    def __init__(self, pod_name: str):
        self.pod_name, self.counts, self.occurrences = pod_name, {}, []

    def update(self, snap: dict, polled_at: float) -> list:
        new = []
        for e in snap["events"]:
            kind = probe_kind(e["message"]) if e["pod"] == self.pod_name and e["reason"] == "Unhealthy" else None
            if kind is None:
                continue
            key = e.get("uid") or e["message"]
            seen = self.counts.get(key, 0)
            delta = e["count"] - seen
            self.counts[key] = max(seen, e["count"])
            new += [{"kind": kind, "message": e["message"], "worker_ts": e.get("last") or e.get("first"),
                     "polled_at": polled_at, "approx": delta > 1} for _ in range(max(0, delta))]
        self.occurrences += new
        return new

    def totals(self) -> dict:
        counted = Counter(o["kind"] for o in self.occurrences)
        return {kind: counted.get(kind, 0) for _, kind in PROBE_KINDS}


def timeline_boundaries(tl: dict) -> list:
    """[(시작 epoch, 구간 이름)] 시간순 - 아직 모르는 경계는 빠진다. 구간은 §44.1 표 그대로."""
    bounds = []

    def add(t, name):
        if t is not None:
            bounds.append((t, name))
    add(tl.get("pod_created"), "startup")
    add(tl.get("ready"), "baseline")
    for i, st in enumerate(tl.get("stages", []), 1):
        add(st.get("create"), f"injection_ramp_{i}")
        add(st.get("allinjected"), f"steady_{i}")
        add(st.get("delete_request"), f"teardown_{i}")
        add(st.get("teardown_end"), "post_teardown" if i == len(STAGES) else f"between_stage_{i}")
    add(tl.get("pod_delete_request"), "shutdown")
    return sorted(bounds, key=lambda b: b[0])


TIMEOUT_MARKERS = ("Client.Timeout exceeded", "context deadline exceeded", "i/o timeout")
STRADDLING = "transition_straddling_"


def is_timeout_failure(message: str) -> bool:
    """probe가 timeoutSeconds를 다 채우고 실패한 유형(Go http client timeout) - 실행 구간이 [이벤트 - timeout, 이벤트]다."""
    return any(marker in message for marker in TIMEOUT_MARKERS)


def classify_occurrence(occ: dict, tl: dict, offset_sec: float, bounds: Optional[list] = None,
                        probe_timeout_sec: Optional[float] = None) -> dict:
    """발생 하나에 PC 시계 기준 시각 t_pc(epoch)·구간 segment·ambiguous를 붙여 새 dict로 돌려준다(§44.1).
    시각 = lastTimestamp(worker 시계, 초 단위 절삭) - 오프셋 + 0.5초(초 해상도의 중앙). steady 경계(AllInjected 확인·CR 삭제 요청)
    +-1초 안이면 보수적으로 steady + ambiguous. 명목 stage 시간은 어디에도 쓰지 않는다.

    계약서 §5.8(2026-09-20): 이벤트 시각만 보고 teardown으로 단정하지 않는다. probe_timeout_sec를 주면 readiness/liveness 실패의
    추정 실행 구간(timeout 유형 실패는 [이벤트 - timeout, 이벤트], 그 밖은 이벤트 시각 한 점)이 CR 삭제 요청 시각을 가로지르는
    (= 시작이 삭제 요청 +1초보다 이른) teardown 시각의 실패를 `transition_straddling_<i>`로 분류한다. steady 실패에도 순수 teardown
    실패에도 포함하지 않고 별도 집계한다."""
    bounds = timeline_boundaries(tl) if bounds is None else bounds
    t = parse_k8s_time(occ["worker_ts"]) - offset_sec + 0.5 if occ["worker_ts"] else occ["polled_at"]
    segment, ambiguous = "pre_start", False
    for start, name in bounds:
        if t >= start:
            segment = name
    for i, st in enumerate(tl.get("stages", []), 1):
        if any(abs(t - edge) <= AMBIGUITY_SEC for edge in (st.get("allinjected"), st.get("delete_request"))
               if edge is not None):
            segment, ambiguous = f"steady_{i}", True
    probe_start = None
    if probe_timeout_sec and occ["kind"] in ("Readiness", "Liveness"):
        probe_start = t - probe_timeout_sec if is_timeout_failure(occ["message"]) else t
        if segment.startswith("teardown_"):
            index = segment.rsplit("_", 1)[1]
            deleted = tl["stages"][int(index) - 1].get("delete_request")
            if deleted is not None and probe_start < deleted + AMBIGUITY_SEC:
                segment = STRADDLING + index
    return {**occ, "t_pc": t, "t_pc_iso": iso(t), "segment": segment, "ambiguous": ambiguous,
            "probe_start_est": probe_start, "probe_start_iso": iso(probe_start)}


def classify_all(occurrences: list, tl: dict, offset_sec: float, probe_timeout_sec: Optional[float] = None) -> list:
    bounds = timeline_boundaries(tl)
    return [classify_occurrence(o, tl, offset_sec, bounds, probe_timeout_sec) for o in occurrences]


def _in_transition(segment: str) -> bool:
    return segment.startswith(("teardown_", STRADDLING))


def probe_event_findings(classified: list) -> list:
    """§44.3 즉시 중단 조건 -> [(코드, 설명)]. `shutdown` 구간은 기록만 하고 제외한다.
    H10 = steady injection window의 readiness/liveness 실패, H11 = liveness 실패(전체 실행 - 단 `transition_straddling`은 별도
    집계라 제외, 계약서 §5.8), H12 = 전이 구간(teardown·transition_straddling)의 실패가 연속(같은 전이 구간에 같은 종류 2건 이상,
    또는 전이 구간 실패가 같은 종류의 다른 실패와 15초 이내)."""
    live = [o for o in classified if o["segment"] != "shutdown"]
    out = []
    for o in live:
        where = f"{o['segment']} @{o['t_pc_iso']}" + (" (경계 모호)" if o["ambiguous"] else "")
        if o["kind"] in ("Readiness", "Liveness") and o["segment"].startswith("steady_"):
            out.append(("H10", f"steady injection window {o['kind']} probe 실패 {where}"))
        if o["kind"] == "Liveness" and not o["segment"].startswith(STRADDLING):
            out.append(("H11", f"liveness probe 실패 {where}"))
    for kind in ("Readiness", "Liveness"):
        failures = sorted((o for o in live if o["kind"] == kind), key=lambda o: o["t_pc"])
        per_window = Counter(o["segment"].rsplit("_", 1)[1] for o in failures if _in_transition(o["segment"]))
        out += [("H12", f"전이 구간 {w}에 {kind} 실패 {n}건(연속)") for w, n in per_window.items() if n >= 2]
        for a, b in zip(failures, failures[1:]):
            gap = b["t_pc"] - a["t_pc"]
            if gap <= CONSECUTIVE_SEC and (_in_transition(a["segment"]) or _in_transition(b["segment"])):
                out.append(("H12", f"전이 구간 {kind} 실패 연속: {gap:.1f}초 간격 ({a['segment']} -> {b['segment']})"))
    return out


def crosscheck_probe_counters(e1: dict, e2: dict, metrics: dict) -> dict:
    """§44.2 측정 유효성 전제 - kubelet probe 카운터(Prometheus `prober_probe_total`)와 이벤트 집계의 교차검증.
    Readiness·Liveness 각각 E1 <= C(failed) <= E2 이고, `successful` series가 존재해야 한다(스크랩됐다는 양성 증거).
    kubelet 이벤트 스팸 필터가 초과분을 조용히 버려도(E < C) 여기서 드러난다."""
    problems, detail = [], {}
    for kind in ("Readiness", "Liveness"):
        failed = int(round(metrics["counters"].get(f"{kind}/failed", 0.0)))
        has_success = f"{kind}/successful" in metrics["counters"]
        detail[kind] = {"events_before": e1[kind], "counter_failed": failed, "events_after": e2[kind],
                        "successful_series": has_success}
        if not has_success:
            problems.append(f"{kind} successful series 없음(Prometheus가 이 pod를 스크랩하지 못함)")
        if not e1[kind] <= failed <= e2[kind]:
            problems.append(f"{kind}: 이벤트 {e1[kind]}..{e2[kind]} vs kubelet 카운터 {failed} 불일치(이벤트 유실 가능)")
    return {"ok": not problems, "problems": problems, "detail": detail}


def judge_v2(result: dict) -> dict:
    """§44.2 회차별 PASS 조건 1~9 + 측정 유효성 전제. 판정 조건(C*) 위반 = FAIL, 측정 유효성(V*) 위반 = INVALID(판정 불가).
    INVALID도 PASS가 아니므로 FAIL과 같이 취급한다(동결하지 않고 멈춘다)."""
    candidate = result["candidate_sec"]
    windows = result.get("windows", [])
    by_name = {w["name"]: w for w in windows}
    probe_events = result.get("probe_events", [])
    events = [o for o in probe_events if o["segment"] != "shutdown"]
    stages = result.get("timeline", {}).get("stages", [])
    hard, cleanup = result.get("hard_fail"), result.get("cleanup") or {}
    cond = {}

    def put(key, ok, detail):
        cond[key] = {"ok": bool(ok), "detail": detail}

    def brief(occurrences):
        return [f"{o['kind']} {o['segment']} @{o['t_pc_iso']}" + (" 경계모호" if o["ambiguous"] else "") for o in occurrences]

    injected = [i for i, st in enumerate(stages, 1) if st.get("allinjected")]
    put("C1_all_stages_allinjected", len(injected) == len(STAGES), f"AllInjected 확인 stage {injected}")
    incomplete = [n for n in ["baseline"] + [x for s in STAGES for x in (s["name"], f"recovery-{s['name']}")]
                  if n not in by_name or not by_name[n]["complete"]]
    put("V2_all_windows_complete", not incomplete, incomplete or "9개 창 모두 완료")
    bad = [f"{w['name']} {w['completion']['ok']}/{w['completion']['n']}" for w in windows
           if w["complete"] and (w["completion"]["n"] < 1 or w["completion"]["ok"] != w["completion"]["n"])]
    put("C2_completion_success_100pct", not bad, bad or "완료된 모든 창 100%(창마다 표본 1개 이상)")
    steady = [o for o in events if o["kind"] in ("Readiness", "Liveness") and o["segment"].startswith("steady_")]
    put("C3_no_steady_probe_failure", not steady, brief(steady) or "0건")
    liveness = [o for o in events if o["kind"] == "Liveness" and not o["segment"].startswith(STRADDLING)]
    put("C4_no_liveness_failure", not liveness, brief(liveness) or "0건(shutdown·transition_straddling 제외)")
    ready_class = hard is not None and hard["code"] in ("H1", "H2", "H3")
    put("C5_no_ready_restart_uid_oom_evict_node", not ready_class, hard if ready_class else "0건")
    consecutive = [m for c, m in probe_event_findings(probe_events) if c == "H12"]
    teardown = Counter(o["segment"] for o in events if o["kind"] == "Readiness" and o["segment"].startswith("teardown_"))
    straddling = [o for o in events if o["segment"].startswith(STRADDLING)]
    put("C6_teardown_readiness_not_consecutive", not consecutive,
        consecutive or f"순수 teardown readiness 실패 구간별 {dict(sorted(teardown.items())) or '없음'}, "
                       f"transition_straddling {len(straddling)}건(비연속 단발 허용, 별도 집계)")
    tm = stage4_t_min(windows, candidate)
    put("C7_t_min_le_candidate", tm["T_min"] is None or tm["T_min"] <= candidate,
        f"L_max={tm['L_max']} T_required={tm['T_required']} T_min={tm['T_min']} 후보={candidate}")
    put("V4_l_max_usable", tm["usable"], tm["note"] or "stage-4 창 데이터 사용 가능")
    put("C8_cleanup_ok", cleanup.get("ok") is True, cleanup.get("problems") or "정리 완전 성공")
    put("C9_no_hard_fail", hard is None, hard or "없음")
    cross = result.get("crosscheck")
    put("V3_probe_counter_crosscheck", bool(cross and cross["ok"]),
        "교차검증 미수행" if cross is None else cross["problems"] or cross.get("detail") or "교차검증 통과")
    offset = result.get("clock_offset")
    put("V1_clock_offset", bool(offset and offset.get("used_sec") is not None), offset or "시계 오프셋 없음")
    failed = sorted(k for k, v in cond.items() if not v["ok"])
    outcome = "FAIL" if any(k.startswith("C") for k in failed) else ("INVALID" if failed else "PASS")
    counts = Counter(f"{o['segment']}/{o['kind']}" for o in probe_events)
    ready_transition = hard is not None and hard["code"] == "H2"
    restarted = hard is not None and hard["code"] == "H1"
    straddling_summary = {  # 계약서 §5.8 - 최종 표에 횟수·Ready 전이·Endpoint 영향·restart를 함께 표시한다
        "count": len(straddling), "events": brief(straddling), "ready_transition": ready_transition,
        "endpoint_impact": "있음(Ready 전이)" if ready_transition else "없음(Ready 전이 0 = Endpoint 유지)",
        "restart": restarted, "not_a_profile_failure": not (ready_transition or restarted or consecutive)}
    return {"run_outcome": outcome, "transition_straddling": straddling_summary, "candidate_sec": candidate, "L_max": tm["L_max"], "T_required": tm["T_required"],
            "T_min": tm["T_min"], "T_cap": T_CAP_SEC, "conditions": dict(sorted(cond.items())),
            "failed_conditions": failed, "reasons": [f"{k}: {cond[k]['detail']}" for k in failed],
            "probe_event_counts": dict(sorted(counts.items()))}


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

    def _prom(self, promql: str) -> list:
        path = (f"/api/v1/namespaces/{PROM_NAMESPACE}/services/{PROM_SERVICE}/proxy/api/v1/query?query="
                + urllib.parse.quote(promql, safe=""))
        body = self._json(["get", "--raw", path])
        if body.get("status") != "success":
            raise RuntimeError(f"Prometheus 질의 실패: {str(body)[:200]}")
        return body["data"]["result"]

    def prober_metrics(self, pod_name: str) -> dict:
        """읽기 전용 - Prometheus가 스크랩한 kubelet probe 카운터(`prober_probe_total`)와 소요시간 히스토그램(증거용).
        pod가 **살아 있을 때** 조회해야 한다(삭제 뒤에는 시계열이 stale이라 비어 나온다)."""
        selector = f'{{namespace="{NAMESPACE}",pod="{pod_name}"}}'
        counters = {}
        for r in self._prom(f"prober_probe_total{selector}"):
            key = f"{r['metric'].get('probe_type')}/{r['metric'].get('result')}"
            counters[key] = counters.get(key, 0.0) + float(r["value"][1])
        buckets = [{"probe_type": r["metric"].get("probe_type"), "result": r["metric"].get("result"),
                    "le": r["metric"].get("le"), "value": float(r["value"][1])}
                   for r in self._prom(f"prober_probe_duration_seconds_bucket{selector}")]
        return {"counters": counters, "duration_buckets": buckets}

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


class SshClock:
    """worker 시계 - PC 시계 오프셋(worker - PC) 측정. `ssh worker date +%s.%N`을 여러 번 왕복해 RTT가 가장 짧은 표본을 쓴다
    (오프셋 = worker 시각 - 왕복 중간 PC 시각, 불확실성 <= RTT/2). kubelet 이벤트 시각은 worker 시계라 PC 시계로 옮기는 데 쓴다."""

    def __init__(self, host: str = WORKER_SSH_HOST, ssh: str = "ssh", run: Callable = subprocess.run,
                 now: Callable = time.time, samples: int = CLOCK_SAMPLES):
        self.host, self.ssh, self._run, self._now, self.samples = host, ssh, run, now, samples

    def measure(self) -> Optional[dict]:
        best = None
        for _ in range(self.samples):
            t0 = self._now()
            try:
                r = self._run([self.ssh, "-o", "BatchMode=yes", "-o", "LogLevel=ERROR", self.host, "date", "+%s.%N"],
                              capture_output=True, text=True, encoding="utf-8", timeout=30)
                t1 = self._now()
                worker = float(r.stdout.strip())
            except (OSError, ValueError, subprocess.SubprocessError):
                continue
            if r.returncode != 0:
                continue
            sample = {"offset_sec": worker - (t0 + t1) / 2.0, "rtt_sec": t1 - t0, "measured_at": iso((t0 + t1) / 2.0)}
            if best is None or sample["rtt_sec"] < best["rtt_sec"]:
                best = sample
        return best


@dataclass
class Deps:
    cluster: object
    chaos: ChaosOps
    probe: object
    clock: Callable = time.monotonic
    sleep: Callable = time.sleep
    now: Callable = lambda: datetime.now(timezone.utc)
    clock_offset: Callable = lambda: None


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


def compact_samples(samples: list) -> dict:
    """원본 표본을 증거로 남길 만큼만 줄인다: [seq, 창 시작 대비 발행 시각, 지연, HTTP status(, 오류)]."""
    out = {"health": [], "completion": []}
    for s in sorted(samples, key=lambda s: (s["kind"], s["seq"])):
        row = [s["seq"], round(s["t"], 3), round(s["latency"], 4), s.get("status")]
        if s.get("error"):
            row.append(str(s["error"])[:120])
        out[s["kind"]].append(row)
    return out


def timeline_iso(tl: dict) -> dict:
    return {key: ([{k: iso(v) for k, v in st.items()} for st in value] if key == "stages" else iso(value))
            for key, value in tl.items()}


def _cleanup(d: Deps, cfg: Config, state: RunState, baseline: dict, tracker: Optional[EventTracker] = None,
             tl: Optional[dict] = None) -> dict:
    out = {"chaos_deleted": {}, "pod_deleted": None, "problems": [], "pod_events": [], "ok": False}
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
        if tl is not None:
            tl["pod_delete_request"] = d.now().timestamp()   # 이 시각 이후는 shutdown 구간(기록만, 판정 제외)
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
        if tracker is not None:
            tracker.update(final, d.now().timestamp())   # 종료 아티팩트도 shutdown 구간으로 기록
        out["pod_events"] = [e for e in final["events"] if e["pod"] == state.pod_name]
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
    stamp = lambda: d.now().timestamp()  # noqa: E731 - PC 시계 epoch(구간 경계·이벤트 분류 기준)
    started = d.clock()
    deadline = started + HARD_LIMIT_SEC
    name = cfg.pod_manifest["metadata"]["name"]
    result = {"run_id": cfg.run_id, "tool": "calibrate_network_tolerant_probe", "preregistration": "§44",
              "started_at": iso(stamp()), "candidate_sec": cfg.candidate_sec,
              "candidate_source": cfg.candidate_source, "stages": [dict(s) for s in STAGES],
              "windows": [], "hard_fail": None, "cleanup": None, "timeline": {"stages": []},
              "clock_offset": None, "probe_events": [], "crosscheck": None, "prometheus": None, "analysis": None}
    baseline = d.cluster.snapshot()
    problems = preflight_problems(baseline)
    offset_start = None if problems else d.clock_offset()
    if not problems and offset_start is None:
        problems.append("worker-PC 시계 오프셋을 측정하지 못함(이벤트 시각을 PC 시계로 옮길 수 없음)")
    if problems:
        result["hard_fail"] = {"code": "PREFLIGHT", "detail": "; ".join(problems)}
        result["analysis"] = judge_v2(result)
        result["finished_at"] = iso(stamp())
        return result
    result["baseline"] = {"pods": {n: {k: p[k] for k in ("uid", "restarts", "ready")}
                                   for n, p in baseline["pods"].items()},
                          "rollout": baseline["rollout"], "services": baseline["services"]}
    state = RunState(pod_name=name)
    tracker = EventTracker(name)
    tl = {"stages": []}   # epoch 초 - 구간 경계(§44.1). 결과에는 ISO로 저장한다.
    interrupted = None

    def snap_now() -> dict:
        snap = d.cluster.snapshot()
        tracker.update(snap, stamp())
        return snap

    def check() -> dict:
        snap = snap_now()
        if d.clock() > deadline:
            raise CalibrationAbort("H9", f"하드 상한 {HARD_LIMIT_SEC:.0f}초 초과")
        violations = evaluate_violations(baseline, snap, state)
        violations += probe_event_findings(classify_all(tracker.occurrences, tl, offset_start["offset_sec"],
                                                        cfg.candidate_sec))
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
        params = {"ip": ip, "port": 8000, "duration_sec": duration, "health_interval_sec": HEALTH_INTERVAL_SEC,
                  "completion_interval_sec": COMPLETION_INTERVAL_SEC, "health_timeout_sec": HEALTH_TIMEOUT_SEC,
                  "completion_timeout_sec": COMPLETION_TIMEOUT_SEC, "completion_payload": cfg.probe_payload}
        t0, began = d.clock(), stamp()
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
                snap1 = snap_now()
            except Exception:
                snap1 = None
            pod1 = (snap1 or {}).get("pods", {}).get(name, {})
            result["windows"].append({
                "name": window_name, "stage": stage, "duration_sec": duration, "complete": complete,
                "started_at": iso(began), "ended_at": iso(stamp()),
                "completed_fraction": round(min(1.0, (d.clock() - t0) / duration), 3) if duration else 1.0,
                "health": summarize_health(samples), "completion": summarize_completion(samples),
                "samples": compact_samples(samples),
                "kubelet": {"restarts": pod1.get("restarts"), "uid": pod1.get("uid"), "ready": pod1.get("ready"),
                            "phase": pod1.get("phase")},
                "nodes": (snap1 or {}).get("nodes")})
        err = handle.error()
        if err:
            raise CalibrationAbort("H9", f"probe 클라이언트 오류: {err}")

    try:
        tl["pod_created"] = stamp()
        d.cluster.create_pod(cfg.pod_manifest)
        state.created = True
        log(f"[pod] {name} 생성 - Ready 대기(<= {POD_READY_TIMEOUT_SEC:.0f}s)")
        ready_deadline = d.clock() + POD_READY_TIMEOUT_SEC
        while True:
            snap = check()
            if snap["pods"].get(name, {}).get("ready"):
                break
            if d.clock() > ready_deadline:
                raise CalibrationAbort("H7", f"calibration pod가 {POD_READY_TIMEOUT_SEC:.0f}초 내 Ready 안 됨")
            d.sleep(POLL_INTERVAL_SEC)
        tl["ready"] = stamp()
        result["t_ready"] = iso(tl["ready"])
        log(f"[pod] Ready - warmup settle {WARMUP_SETTLE_SEC:.0f}s")
        hold(WARMUP_SETTLE_SEC)
        run_window("baseline", BASELINE_SEC, None)
        for i, stage in enumerate(STAGES):
            cr = f"netdelay-calib-{cfg.run_id.rsplit('-', 1)[-1]}-s{i}"
            st = {}
            tl["stages"].append(st)
            state.chaos_names.add(cr)  # 생성 호출이 중간에 실패해도 정리 대상에 들어가게 먼저 등록
            expiry = int(stage["duration_sec"] + INJECTED_TIMEOUT_SEC + COMPLETION_TIMEOUT_SEC + CR_EXPIRY_SLACK_SEC)
            st["create"] = stamp()
            d.chaos.create(cr, cfg.run_id, "calibration", name, stage, f"{expiry}s")
            wait_end = d.clock() + INJECTED_TIMEOUT_SEC
            while not d.chaos.injected(cr):
                if d.clock() > wait_end:
                    raise CalibrationAbort("H6", f"{cr}이(가) {INJECTED_TIMEOUT_SEC:.0f}초 내 AllInjected 안 됨")
                check()
                d.sleep(1.0)
            st["allinjected"] = stamp()   # steady injection window 시작 = AllInjected=True 확인 후
            log(f"[chaos] {stage['name']} AllInjected")
            run_window(stage["name"], float(stage["duration_sec"]), stage)
            st["delete_request"] = stamp()   # steady 끝 = CR 삭제 요청 전 / teardown 시작
            d.chaos.delete(cr)
            if not _wait(d, lambda c=cr: not d.chaos.exists(c), CHAOS_GONE_TIMEOUT_SEC):
                raise CalibrationAbort("H8", f"{cr}이(가) {CHAOS_GONE_TIMEOUT_SEC:.0f}초 내 소멸하지 않음")
            st["gone"] = stamp()   # 삭제 완료 = CR 소멸 첫 확인
            st["teardown_end"] = st["gone"] + TEARDOWN_TAIL_SEC
            run_window(f"recovery-{stage['name']}", RECOVERY_SEC, None)
        # 이벤트 유실 교차검증(§44.2) - pod가 살아 있는 동안 스크랩 1회 이상을 기다린 뒤 카운터를 읽는다
        check()
        before = tracker.totals()
        log(f"[crosscheck] kubelet probe 카운터 교차검증 - {CROSSCHECK_WAIT_SEC:.0f}s 대기(Prometheus 스크랩)")
        hold(CROSSCHECK_WAIT_SEC)
        metrics, failure = None, None
        for _attempt in range(CROSSCHECK_ATTEMPTS):
            try:
                metrics = d.cluster.prober_metrics(name)
                break
            except Exception as e:
                failure = f"{type(e).__name__}: {e}"
                d.sleep(10.0)
        check()
        after = tracker.totals()
        if metrics is None:
            result["crosscheck"] = {"ok": False, "problems": [f"Prometheus 조회 실패: {failure}"], "detail": {}}
        else:
            result["crosscheck"] = crosscheck_probe_counters(before, after, metrics)
            result["prometheus"] = metrics
    except CalibrationAbort as e:
        result["hard_fail"] = {"code": e.code, "detail": e.detail}
    except BaseException as e:  # KeyboardInterrupt 포함 - 정리는 반드시 한다
        result["hard_fail"] = {"code": "H9", "detail": f"{type(e).__name__}: {e}"}
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            interrupted = e
    finally:
        result["cleanup"] = _cleanup(d, cfg, state, baseline, tracker, tl)
    if not result["cleanup"]["ok"]:
        if result["hard_fail"] is None:
            result["hard_fail"] = {"code": "H8", "detail": "; ".join(result["cleanup"]["problems"])}
        else:
            result["hard_fail"]["also_cleanup_failed"] = result["cleanup"]["problems"]
    # 종료 오프셋으로 시계 보정을 확정하고(시작·종료 평균) 모든 이벤트를 다시 분류한다
    offset_end = d.clock_offset()
    used = [o["offset_sec"] for o in (offset_start, offset_end) if o]
    result["clock_offset"] = {"start": offset_start, "end": offset_end,
                              "used_sec": sum(used) / len(used) if used else None,
                              "drift_sec": abs(offset_end["offset_sec"] - offset_start["offset_sec"])
                              if offset_start and offset_end else None}
    result["timeline"] = timeline_iso(tl)
    result["probe_events"] = classify_all(tracker.occurrences, tl, result["clock_offset"]["used_sec"], cfg.candidate_sec)
    result["pod_events"] = result["cleanup"].pop("pod_events")
    result["ready_condition_since"] = state.ready_since
    result["analysis"] = judge_v2(result)
    result["finished_at"] = iso(stamp())
    if interrupted is not None:
        interrupted.calibration_result = result
        raise interrupted
    return result


# ---- CLI ------------------------------------------------------------------------------------------
def timeline_from_iso(tl_iso: dict) -> dict:
    def epoch(value):
        return datetime.fromisoformat(value).timestamp() if value else None
    return {key: ([{k: epoch(v) for k, v in st.items()} for st in value] if key == "stages" else epoch(value))
            for key, value in tl_iso.items()}


def reanalyze(result: dict, probe_timeout_sec: Optional[float] = None) -> dict:
    """저장된 결과 JSON을 계약서 §5.8 정의로 다시 분류·판정한다(원본 결과는 수정하지 않는다 - 측정 데이터는 그대로,
    분류 정의만 바뀐 것이므로). probe_timeout_sec 기본은 그 회차의 후보 timeout이다."""
    timeout = probe_timeout_sec or result["candidate_sec"]
    keys = ("kind", "message", "worker_ts", "polled_at", "approx")
    events = classify_all([{k: o[k] for k in keys} for o in result["probe_events"]],
                          timeline_from_iso(result["timeline"]), result["clock_offset"]["used_sec"], timeout)
    return {"run_id": result["run_id"], "candidate_sec": result["candidate_sec"], "probe_timeout_sec": timeout,
            "original_analysis_outcome": result["analysis"]["run_outcome"], "probe_events": events,
            "analysis": judge_v2({**result, "probe_events": events})}


def print_reanalysis(out, re: dict, original_events: list) -> None:
    def p(text=""):
        print(text, file=out)
    a = re["analysis"]
    p(f"[재분류 계약서 5.8] run_id={re['run_id']} 후보={re['candidate_sec']:g}s probe timeout={re['probe_timeout_sec']:g}s "
      f"(원본 판정 {re['original_analysis_outcome']} -> 재판정 {a['run_outcome']})")
    for old, new in zip(original_events, re["probe_events"]):
        if new["kind"] == "Startup" or old["segment"] == new["segment"] == "shutdown":
            continue
        start = f" probe 시작 추정 {new['probe_start_iso']}" if new.get("probe_start_est") else ""
        p(f"  {new['kind']:9s} {old['segment']:18s} -> {new['segment']:24s} t_pc={new['t_pc_iso']}{start}")
    ts = a["transition_straddling"]
    p(f"  transition_straddling: {ts['count']}건 | Ready 전이 {'있음' if ts['ready_transition'] else '없음'} | "
      f"Endpoint 영향 {ts['endpoint_impact']} | restart {'있음' if ts['restart'] else '없음'} | "
      f"profile 실패 아님 = {ts['not_a_profile_failure']}")
    p(f"  이벤트(구간/종류): {a['probe_event_counts']}")
    p(f"  조건 위반: {a['failed_conditions'] or '없음'}")


def load_probe_payload(path: Path = PROBE_CONFIG) -> dict:
    target = yaml.safe_load(path.read_text(encoding="utf-8"))["target"]
    return {"model": target["model"], "prompt": target["prompt"], "max_tokens": target["max_tokens"]}


def print_plan(out, rollout_changes: list, candidate: dict, cfg: Config, source: str) -> None:
    def p(s=""):
        print(s, file=out)

    p("=== calibration 계획 (dry-run - 클러스터 접근 없음) ===")
    p(f"사전 등록: docs/design/phase8-blue-green-preflight-incident.md §42(절차·정리)·§44(v2 판정) | run_id 예시: {cfg.run_id}")
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
    p("\n[구간 §44.1] steady = AllInjected 확인 후 ~ CR 삭제 요청 전 / teardown = 삭제 요청 ~ 삭제 완료 + "
      f"{TEARDOWN_TAIL_SEC:.0f}s / 그 밖 startup·baseline·injection_ramp·between_stage·post_teardown / shutdown(판정 제외)")
    p("        이벤트는 실제 event timestamp(worker 시계 - 오프셋 + 0.5s)로 분류, steady 경계 +-1s는 보수적으로 steady")
    p(f"[판정 §44.2] 후보 {cfg.candidate_sec:g}s PASS = 4 stage AllInjected / completion 100% / steady probe 실패 0 / liveness 실패 0 / "
      "Ready 전이·restart·UID·OOM·eviction·Node pressure 0 / teardown readiness 실패는 구간당 비연속 단발 <=1 / "
      f"T_min<={cfg.candidate_sec:g}(T_req=max(1.25*L_max, L_max+1.5), 올림) / cleanup 완전 성공 + 측정 유효성(시계 오프셋·창 완료·"
      "kubelet 카운터 교차검증)")
    p("[즉시 중단 §44.3] H1~H9 + H10 steady 실패 / H11 liveness 실패 / H12 연속 teardown readiness 실패 -> 정리 후 FAIL 기록")
    p("\n[클러스터 변경(실행 시)] calibration pod 1개 생성·삭제, NetworkChaos CR 4개 생성·삭제(spec.duration 자동 만료 안전망). "
      "Rollout·Service·운영 pod·recovery-policy 변경 없음.")


def default_deps(args) -> Deps:
    return Deps(cluster=KubectlCluster(), chaos=ChaosOps(), probe=SshNodeProbe(host=args.node_ssh, ssh=args.ssh),
                clock_offset=SshClock(host=args.node_ssh, ssh=args.ssh).measure)


def main(argv=None, deps_factory: Callable = default_deps) -> int:
    parser = argparse.ArgumentParser(description="network_tolerant probe timeout calibration(격리 pod) - 사전 등록 §42·§44")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="오프라인 계획·검증(클러스터 접근 없음)")
    mode.add_argument("--preflight-only", action="store_true", help="읽기 전용 클러스터 확인")
    mode.add_argument("--execute", action="store_true", help="실제 calibration 1회")
    mode.add_argument("--reanalyze", metavar="RESULT_JSON",
                      help="저장된 결과 JSON을 계약서 5.8(transition_straddling) 정의로 다시 분류·판정(클러스터 접근 없음, 원본 불변)")
    parser.add_argument("--candidate-timeout-sec", type=float, default=None,
                        help="후보 timeout(초) - calibration pod에만 적용한다. 기본은 overlay가 렌더한 값")
    parser.add_argument("--node-ssh", default=WORKER_SSH_HOST)
    parser.add_argument("--ssh", default="ssh")
    parser.add_argument("--output-dir", default=str(RESULTS_DIR))
    args = parser.parse_args(argv)

    if args.reanalyze:  # 클러스터·kubectl 접근 없음 - 저장된 JSON만 읽고, 원본은 그대로 두고 재분류 결과를 옆에 저장한다
        source = Path(args.reanalyze)
        result = json.loads(source.read_text(encoding="utf-8"))
        again = reanalyze(result, args.candidate_timeout_sec)
        print_reanalysis(sys.stdout, again, result["probe_events"])
        out_dir = Path(args.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"reanalysis-transition-straddling-{result['run_id']}.json"
        target.write_text(json.dumps(again, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"재분류 결과 저장: {target}")
        return 0

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
        offset = deps.clock_offset()
        print(f"[preflight] worker-PC 시계 오프셋: {offset}")
        if offset is None:
            problems.append("worker-PC 시계 오프셋을 측정하지 못함")
        if vllm:
            try:
                metrics = deps.cluster.prober_metrics(vllm[0][0])
                print(f"[preflight] Prometheus prober_probe_total(운영 pod {vllm[0][0]}): {metrics['counters']}")
                if not any(k.endswith("/successful") for k in metrics["counters"]):
                    problems.append("Prometheus에 운영 pod의 probe 카운터가 없음(교차검증 불가)")
            except Exception as e:
                problems.append(f"Prometheus probe 카운터 조회 실패: {type(e).__name__}: {e}")
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
        print(f"run_outcome={a['run_outcome']} L_max={a['L_max']} T_required={a['T_required']} T_min={a['T_min']} "
              f"(후보 {a['candidate_sec']}s)")
        for key, cond in a["conditions"].items():
            print(f"  [{'OK' if cond['ok'] else '위반'}] {key}: {cond['detail']}")
        print(f"probe 이벤트(구간/종류): {a['probe_event_counts']}")
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
    if res["hard_fail"] and res["hard_fail"]["code"] == "PREFLIGHT":
        return 1
    return 0 if res["analysis"]["run_outcome"] == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
