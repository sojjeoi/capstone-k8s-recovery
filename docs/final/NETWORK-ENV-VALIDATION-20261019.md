# 네트워크 지연 환경 추가 검증: 최종 결과 (2026-10-19)

> 이 문서는 Phase 1·Phase 2 공식 결과와 **분리된 별도 추가 검증**의 최종 결과다. Phase 1·Phase 2 결과, 태그, 동결 소스는 수정하지 않았다.
> 검증은 이 문서로 종료한다. 이후 작업은 결과 정리(Git 보존, 발표 자료)뿐이다.

## 1. 한 줄 결론

목표했던 것은 모두 달성했다. 후보 3개 중 `net-probe-readiness`(readiness probe 소요 시간)가 선택됐고(`SELECTED:net-probe-readiness`), 그 감지기가 새 Pod 로의 실제 복구를 **유효 3/3**으로 성공시켰다. 다만 이 결과를 "AI가 우수하다"거나 "CPU 임계값이 열등하다"는 증거로 쓰면 안 된다(§9 한계).

## 2. 무엇을 검증했나

- 기존 Phase 2 는 CPU 부하 장애였다. 이번에는 "네트워크가 느려지는" 다른 장애에서도 같은 구조(감지 → 승격 → 트래픽 전환)가 동작하는지 따로 확인했다.
- 후보 감지기 3개: CPU 임계값(`cpu-threshold-c`), 정상 CPU 데이터로 학습한 Isolation Forest(`c3-a-if`), `net-probe-readiness`(임계값 0.0064 s). 평가는 15초마다, 60초 창, 3회 연속 이상, 60초 쿨다운(Phase 2 와 같은 규칙).
- 환경: vLLM(Qwen2.5-0.5B) blue-green Rollout, 11초 `network_tolerant` probe 프로필(검증 동안만), 지연 주입은 NetworkChaos(0.5 → 1 → 2 → 4 s 단계).
- 성공 기준: Stage 1 `SELECTED:<detector>` + Stage 2 유효 세션 3개(새 Pod 요청 증가 30 이상, 이전 Pod 2 이하, 활성 endpoint 는 새 Pod 하나, SHADOW 송신 0, 정책 감사 기록은 ACTIVE 감지기만, Recovery Policy 해시 동일).

## 3. 결과 요약

| 단계 | 목표 | 결과 |
|---|---|---|
| Stage 1 정상 세션 | 유효 4 | **4 달성** (n2n03, n2n05, n2n06, n2n07). 시도 7번 중 3번 무효 (n2n01, n2n02, n2n04) |
| Stage 1 지연 주입 세션 | 유효 5 | **5 달성** (n2p02 ~ n2p06). 시도 6번 중 1번 무효 (n2p01) |
| Stage 1 선택 | `SELECTED:<id>` | **`SELECTED:net-probe-readiness`** |
| Stage 2 복구 검증 | 유효 3 | **3 달성** (n2s02, n2s03, n2s04). 시도 4번 중 n2s01 은 복구에 성공했으나 무효 |
| 시연 환경 원복 | 기본 시연 상태 | **기능적으로 완료** (§8) |

## 4. Stage 1: 감지기 선택

정상 4개와 지연 주입 5개의 유효 세션만 선택 입력으로 썼다(무효 세션은 어느 감지기의 성공·실패로도 세지 않았다).

- **무결성·Gate 1**: 문제 없음(`integrity_problems` 없음, 감지기별 Gate 1 문제 없음).
- **Gate 2**(지연 주입 후 363초 안에 3연속 이상, 5번 중 4번 이상 필요):

| 감지기 | 통과 | 주입 후 첫 3연속 이상까지(초, n2p02~06) |
|---|---|---|
| `net-probe-readiness` | **5/5** | 103.0, 97.8, 95.7, 97.5, 97.5 |
| `cpu-threshold-c` | 0/5 | 없음, 554.6(기준 초과), 533.0(기준 초과), 없음, 없음 |
| `c3-a-if` | 0/5 | 모두 없음 |

- **Gate 3**(정상 운전 불필요 신호): Gate 2 를 통과한 후보가 하나뿐이라 선택에는 쓰이지 않았다(`used: false`). 참고용 합계: `cpu-threshold-c` 31건(n2n05 11, n2n06 11, n2n07 9, n2n03 0), `net-probe-readiness` 0건, `c3-a-if` 0건.
- **타이밍·단순성 동점 규칙**: 쓰이지 않았다.
- 결과: `SELECTED:net-probe-readiness`. 근거 파일: `experiments/results/phase2_network_env_selection/phase2-network-selection-evidence.json`.

## 5. Stage 2: 실제 복구 검증

세션마다 별도의 사용자 ACT 승인(해당 세션 1회 한정)을 받았고, 선택된 ACTIVE 감지기는 `net-probe-readiness` 하나였다. CPU 와 c3-A IF 는 SHADOW 로 두었고 송신은 0건이었다.

| 세션 | 시도 | 복구 성공 | 유효성 | 승격·Service 전환 확인 시각(UTC) | 새 Pod 요청 증가 | 이전 Pod 요청 증가 |
|---|---|---|---|---|---|---|
| n2s01 | 1 | 성공 | **무효**(N3, 아래 설명) | — | 60~63 | 0 |
| n2s02 | 2 | 성공 | **유효** | 2026-10-18T14:58:11Z | 60~63 | 0 |
| n2s03 | 3 | 성공 | **유효** | 2026-10-18T15:23:53Z | 60~63 | 0 |
| n2s04 | 4 | 성공 | **유효** | 2026-10-18T16:03:33Z | 60~63 | 0 |

- 유효 세션 3개 모두: 성공 조건 7개(`signal_sent`, `context_set`, `first_send_after_injection`, `policy_audit_read`, `promoted_verified`, `traffic_verified`, `no_shadow_send`) 충족, `authority_leak` 없음, 정책 audit 상태 `verified`, 세션 정리 오류 0, 지연 전달 확인 문제 0건(승격 확인 시각까지 지연 유지, 새 Pod 로 유출 없음), 600초 관찰 완주.
- 요청 증가량은 세션 요약의 확인 구간 기록값의 범위다(기준: 새 Pod 30 이상, 이전 Pod 2 이하).
- 세션마다 종료 후 정책을 `record_only` 로 원복하고 유효성 기록과 `session-end-check` 를 마쳤다(모두 통과).
- 유효 3개가 채워져 규칙상 추가 시도(n2s05)는 열리지 않았고 실행하지 않았다. 시도는 4/5 를 썼다.
- **Recovery Policy 해시**는 Phase 2 와 동일하다(`b96fcdf0a8e40b014fcfa05ba9da9504ec3045d3865fdc46e0793bcecbd060ae`, 네트워크 active profile 과 Phase 2 active profile 비교). Phase 2 소스(`experiments/phase2_detector_selection`)와 `recovery-policy`, `gitops`, `chaos` 는 기준 커밋(`7d01c82^`) 대비 변경이 없다.

## 6. 계획과 달라진 점 (사후 규칙 변경, 모두 공개)

아래는 모두 사용자 승인을 받아 했고, 실험 결과 일부를 본 **뒤에** 이뤄졌다. 감지기 임계값, Gate 와 선택 규칙, Recovery Policy, 트래픽 검증 기준은 어느 개정에서도 바꾸지 않았다.

| 개정 | 내용 | 계기 |
|---|---|---|
| rev1 | 최초 사전등록·설계 동결(`dd3a74f`) | — |
| rev2 (`42a3b46`) | §9.1 PC 절전(호스트 중단) 뒤 재개 허용 | 첫 정상 시도 n2n01 이 PC 절전으로 중단 |
| rev3 (`2209451`) | §9.2 정상 시도 상한 6 → 12, §9.3 n2n03 재사용·무효 시도 전부 기록 | n2n02, n2n04 가 서비스 일시 정지로 중단되어 유효 4개 확보 불가 |
| rev4 (`34e7224`) | §9.4 실제 압박 주입 **이전**의 구현 결함 뒤 재개 허용(압박 시도에만) | n2p01 이 실행 ID 63자 한도 초과로 주입 전에 실패 |
| rev5 (`d83682d`) | §6.2.1 Stage 2 의 N3: 승격이 확인되면 이후 지연 단계 생략 가능, n2s02 부터 적용 | n2s01 이 복구에 성공했으나 "지연 4단계 모두 주입" 조건으로 무효 |
| 구현 동결 r8 | 독립 `session-end-check` 가 승인되어 보존 중인 n2n01·n2p01 의 running 표식을 정리 오류로 세지 않도록 수정 | 거짓 경보. 규칙·판정 기준은 불변 |
| 구현 동결 r9 | Stage 2 진입 검사가 rev4 해시로 만들어진 Stage 1 선택 결과를 받아들이도록, **rev4 해시 하나만** 명시 인정 | n2s02 가 세션 시작 전 진입 검사에서 거부됨(rev5 로 해시가 바뀐 뒤의 구현 불일치). rev4/rev5 문서를 절 단위로 비교하면 §3·§4·§5·§7(Stage 1 선택에 쓰인 부분)은 글자 그대로 같다 |

- 규칙이 사고 뒤에 추가된 것(rev2~rev5)은 각 사전등록 문서의 "예외를 밝힌다" 항목에도 적었다.
- 이 개정들의 설계 동결은 `docs/design/phase2-network-env-design-freeze*.json`, 구현 동결은 `experiments/phase2_network_env/phase2-network-env-implementation-freeze-r1~r9.json` 에 있다.

## 7. 무효 시도와 예외

### 7.1 무효 시도 (전부 기록, 선택 분석에서 제외)

| 시도 | 사유 | 처리 |
|---|---|---|
| n2n01 | PC 절전으로 61분째 중단(틱 간격 63.9초, 정리 오류 3건) | rev2 §9.1 로 재개 승인, 시도는 무효·소진 |
| n2n02 | 서비스 일시 정지(`unexpected_degradation`) | 무효·소진 |
| n2n04 | 증거 누락(서비스 일시 정지로 중단) | 무효·소진 |
| n2p01 | 실행 ID 67자가 NetworkChaos 라벨 한도 63자를 넘어 지연 주입 전에 실패(NetworkChaos 0회, 신호 0건) | rev4 §9.4 로 재개 승인, 시도는 무효·소진 |
| n2s01 | 복구는 성공했으나 지연이 1단계(0.5초)만 주입되어 "4단계 모두" 조건(N3)으로 무효 | **소급해서 유효로 바꾸지 않고** "무효 + 성공 기록"으로 보존, 유효 수에서 제외 |

### 7.2 n2s02 진입 거부 예외

- n2s02 의 첫 준비 시도는 세션 시작 **전에** Stage 2 진입 검사에서 거부됐다(`selection_rule_not_design_freeze`, 원인은 §6 의 r9). 세션은 시작되지 않았고 NetworkChaos 와 신호는 0건이다.
- 사전등록 §8 은 "진입 거부도 시도로 센다"고 적었다. 그러나 사용자 지시에 따라 이 거부는 Stage 2 시도에서 **제외**했다. 이는 사전등록 규칙의 **예외**다.
- 거부 기록은 삭제하지 않고 `experiments/results/phase2_network_env_stage2/refused-before-start-n2s02-20261018/` 로 옮겨 보존했다(`NOTE.json` 에 사유와 파일 해시). 이 거부 때 만든 act 승인·출처 기록은 구현 동결 r8 기준이라 폐기하고 r9 기준으로 새로 만들었다.

## 8. 시연 환경 원복과 `restore-check` 의 알려진 예외

- 원복: 기본 Rollout 재적용, 템플릿 주석 `experiment-prep-ts` 를 기준값으로 복원, 정책 Pod 경유 수동 승격(두 번)으로 마쳤다.
- 최종 상태: Rollout `Healthy`(active = preview = stable), vLLM Pod 1개, readiness·liveness `timeoutSeconds` 기본값(없음), 유예 30초, NetworkChaos·StressChaos 0개, 정책 `record_only`.
- `restore-check`: canonical 항목(템플릿 해시 포함)은 모두 통과했다. **남은 문제는 `running_marker_left` 한 가지**로, 보존 중인 `n2n01`, `n2p01` 의 running 표식 2개다.
- **알려진 예외**: 이 두 표식은 사람이 재개를 승인해 증거로 **의도적으로 보존**한 파일이다. 독립 `session-end-check` 는 r8 에서 이 표식을 제외하도록 고쳤지만, `restore-check` 에는 같은 제외 로직을 넣지 않았다. 추가 코드 수정과 새 구현 동결은 하지 않기로 했고, 기능적 시연 환경 복원은 완료된 것으로 기록한다.
- `demo\prepare-demo.ps1` 은 정책 act 전환·기존 정책 격리·시연 세션 시작을 하므로 읽기 전용 사전점검이 아니다. 실행하지 않았고 시연 직전에 직접 실행한다. 이 스크립트 1단계와 같은 읽기 전용 확인(노드 Ready, Rollout Healthy, active = preview, 유예 30, StressChaos 없음)은 정상으로 확인했다.
- 원시 probe CSV(`experiments/results/*.csv`)는 저장소의 `.gitignore` 규칙에 따라 커밋하지 않는다.

## 9. 한계와 말하지 않는 것

### 9.1 한계 (반드시 함께 쓸 것)
1. `net_probe` 는 주입한 지연을 거의 직접 재는 지표다. 이 환경에서 잘 잡히는 것은 당연한 면이 있어서, "다변량 학습이 CPU 단일 규칙보다 낫다"는 증거가 아니다.
2. IF 는 CPU 부하 상황의 정상 데이터로 학습했다. 네트워크 지연에서의 0/5 는 "이 학습 데이터와 이 환경에서"의 결과이며 IF 가 일반적으로 열등하다는 뜻이 아니다.
3. CPU 임계값은 Gate 3 참고 합계에서 정상 운전 불필요 신호가 많았고 지연 감지도 늦었다. 이것은 이 환경·이 보정에서의 결과이며 CPU 임계값이 일반적으로 열등하다는 뜻이 아니다.
4. 정상 세션 일부(n2n02, n2n04)에서 서비스가 일시 정지했고 원인은 확정하지 못했다. 요청 후 첫 응답이 1.2~1.5초로 느린 현상과 디스크 page-in 이 겹친 정황이 있으나 추정이다.
5. Stage 2 는 짧은 전환 직후의 트래픽 확인이다. 사용자 품질 개선을 입증하지 않으며 "승격 성공 = 복구 성공"이 아니다.
6. 표본이 작다(정상 4, 지연 주입 5, 복구 3). 통계적 증명이 아니다.
7. 선택은 이 환경·이 규칙·이 표본의 확인이며 단순성 순서는 성능 순위가 아니다.
8. rev2~rev5 와 r8·r9 는 사후 변경이다. 규칙을 미리 완전히 고정한 검증이 아니며, 각 변경은 승인되었고 위 표에 모두 밝혔다.

### 9.2 금지 표현
다음은 쓰지 않는다: "통계적으로 증명", "장애 예측", "AI가 장애를 판단", "IF가 CPU보다 우수/우월", "자동 rollback", "공식 비교 8쌍", "Kubernetes는 Pod가 살아 있으면 아무것도 하지 않는다", "promotion 성공 = 복구 성공". CPU 임계값이 일반적으로 우월하다는 주장도 하지 않는다. 서비스 저하의 원인 진단이나 미래 저하의 예측, 모든 LLM 환경에서의 자동 선택, 범용 운영 준비도 주장하지 않는다.

## 10. 증거 위치

| 내용 | 경로 |
|---|---|
| 사전등록(rev1~rev5) | `docs/design/PHASE2-NETWORK-ENV-PREREG-v1.md`, `...-v1-rev2.md` ~ `...-v1-rev5.md` |
| 설계·구현 동결 | `docs/design/phase2-network-env-design-freeze*.json`, `experiments/phase2_network_env/phase2-network-env-implementation-freeze-r1~r9.json` |
| 구현 검토 요약·시험 결과 | `experiments/phase2_network_env/IMPLEMENTATION-REVIEW-SUMMARY.json`, `IMPLEMENTATION-TEST-RESULTS.json` (r9: network 407 / Phase 2 107 통과, 새 regression 0) |
| Stage 1 세션 기록 | `experiments/results/phase2_network_env/` (세션·유효성·재개 승인·감지기 증거) |
| 선택 증거·active profile | `experiments/results/phase2_network_env_selection/` |
| Stage 2 세션 기록 | `experiments/results/phase2_network_env_stage2/` (n2s01~n2s04, 세션별 `ops-n2sNN/` 승인·시계·원복 기록) |
| n2s02 진입 거부 보존 | `experiments/results/phase2_network_env_stage2/refused-before-start-n2s02-20261018/` |
| 세션 실행 로그 | `experiments/results/capstone_if_threshold_candidate/sink-*-n2*.log` |

## 11. 남은 일

- 10/30 시연: 발표 자료에 §9 한계 문구를 반영하고, 시연 직전 `demo\prepare-demo.ps1` 을 직접 실행해 확인한다.
- Git push 는 하지 않았다(로컬 커밋만).
