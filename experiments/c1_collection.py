#!/usr/bin/env python3
"""2026-10-04 새 모델 계열 v3.3-c1 정상 자료 수집(학습·보정 세션) - 계획: docs/design/phase8-c1-normal-collection-plan-20261004.md

세션 1회 = 기존 무주입 관찰 경로(run_v1_detector_trial.run_observation: preview 준비·정리, V2 사전검사·PC commit·실행 중
감시·probe 이미지 확인, SLO 위반 시 즉시 중단, 압박 없음) + 탐지기 원문 기록(SCORE_SERVER_RAW_QUERY_CAPTURE=1)·후보 병렬
기록(SCORE_SERVER_PARALLEL_CANDIDATE=1). 탐지기는 동결 모델 그대로 sink에만 연결된다(실제 정책·승격 없음).
c1 특징은 저장된 실시간 원문으로만 만든다(build_c1_features): cpu = 후보 rate[60s], memory·queue·cache = 같은 평가의 첫
응답. 네 지표·기대 Pod·UID·전체 격자(input_integrity.window_features)와 네 지표 timestamp() 신선도를 모두 통과한 평가만
특징으로 남기고, 나머지는 사유와 함께 제외 목록에 남긴다(조용히 버리지 않음). 재학습·임계값 결정은 하지 않는다."""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import run_v1_detector_trial as v1

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "anomaly-detection"))
import input_integrity as ii  # noqa: E402
import input_capture_analysis as ica  # noqa: E402
from features import FEATURE_NAMES  # noqa: E402

SERIES_ID = "v3.3-c1"
PLAN_ID = "capstone-c1-normal-20261004"
PLAN_DOC = "docs/design/phase8-c1-normal-collection-plan-20261004.md"
SCENARIO = "c1_normal"
RESULTS_DIR = Path(__file__).parent / "results" / "capstone_c1_normal_collection"
SESSION_SEC = 1800.0
REGIME = "serve_1rps"  # probe 1RPS = 실제 vLLM 요청 처리(요청 없는 유휴 상태가 아님)
# 수집 순서(학습·보정을 섞어 시간대 편향을 줄임). 역할은 수집 전 고정 - 결과를 보고 옮기지 않는다.
SESSIONS = {
    "c1tr01": ("train", 501), "c1ca01": ("calibration", 511), "c1tr02": ("train", 502), "c1tr03": ("train", 503),
    "c1ca02": ("calibration", 512), "c1tr04": ("train", 504), "c1tr05": ("train", 505), "c1ca03": ("calibration", 513),
    "c1tr06": ("train", 506), "c1ca04": ("calibration", 514),
}
MIN_C1_COMPLETE_RATIO = 0.9  # 세션의 c1 사용 가능 평가 비율 하한(수집 전 고정, 분모 = 건너뜀 포함 전체 평가)
CAPTURE_ENV, PARALLEL_ENV = "SCORE_SERVER_RAW_QUERY_CAPTURE", "SCORE_SERVER_PARALLEL_CANDIDATE"
# 시작 절차(계획 문서 §6). v1 = 탐지기 기동 직후 관찰 시작(c1tr01·c1ca01·c1tr02·c1tr03). v2 = 첫 정상 평가 기록(특징 창이
# 첫 Pod 상태 표본 이후) 확인 뒤 관찰 시작, 대기 상한 300초(c1ca02부터). 근거: 기동→첫 평가 지연 과거 14회 최대 25.6초 +
# 이상치 158.9초(c1tr03), 첫 표본 이후 창까지 60초 창 + 평가 간격 약 17초 - 이상치가 재발해도 통과하는 유한한 값.
START_PROCEDURE = "c1-start-v2-20261005"
READY_TIMEOUT_SEC = 300.0


def run_id_for(session: str) -> str:
    if session not in SESSIONS:
        raise ValueError(f"알 수 없는 세션: {session!r} (허용: {list(SESSIONS)})")
    return f"{SCENARIO}-{session}-{PLAN_ID}"


def _contract(session: str, run_id: str) -> dict:
    role, rep = SESSIONS[session]
    return {"run_id": run_id, "series": SERIES_ID, "plan_id": PLAN_ID, "plan_doc": PLAN_DOC, "session": session, "rep": rep,
            "role": role, "regime": REGIME, "observation_sec_requested": SESSION_SEC,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "input_rule": "input-candidate-c1-20261004 (CPU rate[60s], 기대 Pod·UID·전체 격자, 시간축 유지, Pod별 신선도)",
            "detector": "동결 score_server.py(후보 artifacts) sink 전용 - 원문 기록용, 실제 정책·승격 없음",
            "detector_env": {CAPTURE_ENV: "1", PARALLEL_ENV: "1"}, "stress": None, "load": "probe 1RPS(실제 요청 처리)",
            "start_procedure": START_PROCEDURE, "detector_ready_timeout_sec": READY_TIMEOUT_SEC,
            "placement": "active = 시작 시 active(sj-worker 예상), preview = 기존 준비 경로의 반대 워커(hard anti-affinity, 사후 확인)",
            "probe_image": v1.IMAGE_FORCE_CLOSE, "probe_image_id": v1.IMAGE_FORCE_CLOSE_ID,
            "validity": ("완료(관찰 시간 채움)·정리 오류 없음·probe 이미지 확인·probe 자료 공백 없음·관찰 중 SLO 위반 없음"
                         f"(위반 시 즉시 중단)·c1 사용 가능 평가 ≥ {MIN_C1_COMPLETE_RATIO:.0%}(미달 세션도 보존·보고)"),
            "not_mixed_with": "V1·V2·icd01·cpd01(동결 계열 결과·개발 자료) 및 다른 역할 세션"}


def run_session(session: str, *, preflight_only: bool = False, dry_run: bool = False) -> int:
    run_id = run_id_for(session)
    role, rep = SESSIONS[session]
    os.environ.update({CAPTURE_ENV: "1", PARALLEL_ENV: "1"})  # 탐지기 하위 프로세스가 물려받는다
    return v1.run_observation(run_id=run_id, rep=rep, results_dir=RESULTS_DIR, contract=_contract(session, run_id),
                              observation_sec=SESSION_SEC, scenario=SCENARIO,
                              summary_extra={"series": SERIES_ID, "plan_id": PLAN_ID, "session": session, "role": role,
                                             "regime": REGIME, "start_procedure": START_PROCEDURE},
                              preflight_only=preflight_only, dry_run=dry_run, detector_ready_timeout_sec=READY_TIMEOUT_SEC)


class _Stored:  # 저장된 timestamp() 응답 본문을 check_freshness_by_pod의 응답 검증에 그대로 통과시키는 껍데기
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


def _ts(x):
    return datetime.fromisoformat(str(x).replace("Z", "+00:00"))


def _excluded(seq, reason):
    return {"seq": seq, "reason": reason}


def build_c1_features(results_dir: Path, run_id: str, target: dict = None) -> dict:
    """저장된 실시간 원문만으로 c1 특징 행을 만든다(사후 조회 없음). 건너뛴 평가까지 분모로 세고 제외 사유를 남긴다."""
    s = json.loads((results_dir / f"summary-{run_id}.json").read_text(encoding="utf-8"))
    samples = [json.loads(x) for x in (results_dir / f"samples-{run_id}.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    ev = results_dir / f"pod-events-{run_id}.json"
    events = json.loads(ev.read_text(encoding="utf-8")) if ev.exists() else []
    # target: 기대 active Pod(이름·UID). 기본은 c1 실행 요약의 active_target, 압박 실행 기록(cpd01 등)은 호출자가
    # stress_delivery_record.target을 넘긴다(2026-10-05 추가, 같은 c1 검사 재사용).
    target = target if target is not None else s.get("active_target")
    phash = (s.get("preview_prep") or {}).get("created_pod_hash")
    rows, excluded = [], []
    for line in (results_dir / f"detector-evidence-{run_id}.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        seq = r.get("evaluation_seq")
        if r.get("record_type") == "evaluation_skipped":
            excluded.append(_excluded(seq, "evaluation_skipped"))
            continue
        if r.get("record_type") != "evaluation_decision":
            continue
        prov, pc = r.get("per_metric_provenance") or {}, r.get("parallel_candidate") or {}
        cand = next((q for q in pc.get("cpu_queries", []) if q.get("label") == "candidate_rate60"), None)
        if cand is None or cand.get("error"):
            excluded.append(_excluded(seq, "candidate_cpu_query_missing_or_failed"))
            continue
        if (cand.get("start_utc"), cand.get("end_utc")) != (r.get("window_start_utc"), r.get("window_end_utc")):
            excluded.append(_excluded(seq, "candidate_window_mismatch"))
            continue
        exp = ica.expected_pods(r["window_start_utc"], r["window_end_utc"], samples, target, phash, events)
        if exp["pods"] is None:
            excluded.append(_excluded(seq, f"expected_unverified:{exp['reason']}"))
            continue
        raw = {"cpu": cand.get("series") or []}
        for m in ("memory", "queue", "cache"):
            cap = (prov.get(m) or {}).get("raw_query_capture")
            if cap is not None:
                raw[m] = cap.get("series") or []
        w = ii.window_features(raw, exp["pods"], _ts(r["window_start_utc"]), _ts(r["window_end_utc"]),
                               runtime_uid_evidence=exp["pods"])
        if w["features"] is None:
            excluded.append(_excluded(seq, f"incomplete:{w['reason']}"))
            continue
        fqs = pc.get("freshness_queries", [])
        bad = [] if sorted(q.get("metric") for q in fqs) == sorted(ii.REQUIRED_METRICS) else ["freshness_queries_missing"]
        for fq in fqs:
            if fq.get("error"):
                bad.append(f"{fq['metric']}:query_failed")
                continue
            f = ii.check_freshness_by_pod("stored", exp["pods"], runtime_uid_evidence=exp["pods"],
                                          get_fn=lambda *a, b=fq.get("body"), **k: _Stored(b),
                                          now_fn=lambda q=fq: _ts(q["query_received_at_utc"]).timestamp())
            if not f["fresh"]:
                bad.append(f"{fq['metric']}:{f['reason_kind']}")
        if bad:
            excluded.append(_excluded(seq, f"not_fresh:{bad}"))
            continue
        rows.append({"seq": seq, "window_start_utc": r["window_start_utc"], "window_end_utc": r["window_end_utc"],
                     "feature_computed_at_utc": r.get("feature_computed_at_utc"),
                     "features": dict(zip(FEATURE_NAMES, w["features"])),
                     "identity": {m: v.get("identity") for m, v in w["per_metric"].items()},
                     "cpu_candidate_query_utc": [cand.get("query_sent_at_utc"), cand.get("query_received_at_utc")]})
    total = len(rows) + len(excluded)
    reasons = {}
    for e in excluded:
        k = e["reason"].split(":")[0]
        reasons[k] = reasons.get(k, 0) + 1
    ratio = len(rows) / total if total else None
    return {"run_id": run_id, "series": SERIES_ID, "session": s.get("session"), "role": s.get("role"),
            "input_rule": "input-candidate-c1-20261004", "feature_names": FEATURE_NAMES,
            "evaluations_total": total, "c1_complete": len(rows), "excluded": len(excluded),
            "c1_complete_ratio": round(ratio, 3) if ratio is not None else None,
            "meets_min_complete_ratio": bool(ratio is not None and ratio >= MIN_C1_COMPLETE_RATIO),
            "excluded_reason_counts": reasons, "excluded_rows": excluded, "rows": rows}


def main():
    p = argparse.ArgumentParser(description="v3.3-c1 정상 자료 수집 세션 1회 또는 수집 뒤 c1 특징 빌드")
    p.add_argument("--session", required=True, choices=list(SESSIONS))
    p.add_argument("--preflight-only", action="store_true")
    p.add_argument("--dry-run-contract-only", action="store_true")
    p.add_argument("--build-features-out", type=Path, help="수집 뒤: c1 특징 파일 경로(새 파일만, 덮어쓰지 않음)")
    a = p.parse_args()
    if a.build_features_out:
        res = build_c1_features(RESULTS_DIR, run_id_for(a.session))
        with open(a.build_features_out, "x", encoding="utf-8") as f:
            f.write(json.dumps(res, ensure_ascii=False, indent=1))
        print(json.dumps({k: res[k] for k in ("evaluations_total", "c1_complete", "excluded", "c1_complete_ratio",
                                               "meets_min_complete_ratio", "excluded_reason_counts")}, ensure_ascii=False, indent=1))
        return
    sys.exit(run_session(a.session, preflight_only=a.preflight_only, dry_run=a.dry_run_contract_only))


if __name__ == "__main__":
    main()
