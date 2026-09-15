# Phase 8 load_ramp 전용 runner 이미지. 기존 run_ramp_in_cluster.py는 매
# trial마다 bare python:3.11-slim pod에 "pip install aiohttp pyyaml"을
# 새로 실행했다 - PyPI 장애나 버전 드리프트가 그대로 invalid_run으로 샐 수
# 있어(계약서 §6), 의존성을 버전 고정해 이미지에 미리 구워 넣는다.
#
# registry 안 쓰는 로컬 데모라 recovery-policy와 동일하게 imagePullPolicy: Never
# 전제 - 이 이미지를 쓰는 워커 노드에서 직접 build해야 한다
# (gitops/apps/recovery-policy/deployment.yaml 주석 참고).
FROM python:3.11-slim

WORKDIR /
COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt

# ramp.py는 chaos/loadgen/ramp.py 원본을 build 직전에 이 디렉터리로 복사해
# 넣는다(단일 출처 유지, 이 디렉터리에 영구 보관 안 함 - .gitignore 참고).
COPY ramp.py /ramp.py
COPY probe.py /probe.py
