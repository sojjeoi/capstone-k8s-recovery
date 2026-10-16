"""n2_preflight 시험(2026-10-17, DEVELOPMENT). 클러스터 접근 없음 - 세션 시작 전 점검이 위험한 상태를 막는지 합성 값으로 검증한다."""
import copy
import math
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import n2_common as nc  # noqa: E402
import n2_preflight as pf  # noqa: E402

pc = nc.pc
FAKE = "f" * 64
ACTIVE, PREVIEW = "aaa111", "bbb222"
PODS = [{"name": "vllm-serving-aaa111-x", "uid": "ua", "hash": ACTIVE, "ready": True, "terminating": False},
        {"name": "vllm-serving-bbb222-y", "uid": "up", "hash": PREVIEW, "ready": True, "terminating": False}]
CLEAN_POLICY = {"mode": "record_only", "context": None, "blocked": False, "unresolved": []}


def body(v, n=1):
    return {"status": "success", "data": {"result": [{"metric": {"pod": f"p{i}"}, "value": [1.0, str(v)]} for i in range(n)]}}


def template(ready=11, live=11):
    return {"spec": {"containers": [{"name": "vllm", "image": "vllm-cpu-env:latest", "imagePullPolicy": "Never",
                                     "args": ["--model=Qwen/Qwen2.5-0.5B-Instruct", "--port=8000"],
                                     "resources": {"limits": {"cpu": "3", "memory": "6Gi"}},
                                     "readinessProbe": {"timeoutSeconds": ready}, "livenessProbe": {"timeoutSeconds": live}}]}}


@pytest.fixture
def ctx(monkeypatch):
    """관측 환경과 같은 environment를 가진 profile. contract_hashes는 저장소 소스 해시라 클러스터 없이 계산된다."""
    monkeypatch.setattr(nc, "PREREG_SHA256", FAKE)
    tpl = template()
    obs = nc.observe_environment(tpl)
    pol_sha = nc.contract_hashes()["recovery_policy_sha256"]
    prof = nc.build_selection_profile({k: v for k, v in obs.items() if k not in ("probe_profile", "pressure")}, pol_sha, FAKE)
    return {"profile": prof, "observed_env": obs, "policy_sha256": pol_sha, "template": tpl}


def run(ctx, **over):
    kw = dict(profile=ctx["profile"], observed_env=ctx["observed_env"], policy_sha256=ctx["policy_sha256"],
              template=ctx["template"], chaos_names=[], pods=PODS, active_hash=ACTIVE, preview_hash=PREVIEW,
              net_probe_body=body(0.004), policy=CLEAN_POLICY)
    kw.update(over)
    return pf.preflight_problems(**kw)


# ---------- 환경 관측·계약 해시 ----------
def test_contract_hashes_are_offline_and_policy_path_unchanged_from_phase2():
    n, p = nc.contract_hashes(), pc.contract_hashes()
    assert all(re.fullmatch(r"[0-9a-f]{64}", v) for v in n.values())
    assert n["slo_contract"] == p["slo_contract"]                       # SLO 판정 코드 같음
    assert n["recovery_policy_sha256"] == p["recovery_policy_sha256"]   # 복구 정책 코드 같음 = 복구 경로를 바꾸지 않았다
    assert n["workload_contract"] != p["workload_contract"] and n["telemetry_contract"] != p["telemetry_contract"]   # 환경별로 다름


def test_observed_probe_profile_tolerant_or_actual_values():
    assert nc.observed_probe_profile(template(11, 11)) == nc.PROBE_PROFILE
    assert nc.observed_probe_profile(template(1, 1)) == {"name": "not_network_tolerant", "readiness_timeout_sec": 1, "liveness_timeout_sec": 1}


def test_observe_environment_has_same_keys_as_profile_environment(ctx):
    assert set(ctx["observed_env"]) == set(ctx["profile"]["environment"])
    assert pc.check_stale(ctx["profile"], ctx["observed_env"], ctx["policy_sha256"]) == []


# ---------- 통과 ----------
def test_clean_state_passes_and_reports_target_and_protected(ctx):
    r = run(ctx)
    assert r["ok"] and r["problems"] == []
    assert r["target"] == "vllm-serving-aaa111-x" and r["protected"] == ["vllm-serving-bbb222-y"]
    assert r["net_probe"] == {"value": 0.004, "n_series": 1}


# ---------- 각 점검이 실제로 막는다 ----------
def test_stale_environment_blocks(ctx):
    changed = copy.deepcopy(ctx["observed_env"])
    changed["serving_args"] = ["--model=other"]
    assert any("PROFILE_STALE" in p for p in run(ctx, observed_env=changed)["problems"])
    assert any("PROFILE_STALE" in p for p in run(ctx, policy_sha256="0" * 64)["problems"])


def test_default_probe_profile_blocks_because_pod_would_restart(ctx):
    r = run(ctx, template=template(1, 1))
    assert not r["ok"] and any("network_tolerant" in p for p in r["problems"])


def test_leftover_network_chaos_blocks(ctx):
    assert any("network_chaos_present" in p for p in run(ctx, chaos_names=["netdelay-old"])["problems"])


@pytest.mark.parametrize("pods,needle", [
    ([PODS[0]], "preview_pod_count=0"),                                                          # 보호할 새 Pod 없음
    ([PODS[0], {**PODS[1], "ready": False}], "preview_pod_not_ready"),
    ([{**PODS[0], "ready": False}, PODS[1]], "active_pod_not_ready"),
    ([PODS[0], copy.deepcopy(PODS[0]), PODS[1]], "active_pod_count=2"),
    ([{**PODS[0], "terminating": True}, PODS[1]], "active_pod_count=0"),
    ([], "active_pod_count=0"),
])
def test_pod_layout_must_be_one_active_and_one_ready_preview(ctx, pods, needle):
    assert any(needle in p for p in run(ctx, pods=pods)["problems"])


def test_active_equals_preview_means_nothing_to_protect(ctx):
    assert any("active_equals_preview" in p for p in run(ctx, preview_hash=ACTIVE)["problems"])


@pytest.mark.parametrize("b,needle", [
    (body(0.004, n=0), "series_count=0"),
    (body(0.004, n=2), "series_count=2"),
    (body("NaN"), "non_finite"),
    ({"unexpected": 1}, "unparsable"),
    (body(1.5), "already_anomalous"),                                                            # 정상 전제가 깨짐(이미 지연 중)
    (body(nc.NET_THRESHOLD + 1e-6), "already_anomalous"),
])
def test_net_probe_must_be_readable_single_and_normal(ctx, b, needle):
    r = run(ctx, net_probe_body=b)
    assert not r["ok"] and any(needle in p for p in r["problems"]), r["problems"]


def test_net_probe_at_threshold_is_normal_strict_greater(ctx):
    assert run(ctx, net_probe_body=body(nc.NET_THRESHOLD))["ok"]


@pytest.mark.parametrize("policy,needle", [
    ({**CLEAN_POLICY, "mode": "act"}, "policy_mode"),
    ({**CLEAN_POLICY, "context": {"run_id": "r"}}, "policy_context_set"),
    ({**CLEAN_POLICY, "blocked": True}, "policy_blocked"),
    ({**CLEAN_POLICY, "unresolved": ["t"]}, "policy_unresolved"),
    (None, "policy_mode"),
])
def test_policy_must_be_record_only_and_clean(ctx, policy, needle):
    assert any(needle in p for p in run(ctx, policy=policy)["problems"])


def test_multiple_problems_are_all_reported(ctx):
    r = run(ctx, template=template(1, 1), chaos_names=["x"], policy={**CLEAN_POLICY, "mode": "act"}, net_probe_body=body(0.004, n=0))
    assert len(r["problems"]) >= 4 and not r["ok"]


def test_protected_pods_excludes_active_and_terminating():
    pods = PODS + [{"name": "old", "uid": "uo", "hash": "ccc333", "ready": True, "terminating": True}]
    assert pf.protected_pods(pods, ACTIVE) == ["vllm-serving-bbb222-y"]
    assert not math.isnan(nc.NET_THRESHOLD)
