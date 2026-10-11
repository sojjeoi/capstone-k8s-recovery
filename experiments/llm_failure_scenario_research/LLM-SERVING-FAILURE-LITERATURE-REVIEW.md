# LLM serving 장애·성능저하 문헌 조사 (2026-10-11, RESEARCH 단계)

> 목적: 실제 LLM serving에서 반복 보고된 성능저하 메커니즘을 먼저 정리하고, 그다음 현재 Kubernetes + vLLM CPU 환경에 대응시킨다.
> 시나리오는 detector 결과와 무관하게 이 문서와 `LITERATURE-GROUNDED-SCENARIOS-v1.md`에서 고정한다. live 실험·클러스터 변경 없음.
> 표기: **[논문]** = 출처가 실제로 주장하는 내용(요약), **[판단]** = 이 프로젝트 환경에 대한 우리 판단(검증 안 됨).
> 거의 모든 출처는 GPU serving 기준이다. CPU vLLM에 그대로 성립한다고 가정하지 않는다.

## 1. 출처 표

### A. Prefill/decode 간섭, head-of-line blocking

| 출처 | [논문] 문제·메커니즘 | 변하는 지표 | CPU만으로 설명? / 지연 위치 | HW 의존 |
|---|---|---|---|---|
| Yu et al., *Orca*, OSDI'22. https://www.usenix.org/system/files/osdi22-yu.pdf | 요청 단위 배치는 먼저 끝난 요청과 새 요청을 묶어 기다리게 한다 → iteration 단위 스케줄링 제안 | 대기 지연, 처리량 | 스케줄링 효과 / 지연은 결과 | GPU 실험. vLLM이 이미 iteration 단위(continuous batching)라 배경 지식 |
| Agrawal et al., *Taming Throughput-Latency Tradeoff in LLM Inference with Sarathi-Serve*, OSDI'24 (arXiv 2403.02310). https://arxiv.org/abs/2403.02310 | prefill은 계산을 포화, decode는 지연이 짧고 계산을 덜 씀 → 한 배치에 섞이면 처리량·지연을 함께 얻기 어렵다. chunked prefill + stall-free 스케줄링 제안 | TTFT, TBT/TPOT 꼬리 | 둘 다 "바쁨"으로 보임 / 지연은 배치 구성의 결과 | GPU(A100) 측정 |
| Zhong et al., *DistServe*, OSDI'24 (arXiv 2401.09670). https://arxiv.org/abs/2401.09670 | 두 단계를 같은 자원에 두면 강한 간섭 → 분리 배치. goodput(TTFT·TPOT SLO 동시 충족) 최대 7.4배 | TTFT, TPOT, goodput | 위와 같음 | 다중 GPU 분리(해법은 범위 밖) |
| Patel et al., *Splitwise*, ISCA'24 (arXiv 2311.18677). https://arxiv.org/abs/2311.18677 | prompt 단계는 계산 집약, token 단계는 메모리 집약 | 처리량, 비용 | 같은 사용률에서 단계 구성이 다르면 자원 프로필이 다름 | GPU |
| Hu et al., *Inference without Interference* (TetriInfer), arXiv 2401.11181. https://arxiv.org/abs/2401.11181 | prefill·decode 특성 차이를 무시하면 큰 간섭. 고정 크기 prompt chunk·분리·길이 예측 | TTFT, 완료 시간 | 위와 같음 | GPU |

### B. 버스트·동시 요청

| 출처 | [논문] | 변하는 지표 | HW 의존 |
|---|---|---|---|
| Wang et al., *BurstGPT*, KDD'25 (arXiv 2401.17644). https://arxiv.org/abs/2401.17644 | Azure OpenAI 실제 trace. 버스트(동시성 급변)와 함께 효율·신뢰성이 떨어진다고 보고. KV cache는 최적화 대상으로 언급될 뿐 원인으로 단정하지 않음 | 지연, 실패, 동시성 | GPU |
| Sun et al., *Llumnix*, OSDI'24 (arXiv 2406.03243). https://arxiv.org/abs/2406.03243 | 기존 시스템은 심한 대기 지연·꼬리 지연·SLO 위반을 겪는다 → 인스턴스 간 요청 이전 | 대기, 꼬리 지연, 메모리 단편화 | 다중 인스턴스(해법 범위 밖) |
| Li et al., *AlpaServe*, OSDI'23 (arXiv 2302.11665). https://arxiv.org/abs/2302.11665 | 버스트 다중 모델 부하에서 통계적 다중화 | SLO 달성률 | GPU 배경 |

### C. KV cache 압박·선점(preemption)

| 출처 | [논문] | 변하는 지표 | HW 의존 |
|---|---|---|---|
| Kwon et al., *Efficient Memory Management for LLM Serving with PagedAttention* (vLLM), SOSP'23 (arXiv 2309.06180, §4.5). https://arxiv.org/abs/2309.06180 | KV 블록이 모자라면 가장 늦게 온 요청을 통째로 선점, swap 또는 recompute로 복구 | KV 사용률, 선점 수, running/waiting | GPU 메모리 기준. CPU는 고정 DRAM 예산 |
| vLLM docs, *Optimization and Tuning*. https://docs.vllm.ai/en/latest/configuration/optimization.html | KV 공간 부족 시 RECOMPUTE 선점(V1 기본), e2e 지연 악화 경고. 완화: 메모리 늘리기, `max_num_seqs`/`max_num_batched_tokens` 줄이기. chunked prefill은 V1 기본, decode 먼저 스케줄 | 선점 경고, 지연 | 버전: docs latest(0.28 고정 문서는 확인 못 함) |
| Jin et al., *S³*, NeurIPS'23 (arXiv 2306.06000). https://arxiv.org/abs/2306.06000 | 최악 출력 길이만큼 메모리를 예약하면 배치가 작아짐, 길이 예측이 틀리면 선점 | 배치 크기, 선점 | GPU |

### D. 자원 경쟁(공유 호스트)

| 출처 | [논문] | CPU 쪽? |
|---|---|---|
| Cheng et al., *Measurement-Driven Diagnosis and Mitigation of Host-CPU Co-location Interference in Single-GPU LLM Serving* (CoTail), arXiv 2609.05425 (preprint, 날짜 표기 불일치 - 확인 필요). https://arxiv.org/abs/2609.05425 | 호스트 CPU 공동 입주자가 serving의 CPU 쪽 단계를 늦춰 꼬리 지연 증폭(TTFT +429.5%) | 예(GPU serving의 CPU 단계) |
| Chen, Delimitrou, Martínez, *PARTIES*, ASPLOS'19. https://doi.org/10.1145/3297858.3304005 | 공동 배치된 지연 민감 서비스가 코어·LLC·메모리 대역폭 등으로 간섭 | 예(LLM 아님) |
| Gupta et al., *The Architectural Implications of Facebook's DNN-based Personalized Recommendation*, HPCA'20 (arXiv 1906.03109). https://arxiv.org/abs/1906.03109 | CPU 추론 공동 배치 시 지연 1.3–2.6배, LLC miss 증가와 함께 | 예(CPU DNN 추론, LLM 아님) |

### E. 요청 모양 이질성

| 출처 | [논문] |
|---|---|
| Sheng et al., *Fairness in Serving Large Language Models* (VTC), OSDI'24 (arXiv 2401.00588). https://arxiv.org/abs/2401.00588 | 부하 단위는 요청 수가 아니라 입력·출력 토큰 - 토큰 단위 공정 스케줄러 |
| Liu et al., *Andes*, arXiv 2404.16283. https://arxiv.org/abs/2404.16283 | 사용자 체감 품질 = TTFT + 토큰 속도 → outcome으로는 평균 지연보다 두 값 쌍이 적절 |

### F. 공식 문서·운영 자료 (탐지 지표 관점)

| 출처 | [자료] 주장 |
|---|---|
| vLLM metrics. https://docs.vllm.ai/en/latest/usage/metrics.html | running·waiting·kv_cache_usage_perc는 gauge, 선점·토큰은 counter, iteration_tokens_total은 step당 토큰 histogram, queue_time·TTFT·ITL·e2e는 지연 histogram |
| vLLM CPU backend. https://docs.vllm.ai/en/latest/getting_started/installation/cpu.html | 온라인 serving 기본값 `max_num_batched_tokens` 2048, `max_num_seqs` 128. KV 크기는 `--kv-cache-memory-bytes`(구 `VLLM_CPU_KVCACHE_SPACE`). CPU에서 chunked prefill 동작 여부는 최신 문서에 명시 없음(미확인) |
| Google GKE, *Best practices for autoscaling LLM inference on GPUs*. https://docs.cloud.google.com/kubernetes-engine/docs/best-practices/machine-learning/inference/autoscaling | GPU 사용률은 "활성 중 실제 처리량"을 재지 못한다. CPU·메모리만으로 판단하지 말 것. queue 크기는 지연과 직접 상관, batch 크기는 처리량·지연과 상관 |
| NVIDIA Triton + TensorRT-LLM autoscaling. https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/tutorials/Deployment/Kubernetes/TensorRT-LLM_Autoscaling_and_Load_Balancing/README.html | GPU 사용률은 잡음이 많아 HPA 신호로 부적합 → 대기시간/계산시간 비율 사용 |
| vLLM production-stack KEDA 예시. https://docs.vllm.ai/projects/production-stack/en/latest/use_cases/autoscaling-keda.html | `vllm:num_requests_waiting` 기준 확장 |
| Databricks, *LLM Inference Performance Engineering*. https://www.databricks.com/blog/llm-inference-performance-engineering-best-practices | prefill은 계산, decode는 메모리 대역폭 제약 - 같은 사용률도 단계·배치에 따라 지연이 크게 다름 |
| GitHub availability report, Dec 2025. https://github.blog/news-insights/company-news/github-availability-report-december-2025/ | 모델 의존 서비스 지연 → timeout·backpressure·대기열 증가, 후속 조치로 queue health 계측 추가 |
| OpenAI status write-up, 2025-02-19. https://status.openai.com/incidents/01JMYB3SYB2VWB9C5C2SJ559AV/write-up | 잘못된 내부 실험이 트래픽 급증 → 추론 계산 포화(버스트 과부하). 탐지 지표는 명시 없음 |

- 운영 자료의 공통점: 사용률보다 queue·batch·동시성 신호를 권한다. **단, 모두 GPU·오토스케일링 맥락이다.** CPU vLLM·이상 탐지에 대해 검증한 자료는 찾지 못했다.

## 2. 장애 분류(taxonomy)와 메커니즘

| 범주 | 메커니즘 | 원인 쪽 운영 상태(입력 후보) | 결과(outcome 전용) |
|---|---|---|---|
| A 간섭 | 계산 무거운 긴 prefill이 같은 배치·같은 엔진의 짧은 요청을 늦춤 | prompt 토큰 rate, step당 토큰(iteration_tokens), running | TTFT·ITL 꼬리, SLO |
| B 버스트 | 짧은 시간에 동시 요청이 몰려 배치가 커지고 step이 느려짐, 한도 넘으면 대기 | running, waiting, 토큰·iteration rate | TTFT, 꼬리 지연, 실패 |
| C KV 압박 | KV 블록 고갈 → 선점·recompute(이미 한 계산을 다시 함) | kv_cache_usage, 선점 rate, waiting(capacity 사유) | e2e 지연 |
| D 경쟁 | 같은 호스트의 다른 작업이 코어·캐시·대역폭을 뺏음 | CPU, 노드 PSI, (대역폭은 지표 없음) | 지연 |
| E 모양 | 입력·출력 길이 구성이 단계 비율과 KV 수요를 바꿈 | prompt/generation 토큰 비율 | 지연 |

## 3. 현재 환경 대응 (사실 + 판단)

현재 환경(읽기 전용 확인, 2026-10-11):
- Qwen2.5-0.5B-Instruct, vLLM 0.28.1 CPU, CPU limit 3 / request 2, `--max-model-len=2048`, max_num_seqs 미설정(문서상 CPU 온라인 기본 128).
- `vllm:cache_config_info`: KV 블록 120 × 128 토큰 = **15,360 토큰**, `kv_cache_max_concurrency` **7.5**, prefix caching **켜짐**.
- 정상(1 RPS, max_tokens=1) CPU 약 1.65 코어로 포화가 아니며 부하가 늘면 2.3까지 오른다(`llm_multivariate_added_value_feasibility_20261011_r2`). 문헌·운영 자료의 논지(GKE: GPU 사용률은 활성 시간 비율이라 실제 처리량을 재지 못한다)가 CPU 사용률이 부하에 반응하는 이 환경에 그대로 옮겨진다고 볼 근거는 없다[판단].
- **이전 판정과의 관계(중요)**: `docs/design/phase8-aiops-v2-request-shape-axis-assessment-20261009.md` §3이 이미 prompt 길이(prefill) 축을 "부적합(더 나쁠 가능성)", 동시 요청 축을 "부적합"으로 **문서상 기각**했다. 이유는 같은 구조 - probe가 단일 CPU 엔진을 모든 부하와 공유하고 SLO 지연 임계(2 × 0.324 s)가 겹치는 요청 1개 크기와 비슷하다. 아래 A·B 판정은 이 기각을 전제로 한다.
- Axis 2 감사: 정상·CPU 압박에서 running·waiting·KV·선점 모두 0, 추가 부하(max_tokens=10, 0.05–0.15 RPS)에서 SLO가 먼저 깨짐.

| 범주 | 판정 | 이유 |
|---|---|---|
| A 긴 prefill 간섭 | **NEEDS_ENVIRONMENT_CHANGE** | 실측은 없지만 prompt 길이 축은 이미 문서상 기각(위). 또 문헌 메커니즘은 긴 prefill이 *진행 중인 decode*(토큰 간 시간)를 막는 것인데, probe는 max_tokens=1이라 그 자체가 짧은 prefill이다 → 이 환경에서 재는 것은 prefill 대 prefill 대기이고, 1536 토큰 이하 prompt는 `max_num_batched_tokens` 2048 안에 들어가 chunk로 나뉘지도 않는다[판단]. 문헌 메커니즘을 제대로 재현하려면 streaming decode probe·토큰 단위 SLO 같은 계약 변경이 필요 |
| B 버스트 | **NEEDS_ENVIRONMENT_CHANGE** | 동시 요청 축도 문서상 기각. max_tokens 10 기준 k=4·8을 30 s마다 보내면 약 0.13–0.27 RPS로 이미 SLO가 깨진 0.05–0.15 RPS 영역. max_num_seqs 128·짧은 요청 1블록이라 waiting은 거의 안 늘 것, 15 s scrape보다 짧은 버스트는 gauge에서 놓침[판단] |
| C KV 압박 | **NEEDS_ENVIRONMENT_CHANGE** | 15,360 토큰을 채우려면 2048 토큰 요청 약 8개가 동시에 필요 → 그 전에 CPU prefill 계산으로 SLO가 깨질 것[판단]. KV 예산 축소(`--kv-cache-memory-bytes`; `num_gpu_blocks_override`는 CPU 백엔드 동작 미확인) 같은 설정 변경이 필요 |
| D 경쟁 | **NOT_FEASIBLE**(이미 시험) | noisy-neighbor는 vLLM CPU를 올려 CPU 단독 분리, 디스크 I/O는 가장 약한 단계부터 SLO 위반. 메모리 대역폭 경쟁은 vLLM CPU에서 검증한 문헌·지표 없음 |
| E 모양 | A·B와 같음 | output 길이·prompt 길이·동시성 세 축 모두 이전 평가에서 같은 이유로 부적합(`phase8-aiops-v2-request-shape-axis-assessment-20261009.md` §3) |
| GPU 전용 | 분리 prefill/decode(DistServe·Splitwise 해법), 다중 인스턴스 이전(Llumnix) | 다중 장치·인스턴스 필요 |

## 4. 입력 후보 지표와 leakage

- 입력 가능(상태·처리량): CPU, memory, `num_requests_running`, `num_requests_waiting`(+`_by_reason`), `kv_cache_usage_perc`, `num_preemptions_total` rate, `prompt_tokens_total`·`generation_tokens_total` rate, `iteration_tokens_total` sum/count(step당 토큰).
- 입력 금지(outcome 전용): e2e·TTFT·ITL·TPOT·queue_time·prefill/decode/inference time, probe P95, SLO 플래그, 요청 실패, recovery 결과.
- `num_requests_waiting_by_reason`(reason=capacity/deferred)은 이 서버 Prometheus에 존재함을 2026-10-11 읽기 전용 조회로 확인(Axis 2 감사 대상에는 없었음).
- `running`은 outcome은 아니지만 Little의 법칙상 도착률 × 체류시간이라 지연과 일부 결합되고 probe 요청도 포함한다 - 해석 시 주의.
- 주의: 토큰·요청 rate는 들어온 부하를 반영한다(Axis 2 감사: 같은 CPU에서 SLO 정상/위반 단계의 토큰 rate 동일). "부하가 크다"와 "서비스가 나빠진다"를 구분해 주지 않는다.

## 5. 한계

- 출처 대부분이 GPU·대형 모델. 0.5B CPU에서 크기·임계점이 다를 수 있다.
- vLLM 문서는 latest(개발판) 기준. 0.28.1과 지표·기본값이 다를 수 있어 실제 `/metrics`·`cache_config_info`로 확인했다.
- 일부 서지 정보 미검증: Sarathi-Serve의 OSDI'24 게재(arXiv 페이지에는 preprint), Gupta et al. HPCA'20, CoTail 날짜, PARTIES 자원 목록 세부.
- 운영 사고 보고에는 탐지 지표가 거의 적혀 있지 않다 - "어떤 지표가 먼저 반응했나"의 근거로는 쓸 수 없다.
