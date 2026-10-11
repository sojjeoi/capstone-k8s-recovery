#!/usr/bin/env python3
"""load_ramp 시나리오의 Injector/Prober 어댑터(run_once.py 계약 구현, 4단계).

Injector = ramp.py 자체 - load_ramp은 클라이언트가 만드는 부하 스파이크가 곧
fault라서, pod_kill/network_degrade/memory_pressure와 달리 별도 Chaos Mesh
리소스가 없다(chaos/scenario-load-ramp.yaml엔 target/stages만 있고 CR 없음).

Prober = 별도 파드에서 IMAGE 상수가 가리키는 이미지의 probe.py를 저율(기본
1RPS) open-loop로 돌려서 SLO를 판정한다 - ramp.py 자신을 판정에도 쓰면
arm마다 실제 주입 부하 자체가 달라 비교 표본이 arm 간에 어긋난다(계약서
§1 교정, 2차 리뷰). probe.py는 매 요청마다 즉시 append+flush하므로, 여기서는
그 raw CSV를 주기적으로 kubectl exec cat으로 읽어와 slo_judge.py(사후분석
함수)를 그때그때 누적된 데이터에 다시 돌려 판정한다.

두 파드 모두 IMAGE 상수의 이미지 사용(pinned aiohttp==3.14.3/PyYAML==6.0.3,
워커 노드에서 빌드) - run_ramp_in_cluster.py의 bare python:3.11-slim + 매
trial pip install 방식을 대체한다. lab-cpu3-warm-v1 확정(2026-09-18) 이후
loadgen-runner:phase8-v3-boundaries로 고정(ramp.py stage 경계 기록 포함,
docs/design/phase8-blue-green-preflight-incident.md §23.6·§26 참고).

run_id는 이 모듈이 만들지 않는다 - 호출자(트라이얼 실행 스크립트)가 먼저
run_id를 정해서 make_load_ramp_injector()/make_load_ramp_prober()와
run_once(run_id=...)에 동일하게 넘겨야 ramp.py/probe.py의 raw 로그가
TrialResult와 같은 run_id로 join된다(run_once.py에 run_id 인자를 추가한
이유가 이것).
"""
import csv
import io
import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from run_once import Injector, Prober, TrialInvalid
import slo_judge

NAMESPACE = "vllm-serving"
IMAGE = "loadgen-runner:phase8-v3-boundaries"  # lab-cpu3-warm-v1 확정과 함께 검증된 태그로 고정(2026-09-18)
# 2026-10-02 - force_close=True probe(요청마다 새 연결) 이미지. 위 IMAGE(e58a37b2…, 2026-09-18 생성)의 /probe.py에는
# force_close가 없다(노드에서 직접 확인). 새 태그는 e58a37b2… 위에 현재 probe.py(sha256 6d610450…) 계층만 얹어 sj-worker에서
# 1회 빌드, 같은 tar(sha256 8e89f24c…)를 sj-worker·sj-worker2 containerd에 반입. 이미지 ID
# sha256:266c266476641d1d16dda4777905a2dbe1ee2155d45f0fa2e2ce9b881ae2048b. V1/V2 새 실행만 이 값을 명시적으로 쓴다
# (기본값 IMAGE는 과거 실행 재현을 위해 그대로 둔다).
IMAGE_FORCE_CLOSE = "loadgen-runner:phase8-v4-forceclose-20261002"
IMAGE_FORCE_CLOSE_ID = "sha256:266c266476641d1d16dda4777905a2dbe1ee2155d45f0fa2e2ce9b881ae2048b"
# ramp.py --summary-out(§23 stage 경계 수정) 포함 버전 - docker image ID(config digest):
# sha256:e58a37b2d1c5903d1ce50474fd00c7d3a39cb300549408c0e0c2305482db897a
# containerd k8s.io manifest digest: sha256:21d6b8ef8bcb1804a28359b2db7a64faae19853493bddb52202b72ac6e9b7aaf
# 이미지 내부 /ramp.py SHA-256(로컬 chaos/loadgen/ramp.py와 정확히 일치, smoke pod에서 확인):
#   aadab9fc7f2a5a51cfee4e666ba7872c8e8fa378389d47a0e68e501f39153a82
SETTLE_SEC = 60  # 새로 뜬 pod의 네트워크 안정화 대기 - run_ramp_in_cluster.py 실측 근거와 동일
POD_READY_TIMEOUT_SEC = 60
RESULTS_DIR = Path(__file__).parent / "results"  # probe raw 캐시 저장용, .gitignore의 results/*.csv에 이미 포함됨
PROBE_REFETCH_INTERVAL_SEC = 5.0  # kubectl exec 왕복 비용 고려 - poll_interval_sec(보통 1s)마다 안 당김
PROBE_DURATION_MARGIN_SEC = 30  # trial timeout_sec 경계에서 probe가 먼저 죽어 오탐나는 걸 피하기 위한 여유
PROBE_FETCH_FAILURE_THRESHOLD = 6  # 연속 실패 허용 횟수(~30초) - 이 이상이면 조용히 stale 데이터로 계속 판정하지 않고 TrialInvalid
# §173(2026-09-26) - load_ramp_followup_adapter.py의 §166 수정을 이 기본
# 어댑터(is_alive()가 여태 test ! -f의 returncode 하나로 "파일 있음"과
# "kubectl exec 자체 실패"를 못 구분하던 원래 결함을 그대로 갖고 있었음 -
# CPU 압박 데모 retry3의 "probe 비정상 종료" 유력 원인으로 지목됨, §172
# 문서 참고)에도 그대로 이식한다. 판정 방향(불명확하면 죽음 취급)은 안
# 바꾸고, 그 판정에 이르는 조회 방식만 §166과 동일하게 고친다.
PROBE_LIVENESS_MAX_ATTEMPTS = 3          # 최초 1회 + 재확인 최대 2회(무한 재시도 아님) - §166과 동일값
PROBE_LIVENESS_RETRY_INTERVAL_SEC = 2.0  # 재확인 사이 고정 대기 - §166과 동일값

# §190 후속(2026-09-29, 사용자 승인 - "이 목적에 필요한 공유 어댑터의 최소
# 변경은 승인") - 클러스터 조회가 무기한 대기하지 않도록 모든 kubectl 호출에
# 개별 시간 제한을 건다. 이 파일의 _run() 호출처를 전수 확인한 분류 근거:
#   조회 - _wait_pod_ready()의 `kubectl get pod`(상태 1줄 조회),
#     _probe_liveness_probe()/_refresh()/_exit_code_ready()/is_started()/
#     is_done()/_fetch_stage_summary()의 `kubectl exec ... cat/test/grep/tail`
#     (이미 떠 있는 pod 안 파일 하나 읽기 - 전부 왕복 수백ms~수초가 정상
#     범위다. kubectl exec cat이 이 세션 §189 조사에서 실제로 의심된 hang
#     지점이었다).
#   시작 - prepare()/start()의 `kubectl run`(오브젝트 생성 제출, 스케줄
#     대기 없이 즉시 반환) + `kubectl cp`(작은 YAML 설정 파일 1개 전송,
#     통상 수 KB) + `kubectl exec ... nohup ... &`(이 파일 자신의 기존
#     주석에 이미 명시: "sh -c 스크립트가 'cmd &' 이후 즉시 끝나므로
#     블로킹 없이 빠르게 반환").
#   정리 - _delete_pod()의 `kubectl delete --wait=true --timeout=60s`만
#     예외적으로 그 자체가 최대 60초까지 정상적으로 걸릴 수 있다(K8s
#     기본 terminationGracePeriodSeconds 유예 대기 - 이 함수 자신의 기존
#     주석에 이미 설명돼 있음).
# "정상적으로 오래 걸릴 수 있는" 명령은 delete 하나뿐이다 - 정지 대상
# pod 내부의 `sleep N`은 kubectl 프로세스가 기다리는 대상이 아니라 pod
# 스펙의 인자일 뿐이라 이 시간 제한과 무관하다. 그래서 delete에만
# KUBECTL_DELETE_TIMEOUT_SEC(kubectl 자체 --timeout=60s보다 넉넉하게 위에서
# 감쌈)을 따로 쓰고, 나머지 조회·시작 전부는 공통 KUBECTL_COMMAND_TIMEOUT_SEC
# (정상 왕복의 수배~수십 배 여유인 20초)를 기본값으로 쓴다. 공식
# 탐지기·모델·임계값·정책·주입 강도·실험 판정 기준(score_server.py·
# recovery-policy·DEMO_STAGES 등)은 이 파일이 애초에 참조하지 않으므로
# 이 변경과 무관하게 전부 그대로다.
KUBECTL_COMMAND_TIMEOUT_SEC = 20.0
KUBECTL_DELETE_TIMEOUT_SEC = 90.0  # kubectl 자체 --wait=true --timeout=60s보다 넉넉하게 위에서 감쌈


def _run(cmd, check=False, timeout=KUBECTL_COMMAND_TIMEOUT_SEC):
    """모든 kubectl 호출의 공유 경로. subprocess.run(timeout=...)은 시간
    초과 시 자식 프로세스를 kill()하고 wait()까지 마친 뒤 예외를 던진다
    (Python 공식 문서: "the child process will be killed and waited for") -
    호출 스레드만 포기하고 프로세스를 실행 상태로 남기지 않는다(지시:
    "단순히 호출 스레드만 포기하고 실행 중인 프로세스를 남기는 방식은
    쓰지 마" - 표준 라이브러리가 이미 이 요구를 충족하므로 별도
    감시 스레드·워커 프로세스가 필요 없다).

    그 예외를 여기서 잡아 "명령 실패"와 같은 모양(returncode!=0, 진단
    메시지가 담긴 stderr)의 결과로 바꿔 반환한다 - 이렇게 하면 이 함수를
    부르는 기존 코드 전부(이미 returncode!=0을 실패로 처리하도록 짜여
    있음)가 새 예외 처리 분기 없이도 타임아웃을 기존 실패 경로 그대로
    수용한다(예: _refresh()의 연속 실패 카운터가 타임아웃도 함께 세고,
    is_alive()의 재시도 로직이 타임아웃도 "exec_failed"로 같이 다루고,
    _delete_pod()의 RuntimeError 전파가 손대지 않아도 그대로 작동한다).
    check=True로 부른 호출(prepare()/inject()/start()의 kubectl run·cp·
    exec)만 예외로, 원래 check=True가 실패 시 CalledProcessError를
    던지던 것과 동일한 심각도를 유지하기 위해 타임아웃도
    CalledProcessError로 던진다 - 반환값을 안 보고 check=True에만 기대는
    호출부들이 타임아웃을 "성공"으로 착각하지 않도록."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               check=check, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        stderr = f"[timeout] {timeout:.0f}s 내 응답 없음(프로세스는 종료·회수됨): {' '.join(str(c) for c in cmd)}"
        if check:
            raise subprocess.CalledProcessError(124, cmd, output=e.stdout, stderr=stderr) from e
        return subprocess.CompletedProcess(args=cmd, returncode=124, stdout=e.stdout or "", stderr=stderr)


def _probe_liveness_probe(pod_name: str):
    """load_ramp_followup_adapter.py의 동명 함수와 완전히 동일 - kubectl exec
    자체 실패와 '/probe.exit 존재 확인'을 구분한다. sh 스크립트는 파일 유무와
    무관하게 항상 0으로 끝나므로, returncode!=0은 이제 'kubectl exec 자체가
    실패함'만을 뜻한다(이전엔 `test ! -f`의 returncode에 두 의미가 섞여
    있었음). 반환: ("alive", None) | ("exited", exit_code_문자열) | ("exec_failed", stderr)."""
    r = _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "sh", "-c",
              "if [ -f /probe.exit ]; then cat /probe.exit; else echo __ALIVE__; fi"])
    if r.returncode != 0:
        return "exec_failed", (r.stderr or "").strip()
    out = (r.stdout or "").strip()
    if out == "__ALIVE__":
        return "alive", None
    return "exited", out


def _classify_probe_fetch_failure(r) -> str:
    """2026-10-01(rep9 -c2 무효 후속) - `kubectl exec cat` 실패를 사후 구분하기
    위한 기록용 분류(판정 로직 불변 - 실패 횟수 집계·TrialInvalid 기준은 그대로).
    cat 자신의 '파일 없음'도 returncode!=0이라 stderr로만 가를 수 있다."""
    if r.returncode == 0:
        return "empty_response"  # exec 성공, 파일은 있으나 내용 없음
    if r.returncode == 124:
        return "kubectl_timeout"  # _run()이 timeout을 124로 변환
    if "No such file" in (r.stderr or ""):
        return "file_missing"  # exec 성공, probe.py가 CSV를 아직/끝내 안 만듦
    return "exec_failed"  # kubectl/apiserver/kubelet 경로 실패


def _write_json_best_effort(path: Path, record: dict) -> None:
    try:
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass  # 진단 기록 실패가 fail-closed 판정 자체를 바꾸지 않음


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
    # §190 후속 - kubectl 자신의 --timeout=60s(delete 요청 뒤 삭제 완료를
    # 기다리는 내부 상한)와 별개로, 이 subprocess 호출 자체가 시작조차
    # 못 하고 멈추는 경우(API 서버 연결 불가 등)를 막기 위해 그보다 넉넉한
    # 상한을 바깥에서 씌운다 - kubectl 자신의 60초 유예가 먼저 정상 작동할
    # 여유를 주면서도, 전체가 무기한 걸리진 않게 한다.
    r = _run(["kubectl", "delete", "pod", pod_name, "-n", NAMESPACE, "--wait=true", "--timeout=60s"],
             timeout=KUBECTL_DELETE_TIMEOUT_SEC)
    if r.returncode != 0 and "NotFound" not in r.stderr:
        raise RuntimeError(f"{pod_name} 삭제 실패: {r.stderr}")


def _classify_timestamp_against_stages(timestamp_iso, stages):
    """실제 stage_start_utc/stage_end_utc 경계로 timestamp_iso를 분류하는
    순수 함수 - kubectl 의존 없어 오프라인 테스트 가능(2026-09-18, stage
    관측성 보완 - §30에서 stage 경계가 명목값(주입시각+90초 단위)으로만
    보고돼 실제 드리프트를 반영 못 했던 문제 수정).

    stages는 ramp.py --summary-out CSV(experiments/results/ramp-summary-
    *.csv)를 csv.DictReader로 읽은 행 리스트 - 문자열 값 그대로 넘겨받아
    여기서 파싱한다. stages가 None/빈 리스트이거나 한 행이라도 stage/
    stage_start_utc/stage_end_utc 파싱에 실패하면 "unknown"을 반환한다
    (임의 추정 안 함 - summary fetch 자체가 실패했거나 손상된 경우).

    stage 구간에 안 속하면 세 가지로 구분한다: 첫 stage 시작 전은
    "baseline", stage 사이(ramp.py가 각 stage 끝에서 최대 10초 straggler
    대기 - §23에서 확인된 드리프트)는 "inter_stage_tail", 마지막 stage
    종료 후는 "drain"."""
    if not stages:
        return "unknown"
    t = datetime.fromisoformat(timestamp_iso)
    parsed = []
    for s in stages:
        try:
            parsed.append({
                "stage": s["stage"],
                "start": datetime.fromisoformat(s["stage_start_utc"]),
                "end": datetime.fromisoformat(s["stage_end_utc"]),
            })
        except (KeyError, ValueError):
            return "unknown"
    parsed.sort(key=lambda p: p["start"])
    if t < parsed[0]["start"]:
        return "baseline"
    for p in parsed:
        if p["start"] <= t <= p["end"]:
            return p["stage"]
    if t > parsed[-1]["end"]:
        return "drain"
    return "inter_stage_tail"


def make_load_ramp_injector(config_path: str, run_id: str, arm: str, rep: int) -> Injector:
    """arm은 ramp.py의 --method로 그대로 전달(3-way 비교 축 태깅용) - 정책
    판단에는 안 쓰인다. native/fixed_threshold/proposed 무엇이든 동일한 부하
    스파이크를 만든다는 게 이 시나리오 비교의 전제(계약서 §1)."""
    pod_name = f"ramp-inj-{uuid.uuid4().hex[:8]}"
    config_name = Path(config_path).name
    log, exitfile = "/ramp.log", "/ramp.exit"
    summary_remote = f"/ramp-summary-{run_id}.csv"  # pod 자체가 trial마다 새로 뜨므로 굳이 필요하진 않지만, run_id를 넣어 확실히 구분(2026-09-18 추가)
    local_summary_path = RESULTS_DIR / f"ramp-summary-{run_id}-{arm}-{rep}.csv"  # probe raw CSV와 동일한 명명 관례
    first_started_at = {"t": None}  # 마커를 처음 관측한 시각(datetime)
    last_not_started_at = {"t": None}  # 마커가 아직 없음을 마지막으로 관측한 시각(datetime)
    stage_cache = {"fetched": False, "stages": None}  # stages=None: 미시도 또는 fetch/파싱 실패(둘 다 "unknown"으로 처리 - 2026-09-18 추가)

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
                 f"--summary-out {summary_remote} "
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
        # (raw CSV는 종료 시점에만 쓰임) 오차가 남는다 - 이 kubectl exec 자체도
        # 수백ms 걸릴 수 있어 poll_interval_sec 같은 설정값만으로는 상한을
        # 보장 못 한다(2026-09-16 정정). 그래서 "마커 없음을 마지막으로 본
        # 시각"과 "마커를 처음 본 시각"의 실측 차이를 get_injection_observation_
        # error_sec()으로 넘긴다.
        r = _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "sh", "-c", f"grep -q === {log}"])
        started = r.returncode == 0
        now = datetime.now(timezone.utc)
        if started:
            if first_started_at["t"] is None:
                first_started_at["t"] = now
        else:
            last_not_started_at["t"] = now
        return started

    def get_actual_injection_time():
        t = first_started_at["t"]
        return t.isoformat() if t is not None else None

    def get_injection_observation_error_sec():
        started_at, not_started_at = first_started_at["t"], last_not_started_at["t"]
        if started_at is None or not_started_at is None:
            return None
        return (started_at - not_started_at).total_seconds()

    def get_last_seen_present_time():
        # run_once.py가 t_injection_last_seen 필드를 채우고, 이 어댑터가
        # get_injection_observation_error_sec()을 안 줄 때의 대체 계산
        # 기준점으로도 쓴다(2026-09-18 추가). 없으면(첫 poll에서 이미
        # 마커가 보임) None - request~observed 구간이 대신 쓰인다.
        t = last_not_started_at["t"]
        return t.isoformat() if t is not None else None

    def _exit_code_ready():
        return _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "test", "-f", exitfile]).returncode == 0

    def is_effective():
        # ponytail: "목표 발사량의 90% 이상"까지는 확인 못 함 - ramp.py가
        # 누적 진행 카운터를 실시간으로 노출해야 가능한데, 현재는 종료
        # 시점에만 CSV를 쓴다(전량 메모리 축적, 사후분석 전제). 지금은 시작
        # 마커가 찍힌 뒤 바로 죽지는 않았는지만 재확인한다. 업그레이드:
        # ramp.py에 진행률 파일 옵션을 추가한 뒤 여기서 그 값을 읽기.
        return is_started() and not _exit_code_ready()

    def _fetch_stage_summary():
        # ramp.py가 정상 종료(exit=0)한 직후 딱 한 번만 시도한다(2026-09-18
        # 추가, stage 관측성 보완) - pod가 곧 cleanup()으로 삭제될 수 있어
        # 재시도는 의미 없고, is_done()이 OBSERVING 루프에서 반복 호출되므로
        # 매번 kubectl exec를 새로 하면 낭비다. 실패해도(kubectl 오류, 빈
        # 응답, 파싱 오류) 예외를 던지지 않는다 - stage 분석은 SLO 판정
        # 결과(outcome/t_slo/t_recovery)에 영향을 주면 안 된다(계약서
        # §5.1 정책). stages는 None으로 남고, classify_stage()가
        # "unknown"으로 처리한다.
        if stage_cache["fetched"]:
            return
        stage_cache["fetched"] = True
        r = _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "cat", summary_remote])
        if r.returncode != 0 or not r.stdout.strip():
            return
        try:
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            local_summary_path.write_text(r.stdout, encoding="utf-8")
            stage_cache["stages"] = list(csv.DictReader(io.StringIO(r.stdout)))
        except Exception:
            stage_cache["stages"] = None

    def is_done():
        if not _exit_code_ready():
            return False
        r = _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "cat", exitfile])
        code = int((r.stdout or "1").strip() or "1")
        if code != 0:
            tail = _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "tail", "-c", "2000", log])
            raise TrialInvalid(f"ramp.py 비정상 종료(exit={code}): {tail.stdout}")
        _fetch_stage_summary()
        return True

    def classify_stage(timestamp_iso):
        _fetch_stage_summary()  # is_done()에서 이미 시도됐어야 하지만 방어적으로 재시도(fetched 플래그로 1회 제한이라 무해)
        return _classify_timestamp_against_stages(timestamp_iso, stage_cache["stages"])

    def cleanup():
        _delete_pod(pod_name)

    return Injector(prepare=prepare, inject=inject, is_started=is_started,
                     is_effective=is_effective, is_done=is_done, cleanup=cleanup,
                     get_actual_injection_time=get_actual_injection_time,
                     get_injection_observation_error_sec=get_injection_observation_error_sec,
                     get_last_seen_present_time=get_last_seen_present_time,
                     classify_stage=classify_stage)


def _is_post_injection_window_evaluable(rows, injection_time, window_sec=None, min_samples=None):
    """주입 이후 유효한 관측 창이 확보됐는지 순수 판정 - kubectl 의존이 없어
    test_load_ramp_adapter.py에서 실클러스터 없이 검증 가능하다(2026-09-17
    정정). 이전 버전은 전체 누적 표본 수(sample_count)만 봐서, probe가 주입
    "전"에 이미 20개 이상을 모았거나 주입 "후" 표본 갱신이 멈춰도 잘못
    evaluable=True가 나올 수 있었다 - 반드시 주입 시각 이후 표본만, 그리고
    최신 표본이 주입 후 한 창(window_sec) 분량 이상 지난 시점의 것이어야
    한다."""
    window_sec = slo_judge.WINDOW_SEC if window_sec is None else window_sec
    min_samples = slo_judge.MIN_SAMPLES_FOR_RELIABLE_P95 if min_samples is None else min_samples
    if injection_time is None or not rows:
        return False
    latest = rows[-1]["sent_at"]  # load_raw()가 sent_at 기준 정렬해서 반환
    if (latest - injection_time).total_seconds() < window_sec:
        return False  # 최신 표본이 주입 후 한 창 분량만큼 아직 안 지남
    post_injection_count = sum(1 for r in rows if r["sent_at"] >= injection_time)
    return post_injection_count >= min_samples


def make_load_ramp_prober(config_path: str, run_id: str, scenario: str, arm: str, rep: int,
                           duration_sec: float, settle_sec: float = SETTLE_SEC, image: str = IMAGE) -> Prober:
    """config_path는 injector(ramp.py)의 scenario yaml이 아니라 전용
    chaos/probe-config.yaml을 넘긴다(2026-09-16 변경) - probe 1RPS 단독으로도
    vLLM pod CPU가 계속 ~4코어에 고정되는 게 실측 확인돼(calibrate_probe_only.py),
    probe payload를 최소화(prompt 짧게, max_tokens=1)해 자체 처리용량 점유를
    줄였다. url/model은 scenario 설정들과 동일 서비스를 보되 payload만 다르다.
    duration_sec은 run_once()에 넘기는 timeout_sec과 같은 값을 받아야 한다 -
    probe가 trial의 관찰 예산보다 먼저 죽으면 run_once()가 정상 trial을
    '관찰 도중 비정상 종료'로 오판한다(여기서 여유분을 더해 호출자가 이 둘을
    못 맞추는 실수를 막는다).

    settle_sec(2026-10-01 추가, 기본값은 그대로 SETTLE_SEC=60이라 기존 호출
    전부 미변경) - 호출부가 이 실행 한 번에만 더 긴 네트워크 안정화 대기를
    쓰고 싶을 때만 넘긴다. probe.py 자신의 --duration-sec 예산은 probe.py가
    "실제로 시작하는 시점"부터 세므로(이 sleep 이후), settle_sec을 늘려도
    관찰 종료 시점의 probe 전송 수명 여유(§194, 342초)는 줄어들지 않는다 -
    바깥 sleep(pod)의 300초 여유만 그만큼 줄어들 뿐이다(재계산 확인,
    test_load_ramp_adapter.py)."""
    pod_name = f"ramp-probe-{uuid.uuid4().hex[:8]}"
    config_name = Path(config_path).name
    raw_remote = "/probe-raw.csv"
    local_raw = RESULTS_DIR / f"probe-{run_id}-{arm}-{rep}-raw.csv"
    local_liveness_diagnostic = RESULTS_DIR / f"probe-{run_id}-{arm}-{rep}-liveness-diagnostic.json"
    local_fetch_diagnostic = RESULTS_DIR / f"probe-{run_id}-{arm}-{rep}-fetch-diagnostic.json"
    fetch_diag = {"failures": []}  # 실패한 cat 호출만 기록(성공 경로는 아무것도 안 남김)
    probe_duration = duration_sec + PROBE_DURATION_MARGIN_SEC
    cache = {"points": [], "rows": [], "fetched_at": 0.0}
    warmup = {"started_at": None}
    injection_ref = {"t": None}  # notify_injected()로 run_once()가 채워줌

    def start():
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        _run(["kubectl", "run", pod_name, "-n", NAMESPACE, f"--image={image}",
              "--image-pull-policy=Never", "--restart=Never", "--", "sleep", str(int(probe_duration) + 300)],
             check=True)
        if not _wait_pod_ready(pod_name):
            raise TrialInvalid(f"{pod_name} Ready 시간초과")
        _run(["kubectl", "cp", os.path.relpath(config_path), f"{NAMESPACE}/{pod_name}:/{config_name}"], check=True)
        time.sleep(settle_sec)
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
        """§173 - load_ramp_followup_adapter.py의 §166 수정을 그대로 이식.
        한 번의 조회 실패를 곧바로 '죽음'으로 판정하지 않는다(일시적 kubectl
        exec 실패일 수 있음). 그렇다고 상태를 모를 때 '살아있음'으로 간주하지도
        않는다 - 최대 PROBE_LIVENESS_MAX_ATTEMPTS회까지만 고정 간격으로
        재확인하고, 그래도 확정 못 하면 기존과 동일하게 fail-closed(죽음
        취급)하되 원인(exec_failed_exhausted)을 '확인된 종료'(exited_confirmed)
        와 구분해 진단 파일에 남긴다. 판정 결과(살아있음/죽음)는 기존과 같은
        기준으로 나오되, 그 조회 방식만 §166과 동일하게 고쳤다."""
        last_detail = None
        for attempt in range(PROBE_LIVENESS_MAX_ATTEMPTS):
            status, detail = _probe_liveness_probe(pod_name)
            if status == "alive":
                return True
            last_detail = detail
            if status == "exited":
                _write_json_best_effort(local_liveness_diagnostic, {
                    "classification": "exited_confirmed", "exit_code": detail,
                    "attempt": attempt + 1, "max_attempts": PROBE_LIVENESS_MAX_ATTEMPTS,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                })
                return False
            # status == "exec_failed" - 일시적일 수 있음, 마지막 시도가 아니면 재확인
            if attempt < PROBE_LIVENESS_MAX_ATTEMPTS - 1:
                time.sleep(PROBE_LIVENESS_RETRY_INTERVAL_SEC)
        _write_json_best_effort(local_liveness_diagnostic, {
            "classification": "exec_failed_exhausted",
            "last_stderr": last_detail, "attempts": PROBE_LIVENESS_MAX_ATTEMPTS,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        })
        return False

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
            # 2026-10-01 - rep9(-c2)는 returncode/stderr를 버려 원인 구분 불가였다. 기록만 추가.
            kind = _classify_probe_fetch_failure(r)
            fetch_diag["failures"] = (fetch_diag["failures"] + [{
                "at": datetime.now(timezone.utc).isoformat(), "kind": kind,
                "returncode": r.returncode, "stderr": (r.stderr or "").strip()[:500],
                "stdout_len": len(r.stdout or ""), "consecutive": cache["consecutive_failures"],
            }])[-20:]
            _write_json_best_effort(local_fetch_diagnostic, fetch_diag)
            if cache["consecutive_failures"] >= PROBE_FETCH_FAILURE_THRESHOLD:
                kinds = sorted({f["kind"] for f in fetch_diag["failures"][-cache["consecutive_failures"]:]})
                raise TrialInvalid(
                    f"probe raw CSV를 {pod_name}에서 {cache['consecutive_failures']}회 연속 못 읽음"
                    f"(kubectl exec 실패 또는 빈 응답) - stale 데이터로 계속 판정할 수 없음"
                    f" | 분류={','.join(kinds)}, 마지막 rc={r.returncode}, "
                    f"stderr={(r.stderr or '').strip()[:200]!r} | 상세: {local_fetch_diagnostic.name}"
                )
            return cache["points"]
        cache["consecutive_failures"] = 0
        try:
            local_raw.write_text(r.stdout, encoding="utf-8")
            rows = slo_judge.load_raw(local_raw)
            cache["points"] = slo_judge.evaluate(rows) if rows else []
            cache["rows"] = rows
        except Exception:
            pass  # 아직 헤더뿐이거나 읽는 순간 걸린 미완성 마지막 줄 - 다음 refresh에서 재시도(fetch 자체는 성공이라 실패 카운트 안 함)
        return cache["points"]

    def notify_injected(t_injection: str):
        injection_ref["t"] = datetime.fromisoformat(t_injection)

    def is_slo_evaluable():
        # run_once.py의 NOT_EVALUABLE 게이트(2026-09-17)가 참조 - "여태
        # 위반이 안 보였다"를 COMPLIANT로 믿으려면 "주입 이후" 표본만으로
        # 유효한 관측 창이 쌓여야 한다(2026-09-17 재정정: 전체 누적 표본 수만
        # 보면 주입 전 표본이 섞이거나 주입 후 갱신이 멈춰도 잘못 True가 나올
        # 수 있었음). 순수 판정 로직은 _is_post_injection_window_evaluable()
        # 참고.
        return _is_post_injection_window_evaluable(cache["rows"], injection_ref["t"])

    def _warmed_up():
        # WINDOW_SEC(60초)만큼 표본이 쌓이기 전엔 find_t_slo() 결과를 안 믿는다
        # - 위 start()의 실측 버그 설명 참고. check_recovered()는 t_slo가
        # 실제로 찍힌 뒤에만(run_once()가) 부르므로 여기 게이트 하나로 충분.
        return warmup["started_at"] is not None and time.monotonic() - warmup["started_at"] >= slo_judge.WINDOW_SEC

    def check_slo_violation():
        if not _warmed_up():
            return False
        return slo_judge.find_t_slo(_refresh(), not_before=injection_ref["t"]) is not None

    def check_recovered():
        points = _refresh()
        t_slo = slo_judge.find_t_slo(points, not_before=injection_ref["t"])
        return t_slo is not None and slo_judge.find_t_recovery(points, t_slo) is not None

    def get_actual_slo_time():
        t = slo_judge.find_t_slo(cache["points"], not_before=injection_ref["t"])
        return t.isoformat() if t else None

    def get_actual_recovery_time():
        points = cache["points"]
        t_slo = slo_judge.find_t_slo(points, not_before=injection_ref["t"])
        t_rec = slo_judge.find_t_recovery(points, t_slo) if t_slo else None
        return t_rec.isoformat() if t_rec else None

    def get_baseline_status():
        # run_once.py의 BASELINE 단계(2026-09-18 추가)가 최대 120초 동안 반복
        # 호출한다. injection_ref는 아직 채워지기 전(notify_injected()는 주입
        # 확인 이후에만 호출됨)이므로 여기서는 안 쓴다 - "주입 전 안정 상태
        # 도달"이 목적이라 not_before 개념 자체가 없다(find_baseline_ready는
        # points 맨 앞부터 찾음).
        points = _refresh()
        if not points:
            return {"ready": False, "sample_count": 0, "p95": None, "availability": None}
        ready = slo_judge.find_baseline_ready(points)
        if ready is not None:
            return {
                "ready": True,
                "ready_at": ready["ready_at"].isoformat(),
                "sample_count": ready["sample_count"],
                "p95": ready["p95"],
                "availability": ready["availability"],
            }
        latest = points[-1]
        return {
            "ready": False,
            "sample_count": latest["sample_count"],
            "p95": latest["p95"],
            "availability": latest["success_rate"],
        }

    def stop():
        if fetch_diag["failures"]:
            # 2026-10-01 - 읽기 실패가 있었던 실행만: pod 삭제 전에 pod 쪽 증거(CSV 존재·크기,
            # probe.exit, probe.log 꼬리)를 남긴다. rep9는 이것이 pod와 함께 사라져 원인 미확정.
            snap = _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "sh", "-c",
                         f"ls -la {raw_remote} /probe.exit; echo '--exit'; cat /probe.exit; "
                         "echo '--probe.log'; tail -n 40 /probe.log; "
                         "echo '--wrapper'; tail -n 20 /probe-wrapper.log; true"])
            fetch_diag["pod_snapshot_before_delete"] = {
                "at": datetime.now(timezone.utc).isoformat(), "returncode": snap.returncode,
                "stdout": (snap.stdout or "")[-4000:], "stderr": (snap.stderr or "").strip()[:500],
            }
            _write_json_best_effort(local_fetch_diagnostic, fetch_diag)
        _run(["kubectl", "exec", "-n", NAMESPACE, pod_name, "--", "sh", "-c", "pkill -f probe.py || true"])
        _delete_pod(pod_name)  # --wait=true라 반환 시점엔 pod와 그 안 프로세스가 전부 확실히 종료됨

    return Prober(start=start, is_alive=is_alive, check_slo_violation=check_slo_violation,
                  check_recovered=check_recovered, stop=stop,
                  get_actual_slo_time=get_actual_slo_time, get_actual_recovery_time=get_actual_recovery_time,
                  is_slo_evaluable=is_slo_evaluable, notify_injected=notify_injected,
                  get_baseline_status=get_baseline_status)
