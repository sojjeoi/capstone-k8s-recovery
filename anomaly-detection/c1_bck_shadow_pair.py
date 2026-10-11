"""정상 운전 N1 공통 tick 섀도 탐지기(새 파일, 2026-10-08) - 계약: docs/design/phase8-v33c1-guarded-normal-bc-n1-contract-20261008.md

동결 B/C/K 탐지기(c1_bck_live_detector)의 판정 부품만 import한다(수정 없음):
  평가 1회 = 동결 evaluate_once 1회, 판정 = 동결 judge_all(B·C·K 규칙별 독립 3연속·쿨다운 60 s, skip 시 연속 0),
  규칙 명세 = 동결 load_spec(rule-spec-v1, 코드 상수와 대조 fail-closed).
다른 점은 하나: 신호를 어디에도 보내지 않는다(실제 arm 없음, 정책 URL 없음). 동결 탐지기 CLI는 act만 받고 실제 arm 신호를
항상 /signal/v2로 보내므로 N1(record_only·송신 없음·arm 순서 없음)에 그대로 쓸 수 없다."""
import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import c1_bck_live_detector as bd
from c1_shadow_detector import (COOLDOWN_SEC, CONSECUTIVE_THRESHOLD, DEFAULT_ARTIFACTS, EVAL_INTERVAL_SEC, WINDOW_SEC,
                                EvidenceWriteFailed, _must_write, evaluate_once, k8s_snapshot, load_artifacts,
                                prom_instant, prom_range)

DETECTOR_ID = "c1_bck_shadow_pair_v1"
EXIT_OK, EXIT_EVIDENCE = 0, 2


def run_shadow(run_id: str, evidence_path: Path, *, artifacts=DEFAULT_ARTIFACTS, spec_path: Path = bd.RULE_SPEC,
               max_evals=None, now_fn=lambda: datetime.now(timezone.utc), monotonic_fn=time.monotonic,
               sleep_fn=time.sleep, snapshot_fn=k8s_snapshot, range_fn=prom_range, instant_fn=prom_instant,
               evaluate_fn=evaluate_once) -> int:
    art = load_artifacts(Path(artifacts))
    spec = bd.load_spec(spec_path)
    states = {a: {"consecutive": 0, "last_signal": None} for a in bd.ARMS}
    seq = 0
    with open(evidence_path, "a", encoding="utf-8") as fh:
        _must_write(fh, {"record_type": "c1_shadow_start", "run_id": run_id, "detector": DETECTOR_ID,
                         "arms": list(bd.ARMS), "actuating_rule": None, "signal_send": False,
                         "rule_spec": spec["path"], "rule_spec_sha256": spec["sha256"], "rules": spec["spec"]["rules"],
                         "thresholds": dict(bd.THRESHOLDS), "artifacts": str(artifacts), "artifact_hashes": art["hashes"],
                         "if_threshold": art["threshold"], "kept_features": art["schema"]["kept_feature_names"],
                         "window_sec": WINDOW_SEC, "eval_interval_sec": EVAL_INTERVAL_SEC,
                         "consecutive_threshold": CONSECUTIVE_THRESHOLD, "cooldown_sec": COOLDOWN_SEC,
                         "started_at_utc": now_fn().isoformat()})
        while max_evals is None or seq < max_evals:
            seq += 1
            rec = evaluate_fn(art, now_fn=now_fn, snapshot_fn=snapshot_fn, range_fn=range_fn, instant_fn=instant_fn)
            rules = bd.judge_all(rec, states, monotonic_fn())
            rec.update(record_type="c1_evaluation", run_id=run_id, evaluation_seq=seq, rules=rules,
                       actuating_rule=None, signal_send=False)
            _must_write(fh, rec)
            flags = " ".join(f"{a}={rules[a]['consecutive']}{'!' if rules[a]['condition_met'] else ''}" for a in bd.ARMS)
            print(f"[{rec.get('score_completed_at_utc') or rec.get('skipped_at_utc')}] seq={seq} {rec['status']} {flags}",
                  flush=True)
            sleep_fn(EVAL_INTERVAL_SEC)
    return EXIT_OK


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="N1 공통 tick 섀도 - B·C·K 판정 기록만, 송신 없음")
    p.add_argument("--run-id", required=True)
    p.add_argument("--evidence-log", required=True, type=Path)
    p.add_argument("--artifacts-dir", default=str(DEFAULT_ARTIFACTS))
    a = p.parse_args(argv)
    try:
        return run_shadow(a.run_id, a.evidence_log, artifacts=a.artifacts_dir)
    except EvidenceWriteFailed as e:
        print(f"필수 기록 실패 - 종료: {e}", file=sys.stderr, flush=True)
        return EXIT_EVIDENCE


if __name__ == "__main__":
    sys.exit(main())
