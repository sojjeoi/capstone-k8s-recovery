#!/usr/bin/env python3
"""Stage 0 I/O feasibility 실행기(새 파일, 2026-10-09, counted=false) - 계약: docs/design/phase8-iof-stage0-contract-20261009.md

질문: CPU가 정상 범위에 머문 채 통제된 노드 디스크 I/O 경합만으로 재현 가능한 서비스 저하(공식 SLO)를 만들 수 있고,
SLO를 해치지 않는 약한 I/O 상태도 만들 수 있는가. 탐지기(IF·R·C2) 없음, 신호·승격·context 없음, record_only.
세션 종류(ID 접두사): iob = 기준선(스트레스 없음), iod = 개발 사다리(I1→I2→I3, SLO 경계 종료), iof = 확증(weak → strong).
동결 부품 재사용(수정 없음): run_guarded_pilot.real_deps(사전검사·정책·probe·무주입 안전 감시·canonical·기존 정책 격리),
run_v2_workload_pilot.make_stage_ramp(배경 L1 부하 pod, jittered_grid_v1), v2_workload_pilot_analysis.stage_slo_verdict(공식 SLO).
새 부품: I/O 쓰기 pod(chaos/loadgen/io_writer.py, active 노드 고정, vLLM 밖), 모델 판단과 독립인 준비 조건, 배치 확인."""
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
import iof_stage0_analysis as ia
import load_ramp_adapter as lra
import run_guarded_pilot as gp
import run_if_threshold_candidate_smoke as smoke
import run_timed_switch_recovery_trial as rt
import run_v2_workload_pilot as rv
import slo_judge
import v2_workload_pilot_analysis as wa
from run_guarded_pilot import BASE_SCALE_DOWN_DELAY_SEC, MODE_RO, Deps, check_policy
from run_once import HarnessCorrupted, TrialInvalid

PLAN_ID = "capstone-iof-stage0-20261009"
CONTRACT_DOC = "docs/design/phase8-iof-stage0-contract-20261009.md"
REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = Path(__file__).parent / "results" / "capstone_iof_stage0"
CONFIG = REPO_ROOT / "chaos" / "iof-stage0.yaml"
WRITER_SCRIPT = REPO_ROOT / "chaos" / "loadgen" / "io_writer.py"
TARGET_NODE = "sj-worker2"
TARGET_NODE_INSTANCE = "192.168.30.117:9100"
PROM_URL = "http://127.0.0.1:9090"
KINDS = {"iob": "baseline", "iod": "development", "iof": "confirmatory"}
REP_BASE = {"iob": 4000, "iod": 5000, "iof": 6000}
RUNS = {f"{p}{n}": (p, REP_BASE[p] + n) for p in KINDS for n in ((90, 91) if p == "iob" else (90, 91, 92))}
MIN_DISK_AVAIL_BYTES = 20e9
SAFETY_POLL_SEC = 5.0
READINESS_POLL_SEC = 15.0
PROBE_CATCHUP_TIMEOUT_SEC = 60.0
PROBE_LIFETIME_SEC = 3600
# freeze: v1(개발까지) → 확증 강도 amendment v1.1(같은 코드, config만 갱신). 있으면 v1.1을 쓴다(코드 수정 없이 amendment 적용).
# v1.0.1(2026-10-09): 기준선 세션에서 probe 원자료를 회수하지 않던 하네스 결함 수정(iob90 invalid) - 규칙·강도·판정 불변
FREEZE_MANIFESTS = {"iof-stage0-v1.1": "docs/design/phase8-iof-stage0-freeze-manifest-v1.1-20261009.json",
                    "iof-stage0-v1.0.1": "docs/design/phase8-iof-stage0-freeze-manifest-v1.0.1-20261009.json",
                    "iof-stage0-v1": "docs/design/phase8-iof-stage0-freeze-manifest-v1-20261009.json"}
FREEZE_VERSION, FREEZE_MANIFEST = "iof-stage0-v1", FREEZE_MANIFESTS["iof-stage0-v1"]


def current_freeze(root: Optional[Path] = None) -> tuple:
    root = root or REPO_ROOT
    for ver, path in FREEZE_MANIFESTS.items():
        if (root / path).exists():
            return ver, path
    return FREEZE_VERSION, FREEZE_MANIFEST
PROVENANCE_REQUIRED = {"contract": CONTRACT_DOC, "runner": "experiments/run_iof_stage0.py",
                       "analyzer": "experiments/iof_stage0_analysis.py", "config": "chaos/iof-stage0.yaml",
                       "writer": "chaos/loadgen/io_writer.py"}


def _sha(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def load_config() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def verify_provenance(manifest: Optional[str] = None, root: Optional[Path] = None) -> dict:
    """v2wl과 같은 규칙: status frozen·버전 일치·필수 파일 포함·모든 해시 일치. 실패면 SystemExit(클러스터 접근 전)."""
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
    return f"iof_stage0-{run}-{PLAN_ID}"


def check_schedule(run: str, results_dir: Path, cfg: dict) -> dict:
    """순서: iob90 → iod90.. → iof90.. (같은 접두사 안에서는 번호 순서). iof는 weak·strong amendment 고정 뒤에만."""
    prefix = RUNS[run][0]
    n = int(run[3:])
    need = [f"{prefix}{n - 1}"] if n > 90 else ({"iod": ["iob90"], "iof": ["iod90"]}.get(prefix, []))
    missing = [r for r in need if not (results_dir / f"summary-{run_id_for(r)}.json").exists()]
    if missing:
        raise SystemExit(f"{run} 거부: 순서 밖 - 먼저 시도할 실행 요약 없음 {missing}")
    if prefix == "iof" and not (cfg["io"].get("confirmatory") or {}).get("strong"):
        raise SystemExit(f"{run} 거부: confirmatory weak·strong이 amendment로 고정되지 않음")
    return {"required_before": need}


def plan_for(kind: str, cfg: dict) -> list:
    """스트레스 단계 목록(실행 순서). 각 {name, role, target_write_mbps, after_sec}."""
    t = cfg["timing"]
    if kind == "baseline":
        return []
    if kind == "development":
        return [{"name": x["name"], "role": None, "target_write_mbps": x["target_write_mbps"], "after_sec": t["dev_recovery_sec"]}
                for x in cfg["io"]["development_ladder"]]
    c = cfg["io"]["confirmatory"]
    return [{"name": c["weak"]["name"], "role": "weak", "target_write_mbps": c["weak"]["target_write_mbps"],
             "after_sec": t["confirm_gap_sec"]},
            {"name": c["strong"]["name"], "role": "strong", "target_write_mbps": c["strong"]["target_write_mbps"],
             "after_sec": t["confirm_tail_sec"]}]


def make_io_writer(run_id: str, rep: int, results_dir: Path, cfg: dict):
    """active 노드에 고정한 쓰기 pod 하나(vLLM 밖). emptyDir sizeLimit, CPU·메모리 limit. 동결 kubectl 헬퍼 재사용."""
    pod = f"iof-writer-{rep}-{hashlib.sha256(run_id.encode()).hexdigest()[:6]}"
    ns, io_cfg = lra.NAMESPACE, cfg["io"]
    overrides = {"apiVersion": "v1", "spec": {
        "nodeName": TARGET_NODE, "restartPolicy": "Never",
        "containers": [{"name": pod, "image": lra.IMAGE, "imagePullPolicy": "Never", "command": ["sleep", "3600"],
                        "resources": {"limits": {"cpu": io_cfg["writer_cpu_limit"], "memory": io_cfg["writer_memory_limit"]}},
                        "volumeMounts": [{"name": "iotest", "mountPath": "/iotest"}]}],
        "volumes": [{"name": "iotest", "emptyDir": {"sizeLimit": io_cfg["emptydir_size_limit"]}}]}}
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
            raise TrialInvalid(f"writer pod 노드 불일치: {r.stdout}")
        lra._run(["kubectl", "cp", os.path.relpath(WRITER_SCRIPT), f"{ns}/{pod}:/io_writer.py"], check=True)
        r = sh("python -c \"import hashlib;print(hashlib.sha256(open('/io_writer.py','rb').read()).hexdigest())\"")
        info["in_pod_sha256"] = (r.stdout or "").strip()
        if info["in_pod_sha256"] != _sha(WRITER_SCRIPT):
            raise TrialInvalid(f"writer pod 파일 해시 불일치: {info['in_pod_sha256']}")

    def start(name, rate, duration):
        sh(f"rm -f /iotest/STOP /e_{name}")
        inner = (f"python /io_writer.py --rate-mbps {rate} --duration-sec {duration} --log /log_{name}.csv "
                 f"> /out_{name}.log 2>&1; echo $? > /e_{name}")
        lra._run(["kubectl", "exec", "-n", ns, pod, "--", "sh", "-c", f"nohup sh -c '{inner}' < /dev/null > /w_{name}.log 2>&1 &"],
                 check=True)
        info["stages"][name] = {"rate_mbps": rate, "duration_sec": duration, "started_utc": datetime.now(timezone.utc).isoformat()}

    def done(name):
        r = sh(f"cat /e_{name} 2>/dev/null")
        return r.returncode == 0 and bool((r.stdout or "").strip())

    def stop():
        sh("touch /iotest/STOP")

    def fetch(name):
        rc = (sh(f"cat /e_{name}").stdout or "").strip()
        log = lra._run(["kubectl", "exec", "-n", ns, pod, "--", "cat", f"/log_{name}.csv"])
        out = sh(f"cat /out_{name}.log").stdout
        p = results_dir / f"writer-{run_id}-{name}.csv"
        with open(p, "x", encoding="utf-8", newline="") as fh:
            fh.write(log.stdout or "")
        rows = list(csv.DictReader(io.StringIO(log.stdout or "")))
        total = sum(int(r["bytes"]) for r in rows)
        ran = (int(rows[-1]["sec"]) + 1) if rows else 0
        res = {"rc": int(rc) if rc.lstrip("-").isdigit() else None, "ran_sec": ran, "bytes": total,
               "achieved_mbps": round(total / 1e6 / max(ran, 1), 3), "log_csv": str(p), "stdout_tail": (out or "")[-400:]}
        info["stages"][name].update(res)
        return res

    def cleanup():
        lra._delete_pod(pod)

    return SimpleNamespace(prepare=prepare, start=start, done=done, stop=stop, fetch=fetch, cleanup=cleanup, info=info)


def readiness_check(target_uid: str, target_name: str, snapshot_fn, prom_raw_fn, now: datetime) -> list:
    """준비 조건(계약 §4, 모델·규칙 판단과 독립). 빈 목록 = 준비됨."""
    probs = []
    try:
        snap = snapshot_fn()
        # 세션 중에는 probe·배경·writer pod가 있는 것이 정상 - 잔여물 항목만 빼고 canonical 조건을 본다
        cp = [x for x in gcg.check_canonical(snap, delay=BASE_SCALE_DOWN_DELAY_SEC) if not x.startswith("residual")]
        if cp:
            probs.append(f"canonical:{cp}")
        if (gcg.active_pod(snap) or {}).get("uid") != target_uid:
            probs.append("target_uid_changed")
    except Exception as e:  # noqa: BLE001
        probs.append(f"snapshot_error:{type(e).__name__}")
    sel = ia.selectors(target_name, TARGET_NODE_INSTANCE)
    for k, n in {"cpu": 8, "gen_tokens": 6, "psi_waiting": 3, "written_bytes": 3}.items():
        try:
            got = len(prom_raw_fn(sel[k], now - timedelta(seconds=120), now))
        except Exception as e:  # noqa: BLE001
            got = f"error:{type(e).__name__}"
        if not isinstance(got, int) or got < n:
            probs.append(f"scrape_incomplete:{k}={got}<{n}")
    return probs


def trial(kind, plan, timing, *, ramp, writer, prober, safety, probe_rows, readiness_fn, disk_ok_fn,
          sleep=time.sleep, monotonic=time.monotonic, now=lambda: datetime.now(timezone.utc)):
    out = {"status": None, "reason": None, "probe_started_utc": None, "background_started_utc": None,
           "observation_start_utc": None, "observation_end_utc": None, "readiness": [], "pre_stress": None,
           "stress_stages": [], "slo_boundary_stage": None, "stages_not_run": [], "cleanup_status": "incomplete",
           "prober_stop_failed": None, "background_cleanup_failed": None, "writer_cleanup_failed": None}
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
        t_probe = monotonic()
        prober.start()
        out["probe_started_utc"] = now().isoformat()
        if not watch(120, until=prober.is_alive):
            raise TrialInvalid("probe 시작 확인 실패")
        if not watch(smoke.BASELINE_TIMEOUT_SEC, until=lambda: prober.get_baseline_status().get("ready")):
            raise TrialInvalid("SLO 기준선 준비 상한 초과")
        ramp.prepare()
        if writer is not None:
            writer.prepare()
        safety(state, proxy)
        ramp.start(1)
        out["background_started_utc"] = now().isoformat()
        watch(timing["readiness_min_bg_sec"])
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
                if not disk_ok_fn():
                    raise HarnessCorrupted("디스크 여유 공간 안전 기준 미달 - 단계 시작 안 함")
                s = now()
                writer.start(st["name"], st["target_write_mbps"], timing["stress_sec"])
                watch(timing["stress_sec"])
                if not watch(30, until=lambda: writer.done(st["name"])):
                    writer.stop()
                    if not watch(30, until=lambda: writer.done(st["name"])):
                        raise TrialInvalid(f"writer {st['name']} 종료 확인 실패")
                e = now()
                w = writer.fetch(st["name"])
                rec = {"name": st["name"], "role": st["role"], "target_write_mbps": st["target_write_mbps"],
                       "start_utc": s.isoformat(), "end_utc": e.isoformat(), "writer": w}
                out["stress_stages"].append(rec)
                if w.get("rc") != 0:
                    raise TrialInvalid(f"writer {st['name']} 비정상 종료 rc={w.get('rc')}")
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

        def covered():   # v1.0.1 수정: 관찰 끝까지의 probe 원자료를 로컬로 회수(정리에서 probe pod가 지워지기 전)
            return any(r["sent_at"] + timedelta(seconds=r["latency"]) > obs1 for r in probe_rows())
        if not watch(PROBE_CATCHUP_TIMEOUT_SEC, until=covered):
            raise TrialInvalid("관찰 끝 뒤 probe 증거 미회수 - 공식 SLO 판정 불가")
        out.update(status="completed", reason="SLO 경계에서 사다리 종료" if out["slo_boundary_stage"] else "계획 완료")
    except (TrialInvalid, HarnessCorrupted, subprocess.CalledProcessError) as e:
        out.update(status="aborted", reason=f"{type(e).__name__}: {e}")
    finally:
        out["ended_at_utc"] = now().isoformat()
        steps = [("prober_stop_failed", prober.stop), ("background_cleanup_failed", ramp.cleanup)]
        if writer is not None:
            steps += [("writer_cleanup_failed", writer.stop), ("writer_cleanup_failed", writer.cleanup)]
        for k, fn in steps:
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                out[k] = f"{type(e).__name__}: {e}"
        out["cleanup_status"] = "complete" if not any(out[k] for k in ("prober_stop_failed", "background_cleanup_failed",
                                                                         "writer_cleanup_failed")) else "incomplete"
    return out


def run_session(run: str, *, deps: Deps, results_dir: Optional[Path] = None, make_ramp=rv.make_stage_ramp,
                make_writer=make_io_writer, prom_raw_fn=None, node_of_fn=None, prometheus_node_fn=None, disk_avail_fn=None,
                code_provenance: Optional[dict] = None, preflight_only=False) -> dict:
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
    seed = int(cfg["arrival"]["seeds"][run])
    prom = prom_raw_fn or (lambda sel, a, b: wa.prom_raw(PROM_URL, sel, a, b))
    node_of = node_of_fn or _node_of
    prom_node = (prometheus_node_fn or (lambda: node_of("monitoring", "prometheus-kube-prom-kube-prometheus-prometheus-0")))()
    disk = disk_avail_fn or (lambda: _disk_avail(prom))
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
    try:
        avail = disk()
    except Exception as e:  # noqa: BLE001
        avail = None
        reasons.append(f"disk_avail_error:{type(e).__name__}")
    if avail is not None and avail < MIN_DISK_AVAIL_BYTES:
        reasons.append(f"disk_avail={avail}")
    preflight.update(policy=policy_check, old_policy=old_policy, delay_before=delay_before, canonical_start=cp, active_pod=p0,
                     prometheus_node=prom_node, disk_avail_bytes=avail, schedule=sched, ok=not reasons, reasons=reasons)
    pf = results_dir / f"preflight-{run_id}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.json"
    with open(pf, "x", encoding="utf-8") as fh:
        fh.write(json.dumps(preflight, ensure_ascii=False, indent=2, default=str))
    if reasons:
        raise SystemExit(f"사전검사 실패 - 시작 안 함: {reasons}")
    if preflight_only:
        return {"preflight_only": True, "run_id": run_id}
    contract = {"run_id": run_id, "run": run, "kind": kind, "plan_id": PLAN_ID, "contract_doc": CONTRACT_DOC, "counted": False,
                "purpose": "Stage 0 I/O feasibility only - 학습·보정·검증 데이터 아님", "config_sha256": _sha(CONFIG),
                "writer_sha256": _sha(WRITER_SCRIPT), "plan": plan, "timing": cfg["timing"], "run_seed": seed,
                "target_node": TARGET_NODE, "detector": None, "signal_send": False, "mode": MODE_RO,
                "code_provenance": code_provenance}
    (results_dir / f"contract-{run_id}.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")
    marker = results_dir / f"running-{run_id}.json"
    since = datetime.now(timezone.utc)
    marker.write_text(json.dumps({"run_id": run_id, "started_at": since.isoformat()}), encoding="utf-8")
    cleanup_errors, err, uid_flags = [], None, []

    def safety(state, prober_):
        deps.base_safety(None, "no_injection")(state, prober_)
        r = deps.old_policy_check()
        if r.get("isolated") is not True:
            raise HarnessCorrupted(f"기존 recovery-policy 격리 풀림: {r.get('reasons')}")
    try:
        ramp = make_ramp(str(CONFIG), run_id, rep, seed, results_dir)
        writer = make_writer(run_id, rep, results_dir, cfg) if plan else None
        prober = deps.make_prober(run_id, rep, "no_injection", PROBE_LIFETIME_SEC)
        probe_path = deps.probe_csv_path(run_id, rep)

        def probe_rows():
            prober.get_baseline_status()   # 동결 prober의 로컬 캐시 갱신
            return slo_judge.load_raw(probe_path) if probe_path and Path(probe_path).exists() else []

        def readiness(t):
            probs = readiness_check(p0["uid"], p0["name"], deps.canonical_snapshot, prom, t)
            uid_flags.append("target_uid_changed" not in probs)
            return probs
        outcome = trial(kind, plan, cfg["timing"], ramp=ramp, writer=writer, prober=prober, safety=safety,
                        probe_rows=probe_rows, readiness_fn=readiness,
                        disk_ok_fn=lambda: (disk() or 0) >= MIN_DISK_AVAIL_BYTES,
                        sleep=deps.sleep_fn, monotonic=deps.monotonic_fn)
        outcome["background_pod"] = getattr(ramp, "info", None)
        outcome["writer_pod"] = getattr(writer, "info", None)
        if writer is not None and node_of(lra.NAMESPACE, writer.info["pod"]) is not None:
            cleanup_errors.append(f"writer_pod_remaining: {writer.info['pod']}")
    except BaseException as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
        outcome = {"status": "harness_error", "reason": err, "cleanup_status": "unknown"}
    if outcome.get("cleanup_status") != "complete":
        cleanup_errors.append("trial_cleanup_incomplete: " + str([outcome.get(k) for k in (
            "prober_stop_failed", "background_cleanup_failed", "writer_cleanup_failed")]))
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
        a = datetime.fromisoformat(outcome.get("probe_started_utc") or since.isoformat()) - timedelta(seconds=60)
        b = datetime.fromisoformat(outcome.get("ended_at_utc") or datetime.now(timezone.utc).isoformat()) + timedelta(seconds=60)
        sel = ia.selectors(p0["name"], TARGET_NODE_INSTANCE)
        m = {"pod": p0["name"], "node_instance": TARGET_NODE_INSTANCE, "start_utc": a.isoformat(), "end_utc": b.isoformat(),
             "sample_kind": "raw_scrape", "selectors": sel, "series": {k: prom(s, a, b) for k, s in sel.items()}}
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
               "probe_raw_csv": str(probe_csv) if probe_csv else None, "target_pod": p0, "end_pod": end_pod,
               "target_uid_constant": bool(uid_flags) and all(uid_flags) and (end_pod or {}).get("uid") == p0["uid"],
               "prometheus_node": prom_node, "run_seed": seed, "canonical_end": canonical_end, "policy_state_after": policy_after,
               "cleanup_errors": cleanup_errors, "metrics_error": metrics_error, "pod_events": events,
               "preflight_file": pf.name, "trial_outcome": outcome, "code_provenance": code_provenance}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if not cleanup_errors:
        marker.unlink()
    return summary


def _node_of(namespace: str, pod: str) -> Optional[str]:  # pragma: no cover - 라이브 읽기
    r = subprocess.run(["kubectl", "get", "pod", pod, "-n", namespace, "-o", "jsonpath={.spec.nodeName}"],
                       capture_output=True, text=True, timeout=20)
    return r.stdout.strip() or None


def _disk_avail(prom) -> Optional[float]:  # pragma: no cover - 라이브 읽기
    now = datetime.now(timezone.utc)
    pts = prom(f'node_filesystem_avail_bytes{{instance="{TARGET_NODE_INSTANCE}",mountpoint="/"}}', now - timedelta(seconds=120), now)
    return pts[-1][1] if pts else None


def main(argv=None) -> int:  # pragma: no cover - 세션 실행기 guarded_pilot_session.ps1이 부른다
    p = argparse.ArgumentParser(description="Stage 0 I/O feasibility(counted=false)")
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
