#!/usr/bin/env python3
"""live_cluster로 표시된 테스트는 기본 실행에서 건너뛴다 - recovery-policy
같은 실제 서비스가 떠 있어야(port-forward 필요) 통과하므로, `pytest`
기본 실행은 클러스터 없이 항상 전부 통과해야 한다(experiments/README.md
"테스트" 절 참고). RUN_LIVE_TESTS=1일 때만 켠다.

live_cluster 마커가 **없는** 테스트는 실제 Kubernetes에 접근할 수 없다
(2026-09-19 추가, cluster_guard 픽스처): kubeconfig/in-cluster config 로드와
kubernetes API client 호출(ApiClient.call_api / RESTClientObject.request)이
전부 차단되고, 시도하면 즉시 실패한다. 코드가 그 예외를 삼키거나 백그라운드
스레드에서 났더라도 위반이 기록돼 teardown에서 실패로 드러난다. 사고 배경:
test_network_degrade_adapter.py 일부 테스트가 *_fn 주입을 빠뜨려 오프라인
스위트가 실제 클러스터에 NetworkChaos CR을 만들고 지웠다(docs/design/
phase8-blue-green-preflight-incident.md §40.6, §41). fake/mock으로 대체하면
방해받지 않는다(그 patch가 가드를 덮어쓴다) - 실제 클러스터가 필요한 테스트만
@pytest.mark.live_cluster로 표시해 명시적으로 허용한다."""
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


class ClusterAccessBlocked(AssertionError):
    """live_cluster 마커 없는 테스트가 실제 Kubernetes 접근을 시도했다."""


class _ClusterGuard:
    def __init__(self):
        self.violations = []

    def blocker(self, what: str):
        def _blocked(*args, **kwargs):
            self.violations.append(what)
            raise ClusterAccessBlocked(
                f"live_cluster 마커 없는 테스트가 실제 Kubernetes 접근을 시도함: {what} - "
                "*_fn 주입이나 mock으로 대체하세요(실제 클러스터가 필요한 테스트면 @pytest.mark.live_cluster)")
        return _blocked

    def acknowledge(self) -> list:
        """가드 자체를 검증하는 테스트가 의도적으로 낸 위반을 teardown 실패에서 제외한다."""
        taken, self.violations = self.violations, []
        return taken


# kubernetes.config가 노출하는 config 로더들 - 패키지 이름과 실제 구현 모듈 양쪽을 막는다
# (`from kubernetes.config import x`로 받아 쓰는 코드도, 모듈 경로로 부르는 코드도 걸리게).
_CONFIG_LOADER_NAMES = (
    "load_kube_config", "load_kube_config_from_dict", "load_incluster_config",
    "load_config", "new_client_from_config", "new_client_from_config_dict",
)


@pytest.fixture(autouse=True)
def cluster_guard(request, monkeypatch):
    if request.node.get_closest_marker("live_cluster") is not None:
        yield None  # 명시적으로 live_cluster로 표시된 테스트만 실제 클러스터 접근을 허용
        return
    try:
        import kubernetes.client.api_client as k8s_api_client
        import kubernetes.client.rest as k8s_rest
        import kubernetes.config as k8s_config
        import kubernetes.config.incluster_config as k8s_incluster
        import kubernetes.config.kube_config as k8s_kube_config
    except ImportError:
        yield None  # kubernetes 미설치 환경 - 막을 접근 경로 자체가 없다
        return

    guard = _ClusterGuard()
    for module in (k8s_config, k8s_kube_config, k8s_incluster):
        for name in _CONFIG_LOADER_NAMES:
            if hasattr(module, name):
                monkeypatch.setattr(module, name, guard.blocker(f"{module.__name__}.{name}()"))
    # 실제 API 호출의 병목 두 곳 - config를 어떻게 로드했든(또는 안 했든) 여기서 막힌다.
    monkeypatch.setattr(k8s_api_client.ApiClient, "call_api",
                        guard.blocker("kubernetes ApiClient.call_api() (실제 API 호출)"))
    monkeypatch.setattr(k8s_rest.RESTClientObject, "request",
                        guard.blocker("kubernetes RESTClientObject.request() (실제 HTTP 요청)"))
    yield guard
    if guard.violations:
        pytest.fail(
            "live_cluster 마커 없는 테스트가 실제 Kubernetes 접근을 시도함(예외를 삼켰거나 다른 스레드에서 "
            f"났더라도 실패): {'; '.join(guard.violations)}", pytrace=False)
