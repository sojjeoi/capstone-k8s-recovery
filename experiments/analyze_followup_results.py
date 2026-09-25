#!/usr/bin/env python3
"""후속 고정관측구간 비교(§137 이후 지시) 결과 분석 - 사전등록한 비교 계약
(harm-cost-protocol.md §5)의 주/보조 지표만 계산한다. 새 판정 로직 없음 -
slo_judge.evaluate()/find_t_slo()/find_t_recovery()를 그대로 재사용.

읽기 전용, results/followup/만 본다 - 기존 core 45건과 절대 합산하지 않는다.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import slo_judge as sj

FOLLOWUP_DIR = Path(__file__).parent / "results" / "followup"


def load_trial(run_id):
    p = FOLLOWUP_DIR / f"trial-{run_id}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def load_evidence(run_id, arm, rep):
    p = FOLLOWUP_DIR / f"probe-{run_id}-{arm}-{rep}-evidence.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def analyze_arm(arm, rep=1):
    """§137 이후 지시 turn 2에서 발견한 실제 계측 공백: make_load_ramp_prober_followup()의
    _refresh()는 check_slo_violation()/check_recovered() 안에서만 호출되는데, 그 둘은
    각각 t_slo/t_recovery가 '아직 안 정해졌을 때만' 불린다 - 원래(조기종료) 설계에서는
    무해했지만(정해지자마자 루프가 break), fixed_duration_observation=True로 그 뒤에도
    수백 초를 더 관찰하면 로컬 raw CSV 캐시가 그 시점 이후로 전혀 안 갱신된다(실측 확인 -
    97개 행에서 멈춤, 실제로는 900개 이상 발신됨). 원격 pod 삭제 전에 raw CSV는 마지막
    갱신을 다시 안 당겨온 반면 evidence JSONL은 stop()에서 통째로 회수하므로 데이터가
    남아있다 - 그래서 주 지표는 evidence(완전)에서 계산하고, raw CSV(불완전)는 참고용
    총 건수만 남긴다. 이건 새 계측 자체의 진짜 공백으로, 다음 반복 전 고쳐야 한다(§8)."""
    run_id = f"load_ramp-{arm}-{rep:02d}-post_hoc_followup-v1"
    raw = load_trial(run_id)
    if raw is None:
        print(f"[{arm}] 결과 파일 없음 - {FOLLOWUP_DIR}/trial-{run_id}.json 미생성(trial 미완료 또는 실패)")
        return None

    probe_path = FOLLOWUP_DIR.parent / f"probe-{run_id}-{arm}-{rep}-raw.csv"
    if not probe_path.exists():
        probe_path = Path("results") / f"probe-{run_id}-{arm}-{rep}-raw.csv"
    prows = sj.load_raw(probe_path) if probe_path.exists() else []
    stale_csv_n = len(prows)

    evidence = load_evidence(run_id, arm, rep)
    sent_ids = {e["request_id"] for e in evidence if e.get("event") == "sent"}
    resolved = [e for e in evidence if e.get("event") in ("completed", "timeout", "error")]
    resolved_ids = {e["request_id"] for e in resolved}
    unresolved = sent_ids - resolved_ids

    n_req = len(resolved)
    n_fail = sum(1 for e in resolved if not e.get("success"))
    n_over = sum(1 for e in resolved if e.get("elapsed_monotonic_sec", 0) > sj.LATENCY_THRESHOLD)
    fail_or_over = sum(1 for e in resolved
                        if not e.get("success") or e.get("elapsed_monotonic_sec", 0) > sj.LATENCY_THRESHOLD)
    eval_sec = None
    if resolved:
        sent_times = [datetime.fromisoformat(e["sent_at"]) for e in resolved]
        eval_sec = (max(sent_times) - min(sent_times)).total_seconds()

    result = {
        "run_id": run_id, "outcome": raw.get("outcome"), "state": raw.get("state"),
        "t_slo": raw.get("t_slo"), "t_recovery": raw.get("t_recovery"),
        "recovery_sec_schema_value": (
            (datetime.fromisoformat(raw["t_recovery"]) - datetime.fromisoformat(raw["t_slo"])).total_seconds()
            if raw.get("t_slo") and raw.get("t_recovery") else None
        ),
        "detection_source": raw.get("detection_source"), "detector": raw.get("detector"),
        "target_replaced": raw.get("target_replaced"),
        # 주 지표(비교 계약 §5) - 동일 probe 프로필, 동일 고정 관측구간(900s) 기준
        "n_requests_observed": n_req, "observed_span_sec": eval_sec,
        "primary_failure_rate": round(n_fail / n_req, 4) if n_req else None,
        "primary_latency_over_threshold_rate": round(n_over / n_req, 4) if n_req else None,
        "failure_or_latency_union_rate": round(fail_or_over / n_req, 4) if n_req else None,
        "n_failure": n_fail, "n_over_threshold": n_over,
        # 미완료 처리(§3/§7) - request_id 기준, CSV엔 안 남고 evidence로만 확인
        "n_sent": len(sent_ids), "n_resolved": len(resolved_ids), "n_unresolved": len(unresolved),
        "unresolved_rate": round(len(unresolved) / len(sent_ids), 4) if sent_ids else None,
        "stale_local_csv_row_count": stale_csv_n,  # 참고용 - _refresh() 공백으로 불완전(위 docstring)
    }
    return result


def main():
    print("=" * 70)
    print("후속 고정관측구간 비교 결과 (post_hoc_followup-v1, load_ramp) - 탐색적 비교")
    print("n=1/arm - 우월성 판정용 표본 아님, 계측·프로토콜 실행가능성 확인 목적")
    print("=" * 70)
    results = {}
    for arm in ("fixed_threshold", "proposed"):
        r = analyze_arm(arm)
        results[arm] = r
        if r:
            print(f"\n[{arm}] outcome={r['outcome']} recovery_sec={r['recovery_sec_schema_value']} "
                  f"detection_source={r['detection_source']}/{r['detector']}")
            print(f"  주 지표(동일 고정 관측 {r['observed_span_sec']}s): "
                  f"실패율={r['primary_failure_rate']}({r['n_failure']}/{r['n_requests_observed']}) "
                  f"지연초과율={r['primary_latency_over_threshold_rate']}({r['n_over_threshold']}/{r['n_requests_observed']}) "
                  f"합집합피해율={r['failure_or_latency_union_rate']}")
            print(f"  미완료 처리: sent={r['n_sent']} resolved={r['n_resolved']} "
                  f"unresolved={r['n_unresolved']}({r['unresolved_rate']})")

    cost_summary = {}
    for arm in ("fixed_threshold", "proposed"):
        run_id = f"load_ramp-{arm}-01-post_hoc_followup-v1"
        cost_path = FOLLOWUP_DIR / f"cost-{run_id}.json"
        if cost_path.exists():
            cost = json.loads(cost_path.read_text(encoding="utf-8"))
            cost_summary[arm] = cost
            print(f"\n[{arm}] 비용: detector(로컬)={cost['local_detector']['summary'] if cost['local_detector'] else None}")
            print(f"  클러스터 pod: {cost['cluster_pods']['summary'] if cost['cluster_pods'] else None}")

    out = {"requests": results, "cost": cost_summary}
    (FOLLOWUP_DIR / "_analysis_summary.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n\n중간 산출물 저장: {FOLLOWUP_DIR}/_analysis_summary.json")


if __name__ == "__main__":
    main()
