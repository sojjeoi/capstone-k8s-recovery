"""통일 비교 표 (docs/design/UNIFIED-COMPARISON-PREREG-20261104.md §6). 결과 폴더를 직접 훑어 (장애 × 군) 칸별 SLO 위반 누적을 모은다.
- H·W·C: results/hpa_compare/main/hc-<fault>-<arm>-b<N>[-tag]/summary.json (check·trace-pair 폴더는 제외)
- G: g_stage2.RUNS_G 의 본 비교 ID(기능 점검 90번대, 요청 추적 계측 판 n2s31·n2s32, 시연 캡처 폴더는 제외), 유효성 = 세션 completed·정리 오류 없음
  (F2 는 동결 유효성 기록 p2-s2-validity 의 valid 도 확인)
- 비교: 칸별 중앙값·범위와 G 대 다른 군의 Mann-Whitney U 양측 p(참고값)
사용: python analyze_u.py
"""
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import hc_common as hc  # noqa: E402
import g_stage2 as g  # noqa: E402
import analyze  # noqa: E402

EXCLUDE_G = {"n2s31", "n2s32"}                       # 요청 단위 계측 판 probe(다른 측정 조건)


def hw_rows():
    out = []
    for s in sorted((hc.RESULTS / "main").glob("hc-*/summary.json")):
        d = json.loads(s.read_text(encoding="utf-8"))
        out.append({"fault": d["fault"], "arm": d["arm"], "run": d["run_id"], "valid": d.get("classification") == "valid",
                    "classification": d.get("classification"), "slo_sec": (d.get("slo") or {}).get("cumulative_sec"),
                    "lower_bound": (d.get("slo") or {}).get("cumulative_lower_bound"), "date": (d.get("started_at") or "")[:10]})
    return out


def g_rows():
    out = []
    for run, (fault, block) in sorted(g.RUNS_G.items()):
        if run in EXCLUDE_G or int(run[3:]) >= 90:
            continue
        d = g.results_dir_for(run)
        sess = d / f"p2-s2-session-{run}.json"
        if not sess.exists():
            continue
        rec = json.loads(sess.read_text(encoding="utf-8"))
        if not (d / f"summary-{rec['run_id']}.json").exists():       # 진입 전 거부(주입 없음)·실행 중: 값 없음, 무효로만 기록
            out.append({"fault": fault, "arm": "G", "run": run, "valid": False, "classification": rec.get("status"),
                        "slo_sec": None, "lower_bound": None, "date": ""})
            continue
        valid = rec.get("outcome_status") == "completed" and not rec.get("cleanup_errors")
        vf = d / f"p2-s2-validity-{run}.json"
        if fault == "f2" and vf.exists():
            valid = valid and bool(json.loads(vf.read_text(encoding="utf-8")).get("valid"))
        r, _ = analyze.row_g(run, fault, block)
        slo = (r or {}).get("slo") or {}
        out.append({"fault": fault, "arm": "G", "run": run, "valid": valid and bool(slo.get("computable")),
                    "classification": rec.get("outcome_status"), "slo_sec": slo.get("cumulative_sec"),
                    "lower_bound": slo.get("cumulative_lower_bound"), "date": ((r or {}).get("t0") or "")[:10]})
    return out


def mwu_p(a, b):
    """Mann-Whitney U 양측 p. scipy 가 있으면 정확 검정, 없으면 None."""
    try:
        from scipy.stats import mannwhitneyu
        return float(mannwhitneyu(a, b, alternative="two-sided", method="exact" if len(a) + len(b) <= 40 else "auto").pvalue)
    except Exception:  # noqa: BLE001
        return None


def main():
    rows = hw_rows() + g_rows()
    cells = {}
    for r in rows:
        if r["valid"] and r["slo_sec"] is not None:
            cells.setdefault((r["fault"], r["arm"]), []).append(r["slo_sec"])
    names = {"f1": "CPU 압박", "f2": "네트워크 지연", "f3": "요청 폭주"}
    arms = {"C": "무전환", "H": "오토스케일링", "W": "계속 분산", "G": "우리 시스템"}
    lines = ["# 통일 비교 표", "", f"생성 {datetime.now(timezone.utc).isoformat(timespec='seconds')} · SLO 위반 누적(초) · 유효 세션만", "",
             "| 장애 | 군 | n | 중앙값 | 최소~최대 | G 대비 MWU p |", "|---|---|---|---|---|---|"]
    summary = {}
    for f in ("f1", "f2", "f3"):
        gv = cells.get((f, "G"), [])
        for arm in ("C", "H", "W", "G"):
            v = cells.get((f, arm), [])
            if not v:
                lines.append(f"| {names[f]} | {arms[arm]} | 0 | - | - | - |")
                continue
            p = mwu_p(gv, v) if arm != "G" and gv else None
            summary[f"{f}-{arm}"] = {"n": len(v), "median": statistics.median(v), "min": min(v), "max": max(v), "values": v, "mwu_p_vs_G": p}
            lines.append(f"| {names[f]} | {arms[arm]} | {len(v)} | {statistics.median(v):.1f} | {min(v):.1f}~{max(v):.1f} | "
                         f"{'-' if p is None else f'{p:.4f}'} |")
    invalid = [r for r in rows if not r["valid"]]
    lines += ["", "## 무효 세션(수치 제외)", ""] + [f"- {r['fault']} {r['arm']} {r['run']}: {r['classification']}" for r in invalid]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = hc.RESULTS / "analysis"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"UNIFIED-TABLE-{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / f"UNIFIED-TABLE-{stamp}.json").write_text(json.dumps({"cells": summary, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
