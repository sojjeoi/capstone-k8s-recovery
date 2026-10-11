# experiments - 실험 실행기·분석기

- 공식 실험 실행기·분석기: `run_guarded_pilot.py`(A/B), `run_guarded_bck.py`·`guarded_bck_analysis.py`(B/C/K), `run_guarded_normal_bc.py`·`guarded_normal_bc_analysis.py`(N1), `guarded_ab_analysis.py`, `slo_judge.py`·`slo_episodes.py`(SLO 판정)
- `c3_normal_coverage/`: c3-A 사전등록 수집·학습·보정·블라인드 검증 코드(freeze manifest 포함)
- `llm_multivariate_added_value/`: Axis 2 지표 감사(NO_GO)
- `llm_failure_scenario_research/`: 문헌 조사·S3 KV cache 압박 계획(pilot 미실행)
- `results/`: 공식 사전등록 문서·평가 요약 일부(원자료는 저장소 밖 보존)
- 그 밖의 `run_*_trial.py`·어댑터: 초기·중간 단계 실험 도구(historical, 일부는 freeze manifest가 참조하므로 유지)

결과 요약: [../docs/final/FINAL-RESEARCH-SYNTHESIS-20261011.md](../docs/final/FINAL-RESEARCH-SYNTHESIS-20261011.md)
