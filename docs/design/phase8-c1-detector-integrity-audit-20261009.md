# c1 detector integrity audit (읽기 전용, 2026-10-09)

> 성능 개선 아님. production·frozen 코드/결과 수정 없음, live·학습·임계값 변경 없음(Prometheus 읽기 조회만).
> 재현 자료: `experiments/results/c1_integrity_audit/` - `c1_replay_audit.py`(전 공식 evidence 독립 재계산, `replay-summary.json`, `replay-mismatches.csv`, `feature-mismatch-detail.txt`), `n1_independent_tsafe.py`(`n1-independent-tsafe.json`, `n1-start-transient-sensitivity.txt`), `e2e_replay_three_runs.py`(`e2e-tick-table-{nb03,nb05,bc03c}.csv`, `e2e-replay-summary.json`, `prometheus-requery-diff-detail.txt`), `calibration-independent.txt`.
> 독립성: 재계산 스크립트는 detector·analyzer 코드를 import하지 않는다(기대 Pod·격자·평균·기울기·scaler·규칙·연속·쿨다운·T_safe·분류 직접 구현). IF 점수만 frozen model.pkl의 decision_function을 호출(점수 함수 재구현 아님).

## A. 범위

공식 c1 계열 evidence 33개(A/B ab01–ab05 a·b 10개, B/C/K bc01–bc06 b·c·k 17개 - bc05k는 telemetry 미준비 abort로 evidence 없음, N1 유효 6개)의 전 평가 tick, C·IF 보정 자료(c1ca01–04), frozen artifact·manifest·run provenance, 기존 detector 테스트(127 passed, 4 strict xfail).

## B. artifact / provenance

| 항목 | 값 | 확인 |
|---|---|---|
| scaler / model | `v3/model_v33_c1/artifacts` scaler.pkl fd4a6152…, model.pkl 0aa5d4c9…(IsolationForest, n_features_in_=4, offset_=-0.5) | SHA256SUMS 9개 파일 현재 bytes 일치. 모든 run의 시작 기록 `artifact_hashes` = 현재 SHA256SUMS. 실행 중 load_artifacts가 SHA256SUMS·threshold를 fail-closed로 확인 |
| feature 순서 | cpu_mean, cpu_slope, memory_mean, memory_slope(kept_feature_indices 0–3) | scaler에 feature_names_in_ 없음(배열로 학습) → 순서는 코드 경로가 보장 |
| IF threshold / 연속 / 쿨다운 | -0.07667538512242333 / 3 / 60 s | threshold.json, rule-spec-v1(b0113f17…), 시작 기록 일치 |
| C / K | cpu_mean > 1.6842544293089357 / > 2.7 | rule-spec-v1, 코드 상수(load_spec fail-closed) |
| sklearn | 학습 1.9.0 = 현재 1.9.0 | training-metadata |
| B/C/K official | freeze v1.2(16파일) - bc01–bc06 전부 이 manifest 검증 기록 | 현재 bytes 일치 |
| N1 official | freeze n1-v1.1(31파일) - nb01–nb08 전부 | 현재 bytes 일치 |
| A/B official | summary에 code_provenance 없음(이 계열은 provenance 기록 이전) | evidence 시작 기록의 artifact hash만 |
| 계산 경로 파일 | input_integrity.py·features.py·score_server.py·feature_selection.py(B/C/K는 c1_shadow_detector.py도)는 **manifest에 없음** | 최종 수정 시각 10/04–10/05 < 모든 공식 run(10/07–10/09). evidence의 PromQL은 run 전체에서 지표마다 한 문자열로 동일 |

버전 경계: B/C/K manifest v1→v1.1→v1.2와 N1 v1→v1.1의 차이는 runner·analyzer 파일뿐이다. detector 파일(c1_bck_live_detector 7db77428, rule-spec b0113f17, c1_shadow_detector 1c9b172d, c1_bck_shadow_pair 72487204, model/scaler) 해시는 이들을 담은 모든 manifest에서 같다.

## C. 과거 known bug

`test_input_integrity.py`의 strict xfail 4개가 frozen legacy 경로(features._mean_slope·score_server·prom_health)에 남은 결함을 고정 기록한다: 부분 Pod 합산, 시간축 압축(np.arange), freshness result[0]만, 인스턴트 응답 시각을 표본 시각으로 사용. **c1 공식 경로는 이 legacy 경로를 쓰지 않는다**: evaluate_once → input_integrity.window_features(기대 Pod 전원·전체 격자 필수, 실제 시각 기울기) + check_freshness_by_pod(Pod별 원시 표본 시각). c1 학습·보정 특징도 같은 고친 함수로 실행 당시 원문에서 재구성했다(c1_collection.build_c1_features). 독립 재계산이 모든 scored tick에서 일치 → pre-fix 결과가 공식 c1 계열에 섞이지 않았다.

## D. Prometheus 입력·집계

| feature | PromQL(evidence 기록) | 출처 job | 집계 |
|---|---|---|---|
| cpu | `rate(container_cpu_usage_seconds_total{namespace="vllm-serving",container="vllm"}[60s])` | kubelet(cAdvisor) | 기대 Pod(active+preview)를 Pod별 1줄로 확인 → 격자마다 합 |
| memory | `container_memory_working_set_bytes{namespace="vllm-serving",container="vllm"}` | kubelet | 같음 |
| queue / cache(모델 미사용, 완전성 검사만) | `vllm:num_requests_waiting`, `vllm:kv_cache_usage_perc` | vllm-active(active Pod), vllm-preview(preview Pod) | 같음 |

1. 같은 Pod 이중 scrape: cpu·memory는 kubelet 한 job. vLLM 지표는 active·preview가 서로 다른 Pod일 때 Pod마다 한 job. 같은 Pod가 두 job에 잡히면 `assemble`이 `duplicate_series`로 그 평가를 **건너뛴다**(합산·부풀림 없음). selector가 같아진 경우(승격 뒤)는 `selector_unknown`으로 먼저 건너뛴다.
2. active+preview 합산: **의도된 정의** - rule-spec `input.cpu_mean`, 학습 통계(memory_mean ≈ 7.01e9 B = Pod 2개), 학습 빌더가 같은 합을 쓴다.
3. finished_reason류 라벨: c1 입력 지표에는 해당 없음(그 문제는 v2 분석기에서만, 이미 수정).
4. UID: cpu·memory는 cAdvisor `id` 경로의 UID를 기대 UID와 대조. queue·cache는 UID 라벨이 없어 같은 snapshot의 이름→UID 매핑(runtime_mapping)으로 인정 - 사실상 이름 기준(INFO, 모델 미사용 지표).
5. 교체 뒤 old Pod: 기대 Pod 이름 집합으로만 거른다(다른 Pod 시계열 무시), 기대 Pod가 창 시작 전에 기동해야 한다.
6. stale: range 격자는 lookback으로 직전 표본을 쓸 수 있다(F4). 최신 표본 나이 ≤ 120 s 검사, 공식 run 최대 관측 28.9 s.
7. 합산 의도는 rule-spec에 명시, K 한계(단일 컨테이너 기준 2.7을 합산값에 적용)도 기록돼 있다.

## E. Pod identity

모든 scored tick에서 기대 Pod를 그 tick의 snapshot만으로 독립 재도출(active·preview selector 다름, 해시별 정확히 1개, Running, 종료 아님, vllm 컨테이너가 창 시작 전 기동, 다른 vllm Pod 없음) → 기록값과 100 % 일치. B/C/K act run의 건너뜀은 전부 승격 뒤 `selector_unknown(active=preview)`. N1·B/C/K는 한 tick = evaluate_once 1회 = 한 snapshot에서 B·C·K 판정.

## F. 시간축·창·신선도

- 창: window_end = 평가 시작(PC 벽시계), start = end − 60 s, 격자 = start + 15 s × {0..4}(양끝 포함 5점, ±0.5 s). 모든 tick 일치.
- 빠진 격자는 채우지 않고 건너뜀(보간·복제 없음). 기울기 x = (시각 − 첫 시각)/15 s(압축 없음) - 독립 최소제곱과 일치.
- 미래 표본: 창 끝 이후 표본 0건.
- 평가 간격 중앙값 16.6–18.2 s(명목 15 s) - 3연속 ≈ 50 s. 임계값은 3연속 여부로만 정해져 간격 영향 없음(threshold.json 기록).
- 창 끝 격자점(F3): 질의가 창 끝 0.2–2 s 뒤에 나가 최신 scrape가 아직 반영되지 않은 직전 표본 값. 지금 다시 조회하면 그 한 점만 다르다(nb03 tick1 cpu 1.156 vs 1.617). 학습 자료도 같은 실시간 방식 → 정의 일관, 단 사후 재조회로 정확 재현 불가(저장 원문 사용).
- clock skew: freshness 나이는 Prometheus 응답 안 시각끼리 계산(PC 시계 무관), 사전검사 PC↔정책 Pod 시계 오차 ≤ 0.5 s.

## G. feature 계산

cpu_mean = 두 Pod rate[60s] 합의 5점 평균(core), cpu_slope = 15 s당 기울기, memory는 bytes. 학습 빌더와 runtime이 같은 `input_integrity.window_features`와 같은 PromQL 문자열(학습 `METRICS["cpu"].replace("[30s]","[60s]")` = runtime `cpu_rate_query()`). NaN/Inf는 건너뜀. 독립 재계산 차이는 213 tick의 부동소수 잡음뿐(cpu ≤ 4.4e-16, memory_slope ≤ 1.7e-6 B = 학습 표준편차의 4e-11).

## H. scaler / 순서

window_features가 고정 순서(cpu, memory, queue, cache)×(mean, slope)로 8개를 만들고 apply_feature_schema가 인덱스 0–3을 뽑는다(dict 순서 무관). 독립 (x − mean_)/scale_가 기록된 scaled vector와 전 scored tick 일치.

## I. IF 판단 방향

`decision_function`(낮을수록 이상), `score < threshold`(strict). 독립 decision_function과 기록 score 차이 ≤ 1e-9(전 tick).

## J. 연속·쿨다운·skip

`advance_streak`: 이상이면 +1, 아니면 0, ≥ 3이고 쿨다운(마지막 발화 후 < 60 s)이 아니면 발화. 첫 true = 1, 세 번째 연속에서 발화, false 즉시 0, skip(누락·불완전·stale·조회 실패·Pod 불일치)은 세 규칙 모두 0. 쿨다운 중 연속은 리셋되지 않음(쿨다운이 끝나면 연속 ≥ 3에서 바로 재발화 - 설계 동작). 중복 seq 0건. 독립 구현의 연속값·발화가 모든 run·모든 tick에서 기록과 일치(B·C·K 각각). tick 표: `e2e-tick-table-*.csv`.

## K. B/C/K same-tick

`judge_all`은 같은 evaluate_once 레코드에서 B = score, C·K = features.cpu_mean을 읽는다(추가 조회 없음). N1 전 scored tick에서 B 값 == score, C 값 == K 값 == cpu_mean(값 단위, 6/6 세션 100 %). B/C/K official 비교는 arm마다 다른 run(bcNNb/c/k)이라 run 쌍 비교다(설계대로) - run 안에서는 세 규칙이 같은 tick.

## L. C calibration

보정 4세션 행만으로 닫힌 형식 재계산: C = max(세션별 max_k min(cpu[k..k+2])) = 1.6842544293089357(정확 일치), IF = min(세션별 min_k max(score[k..k+2])) = -0.07667538512242333(정확 일치). 보정 행 seq 공백 0, 같은 cpu_mean 정의. C 보정 계산 시각(10/07 14:00 UTC)은 A/B 공식 run 뒤지만 입력은 c1ca 세션뿐이고 닫힌 형식이 결정적이라 결과 의존 불가.

## M. N1 analyzer

계약과 코드 일치: t0 = 첫 scored 평가 완료 시각, 판정 창 [t0, t0+3600)(t0 포함, 3600 정각 이후 제외), W = 600 s(관찰 끝 < t+600이면 indeterminate), 인프라 창 [max(t0, t−60), t+600](v1.1 코드 적용), 분류 순서 during_degradation → indeterminate → true_warning_candidate → unnecessary, T_safe = min(첫 unnecessary − t0, 3600).

## N. N1 6 유효 세션 독립 재계산 (analyzer 미사용)

| run | B 첫 신호(t0 기준) | C 첫 신호 | T_safe(B) | T_safe(C) | D | 공식 |
|---|---|---|---|---|---|---|
| nb01 | 34.0 s | 1567.9 s | 34.0 | 1567.9 | −1533.9 | 일치 |
| nb03 | 33.5 | 없음 | 33.5 | 3600 | −3566.5 | 일치 |
| nb04 | 1116.4 | 3158.5 | 1116.4 | 3158.5 | −2042.1 | 일치 |
| nb05 | 1465.1 | 없음 | 1465.1 | 3600 | −2134.9 | 일치 |
| nb06 | 782.7 | 없음 | 782.7 | 3600 | −2817.4 | 일치 |
| nb08 | 1732.4 | 없음 | 1732.4 | 3600 | −1867.6 | 일치 |

모든 신호가 독립 분류에서도 unnecessary(probe 실패 0, 독립 SLO 근사 위반 0, 창 안 인프라 증거 0). **6/6 C 우세는 원자료 독립 계산에서도 그대로.**

## O. end-to-end replay 3건

| run | 대상 | 결과 |
|---|---|---|
| nb03 (B 33.5 s) | tick 1–3, B 발화 tick 3 | 원문 → 발화 전 단계 exact match. Prometheus 재조회는 창 끝 한 점만 다름(F3) |
| nb05 (B 1465 s) | tick 84–87, B 발화 tick 87 | exact match, 재조회 차이는 창 끝 점만 |
| bc03c (압박, C 발화 +77.7 s) | tick 3–6 | exact match, 재조회 차이는 창 끝 점만 |

## P. 테스트 사각

- 단위 테스트는 가짜 Prometheus·가짜 snapshot 중심. 실제 저장 원문을 실제 artifact로 끝까지 재생하는 테스트가 없다 → 이번 audit 스크립트가 그 역할(audit 전용 위치에 보존).
- 있음: duplicate_series, stale, terminating/교체, 쿨다운, 3연속, judge_all, 실제 artifact 로드. 없음/약함: 실제 evidence 재생, 창 끝 ingest 지연, 쿨다운 정확 경계(60.0 s), lookback으로 채워진 격자 검출, 시작 직후(warm-up) 입력 분포.

## Q. findings

| # | 심각도 | 내용 | 영향 run | 결과 차이 | 결론 영향 | 수정 |
|---|---|---|---|---|---|---|
| F1 | **HIGH(해석)** - 코드 결함 아님 | **시작 직후 저-CPU 상태에서 IF가 이상 판정.** 첫 평가의 cpu_mean 0.6–1.35 core(학습 범위 1.43–1.70보다 낮음) → score −0.15 – −0.27로 이상. A/B 10/10, B/C/K B arm 6/6에서 주입 **전** 창의 첫 tick부터 이상이었고, 발화(+24–33 s)를 이룬 세 tick의 cpu_mean은 16개 run 중 15개에서 모두 ≤ 1.70(학습 상한 이하, 예외 ab04b 셋째 tick 1.78). N1 nb01·nb03의 B 신호(34 s)도 같은 시작 구간. C는 같은 구간에서 정상 | A/B 전부, B/C/K B arm 전부, N1 nb01·nb03 | 값·계산은 정확. "B가 C보다 약 40–50 s 일렀다"·"IF가 압박을 조기 탐지했다"는 압박 탐지로 귀속 불가. N1: 시작 120 s 신호를 빼는 기술적 민감도(계약 아님)에서는 nb01 부호가 바뀜(D' = +932 s) → 5/6 | c1 비우월 결론은 유지(오히려 강화), 보조 서술 정정 필요 | 코드 수정 아님. 서술 정정 + 향후 warm-up 규칙 사전 등록 |
| F2 | LOW | provenance 범위: 계산 경로 파일이 manifest에 없음, A/B는 code_provenance 없음 | 전 계열 | 없음(수정 시각 < run, PromQL 동일, 행동 재계산 100 % 일치) | 없음 | 향후 manifest에 포함 |
| F3 | LOW | 창 끝 격자점은 ingest 지연으로 직전 scrape 값(실시간 특성, 학습과 동일) | 전 계열 | 사후 재조회 시 그 한 점만 다름 | 없음 | 재현은 저장 원문으로 |
| F4 | LOW | query_range lookback이 창 안 누락 scrape를 직전 값으로 채울 수 있음, freshness는 최신 표본만 | 전 계열 | 관측 최대 나이 28.9 s, 영향 증거 없음 | 없음 | 향후 원본 표본 사용 검토 |
| F5 | INFO | queue·cache identity는 이름 기준 | - | 모델 미사용 | 없음 | - |
| F6 | INFO | legacy 경로 결함 4개 존속(strict xfail) | c1 비대상 | - | 없음 | legacy 재사용 금지 |
| F7 | INFO | 부동소수 잡음(polyfit) | 213 tick | ≤ 1.7e-6 B | 없음 | - |
| F8 | INFO | 평가 간격 16.6–18.2 s(명목 15 s) | 전 계열 | 없음 | 없음 | 문서화 |
| F9 | INFO | C 보정 계산 시각이 A/B 뒤 | B/C/K·N1 | 닫힌 형식 재현 일치 | 없음 | - |
| F10 | INFO | model_v33_c1·bck_rules가 git 미추적(SHA256SUMS로만 보장) | - | 없음 | 없음 | 추적 권장 |

## R. 결론 영향

- **코드 무결성: NO MATERIAL IMPACT.** 원문 → 특징 → scaler → IF → 임계 → 연속 → 발화 → T_safe가 공식 evidence 전체에서 독립 재계산과 일치.
- **해석: PARTIAL IMPACT(F1).** 공식 primary 값은 그대로지만 "IF가 약 40–50 s 일찍 반응", "AI가 압박을 조기 탐지"라는 서술은 시작 직후 저-CPU 이상 판정의 산물일 수 있어 정정이 필요. N1 6/6은 frozen 계약(grace 없음) 아래 정확하며, 시작 구간을 빼는 민감도에서는 5/6.

## S. Q1–Q10

1. 예. 한 tick = 한 evaluate_once 레코드에서 B는 score, C는 그 레코드의 cpu_mean(값 단위 확인, 6/6 전 tick).
2. 예. analyzer 없이 원문에서 독립 계산해도 T_safe(B)·T_safe(C)·D가 6세션 모두 일치.
3. 예. runtime 8개 순서 고정 → 인덱스 0–3 = 학습 kept 순서, 독립 scaled 전부 일치.
4. 예. decision_function 낮을수록 이상, `score < threshold` strict.
5. 예. 정확히 세 번째 연속 이상 tick에서 발화(nb03 tick 1→2→3), 쿨다운 60 s.
6. 예. skip·stale·누락·불완전은 세 규칙 모두 0으로 리셋(전 run, 위반 0).
7. 아니오. cpu·memory는 kubelet 단일 출처, vLLM 지표 중복은 건너뜀(fail-closed), active+preview 합은 의도·학습과 같은 정의.
8. 모델·scaler·임계값: 예(모든 run 시작 기록 = 현재 SHA256SUMS). runner·detector 파일: B/C/K·N1은 manifest로 예. 계산 경로 일부(input_integrity 등)는 해시 기록이 없어 수정 시각과 행동 재계산으로 간접 확인(F2).
9. 예. 같은 c1 특징 함수·같은 PromQL·같은 보정 세션, 닫힌 형식 재현 일치.
10. 예. 코드 관점에서 "C보다 운영적으로 우월하지 않았다"는 유지된다. B의 "더 이른 반응"은 시작 직후 저-CPU 판정에서 비롯된 부분이 커서 이 결론을 오히려 뒷받침한다.

## T. revalidation 계획(실행 안 함)

1. erratum 메모: A/B·B/C/K 보고의 "IF 약 40–50 s 조기 반응" 서술에 F1 주석(원 결과 파일은 수정하지 않고 별도 문서).
2. 오프라인 민감도 표(이미 산출): 시작 구간 제외 시 N1 5/6, A/B·B/C/K는 첫 이상 tick이 주입 전임을 run별로 표기.
3. 향후 실험(v2 포함)은 결과 전에 warm-up 규칙(탐지 시작을 정상 상태 도달 뒤로 등)과 계산 경로 파일 provenance를 사전 등록.
4. audit 스크립트를 회귀 확인용으로 보존(실제 evidence 재생).

## U. 발표·논문 사용

- 계속 사용 가능: A/B의 선제 전환 효과(전환 vs 무전환, 5/5), B/C/K에서 c1이 calibrated C보다 SLO 이득이 없었다는 결론, N1 6/6(frozen 계약 기준, 한 세션은 시작 구간 신호라는 주석 권장), v2wl feasible=false.
- 정정 필요: "IF가 CPU 임계값보다 약 40–50초 먼저 반응했다", "AI가 장애를 조기에 감지해 전환했다" → "전환은 IF 조건 충족 시점에 일어났지만, 그 조건은 실험 시작 직후 낮은 CPU 상태에서 이미 채워지고 있었다"로.
