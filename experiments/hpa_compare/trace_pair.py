"""요청 단위 보호 검증의 C(무전환) → G(기존 자동 전환) 한 쌍 실행기 (2026-10-23). 순서는 사전 등록 문서(docs/design/PER-REQUEST-PROTECTION-CHECK-DRAFT-20261023.md)대로 고정.
- C: run_hw.py --arm C --fault f2 --block 1 --trace (HPA 없음, Pod 1개, 예비 Pod 없음)
- G: orchestrate.run_g("n2s31", "f2") 를 환경변수 HC_TRACE_DIR 로 계측 판 probe 를 켠 채 실행(act 전환 → 세션 → record_only 원복 → 유효성 → 복원)
하네스 오류(classification == harness_error)만 한 번 다시 하고 원기록은 보존한다. 끝나면 시작 전 노드로 되돌리고 상태를 기록한다.
실행: python trace_pair.py [--only C|G]
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hc_common as hc  # noqa: E402

ROOT = hc.ROOT
OUT = hc.RESULTS / "trace-pair"
CONTRACT = "docs/design/PER-REQUEST-PROTECTION-CHECK-DRAFT-20261023.md"
QUOTE = ("user (chat 2026-10-23: '이번 지시는 이 범위의 로컬 구현·기능 점검·최소 실험 실행을 승인합니다. 다만 다른 세션이 클러스터를 사용 중이면 변경하지 말고 "
         "충돌 내용을 보고하세요. Calico 변경, 안전검사 완화, Git 커밋·push는 하지 마세요.' / '효과 비교가 필요하면 같은 조건의 무전환과 기존 자동 전환을 최소 한 쌍 실행하세요.')")


def run_c(tag=""):
    log = hc.RESULTS / "logs" / f"trace-C-f2-b1{('-' + tag) if tag else ''}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-u", str(HERE / "run_hw.py"), "--arm", "C", "--fault", "f2", "--block", "1", "--trace"] + (["--tag", tag] if tag else [])
    with open(log, "a", encoding="utf-8") as fh:
        rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=ROOT, env=dict(os.environ, PYTHONUTF8="1")).returncode
    run_id = f"hc-f2-c-b1{('-' + tag) if tag else ''}"
    sfile = OUT / run_id / "summary.json"
    summ = json.loads(sfile.read_text(encoding="utf-8")) if sfile.exists() else {}
    return {"arm": "C", "run_id": run_id, "rc": rc, "classification": summ.get("classification") or "no_summary", "summary": str(sfile),
            "restore_error": summ.get("restore_error")}


def run_g_traced(run):
    import orchestrate as orch
    import trace_tools as tt
    orch.APPROVAL_QUOTE = QUOTE
    gdir = OUT / f"g-{run}"
    gdir.mkdir(parents=True, exist_ok=True)
    os.environ["HC_TRACE_DIR"] = str(gdir)
    logs = tt.LogFollowers(gdir / "vllm-logs", hc.vllm_pods).start()
    watch = tt.Watchers(gdir).start()
    try:
        res = orch.run_g(run, "f2", contract_doc=CONTRACT)
    finally:
        time.sleep(35)                                   # 구 Pod 종료 직전·직후 로그까지 받는다
        logs.stop()
        watch.stop()
        os.environ.pop("HC_TRACE_DIR", None)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["C", "G"], default="")
    ap.add_argument("--c-tag", default="t2", help="C 실행 ID 접미사(이미 쓴 ID 는 재사용 금지)")
    ap.add_argument("--g-run", default="n2s32", help="G 실행 ID(g_stage2.RUNS_G 에 등록된 것)")
    a = ap.parse_args()
    os.chdir(ROOT)
    OUT.mkdir(parents=True, exist_ok=True)
    probs = hc.other_experiment_problems()
    if probs:
        raise SystemExit(f"다른 작업과 충돌 - 시작 안 함: {probs}")
    before = OUT / "pair-state-before.json"
    node0 = hc.active_node()
    if not before.exists():
        hc.jdump(before, {"at": hc.now_iso(), "node": node0, "snapshot": hc.snapshot()})
    else:
        node0 = json.loads(before.read_text(encoding="utf-8"))["node"]
    results = {}
    if a.only in ("", "C"):
        r = run_c(a.c_tag)
        if r["classification"] == "harness_error":
            r = {"retry_of": r, **run_c(a.c_tag + "r2")}
        results["C"] = r
        hc.log(f"C: {r['classification']}")
        if r.get("restore_error"):
            raise SystemExit(f"C 복원 오류 - 중단: {r['restore_error']}")
    if a.only in ("", "G"):
        r = run_g_traced(a.g_run)
        if r.get("classification") == "harness_error":
            hc.log("G 하네스 오류 - 원기록 보존, 이번 실행은 여기서 멈춘다(재시도는 보고 후 판단)")
        results["G"] = r
        hc.log(f"G: {r.get('classification')}")
    final = {}
    try:
        hc.wait_until(lambda: hc.settled(1), 900.0, 5.0, "단일 Pod Healthy")
        final["delay"] = hc.restore_delay_if_needed()
        if hc.active_node() != node0:
            final["moved"] = hc.ensure_active_on(node0)
        final["snapshot"] = hc.snapshot()
    except BaseException as e:  # noqa: BLE001
        final["error"] = f"{type(e).__name__}: {e}"
    name = f"pair-result-{hc.now_iso()[:19].replace(':', '')}.json"
    hc.jdump(OUT / name, {"results": results, "final_restore": final, "node_before": node0})
    print(json.dumps({"results": {k: v.get("classification") for k, v in results.items()}, "final_restore_error": final.get("error")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
