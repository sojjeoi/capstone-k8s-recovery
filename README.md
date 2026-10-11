# LLM 서빙 성능 저하 대응을 위한 Kubernetes 자동 복구 시스템

> **한 문장 정의**: "느리지만 살아 있는" LLM 추론 서비스 저하를 탐지 신호로 받아, guarded 복구 정책이 Argo Rollouts Blue/Green 전환과 실제 트래픽 검증까지 수행하는 Kubernetes 자동 복구 시스템. 탐지기(detector)는 교체 가능한 부품으로 두고 복구 계층은 공통으로 재사용한다.

- 지도교수: 김영한 교수님 · 작성자: 박소정 (숭실대 전자정보공학부 IT융합전공)
- Phase 1 최종 snapshot: branch `research-snapshot-20261011`
- 현재 연구 요약(가장 먼저 읽을 문서): [docs/final/FINAL-RESEARCH-SYNTHESIS-20261011.md](docs/final/FINAL-RESEARCH-SYNTHESIS-20261011.md)

## 1. Problem

LLM 서빙 Pod는 죽지 않고 느려지기만 하는 경우가 많다(예: CPU 압박). Kubernetes 기본 self-healing은 Pod가 살아 있으면 개입하지 않으므로, 사용자 응답 지연(SLO 위반)이 길게 이어질 수 있다.

## 2. What We Built

```
Detection Layer  →  Recovery Policy  →  Argo Rollouts  →  Traffic Switch  →  Verification
(교체 가능)          (guarded, 공통)      (Blue/Green 승격)   (Service selector)  (새/이전 Pod 실제 요청 수)
```

- **Detection Layer**: 이상 신호를 내는 부품. 특정 모델 하나에 묶지 않는다. 이번 연구에서 쓴 detector:
  - 보정된 CPU 단일 임계값(calibrated CPU threshold)
  - Isolation Forest(정상 데이터만으로 학습한 c1, 정상 범위를 넓혀 다시 학습한 c3-A)
- **Recovery Policy(guarded)**: 신호를 받아 조치 여부를 결정(record_only / act 모드, 중복 조치·쿨다운·감사 기록).
- **Argo Rollouts + Traffic Switch**: 미리 준비한 preview Pod로 Blue/Green 승격.
- **Verification**: API 응답이 아니라 새 Pod·이전 Pod의 실제 요청 카운터로 전환을 확인.

## 3. Architecture

```
            ┌──────────── 현재 구현·검증 ────────────┐
Environment │ Detection Layer (CPU threshold | IF)   │
(vLLM CPU,  │        ↓ anomaly signal                │
 K8s, Prom) │ Recovery Policy (guarded)              │
            │        ↓                               │
            │ Argo Rollouts → Kubernetes Service      │
            │        ↓                               │
            │ vLLM preview Pod → Traffic Verification │
            └────────────────────────────────────────┘
후속 연구(미구현): Environment → Detector Validation / Selection → Active Detector → (위와 같은 공통 Recovery 경로)
```

## 4. Key Results (공식 실험으로 확인된 숫자만)

| 실험 | 결과 |
|---|---|
| **A/B 선제 전환**(공식 5쌍) | 기록만 한 A의 누적 SLO 위반: 204.504 / ≥210.83 / 201.68 / 287.79 / 207.396 s, 실제 전환한 B: **5/5 모두 0 s**. 실제 전환이 5쌍 모두에서 누적 SLO 피해를 줄였다(부호 검정 p = 0.0625, 기술값). |
| **B/C/K 비교**(c1 IF 대 CPU 임계값) | c1 IF가 보정 CPU 임계값(C)보다 낫다는 증거 없음(D 중앙값 0). 기존 고정 임계값 K(2.7 core)는 5/5 늦고 피해가 컸다. |
| **N1 정상 운전**(6 세션) | c1 IF가 보정 CPU 임계값보다 불필요 신호를 더 일찍 냄(6/6). 원인: 정상 학습 범위가 좁음(무결성 감사로 확인). |
| **c3-A 정상 범위 확장 재학습**(사전등록, 블라인드 검증 4 세션) | 정상 운전 불필요 신호 **c1 14 → c3-A 1(92.9% 감소)**, 사전등록 판정 **PROMISING**. 압박 기준 E는 사전 정의상 5/5 통과했지만, 5개 중 4개는 첫 3연속에 주입 전 평가 1개가 포함됨(주입 뒤만 보면 363초 이내 2/5). |

## 5. What We Learned

- 단순 CPU 압박에서는 **보정된 CPU 임계값이 충분히 강했다.**
- Isolation Forest는 **대표성 있는 정상 데이터 범위에 민감했고**, 정상 범위를 넓히자(c3-A) 불필요 신호가 크게 줄었다(c1 대비).
- 현재 CPU 기반 vLLM 환경에서는 LLM 지표를 넣은 다변량 ML이 CPU 임계값보다 낫다는 것을 **공정하게 검증할 조건을 만들지 못했다**(Axis 2 NO_GO, 문헌 기반 시나리오도 환경 변경 필요).
- 효과의 핵심은 "검증된 전환"이었고, 탐지기는 환경에 따라 바뀔 수 있는 부품이다. **AI가 CPU 임계값보다 우월하다고 주장하지 않는다.**

## 6. Detector-Agnostic Design

이번 결과에서 도출된 설계 원칙: 환경마다 적합한 detector는 다를 수 있으므로 `선택된 detector → 공통 Recovery Policy → 공통 Rollout·트래픽 검증`으로 분리한다.

| 구분 | 내용 |
|---|---|
| **현재(구현·검증)** | CPU 임계값과 IF를 같은 recovery 경로에 연결해 비교(B/C/K, N1), guarded 정책·승격·트래픽 검증 경로 실측 |
| **후속 연구(미구현)** | 환경 profiling, 환경별 detector 검증·선택, active/shadow detector 분리, 새 detector 추가 절차 |

자동 benchmarking·자동 최적 detector 선택·여러 detector 자동 학습은 **구현하지 않았다**.

## 7. Repository Structure (주요 폴더)

```
anomaly-detection/   detector 구현: score server, c1/c3 IF artifact 메타데이터, CPU 임계값 규칙(bck_rules), c1 live/shadow detector
recovery-policy/     guarded 복구 정책(guarded_*.py, main_guarded.py) + 초기 정책 서비스(main.py, 역사적)
experiments/         실험 실행기·분석기·테스트
  c3_normal_coverage/            c3-A 사전등록 수집·학습·검증 코드
  llm_multivariate_added_value/  Axis 2 지표 감사
  llm_failure_scenario_research/ 문헌 조사·S3 KV 압박 계획
  results/                       공식 사전등록 문서·평가 요약 일부(원자료는 저장소 밖 보존)
gitops/              Argo Rollouts(Blue/Green)·Service·RBAC 매니페스트
chaos/               부하·CPU/I/O 압박 생성기, workload ladder
monitoring/, cluster/  관측성·클러스터 설정
docs/final/          현재 결과 요약(CURRENT)
docs/design/         단계별 실험 계약·중간 설계(HISTORICAL, provenance 보존)
```

`finops/`, `operator/`, `pvc-backup/`는 초기 보너스 계획으로 **구현되지 않았다**.

## 8. Reproducibility / Evidence

- 공식 실험 계약·freeze manifest(SHA256)·분석기·테스트는 저장소에 있다.
- **clone만으로 모든 실험을 완전히 재현할 수는 없다**:
  - 원 실험 evidence(`experiments/results/*` 원자료)는 저장소 밖에 보존(관례상 미커밋)
  - `model.pkl`·`scaler.pkl`은 `.gitignore` 대상(메타데이터·해시만 커밋)
  - freeze 해시는 원래 작업 폴더의 파일 바이트(CRLF/LF 혼재) 기준이고 저장소 blob은 `core.autocrlf=true`로 LF 정규화됨 → 새 checkout(Windows 포함)에서는 내용이 같아도 줄바꿈 차이로 provenance 해시가 불일치할 수 있음(2026-10-11 snapshot worktree에서 확인: 불일치 59건 전부 줄바꿈 차이)
  - 일부 frozen 테스트는 "공식 실행 전" 상태를 가정해 지금은 실패함
- 상세: [docs/final/FINAL-RESEARCH-SYNTHESIS-20261011.md](docs/final/FINAL-RESEARCH-SYNTHESIS-20261011.md) §6

## 9. Current Limitations

- CPU 기반 소형 LLM(Qwen2.5-0.5B, vLLM CPU) 단일 환경, workload 다양성 제한
- c3-A 검증 세션 n = 4, 검증·학습이 같은 block(날짜·Pod)을 공유
- AI(IF)가 보정 CPU 임계값보다 낫다는 것은 확인되지 않음(c3-A는 c1과만 비교)
- 운영 GPU·사설 클라우드로의 일반화는 검증하지 않음

## 10. Future Work

**최종 후속 연구**: 환경별 workload·자원·병목 특성에 따라 적합한 detector를 검증·선택하고, 선택된 detector의 anomaly signal을 기존 Recovery Policy와 Argo Rollouts 기반 복구 파이프라인에 공통으로 연결한다(`followup-detector-selection` branch에서 사용자 승인 후 시작 예정).

## Documents

| 목적 | 문서 |
|---|---|
| 현재 연구 요약·후속 방향 | [docs/final/FINAL-RESEARCH-SYNTHESIS-20261011.md](docs/final/FINAL-RESEARCH-SYNTHESIS-20261011.md) |
| 공식 실험 증거·주장 판정표(A/B·B/C/K·N1) | [docs/final/FINAL-EVIDENCE-SUMMARY-20261010.md](docs/final/FINAL-EVIDENCE-SUMMARY-20261010.md) |
| 발표·예상 질문 | [docs/final/PRESENTATION-PACKAGE-20261010.md](docs/final/PRESENTATION-PACKAGE-20261010.md) |
| c3-A 사전등록·결과 | [experiments/results/c3_normal_coverage_prereg_20261010/](experiments/results/c3_normal_coverage_prereg_20261010/), [experiments/results/c3_normal_coverage_evaluation/](experiments/results/c3_normal_coverage_evaluation/) |
| Axis 2·문헌·S3 | [experiments/llm_multivariate_added_value/](experiments/llm_multivariate_added_value/), [experiments/llm_failure_scenario_research/](experiments/llm_failure_scenario_research/) |
| 과거 실험 계약·중간 설계(historical) | [docs/design/](docs/design/), 초기 제안서 [docs/proposal.md](docs/proposal.md) |
