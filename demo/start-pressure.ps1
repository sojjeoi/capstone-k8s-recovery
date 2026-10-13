# 발표 중 한 줄: .\start-pressure.ps1  - 준비된 Phase 2 시연 세션에 '압박 시작' 신호 파일을 만든다(★ 이후 CPU 압박이 주입된다).
# 이 뒤는 자동: CPU 압박 → CPU 임계치 3회 연속 → 이상 감지 → Recovery Policy → Argo 승격 → Service 전환 → (전환 확인 뒤) 이번
# 데모의 StressChaos만 정리 → 요청 이동 검증. 대시보드: http://127.0.0.1:8765/
$ErrorActionPreference = 'Stop'
$DEMO = Join-Path $PSScriptRoot '..\experiments\results\demo_only_phase2'
$waiting = Get-ChildItem $DEMO -Filter 'demo-state-*.json' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending |
    ForEach-Object { Get-Content $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json } | Where-Object { $_.phase -eq 'waiting_for_trigger' } |
    Select-Object -First 1
if (-not $waiting) { Write-Host '압박을 기다리는 세션이 없습니다 - 먼저 .\prepare-demo.ps1 (또는 이미 시작됨)' -ForegroundColor Red; exit 1 }
Set-Content -Path (Join-Path $DEMO $waiting.trigger_file) -Value (Get-Date -Format o) -Encoding ASCII
Write-Host "압박 시작 신호 보냄: $($waiting.run)" -ForegroundColor Green
$state = Join-Path $DEMO "demo-state-$($waiting.run_id).json"
for ($i = 0; $i -lt 10; $i++) {
    Start-Sleep 1
    $s = Get-Content $state -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($s.phase -eq 'pressure_started') { Write-Host "실행기 확인: 압박 주입 시작 ($($s.triggered_at_utc))" -ForegroundColor Green; exit 0 }
}
Write-Host '실행기가 아직 신호를 읽지 않았습니다(2초 주기) - 대시보드를 보세요' -ForegroundColor Yellow
