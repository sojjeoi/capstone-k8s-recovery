# 이상 탐지 모델

K8s 지표 + vLLM 고유 지표를 특성 벡터로 구성해 Isolation Forest(비지도 이상탐지)로 학습하고, 실시간으로 위험도를 판단한다.

- `features.py`: 지표 → 특성 벡터 변환
- `train.py`: 모델 학습
- `score_server.py`: 실시간 이상 점수 계산, 임계치 초과 시 `recovery-policy`로 위험 신호 발행

"장애 발생 가능성을 예측"이 아니라 "이상 점수를 산출해 위험도를 판단"하는 것이 정확한 표현이다 (Isolation Forest는 비지도 모델).
