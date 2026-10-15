# gitops - 배포 매니페스트

| 경로 | 내용 |
|---|---|
| [apps/vllm-serving/](apps/vllm-serving/) | **현재 서빙 구성**: vLLM Argo Rollout(Blue/Green, `rollout.yaml`), active/preview Service(`service.yaml`), RBAC, ServiceMonitor·PrometheusRule, warm-up probe |
| [apps/monitoring/](apps/monitoring/) | Prometheus·Grafana 저장소(PV·StorageClass) |
| [apps/guarded-recovery-policy/](apps/guarded-recovery-policy/guarded-recovery-policy.yaml) | **현재 최종 Recovery Policy**(`recovery-policy/main_guarded.py`) 배포 매니페스트 - ServiceAccount·Role·RoleBinding·PV·PVC·Service·Deployment. 2026-10-16 실제 클러스터에서 읽기 전용으로 내보낸 것(토큰 값 없음, Secret 참조만) |
| [apps/recovery-policy/](apps/recovery-policy/) | **초기** Recovery Policy(`main.py`) 배포 매니페스트 (historical) |

- Blue/Green 구조: `vllm-active` Service가 사용자 트래픽을, `vllm-preview` Service가 대기 중인 새 Pod를 가리킨다. 승격하면 Argo Rollouts가 `vllm-active`의 selector를 새 ReplicaSet hash로 바꾼다.
- guarded 정책은 평상시 `GUARDED_MODE=record_only`로 동작하고, 실험·시연에서만 승인 후 `act`로 바꿨다. Role은 Rollout 읽기·승격(patch/update)과 Pod·Service·Endpoints 읽기만 허용한다.
- 정상 상태는 매니페스트로 관리하고, 시간에 민감한 복구 전환은 Recovery Policy가 Kubernetes API(Argo Rollouts 승격)로 직접 실행한다.
