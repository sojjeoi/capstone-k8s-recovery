# chaos - 부하·압박 생성

- `probe-config.yaml`: 사용자 품질 측정용 probe(1 RPS, max_tokens=1)
- `loadgen/`: 부하 생성기(`ramp.py`, `v2_arrival.py`), CPU·I/O 압박 도구(`cpu_burner.py`, `io_writer.py`)
- `v2-workload-ladder.yaml`, `iof-stage0.yaml`, `nnf-stage0.yaml`: 실현 가능성 연구(v2 workload, 디스크 I/O, noisy-neighbor) 설정
- `scenario-*.yaml`: 초기 단계 Chaos Mesh 시나리오 정의(Pod kill·메모리 압박·네트워크 열화 등, historical)

공식 실험(A/B·B/C/K)의 CPU 압박은 `experiments/`의 실행기가 주입한다. 결과 요약: [../docs/final/FINAL-RESEARCH-SYNTHESIS-20261011.md](../docs/final/FINAL-RESEARCH-SYNTHESIS-20261011.md)
