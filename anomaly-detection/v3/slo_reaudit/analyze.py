#!/usr/bin/env python3
"""§78 - idle/low_load(0.025 RPS) 무장애 세션 전체에 대한 오프라인
재검증. 추가 live 측정 없음 - 보존된 session JSON과 raw probe CSV만
읽는다. slo_judge는 동결된 그대로 재사용(새 판정 로직 없음)."""
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

EXPERIMENTS_DIR = Path(__file__).parent.parent.parent.parent / "experiments"
V3_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(EXPERIMENTS_DIR))
sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

import slo_judge  # noqa: E402

from loso import leave_one_session_out  # noqa: E402
from stats_utils import exact_binomial_ci, outside_robust_range, robust_range  # noqa: E402

# §78.1 registry 기준(측정 대상 확정 후 고정) - v3.1/v3.2 프로토콜과 정확히
# 일치하는(active_plus_preview, 전용 idle/low_load=0.025 RPS regime) 세션만
# 1차 목록에 넣는다. 0.10 RPS이거나 topology가 다른 것은 별도 등록하되
# 위반율 계산에서는 제외(§78.1 명시).
IDLE_SESSIONS = [
    ("anomaly-detection/v3/v31_data/sessions/v31-train-idle-20260920.json", "v3.1 train"),
    ("anomaly-detection/v3/v31_data/sessions/v31-calib-idle-20260920.json", "v3.1 calibration"),
    ("anomaly-detection/v3/v31_data/sessions/v31-holdout-idle-20260920.json", "v3.1 holdout"),
    ("anomaly-detection/v3/v31_data/sessions/calib2-idle-01.json", "v3.2 calibration attempt"),
]
LOW_LOAD_SESSIONS = [
    ("anomaly-detection/v3/qualification_data/sessions/q3c-low_load-20260920-r2.json", "§64 qualification"),
    ("anomaly-detection/v3/official_data/sessions/official-train-low_load-20260920.json", "§66 official train"),
    ("anomaly-detection/v3/official_data/sessions/official-calib-low_load-20260920.json", "§68 official calibration"),
    ("anomaly-detection/v3/v31_data/sessions/v31-train-low_load-20260920.json", "v3.1 train"),
    ("anomaly-detection/v3/v31_data/sessions/v31-calib-low_load-20260920.json", "v3.1 calibration"),
    ("anomaly-detection/v3/v31_data/sessions/v31-holdout-low_load-20260920.json", "v3.1 holdout"),
    ("anomaly-detection/v3/v31_data/sessions/calib2-low-01.json", "v3.2 calibration attempt"),
    ("anomaly-detection/v3/v31_data/sessions/calib2-low-02.json", "v3.2 calibration attempt"),
]
# 프로토콜 불일치(0.10 RPS 및/또는 topology 다름) - 등록만 하고 위반율 계산 제외.
PROTOCOL_MISMATCHED = [
    ("anomaly-detection/v3/data/sessions/qual-low_load-20260920-r6.json", "§60 - 0.10 RPS(4-core 시절 값), boundary_challenge_set"),
    ("anomaly-detection/v3/diagnostics/aba-a1-result.json", "§61-62 A-B-A A1 - 0.10 RPS, active_only"),
    ("anomaly-detection/v3/diagnostics/aba-b-result.json", "§61-62 A-B-A B - 0.10 RPS, active_plus_preview"),
    ("anomaly-detection/v3/diagnostics/aba-a2-result.json", "§61-62 A-B-A A2 - 0.10 RPS, active_only_post_abort"),
]

REPO_ROOT = Path(__file__).parent.parent.parent.parent


def _sha256_file(path: Path):
    import hashlib
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_session(rel_path: str) -> dict:
    return json.loads((REPO_ROOT / rel_path).read_text(encoding="utf-8"))


def registry_entry(rel_path: str, note: str) -> dict:
    d = load_session(rel_path)
    stages = d.get("ramp_candidate_result", {}).get("stages") or []
    success_rate = d.get("ramp_candidate_result", {}).get("all_success_100pct")
    local_raw = d.get("ramp_candidate_result", {}).get("local_raw")
    local_raw_path = Path(local_raw) if local_raw else None
    invalid_windows = (d.get("inventory") or {}).get("invalid_rows")
    return {
        "session_id": d.get("session_id"), "note": note,
        "t_session_start": d.get("t_session_start"),
        "profile": d.get("profile"), "topology": d.get("topology"),
        "split_role_or_purpose": d.get("split_role") or d.get("purpose"),
        "session_json_sha256": _sha256_file(REPO_ROOT / rel_path),
        "raw_csv_path": local_raw, "raw_csv_sha256": _sha256_file(local_raw_path) if local_raw_path else None,
        "success_rate_all_stages_100pct": success_rate,
        "stored_t_slo": d.get("t_slo"),
        "restart_changed": None,  # judge_qualification 계산에 이미 반영돼 session에 별도 원시 필드로 없음 - excluded/reasons로 갈음
        "oom_or_node_issue_reasons": [r for r in (d.get("exclusion_reasons") or [])
                                       if "OOM" in r or "Node" in r or "restart" in r],
        "cleanup_result": d.get("cleanup_result"),
        "stored_excluded": d.get("excluded"), "stored_exclusion_reasons": d.get("exclusion_reasons"),
        "invalid_windows": invalid_windows,
        "data_complete": (invalid_windows == 0) if invalid_windows is not None else None,
        "_full_session": d,  # 재검증 단계에서 재사용, 최종 registry 출력 시 제거
    }


def reverify_slo(entry: dict) -> dict:
    """slo_judge를 원본 raw CSV에 그대로 재적용 - t_slo/t_recovery를
    독립적으로 재계산해 저장된 값과 대조한다."""
    raw_path = entry["raw_csv_path"]
    if not raw_path or not Path(raw_path).exists():
        return {"reverify_status": "raw_csv_missing", "recomputed_t_slo": None, "recomputed_t_recovery": None,
                "matches_stored": None}
    rows = slo_judge.load_raw(raw_path)
    points = slo_judge.evaluate(rows)
    t_slo = slo_judge.find_t_slo(points)
    t_recovery = slo_judge.find_t_recovery(points, t_slo) if t_slo else None

    lat = [r["latency"] for r in rows]
    stored_t_slo_str = entry["stored_t_slo"]
    recomputed_t_slo_str = t_slo.isoformat() if t_slo else None
    matches = (stored_t_slo_str is None) == (recomputed_t_slo_str is None)
    if stored_t_slo_str and recomputed_t_slo_str:
        matches = abs((datetime.fromisoformat(stored_t_slo_str) - datetime.fromisoformat(recomputed_t_slo_str)).total_seconds()) < 1.0

    max_consecutive_violation_sec = 0.0
    streak_start = None
    for p in points:
        if p["latency_violating"]:
            streak_start = streak_start or p["t"]
            max_consecutive_violation_sec = max(max_consecutive_violation_sec, (p["t"] - streak_start).total_seconds())
        else:
            streak_start = None

    return {
        "reverify_status": "ok",
        "n_raw_samples": len(rows),
        "recomputed_t_slo": recomputed_t_slo_str,
        "recomputed_t_recovery": t_recovery.isoformat() if t_recovery else None,
        "matches_stored": matches,
        "raw_latency_median": statistics.median(lat) if lat else None,
        "raw_latency_p95": (statistics.quantiles(lat, n=100)[94] if len(lat) >= 20 else max(lat)) if lat else None,
        "raw_latency_max": max(lat) if lat else None,
        "max_consecutive_violation_sec": max_consecutive_violation_sec,
    }


def feature_time_series_near_slo(entry: dict, lead_sec: float = 120.0) -> dict:
    """t_slo(또는 recomputed 값) 기준 lead_sec 이전부터의 feature_rows와,
    나머지 무장애 세션에서 같은 stage 내 어느 시점에서든 관측된 feature
    범위를 비교할 수 있도록 원자료만 뽑아 반환한다(판정은 하지 않음)."""
    session = entry["_full_session"]
    rows = [r for r in session.get("feature_rows", []) if r["valid"]]
    t_slo_str = entry.get("recomputed_t_slo") or entry.get("stored_t_slo")
    if not t_slo_str:
        return {"has_violation": False, "rows_near_slo": []}
    t_slo_dt = datetime.fromisoformat(t_slo_str)
    lead_start = t_slo_dt - timedelta(seconds=lead_sec)
    near = [r for r in rows if lead_start <= datetime.fromisoformat(r["window_start_utc"]) <= t_slo_dt]
    return {"has_violation": True, "t_slo": t_slo_str, "rows_near_slo": near}


def all_valid_features_flat(entries: list) -> list:
    out = []
    for e in entries:
        session = e["_full_session"]
        out.extend(r["features"] for r in session.get("feature_rows", []) if r["valid"])
    return out


FEATURE_NAMES = ["cpu_mean", "cpu_slope", "memory_mean", "memory_slope", "queue_mean", "queue_slope", "cache_mean", "cache_slope"]


def main():
    idle_entries = [registry_entry(p, n) for p, n in IDLE_SESSIONS]
    low_load_entries = [registry_entry(p, n) for p, n in LOW_LOAD_SESSIONS]
    mismatched_entries = [registry_entry(p, n) for p, n in PROTOCOL_MISMATCHED]

    for e in idle_entries + low_load_entries + mismatched_entries:
        e["reverify"] = reverify_slo(e)

    report = {"idle": [], "low_load": [], "protocol_mismatched": []}
    for label, entries in (("idle", idle_entries), ("low_load", low_load_entries), ("protocol_mismatched", mismatched_entries)):
        for e in entries:
            out = {k: v for k, v in e.items() if k != "_full_session"}
            report[label].append(out)

    # §78.3 - session-level 자연 위반율(재검증 결과 기준)
    def violation_count(entries):
        return sum(1 for e in entries if e["reverify"]["recomputed_t_slo"] is not None)

    idle_violations, idle_n = violation_count(idle_entries), len(idle_entries)
    low_load_violations, low_load_n = violation_count(low_load_entries), len(low_load_entries)
    idle_ci = exact_binomial_ci(idle_violations, idle_n)
    low_load_ci = exact_binomial_ci(low_load_violations, low_load_n)

    report["violation_rates"] = {
        "idle": {"violations": idle_violations, "n": idle_n, "rate": idle_violations / idle_n if idle_n else None,
                 "ci_95_exact_binomial": idle_ci},
        "low_load": {"violations": low_load_violations, "n": low_load_n, "rate": low_load_violations / low_load_n if low_load_n else None,
                      "ci_95_exact_binomial": low_load_ci},
    }

    # §78.4/§78.5 - 실패 세션 vs PASS 세션 feature 비교 + LOSO
    failed_low_load = [e for e in low_load_entries if e["reverify"]["recomputed_t_slo"] is not None]
    passed_low_load = [e for e in low_load_entries if e["reverify"]["recomputed_t_slo"] is None]

    comparison = []
    for failed in failed_low_load:
        other_passed = passed_low_load  # LOSO: 실패 세션 자신은 애초에 "나머지 무장애 세션"에 안 들어감
        ref_all_rows = all_valid_features_flat(other_passed)
        near_slo = feature_time_series_near_slo(failed, lead_sec=120.0)
        per_feature = {}
        for i, name in enumerate(FEATURE_NAMES):
            ref_vals = [row[i] for row in ref_all_rows]
            ref_range = robust_range(ref_vals)
            near_vals = [r["features"][i] for r in near_slo["rows_near_slo"]]
            outside_flags = [outside_robust_range(v, ref_range) for v in near_vals]
            per_feature[name] = {
                "pass_session_robust_range": ref_range,
                "values_in_120s_before_t_slo": near_vals,
                "any_outside_pass_range": any(outside_flags),
                "all_outside_pass_range": all(outside_flags) if outside_flags else None,
            }
        comparison.append({
            "session_id": failed["session_id"], "t_slo": near_slo["t_slo"],
            "n_pass_reference_sessions": len(other_passed), "n_pass_reference_rows": len(ref_all_rows),
            "per_feature": per_feature,
        })
    report["failed_vs_passed_feature_comparison"] = comparison

    # §78.5 - 세션을 통계 단위로 삼는 LOSO(analyze.py의 위 풀링-quartile
    # 비교는 세션 간 이질성 때문에 오도될 수 있어 - 실제로 여러 PASS
    # 세션도 넓은 개별 범위를 보임 - loso.py가 세션 단위로 교정한다).
    report["leave_one_session_out"] = leave_one_session_out(low_load_entries)

    out_path = Path(__file__).parent / "reaudit_report.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"저장: {out_path}")

    print(f"\nidle: {idle_violations}/{idle_n} 위반, 95% CI={idle_ci}")
    print(f"low_load: {low_load_violations}/{low_load_n} 위반, 95% CI={low_load_ci}")
    for e in idle_entries + low_load_entries:
        rv = e["reverify"]
        print(f"  {e['session_id']}: stored_t_slo={e['stored_t_slo']} recomputed={rv['recomputed_t_slo']} "
              f"matches={rv['matches_stored']} raw_p95={rv.get('raw_latency_p95')}")
    for c in comparison:
        print(f"\n=== {c['session_id']} (t_slo={c['t_slo']}) - 120초 이전 feature vs PASS 세션 풀링 quartile(주의: 세션 이질성으로 오도 가능, 아래 LOSO가 교정판) ===")
        for name, info in c["per_feature"].items():
            print(f"  {name}: any_outside_pass_range={info['any_outside_pass_range']} "
                  f"values={info['values_in_120s_before_t_slo']} pass_range=[{info['pass_session_robust_range']['q1']}, {info['pass_session_robust_range']['q3']}]")

    print("\n=== LOSO(세션 단위) - calib2-low-02가 다른 7개 세션의 전체 관측 범위를 벗어나는가 ===")
    for diag in report["leave_one_session_out"]:
        if diag["session_id"] != "calib2-low-02":
            continue
        for name, info in diag["per_feature"].items():
            print(f"  {name}: target=[{info['target_min']:.4g}, {info['target_max']:.4g}] "
                  f"others_full_range=[{info['other_sessions_full_range']['min']:.4g}, {info['other_sessions_full_range']['max']:.4g}] "
                  f"min_outside={info['target_min_outside_others_full_range']} max_outside={info['target_max_outside_others_full_range']}")


if __name__ == "__main__":
    main()
