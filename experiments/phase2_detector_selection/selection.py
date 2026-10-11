#!/usr/bin/env python3
"""Phase 2 선택 규칙(2026-10-11) - prereg §7(사전식, 가중 점수 없음)·§7.4 결과 상태·§8.3 active profile.

select(inputs)는 순수 함수다(같은 입력 → 같은 출력, 세션 입력 순서 무관). 순서:
  무결성(INTEGRITY_FAILURE) → 세션 유효성(SELECTION_INCOMPLETE) → Gate 2(필수, 0개면 NO_ELIGIBLE_DETECTOR, 1개면 선택)
  → Gate 3(2개일 때만) → Tie-break 1(유용 신호 시각) → Tie-break 2(사전 고정 단순성 순서).
뒤 단계의 우위로 앞 단계 실패를 보상하지 않는다. Gate 3 지표는 결과 상태와 상관없이 계산 가능한 모든 후보에 대해 보고한다.

collect()는 Stage 1 기록을 읽어 입력을 만든다(오프라인 재생 - 기록된 features + 동결 artifact, live SHADOW 상태는 쓰지 않음):
  - 유효성은 세션마다 재생 전에 확정된 p2-validity-<run>.json만 읽는다(없으면 거부, 해시를 근거에 남김)
  - Gate 2: window_start_utc ≥ t_injection_observed_utc 평가부터 연속·쿨다운을 새로 시작, 첫 3연속 ≤ 363 s(score_completed_at)
  - Gate 3: 결정 창 3600 s의 class_counts["unnecessary"](동결 N1 분석기 arm_outcome, c3 _context 방식)
클러스터 접근 없음."""
import sys
sys.dont_write_bytecode = True
import argparse  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import statistics  # noqa: E402
from pathlib import Path  # noqa: E402

import p2_common as pc  # noqa: E402

INF = math.inf
INTEGRITY, INCOMPLETE, NO_ELIGIBLE = "INTEGRITY_FAILURE", "SELECTION_INCOMPLETE", "NO_ELIGIBLE_DETECTOR"


# ---------------- 순수 선택 함수 ----------------
def _gate3_sums(normal: list) -> dict:
    out = {}
    for d in pc.DETECTOR_IDS:
        vals = [s["unnecessary"].get(d) for s in normal]
        out[d] = sum(vals) if normal and all(isinstance(v, int) for v in vals) else None
    return out


def _latency(x):
    return INF if x is None or x > pc.GATE2_LIMIT_SEC else x


def _diff(a: float, b: float) -> float:
    return 0.0 if a == b else abs(a - b)        # inf와 inf는 동등(0), inf와 유한은 inf


def select(inputs: dict) -> dict:
    """inputs = {"integrity_problems": [...], "gate1": {det: [problems]}, "normal": [{"session","attempt","unnecessary":{det:int}}],
    "pressure": [{"session","attempt","first_streak_sec":{det: float|None}}]} (normal·pressure = 유효 세션만)."""
    normal = sorted(inputs.get("normal") or [], key=lambda s: (s["attempt"], s["session"]))[:pc.N_NORMAL]
    pressure = sorted(inputs.get("pressure") or [], key=lambda s: (s["attempt"], s["session"]))[:pc.N_PRESSURE]
    gate1 = {d: sorted((inputs.get("gate1") or {}).get(d, ["gate1_not_evaluated"])) for d in pc.DETECTOR_IDS}
    integrity = sorted(inputs.get("integrity_problems") or [])
    g2_sessions = {d: [{"session": s["session"], "first_streak_sec": s["first_streak_sec"].get(d),
                        "pass": _latency(s["first_streak_sec"].get(d)) != INF} for s in pressure] for d in pc.DETECTOR_IDS}
    g2_pass = {d: sum(x["pass"] for x in g2_sessions[d]) for d in pc.DETECTOR_IDS}
    sums = _gate3_sums(normal)
    res = {"order": ["integrity", "session_validity", "gate1", "gate2", "gate3", "tiebreak_timing", "tiebreak_simplicity"],
           "integrity_problems": integrity, "gate1": gate1,
           "session_validity": {"normal_valid": len(normal), "pressure_valid": len(pressure),
                                "required": {"normal": pc.N_NORMAL, "pressure": pc.N_PRESSURE}},
           "gate2": {"limit_sec": pc.GATE2_LIMIT_SEC, "min_pass": pc.GATE2_MIN_PASS, "sessions": g2_sessions,
                     "pass_count": g2_pass, "eligible": None},
           "gate3": {"unnecessary_sum": sums, "per_session": [{"session": s["session"], "unnecessary": s["unnecessary"]}
                                                              for s in normal], "used": False, "result": None},
           "tiebreak_timing": {"used": False, "median_sec": None, "result": None},
           "tiebreak_simplicity": {"used": False, "order": list(pc.SIMPLICITY_ORDER), "result": None}}

    def done(outcome, reason):
        return {"outcome": outcome, "reason": reason, "gate_results": res}
    if integrity or any(gate1.values()):
        return done(INTEGRITY, f"integrity={integrity} gate1={ {d: p for d, p in gate1.items() if p} }")
    if len(normal) < pc.N_NORMAL or len(pressure) < pc.N_PRESSURE:
        return done(INCOMPLETE, f"valid normal {len(normal)}/{pc.N_NORMAL}, pressure {len(pressure)}/{pc.N_PRESSURE}")
    eligible = [d for d in pc.DETECTOR_IDS if g2_pass[d] >= pc.GATE2_MIN_PASS]
    res["gate2"]["eligible"] = eligible
    if not eligible:
        return done(NO_ELIGIBLE, f"Gate 2 통과 0개: {g2_pass}")
    if len(eligible) == 1:
        return done(f"SELECTED:{eligible[0]}", f"Gate 2 통과 1개({eligible[0]}): {g2_pass}")
    a, b = eligible
    res["gate3"]["used"] = True
    if sums[a] is None or sums[b] is None:
        return done(INTEGRITY, f"Gate 3 지표 계산 불가: {sums}")
    if abs(sums[a] - sums[b]) >= pc.GATE3_MIN_DIFF:
        w = a if sums[a] < sums[b] else b
        res["gate3"]["result"] = w
        return done(f"SELECTED:{w}", f"Gate 3 불필요 신호 합계 {sums} - 차이 ≥ {pc.GATE3_MIN_DIFF}")
    res["gate3"]["result"] = "equivalent"
    med = {d: statistics.median(_latency(s["first_streak_sec"].get(d)) for s in pressure) for d in eligible}
    res["tiebreak_timing"].update(used=True, median_sec={d: (None if v == INF else v) for d, v in med.items()})
    if _diff(med[a], med[b]) >= pc.TIMING_MIN_DIFF_SEC:
        w = a if med[a] < med[b] else b
        res["tiebreak_timing"]["result"] = w
        return done(f"SELECTED:{w}", f"Gate 3 동등 {sums}, 첫 3연속 중앙값 {res['tiebreak_timing']['median_sec']} - 차이 ≥ 30 s")
    res["tiebreak_timing"]["result"] = "equivalent"
    w = next(d for d in pc.SIMPLICITY_ORDER if d in eligible)
    res["tiebreak_simplicity"].update(used=True, result=w)
    return done(f"SELECTED:{w}", f"Gate 3·시각 모두 동등 - 사전 고정 단순성 순서 {list(pc.SIMPLICITY_ORDER)}")


# ---------------- 재생(기록 features + 동결 artifact) ----------------
def replay_detector(det: str, evals: list, if_scores: dict) -> list:
    """동결 c3_common.replay(연속·쿨다운·건너뜀/번호 빠짐 리셋). 자기 입력이 없는 평가는 UNKNOWN(연속 0)으로 넘긴다."""
    import c3_common as cc
    recs, scores = [], {}
    for r in evals:
        v = pc.detector_value(det, r, if_scores.get(r["evaluation_seq"]))
        if v is None:
            recs.append({**r, "status": "unknown_input" if r.get("status") == "scored" else r.get("status")})
        else:
            recs.append(r)
            scores[r["evaluation_seq"]] = pc.streak_args(det, v)[0]
    return cc.replay(recs, scores, pc.streak_args(det, 0.0)[1])


def if_scores_for(evals: list, art) -> dict:
    import c3_common as cc
    sc = [r for r in evals if r.get("status") == "scored"]
    for r in sc:
        pc.check_features(r["features"], art["schema"])
    return dict(zip([r["evaluation_seq"] for r in sc],
                    cc.score_rows(art["model"], art["scaler"], art["schema"], [cc.feature_row(r) for r in sc])))


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
                          infra_events=ctx["infra_events"], h=pc.DECISION_SEC)
    return out["class_counts"]["unnecessary"]


# ---------------- Stage 1 기록 읽기 ----------------
def _read(p):  # run_stage1._read와 같음 - selection은 run_stage1을 함수 안에서만 import(순환 방지)
    return json.loads(Path(p).read_text(encoding="utf-8"))


def collect(results_dir, *, profile_path, artifact_root, verify_freeze=pc.verify_implementation_freeze) -> dict:
    """Stage 1 기록 → select 입력 + 근거(파일 해시). 수집이 끝나기 전(§7.2-0·§17)이나 유효성 기록이 없는 시도가 있으면 거부."""
    import c3_common as cc
    import eval_c3_validation as ev
    import run_stage1 as s1
    d = Path(results_dir)
    state = s1.collection_state(d)
    if not state["finished"]:
        raise SystemExit(f"선택 분석 거부: Stage 1 수집이 끝나지 않음 {state['progress']} - 결과를 먼저 보지 않는다")
    missing = [s["run"] for s in s1.attempts(d) if not (d / f"p2-validity-{s['run']}.json").exists()]
    if missing:                                     # 닫기 전에 거부 - 유효성 확정 전 실수로 수집이 닫히지 않게
        raise SystemExit(f"재생 거부: 유효성 기록 없는 시도 {missing} - 유효성을 먼저 확정해야 함(§7.2-0)")
    if not (d / s1.CLOSED_MARKER).exists():         # 재생 전에 수집을 닫는다 - 이후 Stage 1 세션은 영구 거부(§17)
        pc.write_json_new(d / s1.CLOSED_MARKER, {"closed_by": "selection.collect", "collection_state": state})
    integrity, gate1 = [], {pc.CPU_ID: [], pc.IF_ID: []}
    impl = None
    try:
        impl = verify_freeze()
    except SystemExit as e:
        integrity.append(f"implementation_freeze: {e}")
    try:
        loaded = pc.load_profile(profile_path, stage="stage1")
    except pc.ProfileInvalid as e:
        raise SystemExit(f"selection profile 무효 - 분석 거부: {e}") from e
    if impl is not None and loaded["sha256"] != impl.get("selection_profile_sha256"):
        integrity.append("selection_profile_not_implementation_frozen")
    try:
        pc.verify_rule_spec()
    except pc.ProfileInvalid as e:
        gate1[pc.CPU_ID].append(str(e))
    art = None
    try:
        art = pc.verify_artifacts(artifact_root)
    except pc.ProfileInvalid as e:
        gate1[pc.IF_ID].append(str(e))
    if loaded["profile"]["environment"].get("telemetry_contract") != pc.contract_hashes()["telemetry_contract"]:
        for k in gate1:
            gate1[k].append("telemetry_contract_mismatch")
    sessions = sorted((_read(p) for p in d.glob("p2-session-*.json")), key=lambda r: r["attempt_number"])
    missing = [s["run"] for s in sessions if not (d / f"p2-validity-{s['run']}.json").exists()]
    if missing:
        raise SystemExit(f"재생 거부: 유효성 기록 없는 시도 {missing} - 유효성을 먼저 확정해야 함(§7.2-0)")
    integrity += [f"integrity_marker:{p.name}" for p in sorted(d.glob("p2-integrity-failure-*.json"))]
    last = sessions[-1] if sessions else {}
    if str(last.get("status", "")).startswith("refused_") and last.get("cause") in s1.INTEGRITY_CAUSES:
        integrity.append(f"unresumed_refusal:{last['run']}:{last['cause']}")    # §4.4 - 재개 못 한 해시·artifact·freeze 불일치
    normal, pressure, files = [], [], {}
    for s in sessions:
        vp = d / f"p2-validity-{s['run']}.json"
        v = _read(vp)
        files[vp.name] = pc.sha256_bytes(vp)
        if not v.get("valid"):
            continue
        ep = d / s["evidence_file"]
        files[ep.name] = pc.sha256_bytes(ep) if ep.exists() else None
        if files[ep.name] is None or files[ep.name] != s.get("evidence_sha256"):
            integrity.append(f"evidence_hash_mismatch:{s['run']}")
            continue
        starts = [r for r in map(json.loads, filter(str.strip, ep.read_text(encoding="utf-8").splitlines()))
                  if r.get("record_type") == "c1_shadow_start"]
        if s.get("environment_profile_sha256") != loaded["sha256"] or not starts                 or any(r.get("environment_profile_sha256") != loaded["sha256"] or r.get("active_detector") is not None
                       for r in starts):
            integrity.append(f"profile_changed_or_not_shadow:{s['run']}")
            continue
        evals = cc.read_evals(ep, s["run_id"])
        ifs = None
        if art is not None:
            try:
                ifs = if_scores_for(evals, art)
            except pc.ProfileInvalid as e:
                gate1[pc.IF_ID].append(f"{s['run']}: {e}")
        if s["kind"] == "normal":
            ctx = ev._context(v)
            normal.append({"session": s["run"], "attempt": s["attempt_number"],
                           "unnecessary": {pc.CPU_ID: normal_unnecessary(pc.CPU_ID, evals, ctx, {}),
                                           pc.IF_ID: normal_unnecessary(pc.IF_ID, evals, ctx, ifs) if ifs is not None else None}})
        elif not v.get("t_injection_observed_utc"):
            integrity.append(f"validity_without_injection_time:{s['run']}")
        else:
            t_inj = cc.ts(v["t_injection_observed_utc"])
            pressure.append({"session": s["run"], "attempt": s["attempt_number"],
                             "first_streak_sec": {pc.CPU_ID: first_streak_after_injection(pc.CPU_ID, evals, t_inj, {}),
                                                  pc.IF_ID: first_streak_after_injection(pc.IF_ID, evals, t_inj, ifs)
                                                  if ifs is not None else None}})
    ends = sorted(s.get("runner_ended_at_utc") or "" for s in sessions)
    return {"inputs": {"integrity_problems": integrity, "gate1": gate1, "normal": normal, "pressure": pressure},
            "profile": loaded, "files": dict(sorted(files.items())), "decided_at_utc": ends[-1] if ends else None,
            "collection_state": state}


def evidence_from(collected: dict) -> dict:
    """선택 결과 + 근거(결정적: 시각은 마지막 세션 종료 시각, evidence_id = 입력 해시)."""
    r = select(collected["inputs"])
    prof = collected["profile"]
    return {"plan_id": pc.PLAN_ID, "rule": pc.PREREG_SHA256, "evidence_id": f"p2sel-{pc.obj_sha256(collected['inputs'])[:16]}",
            "environment_profile_id": prof["profile"]["profile_id"], "selection_profile_sha256": prof["sha256"],
            "candidates": {d: pc.FROZEN_DETECTORS[d] for d in pc.DETECTOR_IDS}, "inputs": collected["inputs"],
            "input_files_sha256": collected["files"], "decided_at_utc": collected["decided_at_utc"],
            "collection_state": collected.get("collection_state"), **r}


def main(argv=None) -> int:  # pragma: no cover - 파일 쓰기 진입점(오프라인)
    p = argparse.ArgumentParser(description="Phase 2 선택(오프라인) - Stage 1 기록 → 선택 근거, SELECTED면 active profile 생성")
    p.add_argument("--results-dir", required=True, type=Path)
    p.add_argument("--artifact-root", required=True, type=Path)
    p.add_argument("--profile", type=Path, default=pc.SELECTION_PROFILE)
    p.add_argument("--out-dir", required=True, type=Path)
    a = p.parse_args(argv)
    ev = evidence_from(collect(a.results_dir, profile_path=a.profile, artifact_root=a.artifact_root))
    ep = pc.write_json_new(a.out_dir / "phase2-selection-evidence.json", ev)
    print(json.dumps({"outcome": ev["outcome"], "reason": ev["reason"], "evidence": ep.name}, ensure_ascii=False))
    if ev["outcome"].startswith("SELECTED:"):
        sp = pc.load_profile(a.profile, stage="stage1")
        active = pc.derive_active_profile(sp["profile"], sp["sha256"], ev, pc.sha256_bytes(ep))
        ap = pc.write_json_new(a.out_dir / "active-profile.json", active)
        pc.write_json_new(a.out_dir / "active-profile-provenance.json",
                          {"source_selection_profile_sha256": sp["sha256"], "selection_evidence_sha256": pc.sha256_bytes(ep),
                           "selected_detector": ev["outcome"].split(":", 1)[1], "generated_profile_sha256": pc.sha256_bytes(ap)})
        print(f"active profile: {ap.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
