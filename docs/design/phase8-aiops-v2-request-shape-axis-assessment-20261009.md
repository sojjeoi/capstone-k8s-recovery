# AIOps v2 request-shape 축 평가 (설계 권고, 2026-10-09, 코드·live 없음)

> 입력: 완료·잠금된 v2wl-v2 결과(v2wl90·v2wl91, feasible=false - 수정·재분류 안 함)와 Prometheus 읽기 조회.
> 재현: `experiments/results/capstone_v2_workload_pilot/request-shape-axis-analysis/`(스크립트 sha256 eeeca705…, 출력 output.txt 10c707fb…).
> 결론: **max_tokens는 이 환경에서 "SLO 정상 + running 활성화"를 분리하는 축으로 부적합 → request-shape pilot 코드를 만들지 않음.**

## 1. 확인한 사실

- ramp 요청(prompt "Hello", max_tokens 10) 체류 시간(클라이언트 관측): 평균 3.0–3.3 s, 범위 2.5–3.7 s(6단계 121건). 약 0.3 s/token(CPU 추론).
- finished_reason(Prometheus `increase(vllm:request_success_total)`, 단계 창): 모든 단계 **length 100 %, stop 0**. 생성 token 히스토그램: ramp 요청 전부 (5, 10] 구간 = 10 token, probe 전부 1 token. → max_tokens=10에서는 max_tokens가 실제 길이를 결정했다. 10 token을 넘는 길이에서 EOS(stop)로 일찍 끝나는지는 **확인 불가**(live 요청 필요).
- 활성화 ≈ 도착률 × 체류 시간(Little): 0.025→0.075 예상 / 관측 0.05–0.10, 0.10→0.30 / 0.25, 0.15→0.45 / 0.40. probe(1 token, 엔진 체류 매우 짧음)는 running에 거의 안 잡힘(L0 0/20).
- **핵심:** probe가 ramp 요청과 겹치면 지연 평균 0.25 s → 0.50 s, 겹친 probe의 12 %가 SLO 지연 임계 0.648 s 초과(395건 중 48). 안 겹친 probe는 0건.
  초과 비율은 ramp 요청 수명 전체(0–2.5 s)에 고르게 퍼져 있다(구간별 0.12–0.22) - prefill(시작)에 몰리지 않는다.

## 2. 왜 max_tokens가 부적합한가

running 활성화 비율 = 엔진에 요청이 있는 시간 비율 = probe가 겹치는 비율. 겹친 probe가 일정 비율로 임계를 넘으므로,
SLO 위험은 "요청을 몇 개로 나누느냐(RPS)"가 아니라 "겹침 시간 비율(활성화 자체)"로 정해진다. 활성화 0.20을 얻는 순간
probe의 약 20 %가 겹치고, 그중 약 12 %가 임계를 넘어 60 s 창 P95가 경계에 걸린다. 요청을 길게 하면(max_tokens↑) 같은 활성화에서
느린 probe가 한 긴 요청 안에 몰려 60 s 창에 뭉치므로 오히려 같거나 나쁘다.

frozen SLO 판정 함수로 같은 경험 모형 Monte Carlo(조건당 150회, 모형은 관측과 대체로 맞음: L1 위반 0.03 vs 관측 0/2, L4 0.99 vs 1/1):

| 조건 | P(위반) | 평균 활성화 | P(단계 성공: 정상 + 활성화 ≥ 0.20) |
|---|---|---|---|
| RPS 0.05 @ 10 token | 0.21 | 0.15 | 0.25 |
| RPS 0.10 @ 10 token | 0.81 | 0.31 | 0.19 |
| 0.025 RPS @ 20 token | 0.35 | 0.16 | 0.27 |
| 0.025 RPS @ 30 token | 0.69 | 0.24 | 0.28 |
| 0.025 RPS @ 40 token | 0.89 | 0.31 | 0.11 |
| 0.025 RPS @ 60 token | 1.00 | 0.45 | 0.00 |

어떤 max_tokens도 단계 성공 확률 약 0.3을 넘지 못하고, 유효 2 rep에서 같은 후보가 둘 다 성공할 확률은 약 0.08 이하다.
같은 결정 규칙(가장 작은 성공 max_tokens)이 안정적으로 정해질 수 없다. 추가로 10 token 초과 EOS 여부가 미확인이라 체류 시간 제어 자체도 보장되지 않는다.
(모형 한계: 0.3 s/token 선형, 겹침 시 일정 초과 확률, 관측 rep 2개 - 숫자는 방향 판단용이지 정밀 예측이 아니다.)

## 3. 다른 한 축 비교 (모두 같은 구조적 한계)

| 축(하나만 변화) | 체류 시간 제어 | SLO 결합 | 판단 |
|---|---|---|---|
| output length(max_tokens / 길이 유도 prompt) | 가능(EOS 미확인) | 활성화 = 겹침 → 같음, 긴 요청은 뭉침 | 부적합 |
| prompt length(긴 입력, prefill) | 가능 | CPU prefill은 한 step을 더 오래 점유 → 겹친 probe가 더 느려질 가능성 | 부적합(더 나쁠 가능성) |
| concurrency(동시 요청 수) | 간접 | 동시 요청 = 활성화·겹침 그대로, 더 큼 | 부적합 |

근본 원인은 request shape가 아니라 환경이다: SLO probe가 단일 CPU vLLM 엔진을 모든 부하와 공유하고, frozen SLO 지연 임계(2 × 0.324 s)가
동시 decode 1개가 만드는 probe 지연 증가(0.25 → 평균 0.50, 12 %는 0.65 초과)와 거의 같은 크기다. 그래서 "running이 20 % 보이는 정상"이
이 장치에서는 거의 존재하지 않는다.

## 4. 권고 (다음 결정 - 사용자 승인 필요, 아무것도 실행하지 않음)

1. **(권장) 축을 workload가 아니라 service-state 측정 방식으로 옮기는 재설계 검토.** 기존 원자료에서 rate 계열은 SLO 정상 단계에서도 부하를 따라 움직였다:
   생성 token/s L0 1.00 → L1 1.25–1.28 → L2 1.49–1.53 → L3 2.02, 엔진 iteration/s 1.00 → 1.18–1.21 → 1.37–1.39 → 1.74, 완료 요청/s 1.00 → 1.03 → 1.05 → 1.10.
   point-in-time `running` gauge(0.20 활성화)는 이 장치에서 정상 범위와 SLO 경계가 겹치지만, counter rate는 정상 범위 안에서도 정보가 있다.
   단, rate가 "정상 고부하 vs 성능 저하"를 구분하는지는 별개 질문이고 v2 검증 대상이다(여기서 주장하지 않음). 0.20 규칙·v2wl-v2 결과는 바꾸지 않는다 - 새 계약으로.
2. 환경 축(vLLM CPU 자원·노드 배치) 변경은 SLO 기준선(0.324 s)과 frozen 결과의 비교 가능성을 깨므로 권장하지 않음(클러스터 구조 변경 금지 범위).
3. request-shape pilot(max_tokens·prompt·concurrency)은 위 근거로 설계하지 않음. 그래도 원하면 먼저 "10 token 초과 EOS 비율" 확인용 소규모 측정이 필요하며 별도 승인 대상.

## 5. 계보(변경 없음)

v2wl-v2(완료·잠금) → [이번: request-shape 축 평가, 부적합] → 재설계 결정 → (그 뒤) workload/metric feasibility → normal training → calibration → validation → degradation.
이 평가는 학습·보정·검증 근거가 아니다.
