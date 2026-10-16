"""phase2_network_env 단위 테스트(2026-10-17, DEVELOPMENT). 클러스터 접근 없음. 기존 phase2_detector_selection은 import만 한다.

핵심: (1) 후보 2개일 때 일반화 선택 함수가 Phase 2 selection.select와 같은 결과를 낸다(무작위 대조),
      (2) 후보 3개 규칙(Gate 3 best-anchored, 시각, 단순성 CPU > net_probe > IF), (3) net_probe 판정, (4) 프로필·복원 검사."""
import copy
import json
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import n2_common as nc  # noqa: E402
import n2_selection as ns  # noqa: E402
import selection as p2sel  # noqa: E402  (Phase 2 동결 선택 함수 - 대조용)

CPU, NET, IF = nc.CPU_ID, nc.NET_ID, nc.IF_ID
FAKE = "f" * 64


def inputs(normal_unn, pressure_first, *, n_normal=4, n_pressure=5):
    """normal_unn: {det: [4개 값]}, pressure_first: {det: [5개 값 또는 None]}."""
    dets = list(normal_unn)
    return {"integrity_problems": [], "gate1": {d: [] for d in dets},
            "normal": [{"session": f"n{i}", "attempt": i, "unnecessary": {d: normal_unn[d][i] for d in dets}} for i in range(n_normal)],
            "pressure": [{"session": f"p{i}", "attempt": i, "first_streak_sec": {d: pressure_first[d][i] for d in dets}}
                         for i in range(n_pressure)]}


def sel(normal_unn, pressure_first):
    return ns.select(inputs(normal_unn, pressure_first))["outcome"]


# ---------- (1) Phase 2와 대조 ----------
def test_two_candidate_equivalence_with_phase2_random():
    rng = random.Random(20261017)
    times = [None, 90.0, 110.0, 150.0, 200.0, 363.0, 363.5, 420.0]
    for _ in range(600):
        nu = {d: [rng.choice([0, 0, 1, 2, 3]) for _ in range(4)] for d in (CPU, IF)}
        pf = {d: [rng.choice(times) for _ in range(5)] for d in (CPU, IF)}
        inp = inputs(nu, pf)
        a = ns.select(inp, detectors=(CPU, IF), simplicity_order=(CPU, IF))
        b = p2sel.select(inp)
        assert a["outcome"] == b["outcome"], (nu, pf, a["outcome"], b["outcome"])
        assert a["gate_results"]["gate2"]["pass_count"] == b["gate_results"]["gate2"]["pass_count"]


def test_phase2_reported_result_reproduced():
    """Phase 2 최종 결과(CPU 5/5, IF 0/5, 정상 불필요 CPU 1·IF 0) → SELECTED:cpu-threshold-c."""
    nu = {CPU: [0, 0, 0, 1], IF: [0, 0, 0, 0]}
    pf = {CPU: [111.4, 109.6, 106.8, 97.9, 112.2], IF: [408.5, 427.9, 422.8, 428.9, None]}
    assert ns.select(inputs(nu, pf), detectors=(CPU, IF), simplicity_order=(CPU, IF))["outcome"] == f"SELECTED:{CPU}"


# ---------- (2) 후보 3개 ----------
FAST = [58.0] * 5
SLOW_ELIG = [100.0] * 5
FAIL = [None] * 5


def test_single_eligible_selected():
    z = [0, 0, 0, 0]
    assert sel({CPU: z, NET: z, IF: z}, {CPU: FAIL, NET: FAST, IF: FAIL}) == f"SELECTED:{NET}"
    assert sel({CPU: z, NET: z, IF: z}, {CPU: SLOW_ELIG, NET: FAIL, IF: FAIL}) == f"SELECTED:{CPU}"


def test_no_eligible():
    z = [0, 0, 0, 0]
    assert sel({CPU: z, NET: z, IF: z}, {CPU: FAIL, NET: FAIL, IF: FAIL}) == ns.NO_ELIGIBLE


def test_gate2_needs_4_of_5():
    z = [0, 0, 0, 0]
    four = [58.0, 58.0, 58.0, 58.0, None]
    three = [58.0, 58.0, 58.0, None, None]
    assert sel({CPU: z, NET: z, IF: z}, {CPU: FAIL, NET: four, IF: FAIL}) == f"SELECTED:{NET}"
    assert sel({CPU: z, NET: z, IF: z}, {CPU: FAIL, NET: three, IF: FAIL}) == ns.NO_ELIGIBLE


def test_gate3_difference_two_or_more_loses():
    both = {NET: [100.0] * 5, IF: [60.0] * 5, CPU: FAIL}      # IF가 시각은 빠르지만 앞 단계(Gate 3)가 우선
    assert sel({CPU: [0] * 4, NET: [0, 0, 0, 0], IF: [2, 2, 1, 0]}, both) == f"SELECTED:{NET}"


def test_gate3_difference_one_is_equivalent():
    both = {NET: [100.0] * 5, IF: [60.0] * 5, CPU: FAIL}
    assert sel({CPU: [0] * 4, NET: [0, 0, 0, 0], IF: [1, 0, 0, 0]}, both) == f"SELECTED:{IF}"      # 동등 → 시각 40 s 차이 → IF


def test_best_anchored_case():
    """합계 CPU 0 · NET 1 · IF 2: IF는 best(0)보다 2 많아 탈락, CPU·NET은 동등."""
    pf = {CPU: [100.0] * 5, NET: [100.0] * 5, IF: [100.0] * 5}
    out = ns.select(inputs({CPU: [0, 0, 0, 0], NET: [1, 0, 0, 0], IF: [1, 1, 0, 0]}, pf))
    assert out["gate_results"]["gate3"]["retained"] == [CPU, NET]
    assert out["outcome"] == f"SELECTED:{CPU}"       # 시각 동등 → 단순성 CPU > net_probe


def test_timing_tiebreak_picks_faster_when_30s_or_more():
    nu = {CPU: [0, 0, 0, 1], NET: [0, 0, 0, 0], IF: [0, 0, 0, 0]}
    pf = {CPU: [110.0] * 5, NET: [58.0] * 5, IF: FAIL}
    out = ns.select(inputs(nu, pf))
    assert out["outcome"] == f"SELECTED:{NET}" and out["gate_results"]["tiebreak_timing"]["result"] == NET


def test_timing_within_30s_falls_to_simplicity_order():
    nu = {CPU: [0, 0, 0, 1], NET: [0, 0, 0, 0], IF: [0, 0, 0, 0]}
    pf = {CPU: [100.0] * 5, NET: [120.0] * 5, IF: FAIL}       # 20 s 차이 → 동등
    out = ns.select(inputs(nu, pf))
    assert out["outcome"] == f"SELECTED:{CPU}" and out["gate_results"]["tiebreak_simplicity"]["used"] is True


def test_simplicity_order_net_before_if():
    nu = {CPU: [0] * 4, NET: [0] * 4, IF: [0] * 4}
    pf = {CPU: FAIL, NET: [100.0] * 5, IF: [100.0] * 5}
    assert ns.select(inputs(nu, pf))["outcome"] == f"SELECTED:{NET}"


def test_gate2_failure_not_compensated_by_quieter_gate3():
    nu = {CPU: [5, 5, 5, 5], NET: [9, 9, 9, 9], IF: [0, 0, 0, 0]}     # IF가 가장 조용하지만 Gate 2 탈락 → 선택되지 않음
    pf = {CPU: [100.0] * 5, NET: [100.0] * 5, IF: FAIL}
    out = ns.select(inputs(nu, pf))
    assert out["outcome"] == f"SELECTED:{CPU}" and out["gate_results"]["gate3"]["unnecessary_sum"][IF] == 0   # 보고는 하되 선택엔 안 씀


def test_incomplete_and_integrity():
    z = [0, 0, 0, 0]
    assert ns.select(inputs({CPU: z, NET: z, IF: z}, {CPU: FAIL, NET: FAST, IF: FAIL}, n_normal=3))["outcome"] == ns.INCOMPLETE
    inp = inputs({CPU: z, NET: z, IF: z}, {CPU: FAIL, NET: FAST, IF: FAIL})
    inp["gate1"][NET] = ["artifact_hash_mismatch"]
    assert ns.select(inp)["outcome"] == ns.INTEGRITY


def test_deterministic_and_order_independent():
    nu = {CPU: [0, 0, 0, 1], NET: [0, 0, 0, 0], IF: [3, 0, 0, 0]}
    pf = {CPU: [111.0, 109.0, 107.0, 98.0, 112.0], NET: [58.0, 57.0, 59.0, 60.0, 58.0], IF: [None] * 5}
    inp = inputs(nu, pf)
    base = ns.select(inp)
    rng = random.Random(1)
    for _ in range(10):
        sh = copy.deepcopy(inp)
        rng.shuffle(sh["normal"])
        rng.shuffle(sh["pressure"])
        assert ns.select(sh) == base


def test_gate3_missing_metric_is_integrity():
    nu = {CPU: [0, 0, 0, 0], NET: [0, 0, 0, None], IF: [0, 0, 0, 0]}
    pf = {CPU: [100.0] * 5, NET: [100.0] * 5, IF: FAIL}
    assert ns.select(inputs(nu, pf))["outcome"] == ns.INTEGRITY


# ---------- (3) net_probe 판정 ----------
def rec(v, ok=True, t="2026-10-17T10:00:00+00:00"):
    return {"status": "scored" if ok else "skipped", "net_probe": {"value": v}, "score_completed_at_utc": t}


def test_net_judge_streak_needs_three_and_strict_threshold():
    st = {"consecutive": 0, "last_signal": None}
    thr = nc.NET_THRESHOLD
    out = [nc.judge(NET, rec(thr + 0.001), None, st, 100.0 + 15 * i) for i in range(3)]
    assert [o["condition_met"] for o in out] == [False, False, True]
    assert nc.judge(NET, rec(thr), None, {"consecutive": 0, "last_signal": None}, 0.0)["is_anomalous"] is False   # 경계값은 정상(엄격 >)


def test_net_judge_unknown_resets_streak():
    st = {"consecutive": 0, "last_signal": None}
    for i in range(2):
        nc.judge(NET, rec(1.0), None, st, 15.0 * i)
    assert nc.judge(NET, rec(None), None, st, 30.0)["state"] == nc.UNKNOWN and st["consecutive"] == 0
    assert nc.judge(NET, rec(1.0, ok=False), None, st, 45.0)["state"] == nc.UNKNOWN
    for bad in (math.nan, math.inf, True, "x"):
        assert nc.judge(NET, rec(bad), None, st, 60.0)["state"] == nc.UNKNOWN


def test_net_cooldown_60s():
    st = {"consecutive": 0, "last_signal": None}
    met = [nc.judge(NET, rec(1.0), None, st, 15.0 * i)["condition_met"] for i in range(12)]
    idx = [i for i, m in enumerate(met) if m]
    assert idx[0] == 2 and all(b - a >= 4 for a, b in zip(idx, idx[1:]))        # 신호 사이 ≥ 60 s(15 s × 4)


def test_cpu_and_if_use_frozen_judge_unchanged():
    r = {"status": "scored", "features": {"cpu_mean": 2.5}, "score_completed_at_utc": "2026-10-17T10:00:00+00:00"}
    a = nc.judge(CPU, r, None, {"consecutive": 0, "last_signal": None}, 1.0)
    b = nc.pc.judge(CPU, r, None, {"consecutive": 0, "last_signal": None}, 1.0)
    assert a == b
    assert nc.judge(IF, r, -0.5, {"consecutive": 0, "last_signal": None}, 1.0)["state"] == nc.ANOMALY


def _evals(vals, t0):
    out = []
    for i, v in enumerate(vals):
        t = t0 + timedelta(seconds=15 * i)
        out.append({"evaluation_seq": i + 1, "status": "scored", "net_probe": {"value": v},
                    "window_start_utc": (t - timedelta(seconds=60)).isoformat(), "window_end_utc": t.isoformat(),
                    "score_completed_at_utc": t.isoformat()})
    return out


def test_replay_matches_live_judge_for_net():
    """오프라인 재생(Gate 2·3 입력)과 live 판정이 같은 평가에서 신호를 낸다."""
    t0 = datetime(2026, 10, 17, 10, 0, 0, tzinfo=timezone.utc)
    evals = _evals([0.001, 0.001, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0.001, 0.001, 1.0, 1.0, 1.0], t0)
    replayed = [x["condition_met"] for x in ns.replay_detector(NET, evals, {})]
    st, live = {"consecutive": 0, "last_signal": None}, []
    for i, e in enumerate(evals):
        live.append(nc.judge(NET, e, None, st, 15.0 * i)["condition_met"])
    assert replayed == live and any(live)


def test_first_streak_after_injection_excludes_pre_injection_windows():
    t0 = datetime(2026, 10, 17, 10, 0, 0, tzinfo=timezone.utc)
    t_inj = t0 + timedelta(seconds=300)
    evals = _evals([1.0 if i >= 14 else 0.001 for i in range(40)], t0)     # i=14(t0+210 s, 주입 전)부터 이상
    got = ns.first_streak_after_injection(NET, evals, t_inj, {})
    assert got == pytest.approx(90.0)       # 창 전체가 주입 뒤인 첫 평가 t0+360 s부터 새로 세어 3연속째 t0+390 s


# ---------- (4) 프로필·복원·고정값 ----------
def _env():
    return {"serving_backend": {"image": "vllm-cpu-env:latest", "image_pull_policy": "Never"}, "model": "Qwen/Qwen2.5-0.5B-Instruct",
            "compute_type": "CPU", "resources": {"limits": {"cpu": "3", "memory": "6Gi"}}, "serving_args": ["--port=8000"],
            "workload_contract": "a" * 64, "slo_contract": "b" * 64, "telemetry_contract": "c" * 64}


def make_profile():
    return nc.build_selection_profile(_env(), "d" * 64, FAKE)


def test_profile_builds_and_validates_stage1():
    p = make_profile()
    assert [d["detector_id"] for d in p["detectors"]] == list(nc.DETECTOR_IDS) and all(d["role"] == nc.SHADOW for d in p["detectors"])
    nc.validate_profile(p, stage="stage1", prereg_sha256=FAKE)
    with pytest.raises(nc.ProfileInvalid):
        nc.validate_profile(p, stage="stage2", prereg_sha256=FAKE)          # ACTIVE 0개는 Stage 2 거부


def test_unfrozen_prereg_is_refused():
    assert nc.PREREG_SHA256 is None
    with pytest.raises(nc.ProfileInvalid, match="동결되지 않음"):
        nc.validate_profile(make_profile(), stage="stage1")


def test_profile_rejects_changed_threshold_or_artifact():
    p = make_profile()
    for mut in (lambda q: q["detectors"][1].__setitem__("threshold", 0.01),
                lambda q: q["detectors"][1]["artifact"].__setitem__("definition_sha256", "0" * 64),
                lambda q: q["detectors"][0].__setitem__("threshold", 2.7)):
        q = copy.deepcopy(p)
        mut(q)
        with pytest.raises(nc.ArtifactMismatch):
            nc.validate_profile(q, stage="stage1", prereg_sha256=FAKE)


def test_profile_rejects_wrong_probe_profile_pressure_rule_and_abs_path():
    p = make_profile()
    q = copy.deepcopy(p)
    q["environment"]["probe_profile"]["readiness_timeout_sec"] = 1
    with pytest.raises(nc.ProfileInvalid):
        nc.validate_profile(q, stage="stage1", prereg_sha256=FAKE)
    q = copy.deepcopy(p)
    q["environment"]["pressure"]["stages"][0]["latency"] = "100ms"
    with pytest.raises(nc.ProfileInvalid):
        nc.validate_profile(q, stage="stage1", prereg_sha256=FAKE)
    q = copy.deepcopy(p)
    q["selection"]["rule"] = "0" * 64
    with pytest.raises(nc.ProfileInvalid):
        nc.validate_profile(q, stage="stage1", prereg_sha256=FAKE)
    q = copy.deepcopy(p)
    q["environment"]["x"] = "C:\\Users\\a\\b"
    with pytest.raises(nc.ProfileInvalid, match="absolute_path"):
        nc.validate_profile(q, stage="stage1", prereg_sha256=FAKE)


def test_profile_rejects_two_active_and_wrong_detector_set():
    p = make_profile()
    q = copy.deepcopy(p)
    q["detectors"][0]["role"] = q["detectors"][1]["role"] = nc.ACTIVE
    with pytest.raises(nc.ProfileInvalid):
        nc.validate_profile(q, stage="stage2", prereg_sha256=FAKE)
    q = copy.deepcopy(p)
    q["detectors"] = q["detectors"][:2]
    with pytest.raises(nc.ProfileInvalid):
        nc.validate_profile(q, stage="stage1", prereg_sha256=FAKE)


def test_active_profile_derivation_changes_only_role_and_selection():
    p = make_profile()
    ev = {"outcome": f"SELECTED:{NET}", "selection_profile_sha256": "s" * 64, "evidence_id": "e1",
          "decided_at_utc": "2026-10-20T00:00:00+00:00", "gate_results": {"x": 1}, "reason": "r"}
    a = nc.derive_active_profile(p, "s" * 64, ev, "h" * 64, prereg_sha256=FAKE)
    assert [d["detector_id"] for d in a["detectors"] if d["role"] == nc.ACTIVE] == [NET]
    assert nc.active_profile_diff(p, a) == []
    nc.validate_profile(a, stage="stage2", prereg_sha256=FAKE)
    for bad in ({**ev, "outcome": ns.NO_ELIGIBLE}, {**ev, "outcome": "SELECTED:cpu-x"}, {**ev, "selection_profile_sha256": "z"}):
        with pytest.raises(nc.ProfileInvalid):
            nc.derive_active_profile(p, "s" * 64, bad, "h" * 64, prereg_sha256=FAKE)


def template(ready, live):
    return {"spec": {"containers": [{"name": "vllm", "readinessProbe": {"timeoutSeconds": ready},
                                     "livenessProbe": {"timeoutSeconds": live}}]}}


def test_probe_profile_tolerant_and_restore_checks():
    assert nc.probe_profile_problems(template(11, 11), expect_tolerant=True) == []
    assert nc.probe_profile_problems(template(1, 1), expect_tolerant=True)
    canon = {"readiness_timeout_sec": 1, "liveness_timeout_sec": 1}
    assert nc.probe_profile_problems(template(1, 1), expect_tolerant=False, canonical=canon) == []
    assert nc.probe_profile_problems(template(11, 11), expect_tolerant=False, canonical=canon)       # 복원 안 됨
    assert nc.probe_profile_problems(template(1, 1), expect_tolerant=False)                           # canonical 기록 없음 → 거부
    with pytest.raises(nc.ProfileInvalid):
        nc.probe_profile_from_template({"spec": {"containers": [{"name": "other"}]}})


def test_frozen_net_constants_match_calibration_evidence():
    ev = nc.verify_net_calibration()
    assert ev["definition_sha256"] == nc.NET_DEFINITION_SHA256
    d = json.loads((nc.ROOT / nc.NET_CALIBRATION_REL).read_text(encoding="utf-8"))
    assert d["threshold_seconds"] == nc.NET_THRESHOLD and round(nc.NET_THRESHOLD, 4) == 0.0064
    assert d["n_sessions"] == 20


def test_net_expr_same_as_probe_metric_script():
    import network_detector_probe_metric as pm
    assert pm.EXPR == nc.NET_EXPR


def test_pressure_stages_match_pilot_adapter():
    try:
        import network_degrade_adapter as nda
    except Exception as e:  # kubernetes 등 미설치 환경
        pytest.skip(f"adapter import 불가: {e}")
    assert [dict(s) for s in nda.STAGES] == [dict(s) for s in nc.NETWORK_STAGES]


def test_signal_identity_net_has_numeric_threshold_and_hashes():
    ident = nc.signal_identity(NET, {})
    assert ident["detector_id"] == NET and ident["threshold"] == nc.NET_THRESHOLD and len(ident["model_hash"]) == 64
    assert nc.signal_identity(CPU, {})["detector_id"] == CPU


def test_phase2_frozen_files_untouched():
    """기존 Phase 2 폴더는 읽기 전용으로만 쓴다 - 구현 동결 manifest가 있으면 해시가 그대로여야 한다."""
    m = nc.ROOT / nc.pc.IMPLEMENTATION_FREEZE
    if not m.is_file():
        pytest.skip("Phase 2 implementation freeze manifest 없음")
    files = json.loads(m.read_text(encoding="utf-8"))["files"]
    bad = [rel for rel, h in files.items() if rel.startswith("experiments/phase2_detector_selection/")
           and (nc.ROOT / rel).is_file() and nc.pc.text_sha256(nc.ROOT / rel) != h]
    assert bad == []
