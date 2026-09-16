#!/usr/bin/env python3
"""live_cluster로 표시된 테스트는 기본 실행에서 건너뛴다 - recovery-policy
같은 실제 서비스가 떠 있어야(port-forward 필요) 통과하므로, `pytest`
기본 실행은 클러스터 없이 항상 전부 통과해야 한다(experiments/README.md
"테스트" 절 참고). RUN_LIVE_TESTS=1일 때만 켠다."""
import os

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_cluster: 실제 클러스터/서비스가 떠 있어야 통과 (기본 skip, RUN_LIVE_TESTS=1로 실행)",
    )


def pytest_collection_modifyitems(config, items):
    if os.environ.get("RUN_LIVE_TESTS") == "1":
        return
    skip_live = pytest.mark.skip(reason="live_cluster 테스트 - RUN_LIVE_TESTS=1로 실행하세요")
    for item in items:
        if "live_cluster" in item.keywords:
            item.add_marker(skip_live)
