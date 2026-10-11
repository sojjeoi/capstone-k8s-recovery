#!/usr/bin/env python3
"""v3.3-c1 오프라인 재학습·보정(2026-10-05) - 계약: docs/design/phase8-v33-c1-training-contract-20261005.md

v3.2b 규칙을 그대로 쓴다(재구현 없음):
  - 특징 선택: model_v31/feature_selection.compute_feature_schema(학습 행만, 정확히 분산 0인 특징만 제거, 순서 유지)
  - scaler: StandardScaler 기본값 / 모델: IsolationForest(train_v32b.MODEL_PARAMS, random_state=42)
  - 임계값: model_v31/replay.calibrate_threshold(보정 세션마다 독립, 시간순 점수, 3연속·쿨다운 60초 - 모든 보정 세션에서
    경고 0인 가장 높은 관측 점수, 아무 점도 이상으로 못 잡으면 calibration_failed)
입력: experiments/analysis_v1의 c1 특징 파일(빌더가 사용 가능으로 판정한 행만). 세션별 행은 평가 순서(seq)대로.
모드: --write-manifest(실행 전 자료 목록·해시 고정, 새 파일만) / 기본(매니페스트 해시 확인 뒤 학습→보정→민감도→저장→검증)."""
import argparse
import hashlib
import json
import pickle
import platform
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
V3 = HERE.parent
ROOT = V3.parent.parent
EXP = ROOT / "experiments"
sys.path.insert(0, str(V3 / "model_v31"))
sys.path.insert(0, str(V3 / "model_v32b"))
sys.path.insert(0, str(EXP))
sys.stdout.reconfigure(encoding="utf-8")

import numpy  # noqa: E402
import sklearn  # noqa: E402
from sklearn.ensemble import IsolationForest  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

from feature_selection import FEATURE_NAMES, apply_feature_schema, compute_feature_schema  # noqa: E402
from replay import calibrate_threshold  # noqa: E402
from train_v32b import MODEL_PARAMS  # noqa: E402
import c1_collection as c1  # noqa: E402

MODEL_VERSION = "v3.3-c1"
MANIFEST = HERE / "c1-data-manifest.json"
ARTIFACTS = HERE / "artifacts"
FEATURE_FILES = {"c1tr01": "c1-features-c1tr01-20261004.json", "c1ca01": "c1-features-c1ca01-20261004.json"}
RAW_FILES = (("summary", "json"), ("detector-evidence", "jsonl"), ("samples", "jsonl"), ("pod-events", "json"))
CONSECUTIVE, COOLDOWN_SEC, EVAL_INTERVAL_SEC = 3, 60.0, 15.0  # v3.2b threshold.json·replay 기본값 그대로
# 민감도(사전 고정): 기준 행 = 학습 행 중 cpu_mean이 학습 중앙값에 가장 가까운 실제 행(동률이면 세션 목록·seq 순 첫 행).
# cpu_mean만 아래 값으로 바꾸고 나머지 7개는 기준 행 그대로. 학습 최솟값·중앙값·최댓값은 실행 시 학습 통계로 채운다.
SENS_CPU_ABS = [2.0, 2.5, 3.0, 4.0, 6.0]
SENS_CPU_ABOVE_MAX = 0.1


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def feature_path(s: str) -> Path:
    return EXP / "analysis_v1" / FEATURE_FILES.get(s, f"c1-features-{s}-20261005.json")


def write_manifest():
    sess = {}
    for s, (role, _rep) in c1.SESSIONS.items():
        rid = c1.run_id_for(s)
        fp = feature_path(s)
        f = json.loads(fp.read_text(encoding="utf-8"))
        summ = json.loads((c1.RESULTS_DIR / f"summary-{rid}.json").read_text(encoding="utf-8"))
        sess[s] = {"role": role, "run_id": rid,
                   "start_procedure": summ.get("start_procedure", "c1-start-v1(관찰 시작 = 탐지기 기동)"),
                   "features_file": str(fp.relative_to(ROOT)).replace("\\", "/"), "features_sha256": sha256(fp),
                   "raw_files_sha256": {n: sha256(c1.RESULTS_DIR / f"{n}-{rid}.{ext}") for n, ext in RAW_FILES},
                   "usable_rows": f["c1_complete"], "evaluations_total": f["evaluations_total"]}
    with open(MANIFEST, "x", encoding="utf-8") as fh:
        fh.write(json.dumps({"series": MODEL_VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
                             "train": [s for s in c1.SESSIONS if c1.SESSIONS[s][0] == "train"],
                             "calibration": [s for s in c1.SESSIONS if c1.SESSIONS[s][0] == "calibration"],
                             "sessions": sess}, ensure_ascii=False, indent=1))
    print(f"매니페스트 저장: {MANIFEST}")


def _ts(x):
    return datetime.fromisoformat(str(x).replace("Z", "+00:00"))


def load_session_rows(s: str, entry: dict) -> tuple:
    """(행 목록[8개 float, FEATURE_NAMES 순서], 기록). 해시 불일치·이름 순서·중간 빠짐은 fail-closed."""
    fp = ROOT / entry["features_file"]
    if sha256(fp) != entry["features_sha256"]:
        raise RuntimeError(f"fail-closed: {s} 특징 파일 해시가 매니페스트와 다름")
    f = json.loads(fp.read_text(encoding="utf-8"))
    if f["feature_names"] != FEATURE_NAMES:
        raise RuntimeError(f"fail-closed: {s} 특징 이름·순서가 FEATURE_NAMES와 다름: {f['feature_names']}")
    rows = sorted(f["rows"], key=lambda r: r["seq"])
    seqs = [r["seq"] for r in rows]
    if seqs != list(range(seqs[0], seqs[0] + len(seqs))):
        raise RuntimeError(f"fail-closed: {s} 사용 가능 행 중간에 빠진 평가가 있음(계약에 없는 경우)")
    summ = json.loads((c1.RESULTS_DIR / f"summary-{entry['run_id']}.json").read_text(encoding="utf-8"))
    end = _ts(summ["observation_ended_at_utc"])
    start = (_ts(summ["observation_started_at_utc"]) if summ.get("observation_started_at_utc")
             else end - timedelta(seconds=summ["observation_sec_requested"]))  # 절차 v1: 기동 = 관찰 시작
    outside = sum(1 for r in rows if not start <= _ts(r["feature_computed_at_utc"]) <= end)
    return [[r["features"][n] for n in FEATURE_NAMES] for r in rows], {
        "usable_rows": len(rows), "evaluations_total": f["evaluations_total"], "excluded": f["excluded_reason_counts"],
        "start_procedure": entry["start_procedure"], "usable_rows_outside_observation": outside,
        "first_seq": seqs[0], "last_seq": seqs[-1]}


def scores_for(rows, model, scaler, schema):
    return [float(v) for v in model.decision_function(scaler.transform([apply_feature_schema(r, schema) for r in rows]))]


def sensitivity(model, scaler, schema, train_rows, threshold):
    ci = FEATURE_NAMES.index("cpu_mean")
    cpu = [r[ci] for r in train_rows]
    med, top = statistics.median(cpu), max(cpu)
    base = min(train_rows, key=lambda r: abs(r[ci] - med))  # min()은 동률이면 첫 행(세션 목록·seq 순)
    points = [("train_min", min(cpu)), ("train_median", med), ("train_max", top),
              ("train_max+0.1", top + SENS_CPU_ABOVE_MAX)] + [(str(v), v) for v in SENS_CPU_ABS]
    out = []
    for label, v in points:
        row = list(base)
        row[ci] = v
        sc = scores_for([row], model, scaler, schema)[0]
        out.append({"label": label, "cpu_mean": v, "score": sc, "below_threshold": sc < threshold})
    beyond = [q for q in out if q["cpu_mean"] > top]
    at_max = next(q["score"] for q in out if q["label"] == "train_max")
    return {"base_row": dict(zip(FEATURE_NAMES, base)), "base_rule": "cpu_mean이 학습 중앙값에 가장 가까운 실제 학습 행",
            "points": out, "threshold": threshold,
            "saturated_beyond_train_max": len({round(q["score"], 12) for q in beyond}) == 1,
            "score_at_train_max": at_max,
            "beyond_max_scores_below_threshold": [q["label"] for q in beyond if q["below_threshold"]],
            "beyond_max_less_anomalous_than_train_max": [q["label"] for q in beyond if q["score"] > at_max],
            "note": "합성 입력 민감도 확인 - 압박 탐지 가능성을 입증하지 않으며, 임계값·특징·모델 조정에 쓰지 않음"}


def _by_proc(info):
    out = {}
    for i in info.values():
        b = out.setdefault(i["start_procedure"], {"sessions": 0, "usable_rows": 0, "usable_rows_outside_observation": 0})
        b["sessions"] += 1
        b["usable_rows"] += i["usable_rows"]
        b["usable_rows_outside_observation"] += i["usable_rows_outside_observation"]
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--write-manifest", action="store_true")
    if p.parse_args().write_manifest:
        return write_manifest()
    if ARTIFACTS.exists():
        raise SystemExit(f"이미 있음 - 덮어쓰지 않음: {ARTIFACTS}")
    man = json.loads(MANIFEST.read_text(encoding="utf-8"))
    train_ids, calib_ids = man["train"], man["calibration"]
    if set(train_ids) & set(calib_ids) or len(train_ids) != 6 or len(calib_ids) != 4:
        raise RuntimeError("fail-closed: 학습·보정 세션 목록이 계약과 다름")
    for s in train_ids + calib_ids:
        e = man["sessions"][s]
        for n, ext in RAW_FILES:
            if sha256(c1.RESULTS_DIR / f"{n}-{e['run_id']}.{ext}") != e["raw_files_sha256"][n]:
                raise RuntimeError(f"fail-closed: {s} 원자료 {n} 해시가 매니페스트와 다름")

    # 1) 학습 - 학습 세션 행만으로 특징 선택·scaler·모델
    train_rows, train_info = [], {}
    for s in train_ids:
        rows, info = load_session_rows(s, man["sessions"][s])
        train_rows += rows
        train_info[s] = info
    schema = compute_feature_schema(train_rows)
    scaler = StandardScaler()
    Xs = scaler.fit_transform([apply_feature_schema(r, schema) for r in train_rows])
    model = IsolationForest(**MODEL_PARAMS)
    model.fit(Xs)
    print(f"학습 행 {len(train_rows)}(세션 {len(train_ids)}) / kept {schema['kept_feature_names']} / "
          f"removed {[r['name'] for r in schema['removed_features']]}")

    # 2) 보정 - 모델 고정 뒤 보정 세션만, 세션별 독립 시간순 점수로 1회
    calib_scores, calib_info = {}, {}
    for s in calib_ids:
        rows, info = load_session_rows(s, man["sessions"][s])
        calib_scores[s] = scores_for(rows, model, scaler, schema)
        calib_info[s] = info
    cal = calibrate_threshold(calib_scores, consecutive_threshold=CONSECUTIVE, cooldown_sec=COOLDOWN_SEC,
                              eval_interval_sec=EVAL_INTERVAL_SEC)
    thr = cal["threshold"]
    print(f"보정: calibration_failed={cal['calibration_failed']} threshold={thr} reason={cal.get('reason')}")

    ARTIFACTS.mkdir()
    for name, obj in (("model.pkl", model), ("scaler.pkl", scaler)):
        with open(ARTIFACTS / name, "xb") as fh:
            pickle.dump(obj, fh)

    def wj(name, obj):
        with open(ARTIFACTS / name, "x", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False, indent=2, default=str))

    per = {s: {k: v for k, v in r.items()} for s, r in (cal.get("per_session") or {}).items()}
    wj("feature-schema.json", schema)
    wj("threshold.json", {"threshold": thr, "calibration_failed": cal["calibration_failed"], "reason": cal.get("reason"),
                          "decision_rule": "anomaly if decision_function(x) < threshold (strict)",
                          "rule": "model_v31/replay.calibrate_threshold - 모든 보정 세션에서 경고(3연속) 0인 가장 높은 관측 점수",
                          "consecutive_threshold": CONSECUTIVE, "cooldown_sec": COOLDOWN_SEC, "eval_interval_sec": EVAL_INTERVAL_SEC,
                          "eval_interval_note": "실제 c1 평가 간격 약 17초. 경고 0 조건은 3연속 여부로만 정해져 쿨다운·간격 값은 결과에 영향 없음",
                          "calibration_sessions": calib_ids, "per_session_result": per,
                          "note": "보정 자료의 경고 수는 임계값 산정 결과이며 독립 검증 성능이 아님"})
    sens = sensitivity(model, scaler, schema, train_rows, thr) if thr is not None and not cal["calibration_failed"] else None
    wj("sensitivity-check.json", sens)
    wj("split-manifest.json", {"train_sessions": train_ids, "calibration_sessions": calib_ids,
                               "train_rows_by_session": train_info, "calibration_rows_by_session": calib_info,
                               "train_total_rows": len(train_rows),
                               "calibration_total_rows": sum(i["usable_rows"] for i in calib_info.values()),
                               "rows_by_start_procedure": _by_proc({**train_info, **calib_info}),
                               "note": "독립 검증 자료는 이후 새 실행 ID로 수집(여기 없음). V1/V2/icd01/cpd01·기존 학습 자료 미사용"})
    wj("dataset-manifest.json", {"manifest_file": str(MANIFEST.relative_to(ROOT)).replace("\\", "/"),
                                 "manifest_sha256": sha256(MANIFEST), "sessions": man["sessions"]})
    wj("training-metadata.json", {"model_version": MODEL_VERSION, "input_rule": "input-candidate-c1-20261004",
                                  "model_params": MODEL_PARAMS, "scaler_class": "sklearn.preprocessing.StandardScaler (defaults)",
                                  "feature_selection": "model_v31/feature_selection.compute_feature_schema (train only)",
                                  "python_version": sys.version, "platform": platform.platform(),
                                  "sklearn_version": sklearn.__version__, "numpy_version": numpy.__version__,
                                  "model_offset_sklearn_internal_not_used_as_threshold": float(model.offset_),
                                  "created_at": datetime.now(timezone.utc).isoformat()})

    # 3) 저장한 파일을 다시 불러와 같은 점수가 나오는지, 순서·분리 확인
    m2 = pickle.loads((ARTIFACTS / "model.pkl").read_bytes())
    s2 = pickle.loads((ARTIFACTS / "scaler.pkl").read_bytes())
    sc2 = json.loads((ARTIFACTS / "feature-schema.json").read_text(encoding="utf-8"))
    d_train = max(abs(x - y) for x, y in zip(scores_for(train_rows, model, scaler, schema), scores_for(train_rows, m2, s2, sc2)))
    d_cal = max(abs(x - y) for s in calib_ids for x, y in
                zip(calib_scores[s], scores_for(load_session_rows(s, man["sessions"][s])[0], m2, s2, sc2)))
    verify = {"reload_max_abs_score_diff_train": d_train, "reload_max_abs_score_diff_calibration": d_cal,
              "feature_order_is_ordered_subset_of_FEATURE_NAMES":
                  sc2["kept_feature_names"] == [FEATURE_NAMES[i] for i in sc2["kept_feature_indices"]]
                  and sc2["kept_feature_indices"] == sorted(sc2["kept_feature_indices"]),
              "schema_fit_on_train_rows_only": bool(sc2["n_train_rows"] == len(train_rows)
                                                    and int(s2.n_samples_seen_) == len(train_rows)),
              "train_calibration_sessions_disjoint": not set(train_ids) & set(calib_ids)}
    wj("verification.json", verify)
    wj("SHA256SUMS.json", {q.name: sha256(q) for q in sorted(ARTIFACTS.iterdir()) if q.is_file()})
    print(json.dumps({"verify": verify, "threshold": thr,
                      "per_session": {s: {k: r[k] for k in ("n_points", "point_anomaly_count", "signal_count",
                                                              "max_consecutive_anomalous", "score_min", "score_max")}
                                      for s, r in per.items()},
                      "sensitivity": sens}, ensure_ascii=False, indent=1, default=str))
    if d_train != 0 or d_cal != 0 or not all(v for v in verify.values() if isinstance(v, bool)):
        raise SystemExit("검증 실패")


if __name__ == "__main__":
    main()
