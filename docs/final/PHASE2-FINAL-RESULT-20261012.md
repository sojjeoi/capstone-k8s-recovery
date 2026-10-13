# Phase 2 최종 결과 - 환경별 detector 선택과 공통 복구 경로 검증 (2026-10-12)

> Status: **CURRENT** - Phase 2(사전등록 rev 6, implementation freeze r2)의 최종 결과.
> 공식 결과를 새로 만들거나 바꾸지 않는다. 숫자는 아래 근거 파일에서 그대로 옮겼다.
> 최종 판정: **`PHASE2_ARCHITECTURE_VALIDATED`** (근거 `experiments/results/phase2_verdict/phase2-verdict-evidence.json`, sha256 `e87419f95c63f7630435b763d126e3dbe15fb1d6f839dfd7e59a7087c42945e8`)

## 0. 한 문단 요약

Phase 1에서 나온 방향(탐지와 복구를 나누고, 환경에 맞는 detector를 골라 공통 복구 경로에 연결한다)을 사전등록한 규칙으로 실제로 검증했다. 현재 CPU-vLLM 환경에서 새로 수집한 Stage 1 세션(정상 4, 압박 5)에 사전등록 선택 규칙을 적용한 결과 **cpu-threshold-c가 ACTIVE로 선택**되었다(c3-A IF는 필수 압박 Gate를 통과하지 못함). 선택된 detector를 같은 Guarded Recovery 경로에 연결한 Stage 2 live 세션 3회에서 **모두 실제 승격과 트래픽 이동이 확인**되었고(새 Pod +60 / 이전 Pod +0), SHADOW detector의 권한 누수는 없었다.

## 1. 구조

```
Environment / Profile
  → Detector Validation / Selection   (Stage 1: 사전등록 Gate로 후보 검증·선택)
  → Active Profile                     (ACTIVE 정확히 1개, 나머지 SHADOW)
  → Detection Layer [ACTIVE | SHADOW]  (SHADOW는 기록만 - 송신·조치·승격 권한 없음)
  → Recovery Policy (guarded, 공통)
  → Argo Rollouts → Service Switch → Traffic Verification (old/new Pod 실제 요청 수)
```

detector는 환경에 따라 바뀔 수 있지만, Recovery Policy 이후의 복구 경로는 같다.

## 2. Stage 1 - 사전등록 detector 선택

| 항목 | 값 |
|---|---|
| 세션 | 정상 유효 4/4(p2n02·p2n03·p2n05·p2n06), 압박 유효 5/5(p2p01–p2p05). 무효 2(p2n01 탐지기 평가 전 하네스 오류, p2n04 tick 간격 31.1 s > 30 s) - 이력으로 보존 |
| 규칙 순서 | 세션 유효성 → Gate 1 → Gate 2(필수) → Gate 3 → 시각 tie-break → 단순성 tie-break(가중 점수 없음) |
| Gate 2(주입 뒤 평가만, 363 s 안 첫 3연속, 5개 중 4개 이상) | **CPU threshold C 5/5 PASS**(111.4 / 109.6 / 106.8 / 97.9 / 112.2 s), **c3-A IF 0/5**(408.5 / 427.9 / 422.8 / 428.9 s / 3연속 없음) |
| 정상 불필요 신호(보고용) | CPU 0·0·0·1 = **1**, IF 0·0·0·0 = **0** |
| Gate 3·tie-break | 사용 안 함(Gate 2 통과 후보 1개) |
| 결과 | **`SELECTED:cpu-threshold-c`** |

IF는 정상 운전에서 더 조용했지만, 필수 조건인 압박 Gate 2를 통과하지 못해 ACTIVE가 될 수 없었다(prereg §7.1: Gate 2 실패 detector는 정상에서 더 조용해도 ACTIVE 불가). 이 결과는 CPU threshold가 일반적으로 IF보다 낫다는 뜻이 아니다.

근거: `experiments/results/phase2_selection/phase2-selection-evidence.json`(sha256 `e8bffe98…d9c921`), 수집 닫힘 `experiments/results/phase2_stage1/p2-collection-closed.json`(`adedf9d7…79cee`), Gate 2 값은 원자료에서 독립 재계산으로 10개 모두 일치.

## 3. Active profile

`experiments/results/phase2_selection/active-profile.json`(sha256 `246af400…37c2ea`): ACTIVE = cpu-threshold-c, SHADOW = c3-a-if, ACTIVE 정확히 1개. selection profile과의 차이는 `role`과 `selection` 블록뿐(threshold·artifact·feature·environment 불변).

## 4. Stage 2 - 선택된 detector를 공통 복구 경로에 연결(live, act)

| 세션 | 유효 | ACTIVE 송신(주입 후) | 조치 | 승격 | selector 전환 | 트래픽(새 / 이전 Pod) | 판정 |
|---|---|---|---|---|---|---|---|
| p2s01 | ✔ | 1 (73.7 s) | 1 | 1 | 7976ddcdb6 → 66bb554f46 (0.54 s) | +60 / +0 | verified |
| p2s02 | ✔ | 1 (83.6 s) | 1 | 1 | 66bb554f46 → 55f54cbfdb (0.53 s) | +60 / +0 | verified |
| p2s03 | ✔ | 1 (80.5 s) | 1 | 1 | 55f54cbfdb → 7f6dcd75f5 (0.55 s) | +60 / +0 | verified |

- 합계: 유효 3/3, 성공 3/3, 승격 3, 트래픽 확인 3(각 60 s 창·endpoint 표본 12·읽기 실패 0, 기준 새 ≥ 30 / 이전 ≤ 2).
- SHADOW c3-a-if: 송신 0, 조치 0, 승격 0 - 권한 누수 없음.
- 세 세션 모두 정리 오류 0, canonical 복원 정상, 세션 뒤 정책 `record_only` 복원.

근거: `experiments/results/phase2_stage2/`(세션 기록·유효성·요약·평가 원자료, 운영 기록 `ops-*`).

## 5. 최종 판정

동결 `run_stage2.phase2_verdict`(prereg §13) 1회 호출: **`PHASE2_ARCHITECTURE_VALIDATED`**(Stage 1 `SELECTED:cpu-threshold-c`, Stage 2 유효 3·성공 3·실패 0). 원자료로 다시 계산한 세션 결과도 기록과 같다.

## 6. 말할 수 있는 것 / 말하지 않는 것

**말할 수 있는 것**
1. 현재 CPU-vLLM 환경에서 사전등록된 선택 규칙을 적용한 결과, cpu-threshold-c가 ACTIVE로 선택되었다.
2. 선택된 detector를 동일한 Guarded Recovery 경로에 연결한 Stage 2 세션 3회 모두에서 실제 승격과 트래픽 이동을 확인했다.
3. 세 세션 모두 새 Pod +60 / 이전 Pod +0이었다.
4. SHADOW detector에는 recovery authority가 없었고 권한 누수는 관찰되지 않았다.
5. 최종 판정은 `PHASE2_ARCHITECTURE_VALIDATED`이다. 선택 결과는 환경 종속적이며 다른 환경에서는 재검증이 필요하다.

**말하지 않는 것**
- CPU threshold가 IF보다 일반적으로 우월하다 / AI detector가 필요 없다
- 장애를 예측했다 / 주입 후 74–84초 만에 완전히 새로운 이상을 조기 탐지했다
- 다른 환경에서도 같은 detector가 선택된다 / 다른 환경으로 일반화된다
- in-flight state migration을 해결했다

## 7. 해석 caveat (판정 기준과 분리)

- **주입 전 평가 창**: 세 Stage 2 세션 모두 ACTIVE 3연속의 앞쪽 평가 창 일부가 주입 전에 시작했다(동결 live 규칙 그대로). actionable signal은 세 번 모두 주입 뒤에 발생했다.
- **SHADOW IF 조건 충족**: p2s02·p2s03에서 SHADOW IF도 (주입 전 창부터) 3연속 조건을 충족했지만 권한이 없어 송신·조치·승격은 0이었다. Stage 1 선택이나 ACTIVE를 바꾸지 않으며, Stage 2 성공을 IF 성능 비교로 해석하지 않는다.
- **선택 분석 진입 경로**: Stage 1 선택은 runbook CLI가 아니라 같은 동결 `collect()` → `evidence_from()` 경로로 1회 수행했고(당시 승인이 active profile 생성을 금지), active profile은 그 근거에서 동결 `derive_active_profile`로 따로 기계적으로 만들었다(`phase2-selection-provenance-supplement.json`, `active-profile-provenance.json`).
- **p2s02 세션 시작 전 중단**: 첫 launch는 act 전환 시계 gate(0.659 s > 0.5 s)에서 세션 시작 전 fail-closed, 시도 기록 없음. 기준 변경 없이 두 번째 launch(0.415 s)에서 통과. 유효 세션으로 세지 않음(`ops-p2s02/`, `ops-p2s02-launch2/`).
- **압박 Stage 1 사전검사 중단 2회**: 운영 스크립트의 한글 경로 인코딩 문제로 세션 시작 전 fail-closed(시도 미생성, artifact·클러스터 변경 없음).
- **판정 실행 방식**: 동결 코드에 판정 CLI가 없어 순수 함수 `phase2_verdict`를 1회 호출하고 동결 `write_json_new`로 저장했다.

## 8. 한계

- CPU 기반 소형 LLM(Qwen2.5-0.5B, vLLM CPU) 단일 환경, 단일 압박 유형(CPU stress)
- Stage 2 n = 3(사전등록 목표 수), Stage 1 정상 4·압박 5
- Phase 2 원자료(`experiments/results/phase2_*`)는 저장소에 커밋되어 있지 않다(작업 폴더 보존).

## 9. 근거 파일

| 내용 | 위치 |
|---|---|
| 사전등록 rev 6 | `docs/design/PHASE2-DETECTOR-SELECTION-PREREG-v1-rev6.md` |
| implementation freeze r2 | `experiments/phase2_detector_selection/phase2-detector-selection-implementation-freeze-r2.json` |
| 선택 근거·보충 기록 | `experiments/results/phase2_selection/` |
| Stage 1 세션 | `experiments/results/phase2_stage1/` |
| Stage 2 세션 | `experiments/results/phase2_stage2/` |
| 최종 판정 | `experiments/results/phase2_verdict/phase2-verdict-evidence.json` |
