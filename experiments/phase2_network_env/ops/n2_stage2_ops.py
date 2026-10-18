"""Stage 2 운영 도구(2026-10-18) - 동결 연구 코드가 아니라 세션 운영 절차다. 사전등록 §8·rev4.
Phase 2 Stage 2는 세션마다 (1) 정책 서버 act 전환 기록 (2) 시계 오차 측정 (3) 실행 출처 기록 (4) act 승인 기록 (5) 세션 (6) record_only 원복 기록을 남겼다.
이 도구가 같은 기록을 같은 형식으로 만든다. 동결된 실행기가 승인 기록을 직접 검증하므로(run_guarded_pilot.load_act_approval, runs == [run]),
여기서 만든 승인 기록이 그 검증을 통과하지 못하면 세션은 시작하지 않는다.

하위 명령(서버를 바꾸는 것은 switch뿐이다):
  switch --mode act|record_only   guarded-recovery-policy의 GUARDED_MODE를 바꾸고 Pod 재시작을 기다린 뒤 identity·state를 읽어 기록한다(kubectl set env).
  clock                            정책 서버와 PC의 시계 오차를 측정한다(읽기 전용).
  approve --run n2sNN              실행 출처 기록과 act 승인 기록(그 run 1회 한정)을 새 파일로 만든다(서버 접근 없음)."""
import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
NET = HERE.parent
ROOT = NET.parents[1]
sys.path.insert(0, str(NET))
sys.path.insert(0, str(NET.parent))

NAMESPACE, DEPLOYMENT = "vllm-serving", "deployment/guarded-recovery-policy"
POLICY_SVC, POLICY_PORT = "svc/guarded-recovery-policy", 18090
# gap01 사용자 확정값 그대로(A/B·B/C/K·Phase 2 Stage 2와 같다, 새 값 없음). 정책 서버 환경 값(GUARDED_MAX_*)과 같아야 시작 점검을 통과한다.
TIMING = {"max_signal_age_sec": 15.0, "max_future_skew_sec": 2.0, "max_window_to_decision_sec": 10.0}
SERVICE_WAIT_DEADLINE_SEC, SCALE_DOWN_WAIT_MARGIN_SEC = 120.0, 180.0
CLOCK_BOUND_MAX_SEC, CLOCK_MIN_OK = 0.5, 10
RULES = ["trigger 없음·정책 거부 시 수동 승격·재전송·재실행 금지 - 미승격 정리(D5→canonical 복원)로 종료(ITT)",
         "결과 불명·실패: 자동 재승격·미검증 롤백·자동 재시도 금지",
         "threshold·압박·관찰 변경 금지, 시작 전 실패 시 같은 ID 재시작 안 함(사용자 보고)"]
SELECTION_DIR = ROOT / "experiments" / "results" / "phase2_network_env_selection"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def text_sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def write_new(path, obj) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8", newline="\n") as fh:                     # 덮어쓰기 금지
        fh.write(json.dumps(obj, ensure_ascii=False, indent=1) + "\n")
    return path


def clock_bound(clock_file) -> float:
    """시계 측정 기록에서 오차 상한(|best offset| + 불확실성). 측정이 모자라거나 0.5 s를 넘으면 승인 기록을 만들지 않는다."""
    cs = json.loads(Path(clock_file).read_text(encoding="utf-8"))["summary"]
    if cs.get("n_ok", 0) < CLOCK_MIN_OK or "best_offset_sec" not in cs:
        raise SystemExit(f"시계 측정 불충분 - 승인 기록 만들지 않음: n_ok={cs.get('n_ok')}")
    bound = abs(cs["best_offset_sec"]) + cs["best_uncertainty_sec"]
    if not math.isfinite(bound) or bound > CLOCK_BOUND_MAX_SEC:
        raise SystemExit(f"시계 오차 {bound:.3f}s > {CLOCK_BOUND_MAX_SEC}s - 승인 기록 만들지 않음")
    return bound


def build_provenance(run: str, run_id: str, *, impl: dict, act_switch_file, active_profile, selection_evidence, wrapper_script,
                     runner_script, shim_script, invocation: dict, generated_at: str) -> dict:
    """세션이 어떤 코드·입력으로 실행되는지 시작 전에 고정하는 기록(해시는 LF 정규화)."""
    def rel(p):
        p = Path(p).resolve()
        return p.relative_to(ROOT).as_posix() if p.is_relative_to(ROOT) else str(p)
    return {"run": run, "run_id": run_id, "generated_at_utc": generated_at,
            "session_wrapper_path": rel(wrapper_script), "session_wrapper_sha256": text_sha256(wrapper_script),
            "runner_path": rel(runner_script), "runner_sha256_lf": text_sha256(runner_script),
            "runner_shim_path": rel(shim_script), "runner_shim_sha256": text_sha256(shim_script),
            "implementation_freeze": {"manifest": impl.get("manifest"), "manifest_sha256": impl.get("manifest_sha256"),
                                      "selection_profile_sha256": impl.get("selection_profile_sha256")},
            "active_profile_sha256": text_sha256(active_profile), "selection_evidence_sha256": text_sha256(selection_evidence),
            "act_switch": {"file": Path(act_switch_file).name, "sha256": text_sha256(act_switch_file)},
            "wrapper_invocation": invocation}


def build_approval(run: str, *, clock_file, approved_by: str, approved_at: str, impl: dict, contract_doc: str, provenance_path, provenance_sha: str,
                   scope: str) -> dict:
    """그 run 1회에 한정한 act 승인 기록. 동결 실행기의 load_act_approval이 검증하는 필드(status·timing_source·clock_measurement_file·timing·
    service_wait_deadline_sec·approved_by/at)와 runs == [run]을 지킨다."""
    bound = clock_bound(clock_file)
    return {"status": "final", "run": run, "runs": [run], "run_id": run, "launch_attempt": 1, "scope": scope,
            "approved_by": approved_by, "approved_at_utc": approved_at, "contract_doc": contract_doc,
            "freeze": {"version": "phase2-network-env-impl", "manifest": impl.get("manifest"), "manifest_sha256": impl.get("manifest_sha256")},
            "inherits_values_from": "experiments/results/phase2_stage2/ops-p2s01/p2s01-act-approval-20261012T020158Z.json (gap01 사용자 확정값, 변경 없음)",
            "timing_source": "gap01 승인값 재사용(A/B r2·B/C/K·Phase 2 Stage 2와 동일)", "clock_measurement_file": str(Path(clock_file).resolve()),
            "clock_bound_sec": bound, "timing": dict(TIMING), "service_wait_deadline_sec": SERVICE_WAIT_DEADLINE_SEC,
            "scale_down_wait_margin_sec": SCALE_DOWN_WAIT_MARGIN_SEC, "execution_provenance_path": str(Path(provenance_path).resolve()),
            "execution_provenance_sha256": provenance_sha, "rules": list(RULES)}


# ---------------- 서버를 바꾸는 유일한 단계 ----------------
def _run(cmd, **kw):
    cp = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)
    return {"rc": cp.returncode, "out": cp.stdout.strip()[-600:], "err": cp.stderr.strip()[-600:], "at_utc": now_utc()}


class PolicyTunnel:
    """kubectl port-forward를 잠시 열어 정책 서버를 읽는다(읽기 전용). 끝나면 닫는다."""
    def __init__(self, popen=subprocess.Popen):
        self.popen, self.proc = popen, None

    def __enter__(self):
        self.proc = self.popen(["kubectl", "port-forward", "-n", NAMESPACE, POLICY_SVC, f"{POLICY_PORT}:8080"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(4)
        return f"http://127.0.0.1:{POLICY_PORT}"

    def __exit__(self, *exc):
        if self.proc:
            self.proc.terminate()


def read_policy(url):
    import run_guarded_pilot as gp
    token = os.environ.get("GUARDED_ADMIN_TOKEN")
    if not token:
        raise SystemExit("GUARDED_ADMIN_TOKEN 환경변수 필요")
    c = gp.PolicyClient(url, token)
    return c.identity(), c.state()


def switch_policy(mode: str, *, label: str, out_dir, run=_run, reader=None, tunnel=PolicyTunnel, sleep=time.sleep) -> dict:
    """정책 서버 모드 전환(act ↔ record_only). 전환 뒤 identity.mode가 기대값이고 정책 상태가 깨끗(context 없음·blocked 없음·unresolved 없음)해야 ok.
    기록은 성공·실패와 관계없이 새 파일로 남긴다(실패하면 SystemExit)."""
    if mode not in ("act", "record_only"):
        raise SystemExit(f"알 수 없는 모드: {mode}")
    rec = {"label": label, "mode": mode, "started_utc": now_utc(), "procedure": "kubectl set env + rollout status + identity/state 확인(Phase 2 bc90_switch와 같은 단계)"}
    rec["set_env"] = run(["kubectl", "set", "env", DEPLOYMENT, "-n", NAMESPACE, f"GUARDED_MODE={mode}"])
    rec["rollout"] = run(["kubectl", "rollout", "status", DEPLOYMENT, "-n", NAMESPACE, "--timeout=240s"]) if rec["set_env"]["rc"] == 0 else None
    probs = []
    if rec["set_env"]["rc"] != 0 or not rec["rollout"] or rec["rollout"]["rc"] != 0:
        probs.append("set_env_or_rollout_failed")
    else:
        sleep(3)
        try:
            with tunnel() as url:
                rec["identity"], rec["state"] = (reader or read_policy)(url)
        except Exception as e:  # noqa: BLE001
            probs.append(f"policy_read_failed: {type(e).__name__}: {e}")
        else:
            if rec["identity"].get("mode") != mode:
                probs.append(f"identity_mode={rec['identity'].get('mode')} != {mode}")
            if mode == "act" and rec["identity"].get("timing") != TIMING:
                probs.append(f"policy_timing {rec['identity'].get('timing')} != approved {TIMING}")
            st = rec["state"]
            if st.get("mode") != mode or st.get("context") is not None or st.get("blocked") or st.get("unresolved"):
                probs.append(f"policy_state_not_clean: {st}")
    rec["ok"], rec["problems"], rec["ended_utc"] = not probs, probs, now_utc()
    path = write_new(Path(out_dir) / f"{label}.json", rec)
    if probs:
        raise SystemExit(f"{label} 실패(기록 {path}): {probs}")
    return rec


def measure_clock(out_file, *, run=_run, tunnel=PolicyTunnel):
    with tunnel() as url:
        r = run([sys.executable, str(ROOT / "experiments" / "measure_clock_offset.py"), "--base-url", url,
                 "--path-note", "PC->port-forward->guarded Pod (network-env stage2 act)", "--out", str(out_file)])
    if r["rc"] != 0:
        raise SystemExit(f"시계 측정 실패: {r}")
    return clock_bound(out_file)


# ---------------- CLI ----------------
def cmd_approve(a) -> int:
    import n2_freeze as fz
    import n2_stage1 as ns1
    import n2_stage2 as n2s2
    out = Path(a.out_dir)
    impl = fz.verify_implementation_freeze()
    s1 = ns1.load_stage1()
    run_id = n2s2.load_stage2(s1).run_id_for(a.run)
    wrapper = ROOT / "experiments" / "guarded_pilot_session.ps1"
    runner, shim = NET / "n2_run.py", HERE / "n2_stage2_shim.py"
    act_switch = out / f"act-switch-{a.run}.json"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    now = now_utc()
    invocation = {"script": "experiments/guarded_pilot_session.ps1", "Run": a.run, "Mode": "act",
                  "RunnerScript": str(shim), "ActApproval": str(out / f"{a.run}-act-approval-{stamp}.json")}
    prov = build_provenance(a.run, run_id, impl=impl, act_switch_file=act_switch, active_profile=SELECTION_DIR / "active-profile.json",
                            selection_evidence=SELECTION_DIR / "phase2-network-selection-evidence.json", wrapper_script=wrapper, runner_script=runner,
                            shim_script=shim, invocation=invocation, generated_at=now)
    prov_path = write_new(out / f"execution-provenance-{a.run}-{stamp}.json", prov)
    approval = build_approval(a.run, clock_file=a.clock, approved_by=a.approved_by, approved_at=now, impl=impl, contract_doc=ns1.NetConfig().prereg_doc,
                              provenance_path=prov_path, provenance_sha=text_sha256(prov_path),
                              scope=f"네트워크 지연 Stage 2 act 세션 {a.run} 1회 한정. 다른 세션 시작 금지. 다른 ID 재사용 불가")
    path = write_new(invocation["ActApproval"], approval)
    print(json.dumps({"approval": str(path), "provenance": str(prov_path), "clock_bound_sec": approval["clock_bound_sec"]}, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("switch")
    s.add_argument("--mode", required=True, choices=["act", "record_only"])
    s.add_argument("--label", required=True)
    s.add_argument("--out-dir", required=True)
    s = sub.add_parser("clock")
    s.add_argument("--out", required=True, type=Path)
    s = sub.add_parser("approve")
    s.add_argument("--run", required=True)
    s.add_argument("--clock", required=True, type=Path)
    s.add_argument("--approved-by", required=True)
    s.add_argument("--out-dir", required=True)
    a = p.parse_args(argv)
    if a.cmd == "switch":
        r = switch_policy(a.mode, label=a.label, out_dir=a.out_dir)
        print(json.dumps({"ok": r["ok"], "mode": r["identity"]["mode"], "state": r["state"]}, ensure_ascii=False))
        return 0
    if a.cmd == "clock":
        print(json.dumps({"clock_bound_sec": measure_clock(a.out)}))
        return 0
    return cmd_approve(a)


if __name__ == "__main__":
    sys.exit(main())
