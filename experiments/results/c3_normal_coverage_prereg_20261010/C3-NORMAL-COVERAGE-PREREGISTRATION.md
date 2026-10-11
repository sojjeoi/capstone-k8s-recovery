# c3-normal-coverage 사전등록 (설계 전용, 2026-10-10, 수집 전 고정)

> 이 문서는 연구 설계다. 실행·수집·클러스터 접근 승인이 아니다. 새 자료를 보기 전에 고정하며, 수집 결과를 보고 바꾸지 않는다.
> 변경이 필요하면 새 버전 문서로 만들고 변경 이유·시점(어떤 자료를 본 뒤인지)을 기록한다.
> 범위 = **Detector Reliability**만. AI-added-value(IF 대 CPU 임계값 우월성)는 별도 단계.

## A. 연구 질문

여러 정상 운영 상태(session·Pod·node·시간·긴 운전 시간)를 포함하도록 미리 설계한 학습·보정 자료를 쓰면, c1의 특징·모델·판정 구조를 그대로 둔 채,
(1) 독립 정상 검증 세션에서 불필요 3연속 신호를 c1보다 줄이고, (2) 기존 CPU 압박에서 이상 신호를 유지할 수 있는가?

## B. 다루는 c1 한계 (근거)

| 근거(공식·탐색 결과) | 의미 |
|---|---|
| c1 학습·보정 10 세션 = 각 30 분, 같은 active Pod 1개(66ffb65476-xd5mt), 2026-10-04–05 | 한 Pod·짧은 시간대·짧은 세션만 대표 |
| 학습 memory_slope std 42.6 KB/s, memory_mean 범위 7.010–7.017 GB(std 2.5 MB) | 학습 중 메모리가 거의 일정 |
| N1 정상 6 세션: 공식 판단 창 불필요 신호 24, 그중 22가 600 s 이후 | 긴 운전 중 상태가 30 분 학습 세션에 충분히 담기지 않았다는 해석과 일치(확정 아님) |
| forensic: memory_slope만 학습 평균으로 neutralize 시 24 중 23 event의 3연속이 깨짐, trigger 평가 72 중 68이 memory_slope < 0 | 공식 c1은 정상 운전 중 큰 음수 memory_slope에 강한 local sensitivity를 보였다(원인 확정 아님) |
| 기존 자료 위 사후 개선 3건(정상 확대 LOSO, Pod-relative, active-only) 모두 NOT PROMISING | 기존 자료 위 사후 시도 3건이 NOT PROMISING → 다음 시도는 새로 설계한 자료로 |

c3는 "memory_slope가 근본 원인"이라는 가정 위에 서지 않는다. 가설은 **정상 범위 표본이 부족했다**는 것 하나다.

## C. 바꾸지 않는 것 (invariant)

- 특징: cpu_mean, cpu_slope, memory_mean, memory_slope - 순서·이름·schema(`feature-schema.json`의 4특징, queue·cache 제외 결정 포함)를 c1 그대로 고정. 특징 선택을 다시 하지 않는다.
- 특징 계산: c1과 동일(`input_integrity.window_features`, 60 s 창, 15 s 격자, active+preview **합산**, 실제 시각 기울기, 완전성·신선도 120 s 규칙, 제외 행 기록). live 기록 `features` = 오프라인 재계산이 정확히 일치함은 이미 확인됨(forensic 재현 차이 0.0).
- 모델: StandardScaler(기본값, c3 TRAIN에만 fit) + IsolationForest(`train_v32b.MODEL_PARAMS`, n_estimators 100, random_state 42, contamination auto, max_samples auto, max_features 1.0). 하이퍼파라미터 탐색 없음.
- 판정: score < threshold(엄격 미만), 3연속, 쿨다운 60 s, 평가 간격 15 s.
- 보정 알고리즘: `model_v31/replay.calibrate_threshold` 그대로(M절).
- 변형 하나만 만든다. 여러 c3를 만들고 고르지 않는다.
- **구현(재현성)**: c1 학습 스크립트는 세션 목록·특징 파일이 코드에 고정되어 있고 schema를 다시 계산하므로 목록만 바꿔 재사용할 수 없다. c3는 새 스크립트(가칭 `train_calibrate_c3.py`, T-2 단계에서 작성·리뷰)로 만들며 다음을 지킨다.
  - c1 세션 행: 동결 c1 특징 파일을 `train_calibrate_v33c1.load_session_rows`로 그대로 읽는다.
  - 새 세션 행: live evidence `c1_evaluation` 중 `status=="scored"` 평가의 기록 `features`(c1 live 경로가 같은 `window_features`로 계산, 제외 규칙 동일).
  - schema: c1 `feature-schema.json`을 **읽어서** 쓴다(재계산 없음 → 4특징 고정).
  - MODEL_PARAMS·scaler·`calibrate_threshold` import는 c1 스크립트와 같은 출처.
  - 검증: c3 스크립트로 c1 세션 목록만 넣어 학습하면 공식 c1 artifact(model 점수·threshold)를 재현해야 한다(재현 관문, 실패 시 c3 학습 중단).

## D. 유일한 변경

**정상 학습·보정 자료의 구성.** c3 TRAIN = c1tr01–06 ∪ 새 TRAIN 세션, c3 CALIBRATION = c1ca01–04 ∪ 새 CALIBRATION 세션.
- c1 자료를 포함하는 이유: c1과의 차이를 "추가된 정상 coverage" 하나로 만들기 위해서다.
- 가중치·재표본·정규화 추가 없음(새 세션이 길어 학습 행의 다수를 차지하게 되며, 이것을 보정하지 않는다 - 한계로 기록).
- 다음 자료는 c3 학습·보정에 **넣지 않는다**: N1(nb01–08, nb90·91), A/B, B/C/K, v3vs·v3vn, c1sp·c1sn, gap, demo, V1/V2·icd·cpd, nnf·iof·v2wl. 이유: 결과를 이미 본 자료이거나 압박·저하 자료다.

## E. "정상 세션" 정의 (기존 계약에서 상속, 새 정의 없음)

모두 만족해야 정상 세션이다.
1. 주입 없음(chaos 객체 없음, 부하는 probe만), recovery·promotion·수동 조작 없음(guarded 정책 record_only, 기존 recovery-policy 격리 기록 또는 비활성 확인).
2. 관찰 완료(`outcome_status=completed`), 정리 오류 없음(`cleanup_errors=[]`).
3. probe 이미지 실행 확인, probe 자료 공백 ≤ 30 s, 요청 수 ≥ 관찰 초 × 0.95(N1 계약 invalid 기준 상속).
4. **공식 SLO 위반 없음**: `slo_judge.py`(SLO v3: 60 s 창 probe P95 > 0.648 s가 30 s 지속, 또는 성공률 < 0.99) + `slo_episodes` r2. 관찰 전 구간.
5. 요청 실패·timeout(service_event) 0.
6. 기록 완전성(N1 계약 상속): 시작 기록 1개, run_id 일치, 깨진 줄 없음, 평가 번호 연속, 시각 역행 없음, 기록 공백 ≤ 30 s.
7. **(c3 추가 규칙)** 컨테이너 재시작 수 관찰 전후 동일, 기대 Pod UID 불변(교체 없음), 노드 Ready 유지. 기존 계약의 "노드·Pod 사유 중단 = invalid"를 기록 가능한 확인 항목으로 구체화한 것.
8. **(c3 추가 규칙)** PC 전원·절전 사건 없음(전원 이벤트 로그 확인). 기존 계약의 "전원 사유 중단 = invalid"와 v3vs05·06 절전 무효 사례를 구체화한 것.
9. c1 사용 가능 평가 비율 ≥ 0.90(c1 수집 계획 §4 상속, 분모 = 건너뜀 포함 전체).

판정은 `normal_valid` / `unexpected_degradation`(4 또는 5 위반) / `invalid`(그 외) 세 가지. 뒤의 둘은 보존하고 학습·보정·검증 어디에도 넣지 않으며 대체는 새 ID로만 한다.
- 한계: SLO 정상 = 정상 라벨 기준일 뿐 detector 입력이 아니다(지연·SLO를 특징으로 넣지 않음 - outcome 누설 방지).

## F. 수집 strata (정상 운영 상태 축)

| 축 | 설계 | 근거·제약 |
|---|---|---|
| workload | **probe 1 RPS 한 수준만**(c1·N1과 같음). medium·high-but-SLO-normal = **TBD — requires user-approved live preflight** | 기존 증거: 추가 부하 0.05 RPS에서 v2wl91 SLO 위반, 0.15 RPS에서 v2wl90 위반, 기준 부하에서도 probe P95 0.66–0.76 s 표본 산발(임계 0.648 s 근처). 안전하게 SLO 정상인 상위 부하 수준을 정할 근거가 없다 |
| session | 독립 세션 여러 개, 세션마다 새 실행 ID | - |
| Pod | 세션마다 preview Pod는 새로 만들어진다(기존 준비 경로). active Pod는 블록마다 달라야 한다(H절 5) | c1은 active 1개만 |
| node | active가 sj-worker / sj-worker2 양쪽인 블록을 포함하도록 목표 | 승격마다 active 노드가 바뀜. 달성 여부는 기록·보고(관문 아님) |
| 시간 | 3개 이상의 서로 다른 날짜(블록) | c1은 이틀 |
| 운전 길이 | **관찰 4200 s**(N1과 같은 [t0, t0+3600) + 추적 600 s) | c1 30 분은 600 s 이후 상태를 거의 담지 못함 |
| memory 거동 | 고르지 않는다. 자연 발생한 안정·완만·감소·계단 감소를 그대로 포함·기록 | 의도적 선택·제외 금지, 메모리 감소를 만드는 조작 금지 |
| startup | 포함(L절), 구간 태그로 기록 | - |

## G. TRAIN / CALIBRATION / VALIDATION

- TRAIN = c1tr01–06 + 새 TRAIN 세션. CALIBRATION = c1ca01–04 + 새 CALIBRATION 세션. NORMAL VALIDATION = 새 VALIDATION 세션만(c1·c3 어느 쪽 학습·보정에도 쓰이지 않음).
- PRESSURE ROBUSTNESS = 기존 A/B A arm 5 run(ab01a–ab05a) 증거(P절). 압박 자료는 학습·보정에 절대 넣지 않는다.
- 보조(판정 미사용): N1 정상 6 세션에서 c1 대 c3 비교를 **비독립 참고**로만 보고(이미 forensic으로 들여다본 자료).

## H. 세션 단위 분할 규칙

1. 역할은 수집 **전에** 아래 표로 고정한다. 결과를 보고 세션 역할을 옮기지 않는다.
2. 같은 세션의 행은 한 역할에만 들어간다(인접 표본 랜덤 분할 금지).
3. 역할을 블록(날짜·active Pod)마다 섞어 시간·Pod 차이가 역할 사이에 고르게 퍼지게 한다.
4. invalid·unexpected_degradation 세션은 같은 역할의 새 ID로 대체(사용자 승인 후). 대체 ID는 예비 ID를 순서대로 쓴다.
5. **블록 경계 = active Pod UID가 바뀐 뒤.** 다음 블록 시작 전에 active Pod UID가 이전 블록들과 다른지 읽기 전용으로 확인. 같으면 블록을 시작하지 않고 사용자 결정(시연·승인된 승격으로 자연 교체 / 승인된 rollout 재시작)을 기다린다.

### 최소안 (minimum viable, 새 10 세션)

| 블록 | 세션(실행 순서) |
|---|---|
| 1 (날짜 1, active Pod P1) | c3n01 TR, c3n02 VA, c3n03 CA |
| 2 (날짜 2, active Pod P2) | c3n04 TR, c3n05 VA, c3n06 TR |
| 3 (날짜 3, active Pod P3) | c3n07 CA, c3n08 VA, c3n09 TR, c3n10 VA |

합계: TR 4(+c1 6), CA 2(+c1 4), VA 4. 예비 ID c3r01–c3r04.

### 권장안 (recommended, 새 16 세션) = 최소안 + 블록 4

| 4 (날짜 4, active Pod P4) | c3n11 TR, c3n12 VA, c3n13 CA, c3n14 TR, c3n15 VA, c3n16 CA |
|---|---|

합계: TR 6(+6), CA 4(+4), VA 6. 예비 ID c3r01–c3r06.

## I. 최소안 규모·시간

- 세션 1개 = 준비(preview·probe·탐지기 준비) 약 5–7 분 + 관찰 4200 s + 정리 약 3 분 ≈ **80 분**.
- 평가 수: 세션당 약 230–250 scored 예상(N1 실측 228–251, 같은 관찰 길이). 새 TRAIN 4 ≈ 950행 + c1 학습 ≈ 600행(c1 세션당 약 100행).
- 10 세션 ≈ 13.5 시간(클러스터 독점), 하루 최대 4 세션, 3일 이상.
- 시도 상한: 계획 + 4(14회). 도달 시 중단·보고.
- 검증 4 세션: 쌍 부호 기준 최소 p = 0.125 → 방향·효과 크기만 보고.

## J. 권장안 규모·시간

- 16 세션 ≈ 21.5 시간, 4일 이상. 시도 상한 계획 + 6(22회).
- 검증 6 세션: 동률이 없으면 쌍 부호 기준 최소 p = 0.03125. 그래도 일반화 금지.

## K. 세션마다 기록할 metadata

| 항목 | 출처(기존 경로) | 상태 |
|---|---|---|
| session ID, run_id, 역할, 블록, 실행 순서 | 실행기 summary·이 표 | 있음 |
| start/end UTC, duration, observation_started_at_utc, t0 | summary, evidence | 있음 |
| workload 설정(probe 1 RPS, prompt, max_tokens), 실제 요청 수·RPS | probe 기록, loadgen 설정 | 있음 |
| active/preview Pod 이름·UID·노드, active selector, Service selector | `pod_snapshot`, 5 s 표본기, summary `active_target` | 있음 |
| Rollout generation·phase·hash, scaleDownDelay | 사전·사후 읽기 전용 kubectl 기록 | **runner 준비 시 확인 필요** |
| 컨테이너 restart count(관찰 전후) | Pod status | **runner 준비 시 확인 필요** |
| Pod 시작·Ready 시각, 세션 시작 기준 Pod 나이 | Pod status·이벤트 | **일부 있음, 확인 필요** |
| CPU·memory 원 응답(Pod별), c1 4특징, scaled 벡터, score | evidence `queries`·`features`·`scaled_feature_vector`·`score` | 있음 |
| Prometheus 질의 시각·원본 표본 시각(§163–164 계측) | evidence | 있음 |
| 결측·신선도 미달·제외 평가와 사유 | 제외 기록, evaluation_skipped | 있음 |
| SLO 측정(probe 지연·성공), slo_judge·slo_episodes 결과 | probe 기록, 분석기 | 있음 |
| 응답 실패·timeout | probe 기록 | 있음 |
| 노드 상태, 전원 이벤트, 시스템 이상 | 안전 감시, Windows 이벤트 로그 | 있음 |
| 운영자 개입 여부 | 실행 로그 + 수동 기록 칸 | 수동 기록 칸 추가 |

"확인 필요" 항목이 runner 준비 단계에서 기록 불가로 확인되면 수집을 시작하지 않고 설계를 다시 검토한다.

## L. startup 처리 (사전 선택: **포함**)

- (가) 정상 분포에 포함: 실제 guarded 정책은 탐지기 기동 직후부터 첫 신호에 조치할 수 있다(시연·A/B 경로 모두). 정상 분포가 startup을 대표해야 운영과 맞는다. 단점: startup 행은 세션당 몇 개뿐이라 표본이 적다.
- (나) 별도 운영 상태: startup 동안 조치를 막는 정책 규칙이 필요하지만 현재 정책에는 그런 규칙이 없고, 만들면 정책 변경(이번 범위 밖)이 된다.
- **선택: (가).** c1과 같게 모든 사용 가능 평가(t0부터)를 학습·보정·검증에 넣는다. 자동 제외 없음.
- 보고: 신호를 EARLY(t0 + 0–120 s) / MID / LATE(≥ 600 s)로 나눠 c1·c3 각각 보고.

## M. 임계값 보정 규칙 (결정적 알고리즘)

`replay.calibrate_threshold`를 수정 없이 c3 CALIBRATION 세션 점수에 1회 적용한다.
1. 각 보정 세션을 **연속 구간(segment)**으로 나눈다: 평가 번호가 끊기거나 제외·건너뜀 평가가 있으면 그 자리에서 구간을 끊는다(live 탐지기는 건너뜀에서 연속 횟수를 0으로 되돌린다 - 이와 맞춤). 각 구간을 별도 key로 `calibrate_threshold`에 넘긴다. 세션·구간 경계는 이어붙이지 않는다. c1 보정 세션은 중간 공백이 없어(c1 스크립트가 중간 공백 시 fail-closed) 이 규칙에서 c1과 같은 결과를 낸다.
2. 후보 = 모든 보정 점수의 서로 다른 값 내림차순 + [최솟값 − 1e−9].
3. 높은 후보부터, **모든 보정 세션에서 score < 후보인 평가의 최대 연속 길이 < 3**(= 3연속 신호 0)을 처음 만족하는 후보를 threshold로 채택. 신호 0 조건은 쿨다운 값과 무관하다.
4. 채택값이 보정 점 이상을 하나도 만들지 않으면 `calibration_failed` → c3 무효(재보정·규칙 변경 없음, NOT PROMISING).
- 결정성: 같은 점수 집합이면 같은 값(정렬·고유값·순차 탐색, 난수 없음). IF는 random_state 42 고정.
- 알려진 성질: 보정 세션이 늘면 threshold는 같거나 낮아진다(덜 민감). 결과를 보고 규칙을 바꾸지 않는다.
- 알려진 차이(c1과 동일하게 상속): 보정은 평가 순번 기준 연속, 검증 신호는 기록 시각 기준 쿨다운(N1 분석과 동일).
- c3 동결: model·scaler·threshold·schema·manifest(세션 목록·파일 해시) SHA256을 검증 점수 계산 **전에** 기록한다.

## N. 주요 지표 (검증 세션마다 c1·c3 같은 행·같은 시각으로)

- T_safe = min(첫 `unnecessary` 신호 − t0, 3600 s). N1 계약 §5 분류(`guarded_normal_bc_analysis.py`, W = 600 s)를 그대로 쓴다. 신호 = 3연속 + 쿨다운 60 s(기록 시각 기준). 건너뜀·제외 평가에서 연속 횟수 0으로 리셋(M-1과 같음, c1·c3 동일 적용).
- 판단 창 불필요 신호 수, 임계값 미만 평가 비율, 세션별 일관성(c3 ≤ c1 세션 수).
- EARLY / LATE 신호 수.
- memory-decrease 평가 층: **memory_slope < −127,903 B/s**(= c1 학습 평균 −229 − 3 × c1 학습 std 42,558, 지금 고정). 이 층의 임계값 미만 수와, 이 층 평가를 포함하는 신호 event 수. 이 정의는 평가 층일 뿐 수집 선택 규칙이 아니다. 판정 D에 쓰는 이유: 사용자 요구 항목(정상 메모리 감소 구간에서 오탐 감소)이며, c1이 실제로 반응한 상태에서 개선이 있었는지 묻는 것이다(원인 가정 아님).
- 같은 지위의 보고 층(판정 미사용, 진단): memory_mean이 c1 학습 범위(7.010–7.017 GB) 밖인 평가 - forensic에서 trigger 평가 69/72가 이 층이었다. 두 층 모두 같은 표 형식으로 보고한다.
- 표: Session | c1 T_safe | c3 T_safe | c1 triggers | c3 triggers | c1 below ratio | c3 below ratio.

## O. 판정 기준 (고정)

n = normal_valid 검증 세션 수(계획 4 또는 6). 계획보다 적으면 그 n으로 계산하고 부족을 보고한다.

- A: c3 T_safe ≥ c1 T_safe인 세션 ≥ n − 1.
- B: Σ c3 불필요 신호 < Σ c1 불필요 신호(엄격).
- C: 어느 세션도 (c3 T_safe − c1 T_safe ≤ −600 s) 또는 (c3 신호 − c1 신호 ≥ 5)가 아니다.
- D: memory-decrease 층에서 c3 임계값 미만 수 < c1 임계값 미만 수. 층 평가가 0개면 D 평가 불가.
- E: P절 압박 5 run 중 5에서 신호 유지.

| 판정 | 조건(위에서부터 첫 해당) |
|---|---|
| **NOT PROMISING** | c3 `calibration_failed`, 또는 C절 재현 관문 실패 |
| **INCONCLUSIVE** | n < 3(검증 세션 부족), 또는 Σ c1 불필요 신호 = 0(c1이 이 검증에서 이미 깨끗해 B 평가 불가) |
| **PROMISING** | A ∧ B ∧ C ∧ D(충족) ∧ E |
| **MIXED** | B ∧ C ∧ E ∧ (c3 T_safe ≥ c1 T_safe 세션 ≥ n − 2) ∧ (D 충족 또는 D 평가 불가) - PROMISING이 아닌 경우. 즉 A가 1 세션 모자라거나 D 평가 불가, 또는 둘 다일 때 |
| **NOT PROMISING** | 그 밖 전부(D가 평가 가능한데 미충족인 경우 포함) |

## P. 압박 robustness (c3 동결 뒤에만)

- 자료: 공식 A/B A arm ab01a–ab05a evidence의 기록 `features`(c1과 같은 정의로 이미 계산됨 → c3에 그대로 적용 가능). 동결 c3 scaler·IF·threshold로 점수만 다시 낸다.
- 주입 시각 = 각 run summary의 `trial_outcome.t_injection_observed_utc`(이전 c2 분석과 같은 필드).
- 신호 유지 = 주입 시각 이후 첫 3연속 신호가 주입 + 363 s 이내(이전 c2 사전등록 판정 D·A/B 계약의 압박 길이와 같은 값). 3연속에 주입 전 평가가 섞이면 따로 표시.
- 함께 보고: 첫 3연속 시각, score 궤적, 공식 t_slo와의 시간 관계.
- 허용 결론: "gross CPU pressure에서도 이상 신호를 유지했다." 금지: 조기 장애 예측, "몇 초 먼저 예측했다".

## Q. 중단 / 무효 기준

- 세션 수준: E절 정상 정의 위반 → invalid 또는 unexpected_degradation(보존, 대체는 새 ID·사용자 승인).
- 수집 중단(보고 후 사람 확인): invalid 2회 연속, unexpected_degradation 2회, 정리 오류 1회, 시도 상한 도달, 블록 경계 Pod 조건 불충족.
- 학습 전 coverage 관문: 새 TRAIN 세션이 active Pod UID 2개 이상·날짜 2개 이상을 포함하지 않으면 학습하지 않고 보고.
- 검증 전 관문: c3 동결 해시 기록 전에는 검증 세션 점수를 집계·열람하지 않는다.

## R. 누설 방지

- 세션 단위 분할(H), 역할 사전 고정, 대체는 같은 역할 새 ID.
- 검증 세션 blind: 수집 중 유효성 판정은 점수와 무관한 필드(완료·SLO·probe·기록 완전성·Pod·전원)만 본다. live c1 탐지기가 record_only로 점수를 기록하지만 c3 동결 전에는 검증 세션의 score·신호를 집계·열람하지 않는다(실행기 출력에 신호 수가 찍히면 쓰지 않음 - 완전한 blind는 아님, 한계로 기록).
- scaler는 c3 TRAIN에만 fit. 보정·검증·압박 자료는 fit에 쓰지 않는다.
- 압박 자료는 c3 동결 뒤에만 점수화. N1 자료는 학습·보정에 쓰지 않는다.
- c1·c3 비교는 같은 검증 행·같은 판정 코드.

## S. 해석 한계

- 한 실험실 클러스터, CPU-only vLLM, 한 모델, probe 1 RPS 한 부하 수준. 일반화 금지.
- 검증 4–6 세션: 통계적 확정이 아니라 방향·효과 크기.
- PROMISING이어도 "이 검증 세션에서 정상 coverage 확대가 불필요 신호를 줄였다"까지만. AI가 CPU 임계값보다 낫다는 결론 아님. memory_slope 원인 확정 아님.
- workload 축(TBD)을 넣지 못하면 더 무거운 정상 부하에서의 신뢰성은 미검증으로 남는다.
- 새 정상 세션이 우연히 메모리 감소를 거의 포함하지 않으면 D는 평가 불가가 되고 판정은 최대 MIXED.
- 검증 세션은 학습 세션과 같은 블록(날짜·active Pod)을 공유한다. 따라서 결과는 "본 적 없는 Pod·날짜"로의 일반화를 말하지 못한다.

## T. 다음 단계 체크리스트 (모두 사용자 승인 필요, 자동 진행 없음)

1. [ ] 사용자: 최소안/권장안 선택, 시작 시점(10/15 시연 뒤 권장) 승인.
2. [ ] 오프라인: **새 c3 수집 실행기**(자체 plan ID·freeze manifest·실행 ID c3n·c3r·결과 폴더, 순서 강제). `run_guarded_normal_bc.py`는 실행 ID(nb01–09/90/91)·PLAN_ID·RESULTS_DIR·n1-v1.1 provenance·일정 검사를 코드에 고정하고 BCK shadow pair를 함께 돌리므로 "수정 없는 얇은 wrapper"로는 안 된다. 그 함수를 import해 재사용하거나 demo 경로처럼 전역을 실행 중에만 바꿔 쓰는 경우 그 방식·대상을 명시적으로 기록한다. 수집 경로(probe 1 RPS, record_only 탐지기, BCK shadow 포함 여부)는 N1과 같게 둔다. + 테스트 + K절 "확인 필요" 항목 기록 가능 여부 + `train_calibrate_c3.py`(C절) 작성·c1 재현 관문. Codex 리뷰.
3. [ ] 수집 manifest 동결(이 문서 SHA256 포함).
4. [ ] [USER APPROVAL REQUIRED] 블록 1 세션별 실행.
5. [ ] 블록 경계 Pod 확인 → [USER APPROVAL REQUIRED] 다음 블록.
6. [ ] coverage 관문 → c3 학습·보정(오프라인) → 동결 해시.
7. [ ] 검증 세션 c1·c3 점수화 → N·O절 판정.
8. [ ] c3 동결 뒤 압박 robustness(P절).
9. [ ] 결과 문서. 판정과 무관하게 AI-added-value 단계는 별도 사전등록.

---

## Amendment M-1 — Mid-session builder exclusion integrity rule (2026-10-10, 수집·결과 보기 전, 사용자 결정)

> 성격: 데이터 무결성 amendment. 특징·모델·임계값 철학(M절: 보정 세션 전부에서 3연속 신호 0인 가장 민감한 관측값)·
> PROMISING 기준(O절)·세션 수·역할·진단 기준·압박 기준은 바꾸지 않는다. 새 c3 정상 자료를 하나도 수집하기 전, 결과를
> 보지 않고 고정한다. 목적은 live 탐지기와 오프라인 보정의 연속(streak) 의미를 같게 만드는 것이다.

### 1. 문제
- c3 새 세션 학습·보정 행은 공식 c1 빌더(`c1_collection.build_c1_features`, 공식 `expected_pods` + 5 s Pod 표본)로 고른다(행 선택 = c1과 동일).
- M-1(구간 분할)은 빌더가 제외한 평가에서 보정 구간을 끊는다. 그러나 live 탐지기는 그 평가를 실제로 점수화하고 연속을 이어갈 수 있다.
- 세션 가운데에서 이런 일이 생기면 오프라인 보정은 live에서 가능했던 3연속을 보지 못해 live 의미보다 더 민감한 임계값을 고를 수 있다.

### 2. 정확한 정의 (코드 `c3_common.m1_events`)
- 위치: 그 세션 빌더 행의 첫 seq보다 앞 = start, 마지막 seq보다 뒤 = end, 그 사이 = interior(빌더 행이 0개면 모두 start).
- **M-1 사건 = 빌더 제외 AND live scored(`status == "scored"`) AND interior.** 이것만 센다.
- 세지 않음: start·end 가장자리 제외, live 탐지기 자신이 건너뛴 평가(입력 불완전·신선도 미달·조회 실패·Pod 미확인 등으로 live도 점수 없음).
- 사건마다 기록: session ID, evaluation_seq, window_start_utc, score_completed_at_utc, 빌더 제외 사유, live scored 여부, 위치(interior/start/end).

### 3. 규칙
- **CALIBRATION 세션의 M-1 사건 수 > 0 → `INVALID_CALIBRATION_ATTEMPT`.** 임계값 계산에 쓰지 않는다(유효성 판정 `c3_session_validity.py`와 학습 조립 `train_calibrate_c3.py` 두 곳에서 막음).
- 그 세션은 지우거나 숨기지 않는다: 유효성 파일과 동결 기록(`excluded_operational`)에 상태·사건 목록을 남긴다.
- 대체: 기존 예비 ID 규칙(H-4·Q) 그대로 - `--record-replacement c3rNN <대상>`, 예비 ID는 대체된 세션의 역할(CALIBRATION)을 그대로 가진다. TRAIN·VALIDATION으로 재활용하지 않는다. 세션 수·역할 배정은 바뀌지 않는다.
- 대체 세션(예비)도 M-1로 무효가 되면 다음 예비가 **그 예비를** 대체한다(`--record-replacement c3r02 c3r01`). 역할·블록은 대체 사슬을 따라 처음 계획 자리(예: c3n03, CALIBRATION)의 것을 그대로 쓴다. 예비 수(4)와 시도 상한(14)은 바뀌지 않는다.
- M-1 판정을 계산할 수 없으면(빌더 입력 오류 등) CALIBRATION은 fail-closed로 `INVALID_CALIBRATION_ATTEMPT`, 다른 역할은 오류만 기록한다(유효성 파일은 항상 남김).
- 적격 행이 0개인 세션은 학습·보정에 "사용함"으로 세지 않고 `no_eligible_rows`로 기록한다(쓸 행이 없다는 사실의 기록). CALIBRATION 세션은 유효성 판정에서 `INVALID_CALIBRATION_ATTEMPT`(사유: 적격 행 0)로 표시해 같은 역할 예비로 대체할 수 있게 한다. TRAIN 세션은 기록만 한다(학습 전 coverage 관문이 부족을 잡음).
- 학습 단계에서 행 선택 자체가 실패한 세션은 `row_selection_error`로 사유와 함께 빼고 학습은 계속한다.
- live가 건너뛴 평가가 빌더 행이 되는 경우(기존 증거에서는 0건)는 행에서 빼서 보정 구간이 live 리셋 자리를 잇지 않게 한다.
- 중단 규칙(Q)과의 관계: `INVALID_CALIBRATION_ATTEMPT`는 인프라 무효(`invalid`)가 아니므로 "invalid 2회 연속" 규칙에 세지 않는다. 시도 상한(14)에는 센다.
- 범위 밖으로 남긴 것(사용자 정의대로): start·end 가장자리 제외는 세지 않는다. 세션 도중 5 s 기록이 끊겨 그 뒤 전부가 end가 되는 경우도 M-1 위반이 아니라 끝부분 행이 줄어든 것으로 처리된다.
- TRAIN 세션의 M-1 사건은 기록만 한다(학습 행은 연속을 쓰지 않으므로 의미 차이 없음).
- 보정에 쓴 세션의 합계 `calibration_mid_session_builder_excluded_live_scored_total`은 규칙상 0이며 0이어도 동결 기록에 보고한다.

### 4. VALIDATION audit 결과 - CASE B (변경 없음)
- `eval_c3_validation.py`는 빌더 행을 쓰지 않는다: 검증 세션의 live 기록 전체(`read_evals`)를 각 모델로 점수화하고 live 상태기계(`c3_common.replay` = `score_server.advance_streak`, 건너뜀·번호 빠짐에서만 0)로 연속·신호·T_safe·불필요 신호를 계산한다.
- 따라서 빌더 제외 여부와 관계없이 live scored 평가에서 연속이 이어진다 = live 의미 그대로. 운영 신뢰성 지표(T_safe·불필요 신호)는 실제 탐지기가 냈을 신호를 재현해야 하므로 live 의미가 맞고, 보정만 c1 학습 규칙(빌더 행)을 따르기 때문에 차이가 생긴다.
- 확인: 시험 `test_validation_evaluator_uses_live_sequence_not_builder`(빌더 호출 시 실패하도록 막은 상태에서 연속 신호 정상). `validation_mid_session_builder_excluded_live_scored_total` 규칙은 추가하지 않는다.

### 5. PRESSURE audit 결과 - 같음 (변경 없음)
- `pressure_c3.py`도 빌더 행을 쓰지 않고 A/B A arm live 기록 전체를 `c3_common.replay`로 재생한다.
- 확인: 공식 c1 모델로 ab01a–ab05a를 재생하면 평가마다 이상 여부·연속 수·신호가 기록된 live 판정과 모두 같고(c1 재현 관문 E, 불일치 0), 주입 뒤 첫 3연속 시각도 기록된 live 첫 신호와 같다(시험 `test_pressure_evaluator_parity_with_recorded_live_decisions`). 압박 기준(주입 + 363 s)은 그대로.
