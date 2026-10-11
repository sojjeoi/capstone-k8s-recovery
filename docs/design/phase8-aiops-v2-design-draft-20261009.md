# AIOps v2 설계 초안 (DRAFT, 동결 아님, 실행 승인 아님) — 2026-10-09

## 목표 재확인 및 다음 설계 필수 조건 (2026-10-09 사용자 지시, 아래 초안보다 우선)

**원래 연구 질문:** 실제 LLM 운영에서 CPU 단일 지표만으로 구분하기 어려운 정상 상태와 서비스 저하 상황을, 여러 운영 지표의 정상 패턴·관계를 학습한 이상 탐지기가 더 유용하게 구분할 수 있는가? 정상 운전의 불필요 신호를 늘리지 않으면서 저하에 앞선 유용한 신호를 제공하는지 검증한다. 우월성은 가설이며 결과를 미리 정하지 않는다.

IF는 정상 학습에 기반해 이상 점수를 내고, 연속 판정 규칙이 신호를 만들며, guarded 정책이 조치를 결정한다. IF에 장애 원인 진단·미래 장애 확정 능력이 있다고 설명하지 않는다. “CPU+다른 요소”는 우선 관측 지표들의 관계를 뜻하며, 복합 장애 주입 자체가 목적은 아니다.

**기존 검증의 위치:** A/B는 전환 효과, B/C/K는 현재 CPU 압박의 trigger 비교, N1은 정상 불필요 신호를 검증했다. 핵심 다변량 가설을 충분히 검증한 것으로 취급하지 않는다. c1 queue/cache는 분산 0으로 제외됐고, 남은 입력·시나리오가 가설에 필요한 정보를 주는지 먼저 확인했어야 했다. 주변 검증의 축적을 핵심 목표의 진전으로 대신 설명하지 않는다.

**F1 우선 정정:** 감사 문서 `phase8-c1-detector-integrity-audit-20261009.md` §Q·R·U에 따라, 아래 “B의 추가 40–50초 조기성”은 CPU 압박 탐지 성과로 해석하지 않는다. 시작 직후 저CPU에서 주입 전부터 이상 판정이 쌓였다. 공식 A/B·B/C/K·N1 값은 유지한다. 모델이 학습 범위 밖 모든 값을 정확히 이상으로 잡는다고도 주장하지 않는다.

다음 설계안은 실행 제안 전에 다음 다섯 가지를 짧고 구체적으로 답해야 한다.

1. **질문과 상황:** CPU만으로 구분하기 어려운 정상/저하 상황은 무엇이며, 왜 실제 운영 질문과 연결되는가? 비교군이 실패하도록 상황을 선택하지 않는다.
2. **추가 정보와 관측 가능성:** CPU 외 어떤 지표·관계가 정상에서도 관측되고 저하와 구분되는가? 지표가 계속 0이거나 변화가 SLO 위반 뒤에만 나타나면 조기 신호 후보로 삼을 근거가 부족하다. 기존 자료는 가능성 검토에만 쓴다.
3. **공정 비교와 주장:** 정상 학습·보정·독립 검증을 분리하고 정상 운전 범위를 명시한다. IF와 CPU 규칙은 같은 보정 자료·발화 기준으로 비교한다. AI 학습 자체의 추가 가치를 주장하려면 같은 추가 지표를 사용하는 단순 규칙도 비교한다. CPU 단일 규칙 대비 이득만 확인했다면 주장도 그 범위로 제한한다.
4. **사전 판정과 중단:** 정상 불필요 신호와 저하 시 유용한 신호를 함께 평가한다. 시작 구간 처리, SLO와의 시간 관계, 무발화 처리, 성공·중단 기준을 결과 전에 정한다. 정보가 없는 환경에서 반복 수만 늘리지 않는다.
5. **남은 시간:** 10/15에는 확정 결과와 미검증 가설을 구분해서 제시하고, 10/30까지 핵심 질문에 직접 필요한 최소 경로·소요 시간·실패 시 대안을 명시한다. 이미 확인된 복구 경로를 목적 없이 재검증하지 않는다.

**최신 상태와 초안의 구분:** 아래 r1–r3는 이력 있는 초안이며 실행할 확정안이 아니다. `V2WL-FEASIBILITY-RESULT-20261009.md`는 feasible=false, request-shape 축 평가도 부적합이다. `phase8-aiops-v2-progress-metric-feasibility-20261009.md`의 CPU와 처리량 관계는 조건부 탐색 후보이며 압박 방식 의존성이 있다. 이를 v2 성공·새 특징 확정·새 실험 승인으로 읽지 않는다. 후속은 새 모델·새 계약·새 데이터 계보로 설계하며 c1 동결 결과는 보존한다.

## r3 feasibility freeze 전 수정 (2026-10-09, r2·r1보다 우선)

- **`vllm:request_success_total`의 실제 의미(읽기 전용 확인):** Prometheus metadata TYPE=counter, HELP "Count of successfully processed requests."; 라벨 `finished_reason`=stop|length|abort|error|repetition(+ engine, model_name, pod, job). vLLM 0.28.1rc1 소스(`v1/metrics/loggers.py`)에서 `iteration_stats.finished_requests`마다 `.inc()` - **요청이 끝날 때 증가하는 완료 counter이며 도착률이 아니다**. abort(클라이언트 중단)·error(엔진 내부 오류)도 별도 라벨로 포함, 엔진에 들어오기 전 거절된 요청은 없다.
- **재분류:** Service throughput = `request_success_total` rate(finished_reason 합; abort|error는 진단으로 따로). r2의 "Workload context(도착률 대리)" 분류는 폐기(도착률 아님).
- **실제 도착률(offered load) metric은 현재 instrumentation에 없다.** 기본안: (A) harness 목표·실제 발사 RPS는 workload 라벨·진단으로만 보존(탐지기 입력 아님 - 실제 운영에서는 이 값을 알 수 없음), (B) throughput은 service feature 후보. 정상 고부하에서 throughput은 도착률을 따라가므로 대리 역할은 하지만, 포화 시 도착률보다 낮아진다(정보 차이). 새 Prometheus 도착 counter 추가는 이번 단계에서 하지 않음 - **한계로 기록**, 필요성은 pilot 뒤 별도 결정.
- **이중 수집:** 같은 pod를 `vllm-active`·`vllm-preview` job이 모두 수집 → 분석은 `job="vllm-active"`만(이전 `max(...)` 집계는 finished_reason 라벨까지 max로 합쳐 잘못이었음 - 수정).
- **표본:** query_range 평가 격자 대신 원본 scrape 표본(실제 scrape 시각)을 저장해 활성화 비율을 센다.
- **도착 aliasing 수정:** 기존 ramp.py는 고정 간격이라 15 s scrape와 위상 고정 → 발사 시각만 jittered_grid_v1(seed 사전 고정)로 바꾼다. 활성화 0.20 유지. SLO 경계 종료 규칙 추가. 상세는 feasibility 계약 r2.

## r2 사용자 결정 반영 (2026-10-09, 아래 r1 본문보다 우선)

- 일정: **Plan B** - 10/15 현재 frozen 결과로 발표 + v2 설계·feasibility pilot, 10/30 v2-1 독립 검증.
- 시나리오: **V2-S1만**(정상 고부하 위 기존 검증 CPU 압박). ②(이웃 Pod 경합)는 V2-S1이 구조적으로 불가·재현 불가일 때만 재검토. 네트워크·메모리·Pod kill 제외.
- 알고리즘: **Isolation Forest 유지**(c1과 방법 연속, 알고리즘 쇼핑 방지). 다른 알고리즘은 향후 탐색만.
- primary 특징 후보(지연 직접 사용 금지): Resource `cpu_mean`·`cpu_slope`(c1 정의); Service state `vllm:num_requests_running`·slope, `vllm:num_requests_waiting`·slope, `vllm:request_queue_time_seconds`(sum/count) 창 요약·slope; Service throughput `vllm:request_success_total` rate(r3: 완료 counter, 도착률 아님 - 위 r3 참고). **제외:** probe 지연, `vllm:e2e_request_latency_seconds`, `vllm:time_to_first_token_seconds`, `vllm:request_prefill_time_seconds`(SLO 누설). **진단 후보만:** `vllm:generation_tokens_total` rate, CPU/처리량 비, cache 관련 비 - pilot 분산을 본 뒤 별도 결정.
- 최우선 과제: 현재 1 RPS·max_tokens=1에서 service-state 지표가 0 → 모델·보정 전에 **정상이면서 service-state 지표가 움직이는 workload**를 counted=false pilot으로 찾는다(계약 `phase8-aiops-v2-workload-feasibility-contract-20261009.md`).
- 오탐 예산(공통): "보정 정상 자료 전체에서 3연속 발화 0회인 가장 민감한 임계값" - IF v2(score)·C2(cpu_mean) 같은 보정 세션·평가 tick·consecutive=3·입력 유효성 규칙. 동결 `model_v31/replay.calibrate_threshold`(score 낮을수록 이상, C는 −cpu_mean)로 재사용 가능함을 코드로 확인(bck_fixed_threshold_calibration.py가 같은 방식). 계산은 아직 안 함.
- D_safe 게이트(margin 0, 형식적 비열등 시험 아님): HL(D_safe) ≥ 0 **그리고** 불필요 신호 발생 세션 수 v2 ≤ C2. 의미: "v2가 C2보다 정상 고부하에서 더 시끄럽지 않다"는 운영 게이트일 뿐, 동등성·비열등을 통계적으로 입증하는 것은 아니다.
- D_lead 게이트: 유효 degradation 쌍 6, 6/6 D_lead > 0, HL(D_lead) > 0. **늦은 발화·무발화 수치화(제안, 결과 보기 전 고정):** 판정 창 끝 H(주입 뒤 고정 관찰 끝)까지 신호가 없거나 t_slo 이후 신호면 lead = t_slo − min(t_signal, H) 그대로(음수 허용), 신호 없음은 t_signal = H로 놓는다(가장 늦은 가능한 값 = 벌점). t_slo가 없는 run(공식 SLO 저하 미발생)은 lead 정의 불가 → degradation 유효성 규칙에서 "SLO 저하 미발생"으로 사전 분류(arm 결과와 무관하게 판정).
- 규모: normal-high 독립 검증 유효 6 / 최대 9, degradation 독립 검증 유효 6 / 최대 9. 둘 다 같은 tick record_only(v2, C2, historical C, K shadow). N1·B/C/K 결과 합산 금지.
- lineage(실행 ID 분리): ① workload feasibility pilot `v2wl90–92` ② normal training ③ normal calibration ④ normal-high validation ⑤ degradation development pilot ⑥ degradation validation. 서로 재사용 금지, N1·B/C/K 원자료 사용 금지. (②–⑥ ID는 별도 계약에서.)
- V2-2(실제 전환)는 v2-1이 두 게이트와 메커니즘·증거 조건을 모두 통과한 경우에만 별도 설계.

> current v3.3-c1은 완료된 baseline/ablation으로 잠금(인수인계 `CAPSTONE_HANDOFF_2026-10-09-N1-FINAL.md` §4). v2는 **새 모델·새 계약·새 데이터 계보**. 기존 A/B·B/C/K·N1 결과와 원자료는 v2 학습·보정·검증 데이터로 쓰지 않는다(문제 정의·설계 참고만).
> 이 문서 작성 중 live 행동 없음. 아래 "확인된 사실"은 2026-10-09 Prometheus 읽기 조회 결과.

## 1. 왜 v2인가 (현재 결과에서 이어지는 질문)

- 현재 CPU 압박은 컨테이너 CPU 수준이 그대로 올라가는 단조로운 장애라, 보정된 단일 CPU 임계값(C)도 충분히 일찍 반응했다(B와 SLO 5/6 동률). B의 추가 약 40–50 s 조기성은 SLO 차이를 만들지 못했다.
- 정상 운전에서는 current IF가 C보다 더 일찍 불필요 신호를 냈다(6/6). current c1의 정상 학습 자료는 probe 1 RPS·max_tokens=1 한 가지 workload뿐이었다(정상 범위가 좁음).
- 그래서 AIOps의 실제 추가 가치는 "CPU가 높다"가 아니라 **"높은 CPU가 정상 고부하인지 실제 성능 저하인지"**를 구분하는 데서 검증해야 한다.

**v2 연구 질문(안):** 같은 정상 오탐 예산(false-positive budget)에서, 다변량 운영 지표 기반 탐지기가 정상 고부하에서 같은 수준으로 조용하면서, 실제 degradation을 단일 CPU 임계값보다 더 일찍 또는 더 안정적으로 탐지하는가?

## 2. 확인된 사실 — 현재 수집 metric (읽기 전용)

- Prometheus에 vLLM metric 96개(`vllm:*`)가 15 s 간격, Pod 라벨로 수집 중. active·preview 두 job(`vllm-active`, `vllm-preview`)이 같은 Pod를 이중 수집하는 경우가 있어 **`pod` 라벨로 걸러야 한다**.
- 컨테이너 CPU(`container_cpu_usage_seconds_total`, cAdvisor)·메모리, node-exporter(30 s 간격) 사용 가능.
- 현재 workload(probe 1 RPS, max_tokens=1)에서 측정한 분산(nb08 정상 69분 / bc06k CPU 압박 전후):

| metric (exact) | 정상(nb08) | CPU 압박(bc06k, 승격 전) | 판단 |
|---|---|---|---|
| `vllm:num_requests_running` | 항상 0 | 항상 0 | 현재 workload에선 정보 없음(요청 0.23 s, 15 s 표본) |
| `vllm:num_requests_waiting` | 항상 0 | 항상 0 | 동일 |
| `vllm:kv_cache_usage_perc` | 0 | 0 | 동일 |
| `vllm:request_queue_time_seconds` (rate sum/count) | 약 50 µs | 약 50 µs | 대기열 없음 |
| `vllm:request_prefill_time_seconds` (rate sum/count) | 0.204–0.264 s | 0.234–0.620 s | 압박에 반응, 단 사용자 지연과 거의 같은 양(누설 위험) |
| `vllm:time_to_first_token_seconds`, `vllm:e2e_request_latency_seconds` | 0.21–0.27 s | 0.24–0.62 s | 사용자 지연 그 자체에 가까움 → 누설 |
| `vllm:request_success_total`, `vllm:iteration_tokens_total_count` rate | 1.0/s | 1.0/s | probe 부하 그대로 |
| 컨테이너 CPU | 1.27–1.69 코어 | 1.66–2.99 코어 | 현재 c1 입력 |

→ **현재 workload로는 queue·scheduling 지표가 퇴화(분산 0)한다.** v2가 다변량의 의미를 가지려면 동시 요청이 생기는 정상 workload가 필요하다(c1이 queue/cache를 "학습 분산 0"으로 뺀 이유와 같다).
- 정상 고부하 생성 도구: 기존 `experiments/load_ramp_adapter.py`(클러스터 안 ramp Pod, 단계별 config) 재사용 가능. 노드 3대 각 8 vCPU·16 GiB, vLLM 컨테이너 limit 3 코어.

## 3. 특징(feature) 후보

| 분류 | 후보(exact metric 기반) | 왜 필요한가 | 누설 위험 | 현재 수집 |
|---|---|---|---|---|
| A. 자원 | 컨테이너 `cpu_mean`·`cpu_slope`(c1 정의), 메모리 | 부하 수준 | 낮음 | 예 |
| B. 서비스 스케줄링 | `num_requests_running`·`num_requests_waiting` 창 평균, queue time 평균, `iteration_tokens_total` rate(엔진 step 처리율) | 같은 CPU에서 처리 속도가 떨어지는지(대기 증가·처리율 감소) | 중간(대기는 지연의 원인 쪽, SLO 판정 양 자체는 아님) | 예(동시 요청이 있어야 분산 생김) |
| C. throughput(r3: 도착률 아님) | `request_success_total` rate, `prompt_tokens_total`·`generation_tokens_total` rate | 정상 고부하 여부(일이 많아서 CPU가 높은지) - 완료 기준이라 포화 시 도착률보다 낮음 | 낮음 | 예 |
| D. 사용자 지연 | e2e·TTFT·prefill 시간, probe 지연 | - | **높음**(SLO 정의량과 거의 같음) | 예 |

- **추천안:** A(cpu_mean, cpu_slope) + B(running 평균, waiting 평균, queue time 평균) + C(요청 처리율, 생성 토큰 처리율). 핵심 파생: "처리량 대비 CPU"(CPU 코어 ÷ 토큰 처리율) - 정상 고부하는 CPU와 처리량이 함께 오르고, degradation은 처리량 대비 CPU·대기가 나빠진다는 가설.
- **대안:** 추천안 + prefill 시간 평균(서버 계산 시간). 탐지력은 오르지만 SLO 누설 위험 - 채택 시 "SLO 판정량과 같은 축"임을 공개하고 lead time 해석을 따로 한다.
- 제외: e2e·TTFT·probe 지연(primary 탐지 입력에서 제외, 진단만).

## 4. 정상 학습·보정 자료

- 정상 workload 수준 3개를 **사전에 고정**: low / medium / high-but-SLO-normal. 각 수준은 load_ramp 설정(동시 요청 수·요청률·max_tokens)으로 정의하고, high는 pilot에서 공식 SLO가 정상인 최대 수준의 약 80%로 고정(pilot 자료는 학습에 안 씀).
- 공식 SLO는 기존과 같은 probe(1 RPS, max_tokens=1)·slo_judge v3로만 판정. 배경 부하와 별개.
- **세 집합 분리:** 학습(train) / 보정(calibration) / 독립 검증(validation) 세션을 실행 ID로 구분, 순서·개수 사전 고정. 각 수준을 세 집합 모두에 포함.
- 공식 SLO가 정상인 구간만 normal 후보. SLO 위반이 생긴 정상 수집 세션은 버리지 않고 기록·제외 사유 공개.
- 현재 N1·B/C/K·A/B 원자료는 사용하지 않는다.

## 5. 공정한 임계값 비교군

- **주 비교군 C2:** 같은 v2 보정 세션(세 workload 수준 모두)에 동결 함수 `model_v31/replay.calibrate_threshold`를 그대로 적용해 "모든 보정 세션에서 3연속 경고 0인 가장 민감한 cpu_mean 임계값"을 구한다(current C를 만든 방법과 같음, 정상 범위만 넓어짐). 3연속·쿨다운 60 s 동일.
- v2 탐지기 임계값도 **같은 보정 세션·같은 규칙**(3연속 경고 0인 가장 민감한 값)으로 정한다 → 두 탐지기가 같은 오탐 예산.
- 보조: current C(1.6842544293089357, 연속성 기준), K(2.7). 주장에는 쓰지 않음.
- 약한 비교군을 고르지 않는다: C2를 주 비교군으로 고정한 뒤에만 결과를 본다.

## 6. degradation 시나리오 후보

| 후보 | 정상 고부하와 지표 겹침 | SLO 저하 재현성 | Pod Ready 유지 | preview 복구 의미 | 안전 | 구현 시간 | 1회 반복 | 새 계측 |
|---|---|---|---|---|---|---|---|---|
| ① 정상 고부하 위 컨테이너 CPU 압박(기존 StressChaos, 배경 부하 추가) | 높음 - CPU가 정상 고부하에서도 높아 C2 임계값이 올라감 | 높음(기존 3단계 압박 검증됨) | 예 | 예(다른 노드 preview) | 검증된 경로 | 짧음(배경 부하 설정만) | 약 25분 | 없음 |
| ② 같은 노드의 이웃 Pod CPU 경합(stress Pod를 active 노드에 고정) | 중간 - vLLM CPU는 안 오를 수 있고 처리만 느려짐 | 미확인(pilot 필요) | 예 | 예 | stress Pod limit로 제한 가능 | 중간(새 Pod·노드 고정) | 약 25분 | 없음 |
| ③ 네트워크 지연(NetworkChaos) | 낮음 | 높음 | 예 | 예 | 보통 | 중간 | 약 25분 | vLLM 내부 지표가 지연을 못 봄 → 사용자 지연(누설)으로만 탐지 |
| (제외) 메모리 압박·Pod kill·노드 장애 | - | - | OOM·재시작 | Kubernetes 자체 복구와 겹침 | 위험 | - | - | - |
| (제외) 요청 폭주 자체 | 정상 고부하와 정의상 같음 | - | 예 | 아니오(같은 용량 Pod로 바꿔도 해결 안 됨) | - | - | - | - |

**1순위 추천: ①** - 같은 장애를 유지하고 정상 범위만 넓혀 "높은 CPU = 장애?"를 직접 묻는다. 검증된 압박·안전 경로를 그대로 쓰고, 장애를 C2가 실패하도록 고르는 것이 아니다(C2도 같은 넓은 정상 범위로 공정 보정). ②는 시간이 남을 때 두 번째 시나리오.

## 7. 지표·판정 (V2-1, record_only 탐지 비교)

- 같은 tick 짝 비교(N1·B/C/K와 같은 공통 tick 구조): v2 탐지기 vs C2(+ current C, K shadow).
- **정상 고부하 세션(independent validation):** N1과 같은 T_safe·불필요 신호 분류(판정 창·추적 창·infra 창 규칙 상속).
- **degradation 세션:** lead = t_slo − 첫 신호 시각(주입 뒤 신호만, 신호 없거나 t_slo 뒤면 0 또는 음수로 기록 - 사전 고정). 탐지율 = t_slo 전 신호 비율.
- **primary 후보(둘 다 사전 고정, co-primary):**
  1. degradation 짝 lead 차이 D_lead = lead(v2) − lead(C2) (양수 = v2가 먼저).
  2. 정상 고부하 짝 T_safe 차이 D_safe = T_safe(v2) − T_safe(C2) (음수가 크면 v2가 더 시끄러움).
- 통계: 각 6쌍, 정확 Wilcoxon 양측 α 0.05 + HL(동결 함수 재사용), 동률·작은 n 한계 명시.

## 8. "v2가 낫다"고 말할 사전 조건(안)

- D_lead: 6쌍 모두 양수(→ p = 0.03125) 그리고 HL > 0, **그리고**
- D_safe: v2가 C2보다 정상 고부하에서 더 시끄럽지 않음 - 사전 고정 비열등 기준(안: D_safe 음수 쌍 0개, 또는 HL ≥ −600 s; 사용자 결정 필요).
- 하나라도 못 미치면 사전 문장으로 보고("조기성 이득은 있었으나 오탐 비용 증가" 등). current c1 결과와 합산하지 않는다.

## 9. 단계

- **V2-1(우선):** record_only 탐지 비교만(정상 고부하 판별 + degradation 탐지 + 오탐 대 lead time).
- **V2-2(선택, V2-1 성공 시):** 동일 동결 복구 경로(guarded policy → Argo promotion → 서비스 확인)를 연결한 소규모 act 검증. 복구 경로는 이미 A/B·B/C/K로 검증됐으므로 V2-1에서 다시 검증하지 않는다.

## 10. 일정 추정 (작업 시간; 세션 1회 ≈ 준비 8분 + 관찰 + 정리 5분)

| 단계 | 예상 |
|---|---|
| metric feasibility(배경 부하로 queue 지표 분산 확인 pilot 포함) | 0.5일 |
| v2 설계·계약·freeze(특징 추출·학습·C2 보정·분석기·테스트 구현) | 1.5–2일 |
| 정상 수집(3수준 × 학습 2 + 보정 2, 30분 관찰) | 약 9시간(무인 batch) |
| degradation 개발 pilot | 0.5일 |
| 학습·보정 | 2–3시간 |
| 독립 검증(정상 고부하 6 × 약 75분 + degradation 6 × 약 25분) | 약 10시간(무인 batch) |
| 분석 | 0.5일 |
| 선택 act 검증(6쌍) | 0.5일 + 약 5시간 |
| 발표 반영 | 1일 |

합계 약 5–6 작업일 + 무인 실행 약 19시간.

## 11. 계획 비교

| | 내용 | 위험 |
|---|---|---|
| Plan A | 10/15 전 독립 검증까지 | 6일에 5–6 작업일 + 19시간 무인 실행 - 전원·장애·재시도 여유 없음. 서두르면 사전 고정이 약해질 위험 |
| **Plan B(추천)** | 10/15: 현재 결과(A/B·B/C/K·N1)로 발표 + v2 설계·feasibility pilot까지. 10/30 전 v2 본검증 | 10/15 발표에 v2 결과 없음(설계·문제 정의로 제시). 10/30까지 시간 여유 있음 |
| Plan C | v2 없이 10/15 발표, 10/30까지 확장 | B와 비슷하나 10/15에 v2 근거(feasibility)가 전혀 없음 |

## 12. 발표 이야기 (현재 결과만으로 성립)

1. slow-but-alive vLLM 문제 → 2. 조기 신호 → guarded 복구 시스템 구현 → 3. A/B: 실제 전환이 무전환보다 사용자 피해 감소(5/5) → 4. "그럼 AI가 꼭 필요한가?" → 5. B/C/K: 보정된 단일 임계값도 CPU 압박에서는 충분히 강함(5/6 동률), legacy 2.7은 늦음 → 6. N1: current IF는 정상 운전에서 더 일찍 불필요 신호(6/6) → 7. 결론: 단순 CPU 수준 문제에서는 current IF보다 보정 임계값이 더 실용적 → 8. 후속: AIOps의 진짜 가치는 정상 고부하와 실제 저하의 상태 구분(v2).
- 이 흐름은 동결 근거와 맞는다. 과장 위험: "AI 무용론"으로 읽히지 않게 범위(current c1, 현재 workload, n=6)를 항상 붙인다.

## 13. 구현 전 사용자 결정 항목

1. Plan B 채택 여부.
2. degradation 시나리오 ①(추천) 확정 여부, ② 추가 여부.
3. 정상 workload 3수준 정의 방법(pilot으로 high 상한을 찾는 절차 승인).
4. 특징 추천안 vs 대안(prefill 시간 포함 여부 - 누설 위험).
5. 탐지 알고리즘: IF 유지(특징·데이터 효과만 분리) vs 다른 알고리즘.
6. 오탐 예산 정의(보정 세션 3연속 경고 0) 확정.
7. D_safe 비열등 기준 수치.
8. 반복 수(각 6쌍)·최대 시도.
9. V2-2 act 검증 포함 여부.
