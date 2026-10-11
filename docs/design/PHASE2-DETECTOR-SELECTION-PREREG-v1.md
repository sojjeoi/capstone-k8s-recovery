# Phase 2 사전등록 v1 — Environment-specific Detector Selection + Detector-Agnostic Recovery

> 상태: **DESIGN FREEZE 대상 rev 5**(2026-10-11). rev 3(correctness review High/Medium 0) 위에 사용자 승인 조건 9개(Gate 사전식 우선순위, 사전 정보 공개, 외부 artifact 공급 계약, Stage 1 격리 계약, 2단계 freeze, 선택 결과 상태, Stage 2 진입 조건, 일정 분리)를 반영했다(rev 4). rev 4 correctness review(High 1·Medium 8·Low 8)와 재검토 2회(Medium 4·Low 2, Medium 2·Low 1)를 반영했다(rev 5). 동결 기록은 freeze 시점에 같은 폴더의 `phase2-detector-selection-design-freeze.json`으로 만든다. 이 문서 커밋 SHA를 참조하는 후속 커밋에 들어간다. 이 문서 안에는 자기 해시를 두지 않는다.
> 기준: canonical master `ab8d994`, Phase 1 종료 tag `phase1-final-20261011`(= `e119ce3`).
> DESIGN FREEZE는 구현·실험 승인이 아니다. 구현, 클러스터 실험, push는 단계별로 사용자 승인을 받는다(§18).

## 1. 동기

Phase 1에서 효과를 낸 것은 "검증된 전환"이었다. 어떤 detector가 좋은지는 환경과 조건에 따라 달랐다.

Phase 2는 새 detector를 만드는 연구가 아니다. 다음 구조가 실제로 성립하는지 검증한다.
- 환경마다 사전등록 규칙으로 detector를 고른다.
- 고른 detector만 공통 복구 경로를 움직인다.

CPU 임계값이 선택돼도 성공이다.

## 2. Phase 1 근거 (숫자는 Phase 1 문서 그대로)

| Phase 1 결과 | Phase 2 설계로의 연결 |
|---|---|
| A/B 공식 5쌍: 실제 전환이 누적 SLO 피해를 줄임(A 204.504 / ≥210.83 / 201.68 / 287.79 / 207.396 s → B 0 s) | 복구 경로가 효과의 핵심 → 공통 Recovery Layer로 고정 |
| B/C/K: 단순 CPU 압박에서 보정 CPU 임계값이 강했음(c1 IF > C 증거 없음) | CPU 임계값을 1급 후보로 둠 |
| N1: c1 IF 정상 운전 불필요 신호 문제 | 정상 운전 신뢰성을 선택 기준에 둠(Gate 3) |
| c3-A: 정상 coverage 확장으로 불필요 신호 14 → 1, 사전등록 판정 PROMISING. CPU 임계값 대비 우월성은 입증 안 함 | frozen c3-A를 두 번째 후보로 둠 |
| c3-A 압박 기준 E: 4/5에서 첫 3연속에 주입 전 평가 1개 포함. **주입 뒤 평가만으로 다시 세면 363 s 이내 2/5**(`docs/final/FINAL-RESEARCH-SYNTHESIS-20261011.md` §1) | Phase 2 Gate 2는 주입 뒤 평가만으로 3연속을 센다 |
| B/C/K·A/B: IF 식별(A/B)과 CPU 식별(B/C/K arm C) 신호가 모두 수정 없는 같은 guarded 경로를 지났음 | RQ2의 개발 근거(독립 검증 아님) |
| Axis 2·문헌·S3: 환경에 따라 쓸모 있는 운영 신호 자체가 달라질 수 있음 | detector를 시스템의 고정 핵심으로 두지 않음 |

### 2.1 이미 알려진 사전 정보 (공개)

- **사실**: Phase 1에서 주입 뒤 평가만 쓰면, c3-A가 363 s 이내에 3연속을 만든 세션은 5개 중 2개였다.
- **추정**: Phase 1 압박 조건이 그대로 재현되면 CPU 임계값은 Gate 2(유효 압박 세션 5개 중 4개 이상)를 통과하고, c3-A는 실패할 가능성이 높다.
- Gate 2는 이 사실을 안 상태에서 정했다. 목적은 주입 전 평가가 3연속에 섞이는 carry-over를 없애는 것이다.
- **Stage 1의 의미**: 기존 결과를 보고 만든 selection protocol을 freeze한 뒤, fresh independent evidence에서 그 protocol을 확인하는 단계다. 중립적인 경쟁이 아니다.
- 결과가 위 예상과 달라도(예: c3-A가 Gate 2를 통과하거나 CPU가 실패) 규칙은 바꾸지 않는다(§17).
- 결과를 "IF가 새로 실패했다"는 발견으로 소개하지 않는다.
- Phase 2 판정(§13)은 어느 detector가 선택되는지와 무관하다.

## 3. 연구 질문

- **RQ1**: 대상 LLM serving 환경에서 후보 detector들을 같은 사전등록 기준으로 평가해, 결과를 본 뒤 기준을 바꾸지 않고 그 환경의 active detector를 선택할 수 있는가?
- **RQ2**: active detector의 종류가 달라져도 Recovery Policy → Argo Rollouts → Traffic Switch → Verification 경로를 수정하지 않고 그대로 재사용할 수 있는가?
- **RQ3**: 선택되지 않은 detector를 shadow로 실행해, recovery 권한 없이 같은 telemetry에 대한 비교 기록을 계속 남길 수 있는가?

세 질문 모두 Phase 1 결론("전환은 효과적, detector 적합성은 조건 의존")을 잇는다. 범위는 넓히지 않는다. 새 detector, 자동 학습, 다중 환경 일반화는 질문 밖이다.

## 4. 시스템 구조와 계약

```
Environment
    ↓
Environment Profile (검증된 환경 계약 + detector 역할을 고정하는 설정·증거 파일)
    ↓
Detector Validation / Selection (사전등록 규칙, 오프라인·일회성)
    ↓
Active Detector ──────────┐   Shadow Detector → 기록만(recovery 권한 없음)
                          ↓
                 Recovery Policy (guarded, 기존 코드 수정 없음)
                          ↓
                 Argo Rollouts → Traffic Switch → Verification
```

- Environment Profile은 실행 중에 detector를 자동으로 바꾸는 AI가 아니다. 환경을 검증한 뒤 그 환경에서 쓸 detector를 **고정하는 설정·증거 계약**이다.
- **Recovery Policy 코드는 바꾸지 않는다.** 권한은 세 단계로 강제한다.
  1. **실행기 (1차 방어, 내용까지 보장)**: 송신 payload는 ACTIVE detector의 판정 객체에서만 만든다. SHADOW 판정에는 송신 경로가 없다.
  2. **정책 context의 detector 식별 검사 (2차 방어, 식별만)**:
     - 검사 항목: `detector_id`, `model_version`, `model_hash`, `artifact_manifest_hash`, `threshold` (`guarded_policy.py` 377–388행).
     - ACTIVE가 아닌 식별의 신호는 `rejected_context`가 된다.
     - score와 임계값의 관계는 보지 않는다.
     - profile·context 검증에서 `threshold`는 **필수(None 금지)**다. None이면 정책이 임계값 검사를 건너뛰기 때문이다(386·612–614행).
  3. **`actions_allowed` (424행)**: profile이 유효하지 않으면 `false`로 둔다. 그러면 act 모드 조치가 차단된다(`actions_not_allowed`).
- 구현 단계에서 새로 만들 것(§18 IMPLEMENTATION FREEZE 전, 승인 후):
  - profile 검증·context 생성 모듈
  - detector 판정 정규화
  - selection 함수
  - 같은 tick에서 두 detector를 판정하고 ACTIVE만 송신하는 실행기. 기존 `c1_bck_live_detector.py`의 "전부 판정·하나만 송신" 패턴을 profile 역할로 일반화한다.

### 4.1 detector 판정 기록 (매 tick·모든 detector)

새 schema는 만들지 않는다. 기존 평가 기록의 `rules[<detector_id>]` 항목(`value`, `threshold`, `is_anomalous`, `consecutive`, `condition_met`)에 두 필드만 더한다.
- `state`: `NORMAL` / `ANOMALY` / `UNKNOWN`. 평가 `status`가 scored가 아니거나 입력이 무효면 `UNKNOWN`이다.
- `role`: `ACTIVE` / `SHADOW`. profile에서 온다.

기록 시작 줄(`record_type` start)에는 `environment_profile_id`와 profile 파일 sha256을 남긴다. `UNKNOWN`이거나 평가 번호가 빠지면 연속 수는 0으로 리셋된다. 이는 기존 `judge_all`·`c3_common.replay`와 같다.

정규화 규칙:
- CPU: `ANOMALY` ⇔ `cpu_mean > 1.6842544293089357`.
- IF: `ANOMALY` ⇔ frozen c3-A scaler·모델로 같은 평가 기록의 `features`를 점수화해 `decision_function < −0.1948341270493127`.
- 두 detector는 같은 `evaluation_seq`와 같은 특징값을 쓴다(same-tick parity). c3-A는 c1과 특징 schema가 같아서 가능하다.
- `value`와 `threshold`는 detector마다 의미가 달라서 **기록 전용**이다. 공통 판단(선택 규칙·정책)은 `state`, 연속 수, `condition_met`, 식별만 본다.
- 정책으로 가는 신호(wire)는 기존 `c1-signal-v2` 그대로다. ACTIVE의 `condition_met`일 때만 송신한다.

### 4.2 Environment Profile (JSON 1개)

| 블록 | 필드 |
|---|---|
| `profile_id` | 식별(버전 포함, 예: `cpu-vllm-qwen05b-v1`) |
| `environment` | `serving_backend`(vLLM 버전·이미지 ID), `model`, `compute_type`(CPU/GPU), `resources`(CPU·메모리 request/limit), `serving_args`(Rollout 인자), `workload_contract`(probe 설정 해시), `slo_contract`(`slo_judge.py` 해시·SLO 버전), `telemetry_contract`(지표 질의 정의 해시·scrape 간격) |
| `detectors` | 후보별 `detector_id`, `detector_version`, `artifact`(§5.1 논리 식별·해시만, 경로 없음), `threshold`, `role`(ACTIVE는 0개 또는 1개) |
| `selection` | `rule`(이 문서의 design freeze sha256), `validation_evidence_id`, `selected_at`, `outcome`(§7.4), `gate_results`, `reason` |
| `recovery_policy_sha256` | guarded 정책 소스 해시 |

- **무결성**: profile 파일 자체의 sha256을 세션 시작 기록과 정책 context 설정 기록에 남긴다. 파일 안에 자기 해시는 두지 않는다.
- **이식성**: profile에는 기계별 절대 경로를 두지 않는다(§5.1).
- **ACTIVE 개수**:
  - Stage 1 **selection profile**은 ACTIVE가 0개다(모두 SHADOW). 그래서 recovery가 비활성이다.
  - ACTIVE가 정확히 1개인 profile만 act 모드 context를 만들 수 있다.
  - ACTIVE가 2개 이상이면 무효다.

### 4.3 ACTIVE / SHADOW

- **ACTIVE (최대 1개)**: `condition_met`에서 `c1-signal-v2`를 송신할 수 있다. 정책 context의 detector 식별은 ACTIVE에서만 만든다.
- **SHADOW**: 같은 tick에 판정·기록·비교 증거만 남긴다. 송신 코드 경로가 없다. **SHADOW는 어떤 단계에서도 actionable signal을 보낼 수 없다.**
- 남는 위험: 실행기 버그로 SHADOW 판정이 ACTIVE 식별에 실려도 정책은 막지 못한다(식별만 검사하기 때문). 그래서 실행기 테스트로 막는다(§11 테스트 4).

### 4.4 PROFILE_STALE / PROFILE_INVALID

**PROFILE_STALE.** **각 세션 시작 시** 실제 환경을 읽기 전용으로 읽어 profile의 `environment`, `detectors`, `recovery_policy_sha256`과 비교한다. 다음 중 하나라도 다르면 `PROFILE_STALE`이다.
- serving backend/이미지
- CPU↔GPU
- 모델
- 자원 limit
- Rollout 인자
- workload 계약 해시
- SLO 계약 해시
- telemetry 계약 해시
- detector artifact/버전 해시(ACTIVE·SHADOW 모두)
- guarded 정책 소스 해시

**PROFILE_INVALID.** profile 형식 위반(ACTIVE 2개 이상, threshold None 등) 또는 §5.1 artifact 검사 실패를 말한다.

**두 상태의 처리 (공통)**:
- 그 세션은 non-actionable이다. act context를 만들지 않고(`actions_allowed=false`) 실행기도 시작하지 않는다.
- 수집을 멈추고 사람이 확인한다.
- **다른 detector로 자동 전환하지 않는다.**
- profile을 실제 상태에 맞춰 고쳐서 통과시키지 않는다. 기대 해시는 동결 값 그대로 둔다.
- 이 이유로 시작이 거부된 세션도 시도 수(§9 상한)에 센다.
- 재개하려면 사용자 승인이 필요하다. profile, artifact, IMPLEMENTATION FREEZE는 바꾸지 않은 채여야 한다.
- 재개할 수 없으면 결과는 다음과 같다.
  - Stage 1:
    - 원인이 detector artifact/버전 해시, IMPLEMENTATION FREEZE, 증거 파일 해시 불일치면 `INTEGRITY_FAILURE`
    - 그 밖의 원인이면 `SELECTION_INCOMPLETE`
  - Stage 2: 중단한다. 판정은 그때까지 완료된 유효 세션으로 §8.4 분기를 따른다.
    - 실패한 유효 세션이 없을 때만 `PHASE2_SELECTION_VALIDATED_RECOVERY_PENDING`이 된다. 이유를 기록한다.
    - detector artifact나 IMPLEMENTATION FREEZE 불일치는 무결성 사건으로 따로 보고한다.

**세션 중 변경과 한계.**
- 세션 중 환경 변경(Pod 교체, 재시작, 이미지 변경)은 기존 세션 유효성 규칙이 그 세션을 무효로 만든다. 그 규칙은 c3 정상 정의(Pod UID 불변, 재시작 수 동일)다.
- 주기 검사는 두지 않는다(관찰 구간 ≤ 4200 s).
- 이미 시작된 승격을 되돌리는 장치는 없다. 이 한계는 그대로 기록한다.

## 5. 후보 detector (첫 환경, 2개로 고정)

| 후보 | 정의 | 출처 |
|---|---|---|
| A. 보정 CPU 임계값 | `cpu_mean > 1.6842544293089357`, 3연속, cooldown 60 s | B/C/K 동결 규칙(`anomaly-detection/bck_rules/rule-spec-v1.json` arm C). artifact 파일 없음 |
| B. frozen c3-A IF | `decision_function < −0.1948341270493127`, 3연속, cooldown 60 s | `anomaly-detection/v3/model_c3_normal_coverage/artifacts/` 메타데이터(커밋됨) + 외부 공급 pkl(§5.1) |

새 detector 추가, 임계값 재보정, IF 재학습은 금지한다.

### 5.1 외부 artifact 공급 계약 (c3-A pkl)

- `model.pkl`과 `scaler.pkl`은 커밋하지 않는다(`.gitignore` 유지).
- artifact 루트는 runbook 또는 실행 설정(명령행 인자나 환경 변수)으로 **명시적으로** 준다. 기본 경로를 추측하지 않는다.
- profile에는 다음만 둔다. 기계별 절대 경로는 두지 않는다.

| profile 필드 (c3-A) | 동결 값 (커밋된 `SHA256SUMS.json` 기준) |
|---|---|
| `artifact_id` | `c3-normal-coverage/capstone-c3-normal-coverage-20261010` (`freeze.json`의 series/plan_id) |
| `model_sha256` | `24cec78e658833dd24957ff49a8c39499403db2a7e8a0c7e752aff61ba3668cf` |
| `scaler_sha256` | `7970aa165606bfca1d632af448ddbf502e1596a6fdcbb3d8eaa5393f7da1a67e` |
| `feature_schema_sha256` | `9d8ddb6eb45448361015f58c42c7189af59b649b5f52781c677b8f33a45fbc51` |
| `threshold_id` / `threshold_sha256` | `−0.1948341270493127` / `threshold.json` `47eb6ff2c9618586e788507f45b10c1bd5d78ac7a32ad3dc4d551b2abf2266dd` |

CPU 후보는 `rule-spec-v1.json` sha256과 arm C threshold 값을 같은 방식으로 둔다.

**실행 시 검사(세션 시작 전, 매번)**:
1. 파일 존재 확인
2. 각 파일 SHA-256 계산
3. profile 기대값과 일치 확인
4. `freeze.json` series/plan_id와 `artifact_id` 일치 확인
5. 기록된 sklearn 버전(1.9.0)과 실행 환경 sklearn 버전 일치 확인(pickle 호환)
6. 평가 기록 `features` 키 집합이 `feature-schema.json`과 일치 확인
7. `threshold.json` 값 = profile `threshold` = 정책 context `threshold` 확인

**fail-closed**: 아래 중 하나라도 해당하면 `PROFILE_INVALID`(§4.4)다. 그 결과는 non-actionable이고 recovery 권한이 없다.
- artifact 없음
- 해시 불일치
- 버전 불일치
- schema 불일치
- threshold 불일치

## 6. 증거 사용 범위

- **Phase 1 증거**: 설계, 규칙 개발, bootstrap profile, 시연에만 쓴다.
  - **Phase 2 독립 검증이라고 부르지 않는다.** 선택 규칙이 그 결과를 본 뒤 만들어졌기 때문이다(§2.1 사전 정보 포함).
- **Phase 2 공식 판정**: IMPLEMENTATION FREEZE(§18) **뒤** 새로 수집한 세션만 쓴다.
  - 두 후보의 보정·학습 데이터(c1ca01–04, c3n03·c3n07 등)는 새 세션과 겹치지 않는다.

## 7. 선택 규칙 (가중 점수 없음)

### 7.1 사전식(lexicographic) 우선순위

**순서**: 세션 유효성 → Gate 1 → Gate 2 → Gate 3 → Tie-break 1(시각) → Tie-break 2(단순성).
- 앞 단계에서 갈리면 뒤 단계는 선택에 쓰지 않는다.
- 뒤 단계의 우위로 앞 단계 실패를 보상하지 않는다.
- 결과 상태의 판정 순서는 §7.4 표가 우선한다. 무결성을 가장 먼저 본다.
- 선택은 기록된 `features`를 동결 artifact로 다시 재생한 오프라인 결과만 쓴다. live SHADOW 상태는 RQ3 기록용이며 선택에 쓰지 않는다.

**Gate 2는 필수 안전·적격성 요건이다.**
- Gate 2에 실패한 detector는 정상 운전에서 더 조용해도 **ACTIVE가 될 수 없다.**
- 이 규칙은 Phase 2 Stage 1 데이터를 보기 전에 정한 safety-first 정책이다. Stage 1 결과를 보고 만든 사후 규칙이 아니다. 압박에서 제때 신호를 못 내는 detector에 복구 권한을 주지 않는다는 뜻이다.
- 규칙이 Phase 1 결과를 본 뒤 정해졌다는 사실은 §2.1에 공개했다.

**정상 운전 불필요 신호는 항상 보고한다.**
- 유효 정상 세션이 있으면, Gate 3 지표(§7.3)를 artifact 검증을 통과한 **모든 후보**에 대해 계산·보고한다.
- Gate 2 결과, 선택 여부, 결과 상태는 상관없다. `SELECTION_INCOMPLETE`·`INTEGRITY_FAILURE`도 포함한다.
- 다만 Gate 3이 선택에 쓰이는 경우는 Gate 2 통과 후보가 2개일 때뿐이다.

### 7.2 세션 유효성 → Gate 1 → Gate 2

**0. 세션 유효성 (§9)**
- 무효 세션은 detector와 무관하게 빼고 대체한다.
- 각 세션의 유효성은 §9 기준만으로, Gate 1–3 재생을 **실행하기 전에** 확정하고 해시와 함께 기록한다.
- 재생은 유효로 기록된 세션만 읽는다.
- 재생 결과를 본 뒤 유효성을 다시 판정하지 않는다.
- 유효 세션 수가 정상 4·압박 5에 못 미치면(시도 상한 도달) 선택하지 않는다 → `SELECTION_INCOMPLETE`.

**Gate 1. 무결성·적격성 (후보별)**
- artifact·규칙 해시가 profile·§5.1 동결값과 일치해야 한다.
- 필요한 telemetry 질의 정의가 profile과 일치해야 한다.
- 입력 유효 비율은 두 후보가 같은 평가 기록을 쓰므로 세션 유효성 규칙에서만 본다.
- **어느 후보든 Gate 1에 실패하면 → `INTEGRITY_FAILURE`.** 나머지 후보로 선택을 이어가지 않는다.

**Gate 2. 압박 신호 요건 (후보별, 필수)**
- **평가 포함 기준**: `window_start_utc ≥ t_injection_observed_utc`(평가 창 전체가 주입 뒤).
- **재생 방식**: 주입 뒤 첫 포함 평가에서 연속 수와 cooldown 상태를 **새로 시작**하는 오프라인 재생으로 센다. 주입 전 신호가 만든 cooldown도 이어지지 않는다.
- **세션 통과**: 그렇게 센 첫 3연속(`condition_met`)이 `t_injection_observed_utc` 후 363 s 이내다(`score_completed_at_utc` 기준).
- **후보 통과**: 유효 압박 세션 5개 중 **4개 이상** 통과.
- 참고: 평가 창이 60 s라 이 기준에서 가장 이른 3연속은 주입 후 약 90–105 s다(60 s + 평가 주기 15 s × 2~3). 363 s 안이며 지연 결함이 아니다.
- **결과 분기**:
  - Gate 2 통과 후보 0개 → `NO_ELIGIBLE_DETECTOR`
  - 1개 → 그 후보 선택
  - 2개 → Gate 3

### 7.3 Gate 3 → Tie-break

**Gate 3. 정상 운전 신뢰성** (Gate 2 통과 후보가 2개일 때만 선택에 사용)
- **지표**: 유효 정상 세션 4개의 결정 창(3600 s)에서 `class_counts["unnecessary"]` 합계.
- **CPU 후보 변환**: IF 점수 대신 `judge_all`과 같은 변환(value = −cpu_mean, threshold = −1.6842544293089357, 미만이면 ANOMALY)으로 같은 `replay`·`arm_outcome` 입력을 만든다.
- **신호 분류**: `experiments/c3_normal_coverage/eval_c3_validation.py`의 `model_metrics`·`_context` 방식을 그대로 쓴다. 이 방식은 동결 분석기 `guarded_normal_bc_analysis.arm_outcome`에 세션 유효성 파일의 SLO 구간·service·infra 사건을 넣는다.
- `indeterminate` 신호는 세지 않는다.
- cooldown 60 s라 오래 지속되는 오경보는 여러 건으로 세진다. c3-A 검증과 같은 지표다.
- **판정**:
  - 합계 차이 ≥ 2 → 적은 쪽 선택.
  - 차이 ≤ 1 → **동등**. 작은 표본에서 1건 차이는 판별 근거로 쓰지 않는다.

**Tie-break 1. 유용 신호 시각** (Gate 3 동등일 때만)
- 압박 세션 5개 각각에서 Gate 2 방식의 첫 3연속 시각(주입 기준)을 잰다.
- 3연속이 없거나 363 s를 넘은 세션은 +∞로 둔다.
- 중앙값 차이 ≥ 30 s(평가 주기 15 s의 2배) → 빠른 쪽. 미만 → 동등.

**Tie-break 2. 운영 단순성** (위가 모두 동등일 때만)
- 사전 고정 순서: A(단일 지표, artifact 없음) > B(scaler·모델 artifact, 정상 coverage 의존).
- 성능 차이가 없을 때만 쓰는 마지막 규칙이며 AI 패널티가 아니다.

### 7.4 선택 결과 상태 (5개, 강제 선택 없음)

| 결과 | 조건 (판정 순서대로 먼저 맞는 것) | ACTIVE |
|---|---|---|
| `INTEGRITY_FAILURE` | Gate 1 실패(어느 후보든), Stage 1 격리 위반(§8.1: 승격·조치·actionable signal 1건 이상), IMPLEMENTATION FREEZE 불일치, 증거 파일 해시 불일치 | 없음 |
| `SELECTION_INCOMPLETE` | 유효 세션 부족(정상 4·압박 5 미달, 시도 상한 도달) | 없음 |
| `NO_ELIGIBLE_DETECTOR` | Gate 2 통과 후보 0개 | 없음 |
| `SELECTED:cpu-threshold-c` | 7.1–7.3 규칙으로 A 선택 | A |
| `SELECTED:c3-a-if` | 7.1–7.3 규칙으로 B 선택 | B |

- ACTIVE가 없는 결과에서는 어떤 후보도 강제로 고르지 않는다. 자동 recovery는 비활성으로 남는다.
- 규칙은 순수 함수로 구현한다. 같은 입력이면 같은 출력이고 입력 순서와 무관하다.
- 결과와 근거 전체(Gate별 세션 결과, 모든 후보의 Gate 3 지표)를 profile `selection.gate_results`에 남긴다.

## 8. 공식 검증 설계 (IMPLEMENTATION FREEZE 뒤 새 세션, 첫 환경 = 현재 CPU-vLLM)

새 장애 시나리오는 만들지 않는다.

### 8.1 Stage 1 격리 계약 (승격 0·조치 0·actionable signal 0)

Stage 1에서는 두 detector가 모두 SHADOW다. 새 격리 장치는 만들지 않고, 프로젝트에서 이미 검증한 방법을 그대로 쓴다.

1. **selection profile**: ACTIVE 0개이고 실행기 송신 경로가 없다. 그래서 guarded 정책으로 가는 신호가 0이다.
2. **기존 recovery-policy 격리**: `experiments/old_policy_isolation.py`.
   - `isolate`는 별도 승인을 받은 라이브 변경이다. 사용자가 실행한다.
   - 세션 전에 `status`가 격리 통과여야 한다. 통과 조건: replicas 0, Pod 0, endpoints 0, GitOps 자동 되돌림 없음 확인.
3. **guarded-recovery-policy**: `GUARDED_MODE=record_only`.
4. **세션 전 확인**: c3 수집 runbook(`experiments/results/c3_normal_coverage_prereg_20261010/C3-COLLECTION-RUNBOOK-DRAFT.md` §1)의 읽기 전용 확인, `experiments/guarded_pilot_session.ps1 -PreflightOnly`(`old_policy.isolated` 포함), 그리고 `old_policy_isolation.py status` 격리 통과. 마지막 조건은 c3보다 엄격하다. c3는 상태를 기록만 했다.
   - 통과 조건: 노드 Ready, Rollout Healthy, scaleDownDelay 30, chaos 0, 잔여 부하 Pod 0, guarded record_only, 기존 정책 격리.
   - 하나라도 어긋나면 시작하지 않는다.
5. **압박 실행기**: 기존 압박 실행기(`run_guarded_bck.py` 등)는 act 전용이므로 Stage 1에 쓰지 않는다.
   - Stage 1 압박 실행기는 새 Phase 2 코드다.
   - 모드는 record_only로 고정한다. act는 인자 단계에서 거부한다.
   - preflight에 `old_policy.isolated`와 guarded record_only를 포함한다.
   - IMPLEMENTATION FREEZE manifest에 넣는다.
   - act 거부 테스트(§11 테스트 26)를 둔다.
6. **세션 후 확인**: 다음 셋이 모두 0이어야 한다.
   - Rollout active hash 변화
   - guarded 감사 기록의 조치
   - 정책 수신 신호
   - 0이 아니면 `INTEGRITY_FAILURE`(§7.4).

### 8.2 Stage 1 - Selection Validation

- **A. 정상 검증**: c3 정상 수집 계약(probe 1 RPS, max_tokens=1, 관찰 4200 s, 5 s Pod 표본)으로 **유효 4 세션**. 세션 유효성은 c3 정상 정의 그대로다.
  - 실행기는 Phase 2 실행기를 SHADOW 기록 모드로 쓴다. 기존 c3 실행기는 쓰지 않는다. 기존 c3 실행기는 c1 B/C/K 쌍을 기록한다.
  - 이 실행기도 record_only로 고정하고 IMPLEMENTATION FREEZE manifest에 넣는다.
- **B. 압박 검증**: A/B 공식 계약의 압박 주입·workload 조건으로 **유효 5 세션**.
  - 송신이 없으므로 유효성은 A/B 계약 중 정책 결과를 전제하지 않는 항목만 쓴다: 주입 전달 확인, telemetry, probe, Pod, 정리.
- **산출**: §7 규칙으로 결과 상태(§7.4)를 낸다. `SELECTED`일 때만 선택 결과를 담은 **active profile**(ACTIVE 1개)을 동결한다.

### 8.3 Stage 2 진입 조건

Stage 2는 다음을 **모두** 만족할 때만, 사용자 승인 후 시작한다.
- Stage 1이 정상 완료되어 결과가 `SELECTED:<id>`이다.
- active profile이 동결되었고 ACTIVE가 정확히 1개다.
- 세션 시작 검사에서 PROFILE_STALE·PROFILE_INVALID가 없다.
- 실행 코드가 IMPLEMENTATION FREEZE manifest와 일치한다. 그 manifest에는 §11 테스트 100% 통과가 기록되어 있다.
- active profile은 동결된 selection profile에서 기계적으로 만든다.
  - selection profile과 다를 수 있는 것은 `detectors[*].role`과 `selection` 블록뿐이다.
  - Stage 2 세션 시작 검사가 이를 diff로 확인한다.
  - `role`을 뺀 `detectors` 블록은 IMPLEMENTATION FREEZE manifest의 기대 해시·threshold와 같아야 한다.
- Stage 2에서도 §8.1의 2(기존 정책 격리)와 4(preflight)를 요구한다. 다만 guarded 모드는 record_only가 아니라 `act`여야 한다.
  - §8.1의 3은 Stage 2에 적용하지 않는다.
  - preflight는 guarded act와 `old_policy.isolated`를 확인한다.
  - act 모드는 guarded 정책에만 둔다.

**금지 조건**:
- 결과가 `SELECTION_INCOMPLETE`, `NO_ELIGIBLE_DETECTOR`, `INTEGRITY_FAILURE`이면 Stage 2 금지.
- Profile stale, implementation freeze 불일치이면 Stage 2 금지.
- Stage 1 결과를 본 뒤 threshold, IF artifact, 선택 규칙을 바꾸는 것도 금지(§17). 바꾸면 Stage 2에 들어갈 수 없다.

### 8.4 Stage 2 - Recovery Integration Validation

active profile에서는 선택된 1개만 ACTIVE이고 나머지는 SHADOW다.
- **live**: 압박 조건에서 act 모드 **유효 3 세션**.
- **세션 유효성**: §8.2 B 항목(정책 결과를 전제하지 않는 항목)만으로, 성공 판정 전에 확정한다.
- **시도 상한**: 5.
- **유효 3 세션을 채우지 못한 경우**(상한 도달 또는 진입 거부):
  - 실패한 유효 세션이 없으면 §13 `PHASE2_SELECTION_VALIDATED_RECOVERY_PENDING`이 되고, 이유를 기록한다.
  - 실패한 유효 세션이 1개 이상이고 성공도 1개 이상이면 `PHASE2_MIXED`.
  - 실패한 유효 세션이 1개 이상이고 성공이 0개면 `PHASE2_NOT_VALIDATED`.
- 모든 유효 세션의 성공·실패는 항상 보고한다.
- **성공 세션 조건 (모두 필요)**:
  - `promoted_verified`
  - 새/이전 Pod 요청 카운터로 전환 확인
  - SHADOW 송신 0
  - 정책 감사 기록의 detector 식별이 ACTIVE뿐
- **Stage 2 실패로 세는 경우**(세션 자체는 유효):
  - ACTIVE가 세션 안에 신호를 내지 못함
  - 첫 ACTIVE 송신이 주입 관측 시각보다 이름
- 비선택 detector의 live 승격은 하지 않는다.
- "어느 쪽이 ACTIVE여도 같은 경로"는 mock/통합 테스트(§11 테스트 12)와 Phase 1 개발 근거(§2)로 보인다. mock을 live 증명으로 부르지 않는다.

## 9. 제외·중단 규칙

- **세션 무효** (대체 세션으로 보충, 사용자 승인):
  - 정상 = c3 정상 정의 위반: SLO, 요청 실패, Pod 교체, 재시작, 전원, telemetry 공백 > 30 s, 운영자 개입, 사용 가능 평가 비율 < 0.90.
  - 압박 = §8.2 B의 유효성 항목 위반.
- **수집 중단** (보고 후 사람 확인):
  - invalid 2회 연속
  - 예상 밖 저하 2회(정상 세션, 또는 압박 세션에서 A/B 계약의 예상 범위를 벗어난 저하)
  - 정리 오류 1회
  - `PROFILE_STALE`, `PROFILE_INVALID`
  - Stage 1 격리 위반
  - 시도 상한(정상 6·압박 7·Stage 2 5) 도달
- 결과를 본 뒤 임계값, 규칙, 세션 수, tolerance는 바꾸지 않는다(§17·§18).

## 10. Fail-safe (자동 recovery 금지 조건)

| 조건 | 차단 위치 |
|---|---|
| SHADOW 판정 | 실행기에 송신 경로 없음(1차), 식별 불일치 시 정책 `rejected_context`(2차, 식별만) |
| Stage 1 전체 | ACTIVE 0개 profile + 기존 정책 격리 + guarded record_only(§8.1) |
| 알 수 없는 detector / detector 버전·해시·임계값 불일치 | 정책 context 식별 검사(`threshold` 필수) |
| profile 무효(ACTIVE 2개 이상, 해시 불일치) / artifact 검사 실패(§5.1) / `PROFILE_STALE` | `PROFILE_INVALID`·`PROFILE_STALE` → act context 생성 거부(`actions_allowed=false`), 실행기 시작 거부 |
| ACTIVE 0개(Stage 1 selection profile, `NO_ELIGIBLE_DETECTOR`) | act context 생성 거부. 실행기는 송신 경로가 없는 SHADOW 기록 모드로만 시작 가능 |
| 선택 결과가 `SELECTED`가 아님 | Stage 2 진입 금지(§8.3) |
| 오래된 신호 | 기존 `max_signal_age_sec` 검사(`rejected_timing`) |
| telemetry 무효(`UNKNOWN`) | 연속 수 리셋 → 조건 미충족(송신 없음) |

원칙: 모호한 상태에서는 자동 recovery를 하지 않는다.

## 11. 테스트 계약

위치는 `experiments/phase2_detector_selection/test_phase2_*.py`(저장소 관례)다.

Phase 2 gate는 두 가지다.
- 이 폴더의 테스트가 **100% 통과**한다.
- 기존 `recovery-policy/test_*.py`·`anomaly-detection/test_c1_bck_*.py`에서 **새 regression이 0**이다.

필수 테스트(구현 단계에서 작성):
1. CPU 판정 정규화
2. IF(c3-A) 판정 정규화
3. NORMAL/ANOMALY/UNKNOWN 매핑
4. SHADOW는 송신 불가. **그리고** 송신 payload의 score·식별이 항상 ACTIVE 판정과 일치한다(실행기 내부 오배정 방지).
5. ACTIVE는 정책에 진입 가능
6. ACTIVE 2개 profile 거부
7. ACTIVE 0개 → act context 없음
8. stale profile → act context 없음
9. profile 불일치 거부
10. detector 버전·임계값 불일치 → `rejected_context`
11. 오래된 신호 → `rejected_timing`
12. CPU·IF 각각 ACTIVE일 때 같은 정책 entrypoint, 같은 guarded 검증, 같은 승격, 같은 트래픽 검증 경로를 지난다(mock 클러스터, 정책 코드 무수정).
13. 선택 규칙 결정성
14. 같은 증거 → 같은 선택
15. 입력 순서 무관
16. 유효 세션 부족 → `SELECTION_INCOMPLETE`
17. profile 파일 해시 기록·불일치 처리
18. 선택 근거 기록
19. 주입 전 평가·주입 전 cooldown이 Gate 2 계산에 들어가지 않음(`window_start_utc` 기준)
20. same-tick parity(같은 `evaluation_seq`·특징값)
21. context `threshold` None 거부
22. artifact 없음·해시·버전·schema·threshold 불일치 각각 → `PROFILE_INVALID`, 실행기 시작 거부
23. Gate 2 실패 후보는 Gate 3 지표가 더 좋아도 선택되지 않음(사전식 우선순위). Gate 2 실패 후보의 Gate 3 지표도 보고됨.
24. 결과 상태 판정 순서(§7.4): Gate 1 실패 → `INTEGRITY_FAILURE`. Gate 2 통과 0개 → `NO_ELIGIBLE_DETECTOR`.
25. Stage 2 진입 검사는 다음에서 거부한다: `SELECTED` 외 결과, stale, implementation freeze 불일치, selection profile과 `role`을 뺀 `detectors` 블록이 다른 active profile. `role`·`selection`만 다른 active profile은 허용한다.
26. Stage 1 실행기(정상·압박)는 act 모드를 인자 단계에서 거부
27. 재생은 유효성 기록이 없는 세션을 거부
28. 유효 정상 세션이 있으면, 모든 결과 상태에서 `gate_results`에 Gate 3 지표가 있음
29. Stage 2 판정 매핑: §8.4 분할의 각 경우, 유효 실패 뒤 stale 중단 → PENDING 아님, SHADOW 송신 1건 → `PHASE2_NOT_VALIDATED`

**기준선 분리**: 구현을 시작할 때 canonical master의 기존 실패 목록을 `experiments/phase2_detector_selection/KNOWN-BASELINE-FAILURES.txt`로 기록하고 Phase 2 판정에서 제외한다. 이 목록에 들어가는 실패는 다음과 같다.
- 깨끗한 checkout에서 pkl·원자료·줄바꿈 provenance 때문에 나는 실패
- 공식 실행 전 상태를 가정한 frozen 테스트 2개
- `test_fixed_threshold` 2개

전체 suite 0 실패는 성공 조건이 아니다.

## 12. live 실험 경계

- Stage 1·2 모두 세션마다 사용자 승인을 받는다.
- VPN 접속, 클러스터 변경, 승격은 승인된 세션에서만 한다. 승격은 Stage 2에서만 한다.
- Stage 1의 운영 조건은 §8.1 격리 계약을 따른다.

## 13. 판정 정의 (어느 detector가 선택되는지와 무관)

| 판정 | 조건 |
|---|---|
| `PHASE2_ARCHITECTURE_VALIDATED` | Stage 1 `SELECTED` + Stage 2 live **3/3 성공** + 테스트 계약 100% |
| `PHASE2_SELECTION_VALIDATED_RECOVERY_PENDING` | Stage 1이 `SELECTED` 또는 `NO_ELIGIBLE_DETECTOR`로 정상 완료 + 테스트 계약 100%, 그리고 다음 중 하나: Stage 2 미실행, Stage 2 진입 거부, Stage 2 유효 3 세션 미달이면서 실패한 유효 세션 0(이유 기록, §8.4). `NO_ELIGIBLE_DETECTOR`는 Stage 2 금지(§8.3)라 여기서 끝난다. |
| `PHASE2_MIXED` | Stage 1 `SELECTED`, Stage 2 유효 세션에서 성공 ≥1과 실패 ≥1 |
| `PHASE2_NOT_VALIDATED` | 선택 비결정, 권한 누수(SHADOW 송신·조치), 사전등록 위반, `SELECTION_INCOMPLETE`, `INTEGRITY_FAILURE`, Stage 2 유효 세션에서 성공 0·실패 ≥1 |

판정 순서:
1. `PHASE2_NOT_VALIDATED` 조건을 먼저 본다.
2. 그다음 `PHASE2_ARCHITECTURE_VALIDATED`, `PHASE2_MIXED`, `PHASE2_SELECTION_VALIDATED_RECOVERY_PENDING` 순서로 본다.
3. Stage 2 어느 세션에서든 권한 누수가 있으면, 다른 세션이 성공했어도 `PHASE2_NOT_VALIDATED`다.

## 14. 주장 경계

- **말할 수 있는 목표**: "현재 CPU-vLLM 환경에서 사전등록 기준으로 후보 detector를 선택하고, 선택된 detector를 공통 recovery pipeline에 연결하는 detector-agnostic 구조를 검증했다."
- **말하지 않음**:
  - 모든 LLM 환경 자동 최적 선택
  - production-ready 범용 선택
  - AI가 임계값보다 우월 또는 열등(Gate 2 결과는 §2.1 사전 정보 아래의 확인)
  - GPU 환경 검증
  - concept drift 자동 대응

## 15. 일정 메모 (계획 정보일 뿐, 연구 계약 아님)

- **추정 시간**:
  - Stage 1 약 7시간(정상 4 × 약 78분 + 압박 5 × 약 20분)
  - Stage 2 약 1시간
  - 그 전에 구현·테스트가 필요하다.
- 10/15 시연, 10/30 경진대회 일정과 겹치므로 실행 시점은 사용자가 정한다.
- **시간 때문에 바꾸지 않는 것**: 시간 부족을 이유로 다음을 하지 않는다.
  - 세션 수 축소
  - Gate·tolerance 변경
  - 조기 중단 후 선택
  - detector 사전 결정
- 시간이 모자라면 판정은 §13의 미완료 상태로 남긴다.

## 16. 선택적 확장 (Phase 2H, 성공 조건 아님)

두 번째 환경(GPU 클라우드, 다른 모델, 다른 workload) 검증이다. 환경 확보에 실패해도 Phase 2 판정에 영향을 주지 않는다.

## 17. 결과를 본 뒤 조정 금지

다음은 이 문서의 DESIGN FREEZE 시점으로 고정한다.
- 임계값(두 후보)
- c3-A artifact 해시(§5.1)
- 선택 규칙과 사전식 순서
- Gate 기준(`window_start_utc` 기준, 363 s, 4/5)
- tolerance(합계 1건, 30 s)
- 세션 수(정상 4·압박 5·Stage 2 3)
- 시도 상한
- tie-break 순서
- 결과 상태 정의

Stage 1 결과를 본 뒤 이 중 무엇이든 바꾸면 그 Stage 1은 Phase 2 공식 판정에 쓰지 않는다.
- 완료된 세션이 1개라도 있으면 "결과를 봤다"로 본다.
- 제외된 Stage 1 결과도 전부 보고한다.
- 이후 revision은 어떤 Phase 2 데이터를 본 뒤 작성됐는지를 §2.1처럼 공개한다. 그 revision의 Stage 1을 그 데이터와 독립적인 확인이라고 부르지 않는다.

## 18. 2단계 freeze와 교체 절차

**A. DESIGN FREEZE (이 문서)**
- 이 문서를 커밋한다.
- 그다음, 이 문서 커밋 SHA를 참조하는 후속 커밋으로 `docs/design/phase2-detector-selection-design-freeze.json`을 만든다.
- manifest에 기록하는 것: 문서 경로, sha256(커밋된 blob = LF 정규화 내용 기준), 문서 커밋 SHA, revision, 동결 시각, status.

**B. IMPLEMENTATION FREEZE (나중)**
- **순서**: DESIGN FREEZE → 구현 → 테스트·리뷰 → IMPLEMENTATION FREEZE → live Stage 1.
- implementation freeze manifest에는 다음을 기록한다.
  - DESIGN FREEZE manifest의 sha256
  - Phase 2 코드·테스트 파일 해시
  - 외부 artifact 기대 해시(§5.1)
  - selection profile sha256(active profile은 §8.3 규칙으로 여기서 파생)
  - §11 테스트 100% 통과·새 regression 0 실행 결과와 그 기록의 해시
- IMPLEMENTATION FREEZE는 §11 테스트가 100% 통과하고 새 regression이 0일 때만 기록할 수 있다.
- Stage 1·2 세션 시작 시 실행 코드가 이 manifest와 일치하지 않으면 시작하지 않는다.

**동결 뒤 변경**
- DESIGN FREEZE 뒤에는 live 결과를 근거로 이 문서를 고치지 않는다.
- 문서 결함이 발견되면 이 파일을 조용히 고치지 않는다. 대신 다음을 한다.
  1. 새 revision 문서(`PHASE2-DETECTOR-SELECTION-PREREG-v1-rev<N>.md` 등)를 만든다.
  2. 이전 freeze를 명시하는 **교체 manifest**를 만든다. 교체 manifest에는 `supersedes`, 변경 이유, 변경 시각, 그때까지 본 결과 범위를 적는다.
  3. 새 commit으로 남긴다.
- Stage 1 데이터를 본 뒤의 교체는 §17에 따라 그 Stage 1을 공식 판정에서 제외한다.
- 커밋된 동결 파일의 내용이 manifest의 sha256과 다르면, 그 상태로는 IMPLEMENTATION FREEZE와 Stage 1을 시작하지 않는다.
