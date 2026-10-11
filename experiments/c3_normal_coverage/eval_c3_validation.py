#!/usr/bin/env python3
"""c3 독립 정상 검증 평가(2026-10-10, skeleton - c3 동결 뒤에만 실행) - PREREG N·O절.

같은 VALIDATION 세션·같은 기록 features·같은 판정 코드로 동결 c1 대 동결 c3를 비교한다.
  - 점수: 기록 features → 각 모델(scaler·IF). 연속·쿨다운·건너뜀 리셋: c3_common.replay(score_server.advance_streak)
  - 신호 분류·T_safe: N1 분석기 arm_outcome 그대로(판정 창 3600 s, 추적 600 s). 분류 근거(SLO 구간·service·infra)는 유효성
    판정 파일의 t0 상대 시각(ms 반올림)에서 복원
  - 진단(판정 D 포함): memory_slope < -127903 B/s(c1 forensic 기반 사전 고정, 재계산 금지) 평가의 임계값 미만 수,
    memory_mean이 c1 학습 범위(c1 feature-schema train_feature_stats) 밖인 평가의 임계값 미만 수(보고 전용)
동결 전에는 실행되지 않는다: freeze.json(status=frozen) + SHA256SUMS 확인이 VALIDATION 파일을 열기 전에 먼저 일어난다."""
import sys
sys.dont_write_bytecode = True
import argparse  # noqa: E402
import json  # noqa: E402
from datetime import timedelta  # noqa: E402
from pathlib import Path  # noqa: E402

import c3_common as cc  # noqa: E402

EVAL_DIR = cc.EXP / "results" / "c3_normal_coverage_evaluation"
DECISION_SEC, EARLY_SEC, LATE_SEC = 3600.0, 120.0, 600.0


def require_frozen(art, manifest=None) -> dict:
    """동결 c3 확인 - 실패하면 VALIDATION을 읽기 전에 멈춘다."""
    art = Path(art)
    f = art / "freeze.json"
    if not f.exists() or json.loads(f.read_text(encoding="utf-8")).get("status") != "frozen":
        raise SystemExit(f"동결 c3 없음 - VALIDATION 평가 거부: {f}")
    fz = json.loads(f.read_text(encoding="utf-8"))
    sums = json.loads((art / "SHA256SUMS.json").read_text(encoding="utf-8"))
    need = {"model.pkl", "scaler.pkl", "threshold.json", "feature-schema.json", "freeze.json"}
    if not need <= set(sums) or fz.get("manifest_sha256") != cc.sha256(manifest or cc.MANIFEST):
        raise SystemExit(f"동결 기록 불완전 또는 세션 매니페스트 변경 - 거부: 누락 {sorted(need - set(sums))}")
    changed = [k for k, h in (fz.get("source_sha256") or {}).items()
               if not (cc.ROOT / k).is_file() or cc.sha256(cc.ROOT / k) != h]
    if changed:
        raise SystemExit(f"동결 뒤 학습 코드가 바뀜 - 거부: {changed}")
    return {**cc.load_frozen(art), "freeze": fz}


def _context(v: dict) -> dict:
    b = v["base"]
    t0 = cc.ts(b["t0_utc"])
    at = lambda s: t0 + timedelta(seconds=s)  # noqa: E731
    return {"t0": t0, "obs_end": cc.ts(b["observation_end_utc"]),
            "episodes": [(at(e["t_slo_rel_sec"]), at(e["end_rel_sec"])) for e in (b.get("slo") or {}).get("episodes") or []],
            "service_events": [at(s) for s in b.get("service_events") or []],
            "infra_events": [at(e["t_rel_sec"]) for e in b.get("infra_events") or []]}


def model_metrics(evals, ctx, art, mem_range) -> dict:
    import guarded_normal_bc_analysis as nab
    sc = [r for r in evals if r.get("status") == "scored"]
    scores = dict(zip([r["evaluation_seq"] for r in sc],
                      cc.score_rows(art["model"], art["scaler"], art["schema"], [cc.feature_row(r) for r in sc])))
    rp = cc.replay(evals, scores, art["threshold"])
    t0, end = ctx["t0"], ctx["t0"] + timedelta(seconds=DECISION_SEC)
    out = nab.arm_outcome([x["t"] for x in rp if x["condition_met"]], t0, obs_end=ctx["obs_end"], episodes=ctx["episodes"],
                          service_events=ctx["service_events"], infra_events=ctx["infra_events"], h=DECISION_SEC)
    win = [r for r in sc if t0 <= cc.ts(r["score_completed_at_utc"]) < end]
    below = [r for r in win if scores[r["evaluation_seq"]] < art["threshold"]]
    memdec = [r for r in win if r["features"]["memory_slope"] < cc.MEMORY_DECREASE_DIAG_BPS]
    memout = [r for r in win if not mem_range[0] <= r["features"]["memory_mean"] <= mem_range[1]]
    sig = [x["t_rel_sec"] for x in out["signals"]]
    return {"t_safe_sec": out["t_safe_sec"], "unnecessary": out["class_counts"]["unnecessary"],
            "raw_triggers": out["raw_trigger_count"], "class_counts": out["class_counts"],
            "below_count": len(below), "below_ratio": len(below) / len(win) if win else None, "n_window_scored": len(win),
            "early_triggers": sum(t <= EARLY_SEC for t in sig), "late_triggers": sum(t >= LATE_SEC for t in sig),
            "memdec_evals": len(memdec), "memdec_below": sum(scores[r["evaluation_seq"]] < art["threshold"] for r in memdec),
            "memmean_out_evals": len(memout),
            "memmean_out_below": sum(scores[r["evaluation_seq"]] < art["threshold"] for r in memout)}


def decide(rows: list, *, pressure_ok: bool, calibration_failed: bool) -> dict:
    """PREREG O절 판정표(위에서부터 첫 해당). rows = [{"c1": metrics, "c3": metrics}] normal_valid 검증 세션."""
    n = len(rows)
    c1u, c3u = sum(r["c1"]["unnecessary"] for r in rows), sum(r["c3"]["unnecessary"] for r in rows)
    a_cnt = sum(r["c3"]["t_safe_sec"] >= r["c1"]["t_safe_sec"] for r in rows)
    crit = {"n": n, "A_sessions_c3_ge_c1": a_cnt, "A": a_cnt >= n - 1, "B": c3u < c1u, "sum_unnecessary": {"c1": c1u, "c3": c3u},
            "C": not any(r["c3"]["t_safe_sec"] - r["c1"]["t_safe_sec"] <= -600
                         or r["c3"]["unnecessary"] - r["c1"]["unnecessary"] >= 5 for r in rows),
            "D_evaluable": sum(r["c1"]["memdec_evals"] for r in rows) > 0, "E": pressure_ok}
    crit["D"] = crit["D_evaluable"] and sum(r["c3"]["memdec_below"] for r in rows) < sum(r["c1"]["memdec_below"] for r in rows)
    if calibration_failed:
        v = "NOT PROMISING"
    elif n < 3 or c1u == 0:
        v = "INCONCLUSIVE"
    elif crit["A"] and crit["B"] and crit["C"] and crit["D"] and crit["E"]:
        v = "PROMISING"
    elif crit["B"] and crit["C"] and crit["E"] and a_cnt >= n - 2 and (crit["D"] or not crit["D_evaluable"]):
        v = "MIXED"
    else:
        v = "NOT PROMISING"
    return {"verdict": v, "criteria": crit}


def evaluate(c3_art=cc.C3_ART, pressure_result=None, collection_dir=cc.COLLECTION_DIR, manifest=None, out_dir=EVAL_DIR):
    c3 = require_frozen(c3_art, manifest)                # 동결 확인이 VALIDATION 읽기보다 먼저
    c1 = cc.load_frozen(cc.C1_ART)
    pr = json.loads(Path(pressure_result).read_text(encoding="utf-8"))
    if pr.get("c3_sha256sums") != c3["hashes"]:
        raise SystemExit("압박 결과가 이 동결 c3로 만든 것이 아님 - 거부")
    st = cc.load_c1_schema()["train_feature_stats"]["memory_mean"]
    m = cc.load_manifest(manifest)
    rows, excluded = [], {}
    for sid in c3["freeze"]["validation_sessions_metadata_only"]:
        vp = Path(collection_dir) / f"{sid}-validity.json"
        v = json.loads(vp.read_text(encoding="utf-8")) if vp.exists() else {"status": "no_validity_record"}
        if v["status"] != "normal_valid":
            excluded[sid] = v["status"]
            continue
        evals = cc.read_evals(Path(collection_dir) / v["evidence_file"], v["run_id"])
        ctx = _context(v)
        rows.append({"session": sid, "c1": model_metrics(evals, ctx, c1, (st["min"], st["max"])),
                     "c3": model_metrics(evals, ctx, c3, (st["min"], st["max"]))})
    res = {"manifest_plan": m["plan"], "c3_freeze": c3["freeze"], "sessions": rows, "excluded": excluded,
           "pressure": pr, "diagnostic_note": cc.DIAG_NOTE,
           **decide(rows, pressure_ok=pr["c3_signal_kept"] == pr["n_runs"] == 5,
                    calibration_failed=bool(c3["calibration_failed"]))}
    cc.write_new_json(Path(out_dir) / "c3-validation-evaluation.json", res)
    return res


def main(argv=None) -> int:  # pragma: no cover - c3 동결 뒤에만(사용자 승인)
    p = argparse.ArgumentParser(description="c3 독립 정상 검증 평가 - 동결 c3 필요, 클러스터 접근 없음")
    p.add_argument("--pressure-result", required=True, help="pressure_c3.py 결과 JSON")
    a = p.parse_args(argv)
    r = evaluate(pressure_result=a.pressure_result)
    print(json.dumps({"verdict": r["verdict"], "criteria": r["criteria"]}, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
