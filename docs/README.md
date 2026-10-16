# docs - 문서 안내

| 폴더 | 성격 | 언제 보나 |
|---|---|---|
| [final/](final/) | **현재 최종 결과·정리** | 결과 수치와 해석 범위를 확인할 때 - 여기부터 |
| [demo/](demo/) | 발표 시연 runbook | 시연 절차를 볼 때 |
| [design/](design/) | 실험 계약·사전등록·freeze manifest·중간 설계 (**frozen / historical**) | 어떤 규칙을 결과 전에 고정했는지 확인할 때. 당시 기준 문서이며 현재 설계 설명이 아니다 |
| [history/](history/) | 초기 제안서·대체된 요약 (**historical**) | 프로젝트 출발점이 궁금할 때 |

## final/ - 현재 문서

| 문서 | 내용 |
|---|---|
| [PHASE2-FINAL-RESULT-20261012.md](final/PHASE2-FINAL-RESULT-20261012.md) | **최종 결과**: 환경별 detector 선택(CPU 5/5, IF 0/5) → 선택된 detector로 실제 복구 3/3, 말할 수 있는 것/없는 것, caveat |
| [FINAL-RESEARCH-SYNTHESIS-20261011.md](final/FINAL-RESEARCH-SYNTHESIS-20261011.md) | Phase 1 전체 정리(A/B, B/C/K, N1, c3-A, Axis 2)와 무결성 감사 |
| [FINAL-EVIDENCE-SUMMARY-20261010.md](final/FINAL-EVIDENCE-SUMMARY-20261010.md) | 공식 실험별 증거·주장 판정표 |
| [PHASE1-AB-ADDITIONAL-REPLICATION-20261013.md](final/PHASE1-AB-ADDITIONAL-REPLICATION-20261013.md) | A/B 사전등록 추가 반복 3쌍(ab06–ab08) - 공식 5쌍과 분리 보고 |
| [AB-TRAFFIC-SWITCH-TIMING-20261015.md](final/AB-TRAFFIC-SWITCH-TIMING-20261015.md) | A/B 실제 전환 8회의 승격 요청 → selector 변경 확인 시간 |
| [GUARD-REFUSAL-EVIDENCE-20261016.md](final/GUARD-REFUSAL-EVIDENCE-20261016.md) | Recovery Guard 거부 경로: 조건이 어긋나면 승격을 호출하지 않는 17개 시나리오(단위 테스트, 실제 클러스터 실험 아님) |
| [PRESENTATION-PACKAGE-20261010.md](final/PRESENTATION-PACKAGE-20261010.md) | 발표 문구·예상 질문 (2026-10-10 기준 - 이후 Phase 2 결과는 위 최종 결과 문서가 우선) |

## design/ - 자주 찾는 frozen 문서

- [PHASE2-DETECTOR-SELECTION-PREREG-v1-rev6.md](design/PHASE2-DETECTOR-SELECTION-PREREG-v1-rev6.md) - Phase 2 detector 선택 사전등록(Gate 규칙)
- [phase8-v33c1-guarded-ab-comparison-contract-r2-20261007.md](design/phase8-v33c1-guarded-ab-comparison-contract-r2-20261007.md) - Phase 1 A/B 비교 계약(canonical 시작 상태 포함)
- [slo-definition.md](design/slo-definition.md) - 서비스 품질(SLO) 판정 정의

design/의 파일은 freeze 해시·실행기 경로가 참조하므로 이동·수정하지 않는다.

## 세부 연구 기록 - 실험 단계별 결과

| 실험 | 한 줄 결과 |
|---|---|
| Phase 1 A/B | 같은 압박에서 기록만 한 경우 vs 실제 전환. 공식 5쌍 5/5 + 사전등록 추가 3쌍 3/3 같은 방향(8쌍 합산은 참고 관측) |
| B/C/K | 초기 Isolation Forest(c1)가 보정 CPU 임계값보다 낫다는 증거 없음. 기존 고정 임계값(2.7 core)은 5/5 늦고 피해가 컸음 |
| N1 정상 운전 | 초기 Isolation Forest(c1)가 불필요 신호를 더 일찍 냄(정상 학습 범위가 좁음) |
| c3-A | 정상 범위를 넓혀 다시 학습해 정상 불필요 신호 14 → 1, 사전등록 판정 PROMISING |
| Axis 2 | LLM 지표를 쓰는 다변량 감지기 NO_GO - 현재 환경에서 LLM 지표가 거의 변하지 않음 |
| Phase 2 | 사전등록 규칙으로 감지기 선택(CPU 5/5 · IF 0/5) → 선택된 감지기로 실제 복구 3/3, `PHASE2_ARCHITECTURE_VALIDATED` |

모든 공식 실험은 결과를 보기 전에 비교 지표·반복 수·중단 규칙·선택 기준을 사전등록하고 고정했다. 서비스 품질 판정 정의: [design/slo-definition.md](design/slo-definition.md). 실험별 실행기·근거 위치: [../experiments/README.md](../experiments/README.md)

## 재현성 범위

- 실험 계약·freeze manifest(SHA256)·분석기·테스트는 저장소에 있지만 clone만으로 전부 재현할 수는 없다.
- 대용량 원자료(탐지기 tick 기록·로그)는 저장소 밖에 보존하고, `model.pkl`·`scaler.pkl`은 커밋하지 않았다(메타데이터·해시만 커밋).
- freeze 해시는 원래 작업 폴더의 파일 바이트(CRLF/LF 혼재) 기준이라, 새 checkout에서는 줄바꿈 차이로 일부 해시가 다를 수 있다.
- 일부 frozen 테스트는 공식 결과가 저장소에 추가되기 전 상태를 가정하므로 지금 checkout에서는 실패할 수 있다(현재 구현의 regression이 아니라 전제 차이).
- vLLM·Recovery Policy 이미지는 노드에 직접 적재한 로컬 이미지라 레지스트리에서 받을 수 없다.
- Phase 1 최종 연구 상태는 tag `phase1-final-20261011`에 있다. 초기 계획 README만 있던 `cluster/`, `monitoring/`, `finops/`, `operator/`, `pvc-backup/`와 참조되지 않는 중간 기록 일부는 2026-10-14 정리에서 master에서 뺐다(tag `phase1-final-20261011`·branch `research-snapshot-20261011`에 남아 있음).
