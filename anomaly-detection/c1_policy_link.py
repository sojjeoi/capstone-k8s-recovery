"""c1 탐지 기록 → guarded 정책(v2 신호) 연결 부품 - 동결 c1_shadow_detector.py는 수정하지 않는다(새 파일).

이 모듈은 아직 실시간 탐지 루프에 연결돼 있지 않다(실전 탐지기 변형은 다음 단계). 부품만 제공한다:
- build_v2_payload(start_rec, eval_rec): 같은 실행의 c1_shadow_start 기록 + 송신 조건 평가 기록으로 v2 payload를 만든다.
  누락 정보는 현재 시각이나 기본값으로 채우지 않는다(PayloadError). 재전송 시 원래 시각·seq가 그대로 유지된다.
- check_receiver(base_url, expect): 보내기 전에 수신자를 확인한다.
  guarded  : GET /identity 가 role·version·mode까지 일치해야 한다(version·mode 기대값은 필수 인자).
  capture_sink: /identity 404 + /admin/experiment-run 404 + /healthz 200 - **제한된 호환성 검사이지 신원 증명이 아니다**
             (다른 서버도 404를 낼 수 있다). 기존 recovery-policy(/admin/experiment-run 200)로 잘못 보내는 것은 막는다.
  모든 요청은 리다이렉트를 따르지 않는다.
- send(url, payload): 결과를 delivered / delivered_unparsable / http_error / redirect_refused / unknown_timeout /
  unknown_connection_error / not_delivered / error_unknown 으로 구분한다. HTTP 2xx만 전달 성공이고, 정책 판단(outcome)은
  응답 본문에서 따로 읽는다. not_delivered는 연결 자체가 성립하지 않은 것이 확실할 때만(연결 시간 초과·새 연결 실패·이름
  해석 실패) 쓰고, 그 밖의 연결 오류(응답 중 끊김 등)는 서버가 처리했을 수 있으므로 결과 불명으로 둔다.
"""
import hashlib
import json

import requests

MODEL_VERSION = "v3.3-c1"
GUARDED_ROLE = "guarded-recovery-policy"
SCHEMA_VERSION = "c1-signal-v2"
TIMEOUT = (3.0, 10.0)


class PayloadError(Exception):
    pass


def artifact_manifest_hash(artifact_hashes: dict) -> str:
    return hashlib.sha256(json.dumps(artifact_hashes, sort_keys=True).encode("utf-8")).hexdigest()


def _pod_by_hash(snapshot: dict, h: str) -> dict:
    cand = [p for p in snapshot.get("pods") or [] if p.get("hash") == h]
    if len(cand) != 1:
        raise PayloadError(f"snapshot pod count for hash {h} = {len(cand)}")
    return cand[0]


def build_v2_payload(start_rec: dict, eval_rec: dict, *, model_version: str = MODEL_VERSION) -> dict:
    if start_rec.get("record_type") not in ("c1_shadow_start", "c1_live_start") or eval_rec.get("record_type") != "c1_evaluation":
        raise PayloadError("record_type 불일치")
    if start_rec.get("run_id") != eval_rec.get("run_id") or not eval_rec.get("run_id"):
        raise PayloadError("run_id 불일치·없음")
    if eval_rec.get("status") != "scored" or not eval_rec.get("condition_met") or not eval_rec.get("condition_met_at_utc"):
        raise PayloadError("송신 조건 평가 기록이 아님")
    hashes = start_rec.get("artifact_hashes") or {}
    if not hashes.get("model.pkl") or start_rec.get("threshold") is None or not start_rec.get("started_at_utc"):
        raise PayloadError("시작 기록에 모델 해시·임계값·시작 시각 없음")
    snap = eval_rec.get("pod_snapshot") or {}
    act, pre = snap.get("active_selector"), snap.get("preview_selector")
    if not act or not pre or act == pre:
        raise PayloadError("관측 selector 없음·동일")
    a_pod, p_pod = _pod_by_hash(snap, act), _pod_by_hash(snap, pre)
    expected = eval_rec.get("expected_pods") or {}
    for p in (a_pod, p_pod):
        if expected.get(p.get("name")) != p.get("uid"):
            raise PayloadError(f"기대 Pod UID 불일치: {p.get('name')}")
    for k in ("window_start_utc", "window_end_utc", "evaluation_seq", "score", "consecutive_anomalous"):
        if eval_rec.get(k) is None:
            raise PayloadError(f"평가 기록에 {k} 없음")
    return {
        "schema_version": SCHEMA_VERSION, "signal_type": "anomaly_risk",
        "detector_id": start_rec.get("detector"), "detector_session_id": start_rec["started_at_utc"],
        "model_version": model_version, "model_hash": hashes["model.pkl"],
        "artifact_manifest_hash": artifact_manifest_hash(hashes), "threshold": start_rec["threshold"],
        "experiment_run_id": eval_rec["run_id"], "evaluation_seq": eval_rec["evaluation_seq"],
        "window_start_utc": eval_rec["window_start_utc"], "window_end_utc": eval_rec["window_end_utc"],
        "decided_at_utc": eval_rec["condition_met_at_utc"], "score": eval_rec["score"],
        "consecutive_count": eval_rec["consecutive_anomalous"],
        "observed": {"active_hash": act, "preview_hash": pre, "active_pod_uid": a_pod["uid"], "preview_pod_uid": p_pod["uid"],
                     "active_restart_count": a_pod.get("restart_count"), "preview_restart_count": p_pod.get("restart_count")},
    }


def _get(get, url):
    return get(url, timeout=TIMEOUT, allow_redirects=False)


def check_receiver(base_url: str, expect: str, *, expected_version: str = None, expected_mode: str = None,
                   get=requests.get) -> dict:
    base = base_url.rstrip("/")
    try:
        if expect == "guarded":
            if not expected_version or not expected_mode:
                return {"ok": False, "reason": "expected_version_and_mode_required"}
            r = _get(get, f"{base}/identity")
            if 300 <= r.status_code < 400:
                return {"ok": False, "reason": "redirect_refused"}
            if r.status_code != 200:
                return {"ok": False, "reason": f"identity_http_{r.status_code}"}
            ident = r.json()
            if ident.get("role") != GUARDED_ROLE:
                return {"ok": False, "reason": f"role_mismatch: {ident.get('role')}"}
            if ident.get("version") != expected_version:
                return {"ok": False, "reason": f"version_mismatch: {ident.get('version')}"}
            if ident.get("mode") != expected_mode:
                return {"ok": False, "reason": f"mode_mismatch: {ident.get('mode')}"}
            return {"ok": True, "identity": ident}
        if expect == "capture_sink":
            codes = {p: _get(get, f"{base}{p}").status_code for p in ("/identity", "/admin/experiment-run", "/healthz")}
            if any(300 <= c < 400 for c in codes.values()):
                return {"ok": False, "reason": "redirect_refused", "codes": codes}
            ok = codes["/identity"] == 404 and codes["/admin/experiment-run"] == 404 and codes["/healthz"] == 200
            return {"ok": ok, "codes": codes, "reason": None if ok else "not_capture_sink_compatible",
                    "note": "제한된 호환성 검사(신원 증명 아님)"}
        return {"ok": False, "reason": f"unknown_expect: {expect}"}
    except (requests.RequestException, ValueError) as e:
        return {"ok": False, "reason": f"check_error: {type(e).__name__}"}


PASS_THROUGH = ("policy_passed", "promotion", "service_ok", "service_check", "blocked", "state_error", "audit_error",
                "ledger_error", "action_status", "original_outcome", "target_key", "reasons", "duplicate")


def send(url: str, payload: dict, *, post=requests.post, timeout: tuple = TIMEOUT) -> dict:
    """timeout=(연결, 읽기). 실전 탐지기는 동기 승격 처리 상한(재조회+CLI+검증+저장)보다 긴 읽기 시간을 넘긴다."""
    try:
        r = post(url, json=payload, timeout=timeout, allow_redirects=False)
    except requests.exceptions.ConnectTimeout as e:  # 연결 성립 전 시간 초과 - 요청이 서버에 닿지 않음
        return {"delivery": "not_delivered", "error": f"{type(e).__name__}: {e}"}
    except requests.exceptions.Timeout as e:
        return {"delivery": "unknown_timeout", "error": f"{type(e).__name__}: {e}"}
    except requests.exceptions.ConnectionError as e:
        text = repr(e)
        if any(m in text for m in ("NewConnectionError", "Failed to establish a new connection", "NameResolutionError")):
            return {"delivery": "not_delivered", "error": f"{type(e).__name__}: {e}"}
        return {"delivery": "unknown_connection_error", "error": f"{type(e).__name__}: {e}"}
    except requests.RequestException as e:
        return {"delivery": "error_unknown", "error": f"{type(e).__name__}: {e}"}
    if 300 <= r.status_code < 400:
        return {"delivery": "redirect_refused", "http_status": r.status_code}
    if not 200 <= r.status_code < 300:
        return {"delivery": "http_error", "http_status": r.status_code, "body": r.text[:300]}
    try:
        body = r.json()
        outcome = body["outcome"]
    except (ValueError, KeyError, TypeError):
        return {"delivery": "delivered_unparsable", "http_status": r.status_code, "body": r.text[:300]}
    return {"delivery": "delivered", "http_status": r.status_code, "policy_outcome": outcome,
            **{k: body.get(k) for k in PASS_THROUGH if k in body}}
