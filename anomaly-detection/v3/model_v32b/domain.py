#!/usr/bin/env python3
"""§79 - v3.2b 정상 domain 재정의의 핵심. `qualify_normal_profile.
judge_qualification()`이 이미 만드는 `exclusion_reasons`(고정된 문자열
어휘)를 두 축으로 분리한다:
  - infrastructure-normal: Chaos/fault 없음, restart/OOM/Node 이상 없음,
    topology/Endpoint/promotion/cleanup 정상, 요청 성공률 100%, metric
    결측 없음, feature window가 session 경계 안(=현재 judge_qualification
    이 실제로 검사하는 전부, latency 자체만 빼고).
  - SLO label: `t_slo` 존재 여부(sustained SLO 위반) - **infrastructure-
    normal 판정에 영향을 주지 않는다.** §78에서 survivorship bias
    위험을 확인한 것이 이 분리의 근거."""

# judge_qualification()이 만드는 문자열과 정확히 대응(qualify_normal_profile.py 참고) -
# 이 중 하나라도 exclusion_reasons에 있으면 infrastructure 축이 깨진 것이다.
_INFRASTRUCTURE_MARKERS = [
    "요청 성공률", "Node 상태", "restartCount", "OOMKilled", "target 교체", "promotion",
    "Endpoint", "무효 window", "cleanup", "session 경계",
]
# latency/SLO 축 - infrastructure-normal 여부와 무관하게 별도 label로만 기록한다.
_SLO_DOMAIN_MARKERS = ["sustained SLO 위반", "비정상적인", "다초 단위"]


def classify_exclusion_reasons(exclusion_reasons: list) -> dict:
    """반환: {"infrastructure_normal": bool, "infrastructure_issues": [...],
    "slo_violation_reasons": [...], "unclassified_reasons": [...]}.
    `unclassified_reasons`가 비어있지 않으면 새로운 종류의 사유가 생긴
    것이므로 이 함수의 마커 목록을 갱신해야 한다(침묵 실패 방지)."""
    reasons = exclusion_reasons or []
    infra_issues = [r for r in reasons if any(m in r for m in _INFRASTRUCTURE_MARKERS)]
    slo_reasons = [r for r in reasons if any(m in r for m in _SLO_DOMAIN_MARKERS)]
    unclassified = [r for r in reasons if r not in infra_issues and r not in slo_reasons]
    return {
        "infrastructure_normal": len(infra_issues) == 0,
        "infrastructure_issues": infra_issues,
        "slo_violation_reasons": slo_reasons,
        "unclassified_reasons": unclassified,
    }


def slo_label(session: dict) -> str:
    """session JSON 전체를 받아 SLO label만 뽑는다 - "sustained_violation"
    또는 "clean" 둘 중 하나(이번 범위에서 다른 outcome 종류를 새로 만들지
    않음)."""
    return "sustained_violation" if session.get("t_slo") else "clean"
