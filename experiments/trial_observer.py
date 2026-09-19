#!/usr/bin/env python3
"""network_degrade 파일럿 관찰기 - 읽기 전용(2026-09-20).

trial 러너와 **별도 프로세스**로 돌면서 target pod의 Ready·Endpoint·restart·kubelet probe 실패 이벤트와 NetworkChaos CR의
단계별 타임라인(생성·AllInjected·삭제 요청·소멸)을 기록하고(`watch`), trial이 끝난 뒤 계약서 §5.8 정의로 probe 실패를
steady / transition_straddling / 순수 teardown으로 분류해 중단 조건을 판정한다(`analyze`).

TrialResult(결과 스키마)와 무관하다 - 새 필드를 만들지 않고 별도 JSONL/JSON 파일만 쓴다. 클러스터에는 `kubectl get`만 하고
아무 것도 바꾸지 않는다(이 파일에 쓰기 호출이 없다 - 테스트가 고정). 분류·이벤트 추적은 calibrate_network_tolerant_probe.py의
검증된 함수(EventTracker·classify_all·probe_event_findings)를 그대로 쓴다.

기록 형식(JSONL, 한 줄 = 한 레코드): header / state(변경이 있을 때만 전체 상태) / heartbeat / chaos(CR watch 스트림 - 단계별
정확한 시각) / footer. 시각은 전부 PC 시계(epoch 초)다. kubelet 이벤트 시각(worker 시계)은 header/footer의 worker 오프셋으로 옮긴다.

사용:
  python trial_observer.py watch --out results/pilot/observer-<name>.jsonl [--interval 3] [--stop-file PATH]
  python trial_observer.py analyze results/pilot/observer-<name>.jsonl --probe-timeout 11 [--json-out PATH]
"""
import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

import calibrate_network_tolerant_probe as cal

NAMESPACE = "vllm-serving"
RESOURCES = "pods,services,endpoints,events,rollouts.argoproj.io,networkchaos.chaos-mesh.org,nodes"
CHAOS_WATCH_CMD = ["kubectl", "get", "networkchaos.chaos-mesh.org", "-n", NAMESPACE, "--watch", "--output-watch-events",
                   "-o", "json"]
WATCHED_EVENT_REASONS = ("Unhealthy", "Killing")
ACTIVE_SERVICE = "vllm-active"
LOCAL_CHECKS = {"recovery_policy": "http://localhost:8080/healthz", "prometheus": "http://localhost:9090/-/healthy"}
LOCAL_CHECK_EVERY_SEC = 10.0
HEARTBEAT_SEC = 30.0


# ---- 상태 수집(순수) ------------------------------------------------------------------------------
def kubectl_items(run: Callable = subprocess.run) -> list:
    r = run(["kubectl", "get", RESOURCES, "-n", NAMESPACE, "-o", "json"], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"kubectl get 실패: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout)["items"]


def chaos_summary(item: dict) -> dict:
    conds = {c.get("type"): c for c in (item.get("status") or {}).get("conditions") or []}
    injected = conds.get("AllInjected") or {}
    targets = (((item.get("spec") or {}).get("selector") or {}).get("pods") or {}).get(NAMESPACE) or []
    md = item.get("metadata", {})
    # Chaos Mesh의 status.conditions에는 lastTransitionTime이 없다(실측 2026-09-20) - 없으면 True로 "주입됨"만 표시하고, 시각은
    # chaos watch 스트림의 수신 시각이 대신한다.
    injected_since = (injected.get("lastTransitionTime") or True) if injected.get("status") == "True" else None
    return {"target": targets[0] if targets else None, "created": md.get("creationTimestamp"),
            "deleting": md.get("deletionTimestamp"), "injected_since": injected_since}


def build_state(items: list) -> dict:
    """kubectl get -o json의 items -> 비교·저장용 compact 상태."""
    st = {"pods": {}, "services": {}, "endpoints": {}, "rollout": None, "chaos": {}, "nodes": {}, "events": []}
    for item in items:
        kind, md = item.get("kind"), item.get("metadata", {})
        name = md.get("name")
        if kind == "Pod":
            status = item.get("status", {})
            cs = (status.get("containerStatuses") or [{}])[0]
            ready = next((c for c in status.get("conditions", []) if c.get("type") == "Ready"), {})
            labels = md.get("labels", {})
            st["pods"][name] = {"uid": md["uid"], "app": labels.get("app"), "hash": labels.get("rollouts-pod-template-hash"),
                                "phase": status.get("phase"), "ready": ready.get("status") == "True",
                                "ready_since": ready.get("lastTransitionTime"), "restarts": cs.get("restartCount", 0),
                                "deleting": "deletionTimestamp" in md, "ip": status.get("podIP")}
        elif kind == "Service":
            st["services"][name] = item.get("spec", {}).get("selector")
        elif kind == "Endpoints":
            ready, not_ready = [], []
            for subset in item.get("subsets") or []:
                ready += [a["targetRef"]["name"] for a in subset.get("addresses") or [] if a.get("targetRef")]
                not_ready += [a["targetRef"]["name"] for a in subset.get("notReadyAddresses") or [] if a.get("targetRef")]
            st["endpoints"][name] = {"ready": sorted(ready), "not_ready": sorted(not_ready)}
        elif kind == "Rollout" and name == "vllm-serving":
            status, bg = item.get("status", {}), item.get("status", {}).get("blueGreen", {})
            st["rollout"] = {"phase": status.get("phase"), "abort": bool(status.get("abort")),
                             "current_hash": status.get("currentPodHash"), "stable_rs": status.get("stableRS"),
                             "active_selector": bg.get("activeSelector"), "preview_selector": bg.get("previewSelector")}
        elif kind == "NetworkChaos":
            st["chaos"][name] = chaos_summary(item)
        elif kind == "Node":
            conds = {c["type"]: c["status"] for c in item.get("status", {}).get("conditions", [])}
            st["nodes"][name] = {"ready": conds.get("Ready") == "True",
                                 "bad": [t for t in cal.NODE_BAD_CONDITIONS if conds.get(t) == "True"]}
        elif (kind == "Event" and item.get("involvedObject", {}).get("kind") == "Pod"
              and item.get("reason") in WATCHED_EVENT_REASONS and item["involvedObject"]["name"].startswith("vllm-")):
            st["events"].append({"pod": item["involvedObject"]["name"], "reason": item["reason"],
                                 "message": item.get("message", ""), "count": item.get("count", 1), "uid": md.get("uid"),
                                 "first": item.get("firstTimestamp"), "last": item.get("lastTimestamp")})
    st["events"].sort(key=lambda e: (e["pod"], e["uid"] or "", e["first"] or ""))
    return st


def parse_watch_stream(lines: Iterable[str]):
    """`kubectl get -w -o json --output-watch-events`의 JSON 객체 스트림을 하나씩 돌려준다. 실측(2026-09-20)상 kubectl은 이벤트마다
    **한 줄짜리 compact JSON**을 내보내지만(여러 줄 pretty-print 가정으로 처음 짜서 CR 스트림이 비었다), 여러 줄이어도 받도록
    `}`로 끝나는 줄마다 지금까지 쌓은 버퍼가 완결된 객체인지 시도한다."""
    decoder, buffer = json.JSONDecoder(), ""
    for line in lines:
        buffer += line
        if not line.rstrip().endswith("}"):
            continue
        try:
            obj, _ = decoder.raw_decode(buffer.strip())
        except ValueError:
            continue  # 아직 완결되지 않은 여러 줄 객체
        buffer = ""
        yield obj


# ---- 기록(watch) ---------------------------------------------------------------------------------
class Recorder:
    def __init__(self, path):
        self._fh = open(path, "a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()

    def write(self, record: dict) -> None:
        with self._lock:
            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def default_local_check(url: str) -> Optional[int]:
    import requests
    try:
        return requests.get(url, timeout=2).status_code
    except requests.exceptions.RequestException:
        return None


class StatePoller:
    """변경이 있을 때만 전체 상태를 기록하고(30초마다 heartbeat) 눈에 띄는 변화는 ALERT로 출력한다."""

    def __init__(self, fetch: Callable, recorder, now: Callable = time.time, local_check: Optional[Callable] = None,
                 alert: Callable = print):
        self.fetch, self.recorder, self.now, self.local_check, self.alert = fetch, recorder, now, local_check, alert
        self.last, self.last_write, self._local, self._local_at = None, None, None, None

    def _local_status(self, t: float) -> Optional[dict]:
        if self.local_check is None:
            return None
        if self._local_at is None or t - self._local_at >= LOCAL_CHECK_EVERY_SEC:
            self._local = {name: self.local_check(url) for name, url in LOCAL_CHECKS.items()}
            self._local_at = t
        return self._local

    def poll(self) -> None:
        t = self.now()
        state = build_state(self.fetch())
        state["local"] = self._local_status(t)
        if state != self.last:
            self.recorder.write({"type": "state", "t": t, "state": state})
            self._alerts(t, self.last, state)
            self.last, self.last_write = state, t
        elif t - self.last_write >= HEARTBEAT_SEC:
            self.recorder.write({"type": "heartbeat", "t": t})
            self.last_write = t

    def _alerts(self, t: float, old: Optional[dict], new: dict) -> None:
        stamp = cal.iso(t)[11:23]
        before = old or {"pods": {}, "events": [], "nodes": {}, "endpoints": {}, "rollout": None, "chaos": {}, "local": None}
        for name, pod in new["pods"].items():
            prev = before["pods"].get(name)
            if not name.startswith("vllm-"):
                continue
            if prev and prev["ready"] and not pod["ready"] and not pod["deleting"]:
                self.alert(f"[{stamp}] ALERT {name} Ready=False")
            if prev and pod["restarts"] > prev["restarts"]:
                self.alert(f"[{stamp}] ALERT {name} restart {prev['restarts']}->{pod['restarts']}")
            if prev is None:
                self.alert(f"[{stamp}] pod 생성 {name} (app={pod['app']} hash={pod['hash']})")
        for name in before["pods"]:
            if name.startswith("vllm-") and name not in new["pods"]:
                self.alert(f"[{stamp}] pod 소멸 {name}")
        seen = {(e["uid"], e["count"]) for e in before["events"]}
        for e in new["events"]:
            if (e["uid"], e["count"]) not in seen and e["reason"] == "Unhealthy":
                self.alert(f"[{stamp}] kubelet {e['message'][:90]} (pod {e['pod']} count={e['count']} last={e['last']})")
        for name, node in new["nodes"].items():
            if not node["ready"] or node["bad"]:
                self.alert(f"[{stamp}] ALERT Node {name} 이상 {node}")
        if new["endpoints"].get(ACTIVE_SERVICE) != before["endpoints"].get(ACTIVE_SERVICE):
            self.alert(f"[{stamp}] endpoints/{ACTIVE_SERVICE}: {new['endpoints'].get(ACTIVE_SERVICE)}")
        if new["rollout"] != before["rollout"]:
            self.alert(f"[{stamp}] rollout: {new['rollout']}")
        for name, c in new["chaos"].items():
            if before["chaos"].get(name) != c:
                self.alert(f"[{stamp}] chaos {name}: injected_since={c['injected_since']} deleting={c['deleting']}")
        if new.get("local") and any(v != 200 for v in new["local"].values()):
            self.alert(f"[{stamp}] ALERT 로컬 port-forward 이상: {new['local']}")


class ChaosStream(threading.Thread):
    """kubectl watch 스트림으로 NetworkChaos CR의 변화(ADDED/MODIFIED/DELETED)를 받는 즉시(PC 시계) 기록한다 - 삭제 요청·소멸
    시각을 폴링 간격이 아니라 sub-second로 얻기 위함. 끊기면 다시 붙는다(재접속 자체도 기록)."""

    def __init__(self, recorder, now: Callable = time.time, popen: Callable = subprocess.Popen, cmd: list = CHAOS_WATCH_CMD):
        super().__init__(daemon=True)
        self.recorder, self.now, self.popen, self.cmd = recorder, now, popen, cmd
        self.stop_flag, self.proc = threading.Event(), None

    def run(self) -> None:
        while not self.stop_flag.is_set():
            self.proc = self.popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
            self.recorder.write({"type": "chaos_stream", "t": self.now(), "event": "connected"})
            for event in parse_watch_stream(iter(self.proc.stdout.readline, "")):
                obj = event.get("object", event)
                self.recorder.write({"type": "chaos", "t": self.now(), "event": event.get("type", "LIST"),
                                     "name": obj.get("metadata", {}).get("name"), **chaos_summary(obj)})
            if not self.stop_flag.wait(2.0):
                self.recorder.write({"type": "chaos_stream", "t": self.now(), "event": "reconnecting"})

    def stop(self) -> None:
        self.stop_flag.set()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()


def run_watch(out_path: Path, interval: float, stop_file: Optional[Path], worker_ssh: Optional[str], ssh: str = "ssh",
              fetch: Callable = kubectl_items, stream: bool = True, sleep: Callable = time.sleep) -> None:
    recorder = Recorder(out_path)
    clock = cal.SshClock(host=worker_ssh, ssh=ssh) if worker_ssh else None
    recorder.write({"type": "header", "started_at": cal.iso(time.time()), "interval": interval,
                    "worker_offset": clock.measure() if clock else None})
    poller = StatePoller(fetch, recorder, local_check=default_local_check)
    chaos = ChaosStream(recorder) if stream else None
    if chaos:
        chaos.start()
    print(f"observer 시작: {out_path} (간격 {interval}s, stop-file={stop_file})", flush=True)
    try:
        while not (stop_file and stop_file.exists()):
            try:
                poller.poll()
            except Exception as e:  # 한 번의 kubectl 실패로 관찰을 멈추지 않는다 - 실패 자체를 기록한다
                recorder.write({"type": "poll_error", "t": time.time(), "error": f"{type(e).__name__}: {e}"[:200]})
            sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        if chaos:
            chaos.stop()
        recorder.write({"type": "footer", "stopped_at": cal.iso(time.time()),
                        "worker_offset": clock.measure() if clock else None})
        recorder.close()
        print("observer 종료", flush=True)


# ---- 분석(analyze) - 계약서 5.8 --------------------------------------------------------------------
def load_records(path: Path) -> list:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def chaos_stages(records: list) -> list:
    """chaos 스트림 레코드 -> 단계별 타임라인(PC epoch). 스트림이 없으면 상태 폴링으로 대신한다(정밀도 낮음 - source 표시)."""
    by_name = {}
    for r in records:
        if r["type"] != "chaos":
            continue
        s = by_name.setdefault(r["name"], {"name": r["name"], "target": r["target"], "created_ts": r["created"], "create": None,
                                           "allinjected": None, "delete_request": None, "gone": None, "source": "watch"})
        if s["create"] is None:
            s["create"] = r["t"]
        if r["injected_since"] and s["allinjected"] is None:
            s["allinjected"] = r["t"]
        if r["deleting"] and s["delete_request"] is None:
            s["delete_request"] = r["t"]
        if r["event"] == "DELETED" and s["gone"] is None:
            s["gone"] = r["t"]
            s["delete_request"] = s["delete_request"] if s["delete_request"] is not None else r["t"] - 2.3
    if not by_name:  # 폴링 대체 경로
        seen = {}
        for r in records:
            if r["type"] != "state":
                continue
            for name, c in r["state"]["chaos"].items():
                s = seen.setdefault(name, {"name": name, "target": c["target"], "created_ts": c["created"], "create": r["t"],
                                           "allinjected": None, "delete_request": None, "gone": None, "source": "poll"})
                if c["injected_since"] and s["allinjected"] is None:
                    s["allinjected"] = r["t"]
                if c["deleting"] and s["delete_request"] is None:
                    s["delete_request"] = r["t"]
            for name, s in seen.items():
                if name not in r["state"]["chaos"] and s["gone"] is None:
                    s["gone"] = r["t"]
                    s["delete_request"] = s["delete_request"] if s["delete_request"] is not None else r["t"] - 2.3
        by_name = seen
    stages = sorted(by_name.values(), key=lambda s: s["created_ts"] or "")
    for s in stages:
        s["teardown_end"] = s["gone"] + cal.TEARDOWN_TAIL_SEC if s["gone"] is not None else None
    return stages


def analyze(records: list, probe_timeout_sec: float, offset_sec: Optional[float] = None) -> dict:
    states = [(r["t"], r["state"]) for r in records if r["type"] == "state"]
    header = next((r for r in records if r["type"] == "header"), {})
    footer = next((r for r in records if r["type"] == "footer"), {})
    offsets = [o["offset_sec"] for o in (header.get("worker_offset"), footer.get("worker_offset")) if o]
    offset = offset_sec if offset_sec is not None else (sum(offsets) / len(offsets) if offsets else 0.0)
    stages = chaos_stages(records)
    target = next((s["target"] for s in stages if s["target"]), None)
    out = {"target": target, "probe_timeout_sec": probe_timeout_sec, "worker_offset_sec": offset,
           "offset_source": "given" if offset_sec is not None else ("measured" if offsets else "assumed 0"),
           "stages": [{k: (cal.iso(v) if k in ("create", "allinjected", "delete_request", "gone", "teardown_end") else v)
                       for k, v in s.items()} for s in stages]}
    stop = []
    # kubelet probe 실패 -> 계약서 5.8 분류(target pod). 나머지 pod의 실패는 별도로만 센다.
    trackers = {}
    for t, st in states:
        for pod in {e["pod"] for e in st["events"]}:
            trackers.setdefault(pod, cal.EventTracker(pod)).update({"events": st["events"]}, t)
    tl = {"stages": [{k: s[k] for k in ("create", "allinjected", "delete_request", "gone", "teardown_end")} for s in stages]}
    # target pod 자신의 종료(promotion 뒤 Argo scale-down 등)가 시작된 뒤의 probe 실패는 종료 아티팩트(shutdown)다 - 기록만 하고 판정
    # 에서 제외한다(계약서 5.8 표·§44.1과 같은 규칙). 시작 시각 = target의 kubelet `Killing` 이벤트(worker 시계 -> PC) 또는 target이
    # deleting으로 처음 관찰된 시각 중 이른 쪽.
    kills = [cal.parse_k8s_time(e["first"] or e["last"]) - offset + 0.5 for _, st in states for e in st["events"]
             if e["pod"] == target and e["reason"] == "Killing" and (e["first"] or e["last"])]
    deleting = [t for t, st in states if target in st["pods"] and st["pods"][target]["deleting"]]
    if kills or deleting:
        tl["pod_delete_request"] = min(kills + deleting[:1])
    out["target_termination_start"] = cal.iso(tl.get("pod_delete_request"))
    classified = cal.classify_all(trackers[target].occurrences, tl, offset, probe_timeout_sec) if target in trackers else []
    kubelet = [o for o in classified if o["kind"] in ("Readiness", "Liveness")]
    out["probe_events"] = [{"kind": o["kind"], "segment": o["segment"], "ambiguous": o["ambiguous"], "t_pc_iso": o["t_pc_iso"],
                            "probe_start_iso": o["probe_start_iso"], "message": o["message"][:110]} for o in kubelet]
    counts = {"steady": 0, "transition_straddling": 0, "teardown": 0, "other": 0, "shutdown": 0}
    for o in kubelet:
        seg = o["segment"]
        key = ("steady" if seg.startswith("steady_") else "transition_straddling" if seg.startswith(cal.STRADDLING)
               else "teardown" if seg.startswith("teardown_") else "shutdown" if seg == "shutdown" else "other")
        counts[key] += 1
    out["counts"] = counts
    out["other_pod_probe_failures"] = {p: len(tr.occurrences) for p, tr in trackers.items() if p != target}
    findings = cal.probe_event_findings(classified)
    stop += [("S1_steady_failure", m) for c, m in findings if c == "H10"]
    stop += [("S5_consecutive_transition_failures", m) for c, m in findings if c == "H12"]
    out["notes"] = [m for c, m in findings if c == "H11"]  # 순수 liveness 실패(steady 아님) - 계약서 중단 목록에는 없어 기록만
    # target pod의 Ready·Endpoint·restart
    first_restarts, prev_pod, prev_state, promotion_t, lost_t = None, None, None, None, None
    ready_false, flaps, removals, initial_active = [], [], [], None
    for t, st in states:
        initial_active = initial_active if initial_active is not None else (st["rollout"] or {}).get("active_selector")
        if promotion_t is None and (st["rollout"] or {}).get("active_selector") not in (None, initial_active):
            promotion_t = t
        pod = st["pods"].get(target)
        if pod is None or pod["deleting"]:
            if lost_t is None and promotion_t is None and prev_pod is not None:
                lost_t = t  # 승격 없이 target이 사라지거나 삭제 중 - 교체(재시작 연쇄)
        elif prev_pod is not None:
            if prev_pod["ready"] and not pod["ready"]:
                ready_false.append(cal.iso(t))
            elif pod["ready"] and pod["ready_since"] != prev_pod["ready_since"]:
                flaps.append(cal.iso(t))
        if pod is not None and first_restarts is None:
            first_restarts = pod["restarts"]
        if pod is not None and not pod["deleting"] and prev_state is not None:
            was = target in (prev_state["endpoints"].get(ACTIVE_SERVICE) or {}).get("ready", [])
            now_in = target in (st["endpoints"].get(ACTIVE_SERVICE) or {}).get("ready", [])
            if was and not now_in and prev_state["services"].get(ACTIVE_SERVICE) == st["services"].get(ACTIVE_SERVICE):
                removals.append(cal.iso(t))
        if pod is not None:
            prev_pod = pod
        prev_state = st
    last_pod = next((st["pods"][target] for _, st in reversed(states) if target in st["pods"]), None)
    restarts = (last_pod["restarts"] - first_restarts) if last_pod and first_restarts is not None else 0
    out.update(ready_false=ready_false, ready_flaps=flaps, endpoint_removals=removals, restarts=restarts,
               target_lost_before_promotion=cal.iso(lost_t), promotion_observed=cal.iso(promotion_t))
    if ready_false or flaps:
        stop.append(("S2_ready_false", f"Ready=False {ready_false} / 순간 전이 {flaps}"))
    if removals:
        stop.append(("S3_endpoint_removed", f"{ACTIVE_SERVICE} Endpoint에서 target 제거 {removals}"))
    if restarts or lost_t is not None:
        stop.append(("S4_restart_or_replaced", f"restart +{restarts}, 승격 전 target 소멸/삭제 {cal.iso(lost_t)}"))
    bad_nodes = sorted({(n, tuple(v["bad"])) for _, st in states for n, v in st["nodes"].items() if not v["ready"] or v["bad"]})
    if bad_nodes:
        stop.append(("S6_node_anomaly", str(bad_nodes)))
    down = [cal.iso(t) for t, st in states if st.get("local") and any(v != 200 for v in st["local"].values())]
    if down:
        stop.append(("S7_local_port_forward_down", f"{down[:3]}"))
    out["stop_conditions"] = stop
    out["verdict"] = "FAIL" if stop else "PASS"
    out["row"] = {"transition_straddling": counts["transition_straddling"], "ready_transition": bool(ready_false or flaps),
                  "endpoint_impact": bool(removals), "restart": bool(restarts or lost_t is not None),
                  "steady": counts["steady"], "pure_teardown": counts["teardown"]}
    return out


def print_analysis(out, a: dict) -> None:
    def p(text=""):
        print(text, file=out)
    p(f"[observer 분석 - 계약서 5.8] target={a['target']} probe timeout={a['probe_timeout_sec']:g}s "
      f"worker 오프셋={a['worker_offset_sec']:+.3f}s({a['offset_source']}) 판정={a['verdict']}")
    for s in a["stages"]:
        p(f"  {s['name']}: create {s['create'][11:23] if s['create'] else '-'} allinjected {(s['allinjected'] or '-')[11:23]} "
          f"delete_request {(s['delete_request'] or '-')[11:23]} gone {(s['gone'] or '-')[11:23]} ({s['source']})")
    for e in a["probe_events"]:
        p(f"  probe 실패 {e['kind']:9s} {e['segment']:24s} t_pc={e['t_pc_iso'][11:23]} probe 시작 추정 "
          f"{(e['probe_start_iso'] or '-')[11:23]}{' (경계 모호)' if e['ambiguous'] else ''}")
    r = a["row"]
    p(f"  최종 행: transition_straddling {r['transition_straddling']}건 | Ready 전이 {'있음' if r['ready_transition'] else '없음'} | "
      f"Endpoint 영향 {'있음' if r['endpoint_impact'] else '없음'} | restart {'있음' if r['restart'] else '없음'} | "
      f"steady {r['steady']} | 순수 teardown {r['pure_teardown']}")
    p(f"  target 소멸(승격 전) {a['target_lost_before_promotion']} / 승격 관찰 {a['promotion_observed']} / target 종료 시작 "
      f"{a['target_termination_start']} (그 뒤 probe 실패 = shutdown 아티팩트, 판정 제외) / 종료 아티팩트 {a['counts']['shutdown']}건 / "
      f"다른 pod probe 실패 {a['other_pod_probe_failures']}")
    for code, text in a["stop_conditions"]:
        p(f"  중단 조건 {code}: {text}")
    for note in a["notes"]:
        p(f"  참고(중단 조건 아님): {note}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="network_degrade 파일럿 관찰기(읽기 전용)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("watch")
    w.add_argument("--out", required=True)
    w.add_argument("--interval", type=float, default=3.0)
    w.add_argument("--stop-file", default=None)
    w.add_argument("--worker-ssh", default="capstone-worker", help="kubelet 이벤트 시계 보정용(빈 문자열이면 생략)")
    a = sub.add_parser("analyze")
    a.add_argument("path")
    a.add_argument("--probe-timeout", type=float, required=True)
    a.add_argument("--offset-sec", type=float, default=None)
    a.add_argument("--json-out", default=None)
    args = parser.parse_args(argv)
    if args.cmd == "watch":
        run_watch(Path(args.out), args.interval, Path(args.stop_file) if args.stop_file else None, args.worker_ssh or None)
        return 0
    result = analyze(load_records(Path(args.path)), args.probe_timeout, args.offset_sec)
    print_analysis(sys.stdout, result)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
