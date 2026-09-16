#!/usr/bin/env python3
"""pod_kill_adapter.py 검증 - 정상 종료/대상 없음/다중 대상/주입 미발생/
cleanup 재호출 5가지를 실클러스터 없이 검증한다. make_pod_kill_injector의
*_fn 의존성 주입 지점에 fake를 꽂아 오프라인으로 확인한다(collect_metrics.py
테스트와 같은 assert+print 스타일)."""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from pod_kill_adapter import make_pod_kill_injector
from run_once import TrialInvalid

RUN_ID = "pod_kill-proposed-01-20260916T120000Z"
ARM = "proposed"


def test_normal_completion():
    calls = {"create": [], "delete": []}
    pod_state = {"uid": "uid-1", "alive": True}

    def get_pod_fn(name):
        return {"name": name, "uid": pod_state["uid"]} if pod_state["alive"] else None

    injector = make_pod_kill_injector(
        RUN_ID, ARM, 1,
        get_active_pods_fn=lambda: [{"name": "vllm-abc123", "uid": "uid-1"}],
        get_pod_fn=get_pod_fn,
        create_chaos_fn=lambda *args: calls["create"].append(args),
        delete_chaos_fn=lambda cr_name: calls["delete"].append(cr_name))

    injector.prepare()
    injector.inject()
    assert len(calls["create"]) == 1
    cr_name, run_id, arm, target_name = calls["create"][0]
    assert run_id == RUN_ID and arm == ARM and target_name == "vllm-abc123"

    assert injector.is_started() is False, "아직 살아있는데 시작됨으로 판정됨"
    assert injector.get_actual_injection_time() is None
    assert injector.is_done() is False

    pod_state["alive"] = False  # 실제 종료 발생
    assert injector.is_started() is True
    t1 = injector.get_actual_injection_time()
    assert t1 is not None
    assert injector.is_done() is True

    assert injector.is_started() is True  # 반복 호출해도 최초 시각 유지
    assert injector.get_actual_injection_time() == t1

    injector.cleanup()
    assert calls["delete"] == [cr_name]
    print("OK - 정상 종료: CR 생성→기존 UID 소멸 감지→t_injection 고정→cleanup")


def test_no_target_found():
    injector = make_pod_kill_injector(RUN_ID, ARM, 1, get_active_pods_fn=lambda: [])
    try:
        injector.prepare()
        assert False, "TrialInvalid가 발생해야 함"
    except TrialInvalid as e:
        assert "없음" in str(e)
    print("OK - 대상 없음: fail-closed로 TrialInvalid")


def test_multiple_targets_found():
    pods = [{"name": "vllm-a", "uid": "uid-a"}, {"name": "vllm-b", "uid": "uid-b"}]
    injector = make_pod_kill_injector(RUN_ID, ARM, 1, get_active_pods_fn=lambda: pods)
    try:
        injector.prepare()
        assert False, "TrialInvalid가 발생해야 함"
    except TrialInvalid as e:
        assert "vllm-a" in str(e) and "vllm-b" in str(e)
    print("OK - 다중 대상: fail-closed로 TrialInvalid, 대상 이름 포함")


def test_injection_never_happens():
    # 대상 pod가 계속 살아있는 상황(주입이 실제 효과를 내지 못함)
    injector = make_pod_kill_injector(
        RUN_ID, ARM, 1,
        get_active_pods_fn=lambda: [{"name": "vllm-abc123", "uid": "uid-1"}],
        get_pod_fn=lambda name: {"name": name, "uid": "uid-1"},
        create_chaos_fn=lambda *args: None)

    injector.prepare()
    injector.inject()
    for _ in range(3):
        assert injector.is_started() is False
    assert injector.get_actual_injection_time() is None
    assert injector.is_done() is False
    print("OK - 주입 미발생: 대상이 안 죽으면 is_started/is_done 계속 False")


def test_cleanup_idempotent_and_safe_without_injection():
    calls = []
    injector = make_pod_kill_injector(
        RUN_ID, ARM, 1, delete_chaos_fn=lambda cr_name: calls.append(cr_name))

    # prepare()/inject()를 한 번도 안 불러도(예외로 조기 중단된 상황) cleanup은
    # 안전해야 하고, 여러 번 불러도 항상 같은 CR 이름을 대상으로 해야 한다.
    injector.cleanup()
    injector.cleanup()
    injector.cleanup()
    assert len(calls) == 3
    assert len(set(calls)) == 1, "매 호출마다 같은 CR 이름을 대상으로 해야 함"
    print("OK - cleanup 재호출: 여러 번 불러도 안전, 항상 동일 CR 대상")


if __name__ == "__main__":
    test_normal_completion()
    test_no_target_found()
    test_multiple_targets_found()
    test_injection_never_happens()
    test_cleanup_idempotent_and_safe_without_injection()
    print("\n모두 통과")
