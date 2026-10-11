"""v2 workload feasibility pilot - 선택 규칙·단계 판정·발사 계획(aliasing·재현·달성 RPS)·SLO 경계 종료·실행기 끝까지·provenance.
네트워크·클러스터 접속 없음."""
import csv
import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

import run_guarded_pilot as gp
import run_v2_workload_pilot as rv
import test_run_guarded_pilot as tgp
import v2_workload_pilot_analysis as wa
from v2_workload_pilot_analysis import v2_arrival

T0 = datetime(2026, 10, 10, tzinfo=timezone.utc)
LV = [("L1", 0.025), ("L2", 0.05), ("L3", 0.10), ("L4", 0.15), ("L5", 0.20)]
LADDER = yaml.safe_load(rv.LADDER.read_text(encoding="utf-8"))
SEEDS = LADDER["arrival"]["seeds"]
STAGES = LADDER["stages"]


def at(s):
    return T0 + timedelta(seconds=s)


def st(level, rps, ok=True, act=False, ev=True, lc=True):
    return {"level": level, "rps": rps, "normal_and_safe": ok, "activated": act, "evaluable": ev, "load_conformant": lc}


def rep(spec, valid=True, upto=5):
    return {"run": "x", "valid": valid,
            "stages": [st("L0", 0)] + [st(lv, r, *spec.get(lv, (True, False))) for lv, r in LV[:upto]]}


# ---------------- 선택 규칙 ----------------
def test_selection_high_medium_low_and_tie_goes_lower():
    spec = {"L3": (True, True), "L4": (True, True), "L5": (False, True)}
    s = wa.select_levels([rep(spec), rep(spec)])
    assert (s["low"], s["medium"], s["high"], s["feasible"]) == ("L1", "L2", "L4", True)  # 0.075에 L2·L3 동률 → 낮은 쪽


def test_selection_requires_every_valid_rep_and_ignores_invalid():
    a = rep({"L4": (True, True), "L3": (True, True)})
    b = rep({"L4": (True, False), "L3": (True, True)})
    s = wa.select_levels([a, b, rep({}, valid=False)])
    assert s["high"] == "L3" and s["activated_levels"] == ["L3"]


def test_no_activation_is_infeasible_not_guessed():
    s = wa.select_levels([rep({}), rep({})])
    assert s["feasible"] is False and s["reason"] == "no_level_normal_safe_and_activated" and s["high"] is None


def test_high_at_lowest_level_leaves_medium_missing():
    s = wa.select_levels([rep({"L1": (True, True), "L2": (False, False), "L3": (False, False), "L4": (False, False),
                               "L5": (False, False)})])
    assert s["high"] == "L1" and s["medium"] is None and s["feasible"] is False


def test_unevaluable_or_nonconformant_stage_is_not_selectable():
    assert wa.select_levels([rep({"L4": (True, True, False), "L3": (True, True)})])["high"] == "L3"
    assert wa.select_levels([rep({"L4": (True, True, True, False), "L3": (True, True)})])["high"] == "L3"


def test_slo_boundary_stage_never_high_and_unrun_stage_not_selectable():
    # rep a: L4 SLO 위반 → L5 미실행. rep b: L5까지 정상·활성화. L5는 a에 없어 불가, L4는 a에서 위반이라 불가
    a = rep({"L3": (True, True), "L4": (False, True)}, upto=4)
    b = rep({"L3": (True, True), "L4": (True, True), "L5": (True, True)})
    s = wa.select_levels([a, b])
    assert s["high"] == "L3" and "L4" not in s["normal_safe_levels"] and "L5" not in s["normal_safe_levels"]


# ---------------- A. 발사 계획 - scrape 위상 고정 없음 ----------------
def _fixed(rps, dur=300):   # 동결 ramp.py 방식(next_fire += 1/rps) 재현 - 비교 기준
    return [k / rps for k in range(int(dur * rps - 1e-9) + 1) if k / rps < dur]


def _sampled_busy_fraction(sends, busy_sec, offset, dur=300, scrape=15):
    grid = [offset + scrape * i for i in range(int(dur // scrape) + 1) if offset + scrape * i < dur]
    return sum(any(s <= g < s + busy_sec for s in sends) for g in grid) / len(grid)


@pytest.mark.parametrize("run", sorted(SEEDS))
def test_arrival_phases_not_locked_to_15s_scrape_grid(run):
    for i, (_, rps) in enumerate(LV, 1):
        plan = v2_arrival.schedule(rps, 300, v2_arrival.stage_seed(SEEDS[run], i))
        assert len(Counter(round(t % 15, 3) for t in plan)) == len(plan)          # 모든 요청이 서로 다른 scrape 위상
        assert len(Counter(round(t % 15, 3) for t in _fixed(rps))) <= 9            # 고정 간격은 위상 ≤9개(테스트가 구분함)
        if len(plan) >= 30:   # L3 이상: 1 s 위상 칸 하나에 몰리지 않음(고정 5·10 s 간격은 1/3)
            assert max(Counter(int(t % 15) for t in plan).values()) / len(plan) <= 0.25


def test_gauge_sampling_not_aliased_at_top_level():
    """running gauge를 15 s 격자로 찍을 때(요청당 1 s busy, L5) scrape 위상과 무관하게 실제 busy 비율(0.2) 근처.
    고정 5 s 간격은 위상에 따라 0 또는 1(완전 aliasing)."""
    fixed = [_sampled_busy_fraction(_fixed(0.2), 1.0, o / 2) for o in range(30)]
    assert min(fixed) == 0.0 and max(fixed) == 1.0
    plans = [v2_arrival.schedule(0.2, 300, v2_arrival.stage_seed(s, 5)) for s in SEEDS.values()]
    pooled = [sum(_sampled_busy_fraction(p, 1.0, o / 2) for p in plans) / len(plans) for o in range(30)]
    assert max(abs(x - 0.2) for x in pooled) <= 0.1


# ---------------- B. 재현성 ----------------
def test_same_seed_same_schedule_different_seed_different():
    a = v2_arrival.schedule(0.15, 300, v2_arrival.stage_seed(SEEDS["v2wl90"], 4))
    assert a == v2_arrival.schedule(0.15, 300, v2_arrival.stage_seed(SEEDS["v2wl90"], 4))
    others = [v2_arrival.schedule(0.15, 300, v2_arrival.stage_seed(SEEDS[r], 4)) for r in ("v2wl91", "v2wl92")]
    assert all(o != a for o in others) and others[0] != others[1]
    assert v2_arrival.schedule(0.15, 300, v2_arrival.stage_seed(SEEDS["v2wl90"], 3)) != a   # 단계마다 다른 seed


def test_seeds_preregistered_and_bound_to_run_id():
    assert SEEDS == {"v2wl90": 2026100990, "v2wl91": 2026100991, "v2wl92": 2026100992}
    assert {r: rv.run_seed_for(r) for r in rv.RUNS} == SEEDS     # 재시도도 같은 ID면 같은 seed - 인자로 못 바꿈
    assert LADDER["arrival"]["rule"] == v2_arrival.RULE and LADDER["arrival"]["max_send_lag_sec"] == wa.MAX_SEND_LAG_SEC


# ---------------- C. 달성 RPS ----------------
def _raw(plan, lag=0.01):
    return [{"k": k, "scheduled_offset_sec": t, "send_lag_sec": lag} for k, t in enumerate(plan)]


@pytest.mark.parametrize("i,rps", [(i, r) for i, (_, r) in enumerate(LV, 1)])
def test_achieved_rps_within_contract_tolerance(i, rps):
    seed = v2_arrival.stage_seed(SEEDS["v2wl90"], i)
    plan = v2_arrival.schedule(rps, 300, seed)
    assert abs(len(plan) / 300 - rps) <= 1 / 300 and all(0 <= t < 300 for t in plan) and plan == sorted(plan)
    row = {"target_rps": rps, "duration_sec": 300, "sent": len(plan), "seed": seed}
    assert wa.load_conformance(row, _raw(plan), SEEDS["v2wl90"], i)["load_conformant"]
    short = wa.load_conformance({**row, "sent": len(plan) - 1}, _raw(plan[:-1]), SEEDS["v2wl90"], i)
    late = wa.load_conformance(row, _raw(plan, lag=1.5), SEEDS["v2wl90"], i)
    wrong_seed = wa.load_conformance(row, _raw(plan), SEEDS["v2wl91"], i)
    assert not short["load_conformant"] and not late["load_conformant"] and "seed_mismatch" in wrong_seed["load_problems"]


# ---------------- 단계 판정(합성 파일) ----------------
def _series(f, n0=0, n1=2100):
    return [[at(t).timestamp(), f(t)] for t in range(n0, n1, 15)]


def _synthetic(tmp_path, slow=None, fail_at=(), running=None, upto=5, boundary=False):
    # L0 0-300, L1..L5 각 300 s (300-1800). 단계 raw는 사전 seed 계획 그대로(지연 0.01 s)
    stages = []
    for i, (lv, r) in enumerate(LV[:upto], 1):
        seed = v2_arrival.stage_seed(SEEDS["v2wl90"], i)
        plan = v2_arrival.schedule(r, 300, seed)
        raw = tmp_path / f"raw{i}.csv"
        with open(raw, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["k", "scheduled_offset_sec", "send_lag_sec"])
            w.writeheader()
            w.writerows(_raw(plan))
        stages.append({"stage": f"{lv}-{r}rps", "target_rps": r, "duration_sec": 300, "seed": seed, "sent": len(plan),
                       "success": len(plan), "stage_start_utc": at(300 * i).isoformat(),
                       "stage_end_utc": at(300 * (i + 1)).isoformat(), "raw_csv": str(raw)})
    s = {"run": "v2wl90", "run_id": "rid", "outcome_status": "completed", "cleanup_errors": [], "run_seed": SEEDS["v2wl90"],
         "baseline_window": {"start_utc": at(0).isoformat(), "end_utc": at(300).isoformat()}, "ramp_stages": stages,
         "ramp_terminated_after_slo_boundary": boundary, "slo_boundary_stage": stages[-1]["stage"] if boundary else None}
    lvl = lambda t: 0 if t < 300 else min(5, int(t // 300))  # noqa: E731
    running = running or (lambda t: 1 if lvl(t) >= 3 and (t // 15) % 2 == 0 else 0)
    m = {"series": {"running": _series(running),
                    "waiting": _series(lambda t: 0), "kv_cache_perc": _series(lambda t: 0.01 * lvl(t)),
                    "queue_time_sum": _series(lambda t: t * 1e-4), "queue_time_count": _series(lambda t: t),
                    "finished_requests_total": _series(lambda t: t), "finished_abort_error_total": _series(lambda t: 0),
                    "generation_tokens_total": _series(lambda t: 3 * t),
                    "iteration_count": _series(lambda t: 2 * t), "cpu_seconds_total": _series(lambda t: 1.5 * t)}}
    p = tmp_path / "probe.csv"
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["sent_at", "latency", "success", "status"])
        for i in range(-10, 300 * (upto + 2) + 10):
            lat = 3.0 if slow and slow[0] <= i <= slow[1] else 0.25
            w.writerow([at(i).isoformat(), lat, "False" if i in fail_at else "True", 200])
    return s, m, p


def test_analyze_rep_marks_slo_violation_only_in_its_stage_and_reports_counts(tmp_path):
    s, m, p = _synthetic(tmp_path, slow=(1600, 1700))
    r = wa.analyze_rep(s, m, p)
    by = {x["level"]: x for x in r["stages"]}
    assert r["valid"] and by["L5"]["slo_normal"] is False and by["L4"]["slo_normal"] is True
    assert by["L3"]["activated"] and not by["L2"]["activated"] and by["L3"]["running_nonzero_fraction"] >= 0.2
    assert by["L1"]["evaluable"] and abs(by["L1"]["queue_time_mean_sec"] - 1e-4) < 1e-9
    l3 = by["L3"]
    for k in ("samples", "running_nonzero_samples", "running_nonzero_fraction", "waiting_nonzero_samples",
              "waiting_nonzero_fraction", "queue_time_mean_sec", "queue_time_requests", "achieved_rps", "sample_count_note"):
        assert l3[k] is not None, k
    assert l3["achieved_rps"] == 0.1 and l3["load_conformant"] and l3["samples"] == 20 and l3["sample_count_note"] == "as_expected"
    assert wa.select_levels([r])["high"] == "L4"


def test_slo_boundary_rep_is_valid_and_judged_up_to_boundary(tmp_path):
    s, m, p = _synthetic(tmp_path, slow=(1000, 1100), upto=3, boundary=True)   # L3(900-1200)에서 위반 → 종료
    r = wa.analyze_rep(s, m, p)
    assert r["valid"] and r["ramp_terminated_after_slo_boundary"] and r["slo_boundary_stage"] == "L3-0.1rps"
    assert [x["level"] for x in r["stages"]] == ["L0", "L1", "L2", "L3"] and r["stages"][-1]["slo_normal"] is False
    assert wa.select_levels([r])["high"] is None   # 활성화된 L3가 위반 → 정상·활성화 단계 없음(infeasible)


def test_analyze_rep_fail_closed_when_incomplete(tmp_path):
    s, m, p = _synthetic(tmp_path)
    s["outcome_status"] = "aborted"
    r = wa.analyze_rep(s, m, p)
    assert r["valid"] is False and r["stages"] == []


# ---------------- E. 활성화 경계 0.20 ----------------
def test_activation_threshold_boundary(tmp_path):
    # 단계 [300k, 300(k+1))의 15 s 표본 20개. L2: 앞 3개 비zero(0.15, 바로 아래), L3: 앞 4개(0.20, 경계 이상)
    def running(t):
        k = (t % 300) // 15
        return 1 if (t // 300 == 2 and k < 3) or (t // 300 == 3 and k < 4) else 0
    s, m, p = _synthetic(tmp_path, running=running)
    by = {x["level"]: x for x in wa.analyze_rep(s, m, p)["stages"]}
    assert by["L2"]["running_nonzero_fraction"] < wa.ACTIVATION_MIN_FRACTION and not by["L2"]["activated"]
    assert by["L3"]["running_nonzero_fraction"] == wa.ACTIVATION_MIN_FRACTION and by["L3"]["activated"]
    assert (by["L2"]["running_nonzero_samples"], by["L3"]["running_nonzero_samples"], by["L3"]["samples"]) == (3, 4, 20)
    assert wa.ACTIVATION_MIN_FRACTION == 0.20


# ---------------- F. request_success_total 의미 ----------------
def test_request_success_total_is_throughput_not_arrival():
    assert "request_success_total" not in wa.METRICS and wa.FEATURE_ROLE["finished_requests_total"] == "service_throughput"
    assert "vllm:request_success_total" in wa.METRICS["finished_requests_total"]
    assert all("arrival" not in k for k in wa.METRICS) and all("arrival" not in v for v in wa.FEATURE_ROLE.values())
    assert wa.FEATURE_ROLE["harness_target_rps"] == "workload_label_only"
    assert all('job="vllm-active"' in e for e in wa.METRICS.values() if "vllm:" in e)   # preview job 이중 수집 제외
    for doc in (rv.REPO_ROOT / rv.CONTRACT_DOC, rv.REPO_ROOT / "docs/design/phase8-aiops-v2-design-draft-20261009.md"):
        for line in doc.read_text(encoding="utf-8").splitlines():
            if "request_success_total" in line and ("arrival" in line or "도착률" in line):
                assert "아님" in line or "아니" in line or "폐기" in line, line


def test_prom_raw_sums_label_variants_at_same_scrape_time():
    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"result": [{"values": [[1.0, "2"], [16.0, "3"]]}, {"values": [[1.0, "5"], [16.0, "5"]]}]}}
    seen = {}
    out = wa.prom_raw("http://p", "m{}", at(0), at(30), get=lambda url, params, timeout: seen.update(params) or R())
    assert out == [[1.0, 7.0], [16.0, 8.0]] and seen["query"] == "m{}[31s]"


# ---------------- trial(단계별 실행·D. SLO 경계 종료) ----------------
class Clock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, s):
        self.t += s

    def mono(self):
        return self.t


class FakeStageRamp:
    def __init__(self, clock, dur=300):
        self.c, self.dur, self.calls, self.t0 = clock, dur, [], {}

    def prepare(self):
        self.calls.append("prepare")

    def start(self, i):
        self.calls.append(f"start{i}")
        self.t0[i] = self.c.t

    def done(self, i):
        return self.c.t - self.t0[i] >= self.dur

    def fetch(self, i):
        self.calls.append(f"fetch{i}")
        return {"stage": STAGES[i - 1]["name"], "target_rps": STAGES[i - 1]["rps"],
                "stage_start_utc": at(self.t0[i]).isoformat(), "stage_end_utc": at(self.c.t).isoformat()}

    def cleanup(self):
        self.calls.append("cleanup")


def _probe_rows(clock, slow=None):
    """1 RPS probe - 현재 시계까지 응답이 확인된 요청만(실제 probe CSV는 완료 순서로 append)."""
    def rows():
        out = []
        for i in range(-10, int(clock.t) + 1):
            lat = 3.0 if slow and slow[0] <= i <= slow[1] else 0.25
            if i + lat <= clock.t:
                out.append({"sent_at": at(i), "latency": lat, "success": True})
        return out
    return rows


def _trial(clock, ramp, prober, safety=lambda s, p: None, slow=None):
    return rv.trial(ramp, prober, safety, _probe_rows(clock, slow), STAGES, sleep=clock.sleep, monotonic=clock.mono,
                    now=lambda: at(clock.t))


class SloViolatingProber(tgp.FakeProber):
    def check_slo_violation(self):
        return True


def test_trial_runs_all_stages_when_slo_normal_and_safety_never_sees_slo():
    c = Clock()
    seen = []
    ramp, prober = FakeStageRamp(c), SloViolatingProber()
    o = _trial(c, ramp, prober, safety=lambda s, p: seen.append(p.check_slo_violation()))
    assert o["status"] == "completed" and o["cleanup_status"] == "complete" and not o["ramp_terminated_after_slo_boundary"]
    assert [x for x in ramp.calls if x.startswith("start")] == [f"start{i}" for i in range(1, 6)] and ramp.calls[-1] == "cleanup"
    assert len(o["ramp_stages"]) == 5 and all(v["computable"] and not v["violating"] for v in o["stage_slo_verdicts"])
    assert seen and not any(seen) and "stop" in prober.calls   # 안전 감시는 SLO 위반을 보지 않는다(판정은 단계 끝에서만)


def test_trial_stops_after_slo_violating_stage_not_invalid():
    c = Clock()
    ramp = FakeStageRamp(c)
    o = _trial(c, ramp, tgp.FakeProber(), slow=(700, 800))   # 기준선 ~0-300, L1 ~300-600, L2 ~600-900 안쪽을 느리게
    assert o["status"] == "completed" and o["reason"] == "SLO 경계에서 ladder 종료"
    assert o["ramp_terminated_after_slo_boundary"] is True and o["slo_boundary_stage"] == "L2-0.05rps"
    assert o["stages_not_run"] == ["L3-0.10rps", "L4-0.15rps", "L5-0.20rps"]
    assert "start3" not in ramp.calls and ramp.calls[-1] == "cleanup" and o["cleanup_status"] == "complete"
    assert [r["stage"] for r in o["ramp_stages"]] == ["L1-0.025rps", "L2-0.05rps"]   # 위반 단계 데이터 보존
    assert [v["violating"] for v in o["stage_slo_verdicts"]] == [False, True]


def test_trial_slo_evidence_missing_is_fail_closed():
    c = Clock()
    ramp = FakeStageRamp(c)
    o = rv.trial(ramp, tgp.FakeProber(), lambda s, p: None, lambda: [], STAGES, sleep=c.sleep, monotonic=c.mono,
                 now=lambda: at(c.t))
    assert o["status"] == "aborted" and "probe 증거 미도착" in o["reason"] and "start2" not in ramp.calls
    assert ramp.calls[-1] == "cleanup"


def test_trial_safety_failure_aborts_and_cleans_up():
    c = Clock()
    ramp = FakeStageRamp(c)

    def safety(s, p):
        if c.t > 400:
            raise rv.HarnessCorrupted("node NotReady")
    o = _trial(c, ramp, tgp.FakeProber(), safety=safety)
    assert o["status"] == "aborted" and "NotReady" in o["reason"] and ramp.calls[-1] == "cleanup"


# ---------------- 실행기 끝까지(동결 World) ----------------
def _world(tmp_path, monkeypatch):
    return tgp.World(tmp_path, monkeypatch, mode=gp.MODE_RO, send_at=())


def _run(w, run, monkeypatch, **kw):
    # World 시계는 실제 시각과 달라 공식 판정 함수는 trial 테스트에서 검증 - 여기서는 배선만
    monkeypatch.setattr(rv.wa, "stage_slo_verdict",
                        lambda rows, a0, s, e: {"computable": True, "reason": None, "violating": False, "episodes": []})
    made = []

    def make_ramp(ladder, run_id, rep, seed, results_dir):
        made.append(seed)
        return FakeStageRamp(w.clock)
    s = rv.run_pilot(run, deps=w.deps(), results_dir=w.results, make_ramp=make_ramp,
                     prom_range_fn=lambda e, a, b: [[a.timestamp(), 0.0]], **kw)
    return s, made


def test_run_pilot_end_to_end_no_promotion_no_context(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    s, made = _run(w, "v2wl90", monkeypatch)
    assert s["outcome_status"] == "completed" and s["cleanup_errors"] == [] and s["counted"] is False
    assert w.promotes == [] and w.overrides == [] and w.policy.snapshot()["context"] is None
    assert made == [SEEDS["v2wl90"]] and s["run_seed"] == SEEDS["v2wl90"] and len(s["ramp_stages"]) == 5
    c = json.loads((w.results / f"contract-{s['run_id']}.json").read_text(encoding="utf-8"))
    assert c["detector"] is None and c["signal_send"] is False and c["counted"] is False
    assert c["run_seed"] == SEEDS["v2wl90"] and c["arrival_rule"] == v2_arrival.RULE
    assert (w.results / f"metrics-{s['run_id']}.json").exists() and not list(w.results.glob("running-*"))


def test_run_pilot_schedule_and_id_reuse(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="순서 밖"):
        _run(w, "v2wl91", monkeypatch)
    _run(w, "v2wl90", monkeypatch)
    with pytest.raises(SystemExit, match="재사용 금지"):
        _run(w, "v2wl90", monkeypatch)


def test_act_policy_refused(tmp_path, monkeypatch):
    w = tgp.World(tmp_path, monkeypatch, mode=gp.MODE_ACT, send_at=())
    with pytest.raises(SystemExit, match="사전검사 실패"):
        _run(w, "v2wl90", monkeypatch)


def test_provenance_fail_closed(tmp_path):
    files = {}
    for rel in rv.PROVENANCE_REQUIRED.values():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(rel, encoding="utf-8")
        files[rel] = hashlib.sha256(rel.encode()).hexdigest()
    mp = tmp_path / rv.FREEZE_MANIFEST
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps({"status": "frozen", "version": rv.FREEZE_VERSION, "files": files}), encoding="utf-8")
    assert rv.verify_provenance(root=tmp_path)["verification_status"] == "verified"
    (tmp_path / rv.PROVENANCE_REQUIRED["arrival"]).write_text("changed", encoding="utf-8")
    with pytest.raises(SystemExit, match="해시 불일치"):
        rv.verify_provenance(root=tmp_path)
    mp.write_text(json.dumps({"status": "candidate", "version": rv.FREEZE_VERSION, "files": files}), encoding="utf-8")
    with pytest.raises(SystemExit, match="형식 오류"):
        rv.verify_provenance(root=tmp_path)


def test_ladder_is_single_axis_and_preregistered():
    assert LADDER["target"]["max_tokens"] == 10 and LADDER["target"]["prompt"] == "Hello"
    assert [s["rps"] for s in STAGES] == [0.025, 0.05, 0.10, 0.15, 0.20]
    assert {s["duration_sec"] for s in STAGES} == {300}
    assert Path(rv.ARRIVAL_SCRIPT).name == "v2_arrival.py"


def test_run_pilot_default_metric_capture_uses_prom_raw(tmp_path, monkeypatch):
    """prom_range_fn 없이(라이브 기본 경로) metric 저장이 wa.prom_raw를 실제로 부르는지 - 이름 불일치 회귀 방지."""
    w = _world(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(rv.wa, "prom_raw", lambda url, sel, a, b: calls.append((url, sel)) or [[a.timestamp(), 0.0]])
    monkeypatch.setattr(rv.wa, "stage_slo_verdict",
                        lambda rows, a0, s, e: {"computable": True, "reason": None, "violating": False, "episodes": []})
    s = rv.run_pilot("v2wl90", deps=w.deps(), results_dir=w.results, make_ramp=lambda *a: FakeStageRamp(w.clock))
    assert s["metrics_error"] is None and len(calls) == len(wa.METRICS) and {u for u, _ in calls} == {rv.PROM_URL}


def test_make_stage_ramp_commands_and_in_pod_hash_check(tmp_path, monkeypatch):
    """실제 kubectl 명령 모양(이미지·sleep·복사·단계 명령·seed)과 pod 안 해시 대조 fail-closed."""
    import subprocess
    cmds = []
    good = [rv.RAMP_PY_SHA256, rv._sha(rv.ARRIVAL_SCRIPT), rv._sha(rv.LADDER)]
    hashes = {"v": good}

    def run(cmd, check=False, timeout=20.0):
        cmds.append(cmd)
        out = ""
        if "hashlib" in cmd[-1]:
            out = "\n".join(hashes["v"])
        elif cmd[-1].startswith("cat /e"):
            out = "0"
        elif cmd[-2:-1] == ["cat"]:
            out = "stage,target_rps,sent\nL1-0.025rps,0.025,8\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")
    monkeypatch.setattr(rv.lra, "_run", run)
    monkeypatch.setattr(rv.lra, "_wait_pod_ready", lambda pod: True)
    monkeypatch.setattr(rv.lra, "_delete_pod", lambda pod: cmds.append(["delete", pod]))
    monkeypatch.setattr(rv.time, "sleep", lambda s: None)
    r = rv.make_stage_ramp(str(rv.LADDER), "rid", 3090, SEEDS["v2wl90"], tmp_path)
    r.prepare()
    assert cmds[0][:2] == ["kubectl", "run"] and f"--image={rv.lra.IMAGE}" in cmds[0] and cmds[0][-2:] == ["sleep", "2400"]
    assert [Path(c[2]).name for c in cmds if c[:2] == ["kubectl", "cp"]] == ["v2-workload-ladder.yaml", "v2_arrival.py"]
    r.start(3)
    stage_cmd = cmds[-1][-1]
    assert (f"python /v2_arrival.py --config /v2-workload-ladder.yaml --stage 3 --run-seed {SEEDS['v2wl90']} "
            f"--summary-out /s3.csv --raw-out /r3.csv") in stage_cmd
    assert r.done(3) is True
    row = r.fetch(3)
    assert row["sent"] == "8" and Path(row["raw_csv"]).exists()
    with pytest.raises(FileExistsError):
        r.fetch(3)   # x 모드 - 같은 단계 증거 덮어쓰기 금지
    r.cleanup()
    hashes["v"] = ["0" * 64] + good[1:]
    with pytest.raises(rv.TrialInvalid, match="해시 불일치"):
        rv.make_stage_ramp(str(rv.LADDER), "rid2", 3091, SEEDS["v2wl91"], tmp_path).prepare()
