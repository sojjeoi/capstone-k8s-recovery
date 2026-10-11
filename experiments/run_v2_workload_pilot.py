#!/usr/bin/env python3
"""AIOps v2 workload feasibility pilot 실행기(새 파일, 2026-10-09, counted=false) - 계약:
docs/design/phase8-aiops-v2-workload-feasibility-contract-20261009.md

질문은 하나: 정상이면서 vLLM service-state metric이 움직이는 부하 수준이 있는가. 탐지기(IF·CPU 임계값)를 띄우지 않는다.
동결 부품 재사용(수정 없음): run_guarded_pilot.real_deps(사전검사·정책 확인·probe·무주입 안전 감시·canonical·기존 정책 격리 확인),
load_ramp_adapter의 kubectl 헬퍼·이미지(동결 ramp.py의 요청 전송 함수), slo_judge/slo_episodes(단계 판정·분석기).
안전 감시 차이 하나: 동결 무주입 안전 감시의 "SLO 위반 즉시 중단"만 끄고(이 pilot은 SLO 경계를 재는 것이 목적), 나머지
(probe 생존·active Pod 1개·OOM·재시작·노드 상태·기존 정책 격리)는 그대로 중단 사유. SLO는 분석기가 공식 판정으로 기록.
preview·승격·guarded context·신호 없음. 실행 순서: probe → 기준선 300 s(L0) → ramp L1..L5(각 300 s) → 정리 → metric 저장.
r2(freeze 후보 v2): ramp를 단계마다 따로 실행(chaos/loadgen/v2_arrival.py, jittered_grid_v1, 실행 ID에 묶인 사전 고정 seed).
단계가 끝나면 공식 SLO 판정(분석기와 같은 함수) - 위반이면 그 단계 결과는 보존하고 더 높은 단계는 시작하지 않는다
(ramp_terminated_after_slo_boundary, invalid 아님). 단계 진행 중에는 SLO로 멈추지 않는다."""
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
import run_guarded_pilot as gp
import run_if_threshold_candidate_smoke as smoke
import run_timed_switch_recovery_trial as rt
import slo_judge
import v2_workload_pilot_analysis as wa
from run_guarded_pilot import BASE_SCALE_DOWN_DELAY_SEC, MODE_RO, Deps, check_policy
from run_once import HarnessCorrupted, TrialInvalid

PLAN_ID = "capstone-v2-workload-feasibility-20261009"
CONTRACT_DOC = "docs/design/phase8-aiops-v2-workload-feasibility-contract-20261009.md"
REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = Path(__file__).parent / "results" / "capstone_v2_workload_pilot"
LADDER = REPO_ROOT / "chaos" / "v2-workload-ladder.yaml"
RUNS = {f"v2wl{n}": (n, 3000 + n) for n in (90, 91, 92)}   # 유효 목표 2, 최대 시도 3
ARRIVAL_SCRIPT = REPO_ROOT / "chaos" / "loadgen" / "v2_arrival.py"
# 이미지 loadgen-runner:phase8-v3-boundaries 내장 /ramp.py(동결, load_ramp_adapter.py 주석의 smoke 확인값) - v2_arrival이 import
RAMP_PY_SHA256 = "aadab9fc7f2a5a51cfee4e666ba7872c8e8fa378389d47a0e68e501f39153a82"
BASELINE_SEC = 300.0
STAGE_TIMEOUT_SEC = 300 + 10 + 60           # 단계 길이 + straggler 대기 + 여유
PROBE_CATCHUP_TIMEOUT_SEC = 60.0            # 단계 끝 뒤 probe 증거가 끝 시각을 덮을 때까지(1 RPS, 5 s 갱신)
RAMP_POD_SLEEP_SEC = 2400                   # 단계 5개 + 단계 사이 판정 + 여유(실제 종료는 cleanup의 pod 삭제)
SAFETY_POLL_SEC = 5.0
PROM_URL = "http://127.0.0.1:9090"            # 세션 실행기의 Prometheus port-forward
ARM = "v2wl"
FREEZE_VERSION = "v2wl-v2"
FREEZE_MANIFEST = "docs/design/phase8-aiops-v2-workload-feasibility-freeze-manifest-v2-20261009.json"
PROVENANCE_REQUIRED = {"contract": CONTRACT_DOC, "runner": "experiments/run_v2_workload_pilot.py",
                       "analyzer": "experiments/v2_workload_pilot_analysis.py", "ladder": "chaos/v2-workload-ladder.yaml",
                       "arrival": "chaos/loadgen/v2_arrival.py"}


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def verify_provenance(manifest: Optional[str] = None, root: Optional[Path] = None) -> dict:
    """run_guarded_normal_bc.verify_provenance와 같은 규칙(이 시리즈 버전·필수 항목). 실패면 SystemExit(클러스터 접근 전)."""
    manifest, root = manifest or FREEZE_MANIFEST, root or REPO_ROOT
    try:
        raw = (root / manifest).read_bytes()
        m = json.loads(raw)
        files = m["files"]
        if m.get("status") != "frozen" or m.get("version") != FREEZE_VERSION or not isinstance(files, dict):
            raise ValueError(f"status={m.get('status')} version={m.get('version')}")
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SystemExit(f"provenance fail-closed: freeze manifest 읽기·형식 오류 {manifest}: {type(e).__name__}: {e}") from e
    missing = sorted(k for k, rel in PROVENANCE_REQUIRED.items() if rel not in files)
    actual = {rel: _sha(root / rel) if (root / rel).is_file() else None for rel in files}
    bad = sorted(rel for rel, want in files.items() if actual[rel] != want)
    if missing or bad or manifest in files:
        raise SystemExit(f"provenance fail-closed: 필수 항목 누락 {missing}, 해시 불일치 {bad}, 자기 참조 {manifest in files}")
    return {"freeze_version": FREEZE_VERSION, "freeze_manifest_sha256": hashlib.sha256(raw).hexdigest(),
            "verified_files": actual, "verification_status": "verified", "verified_at_utc": datetime.now(timezone.utc).isoformat()}


def run_id_for(run: str) -> str:
    return f"v2_workload_pilot-{run}-{PLAN_ID}"


def check_schedule(run: str, results_dir: Path) -> list:
    """v2wl91은 v2wl90, v2wl92는 v2wl91이 시도(요약 있음)된 뒤에만."""
    n = RUNS[run][0]
    prev = [f"v2wl{n - 1}"] if n > 90 else []
    missing = [r for r in prev if not (results_dir / f"summary-{run_id_for(r)}.json").exists()]
    if missing:
        raise SystemExit(f"{run} 거부: 순서 밖 - 먼저 시도할 실행 요약 없음 {missing}")
    return prev


class _NoSloAbort:
    """동결 무주입 안전 감시에 넘기는 prober 대리 - SLO 위반 즉시 중단만 끈다(나머지 호출은 원본 그대로)."""

    def __init__(self, prober):
        self._p = prober

    def check_slo_violation(self):
        return False

    def __getattr__(self, name):
        return getattr(self._p, name)


def run_seed_for(run: str) -> int:
    """실행 ID에 묶인 사전 고정 seed(ladder arrival.seeds). 인자로 바꿀 수 없다 - 재시도도 같은 ID면 같은 seed."""
    return int(yaml.safe_load(LADDER.read_text(encoding="utf-8"))["arrival"]["seeds"][run])


def make_stage_ramp(ladder_path: str, run_id: str, rep: int, run_seed: int, results_dir: Path):
    """ramp pod 하나에서 단계마다 v2_arrival.py를 따로 실행한다(동결 load_ramp_adapter의 kubectl 헬퍼 재사용).
    prepare에서 pod 안 /ramp.py·/v2_arrival.py 해시를 대조한다(fail-closed)."""
    pod = f"ramp-v2wl-{rep}-{hashlib.sha256(run_id.encode()).hexdigest()[:6]}"
    cfg = Path(ladder_path).name
    ns = lra.NAMESPACE
    info = {"pod": pod, "in_pod_sha256": None}

    def sh(cmd):
        return lra._run(["kubectl", "exec", "-n", ns, pod, "--", "sh", "-c", cmd])

    def prepare():
        lra._run(["kubectl", "run", pod, "-n", ns, f"--image={lra.IMAGE}", "--image-pull-policy=Never", "--restart=Never",
                  "--", "sleep", str(RAMP_POD_SLEEP_SEC)], check=True)
        if not lra._wait_pod_ready(pod):
            raise TrialInvalid(f"{pod} Ready 시간초과")
        for src in (ladder_path, str(ARRIVAL_SCRIPT)):
            lra._run(["kubectl", "cp", os.path.relpath(src), f"{ns}/{pod}:/{Path(src).name}"], check=True)
        r = sh("python -c \"import hashlib,sys;[print(hashlib.sha256(open(p,'rb').read()).hexdigest()) for p in sys.argv[1:]]\""
               f" /ramp.py /v2_arrival.py /{cfg}")
        got = (r.stdout or "").split()
        want = [RAMP_PY_SHA256, _sha(ARRIVAL_SCRIPT), _sha(Path(ladder_path))]
        info["in_pod_sha256"] = dict(zip(["/ramp.py", "/v2_arrival.py", f"/{cfg}"], got))
        if got != want:
            raise TrialInvalid(f"ramp pod 파일 해시 불일치: {info['in_pod_sha256']}")
        time.sleep(lra.SETTLE_SEC)

    def start(i):
        inner = (f"PYTHONUNBUFFERED=1 python /v2_arrival.py --config /{cfg} --stage {i} --run-seed {run_seed} "
                 f"--summary-out /s{i}.csv --raw-out /r{i}.csv > /l{i}.log 2>&1; echo $? > /e{i}")
        lra._run(["kubectl", "exec", "-n", ns, pod, "--", "sh", "-c", f"nohup sh -c '{inner}' < /dev/null > /w{i}.log 2>&1 &"],
                 check=True)

    def done(i):
        r = sh(f"cat /e{i} 2>/dev/null")
        if r.returncode != 0 or not (r.stdout or "").strip():
            return False
        if r.stdout.strip() != "0":
            raise TrialInvalid(f"v2_arrival 단계 {i} 비정상 종료(exit={r.stdout.strip()}): {sh(f'tail -c 2000 /l{i}.log').stdout}")
        return True

    def fetch(i):
        """단계 summary·raw를 받아 results_dir에 보존(x 모드)하고 summary 행 + raw_csv 경로를 돌려준다."""
        out = {}
        for kind, remote in (("summary", f"/s{i}.csv"), ("raw", f"/r{i}.csv")):
            r = lra._run(["kubectl", "exec", "-n", ns, pod, "--", "cat", remote])
            if r.returncode != 0 or not (r.stdout or "").strip():
                raise TrialInvalid(f"단계 {i} {kind} CSV 회수 실패: rc={r.returncode}")
            p = results_dir / f"ramp-stage-{run_id}-{i}-{kind}.csv"
            with open(p, "x", encoding="utf-8", newline="") as fh:
                fh.write(r.stdout)
            out[kind] = (p, r.stdout)
        row = next(csv.DictReader(io.StringIO(out["summary"][1])))
        return {**row, "raw_csv": str(out["raw"][0])}

    def cleanup():
        lra._delete_pod(pod)

    return SimpleNamespace(prepare=prepare, start=start, done=done, fetch=fetch, cleanup=cleanup, info=info)


def trial(ramp, prober, safety, probe_rows, stages, *, sleep=time.sleep, monotonic=time.monotonic,
          now=lambda: datetime.now(timezone.utc), baseline_sec=BASELINE_SEC):
    """stages = ladder 단계 목록(순서대로). 단계 i가 끝나면 probe 증거가 끝 시각을 덮을 때까지 기다려 공식 SLO 판정
    (wa.stage_slo_verdict) - 위반이면 단계 결과 보존·더 높은 단계 시작 안 함·completed. 판정 불가면 aborted(fail-closed)."""
    out = {"status": None, "reason": None, "baseline_window": None, "ramp_started_at_utc": None, "ramp_stages": [],
           "stage_slo_verdicts": [], "ramp_terminated_after_slo_boundary": False, "slo_boundary_stage": None,
           "stages_not_run": [], "prober_stop_failed": None, "injector_cleanup_failed": None, "cleanup_status": "incomplete"}
    state = {"active_restart_count": None}
    proxy = _NoSloAbort(prober)

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
    try:
        prober.start()
        if not watch(120, until=prober.is_alive):
            raise TrialInvalid("probe 시작 확인 실패")
        if not watch(smoke.BASELINE_TIMEOUT_SEC, until=lambda: prober.get_baseline_status().get("ready")):
            raise TrialInvalid("SLO 기준선 준비 상한 초과")
        b0 = now()
        watch(baseline_sec)
        out["baseline_window"] = {"start_utc": b0.isoformat(), "end_utc": now().isoformat()}
        ramp.prepare()
        safety(state, proxy)
        out["ramp_started_at_utc"] = now().isoformat()
        for i, st in enumerate(stages, 1):
            ramp.start(i)
            if not watch(STAGE_TIMEOUT_SEC, until=lambda: ramp.done(i)):
                raise TrialInvalid(f"단계 {st['name']} 종료 상한 초과")
            row = ramp.fetch(i)
            out["ramp_stages"].append(row)
            s, e = wa._ts(row["stage_start_utc"]), wa._ts(row["stage_end_utc"])
            verdict = {}

            def judged():  # 분석기와 같은 함수·같은 a0(기준선 시작)
                verdict["v"] = wa.stage_slo_verdict(probe_rows(), b0, s, e)
                return verdict["v"] is not None
            if not watch(PROBE_CATCHUP_TIMEOUT_SEC, until=judged):
                raise TrialInvalid(f"단계 {st['name']} 끝 뒤 probe 증거 미도착")
            v = verdict["v"]
            out["stage_slo_verdicts"].append({"stage": st["name"], **v})
            if not v["computable"]:
                raise TrialInvalid(f"단계 {st['name']} 공식 SLO 판정 불가: {v['reason']}")
            if v["violating"]:
                out.update(ramp_terminated_after_slo_boundary=True, slo_boundary_stage=st["name"],
                           stages_not_run=[x["name"] for x in stages[i:]])
                break
        out.update(status="completed", reason="SLO 경계에서 ladder 종료" if out["ramp_terminated_after_slo_boundary"]
                   else "기준선 + ladder 완료")
    except (TrialInvalid, HarnessCorrupted, subprocess.CalledProcessError) as e:
        out.update(status="aborted", reason=f"{type(e).__name__}: {e}")
    finally:
        out["ended_at_utc"] = now().isoformat()
        for k, fn in (("prober_stop_failed", prober.stop), ("injector_cleanup_failed", ramp.cleanup)):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                out[k] = f"{type(e).__name__}: {e}"
        out["cleanup_status"] = "complete" if not (out["prober_stop_failed"] or out["injector_cleanup_failed"]) else "incomplete"
    return out


def run_pilot(run: str, *, deps: Deps, results_dir: Optional[Path] = None, make_ramp=make_stage_ramp,
              prom_range_fn=None, code_provenance: Optional[dict] = None, preflight_only=False) -> dict:
    if run not in RUNS:
        raise SystemExit(f"알 수 없는 실행 ID: {run}")
    n, rep = RUNS[run]
    run_id = run_id_for(run)
    results_dir = results_dir or RESULTS_DIR
    results_dir.mkdir(parents=True, exist_ok=True)
    summary_path = results_dir / f"summary-{run_id}.json"
    if summary_path.exists() or (results_dir / f"contract-{run_id}.json").exists():
        raise SystemExit(f"실행 ID 재사용 금지: {run}")
    prev = check_schedule(run, results_dir)
    run_seed = run_seed_for(run)
    deps.check_unresolved()
    contract = {"run_id": run_id, "run": run, "plan_id": PLAN_ID, "contract_doc": CONTRACT_DOC, "counted": False,
                "purpose": "workload/metric feasibility only - 학습·보정·검증·우월성 근거 아님",
                "ladder": str(LADDER.relative_to(REPO_ROOT)).replace("\\", "/"), "ladder_sha256": _sha(LADDER),
                "arrival_rule": wa.v2_arrival.RULE, "arrival_script_sha256": _sha(ARRIVAL_SCRIPT), "run_seed": run_seed,
                "slo_boundary_rule": "단계 종료 후 공식 SLO 위반이면 결과 보존·더 높은 단계 시작 안 함(invalid 아님)",
                "baseline_sec": BASELINE_SEC, "probe_config_sha256": _sha(smoke.DEFAULT_PROBE_CONFIG),
                "detector": None, "signal_send": False, "preview": None, "mode": MODE_RO, "required_before": prev,
                "safety_note": "동결 무주입 안전 감시에서 SLO 즉시 중단만 끔 - SLO는 기록·판정",
                "code_provenance": code_provenance}
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
    p0 = gcg.active_pod(snap or {})
    reasons += [f"canonical_start: {x}" for x in cp] + ([] if p0 else ["active_pod_unknown"])
    preflight.update(policy=policy_check, old_policy=old_policy, delay_before=delay_before, canonical_start=cp,
                     active_pod=p0, ok=not reasons, reasons=reasons)
    pf = results_dir / f"preflight-{run_id}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.json"
    with open(pf, "x", encoding="utf-8") as fh:
        fh.write(json.dumps(preflight, ensure_ascii=False, indent=2, default=str))
    if reasons:
        raise SystemExit(f"사전검사 실패 - 시작 안 함: {reasons}")
    if preflight_only:
        return {"preflight_only": True, "run_id": run_id}
    (results_dir / f"contract-{run_id}.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")
    marker = results_dir / f"running-{run_id}.json"
    since = datetime.now(timezone.utc)
    marker.write_text(json.dumps({"run_id": run_id, "started_at": since.isoformat()}), encoding="utf-8")
    cleanup_errors, err = [], None

    def safety(state, prober_):
        deps.base_safety(None, "no_injection")(state, prober_)
        r = deps.old_policy_check()
        if r.get("isolated") is not True:
            raise HarnessCorrupted(f"기존 recovery-policy 격리 풀림: {r.get('reasons')}")
    try:
        ramp = make_ramp(str(LADDER), run_id, rep, run_seed, results_dir)
        prober = deps.make_prober(run_id, rep, "no_injection", BASELINE_SEC + RAMP_POD_SLEEP_SEC)
        probe_path = deps.probe_csv_path(run_id, rep)

        def probe_rows():
            prober.get_baseline_status()   # probe raw 로컬 캐시 갱신(5 s 제한·연속 실패 시 TrialInvalid - 동결 prober 동작)
            return slo_judge.load_raw(probe_path) if probe_path and Path(probe_path).exists() else []
        stages = yaml.safe_load(LADDER.read_text(encoding="utf-8"))["stages"]
        outcome = trial(ramp, prober, safety, probe_rows, stages, sleep=deps.sleep_fn, monotonic=deps.monotonic_fn)
        outcome["ramp_pod"] = getattr(ramp, "info", None)
    except BaseException as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
        outcome = {"status": "harness_error", "reason": err, "cleanup_status": "unknown"}
    if outcome.get("cleanup_status") != "complete":
        cleanup_errors.append(f"trial_cleanup_incomplete: {outcome.get('prober_stop_failed')} {outcome.get('injector_cleanup_failed')}")
    canonical_end = gcg.wait_canonical(deps.canonical_snapshot, delay=BASE_SCALE_DOWN_DELAY_SEC,
                                       sleep=deps.sleep_fn, monotonic=deps.monotonic_fn)
    if not canonical_end["ok"]:
        cleanup_errors.append(f"canonical_end_not_reached: {canonical_end['problems']}")
    try:
        end_snap = deps.canonical_snapshot()
        if (end_snap or {}).get("active") != (snap or {}).get("active"):
            cleanup_errors.append(f"active_selector_changed: {(snap or {}).get('active')} -> {(end_snap or {}).get('active')}")
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
        start = datetime.fromisoformat((outcome.get("baseline_window") or {}).get("start_utc") or since.isoformat())
        end = datetime.fromisoformat(outcome.get("ended_at_utc") or datetime.now(timezone.utc).isoformat())
        m = wa.capture_metrics(p0["name"], start - timedelta(seconds=30), end + timedelta(seconds=30),
                               prom_range_fn or (lambda e, a, b: wa.prom_raw(PROM_URL, e, a, b)))
        with open(results_dir / f"metrics-{run_id}.json", "x", encoding="utf-8") as fh:
            fh.write(json.dumps(m, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        metrics_error = f"{type(e).__name__}: {e}"
    try:
        events = deps.capture_events(since.isoformat())
    except Exception as e:  # noqa: BLE001
        events = {"error": str(e)}
    probe_csv = deps.probe_csv_path(run_id, rep)
    summary = {"run_id": run_id, "run": run, "counted": False, "outcome_status": outcome.get("status"),
               "outcome_reason": outcome.get("reason"), "baseline_window": outcome.get("baseline_window"),
               "ramp_started_at_utc": outcome.get("ramp_started_at_utc"), "ended_at_utc": outcome.get("ended_at_utc"),
               "ramp_stages": outcome.get("ramp_stages") or [], "run_seed": run_seed,
               "ramp_terminated_after_slo_boundary": outcome.get("ramp_terminated_after_slo_boundary"),
               "slo_boundary_stage": outcome.get("slo_boundary_stage"), "stages_not_run": outcome.get("stages_not_run"),
               "stage_slo_verdicts": outcome.get("stage_slo_verdicts"), "probe_raw_csv": str(probe_csv) if probe_csv else None,
               "active_pod": p0, "canonical_end": canonical_end, "policy_state_after": policy_after,
               "cleanup_errors": cleanup_errors, "metrics_error": metrics_error, "pod_events": events,
               "preflight_file": pf.name, "trial_outcome": outcome, "code_provenance": code_provenance}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if not cleanup_errors:
        marker.unlink()
    return summary


def main(argv=None) -> int:  # pragma: no cover - 라이브 진입점(세션 실행기 guarded_pilot_session.ps1이 부른다)
    p = argparse.ArgumentParser(description="AIOps v2 workload feasibility pilot(counted=false)")
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
    s = run_pilot(a.run, deps=d, code_provenance=prov, preflight_only=a.preflight_only)
    print(f"결과: {s.get('outcome_status') or s} cleanup_errors={s.get('cleanup_errors')}")
    return 2 if s.get("cleanup_errors") else 0


if __name__ == "__main__":
    sys.exit(main())
