# 복구 정책 결정 서비스

`anomaly-detection`의 위험 신호(예측 경로) + Alertmanager 알림(반응 경로, 필수)을 둘 다 받아 조치를 결정하고, Kubernetes API(Argo Rollouts promotion)로 시간민감적 트래픽 전환을 즉시 실행한다. **Git은 감사 기록 전용** — 판단 근거·실행 결과를 사후 추적용으로 커밋할 뿐, 실행 자체를 막거나 트리거하는 승인 게이트가 아니다(정상 상태·배포 구성은 별도로 GitOps/ArgoCD가 관리).

in-cluster Deployment로 배포한다(`gitops/apps/recovery-policy/`) — `rollouts_client.py`는 `KUBERNETES_SERVICE_HOST` 존재 여부로 in-cluster/로컬을 자동 판단.

- `main.py`: 신호 수신 엔드포인트 (`/signals/anomaly`, `/webhooks/alertmanager`)
- `policy.py`: 신호 유형별 조치 결정 (규칙 기반, LLM 미사용)
- `safety.py`: idempotency·cooldown·promote 전후 검증
- `rollouts_client.py`: K8s API/CLI 얇은 래퍼 — promote 실행 (완성·실측 검증됨)
- `decision_log.py`: 판단 근거 레코드 생성
- `git_client.py`: decision_log 레코드를 `audit-log/`에 커밋·push (비동기, 감사 전용)
- `fixed_threshold.py`: 3-way 비교용 고정 임계치 baseline (예: CPU>90% → promote_preview)
