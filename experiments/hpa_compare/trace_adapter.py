"""계측 probe 연결 (2026-10-23). load_ramp_adapter.make_load_ramp_prober 를 그대로 부르되(SLO 판정·CSV 읽기·생명주기는 원본 그대로),
프로브 파드에서 `python /probe.py` 를 실행하는 순간에만 probe_trace.py 를 /probe.py 자리에 올려 실행하고, 종료 직전에 evidence JSONL 을 회수한다.
동결 소스는 수정하지 않는다 - 이 프로세스에서 모듈 속성 하나만 바꾼다. install(dir) 을 부르지 않으면 아무 영향 없다.
"""
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROBE_TRACE = HERE / "probe_trace.py"


def install(evidence_dir):
    import load_ramp_adapter as lra
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    orig = lra.make_load_ramp_prober

    def traced(config_path, run_id, scenario, arm, rep, duration_sec, *a, **kw):
        prober = orig(config_path, run_id, scenario, arm, rep, duration_sec, *a, **kw)
        st = {"pod": None}
        start0, stop0 = prober.start, prober.stop

        def start():
            real = lra._run

            def hooked(cmd, *args, **kwargs):
                if isinstance(cmd, list) and cmd[:2] == ["kubectl", "exec"] and "python /probe.py" in str(cmd[-1]):
                    st["pod"] = cmd[4]
                    real(["kubectl", "cp", os.path.relpath(str(PROBE_TRACE)), f"{lra.NAMESPACE}/{st['pod']}:/probe.py"], check=True)
                return real(cmd, *args, **kwargs)
            lra._run = hooked
            try:
                return start0()
            finally:
                lra._run = real

        def stop():
            if st["pod"]:
                try:
                    r = lra._run(["kubectl", "exec", "-n", lra.NAMESPACE, st["pod"], "--", "cat", "/probe-evidence.jsonl"])
                    out = evidence_dir / f"probe-{run_id}-{arm}-{rep}-evidence.jsonl"
                    if r.returncode == 0:
                        out.write_text(r.stdout, encoding="utf-8")
                    else:
                        (evidence_dir / f"probe-{run_id}-{arm}-{rep}-evidence-ERROR.txt").write_text(str(r.stderr), encoding="utf-8")
                    c = lra._run(["kubectl", "exec", "-n", lra.NAMESPACE, st["pod"], "--", "cat", "/probe-raw.csv"])     # 교차 검증용 최종 CSV 사본
                    if c.returncode == 0:
                        (evidence_dir / f"probe-{run_id}-{arm}-{rep}-pod-raw.csv").write_text(c.stdout, encoding="utf-8")
                    (evidence_dir / f"probe-{run_id}-{arm}-{rep}-pod.txt").write_text(st["pod"], encoding="utf-8")
                except Exception as e:  # noqa: BLE001
                    (evidence_dir / f"probe-{run_id}-{arm}-{rep}-evidence-ERROR.txt").write_text(repr(e), encoding="utf-8")
            return stop0()

        prober.start, prober.stop = start, stop
        return prober
    lra.make_load_ramp_prober = traced
    return traced
