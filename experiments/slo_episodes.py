#!/usr/bin/env python3
"""V2 계약 v2-main-contract-20261003 - 관찰 구간의 모든 SLO 위반 구간과 누적 위반시간(오프라인 분석 전용).

정의: docs/design/phase8-v2-main-experiment-contract-v2-20261003.md §3. slo_judge v3(evaluate·find_t_slo·
find_t_recovery)를 수정하지 않고 그대로 순서대로 호출한다 - 위반 확정·30초 회복 규칙은 기존과 같다.
첫 구간은 기존 첫 위반·첫 회복과 같고, 다음 구간은 직전 회복 시각 이후에서 새로 확정되어야 한다."""
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import slo_judge

MIN_POST_INJECTION_REQUESTS = 570  # 계획 §3.2 4번(600초×1RPS의 95%)
MAX_GAP_SEC = 30.0                 # 계획 §3.2 4번 내부 공백 기준(관찰 구간 양 끝 포함)
CONTRACT_ID = "v2-main-contract-20261003"


ANALYZER_REVISION = "r2-20261004"  # r1→r2: 관찰 종료 뒤 증거로 위반·회복을 확정하던 오류 수정(docs/design/phase8-v2-slo-episodes-fix-20261004.md)


def _iso(t):
    return t.isoformat() if t is not None else None


def _recovery_with_confirmation(points: list, t_slo):
    """slo_judge.find_t_recovery와 같은 반복(수정 없이 그대로 재현)으로 (정상 구간 시작 시각, 회복을 확인한 시각)을
    돌려준다. 시작 시각 = find_t_recovery 반환값(정상 스트릭 첫 표본의 observed_at), 확인 시각 = 30초 지속을
    채운 표본의 observed_at. 호출부에서 시작 시각이 find_t_recovery와 같은지 매번 대조한다."""
    if t_slo is None:
        return None, None
    streak_start, start_point = None, None
    for p in (p for p in points if p["observed_at"] >= t_slo):
        if p["latency_evaluable"] and not p["latency_violating"] and not p["availability_violating"]:
            if streak_start is None:
                streak_start, start_point = p["t"], p
            if (p["t"] - streak_start).total_seconds() >= slo_judge.LATENCY_PERSIST_SEC:
                return start_point["observed_at"], p["observed_at"]
        else:
            streak_start, start_point = None, None
    return None, None


def violation_episodes(rows: list, t_injection: Optional[datetime], observation_end: Optional[datetime],
                       min_requests: int = MIN_POST_INJECTION_REQUESTS, max_gap_sec: float = MAX_GAP_SEC) -> dict:
    """rows: slo_judge.load_raw() 형식. 반환: computable, reason, episodes[{t_slo, t_recovery, duration_sec,
    lower_bound}], cumulative_sec(주 지표), first_episode_sec(보조 지표), 각 하한 여부."""
    out = {"contract": CONTRACT_ID, "analyzer_revision": ANALYZER_REVISION, "slo_version": slo_judge.SLO_VERSION,
           "computable": False, "reason": None, "evidence_rule": "response_observed_at<=observation_end",
           "t_injection": _iso(t_injection), "observation_end": _iso(observation_end),
           "requests_in_window": None, "max_gap_in_window_sec": None, "episodes": [],
           "cumulative_sec": None, "cumulative_lower_bound": None,
           "first_episode_sec": None, "first_episode_lower_bound": None}
    if t_injection is None or observation_end is None or observation_end <= t_injection:
        out["reason"] = "injection_or_observation_end_missing"
        return out
    win = [r["sent_at"] for r in rows if t_injection <= r["sent_at"] <= observation_end]
    out["requests_in_window"] = len(win)
    if len(win) < min_requests:
        out["reason"] = f"requests_in_window<{min_requests}"
        return out
    edges = [t_injection] + win + [observation_end]
    gap = max((b - a).total_seconds() for a, b in zip(edges, edges[1:]))
    out["max_gap_in_window_sec"] = round(gap, 3)
    if gap > max_gap_sec:
        out["reason"] = f"gap_in_window>{max_gap_sec}s"
        return out

    # r2: 관찰 종료 시각까지 응답이 확인된 요청만 증거로 쓴다(observed_at = sent_at + latency ≤ 관찰 종료).
    # 종료 전에 보냈지만 종료 뒤에 응답한 요청은 종료 시점에 결과를 알 수 없으므로 판정에 넣지 않는다.
    known = [r for r in rows if r["sent_at"] + timedelta(seconds=r["latency"]) <= observation_end]
    out["requests_excluded_response_after_end"] = sum(
        1 for r in rows if r["sent_at"] <= observation_end < r["sent_at"] + timedelta(seconds=r["latency"]))
    points = slo_judge.evaluate(known)
    episodes, not_before = [], t_injection
    while True:
        t_slo = slo_judge.find_t_slo(points, not_before=not_before)  # 위반을 확정한 표본의 observed_at
        if t_slo is None or t_slo > observation_end:
            break
        if episodes and t_slo < datetime.fromisoformat(episodes[-1]["t_recovery"]):
            out["reason"] = "episode_overlap"  # 방어 확인 - 정의상 일어나면 안 됨
            return out
        t_rec = slo_judge.find_t_recovery(points, t_slo)  # 정상 구간 시작 시각
        t_rec_start, t_confirmed = _recovery_with_confirmation(points, t_slo)
        if t_rec_start != t_rec:
            out["reason"] = "recovery_reimplementation_mismatch"  # 재현 루프가 slo_judge와 어긋나면 값을 내지 않음
            return out
        lower = t_confirmed is None or t_confirmed > observation_end  # 회복은 '확인한 시각'으로 종료 전후를 가른다
        end = observation_end if lower else t_rec
        episodes.append({"t_slo": _iso(t_slo), "t_recovery": _iso(t_rec) if not lower else None,
                         "t_recovery_confirmed_at": _iso(t_confirmed) if not lower else None,
                         "end_counted": _iso(end),
                         "duration_sec": round((end - t_slo).total_seconds(), 3), "lower_bound": lower,
                         "t_slo_rel_sec": round((t_slo - t_injection).total_seconds(), 1),
                         "end_rel_sec": round((end - t_injection).total_seconds(), 1)})
        if lower:
            break
        not_before = t_rec

    out.update(computable=True, episodes=episodes,
               cumulative_sec=round(sum(e["duration_sec"] for e in episodes), 3),
               cumulative_lower_bound=any(e["lower_bound"] for e in episodes),
               first_episode_sec=episodes[0]["duration_sec"] if episodes else 0.0,
               first_episode_lower_bound=episodes[0]["lower_bound"] if episodes else False)
    return out


def analyze_summary(summary_path: Path) -> dict:
    """실행 요약(JSON)과 그 probe CSV로 분석. 요약·CSV는 읽기만 한다."""
    s = json.loads(Path(summary_path).read_text(encoding="utf-8"))
    base = {"run_id": s.get("run_id"), "arm": s.get("arm"), "pair": s.get("pair"),
            "comparison": s.get("comparison"), "probe_raw_csv": s.get("probe_raw_csv")}
    t_inj, t_end = s.get("t_injection_observed_utc"), s.get("observation_ended_at_utc")
    try:
        rows = slo_judge.load_raw(Path(s["probe_raw_csv"]))
    except Exception as e:  # noqa: BLE001 - CSV 없음·파싱 실패 = 판정 불가
        return {**base, "computable": False, "reason": f"probe_csv_unreadable: {type(e).__name__}: {e}"}
    return {**base, **violation_episodes(rows, datetime.fromisoformat(t_inj) if t_inj else None,
                                         datetime.fromisoformat(t_end) if t_end else None)}


def main():
    parser = argparse.ArgumentParser(description="V2 실행 요약들의 SLO 위반 구간·누적 위반시간을 새 파일에 저장")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("summaries", nargs="+", type=Path)
    args = parser.parse_args()
    if args.out.exists():
        raise SystemExit(f"이미 있음 - 덮어쓰지 않음: {args.out}")
    result = {"contract": CONTRACT_ID, "analyzer_revision": ANALYZER_REVISION, "generated_at": datetime.now().astimezone().isoformat(),
              "runs": [analyze_summary(p) for p in args.summaries]}
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
