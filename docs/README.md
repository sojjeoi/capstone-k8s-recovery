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
| [PRESENTATION-PACKAGE-20261010.md](final/PRESENTATION-PACKAGE-20261010.md) | 발표 문구·예상 질문 (2026-10-10 기준 - 이후 Phase 2 결과는 위 최종 결과 문서가 우선) |

## design/ - 자주 찾는 frozen 문서

- [PHASE2-DETECTOR-SELECTION-PREREG-v1-rev6.md](design/PHASE2-DETECTOR-SELECTION-PREREG-v1-rev6.md) - Phase 2 detector 선택 사전등록(Gate 규칙)
- [phase8-v33c1-guarded-ab-comparison-contract-r2-20261007.md](design/phase8-v33c1-guarded-ab-comparison-contract-r2-20261007.md) - Phase 1 A/B 비교 계약(canonical 시작 상태 포함)
- [slo-definition.md](design/slo-definition.md) - 서비스 품질(SLO) 판정 정의

design/의 파일은 freeze 해시·실행기 경로가 참조하므로 이동·수정하지 않는다.
