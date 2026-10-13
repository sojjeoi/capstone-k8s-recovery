# Phase 2 시연 준비(demo-only, counted=false) - 발표 4~6분 전에 한 줄: .\prepare-demo.ps1
# ★ 클러스터를 바꾸는 단계가 있다(정책 act 전환, 기존 recovery-policy 격리, 세션 시작). 사용자가 직접 실행한다.
# 끝나면 "준비 완료"가 뜨고, 실행기는 새 Pod·요청 발생기·감지기를 준비한 뒤 압박 직전에서 기다린다(최대 15분).
# 공식 Phase 2 결과·실행기·Recovery Policy·Argo manifest·감지기 임계값은 바꾸지 않는다.
param(
    [string]$Run = '',                    # 비우면 아직 안 쓴 p2demo01..09 중 첫 번째
    [int]$DashboardPort = 8765,
    [int]$ReadyWaitSec = 720,
    [string]$MainRepo = $env:CAPSTONE_MAIN_REPO,   # keep_awake.py·measure_clock_offset.py·c3 artifacts가 있는 저장소
    [string]$PythonExe = $(if ($env:PYTHON_EXE) { $env:PYTHON_EXE } else { 'python' }),
    [string]$TokenFile = ''               # GUARDED_ADMIN_TOKEN 환경변수가 없을 때만 읽는다
)
$ErrorActionPreference = 'Stop'
$WT = (Resolve-Path "$PSScriptRoot\..").Path
$MAIN = $MainRepo
$PY = $PythonExe
$TOKFILE = $TokenFile
if (-not $MAIN) { Write-Host '중단: -MainRepo 또는 CAPSTONE_MAIN_REPO 환경변수가 필요합니다' -ForegroundColor Red; exit 1 }
$DEMO = "$WT\experiments\results\demo_only_phase2"
$AP = "$WT\experiments\results\phase2_selection\active-profile.json"
$EVID = "$WT\experiments\results\phase2_selection\phase2-selection-evidence.json"
$ART = "$MAIN\anomaly-detection\v3\model_c3_normal_coverage\artifacts"
$KA = "$MAIN\experiments\keep_awake.py"   # worktree에는 없음(메인 저장소에만 있음)
function Step([string]$t) { Write-Host ""; Write-Host "== $t" -ForegroundColor Cyan }
function Stop-Demo([string]$why) { Write-Host ""; Write-Host "중단: $why" -ForegroundColor Red; Write-Host "(이미 바꾼 것이 있으면 .\cleanup-demo.ps1 로 되돌립니다.)"; exit 1 }

New-Item -ItemType Directory -Force $DEMO | Out-Null
Set-Location $WT
if (-not $Run) {
    $Run = (1..9 | ForEach-Object { 'p2demo{0:D2}' -f $_ } | Where-Object {
        $r = $_
        -not (Test-Path "$DEMO\p2demo-session-$r.json") -and -not (Test-Path "$DEMO\$r-act-approval.json") -and -not (Get-ChildItem $DEMO -Filter "demo-tag-*-$r-*.json" -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -notlike '*-preflight-only.json' })
    } | Select-Object -First 1)
    if (-not $Run) { Stop-Demo 'p2demo01..09를 모두 썼습니다' }
}
$RUNID = "guarded_pressure_act-$Run-capstone-phase2-demo-only-20261012"
Write-Host "시연 실행 ID: $Run  (결과: $DEMO, counted=false)" -ForegroundColor Yellow

Step '1/8 읽기 전용 사전 확인'
$nodes = kubectl get nodes --no-headers
if ($LASTEXITCODE -ne 0) { Stop-Demo 'kubectl 연결 실패(VPN·kubeconfig 확인)' }
$nodes | ForEach-Object { Write-Host "  $_" }
if ($nodes | Where-Object { $_ -notmatch '\sReady\s' }) { Stop-Demo 'Ready가 아닌 노드가 있습니다' }
$ro = kubectl get rollout vllm-serving -n vllm-serving -o jsonpath='{.status.phase}|{.status.blueGreen.activeSelector}|{.status.blueGreen.previewSelector}|{.spec.strategy.blueGreen.scaleDownDelaySeconds}'
$phase, $act, $prev, $delay = $ro -split '\|'
Write-Host "  Rollout $phase active=$act preview=$prev scaleDownDelay=$delay"
if ($phase -ne 'Healthy' -or $act -ne $prev -or $delay -ne '30') { Stop-Demo 'Rollout이 시작 상태(Healthy, active=preview, delay 30)가 아닙니다' }
$ErrorActionPreference = 'Continue'   # PS 5.1: 2>$null + Stop이면 'No resources found'(stderr)가 종료 오류가 된다
$chaos = kubectl get stresschaos -n vllm-serving -o name 2>$null
$ErrorActionPreference = 'Stop'
if ($chaos) { $chaos | ForEach-Object { Write-Host "  $_" }; Stop-Demo '남아 있는 StressChaos가 있습니다' }
foreach ($f in @($AP, $EVID, "$ART\model.pkl", $KA)) { if (-not (Test-Path $f)) { Stop-Demo "파일 없음: $f" } }
& $PY "$WT\experiments\old_policy_isolation.py" status | Out-Null   # 정보용(격리 전이라 종료 코드 1이 정상)

Step '2/8 관리자 토큰·Phase 2 경로'
if (-not $env:GUARDED_ADMIN_TOKEN) {
    if (-not $TOKFILE -or -not (Test-Path $TOKFILE)) { Stop-Demo 'GUARDED_ADMIN_TOKEN 없음(환경변수 또는 -TokenFile)' }
    $env:GUARDED_ADMIN_TOKEN = (Get-Content $TOKFILE -Raw).Trim()
}
$env:PHASE2_ACTIVE_PROFILE = $AP
$env:PHASE2_SELECTION_EVIDENCE = $EVID
$env:PHASE2_ARTIFACT_ROOT = $ART
Write-Host "  Active Profile: $AP"

Step '3/8 ★ 정책 act 전환'
kubectl set env deployment/guarded-recovery-policy -n vllm-serving GUARDED_MODE=act
if ($LASTEXITCODE -ne 0) { Stop-Demo '정책 모드 변경 실패' }
kubectl rollout status deployment/guarded-recovery-policy -n vllm-serving --timeout=240s
if ($LASTEXITCODE -ne 0) { Stop-Demo '정책 rollout 실패' }

Step '4/8 정책 확인·시계 오차 측정(약 25초)'
$pf = Start-Process kubectl -ArgumentList 'port-forward', '-n', 'vllm-serving', 'svc/guarded-recovery-policy', '18090:8080' -PassThru -WindowStyle Hidden
try {
    Start-Sleep 5
    $id = Invoke-RestMethod http://127.0.0.1:18090/identity
    Write-Host "  mode=$($id.mode) actions_enabled=$($id.actions_enabled)"
    if ($id.mode -ne 'act') { Stop-Demo '정책이 act가 아닙니다' }
    & $PY "$MAIN\experiments\measure_clock_offset.py" --base-url http://127.0.0.1:18090 --path-note "PC->port-forward->guarded Pod (phase2 demo)" --out "$DEMO\clock-offset-$Run.json"
    if ($LASTEXITCODE -ne 0) { Stop-Demo '시계 측정 실패' }
} finally { Stop-Process -Id $pf.Id -ErrorAction SilentlyContinue }

Step '5/8 ★ 기존 recovery-policy 격리 + demo 승인 기록'
& $PY "$WT\experiments\old_policy_isolation.py" isolate --record "$DEMO\old-policy-isolation-$Run.json"
if ($LASTEXITCODE -ne 0) { Stop-Demo '기존 정책 격리 실패' }
& $PY "$WT\experiments\demo_only_approval.py" --run $Run --clock "$DEMO\clock-offset-$Run.json" --out "$DEMO\$Run-act-approval.json" --approved-by "$env:USERNAME, $(Get-Date -Format 'yyyy-MM-dd HH:mm') Phase 2 시연 1회(demo-only)"
if ($LASTEXITCODE -ne 0) { Stop-Demo '승인 기록 생성 실패(시계 오차 0.5초 초과 등)' }

Step '6/8 대시보드(LIVE, Active Profile = 실행기와 같은 파일)'
$busy = Get-NetTCPConnection -LocalPort $DashboardPort -State Listen -ErrorAction SilentlyContinue
if (-not (Test-Path "$MAIN\dashboard\server.py")) { Write-Host '  대시보드 없음 - 건너뜀(시연 결과에는 영향 없음)' -ForegroundColor Yellow }
elseif ($busy) { Write-Host "  포트 $DashboardPort 사용 중 - 이미 떠 있는 대시보드를 그대로 씁니다(데모 폴더·프로필이 같은지 확인)" -ForegroundColor Yellow }
else {
    Start-Process $PY -WorkingDirectory $MAIN -ArgumentList "`"$MAIN\dashboard\server.py`"", '--port', $DashboardPort, '--results-dir', "`"$DEMO`"", '--active-profile', "`"$AP`"" -WindowStyle Minimized
    Write-Host "  브라우저: http://127.0.0.1:$DashboardPort/  (F11 전체 화면)"
}

Step '7/8 실행기 사전검사(preflight-only, 클러스터 변경 없음)'
$session = "$WT\experiments\guarded_pilot_session.ps1"
$runner = "$WT\experiments\run_demo_phase2.py"
$approval = "$DEMO\$Run-act-approval.json"
powershell -NoProfile -File $session -Run $Run -Mode act -ActApproval $approval -RunnerScript $runner -SessionRoot $DEMO -PythonExe $PY -KeepAwakeScript $KA -PreflightOnly
if ($LASTEXITCODE -ne 0) { Stop-Demo "사전검사 막힘(exit $LASTEXITCODE) - 위 출력 확인" }

Step '8/8 ★ 세션 시작 - 새 Pod·요청 발생기·감지기 준비 후 압박 직전에서 대기'
$state = "$DEMO\demo-state-$RUNID.json"
Remove-Item $state, "$DEMO\demo-go-$Run.flag" -ErrorAction SilentlyContinue   # 지난 시도의 상태·신호 파일로 '준비 완료'를 오판하지 않도록
$t0 = Get-Date
Start-Process powershell -WorkingDirectory $WT -ArgumentList '-NoProfile', '-File', "`"$session`"", '-Run', $Run, '-Mode', 'act', '-ActApproval', "`"$approval`"", '-RunnerScript', "`"$runner`"", '-SessionRoot', "`"$DEMO`"", '-PythonExe', "`"$PY`"", '-KeepAwakeScript', "`"$KA`"" -WindowStyle Hidden
Remove-Item Env:GUARDED_ADMIN_TOKEN -ErrorAction SilentlyContinue
while ($true) {
    if (Test-Path $state) {
        $s = Get-Content $state -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($s.phase -eq 'waiting_for_trigger') { break }
    }
    $ended = Get-ChildItem $DEMO -Directory -Filter "session-$Run-*" -ErrorAction SilentlyContinue |
        Where-Object { $_.CreationTime -gt $t0 -and (Test-Path "$($_.FullName)\session-end.json") }
    if ($ended) { Stop-Demo "실행기가 대기 전에 끝났습니다 - $($ended[0].FullName) 의 로그 확인" }
    if (((Get-Date) - $t0).TotalSeconds -gt $ReadyWaitSec) { Stop-Demo "준비가 $ReadyWaitSec 초 안에 끝나지 않았습니다" }
    Write-Host -NoNewline '.'
    Start-Sleep 5
}
Write-Host ""
Write-Host "준비 완료 ($Run) - 정상 서비스 중, 압박 없음. 15분 안에 압박을 시작하세요:" -ForegroundColor Green
Write-Host "  $WT\demo\start-pressure.ps1" -ForegroundColor Green
