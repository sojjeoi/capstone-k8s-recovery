"""진행 중 요청 점검 (2026-10-23, 기능 검증 - 장애 주입 없음, SLO 0.648초 기준 미적용).
예비 Pod 를 준비하고, 긴 스트리밍 답변 요청 여러 개를 보낸 뒤 모두 생성 중일 때 기존 승격 경로(argo promote)로 전환한다.
요청마다 완료/중단/오류와 받은 청크 수·도착 시각을 기록하고, 요청의 처리 Pod(접속 로그 대조)와 Service 선택자·엔드포인트 변경 시각, 구 Pod 종료 시각을 함께 남긴다.
시작 전 상태 기록, 끝나면 단일 Pod·원래 노드·delay 30 으로 복원 후 확인. 다른 작업이 실행 중이면 시작하지 않는다.
실행: python trace_inflight.py [--plan "0:1536,2:1536,4:256,6:256"]
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import hc_common as hc  # noqa: E402
import trace_tools as tt  # noqa: E402

IMAGE = "loadgen-runner:phase8-v4-forceclose-20261002"
URL_PREVIEW = "http://vllm-preview.vllm-serving.svc.cluster.local:8000/v1/completions"
URL_ACTIVE = "http://vllm-active.vllm-serving.svc.cluster.local:8000/v1/completions"


def iso():
    return datetime.now(timezone.utc).isoformat()


def kx(pod, *cmd):
    return hc.kubectl("exec", "-n", hc.NS, pod, "--", *cmd, check=False, timeout=60)


def run_plan(pod, name, plan, url=None, background=False):
    inner = f"python /inflight_probe.py --config /probe-config.yaml --plan {plan} --out /{name}.jsonl" + (f" --url {url}" if url else "")
    if background:
        hc.kubectl("exec", "-n", hc.NS, pod, "--", "sh", "-c", f"nohup sh -c '{inner} > /{name}.log 2>&1; echo $? > /{name}.exit' < /dev/null > /dev/null 2>&1 &")
    else:
        hc.kubectl("exec", "-n", hc.NS, pod, "--", "sh", "-c", inner, timeout=120)


def fetch(pod, name):
    r = kx(pod, "cat", f"/{name}.jsonl")
    return [json.loads(x) for x in r.stdout.splitlines() if x.strip()] if r.returncode == 0 else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default="0:1536,2:1536,4:256,6:256")
    ap.add_argument("--min-chunks", type=int, default=20, help="승격 전에 모든 요청이 이만큼 이상 생성 중이어야 한다")
    ap.add_argument("--label", default="inflight", help="결과 폴더 접두사(구성 이름 등)")
    ap.add_argument("--probes", action="store_true", help="고정(새 연결)·keepalive probe 와 Pod 별 처리 중 요청 수 기록을 함께 돌린다(드레인 검증용)")
    ap.add_argument("--fault", choices=["none", "f2"], default="none", help="f2: 기존 네트워크 지연 주입기를 구 Pod 하나에 고정해 주입(승격은 주입 시작 +30초, 사전 등록)")
    ap.add_argument("--promote-after-injection-sec", type=float, default=30.0)
    a = ap.parse_args()
    os.chdir(hc.ROOT)
    probs = hc.other_experiment_problems()
    if probs:
        raise SystemExit(f"다른 작업과 충돌 - 시작 안 함: {probs}")
    ro0 = hc.rollout_state()
    if not (hc.settled(1) and ro0["scale_down_delay"] == 30):
        raise SystemExit(f"시작 상태가 단일 Pod/delay 30 이 아님 - 시작 안 함: {ro0['phase']} delay={ro0['scale_down_delay']}")
    node0 = hc.active_node()
    out = hc.RESULTS / "trace" / f"{a.label}-{hc.now_iso()[:19].replace(':', '')}"
    out.mkdir(parents=True)
    summ = {"started_at": iso(), "plan": a.plan, "state_before": hc.snapshot(), "node_before": node0, "events": {}}
    sampler = hc.Sampler(out / "timeline.jsonl")
    logs = tt.LogFollowers(out / "vllm-logs", hc.vllm_pods).start()
    watch = tt.Watchers(out).start()
    sampler.start()
    probe = f"inflight-probe-{int(time.time())}"
    err, injector = None, None
    try:
        import blue_green_prep as bgp
        prep = bgp.prepare_preview_with_rollback(hc.ROLLOUT, hc.NS)
        summ["prep"] = {k: prep.get(k) for k in ("ready", "t_prep_start", "t_preview_ready", "prep_duration_sec", "created_pod_hash", "rollback_attempted", "rollback_ok")}
        if not prep.get("ready"):
            raise RuntimeError(f"예비 Pod 준비 실패: {summ['prep']}")
        ro = hc.rollout_state()
        summ["hashes"] = {"old_active": ro["active"], "new_preview": ro["preview"]}
        hc.kubectl("run", probe, "-n", hc.NS, f"--image={IMAGE}", "--image-pull-policy=Never", "--restart=Never", "--", "sleep", "900")
        hc.kubectl("wait", "--for=condition=Ready", f"pod/{probe}", "-n", hc.NS, "--timeout=90s")
        for src, dst in ((HERE / "inflight_probe.py", "/inflight_probe.py"), (hc.ROOT / "chaos" / "probe-config.yaml", "/probe-config.yaml")):
            hc.kubectl("cp", os.path.relpath(str(src)), f"{hc.NS}/{probe}:{dst}")
        # 1) 두 Pod 구별 검증: 승격 전에 예비/활성 서비스로 각각 짧은 요청
        run_plan(probe, "pre-preview", "0:1,1:1,2:1", URL_PREVIEW)
        run_plan(probe, "pre-active", "0:1,1:1,2:1", URL_ACTIVE)
        summ["pre"] = {"preview": fetch(probe, "pre-preview"), "active": fetch(probe, "pre-active")}
        if a.probes:                                         # 고정/keepalive probe + Pod 별 처리 중 요청 수 기록(관측 전용)
            ips = {p["hash"]: hc.kubectl("get", "pod", p["name"], "-n", hc.NS, "-o", "jsonpath={.status.podIP}").stdout.strip()
                   for p in hc.vllm_pods() if p["hash"] in (ro["active"], ro["preview"])}
            summ["pod_ips"] = ips
            for src, dst in ((HERE / "probe_trace.py", "/probe_trace.py"), (HERE / "pod_poll.py", "/pod_poll.py")):
                hc.kubectl("cp", os.path.relpath(str(src)), f"{hc.NS}/{probe}:{dst}")
            bg = lambda cmd: hc.kubectl("exec", "-n", hc.NS, probe, "--", "sh", "-c", f"nohup sh -c '{cmd}' < /dev/null > /dev/null 2>&1 &")  # noqa: E731
            base = "python /probe_trace.py --config /probe-config.yaml --run-id drain --scenario drain --rep 1 --duration-sec 240"
            bg(f"{base} --arm fc --out /fc.csv --evidence-out /fc-evidence.jsonl")
            bg(f"{base} --arm ka --out /ka.csv --evidence-out /ka-evidence.jsonl --keepalive")
            bg(f"python /pod_poll.py --targets old={ips[ro['active']]}:8000,new={ips[ro['preview']]}:8000 --interval 2 --duration 240 --out /poll.jsonl")
            summ["events"]["probes_started_at"] = iso()
            time.sleep(8)                                    # 승격 전 몇 건이 쌓이게
        # 2) 긴 스트리밍 요청 시작 -> 모두 생성 중일 때 승격
        run_plan(probe, "main", a.plan, background=True)
        t_wait = time.monotonic()
        n_plan = len(a.plan.split(","))
        while True:
            prog = {}
            for e in fetch(probe, "main"):
                if e["event"] in ("progress", "end"):
                    prog[e["request_id"]] = max(prog.get(e["request_id"], 0), e.get("n_chunks", 0))
            if len(prog) >= n_plan and all(v >= a.min_chunks for v in prog.values()):
                break
            if time.monotonic() - t_wait > 120:
                raise RuntimeError(f"승격 전 조건(모든 요청 {a.min_chunks}청크 이상) 미충족: {prog}")
            time.sleep(1.0)
        summ["events"]["progress_before_promote"] = prog
        if a.fault == "f2":                                  # 구 Pod 하나에 고정한 기존 네트워크 지연 주입기, 주입 시작 +N초에 승격(사전 등록)
            import network_degrade_adapter as nda
            import run_timed_switch_recovery_trial as rt
            old = next(p for p in hc.vllm_pods() if p["hash"] == ro["active"])
            injector = nda.make_network_degrade_injector(f"drain-{a.label}", rt.ARM_LABEL, 9100,
                                                         get_active_pods_fn=lambda: [{"name": p["name"], "uid": p["uid"]} for p in hc.vllm_pods()
                                                                                     if p["name"] == old["name"] and not p["deleting"]])
            injector.prepare()
            injector.inject()
            hc.wait_until(injector.is_started, 90.0, 1.0, "NetworkChaos 적용 관측")
            t_inj = injector.get_actual_injection_time()
            summ["events"]["injection_started_at"] = t_inj
            time.sleep(max(0.0, a.promote_after_injection_sec - (datetime.now(timezone.utc) - datetime.fromisoformat(t_inj)).total_seconds()))
        summ["events"]["promote_call_at"] = iso()
        hc.promote_via_policy_pod()
        summ["events"]["promote_returned_at"] = iso()
        # 3) 요청이 모두 끝날 때까지
        t_end = time.monotonic()
        while time.monotonic() - t_end < 330:
            if any(e["event"] == "plan_end" for e in fetch(probe, "main")):
                break
            time.sleep(3.0)
        summ["main_events"] = fetch(probe, "main")
        if a.probes:                                         # 구 Pod 가 사라질 때까지(최대 200초) 기다린 뒤 probe 기록 회수
            t_gone = time.monotonic()
            while time.monotonic() - t_gone < 200 and any(p["hash"] == ro["active"] for p in hc.vllm_pods()):
                time.sleep(3.0)
            summ["events"]["old_pod_gone_observed_at"] = iso()
            for name in ("fc-evidence", "ka-evidence", "poll"):
                r = kx(probe, "cat", f"/{name}.jsonl")
                (out / f"{name}.jsonl").write_text(r.stdout if r.returncode == 0 else "", encoding="utf-8")
        summ["events"]["observed_end_at"] = iso()
    except BaseException as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
        print("오류:", err)
    finally:
        if injector is not None:                             # 주입은 어떤 경우에도 정리하고 확인한다
            try:
                injector.cleanup()
                summ["events"]["injector_cleanup"] = "ok"
            except BaseException as e:  # noqa: BLE001
                summ["events"]["injector_cleanup"] = f"{type(e).__name__}: {e}"
        sampler.stop()
        time.sleep(35)
        logs.stop()
        watch.stop()
        hc.kubectl("delete", "pod", probe, "-n", hc.NS, "--ignore-not-found=true", "--wait=true", check=False)
        summ["error"] = err
        restore = {}
        try:
            hc.wait_until(lambda: hc.settled(1), 900.0, 5.0, "승격 뒤 단일 Pod Healthy")
            restore["delay"] = hc.restore_delay_if_needed()
            if hc.active_node() != node0:
                restore["moved"] = hc.ensure_active_on(node0)
            restore["state_after"] = hc.snapshot()
        except BaseException as e:  # noqa: BLE001
            restore["error"] = f"{type(e).__name__}: {e}"
        summ["restore"], summ["ended_at"] = restore, iso()
        (out / "summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        print("out:", out, "| restore:", {k: (v if k == "error" else "ok") for k, v in restore.items()})


if __name__ == "__main__":
    main()
