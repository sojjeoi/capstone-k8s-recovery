#!/usr/bin/env python3
"""memory_pressure negative-control 최종 후보(1000MB×120초, direct 단일
점프)의 3회 재현성 검증(2026-09-22). §56/§57이 1500MB direct를 검증한
것과 완전히 같은 패턴이지만 강도와 판정 방향이 다르다 - §56/§57은
"sustained SLO 위반이 2/3 이상 재현되는가"(위반이 목표 신호)를 물었고,
이 스크립트는 "sustained SLO 위반이 3/3 전부 재현되지 **않는가**"(위반의
부재가 목표 신호, negative control 자격)를 묻는다. 사전 등록:
docs/design/phase8-blue-green-preflight-incident.md §94 - 이 파일의
동작이 그 절의 규칙과 어긋나면 문서가 맞다(측정 뒤 규칙을 사후 조정하지
않는다는 원칙, §42/§50/§52/§54/§56과 동일).

`explore_memory_pressure_intensity.run_round()`를 그대로 재사용한다
(1000.0은 이미 ALLOWED_SIZES_MB에 포함돼 있어 run_round()의 fail-closed
게이트를 그대로 통과함, 코드 변경 없음) - 새 어댑터·새 주입 경로를
만들지 않는다. `verify_memory_pressure_direct_candidate.py`의
`judge_direct_safety()`를 그대로 재사용하고(안전 기준은 동일), SLO
판정 방향만 반대인 새 함수(`judge_negative_control_reproducibility()`)를
추가한다. `verify_memory_pressure_candidate.py`의 quiescence 확인
헬퍼도 그대로 재사용한다(중복 구현 없음).

`run_memory_pressure_trial.py`(smoke 전용, 1GB 이상 차단)와
`explore_memory_pressure_intensity.py`(§50/§52 calibration 도구)의
동작은 전혀 건드리지 않는다 - 완전히 별도 경로. 산출물은 `results/`
(top-level) 아래 run_round()가 이미 쓰는 명명 규칙
(`explore-memory_pressure-native-1000mb-120s-{timestamp}-summary.json`)을
그대로 따른다 - `collect_metrics.py`는 `trial-*.json`만 glob하므로 본
실험 분석에 절대 섞이지 않는다."""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from explore_memory_pressure_intensity import (
    DEFAULT_PROBE_CONFIG,
    PROBE_RESULTS_DIR,
    analyze_slo,
    run_round,
)
from verify_memory_pressure_candidate import check_cluster_quiescent, wait_for_quiescence
from verify_memory_pressure_direct_candidate import judge_direct_safety

RESULTS_DIR = Path(__file__).parent / "results"

# 사전 등록(§94.2) - negative-control 후보 구성. CLI로 바꿀 수 없다
# (다른 강도/시간을 시험하려면 §50/§52의 calibration 경로를 다시 거쳐야
# 한다 - §56.1과 동일 원칙).
NEGATIVE_CONTROL_SIZE_MB = 1000.0
NEGATIVE_CONTROL_STAGE_DURATION_SEC = 120.0
NEGATIVE_CONTROL_WORKERS = 1
REPETITIONS = 3
COOLDOWN_SEC = 300.0  # §94.2 - 반복 사이 최소 300초(§56의 120초보다 김 - 이번 지시값)
STAGE_TAIL_MARGIN_SEC = 30.0  # analyze_slo(upper_bound_iso=...) 여유 - LATENCY_PERSIST_SEC(30초)와 동일 크기
MIN_WORKING_SET_RISE_BYTES = 800 * 1024 * 1024  # §94.5 - 요청량(1000MB)의 최소 80%, §50.4/§54.4와 동일 기준 재사용


def compute_scoped_slo(result: dict) -> dict:
    """§94.3 - stage 경계(§55.2/§56.3와 같은 이유로 명목이 아니라 실제
    t_injection~t_injection_end)로 좁힌 SLO 분석. run_round() 자신의
    `result["slo"]`는 기존 호출부와 같은 계산(upper_bound 없음)을 그대로
    유지해야 하므로(사후 재정의 금지), 이 스코핑은 별도로 다시 계산한다 -
    run_round() 내부는 손대지 않는다."""
    if result.get("aborted") or not result.get("t_injection") or not result.get("t_injection_end"):
        return {"error": "주입 시각 미확보(중단됨 등) - 스코핑 계산 불가"}
    local_raw = PROBE_RESULTS_DIR / f"probe-{result['run_id']}-native-1-raw.csv"
    return analyze_slo(local_raw, result["t_injection"],
                        upper_bound_iso=result["t_injection_end"], upper_margin_sec=STAGE_TAIL_MARGIN_SEC)


def stage_violates(result: dict) -> bool:
    """§94.6 - sustained 위반 판정(§56의 stage_violates()와 동일 로직,
    재사용 목적의 재정의 - import로 공유하면 §56 스크립트의 이름 공간을
    이 스크립트가 오염시키므로 이 파일에 독립적으로 둔다). t_slo not
    None AND 그 t_slo가 이 stage 경계(+30초 여유) 안에 있어야 위반으로
    인정한다."""
    scoped = result.get("scoped_slo", {})
    if "error" in scoped:
        return False
    return scoped.get("t_slo") is not None and scoped.get("t_slo_within_window") is True


def judge_negative_control_repetition(result: dict) -> dict:
    """§94.5 - 반복별 PASS 조건 전부: §56의 안전 기준(judge_direct_safety,
    재사용) + working set 상승 최소 800MiB(신규) + sustained SLO 위반
    없음(신규, §56과 반대 방향). 판정 로직은 순수 함수 - result dict만
    읽는다(오프라인 테스트 대상)."""
    safety = judge_direct_safety(result)
    reasons = list(safety["reasons"])

    rise = result.get("working_set_rise_bytes")
    if rise is None or rise < MIN_WORKING_SET_RISE_BYTES:
        reasons.append(f"working set 상승이 800MiB 미만 또는 미확보: {rise}")

    if stage_violates(result):
        scoped = result.get("scoped_slo", {})
        reasons.append(f"sustained SLO 위반 발생(negative control 부적격): t_slo={scoped.get('t_slo')}")

    return {"pass": len(reasons) == 0, "reasons": reasons}


def judge_negative_control_reproducibility(repetitions: list) -> dict:
    """§94.8 - 3회 최종 판정: 3/3 전부 §94.5 기준(위 judge_negative_
    control_repetition) 충족해야 PASS/Freeze 가능. 한 번이라도 실패하면
    (안전 실패든 SLO 위반이든) 전체 FAIL."""
    per_rep_pass = [r.get("negative_control", {}).get("pass") for r in repetitions]
    checks = {
        "enough_repetitions": len(repetitions) >= REPETITIONS,
        "all_reps_pass": len(repetitions) >= REPETITIONS and all(per_rep_pass),
    }
    return {"overall_pass": all(checks.values()), "checks": checks,
            "num_repetitions": len(repetitions), "per_rep_pass": per_rep_pass}


def print_repetition(result: dict, scoped_slo: dict, negative_control: dict) -> None:
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
    for reason in negative_control["reasons"]:
        print(f"  - FAIL: {reason}")
    print(f"  negative_control.pass={negative_control['pass']}")


def main():
    parser = argparse.ArgumentParser(
        description="memory_pressure negative-control 후보(1000MB x 120초, direct 단일 점프) 3회 재현성 검증(§94)")
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

        result = run_round(NEGATIVE_CONTROL_SIZE_MB, NEGATIVE_CONTROL_WORKERS, args.probe_config,
                            stage_duration_sec=NEGATIVE_CONTROL_STAGE_DURATION_SEC, min_headroom_bytes=0.0)
        result["rep_index"] = i
        scoped_slo = compute_scoped_slo(result)
        result["scoped_slo"] = scoped_slo
        negative_control = judge_negative_control_repetition(result)
        result["negative_control"] = negative_control
        print_repetition(result, scoped_slo, negative_control)

        out_path = RESULTS_DIR / f"{result['run_id']}-negative-control-verify-summary.json"
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        print(f"요약 저장: {out_path}")
        repetitions.append(result)

        if not negative_control["pass"]:
            print("이 회차가 negative-control 기준을 충족하지 못함(안전 실패 또는 SLO 위반) - "
                  "이후 반복을 실행하지 않는다(지시)", file=sys.stderr)
            break

        if i < args.repetitions:
            wait_for_quiescence(vllm_pod, node_name, baseline_restart_count, args.cooldown_sec)

    verdict = judge_negative_control_reproducibility(repetitions)
    print(f"\n{'=' * 20} 최종 판정 {'=' * 20}")
    for k, v in verdict["checks"].items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    print(f"\n종합: {'PASS' if verdict['overall_pass'] else 'FAIL'} "
          f"({sum(1 for p in verdict['per_rep_pass'] if p)}/{verdict['num_repetitions']}회 PASS)")

    verdict_path = RESULTS_DIR / f"verify-memory_pressure-negative-control-verdict-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    verdict_path.write_text(json.dumps({"verdict": verdict, "run_ids": [r["run_id"] for r in repetitions]},
                                        indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"판정 저장: {verdict_path}")
    return repetitions, verdict


if __name__ == "__main__":
    main()
