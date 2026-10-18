"""n2_stage1(config + adapter + 원본 재사용 로더) 시험(2026-10-17, DEVELOPMENT). 클러스터 접근 없음.
핵심: (1) 원본 run_stage1·p2_common이 전혀 바뀌지 않는다, (2) 인스턴스는 네트워크 설정으로 동작한다, (3) adapter가 원본이 쓰는 모든 이름을 제공한다."""
import json
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import n2_common as nc  # noqa: E402
import n2_stage1 as ns1  # noqa: E402
from test_n2_executor import env  # noqa: E402,F401  (합성 외부 artifact + 동결 해시 교체 fixture)

pc = nc.pc
import run_stage1 as orig  # noqa: E402  (Phase 2 동결 원본 - 읽기 전용 대조)

FAKE = "f" * 64


def snapshot_template(ready=11, live=11):
    return {"spec": {"containers": [{"name": "vllm", "image": "vllm-cpu-env:latest", "imagePullPolicy": "Never",
                                     "args": ["--model=Qwen/Qwen2.5-0.5B-Instruct", "--port=8000"],
                                     "resources": {"limits": {"cpu": "3", "memory": "6Gi"}},
                                     "readinessProbe": {"timeoutSeconds": ready}, "livenessProbe": {"timeoutSeconds": live}}]}}


# ---------- (1) 원본 불변 ----------
def test_original_module_and_p2_common_untouched_by_loading_instance():
    before = (dict(orig.NORMAL_RUNS), dict(orig.PRESSURE_RUNS), orig.RESULTS_DIR, orig.EXECUTOR, orig.pc, pc.PLAN_ID,
              tuple(pc.DETECTOR_IDS), pc.PREREG_DOC)
    inst = ns1.load_stage1()
    after = (dict(orig.NORMAL_RUNS), dict(orig.PRESSURE_RUNS), orig.RESULTS_DIR, orig.EXECUTOR, orig.pc, pc.PLAN_ID,
             tuple(pc.DETECTOR_IDS), pc.PREREG_DOC)
    assert before == after and orig.pc is pc and inst is not orig and inst.pc is not pc
    assert all(k.startswith("p2n") for k in orig.NORMAL_RUNS) and orig.RESULTS_DIR.name == "phase2_stage1"
    assert orig.pc.PLAN_ID == "capstone-phase2-detector-selection-v1" and tuple(pc.DETECTOR_IDS) == (nc.CPU_ID, nc.IF_ID)


def test_run_stage1_source_hash_matches_phase2_implementation_freeze():
    m = nc.ROOT / pc.IMPLEMENTATION_FREEZE
    if not m.is_file():
        pytest.skip("Phase 2 implementation freeze manifest 없음")
    want = json.loads(m.read_text(encoding="utf-8"))["files"]["experiments/phase2_detector_selection/run_stage1.py"]
    assert pc.text_sha256(ns1.STAGE1_SOURCE) == want              # 재사용하는 원본 소스가 동결 때와 같다


# ---------- (2) 인스턴스는 네트워크 설정 ----------
def test_instance_uses_network_config_and_ids():
    inst = ns1.load_stage1()
    assert inst.pc.PLAN_ID == "capstone-phase2-network-env-v1" and inst.pc.DETECTOR_IDS == nc.DETECTOR_IDS
    assert list(inst.NORMAL_RUNS)[:2] == ["n2n01", "n2n02"] and len(inst.NORMAL_RUNS) == nc.MAX_ATTEMPTS["normal"] == 12 and list(inst.NORMAL_RUNS)[-1] == "n2n12"
    assert len(inst.PRESSURE_RUNS) == pc.MAX_ATTEMPTS["pressure"] and inst.RESULTS_DIR.name == "phase2_network_env"
    assert inst.EXECUTOR.name == "n2_executor.py" and inst.TARGET == {"normal": nc.N_NORMAL, "pressure": nc.N_PRESSURE}
    assert inst.run_id_for("n2n01") == "guarded_normal_record_only-n2n01-capstone-phase2-network-env-v1"
    assert inst.run_id_for("n2p02") == "guarded_pressure_record_only-n2p02-cap-p2-net-v1"        # 라벨 한도(63자) 때문에 압박 ID만 짧은 태그(n2p01 결함)
    assert inst.kind_of("n2n03") == "normal" and inst.kind_of("n2p01") == "pressure"
    with pytest.raises(SystemExit, match="알 수 없는"):
        inst.kind_of("p2n01")                                      # Phase 2 실행 ID는 이 인스턴스에서 거부


def test_instances_are_independent_and_config_is_explicit(tmp_path):
    a = ns1.load_stage1(ns1.NetConfig(results_dir=tmp_path / "a"))
    b = ns1.load_stage1(ns1.NetConfig(results_dir=tmp_path / "b", plan_id="other-plan"))
    assert a.RESULTS_DIR != b.RESULTS_DIR and a.pc.PLAN_ID != b.pc.PLAN_ID and a.CONFIG.plan_id == "capstone-phase2-network-env-v1"


def test_schedule_and_stop_rules_reused_from_original_logic(tmp_path):
    inst = ns1.load_stage1(ns1.NetConfig(results_dir=tmp_path))
    s = inst.schedule("n2n01", tmp_path)
    assert s["kind"] == "normal" and s["attempt_number"] == 1 and s["counted"] is True
    with pytest.raises(SystemExit, match="순서 밖"):
        inst.schedule("n2n02", tmp_path)                             # 원본의 순서 규칙
    with pytest.raises(SystemExit, match="알 수 없는"):
        inst.schedule("p2p01", tmp_path)
    assert inst.stop_reason(tmp_path) is None and inst.collection_state(tmp_path)["finished"] is False
    pc.write_json_new(tmp_path / "p2-session-n2n01.json", {"run": "n2n01", "kind": "normal", "attempt_number": 1, "status": "run",
                                                           "cleanup_errors": []})
    assert "유효성 기록 없는 시도" in inst.stop_reason(tmp_path)         # 원본의 중단 규칙: 유효성 기록 없는 시도가 있으면 멈춤
    with pytest.raises(SystemExit, match="이미 시도한 ID"):
        inst.schedule("n2n01", tmp_path)


def test_refuse_records_attempt_with_network_run_id(tmp_path):
    inst = ns1.load_stage1(ns1.NetConfig(results_dir=tmp_path))
    sched = inst.schedule("n2n01", tmp_path)
    with pytest.raises(SystemExit, match="세션 시작 안 함"):
        inst.refuse("n2n01", tmp_path, sched, "refused_profile_stale", "PROFILE_STALE: x", "2026-10-20T00:00:00+00:00", "stale")
    rec = json.loads((tmp_path / "p2-session-n2n01.json").read_text(encoding="utf-8"))
    val = json.loads((tmp_path / "p2-validity-n2n01.json").read_text(encoding="utf-8"))
    assert rec["run_id"].endswith("capstone-phase2-network-env-v1") and val["status"] == "not_run_refused_profile_stale" and val["valid"] is False


# ---------- (3) adapter 완전성 ----------
def test_adapter_provides_every_name_the_original_source_uses():
    names = set(re.findall(r"\bpc\.([A-Za-z_][A-Za-z_0-9]*)", ns1.STAGE1_SOURCE.read_text(encoding="utf-8")))
    adapter = ns1.NetPC(ns1.NetConfig())
    missing = sorted(n for n in names if not hasattr(adapter, n))
    assert missing == [] and len(names) >= 20


def test_overrides_are_explicit_instance_attributes_and_rest_delegates():
    a = ns1.NetPC(ns1.NetConfig())
    for name in ns1.NetPC.OVERRIDES:
        assert name in a.__dict__, name                               # 명시한 이름은 adapter 자신의 값
    assert a.sha256_bytes is pc.sha256_bytes and a.write_json_new is pc.write_json_new and a.SHADOW == pc.SHADOW   # 나머지는 위임
    assert a.IF_THRESHOLD == pc.IF_THRESHOLD and a.N_NORMAL == nc.N_NORMAL and a.MAX_ATTEMPTS == nc.MAX_ATTEMPTS and a.MAX_ATTEMPTS["pressure"] == pc.MAX_ATTEMPTS["pressure"] and a.MAX_ATTEMPTS["stage2"] == pc.MAX_ATTEMPTS["stage2"]
    assert a.DETECTOR_IDS != pc.DETECTOR_IDS and a.PLAN_ID != pc.PLAN_ID


def test_prereg_sha_is_live_and_signal_identity_covers_the_network_candidate(monkeypatch):
    a = ns1.NetPC(ns1.NetConfig())
    monkeypatch.setattr(nc, "PREREG_SHA256", None)
    assert a.PREREG_SHA256 is None                                    # 동결 전 - 어떤 profile도 거부되는 값
    monkeypatch.setattr(nc, "PREREG_SHA256", FAKE)
    assert a.PREREG_SHA256 == FAKE                                    # 동결 뒤 채워지면 adapter도 따라간다(복사해 두지 않음)
    ident = a.signal_identity(nc.NET_ID, {"scaler.pkl": "a" * 64})
    assert ident["detector_id"] == nc.NET_ID and ident["threshold"] == nc.NET_THRESHOLD
    assert pc.signal_identity(nc.CPU_ID, {})["detector_id"] == a.signal_identity(nc.CPU_ID, {})["detector_id"]


def test_unfrozen_implementation_freeze_fails_closed():
    inst = ns1.load_stage1()
    with pytest.raises(SystemExit, match="fail-closed"):
        inst.pc.verify_implementation_freeze()


# ---------- 재사용한 사전 점검(phase2_preflight)이 네트워크 프로필로 동작 ----------
def _write_profile(tmp_path, template):
    obs = nc.observe_environment(template)
    pol = nc.contract_hashes()["recovery_policy_sha256"]
    prof = nc.build_selection_profile({k: v for k, v in obs.items() if k not in ("probe_profile", "pressure")}, pol, FAKE)
    p = tmp_path / "net-profile.json"
    p.write_text(json.dumps(prof, indent=2), encoding="utf-8")
    return p


def test_reused_phase2_preflight_checks_network_profile_and_stale(env, tmp_path):
    _, root = env
    inst = ns1.load_stage1()
    tpl = snapshot_template(11, 11)
    profile = _write_profile(tmp_path, tpl)
    ok = inst.phase2_preflight(lambda: {"template": tpl}, profile, root)
    assert ok["stale"] == [] and ok["loaded"]["profile"]["profile_id"] == "network-tolerant-vllm-qwen05b-v1"
    assert set(ok["observed_environment"]) == set(ok["loaded"]["profile"]["environment"])
    stale = inst.phase2_preflight(lambda: {"template": snapshot_template(1, 1)}, profile, root)     # 기본 probe 설정으로 되돌아간 환경
    assert any("probe_profile" in s for s in stale["stale"])


def test_reused_phase2_preflight_rejects_cpu_environment_profile(env, tmp_path):
    """CPU 환경 selection profile(2개 후보)은 네트워크 인스턴스에서 거부된다."""
    _, root = env
    inst = ns1.load_stage1()
    with pytest.raises(nc.ProfileInvalid):
        inst.phase2_preflight(lambda: {"template": snapshot_template()}, pc.SELECTION_PROFILE, root)
