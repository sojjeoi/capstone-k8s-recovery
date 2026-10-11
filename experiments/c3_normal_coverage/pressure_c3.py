#!/usr/bin/env python3
"""c3 압박 robustness(2026-10-10, skeleton - c3 동결 뒤에만 실행) - PREREG P절.

자료: 공식 A/B A arm ab01a–ab05a evidence의 기록 features(읽기만). 주입 시각 = summary trial_outcome.t_injection_observed_utc.
기준: 주입 시각 이후 첫 3연속 신호(condition_met)가 주입 + 363 s 이내. 그 3연속에 주입 전 평가가 섞이면 표시.
t_slo와의 시간 관계는 공식 A/B 분석 결과를 그대로 인용한다(여기서 다시 계산하지 않음).
허용 결론: "gross CPU pressure에서도 이상 신호를 유지했다". 조기 예측 주장 금지."""
import sys
sys.dont_write_bytecode = True
import argparse  # noqa: E402
import json  # noqa: E402

import c3_common as cc  # noqa: E402

AB_DIR = cc.EXP / "results" / "capstone_guarded_ab_v1"
RUNS = ("ab01a", "ab02a", "ab03a", "ab04a", "ab05a")
LIMIT_SEC = 363.0
OUT = cc.EXP / "results" / "c3_normal_coverage_evaluation" / "c3-pressure-robustness.json"


def run_files(run):
    rid = f"guarded_pressure_record_only-{run}-capstone-guarded-ab-v1-20261007"
    return AB_DIR / f"c1-live-evidence-{rid}.jsonl", AB_DIR / f"summary-{rid}.json"


def pressure_check(evals, t_inj, art) -> dict:
    sc = [r for r in evals if r.get("status") == "scored"]
    scores = dict(zip([r["evaluation_seq"] for r in sc],
                      cc.score_rows(art["model"], art["scaler"], art["schema"], [cc.feature_row(r) for r in sc])))
    rp = cc.replay(evals, scores, art["threshold"])
    traj = [{"rel_inj_sec": round((y["t"] - t_inj).total_seconds(), 3), "score": scores[y["seq"]]}
            for y in rp if y["status"] == "scored"]
    for i, x in enumerate(rp):
        if x["condition_met"] and x["t"] >= t_inj:
            streak = rp[i - x["consecutive"] + 1: i + 1]
            return {"first_signal_rel_inj_sec": round((x["t"] - t_inj).total_seconds(), 3),
                    "kept": (x["t"] - t_inj).total_seconds() <= LIMIT_SEC,
                    "streak_includes_pre_injection": any(y["t"] < t_inj for y in streak), "score_trajectory": traj}
    return {"first_signal_rel_inj_sec": None, "kept": False, "streak_includes_pre_injection": None, "score_trajectory": traj}


def evaluate(c3_art=cc.C3_ART, out=OUT) -> dict:
    import eval_c3_validation as ev
    c3 = ev.require_frozen(c3_art)
    c1 = cc.load_frozen(cc.C1_ART)
    res = {}
    for run in RUNS:
        evp, smp = run_files(run)
        t_inj = cc.ts(json.loads(smp.read_text(encoding="utf-8"))["trial_outcome"]["t_injection_observed_utc"])
        evals = cc.read_evals(evp)
        res[run] = {"t_injection_observed_utc": t_inj.isoformat(),
                    "c3": pressure_check(evals, t_inj, c3), "c1_reference": pressure_check(evals, t_inj, c1)}
    out_obj = {"n_runs": len(RUNS), "c3_signal_kept": sum(r["c3"]["kept"] for r in res.values()), "runs": res,
               "c3_sha256sums": c3["hashes"],
               "limit_sec": LIMIT_SEC, "allowed_conclusion": "gross CPU pressure에서도 이상 신호를 유지했다(예측 아님)"}
    cc.write_new_json(out, out_obj)
    return out_obj


def main(argv=None) -> int:  # pragma: no cover - c3 동결 뒤에만(사용자 승인)
    argparse.ArgumentParser(description="c3 압박 robustness - 동결 c3 필요, 클러스터 접근 없음").parse_args(argv)
    r = evaluate()
    print(json.dumps({"c3_signal_kept": r["c3_signal_kept"], "n_runs": r["n_runs"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
