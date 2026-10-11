#!/usr/bin/env python3
"""v3.3-c1 기록 전용(섀도) 실시간 탐지기 - 개발 pilot용(2026-10-05). 운영 score_server.py·기존 artifacts는 그대로 둔다.

평가 1회(EVAL_INTERVAL_SEC마다, score_server와 같은 60초 창·3연속·쿨다운 60초):
  1) 기대 Pod - 이 평가 시점의 Kubernetes API 조회만 쓴다(실행 뒤 Pod 이벤트·미래 기록 미사용):
     Rollout active·preview selector, vllm-serving Pod 목록(이름·UID·해시·종료 여부·vllm 컨테이너 시작 시각).
     active·preview 해시 Pod가 각각 정확히 1개, 종료 중 아님, 컨테이너가 평가 창 시작 전에 시작, 그 밖의 vllm Pod 없음
     → 기대 목록 {이름: UID}. 하나라도 어긋나면 건너뜀(사유 기록).
  2) 원문 조회 - CPU rate[60s](input_integrity.cpu_rate_query), memory·queue·cache는 features.METRICS 그대로.
  3) 특징 - input_integrity.window_features(네 지표·기대 Pod·UID·전체 격자, 동결 순서·시간축 기울기).
  4) 신선도 - 네 지표 timestamp() 응답을 input_integrity.check_freshness_by_pod로 Pod별 판정(c1 빌더와 같은 방식).
  5) 점수 - v3.3-c1 모델·scaler·스키마·임계값(SHA256SUMS 확인) → score_server.advance_streak.
  건너뛴 평가는 실제 탐지기의 데이터 공백 처리대로 연속을 0으로 리셋한다. 판단 결과는 로컬 evidence(JSONL, 줄마다 fsync)와
  선택적 capture sink(루프백·8080 아닌 포트만)로만 보낸다 - 실제 recovery-policy로는 구조적으로 보내지 않는다.
  시각: 평가 창(window_*), 자료 조회 완료(query_completed_at), 점수 계산 완료(score_completed_at), 경고 조건 충족
  (condition_met_at = 그 판단의 점수 계산 완료 시각)을 따로 남긴다."""
import argparse
import hashlib
import json
import math
import pickle
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "v3" / "model_v31"))
import requests  # noqa: E402

import features  # noqa: E402
import input_integrity as ii  # noqa: E402
from feature_selection import FEATURE_NAMES, apply_feature_schema  # noqa: E402
from score_server import (COOLDOWN_SEC, CONSECUTIVE_THRESHOLD, EVAL_INTERVAL_SEC, FRESHNESS_PROBE_QUERIES,  # noqa: E402
                          WINDOW_SEC, _write_evidence_line, advance_streak)

DETECTOR_ID = "c1_shadow_v3.3"
DEFAULT_ARTIFACTS = HERE / "v3" / "model_v33_c1" / "artifacts"
EXPECTED_THRESHOLD = -0.07667538512242333
NAMESPACE, ROLLOUT_NAME, POD_PREFIX = "vllm-serving", "vllm-serving", "vllm-serving-"
FORBIDDEN_SINK_PORTS = {8080}  # recovery-policy 포트포워드(REAL_RECOVERY_POLICY_SIGNAL_URL)
K8S_REQUEST_TIMEOUT = (3.0, 5.0)  # Kubernetes API (연결, 응답) 제한 시간(초) - 초과는 이 평가 건너뜀(pod_snapshot_failed)


class EvidenceWriteFailed(RuntimeError):
    """필수 evidence(시작·평가·sink 결과) 저장 실패 - 섀도 프로세스를 끝내 실행기의 생존 감시가 중단·정리하게 한다.
    (동결 score_server의 기록 실패 처리 방식은 바꾸지 않는다 - 이 모듈만 해당.)"""


def _must_write(fh, rec):
    if not _write_evidence_line(fh, rec):
        raise EvidenceWriteFailed(f"필수 evidence 저장 실패: record_type={rec.get('record_type')} seq={rec.get('evaluation_seq')}")


def _iso(t):
    return t.isoformat() if t else None


def load_artifacts(art: Path) -> dict:
    sums = json.loads((art / "SHA256SUMS.json").read_text(encoding="utf-8"))
    bad = [k for k, v in sums.items() if hashlib.sha256((art / k).read_bytes()).hexdigest() != v]
    thr = json.loads((art / "threshold.json").read_text(encoding="utf-8"))
    if bad or thr["threshold"] != EXPECTED_THRESHOLD or thr.get("calibration_failed"):
        raise RuntimeError(f"fail-closed: v3.3-c1 artifacts 확인 실패 bad={bad} threshold={thr.get('threshold')}")
    return {"model": pickle.loads((art / "model.pkl").read_bytes()), "scaler": pickle.loads((art / "scaler.pkl").read_bytes()),
            "schema": json.loads((art / "feature-schema.json").read_text(encoding="utf-8")), "threshold": thr["threshold"],
            "hashes": sums}


def check_sink_url(url):
    """capture sink만 허용 - 루프백 주소, recovery-policy 포트(8080) 금지."""
    if url is None:
        return None
    u = urlparse(url)
    if u.scheme != "http" or u.hostname not in ("127.0.0.1", "localhost") or (u.port or 80) in FORBIDDEN_SINK_PORTS:
        raise ValueError(f"sink URL 거부(루프백 capture sink만, 포트 8080 금지): {url}")
    return url


def resolve_expected(snapshot: dict, window_start: datetime) -> dict:
    """snapshot: {"active_selector", "preview_selector", "pods": [{name, uid, hash, terminating, phase,
    vllm_started_at(ISO|None)}]} - 평가 시점 조회 결과만. 반환 {"pods": {name: uid}|None, "reason"}."""
    act, pre = snapshot.get("active_selector"), snapshot.get("preview_selector")
    if not act or not pre or act == pre:
        return {"pods": None, "reason": f"selector_unknown(active={act}, preview={pre})"}
    pods = [p for p in snapshot.get("pods") or [] if p["name"].startswith(POD_PREFIX)]
    others = sorted(p["name"] for p in pods if p.get("hash") not in (act, pre))
    if others:
        return {"pods": None, "reason": f"unexpected_vllm_pods={others}"}
    out = {}
    for role, h in (("active", act), ("preview", pre)):
        cand = [p for p in pods if p.get("hash") == h]
        if len(cand) != 1:
            return {"pods": None, "reason": f"{role}_pod_count={len(cand)}"}
        p = cand[0]
        if p.get("terminating") or p.get("phase") != "Running" or not p.get("uid"):
            return {"pods": None, "reason": f"{role}_pod_not_running_or_terminating"}
        started = p.get("vllm_started_at")
        if not started or datetime.fromisoformat(started.replace("Z", "+00:00")) > window_start:
            return {"pods": None, "reason": f"{role}_container_started_after_window_start"}
        out[p["name"]] = p["uid"]
    return {"pods": out, "reason": None}


def k8s_snapshot() -> dict:  # 실제 클러스터 조회(읽기 전용) - 테스트는 client를 가짜로 바꿔 제한 시간 전달만 확인
    from kubernetes import client, config
    try:
        config.load_kube_config()
    except Exception:  # noqa: BLE001
        config.load_incluster_config()
    observed = datetime.now(timezone.utc)
    ro = client.CustomObjectsApi().get_namespaced_custom_object("argoproj.io", "v1alpha1", NAMESPACE, "rollouts", ROLLOUT_NAME,
                                                                _request_timeout=K8S_REQUEST_TIMEOUT)
    bg = (ro.get("status") or {}).get("blueGreen") or {}
    pods = []
    for p in client.CoreV1Api().list_namespaced_pod(NAMESPACE, _request_timeout=K8S_REQUEST_TIMEOUT).items:
        cs = next((c for c in (p.status.container_statuses or []) if c.name == "vllm"), None)
        run_state = cs.state.running if cs and cs.state else None
        pods.append({"name": p.metadata.name, "uid": p.metadata.uid,
                     "hash": (p.metadata.labels or {}).get("rollouts-pod-template-hash"),
                     "terminating": p.metadata.deletion_timestamp is not None, "phase": p.status.phase,
                     "vllm_started_at": _iso(run_state.started_at) if run_state and run_state.started_at else None,
                     "restart_count": cs.restart_count if cs else None, "node": p.spec.node_name})
    return {"observed_at_utc": observed.isoformat(), "active_selector": bg.get("activeSelector"),
            "preview_selector": bg.get("previewSelector"), "pods": pods}


def first_scored_evaluation(evidence_path) -> dict:
    """pilot 준비 판정(2026-10-05): 섀도 evidence의 완결된 줄(줄바꿈으로 끝남)만 읽어, c1 입력 검사를 통과해 유한한 점수를
    기록한 c1_evaluation(status=scored)이 하나라도 있으면 ready. 점수의 정상/이상은 보지 않는다. 완결된 줄의 JSON 파싱
    실패는 error(기록 오류). 동결 탐지기 기록 형식(first_complete_evaluation)과 다른 섀도 형식 전용."""
    detail = {"evaluations_seen": 0, "skipped_seen": 0, "last_skip_reason": None, "ready_seq": None}
    p = Path(evidence_path)
    if not p.exists():
        return {"ready": False, "detail": detail}
    for i, line in enumerate(p.read_text(encoding="utf-8").split("\n")[:-1], 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            return {"ready": False, "error": f"섀도 evidence {i}번째 줄 JSON 파싱 실패", "detail": detail}
        if r.get("record_type") != "c1_evaluation":
            continue
        detail["evaluations_seen"] += 1
        sc = r.get("score")
        if r.get("status") == "scored" and isinstance(sc, (int, float)) and math.isfinite(sc):
            detail["ready_seq"] = r.get("evaluation_seq")
            return {"ready": True, "detail": detail}
        detail["skipped_seen"] += 1
        detail["last_skip_reason"] = r.get("skip_reason")
    return {"ready": False, "detail": detail}


def prom_range(promql, start, end):
    """원문 시계열 목록(합산 전). 실패는 예외 - 호출부가 건너뜀으로 기록."""
    raw = []
    features._query_range_with_timestamps(promql, start, end, "15s", raw_out=raw)
    return raw


def prom_instant(promql):
    r = requests.get(f"{features.PROM_URL}/api/v1/query", params={"query": promql}, timeout=5.0)
    r.raise_for_status()
    return r.json()


class _Stored:  # 저장한 응답 본문을 check_freshness_by_pod 응답 검증에 그대로 통과(c1 빌더와 같은 방식)
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


def evaluate_once(art: dict, *, now_fn, snapshot_fn, range_fn=prom_range, instant_fn=prom_instant) -> dict:
    """한 번의 판단(연속 처리는 호출부). 반환 record: status ∈ {"scored", "skipped"}."""
    end = now_fn()
    start = end - timedelta(seconds=WINDOW_SEC)
    rec = {"eval_started_at_utc": end.isoformat(), "window_start_utc": start.isoformat(), "window_end_utc": end.isoformat()}

    def skip(reason):
        rec.update(status="skipped", skip_reason=reason, skipped_at_utc=now_fn().isoformat())
        return rec
    try:
        snap = snapshot_fn()
    except Exception as e:  # noqa: BLE001
        return skip(f"pod_snapshot_failed:{type(e).__name__}: {e}")
    rec["pod_snapshot"] = snap
    exp = resolve_expected(snap, start)
    rec["expected_pods"], rec["expected_reason"] = exp["pods"], exp["reason"]
    if exp["pods"] is None:
        return skip(f"expected_unverified:{exp['reason']}")
    queries, raw = {}, {}
    for m, promql in (("cpu", ii.cpu_rate_query()), *((k, features.METRICS[k]) for k in ("memory", "queue", "cache"))):
        sent = now_fn()
        try:
            raw[m] = range_fn(promql, start, end)
            err = None
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
        queries[m] = {"promql": promql, "sent_at_utc": sent.isoformat(), "received_at_utc": now_fn().isoformat(),
                      "series": raw.get(m), "error": err}
        if err:
            rec["queries"] = queries
            return skip(f"query_failed:{m}")
    rec["queries"] = queries
    rec["query_completed_at_utc"] = now_fn().isoformat()
    w = ii.window_features(raw, exp["pods"], start, end, runtime_uid_evidence=exp["pods"])
    rec["completeness"] = {"status": w["status"], "reason": w["reason"], "per_metric": w["per_metric"]}
    if w["features"] is None:
        return skip(f"incomplete:{w['reason']}")
    fresh, bad = {}, []
    for m, selector in FRESHNESS_PROBE_QUERIES.items():
        sent = now_fn()
        try:
            body, err = instant_fn(ii.timestamp_query(selector)), None
        except Exception as e:  # noqa: BLE001
            body, err = None, f"{type(e).__name__}: {e}"
        received = now_fn()
        if err:
            fresh[m] = {"sent_at_utc": sent.isoformat(), "received_at_utc": received.isoformat(), "error": err}
            bad.append(f"{m}:query_failed")
            continue
        f = ii.check_freshness_by_pod("stored", exp["pods"], runtime_uid_evidence=exp["pods"],
                                      get_fn=lambda *a, b=body, **k: _Stored(b), now_fn=lambda r=received: r.timestamp())
        fresh[m] = {"sent_at_utc": sent.isoformat(), "received_at_utc": received.isoformat(), "body": body,
                    "fresh": f["fresh"], "reason_kind": f["reason_kind"], "per_pod": f["per_pod"]}
        if not f["fresh"]:
            bad.append(f"{m}:{f['reason_kind']}")
    rec["freshness"] = fresh
    if bad:
        return skip(f"not_fresh:{bad}")
    x = apply_feature_schema(w["features"], art["schema"])
    xs = art["scaler"].transform([x])
    score = float(art["model"].decision_function(xs)[0])
    if math.isnan(score) or math.isinf(score):
        return skip("score_not_finite")
    rec.update(status="scored", features=dict(zip(FEATURE_NAMES, w["features"])), ordered_feature_vector=x,
               scaled_feature_vector=[float(v) for v in xs[0]], score=score, score_completed_at_utc=now_fn().isoformat())
    return rec


def step_streak(rec: dict, state: dict, threshold: float, monotonic_now: float) -> dict:
    """연속 처리 - 건너뜀은 0으로 리셋(score_server main의 데이터 공백 처리와 같음)."""
    if rec["status"] != "scored":
        state["consecutive"] = 0
        rec.update(consecutive_anomalous=0, condition_met=False, condition_met_at_utc=None)
        return rec
    st = advance_streak(rec["score"], threshold, state["consecutive"], state["last_signal"], monotonic_now,
                        cooldown_sec=COOLDOWN_SEC, consecutive_threshold=CONSECUTIVE_THRESHOLD)
    state["consecutive"], state["last_signal"] = st["consecutive_anomalous"], st["last_signal_at"]
    rec.update(is_anomalous=st["is_anomalous"], consecutive_anomalous=st["consecutive_anomalous"],
               condition_met=st["should_signal"],
               condition_met_at_utc=rec["score_completed_at_utc"] if st["should_signal"] else None)
    return rec


def post_sink(url, run_id, rec):
    payload = {"signal_type": "anomaly_risk", "detector": DETECTOR_ID, "experiment_run_id": run_id,
               "score": rec["score"], "evaluation_seq": rec["evaluation_seq"],
               "condition_met_at_utc": rec["condition_met_at_utc"], "timestamp": rec["condition_met_at_utc"]}
    try:
        r = requests.post(url, json=payload, timeout=(5.0, 10.0))
        return {"outcome": "sent", "http_status": r.status_code, "error": None}
    except Exception as e:  # noqa: BLE001
        return {"outcome": "error", "http_status": None, "error": f"{type(e).__name__}: {e}"}


def run(run_id, evidence_path, *, artifacts=DEFAULT_ARTIFACTS, sink_url=None, max_evals=None,
        now_fn=lambda: datetime.now(timezone.utc), monotonic_fn=time.monotonic, sleep_fn=time.sleep,
        snapshot_fn=k8s_snapshot, range_fn=prom_range, instant_fn=prom_instant, post_fn=post_sink):
    sink_url = check_sink_url(sink_url)
    art = load_artifacts(Path(artifacts))
    state, seq = {"consecutive": 0, "last_signal": None}, 0
    with open(evidence_path, "a", encoding="utf-8") as fh:
        _must_write(fh, {"record_type": "c1_shadow_start", "run_id": run_id, "detector": DETECTOR_ID,
                                  "artifacts": str(artifacts), "artifact_hashes": art["hashes"], "threshold": art["threshold"],
                                  "kept_features": art["schema"]["kept_feature_names"], "sink_url": sink_url,
                                  "window_sec": WINDOW_SEC, "eval_interval_sec": EVAL_INTERVAL_SEC,
                                  "consecutive_threshold": CONSECUTIVE_THRESHOLD, "cooldown_sec": COOLDOWN_SEC,
                                  "started_at_utc": now_fn().isoformat()})
        while max_evals is None or seq < max_evals:
            seq += 1
            rec = evaluate_once(art, now_fn=now_fn, snapshot_fn=snapshot_fn, range_fn=range_fn, instant_fn=instant_fn)
            rec.update(record_type="c1_evaluation", run_id=run_id, evaluation_seq=seq)
            step_streak(rec, state, art["threshold"], monotonic_fn())
            _must_write(fh, rec)  # 실패면 sink로 보내지 않고 종료(기록 없는 경고 금지)
            if rec["condition_met"] and sink_url:
                res = post_fn(sink_url, run_id, rec)
                _must_write(fh, {"record_type": "c1_sink_result", "run_id": run_id, "evaluation_seq": seq,
                                          "completed_at_utc": now_fn().isoformat(), **res})
            print(f"[{rec.get('score_completed_at_utc') or rec.get('skipped_at_utc')}] seq={seq} {rec['status']} "
                  f"score={rec.get('score')} 연속={rec['consecutive_anomalous']} {rec.get('skip_reason') or ''}", flush=True)
            sleep_fn(EVAL_INTERVAL_SEC)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="v3.3-c1 기록 전용 실시간 탐지기(capture sink·로컬 기록만)")
    p.add_argument("--run-id", required=True)
    p.add_argument("--evidence-log", required=True)
    p.add_argument("--artifacts-dir", default=str(DEFAULT_ARTIFACTS))
    p.add_argument("--sink-url", default=None)
    a = p.parse_args()
    run(a.run_id, a.evidence_log, artifacts=a.artifacts_dir, sink_url=a.sink_url)
