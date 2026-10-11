#!/usr/bin/env python3
"""c3 세션 운영 유효성 판정(2026-10-10) - PREREG E절. 탐지기 결과(점수·이상·신호)는 판정에 쓰지 않고 기록하지도 않는다.

기본 판정은 N1 분석기 analyze_session(파일 수정 없음, 전역만 c3로 잠시 바꿈)을 그대로 쓴다: 기록 완전성·공백 ≤ 30 s·정리/하네스
오류·probe 이미지·probe 공백·요청 ≥ 관찰 × 0.95·관찰 길이·공식 SLO(slo_judge v3·slo_episodes r2). analyze_session이
내부에서 계산하는 arm 결과(arms·신호)는 버리고 출력하지 않는다.
c3 추가 확인: 주입 없음·승격/조치 없음·active selector 불변·기대 Pod UID 불변·restart 수 불변·c1 사용 가능 비율 ≥ 0.90·
PC 전원 사건 없음·운영자 개입 없음. memory_slope 등 특징 값과 c1 이상 여부는 유효성 조건이 아니다."""
import sys
sys.dont_write_bytecode = True
import argparse  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402

import c3_common as cc  # noqa: E402

USABLE_MIN = 0.90
OUT_KEYS = ("status", "invalid_reasons", "t0_utc", "observation_end_utc", "nodes", "slo", "service_events", "infra_events",
            "input_grid")


def _vllm_pods(snap):
    return [p for p in (snap or {}).get("pods") or [] if (p.get("name") or "").startswith("vllm-serving-") and p.get("hash")]


def c3_reasons(*, summary, contract, evals, snapshots, c3rec, operator, obs_start, obs_end) -> list:
    """c3 추가 무효 사유(운영·무결성만). evals = c1_evaluation 기록(점수 필드는 읽지 않음)."""
    r = []
    cond = contract.get("conditions") or {}
    if contract.get("kind") != "no_injection" or summary.get("kind") != "no_injection" or cond.get("injection") is not None:
        r.append("injection_present_or_unknown")
    if contract.get("mode") != "record_only" or contract.get("no_promotion_path") is not True:
        r.append("promotion_path_not_excluded")
    acts = ((summary.get("policy_state_after") or {}).get("actions") or {})
    acts = acts.values() if isinstance(acts, dict) else acts
    if any((a or {}).get("run_id") == summary.get("run_id") for a in acts):
        r.append("policy_action_for_this_run")
    iso = summary.get("old_policy_isolation") or {}
    if (iso.get("start") or {}).get("isolated") is not True or (iso.get("end") or {}).get("isolated") is not True:
        r.append("old_recovery_policy_not_isolated")
    sels = {(e["pod_snapshot"].get("active_selector"), e["pod_snapshot"].get("preview_selector"))
            for e in evals if e.get("pod_snapshot")}
    if len(sels) > 1:
        r.append(f"rollout_selector_changed: {sorted(map(str, sels))}")
    in_obs = [e for e in evals if e.get("eval_started_at_utc") and obs_start <= cc.ts(e["eval_started_at_utc"]) <= obs_end]
    exp = {json.dumps(e.get("expected_pods"), sort_keys=True) for e in in_obs if e.get("status") == "scored"}
    if len(exp) > 1:
        r.append("expected_pod_uid_changed")
    restarts = {}
    for snap in [e.get("pod_snapshot") for e in evals] + list(snapshots):
        for p in _vllm_pods(snap):
            n = p.get("restart_count", p.get("restarts"))
            if n is not None:
                restarts.setdefault(p["uid"], set()).add(n)
    if any(len(v) > 1 for v in restarts.values()):
        r.append(f"restart_count_changed: { {k: sorted(v) for k, v in restarts.items() if len(v) > 1} }")
    # Rollout 스냅샷(시작·끝): active Service selector·endpoints는 같아야 한다. generation은 preview 준비가 template을 바꿔
    # 정상적으로도 변하므로 기록만 한다(판정 아님).
    for key in ("svc_selectors", "endpoints"):
        vals = {json.dumps((s.get(key) or {}).get("vllm-active"), sort_keys=True) for s in snapshots if s.get(key) is not None}
        if len(vals) > 1:
            r.append(f"active_service_{key}_changed")
    if not snapshots:
        r.append("k8s_snapshots_missing")
    usable = sum(e.get("status") == "scored" for e in in_obs) / len(in_obs) if in_obs else 0.0
    if usable < USABLE_MIN:
        r.append(f"c1_usable_ratio={usable:.3f} < {USABLE_MIN}")
    if c3rec is None:
        r.append("c3_session_record_missing")
    else:
        if c3rec.get("power_events_error") or c3rec.get("power_events") is None:
            r.append(f"power_check_failed: {c3rec.get('power_events_error')}")
        elif c3rec["power_events"]:
            r.append(f"pc_power_event: {len(c3rec['power_events'])}")
    if operator != "none":
        r.append(f"operator_intervention: {operator}")
    return r


def classify(base: dict, extra: list) -> str:
    if base.get("status") == "invalid" or extra:
        return "invalid"
    if base.get("status") == "unexpected_degradation" or base.get("service_events"):
        return "unexpected_degradation"
    return "normal_valid" if base.get("status") == "normal" else "invalid"


def validate(run, *, operator, results_dir=None, manifest=None) -> dict:
    import guarded_normal_bc_analysis as nab
    import run_guarded_normal_bc as rn
    m = cc.load_manifest(manifest)
    d = Path(results_dir or cc.COLLECTION_DIR)
    with cc.patched_n1(rn, runs=cc.runs_table(m), results_dir=d):
        sess = nab.read_session(d, run)
        base = nab.analyze_session(sess)
        run_id = rn.run_id_for(run)
    base = {k: base.get(k) for k in OUT_KEYS}           # arms(탐지기 결과)는 버림
    summary, contract = sess.get("summary") or {}, sess.get("contract") or {}
    rec_p = d / f"c3-session-{run}.json"
    rec = json.loads(rec_p.read_text(encoding="utf-8")) if rec_p.exists() else None
    extra = []
    if sess.get("missing"):
        extra.append(f"missing:{sess['missing']}")
    else:
        evals = cc.read_evals(sess["evidence_path"], run_id)
        snaps_p = d / f"c3-k8s-snapshots-{run}.jsonl"
        snaps = [json.loads(x) for x in snaps_p.read_text(encoding="utf-8").splitlines() if x.strip()] if snaps_p.exists() else []
        obs_s, obs_e = summary.get("observation_started_at_utc"), summary.get("observation_ended_at_utc")
        if not obs_s or not obs_e:
            extra.append("observation_window_missing")
        else:
            extra += c3_reasons(summary=summary, contract=contract, evals=evals, snapshots=snaps, c3rec=rec,
                                operator=operator, obs_start=cc.ts(obs_s), obs_end=cc.ts(obs_e))
    nodes = base.get("nodes") or {}
    rec = rec or {}
    identity = {"active_target": rec.get("active_target"),                      # 행 선택(공식 기대 Pod)용 - 무효 기준 아님
                "preview_hash": (rec.get("preview_prep") or {}).get("created_pod_hash"),
                "samples_file": (rec.get("identity_recorder") or {}).get("samples_file"),
                "pod_events_file": rec.get("pod_events_file"), "identity_recorder": rec.get("identity_recorder")}
    status, m1 = classify(base, extra), None
    if status == "normal_valid":
        # PREREG M-1(2026-10-10 amendment): CALIBRATION은 빌더 제외 AND live scored AND interior 평가가 1개라도 있으면
        # INVALID_CALIBRATION_ATTEMPT(보정 불가, 같은 역할 예비 ID로 대체). 다른 역할은 사건만 기록.
        role = cc.session_role(m, run, cc.load_replacements(m, d))
        try:
            b = cc.live_consistent(cc.official_rows(
                evals, d / identity["samples_file"] if identity["samples_file"] else None,
                d / identity["pod_events_file"] if identity["pod_events_file"] else None,
                identity["active_target"], identity["preview_hash"]), evals)
            r = cc.m1_events(run, evals, b)
            m1 = {"role": role, "m1_count": r["m1_count"], "m1_events": [e for e in r["events"] if e["counts_for_m1"]],
                  "events": r["events"], "eligible_rows": len(b["rows"]), "error": None}
        except Exception as e:  # noqa: BLE001 - 판정 불가: 보정이면 fail-closed, 다른 역할은 기록만(유효성 파일은 남김)
            m1 = {"role": role, "m1_count": None, "m1_events": [], "events": [], "eligible_rows": None,
                  "error": f"{type(e).__name__}: {e}"}
        # 보정 시도로 쓸 수 없음: M-1 사건 > 0, 판정 불가, 적격 행 0개 → 같은 역할 예비 ID로 대체 가능하게 상태를 바꾼다
        if role == "CALIBRATION" and (m1["error"] or m1["m1_count"] > 0 or not m1["eligible_rows"]):
            status = "INVALID_CALIBRATION_ATTEMPT"
    return {"run": run, "run_id": run_id, "status": status, "base_n1_status": base.get("status"),
            "row_selection_identity": identity, "m1": m1,
            "invalid_reasons": (base.get("invalid_reasons") or []) + extra, "c3_reasons": extra,
            "evidence_file": Path(sess["evidence_path"]).name, "active_uid": nodes.get("active_uid"),
            "t0_utc": base.get("t0_utc"), "base": base, "operator_intervention": operator,
            "rule": "PREREG E - 운영·무결성 조건만, 탐지기 결과 미사용"}


def main(argv=None) -> int:  # pragma: no cover - 파일 쓰기 진입점(오프라인, 클러스터 접근 없음)
    p = argparse.ArgumentParser(description="c3 세션 운영 유효성 판정(오프라인)")
    p.add_argument("--run", required=True)
    p.add_argument("--operator-intervention", required=True, help="'none' 또는 개입 내용(사람이 기록)")
    a = p.parse_args(argv)
    v = validate(a.run, operator=a.operator_intervention)
    cc.write_new_json(cc.COLLECTION_DIR / f"{a.run}-validity.json", v)
    print(json.dumps({k: v[k] for k in ("run", "status", "invalid_reasons")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
