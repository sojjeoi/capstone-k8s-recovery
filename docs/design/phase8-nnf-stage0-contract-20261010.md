# noisy-neighbor CPU Stage 0 feasibility 계약 v1 (counted=false, 2026-10-10)

> 상위: `phase8-aiops-noisy-neighbor-feasibility-assessment-20261010.md`(PROCEED TO FEASIBILITY), 사용자 승인 2026-10-10.
> 새 research family `nnf`. c1·A/B·B/C/K·N1·감사(F1)·v2wl·request-shape·progress·iof 결과는 그대로 보존하고 섞지 않는다.
> 이 단계는 IF2 학습·보정·검증이 아니다. 모든 run은 counted=false이고 이후 IF2 학습·보정·검증에 재사용하지 않는다.

## 1. 질문과 성공 기준

weak 정상 경합과 strong 저하(slow-but-alive)를 만들 수 있으면서, 그 둘이 **단일 지표 하나로 너무 쉽게 갈리지는 않는가?**

| 항목 | 정의(확증 rep 기준, 각 종류 처음 두 유효 rep) |
|---|---|
| W weak 존재 | weak 확증 2/2가 weak 상태(§5) |
| C1 strong slow-but-alive | strong 확증 2/2가 strong 상태(§5), 그리고 어떤 strong 시도에서도 안전 감시 중단(재시작·OOM·노드 NotReady 등) 없음 |
| C2 vLLM CPU 비자명 | 단일 지표 D(vLLM 컨테이너 CPU)가 trivial separator(§8)가 아님 - C5의 일부, 따로 보고 |
| C3 측정 무결성 | weak·strong 유효 rep 각 2개 + 확증 시도 중 측정 사유(무결성 간격·probe/Prometheus 부하 노드·probe 노드 미상·target UID 변화)로 invalid된 시도 0 |
| C4 strong 재현 | strong 확증 처음 두 유효 rep 2/2 공식 SLO 위반 |
| C5 trivial 단일 지표 없음 | 사전 고정 단일 지표 규칙(§7) 중 trivial separator(§8)가 하나도 없음 |

- **SCENARIO_FEASIBLE** = W ∧ C1 ∧ C3 ∧ C4. **AIOPS_JUSTIFIED** = SCENARIO_FEASIBLE ∧ C5.
- 1–4 PASS·5 FAIL = "noisy-neighbor 저하는 재현 가능하지만 사전 고정 단일 경합 지표 규칙만으로 weak와 strong을 충분히 구분" → AIOps 우월성 주 실험으로는 추가 가치 제한. IF2를 만들어 억지 비교하지 않고, 새 stress scenario를 자동 탐색하지 않는다.
- C5 PASS는 IF2 우월성 증명이 아니다("단일 지표 하나로 너무 쉽게 풀리는 문제는 아니다"는 feasibility 통과일 뿐). IF2로 넘어가려면 사용자 승인 필요.
- 지연·SLO(probe 지연, vLLM 지연, TTFT, 공식 SLO)는 t_slo·라벨·결과 확인에만 쓴다. 단일 지표 규칙과 향후 IF2 입력에 넣지 않는다.

## 2. 확인된 환경(읽기 전용)

평가 문서 §2 참조. active vLLM `vllm-serving-7976ddcdb6-56xmz`(sj-worker2, Burstable req 2 / lim 3, OMP 7 스레드 CPU 0–6 고정), Prometheus sj-worker, 노드 8 vCPU, sj-worker2 요청 2250m. sj-worker 요청 550m.

## 3. 측정 plane 분리

- sj-worker2: active vLLM + 부하 pod만. 다른 노드: probe client pod, Prometheus.
- 사전검사: active 노드 = sj-worker2, Prometheus 노드 ≠ sj-worker2(아니면 시작 안 함).
- 세션 중: 부하 pod를 probe보다 먼저(유휴 `sleep`) 올린 뒤 probe를 시작하고, probe pod가 정확히 1개이고 sj-worker2가 아닌지 확인한다. 아니면 TrialInvalid(재시도 루프 없음). 관찰자도 probe pod 노드를 기록하고 사후 관문이 다시 확인한다.

## 4. 세션 구조·준비 규칙

요청 workload: 동결 probe 1 RPS만(배경 L1 없음). 순서: 사전검사 → 부하 pod 준비(유휴) → probe 시작·배치 확인 → 동결 SLO 기준선 준비 → 최소 120 s → **준비 조건** → 관찰 시작(obs0) → 종류별 구간 → 정리.

준비 조건(IF·R·C2와 독립, 15 s마다, probe 시작부터 600 s 상한 - 넘으면 invalid):
1. Rollout canonical(세션 pod 잔여 항목만 제외), active UID = 사전검사 UID
2. 최근 120 s 원본 표본 수: 컨테이너 CPU ≥ 8, vLLM 생성 token ≥ 6, sj-worker2 CPU PSI ≥ 3

## 5. 세션 종류·강도·상태 정의

| 종류 | ID | 구간(obs0 뒤) |
|---|---|---|
| 기준선 | nnb90(필요 시 nnb91) | 1200 s, 부하 없음 |
| 개발 | nnd90–92(유효 2, 최대 3) | 사전 300 s → W2 300 s → 회복 120 s → W4 → … → W12 (SLO 경계 종료) |
| 확증 weak | nnw90–92(유효 2, 최대 3) | 사전 300 s → weak 300 s → 꼬리 240 s |
| 확증 strong | nns90–92(유효 2, 최대 3) | 사전 300 s → strong 300 s → 꼬리 240 s |

- 부하 도구: `chaos/loadgen/cpu_burner.py`(busy 프로세스 N개)를 `nodeName: sj-worker2` pod(이미지 loadgen-runner:phase8-v3-boundaries)에서 실행. pod 안 파일 해시 대조, 노드 확인, 초 단위 로그 보존.
- **강도 축 하나 = workers.** 고정: CPU request 2(vLLM과 같은 CFS 가중치), CPU limit 8, 메모리 limit 256Mi. 개발 사다리 W2·W4·W6·W8·W12. 다른 값·축 없음.
- **부하 적용**: 종료 코드 0, 모든 worker가 살아 있던 초 ≥ 95 % × 300, 단계 중 부하 pod CPU(60 s 창, 15 s 간격) 중앙값 ≥ 0.4 × min(workers, 8). 부하 pod CPU는 이 확인에만 쓴다.
- **경합 변화**: sj-worker2 CPU PSI waiting rate(120 s 창, 30 s 간격) 단계 중앙값 > 같은 세션 사전 구간 최댓값.
- **공식 SLO**: 동결 slo_episodes r2·slo_judge v3, 단계 [시작, 끝]과 겹치는 위반 구간 = 위반(v2wl `stage_slo_verdict`, a0 = obs0). t_slo = 단계 시작 이후 첫 위반 구간 시작.
- **weak 상태** = 부하 적용 + 경합 변화 + SLO 정상 + 단계 무결성. 요청 실패는 SLO availability 조건 안에서만 허용.
- **strong 상태** = 부하 적용 + SLO 위반 + 단계 무결성 (+ pod 생존: 안전 감시가 보장, 위반 시 세션 aborted = C1 위반 기록).
- **측정 무결성**: 지표별 최대 원본 표본 간격 ≤ 2.5 × 명목 scrape(cAdvisor 10 s, vLLM 15 s, node-exporter 30 s), target UID 불변, probe·Prometheus 부하 노드 밖, 사전검사 시계 오차 ≤ 0.5 s, probe 판정 가능, 부하 시작·끝 기록.

## 6. 개발 → 확증 (분리)

- 선택 규칙(기계): weak = 모든 유효 개발 rep에서 weak 상태인 가장 높은 강도, strong = 모든 유효 개발 rep에서 strong 상태인 가장 낮은 강도이면서 weak보다 높은 것. 유효 rep 2개 필요.
- 둘 중 하나라도 없으면 확증 없이 **SCENARIO_FEASIBLE = false**. 강도를 더 만들거나 바꾸지 않는다.
- 고른 강도와 §7 임계값(분석기 `calibration_preview` 값 그대로)을 `nnf-stage0.yaml`의 `stress.confirmatory`·`single_metric_rules`에 **amendment**(freeze manifest v1.1, 버전 `nnf-stage0-v1.1`, 코드 동일)로 고정한 뒤 확증 실행. 실행기는 weak < strong 강도·6개 지표의 non-null 임계값이 없으면 nnw·nns를 거부한다. 분석기는 고정 규칙이 개발 자료 보정값과 다르거나 확증 run의 config 해시가 서로 다르면 판정을 막는다(amendment_guard).

## 7. 사전 등록 단일 지표 규칙 (진단용 비교기, IF2 아님)

| ID | exact 지표(sj-worker2 / active vLLM 컨테이너) | 값 | 창/간격 | 방향 |
|---|---|---|---|---|
| A | `node_pressure_cpu_waiting_seconds_total` | 증가율 | 120/30 s | upper |
| B | `node_schedstat_waiting_seconds_total`(cpu 8개 합) | 증가율 | 120/30 s | upper |
| C | `node_load1` | 창 안 마지막 표본 | 60/30 s | upper |
| D | `container_cpu_usage_seconds_total{container="vllm"}` | 증가율 | 60/15 s | two_sided |
| E | `container_cpu_cfs_throttled_periods_total / container_cpu_cfs_periods_total`(vllm) | 증가율 비 | 60/15 s | two_sided |
| F | 8 − `node_cpu_seconds_total{mode="idle"}`(합) 증가율 | 노드 사용 core | 120/30 s | upper |

- D·E는 경합에서 높아질지 낮아질지 사전에 알 수 없으므로 결과 보기 전에 two_sided로 고정. 금지: 지연·SLO·probe 값, 부하 pod 자신의 CPU, 결과를 본 뒤 후보 추가·삭제·방향 변경(코드 `validate_rule_metrics`가 거부).
- **보정 증거**: 유효 기준선 관찰 구간 + 유효 개발 rep의 사전 구간 + weak 이하 강도 중 SLO 정상 단계 구간. weak보다 높은 단계(strong 후보 포함)·회복 구간·확증 자료는 쓰지 않는다.
- **임계값**: "보정 증거에서 3-consecutive 오탐 0회인 가장 민감한 값". upper: t = max(연속 3창마다의 최솟값), 발화 = 3창 연속 값 > t. two_sided는 같은 방식의 lower(t = min(연속 3창마다의 최댓값), 발화 = 3창 연속 < t)를 함께. 구간 사이는 연속으로 잇지 않는다.
- 결측: 값이 없는 창은 연속을 끊고 발화하지 않는다. 표본 간격 문제는 §5 무결성으로 invalid.
- 이 값들은 개발 뒤·확증 전에 amendment로 고정하고 확증 자료로 다시 계산하지 않는다.

## 8. 기준 5 - trivial single-metric separator

지표 규칙 하나가 (a) 유효 weak 확증 2/2의 관찰 전체(사전·weak·꼬리)에서 한 번도 발화하지 않고 **그리고** (b) 유효 strong 확증 2/2에서 단계 시작 이후 첫 발화가 공식 t_slo보다 이르면 → trivial separator. 하나라도 있으면 C5 = FAIL.

함께 기록(판정에 쓰지 않음, 이것을 보고 임계값을 바꾸지 않음): 지표별 첫 발화 시각, t_slo, lead = t_slo − 발화, weak 오탐 여부, strong 사전 구간 발화, weak·strong 단계와 사전 구간의 중앙값·범위·p10/p90·원 궤적. 누가 몇 초 빠른지는 우월성 결과로 주장하지 않는다.

## 9. 유효성·안전·정리

- 세션 invalid: outcome ≠ completed(SLO 경계 종료는 completed), 정리 오류, 준비 상한 초과, metric 저장 실패, 무결성 실패, target UID 변경, probe/Prometheus 부하 노드, probe 노드 미상, 부하 비정상, 사전 구간부터 SLO 위반, probe 판정 불가, 확증 단계 수 ≠ 1.
- 즉시 중단(안전 감시 5 s): probe 비정상, active pod 1개 아님·OOM·재시작, 노드 이상, 기존 recovery-policy 격리 풀림. 중단 시 원자료 보존 → 부하 정지·pod 삭제 → probe 정리 → canonical 확인.
- 정리 확인(매 run): 부하 pod 제거, active vLLM·Rollout Healthy·selector 불변·승격 없음·context 없음·record_only, 노드 Ready, **노드 CPU 정상화**(최근 120 s 노드 사용 core ≤ 사전 구간 최댓값 + 0.5, 상한 300 s), Prometheus·probe 정상, 잔여 workload 없음. 하나라도 실패하면 이후 run 금지.
- 연속 invalid 2회 → 일시 중지·보고. 외부 전원 Offline이면 시작하지 않는다. 기준 1–4 중 하나가 이미 확정 FAIL이면 남은 확증 run을 하지 않는다.

## 10. 코드·freeze

| 파일 | 역할 |
|---|---|
| `chaos/nnf-stage0.yaml` | 강도 사다리·고정 자원·시간(+ amendment: 확증 강도·단일 지표 규칙) |
| `chaos/loadgen/cpu_burner.py` | 부하 |
| `experiments/run_nnf_stage0.py` | 실행기 |
| `experiments/nnf_stage0_analysis.py` | 판정(상태·선택·보정·기준 1–5) |
| `experiments/test_nnf_stage0.py` | 테스트 |

freeze manifest `phase8-nnf-stage0-freeze-manifest-v1-20261010.json`(버전 `nnf-stage0-v1`). 실행기는 시작 전 재해시하고 frozen이 아니면 시작하지 않는다.
