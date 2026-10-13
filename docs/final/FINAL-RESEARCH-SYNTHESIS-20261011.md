# Phase 1 최종 연구 정리 (2026-10-11)

> Status: **CURRENT(Phase 1 정리)** - Phase 1(이 snapshot까지)의 결과와 최종 후속 연구 방향을 한곳에 정리한 문서.
> **2026-10-12 업데이트**: §4의 후속 방향(Phase 2)은 이후 사전등록·실행·판정까지 완료되었다 - 최종 판정 `PHASE2_ARCHITECTURE_VALIDATED`. 결과는 [PHASE2-FINAL-RESULT-20261012.md](PHASE2-FINAL-RESULT-20261012.md) 참조. 아래 본문은 Phase 1 시점 기록으로 그대로 둔다.
> 공식 결과를 새로 만들거나 바꾸지 않는다. 숫자는 각 공식 결과 파일에서 그대로 옮겼다.
> `FINAL-EVIDENCE-SUMMARY-20261010.md`(A/B·B/C/K·N1 공식 증거·주장 판정표)는 그대로 유효하며, 이 문서는 그 이후 결과(c3-A, Axis 2, 문헌·S3)와 방향을 더한다.

## 0. 한 문단 요약

느리지만 살아 있는 LLM 추론 서비스 저하에서, 이상 신호에 따라 미리 준비한 Pod로 **실제 전환하면 사용자 SLO 피해가 줄었다**(A/B 5/5). 탐지기 쪽에서는 단순 CPU 압박에서 **보정된 CPU 임계값이 충분히 강했고**, Isolation Forest는 **정상 데이터 범위에 민감했다** - 정상 범위를 넓힌 c3-A는 c1보다 불필요 신호를 크게 줄였다(14 → 1). 현재 CPU 기반 vLLM 환경에서는 LLM 지표를 쓴 다변량 ML이 CPU 임계값보다 낫다는 것을 공정하게 검증할 조건을 만들 수 없었다. 그래서 **탐지기와 복구를 분리하고, 환경에 맞는 탐지기를 선택해 공통 복구 경로에 연결하는 것**을 최종 후속 연구 방향으로 둔다.

## 1. Phase 1 연구 흐름

| 순서 | 연구 | 질문 | 결과 | 근거 |
|---|---|---|---|---|
| 1 | A/B 선제 전환 | 같은 신호에서 실제 전환이 기록만 할 때보다 피해를 줄이나 | 5/5 줄임(A 204.504 / ≥210.83 / 201.68 / 287.79 / 207.396 s → B 0 s) | `FINAL-EVIDENCE-SUMMARY-20261010.md` §1 |
| 2 | B/C/K | c1 IF 대 보정 CPU 임계값 C·기존 임계값 K | IF > C 증거 없음(D 중앙값 0), K는 5/5 늦고 피해 큼 | 같은 문서 §2 |
| 3 | N1 정상 운전 | 정상에서 불필요 신호 | c1 IF가 C보다 불필요 신호를 더 일찍 냄(6/6) | 같은 문서 §3 |
| 4 | c1 무결성 감사 | 계산 오류인가 해석 문제인가 | 계산 정확, 정상 학습 범위가 좁아 시작 직후 저CPU를 이상으로 봄 | 같은 문서 §4 |
| 5 | **c3-A** 정상 범위 확장 재학습 | 정상 coverage만 넓히면 같은 IF 구조의 불필요 신호가 줄어드나 | 사전등록 판정 **PROMISING**: 불필요 신호 c1 14 → c3-A 1(92.9%↓), 기준 A–D 충족, 압박 기준 E 5/5(단 4/5는 첫 3연속에 주입 전 평가 1개 포함, 주입 뒤만 보면 363 s 이내 2/5) | `experiments/results/c3_normal_coverage_evaluation/c3-validation-evaluation.json`, 사전등록 `experiments/results/c3_normal_coverage_prereg_20261010/` |
| 6 | **Axis 2** | LLM 지표 다변량 detector가 CPU 임계값보다 추가 가치를 주나 | **NO_GO_LLM_MULTIVARIATE**: 정상·CPU 압박에서 running·waiting·KV·선점이 0, LLM 지표를 움직일 부하는 SLO를 먼저 깸 | `experiments/llm_multivariate_added_value/AXIS2-FEASIBILITY-20261011.md` |
| 7 | 문헌 기반 시나리오 | 알려진 LLM serving 저하 중 재현 가능한 것이 있나 | **LITERATURE_SCENARIOS_NEED_ENVIRONMENT_CHANGE** | `experiments/llm_failure_scenario_research/` |
| 8 | S3 KV cache 압박 | KV 예산만 줄여 SLO 전에 KV 압박을 볼 수 있나 | **S3_PILOT_DESIGN_NOT_FEASIBLE(예상)**, pilot 미실행 | 같은 폴더 `S3-KV-PRESSURE-*.md` |

## 2. 주장 경계 (기존 판정표에 더하는 것)

- 써도 됨: "정상 범위를 넓혀 다시 학습한 c3-A는 사전등록된 블라인드 검증 4 세션에서 c1보다 불필요 이상 신호가 크게 적었다(14 → 1)."
- 써도 됨: "현재 환경에서는 다변량 ML의 추가 우월성을 공정하게 검증할 조건이 없었다(NO_GO)."
- 쓰면 안 됨: "c3-A가 CPU 임계값보다 낫다"(c3-A는 c1과만 비교), "c3-A가 장애를 더 빨리 잡는다", "c3-A가 운영 준비 완료(production-ready)다", "AI가 장애를 예측한다".
- 압박 기준 E는 반드시 caveat(주입 전 평가 포함 4/5)와 함께 말한다.

## 3. 결과에서 도출된 설계 원칙

1. 단순 CPU 압박에서는 보정된 CPU 임계값이 충분히 강했다(B/C/K).
2. IF는 정상 학습 coverage에 민감했고(N1·감사), coverage를 넓히자 reliability가 개선됐다(c3-A).
3. 현재 환경에서는 다변량 ML의 추가 우월성이 검증되지 않았다(Axis 2·문헌·S3).
4. 반면 복구 경로(guarded 정책 → Argo Rollouts → 트래픽 전환 → 검증)는 신호가 IF든 CPU 규칙이든 같게 동작했다(A/B, B/C/K act run).

→ 따라서 복구 시스템을 특정 detector에 종속시키기보다 **detector와 recovery를 분리**하는 것이 합리적이다. 이 방향은 새로 떠올린 아이디어가 아니라 위 결과에서 나온 결론이다.

## 4. 최종 후속 연구 방향 (Phase 2 제안, 미구현)

> 2026-10-12: 이 방향은 Phase 2로 구현·검증되었다(현재 CPU-vLLM 환경, `PHASE2_ARCHITECTURE_VALIDATED`, [PHASE2-FINAL-RESULT-20261012.md](PHASE2-FINAL-RESULT-20261012.md)). 아래는 제안 당시 기록이다.

환경별 workload와 병목 특성에 따라 적합한 detector를 선택할 수 있도록 Detection Layer를 교체 가능한 구조로 확장하고, 선택된 detector의 anomaly signal은 기존 Recovery Policy와 Argo Rollouts 기반 복구 파이프라인에 공통으로 연결하는 방향을 후속 연구로 제안한다.

```
Environment
  → Detector Validation / Selection   (환경별 정상·저하 데이터로 후보 검증 - 수동 절차부터)
  → Selected (Active) Detector        (선택된 하나만 조치 신호, 나머지는 shadow 기록)
  → Recovery Policy (guarded, 공통)
  → Argo Rollouts → Traffic Switch → Verification
```

- detector 후보 예: 보정 CPU 임계값, Isolation Forest(c3-A), 앞으로의 detector.
- 환경마다 detector는 달라질 수 있지만 Recovery Layer는 재사용한다.
- 목표는 AI 우월성 증명이 아니라 **환경에 맞는 detector 선택**이다.
- **아직 구현하지 않은 것**: 자동 benchmarking, 자동 최적 detector 선택, 여러 detector 자동 학습, 환경 자동 profiling. 현재 구현은 CPU 임계값과 IF를 같은 복구 경로에 연결해 비교한 것까지다.

## 5. 한계

- CPU 기반 소형 LLM(Qwen2.5-0.5B, vLLM 0.28.1 CPU) 단일 환경, workload 다양성 제한.
- c3-A 검증 n = 4, 검증·학습이 같은 block(날짜·Pod) 공유, 날짜 2개(설계 목표 3개 미충족, 날짜 관문을 위해 c3n09 시작 시각을 00:00Z 이후로 조정).
- AI가 보정 CPU 임계값보다 낫다는 것은 확인되지 않음.
- 운영 GPU·사설 클라우드 일반화 미검증.

## 6. 재현성 (known limitation - 이번 단계에서 해결하지 않음)

- 원 실험 evidence(`experiments/results/*` 원자료)는 저장소 밖 보존(관례상 미커밋).
- `model.pkl`·`scaler.pkl`은 `.gitignore` 대상 → c1 모델을 import 시점에 해시하는 테스트 모듈 3개는 깨끗한 checkout에서 수집 실패. 메타데이터(freeze·threshold·SHA256SUMS)는 커밋됨.
- 줄바꿈: freeze 해시는 원래 작업 폴더 파일 바이트(CRLF/LF 혼재) 기준, 저장소 blob은 `core.autocrlf=true`(이 PC 시스템 설정)로 LF 정규화 → 새 checkout(Windows 포함)에서는 내용이 같아도 provenance 해시 불일치 가능. 2026-10-11 snapshot worktree 확인: c3·BCK v1.2·N1 v1.1·nnf manifest 불일치 59건 전부 줄바꿈 차이(내용 동일), 원래 작업 폴더에서는 모두 일치.
- "공식 실행 전" 상태를 가정한 frozen 테스트 2개(`test_guarded_bck_analysis`, `test_guarded_ab_analysis`의 real-folder 테스트)는 공식 실행 뒤라 실패, 그 밖에 이전부터 실패하던 `test_fixed_threshold` 2개.
- 깨끗한 snapshot에서 commit된 코드의 import 누락 모듈은 0(2026-10-11 확인).

## 7. 문서 지도·분류 (canonical master 정리 후, 2026-10-11)

| 분류 | 문서 |
|---|---|
| A. CURRENT | `README.md`, 이 문서, `docs/final/FINAL-EVIDENCE-SUMMARY-20261010.md`(A/B·B/C/K·N1 공식 증거), `docs/final/PRESENTATION-PACKAGE-20261010.md`(발표 문구·E8), `docs/presentation/`, `experiments/llm_multivariate_added_value/AXIS2-FEASIBILITY-20261011.md`, `experiments/llm_failure_scenario_research/*.md`, 하위 폴더 README(`anomaly-detection/`, `recovery-policy/`, `chaos/`, `gitops/`, `experiments/`) |
| B. FROZEN / REPRODUCIBILITY(경로·내용 유지) | freeze manifest가 해시로 참조하는 문서·코드(예: `docs/design/phase8-aiops-next-research-direction-20261009.md`, `experiments/results/c3_normal_coverage_prereg_20261010/` 사전등록·runbook, 각 `docs/design/*freeze-manifest*.json`), 공식 평가 결과 JSON |
| C. HISTORICAL BUT NECESSARY | `docs/design/` 실험 계약·중간 설계(당시 기준), `docs/history/`(초기 제안서 `PROPOSAL-2026-07.md`, 대체된 `REPRODUCIBILITY-AUDIT-20261010.md`, `C1-DETECTOR-LIMITATIONS-AND-FUTURE-WORK-20261010.md`) |
| D. OBSOLETE / CONFUSING | `cluster/`, `monitoring/`(존재하지 않는 설정 파일을 설명하던 README), `finops/`, `operator/`, `pvc-backup/`(미구현 보너스 계획) - 삭제 후보였으나 이번 정리에서는 삭제하지 않고 README를 현재 상태 설명으로 바꿈 |
