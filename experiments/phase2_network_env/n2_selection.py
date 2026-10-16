#!/usr/bin/env python3
"""네트워크 지연 환경 선택 규칙(2026-10-17, DEVELOPMENT - 동결 전) - Phase 2 rev6 §7을 후보 3개로 일반화한 순수 함수.

일반화 규칙(후보가 2개일 때 Phase 2 selection.select와 결과가 정확히 같다 - 테스트로 확인):
  순서: 무결성 → 세션 유효성 → Gate 2(필수) → Gate 3 → 시각 tie-break → 단순성 tie-break(사전 고정 순서 CPU > net_probe > IF).
  Gate 2 통과 후보가 1개면 그 후보, 0개면 NO_ELIGIBLE_DETECTOR.
  Gate 3: 통과 후보 중 불필요 신호 합계가 가장 적은 값(best)을 기준으로, best보다 GATE3_MIN_DIFF(2) 이상 많은 후보는 탈락.
          남은 후보가 1개면 선택, 2개 이상이면 동등. (2개일 때 "차이 ≥ 2면 적은 쪽"과 같다)
  시각: 남은 후보의 첫 3연속 중앙값(363 s 초과·없음 = ∞) 중 가장 빠른 값 기준으로 TIMING_MIN_DIFF_SEC(30 s) 이상 느린 후보는 탈락.
  단순성: 그래도 2개 이상 남으면 SIMPLICITY_ORDER에서 앞선 후보.
뒤 단계의 우위로 앞 단계 실패를 보상하지 않는다. 클러스터 접근 없음."""
import sys
sys.dont_write_bytecode = True
import math  # noqa: E402
import statistics  # noqa: E402

import n2_common as nc  # noqa: E402

pc = nc.pc
INF = math.inf
INTEGRITY, INCOMPLETE, NO_ELIGIBLE = "INTEGRITY_FAILURE", "SELECTION_INCOMPLETE", "NO_ELIGIBLE_DETECTOR"


def _latency(x):
    return INF if x is None or x > nc.GATE2_LIMIT_SEC else x


def _diff(a: float, b: float) -> float:
    return 0.0 if a == b else abs(a - b)        # inf와 inf는 동등(0), inf와 유한은 inf


def select(inputs: dict, detectors=nc.DETECTOR_IDS, simplicity_order=nc.SIMPLICITY_ORDER) -> dict:
    """inputs = {"integrity_problems": [...], "gate1": {det: [problems]}, "normal": [{"session","attempt","unnecessary":{det:int}}],
    "pressure": [{"session","attempt","first_streak_sec":{det: float|None}}]} (normal·pressure = 유효 세션만)."""
    normal = sorted(inputs.get("normal") or [], key=lambda s: (s["attempt"], s["session"]))[:nc.N_NORMAL]
    pressure = sorted(inputs.get("pressure") or [], key=lambda s: (s["attempt"], s["session"]))[:nc.N_PRESSURE]
    gate1 = {d: sorted((inputs.get("gate1") or {}).get(d, ["gate1_not_evaluated"])) for d in detectors}
    integrity = sorted(inputs.get("integrity_problems") or [])
    g2 = {d: [{"session": s["session"], "first_streak_sec": s["first_streak_sec"].get(d),
               "pass": _latency(s["first_streak_sec"].get(d)) != INF} for s in pressure] for d in detectors}
    g2_pass = {d: sum(x["pass"] for x in g2[d]) for d in detectors}
    sums = {}
    for d in detectors:
        vals = [s["unnecessary"].get(d) for s in normal]
        sums[d] = sum(vals) if normal and all(isinstance(v, int) and not isinstance(v, bool) for v in vals) else None
    res = {"order": ["integrity", "session_validity", "gate1", "gate2", "gate3", "tiebreak_timing", "tiebreak_simplicity"],
           "detectors": list(detectors), "integrity_problems": integrity, "gate1": gate1,
           "session_validity": {"normal_valid": len(normal), "pressure_valid": len(pressure),
                                "required": {"normal": nc.N_NORMAL, "pressure": nc.N_PRESSURE}},
           "gate2": {"limit_sec": nc.GATE2_LIMIT_SEC, "min_pass": nc.GATE2_MIN_PASS, "sessions": g2, "pass_count": g2_pass,
                     "eligible": None},
           "gate3": {"unnecessary_sum": sums, "per_session": [{"session": s["session"], "unnecessary": s["unnecessary"]}
                                                              for s in normal], "used": False, "retained": None, "result": None},
           "tiebreak_timing": {"used": False, "median_sec": None, "retained": None, "result": None},
           "tiebreak_simplicity": {"used": False, "order": list(simplicity_order), "result": None}}

    def done(outcome, reason):
        return {"outcome": outcome, "reason": reason, "gate_results": res}
    if integrity or any(gate1.values()):
        return done(INTEGRITY, f"integrity={integrity} gate1={ {d: p for d, p in gate1.items() if p} }")
    if len(normal) < nc.N_NORMAL or len(pressure) < nc.N_PRESSURE:
        return done(INCOMPLETE, f"valid normal {len(normal)}/{nc.N_NORMAL}, pressure {len(pressure)}/{nc.N_PRESSURE}")
    eligible = [d for d in detectors if g2_pass[d] >= nc.GATE2_MIN_PASS]
    res["gate2"]["eligible"] = eligible
    if not eligible:
        return done(NO_ELIGIBLE, f"Gate 2 통과 0개: {g2_pass}")
    if len(eligible) == 1:
        return done(f"SELECTED:{eligible[0]}", f"Gate 2 통과 1개({eligible[0]}): {g2_pass}")
    res["gate3"]["used"] = True
    if any(sums[d] is None for d in eligible):
        return done(INTEGRITY, f"Gate 3 지표 계산 불가: {sums}")
    best = min(sums[d] for d in eligible)
    kept = [d for d in eligible if sums[d] - best < nc.GATE3_MIN_DIFF]
    res["gate3"]["retained"] = kept
    if len(kept) == 1:
        res["gate3"]["result"] = kept[0]
        return done(f"SELECTED:{kept[0]}", f"Gate 3 불필요 신호 합계 {sums} - 기준(최소 {best})보다 ≥ {nc.GATE3_MIN_DIFF} 많은 후보 탈락")
    res["gate3"]["result"] = "equivalent"
    med = {d: statistics.median(_latency(s["first_streak_sec"].get(d)) for s in pressure) for d in kept}
    res["tiebreak_timing"].update(used=True, median_sec={d: (None if v == INF else v) for d, v in med.items()})
    fastest = min(med.values())
    kept2 = [d for d in kept if _diff(med[d], fastest) < nc.TIMING_MIN_DIFF_SEC]
    res["tiebreak_timing"]["retained"] = kept2
    if len(kept2) == 1:
        res["tiebreak_timing"]["result"] = kept2[0]
        return done(f"SELECTED:{kept2[0]}",
                    f"Gate 3 동등 {sums}, 첫 3연속 중앙값 {res['tiebreak_timing']['median_sec']} - 가장 빠른 값보다 ≥ 30 s 느린 후보 탈락")
    res["tiebreak_timing"]["result"] = "equivalent"
    w = next(d for d in simplicity_order if d in kept2)
    res["tiebreak_simplicity"].update(used=True, result=w)
    return done(f"SELECTED:{w}", f"Gate 3·시각 모두 동등 - 사전 고정 단순성 순서 {list(simplicity_order)}")


# ---------------- 재생(기록된 값 + 동결 artifact) - Phase 2 selection의 일반화 ----------------
def detector_value(det: str, rec: dict, if_score):
    """CPU·IF는 동결 pc.detector_value 그대로, net_probe는 기록의 net_probe.value."""
    return nc.net_value(rec) if det == nc.NET_ID else pc.detector_value(det, rec, if_score)


def streak_args(det: str, v: float):
    """동결 advance_streak(value < threshold = 이상) 입력. CPU·net_probe는 '높을수록 이상'이라 부호를 바꾼다."""
    if det == nc.NET_ID:
        return (-v, -nc.NET_THRESHOLD)
    return pc.streak_args(det, v)


def replay_detector(det: str, evals: list, if_scores: dict) -> list:
    """동결 c3_common.replay(연속·쿨다운·건너뜀/번호 빠짐 리셋). 자기 입력이 없는 평가는 UNKNOWN(연속 0)으로 넘긴다."""
    import c3_common as cc
    recs, scores = [], {}
    for r in evals:
        v = detector_value(det, r, if_scores.get(r["evaluation_seq"]))
        if v is None:
            recs.append({**r, "status": "unknown_input" if r.get("status") == "scored" else r.get("status")})
        else:
            recs.append(r)
            scores[r["evaluation_seq"]] = streak_args(det, v)[0]
    return cc.replay(recs, scores, streak_args(det, 0.0)[1])


def first_streak_after_injection(det: str, evals: list, t_inj, if_scores: dict):
    """Gate 2(§7.2): 평가 창 전체가 주입 뒤인 평가부터 새로 재생 - 주입 전 연속·쿨다운은 이어지지 않는다. 초(또는 None)."""
    import c3_common as cc
    inc = [r for r in evals if r.get("window_start_utc") and cc.ts(r["window_start_utc"]) >= t_inj]
    for x in replay_detector(det, inc, if_scores):
        if x["condition_met"]:
            return round((x["t"] - t_inj).total_seconds(), 3)
    return None


def normal_unnecessary(det: str, evals: list, ctx: dict, if_scores: dict) -> int:
    """Gate 3(§7.3): 결정 창 3600 s 불필요 신호 수 - 동결 N1 arm_outcome에 유효성 파일의 SLO·service·infra 사건을 넣는다."""
    import guarded_normal_bc_analysis as nab
    rp = replay_detector(det, evals, if_scores)
    out = nab.arm_outcome([x["t"] for x in rp if x["condition_met"]], ctx["t0"], obs_end=ctx["obs_end"],
                          episodes=ctx["episodes"], service_events=ctx["service_events"],
                          infra_events=ctx["infra_events"], h=nc.DECISION_SEC)
    return out["class_counts"]["unnecessary"]
