# anomaly-detection - Detection Layer

이상 신호를 내는 detector 쪽 코드. 복구 쪽(`recovery-policy/`)과 분리되어 있고, 신호를 HTTP sink로 보낸다.

- `score_server.py`, `features.py`, `input_integrity.py`: Prometheus 지표(CPU·메모리 등)로 평가 창 특징을 만들고 점수·신호를 기록하는 서버
- `c1_live_detector.py`, `c1_shadow_detector.py`, `c1_policy_link.py`: Isolation Forest(c1) live/shadow 실행과 정책 연결
- `bck_rules/`, `c1_bck_live_detector.py`, `c1_bck_shadow_pair.py`, `fixed_threshold.py`: 보정 CPU 임계값 규칙과 IF·CPU 규칙 비교용 실행
- `v3/model_v33_c1/`, `v3/model_c3_normal_coverage/`: c1·c3-A 학습 스크립트와 artifact 메타데이터(`model.pkl`·`scaler.pkl`은 `.gitignore` 대상)
- 그 밖의 `v3/` 하위 폴더, `train.py`, `collect_*.py`, `data/`, `regime_configs/`: 초기 모델 단계 자료(historical)

결과 요약: [../docs/final/FINAL-RESEARCH-SYNTHESIS-20261011.md](../docs/final/FINAL-RESEARCH-SYNTHESIS-20261011.md)
