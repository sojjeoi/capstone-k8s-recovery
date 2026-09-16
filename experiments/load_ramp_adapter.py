#!/usr/bin/env python3
"""load_ramp 시나리오의 Injector/Prober 어댑터(run_once.py 계약 구현, 4단계).

Injector = ramp.py 자체 - load_ramp은 클라이언트가 만드는 부하 스파이크가 곧
fault라서, pod_kill/network_degrade/memory_pressure와 달리 별도 Chaos Mesh
리소스가 없다(chaos/scenario-load-ramp.yaml엔 target/stages만 있고 CR 없음).

Prober = 별도 파드에서 loadgen-runner:local 이미지의 probe.py를 저율(기본
1RPS) open-loop로 돌려서 SLO를 판정한다 - ramp.py 자신을 판정에도 쓰면
arm마다 실제 주입 부하 자체가 달라 비교 표본이 arm 간에 어긋난다(계약서
§1 교정, 2차 리뷰). probe.py는 매 요청마다 즉시 append+flush하므로, 여기서는
그 raw CSV를 주기적으로 kubectl exec cat으로 읽어와 slo_judge.py(사후분석
함수)를 그때그때 누적된 데이터에 다시 돌려 판정한다.

두 파드 모두 loadgen-runner:local 이미지 사용(pinned aiohttp==3.14.3/
PyYAML==6.0.3, 2026-09-16 워커 노드에서 빌드) - run_ramp_in_cluster.py의
bare python:3.11-slim + 매 trial pip install 방식을 대체한다.

run_id는 이 모듈이 만들지 않는다 - 호출자(트라이얼 실행 스크립트)가 먼저
run_id를 정해서 make_load_ramp_injector()/make_load_ramp_prober()와
run_once(run_id=...)에 동일하게 넘겨야 ramp.py/probe.py의 raw 로그가
TrialResult와 같은 run_id로 join된다(run_once.py에 run_id 인자를 추가한
이유가 이것).
"""
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from run_once import Injector, Prober, TrialInvalid
import slo_judge

NAMESPACE = "vllm-serving"
IMAGE = "loadgen-runner:local"
SETTLE_SEC = 60  # 새로 뜬 pod의 네트워크 안정화 대기 - run_ramp_in_cluster.py 실측 근거와 동일
POD_READY_TIMEOUT_SEC = 60
RESULTS_DIR = Path(__file__).parent / "results"  # probe raw 캐시 저장용, .gitignore의 results/*.csv에 이미 포함됨
PROBE_REFETCH_INTERVAL_SEC = 5.0  # kubectl exec 왕복 비용 고려 - poll_interval_sec(보통 1s)마다 안 당김
PROBE_DURATION_MARGIN_SEC = 30  # trial timeout_sec 경계에서 probe가 먼저 죽어 오탐나는 걸 피하기 위한 여유
PROBE_FETCH_FAILURE_THRESHOLD = 6  # 연속 실패 허용 횟수(~30초) - 이 이상이면 조용히 stale 데이터로 계속 판정하지 않고 TrialInvalid


def _run(cmd, check=False):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=check)


def _wait_pod_ready(pod_name: str, timeout=POD_READY_TIMEOUT_SEC) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = _run(["kubectl", "get", "pod", pod_name, "-n", NAMESPACE,
                   "-o", "jsonpath={.status.containerStatuses[0].ready}"])
        if r.stdout.strip() == "true":
            return True
        time.sleep(2)
    return False


def _delete_pod(pod_name: str) -> None:
    # timeout=30s는 K8s 기본 terminationGracePeriodSeconds(30s)와 정확히
    # 같아서 경합으로 자주 타임아웃났다(실측: 실제로는 삭제 성공, 확인만
    # 30초 안에 못 끝남) - 유예시간보다 확실히 길게 잡는다.
    r = _run(["kubectl", "delete", "pod", pod_name, "-n", NAMESPACE, "--wait=true", "--timeout=60s"])
    if r.returncode != 0 and "NotFound" not in r.stderr:
        raise RuntimeError(f"{pod_name} 삭제 실패: {r.stderr}")


def make_load_ramp_injector(config_path: str, run_id: str, arm: str, rep: int) -> Injector:
    """arm은 ramp.py의 --method로 그대로 전달(3-way 비교 축 태깅용) - 정책
    판단에는 안 쓰인다. native/fixed_threshold/proposed 무엇이든 동일한 부하
    스파이크를 만든다는 게 이 시나리오 비교의 전제(계약서 §1)."""
    pod_name = f"ramp-inj-{uuid.uuid4().hex[:8]}"
    config_name = Path(config_path).name
    log, exitfile = "/ramp.log", "/ramp.exit"
    first_started_at = {"t": None}

    def prepare():
        _run(["kubectl", "run", pod_name, "-n", NAMESPACE, f"--image={IMAGE}",
              "--image-pull-policy=Never", "--restart=Never", "--", "sleep", "1200"], check=True)
        # --image-pull-policy=Never 필수: registry 안 쓰는 로컬 이미지라
        # 기본 정책(IfNotPresent)이어도 첫 스케줄에서 pull을 시도하면
        # ErrImageNeverPull류로 실패한다(smoke test로 실측 확인, 2026-09-16) -
        # sj-worker 노드에 docker save | ctr -n k8s.io images import로 이미
        # 올려둔 이미지를 그대로 쓰게 강제해야 한다.
        # sleep 1200: run_ramp_in_cluster.py의 sleep 600은 450초 램프+60초
        # 안정화+파일준비까지 합치면 빠듯하다는 지적 반영 - 여유를 크게 두고
        # 실제 종료는 cleanup()의 명시적 pod 삭제로 처리한다.
        if not _wait_pod_ready(pod_name):
            raise TrialInvalid(f"{pod_name} Ready 시간초과")
        _run(["kubectl", "cp", os.path.relpath(config_path), f"{NAMESPACE}/{pod_name}:/{config_name}"], check=True)
        time.sleep(SETTLE_SEC)

    def inject():
        # kubectl exec엔 -d/--detach가 없다(실측 확인, 2026-09-16 - help에
        # 아예 없음). 대신 pod 안에서 nohup+파일 리다이렉트로 백그라운드시키면
        # exec 세션이 끝나도 프로세스가 안 죽는다(smoke test로 생존 실측
        # 확인) - kubectl exec 자체는 sh -c 스크립트가 "cmd &" 이후 즉시
        # 끝나므로 블로킹 없이 빠르게 반환한다.
        inner = (f"PYTHONUNBUFFERED=1 python /ramp.py --config /{config_name} "
                 f"--run-id {run_id} --method {arm} --rep {rep} "
                 f"> {log} 2>&1; echo $? > {exitfile}")
        cmd = f"nohup sh -c '{inner}' < /dev/null > /ramp-wrapper.log 2>&1 &"
        _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "sh", "-c", cmd], check=True)

    def is_started():
        # PYTHONUNBUFFERED=1 덕분에 ramp.py의 stage 시작 print("=== ...")가
        # 리다이렉트된 파일에도 즉시 보인다(버퍼링 안 걸림) - 이 줄이 찍힌 직후
        # 같은 이벤트루프 tick에서 첫 요청이 발사된다. t_injection 정의(계약서,
        # 2026-09-16)가 "첫 ramp 요청이 실제 전송된 시각"이므로, 이 마커를
        # 최초로 확인한 시각을 기록해 get_actual_injection_time()으로 넘긴다 -
        # inject() 호출 시각(kubectl exec 왕복+프로세스 기동 전)보다 더 정확함.
        # ramp.py 자체가 요청 단위 정밀 타임스탬프를 실시간 노출하진 않으므로
        # (raw CSV는 종료 시점에만 쓰임) poll 주기(~1초) 만큼의 오차는 남는다 -
        # 이 상한은 run_once.py가 결과의 injection_observation_error_sec에 남긴다.
        r = _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "sh", "-c", f"grep -q === {log}"])
        started = r.returncode == 0
        if started and first_started_at["t"] is None:
            first_started_at["t"] = datetime.now(timezone.utc).isoformat()
        return started

    def get_actual_injection_time():
        return first_started_at["t"]

    def _exit_code_ready():
        return _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "test", "-f", exitfile]).returncode == 0

    def is_effective():
        # ponytail: "목표 발사량의 90% 이상"까지는 확인 못 함 - ramp.py가
        # 누적 진행 카운터를 실시간으로 노출해야 가능한데, 현재는 종료
        # 시점에만 CSV를 쓴다(전량 메모리 축적, 사후분석 전제). 지금은 시작
        # 마커가 찍힌 뒤 바로 죽지는 않았는지만 재확인한다. 업그레이드:
        # ramp.py에 진행률 파일 옵션을 추가한 뒤 여기서 그 값을 읽기.
        return is_started() and not _exit_code_ready()

    def is_done():
        if not _exit_code_ready():
            return False
        r = _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "cat", exitfile])
        code = int((r.stdout or "1").strip() or "1")
        if code != 0:
            tail = _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "tail", "-c", "2000", log])
            raise TrialInvalid(f"ramp.py 비정상 종료(exit={code}): {tail.stdout}")
        return True

    def cleanup():
        _delete_pod(pod_name)

    return Injector(prepare=prepare, inject=inject, is_started=is_started,
                     is_effective=is_effective, is_done=is_done, cleanup=cleanup,
                     get_actual_injection_time=get_actual_injection_time)


def make_load_ramp_prober(config_path: str, run_id: str, scenario: str, arm: str, rep: int,
                           duration_sec: float) -> Prober:
    """config_path는 injector(ramp.py)의 scenario yaml이 아니라 전용
    chaos/probe-config.yaml을 넘긴다(2026-09-16 변경) - probe 1RPS 단독으로도
    vLLM pod CPU가 계속 ~4코어에 고정되는 게 실측 확인돼(calibrate_probe_only.py),
    probe payload를 최소화(prompt 짧게, max_tokens=1)해 자체 처리용량 점유를
    줄였다. url/model은 scenario 설정들과 동일 서비스를 보되 payload만 다르다.
    duration_sec은 run_once()에 넘기는 timeout_sec과 같은 값을 받아야 한다 -
    probe가 trial의 관찰 예산보다 먼저 죽으면 run_once()가 정상 trial을
    '관찰 도중 비정상 종료'로 오판한다(여기서 여유분을 더해 호출자가 이 둘을
    못 맞추는 실수를 막는다)."""
    pod_name = f"ramp-probe-{uuid.uuid4().hex[:8]}"
    config_name = Path(config_path).name
    raw_remote = "/probe-raw.csv"
    local_raw = RESULTS_DIR / f"probe-{run_id}-{arm}-{rep}-raw.csv"
    probe_duration = duration_sec + PROBE_DURATION_MARGIN_SEC
    cache = {"points": [], "fetched_at": 0.0}
    warmup = {"started_at": None}

    def start():
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        _run(["kubectl", "run", pod_name, "-n", NAMESPACE, f"--image={IMAGE}",
              "--image-pull-policy=Never", "--restart=Never", "--", "sleep", str(int(probe_duration) + 300)],
             check=True)
        if not _wait_pod_ready(pod_name):
            raise TrialInvalid(f"{pod_name} Ready 시간초과")
        _run(["kubectl", "cp", os.path.relpath(config_path), f"{NAMESPACE}/{pod_name}:/{config_name}"], check=True)
        time.sleep(SETTLE_SEC)
        # inject()와 동일한 이유로 -d 대신 nohup 백그라운드 패턴 사용.
        inner = (f"PYTHONUNBUFFERED=1 python /probe.py --config /{config_name} "
                 f"--run-id {run_id} --scenario {scenario} --arm {arm} --rep {rep} "
                 f"--out {raw_remote} --duration-sec {probe_duration} "
                 f"> /probe.log 2>&1; echo $? > /probe.exit")
        cmd = f"nohup sh -c '{inner}' < /dev/null > /probe-wrapper.log 2>&1 &"
        _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "sh", "-c", cmd], check=True)
        warmup["started_at"] = time.monotonic()
        # 실측 버그(2026-09-16): probe가 막 시작한 시점엔 표본이 1~2개뿐이라
        # slo_judge의 availability 위반(즉시 판정, 지속시간 조건 없음)이
        # 요청 1건의 일시적 실패만으로도 바로 True가 됐다(주입 1.3초만에
        # t_slo 발생 - stage-1은 1RPS 60초 baseline으로 원래 100% 성공
        # 구간이라 명백한 오탐). slo_judge._window()가 WINDOW_SEC(60초) 표본을
        # 전제로 설계됐으므로, 그만큼 표본이 쌓이기 전엔 판정 자체를 안 믿는다.

    def is_alive():
        # probe.exit가 아직 없으면(=probe.py 프로세스가 아직 살아서 도는 중)
        # True. 측정 대상 서비스가 죽는 건 관찰 대상이지 probe 고장이 아니다 -
        # probe.py 자체는 요청 실패도 success=False로 기록만 하고 계속 돈다.
        r = _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "test", "!", "-f", "/probe.exit"])
        return r.returncode == 0

    def _refresh():
        # 실측 버그(2026-09-16): kubectl exec cat이 trial 도중 계속 실패해도
        # 이 함수가 조용히 마지막 성공 시점의 stale cache["points"]를 계속
        # 돌려줘서, run_once()가 트래픽 재개 없이 그 시점 데이터만으로
        # t_slo/t_recovery를 판정해버린 사례를 발견했다(is_alive()는 probe
        # 프로세스 생존만 보므로 이 문제를 못 잡음). 연속 실패가 threshold를
        # 넘으면 stale 데이터로 계속 판정하지 말고 TrialInvalid로 명확히
        # 실패시킨다.
        now = time.monotonic()
        if now - cache["fetched_at"] < PROBE_REFETCH_INTERVAL_SEC:
            return cache["points"]
        r = _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "cat", raw_remote])
        cache["fetched_at"] = now
        if r.returncode != 0 or not r.stdout.strip():
            cache["consecutive_failures"] = cache.get("consecutive_failures", 0) + 1
            if cache["consecutive_failures"] >= PROBE_FETCH_FAILURE_THRESHOLD:
                raise TrialInvalid(
                    f"probe raw CSV를 {pod_name}에서 {cache['consecutive_failures']}회 연속 못 읽음"
                    f"(kubectl exec 실패 또는 빈 응답) - stale 데이터로 계속 판정할 수 없음"
                )
            return cache["points"]
        cache["consecutive_failures"] = 0
        try:
            local_raw.write_text(r.stdout, encoding="utf-8")
            rows = slo_judge.load_raw(local_raw)
            cache["points"] = slo_judge.evaluate(rows) if rows else []
        except Exception:
            pass  # 아직 헤더뿐이거나 읽는 순간 걸린 미완성 마지막 줄 - 다음 refresh에서 재시도(fetch 자체는 성공이라 실패 카운트 안 함)
        return cache["points"]

    def _warmed_up():
        # WINDOW_SEC(60초)만큼 표본이 쌓이기 전엔 find_t_slo() 결과를 안 믿는다
        # - 위 start()의 실측 버그 설명 참고. check_recovered()는 t_slo가
        # 실제로 찍힌 뒤에만(run_once()가) 부르므로 여기 게이트 하나로 충분.
        return warmup["started_at"] is not None and time.monotonic() - warmup["started_at"] >= slo_judge.WINDOW_SEC

    def check_slo_violation():
        if not _warmed_up():
            return False
        return slo_judge.find_t_slo(_refresh()) is not None

    def check_recovered():
        points = _refresh()
        t_slo = slo_judge.find_t_slo(points)
        return t_slo is not None and slo_judge.find_t_recovery(points, t_slo) is not None

    def get_actual_slo_time():
        t = slo_judge.find_t_slo(cache["points"])
        return t.isoformat() if t else None

    def get_actual_recovery_time():
        points = cache["points"]
        t_slo = slo_judge.find_t_slo(points)
        t_rec = slo_judge.find_t_recovery(points, t_slo) if t_slo else None
        return t_rec.isoformat() if t_rec else None

    def stop():
        _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "sh", "-c", "pkill -f probe.py || true"])
        _delete_pod(pod_name)  # --wait=true라 반환 시점엔 pod와 그 안 프로세스가 전부 확실히 종료됨

    return Prober(start=start, is_alive=is_alive, check_slo_violation=check_slo_violation,
                  check_recovered=check_recovered, stop=stop,
                  get_actual_slo_time=get_actual_slo_time, get_actual_recovery_time=get_actual_recovery_time)
