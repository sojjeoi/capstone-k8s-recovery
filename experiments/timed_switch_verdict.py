#!/usr/bin/env python3
"""2026-10-02 - 정시 전환 회복 실험의 "비교 근거 자격" 판정(순수 함수, 클러스터 미접속).

SLO 판정(slo_judge.py v3)은 건드리지 않는다 - 여기서는 그 결과를 A/B 비교에 써도 되는
실행인지만 가린다. verified는 True(확인)/False(위반 확인)/None(데이터 부족으로 판정 불가)
세 값이고, True가 아니면 비교 근거로 쓰지 않는다.

압박 전달은 Chaos CR 기록이 아니라 구 Pod의 실측 CPU(어댑터와 같은 PromQL,
rate[30s])와 생존 표본으로 판정한다. CR 기록은 stage 창의 시각을 정하는 데만 쓴다.
CPU 기준(baseline + 0.2코어, 어댑터 MIN_EFFECTIVE_CPU_RISE_CORES)은 "최소 전달" 확인이지
A/B가 같은 CPU 양을 받았다는 증명이 아니다 - 전환 후 구 Pod는 요청 처리 몫이 빠진다."""
import json
import math
from datetime import datetime, timedelta
from statistics import median
from typing import Optional

CPU_LOOKBACK_SEC = 30.0       # rate[30s] - stage 시작 직후 30초는 이전 상태가 섞여 제외
MIN_SAMPLE_COVERAGE = 0.8     # stage 창에서 기대 표본 수 대비 유효(유한) 표본 비율 하한
MAX_LIVENESS_GAP_FACTOR = 3   # 생존 표본 간격이 sample_interval의 3배를 넘으면 그 구간은 미확인
MAX_CPU_GAP_FACTOR = 2        # CPU 표본(query_range step=interval) 사이·창 양끝 공백 상한 = 표본 1개 누락까지


def _continuity_gap(times: list, lo: datetime, hi: datetime) -> float:
    """[lo, hi]를 표본 시각들이 덮는지 - 창 시작·끝까지의 거리와 내부 최대 간격 중 가장 큰 값(초)."""
    pts = [lo] + sorted(t for t in times if lo <= t <= hi) + [hi]
    return max((b - a).total_seconds() for a, b in zip(pts, pts[1:]))


def _ts(x) -> Optional[datetime]:
    if x is None or isinstance(x, datetime):
        return x
    return datetime.fromisoformat(str(x).replace("Z", "+00:00"))


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def _alive(sample: dict, target_uid: str) -> bool:
    p = sample.get("old_pod") or {}
    return bool(p.get("exists") and p.get("uid") == target_uid and p.get("running") and not p.get("terminating"))


def evaluate_stress_delivery(*, delivery_record: dict, planned_stage_count: int, cpu_samples: list,
                             liveness_samples: list, baseline_cpu: Optional[float], min_rise: float,
                             sample_interval_sec: float, planned_total_sec: float,
                             continuity_from=None) -> dict:
    """continuity_from(B군은 전환 시각): 이 시각부터 계획된 압박 종료까지 CPU 실측이 끊김 없이
    이어져야 한다(단계 창의 30초 lookback 제외 구간도 포함해 모든 유효 표본으로 확인). None이면
    첫 stage 시작부터."""
    reasons, per_stage = [], []
    stages = delivery_record.get("stages") or []
    target = delivery_record.get("target") or {}
    if delivery_record.get("end_reason") != "completed" or len(stages) != planned_stage_count \
            or any(s.get("end") != "completed" for s in stages):
        reasons.append(f"stage_not_completed(end_reason={delivery_record.get('end_reason')}, "
                       f"stages={len(stages)}/{planned_stage_count})")
        return {"verified": False, "reasons": reasons, "per_stage": per_stage}
    if baseline_cpu is None:
        return {"verified": None, "reasons": ["baseline_cpu_missing"], "per_stage": per_stage}

    unverifiable = False
    max_gap = MAX_CPU_GAP_FACTOR * sample_interval_sec
    finite_samples = [(_ts(t), v) for t, v in cpu_samples if _finite(v)]
    for s in stages:
        lo = _ts(s["created_at"]) + timedelta(seconds=CPU_LOOKBACK_SEC)
        hi = _ts(s["deleted_at"])
        in_win = [(t, v) for t, v in finite_samples if lo <= t <= hi]
        expected = max(1, int((hi - lo).total_seconds() // sample_interval_sec))
        coverage = len(in_win) / expected
        gap = _continuity_gap([t for t, _ in in_win], lo, hi)
        med = median(v for _, v in in_win) if in_win else None
        ok = None if (coverage < MIN_SAMPLE_COVERAGE or gap > max_gap) else (med >= baseline_cpu + min_rise)
        per_stage.append({"index": s["index"], "window": [lo.isoformat(), hi.isoformat()],
                          "samples": len(in_win), "expected": expected, "max_gap_sec": gap,
                          "median_cores": med, "ok": ok})
        if ok is None:
            unverifiable = True
            reasons.append(f"stage{s['index']}_cpu_coverage_{coverage:.2f}_gap_{gap:.0f}s")
        elif not ok:
            reasons.append(f"stage{s['index']}_cpu_below_baseline_plus_{min_rise}")

    # 생존: 첫 stage 시작부터 마지막 stage 실제 종료(계획 총 시간 이상)까지 끊김 없이 살아있어야 한다
    t0 = _ts(stages[0]["created_at"])
    t_end = max(_ts(stages[-1]["deleted_at"]), t0 + timedelta(seconds=planned_total_sec))
    # CPU 연속성: continuity_from(B=전환 시각)부터 계획된 압박 종료까지 실측이 끊기지 않아야 한다
    c_from = max(_ts(continuity_from) or t0, t0)
    c_gap = _continuity_gap([t for t, _ in finite_samples], c_from, t_end) if c_from < t_end else None
    if c_gap is None or c_gap > max_gap:
        unverifiable = True
        reasons.append(f"cpu_not_continuous_from_{c_from.isoformat()}_to_stress_end(gap={c_gap})")
    not_alive, live_unverifiable = _liveness(liveness_samples, target.get("uid"), t0, t_end, sample_interval_sec, reasons)
    unverifiable = unverifiable or live_unverifiable

    below = any(s["ok"] is False for s in per_stage)
    verified = False if (below or not_alive) else (None if unverifiable else True)
    return {"verified": verified, "reasons": reasons, "per_stage": per_stage,
            "required_alive_until": t_end.isoformat(), "cpu_continuity_from": c_from.isoformat(),
            "cpu_continuity_max_gap_sec": c_gap}


def _liveness(liveness_samples: list, target_uid, t0, t_end, sample_interval_sec: float, reasons: list):
    """주입 대상 Pod가 [t0, t_end] 동안 끊김 없이 살아있었는지. 반환 (not_alive, unverifiable), 사유는 reasons에 추가.
    old_pod=None은 조회 실패(API 오류) - 소실 증거가 아니므로 빼고, 그 자리는 간격(gap) 검사가 미확인으로 잡는다."""
    live = sorted(((_ts(x["t"]), x) for x in liveness_samples
                   if x.get("old_pod") is not None and t0 <= _ts(x["t"]) <= t_end), key=lambda p: p[0])
    if not live:
        reasons.append("no_liveness_samples")
        return False, True
    not_alive = unverifiable = False
    if any(not _alive(x, target_uid) for _, x in live):
        not_alive = True
        reasons.append("target_not_alive_during_stress")
    points = [t0] + [t for t, _ in live] + [t_end]
    if max((b - a).total_seconds() for a, b in zip(points, points[1:])) > MAX_LIVENESS_GAP_FACTOR * sample_interval_sec:
        unverifiable = True
        reasons.append("liveness_sample_gap")
    return not_alive, unverifiable


# ---- 2026-10-02 압박 전달 기준 v2(원시 카운터) - docs/design/phase8-v2-stress-delivery-criterion-20261002.md ----
# pilot02 A 결과를 보고 수치를 고르지 않았다. 근거: kubelet cAdvisor ServiceMonitor interval=10s, honorTimestamps=true
# (읽기 전용 확인) + 실험 전 6시간 독립 자료(2개 컨테이너, 각 약 1,500 간격)의 원시 표본 간격 10.0~20.0초.
# v2.1(같은 날, 실행 전 코드 리뷰 반영): 단계 창 최소 관측 범위 + 압박 전체 구간 카운터 리셋 판정 불가. v2로 실행한 기록 없음.
# v2.2(같은 날, 실행 전 코드 리뷰 반영): 창 경계를 걸친 원시 간격까지 검사해 단계 창 전체(최소 창−20초)의 압박 확인.
# v2.3(2026-10-04, pair04 B 결과를 본 뒤 발견한 누락 보완 - 계약 v2 §7): 압박 종료와 같거나 뒤의 전환은 연속성 해당 없음.
STRESS_CRITERION_ID = "raw-counter-v2.3-20261004"
CONTINUITY_NOT_APPLICABLE = "not_applicable_switch_at_or_after_stress_end"
CADVISOR_SCRAPE_INTERVAL_SEC = 10.0
RAW_NORMAL_GAP_SEC = 2 * CADVISOR_SCRAPE_INTERVAL_SEC  # 정상: cAdvisor 자체 수집 시각 때문에 생기는 10~20초 간격
RAW_LONG_GAP_SEC = 4 * CADVISOR_SCRAPE_INTERVAL_SEC    # 초과하면 긴 누락(판정 불가). 사이는 일시 누락(판정 가능)
RAW_GAP_TOLERANCE_SEC = 0.5                            # 시각 반올림·지연 흡수


def classify_raw_gap(max_gap_sec: Optional[float]) -> str:
    if max_gap_sec is None:
        return "long_loss"
    if max_gap_sec <= RAW_NORMAL_GAP_SEC + RAW_GAP_TOLERANCE_SEC:
        return "normal"
    if max_gap_sec <= RAW_LONG_GAP_SEC + RAW_GAP_TOLERANCE_SEC:
        return "temporary_loss"
    return "long_loss"


def raw_counter_window(raw: list, lo: datetime, hi: datetime) -> dict:
    """원시 누적 CPU 카운터 표본 [(시각, 초)]으로 [lo, hi] 평균 코어 = Δ카운터/Δ시각. 카운터는 누적값이라
    표본이 몇 개 빠져도 그 사이 사용량은 다음 표본에 그대로 반영된다 - 공백은 '해상도'만 낮춘다."""
    pts = sorted((t, v) for t, v in raw if lo <= t <= hi)
    gap = _continuity_gap([t for t, _ in pts], lo, hi)
    reset = any(b < a for (_, a), (_, b) in zip(pts, pts[1:]))
    span = (pts[-1][0] - pts[0][0]).total_seconds() if len(pts) >= 2 else 0.0
    # v2.1 최소 관측 범위: 정상 수집(간격 ≤ 20초)이면 창 양끝 공백이 각각 20초 미만이라 첫·마지막 표본 사이가
    # 항상 (창 길이 − 2×20초) 이상이다(60초 창 → 20초). 그보다 짧게 덮으면 단계 평균의 근거가 부족하다.
    min_span = max(0.0, (hi - lo).total_seconds() - 2 * RAW_NORMAL_GAP_SEC) - RAW_GAP_TOLERANCE_SEC
    short_span = span < max(min_span, 0.0) or len(pts) < 2
    cores = (pts[-1][1] - pts[0][1]) / span if span > 0 and not reset and not short_span else None
    # v2.2 창 전체 확인: 창 안 표본만으로는 정상 수집에서도 관측이 20초까지 줄 수 있다(창 앞뒤 각 20초 미관측).
    # 창 경계를 걸치는 원시 간격까지 포함해, 창과 자기 길이의 절반 이상 겹치는 간격을 '검사 간격'으로 삼는다.
    # 검사 간격 각각의 평균 코어(minimum)를 판정에 쓰고, 검사 간격이 덮는 창 길이가 창 − 20초 이상이어야 한다.
    # 정상 간격 ≤ 20초(실험 전 6시간 자료 최대 20.0초)면 검사에서 빠지는 창 끝 조각은 각각 10초 미만 → 항상 40초 초과.
    seq = sorted(raw)
    checked, cover = [], 0.0
    for (a, va), (b, vb) in zip(seq, seq[1:]):
        length = (b - a).total_seconds()
        overlap = (min(b, hi) - max(a, lo)).total_seconds()
        if length > 0 and overlap > 0 and overlap >= 0.5 * length:
            checked.append((vb - va) / length)
            cover += overlap
    min_cover = max(0.0, (hi - lo).total_seconds() - RAW_NORMAL_GAP_SEC) - RAW_GAP_TOLERANCE_SEC
    if any(c < 0 for c in checked):
        reset = True
    return {"samples": len(pts), "max_gap_sec": round(gap, 3), "gap_class": classify_raw_gap(gap),
            "counter_reset": reset, "observed_span_sec": round(span, 3), "min_span_sec": round(min_span, 3),
            "short_span": short_span, "cores": cores if not reset else None,
            "checked_cover_sec": round(cover, 3), "min_cover_sec": round(min_cover, 3),
            "insufficient_cover": cover < min_cover,
            "min_interval_cores": min(checked) if checked and not reset else None}


def evaluate_stress_delivery_raw(*, delivery_record: dict, planned_stage_count: int, cpu_raw_samples: list,
                                 liveness_samples: list, baseline_cpu: Optional[float], min_rise: float,
                                 sample_interval_sec: float, planned_total_sec: float,
                                 continuity_from=None) -> dict:
    """기준 v2 - A·B 동일 적용. 단계 창(생성+30초 ~ 삭제)마다 원시 카운터로 평균 코어를 계산한다.
    normal·temporary_loss는 판정(평균 ≥ 기준선+min_rise면 통과, 미만이면 압박 부족=False),
    long_loss·카운터 리셋·표본 2개 미만·관측 범위 부족(v2.1)은 판정 불가(None). 압박 전체 구간(첫 단계 직전 표본~
    압박 종료)의 카운터 리셋도 판정 불가(v2.1). B는 전환 시각부터 압박 종료까지도 같은 공백 기준."""
    reasons, notes, per_stage = [], [], []
    stages = delivery_record.get("stages") or []
    target = delivery_record.get("target") or {}
    if delivery_record.get("end_reason") != "completed" or len(stages) != planned_stage_count \
            or any(s.get("end") != "completed" for s in stages):
        reasons.append(f"stage_not_completed(end_reason={delivery_record.get('end_reason')}, "
                       f"stages={len(stages)}/{planned_stage_count})")
        return {"verified": False, "reasons": reasons, "per_stage": per_stage, "criterion": STRESS_CRITERION_ID}
    if baseline_cpu is None:
        return {"verified": None, "reasons": ["baseline_cpu_missing"], "per_stage": per_stage,
                "criterion": STRESS_CRITERION_ID}

    raw = [(_ts(t), float(v)) for t, v in cpu_raw_samples if _finite(float(v))]
    unverifiable = False
    for s in stages:
        lo = _ts(s["created_at"]) + timedelta(seconds=CPU_LOOKBACK_SEC)
        hi = _ts(s["deleted_at"])
        w = raw_counter_window(raw, lo, hi)
        if w["gap_class"] == "long_loss" or w["cores"] is None or w["insufficient_cover"] \
                or w["min_interval_cores"] is None:
            ok = None
            unverifiable = True
            kind = ("counter_reset" if w["counter_reset"] else
                    "long_loss" if w["gap_class"] == "long_loss" else
                    "short_span" if w["short_span"] else "insufficient_cover")
            reasons.append(f"stage{s['index']}_cpu_{kind}_gap_{w['max_gap_sec']:.0f}s_samples_{w['samples']}"
                           f"_cover_{w['checked_cover_sec']:.0f}s")
        else:
            threshold = baseline_cpu + min_rise
            ok = w["cores"] >= threshold and w["min_interval_cores"] >= threshold  # v2.2: 창 전체 각 간격도
            if not ok:
                reasons.append(f"stage{s['index']}_cpu_below_baseline_plus_{min_rise}"
                               + ("" if w["cores"] < threshold else "_in_part_of_window"))
            if w["gap_class"] == "temporary_loss":
                notes.append(f"stage{s['index']}_temporary_gap_{w['max_gap_sec']:.0f}s")
        per_stage.append({"index": s["index"], "window": [lo.isoformat(), hi.isoformat()], **w, "ok": ok})

    t0 = _ts(stages[0]["created_at"])
    t_end = max(_ts(stages[-1]["deleted_at"]), t0 + timedelta(seconds=planned_total_sec))
    c_from = max(_ts(continuity_from) or t0, t0)
    # v2.1 압박 전체 구간 리셋: 단계 창 사이(30초 lookback 구간)의 리셋도 잡는다. 첫 단계 직전 표본 1개부터 본다.
    seq = sorted(raw)
    before = [p for p in seq if p[0] < t0][-1:]
    span_pts = before + [p for p in seq if t0 <= p[0] <= t_end]
    stress_reset = any(b < a for (_, a), (_, b) in zip(span_pts, span_pts[1:]))
    if stress_reset:
        unverifiable = True
        reasons.append("counter_reset_during_stress")
    # v2.3(2026-10-04, 계약 v2 §7): 전환이 압박 종료와 같거나 뒤면 "전환~압박 종료" 구간이 없으므로 해당 없음.
    # A·전환하지 않은 B(continuity_from=None → t0)는 기존대로. 단계별·리셋·생존 검사는 그대로 적용된다.
    if continuity_from is not None and c_from >= t_end:
        c_gap, c_class = None, CONTINUITY_NOT_APPLICABLE
    else:
        c_gap = _continuity_gap([t for t, _ in raw], c_from, t_end) if c_from < t_end else None
        c_class = classify_raw_gap(c_gap)
    if c_class == "long_loss":
        unverifiable = True
        reasons.append(f"cpu_not_continuous_from_{c_from.isoformat()}_to_stress_end(gap={c_gap})")
    elif c_class == "temporary_loss":
        notes.append(f"continuity_temporary_gap_{c_gap:.0f}s")
    not_alive, live_unverifiable = _liveness(liveness_samples, target.get("uid"), t0, t_end, sample_interval_sec, reasons)
    unverifiable = unverifiable or live_unverifiable

    below = any(s["ok"] is False for s in per_stage)
    verified = False if (below or not_alive) else (None if unverifiable else True)
    return {"verified": verified, "reasons": reasons, "notes": notes, "per_stage": per_stage,
            "criterion": STRESS_CRITERION_ID, "required_alive_until": t_end.isoformat(),
            "cpu_continuity_from": c_from.isoformat(), "cpu_continuity_max_gap_sec": c_gap,
            "cpu_continuity_gap_class": c_class, "counter_reset_during_stress": stress_reset}


def counter_delta(samples: list, pod: Optional[str], start, end, max_edge_gap_sec: float) -> Optional[float]:
    """샘플러가 Pod /metrics에서 직접 읽은 누적 카운터(pod_counters[pod])의 [start, end] 증가량.
    창 양끝 max_edge_gap_sec 안에 표본이 없거나, 값이 줄면(재시작·리셋) 판정 불가(None)."""
    if pod is None:
        return None
    start, end = _ts(start), _ts(end)
    pts = sorted((_ts(s["t"]), (s.get("pod_counters") or {}).get(pod)) for s in samples
                 if start <= _ts(s["t"]) <= end)
    pts = [(t, v) for t, v in pts if _finite(v)]
    if len(pts) < 2 or (pts[0][0] - start).total_seconds() > max_edge_gap_sec \
            or (end - pts[-1][0]).total_seconds() > max_edge_gap_sec:
        return None
    if any(b < a for (_, a), (_, b) in zip(pts, pts[1:])):
        return None
    return pts[-1][1] - pts[0][1]


def identify_new_pod(*, t_switch, created_pod_hash: Optional[str], samples: list, pod_events: Optional[list],
                     target_replacement: Optional[dict] = None, settle_sec: float = 10.0,
                     name_prefix: str = "vllm-serving-") -> dict:
    """계약 v2 §7(2026-10-04) - 압박 도구의 target_replacement에만 기대지 않고, 실행 당시 저장된 증거로 새 Pod의
    이름·UID·IP를 잇는다. 이름 = preview 준비 해시를 가진 표본기 요청 카운터 키(정확히 1개), UID = 수집한 Pod 이벤트의
    그 이름 UID(정확히 1개), IP = 전환+settle 이후 표본에서 active selector가 그 해시이고 endpoint가 IP 하나로만
    바뀐 값(원래 Pod IP·전환 전 endpoint와 다름). 후보 없음·복수·불일치는 판정 불가(verified=None) - 추측하지 않는다."""
    out = {"verified": None, "reasons": [], "name": None, "uid": None, "ip": None}
    t_switch = _ts(t_switch)
    if t_switch is None or not created_pod_hash:
        out["reasons"].append("switch_time_or_created_pod_hash_missing")
        return out
    prefix = f"{name_prefix}{created_pod_hash}-"
    names = sorted({k for s in samples for k in (s.get("pod_counters") or {}) if k.startswith(prefix)})
    if len(names) != 1:
        out["reasons"].append(f"new_pod_name_candidates={names}")
        return out
    name = names[0]
    uids = sorted({(e.get("involved_object") or {}).get("uid") for e in (pod_events or [])
                   if (e.get("involved_object") or {}).get("kind") == "Pod"
                   and (e.get("involved_object") or {}).get("name") == name} - {None})
    if len(uids) != 1:
        out["reasons"].append(f"new_pod_uid_candidates={uids}")
        return out
    rp = (target_replacement or {}).get("replacement_pod") if target_replacement else None
    if target_replacement and (not rp or rp.get("name") != name or rp.get("uid") != uids[0]):
        out["reasons"].append(f"target_replacement_mismatch({(rp or {}).get('name')}/{(rp or {}).get('uid')})")
        return out
    after = [s for s in samples if _ts(s["t"]) >= t_switch + timedelta(seconds=settle_sec)]
    if not after:
        out["reasons"].append("no_samples_after_switch")
        return out
    if any(s.get("active_selector") != created_pod_hash for s in after):
        out["reasons"].append("active_selector_not_created_hash_after_switch")
        return out
    ip_sets = {tuple(sorted(s.get("endpoint_ips") or [])) for s in after}
    if len(ip_sets) != 1 or len(next(iter(ip_sets))) != 1:
        out["reasons"].append(f"endpoint_after_switch_not_single_ip={sorted(ip_sets)}")
        return out
    ip = next(iter(ip_sets))[0]
    before_ips = {i for s in samples if _ts(s["t"]) < t_switch for i in (s.get("endpoint_ips") or [])}
    old_ips = {(s.get("old_pod") or {}).get("ip") for s in samples} - {None}
    if not before_ips:  # 전환 전 endpoint 증거가 없으면 IP가 바뀌었음을 보일 수 없다
        out["reasons"].append("no_endpoint_evidence_before_switch")
        return out
    if ip in before_ips or ip in old_ips:
        out["reasons"].append(f"new_ip_seen_before_switch_or_old_pod({ip})")
        return out
    out.update(verified=True, name=name, uid=uids[0], ip=ip)
    return out


def evaluate_new_pod_traffic(*, t_switch, new_pod_ip: Optional[str], endpoint_samples: list,
                             new_pod_increase: Optional[float], old_pod_increase: Optional[float],
                             settle_sec: float = 10.0, min_new: float = 30.0, max_old: float = 2.0,
                             min_endpoint_samples: int = 3) -> dict:
    """전환 후 vllm-active ready endpoint가 새 Pod IP 하나로만 바뀌었고, 새 Pod의 요청 카운터가
    전환 이후 창에서 늘고(구 Pod는 거의 안 늘고) 있어야 확인. 카운터 지표가 없으면 판정 불가."""
    t_switch = _ts(t_switch)
    if t_switch is None or new_pod_ip is None:
        return {"verified": None, "reasons": ["switch_time_or_new_pod_ip_missing"]}
    after = [x for x in endpoint_samples if _ts(x["t"]) >= t_switch + timedelta(seconds=settle_sec)]
    if len(after) < min_endpoint_samples:
        return {"verified": None, "reasons": [f"endpoint_samples_after_switch={len(after)}"]}
    reasons = []
    if any(sorted(x.get("endpoint_ips") or []) != [new_pod_ip] for x in after):
        reasons.append("active_endpoint_not_only_new_pod")
    if not (_finite(new_pod_increase) and _finite(old_pod_increase)):  # None·NaN·inf는 증거 아님
        return {"verified": False if reasons else None, "reasons": reasons + ["request_counter_unavailable"]}
    if new_pod_increase < min_new:
        reasons.append(f"new_pod_requests_{new_pod_increase}<{min_new}")
    if old_pod_increase > max_old:
        reasons.append(f"old_pod_requests_{old_pod_increase}>{max_old}")
    return {"verified": not reasons, "reasons": reasons,
            "new_pod_increase": new_pod_increase, "old_pod_increase": old_pod_increase}


def evaluate_switch_provenance(*, send: dict, exp_state: Optional[dict], run_id: str,
                               max_send_lag_sec: float = 5.0, max_switch_lag_sec: float = 30.0) -> dict:
    """B의 승격이 "이 실행의 정시 신호"로 일어났는지. 근거는 우리 POST에 대한 정책 서버의
    동기 응답(DecisionRecord: action/outcome/evidence/result.verified_at)과 experiment-run 상태.
    Alertmanager 등 다른 경로의 승격, 실패 후 다른 승격, 늦은 송신은 전부 False."""
    if send.get("status") != "sent" or send.get("http_status") != 200:
        return {"verified": False, "reasons": [f"signal_not_sent_ok({send.get('status')}, {send.get('http_status')})"]}
    try:
        body = json.loads(send.get("response") or "")
    except ValueError:
        return {"verified": None, "reasons": ["signal_response_unparseable"]}
    reasons = []
    ev, res = body.get("evidence") or {}, body.get("result") or {}
    if body.get("action") != "promote_preview" or body.get("outcome") != "executed_verified":
        reasons.append(f"decision_{body.get('action')}_{body.get('outcome')}")
    if ev.get("experiment_run_id") != run_id or ev.get("detector") != "timed_switch":
        reasons.append("decision_not_attributed_to_this_timed_signal")
    planned, attempted, verified_at = _ts(send.get("planned_send_utc")), _ts(send.get("attempted_utc")), _ts(res.get("verified_at"))
    if planned is None or attempted is None or (attempted - planned).total_seconds() > max_send_lag_sec:
        reasons.append("signal_sent_late_or_untimed")
    if verified_at is None or attempted is None or not (
            attempted <= verified_at <= attempted + timedelta(seconds=max_switch_lag_sec)):
        reasons.append("switch_not_verified_shortly_after_signal")
    if (exp_state or {}).get("run_id") != run_id or (exp_state or {}).get("promotion_verified") is not True:
        reasons.append("experiment_state_mismatch")
    return {"verified": not reasons, "reasons": reasons, "t_switch": verified_at.isoformat() if verified_at else None}


def evaluate_detector_switch_provenance(*, audit: dict, exp_state: Optional[dict], run_id: str,
                                       t_injection, signal_results: list, expected_threshold: float,
                                       expected_model_hash: str, max_state_skew_sec: float = 2.0) -> dict:
    """탐지기 전환 A/B(V2) - 이 실행의 승격이 **이 실행의 후보 IF 신호**로 주입 이후 1회 일어났는지.
    `signal_source=anomaly`만으로는 판정하지 않는다(다른 탐지기도 /signal을 쓴다). 근거:
    - audit: recovery-policy `/admin/audit/{run_id}`의 **원본** 결정 기록(evidence.detector·correlation_id·
      evaluation_seq·threshold·model_hash 포함). 공용 _fetch_audit_verified_at은 evidence를 버리므로 쓰지 않는다.
    - signal_results: 이 실행 탐지기 평가 기록의 record_type=signal_result(correlation_id·evaluation_seq·
      http_status·outcome). 승격 근거의 correlation_id·evaluation_seq가 여기서 전송 성공으로 확인돼야 한다.
    승격 관련 기록이 둘 이상이거나(시도 실패 포함), 필드가 빠졌거나, 다른 탐지기·다른 run의 신호면 False.
    승격이 없으면 switched=False(B는 배정 기준 분석이라 유효할 수 있음)."""
    if (audit or {}).get("fetch_error"):
        return {"verified": None, "switched": None, "reasons": [f"audit_fetch_error: {audit['fetch_error']}"]}
    records = (audit or {}).get("records") or []
    promote_related = [r for r in records if r.get("action") == "promote_preview"]
    state_ok = bool(exp_state) and "fetch_error" not in exp_state and exp_state.get("run_id") == run_id
    if not promote_related:
        reasons = [] if state_ok and exp_state.get("promotion_verified") is not True else ["experiment_state_mismatch"]
        return {"verified": not reasons, "switched": False, "reasons": reasons, "t_switch": None,
                "pre_injection_promotion": False}
    reasons = []
    executed = [r for r in promote_related if r.get("outcome") == "executed_verified"]
    if len(promote_related) != 1 or len(executed) != 1:
        reasons.append(f"ambiguous_promotion_records(promote_related={len(promote_related)}, "
                       f"executed_verified={len(executed)})")
    p = (executed or promote_related)[0]
    ev = p.get("evidence") or {}
    if p.get("signal_source") != "anomaly":
        reasons.append(f"promotion_by_{p.get('signal_source')}")  # Alertmanager 등 IF 외 경로
    if ev.get("experiment_run_id") != run_id:
        reasons.append("evidence_run_id_mismatch_or_missing")
    if ev.get("detector") != "isolation_forest":
        reasons.append(f"evidence_detector={ev.get('detector')!r}")
    if not (isinstance(ev.get("threshold"), (int, float)) and abs(ev["threshold"] - expected_threshold) < 1e-9):
        reasons.append(f"evidence_threshold={ev.get('threshold')!r}")
    if ev.get("model_hash") != expected_model_hash:
        reasons.append("evidence_model_hash_mismatch_or_missing")
    cid, seq = ev.get("correlation_id"), ev.get("evaluation_seq")
    if not cid or seq is None:
        reasons.append("evidence_correlation_missing")
    elif not any(s.get("correlation_id") == cid and s.get("evaluation_seq") == seq and s.get("run_id") == run_id
                 and s.get("outcome") == "sent" and s.get("http_status") == 200 for s in signal_results or []):
        reasons.append("no_matching_signal_result_in_this_run")  # 이 실행 탐지기가 보낸 그 신호인지 확인 불가
    t_switch, t_inj = _ts((p.get("result") or {}).get("verified_at")), _ts(t_injection)
    pre = t_switch is not None and t_inj is not None and t_switch < t_inj
    if t_switch is None or t_inj is None:
        reasons.append("switch_or_injection_time_missing")
    elif pre:
        reasons.append("pre_injection_promotion")  # 불필요 실제 조치 1건 - 비교에서 제외
    state_switch = _ts((exp_state or {}).get("t_switch"))
    if not state_ok or exp_state.get("promotion_verified") is not True or state_switch is None or t_switch is None \
            or abs((state_switch - t_switch).total_seconds()) > max_state_skew_sec:
        reasons.append("experiment_state_mismatch")
    return {"verified": not reasons, "switched": True, "reasons": reasons,
            "t_switch": t_switch.isoformat() if t_switch else None, "pre_injection_promotion": pre}


def detector_ab_eligibility(*, arm: str, outcome_status: str, invalidity_reasons, stress: dict,
                            traffic: Optional[dict], provenance: dict, active_selector_changed: Optional[bool],
                            override_verified: Optional[bool], cleanup_errors: Optional[list] = None) -> dict:
    """V2 A/B 비교 자격. B는 배정 기준 - IF가 전환하지 않은 B도 다른 조건을 만족하면 포함(b_status 표시)."""
    reasons = []
    if outcome_status != "completed" or invalidity_reasons:
        reasons.append(f"run_not_valid(status={outcome_status}, invalidity={invalidity_reasons})")
    if cleanup_errors:
        reasons.append(f"cleanup_errors({cleanup_errors})")
    if stress.get("verified") is not True:
        reasons.append(f"stress_delivery_{stress.get('verified')}")
    if provenance.get("verified") is not True:
        reasons.append(f"provenance_{provenance.get('verified')}({provenance.get('reasons')})")
    b_status = None
    if arm == "control":
        if provenance.get("switched") is not False or active_selector_changed is not False:
            reasons.append("control_had_promotion_or_selector_unknown")
    elif arm == "switch":
        if override_verified is not True:
            reasons.append("scale_down_delay_override_not_verified")
        if provenance.get("switched"):
            b_status = "switched"
            if (traffic or {}).get("verified") is not True:
                reasons.append(f"new_pod_traffic_{(traffic or {}).get('verified')}")
        elif provenance.get("switched") is False:
            b_status = "not_switched"
            if active_selector_changed is not False:
                reasons.append("selector_changed_without_if_promotion_or_unknown")
    else:
        reasons.append(f"unknown_arm_{arm}")
    return {"eligible": not reasons, "reasons": reasons, "b_status": b_status}


def comparison_eligibility(*, arm: str, outcome_status: str, invalidity_reasons, stress: dict,
                           traffic: Optional[dict], promotion_verified: Optional[bool],
                           active_selector_changed: Optional[bool], override_verified: Optional[bool],
                           switch_provenance: Optional[dict] = None, cleanup_errors: Optional[list] = None) -> dict:
    reasons = []
    if outcome_status != "completed" or invalidity_reasons:
        reasons.append(f"run_not_valid(status={outcome_status}, invalidity={invalidity_reasons})")
    if cleanup_errors:  # context 정리·지연 설정 원복 실패 - 다음 실행 상태가 오염됐을 수 있음
        reasons.append(f"cleanup_errors({cleanup_errors})")
    if stress.get("verified") is not True:
        reasons.append(f"stress_delivery_{stress.get('verified')}")
    if arm == "control":
        if active_selector_changed is not False or promotion_verified:
            reasons.append("control_had_promotion_or_selector_unknown")  # 주변 신호(Alertmanager 등)에 의한 오염 포함
    elif arm == "switch":
        if (switch_provenance or {}).get("verified") is not True:
            reasons.append(f"timed_switch_provenance_{(switch_provenance or {}).get('verified')}")
        if override_verified is not True:
            reasons.append("scale_down_delay_override_not_verified")
        if (traffic or {}).get("verified") is not True:
            reasons.append(f"new_pod_traffic_{(traffic or {}).get('verified')}")
    else:
        reasons.append(f"unknown_arm_{arm}")
    return {"eligible": not reasons, "reasons": reasons}
