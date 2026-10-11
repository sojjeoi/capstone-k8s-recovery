#!/usr/bin/env python3
"""c3-normal-coverage 공용 부품(2026-10-10) - 사전등록:
experiments/results/c3_normal_coverage_prereg_20261010/C3-NORMAL-COVERAGE-PREREGISTRATION.md

c1에서 바꾸지 않는 판정 부품은 모두 기존 검증 함수를 import한다(복제 없음):
  - 연속·쿨다운: score_server.advance_streak(score < threshold 엄격 미만, 3연속, 쿨다운 60 s)
  - 특징 순서·적용: feature_selection.FEATURE_NAMES / apply_feature_schema
  - c1 학습·보정 행: train_calibrate_v33c1.load_session_rows(매니페스트 해시 확인)
건너뜀(status != scored)과 평가 번호 빠짐은 live 탐지기(c1_shadow_detector.step_streak)처럼 연속을 0으로 되돌린다.
세션 경계는 이어붙이지 않는다. 클러스터 접근 없음(이 모듈)."""
import contextlib
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXP = HERE.parent
ROOT = EXP.parent
AD = ROOT / "anomaly-detection"
C1_DIR = AD / "v3" / "model_v33_c1"
C1_ART = C1_DIR / "artifacts"
for _p in (EXP, AD, AD / "v3" / "model_v31", AD / "v3" / "model_v32b", C1_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from feature_selection import FEATURE_NAMES, apply_feature_schema  # noqa: E402
from score_server import COOLDOWN_SEC, CONSECUTIVE_THRESHOLD, advance_streak  # noqa: E402

PLAN_ID = "capstone-c3-normal-coverage-20261010"
PREREG_DOC = "experiments/results/c3_normal_coverage_prereg_20261010/C3-NORMAL-COVERAGE-PREREGISTRATION.md"
MANIFEST = HERE / "c3-session-manifest.json"
COLLECTION_DIR = EXP / "results" / "c3_normal_coverage_collection"
C3_ART = AD / "v3" / "model_c3_normal_coverage" / "artifacts"
ROLES = ("TRAIN", "CALIBRATION", "VALIDATION")
PLAN_COUNTS = {"TRAIN": 4, "CALIBRATION": 2, "VALIDATION": 4}   # PREREG H 최소안 - 바꾸지 않음
C1_SCHEMA_SHA256 = "9d8ddb6eb45448361015f58c42c7189af59b649b5f52781c677b8f33a45fbc51"
C1_THRESHOLD = -0.07667538512242333
# PREREG N: c1 forensic에서 미리 정한 진단 기준(c1 학습 평균 − 3 × c1 학습 std). 새 자료로 재계산 금지.
MEMORY_DECREASE_DIAG_BPS = -127903.0
DIAG_NOTE = "memory_slope < -127903 B/s = c1 forensic 기반 사전 고정 진단 기준(c1 학습 평균-3std), 새 자료로 재계산하지 않음"


class LeakageError(RuntimeError):
    """학습·보정 경로가 VALIDATION 세션(또는 역할 밖 세션)을 읽으려 함."""


def sha256(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def ts(x) -> datetime:
    return datetime.fromisoformat(str(x).replace("Z", "+00:00"))


# ---------------- 쓰기 보호 (allowlist) ----------------
TOOLING_DIR = EXP / "results" / "c3_normal_coverage_tooling_20261010"
EVAL_DIR = EXP / "results" / "c3_normal_coverage_evaluation"
FREEZE_MANIFEST_FILE = HERE / "c3-collection-freeze-manifest.json"
# 쓰기 허용: c3 수집 결과·c3 tooling 결과(평가 포함)·c3 동결 artifact 폴더, 그리고 수집 동결 매니페스트 파일 하나.
# 그 밖의 모든 경로(공식 c1 artifact·c1/N1/A-B/B-C-K 증거·공식 결과·저장소 나머지)는 fail-closed.
ALLOWED_WRITE_ROOTS = [COLLECTION_DIR, TOOLING_DIR, EVAL_DIR, C3_ART]
ALLOWED_WRITE_FILES = [FREEZE_MANIFEST_FILE]


def check_write_path(p) -> Path:
    """resolve()로 '..'·상대 경로·심볼릭 링크를 실제 위치로 푼 뒤, 허용 루트(역시 resolve) 안이거나 허용 파일일 때만 통과.
    적용 범위: c3 코드가 직접 쓰는 파일. 재사용하는 N1 실행기(run_normal)는 확인된 수집 폴더(results_dir)에 쓰고,
    probe 원자료 CSV만 기존 probe 결과 폴더(PROBE_RESULTS_DIR)에 c3 run_id·반복 번호 3001+ 이름으로 쓴다(기존 파일과
    이름이 겹치지 않는 허용 예외)."""
    p = Path(p).resolve()
    if any(p == Path(f).resolve() for f in ALLOWED_WRITE_FILES):
        return p
    for r in ALLOWED_WRITE_ROOTS:
        if p.is_relative_to(Path(r).resolve()):
            return p
    raise PermissionError(f"쓰기 거부(allowlist 밖): {p}")


def write_new_json(p, obj) -> Path:
    p = check_write_path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "x", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    return p


# ---------------- 세션 매니페스트 (수집 전 고정) ----------------
def load_manifest(path=None) -> dict:
    """역할·블록·순서를 검증해 돌려준다. 중복 ID·역할 수 불일치·순서 빈칸은 fail-closed."""
    m = json.loads(Path(path or MANIFEST).read_text(encoding="utf-8"))
    ids = [s["id"] for s in m["sessions"]] + list(m["spares"])
    if len(ids) != len(set(ids)):
        raise ValueError(f"중복 세션 ID: {sorted({i for i in ids if ids.count(i) > 1})}")
    if any(s["role"] not in ROLES for s in m["sessions"]):
        raise ValueError("알 수 없는 역할")
    counts = {r: sum(s["role"] == r for s in m["sessions"]) for r in ROLES}
    if counts != PLAN_COUNTS:
        raise ValueError(f"역할 수가 사전등록과 다름: {counts}")
    if [s["order"] for s in m["sessions"]] != list(range(1, len(m["sessions"]) + 1)):
        raise ValueError("실행 순서가 1..N 연속이 아님")
    blocks = [s["block"] for s in m["sessions"]]
    if blocks != sorted(blocks) or len(set(blocks)) < 3:
        raise ValueError("블록이 순서대로가 아니거나 3개 미만")
    return m


def planned(m: dict) -> dict:
    return {s["id"]: s for s in m["sessions"]}


def slot_of(m: dict, sid: str, replacements: dict) -> str:
    """세션이 채우는 계획 자리(계획 ID). 예비가 예비를 대체한 사슬은 끝까지 따라간다(순환은 거부)."""
    seen = set()
    while sid not in planned(m):
        if sid in seen or sid not in m["spares"] or sid not in replacements:
            raise KeyError(f"매니페스트에 없거나 대체 기록 없는 세션: {sid}")
        seen.add(sid)
        sid = replacements[sid]
    return sid


def session_role(m: dict, sid: str, replacements: dict) -> str:
    """replacements = {예비 ID: 대체된 ID}(수집 기록에서). 예비는 대체 사슬이 가리키는 계획 자리의 역할을 그대로 가진다."""
    return planned(m)[slot_of(m, sid, replacements)]["role"]


def require_role(m: dict, sid: str, allowed: tuple, replacements: dict) -> str:
    role = session_role(m, sid, replacements)
    if role == "VALIDATION" or role not in allowed:
        raise LeakageError(f"{sid}({role})는 이 경로에서 읽을 수 없음(허용 {allowed})")
    return role


# ---------------- evidence → 평가 (새 세션) ----------------
def read_evals(path, run_id=None) -> list:
    """c1_evaluation 기록을 evaluation_seq 순으로. run_id가 주어지면 모두 일치해야 함."""
    out = []
    for ln in Path(path).read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        r = json.loads(ln)
        if r.get("record_type") == "c1_evaluation":
            if run_id is not None and r.get("run_id") != run_id:
                raise ValueError(f"run_id 불일치: {r.get('run_id')} != {run_id}")
            out.append(r)
    return sorted(out, key=lambda r: r["evaluation_seq"])


def feature_row(rec: dict) -> list:
    """기록 features(이름→값) → FEATURE_NAMES 순서 8개(학습·점수 입력)."""
    return [float(rec["features"][n]) for n in FEATURE_NAMES]


# ---------------- 공식 c1 빌더로 새 세션 행 만들기 (행 선택 = c1과 동일) ----------------
def builder_adapter_line(r: dict) -> dict:
    """live 기록 → 공식 빌더 detector-evidence 한 줄. 원 응답·신선도 응답·창은 그대로 옮긴다(값 가공 없음).
    pod 조회 전 단계에서 멈춘 기록(원자료 없음)은 빌더의 evaluation_skipped로.
    알려진 차이(행 선택·특징 값에는 영향 없음): live는 CPU를 판단 창 그대로 조회하므로 빌더의 candidate_window_mismatch
    검사는 항상 통과(c1은 별도 후보 조회라 의미가 있었음), feature_computed_at_utc = live query_completed_at_utc,
    cpu_candidate_query_utc는 비어 있음. 신선도 metric 집합(cpu·memory·queue·cache)과 수신 시각은 그대로."""
    seq = r["evaluation_seq"]
    if "window_start_utc" not in r or "pod_snapshot" not in r:
        return {"record_type": "evaluation_skipped", "evaluation_seq": seq}
    q = r.get("queries") or {}
    cpu = q.get("cpu") or {}
    pc = {"cpu_queries": [{"label": "candidate_rate60", "start_utc": r["window_start_utc"], "end_utc": r["window_end_utc"],
                           "series": cpu.get("series") or [], "error": cpu.get("error")}]}
    if "freshness" in r:
        pc["freshness_queries"] = [{"metric": m, "body": f.get("body"), "error": f.get("error"),
                                    "query_received_at_utc": f["received_at_utc"]} for m, f in r["freshness"].items()]
    return {"record_type": "evaluation_decision", "evaluation_seq": seq, "window_start_utc": r["window_start_utc"],
            "window_end_utc": r["window_end_utc"], "feature_computed_at_utc": r.get("query_completed_at_utc"),
            "parallel_candidate": pc,
            "per_metric_provenance": {m: {"raw_query_capture": {"series": (q.get(m) or {}).get("series") or []}}
                                      for m in ("memory", "queue", "cache") if m in q and not (q.get(m) or {}).get("error")}}


def official_rows(evals: list, samples_path, events_path, active_target, preview_hash) -> dict:
    """공식 c1_collection.build_c1_features를 그대로 호출(기대 Pod = 공식 input_capture_analysis.expected_pods + 실제 5 s
    표본·Pod 이벤트). 표본이 창을 덮지 못하면 공식 규칙대로 제외 - 보간·추정 없음. 임시 폴더에만 어댑터 파일을 쓴다."""
    import tempfile
    import c1_collection as c1b
    with tempfile.TemporaryDirectory() as td:
        d, rid = Path(td), "c3"
        (d / f"summary-{rid}.json").write_text(json.dumps({"active_target": active_target,
                                                           "preview_prep": {"created_pod_hash": preview_hash}}),
                                               encoding="utf-8")
        sp = Path(samples_path) if samples_path else None
        (d / f"samples-{rid}.jsonl").write_text(sp.read_text(encoding="utf-8") if sp and sp.exists() else "",
                                                encoding="utf-8")
        ep = Path(events_path) if events_path else None
        if ep and ep.exists():
            (d / f"pod-events-{rid}.json").write_text(ep.read_text(encoding="utf-8"), encoding="utf-8")
        (d / f"detector-evidence-{rid}.jsonl").write_text("\n".join(json.dumps(builder_adapter_line(r)) for r in evals),
                                                           encoding="utf-8")
        return c1b.build_c1_features(d, rid)


def live_consistent(builder: dict, evals: list) -> dict:
    """빌더 행 중 live가 점수를 내지 않은 평가(live 건너뜀)는 행에서 빼고 제외 목록으로 옮긴다 - 보정 구간이 live가 연속을
    0으로 되돌린 자리를 잇지 않도록(기존 증거에서는 해당 0건, 방어)."""
    live = {r["evaluation_seq"]: r.get("status") for r in evals}
    keep = [x for x in builder["rows"] if live.get(x["seq"]) == "scored"]
    moved = [{"seq": x["seq"], "reason": f"live_not_scored:{live.get(x['seq'])}"} for x in builder["rows"]
             if live.get(x["seq"]) != "scored"]
    return {**builder, "rows": keep, "excluded_rows": builder["excluded_rows"] + moved}


def m1_events(sid: str, evals: list, builder: dict) -> dict:
    """PREREG M-1(2026-10-10 amendment) - 빌더가 제외한 평가마다 위치를 기록한다.
    position: 첫 빌더 행보다 앞 = start, 마지막 빌더 행보다 뒤 = end, 그 사이 = interior(빌더 행이 0개면 모두 start).
    M-1 위반으로 세는 것 = 빌더 제외 AND live scored AND interior 뿐(가장자리 제외·live가 건너뛴 평가는 세지 않음)."""
    rows = sorted(x["seq"] for x in builder["rows"])
    exc = {x["seq"]: x["reason"] for x in builder["excluded_rows"]}
    out = []
    for r in evals:
        s = r["evaluation_seq"]
        if s not in exc:
            continue
        pos = "interior" if rows and rows[0] < s < rows[-1] else ("end" if rows and s > rows[-1] else "start")
        live = r.get("status") == "scored"
        out.append({"session": sid, "evaluation_seq": s, "window_start_utc": r.get("window_start_utc"),
                    "score_completed_at_utc": r.get("score_completed_at_utc"), "exclusion_reason": exc[s],
                    "live_scored": live, "position": pos, "counts_for_m1": live and pos == "interior"})
    return {"events": out, "m1_count": sum(e["counts_for_m1"] for e in out)}


def load_replacements(m, collection_dir) -> dict:
    """예비 ID 대체 기록(c3-replacement-*.json, 실행 전에 필수로 쓰임) - 세션이 중간에 깨져도 역할이 남는다."""
    out = {}
    for sid in m["spares"]:
        p = Path(collection_dir) / f"c3-replacement-{sid}.json"
        if p.exists():
            out[sid] = json.loads(p.read_text(encoding="utf-8"))["replaces"]
    return out


def row_segments(builder_rows: list) -> list:
    """빌더 행(seq 순)을 연속 seq 구간으로 - 제외·건너뜀 평가에서 끊김(live: 연속 0)."""
    segs, prev = [], None
    for x in sorted(builder_rows, key=lambda x: x["seq"]):
        if prev is None or x["seq"] != prev + 1:
            segs.append([])
        segs[-1].append([float(x["features"][n]) for n in FEATURE_NAMES])
        prev = x["seq"]
    return segs


def score_rows(model, scaler, schema, rows) -> list:
    if not rows:
        return []
    return [float(v) for v in model.decision_function(scaler.transform([apply_feature_schema(r, schema) for r in rows]))]


def replay(evals: list, scores_by_seq: dict, threshold: float) -> list:
    """live 상태기계 재생. scores_by_seq = {seq: score}(scored만). 시각 = score_completed_at(벽시계, N1 분석과 같음).
    반환: 평가마다 {seq, status, is_anomalous, consecutive, condition_met, t}."""
    consec, last, prev, out = 0, None, None, []
    for r in evals:
        seq = r["evaluation_seq"]
        if prev is not None and seq != prev + 1:
            consec = 0
        prev = seq
        if r.get("status") != "scored":
            consec = 0
            out.append({"seq": seq, "status": r.get("status"), "is_anomalous": None, "consecutive": 0,
                        "condition_met": False, "t": None})
            continue
        t = ts(r["score_completed_at_utc"])
        st = advance_streak(scores_by_seq[seq], threshold, consec, last, t.timestamp(),
                            cooldown_sec=COOLDOWN_SEC, consecutive_threshold=CONSECUTIVE_THRESHOLD)
        consec, last = st["consecutive_anomalous"], st["last_signal_at"]
        out.append({"seq": seq, "status": "scored", "is_anomalous": st["is_anomalous"], "consecutive": consec,
                    "condition_met": st["should_signal"], "t": t})
    return out


# ---------------- 스키마·모델 ----------------
def load_c1_schema() -> dict:
    """c1 feature-schema.json을 그대로 쓴다(재계산 없음). 해시가 다르면 fail-closed."""
    p = C1_ART / "feature-schema.json"
    if sha256(p) != C1_SCHEMA_SHA256:
        raise RuntimeError("fail-closed: c1 feature-schema.json 해시가 기대값과 다름")
    s = json.loads(p.read_text(encoding="utf-8"))
    if s["kept_feature_names"] != ["cpu_mean", "cpu_slope", "memory_mean", "memory_slope"]:
        raise RuntimeError("fail-closed: c1 schema 특징이 4특징과 다름")
    return s


def load_frozen(art: Path) -> dict:
    """SHA256SUMS로 모든 파일 확인 뒤 model·scaler·schema·threshold."""
    import pickle
    sums = json.loads((art / "SHA256SUMS.json").read_text(encoding="utf-8"))
    bad = [k for k, v in sums.items() if sha256(art / k) != v]
    if bad:
        raise RuntimeError(f"fail-closed: artifact 해시 불일치 {bad}")
    thr = json.loads((art / "threshold.json").read_text(encoding="utf-8"))
    return {"model": pickle.loads((art / "model.pkl").read_bytes()), "scaler": pickle.loads((art / "scaler.pkl").read_bytes()),
            "schema": json.loads((art / "feature-schema.json").read_text(encoding="utf-8")),
            "threshold": thr["threshold"], "calibration_failed": thr.get("calibration_failed"), "hashes": sums}


# ---------------- N1 실행기·분석기 전역을 c3로 잠시 바꾸기 ----------------
@contextlib.contextmanager
def patched_n1(rn, *, runs: dict, schedule_fn=None, results_dir=None, freeze=None):
    """run_guarded_normal_bc(파일 수정 없음)의 계열 전역을 c3 값으로 바꾸고 finally에서 되돌린다(demo 경로와 같은 방식).
    바꾸는 이름: PLAN_ID, CONTRACT_DOC, RESULTS_DIR, RUNS, check_schedule, (freeze 주면) FREEZE_VERSION·FREEZE_MANIFEST·
    PROVENANCE_REQUIRED. run_id_for·observation_sec_for·analysis.read_session/analyze_session은 이 전역을 호출 시점에 읽는다."""
    names = ["PLAN_ID", "CONTRACT_DOC", "RESULTS_DIR", "RUNS", "check_schedule",
             "FREEZE_VERSION", "FREEZE_MANIFEST", "PROVENANCE_REQUIRED"]
    saved = {n: getattr(rn, n) for n in names}
    if any(v[0] in rn.PILOTS for v in runs.values()):
        raise ValueError("c3 세션 번호가 N1 pilot 번호와 겹침(관찰 600 s로 바뀜)")
    try:
        rn.PLAN_ID, rn.CONTRACT_DOC = PLAN_ID, PREREG_DOC
        rn.RESULTS_DIR, rn.RUNS = Path(results_dir or COLLECTION_DIR), runs
        if schedule_fn is not None:
            rn.check_schedule = schedule_fn
        if freeze is not None:
            rn.FREEZE_VERSION, rn.FREEZE_MANIFEST, rn.PROVENANCE_REQUIRED = freeze
        yield rn
    finally:
        for n, v in saved.items():
            setattr(rn, n, v)


def runs_table(m: dict) -> dict:
    """{ID: (세션 번호, probe 반복 번호)} - 계획 1..N → 3001.., 예비 51.. → 3051.. (N1 pilot 90·91과 겹치지 않음)."""
    t = {s["id"]: (s["order"], 3000 + s["order"]) for s in m["sessions"]}
    t.update({sid: (51 + i, 3051 + i) for i, sid in enumerate(m["spares"])})
    return t
