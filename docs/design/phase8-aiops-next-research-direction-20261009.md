# AIOps 다음 연구 방향 설계안 (2026-10-09 작성, 2026-10-10 갱신, 문서 전용 - 실행 승인 아님)

> 근거 문서: `AGENTS.md`, `CAPSTONE_HANDOFF_2026-10-09-N1-FINAL.md`, `phase8-c1-detector-integrity-audit-20261009.md`(F1), `phase8-aiops-v2-design-draft-20261009.md`, `phase8-aiops-v2-request-shape-axis-assessment-20261009.md`, `phase8-aiops-v2-progress-metric-feasibility-20261009.md`, `V2WL-FEASIBILITY-RESULT-20261009.md`, nb02 forensic.
> 이번에 새로 만든 근거(탐색 전용, 임계값으로 쓰지 않음): `experiments/results/exploratory_v2_progress_metric_20261009/stage0-offline-separability-estimate.txt`, `stage0-stress-stage-probe-latency.txt`.
> 최신 결과가 이전 초안보다 우선한다: v2wl feasible=false, request-shape 축 부적합, F1 해석 정정, c1 결론·자료 보존. 표기: **[확인]** 저장 자료로 확인한 사실, **[추정]** 근거 있는 예상, **[계획]** 아직 실행하지 않은 제안.

> **2026-10-10 현재 방향:** c1 forensic 완료 → c3-A normal coverage → 필요 시 c3-B → reliability 확보 후 AI Added Value. 아래 §2–5의 기존 workload/I/O 분석·실험안은 검토 이력으로 보존하며 현재 실행 계획이 아니다. 최신 진행 순서는 §6, 승인 경계는 §10을 따른다.

## 0. 쉬운 설명 (무엇을, 왜 다르게)

먼저 정상 운전에서 불필요 신호를 줄일 수 있는지 확인하고, 그 신뢰성이 확보된 뒤 AI가 단순 CPU 규칙보다 추가 가치를 주는지 따로 검증한다. 지금은 memory_slope를 바로 약화하는 단계가 아니라, 정상 학습·보정 coverage만 바꾸는 c3-A를 사전 설계할 단계다.

현재 확인된 시스템 성과는 **recovery mechanism의 end-to-end 검증과 A/B SLO harm reduction**이다. 미래 장애 예측·사전 감지·AI 정확도 우월성을 성과로 주장하지 않는다.

## 1. 연구 질문 (고정)

### 확장 방향 1: Detector Reliability

- **완료:** c1 forensic으로 false-trigger의 주요 local sensitivity source를 memory_slope 중심으로 좁혔다.
- 기존 탐색은 normal augmentation **NOT PROMISING**, pod-relative memory **NOT PROMISING**, active-only **NOT PROMISING**이다. Active-only는 unnecessary trigger **24 → 70**, median T_safe **약 950 → 283 s**, **2/6 same·4/6 worse·0 improved**, pressure **5/5 유지**였다. 압박 신호 유지가 정상 신뢰성 개선을 뜻하지 않는다. **기존 데이터에 대한 post-hoc feature/tuning 탐색은 중단한다.**
- **다음 [계획]: c3-A normal-coverage detector.** c1의 4 features(`cpu_mean`, `cpu_slope`, `memory_mean`, `memory_slope`), feature logic(60 s 창·15 s 격자·active+preview 집계·입력 무결성), `StandardScaler + IsolationForest(n_estimators=100, random_state=42)`, 3-consecutive 철학을 유지한다. 바꾸는 것은 **normal training/calibration coverage 하나뿐**이다. Feature 추가·삭제, normalization/representation 변경, hyperparameter search, 결과를 본 임계값 재튜닝은 하지 않는다. 새 데이터로 scaler/model을 fit하고 같은 보정 절차를 적용하되 공식 c1 artifacts와 분리한다.
- 여러 정상 operational regime(**low/medium/high-but-SLO-normal workload**), independent sessions, **Pod/time diversity**, **startup/steady-state**, 자연스러운 **normal memory decrease/staircase decline**을 representative하게 포함한다. 원하는 slope를 만들기 위해 메모리 감소를 인위적으로 유도하거나 결과에 맞는 구간만 선택하지 않는다. SLO를 만족하는 고부하 범위가 확보되지 않으면 그 한계를 보고한다.
- **TRAIN / CALIBRATION / NORMAL VALIDATION을 session-level로 독립 분리**한다. 세션 수·길이·배정, workload 일정, 정상 판정·포함/제외 규칙, calibration 규칙, 성공/중단 기준을 preregistration에서 고정한다. Validation은 설계·fit·보정에 쓰지 않고 freeze 뒤 평가한다. **새 정상 데이터 수집과 검증은 preregistration 후 사용자 승인이 필요하다.**
- **c3-A로 충분한 reliability 개선이 안 될 때만 c3-B memory-slope robustness/representation 연구**로 넘어간다. c3-B는 memory_slope representation/sensitivity 자체를 구조적으로 바꾸는 별도 연구다. Extreme slope 영향 제한·상대 표현은 **후보 예시이며 설계 확정이 아니다**.
- N1은 이미 문제 발견/development evidence다. **c3-B의 독립 validation으로 재사용하지 않으며 새 정상 validation이 필요하다.** c3-A도 새 독립 정상 세션으로 검증한다.
- 핵심은 지금 memory_slope를 삭제·약화하는 post-hoc tuning이 아니라, 먼저 **정상 coverage 부족인지 feature representation 문제인지**를 단계적으로 분리하는 것이다.

### 확장 방향 2: AI Added Value

- **Detector Reliability 확보 뒤 별도 preregistration**으로 진행한다. 질문은 **단일 CPU threshold로 구분이 애매한 상황에서 multivariate AI가 실제로 더 나은 판단을 하는가?**다.
- 기존 B/C/K 결과는 IF 우월성을 보여주지 못했고, calibrated threshold C가 normal quietness에서 우세했다. N1에서는 **6/6에서 B가 더 이르게 불필요 신호**를 냈다. 이 결과를 AI 우월성 근거로 바꾸지 않는다.
- Single-threshold가 이미 쉽게 구분하는 CPU-only fault를 반복해 AI 우월성을 주장하지 않는다. **다변량 관계 자체가 의미 있는 scenario**를 사전 정의한다(예: 같은 CPU 수준에서 service progress/throughput 등 다른 operational signal이 달라지는 경우). 이는 후보 개념이며 기존 workload/noisy-neighbor exploratory는 이를 확립하지 못했다. 유리한 세션·구간만 고르는 cherry-pick은 금지한다.
- CPU 단일 규칙뿐 아니라 같은 지표를 쓰는 단순 규칙과도 같은 조건·정상 오탐 예산에서 비교해, 지표 추가 효과와 AI 학습 효과를 구분한다. Scenario 관측 가능성·성공/중단 기준은 실행 전에 고정한다.
- 이 축의 새 지표 후보는 c3-A feature 변경 승인이 아니다. **Latency/SLO를 c3 detector feature로 추가하지 않는다(outcome leakage 우려).**

### c1 false-trigger forensic 결론 [완료]

**[확인]** N1 official normal 6세션의 공식 판단 창 false trigger는 **24개**이며, **22개가 600 s 이후 LATE**다. 24 event를 구성한 72 trigger evaluation 중 **68개에서 memory_slope가 음수**였다. 세션 전체 31개와 공식 판단 창 24개는 집계 범위가 다르며 혼용하지 않는다.

| training mean 수준으로 neutralize한 특징 | 3-consecutive condition이 깨진 event |
|---|---|
| memory_slope 단독 | **23/24** |
| memory_mean + memory_slope pair | **24/24** |
| CPU pair | **8/24** |

- trigger evaluation의 memory_slope median은 약 **−226 KB/s**, training std는 약 **42.6 KB/s**, 표준화 위치는 약 **−5.3σ**다.
- memory_mean은 trigger 평가 69/72에서 학습 범위 밖이지만 단독 neutralization의 score 영향은 memory_slope보다 작았다(Δscore median +0.018 vs +0.154; memory_mean 단독 event 깨짐 12/24). CPU는 대부분 학습 범위 안이었다.
- EARLY 2건은 **nb01 startup low CPU**, **nb03 startup large memory drop**으로 구분한다.
- 허용 해석: **공식 c1 model은 정상운전 중 큰 음수 memory_slope에 강한 local sensitivity를 보였다.** memory_slope를 주요 local sensitivity source로 좁힌 결과이며 causal proof나 운영상 메모리 감소 원인 확정이 아니다. Neutralization은 국소 반응 분석으로, 이후 재발화까지 줄어든다는 검증이 아니다.

근거: `experiments/results/exploratory_c1_false_trigger_forensics_20261010/C1-FALSE-TRIGGER-FORENSICS-RESULT-20261010.md`(설명용 탐색; 공식 c1 결과 변경 없음).

### GitHub 선행 구현 참고 방향 [계획]

아래는 사용자 제공 선행 구현 요약에 따른 **검토 대상과 추출할 원칙**이다. 이번 문서 수정에서 외부 저장소의 최신 구현을 재검증한 결과는 아니며, 적용 전 원문·버전·평가 조건과 provenance를 확인한다.

| 참고 대상 | 설계에 참고할 원칙 |
|---|---|
| `rasik987/aiops-canary-rollback` — 가장 유사한 참고 대상 | Healthy training을 10/20/30 users로 층화, 별도 calibration, untouched healthy validation. IF가 static threshold를 자동으로 이기지 않았다는 결과도 함께 검토한다. 사용자 수를 그대로 이 환경에 복사하지 않는다. |
| `code-and-secure/Self-Healing-Deployment-Engine` | Detector → Argo Rollouts 연결 구조와 복구 안전 |
| RootLens | Evidence provenance, 평가, 안전한 remediation |
| Robusta | Persistence, cooldown, grouping, remediation eligibility |
| CloudPilot | Same-condition baseline vs ML 비교 |

외부 구현을 그대로 복사하지 않고 **dataset design/evaluation principle**만 참고한다. 복구 안전 사례는 설계 검토용이며 정책 변경·프로모션 승인이 아니다. Latency/SLO는 outcome 평가와 정상 regime 정의에만 쓰고 c3 detector feature로 추가하지 않는다.

## 2. 우선 후보(CPU–처리량 관계)로 이 환경에서 질문을 검증할 수 있는가 → **아니다**

| 점검 | 결과 |
|---|---|
| 정상 부하 변화에서 CPU와 처리량이 함께 오르나 | **[확인]** 예. v2wl L0→L3: CPU 1.63→2.07 core, 생성 token 1.00→2.02/s(두 rep 일관) |
| 저하에서 그 관계가 달라지나 | **[확인]** 예. A/B 압박 5/5: CPU 1.6→3.0, token 1.00/s 고정 |
| 그 차이가 SLO 위반 전에 보이나 | **[확인]** 예. 그러나 CPU 단독도 같은 때 보인다 |
| CPU 단일 임계값으로도 충분한 상황인가 | **[확인] 그렇다.** SLO 정상 부하(L0–L3)로 만든 CPU 경계(1.75–2.07)와 관계 규칙 경계를 압박 궤적에 적용하면 5/5 run에서 **같은 평가 시점**(주입 뒤 첫 완전한 창, t_slo 전 28–116 s)에 발화 - 관계 정보의 추가 이득 없음 |
| 차이가 압박 도구 때문은 아닌가 | **[확인] 그렇다.** StressChaos 압박기가 vllm 컨테이너 CPU에 포함되어 "CPU↑ + 처리량 고정"이 구성상 생긴다. 가장 약한 압박(1 worker)에서도 CPU 2.2–2.4(정상 범위 위)인데 probe P95는 0.32→0.38–0.46 s로 SLO 위반 없음 → **"CPU는 정상 범위인데 서비스가 나빠지는" 구간이 이 압박 방식에는 없다** |
| 정상 고부하를 늘리면 겹침이 생기나 | **[확인]** 아니다. CPU 2.1 이상의 정상 부하는 frozen SLO를 넘는다(v2wl feasible=false, request-shape 축 부적합: 엔진 점유 = probe 지연 결합) |

결론: 컨테이너 내부 CPU 압박 + 이 SLO에서는 CPU가 저하를 이미 잘 구분하므로 다변량 관계의 가치를 시험할 상황이 없다. 반복을 늘려도 동률만 쌓인다(**[추정]**, 위 오프라인 결과 근거). 관측된 관계를 모든 LLM 장애의 성질로 일반화하지 않는다.

## 3. 대안 하나: 노드 디스크 I/O 경합으로 인한 서비스 저하 (기존안·보류, 현재 실행 계획 아님)

**근거 [확인].**
- nb02(공식 SLO 위반)와 bc03b(승격 뒤 약 6 s 정지): 정지 시각에 B·C·K 조건 모두 false(cpu_mean 1.50–1.62), active 노드 CPU busy는 오히려 감소(3.7→3.1 core), 같은 노드 iowait 0.05→0.72 core, io PSI 0.006→0.128, 디스크 쓰기 지연 상승. 메모리 PSI·steal·네트워크 drop은 0.
- 같은 날 2 MB/s 이상 쓰기 급증은 여러 번 있었지만(상당수 preview 생성, Prometheus compaction) 대부분 정지가 없었다 → **정상에도 I/O 급증이 있다**. "I/O가 높으면 이상"이라는 단일 규칙은 불필요 신호를 낼 수 있고, "I/O가 높아도 처리가 평소처럼 진행되면 정상, 처리가 밀리면 저하"라는 **관계**가 의미를 가질 수 있다 - 원래 질문의 구조와 맞다.
- 필요한 지표가 이미 수집 중: `node_pressure_io_waiting_seconds_total`, `node_cpu_seconds_total{mode="iowait"}`, `node_disk_io_time_seconds_total`, `node_disk_write_time_seconds_total`(node-exporter, metadata 확인), `vllm:generation_tokens_total`, `container_cpu_usage_seconds_total`. 새 계측 불필요.

**한계 [확인/추정].** 정지 원인은 미확정(E) - 디스크 포화와 동시였을 뿐 인과는 미입증. 급증이 있어도 대부분 정지하지 않았다 → 통제 주입으로 재현되지 않을 수 있다(가장 큰 위험). 현재 active vLLM과 Prometheus가 같은 노드(sj-worker)라, 그 노드에 디스크 부하를 주면 측정 체계가 흔들릴 수 있다. 결과는 "노드 디스크 I/O 경합" 한 유형에만 해당한다.

## 4. 단계와 진행·중단 조건 (기존안·보류, 현재 실행 계획 아님)

### 단계 1 - 관측 가능성 확인(가능성 시험, counted=false, 약 1일) [당시 계획·보류]

질문: 통제된 디스크 쓰기 부하가 (a) 공식 SLO 저하를 만들고, (b) 그 동안 vLLM CPU가 정상 범위에 머물며, (c) I/O·처리 진행 지표가 t_slo 전에 변하는가? (d) 같은 장치의 약한 부하는 SLO를 해치지 않는가(정상 대조)?

- 주입: active vLLM 노드에 고정한 임시 Pod 하나가 emptyDir(노드 디스크)에 순차 쓰기(기존 loadgen 이미지의 dd 또는 python). 강도 사다리(쓰기 속도 3단계)는 단계 1 계약에서 노드 디스크 사양으로 실행 전 고정하고 결과를 보고 바꾸지 않는다. 탐지기는 record_only 섀도(신호 없음, 전환 없음).
- 배경 workload: probe 1 RPS + L1(0.025 RPS, jittered_grid_v1, max_tokens 10) - 두 rep 모두 SLO 정상이었던 수준.
- 반복: 유효 2(최대 3).
- **GO 조건(모두):** ① 어느 강도에서 공식 SLO 위반이 유효 rep 2/2에서 발생 ② 그 강도에서 위반 전 60 s 동안 vLLM cpu_mean이 단계 1 주입 전 관찰 구간의 범위 안에 머문 rep가 2/2 ③ 위반을 만들지 않은 약한 강도가 하나 이상 존재(정상 I/O 대조로 사용) ④ 측정 무결성: Prometheus scrape 공백 0, 기대 Pod·신선도 통과율 ≥ 95 %.
- **NO-GO:** 하나라도 실패 → 단계 2로 가지 않는다. §8 종료안.

### 단계 2 - 최소 비교 설계·구현·freeze(약 2일) [계획]

비교군은 같은 평가 tick·같은 입력 유효성·같은 3연속·쿨다운 60 s를 쓰고, 하나의 평가 기록에서 동시에 판정한다(실험 수 증가 없음).

| 비교군 | 입력 | 판정 | 역할 |
|---|---|---|---|
| C2 | vLLM active Pod cpu_mean | cpu_mean > T_C2 | 단일 CPU 규칙(새 정상 자료로 재보정 - 기존 C 재사용 금지) |
| R | 노드 io PSI rate | io_psi > T_R | "지표 추가만"의 효과 |
| IF2 | cpu_mean, 생성 token rate, io PSI rate, iowait rate(각 60 s 창 평균) | IF score < T_IF | 정상 관계 학습의 추가 가치 |

- R 선택 근거: nb02·bc03b에서 정지와 함께 가장 크게 오른 지표가 io PSI였고, 비율 규칙(CPU/처리량)은 §2에서 추가 정보가 없었다. R은 IF2와 같은 추가 지표 계열을 쓰지만 처리 진행과의 관계는 보지 않는다 → IF2와 R의 차이가 "관계 학습"의 몫이다.
- 전처리 공개: R은 원값 임계값, IF2는 학습 정상 자료의 StandardScaler + IF(현행 파라미터·random_state 고정). 비율을 쓰지 않으므로 분모 0 처리 불필요. 지연·SLO 값·주입 여부는 입력에 넣지 않는다.
- **관계 학습 확인(사전 등록):** 학습 자료의 각 열을 독립적으로 섞어(주변분포 유지, 결합 관계 제거) 같은 파라미터로 학습한 IF2-perm을 오프라인으로 같은 검증 기록에 적용한다. IF2와 IF2-perm의 탐지·불필요 신호가 같으면 "관계 학습의 증거 없음"으로 보고한다.
- 계산 경로: c1에서 감사로 검증된 입력 무결성 구조(기대 Pod·UID·전체 격자·Pod별 신선도·skip 리셋)를 재사용하되 active Pod 단일 기준(record_only, preview 없음). 감사 빈틈 보완: 계산 경로 파일 전부 manifest에 포함, 저장 원문 재생 테스트 필수.

### 단계 3 - 정상 학습·보정(약 2일, 세션 단위 분리) [계획]

- 정상 세션(각 40 min): probe + 배경 L0/L1 고정 일정(L0 10분 → L1 15분 → L0 5분 → L1 10분) + **정상 I/O 사건**(단계 1에서 SLO를 해치지 않은 약한 강도, 고정 시각 2회 각 3분). 정상 부하·정상 I/O·부하 전환을 모두 학습·보정에 포함한다.
- 학습 4세션(`v2n-tr01..04`), 보정 3세션(`v2n-ca01..03`). 기존 c1·v2wl·N1·A/B·B/C/K·탐색 자료 재사용 금지, 탐색에서 본 숫자(1.70, 2.14, 0.128 등)를 임계값으로 쓰지 않는다.
- 공통 정상 보정 기준: 보정 세션 전체에서 3연속 발화 0회가 되는 가장 민감한 임계값을 C2·R·IF2에 같은 함수로 계산(감사에서 C·IF에 대해 닫힌 형식 재현을 확인한 규칙).

### 단계 4 - 독립 비교(약 2.5일) [계획]

- 정상 검증 4세션(`v2n-va01..04`, 각 60 min, 학습과 같은 일정 구조, 정상 I/O 사건 시각만 다른 고정값).
- 저하 검증 유효 6쌍(최대 9쌍, `v2d-va01..`): 한 쌍 = 같은 배경 workload·같은 주입 Pod·같은 시간 구조에서 **약한 강도(정상 대조)** 세션과 **저하 강도** 세션. 요청 형태·양은 같고 디스크 쓰기 강도만 다르다 → 모델이 "요청이 달라서" 구분할 수 없다.
- 판정 창은 §5로 사전 고정하고, CPU가 겹치는 구간만 골라 쓰지 않는다.

### 단계 5 - 실제 전환 검증

이번 일정에서는 하지 않는다(복구 경로는 A/B로 검증됨). 단계 4가 성공 기준을 통과한 경우에만 별도 계약으로 제안.

## 5. 판정 기준 (결과 보기 전 고정) (기존안·보류, 현재 실행 계획 아님)

**시작 구간(모델 판정과 독립):** 준비 조건 = probe 가동 ≥ 180 s, 배경 workload 현재 단계 ≥ 120 s, 완전한 입력 창 4회 연속, 정상 I/O 사건 종료 뒤 ≥ 120 s. 최대 대기 600 s - 넘으면 세션 invalid(인프라). 준비 전 신호는 `warmup_signal`로 따로 기록하고 어떤 지표에도 넣지 않는다. IF가 정상 판정을 낼 때까지 기다리는 규칙은 쓰지 않는다.

**저하 세션 시간 구조:** 준비 → 주입 전 관찰 300 s → 주입 시작 t_inj → 판정 창 [t_inj, t_inj + 600 s) → 추적 300 s.
- 주입 전 관찰 구간의 신호 = 불필요 신호(정상 쪽 집계).
- 탐지로 인정: 발화한 연속의 **첫 이상 tick 창 시작 ≥ t_inj**. 주입 전부터 이어진 연속은 탐지로 세지 않고 `carryover`로 기록.
- 유효 탐지 = t_slo 이전 인정 탐지. lead = t_slo − t_signal. 무발화·t_slo 이후 발화는 t_signal = min(실제 발화, 창 끝)으로 음수 lead 허용(벌점).
- 저하 강도 세션의 창 안에 공식 SLO 위반이 없으면 "SLO 저하 미발생" - lead 통계에서 제외하고 횟수만 보고(유효 쌍 수에 넣지 않음).

**정상 쪽 지표:** 세션별 불필요 신호 수, 첫 불필요 신호까지 시간(N1 T_safe 정의).

**주목표(사전 선택): "불필요 신호를 늘리지 않으면서 저하 탐지율·적시성 개선".** 이유: 보정을 같은 정상 예산(3연속 0회)으로 맞추므로 남는 자유 변수는 저하 쪽 성능이다. 대안 저하는 CPU로 잘 안 보이는 유형이라 C2 대비 차이는 구성상 클 수 있으므로, 의미 있는 주비교는 **IF2 vs R**(관계 학습의 몫)로 둔다.
- Gate 1(필수): 정상 검증에서 불필요 신호가 난 세션 수 IF2 ≤ R 그리고 IF2 ≤ C2.
- Primary: 유효 저하 쌍에서 D_lead = lead(IF2) − lead(R). 6/6 > 0이고 HL > 0이면 "IF2 우위", 차이가 1 tick 안이면 "동등", 음수 다수면 "R 우위". 정확 Wilcoxon·sign은 보조(n = 6 최소 p 0.03125).
- 보조: 탐지율, IF2 vs C2, IF2 vs IF2-perm.
- 포장 금지: 더 빠르지만 Gate 1 실패, 더 조용하지만 탐지율 하락은 실패로 보고한다.

**입력·반복·중단:** 불완전 입력 tick은 세 규칙 모두 연속 0. 판정 창 skip 비율 > 10 % → 세션 invalid. 연속 invalid 2회 → 일시 중지·보고. 정리·canonical 실패 → 즉시 중단. 저하 최대 9쌍, 정상 검증 최대 6시도.

## 6. 현재 상태와 다음 단계 (2026-10-10, 미실행 단계는 [계획])

1. **c1 forensic 완료** — memory_slope 중심의 주요 local sensitivity 확인(인과 확정 아님).
2. **선행 구현에서 normal dataset/validation 설계 원칙 추출** — 원문·버전·평가 조건을 확인해 적용 가능한 원칙만 기록.
3. **c3-A normal-coverage preregistration** — 한 변경 축, 세션별 TRAIN/CALIBRATION/NORMAL VALIDATION 독립 분리, 성공/중단 기준 고정.
4. **사용자 승인 후 새 정상 데이터 수집 및 c3-A reliability 검증** — 검증 결과를 본 재튜닝 금지.
5. **실패 시 c3-B memory-slope robustness/representation 연구** — 별도 설계·preregistration·새 정상 validation 및 실행 승인.
6. **Reliability 확보 후 AI-added-value 연구** — 별도 preregistration과 사용자 승인.
7. **마지막에 demo/PPT 통합** — 확인된 결과·한계·미실행 계획을 구분한다.

이 순서는 실행 승인이 아니다. 세션 수·소요시간·달력 일정은 preregistration에서 정하며, 10/15 시연·10/30 대회까지 미완료된 연구를 성과로 표시하지 않는다.

## 7. 10/15 시연 범위

확정 성과는 recovery mechanism의 end-to-end 검증과 A/B SLO harm reduction이다. B/C/K의 c1 비우월, N1의 정상 불필요 신호, c1 forensic의 local sensitivity, 기존 개선 탐색의 NOT PROMISING 결과를 함께 제시한다. c3-A·c3-B·AI Added Value는 검증 전까지 계획으로만 표시한다. 최종 demo/PPT 통합은 §6 순서를 따르며, 완료되지 않은 새 연구의 성공을 전제로 구성하지 않는다.

## 8. NO-GO 시 종료안 (질문은 바꾸지 않음)

c3-A가 사전 고정 reliability 기준을 충족하지 못하면 실패 결과를 보존하고 c3-B의 별도 설계 여부를 검토한다. c3-B도 신뢰성을 확보하지 못하면 AI Added Value 비교로 넘어가지 않는다. Reliability를 확보하더라도 다변량 관계가 의미 있는 scenario를 확립하지 못하면 AI Added Value는 미검증으로 보고한다. 기존 데이터에 맞춘 반복 tuning이나 유리한 구간 선택으로 NO-GO를 뒤집지 않는다. 일정 내 미완료 시 확인된 복구 성과와 한계를 발표한다.

## 9. 한계

- Neutralization은 local sensitivity 분석이며 causal proof가 아니다. Coverage 부족과 representation 문제 중 어느 쪽을 바꾸면 해결되는지는 아직 확인되지 않았다.
- 기존 정상 확대 탐색은 원 보정 자료를 유지한 post-hoc 탐색으로, 새 독립 training/calibration/validation을 갖춘 c3-A와 구분한다. NOT PROMISING을 c3-A 성공 가능성이나 실패 확정으로 일반화하지 않는다.
- N1은 development evidence가 되었으므로 새 detector의 독립 validation으로 재사용하지 않는다.
- 기존 workload/noisy-neighbor 탐색은 AI Added Value scenario를 확립하지 못했다. §2–5는 그 배경과 당시 가설·설계를 남긴 이력이다.
- 정상 regime의 SLO 여유, Pod/time 다양성, 표본 수와 일정 제약은 preregistration에서 명시하고 결과의 일반화 범위를 제한한다.

## 10. 실행 전 결정 사항 (최소)

1. c3-A preregistration에서 정상 regime·세션 배정·길이·calibration·비교 기준·성공/중단 기준을 확정한다. 이 문서는 preregistration 자체나 실행 승인을 대신하지 않는다.
2. 새 정상 데이터 수집·검증, 실험, 클러스터 접근, 프로모션, Git sync/push는 **사용자의 명시적 승인 후에만** 수행한다.
3. 기존 raw evidence와 official c1 artifacts·결과는 수정하지 않는다. 새 연구 산출물은 별도 경로와 provenance로 분리한다.
4. c3-B 및 AI Added Value는 각 진입 조건을 충족한 뒤 별도 preregistration과 실행 승인을 받는다. 기존 §3–5의 I/O안은 승인 요청이나 자동 재개 대상으로 취급하지 않는다.

