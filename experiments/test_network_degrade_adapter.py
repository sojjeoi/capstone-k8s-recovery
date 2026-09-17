#!/usr/bin/env python3
"""network_degrade_adapter.py 검증 - 정상 종료/대상 없음/다중 대상/주입
효과 미관측/cleanup 중도 중단/주입 도중 대상 pod 변경/단계 소멸 미확인/
cleanup 후 잔존 CR 8가지를 실클러스터 없이 검증한다(test_pod_kill_adapter.py와
같은 assert+print 스타일). 백그라운드 스레드가 실제로 도는 부분만 테스트용
초단기 duration_sec/timeout으로 오버라이드한다.

get_pod_fn은 모든 테스트가 명시적으로 넘긴다(기본값은 실클러스터를 두드리는
active_pod_resolver.get_pod라, 안 넘기면 오프라인 테스트가 깨진다) -
2026-09-18 리뷰에서 UID 재확인 로직을 추가하면서 필수가 됐다."""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from network_degrade_adapter import make_network_degrade_injector
from run_once import TrialInvalid

RUN_ID = "network_degrade-proposed-01-20260918T120000Z"
ARM = "proposed"
TARGET_POD = {"name": "vllm-abc123", "uid": "uid-1"}

FAST_STAGES = [
    {"name": "s0", "latency": "500ms", "jitter": "50ms", "duration_sec": 0.05},
    {"name": "s1", "latency": "1000ms", "jitter": "100ms", "duration_sec": 0.05},
    {"name": "s2", "latency": "2000ms", "jitter": "200ms", "duration_sec": 0.05},
    {"name": "s3", "latency": "4000ms", "jitter": "400ms", "duration_sec": 0.05},
]


def _wait_for(check, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(interval)
    return False


def _unchanged_get_pod_fn(name):
    return dict(TARGET_POD)


def test_normal_completion():
    calls = {"create": [], "delete": []}
    injected = {"v": False}  # is_stage_injected_fn이 첫 poll엔 False, 이후 True
    existing = set()  # 생성됐고 아직 delete_chaos_fn이 안 불린 CR 이름들

    def is_stage_injected_fn(cr_name):
        was = injected["v"]
        injected["v"] = True
        return was

    def create_chaos_fn(*args):
        calls["create"].append(args)
        existing.add(args[0])

    def delete_chaos_fn(cr_name):
        calls["delete"].append(cr_name)
        existing.discard(cr_name)

    injector = make_network_degrade_injector(
        RUN_ID, ARM, 1,
        get_active_pods_fn=lambda: [TARGET_POD],
        get_pod_fn=_unchanged_get_pod_fn,
        is_stage_injected_fn=is_stage_injected_fn,
        does_chaos_exist_fn=lambda cr_name: cr_name in existing,
        create_chaos_fn=create_chaos_fn,
        delete_chaos_fn=delete_chaos_fn,
        stages=FAST_STAGES,
        stage_delete_poll_interval_sec=0.01)

    injector.prepare()
    injector.inject()

    assert _wait_for(lambda: len(calls["create"]) >= 1), "stage-1 CR이 생성돼야 함"
    cr_name, run_id, arm, target_name, stage = calls["create"][0]
    assert run_id == RUN_ID and arm == ARM and target_name == TARGET_POD["name"]
    assert stage["name"] == "s0"

    assert _wait_for(injector.is_started), "is_stage_injected_fn이 True를 준 뒤엔 is_started여야 함"
    assert injector.is_effective() is True
    t1 = injector.get_actual_injection_time()
    assert t1 is not None
    err1 = injector.get_injection_observation_error_sec()
    assert err1 is not None and err1 >= 0, "미적용->적용 관측 오차가 나와야 함"
    assert injector.get_last_seen_present_time() is not None

    assert injector.is_started() is True  # latch 유지
    assert injector.get_actual_injection_time() == t1

    assert _wait_for(injector.is_done, timeout=5.0), "4단계 모두 짧게 끝나면 is_done이 True여야 함"
    assert len(calls["create"]) == 4, "4단계 전부 생성돼야 함"
    assert [c[4]["name"] for c in calls["create"]] == ["s0", "s1", "s2", "s3"], "순서대로 진행돼야 함"
    assert len(calls["delete"]) == 4, "각 단계는 다음 단계 전에 삭제까지 확인돼야 함"

    injector.cleanup()
    assert set(calls["delete"]) == {cr_name for cr_name, *_ in calls["create"]}
    print("OK - 정상 종료: 4단계 순차 주입(단계별 소멸 확인 포함)->첫 적용 관측 시 t_injection 고정->전체 완료->cleanup")


def test_no_target_found():
    injector = make_network_degrade_injector(RUN_ID, ARM, 1, get_active_pods_fn=lambda: [])
    try:
        injector.prepare()
        assert False, "TrialInvalid가 발생해야 함"
    except TrialInvalid as e:
        assert "없음" in str(e)
    print("OK - 대상 없음: fail-closed로 TrialInvalid")


def test_multiple_targets_found():
    pods = [{"name": "vllm-a", "uid": "uid-a"}, {"name": "vllm-b", "uid": "uid-b"}]
    injector = make_network_degrade_injector(RUN_ID, ARM, 1, get_active_pods_fn=lambda: pods)
    try:
        injector.prepare()
        assert False, "TrialInvalid가 발생해야 함"
    except TrialInvalid as e:
        assert "vllm-a" in str(e) and "vllm-b" in str(e)
    print("OK - 다중 대상: fail-closed로 TrialInvalid, 대상 이름 포함")


def test_injection_never_effective():
    # CR은 계속 생성되지만 status가 끝까지 AllInjected를 안 준 상황
    # (예: chaos-daemon 문제로 tc 규칙이 실제로 안 걸림).
    calls = {"create": []}
    injector = make_network_degrade_injector(
        RUN_ID, ARM, 1,
        get_active_pods_fn=lambda: [TARGET_POD],
        get_pod_fn=_unchanged_get_pod_fn,
        is_stage_injected_fn=lambda cr_name: False,
        does_chaos_exist_fn=lambda cr_name: False,  # delete가 항상 즉시 반영된다고 가정(이 테스트의 관심사 아님)
        create_chaos_fn=lambda *args: calls["create"].append(args),
        stages=FAST_STAGES,
        stage_delete_poll_interval_sec=0.01)

    injector.prepare()
    injector.inject()
    assert _wait_for(lambda: len(calls["create"]) >= 1), "stage-1 CR이 생성돼야 is_started가 뭔가 확인할 수 있음"
    # is_started() 자체가 last_seen_not_injected_at을 갱신하는 부수효과를 가진다
    # (pod_kill_adapter.py의 is_started()와 동일 패턴) - 그래서 getter가 아니라
    # is_started()를 직접 반복 호출해서 관측을 발생시켜야 한다.
    for _ in range(3):
        assert injector.is_started() is False
    assert injector.get_last_seen_present_time() is not None, "False 관측이 최소 1회는 있었어야 함"
    assert injector.get_actual_injection_time() is None
    assert injector.get_injection_observation_error_sec() is None
    # 단계 자체는 효과 관측과 무관하게 타이머로 끝까지 진행된다(run_once.py가
    # is_started 미확인 시 이미 injection_valid=False로 TrialInvalid 처리하므로
    # 이 상태로 OBSERVING까지 가는 실제 경로는 없지만, 어댑터 자체는 안전해야 함).
    assert _wait_for(injector.is_done, timeout=5.0)
    print("OK - 주입 효과 미관측: 계속 False 유지, 단계는 타이머로 끝까지 진행")


def test_cleanup_stops_background_thread_mid_stage():
    calls = {"create": [], "delete": []}
    existing = set()
    slow_stages = [
        {"name": "s0", "latency": "500ms", "jitter": "50ms", "duration_sec": 5.0},
        {"name": "s1", "latency": "1000ms", "jitter": "100ms", "duration_sec": 0.05},
    ]

    def create_chaos_fn(*args):
        calls["create"].append(args)
        existing.add(args[0])

    def delete_chaos_fn(cr_name):
        calls["delete"].append(cr_name)
        existing.discard(cr_name)

    injector = make_network_degrade_injector(
        RUN_ID, ARM, 1,
        get_active_pods_fn=lambda: [TARGET_POD],
        get_pod_fn=_unchanged_get_pod_fn,
        is_stage_injected_fn=lambda cr_name: True,
        does_chaos_exist_fn=lambda cr_name: cr_name in existing,
        create_chaos_fn=create_chaos_fn,
        delete_chaos_fn=delete_chaos_fn,
        stages=slow_stages,
        stage_delete_poll_interval_sec=0.01)

    injector.prepare()
    injector.inject()
    assert _wait_for(lambda: len(calls["create"]) >= 1)
    assert _wait_for(injector.is_started)

    injector.cleanup()  # stage-1(5초 sleep) 도중 중단
    assert len(calls["create"]) == 1, "cleanup이 stage-2 생성 전에 스레드를 멈춰야 함"
    assert injector.is_done() is False, "중도 중단이면 완료가 아니어야 함"
    assert len(calls["delete"]) >= 1, "cleanup은 진행중이던 CR을 정리해야 함"
    print("OK - cleanup 중도 중단: 다음 단계로 안 넘어가고 즉시 멈춤, 남은 CR 정리")


def test_uid_change_mid_injection_raises_invalid():
    # stage-0은 원래 pod로 정상 진행되지만, stage-1로 넘어가기 직전 재확인에서
    # active pod의 UID가 바뀐 것으로 관측되는 상황(예: BlueGreen 전환).
    existing = set()
    call_count = {"n": 0}

    def get_pod_fn(name):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return dict(TARGET_POD)  # stage-0 진입 전 첫 확인은 그대로
        return {"name": name, "uid": "uid-DIFFERENT"}  # 그 이후엔 바뀜

    def create_chaos_fn(*args):
        existing.add(args[0])

    def delete_chaos_fn(cr_name):
        existing.discard(cr_name)

    injector = make_network_degrade_injector(
        RUN_ID, ARM, 1,
        get_active_pods_fn=lambda: [TARGET_POD],
        get_pod_fn=get_pod_fn,
        is_stage_injected_fn=lambda cr_name: True,  # stage-0가 바로 효과 관측되게
        does_chaos_exist_fn=lambda cr_name: cr_name in existing,
        create_chaos_fn=create_chaos_fn,
        delete_chaos_fn=delete_chaos_fn,
        stages=FAST_STAGES,
        stage_delete_poll_interval_sec=0.01)

    injector.prepare()
    injector.inject()
    assert _wait_for(injector.is_started), "stage-0는 정상 진행돼야 함"

    def raised():
        try:
            injector.is_done()
        except TrialInvalid as e:
            raised.exc = e
            return True
        return False
    raised.exc = None

    assert _wait_for(raised, timeout=5.0), "UID 변경이 is_done()을 통해 TrialInvalid로 드러나야 함"
    assert "바뀜" in str(raised.exc)
    injector.cleanup()  # 남은 CR 정리(idempotent 확인 겸)
    print("OK - 주입 도중 대상 pod 변경: 다음 단계로 안 넘어가고 TrialInvalid, invalid_run 경로로 이어짐")


def test_stage_deletion_not_confirmed_raises():
    # delete_chaos_fn은 불리지만 does_chaos_exist_fn이 절대 False를 안 줌
    # (예: Chaos Mesh finalizer가 걸려 실제 소멸이 안 되는 상황).
    injector = make_network_degrade_injector(
        RUN_ID, ARM, 1,
        get_active_pods_fn=lambda: [TARGET_POD],
        get_pod_fn=_unchanged_get_pod_fn,
        is_stage_injected_fn=lambda cr_name: True,
        does_chaos_exist_fn=lambda cr_name: True,  # 절대 안 사라짐
        stages=FAST_STAGES,
        stage_delete_poll_interval_sec=0.01,
        stage_recovery_timeout_sec=0.05)

    injector.prepare()
    injector.inject()
    assert _wait_for(injector.is_started)

    def raised():
        try:
            injector.is_done()
        except TrialInvalid as e:
            raised.exc = e
            return True
        return False
    raised.exc = None

    assert _wait_for(raised, timeout=5.0), "단계 소멸 시간초과가 TrialInvalid로 드러나야 함"
    assert "소멸" in str(raised.exc) or "복구" in str(raised.exc)
    print("OK - 단계 소멸 미확인: 다음 단계로 안 넘어가고 시간초과로 TrialInvalid")


def test_cleanup_raises_if_residual_cr_remains():
    # delete_chaos_fn을 불러도 does_chaos_exist_fn이 계속 True(예: 잔존 finalizer).
    injector = make_network_degrade_injector(
        RUN_ID, ARM, 1,
        get_active_pods_fn=lambda: [TARGET_POD],
        get_pod_fn=_unchanged_get_pod_fn,
        does_chaos_exist_fn=lambda cr_name: True,
        stages=FAST_STAGES,
        stage_delete_poll_interval_sec=0.01,
        cleanup_verify_timeout_sec=0.05)

    injector.prepare()  # inject() 없이도(스레드가 아예 안 돎) cleanup이 검증해야 함
    try:
        injector.cleanup()
        assert False, "잔존 CR이 있으면 cleanup()이 예외를 던져야 함"
    except RuntimeError as e:
        assert "남아있는" in str(e)
    print("OK - cleanup 후 잔존 CR: 조용히 성공하지 않고 예외로 드러남")


if __name__ == "__main__":
    test_normal_completion()
    test_no_target_found()
    test_multiple_targets_found()
    test_injection_never_effective()
    test_cleanup_stops_background_thread_mid_stage()
    test_uid_change_mid_injection_raises_invalid()
    test_stage_deletion_not_confirmed_raises()
    test_cleanup_raises_if_residual_cr_remains()
    print("\n모두 통과")
