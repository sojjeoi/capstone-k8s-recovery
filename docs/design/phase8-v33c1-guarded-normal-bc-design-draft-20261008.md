# 정상 운전 B/C 비교 N1 — 계약 초안 r2 (DRAFT, 동결 아님, 실행 승인 아님) — 2026-10-08

> **대체됨(2026-10-08):** 확정본은 `phase8-v33c1-guarded-normal-bc-n1-contract-20261008.md`. 이 문서는 설계 이력으로만 보존한다.

> r1(같은 날 초안)을 사용자 검토(2026-10-08)대로 고쳤다: 불필요 신호 정의, primary = T_safe, arm 순서 제거, no-grace, N2 보류.
> 라이브 실행·코드 작성·동결은 별도 승인으로만 한다.
> B/C/K 압박 연구(v1.2, bc01–bc06)와 **별개 연구**다. 결과 합산·번호 이어붙이기 금지. 압박 연구는 "stage1 pause, resumable" 그대로.
> 기존 bc01–bc06, v1.2 freeze·임계값·stage1 분석기, bc03b forensic, v3vn 원자료는 수정하지 않는다.

## 0. 설계 전에 알고 있던 자료 (공개 — 설계 참고 전용, N1 반복 수·통계에 포함하지 않음)

v3vn01–03(2026-10-05, 각 3600 s, record-only 섀도)을 C 규칙으로 읽기 전용 재생(2026-10-08):

| 세션 | B 신호 조건(시작 기준 s) | C 3연속(s) |
|---|---|---|
| v3vn01 | 370, 2173, 2618, 2867, 3578 | 없음 |
| v3vn02 | 1893, 1975, 2108, 2222, 2982 | 181 |
| v3vn03 | 없음 | 197 (215까지 연속) |

- C 발생은 시작 후 약 3–3.6분에 몰렸다. **이를 알고 있으므로 primary에서 어떤 시작 구간도 제외하지 않는다(§4).**
- 참고 자료상 T_safe(§3)는 B 370/1893/3600, C 3600/181/197 → 방향이 세션마다 엇갈린다. 결과를 예단하지 않는다.

## A. 연구 질문

> 같은 정상 세션에서 같은 입력·같은 평가 tick으로 판정할 때, Isolation Forest(B)는 보정 CPU 임계값(C)보다
> **불필요한 복구 신호 없이 더 오래 버티는가**(첫 불필요 신호까지 시간이 더 긴가)?

- 압박 연구(B가 반복적으로 약 40–50초 먼저 발화, SLO 결과는 대부분 0 대 0)와 함께 "조기성 대 불필요 대응" trade-off를 해석하기 위한 질문이다. 이 연구 하나로 AIOps 전체 우월을 주장하지 않는다.

## B. 불필요 신호의 정의 (기계 판정)

**신호** = 동결 `c1_bck_live_detector.judge_all`의 규칙별 `condition_met`(3연속, 쿨다운 60 s, skip 시 연속 0) — B·C 각자.

**세션 정상성 상태** (분석기가 아래 순서로 판정, 사람 판단 없음):
1. `invalid` — §G 무효 조건.
2. `unexpected_degradation` — 관찰 구간 [t0, t0+4200 s] 안에 공식 SLO 위반 구간(slo_judge v3, 동결 판정)이 시작되거나, 요청 실패·timeout이 1건 이상. 동결 무주입 안전 함수가 SLO 위반에서 세션을 중단하는 것도 여기에 든다.
3. `normal` — 위 둘이 아님.

**신호별 분류** (모든 세션의 모든 B·C 신호에 적용, W = 600 s):
| 분류 | 조건 |
|---|---|
| `unnecessary` | 신호 시각 t 이후 (t, t+W]에 SLO 위반 시작·요청 실패 없음, [t−60 s, t+W]에 인프라 이상 증거 없음, (t, t+W] 전체가 관찰됨 |
| `true_warning_candidate` | (t, t+W] 안에 SLO 위반 시작 또는 요청 실패, 같은 창에 인프라 이상 증거 없음 |
| `during_degradation` | t에 SLO 위반 구간이 이미 진행 중 |
| `indeterminate` | [t−60 s, t+W]에 인프라 이상 증거 있음, 또는 (t, t+W]가 다 관찰되지 않음(세션 조기 종료) |

- 인프라 이상 증거(기존 evidence에서 기계 추출): vllm-serving 네임스페이스 Warning 이벤트, active·preview Pod 재시작·OOM·Ready 해제, 노드 조건 이상, run_guard 메모리 하락·읽기 실패, SSH 디스크 점검 실패, 탐지기 입력 skip.
- W = 600 s 근거(결과 무관): 모든 guarded 연구의 고정 관찰 길이(`rt.OBSERVATION_SEC`)이고, 독립 검증에서 IF 선행 시간 최댓값(191.4 s)의 3배 이상이다.
- `normal` 세션은 정의상 모든 신호가 `unnecessary`다(관찰 끝 3600 s + 추적 600 s까지 위반·실패 없음).

## C. Primary — T_safe (같은 세션 짝)

- t0 = 탐지기 첫 scored 평가 완료 시각. horizon H = 3600 s.
- **T_safe(arm) = min(첫 `unnecessary` 신호 시각 − t0, 3600 s)**. 불필요 신호 없음 → 3600.
- 분석 집합 = `normal` 세션. D = T_safe(B) − T_safe(C) (양수 = B가 더 오래 버팀).
- T_safe는 3600에서 잘린 "제한 시간"으로, 정의상 모든 세션에서 완전히 관측된다(검열 없는 값). 둘 다 3600이면 D = 0(동률).

## D. Secondary·Diagnostic

| 구분 | 지표 |
|---|---|
| Secondary | 세션 단위 불필요 신호 발생 여부(H 안, 예/아니오), 짝 불일치 수 |
| Secondary | 첫 신호까지 시간(분류 무관, H에서 검열) |
| Secondary | 시간당 신호 수(쿨다운 규칙 그대로) — 이 수는 act 운영의 승격 횟수와 대응하지 않는다(첫 승격 뒤 평가 중단) |
| Diagnostic | 신호 시각 분포(5분 구간 히스토그램), preview Ready 기준 상대 시각 — 시작 구간 집중 여부를 **기술만** |
| Diagnostic | 모든 세션의 신호별 분류 수(`true_warning_candidate`·`indeterminate` 포함), `unexpected_degradation` 세션 상세 |
| Diagnostic | K shadow 판정(공통 tick이 자동 기록), cpu_mean·score 분포 |

## E. No-grace

- primary·secondary는 t0부터 모든 평가를 쓴다. 시작 구간 제외 없음.
- 시작 구간 timing은 D의 diagnostic으로만 보고.
- warm-up 민감도 분석은 지금 제안하지 않는다. 나중에 제안하려면 결과와 무관한 근거(예: 측정된 preview 기동 사건 시각)가 있어야 하고 탐색으로만, primary 결론을 바꾸는 데 쓰지 않는다.

## F. 세션 구조 (N1)

1. fresh preflight(압박 연구 gate와 같은 항목: v1.0 normal manifest·v1.2 상속 해시, 노드·Pressure·SSH·전원·시계, canonical Healthy, gen=observed, active Ready·restart 0, selector·endpoint, scaleDownDelay 30, guarded record_only·미해결 0·context 없음, marker·잔여 0, run ID 미사용).
2. 기존 recovery-policy 격리(동결 `old_policy_isolation`) — Alertmanager 경로 승격 차단.
3. preview 준비(동결 injector.prepare, 압박 연구와 같은 시작 상태 active+preview). scaleDownDelay 변경 없음(승격 없음).
4. probe 1 RPS·max_tokens=1·같은 설정 sha256·같은 이미지.
5. **공통 tick shadow 탐지기 시작 — 신호 송신 없음, 실제 arm 없음.** B·C(·K) 판정을 같은 레코드에 기록.
6. 첫 scored 평가 = t0(상한 300 s, 초과 시 invalid). t0부터 4200 s 관찰(H 3600 + 추적 600).
7. 동결 무주입 안전 감시 그대로(probe 생존, Pod·노드 이상, SLO 위반 시 즉시 중단).
8. 정리: preview 정리·canonical 복원(동결 경로), 탐지기·probe·port-forward 종료, record_only 확인, 기존 정책 복원은 block 끝에.
- **arm 순서 없음**(B·C를 같은 tick에서 동시에 계산). 세션 = 1쌍.
- 기록: session ID, 시작 active 노드·preview 노드, 입력 격자(창·Pod UID·completeness·freshness), 평가별 B·C 판정, 첫·모든 신호 시각, SLO 타임라인(probe CSV·slo_episodes), 인프라 이상 증거, 코드 provenance.
- 노드: 승격이 없어 시작 노드는 이전 상태를 따른다. 노드 균형을 위한 조작·승격은 하지 않고 기록만(한계로 보고).
- 실행 ID: `nb01`–`nb09`(arm 접미사 없음), pilot `nb90`(counted=false). rep 2001–2009, 2090. 결과 `experiments/results/capstone_guarded_normal_bc_v1/`.

## G. 유효·무효·indeterminate

- **invalid**(시도 소모, 결과와 무관하게 기계 판정): 사전검사 실패 뒤 미시작, 탐지기 준비 300 s 초과, telemetry-ready 실패, 평가 기록 공백 > 30 s 또는 평가 가능(scored) 비율 < 독립 검증 계약 입력 품질 기준, Prometheus 수집 공백, 노드 NotReady/Pressure, active·preview Pod 재시작·OOM, SSH·증거 수집 실패, 외부 전원 상실, runner·하네스 crash, 세션 전 stale context·marker·미해결, 실행 ID 중복, 기존 정책 격리 실패, 정리 오류(다음 세션 차단).
- **unexpected_degradation**: §B. 유효 실행이지만 primary 분석 집합에서 빠지고 시도 1회를 소모. record_only라 B·C 판정이 서비스에 영향을 줄 수 없으므로 이 제외는 두 arm에 중립이다.
- **indeterminate**: 신호 단위 분류(§B). 세션 단위 아님.
- 결과(누가 더 빨리 발화했는지)를 보고 무효·제외로 돌릴 수 없다. 분석기 규칙 외 수동 분류 금지.
- stop rule(보고 후 사람 확인, 자동 중단 아님): invalid 2회 연속, unexpected_degradation 2회, 정리 오류 1회.

## H. 통계

- 유효 `normal` 세션 < 6: 기술 통계만(쌍별 T_safe, D, 중앙값·범위, B 우세/동률/C 우세 수).
- 6 도달: **정확 Wilcoxon 부호순위(양측, α = 0.05, 2^m 열거, midrank, D = 0 제외하고 제외 수 보고)** + **Hodges–Lehmann 추정치** + 평균 D(제한 평균 시간 차) + 쌍별 값 전부. 동결 `guarded_ab_analysis.wilcoxon_exact`·`hodges_lehmann`·`sign_test_exact` import.
- secondary 예/아니오: 불일치 쌍 (B만 / C만)에 정확 McNemar(= 불일치 쌍 이항 부호검정, 양측) — 참고값.
- 첫 신호 시간: Kaplan–Meier 기술(검정 없음). 시간당 신호 수: 기술.
- **작은 n 한계(계약 명시):** 0이 아닌 쌍 수 m에서 양측 최소 p = 2/2^m → m=6이면 0.03125, **m ≤ 5면 0.0625 이상이라 α = 0.05에 도달 불가.** 동률(둘 다 3600)이 하나라도 있으면 유의 판정은 불가능하다. 이 경우 효과크기와 방향만 보고한다. 유의하더라도 일반화·비율 추정 금지.

## L. 해석 구조 (사전 등록)

- 압박(확정 사실): B는 C보다 반복적으로 먼저 발화(6쌍에서 약 40–50 s), 사용자 SLO 추가 이득은 확인되지 않음(D(C−B) 0이 5, 음수 1), 비열등 마진 미등록.
- 정상 N1 결과별 사전 문장:
  1. B 우세 + 정확 검정 p < 0.05 → "같은 정상 세션에서 B가 C보다 불필요 신호 없이 더 오래 유지됐다(6세션)." + 압박과 합쳐 "B는 더 이르게 반응하면서 정상 불필요 신호도 적었다(작은 n)."
  2. B 우세, 검정 미도달 → "방향은 B 우세였으나 6세션에서 확인 수준에 이르지 못했다."
  3. 차이 없음(동률 다수) → "정상 불필요 신호에서 차이를 보이지 못했다. B의 조기성은 정상 비용 증가 없이 관찰됐다."(비용 증가 없음이 기술적으로 맞을 때만)
  4. C 우세 → "B는 압박에서 먼저 반응했지만 정상 운전에서 더 일찍 불필요 신호를 냈다 — 조기성과 불필요 대응 비용의 trade-off."
- 어느 경우도 "AIOps가 우월하다"는 단독 결론을 쓰지 않는다.

## I. 6 유효 / 최대 9 근거

- 관찰 3600 s = v3vn 무주입과 같은 시간 규모(동결 독립 검증 계약).
- 6 유효 / 9 시도 = 압박 stage1과 같은 구조(사전 등록 일관성).
- 시간: 세션 1회 약 85분(준비 약 8 + 관찰 70 + 정리 약 5) → 유효 6이면 약 8.5시간, 최대 9회 약 13시간. 시연 2026-10-15 전 완료 가능, 대회 2026-10-30.
- v3vn 자료는 포함하지 않는다.

## J. N2 (실제 승격 비용) — 보류

- 이번 freeze 범위 밖. N1 완료 뒤 불필요 신호 발생 실태를 보고 가치가 있으면 별도 설계·승인.
- N2는 B/C 우열 근거가 아니라 "불필요 승격 1회의 실제 운영 비용" 보조 실험이다.

## K. 최소 구현 파일 (아직 작성 안 함)

| 파일 | 내용 |
|---|---|
| `anomaly-detection/c1_bck_shadow_pair.py` | 동결 `evaluate_once`·`judge_all`·`load_spec`·`load_artifacts` import, 송신 없는 평가 루프·기록(약 40줄). 동결 `c1_bck_live_detector`는 CLI가 act만 받고 실제 arm 신호를 항상 보내므로 N1에 그대로 못 쓴다 |
| `experiments/run_guarded_normal_bc.py` | 압박 runner 순서 복사, 주입 없음, record_only. 동결 `run_no_injection_smoke`(관찰 4200 s, detector_ready_fn)·무주입 안전 함수·deps 조립·cleanup gate·provenance 함수 import |
| `experiments/guarded_normal_bc_analysis.py` | 세션 상태·신호 분류·T_safe·통계(동결 통계 함수 import) |
| 테스트 3개 | 위 세 파일 |
| 문서 | 이 계약 확정본, 분석 규칙, freeze manifest(새 family, v1.2 manifest는 읽기 전용 상속 확인) |

- 변경 없음: `guarded_pilot_session.ps1`(-Mode record_only 지원), recovery-policy 이미지, v1.2 파일 전부, 동결 무주입 trial·안전 함수.
