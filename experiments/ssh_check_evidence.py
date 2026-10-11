"""guarded 실행 경로 전용 - 노드 디스크 점검(SSH)의 근거 보존 계측(2026-10-06). 동결 node_disk_health_gate.py는 그대로.

기존 점검 호출을 계측한다(병렬 SSH 감시 없음, 같은 순차 호출 1회씩):
  check_all_nodes(list_nodes_fn, fetch_fn=collector.fetch)
  → 기존 판정 함수 evaluate_node_disk_health·40초 제한·30분 범위·디스크 오류 정규식·실패 시 중단 그대로.
원격 명령은 기존 REMOTE_CMD와 같은 순서·같은 stdout이다. 단계 표시는 stderr로만 낸다(기존 파서 입력 stdout과 분리):
  @@GUARDED_STAGE T0 <원격 epoch>          명령 시작
  @@GUARDED_STAGE T1 <원격 epoch>          date·findmnt 끝, dmesg 시작 직전
  @@GUARDED_STAGE T2 rc=<rc> <원격 epoch>  dmesg 끝 - 끝에 같은 rc로 exit(기존과 같은 종료 코드)
판정 입력(fetch 반환):
  - 시간 안에 종료: {"returncode": rc, "stdout": 원문, "stderr": 단계 표시 줄을 뺀 원문}
  - 시간 초과: {"returncode": None, "stdout": "", "stderr": "TimeoutExpired: ..."} - 부분 출력·완료 표시가 있어도
    기존과 같은 실패 형태로 넘긴다(부분 출력은 근거 파일에만).
  - 실행 오류: {"returncode": None, ...}  /  근거 저장 실패: {"returncode": None, "stderr": "evidence_write_failed: ..."}
근거(새 파일, 덮어쓰지 않음): <dir>/<세션>-<순번>-<노드>.stdout/.stderr(받은 바이트 그대로)·.json(기록), <dir>/ssh-check-index.jsonl.
관측 분류(원인 단정 없음 - 네트워크·dmesg 등으로 해석하지 않는다):
  exited_after_completion_marker / exited_without_completion_marker / exited_without_start_marker /
  exited_output_not_closed_within_limit(프로세스는 끝났으나 제한 안에 출력 스트림이 닫히지 않음 - 기존 run()도 시간 초과) /
  timeout_no_start_marker / timeout_after_start_no_completion_marker(명령 시작 후 완료 표시 없음) /
  timeout_after_completion_marker(완료 표시 후 종료 안 됨) / reader_error / launch_error / internal_error
"""
import json
import os
import subprocess
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import node_disk_health_gate as g

MARK = "@@GUARDED_STAGE"
_DMESG = "echo ---DMESG;"
if g.REMOTE_CMD.count(_DMESG) != 1:  # 기존 명령 구조가 바뀌면 계측 명령이 기존과 달라지므로 import에서 멈춘다
    raise ImportError("node_disk_health_gate.REMOTE_CMD 구조 변경 - ssh_check_evidence 갱신 필요")
INSTRUMENTED_CMD = (f'echo "{MARK} T0 $(date +%s.%N)" >&2; '
                    + g.REMOTE_CMD.replace(_DMESG, f'echo "{MARK} T1 $(date +%s.%N)" >&2; {_DMESG}')
                    + f'; rc=$?; echo "{MARK} T2 rc=$rc $(date +%s.%N)" >&2; exit $rc')
SSH_PREFIX = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]  # 기존 fetch_node_kernel_log와 같은 옵션
HOST_TO_NODE = {v: k for k, v in g.NODE_SSH_HOSTS.items()}
INDEX = "ssh-check-index.jsonl"


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _text(b: bytes) -> str:  # 기존 subprocess.run(text=True)와 같은 디코딩·줄바꿈 처리
    return b.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")


def classify(timed_out: bool, stages: dict) -> str:
    if timed_out:
        if "T2" in stages:
            return "timeout_after_completion_marker"
        return "timeout_after_start_no_completion_marker" if "T0" in stages else "timeout_no_start_marker"
    if "T2" in stages:
        return "exited_after_completion_marker"
    return "exited_without_completion_marker" if "T0" in stages else "exited_without_start_marker"


class SshCheckEvidence:
    def __init__(self, evidence_dir: Path, *, ssh_prefix: Optional[list] = None,
                 timeout_sec: float = g.SSH_TIMEOUT_SEC, monotonic: Callable = time.monotonic):
        self.dir = Path(evidence_dir)
        self.ssh_prefix = list(ssh_prefix or SSH_PREFIX)
        self.timeout = timeout_sec
        self.monotonic = monotonic
        self.session = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.seq = 0
        self.records, self.write_errors = [], []

    def disk_fn(self, list_nodes_fn: Callable[[], list]) -> Callable[[], dict]:
        """기존 점검 함수에 그대로 넣는 연결(ab.check_run_guard의 disk_fn, rt.run_preflight의 disk_check_fn)."""
        return lambda: g.check_all_nodes(list_nodes_fn, fetch_fn=self.fetch)

    def fetch(self, ssh_host: str) -> dict:
        """fetch_node_kernel_log와 같은 계약: 예외를 던지지 않고 {"returncode","stdout","stderr"}를 돌려준다."""
        self.seq += 1
        node = HOST_TO_NODE.get(ssh_host, ssh_host)
        rec = {"seq": self.seq, "node": node, "ssh_host": ssh_host, "started_utc": _utc(), "timeout_sec": self.timeout,
               "stages_remote": {}, "stages_local_rel_sec": {}, "first_stdout_rel_sec": None, "last_stdout_rel_sec": None}
        out, err = [], []
        try:
            fetched = self._run(rec, out, err)
        except Exception as e:  # noqa: BLE001 - 계측 내부 오류도 기존처럼 점검 실패로(통과시키지 않음)
            rec.update(observed="internal_error", error=f"{type(e).__name__}: {e}")
            fetched = {"returncode": None, "stdout": "", "stderr": f"{type(e).__name__}: {e}"}
        return self._save(rec, b"".join(out), b"".join(err), fetched)

    def _run(self, rec: dict, out: list, err: list) -> dict:
        cmd = self.ssh_prefix + [rec["ssh_host"], INSTRUMENTED_CMD]
        t0 = self.monotonic()
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        except Exception as e:  # noqa: BLE001 - 기존과 같은 실패 형태
            rec.update(observed="launch_error", timed_out=False, returncode=None, error=f"{type(e).__name__}: {e}",
                       ended_utc=_utc(), elapsed_sec=round(self.monotonic() - t0, 3), last_stage=None)
            return {"returncode": None, "stdout": "", "stderr": f"{type(e).__name__}: {e}"}

        reader_errors = []

        def guarded(fn):
            def run():
                try:
                    fn()
                except Exception as e:  # noqa: BLE001 - 읽기 실패는 출력 불완전 - 통과시키지 않는다
                    reader_errors.append(f"{type(e).__name__}: {e}")
            return run

        def read_out():
            for chunk in iter(lambda: p.stdout.read1(65536), b""):
                rel = round(self.monotonic() - t0, 3)
                out.append(chunk)
                if rec["first_stdout_rel_sec"] is None:
                    rec["first_stdout_rel_sec"] = rel
                rec["last_stdout_rel_sec"] = rel

        def read_err():
            for line in iter(p.stderr.readline, b""):
                err.append(line)
                s = line.decode("utf-8", "replace").strip()
                if s.startswith(MARK) and len(s.split()) >= 2:
                    stage = s.split()[1]
                    rec["stages_local_rel_sec"][stage] = round(self.monotonic() - t0, 3)
                    rec["stages_remote"][stage] = " ".join(s.split()[2:])
        readers = [threading.Thread(target=guarded(read_out), daemon=True),
                   threading.Thread(target=guarded(read_err), daemon=True)]
        rc, timed_out, open_after_exit = None, False, False
        try:
            for t in readers:
                t.start()
            try:  # 기존 subprocess.run(timeout=40)과 같이 '종료 + 출력 끝(EOF)'까지를 한 제한 안에서 본다
                rc = p.wait(timeout=max(0.0, self.timeout - (self.monotonic() - t0)))  # 실행 시작부터 남은 시간만
                for t in readers:
                    t.join(max(0.0, self.timeout - (self.monotonic() - t0)))
                open_after_exit = any(t.is_alive() for t in readers)
            except subprocess.TimeoutExpired:
                timed_out = True
        finally:
            if p.poll() is None:
                p.kill()  # 이 점검이 띄운 ssh 프로세스만 종료
                try:
                    rec["returncode_after_kill"] = p.wait(10)  # 판정용이 아니라 근거용 실제 종료 상태
                except subprocess.TimeoutExpired:
                    rec["kill_wait_timed_out"] = True
            for t in readers:
                t.join(5)
        stages = rec["stages_local_rel_sec"]
        observed = ("exited_output_not_closed_within_limit" if open_after_exit
                    else "reader_error" if reader_errors and not timed_out else classify(timed_out, stages))
        rec.update(timed_out=timed_out, returncode=rc, ended_utc=_utc(), elapsed_sec=round(self.monotonic() - t0, 3),
                   observed=observed, last_stage=list(stages)[-1] if stages else None,  # 도착 순서상 마지막
                   reader_errors=reader_errors, readers_alive_after_join=[t.is_alive() for t in readers])
        if timed_out or open_after_exit or reader_errors:  # 부분 출력·완료 표시가 있어도 통과시키지 않는다
            return {"returncode": None, "stdout": "",
                    "stderr": f"TimeoutExpired: ssh {rec['ssh_host']} timed out after {self.timeout} seconds "
                              f"(observed={observed}, last_stage={rec['last_stage']})"
                    if (timed_out or open_after_exit) else f"ssh_output_read_failed: {reader_errors}"}
        clean_err = b"".join(ln for ln in list(err) if not ln.lstrip().startswith(MARK.encode()))
        return {"returncode": rc, "stdout": _text(b"".join(out)), "stderr": _text(clean_err)}

    def _save(self, rec: dict, out: bytes, err: bytes, fetched: dict) -> dict:
        tag = f"{self.session}-{rec['seq']:04d}-{rec['node']}"
        rec.update(stdout_bytes=len(out), stderr_bytes=len(err), stdout_file=f"{tag}.stdout",
                   stderr_file=f"{tag}.stderr", judged_returncode=fetched["returncode"])
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            for name, data in ((f"{tag}.stdout", out), (f"{tag}.stderr", err),
                               (f"{tag}.json", json.dumps(rec, ensure_ascii=False, indent=1).encode("utf-8"))):
                with open(self.dir / name, "xb") as fh:  # 덮어쓰지 않음
                    fh.write(data)
            with open(self.dir / INDEX, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({k: rec.get(k) for k in (
                    "seq", "node", "started_utc", "ended_utc", "observed", "last_stage", "timed_out", "returncode",
                    "judged_returncode", "elapsed_sec", "stdout_bytes", "stderr_bytes", "stdout_file")},
                    ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as e:  # 필수 근거 저장 실패 - 점검 실패로 바꿔 기존 중단 경로를 탄다
            msg = f"evidence_write_failed: {type(e).__name__}: {e}"
            rec["evidence_write_error"] = msg
            self.write_errors.append({"seq": rec["seq"], "node": rec["node"], "error": msg})
            fetched = {"returncode": None, "stdout": "", "stderr": msg}
            rec["judged_returncode"] = None
        self.records.append(rec)
        return fetched

    def summary(self) -> dict:
        keys = ("seq", "node", "started_utc", "observed", "last_stage", "timed_out", "returncode", "elapsed_sec",
                "stdout_bytes", "stderr_bytes", "stdout_file", "stderr_file", "evidence_write_error")
        return {"dir": str(self.dir), "index": str(self.dir / INDEX), "session": self.session, "checks": len(self.records),
                "by_observed": dict(Counter(r.get("observed") for r in self.records)),
                "failed_checks": [{k: r.get(k) for k in keys} for r in self.records if r.get("judged_returncode") != 0],
                "write_errors": self.write_errors,
                "note": "관측 분류이며 원인(네트워크·dmesg 등)을 단정하지 않는다. 판정은 기존 evaluate_node_disk_health 그대로."}
