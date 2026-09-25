#!/usr/bin/env python3
"""후속 비용 계측 (§137 이후 지시 §5). detector는 클러스터 밖 로컬
서브프로세스이므로(arm_controller.py, §137에서 코드로 확인) 클러스터
지표만으로는 그 비용을 잴 수 없다 - 로컬 PID를 직접 psutil로 표본화하고,
클러스터 pod(active/preview)는 별도로 kubectl top으로 표본화한다. 요청마다
무거운 관리 명령을 실행하지 않는다 - 독립된 고정 주기 폴링 스레드 하나뿐.

이미 설치된 psutil(신규 의존성 아님, `import psutil` 확인됨)만 쓴다.
"""
import json
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import psutil

DEFAULT_INTERVAL_SEC = 5.0


class LocalProcessSampler:
    """detector 로컬 서브프로세스(PID)의 CPU 누적 사용시간·메모리를 고정
    주기로 표본화한다. 자식 프로세스도 포함한다(자식 유무는 detector
    구현에 따라 다를 수 있어 명시적으로 범위를 남긴다).

    pid는 정수 또는 "PID를 반환하는 0-인자 콜러블"(예: Detector.get_pid)을
    받는다 - detector 서브프로세스는 run_once() 내부에서 start()가 호출된
    뒤에야 PID가 생기므로, 호출자가 run_once() 호출 *전에* 샘플러를 미리
    만들어 두고 콜러블로 넘기면 PID가 생기기 전까지는 "아직 시작 안 됨"으로
    조용히 건너뛰다가(수집 누락이 아님 - 관측 대상이 아직 없을 뿐) 생기는
    즉시 샘플링을 시작한다."""

    def __init__(self, pid, interval_sec: float = DEFAULT_INTERVAL_SEC, include_children: bool = True):
        self._pid_or_provider = pid
        self.interval_sec = interval_sec
        self.include_children = include_children
        self.samples = []
        self.missed = []
        self.not_yet_started_ticks = 0
        self._stop = threading.Event()
        self._thread = None
        self._started_at = None

    def _current_pid(self):
        return self._pid_or_provider() if callable(self._pid_or_provider) else self._pid_or_provider

    def _sample_once(self):
        pid = self._current_pid()
        if pid is None:
            return {"t": datetime.now(timezone.utc).isoformat(), "ok": None}  # 아직 프로세스 없음 - 누락 아님
        try:
            proc = psutil.Process(pid)
            procs = [proc] + (proc.children(recursive=True) if self.include_children else [])
            cpu_sec = 0.0
            rss = 0
            n_procs = 0
            for p in procs:
                try:
                    t = p.cpu_times()
                    cpu_sec += t.user + t.system
                    rss += p.memory_info().rss
                    n_procs += 1
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue  # 이 하위 프로세스만 누락 - 전체를 실패시키지 않음
            return {"t": datetime.now(timezone.utc).isoformat(), "cpu_cumulative_sec": round(cpu_sec, 3),
                    "rss_bytes": rss, "n_processes_included": n_procs, "ok": True}
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            return {"t": datetime.now(timezone.utc).isoformat(), "ok": False, "error": str(e)}

    def _run(self):
        self._started_at = datetime.now(timezone.utc).isoformat()
        while not self._stop.is_set():
            s = self._sample_once()
            if s["ok"] is None:
                self.not_yet_started_ticks += 1  # 프로세스가 아직 없음 - 누락(missed)과 구분
            elif s["ok"]:
                self.samples.append(s)
            else:
                self.missed.append(s)
            self._stop.wait(self.interval_sec)

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_sec + 2)

    def summary(self) -> dict:
        if not self.samples:
            return {"n_samples": 0, "n_missed": len(self.missed),
                    "not_yet_started_ticks": self.not_yet_started_ticks,
                    "note": "표본 없음 - 전부 미확인이거나 프로세스가 관측 기간 내내 시작되지 않음"}
        rss_vals = [s["rss_bytes"] for s in self.samples]
        cpu_vals = [s["cpu_cumulative_sec"] for s in self.samples]
        return {
            "last_known_pid": self._current_pid(), "started_at": self._started_at, "interval_sec": self.interval_sec,
            "n_samples": len(self.samples), "n_missed": len(self.missed),
            "not_yet_started_ticks": self.not_yet_started_ticks,
            "cpu_cumulative_sec_first": cpu_vals[0], "cpu_cumulative_sec_last": cpu_vals[-1],
            "cpu_used_sec_over_window": round(cpu_vals[-1] - cpu_vals[0], 3),
            "rss_bytes_mean": round(sum(rss_vals) / len(rss_vals)), "rss_bytes_max": max(rss_vals),
            "include_children": self.include_children,
        }


class ClusterPodSampler:
    """active/preview 등 지정 namespace의 pod CPU·메모리를 kubectl top으로
    고정 주기 표본화한다. 요청 단위가 아니라 독립 타이머로만 동작 - 요청마다
    kubectl을 부르지 않는다."""

    def __init__(self, namespace: str, interval_sec: float = DEFAULT_INTERVAL_SEC):
        self.namespace = namespace
        self.interval_sec = interval_sec
        self.samples = []
        self.missed = []
        self._stop = threading.Event()
        self._thread = None

    def _sample_once(self):
        t = datetime.now(timezone.utc).isoformat()
        try:
            out = subprocess.run(
                ["kubectl", "top", "pod", "-n", self.namespace, "--no-headers"],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode != 0:
                return {"t": t, "ok": False, "error": out.stderr.strip()[:300]}
            pods = []
            for line in out.stdout.strip().splitlines():
                parts = line.split()
                if len(parts) < 3:
                    continue
                name, cpu, mem = parts[0], parts[1], parts[2]
                pods.append({"pod": name, "cpu_millicores": int(cpu.rstrip("m")) if cpu.endswith("m") else None,
                             "memory_mib": int(mem.rstrip("Mi")) if mem.endswith("Mi") else None})
            return {"t": t, "ok": True, "pods": pods}
        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError) as e:
            return {"t": t, "ok": False, "error": str(e)}

    def _run(self):
        while not self._stop.is_set():
            s = self._sample_once()
            (self.samples if s["ok"] else self.missed).append(s)
            self._stop.wait(self.interval_sec)

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_sec + 2)

    def summary(self) -> dict:
        by_pod = {}
        for s in self.samples:
            for p in s["pods"]:
                by_pod.setdefault(p["pod"], {"cpu": [], "mem": []})
                if p["cpu_millicores"] is not None:
                    by_pod[p["pod"]]["cpu"].append(p["cpu_millicores"])
                if p["memory_mib"] is not None:
                    by_pod[p["pod"]]["mem"].append(p["memory_mib"])
        per_pod_summary = {}
        for pod, v in by_pod.items():
            per_pod_summary[pod] = {
                "n_samples": len(v["cpu"]),
                "cpu_millicores_mean": round(sum(v["cpu"]) / len(v["cpu"]), 1) if v["cpu"] else None,
                "cpu_millicores_max": max(v["cpu"]) if v["cpu"] else None,
                "memory_mib_mean": round(sum(v["mem"]) / len(v["mem"]), 1) if v["mem"] else None,
                "memory_mib_max": max(v["mem"]) if v["mem"] else None,
            }
        return {"namespace": self.namespace, "interval_sec": self.interval_sec,
                "n_ticks": len(self.samples), "n_missed": len(self.missed), "per_pod": per_pod_summary}


def dump(path: Path, local: Optional[LocalProcessSampler], cluster: Optional[ClusterPodSampler], phase_marks: dict):
    """phase_marks: {"prep_start":.., "injection":.., "run_end":.., "cleanup_end":..} 같은
    타임스탬프 - 샘플러 자체는 구간을 모르고, 사후에 이 타임스탬프로 구간을 나눠 볼 수 있게
    원본 샘플을 전부 함께 저장한다(사후 절단은 여기서 하지 않음 - 원자료 그대로 보존)."""
    out = {
        "phase_marks": phase_marks,
        "local_detector": {"summary": local.summary(), "samples": local.samples, "missed": local.missed} if local else None,
        "cluster_pods": {"summary": cluster.summary(), "samples": cluster.samples, "missed": cluster.missed} if cluster else None,
    }
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="오프라인 자체 점검용 - 실제 PID를 짧게 샘플링해 동작 확인")
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--seconds", type=float, default=6.0)
    p.add_argument("--interval", type=float, default=1.0)
    args = p.parse_args()
    s = LocalProcessSampler(args.pid, interval_sec=args.interval)
    s.start()
    time.sleep(args.seconds)
    s.stop()
    print(json.dumps(s.summary(), ensure_ascii=False, indent=2))
