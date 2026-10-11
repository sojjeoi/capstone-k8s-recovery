# 관련 구현 설계 노트 (c3 사전등록 참고, 2026-10-10)

> 공개 GitHub 5개를 읽기 전용으로 검토(gh API·raw 파일, 복제·실행 없음). [src] = 코드·자료에서 확인, [README] = README·문서 주장만.
> 목적: c3에 쓸 **설계 원칙**만 추출. 외부 repo가 그렇게 했다는 이유로 옳다고 보지 않는다.

## 1. 요약 표

| Repository | Normal data design | Validation separation | Detector inputs | Persistence/policy | Evaluation method | 우리에게 적용할 점 | 적용하지 않을 점 |
|---|---|---|---|---|---|---|---|
| rasik987/aiops-canary-rollback | 학습 900 = 10/20/30 users × 300 [src: `datasets/phase7b/healthy_pool/healthy_pool.csv`], 08-20 하루 안 여러 묶음(약 60개씩), 5 s 간격, Minikube 실측(합성 아님) | 보정 180(08-20 19:32–20:07), 최종 검증 180(08-20 20:39–21:16), 공통 검증 180(08-24) - **시간·세션 분리**, 인접 표본 랜덤 분할 아님 [src] | 특징별 **1차원 IF 4개**(app p95·Istio p95·CPU·memory), 활성은 CPU·memory·Istio [src: `ml/train_phase7b_isolation_forests.py`] | 같은 detector 3연속 → HTTPRoute 100/0 [src: `phase8-engine/phase8_controller.py`] | IF·static 2 × 장애 3 × 3 rep, 같은 공통 정상 180에서 FP 둘 다 3/180, 탐지 9/9 둘 다, MTTR 차이 유의하지 않음 [src: `results/phase10/analysis`] | 정상을 **부하 수준별 층**으로 미리 나눔; 학습·보정·검증을 **수집 세션 단위로 시간 분리**; IF와 비교군을 **같은 정상 검증**에서 평가; 3연속 persistence | 1차원 IF(관계 학습 아님); 지연(Istio p95)을 입력에 넣는 것(우리 질문에서 outcome 누설); 표본 수 복제(900/180); 0.5 % quantile 임계값(우리는 기존 3연속 0 규칙 유지); 검증 정상이 각 5 분·총 15 분으로 짧은 점 |
| code-and-secure/Self-Healing-Deployment-Engine | 정의된 정상 자료 없음 - 첫 20개 live 표본으로 fit, 100개마다 전체 이력(이상 포함)으로 재fit [src: `ml/anomaly_detector.py`] | 없음 | 6차원(error·p99·가용성·CPU·memory·연결), 결측 → 0.0 | 고정 −0.15, **1개 표본**으로 발화, 300 s 쿨다운, 조치는 별도 규칙 | 실험·지표 NOT FOUND | Prometheus → detector → Argo Rollouts 구조, detector(신호)와 action 선택 분리 | online 자기학습(오염), 결측 0 대체, 보정 없는 임계값, persistence 없음 |
| raman-gatech/RootLens | 분석마다 사고 창 **이전에 끝나는** 기준 창 [docs] | ground truth를 런타임 밖 append-only 기록에 두고 예측 뒤에만 붙임 [src: `experiment_controller/ground_truth.py`] | request rate·error·p95, 통계 70 % + IF 30 % | 조치 기본 off, Pod 재시작만 승인+재검증 [docs] | 합성 replay 100건은 모든 ablation이 top-1 1.0(변별 없음), **live 100회는 top-1 0.04** [src: `docs/evaluation-*.json`] | ground truth·평가 라벨을 detector와 분리; 기준 창은 평가 창보다 먼저; **replay 결과를 live 증거로 주장하지 않기**; 조치 기본 off·승인 | 합성 replay 평가; 통계+IF 혼합 점수 |
| robusta-dev/robusta | 해당 없음(규칙 기반) | 해당 없음 | Alertmanager 알림 | persistence는 PrometheusRule `for`에 맡김(Robusta 자체엔 NOT FOUND), `rate_limit` 쿨다운 [src: `src/robusta/utils/rate_limiter.py`], Slack grouping, trigger와 action 블록 분리 [docs] | 해당 없음 | 신호와 조치의 분리·쿨다운은 이미 우리 guarded 정책에 있음 - 유지 | 일반 remediation 적격성 관문은 찾지 못함 |
| JK-8875/CloudPilot | ML 없음(RPS 선형 추세), 이상 = 고정 임계값 [src: `backend/app/predictor.py`, `controller.py`] | 없음 | p95·error·restart·memory | 신뢰도 < 0.35면 조치 없음, 쿨다운, 결정마다 SQLite 기록 [src] | fixed·HPA·predictive를 같은 Locust 시나리오로 1회씩, **결과 미커밋** | 매 결정(tick)과 이유를 기록하는 decision history; 같은 조건 비교 | 1회 실행, 결과 없는 비교 |

## 2. c3에 가져올 원칙 (적용 위치)

1. **정상을 미리 정의한 층으로 나눠 수집**(rasik987) → 사전등록 F절. 단 우리 workload 축은 SLO 여유가 거의 없어 probe 1 RPS 한 수준만 확정, 상위 수준은 TBD.
2. **세션·시간 단위로 학습/보정/검증 분리**(rasik987, RootLens 기준 창) → G·H절.
3. **비교 대상을 같은 정상 검증에서 평가**(rasik987) → c1과 c3를 같은 검증 세션·같은 행으로(N절).
4. **ground truth(정상 여부)를 detector와 분리**(RootLens) → 정상 판정은 SLO·probe·인프라 기록으로만, 점수와 무관(E·R절). 지연·SLO는 detector 입력에 넣지 않음.
5. **replay ≠ live**(RootLens 100 % → 4 %) → c3 결과는 새로 수집한 독립 세션에서만 판정, 기존 자료 replay 개선은 판정 근거로 쓰지 않음.
6. **결정 기록**(CloudPilot) → 이미 evidence가 평가마다 기록. 유지.

## 3. 가져오지 않을 것 (이유)

- 지연·SLO 지표를 detector 입력으로: 우리 질문에서 SLO는 결과 변수라 누설.
- 특징별 1차원 IF: 관계 학습이 아니며, c3는 c1 구조를 바꾸지 않는다.
- 표본 수 복제(900/180/180): 필요한 것은 coverage 구조. 우리 세션당 평가 수·시간 비용에 맞춰 정함(PREREG I·J절).
- quantile 임계값으로 변경: c1의 "보정 세션 3연속 0" 철학 유지(M절).
- online 재학습·결측 0 대체·1표본 발화: 오염·오탐 위험.
- 합성 replay 기반 성능 주장.

## 4. 우리 c1과의 차이

| 항목 | rasik987 | c1 | c3 계획 |
|---|---|---|---|
| 정상 층 | 부하 3 수준 | 1 수준, 한 active Pod, 이틀, 30 분 세션 | 1 수준(+TBD), 여러 active Pod·날짜, 70 분 세션 |
| 분리 | 시간 분리 세션 | 세션 분리(학습 6·보정 4) | 세션 분리 + 블록 교차 배치 + 독립 검증 |
| 정상 검증 길이 | 15 분 | 독립 정상 3 h(v3vn) + N1 6 h | 새 검증 4–6 × 70 분 |
| detector | 1차원 IF × 4 | 4특징 다변량 IF | 동일(c1 그대로) |
| 결론 수준 | IF ≈ static(정직한 null) | 압박: 동작, 정상: 불필요 신호 많음 | Detector Reliability만 |

## 5. 주의

- 외부 repo 어디에서도 "같은 특징을 쓰는 단순 규칙 대비 다변량 관계 학습의 이득"을 실측으로 검증한 예를 찾지 못했다. 이것은 AI-added-value 단계에서 따로 설계할 부분이다.
- rasik987의 IF/static 비교는 날짜가 다른 실행끼리 짝지어졌고(IF 08-21, static 08-24) 장애 실험 뒤 detector를 다시 설계했다 - 우리도 같은 함정(결과를 본 뒤 재설계, 다른 날 비교)을 피해야 한다.
