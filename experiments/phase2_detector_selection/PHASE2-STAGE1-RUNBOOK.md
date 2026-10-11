# Phase 2 Stage 1 runbook (Selection Validation)

> **이 문서는 실행 승인이 아니다.** `[USER APPROVAL REQUIRED]` 명령은 클러스터 상태를 바꾸거나 workload를 만든다. 세션마다 사용자 승인 뒤에만 실행한다.
> 기준: 설계 동결 `docs/design/PHASE2-DETECTOR-SELECTION-PREREG-v1-rev6.md` rev 6(design freeze 교체 `phase2-detector-selection-design-freeze-rev6.json` - rev 5를 supersede), 구현 동결 `phase2-detector-selection-implementation-freeze-r2.json`(f81c876 동결을 supersede - p2n01 하네스 실패 뒤 재동결).
> 결과를 본 뒤 임계값·artifact·규칙·세션 수를 바꾸지 않는다(prereg §17). 문서·코드 결함은 새 revision + 교체 manifest로만(§18).

## 0. 어디서 실행하나

- `followup-detector-selection` worktree(implementation freeze의 `git_commit`)에서만 실행한다. 메인 작업 폴더(미커밋 변경 있음)에서는 실행하지 않는다 - 실행기가 시작 전에 freeze 해시를 다시 확인하므로 다르면 거부된다.
- c3-A `model.pkl`·`scaler.pkl`은 저장소에 없다. 외부 artifact 폴더를 **명시적으로** 준다(기본 경로·자동 탐색 없음).

```powershell
$WT  = 'C:\wt\followup-detector-selection'
cd $WT
$PY  = 'C:\Users\qkrru\AppData\Local\Python\pythoncore-3.14-64\python.exe'
$P2  = "$WT\experiments\phase2_detector_selection"
$S1  = "$WT\experiments\results\phase2_stage1"
$env:PHASE2_ARTIFACT_ROOT = '<c3-A 원본 artifact 폴더>'   # model.pkl·scaler.pkl·feature-schema.json·threshold.json·freeze.json
$TOK = '<관리자 토큰 파일>'                                  # 내용 출력 금지
```

## 1. 오프라인 확인 (클러스터 접속 없음)

```powershell
& $PY -m pytest experiments\phase2_detector_selection -q -p no:cacheprovider   # 실제 artifact 해시 확인 테스트 포함
& $PY $P2\run_stage1.py --plan
```
`test_real_external_artifacts_match_frozen_hashes`가 **통과**(skip 아님)해야 한다. 실패하면 PROFILE_INVALID - 시작하지 않는다.

## 2. Stage 1 격리 (prereg §8.1, 기존 방법 그대로)

1. 기존 recovery-policy 격리 `[USER APPROVAL REQUIRED]`(사용자가 실행): `& $PY experiments\old_policy_isolation.py isolate --record <파일>`
2. 확인(읽기): `& $PY experiments\old_policy_isolation.py status` → isolated
3. guarded-recovery-policy `GUARDED_MODE=record_only` 확인(읽기): c3 runbook §1 명령과 같음
4. c3 수집 runbook(`experiments/results/c3_normal_coverage_prereg_20261010/C3-COLLECTION-RUNBOOK-DRAFT.md`) §1 읽기 전용 확인 - 노드 Ready, Rollout Healthy, delay 30, chaos 0, 잔여 부하 Pod 0

실행기 자체도 세션마다 다시 확인한다: selection profile(ACTIVE 0)·외부 artifact·rule spec·PROFILE_STALE(Phase 2 preflight) → guarded record_only·기존 정책 격리·canonical 시작(기존 preflight). 하나라도 실패하면 클러스터를 바꾸기 전에 멈춘다.

## 3. 세션 (정상 p2n01-, 압박 p2p01-, 각각 사용자 승인)

```powershell
$env:GUARDED_ADMIN_TOKEN = (Get-Content $TOK -Raw).Trim()
# [USER APPROVAL REQUIRED] 사전검사만(클러스터 읽기)
powershell -NoProfile -File experiments\guarded_pilot_session.ps1 -Run p2n01 -Mode record_only -RunnerScript $P2\run_stage1.py -SessionRoot $S1 -PythonExe $PY -PreflightOnly
# [USER APPROVAL REQUIRED] 세션 실행(정상: probe 1 RPS·관찰 4200 s / 압박: A/B A arm 조건·관찰 600 s) - record_only, 송신 없음
powershell -NoProfile -File experiments\guarded_pilot_session.ps1 -Run p2n01 -Mode record_only -RunnerScript $P2\run_stage1.py -SessionRoot $S1 -PythonExe $PY
Remove-Item Env:GUARDED_ADMIN_TOKEN
```
- `-Mode act`는 실행기 인자 단계에서 거부된다(종료 코드 2).
- 세션 중 점수·신호를 보지 않는다(안전 감시·진행 시각만).
- 세션 뒤 실행기가 무결성을 확인한다(송신 0·SHADOW 외 역할 0·정책 조치/context 0·active selector 불변·profile 해시 불변). 어긋나면 `p2-integrity-failure-<run>.json` → `INTEGRITY_FAILURE`, 이후 세션은 거부된다.

## 4. 세션 뒤 유효성 확정 (오프라인, 다음 세션 전에 필수)

```powershell
& $PY $P2\run_stage1.py --record-validity p2n01 --operator-intervention none
```
- 정상: c3 정상 정의(N1 analyze_session + c3 추가 조건). 압박: A/B 공식 run_validity(A arm 경로). 탐지기 결과는 쓰지 않는다.
- 유효성 파일은 새 파일로만 쓰인다(덮어쓰기 불가). 재생(선택 분석)은 유효성 기록이 있는 세션만 읽는다.

## 5. 중단 규칙 (실행기가 강제, prereg §9)

- **runtime 폴더**: 실행기가 세션마다 Phase 2 preflight 직후(클러스터 변경 전) 재사용하는 동결 함수의 출력 폴더(sink 로그·probe CSV·Stage 1 결과)를 만든다(폴더 생성만, 기존 파일 불변). 동결 입력(probe 설정·sink 서버·실행기)이 없거나 작업 폴더가 저장소와 다른 드라이브면 시작하지 않는다. 사람이 미리 폴더를 만들 필요 없음.
- **정리·하네스 오류 뒤 재개(시도 한정)**: 정리 오류가 난 시도가 있으면 다음 세션은 막힌다. running 표시·요약·원자료는 지우거나 고치지 않는다. 사람이 확인한 뒤에만(오프라인):
  ```powershell
  & $PY $P2\run_stage1.py --authorize-cleanup-resume p2n01 --authorized-by <이름> --review-note "<확인 내용>" --artifact-root $env:PHASE2_ARTIFACT_ROOT
  ```
  기록(`p2-resume-authorization-<run>.json`, 새 파일로만)은 다음을 모두 만족할 때만 써진다: 그 시도가 무효로 끝남, 정리 오류가 하네스·미완료 정리 계열뿐이고 승인된 하네스 예외(`FileNotFoundError` - 잔여 probe Pod 확인이 실제로 돈 경우)가 있음, probe·injector 정리 실패 없음, 탐지기 시작·평가 기록 없음, 송신·조치·승격 0, 정책 context 없음·record_only, 무결성 통과·정책 감사·스냅샷 확인됨, canonical 끝·복원 확인, 기존 정책 격리(시작·세션 중·끝) 확인, 그 시도와 현재의 implementation freeze·profile·artifact 일치. 기록은 그 시도에만 적용되고 시도 기록·요약·유효성·running 표시·freeze가 바뀌면 무효다. 원 시도는 무효·시도 1회로 남는다(시도 수 되돌림 없음, ID 재사용 금지 - 다음은 새 ID).
- 재개 뒤에도 세션마다 전체 preflight(§1·§2·실행기 사전검사)를 다시 한다.
- 재개 기록은 기록 당시 implementation freeze(manifest 해시)에 묶인다. 이후 다시 재동결하면 그 기록은 효력을 잃고(덮어쓰기 불가) Stage 1은 그 시도에서 다시 막힌다 - fail-closed. 재동결이 필요해지면 먼저 사용자와 처리 방법을 정한다.
- 재개 기록이 효력을 잃거나 freeze 확인이 실패하면 정리 오류 중단 규칙이 다시 걸려, 선택 분석 기준으로는 "수집 끝(중단)"으로 보인다. 이것을 "목표 세션 수 충족"으로 읽지 않는다(선택 근거의 `collection_state.stop_reason` 확인).

invalid 2회 연속 · 예상 밖 저하 2회 · 정리 오류 1회 · PROFILE_STALE/INVALID(거부도 시도로 셈, 재개는 사용자 승인 뒤 `--resume-after-refusal <run>`, profile·artifact·freeze 변경 없이) · INTEGRITY_FAILURE · 시도 상한(정상 6·압박 7) · 유효 목표(정상 4·압박 5)를 채운 뒤 추가 수집 없음.

## 6. 선택 (오프라인)

```powershell
& $PY $P2\selection.py --results-dir $S1 --artifact-root $env:PHASE2_ARTIFACT_ROOT --out-dir "$WT\experiments\results\phase2_selection"
```
결과 상태: `INTEGRITY_FAILURE` / `SELECTION_INCOMPLETE` / `NO_ELIGIBLE_DETECTOR` / `SELECTED:cpu-threshold-c` / `SELECTED:c3-a-if`. `SELECTED`일 때만 `active-profile.json`(selection profile에서 role·selection만 바뀐 기계적 파생)과 provenance가 생긴다. 강제 선택 없음.

## 7. Stage 2

`SELECTED`가 아니면 금지. 진입 검사는 `run_stage2.py`가 한다(별도 승인, 이 문서 범위 밖).

## 알려진 한계 (기록)

- 정상 세션은 동결 N1 실행기를 그대로 쓰므로 그 계약 파일의 `detector`·`common_evaluation` 문구는 N1 값이다. 실제 실행기는 세션 기록·evidence 시작 줄(`phase2_same_tick_v1`)로 확인한다.
- vLLM 이미지는 노드 로컬 `vllm-cpu-env:latest`(레지스트리 없음)라 profile은 이미지 참조·pull 정책만 고정한다(내용 digest는 Rollout 스펙에서 읽을 수 없음).
- 압박 세션(prereg rev 6 §9): 별도의 저하 등급·예상 저하 범위·피해 상한·저하 횟수가 없다. 안전 중단은 그대로 적용되고, 중단·미완료 세션은 A/B 공식 run_validity(V1 등)로 `invalid`가 되어 "invalid 2회 연속" 규칙에 들어간다. 실제 latency·SLO는 원자료·보고에 그대로 남긴다. "예상 밖 저하 2회"는 정상 세션의 c3 분류(`unexpected_degradation`)만 센다.
- 외부 artifact의 JSON 파일(feature-schema·threshold·freeze)은 원본 바이트로 해시한다(동결 SHA256SUMS가 원본 바이트 기준). 줄바꿈이 바뀐 복사본은 해시 불일치로 거부된다(fail-closed) - 원본 artifact 폴더를 그대로 지정한다.
- 선택 분석(`selection.py`)은 Stage 1 수집이 끝났을 때만(두 종류 모두 목표 충족·시도 상한 또는 중단 규칙) 실행된다.
