#!/usr/bin/env python3
"""memory_pressure "direct"(급격한 단일 점프) 후보의 3회 재현성 검증
(2026-09-20). §54의 progressive 후보(500->1000->1500MB)가 §55에서
재현성 FAIL로 판정된 뒤의 지시 - "direct" 후보는 progressive 선행 압박
없이 **정상 baseline에서 곧장 1500MB×120초로** 주입한다(§53에서 이미
이 정확한 조건으로 1회 sustained 위반을 관측한 조건 그대로). 사전 등록:
docs/design/phase8-blue-green-preflight-incident.md §56 - 이 파일의
동작이 그 절의 규칙과 어긋나면 문서가 맞다(측정 뒤 규칙을 사후 조정하지
않는다는 원칙, §42/§50/§52/§54와 동일).

`explore_memory_pressure_intensity.run_round()`를 그대로 재사용한다(§53이
이미 `run_round(1500.0, stage_duration_sec=120.0)` 그 자체였다) - 새 어댑터
로직·새 주입 경로를 만들지 않는다. `verify_ramp_candidate.py`가
`explore_ramp_intensity.run_candidate()`를 감싸는 것과 완전히 같은 패턴으로,
이 스크립트는 run_round()를 3회 반복 + cooldown/quiescence 확인 + §56
기준 판정만 얹는다. `verify_memory_pressure_candidate.py`(progressive
후보용)의 quiescence 확인 헬퍼를 그대로 재사용한다(중복 구현 없음).

`run_memory_pressure_trial.py`(smoke 전용, 1GB 이상 차단)와
`explore_memory_pressure_intensity.py`(§50/§52 calibration 도구)의 동작은
전혀 건드리지 않는다 - 완전히 별도 경로. 산출물은 `results/`(top-level)
아래 run_round()가 이미 쓰는 명명 규칙(`explore-memory_pressure-native-
1500mb-120s-{timestamp}-summary.json`)을 그대로 따른다 - `collect_metrics.py`는
`trial-*.json`만 glob하므로 본 실험 분석에 절대 섞이지 않는다."""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from explore_memory_pressure_intensity import (
    DEFAULT_PROBE_CONFIG,
    MAX_TARGET_WORKING_SET_BYTES,
    MIN_NODE_AVAILABLE_PASS_BYTES,
    PROBE_RESULTS_DIR,
    analyze_slo,
    node_healthy,
    run_round,
)
from verify_memory_pressure_candidate import check_cluster_quiescent, wait_for_quiescence

RESULTS_DIR = Path(__file__).parent / "results"

# 사전 등록(§56.1) - direct 후보 구성. CLI로 바꿀 수 없다(다른 강도/시간을
# 시험하려면 §50/§52의 calibration 경로를 다시 거쳐야 한다).
DIRECT_SIZE_MB = 1500.0
DIRECT_STAGE_DURATION_SEC = 120.0
DIRECT_WORKERS = 1
REPETITIONS = 3
COOLDOWN_SEC = 120.0  # §54와 동일 근거(반복 사이 완전 cleanup·baseline 복귀 확인 후 대기)
STAGE_TAIL_MARGIN_SEC = 30.0  # analyze_slo(upper_bound_iso=...) 여유 - LATENCY_PERSIST_SEC(30초)와 동일 크기


def judge_direct_safety(result: dict) -> dict:
    """§56.2의 10개 안전 기준을 이 1회 실행에 전부 적용한다 - 순수 함수
    (result dict만 읽음, 오프라인 테스트 대상). run_round()의 judge_pass()와
    같은 스타일이지만 completion 성공률과 target UID 불변을 명시적으로
    추가 확인한다(judge_pass()는 이 둘을 직접 확인하지 않았음)."""
    if result.get("aborted"):
        return {"pass": False, "reasons": [f"중단됨: {result.get('abort_reason')}"]}

    reasons = []
    if not result.get("all_injected_confirmed"):
        reasons.append("AllInjected 확인 안 됨")

    slo = result.get("slo", {})
    if slo.get("error") or slo.get("success_rate") != 1.0:
        reasons.append(f"completion 성공률 100% 아님: {slo.get('success_rate')}, error={slo.get('error')}")

    ticks = result.get("ticks", [])
    ws_values = [t["working_set_bytes"] for t in ticks if t.get("working_set_bytes") is not None]
    if any(ws >= MAX_TARGET_WORKING_SET_BYTES for ws in ws_values):
        reasons.append("working set이 5GiB 이상인 시점 존재")

    avail_values = [t["node_available_bytes"] for t in ticks if t.get("node_available_bytes") is not None]
    if any(a < MIN_NODE_AVAILABLE_PASS_BYTES for a in avail_values):
        reasons.append("Node MemAvailable이 4GiB 미만인 시점 존재")

    restart_values = {t["restart_count"] for t in ticks if t.get("restart_count") is not None}
    if len(restart_values) > 1:
        reasons.append(f"restartCount 변화 관측: {sorted(restart_values)}")

    if any(t.get("oom_killed") for t in ticks):
        reasons.append("OOMKilled 관측")

    node_issues = [t for t in ticks if t.get("node_conditions") and not node_healthy(t["node_conditions"])]
    if node_issues:
        reasons.append(f"Node 상태 이상 {len(node_issues)}건")

    if result.get("target_replacement") is not None:
        reasons.append(f"target UID 변경 관측: {result['target_replacement']}")

    recovery_checks = [t for t in ticks if t.get("event") == "cleanup_recovery_check"]
    if not recovery_checks or not recovery_checks[-1].get("recovered"):
        reasons.append("cleanup 후 30초 내 baseline 복귀 확인 안 됨")

    if not result.get("cleanup_confirmed", True):
        reasons.append("CR·observer·context 완전 정리 확인 안 됨")

    return {"pass": len(reasons) == 0, "reasons": reasons}


def compute_scoped_slo(result: dict) -> dict:
    """§56.3 - stage 경계(§53/§55와 같은 이유로 명목이 아니라 실제
    t_injection~t_injection_end)로 좁힌 SLO 분석. run_round() 자신의
    `result["slo"]`는 §50~§53 호출부와 같은 계산(upper_bound 없음)을
    그대로 유지해야 하므로(사후 재정의 금지), 이 스코핑은 별도로 다시
    계산한다 - run_round() 내부는 손대지 않는다."""
    if result.get("aborted") or not result.get("t_injection") or not result.get("t_injection_end"):
        return {"error": "주입 시각 미확보(중단됨 등) - 스코핑 계산 불가"}
    local_raw = PROBE_RESULTS_DIR / f"probe-{result['run_id']}-native-1-raw.csv"
    return analyze_slo(local_raw, result["t_injection"],
                        upper_bound_iso=result["t_injection_end"], upper_margin_sec=STAGE_TAIL_MARGIN_SEC)


def stage_violates(result: dict) -> bool:
    """§56.4 - sustained 위반 판정. t_slo not None(20표본 이상 evaluable한
    rolling P95의 30초 연속 위반 또는 즉시 availability 위반 - slo_judge의
    기존 정의 그대로, 새 판정 로직 없음) AND 그 t_slo가 실제 이 stage
    경계(+30초 여유) 안에 있어야 한다(다음 관측 구간으로 새어나간 사건을
    이 stage의 위반으로 잘못 세지 않기 위함, §55.2와 같은 원칙)."""
    scoped = result.get("scoped_slo", {})
    if "error" in scoped:
        return False
    return scoped.get("t_slo") is not None and scoped.get("t_slo_within_window") is True


def judge_direct_reproducibility(repetitions: list) -> dict:
    """§56.4 SLO 재현성 기준 - 3회 중 최소 2회 sustained 위반 + 안전 기준
    3회 전부 PASS."""
    safety_pass = [r.get("safety", {}).get("pass") for r in repetitions]
    checks = {"enough_repetitions": len(repetitions) >= REPETITIONS,
              "all_reps_safety_pass": len(repetitions) >= REPETITIONS and all(safety_pass)}
    flags = [stage_violates(r) for r in repetitions]
    violate_count = sum(1 for f in flags if f)
    checks[f"violates_at_least_2_of_{REPETITIONS}"] = len(flags) >= REPETITIONS and violate_count >= 2
    return {"overall_pass": all(checks.values()), "checks": checks, "num_repetitions": len(repetitions),
            "violate_count": violate_count}


def print_repetition(result: dict, scoped_slo: dict, safety: dict) -> None:
    print(f"\n=== rep {result.get('rep_index')} 결과(run_id={result['run_id']}) ===")
    if result["aborted"]:
        print(f"중단됨: {result['abort_reason']}")
        return
    print(f"  max_working_set_bytes={result.get('max_working_set_bytes')} "
          f"rise={result.get('working_set_rise_bytes')}")
    print(f"  scoped_slo: t_slo={scoped_slo.get('t_slo')} t_recovery={scoped_slo.get('t_recovery')} "
          f"within_window={scoped_slo.get('t_slo_within_window')} p95_peak={scoped_slo.get('p95_peak')} "
          f"evaluable={scoped_slo.get('post_injection_evaluable_samples')}")
    print(f"  target_replacement={result.get('target_replacement')}")
    for reason in safety["reasons"]:
        print(f"  - SAFETY FAIL: {reason}")
    print(f"  safety.pass={safety['pass']}")


def main():
    parser = argparse.ArgumentParser(
        description="memory_pressure direct 후보(1500MB x 120초, 단일 점프) 3회 재현성 검증(§56)")
    parser.add_argument("--probe-config", default=str(DEFAULT_PROBE_CONFIG))
    parser.add_argument("--repetitions", type=int, default=REPETITIONS)
    parser.add_argument("--cooldown-sec", type=float, default=COOLDOWN_SEC)
    args = parser.parse_args()

    from active_pod_resolver import get_active_pods
    from memory_pressure_adapter import get_pod_details

    pods = get_active_pods()
    if len(pods) != 1:
        print(f"시작 전 active pod이 1개가 아님({len(pods)}개) - 중단", file=sys.stderr)
        sys.exit(2)
    vllm_pod = pods[0]["name"]
    details0 = get_pod_details(vllm_pod)
    node_name = details0["node_name"]
    baseline_restart_count = details0["restart_count"]

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    repetitions = []
    for i in range(1, args.repetitions + 1):
        print(f"\n{'=' * 20} 반복 {i}/{args.repetitions} {'=' * 20}")
        status_before = check_cluster_quiescent(vllm_pod, node_name, baseline_restart_count)
        if not status_before["node_ok"] or not status_before["restart_unchanged"]:
            print(f"시작 전 클러스터가 정상이 아님(node_ok={status_before['node_ok']}, "
                  f"restart_unchanged={status_before['restart_unchanged']}) - 중단", file=sys.stderr)
            break

        result = run_round(DIRECT_SIZE_MB, DIRECT_WORKERS, args.probe_config,
                            stage_duration_sec=DIRECT_STAGE_DURATION_SEC, min_headroom_bytes=0.0)
        result["rep_index"] = i
        scoped_slo = compute_scoped_slo(result)
        result["scoped_slo"] = scoped_slo
        safety = judge_direct_safety(result)
        result["safety"] = safety
        print_repetition(result, scoped_slo, safety)

        out_path = RESULTS_DIR / f"{result['run_id']}-direct-verify-summary.json"
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        print(f"요약 저장: {out_path}")
        repetitions.append(result)

        if not safety["pass"]:
            print("이 회차가 안전 기준을 충족하지 못함 - 이후 반복을 실행하지 않는다(지시)", file=sys.stderr)
            break

        if i < args.repetitions:
            wait_for_quiescence(vllm_pod, node_name, baseline_restart_count, args.cooldown_sec)

    verdict = judge_direct_reproducibility(repetitions)
    print(f"\n{'=' * 20} 최종 판정 {'=' * 20}")
    for k, v in verdict["checks"].items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    print(f"\n종합: {'PASS' if verdict['overall_pass'] else 'FAIL'} (위반 {verdict['violate_count']}/{verdict['num_repetitions']}회)")

    verdict_path = RESULTS_DIR / f"verify-memory_pressure-direct-candidate-verdict-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    verdict_path.write_text(json.dumps({"verdict": verdict, "run_ids": [r["run_id"] for r in repetitions]},
                                        indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"판정 저장: {verdict_path}")
    return repetitions, verdict


if __name__ == "__main__":
    main()
