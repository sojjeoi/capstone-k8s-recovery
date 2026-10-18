"""n2_session 시험(2026-10-17, DEVELOPMENT). 클러스터 접근 없음 - 가짜 클러스터(World)와 가짜 지연 설정으로 압박 세션 전체 흐름을 검증한다.
핵심: (1) 정상 세션이 끝까지 돌고 전달 증거가 남는다, (2) 위험한 상태는 세션 시작 전에 막힌다, (3) 지연이 새 Pod로 새거나 설정이 어긋나면 무효,
(4) 실제 조회기는 get 명령만 쓴다, (5) 원본 run_stage1은 바뀌지 않는다."""
import copy
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import n2_cleanup as cl  # noqa: E402
import n2_common as nc  # noqa: E402
import n2_session as ns2  # noqa: E402
import n2_stage1 as ns1  # noqa: E402
from test_n2_executor import env  # noqa: E402,F401  (합성 외부 artifact + 동결 해시 교체 fixture)

pc = nc.pc
import arm_controller  # noqa: E402
import run_guarded_pilot as gp  # noqa: E402
import run_stage1 as orig  # noqa: E402  (Phase 2 동결 원본 - 읽기 전용 대조)
import test_run_guarded_pilot as tgp  # noqa: E402

ACTIVE_POD, NEW_POD = "vllm-serving-aaa111-x", "vllm-serving-bbb222-y"
CLEAN_POLICY = {"mode": "record_only", "context": None, "blocked": False, "unresolved": []}
PREP = {"ready": True, "pre_prepare_active_selector": "aaa111", "created_pod_hash": "bbb222", "external_interference": False}


def template(ready=11, live=11):
    """ready·live가 None이면 timeoutSeconds를 적지 않는다 - 실제 기본 Rollout(gitops/apps/vllm-serving/rollout.yaml)이 이 모양이다."""
    def probe(v):
        return {"httpGet": {"path": "/health", "port": 8000}, "periodSeconds": 5} if v is None else {"timeoutSeconds": v}
    return {"spec": {"containers": [{"name": "vllm", "image": "vllm-cpu-env:latest", "imagePullPolicy": "Never",
                                     "args": ["--model=Qwen/Qwen2.5-0.5B-Instruct", "--port=8000"],
                                     "resources": {"limits": {"cpu": "3", "memory": "6Gi"}},
                                     "readinessProbe": probe(ready), "livenessProbe": probe(live)}]}}


def base_template():
    return template(None, None)


def prom_body(v, n=1):
    return {"status": "success", "data": {"result": [{"metric": {"pod": f"p{i}"}, "value": [1.0, str(v)]} for i in range(n)]}}


def cr_body(cr_name, run_id, arm, target, stage, duration=None, *, records=True, selector=None):
    body = ns2.pr.expected_cr_body(cr_name, run_id, arm, target, stage, duration=duration)
    if selector:
        body["spec"]["selector"].update(selector)
    body["status"] = {"conditions": [{"type": "AllInjected", "status": "True"}]}
    if records:
        body["status"]["experiment"] = {"containerRecords": [{"id": f"vllm-serving/{target}", "phase": "Injected"}]}
    return body


class FakeNet:
    """RealNetLive 자리 - 읽기 값만 돌려준다(클러스터 없음). 지연 설정은 chaos_fns()가 이 객체의 dict에 만든다."""

    def __init__(self, profile_path, tpl=None, **over):
        self.profile_path, self.tpl = profile_path, tpl or template()
        self.crs, self.chaos_listed, self.policy, self.leak = {}, [], dict(CLEAN_POLICY), 0.01
        self.gone_left, self.cr_kwargs, self.pods, self.queries = [], {}, None, []
        self.protected_calls, self.replace_after, self.empty_after, self.fail_after = 0, None, None, None
        for k, v in over.items():
            setattr(self, k, v)

    def read_state(self):
        pods = self.pods or [{"name": ACTIVE_POD, "uid": "ua", "hash": "aaa111", "ready": True, "terminating": False}]
        return {"template": self.tpl, "pods": pods, "active_hash": "aaa111", "preview_hash": "aaa111",
                "chaos_names": list(self.chaos_listed), "net_probe_body": prom_body(0.004), "policy": dict(self.policy), "delay": 30}

    def read_end_state(self, results_dir):
        markers = sorted(p.name for p in Path(results_dir).glob("running-*.json"))
        return cl.capture_state(self.tpl, 30, self.policy, self.chaos_listed, markers)

    def read_chaos_cr(self, name):
        return copy.deepcopy(self.crs.get(name))

    def protected_pods(self, created_hash):
        self.protected_calls += 1
        if created_hash != "bbb222":
            return []
        if self.fail_after and self.protected_calls > self.fail_after:
            raise TimeoutError("kubectl timeout")
        if self.empty_after and self.protected_calls > self.empty_after:
            return []
        return ["vllm-serving-ccc333-z"] if self.replace_after and self.protected_calls > self.replace_after else [NEW_POD]

    def protected_probe_seconds(self, pods, t0, t1):
        """요청한 구간만 5 s 간격으로 돌려준다 - 조회 구간이 실제로 쓰이는지 시험이 볼 수 있게 한다."""
        a, b = datetime.fromisoformat(t0).timestamp(), datetime.fromisoformat(t1).timestamp()
        self.queries.append((a, b))
        return {p: [[a + 5.0 * k, self.leak] for k in range(int((b - a) // 5) + 1)] for p in pods}

    def wait_network_chaos_gone(self, names, **_):
        return list(self.gone_left)

    def chaos_fns(self):
        def create(cr_name, run_id, arm, target, stage, duration=None):
            self.crs[cr_name] = cr_body(cr_name, run_id, arm, target, stage, duration, **self.cr_kwargs)
        return create, lambda n: self.crs.pop(n, None), lambda n: n in self.crs


class NetInjector(tgp.FakeInjector):
    """주입기 자리: 단계 4개를 기록기로 만들고(적용 확인) 지운다. 클러스터에는 닿지 않는다."""

    def __init__(self, rec, target=ACTIVE_POD):
        super().__init__()
        self.rec, self.target = rec, target

    def inject(self):
        super().inject()
        for i, st in enumerate(nc.NETWORK_STAGES):
            name = f"netdelay-s{i}"
            self.rec.create(name, "rid", "arm", self.target, st)
            if i == 0:
                assert self.rec.is_injected(name)              # 실제 주입기의 is_started는 첫 단계에서 굳는다 - 이후 단계는 확인하지 않는다
            self.rec.delete(name)


def hooks_for(net):
    return {"chaos": net.chaos_fns(), "base_injector": lambda run_id, rep, rec: NetInjector(rec),
            "wrap": lambda inj, cleanup: arm_controller.wrap_injector_with_preview_prep(
                inj, gp.rt.ARM_LABEL, prepare_preview_fn=lambda *a: dict(PREP), cleanup_preview_fn=cleanup)}


def write_profile(tmp_path, tpl):
    obs = nc.observe_environment(tpl)
    prof = nc.build_selection_profile({k: v for k, v in obs.items() if k not in ("probe_profile", "pressure")},
                                      nc.contract_hashes()["recovery_policy_sha256"], "f" * 64)
    p = tmp_path / "net-profile.json"
    p.write_text(json.dumps(prof, indent=2), encoding="utf-8")
    return p


def fast_stages(monkeypatch, sec=0):
    monkeypatch.setattr(nc, "NETWORK_STAGES", tuple({**s, "duration_sec": sec} for s in nc.NETWORK_STAGES))


def session(tmp_path, monkeypatch, env, run="n2p01", net_over=None, tpl=None, preflight_only=False, deps_over=None, hooks_over=None,
            stage_sec=0):
    fast_stages(monkeypatch, stage_sec)
    w = tgp.World(tmp_path / "w", monkeypatch, mode=gp.MODE_RO, send_at=(), wrap_preview=True)
    w.results = tmp_path / "net-results"
    w.results.mkdir()
    profile = write_profile(tmp_path, template())                  # 프로필은 항상 11 s 환경 기준(관측이 달라지면 STALE)
    net = FakeNet(profile, tpl, **(net_over or {}))
    inst = ns1.load_stage1(ns1.NetConfig(results_dir=w.results))
    hooks = {**hooks_for(net), **(hooks_over or {})}
    sha = nc.load_profile(profile, stage="stage1")["sha256"]
    kw = dict(deps=w.deps(**(deps_over or {})), starter=lambda rd: w.detector.starter_factory(rd, "http://p", "guarded-v1", "record_only"),
              art_hashes={"scaler.pkl": "x" * 8}, profile_sha=sha, results_dir=w.results, net=net, hooks=hooks,
              preflight_only=preflight_only)
    return w, net, inst, kw


# ---------- (1) 정상 세션 ----------
def test_pressure_session_completes_and_records_delivery_evidence(tmp_path, monkeypatch, env):
    w, net, inst, kw = session(tmp_path, monkeypatch, env)
    s = ns2.run_pressure_net(inst, "n2p01", **kw)
    assert s["outcome_status"] == "completed" and s["cleanup_errors"] == [], s["cleanup_errors"]
    assert w.promotes == [] and w.overrides == [] and w.delay == 30 and w.policy.snapshot()["context"] is None
    d = s["network_delivery"]
    assert [c["stage"] for c in d["crs"]] == [x["name"] for x in nc.NETWORK_STAGES] and d["targets"] == [ACTIVE_POD]
    assert all(c["duration"] == c["actual"]["spec"]["duration"] == f"{int(c['stage_def']['duration_sec'] + 60)}s" and c["injected_at"]
               and c["deleted_at"] for c in d["crs"])                                      # 단계 시간 + 60 s 안전망, 실제 저장값 확인
    obs = s["network_protected_pod_observations"]
    assert obs and all(o["pods"] == [NEW_POD] and o["error"] is None for o in obs)
    assert s["network_delivery_check"] == {"problems": [], "container_records_present": True}
    q0, q1 = net.queries[-1]                                                                  # 조회 구간 = [첫 생성 - 30 s, 마지막 삭제 + 45 s]
    assert abs(q0 - (ns2._epoch(d["first_created_at"]) - ns2.QUERY_PREROLL_SEC)) < 1 and abs(q1 - (ns2._epoch(d["last_deleted_at"]) + ns2.QUERY_TAIL_SEC)) < 1
    assert all(c["injected_at"] for c in d["crs"])                          # 4단계 모두 적용 확인(감시 스레드) - 주입기가 첫 단계만 확인해도
    assert not list(w.results.glob("running-*.json")) and s["detector_started"] is True
    c = json.loads(next(w.results.glob("contract-*.json")).read_text(encoding="utf-8"))
    assert c["kind"] == "pressure" and c["signal_send"] is False and c["conditions"]["stress_stages"] == list(nc.NETWORK_STAGES)
    assert c["conditions"]["probe_profile"] == nc.PROBE_PROFILE and c["net_probe"]["threshold"] == nc.NET_THRESHOLD
    pf = json.loads(next(w.results.glob("preflight-*-run-*.json")).read_text(encoding="utf-8"))
    assert pf["ok"] is True and pf["network"]["ok"] is True and pf["network"]["target"] == ACTIVE_POD


def test_preflight_only_changes_nothing(tmp_path, monkeypatch, env):
    w, net, inst, kw = session(tmp_path, monkeypatch, env, preflight_only=True)
    r = ns2.run_pressure_net(inst, "n2p01", **kw)
    assert r["preflight_only"] is True and r["preflight"]["network"]["ok"] is True
    assert w.injector.calls == [] and net.crs == {} and not list(w.results.glob("contract-*")) and not list(w.results.glob("running-*"))


# ---------- (2) 위험한 시작 상태는 세션 전에 막는다 ----------
@pytest.mark.parametrize("net_over,tpl,needle", [
    ({}, template(1, 1), "PROFILE_STALE"),                                              # probe 제한 1 s로 되돌아감 -> 환경이 프로필과 다름 + 재시작 연쇄 위험
    ({}, template(1, 1), "network_tolerant"),
    ({"chaos_listed": ["netdelay-old"]}, None, "network_chaos_present"),                # 남은 지연 설정
    ({"policy": {**CLEAN_POLICY, "mode": "act"}}, None, "policy_mode"),                # act 모드로 시작 금지
    ({"pods": [{"name": ACTIVE_POD, "uid": "ua", "hash": "aaa111", "ready": True, "terminating": False},
               {"name": NEW_POD, "uid": "ub", "hash": "bbb222", "ready": True, "terminating": False}]}, None, "unexpected_extra_pods"),
])
def test_unsafe_start_refused_before_any_cluster_change(tmp_path, monkeypatch, env, net_over, tpl, needle):
    w, net, inst, kw = session(tmp_path, monkeypatch, env, net_over=net_over, tpl=tpl)
    with pytest.raises(SystemExit, match="사전검사 실패") as e:
        ns2.run_pressure_net(inst, "n2p01", **kw)
    assert needle in str(e.value)
    assert w.injector.calls == [] and net.crs == {} and not list(w.results.glob("running-*")) and not list(w.results.glob("contract-*"))


def test_profile_changed_since_session_start_refused(tmp_path, monkeypatch, env):
    w, net, inst, kw = session(tmp_path, monkeypatch, env)
    kw["profile_sha"] = "0" * 64
    with pytest.raises(SystemExit, match="profile 해시가 세션 시작 때와 다름"):
        ns2.run_pressure_net(inst, "n2p01", **kw)
    assert w.injector.calls == []


def test_run_id_reuse_is_refused(tmp_path, monkeypatch, env):
    w, net, inst, kw = session(tmp_path, monkeypatch, env)
    ns2.run_pressure_net(inst, "n2p01", **kw)
    with pytest.raises(SystemExit, match="실행 ID 재사용 금지"):
        ns2.run_pressure_net(inst, "n2p01", **kw)


# ---------- (3) 전달 증거가 어긋나면 무효(세션 자체는 정리까지 끝난다) ----------
@pytest.mark.parametrize("net_over,needle", [
    ({"leak": 0.5}, "probe_duration_mean_max"),                                                  # 지연이 새 Pod로 샘
    ({"cr_kwargs": {"selector": {"labelSelectors": {"app": "vllm"}}}}, "selector_has_other_selectors"),   # 라벨 선택 = 여러 Pod 대상
    ({"cr_kwargs": {"records": False}}, None),                                              # containerRecords 없음은 사실 기록(무효 아님)
])
def test_delivery_problems_make_session_invalid_but_cleanup_complete(tmp_path, monkeypatch, env, net_over, needle):
    w, net, inst, kw = session(tmp_path, monkeypatch, env, net_over=net_over)
    s = ns2.run_pressure_net(inst, "n2p01", **kw)
    assert s["cleanup_errors"] == [] and w.promotes == []
    chk = s["network_delivery_check"]
    if needle is None:
        assert chk == {"problems": [], "container_records_present": False}
    else:
        assert any(needle in p for p in chk["problems"]), chk


def test_new_pod_never_captured_is_invalid(tmp_path, monkeypatch, env):
    w, net, inst, kw = session(tmp_path, monkeypatch, env)
    net.protected_pods = lambda h: []
    s = ns2.run_pressure_net(inst, "n2p01", **kw)
    assert all(o["pods"] == [] for o in s["network_protected_pod_observations"])
    assert any("no_protected_pod" in p for p in s["network_delivery_check"]["problems"])


def test_stage_cut_short_is_invalid(tmp_path, monkeypatch, env):
    # 계획 5 s 단계를 주입기가 즉시 지운다(정리가 대기를 끊은 것과 같은 모양) -> 단계 유지 시간 미달
    w, net, inst, kw = session(tmp_path, monkeypatch, env, stage_sec=5)
    s = ns2.run_pressure_net(inst, "n2p01", **kw)
    cut = [p for p in s["network_delivery_check"]["problems"] if "stage_cut_short" in p]
    assert len(cut) == len(nc.NETWORK_STAGES) and "planned 5s" in cut[0]


def test_new_pod_replaced_during_injection_is_invalid(tmp_path, monkeypatch, env):
    w, net, inst, kw = session(tmp_path, monkeypatch, env, net_over={"replace_after": 1})       # 두 번째 확인부터 다른 새 Pod
    s = ns2.run_pressure_net(inst, "n2p01", **kw)
    assert len({tuple(o["pods"]) for o in s["network_protected_pod_observations"]}) >= 2
    assert any("protected_pods_changed_during_injection" in p for p in s["network_delivery_check"]["problems"])


def test_new_pod_disappearing_is_a_change_not_ignored(tmp_path, monkeypatch, env):
    """성공한 관측이 [새 Pod] 다음에 [](사라짐)이면 같은 집합으로 취급하지 않는다(Codex 2차 지적)."""
    w, net, inst, kw = session(tmp_path, monkeypatch, env, net_over={"empty_after": 1})
    s = ns2.run_pressure_net(inst, "n2p01", **kw)
    assert any("protected_pods_changed_during_injection" in p for p in s["network_delivery_check"]["problems"])


def test_lookup_failures_are_recorded_and_only_matter_when_a_stage_loses_all_observations(tmp_path, monkeypatch, env):
    w, net, inst, kw = session(tmp_path, monkeypatch, env, net_over={"fail_after": 1})            # 첫 관측만 성공, 이후 조회는 모두 실패
    s = ns2.run_pressure_net(inst, "n2p01", **kw)
    obs = s["network_protected_pod_observations"]
    assert obs[0]["error"] is None and all(o["pods"] is None and "TimeoutError" in o["error"] for o in obs[1:]) and len(obs) >= 2
    probs = s["network_delivery_check"]["problems"]
    assert not any("changed" in p for p in probs)                                                  # 실패는 "바뀜"이 아니다
    # 첫 성공 관측이 모든 단계(수 ms 안)를 덮으므로 이 가짜 세션에서는 단계 관측 공백이 생기지 않는다 - 공백 규칙은 단위 시험에서 확인
    assert probs == []


def test_protected_pod_probe_telemetry_missing_for_a_stage_is_invalid(tmp_path, monkeypatch, env):
    w, net, inst, kw = session(tmp_path, monkeypatch, env)
    net.protected_probe_seconds = lambda pods, t0, t1: {p: [] for p in pods}                  # 새 Pod의 probe 값이 전혀 없음
    s = ns2.run_pressure_net(inst, "n2p01", **kw)
    assert sum("unverifiable_in_stage" in p for p in s["network_delivery_check"]["problems"]) == len(nc.NETWORK_STAGES)


def test_postinjection_readback_failure_is_not_replaced_by_creation_snapshot(tmp_path, monkeypatch, env):
    w, net, inst, kw = session(tmp_path, monkeypatch, env)
    real_read, calls = net.read_chaos_cr, {}

    def read(name):                                         # 생성 직후 읽기만 성공, 적용 뒤 읽기는 모두 실패
        calls[name] = calls.get(name, 0) + 1
        if calls[name] == 1:
            return real_read(name)
        raise TimeoutError("api timeout")
    net.read_chaos_cr = read
    s = ns2.run_pressure_net(inst, "n2p01", **kw)
    probs = s["network_delivery_check"]["problems"]
    assert sum("cr_readback_missing_after_injection" in p for p in probs) == len(nc.NETWORK_STAGES)
    assert all(c["created"] for c in s["network_delivery"]["crs"])


BASE = 1_760_000_000.0


def iso_at(sec):
    return datetime.fromtimestamp(sec, timezone.utc).isoformat()


def good_delivery():
    crs = []
    for i, st in enumerate(nc.NETWORK_STAGES):
        b = cr_body(f"c{i}", "r", "a", ACTIVE_POD, st, "150s")
        crs.append({"name": f"c{i}", "stage": st["name"], "target_pod": ACTIVE_POD, "duration": "150s", "expected": b, "created": b,
                    "actual": copy.deepcopy(b), "injected_at": iso_at(BASE + 100 * i), "deleted_at": iso_at(BASE + 100 * i + 90),
                    "created_at": iso_at(BASE + 100 * i - 1), "closed": True, "violations": [], "lifecycle_problems": [],
                    "watcher_alive_at_delete": False})
    return {"crs": crs, "targets": [ACTIVE_POD]}


def good_samples(leak_at=None, drop_stage=None, value=0.01, until=120):
    """단계 i의 구간 [100i, 100i+90] + 꼬리를 15 s 간격으로 덮는다. leak_at = 값을 올릴 시각(BASE 기준 초)."""
    out = []
    for i in range(len(nc.NETWORK_STAGES)):
        if i == drop_stage:
            continue
        out += [[BASE + 100 * i + k, 0.01] for k in range(0, until + 1, 15)]
    if leak_at is not None:
        out = [[x, value if x == BASE + leak_at else v] for x, v in out]
    return {NEW_POD: out}


def good_obs():
    return [{"t": BASE + 100 * i + 10, "pods": [NEW_POD], "error": None} for i in range(len(nc.NETWORK_STAGES))]


def test_delivery_problems_unit_cases():
    good = good_delivery()
    assert ns2.delivery_problems(good, good_samples(), good_obs()) == {"problems": [], "container_records_present": True}
    bad = copy.deepcopy(good)
    bad["crs"][1]["actual"] = None                                                           # 적용 뒤 설정 읽기 실패
    bad["crs"][2]["deleted_at"] = None                                                       # 실행기가 지우지 않음
    bad["crs"][3]["actual"]["spec"].pop("duration")                                          # 안전망이 저장되지 않음
    p = ns2.delivery_problems(bad, good_samples(), good_obs())["problems"]
    assert any("cr_readback_missing_after_injection" in x for x in p) and any("not_deleted_by_runner" in x for x in p)
    assert any("duration_not_stored" in x for x in p)
    short = {"crs": good["crs"][:3], "targets": [ACTIVE_POD]}
    assert any("stages" in x for x in ns2.delivery_problems(short, good_samples(), good_obs())["problems"])   # 4단계 미만
    two = {**good, "targets": [ACTIVE_POD, NEW_POD]}
    assert any("대상 Pod 1개" in x for x in ns2.delivery_problems(two, good_samples(), good_obs())["problems"])
    other = copy.deepcopy(good)
    other["crs"][0]["actual"]["status"]["experiment"]["containerRecords"][0]["id"] = f"vllm-serving/{NEW_POD}"
    assert any("containerRecords_other_pods" in x for x in ns2.delivery_problems(other, good_samples(), good_obs())["problems"])
    wrong_created = copy.deepcopy(good)                                                      # 생성 직후 값이 다른 Pod를 가리킴
    wrong_created["crs"][0]["created"]["spec"]["selector"]["pods"] = {"vllm-serving": [NEW_POD]}
    assert any("created:selector.pods" in x for x in ns2.delivery_problems(wrong_created, good_samples(), good_obs())["problems"])
    assert ns2.delivery_problems(None, good_samples(), good_obs())["problems"] == ["no_delivery_record"]
    short_held = copy.deepcopy(good)                                                         # 90 s 단계가 30 s 만에 지워짐
    short_held["crs"][2]["deleted_at"] = iso_at(BASE + 200 + 30)
    assert any("c2: stage_cut_short(" in x for x in ns2.delivery_problems(short_held, good_samples(), good_obs())["problems"])
    lifecycle = copy.deepcopy(good)
    lifecycle["crs"][0].update(watcher_alive_at_delete=True, lifecycle_problems=["lifecycle_lock_timeout"], violations=["actual:x"])
    p = ns2.delivery_problems(lifecycle, good_samples(), good_obs())["problems"]
    assert any("watcher_still_running_after_delete" in x for x in p) and any("lifecycle_lock_timeout" in x for x in p) and any("actual:x" in x for x in p)


def test_protected_observations_rules():
    good = good_delivery()
    assert ns2.protected_observation_problems(good, good_obs()) == ([], [NEW_POD])
    assert ns2.protected_observation_problems(good, [])[0] and ns2.protected_observation_problems(good, [])[1] == []
    empty_only = [{"t": BASE + 10, "pods": [], "error": None}]
    assert "no_protected_pod_captured" in ns2.protected_observation_problems(good, empty_only)[0][0]
    vanished = good_obs() + [{"t": BASE + 150, "pods": [], "error": None}]                      # 성공한 빈 관측 = 사라짐 = 바뀜
    assert any("changed" in x for x in ns2.protected_observation_problems(good, vanished)[0])
    replaced = good_obs() + [{"t": BASE + 250, "pods": ["other"], "error": None}]
    assert any("changed" in x for x in ns2.protected_observation_problems(good, replaced)[0])
    after = good_obs() + [{"t": BASE + 100 * 3 + 90 + 500, "pods": ["other"], "error": None}]   # 주입이 모두 끝난 한참 뒤의 교체는 격리 증거가 아니다
    assert ns2.protected_observation_problems(good, after) == ([], [NEW_POD])
    before = [{"t": BASE - 3600, "pods": ["other"], "error": None}] + good_obs()                # 주입 전의 다른 값도 마찬가지
    assert ns2.protected_observation_problems(good, before) == ([], [NEW_POD])
    only_errors = good_obs()[:1] + [{"t": BASE + 100 * i + 10, "pods": None, "error": "TimeoutError"} for i in range(1, 4)]
    probs = ns2.protected_observation_problems(good, only_errors)[0]
    assert sum("unobserved_in_stage" in x for x in probs) >= 1                                  # 조회 실패로 관측이 비는 단계는 공백


def test_protected_probe_must_cover_every_stage_with_timestamps():
    good = good_delivery()
    assert ns2.protected_coverage_problems(good, good_samples(), [NEW_POD]) == []
    leak = ns2.protected_coverage_problems(good, good_samples(leak_at=230, value=0.5), [NEW_POD])
    assert len(leak) == 1 and "stage-3" in leak[0] and "probe_duration_mean_max" in leak[0]    # 샌 단계가 정확히 짚인다
    tail = ns2.protected_coverage_problems(good, good_samples(leak_at=105, value=0.5), [NEW_POD])   # 단계 2 시작 직후(= 단계 1 삭제 뒤 꼬리)에 값이 올라간 경우
    assert any("stage-1" in x for x in tail) and any("stage-2" in x for x in tail)               # 삭제 요청 뒤 꼬리 구간 값도 본다(보수적으로 양쪽 모두 짚음)
    gap = ns2.protected_coverage_problems(good, good_samples(drop_stage=1), [NEW_POD])
    assert gap and all("stage-2" in x for x in gap) and any(("unobserved_gap" in x) or ("unverifiable" in x) for x in gap)   # 한 단계의 관측 공백 = 확인 불가
    early_only = good_samples()
    early_only[NEW_POD] = [x for x in early_only[NEW_POD] if not (BASE <= x[0] <= BASE + 100 and x[0] > BASE + 30)]   # 단계 1: 앞 3점만 있고 뒤는 사라짐
    vanish = ns2.protected_coverage_problems(good, early_only, [NEW_POD])
    assert any("stage-1" in x and "unobserved_gap" in x for x in vanish)                      # 3점 이상이어도 나머지 구간이 비면 통과하지 못한다
    no_tail = {NEW_POD: [x for x in good_samples()[NEW_POD] if x[0] < BASE + 300 + 40]}      # 마지막 단계: 시작 직후 값만 있고 끝 쪽이 비어 있음
    assert any("stage-4" in x and "no_sample_after_stage_end" in x for x in ns2.protected_coverage_problems(good, no_tail, [NEW_POD]))
    only45 = {NEW_POD: [x for x in good_samples()[NEW_POD] if x[0] < BASE + 300 or x[0] <= BASE + 300 + 45]}   # 마지막 단계: 0,15,30,45 s만 있고 끝·꼬리가 비어 있음
    assert any("stage-4" in x and "no_sample_after_stage_end" in x for x in ns2.protected_coverage_problems(good, only45, [NEW_POD]))
    before_only = {NEW_POD: [[BASE - 200 + 15 * k, 0.01] for k in range(12)]}                  # 값은 많지만 모두 주입 전 시각
    assert len(ns2.protected_coverage_problems(good, before_only, [NEW_POD])) == len(nc.NETWORK_STAGES)
    nan = good_samples()
    nan[NEW_POD][1][1] = float("nan")
    assert any("unverifiable_in_stage" in x for x in ns2.protected_coverage_problems(good, nan, [NEW_POD]))
    assert ns2.protected_coverage_problems(good, {}, [NEW_POD]) and ns2.protected_coverage_problems(good, good_samples(), []) == []


# ---------- 새 Pod 누출 배제(사용자 결정 2026-10-17): 실패는 감지기 실패가 아니라 세션 무효 ----------
def one_stage():
    d = good_delivery()
    return {"crs": d["crs"][:1], "targets": d["targets"]}                                         # 단계 하나 [BASE, BASE+90] (+ 삭제 뒤 30 s까지 본다)


def window_samples(times, value=0.01):
    return {NEW_POD: [[BASE + t, value] for t in times]}


def test_leakage_rule_mean_over_0_1_is_invalid_and_exactly_0_1_is_valid():
    assert ns2.protected_coverage_problems(one_stage(), window_samples([0, 45, 90], 0.1), [NEW_POD]) == []                  # 0.1 이하
    bad = ns2.protected_coverage_problems(one_stage(), window_samples([0, 45, 90], 0.1000001), [NEW_POD])
    assert len(bad) == 1 and "probe_duration_mean_max" in bad[0]
    mixed = {NEW_POD: [[BASE, 0.01], [BASE + 45, 0.5], [BASE + 90, 0.01]]}                                                   # 한 점이라도 넘으면 무효
    assert any("probe_duration_mean_max" in p for p in ns2.protected_coverage_problems(one_stage(), mixed, [NEW_POD]))


def test_leakage_rule_needs_at_least_3_valid_samples():
    assert ns2.protected_coverage_problems(one_stage(), window_samples([0, 45, 90]), [NEW_POD]) == []                      # 정확히 3개
    two = ns2.protected_coverage_problems(one_stage(), window_samples([0, 90]), [NEW_POD])
    assert len(two) == 1 and "unverifiable_in_stage(2/2 finite, need 3)" in two[0]
    nan = {NEW_POD: [[BASE, 0.01], [BASE + 45, float("nan")], [BASE + 90, 0.01]]}                                           # 유한하지 않은 값은 유효 샘플이 아니다
    assert any("unverifiable_in_stage" in p for p in ns2.protected_coverage_problems(one_stage(), nan, [NEW_POD]))
    assert any("unverifiable_in_stage" in p for p in ns2.protected_coverage_problems(one_stage(), {}, [NEW_POD]))             # 값이 없으면 확인 불가


def test_leakage_rule_max_gap_45_is_valid_and_over_45_is_invalid():
    assert ns2.protected_coverage_problems(one_stage(), window_samples([0, 45, 90]), [NEW_POD]) == []                      # 공백 정확히 45 s
    gap = ns2.protected_coverage_problems(one_stage(), window_samples([0, 46, 92]), [NEW_POD])
    assert len(gap) == 1 and "unobserved_gap_in_stage" in gap[0]
    late = ns2.protected_coverage_problems(one_stage(), window_samples([50, 70, 90]), [NEW_POD])                           # 단계 시작부터 첫 값까지의 공백도 센다
    assert any("unobserved_gap_in_stage" in p for p in late)
    no_tail = ns2.protected_coverage_problems(one_stage(), window_samples([0, 15, 30, 45]), [NEW_POD])                     # 삭제 뒤 값이 없다
    assert any("no_sample_after_stage_end" in p for p in no_tail)


def test_leakage_rule_all_three_satisfied_over_a_full_session_is_valid():
    probs = ns2.delivery_problems(good_delivery(), good_samples(), good_obs())["problems"]
    assert probs == [] and ns2.isolation_reasons(probs) == []
    assert ns2.validity_flags([]) == {"isolation_not_established": False, "counts_as_detector_failure": False, "detector_gate_inclusion": "included"}


def test_leakage_failure_is_session_invalid_never_a_detector_failure():
    probs = ns2.delivery_problems(good_delivery(), good_samples(leak_at=45, value=0.5), good_obs())["problems"]
    why = ns2.isolation_reasons(probs)
    assert why and why[0].startswith("SESSION INVALID — target isolation / new-Pod leakage exclusion not established")
    flags = ns2.validity_flags(why + [f"N3_{p}" for p in probs])
    assert flags == {"isolation_not_established": True, "counts_as_detector_failure": False, "detector_gate_inclusion": "excluded"}
    for markers_case in (ns2.delivery_problems(good_delivery(), good_samples(drop_stage=1), good_obs())["problems"],               # 값 공백
                         ns2.delivery_problems(good_delivery(), good_samples(), [])["problems"],                                    # 새 Pod 관측 없음
                         ns2.delivery_problems(good_delivery(), good_samples(), good_obs() + [{"t": BASE + 150, "pods": [], "error": None}])["problems"]):  # 새 Pod 사라짐
        assert ns2.isolation_reasons(markers_case)                                                                                  # 모두 같은 무효 분류
    other = good_delivery()
    other["crs"][2]["deleted_at"] = None                                                                                            # 격리와 무관한 전달 문제
    probs = ns2.delivery_problems(other, good_samples(), good_obs())["problems"]
    assert probs and ns2.isolation_reasons(probs) == []                                                                             # 무효이지만 '격리 확립 실패' 라벨은 아니다


# ---------- 정리 확인 ----------
def test_residual_chaos_after_session_keeps_marker_and_reports(tmp_path, monkeypatch, env):
    w, net, inst, kw = session(tmp_path, monkeypatch, env, net_over={"gone_left": ["netdelay-s3"]})
    s = ns2.run_pressure_net(inst, "n2p01", **kw)
    assert any("network_chaos_residual" in e for e in s["cleanup_errors"])
    assert list(w.results.glob("running-*.json"))                                              # 정리가 안 끝나면 표식을 남겨 다음 세션을 막는다


def test_a_resume_authorized_earlier_attempts_marker_is_not_a_cleanup_error_but_any_other_marker_is(tmp_path, monkeypatch, env):
    """n2p01에서 확인한 결함: 이미 사람이 재개를 승인한 n2n01의 '실행 중 표시'가 압박 세션 끝 점검에서 정리 오류로 세졌다."""
    w, net, inst, kw = session(tmp_path, monkeypatch, env)
    kw["deps"].check_unresolved = lambda: inst.check_unresolved_stage1(w.results)       # 실제 실행(n2_run)과 같은 시작 관문: 재개 승인된 시도만 해소로 본다
    (w.results / "running-guarded_normal_record_only-n2n01-x.json").write_text("{}", encoding="utf-8")
    (w.results / "p2-session-n2n01.json").write_text(json.dumps({"run": "n2n01", "run_id": "guarded_normal_record_only-n2n01-x",
                                                                  "attempt_number": 1, "kind": "normal", "cleanup_errors": ["x"]}), encoding="utf-8")
    monkeypatch.setattr(inst, "resume_authorized", lambda d, r: r["run"] == "n2n01")        # 재개가 승인된 시도만 해소로 본다(승인 없는 표식은 세션 시작 단계에서 이미 거부된다)
    s = ns2.run_pressure_net(inst, "n2p01", **kw)
    assert not any("running_marker_left" in e for e in s["cleanup_errors"]), s["cleanup_errors"]


def test_every_run_id_fits_the_kubernetes_label_limit_and_normal_ids_are_unchanged():
    """n2p01 결함: 압박 실행 ID가 67자라 NetworkChaos 라벨('experiment-run-id', 63자 한도) 생성이 거부됐다."""
    inst = ns1.load_stage1()
    ids = [inst.run_id_for(r) for r in (*inst.NORMAL_RUNS, *inst.PRESSURE_RUNS)]
    assert all(len(i) <= 63 for i in ids) and len(set(ids)) == len(ids)
    assert inst.run_id_for("n2n01") == "guarded_normal_record_only-n2n01-capstone-phase2-network-env-v1"        # 이미 기록된 정상 세션 ID는 그대로
    assert inst.run_id_for("n2p01") == "guarded_pressure_record_only-n2p01-cap-p2-net-v1"
    assert all(c.isalnum() or c in "-_." for c in inst.run_id_for("n2p01"))


def _early(n=1, **over):
    d = good_delivery()
    d["crs"] = d["crs"][:n]
    for k, v in over.items():
        d["crs"][-1][k] = v
    return d


def test_early_recovery_rule_allows_stopping_after_a_verified_promotion_but_only_for_what_rev5_says():
    """rev5 §6.2.1(Stage 2, 승격 확인된 세션): 지연이 승격 확인 시각까지 유지되면 이후 단계는 생략 가능. 승격이 없으면 기존 규칙(4단계 전부)."""
    ok_t = iso_at(BASE + 50)                                                            # 승격·Service 전환 확인 시각(1단계 한가운데)
    assert ns2.delivery_problems(_early(1), good_samples(), good_obs(), recovery_verified_at=ok_t) == {"problems": [], "container_records_present": True}
    strict = ns2.delivery_problems(_early(1), good_samples(), good_obs())               # 승격이 없으면(None) 같은 증거도 무효 - 기존 규칙 불변
    assert any("stages" in x for x in strict["problems"])
    assert ns2.delivery_problems(_early(2), good_samples(), good_obs(), recovery_verified_at=ok_t)["problems"] == []      # 앞부분 2단계도 허용


def test_early_recovery_rule_still_requires_the_delay_to_last_until_the_promotion_is_verified():
    late = ns2.delivery_problems(_early(1), good_samples(), good_obs(), recovery_verified_at=iso_at(BASE + 95))["problems"]     # 삭제(BASE+90) 뒤에 확인됨
    assert any("delay_ended_before_promotion_verified" in x for x in late)
    cut_ok = _early(1, deleted_at=iso_at(BASE + 60))                                        # 마지막 단계가 계획(90 s)보다 짧게 끝났지만 확인(BASE+50) 뒤
    assert ns2.delivery_problems(cut_ok, good_samples(), good_obs(), recovery_verified_at=iso_at(BASE + 50))["problems"] == []
    cut_bad = ns2.delivery_problems(cut_ok, good_samples(), good_obs(), recovery_verified_at=iso_at(BASE + 75))["problems"]       # 확인 전에 끊김
    assert any("stage_cut_short" in x for x in cut_bad) and any("delay_ended_before_promotion_verified" in x for x in cut_bad)


def test_early_recovery_rule_does_not_excuse_skipped_reordered_or_cut_earlier_stages_or_leaks():
    ok_t = iso_at(BASE + 50)
    d = good_delivery()
    skipped = {**d, "crs": d["crs"][1:2]}                                                # 1단계를 건너뛰고 2단계만
    assert any("leading prefix" in x for x in ns2.delivery_problems(skipped, good_samples(), good_obs(), recovery_verified_at=ok_t)["problems"])
    two = good_delivery()
    two["crs"] = two["crs"][:2]
    two["crs"][0]["deleted_at"] = iso_at(BASE + 40)                                      # 마지막이 아닌 단계는 계획 시간을 채워야 한다
    assert any("stage_cut_short" in x for x in ns2.delivery_problems(two, good_samples(), good_obs(), recovery_verified_at=iso_at(BASE + 105))["problems"])
    leak = ns2.delivery_problems(_early(1), good_samples(leak_at=15, value=0.5), good_obs(), recovery_verified_at=ok_t)["problems"]
    assert any("probe_duration_mean_max" in x for x in leak)                              # 새 Pod 유출 검사는 완화되지 않는다
    assert ns2.delivery_problems({"crs": [], "targets": []}, {}, [], recovery_verified_at=ok_t)["problems"] == ["no_delivery_record"]


def test_collect_delivery_evidence_records_which_n3_rule_was_used():
    class Rec:
        def record(self):
            d = _early(1)
            return {**d, "first_created_at": d["crs"][0]["created_at"], "last_deleted_at": d["crs"][0]["deleted_at"]}

    class Watch:
        obs = good_obs()

    class Net:
        def protected_probe_seconds(self, names, q0, q1):
            return good_samples()
    strict = ns2.collect_delivery_evidence(Rec(), Watch(), Net())
    early = ns2.collect_delivery_evidence(Rec(), Watch(), Net(), recovery_verified_at=iso_at(BASE + 50))
    assert strict["n3_rule"] == "strict" and strict["recovery_verified_at"] is None and strict["network_delivery_check"]["problems"]
    assert early["n3_rule"] == "early_recovery_rev5" and early["network_delivery_check"]["problems"] == []


def test_session_end_state_problems_are_reported(tmp_path, monkeypatch, env):
    w, net, inst, kw = session(tmp_path, monkeypatch, env)
    real_end = net.read_end_state
    net.read_end_state = lambda d: {**real_end(d), "network_chaos_names": ["netdelay-left"]}       # 끝났는데 지연 설정이 남음
    s = ns2.run_pressure_net(inst, "n2p01", **kw)
    assert any("network_session_end" in e and "network_chaos_residual" in e for e in s["cleanup_errors"])


def test_harness_error_in_trial_still_cleans_up_and_reports(tmp_path, monkeypatch, env):
    def boom(*a, **k):
        raise RuntimeError("trial exploded")
    w, net, inst, kw = session(tmp_path, monkeypatch, env, deps_over={"trial_pressure": boom})
    s = ns2.run_pressure_net(inst, "n2p01", **kw)
    assert s["outcome_status"] == "harness_error" and any("trial exploded" in e for e in s["cleanup_errors"])
    assert s["network_delivery_check"]["problems"]                                             # 전달 증거 없음 = 무효 사유
    assert list(w.results.glob("running-*.json"))


# ---------- 기록기 ----------
def make_recorder(store, calls=None, **kw):
    def create(name, run_id, arm, target, stage, duration=None):
        if calls is not None:
            calls.append((name, duration))
        store[name] = cr_body(name, run_id, arm, target, stage, duration)
    return ns2.DeliveryRecorder(create_fn=kw.pop("create_fn", create), delete_fn=kw.pop("delete_fn", lambda n: store.pop(n, None)),
                                injected_fn=kw.pop("injected_fn", lambda n: n in store), read_fn=kw.pop("read_fn", lambda n: store.get(n)),
                                poll_sec=0.01, **kw)


def test_recorder_adds_duration_safety_net_and_reads_back_after_injection():
    calls, store = [], {}
    rec = make_recorder(store, calls)
    st = nc.NETWORK_STAGES[0]
    rec.create("c0", "r", "a", ACTIVE_POD, st)
    assert calls == [("c0", "150s")] and rec.crs["c0"]["created"]["spec"]["duration"] == "150s"
    assert rec.is_injected("c0") and rec.crs["c0"]["injected_at"] is not None and rec.crs["c0"]["actual"]["spec"]["duration"] == "150s"
    first = rec.crs["c0"]["injected_at"]
    assert rec.is_injected("c0") and rec.crs["c0"]["injected_at"] == first                      # 처음 확인한 시각을 유지
    rec.delete("c0")
    rec.delete("c0")                                                                            # 두 번째 삭제는 안전하고 증거를 바꾸지 않는다
    c = rec.crs["c0"]
    assert rec.record()["last_deleted_at"] == c["deleted_at"] and rec.planned_cr_names() == ["c0"] and c["closed"]
    assert c["watcher_alive_at_delete"] is False and not rec._watch["c0"][0].is_alive()


def test_watcher_marks_later_stages_even_if_nobody_else_checks():
    """실제 주입기는 첫 단계 뒤 적용 확인을 다시 부르지 않는다 - 기록기가 단계마다 스스로 확인해야 한다(Codex 1차 지적)."""
    store = {}
    rec = make_recorder(store)
    for i, st in enumerate(nc.NETWORK_STAGES):
        rec.create(f"c{i}", "r", "a", ACTIVE_POD, st)
        time.sleep(0.05)
        rec.delete(f"c{i}")
    assert all(r["injected_at"] and r["actual"] is not None and r["deleted_at"] for r in rec.crs.values())


def test_never_injected_stage_stays_unproven():
    store = {}
    rec = make_recorder(store, injected_fn=lambda n: False)
    rec.create("c0", "r", "a", ACTIVE_POD, nc.NETWORK_STAGES[0])
    rec.delete("c0")
    assert rec.crs["c0"]["injected_at"] is None and rec.crs["c0"]["actual"] is None and rec.crs["c0"]["deleted_at"]


def test_recorder_read_failure_is_evidence_not_crash_and_never_reuses_creation_snapshot():
    store, calls = {}, {"n": 0}

    def read(name):
        calls["n"] += 1
        if calls["n"] == 1:
            return store.get(name)                            # 생성 직후만 성공
        raise TimeoutError("api timeout")
    rec = make_recorder(store, read_fn=read)
    rec.create("c0", "r", "a", ACTIVE_POD, nc.NETWORK_STAGES[0])
    assert rec.is_injected("c0")
    c = rec.crs["c0"]
    assert c["created"] is not None and c["actual"] is None and "TimeoutError" in c["read_error"]
    rec.delete("c0")
    assert c["actual"] is None                                # 지우기 직전 읽기도 실패 - 생성 직후 값으로 채우지 않는다


def test_bad_snapshot_is_not_erased_by_a_later_clean_one():
    store, reads = {}, {"n": 0}

    def read(name):
        reads["n"] += 1
        c = copy.deepcopy(store.get(name))
        if c and reads["n"] == 2:                             # 적용 직후 첫 읽기: 다른 Pod 기록이 섞여 있음
            c["status"]["experiment"]["containerRecords"][0]["id"] = f"vllm-serving/{NEW_POD}"
        return c
    rec = make_recorder(store, read_fn=read)
    rec.create("c0", "r", "a", ACTIVE_POD, nc.NETWORK_STAGES[0])
    assert rec.is_injected("c0")
    rec.delete("c0")                                          # 마지막 읽기는 깨끗하다
    c = rec.crs["c0"]
    assert reads["n"] >= 3 and ns2.pr.cr_status_problems(c["actual"], ACTIVE_POD) == []
    assert any("containerRecords_other_pods" in v for v in c["violations"])
    d = {"crs": [rec.record()["crs"][0]], "targets": [ACTIVE_POD]}
    assert any("containerRecords_other_pods" in p for p in ns2.delivery_problems(d, {}, [])["problems"])


def test_injected_check_exception_is_recorded_not_raised():
    store = {}

    def boom(n):
        raise TimeoutError("k8s api")
    rec = make_recorder(store, injected_fn=boom)
    rec.create("c0", "r", "a", ACTIVE_POD, nc.NETWORK_STAGES[0])
    rec.delete("c0")
    assert "TimeoutError" in rec.crs["c0"]["injected_check_error"] and rec.crs["c0"]["injected_at"] is None


def test_stalled_final_evidence_does_not_block_deletion():
    store, calls, stall = {}, {"n": 0}, threading.Event()

    def read(name):
        calls["n"] += 1
        if calls["n"] >= 3:                                # 1=생성 직후, 2=적용 뒤, 3=삭제 직전 마지막 읽기 - 여기서 멈춘다
            stall.wait(5)
        return store.get(name)
    rec = make_recorder(store, read_fn=read, final_sec=0.1)
    rec.create("c0", "r", "a", ACTIVE_POD, nc.NETWORK_STAGES[0])
    assert rec.is_injected("c0")
    t0 = time.monotonic()
    rec.delete("c0")
    assert time.monotonic() - t0 < 2.0 and "c0" not in store and rec.crs["c0"]["closed"]
    assert "final_evidence_timeout" in rec.crs["c0"]["lifecycle_problems"]
    stall.set()
    time.sleep(0.1)
    c = rec.crs["c0"]
    assert c["actual"] is not None and c["read_error"] is None        # 늦게 풀린 읽기는 닫힌 증거를 바꾸지 못한다(적용 직후 값 유지)


def test_late_watcher_result_cannot_change_closed_evidence():
    store, release, started = {}, threading.Event(), threading.Event()

    def slow_injected(n):
        started.set()
        release.wait(5)
        return True
    rec = make_recorder(store, injected_fn=slow_injected, join_sec=0.05, lock_sec=0.05)
    rec.create("c0", "r", "a", ACTIVE_POD, nc.NETWORK_STAGES[0])
    assert started.wait(2)
    rec.delete("c0")                                          # 감시가 잠금을 쥔 채 멈춰 있음 -> 잠금 시간 초과, 그래도 지운다
    c = rec.crs["c0"]
    assert "c0" not in store and c["closed"] and c["deleted_at"] and c["watcher_alive_at_delete"] is True
    assert "lifecycle_lock_timeout" in c["lifecycle_problems"]
    release.set()
    rec._watch["c0"][0].join(5)
    assert not rec._watch["c0"][0].is_alive()
    assert c["injected_at"] is None and c["actual"] is None  # 늦게 돌아온 응답은 닫힌 증거를 바꾸지 못한다
    d = {"crs": [rec.record()["crs"][0]], "targets": [ACTIVE_POD]}
    probs = ns2.delivery_problems(d, {}, [])["problems"]
    assert any("watcher_still_running_after_delete" in p for p in probs) and any("lifecycle_lock_timeout" in p for p in probs)


def test_concurrent_deletes_are_serialized_and_idempotent():
    store, calls = {}, []

    def slow_delete(n):
        calls.append(n)
        time.sleep(0.1)
        store.pop(n, None)
    rec = make_recorder(store, delete_fn=slow_delete)
    rec.create("c0", "r", "a", ACTIVE_POD, nc.NETWORK_STAGES[0])
    ts = [threading.Thread(target=rec.delete, args=("c0",)) for _ in range(2)]
    [x.start() for x in ts]
    [x.join(5) for x in ts]
    c = rec.crs["c0"]
    assert not any(x.is_alive() for x in ts) and c["closed"] and c["deleted_at"] and "c0" not in store
    assert calls == ["c0", "c0"]                              # 두 번 모두 안전하게 호출되고(삭제는 멱등) 증거는 한 번만 기록된다


def test_cleanup_during_create_waits_for_registration_and_stops_the_watcher():
    store, in_create, go = {}, threading.Event(), threading.Event()

    def slow_create(name, run_id, arm, target, stage, duration=None):
        in_create.set()
        go.wait(5)
        store[name] = cr_body(name, run_id, arm, target, stage, duration)
    rec = make_recorder(store, create_fn=slow_create)
    t = threading.Thread(target=rec.create, args=("c0", "r", "a", ACTIVE_POD, nc.NETWORK_STAGES[0]))
    t.start()
    assert in_create.wait(2)
    d = threading.Thread(target=rec.delete, args=("c0",))
    d.start()
    time.sleep(0.1)
    assert d.is_alive() and "c0" not in rec.crs               # 만드는 중에는 삭제가 기다린다(등록 전 상태를 건드리지 않는다)
    go.set()
    t.join(5)
    d.join(5)
    c = rec.crs["c0"]
    assert c["closed"] and c["deleted_at"] and "c0" not in store and not rec._watch["c0"][0].is_alive()


# ---------- (4) 실제 조회기는 get만 ----------
def test_real_live_reader_issues_only_kubectl_get(tmp_path):
    chaos = {"items": [{"metadata": {"name": "b"}}, {"metadata": {"name": "a"}}]}
    seen = []

    def fake_run(cmd):
        seen.append(cmd)
        if cmd[2:4] == ["networkchaos", "-n"]:                                        # 목록
            return 0, json.dumps(chaos), ""
        if cmd[2] == "networkchaos":                                                    # 개별 조회 - 없음
            return 1, "", 'Error from server (NotFound): networkchaos "x" not found'
        if "query_range" in cmd[-1]:
            return 0, json.dumps({"status": "success", "data": {"result": [{"metric": {}, "values": [[1, "0.01"], [16, "0.02"]]}]}}), ""
        return 0, json.dumps(prom_body(0.004)), ""
    snap = {**tgp.CANON_SNAP, "template": template(), "pods": [
        {"name": ACTIVE_POD, "uid": "ua", "hash": "aaa111", "ready": True, "deleting": False, "restarts": 0},
        {"name": NEW_POD, "uid": "ub", "hash": "bbb222", "ready": True, "deleting": True, "restarts": 0}]}
    live = ns2.RealNetLive(lambda: snap, tmp_path / "p.json", run=fake_run, policy_state_fn=lambda: dict(CLEAN_POLICY))
    st = live.read_state()
    assert st["chaos_names"] == ["a", "b"] and [p["terminating"] for p in st["pods"]] == [False, True]
    assert live.read_chaos_cr("x") is None and live.net_probe_body()["status"] == "success"
    assert live.protected_pods("bbb222") == [] and live.protected_pods("aaa111") == [ACTIVE_POD]     # 종료 중인 Pod는 보호 대상에서 제외
    assert live.protected_probe_seconds([NEW_POD], "2026-10-20T01:00:00+00:00", "2026-10-20T01:06:00+00:00") == {NEW_POD: [[1.0, 0.01], [16.0, 0.02]]}
    live.read_end_state(tmp_path)
    assert seen and all(c[:2] == ["kubectl", "get"] for c in seen), [c[:2] for c in seen]
    assert len(live.commands) == len(seen)


@pytest.mark.parametrize("out", ["{}", '{"items": null}', "[]", '{"items": [{"metadata": {}}]}', '{"items": [1]}'])
def test_malformed_chaos_list_is_unknown_not_empty(tmp_path, out):
    live = ns2.RealNetLive(lambda: {}, tmp_path / "p.json", run=lambda cmd: (0, out, ""))
    with pytest.raises(RuntimeError):
        live.list_chaos_names()                                       # 빈 목록으로 읽으면 잔여 지연 설정을 놓친다
    assert ns2.RealNetLive(lambda: {}, tmp_path / "p.json", run=lambda cmd: (0, '{"items": []}', "")).list_chaos_names() == []


def test_malformed_single_cr_and_multi_series_probe_are_errors(tmp_path):
    live = ns2.RealNetLive(lambda: {}, tmp_path / "p.json", run=lambda cmd: (0, "{}", ""))
    with pytest.raises(RuntimeError):
        live.read_chaos_cr("x")
    two = json.dumps({"status": "success", "data": {"result": [{"values": [[1, "0.1"]]}, {"values": [[1, "0.1"]]}]}})
    with pytest.raises(RuntimeError):
        ns2.RealNetLive(lambda: {}, tmp_path / "p.json", run=lambda cmd: (0, two, "")).protected_probe_seconds(
            [NEW_POD], "2026-10-20T01:00:00+00:00", "2026-10-20T01:06:00+00:00")


def test_chaos_gone_polling_returns_what_is_left(tmp_path):
    state = {"names": ["n1", "n2"]}
    clock = {"t": 0.0}

    def fake_run(cmd):
        return 0, json.dumps({"items": [{"metadata": {"name": n}} for n in state["names"]]}), ""

    def sleep(s):
        clock["t"] += s
        state["names"] = ["n2"]                                                                 # n1은 사라지고 n2는 남음
    live = ns2.RealNetLive(lambda: {}, tmp_path / "p.json", run=fake_run)
    assert live.wait_network_chaos_gone(["n1", "n2"], sleep=sleep, monotonic=lambda: clock["t"], timeout_sec=6) == ["n2"]
    assert live.wait_network_chaos_gone([], sleep=sleep, monotonic=lambda: clock["t"]) == []


# ---------- 조건 일관성 ----------
def test_contract_conditions_cover_original_v4_keys_and_differ_only_in_network_parts():
    inst = ns1.load_stage1()
    net_c, cpu_c = ns2.pressure_conditions(inst), orig.pressure_conditions()
    for k in ("stress_stages", "probe_config_sha256", "probe_image", "probe_image_id", "detector_rule", "start_state_rule"):
        assert k in net_c                                                                       # 원본 run_validity V4가 비교하는 키
    same = [k for k in cpu_c if k != "stress_stages"]
    assert all(net_c[k] == cpu_c[k] for k in same)                                              # 압박 정의 외의 조건은 Phase 2와 같다
    assert net_c["stress_stages"] == list(nc.NETWORK_STAGES) != cpu_c["stress_stages"]
    fz = ns2.pressure_frozen(inst, {"h": 1})
    assert fz["observation_sec"] == ns2.OBSERVATION_SEC == inst.gp.PRESSURE_OBSERVATION_SEC and set(fz["threshold"]) == set(nc.DETECTOR_IDS)


# ---------- (5) 원본 불변 + 설치 ----------
def test_install_replaces_only_instance_slots_and_leaves_original_untouched(tmp_path):
    before = (orig.run_pressure, orig.validate, orig.pressure_conditions, orig.pressure_frozen)
    inst = ns1.load_stage1(ns1.NetConfig(results_dir=tmp_path))
    inst_pressure, inst_validate = inst.run_pressure, inst.validate
    ns2.install(inst, object())
    assert (orig.run_pressure, orig.validate, orig.pressure_conditions, orig.pressure_frozen) == before
    assert inst.run_pressure is not inst_pressure and inst.validate is not inst_validate and inst.pressure_conditions() != orig.pressure_conditions()
    assert inst.pressure_conditions()["stress_stages"] == list(nc.NETWORK_STAGES)


def test_net_deps_does_not_mutate_callers_deps(tmp_path, monkeypatch, env):
    w = tgp.World(tmp_path / "w", monkeypatch, mode=gp.MODE_RO, send_at=(), wrap_preview=True)
    deps = w.deps()
    keep = (deps.make_injector, deps.wait_chaos_gone)
    out = ns2.net_deps(deps, lambda *a: None, FakeNet(tmp_path / "p.json"))
    assert (deps.make_injector, deps.wait_chaos_gone) == keep and out is not deps and out.make_injector is not deps.make_injector


# ---------- 유효성 ----------
def _validity_setup(tmp_path, monkeypatch, delivery_check, base_reasons, post=()):
    import guarded_ab_analysis as gab
    inst = ns1.load_stage1(ns1.NetConfig(results_dir=tmp_path))
    pc.write_json_new(tmp_path / "p2-session-n2p01.json", {"run": "n2p01", "kind": "pressure", "status": "run", "run_id": "rid",
                                                           "evidence_file": "e.jsonl", "postflight_invalid_reasons": list(post)})
    summary = {"network_delivery_check": delivery_check, "trial_outcome": {"t_injection_observed_utc": "2026-10-20T01:00:00+00:00"}}
    monkeypatch.setattr(inst, "pressure_run_dict", lambda d, run, pdir: ({"summary": summary, "run": run}, tmp_path / "x.csv"))
    monkeypatch.setattr(pc, "expected_if_hashes", lambda: {"h": 1})
    monkeypatch.setattr(gab, "primary", lambda dd, csv: {"computable": True})
    seen = {}
    monkeypatch.setattr(gab, "run_validity", lambda dd, frozen, prim: seen.update(frozen=frozen) or {"reasons": list(base_reasons), "notes": {}})
    return inst, seen


def test_validity_drops_cpu_only_v3_and_judges_network_delivery(tmp_path, monkeypatch):
    inst, seen = _validity_setup(tmp_path, monkeypatch, {"problems": [], "container_records_present": True},
                                 ["V3_stress_not_verified: None ['stress_evidence_missing']"])
    v = ns2.validate_pressure_net(inst, "n2p01", tmp_path, operator="none")
    assert v["valid"] is True and v["invalid_reasons"] == [] and v["status"] == "valid"            # CPU 스트레스 증거가 없어도 네트워크 증거로 유효
    assert seen["frozen"]["stress_stages"] == list(nc.NETWORK_STAGES)                                    # 원본 V4 비교값이 네트워크 계약값
    assert v["network_delivery"] == {"container_records_present": True, "n_problems": 0}


def test_validity_keeps_other_original_reasons_and_adds_network_ones(tmp_path, monkeypatch):
    inst, _ = _validity_setup(tmp_path, monkeypatch, {"problems": ["NEWPOD: probe_duration_max=0.5s"], "container_records_present": False},
                              ["V3_x", "V1_not_completed: aborted", "V5_old_policy_not_isolated"], post=["k8s_snapshots_missing"])
    v = ns2.validate_pressure_net(inst, "n2p01", tmp_path, operator="manual restart")
    r = v["invalid_reasons"]
    assert v["valid"] is False and "V3_x" not in r and "V1_not_completed: aborted" in r and "V5_old_policy_not_isolated" in r
    assert "N3_NEWPOD: probe_duration_max=0.5s" in r and "k8s_snapshots_missing" in r and "operator_intervention: manual restart" in r


def test_validity_without_delivery_check_is_invalid(tmp_path, monkeypatch):
    inst, _ = _validity_setup(tmp_path, monkeypatch, None, [])
    assert "N3_network_delivery_check_missing" in ns2.validate_pressure_net(inst, "n2p01", tmp_path, operator="none")["invalid_reasons"]


def test_installed_validate_routes_pressure_to_network_and_normal_to_original(tmp_path, monkeypatch):
    inst = ns1.load_stage1(ns1.NetConfig(results_dir=tmp_path))
    calls = []
    inst.validate = lambda run, d, *, operator, probe_dir=None: calls.append(("original", run)) or {"valid": "orig"}
    monkeypatch.setattr(ns2, "validate_pressure_net", lambda i, run, d, *, operator, probe_dir=None: calls.append(("network", run)) or {"valid": "net"})
    ns2.install(inst, object())
    assert inst.validate("n2p01", tmp_path, operator="none") == {"valid": "net"}
    assert inst.validate("n2n01", tmp_path, operator="none") == {"valid": "orig"}
    assert calls == [("network", "n2p01"), ("original", "n2n01")]                       # 압박만 네트워크 판정, 정상 세션은 원본 경로


def test_validity_record_labels_leakage_and_excludes_the_session_from_detector_results(tmp_path, monkeypatch):
    leak = {"problems": ["stage-1-500ms: vllm-serving-bbb222-y: probe_duration_mean_max=0.5000s > 0.1s (지연이 샌 것으로 봄)"],
            "container_records_present": True}
    inst, _ = _validity_setup(tmp_path, monkeypatch, leak, [])
    v = ns2.validate_pressure_net(inst, "n2p01", tmp_path, operator="none")
    assert v["valid"] is False and v["invalid_reasons"][0].startswith(ns2.ISOLATION_INVALID) and any(r.startswith("N3_") for r in v["invalid_reasons"])
    assert (v["isolation_not_established"], v["counts_as_detector_failure"], v["detector_gate_inclusion"]) == (True, False, "excluded")
    ok = {"problems": [], "container_records_present": True}
    (tmp_path / "ok").mkdir()
    inst2, _ = _validity_setup(tmp_path / "ok", monkeypatch, ok, [])
    v2 = ns2.validate_pressure_net(inst2, "n2p01", tmp_path / "ok", operator="none")
    assert v2["valid"] is True and (v2["isolation_not_established"], v2["detector_gate_inclusion"]) == (False, "included")
