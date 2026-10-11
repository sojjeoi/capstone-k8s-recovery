# Stage 0 I/O feasibility 계약 v1 (counted=false, 2026-10-09)

> 상위: `phase8-aiops-next-research-direction-20261009.md`(사용자 승인 2026-10-09). 새 연구 family - c1·A/B·B/C/K·N1·감사(F1)·v2wl·request-shape·progress 탐색 결과는 그대로 보존하고 섞지 않는다.
> 이 단계는 AI 성능 검증이 아니다. IF2·R·C2 학습·보정·비교 없음. 모든 run은 counted=false이고 이후 학습·보정·검증에 재사용하지 않는다.

## 1. 질문

CPU가 기존 정상 범위에 머문 상태에서 통제된 노드 디스크 I/O 경합만으로 재현 가능한 서비스 저하(공식 SLO)를 만들 수 있으며, 동시에 SLO를 해치지 않는 약한 I/O 상태도 만들 수 있는가?

## 2. 확인된 환경(읽기 전용, 2026-10-09)

- 노드 디스크: 세 노드 모두 `vda`(sj-worker2 루트 ext4 166 GB, 여유 146 GB). sj-worker2 7일: 쓰기 p99 2.24 MB/s, 최대 6.0 MB/s, io_time 최대 1.0(포화 순간 있음), 쓰기 지연 중앙값 92 ms(유휴)–375 ms(4–6 MB/s). 클러스터 7일 최대 쓰기 17.8 MB/s(sj-worker).
- 지표(node-exporter, scrape 30 s): `node_disk_{written,read}_bytes_total`, `node_disk_io_time_seconds_total`(util), `node_disk_io_time_weighted_seconds_total`(평균 queue depth), `node_disk_{write_time_seconds,writes_completed}_total`(쓰기 지연), `node_pressure_io_{waiting,stalled}_seconds_total`(PSI), `node_cpu_seconds_total{mode="iowait"}`. 컨테이너 CPU는 cAdvisor(scrape 10 s), vLLM 지표 15 s.
- 주의: sj-worker2 node-exporter 재시작 93회(마지막 7일 전), 9일간 `up` 변화 2회 → §8 무결성 규칙으로 감시.
- nb02·bc03b forensic의 iowait·io PSI도 node-exporter의 같은 이름·정의(노드 단위).
- vLLM 모델 가중치는 hostPath `/home/ubuntu/hf-cache`(노드 디스크), stdout 로그는 컨테이너 런타임이 노드 디스크에 기록. Chaos Mesh에는 CPU·메모리 StressChaos와 FUSE 기반 IOChaos(대상 pod 변경 필요)만 있어 쓰지 않는다.

## 3. 배치(placement) - 실행 전 결정

- 이 research family의 canonical: **active vLLM = sj-worker2**, Prometheus = sj-worker(측정 평면 분리). family 동안 유지하고, 결과를 보고 바꾸지 않는다(Stage 0 뒤에도 원복하지 않음).
- 이동 1회, 검증된 경로 그대로: `blue_green_prep.prepare_preview_with_rollback`(preview 생성) → preview pod 노드가 sj-worker2인지 확인(아니면 미승격 preview 정리 뒤 중단·보고, 재시도 루프 없음) → guarded 정책 pod 안의 `kubectl-argo-rollouts promote`(A/B·B/C/K 모든 승격과 같은 바이너리 v1.7.2·서비스 계정) → canonical 확인. 이동 동안 기존 recovery-policy 격리. 이동은 실험 데이터가 아니다.
- 이동 전후 기록: active pod 이름·UID·노드·IP, selector, endpoints, `/health`(snapshot health_rc), Rollout Healthy·revision, preview 상태, scaleDownDelay 30, guarded 상태(record_only·context 없음·unresolved 0), 기존 recovery-policy 상태. 하나라도 어긋나면 Stage 0을 시작하지 않는다.

## 4. 세션 구조와 준비(warm-up) 규칙

배경 workload: probe 1 RPS(동결 SLO 측정기) + 배경 L1(0.025 RPS, `v2_arrival.py` jittered_grid_v1, max_tokens 10, 실행 ID별 사전 seed, `chaos/iof-stage0.yaml`).

순서: 사전검사 → probe 시작 → 동결 SLO 기준선 준비(probe 기반, 탐지기 무관) → 배경 pod·writer pod 준비 → 배경 시작 → **준비 조건** → 관찰 시작(obs0) → 종류별 구간 → 정리.

준비 조건(모델·규칙 판단과 독립): 배경 시작 120 s 뒤부터 15 s마다 확인, probe 시작부터 600 s 안에 만족하지 못하면 세션 invalid.
1. Rollout canonical(세션 pod 잔여 항목만 제외), active pod UID = 사전검사 UID(sj-worker2)
2. 최근 120 s Prometheus 원본 표본 수: 컨테이너 CPU ≥ 8, vLLM 생성 token ≥ 6, sj-worker2 io PSI ≥ 3, 쓰기 bytes ≥ 3
3. probe 생존(안전 감시), 배경 가동 ≥ 120 s

IF·R·C2 조건을 기다리는 규칙은 없다. 준비 확인 기록은 진단으로 summary에 남긴다.

## 5. 세션 종류·ID·스트레스

| 종류 | ID | 구간(obs0 뒤) | 용도 |
|---|---|---|---|
| 기준선 | iob90(필요 시 iob91) | 1200 s, 스트레스 없음 | CPU envelope, I/O·SLO 기준 |
| 개발 | iod90–iod92(유효 2, 최대 3) | 사전 300 s → I1 300 s → 회복 120 s → I2 → 회복 → I3 → 회복 | 강도 확인 |
| 확증 | iof90–iof92(유효 2, 최대 3) | 사전 300 s → weak 300 s → 240 s → strong 300 s → 240 s | feasibility 판정 |

- 순서 강제(iob90 → iod90… → iof90…), ID 재사용 금지, seed는 config에 고정.
- 스트레스 도구: `chaos/loadgen/io_writer.py`를 active 노드(sj-worker2)에 `nodeName`으로 고정한 별도 pod(이미지 loadgen-runner:phase8-v3-boundaries, vLLM 컨테이너 밖, CPU limit 250m·메모리 128Mi, emptyDir sizeLimit 2Gi)에서 실행. O_DIRECT 순차 쓰기, 블록 1 MiB, 쓰기 1개, 1 GiB 파일 순환 덮어쓰기(끝나면 삭제). pod 안 파일 해시 대조, pod 노드 확인. 시작·끝 시각, 목표·달성 속도, 초 단위 쓰기 로그 보존.
- 강도 축 하나 = 목표 쓰기 속도: **I1 2 MB/s**(sj-worker2 7일 p99), **I2 6 MB/s**(sj-worker2 7일 최대), **I3 18 MB/s**(클러스터 7일 최대). 모두 이미 관측된 범위 안. 더 큰 값·다른 축은 넣지 않는다.
- 개발 사다리는 올라가는 순서이고, 단계의 공식 SLO가 위반이면 더 높은 단계는 시작하지 않는다(SLO 경계 종료, invalid 아님). 스트레스 중 SLO 위반으로 즉시 멈추지 않는다.

## 6. 판정 정의 (결과 보기 전 고정)

- **공식 SLO**: 동결 slo_episodes r2·slo_judge v3. 단계 [시작, 끝]과 겹치는 위반 구간이 있으면 위반(v2wl `stage_slo_verdict`, a0 = obs0). 판정 불가면 세션 invalid.
- **CPU feasibility guard**(탐지기 임계값 아님): 유효 기준선 세션의 관찰 구간 60 s 창(15 s 간격) 컨테이너 CPU [min, max] = envelope. 스트레스 단계에서 창 끝이 [단계 시작 + 15 s, t_slo(위반 시) 또는 단계 끝]인 60 s 창이 모두 envelope 안이면 통과(양쪽). 기존 C(1.684…)는 쓰지 않는다.
- **I/O 변화**: sj-worker2 io PSI waiting rate(120 s 창, 30 s 간격)의 단계 중앙값 > 같은 세션 사전 구간 최댓값. 다른 I/O 지표(쓰기·읽기 MB/s, util, 평균 queue depth, 쓰기 지연, iowait, PSI stalled)는 기술값.
- **writer 정상**: 종료 코드 0, 실행 ≥ 95 % × 300 s.
- **측정 무결성**: 구간 안 지표별 최대 원본 표본 간격 ≤ 2.5 × 명목 scrape(CPU 25 s, vLLM 37.5 s, 노드 75 s), target UID 불변, Prometheus가 target 노드에 없음, 사전검사 시계 오차 ≤ 0.5 s, probe 판정 가능.
- **weak 상태**: writer 정상 + I/O 변화 + SLO 정상 + CPU guard 통과 + 무결성.
- **strong 상태**: writer 정상 + SLO 위반 + CPU guard 통과 + 무결성 + target pod 생존(재시작·OOM·종료 없음 - 안전 감시가 보장하며, 발생하면 세션 aborted).

## 7. 개발 → 확증 (분리)

강도를 고르는 run과 2/2 재현을 주장하는 run은 다르다.
- 선택 규칙(기계): weak = 모든 유효 개발 rep에서 weak 상태인 가장 높은 강도, strong = 모든 유효 개발 rep에서 strong 상태인 가장 낮은 강도이면서 weak보다 높은 것. 유효 개발 rep 2개 필요.
- 둘 중 하나라도 없으면 확증을 실행하지 않고 **feasible=false**. 강도를 더 만들거나 바꾸지 않는다.
- 고른 값은 `iof-stage0.yaml`의 `io.confirmatory`에 **amendment**(freeze manifest v1.1, 버전 `iof-stage0-v1.1`, 코드 동일)로 고정한 뒤 확증 실행. v1 manifest는 이력으로 보존.

## 8. 유효성·안전·중단

- 세션 invalid: outcome ≠ completed(SLO 경계 종료는 completed), 정리 오류, 준비 상한 초과, metric 저장 실패, 무결성 실패, target UID 변경, Prometheus가 target 노드, writer 비정상, 사전 구간부터 SLO 위반(환경 문제), probe 판정 불가.
- 즉시 중단(안전 감시 5 s 간격): probe 비정상, active pod 1개 아님·OOM·재시작, 노드 이상(NotReady·pressure), 기존 recovery-policy 격리 풀림, 단계 시작 전 sj-worker2 루트 여유 < 20 GB. 중단 시 원자료 보존 → writer 정지·pod 삭제 → 배경·probe 정리 → canonical 확인 → 이후 자동 실행 금지.
- 연속 invalid 2회 또는 정리 실패 → 일시 중지·보고. 외부 전원 Offline이면 시작하지 않는다.

## 9. feasibility 판정 (확증)

처음 두 유효 확증 rep에서 모두:
1. strong에서 공식 SLO 위반 (2/2)
2. strong의 CPU guard 통과 (2/2)
3. weak에서 I/O 변화 + SLO 정상 + CPU guard 통과 (2/2)
4. 측정 무결성(세션·단계 모두)

넷 다 참이면 feasible=true, 하나라도 거짓이면 feasible=false. 개발 결과를 보고 조건을 완화하지 않는다.

## 10. 향후(이번에 구현 안 함)

feasible=true일 때만 별도 계약: 주비교 IF2 vs R(단일 I/O pressure 임계값), C2는 보조, 같은 정상 오탐 예산. 관계 학습 확인용 column-shuffled IF control. 이번 Stage 0 자료는 쓰지 않는다.

## 11. 코드·freeze

| 파일 | 역할 |
|---|---|
| `chaos/iof-stage0.yaml` | 배경·seed·강도 사다리·시간 |
| `chaos/loadgen/io_writer.py` | 쓰기 부하 |
| `experiments/run_iof_stage0.py` | 실행기(사전검사·배치 확인·준비 조건·단계·정리·원본 표본 저장) |
| `experiments/iof_stage0_analysis.py` | 판정(SLO·CPU guard·I/O 변화·무결성·선택·확증) |
| `experiments/test_iof_stage0.py` | 테스트 |

freeze manifest `phase8-iof-stage0-freeze-manifest-v1-20261009.json`(버전 `iof-stage0-v1`). 실행기는 시작 전 재해시하고 frozen이 아니면 시작하지 않는다.
