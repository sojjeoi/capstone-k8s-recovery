#!/usr/bin/env python3
"""memory_pressure_adapter.py 검증(2026-09-20 추가) - network_degrade_adapter.py/
pod_kill_adapter.py와 같은 오프라인 assert+print 스타일(test_network_degrade_
adapter.py 참고). 실클러스터·Prometheus 없이 전부 fake *_fn으로 동작한다.

이 어댑터가 다른 두 어댑터와 다른 점(추가 검증 대상): 주입 전 headroom
게이트(Node MemAvailable·컨테이너 limit·안전 상한 3개), 주입 중 안전 감시
5종(Node MemAvailable/target working set/OOMKilled/restartCount/Node 조건),
각 CR의 duration 안전망, is_effective()의 이중 조건(AllInjected AND working
set 상승), 안전 관측값(evidence) 로그.

클러스터를 만지는 *_fn 훅은 전부 명시적으로 주입해야 한다 - conftest.py의
cluster_guard가 빠뜨린 실제 K8s/Prometheus 호출을 즉시 실패로 만든다."""
import sys
import time
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8")

from memory_pressure_adapter import (
    EFFECTIVE_WORKING_SET_RISE_BYTES,
    GIB,
    STAGE_DURATION_SAFETY_MARGIN_SEC,
    _classify_timestamp_against_windows,
    make_memory_pressure_injector,
)
from run_once import HarnessCorrupted, TrialInvalid

RUN_ID = "memory_pressure-proposed-01-20260920T120000Z"
ARM = "proposed"
REP = 1
TARGET_POD = {"name": "vllm-abc123", "uid": "uid-1"}
REPLACEMENT_POD = {"name": "vllm-def456", "uid": "uid-2"}
NODE_NAME = "sj-worker"
NODE_IP = "192.168.30.76"

HEALTHY_CONDITIONS = {"Ready": "True", "MemoryPressure": "False", "DiskPressure": "False", "PIDPressure": "False"}
NOT_READY_CONDITIONS = {"Ready": "False", "MemoryPressure": "False", "DiskPressure": "False", "PIDPressure": "False"}

BASELINE_WS = 3.5 * GIB
HEALTHY_AVAILABLE = 8.0 * GIB
HEALTHY_LIMIT = 6.0 * GIB

FAST_STAGE = [{"name": "stage-1-500mb", "size_mb": 500, "workers": 1, "duration_sec": 0.05}]
SLOW_STAGE = [{"name": "stage-1-500mb", "size_mb": 500, "workers": 1, "duration_sec": 5.0}]
TWO_FAST_STAGES = [
    {"name": "stage-1-500mb", "size_mb": 500, "workers": 1, "duration_sec": 0.05},
    {"name": "stage-2-1000mb", "size_mb": 1000, "workers": 1, "duration_sec": 0.05},
]


def _wait_for(check, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(interval)
    return False


def _raises_exception(fn, exc_types, timeout=5.0):
    result = {"exc": None}

    def check():
        try:
            fn()
        except exc_types as e:
            result["exc"] = e
            return True
        return False
    ok = _wait_for(check, timeout=timeout)
    return ok, result["exc"]


def _pod_details(uid="uid-1", node_name=NODE_NAME, memory_limit_bytes=HEALTHY_LIMIT,
                  restart_count=0, oom_killed=False, name="vllm-abc123"):
    return {"name": name, "uid": uid, "node_name": node_name, "memory_limit_bytes": memory_limit_bytes,
            "restart_count": restart_count, "oom_killed": oom_killed}


def _sequence_fn(values):
    """호출될 때마다 다음 값을 돌려주고 마지막 값에서 멈춘다(clamp) - 시간에
    따라 바뀌는 안전 지표(예: 몇 번째 확인부터 위반)를 시뮬레이션한다."""
    state = {"i": 0}

    def fn(*args, **kwargs):
        i = min(state["i"], len(values) - 1)
        state["i"] += 1
        return values[i]
    return fn


def _rising_working_set_fn():
    """prepare()의 baseline 측정(첫 호출)은 BASELINE_WS를 주고, 그 뒤로는
    is_started()가 곧바로 latch할 수 있을 만큼 충분히 오른 값을 준다(2026-09-20
    정정 이후 is_started()가 AllInjected와 working set 상승을 함께 요구하므로,
    "효과가 이미 확정된" 상황을 빠르게 만들어야 하는 target-replacement/
    classify_stage 테스트에서 쓴다). 상승값은 안전 상한(5GiB)보다 한참
    낮아 안전 tick에서도 위반을 일으키지 않는다."""
    state = {"n": 0}

    def fn(name):
        state["n"] += 1
        return BASELINE_WS if state["n"] == 1 else BASELINE_WS + EFFECTIVE_WORKING_SET_RISE_BYTES + (10 * 1024 * 1024)
    return fn


def _make_healthy_injector(stages=FAST_STAGE, log=None, **overrides):
    """기본적으로 prepare()가 통과하고 안전 위반이 전혀 없는 injector -
    overrides로 특정 *_fn만 갈아끼운다."""
    kwargs = dict(
        get_active_pods_fn=lambda: [TARGET_POD],
        get_pod_details_fn=lambda name: _pod_details(),
        get_node_ip_fn=lambda node_name: NODE_IP,
        get_node_conditions_fn=lambda node_name: dict(HEALTHY_CONDITIONS),
        get_node_available_fn=lambda ip: HEALTHY_AVAILABLE,
        get_working_set_fn=lambda name: BASELINE_WS,
        is_stage_injected_fn=lambda cr_name: True,
        does_chaos_exist_fn=lambda cr_name: False,
        create_chaos_fn=lambda *a, **kw: None,
        delete_chaos_fn=lambda cr_name: None,
        safety_poll_interval_sec=0.02,
        stage_delete_poll_interval_sec=0.01,
        cleanup_recovery_timeout_sec=0.05,
        log_fn=(log.append if log is not None else (lambda r: None)),
    )
    kwargs.update(overrides)
    return make_memory_pressure_injector(RUN_ID, ARM, REP, stages=stages, **kwargs)


# --- stages 필수화 -----------------------------------------------------

def test_stages_empty_list_rejected():
    try:
        make_memory_pressure_injector(RUN_ID, ARM, REP, stages=[])
        assert False, "빈 stages는 거부돼야 함"
    except ValueError:
        pass
    print("OK - stages=[] 거부(기본값 없음 - 우발적 기본 실행 방지)")


def test_stages_missing_argument_is_a_type_error():
    try:
        make_memory_pressure_injector(RUN_ID, ARM, REP)
        assert False, "stages 없이 호출되면 안 됨"
    except TypeError:
        pass
    print("OK - stages 인자 자체가 없으면 TypeError(기본값이 아예 없음)")


# --- prepare() 게이트 ----------------------------------------------------

def test_prepare_no_target_found():
    injector = _make_healthy_injector(get_active_pods_fn=lambda: [])
    try:
        injector.prepare()
        assert False
    except TrialInvalid as e:
        assert "없음" in str(e)
    print("OK - 대상 없음: fail-closed TrialInvalid")


def test_prepare_multiple_targets_found():
    pods = [{"name": "vllm-a", "uid": "uid-a"}, {"name": "vllm-b", "uid": "uid-b"}]
    injector = _make_healthy_injector(get_active_pods_fn=lambda: pods)
    try:
        injector.prepare()
        assert False
    except TrialInvalid as e:
        assert "vllm-a" in str(e) and "vllm-b" in str(e)
    print("OK - 다중 대상: fail-closed TrialInvalid")


def test_prepare_pod_details_unreadable():
    injector = _make_healthy_injector(get_pod_details_fn=lambda name: None)
    try:
        injector.prepare()
        assert False
    except TrialInvalid as e:
        assert "상세" in str(e)
    print("OK - pod 상세 조회 실패: fail-closed TrialInvalid")


def test_prepare_node_ip_unreadable():
    injector = _make_healthy_injector(get_node_ip_fn=lambda n: None)
    try:
        injector.prepare()
        assert False
    except TrialInvalid as e:
        assert "InternalIP" in str(e)
    print("OK - Node InternalIP 조회 실패: fail-closed TrialInvalid")


def test_prepare_node_unhealthy():
    injector = _make_healthy_injector(get_node_conditions_fn=lambda n: dict(NOT_READY_CONDITIONS))
    try:
        injector.prepare()
        assert False
    except TrialInvalid as e:
        assert "Node" in str(e)
    print("OK - 주입 전 Node NotReady: fail-closed TrialInvalid(아직 아무 것도 안 만짐 - HarnessCorrupted 아님)")


def test_prepare_node_available_unreadable():
    injector = _make_healthy_injector(get_node_available_fn=lambda ip: None)
    try:
        injector.prepare()
        assert False
    except TrialInvalid as e:
        assert "MemAvailable" in str(e)
    print("OK - Node MemAvailable 조회 실패: fail-closed TrialInvalid")


def test_prepare_node_available_below_threshold():
    injector = _make_healthy_injector(get_node_available_fn=lambda ip: 2.0 * GIB)
    try:
        injector.prepare()
        assert False
    except TrialInvalid as e:
        assert "부족" in str(e)
    print("OK - Node MemAvailable < 3GiB: 주입 전 fail-closed")


def test_prepare_working_set_unreadable():
    injector = _make_healthy_injector(get_working_set_fn=lambda name: None)
    try:
        injector.prepare()
        assert False
    except TrialInvalid as e:
        assert "working set" in str(e)
    print("OK - target working set 조회 실패: fail-closed TrialInvalid")


def test_prepare_headroom_insufficient_against_container_limit():
    # limit 4GiB, baseline 3.9GiB + stage 500MB 투영 -> limit 초과
    injector = _make_healthy_injector(
        get_pod_details_fn=lambda name: _pod_details(memory_limit_bytes=4.0 * GIB),
        get_working_set_fn=lambda name: 3.9 * GIB)
    try:
        injector.prepare()
        assert False
    except TrialInvalid as e:
        assert "headroom" in str(e)
    print("OK - 컨테이너 limit 기준 headroom 부족: fail-closed TrialInvalid")


def test_prepare_headroom_insufficient_against_safety_ceiling_even_without_known_limit():
    # 컨테이너 limit을 모르거나(파싱 실패) 아주 커도 5GiB 안전 상한은 항상 적용돼야 한다.
    injector = _make_healthy_injector(
        get_pod_details_fn=lambda name: _pod_details(memory_limit_bytes=None),
        get_working_set_fn=lambda name: 4.8 * GIB)
    try:
        injector.prepare()
        assert False
    except TrialInvalid as e:
        assert "headroom" in str(e)
    print("OK - 컨테이너 limit을 몰라도 5GiB 안전 상한(MAX_TARGET_WORKING_SET_BYTES)은 항상 적용")


def test_prepare_headroom_uses_largest_stage_in_list():
    # 여러 stage 중 가장 큰 것 기준으로 투영해야 한다(순서와 무관).
    injector = _make_healthy_injector(
        stages=[{"name": "s0", "size_mb": 100, "workers": 1, "duration_sec": 0.01},
                {"name": "s1", "size_mb": 2000, "workers": 1, "duration_sec": 0.01}],
        get_pod_details_fn=lambda name: _pod_details(memory_limit_bytes=4.0 * GIB),
        get_working_set_fn=lambda name: 3.5 * GIB)  # 3.5+2.0=5.5GiB > 4GiB limit
    try:
        injector.prepare()
        assert False
    except TrialInvalid as e:
        assert "headroom" in str(e)
    print("OK - headroom 투영은 stage 목록 중 최댓값 기준")


def test_prepare_ok_logs_actual_applied_values_as_evidence():
    log = []
    injector = _make_healthy_injector(log=log)
    injector.prepare()
    rec = next(r for r in log if r["event"] == "prepare_ok")
    assert rec["baseline_working_set_bytes"] == BASELINE_WS
    assert rec["node_available_bytes"] == HEALTHY_AVAILABLE
    assert rec["memory_limit_bytes"] == HEALTHY_LIMIT
    assert rec["baseline_restart_count"] == 0
    print("OK - prepare 성공 시 실제 적용값(headroom 근거)이 evidence 로그에 남음")


# --- is_started()/is_effective(): AllInjected AND working set 상승 --------
#
# 2026-09-20 정정(live smoke 실측 발견) - 이 둘의 확인 책임을 is_effective()가
# 아니라 is_started() 쪽에 둔다. run_once.py는 `_wait_for(injector.is_started,
# injection_started_timeout_sec, poll_interval_sec)`로 is_started()는 반복
# 재시도하지만, `result.injection_valid = started and injector.is_effective()`
# 에서 is_effective()는 그 직후 **단 한 번만** 확인한다 - Prometheus cAdvisor
# 지표 반영 지연(smoke 실측 약 18초)을 흡수하지 못해, 실제로는 정상 적용된
# 압박이 injection_valid=False(invalid_run)로 잘못 판정됐다. working set 상승
# 확인 자체를 재시도되는 is_started()로 옮겼고, is_effective()는 그 결과를
# 그대로 재사용한다(`return is_started()`).

def test_is_started_requires_both_allinjected_and_working_set_rise():
    ws_state = {"v": BASELINE_WS}
    injector = _make_healthy_injector(
        stages=SLOW_STAGE,
        is_stage_injected_fn=lambda cr_name: True,
        get_working_set_fn=lambda name: ws_state["v"])
    injector.prepare()
    injector.inject()
    for _ in range(5):
        assert injector.is_started() is False, "AllInjected만으로는 아직 시작 확정 아님"
    ws_state["v"] = BASELINE_WS + EFFECTIVE_WORKING_SET_RISE_BYTES + (10 * 1024 * 1024)
    assert _wait_for(injector.is_started), "working set이 오르면(재시도 끝에) latch돼야 함"
    assert injector.is_effective() is True, "is_effective는 is_started와 같은 상태를 재사용"
    injector.cleanup()
    print("OK - is_started는 AllInjected만으로 부족 - working set 상승까지 재시도하며 함께 확인")


def test_working_set_unreadable_after_prepare_trips_safety_before_effective():
    # prepare()의 baseline 측정만 정상값을 받고, 그 뒤로 계속 조회 실패하는 상황.
    # is_started()가 이제 매 폴링마다 working set도 함께 확인하므로, CR 생성 전
    # 안전 pre-check가 먼저 이를 잡아 TrialInvalid로 fail-closed한다 - "확인
    # 안 됨"을 "효과 있음"으로 착각하지 않는다.
    ws_seq = _sequence_fn([BASELINE_WS, None])
    injector = _make_healthy_injector(stages=SLOW_STAGE, get_working_set_fn=ws_seq)
    injector.prepare()
    injector.inject()
    ok, exc = _raises_exception(injector.is_done, (TrialInvalid, HarnessCorrupted))
    assert ok and isinstance(exc, TrialInvalid), exc
    assert "working_set_unreadable" in str(exc)
    print("OK - working set을 계속 못 읽으면 CR 생성 전 안전 pre-check가 fail-closed")


# --- 주입 중 즉시 중단 조건(안전 감시) --------------------------------------

def test_safety_violation_node_available_low_deletes_cr_and_raises_trial_invalid():
    calls = {"create": [], "delete": []}
    available_seq = _sequence_fn([HEALTHY_AVAILABLE, HEALTHY_AVAILABLE, 2.0 * GIB])
    injector = _make_healthy_injector(
        stages=SLOW_STAGE, get_node_available_fn=available_seq,
        create_chaos_fn=lambda *a, **kw: calls["create"].append(a),
        delete_chaos_fn=lambda cr_name: calls["delete"].append(cr_name))
    injector.prepare()
    injector.inject()
    ok, exc = _raises_exception(injector.is_done, (TrialInvalid, HarnessCorrupted))
    assert ok and isinstance(exc, TrialInvalid), exc
    assert "node_memavailable_low" in str(exc)
    assert len(calls["delete"]) >= 1, "위반 감지 시 CR을 즉시 삭제해야 함"
    print("OK - Node MemAvailable < 3GiB: 즉시 CR 삭제 + TrialInvalid")


def test_safety_violation_node_available_unreadable_mid_injection_fails_closed():
    available_seq = _sequence_fn([HEALTHY_AVAILABLE, HEALTHY_AVAILABLE, None])
    injector = _make_healthy_injector(stages=SLOW_STAGE, get_node_available_fn=available_seq)
    injector.prepare()
    injector.inject()
    ok, exc = _raises_exception(injector.is_done, (TrialInvalid, HarnessCorrupted))
    assert ok and isinstance(exc, TrialInvalid), exc
    assert "node_memavailable_unreadable" in str(exc)
    print("OK - 주입 중 Node MemAvailable 조회 실패: 계속 진행 안 하고 fail-closed TrialInvalid")


def test_safety_violation_working_set_high_deletes_cr_and_raises_trial_invalid():
    calls = {"delete": []}
    ws_seq = _sequence_fn([BASELINE_WS, BASELINE_WS, 5.5 * GIB])
    injector = _make_healthy_injector(
        stages=SLOW_STAGE, get_working_set_fn=ws_seq,
        delete_chaos_fn=lambda cr_name: calls["delete"].append(cr_name))
    injector.prepare()
    injector.inject()
    ok, exc = _raises_exception(injector.is_done, (TrialInvalid, HarnessCorrupted))
    assert ok and isinstance(exc, TrialInvalid), exc
    assert "working_set_high" in str(exc)
    assert len(calls["delete"]) >= 1
    print("OK - target working set > 5GiB: 즉시 CR 삭제 + TrialInvalid")


def test_safety_violation_node_unhealthy_raises_harness_corrupted():
    conditions_seq = _sequence_fn([dict(HEALTHY_CONDITIONS), dict(HEALTHY_CONDITIONS), dict(NOT_READY_CONDITIONS)])
    injector = _make_healthy_injector(stages=SLOW_STAGE, get_node_conditions_fn=conditions_seq)
    injector.prepare()
    injector.inject()
    ok, exc = _raises_exception(injector.is_done, (TrialInvalid, HarnessCorrupted))
    assert ok and isinstance(exc, HarnessCorrupted), exc
    assert "node_unhealthy" in str(exc)
    print("OK - 주입 중 Node NotReady/pressure: HarnessCorrupted(배치 중단 필요, invalid_run이 아님)")


def test_safety_violation_oom_killed_raises_trial_invalid():
    details_seq = _sequence_fn([_pod_details(), _pod_details(), _pod_details(restart_count=1, oom_killed=True)])
    injector = _make_healthy_injector(stages=SLOW_STAGE, get_pod_details_fn=details_seq)
    injector.prepare()
    injector.inject()
    ok, exc = _raises_exception(injector.is_done, (TrialInvalid, HarnessCorrupted))
    assert ok and isinstance(exc, TrialInvalid), exc
    assert "oom_killed" in str(exc)
    print("OK - 주입 중 OOMKilled: 즉시 TrialInvalid(HarnessCorrupted 아님)")


def test_safety_violation_restart_increased_without_oom_labels_cause_correctly():
    details_seq = _sequence_fn([_pod_details(), _pod_details(), _pod_details(restart_count=1, oom_killed=False)])
    injector = _make_healthy_injector(stages=SLOW_STAGE, get_pod_details_fn=details_seq)
    injector.prepare()
    injector.inject()
    ok, exc = _raises_exception(injector.is_done, (TrialInvalid, HarnessCorrupted))
    assert ok and isinstance(exc, TrialInvalid), exc
    assert "restart_increased" in str(exc)
    assert "OOMKilled 아님" in str(exc), "원인이 OOM이 아님을 메시지에 구분해 남겨야 함"
    print("OK - 주입 중 restartCount 증가(OOM 아님): TrialInvalid + 원인 구분(liveness 등으로 추정)")


def test_safety_violation_target_unreadable_mid_injection_fails_closed():
    details_seq = _sequence_fn([_pod_details(), _pod_details(), None])
    injector = _make_healthy_injector(stages=SLOW_STAGE, get_pod_details_fn=details_seq)
    injector.prepare()
    injector.inject()
    ok, exc = _raises_exception(injector.is_done, (TrialInvalid, HarnessCorrupted))
    assert ok and isinstance(exc, TrialInvalid), exc
    assert "target_unreadable" in str(exc)
    print("OK - 주입 중 target pod 조회 실패(사라짐 등): fail-closed TrialInvalid")


# --- CR duration 안전망 ---------------------------------------------------

def test_stage_cr_created_with_duration_safety_margin():
    captured = {}

    def create_chaos_fn(cr_name, run_id, arm, target_name, stage, duration=None):
        captured["duration"] = duration

    injector = _make_healthy_injector(stages=SLOW_STAGE, create_chaos_fn=create_chaos_fn)
    injector.prepare()
    injector.inject()
    assert _wait_for(lambda: "duration" in captured)
    expected = f"{SLOW_STAGE[0]['duration_sec'] + STAGE_DURATION_SAFETY_MARGIN_SEC:.0f}s"
    assert captured["duration"] == expected, captured
    injector.cleanup()
    print("OK - 각 StressChaos CR에 stage 지속시간 + 안전 여유가 duration으로 명시됨(하니스 죽어도 자동 복구)")


def test_stage_selector_uses_pods_not_labels():
    # StressChaos는 selector.pods로 특정 pod에 고정해야 한다(labelSelectors 아님).
    # create_memory_stress_chaos()는 load_kube_config()도 직접 부르므로(conftest.py의
    # cluster_guard가 막는 실제 K8s 접근) 이것도 함께 무해한 함수로 바꿔치기해야 한다.
    import memory_pressure_adapter as mpa
    created = {}

    class _FakeCustomObjectsApi:
        def create_namespaced_custom_object(self, group, version, namespace, plural, body):
            created["body"] = body

    orig_client_cls = mpa.client.CustomObjectsApi
    orig_load_kube_config = mpa.load_kube_config
    mpa.client.CustomObjectsApi = _FakeCustomObjectsApi
    mpa.load_kube_config = lambda: None
    try:
        mpa.create_memory_stress_chaos("cr-1", RUN_ID, ARM, "vllm-abc123",
                                        {"size_mb": 500, "workers": 1}, duration="120s")
    finally:
        mpa.client.CustomObjectsApi = orig_client_cls
        mpa.load_kube_config = orig_load_kube_config
    selector = created["body"]["spec"]["selector"]
    assert "pods" in selector and selector["pods"] == {"vllm-serving": ["vllm-abc123"]}, selector
    assert "labelSelectors" not in selector
    assert created["body"]["spec"]["duration"] == "120s"
    assert created["body"]["spec"]["stressors"]["memory"] == {"workers": 1, "size": "500MB"}
    print("OK - StressChaos CR이 selector.pods로 특정 pod에 고정되고(labelSelectors 아님) duration이 실림")


# --- target replacement(공통 규칙 재사용) ----------------------------------

def test_target_replacement_before_effect_is_invalid():
    call_count = {"n": 0}

    def get_active_pods_fn():
        call_count["n"] += 1
        return [TARGET_POD] if call_count["n"] == 1 else [REPLACEMENT_POD]

    calls = {"create": []}
    injector = _make_healthy_injector(
        stages=SLOW_STAGE, get_active_pods_fn=get_active_pods_fn,
        is_stage_injected_fn=lambda cr_name: False,  # 효과가 절대 안 남
        create_chaos_fn=lambda *a, **kw: calls["create"].append(a))
    injector.prepare()
    injector.inject()
    ok, exc = _raises_exception(injector.is_done, (TrialInvalid, HarnessCorrupted))
    assert ok and isinstance(exc, TrialInvalid), exc
    assert "외부 오염" in str(exc)
    assert injector.get_target_replacement() is None
    assert calls["create"] == [], "효과 전 대상 변경은 stage-0 CR도 만들기 전에 걸러야 함"
    print("OK - 효과를 내기 전 대상 교체: TrialInvalid(외부 오염 가능성), stage CR 생성 안 함")


def test_target_replacement_after_effect_is_recorded_not_invalid():
    call_count = {"n": 0}

    def get_active_pods_fn():
        call_count["n"] += 1
        return [TARGET_POD] if call_count["n"] <= 2 else [REPLACEMENT_POD]

    injector = _make_healthy_injector(
        stages=TWO_FAST_STAGES, get_active_pods_fn=get_active_pods_fn,
        is_stage_injected_fn=lambda cr_name: True,
        get_working_set_fn=_rising_working_set_fn())
    injector.prepare()
    injector.inject()
    assert _wait_for(injector.is_started), "stage-0는 정상 진행돼야 함(효과 확정 - AllInjected+working set 상승)"
    assert _wait_for(injector.is_done, timeout=5.0), "대상 교체가 감지되면 is_done은 True(예외 아님)"
    replacement = injector.get_target_replacement()
    assert replacement is not None
    assert replacement["replacement_pod"] == REPLACEMENT_POD
    injector.cleanup()
    print("OK - 효과 후 대상 교체(연쇄장애 맥락): invalid 아님, 기록만 하고 남은 stage 생성 안 함")


# --- cleanup ---------------------------------------------------------------

def test_cleanup_raises_if_residual_cr_remains():
    calls = {"create": [], "delete": []}
    injector = _make_healthy_injector(
        does_chaos_exist_fn=lambda cr_name: True,  # 삭제해도 계속 존재(잔존 finalizer 등)
        create_chaos_fn=lambda *a, **kw: calls["create"].append(a),
        delete_chaos_fn=lambda cr_name: calls["delete"].append(cr_name),
        cleanup_verify_timeout_sec=0.05)
    injector.prepare()  # inject() 없이도(스레드가 아예 안 돎) cleanup이 검증해야 함
    try:
        injector.cleanup()
        assert False, "잔존 CR이 있으면 cleanup()이 예외를 던져야 함"
    except RuntimeError as e:
        assert "남아있는" in str(e)
    assert calls["create"] == [], "inject() 없이는 CR을 만들면 안 됨"
    print("OK - cleanup 후 잔존 CR: 조용히 성공하지 않고 예외로 드러남(CR 삭제·소멸 실패, 즉시 중단 조건)")


def test_cleanup_recovery_check_logs_recovered_true_without_raising():
    log = []
    injector = _make_healthy_injector(get_working_set_fn=lambda name: BASELINE_WS, log=log)
    injector.prepare()
    injector.cleanup()  # inject() 없이도 baseline이 기록돼 있으면 recovery check가 동작해야 함
    rec = next(r for r in log if r["event"] == "cleanup_recovery_check")
    assert rec["recovered"] is True
    assert rec["baseline_working_set_bytes"] == BASELINE_WS
    print("OK - cleanup 후 working set이 baseline 근처면 recovered=True로 evidence에 기록")


def test_cleanup_recovery_check_logs_recovered_false_without_raising():
    # working set이 끝내 baseline으로 안 돌아와도 cleanup() 자체는 예외를 던지지 않는다
    # (느린 페이지 캐시 회수 등 정상적인 지연일 수 있음 - 비차단 관측). prepare()의 baseline
    # 측정은 정상값을 써야 하므로(안 그러면 headroom 게이트에서부터 막힘) cleanup 시점에만
    # 회복 안 된 값으로 바뀌게 한다.
    ws_state = {"v": BASELINE_WS}
    log = []
    injector = _make_healthy_injector(get_working_set_fn=lambda name: ws_state["v"], log=log)
    injector.prepare()
    ws_state["v"] = BASELINE_WS + (2 * GIB)  # cleanup 시점엔 아직 안 돌아온 상태
    injector.cleanup()  # 예외 없이 반환돼야 함
    rec = next(r for r in log if r["event"] == "cleanup_recovery_check")
    assert rec["recovered"] is False
    print("OK - cleanup 후 working set이 복귀 안 해도 cleanup()은 예외를 던지지 않음(evidence에만 기록)")


def test_cleanup_skips_recovery_check_when_prepare_never_ran():
    log = []
    injector = _make_healthy_injector(log=log)
    injector.cleanup()  # prepare() 자체를 안 함 - idempotent해야 함
    assert not any(r["event"] == "cleanup_recovery_check" for r in log)
    print("OK - prepare() 전 cleanup(): baseline이 없으니 recovery check를 건너뜀(예외 없음)")


# --- classify_stage(network_degrade_adapter.py와 동일 순수 로직) -----------

T0 = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)


def _at(sec):
    return (T0 + timedelta(seconds=sec)).isoformat()


def _win(name, start, end):
    return {"name": name, "start": T0 + timedelta(seconds=start),
            "end": None if end is None else T0 + timedelta(seconds=end)}


def test_classify_stage_pure_boundaries():
    windows = [_win("s0", 10, 100), _win("s1", 103, 193)]
    c = lambda sec, w=windows: _classify_timestamp_against_windows(_at(sec), w)
    assert c(0) == "baseline"
    assert c(10) == "s0" and c(100) == "s0", "창의 양 끝은 포함"
    assert c(101.5) == "inter_stage_tail"
    assert c(103) == "s1" and c(150) == "s1"
    assert c(194) == "drain"
    print("OK - classify_stage 순수 함수: baseline/stage(양끝 포함)/inter_stage_tail/drain")


def test_classify_stage_never_raises_and_never_guesses():
    windows = [_win("s0", 10, 100)]
    naive = (T0 + timedelta(seconds=50)).replace(tzinfo=None).isoformat()
    for bad in ("garbage", "", None, naive, 12345):
        assert _classify_timestamp_against_windows(bad, windows) == "unknown", bad
    assert _classify_timestamp_against_windows(_at(50), []) == "unknown"
    assert _classify_timestamp_against_windows(_at(50), None) == "unknown"
    injector = _make_healthy_injector()
    assert injector.classify_stage(_at(0)) == "unknown", "inject() 전(창 없음)에는 추정하지 않음"
    print("OK - classify_stage: 파싱 불가/naive/창 없음은 예외 대신 unknown")


class _StepClock:
    def __init__(self):
        self.n = 0

    def __call__(self):
        t = T0 + timedelta(seconds=10 * self.n)
        self.n += 1
        return t


def test_classify_stage_full_run_uses_recorded_windows():
    injector = _make_healthy_injector(stages=TWO_FAST_STAGES, now_fn=_StepClock())
    injector.prepare()
    injector.inject()
    assert _wait_for(injector.is_done)
    c = injector.classify_stage
    assert c(_at(-5)) == "baseline"
    assert [c(_at(5)), c(_at(25))] == ["stage-1-500mb", "stage-2-1000mb"]
    assert c(_at(15)) == "inter_stage_tail"
    assert c(_at(35)) == "drain"
    injector.cleanup()
    print("OK - 정상 종료: 실제 stage 창으로 baseline/stage/inter_stage_tail/drain 분류")


def test_classify_stage_after_truncation_is_drain_not_missing_stage():
    call_count = {"n": 0}

    def get_active_pods_fn():
        call_count["n"] += 1
        return [TARGET_POD] if call_count["n"] <= 2 else [REPLACEMENT_POD]

    injector = _make_healthy_injector(stages=TWO_FAST_STAGES, get_active_pods_fn=get_active_pods_fn,
                                       get_working_set_fn=_rising_working_set_fn(), now_fn=_StepClock())
    injector.prepare()
    injector.inject()
    assert _wait_for(injector.is_started)
    assert _wait_for(injector.is_done)
    assert injector.get_target_replacement() is not None
    c = injector.classify_stage
    assert c(_at(5)) == "stage-1-500mb"
    assert c(_at(25)) == "drain", "만들어지지 않은 stage-2는 창이 없고 그 뒤는 drain"
    injector.cleanup()
    print("OK - truncation: 만들어진 stage만 창이 있고 그 뒤는 drain(누락 stage로 오분류 안 함)")


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    for t in tests:
        t()
    print(f"전체 통과 ({len(tests)}개)")
