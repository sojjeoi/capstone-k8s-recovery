# kubectl-argo-rollouts CLI 버전은 클러스터 컨트롤러 버전(v1.7.2)과 반드시
# 맞춰야 한다 - rollouts_client.py 모듈 docstring 참고(실측상 버전 안 맞으면
# promote 자체가 안 먹힘).
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl git && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /usr/local/bin/kubectl-argo-rollouts \
    https://github.com/argoproj/argo-rollouts/releases/download/v1.7.2/kubectl-argo-rollouts-linux-amd64 \
    && chmod +x /usr/local/bin/kubectl-argo-rollouts

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# Windows 호스트에서 COPY하면 실행비트가 안 살아남는 경우가 있어 명시적으로 부여
RUN chmod +x git_askpass.sh

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
