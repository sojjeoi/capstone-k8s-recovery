# 시연 뒤 한 줄: .\cleanup-demo.ps1  - 세션이 스스로 끝나기를 기다린 뒤(관찰 600초 + 정리) 시연 전 상태로 되돌린다.
# ★ 정책 record_only 복귀, 기존 recovery-policy 복원(클러스터 변경). 기존 Pod 삭제·scaleDownDelay 변경은 하지 않는다
# (실행기가 이미 420→30으로 되돌리고, 기존 Pod는 Argo가 지운다). 마지막에 읽기 전용으로 최종 상태를 보여준다.
param([string]$Run = '', [int]$WaitSec = 1500,
      [string]$PythonExe = $(if ($env:PYTHON_EXE) { $env:PYTHON_EXE } else { 'python' }))
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'   # 한글 경로(cp949 기본값)에서도 Python 입출력을 UTF-8로
$WT = (Resolve-Path "$PSScriptRoot\..").Path
$PY = $PythonExe
$DEMO = "$WT\experiments\results\demo_only_phase2"
if (-not $Run) {   # 격리 기록이 있는 가장 최근 시연
    $rec = Get-ChildItem $DEMO -Filter 'old-policy-isolation-p2demo*.json' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $rec) { Write-Host '되돌릴 시연 기록(old-policy-isolation-*.json)이 없습니다' -ForegroundColor Red; exit 1 }
    $Run = $rec.BaseName -replace '^old-policy-isolation-', ''
}
$RUNID = "guarded_pressure_act-$Run-capstone-phase2-demo-only-20261012"
Write-Host "정리 대상: $Run" -ForegroundColor Yellow

Write-Host "`n== 1/4 세션 종료 대기(실행기가 압박·요청 발생기 정리, scaleDownDelay 복원까지)" -ForegroundColor Cyan
$t0 = Get-Date
while ($true) {
    $sess = Get-ChildItem $DEMO -Directory -Filter "session-$Run-*" -ErrorAction SilentlyContinue | Sort-Object CreationTime -Descending | Select-Object -First 1
    $live = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "run_demo_phase2\.py.*--run $Run" -and $_.CommandLine -notmatch '--preflight-only' }
    if (-not $live -and $sess -and (Test-Path "$($sess.FullName)\session-end.json")) { break }
    if (-not $live -and -not $sess) { Write-Host '  실행 중인 세션 없음'; break }
    if (((Get-Date) - $t0).TotalSeconds -gt $WaitSec) { Write-Host "  $WaitSec 초 안에 세션이 끝나지 않음 - 정책 복귀를 멈춥니다(실행기 확인 필요)" -ForegroundColor Red; exit 1 }
    Write-Host -NoNewline '.'
    Start-Sleep 10
}
if ($sess -and (Test-Path "$($sess.FullName)\session-end.json")) {
    $end = Get-Content "$($sess.FullName)\session-end.json" -Raw -Encoding UTF8 | ConvertFrom-Json
    Write-Host "`n  세션 종료 코드: $($end.exit_code) (0 = 정상, 2 = 정리 오류 기록)"
}

Write-Host "`n== 2/4 ★ 정책 record_only 복귀" -ForegroundColor Cyan
kubectl set env deployment/guarded-recovery-policy -n vllm-serving GUARDED_MODE=record_only
if ($LASTEXITCODE -ne 0) { Write-Host '  정책 모드 변경 실패 - 기존 정책 복원을 하지 않고 멈춥니다(두 정책 동시 동작 방지)' -ForegroundColor Red; exit 1 }
kubectl rollout status deployment/guarded-recovery-policy -n vllm-serving --timeout=240s
if ($LASTEXITCODE -ne 0) { Write-Host '  정책 rollout 미완료 - 기존 정책 복원을 하지 않고 멈춥니다' -ForegroundColor Red; exit 1 }
# PS 5.1은 네이티브 인자 속 큰따옴표를 지우므로 JSONPath 필터 대신 JSON을 읽는다
$dep = kubectl get deployment guarded-recovery-policy -n vllm-serving -o json | Out-String | ConvertFrom-Json
$mode = ($dep.spec.template.spec.containers | ForEach-Object { $_.env } | Where-Object { $_.name -eq 'GUARDED_MODE' } | Select-Object -First 1).value
if ($mode -ne 'record_only') { Write-Host "  정책 모드가 record_only가 아님($mode) - 기존 정책 복원을 하지 않고 멈춥니다" -ForegroundColor Red; exit 1 }
Write-Host '  정책 record_only 확인'

Write-Host "`n== 3/4 ★ 기존 recovery-policy 복원" -ForegroundColor Cyan
& $PY "$WT\experiments\old_policy_isolation.py" restore --record "$DEMO\old-policy-isolation-$Run.json"
$restoreFailed = $LASTEXITCODE -ne 0
if ($restoreFailed) { Write-Host '  기존 recovery-policy 복원 실패 - 위 출력 확인(수동 확인 필요)' -ForegroundColor Red }

Write-Host "`n== 4/4 최종 상태(읽기 전용)" -ForegroundColor Cyan
kubectl get rollout vllm-serving -n vllm-serving -o jsonpath='{.status.phase} active={.status.blueGreen.activeSelector} preview={.status.blueGreen.previewSelector} delay={.spec.strategy.blueGreen.scaleDownDelaySeconds}'
Write-Host ""
kubectl get stresschaos -n vllm-serving
kubectl get pods -A | Select-String -Pattern 'ramp|probe|loadgen|burner|stress'
kubectl get nodes
$sr = "$DEMO\p2demo-session-$Run.json"
if (Test-Path $sr) {
    $r = Get-Content $sr -Raw -Encoding UTF8 | ConvertFrom-Json
    Write-Host "`n시연 결과(counted=false): 성공=$($r.result.success)  정리 오류=$($r.cleanup_errors.Count)"
    if ($r.demo_stress_cleanup) { Write-Host "데모 압박 정리(harness, 정책 기능 아님): $($r.demo_stress_cleanup.status) $($r.demo_stress_cleanup.error)" }
}
$cr = "$DEMO\demo-stress-cleanup-$RUNID.json"
if (-not (Test-Path $sr) -and (Test-Path $cr)) { Write-Host "데모 압박 정리 기록: $((Get-Content $cr -Raw -Encoding UTF8 | ConvertFrom-Json).status)" }
Write-Host "`n기대: 정책 record_only, 기존 recovery-policy replicas 1, Rollout Healthy(active = preview), delay 30, StressChaos·잔여 Pod 없음." -ForegroundColor Green
Write-Host "대시보드 창은 닫아도 됩니다." -ForegroundColor Green
if ($restoreFailed) { Write-Host '주의: 기존 recovery-policy 복원이 실패했습니다(위 3/4 출력).' -ForegroundColor Red; exit 1 }
