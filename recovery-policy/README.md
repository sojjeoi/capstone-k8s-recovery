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

## 수동 이미지 빌드·배포 절차 (worker 노드)

레지스트리 없이 worker 노드(`capstone-worker`)에서 직접 빌드해 containerd에 넣는다(`imagePullPolicy: Never`). **이미지에 들어가는 소스는 반드시 커밋된 blob과 바이트 단위로 같아야 한다.** 2026-09-19에 `core.autocrlf=true`(Windows 기본)인 PC에서 옵션 없는 `git archive`가 전 파일을 CRLF로 내보내 `git_askpass.sh`의 셔뱅이 `#!/bin/sh\r`가 됐고, 컨테이너 안에서 `cannot exec '/app/git_askpass.sh'`로 모든 감사 push가 실패했다(`docs/design/phase8-blue-green-preflight-incident.md` §40.2). Python은 CRLF를 허용해 `/healthz`·API가 정상이라 기존 배포 검증을 그대로 통과했다. 루트 `.gitattributes`가 `git_askpass.sh`만 LF로 고정하고(`test_git_askpass.py`가 고정) 다른 파일은 여전히 변환되므로, 아래 절차 - 특히 1번의 `-c core.autocrlf=false`와 2·4·5·7번의 SHA-256 대조 - 는 그대로 지켜야 한다.

아래에서 `<commit>`은 배포할 커밋, `<repo>`는 저장소 루트, `<out>`은 새 임시 디렉터리다. Windows Git Bash에서 컨테이너 안 경로(`/app/...`)를 넘길 때는 `MSYS_NO_PATHCONV=1`을 붙인다.

1. **커밋에서 내보내기** - 작업트리 복사나 옵션 없는 `git archive`는 쓰지 않는다:
   ```
   git -C <repo> -c core.autocrlf=false archive <commit> recovery-policy | tar -x -C <out>
   ```
2. **기준 해시 매니페스트** - 자기 추출본이 아니라 **커밋 blob**에서 만든 뒤, 추출본이 그것과 같은지 먼저 확인한다:
   ```
   cd <out>/recovery-policy
   for f in $(ls); do echo "$(git -C <repo> show <commit>:recovery-policy/$f | sha256sum | cut -d' ' -f1)  $f"; done > ../MANIFEST.sha256
   sha256sum -c ../MANIFEST.sha256      # 전부 OK여야 한다 (CRLF가 섞였으면 여기서 FAILED)
   ```
3. **워커로 전송** - 새 빌드 디렉터리로. 매니페스트는 빌드 컨텍스트 **밖**에 둔다(`COPY . .`로 이미지에 섞이지 않게):
   ```
   ssh capstone-worker "mkdir /tmp/recovery-policy-build-<commit>"
   scp <out>/recovery-policy/* capstone-worker:/tmp/recovery-policy-build-<commit>/
   scp <out>/MANIFEST.sha256 capstone-worker:/tmp/MANIFEST-recovery-policy-<commit>.sha256
   ```
4. **워커 측에서 확인한 뒤 빌드**:
   ```
   ssh capstone-worker "cd /tmp/recovery-policy-build-<commit> && sha256sum -c /tmp/MANIFEST-recovery-policy-<commit>.sha256 && sudo docker build -t recovery-policy:local ."
   ```
5. **롤아웃 전에 이미지 안을 검증** - 커밋 blob 대비 SHA-256이 전 파일 OK이고, 셔뱅이 LF이며 실행비트가 유지돼야 한다:
   ```
   ssh capstone-worker "cat /tmp/MANIFEST-recovery-policy-<commit>.sha256 | sudo docker run -i --rm --entrypoint sh recovery-policy:local -c 'cd /app && sha256sum -c - && head -c 12 git_askpass.sh | od -c | head -1 && ls -l git_askpass.sh'"
   ```
6. **반입·롤아웃**:
   ```
   ssh capstone-worker "sudo docker save recovery-policy:local | sudo ctr -n k8s.io images import -"
   kubectl rollout restart deployment/recovery-policy -n vllm-serving
   kubectl rollout status deployment/recovery-policy -n vllm-serving --timeout=180s
   ```
7. **롤아웃 후 실행 중인 파드 안을 다시 검증**하고 상태를 확인한다:
   ```
   POD=$(kubectl get pods -n vllm-serving -l app=recovery-policy -o jsonpath='{.items[0].metadata.name}')
   kubectl exec -i $POD -n vllm-serving -- sh -c 'cd /app && sha256sum -c -' < <out>/MANIFEST.sha256
   kubectl get pod $POD -n vllm-serving -o jsonpath='{.status.containerStatuses[0].imageID} restarts={.status.containerStatuses[0].restartCount}'
   ```
   `imageID`가 이전과 달라졌는지, `restarts`가 0인지, `/healthz`가 ok인지도 함께 본다.
