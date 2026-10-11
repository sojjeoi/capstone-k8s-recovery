# c1 detector - 한계와 향후 과제 (최종 정리, 2026-10-10)

> Status: **OUTDATED-SUMMARY**(2026-10-10). 확장 방향 1(c3-A)은 이후 사전등록 실행 결과 PROMISING(불필요 신호 c1 14 → 1), 확장 방향 2(AI Added Value)는 NO_GO로 정리됨 → `docs/final/FINAL-RESEARCH-SYNTHESIS-20261011.md` 참조.

> 공식 결과는 바꾸지 않았다. 아래 탐색 분석은 모두 공식 c1 artifact를 읽기만 했고 결과를 별도 폴더에 두었다. 기존 데이터에 대한 post-hoc feature/tuning 탐색은 종료한다. 새 독립 데이터 기반 c3-A와 조건부 c3-B는 별도 사전 설계 과제로 구분한다(실행 승인 아님).

## 최종 표현

> 공식 c1 model은 정상운전 중 큰 음수 memory_slope에 강한 local sensitivity를 보였다. 이는 주요 local sensitivity source를 좁힌 결과이며 오탐의 인과적 원인을 확정한 것은 아니다.
> Normal augmentation·Pod-relative memory·active-only는 모두 NOT PROMISING이었다. 기존 데이터에 대한 post-hoc tuning은 중단하고, 먼저 정상 training/calibration coverage만 바꾸는 c3-A를 설계한다. 충분한 reliability 개선이 없을 때 c3-B representation 연구를 별도로 검토한다.
> 현재 시스템 성과는 recovery mechanism의 end-to-end 검증과 A/B SLO harm reduction이다. AI 우월성은 아직 검증되지 않았다.

## A. 공식 c1 구조
- 입력: 60 s 창(15 s 격자)의 vLLM 컨테이너 cpu_mean·cpu_slope·memory_mean·memory_slope(active+preview 두 Pod 합). queue·cache는 학습에서 분산 0이라 제외.
- 모델: StandardScaler + IsolationForest(n_estimators 100, random_state 42), 학습 c1tr01–06(619 평가).
- 임계값: 보정 c1ca01–04에서 3연속 경고 0인 가장 높은 관측 점수 → −0.07667538512242333. 판정: score < threshold, 3연속, 쿨다운 60 s.
- artifact: `anomaly-detection/v3/model_v33_c1/artifacts/` (SHA256SUMS 9/9 일치, 2026-10-10 재확인).

## B. 공식 integrity audit (`docs/design/phase8-c1-detector-integrity-audit-20261009.md`)
- 계산: 원자료 → 특징 → scaler → 점수 → 임계값 → 3연속 → 신호 → T_safe를 독립 재계산, 공식 run 전 tick 일치 → **실질 오류 없음**.
- 해석 문제(F1): 시작 직후 저CPU(0.6–1.35, 학습 1.43–1.70)를 이상으로 봄 → 빠른 신호를 압박 조기 탐지로 해석할 수 없음.

## C. N1에서 확인된 불필요 anomaly

- 정상 6 세션 모두 D = T_safe(IF) − T_safe(CPU 규칙) < 0 (중앙값 −2088.5 s): B가 정상 운전에서 불필요 신호를 더 일찍 냈다.

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

## D. 보정 세트·memory baseline 분석 (`experiments/results/exploratory_c1_calibration_memory_20261010/`)
- CPU 두 특징: 보정 세트가 N1 정상 운전을 덮음(범위 안 98.6–98.7 %).
- memory_mean: N1의 16.9 %만 보정 범위 안(구간 IoU 0.04). 분산의 99.5 %가 세션 사이(비 180.6) - 세션마다 다른 preview Pod 수준과 강하게 연관되어 보임.
- 공식 임계값에서 3연속 신호를 이룬 평가는 90 % 이상 memory_slope 음수(중앙값 −269 KB, 메모리가 계단처럼 줄어드는 구간), 메모리 수준이 높은 세션(nb03·04·05)에 몰림. 세션 전체 신호 31회 중 29회가 시작 600 s 이후였다. 이는 초기 분석의 전체 세션 집계이며, 공식 판단 창의 **24회 중 LATE 22회**(§C)와 혼용하지 않는다 → 시작 직후만의 문제가 아님.
- 판정: Q1 PARTIALLY REPRESENTATIVE, Q2 HIGH, Q3 NO.

## E. 정상 학습 범위 확대 (`experiments/results/exploratory_normal_coverage_20261010/`) - NOT PROMISING
- 원 c1 학습 + N1 정상 5 세션으로 학습(6-fold leave-one-session-out), 보정 세트는 원 c1ca01–04 유지. 원 절차 재현 확인(임계값 동일, 점수 차이 0).
- 개선 1/6, 둘 다 악화 2/6. 새 임계값이 +0.03–0.06으로 올라 정상 신호가 늘었다. 압박 신호 5/5 유지.

## F. Pod-relative memory 표현 (`experiments/results/exploratory_pod_relative_memory_20261010/`) - NOT PROMISING
- memory_mean을 세션 첫 사용 가능 평가 대비 차이로 바꿈(사전 고정, 변형 탐색 없음).
- 세션 사이 분산 비는 180.6 → 2.47로 줄었지만, 불필요 신호 합 24 → 99, T_safe 같거나 개선 3/6. 정상 운전의 메모리 누적 감소(15–64 MB)가 학습 범위 밖 편차로 남았다. 압박 신호 5/5 유지.

## G. 현재까지 확인된 것

1. 공식 c1 계산 구현에 실질 오류가 없으며 정상 운전에서 불필요 anomaly signal을 반복해서 낸다.
2. c1 forensic은 큰 음수 memory_slope를 주요 local sensitivity source로 좁혔다. Coverage와 representation 중 어느 개선이 필요한지는 독립 연구로 분리한다.
3. CPU는 trigger 평가 대부분에서 학습 범위 안이고, memory_mean은 다수 범위 밖이지만 단독 score 영향은 memory_slope보다 작았다.
4. Normal augmentation 및 Pod-relative memory: **NOT PROMISING**(§E·F).
5. Active-only: **NOT PROMISING**. 불필요 trigger **24 → 70**, median T_safe **949.6 → 283.1 s**, **2/6 same·4/6 worse·0 improved**, pressure **5/5 유지**. 근거: `experiments/results/exploratory_c2_active_only_20261010/C2-ACTIVE-ONLY-RESULT-20261010.md`.
6. 세 탐색 결과에 따라 기존 데이터의 post-hoc feature/tuning 탐색을 종료한다. 새 데이터 기반 reliability 연구 전체의 종료를 뜻하지 않는다.

## H. 확인되지 않은 것
- IF가 CPU 임계값보다 우월하다는 것(B/C/K·N1 모두 근거 없음).
- IF가 장애를 미리(조기) 탐지했다는 것.
- 불필요 anomaly의 확정 원인(관찰된 연관만 있음).
- 어떤 memory 표현·보정 구성이 문제를 해결한다는 것.
- 다변량 관계 학습이 단일 지표보다 낫다는 것.

## I. 기존 데이터 post-hoc 탐색 종료와 새 연구의 경계

- Normal augmentation·Pod-relative memory·active-only가 모두 NOT PROMISING이므로 기존 데이터에 맞춘 추가 feature/tuning 탐색은 중단한다.
- 새 정상 training/calibration coverage를 갖춘 c3-A는 기존 탐색과 다른 연구다. 새 독립 정상 validation과 preregistration, 사용자 실행 승인이 필요하며 일정 내 성공을 전제하지 않는다.
- 프로젝트의 확인된 recovery mechanism end-to-end 검증 및 A/B SLO harm reduction은 detector 개선과 독립이다.

## J. 향후 실제 개선 방향 (설계 아이디어 수준)

### 확장 방향 1: Detector Reliability

- **완료:** c1 forensic으로 false-trigger의 주요 local sensitivity source를 memory_slope 중심으로 좁혔다.
- 기존 탐색은 normal augmentation **NOT PROMISING**, pod-relative memory **NOT PROMISING**, active-only **NOT PROMISING**이다. Active-only는 unnecessary trigger **24 → 70**, median T_safe **약 950 → 283 s**, **2/6 same·4/6 worse·0 improved**, pressure **5/5 유지**였다. 압박 신호 유지가 정상 신뢰성 개선을 뜻하지 않는다. **기존 데이터에 대한 post-hoc feature/tuning 탐색은 중단한다.**
- **다음 [계획]: c3-A normal-coverage detector.** c1의 4 features(`cpu_mean`, `cpu_slope`, `memory_mean`, `memory_slope`), feature logic(60 s 창·15 s 격자·active+preview 집계·입력 무결성), `StandardScaler + IsolationForest(n_estimators=100, random_state=42)`, 3-consecutive 철학을 유지한다. 바꾸는 것은 **normal training/calibration coverage 하나뿐**이다. Feature 추가·삭제, normalization/representation 변경, hyperparameter search, 결과를 본 임계값 재튜닝은 하지 않는다. 새 데이터로 scaler/model을 fit하고 같은 보정 절차를 적용하되 공식 c1 artifacts와 분리한다.
- 여러 정상 operational regime(**low/medium/high-but-SLO-normal workload**), independent sessions, **Pod/time diversity**, **startup/steady-state**, 자연스러운 **normal memory decrease/staircase decline**을 representative하게 포함한다. 원하는 slope를 만들기 위해 메모리 감소를 인위적으로 유도하거나 결과에 맞는 구간만 선택하지 않는다. SLO를 만족하는 고부하 범위가 확보되지 않으면 그 한계를 보고한다.
- **TRAIN / CALIBRATION / NORMAL VALIDATION을 session-level로 독립 분리**한다. 세션 수·길이·배정, workload 일정, 정상 판정·포함/제외 규칙, calibration 규칙, 성공/중단 기준을 preregistration에서 고정한다. Validation은 설계·fit·보정에 쓰지 않고 freeze 뒤 평가한다. **새 정상 데이터 수집과 검증은 preregistration 후 사용자 승인이 필요하다.**
- **c3-A로 충분한 reliability 개선이 안 될 때만 c3-B memory-slope robustness/representation 연구**로 넘어간다. c3-B는 memory_slope representation/sensitivity 자체를 구조적으로 바꾸는 별도 연구다. Extreme slope 영향 제한·상대 표현은 **후보 예시이며 설계 확정이 아니다**.
- N1은 이미 문제 발견/development evidence다. **c3-B의 독립 validation으로 재사용하지 않으며 새 정상 validation이 필요하다.** c3-A도 새 독립 정상 세션으로 검증한다.
- 핵심은 지금 memory_slope를 삭제·약화하는 post-hoc tuning이 아니라, 먼저 **정상 coverage 부족인지 feature representation 문제인지**를 단계적으로 분리하는 것이다.

#### c3-A 구현 결정 (2026-10-10, 오프라인 준비 - 실행 승인 아님)

- **c3-A는 normal coverage만 바꾼다.** 특징·특징 계산·scaler/IF 설정·임계값 철학·세션 수·역할·판정 기준은 사전등록(`experiments/results/c3_normal_coverage_prereg_20261010/`) 그대로다.
- **학습 행 선택(row-selection) 의미도 c1과 같게 유지한다.** 새 c3 세션의 학습 행은 공식 c1 빌더(`c1_collection.build_c1_features`, 공식 `input_capture_analysis.expected_pods`)로 만든다. 처음 구현은 live 탐지기의 기대 Pod 판정을 그대로 써서, 기존 증거 1,212창 중 65창(세션 시작 62·끝 3)에서 **expected-Pod identity evidence availability 차이에 따른 row-eligibility mismatch**가 있었다(특징 계산 불일치 아님. 판정 과정 4,543평가 불일치 0, 기록이 있는 1,147창은 모두 일치). 사용자 결정으로 동등성 조건을 완화하지 않고 공식 규칙을 적용했다. 그 결과 기존 증거에서 최종 행 집합 불일치 0이다.
- **5초 Pod identity recorder는 detector feature가 아니라 증거·무결성 층이다.** c1 수집과 같은 공식 표본기(`run_v1_detector_trial.V1NoInjectionSampler`, `with_sampler`)를 탐지기와 함께 켜고 끈다. 용도는 공식 기대 Pod 판정(행 적격성)과 무결성 기록뿐이다. 기록이 빠진 창은 c1처럼 행에서 제외한다(보간·추정 없음). 기록 누락은 세션 무효 기준이 아니다(세션 유효성과 행 적격성은 분리).
- **보정 연속 의미 = live 의미(Amendment M-1).** 보정 세션 가운데에서 빌더가 뺐지만 live는 점수화한 평가가 하나라도 있으면 그 보정 시도는 `INVALID_CALIBRATION_ATTEMPT`로 쓰지 않고 같은 역할 예비 세션으로 대체한다. 검증·압박 평가는 처음부터 live 기록 전체를 live 상태기계로 재생하므로 변경 없음.
- **c3-B(memory_slope representation 변경)는 c3-A 결과 이후에만 검토한다.**
- **리뷰 체계를 분리한다.** 정확성(행 선택·누설·보정·쓰기 경로·역할·c1 재현)은 일반 correctness review가 맡는다. 실제 `ponytail:ponytail-review`는 문서화된 범위인 과잉 설계만 검토한다(정확성 보증으로 쓰지 않음).

### 확장 방향 2: AI Added Value

- **Detector Reliability 확보 뒤 별도 preregistration**으로 진행한다. 질문은 **단일 CPU threshold로 구분이 애매한 상황에서 multivariate AI가 실제로 더 나은 판단을 하는가?**다.
- 기존 B/C/K 결과는 IF 우월성을 보여주지 못했고, calibrated threshold C가 normal quietness에서 우세했다. N1에서는 **6/6에서 B가 더 이르게 불필요 신호**를 냈다. 이 결과를 AI 우월성 근거로 바꾸지 않는다.
- Single-threshold가 이미 쉽게 구분하는 CPU-only fault를 반복해 AI 우월성을 주장하지 않는다. **다변량 관계 자체가 의미 있는 scenario**를 사전 정의한다(예: 같은 CPU 수준에서 service progress/throughput 등 다른 operational signal이 달라지는 경우). 이는 후보 개념이며 기존 workload/noisy-neighbor exploratory는 이를 확립하지 못했다. 유리한 세션·구간만 고르는 cherry-pick은 금지한다.
- CPU 단일 규칙뿐 아니라 같은 지표를 쓰는 단순 규칙과도 같은 조건·정상 오탐 예산에서 비교해, 지표 추가 효과와 AI 학습 효과를 구분한다. Scenario 관측 가능성·성공/중단 기준은 실행 전에 고정한다.
- 이 축의 새 지표 후보는 c3-A feature 변경 승인이 아니다. **Latency/SLO를 c3 detector feature로 추가하지 않는다(outcome leakage 우려).**

세부 선행 구현 참고 원칙과 설계 이력: `docs/design/phase8-aiops-next-research-direction-20261009.md` §1.

### 현재 상태와 다음 단계

1. **c1 forensic 완료** — memory_slope 중심의 주요 local sensitivity 확인(인과 확정 아님).
2. **선행 구현에서 normal dataset/validation 설계 원칙 추출** — 원문·버전·평가 조건을 확인해 적용 가능한 원칙만 기록.
3. **c3-A normal-coverage preregistration** — 한 변경 축, 세션별 TRAIN/CALIBRATION/NORMAL VALIDATION 독립 분리, 성공/중단 기준 고정. **(완료: 사전등록·오프라인 도구·c1 재현 관문·행 선택 동등성 확인. 실제 수집은 미실행)**
4. **사용자 승인 후 새 정상 데이터 수집 및 c3-A reliability 검증** — 검증 결과를 본 재튜닝 금지.
5. **실패 시 c3-B memory-slope robustness/representation 연구** — 별도 설계·preregistration·새 정상 validation 및 실행 승인.
6. **Reliability 확보 후 AI-added-value 연구** — 별도 preregistration과 사용자 승인.
7. **마지막에 demo/PPT 통합** — 확인된 결과·한계·미실행 계획을 구분한다.

이 순서는 실행 승인이 아니다. 세션 수·소요시간·달력 일정은 preregistration에서 정하며, 10/15 시연·10/30 대회까지 미완료된 연구를 성과로 표시하지 않는다.

사용자 승인 없이 실험·클러스터 접근·프로모션·Git sync/push를 수행하지 않는다. 기존 raw evidence와 official c1 artifacts는 수정하지 않는다.

