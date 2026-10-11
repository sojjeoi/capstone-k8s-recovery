# 관측성 스택

> Status: **HISTORICAL**(초기 설명). 현재 사용 지표·수집 방식은 `docs/final/FINAL-RESEARCH-SYNTHESIS-20261011.md` 참조.

Prometheus로 K8s 지표 + vLLM 고유 지표(`/metrics`)를 수집하고 Grafana로 시각화한다.

- `prometheus/vllm-scrape-config.yaml`: vLLM 서빙 파드의 `/metrics` 엔드포인트 스크랩 설정
- `prometheus/alert-rules.yaml`: 반응형 경로(baseline 비교용) 알림 규칙
- `grafana/dashboards/`: 대시보드 정의
