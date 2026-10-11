# 최종 증거 요약 (2026-10-10)

> 이 문서는 **공식 결과를 다시 확인한 요약**이다. 공식 결과를 바꾸거나 새로 만들지 않았다.
> 독립 재계산 스크립트·출력: `experiments/results/final_evidence_audit_20261010/{ab,bck,n1,provenance,tests}/` (기존 파일 덮어쓰기 없음, 클러스터 접근 없음).
> 탐색·feasibility 결과(공식 아님)는 §5에만 두고, 공식 결과(§1–§4)와 섞지 않는다.

## 0. 한눈에 (10살 설명)

- 서버가 "느리지만 살아 있는" 상태가 되면, **미리 띄워 둔 새 서버로 손님(요청)을 옮기면 피해가 줄었다** - 이건 실험으로 5번 모두 확인했다(A/B).
- 하지만 "AI(Isolation Forest)가 CPU 숫자 기준보다 더 잘 알아챈다"는 것은 **확인하지 못했다**. CPU 기준과 비교한 실험(B/C/K)에서 AI가 더 나은 증거가 없었고, 평소 상태 실험(N1)에서는 AI가 쓸데없는 경고를 더 일찍 냈다.
- 그 이유를 감사해 보니 계산은 정확했지만, AI가 배운 "정상"의 범위가 너무 좁아서 서버가 막 켜진 저CPU 상태도 이상으로 봤다.

## 1. A/B 선제 전환 실험 (공식)

| 항목 | 값 | 재계산 |
|---|---|---|
| 계약 | `docs/design/phase8-v33c1-guarded-ab-comparison-contract-r2-20261007.md`, 분석 규칙 `...-ab-analysis-rules-20261007.md`, 분석기 `experiments/guarded_ab_analysis.py` | - |
| 공식 유효 쌍 | 5 (ab01–ab05). 리허설 ab00a·ab90a·ab90b 제외 | 일치 |
| A(record_only) 누적 SLO 위반 s | 204.504 / **210.83(하한, ab02)** / 201.68 / 287.79 / 207.396 | 두 경로(자체 구현·frozen slo_episodes) 모두 정확히 일치 |
| B(act) 누적 SLO 위반 s | 0 / 0 / 0 / 0 / 0 | 일치 |
| D = A − B | 5/5 양수, 중앙값 207.396 s(범위 201.68–287.79), 하한 쌍 뺀 n=4 중앙값 205.95 s | 일치 |
| 부호 검정 | 양측 p = 0.0625 (stage1 기술값, 유의성 주장 아님) | 일치 |
| 전환 근거(B 5회) | `promoted_verified`(평가 3번째), promote CLI rc 0, selector 전환 ≈0.52–0.54 s, `service_check.verdict.verified=true`, endpoint 표본 12, **새 Pod 요청 +60/+61, 이전 Pod +0**, 새/이전 Pod 이름·UID·IP 기록 | 저장된 verdict 기준 확인(카운터 원시 시계열은 미재검) |
| 요청 수 | run당 597–602(최소 570) | 일치 |

**검증한 질문**: 같은 detector·같은 압박 조건에서, 신호에 따라 **실제로 전환하는 것(B)**이 **기록만 하는 것(A)**보다 이후 사용자 SLO 피해를 줄이는가? → 5/5 그렇다.

**검증하지 않은 것**: AI가 CPU 규칙보다 낫다는 것, AI가 장애를 미리 알아챘다는 것. 두 arm 모두 같은 detector 신호를 썼고, 무결성 감사(F1) 이후 그 첫 신호는 주입 전 시작 직후 저CPU 상태에서 이미 쌓이던 것으로 확인됐다. 따라서 A/B는 "**전환이라는 조치의 효과**"이지 "**탐지의 우수성**"이 아니다. 전환 뒤 압박 대상 Pod가 서비스에서 빠지므로 B가 0이 되는 것은 조치의 직접 효과다.

## 2. B/C/K 비교 (공식, stage 1)

| 항목 | 값 | 재계산 |
|---|---|---|
| 계약 | frozen v1.2(`phase8-v33c1-guarded-bck-freeze-manifest-v1.2-20261008.json`), 분석기 `guarded_bck_analysis.py` | manifest 해시 현재 일치 |
| arm | B = c1 IF(score < −0.07668, 3연속), C = 보정 CPU 임계값(cpu_mean > 1.68425), K = 기존 CPU 임계값(> 2.7) | - |
| 공식 블록 | bc01–bc06(각 b·c·k). bc05k invalid(주입 전 telemetry 준비 실패, 주입 없음). pilot bc90 제외. stage1_reached, final=false(bc07 미실행) | 일치 |
| 주입→trigger s(중앙값) | B ≈ 30.2, C ≈ 79.2, K ≈ 196–246 | 일치 |
| 1차 D(C−B) 누적 SLO 위반 | [0, 0, **−64.994(bc03)**, 0, 0, 0] → 양수 0·0이 5·음수 1, 중앙값 0, p = 1.0 | 17개 유효 run 전부 일치, result_digest 3524f2fd… 재현 |
| 2차 D(K−B) | 5/5 양수(+49.1 – +121.7), 중앙값 +93.7 | 일치 |
| 탐색 D(K−C) | 5/5 양수, 중앙값 +94.8 | 일치 |

**결론**: c1 IF(B)가 보정 CPU 임계값(C)보다 낫다는 증거 **없음**. 6쌍 중 5쌍은 둘 다 위반 0, bc03에서는 B가 더 나빴다(원인 미확정 단발 응답 정지). B가 C보다 일찍 발화한 것은 F1(시작 직후 저CPU 이상)에서 온 것이라 "조기 탐지"로 해석할 수 없다. 확인된 것은 **기존 고정 임계값 K(2.7)가 B·C 모두보다 늦고 피해가 컸다**는 것(5/5)이다.

## 3. N1 정상 운전 연구 (공식)

| 항목 | 값 | 재계산 |
|---|---|---|
| 계약 | n1-v1.1(`phase8-v33c1-guarded-normal-bc-n1-contract-20261008.md` + amendment v1.1), 분석기 `guarded_normal_bc_analysis.py` | manifest 해시 현재 일치 |
| 시도 | nb01–nb08: **정상 유효 6**(nb01·03·04·05·06·08), **예상 밖 저하 1**(nb02, 원인 미확정), **invalid 1**(nb07, 사용자 중단). pilot nb90·91 제외 | 일치 |
| T_safe | 그 arm의 첫 "불필요 신호"까지 시간(최대 3600 s). 클수록 더 오래 조용함 | - |
| D = T_safe(B) − T_safe(C) | −1533.9 / −3566.5 / −2042.1 / −2134.9 / −2817.4 / −1867.6 → **6/6 음수**, 중앙값 −2088.5 s | 6개 전부 일치(신호 재구성·분류 포함) |
| 통계 | Wilcoxon 정확 양측 p = 0.03125(n=6 최소값), Hodges–Lehmann −2175.6 s. McNemar(참고) B만 4·C만 0·둘 다 2, p = 0.125 | 기술 |
| 사전 등록 해석 | ④ "C 우세" - 이른 반응과 불필요 신호 비용의 trade-off, 일반화 금지 | - |

**정확한 의미**: 정상 운전에서 **현재 c1 IF가 보정 CPU 임계값보다 불필요한 신호를 더 일찍(더 많이) 냈다**. "IF가 더 오래 조용했다"는 반대 방향이므로 쓰면 안 된다.
시작 120 s 제외 민감도(5/6, nb01 부호 반전)는 계약에 없는 **탐색값**이다(계약: 웜업 제외 없음).

## 4. c1 detector 무결성 감사 (2026-10-09)

- **계산 정확성**: 원자료 → 특징 → scaler → IF 점수 → 임계값 → 3연속 → 신호 → T_safe를 detector·analyzer 코드 없이 독립 재계산, A/B 10·B/C/K 17·N1 6개 run의 모든 tick 일치. **공식 c1 계산 구현에 실질 오류 없음.**
- **해석 문제(F1, HIGH)**: 실행 시작 직후 cpu_mean(0.6–1.35)이 학습 범위(1.43–1.70)보다 낮아 IF가 그 상태를 이상으로 봤다(A/B 10/10, B/C/K B arm 6/6 주입 전 tick, N1 nb01·nb03).

> c1 Isolation Forest는 설계대로 학습된 정상 분포의 이탈을 감지했다.
> 다만 정상 학습 데이터가 실제 운영 정상 변동을 충분히 포함하지 못해 CPU pressure 이전의 저CPU 상태와 일부 정상 운전에서도 anomaly signal이 발생했다.
> 따라서 빠른 anomaly signal을 CPU degradation 조기 탐지 성능으로 해석할 수 없다.

## 5. 추가 feasibility 연구 (전부 counted=false, 공식 evidence 아님)

| 연구 | 결과 | 근거 |
|---|---|---|
| v2 workload feasibility(v2wl90·91) | feasible=false - SLO 경계가 rep마다 L4·L2로 달랐고, 두 rep 모두 SLO 정상인 L1은 running ≥1 비율 ≤ 0.10 | `experiments/results/capstone_v2_workload_pilot/V2WL-FEASIBILITY-RESULT-20261009.md` |
| request-shape 평가 | max_tokens 축 부적합 - 요청 길이와 SLO가 겹침 시간으로 묶여 분리 불가 | `docs/design/phase8-aiops-v2-request-shape-axis-assessment-20261009.md` |
| progress-metric 탐색 | 진행 지표 단독은 SLO 전 평탄(5/5) → 부적합. CPU÷생성 token 관계는 정상/저하를 가르고 CPU 단독보다 15–30 s 일찍 벗어남(탐색) | `docs/design/phase8-aiops-v2-progress-metric-feasibility-20261009.md` |
| 디스크 I/O Stage 0(iof) | feasible=false - I1(2 MB/s)에서 SLO 위반이 났지만 CPU guard 실패, 위반도 배경 부하 겹침과 같은 크기 → weak 불가 | `experiments/results/capstone_iof_stage0/IOF-STAGE0-RESULT-20261010.md` |
| noisy-neighbor 평가 | 읽기 전용 평가 → PROCEED TO FEASIBILITY | `docs/design/phase8-aiops-noisy-neighbor-feasibility-assessment-20261010.md` |
| nnb90 기준선 | valid(P95 0.284 s, 실패 0) | `experiments/results/capstone_nnf_stage0/NNF-STAGE0-SCREENING-RESULT-20261010.md` |
| nnd90 quick screening | frozen 규칙상 **invalid**(부하 Pod 자신의 사전 구간 표본 간격 27 s > 25 s). W2 구간 자체의 vLLM·노드·probe 측정은 정상. **W2에서 slow-but-alive 저하**(P50 0.28→2.12 s, 실패 0, Pod 생존). frozen 사다리에 **weak 없음**. vLLM CPU·노드 사용 core·PSI·schedstat가 기준선과 크게 분리 → 단일 지표 trivial separator **HIGH RISK**(screening 수준, 기준 5 공식 FAIL 아님) → **추가 Stage 0·IF2 연구 중단** | 같은 문서 |

이 §5 결과는 IF 우월성 근거로 쓰지 않는다.

## 6. 주장 판정표 (claim matrix)

### SUPPORTED (실험으로 확인)
| 주장 | 근거 |
|---|---|
| 선제 전환(Blue/Green)이 기록만 할 때보다 이후 사용자 SLO 피해를 줄였다 | A/B 5/5, D 중앙값 207 s |
| 실제 트래픽이 새 Pod로 이동했다(API 성공만이 아님) | A/B B 5회: 새 Pod 요청 +60/61, 이전 Pod +0, 이름·UID·IP 기록, endpoint 표본 12 |
| guarded policy·Argo Rollouts 승격·서비스 확인 경로가 끝까지 동작한다 | gap02·A/B·B/C/K act run `promoted_verified` |
| 기존 고정 CPU 임계값(2.7)은 늦고 피해가 컸다 | B/C/K K−B·K−C 5/5 양수 |
| 공식 c1 계산 구현에 실질 오류가 없다 | 감사 독립 재계산 전 tick 일치 |
| 공식 수치가 원자료에서 재현된다 | 본 audit(A/B·B/C/K·N1 모두 일치) |

### NOT SUPPORTED (주장 불가)
| 주장 | 이유 |
|---|---|
| Isolation Forest가 CPU 장애를 조기 예측(탐지)했다 | F1: 이른 신호는 시작 직후 저CPU 상태에서 온 것 |
| IF가 보정 CPU 임계값보다 우월하다 | B/C/K D(C−B) 중앙값 0, bc03 B 더 나쁨 |
| IF가 정상 운전에서 더 조용했다(불필요 경고가 적었다) | N1 6/6 반대 방향 |
| 현재 c1의 정상 학습 범위가 충분했다 | F1 |
| 다변량 관계 학습이 단일 지표보다 낫다 | 검증한 실험 없음(v2·iof·nnf 모두 feasibility 단계에서 중단) |

### INVALID / EXPLORATORY (공식 evidence 아님)
- 리허설·pilot: ab00a, ab90a/b, bc90b/c/k, nb90/91, gap01/02(개발 pilot), grn/grp
- invalid: bc05k, nb07, iob90, nnd90(+ nnb90 시작 실패 시도 2회)
- 탐색: N1 시작 120 s 제외 민감도(5/6), progress-metric 관계, v2wl·iof·nnf feasibility 전부

### DO NOT CLAIM (발표·논문 금지 표현)
- "AI가 장애를 예측했다 / 미리 감지했다 / 40–50초 먼저 알아챘다"
- "AI가 CPU 임계값보다 더 정확하다·더 빠르다·더 낫다"
- "AI가 오탐이 적다"
- "다변량 AI가 단일 지표의 한계를 극복했다"
- "A/B 결과는 AI 탐지 덕분이다"
- "p < 0.05로 유의하다"(A/B p=0.0625 기술값, N1 p는 반대 방향 결과)
- noisy-neighbor·I/O 실험을 "AI가 잡아낸 장애"로 소개

### PRESENTATION-SAFE CLAIM (발표에서 써도 되는 표현)
- "느리지만 살아 있는 AI 추론 서비스 저하에서, 이상 신호에 따라 미리 준비한 Pod로 실제 전환하면 기록만 할 때보다 사용자 SLO 피해가 5회 모두 줄었다(중앙값 약 207초 → 0초)."
- "전환 성공은 API 응답이 아니라 새 Pod·이전 Pod의 실제 요청 카운터로 확인했다."
- "Isolation Forest는 '학습된 정상 패턴에서 벗어남'을 알리는 신호로 썼고, 조치 결정은 guarded policy가 한다."
- "CPU 임계값과의 공정 비교에서 AI의 우월성은 확인되지 않았고, 원인은 정상 학습 범위가 좁았기 때문임을 무결성 감사로 밝혔다."
- "기존 고정 임계값(2.7 core)은 늦고 피해가 컸다."

## 7. 재현성·provenance 요약

`docs/final/REPRODUCIBILITY-AUDIT-20261010.md` 참조.
