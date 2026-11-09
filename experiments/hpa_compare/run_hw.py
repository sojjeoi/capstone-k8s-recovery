"""H/W 세션 1회 (2026-10-22). 동결 시험 함수(run_no_switch_control_trial)와 주입기·prober 를 그대로 불러 쓰고,
군 구성(HPA / 상시 2개)·대상 Pod 고정·시계열 기록·복원만 이 파일이 한다. 동결 소스는 수정하지 않는다.

사용: python experiments/hpa_compare/run_hw.py --arm H|W|C --fault f1|f2|f3 --block N [--tag NAME] [--check]
  f1(CPU 압박, 2026-11-04 통일 비교): StressChaos 1~4 workers × 90 s 를 시작 서비스 Pod 하나에 고정(run_cpu_demo_trial.DEMO_STAGES)
  --check : 기능 점검 세션(결과는 본 세션과 다른 폴더 `check/` 에 보관, 본 비교에 쓰지 않는다)
"""
import argparse
import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hc_common as hc  # noqa: E402


def parse_cpu(s):
    if s is None:
        return None
    s = str(s)
    if s.endswith("n"):
        return int(s[:-1]) / 1e9
    if s.endswith("u"):
        return int(s[:-1]) / 1e6
    if s.endswith("m"):
        return int(s[:-1]) / 1e3
    return float(s)


def parse_mem_mib(s):
    if s is None:
        return None
    s = str(s)
    if s.endswith("Ki"):
        return int(s[:-2]) / 1024
    if s.endswith("Mi"):
        return float(s[:-2])
    if s.endswith("Gi"):
        return float(s[:-2]) * 1024
    return None


def usage_stats(timeline, t0, t1) -> dict:
    """metrics-server 의 Pod 별 CPU(코어)·메모리(MiB) 사용량 평균·최대(구간 [t0, t1]). 예약량과 별개의 실제 사용량 기록."""
    acc = {}
    for rec in timeline:
        ts = hc._dt(rec["t"])
        if not (t0 <= ts <= t1):
            continue
        for pod, u in (rec.get("top") or {}).items():
            c, mib = parse_cpu(u.get("cpu")), parse_mem_mib(u.get("memory"))
            a = acc.setdefault(pod, {"cpu": [], "mem": []})
            if c is not None:
                a["cpu"].append(c)
            if mib is not None:
                a["mem"].append(mib)
    return {p: {"cpu_mean_cores": round(sum(v["cpu"]) / len(v["cpu"]), 3) if v["cpu"] else None,
                "cpu_max_cores": round(max(v["cpu"]), 3) if v["cpu"] else None,
                "mem_mean_mib": round(sum(v["mem"]) / len(v["mem"]), 1) if v["mem"] else None, "n": len(v["cpu"])}
            for p, v in acc.items()}


def events_from(timeline, t_inj) -> dict:
    """장애 주입 이후의 이벤트 시각: HPA 확장 결정, 추가 Pod 생성·Ready, 추가 Pod 의 첫 요청 증가."""
    ev = {"hpa_scale_decision": None, "extra_pod_created": None, "extra_pod_ready": None, "extra_pod_first_request": None,
          "extra_pod": None}
    base_pods = set()
    before = [r for r in timeline if hc._dt(r["t"]) <= t_inj and r.get("pods")]
    if before:
        base_pods = {p["name"] for p in before[-1]["pods"]}
    base_counter = {}
    for rec in timeline:
        ts = hc._dt(rec["t"])
        if ts < t_inj:
            continue
        ro = rec.get("rollout") or {}
        h = rec.get("hpa") or {}
        if ev["hpa_scale_decision"] is None and len(base_pods) == 1 and ((h.get("desired") or 0) >= 2 or (ro.get("replicas_spec") or 1) >= 2):
            ev["hpa_scale_decision"] = rec["t"]
        for p in rec.get("pods") or []:
            if p["name"] in base_pods or p.get("deleting"):
                continue
            if ev["extra_pod"] is None:
                ev["extra_pod"] = p["name"]
                ev["extra_pod_created"] = p["created"]
            if p["name"] == ev["extra_pod"] and p.get("ready") and ev["extra_pod_ready"] is None:
                ev["extra_pod_ready"] = p.get("ready_since") or rec["t"]
        if ev["extra_pod"] and rec.get("counters") and ev["extra_pod"] in rec["counters"]:
            v = rec["counters"][ev["extra_pod"]]
            if v is not None:
                base_counter.setdefault("v", v)
                if v > base_counter["v"] and ev["extra_pod_first_request"] is None:
                    ev["extra_pod_first_request"] = rec["t"]
    return ev


def prom_forward():
    """F1 CPU 주입기는 주입 전 CPU 기준선을 로컬 Prometheus(localhost:9090)에서 읽는다. G 실행기(guarded_pilot_session.ps1)처럼
    세션 동안만 port-forward 를 띄운다(상시로 열어 두면 G 실행기의 9090 port-forward 와 충돌한다). 이미 응답하면 띄우지 않는다."""
    import subprocess
    import urllib.request

    def healthy():
        try:
            return urllib.request.urlopen("http://localhost:9090/-/healthy", timeout=2).status == 200
        except Exception:  # noqa: BLE001
            return False
    if healthy():
        return None, {"started": False, "reason": "already_listening"}
    p = subprocess.Popen(["kubectl", "port-forward", "-n", "monitoring", "svc/kube-prom-kube-prometheus-prometheus", "9090:9090"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        if healthy():
            return p, {"started": True, "pid": p.pid}
        if p.poll() is not None:
            break
        time.sleep(1)
    p.terminate()
    raise RuntimeError("Prometheus port-forward(localhost:9090) 가 응답하지 않음")


def choose_target(arm, pods) -> dict:
    ready = [p for p in pods if p["ready"] and not p["deleting"]]
    if arm in ("H", "C"):
        if len(ready) != 1:
            raise RuntimeError(f"{arm} 시작 Pod 가 1개가 아님: {[p['name'] for p in ready]}")
        return ready[0]
    if len(ready) != 2:
        raise RuntimeError(f"W 시작 Pod 가 2개가 아님: {[p['name'] for p in ready]}")
    return sorted(ready, key=lambda p: p["name"])[0]          # 사전 규칙: 이름 사전순 앞의 Pod


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True, choices=["H", "W", "C"])      # C: 무전환 대조(HPA 없음, Pod 1개) - 요청 단위 보호 검증용(2026-10-23)
    ap.add_argument("--trace", action="store_true", help="요청 단위 계측 판 probe + Pod 로그·Service 변경 기록(결과는 trace-pair/ 에)")
    ap.add_argument("--fault", required=True, choices=["f1", "f2", "f3"])
    ap.add_argument("--block", type=int, required=True)
    ap.add_argument("--tag", default="")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--f3-config", default=str(hc.ROOT / "chaos" / "scenario-load-ramp.yaml"))
    ap.add_argument("--stand-sec", type=float, default=hc.STAND_SEC)
    ap.add_argument("--obs-sec", type=float, default=hc.OBS_SEC)
    a = ap.parse_args(argv)

    os.chdir(hc.ROOT)
    import load_ramp_adapter as lra
    import run_guarded_pilot as gp
    import run_if_threshold_candidate_smoke as smoke
    import run_once
    import run_timed_switch_recovery_trial as rt

    base = hc.RESULTS / ("trace-pair" if a.trace else "check" if a.check else "main")
    run_id = f"hc-{a.fault}-{a.arm.lower()}-b{a.block}{('-' + a.tag) if a.tag else ''}"
    rep = 7200 + (100 if a.check else 0) + a.block * 10 + {"H": 0, "W": 1, "C": 2}[a.arm] + {"f1": 7, "f2": 0, "f3": 5}[a.fault]
    out = base / run_id
    if out.exists():
        prev = out / "summary.json"
        if prev.exists() and json.loads(prev.read_text(encoding="utf-8")).get("classification") == "blocked_by_other_work":
            out.rename(out.with_name(out.name + "-blocked-" + hc.now_iso().replace(":", "").replace("+", "").replace(".", "")[:17]))   # 시작하지 못한 기록은 보존하고 같은 ID 로 다시 한다
        else:
            raise SystemExit(f"실행 ID 재사용 금지(원기록 보존): {out}")
    out.mkdir(parents=True)
    summary = {"run_id": run_id, "arm": a.arm, "fault": a.fault, "block": a.block, "rep": rep, "check": a.check,
               "started_at": hc.now_iso(), "problems": [], "classification": None, "obs_sec": a.obs_sec, "stand_sec": a.stand_sec}
    marker = out / "running.json"
    marker.write_text(json.dumps({"run_id": run_id, "started_at": summary["started_at"]}), encoding="utf-8")

    sampler = hc.Sampler(out / "timeline.jsonl")
    outcome, target, trial_error, tracers, pf = None, None, None, (), None
    try:
        if a.fault == "f1":
            pf, summary["prom_forward"] = prom_forward()
        hc.log(f"[{run_id}] 사전 점검")
        probs = hc.other_experiment_problems()
        if probs:
            summary["classification"] = "blocked_by_other_work"
            raise SystemExit(f"다른 작업과 충돌 - 시작 안 함: {probs}")
        ro = hc.rollout_state()
        if not hc.is_tolerant_profile(ro):
            raise RuntimeError(f"11초 프로필이 아님: readiness={ro['readiness_timeout']} liveness={ro['liveness_timeout']}")
        if not hc.settled(1):
            hc.log("시작 상태가 base(Pod 1개)가 아님 - 복원 시도")
            hc.restore_base()
        summary["delay_restored"] = hc.restore_delay_if_needed()
        summary["placement"] = hc.ensure_active_on("sj-worker")             # 모든 군 공통: 서비스 Pod 시작 노드 고정
        summary["state_before"] = hc.snapshot()
        sampler.start()

        hc.log(f"[{run_id}] 군 구성: {a.arm}")
        if a.arm == "H":
            summary["hpa_setup"] = hc.setup_hpa()
        elif a.arm == "W":
            summary["w_setup"] = hc.setup_w()
        if a.trace:
            import trace_adapter
            import trace_tools as tt
            trace_adapter.install(out / "trace")
            tracers = (tt.LogFollowers(out / "trace" / "vllm-logs", hc.vllm_pods).start(), tt.Watchers(out / "trace").start())
        summary["state_after_setup"] = hc.snapshot()
        time.sleep(a.stand_sec)                                       # 준비 Pod 가 Ready 인 채 안정(비용 구간 기준)
        target = choose_target(a.arm, hc.vllm_pods())
        summary["target"] = {"name": target["name"], "uid": target["uid"]}
        hc.log(f"대상 Pod: {target['name']}")

        # ---- 주입기·prober (동결 코드 그대로) ----
        def pinned_pods():
            return [{"name": p["name"], "uid": p["uid"]} for p in hc.vllm_pods() if p["name"] == target["name"] and not p["deleting"]]
        if a.fault == "f2":
            import network_degrade_adapter as nda
            injector = nda.make_network_degrade_injector(run_id, rt.ARM_LABEL, rep, get_active_pods_fn=pinned_pods)
        elif a.fault == "f1":
            import cpu_pressure_demo_adapter as cpa
            from run_cpu_demo_trial import DEMO_STAGES
            injector = cpa.make_cpu_pressure_injector(run_id, rt.ARM_LABEL, rep, stages=DEMO_STAGES, get_active_pods_fn=pinned_pods)
        else:
            injector = lra.make_load_ramp_injector(a.f3_config, run_id, rt.ARM_LABEL, rep)
        lifetime = gp.READY_TIMEOUT_SEC + a.obs_sec + smoke.BASELINE_TIMEOUT_SEC + smoke.PROBE_INJECTION_START_MARGIN_SEC
        prober = lra.make_load_ramp_prober(smoke.DEFAULT_PROBE_CONFIG, run_id, f"hpa_compare_{a.fault}", rt.ARM_LABEL, rep,
                                           lifetime, image=lra.IMAGE_FORCE_CLOSE)

        def safety(baseline_state, prober_):
            if a.fault in ("f1", "f2"):
                for c in hc.chaos_crs():
                    pods_sel = (c["pods"] or {}).get(hc.NS) or []
                    if pods_sel != [target["name"]]:
                        raise run_once.HarnessCorrupted(f"{c['kind']} 대상이 지정 Pod 하나가 아님: {c['name']} -> {pods_sel}")
                    # NetworkChaos 기록은 ns/pod, StressChaos 는 ns/pod/container 일 수 있다 - 앞 두 칸(Pod)만 비교
                    extra = [r for r in c["records"] if r and "/".join(r.split("/")[:2]) != f"{hc.NS}/{target['name']}"]
                    if extra:
                        raise run_once.HarnessCorrupted(f"NetworkChaos 적용 기록에 다른 Pod: {extra}")
            injector.is_done()                                      # 주입 스레드 예외를 여기서 드러낸다

        noop = smoke.DetectorHandle(is_alive=lambda: True, stop=lambda: {"returncode": 0, "exited_cleanly": True, "note": "H/W: 감지기 없음"},
                                    label="no-detector(H/W)")
        sink_port = smoke._pick_free_loopback_port()
        hc.log(f"[{run_id}] 시험 시작 (관찰 {a.obs_sec:.0f}s)")
        outcome = smoke.run_no_switch_control_trial(
            run_id, injector, prober, sink_port, out / "detector-evidence.jsonl", out / "sink-captured.jsonl",
            gp.PROBE_RESULTS_DIR / f"probe-{run_id}-{rt.ARM_LABEL}-{rep}-raw.csv",
            post_injection_observation_sec=a.obs_sec, check_safety_fn=safety, detector_start_fn=lambda rid, p, url: noop)
        summary["outcome"] = outcome
    except SystemExit as e:
        trial_error = f"SystemExit: {e}"
    except BaseException as e:  # noqa: BLE001
        trial_error = f"{type(e).__name__}: {e}"
        hc.log(f"오류: {trial_error}")
    finally:
        sampler.stop()
        for t in tracers:
            t.stop()
        if pf is not None:
            pf.terminate()
            try:
                pf.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pf.kill()
        restore_err = None
        if summary.get("classification") != "blocked_by_other_work":
            try:
                hc.log("복원")
                summary["state_after_restore"] = hc.restore_base()
            except BaseException as e:  # noqa: BLE001
                restore_err = f"{type(e).__name__}: {e}"
        summary["trial_error"] = trial_error
        summary["restore_error"] = restore_err
        summary["sampler_errors"] = sampler.errors
        summary["ended_at"] = hc.now_iso()

    # ---- 분석 ----
    try:
        if outcome and outcome.get("t_injection_observed_utc") and outcome.get("observation_ended_at_utc"):
            t_inj, t_end = outcome["t_injection_observed_utc"], outcome["observation_ended_at_utc"]
            tl = hc.read_timeline(out / "timeline.jsonl")
            ti, te = hc._dt(t_inj), hc._dt(t_end)
            csv = Path(gp.PROBE_RESULTS_DIR / f"probe-{run_id}-{rt.ARM_LABEL}-{rep}-raw.csv")
            if csv.exists():
                (out / csv.name).write_bytes(csv.read_bytes())
                summary["slo"] = hc.slo_cumulative(csv, t_inj, t_end)
            w0 = ti - timedelta(seconds=hc.STAND_SEC)
            pods = hc.pod_seconds(tl, w0, te)
            summary["cost"] = {"window": [w0.isoformat(), te.isoformat()], "pods": pods, **hc.cost_from(pods)}
            summary["usage"] = usage_stats(tl, w0, te)
            summary["request_increase"] = hc.counter_delta(tl, ti, te)
            summary["events"] = events_from(tl, ti)
            summary["events"]["t_injection"] = t_inj
            summary["events"]["observation_end"] = t_end
            if a.fault in ("f1", "f2"):
                summary["chaos_seen"] = sorted({(c["name"], json.dumps(c["pods"], sort_keys=True), tuple(c["records"]))
                                                for r in tl for c in (r.get("chaos") or [])}, key=str)
    except BaseException as e:  # noqa: BLE001
        summary["problems"].append(f"analysis_error: {type(e).__name__}: {e}")
    # ---- 분류: 하네스 오류와 성능 결과를 구분한다 ----
    if summary.get("classification") != "blocked_by_other_work":
        harness = []
        if summary.get("trial_error"):
            harness.append(summary["trial_error"])
        if summary.get("restore_error"):
            harness.append(f"restore_error: {summary['restore_error']}")
        o = summary.get("outcome") or {}
        if o.get("status") != "completed":
            harness.append(f"trial_status={o.get('status')}: {o.get('reason')}")
        if o.get("cleanup_status") not in (None, "complete"):
            harness.append(f"cleanup_status={o.get('cleanup_status')}")
        slo = summary.get("slo") or {}
        if not slo.get("computable"):
            harness.append(f"slo_not_computable: {slo.get('reason')}")
        summary["classification"] = "harness_error" if harness else "valid"
        summary["harness_problems"] = harness
    hc.jdump(out / "summary.json", summary)
    try:
        marker.unlink()
    except OSError:
        pass
    hc.log(f"[{run_id}] 완료: {summary['classification']}, SLO 누적={((summary.get('slo') or {}).get('cumulative_sec'))}")
    return 0 if summary["classification"] == "valid" else 2


if __name__ == "__main__":
    sys.exit(main())
