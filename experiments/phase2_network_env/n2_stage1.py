#!/usr/bin/env python3
"""네트워크 환경 Stage 1 세션 실행기의 설정(config)과 adapter(2026-10-17, DEVELOPMENT - 동결 전, 클러스터 접근 없음).

방식 B: 기존 phase2_detector_selection/run_stage1.py(동결, 수정 금지)의 **같은 소스**를 재사용한다.
  - 원본 모듈은 건드리지 않는다. 소스 파일을 읽어 **별도 모듈 인스턴스**로 실행하고(load_stage1), 그 인스턴스의 pc 자리에만
    NetPC adapter를 둔다. 공유 모듈(p2_common 등)의 전역 상태를 바꾸지 않는다(monkeypatch 아님).
  - NetConfig가 네트워크 환경에서 달라지는 값(계획 ID·실행 ID·결과 폴더·실행기 경로·프로필·동결 manifest)을 명시한다.
  - NetPC는 명시한 이름만 네트워크 값이고 나머지는 기존 p2_common에 위임한다. OVERRIDES가 곧 달라지는 목록이다.
  - 원본에서 CPU 환경 전용인 부분(run_pressure·pressure_conditions·pressure_frozen·압박 유효성)은 인스턴스에서 네트워크 버전으로 교체한다(n2_session).
정상 세션 경로(일정·중단 규칙·N1 실행·유효성 판정)는 원본 로직을 그대로 쓴다."""
import sys
sys.dont_write_bytecode = True
import importlib.util  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from pathlib import Path  # noqa: E402

import n2_common as nc  # noqa: E402

pc = nc.pc
STAGE1_SOURCE = nc.P2DIR / "run_stage1.py"


@dataclass(frozen=True)
class NetConfig:
    plan_id: str = "capstone-phase2-network-env-v1"
    short_plan_tag: str = "cap-p2-net-v1"                  # 실행 ID가 쿠버네티스 라벨 한도(63자)를 넘을 때만 plan_id 대신 쓴다(압박 67자, Stage 2 64자; 정상 62자는 그대로)
    prereg_doc: str = nc.PREREG_DOC
    results_dir: Path = nc.EXP / "results" / "phase2_network_env"
    executor: Path = nc.HERE / "n2_executor.py"
    selection_profile: Path = nc.HERE / "profiles" / "selection-profile-network-tolerant-vllm-qwen05b-v5.json"
    # 이전 revision들의 profile(사전등록 해시 필드만 다른 것). 이 profile로 실행된 유효 세션은 선택 분석에 재사용하고(rev3 §9.3: n2n03 = v2),
    # 재개 기록은 이 profile로 실행된 시도에 대해서도 쓸 수 있다(n2n01 = v1, n2p01 = v3, n2p02~06·n2s01 = v4).
    previous_selection_profiles: tuple = tuple(nc.HERE / "profiles" / f"selection-profile-network-tolerant-vllm-qwen05b-v{i}.json" for i in (1, 2, 3, 4))
    implementation_freeze: str = "experiments/phase2_network_env/phase2-network-env-implementation-freeze-r8.json"
    normal_runs: dict = field(default_factory=lambda: {f"n2n{i:02d}": (i, 6000 + i) for i in range(1, nc.MAX_ATTEMPTS["normal"] + 1)})
    pressure_runs: dict = field(default_factory=lambda: {f"n2p{i:02d}": (i, 6100 + i) for i in range(1, nc.MAX_ATTEMPTS["pressure"] + 1)})


class NetPC:
    """run_stage1 인스턴스의 pc 자리. 아래 OVERRIDES 이름만 네트워크 환경 값이고 나머지는 기존 p2_common으로 위임한다."""
    OVERRIDES = ("PLAN_ID", "PREREG_DOC", "SELECTION_PROFILE", "HERE", "IMPLEMENTATION_FREEZE", "DETECTOR_IDS", "MAX_ATTEMPTS",
                 "FROZEN_DETECTORS", "SIMPLICITY_ORDER", "load_profile", "observe_environment", "contract_hashes",
                 "verify_implementation_freeze", "signal_identity")

    def __init__(self, cfg: NetConfig, verify_freeze=None):
        self._cfg = cfg
        self.PLAN_ID, self.PREREG_DOC = cfg.plan_id, cfg.prereg_doc
        self.SELECTION_PROFILE, self.HERE, self.IMPLEMENTATION_FREEZE = cfg.selection_profile, nc.HERE, cfg.implementation_freeze
        self.DETECTOR_IDS, self.FROZEN_DETECTORS, self.SIMPLICITY_ORDER = nc.DETECTOR_IDS, nc.FROZEN_DETECTORS, nc.SIMPLICITY_ORDER
        self.MAX_ATTEMPTS = nc.MAX_ATTEMPTS                  # rev3: 정상 시도 상한 12(나머지는 Phase 2와 같다)
        self.load_profile, self.observe_environment, self.contract_hashes = nc.load_profile, nc.observe_environment, nc.contract_hashes
        self.verify_implementation_freeze = verify_freeze or _unfrozen
        self.signal_identity = nc.signal_identity            # Stage 2: 선택된 후보(CPU·net_probe·IF)의 정책 context identity

    @property
    def PREREG_SHA256(self):                                 # 동결 때 채워지는 값 - 항상 현재 값(None이면 fail-closed)
        return nc.PREREG_SHA256

    def __getattr__(self, name):          # OVERRIDES에 없는 이름은 기존 동결 모듈 그대로
        return getattr(pc, name)


LABEL_MAX = 63


def fit_run_id(orig, cfg):
    """실행 ID는 NetworkChaos 라벨('experiment-run-id')에 그대로 들어가므로 63자를 넘으면 쿠버네티스가 거부한다(n2p01에서 확인).
    63자 이하면 원본 그대로(기존 기록과 같은 ID), 넘으면 plan_id 부분만 짧은 태그로 바꾼다. 같은 입력이면 같은 출력인 순수 함수."""
    def run_id_for(run: str) -> str:
        full = orig(run)
        return full if len(full) <= LABEL_MAX else full.replace(cfg.plan_id, cfg.short_plan_tag)
    return run_id_for


def _unfrozen(*a, **k):
    raise SystemExit("implementation freeze fail-closed: 네트워크 환경 구현 동결 검증기가 연결되지 않음(동결 전)")


def load_stage1(cfg: NetConfig = None, verify_freeze=None, live_fn=None):
    """run_stage1.py 소스를 별도 모듈 인스턴스로 실행하고 네트워크 설정을 끼운 인스턴스를 돌려준다. 원본 모듈·p2_common은 바뀌지 않는다."""
    cfg = cfg or NetConfig()
    spec = importlib.util.spec_from_file_location("n2_stage1_instance", STAGE1_SOURCE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)                                   # 이 시점의 최상위 코드는 원본 pc로 실행된다(값은 아래에서 교체)
    m.pc = NetPC(cfg, verify_freeze)
    m.NORMAL_RUNS, m.PRESSURE_RUNS = dict(cfg.normal_runs), dict(cfg.pressure_runs)
    m.RESULTS_DIR, m.EXECUTOR = Path(cfg.results_dir), Path(cfg.executor)
    m.TARGET = {"normal": nc.N_NORMAL, "pressure": nc.N_PRESSURE}
    m.CONFIG = cfg
    import n2_resume as rn                                       # 호스트 중단(절전) 뒤 재개 조건: 원본 resume_check 대신(rev2 §9.1)
    m.run_id_for = fit_run_id(m.run_id_for, cfg)                        # 라벨 한도(63자) 안에 들어가게(이미 들어가는 정상 세션 ID는 그대로)
    tag = Path(cfg.implementation_freeze).stem.rsplit("-", 1)[-1]       # 구현 동결 revision(r5 등): 재개 기록은 revision마다 따로(덮어쓰기 금지, 이전 기록은 이력으로 남는다)
    m._auth_path = lambda d, run: Path(d) / f"p2-resume-authorization-{run}-{tag}.json"
    m.resume_check = rn.make_resume_check(m, live_fn)
    m.authorize_cleanup_resume = rn.make_authorize(m, live_fn, previous_profiles=cfg.previous_selection_profiles)
    return m
