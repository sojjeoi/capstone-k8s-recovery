#!/usr/bin/env python3
"""2026-10-02 - IF vs 고정 임계치, 같은 평가 시점 오프라인 비교(읽기 전용).

사전 등록: docs/design/phase8-detector-same-tick-comparison-protocol-20261002.md - 규칙·자료 목록·
유효성 기준은 그 문서와 1:1로 대응하며 결과를 보고 바꾸지 않는다.

같은 시점·같은 창이 보장되는 이유: 후보 IF 평가 기록의 각 평가가 features.py로 계산한
raw_feature_vector(0번 = cpu_mean)와 score를 함께 남겼다. 고정 임계치(fixed_threshold.py)는 같은
features.py의 cpu_mean만 보므로, 같은 기록의 raw_feature_vector[0]에 적용하면 시각·창·입력이 같다.
새 Prometheus 질의·클러스터 접속 없음. 원자료는 읽기만 하고, 결과는 --out 경로에만 쓴다."""
import argparse
import json
import math
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import arm_controller
import slo_judge
from run_if_threshold_candidate_smoke import CANDIDATE_THRESHOLD, CONSECUTIVE_THRESHOLD, OFFICIAL_THRESHOLD

RESULTS = Path(__file__).parent / "results"
EVIDENCE_DIR = RESULTS / "capstone_if_threshold_candidate"
FIXED_THRESHOLD_CORES = arm_controller.FIXED_THRESHOLD_CPU_LIMIT_CORES * 0.90  # 2.7 - fixed_threshold 비교와 같은 식
EVAL_INTERVAL_SEC = 15.0
MAX_TICK_GAP_SEC = 30.0
MIN_TICK_COVERAGE = 0.9
COVERAGE_TOLERANCE_SEC = 30.0  # §5.8 - 관찰 구간 시작·끝과 평가 기록 시작·끝의 허용 차이(평가 주기 2회분)
FEATURE_COUNT = 8
CPU_MEAN_INDEX = 0
MODEL_VERSION = "v3.2b"
MODEL_PKL_SHA256 = "2102e4f5f0d06809402f6f11c0396f0249bda3864bf6eeb18c52ac626e1a9243"

# 고정 자료 목록(문서 §3) - (구분, run_id, probe rep)
_P183 = "capstone-if-threshold-candidate-183-v1"
_P188 = "capstone-if-threshold-candidate-no-injection-long-188-v1"
RUNS = [
    ("pressure", f"if_threshold_candidate_no_switch_control-proposed-01-{_P183}", 1),
    ("pressure", f"if_threshold_candidate_no_switch_control-proposed-02-{_P183}", 2),
    ("pressure", f"if_threshold_candidate_no_switch_control-proposed-03-{_P183}", 3),
    ("no_injection", f"if_threshold_candidate_no_injection_long-proposed-01-{_P188}", 1),
    ("no_injection", f"if_threshold_candidate_no_injection_long-gated-proposed-01-{_P188}", 1),
    ("no_injection", f"if_threshold_candidate_no_injection_long-gated-proposed-08-{_P188}", 8),
]

RULES = {
    "if_candidate": lambda t: t["score"] < CANDIDATE_THRESHOLD,
    "if_official": lambda t: t["score"] < OFFICIAL_THRESHOLD,
    "fixed_threshold": lambda t: t["cpu_mean"] > FIXED_THRESHOLD_CORES,
}

# ---- 2026-10-04 V1 독립 검증(docs/design/phase8-v1-detector-validation-contract-20261004.md) ----
# 기존 개발 자료 경로(--set dev, 위 RUNS)는 그대로다. V1은 V1 계약 요약에서만 아래 분기를 탄다.
V1_EVIDENCE_DIR = RESULTS / "capstone_v1_detector"
V1_CONTRACT_ID = "v1-detector-contract-20261004"
MEM_SLOPE_INDEX = 3  # gated_signal_detector.MEM_SLOPE_INDEX와 같은 위치(raw_feature_vector[3] = memory_slope)
G1_DEV_CONSISTENCY_RUNS = [  # G1 재생 정합성 확인 전용(도구 검증, 성능 판정 아님) - 개발 자료
    f"if_threshold_candidate_no_injection_long-gated-proposed-01-{_P188}",
    f"if_threshold_candidate_no_injection_long-gated-proposed-08-{_P188}",
]


def v1_runs() -> list:
    """V1 계약의 고정 실행 목록(실행 전 고정). 수집되지 않은 ID는 분석에서 not_collected로 남긴다."""
    import run_v1_detector_trial as v1
    return ([("pressure", v1.run_id_for(r), rep) for r, rep in v1.PRESSURE_RUNS.items()]
            + [("no_injection", v1.run_id_for(r), rep) for r, rep in v1.NO_INJECTION_RUNS.items()])


def _ts(x) -> Optional[datetime]:
    return datetime.fromisoformat(str(x).replace("Z", "+00:00")) if x else None


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


EVALUATION_RECORD_TYPES = ("evaluation_decision", "evaluation_skipped")


def load_ticks(lines) -> list:
    """평가 기록(evaluation_decision·evaluation_skipped, 또는 score가 있는 기록)을 기록 순서대로.
    건너뛴 평가도 남긴다 - 실제 탐지기는 건너뜀에서 연속을 끊으므로, 버리면 가짜 3연속이 생긴다
    (점수 없는 평가는 §5.4로 그 실행을 무효 처리). signal_result 등 비평가 기록은 제외."""
    ticks = []
    for line in lines:
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("record_type") not in EVALUATION_RECORD_TYPES and "score" not in r:
            continue
        raw = r.get("raw_feature_vector")
        cpu_prov = (r.get("per_metric_provenance") or {}).get("cpu") or {}
        ticks.append({
            "t": _ts(r.get("feature_computed_at_utc")),
            "window_start": r.get("window_start_utc"), "window_end": r.get("window_end_utc"),
            "score": r.get("score"), "raw": raw,
            "cpu_mean": raw[CPU_MEAN_INDEX] if isinstance(raw, list) and raw else None,
            "mem_slope": raw[MEM_SLOPE_INDEX] if isinstance(raw, list) and len(raw) > MEM_SLOPE_INDEX else None,
            "seq": r.get("evaluation_seq"), "corr": r.get("correlation_id"),
            "model_version": r.get("model_version"),
            "model_hash": (r.get("artifact_hashes") or {}).get("model.pkl"),
            "has_hashes": "artifact_hashes" in r,
            "cpu_status": cpu_prov.get("raw_sample_status"),
            "has_cpu_status": "raw_sample_status" in cpu_prov,  # §5.6은 이 값이 있을 때만 검사
        })
    return ticks


def episodes(ticks: list, pred: Callable[[dict], bool], n: int = CONSECUTIVE_THRESHOLD) -> list:
    """이상 판정 연속 구간이 길이 n에 도달한 평가의 인덱스 목록(구간당 1회, 쿨다운 없음)."""
    out, streak = [], 0
    for i, t in enumerate(ticks):
        evaluable = _finite(t["score"]) and _finite(t["cpu_mean"])  # 건너뛴 평가는 실제 탐지기처럼 연속을 끊는다
        streak = streak + 1 if evaluable and pred(t) else 0
        if streak == n:
            out.append(i)
    return out


def _align(t: datetime, rows: list) -> datetime:
    """probe CSV 시각이 naive면 비교를 위해 tz를 떼어 맞춘다."""
    return t if rows and rows[0]["sent_at"].tzinfo is not None else t.replace(tzinfo=None)


def validate(kind: str, summary: dict, ticks: list, probe_rows: Optional[list], v1: bool = False,
             detector_log_lines: Optional[list] = None) -> list:
    """문서 §5의 1~7. 반환: 위반 사유 목록(빈 목록 = 유효)."""
    reasons = []
    if summary.get("outcome_status") != "completed":
        reasons.append(f"1:outcome_status={summary.get('outcome_status')}")
    # V1 판별: --set v1 고정 목록으로 분석할 때(v1=True). V2 실행기 A 경로 요약에는 main_contract가 없고 계약 파일에만 있다
    # (2026-10-04 v1s01 첫 분석에서 발견한 판별 버그 수정 - 판정 기준은 그대로).
    is_v1 = v1 or summary.get("main_contract") == V1_CONTRACT_ID
    if kind == "pressure" and is_v1:
        reasons += _v1_pressure_item2(summary, probe_rows)
    elif kind == "pressure":
        if summary.get("injection_valid") is not True or not summary.get("t_injection_observed_utc"):
            reasons.append("2:injection_not_valid_or_time_missing")
        if summary.get("probe_data_gap_detected") is not False:
            reasons.append(f"2:probe_data_gap_detected={summary.get('probe_data_gap_detected')}")
    if not ticks or any(t["t"] is None for t in ticks):
        reasons.append("3:no_ticks_or_missing_time")
    else:
        span = (ticks[-1]["t"] - ticks[0]["t"]).total_seconds()
        expected = span / EVAL_INTERVAL_SEC + 1
        if is_v1:  # V1 계약 §5.1(2026-10-04 개정): 개수 비율 대신 평가 기록 완전성을 직접 검사(아래 함수)
            reasons += evaluation_completeness_reasons(ticks, detector_log_lines)
        elif len(ticks) < MIN_TICK_COVERAGE * expected:
            reasons.append(f"3:tick_coverage={len(ticks)}/{expected:.0f}")
        gaps = [(b["t"] - a["t"]).total_seconds() for a, b in zip(ticks, ticks[1:])]
        if gaps and max(gaps) > MAX_TICK_GAP_SEC:
            reasons.append(f"3:max_tick_gap={max(gaps):.1f}s")
        if any(g <= 0 for g in gaps):  # 중복·역행 기록은 연속 횟수와 선후를 왜곡한다
            reasons.append("3:non_increasing_tick_time")
    if any(not (isinstance(t["raw"], list) and len(t["raw"]) == FEATURE_COUNT and all(_finite(x) for x in t["raw"])
                and _finite(t["score"])) for t in ticks):
        reasons.append("4:non_finite_or_malformed_features_or_score")
    if any(t["model_version"] != MODEL_VERSION or (t["has_hashes"] and t["model_hash"] != MODEL_PKL_SHA256)
           for t in ticks):
        reasons.append("5:model_version_or_hash_mismatch")
    if any(t["has_cpu_status"] and t["cpu_status"] != "ok" for t in ticks):
        reasons.append("6:cpu_raw_sample_status_not_ok")
    if kind == "pressure":
        t_inj = _ts(summary.get("t_injection_observed_utc"))
        if not probe_rows or t_inj is None or not any(r["sent_at"] >= _align(t_inj, probe_rows) for r in probe_rows):
            reasons.append("7:probe_csv_missing_or_no_request_after_injection")
    reasons += coverage_reasons(kind, summary, ticks)
    if is_v1:
        reasons += _v1_common_reasons(kind, summary)
    return reasons


_LOG_EVAL_RE = re.compile(r"^\[[^\]]+\] (?:score=.*? seq=(\d+) corr=([0-9a-f]+)|평가 스킵\(seq=(\d+))")


def parse_detector_log(lines) -> list:
    """탐지기 콘솔 로그(score_server-stdout-<run_id>.log)의 평가 줄만 [(seq, corr 또는 None)] 순서대로.
    평가 기록(evidence)과 별도로 같은 프로세스가 매 평가 뒤에 찍는 독립 기록이다."""
    out = []
    for line in lines:
        m = _LOG_EVAL_RE.match(line)
        if m:
            out.append((int(m.group(1) or m.group(3)), m.group(2)))
    return out


def evaluation_completeness_reasons(ticks: list, detector_log_lines: Optional[list]) -> list:
    """V1 계약 §5.1 - 평가 기록 완전성. 번호 연속만으로 완전하다고 보지 않고, 독립 기록(탐지기 콘솔 로그)과 맞춰
    앞(1번부터)·뒤(마지막 번호)·중간(누락·중복·역행)을 확인한다. 근거가 없으면 판정 불가(무효)로 남긴다.
    최대 간격 30초·시각 역행·관찰 구간 양끝(±30초)·건너뜀·특징/점수 이상은 기존 검사가 그대로 본다."""
    out = []
    seqs = [t.get("seq") for t in ticks]
    if any(not isinstance(q, int) or isinstance(q, bool) for q in seqs):
        return ["3:evaluation_seq_missing"]
    if seqs[0] != 1:
        out.append(f"3:evaluation_seq_starts_at_{seqs[0]}")
    if any(b != a + 1 for a, b in zip(seqs, seqs[1:])):
        out.append("3:evaluation_seq_gap_duplicate_or_reversal")
    if detector_log_lines is None:
        return out + ["3:completeness_unverifiable(detector_log_missing)"]
    log = parse_detector_log(detector_log_lines)
    if not log:
        return out + ["3:completeness_unverifiable(no_evaluation_lines_in_detector_log)"]
    log_seqs = [q for q, _ in log]
    if log_seqs[0] != 1 or any(b != a + 1 for a, b in zip(log_seqs, log_seqs[1:])):
        out.append("3:detector_log_seq_not_contiguous_from_1")
    if not set(log_seqs) <= set(seqs):
        out.append(f"3:evaluations_in_log_missing_from_record={sorted(set(log_seqs) - set(seqs))[:5]}")
    if not 0 <= seqs[-1] - log_seqs[-1] <= 1:  # 기록 쓰기 직후·로그 출력 전에 끝난 경우 1개 차이만 허용
        out.append(f"3:last_seq_mismatch(record={seqs[-1]}, log={log_seqs[-1]})")
    corr = {t["seq"]: t.get("corr") for t in ticks}
    # 점수가 있는 평가 줄은 corr가 필수(정규식) - 같은 번호의 기록 corr가 없거나 다르면 거부(번호만 맞는 무관한 줄 차단)
    if any(c is not None and corr.get(q) != c for q, c in log):
        out.append("3:correlation_id_mismatch_between_record_and_log")
    return out


def evaluation_diagnostics(ticks: list) -> dict:
    """진단 전용(판정에 쓰지 않음): 실제 평가 간격과 기존 15초 기준 커버리지."""
    times = [t["t"] for t in ticks if t["t"] is not None]
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    if not gaps:
        return {"evaluations": len(ticks), "mean_interval_sec": None, "max_interval_sec": None,
                "legacy_15s_coverage": None}
    expected = (times[-1] - times[0]).total_seconds() / EVAL_INTERVAL_SEC + 1
    return {"evaluations": len(ticks), "mean_interval_sec": round(sum(gaps) / len(gaps), 2),
            "max_interval_sec": round(max(gaps), 2),
            "legacy_15s_coverage": round(len(ticks) / expected, 4) if expected > 0 else None}  # 시각 역행 기록 방어


def _v1_pressure_item2(summary: dict, probe_rows: Optional[list]) -> list:
    """V1 압박(V2 실행기 A 경로) 요약의 §5-2 대응: 압박 전달(v2.3) 확인, 실행기 비교 자격(무승격·probe 이미지·정리),
    주입 시각, probe 완전성(V2 분석기와 같은 기준: 관찰 구간 요청 ≥ 570, 공백 ≤ 30초)."""
    import slo_episodes
    out = []
    if not summary.get("t_injection_observed_utc"):
        out.append("2:injection_time_missing")
    if (summary.get("stress_delivery") or {}).get("verified") is not True:
        out.append(f"2:stress_delivery_{(summary.get('stress_delivery') or {}).get('verified')}")
    if not (summary.get("comparison") or {}).get("eligible"):
        out.append(f"2:runner_not_eligible{(summary.get('comparison') or {}).get('reasons')}")
    t_inj, t_end = _ts(summary.get("t_injection_observed_utc")), _ts(summary.get("observation_ended_at_utc"))
    if probe_rows and t_inj and t_end:
        ep = slo_episodes.violation_episodes(probe_rows, t_inj, t_end)
        if not ep["computable"]:
            out.append(f"2:probe_{ep['reason']}")
    return out


def _v1_common_reasons(kind: str, summary: dict) -> list:
    out = []
    if summary.get("cleanup_errors"):
        out.append("9:cleanup_errors")
    if summary.get("harness_error"):
        out.append("9:harness_error")
    if not (summary.get("probe_image_runtime_check") or {}).get("verified"):
        out.append("9:probe_image_not_verified")
    if kind == "no_injection" and summary.get("probe_data_gap_detected") is not False:
        out.append(f"9:probe_data_gap_detected={summary.get('probe_data_gap_detected')}")
    return out


def g1_replay(ticks: list) -> list:
    """G1 = 후보 IF −0.04 + mem_slope>0 게이트 + 3연속 + 60초 쿨다운. gated_signal_detector.gated_advance_streak()를
    직접 호출한다(판정식을 다시 쓰지 않음). now = feature_computed_at_utc(초) - 실제 탐지기는 monotonic이지만
    쿨다운은 시각 차이만 쓴다. 점수 없는 평가(건너뜀)는 실제 탐지기처럼 연속을 끊는다(None으로 남김)."""
    import sys
    gsd_dir = str(Path(__file__).parent.parent / "anomaly-detection")
    if gsd_dir not in sys.path:
        sys.path.insert(0, gsd_dir)
    import gated_signal_detector as gsd
    out, consec, last = [], 0, None
    for t in ticks:
        if not _finite(t["score"]) or t["t"] is None:
            consec = 0
            out.append(None)
            continue
        step = gsd.gated_advance_streak(t["score"], CANDIDATE_THRESHOLD, t["mem_slope"], consec, last, t["t"].timestamp())
        consec, last = step["consecutive_anomalous"], step["last_signal_at"]
        out.append(step)
    return out


def g1_replay_consistency(lines) -> dict:
    """도구 검증 전용: 기록된 게이트 탐지기 평가(연속·original_fire_condition·gate_pass·signal_send_intended)와
    재생 결과가 평가마다 같은지. 성능 판정에 쓰지 않는다."""
    recs = [json.loads(x) for x in lines if x.strip()]
    ev = [r for r in recs if r.get("record_type") in EVALUATION_RECORD_TYPES]
    steps = g1_replay(load_ticks(json.dumps(r) for r in ev))
    mismatches, n = [], 0
    for r, st in zip(ev, steps):
        if r.get("record_type") != "evaluation_decision":
            continue
        n += 1
        recorded = (r.get("consecutive_anomalous"), r.get("original_fire_condition"), r.get("gate_pass"),
                    r.get("signal_send_intended"))
        replayed = None if st is None else (st["consecutive_anomalous"], st["original_fire_condition"],
                                            st["gate_pass"], st["should_signal"])
        if recorded != replayed:
            mismatches.append({"evaluation_seq": r.get("evaluation_seq"), "recorded": recorded, "replayed": replayed})
    return {"evaluations": n, "mismatches": mismatches, "consistent": n > 0 and not mismatches}


def g1_summary(kind: str, ticks: list, t_inj: Optional[datetime] = None, t_slo: Optional[datetime] = None) -> dict:
    steps = g1_replay(ticks)
    sig = [i for i, st in enumerate(steps) if st and st["should_signal"]]
    fire = [i for i, st in enumerate(steps) if st and st["original_fire_condition"]]
    if kind == "no_injection":
        return {"signals": len(sig), "original_fire_conditions": len(fire), "gate_blocked": len(fire) - len(sig)}
    post = [i for i in sig if ticks[i]["t"] >= t_inj]
    first = ticks[post[0]]["t"] if post else None
    return {"first_signal_rel": _rel(first, t_inj), "pre_injection_signals": len(sig) - len(post),
            "lead_vs_t_slo_sec": _rel(t_slo, first) if first and t_slo else None,
            "gate_blocked": len(fire) - len(sig)}


def coverage_reasons(kind: str, summary: dict, ticks: list) -> list:
    """§5.8 - 평가 기록이 요구 관찰 구간 전체를 덮는지(기록 안 빈틈은 3번이 본다)."""
    times = [t["t"] for t in ticks if t["t"] is not None]
    if not times:
        return []  # 3번이 이미 위반으로 기록
    first, last, tol = min(times), max(times), COVERAGE_TOLERANCE_SEC
    out = []
    end = _ts(summary.get("observation_ended_at_utc"))
    if kind == "pressure":
        start = _ts(summary.get("t_injection_observed_utc"))
        if start is None or end is None:
            return ["8:observation_window_unknown(injection_or_end_missing)"]
    else:
        requested = summary.get("observation_sec_requested")
        if not _finite(requested):
            return ["8:observation_sec_requested_missing"]
        if (last - first).total_seconds() < requested - tol:
            out.append(f"8:evaluation_span={(last - first).total_seconds():.0f}s<requested-{tol:.0f}")
        if end is None:
            return out  # 종료 시각을 기록하기 전 러너 버전 - 길이 조건만(§5.8)
        start = end - timedelta(seconds=requested)
    if (first - start).total_seconds() > tol:
        out.append(f"8:evaluation_starts_late_by={(first - start).total_seconds():.0f}s")
    if (end - last).total_seconds() > tol:
        out.append(f"8:evaluation_ends_early_by={(end - last).total_seconds():.0f}s")
    return out


def harm_times(probe_rows: list, t_inj: datetime) -> dict:
    nb = _align(t_inj, probe_rows)
    t_slo = slo_judge.find_t_slo(slo_judge.evaluate(probe_rows), not_before=nb)
    first_bad = next((r for r in probe_rows if r["sent_at"] >= nb
                      and (not r["success"] or r["latency"] > slo_judge.LATENCY_THRESHOLD)), None)
    return {"t_slo": t_slo, "t_first_bad": first_bad["sent_at"] if first_bad else None}


def _rel(t, t0) -> Optional[float]:
    if t is None or t0 is None:
        return None
    if (t.tzinfo is None) != (t0.tzinfo is None):
        t, t0 = t.replace(tzinfo=None), t0.replace(tzinfo=None)
    return round((t - t0).total_seconds(), 1)


def ordering(if_tick: Optional[int], fixed_tick: Optional[int]) -> str:
    if if_tick is None and fixed_tick is None:
        return "neither"
    if fixed_tick is None:
        return "if_only"
    if if_tick is None:
        return "fixed_only"
    return "if_first" if if_tick < fixed_tick else ("fixed_first" if fixed_tick < if_tick else "same_tick")


def analyze_pressure(summary: dict, ticks: list, probe_rows: list) -> dict:
    t_inj = _ts(summary["t_injection_observed_utc"])
    harm = harm_times(probe_rows, t_inj)
    out = {"t_slo_rel": _rel(harm["t_slo"], t_inj), "t_first_bad_rel": _rel(harm["t_first_bad"], t_inj), "rules": {}}
    for name, pred in RULES.items():
        idx = episodes(ticks, pred)
        pre = [i for i in idx if ticks[i]["t"] < t_inj]
        post = [i for i in idx if ticks[i]["t"] >= t_inj]
        first = ticks[post[0]]["t"] if post else None
        out["rules"][name] = {
            "first_signal_rel": _rel(first, t_inj), "first_signal_tick": post[0] if post else None,
            "pre_injection_episodes": len(pre),
            "lead_vs_t_slo_sec": _rel(harm["t_slo"], first) if first and harm["t_slo"] else None,
            "lead_vs_t_first_bad_sec": _rel(harm["t_first_bad"], first) if first and harm["t_first_bad"] else None,
        }
    out["ordering"] = ordering(out["rules"]["if_candidate"]["first_signal_tick"],
                               out["rules"]["fixed_threshold"]["first_signal_tick"])
    return out


def analyze_no_injection(ticks: list) -> dict:
    hours = (ticks[-1]["t"] - ticks[0]["t"]).total_seconds() / 3600 if len(ticks) > 1 else 0.0
    out = {"ticks": len(ticks), "observed_hours": round(hours, 3), "rules": {}}
    for name, pred in RULES.items():
        n = len(episodes(ticks, pred))
        out["rules"][name] = {"episodes": n, "per_hour": round(n / hours, 3) if hours else None}
    return out


def analyze_run(kind: str, run_id: str, rep: int, evidence_dir: Path = EVIDENCE_DIR,
                probe_dir: Path = RESULTS, with_g1: bool = False, validity_only: bool = False) -> dict:
    summary_path = evidence_dir / f"summary-{run_id}.json"
    if with_g1 and not summary_path.exists():  # V1 고정 목록 중 아직(또는 끝내) 수집되지 않은 ID
        return {"kind": kind, "run_id": run_id, "valid": False, "invalid_reasons": ["not_collected"]}
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    evidence_path = evidence_dir / f"detector-evidence-{run_id}.jsonl"
    if with_g1 and not evidence_path.exists():  # V1: 조기 중단으로 평가 기록이 없는 실행도 보고에 남긴다
        return {"kind": kind, "run_id": run_id, "valid": False, "invalid_reasons": ["3:evidence_missing"]}
    with open(evidence_path, encoding="utf-8") as f:
        ticks = load_ticks(f)
    probe_rows = None
    if kind == "pressure":
        csv = probe_dir / f"probe-{run_id}-proposed-{rep}-raw.csv"
        probe_rows = slo_judge.load_raw(csv) if csv.exists() else None
    log_lines = None
    if with_g1:  # V1: 탐지기 콘솔 로그(독립 기록) - sink 경로 탐지기가 이 이름으로 남긴다
        log_path = RESULTS / "capstone_if_threshold_candidate" / f"score_server-stdout-{run_id}.log"
        log_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines() if log_path.exists() else None
    reasons = validate(kind, summary, ticks, probe_rows, v1=with_g1, detector_log_lines=log_lines)
    result = {"kind": kind, "run_id": run_id, "valid": not reasons, "invalid_reasons": reasons}
    if with_g1:
        result["evaluation_diagnostics"] = evaluation_diagnostics(ticks)
    if not reasons and not validity_only:  # validity_only: 규칙별 경고 비교를 계산하지 않음(유효성 재판정 전용)
        result.update(analyze_pressure(summary, ticks, probe_rows) if kind == "pressure" else analyze_no_injection(ticks))
        if with_g1:
            t_inj = _ts(summary.get("t_injection_observed_utc"))
            t_slo = harm_times(probe_rows, t_inj)["t_slo"] if kind == "pressure" else None
            result["g1"] = g1_summary(kind, ticks, t_inj, t_slo)
    return result


def main():
    parser = argparse.ArgumentParser(description="IF vs 고정 임계치 같은 평가 시점 비교(오프라인, 읽기 전용)")
    parser.add_argument("--out", required=True, help="결과 JSON 경로(experiments/results 밖)")
    parser.add_argument("--set", choices=("dev", "v1"), default="dev",
                        help="dev=2026-10-02 개발 자료 고정 목록(기존 그대로) / v1=V1 계약 고정 목록 + G1 재생")
    parser.add_argument("--validity-only", action="store_true", help="v1 전용: 유효성·진단만, 규칙별 경고 비교는 계산하지 않음")
    args = parser.parse_args()
    out = Path(args.out).resolve()
    if RESULTS.resolve() in out.parents:
        raise SystemExit("원자료 폴더(experiments/results) 안에는 쓰지 않음 - 다른 경로를 지정")
    if out.exists():
        raise SystemExit(f"출력 파일이 이미 있음 - 덮어쓰지 않음(사전 등록 1회 실행 원칙): {out}")
    report = {"protocol": "docs/design/phase8-detector-same-tick-comparison-protocol-20261002.md",
              "thresholds": {"if_candidate": CANDIDATE_THRESHOLD, "if_official": OFFICIAL_THRESHOLD,
                             "fixed_threshold_cores": FIXED_THRESHOLD_CORES, "consecutive": CONSECUTIVE_THRESHOLD},
              "runs": [analyze_run(kind, rid, rep) for kind, rid, rep in RUNS] if args.set == "dev" else
                      [analyze_run(kind, rid, rep, evidence_dir=V1_EVIDENCE_DIR, with_g1=True, validity_only=args.validity_only)
                       for kind, rid, rep in v1_runs()]}
    if args.set == "v1":
        report["contract"] = "docs/design/phase8-v1-detector-validation-contract-20261004.md"
        report["g1_replay_consistency_dev"] = {
            rid: g1_replay_consistency((EVIDENCE_DIR / f"detector-evidence-{rid}.jsonl").read_text(encoding="utf-8").splitlines())
            for rid in G1_DEV_CONSISTENCY_RUNS}
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    for r in report["runs"]:
        print(r["run_id"][:70], "valid" if r["valid"] else f"INVALID {r['invalid_reasons']}")
    print(f"결과: {out}")


if __name__ == "__main__":
    main()
