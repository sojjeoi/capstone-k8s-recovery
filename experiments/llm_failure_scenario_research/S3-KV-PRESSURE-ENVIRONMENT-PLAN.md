# S3 KV cache pressure — 환경 변경·pilot 계획 (2026-10-11, PLAN 단계)

> 범위: 계획만. vLLM 재시작·설정 변경·부하·pilot·detector 비교는 하지 않았다. A/B·B/C/K·N1·c3-A·Axis 2·문헌 조사 결과는 frozen 그대로이며, 이 pilot은 그것들과 다른 새 환경 조건이다.
> 질문: 현재 CPU-vLLM을 최소한으로 바꿨을 때, SLO가 깨지기 전에 KV cache pressure라는 LLM 고유 운영 상태를 재현 가능하게 관찰할 수 있는가? (AI 우월성 질문 아님)
> **판정: S3_PILOT_DESIGN_NOT_FEASIBLE (예상 판정 - 생성 128 토큰 요청으로는 측정하지 않음)** - 설정 변경 자체는 지원되지만(3절), 현재 probe·SLO v3에서는 KV 압박이 생기기 전에 SLO가 먼저 깨질 것으로 예상된다(7절). 근거와 대안은 7·13절.

## 1. 문헌 근거

- vLLM PagedAttention(Kwon et al., SOSP'23 §4.5): KV 블록이 모자라면 가장 늦게 온 요청을 통째로 선점, swap/recompute로 복구.
- vLLM *Optimization and Tuning*(docs latest): KV 공간 부족 → RECOMPUTE 선점(V1 기본), e2e 지연 악화. 완화: 메모리↑, `max_num_seqs`·`max_num_batched_tokens`↓.
- 상세·한계: `LLM-SERVING-FAILURE-LITERATURE-REVIEW.md` C절.

## 2. 현재 환경 (읽기 전용 확인, 2026-10-11 출처 명시)

| 항목 | 값 | 출처 |
|---|---|---|
| vLLM | 0.28.1rc1.dev133+g8fd9eb85d, V1 엔진, CPU 백엔드 | 시작 로그 |
| 인자 | `--model=Qwen/Qwen2.5-0.5B-Instruct --gpu-memory-utilization=0.3 --enforce-eager --max-model-len=2048` | Pod spec, 로그 `non-default args` |
| `kv_cache_memory_bytes` | 미설정(None) | 로그 non-default args에 없음 |
| KV 할당 방식 | `requested = 0.3 × 노드 메모리(6.0 GiB) = 1.8 GiB`, KV = requested − 프로세스 RSS(1.62 GiB) = **0.18 GiB** | `cpu_worker.py` 74–77·229–252행, 로그 "Auto set (0.18/6.0) GiB … 1.62 GiB … non-kv" |
| KV 크기 | **15,360 토큰**, 최대 동시성(2048 토큰 기준) 7.50 | 로그 `kv_cache_utils.py:2020`, `vllm:cache_config_info` |
| block | 128 토큰 × 120 블록 | `cache_config_info`(block_size 128, num_gpu_blocks 120), `platforms/cpu.py` 175–176(CPU 기본 128) |
| 토큰당 KV 바이트 | 2(K,V) × 24층 × 2 KV head × 64 dim × 2 B(bf16) = **12,288 B** → 블록당 1,572,864 B | 모델 config.json(층 24, KV head 2, hidden 896/14 head = 64, bfloat16). 검산: 15,360 × 12,288 = 188.7 MB = 0.176 GiB ≈ 로그 0.18 GiB |
| prefix caching / chunked prefill | 둘 다 True | 엔진 config 로그 |
| `max_num_seqs` / `max_num_batched_tokens` | 128 / 2048 (CPU·OpenAI 서버 기본, world_size 1) | `engine/arg_utils.py` 2669–2676 |
| 최소 KV | max_model_len 요청 1개(2,048 토큰 = 16 블록)를 못 담으면 기동 실패 | `kv_cache_utils.py` 854–858("To serve at least one request with the model's max seq len") |

참고: 지금의 15,360 토큰은 "0.3 × 메모리 − 시작 시 RSS"라 RSS에 따라 재시작마다 조금씩 달라질 수 있다(자동값).

## 3. `--kv-cache-memory-bytes` 지원성 (설치된 소스 확인)

- CLI: `engine/arg_utils.py` 1233행 `--kv-cache-memory-bytes` 존재, 2029행에서 CacheConfig로 전달.
- 의미: `config/cache.py` 232–238행 - KV 크기(바이트, int). **지정하면 gpu_memory_utilization을 무시**한다.
- CPU 적용: `v1/worker/cpu_worker.py` 208–228행 - 지정값이 있으면 그 크기 그대로 KV로 쓰고 "Explicitly set … GiB for KV cache" 로그를 남긴다. 노드 가용 메모리보다 크면 기동 실패. 지정 시 시작 단계의 0.3 비율 검사(81–96행)도 건너뛴다.
- 효과: Pod 재시작이 필요(인자 변경 = 새 Rollout revision). 자동값보다 재현성이 좋다(RSS 무관한 고정 크기).
- `num_gpu_blocks_override`: v1에서는 GPU 경로(`gpu_model_runner.py`)·`kv_cache_utils.py`에서만 참조 - CPU에서 쓰지 않는다(이 계획에서 사용 안 함).
- 결론: **지원됨**(CPU 백엔드, 바이트 단위, 자동 계산을 덮어씀). 단 이번 단계에서 변경하지 않았다.

## 4. 후보 KV 예산 (최대 3개, 블록 = 1,572,864 B)

| 후보 | bytes | 블록 | 토큰 | 2048 토큰 요청 동시 수 | 정상(probe) 여유 | 압박 지점(예: 640 토큰 요청) | 위험 |
|---|---|---|---|---|---|---|---|
| 현재(자동) | ≈188,743,680 | 120 | 15,360 | 7.5 | 매우 큼 | ≈24개 동시 | - |
| Mild(½) | 94,371,840 | 60 | 7,680 | 3.75 | 매우 큼(probe 1블록) | ≈12개 동시 | 압박에 동시 요청이 많이 필요 → SLO 먼저 |
| Medium(¼) | 50,331,648 | 32 | 4,096 | 2.0 | 큼 | ≈6–7개 동시 | 같은 문제, 덜함 |
| Strong(⅛, 최소) | 25,165,824 | 16 | 2,048 | 1.0 | probe는 가능하나 긴 요청 1개가 KV 전체 점유 | ≈3–4개 동시 | 기동 하한, "극단적 축소" - 사용자 지시 4절("환경을 무작정 극단적으로 줄이지 마세요")에 해당 |

- 640 토큰 = 고유 prompt 512 + 생성 128 가정(5블록). `/v1/completions`(chat template 없음)로 고정하고, 요청당 실제 prompt 토큰 수를 counter 증가분으로 확인한다(단어 셔플 prompt는 정확히 512 토큰이 아닐 수 있음 - 6블록이 될 수 있음).
- 바이트→블록 변환: 층당 블록 페이지 = 128 × 2 × 64 × 2 × 2 = 65,536 B, 블록 수 = bytes // 65,536 // 24 → 세 후보 모두 정확히 60·32·16 블록(내림 손실 없음).
- 후보 집합 {½, ¼, ⅛}은 결과를 보기 전에 저자가 정한 설계 선택이다(규칙이 도출한 것이 아님).
- 선정 원칙(결과 보기 전 고정): ① 기동 하한의 2배 이상(= max_model_len 요청 2개 이상 수용, 긴 요청 1개가 전체를 독점하지 않음), ② 현재 값을 뺀 축소 후보 중 ①을 만족하는 가장 큰 예산(덜 바꾼 환경 우선), ③ 이후 바꾸지 않음.
- 이 원칙의 first candidate는 **Mild(60 블록)**. Mild가 실패해도 Medium·Strong은 **이번 pilot과 이후 어떤 pilot에서도** 쓰지 않는다(11절과 같은 규칙).

## 5. 새 환경 정상 기준선 계획

- KV 예산을 바꾸면 기존 정상 기준선(c1·c3·N1)을 쓸 수 없다 → **NEW-ENV NORMAL BASELINE** 먼저.
- 내용: 기존 정상 계약 그대로(probe 1 RPS·max_tokens=1·관찰 4200 s·record_only·5 s Pod 표본), 2 세션.
- 확인: SLO v3 위반 0·요청 실패 0, KV 사용률·running·waiting·선점·CPU·memory·토큰/iteration rate 기준선, "Explicitly set" 로그로 예산 적용 확인.
- 하나라도 불안정하면 pilot 중단.

## 6. workload 설계

- probe(품질 측정)와 pressure(부하)를 **다른 생성기·다른 요청 모양**으로 분리: probe는 그대로, pressure는 별도 Pod.
- KV를 실제로 오래 점유하려면 생성 길이가 필요하다(prefill만 하는 max_tokens=1 요청은 KV를 잠깐만 씀). 후보: 고유 prompt 약 512 토큰 + max_tokens 128(`ignore_eos`), 동시 수 {1, 2, 4, 8, 13}(13 = 60블록을 넘는 첫 동시 수, 선점이 가능한 유일한 단계), 단계당 600 s 이상.
- 같은 엔진에서 둘을 섞는 것은 시나리오 자체(사용자 요청이 KV를 다투는 상황)라 분리할 수 없다 - 분리되는 것은 측정 역할뿐.

## 7. 실현 가능성 판단 (핵심 - pilot 전 이미 알려진 사실)

- KV 압박(용량 부족 대기·선점)은 **동시에 오래 살아 있는 요청 여러 개**가 있어야 생긴다. Mild에서 640 토큰(5블록) 요청 기준 60블록을 넘기려면 약 13개가 동시에 필요하다(사용률 50%는 6개 - 단 사용률 자체는 "요청 수 × 길이"일 뿐 부족 상태가 아니다).
- 그러나 이 환경에서는 **겹치는 요청 1개**만으로 probe 지연이 평균 0.25 → 0.50 s로 늘고 12%가 0.648 s를 넘었다(`docs/design/phase8-aiops-v2-request-shape-axis-assessment-20261009.md` 9–13행, max_tokens=10 부하). 생성 128 토큰짜리 요청이 계속 겹치면 60 s 창 P95가 임계를 넘는 상태가 지속될 것으로 예상된다[판단].
- 따라서 KV 사용률이 의미 있게 오르는 구간(동시 수 ≥ 수 개)은 SLO 위반 구간과 겹치고, SLO 정상 구간에서의 KV 사용률은 요청 1개분(Mild 기준 5/60 ≈ 8%) 이하일 것이다[판단].
- 예산을 더 줄여도(Strong) 해결되지 않는다: 겹치는 요청 1개만으로 probe가 SLO 경계를 넘는다면 동시 수 1부터 SLO가 깨지기 때문이다. 이를 바꾸려면 probe/SLO 계약 변경이나 CPU 용량 추가가 필요하다(13절). 극단적 축소는 사용자 지시 4절과 11절 abort 사유에도 해당한다.
- 남은 불확실성: 생성 128 토큰 요청 1개가 계속 겹칠 때 실제 P95(측정된 적 없음 - 이전 측정은 10 토큰). 그래서 이 판단은 [판단]이며, 사용자가 원하면 `S3-KV-PRESSURE-PILOT-PREREG-DRAFT.md`의 pilot으로 확인할 수 있다.

## 8. 텔레메트리

- Prometheus 15 s scrape(ServiceMonitor 확인): KV 사용률·running·waiting은 gauge라 15 s 시점 표본. 선점은 counter라 놓치지 않는다(rate로 확인). vllm-active·vllm-preview 두 job이 같은 Pod를 긁으므로 모든 질의는 `job="vllm-active"`로 거른다.
- 판단: 600 s 이상 유지되는 압박 단계라면 15 s로 "상태 존재" 확인에는 충분하다. 전이 시각을 더 정밀하게 볼 필요가 생기면 vLLM 로그의 10 s 주기 `loggers.py` 줄(Running/Waiting/KV usage)을 보조 출처로 쓸 수 있다 - scrape 설정 변경 없이 가능.
- scrape 간격 변경은 다른 모든 실험과 지표 의미(rate 창)를 바꾸므로 제안하지 않는다.

## 9. pilot 질문 (detector 결과 안 봄)

Q1 새 예산에서 정상 기준선이 안정적인가 · Q2 SLO 위반 전 KV 압박(선점·용량 부족 대기)이 생기는 구간이 있는가(KV 사용률은 서술용) · Q3 SLO 위반 전 waiting·선점·running 변화가 있는가 · Q4 그 변화가 CPU 변화만으로 설명되지 않는가 · Q5 반복 재현되는가

## 10. 성공 기준 (pilot 전 고정, detector 무관)

모두 만족해야 FEASIBLE:
1. 새 환경 정상 기준선 2 세션 모두 SLO 위반 0·실패 0.
2. pressure 단계(600 s 이상) 중 **SLO 정상인 단계**(그 단계 600 s 전체에 SLO v3 episode가 없음)에서 KV 압박 지표가 나타남: 선점 rate > 0, 또는 `waiting_by_reason{reason="capacity"}` ≥ 1인 표본 비율 ≥ 0.2. KV 사용률은 서술용으로만 보고(요청 수 × 길이에 비례할 뿐 부족 상태가 아님). reason="deferred"(토큰 예산 대기)는 KV 압박으로 세지 않는다.
3. CPU로 완전히 설명되지 않음: 2를 만족한 단계 안에서 0.1 코어 CPU 구간별로 보면, 압박 지표 > 0인 표본의 50% 이상이 압박 지표 = 0인 표본과 같은 CPU 구간에 있을 것(같은 CPU에서 압박 유무가 갈림). 기준선과의 비교는 서술용.
4. 2 rep에서 같은 단계에서 재현.
5. 해당 상태가 15 s 표본 ≥ 20개(5분) 이상 연속 관찰됨(선점은 각 표본에서 `rate(vllm:num_preemptions_total[1m])` > 0, 대기는 capacity 사유 gauge ≥ 1).

## 11. 중단·NO_GO 기준

정상 기준선부터 SLO 위반·실패 / KV 사용률이 계속 ≈ 0 / 용량 부족(capacity) 대기·선점 전혀 없음 / 지표 변화 전에 SLO가 먼저 깨짐 / CPU만으로 설명됨 / 15 s로 관찰 불가 / 재현 안 됨 / 예산을 더 줄여야만 성립 → NO_GO. **실패 후 예산을 더 줄이며 다시 시도하지 않는다.**

## 12. confounder

- prefix caching: pressure prompt는 첫 128 토큰 블록부터 서로 달라야 하고, 모든 단계·두 rep 전체에서 한 번씩만 쓴다(재사용 시 캐시 적중) → 고정 seed로 만든 결정적 prompt 집합, 요청 순서 고정.
- 예산 변경으로 정상 상태 자체가 바뀜(새 기준선으로 처리).
- 생성 128 토큰이 EOS로 일찍 끝날 수 있음(`ignore_eos` 사용 여부를 사전등록에서 고정).
- 부하 생성기 CPU(별도 Pod/노드), recompute 비용이 CPU에도 보일 수 있음.

## 13. 이 pilot이 증명할 수 있는 것 / 없는 것

- 할 수 있다면: "KV 예산을 Mild로 줄인 CPU-vLLM에서 KV 압박 상태가 SLO 정상 범위에서 관찰 가능한가"(환경 사실).
- 할 수 없는 것: detector 우월성, GPU serving 일반화, 원래 환경(자동 KV 15,360 토큰)에 대한 결론.
- 현재 판단으로는 성공 기준 2를 만족할 가능성이 낮다(7절). 의미 있게 만들려면 최소 변경을 넘어선 변경(probe/SLO 계약을 토큰 단위로, 또는 더 큰 CPU 용량)이 필요하며 이는 사용자 결정 사항이다.
