# 탐지 방식 비교 계약 (guarded-bck-v1) — B(IF) / C(보정 고정 CPU) / K(legacy 2.7)

작성: 2026-10-07(KST). **상태: FROZEN (2026-10-07, 사용자 승인) — 실행 승인 아님.** 사용자 결정 D1–D5·C 정밀도(2026-10-07 채팅)를 반영했다. 동결 해시는 `phase8-v33c1-guarded-bck-freeze-manifest-20261007.json`에 기록한다. 이후 이 문서·규칙 명세·코드를 바꾸려면 실행을 멈추고 새 버전 ID와 이유를 먼저 기록한다(결과를 보고 바꾸지 않는다). bc90 pilot·공식 run은 별도 사용자 승인 후에만 실행한다.
기존 A/B 계약 r1·r2, 분석기·규칙·runner·cleanup gate·테스트, ab00–ab90·ab01–ab05 원자료는 **수정하지 않는다.** 이 계약은 A/B와 별도 시리즈다(파일명의 `draft`는 runner·분석기가 참조하는 경로를 유지하기 위한 것이다).

## 1. 연구 질문·가설

같은 Blue/Green 복구 조치를 수행할 때, 어떤 판단 방식으로 조치를 시작하느냐에 따라 사용자 SLO 피해가 달라지는가?

- **Primary (확인 검정):** 동일한 false-positive calibration 기준(정상 보정 세션 3연속 경고 0)에서 IF(B)가 calibrated fixed CPU rule(C)보다 누적 SLO 위반시간을 줄이는가?
- **Secondary:** IF(B)가 legacy 90%-CPU rule(K)보다 누적 SLO 위반시간을 줄이는가? — 효과·방향·원값은 계산·보고하지만 **확인적 유의성 주장은 하지 않는다**(D2).
- **탐색(기술 통계만):** C vs K.
- **주장하지 않는 것:** 모든 장애 예측, CPU pressure 밖의 일반화, 운영 false-positive 비용 평가(별도 질문), A/B 결과와의 합산.

## 2. 자료 분리

- 공식 자료 = `bc01`–`bc18`만. ab00·ab90·ab01–ab05(A/B), gap·grp·grn, 독립 검증 v3vs, bc90 pilot은 집계하지 않는다.
- **A/B의 B와 이 계약의 B는 다른 시리즈다.** 수치를 합치거나 반복 수로 세지 않는다.
- 별도 plan ID·결과 폴더·실행 ID 접두어(`bc`)·probe 반복 번호(§10).

## 3. Arm 정의 (trigger source만 다름)

| arm | 판단 규칙(같은 평가 tick·같은 c1 입력) | 실제 조치 |
|---|---|---|
| **B** | IF v3.3-c1 `decision_function < -0.07667538512242333` (strict) | guarded `act` → 최대 1회 Blue/Green promotion |
| **C** | `cpu_mean > 1.6842544293089357` (strict, 표시 약 1.6843 core) | 동일 |
| **K** | `cpu_mean > 2.7` (strict) | 동일 |

공통(세 arm 동일): c1 입력 무결성(`input_integrity`·`c1_shadow_detector.evaluate_once`), 평가 간격 15초(실측 약 17초), 60초 창, **3연속**, **송신 쿨다운 60초**, 건너뛴 평가는 연속 0 리셋, signal schema `c1-signal-v2`, 같은 guarded context 결속 방식, 같은 정책 이미지, 같은 promotion·selector 검증·new/old Pod 요청 확인(pod /metrics)·관찰 600초·D6 cleanup·canonical end.

- 배정 기준(ITT): arm 규칙이 신호를 내지 않거나 정책이 거부해도 다른 유효 조건을 만족하면 그 arm 결과에 포함한다(예: K가 관찰 끝까지 발화하지 않으면 무전환 결과로 그대로 셈).

## 4. 임계값 동결 (변경 금지)

| | machine-readable 값 | 근거 |
|---|---|---|
| IF | `-0.07667538512242333` | `model_v33_c1/artifacts/threshold.json` (SHA256SUMS 확인, `c1_shadow_detector.EXPECTED_THRESHOLD`) |
| C | `1.6842544293089357` | 아래 재현 (사람이 읽는 문서·발표 표기: 약 1.6843 core) |
| K | `2.7` | 컨테이너 CPU limit 3코어 × 90% — `anomaly-detection/fixed_threshold.py:51,55-68`, `docs/design/experiment-contract.md:504`(§6), 2026-09-20 변경 기록(:601) |

**C provenance (2026-10-07, 오프라인 재현):** `experiments/bck_fixed_threshold_calibration.py` → `experiments/analysis_v1/bck-fixed-threshold-calibration-20261007.json`.
- 방법: IF 임계값과 **같은 함수** `model_v31/replay.calibrate_threshold`, **같은 사전 지정 보정 4세션** c1ca01–04(매니페스트 `c1-data-manifest.json` sha256 `99a2242d01586f41b11a86f3739dc3f202c68692ab68f7bb13c7d10f183da283`, 행 seq 연속 확인), 같은 기준(3연속·쿨다운 60·간격 15), 점수 = −cpu_mean.
- 결과 `1.6842544293089357`(calibration_failed=false). 세션별 경고 0, 최대 연속 2/2/2/0, 단일 점 초과 6/12/3/0. K=2.7은 보정 자료에서 단일 점 초과도 0.
- **ab01–ab05 결과·독립 검증 등 압박 자료는 C·K 결정에 사용하지 않았다.**
- **pilot·공식 결과를 보고 IF·C·K 임계값을 바꾸지 않는다.** 주입 전 발화가 나와도 바꾸지 않고 false-positive evidence로 보존한다.

## 5. 공통 평가 tick 구조 (구현: `anomaly-detection/c1_bck_live_detector.py`)

1. 평가 1회 = 동결 `evaluate_once` 1회 → 같은 snapshot·같은 Prometheus 원문·같은 무결성·신선도 검사에서 `features.cpu_mean`과 IF `score`를 함께 얻는다(규칙별 Prometheus 추가 조회 없음).
2. 같은 레코드에서 세 판정. 연속·쿨다운은 동결 `score_server.advance_streak`(value < threshold = 이상)를 규칙마다 **독립 상태**로 호출:
   - B: `advance_streak(score, -0.07667538512242333)`
   - C: `advance_streak(-cpu_mean, -1.6842544293089357)` ⇔ `cpu_mean > 1.6842544293089357`
   - K: `advance_streak(-cpu_mean, -2.7)` ⇔ `cpu_mean > 2.7`
   - 건너뛴 평가는 세 상태 모두 0 리셋(동결 `step_streak`과 같은 처리). B 판정 순서열이 동결 `step_streak`과 같음을 테스트로 확인.
3. 평가 기록 1줄에 `rules{B,C,K}`(값·임계값·이상 여부·연속·condition_met·시각)와 `actuating_rule`. 최상위 `condition_met`·`consecutive_anomalous`는 실제 arm 규칙 값(기존 실행기·D5 관문·준비 확인이 읽는 형식 유지), 최상위 `score`는 IF score. 신호 시각 = 그 평가의 점수 계산 완료 시각(세 규칙 동일 기준).
4. **실제 arm 규칙만** `/signal/v2`로 송신. 나머지 두 규칙은 shadow evidence(송신 없음). 송신·결과 분류·종료·post_action은 동결 `c1_live_detector`와 같은 규칙(`classify_send_result`, 종료 코드).
5. 시작 기록: arm, `signal_identity`, 규칙 명세 경로·sha256, 세 규칙, 모델 아티팩트 해시. 규칙 명세가 코드 상수(임계값·identity·비교 방향·연속·쿨다운)와 다르면 시작하지 않는다(fail-closed).

**규칙 명세** `anomaly-detection/bck_rules/rule-spec-v1.json`: 입력 정의, 공통 조건, 세 규칙(detector_id·model_version·value·anomalous_if·threshold·streak 입력·provenance), signal identity 규칙.

**신호 identity (정책 수정 없음 — 코드 확인):** 정책은 context와 `experiment_run_id`·`detector_id`·`model_version`·`model_hash`·`artifact_manifest_hash`·`threshold` 정확 일치만 확인하고(`recovery-policy/guarded_policy.py:377-388`) score 방향·연속 수·탐지기 종류는 보지 않는다. schema는 extra 금지(`guarded_schema.py:33-50`) → 새 필드 없음. identity는 `signal_identity()` 한 함수에서만 만들고 runner가 같은 함수로 context를 설정한다.

| arm | detector_id | model_version | model_hash | threshold | score |
|---|---|---|---|---|---|
| B | `c1_bck_v1_if` | `v3.3-c1` | model.pkl sha256 | −0.07667538512242333 | IF score |
| C | `c1_bck_v1_fixed_cal` | `fixed-cpu-gt-1.6842544293089357` | rule-spec sha256 | 1.6842544293089357 | cpu_mean |
| K | `c1_bck_v1_fixed_legacy` | `fixed-cpu-gt-2.7` | rule-spec sha256 | 2.7 | cpu_mean |

`artifact_manifest_hash` = 기존 `c1_policy_link.artifact_manifest_hash` (B: 모델 SHA256SUMS, C/K: `{"rule-spec-v1.json": sha}`). 다른 arm context로는 정책이 거부함을 테스트로 확인(구조적 식별).

## 6. 실행 조건 (A/B r1 §5·r2 §1–§3 승계, 세 arm 동일)

CPU 압박 1→2→3→4 workers × 90초, 원래 대상 Pod에 끝까지 유지(`continue_after_target_replacement=True`), probe 1 RPS·`max_tokens=1`(설정 sha256 기록), 관찰 주입 후 600초, 탐지기 첫 scored 평가 확인 후 주입, canonical Healthy 시작(`gcg.check_canonical`), 외부 전원 필수, 기존 recovery-policy 격리, 세 arm 모두 주입 전 scaleDownDelay 420 → 승격 시 D6로 30 원복, 미승격이면 D5 abort → canonical 복원, 모든 run 끝 canonical 재확인. 실행 계약 `conditions`에 기록하고 세 arm이 같아야 유효(arm identity만 다름).

## 7. 실행 순서 (사전 등록, 변경 금지)

Latin-square 6행 반복: ① (B,C,K) ② (C,K,B) ③ (K,B,C) ④ (K,C,B) ⑤ (C,B,K) ⑥ (B,K,C) → ① …
- 한 행 = 한 **block**(3 run). block NN의 순서 = 행 ((NN−1) mod 6)+1. pilot bc90 = (B,C,K).
- **시도한 block 기준으로 진행**한다(무효 run·block도 행을 소모, 결과로 순서를 고르지 않음). runner가 순서 밖 run을 거부한다(같은 block의 앞 arm, block 첫 arm이면 앞 block 세 run의 요약이 있어야 시작 — 코드 강제).

## 8. 시작 노드

anti-affinity + 워커 2대 → 승격된 run 다음 run은 반대 노드에서 시작한다. **A/B의 같은 노드 요건은 B/C/K에서 유지할 수 없다.** 노드를 되돌리는 추가 승격은 하지 않는다. 각 run 시작 active 노드를 기록하고 arm별 노드를 보고한다(보정·제외에 쓰지 않음). 한계로 공개.

## 9. 유효·무효·중단

- run 유효(분석기 `run_validity`) = 동결 A/B 분석기의 B 분기 규칙 V1–V7을 세 arm에 똑같이 적용(completed·injection_valid·탐지기 준비, 요청 ≥570·공백 ≤30초, 압박 전달 `raw-counter-v2.3`, 동결 조건 일치, 기존 정책 격리, 설명 안 되는 selector 변화 없음, canonical 시작, 관찰 중 문제 없음) + V4 arm identity·임계값·context 일치 + **V8 공통 tick 기록**(시작 기록 actuating_rule = arm·규칙 명세 sha 일치, 모든 scored 평가에 B·C·K 세 판정과 actuating_rule = arm).
- **비교 단위 = 같은 block의 두 run.** primary 쌍(B,C) 유효 = 같은 block의 B·C 둘 다 유효 + 공통 조건 일치. secondary(B,K)·탐색(C,K)도 같다. 한 run 무효는 그 run이 들어간 쌍만 제외.
- **주입 전 승격:** 승격 시각 < 주입 관측 시각이면 그 run이 들어간 쌍은 비교에서 제외하고 arm별로 별도 집계(숨기지 않음). 승격 없는 주입 전 발화는 run을 무효로 하지 않고 규칙별 false-positive evidence(`rule_timeline.pre_injection_condition_met`)로 집계.
- 무효 사유로 쓰지 않는 것: 결과가 나쁨, 규칙이 늦음·발화 없음, 정책 거부, 위반 0초.
- 즉시 중단·정리 오류 처리는 A/B r1 §8·§10 그대로(자동 재시도·재승격·미검증 롤백 없음, 정리 오류는 다음 run 차단).

## 10. ID·경로·승인 (D4)

- plan ID `capstone-guarded-bck-v1-20261007`, 결과 `experiments/results/capstone_guarded_bck_v1/`.
- pilot `bc90b`·`bc90c`·`bc90k` (`counted_in_comparison=false`), probe rep 1990–1992. 공식 `bc01{b,c,k}`–`bc18{b,c,k}`, probe rep `1000 + 3·NN + {b:0,c:1,k:2}` (1003–1058). A/B·pilot rep과 겹치지 않음(테스트).
- 실행 ID = `guarded_pressure_act-<run>-capstone-guarded-bck-v1-20261007`. 같은 ID 재사용 금지(요약·탐지기 원자료 있으면 거부).
- **승인:** 채팅 승인 단위는 1 block. 그러나 guarded act/context 범위는 run별로 격리한다: **record_only → 해당 run 하나만 승인 → act → run → cleanup/canonical → record_only** 복귀 후 다음 arm. 승인 기록 `runs`는 **정확히 `[<그 run ID>]` 하나**여야 시작(코드 강제, 예: bc01b는 `runs:["bc01b"]`만).

## 11. 반복 수·중단 (D1)

- **1단계: 유효 primary 쌍(block) 6, 최대 시도 block 9. 최종: 유효 12, 최대 시도 block 18.** (Latin square 6행 1·2바퀴 → 순서 균형)
- 최종 진행 여부는 일정·서버 상태로만 정하고 1단계 결과로 정하지 않는다.
- 조기 중단(분석기 `stop_rules`가 보고, 사람이 판단): 처음 4 block 시도 중 유효 primary 쌍 <2, 메커니즘 실패(승격 불명·서비스 확인 failed/unknown·D6 보류) 누적 2, 같은 arm 주입 전 승격 2, 안전 문제 반복.
- 시간 추정(추정): block 약 65분 + run별 모드 전환.

## 12. 지표·분석 규칙 (분석기 `experiments/guarded_bck_analysis.py`)

- **주 지표:** run별 관찰 구간 공식 누적 SLO 위반 초 — `slo_episodes.violation_episodes`(slo_judge v3, 수정 없음), 관찰 종료 미회복 = lower-bound 표시.
- **D_primary = C_SLO − B_SLO**, **D_secondary = K_SLO − B_SLO** (양수 = IF 유리). 탐색 `K − C`.
- 유효 쌍 < 6: 기술 통계만. 1단계(≥6, 최종 전): 쌍별 값, 중앙값·범위, 양수/0/음수, 부호검정 p(참고값). 최종(유효 12 또는 시도 18): **primary만 확인 검정 — 정확 Wilcoxon 부호순위(양측, α=0.05, 2^m 열거, midrank) + Hodges–Lehmann.** secondary는 같은 계산을 `confirmatory=false`로 보고. 탐색은 기술 통계만.
- **D=0 쌍(양쪽 0초 포함)은 별도 집계(`zero`)하고 검정에서 제외, 제외 수(`zeros_dropped`) 보고.**
- 민감도: lower-bound 포함 쌍 제외 분석(주 분석 대체 아님).
- 보조(기술 통계만, 사후 primary 승격 금지): injection→trigger, trigger→promotion, promotion→selector, selector→service verified, P50/P95/P99, slow requests(>0.648초), failures, timeouts, max gap, 단계별 원래 Pod 실측 CPU, cleanup 결과, 세 규칙 shadow 판정 시각, 주입 전 shadow 발화 수, 시작 노드.
- 동결 `guarded_ab_analysis`의 `primary`·`secondary`·`stress`·`b_outcome`·`wilcoxon_exact`·`sign_test_exact`·`hodges_lehmann`·`load_corrections`를 import 재사용.
- 자동 판정 correction 규칙 = A/B r2 §6 그대로.

## 13. Pilot bc90 (counted=false, 별도 승인 후에만 실행)

순서 B→C→K. **통과 기준은 결과 우월성이 아니다. SLO 값은 통과 판정에 쓰지 않는다.**
1. common input/evaluation evidence 정상(모든 scored 평가에 세 규칙 판정) 2. 각 run의 actuating 규칙 = arm(구조적 식별) 3. C·K trigger → `/signal/v2` delivered 4. guarded `promoted_verified` 5. selector 전환 6. service verified(new 증가·old 0) 7. 원래 대상 압박 지속 8. D6 cleanup 9. canonical end 10. 분석기가 B/C/K를 정확히 구분(pilot은 집계 0).

**K 미발화 시(D5):** threshold 2.7·압박 조건 변경 금지, 강제 promotion 금지, 추가 자동 retry 금지. pilot을 끝까지 보존·보고하고 "K arm의 live end-to-end promotion path는 이번 pilot에서 직접 실행되지 않았다"고 표시한다. K의 규칙 평가·signal schema·detector_id/hash·guarded context 결속은 unit/integration test로 검증한다(`test_c1_bck_live_detector.py`, `test_run_guarded_bck.py`). 이후 공식 시리즈 진행 여부는 사용자에게 다시 승인받는다.

## 14. 구현 (새 파일만, 동결 파일 수정 없음 — D3)

| 파일 | 내용 |
|---|---|
| `anomaly-detection/bck_rules/rule-spec-v1.json` | 세 규칙 명세 |
| `anomaly-detection/c1_bck_live_detector.py` | 공통 tick 탐지기 |
| `experiments/run_guarded_bck.py` | runner — 동결 runner 부품 import, A/B ID·탐지기·context 고정 때문에 재사용 불가한 orchestration 본문만 옮겨 수정 |
| `experiments/guarded_bck_analysis.py` | 분석기 |
| `experiments/bck_fixed_threshold_calibration.py` (+ 결과 JSON) | C 재현 |
| `test_c1_bck_live_detector.py`, `test_run_guarded_bck.py`(동결 runner와의 characterization 포함), `test_guarded_bck_analysis.py` | 테스트 |

guarded 정책 이미지 변경 없음. Ponytail review(2026-10-07): High 0 / Medium 0 / Low 2(`rule_value()`·`run_name()` 한 줄 helper 축약 제안) — 사용자 결정 accepted/no-change.

## 15. 알려진 한계 (사전 공개)

- **C=1.6842544293089357은 정상 cpu_mean 중앙값(보정 세션 1.622–1.638, 최대 1.7046; 학습 최대 1.702)보다 약 0.05코어 높을 뿐이다.** 보정 자료는 약 2시간. 실제 운영 false-positive 위험이 높을 수 있다. 주입 전 발화가 나와도 임계값을 바꾸지 않는다.
- **K=2.7은 단일 컨테이너 한도 기반 값을 두 Pod 합산 cpu_mean(preview 약 0.02코어)에 적용한다.** 사전에 알고 있던 근거: 독립 검증에서 같은 평가 시점 legacy 규칙(c1 cpu_mean > 2.7, 연속 3)은 선행 중앙값 3.5초, 10회 중 4회 t_slo 이후 발화(`phase8-v33c1-independent-validation-result-final-20261006.md`). 그래서 K는 secondary다.
- 추정(미검증): C는 압박 1단계에서 IF와 비슷한 시점에 발화할 수 있어 B vs C 차이가 0에 가까울 수 있다(0 쌍 다수 → 검정 n 감소).
- 같은 시작 노드 보장 불가(§8). 순서 효과는 Latin square로 균형만 맞추고 통계적으로 분리하지 않는다.
- 승격 arm의 1단계 실측 CPU 감소(A/B에서 관찰, 원래 대상 Pod 부하 이동 추정)는 세 arm 모두에 생길 수 있으며 기술적으로 기록한다.
- 동결 A/B runner의 `check_unresolved`는 B/C/K 결과 폴더를 보지 않는다(동결 파일이라 수정 안 함). B/C/K runner는 기존 폴더 전부 + 자기 폴더를 본다. A/B 재개는 현재 계획에 없다.
- runner orchestration 본문은 동결 runner와 중복이다(D3). characterization 테스트로 동결 runner와 같은 조건에서 같은 결과(요약 키·정리 경로·승격 수·D6 경로·canonical end·증거 집계)를 확인한다.
