#!/usr/bin/env python3
"""§74/§8 - artifact SHA-256 무결성 확인. `SHA256SUMS.json`에 적힌 값과
실제 파일 해시를 대조해 불일치를 찾는다 - freeze 이후 누군가 artifact를
조용히 바꿔치기하지 않았는지 확인하는 용도."""
import hashlib
import json
from pathlib import Path


def verify_sha256sums(artifacts_dir: Path) -> list:
    """불일치 목록을 반환한다(빈 리스트 = 전부 일치). manifest에 있는데
    파일이 없거나, 파일은 있는데 해시가 다르면 전부 불일치로 보고한다."""
    manifest_path = artifacts_dir / "SHA256SUMS.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mismatches = []
    for name, expected_sha in manifest.items():
        path = artifacts_dir / name
        if not path.exists():
            mismatches.append(f"{name}: 파일 없음(manifest에는 있음)")
            continue
        actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_sha != expected_sha:
            mismatches.append(f"{name}: SHA 불일치(manifest={expected_sha[:16]}..., 실제={actual_sha[:16]}...)")
    return mismatches
