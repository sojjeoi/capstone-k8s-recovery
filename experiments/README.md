# 실험

3-way(self-healing / 고정임계치 / 제안방식) × 3종 시나리오 × 10회+ 반복 실험 자동화 및 결과 수집.

- `run_all_scenarios.py`: 전체 실험 반복 실행
- `run_calibration.py`: 무개입 대조군 실행
- `collect_metrics.py`: 종단간 지연시간 분해, 가용성, 정확성 등 전체 평가지표 추출
- `results/`: 실험 결과 (CSV, `.gitignore`에 의해 커밋되지 않음 — 별도 백업 필요)

## 테스트

기본 실행은 클러스터·서비스 없이 항상 전부 통과한다:

```bash
python -m pytest
```

`recovery-policy` 같은 실제 서비스가 떠 있어야 통과하는 테스트는
`@pytest.mark.live_cluster`로 표시돼 있고, `conftest.py`가 기본적으로
건너뛴다. 실행하려면 먼저 포트포워드를 열고 `RUN_LIVE_TESTS=1`로 명시적으로
켠다:

```bash
kubectl port-forward -n vllm-serving svc/recovery-policy 8080:8080
```

```bash
RUN_LIVE_TESTS=1 python -m pytest test_run_once.py -v
```

PowerShell에서는:

```powershell
$env:RUN_LIVE_TESTS=1; python -m pytest test_run_once.py -v
```
