# gitops - 배포 매니페스트

- `apps/vllm-serving/`: vLLM 서빙 Argo Rollout(Blue/Green), active/preview Service, RBAC, ServiceMonitor
- `apps/recovery-policy/`, `apps/monitoring/`: 복구 정책 배포·관측성 저장소 설정
- 정상 상태는 이 매니페스트로 관리하고, 시간에 민감한 복구 전환은 recovery-policy가 Kubernetes API(Argo Rollouts 승격)로 직접 실행한다. Git(`audit-log/`)은 사후 감사 기록이다.
