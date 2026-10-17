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


# ---------- (2b) 사용자 결정(2026-10-17) 명세 그대로의 3후보 규칙 시험 ----------
# 명세: Gate 2 통과 0개 -> 선택 없음 / 1개 -> 선택 / 2개 이상 -> Gate 3.
#       Gate 3: 통과 후보의 정상 불필요 신호 합계 중 최소값 m을 찾고 (합계 >= m + 2)인 후보를 제거, 0~1건 차이는 동등.
#       시각: 중앙값 중 가장 빠른 값 b를 기준으로 (중앙값 >= b + 30 s)인 후보를 제거, 30 s 미만은 동등.
#       마지막: CPU > net_probe > c3-A IF (성능 우열이 아니라 운영 의존성 순서 - 앞선 기준으로 승부가 나지 않았을 때만).
# 사용자 문구의 "NO_SELECTION"은 코드·Phase 2 판정 함수의 이름 NO_ELIGIBLE_DETECTOR와 같은 결과다(동결된 phase2_verdict가 이 이름을 읽는다).
def spread(total):
    return [total, 0, 0, 0]


def pf(**kw):
    """통과 후보는 5세션 모두 해당 초(첫 3연속), 실패 후보는 None."""
    return {d: ([kw[d]] * 5 if kw.get(d) is not None else FAIL) for d in (CPU, NET, IF)}


def run_spec(unn, **timing):
    return ns.select(inputs({d: spread(unn.get(d, 0)) for d in (CPU, NET, IF)}, pf(**timing)))


def test_spec_1_no_gate2_pass_means_no_selection_and_the_name_matches_the_frozen_verdict_input():
    out = run_spec({}, **{CPU: None, NET: None, IF: None})
    assert out["outcome"] == ns.NO_ELIGIBLE == "NO_ELIGIBLE_DETECTOR" and out["gate_results"]["gate2"]["eligible"] == []
    assert out["gate_results"]["gate3"]["used"] is False and out["gate_results"]["tiebreak_simplicity"]["used"] is False
    import run_stage2 as frozen_s2                                                               # 동결 판정 함수가 이 이름을 읽는다
    assert frozen_s2.phase2_verdict(out["outcome"], [])["verdict"] == "PHASE2_SELECTION_VALIDATED_RECOVERY_PENDING"


@pytest.mark.parametrize("only", [CPU, NET, IF])
def test_spec_2_single_gate2_pass_is_selected_immediately_even_if_noisy(only):
    out = run_spec({only: 9, **{d: 0 for d in (CPU, NET, IF) if d != only}}, **{only: 100.0, **{d: None for d in (CPU, NET, IF) if d != only}})
    assert out["outcome"] == f"SELECTED:{only}" and out["gate_results"]["gate3"]["used"] is False         # Gate 2만으로 결정 - 소음은 선택에 쓰지 않는다
    assert out["gate_results"]["gate3"]["unnecessary_sum"][only] == 9                                        # 그래도 보고는 한다


@pytest.mark.parametrize("perm", [(CPU, NET, IF), (NET, IF, CPU), (IF, CPU, NET), (IF, NET, CPU), (CPU, IF, NET), (NET, CPU, IF)])
def test_spec_3_unnecessary_0_1_3_removes_only_the_3(perm):
    zero, one, three = perm
    out = run_spec({zero: 0, one: 1, three: 3}, **{d: 100.0 for d in (CPU, NET, IF)})
    assert three not in out["gate_results"]["gate3"]["retained"] and set(out["gate_results"]["gate3"]["retained"]) == {zero, one}
    assert out["gate_results"]["gate3"]["result"] == "equivalent"                                          # 0과 1은 동등 -> 다음 기준으로
    assert out["outcome"] == f"SELECTED:{min((zero, one), key=nc.SIMPLICITY_ORDER.index)}"                 # 시각도 같아 단순성 순서


def test_spec_3b_the_users_example_cpu0_net1_if3():
    out = run_spec({CPU: 0, NET: 1, IF: 3}, **{d: 100.0 for d in (CPU, NET, IF)})
    assert out["gate_results"]["gate3"]["retained"] == [CPU, NET] and out["outcome"] == f"SELECTED:{CPU}"


@pytest.mark.parametrize("zero", [CPU, NET, IF])
def test_spec_4_unnecessary_0_2_2_keeps_only_the_zero(zero):
    others = {d: 2 for d in (CPU, NET, IF) if d != zero}
    out = run_spec({zero: 0, **others}, **{d: 100.0 for d in (CPU, NET, IF)})
    assert out["gate_results"]["gate3"]["retained"] == [zero] and out["outcome"] == f"SELECTED:{zero}"
    assert out["gate_results"]["tiebreak_timing"]["used"] is False                                         # Gate 3에서 끝남


def test_spec_5_equal_unnecessary_moves_on_to_timing():
    out = run_spec({CPU: 1, NET: 1, IF: 1}, **{CPU: 140.0, NET: 100.0, IF: 100.0})
    assert out["gate_results"]["gate3"]["result"] == "equivalent" and out["gate_results"]["tiebreak_timing"]["used"] is True
    assert out["gate_results"]["tiebreak_timing"]["retained"] == [NET, IF] and out["outcome"] == f"SELECTED:{NET}"


def test_spec_6_timing_100_120_140_removes_only_the_140():
    for perm in ((CPU, NET, IF), (IF, NET, CPU), (NET, IF, CPU), (CPU, IF, NET)):
        fast, mid, slow = perm
        out = run_spec({}, **{fast: 100.0, mid: 120.0, slow: 140.0})
        assert set(out["gate_results"]["tiebreak_timing"]["retained"]) == {fast, mid} and slow not in out["gate_results"]["tiebreak_timing"]["retained"]
        assert out["outcome"] == f"SELECTED:{min((fast, mid), key=nc.SIMPLICITY_ORDER.index)}"            # 20 s 차이는 동등 -> 단순성


def test_spec_6b_timing_boundary_is_best_plus_30_removed_and_just_below_kept():
    assert run_spec({}, **{CPU: 100.0, NET: 130.0, IF: None})["outcome"] == f"SELECTED:{CPU}"                 # 정확히 30 s 느리면 제거(>=)
    out = run_spec({}, **{CPU: 100.0, NET: 129.9, IF: None})
    assert out["gate_results"]["tiebreak_timing"]["retained"] == [CPU, NET] and out["gate_results"]["tiebreak_simplicity"]["used"] is True


def test_spec_7_all_timing_gaps_under_30_go_to_the_final_tiebreak():
    out = run_spec({}, **{CPU: 120.0, NET: 100.0, IF: 110.0})                                                # 가장 빠른 것(NET)과 최대 20 s 차이
    assert out["gate_results"]["tiebreak_timing"]["result"] == "equivalent" and out["gate_results"]["tiebreak_simplicity"]["used"] is True
    assert out["outcome"] == f"SELECTED:{CPU}"                                                               # 시각이 가장 느려도 단순성 1순위가 이긴다(성능 순서가 아님)


def test_spec_8_three_way_final_tie_is_cpu():
    out = run_spec({}, **{d: 100.0 for d in (CPU, NET, IF)})
    assert out["gate_results"]["tiebreak_simplicity"] == {"used": True, "order": [CPU, NET, IF], "result": CPU} and out["outcome"] == f"SELECTED:{CPU}"


def test_spec_9_cpu_out_then_net_probe_before_if():
    out = run_spec({}, **{CPU: None, NET: 100.0, IF: 100.0})
    assert out["gate_results"]["gate2"]["eligible"] == [NET, IF] and out["outcome"] == f"SELECTED:{NET}"
    out = run_spec({CPU: 0, NET: 3, IF: 0}, **{CPU: None, NET: 100.0, IF: 100.0})                              # CPU는 조용해도 Gate 2 탈락 / NET은 Gate 3에서 탈락
    assert out["outcome"] == f"SELECTED:{IF}"


@pytest.mark.parametrize("pair", [(CPU, IF), (CPU, NET), (NET, IF)])
def test_spec_10_every_two_candidate_subset_matches_the_frozen_phase2_rule(pair):
    """후보 2개만 남는 입력에서 Phase 2 동결 selection.select와 같은 결과. 쌍의 앞을 Phase 2의 CPU 자리, 뒤를 IF 자리로 옮겨 대조한다."""
    a, b = pair
    rng = random.Random(sum(map(ord, a + b)))
    times = [None, 90.0, 110.0, 150.0, 200.0, 363.0, 363.5, 420.0]
    for _ in range(300):
        nu = {d: [rng.choice([0, 0, 1, 2, 3]) for _ in range(4)] for d in pair}
        pt = {d: [rng.choice(times) for _ in range(5)] for d in pair}
        mine = ns.select(inputs(nu, pt), detectors=pair, simplicity_order=pair)
        theirs = p2sel.select(inputs({CPU: nu[a], IF: nu[b]}, {CPU: pt[a], IF: pt[b]}))
        relabel = lambda o: o.replace(a, CPU).replace(b, IF) if o.startswith("SELECTED:") else o        # noqa: E731
        assert relabel(mine["outcome"]) == theirs["outcome"], (pair, nu, pt)


def test_invalid_pressure_session_is_not_a_detector_failure_in_gate2():
    """유효 세션만 select 입력이 된다. 5개를 채우지 못하면 후보의 실패가 아니라 SELECTION_INCOMPLETE다."""
    z = [0] * 4
    pt = {CPU: FAIL, NET: [58.0] * 4, IF: FAIL}                                                                # 유효 세션 4개뿐(무효 1개는 입력에 없음)
    out = ns.select(inputs({CPU: z, NET: z, IF: z}, pt, n_pressure=4))
    assert out["outcome"] == ns.INCOMPLETE and out["gate_results"]["gate2"]["eligible"] is None            # Gate 2 판정 자체를 하지 않는다


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


def test_unfrozen_prereg_is_refused(monkeypatch):
    monkeypatch.setattr(nc, "PREREG_SHA256", None)                      # 동결 전 상태를 가정(동결 뒤에도 거부 동작을 계속 시험)
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
