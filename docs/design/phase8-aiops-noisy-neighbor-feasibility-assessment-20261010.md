# 같은 노드 noisy-neighbor CPU 경합 feasibility 평가 (읽기 전용, 2026-10-10)

> 상태: 평가 문서. 실행·구현·stress·cluster 변경 없음. 아래 Stage 0 초안은 **frozen 아님**.
> 기존 결과(c1, A/B, B/C/K, N1, 감사 F1, v2wl, request-shape, progress, iof Stage 0)는 수정·재분류하지 않는다.
> 표기: [확인] = 이번에 읽기 전용으로 직접 확인, [추정] = 근거 있는 예측, [계획] = 실행 안 한 초안.

## 1. 질문

현재 K8s/vLLM 환경에서 단일 임계값만으로는 정상과 저하를 구분하기 어렵고, 다변량 정상분포 기반 탐지(IF2)가 추가 정보를 쓸 수 있는 현실적인 운영 상황이 있는가? 1순위 후보: 같은 노드의 **다른 pod**가 CPU를 쓰는 경합(vLLM 컨테이너 안 stress 아님).

## 2. 확인된 환경 [확인]

| 항목 | 값 |
|---|---|
| active vLLM | `vllm-serving-7976ddcdb6-56xmz`, sj-worker2(Stage 0 I/O family canonical 배치 유지) |
| 리소스·QoS | requests cpu 2 / mem 4Gi, limits cpu 3 / mem 6Gi → **Burstable**. cgroup v1 cpu.shares 2048, cfs_quota 300000/100000 |
| CPU manager | kubelet 설정 읽기 실패(configz 빈 응답). Burstable이므로 정책과 무관하게 전용 코어 없음 |
| 노드 | sj-worker·sj-worker2 각 8 vCPU KVM(Xeon E5-2697 v4), 코어당 1 thread, NUMA 1, allocatable 8 |
| sj-worker2 할당 | requests 2250m(28 %), limits 3(37 %). pod: vLLM, chaos-daemon, calico-node, kube-proxy, node-exporter |
| vLLM 스레드 | `OMP_NUM_THREADS=7`, libiomp5. VLLM::Worker(PID 143) 스레드 54개 중 **48개가 CPU 0**, 6개가 CPU 1–6에 하나씩 고정. EngineCore 34개·API 서버는 0–7 |
| steal | 기준선·개발 세션 모두 ≈ 0.001 core → 하이퍼바이저 경합 흔적 없음 |

기존 원자료 단서(Prometheus 읽기 전용 range 질의, step 30 s, 2분 rate; 스크립트 `experiments/results/exploratory_noisy_neighbor_20261010/contention_window_query.py`):

| 지표(sj-worker2) | iob91 기준선 중앙값 [min–max] | iod90(I/O 개발) 중앙값 [min–max] |
|---|---|---|
| 노드 사용 core(8 − idle) | 3.53 [2.36–3.64] | 3.67 [2.47–3.80] |
| CPU PSI some waiting (s/s) | 0.136 [0.051–0.155] | 0.146 [0.050–0.156] |
| schedstat waiting 합 (s/s) | 3.74 [0.65–4.49] | 3.90 [0.63–4.40] |
| load1 | 5.3 [2.3–9.8] | 4.8 [2.6–11.3] |
| vLLM 컨테이너 CPU (core) | 1.73 [0.02–1.84] | 1.76 [0.02–1.92] |
| vLLM CFS throttled 비율 | **0.28** [0–0.38] | 0.35 [0–0.38] |
| CPU 0 사용률 | **0.88** | 0.88 |
| CPU 7 사용률 | 0.38 | 0.40 |

해석:
- [확인] 정상 상태에서 이미 vLLM은 quota(3 core) 때문에 주기의 약 28 %가 throttle되고, OMP 주 스레드가 있는 CPU 0이 88 % 바쁘다. 노드 전체로는 약 4.5 core가 놀고 있다.
- [추정] 이웃 pod가 **남는 4.5 core 안에서만** 쓰면 vLLM은 거의 영향이 없고, 노드 지표(PSI·load·schedstat)만 오른다 → 자연스러운 **weak 후보**. 이웃이 노드를 포화시키면 7개 OMP 스레드가 코어를 나눠 쓰게 되고, OMP는 가장 늦은 스레드를 기다리므로(barrier) 한 단계 전체가 느려진다 → **strong 후보**. I/O와 달리 vLLM CPU 백엔드는 CPU-bound라서 물리적으로 영향 경로가 직접적이다.
- [추정] 다만 CFS는 cgroup 가중치(vLLM 2048 vs 이웃 request에 비례)를 코어별로 나눠 주므로, 이웃 request가 작으면 강하게 포화시켜도 vLLM이 대부분을 이긴다. 강도 축은 "이웃 스레드 수·limit"뿐 아니라 "이웃 request(가중치)"가 핵심이다.

## 3. 현실성

- 현실 운영에서 흔하다: requests < limits인 Burstable pod 여러 개가 같은 노드에 몰리면, 각자는 정상인데 합쳐서 노드가 포화된다. 클라우드 "noisy neighbor"의 K8s 판(같은 노드 batch job, 로그 압축, 빌드 job 등).
- 우리 환경에서 재현 가능: sj-worker2에는 requests 여유 5.75 core가 있어 정상적인 스케줄링으로 이웃 pod를 올릴 수 있다(`nodeName` 고정은 실험 통제용).
- 제약: vLLM은 Guaranteed가 아니고 CPU manager static 여부도 미확인 → 운영 관점의 정답 조치는 "Guaranteed + 전용 코어"일 수 있다. 이 점은 발표에서 숨기지 말고 "현재 Burstable 운영 가정"으로 명시해야 한다.

## 4. weak / strong 가능성

| 상태 | 만드는 법 [계획] | 기대 [추정] | 주요 위험 |
|---|---|---|---|
| weak | 이웃이 idle core 범위 안(예: busy 스레드 3–4개, limit 3–4, request 작게) | 노드 사용·PSI·load↑, vLLM CPU·throughput·SLO 거의 그대로 | 이웃 스레드가 CPU 0에 올라가면 weak도 SLO 위반 가능 |
| strong | 이웃이 노드 포화(스레드 8개, limit 6–8, request 2–4로 가중치 확보) | OMP barrier 지연 → 생성 속도↓, probe 지연↑, pod는 살아 있음(slow-but-alive) | CFS 가중치 때문에 SLO 위반까지 안 갈 수 있음 |

SLO 여유: probe만 있을 때 P95 0.30–0.37 s, 임계 0.648 s → strong은 대략 1.8–2배 느려져야 위반. 배경 L1을 넣으면 사전 P95가 0.568 s로 여유가 거의 없어 iof Stage 0처럼 배경 겹침만으로 위반이 난다 → **이 family는 배경 L1을 쓰지 않는다**.

## 5. vLLM CPU가 trivial 신호가 되는가

- 위험 [추정]: 경합이 커지면 vLLM이 받는 CPU 시간이 줄어 컨테이너 CPU가 1.73 → 더 낮아질 수 있다. 그러면 기존 C2(컨테이너 CPU 기반 단일 규칙)가 바로 잡아내고, "다변량이 필요하다"는 주장이 약해진다.
- 반대 근거 [추정]: OMP 대기 스레드는 spin하며 CPU를 태우고, 이미 quota로 throttle 중이므로(28 %) 경합으로 잃은 시간 일부는 throttle 감소로 상쇄될 수 있다 → 컨테이너 CPU는 비슷한데 진행(token/s)만 줄어드는 패턴 가능.
- 결론: 어느 쪽인지는 측정해야만 안다. Stage 0의 핵심 관찰 항목으로 둔다(guard는 iof처럼 양쪽 envelope).

## 6. 복구 조치와의 맞음

- Blue/Green은 새 pod를 만든다. 이웃이 sj-worker2에 남아 있는데 preview도 sj-worker2에 뜨면 **복구 효과 없음**.
- 현재 preview 배치는 스케줄러에 맡겨져 있고(Stage 0 이동 때도 "배치 확인 후 진행" 방식), 경합 노드를 피하는 규칙이 없다.
- 맞추려면 이후 계약에서 "경합 노드 회피(preview에 node anti-affinity 또는 nodeAffinity)"가 필요 → **Rollout manifest 변경 = 별도 승인 사항**. Stage 0은 탐지 feasibility만 보고 복구는 다루지 않는다.
- 장점: "노드를 옮겨 피한다"는 조치는 noisy neighbor에 대한 실제 운영 대응과 의미가 정확히 맞는다(재시작으로 해결 안 되는 원인을 재배치로 해결).

## 7. 지표 목록 [확인: metadata·series 존재]

| 지표 | 출처·scrape | 범위 | 의미 |
|---|---|---|---|
| `node_pressure_cpu_waiting_seconds_total` | node-exporter 30 s | 노드 | CPU PSI some(runnable 대기 시간) |
| `node_schedstat_waiting_seconds_total`, `_running_`, `_timeslices_` | node-exporter 30 s | 노드·cpu 라벨 | 코어별 run-queue 대기·실행·timeslice |
| `node_load1`, `node_procs_running`, `node_procs_blocked`, `node_context_switches_total` | node-exporter 30 s | 노드 | 부하·실행 대기 프로세스 |
| `node_cpu_seconds_total{mode}` | node-exporter 30 s | 노드·cpu | idle/user/system/steal 등 |
| `container_cpu_usage_seconds_total` | cAdvisor(kubelet) 10 s | 컨테이너 | vLLM·이웃 CPU 사용 |
| `container_cpu_cfs_{periods,throttled_periods,throttled_seconds}_total` | cAdvisor 10 s | 컨테이너 | quota throttle |
| `container_cpu_load_average_10s` | cAdvisor 10 s | 컨테이너 | (값 확인 필요 - 많은 환경에서 0) |
| `vllm:generation_tokens_total`, `request_success_total`, `iteration_tokens_total` | vLLM 15 s | pod | 진행·완료 throughput |
| `vllm:num_requests_running/waiting` | vLLM 15 s | pod | 큐 |
| `vllm:e2e_request_latency_seconds`, `request_{decode,prefill,inference}_time_seconds` 등 | vLLM 15 s | pod | 서버측 지연 histogram |
| 없음 | - | - | `container_cpu_schedstat_run_queue_seconds_total`(컨테이너 run-queue) 부재 |

## 8. 스트레스 수단

- 1안 [계획]: 일반 CPU-burn pod. 이미지 `loadgen-runner:phase8-v3-boundaries`(Python 있음), `nodeName: sj-worker2`, busy 루프 프로세스 N개, CPU request/limit으로 가중치·상한 설정. iof의 io_writer pod 생성·해시 확인·삭제 경로를 그대로 재사용 가능.
- 2안: Chaos Mesh StressChaos(cpu stressor)를 sj-worker2에 고정된 dummy pod에 주입. chaos-daemon이 sj-worker2에 있음. 1안보다 부품이 하나 많아 우선순위 낮음.
- vLLM 컨테이너 안 stress는 쓰지 않는다(이번 후보 정의 밖, 기존 c1 시나리오와 섞임).

## 9. 단순 비교기 R과 C2

- R(주비교) [계획]: 노드 CPU 경합 단일 지표 하나의 임계값. 1순위 후보 `rate(node_pressure_cpu_waiting_seconds_total[2m])`, 대안 schedstat waiting 합. 같은 정상 오탐 예산으로 보정. 개발 자료로 하나만 고르고 동결.
- C2(보조): vLLM 컨테이너 CPU 기반 단일 규칙(기존 성격 유지, 재보정 범위는 별도 계약).
- 지연 지표 문제: vLLM 서버측 latency histogram(e2e·decode)을 R이 쓸 수 있으면 strong은 아마 단일 임계값으로 잡힌다 → 그 경우 IF2의 이점 주장은 어렵다. 두 선택지 중 하나를 **사전 고정**해야 한다:
  (a) "자원 지표만으로 사용자 지연 전에 탐지" - IF2·R 모두 latency 계열 제외, 또는
  (b) latency 포함 - R 후보에도 latency 단일 규칙 포함.

## 10. IF2가 실제로 쓸 수 있는 관계 정보 [추정]

| 상황 | 노드 PSI | vLLM CPU | token/s | 단일 PSI 규칙 |
|---|---|---|---|---|
| 정상 고부하(요청 많음) | ↑ | ↑ | ↑ | 오탐 위험 |
| weak 이웃 | ↑ | = | = | 오탐(서비스 문제 없음) |
| strong 이웃 | ↑↑ | = 또는 ↓ | ↓ | 탐지 |

IF2가 이길 수 있는 근거는 "PSI는 높은데 vLLM은 일을 덜 한다(CPU 대비 token/s, throttle 비율 감소)"라는 **관계**뿐이다. weak와 strong의 PSI 크기가 겹치지 않으면 단일 PSI 임계값으로 충분하다 → Stage 0에서 weak/strong의 PSI 분포 겹침 여부를 기술값으로 꼭 남긴다. 겹치지 않으면 IF2 주장은 약하다.

## 11. 공정성·누설

- 정상 학습 자료에 이웃 stress 구간 포함 금지, 개발 run·확증 run 분리, 강도 선택 run을 비교 평가에 재사용 금지(counted=false).
- probe 지연·SLO 판정값은 탐지 특징에서 제외(정답 누설).
- 이웃 pod 자신의 CPU(`container_cpu_usage_seconds_total{pod=noisy}`)는 **주입 표식**이므로 특징에서 제외. 노드 단위 지표는 허용(운영에서 실제로 보이는 값).
- R·IF2 동일 특징 풀, 동일 정상 오탐 예산, 동일 3-consecutive/쿨다운.
- column-shuffled IF control로 "관계"를 쓰는지 확인.

## 12. 대안 후보 (최대 2, 디스크 I/O 제외)

| 후보 | 장점 | 단점 |
|---|---|---|
| A1 메모리 대역폭·캐시 경합 이웃(stream 성격 busy pod) | decode는 메모리 대역폭에 민감 → 영향 경로 실재 | Prometheus에 대역폭·LLC 지표 없음(perf 미수집) → 관측이 vLLM 진행 지표뿐, 비교가 빈약 |
| A2 하이퍼바이저 steal | 클라우드에서 현실적 | 통제 불가(호스트 접근 없음), 현재 steal ≈ 0.001 → 재현 불가 |

## 13. 점수 (1–5, 높을수록 좋음, [추정])

| 기준 | 노이지 네이버 CPU | A1 메모리 대역폭 | A2 steal | (참고) 디스크 I/O |
|---|---|---|---|---|
| 현실성 | 5 | 4 | 4 | 4 |
| 통제·재현성 | 4 | 3 | 1 | 4 |
| weak/strong 가능성 | 4 | 2 | 1 | 1(실측 false) |
| 비자명성(IF2 여지) | 3 | 3 | - | 2 |
| 지표 관측성 | 5 | 2 | 2 | 4 |
| 복구 조치 맞음 | 3(배치 규칙 필요) | 3 | 2 | 3 |
| 일정(10/30) | 3 | 2 | 1 | - |
| 합계 | **27** | 19 | 11 | - |

## 14. Stage 0 초안 [계획, frozen 아님]

질문: 이웃 pod CPU 경합만으로 (1) SLO를 지키면서 노드 경합이 보이는 weak와 (2) pod가 살아 있는 채 공식 SLO를 위반하는 strong을 2/2 재현할 수 있는가? 그리고 그때 vLLM 컨테이너 CPU가 기준선 envelope 안에 머무는가?

- family: 새 이름(예: `nnf`), ID `nnb90`(기준선)·`nnd90–92`(개발)·`nnf90–92`(확증), seed는 config에 고정. 모두 counted=false.
- 배치: active vLLM = sj-worker2 유지(이동 없음), Prometheus = sj-worker. 이웃 pod `nodeName: sj-worker2`.
- 배경: **probe 1 RPS만**(배경 L1 없음). 공식 SLO는 동결 slo_judge v3·slo_episodes r2.
- 강도 사다리(이웃 = busy 프로세스 수 / request / limit) 예시: N1 3 / 0.5 / 3, N2 6 / 1 / 6, N3 8 / 3 / 8. 올라가는 순서, SLO 위반이면 상위 단계 시작 안 함(SLO 경계 종료). 값은 freeze 전에 노드 여유(requests 5.75 core)와 맞춰 확정.
- 준비(warm-up) 규칙(IF·R·C2와 무관): probe 시작 뒤 120 s 경과 + 최근 120 s 원본 표본 수(컨테이너 CPU ≥ 8, vLLM 생성 token ≥ 6, 노드 CPU PSI ≥ 3) + probe 연속 성공 ≥ 60 + 최근 60 s probe P95 < 0.648 s + Rollout canonical·UID 일치. probe 시작부터 600 s 안에 못 맞추면 invalid. 시작 직후 저CPU 구간을 IF가 이상으로 본 감사 F1 문제를 피하려고 탐지기 상태를 기다리지 않는다.
- 판정: weak = 이웃 정상 + 노드 CPU PSI 단계 중앙값 > 사전 최대 + SLO 정상 + vLLM CPU guard 통과 + 무결성. strong = 이웃 정상 + SLO 위반 + vLLM CPU guard 통과 + 무결성 + pod 생존. 선택·확증·feasible 규칙은 iof §7·§9 구조 그대로.
- 기술값(판정 아님): vLLM token/s, throttle 비율, schedstat waiting, CPU 0 사용률, weak/strong PSI 분포 겹침.
- 안전: 이웃 pod CPU limit 상한 8, 메모리 128Mi, 실행 시간 상한, 5 s 안전 감시(probe·pod·노드·격리), 정리 후 이웃 pod 잔여 0 확인.
- 코드: `run_iof_stage0.py`·`iof_stage0_analysis.py`를 복사해 writer → cpu-burner, I/O 변화 → CPU PSI 변화, 배경 제거만 바꾼다(새 판정 개념 없음). 테스트·Ponytail 검토·freeze manifest 뒤 승인 받고 실행.

## 15. 일정 [추정]

| 날짜 | 일 |
|---|---|
| 10/10–11 | 계약·코드 복사 수정·테스트·freeze(약 1일) |
| 10/12 | Stage 0 실행: 기준선 30분 + 개발 2–3회 × 약 30분 + 확증 2–3회 × 약 25분 ≈ 4–5시간 |
| 10/15 | 시연: 기존 확정 결과 사용. noisy-neighbor는 "진행 중 검증"으로만 언급 가능 |
| 10/13–22 | feasible=true일 때만: 정상 자료 수집(이웃 없음, 여러 부하) → IF2 학습·R 보정(동결) → 확증 비교 |
| 10/23–30 | 결과 정리·발표 |

여유가 거의 없다. Stage 0이 false면 10/12에 바로 멈추고 기존 결과로 발표를 굳힌다.

## 16. 판정

**PROCEED TO FEASIBILITY** (Stage 0만, counted=false, 승인 후).

이유:
1. vLLM CPU 백엔드는 CPU-bound이고 OMP 스레드가 코어 0–6에 고정돼 있어, 이웃 CPU 경합이 서비스 속도에 영향을 주는 경로가 I/O보다 훨씬 직접적이다.
2. 노드에 약 4.5 core 여유가 있어 "노드는 바빠 보이지만 서비스는 괜찮은 weak"과 "포화로 느려지는 strong"을 강도 하나로 나눌 수 있을 가능성이 있다.
3. 필요한 지표(CPU PSI·schedstat·throttle·token)가 이미 수집되고 있고, 스트레스 수단·실행기 골격이 이미 있다.

그대로 남는 위험(PROCEED여도 성공 보장 아님):
- vLLM 컨테이너 CPU가 같이 떨어지면 C2로 쉽게 잡혀 IF2 주장이 약해진다.
- weak/strong PSI가 겹치지 않으면 단일 PSI 규칙으로 충분하다.
- CFS 가중치 때문에 strong이 SLO 위반까지 안 갈 수 있다.
- 복구 효과를 보이려면 preview 노드 회피 규칙(manifest 변경, 별도 승인)이 필요하다.
- 10/30까지 일정이 빠듯하다.
