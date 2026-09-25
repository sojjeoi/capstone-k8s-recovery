#!/usr/bin/env python3
"""§140 계약 이후 두 번째 쌍(proposed rep02 -> fixed_threshold rep02) 분석.
correct_followup_138.py의 함수(analyze_arm_corrected/recompute_cost)를
그대로 재사용한다 - 새 판정 로직 없음, rep 인자만 다르게 넘긴다. 각
반복(쌍)의 결과를 항상 분리해서 보여준다 - 전체 요청을 합산한 단일
비율로 결론 내리지 않는다(§140 계약 §2 금지사항).
"""
import json

from correct_followup_138 import analyze_arm_corrected, recompute_cost, FOLLOWUP_DIR


def check_pass_conditions(rep):
    """§140에서 사전 고정한 기술적 통과조건 2개를 기계적으로 확인한다."""
    results = {}
    for arm in ("fixed_threshold", "proposed"):
        run_id = f"load_ramp-{arm}-{rep:02d}-post_hoc_followup-v1"
        drained_ok = True  # RuntimeError 없이 정상 종료했다는 뜻(호출부에서 exit code 0 확인 완료)
        r = analyze_arm_corrected(arm, rep=rep)
        c = r["cohort_900s"]
        anomalies = c["n_terminal_only_anomaly"] + c["n_duplicate_sent_anomaly"] + c["n_duplicate_terminal_excluded"]
        results[arm] = {"drained_ok": drained_ok, "cohort_anomalies": anomalies, "pass": drained_ok and anomalies == 0}
    return results


def main():
    print("=" * 70)
    print("§140 기술적 통과조건 확인 (rep02)")
    print("=" * 70)
    pass_check = check_pass_conditions(2)
    for arm, v in pass_check.items():
        print(f"  [{arm}] drain 정상종료={v['drained_ok']} cohort 이상={v['cohort_anomalies']}건 -> "
              f"{'PASS' if v['pass'] else 'FAIL'}")
    all_pass = all(v["pass"] for v in pass_check.values())
    print(f"\n전체 통과조건: {'PASS - 계측 프로토콜이 의도대로 동작함' if all_pass else 'FAIL - 추가 반복 전 재점검 필요'}")

    print("\n" + "=" * 70)
    print("반복별 결과(항상 분리 - 합산 비율로 결론 내리지 않음)")
    print("=" * 70)
    out = {"pass_conditions": pass_check, "reps": {}}
    for rep in (1, 2):
        print(f"\n--- rep{rep:02d} ---")
        out["reps"][rep] = {}
        for arm in ("fixed_threshold", "proposed"):
            r = analyze_arm_corrected(arm, rep=rep)
            out["reps"][rep][arm] = r
            c = r["cohort_900s"]
            t = r["timing"]
            cost = recompute_cost(r["run_id"])
            obs = cost["detector"]["phases"]["observe_900s"] if cost.get("detector") else {}
            print(f"  [{arm}] episode1 recovery_sec={t['recovery_sec_first_episode']:.2f}s | "
                  f"episode 수={r['n_episodes_in_full_900s']} | 탐지관계={t['timing_relation'][:20]}...")
            print(f"    cohort: 실패={c['failure_count']}/900({c['failure_rate']}) "
                  f"지연초과={c['over_threshold_count']}/900({c['over_threshold_rate']}) "
                  f"raw위반시간={r['raw_violating_sec_in_cohort']}s")
            if obs.get("cpu_used_sec") is not None:
                print(f"    비용(관측구간): CPU={obs['cpu_used_sec']}s 메모리평균={obs['rss_bytes_mean']/1e6:.1f}MB "
                      f"최대={obs['rss_bytes_max']/1e6:.1f}MB")

    print("\n" + "=" * 70)
    print("반복 간 비교 - 일관된 점 / 달라진 점")
    print("=" * 70)
    for arm in ("fixed_threshold", "proposed"):
        r1, r2 = out["reps"][1][arm], out["reps"][2][arm]
        c1, c2 = r1["cohort_900s"], r2["cohort_900s"]
        print(f"[{arm}] rep01 vs rep02:")
        print(f"  실패율: {c1['failure_rate']} vs {c2['failure_rate']}")
        print(f"  지연초과율: {c1['over_threshold_rate']} vs {c2['over_threshold_rate']}")
        print(f"  episode1 recovery_sec: {r1['timing']['recovery_sec_first_episode']:.2f}s vs "
              f"{r2['timing']['recovery_sec_first_episode']:.2f}s")
        print(f"  episode 수: {r1['n_episodes_in_full_900s']} vs {r2['n_episodes_in_full_900s']}")

    print("\n[arm 간 차이가 반복마다 같은 방향인가?]")
    for rep in (1, 2):
        f = out["reps"][rep]["fixed_threshold"]["cohort_900s"]
        p = out["reps"][rep]["proposed"]["cohort_900s"]
        fail_diff = f["failure_rate"] - p["failure_rate"]
        over_diff = f["over_threshold_rate"] - p["over_threshold_rate"]
        print(f"  rep{rep:02d}: fixed-proposed 실패율차={fail_diff:+.4f} 지연초과율차={over_diff:+.4f} "
              f"({'proposed가 낮음(유리)' if over_diff>0 else 'fixed가 낮음(유리)' if over_diff<0 else '동일'})")

    (FOLLOWUP_DIR / "_pair_comparison_140.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n\n중간 산출물 저장: {FOLLOWUP_DIR}/_pair_comparison_140.json")


if __name__ == "__main__":
    main()
