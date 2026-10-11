"""Phase 2 detector selection 시험(prereg §11 1-29 + 실행기 연결). 클러스터·VPN·실제 artifact 접근 없음.
실행: 저장소 루트에서 python -m pytest experiments/phase2_detector_selection -q -p no:cacheprovider
c3-A 외부 artifact는 tmp에 만든 합성 IF/scaler(실제 메타데이터 파일 복사)로 대신하고, 기대 해시만 그 값으로 바꾼다.
실제 artifact 확인은 PHASE2_ARTIFACT_ROOT가 있을 때만(test_real_external_artifacts_match_frozen_hashes)."""
import copy
import inspect
import json
import os
import pickle
import random
import shutil
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
import yaml

import p2_common as pc
import p2_executor as ex
import run_stage1 as s1
import run_stage2 as s2
import selection as sel

import c1_policy_link as link  # noqa: E402
import guarded_live_detector_runner as gr  # noqa: E402
import guarded_schema as gsch  # noqa: E402
import guarded_state as gs  # noqa: E402
import run_guarded_pilot as gp  # noqa: E402
import test_run_guarded_pilot as tgp  # noqa: E402
from feature_selection import FEATURE_NAMES  # noqa: E402
from guarded_policy import AuditLog, GuardConfig, GuardedPolicy  # noqa: E402

T0 = datetime(2026, 10, 20, 1, 0, tzinfo=timezone.utc)
TEMPLATE = yaml.safe_load((pc.ROOT / "gitops/apps/vllm-serving/rollout.yaml").read_text(encoding="utf-8"))["spec"]["template"]
SNAP = {"active_selector": "aaa111", "preview_selector": "bbb222",
        "pods": [{"name": "vllm-serving-aaa111-x", "uid": "ua", "hash": "aaa111", "restart_count": 0},
                 {"name": "vllm-serving-bbb222-y", "uid": "up", "hash": "bbb222", "restart_count": 0}]}
EXPECTED = {"vllm-serving-aaa111-x": "ua", "vllm-serving-bbb222-y": "up"}
HOT, COLD = (3.0, -0.5), (1.0, 0.2)      # (cpu_mean, if_score): 둘 다 이상 / 둘 다 정상


# ---------------- 공통 픽스처 ----------------
@pytest.fixture
def art_root(tmp_path, monkeypatch):
    """합성 외부 artifact(4특징 IF + scaler) + 실제 메타데이터 복사. 동결 기대 해시를 이 파일들 값으로 바꾼다."""
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
        frozen[pc.IF_ID]["artifact"][k] = pc.sha256_bytes(root / f)
    monkeypatch.setattr(pc, "FROZEN_DETECTORS", frozen)
    return root


@pytest.fixture
def profile(tmp_path, art_root):
    p = tmp_path / "selection-profile.json"
    p.write_text(json.dumps(pc.build_selection_profile(TEMPLATE), indent=2), encoding="utf-8")
    return p


def _write_profile(d, prof, name="p.json"):
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(json.dumps(prof), encoding="utf-8")
    return p


def _active(profile_path, det):
    p = json.loads(profile_path.read_text(encoding="utf-8"))
    for d in p["detectors"]:
        d["role"] = pc.ACTIVE if d["detector_id"] == det else pc.SHADOW
    return p


def _rec(i, cpu, score, status="scored"):
    t = T0 + timedelta(seconds=15 * i)
    r = {"eval_started_at_utc": t.isoformat(), "window_start_utc": (t - timedelta(seconds=60)).isoformat(),
         "window_end_utc": t.isoformat(), "status": status, "pod_snapshot": SNAP, "expected_pods": dict(EXPECTED)}
    if status == "scored":
        r.update(features={**{n: 0.0 for n in FEATURE_NAMES}, "cpu_mean": cpu}, score=score,
                 score_completed_at_utc=(t + timedelta(seconds=1)).isoformat())
    else:
        r.update(skip_reason="x", skipped_at_utc=t.isoformat())
    return r


def _evals(pairs, start=1):
    """[(cpu, score) 또는 None(건너뜀)] → c1_evaluation 기록(번호 연속)."""
    out = []
    for i, p in enumerate(pairs, start=start):
        r = _rec(i, *(p or (None, None)), status="scored" if p else "skipped")
        r.update(record_type="c1_evaluation", run_id="rid", evaluation_seq=i)
        out.append(r)
    return out


def _run_exec(monkeypatch, tmp_path, recs, fn, **kw):
    it = iter(recs)
    monkeypatch.setattr(ex, "evaluate_once", lambda art, **k: copy.deepcopy(next(it)))
    p = tmp_path / "ev.jsonl"
    if p.exists():
        p.unlink()
    code = fn("rid", p, max_evals=len(recs), sleep_fn=lambda s: None, monotonic_fn=iter(range(0, 10_000, 15)).__next__, **kw)
    return code, [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]


def _active_run(monkeypatch, tmp_path, recs, profile_path, root):
    sends = []

    def send_fn(url, payload, timeout):
        sends.append(payload)
        return {"delivery": "delivered", "http_status": 200, "policy_outcome": "would_pass_record_only"}
    code, out = _run_exec(monkeypatch, tmp_path, recs, ex.run_active, profile_path=profile_path, artifact_root=root,
                          policy_base_url="http://127.0.0.1:18090", expected_version="guarded-v1", expected_mode="act",
                          check_fn=lambda *a, **k: {"ok": True}, send_fn=send_fn)
    return code, out, sends


# ---------------- 1-3 정규화 ----------------
def test_01_cpu_normalization_strict_greater_and_three_consecutive():
    st = {"consecutive": 0, "last_signal": None}
    assert pc.judge(pc.CPU_ID, _rec(1, pc.CPU_THRESHOLD, 0), None, st, 0)["state"] == pc.NORMAL   # 같으면 정상(엄격)
    js = [pc.judge(pc.CPU_ID, _rec(i, 2.0, 0), None, st, 15.0 * i) for i in range(2, 5)]
    assert [j["state"] for j in js] == [pc.ANOMALY] * 3 and [j["condition_met"] for j in js] == [False, False, True]
    assert js[-1]["value"] == 2.0 and js[-1]["threshold"] == pc.CPU_THRESHOLD


def test_02_if_normalization_strict_less_and_offline_score_equals_model(art_root):
    import c3_common as cc
    st = {"consecutive": 0, "last_signal": None}
    assert pc.judge(pc.IF_ID, _rec(1, 0, pc.IF_THRESHOLD), pc.IF_THRESHOLD, st, 0)["state"] == pc.NORMAL
    assert pc.judge(pc.IF_ID, _rec(2, 0, -0.3), -0.3, st, 15)["state"] == pc.ANOMALY
    art = pc.verify_artifacts(art_root)
    evals = _evals([(1.0, None), (2.0, None)])
    got = sel.if_scores_for(evals, art)
    want = cc.score_rows(art["model"], art["scaler"], art["schema"], [cc.feature_row(r) for r in evals])
    assert list(got.values()) == want and list(got) == [1, 2]


def test_03_state_mapping_unknown_is_per_detector_and_resets_streak():
    sts = {d: {"consecutive": 0, "last_signal": None} for d in pc.DETECTOR_IDS}
    roles = {d: pc.SHADOW for d in pc.DETECTOR_IDS}
    art = {"schema": {"kept_feature_names": ["cpu_mean"]}}
    ex.tick(_rec(1, *HOT), art, sts, roles, 0)
    r = ex.tick(_rec(2, None, None, status="skipped"), art, sts, roles, 15)
    assert {d: r[d]["state"] for d in pc.DETECTOR_IDS} == {pc.CPU_ID: pc.UNKNOWN, pc.IF_ID: pc.UNKNOWN}
    assert all(r[d]["consecutive"] == 0 for d in pc.DETECTOR_IDS)
    r = ex.tick(_rec(3, float("nan"), -0.5), art, sts, roles, 30)   # CPU 입력만 무효 → CPU만 UNKNOWN
    assert r[pc.CPU_ID]["state"] == pc.UNKNOWN and r[pc.IF_ID]["state"] == pc.ANOMALY and r[pc.IF_ID]["consecutive"] == 1


# ---------------- 4-5 권한 ----------------
def test_04_shadow_has_no_send_path_and_payload_always_from_active(monkeypatch, tmp_path, profile, art_root):
    params = inspect.signature(ex.run_shadow).parameters
    assert "send_fn" not in params and "policy_base_url" not in params
    code, recs = _run_exec(monkeypatch, tmp_path, [_rec(i, *HOT) for i in range(1, 5)], ex.run_shadow,
                           profile_path=profile, artifact_root=art_root)
    assert code == 0 and not [r for r in recs if r["record_type"].startswith("c1_policy")]
    ev = [r for r in recs if r["record_type"] == "c1_evaluation"]
    assert ev[2]["rules"][pc.CPU_ID]["condition_met"] and ev[2]["rules"][pc.IF_ID]["condition_met"]   # 둘 다 발화해도 송신 0
    assert all(j["role"] == pc.SHADOW for r in ev for j in r["rules"].values()) and recs[0]["active_detector"] is None
    for det, val in ((pc.CPU_ID, 3.0), (pc.IF_ID, -0.5)):
        ap = _write_profile(tmp_path, _active(profile, det), f"a-{det}.json")
        code, out, sends = _active_run(monkeypatch, tmp_path, [_rec(i, *HOT) for i in range(1, 5)], ap, art_root)
        assert code == 0 and len(sends) == 1
        assert sends[0]["detector_id"] == det and sends[0]["score"] == val
        assert sends[0]["threshold"] == pc.FROZEN_DETECTORS[det]["threshold"]
        assert all(r.get("detector_id") == det for r in out if r["record_type"] == "c1_policy_send_attempt")
    with pytest.raises(link.PayloadError):        # 실행기 내부 오배정 방지: SHADOW 판정으로 payload를 만들 수 없음
        ex.build_payload({"record_type": "c1_live_start", "active_detector": pc.CPU_ID},
                         {"rules": {pc.IF_ID: {"role": pc.SHADOW}}}, pc.IF_ID)


def test_04b_shadow_only_profile_cannot_start_active_and_vice_versa(tmp_path, profile, art_root):
    with pytest.raises(pc.ProfileInvalid):
        ex.run_active("rid", tmp_path / "e.jsonl", profile_path=profile, artifact_root=art_root, policy_base_url="http://x",
                      expected_version="v", expected_mode="act")
    with pytest.raises(pc.ProfileInvalid):
        ex.run_shadow("rid", tmp_path / "e.jsonl", profile_path=_write_profile(tmp_path, _active(profile, pc.CPU_ID)),
                      artifact_root=art_root)


@pytest.mark.parametrize("det", pc.DETECTOR_IDS)
def test_05_active_payload_passes_real_schema_and_policy_identity(monkeypatch, tmp_path, profile, art_root, det):
    ap = _write_profile(tmp_path, _active(profile, det))
    _, _, sends = _active_run(monkeypatch, tmp_path, [_rec(i, *HOT) for i in range(1, 4)], ap, art_root)
    sig, err = gsch.parse_signal(sends[0])
    assert err is None, err
    art = pc.verify_artifacts(art_root)
    ctx = pc.build_context("rid", dict(tgp.ROLL), pc.load_profile(ap, stage="stage2"), art["hashes"])
    assert ctx["actions_allowed"] is True and ctx["detector_id"] == det
    assert GuardedPolicy._context_reasons(None, ctx, sig) == []
    other = next(d for d in pc.DETECTOR_IDS if d != det)
    assert GuardedPolicy._context_reasons(None, {"run_id": "rid", **pc.signal_identity(other, art["hashes"])}, sig) != []


# ---------------- 6-9·17·21 profile ----------------
def test_06_two_active_rejected_always(profile):
    p = json.loads(profile.read_text(encoding="utf-8"))
    for d in p["detectors"]:
        d["role"] = pc.ACTIVE
    for stage in ("stage1", "stage2"):
        with pytest.raises(pc.ProfileInvalid, match="active_count"):
            pc.validate_profile(p, stage=stage)


def test_07_no_active_means_no_act_context(profile):
    lp = pc.load_profile(profile, stage="stage1")
    assert lp["active"] is None
    with pytest.raises(pc.ProfileInvalid):
        pc.build_context("rid", dict(tgp.ROLL), lp, {})
    with pytest.raises(pc.ProfileInvalid, match="stage2_requires_active_1"):
        pc.load_profile(profile, stage="stage2")


def test_08_stale_profile_detected_for_each_contract_item(profile):
    p = pc.load_profile(profile, stage="stage1")["profile"]
    pol = pc.contract_hashes()["recovery_policy_sha256"]
    env0 = pc.observe_environment(TEMPLATE)
    assert pc.check_stale(p, env0, pol) == []
    for key, val in (("serving_backend", {"image": "vllm-gpu:1", "image_pull_policy": "Never"}), ("model", "other"),
                     ("compute_type", "GPU"), ("resources", {}), ("serving_args", ["--x"]),
                     ("workload_contract", "0" * 64), ("slo_contract", "1" * 64), ("telemetry_contract", "2" * 64)):
        assert pc.check_stale(p, {**env0, key: val}, pol) == [f"environment.{key}"]
    assert pc.check_stale(p, env0, "f" * 64) == ["recovery_policy_sha256"]
    gpu = copy.deepcopy(TEMPLATE)
    gpu["spec"]["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] = 1
    assert pc.env_from_template(gpu)["compute_type"] == "GPU"


@pytest.mark.parametrize("mutate,match", [
    (lambda p: p["detectors"][0].update(threshold=1.7), "detector_differs_from_frozen"),
    (lambda p: p["detectors"][1].update(detector_version="other"), "detector_differs_from_frozen"),
    (lambda p: p["detectors"][1]["artifact"].update(model_sha256="0" * 64), "detector_differs_from_frozen"),
    (lambda p: p["environment"].update(model_dir="C:\\models\\x"), "absolute_path"),
    (lambda p: p["environment"].update(model_dir="/home/ubuntu/x"), "absolute_path"),
    (lambda p: p["selection"].update(rule="0" * 64), "selection_rule_not_design_freeze"),
    (lambda p: p["detectors"][0].update(role="PRIMARY"), "role_unknown"),
])
def test_09_profile_mismatch_rejected(profile, mutate, match):
    p = json.loads(profile.read_text(encoding="utf-8"))
    mutate(p)
    with pytest.raises(pc.ProfileInvalid, match=match):
        pc.validate_profile(p, stage="stage1")


def test_09b_unknown_or_missing_detector_rejected(profile):
    p = json.loads(profile.read_text(encoding="utf-8"))
    p["detectors"][1]["detector_id"] = "new-detector"
    with pytest.raises(pc.ProfileInvalid, match="후보"):
        pc.validate_profile(p, stage="stage1")
    p["detectors"] = p["detectors"][:1]
    with pytest.raises(pc.ProfileInvalid):
        pc.validate_profile(p, stage="stage1")


def test_17_profile_hash_recorded_and_line_ending_independent(monkeypatch, tmp_path, profile, art_root):
    _, recs = _run_exec(monkeypatch, tmp_path, [_rec(1, *COLD)], ex.run_shadow, profile_path=profile, artifact_root=art_root)
    sha = pc.load_profile(profile, stage="stage1")["sha256"]
    assert recs[0]["environment_profile_sha256"] == sha and recs[0]["environment_profile_id"] == "cpu-vllm-qwen05b-v1"
    lf, crlf = tmp_path / "lf.json", tmp_path / "crlf.json"
    lf.write_bytes(profile.read_bytes().replace(b"\r\n", b"\n"))
    crlf.write_bytes(lf.read_bytes().replace(b"\n", b"\r\n"))
    assert pc.load_profile(lf, stage="stage1")["sha256"] == pc.load_profile(crlf, stage="stage1")["sha256"] == sha


def test_21_threshold_none_rejected(profile):
    p = json.loads(profile.read_text(encoding="utf-8"))
    p["detectors"][0]["threshold"] = None
    with pytest.raises(pc.ProfileInvalid, match="threshold_none"):
        pc.validate_profile(p, stage="stage1")
    assert all(pc.signal_identity(d, {"model.pkl": "a" * 64})["threshold"] is not None for d in pc.DETECTOR_IDS)


# ---------------- 10-11 정책 2차 방어(실제 guarded 정책) ----------------
def _policy(d):
    return GuardedPolicy(GuardConfig(mode="record_only", **tgp.TIMING), gs.StateStore(d / "state.json"),
                         audit=AuditLog(d / "audit.jsonl"), cluster_reader=tgp.view, initialize=True)


def test_10_detector_version_or_threshold_mismatch_rejected_context(monkeypatch, tmp_path, profile, art_root):
    ap = _write_profile(tmp_path, _active(profile, pc.CPU_ID))
    _, _, sends = _active_run(monkeypatch, tmp_path, [_rec(i, *HOT) for i in range(1, 4)], ap, art_root)
    art = pc.verify_artifacts(art_root)
    for k, v in (("model_version", "fixed-cpu-gt-2.7"), ("threshold", 2.7), ("model_hash", "0" * 64)):
        pol = _policy(tmp_path / k)
        pol.set_context({**pc.signal_identity(pc.CPU_ID, art["hashes"]), k: v, "run_id": "rid", "rollout": dict(tgp.ROLL)})
        assert pol.process(sends[0])["outcome"] == "rejected_context"


def test_11_stale_signal_rejected_timing(monkeypatch, tmp_path, profile, art_root):
    ap = _write_profile(tmp_path, _active(profile, pc.IF_ID))
    _, _, sends = _active_run(monkeypatch, tmp_path, [_rec(i, *HOT) for i in range(1, 4)], ap, art_root)
    pol = _policy(tmp_path / "p")
    pol.set_context({**pc.signal_identity(pc.IF_ID, pc.verify_artifacts(art_root)["hashes"]), "run_id": "rid",
                     "rollout": dict(tgp.ROLL)})
    assert pol.process(sends[0])["outcome"] == "rejected_timing"     # T0 기준 신호 - 오래됨


# ---------------- 12 같은 recovery 경로(실제 guarded 정책 앱 + 가짜 클러스터) ----------------
class P2ActiveDetector(tgp.FakeDetector):
    """동결 FakeDetector 시계·송신 규칙 그대로 + payload는 실제 p2_executor.build_payload(ACTIVE rules)로 만든다."""

    def __init__(self, world, send_at, loaded, art):
        super().__init__(world, send_at)
        self.loaded, self.art, self.active = loaded, art, loaded["active"]

    def starter_factory(self, results_dir, policy_url, version, mode, arm=None):
        self.factory_args = (policy_url, version, mode, arm)

        def start(rid, path, sink_url):
            self.rid, self.path, self.alive = rid, gr.live_evidence_path(results_dir, rid), True
            ident = pc.signal_identity(self.active, self.art["hashes"])
            self.start_rec = ex._start_record("c1_live_start", rid, self.loaded, self.art, tgp._now, actuating_rule=self.active,
                                              signal_identity=ident, threshold=ident["threshold"])
            self._w(self.start_rec)
            self._w({"record_type": "c1_evaluation", "run_id": rid, "evaluation_seq": 0, "status": "scored", "score": 0.1})
            self.t0 = self.world.clock.t
            self.world.clock.listeners.append(self.tick)
            return tgp.DetectorHandle(is_alive=lambda: self.alive, stop=self.stop, label="p2-active-fake")
        return start

    def _send(self, payload):
        self._w({"record_type": "c1_policy_send_attempt", "run_id": self.rid, "evaluation_seq": self.seq,
                 "detector_id": payload["detector_id"], "attempted_at_utc": tgp._now().isoformat()})
        sr = gp.link.send("http://p/signal/v2", payload, post=self._post)
        dec = gp.ld.classify_send_result(sr)
        self._w({"record_type": "c1_policy_send_result", "run_id": self.rid, "evaluation_seq": self.seq, "decision": dec, **sr})
        self.post_action = self.post_action or dec == "post_action"

    def tick(self, t):
        if not self.alive or self.post_action or not self.send_at or t - self.t0 < self.send_at[0]:
            return
        self.send_at.pop(0)
        self.seq += 1
        d = tgp._now()
        val = {pc.CPU_ID: 3.0, pc.IF_ID: -0.5}
        rules = {x: {"value": val[x], "threshold": pc.FROZEN_DETECTORS[x]["threshold"], "state": pc.ANOMALY,
                     "is_anomalous": True, "consecutive": 3, "condition_met": True, "condition_met_at_utc": d.isoformat(),
                     "role": pc.ACTIVE if x == self.active else pc.SHADOW} for x in pc.DETECTOR_IDS}
        rec = {"record_type": "c1_evaluation", "run_id": self.rid, "evaluation_seq": self.seq, "status": "scored",
               "score": -0.5, "window_start_utc": (d - timedelta(seconds=62)).isoformat(),
               "window_end_utc": (d - timedelta(seconds=2)).isoformat(), "rules": rules,
               "expected_pods": {p["name"]: p["uid"] for p in tgp.PODS},
               "pod_snapshot": {"active_selector": "aaa111", "preview_selector": "bbb222", "pods": tgp.PODS}}
        self._w(rec)
        self._send(ex.build_payload(self.start_rec, rec, self.active))


def test_12_cpu_and_if_active_use_same_unmodified_recovery_path(tmp_path, monkeypatch, profile, art_root):
    import run_guarded_bck as gb
    before = (gb.RUNS, gb.BCK_PLAN_ID, gb.ARM_LABEL, gb.bd.signal_identity)
    out = {}
    for det in pc.DETECTOR_IDS:
        ap = _write_profile(tmp_path, _active(profile, det), f"active-{det}.json")
        loaded, art = pc.load_profile(ap, stage="stage2"), pc.verify_artifacts(art_root)
        w = tgp.World(tmp_path / det, monkeypatch, mode=gp.MODE_ACT, send_at=(50.0,), wrap_preview=True)
        w.results = tmp_path / f"res-{det}"
        w.detector = P2ActiveDetector(w, (50.0,), loaded, art)
        w.injector.get_stress_delivery_record = lambda: {"planned_cr_names": [], "target": {"name": "vllm-serving-aaa111-x"}}
        entry = {"selected": det, "art": art, "loaded": loaded, "evidence_sha256": "e" * 64}
        audit = tmp_path / det / "policy" / "audit.jsonl"
        r = s2.run_session("p2s01", deps=w.deps(), entry=entry, active_profile_path=ap, artifact_root=art_root,
                           audit_reader=lambda a=audit: [json.loads(x) for x in a.read_text(encoding="utf-8").splitlines() if x],
                           approval_path=w.approval(runs=["p2s01"]), policy_url="http://p", results_dir=w.results,
                           starter_factory=w.detector.starter_factory)
        s, res = r["summary"], r["session_record"]["result"]
        out[det] = (w, s)
        assert len(w.promotes) == 1 and s["cleanup_errors"] == [], s["cleanup_errors"]
        assert s["context"]["detector_id"] == det and s["context"]["threshold"] == pc.FROZEN_DETECTORS[det]["threshold"]
        assert res["checks"]["promoted_verified"] and res["checks"]["traffic_verified"] and res["authority_leak"] is False
        assert [x["detector_id"] for x in s["policy_decisions"]] == [det] and res["success"] is True     # 정책 감사 기록 identity
    assert (gb.RUNS, gb.BCK_PLAN_ID, gb.ARM_LABEL, gb.bd.signal_identity) == before          # 동결 실행기 전역 복원
    (wc, sc), (wi, si) = out[pc.CPU_ID], out[pc.IF_ID]
    assert sc["scale_down_delay_restore"]["path"] == si["scale_down_delay_restore"]["path"] == "promoted_service_ok"
    assert set(sc) == set(si) and sc["watcher"]["guard_result"]["result"] == si["watcher"]["guard_result"]["result"] == "verified"
    assert type(wc.promotes[0]) is type(wi.promotes[0])


# ---------------- 13-16·18·23·24·28 선택 규칙 ----------------
def _inputs(n_normal=4, n_pressure=5, unn=(0, 0), lat=(100.0, 100.0), integrity=(), gate1=None):
    return {"integrity_problems": list(integrity), "gate1": gate1 or {d: [] for d in pc.DETECTOR_IDS},
            "normal": [{"session": f"n{i}", "attempt": i,                 # unn = 합계(첫 세션에만 둠)
                        "unnecessary": dict(zip(pc.DETECTOR_IDS, unn if i == 0 else (0, 0)))} for i in range(n_normal)],
            "pressure": [{"session": f"p{i}", "attempt": 10 + i, "first_streak_sec": dict(zip(pc.DETECTOR_IDS, lat))}
                         for i in range(n_pressure)]}


def test_13_14_selection_is_deterministic_byte_equal():
    a, b = sel.select(_inputs(unn=(1, 5))), sel.select(_inputs(unn=(1, 5)))
    assert pc.canonical(a) == pc.canonical(b) and a["outcome"] == f"SELECTED:{pc.CPU_ID}"


def test_15_input_order_invariant():
    x = _inputs(unn=(3, 3), lat=(100.0, 200.0))
    y = copy.deepcopy(x)
    random.Random(1).shuffle(y["normal"])
    random.Random(2).shuffle(y["pressure"])
    assert pc.canonical(sel.select(x)) == pc.canonical(sel.select(y))


def test_16_insufficient_valid_sessions_incomplete():
    assert sel.select(_inputs(n_normal=3))["outcome"] == sel.INCOMPLETE
    assert sel.select(_inputs(n_pressure=4))["outcome"] == sel.INCOMPLETE


def test_18_selection_records_all_grounds():
    r = sel.select(_inputs(unn=(2, 2), lat=(100.0, 100.0)))
    g = r["gate_results"]
    assert r["outcome"] == f"SELECTED:{pc.CPU_ID}" and g["tiebreak_simplicity"]["used"] and g["tiebreak_timing"]["used"]
    assert g["gate3"]["result"] == "equivalent" and g["tiebreak_timing"]["result"] == "equivalent"
    assert set(g) >= {"integrity_problems", "gate1", "session_validity", "gate2", "gate3", "tiebreak_timing", "tiebreak_simplicity"}
    assert g["gate2"]["pass_count"] == {pc.CPU_ID: 5, pc.IF_ID: 5} and r["reason"]


@pytest.mark.parametrize("unn,lat,want", [
    ((0, 2), (100.0, 100.0), pc.CPU_ID),        # Gate 3 차이 2 → 적은 쪽
    ((3, 1), (100.0, 100.0), pc.IF_ID),
    ((1, 0), (130.0, 100.0), pc.IF_ID),         # Gate 3 동등(차이 1) → 시각 차이 30 s → 빠른 쪽
    ((1, 0), (129.0, 100.0), pc.CPU_ID),        # 시각 차이 29 s → 동등 → 단순성 순서
])
def test_gate3_and_timing_tiebreaks(unn, lat, want):
    assert sel.select(_inputs(unn=unn, lat=lat))["outcome"] == f"SELECTED:{want}"


def test_timing_tiebreak_infinity_semantics():
    x = _inputs()
    for i, s in enumerate(x["pressure"]):          # IF: 4개 통과 + 1개 미발화(+inf) → 중앙값은 유한
        s["first_streak_sec"] = {pc.CPU_ID: 200.0, pc.IF_ID: None if i == 0 else 100.0}
    r = sel.select(x)
    assert r["outcome"] == f"SELECTED:{pc.IF_ID}" and r["gate_results"]["tiebreak_timing"]["median_sec"][pc.IF_ID] == 100.0


def test_23_gate2_failure_never_selected_even_if_quieter_and_gate3_still_reported():
    x = _inputs(unn=(10, 0))
    for i, s in enumerate(x["pressure"]):          # IF: 5개 중 2개만 363 s 이내(Phase 1 사전 정보와 같은 모양)
        s["first_streak_sec"] = {pc.CPU_ID: 105.0, pc.IF_ID: 120.0 if i < 2 else 400.0}
    r = sel.select(x)
    assert r["outcome"] == f"SELECTED:{pc.CPU_ID}" and r["gate_results"]["gate2"]["eligible"] == [pc.CPU_ID]
    assert r["gate_results"]["gate3"]["unnecessary_sum"] == {pc.CPU_ID: 10, pc.IF_ID: 0} and not r["gate_results"]["gate3"]["used"]


def test_24_outcome_order_integrity_first_then_no_eligible():
    g1 = {pc.CPU_ID: [], pc.IF_ID: ["PROFILE_INVALID: artifact 해시 불일치"]}
    assert sel.select(_inputs(gate1=g1))["outcome"] == sel.INTEGRITY                # 나머지 후보로 넘어가지 않음
    assert sel.select(_inputs(n_normal=1, integrity=["x"]))["outcome"] == sel.INTEGRITY  # 무결성 > 세션 부족
    r = sel.select(_inputs(lat=(None, 400.0)))
    assert r["outcome"] == sel.NO_ELIGIBLE and r["gate_results"]["gate2"]["eligible"] == []


def test_24b_gate2_four_of_five_boundary():
    x = _inputs()
    for i, s in enumerate(x["pressure"]):
        s["first_streak_sec"] = {pc.CPU_ID: 363.0 if i else 363.001, pc.IF_ID: None if i < 2 else 100.0}
    r = sel.select(x)
    assert r["gate_results"]["gate2"]["pass_count"] == {pc.CPU_ID: 4, pc.IF_ID: 3} and r["outcome"] == f"SELECTED:{pc.CPU_ID}"


def test_28_gate3_metrics_present_in_every_outcome():
    for x in (_inputs(n_pressure=2), _inputs(integrity=["marker"]), _inputs(lat=(None, None))):
        g3 = sel.select(x)["gate_results"]["gate3"]
        assert g3["unnecessary_sum"] == {pc.CPU_ID: 0, pc.IF_ID: 0} and len(g3["per_session"]) == 4


def test_selection_function_has_no_weighted_score():
    src = inspect.getsource(sel.select)
    assert "weight" not in src and "*" not in src.replace("**", "")


# ---------------- 19 Gate 2 재생 ----------------
def test_19_pre_injection_evaluations_and_cooldown_excluded():
    pre, post = [HOT] * 3, [HOT] * 10
    evals = _evals(pre + post)
    t_inj = T0 + timedelta(seconds=15 * 3 + 1)      # 주입 전 평가 3개(이미 3연속 신호·쿨다운 시작)
    assert sel.replay_detector(pc.CPU_ID, evals[:3], {})[2]["condition_met"] is True
    first_inc = next(i for i, r in enumerate(evals) if datetime.fromisoformat(r["window_start_utc"]) >= t_inj)
    got = sel.first_streak_after_injection(pc.CPU_ID, evals, t_inj, {})
    want = (datetime.fromisoformat(evals[first_inc + 2]["score_completed_at_utc"]) - t_inj).total_seconds()
    assert got == round(want, 3)                    # 주입 뒤 평가만 새로 3개 - 쿨다운도 이어지지 않음
    assert sel.first_streak_after_injection(pc.CPU_ID, _evals(pre + [COLD] * 10), t_inj, {}) is None


def test_19b_skip_inside_post_injection_resets_streak():
    evals = _evals([(3.0, 0)] * 2 + [None] + [(3.0, 0)] * 3)
    t_inj = T0 - timedelta(seconds=120)
    got = sel.first_streak_after_injection(pc.CPU_ID, evals, t_inj, {})
    assert got == round((datetime.fromisoformat(evals[5]["score_completed_at_utc"]) - t_inj).total_seconds(), 3)


# ---------------- 20 same-tick parity ----------------
def test_20_same_tick_parity(monkeypatch, tmp_path, profile, art_root):
    _, recs = _run_exec(monkeypatch, tmp_path, [_rec(1, 2.5, -0.4), _rec(2, 1.0, 0.3)], ex.run_shadow,
                        profile_path=profile, artifact_root=art_root)
    ev = [x for x in recs if x["record_type"] == "c1_evaluation"]
    assert [r["evaluation_seq"] for r in ev] == [1, 2]
    for r in ev:
        assert r["rules"][pc.CPU_ID]["value"] == r["features"]["cpu_mean"] and r["rules"][pc.IF_ID]["value"] == r["score"]
        assert set(r["rules"]) == set(pc.DETECTOR_IDS)


# ---------------- 22 외부 artifact fail-closed ----------------
@pytest.mark.parametrize("break_it,match", [
    (lambda r: (r / "model.pkl").unlink(), "artifact 없음"),
    (lambda r: (r / "scaler.pkl").write_bytes(b"x"), "해시 불일치"),
    (lambda r: (r / "feature-schema.json").write_text("{}", encoding="utf-8"), "해시 불일치"),
    (lambda r: (r / "threshold.json").write_text('{"threshold": -0.1}', encoding="utf-8"), "해시 불일치"),
])
def test_22_artifact_missing_or_hash_mismatch_profile_invalid(art_root, break_it, match):
    break_it(art_root)
    with pytest.raises(pc.ProfileInvalid, match=match):
        pc.verify_artifacts(art_root)


def test_22b_version_sklearn_threshold_schema_and_root_checks(art_root):
    with pytest.raises(pc.ProfileInvalid, match="sklearn"):
        pc.verify_artifacts(art_root, sklearn_version="1.8.0")
    for root in (None, ""):
        with pytest.raises(pc.ProfileInvalid, match="미지정"):
            pc.verify_artifacts(root)
    fz = json.loads((art_root / "freeze.json").read_text(encoding="utf-8"))
    fz["plan_id"] = "other"
    (art_root / "freeze.json").write_text(json.dumps(fz), encoding="utf-8")
    pc.FROZEN_DETECTORS[pc.IF_ID]["artifact"]["freeze_sha256"] = pc.sha256_bytes(art_root / "freeze.json")
    with pytest.raises(pc.ProfileInvalid, match="버전 불일치"):
        pc.verify_artifacts(art_root)
    with pytest.raises(pc.ProfileInvalid, match="feature schema"):
        pc.check_features({"cpu_mean": 1.0}, {"kept_feature_names": ["cpu_mean"]})


def test_22c_threshold_value_mismatch_profile_invalid(art_root):
    (art_root / "threshold.json").write_text(json.dumps({"threshold": -0.2}), encoding="utf-8")
    pc.FROZEN_DETECTORS[pc.IF_ID]["artifact"]["threshold_sha256"] = pc.sha256_bytes(art_root / "threshold.json")
    with pytest.raises(pc.ProfileInvalid, match="threshold 불일치"):
        pc.verify_artifacts(art_root)


def test_22d_executor_refuses_to_start_on_profile_invalid(tmp_path, profile, art_root):
    (art_root / "model.pkl").unlink()
    with pytest.raises(pc.ProfileInvalid):
        ex.run_shadow("rid", tmp_path / "e.jsonl", profile_path=profile, artifact_root=art_root)
    assert not (tmp_path / "e.jsonl").exists()
    assert ex.main(["shadow", "--run-id", "r", "--evidence-log", str(tmp_path / "e.jsonl"), "--profile", str(profile),
                    "--artifact-root", str(art_root)]) == ex.EXIT_PROFILE_INVALID


def test_real_external_artifacts_match_frozen_hashes():
    root = os.environ.get("PHASE2_ARTIFACT_ROOT")
    if not root:
        pytest.skip("PHASE2_ARTIFACT_ROOT 없음 - 실제 c3-A pkl 확인은 runbook 단계에서")
    art = pc.verify_artifacts(root)
    assert art["hashes"]["model.pkl"] == "24cec78e658833dd24957ff49a8c39499403db2a7e8a0c7e752aff61ba3668cf"


# ---------------- 25 Stage 2 진입 ----------------
OUTCOME_INPUTS = {f"SELECTED:{pc.CPU_ID}": dict(unn=(0, 2)), f"SELECTED:{pc.IF_ID}": dict(unn=(3, 1)),
                  sel.INCOMPLETE: dict(n_normal=3), sel.NO_ELIGIBLE: dict(lat=(None, None)), sel.INTEGRITY: dict(integrity=["x"])}


def _evidence(d, profile, outcome):
    """선택 함수로 실제 재현 가능한 증거(입력 → 같은 결과)를 만든다."""
    d.mkdir(parents=True, exist_ok=True)
    sp = pc.load_profile(profile, stage="stage1")
    inputs = _inputs(**OUTCOME_INPUTS[outcome])
    ev = {"rule": pc.PREREG_SHA256, "evidence_id": f"p2sel-{pc.obj_sha256(inputs)[:16]}", "inputs": inputs,
          "decided_at_utc": "2026-10-20T00:00:00+00:00", "selection_profile_sha256": sp["sha256"], **sel.select(inputs)}
    assert ev["outcome"] == outcome
    p = d / "ev.json"
    p.write_bytes(pc.canonical(ev))
    return p, ev, sp


def _entry(d, profile, art_root, outcome=f"SELECTED:{pc.CPU_ID}", impl=None, template=TEMPLATE, active_profile=None):
    ep, ev, sp = _evidence(d, profile, outcome)
    ap = active_profile
    if ap is None and outcome.startswith("SELECTED:"):
        ap = _write_profile(d, pc.derive_active_profile(sp["profile"], sp["sha256"], ev, pc.sha256_bytes(ep)), "ap.json")
    return s2.entry_check(selection_profile_path=profile, active_profile_path=ap or profile, evidence_path=ep,
                          artifact_root=art_root, snapshot_fn=lambda: {"template": template},
                          impl=impl or {"selection_profile_sha256": sp["sha256"]})


def test_25_stage2_entry_accepts_mechanical_active_profile(tmp_path, profile, art_root):
    e = _entry(tmp_path / "e", profile, art_root)
    assert e["selected"] == pc.CPU_ID and e["loaded"]["active"] == pc.CPU_ID


@pytest.mark.parametrize("outcome", [sel.INCOMPLETE, sel.NO_ELIGIBLE, sel.INTEGRITY])
def test_25b_stage2_forbidden_unless_selected(tmp_path, profile, art_root, outcome):
    with pytest.raises(SystemExit, match="Stage 2 금지"):
        _entry(tmp_path / "e", profile, art_root, outcome=outcome)


def test_25d_stage2_refuses_tampered_selection_evidence(tmp_path, profile, art_root):
    ep, ev, sp = _evidence(tmp_path / "t", profile, f"SELECTED:{pc.CPU_ID}")
    bad = {**ev, "outcome": f"SELECTED:{pc.IF_ID}"}                      # 결과만 바꾸고 증거·profile 사슬은 다시 만듦
    ep.write_bytes(pc.canonical(bad))
    ap = _write_profile(tmp_path / "t", pc.derive_active_profile(sp["profile"], sp["sha256"], bad, pc.sha256_bytes(ep)), "ap.json")
    with pytest.raises(SystemExit, match="not_reproducible"):
        s2.entry_check(selection_profile_path=profile, active_profile_path=ap, evidence_path=ep, artifact_root=art_root,
                       snapshot_fn=lambda: {"template": TEMPLATE}, impl={"selection_profile_sha256": sp["sha256"]})


def test_25c_stage2_refuses_stale_freeze_mismatch_and_hand_edited_profile(tmp_path, profile, art_root):
    gpu = copy.deepcopy(TEMPLATE)
    gpu["spec"]["containers"][0]["image"] = "vllm-gpu:1"
    with pytest.raises(SystemExit, match="PROFILE_STALE"):
        _entry(tmp_path / "a", profile, art_root, template=gpu)
    with pytest.raises(SystemExit, match="not_implementation_frozen"):
        _entry(tmp_path / "b", profile, art_root, impl={"selection_profile_sha256": "0" * 64})
    ep, ev, sp = _evidence(tmp_path / "c", profile, f"SELECTED:{pc.CPU_ID}")
    hand = pc.derive_active_profile(sp["profile"], sp["sha256"], ev, pc.sha256_bytes(ep))
    hand["environment"]["serving_args"] = hand["environment"]["serving_args"] + ["--x"]
    with pytest.raises(SystemExit, match="mechanical_derivation"):
        _entry(tmp_path / "c", profile, art_root, active_profile=_write_profile(tmp_path / "c", hand, "hand.json"))
    other = pc.derive_active_profile(sp["profile"], sp["sha256"], {**ev, "outcome": f"SELECTED:{pc.IF_ID}"}, pc.sha256_bytes(ep))
    with pytest.raises(SystemExit, match="chain_mismatch|selected"):
        _entry(tmp_path / "c", profile, art_root, active_profile=_write_profile(tmp_path / "c", other, "other.json"))


def test_active_profile_derivation_changes_only_role_and_selection(tmp_path, profile):
    ep, ev, sp = _evidence(tmp_path, profile, f"SELECTED:{pc.IF_ID}")
    a = pc.derive_active_profile(sp["profile"], sp["sha256"], ev, pc.sha256_bytes(ep))
    assert pc.active_profile_diff(sp["profile"], a) == []
    assert {d["detector_id"]: d["role"] for d in a["detectors"]} == {pc.IF_ID: pc.ACTIVE, pc.CPU_ID: pc.SHADOW}
    assert a["selection"]["source_selection_profile_sha256"] == sp["sha256"]
    assert a["selection"]["selection_evidence_sha256"] == pc.sha256_bytes(ep)
    for bad in (sel.NO_ELIGIBLE, sel.INCOMPLETE, sel.INTEGRITY, "SELECTED:new-detector"):
        with pytest.raises(pc.ProfileInvalid):
            pc.derive_active_profile(sp["profile"], sp["sha256"], {**ev, "outcome": bad}, "x")


# ---------------- 26 Stage 1 act 거부 ----------------
def test_26_stage1_rejects_act_at_argument_and_function_level(tmp_path, profile, art_root):
    with pytest.raises(SystemExit) as e:
        s1.main(["--run", "p2n01", "--mode", "act"])
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        s1.main(["--run", "p2n01", "--mode", "record_only", "--act-approval", "x.json"])
    assert e.value.code == 2
    with pytest.raises(SystemExit, match="record_only 전용"):
        s1.run_session("p2n01", deps=None, profile_path=profile, artifact_root=art_root, mode="act", results_dir=tmp_path / "r",
                       impl={}, audit_reader=list)
    assert not (tmp_path / "r").exists()


# ---------------- 27 유효성 기록 없는 세션 재생 거부 ----------------
def test_27_replay_refuses_sessions_without_validity_record(tmp_path, profile, art_root):
    (tmp_path / "p2-session-p2n01.json").write_text(json.dumps({"run": "p2n01", "attempt_number": 1, "kind": "normal"}),
                                                    encoding="utf-8")
    with pytest.raises(SystemExit, match="유효성 기록 없는"):
        sel.collect(tmp_path, profile_path=profile, artifact_root=art_root, verify_freeze=lambda: {})
    assert not (tmp_path / s1.CLOSED_MARKER).exists()                    # 실수로 수집이 닫히지 않음


# ---------------- 29 판정 매핑(§8.4·§13) ----------------
OK = {"success": True, "authority_leak": False}
FAIL = {"success": False, "authority_leak": False}
LEAK = {"success": False, "authority_leak": True}
SEL = f"SELECTED:{pc.CPU_ID}"


@pytest.mark.parametrize("stage1,results,want", [
    (SEL, [OK, OK, OK], "PHASE2_ARCHITECTURE_VALIDATED"),
    (SEL, [OK, FAIL, OK], "PHASE2_MIXED"),
    (SEL, [FAIL, FAIL, FAIL], "PHASE2_NOT_VALIDATED"),
    (SEL, [], "PHASE2_SELECTION_VALIDATED_RECOVERY_PENDING"),
    (SEL, [OK, OK], "PHASE2_SELECTION_VALIDATED_RECOVERY_PENDING"),    # 유효 3 미달, 실패 0
    (SEL, [OK, FAIL], "PHASE2_MIXED"),                                   # 유효 3 미달, 성공·실패 섞임
    (SEL, [FAIL], "PHASE2_NOT_VALIDATED"),                               # 유효 실패 뒤 stale 중단 → PENDING 아님
    (SEL, [OK, OK, LEAK], "PHASE2_NOT_VALIDATED"),                       # 권한 누수 1건이면 다른 성공과 무관
    ("NO_ELIGIBLE_DETECTOR", [], "PHASE2_SELECTION_VALIDATED_RECOVERY_PENDING"),
    ("SELECTION_INCOMPLETE", [], "PHASE2_NOT_VALIDATED"),
    ("INTEGRITY_FAILURE", [], "PHASE2_NOT_VALIDATED"),
])
def test_29_verdict_mapping(stage1, results, want):
    assert s2.phase2_verdict(stage1, results)["verdict"] == want


def test_29b_session_result_failure_and_leak_rules():
    summ = {"watcher": {"action": {"outcome": "promoted_verified"}, "guard_result": {"result": "verified"}},
            "context": {"detector_id": pc.CPU_ID}, "trial_outcome": {"t_injection_observed_utc": "2026-10-20T01:00:00+00:00"}}
    start = {"record_type": "c1_live_start", "signal_identity": {"detector_id": pc.CPU_ID}}

    def send(t, d=pc.CPU_ID):
        return {"record_type": "c1_policy_send_attempt", "detector_id": d, "attempted_at_utc": t}
    assert s2.stage2_session_result(summ, [start, send("2026-10-20T01:02:00+00:00")], pc.CPU_ID)["success"] is True
    r = s2.stage2_session_result(summ, [start, send("2026-10-20T00:59:00+00:00")], pc.CPU_ID)
    assert r["success"] is False and not r["checks"]["first_send_after_injection"]          # 주입 전 첫 송신
    assert s2.stage2_session_result(summ, [start], pc.CPU_ID)["success"] is False           # 신호 없음
    assert s2.stage2_session_result(summ, [start, send("2026-10-20T01:02:00+00:00", pc.IF_ID)], pc.CPU_ID)["authority_leak"]


# ---------------- Stage 1 실행기 연결(실제 guarded 정책 record_only + 가짜 클러스터) ----------------
class Rec:
    """c1 5 s 표본기 자리(클러스터 조회 없음) - start/stop만."""

    def __init__(self, **kw):
        self.kw = kw

    def start(self):
        self.kw["evidence_path"].write_text("", encoding="utf-8")
        alive = {"v": True}
        return tgp.DetectorHandle(is_alive=lambda: alive["v"],
                                  stop=lambda: alive.update(v=False) or {"returncode": 0, "exited_cleanly": True}, label="rec")


def _shadow_factory(monkeypatch, profile_path, root, recs, extra=None):
    def factory(results_dir):
        def start(rid, path, sink_url):
            it = iter(copy.deepcopy(recs))
            monkeypatch.setattr(ex, "evaluate_once", lambda art, **k: next(it))
            ep = gr.live_evidence_path(results_dir, rid)
            ex.run_shadow(rid, ep, profile_path=profile_path, artifact_root=root, max_evals=len(recs), sleep_fn=lambda s: None,
                          monotonic_fn=iter(range(0, 10_000, 15)).__next__)
            if extra:
                with open(ep, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(extra) + "\n")
            alive = {"v": True}
            return tgp.DetectorHandle(is_alive=lambda: alive["v"],
                                      stop=lambda: alive.update(v=False) or {"returncode": 0, "exited_cleanly": True},
                                      label="p2-shadow-test")
        return start
    return factory


def _stage1(tmp_path, monkeypatch, profile, art_root, run, extra=None, **kw):
    import run_c3_collection as rc
    w = tgp.World(tmp_path / run, monkeypatch, mode=gp.MODE_RO, send_at=(), wrap_preview=True)
    w.results = tmp_path / "stage1"
    w.injector.get_stress_delivery_record = lambda: {"planned_cr_names": [], "target": {"name": "vllm-serving-aaa111-x"}}
    deps = w.deps(canonical_snapshot=lambda: {**json.loads(json.dumps(w.canon)), "template": TEMPLATE})
    monkeypatch.setattr(rc, "identity_metadata", lambda inj, p: {"identity_recorder": {"samples_file": p.name}})
    audit = tmp_path / run / "policy" / "audit.jsonl"                 # 실제 guarded 정책 앱의 감사 기록
    kw.setdefault("impl", {"selection_profile_sha256": pc.load_profile(profile, stage="stage1")["sha256"]})
    r = s1.run_session(run, deps=deps, profile_path=profile, artifact_root=art_root, results_dir=w.results,
                       audit_reader=lambda: [json.loads(x) for x in audit.read_text(encoding="utf-8").splitlines() if x.strip()],
                       power_events_fn=lambda a, b: [], sampler_cls=Rec,
                       starter_fn=_shadow_factory(monkeypatch, profile, art_root, [_rec(i, *HOT) for i in range(1, 5)], extra),
                       **kw)
    return w, r


@pytest.mark.parametrize("run", ["p2n01", "p2p01"])
def test_stage1_session_zero_action_zero_signal_and_integrity_ok(tmp_path, monkeypatch, profile, art_root, run):
    import run_guarded_normal_bc as rn
    before = (rn.PLAN_ID, rn.RESULTS_DIR, rn.RUNS, rn.check_schedule)
    w, r = _stage1(tmp_path, monkeypatch, profile, art_root, run)
    s, rec = r["summary"], r["session_record"]
    assert (rn.PLAN_ID, rn.RESULTS_DIR, rn.RUNS, rn.check_schedule) == before
    assert s["outcome_status"] == "completed" and s["cleanup_errors"] == [], s["cleanup_errors"]
    assert w.promotes == [] and w.overrides == [] and w.delay == 30 and w.policy.snapshot()["context"] is None
    assert rec["integrity"] == {"ok": True, "problems": []} and rec["kind"] == s1.kind_of(run)
    assert rec["evidence_sha256"] == pc.sha256_bytes(w.results / rec["evidence_file"])
    assert rec["environment_profile_sha256"] == pc.load_profile(profile, stage="stage1")["sha256"]
    assert not list(w.results.glob("p2-integrity-failure-*"))
    c = json.loads(next(w.results.glob("contract-*.json")).read_text(encoding="utf-8"))
    assert c["contract_doc"] == pc.PREREG_DOC and c["plan_id"] == pc.PLAN_ID and c["mode"] == "record_only"
    if run == "p2p01":
        assert c["kind"] == "pressure" and c["signal_send"] is False and "inject" in w.injector.calls
        assert c["artifact_hashes"] == pc.verify_artifacts(art_root)["hashes"]
        assert {k: c["conditions"][k] for k in s1.pressure_conditions()} == s1.pressure_conditions()


def test_stage1_signal_in_evidence_is_integrity_failure_and_blocks_next(tmp_path, monkeypatch, profile, art_root):
    sha = pc.load_profile(profile, stage="stage1")["sha256"]
    _, r = _stage1(tmp_path, monkeypatch, profile, art_root, "p2p01",
                   extra={"record_type": "c1_policy_send_attempt", "run_id": "x", "evaluation_seq": 3})
    assert "actionable_signal_sent" in r["session_record"]["integrity"]["problems"]
    d = tmp_path / "stage1"
    assert list(d.glob("p2-integrity-failure-p2p01.json"))
    (d / "p2-validity-p2p01.json").write_text(json.dumps({"status": "valid", "valid": True,
                                                          "t_injection_observed_utc": T0.isoformat()}), encoding="utf-8")
    with pytest.raises(SystemExit, match="INTEGRITY_FAILURE"):
        s1.schedule("p2p02", d)
    c = sel.collect(d, profile_path=profile, artifact_root=art_root, verify_freeze=lambda: {"selection_profile_sha256": sha})
    assert sel.select(c["inputs"])["outcome"] == sel.INTEGRITY


def test_stage1_profile_stale_or_invalid_refused_before_cluster_change(tmp_path, monkeypatch, profile, art_root):
    w = tgp.World(tmp_path / "w", monkeypatch, mode=gp.MODE_RO, send_at=(), wrap_preview=True)
    other = copy.deepcopy(TEMPLATE)
    other["spec"]["containers"][0]["args"] = ["--model=Other/Model"]
    d = tmp_path / "stage1"
    impl = {"selection_profile_sha256": pc.load_profile(profile, stage="stage1")["sha256"]}
    with pytest.raises(SystemExit, match="REFUSED_PROFILE_STALE"):
        s1.run_session("p2n01", deps=w.deps(canonical_snapshot=lambda: {"template": other}), profile_path=profile, impl=impl,
                       audit_reader=list,
                       artifact_root=art_root, results_dir=d)
    assert w.injector.calls == [] and json.loads((d / "p2-validity-p2n01.json").read_text(encoding="utf-8"))["valid"] is False
    with pytest.raises(SystemExit, match="refused_profile_stale"):       # 사람 승인 없이 재개 불가
        s1.schedule("p2n02", d)
    (art_root / "scaler.pkl").write_bytes(b"x")
    with pytest.raises(SystemExit, match="REFUSED_PROFILE_INVALID"):
        s1.run_session("p2n02", deps=w.deps(canonical_snapshot=lambda: {"template": TEMPLATE}), profile_path=profile, impl=impl,
                       audit_reader=list,
                       artifact_root=art_root, results_dir=d, resume_ack="p2n01")
    assert w.injector.calls == [] and len(s1.attempts(d)) == 2           # 거부도 시도로 센다


def _attempt(d, run, n, status, **kw):
    validity = kw.pop("validity", {})
    d.mkdir(parents=True, exist_ok=True)
    (d / f"p2-session-{run}.json").write_text(json.dumps({"run": run, "attempt_number": n, "kind": s1.kind_of(run),
                                                          "status": "run", **kw}), encoding="utf-8")
    (d / f"p2-validity-{run}.json").write_text(json.dumps({"status": status, "valid": status in ("normal_valid", "valid"),
                                                           **validity}), encoding="utf-8")


def test_stage1_schedule_stop_rules_and_caps(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match="순서 밖"):
        s1.schedule("p2n02", tmp_path / "a")
    _attempt(tmp_path / "a", "p2n01", 1, "invalid")
    _attempt(tmp_path / "a", "p2n02", 2, "invalid")
    with pytest.raises(SystemExit, match="invalid 2회 연속"):
        s1.schedule("p2n03", tmp_path / "a")
    for i in range(1, 5):
        _attempt(tmp_path / "b", f"p2n0{i}", i, "normal_valid")
    with pytest.raises(SystemExit, match="이미 채움"):
        s1.schedule("p2n05", tmp_path / "b")
    _attempt(tmp_path / "c", "p2p01", 1, "valid")
    _attempt(tmp_path / "c", "p2p02", 2, "invalid")
    monkeypatch.setitem(pc.MAX_ATTEMPTS, "pressure", 2)
    with pytest.raises(SystemExit, match="시도 상한"):
        s1.schedule("p2p03", tmp_path / "c")
    _attempt(tmp_path / "d", "p2n01", 1, "normal_valid", cleanup_errors=["x"])
    with pytest.raises(SystemExit, match="정리 오류"):
        s1.schedule("p2n02", tmp_path / "d")
    _attempt(tmp_path / "e", "p2n01", 1, "unexpected_degradation")
    _attempt(tmp_path / "e", "p2n02", 2, "unexpected_degradation")
    with pytest.raises(SystemExit, match="예상 밖 저하"):
        s1.schedule("p2n03", tmp_path / "e")


def test_selection_end_to_end_from_stage1_records_is_byte_deterministic(tmp_path, monkeypatch, profile, art_root):
    """합성 Stage 1 기록(정상 4·압박 5) → collect → evidence 두 번 = 같은 바이트, Gate 2는 주입 기준 재생."""
    d = tmp_path / "stage1"
    sha = pc.load_profile(profile, stage="stage1")["sha256"]
    base = {"t0_utc": T0.isoformat(), "observation_end_utc": (T0 + timedelta(seconds=4200)).isoformat(),
            "slo": {"episodes": []}, "service_events": [], "infra_events": []}
    plan = [(f"p2n0{i}", [COLD] * 8, {"base": base}) for i in range(1, 5)] + \
           [(f"p2p0{i}", [COLD] * 4 + [HOT] * 6, {"t_injection_observed_utc": (T0 + timedelta(seconds=60)).isoformat()})
            for i in range(1, 6)]
    for n, (run, pairs, extra) in enumerate(plan, 1):
        rid = s1.run_id_for(run)
        d.mkdir(parents=True, exist_ok=True)
        ep = d / f"c1-live-evidence-{rid}.jsonl"
        start = {"record_type": "c1_shadow_start", "run_id": rid, "environment_profile_sha256": sha, "active_detector": None}
        ep.write_text("".join(json.dumps({**r, "run_id": rid}) + "\n" for r in [start, *_evals(pairs)]), encoding="utf-8")
        _attempt(d, run, n, "normal_valid" if run.startswith("p2n") else "valid", run_id=rid, evidence_file=ep.name,
                 evidence_sha256=pc.sha256_bytes(ep), environment_profile_sha256=sha,
                 runner_ended_at_utc=f"2026-10-2{n % 10}T00:00:00+00:00", validity=extra)
    monkeypatch.setattr(sel, "if_scores_for", lambda evals, art: {r["evaluation_seq"]: r["score"] for r in evals
                                                                    if r.get("status") == "scored"})
    a = sel.evidence_from(sel.collect(d, profile_path=profile, artifact_root=art_root, verify_freeze=lambda: {"selection_profile_sha256": sha}))
    b = sel.evidence_from(sel.collect(d, profile_path=profile, artifact_root=art_root, verify_freeze=lambda: {"selection_profile_sha256": sha}))
    assert pc.canonical(a) == pc.canonical(b)
    assert a["outcome"] == f"SELECTED:{pc.CPU_ID}" and a["gate_results"]["tiebreak_simplicity"]["used"] is True
    assert a["gate_results"]["gate2"]["pass_count"] == {pc.CPU_ID: 5, pc.IF_ID: 5}
    assert a["gate_results"]["gate3"]["unnecessary_sum"] == {pc.CPU_ID: 0, pc.IF_ID: 0}
    assert a["selection_profile_sha256"] == sha and a["decided_at_utc"] == "2026-10-29T00:00:00+00:00"
    ep = d / f"c1-live-evidence-{s1.run_id_for('p2p01')}.jsonl"
    ep.write_text(ep.read_text(encoding="utf-8") + "\n", encoding="utf-8")       # 재생 뒤 원자료 변경 → 무결성 실패
    c = sel.collect(d, profile_path=profile, artifact_root=art_root, verify_freeze=lambda: {"selection_profile_sha256": sha})
    assert sel.select(c["inputs"])["outcome"] == sel.INTEGRITY


# ---------------- 저장소 산출물·동결 상태 ----------------
def test_committed_selection_profile_matches_gitops_and_frozen_values():
    p = pc.load_profile(pc.SELECTION_PROFILE, stage="stage1")
    assert p["active"] is None and p["profile"] == json.loads(json.dumps(pc.build_selection_profile(TEMPLATE)))


def test_frozen_constants_match_prereg_and_phase1_sources():
    import c1_bck_live_detector as bd
    text = (pc.ROOT / pc.PREREG_DOC).read_text(encoding="utf-8")
    assert pc.text_sha256(pc.ROOT / pc.PREREG_DOC) == pc.PREREG_SHA256
    assert bd.THRESHOLDS["C"] == pc.CPU_THRESHOLD
    art_dir = pc.AD / "v3" / "model_c3_normal_coverage" / "artifacts"
    assert json.loads((art_dir / "threshold.json").read_text(encoding="utf-8"))["threshold"] == pc.IF_THRESHOLD
    sums = json.loads((art_dir / "SHA256SUMS.json").read_text(encoding="utf-8"))
    a = pc.FROZEN_DETECTORS[pc.IF_ID]["artifact"]
    assert {f: a[k] for f, k in pc.C3A_FILES.items()} == sums
    for v in (a["model_sha256"], a["scaler_sha256"], a["feature_schema_sha256"], a["threshold_sha256"], "363 s", "4개 이상"):
        assert v in text
    assert (pc.N_NORMAL, pc.N_PRESSURE, pc.N_STAGE2, pc.MAX_ATTEMPTS) == (4, 5, 3, {"normal": 6, "pressure": 7, "stage2": 5})
    assert pc.verify_rule_spec()["sha256"]


def test_implementation_freeze_verification_fail_closed(tmp_path):
    req = ("x.py",)
    for rel in (pc.PREREG_DOC, pc.DESIGN_FREEZE):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(pc.ROOT / rel, tmp_path / rel)
    (tmp_path / "x.py").write_text("a = 1\n", encoding="utf-8")

    def manifest(**over):
        m = {"status": "implementation_frozen", "files": {"x.py": pc.text_sha256(tmp_path / "x.py")},
             "design_freeze": {"prereg_sha256": pc.PREREG_SHA256, "manifest_path": pc.DESIGN_FREEZE,
                               "manifest_sha256": pc.text_sha256(tmp_path / pc.DESIGN_FREEZE)},
             "tests": {"phase2_failed": 0, "new_regressions": 0}, "selection_profile_sha256": "s", **over}
        (tmp_path / "m.json").write_text(json.dumps(m), encoding="utf-8")
    manifest()
    assert pc.verify_implementation_freeze("m.json", tmp_path, required=req)["status"] == "verified"
    for over in ({"tests": {"phase2_failed": 1, "new_regressions": 0}}, {"status": "draft"},
                 {"design_freeze": {"prereg_sha256": "0" * 64}}, {"files": {"x.py": "0" * 64}}):
        manifest(**over)
        with pytest.raises(SystemExit, match="fail-closed"):
            pc.verify_implementation_freeze("m.json", tmp_path, required=req)
    manifest()
    (tmp_path / "x.py").write_text("a = 2\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="x.py"):
        pc.verify_implementation_freeze("m.json", tmp_path, required=req)
    with pytest.raises(SystemExit, match="fail-closed"):
        pc.verify_implementation_freeze("missing.json", tmp_path, required=req)



# ---------------- correctness review 반영(H1-H4, M1-M6) ----------------
def test_validity_loader_blinds_detector_results_and_restores(monkeypatch, tmp_path, profile, art_root):
    import guarded_normal_bc_analysis as nab
    _run_exec(monkeypatch, tmp_path, [_rec(i, *HOT) for i in range(1, 5)], ex.run_shadow, profile_path=profile, artifact_root=art_root)
    orig = nab.cspa.load_shadow
    with s1.blinded_n1_loader(nab):
        evals, reasons = nab.cspa.load_shadow(tmp_path / "ev.jsonl", "rid")
    assert nab.cspa.load_shadow is orig and reasons == []
    # N1 analyze_session의 B·C·K 계산 식이 Phase 2 기록에서 깨지지 않고, Phase 2 판정 값은 보이지 않는다
    assert all(set(r["rules"]) == {"B", "C", "K"} and not any(j["condition_met"] for j in r["rules"].values()) for r in evals)


def test_postflight_missing_evidence_is_invalid_not_integrity(tmp_path, profile):
    sha = pc.load_profile(profile, stage="stage1")["sha256"]
    v, inv = s1.postflight({"run_id": "r"}, tmp_path / "none.jsonl", [], profile, sha, decisions=[])
    assert v == [] and set(inv) == {"executor_evidence_missing", "k8s_snapshots_missing"}
    v, inv = s1.postflight({"run_id": "r"}, tmp_path / "none.jsonl", [{"active": "a"}], profile, sha, decisions=None)
    assert v == [] and "policy_audit_unreadable" in inv
    v, _ = s1.postflight({"run_id": "r"}, tmp_path / "none.jsonl", [{"active": "a"}, {"active": "b"}], profile, sha,
                         decisions=[{"event": "decision"}])
    assert "policy_received_signals: 1" in v and any(x.startswith("active_selector_changed") for x in v)


def test_policy_decisions_window():
    audit = [{"event": "decision", "delivery_key": "r:d:s:1", "received_at": "2026-10-20T01:00:05+00:00"},
             {"event": "decision", "delivery_key": "r:d:s:2", "received_at": "2026-10-20T02:00:00+00:00"},
             {"event": "context_set", "received_at": "2026-10-20T01:00:06+00:00"},
             {"event": "promotion_result", "delivery_key": "r:d:s:1", "received_at": "2026-10-20T01:00:05+00:00"}]
    assert s1.policy_decisions(audit, "2026-10-20T01:00:00+00:00", "2026-10-20T01:10:00+00:00") == audit[:1]


def test_stage1_refuses_profile_not_pinned_to_implementation_freeze(tmp_path, monkeypatch, profile, art_root):
    w = tgp.World(tmp_path / "w", monkeypatch, mode=gp.MODE_RO, send_at=(), wrap_preview=True)
    d = tmp_path / "stage1"
    with pytest.raises(SystemExit, match="implementation freeze"):
        s1.run_session("p2n01", deps=w.deps(canonical_snapshot=lambda: {"template": TEMPLATE}), profile_path=profile,
                       artifact_root=art_root, results_dir=d, impl={"selection_profile_sha256": "0" * 64}, audit_reader=list)
    rec = json.loads((d / "p2-session-p2n01.json").read_text(encoding="utf-8"))
    assert w.injector.calls == [] and rec["cause"] == "profile_not_frozen"


def test_collect_refuses_before_collection_finished_and_maps_unresumed_artifact_refusal(tmp_path, profile, art_root):
    sha = pc.load_profile(profile, stage="stage1")["sha256"]
    _attempt(tmp_path / "a", "p2n01", 1, "normal_valid")
    with pytest.raises(SystemExit, match="수집이 끝나지 않음"):
        sel.collect(tmp_path / "a", profile_path=profile, artifact_root=art_root,
                    verify_freeze=lambda: {"selection_profile_sha256": sha})
    b = tmp_path / "b"
    b.mkdir()
    (b / "p2-session-p2n01.json").write_text(json.dumps({"run": "p2n01", "attempt_number": 1, "kind": "normal",
                                                          "status": "refused_profile_invalid", "cause": "artifact"}), encoding="utf-8")
    (b / "p2-validity-p2n01.json").write_text(json.dumps({"status": "not_run", "valid": False}), encoding="utf-8")
    c = sel.collect(b, profile_path=profile, artifact_root=art_root, verify_freeze=lambda: {"selection_profile_sha256": sha})
    assert "unresumed_refusal:p2n01:artifact" in c["inputs"]["integrity_problems"]
    assert sel.select(c["inputs"])["outcome"] == sel.INTEGRITY
    c = sel.collect(b, profile_path=profile, artifact_root=art_root, verify_freeze=lambda: {"selection_profile_sha256": "0" * 64})
    assert "selection_profile_not_implementation_frozen" in c["inputs"]["integrity_problems"]


def test_29c_leak_in_any_stage2_session_is_not_validated_and_missing_context_is_not_leak():
    assert s2.phase2_verdict(SEL, [OK, OK], any_authority_leak=True)["verdict"] == "PHASE2_NOT_VALIDATED"
    summ = {"watcher": {}, "context": None, "trial_outcome": {}}
    r = s2.stage2_session_result(summ, [], pc.CPU_ID)
    assert r["authority_leak"] is False and r["success"] is False and r["checks"]["context_set"] is False
    r = s2.stage2_session_result({**summ, "context": {"detector_id": pc.CPU_ID},
                                  "policy_decisions": [{"detector_id": pc.IF_ID}]}, [], pc.CPU_ID)
    assert r["authority_leak"] is True                                     # 정책이 SHADOW identity 신호를 받음


def test_freeze_required_files_and_executor_progress_hides_results(tmp_path, monkeypatch, profile, art_root, capsys):
    assert "experiments/phase2_detector_selection/p2_executor.py" in pc.FREEZE_REQUIRED
    assert pc.PREREG_DOC in pc.FREEZE_REQUIRED and "recovery-policy/guarded_policy.py" in pc.FREEZE_REQUIRED
    _run_exec(monkeypatch, tmp_path, [_rec(i, *HOT) for i in range(1, 4)], ex.run_shadow, profile_path=profile, artifact_root=art_root)
    out = capsys.readouterr().out
    assert "ANOMALY" not in out and "NORMAL" not in out and "!" not in out


def test_implementation_freeze_requires_listed_files(tmp_path):
    for rel in (pc.PREREG_DOC, pc.DESIGN_FREEZE):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(pc.ROOT / rel, tmp_path / rel)
    m = {"status": "implementation_frozen", "files": {pc.PREREG_DOC: pc.PREREG_SHA256},
         "design_freeze": {"prereg_sha256": pc.PREREG_SHA256, "manifest_path": pc.DESIGN_FREEZE,
                           "manifest_sha256": pc.text_sha256(tmp_path / pc.DESIGN_FREEZE)},
         "tests": {"phase2_failed": 0, "new_regressions": 0}}
    (tmp_path / "m.json").write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(SystemExit, match="required_files_not_frozen"):
        pc.verify_implementation_freeze("m.json", tmp_path)


# ---------------- correctness re-review 반영(M-a, M-b, L-a) ----------------
def test_refusal_cause_is_typed_not_message_based(profile, art_root):
    p = json.loads(profile.read_text(encoding="utf-8"))
    q = copy.deepcopy(p)
    q["detectors"][0]["threshold"] = None                       # 형식 오류(threshold 단어 포함) → artifact 원인 아님
    with pytest.raises(pc.ProfileInvalid) as e:
        pc.validate_profile(q, stage="stage1")
    assert not isinstance(e.value, pc.ArtifactMismatch)
    q = copy.deepcopy(p)
    q["detectors"][1]["artifact"]["model_sha256"] = "0" * 64    # 동결 artifact 해시와 다름 → ArtifactMismatch
    with pytest.raises(pc.ArtifactMismatch):
        pc.validate_profile(q, stage="stage1")
    (art_root / "model.pkl").write_bytes(b"x")
    with pytest.raises(pc.ArtifactMismatch):
        pc.verify_artifacts(art_root)


def test_selection_run_closes_collection_permanently(tmp_path, profile, art_root):
    sha = pc.load_profile(profile, stage="stage1")["sha256"]
    d = tmp_path / "s"
    (d).mkdir()
    (d / "p2-session-p2n01.json").write_text(json.dumps({"run": "p2n01", "attempt_number": 1, "kind": "normal",
                                                          "status": "refused_profile_stale", "cause": "stale"}), encoding="utf-8")
    (d / "p2-validity-p2n01.json").write_text(json.dumps({"status": "not_run", "valid": False}), encoding="utf-8")
    c = sel.collect(d, profile_path=profile, artifact_root=art_root, verify_freeze=lambda: {"selection_profile_sha256": sha})
    assert sel.select(c["inputs"])["outcome"] == sel.INCOMPLETE and (d / s1.CLOSED_MARKER).exists()
    with pytest.raises(SystemExit, match="선택 분석이 이미 실행됨"):   # 결과를 본 뒤 재개(--resume-after-refusal) 불가
        s1.schedule("p2n02", d, resume_ack="p2n01")


def test_stage1_counts_unkeyed_policy_decisions():
    audit = [{"event": "decision", "delivery_key": None, "outcome": "rejected_schema", "received_at": "2026-10-20T01:00:05+00:00"}]
    assert s1.policy_decisions(audit, "2026-10-20T01:00:00+00:00", "2026-10-20T01:10:00+00:00") == []
    assert len(s1.policy_decisions(audit, "2026-10-20T01:00:00+00:00", "2026-10-20T01:10:00+00:00", any_decision=True)) == 1
