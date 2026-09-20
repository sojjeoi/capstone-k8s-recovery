#!/usr/bin/env python3
"""v3 정상 데이터 파이프라인(2026-09-20, Isolation Forest 감사 후속) - v1
(data/regimes.jsonl, artifacts/model.pkl·scaler.pkl)은 이력으로 그대로 두고
이 모듈은 절대 덮어쓰지 않는다.

features.py의 PromQL·mean/slope 계산(METRICS, _query_range, _mean_slope)을
그대로 재사용한다 - 새 지표·새 계산식을 만들지 않는다. 이 파일이 얹는 것은
세 가지뿐이다:
  1. score_server.py와 정확히 같은 고정 60초 트레일링 윈도우·15초 스텝을
     세션 구간 안에서 롤링 생성(inference와 동일 window - train.py처럼
     세션 전체를 평균 내지 않는다).
  2. **strict completeness**: 4개 지표 중 하나라도 빈 응답이면 그 윈도우
     전체를 invalid로 버린다 - features.py._mean_slope([])의 (0.0, 0.0)
     완충을 학습 데이터 생성 경로에서는 절대 쓰지 않는다(감사 지적사항).
     주의: features.py/score_server.py 자체(런타임)는 이번에 손대지
     않는다 - 이 완충은 실시간 경로에서는 그대로 남는다.
  3. 세션 단위 결정론적 split(seed 고정) + 중복 검출 + manifest 기록.

CLI로 직접 fit()을 부르지 않는다 - "아직 official 학습 데이터로 확정하거나
모델을 fit하지 말라"는 지시대로, 이 스크립트의 산출물은 inventory/manifest
JSON뿐이다."""
import argparse
import json
import random
import subprocess
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))  # features.py를 상위 디렉터리에서 임포트
sys.stdout.reconfigure(encoding="utf-8")

from features import FEATURE_NAMES, METRICS, _mean_slope, _query_range  # noqa: E402

from windows import ALL_CANDIDATE_SESSIONS, CandidateSession, validate_sessions  # noqa: E402

V3_DIR = Path(__file__).parent
DATA_DIR = V3_DIR / "data"

WINDOW_SEC = 60.0  # score_server.py.WINDOW_SEC과 반드시 동일(추론과 같은 window - 감사 지적사항 수정)
STEP_SEC = 15.0    # score_server.py.EVAL_INTERVAL_SEC과 동일
DEFAULT_SPLIT_SEED = 20260920
DEFAULT_SPLIT_RATIOS = (0.6, 0.2, 0.2)  # train/calibration/holdout


@dataclass
class WindowRow:
    session_id: str
    regime: str
    topology: str
    source_run_id: str
    window_start_utc: str
    window_end_utc: str
    valid: bool
    invalid_reason: Optional[str] = None
    features: Optional[list] = None


def iter_window_starts(session_start: datetime, session_end: datetime,
                        window_sec: float = WINDOW_SEC, step_sec: float = STEP_SEC) -> list:
    """[w_start, w_start+window_sec)가 세션 구간 안에 완전히 들어가는
    시작점만 생성한다 - 세션 밖으로 새는 창은 아예 안 만든다."""
    starts = []
    t = session_start
    while t + timedelta(seconds=window_sec) <= session_end:
        starts.append(t)
        t += timedelta(seconds=step_sec)
    return starts


def extract_window_strict(start: datetime, end: datetime,
                           query_range_fn: Callable = _query_range) -> tuple:
    """features.py.extract_features()와 같은 순서(METRICS dict 순서)로
    계산하되, 한 지표라도 빈 응답이면 (None, 사유)를 반환한다 - 0으로
    채우지 않는다(감사 지적: 결측=0 은닉 금지)."""
    feats = []
    for metric_name, promql in METRICS.items():
        values = query_range_fn(promql, start, end)
        if not values:
            return None, f"{metric_name} 지표 응답 없음({start.isoformat()}~{end.isoformat()})"
        mean, slope = _mean_slope(values)
        feats.extend([mean, slope])
    return feats, None


def build_rows_for_session(session: CandidateSession, query_range_fn: Callable = _query_range) -> list:
    rows = []
    for w_start in iter_window_starts(session.start_utc, session.end_utc):
        w_end = w_start + timedelta(seconds=WINDOW_SEC)
        feats, reason = extract_window_strict(w_start, w_end, query_range_fn)
        rows.append(WindowRow(
            session_id=session.session_id, regime=session.regime, topology=session.topology,
            source_run_id=session.source_run_id,
            window_start_utc=w_start.isoformat(), window_end_utc=w_end.isoformat(),
            valid=feats is not None, invalid_reason=reason, features=feats,
        ))
    return rows


def find_duplicate_timestamps(rows: list) -> list:
    """세션이 다른데 window_start_utc가 완전히 겹치는 경우 - 후보 세션
    정의 실수(같은 구간을 두 번 등록)를 잡아낸다."""
    seen = {}
    dups = []
    for r in rows:
        key = r.window_start_utc
        if key in seen and seen[key] != r.session_id:
            dups.append((key, seen[key], r.session_id))
        seen.setdefault(key, r.session_id)
    return dups


def find_duplicate_feature_vectors(rows: list) -> list:
    """유효한 서로 다른 (session, window) 쌍이 부동소수점까지 완전히 같은
    feature 벡터를 내면 의심스럽다(예: 쿼리가 캐시된 값을 반환했거나 두
    세션이 실제로는 같은 시계열을 가리킴) - 순수 함수, 오프라인 테스트 대상."""
    seen = {}
    dups = []
    for r in rows:
        if not r.valid:
            continue
        key = tuple(r.features)
        if key in seen and seen[key] != (r.session_id, r.window_start_utc):
            dups.append({"vector": key, "first": seen[key], "second": (r.session_id, r.window_start_utc)})
        else:
            seen.setdefault(key, (r.session_id, r.window_start_utc))
    return dups


def split_sessions(sessions: list, seed: int = DEFAULT_SPLIT_SEED,
                    ratios: tuple = DEFAULT_SPLIT_RATIOS) -> dict:
    """세션 단위 결정론적 split - **(regime, topology) 조합별로 독립
    층화**해서 각 split에 그 조합의 세션이 최소 1개는 들어가게 한다
    (지시: "train/calibration/holdout에 각 regime가 최소 1개 이상의 독립
    session을 갖도록 함"). regime만으로 층화하면 지금처럼 regime이 1종류뿐일
    때 우선순위 topology(active_plus_preview, §2 결론)가 세 split 중 하나에
    아예 안 들어갈 수 있다(실측 확인 - 처음 버전은 calibration에 active_
    plus_preview가 0개였다) - topology도 같이 층화해 이 문제를 막는다. 같은
    session의 row가 절대 둘로 안 나뉜다(세션 자체를 배정 단위로 씀). 세션
    수가 모자란 조합은 억지로 3분할하지 않고 그 사실을 shortfalls에 남긴다
    (거짓으로 채우지 않는다)."""
    by_key = defaultdict(list)
    for s in sessions:
        by_key[(s.regime, s.topology)].append(s.session_id)

    result = {"train": [], "calibration": [], "holdout": [], "seed": seed, "ratios": list(ratios), "shortfalls": []}
    for key in sorted(by_key):
        ids = sorted(set(by_key[key]))
        rng = random.Random(seed)
        rng.shuffle(ids)
        n = len(ids)
        label = f"{key[0]}/{key[1]}"
        if n >= 3:
            n_train = max(1, round(n * ratios[0]))
            n_train = min(n_train, n - 2)
            n_calib = max(1, round(n * ratios[1]))
            n_calib = min(n_calib, n - n_train - 1)
            train_ids, calib_ids, holdout_ids = ids[:n_train], ids[n_train:n_train + n_calib], ids[n_train + n_calib:]
        elif n == 2:
            train_ids, calib_ids, holdout_ids = ids[:1], [], ids[1:]
            result["shortfalls"].append(f"{label}: 세션 2개뿐 - calibration 확보 못 함(train/holdout만 배정)")
        elif n == 1:
            train_ids, calib_ids, holdout_ids = ids[:], [], []
            result["shortfalls"].append(f"{label}: 세션 1개뿐 - train에만 배정(calibration/holdout 확보 못 함)")
        else:
            train_ids, calib_ids, holdout_ids = [], [], []
        result["train"] += train_ids
        result["calibration"] += calib_ids
        result["holdout"] += holdout_ids
    return result


def _git_commit_sha() -> Optional[str]:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=V3_DIR, capture_output=True,
                               text=True, timeout=5, check=True).stdout.strip()
    except Exception:
        return None


def summarize_inventory(rows: list, sessions: list) -> dict:
    """§4 후보 데이터 inventory - row 수와 독립 session 수를 항상 분리해서
    보고한다(지시). 재학습·threshold 결정에 쓰지 않는 순수 집계."""
    valid_rows = [r for r in rows if r.valid]
    invalid_rows = [r for r in rows if not r.valid]

    def _stats(name_idx):
        values = [r.features[name_idx] for r in valid_rows if r.features is not None]
        if not values:
            return None
        n = len(values)
        sorted_v = sorted(values)
        median = sorted_v[n // 2] if n % 2 else (sorted_v[n // 2 - 1] + sorted_v[n // 2]) / 2
        mean = sum(values) / n
        std = (sum((v - mean) ** 2 for v in values) / n) ** 0.5
        return {"min": min(values), "median": median, "max": max(values), "std": std, "n": n}

    feature_stats = {name: _stats(i) for i, name in enumerate(FEATURE_NAMES)}
    queue_mean_idx = FEATURE_NAMES.index("queue_mean")
    queue_nonzero = sum(1 for r in valid_rows if r.features and r.features[queue_mean_idx] != 0.0)

    by_session = defaultdict(list)
    for r in rows:
        by_session[r.session_id].append(r)

    return {
        "num_candidate_sessions": len(sessions),
        "num_sessions_with_any_valid_row": sum(1 for rs in by_session.values() if any(r.valid for r in rs)),
        "sessions_by_regime": {k: len(v) for k, v in
                                defaultdict(list, {s.regime: [x for x in sessions if x.regime == s.regime]
                                                    for s in sessions}).items()},
        "sessions_by_topology": {k: len(v) for k, v in
                                  defaultdict(list, {s.topology: [x for x in sessions if x.topology == s.topology]
                                                      for s in sessions}).items()},
        "windows_per_session": {sid: len(rs) for sid, rs in by_session.items()},
        "total_rows": len(rows),
        "valid_rows": len(valid_rows),
        "invalid_rows": len(invalid_rows),
        "invalid_reasons": [{"session_id": r.session_id, "window_start_utc": r.window_start_utc,
                              "reason": r.invalid_reason} for r in invalid_rows],
        "feature_stats": feature_stats,
        "queue_mean_nonzero_valid_rows": queue_nonzero,
        "queue_mean_nonzero_fraction": (queue_nonzero / len(valid_rows)) if valid_rows else None,
    }


def build_manifest(sessions: list, rows: list, split: dict) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generator": "anomaly-detection/v3/build_dataset.py",
        "git_commit_sha": _git_commit_sha(),
        "feature_names": FEATURE_NAMES,
        "window_sec": WINDOW_SEC,
        "step_sec": STEP_SEC,
        "sessions": [asdict(s) | {"start_utc": s.start_utc.isoformat(), "end_utc": s.end_utc.isoformat()}
                     for s in sessions],
        "split": split,
        "inventory": summarize_inventory(rows, sessions),
        "duplicate_timestamps": find_duplicate_timestamps(rows),
        "duplicate_feature_vectors": [
            {"vector": list(d["vector"]), "first": list(d["first"]), "second": list(d["second"])}
            for d in find_duplicate_feature_vectors(rows)
        ],
    }


def main():
    parser = argparse.ArgumentParser(
        description="v3 정상 데이터 inventory 생성(§ Isolation Forest 감사 후속) - "
                    "official 학습 데이터 확정도, 모델 fit도 하지 않는다. Prometheus read-only 조회만 함.")
    parser.add_argument("--seed", type=int, default=DEFAULT_SPLIT_SEED)
    parser.add_argument("--out", default=str(DATA_DIR / "v3-inventory.json"))
    args = parser.parse_args()

    problems = validate_sessions(ALL_CANDIDATE_SESSIONS)
    if problems:
        print("세션 정의 자체 검증 실패:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        sys.exit(1)

    all_rows = []
    for session in ALL_CANDIDATE_SESSIONS:
        rows = build_rows_for_session(session)
        n_valid = sum(1 for r in rows if r.valid)
        print(f"{session.session_id}({session.regime}/{session.topology}): "
              f"{len(rows)}개 창 중 {n_valid}개 valid")
        all_rows.extend(rows)

    split = split_sessions(ALL_CANDIDATE_SESSIONS, seed=args.seed)
    manifest = build_manifest(ALL_CANDIDATE_SESSIONS, all_rows, split)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out)
    out_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\ninventory 저장: {out_path}")
    print(f"세션 {manifest['inventory']['num_candidate_sessions']}개, "
          f"row {manifest['inventory']['valid_rows']}/{manifest['inventory']['total_rows']} valid")


if __name__ == "__main__":
    main()
