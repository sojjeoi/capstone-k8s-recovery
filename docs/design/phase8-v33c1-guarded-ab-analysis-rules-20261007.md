# A/B 공식 분석 규칙 (계약 r2 부속, analysis-rules-20261007) — 첫 공식 결과 전 고정

작성: 2026-10-07(KST). 계약 r1 §8·§9와 r2를 **코드로 옮길 때 생기는 해석 빈칸만** 채운다. 새 지표·새 검정·새 무효 사유를 추가하지 않는다. 구현: `experiments/guarded_ab_analysis.py`(sha256은 `…-analysis-rules-20261007.sha256.txt`에 기록). 이 문서·분석기는 공식 ab01 결과를 보기 전에 동결하며, 바꾸려면 계약과 같은 개정 절차(이유·전후 해시·모든 쌍 동일 적용)를 따른다.

## 1. 공식 집계 포함 (모두 만족해야 포함 — 하나만으로 포함하지 않음)
1. 실행 ID가 `ab01a`~`ab15b`(정규식 `ab(0[1-9]|1[0-5])[ab]`). ab00·ab90·gap·grp·grn·과거 V1/V2·검증 자료는 이름 단계에서 제외.
2. 실행 계약 `plan_id` = `capstone-guarded-ab-v1-20261007`, `contract_doc` = r2 문서.
3. 실행 계약과 요약 둘 다 `comparison.counted_in_comparison` = true, `comparison.pair` = ID 앞 4글자.
4. arm 일치: ID 끝 글자(a/b) ↔ `comparison.arm`(A_no_switch/B_if_switch) ↔ 요약·계약 `mode`(record_only/act). 하나라도 다르면 `analysis_unknown`(집계 제외, 추측하지 않음). `outcome_reason` 등 자연어는 표시용이며 판정에 쓰지 않는다.

## 2. 실행 유효성 (계약 r1 §8 + r2 §1을 그대로)
- V1 `outcome_status`=completed, `trial_outcome.injection_valid`=true, 탐지기 준비 기록 있음.
- V2 주 지표 계산 가능: `slo_episodes.violation_episodes` computable(관찰 구간 요청 ≥570, 구간 양끝 포함 공백 ≤30초).
- V3 압박 전달 `raw-counter-v2.3-guarded-ab-20261007`: `timed_switch_verdict.evaluate_stress_delivery_raw`(단계 수 4, 기준선+`MIN_EFFECTIVE_CPU_RISE_CORES`, B 전환 시 continuity_from=전환 확인 시각)를 그대로 호출하고, 생존 검사만 압박 도구 기록(모든 단계 completed, `end_reason`=completed, `target_lost` 거짓)으로 대체한다. 판정: 단계 평균 미달 또는 대상 소실 → false, 그 밖 판정 불가 사유(공백·리셋·관측 부족) → 불명(무효), 아니면 true.
- V4 동결값: threshold = −0.07667538512242333, 아티팩트 해시 = 동결 파일, `conditions`(압박 단계·probe 설정 sha·이미지·탐지 규칙 3연속/60초·시작 상태 규칙) = 계약값, 관찰 600초, probe 이미지 실행 중 확인 통과.
- V5 개입 없음: 기존 정책 시작·끝 격리 true. A: 승격 조치 없음 + 미승격 복원 최종 active·Pod UID = 시작 canonical 값. B: 끝 active ≠ 시작 active이면 이 실행의 승격(조치 verified, 대상 new = 끝 active)으로 설명돼야 하고, 같으면 승격이 없어야 한다.
- V6 시작 canonical: 이번 실행 run-preflight(`summary.preflight_file`)의 `ok`=true, `canonical_start.problems`=[].
- V7 관찰 중 문제: `harness_error` 또는 정리 오류 `harness_error*`·`trial_cleanup_incomplete*`·`old_recovery_policy_not_isolated_at_end*` → 무효. 그 밖의 정리 오류는 측정을 무효로 하지 않고 `post_observation_cleanup_errors`와 `next_run_independence_risk`로 따로 기록.
- 무효 사유가 아닌 것: 결과 값, IF 신호 없음·늦음, 정책 거부, B 승격 없음, B의 SLO 위반, A의 위반 없음.
- **주입 전 승격(계약 r1 §3 예외):** B의 `promote_called_at` < `t_injection_observed_utc`이면 그 쌍을 비교에서 빼고 `pre_injection_promotion`으로 센다.
- **B 메커니즘 실패(계약 r1 §7 조기 중단용 집계):** B에서 조치 결과 promoted_unverified/promotion_unknown, 서비스 확인 결과가 verified가 아님(승격했을 때), 또는 D6 보류.

## 3. 쌍
- 쌍 = 같은 번호 a·b. 둘 다 포함·유효해야 유효 쌍. 하나만 있으면 `incomplete`.
- 쌍 일치 확인: 같은 contract_doc, threshold, 아티팩트 해시, `conditions`, 관찰 시간, B 시작 노드 = A 시작 노드 = B `expected_start_active_node`. 다르면 쌍 무효(원인 기록).
- 시도 수 = A 요약이 있는 공식 쌍 수(A가 사전검사를 통과해 시작된 쌍).

## 4. 지표
- 주 지표: 실행별 `slo_episodes.violation_episodes`(slo_judge v3, 수정 없음)의 `cumulative_sec`, 첫 `t_slo`, 구간 목록, `cumulative_lower_bound`(관찰 종료까지 회복 미확인=하한). 위반 없음 = 0.
- **쌍 효과 D = A 누적 − B 누적(초). 양수 = B의 위반시간이 더 적음.**
- 보조(기술 통계만): 관찰 구간(주입 관측~관찰 종료) 전송 요청 기준 — 요청 수, 성공, 실패, timeout(= `status`가 숫자가 아닌 실패), 지연 P50/P95/P99(`statistics.quantiles(n=100)` 기본 방식, slo_judge와 같음)·최대, 느린 요청(지연 > 2×L_BASELINE = 0.648초), 최대 요청 공백. B 조치 시각들. 쌍 보조 차이도 A − B로만 표시하고 검정하지 않는다.

## 5. 통계 (계약 r1 §9의 빈칸 고정)
- 단계: 유효 쌍 < 5 → 기술 통계만. 5 ≤ 유효 쌍이고 최종 아님 → 1단계 기술 통계(쌍별 A·B·D, D 중앙값·범위, B<A/B=A/B>A 수) + 부호 검정 정확 양측 p(참고값, 0 제외). **최종 = 유효 쌍 ≥ 10 또는 시도 15 도달** → Wilcoxon 부호순위 정확 양측(α=0.05) 1개.
- Wilcoxon: D=0 제외(제외 수 보고). |D| 동점은 평균 순위. 정확 분포 = 0이 아닌 m개 부호의 2^m 경우 전부 열거(조건부 정확 검정). 통계량 T+ = 양수 D 순위 합, p = |T − 기대값| ≥ |T관측 − 기대값|(허용오차 1e-9)인 경우 비율. 최종인데 유효 쌍 < 5이면 검정하지 않는다.
- 효과 크기: D 중앙값, Hodges–Lehmann = 모든 유효 쌍 D(0 포함)의 Walsh 평균 (D_i+D_j)/2 (i ≤ j) 중앙값.
- 하한: 주 분석은 값 그대로 + 쌍별 하한 표시. 민감도(주 분석 대체 아님) = A·B 어느 쪽이든 하한이 있는 쌍을 뺀 같은 통계.
- 결측·무효·incomplete·주입 전 승격 쌍은 통계에 넣지 않고 목록·사유로 보고.

## 6. 출처·재현·정정
- 결과에 분석기 sha256, 계약 r1·r2·이 문서 sha256, slo_judge·slo_episodes·timed_switch_verdict sha256, 모델 아티팩트 해시·threshold, 실행별 원자료 경로·sha256, 포함/제외 실행과 사유를 남긴다. 출력 파일은 새 이름으로만 쓴다(덮어쓰기 없음).
- 같은 원자료 재실행 → 같은 결과: 분석 시각을 뺀 결과의 sha256(`result_digest`)이 같아야 한다.
- 정정 기록(`<결과 폴더>/corrections/*.json`): 필수 필드 `original_verdict_path`, `original_verdict_sha256`, `error_type`, `contract_basis`, `corrected_interpretation`, `applies_to`="all_runs". 원본 파일이 있고 해시가 맞아야 받아들인다(아니면 rejected로 기록). 정정은 이미 분석기 코드에 반영된 결정적 구현 수정을 **문서로 연결**할 뿐, 실행별로 판정을 바꾸는 입력이 아니다(특정 arm·결과만 바꾸는 경로 없음). 원본 판정 파일은 읽기만 한다. generation 비교는 `guarded_cleanup_gate.norm_generation`을 쓴다.
