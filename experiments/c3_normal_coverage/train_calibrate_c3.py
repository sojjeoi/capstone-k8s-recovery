#!/usr/bin/env python3
"""c3-normal-coverage 학습·보정(2026-10-10) - PREREG C·D·M절. 바뀌는 것은 학습·보정 세션 목록뿐이다.

  - schema: c1 feature-schema.json을 읽어 쓴다(해시 고정, 재계산 없음)
  - scaler: StandardScaler 기본값(TRAIN 행에만 fit) / 모델: IsolationForest(train_v32b.MODEL_PARAMS) - c1과 같은 출처
  - 임계값: model_v31/replay.calibrate_threshold 그대로. 보정 세션을 연속 구간(건너뜀·번호 빠짐에서 끊음)별 key로 넘긴다
  - VALIDATION 세션: 이 스크립트에는 경로를 받는 인자가 없고, 역할 확인(require_role)이 파일을 열기 전에 막는다

모드:
  --reproduce-c1 --out DIR : 공식 c1 학습·보정 세션만으로 같은 절차를 돌려 공식 c1과 정확히 같은지 확인(C1 재현 관문)
  --train                  : [수집 완료 뒤에만] c1 + 새 TRAIN/CALIBRATION(normal_valid) → c3 artifact 동결(C3_ART, 새 폴더만)"""
import sys
sys.dont_write_bytecode = True
import argparse  # noqa: E402
import json  # noqa: E402
import pickle  # noqa: E402
import platform  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import c3_common as cc  # noqa: E402  (경로 설정 포함)
import numpy as np  # noqa: E402
import sklearn  # noqa: E402
from sklearn.ensemble import IsolationForest  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

import train_calibrate_v33c1 as tc  # noqa: E402  (main 실행 안 함)
from feature_selection import apply_feature_schema, compute_feature_schema  # noqa: E402
from replay import calibrate_threshold  # noqa: E402
from train_v32b import MODEL_PARAMS  # noqa: E402

N1_DIR = cc.EXP / "results" / "capstone_guarded_normal_bc_v1"
AB_DIR = cc.EXP / "results" / "capstone_guarded_ab_v1"
SOURCE_FILES = [Path(__file__), cc.HERE / "c3_common.py", cc.AD / "v3" / "model_v31" / "replay.py",
                cc.AD / "v3" / "model_v31" / "feature_selection.py", cc.AD / "v3" / "model_v32b" / "train_v32b.py",
                cc.AD / "score_server.py", cc.C1_DIR / "train_calibrate_v33c1.py"]


def check_model_params():
    if MODEL_PARAMS.get("n_estimators") != 100 or MODEL_PARAMS.get("random_state") != 42:
        raise RuntimeError(f"fail-closed: MODEL_PARAMS가 c1과 다름 {MODEL_PARAMS}")


def c1_manifest() -> dict:
    return json.loads(tc.MANIFEST.read_text(encoding="utf-8"))


def c1_session_rows(ids) -> dict:
    """공식 c1 세션 행(매니페스트 해시·중간 빠짐 fail-closed는 c1 로더 그대로). c1 세션 = 연속 구간 1개."""
    man = c1_manifest()
    return {s: tc.load_session_rows(s, man["sessions"][s])[0] for s in ids}


def fit(train_rows, schema):
    check_model_params()
    scaler = StandardScaler()
    xs = scaler.fit_transform([apply_feature_schema(r, schema) for r in train_rows])
    return scaler, IsolationForest(**MODEL_PARAMS).fit(xs)


def calibrate(model, scaler, schema, calib_segments: dict) -> dict:
    """calib_segments = {key: [행...]} - 세션·연속 구간마다 독립(이어붙이지 않음)."""
    scores = {k: cc.score_rows(model, scaler, schema, rows) for k, rows in calib_segments.items()}
    return calibrate_threshold(scores, consecutive_threshold=tc.CONSECUTIVE, cooldown_sec=tc.COOLDOWN_SEC,
                               eval_interval_sec=tc.EVAL_INTERVAL_SEC)


# ---------------- 새 세션(수집 뒤) ----------------
load_replacements = cc.load_replacements


def session_rows(d, v, sid) -> tuple:
    """(공식 빌더 결과, M-1 사건 기록) - 행 선택 = 공식 c1 빌더(공식 기대 Pod + 실제 5 s 표본), 표본 부족 창은 c1처럼 제외."""
    evals = cc.read_evals(d / v["evidence_file"], v["run_id"])
    ident = v.get("row_selection_identity") or {}
    b = cc.live_consistent(cc.official_rows(evals, d / ident["samples_file"] if ident.get("samples_file") else None,
                                            d / ident["pod_events_file"] if ident.get("pod_events_file") else None,
                                            ident.get("active_target"), ident.get("preview_hash")), evals)
    return b, cc.m1_events(sid, evals, b)


def new_session_segments(m, sid, collection_dir, replacements, allowed) -> dict:
    """역할 확인이 먼저(VALIDATION이면 어떤 파일도 열기 전에 LeakageError). normal_valid가 아니면 segments=None.
    PREREG M-1: CALIBRATION 세션에 M-1 사건(빌더 제외 AND live scored AND interior)이 1개라도 있으면
    INVALID_CALIBRATION_ATTEMPT - 임계값 계산에 쓰지 않는다(유효성 판정과 별개로 여기서도 다시 막음)."""
    role = cc.require_role(m, sid, allowed, replacements)
    vp = Path(collection_dir) / f"{sid}-validity.json"
    if not vp.exists():
        return {"status": "no_validity_record", "segments": None}
    v = json.loads(vp.read_text(encoding="utf-8"))
    if v["status"] != "normal_valid":
        m1v = v.get("m1") or {}
        return {"status": v["status"], "segments": None,
                **({"builder": {k: m1v.get(k) for k in ("m1_count", "m1_events", "eligible_rows", "error")}} if m1v else {})}
    try:
        b, m1 = session_rows(Path(collection_dir), v, sid)
    except Exception as e:  # noqa: BLE001 - 행 선택 불가 세션은 학습·보정에서 빼고 사유를 남김(학습 전체를 멈추지 않음)
        return {"status": "row_selection_error", "segments": None, "builder": {"error": f"{type(e).__name__}: {e}"}}
    info = {**{k: b[k] for k in ("evaluations_total", "c1_complete", "excluded", "excluded_reason_counts")},
            "segments": len(cc.row_segments(b["rows"])), "m1_count": m1["m1_count"],
            "m1_events": [e for e in m1["events"] if e["counts_for_m1"]]}
    if role == "CALIBRATION" and m1["m1_count"] > 0:
        return {"status": "INVALID_CALIBRATION_ATTEMPT", "segments": None, "builder": info}
    if not b["rows"]:   # 적격 행 0개 - "사용함"으로 세지 않음(새 기준 아님: 쓸 행이 없을 뿐, 사유와 함께 기록)
        return {"status": "no_eligible_rows", "segments": None, "builder": info}
    return {"status": "normal_valid", "validity": v, "segments": cc.row_segments(b["rows"]), "builder": info}


def coverage_gate(train_validity: list) -> dict:
    uids = sorted({v["active_uid"] for v in train_validity})
    dates = sorted({v["t0_utc"][:10] for v in train_validity})
    return {"active_uids": uids, "dates": dates, "ok": len(uids) >= 2 and len(dates) >= 2}


def assemble(m, collection_dir) -> dict:
    """학습 행·보정 구간 조립(train과 c1 재현 관문이 같은 함수를 쓴다). VALIDATION 세션은 열지 않는다."""
    reps = load_replacements(m, collection_dir)
    ids = [s["id"] for s in m["sessions"]] + list(reps)
    roles = {sid: cc.session_role(m, sid, reps) for sid in ids}
    used, excluded = {"TRAIN": {}, "CALIBRATION": {}}, {}
    for sid in ids:
        if roles[sid] not in used:
            continue  # VALIDATION: 열지 않음
        r = new_session_segments(m, sid, collection_dir, reps, ("TRAIN", "CALIBRATION"))
        if r["segments"] is None:   # 버리지 않고 사유·M-1 사건 기록과 함께 남김
            excluded[sid] = {"status": r["status"], **({"row_selection": r["builder"]} if "builder" in r else {})}
        else:
            used[roles[sid]][sid] = r
    man1 = c1_manifest()
    c1_tr, c1_ca = c1_session_rows(man1["train"]), c1_session_rows(man1["calibration"])
    train_rows = [r for rows in c1_tr.values() for r in rows] + \
                 [r for x in used["TRAIN"].values() for seg in x["segments"] for r in seg]
    calib = dict(c1_ca)
    calib.update({f"{sid}#{k}": seg for sid, x in used["CALIBRATION"].items() for k, seg in enumerate(x["segments"])})
    return {"reps": reps, "ids": ids, "roles": roles, "used": used, "excluded": excluded, "c1_tr": c1_tr, "c1_ca": c1_ca,
            "train_rows": train_rows, "calib": calib}


def train(collection_dir=cc.COLLECTION_DIR, out_art=cc.C3_ART, manifest=None):  # 실제 c3 학습 - 수집 완료·사용자 승인 뒤에만
    m = cc.load_manifest(manifest)
    out_art = cc.check_write_path(out_art)
    if out_art.exists():
        raise SystemExit(f"이미 있음 - 덮어쓰지 않음: {out_art}")
    a = assemble(m, collection_dir)
    reps, ids, roles, used, excluded = a["reps"], a["ids"], a["roles"], a["used"], a["excluded"]
    c1_tr, c1_ca, train_rows, calib = a["c1_tr"], a["c1_ca"], a["train_rows"], a["calib"]
    cov = coverage_gate([r["validity"] for r in used["TRAIN"].values()])
    if not cov["ok"]:
        raise SystemExit(f"coverage 관문 미통과 - 학습하지 않음(PREREG Q): {cov}")
    schema = cc.load_c1_schema()
    scaler, model = fit(train_rows, schema)
    cal = calibrate(model, scaler, schema, calib)
    out_art.mkdir(parents=True)
    for name, obj in (("model.pkl", model), ("scaler.pkl", scaler)):
        with open(out_art / name, "xb") as fh:
            pickle.dump(obj, fh)
    cc.write_new_json(out_art / "feature-schema.json", schema)
    cc.write_new_json(out_art / "threshold.json", {
        "threshold": cal["threshold"], "calibration_failed": cal["calibration_failed"], "reason": cal.get("reason"),
        "decision_rule": "anomaly if decision_function(x) < threshold (strict)",
        "rule": "model_v31/replay.calibrate_threshold, key = 세션·연속 구간", "per_segment_result": cal.get("per_session")})
    cc.write_new_json(out_art / "freeze.json", {
        "status": "frozen", "series": "c3-normal-coverage", "plan_id": cc.PLAN_ID,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "train_sessions": {"c1": list(c1_tr), "new": list(used["TRAIN"])},
        "calibration_sessions": {"c1": list(c1_ca), "new": list(used["CALIBRATION"])},
        "validation_sessions_metadata_only": [s for s in ids if roles[s] == "VALIDATION"],
        "excluded_operational": excluded, "replacements": reps,
        "new_session_row_selection": {sid: x["builder"] for role in used.values() for sid, x in role.items()},
        # PREREG M-1: 보정에 쓴 세션의 합계(규칙상 반드시 0, 0이어도 보고). 위반 세션은 excluded_operational에 사건과 함께.
        "calibration_mid_session_builder_excluded_live_scored_total": sum(
            x["builder"]["m1_count"] for x in used["CALIBRATION"].values()),
        "row_selection_rule": "공식 c1_collection.build_c1_features(공식 expected_pods + 5 s 표본) - c1과 동일", "coverage_gate": cov, "train_rows": len(train_rows),
        "feature_schema_sha256": cc.C1_SCHEMA_SHA256, "model_params": MODEL_PARAMS,
        "feature_schema_note": "feature-schema.json은 c1 파일 그대로(특징 선택 고정) - 안의 n_train_rows·train_feature_stats는 c1 값",
        "manifest_sha256": cc.sha256(manifest or cc.MANIFEST), "c1_manifest_sha256": cc.sha256(tc.MANIFEST),
        "source_sha256": {str(p.relative_to(cc.ROOT)).replace("\\", "/"): cc.sha256(p) for p in SOURCE_FILES},
        "versions": {"python": sys.version, "platform": platform.platform(), "sklearn": sklearn.__version__,
                     "numpy": np.__version__}})
    cc.write_new_json(out_art / "SHA256SUMS.json", {q.name: cc.sha256(q) for q in sorted(out_art.iterdir()) if q.is_file()})
    return {"threshold": cal["threshold"], "calibration_failed": cal["calibration_failed"], "excluded": excluded}


# ---------------- C1 재현 관문 ----------------
def _trees_equal(a, b) -> bool:
    if len(a.estimators_) != len(b.estimators_) or a.offset_ != b.offset_ or a.max_samples_ != b.max_samples_:
        return False
    for ta, tb, fa, fb in zip(a.estimators_, b.estimators_, a.estimators_features_, b.estimators_features_):
        if not np.array_equal(fa, fb):
            return False
        x, y = ta.tree_, tb.tree_
        for k in ("feature", "threshold", "children_left", "children_right", "n_node_samples", "value"):
            if not np.array_equal(getattr(x, k), getattr(y, k)):
                return False
    return True


def decision_parity(evals, model, scaler, schema, thr, recorded) -> dict:
    """기록 features → scaler·점수·연속 재생이 기록값과 같은지. recorded(rec) → (is_anomalous, consecutive, condition_met)."""
    sc = [r for r in evals if r.get("status") == "scored"]
    rows = [cc.feature_row(r) for r in sc]
    xs = scaler.transform([apply_feature_schema(r, schema) for r in rows]) if rows else np.zeros((0, 4))
    scores = [float(v) for v in model.decision_function(xs)] if rows else []
    d_scaled = max((float(np.max(np.abs(np.asarray(r["scaled_feature_vector"]) - x))) for r, x in zip(sc, xs)), default=0.0)
    d_score = max((abs(s - r["score"]) for r, s in zip(sc, scores)), default=0.0)
    rp = cc.replay(evals, {r["evaluation_seq"]: s for r, s in zip(sc, scores)}, thr)
    mism = [x["seq"] for r, x in zip(evals, rp) if recorded(r) != (x["is_anomalous"], x["consecutive"], x["condition_met"])]
    return {"n_evals": len(evals), "n_scored": len(sc), "max_abs_scaled_diff": d_scaled, "max_abs_score_diff": d_score,
            "decision_mismatch_seqs": mism, "signals": sum(x["condition_met"] for x in rp),
            "ok": d_scaled == 0.0 and d_score == 0.0 and not mism}


def _rec_n1(r):
    b = (r.get("rules") or {}).get("B") or {}
    return (b.get("is_anomalous"), b.get("consecutive", 0), bool(b.get("condition_met"))) if r.get("status") == "scored" \
        else (None, 0, False)


def _rec_ab(r):
    return (r.get("is_anomalous"), r.get("consecutive_anomalous", 0), bool(r.get("condition_met"))) \
        if r.get("status") == "scored" else (None, 0, False)


def reproduce_c1(out_dir) -> dict:
    out = cc.check_write_path(Path(out_dir) / "c1-reproduction-gate.json")
    # c3 학습과 같은 조립 함수(새 세션 0개 = 존재하지 않는 수집 폴더) → c1 세션만 남아야 한다
    a = assemble(cc.load_manifest(), Path(out_dir) / "_no_collection_for_reproduction")
    man1 = c1_manifest()
    tr, ca = c1_session_rows(man1["train"]), c1_session_rows(man1["calibration"])
    assembled_ok = (a["train_rows"] == [r for rows in tr.values() for r in rows] and a["calib"] == ca
                    and not a["used"]["TRAIN"] and not a["used"]["CALIBRATION"])
    train_rows, ca = a["train_rows"], a["calib"]
    schema = cc.load_c1_schema()
    off = {"model": pickle.loads((cc.C1_ART / "model.pkl").read_bytes()),
           "scaler": pickle.loads((cc.C1_ART / "scaler.pkl").read_bytes()),
           "thr": json.loads((cc.C1_ART / "threshold.json").read_text(encoding="utf-8"))}
    g = {}
    same_schema = json.loads(json.dumps(compute_feature_schema(train_rows))) == schema
    g["A_feature_rows"] = {"train_rows": len(train_rows), "c1_official_n_train_rows": schema["n_train_rows"],
                           "schema_recomputed_equals_official": same_schema, "c3_assemble_equals_c1_rows": assembled_ok,
                           "ok": same_schema and assembled_ok and len(train_rows) == schema["n_train_rows"]}
    scaler, model = fit(train_rows, schema)
    g["B_scaler"] = {"ok": all(np.array_equal(getattr(scaler, k), getattr(off["scaler"], k))
                               for k in ("mean_", "var_", "scale_", "n_samples_seen_"))}
    all_rows = train_rows + [r for rows in ca.values() for r in rows]
    s_new, s_off = cc.score_rows(model, scaler, schema, all_rows), cc.score_rows(off["model"], off["scaler"], schema, all_rows)
    g["C_scores"] = {"trees_equal": _trees_equal(model, off["model"]), "n_rows": len(all_rows),
                     "max_abs_score_diff": max(abs(a - b) for a, b in zip(s_new, s_off))}
    g["C_scores"]["ok"] = g["C_scores"]["trees_equal"] and g["C_scores"]["max_abs_score_diff"] == 0.0
    cal = calibrate(model, scaler, schema, ca)
    keys = ("n_points", "point_anomaly_count", "signal_count", "max_consecutive_anomalous", "score_min", "score_max")
    per_eq = all({k: cal["per_session"][s][k] for k in keys} == {k: off["thr"]["per_session_result"][s][k] for k in keys}
                 for s in ca)
    g["D_threshold"] = {"reproduced": cal["threshold"], "official": cc.C1_THRESHOLD, "per_session_equal": per_eq,
                        "ok": cal["threshold"] == cc.C1_THRESHOLD == off["thr"]["threshold"] and per_eq
                        and not cal["calibration_failed"]}
    seq = {}
    for p in sorted(N1_DIR.glob("c1-live-evidence-guarded_normal_record_only-nb0*.jsonl")):
        seq[p.name] = decision_parity(cc.read_evals(p), model, scaler, schema, cal["threshold"], _rec_n1)
    for p in sorted(AB_DIR.glob("c1-live-evidence-guarded_pressure_record_only-ab0?a-*.jsonl")):
        seq[p.name] = decision_parity(cc.read_evals(p), model, scaler, schema, cal["threshold"], _rec_ab)
    g["E_decision_sequence"] = {"files": seq, "n_files": len(seq), "ok": len(seq) > 0 and all(v["ok"] for v in seq.values())}
    verdict = "C1_REPRODUCTION_GATE_PASS" if all(v["ok"] for v in g.values()) else "C1_REPRODUCTION_GATE_FAIL"
    res = {"verdict": verdict, "checked_at_utc": datetime.now(timezone.utc).isoformat(), "gate": g,
           "note": "공식 c1 artifact·특징·evidence는 읽기만. 재현 모델은 메모리에만 있고 저장하지 않음",
           "versions": {"python": sys.version, "sklearn": sklearn.__version__, "numpy": np.__version__}}
    cc.write_new_json(out, res)
    return res


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="c3 학습·보정 / c1 재현 관문 (클러스터 접근 없음, VALIDATION 경로 인자 없음)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--reproduce-c1", action="store_true")
    g.add_argument("--train", action="store_true")
    p.add_argument("--out", help="--reproduce-c1 결과 폴더(experiments/results/c3_normal_coverage* 아래)")
    a = p.parse_args(argv)
    if a.reproduce_c1:
        if not a.out:
            raise SystemExit("--out 필요")
        r = reproduce_c1(a.out)
        print(json.dumps({"verdict": r["verdict"], **{k: v["ok"] for k, v in r["gate"].items()}}, ensure_ascii=False))
        return 0 if r["verdict"].endswith("PASS") else 1
    print(json.dumps(train(), ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
