#!/usr/bin/env python3
"""§171(2026-09-26) - CPU 압박 시연 전용 조기경보 규칙. retry3 실행에서
자동 탐지가 첫 개별 느린 요청·공식 t_slo보다 늦게 반응한 것을 확인한 뒤,
그 trial의 detector-evidence-log를 사후에 다시 읽어 만들었다.

**이 규칙은 사전 등록된 과학적 발견이 아니라 n=1 trial을 본 뒤 만든 시연
보조 규칙이다.** 기존 공식 IsolationForest 모델·정책·SLO·임계값·CPU 압박
stage 설정은 이 파일에서 전혀 참조하지도 바꾸지도 않는다 - 원시 cpu_mean
feature 하나만 보는, 그것들과 완전히 독립된 별도 판정이다. 오탐률이 검증
안 됐으므로 공식 실험 결과·성과로 취급하면 안 된다. 이 파일은 아직 실제
정책 결정 경로에 연결돼 있지 않다(의도적 - 이번 작업 범위 밖).

retry3 재구성 근거(results/capstone_cpu_demo/detector-evidence-cpu_pressure_
demo-proposed-01-retry3-capstone-cpu-demo-166-v1.jsonl, 2026-09-26 사후분석,
미래 stage 정보 전혀 안 씀 - 각 평가는 그 시점까지의 raw_feature_vector만
사용): 사전 주입 기준선(seq1, 19:22:34) cpu_mean=1.243. 주입 후 cpu_mean이
seq3(19:23:08)=1.725(+38.8%)부터 뚜렷이 상승해 seq9(19:24:47)=2.107(+69.5%)
까지 계속 상승했다 - 이 구간(seq3~9) 전부가 그 trial의 '첫 개별 느린
요청'(19:24:58.84, SLO 0.648s 초과)보다 최소 11초~1분50초 앞선다. 반면 같은
구간의 공식 IF 종합 score는 -0.10~+0.05 사이를 오가며 뚜렷한 이상 신호를
내지 않았다(다른 7개 feature가 희석시켰을 것으로 추정 - 확정 아님, 공식
모델의 결함이라 단정하지도 않음). 그래서 원시 cpu_mean만 보는 이 규칙이
그 trial에서는 더 일찍 반응했을 후보다."""
from typing import Optional, Sequence, Tuple

# n=1(retry3) 관찰에서 역산 - 검증된 오탐률 아님. baseline 대비 30% 상승:
# retry3 seq3(+38.8%)는 잡되 seq2(-12.9%, 주입 직후 자연 변동) 노이즈는
# 거르도록 여유를 둠.
RELATIVE_RISE_FACTOR = 1.3
# 정상 상태에서 단일 샘플 노이즈로 불필요하게 전환하지 않도록 하는 조건 -
# 기존 시스템의 "연속 판정" 철학(계약서 3-연속 규칙)과 같은 발상이지만 이
# 규칙 자체는 별도이므로 다른 숫자를 쓴다(2 - 시연 보조 규칙이라 3보다는
# 짧게, 그러나 1(무조건 즉시)보다는 안전하게).
CONSECUTIVE_REQUIRED = 2


def find_demo_early_warning_trigger(
    cpu_mean_series: Sequence[Tuple[str, float]],
    baseline_cpu_mean: float,
    relative_rise_factor: float = RELATIVE_RISE_FACTOR,
    consecutive_required: int = CONSECUTIVE_REQUIRED,
) -> Optional[int]:
    """cpu_mean_series: [(timestamp_iso, cpu_mean), ...] 시간순으로 정렬된
    입력. baseline_cpu_mean은 호출자가 주입 "전"에 이미 측정해 넘겨야 한다 -
    이 함수는 Prometheus 접근도, 미래 값 조회도 하지 않는 순수 순차 스캔
    함수다(인덱스 i 판정은 series[0..i]까지만 씀 - 뒤 값을 보고 앞을
    판단하지 않는다). threshold를 relative_rise_factor 연속
    consecutive_required회 이상 만족하는 첫 인덱스를 반환, 끝까지 못
    만족하면 None."""
    threshold = baseline_cpu_mean * relative_rise_factor
    streak = 0
    for i, (_ts, cpu_mean) in enumerate(cpu_mean_series):
        if cpu_mean >= threshold:
            streak += 1
        else:
            streak = 0
        if streak >= consecutive_required:
            return i
    return None


def get_threshold(baseline_cpu_mean: float, relative_rise_factor: float = RELATIVE_RISE_FACTOR) -> float:
    return baseline_cpu_mean * relative_rise_factor
