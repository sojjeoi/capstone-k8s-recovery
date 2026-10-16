#!/usr/bin/env python3
"""네트워크 지연 압박의 '대상 Pod만 영향' 검사(2026-10-17, DEVELOPMENT - 동결 전, 클러스터 접근 없음).

이유: Chaos Mesh NetworkChaos가 지연 대상(현재 active Pod)만 잡고 preview·새 Pod는 건드리지 않아야, 승격 뒤 새 Pod가
지연을 벗어나 '복구'가 성립한다. 한 겹이 아니라 세 겹으로 확인한다(파일럿 기록에는 CR 내부 상태 필드가 남아 있지 않아 한 필드에만 의존하지 않는다):
  1) CR 설정 검사: Pod 이름 1개만 지정(라벨·네임스페이스 전체 선택 금지), mode=one, action=delay, 지연값이 단계 정의와 같음.
  2) CR 상태 검사: status.experiment.containerRecords가 있으면 대상 Pod만(없으면 사실만 기록 - 3)이 최종 판정).
  3) 결과 검사: 지연 중 preview·새 Pod의 readiness probe 소요 시간이 PROTECTED_MAX_PROBE_SEC 이하여야 한다(지연이 새 Pod로 샜는지 직접 확인).
세션 유효성(탐지기 결과와 무관)에 쓰는 순수 함수다. 위반 → 그 세션은 무효이고 INTEGRITY가 아니라 invalid로 센다."""
import sys
sys.dont_write_bytecode = True
import math  # noqa: E402

import n2_common as nc  # noqa: E402

CHAOS_GROUP, CHAOS_VERSION, CHAOS_KIND = "chaos-mesh.org", "v1alpha1", "NetworkChaos"
PROTECTED_MAX_PROBE_SEC = 0.1    # 정상 probe 소요 최대 약 0.019초, 지연 최소 단계(0.5초)의 20% 미만 - 지연이 샌 것과 정상 지터를 가르는 사전 고정 값


def expected_cr_body(cr_name: str, run_id: str, arm: str, target_pod: str, stage: dict, *, namespace: str = "vllm-serving",
                     duration=None) -> dict:
    """파일럿에서 검증된 어댑터(network_degrade_adapter.create_network_chaos)가 만드는 본문과 같아야 한다 - 테스트가 대조한다."""
    body = {"apiVersion": f"{CHAOS_GROUP}/{CHAOS_VERSION}", "kind": CHAOS_KIND,
            "metadata": {"name": cr_name, "namespace": namespace, "labels": {"experiment-run-id": run_id, "phase8-arm": arm}},
            "spec": {"action": "delay", "mode": "one",
                     "selector": {"namespaces": [namespace], "pods": {namespace: [target_pod]}},
                     "delay": {"latency": stage["latency"], "jitter": stage["jitter"]}}}
    if duration is not None:
        body["spec"]["duration"] = duration
    return body


def cr_spec_problems(cr: dict, target_pod: str, *, namespace: str = "vllm-serving", stage: dict = None) -> list:
    """1) 설정 검사. 빈 목록 = 대상 Pod 1개만 지정."""
    probs = []
    spec = (cr or {}).get("spec") or {}
    sel = spec.get("selector") or {}
    if (cr or {}).get("kind") not in (None, CHAOS_KIND):
        probs.append(f"kind={cr.get('kind')}")
    if spec.get("action") != "delay":
        probs.append(f"action={spec.get('action')}")
    if spec.get("mode") != "one":
        probs.append(f"mode={spec.get('mode')}")
    if sel.get("pods") != {namespace: [target_pod]}:
        probs.append(f"selector.pods={sel.get('pods')}")
    extra = sorted(k for k in sel if k not in ("namespaces", "pods"))
    if extra:
        probs.append(f"selector_has_other_selectors:{extra}")          # labelSelectors·nodes·fieldSelectors 등 넓은 선택 금지
    if sel.get("namespaces") not in (None, [namespace]):
        probs.append(f"selector.namespaces={sel.get('namespaces')}")
    if stage is not None:
        d = spec.get("delay") or {}
        if (d.get("latency"), d.get("jitter")) != (stage["latency"], stage["jitter"]):
            probs.append(f"delay={d.get('latency')}/{d.get('jitter')} != {stage['latency']}/{stage['jitter']}")
    return probs


def cr_status_problems(cr: dict, target_pod: str, *, namespace: str = "vllm-serving") -> list:
    """2) 상태 검사: containerRecords가 있으면 id가 모두 '<ns>/<대상 Pod>'여야 한다. 없으면 빈 목록(사실 기록은 호출부 - 3)이 최종 판정)."""
    recs = (((cr or {}).get("status") or {}).get("experiment") or {}).get("containerRecords")
    if not recs:
        return []
    want = f"{namespace}/{target_pod}"
    bad = sorted({str(r.get("id")) for r in recs if r.get("id") != want})
    return [f"containerRecords_other_pods:{bad}"] if bad else []


def has_container_records(cr: dict) -> bool:
    return bool((((cr or {}).get("status") or {}).get("experiment") or {}).get("containerRecords"))


def all_crs_target_only(crs: list, target_pod: str, protected_pods, *, namespace: str = "vllm-serving") -> list:
    """클러스터의 모든 NetworkChaos CR(읽기 전용 목록)이 대상 Pod만 지정하고 보호 Pod(preview·새 Pod)를 건드리지 않는지."""
    probs = []
    protected = set(protected_pods or [])
    for cr in crs or []:
        name = ((cr or {}).get("metadata") or {}).get("name")
        for p in cr_spec_problems(cr, target_pod, namespace=namespace) + cr_status_problems(cr, target_pod, namespace=namespace):
            probs.append(f"{name}: {p}")
        named = ((((cr or {}).get("spec") or {}).get("selector") or {}).get("pods") or {}).get(namespace) or []
        hit = sorted(protected & set(named))
        if hit:
            probs.append(f"{name}: protected_pod_selected:{hit}")
    return probs


def protected_pods_unaffected_problems(probe_seconds_by_pod: dict, protected_pods, *, limit: float = PROTECTED_MAX_PROBE_SEC,
                                       min_points: int = 4) -> list:
    """3) 결과 검사: 지연 구간에서 보호 Pod별 readiness probe 소요 시간 값들(초). 한 값이라도 limit 초과면 지연이 샌 것.
    값이 없거나(min_points 미만) 유한하지 않으면 확인 불가 → 위반으로 본다(fail-closed)."""
    probs = []
    for pod in sorted(set(protected_pods or [])):
        vals = (probe_seconds_by_pod or {}).get(pod)
        finite = [v for v in (vals or []) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)]
        if len(finite) < min_points or len(finite) != len(vals):
            probs.append(f"{pod}: unverifiable({len(finite)}/{len(vals or [])} finite, need {min_points})")
        elif max(finite) > limit:
            probs.append(f"{pod}: probe_duration_max={max(finite):.4f}s > {limit}s (지연이 샌 것으로 봄)")
    return probs


def stage_windows_problems(windows: list) -> list:
    """주입한 단계가 사전등록 정의와 같은 순서·개수로 모두 실제 적용됐는지(어댑터 stage_windows 기록 기준)."""
    want = [s["name"] for s in nc.NETWORK_STAGES]
    got = [w.get("name") for w in windows or []]
    probs = []
    if got != want:
        probs.append(f"stages {got} != {want}")
    probs += [f"stage_not_injected:{w.get('name')}" for w in windows or [] if not w.get("injected")]
    return probs
