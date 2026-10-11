# LLM 서빙 성능 저하 대응을 위한 Kubernetes 자동 복구 시스템

> 느리지만 살아 있는 LLM 추론 서비스의 이상 신호를 받아, guarded recovery policy가 Argo Rollouts Blue/Green 전환과 실제 traffic verification까지 수행하는 Kubernetes 자동 복구 시스템. 특정 detector에 종속되지 않도록 Detection Layer와 Recovery Layer를 분리한다.

- 지도교수: 김영한 교수님 · 작성자: 박소정 (숭실대 전자정보공학부 IT융합전공)
- 가장 먼저 읽을 문서: [docs/final/FINAL-RESEARCH-SYNTHESIS-20261011.md](docs/final/FINAL-RESEARCH-SYNTHESIS-20261011.md)
- Phase 1 최종 연구 상태: tag `phase1-final-20261011`

## 1. Problem

LLM 서빙 Pod는 죽지 않고 느려지기만 하는 경우가 많다(예: CPU 압박). Kubernetes 기본 self-healing은 Pod가 살아 있으면 개입하지 않으므로 사용자 응답 지연(SLO 위반)이 길게 이어질 수 있다.

## 2. System Overview

- **Detection Layer**: 이상 신호를 내는 교체 가능한 부품(이번 연구: 보정 CPU 임계값, Isolation Forest).
- **Recovery Policy(guarded)**: 신호를 받아 조치 여부를 결정한다(record_only / act, 중복 조치 방지·쿨다운·감사 기록).
- **Argo Rollouts**: 미리 준비한 preview Pod를 Blue/Green으로 승격한다.
- **Traffic Switch**: Kubernetes Service selector가 새 Pod로 바뀐다.
- **Verification**: API 응답이 아니라 새 Pod·이전 Pod의 실제 요청 카운터로 전환을 확인한다.

## 3. Architecture

```
Environment (Kubernetes · vLLM CPU · Prometheus)
      ↓
Detection Layer
      ↓
Selected Detector        ← 현재: 실험마다 사람이 지정(CPU threshold 또는 IF)
      ↓
Recovery Policy (guarded)
      ↓
Argo Rollouts (Blue/Green promotion)
      ↓
Traffic Switch (Service selector)
      ↓
Verification (새/이전 Pod 실제 요청 수)
```

| 구분 | 내용 |
|---|---|
| **현재 구현·검증** | CPU threshold·IF detector 연구와 비교, guarded recovery, 실제 promotion, 실제 traffic verification |
| **후속 연구(미구현)** | Environment Profile → Detector Validation → Active / Shadow Detector Selection → 공통 Recovery Pipeline |

## 4. Detector Strategy

이번 연구에서는 같은 복구 경로에 다음 detector를 연결해 비교했다.

- **보정 CPU 임계값**(calibrated CPU threshold): `cpu_mean > 1.68425`, 3연속
- **Isolation Forest**: 정상 데이터로만 학습. c1(초기), c3-A(정상 범위를 넓혀 다시 학습)

환경에 따라 적합한 detector가 달라질 수 있으므로 recovery system을 특정 detector에 종속시키지 않는다. **자동 detector 선택은 구현하지 않았다** - 후속 연구 방향이다.

## 5. Key Results (공식 실험으로 확인된 숫자만)

| 실험 | 결과 |
|---|---|
| **A/B 선제 전환**(공식 5쌍) | 기록만 한 A의 누적 SLO 위반 204.504 / ≥210.83 / 201.68 / 287.79 / 207.396 s, 실제 전환한 B **5/5 모두 0 s**. 실제 전환이 5쌍 모두에서 누적 SLO 피해를 줄였다(부호 검정 p = 0.0625, 기술값). |
| **B/C/K**(c1 IF 대 CPU 임계값) | c1 IF가 보정 CPU 임계값보다 낫다는 증거 없음(D 중앙값 0). 기존 고정 임계값(2.7 core)은 5/5 늦고 피해가 컸다. |
| **N1 정상 운전**(6 세션) | c1 IF가 보정 CPU 임계값보다 불필요 신호를 더 일찍 냄(6/6). 원인: 정상 학습 범위가 좁음(무결성 감사로 확인). |
| **c3-A**(사전등록, 블라인드 검증 4 세션) | 정상 운전 불필요 신호 **c1 14 → c3-A 1(92.9% 감소)**, 사전등록 판정 **PROMISING**. 압박 기준은 사전 정의상 5/5 통과했지만, 4/5는 첫 3연속에 주입 전 평가 1개가 포함됨(주입 뒤만 보면 363초 이내 2/5). |
| **Axis 2**(LLM 지표 다변량 detector) | **NO_GO** - 현재 환경에서 LLM 지표가 정상·CPU 압박에서 거의 변하지 않고, 변하게 할 부하는 SLO를 먼저 깸. 문헌 기반 KV cache 압박(S3)도 환경 변경 없이는 불가(예상), pilot 미실행. |

## 6. What We Learned

- 단순 CPU 압박에서는 **보정된 CPU 임계값이 충분히 강했다.**
- Isolation Forest는 **정상 데이터 범위에 민감했다**. 정상 범위를 넓힌 c3-A에서 정상 운전 불필요 신호가 크게 줄었다(c1 대비).
- 현재 CPU 기반 vLLM 환경에서 AI가 CPU 임계값보다 낫다는 추가 우월성은 **확인되지 않았다.**
- 효과의 핵심은 "검증된 전환"이었다.

## 7. Current Design Principle - Detector-agnostic Recovery

복구 경로(guarded 정책 → 승격 → 전환 → 검증)는 신호가 IF든 CPU 규칙이든 같게 동작했다(A/B, B/C/K). 그래서 detector는 환경에 맞게 바꿀 수 있는 부품으로 두고, Recovery Layer는 공통으로 재사용한다.

## 8. Limitations

- CPU 기반 소형 LLM(Qwen2.5-0.5B, vLLM CPU) 단일 환경, workload 다양성 제한
- c3-A 검증 세션 n = 4, 검증·학습이 같은 block(날짜·Pod) 공유, c3-A는 c1과만 비교(CPU 임계값과의 새 비교 없음)
- 운영 GPU·사설 클라우드 일반화 미검증
- **재현성 범위**: 실험 계약·freeze manifest(SHA256)·분석기·테스트는 저장소에 있지만 clone만으로 전부 재현할 수는 없다.
  - 원 실험 evidence(`experiments/results/*` 원자료)는 저장소 밖 보존, `model.pkl`·`scaler.pkl`은 `.gitignore` 대상(메타데이터·해시만 커밋)
  - freeze 해시는 원래 작업 폴더 파일 바이트(CRLF/LF 혼재) 기준이라 새 checkout에서는 내용이 같아도 줄바꿈 차이로 provenance 해시가 다를 수 있음
  - "공식 실행 전" 상태를 가정한 일부 frozen 테스트는 지금 실패함

## 9. Future Work

```
Environment Profile → Detector Validation → Selected (Active) Detector + Shadow 후보 → 공통 Recovery Pipeline
```

환경별 workload·자원·병목 특성에 따라 적합한 detector를 검증·선택하고, 선택된 detector의 신호를 같은 복구 경로에 연결한다. 목표는 AI 우월성 증명이 아니라 환경에 맞는 detector 선택이다. 자동 benchmarking·자동 선택·여러 detector 자동 학습은 아직 구현하지 않았다.

## 10. Repository Structure

```
anomaly-detection/   detector: score server, IF artifact 메타데이터(c1·c3-A), CPU 임계값 규칙, c1 live/shadow detector
recovery-policy/     guarded 복구 정책(guarded_*.py, main_guarded.py)과 초기 정책 서비스(main.py)
gitops/              Argo Rollouts(Blue/Green)·Service·RBAC·ServiceMonitor 매니페스트
chaos/               부하·CPU/I/O 압박 생성기와 시나리오 정의
experiments/         실험 실행기·분석기·테스트, c3-A·Axis 2·문헌 연구 폴더, 공식 사전등록·평가 요약(results/)
audit-log/           recovery-policy가 남긴 조치 감사 기록(bot commit)
docs/final/          현재 문서
docs/design/         실험 계약·freeze manifest·중간 설계(historical/frozen)
docs/history/        초기 제안서와 대체된 요약
```

`cluster/`, `monitoring/`, `finops/`, `operator/`, `pvc-backup/`에는 초기 계획 README만 있다(설정 파일·구현 없음, 각 README 상단에 상태 표시).

## 11. Documents

**현재 문서**

| 목적 | 문서 |
|---|---|
| Phase 1 연구 정리·후속 방향 | [docs/final/FINAL-RESEARCH-SYNTHESIS-20261011.md](docs/final/FINAL-RESEARCH-SYNTHESIS-20261011.md) |
| 공식 실험 증거·주장 판정표(A/B·B/C/K·N1) | [docs/final/FINAL-EVIDENCE-SUMMARY-20261010.md](docs/final/FINAL-EVIDENCE-SUMMARY-20261010.md) |
| 발표 문구·예상 질문 | [docs/final/PRESENTATION-PACKAGE-20261010.md](docs/final/PRESENTATION-PACKAGE-20261010.md), [docs/presentation/](docs/presentation/) |
| c3-A 결과 | [experiments/results/c3_normal_coverage_evaluation/](experiments/results/c3_normal_coverage_evaluation/) |
| Axis 2·문헌·S3 | [experiments/llm_multivariate_added_value/](experiments/llm_multivariate_added_value/), [experiments/llm_failure_scenario_research/](experiments/llm_failure_scenario_research/) |

**Historical / Frozen experiment evidence**(당시 기준 문서 - 현재 설계 설명이 아님)

| 내용 | 위치 |
|---|---|
| 실험 계약·freeze manifest·중간 설계 | [docs/design/](docs/design/) |
| c3-A 사전등록·수집 runbook(frozen) | [experiments/results/c3_normal_coverage_prereg_20261010/](experiments/results/c3_normal_coverage_prereg_20261010/) |
| 초기 제안서·대체된 요약 | [docs/history/](docs/history/) |
