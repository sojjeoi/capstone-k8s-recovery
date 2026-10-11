#!/usr/bin/env python3
"""c3 학습 행 선택 불변 검증 v2(2026-10-10, 오프라인·읽기 전용) - 공식 c1 행 선택 대 c3 학습 조립 경로.

c3 새 세션 행 = c3_common.official_rows: 공식 c1_collection.build_c1_features를 그대로 호출(기대 Pod = 공식
input_capture_analysis.expected_pods + 세션의 실제 5 s 표본·Pod 이벤트). 별도 선택 로직 없음.
 LEVEL 1 특징·평가 판정: 기대 Pod 단계만 live 판정을 넘겨 빌더를 돌리고 live 기록과 비교 - 평가 순서·창 시각·사용/제외·
         제외 사유·특징 8개 값(어댑터가 원 응답을 그대로 옮기는지 = 빌더 입력 충실도).
 LEVEL 2 기대 Pod 적격성(최종 행): c3 경로(빌더 + 실제 표본) 행 집합 대 기준 = "live 평가 사용 가능 AND 공식 expected_pods가
         live와 같은 Pod로 확인"(표본 없음·창 미포함은 공식 규칙대로 제외). 표본 파일이 없는 세션은 공식 규칙상 전부 제외.
증명 범위(정확한 표현): live 형식 증거에서 "c3 경로 = 공식 c1 빌더·기대 Pod 규칙"이 성립함을 보인다. c1 학습·보정 세션은
live 형식 증거가 없어(구 score_server 경로) 그 행과 직접 대조하지는 못한다 - c3 행은 그 행을 만든 같은 공식 함수로 만든다.
표본이 있는 파일 중 v3vs06·v3vs12는 양쪽 모두 0행이라 증거가 되지 않는다.
이전 v1 결과(행 선택 불일치 65창 = 기대 Pod 표본 가용성 차이에 따른 행 적격성 불일치)는 c3-row-selection-invariant.json에
그대로 둔다. 결과는 c3 tooling 폴더에만 쓴다. 학습·튜닝에 쓰지 않는다."""
import sys
sys.dont_write_bytecode = True
import argparse  # noqa: E402
import contextlib  # noqa: E402
import json  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

import c3_common as cc  # noqa: E402

import c1_collection as c1b  # noqa: E402  (공식 빌더 - 수정 없음)
import input_capture_analysis as ica  # noqa: E402

RES = cc.EXP / "results"
OUT = cc.TOOLING_DIR / "c3-row-selection-invariant-v2.json"
LIVE_GLOBS = ("*/c1-live-evidence-*.jsonl", "*/c1-shadow-evidence-*.jsonl")
STARTUP_SEC = 120.0
REASON_MAP = {"query_failed:cpu": "candidate_cpu_query_missing_or_failed", "pod_snapshot_failed": "evaluation_skipped"}


@contextlib.contextmanager
def _live_expected(by_window: dict):
    orig = c1b.ica.expected_pods
    c1b.ica.expected_pods = lambda ws, we, *a: by_window[(ws, we)]
    try:
        yield
    finally:
        c1b.ica.expected_pods = orig


def _norm_live_reason(reason: str) -> str:
    for k, v in REASON_MAP.items():
        if reason.startswith(k):
            return v
    return "incomplete" if reason.startswith("query_failed:") else reason


def level1(evals: list) -> dict:
    by_window = {(r["window_start_utc"], r["window_end_utc"]): {"pods": r.get("expected_pods"), "reason": r.get("expected_reason")}
                 for r in evals if "window_start_utc" in r}
    with tempfile.TemporaryDirectory() as td, _live_expected(by_window):
        d, rid = Path(td), "fixture"
        (d / f"summary-{rid}.json").write_text(json.dumps({"active_target": None}), encoding="utf-8")
        (d / f"samples-{rid}.jsonl").write_text("", encoding="utf-8")
        (d / f"detector-evidence-{rid}.jsonl").write_text(
            "\n".join(json.dumps(cc.builder_adapter_line(r)) for r in evals), encoding="utf-8")
        b = c1b.build_c1_features(d, rid)
    brow, bexc = {x["seq"]: x for x in b["rows"]}, {x["seq"]: x["reason"] for x in b["excluded_rows"]}
    mism = []
    for r in evals:
        seq = r["evaluation_seq"]
        if r["status"] == "scored":
            x = brow.get(seq)
            if x is None:
                mism.append({"seq": seq, "what": "live_usable_builder_excluded", "builder": bexc.get(seq)})
            elif [x["features"][n] for n in cc.FEATURE_NAMES] != cc.feature_row(r) or \
                    (x["window_start_utc"], x["window_end_utc"]) != (r["window_start_utc"], r["window_end_utc"]):
                mism.append({"seq": seq, "what": "feature_or_window_differs"})
        elif seq in brow:
            mism.append({"seq": seq, "what": "live_skipped_builder_usable", "live": r.get("skip_reason")})
        else:
            lr, br = _norm_live_reason(r.get("skip_reason") or ""), bexc.get(seq, "")
            if not (br == lr or (lr == "incomplete" and br.startswith("incomplete"))):
                mism.append({"seq": seq, "what": "skip_reason_differs", "live": r.get("skip_reason"), "builder": br})
    order_ok = [x["seq"] for x in b["rows"]] == sorted(x["seq"] for x in b["rows"])
    return {"n_mismatch": len(mism), "mismatches": mism[:20], "order_ok": order_ok}


def _session_inputs(path: Path) -> dict:
    """공식 빌더 입력(표본·이벤트·요약의 active_target·preview hash) - 없으면 None(공식 규칙상 기대 Pod 확인 불가)."""
    rid = path.name.split("-evidence-", 1)[1][:-len(".jsonl")]
    d = path.parent
    sp, smp, ev = d / f"samples-{rid}.jsonl", d / f"summary-{rid}.json", d / f"pod-events-{rid}.json"
    s = json.loads(smp.read_text(encoding="utf-8")) if smp.exists() else {}
    return {"samples": sp if sp.exists() else None, "events": ev if ev.exists() else None,
            "target": s.get("active_target") or ((s.get("stress_delivery_record") or {}).get("target")),
            "phash": (s.get("preview_prep") or {}).get("created_pod_hash")}


def level2(evals: list, inp: dict) -> dict:
    samples = [json.loads(x) for x in inp["samples"].read_text(encoding="utf-8").splitlines() if x.strip()] \
        if inp["samples"] else []
    events = json.loads(inp["events"].read_text(encoding="utf-8")) if inp["events"] else []
    expected_rows, avail = {}, {"windows": 0, "identity_resolved": 0, "excluded_by_reason": {}}
    for r in evals:
        if "window_start_utc" not in r:
            continue
        avail["windows"] += 1
        off = ica.expected_pods(r["window_start_utc"], r["window_end_utc"], samples, inp["target"], inp["phash"], events)
        if off["pods"] is None:
            k = off["reason"].split("=")[0]
            avail["excluded_by_reason"][k] = avail["excluded_by_reason"].get(k, 0) + 1
            continue
        avail["identity_resolved"] += 1
        if r["status"] == "scored" and off["pods"] == r.get("expected_pods"):
            expected_rows[r["evaluation_seq"]] = cc.feature_row(r)
    b = cc.official_rows(evals, inp["samples"], inp["events"], inp["target"], inp["phash"])
    c3_rows = {x["seq"]: [x["features"][n] for n in cc.FEATURE_NAMES] for x in b["rows"]}
    ok = c3_rows == expected_rows and [x["seq"] for x in b["rows"]] == sorted(c3_rows)
    return {"identity_availability": avail, "c3_final_rows": len(c3_rows), "reference_rows": len(expected_rows),
            "row_set_equal": c3_rows.keys() == expected_rows.keys(), "values_equal": c3_rows == expected_rows,
            "ok": ok, "c3_excluded_reason_counts": b["excluded_reason_counts"]}


def check_file(path: Path) -> dict:
    evals = cc.read_evals(path)
    l1, inp = level1(evals), _session_inputs(path)
    l2 = level2(evals, inp)
    scored = [r for r in evals if r["status"] == "scored"]
    t0 = cc.ts(scored[0]["score_completed_at_utc"]) if scored else None
    return {"file": str(path.relative_to(cc.ROOT)).replace("\\", "/"), "n_evals": len(evals), "live_scored": len(scored),
            "skips": len(evals) - len(scored), "has_identity_recorder": inp["samples"] is not None,
            "startup_scored_first_120s": sum((cc.ts(r["score_completed_at_utc"]) - t0).total_seconds() <= STARTUP_SEC
                                             for r in scored),
            "level1": l1, "level2": l2, "ok": l1["n_mismatch"] == 0 and l1["order_ok"] and l2["ok"]}


def run(out=OUT) -> dict:
    files = sorted({p for g in LIVE_GLOBS for p in RES.glob(g)})
    per = [check_file(p) for p in files]
    with_rec = [x for x in per if x["has_identity_recorder"]]
    summ = {"files": len(per), "files_ok": sum(x["ok"] for x in per), "evals": sum(x["n_evals"] for x in per),
            "skips": sum(x["skips"] for x in per), "startup_scored": sum(x["startup_scored_first_120s"] for x in per),
            "level1_mismatch": sum(x["level1"]["n_mismatch"] for x in per),
            "level2_files_with_identity_recorder": len(with_rec),
            "level2_windows_with_recorder": sum(x["level2"]["identity_availability"]["windows"] for x in with_rec),
            "level2_identity_resolved": sum(x["level2"]["identity_availability"]["identity_resolved"] for x in with_rec),
            "level2_c3_final_rows_with_recorder": sum(x["level2"]["c3_final_rows"] for x in with_rec),
            "level2_row_set_mismatch_files": sum(not x["level2"]["ok"] for x in per),
            "level2_c3_final_rows_without_recorder": sum(x["level2"]["c3_final_rows"] for x in per
                                                         if not x["has_identity_recorder"])}
    res = {"sessions": per, "summary": summ, "reason_map": REASON_MAP,
           "mismatch_v1_wording": "expected-Pod identity evidence availability 차이에 따른 row-eligibility mismatch(특징 계산 불일치 아님)",
           "use": "동등성 확인 전용 - 학습·튜닝에 쓰지 않음"}
    res["verdict"] = "C3_ROW_SELECTION_INVARIANT_PASS" if per and all(x["ok"] for x in per) else "C3_ROW_SELECTION_INVARIANT_FAIL"
    if out is not None:
        cc.write_new_json(out, res)
    return res


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="c3 학습 행 선택 불변 검증 v2(읽기 전용, 클러스터 접근 없음)")
    p.add_argument("--dry", action="store_true", help="결과 파일을 쓰지 않음")
    a = p.parse_args(argv)
    r = run(None if a.dry else OUT)
    print(json.dumps({"verdict": r["verdict"], **r["summary"]}, ensure_ascii=False))
    return 0 if r["verdict"].endswith("PASS") else 1


if __name__ == "__main__":
    sys.exit(main())
