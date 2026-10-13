# Phase 2 시연 runbook - demo-only (2026-10-12)

> 실행기 `experiments/run_demo_phase2.py` - 동결 B/C/K 실행기(`run_guarded_bck.run_bck`)를 공식 Stage 2와 같은 방식으로 **수정 없이** 호출한다.
> 실행 ID `p2demo01`–`p2demo09`(1회용), 결과 `experiments/results/demo_only_phase2/`, `counted=false`. 공식 Phase 2 결과·실행기·prereg·freeze·Recovery Policy·Argo manifest·감지기 임계값은 바꾸지 않는다.
> ACTIVE/SHADOW = `experiments/results/phase2_selection/active-profile.json`(실행기 `PHASE2_ACTIVE_PROFILE`와 대시보드 `--active-profile`이 같은 파일). c1 IF 경로를 쓰지 않는다.
> ★ = 클러스터를 바꾸는 단계. 사용자가 직접 Enter를 누른다.

## 명령 (PowerShell, 저장소의 `demo\` 폴더)

준비: `$env:CAPSTONE_MAIN_REPO`(keep_awake.py·measure_clock_offset.py·c3 artifacts가 있는 저장소, 또는 `-MainRepo`), `$env:PYTHON_EXE`(없으면 `python`), `$env:GUARDED_ADMIN_TOKEN`(또는 `-TokenFile`). 토큰은 저장소에 두지 않는다. 대시보드는 Git에 포함하지 않은 로컬 시연 도구라서 `<MainRepo>\dashboard\server.py`가 없으면 건너뛴다.

| 언제 | 한 줄 | 하는 일 |
|---|---|---|
| 발표 4–6분 전 | `.\prepare-demo.ps1` | 읽기 전용 사전 확인 → ★ 정책 act → 시계 측정 → ★ 기존 정책 격리·demo 승인 → 대시보드(8765) → preflight-only → ★ 세션 시작 → "준비 완료"(새 Pod·요청 발생기·감지기 준비, 압박 없이 대기, 최대 15분) |
| 발표 중 | `.\start-pressure.ps1` | 시작 신호 파일 → 이후 자동: CPU 압박 → CPU 임계치 3회 연속 → 정책 → Argo 승격 → Service 전환 → (전환 확인 뒤) 이번 데모 StressChaos만 정리 → 요청 이동 검증 |
| 발표 뒤 | `.\cleanup-demo.ps1` | 세션 종료 대기(관찰 600초 + 정리) → ★ 정책 record_only → ★ 기존 정책 복원 → 최종 상태(읽기 전용) |

## 데모 전용 동작 2가지

1. **압박 대기**: 압박 직전(`inject`)에서 `demo-go-<run>.flag`를 기다린다. 대기 시간만큼 요청 발생기 수명을 늘린다. 15분 안에 신호가 없으면 압박 없이 공식 정리 경로로 끝난다.
2. **데모 압박 정리**(Recovery Policy 기능 아님): ① ACTIVE 신호의 정책 결과 `promoted_verified` ② Rollout activeSelector = new ③ vllm-active Service selector = new(앞뒤 두 번 읽어 같음) ④ old ≠ new ⑤ new Pod(UID 일치) Ready — 모두 확인된 뒤, 이름이 이번 실행 계획 목록에 있고 annotation `experiment-run-id-full`이 이번 run_id와 정확히 같은 StressChaos만 지운다. 기존 Pod·scaleDownDelay(420)는 건드리지 않는다. 결과는 `demo-stress-cleanup-<run_id>.json`에 트래픽 검증과 따로 남는다.

## 대시보드

- 검증 중: 정책이 측정하는 동안(`verified_service_pending`)만 apiserver pod proxy GET으로 old/new Pod `/metrics`를 읽어 "실시간 카운터 · 공식 판정 대기"로 보여준다(기준점 = 전환 관측 +10초). 공식 숫자·"검증 완료"는 정책 service_check 판정 뒤에만.
- Pod 표시: 기존 Pod / 새 Pod(정책 결과 target_key 기준, 결과 전에는 active/preview), hash는 작게, Pod 이름·Node는 hover.

## 실패 시

자동 재시도·수동 승격 금지. 대시보드 현재 상태와 `summary-*`의 `outcome_reason`을 보여 주고, 공식 Phase 2 Stage 2 결과(3/3)를 **과거 기록이라고 밝히고** 보여 준다.
