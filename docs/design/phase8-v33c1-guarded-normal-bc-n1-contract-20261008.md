# 정상 운전 B/C 비교 N1 — 계약 (freeze 후보, 2026-10-08)

> 사용자 승인(2026-10-08): 계약 확정 → 최소 구현 → 테스트 → counted=false pilot 준비. **공식 N1 세션 실행은 별도 승인.**
> 설계 이력: `phase8-v33c1-guarded-normal-bc-design-draft-20261008.md`(r1·r2 초안).
> B/C/K 압박 연구(v1.2, bc01–bc06)와 별개. 결과 합산·번호 이어붙이기 금지. 압박 연구는 "stage1 pause, resumable".
> 수정 금지: B/C/K v1.2 파일·manifest, bc01–bc06 원자료·분석, 임계값, c1 모델, recovery-policy 이미지, bc03b forensic, v3vn 원자료.

## 1. 질문

같은 정상 세션에서 같은 입력·같은 평가 tick으로 판정할 때, IF(B)는 보정 CPU 임계값(C)보다 **불필요한 신호 없이 더 오래 유지되는가**?
압박 연구(B가 반복적으로 약 40–50 s 먼저 발화, SLO 추가 이득 미확인, 비열등 마진 미등록)와 함께 "조기성 대 불필요 대응 비용"을 해석한다. 이 연구 하나로 AIOps 전체 우월을 주장하지 않는다.

설계 전 알고 있던 자료(공개): v3vn01–03 읽기 전용 재생 — B 신호 조건 10회(5·5·0), C 3연속 2회(시작 후 약 181–215 s). 설계 참고 전용, N1 반복 수·통계에 포함하지 않는다.

## 2. arm

| arm | 판정 | 출처 |
|---|---|---|
| B | IF score < −0.07667538512242333, 3연속, 쿨다운 60 s | rule-spec-v1(동결) |
| C | cpu_mean > 1.6842544293089357, 3연속, 쿨다운 60 s | rule-spec-v1(동결) |
| K | cpu_mean > 2.7 — shadow 진단 기록만 | rule-spec-v1(동결) |

공통: 동결 `evaluate_once` 1회/tick → 동결 `judge_all`(규칙별 독립 연속·쿨다운, skip 시 연속 0). c1 입력 규칙 그대로(expected Pod/UID, full grid, freshness, 불완전 입력 skip, streak reset). rate window·scrape 설정 변경 없음.

## 3. 정상 세션 (N1)

- 주입·Chaos·Pod kill·네트워크 저하 없음. 새 부하 없음. 압박 연구와 같은 시작 상태(canonical Healthy, active + preview 준비, probe 1 RPS·max_tokens=1·같은 설정 sha256·같은 이미지).
- guarded 정책 record_only. **신호 송신 없음, 실제 arm 없음, arm 순서 없음.** 정책 context를 설정하지 않는다. scaleDownDelay 변경 없음. 기존 recovery-policy 격리.
- t0 = 탐지기 첫 scored 평가 완료 시각(준비 상한 300 s). 관찰 = 4200 s.
  - **판정 창 [t0, t0+3600)**: 이 안의 신호만 primary·secondary에 쓴다.
  - **추적 전용 [t0+3600, t0+4200]**: 판정 창 신호의 W = 600 s 추적 확보용. 여기서 새로 난 신호는 T_safe·횟수·세션 결과에 넣지 않는다(예: t=3599 신호 → t=4199까지 추적).
- grace/warm-up 제외 없음. 시작 구간 timing은 진단 보고만.
- 시작 active 노드·preview 노드·Pod UID 기록. 노드 균형용 승격 금지(쏠리면 한계로 보고).
- pilot `nb90`: 관찰 600 s, counted=false. 목적 = 같은 tick B/C 평가, 송신 없음, 기록, provenance, cleanup/canonical, 분석기 입력 형식. pilot 결과로 임계값·모델·지표·통계를 바꾸지 않고 정상 결과를 주장하지 않는다.

## 4. 세션 상태 (분석기 기계 판정)

1. **invalid** — arm 판단과 무관한 인프라·증거 실패: 파일 누락, 기록 완전성 위반(독립 검증 계약 §5 상속: 시작 기록 1개·run_id 일치·깨진 줄 없음·평가 번호 연속·시각 역행 없음·**기록 공백 ≤ 30 s**), 정리·하네스 오류, probe 이미지 미확인, 창 정보 누락, 탐지기 미시작, 계약 불일치, SLO 위반 외 사유의 중단(노드·Pod·탐지기·probe·전원 등), completed인데 probe 판정 불가(요청 < 관찰 초 × 0.95 또는 공백 > 30 s, slo_episodes r2)·probe 공백·관찰 부족·평가 기록이 관찰 끝 30 s 전까지 미도달.
2. **unexpected_degradation** — 판정·추적 구간 [t0, 관찰 끝]에 **동결 공식 SLO 판정(slo_judge v3·slo_episodes r2)** 위반 구간이 있음. 새 SLO 정의 없음. 무주입 안전 감시가 공식 판정으로 중단한 세션도 여기.
   - 참고(동결 판정의 성질): 60 s 창 가용성 99% 기준이라 1 RPS에서 실패 1건도 공식 위반이 된다. 실패는 별도로 `service_event`로도 기록한다.
3. **normal** — 위 둘이 아님.

- unexpected_degradation·invalid는 시도 1회를 소모하고 primary 짝 수에는 넣지 않는다(결과는 별도 보존). record_only라 B·C 판정이 서비스에 영향을 줄 수 없으므로 이 제외는 두 arm에 중립이다.
- 결과(누가 먼저 발화했는지)는 상태 판정에 쓰지 않는다. 사람이 상태·분류를 바꾸는 입력은 없다.

## 5. 신호 분류 (판정 창 안 모든 신호, W = 600 s, 아래 순서로 첫 해당)

| 분류 | 조건 |
|---|---|
| `during_degradation` | 신호 시각 t에 공식 SLO 위반 구간 진행 중 |
| `indeterminate` | (t, t+W]가 관찰 끝 전에 다 관찰되지 않음, 또는 [t−60 s, t+W]에 인프라 이상 증거(K8s Warning 이벤트, 탐지기 입력 skip) |
| `true_warning_candidate` | (t, t+W]에 공식 SLO 위반 시작 또는 service_event(요청 실패·timeout) — **시간 선행 관계만 뜻함, 예측 성공 확정 아님** |
| `unnecessary` | 위 어느 것도 아님(600 s 완전 관찰, 공식 위반·service_event·인프라 이상 없음) |

- 60 s = c1 입력 창(신호를 만든 입력 구간). W = 600 s = 모든 guarded 연구의 고정 관찰 길이, 독립 검증 IF 선행 최대(191.4 s)의 3배 이상.

## 6. 지표

- **Primary**: T_safe(arm) = min(첫 `unnecessary` 신호 − t0, 3600 s). 없으면 3600. **D = T_safe(B) − T_safe(C)** (D > 0: B가 더 오래 유지, D < 0: C가 더 오래 유지). 분석 집합 = normal 세션.
- Secondary: 세션 단위 unnecessary 발생 여부(예/아니오), 첫 raw trigger까지 시간, raw trigger 수/시간(**실제 승격 횟수로 해석 금지** — act에서는 첫 승격 뒤 평가가 멈춘다).
- Diagnostic: 5분 구간 trigger timing, preview Ready(vllm 시작) 기준 상대 시각, 모든 신호 분류, service_event, 공식 SLO 타임라인, 인프라 이상 증거, K shadow, 입력 격자(scored/skip, 공백).

## 7. 통계 (사전 고정, 동률이 많아도 사후 변경 없음)

- 유효 normal 세션 < 6: 기술 통계(쌍별 T_safe·D, 평균·중앙값·범위, B 우세/동률/C 우세).
- 6 도달: **정확 Wilcoxon 부호순위**(양측, α 0.05, 2^m 열거, midrank, D = 0 제외 수 보고) + **Hodges–Lehmann** + 평균·중앙값 D. 예/아니오는 **정확 McNemar 참고**(불일치 쌍 이항, 양측). 동결 `guarded_ab_analysis.wilcoxon_exact·hodges_lehmann·sign_test_exact` 사용.
- **작은 n 한계:** 0이 아닌 쌍 m의 양측 최소 p = 2/2^m → m=6이면 0.03125, m ≤ 5면 0.0625 이상이라 α 0.05에 도달할 수 없다. 동률(둘 다 3600)이 하나라도 있으면 유의 판정 불가 → 방향·효과크기만 보고. 유의해도 일반화·비율 추정 금지.

## 8. 해석 문장 (사전 등록)

- 압박(사실): B는 C보다 반복적으로 먼저 발화, SLO 추가 이득 미확인, 비열등 마진 미등록.
- 정상 결과별: ① B 우세 + p < 0.05 → "같은 정상 세션에서 B가 C보다 불필요 신호 없이 더 오래 유지됐다(6세션)." ② B 우세, 미도달 → "방향은 B 우세, 6세션에서 확인 수준 미도달." ③ 차이 없음 → "정상 불필요 신호에서 차이를 보이지 못했다." ④ C 우세 → "B는 압박에서 먼저 반응했지만 정상 운전에서 더 일찍 불필요 신호를 냈다 — 조기성과 불필요 대응 비용의 trade-off."
- 어느 경우에도 "AIOps가 우월하다" 단독 결론 금지.

## 9. 규모·순서·중단

- 공식 `nb01`–`nb09`(rep 2001–2009), 유효 normal 목표 6, 최대 시도 9. `nbNN`은 `nb(NN−1)` 시도 뒤에만(runner 강제). 6 도달 시 더 시도하지 않는다.
- stop rule(보고 후 사람 확인): invalid 2회 연속, unexpected_degradation 2회, 정리 오류 1회.
- 실행 ID 재사용·같은 ID 재시도 금지. 실행마다 전원 Online·fresh preflight. 실행은 PowerShell `Start-Process` + 세션 실행기.

## 10. N2

실제 승격 비용 연구는 보류. N1 공식 결과 검토 전 구현·실행 금지.

## 11. 코드 (구현 완료, 동결 대상)

| 파일 | 역할 |
|---|---|
| `anomaly-detection/c1_bck_shadow_pair.py` | 공통 tick 섀도(동결 evaluate_once·judge_all·load_spec import, 송신 없음) |
| `experiments/run_guarded_normal_bc.py` | 실행기(동결 run_guarded no_injection·record_only 순서, canonical r2, provenance fail-closed) |
| `experiments/guarded_normal_bc_analysis.py` | 세션 상태·신호 분류·T_safe·통계 |
| 테스트 3개 | `test_c1_bck_shadow_pair.py`, `test_run_guarded_normal_bc.py`, `test_guarded_normal_bc_analysis.py` |

freeze manifest: `phase8-v33c1-guarded-normal-bc-n1-freeze-manifest-v1-20261008.json`(버전 `n1-v1`, 자기 해시 없음). runner는 실행 전 manifest의 모든 파일을 다시 해시하고 `status = frozen`이 아니면 시작하지 않는다.
