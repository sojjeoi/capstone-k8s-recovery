#!/usr/bin/env python3
"""§172(2026-09-26) - 시연 전용, active/preview Pod CPU 분리 계측(관찰
전용/shadow). retry3(단일 trial)에서 관찰된 조기 CPU 신호가 재현 가능한지
검증하려면 지금까지처럼 active+preview가 합산된 단일 cpu_mean만으로는
"어느 pod의 상승인지" 구분할 수 없었다 - 이 파일은 그 구분만 추가한다.

공식 IsolationForest의 feature 계산(anomaly-detection/features.py)·모델·
임계값·연속판정·정책·SLO는 이 파일에서 전혀 참조·수정하지 않는다. 기존
합산 cpu_mean을 대체하지 않고 완전히 별도 파일(호출자가 지정하는
output_path)에 기록한다 - 공식 detector 프로세스는 이 모듈의 존재를 모른
채 그대로 돈다. demo_early_warning_rule.py(§171)의 30%·2연속 조건은 그대로
가져다 쓴다(안 바꿈) - analyze_shadow_trace()는 그 규칙을 active pod의
개별 시계열에 적용해 "발화 여부/시각"만 계산할 뿐, 어떤 promote·정책
함수도 호출하지 않는다(이 파일의 함수 시그니처 자체에 그런 인자가 없다 -
구조적으로 정책과 분리)."""
import json
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional, Tuple

from kubernetes import client

from active_pod_resolver import NAMESPACE, get_active_pods, load_kube_config
from cpu_pressure_demo_adapter import get_pod_cpu_usage_cores_diagnostic
from demo_early_warning_rule import (
    RELATIVE_RISE_FACTOR, CONSECUTIVE_REQUIRED, find_demo_early_warning_trigger,
)
from run_once import Prober

PREVIEW_SERVICE = "vllm-preview"
# anomaly-detection/score_server.py:65(공식 detector 평가 주기, 그대로 인용
# - 새 숫자 발명 아님) - 같은 간격으로 tick을 맞춰야 시각 비교가 의미 있다.
EVAL_INTERVAL_SEC = 15
# §174(2026-09-27 무주입 라이브 진단, §7) 실측 근거 - empty_result 23건 중
# 22건에서 같은 30초 창의 원시 카운터 표본이 실제로 존재·변화하고 있었다
# (수집 공백 아님, instant rate() 질의 평가 자체가 그 순간 간헐적으로
# 빈 결과를 낸 것). 그래서 판정 기준(PromQL·30초 창)은 그대로 두고,
# empty_result에만(다른 사유는 재시도 안 함 - 성격이 다름) 짧게 재확인한다.
# §166/§169와 같은 발상(무한 재시도 아님, 값 대체 없음), 값은 새 숫자
# 아니라 §166과 동일하게 인용.
EMPTY_RESULT_RETRY_MAX_ATTEMPTS = 3
EMPTY_RESULT_RETRY_INTERVAL_SEC = 2.0

# §172 프로토콜(docs/design/phase8-v2-cpu-precursor-shadow-protocol-172.md
# §"실행 절차" 3번)의 최소 요구량 - 여기 한 곳에만 정의하고 러너
# (run_cpu_demo_shadow_trial.py)가 이 값을 그대로 가져다 쓴다(중복 정의로
# 값이 어긋나는 것 방지). 실행 전에 고정 - 결과를 보고 낮추지 않는다.
MIN_STABLE_BASELINE_TICKS = 8


def get_preview_pods() -> list:
    """active_pod_resolver.get_active_pods()와 완전히 같은 패턴 - 대상
    Service만 vllm-preview로 다르다. §172 전용 국소 함수(active_pod_
    resolver.py 자체는 안 건드림 - 시연 계측 범위를 이 파일 하나로
    한정하기 위함)."""
    load_kube_config()
    core = client.CoreV1Api()
    svc = core.read_namespaced_service(PREVIEW_SERVICE, NAMESPACE)
    selector = svc.spec.selector or {}
    label_selector = ",".join(f"{k}={v}" for k, v in selector.items())
    pods = core.list_namespaced_pod(NAMESPACE, label_selector=label_selector)
    return [{"name": p.metadata.name, "uid": p.metadata.uid} for p in pods.items]


def _query_with_bounded_retry_on_empty(
    pod_name: str,
    get_cpu_diagnostic_fn: Callable[[str], dict],
    max_attempts: int = EMPTY_RESULT_RETRY_MAX_ATTEMPTS,
    retry_interval_sec: float = EMPTY_RESULT_RETRY_INTERVAL_SEC,
) -> Tuple[dict, list]:
    """§174 §7 실측(empty_result 22/23건이 원시 데이터는 있는데 instant
    질의만 간헐적으로 빈 결과) 근거로, "empty_result"에만(다른 사유 -
    request_exception/stale_sample/http_status_* - 는 성격이 달라 즉시
    반환, 재시도 안 함) 짧게 재확인한다. 무한 재시도 아니고, 끝까지
    실패해도 0이나 직전 값으로 메우지 않는다(여전히 empty_result 그대로
    반환). 두 번째 반환값(attempts)에 시도별 사유를 순서대로 남겨 "언제
    실제로 값이 도착했는지"를 그대로 보존한다(사후 추정 없음)."""
    attempts = []
    for attempt in range(1, max_attempts + 1):
        diag = get_cpu_diagnostic_fn(pod_name)
        attempts.append({"attempt": attempt, "reason": diag["reason"]})
        if diag["reason"] != "empty_result":
            if attempt > 1 and diag["reason"] == "ok":
                return {"cores": diag["cores"], "reason": f"ok_after_retry({attempt})"}, attempts
            return diag, attempts
        if attempt < max_attempts:
            time.sleep(retry_interval_sec)
    return {"cores": None, "reason": "empty_result"}, attempts


def record_tick(
    tick_seq: int,
    get_active_pods_fn: Callable[[], list] = get_active_pods,
    get_preview_pods_fn: Callable[[], list] = get_preview_pods,
    get_cpu_diagnostic_fn: Callable[[str], dict] = get_pod_cpu_usage_cores_diagnostic,
    empty_retry_max_attempts: int = EMPTY_RESULT_RETRY_MAX_ATTEMPTS,
    empty_retry_interval_sec: float = EMPTY_RESULT_RETRY_INTERVAL_SEC,
) -> list:
    """이번 tick에 실제로 존재하는 각 pod(active/preview)마다 CPU를
    개별(pod 단위) 조회해 기록 하나씩 만든다 - 기존 합산 쿼리는 전혀 안
    쓴다. pod가 없으면(예: preview 미존재, native arm 등) 그 role은
    건너뛴다(억지로 0이나 None을 채워 넣지 않음 - 실측 아닌 값은 안 만든다).
    §174 §7 근거로 empty_result만 짧게 재확인한다(_query_with_bounded_
    retry_on_empty 참고) - query_received_at_utc는 재확인이 끝난(성공
    또는 최종 실패) 실제 시각을 기록한다."""
    records = []
    for role, get_pods_fn in (("active", get_active_pods_fn), ("preview", get_preview_pods_fn)):
        pods = get_pods_fn()
        if len(pods) != 1:
            continue  # 0개(없음) 또는 모호(2개+)면 이번 tick은 그 role을 기록하지 않음
        pod_name = pods[0]["name"]
        diag, attempts = _query_with_bounded_retry_on_empty(
            pod_name, get_cpu_diagnostic_fn, empty_retry_max_attempts, empty_retry_interval_sec)
        records.append({
            "tick_seq": tick_seq,
            "pod_role": role,
            "pod_name": pod_name,
            "query_received_at_utc": datetime.now(timezone.utc).isoformat(),
            "cores": diag["cores"],
            "status_reason": diag["reason"],
            "empty_retry_attempts": attempts,
        })
    return records


def run_shadow_trace(
    output_path,
    max_ticks: int,
    interval_sec: float = EVAL_INTERVAL_SEC,
    stop_event: Optional[threading.Event] = None,
    get_active_pods_fn: Callable[[], list] = get_active_pods,
    get_preview_pods_fn: Callable[[], list] = get_preview_pods,
    get_cpu_diagnostic_fn: Callable[[str], dict] = get_pod_cpu_usage_cores_diagnostic,
) -> None:
    """max_ticks회 record_tick()을 반복해 output_path(JSONL)에 append만
    한다. 판정(demo_early_warning_rule 적용)은 여기서 하지 않고
    analyze_shadow_trace()로 별도(실행 중 또는 종료 후 아무 때나) 수행한다
    - 이 루프의 시그니처 자체에 promote·정책 콜백 인자가 없어 구조적으로
    정책과 분리돼 있다."""
    stop_event = stop_event or threading.Event()
    with open(output_path, "a", encoding="utf-8") as f:
        for tick_seq in range(1, max_ticks + 1):
            if stop_event.is_set():
                return
            for r in record_tick(tick_seq, get_active_pods_fn, get_preview_pods_fn, get_cpu_diagnostic_fn):
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.flush()
            if stop_event.wait(interval_sec):
                return


def analyze_shadow_trace(
    records: list,
    relative_rise_factor: float = RELATIVE_RISE_FACTOR,
    consecutive_required: int = CONSECUTIVE_REQUIRED,
    stable_baseline_start_utc: Optional[str] = None,
    injection_boundary_utc: Optional[str] = None,
    min_stable_baseline_ticks: int = MIN_STABLE_BASELINE_TICKS,
) -> Optional[dict]:
    """record_tick()이 남긴 레코드(딕셔너리) 리스트에서 active pod의 cores
    값만 시간순으로 뽑아 demo_early_warning_rule(§171, 값 그대로) 그대로
    적용한다 - 새 판정 로직을 추가하지 않고 기존 순수 함수를 감싸기만
    한다. 이 함수 자체는 policy 결정을 절대 내리지 않고 관찰 기록만
    반환한다.

    §175(2026-09-27) 정정: run_once.py 실측 확인 - prober.start()(실제
    요청 부하 시작)는 injector.prepare()(preview 준비, 실측 260~340초)가
    끝난 "뒤"에야 호출된다. 그래서 이전 버전처럼 시계열의 "첫 유효값"을
    기준선으로 쓰면, 그 값이 preview 준비 중(요청 부하가 전혀 없던 시점)
    표본일 수 있다 - §172 프로토콜이 요구하는 "요청 부하가 흐르는 상태의
    정상 tick"이 아니다.

    stable_baseline_start_utc(보통 trial의 t_baseline_ready - 요청 부하가
    실제로 안정됐다고 이미 공식 로직이 판단한 시각, 새 판정 기준 발명
    아님)를 넘기면, 그 시각 "이후"의 값만 쓴다(그 이전 preview-prep 구간
    표본은 기준선·판정 모두에서 제외). injection_boundary_utc도 같이
    넘기면, 그 안정 구간에 min_stable_baseline_ticks(§172 프로토콜 최소
    요구량) 이상의 주입 전 tick이 실제로 있는지 확인한다 - 부족하면
    발화 여부를 "판정하지 않고"(fired=None) 그 사실 자체를 반환한다(성공
    으로 위장하지 않음). 두 인자를 안 넘기면(호출자가 안정 구간 정보가
    없으면) 기존 동작(첫 유효값 기준, 하위 호환)을 그대로 쓴다."""
    active_series_all = [(r["query_received_at_utc"], r["cores"]) for r in records
                          if r.get("pod_role") == "active" and r.get("cores") is not None]
    if not active_series_all:
        return None

    if stable_baseline_start_utc is None:
        series = active_series_all
        baseline_source = "first_valid_tick(no_stability_window_provided)"
    else:
        series = [(ts, v) for ts, v in active_series_all if ts >= stable_baseline_start_utc]
        if not series:
            return {"fired": None, "reason": "no_ticks_after_stable_baseline_start",
                     "stable_baseline_start_utc": stable_baseline_start_utc}
        if injection_boundary_utc is not None:
            pre_injection_stable_count = sum(1 for ts, _ in series if ts < injection_boundary_utc)
            if pre_injection_stable_count < min_stable_baseline_ticks:
                return {"fired": None, "reason": "insufficient_stable_baseline_ticks",
                         "stable_pre_injection_ticks_found": pre_injection_stable_count,
                         "min_required": min_stable_baseline_ticks}
        baseline_source = "first_tick_at_or_after_stable_baseline_start"

    baseline = series[0][1]
    idx = find_demo_early_warning_trigger(series, baseline, relative_rise_factor, consecutive_required)
    if idx is None:
        return {"fired": False, "baseline_cpu_mean": baseline, "baseline_source": baseline_source}
    return {"fired": True, "baseline_cpu_mean": baseline, "baseline_source": baseline_source,
            "fired_at_utc": series[idx][0], "fired_cores": series[idx][1]}


# cpu_pressure_demo_adapter._cpu_promql()의 rate(...)[30s]와 동일 값을
# 그대로 인용한다(새 숫자 아님) - 이 함수 자신의 30초 lookback 창이 요청
# 부하 시작 전 유휴 구간까지 되돌아보지 않도록, 게이트 시작(첫 호출) 후
# 이만큼은 아예 tick을 세지 않고 기다린다(§177 - 첫 유효값 오염 방지).
RATE_WINDOW_WARMUP_SEC = 30.0


def wrap_prober_with_pre_injection_tick_gate(
    prober: Prober,
    get_active_pods_fn: Callable[[], list] = get_active_pods,
    get_cpu_diagnostic_fn: Callable[[str], dict] = get_pod_cpu_usage_cores_diagnostic,
    min_valid_ticks: int = MIN_STABLE_BASELINE_TICKS,
    tick_interval_sec: float = EVAL_INTERVAL_SEC,
    warmup_sec: float = RATE_WINDOW_WARMUP_SEC,
) -> Tuple[Prober, dict]:
    """§176/§177(2026-09-27) - run_once.py는 전혀 안 건드리고, prober.get_
    baseline_status()(run_once._wait_for_baseline()이 poll_interval_sec
    ~1초 간격으로 반복 호출 - 실측 코드 확인)만 감싸 "SLO 판정용 baseline
    준비"와 "active pod의 유효(빈 결과 제외) CPU tick min_valid_ticks개
    확보"를 함께 게이트한다.

    §177 정정 3가지(라이브 실행 전 오프라인 검토에서 발견):
    1) **웜업**: 이 함수가 처음 불린 시각(=요청 부하가 막 시작된 시점의
       대리 지표 - run_once.py 순서상 prober.start() 직후에만 baseline
       단계가 시작됨)부터 warmup_sec(30초, CPU rate() 자신의 lookback
       창과 동일)이 지나기 전까지는 tick 자체를 세지 않는다 - 그 전에
       세면 rate()의 30초 창이 유휴 구간까지 되돌아봐 첫 유효값이
       오염될 수 있다.
    2) **판정과 게이트의 데이터 일원화**: 이 함수가 모은 state["valid_
       ticks"](정확히 min_valid_ticks개, 값·시각·pod_uid 전부 보존)가
       조기경보 판정에 실제로 쓰일 "사전 고정 기준선"이다. 호출자는 이
       리스트를 demo_early_warning_rule 판정에 **그대로** 이어붙여야
       한다(연속 shadow 트레이서의 별도 폴링에서 "우연히 같은 8개가
       다시 잡히길" 기대하며 시각으로 재필터링하지 않는다) - 그래야
       "게이트는 통과했는데 판정은 기록 부족으로 보류"되는 불일치가
       생기지 않는다.
    3) **t_baseline_ready 의미 분리**: run_once.py의 TrialResult.
       t_baseline_ready는 이 wrapper 때문에 "SLO 조건과 CPU 게이트 둘 다
       충족된 시각"이 된다(§176에서 이미 고지). 순수 SLO 준비 시각과
       CPU 게이트 완료 시각을 각각 알고 싶은 호출자를 위해 state에
       slo_baseline_ready_at_utc/cpu_gate_ready_at_utc를 따로 남긴다 -
       둘을 같은 값으로 표시하지 않는다(서로 다른 시각일 수 있음).

    §178 정정(2026-09-27) - 위 3)은 "ready만 AND로 게이트하면 t_baseline_
    ready도 자동으로 AND 시각이 된다"고 가정했으나 틀렸다: run_once.
    _wait_for_baseline()(run_once.py:500-503)은 status["ready_at"]가
    있으면 그 값을 그대로 쓴다("or _now()"는 없을 때만). 실측 확인 결과
    base_status(예: load_ramp_adapter.get_baseline_status())가 자신의
    ready_at을 순수 SLO 조건 충족 시각으로 이미 채워 넣고 있어, 이 wrapper가
    **ready만** 겹쳐 쓰고 ready_at은 그대로 흘려보내면(**base_status 스프레드
    그대로) t_baseline_ready가 CPU 게이트를 기다리지 않은 더 이른 시각으로
    기록된다. 그래서 이제 combined_ready가 True가 되는 바로 그 호출에서
    ready_at도 "지금"으로 함께 덮어쓴다 - 그래야 t_baseline_ready가 실제
    AND 시각과 일치한다.

    get_baseline_status()가 이미 매초 불리므로, 실제 CPU 질의는 이
    안에서 tick_interval_sec(기본 15초)마다만 자체 제한한다(Prometheus
    과다 호출 방지 - 매초 쏘지 않음). _query_with_bounded_retry_on_empty
    (§174)를 그대로 재사용해 empty_result만 짧게 재확인하고, 그래도
    실패하면 그 tick은 유효로 안 센다(0/직전값 대체 없음). Pod uid가
    바뀌면 그동안 모은 tick을 버리고 새 Pod부터 다시 센다(기준선 오염
    방지)."""
    original_get_baseline_status = prober.get_baseline_status
    state = {
        "valid_ticks": [], "last_uid": None, "last_tick_query_monotonic": None,
        "gate_first_called_monotonic": None,
        "slo_baseline_ready_at_utc": None, "cpu_gate_ready_at_utc": None,
    }

    def gated_get_baseline_status():
        base_status = original_get_baseline_status()
        now_mono = time.monotonic()
        if state["gate_first_called_monotonic"] is None:
            state["gate_first_called_monotonic"] = now_mono
        if base_status.get("ready") and state["slo_baseline_ready_at_utc"] is None:
            state["slo_baseline_ready_at_utc"] = datetime.now(timezone.utc).isoformat()

        past_warmup = (now_mono - state["gate_first_called_monotonic"]) >= warmup_sec
        if past_warmup and (
                state["last_tick_query_monotonic"] is None
                or now_mono - state["last_tick_query_monotonic"] >= tick_interval_sec):
            state["last_tick_query_monotonic"] = now_mono
            pods = get_active_pods_fn()
            if len(pods) == 1:
                pod_name, pod_uid = pods[0]["name"], pods[0]["uid"]
                diag, attempts = _query_with_bounded_retry_on_empty(pod_name, get_cpu_diagnostic_fn)
                tick_record = {
                    "pod_role": "active",
                    "query_received_at_utc": datetime.now(timezone.utc).isoformat(),
                    "pod_name": pod_name, "pod_uid": pod_uid,
                    "cores": diag["cores"], "status_reason": diag["reason"],
                    "empty_retry_attempts": attempts,
                }
                if diag["cores"] is not None:
                    if state["last_uid"] is not None and pod_uid != state["last_uid"]:
                        state["valid_ticks"] = []  # Pod 교체 - 기준선 다시 셈(오염 방지)
                    state["last_uid"] = pod_uid
                    state["valid_ticks"].append(tick_record)
        tick_gate_ok = len(state["valid_ticks"]) >= min_valid_ticks
        if tick_gate_ok and state["cpu_gate_ready_at_utc"] is None:
            state["cpu_gate_ready_at_utc"] = state["valid_ticks"][min_valid_ticks - 1]["query_received_at_utc"]
        combined_ready = bool(base_status.get("ready")) and tick_gate_ok
        result_status = {
            **base_status,
            "ready": combined_ready,
            "cpu_tick_gate_ok": tick_gate_ok,
            "cpu_valid_ticks_collected": len(state["valid_ticks"]),
        }
        if combined_ready:
            # §178(2026-09-27) - run_once._wait_for_baseline()(run_once.py:500-503)은
            # status["ready_at"]가 있으면 그 값을 t_baseline_ready로 우선 쓴다
            # ("or _now()"는 없을 때만 씀). base_status(예: load_ramp_adapter.
            # get_baseline_status(), 실측 확인)의 ready_at은 순수 SLO 조건만
            # 충족된 시각이라 CPU 게이트를 기다리지 않는다 - 그대로 흘려보내면
            # 두 조건이 다 갖춰지기 전인 더 이른 시각이 t_baseline_ready로
            # 기록된다(baseline_ready_semantics가 약속한 AND 의미와 어긋남).
            # 두 조건이 실제로 모두 충족된 이 순간("지금")으로 덮어써야 한다.
            result_status["ready_at"] = datetime.now(timezone.utc).isoformat()
        return result_status

    wrapped = Prober(
        start=prober.start, is_alive=prober.is_alive,
        check_slo_violation=prober.check_slo_violation, check_recovered=prober.check_recovered,
        stop=prober.stop, get_actual_slo_time=prober.get_actual_slo_time,
        get_actual_recovery_time=prober.get_actual_recovery_time,
        is_slo_evaluable=prober.is_slo_evaluable, notify_injected=prober.notify_injected,
        get_baseline_status=gated_get_baseline_status,
    )
    return wrapped, state
