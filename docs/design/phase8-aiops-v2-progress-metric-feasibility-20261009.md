# AIOps v2 progress-metric exploratory 분석 (읽기 전용, 2026-10-09)

> **탐색 전용.** 학습·보정·검증·우월성·확증 근거 아님. live·학습·임계값 산정 없음. v2wl-v2 feasible=false, request-shape 축 부적합, 0.20 규칙, frozen SLO는 그대로.
> 재현: `experiments/results/exploratory_v2_progress_metric_20261009/`(스크립트 + 출력 JSON). 사전 고정 기술 규칙: 60 s 창·15 s 간격, "first departure" = 주입 뒤 기준선 [min,max] 밖 2창 연속(검정 아님).
> 아래 "정상 관측 범위 밖" 비교는 관측값 기술일 뿐 탐지 임계값이 아니다.

## A. 사용한 기존 데이터

| 출처 | 용도 | 비고 |
|---|---|---|
| v2wl90·v2wl91 각 단계(L0-L4 / L0-L2), active pod 78dfbd9c98-mglvh | 정상 workload 변화(SLO 정상 단계만 정상 범위로) | SLO 라벨은 frozen v2wl 분석 결과 그대로 |
| A/B 공식 arm a ab01a-ab05a(record_only, 전환 없음), 압박받은 active pod | CPU 압박 timeline(주입 → t_slo → 관찰 끝) | t_slo = A/B 공식 분석 primary.t_slo_first. C 임계 교차·c1 IF 조건 시각은 기준점 표시만 |
| (미사용) N1 정상 세션, B/C/K | N1 = probe만(L0와 같은 workload, 추가 정보 없음). B/C/K act는 실행 중 승격으로 serving pod가 바뀌어 pre-SLO timeline이 A/B arm a보다 덜 깨끗함 | |

모든 metric은 Prometheus 원본 scrape(retention 30 d) 읽기 조회. 새 데이터·live 없음.

## B·C. metric inventory와 의미 (Prometheus metadata + vLLM 0.28.1 소스 읽기)

| 후보 | 정확한 이름 | TYPE / 의미 | 라벨·중복 처리 | 판정 |
|---|---|---|---|---|
| CPU | `container_cpu_usage_seconds_total{container="vllm",job="kubelet"}` | counter, 누적 CPU 초. **StressChaos 압박기는 vllm 컨테이너 cgroup 안에서 돌므로 압박기 CPU가 포함됨** | pod 단위 1줄 | rate 가능 |
| CPU throttling(보조) | `container_cpu_cfs_throttled_periods_total` / `..._periods_total` | counter, 한도(limit 3 core) 초과로 막힌 주기 | 같음 | rate 비율 가능 |
| memory | `container_memory_working_set_bytes` | gauge | 같음 | |
| 생성 token | `vllm:generation_tokens_total` | counter "Number of generation tokens processed." 엔진 출력마다 증가 | `job="vllm-active"`만(같은 pod를 preview job도 수집) | rate 가능 |
| prompt token | `vllm:prompt_tokens_total` | counter "Number of prefill tokens processed." | 같음 | |
| 완료 요청 | `vllm:request_success_total` | counter, 요청이 끝날 때 증가(finished_reason 라벨 합) - 도착 아님 | 같음, finished_reason 합 | |
| engine 출력 batch | `vllm:iteration_tokens_total_count` | histogram count. 소스: frontend가 출력 1개 이상인 EngineCoreOutputs를 받을 때마다 1번 observe(prompt computed + generation token 수) → "token을 낸 엔진 step 수"에 가까움(scheduler loop 전체 횟수 아님) | 같음 | rate 가능 - 의미가 완전 확정은 아니라 **진단용 유지** |
| preemption | `vllm:num_preemptions_total` | counter | 같음 | 항상 0 |
| 그 밖 | `vllm:estimated_flops/read/write_bytes_per_gpu_total`(token 기반 추정치, 측정 아님), prefix/mm cache counter(이 workload에서 무관) | | | 제외 |

probe(1 RPS, 1 token)와 ramp 기여는 라벨로 나눌 수 없다 → 완료·token rate는 **전체 service throughput**으로만 해석.
제외(지연 계열, timeline 라벨용만): probe latency, e2e, TTFT, prefill/decode/inference time, TPOT, queue time.

## D. 정상 workload 변화 (60 s 창 중앙값 [범위])

| 단계 | CPU core | 생성 token/s | iteration/s | 완료/s | CPU÷token | SLO |
|---|---|---|---|---|---|---|
| L0 (90/91) | 1.64 / 1.63 | 1.00 | 1.00 | 1.00 | 1.64 / 1.63 | 정상 |
| L1 0.025 | 1.74 [1.63-1.89] | 1.22 [1.0-1.44] | 1.16-1.18 | 1.02 | 1.41 [1.22-1.70] | 정상 |
| L2 0.05 | 1.86 / 1.87 | 1.44 / 1.47 | 1.36 / 1.33 | 1.04 | 1.26 | 정상 / 위반 |
| L3 0.10 (90) | 2.07 [1.93-2.14] | 2.02 [1.82-2.20] | 1.76 | 1.09 | 1.01 | 정상 |
| L4 0.15 (90) | 2.30 | 2.56 | 2.11 | 1.16 | 0.92 | 위반 |

두 rep 일관(같은 단계 차이 ≤ 0.03 core, token/s ≤ 0.03). 정상 부하에서는 CPU와 throughput이 **같이** 오르고 CPU÷token은 **내려간다**. memory는 변화 없음(3.30-3.33 GiB). → CPU·throughput 모두 정상 부하 자체에 민감 → 탐지에 쓰려면 부하 맥락이 필요.

## E·F. CPU 압박에서의 변화 (ab01a-ab05a, t_slo는 주입 뒤 133-221 s)

- CPU: 주입 직전 1.60-1.66 → 압박 단계마다 상승(약 2.2-2.4 → 3.0 core 포화, limit 3). first departure 주입 +30 s(= t_slo 전 103-191 s), 5/5.
- throttling 비율: 주입 전 0.12-0.28 → +90 s 0.31-0.37 → t_slo 직전 0.40-0.79 → t_slo 뒤 1.0.
- **생성 token/s·iteration/s·완료/s·prompt token/s: t_slo 전 전 구간 1.00(probe 수요 그대로), 5/5.** 변화가 보인 것은 ab02a·ab03a에서 t_slo **뒤** +154/+172 s(0.9999 수준)뿐. open-loop 저부하에서는 처리량 = 수요라 서버가 밀리기 전(= 이미 SLO 위반)까지 throughput이 줄지 않는다.
- preemption 0. memory: 주입 뒤 약 0.05 GiB 감소(3.30 → 3.25) - 원인 불명, 크기 작음.
- CPU÷token: probe만 있으므로 = CPU 값 그대로(1.6 → 2.4 → 3.0), 정상 부하 방향(내려감)과 **반대로 올라감**.
- 주의: 주입 전 probe 가동이 60-80 s뿐이라 "기준선 [min,max]"에 probe 시작 구간이 섞임(ab03a·ab04a에서 CPU÷token departure가 늦거나 없게 나온 이유) - 기술 규칙의 한계.

## G·H. CPU만의 겹침 vs CPU + progress 관계 (관측값 기술, 임계값 아님)

- 정상 SLO 정상 단계 CPU 창 범위 1.61-2.14 core. 압박 run의 t_slo 전 창 중 CPU가 이 범위 안인 것: 각 run 2창(주입 직후 약 30 s). 그 뒤 CPU는 정상 관측 최대를 t_slo 전 88-176 s에 넘음.
  → CPU만 보면 "압박 초기"는 정상 L1-L3와 겹치고, 정상 L4(위반, 2.14-2.43)와 압박 중반(2.2-2.4)은 같은 CPU 대역.
- 2D(CPU, 생성 token/s): 정상은 CPU가 오를 때 token/s도 오름(1.6→1.0, 2.1→2.0). 압박은 CPU 1.6→3.0 동안 token/s 1.0 고정. CPU 2.0-2.1 부근에서 정상은 token/s ≈ 1.8-2.2, 압박은 1.0 → 이 데이터에서는 구조적으로 떨어져 있다.
- CPU÷token이 정상 관측 최대(1.70)를 넘은 시각: t_slo 전 118-206 s, CPU 단독보다 **15-30 s(1-2창) 이르다**, 5/5. 같은 run에서 c1 IF 조건·C 교차는 주입 뒤 약 30-50 s.
- 이것은 분류 정확도가 아니다(모델 fit 없음, 정상 rep 2개·압박 run 5개).

## I. 분류

| 후보 | 분류 | 근거(run 단위) |
|---|---|---|
| CPU (cpu_mean·slope) | promising(이미 C·c1이 쓰는 정보) | 5/5 t_slo 전 103-191 s 상승. 단, 정상 부하에서도 같이 올라 겹침 |
| CPU throttling 비율 | ambiguous | 5/5 상승하지만 정상 L3-L4(0.40-0.65)와 압박 t_slo 직전(0.40-0.79) 겹침, CPU와 같은 계열 |
| memory | unsuitable | 정상·압박 모두 변화 미미(0.05 GiB), 의미 불명 |
| 생성 token/s, iteration/s, 완료/s, prompt token/s (단독) | **unsuitable** | 5/5 t_slo 전 변화 없음(수요 고정 open-loop), 변화는 t_slo 뒤만(2/5) |
| preemption | unsuitable | 항상 0 |
| CPU ÷ 생성 token/s (관계, 진단) | **promising(조건부)** | 정상에서 부하↑ → 감소, 압박에서 → 증가, 5/5 정상 관측 범위 이탈이 CPU 단독보다 15-30 s 이르다. 조건: 아래 K·L |
| CPU vs iteration/s (관계, 진단) | ambiguous | token/s와 같은 모양이나 metric 의미가 step 수에 "가까울" 뿐 확정 아님 |

## J. 가장 유망한 1-3개

1. CPU와 생성 token/s의 관계(2D 또는 CPU÷token, 분모 작을 때 불안정 - epsilon 미정, primary 채택 보류)
2. CPU(cpu_mean·slope) - 기존 정보
3. (진단) CPU throttling 비율

## K. leakage 위험

- token/s·완료/s는 지연 계열이 아니지만, 단독으로는 서버가 밀릴 때(사실상 SLO 위반 뒤)만 변해 사후 신호다.
- CPU÷token의 분리는 **압박 방식 의존**: StressChaos 압박기 CPU가 vllm 컨테이너 사용량에 포함되기 때문에 "CPU↑ + 진행 고정"이 생긴다. 다른 pod의 이웃 경합처럼 컨테이너 밖 압박이면 vllm CPU 사용량은 오히려 막힐 수 있어 같은 모양이 보장되지 않는다(외적 타당성 한계). V2-S1(같은 압박) 안에서는 일관된 구성이다.
- 정상 범위가 rep 2개·저부하(L1-L3)뿐 - 더 높은 정상 부하는 SLO 경계와 겹쳐 관측 불가(v2wl 결론).

## L. instrumentation 한계

- probe와 ramp의 throughput 기여를 라벨로 나눌 수 없음(전체 throughput만).
- iteration count의 정확한 의미는 "출력이 있는 엔진 출력 batch 수" 수준까지만 확인.
- 실제 도착률 metric 없음, 15 s scrape·60 s 창.
- A/B 주입 전 probe 가동 60-80 s(기준선 짧음), A/B pod와 v2wl pod는 다른 pod·다른 날(같은 이미지·설정).

## M. 재사용 금지 확인

v2wl·N1·B/C/K·A/B 원자료는 설계 참고용으로만 썼다. 향후 v2 학습·보정·독립 검증에는 새로 수집한 정상 학습·정상 보정·독립 검증 자료만 쓴다. 이 분석의 숫자(1.70, 2.14 등)는 임계값으로 쓰지 않는다.

## N. v2를 계속할 가치

판단: **CASE A와 B 사이(약한 A).** t_slo 전 신호는 progress 단독이 아니라 "CPU와 progress의 관계"에서만 보이고, 그 분리는 V2-S1 압박 방식에 기대며, 이득은 CPU 단독보다 15-30 s 이른 정도다(B가 C보다 40-50 s 일렀지만 SLO 이득이 없었던 전례). 따라서 v2의 현실적 기대 가치는 **lead 단축이 아니라 정상 고부하 오탐 감소(D_safe 쪽)**다: C/C2가 정상 부하 증가를 장애로 볼 때(CPU 1.86-2.07은 frozen C 1.684 초과) 관계 feature는 "처리량이 같이 늘었으니 정상"으로 볼 여지가 있다.
계속한다면: resource + progress 관계 탐지기를 V2-S1 범위로 새 계약(정상 부하 정의 기준을 running 0.20이 아니라 throughput 변화로 바꾸는 별도 feasibility 포함)에서 사전등록. 안 한다면: 현재 frozen 결과(A/B 효과, B/C/K, N1, v2wl feasible=false, 이 탐색)로 마무리하는 것도 타당하다.

## O. 10/15 전

- 할 일: 확정 증거(A/B 복구 효과, B/C/K, N1, v2wl feasible=false)로 발표 구성. 이 탐색은 최대 "후속 분석에서 resource 사용량뿐 아니라 service progress 지표를 이용한 상태 구분 가능성을 검토 중" 한 줄.
- 하지 말 것: v2 성공·개선 주장, 이 숫자를 임계값·정확도로 제시, 새 live 실험.

## P. 후속 구현 전 필요한 결정

1. v2 지속 여부(약한 A로 진행 vs 현재 결과로 마무리).
2. 진행 시: 정상 workload 정의 기준을 "running 0.20" 대신 "SLO 정상 + throughput 변화"로 하는 새 feasibility 계약 승인(기존 0.20 결과는 그대로 보존).
3. 관계 feature 형태(2D 입력 vs 비율) 사전 고정, 분모 안정화 규칙을 결과 보기 전에 정할지.
4. 외적 타당성: 압박기 CPU가 컨테이너 안에 포함되는 V2-S1 한계를 받아들일지.
