#!/usr/bin/env python3
"""ramp.py를 in-cluster에서 실행한다.

실측 확인: kubectl port-forward 터널은 갓 열렸을 때 동시 연결이 몰리면
(open-loop 특성상 응답을 안 기다리고 계속 새 요청을 쏨) 앞쪽 요청들이
줄줄이 타임아웃되는 걸로 확인됨(서버/vLLM 웜업 문제가 아니었음 - in-cluster
직접 호출로는 100% 성공). 그래서 실제로 신뢰할 측정이 필요할 땐(정상
데이터셋, calibration, Phase 8 실험 전부) 반드시 이 방식을 쓴다.

임시 pod를 하나 띄우고 ramp.py+config를 그 안에 복사해 실행한 뒤, 결과
CSV 두 개(summary/raw)를 꺼내온다. config의 target.url은 in-cluster 서비스
DNS(예: http://vllm-active.vllm-serving.svc.cluster.local:8000/...)여야
한다 - localhost:8000은 pod 안에서 안 먹힌다.
"""
import argparse
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional, Tuple

NAMESPACE = "vllm-serving"
IMAGE = "python:3.11-slim"
RAMP_PY = Path(__file__).parent.parent / "chaos" / "loadgen" / "ramp.py"


def _run(cmd, **kw):
    print("$", " ".join(cmd))
    return subprocess.run(cmd, check=True, **kw)


def _wait_ready(pod_name: str, timeout=60) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = subprocess.run(
            ["kubectl", "get", "pod", pod_name, "-n", NAMESPACE,
             "-o", "jsonpath={.status.containerStatuses[0].ready}"],
            capture_output=True, text=True,
        )
        if r.stdout.strip() == "true":
            return True
        time.sleep(2)
    return False


def run_in_cluster(config_path: str, run_id: Optional[str], method: str, rep: str, out_dir: Path) -> Tuple[Path, Path]:
    """실행마다 새 이름의 pod를 띄워 ramp.py를 실행하고 결과 CSV 2개를
    out_dir로 꺼내온다. (summary_path, raw_path) 반환. pod 이름을 매번
    고유하게 만들어서 이전 실행의 삭제(--wait=false는 비동기라 늦게 끝남)와
    겹쳐도 "already exists" 충돌이 안 나게 한다."""
    pod_name = f"ramp-runner-{uuid.uuid4().hex[:8]}"
    _run(["kubectl", "run", pod_name, "-n", NAMESPACE, f"--image={IMAGE}",
          "--restart=Never", "--", "sleep", "600"])
    try:
        if not _wait_ready(pod_name):
            raise RuntimeError(f"{pod_name}가 시간 내에 Ready되지 않음")

        # Windows 절대경로(D:\...)를 그대로 주면 kubectl cp가 "D:"를 pod 참조로
        # 오인해서 실패한다("one of src or dest must be a local file specification") -
        # 콜론이 없는 상대경로로 바꿔서 넘긴다.
        # 원본 파일명을 유지해야 ramp.py가 Path(config_path).stem으로 뽑는
        # scenario 이름이 "config"가 아니라 실제 시나리오 이름으로 찍힌다.
        config_name = Path(config_path).name
        _run(["kubectl", "cp", os.path.relpath(RAMP_PY), f"{NAMESPACE}/{pod_name}:/ramp.py"])
        _run(["kubectl", "cp", os.path.relpath(config_path), f"{NAMESPACE}/{pod_name}:/{config_name}"])

        sh_cmd = f"pip install -q aiohttp pyyaml && python /ramp.py --config /{config_name} --method " + method + " --rep " + str(rep)
        if run_id:
            sh_cmd += f" --run-id {run_id}"
        result = _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "sh", "-c", sh_cmd],
                       capture_output=True, text=True, encoding="utf-8")
        print(result.stdout)

        out_dir.mkdir(parents=True, exist_ok=True)
        summary_path = raw_path = None
        for line in result.stdout.splitlines():
            if ":" not in line:
                continue
            remote = line.split(":", 1)[1].strip()
            if not remote.startswith("/results/"):
                continue
            local = out_dir / Path(remote).name
            _run(["kubectl", "cp", f"{NAMESPACE}/{pod_name}:{remote}", os.path.relpath(local)])
            if remote.endswith("-raw.csv"):
                raw_path = local
            elif remote.endswith(".csv"):
                summary_path = local

        if summary_path is None or raw_path is None:
            raise RuntimeError("ramp.py 출력에서 결과 CSV 경로를 못 찾음:\n" + result.stdout)
        return summary_path, raw_path
    finally:
        subprocess.run(["kubectl", "delete", "pod", pod_name, "-n", NAMESPACE, "--wait=false"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ramp.py를 in-cluster pod에서 실행하고 결과를 꺼내온다")
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--method", default="manual")
    parser.add_argument("--rep", default="1")
    parser.add_argument("--out-dir", default=str(Path(__file__).parent.parent / "chaos" / "loadgen" / "results"))
    args = parser.parse_args()

    summary, raw = run_in_cluster(args.config, args.run_id, args.method, args.rep, Path(args.out_dir))
    print(f"summary: {summary}")
    print(f"raw: {raw}")
