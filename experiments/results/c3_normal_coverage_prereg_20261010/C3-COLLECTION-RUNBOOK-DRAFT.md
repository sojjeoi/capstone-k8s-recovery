# c3 정상 수집 runbook 초안 (DRAFT, 2026-10-10)

> **이 문서는 실행 승인이 아니다.** 이번 작업에서 아무 명령도 실행하지 않았다.
> [USER APPROVAL REQUIRED] 표시 명령은 클러스터·workload 상태를 바꾸거나 수집을 시작한다 - 사용자 승인 후에만.
> 기준 문서: `C3-NORMAL-COVERAGE-PREREGISTRATION.md`(이하 PREREG).
> **구현(2026-10-10 오프라인 준비 완료, 실행 안 함)**: `experiments/c3_normal_coverage/`
> - `run_c3_collection.py` 수집 실행기(N1 실행기 `run_guarded_normal_bc.py`를 수정 없이 호출, 계열 전역만 호출 동안 c3 값으로 바꿈 - plan ID `capstone-c3-normal-coverage-20261010`, freeze `c3-collection-freeze-manifest.json`)
> - `c3-session-manifest.json` 역할·블록·순서(PREREG H 최소안), `c3_session_validity.py` 운영 유효성 판정, `train_calibrate_c3.py` 학습·보정·c1 재현 관문, `eval_c3_validation.py`·`pressure_c3.py` 동결 뒤 평가, `test_c3_normal_coverage.py`
> - 리뷰는 사용자 지시에 따라 Codex 대신 Ponytail targeted review(PREREG T-2의 "Codex 리뷰" 대체).

## 0. 준비 (PowerShell, 저장소 루트)

```powershell
$ROOT = 'D:\Desktop\졸업작품\capstone-k8s-recovery'
cd $ROOT
$PY  = 'C:\Users\qkrru\AppData\Local\Python\pythoncore-3.14-64\python.exe'
$C3  = "$ROOT\experiments\results\c3_normal_coverage_collection"   # 수집 결과 폴더(이 prereg 폴더와 분리)
$RUN = 'c3n01'   # PREREG H절 순서대로만, 한 번 쓴 ID 재사용 금지
$TOK = 'C:\Users\qkrru\AppData\Local\Temp\claude\D--Desktop------capstone-k8s-recovery\52d2cee3-8749-4f38-9e21-08a742a82634\scratchpad\guarded-admin-token.txt'   # 내용 출력 금지
```
전원: AC 연결, 덮개 열림, keep_awake 도우미 실행(과거 v3vs05·06 절전 무효 교훈).

## 1. 세션 전 읽기 전용 확인 (읽기만)

```powershell
kubectl get nodes
kubectl get rollout vllm-serving -n vllm-serving -o jsonpath='{.status.phase} active={.status.blueGreen.activeSelector} preview={.status.blueGreen.previewSelector} gen={.metadata.generation} delay={.spec.strategy.blueGreen.scaleDownDelaySeconds}'
kubectl get pods -n vllm-serving -o jsonpath='{range .items[*]}{.metadata.name} {.metadata.uid} {.spec.nodeName} {.status.startTime} restarts={.status.containerStatuses[0].restartCount}{"\n"}{end}'
kubectl get stresschaos,podchaos,networkchaos,iochaos -A
kubectl get deployment guarded-recovery-policy -n vllm-serving -o jsonpath='{.spec.template.spec.containers[0].env}'
& $PY experiments\old_policy_isolation.py status
```
통과: 노드 3 Ready, Rollout Healthy(active = preview 한 hash), delay 30, chaos 0, ramp·loadgen·burner Pod 0, guarded 정책 record_only, 기존 recovery-policy 상태 기록. 하나라도 어긋나면 시작하지 않는다.
블록 첫 세션이면: active Pod UID를 이전 블록 기록과 비교(PREREG H-5). 같으면 멈추고 사용자 결정.

## 2. 세션 실행 (세션마다 개별 승인)

```powershell
$C3R = "$ROOT\experiments\c3_normal_coverage\run_c3_collection.py"
& $PY $C3R --plan                                                     # 계획·진행만(클러스터 접속 없음)
& $PY $C3R --write-freeze-manifest                                    # [USER APPROVAL REQUIRED] 첫 세션 직전 1회 - 코드·문서 해시 동결
$env:GUARDED_ADMIN_TOKEN = (Get-Content $TOK -Raw).Trim()            # 토큰 출력 금지
# [USER APPROVAL REQUIRED] 사전검사(클러스터 읽기 접속)
powershell -NoProfile -File experiments\guarded_pilot_session.ps1 -Run $RUN -Mode record_only -RunnerScript $C3R -SessionRoot $C3 -PythonExe $PY -PreflightOnly
# [USER APPROVAL REQUIRED] 세션 실행 - preview 생성·probe 1 RPS·record_only 탐지기·관찰 4200 s·자동 정리
Start-Process powershell -WorkingDirectory $ROOT -ArgumentList '-NoProfile','-File',"$ROOT\experiments\guarded_pilot_session.ps1",'-Run',$RUN,'-Mode','record_only','-RunnerScript',$C3R,'-SessionRoot',$C3,'-PythonExe',$PY -WindowStyle Hidden
Remove-Item Env:GUARDED_ADMIN_TOKEN
```
- 예비 ID: 대상 세션 판정이 normal_valid가 아닐 때만 `& $PY $C3R --record-replacement c3r01 <대상ID>`(오프라인) 뒤 같은 방법으로 실행.
- 세션은 c1 수집과 같은 공식 5초 Pod identity 표본기(`samples-<run_id>.jsonl`)를 탐지기와 함께 자동 기록한다. 탐지기 입력이 아니라 학습 행 선택(공식 기대 Pod 판정)·무결성 증거용이며, 표본 누락은 세션 무효 기준이 아니다(해당 창만 c1처럼 행에서 제외).
- 실행 중: 점수·신호 집계를 보지 않는다(PREREG R, 검증 세션 blind). 안전 감시·진행 시각만 확인.
- 자동 재시도 없음. 실패·중단 시 보존하고 보고.

## 3. 세션 후 확인 (읽기 전용)

- PREREG E절 9개 조건 판정(점수 필드는 보지 않음): outcome_status, cleanup_errors, probe 공백·요청 수, slo_judge·slo_episodes, service_event, 기록 완전성, restart count 전후, Pod UID 불변, 전원 이벤트, c1 사용 가능 비율.
- PREREG Amendment M-1: CALIBRATION 세션은 유효성 판정에서 M-1 사건(빌더 제외 AND live scored AND interior)이 1개라도 있으면 `INVALID_CALIBRATION_ATTEMPT` → 같은 역할 예비 ID로 대체(`--record-replacement`). 가장자리 제외·live 건너뜀은 세지 않는다.
- 판정 기록(오프라인): `& $PY experiments\c3_normal_coverage\c3_session_validity.py --run $RUN --operator-intervention none` → `$C3\<run>-validity.json`(normal_valid / unexpected_degradation / invalid + 근거). 개입이 있었으면 none 대신 내용을 적는다. 다음 세션은 이 판정 파일이 있어야 시작된다.
- 1절 명령 재실행으로 잔여 Pod 0·Rollout 상태 확인.

## 4. 블록 사이

- [USER APPROVAL REQUIRED] active Pod 교체가 필요하면(시연·승인된 승격으로 이미 바뀌지 않은 경우) 사용자가 방법을 결정한다. 이 runbook은 교체 명령을 미리 넣지 않는다.
- 교체 뒤 1절 확인 + 새 active UID 기록.

## 5. 중단 규칙 (PREREG Q)

invalid 2회 연속 / unexpected_degradation 2회 / 정리 오류 1회 / 시도 상한 / 블록 경계 Pod 조건 불충족 → 멈추고 보고. 대체 세션은 예비 ID(c3r..)로 사용자 승인 후.

## 6. 수집 완료 뒤 (오프라인, 클러스터 접근 없음)

0. c1 재현 관문 재확인: `& $PY experiments\c3_normal_coverage\train_calibrate_c3.py --reproduce-c1 --out <새 c3_normal_coverage* 폴더>` → PASS여야 다음 단계.
1-2. [USER APPROVAL REQUIRED] `& $PY experiments\c3_normal_coverage\train_calibrate_c3.py --train` - coverage 관문(새 TRAIN: active UID ≥ 2, 날짜 ≥ 2) 통과 시 c3 학습·보정·동결(`anomaly-detection/v3/model_c3_normal_coverage/artifacts`, 새 폴더만).
4. `& $PY experiments\c3_normal_coverage\pressure_c3.py`(동결 c3 필요) → PREREG P.
3. `& $PY experiments\c3_normal_coverage\eval_c3_validation.py --pressure-result <압박 결과>`(동결 c3 필요) → PREREG N·O 판정.
5. 결과 문서.

## 7. 하지 않는 것

새 부하 수준, chaos·stress, 메모리 감소 유도, recovery·promotion(4절 사용자 결정 제외), 공식 c1 artifact·결과 수정, Git push.
