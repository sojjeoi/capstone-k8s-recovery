"""phase2_network_env 실행기 시험(2026-10-17, DEVELOPMENT). 클러스터·VPN·실제 artifact 접근 없음.
c3-A 외부 artifact는 tmp에 만든 합성 IF/scaler(실제 메타데이터 파일 복사)로 대신하고 기대 해시만 그 값으로 바꾼다(Phase 2 시험과 같은 방식)."""
import copy
import inspect
import json
import math
import pickle
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import n2_common as nc  # noqa: E402
import n2_executor as ex  # noqa: E402

pc = nc.pc
import c1_policy_link as link  # noqa: E402
import guarded_schema as gsch  # noqa: E402
import test_run_guarded_pilot as tgp  # noqa: E402
from feature_selection import FEATURE_NAMES  # noqa: E402
from guarded_policy import GuardedPolicy  # noqa: E402

CPU, NET, IF = nc.CPU_ID, nc.NET_ID, nc.IF_ID
FAKE = "f" * 64
T0 = datetime(2026, 10, 20, 1, 0, tzinfo=timezone.utc)
SNAP = {"active_selector": "aaa111", "preview_selector": "bbb222",
        "pods": [{"name": "vllm-serving-aaa111-x", "uid": "ua", "hash": "aaa111", "restart_count": 0},
                 {"name": "vllm-serving-bbb222-y", "uid": "up", "hash": "bbb222", "restart_count": 0}]}
EXPECTED = {"vllm-serving-aaa111-x": "ua", "vllm-serving-bbb222-y": "up"}
HOT, COLD = (3.0, -0.5, 1.0), (1.0, 0.2, 0.001)       # (cpu_mean, if_score, net_probe) 모두 이상 / 모두 정상
VALUE = {CPU: 3.0, IF: -0.5, NET: 1.0}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """합성 외부 artifact + 동결 해시 교체 + 사전등록 해시 가짜 값. 반환: (선택 profile 경로, artifact 루트)."""
    from sklearn.ensemble import IsolationForest
    from sklearn.preprocessing import StandardScaler
    root = tmp_path / "external-artifacts"
    root.mkdir()
    for f in ("feature-schema.json", "threshold.json", "freeze.json"):
        shutil.copy(pc.AD / "v3" / "model_c3_normal_coverage" / "artifacts" / f, root / f)
    x = np.random.default_rng(0).normal(size=(200, 4))
    sc = StandardScaler().fit(x)
    (root / "model.pkl").write_bytes(pickle.dumps(IsolationForest(n_estimators=10, random_state=0).fit(sc.transform(x))))
    (root / "scaler.pkl").write_bytes(pickle.dumps(sc))
    frozen = copy.deepcopy(pc.FROZEN_DETECTORS)
    for f, k in pc.C3A_FILES.items():
        frozen[IF]["artifact"][k] = pc.sha256_bytes(root / f)
    monkeypatch.setattr(pc, "FROZEN_DETECTORS", frozen)
    monkeypatch.setattr(nc, "FROZEN_DETECTORS", {CPU: frozen[CPU], NET: nc.FROZEN_NET, IF: frozen[IF]})
    monkeypatch.setattr(nc, "PREREG_SHA256", FAKE)
    environment = {"serving_backend": {"image": "vllm-cpu-env:latest", "image_pull_policy": "Never"}, "model": "Qwen/Qwen2.5-0.5B-Instruct",
                   "compute_type": "CPU", "resources": {"limits": {"cpu": "3", "memory": "6Gi"}}, "serving_args": ["--port=8000"],
                   "workload_contract": "a" * 64, "slo_contract": "b" * 64, "telemetry_contract": "c" * 64}
    p = tmp_path / "selection-profile.json"
    p.write_text(json.dumps(nc.build_selection_profile(environment, "d" * 64, FAKE), indent=2), encoding="utf-8")
    return p, root


def write_profile(d, prof, name="p.json"):
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(json.dumps(prof), encoding="utf-8")
    return p


def active_of(profile_path, det):
    p = json.loads(profile_path.read_text(encoding="utf-8"))
    for d in p["detectors"]:
        d["role"] = nc.ACTIVE if d["detector_id"] == det else nc.SHADOW
    return p


def rec(i, cpu, score, net, status="scored"):
    t = T0 + timedelta(seconds=15 * i)
    r = {"eval_started_at_utc": t.isoformat(), "window_start_utc": (t - timedelta(seconds=60)).isoformat(),
         "window_end_utc": t.isoformat(), "status": status, "pod_snapshot": SNAP, "expected_pods": dict(EXPECTED)}
    if status == "scored":
        r.update(features={**{n: 0.0 for n in FEATURE_NAMES}, "cpu_mean": cpu}, score=score,
                 score_completed_at_utc=(t + timedelta(seconds=1)).isoformat())
    else:
        r.update(skip_reason="x", skipped_at_utc=t.isoformat())
    return r


def body(v):
    return {"status": "success", "data": {"result": [] if v is None else [{"metric": {"pod": "p"}, "value": [1.0, str(v)]}]}}


def run(monkeypatch, tmp_path, recs, nets, fn, **kw):
    """recs: 평가 기록(evaluate_once 대체), nets: tick별 net_probe 질의 응답 본문 또는 예외."""
    it, nt = iter(recs), iter(nets)
    monkeypatch.setattr(ex, "evaluate_once", lambda art, **k: copy.deepcopy(next(it)))

    def instant(q):
        b = next(nt)
        if isinstance(b, Exception):
            raise b
        return b
    p = tmp_path / "ev.jsonl"
    if p.exists():
        p.unlink()
    code = fn("rid", p, max_evals=len(recs), sleep_fn=lambda s: None, monotonic_fn=iter(range(0, 10_000, 15)).__next__,
              instant_fn=instant, **kw)
    return code, [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]


def hot_run(monkeypatch, tmp_path, fn, n=4, **kw):
    recs = [rec(i, HOT[0], HOT[1], HOT[2]) for i in range(1, n + 1)]
    return run(monkeypatch, tmp_path, recs, [body(HOT[2])] * n, fn, **kw)


def active_run(monkeypatch, tmp_path, profile_path, root, n=4):
    sends = []

    def send_fn(url, payload, timeout):
        sends.append(payload)
        return {"delivery": "delivered", "http_status": 200, "policy_outcome": "would_pass_record_only"}
    code, out = hot_run(monkeypatch, tmp_path, ex.run_active, n, profile_path=profile_path, artifact_root=root,
                        policy_base_url="http://127.0.0.1:18090", expected_version="guarded-v1", expected_mode="act",
                        check_fn=lambda *a, **k: {"ok": True}, send_fn=send_fn)
    return code, out, sends


# ---------- 같은 tick 판정 ----------
def test_shadow_judges_three_detectors_same_tick_and_cannot_send(monkeypatch, tmp_path, env, capsys):
    profile, root = env
    params = inspect.signature(ex.run_shadow).parameters
    assert "send_fn" not in params and "policy_base_url" not in params
    code, recs = hot_run(monkeypatch, tmp_path, ex.run_shadow, profile_path=profile, artifact_root=root)
    assert code == 0 and not [r for r in recs if r["record_type"].startswith("c1_policy")]
    ev = [r for r in recs if r["record_type"] == "c1_evaluation"]
    assert set(ev[0]["rules"]) == set(nc.DETECTOR_IDS)
    assert all(ev[2]["rules"][d]["condition_met"] for d in nc.DETECTOR_IDS)            # 셋 다 발화해도 송신 0
    assert all(j["role"] == nc.SHADOW for r in ev for j in r["rules"].values()) and recs[0]["active_detector"] is None
    assert ev[0]["net_probe"]["value"] == HOT[2] and ev[0]["net_probe"]["n_series"] == 1
    assert recs[0]["thresholds"][NET] == nc.NET_THRESHOLD and recs[0]["net_probe_definition_sha256"] == nc.NET_DEFINITION_SHA256
    out = capsys.readouterr().out
    assert "seq=1 scored" in out and "ANOMALY" not in out and "condition_met" not in out      # 진행 표시는 결과를 숨긴다


@pytest.mark.parametrize("bad", [body(None), {"data": {"result": [{"value": [1, "1.0"]}, {"value": [1, "2.0"]}]}},
                                 body("NaN"), body("inf"), {"unexpected": 1}, RuntimeError("boom")])
def test_net_probe_unknown_when_not_exactly_one_finite_series(monkeypatch, tmp_path, env, bad):
    profile, root = env
    recs = [rec(i, *HOT) for i in range(1, 4)]
    code, out = run(monkeypatch, tmp_path, recs, [bad] * 3, ex.run_shadow, profile_path=profile, artifact_root=root)
    ev = [r for r in out if r["record_type"] == "c1_evaluation"]
    assert all(r["rules"][NET]["state"] == nc.UNKNOWN and r["rules"][NET]["consecutive"] == 0 for r in ev)
    assert all(r["net_probe"]["value"] is None and r["net_probe"]["error"] for r in ev)
    assert ev[2]["rules"][CPU]["condition_met"] and ev[2]["rules"][IF]["condition_met"]      # 다른 후보는 영향 없음


def test_skipped_evaluation_resets_all_and_skips_net_query(monkeypatch, tmp_path, env):
    profile, root = env
    recs = [rec(1, *HOT), rec(2, *HOT), rec(3, None, None, None, status="skipped"), rec(4, *HOT)]
    asked = []
    it = iter(recs)
    monkeypatch.setattr(ex, "evaluate_once", lambda art, **k: copy.deepcopy(next(it)))
    p = tmp_path / "ev.jsonl"
    ex.run_shadow("rid", p, profile_path=profile, artifact_root=root, max_evals=4, sleep_fn=lambda s: None,
                  monotonic_fn=iter(range(0, 1000, 15)).__next__, instant_fn=lambda q: asked.append(q) or body(1.0))
    ev = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if '"c1_evaluation"' in x]
    assert len(asked) == 3 and ev[2]["net_probe"] is None                                    # 건너뛴 평가에는 질의하지 않음
    assert all(ev[2]["rules"][d]["state"] == nc.UNKNOWN for d in nc.DETECTOR_IDS)
    assert all(ev[3]["rules"][d]["consecutive"] == 1 for d in nc.DETECTOR_IDS)                # 건너뜀 뒤 연속은 다시 1부터


def test_cold_values_stay_normal(monkeypatch, tmp_path, env):
    profile, root = env
    recs = [rec(i, *COLD) for i in range(1, 5)]
    _, out = run(monkeypatch, tmp_path, recs, [body(COLD[2])] * 4, ex.run_shadow, profile_path=profile, artifact_root=root)
    ev = [r for r in out if r["record_type"] == "c1_evaluation"]
    assert all(j["state"] == nc.NORMAL and not j["condition_met"] for r in ev for j in r["rules"].values())


# ---------- 권한 ----------
@pytest.mark.parametrize("det", nc.DETECTOR_IDS)
def test_active_payload_only_from_active_and_passes_real_schema_and_policy_identity(monkeypatch, tmp_path, env, det):
    profile, root = env
    ap = write_profile(tmp_path, active_of(profile, det))
    code, out, sends = active_run(monkeypatch, tmp_path, ap, root)
    assert code == 0 and len(sends) == 1
    assert sends[0]["detector_id"] == det and sends[0]["score"] == VALUE[det]
    assert sends[0]["threshold"] == nc.FROZEN_DETECTORS[det]["threshold"]
    assert all(r.get("detector_id") == det for r in out if r["record_type"] == "c1_policy_send_attempt")
    sig, err = gsch.parse_signal(sends[0])
    assert err is None, err
    art = pc.verify_artifacts(root)
    ctx = nc.build_context("rid", dict(tgp.ROLL), nc.load_profile(ap, stage="stage2"), art["hashes"])
    assert ctx["actions_allowed"] is True and ctx["detector_id"] == det
    assert GuardedPolicy._context_reasons(None, ctx, sig) == []
    for other in (d for d in nc.DETECTOR_IDS if d != det):                                    # 다른 detector 식별은 정책이 거부
        assert GuardedPolicy._context_reasons(None, {"run_id": "rid", **nc.signal_identity(other, art["hashes"])}, sig) != []


def test_build_payload_refuses_shadow_judgement():
    with pytest.raises(link.PayloadError):
        ex.build_payload({"record_type": "c1_live_start", "active_detector": CPU}, {"rules": {IF: {"role": nc.SHADOW}}}, IF)


def test_shadow_only_profile_cannot_start_active_and_vice_versa(tmp_path, env):
    profile, root = env
    with pytest.raises(nc.ProfileInvalid):
        ex.run_active("rid", tmp_path / "e.jsonl", profile_path=profile, artifact_root=root, policy_base_url="http://x",
                      expected_version="v", expected_mode="act")
    with pytest.raises(nc.ProfileInvalid):
        ex.run_shadow("rid", tmp_path / "e.jsonl", profile_path=write_profile(tmp_path, active_of(profile, CPU)), artifact_root=root)
    with pytest.raises(SystemExit):
        ex.run_active("rid", tmp_path / "e.jsonl", profile_path=profile, artifact_root=root, policy_base_url="http://x",
                      expected_version="v", expected_mode="record_only")


def test_executor_refuses_to_start_on_invalid_profile_before_any_evidence(tmp_path, env, monkeypatch):
    profile, root = env
    for mutate in (lambda p: p["detectors"][1].__setitem__("threshold", 0.01),        # net_probe 임계값 변경
                   lambda p: p["environment"]["probe_profile"].__setitem__("readiness_timeout_sec", 1),
                   lambda p: p["selection"].__setitem__("rule", "0" * 64)):
        p = json.loads(profile.read_text(encoding="utf-8"))
        mutate(p)
        bad = write_profile(tmp_path / "bad", p)
        ev = tmp_path / "bad" / "e.jsonl"
        with pytest.raises(nc.ProfileInvalid):
            ex.run_shadow("rid", ev, profile_path=bad, artifact_root=root)
        assert not ev.exists()                                                           # 증거 파일도 만들지 않는다
    monkeypatch.setattr(nc, "PREREG_SHA256", None)                                       # 사전등록 미동결이면 어떤 프로필도 거부
    with pytest.raises(nc.ProfileInvalid, match="동결되지 않음"):
        ex.run_shadow("rid", tmp_path / "e2.jsonl", profile_path=profile, artifact_root=root)


def test_executor_refuses_when_net_calibration_evidence_changes(tmp_path, env, monkeypatch):
    profile, root = env
    monkeypatch.setattr(nc, "NET_CALIBRATION_SHA256", "0" * 64)
    with pytest.raises(nc.ArtifactMismatch, match="해시 불일치"):
        ex.run_shadow("rid", tmp_path / "e.jsonl", profile_path=profile, artifact_root=root)


def test_active_sends_once_per_context_and_stops_after_post_action(monkeypatch, tmp_path, env):
    profile, root = env
    ap = write_profile(tmp_path, active_of(profile, NET))
    sends = []

    def send_fn(url, payload, timeout):
        sends.append(payload)
        return {"delivery": "delivered", "http_status": 200, "policy_outcome": "promoted_verified", "policy_passed": True,
                "promotion": {"attempted": True, "verified": True}, "target_key": "t", "reasons": []}
    code, out = hot_run(monkeypatch, tmp_path, ex.run_active, 8, profile_path=ap, artifact_root=root,
                        policy_base_url="http://127.0.0.1:18090", expected_version="guarded-v1", expected_mode="act",
                        check_fn=lambda *a, **k: {"ok": True}, send_fn=send_fn)
    assert code == 0 and len(sends) == 1
    ev = [r for r in out if r["record_type"] == "c1_evaluation"]
    assert [r["post_action"] for r in ev][:3] == [False, False, False] and ev[-1]["post_action"] is True


def test_read_net_probe_records_query_and_times():
    t = iter([T0, T0 + timedelta(seconds=1)])
    r = ex.read_net_probe(lambda q: body(0.0123), lambda: next(t))
    assert r["value"] == 0.0123 and r["promql"] == nc.NET_EXPR and r["error"] is None and r["n_series"] == 1
    assert r["received_at_utc"] > r["sent_at_utc"]
    assert math.isfinite(r["value"])
