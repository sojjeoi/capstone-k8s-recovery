# experiments - 공식 실험 실행기·분석기·근거

**검증 근거가 필요할 때 보는 곳**이다. 처음 보는 사람은 먼저 [docs/final/](../docs/README.md)의 결과 문서를 읽고, 숫자의 출처를 확인하고 싶을 때 아래로 내려오면 된다. 여기의 실행기는 실제 클러스터를 바꾸므로 사용자 승인 없이 실행하지 않는다.

## 결과 → 근거 위치

| 결과 | 실행기·분석기 | 근거 파일 |
|---|---|---|
| Phase 1 A/B(공식 5쌍 + 사전등록 추가 3쌍) | `run_guarded_pilot.py`, `guarded_ab_analysis.py` | [results/capstone_guarded_ab_v1/EVIDENCE-INDEX.md](results/capstone_guarded_ab_v1/EVIDENCE-INDEX.md) |
| Phase 2 detector 선택(Stage 1) | [phase2_detector_selection/](phase2_detector_selection/) `run_stage1.py`, `selection.py` | [results/phase2_stage1/](results/phase2_stage1/), [results/phase2_selection/](results/phase2_selection/) (`active-profile.json` 포함) |
| Phase 2 선택된 detector로 실제 복구(Stage 2) | `phase2_detector_selection/run_stage2.py`, `p2_executor.py` | [results/phase2_stage2/](results/phase2_stage2/), [results/phase2_verdict/phase2-verdict-evidence.json](results/phase2_verdict/phase2-verdict-evidence.json) |
| c3-A 정상 불필요 신호 블라인드 검증 | [c3_normal_coverage/](c3_normal_coverage/) | [results/c3_normal_coverage_evaluation/](results/c3_normal_coverage_evaluation/), 사전등록 [results/c3_normal_coverage_prereg_20261010/](results/c3_normal_coverage_prereg_20261010/) |
| B/C/K(c1 IF 대 CPU 임계값), N1 정상 운전 | `run_guarded_bck.py`·`guarded_bck_analysis.py`, `run_guarded_normal_bc.py`·`guarded_normal_bc_analysis.py` | 요약은 [docs/final/FINAL-EVIDENCE-SUMMARY-20261010.md](../docs/final/FINAL-EVIDENCE-SUMMARY-20261010.md) (원자료는 저장소 밖 보존) |
| Axis 2(LLM 지표 다변량), 문헌·S3 계획 | [llm_multivariate_added_value/](llm_multivariate_added_value/), [llm_failure_scenario_research/](llm_failure_scenario_research/) | 각 폴더 안 문서 (Axis 2 = NO_GO, S3 pilot 미실행) |

공통 부품: `slo_judge.py`·`slo_episodes.py`(서비스 품질 판정), `guarded_cleanup_gate.py`(실행 전후 시작 상태 확인·복원), `guarded_pilot_session.ps1`(세션 래퍼), `run_demo_phase2.py`(발표 시연 전용 실행기, 공식 결과에 포함되지 않음).

## 주의

- `results/`에는 공식 사전등록·분석·요약 중 일부만 커밋돼 있다. 대용량 원자료(탐지기 tick 기록·로그)는 저장소 밖에 보존한다.
- 그 밖의 `run_*_trial.py`·어댑터·탐색 도구는 초기·중간 단계 실험 도구다(historical). 일부는 freeze manifest가 경로·해시로 참조하므로 이동·삭제하지 않는다.
