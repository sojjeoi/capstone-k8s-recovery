"""발표 12장 시연 캡처용 G 세션 1회 (2026-10-27, 사용자 채팅 승인). demo-only, counted=false - HPA 비교 13세션과 섞지 않는다.
동결·비교 파일(g_stage2.py, orchestrate.py 등)은 고치지 않고, 이 프로세스 안에서만 실행 ID n2s41(F2)을 허용하고
결과 폴더를 hpa_compare/demo-capture-20261027/<run> 으로 바꾼다. 순서는 orchestrate.run_g 와 같다.
사용: python experiments/hpa_compare/g_demo_capture.py --drive
     (guarded_pilot_session.ps1 이 RunnerScript 로 부를 때는 g_stage2.py 와 같은 인자)
"""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import g_stage2 as g  # noqa: E402

RUN = os.environ.get("G_DEMO_RUN", "n2s41")  # 래퍼가 RunnerScript 로 다시 부를 때도 환경변수로 같은 값이 전달된다
OUT = g.RES / ("demo-capture-20261027" if RUN == "n2s41" else "demo-capture-20261028")
g.RUNS_G[RUN] = ("f2", int(RUN[-2:]))
_orig_dir = g.results_dir_for
g.results_dir_for = lambda run: OUT / run if run == RUN else _orig_dir(run)
APPROVED_BY = {
    "n2s41": ("user (chat 2026-10-27: '12장 먼저해줘' / '엥 괜찮아 지금 충전도 하고 있고 안자서 해도됨') - "
              "발표 12장 채팅 캡처용 G 세션 n2s41 1회, demo-only, counted=false"),
    "n2s42": ("user (chat 2026-10-28: '같은 순간 캡처 실행 승인: CPU 압박 p2live02 1회 + 네트워크 지연 오토스케일링(H) 1회 + "
              "우리 시스템(G) 1회 ... demo-only, 안전검사 막히면 중단, 끝나면 원래 상태 복원') - "
              "발표 12장 같은 순간 채팅 캡처용 G 세션 n2s42 1회, demo-only, counted=false"),
    "n2s43": ("user (chat 2026-10-28: '네트워크 지연도 다시 실행해줄래? 전체 초에서도 우리 시스템이 더 적게 나온 화면을 쓰고 싶어') - "
              "발표 12장 채팅 캡처용 G 세션 n2s43 1회(전환 뒤 새 Pod 예열 후 질문), demo-only, counted=false"),
    "n2s44": ("user (chat 2026-10-28: '둘다 해보고 결과 말해줘 !' - 연속 질문 3개 + 가장 센 지연 단계 질문, H·G 각 1회) - "
              "발표 12장 채팅 시간 측정용 G 세션 n2s44 1회, demo-only, counted=false"),
    "n2s45": ("user (chat 2026-10-28: 'G 1회 재실행 승인: 네트워크 지연 주입, 정책 act 전환, 11초 프로필 적용·복원 포함. "
              "전환 뒤 예열하고 바로 질문 3개. demo-only, 끝나면 원래 상태 복원.') - 발표 12장 채팅 시간 측정용 G 세션 n2s45 1회, counted=false"),
    **{r: ("user (chat 2026-10-28: '한 3번 정도만 더 실험해주고 차이 가장 큰걸로 알려줘. 그걸 캡쳐해줘') - "
           f"발표 12장 채팅 화면 캡처용 G 세션 {r} (3회 중 1회), demo-only, counted=false") for r in ("n2s46", "n2s47", "n2s48")},
}[RUN]


def drive():
    import hc_common as hc
    import orchestrate as o
    import n2_stage2_ops as ops
    me = Path(__file__).resolve()
    _, s2, d, profile = o.g_context(RUN, "f2")
    run_id = s2.run_id_for(RUN)
    out_dir = OUT / "ops" / RUN
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {"kind": "G-demo", "run": RUN, "run_id": run_id, "steps": {}}
    try:
        probs = hc.other_experiment_problems()
        if probs:
            result["blocked"] = probs
            return result
        result["steps"]["delay_restored"] = hc.restore_delay_if_needed()
        result["steps"]["placement"] = hc.ensure_active_on("sj-worker")
        act_label = o.uniq(out_dir, f"act-switch-{RUN}")
        ops.switch_policy("act", label=act_label, out_dir=out_dir)
        clock = out_dir / f"clock-offset-{act_label}.json"
        ops.measure_clock(clock)
        impl = g.tolerant_verify()
        wrapper = o.ROOT / "experiments" / "guarded_pilot_session.ps1"
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        ap_path = out_dir / f"{RUN}-act-approval-{stamp}.json"
        now = ops.now_utc()
        inv = {"script": "experiments/guarded_pilot_session.ps1", "Run": RUN, "Mode": "act", "RunnerScript": str(me), "ActApproval": str(ap_path)}
        prov = ops.build_provenance(RUN, run_id, impl=impl, act_switch_file=out_dir / f"{act_label}.json", active_profile=profile,
                                    selection_evidence=g.SEL / "phase2-network-selection-evidence.json", wrapper_script=wrapper,
                                    runner_script=me, shim_script=g.HERE / "g_stage2.py", invocation=inv, generated_at=now)
        prov_path = ops.write_new(out_dir / f"execution-provenance-{RUN}-{stamp}.json", prov)
        approval = ops.build_approval(RUN, clock_file=clock, approved_by=APPROVED_BY, approved_at=now, impl=impl,
                                      contract_doc="docs/design/HPA-COMPARISON-DESIGN-DRAFT-20261022.md", provenance_path=prov_path,
                                      provenance_sha=ops.text_sha256(prov_path), scope=f"발표 캡처용 G 세션 {RUN} 1회 한정(demo-only)")
        ops.write_new(ap_path, approval)
        cmd = ["powershell", "-NoProfile", "-File", str(wrapper), "-Run", RUN, "-Mode", "act", "-ActApproval", str(ap_path),
               "-RunnerScript", str(me), "-SessionRoot", str(Path.home() / "n2-sessions"), "-PythonExe", sys.executable]
        log = OUT / f"G-{RUN}.log"
        with open(log, "a", encoding="utf-8") as fh:
            rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=o.ROOT,
                                env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")).returncode
        result["steps"]["wrapper_rc"], result["log"] = rc, str(log)
    except BaseException as e:  # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        try:
            ops.switch_policy("record_only", label=o.uniq(out_dir, f"revert-record-only-after-{RUN}"), out_dir=out_dir)
            result["steps"]["reverted"] = True
        except BaseException as e:  # noqa: BLE001
            result["steps"]["reverted"] = False
            result["revert_error"] = f"{type(e).__name__}: {e}"
    try:
        hc.wait_until(lambda: hc.settled(1), 900, 5.0, "G 종료 후 base 상태")
        result["steps"]["delay_restored_after"] = hc.restore_delay_if_needed()
        result["steps"]["base_restored"] = True
    except BaseException as e:  # noqa: BLE001
        result["steps"]["base_restored"] = False
        result["restore_error"] = f"{type(e).__name__}: {e}"
    sess = d / f"p2-s2-session-{RUN}.json"
    if sess.exists():
        rec = json.loads(sess.read_text(encoding="utf-8"))
        result["session"] = {"outcome_status": rec.get("outcome_status"), "result": rec.get("result"), "cleanup_errors": rec.get("cleanup_errors")}
    return result


if __name__ == "__main__":
    if sys.argv[1:] == ["--drive"]:
        os.chdir(g.ROOT)
        r = drive()
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / f"drive-result-{RUN}.json").write_text(json.dumps(r, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        print(json.dumps(r, ensure_ascii=False, indent=1, default=str))
        sys.exit(0 if not (r.get("error") or r.get("revert_error") or r.get("restore_error") or r.get("blocked")) else 1)
    sys.exit(g.main(sys.argv[1:]))
