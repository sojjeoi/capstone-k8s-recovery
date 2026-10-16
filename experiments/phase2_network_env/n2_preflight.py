#!/usr/bin/env python3
"""네트워크 지연 세션의 사전 점검(2026-10-17, DEVELOPMENT - 동결 전, 클러스터 접근 없음 - 읽기 전용 값을 받아 판정만 한다).

Phase 2 run_pressure가 이미 하는 점검(정책·세션·canonical·유예 시간·기존 정책 격리)은 그대로 쓰고, 여기서는 네트워크 환경에만 필요한
점검을 더한다. 하나라도 걸리면 세션을 시작하지 않는다(클러스터 변경 전에 멈춘다):
  1. PROFILE_STALE: profile.environment(서빙 계약·계약 해시·probe 설정·압박 정의)가 지금 관측과 다르지 않은가.
  2. network_tolerant: Rollout probe 제한이 11 s인가(기본값이면 지연이 1초를 넘을 때 Pod가 재시작된다).
  3. NetworkChaos가 하나도 없는가.
  4. active Pod가 정확히 1개이고 Ready인가, 대상(active)과 보호 Pod(preview·새 Pod)가 구분되는가.
  5. net_probe 지표가 지금 읽히는가(active Pod 시계열 정확히 1개, 유한) 그리고 정상 범위인가(이미 임계값 위면 정상 전제가 깨진 것).
  6. 정책: 지정 모드(Stage 1 record_only), context·blocked·unresolved 없음."""
import sys
sys.dont_write_bytecode = True
import math  # noqa: E402

import n2_common as nc  # noqa: E402

pc = nc.pc


def net_probe_reading(body, *, threshold: float = nc.NET_THRESHOLD) -> dict:
    """Prometheus 즉시 질의 응답 본문 → {"value", "n_series", "problems"}. 값은 active Pod 시계열이 정확히 1개이고 유한할 때만."""
    out = {"value": None, "n_series": None, "problems": []}
    try:
        res = body["data"]["result"]
        out["n_series"] = len(res)
        if len(res) != 1:
            out["problems"].append(f"net_probe_series_count={len(res)}")
            return out
        v = float(res[0]["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        out["problems"].append("net_probe_response_unparsable")
        return out
    if not math.isfinite(v):
        out["problems"].append("net_probe_non_finite")
        return out
    out["value"] = v
    if v > threshold:
        out["problems"].append(f"net_probe_already_anomalous:{v:.4f}>{threshold}")
    return out


def pod_problems(pods: list, active_hash: str, preview_hash: str) -> list:
    """pods = [{"name","uid","hash","ready","terminating"}]. active(대상) 정확히 1개·Ready, preview(보호) 1개가 다른 hash·Ready."""
    probs = []
    act = [p for p in pods if p.get("hash") == active_hash and not p.get("terminating")]
    prev = [p for p in pods if p.get("hash") == preview_hash and not p.get("terminating")]
    if len(act) != 1:
        probs.append(f"active_pod_count={len(act)}")
    elif not act[0].get("ready"):
        probs.append("active_pod_not_ready")
    if active_hash == preview_hash:
        probs.append("active_equals_preview(보호할 새 Pod 없음)")
    elif len(prev) != 1:
        probs.append(f"preview_pod_count={len(prev)}")
    elif not prev[0].get("ready"):
        probs.append("preview_pod_not_ready")
    return probs


def protected_pods(pods: list, active_hash: str) -> list:
    """대상(active) 외의 살아 있는 Pod 이름 = 지연이 닿으면 안 되는 보호 Pod."""
    return sorted(p["name"] for p in pods if p.get("hash") != active_hash and not p.get("terminating"))


def preflight_problems(*, profile: dict, observed_env: dict, policy_sha256: str, template: dict, chaos_names, pods: list,
                       active_hash: str, preview_hash: str, net_probe_body, policy: dict, policy_mode: str = "record_only") -> dict:
    """모든 점검을 모아 {"ok", "problems", "net_probe", "target", "protected"} 반환. 읽기 전용 값만 받는다."""
    probs = []
    stale = pc.check_stale(profile, observed_env, policy_sha256)
    if stale:
        probs.append(f"PROFILE_STALE:{stale}")
    probs += nc.probe_profile_problems(template, expect_tolerant=True)
    if chaos_names:
        probs.append(f"network_chaos_present:{sorted(chaos_names)}")
    probs += pod_problems(pods, active_hash, preview_hash)
    reading = net_probe_reading(net_probe_body)
    probs += reading["problems"]
    p = policy or {}
    if p.get("mode") != policy_mode:
        probs.append(f"policy_mode={p.get('mode')} != {policy_mode}")
    if p.get("context"):
        probs.append("policy_context_set")
    if p.get("blocked"):
        probs.append("policy_blocked")
    if p.get("unresolved"):
        probs.append(f"policy_unresolved:{p.get('unresolved')}")
    target = [x["name"] for x in pods if x.get("hash") == active_hash and not x.get("terminating")]
    return {"ok": not probs, "problems": probs, "net_probe": {k: reading[k] for k in ("value", "n_series")},
            "target": target[0] if len(target) == 1 else None, "protected": protected_pods(pods, active_hash)}
