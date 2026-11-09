"""13 세션 실행 순서 관리 (2026-10-22 사용자 승인 범위). 결과와 무관하게 사전 고정한 순서로 진행하고, 세션마다 복원을 확인한다.

순서(사전 고정): F2 블록1 H-W-G, 블록2 W-G-H, 블록3 G-H-W, F3 블록1 H-G, 블록2 G-H.
- H/W: run_hw.py, G: guarded_pilot_session.ps1 -> g_stage2.py (act 전환 -> 시계 -> 승인 기록 -> 세션 -> record_only 원복 -> 유효성).
- 명백한 하네스 오류(classification == harness_error)로 무효인 세션만 1회 다시 한다(원기록 보존). 복구 실패·미회복은 유효한 결과다.
- 시한: 2026-10-27 18:00(로컬) 이후에는 새 세션을 시작하지 않는다.
사용법: python orchestrate.py [--only INDEX[,INDEX...]] [--dry-run] [--g-preflight-only RUN]
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hc_common as hc  # noqa: E402

ROOT, EXP = hc.ROOT, hc.EXP
NET = EXP / "phase2_network_env"
for p in (str(NET), str(NET / "ops"), str(EXP), str(EXP / "phase2_detector_selection")):
    if p not in sys.path:
        sys.path.insert(0, p)

SEQ = [("f2", 1, "H"), ("f2", 1, "W"), ("f2", 1, "G"),
       ("f2", 2, "W"), ("f2", 2, "G"), ("f2", 2, "H"),
       ("f2", 3, "G"), ("f2", 3, "H"), ("f2", 3, "W"),
       ("f3", 1, "H"), ("f3", 1, "G"),
       ("f3", 2, "G"), ("f3", 2, "H")]
G_RUN = {("f2", 1): "n2s11", ("f2", 2): "n2s12", ("f2", 3): "n2s13", ("f3", 1): "n2s14", ("f3", 2): "n2s15"}
G_RERUN = {("f2", 1): "n2s21", ("f2", 2): "n2s22", ("f2", 3): "n2s23", ("f3", 1): "n2s24", ("f3", 2): "n2s25"}
DEADLINE = datetime(2026, 10, 27, 18, 0, 0)
STATE = hc.RESULTS / "orchestrator-state.json"
APPROVAL_QUOTE = ("user (chat 2026-10-22: '이번 지시는 아래 범위의 연결 모듈 구현, 필요한 클러스터 설정 변경, 기능 점검, 본 실험, 세션별 복원, 분석까지 "
                  "실행 승인입니다. 매 세션마다 승인을 다시 묻지 말고 진행하세요.' / '13세션은 결과와 무관하게 정해진 순서로 실행하세요.')")


def load_state():
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"sessions": {}}


def save_state(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def run_hw(arm, fault, block, tag="", check=False, extra=()):
    log = hc.RESULTS / "logs" / f"{'check' if check else 'main'}-{arm}-{fault}-b{block}{('-' + tag) if tag else ''}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-u", str(HERE / "run_hw.py"), "--arm", arm, "--fault", fault, "--block", str(block)]
    cmd += (["--tag", tag] if tag else []) + (["--check"] if check else []) + list(extra)
    with open(log, "a", encoding="utf-8") as fh:
        rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=ROOT, env=dict(os.environ, PYTHONUTF8="1")).returncode
    run_id = f"hc-{fault}-{arm.lower()}-b{block}{('-' + tag) if tag else ''}"
    sfile = hc.RESULTS / ("check" if check else "main") / run_id / "summary.json"
    summ = json.loads(sfile.read_text(encoding="utf-8")) if sfile.exists() else {}
    cls = summ.get("classification") or "no_summary"
    return {"kind": "HW", "run_id": run_id, "rc": rc, "classification": cls, "summary": str(sfile), "log": str(log),
            "restore_error": summ.get("restore_error")}


def uniq(out_dir, label):
    """이미 있는 기록은 덮어쓰지 않으므로, 같은 이름이 있으면 시각 접미사를 붙인다."""
    if not (Path(out_dir) / f"{label}.json").exists():
        return label
    return f"{label}-{datetime.utcnow().strftime('%H%M%S')}"


def g_context(run, fault):
    import g_stage2 as g
    s1, s2, _, d = g.load(run)
    profile = g.SEL / "active-profile.json" if fault == "f2" else g.build_f3_profile()
    return g, s2, d, profile


def run_g(run, fault, *, preflight_only=False, contract_doc="docs/design/HPA-COMPARISON-DESIGN-DRAFT-20261022.md"):
    """G 세션 1회. 어떤 경우에도 마지막에 정책을 record_only 로 되돌린다."""
    import n2_freeze as fz
    import n2_stage2_ops as ops
    g, s2, d, profile = g_context(run, fault)
    run_id = s2.run_id_for(run)
    out_dir = d.parent / ("ops-check" if preflight_only else "ops") / run
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {"kind": "G", "run": run, "run_id": run_id, "fault": fault, "steps": {}}
    sampler = None
    try:
        probs = hc.other_experiment_problems()
        if probs:
            result["classification"] = "blocked_by_other_work"
            result["problems"] = probs
            return result
        result["steps"]["delay_restored"] = hc.restore_delay_if_needed()
        result["steps"]["placement"] = hc.ensure_active_on("sj-worker")     # 서비스 Pod 를 sj-worker 에 두어 preview 가 sj-worker2 에 놓이게 한다
        hc.log(f"[G {run}] act 전환")
        act_label = uniq(out_dir, f"act-switch-{run}")
        ops.switch_policy("act", label=act_label, out_dir=out_dir)
        clock = out_dir / f"clock-offset-{act_label}.json"
        ops.measure_clock(clock)
        impl = g.tolerant_verify()
        wrapper = ROOT / "experiments" / "guarded_pilot_session.ps1"
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        ap_path = out_dir / f"{run}-act-approval-{stamp}.json"
        now = ops.now_utc()
        inv = {"script": "experiments/guarded_pilot_session.ps1", "Run": run, "Mode": "act", "RunnerScript": str(g.HERE / "g_stage2.py"),
               "ActApproval": str(ap_path)}
        prov = ops.build_provenance(run, run_id, impl=impl, act_switch_file=out_dir / f"{act_label}.json", active_profile=profile,
                                    selection_evidence=g.SEL / "phase2-network-selection-evidence.json", wrapper_script=wrapper,
                                    runner_script=g.HERE / "g_stage2.py", shim_script=g.HERE / "g_stage2.py", invocation=inv, generated_at=now)
        prov_path = ops.write_new(out_dir / f"execution-provenance-{run}-{stamp}.json", prov)
        approval = ops.build_approval(run, clock_file=clock, approved_by=APPROVAL_QUOTE + f", approval scope 정확히 runs:[\"{run}\"]", approved_at=now,
                                      impl=impl, contract_doc=contract_doc, provenance_path=prov_path,
                                      provenance_sha=ops.text_sha256(prov_path), scope=f"HPA 비교 G 세션 {run} 1회 한정. 다른 ID 재사용 불가")
        ops.write_new(ap_path, approval)
        sampler = hc.Sampler(d / f"timeline-{run}{'-preflight' if preflight_only else ''}.jsonl")
        sampler.start()
        cmd = ["powershell", "-NoProfile", "-File", str(wrapper), "-Run", run, "-Mode", "act", "-ActApproval", str(ap_path),
               "-RunnerScript", str(g.HERE / "g_stage2.py"), "-SessionRoot", str(Path.home() / "n2-sessions"), "-PythonExe", sys.executable]
        if preflight_only:
            cmd.append("-PreflightOnly")
        hc.log(f"[G {run}] 세션 시작{' (preflight-only)' if preflight_only else ''}")
        log = hc.RESULTS / "logs" / f"G-{run}{'-preflight' if preflight_only else ''}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as fh:
            rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=ROOT,
                                env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")).returncode
        result["steps"]["wrapper_rc"], result["log"] = rc, str(log)
    except BaseException as e:  # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {e}"
        hc.log(f"[G {run}] 오류: {result['error']}")
    finally:
        if sampler:
            sampler.stop()
        try:
            ops.switch_policy("record_only", label=uniq(out_dir, f"revert-record-only-after-{run}{'-preflight' if preflight_only else ''}"), out_dir=out_dir)
            result["steps"]["reverted"] = True
        except BaseException as e:  # noqa: BLE001
            result["steps"]["reverted"] = False
            result["revert_error"] = f"{type(e).__name__}: {e}"
    if not preflight_only and result.get("classification") != "blocked_by_other_work":
        try:
            r = subprocess.run([sys.executable, str(g.HERE / "g_stage2.py"), "--run", run, "--validity"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", cwd=ROOT, env=dict(os.environ, PYTHONUTF8="1"))
            result["steps"]["validity"] = (r.stdout or r.stderr)[-400:]
        except BaseException as e:  # noqa: BLE001
            result["steps"]["validity_error"] = f"{type(e).__name__}: {e}"
        try:
            hc.wait_until(lambda: hc.settled(1), 900, 5.0, "G 종료 후 base 상태")
            result["steps"]["delay_restored_after"] = hc.restore_delay_if_needed()
            result["steps"]["base_restored"] = True
        except BaseException as e:  # noqa: BLE001
            result["steps"]["base_restored"] = False
            result["restore_error"] = f"{type(e).__name__}: {e}"
        sess = d / f"p2-s2-session-{run}.json"
        if sess.exists():
            rec = json.loads(sess.read_text(encoding="utf-8"))
            result["session"] = {"outcome_status": rec.get("outcome_status"), "result": rec.get("result"), "cleanup_errors": rec.get("cleanup_errors")}
            harness = bool(rec.get("cleanup_errors")) or rec.get("outcome_status") != "completed" or bool(result.get("restore_error")) \
                or not result["steps"].get("reverted")
            result["classification"] = "harness_error" if harness else "valid"
        else:
            result["classification"] = "harness_error"
    return result


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--g-preflight-only", default="")
    a = ap.parse_args(argv)
    os.chdir(ROOT)
    if a.g_preflight_only:
        import g_stage2 as g
        run = a.g_preflight_only
        print(json.dumps(run_g(run, g.RUNS_G[run][0], preflight_only=True), ensure_ascii=False, indent=1, default=str))
        return 0
    st = load_state()
    todo = [int(x) for x in a.only.split(",") if x] or list(range(1, len(SEQ) + 1))
    for i in todo:
        fault, block, arm = SEQ[i - 1]
        key = f"{i:02d}-{fault}-b{block}-{arm}"
        done = st["sessions"].get(key)
        if done and done.get("final"):
            continue
        if datetime.now() >= DEADLINE:
            hc.log(f"시한(10/27 18:00) 경과 - {key} 는 미실행으로 남긴다")
            st["sessions"][key] = {"final": True, "status": "not_run_deadline"}
            save_state(st)
            continue
        hc.log(f"===== 세션 {i}/{len(SEQ)}: {key} =====")
        if a.dry_run:
            continue
        attempts = list((done or {}).get("attempts") or [])
        for attempt in range(len(attempts) + 1, 3):
            if arm == "G":
                res = run_g(G_RUN[(fault, block)] if attempt == 1 else G_RERUN[(fault, block)], fault)
            else:
                res = run_hw(arm, fault, block, tag="" if attempt == 1 else "r2")
            attempts.append(res)
            st["sessions"][key] = {"final": False, "attempts": attempts}
            save_state(st)
            hc.log(f"{key} 시도{attempt}: {res.get('classification')}")
            if res.get("classification") != "harness_error":
                break
        st["sessions"][key] = {"final": True, "attempts": attempts, "classification": attempts[-1].get("classification"), "finished": hc.now_iso()}
        save_state(st)
        if attempts[-1].get("classification") == "blocked_by_other_work":
            hc.log("다른 작업과 충돌 - 순서를 멈춘다(보고 필요)")
            return 3
        if any(x.get("restore_error") or x.get("revert_error") for x in attempts):
            hc.log("복원 실패 - 순서를 멈춘다(보고 필요)")
            return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
