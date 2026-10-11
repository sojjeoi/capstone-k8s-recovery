"""B/C/K 계약용 C(보정 고정 CPU) 임계값 재현 - 오프라인, 읽기 전용(새 결과 파일만 "x"로 쓴다).

IF 임계값과 같은 함수(model_v31/replay.calibrate_threshold)·같은 보정 4세션(c1ca01-04, 매니페스트 해시·행 seq 연속 확인 =
train_calibrate_v33c1.load_session_rows)에 점수 = -cpu_mean을 넣는다. -cpu_mean < -T  <=>  cpu_mean > T (strict).
결과: "모든 보정 세션에서 3연속 경고 0인 가장 민감한 값". 동결 운영값 1.6843·legacy 2.7도 같은 재생으로 확인한다.
ab01-ab05 등 압박 실행 결과는 읽지 않는다."""
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "anomaly-detection" / "v3" / "model_v33_c1"))
import train_calibrate_v33c1 as t  # noqa: E402  (model_v31·c1_collection 경로를 함께 잡는다)
from replay import calibrate_threshold, replay_detector  # noqa: E402

OUT = Path(__file__).parent / "analysis_v1" / "bck-fixed-threshold-calibration-20261007.json"
FROZEN = {"C_calibrated": 1.6843, "K_legacy": 2.7}
KW = dict(consecutive_threshold=t.CONSECUTIVE, cooldown_sec=t.COOLDOWN_SEC, eval_interval_sec=t.EVAL_INTERVAL_SEC)
KEYS = ("n_points", "point_anomaly_count", "signal_count", "max_consecutive_anomalous")


def main():
    man = json.loads(t.MANIFEST.read_text(encoding="utf-8"))
    ci = t.FEATURE_NAMES.index("cpu_mean")
    cpu = {s: [r[ci] for r in t.load_session_rows(s, man["sessions"][s])[0]] for s in man["calibration"]}
    neg = {s: [-v for v in xs] for s, xs in cpu.items()}
    cal = calibrate_threshold(neg, **KW)
    exact = -cal["threshold"]
    out = {"created_at_utc": datetime.now(timezone.utc).isoformat(), "script": Path(__file__).name,
           "rule": "calibrate_threshold(score=-cpu_mean): 모든 보정 세션 3연속 경고 0인 가장 민감한 값; 경고 = cpu_mean > T (strict)",
           "manifest": str(t.MANIFEST.relative_to(ROOT)).replace("\\", "/"), "manifest_sha256": t.sha256(t.MANIFEST),
           "calibration_sessions": man["calibration"], "replay_params": KW,
           "computed_threshold_exact": exact, "calibration_failed": cal["calibration_failed"],
           "per_session_at_exact": {s: {k: r[k] for k in KEYS} for s, r in cal["per_session"].items()},
           "frozen": {name: {"threshold": T, "per_session": {
               s: {k: replay_detector(xs, -T, **KW)[k] for k in KEYS} for s, xs in neg.items()}} for name, T in FROZEN.items()},
           "observed_between_exact_and_1.6843": sorted(v for xs in cpu.values() for v in xs if exact < v <= FROZEN["C_calibrated"]),
           "cpu_mean_by_session": {s: {"n": len(xs), "median": statistics.median(xs), "max": max(xs)} for s, xs in cpu.items()}}
    with open(OUT, "x", encoding="utf-8") as fh:
        fh.write(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps({k: out[k] for k in ("computed_threshold_exact", "calibration_failed", "observed_between_exact_and_1.6843")}))


if __name__ == "__main__":
    main()
