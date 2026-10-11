# LITERATURE-GROUNDED-SCENARIOS-v1 (2026-10-11, 고정 후보 - detector 결과 보기 전)

> 이 문서는 시나리오 **선정**만 고정한다. detector 결과 기준(성공/실패)은 넣지 않는다 - 선정이 AI 결과와 독립임을 남기는 목적.
> 근거 출처·환경 대응: `LLM-SERVING-FAILURE-LITERATURE-REVIEW.md`. live 실험·workload·클러스터 변경은 이번 단계에서 하지 않았다.
> 선정 규칙(사용자 지시 6절): 문헌 보고 + 현재 환경 재현 가능성 + 메커니즘 설명 + 지표 수집 가능 + leakage 없음 + 반복·안전 + AI에 불리해도 질문 성립.
> 금지 규칙 준수: 기존 데이터를 detector로 재생해 시나리오를 고르지 않았다(Axis 2 감사는 지표 변동만 봤고 detector 점수는 보지 않음).
> Axis 2 원칙과의 관계: `AXIS2-FEASIBILITY-20261011.md`(L절)는 "AI가 이길 조건을 찾으려고 workload를 조정하지 않는다"고 했다. 이 문서는 그 원칙을 그대로 따른다 -
> 시나리오는 문헌에서 먼저 정했고, 아래 "다변량이 추가로 볼 정보"는 기대가 아니라 측정할 항목 목록이며, 현재 환경에서 성립하지 않는 후보는 실행 권고하지 않는다.

## 결론 요약

| 후보 | 문헌 근거 | 현재 환경 판정 | 이유 한 줄 |
|---|---|---|---|
| S1 긴 prompt prefill 간섭 | 강함 | **NEEDS_ENVIRONMENT_CHANGE** | prompt 길이 축은 이전에 문서상 기각, probe가 max_tokens=1이라 문헌 메커니즘(decode 정체)과 다름 |
| S2 버스트 동시 도착 | 강함 | **NEEDS_ENVIRONMENT_CHANGE** | 동시 요청 축도 이전에 문서상 기각, 후보 격자 대부분이 이미 SLO가 깨진 부하 영역 |
| S3 KV cache 압박·선점 | 강함 | **NEEDS_ENVIRONMENT_CHANGE** | KV 15,360 토큰을 채우기 전에 prefill 계산으로 SLO가 무너질 것, KV 예산 축소 설정 필요 |

## 0. 공통 조건

- 서빙·KV·CPU 기준선은 리뷰 문서 3절 그대로. probe는 1 RPS·max_tokens=1·SLO v3(60 s 창 P95 > 0.648 s가 30 s 지속). SLO·지연·실패는 **outcome 전용**.
- 비교 대상(나중 단계): C(보정된 CPU 단일 임계값), c3-A(frozen, cpu/memory mean·slope 4개). LLM 지표 detector가 필요하면 c3-A와 분리된 새 Axis 2 버전으로만.
- "CPU를 같은 값으로 맞추기"는 목표가 아니다. 나중 질문은 "CPU 단일 detector가 이 상태를 충분히 구분했는가".
- prefix caching이 켜져 있고 128 토큰 블록 단위로 캐시하므로, 긴 prompt는 **첫 블록부터** 요청마다 달라야 한다(공통 템플릿 앞부분만 있어도 부분 캐시 적중).

## S1. 긴 prompt prefill 간섭

| 항목 | 내용 |
|---|---|
| 문헌 근거 | Sarathi-Serve(OSDI'24), DistServe(OSDI'24), Splitwise(ISCA'24), TetriInfer(arXiv'24); vLLM 문서(chunked prefill V1 기본, decode 먼저 스케줄) |
| 문헌의 메커니즘 | 계산 무거운 긴 prefill이 **진행 중인 decode**의 step을 길게 만들어 토큰 간 시간(TBT/ITL)이 튐 |
| 이 환경과의 차이[판단] | probe는 max_tokens=1이라 그 자체가 짧은 prefill → 재게 되는 것은 prefill 대 prefill 대기(head-of-line). 1536 토큰 이하 prompt는 `max_num_batched_tokens` 2048 안에 들어가 chunk로 나뉘지 않는다. CPU에서 chunked prefill 동작은 문서상 미확인 |
| 이전 판정 | `phase8-aiops-v2-request-shape-axis-assessment-20261009.md` §3: prompt 길이 축 "부적합(더 나쁠 가능성)" - CPU prefill이 step을 더 오래 점유해 겹친 probe가 더 느려짐 |
| workload 후보(참고) | 고유 긴 prompt {256, 512, 1024, 1536} 토큰, max_tokens=1, 빈도 {1/60 s, 1/30 s, 1/15 s}, seed 고정 jitter 도착 |
| 예상 운영 지표 | prompt 토큰 rate, step당 토큰(`iteration_tokens_total` sum/count), running 0/1 빈도, CPU(prefill은 계산 집약이라 CPU에도 보일 가능성 높음[판단]) |
| CPU만으로 부족할 수 있는 이유 | 같은 CPU 상승이 짧은 요청 여러 개인지 엔진을 길게 막는 prefill인지 구분 못 함 - CPU가 충분히 구분할 수도 있음(그것도 결과) |
| 다변량이 볼 정보(측정 항목) | step당 토큰, prompt 토큰 rate, running 비율 |
| outcome | SLO v3, probe 지연 꼬리, TTFT, 요청 성공 |
| confounder | prefix caching, 부하 생성기 CPU, probe 겹침 비율, rate[1m] 단계 경계 혼합, 15 s scrape |
| leakage | queue_time·TTFT·prefill_time 입력 금지. prompt 토큰 rate는 offered load |
| 의미 있게 만들 환경 변경 | streaming decode가 있는 probe(예: max_tokens ≥ 16, 토큰 단위 SLO - Andes가 말하는 TTFT+토큰 속도) 또는 probe와 부하의 엔진 분리. 둘 다 SLO 계약·기준선 변경 |
| 판정 | **NEEDS_ENVIRONMENT_CHANGE** |

## S2. 버스트 동시 도착

| 항목 | 내용 |
|---|---|
| 문헌 근거 | BurstGPT(KDD'25), Llumnix(OSDI'24), AlpaServe(OSDI'23); GKE·NVIDIA Triton(사용률보다 queue·batch), OpenAI 2025-02-19 write-up(트래픽 급증 → 계산 포화) |
| 메커니즘 | 짧은 순간 동시 요청이 몰려 배치가 커지고 step이 느려짐, 한도를 넘으면 대기열 |
| 이전 판정 | request-shape assessment §3: 동시 요청 축 "부적합" - 동시 요청 = 겹침 그대로, 더 큼 |
| 격자 점검 | max_tokens 10 기준 k=4·8을 30 s마다 보내면 약 0.13–0.27 RPS → 이미 SLO가 깨진 0.05–0.15 RPS 영역. 새 정보가 나올 칸이 적다 |
| 예상 운영 지표 | running 순간 증가, 토큰·iteration rate 계단형, CPU 상승. waiting은 max_num_seqs 128이라 거의 안 늘 것[판단] |
| 기술적 한계 | 15 s scrape·~16 s 평가 주기보다 짧은 버스트는 gauge로 보기 어렵다 |
| 의미 있게 만들 환경 변경 | 대기열이 SLO 위반보다 먼저 생기도록 `--max-num-seqs`를 작게 명시(waiting이 실제로 쌓이는 구성) + 더 촘촘한 수집. 설정 변경·정상 기준선 재수집 필요 |
| 판정 | **NEEDS_ENVIRONMENT_CHANGE** |

## S3. KV cache 압박·선점

| 항목 | 내용 |
|---|---|
| 문헌 근거 | vLLM PagedAttention(SOSP'23 §4.5), vLLM *Optimization and Tuning*(RECOMPUTE 선점), S³(NeurIPS'23) |
| 메커니즘 | KV 블록 고갈 → 늦게 온 요청 선점 → recompute로 이미 한 계산 반복 |
| 현재 환경 | KV 15,360 토큰(`kv_cache_max_concurrency` 7.5). 2048 토큰 요청 약 8개가 동시에 있어야 고갈 → 그 전에 prefill 계산으로 SLO가 무너질 것[판단] |
| 필요한 변경 | vLLM 설정: KV 예산 축소(`--kv-cache-memory-bytes`, CPU 문서상 현재 옵션). `num_gpu_blocks_override`가 CPU 백엔드에서 동작하는지는 미확인 |
| 예상 운영 지표 | kv_cache_usage → 1, 선점 rate > 0, waiting(capacity) 증가 - CPU에 직접 안 보이는 상태(유일하게 LLM 지표가 꼭 필요한 후보) |
| confounder | 예산 축소 자체가 정상 상태를 바꿈(정상 기준선 재수집 필요), recompute 비용이 CPU에 보일 수 있음 |
| 판정 | **NEEDS_ENVIRONMENT_CHANGE** |

제외: 자원 경쟁(noisy-neighbor·디스크 I/O 이미 시험 - CPU 단독 분리 / SLO 즉시 위반), GPU 전용 해법(분리 배치·인스턴스 이전).

## 4. c3-A 4개 feature로 충분한가, LLM 지표가 필요한가 (판단)

- S1·S2는 추가 계산을 만들어 CPU가 오를 개연성이 높다 → c3-A의 cpu mean·slope도 반응할 수 있다. memory는 이전 부하 단계에서 평평했다.
- LLM 지표가 꼭 필요한 것은 S3(KV·선점). S1·S2의 추가 후보는 step당 토큰·prompt 토큰 rate·running.

## 5. 환경을 바꾼 뒤 pilot을 한다면 (사용자 승인 후, 탐색용 - 공식 증거 아님)

- 순서: 환경 변경 승인 → 바뀐 환경의 정상 기준선 재수집 → pilot → pilot과 분리된 사전등록 → 새 독립 세션 공식 실험.
- pilot 단계 길이는 **600 s 이상**(아래 (b)의 10분 조건과 맞춤).
- GO 기준은 detector와 무관한 환경 기준만:
  - (a) 메커니즘 재현 - 부하량만으로 설명되지 않는 신호여야 함: 같은 토큰 rate 구간에서 step당 토큰·probe 겹침·선점·waiting이 기준선과 다른가(토큰 rate 증가 자체는 부하의 정의라 기준이 아님).
  - (b) 등급 존재 - 메커니즘이 켜진 채 SLO 정상으로 10분 이상 유지되는 단계 ≥ 1과 SLO 위반 단계 ≥ 1이 같은 rep에서.
  - (c) 정답 정의 - "어느 상태가 비정상인가"를 SLO outcome과 모순 없이 결과 보기 전에 정할 수 있는가(Axis 2 Q5를 막은 조건). 이것이 안 되면 (a)(b)를 통과해도 NO_GO.
  - (d) 반복성 - 2 rep에서 같은 단계 근처.
- 이전 decode형 ladder는 (b)만 보면 통과했을 것(v2wl90 L1–L3 SLO 정상, L4 위반)이지만 NO_GO였다 - 그래서 (a)(c)를 함께 요구한다.

## 6. 이번 판정

- 문헌에는 CPU 단일 사용률로 충분히 설명되지 않는 serving 성능저하 메커니즘이 분명히 있다(S1·S2·S3).
- 그러나 현재 환경(단일 CPU 엔진 공유, max_tokens=1 probe, SLO 임계 ≈ 겹치는 요청 1개)에서는 S1·S2가 이미 기각된 축으로 되돌아가고, S3는 설정 변경이 있어야 일어난다.
- 따라서 현재 환경 그대로의 pilot은 권고하지 않는다. 의미 있는 다음 단계는 모두 환경·계약 변경(5절)이며 사용자 결정 사항이다.
