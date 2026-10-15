# gitops - 배포 매니페스트

| 경로 | 내용 |
|---|---|
| [apps/vllm-serving/](apps/vllm-serving/) | **현재 서빙 구성**: vLLM Argo Rollout(Blue/Green, `rollout.yaml`), active/preview Service(`service.yaml`), RBAC, ServiceMonitor·PrometheusRule, warm-up probe |
| [apps/monitoring/](apps/monitoring/) | Prometheus·Grafana 저장소(PV·StorageClass) |
| [apps/recovery-policy/](apps/recovery-policy/) | **초기** Recovery Policy(`main.py`) 배포 매니페스트 (historical) |

- Blue/Green 구조: `vllm-active` Service가 사용자 트래픽을, `vllm-preview` Service가 대기 중인 새 Pod를 가리킨다. 승격하면 Argo Rollouts가 `vllm-active`의 selector를 새 ReplicaSet hash로 바꾼다.
- 최종 guarded 정책(`recovery-policy/main_guarded.py`, 클러스터의 `guarded-recovery-policy`)의 배포 매니페스트는 이 폴더에 포함돼 있지 않다.
- 정상 상태는 매니페스트로 관리하고, 시간에 민감한 복구 전환은 Recovery Policy가 Kubernetes API(Argo Rollouts 승격)로 직접 실행한다.
