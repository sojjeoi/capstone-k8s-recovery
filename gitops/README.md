# GitOps

ArgoCD가 감시하는 "정상 상태" 선언. 모든 복구 조치는 이 디렉토리의 매니페스트 변경(Git 커밋)을 통해서만 이뤄진다 — ArgoCD API를 직접 호출하지 않는 것이 감사 가능성(auditability) 확보의 핵심이다.

- `argocd/application.yaml`: ArgoCD Application 정의
- `apps/vllm-serving/`: vLLM 서빙을 위한 Rollout(Argo Rollouts), Service, ConfigMap
