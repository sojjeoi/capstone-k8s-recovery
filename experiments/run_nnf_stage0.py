#!/usr/bin/env python3
"""noisy-neighbor CPU Stage 0 실행기(새 파일, 2026-10-10, counted=false) - 계약: docs/design/phase8-nnf-stage0-contract-20261010.md

질문: 같은 노드의 다른 pod CPU 경합만으로 SLO를 지키는 weak 경합과 살아 있는 채 SLO를 위반하는 strong 저하를 만들 수 있고,
그 둘이 단일 지표 하나로 너무 쉽게 갈리지는 않는가. 탐지기(IF·R·C2) 없음, 신호·승격·context 없음, record_only.
세션 종류(ID 접두사): nnb = 기준선, nnd = 개발 사다리(W2→W12, SLO 경계 종료), nnw = 확증 weak, nns = 확증 strong.
run_iof_stage0.py 구조 그대로(사전검사·준비 조건·단계·정리·원본 표본 저장). 바뀐 점: I/O writer → CPU 부하 pod(workers 한 축),
배경 L1 없음(probe 1 RPS만), probe pod가 부하 노드 밖인지 확인, 정리 뒤 노드 CPU 정상화 확인."""
import argparse
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import yaml

import guarded_cleanup_gate as gcg
import load_ramp_adapter as lra
import nnf_stage0_analysis as na
import run_guarded_pilot as gp
import run_if_threshold_candidate_smoke as smoke
import run_timed_switch_recovery_trial as rt
import run_v2_workload_pilot as rv
import slo_judge
import v2_workload_pilot_analysis as wa
from run_guarded_pilot import BASE_SCALE_DOWN_DELAY_SEC, MODE_RO, Deps, check_policy
from run_once import HarnessCorrupted, TrialInvalid

PLAN_ID = "capstone-nnf-stage0-20261010"
CONTRACT_DOC = "docs/design/phase8-nnf-stage0-contract-20261010.md"
REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = Path(__file__).parent / "results" / "capstone_nnf_stage0"
CONFIG = REPO_ROOT / "chaos" / "nnf-stage0.yaml"
BURNER_SCRIPT = REPO_ROOT / "chaos" / "loadgen" / "cpu_burner.py"
TARGET_NODE = "sj-worker2"
TARGET_NODE_INSTANCE = "192.168.30.117:9100"
PROM_URL = "http://127.0.0.1:9090"
KINDS = {"nnb": "baseline", "nnd": "development", "nnw": "confirm_weak", "nns": "confirm_strong"}
REP_BASE = {"nnb": 7000, "nnd": 7100, "nnw": 7200, "nns": 7300}
RUNS = {f"{p}{n}": (p, REP_BASE[p] + n) for p in KINDS for n in ((90, 91) if p == "nnb" else (90, 91, 92))}
SAFETY_POLL_SEC = 5.0
READINESS_POLL_SEC = 15.0
PROBE_CATCHUP_TIMEOUT_SEC = 60.0
PROBE_LIFETIME_SEC = 3600
CPU_SETTLE_MARGIN_CORES = 0.5
# freeze: v1(개발까지) → 확증 강도·단일 지표 규칙 amendment v1.1(같은 코드, config만 갱신)
FREEZE_MANIFESTS = {"nnf-stage0-v1.1": "docs/design/phase8-nnf-stage0-freeze-manifest-v1.1-20261010.json",
                    "nnf-stage0-v1": "docs/design/phase8-nnf-stage0-freeze-manifest-v1-20261010.json"}
PROVENANCE_REQUIRED = {"contract": CONTRACT_DOC, "runner": "experiments/run_nnf_stage0.py",
                       "analyzer": "experiments/nnf_stage0_analysis.py", "config": "chaos/nnf-stage0.yaml",
                       "burner": "chaos/loadgen/cpu_burner.py"}


def current_freeze(root: Optional[Path] = None) -> tuple:
    root = root or REPO_ROOT
    for ver, path in FREEZE_MANIFESTS.items():
        if (root / path).exists():
            return ver, path
    return "nnf-stage0-v1", FREEZE_MANIFESTS["nnf-stage0-v1"]


def _sha(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def load_config() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def verify_provenance(manifest: Optional[str] = None, root: Optional[Path] = None) -> dict:
    """iof와 같은 규칙: status frozen·버전 일치·필수 파일 포함·모든 해시 일치. 실패면 SystemExit(클러스터 접근 전)."""
    root = root or REPO_ROOT
    version, default = current_freeze(root)
    manifest = manifest or default
    try:
        raw = (root / manifest).read_bytes()
        m = json.loads(raw)
        files = m["files"]
        if m.get("status") != "frozen" or m.get("version") != version or not isinstance(files, dict):
            raise ValueError(f"status={m.get('status')} version={m.get('version')}")
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SystemExit(f"provenance fail-closed: freeze manifest 읽기·형식 오류 {manifest}: {type(e).__name__}: {e}") from e
    missing = sorted(k for k, rel in PROVENANCE_REQUIRED.items() if rel not in files)
    actual = {rel: _sha(root / rel) if (root / rel).is_file() else None for rel in files}
    bad = sorted(rel for rel, want in files.items() if actual[rel] != want)
    if missing or bad or manifest in files:
        raise SystemExit(f"provenance fail-closed: 필수 항목 누락 {missing}, 해시 불일치 {bad}, 자기 참조 {manifest in files}")
    return {"freeze_version": version, "freeze_manifest_path": manifest, "freeze_manifest_sha256": hashlib.sha256(raw).hexdigest(),
            "verified_files": actual, "verification_status": "verified", "verified_at_utc": datetime.now(timezone.utc).isoformat()}


def run_id_for(run: str) -> str:
    return f"nnf_stage0-{run}-{PLAN_ID}"


def amendment_ready(cfg: dict) -> bool:
    """weak < strong 강도 + 사전 등록 지표 6개 모두 방향에 맞는 non-null 임계값(null이면 발화 불가 = C5 공허 통과 방지)."""
    c = cfg["stress"].get("confirmatory") or {}
    rules = cfg.get("single_metric_rules") or {}
    if not (c.get("weak") and c.get("strong")) or c["weak"]["workers"] >= c["strong"]["workers"]:
        return False
    if set(rules) != set(na.RULE_METRICS):
        return False
    return all(r.get("upper") is not None and (na.RULE_METRICS[k]["direction"] != "two_sided" or r.get("lower") is not None)
               for k, r in rules.items())


def check_schedule(run: str, results_dir: Path, cfg: dict) -> dict:
    """순서: nnb90 → nnd90.. → (amendment) → nnw90.. / nns90.. (같은 접두사 안에서는 번호 순서)."""
    prefix = RUNS[run][0]
    n = int(run[3:])
    need = [f"{prefix}{n - 1}"] if n > 90 else {"nnd": ["nnb90"], "nnw": ["nnd90"], "nns": ["nnd90"]}.get(prefix, [])
    missing = [r for r in need if not (results_dir / f"summary-{run_id_for(r)}.json").exists()]
    if missing:
        raise SystemExit(f"{run} 거부: 순서 밖 - 먼저 시도할 실행 요약 없음 {missing}")
    if prefix in ("nnw", "nns") and not amendment_ready(cfg):
        raise SystemExit(f"{run} 거부: weak·strong 강도와 단일 지표 규칙이 amendment로 고정되지 않음")
    return {"required_before": need}


def plan_for(kind: str, cfg: dict) -> list:
    """스트레스 단계 목록(실행 순서). 각 {name, role, workers, after_sec}."""
    t, s = cfg["timing"], cfg["stress"]
    if kind == "baseline":
        return []
    if kind == "development":
        return [{"name": x["name"], "role": None, "workers": x["workers"], "after_sec": t["dev_recovery_sec"]}
                for x in s["development_ladder"]]
    role = "weak" if kind == "confirm_weak" else "strong"
    c = s["confirmatory"][role]
    return [{"name": c["name"], "role": role, "workers": c["workers"], "after_sec": t["confirm_tail_sec"]}]


def burner_pod_name(run_id: str, rep: int) -> str:
    return f"nnf-burner-{rep}-{hashlib.sha256(run_id.encode()).hexdigest()[:6]}"


def make_cpu_burner(run_id: str, rep: int, results_dir: Path, cfg: dict):
    """active 노드에 고정한 CPU 부하 pod 하나(vLLM 밖). request·limit 고정, 강도는 workers만. 동결 kubectl 헬퍼 재사용."""
    pod = burner_pod_name(run_id, rep)
    ns, sc = lra.NAMESPACE, cfg["stress"]
    overrides = {"apiVersion": "v1", "spec": {
        "nodeName": TARGET_NODE, "restartPolicy": "Never",
        "containers": [{"name": pod, "image": lra.IMAGE, "imagePullPolicy": "Never", "command": ["sleep", "7200"],
                        "resources": {"requests": {"cpu": sc["cpu_request"]},
                                      "limits": {"cpu": sc["cpu_limit"], "memory": sc["memory_limit"]}}}]}}
    info = {"pod": pod, "node": TARGET_NODE, "in_pod_sha256": None, "stages": {}}

    def sh(cmd):
        return lra._run(["kubectl", "exec", "-n", ns, pod, "--", "sh", "-c", cmd])

    def prepare():
        lra._run(["kubectl", "run", pod, "-n", ns, f"--image={lra.IMAGE}", "--restart=Never",
                  f"--overrides={json.dumps(overrides)}"], check=True)
        if not lra._wait_pod_ready(pod):
            raise TrialInvalid(f"{pod} Ready 시간초과")
        r = lra._run(["kubectl", "get", "pod", pod, "-n", ns, "-o", "jsonpath={.spec.nodeName}"])
        if (r.stdout or "").strip() != TARGET_NODE:
            raise TrialInvalid(f"burner pod 노드 불일치: {r.stdout}")
        lra._run(["kubectl", "cp", os.path.relpath(BURNER_SCRIPT), f"{ns}/{pod}:/cpu_burner.py"], check=True)
        r = sh("python -c \"import hashlib;print(hashlib.sha256(open('/cpu_burner.py','rb').read()).hexdigest())\"")
        info["in_pod_sha256"] = (r.stdout or "").strip()
        if info["in_pod_sha256"] != _sha(BURNER_SCRIPT):
            raise TrialInvalid(f"burner pod 파일 해시 불일치: {info['in_pod_sha256']}")

    def start(name, workers, duration):
        sh(f"rm -f /tmp/STOP /e_{name}")
        inner = (f"python /cpu_burner.py --workers {workers} --duration-sec {duration} --log /log_{name}.csv "
                 f"> /out_{name}.log 2>&1; echo $? > /e_{name}")
        lra._run(["kubectl", "exec", "-n", ns, pod, "--", "sh", "-c", f"nohup sh -c '{inner}' < /dev/null > /w_{name}.log 2>&1 &"],
                 check=True)
        info["stages"][name] = {"workers": workers, "duration_sec": duration, "started_utc": datetime.now(timezone.utc).isoformat()}

    def done(name):
        r = sh(f"cat /e_{name} 2>/dev/null")
        return r.returncode == 0 and bool((r.stdout or "").strip())

    def stop():
        sh("touch /tmp/STOP")

    def fetch(name):
        rc = (sh(f"cat /e_{name}").stdout or "").strip()
        log = lra._run(["kubectl", "exec", "-n", ns, pod, "--", "cat", f"/log_{name}.csv"])
        out = sh(f"cat /out_{name}.log").stdout
        p = results_dir / f"burner-{run_id}-{name}.csv"
        with open(p, "x", encoding="utf-8", newline="") as fh:
            fh.write(log.stdout or "")
        res = burner_result(log.stdout or "", info["stages"][name]["workers"], rc)
        res.update(log_csv=str(p), stdout_tail=(out or "")[-400:])
        info["stages"][name].update(res)
        return res

    def cleanup():
        lra._delete_pod(pod)

    return SimpleNamespace(prepare=prepare, start=start, done=done, stop=stop, fetch=fetch, cleanup=cleanup, info=info)


def burner_result(log_text: str, workers: int, rc_text: str) -> dict:
    """ran_sec = 모든 worker가 살아 있던 초 수."""
    rows = list(csv.DictReader(io.StringIO(log_text)))
    full = sum(1 for r in rows if int(r["alive"]) == workers)
    return {"rc": int(rc_text) if rc_text.lstrip("-").isdigit() else None, "ran_sec": full, "log_rows": len(rows)}


def readiness_check(target_uid: str, target_name: str, snapshot_fn, prom_raw_fn, now: datetime, burner_pod: str) -> list:
    """준비 조건(계약 §4, 모델·규칙 판단과 독립). 빈 목록 = 준비됨."""
    probs = []
    try:
        snap = snapshot_fn()
        cp = [x for x in gcg.check_canonical(snap, delay=BASE_SCALE_DOWN_DELAY_SEC) if not x.startswith("residual")]
        if cp:
            probs.append(f"canonical:{cp}")
        if (gcg.active_pod(snap) or {}).get("uid") != target_uid:
            probs.append("target_uid_changed")
    except Exception as e:  # noqa: BLE001
        probs.append(f"snapshot_error:{type(e).__name__}")
    sel = na.selectors(target_name, TARGET_NODE_INSTANCE, burner_pod)
    for k, n in {"cpu": 8, "gen_tokens": 6, "psi_cpu": 3}.items():
        try:
            got = len(prom_raw_fn(sel[k], now - timedelta(seconds=120), now))
        except Exception as e:  # noqa: BLE001
            got = f"error:{type(e).__name__}"
        if not isinstance(got, int) or got < n:
            probs.append(f"scrape_incomplete:{k}={got}<{n}")
    return probs


def probe_placement(read_probe_pods) -> dict:
    """probe pod가 정확히 하나이고 부하 노드 밖인지(계약 §3). 아니면 TrialInvalid."""
    pods = [p for p in read_probe_pods() if not p.get("terminating")]
    if len(pods) != 1:
        raise TrialInvalid(f"probe pod 수 {len(pods)} != 1")
    if pods[0].get("node") in (None, TARGET_NODE):
        raise TrialInvalid(f"probe pod가 부하 노드에 있음/노드 미상: {pods[0].get('node')}")
    return {"name": pods[0]["name"], "node": pods[0]["node"]}


def trial(kind, plan, timing, *, burner, prober, safety, probe_rows, readiness_fn, probe_place_fn,
          sleep=time.sleep, monotonic=time.monotonic, now=lambda: datetime.now(timezone.utc)):
    out = {"status": None, "reason": None, "probe_started_utc": None, "probe_pod": None, "observation_start_utc": None,
           "observation_end_utc": None, "readiness": [], "pre_stress": None, "stress_stages": [], "slo_boundary_stage": None,
           "stages_not_run": [], "cleanup_status": "incomplete", "prober_stop_failed": None, "burner_cleanup_failed": None}
    state = {"active_restart_count": None}
    proxy = rv._NoSloAbort(prober)

    def watch(seconds, until=None):
        end, last = monotonic() + seconds, None
        while monotonic() < end:
            if last is None or monotonic() - last >= SAFETY_POLL_SEC:
                safety(state, proxy)
                last = monotonic()
                if until is not None and until():
                    return True
            sleep(1.0)
        return False

    def verdict(s, e, obs0):
        box = {}

        def judged():
            box["v"] = wa.stage_slo_verdict(probe_rows(), obs0, s, e)
            return box["v"] is not None
        if not watch(PROBE_CATCHUP_TIMEOUT_SEC, until=judged):
            raise TrialInvalid("단계 끝 뒤 probe 증거 미도착")
        return box["v"]
    try:
        if burner is not None:
            burner.prepare()   # 유휴(sleep) 상태로 먼저 올린다 - 부하는 단계에서만
        t_probe = monotonic()
        prober.start()
        out["probe_started_utc"] = now().isoformat()
        out["probe_pod"] = probe_place_fn()
        if not watch(120, until=prober.is_alive):
            raise TrialInvalid("probe 시작 확인 실패")
        if not watch(smoke.BASELINE_TIMEOUT_SEC, until=lambda: prober.get_baseline_status().get("ready")):
            raise TrialInvalid("SLO 기준선 준비 상한 초과")
        safety(state, proxy)
        watch(timing["readiness_min_sec"])
        while True:
            probs = readiness_fn(now())
            out["readiness"].append({"at_utc": now().isoformat(), "problems": probs})
            if not probs:
                break
            if monotonic() - t_probe >= timing["readiness_max_sec"]:
                raise TrialInvalid(f"준비 조건 상한({timing['readiness_max_sec']} s) 초과: {probs}")
            watch(READINESS_POLL_SEC)
        obs0 = now()
        out["observation_start_utc"] = obs0.isoformat()
        if kind == "baseline":
            watch(timing["baseline_observation_sec"])
        else:
            p0 = now()
            watch(timing["pre_stress_sec"])
            out["pre_stress"] = {"start_utc": p0.isoformat(), "end_utc": now().isoformat()}
            for i, st in enumerate(plan):
                s = now()
                burner.start(st["name"], st["workers"], timing["stress_sec"])
                watch(timing["stress_sec"])
                if not watch(30, until=lambda: burner.done(st["name"])):
                    burner.stop()
                    if not watch(30, until=lambda: burner.done(st["name"])):
                        raise TrialInvalid(f"burner {st['name']} 종료 확인 실패")
                e = now()
                b = burner.fetch(st["name"])
                rec = {"name": st["name"], "role": st["role"], "workers": st["workers"],
                       "start_utc": s.isoformat(), "end_utc": e.isoformat(), "burner": b}
                out["stress_stages"].append(rec)
                if b.get("rc") != 0:
                    raise TrialInvalid(f"burner {st['name']} 비정상 종료 rc={b.get('rc')}")
                v = verdict(s, e, obs0)
                rec["slo_verdict"] = {k: v.get(k) for k in ("computable", "violating", "reason")}
                if not v["computable"]:
                    raise TrialInvalid(f"단계 {st['name']} 공식 SLO 판정 불가: {v['reason']}")
                watch(st["after_sec"])
                if kind == "development" and v["violating"]:
                    out.update(slo_boundary_stage=st["name"], stages_not_run=[x["name"] for x in plan[i + 1:]])
                    break
        obs1 = now()
        out["observation_end_utc"] = obs1.isoformat()

        def covered():   # 관찰 끝까지의 probe 원자료를 로컬로 회수(정리에서 probe pod가 지워지기 전) - iof v1.0.1과 같음
            return any(r["sent_at"] + timedelta(seconds=r["latency"]) > obs1 for r in probe_rows())
        if not watch(PROBE_CATCHUP_TIMEOUT_SEC, until=covered):
            raise TrialInvalid("관찰 끝 뒤 probe 증거 미회수 - 공식 SLO 판정 불가")
        out.update(status="completed", reason="SLO 경계에서 사다리 종료" if out["slo_boundary_stage"] else "계획 완료")
    except (TrialInvalid, HarnessCorrupted, subprocess.CalledProcessError) as e:
        out.update(status="aborted", reason=f"{type(e).__name__}: {e}")
    finally:
        out["ended_at_utc"] = now().isoformat()
        steps = [("prober_stop_failed", prober.stop)]
        if burner is not None:
            steps += [("burner_cleanup_failed", burner.stop), ("burner_cleanup_failed", burner.cleanup)]
        for k, fn in steps:
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                out[k] = f"{type(e).__name__}: {e}"
        out["cleanup_status"] = "complete" if not any(out[k] for k in ("prober_stop_failed", "burner_cleanup_failed")) \
            else "incomplete"
    return out


def node_busy_series(prom, a: datetime, b: datetime) -> list:
    m = {"series": {"cpu_idle": prom(na.selectors("x", TARGET_NODE_INSTANCE, "x")["cpu_idle"], a - timedelta(seconds=150), b)}}
    return [v for _, v in na.metric_values(m, "F_node_cpu_busy_cores", a, b) if v is not None]


def wait_cpu_settled(prom, pre_window, timing, *, sleep=time.sleep, monotonic=time.monotonic,
                     now=lambda: datetime.now(timezone.utc)) -> dict:
    """정리 뒤 노드 CPU 정상화(계약 §9): 최근 120 s 노드 사용 core ≤ 사전 구간 최댓값 + 0.5, 상한 cpu_settle_max_sec."""
    a, b = (datetime.fromisoformat(x) for x in pre_window)
    pre = node_busy_series(prom, a, b)
    if not pre:
        return {"ok": False, "reason": "pre_busy_unavailable"}
    limit = max(pre) + CPU_SETTLE_MARGIN_CORES
    t0 = monotonic()
    while True:
        t = now()
        last = node_busy_series(prom, t - timedelta(seconds=1), t)
        if last and last[-1] <= limit:
            return {"ok": True, "limit": round(limit, 4), "last": round(last[-1], 4), "waited_sec": round(monotonic() - t0, 1)}
        if monotonic() - t0 >= timing["cpu_settle_max_sec"]:
            return {"ok": False, "limit": round(limit, 4), "last": last and round(last[-1], 4), "reason": "not_settled"}
        sleep(15)


def run_session(run: str, *, deps: Deps, results_dir: Optional[Path] = None, make_burner=make_cpu_burner, prom_raw_fn=None,
                node_of_fn=None, prometheus_node_fn=None, code_provenance: Optional[dict] = None, preflight_only=False) -> dict:
    if run not in RUNS:
        raise SystemExit(f"알 수 없는 실행 ID: {run}")
    prefix, rep = RUNS[run]
    kind = KINDS[prefix]
    run_id = run_id_for(run)
    results_dir = results_dir or RESULTS_DIR
    results_dir.mkdir(parents=True, exist_ok=True)
    summary_path = results_dir / f"summary-{run_id}.json"
    if summary_path.exists() or (results_dir / f"contract-{run_id}.json").exists():
        raise SystemExit(f"실행 ID 재사용 금지: {run}")
    cfg = load_config()
    sched = check_schedule(run, results_dir, cfg)
    deps.check_unresolved()
    plan = plan_for(kind, cfg)
    prom = prom_raw_fn or (lambda sel, a, b: wa.prom_raw(PROM_URL, sel, a, b))
    node_of = node_of_fn or _node_of
    prom_node = (prometheus_node_fn or (lambda: node_of("monitoring", "prometheus-kube-prom-kube-prometheus-prometheus-0")))()
    pol = deps.policy
    policy_check = check_policy(pol, MODE_RO, None)
    session_start = deps.session_check()
    ssh_ev = deps.make_ssh_evidence(results_dir / f"ssh-evidence-{run_id}")
    preflight = deps.preflight(pol, disk_check_fn=ssh_ev.disk_fn(deps.preflight_nodes))
    try:
        delay_before = deps.read_delay()
    except Exception as e:  # noqa: BLE001
        delay_before = f"read_error: {type(e).__name__}: {e}"
    try:
        old_policy = deps.old_policy_check()
    except Exception as e:  # noqa: BLE001
        old_policy = {"isolated": False, "reasons": [f"check_error: {e}"]}
    reasons = list(preflight.get("reasons") or []) + policy_check["problems"] + list(session_start.get("problems") or [])
    if old_policy.get("isolated") is not True:
        reasons.append(f"old_recovery_policy_not_isolated: {old_policy.get('reasons')}")
    if delay_before != BASE_SCALE_DOWN_DELAY_SEC:
        reasons.append(f"scaleDownDelaySeconds={delay_before}")
    try:
        snap = deps.canonical_snapshot()
        cp = gcg.check_canonical(snap, delay=BASE_SCALE_DOWN_DELAY_SEC)
    except Exception as e:  # noqa: BLE001
        snap, cp = None, [f"snapshot_error: {type(e).__name__}: {e}"]
    p0 = dict(gcg.active_pod(snap or {}) or {})
    reasons += [f"canonical_start: {x}" for x in cp] + ([] if p0 else ["active_pod_unknown"])
    if p0:
        p0["node"] = node_of(lra.NAMESPACE, p0["name"])
        if p0["node"] != TARGET_NODE:
            reasons.append(f"placement: active node {p0['node']} != {TARGET_NODE}")
    if prom_node in (None, TARGET_NODE):
        reasons.append(f"placement: prometheus node {prom_node}")
    preflight.update(policy=policy_check, old_policy=old_policy, delay_before=delay_before, canonical_start=cp, active_pod=p0,
                     prometheus_node=prom_node, schedule=sched, ok=not reasons, reasons=reasons)
    pf = results_dir / f"preflight-{run_id}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.json"
    with open(pf, "x", encoding="utf-8") as fh:
        fh.write(json.dumps(preflight, ensure_ascii=False, indent=2, default=str))
    if reasons:
        raise SystemExit(f"사전검사 실패 - 시작 안 함: {reasons}")
    if preflight_only:
        return {"preflight_only": True, "run_id": run_id}
    burner_pod = burner_pod_name(run_id, rep)
    contract = {"run_id": run_id, "run": run, "kind": kind, "plan_id": PLAN_ID, "contract_doc": CONTRACT_DOC, "counted": False,
                "purpose": "Stage 0 noisy-neighbor CPU feasibility only - 학습·보정·검증 데이터 아님", "config_sha256": _sha(CONFIG),
                "burner_sha256": _sha(BURNER_SCRIPT), "plan": plan, "timing": cfg["timing"], "stress_fixed": {
                    k: cfg["stress"][k] for k in ("cpu_request", "cpu_limit", "memory_limit", "axis")},
                "single_metric_rules": cfg.get("single_metric_rules"), "target_node": TARGET_NODE, "burner_pod": burner_pod,
                "detector": None, "signal_send": False, "mode": MODE_RO, "code_provenance": code_provenance}
    (results_dir / f"contract-{run_id}.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")
    marker = results_dir / f"running-{run_id}.json"
    since = datetime.now(timezone.utc)
    marker.write_text(json.dumps({"run_id": run_id, "started_at": since.isoformat()}), encoding="utf-8")
    cleanup_errors, err, uid_flags, settle = [], None, [], None

    def safety(state, prober_):
        deps.base_safety(None, "no_injection")(state, prober_)
        r = deps.old_policy_check()
        if r.get("isolated") is not True:
            raise HarnessCorrupted(f"기존 recovery-policy 격리 풀림: {r.get('reasons')}")
    try:
        burner = make_burner(run_id, rep, results_dir, cfg) if plan else None
        prober = deps.make_prober(run_id, rep, "no_injection", PROBE_LIFETIME_SEC)
        probe_path = deps.probe_csv_path(run_id, rep)

        def probe_rows():
            prober.get_baseline_status()   # 동결 prober의 로컬 캐시 갱신
            return slo_judge.load_raw(probe_path) if probe_path and Path(probe_path).exists() else []

        def readiness(t):
            probs = readiness_check(p0["uid"], p0["name"], deps.canonical_snapshot, prom, t, burner_pod)
            uid_flags.append("target_uid_changed" not in probs)
            return probs
        outcome = trial(kind, plan, cfg["timing"], burner=burner, prober=prober, safety=safety, probe_rows=probe_rows,
                        readiness_fn=readiness, probe_place_fn=lambda: probe_placement(deps.read_probe_pods),
                        sleep=deps.sleep_fn, monotonic=deps.monotonic_fn)
        outcome["burner_pod"] = getattr(burner, "info", None)
        if burner is not None and node_of(lra.NAMESPACE, burner.info["pod"]) is not None:
            cleanup_errors.append(f"burner_pod_remaining: {burner.info['pod']}")
        if burner is not None and outcome.get("pre_stress"):
            pw = outcome["pre_stress"]
            settle = wait_cpu_settled(prom, (pw["start_utc"], pw["end_utc"]), cfg["timing"], sleep=deps.sleep_fn,
                                      monotonic=deps.monotonic_fn)
            if not settle["ok"]:
                cleanup_errors.append(f"node_cpu_not_settled: {settle}")
    except BaseException as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
        outcome = {"status": "harness_error", "reason": err, "cleanup_status": "unknown"}
    if outcome.get("cleanup_status") != "complete":
        cleanup_errors.append("trial_cleanup_incomplete: " + str([outcome.get(k) for k in (
            "prober_stop_failed", "burner_cleanup_failed")]))
    canonical_end = gcg.wait_canonical(deps.canonical_snapshot, delay=BASE_SCALE_DOWN_DELAY_SEC,
                                       sleep=deps.sleep_fn, monotonic=deps.monotonic_fn)
    if not canonical_end["ok"]:
        cleanup_errors.append(f"canonical_end_not_reached: {canonical_end['problems']}")
    end_pod = None
    try:
        end_snap = deps.canonical_snapshot()
        end_pod = gcg.active_pod(end_snap or {})
        if (end_snap or {}).get("active") != (snap or {}).get("active") or (end_pod or {}).get("uid") != p0["uid"]:
            cleanup_errors.append(f"active_changed: {p0} -> {end_pod}")
    except Exception as e:  # noqa: BLE001
        cleanup_errors.append(f"end_snapshot_failed: {e}")
    policy_after = pol.state()
    for k in ("unresolved", "blocked", "context"):
        if (policy_after or {}).get(k):
            cleanup_errors.append(f"policy_{k}_after: {policy_after[k]}")
    if deps.read_delay() != BASE_SCALE_DOWN_DELAY_SEC:
        cleanup_errors.append("scale_down_delay_changed")
    if err:
        cleanup_errors.append(f"harness_error: {err}")
    metrics_error = None
    try:
        a = datetime.fromisoformat(outcome.get("probe_started_utc") or since.isoformat()) - timedelta(seconds=180)
        b = datetime.fromisoformat(outcome.get("ended_at_utc") or datetime.now(timezone.utc).isoformat()) + timedelta(seconds=60)
        sel = na.selectors(p0["name"], TARGET_NODE_INSTANCE, burner_pod)
        m = {"pod": p0["name"], "node_instance": TARGET_NODE_INSTANCE, "burner_pod": burner_pod, "start_utc": a.isoformat(),
             "end_utc": b.isoformat(), "sample_kind": "raw_scrape", "selectors": sel,
             "series": {k: prom(s, a, b) for k, s in sel.items()}}
        with open(results_dir / f"metrics-{run_id}.json", "x", encoding="utf-8") as fh:
            fh.write(json.dumps(m, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        metrics_error = f"{type(e).__name__}: {e}"
    try:
        events = deps.capture_events(since.isoformat())
    except Exception as e:  # noqa: BLE001
        events = {"error": str(e)}
    probe_csv = deps.probe_csv_path(run_id, rep)
    summary = {"run_id": run_id, "run": run, "kind": kind, "counted": False, "outcome_status": outcome.get("status"),
               "outcome_reason": outcome.get("reason"), "observation_start_utc": outcome.get("observation_start_utc"),
               "observation_end_utc": outcome.get("observation_end_utc"), "pre_stress": outcome.get("pre_stress"),
               "stress_stages": outcome.get("stress_stages") or [], "slo_boundary_stage": outcome.get("slo_boundary_stage"),
               "stages_not_run": outcome.get("stages_not_run"), "readiness": outcome.get("readiness"),
               "probe_raw_csv": str(probe_csv) if probe_csv else None, "probe_pod": outcome.get("probe_pod"),
               "burner_pod": burner_pod if plan else None, "target_pod": p0, "end_pod": end_pod,
               "target_uid_constant": bool(uid_flags) and all(uid_flags) and (end_pod or {}).get("uid") == p0["uid"],
               "prometheus_node": prom_node, "cpu_settle": settle, "canonical_end": canonical_end,
               "policy_state_after": policy_after, "cleanup_errors": cleanup_errors, "metrics_error": metrics_error,
               "pod_events": events, "preflight_file": pf.name, "trial_outcome": outcome, "code_provenance": code_provenance}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if not cleanup_errors:
        marker.unlink()
    return summary


def _node_of(namespace: str, pod: str) -> Optional[str]:  # pragma: no cover - 라이브 읽기
    r = subprocess.run(["kubectl", "get", "pod", pod, "-n", namespace, "-o", "jsonpath={.spec.nodeName}"],
                       capture_output=True, text=True, timeout=20)
    return r.stdout.strip() or None


def main(argv=None) -> int:  # pragma: no cover - 세션 실행기 guarded_pilot_session.ps1이 부른다
    p = argparse.ArgumentParser(description="Stage 0 noisy-neighbor CPU feasibility(counted=false)")
    p.add_argument("--run", required=True, choices=sorted(RUNS))
    p.add_argument("--mode", required=True, choices=[MODE_RO])
    p.add_argument("--policy-url", default="http://127.0.0.1:18090")
    p.add_argument("--session-file", default=None)
    p.add_argument("--preflight-only", action="store_true")
    a = p.parse_args(argv)
    prov = verify_provenance()
    token = os.environ.get("GUARDED_ADMIN_TOKEN")
    if not token:
        raise SystemExit("GUARDED_ADMIN_TOKEN 환경변수 필요")
    d = gp.real_deps(a.policy_url, token, a.session_file)
    base = d.check_unresolved

    def check_unresolved():
        base()
        rt.check_no_unresolved_failures(RESULTS_DIR)
    d.check_unresolved = check_unresolved
    s = run_session(a.run, deps=d, code_provenance=prov, preflight_only=a.preflight_only)
    print(f"결과: {s.get('outcome_status') or s} cleanup_errors={s.get('cleanup_errors')}")
    return 2 if s.get("cleanup_errors") else 0


if __name__ == "__main__":
    sys.exit(main())
