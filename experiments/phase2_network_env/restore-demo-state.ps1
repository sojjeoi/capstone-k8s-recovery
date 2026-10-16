<#
.SYNOPSIS
  시연 환경 원복 절차 (네트워크 지연 검증을 중단하거나 끝낼 때).  DEVELOPMENT - 작성만 했고 실행한 적 없다.

.DESCRIPTION
  기본은 점검만 하는 드라이런이다. 클러스터를 바꾸려면 -Apply 를 줘야 하고, 바꾸는 단계마다 한 번 더 확인(ShouldProcess)을 묻는다.
  이 스크립트가 바꿀 수 있는 것은 아래 세 가지뿐이다 (그 밖의 kubectl 호출은 모두 get, 다른 스크립트는 실행하지 않는다):
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
.PARAMETER VerifyWaitSec
  원복 확인(restore-check)이 통과할 때까지 기다리는 최대 초. 승격 뒤 이전 리비전이 줄어드는 시간 등을 기다린다 (기본 300).

.NOTES
  기존 시연 사전점검은 이 스크립트가 실행하지 않는다. 어떤 스크립트가 읽기 전용인지 이 스크립트가 보장할 수 없고, 변경은 위 세 가지로만 제한하기 때문이다.
  원복 확인(읽기 전용)이 통과한 뒤 사람이 직접 실행해서 통과를 확인한다. 둘 다 통과해야 원복이 끝난 것이다.

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
    [int]$WaitMinutes = 20,
    [int]$VerifyWaitSec = 300,
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

function Format-Timeout {
    # 기본 Rollout 에는 readiness/liveness timeoutSeconds 가 적혀 있지 않다(null = Kubernetes 기본 1초). 없음도 정상 값이다.
    param($Value)
    if ($null -eq $Value) { return '없음(기본 1)' }
    return "$Value"
}

function Test-SameTimeout {
    # null 을 0 으로 바꾸지 않는다: 둘 다 없음이거나 숫자가 같을 때만 같다.
    param($A, $B)
    if ($null -eq $A -or $null -eq $B) { return ($null -eq $A -and $null -eq $B) }
    return ($A -eq $B)
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
    & $Action | Out-Host
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
$wantReadiness = $base.probe_profile.readiness_timeout_sec
$wantLiveness = $base.probe_profile.liveness_timeout_sec
Write-Host "원래 상태(보존 파일): readiness=$(Format-Timeout $wantReadiness), liveness=$(Format-Timeout $wantLiveness), 유예=$($base.scale_down_delay) s, 정책 mode=$($base.policy.mode)"
if (-not $Apply) { Write-Host "※ 드라이런입니다. 클러스터를 바꾸지 않습니다. (-Apply 를 줘야 바꿉니다)" }

# ---------- 1. 지금 상태 읽기 (읽기 전용) ----------
$ro = Get-KubectlJson @('get', 'rollout', $Rollout, '-n', $Namespace)
$cur = Get-VllmProbeTimeouts $ro
$ours = Get-ChaosNames $true
$others = Get-ChaosNames $false
Write-Host "지금: readiness=$(Format-Timeout $cur.Readiness), liveness=$(Format-Timeout $cur.Liveness), phase=$($ro.status.phase), active=$($ro.status.blueGreen.activeSelector), preview=$($ro.status.blueGreen.previewSelector)"
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

# ---------- 3. 기본 Rollout 복구 (상태를 보고 판단한다 - 중간에 멈췄다가 다시 실행해도 이어진다) ----------
function Get-Rollout { return (Get-KubectlJson @('get', 'rollout', $Rollout, '-n', $Namespace)) }

function Test-Reconciled {
    # 컨트롤러가 현재 spec(generation)을 다 반영했는가 - 적용 직후 옛 status 를 믿지 않기 위한 확인
    param($R)
    return (([string]$R.status.observedGeneration) -eq ([string]$R.metadata.generation))
}

$ro = Get-Rollout
$cur = Get-VllmProbeTimeouts $ro
$templateRestored = (Test-SameTimeout $cur.Readiness $wantReadiness) -and (Test-SameTimeout $cur.Liveness $wantLiveness)
if (-not $Stopped -and -not $templateRestored) {
    Write-Host "probe 설정이 원래와 다르다 -> 기본 Rollout 다시 적용이 필요하다 (새 리비전이 뜨고 vLLM Pod 가 새로 뜬다)"
    $applied = Invoke-Change "기본 Rollout 다시 적용: kubectl apply -f $BaseRolloutYaml" { & kubectl apply -f $BaseRolloutYaml -n $Namespace; if ($LASTEXITCODE -ne 0) { throw 'apply 실패' } }
    if ($applied) {
        $reconciled = Wait-Until { Test-Reconciled (Get-Rollout) } 'Rollout 컨트롤러가 적용한 템플릿을 반영(observedGeneration = generation)'
        if (-not $reconciled) { $Stopped = $true }
    }
    else { Write-Host "[dry-run] 적용 뒤 이어서 할 일: 새 리비전 preview 가 Paused 로 대기하는지 확인 -> 승격(kubectl argo rollouts promote) -> Healthy 확인" }
}

# 승격이 필요한가는 지금 상태로 판단한다 (적용을 이번 실행에서 했는지와 무관하게 - 이전 실행이 중간에 멈췄어도 이어서 한다)
$ro = Get-Rollout
$cur = Get-VllmProbeTimeouts $ro
$bg = $ro.status.blueGreen
$restoredNow = (Test-SameTimeout $cur.Readiness $wantReadiness) -and (Test-SameTimeout $cur.Liveness $wantLiveness)
$previewPending = $bg.previewSelector -and ($bg.previewSelector -ne $bg.activeSelector)
$previewIsRestored = $restoredNow -and $previewPending -and ($bg.previewSelector -eq $ro.status.currentPodHash) -and (Test-Reconciled $ro)
if (-not $Stopped -and $restoredNow -and $previewPending -and -not $previewIsRestored) {
    Write-Warning "원래 템플릿인데 preview 가 현재 리비전이 아니거나 컨트롤러가 아직 반영하지 않았다 - 승격하지 않는다. 상태를 확인한다. (active=$($bg.activeSelector) preview=$($bg.previewSelector) current=$($ro.status.currentPodHash))"
    $Stopped = $true
}
if (-not $Stopped -and $previewIsRestored) {
    $plugin = $true
    try {
        & kubectl argo rollouts version > $null 2> $null
        if ($LASTEXITCODE -ne 0) { $plugin = $false }
    }
    catch { $plugin = $false }
    if (-not $plugin) {
        Write-Warning "kubectl argo rollouts 플러그인이 없다 - 승격은 평소 쓰는 경로(recovery-policy 승격 경로 등)로 사람이 한 뒤 이 스크립트를 다시 실행한다."
        $Stopped = $true
    }
    else {
        $ready = Wait-Until {
            $r = Get-Rollout
            (Test-Reconciled $r) -and ($r.status.phase -eq 'Paused') -and ($r.status.blueGreen.previewSelector -eq $r.status.currentPodHash) -and ($r.status.blueGreen.previewSelector -ne $r.status.blueGreen.activeSelector)
        } '복구한 리비전의 preview 가 Paused 로 대기'
        if (-not $ready) { $Stopped = $true }
        else {
            $did = Invoke-Change "복구한 리비전 승격: kubectl argo rollouts promote $Rollout" { & kubectl argo rollouts promote $Rollout -n $Namespace; if ($LASTEXITCODE -ne 0) { throw 'promote 실패' } }
            if ($did) {
                $done = Wait-Until {
                    $r = Get-Rollout
                    (Test-Reconciled $r) -and ($r.status.phase -eq 'Healthy') -and ($r.status.blueGreen.activeSelector -eq $r.status.currentPodHash) -and ($r.status.blueGreen.activeSelector -eq $r.status.blueGreen.previewSelector)
                } '승격 완료(Healthy, active = 현재 리비전 = preview)'
                if (-not $done) { $Stopped = $true }
            }
        }
    }
}
elseif ($restoredNow -and -not $previewPending) { Write-Host "원래 템플릿이고 대기 중인 preview 도 없다 -> 승격은 필요 없다" }

# ---------- 4. 원복 확인 (읽기 전용: 원래 상태 + canonical Healthy) ----------
$verifyOk = $false
if ($env:GUARDED_ADMIN_TOKEN) {
    Write-Host '--- 원복 확인 (n2_run.py restore-check, 읽기 전용) ---'
    & $Python $RunPy restore-check --baseline $Baseline --wait-sec $VerifyWaitSec
    $verifyOk = ($LASTEXITCODE -eq 0)
}
else {
    Write-Warning 'GUARDED_ADMIN_TOKEN 환경변수가 없어 원복 확인(정책 상태 읽기)을 못 했다 - 원복이 확인되지 않았다.'
}

# ---------- 5. 기존 시연 사전점검 (이 스크립트는 실행하지 않는다) ----------
Write-Warning '기존 시연 사전점검은 이 스크립트가 실행하지 않는다 - 원복 확인이 통과한 뒤 직접 실행해서 통과를 확인한다. 둘 다 통과해야 원복이 끝난 것이다.'

# ---------- 요약 ----------
Write-Host "요약: 변경 허용=$([bool]$Apply) 중단=$Stopped 원복확인(읽기 전용)=$verifyOk 시연사전점검=직접 확인 필요"
if ($Apply -and (-not $Stopped) -and $verifyOk) { exit 0 } else { exit 2 }
