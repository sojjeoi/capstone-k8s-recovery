# 복구 정책 결정 서비스

`anomaly-detection`의 위험 신호(예측 경로) + Alertmanager 알림(반응 경로)을 둘 다 받아 조치를 결정하고, Git 매니페스트를 변경·커밋한다.

- `main.py`: 신호 수신 엔드포인트
- `policy.py`: 신호 유형별 조치 결정 (규칙 기반, LLM 미사용)
- `git_client.py`: Git 매니페스트 변경·커밋 (ArgoCD가 감지해 동기화)
- `fixed_threshold.py`: 3-way 비교용 고정 임계치 baseline (예: CPU>90% → 재시작)
