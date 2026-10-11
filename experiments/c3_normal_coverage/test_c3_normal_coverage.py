"""c3 오프라인 도구 시험(클러스터 접근 없음). 실행: experiments/c3_normal_coverage 에서 python -m pytest -q -p no:cacheprovider"""
import sys
sys.dont_write_bytecode = True
import json  # noqa: E402
import shutil  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402

import c3_common as cc  # noqa: E402
import c3_session_validity as vd  # noqa: E402
import eval_c3_validation as ev  # noqa: E402
import pressure_c3 as pz  # noqa: E402
import run_c3_collection as rc  # noqa: E402
import train_calibrate_c3 as tcc  # noqa: E402
from replay import replay_detector  # noqa: E402

T0 = datetime(2026, 10, 20, tzinfo=timezone.utc)
FEAT = {n: 0.0 for n in cc.FEATURE_NAMES}
SN = cc.EXP / "results" / "capstone_c1_shadow_pilot"          # 실제 c1 섀도 pilot(읽기 전용 fixture)
SN_RID = "c1_shadow_no_injection-c1sn01-capstone-c1-shadow-pilot-20261005"


@pytest.fixture(autouse=True)
def _allow_tmp_writes(tmp_path, monkeypatch):
    """시험 산출물은 tmp에만 - allowlist에 tmp만 더한다(공식 경로 거부는 그대로)."""
    monkeypatch.setattr(cc, "ALLOWED_WRITE_ROOTS", cc.ALLOWED_WRITE_ROOTS + [tmp_path])


def _sn_inputs():
    s = json.loads((SN / f"summary-{SN_RID}.json").read_text(encoding="utf-8"))
    samples = [json.loads(x) for x in (SN / f"samples-{SN_RID}.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    return (cc.read_evals(SN / f"c1-shadow-evidence-{SN_RID}.jsonl"), samples, SN / f"pod-events-{SN_RID}.json",
            s["active_target"], s["preview_prep"]["created_pod_hash"])


def _rows_with_samples(tmp_path, samples, name="s.jsonl"):
    evals, _, ev, tgt, ph = _sn_inputs()
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(x) for x in samples), encoding="utf-8")
    return evals, cc.official_rows(evals, p, ev, tgt, ph)


def ev_rec(seq, status="scored", t=None, **feat):
    t = t if t is not None else seq * 15.0
    r = {"record_type": "c1_evaluation", "run_id": "R", "evaluation_seq": seq, "status": status,
         "eval_started_at_utc": (T0 + timedelta(seconds=t - 2)).isoformat()}
    if status == "scored":
        r.update(features={**FEAT, **feat}, score_completed_at_utc=(T0 + timedelta(seconds=t)).isoformat())
    return r


class FakeModel:  # decision_function = 첫 특징(cpu_mean) 값
    def decision_function(self, X):
        return np.asarray(X, dtype=float)[:, 0]


class FakeScaler:
    def transform(self, X):
        return np.asarray(X, dtype=float)


SCHEMA = {"original_feature_names": cc.FEATURE_NAMES, "kept_feature_indices": [0, 1, 2, 3],
          "kept_feature_names": cc.FEATURE_NAMES[:4]}


def _rows(vals):
    return [[v] + [0.0] * 7 for v in vals]


# 1 세션 경계에서 연속이 이어지지 않음
def test_session_boundary_resets_streak():
    sep = tcc.calibrate(FakeModel(), FakeScaler(), SCHEMA, {"s1": _rows([-0.5, -0.5]), "s2": _rows([-0.5, 0.1])})
    cat = tcc.calibrate(FakeModel(), FakeScaler(), SCHEMA, {"s": _rows([-0.5, -0.5, -0.5, 0.1])})
    assert sep["threshold"] == 0.1 and cat["threshold"] == -0.5


# 2 평가 번호 빠짐 = 연속 리셋(구간도 끊김)
def test_missing_evaluation_resets_streak():
    gap = [ev_rec(1), ev_rec(2), ev_rec(4)]
    full = [ev_rec(1), ev_rec(2), ev_rec(3)]
    sc = lambda es: {r["evaluation_seq"]: -1.0 for r in es}  # noqa: E731
    assert not any(x["condition_met"] for x in cc.replay(gap, sc(gap), 0.0))
    assert cc.replay(full, sc(full), 0.0)[-1]["condition_met"]
    assert [len(s) for s in cc.row_segments([{"seq": r["evaluation_seq"], "features": r["features"]} for r in gap])] == [2, 1]


# 3 건너뜀 = 연속 0 - 공식 live step_streak과 같은 결과
def test_skipped_evaluation_matches_official_step_streak():
    import c1_shadow_detector as sd
    es = [ev_rec(1), ev_rec(2), ev_rec(3, status="skipped"), ev_rec(4), ev_rec(5), ev_rec(6), ev_rec(7)]
    scores = {r["evaluation_seq"]: -1.0 for r in es if r["status"] == "scored"}
    mine = cc.replay(es, scores, 0.0)
    state, official = {"consecutive": 0, "last_signal": None}, []
    for r in es:
        rec = dict(r, score=scores.get(r["evaluation_seq"]))
        sd.step_streak(rec, state, 0.0, cc.ts(r["score_completed_at_utc"]).timestamp() if r["status"] == "scored" else 0)
        official.append((rec["consecutive_anomalous"], rec["condition_met"]))
    assert [(x["consecutive"], x["condition_met"]) for x in mine] == official
    kept = [{"seq": r["evaluation_seq"], "features": r["features"]} for r in es if r["status"] == "scored"]
    assert [len(s) for s in cc.row_segments(kept)] == [2, 4]          # 학습 구간도 건너뜀에서 끊김


# 4 보정: 결정적이며 조건을 만족하는 가장 민감한 관측값
def test_calibration_deterministic_most_sensitive():
    rng = np.random.default_rng(0)
    segs = {f"k{i}": _rows(rng.normal(0, 1, 40)) for i in range(3)}
    a = tcc.calibrate(FakeModel(), FakeScaler(), SCHEMA, segs)
    b = tcc.calibrate(FakeModel(), FakeScaler(), SCHEMA, segs)
    assert a["threshold"] == b["threshold"]
    allv = sorted({r[0] for rows in segs.values() for r in rows}, reverse=True)
    ok = [c for c in allv if all(replay_detector([r[0] for r in rows], c)["signal_count"] == 0 for rows in segs.values())]
    assert a["threshold"] == ok[0]


def _gap_samples(samples):
    """세션 가운데 5 s 표본 11개(약 55 s)를 뺀 사본 - 그 창은 공식 기대 Pod 판정이 확인 불가로 제외(live는 scored)."""
    return samples[:100] + samples[111:]


def _fixture_collection(tmp_path, gap_for=()):
    """TRAIN·CALIBRATION = 실제 c1sn01 원자료 사본(evidence·5 s 표본·Pod 이벤트) + normal_valid 판정(합성 라벨),
    VALIDATION = 읽히면 JSON 오류가 나는 기록. gap_for의 세션은 가운데가 빈 표본 파일을 쓴다."""
    m = cc.load_manifest()
    _, samples, _, tgt, ph = _sn_inputs()
    for n in (f"c1-shadow-evidence-{SN_RID}.jsonl", f"samples-{SN_RID}.jsonl", f"pod-events-{SN_RID}.json"):
        shutil.copy(SN / n, tmp_path / n)
    (tmp_path / "samples-gap.jsonl").write_text("\n".join(json.dumps(x) for x in _gap_samples(samples)), encoding="utf-8")
    day = {"c3n01": 20, "c3n03": 20, "c3n04": 21, "c3n06": 21, "c3n07": 22, "c3n09": 22}
    for s in m["sessions"]:
        sid = s["id"]
        if s["role"] == "VALIDATION":
            (tmp_path / f"{sid}-validity.json").write_text("{corrupt", encoding="utf-8")
            continue
        (tmp_path / f"{sid}-validity.json").write_text(json.dumps(
            {"status": "normal_valid", "run_id": SN_RID, "evidence_file": f"c1-shadow-evidence-{SN_RID}.jsonl",
             "active_uid": f"uid-{day[sid]}", "t0_utc": f"2026-10-{day[sid]}T01:00:00+00:00",
             "row_selection_identity": {"active_target": tgt, "preview_hash": ph,
                                        "samples_file": "samples-gap.jsonl" if sid in gap_for else f"samples-{SN_RID}.jsonl",
                                        "pod_events_file": f"pod-events-{SN_RID}.json"}}), encoding="utf-8")
    return m


# ---------------- PREREG M-1 (빌더 제외 AND live scored AND interior) ----------------
def test_m1_interior_counts_and_invalidates_calibration_only(tmp_path):
    evals, samples, ev, tgt, ph = _sn_inputs()
    _, b = _rows_with_samples(tmp_path, _gap_samples(samples))
    m1 = cc.m1_events("c3n03", evals, b)
    hit = [e for e in m1["events"] if e["counts_for_m1"]]
    assert m1["m1_count"] == len(hit) > 0                                                         # 1
    e = hit[0]                                                                                    # 5 사건 기록
    assert e["session"] == "c3n03" and e["position"] == "interior" and e["live_scored"] is True
    assert e["exclusion_reason"].startswith("expected_unverified") and e["window_start_utc"] and e["evaluation_seq"]
    m = _fixture_collection(tmp_path, gap_for=("c3n03", "c3n01"))
    cal = tcc.new_session_segments(m, "c3n03", tmp_path, {}, ("TRAIN", "CALIBRATION"))
    assert cal["status"] == "INVALID_CALIBRATION_ATTEMPT" and cal["segments"] is None and cal["builder"]["m1_count"] > 0
    tr = tcc.new_session_segments(m, "c3n01", tmp_path, {}, ("TRAIN", "CALIBRATION"))
    assert tr["status"] == "normal_valid" and tr["builder"]["m1_count"] > 0                        # TRAIN은 기록만


def test_m1_start_and_end_edges_do_not_count(tmp_path):
    evals, samples, ev, tgt, ph = _sn_inputs()
    _, b = _rows_with_samples(tmp_path, samples)
    m1 = cc.m1_events("s", evals, b)
    assert m1["m1_count"] == 0 and any(e["position"] == "start" for e in m1["events"])           # 2
    _, b = _rows_with_samples(tmp_path, samples[: len(samples) // 2], "half.jsonl")
    m1 = cc.m1_events("s", evals, b)
    assert m1["m1_count"] == 0 and any(e["position"] == "end" for e in m1["events"])             # 3


def test_m1_live_skipped_interior_does_not_count(tmp_path):
    evals, samples, ev, tgt, ph = _sn_inputs()
    evals = json.loads(json.dumps(evals))
    k = len(evals) // 2
    for key in ("pod_snapshot", "queries", "freshness", "features", "score", "score_completed_at_utc"):
        evals[k].pop(key, None)
    evals[k]["status"], evals[k]["skip_reason"] = "skipped", "pod_snapshot_failed:x"
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in samples), encoding="utf-8")
    b = cc.official_rows(evals, p, ev, tgt, ph)
    m1 = cc.m1_events("s", evals, b)
    e = next(x for x in m1["events"] if x["evaluation_seq"] == evals[k]["evaluation_seq"])
    assert e["position"] == "interior" and e["live_scored"] is False and not e["counts_for_m1"]   # 4
    assert m1["m1_count"] == 0


def test_invalid_calibration_not_used_and_replacement_keeps_role(tmp_path):
    m = _fixture_collection(tmp_path, gap_for=("c3n03",))
    out = tmp_path / "art"
    tcc.train(collection_dir=tmp_path, out_art=out)
    fz = json.loads((out / "freeze.json").read_text(encoding="utf-8"))
    assert fz["calibration_sessions"]["new"] == ["c3n07"]                                          # 6
    ex = fz["excluded_operational"]["c3n03"]
    assert ex["status"] == "INVALID_CALIBRATION_ATTEMPT" and ex["row_selection"]["m1_events"]
    assert fz["calibration_mid_session_builder_excluded_live_scored_total"] == 0
    v = json.loads((tmp_path / "c3n03-validity.json").read_text(encoding="utf-8"))
    v["status"] = "INVALID_CALIBRATION_ATTEMPT"
    (tmp_path / "c3n03-validity.json").write_text(json.dumps(v), encoding="utf-8")
    rc.record_replacement(m, "c3r01", "c3n03", tmp_path)
    assert cc.session_role(m, "c3r01", cc.load_replacements(m, tmp_path)) == "CALIBRATION"       # 7


def test_spare_can_replace_failed_spare_and_keeps_calibration_slot(tmp_path):
    import run_guarded_normal_bc as rn
    m = cc.load_manifest()
    for sid in ("c3n01", "c3n02", "c3n03"):
        _run(rn, tmp_path, sid)
        _valid(tmp_path, sid)
    _valid(tmp_path, "c3n03", "INVALID_CALIBRATION_ATTEMPT")
    rc.record_replacement(m, "c3r01", "c3n03", tmp_path)
    _run(rn, tmp_path, "c3r01")
    _valid(tmp_path, "c3r01", "INVALID_CALIBRATION_ATTEMPT")
    with pytest.raises(SystemExit, match="이미 대체"):
        rc.record_replacement(m, "c3r02", "c3n03", tmp_path)               # 자리는 현재 차지한 c3r01만 대체
    rc.record_replacement(m, "c3r02", "c3r01", tmp_path)                    # 예비→예비 사슬
    s = rc.schedule(m, "c3r02", tmp_path)
    assert s["role"] == "CALIBRATION" and s["block"] == 1 and s["replaces"] == "c3r01"
    assert cc.slot_of(m, "c3r02", cc.load_replacements(m, tmp_path)) == "c3n03"
    with pytest.raises(SystemExit, match="맞지 않음"):
        rc.record_replacement(m, "c3r03", "c3r04", tmp_path)              # 시도하지 않은 예비는 대상 아님


def test_no_eligible_rows_not_counted_as_used(tmp_path):
    m = _fixture_collection(tmp_path)
    v = json.loads((tmp_path / "c3n07-validity.json").read_text(encoding="utf-8"))
    v["row_selection_identity"]["samples_file"] = None                     # 5 s 기록 없음 → c1 규칙상 행 0개
    (tmp_path / "c3n07-validity.json").write_text(json.dumps(v), encoding="utf-8")
    r = tcc.new_session_segments(m, "c3n07", tmp_path, {}, ("TRAIN", "CALIBRATION"))
    assert r["status"] == "no_eligible_rows" and r["segments"] is None and r["builder"]["m1_count"] == 0
    v["row_selection_identity"]["samples_file"] = "missing-file.jsonl"         # 행 선택 불가 → 학습 중단 대신 제외
    v["evidence_file"] = "missing-evidence.jsonl"
    (tmp_path / "c3n07-validity.json").write_text(json.dumps(v), encoding="utf-8")
    r = tcc.new_session_segments(m, "c3n07", tmp_path, {}, ("TRAIN", "CALIBRATION"))
    assert r["status"] == "row_selection_error" and r["segments"] is None


def test_live_skipped_builder_row_is_removed():
    b = {"rows": [{"seq": 1}, {"seq": 2}, {"seq": 3}], "excluded_rows": []}
    evals = [ev_rec(1), ev_rec(2, status="skipped"), ev_rec(3)]
    out = cc.live_consistent(b, evals)
    assert [x["seq"] for x in out["rows"]] == [1, 3] and out["excluded_rows"][0]["reason"].startswith("live_not_scored")
    assert [len(s) for s in cc.row_segments([{"seq": x["seq"], "features": FEAT} for x in out["rows"]])] == [1, 1]


def test_freeze_manifest_fail_closed_on_any_frozen_file_change(tmp_path):
    """가짜 저장소 루트(tmp)에서 동결 → 검증 통과 → 동결 대상 하나라도 바뀌면 실행 전 SystemExit(실제 파일은 건드리지 않음)."""
    import run_guarded_normal_bc as rn
    for rel in set(rc.PROVENANCE_REQUIRED.values()):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(f"content of {rel}", encoding="utf-8")
    rc.write_freeze_manifest(root=tmp_path)
    m = json.loads((tmp_path / rc.FREEZE_MANIFEST).read_text(encoding="utf-8"))
    assert m["status"] == "frozen" and m["version"] == rc.FREEZE_VERSION and m["created_at_utc"]
    assert set(m["files"]) == set(rc.PROVENANCE_REQUIRED.values())
    freeze = (rc.FREEZE_VERSION, rc.FREEZE_MANIFEST, rc.PROVENANCE_REQUIRED)
    with cc.patched_n1(rn, runs=cc.runs_table(cc.load_manifest()), freeze=freeze):
        assert rn.verify_provenance(root=tmp_path)["verification_status"] == "verified"
        for rel in ("experiments/c3_normal_coverage/train_calibrate_c3.py", "experiments/c1_collection.py",
                    "anomaly-detection/v3/model_v33_c1/artifacts/feature-schema.json"):
            p = tmp_path / rel
            orig = p.read_text(encoding="utf-8")
            p.write_text(orig + " changed", encoding="utf-8")
            with pytest.raises(SystemExit, match="provenance fail-closed"):
                rn.verify_provenance(root=tmp_path)
            p.write_text(orig, encoding="utf-8")
    with pytest.raises(FileExistsError):
        rc.write_freeze_manifest(root=tmp_path)                             # 동결은 1회 - 덮어쓰기 없음


def test_validation_evaluator_uses_live_sequence_not_builder(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("평가 경로는 빌더 행 선택을 쓰지 않아야 함")
    monkeypatch.setattr(cc, "official_rows", boom)
    es = [ev_rec(i, cpu_mean=-1.0) for i in range(1, 5)]                                          # 모두 live scored
    ctx = {"t0": T0, "obs_end": T0 + timedelta(seconds=5000), "episodes": [], "service_events": [], "infra_events": []}
    art = {"model": FakeModel(), "scaler": FakeScaler(), "schema": SCHEMA, "threshold": 0.0}
    out = ev.model_metrics(es, ctx, art, (0.0, 1.0))
    assert out["raw_triggers"] == 1 and out["below_count"] == 4                                   # 8 live 연속 그대로


def test_pressure_evaluator_parity_with_recorded_live_decisions():
    c1 = cc.load_frozen(cc.C1_ART)
    for run in pz.RUNS:
        evp, smp = pz.run_files(run)
        evals = cc.read_evals(evp)
        par = tcc.decision_parity(evals, c1["model"], c1["scaler"], c1["schema"], c1["threshold"], tcc._rec_ab)
        assert par["ok"], (run, par["decision_mismatch_seqs"][:5])                                # 9
        t_inj = cc.ts(json.loads(smp.read_text(encoding="utf-8"))["trial_outcome"]["t_injection_observed_utc"])
        rec = next((cc.ts(r["condition_met_at_utc"]) for r in evals if r.get("condition_met")
                    and cc.ts(r["condition_met_at_utc"]) >= t_inj), None)
        got = pz.pressure_check(evals, t_inj, c1)["first_signal_rel_inj_sec"]
        assert rec is not None and got == round((rec - t_inj).total_seconds(), 3)


# 5 학습은 VALIDATION을 읽지 못함(역할 확인이 파일 열기보다 먼저) + 동결 기록
def test_training_never_reads_validation(tmp_path):
    m = _fixture_collection(tmp_path)
    with pytest.raises(cc.LeakageError):
        tcc.new_session_segments(m, "c3n02", tmp_path, {}, ("TRAIN", "CALIBRATION"))
    out = tmp_path / "art"
    r = tcc.train(collection_dir=tmp_path, out_art=out)
    fz = json.loads((out / "freeze.json").read_text(encoding="utf-8"))
    assert fz["validation_sessions_metadata_only"] == ["c3n02", "c3n05", "c3n08", "c3n10"]
    assert fz["train_sessions"]["new"] == ["c3n01", "c3n04", "c3n06", "c3n09"]
    assert fz["calibration_sessions"]["new"] == ["c3n03", "c3n07"]
    assert fz["feature_schema_sha256"] == cc.C1_SCHEMA_SHA256 and fz["model_params"]["random_state"] == 42
    assert fz["model_params"]["n_estimators"] == 100 and fz["manifest_sha256"] == cc.sha256(cc.MANIFEST)
    assert cc.load_frozen(out)["threshold"] == r["threshold"]
    sel = fz["new_session_row_selection"]["c3n01"]                       # 공식 빌더 규칙: 표본 전 창은 제외됨
    assert sel["c1_complete"] > 0 and sel["excluded_reason_counts"].get("expected_unverified", 0) > 0


# ---------------- 행 선택 = 공식 c1 (실제 c1sn01 원자료로 edge 검사) ----------------
def _reference(evals, samples, ev, tgt, ph):
    import input_capture_analysis as ica
    events = json.loads(ev.read_text(encoding="utf-8"))
    out = {}
    for r in evals:
        o = ica.expected_pods(r["window_start_utc"], r["window_end_utc"], samples, tgt, ph, events)
        if r["status"] == "scored" and o["pods"] is not None and o["pods"] == r["expected_pods"]:
            out[r["evaluation_seq"]] = cc.feature_row(r)
    return out


def _c3(b):
    return {x["seq"]: [x["features"][n] for n in cc.FEATURE_NAMES] for x in b["rows"]}


def test_identity_present_parity_and_start_edge_excluded(tmp_path):
    evals, samples, ev, tgt, ph = _sn_inputs()
    _, b = _rows_with_samples(tmp_path, samples)
    assert _c3(b) == _reference(evals, samples, ev, tgt, ph) and len(b["rows"]) > 0          # B: 표본 뒤 행 허용
    first = cc.ts(samples[0]["t"])
    early = [r["evaluation_seq"] for r in evals if cc.ts(r["window_start_utc"]) < first]
    assert early and not set(early) & set(_c3(b))                                           # A: 표본 전 창 제외


def test_end_edge_identity_missing_parity(tmp_path):
    evals, samples, ev, tgt, ph = _sn_inputs()
    cut = samples[: len(samples) // 2]
    _, b = _rows_with_samples(tmp_path, cut)
    last = cc.ts(cut[-1]["t"])
    late = [r["evaluation_seq"] for r in evals if cc.ts(r["window_end_utc"]) > last]
    assert late and not set(late) & set(_c3(b))                                            # C: 표본 끊긴 뒤 제외
    assert _c3(b) == _reference(evals, cut, ev, tgt, ph)


def test_pod_replacement_parity(tmp_path):
    evals, samples, ev, tgt, ph = _sn_inputs()
    mod = json.loads(json.dumps(samples))
    for s in mod[100:110]:
        s["old_pod"]["uid"] = "replaced-uid"
    _, b = _rows_with_samples(tmp_path, mod)
    assert _c3(b) == _reference(evals, mod, ev, tgt, ph)                                    # D: 교체 창 제외(공식과 같음)
    assert len(_c3(b)) < len(_c3(_rows_with_samples(tmp_path, samples, "o.jsonl")[1]))
    assert b["excluded_reason_counts"].get("expected_unverified", 0) > 0


def test_duplicate_and_out_of_order_samples_parity(tmp_path):
    evals, samples, ev, tgt, ph = _sn_inputs()
    import random
    shuffled = samples + samples[50:60]
    random.Random(0).shuffle(shuffled)
    _, b = _rows_with_samples(tmp_path, shuffled)
    _, b0 = _rows_with_samples(tmp_path, samples, "o.jsonl")
    assert _c3(b) == _reference(evals, shuffled, ev, tgt, ph) == _c3(b0)                   # E: 공식 함수가 정렬, 중복 무해


def test_no_recorder_means_no_rows_like_c1(tmp_path):
    evals, _, ev, tgt, ph = _sn_inputs()
    b = cc.official_rows(evals, None, ev, tgt, ph)
    assert b["rows"] == [] and set(b["excluded_reason_counts"]) <= {"expected_unverified", "evaluation_skipped"}


def test_final_row_set_equal_on_real_evidence():
    import c3_row_invariant as ri
    x = ri.check_file(SN / f"c1-shadow-evidence-{SN_RID}.jsonl")
    assert x["ok"] and x["level1"]["n_mismatch"] == 0 and x["level2"]["row_set_equal"] and x["level2"]["values_equal"]


def test_recorder_is_official_sampler_and_ordered(tmp_path):
    import run_timed_switch_recovery_trial as rt
    made = {}

    class Rec(rt.TimedSwitch):                                  # 공식 TimedSwitch 표본 루프 그대로, 클러스터 함수만 가짜
        def __init__(self, **kw):
            made.update(kw)
            t = iter(range(10_000))
            super().__init__(sample_interval_sec=0.01, pod_state_fn=lambda n: {"uid": "U"}, endpoint_ips_fn=lambda: [],
                             active_selector_fn=lambda: "a", pod_counters_fn=lambda: {},
                             now_fn=lambda: T0 + timedelta(seconds=5 * next(t)), **kw)
    inj = SimpleNamespace(get_stress_delivery_record=lambda: {"target": {"name": "vllm-serving-a-1"}})
    errs = []
    alive = {"v": True}
    base = lambda rid, p, u: SimpleNamespace(is_alive=lambda: alive["v"], label="det",  # noqa: E731
                                             stop=lambda: alive.update(v=False) or {"ok": True})
    h = rc.c3_starter(base, tmp_path, {"injector": inj}, errs, sampler_cls=Rec)("rid")
    import time
    time.sleep(0.1)
    h.stop()
    assert made["switch_at_sec"] is None and made["send_signal"] is False
    assert made["evidence_path"] == tmp_path / "samples-rid.jsonl"
    rows = [json.loads(x) for x in (tmp_path / "samples-rid.jsonl").read_text(encoding="utf-8").splitlines()]
    ts_ = [cc.ts(r["t"]) for r in rows]
    assert len(rows) >= 2 and ts_ == sorted(ts_) and len(set(ts_)) == len(ts_) and not errs
    import run_v1_detector_trial as v1                          # 기본 표본기 = 공식 c1 클래스
    assert issubclass(v1.V1NoInjectionSampler, rt.TimedSwitch)


# ---------------- 쓰기 allowlist ----------------
def test_write_allowlist(tmp_path, monkeypatch):
    for p in (cc.C1_ART / "x.json", cc.EXP / "results" / "capstone_guarded_normal_bc_v1" / "x.json",
              cc.EXP / "results" / "capstone_guarded_ab_v1" / "x.json", cc.EXP / "results" / "capstone_guarded_bck_v1" / "x.json",
              cc.EXP / "results" / "capstone_c1_normal_collection" / "x.json", cc.EXP / "analysis_v1" / "x.json",
              cc.EXP / "results" / "c3_normal_coverage_prereg_20261010" / "x.json",       # 형제 c3 폴더도 거부
              cc.EXP / "results" / "c3_normal_coverage_collection_evil" / "x.json",
              cc.COLLECTION_DIR / ".." / "capstone_guarded_ab_v1" / "x.json",              # 상대 경로 탈출
              cc.HERE / "c3_common.py", cc.ROOT / "README.md"):
        with pytest.raises(PermissionError):
            cc.check_write_path(p)
    cc.check_write_path(cc.COLLECTION_DIR / "x.json")
    cc.check_write_path(cc.TOOLING_DIR / "sub" / "x.json")
    cc.check_write_path(cc.C3_ART / "model.pkl")
    cc.check_write_path(cc.FREEZE_MANIFEST_FILE)
    allowed, outside = tmp_path / "allowed", tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setattr(cc, "ALLOWED_WRITE_ROOTS", [allowed])
    cc.write_new_json(allowed / "ok.json", {})                                                # 허용 루트 쓰기 성공
    with pytest.raises(PermissionError):
        cc.write_new_json(allowed / ".." / "outside" / "x.json", {})
    try:
        (allowed / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("이 환경은 심볼릭 링크 생성 권한 없음")
    with pytest.raises(PermissionError):                                                       # 심볼릭 링크 탈출
        cc.write_new_json(allowed / "link" / "x.json", {})


def test_coverage_gate_blocks_single_pod(tmp_path):
    _fixture_collection(tmp_path)
    for p in tmp_path.glob("*-validity.json"):
        t = p.read_text(encoding="utf-8")
        if t.startswith('{"'):
            v = json.loads(t)
            v["active_uid"] = "same"
            p.write_text(json.dumps(v), encoding="utf-8")
    with pytest.raises(SystemExit, match="coverage"):
        tcc.train(collection_dir=tmp_path, out_art=tmp_path / "art")


def _c3_inputs(**over):
    evals = [ev_rec(i, memory_slope=-5e6) for i in range(1, 11)]
    for e in evals:
        e["pod_snapshot"] = {"active_selector": "a", "preview_selector": "p",
                             "pods": [{"name": "vllm-serving-a-1", "uid": "U1", "hash": "a", "restart_count": 0},
                                      {"name": "vllm-serving-p-1", "uid": "U2", "hash": "p", "restart_count": 0}]}
        e["expected_pods"] = {"vllm-serving-a-1": "U1", "vllm-serving-p-1": "U2"}
    kw = dict(summary={"kind": "no_injection", "run_id": "R", "old_policy_isolation": {"start": {"isolated": True},
                                                                                      "end": {"isolated": True}}},
              contract={"kind": "no_injection", "mode": "record_only", "no_promotion_path": True,
                        "conditions": {"injection": None}},
              evals=evals, snapshots=[_snap("U1"), _snap("U1")], c3rec={"power_events": [], "power_events_error": None}, operator="none",
              obs_start=T0, obs_end=T0 + timedelta(seconds=200))
    kw.update(over)
    return kw


# 6 무효 세션 거부 / 7 음수 memory_slope는 무효 사유가 아님
def test_invalid_session_rejected_and_negative_memory_slope_is_not_a_reason():
    assert vd.c3_reasons(**_c3_inputs()) == []
    assert vd.classify({"status": "normal"}, []) == "normal_valid"
    inj = _c3_inputs(contract={"kind": "no_injection", "mode": "record_only", "no_promotion_path": True,
                               "conditions": {"injection": "cpu"}})
    assert "injection_present_or_unknown" in vd.c3_reasons(**inj)
    assert vd.classify({"status": "normal"}, ["x"]) == "invalid"
    assert vd.classify({"status": "invalid"}, []) == "invalid"
    assert vd.classify({"status": "unexpected_degradation"}, []) == "unexpected_degradation"
    assert vd.classify({"status": "normal", "service_events": [3.0]}, []) == "unexpected_degradation"
    assert vd.c3_reasons(**_c3_inputs(operator="restarted probe"))[-1].startswith("operator_intervention")
    assert any(x.startswith("pc_power_event") for x in vd.c3_reasons(**_c3_inputs(c3rec={"power_events": [{"Id": 42}]})))
    assert any(x.startswith("power_check_failed") for x in
               vd.c3_reasons(**_c3_inputs(c3rec={"power_events": None, "power_events_error": "x"})))
    pol = _c3_inputs()
    pol["summary"]["policy_state_after"] = {"actions": {"a1": {"run_id": "R"}}}
    assert "policy_action_for_this_run" in vd.c3_reasons(**pol)


# 8 Pod UID 변경 / 9 restart 변경
def test_pod_uid_and_restart_change_invalidate():
    kw = _c3_inputs()
    kw["evals"][5]["expected_pods"] = {"vllm-serving-a-1": "U9", "vllm-serving-p-1": "U2"}
    assert "expected_pod_uid_changed" in vd.c3_reasons(**kw)
    kw = _c3_inputs()
    kw["evals"][7]["pod_snapshot"]["pods"][0]["restart_count"] = 1
    assert any(x.startswith("restart_count_changed") for x in vd.c3_reasons(**kw))
    kw = _c3_inputs(snapshots=[{"pods": [{"name": "vllm-serving-a-1", "uid": "U1", "hash": "a", "restarts": 2}]}])
    assert any(x.startswith("restart_count_changed") for x in vd.c3_reasons(**kw))


def test_active_service_change_and_missing_snapshots_invalidate():
    moved = _snap("U1")
    moved["endpoints"] = {"vllm-active": ["10.0.0.9"]}
    assert "active_service_endpoints_changed" in vd.c3_reasons(**_c3_inputs(snapshots=[_snap("U1"), moved]))
    gen = _snap("U1")
    gen["generation"] = 8                       # generation 변화는 기록만(preview 준비로 정상 변화)
    assert vd.c3_reasons(**_c3_inputs(snapshots=[_snap("U1"), gen])) == []
    assert "k8s_snapshots_missing" in vd.c3_reasons(**_c3_inputs(snapshots=[]))


def test_frozen_requires_manifest_match_and_pressure_tie(tmp_path, monkeypatch):
    art = _fake_frozen(tmp_path)
    ev.require_frozen(art)
    other = tmp_path / "m.json"
    other.write_text(cc.MANIFEST.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(SystemExit, match="매니페스트"):
        ev.require_frozen(art, other)
    (tmp_path / "pr.json").write_text(json.dumps({"c3_sha256sums": {}, "n_runs": 5, "c3_signal_kept": 5}), encoding="utf-8")
    with pytest.raises(SystemExit, match="압박 결과"):
        ev.evaluate(c3_art=art, pressure_result=tmp_path / "pr.json", collection_dir=tmp_path, out_dir=tmp_path)


def test_crashed_spare_keeps_role_via_replacement_record(tmp_path):
    m = cc.load_manifest()
    (tmp_path / "c3-replacement-c3r01.json").write_text(json.dumps({"replaces": "c3n02"}), encoding="utf-8")
    assert tcc.load_replacements(m, tmp_path) == {"c3r01": "c3n02"}
    assert tcc.assemble(m, tmp_path)["roles"]["c3r01"] == "VALIDATION"


def test_low_usable_ratio_invalidates():
    kw = _c3_inputs()
    kw["evals"] = kw["evals"][:5] + [ev_rec(i, status="skipped") for i in range(6, 11)]
    assert any(x.startswith("c1_usable_ratio") for x in vd.c3_reasons(**kw))


# 10 역할 매니페스트 고정 / 11 중복 ID 거부
def test_manifest_fixed_and_duplicates_rejected(tmp_path):
    m = cc.load_manifest()
    assert [(s["id"], s["role"], s["block"]) for s in m["sessions"]] == [
        ("c3n01", "TRAIN", 1), ("c3n02", "VALIDATION", 1), ("c3n03", "CALIBRATION", 1), ("c3n04", "TRAIN", 2),
        ("c3n05", "VALIDATION", 2), ("c3n06", "TRAIN", 2), ("c3n07", "CALIBRATION", 3), ("c3n08", "VALIDATION", 3),
        ("c3n09", "TRAIN", 3), ("c3n10", "VALIDATION", 3)]
    p = tmp_path / "m.json"
    bad = json.loads(cc.MANIFEST.read_text(encoding="utf-8"))
    bad["spares"].append("c3n01")
    p.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="중복"):
        cc.load_manifest(p)
    bad = json.loads(cc.MANIFEST.read_text(encoding="utf-8"))
    bad["sessions"][1]["role"] = "TRAIN"
    p.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="역할 수"):
        cc.load_manifest(p)


# 12 schema 해시 불일치 거부
def test_wrong_schema_hash_rejected(monkeypatch):
    assert cc.load_c1_schema()["kept_feature_names"] == ["cpu_mean", "cpu_slope", "memory_mean", "memory_slope"]
    monkeypatch.setattr(cc, "C1_SCHEMA_SHA256", "0" * 64)
    with pytest.raises(RuntimeError, match="fail-closed"):
        cc.load_c1_schema()


# 13 c1 재현 관문(공식 자료 읽기만, 결과는 tmp)
def test_c1_reproduction_gate_passes(tmp_path):
    r = tcc.reproduce_c1(tmp_path)
    assert r["verdict"] == "C1_REPRODUCTION_GATE_PASS", {k: v["ok"] for k, v in r["gate"].items()}


# 14 비교 연산자 = c1(엄격 미만), 오프라인 replay_detector와 같은 신호
def test_threshold_comparator_matches_c1():
    es = [ev_rec(i) for i in range(1, 5)]
    assert not any(x["is_anomalous"] for x in cc.replay(es, {i: 0.0 for i in range(1, 5)}, 0.0))
    vals = list(np.random.default_rng(1).normal(0, 1, 200))
    es = [ev_rec(i + 1) for i in range(200)]
    mine = [i for i, x in enumerate(cc.replay(es, {i + 1: v for i, v in enumerate(vals)}, -0.3)) if x["condition_met"]]
    assert mine == replay_detector(vals, -0.3)["signal_indices"]


def _fake_frozen(tmp_path):
    art = tmp_path / "c3art"
    shutil.copytree(cc.C1_ART, art)
    (art / "SHA256SUMS.json").unlink()
    (art / "freeze.json").write_text(json.dumps({"status": "frozen", "validation_sessions_metadata_only": [],
                                                 "manifest_sha256": cc.sha256(cc.MANIFEST)}), encoding="utf-8")
    sums = {q.name: cc.sha256(q) for q in art.iterdir()}
    (art / "SHA256SUMS.json").write_text(json.dumps(sums), encoding="utf-8")
    return art


# 15 압박 평가는 t_injection_observed_utc 기준, 동결 전 거부
def test_pressure_uses_observed_injection_time(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match="동결"):
        pz.evaluate(c3_art=tmp_path / "none", out=tmp_path / "o.json")
    art = _fake_frozen(tmp_path)
    ab = tmp_path / "ab"
    ab.mkdir()
    monkeypatch.setattr(pz, "AB_DIR", ab)
    monkeypatch.setattr(pz, "RUNS", ("ab01a",))
    evp, smp = pz.run_files("ab01a")
    hot = dict(cpu_mean=1.6, memory_mean=7.2e9, memory_slope=-8.5e6)   # c1이 크게 반응한 형태(forensic nb03 시작)
    evp.write_text("\n".join(json.dumps(ev_rec(i, **hot)) for i in range(1, 30)), encoding="utf-8")
    smp.write_text(json.dumps({"trial_outcome": {"t_injection_requested_utc": T0.isoformat(),
                                                 "t_injection_observed_utc": (T0 + timedelta(seconds=200)).isoformat()}}),
                   encoding="utf-8")
    r = pz.evaluate(c3_art=art, out=tmp_path / "pr.json")
    run = r["runs"]["ab01a"]
    assert run["t_injection_observed_utc"] == (T0 + timedelta(seconds=200)).isoformat()
    assert 0 <= run["c3"]["first_signal_rel_inj_sec"] <= pz.LIMIT_SEC and run["c3"]["kept"]
    assert run["c3"]["streak_includes_pre_injection"] is True


def test_decide_table():
    m = lambda t, u, md=1, mb=0: {"t_safe_sec": t, "unnecessary": u, "memdec_evals": md, "memdec_below": mb}  # noqa: E731
    good = [{"c1": m(1000, 3, 2, 2), "c3": m(3600, 0, 2, 0)} for _ in range(4)]
    assert ev.decide(good, pressure_ok=True, calibration_failed=False)["verdict"] == "PROMISING"
    assert ev.decide(good, pressure_ok=True, calibration_failed=True)["verdict"] == "NOT PROMISING"
    assert ev.decide(good[:2], pressure_ok=True, calibration_failed=False)["verdict"] == "INCONCLUSIVE"
    zero = [{"c1": m(3600, 0), "c3": m(3600, 0)} for _ in range(4)]
    assert ev.decide(zero, pressure_ok=True, calibration_failed=False)["verdict"] == "INCONCLUSIVE"
    nod = [{"c1": m(1000, 3, 0, 0), "c3": m(3600, 0, 0, 0)} for _ in range(4)]
    assert ev.decide(nod, pressure_ok=True, calibration_failed=False)["verdict"] == "MIXED"
    a_short = [{"c1": m(1000, 3, 2, 2), "c3": m(3600, 0, 2, 0)} for _ in range(2)] + \
              [{"c1": m(3000, 3, 2, 2), "c3": m(2900, 1, 2, 0)} for _ in range(2)]
    assert ev.decide(a_short, pressure_ok=True, calibration_failed=False)["verdict"] == "MIXED"
    assert ev.decide(good, pressure_ok=False, calibration_failed=False)["verdict"] == "NOT PROMISING"
    worse_d = [{"c1": m(1000, 3, 2, 0), "c3": m(3600, 0, 2, 1)} for _ in range(4)]
    assert ev.decide(worse_d, pressure_ok=True, calibration_failed=False)["verdict"] == "NOT PROMISING"
    severe = good[:3] + [{"c1": m(3600, 0, 2, 2), "c3": m(2000, 5, 2, 0)}]
    assert ev.decide(severe, pressure_ok=True, calibration_failed=False)["criteria"]["C"] is False


def test_eval_refuses_before_freeze(tmp_path):
    with pytest.raises(SystemExit, match="동결"):
        ev.evaluate(c3_art=tmp_path / "none", pressure_result=tmp_path / "p.json", collection_dir=tmp_path,
                    out_dir=tmp_path)


# 16 공식 폴더에는 쓸 수 없음
def test_no_write_into_official_dirs(tmp_path):
    for p in (cc.C1_ART / "x.json", cc.EXP / "results" / "capstone_guarded_normal_bc_v1" / "x.json",
              cc.EXP / "results" / "capstone_guarded_ab_v1" / "x.json", cc.EXP / "analysis_v1" / "x.json"):
        with pytest.raises(PermissionError):
            cc.write_new_json(p, {})
    cc.check_write_path(cc.EXP / "results" / "c3_normal_coverage_collection" / "x.json")
    cc.write_new_json(tmp_path / "a.json", {})
    with pytest.raises(FileExistsError):
        cc.write_new_json(tmp_path / "a.json", {})


# ---------------- 수집 실행기(가짜 의존성, 클러스터 없음) ----------------
def _snap(uid):
    return {"pods": [{"name": "vllm-serving-a-1", "uid": uid, "hash": "a", "restarts": 0}], "generation": 7,
            "svc_selectors": {"vllm-active": {"h": "a"}}, "endpoints": {"vllm-active": ["10.0.0.1"]}, "template": {"big": 1}}


def _fake_run_normal(rn, uid):
    def run_normal(run, *, deps, preflight_only, results_dir, code_provenance, starter=None):
        assert rn.PLAN_ID == cc.PLAN_ID and rn.CONTRACT_DOC == cc.PREREG_DOC and rn.RESULTS_DIR == results_dir
        assert rn.observation_sec_for(run) == 4200.0 and cc.PLAN_ID in rn.run_id_for(run)
        assert rn.check_schedule(run, results_dir)["session"] == run
        deps.canonical_snapshot()
        pf = results_dir / f"preflight-{run}.json"
        pf.write_text(json.dumps({"canonical_start": {"start_pod": ["vllm-serving-a-1", uid]}}), encoding="utf-8")
        return {"preflight_file": pf.name, "outcome_status": "completed", "cleanup_errors": []}
    return run_normal


def _run(rn, tmp_path, sid, uid="U1"):
    deps = SimpleNamespace(canonical_snapshot=lambda: _snap(uid))
    return rc.run_c3(sid, deps=deps, rn=rn, results_dir=tmp_path, power_events_fn=lambda a, b: [],
                     run_normal_fn=_fake_run_normal(rn, uid))


def _valid(tmp_path, sid, status="normal_valid"):
    (tmp_path / f"{sid}-validity.json").write_text(json.dumps({"status": status}), encoding="utf-8")


def test_runner_patches_and_restores_n1_and_enforces_order(tmp_path):
    import run_guarded_normal_bc as rn
    before = (rn.PLAN_ID, rn.RESULTS_DIR, rn.RUNS, rn.check_schedule, rn.FREEZE_MANIFEST)
    with pytest.raises(SystemExit, match="순서 밖"):
        _run(rn, tmp_path, "c3n02")
    r = _run(rn, tmp_path, "c3n01")
    assert (rn.PLAN_ID, rn.RESULTS_DIR, rn.RUNS, rn.check_schedule, rn.FREEZE_MANIFEST) == before
    assert r["c3_record"]["role"] == "TRAIN" and r["c3_record"]["start_active_uid"] == "U1"
    snap0 = json.loads((tmp_path / "c3-k8s-snapshots-c3n01.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert snap0["generation"] == 7 and "template" not in snap0
    with pytest.raises(SystemExit, match="재사용"):
        _run(rn, tmp_path, "c3n01")
    with pytest.raises(SystemExit, match="유효성 판정이 없음"):
        _run(rn, tmp_path, "c3n02")
    _valid(tmp_path, "c3n01")
    for sid in ("c3n02", "c3n03"):
        _run(rn, tmp_path, sid)
        _valid(tmp_path, sid)
    with pytest.raises(SystemExit, match="블록 경계"):   # c3n04 = 블록 2 첫 세션, active UID 그대로
        _run(rn, tmp_path, "c3n04")
    r = _run(rn, tmp_path, "c3n04", uid="U2")
    assert r["c3_record"]["block_boundary"]["checked"] and r["c3_record"]["attempt_number"] == 4


def test_patch_restored_on_exception():
    import run_guarded_normal_bc as rn
    before = rn.PLAN_ID
    with pytest.raises(RuntimeError):
        with cc.patched_n1(rn, runs=cc.runs_table(cc.load_manifest())):
            assert rn.PLAN_ID == cc.PLAN_ID
            raise RuntimeError("boom")
    assert rn.PLAN_ID == before


def test_spare_requires_replacement_record_and_stop_rules(tmp_path):
    import run_guarded_normal_bc as rn
    m = cc.load_manifest()
    _run(rn, tmp_path, "c3n01")
    _valid(tmp_path, "c3n01")
    with pytest.raises(SystemExit, match="대체 기록"):
        rc.schedule(m, "c3r01", tmp_path)
    with pytest.raises(SystemExit, match="대체 대상 아님"):
        rc.record_replacement(m, "c3r01", "c3n01", tmp_path)
    _valid(tmp_path, "c3n01", "invalid")
    rc.record_replacement(m, "c3r01", "c3n01", tmp_path)
    with pytest.raises(SystemExit, match="이미 대체"):
        rc.record_replacement(m, "c3r02", "c3n01", tmp_path)
    s = rc.schedule(m, "c3r01", tmp_path)
    assert s["role"] == "TRAIN" and s["replaces"] == "c3n01"
    _run(rn, tmp_path, "c3r01")
    _valid(tmp_path, "c3r01", "invalid")
    with pytest.raises(SystemExit, match="invalid 2회 연속"):
        rc.schedule(m, "c3n02", tmp_path)
