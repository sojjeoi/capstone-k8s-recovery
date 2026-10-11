# Axis 2 — LLM-serving multivariate detector 추가 가치: 설계·실현 가능성 (2026-10-11)

> DESIGN 단계 문서. live 실험·workload·클러스터 변경 없음. c3-A(PROMISING, frozen)와 별개 연구축.
> 질문: CPU 하나로 구분하기 어려운 LLM serving 상태에서, LLM-serving 지표를 포함한 다변량 detector가 단일 CPU 임계값보다 추가 운영 가치를 주는가?
> 판정: **NO_GO_LLM_MULTIVARIATE** (현재 환경·SLO 그대로일 때). 재검토 조건은 9절.

## 0. 근거 자료 (모두 기존 공식 evidence 구간, DEVELOPMENT 용도)

- 이번 감사: `experiments/llm_multivariate_added_value/metric_audit.py` → `experiments/results/llm_multivariate_added_value_feasibility_20261011_r2/metric-audit.json`
  (`..._20261011/`은 request_success 다중 시계열 합산 버그가 있던 1차 실행 - 보존, 사용 안 함)
  - normal: c3 정상 세션 10개(normal_valid, 약 11.7 h, 2800+ 점) · pressure: ab01a–ab05a 주입~+363 s · workload: v2wl90/91 L0–L4
  - Prometheus 30일 보존, step 15 s, `job="vllm-active"` 한 시계열, 카운터 리셋 0
- 이전 실현 가능성 작업(이번에 다시 읽음): `docs/design/phase8-aiops-v2-design-draft-20261009.md:63-74`, `phase8-aiops-v2-progress-metric-feasibility-20261009.md`,
  `phase8-aiops-v2-request-shape-axis-assessment-20261009.md`, `phase8-aiops-next-research-direction-20261009.md:74-76`,
  `experiments/results/capstone_v2_workload_pilot/V2WL-FEASIBILITY-RESULT-20261009.md`, `capstone_iof_stage0/IOF-STAGE0-RESULT-20261010.md`,
  `capstone_nnf_stage0/NNF-STAGE0-SCREENING-RESULT-20261010.md`
- 서빙 구성: Qwen2.5-0.5B-Instruct, vLLM 0.28.1 CPU, `--max-model-len=2048 --enforce-eager`, max_num_seqs 미설정, CPU request 2 / limit 3 (`gitops/apps/vllm-serving/rollout.yaml`)

## A·B. 실제 노출 지표와 변동 (사실)

Prometheus에 vLLM 지표 약 100개가 실제로 있다. detector 입력 후보(지연 계열 제외)의 변동:

| 지표 (exact) | 종류 | normal (1 RPS, max_tokens=1) | pressure (CPU stress) | workload ladder (max_tokens=10, +0.025–0.15 RPS) |
|---|---|---|---|---|
| `container_cpu_usage_seconds_total` rate (vllm) | counter→rate | 1.65 코어(중앙), 1.0–1.72 | 2.98(한도 3 근처) | 1.62–2.36 |
| `container_memory_working_set_bytes` | gauge | 3.49 GB, 거의 일정 | 일정 | 일정 |
| `vllm:num_requests_running` | gauge | **항상 0** (unique 1) | **항상 0** | 0/1, 비영 14%(L4 38%) |
| `vllm:num_requests_waiting` | gauge | **항상 0** | **항상 0** | **항상 0** |
| `vllm:kv_cache_usage_perc` | gauge | **항상 0** | **항상 0** | 0 / 0.0083 (running과 같은 꼴) |
| `vllm:num_preemptions_total` rate | counter | 0 | 0 | 0 |
| `vllm:generation_tokens_total` rate | counter | 1.00 ± 0.02 (= probe) | 1.00 | 1.22–2.69 (부하와 함께) |
| `vllm:prompt_tokens_total` rate | counter | 1.00 | 1.00 | 1.03–1.18 |
| `vllm:request_success_total` rate (sum) | counter | 1.00 | 1.00 | 1.03–1.16 |
| `vllm:iteration_tokens_total_count` rate | counter | 1.00 | 1.00 | 1.25–2.22 |

- running/kv는 15 s scrape 시점의 0/1 샘플이라 시간 해상도가 낮다(요청 1개가 약 0.25 s).
- running이 1 이상이 되는 데는 추가 부하가 필요하고, 그 부하는 0.05–0.15 RPS에서 SLO를 깼다(v2wl91 L2, v2wl90 L4).

## C. leakage 판정

- 입력 제외(outcome 전용): `e2e_request_latency`, `time_to_first_token`, `inter_token_latency`, `request_queue_time`, `request_prefill/decode/inference_time`, `request_time_per_output_token`, probe P95, SLO 플래그.
- 입력 가능(상태·처리량): running, waiting, kv_cache_usage, 토큰·iteration·완료 rate, preemption, CPU, memory.
- **단서**: 처리량 rate는 open-loop probe에서는 **들어온 부하(offered load)**를 거의 그대로 반영한다. 서비스 저하로 처리량이 줄어드는 것은 SLO 위반 뒤에만 관찰됐다(이전 작업: ab 5개 중 2개, 위반 후). 즉 처리량은 "저하의 앞선 징후"보다 "부하 크기"에 가깝다.

## D. CPU와의 관계 (같은 CPU 구간 비교, 사실)

| CPU 구간(코어) | normal | pressure | workload (SLO 정상 단계) | workload (SLO 위반 단계) |
|---|---|---|---|---|
| 1.25–1.75 | gen 1.00 (2800점) | 1.00 (10점, 주입 직후) | 1.00 (80) | 1.24 (2) |
| 1.75–2.0 | – | 1.00 (5) | 1.44 (38) | 1.45 (19) |
| 2.0–2.25 | – | 1.00 (3) | 2.04 (8) | 2.33 (9) |
| 2.25–2.5 | – | 1.00 (25) | – | 2.48 (12) |
| 2.5+ | – | 1.00 (82) | – | – |

- "CPU는 비슷한데 LLM 지표가 다른" 구간은 **pressure 대 추가 부하** 조합에서만 보인다(1.75–2.5 코어에서 토큰 rate 1.0 대 1.4–2.5).
- 그러나 (1) pressure는 주입 15–30 s 안에 CPU가 한도(3)로 올라가 그 구간 체류 점이 33개뿐이고, (2) 같은 CPU의 추가 부하는 이미 SLO 위반 단계가 많으며,
  (3) 이전 오프라인 재생에서 CPU 단독 규칙과 CPU 대비 토큰 잔차 규칙이 5/5 같은 시각에 발화했다(`exploratory_v2_progress_metric_20261009/stage0-offline-separability-estimate.txt`),
  (4) 분리는 stress 도구가 vLLM 컨테이너 안에서 돌아 그 CPU가 vLLM CPU로 잡히는 구조 때문이라고 이전 문서가 스스로 적었다(`progress-metric-feasibility:84`).
- noisy-neighbor(Pod 밖 경쟁)는 vLLM CPU를 오히려 올렸다(1.63→2.74) → CPU 단독으로 분리됨(`NNF-STAGE0-SCREENING-RESULT:24-34`).
- 1.75–2.0 코어에서 SLO 정상 단계(38점)와 SLO 위반 단계(19점)의 토큰 rate 중앙값이 같다(약 1.44). 처리량은 위반 여부가 아니라 들어온 부하를 따라간다(C절 단서의 직접 증거).
- 측정 주의: pressure CPU 중앙값 2.98은 5개 중 4개 기준이고 ab02a는 2.62. rate[1m]이라 각 구간(압박·ladder 단계) 첫 1분은 이전 상태와 섞인다.

## E. 시나리오 후보 1 — "정상 고부하 대 CPU 경쟁" (CPU 매칭)

- 메커니즘: 같은 CPU 수준이라도 (N) 정상 추가 요청 때문이면 토큰·iteration rate가 높고, (D) CPU 경쟁 때문이면 처리량은 그대로(1.0)인데 CPU만 높다.
- 예상 변화 지표: gen_tokens/s, engine_steps/s, (running 비율). CPU 단독은 N과 D를 같은 값으로 본다.
- workload: probe 1 RPS + 부하 ladder(N) / probe + 약한 in-container stress(D), CPU 구간을 사전 고정해 매칭.
- SLO는 outcome으로만. detector 입력: CPU, memory, gen_tokens/s, engine_steps/s.
- baseline: CPU 임계값 C. confounder: stress가 컨테이너 안에서 돌아 CPU 계정이 섞임, 부하 요청이 probe와 겹쳐 지연을 직접 올림.
- 안전 위험: 부하 ladder가 SLO를 깸. 재현성: SLO 경계가 rep마다 다름(0.05 대 0.15 RPS).
- **실패 조건(이미 관찰됨)**: N 쪽이 0.05–0.15 RPS에서 SLO를 깨 "정상"이 아니게 된다. D 쪽 약한 stress(1 worker, 2.2–2.4 코어)는 SLO를 깨지 않았다
  (`next-research-direction:75`). 즉 운영상 나쁜 쪽은 오히려 N(부하)이고, "CPU 높고 처리량 그대로"(D)를 이상으로 잡는 detector는 SLO와 반대 방향의 신호가 된다.
  → 정답(어느 쪽이 비정상인가)을 결과 보기 전에 SLO와 모순 없이 정의할 수 없다.

## F. 시나리오 후보 2 — "요청 모양(생성 길이) 변화 대 동일 CPU"

- 메커니즘: 같은 CPU에서 짧은 요청 다수 대 긴 생성 소수 → 요청당 토큰·running 점유가 다름. 예상 변화 지표: gen_tokens/s, running 비율.
- **실패 조건(이미 평가됨)**: SLO 위험은 요청 모양이 아니라 엔진 점유(겹침) 비율로 정해지고, probe가 부하 요청과 겹치면 12%가 0.648 s를 넘었다.
  max_tokens·prompt 길이·동시성 모두 같은 구조적 한계(`request-shape-axis-assessment:9-47`). SLO 정상이면서 running ≥ 0.20인 수준이 없음(v2wl feasible=false).

## G. "CPU가 비슷하다" 정의 후보 (사전 고정용, 장단점)

1. 고정 구간(band): 1분 cpu_mean이 [a, b) 안 - 단순·사전 고정 쉬움 / 구간마다 표본 수가 크게 다름(위 표: pressure는 2.5+에 몰림).
2. 차이 허용(tolerance): 두 조건의 구간별 cpu_mean 중앙값 차 ≤ δ(예: 0.1 코어) - 직관적 / δ 선택이 자의적이 될 수 있음.
3. 분포 겹침: 두 조건 CPU 분포의 겹침 계수 ≥ τ, 그리고 CPU 단독 분류 AUC ≤ 0.6 - "CPU로 못 가른다"를 직접 정량화 / 표본이 충분해야 하고 계산이 복잡.
- 이 환경에서는 어떤 정의든 매칭 구간에 pressure 체류 표본이 거의 없어(33점) 성립하기 어렵다.

## H. baseline 설계

- C: `cpu_mean > 1.6842544293089357`, 3연속(N1·BCK 공식값). 재사용 장점: 사전 고정·비교 가능성 / 단점: 1 RPS max_tokens=1 정상에서 보정됐으므로 부하가 있는 workload에서는 정상 고부하만으로도 발화(정상 L1–L3 CPU 1.73–1.97 > 1.684).
- 새 C 재보정: 새 workload의 정상 보정 세션으로 같은 규칙(정상 신호 0인 가장 민감한 값) - 공정하나 결과 보기 전에 보정 세션을 고정해야 함.
- 어느 쪽이든 공식 실험 전에 하나를 사전등록해야 한다. 이번 판정(NO_GO)에서는 선택하지 않는다. K 규칙은 포함 이유가 없다.
- recovery 계층: detector 비교는 record_only/shadow로 충분하다. 실제 전환 효과는 A/B가 이미 보였으므로 매 세션 recovery를 붙일 이유가 없다.

## I. 다변량 feature set 후보 (GO였다면)

- 최소: cpu_mean, memory_mean, gen_tokens_per_s(mean), engine_steps_per_s(mean). running·waiting·kv·preemption은 정상 분산 0이라 제외.
- 문제: normal-only 학습에서 토큰·iteration rate는 상수 1.0이라, 정상 학습 IF는 부하가 조금만 늘어도(정상 고부하) 이상으로 볼 것이다 - 이것은 "LLM 상태 이해"가 아니라 "학습 때와 다른 부하"를 잡는 것.

## J. confounder·위험

- stress/부하가 같은 CPU 엔진·같은 컨테이너를 공유(계정 혼합, 겹침 지연).
- SLO 여유가 매우 좁음(기준선 P95가 0.648 s 근처, 0.66–0.76 s 산발).
- 15 s scrape 대 0.25 s 요청 → running/kv 샘플링 해상도 부족.
- 개발 자료로 feature를 골랐으므로 공식 검증은 새 독립 세션이어야 함.

## K. 실현 가능성 Q1–Q6

| Q | 답 | 근거 |
|---|---|---|
| Q1 CPU 외에 실제로 변하는 LLM 지표가 있는가 | **YES(제한적)** | 토큰·iteration·완료 rate, running 비율은 **추가 부하가 있을 때만** 변함. 정상 1 RPS·CPU 압박에서는 모두 상수. waiting·kv·preemption은 사실상 0 |
| Q2 leakage 없이 입력으로 쓸 수 있는가 | **YES(단서)** | 상태·처리량 지표는 지연이 아님. 단 처리량은 offered load를 반영(저하의 앞선 징후 아님) |
| Q3 CPU가 비슷하고 LLM 상태가 다른 두 조건을 재현 가능하게 만들 수 있는가 | **NO (SLO 정상·교란 없는 조건에서는)** | 분리 자체는 있다: CPU 1.75–2.5 코어에서 토큰 rate가 pressure 1.0 대 추가 부하 1.4–2.5로 갈린다. 그러나 그 분리는 (a) stress가 vLLM 컨테이너 안에서 도는 교란 조건, (b) 추가 부하 쪽 상당수가 이미 SLO 위반 단계, (c) pressure 체류 33점의 작은 표본에서만 나타난다. 이전 오프라인 재생에서는 CPU 규칙과 같은 시각에 발화했고(파일 재검증은 이번에 안 함), noisy-neighbor는 CPU 단독으로 분리됐다 |
| Q4 그 조건을 만들려면 SLO를 먼저 깨야 하는가 | **YES** | LLM 지표를 움직일 부하가 0.05–0.15 RPS에서 SLO 위반, waiting은 위반 전까지 한 번도 1이 안 됨 |
| Q5 정상/저하 차이를 cherry-pick 없이 사전 정의할 수 있는가 | **NO** | 후보 1에서 SLO상 나쁜 쪽(부하)과 detector가 이상으로 볼 쪽(경쟁)이 반대 - 정답 정의가 SLO와 모순 |
| Q6 같은 workload·환경에서 공정 비교가 가능한가 | **YES(형식상)** | 같은 evidence 재생으로 가능하나 Q3·Q5가 막혀 비교할 대상 상태가 없음 |

## L. GO / NO-GO 이유

- 핵심 전제("CPU는 같은데 LLM serving 상태가 다른, SLO 정상 범위의 두 조건")를 이 환경에서 만들 수 없다는 증거가 반복적으로 나왔다: v2 ladder, 요청 모양, I/O, noisy-neighbor, 이번 감사.
- 원인은 환경 구조: CPU 하나에서 도는 엔진 1개를 probe와 모든 부하가 공유하고, SLO 지연 임계값(2 × 0.324 s)이 동시 decode 1개 정도의 크기다.
- AI가 이길 조건을 찾으려고 workload를 조정하지 않는다 → **NO_GO_LLM_MULTIVARIATE**.

## 9. 재검토 조건 (사용자 결정 사항, 이번 범위 밖)

다음 중 하나가 바뀌면 다시 평가할 가치가 있다 - 모두 환경·계약 변경이라 별도 승인과 새 사전등록이 필요하다.
1. 서빙 용량: 더 큰 CPU 한도/전용 노드, 또는 GPU - 부하가 SLO 정상 범위에서 running·waiting을 실제로 움직일 수 있어야 함.
2. SLO 계약: 생성 길이에 맞는 지연 기준(예: 토큰당 시간) - 지금의 단일 짧은 요청 기준은 여유가 거의 없음.
3. `--max-num-seqs`·배치 설정을 명시해 대기열이 생기는 지점을 SLO 위반보다 앞에 두는 구성(사전 측정 필요).
4. 측정 해상도: scrape 간격 단축 또는 rate 기반 지표만 사용.

## M–N. 사전등록 초안 / 구현 준비

- NO_GO이므로 사전등록 초안은 만들지 않았다(만들면 실행 가능한 계획처럼 보일 위험).
- 준비된 것: `metric_audit.py`(Prometheus GET만, 기존 구간만), `test_metric_audit.py`(6개), 감사 결과 JSON(r2).
