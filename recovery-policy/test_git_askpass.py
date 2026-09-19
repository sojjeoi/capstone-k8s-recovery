#!/usr/bin/env python3
"""git_askpass.sh 줄바꿈 회귀 검증(2026-09-19 추가) - 이 스크립트는 Linux 컨테이너에서 셔뱅(#!/bin/sh)으로
직접 실행되므로 CRLF가 섞이면 `cannot exec '/app/git_askpass.sh': No such file or directory`로 모든 감사
push가 실패한다. 2026-09-19 장애: core.autocrlf=true인 Windows PC에서 `git archive`가 전 파일을 CRLF로
내보냈고 그 상태로 이미지를 빌드했다(docs/design/phase8-blue-green-preflight-incident.md §40.2). Python은
CRLF를 허용해 /healthz·API가 정상이라 배포 검증을 그대로 통과했으므로, 여기서 파일 바이트를 직접 고정한다.
루트 .gitattributes의 `recovery-policy/git_askpass.sh text eol=lf`가 재발 방지 장치이고, 마지막 테스트가
그 장치가 실제 장애 조건(autocrlf=true의 git archive)에서 동작함을 고정한다."""
import io
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ASKPASS = Path(__file__).with_name("git_askpass.sh")
REPO_ROOT = Path(__file__).resolve().parent.parent
ASKPASS_REPO_PATH = "recovery-policy/git_askpass.sh"
# .gitattributes 규칙은 그 뒤에 체크아웃되는 파일에만 적용된다 - 규칙 이전에 autocrlf=true로 받은 클론/워크트리는
# 작업트리 파일이 CRLF인 채로 남아 있다(2026-09-19 커밋 검증 중 실제로 겪음).
REFETCH_HINT = "(.gitattributes 적용 이전에 체크아웃된 파일이면 `git checkout -- recovery-policy/git_askpass.sh`로 다시 받으세요)"


def test_git_askpass_starts_with_lf_shebang():
    data = ASKPASS.read_bytes()
    assert data.startswith(b"#!/bin/sh\n"), f"LF 셔뱅이어야 함(CRLF면 컨테이너에서 exec 불가): {data[:16]!r} {REFETCH_HINT}"


def test_git_askpass_has_no_crlf_and_no_utf8_bom():
    data = ASKPASS.read_bytes()
    assert b"\r" not in data, f"CR 문자가 섞임 - 셔뱅이 '#!/bin/sh\\r'가 돼 컨테이너에서 실행되지 않음 {REFETCH_HINT}"
    assert not data.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM이 있으면 셔뱅 인식이 깨짐"


def _git(*args):
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True)


def test_git_archive_keeps_git_askpass_lf_even_under_autocrlf_true():
    """장애 재현 조건(core.autocrlf=true)의 git archive가 내보낸 바이트도 LF여야 한다 - .gitattributes의
    `text eol=lf`가 실제로 동작하는지. 작업트리의 .gitattributes를 보도록 --worktree-attributes를 쓴다."""
    try:
        probe = _git("rev-parse", "--verify", f"HEAD:{ASKPASS_REPO_PATH}")
    except FileNotFoundError:
        pytest.skip("git 없음")
    if probe.returncode != 0:
        pytest.skip("git 저장소/HEAD가 아님(예: 이미지 안에서 실행) - 작업트리 바이트 검증만 적용")
    out = _git("-c", "core.autocrlf=true", "archive", "--worktree-attributes", "HEAD", ASKPASS_REPO_PATH)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")
    with tarfile.open(fileobj=io.BytesIO(out.stdout)) as tar:
        data = tar.extractfile(ASKPASS_REPO_PATH).read()
    assert b"\r" not in data, ".gitattributes(text eol=lf)가 autocrlf=true의 CRLF 변환을 막지 못함"
    assert data.startswith(b"#!/bin/sh\n")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
