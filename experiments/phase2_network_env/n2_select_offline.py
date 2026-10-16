#!/usr/bin/env python3
"""네트워크 환경 오프라인 선택 분석(2026-10-17, M3, DEVELOPMENT - 동결 전, 클러스터 접근 없음, 결과 파일을 새로 쓴다).

Stage 1 기록(정상 4·압박 5 세션의 원자료 + 유효성 기록)을 읽어 후보 3개(CPU 임계값·net_probe·c3-A IF)를 같은 평가 기록에서 재생하고
n2_selection.select 입력을 만든다. Phase 2 selection.collect를 후보 3개로 일반화한 것이며, 달라지는 점만 바꿨다:
  - 세 후보 모두 같은 평가 기록의 같은 tick에서 재생한다(IF 점수는 기록된 features + 동결 artifact로 다시 계산, net_probe는 기록된 값).
  - Gate 1에 net_probe 보정 근거 검사(고정 해시)와 세션이 쓴 net_probe 정의·보정 해시 대조를 더했다.
  - 수집을 닫은 뒤에만 분석한다(유효성을 확정하기 전 결과를 먼저 보지 않는다). 재생 뒤 원자료가 바뀌면 INTEGRITY_FAILURE.
출력: 선택 증거(결정적 - 시각은 마지막 세션 종료 시각, evidence_id = 입력 해시), SELECTED면 active profile(기계적 파생)과 출처 기록."""
import sys
sys.dont_write_bytecode = True
import argparse  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402

import n2_common as nc  # noqa: E402
import n2_selection as nsel  # noqa: E402

pc = nc.pc
EVIDENCE_PREFIX = "n2sel-"          # n2_stage2.EVIDENCE_PREFIX와 같아야 한다(Stage 2 진입이 증거를 다시 계산해 대조한다)
EVIDENCE_NAME = "phase2-network-selection-evidence.json"


def _read(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def collect(s1, results_dir, *, profile_path, artifact_root, verify_freeze=None) -> dict:
    """Stage 1 기록 → select 입력 + 근거(파일 해시). s1 = n2_stage1.load_stage1 결과(실행 ID·결과 폴더 규칙을 그대로 쓴다).
    수집이 끝나기 전이나 유효성 기록이 없는 시도가 있으면 거부."""
    import c3_common as cc
    import eval_c3_validation as ev
    import p2_common as p2
    import selection as p2sel                       # 동결 Phase 2 selection - IF 점수 재계산(if_scores_for)만 재사용(후보와 무관)
    verify_freeze = verify_freeze or s1.pc.verify_implementation_freeze
    d = Path(results_dir)
    state = s1.collection_state(d)
    if not state["finished"]:
        raise SystemExit(f"선택 분석 거부: Stage 1 수집이 끝나지 않음 {state['progress']} - 결과를 먼저 보지 않는다")
    missing = [x["run"] for x in s1.attempts(d) if not (d / f"p2-validity-{x['run']}.json").exists()]
    if missing:                                     # 닫기 전에 거부 - 유효성 확정 전 실수로 수집이 닫히지 않게
        raise SystemExit(f"재생 거부: 유효성 기록 없는 시도 {missing} - 유효성을 먼저 확정해야 함(§7.2-0)")
    if not (d / s1.CLOSED_MARKER).exists():         # 재생 전에 수집을 닫는다 - 이후 Stage 1 세션은 영구 거부(§17)
        pc.write_json_new(d / s1.CLOSED_MARKER, {"closed_by": "n2_select_offline.collect", "collection_state": state})
    integrity, gate1 = [], {det: [] for det in nc.DETECTOR_IDS}
    impl = None
    try:
        impl = verify_freeze()
    except SystemExit as e:
        integrity.append(f"implementation_freeze: {e}")
    try:
        loaded = nc.load_profile(profile_path, stage="stage1")
    except p2.ProfileInvalid as e:
        raise SystemExit(f"selection profile 무효 - 분석 거부: {e}") from e
    if impl is not None and loaded["sha256"] != impl.get("selection_profile_sha256"):
        integrity.append("selection_profile_not_implementation_frozen")
    try:
        pc.verify_rule_spec()
    except p2.ProfileInvalid as e:
        gate1[nc.CPU_ID].append(str(e))
    try:
        nc.verify_net_calibration()
    except p2.ProfileInvalid as e:
        gate1[nc.NET_ID].append(str(e))
    art = None
    try:
        art = pc.verify_artifacts(artifact_root)
    except p2.ProfileInvalid as e:
        gate1[nc.IF_ID].append(str(e))
    if loaded["profile"]["environment"].get("telemetry_contract") != nc.contract_hashes()["telemetry_contract"]:
        for k in gate1:
            gate1[k].append("telemetry_contract_mismatch")
    sessions = sorted((_read(p) for p in d.glob("p2-session-*.json")), key=lambda r: r["attempt_number"])
    missing = [x["run"] for x in sessions if not (d / f"p2-validity-{x['run']}.json").exists()]
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
        if s.get("environment_profile_sha256") != loaded["sha256"] or not starts \
                or any(r.get("environment_profile_sha256") != loaded["sha256"] or r.get("active_detector") is not None for r in starts):
            integrity.append(f"profile_changed_or_not_shadow:{s['run']}")
            continue
        if any(r.get("net_probe_definition_sha256") != nc.NET_DEFINITION_SHA256
               or r.get("net_probe_calibration_sha256") != nc.NET_CALIBRATION_SHA256 for r in starts):
            integrity.append(f"net_probe_definition_changed:{s['run']}")        # 세션이 다른 정의·보정으로 net_probe를 계산했다
            continue
        evals = cc.read_evals(ep, s["run_id"])
        ifs = None
        if art is not None:
            try:
                ifs = p2sel.if_scores_for(evals, art)
            except p2.ProfileInvalid as e:
                gate1[nc.IF_ID].append(f"{s['run']}: {e}")
        if s["kind"] == "normal":
            ctx = ev._context(v)
            normal.append({"session": s["run"], "attempt": s["attempt_number"],
                           "unnecessary": {nc.CPU_ID: nsel.normal_unnecessary(nc.CPU_ID, evals, ctx, {}),
                                           nc.NET_ID: nsel.normal_unnecessary(nc.NET_ID, evals, ctx, {}),
                                           nc.IF_ID: nsel.normal_unnecessary(nc.IF_ID, evals, ctx, ifs) if ifs is not None else None}})
        elif not v.get("t_injection_observed_utc"):
            integrity.append(f"validity_without_injection_time:{s['run']}")
        else:
            t_inj = cc.ts(v["t_injection_observed_utc"])
            pressure.append({"session": s["run"], "attempt": s["attempt_number"],
                             "first_streak_sec": {nc.CPU_ID: nsel.first_streak_after_injection(nc.CPU_ID, evals, t_inj, {}),
                                                  nc.NET_ID: nsel.first_streak_after_injection(nc.NET_ID, evals, t_inj, {}),
                                                  nc.IF_ID: nsel.first_streak_after_injection(nc.IF_ID, evals, t_inj, ifs)
                                                  if ifs is not None else None}})
    ends = sorted(x.get("runner_ended_at_utc") or "" for x in sessions)
    return {"inputs": {"integrity_problems": integrity, "gate1": gate1, "normal": normal, "pressure": pressure},
            "profile": loaded, "files": dict(sorted(files.items())), "decided_at_utc": ends[-1] if ends else None,
            "collection_state": state, "plan_id": s1.pc.PLAN_ID}


def evidence_from(collected: dict) -> dict:
    """선택 결과 + 근거(결정적: 시각은 마지막 세션 종료 시각, evidence_id = 입력 해시)."""
    r = nsel.select(collected["inputs"])
    prof = collected["profile"]
    return {"plan_id": collected["plan_id"], "rule": nc.PREREG_SHA256,
            "evidence_id": f"{EVIDENCE_PREFIX}{pc.obj_sha256(collected['inputs'])[:16]}",
            "environment_profile_id": prof["profile"]["profile_id"], "selection_profile_sha256": prof["sha256"],
            "candidates": {det: nc.FROZEN_DETECTORS[det] for det in nc.DETECTOR_IDS}, "inputs": collected["inputs"],
            "input_files_sha256": collected["files"], "decided_at_utc": collected["decided_at_utc"],
            "collection_state": collected.get("collection_state"), **r}


def write_outputs(ev: dict, profile_path, out_dir) -> dict:
    """선택 증거를 쓰고, SELECTED면 active profile(기계적 파생)과 출처 기록도 쓴다. 반환: 쓴 파일 이름."""
    out_dir = Path(out_dir)
    ep = pc.write_json_new(out_dir / EVIDENCE_NAME, ev)
    written = {"evidence": ep.name}
    if ev["outcome"].startswith("SELECTED:"):
        sp = nc.load_profile(profile_path, stage="stage1")
        active = nc.derive_active_profile(sp["profile"], sp["sha256"], ev, pc.sha256_bytes(ep))
        ap = pc.write_json_new(out_dir / "active-profile.json", active)
        pc.write_json_new(out_dir / "active-profile-provenance.json",
                          {"source_selection_profile_sha256": sp["sha256"], "selection_evidence_sha256": pc.sha256_bytes(ep),
                           "selected_detector": ev["outcome"].split(":", 1)[1], "generated_profile_sha256": pc.sha256_bytes(ap)})
        written["active_profile"] = ap.name
    return written


def main(argv=None, s1=None) -> int:  # pragma: no cover - 파일 쓰기 진입점(오프라인, 수집이 끝난 뒤에만)
    import n2_stage1 as ns1
    p = argparse.ArgumentParser(description="네트워크 환경 선택(오프라인) - Stage 1 기록 → 선택 근거, SELECTED면 active profile 생성")
    p.add_argument("--results-dir", type=Path, default=None)
    p.add_argument("--artifact-root", required=True, type=Path)
    p.add_argument("--profile", type=Path, default=None)
    p.add_argument("--out-dir", required=True, type=Path)
    a = p.parse_args(argv)
    cfg = ns1.NetConfig()
    s1 = s1 or ns1.load_stage1(cfg)
    ev = evidence_from(collect(s1, a.results_dir or cfg.results_dir, profile_path=a.profile or cfg.selection_profile,
                               artifact_root=a.artifact_root))
    w = write_outputs(ev, a.profile or cfg.selection_profile, a.out_dir)
    print(json.dumps({"outcome": ev["outcome"], "reason": ev["reason"], **w}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
