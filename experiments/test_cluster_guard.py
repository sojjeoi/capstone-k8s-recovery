#!/usr/bin/env python3
"""conftest.py의 cluster_guard 검증(2026-09-19 추가) - live_cluster 마커가 없는 테스트가 실제
Kubernetes(kubeconfig/in-cluster config 로드, API client 호출)에 접근하면 즉시 실패하고, 예외를
삼키거나 다른 스레드에서 났더라도 teardown에서 실패로 드러나며, live_cluster 마커가 있는
테스트만 허용하고, 기존 fake/mock 기반 테스트는 방해하지 않는다는 것을 고정한다.

사고 배경: test_network_degrade_adapter.py의 일부 테스트가 *_fn 주입을 빠뜨려 오프라인 스위트를
돌릴 때마다 실제 클러스터에 NetworkChaos CR을 만들고 지웠다(docs/design/
phase8-blue-green-preflight-incident.md §40.6, §41). 이 검증 자체도 가드가 고장 난 경우에 실제
클러스터를 바꾸지 않게 설계했다 - 프로세스 안 테스트는 읽기 전용 호출(GET)만 쓰고, 실제 pytest
세션을 띄우는 통합 테스트는 존재하지 않는 KUBECONFIG로 격리한 서브프로세스에서 돈다."""
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from kubernetes import client, config

BLOCKED = "Kubernetes 접근"  # ClusterAccessBlocked 메시지의 고정 부분
CONFIG_LOADERS = [
    "load_kube_config", "load_kube_config_from_dict", "load_incluster_config",
    "load_config", "new_client_from_config", "new_client_from_config_dict",
]


@pytest.mark.parametrize("name", CONFIG_LOADERS)
def test_config_loaders_are_blocked(cluster_guard, name):
    with pytest.raises(AssertionError, match=BLOCKED):
        getattr(config, name)()
    assert cluster_guard.acknowledge() == [f"kubernetes.config.{name}()"]


def test_underlying_loader_modules_are_blocked_too(cluster_guard):
    # 패키지 이름이 아니라 구현 모듈 경로로 부르는 코드도 걸려야 한다.
    from kubernetes.config import incluster_config, kube_config
    with pytest.raises(AssertionError, match=BLOCKED):
        kube_config.load_kube_config()
    with pytest.raises(AssertionError, match=BLOCKED):
        incluster_config.load_incluster_config()
    assert len(cluster_guard.acknowledge()) == 2


def test_real_api_client_calls_are_blocked_before_any_network(cluster_guard):
    # 읽기 전용(GET) 호출만 쓴다 - 가드가 고장 나도 클러스터를 바꾸지 않는다.
    with pytest.raises(AssertionError, match=BLOCKED):
        client.CoreV1Api().list_namespace()
    with pytest.raises(AssertionError, match=BLOCKED):
        client.CustomObjectsApi().get_namespaced_custom_object("g", "v", "ns", "plural", "name")
    assert len(cluster_guard.acknowledge()) == 2


def test_violation_is_recorded_even_if_the_code_swallows_the_exception(cluster_guard):
    try:
        config.load_kube_config()
    except Exception:
        pass  # 어댑터 스레드처럼 코드가 예외를 삼켜도
    assert cluster_guard.violations, "위반이 기록돼 teardown에서 실패로 드러나야 함"
    cluster_guard.acknowledge()


def test_fake_and_mock_based_tests_are_not_interfered_with(cluster_guard):
    fake_api = MagicMock()
    fake_api.list_namespace.return_value = "fake"
    with patch("kubernetes.config.load_kube_config"), \
            patch("kubernetes.client.CoreV1Api", return_value=fake_api):
        config.load_kube_config()  # 테스트의 patch가 가드를 덮어써 정상 호출
        assert client.CoreV1Api().list_namespace() == "fake"
    assert cluster_guard.violations == [], "mock 기반 사용은 위반이 아님"
    with pytest.raises(AssertionError, match=BLOCKED):  # patch가 끝나면 가드가 원복돼 다시 차단
        config.load_kube_config()
    cluster_guard.acknowledge()


SESSION_TESTS = '''
import threading
from unittest.mock import patch

import pytest
from kubernetes import client, config


def test_direct_call_is_blocked():
    config.load_kube_config()


def test_swallowed_in_thread_still_fails_at_teardown():
    def work():
        try:
            config.load_incluster_config()
        except Exception:
            pass
    t = threading.Thread(target=work)
    t.start()
    t.join()


def test_mock_based_test_is_not_blocked():
    with patch("kubernetes.config.load_kube_config"), patch("kubernetes.client.CoreV1Api"):
        config.load_kube_config()
        client.CoreV1Api().list_namespace()


@pytest.mark.live_cluster
def test_live_cluster_marker_is_allowed():
    assert config.load_kube_config.__module__.startswith("kubernetes"), "live_cluster에는 가드가 없어야 함"
    assert client.ApiClient.call_api.__module__.startswith("kubernetes")
'''


def _outcomes(xml_path: Path) -> dict:
    # pytest junitxml은 "call 실패 + teardown 에러"를 같은 이름의 <testcase> 2개로 기록한다 -
    # 이름별로 합쳐야 한다.
    out = {}
    for case in ET.parse(xml_path).getroot().iter("testcase"):
        out.setdefault(case.get("name"), set()).update(
            c.tag for c in case if c.tag in ("failure", "error", "skipped"))
    return out


def test_guard_in_a_real_pytest_session(tmp_path):
    """실제 pytest 세션(서브프로세스)에서: 직접 호출은 실패, 스레드에서 삼킨 위반은 teardown 에러,
    mock 기반은 통과, live_cluster 마커만 허용. 존재하지 않는 KUBECONFIG로 격리해 가드가 고장 나도
    실제 클러스터에는 닿지 않는다."""
    shutil.copy(Path(__file__).with_name("conftest.py"), tmp_path / "conftest.py")
    (tmp_path / "test_guarded.py").write_text(SESSION_TESTS, encoding="utf-8")
    xml_path = tmp_path / "out.xml"
    env = {**os.environ, "RUN_LIVE_TESTS": "1", "KUBECONFIG": str(tmp_path / "no-such-dir" / "config"),
           "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    env.pop("PYTEST_ADDOPTS", None)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", f"--junitxml={xml_path}",
         "test_guarded.py"],
        cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
    outcomes = _outcomes(xml_path)
    detail = f"\n--- 서브프로세스 pytest 출력 ---\n{proc.stdout}\n{proc.stderr}"
    assert "failure" in outcomes["test_direct_call_is_blocked"], detail
    assert outcomes["test_swallowed_in_thread_still_fails_at_teardown"] == {"error"}, detail
    assert outcomes["test_mock_based_test_is_not_blocked"] == set(), detail
    assert outcomes["test_live_cluster_marker_is_allowed"] == set(), detail


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
