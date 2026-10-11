#!/usr/bin/env python3
"""§196(2026-09-30) - CPU 추가 실험(run_cpu_demo_*.py 5개) 전용, preview를
"생성하기 전" 워커 노드 자원 여유를 확인하는 최소 안전 게이트. 공식 실험
경로(run_if_threshold_candidate_smoke.py, run_if_threshold_no_injection_
long_trial.py)·arm_controller.py(공유/동결)·공통 모델/정책/임계값은 전혀
건드리지 않는다 - CPU 데모 실행군에서만 선택적으로 조립해 쓴다.

## 배경 (§195/rep5, 2026-09-29)
preview pod 생성 도중 워커 노드(sj-worker)가 NodeNotReady로 전환돼 active
서비스까지 재시작됐다(exit 137, liveness probe 타임아웃이 방아쇠 - 앱 자체
크래시 아님, 종료 로그로 확인). 원자료 대조(§196 조사) 결과 preview 생성
"직전" Node MemAvailable이 과거 성공 3건(5.22~5.49GB)보다 낮았고(4.17GB),
준비 중 최저치(0.82GB)도 과거 성공 3건 최저치(1.52~2.05GB)보다 뚜렷이
낮았다. 반면 active+preview pod 자체의 메모리 사용량(peak)은 오늘과 과거가
거의 동일(~7.1~7.2GB 합산) - preview가 "더 많이 먹어서"가 아니라 시작
시점의 baseline 자체가 낮았던 것이 핵심 차이로 보인다(근거 있는 추정, 왜
baseline이 낮았는지는 원인 미상).

## 기존 게이트와의 관계 (중복이 아니라 다른 구간을 본다)
memory_pressure_adapter.py는 이미 §96.3에서 "preview가 이미 Ready된 뒤,
stress 주입 직전" 구간의 headroom을 확인한다
(`available - expected_stress_delta >= MIN_NODE_AVAILABLE_WITH_PROJECTED_
STRESS_BYTES`, 4GiB - docs/design/phase8-blue-green-preflight-incident.md
§96.3 참고). §97.3에서 이 게이트가 실제 라이브에서 발동해
`fixed_threshold` arm을 정상 차단한 이력이 있다(proposed는 §5 지시대로
미실행). 이 게이트는 그보다 "더 이른" 구간 - preview 자체를 만들기 전 -
을 본다. §96.3은 자기 docstring부터 "preview가 이미 떠 있는" 상태를
전제로 시작해 이 구간을 전혀 보지 않는다. rep5는 바로 이 구간(preview
생성 자체가 아직 진행 중)에서 실패했다 - §96.3이 있었어도 막지 못했을
사고다.

cpu_pressure_demo_adapter.py 자신의 docstring은 "CPU stress 주입은 cgroup
CPU quota가 하드 캡이라 노드 차원 폭주 경로가 없다"고 의도적으로 이
게이트류를 생략했는데, 그 추론은 "주입"(CPU stress)에 대해서만 맞다 -
preview pod 자체는 어떤 stress를 시험하든 상관없이 vLLM 가중치를 "메모리에"
올려야 존재할 수 있고, 그 자체가 이미 실측된 메모리 비용이다(이번 사고).
즉 CPU 데모 어댑터의 기존 단순화는 "주입 단계"만 정확히 분석했고
"preview 생성 단계"의 메모리 비용은 별도로 검토된 적이 없다.

## 임계값의 한계 (반드시 읽을 것 - 억지로 확정한 숫자가 아님을 명시)
아래 NODE_MEMAVAILABLE_MIN_BEFORE_PREVIEW_BYTES는 "검증된 안전 기준"이
아니라 두 근거를 합친 잠정값이다:
  1. memory_pressure_adapter.MIN_NODE_AVAILABLE_WITH_PROJECTED_STRESS_BYTES
     (4GiB)와 같은 값 - 그 값 자체도 §50.4에서 "3GiB 즉시중단보다 엄격한
     PASS 바"로 처음 정해졌을 뿐, 그 이상의 수식적 도출 근거는 문서에
     없다. 이후 §50~§57·§96~§97·§127~§129에서 여러 차례 라이브로
     재확인됐지만, 전부 memory_pressure 시나리오의 "preview 준비 후"
     구간이다 - 이 파일이 보는 "preview 준비 전" 구간에 그대로 적용
     가능하다는 독립적 근거는 아니다.
  2. 이번 §196 조사에서 대조한 4건 - preview 시작 "직전" MemAvailable이
     성공 3건 5.22~5.49GB(=4.86~5.11GiB), 실패 1건 4.17GB(=3.89GiB). 4GiB가
     이 4건을 정확히 가르지만, 이는 표본 4개(실패 1건)로 이 값이 그
     표본들을 구분하도록 "사후에" 맞춰진 것과 다르지 않다.
     4.17~5.22GB(=3.89~4.86GiB) 구간은 전혀 관측된 적이 없다 - 이 구간이
     실제로 안전한지 위험한지 모른다(판정불가로 남김).
  3. 이 노드가 "preview 활동과 무관하게" 평소 얼마나 자주 4GiB 밑으로
     내려가는지 데이터가 없다 - 그래서 이 게이트가 막을 "거짓 차단"
     (정말 안전했을 시도까지 막는) 비율을 알 수 없다. 임계값을 넉넉히
     잡을수록(높일수록) 이 비용은 커진다 - 이 비용을 데이터로 줄이는
     유일한 방법은 이 게이트를 실제로 몇 차례 더 돌려 기록을 쌓는 것뿐이다.

## 감시 범위 (한 번만 확인 - 준비 중 급락 감시는 없음, 의도적 최소 구현)
이 게이트는 preview 생성 "직전" 1회만 확인한다. 준비가 시작된 "뒤" 자원이
급락하는 경우(예: 다른 워크로드가 동시에 시작)는 이 게이트가 못 잡는다 -
그런 사고를 안전하게 잡으려면 준비 중 주기적 재확인 + 중단 시 preview를
안전하게 되돌리는 경로가 추가로 필요한데, 그 되돌리기 자체가 이미 rep5에서
"자동 rollback 실패"로 실패한 적이 있는 바로 그 경로라 여기서 같이
설계하면 검증 안 된 채 더 위험해질 수 있다. 그래서 이번 최소 구현에서는
사전 1회 확인만 넣고, 준비 중 감시는 검증된 안전한 중단 경로가 먼저 있어야
하는 별도 과제로 명시적으로 남긴다(미구현).

## §197(2026-09-30) 정정 - 위 4GiB 재사용은 측정 단계가 달라 무효였다
사용자 재검토로 근본 결함을 발견: `MIN_NODE_AVAILABLE_WITH_PROJECTED_
STRESS_BYTES`(4GiB)는 §96.3에서 "preview가 이미 Ready된 뒤, 예상 stress
증분을 뺀 잔여치"에 적용하는 값이다. 이 파일의 최초 구현(§196)은 그 값을
"preview 뜨기 전" 원시 baseline과 **직접** 비교했다 - 측정 단계가 다른 두
값을 같은 상수로 비교한 범주 오류였다.

**재계산으로 확인한 사실**: baseline이 4GiB(≈4.295GB)를 살짝 넘어(예:
4.30GB≈4.005GiB) 구 게이트를 통과하더라도, 이번에 실측된 드롭 범위
(3.124~3.510GiB, 아래 4건)를 그대로 적용하면 투영 잔여치가 0.495~0.881GiB
로 떨어진다 - rep5의 실제 장애 수준(0.764GiB)과 같거나 더 나쁘다. 즉 구
게이트는 오늘과 똑같이 위험한 시작 조건을 통과시켰을 것이다.

baseline(GiB)/최저치(GiB)/드롭(GiB), 전부 §196/§197 조사 실측:
  run01: 5.112 / 1.912 / 3.200 (성공)
  run05: 4.928 / 1.417 / 3.510 (성공)
  run06: 4.863 / 1.623 / 3.240 (성공)
  rep5:  3.887 / 0.764 / 3.124 (NodeNotReady 실패)

**올바른 형태(투영: baseline - 최대관측드롭 >= FLOOR)로 다시 시도해도
문제가 남는다**:
  - FLOOR=4GiB(§96.3 그대로): 필요 baseline ≥ 7.510GiB - 성공 3건 포함
    4건 전부 차단(run01 5.112 < 7.510 등) - 기존 성공 사례조차 통과
    못 시킬 만큼 과도하게 보수적이다.
  - FLOOR=3GiB(§50.5 즉시중단 그대로): 필요 baseline ≥ 6.510GiB - 역시
    4건 전부 차단.
  - 성공 3건 중 가장 낮은 최저치(run05, 1.417GiB) + 최대드롭(run05,
    3.510GiB)으로 가장 관대하게 다시 만들어도(필요 baseline ≥
    4.928GiB) run06(4.863GiB, 실제 성공)이 차단된다 - 성공 3건 자신의
    (baseline, 드롭, 최저치) 조합조차 서로 완전히 일관되지 않는다(드롭이
    run01 3.200~run05 3.510GiB로 약 0.31GiB 퍼져 있고, 이 퍼짐이 baseline
    성공/실패를 가르는 격차 자체와 비슷한 크기다).

**결론**: 이 4개 표본으로는 이 시나리오(preview 뜨기 전 headroom) 전용의
방어 가능한 "통과" 문턱을 만들 수 없다 - 기존 4GiB/3GiB 모두 원래 맥락
(preview 뜬 뒤 잔여치)에서만 검증됐을 뿐, 이 맥락(뜨기 전 원시값에 대한
투영)으로 그대로 옮기면 너무 보수적이거나(전부 차단) 성공 사례 자체를
설명 못 하거나(자기모순) 둘 중 하나다. 그래서 이 게이트는 §197부터
"통과/차단을 가르는" 임계값 비교를 그만둔다 - 측정·투영값은 계속
계산·반환하지만(감사·향후 임계값 도출용 기록), non-native arm에 대해
**항상 차단**한다(`ALWAYS_BLOCK_NON_NATIVE_ARM=True`). 방어 가능한 통과
조건이 생기기 전까지 fail-closed다.

## §199(2026-09-30) 정정 - "preview는 항상 active와 같은 노드" 가정이
새 워커 후보(sj-worker2, 아직 클러스터 미합류) 등장으로 더 이상 항상
참이 아님을 확인. 이 게이트는 지금까지 preview의 목적지 노드를 "active
pod가 지금 떠 있는 노드"로 그냥 가정해 그 노드만 조회했다 - 워커가
1개뿐인 지금은 이 가정이 우연히 참이지만(스케줄 가능한 노드가 하나뿐이라
다를 수 없음), 두 번째 워커가 join되면 preview가 실제로 어디에 뜰지는
스케줄러 배치 결정(anti-affinity 등)에 달려 있어 이 가정이 깨진다.
`count_schedulable_worker_nodes()`로 스케줄 가능한 워커 수를 먼저 확인해,
1개일 때만 기존 방식(active의 노드 = preview의 노드)을 쓰고, 1개가
아니거나 조회 자체가 실패하면 "목적지 확정 불가"로 즉시 차단한다 -
`ALWAYS_BLOCK_NON_NATIVE_ARM`이나 통과 임계값은 전혀 건드리지 않았다
(지시대로 "안전한 노드 식별·기록"만 고쳤다). 실제 다중 노드 배치가
생기면(§198 조사에서 다룬 Argo Rollouts BlueGreen `antiAffinity` 등)
이 함수만으로는 목적지를 예측할 수 없다는 뜻이므로, 그때는 이 파일이
아니라 배치 설계 자체에 맞춰 새 해석 로직을 별도로 추가해야 한다 -
이 파일은 그 로직을 추정해서 만들지 않는다.
"""
import time
from typing import Callable, Optional

import requests
from kubernetes import client

from active_pod_resolver import get_active_pods, load_kube_config
from memory_pressure_adapter import (
    GIB,
    LOCAL_PROMETHEUS_URL,
    PROM_QUERY_TIMEOUT_SEC,
    FRESHNESS_MAX_AGE_SEC,
    MIN_NODE_AVAILABLE_WITH_PROJECTED_STRESS_BYTES,
    get_node_ip,
    get_pod_details,
)
from run_once import Injector, TrialInvalid

# §197 - 이제 pass/fail을 가르는 임계값이 아니라 "투영 잔여치를 계산해
# 기록만 하는" 용도로만 쓴다(위 docstring §197 참고). §196/§197 조사에서
# 실측한 4건의 드롭(3.124~3.510GiB) 중 최댓값 - 새 숫자를 발명하지 않고
# 실측 최댓값을 그대로 쓴다. 표본 4개뿐이라 이보다 큰 드롭이 실제로는
# 없다고 보장하지 않는다(관찰된 최댓값일 뿐 이론적 상한 아님).
WORST_OBSERVED_PREVIEW_STARTUP_DROP_BYTES = 3.510 * GIB

# §197 - 위 docstring의 재계산(§96.3의 4GiB·§50.5의 3GiB 둘 다 이 맥락에
# 투영 형태로 재사용하면 성공 3건까지 차단하거나 자기모순이 생김, 성공
# 3건 자체도 서로 완전히 일관되지 않음)에 따라, 방어 가능한 통과 문턱이
# 아직 없다. non-native arm은 항상 차단한다 - True를 False로 바꾸는 것은
# 곧 "이제 안전하다고 판단했다"는 뜻이므로, 그렇게 바꿀 때는 반드시 새
# 근거(추가 관측치로 재도출한 값 등)를 이 파일에 함께 남길 것.
ALWAYS_BLOCK_NON_NATIVE_ARM = True

# 하위 호환용 별칭(과거 §196 코드/문서가 이 이름을 참조할 수 있음) - 더 이상
# pass/fail 비교에 쓰이지 않는다.
NODE_MEMAVAILABLE_MIN_BEFORE_PREVIEW_BYTES = MIN_NODE_AVAILABLE_WITH_PROJECTED_STRESS_BYTES


def count_schedulable_worker_nodes() -> Optional[int]:
    """§199(2026-09-30) - 새 노드(sj-worker2) 준비 검토 계기로 발견된 문제:
    이 게이트는 지금까지 "preview는 항상 active와 같은 노드에 뜬다"고
    가정하고 active pod의 노드만 확인했다. 워커 노드가 1개뿐인 지금은 이
    가정이 실제로 참이지만(스케줄 가능한 노드가 하나뿐이니 다를 수가
    없음), 두 번째 워커가 클러스터에 join되고 나면 이 가정이 더 이상
    자동으로 참이 아니다 - preview가 어느 노드에 뜰지는 스케줄러의 실제
    배치 결정(anti-affinity 설정 여부·자원 상황 등)에 달려 있고, 이
    함수는 그 배치를 예측하지 않는다.

    이 함수는 "NoSchedule/NoExecute taint가 없는(=스케줄 가능한) 노드가
    몇 개인가"만 센다(읽기 전용, 클러스터 상태 변경 없음). 반환값
    해석은 호출자 몫이다: 1개면 지금 이 클러스터처럼 목적지가 모호할
    수 없으니 active의 노드로 확정해도 안전하고, 2개 이상이면 이
    함수만으로는 목적지를 확정할 수 없으므로 fail-closed(차단)해야
    한다 - 이 파일은 스케줄러 결정을 추정하는 로직을 새로 만들지
    않는다. 조회 자체가 실패하면 None(호출자가 fail-closed 처리)."""
    try:
        load_kube_config()
        nodes = client.CoreV1Api().list_node().items
    except Exception:
        return None
    count = 0
    for n in nodes:
        taints = (n.spec.taints or []) if n.spec else []
        if any(t.effect in ("NoSchedule", "NoExecute") for t in taints):
            continue
        count += 1
    return count


def check_node_memavailable_before_preview(
        node_name: Optional[str], node_ip: Optional[str],
        prom_url: str = LOCAL_PROMETHEUS_URL,
        expected_startup_drop_bytes: float = WORST_OBSERVED_PREVIEW_STARTUP_DROP_BYTES) -> dict:
    """워커 노드의 최신 MemAvailable을 조회하고, 실측된 최악의 preview 기동
    드롭을 뺀 투영 잔여치를 계산해 기록한다(§197 - 더 이상 pass/fail을
    가르지 않음, ALWAYS_BLOCK_NON_NATIVE_ARM 참고). 조회 자체가
    안 되거나(누락/오래됨/노드 불특정/조회 오류) 노드를 특정 못 하면 여전히
    ok=False로 fail-closed 반환한다(이 부분은 §196과 동일 -
    cpu_pressure_demo_adapter.get_pod_cpu_usage_cores_diagnostic()과 같은
    reason 체계: empty_result/stale_sample(age=...)/http_status_*/
    request_exception:*/parse_error:*, 이 함수 전용으로
    node_identity_unconfirmed/ambiguous_result 추가). 조회에 성공해도
    ok은 항상 False다(호출자 wrap_injector_with_node_resource_gate()가
    ALWAYS_BLOCK_NON_NATIVE_ARM에 따라 이렇게 만든다) - available_gb/
    projected_residual_gb는 판정용이 아니라 감사·향후 임계값 도출용
    기록이다. 반환값 {"ok": bool, "reason": str, "available_gb": float|None,
    "projected_residual_gb": float|None, "expected_drop_gb": float}."""
    expected_drop_gb = expected_startup_drop_bytes / GIB
    if not node_name or not node_ip:
        return {"ok": False, "reason": "node_identity_unconfirmed", "available_gb": None,
                "projected_residual_gb": None, "expected_drop_gb": expected_drop_gb}

    promql = f'node_memory_MemAvailable_bytes{{instance="{node_ip}:9100"}}'
    try:
        r = requests.get(f"{prom_url}/api/v1/query", params={"query": promql}, timeout=PROM_QUERY_TIMEOUT_SEC)
    except requests.exceptions.RequestException as e:
        return {"ok": False, "reason": f"request_exception:{type(e).__name__}", "available_gb": None,
                "projected_residual_gb": None, "expected_drop_gb": expected_drop_gb}
    if r.status_code != 200:
        return {"ok": False, "reason": f"http_status_{r.status_code}", "available_gb": None,
                "projected_residual_gb": None, "expected_drop_gb": expected_drop_gb}
    try:
        body = r.json()
        if body.get("status") != "success":
            return {"ok": False, "reason": "non_success_status", "available_gb": None,
                    "projected_residual_gb": None, "expected_drop_gb": expected_drop_gb}
        result = body.get("data", {}).get("result", [])
        if not result:
            return {"ok": False, "reason": "empty_result", "available_gb": None,
                    "projected_residual_gb": None, "expected_drop_gb": expected_drop_gb}
        if len(result) > 1:
            return {"ok": False, "reason": f"ambiguous_result(n={len(result)})", "available_gb": None,
                    "projected_residual_gb": None, "expected_drop_gb": expected_drop_gb}
        sample_ts, sample_value = result[0]["value"]
        age_sec = time.time() - float(sample_ts)
        if age_sec > FRESHNESS_MAX_AGE_SEC:
            return {"ok": False, "reason": f"stale_sample(age={age_sec:.1f}s)", "available_gb": None,
                    "projected_residual_gb": None, "expected_drop_gb": expected_drop_gb}
        available_bytes = float(sample_value)
    except (ValueError, KeyError, IndexError, TypeError) as e:
        return {"ok": False, "reason": f"parse_error:{type(e).__name__}", "available_gb": None,
                "projected_residual_gb": None, "expected_drop_gb": expected_drop_gb}

    available_gb = available_bytes / GIB
    projected_residual_gb = (available_bytes - expected_startup_drop_bytes) / GIB
    # §197 - 방어 가능한 통과 문턱이 없어 측정에 성공해도 항상 차단한다.
    return {"ok": False, "reason": "no_defensible_pass_threshold_yet(§197 - see module docstring)",
            "available_gb": available_gb, "projected_residual_gb": projected_residual_gb,
            "expected_drop_gb": expected_drop_gb}


def wrap_injector_with_node_resource_gate(
        injector: Injector, arm: str,
        get_active_pods_fn: Callable[[], list] = get_active_pods,
        get_pod_details_fn: Callable[[str], Optional[dict]] = get_pod_details,
        get_node_ip_fn: Callable[[str], Optional[str]] = get_node_ip,
        check_fn: Callable[..., dict] = check_node_memavailable_before_preview,
        count_schedulable_worker_nodes_fn: Callable[[], Optional[int]] = count_schedulable_worker_nodes,
) -> Injector:
    """native는 preview 자체가 없으므로 그대로 반환. non-native만 감싼다.

    합성 순서(중요): 반드시 arm_controller.wrap_injector_with_preview_prep()
    "다음"(바깥쪽)에 적용해야 한다 - 이 게이트의 prepare()가 먼저 실행돼야
    실패 시 preview 자체가 아예 생성되지 않는다.
        injector = make_cpu_pressure_injector(...)
        injector = arm_controller.wrap_injector_with_preview_prep(injector, arm)
        injector = wrap_injector_with_node_resource_gate(injector, arm)  # 이 순서

    이 게이트가 막으면 TrialInvalid를 던진다(HarnessCorrupted 아님 - 이
    시점엔 preview를 포함해 아무 것도 아직 안 만들었으므로 클러스터가
    더럽혀질 수 없다). injector.cleanup()은 건드리지 않는다 - 안쪽
    prepare_with_preview()가 아예 호출되지 않아 prep_state["last"]가
    None으로 남고, blue_green_prep.cleanup_unpromoted_preview()는
    prep_info=None을 이미 안전하게 무시하도록 구현돼 있다(정리할 게
    없다는 뜻 - 코드 확인, 이 파일에서 새로 만든 동작 아님)."""
    if arm == "native":
        return injector

    original_prepare = injector.prepare
    gate_state = {"last": None}

    def prepare_with_node_resource_gate():
        pods = get_active_pods_fn()
        if len(pods) != 1:
            reason = f"active_pod_count_{len(pods)}"
            gate_state["last"] = {"ok": False, "reason": reason, "available_gb": None,
                                   "projected_residual_gb": None, "expected_drop_gb": None}
            raise TrialInvalid(
                f"preview_pre_creation_resource_gate: reason={reason} - active selector에 pod가 "
                f"{len(pods)}개(1개여야 함) - fail-closed, preview 생성 시도 안 함")

        schedulable_count = count_schedulable_worker_nodes_fn()
        if schedulable_count is None:
            gate_state["last"] = {"ok": False, "reason": "schedulable_node_count_unknown",
                                   "available_gb": None, "projected_residual_gb": None, "expected_drop_gb": None}
            raise TrialInvalid(
                "preview_pre_creation_resource_gate: reason=schedulable_node_count_unknown - "
                "스케줄 가능 노드 수 조회 실패로 배치 대상 확정 불가(fail-closed), preview 생성 시도 안 함")
        if schedulable_count != 1:
            reason = f"preview_target_node_ambiguous(schedulable_workers={schedulable_count})"
            gate_state["last"] = {"ok": False, "reason": reason,
                                   "available_gb": None, "projected_residual_gb": None, "expected_drop_gb": None}
            raise TrialInvalid(
                f"preview_pre_creation_resource_gate: reason={reason} - 스케줄 가능한 워커가 1개가 "
                f"아니라서(§199) active의 노드가 preview의 실제 목적지라고 확정할 수 없음(fail-closed), "
                f"preview 생성 시도 안 함")

        details = get_pod_details_fn(pods[0]["name"])
        node_name = details.get("node_name") if details else None
        node_ip = get_node_ip_fn(node_name) if node_name else None

        result = check_fn(node_name, node_ip)
        gate_state["last"] = result
        if not result["ok"]:
            detail = ""
            if result.get("available_gb") is not None:
                detail = (f" available={result['available_gb']:.2f}GiB "
                          f"projected_residual={result['projected_residual_gb']:.2f}GiB "
                          f"(예상드롭 {result['expected_drop_gb']:.2f}GiB 반영)")
            raise TrialInvalid(
                f"preview_pre_creation_resource_gate: node={node_name or '?'} reason={result['reason']}"
                f"{detail} (arm={arm}) - preview 생성 자체를 시도하지 않음(fail-closed)")

        original_prepare()

    injector.prepare = prepare_with_node_resource_gate
    injector.get_node_resource_gate_info = lambda: gate_state["last"]
    return injector


# ============================================================================
# §200(2026-09-30) - 무주입·capture-sink 전용 1회 진단 예외
# docs/design/phase8-no-injection-gate-options-20260930.md 선택지 B(승인됨).
# 위 ALWAYS_BLOCK_NON_NATIVE_ARM·check_node_memavailable_before_preview()·
# wrap_injector_with_node_resource_gate()는 이 섹션에서 전혀 건드리지 않는다
# - run_if_threshold_no_injection_long_trial.py만 이 섹션의 함수를 import한다
# (다른 CPU 압박 러너·공식 러너는 이 이름을 참조하지 않음 - grep으로 감사
# 가능). 기본은 off - 호출자가 CLI로 매번 명시적으로 켜야 한다.
#
# **이 예외를 쓰면 기존 안전 차단(스케줄 가능 워커 2개일 때의
# preview_target_node_ambiguous, ALWAYS_BLOCK_NON_NATIVE_ARM)을 의도적으로
# 우회한다.** 아래 두 조건을 전부 만족해야만 preview 생성을 허용한다:
#   1) 호출자가 run_mode="no_injection_long"을 정확히 문자열 일치로 전달
#      (CPU 압박 러너는 이 문자열을 쓰지 않음 - 완벽한 런타임 증명은 아니고
#      "명시적으로 이 모드라고 선언"까지만 강제한다는 한계를 명시한다).
#   2) 목적지 사전 예측(hard anti-affinity 설정 확인·워커 정확히 2개·둘 다
#      Ready·active 노드 확인)이 성립.
# 둘 다 성립해도, 실제 preview가 예측 노드에 뜨지 않으면(사후 검증) 즉시
# TrialInvalid로 무효 처리한다 - run_once.py의 기존 finally 블록이
# injector.cleanup()을 항상 호출하므로(코드 확인, 707/895/1015행) 여기서
# 별도 정리를 새로 구현하지 않는다. 새 pass 임계값은 만들지 않는다 - 이
# 섹션은 "배치 예측이 맞았는가"만 판단한다.
# ============================================================================

NO_INJECTION_DIAGNOSTIC_EXPECTED_RUN_MODE = "no_injection_long"

_ROLLOUTS_GROUP = "argoproj.io"
_ROLLOUTS_VERSION = "v1alpha1"
_ROLLOUTS_PLURAL = "rollouts"


def get_rollout_hard_anti_affinity_configured(
        name: str = "vllm-serving", namespace: str = "vllm-serving") -> Optional[bool]:
    """Rollout의 spec.strategy.blueGreen.antiAffinity.
    requiredDuringSchedulingIgnoredDuringExecution 필드가 존재하는지만
    확인한다(읽기 전용, 값 내용은 안 봄 - 빈 {}면 충분, Stage C에서 이
    형태로 적용됨). 조회 자체가 실패하면 None(호출자가 fail-closed 처리)."""
    try:
        load_kube_config()
        obj = client.CustomObjectsApi().get_namespaced_custom_object(
            _ROLLOUTS_GROUP, _ROLLOUTS_VERSION, namespace, _ROLLOUTS_PLURAL, name)
    except Exception:
        return None
    blue_green = (((obj.get("spec") or {}).get("strategy") or {}).get("blueGreen") or {})
    anti_affinity = blue_green.get("antiAffinity")
    if anti_affinity is None:
        return False
    return "requiredDuringSchedulingIgnoredDuringExecution" in anti_affinity


def predict_no_injection_preview_destination(
        get_active_pods_fn: Callable[[], list] = get_active_pods,
        get_pod_details_fn: Callable[[str], Optional[dict]] = get_pod_details,
        count_schedulable_worker_nodes_fn: Callable[[], Optional[int]] = count_schedulable_worker_nodes,
        get_hard_anti_affinity_fn: Callable[..., Optional[bool]] = get_rollout_hard_anti_affinity_configured,
        list_nodes_fn: Optional[Callable[[], list]] = None) -> dict:
    """§200 사전 목적지 예측(읽기 전용, 클러스터 상태 변경 없음). 문서의
    5개 조건을 전부 확인한다: hard anti-affinity 설정, 스케줄 가능 워커
    정확히 2개, 둘 다 Ready, active pod 노드 확인, 나머지 하나를 예측
    목적지로 확정. 성공: {"ok": True, "predicted_node": str,
    "active_node": str}. 실패: {"ok": False, "reason": str} - 새 pass
    임계값이 아니라 "예측 가능한가"만 판단한다."""
    anti_affinity_ok = get_hard_anti_affinity_fn()
    if not anti_affinity_ok:
        reason = "hard_anti_affinity_query_failed" if anti_affinity_ok is None else "hard_anti_affinity_not_configured"
        return {"ok": False, "reason": reason}

    schedulable_count = count_schedulable_worker_nodes_fn()
    if schedulable_count != 2:
        return {"ok": False, "reason": f"schedulable_workers_not_exactly_2({schedulable_count})"}

    pods = get_active_pods_fn()
    if len(pods) != 1:
        return {"ok": False, "reason": f"active_pod_count_{len(pods)}"}
    details = get_pod_details_fn(pods[0]["name"])
    active_node = details.get("node_name") if details else None
    if not active_node:
        return {"ok": False, "reason": "active_node_unresolved"}

    try:
        if list_nodes_fn is not None:
            nodes = list_nodes_fn()
        else:
            load_kube_config()
            nodes = client.CoreV1Api().list_node().items
    except Exception:
        return {"ok": False, "reason": "node_list_query_failed"}

    schedulable_names = []
    for n in nodes:
        taints = (n.spec.taints or []) if n.spec else []
        if any(t.effect in ("NoSchedule", "NoExecute") for t in taints):
            continue
        ready = any(c.type == "Ready" and c.status == "True" for c in (n.status.conditions or []))
        if not ready:
            return {"ok": False, "reason": f"schedulable_node_not_ready({n.metadata.name})"}
        schedulable_names.append(n.metadata.name)

    if active_node not in schedulable_names:
        return {"ok": False, "reason": "active_node_not_in_schedulable_set"}
    other_nodes = [n for n in schedulable_names if n != active_node]
    if len(other_nodes) != 1:
        return {"ok": False, "reason": f"unexpected_other_node_count({len(other_nodes)})"}

    return {"ok": True, "predicted_node": other_nodes[0], "active_node": active_node}


def resolve_preview_actual_node(
        name: str = "vllm-serving", namespace: str = "vllm-serving",
        list_pods_fn: Optional[Callable[[str], list]] = None) -> Optional[str]:
    """preview(Rollout의 현재 currentPodHash)가 실제로 배치된 노드를
    조회한다(읽기 전용). 조회 실패·아직 스케줄 안 됨(node_name 없음)이면
    None."""
    import blue_green_prep as bgp
    status = bgp.get_blue_green_status(name, namespace)
    pod_hash = status.get("current_pod_hash")
    if not pod_hash:
        return None
    try:
        if list_pods_fn is not None:
            pods = list_pods_fn(pod_hash)
        else:
            load_kube_config()
            pods = client.CoreV1Api().list_namespaced_pod(
                namespace, label_selector=f"rollouts-pod-template-hash={pod_hash}").items
    except Exception:
        return None
    for p in pods:
        node_name = getattr(getattr(p, "spec", None), "node_name", None)
        if node_name:
            return node_name
    return None


def wrap_injector_with_no_injection_diagnostic_exception(
        injector: Injector, arm: str, run_mode: str,
        predict_fn: Callable[..., dict] = predict_no_injection_preview_destination,
        resolve_actual_node_fn: Callable[..., Optional[str]] = resolve_preview_actual_node,
) -> Injector:
    """§200 - 무주입 전용 1회 진단 예외(모듈 상단 섹션 설명 참고). native는
    그대로 반환. run_mode가 NO_INJECTION_DIAGNOSTIC_EXPECTED_RUN_MODE와
    정확히 일치하지 않으면 이 wrap 자체를 구성하는 시점에 즉시 ValueError
    (다른 모드로의 재사용 시도를 막음 - TrialInvalid가 아니라 ValueError인
    이유는 이건 "이번 trial이 무효"가 아니라 "이 함수를 잘못 호출한
    프로그래밍 오류"이기 때문).

    합성 순서: arm_controller.wrap_injector_with_preview_prep() 바로
    다음(바깥쪽)에 적용해야 한다(기존 node_resource_safety_gate와 동일한
    이유 - 이 게이트가 먼저 실행돼야 실패 시 원래 preview 준비 로직이
    시작되지 않음). node_resource_safety_gate.wrap_injector_with_node_
    resource_gate()는 이 예외 경로에서 사용하지 않는다(대체하는 것이지
    같이 쌓지 않음 - 문서의 "기존 게이트는 그대로, 새 진입점만 추가"
    원칙)."""
    if arm == "native":
        return injector
    if run_mode != NO_INJECTION_DIAGNOSTIC_EXPECTED_RUN_MODE:
        raise ValueError(
            f"wrap_injector_with_no_injection_diagnostic_exception: run_mode={run_mode!r}는 허용되지 "
            f"않음(정확히 {NO_INJECTION_DIAGNOSTIC_EXPECTED_RUN_MODE!r}이어야 함) - 이 예외는 무주입 "
            f"러너 전용이며 다른 모드에서 재사용할 수 없음")

    original_prepare = injector.prepare
    exception_state = {"last": None}

    def prepare_with_diagnostic_exception():
        prediction = predict_fn()
        exception_state["last"] = {"prediction": prediction, "actual_node": None}
        if not prediction["ok"]:
            raise TrialInvalid(
                f"no_injection_diagnostic_exception: 사전 목적지 예측 실패 reason={prediction['reason']} "
                f"(arm={arm}, run_mode={run_mode}) - 기존 안전 차단을 우회하지 않고 fail-closed, "
                f"preview 생성 시도 안 함")

        original_prepare()  # 여기서 실패(준비 타임아웃 등)하면 그대로 전파 - 새로 안 잡음

        actual_node = resolve_actual_node_fn()
        exception_state["last"]["actual_node"] = actual_node
        predicted_node = prediction["predicted_node"]
        if actual_node != predicted_node:
            raise TrialInvalid(
                f"no_injection_diagnostic_exception: 실제 배치 노드({actual_node})가 예측"
                f"({predicted_node})과 다름 - 무효 처리(정리는 기존 injector.cleanup() 경로가 "
                f"이어서 수행함)")

    injector.prepare = prepare_with_diagnostic_exception
    injector.get_no_injection_diagnostic_exception_info = lambda: exception_state["last"]
    return injector


# ============================================================================
# §201(2026-10-01) - CPU-sink 1회 진단(run_cpu_sink_first_stage_diagnostic.py)
# 전용 예외. §200과 목적(워커 2개 상황의 fail-closed 차단을 명시적 1회
# 진단에 한해 우회)·메커니즘(목적지 사전예측+사후검증)이 완전히 동일하므로
# predict_no_injection_preview_destination()/resolve_preview_actual_node()를
# 그대로(수정 없이) 재사용한다 - 이 두 함수는 애초에 "주입 여부"와 무관하게
# preview가 어느 노드에 뜨는지만 다룬다. §200 자체(wrap_injector_with_
# no_injection_diagnostic_exception, NO_INJECTION_DIAGNOSTIC_EXPECTED_RUN_MODE)
# 는 이 섹션에서 전혀 건드리지 않는다 - run_mode 검증이 "no_injection_long"
# 하나로 고정된 별도 함수라 그대로 둬야 감사 범위가 좁게 유지된다(합쳐서
# run_mode 허용 목록을 넓히는 대신 별도 함수로 분리 - 각 예외가 정확히
# 하나의 호출부에서만 켜진다는 성질을 유지하기 위함).
#
# 일반 CPU 주입 경로(run_if_threshold_candidate_smoke.py의 cpu_pressure·
# fixed_threshold_pressure·no_switch_control, run_cpu_demo_shadow_*.py 등)는
# 전부 여전히 wrap_injector_with_node_resource_gate()의 기존 fail-closed
# 차단을 그대로 받는다 - 이 새 예외는 run_cpu_sink_first_stage_diagnostic.py
# 단 한 곳에서만, 명시적 run_mode 문자열로만 활성화된다(완벽한 런타임
# 증명은 아니고 "명시적으로 이 모드라고 선언"까지만 강제한다는 한계는
# §200과 동일).
# ============================================================================

CPU_SINK_DIAGNOSTIC_EXPECTED_RUN_MODE = "cpu_sink_first_stage_diagnostic"


def wrap_injector_with_cpu_sink_diagnostic_exception(
        injector: Injector, arm: str, run_mode: str,
        predict_fn: Callable[..., dict] = predict_no_injection_preview_destination,
        resolve_actual_node_fn: Callable[..., Optional[str]] = resolve_preview_actual_node,
) -> Injector:
    """§201 - CPU-sink 1회 진단 전용 예외(모듈 상단 섹션 설명 참고). native는
    그대로 반환. run_mode가 CPU_SINK_DIAGNOSTIC_EXPECTED_RUN_MODE와 정확히
    일치하지 않으면 이 wrap 자체를 구성하는 시점에 즉시 ValueError(다른
    모드로의 재사용 시도를 막음 - §200과 동일 이유로 TrialInvalid가 아님:
    "이번 trial이 무효"가 아니라 "이 함수를 잘못 호출한 프로그래밍 오류").

    합성 순서·배치 원칙은 §200과 동일: arm_controller.wrap_injector_with_
    preview_prep() 바로 다음(바깥쪽)에 적용하고, wrap_injector_with_node_
    resource_gate()는 이 경로에서 쓰지 않는다(대체하는 것이지 같이 쌓지
    않음). 실제 CPU 주입(injector.inject())은 이 함수가 전혀 건드리지
    않는다 - prepare()의 preview 배치 예측/검증만 감싼다."""
    if arm == "native":
        return injector
    if run_mode != CPU_SINK_DIAGNOSTIC_EXPECTED_RUN_MODE:
        raise ValueError(
            f"wrap_injector_with_cpu_sink_diagnostic_exception: run_mode={run_mode!r}는 허용되지 "
            f"않음(정확히 {CPU_SINK_DIAGNOSTIC_EXPECTED_RUN_MODE!r}이어야 함) - 이 예외는 CPU-sink "
            f"진단 러너 전용이며 다른 모드에서 재사용할 수 없음")

    original_prepare = injector.prepare
    exception_state = {"last": None}

    def prepare_with_diagnostic_exception():
        prediction = predict_fn()
        exception_state["last"] = {"prediction": prediction, "actual_node": None}
        if not prediction["ok"]:
            raise TrialInvalid(
                f"cpu_sink_diagnostic_exception: 사전 목적지 예측 실패 reason={prediction['reason']} "
                f"(arm={arm}, run_mode={run_mode}) - 기존 안전 차단을 우회하지 않고 fail-closed, "
                f"preview 생성 시도 안 함")

        original_prepare()  # 여기서 실패(준비 타임아웃 등)하면 그대로 전파 - 새로 안 잡음

        actual_node = resolve_actual_node_fn()
        exception_state["last"]["actual_node"] = actual_node
        predicted_node = prediction["predicted_node"]
        if actual_node != predicted_node:
            raise TrialInvalid(
                f"cpu_sink_diagnostic_exception: 실제 배치 노드({actual_node})가 예측"
                f"({predicted_node})과 다름 - 무효 처리(정리는 기존 injector.cleanup() 경로가 "
                f"이어서 수행함)")

    injector.prepare = prepare_with_diagnostic_exception
    injector.get_cpu_sink_diagnostic_exception_info = lambda: exception_state["last"]
    return injector


# ============================================================================
# §202(2026-10-01) - 실제 recovery-policy 승격을 동반하는 CPU 실험(run_if_
# threshold_candidate_smoke.py --mode cpu_pressure/fixed_threshold_pressure)
# 전용 1회 진단 예외. 이 파일은 지금까지 node_resource_safety_gate를 전혀
# import하지 않았다(§196~§201 작업 범위 밖 - sj-worker2 합류 전에 작성된
# rep=3~5 실행부터 그대로 미보호 상태였다는 뜻, 코드 확인). §200/§201과
# 완전히 동일한 목적지 예측/사후검증 메커니즘(predict_no_injection_preview_
# destination()/resolve_preview_actual_node(), 둘 다 수정 없이 재사용 - "주입
# 여부"·"승격 여부"와 무관하게 preview가 어느 노드에 뜨는지만 다룬다)을
# 적용한다.
#
# **기본값을 바꾼다는 점이 §200/§201과 다르다**: run_if_threshold_candidate_
# smoke.py의 cpu_pressure/fixed_threshold_pressure 모드는 이 섹션 추가 전엔
# 어떤 노드 배치 게이트도 없었다(다른 5개 CPU 주입 러너는 전부 이미
# wrap_injector_with_node_resource_gate를 쓰고 있었다 - 이 파일만 예외였던
# 것으로 보임). 그래서 이 두 모드는 이제 기본으로 표준 fail-closed 게이트
# (wrap_injector_with_node_resource_gate, §197)를 받는다 - 다른 CPU 주입
# 러너들과 동일한 수준으로 맞추는 것뿐, 판정 로직은 전혀 안 바꾼다. 이 새
# 예외는 --allow-real-promotion-diagnostic-exception을 명시해야만 그 기본
# 차단을 대체한다(같이 쌓지 않음 - §200/§201과 동일 원칙).
# ============================================================================

REAL_PROMOTION_DIAGNOSTIC_EXPECTED_RUN_MODE = "cpu_pressure_real_promotion_diagnostic"


def wrap_injector_with_real_promotion_diagnostic_exception(
        injector: Injector, arm: str, run_mode: str,
        predict_fn: Callable[..., dict] = predict_no_injection_preview_destination,
        resolve_actual_node_fn: Callable[..., Optional[str]] = resolve_preview_actual_node,
) -> Injector:
    """§202 - 실제 승격 동반 CPU 실험 전용 1회 진단 예외(모듈 상단 섹션
    설명 참고). native는 그대로 반환. run_mode가 REAL_PROMOTION_DIAGNOSTIC_
    EXPECTED_RUN_MODE와 정확히 일치하지 않으면 이 wrap 자체를 구성하는
    시점에 즉시 ValueError(§200/§201과 동일 이유 - 프로그래밍 오류이지
    trial 무효가 아님).

    이 함수는 preview 배치 예측/검증만 감싼다 - IF 모델·후보 임계값(-0.04)·
    3연속 판정·mem_slope 조건·policy.decide() 판정·CPU 주입 강도(DEMO_
    STAGES)·실제 recovery-policy 연결 여부는 한 글자도 건드리지 않는다
    (run_cpu_pressure_trial() 자체도 이 함수가 감싸지 않음 - injector.
    prepare()만 감싼다)."""
    if arm == "native":
        return injector
    if run_mode != REAL_PROMOTION_DIAGNOSTIC_EXPECTED_RUN_MODE:
        raise ValueError(
            f"wrap_injector_with_real_promotion_diagnostic_exception: run_mode={run_mode!r}는 "
            f"허용되지 않음(정확히 {REAL_PROMOTION_DIAGNOSTIC_EXPECTED_RUN_MODE!r}이어야 함) - "
            f"이 예외는 실제 승격 동반 CPU 진단 전용이며 다른 모드에서 재사용할 수 없음")

    original_prepare = injector.prepare
    exception_state = {"last": None}

    def prepare_with_diagnostic_exception():
        prediction = predict_fn()
        exception_state["last"] = {"prediction": prediction, "actual_node": None}
        if not prediction["ok"]:
            raise TrialInvalid(
                f"real_promotion_diagnostic_exception: 사전 목적지 예측 실패 reason="
                f"{prediction['reason']} (arm={arm}, run_mode={run_mode}) - 기존 안전 차단을 "
                f"우회하지 않고 fail-closed, preview 생성 시도 안 함")

        original_prepare()  # 여기서 실패(준비 타임아웃 등)하면 그대로 전파 - 새로 안 잡음

        actual_node = resolve_actual_node_fn()
        exception_state["last"]["actual_node"] = actual_node
        predicted_node = prediction["predicted_node"]
        if actual_node != predicted_node:
            raise TrialInvalid(
                f"real_promotion_diagnostic_exception: 실제 배치 노드({actual_node})가 예측"
                f"({predicted_node})과 다름 - 무효 처리(정리는 기존 injector.cleanup() 경로가 "
                f"이어서 수행함)")

    injector.prepare = prepare_with_diagnostic_exception
    injector.get_real_promotion_diagnostic_exception_info = lambda: exception_state["last"]
    return injector
