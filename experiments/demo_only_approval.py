#!/usr/bin/env python3
"""demo-only act 승인 기록 생성(2026-10-10). gap02 승인 기록과 같은 형식 + demo_only/counted/runs.
값은 gap01 사용자 확정값 그대로(새 값 없음). 새 파일만 만든다("x"). 사람이 실행하고, 실행 = 그 시연 1회 승인 기록.
사용: python experiments/demo_only_approval.py --run demo01 --clock <clock-offset.json> --out <승인 파일> --approved-by "<누가, 언제 승인>" """
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def build(run: str, clock: Path, approved_by: str) -> dict:
    cs = json.loads(clock.read_text(encoding="utf-8"))["summary"]
    bound = abs(cs["best_offset_sec"]) + cs["best_uncertainty_sec"]
    if cs["n_ok"] < 10 or bound > 0.5:
        raise SystemExit(f"시계 측정 불충분 - 승인 기록 만들지 않음: n_ok={cs['n_ok']} bound={bound:.3f}s (> 0.5 s)")
    return {"status": "final", "run": run, "runs": [run], "demo_only": True, "counted": False,
            "scope": f"교수 시연 전용 {run} 1회(counted=false, 연구 표본·공식 실험 아님)",
            "approved_by": approved_by, "approved_at_utc": datetime.now(timezone.utc).isoformat(),
            "timing_source": "gap01 사용자 확정값 재사용(gap02·A/B·B/C/K와 동일)",
            "clock_measurement_file": str(clock.resolve()), "clock_bound_sec": bound,
            "timing": {"max_signal_age_sec": 15.0, "max_future_skew_sec": 2.0, "max_window_to_decision_sec": 10.0},
            "service_wait_deadline_sec": 120.0, "scale_down_wait_margin_sec": 180.0,
            "rules": ["결과 불명·실패: 자동 재승격·미검증 롤백·자동 재시도 금지", "threshold·압박·관찰 변경 금지"]}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run", required=True)
    p.add_argument("--clock", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--approved-by", required=True)
    a = p.parse_args()
    rec = build(a.run, Path(a.clock), a.approved_by)
    with open(a.out, "x", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, indent=1))
    print(a.out, "clock_bound", round(rec["clock_bound_sec"], 3))


if __name__ == "__main__":
    main()
