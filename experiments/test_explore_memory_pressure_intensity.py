#!/usr/bin/env python3
"""explore_memory_pressure_intensity.py 검증 - 순수 함수만 대상(§50.4 PASS
판정·§50.6 SLO 분석·이벤트 diff). `run_round()`의 라이브 오케스트레이션
자체는 explore_ramp_intensity.py의 run_candidate()와 같은 이유로 오프라인
테스트 대상이 아니다(실클러스터·kubectl exec·probe pod 의존)."""
import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")

from explore_memory_pressure_intensity import (
    GIB,
    MAX_TARGET_WORKING_SET_BYTES,
    MIB,
    MIN_NODE_AVAILABLE_PASS_BYTES,
    analyze_slo,
    diff_unhealthy_events,
    judge_pass,
    node_healthy,
    run_round,
)

HEALTHY = {"Ready": "True", "MemoryPressure": "False", "DiskPressure": "False", "PIDPressure": "False"}
NOT_READY = {"Ready": "False", "MemoryPressure": "False", "DiskPressure": "False", "PIDPressure": "False"}


def _clean_result(size_mb=1000.0, rise_bytes=None):
    """§50.4의 9개 조건을 전부 충족하는 최소 result dict."""
    rise = rise_bytes if rise_bytes is not None else size_mb * MIB * 0.9  # 요청량의 90%(80% 기준 여유 있게 충족)
    return {
        "size_mb": size_mb,
        "aborted": False,
        "all_injected_confirmed": True,
        "working_set_rise_bytes": rise,
        "cleanup_confirmed": True,
        "ticks": [
            {"working_set_bytes": 3.5 * GIB, "node_available_bytes": 8 * GIB,
             "restart_count": 0, "oom_killed": False, "node_conditions": dict(HEALTHY)},
            {"working_set_bytes": 3.5 * GIB + rise, "node_available_bytes": 7.5 * GIB,
             "restart_count": 0, "oom_killed": False, "node_conditions": dict(HEALTHY)},
            {"event": "cleanup_recovery_check", "recovered": True},
        ],
    }


# --- node_healthy ----------------------------------------------------------

def test_node_healthy_true_when_all_conditions_ok():
    assert node_healthy(HEALTHY) is True


def test_node_healthy_false_when_not_ready():
    assert node_healthy(NOT_READY) is False


def test_node_healthy_false_when_pressure_present():
    assert node_healthy({**HEALTHY, "MemoryPressure": "True"}) is False


# --- diff_unhealthy_events ---------------------------------------------------

def test_diff_unhealthy_events_counts_only_new_occurrences():
    before = {"uid-1": {"kind": "Readiness", "count": 2}}
    after = {"uid-1": {"kind": "Readiness", "count": 5}, "uid-2": {"kind": "Liveness", "count": 1}}
    diff = diff_unhealthy_events(before, after)
    assert diff == {"Readiness": 3, "Liveness": 1}
    print("OK - 기존 이벤트는 count 증가분만, 새 이벤트는 전체를 더함")


def test_diff_unhealthy_events_no_change_is_zero():
    snap = {"uid-1": {"kind": "Readiness", "count": 4}}
    assert diff_unhealthy_events(snap, snap) == {"Readiness": 0, "Liveness": 0}
    print("OK - 변화 없으면 0")


# --- judge_pass --------------------------------------------------------------

def test_judge_pass_all_criteria_met():
    verdict = judge_pass(_clean_result())
    assert verdict == {"pass": True, "reasons": []}
    print("OK - 9개 조건 전부 충족 시 pass=True, reasons 없음")


def test_judge_pass_aborted_short_circuits():
    r = _clean_result()
    r["aborted"] = True
    r["abort_reason"] = "Node MemAvailable 부족"
    verdict = judge_pass(r)
    assert verdict["pass"] is False
    assert "Node MemAvailable 부족" in verdict["reasons"][0]
    print("OK - aborted면 다른 조건 계산 없이 즉시 실패")


def test_judge_pass_fails_on_insufficient_rise():
    r = _clean_result(size_mb=1000.0, rise_bytes=1000 * MIB * 0.5)  # 요청량의 50%(<80%)
    verdict = judge_pass(r)
    assert verdict["pass"] is False
    assert any("상승 부족" in reason for reason in verdict["reasons"])
    print("OK - working set 상승이 요청량의 80% 미만이면 실패")


def test_judge_pass_fails_on_working_set_at_or_above_ceiling():
    r = _clean_result()
    r["ticks"][1]["working_set_bytes"] = MAX_TARGET_WORKING_SET_BYTES
    verdict = judge_pass(r)
    assert verdict["pass"] is False
    assert any("5GiB 이상" in reason for reason in verdict["reasons"])
    print("OK - working set이 5GiB 이상인 tick이 있으면 실패")


def test_judge_pass_fails_on_low_node_available():
    r = _clean_result()
    r["ticks"][1]["node_available_bytes"] = MIN_NODE_AVAILABLE_PASS_BYTES - 1
    verdict = judge_pass(r)
    assert verdict["pass"] is False
    assert any("4GiB 미만" in reason for reason in verdict["reasons"])
    print("OK - Node MemAvailable이 4GiB 미만인 tick이 있으면 실패(즉시중단 3GiB보다 엄격)")


def test_judge_pass_fails_on_restart_count_change():
    r = _clean_result()
    r["ticks"][1]["restart_count"] = 1
    verdict = judge_pass(r)
    assert verdict["pass"] is False
    assert any("restartCount" in reason for reason in verdict["reasons"])
    print("OK - restartCount가 tick 사이에 바뀌면 실패")


def test_judge_pass_fails_on_oom_killed():
    r = _clean_result()
    r["ticks"][1]["oom_killed"] = True
    verdict = judge_pass(r)
    assert verdict["pass"] is False
    assert any("OOMKilled" in reason for reason in verdict["reasons"])
    print("OK - OOMKilled 관측 시 실패")


def test_judge_pass_fails_on_node_unhealthy():
    r = _clean_result()
    r["ticks"][1]["node_conditions"] = dict(NOT_READY)
    verdict = judge_pass(r)
    assert verdict["pass"] is False
    assert any("Node 상태 이상" in reason for reason in verdict["reasons"])
    print("OK - Node 상태 이상 tick이 있으면 실패")


def test_judge_pass_fails_when_recovery_check_missing():
    r = _clean_result()
    r["ticks"] = [t for t in r["ticks"] if t.get("event") != "cleanup_recovery_check"]
    verdict = judge_pass(r)
    assert verdict["pass"] is False
    assert any("30초" in reason for reason in verdict["reasons"])
    print("OK - cleanup_recovery_check 기록이 없으면 실패")


def test_judge_pass_fails_when_recovery_check_not_recovered():
    r = _clean_result()
    r["ticks"][-1]["recovered"] = False
    verdict = judge_pass(r)
    assert verdict["pass"] is False
    assert any("30초" in reason for reason in verdict["reasons"])
    print("OK - cleanup_recovery_check.recovered=False면 실패")


def test_judge_pass_fails_when_cleanup_not_confirmed():
    r = _clean_result()
    r["cleanup_confirmed"] = False
    verdict = judge_pass(r)
    assert verdict["pass"] is False
    assert any("CR·observer" in reason for reason in verdict["reasons"])
    print("OK - CR·observer 정리 확인 안 되면 실패")


def test_judge_pass_fails_when_all_injected_not_confirmed():
    r = _clean_result()
    r["all_injected_confirmed"] = False
    verdict = judge_pass(r)
    assert verdict["pass"] is False
    assert any("AllInjected" in reason for reason in verdict["reasons"])
    print("OK - AllInjected 미확인이면 실패")


def test_judge_pass_accumulates_multiple_reasons():
    r = _clean_result()
    r["ticks"][1]["oom_killed"] = True
    r["ticks"][1]["restart_count"] = 1
    verdict = judge_pass(r)
    assert verdict["pass"] is False
    assert len(verdict["reasons"]) >= 2
    print("OK - 여러 조건이 동시에 실패하면 전부 reasons에 누적")


# --- analyze_slo -------------------------------------------------------------

def test_analyze_slo_missing_file_returns_error(tmp_path):
    result = analyze_slo(tmp_path / "nope.csv", None)
    assert "error" in result
    print("OK - raw CSV가 없으면 error 필드로 명시(추정 안 함)")


def test_analyze_slo_empty_file_returns_error(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("sent_at,latency,success\n", encoding="utf-8")
    result = analyze_slo(path, None)
    assert "error" in result
    print("OK - 표본이 0개면 error 필드로 명시")


def test_analyze_slo_computes_success_rate_and_sample_count(tmp_path):
    path = tmp_path / "raw.csv"
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    lines = ["sent_at,latency,success"]
    for i in range(25):
        t = t0.replace(second=i % 60)
        lines.append(f"{t.isoformat()},0.1,True")
    lines.append(f"{t0.replace(second=25).isoformat()},0.1,False")
    path.write_text("\n".join(lines), encoding="utf-8")
    result = analyze_slo(path, None)
    assert "error" not in result
    assert result["total_samples"] == 26
    assert abs(result["success_rate"] - 25 / 26) < 1e-9
    print("OK - 성공률·표본 수를 raw CSV에서 정확히 계산")


# --- run_round의 사전 등록 값 검증(오프라인 - 클러스터 접근 전에 즉시 거부) ---

def test_run_round_rejects_disallowed_size_before_touching_cluster():
    try:
        run_round(2000.0)
        assert False, "2000MB는 거부돼야 함(§50.1, 5GiB 안전 상한과 충돌)"
    except ValueError as e:
        assert "2000" in str(e)
    print("OK - 2000MB(및 그 밖의 미등록 값)는 클러스터 접근 전에 즉시 ValueError")


def test_run_round_rejects_arbitrary_unregistered_size():
    try:
        run_round(1200.0)
        assert False, "사전 등록 안 된 값은 거부돼야 함"
    except ValueError:
        pass
    print("OK - 1000/1500 외의 임의 값도 거부(사전 등록된 값만 허용)")


if __name__ == "__main__":
    import inspect
    import tempfile
    from pathlib import Path

    tests = [(name, obj) for name, obj in list(globals().items())
             if name.startswith("test_") and callable(obj)]
    for name, t in tests:
        if "tmp_path" in inspect.signature(t).parameters:
            with tempfile.TemporaryDirectory() as d:
                t(Path(d))
        else:
            t()
    print(f"전체 통과 ({len(tests)}개)")
