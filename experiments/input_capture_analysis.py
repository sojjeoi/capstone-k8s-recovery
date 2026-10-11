#!/usr/bin/env python3
"""2026-10-04 입력 계측판 진단 분석(오프라인, 읽기 전용) - 평가마다 합산 전 CPU·메모리 응답 원문을 실행 당시 있어야 하는
active·preview(이름·UID)와 대조해 창을 분류한다. 기존 점수·신호는 바꾸지 않는다(분류는 기록일 뿐).

기대 목록의 근거(실행 당시 기록만 사용, 현재 클러스터 상태 미사용):
- active = 압박 대상 Pod(stress_delivery_record.target 이름·UID). 창 안 5초 표본이 끊김 없이(간격 ≤ 15초) 덮고, 모든 표본에서
  같은 UID로 살아 있으며(종료 중 아님) active selector가 바뀌지 않아야 한다.
- preview = preview 준비 해시(preview_prep.created_pod_hash)를 가진 표본기 카운터 키(정확히 1개)와 Pod 이벤트 UID(정확히 1개).
  창 안 모든 표본에서 그 Pod의 요청 카운터가 읽혀야 한다.
근거가 부족하거나 Pod 교체 구간이면 expected_unverified."""
import argparse
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

SAMPLE_GAP_MAX_SEC = 15.0  # 5초 표본 간격의 3배(timed_switch_verdict.MAX_LIVENESS_GAP_FACTOR와 같은 배수)
GRID_TOL_SEC = 0.5
METRICS = ("cpu", "memory")
ALL_METRICS = ("cpu", "memory", "queue", "cache")  # 부담 집계는 계측이 붙는 네 지표 전부
_UID_RE = re.compile(r"[0-9a-f]{8}[-_][0-9a-f]{4}[-_][0-9a-f]{4}[-_][0-9a-f]{4}[-_][0-9a-f]{12}")


def _ts(x):
    return x if isinstance(x, datetime) or x is None else datetime.fromisoformat(str(x).replace("Z", "+00:00"))


def expected_pods(ws, we, samples, target, preview_hash, pod_events) -> dict:
    """반환 {"pods": {name: uid}, "reason": None} 또는 {"pods": None, "reason": 사유}."""
    ws, we = _ts(ws), _ts(we)
    near = sorted((s for s in samples if ws - timedelta(seconds=SAMPLE_GAP_MAX_SEC) <= _ts(s["t"])
                   <= we + timedelta(seconds=SAMPLE_GAP_MAX_SEC)), key=lambda s: _ts(s["t"]))
    if not near or _ts(near[0]["t"]) > ws or _ts(near[-1]["t"]) < we:
        return {"pods": None, "reason": "samples_do_not_cover_window"}
    if max(((_ts(b["t"]) - _ts(a["t"])).total_seconds() for a, b in zip(near, near[1:])), default=0) > SAMPLE_GAP_MAX_SEC:
        return {"pods": None, "reason": "sample_gap"}
    if not (target or {}).get("name") or not target.get("uid"):
        return {"pods": None, "reason": "target_unknown"}
    for s in near:
        op = s.get("old_pod")
        if not op or not op.get("exists") or not op.get("running") or op.get("terminating") or op.get("uid") != target["uid"]:
            return {"pods": None, "reason": "active_not_alive_or_replaced_in_window"}
    if len({s.get("active_selector") for s in near}) != 1 or near[0].get("active_selector") is None:
        return {"pods": None, "reason": "active_selector_changed_or_unknown"}
    if near[0]["active_selector"] != target["name"].rsplit("-", 2)[-2]:  # 창 이전에 전환이 끝났으면 대상은 더 이상 active 아님
        return {"pods": None, "reason": "active_selector_not_target_hash"}
    if not preview_hash:
        return {"pods": None, "reason": "preview_hash_unknown"}
    prefix = f"vllm-serving-{preview_hash}-"
    names = sorted({k for s in near for k in (s.get("pod_counters") or {}) if k.startswith(prefix)})  # 그 창 주변만
    if len(names) != 1:
        return {"pods": None, "reason": f"preview_name_candidates={names}"}
    uids = sorted({(e.get("involved_object") or {}).get("uid") for e in pod_events or []
                   if (e.get("involved_object") or {}).get("kind") == "Pod"
                   and (e.get("involved_object") or {}).get("name") == names[0]} - {None})
    if len(uids) != 1:
        return {"pods": None, "reason": f"preview_uid_candidates={uids}"}
    if any((s.get("pod_counters") or {}).get(names[0]) is None for s in near):
        return {"pods": None, "reason": "preview_counter_missing_in_window"}
    return {"pods": {target["name"]: target["uid"], names[0]: uids[0]}, "reason": None}


def _label_uids(labels: dict) -> set:
    """라벨 값 안의 UID 형태 문자열(cAdvisor id 경로는 '-' 대신 '_')을 '-' 형태로 모은다."""
    return {u.replace("_", "-") for v in labels.values() for u in _UID_RE.findall(str(v))}


def classify_capture(capture: dict, expected: dict) -> dict:
    """한 지표의 원문 응답을 기대 목록과 대조. 분류: complete / partial_missing / all_missing / expected_unverified."""
    start, end = _ts(capture["start_utc"]).timestamp(), _ts(capture["end_utc"]).timestamp()
    step = float(str(capture.get("step", "15s")).rstrip("s"))
    grid = [start + i * step for i in range(int((end - start) / step + GRID_TOL_SEC) + 1)]
    by_pod, out = {}, {"grid_points": len(grid)}
    for sr in capture.get("series") or []:
        by_pod.setdefault(sr["metric"].get("pod"), []).append(sr)
    out["duplicate_series"] = {p: len(v) for p, v in by_pod.items() if len(v) > 1}
    if expected.get("pods") is None:
        out.update(classification="expected_unverified", reason=expected.get("reason"),
                   response_pods=sorted(p for p in by_pod if p))
        return out
    out["unexpected_series_pods"] = sorted(p for p in by_pod if p not in expected["pods"])
    missing, uid_status = {}, {}
    for pod, uid in expected["pods"].items():  # UID 라벨이 있으면 기대 UID와 같아야 한다 - 다르면 판정하지 않음
        found = set().union(*(_label_uids(sr["metric"]) for sr in by_pod.get(pod, []))) if by_pod.get(pod) else set()
        if found - {uid}:
            out.update(classification="expected_unverified", reason=f"uid_label_conflict({pod}: {sorted(found)})",
                       response_pods=sorted(p for p in by_pod if p))
            return out
        present = {i for sr in by_pod.get(pod, []) for ts, _v in sr["values"]
                   for i, g in enumerate(grid) if abs(float(ts) - g) <= GRID_TOL_SEC}
        miss = [i for i in range(len(grid)) if i not in present]
        if miss:
            missing[pod] = miss
        labels = [sr["metric"] for sr in by_pod.get(pod, [])]
        uid_status[pod] = ("absent_from_response" if not labels else
                           "matched" if any(uid in _label_uids(lb) for lb in labels) else "not_in_labels(name_only)")
    out["missing_grid_indices"] = missing
    out["pods_missing_entire_window"] = sorted(p for p, m in missing.items() if len(m) == len(grid))
    out["uid_label_status"] = uid_status
    total = len(grid) * len(expected["pods"])
    n_missing = sum(len(m) for m in missing.values())
    out["classification"] = "complete" if n_missing == 0 else ("all_missing" if n_missing == total else "partial_missing")
    return out


def analyze(results_dir: Path, run_id: str) -> dict:
    s = json.loads((results_dir / f"summary-{run_id}.json").read_text(encoding="utf-8"))
    samples = [json.loads(x) for x in (results_dir / f"samples-{run_id}.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    ev_path = results_dir / f"pod-events-{run_id}.json"
    events = json.loads(ev_path.read_text(encoding="utf-8")) if ev_path.exists() else []
    target = (s.get("stress_delivery_record") or {}).get("target")
    preview_hash = (s.get("preview_prep") or {}).get("created_pod_hash")
    t0 = _ts(s.get("t_injection_observed_utc"))
    rows, totals = [], {m: {} for m in METRICS}
    cost = {"capture_bytes": [], "capture_build_ms": [], "evaluation_wall_ms": [],
            "evidence_line_bytes": [], "capture_field_bytes_per_eval": []}
    for line in (results_dir / f"detector-evidence-{run_id}.jsonl").read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r.get("record_type") not in ("evaluation_decision", "evaluation_skipped"):
            continue
        prov = r.get("per_metric_provenance") or {}
        cost["evidence_line_bytes"].append(len(line.encode("utf-8")))
        caps = [(prov.get(m) or {}).get("raw_query_capture") for m in ALL_METRICS]
        cost["capture_field_bytes_per_eval"].append(sum(len(json.dumps(c, ensure_ascii=False).encode("utf-8"))
                                                        for c in caps if c is not None))
        for c in caps:
            if c is not None:
                cost["capture_bytes"].append(c.get("capture_bytes", 0))
                cost["capture_build_ms"].append(c.get("capture_build_ms", 0.0))
        t_eval = _ts(r.get("feature_computed_at_utc") or r.get("wall_clock_after_utc"))
        row = {"seq": r.get("evaluation_seq"), "record_type": r["record_type"], "score": r.get("score"),
               "rel_sec": round((t_eval - t0).total_seconds(), 1) if t0 and t_eval else None, "metrics": {}}
        if r.get("wall_clock_before_utc") and r.get("wall_clock_after_utc"):
            cost["evaluation_wall_ms"].append((_ts(r["wall_clock_after_utc"]) - _ts(r["wall_clock_before_utc"])).total_seconds() * 1000)
        for m in METRICS:
            cap = (prov.get(m) or {}).get("raw_query_capture")
            if cap is None:
                row["metrics"][m] = {"classification": "no_capture"}
            else:
                exp = expected_pods(cap["start_utc"], cap["end_utc"], samples, target, preview_hash, events)
                row["metrics"][m] = classify_capture(cap, exp)
            c = row["metrics"][m]["classification"]
            totals[m][c] = totals[m].get(c, 0) + 1
        rows.append(row)

    def stat(v):
        return {"n": len(v), "sum": round(sum(v), 3), "mean": round(sum(v) / len(v), 3), "max": round(max(v), 3)} if v else None
    return {"run_id": run_id, "note": "새 실행의 관측. 과거 v1s01 원인 추정과 구분해 해석한다. 분류는 점수·신호를 바꾸지 않는다.",
            "expected_basis": {"active": target, "preview_hash": preview_hash},
            "classification_counts": totals, "capture_cost": {k: stat(v) for k, v in cost.items()},
            "capture_cost_note": ("capture_build_ms는 원문 복사 뒤 본문 구성·크기 계산만 잰 하한이다. 원문 복사·기록 쓰기를 포함한 "
                                  "전체 부담은 계측 없는 같은 경로 실행(V2 A·v1s01)의 evaluation_wall_ms와 비교해 판단한다."),
            "evaluations": rows}


def main():
    p = argparse.ArgumentParser(description="입력 계측판 진단 분석 - 새 파일에만 저장")
    p.add_argument("--results-dir", required=True, type=Path)
    p.add_argument("--run-id", required=True)
    p.add_argument("--out", required=True, type=Path)
    a = p.parse_args()
    out = analyze(a.results_dir, a.run_id)
    with open(a.out, "x", encoding="utf-8") as f:  # 배타적 생성 - 덮어쓰지 않음
        f.write(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    print(json.dumps({k: out[k] for k in ("classification_counts", "capture_cost")}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
