"""restore-demo-state.ps1 정적 시험(2026-10-17, M4, DEVELOPMENT). **스크립트를 실행하지 않는다** - 문법 분석(파서)과 텍스트 검사만 한다.
핵심: (1) 문법 오류가 없다, (2) 클러스터를 바꾸는 호출은 세 가지뿐이고 모두 -Apply + 확인을 거치는 한 통로(Invoke-Change)에 있다,
(3) 위험한 동작이 없다, (4) 지우는 이름 접두어가 실제 지연 설정 이름과 같고 호출하는 진입점·경로가 실제로 있다."""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
SCRIPT = HERE / "restore-demo-state.ps1"
ROOT = HERE.parent.parent


def code_lines():
    """주석 블록(<# #>)과 # 주석을 뺀 실행 줄들."""
    text = SCRIPT.read_text(encoding="utf-8-sig")
    text = re.sub(r"<#.*?#>", "", text, flags=re.S)
    return [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def test_script_is_utf8_with_bom_so_powershell_5_reads_korean_correctly():
    assert SCRIPT.read_bytes()[:3] == b"\xef\xbb\xbf"


@pytest.mark.skipif(shutil.which("powershell") is None, reason="powershell 없음")
def test_script_parses_without_syntax_errors_and_is_not_executed():
    cmd = ("$e=$null;$t=$null;[void][System.Management.Automation.Language.Parser]::ParseFile($env:N2_SCRIPT,[ref]$t,[ref]$e);"
           "if($e.Count -gt 0){$e | ForEach-Object {$_.Message};exit 1}else{'PARSE_OK'}")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=60,
                       env={**os.environ, "N2_SCRIPT": str(SCRIPT)}, encoding="utf-8", errors="replace")
    assert r.returncode == 0 and "PARSE_OK" in r.stdout, r.stdout + r.stderr


def test_only_three_kinds_of_change_and_each_goes_through_the_guarded_helper():
    lines = code_lines()
    kube = [ln for ln in lines if re.search(r"&\s*kubectl\b", ln)]                            # 실제 호출만(문자열 안의 단어는 제외)
    mutating = [ln for ln in kube if re.search(r"&\s*kubectl\s+(delete|apply|argo\s+rollouts\s+promote)\b", ln)]
    assert len(mutating) == 3
    kinds = {("delete" if "delete networkchaos" in ln else "apply" if "apply -f" in ln else "promote" if "promote" in ln else "?") for ln in mutating}
    assert kinds == {"delete", "apply", "promote"}
    for ln in mutating:
        assert "Invoke-Change" in ln, ln                          # 바꾸는 호출은 모두 -Apply + ShouldProcess 를 거치는 통로 안에 있다
    for ln in kube:                                               # 나머지 kubectl 은 읽기(get)나 플러그인 존재 확인(argo rollouts version)뿐
        if ln in mutating:
            continue
        assert re.search(r"&\s*kubectl\s+(argo\s+rollouts\s+version|@KubectlArgs)", ln), ln
    assert "'get'" in "\n".join(lines)                            # 읽기 통로는 get 만 허용한다


def test_helper_is_dry_run_by_default_and_asks_before_each_change():
    text = "\n".join(code_lines())
    assert "[switch]$Apply" in text and not re.search(r"\$Apply\s*=\s*\$true", text)
    assert "SupportsShouldProcess = $true" in text and "$PSCmdlet.ShouldProcess" in text and "if (-not $Apply)" in text
    assert text.count("function Invoke-Change") == 1


@pytest.mark.parametrize("forbidden", [
    r"kubectl\s+patch", r"kubectl\s+scale", r"rollout\s+undo", r"rollouts\s+(abort|retry|undo|restart|set)", r"--force", r"--grace-period",
    r"delete\s+(pod|rollout|deploy|svc|service|ns|namespace|rs|replicaset)\b", r"Invoke-Expression", r"\biex\b", r"Set-Content", r"Out-File",
    r"Add-Content", r"Remove-Item", r"Stop-Process", r"Restart-", r"git\s+(push|commit|reset|checkout)", r"kubectl\s+edit", r"kubectl\s+exec",
])
def test_no_dangerous_operations(forbidden):
    assert not re.search(forbidden, "\n".join(code_lines()), flags=re.I), forbidden


def test_admin_token_is_only_checked_never_printed_or_stored():
    for ln in code_lines():
        if "GUARDED_ADMIN_TOKEN" in ln:
            assert ln.strip().startswith("if ($env:GUARDED_ADMIN_TOKEN)") or "Write-Warning" in ln, ln
            assert "Write-Host" not in ln, ln
    assert not any(re.search(r"Write-(Host|Output|Verbose|Debug).*\$env:", ln) for ln in code_lines())


def test_chaos_prefix_matches_the_real_adapter_naming_and_other_chaos_is_never_deleted():
    import network_degrade_adapter as nda
    m = re.search(r"\$ChaosPrefix\s*=\s*'([^']+)'", SCRIPT.read_text(encoding="utf-8-sig"))
    assert m and nda._sanitize_cr_name("guarded_pressure_record_only-n2p01-x", 0).startswith(m.group(1))
    text = "\n".join(code_lines())
    assert "-like \"$ChaosPrefix*\"" in text and "-notlike \"$ChaosPrefix*\"" in text         # 우리 이름만 지우고 나머지는 보고만 한다
    assert re.search(r"delete networkchaos \$name", text) and "$others" in text


def test_referenced_files_and_entry_point_exist():
    assert (ROOT / "gitops" / "apps" / "vllm-serving" / "rollout.yaml").is_file()          # 원복에 쓰는 기본 Rollout(overlay 주석 5번)
    run_py = (HERE / "n2_run.py").read_text(encoding="utf-8")
    assert "n2_run.py" in SCRIPT.read_text(encoding="utf-8-sig") and 'add_parser("restore-check")' in run_py
    overlay = (ROOT / "gitops" / "apps" / "vllm-serving" / "overlays" / "network-tolerant" / "kustomization.yaml").read_text(encoding="utf-8")
    assert "kubectl apply -f gitops/apps/vllm-serving/rollout.yaml" in overlay             # 문서화된 원복 방법과 같은 명령
