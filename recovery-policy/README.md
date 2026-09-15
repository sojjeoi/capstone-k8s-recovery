# 복구 정책 결정 서비스

`anomaly-detection`의 위험 신호(예측 경로) + Alertmanager 알림(반응 경로, 필수)을 둘 다 받아 조치를 결정하고, Kubernetes API(Argo Rollouts promotion)로 시간민감적 트래픽 전환을 즉시 실행한다. **Git은 감사 기록 전용** — 판단 근거·실행 결과를 사후 추적용으로 커밋할 뿐, 실행 자체를 막거나 트리거하는 승인 게이트가 아니다(정상 상태·배포 구성은 별도로 GitOps/ArgoCD가 관리).

in-cluster Deployment로 배포한다(`gitops/apps/recovery-policy/`) — `rollouts_client.py`는 `KUBERNETES_SERVICE_HOST` 존재 여부로 in-cluster/로컬을 자동 판단.

- `main.py`: 신호 수신 엔드포인트 (`/signal`, `/webhooks/alertmanager`)
- `policy.py`: 신호 유형별 조치 결정 (규칙 기반, LLM 미사용)
- `safety.py`: idempotency·cooldown·promote 전후 검증
- `rollouts_client.py`: K8s API/CLI 얇은 래퍼 — promote 실행 (완성·실측 검증됨)
- `decision_log.py`: 판단 근거 레코드 생성
- `git_client.py`: PVC(`recovery-policy-data` PVC, `/data`에 마운트)에 감사기록을 동기 기록하고, 단일 백그라운드 워커가 Git에 비동기 커밋·push. 재시작 시 미전송(`pending`/`failed`) 기록을 다시 처리(guideline.md 9-6절: PVC 기반 단순 감사 outbox)
- `fixed_threshold.py`: 3-way 비교용 고정 임계치 baseline (예: CPU>90% → promote_preview)

## Git 자격증명 설정 (배포 전 1회 필요)

`git_client.py`가 push할 수 있으려면 `recovery-policy-git-credentials` Secret이 `vllm-serving` 네임스페이스에 있어야 한다. **토큰 값은 이 저장소에 절대 커밋하지 말 것** — 아래 명령을 직접 실행해서 생성한다.

1. GitHub에서 이 저장소(`sojjeoi/capstone-k8s-recovery`)에 push 가능한 PAT 발급 (Settings → Developer settings → Fine-grained tokens, Contents: Read and write 권한만)
2. Secret 생성:
   ```
   kubectl create secret generic recovery-policy-git-credentials \
     -n vllm-serving --from-literal=token=<발급받은 PAT>
   ```
3. worker 노드에 PV가 가리키는 디렉터리 준비: `ssh capstone-worker "mkdir -p /home/ubuntu/recovery-policy-data"`
4. `kubectl apply -f gitops/apps/recovery-policy/pv.yaml -f gitops/apps/recovery-policy/pvc.yaml`
