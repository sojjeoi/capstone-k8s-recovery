#!/usr/bin/env python3
"""2026-10-02 - 실험 시작 전 노드 디스크 건강 사전검사(읽기 전용, fail-closed).

배경: 2026-10-01 sj-worker2의 /dev/vda에서 쓰기 I/O·EXT4 오류가 계속됐는데 노드는 Ready,
Pressure 없음이었다 - 기존 안전검사(Ready/Pressure/MemAvailable)로는 막을 수 없었고 rep10은
probe 컨테이너 파일 쓰기 실패로 무효가 됐다.

판정(클러스터의 모든 노드 각각):
- 노드에 SSH(BatchMode)로 `date`·`findmnt /`·`sudo -n dmesg --time-format iso`만 실행한다(읽기 전용).
- 노드 자신의 시계로 최근 LOOKBACK_SEC 창을 잡는다(로컬 PC와의 시계 차이 배제).
- 창 안에 디스크 오류 패턴이 한 줄이라도 있으면 차단.
- 루트 파일시스템이 rw가 아니면 차단.
- dmesg 링버퍼의 가장 오래된 줄이 창 시작보다 늦으면(오류 폭주 등으로 창 전체를 못 봄) 차단 - 못 본 구간은 깨끗하다고 가정하지 않는다.
- SSH 실패·sudo 실패·타임아웃·출력 파싱 실패·노드→SSH 호스트 매핑 없음·노드 목록 조회 실패는 전부 차단.
즉 "최근 LOOKBACK_SEC 동안 오류 0건을 직접 확인한 노드"만 통과한다."""
import re
import subprocess
from datetime import datetime, timedelta
from typing import Callable, Optional

LOOKBACK_SEC = 1800  # 최근 30분 무오류 - 2026-10-01 오류는 수 초 간격으로 계속돼 이 창이면 확실히 잡힌다
NODE_SSH_HOSTS = {"sj-control": "capstone-control", "sj-worker": "capstone-worker", "sj-worker2": "capstone-worker2"}
SSH_TIMEOUT_SEC = 40.0
DISK_ERROR_RE = re.compile(
    r"I/O error|EXT4-fs error|EXT4-fs warning.*I/O|Buffer I/O error|blk_update_request|"
    r"Remounting filesystem read-only|Aborting journal|journal commit I/O error", re.IGNORECASE)
REMOTE_CMD = ("date -u +%Y-%m-%dT%H:%M:%S+00:00; echo ---MOUNT; findmnt -no OPTIONS /; "
              "echo ---DMESG; sudo -n dmesg --time-format iso")


def fetch_node_kernel_log(ssh_host: str) -> dict:
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", ssh_host, REMOTE_CMD],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=SSH_TIMEOUT_SEC)
        return {"returncode": r.returncode, "stdout": r.stdout, "stderr": r.stderr}
    except Exception as e:  # noqa: BLE001 - 접근 실패도 차단 사유
        return {"returncode": None, "stdout": "", "stderr": f"{type(e).__name__}: {e}"}


def _parse_dmesg_ts(line: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(line.split(" ", 1)[0].replace(",", "."))
    except ValueError:
        return None


def evaluate_node_disk_health(node: str, fetched: dict, lookback_sec: float = LOOKBACK_SEC) -> dict:
    out = {"node": node, "ok": False, "reasons": [], "error_lines": [], "coverage_start": None, "node_now": None}
    if fetched.get("returncode") != 0:
        out["reasons"].append(f"log_access_failed(rc={fetched.get('returncode')}): {(fetched.get('stderr') or '')[:300]}")
        return out
    text = fetched.get("stdout") or ""
    if "---MOUNT" not in text or "---DMESG" not in text:
        out["reasons"].append("unexpected_output")
        return out
    head, rest = text.split("---MOUNT", 1)
    mount, dmesg = rest.split("---DMESG", 1)
    try:
        now = datetime.fromisoformat(head.strip().splitlines()[-1])
    except (ValueError, IndexError):
        out["reasons"].append("node_time_unparseable")
        return out
    out["node_now"] = now.isoformat()
    if "rw" not in mount.strip().split(","):
        out["reasons"].append(f"root_fs_not_rw({mount.strip()})")
    all_lines = [(_parse_dmesg_ts(ln), ln) for ln in dmesg.splitlines() if ln.strip()]
    stamped = [(t, ln) for t, ln in all_lines if t is not None]
    # 시각을 못 읽은 줄(연속 줄 등)에 디스크 오류가 있으면 언제인지 모르므로 최근 오류로 본다(fail-closed)
    unstamped_errors = [ln for t, ln in all_lines if t is None and DISK_ERROR_RE.search(ln)]
    if unstamped_errors:
        out["reasons"].append(f"disk_errors_with_unparseable_time={len(unstamped_errors)}")
        out["error_lines"] = unstamped_errors[-5:]
    if not stamped:
        out["reasons"].append("dmesg_empty_or_unparseable")
        return out
    window_start = now - timedelta(seconds=lookback_sec)
    out["coverage_start"] = stamped[0][0].isoformat()
    if stamped[0][0] > window_start:
        out["reasons"].append(f"dmesg_does_not_cover_lookback(earliest={stamped[0][0].isoformat()})")
    errors = [ln for t, ln in stamped if t >= window_start and DISK_ERROR_RE.search(ln)]
    if errors:
        out["reasons"].append(f"disk_errors_in_last_{int(lookback_sec)}s={len(errors)}")
        out["error_lines"] = (out["error_lines"] + errors)[-5:]
    out["ok"] = not out["reasons"]
    return out


def check_all_nodes(list_nodes_fn: Callable[[], list], fetch_fn: Callable[[str], dict] = fetch_node_kernel_log,
                    ssh_hosts: dict = NODE_SSH_HOSTS, lookback_sec: float = LOOKBACK_SEC) -> dict:
    """반환 {"ok": bool, "nodes": [...], "reasons": [...]}. 예외를 던지지 않는다 - 호출부가 ok=False면 시작 안 함."""
    try:
        nodes = list(list_nodes_fn())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "nodes": [], "reasons": [f"node_list_failed: {type(e).__name__}: {e}"]}
    if not nodes:
        return {"ok": False, "nodes": [], "reasons": ["no_nodes"]}
    results = []
    for node in nodes:
        host = ssh_hosts.get(node)
        if host is None:
            results.append({"node": node, "ok": False, "reasons": ["no_ssh_host_mapping"]})
            continue
        results.append(evaluate_node_disk_health(node, fetch_fn(host), lookback_sec))
    bad = [f"{r['node']}: {r['reasons']}" for r in results if not r["ok"]]
    return {"ok": not bad, "nodes": results, "reasons": bad}


def list_cluster_nodes() -> list:
    from kubernetes import client, config
    config.load_kube_config()
    return [n.metadata.name for n in client.CoreV1Api().list_node().items]
