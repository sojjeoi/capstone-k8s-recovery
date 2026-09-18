#!/usr/bin/env python3
"""RFC3339 timestamp 두 개의 실제 선후 관계를 판정한다.

HEADROOM-COLDSTART-03-ATTEMPT1에서 실제로 발생한 버그(docs/design/
phase8-blue-green-preflight-incident.md 해당 절 참고)를 계기로 분리했다.
원인은 두 가지가 겹쳤다: (1) 'Z' 종결 문자열과 '+00:00' 종결 문자열을
그냥 문자열로 비교해 ASCII 순서('.'가 '+'보다 큼) 때문에 실제로는 이른
시각이 늦은 것으로 잘못 판정됨 (2) K8s Ready condition의
lastTransitionTime은 초 단위로 절삭되어 있어, 두 이벤트가 같은 초
안에 있으면 이 필드만으로는 실제 순서를 알 수 없는데도 절삭된 값을
그대로 비교해 잘못된 False를 반환함.

이 모듈은 (1)을 정확한 파싱으로 고치고, (2)를 조용히 틀리는 대신
명시적으로 '확인 불가'(None)로 반환한다 - 원본에 없는 정밀도를
만들어내지 않는다."""
import re
from datetime import datetime

_RFC3339_RE = re.compile(r"^(?P<base>.*T\d{2}:\d{2}:\d{2})(?P<frac>\.\d+)?(?P<tz>Z|[+-]\d{2}:\d{2})$")


def parse_rfc3339(ts: str) -> datetime:
    """'Z' 또는 '+00:00'류 종결, 임의 자릿수 소수초를 모두 timezone-aware
    datetime으로 정확히 파싱한다."""
    if not ts:
        raise ValueError("timestamp가 비어있음")
    m = _RFC3339_RE.match(ts.strip())
    if not m:
        raise ValueError(f"RFC3339으로 파싱할 수 없는 timestamp: {ts!r}")
    base, frac, tz = m.group("base"), m.group("frac"), m.group("tz")
    if tz == "Z":
        tz = "+00:00"
    if frac:
        digits = frac[1:][:6].ljust(6, "0")  # datetime은 마이크로초(6자리)까지만 허용
        base = f"{base}.{digits}"
    return datetime.fromisoformat(base + tz)


def compare_before(earlier_candidate: str, later_candidate: str):
    """earlier_candidate가 later_candidate보다 실제로 이전이면 True, 이후면
    False. 둘 중 하나라도 소수초 표기가 없는(K8s condition의
    lastTransitionTime처럼 초 단위로만 기록된) 상태에서 두 값의 초 단위가
    같으면, 그 쪽의 실제 소수초 값을 알 수 없어 순서를 확정할 수 없다 -
    추정하지 않고 None을 반환한다."""
    a = parse_rfc3339(earlier_candidate)
    b = parse_rfc3339(later_candidate)
    same_second = a.replace(microsecond=0) == b.replace(microsecond=0)
    has_subsecond = "." in earlier_candidate and "." in later_candidate
    if same_second and not has_subsecond:
        return None
    return a < b
