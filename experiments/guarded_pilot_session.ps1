# guarded-v1 실행 세션 시작기(2026-10-06 작성, 실제 실행은 사용자 승인 뒤).
#
# 순서와 보장:
#   1) keep_awake → Prometheus 포트포워드 → guarded 정책 포트포워드 순서로 시작. 실제로 시작한 프로세스만 추적한다.
#      시작 직후 일찍 끝난 프로세스가 있으면 시작 실패로 본다.
#   2) session.json 기록(PID·주소) → run_guarded_pilot.py 시작 → 끝날 때까지 기다린다(관찰·서비스 확인 연장·정리·요약 포함).
#   3) finally: 실행기가 아직 살아 있으면 먼저 끝까지 기다린다(상한 RunnerFinalWaitSec). 그래도 살아 있으면 연결을 끊지 않고
#      남겨 둔 PID를 기록한다(실행기 정리가 연결을 쓰는 중일 수 있음).
#      실행기가 끝났으면 keep_awake에 중지 파일 → 종료 확인(미종료는 기록하고 강제 종료) → 포트포워드 정지·확인.
#   4) session-end.json 기록. 종료 코드: 실행기 종료 코드(0이 아니면 그대로), 시작 실패 20, 세션 정리 문제 21,
#      실행기가 끝나지 않아 연결을 남김 22.
# 이 스크립트 자체도 분리 실행해야 도구 셸의 백그라운드 2시간 제한을 받지 않는다(예):
#   Start-Process powershell -ArgumentList '-NoProfile','-File','experiments\guarded_pilot_session.ps1','-Run','grn01','-Mode','record_only' -WindowStyle Hidden
# 아래 '시험용 대체' 인자는 기본값이 실제 경로다(테스트는 로컬 더미 프로세스로 실패 경로만 확인한다).
param(
    [Parameter(Mandatory = $true)][string]$Run,
    [Parameter(Mandatory = $true)][ValidateSet('record_only', 'act')][string]$Mode,
    [string]$ActApproval = '',
    [int]$PolicyPort = 18090,
    [switch]$PreflightOnly,
    # ---- 시험용 대체 ----
    [string]$SessionRoot = '',
    [string]$PythonExe = 'python',
    [string]$RunnerExe = '',
    [string]$RunnerScript = '',
    [string]$KeepAwakeScript = '',
    [string]$PromPfExe = 'kubectl',
    [string]$PolicyPfExe = 'kubectl',
    [string]$PfPrefix = '',
    [int]$PfSettleSec = 5,
    [int]$KeepAwakeStopWaitSec = 15,
    [int]$RunnerFinalWaitSec = 7200
)
$ErrorActionPreference = 'Stop'
$exp = $PSScriptRoot
if (-not $SessionRoot) { $SessionRoot = Join-Path $exp 'results\capstone_guarded_v1_pilot' }
if (-not $RunnerExe) { $RunnerExe = $PythonExe }
if (-not $RunnerScript) { $RunnerScript = Join-Path $exp 'run_guarded_pilot.py' }
if (-not $KeepAwakeScript) { $KeepAwakeScript = Join-Path $exp 'keep_awake.py' }
$sess = Join-Path $SessionRoot ("session-$Run-" + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
New-Item -ItemType Directory -Force $sess | Out-Null
$stopFile = Join-Path $sess 'keep_awake.stop'
$started = [ordered]@{}
$endRec = [ordered]@{ run = $Run; mode = $Mode; session_dir = $sess; started = [ordered]@{}; errors = @();
    runner_exit = $null; keep_awake = $null; port_forwards = [ordered]@{}; left_running = @() }
$exitCode = 0

function Q([string]$s) { '"' + $s + '"' }
function Start-Tracked([string]$name, [string]$exe, [string[]]$argList, [string]$logBase) {
    $p = Start-Process -FilePath $exe -ArgumentList $argList -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $sess "$logBase.log") -RedirectStandardError (Join-Path $sess "$logBase.err")
    $null = $p.Handle  # 종료 코드를 나중에 읽으려면 핸들을 먼저 잡아야 한다(PowerShell 5.1)
    $script:started[$name] = $p
    $script:endRec.started[$name] = $p.Id
    return $p
}
function Stop-Tracked([string]$name) {
    $p = $started[$name]
    if (-not $p.HasExited) { try { Stop-Process -Id $p.Id -Force -ErrorAction Stop } catch {} }
    $gone = $p.WaitForExit(5000)
    $script:endRec.port_forwards[$name] = [ordered]@{ pid = $p.Id; stopped = $gone }
    if (-not $gone) { $script:endRec.errors += "$name not stopped (pid $($p.Id))" }
}

try {
    if (-not $env:GUARDED_ADMIN_TOKEN) { throw 'GUARDED_ADMIN_TOKEN 환경변수가 필요하다(값은 파일에 남기지 않음)' }
    if ($Mode -eq 'act' -and -not $ActApproval) { throw 'act는 -ActApproval(확정 시간 기준 승인 기록)이 필요하다' }
    Start-Tracked 'keep_awake' $PythonExe @((Q $KeepAwakeScript), '--log', (Q (Join-Path $sess 'keep_awake.log')),
        '--stop-file', (Q $stopFile), '--max-sec', '10800') 'keep_awake_stdout' | Out-Null
    $promArgs = @('port-forward', '-n', 'monitoring', 'svc/kube-prom-kube-prometheus-prometheus', '9090:9090')
    $polArgs = @('port-forward', '-n', 'vllm-serving', 'svc/guarded-recovery-policy', "$($PolicyPort):8080")
    if ($PfPrefix) { $promArgs = @((Q $PfPrefix)) + $promArgs; $polArgs = @((Q $PfPrefix)) + $polArgs }
    Start-Tracked 'pf_prometheus' $PromPfExe $promArgs 'pf9090' | Out-Null
    Start-Tracked 'pf_policy' $PolicyPfExe $polArgs "pf$PolicyPort" | Out-Null
    Start-Sleep -Seconds $PfSettleSec
    foreach ($k in @($started.Keys)) {
        if ($started[$k].HasExited) { throw "$k 가 시작 직후 끝남(exit $($started[$k].ExitCode))" }
    }
    [ordered]@{ run = $Run; mode = $Mode; started_local = (Get-Date -Format o)
        keep_awake = $started['keep_awake'].Id; pf_prometheus = $started['pf_prometheus'].Id; pf_policy = $started['pf_policy'].Id
        prometheus_url = 'http://127.0.0.1:9090'; policy_url = "http://127.0.0.1:$PolicyPort"
    } | ConvertTo-Json | Out-File -Encoding utf8 (Join-Path $sess 'session.json')
    $runnerArgs = @((Q $RunnerScript), '--run', $Run, '--mode', $Mode, '--policy-url', "http://127.0.0.1:$PolicyPort",
        '--session-file', (Q (Join-Path $sess 'session.json')))
    if ($ActApproval) { $runnerArgs += @('--act-approval', (Q $ActApproval)) }
    if ($PreflightOnly) { $runnerArgs += '--preflight-only' }
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $runner = Start-Tracked 'runner' $RunnerExe $runnerArgs 'runner'
    $runner.WaitForExit()
}
catch {
    $endRec.errors += "start_or_wait: $($_.Exception.Message)"
    $exitCode = 20
}
finally {
    $runnerDone = $true
    if ($started.Contains('runner')) {
        $r = $started['runner']
        if (-not $r.HasExited) { $null = $r.WaitForExit($RunnerFinalWaitSec * 1000) }  # 정리 중인 실행기를 먼저 기다린다
        if ($r.HasExited) {
            $endRec.runner_exit = $r.ExitCode
            if ($r.ExitCode -ne 0) { $exitCode = $r.ExitCode }
        }
        else {
            $runnerDone = $false
            $endRec.errors += "runner still running after $RunnerFinalWaitSec s - 연결을 끊지 않음"
            $endRec.left_running = @($started.Keys | ForEach-Object { "$($_):$($started[$_].Id)" })
            $exitCode = 22
        }
    }
    if ($runnerDone) {
        if ($started.Contains('keep_awake')) {
            New-Item -ItemType File -Force $stopFile | Out-Null
            $ka = $started['keep_awake']
            $stoppedByRequest = $ka.WaitForExit($KeepAwakeStopWaitSec * 1000)
            $forced = $false
            if (-not $stoppedByRequest) {
                $endRec.errors += "keep_awake not stopped after stop file ($KeepAwakeStopWaitSec s) - forced"
                try { Stop-Process -Id $ka.Id -Force -ErrorAction Stop } catch {}
                $forced = $ka.WaitForExit(5000)
            }
            $endRec.keep_awake = [ordered]@{ pid = $ka.Id; stopped_by_request = $stoppedByRequest; forced_stop = $forced
                exited = $ka.HasExited }
        }
        foreach ($k in @('pf_prometheus', 'pf_policy')) { if ($started.Contains($k)) { Stop-Tracked $k } }
        if ($exitCode -eq 0 -and $endRec.errors.Count -gt 0) { $exitCode = 21 }
    }
    $endRec.exit_code = $exitCode
    $endRec.ended_local = (Get-Date -Format o)
    $endRec | ConvertTo-Json -Depth 5 | Out-File -Encoding utf8 (Join-Path $sess 'session-end.json')
}
exit $exitCode
