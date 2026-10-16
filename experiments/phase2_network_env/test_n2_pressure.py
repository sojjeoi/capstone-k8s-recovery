"""n2_pressure 시험(2026-10-17, DEVELOPMENT). 클러스터 접근 없음. 핵심: 파일럿 어댑터가 실제로 만드는 CR 본문과 기대 본문이 같다."""
import copy
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import n2_common as nc  # noqa: E402
import n2_pressure as pr  # noqa: E402

NS, TARGET, NEW, PREVIEW = "vllm-serving", "vllm-serving-aaa111-x", "vllm-serving-bbb222-y", "vllm-serving-ccc333-z"
STAGE = nc.NETWORK_STAGES[0]


def cr(target=TARGET, **spec_over):
    c = pr.expected_cr_body("netdelay-x-s0", "run1", "pressure", target, STAGE)
    c["spec"].update(spec_over)
    return c


# ---------- 파일럿 어댑터와 같은 본문 ----------
def test_expected_body_equals_pilot_adapter_body(monkeypatch):
    try:
        import network_degrade_adapter as nda
    except Exception as e:  # kubernetes 등 미설치
        pytest.skip(f"adapter import 불가: {e}")
    seen = {}

    class FakeApi:
        def create_namespaced_custom_object(self, group, version, ns, plural, body):
            seen.update(group=group, version=version, ns=ns, plural=plural, body=body)

    monkeypatch.setattr(nda, "load_kube_config", lambda: None)
    monkeypatch.setattr(nda.client, "CustomObjectsApi", lambda: FakeApi())
    for stage in nc.NETWORK_STAGES:
        for duration in (None, "150s"):
            nda.create_network_chaos("netdelay-x-s0", "run1", "pressure", TARGET, stage, duration=duration)
            assert seen["body"] == pr.expected_cr_body("netdelay-x-s0", "run1", "pressure", TARGET, stage, duration=duration)
    assert (seen["group"], seen["version"], seen["ns"], seen["plural"]) == (pr.CHAOS_GROUP, pr.CHAOS_VERSION, NS, "networkchaos")


# ---------- 1) 설정 검사 ----------
def test_spec_ok_for_single_named_target():
    assert pr.cr_spec_problems(cr(), TARGET, stage=STAGE) == []


@pytest.mark.parametrize("mutate,needle", [
    (lambda c: c["spec"]["selector"].__setitem__("pods", {NS: [TARGET, NEW]}), "selector.pods"),            # 새 Pod까지 지정
    (lambda c: c["spec"]["selector"].__setitem__("pods", {NS: [NEW]}), "selector.pods"),                   # 엉뚱한 Pod
    (lambda c: c["spec"]["selector"].__setitem__("labelSelectors", {"app": "vllm-serving"}), "other_selectors"),   # 초기 시나리오 파일 방식
    (lambda c: c["spec"]["selector"].__setitem__("namespaces", [NS, "kube-system"]), "namespaces"),
    (lambda c: c["spec"].__setitem__("mode", "all"), "mode"),
    (lambda c: c["spec"].__setitem__("action", "loss"), "action"),
    (lambda c: c["spec"]["delay"].__setitem__("latency", "100ms"), "delay"),
])
def test_spec_rejects_broader_or_different_selection(mutate, needle):
    c = cr()
    mutate(c)
    probs = pr.cr_spec_problems(c, TARGET, stage=STAGE)
    assert probs and any(needle in p for p in probs), probs


def test_initial_scenario_yaml_is_rejected():
    """chaos/scenario-network-degrade.yaml(라벨 선택)은 쓰면 안 된다 - 같은 모양의 CR이 거부되는지."""
    c = cr()
    c["spec"]["selector"] = {"namespaces": [NS], "labelSelectors": {"app": "vllm-serving"}}
    probs = pr.cr_spec_problems(c, TARGET, stage=STAGE)
    assert any("selector.pods" in p for p in probs) and any("other_selectors" in p for p in probs)


# ---------- 2) 상태 검사 ----------
def test_status_records_only_target_ok_or_absent():
    c = cr()
    assert pr.cr_status_problems(c, TARGET) == [] and not pr.has_container_records(c)           # 필드 없음 → 3)이 판정
    c["status"] = {"experiment": {"containerRecords": [{"id": f"{NS}/{TARGET}", "phase": "Injected"}]}}
    assert pr.cr_status_problems(c, TARGET) == [] and pr.has_container_records(c)
    c["status"]["experiment"]["containerRecords"].append({"id": f"{NS}/{NEW}", "phase": "Injected"})
    assert pr.cr_status_problems(c, TARGET) == [f"containerRecords_other_pods:['{NS}/{NEW}']"]


# ---------- 클러스터 전체 CR 목록 ----------
def test_all_crs_target_only_and_protected_pods():
    ok = [cr(), cr()]
    assert pr.all_crs_target_only(ok, TARGET, [NEW, PREVIEW]) == []
    leaked = copy.deepcopy(ok)
    leaked[1]["spec"]["selector"]["pods"] = {NS: [NEW]}
    leaked[1]["metadata"]["name"] = "netdelay-leak"
    probs = pr.all_crs_target_only(leaked, TARGET, [NEW, PREVIEW])
    assert any("netdelay-leak" in p and "protected_pod_selected" in p for p in probs)
    assert pr.all_crs_target_only([], TARGET, [NEW]) == []


# ---------- 3) 결과 검사 ----------
def test_protected_pods_unaffected_by_probe_duration():
    normal = {NEW: [0.004, 0.005, 0.004, 0.006, 0.005], PREVIEW: [0.004] * 5}
    assert pr.protected_pods_unaffected_problems(normal, [NEW, PREVIEW]) == []
    leaked = {NEW: [0.004, 0.006, 1.2, 1.1, 1.0], PREVIEW: [0.004] * 5}                          # 지연이 새 Pod로 샌 경우
    probs = pr.protected_pods_unaffected_problems(leaked, [NEW, PREVIEW])
    assert len(probs) == 1 and NEW in probs[0] and "샌" in probs[0]
    assert pr.protected_pods_unaffected_problems(normal, [NEW, "missing-pod"])                    # 값 없는 보호 Pod → 확인 불가 = 위반
    assert pr.protected_pods_unaffected_problems({NEW: [0.004, float("nan"), 0.005, 0.004, 0.004]}, [NEW])
    assert pr.protected_pods_unaffected_problems({NEW: [0.004, 0.005]}, [NEW])                    # 표본 부족


def test_protected_limit_is_far_below_smallest_delay_and_above_normal_jitter():
    assert 0.019 < pr.PROTECTED_MAX_PROBE_SEC < 0.5       # 정상 최대(0.019초) 위, 최소 지연 단계(0.5초) 아래


# ---------- 단계 기록 ----------
def test_stage_windows_must_match_registered_definition():
    good = [{"name": s["name"], "injected": True} for s in nc.NETWORK_STAGES]
    assert pr.stage_windows_problems(good) == []
    assert pr.stage_windows_problems(good[:3])                                                    # 단계 누락(예: 승격 등으로 중단)
    assert pr.stage_windows_problems(good[::-1])                                                  # 순서 다름
    bad = copy.deepcopy(good)
    bad[2]["injected"] = False
    assert pr.stage_windows_problems(bad) == ["stage_not_injected:stage-3-2000ms"]
