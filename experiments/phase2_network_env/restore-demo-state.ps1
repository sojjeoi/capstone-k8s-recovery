<#
.SYNOPSIS
  시연 환경 원복 절차 (네트워크 지연 검증을 중단하거나 끝낼 때).  DEVELOPMENT - 작성만 했고 실행한 적 없다.

.DESCRIPTION
  기본은 점검만 하는 드라이런이다. 클러스터를 바꾸려면 -Apply 를 줘야 하고, 바꾸는 단계마다 한 번 더 확인(ShouldProcess)을 묻는다.
  이 스크립트가 바꿀 수 있는 것은 아래 세 가지뿐이다 (그 밖의 kubectl 호출은 모두 get):
    1. 남은 지연 설정 삭제          kubectl delete networkchaos netdelay-*      (우리 실험이 만든 이름 접두어만)
    2. 기본 Rollout 다시 적용        kubectl apply -f gitops/apps/vllm-serving/rollout.yaml
         = overlays/network-tolerant/kustomization.yaml 5번에 적힌 원복 방법. 11초 probe 템플릿이 남아 있을 때만.
    3. 새 리비전 승격               kubectl argo rollouts promote <rollout>     (preview 가 Ready 로 Paused 가 된 뒤에만)
  정책(recovery-policy) 상태·토큰·Service·Pod 는 건드리지 않는다. 정책 context/mode 가 어긋나 있으면 마지막 확인에서 문제로만 보고한다.

  비용 주의: 2번과 3번은 Rollout 템플릿 변경이라 vLLM Pod 가 새로 뜬다(그동안 vLLM Pod 가 2개). 걸리는 시간은 이 환경에서 측정한 적이 없다.
  CPU 여유가 부족하면 새 Pod 가 늦게 Ready 가 될 수 있다(docs/design/phase8-blue-green-preflight-incident.md 의 CPU headroom 기록).
  그래서 대기 상한(-WaitMinutes)을 넘으면 멈추고 사람이 상태를 본 뒤 다시 실행한다(이 스크립트는 여러 번 실행해도 안전하다).

.PARAMETER Baseline
  n2_run.py capture-baseline 으로 수집 시작 전에 저장한 원래 상태 파일 (필수).
.PARAMETER Apply
  지정해야만 클러스터를 바꾼다. 없으면 무엇을 할지 출력만 한다.
.PARAMETER DemoPreflight
  기존 시연 사전점검 스크립트 경로. 주면 마지막에 실행한다. 없으면 "직접 실행해야 한다"고 알린다.

.EXAMPLE
  # 점검만 (아무것도 바꾸지 않음)
  .\restore-demo-state.ps1 -Baseline C:\path\baseline.json
.EXAMPLE
  # 실제 원복 (사용자 승인 뒤)
  $env:GUARDED_ADMIN_TOKEN = '...'   # 확인 단계(정책 상태 읽기)에 필요. 이 스크립트는 토큰을 출력·저장하지 않는다.
  .\restore-demo-state.ps1 -Baseline C:\path\baseline.json -Apply
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory = $true)][string]$Baseline,
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$Namespace = 'vllm-serving',
    [string]$Rollout = 'vllm-serving',
    [string]$Python = 'python',
    [string]$DemoPreflight = '',
    [int]$WaitMinutes = 20,
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$ChaosPrefix = 'netdelay-'
$BaseRolloutYaml = Join-Path $RepoRoot 'gitops\apps\vllm-serving\rollout.yaml'
$RunPy = Join-Path $PSScriptRoot 'n2_run.py'
$Stopped = $false

function Get-KubectlJson {
    # 읽기 전용: get 만 허용한다.
    param([string[]]$KubectlArgs)
    if ($KubectlArgs[0] -ne 'get') { throw "Get-KubectlJson 은 get 만 쓴다: $($KubectlArgs -join ' ')" }
    $out = & kubectl @KubectlArgs -o json
    if ($LASTEXITCODE -ne 0) { throw "kubectl $($KubectlArgs -join ' ') 실패 (exit $LASTEXITCODE)" }
    return ($out | Out-String | ConvertFrom-Json)
}

function Get-VllmProbeTimeouts {
    param($RolloutObject)
    $c = $RolloutObject.spec.template.spec.containers | Where-Object { $_.name -eq 'vllm' } | Select-Object -First 1
    return [pscustomobject]@{ Readiness = $c.readinessProbe.timeoutSeconds; Liveness = $c.livenessProbe.timeoutSeconds }
}

function Get-ChaosNames {
    param([bool]$Ours)
    $items = (Get-KubectlJson @('get', 'networkchaos', '-n', $Namespace)).items
    $names = @($items | ForEach-Object { $_.metadata.name })
    if ($Ours) { return @($names | Where-Object { $_ -like "$ChaosPrefix*" }) }
    return @($names | Where-Object { $_ -notlike "$ChaosPrefix*" })
}

function Invoke-Change {
    # 클러스터를 바꾸는 유일한 통로. -Apply 가 없으면 하지 않고, 있으면 단계마다 한 번 더 확인을 묻는다.
    [CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
    param([string]$Description, [scriptblock]$Action)
    if (-not $Apply) { Write-Host "[dry-run] 하게 될 일: $Description"; return $false }
    if (-not $PSCmdlet.ShouldProcess("$Namespace/$Rollout", $Description)) { Write-Host "[건너뜀] $Description"; return $false }
    Write-Host "[실행] $Description"
    & $Action
    return $true
}

function Wait-Until {
    param([scriptblock]$Condition, [string]$What)
    $deadline = (Get-Date).AddMinutes($WaitMinutes)
    while ((Get-Date) -lt $deadline) {
        if (& $Condition) { return $true }
        Start-Sleep -Seconds 10
    }
    Write-Warning "대기 상한($WaitMinutes 분) 초과: $What - 멈춘다. 상태를 확인한 뒤 다시 실행한다."
    return $false
}

# ---------- 0. 준비 확인 ----------
if (-not (Test-Path -LiteralPath $Baseline)) { throw "원래 상태 파일이 없다: $Baseline" }
if (-not (Test-Path -LiteralPath $BaseRolloutYaml)) { throw "기본 Rollout 파일이 없다: $BaseRolloutYaml" }
$base = (Get-Content -LiteralPath $Baseline -Raw -Encoding UTF8 | ConvertFrom-Json).state
$wantReadiness = [int]$base.probe_profile.readiness_timeout_sec
$wantLiveness = [int]$base.probe_profile.liveness_timeout_sec
Write-Host "원래 상태(보존 파일): readiness=$wantReadiness s, liveness=$wantLiveness s, 유예=$($base.scale_down_delay) s, 정책 mode=$($base.policy.mode)"
if (-not $Apply) { Write-Host "※ 드라이런입니다. 클러스터를 바꾸지 않습니다. (-Apply 를 줘야 바꿉니다)" }

# ---------- 1. 지금 상태 읽기 (읽기 전용) ----------
$ro = Get-KubectlJson @('get', 'rollout', $Rollout, '-n', $Namespace)
$cur = Get-VllmProbeTimeouts $ro
$ours = Get-ChaosNames $true
$others = Get-ChaosNames $false
Write-Host "지금: readiness=$($cur.Readiness) s, liveness=$($cur.Liveness) s, phase=$($ro.status.phase), active=$($ro.status.blueGreen.activeSelector), preview=$($ro.status.blueGreen.previewSelector)"
Write-Host "남은 지연 설정(우리 접두어 $ChaosPrefix): $($ours.Count)개 $($ours -join ', ')"
if ($others.Count -gt 0) {
    Write-Warning "우리 접두어가 아닌 NetworkChaos 가 있다: $($others -join ', ') - 이 스크립트는 지우지 않는다. 사람이 확인한다."
}

# ---------- 2. 남은 지연 설정 삭제 ----------
$deleted = $false
foreach ($name in $ours) {
    $did = Invoke-Change "NetworkChaos '$name' 삭제" { & kubectl delete networkchaos $name -n $Namespace --wait=true; if ($LASTEXITCODE -ne 0) { throw "삭제 실패: $name" } }
    if ($did) { $deleted = $true }
}
if ($deleted) {
    $gone = Wait-Until { (Get-ChaosNames $true).Count -eq 0 } '지연 설정 소멸'
    if (-not $gone) { $Stopped = $true }
}

# ---------- 3. 기본 Rollout 복구 (11초 probe 템플릿이 남아 있을 때만) ----------
$needRollout = ($cur.Readiness -ne $wantReadiness) -or ($cur.Liveness -ne $wantLiveness)
if (-not $Stopped -and $needRollout) {
    Write-Host "probe 설정이 원래와 다르다 -> 기본 Rollout 다시 적용 + 새 리비전 승격이 필요하다 (vLLM Pod 가 새로 뜬다)"
    $plugin = $true
    try {
        & kubectl argo rollouts version > $null 2> $null
        if ($LASTEXITCODE -ne 0) { $plugin = $false }
    }
    catch { $plugin = $false }
    if (-not $plugin) { Write-Warning "kubectl argo rollouts 플러그인이 없다 - 승격은 평소 쓰는 경로(recovery-policy 승격 경로 등)로 사람이 한다." }
    $applied = Invoke-Change "기본 Rollout 다시 적용: kubectl apply -f $BaseRolloutYaml" { & kubectl apply -f $BaseRolloutYaml -n $Namespace; if ($LASTEXITCODE -ne 0) { throw 'apply 실패' } }
    if ($applied) {
        $ready = Wait-Until {
            $r = Get-KubectlJson @('get', 'rollout', $Rollout, '-n', $Namespace)
            ($r.status.phase -eq 'Paused') -and ($r.status.blueGreen.previewSelector) -and ($r.status.blueGreen.previewSelector -ne $r.status.blueGreen.activeSelector)
        } '새 리비전 preview 가 Paused 로 대기'
        if (-not $ready) { $Stopped = $true }
        elseif ($plugin) {
            [void](Invoke-Change "새 리비전 승격: kubectl argo rollouts promote $Rollout" { & kubectl argo rollouts promote $Rollout -n $Namespace; if ($LASTEXITCODE -ne 0) { throw 'promote 실패' } })
            $done = Wait-Until {
                $r = Get-KubectlJson @('get', 'rollout', $Rollout, '-n', $Namespace)
                ($r.status.phase -eq 'Healthy') -and ($r.status.blueGreen.activeSelector -eq $r.status.blueGreen.previewSelector)
            } '승격 완료(Healthy, active=preview)'
            if (-not $done) { $Stopped = $true }
        }
        else { $Stopped = $true }
    }
}
else { Write-Host "probe 설정은 이미 원래와 같거나 앞 단계에서 멈췄다 -> Rollout 은 건드리지 않는다" }

# ---------- 4. 원복 확인 (읽기 전용) ----------
$verifyOk = $false
if ($env:GUARDED_ADMIN_TOKEN) {
    Write-Host '--- 원복 확인 (n2_run.py restore-check, 읽기 전용) ---'
    & $Python $RunPy restore-check --baseline $Baseline
    $verifyOk = ($LASTEXITCODE -eq 0)
}
else {
    Write-Warning 'GUARDED_ADMIN_TOKEN 환경변수가 없어 원복 확인(정책 상태 읽기)을 못 했다 - 원복이 확인되지 않았다.'
}

# ---------- 5. 기존 시연 사전점검 ----------
$demoOk = $false
if ($DemoPreflight) {
    Write-Host "--- 기존 시연 사전점검: $DemoPreflight ---"
    & $Python $DemoPreflight
    $demoOk = ($LASTEXITCODE -eq 0)
}
else {
    Write-Warning '기존 시연 사전점검은 이 스크립트가 대신하지 않는다 - -DemoPreflight 로 경로를 주거나 직접 실행해야 한다. 둘 다 통과해야 원복이 끝난 것이다.'
}

# ---------- 요약 ----------
$allOk = $Apply -and (-not $Stopped) -and $verifyOk -and $demoOk
Write-Host "요약: 변경 허용=$([bool]$Apply) 중단=$Stopped 원복확인=$verifyOk 시연사전점검=$demoOk"
if ($allOk) { exit 0 } else { exit 2 }
