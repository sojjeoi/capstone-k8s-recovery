"""통일 비교 실행 순서 (2026-11-04, docs/design/UNIFIED-COMPARISON-PREREG-20261104.md).
모든 장애(F1 CPU 압박·F2 네트워크 지연·F3 요청 폭주)에 같은 4군(C 무전환·H 오토스케일링·W 계속 분산·G 우리 시스템)을
블록 단위로, 블록마다 군 순서를 한 칸씩 돌려 실행한다. 세션 실행·복원은 orchestrate.run_hw / run_g 를 그대로 쓴다.
- 하네스 오류만 같은 칸 1회 재시도(원기록 보존). 다른 작업 충돌·복원 실패면 멈춘다. 결과와 무관하게 순서를 바꾸지 않는다.
- 상태는 results/hpa_compare/unified-state.json (기존 orchestrator-state.json 은 건드리지 않는다).
사용: python orchestrate_u.py [--phase check|main|all] [--dry-run]
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import orchestrate as o  # noqa: E402
import g_stage2 as g  # noqa: E402

hc = o.hc
STATE = hc.RESULTS / "unified-state.json"
o.APPROVAL_QUOTE = ("user (chat 2026-11-04: '준비 + 필수 단계까지 오늘 진행해보자' / '돌릴 수 있는거 쭉 돌려줘도 됨' / "
                    "'충분히 확보되는만큼 최대한 진행해줘. 논문에서 신뢰할 수 있을만큼의 반복횟수로') - 통일 비교 사전 등록 20261104")
ARMS = ["C", "H", "W", "G"]


def rot(k, arms=ARMS):
    return arms[k % len(arms):] + arms[:k % len(arms)]


def build_seq():
    """사전 등록 §4: F1 → F2 → F3 우선순위. 기존 유효 세션(F2 H·W 3, G 4 / F3 H·G 2)을 더해 각 칸 5회가 되게 한다."""
    seq = []
    for i in range(5):                                   # F1 블록 1~5: 4군 × 5
        seq += [("f1", 1 + i, a) for a in rot(i)]
    for i in range(2):                                   # F2 블록 11·12: 4군 → H5 W5 G6 C2
        seq += [("f2", 11 + i, a) for a in rot(i)]
    seq += [("f2", 13 + i, "C") for i in range(3)]       # F2 블록 13~15: C 만 → C5
    for i in range(3):                                   # F3 블록 11~13: 4군 → H5 G5 C3 W3
        seq += [("f3", 11 + i, a) for a in rot(i)]
    for i in range(2):                                   # F3 블록 14·15: C·W → C5 W5
        seq += [("f3", 14 + i, a) for a in rot(i, ["C", "W"])]
    # f3-b12-W 는 PC 절전(11/04 14:40~15:22)으로 관측이 끊겨 무효, 재시도(r2)도 같은 끊김으로 시작 못 함 - 기록 보존, 맨 끝에 보충 1회.
    seq.append(("f3", 16, "W"))
    # f3-b14-C 는 앞 세션 축소(11/04 17:52) 때 Rollout 상태가 굳어 복원 실패, 재시도도 같은 이유로 시작 못 함 - 기록 보존, 맨 끝에 보충 1회.
    seq.append(("f3", 16, "C"))
    # f3-b16-C 는 두 시도 모두 시계 불일치(probe_clock_inconsistent) 무효 - 기록 보존. F3 C 만 4회라 5회 목표를 먼저 채운다.
    seq.append(("f3", 26, "C"))
    # 사전 등록 §4 확장: 유효 10회까지 같은 규칙(블록 회전 이어서, F1 → F2 → F3).
    for i in range(5):                                   # F1 블록 6~10: 4군 × 5 → 각 10
        seq += [("f1", 6 + i, a) for a in rot(5 + i)]
    for i in range(5):                                   # F2 블록 21~25: 4군 × 5, G 는 이미 6회라 마지막 블록 제외 → 각 10
        seq += [("f2", 21 + i, a) for a in rot(2 + i) if not (i == 4 and a == "G")]
    for i in range(5):                                   # F3 블록 21~25: 4군 × 5 → 각 10
        seq += [("f3", 21 + i, a) for a in rot(3 + i)]
    return seq


# 새 경로 기능 점검(본 비교에 쓰지 않음). check-f1-b1-W 는 로컬 Prometheus port-forward(localhost:9090)가 없어
# 주입 전 CPU 기준선 조회 실패로 중단(11/04 01:08) - 기록 보존, 다른 ID(b2)로 다시 점검한다.
# check-f1-b93-G 는 상시 9090 port-forward 와 G 실행기의 port-forward 가 충돌해 시작 직후 중단 - 기록 보존, b94 로 다시 점검한다.
CHECKS = [("f1", 2, "W"), ("f1", 94, "G")]


def load_state():
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"sessions": {}, "g_used": []}


def save_state(st):
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def next_g_id(st, fault):
    pool = sorted(k for k, v in g.RUNS_G.items() if v[0] == fault and 50 < int(k[3:]) < 90)
    for k in pool:
        if k not in st["g_used"] and not g.results_dir_for(k).exists():
            return k
    raise SystemExit(f"{fault} G 실행 ID 가 모자람 - g_stage2.RUNS_G 에 추가 필요")


def run_one(st, fault, block, arm, attempt, check=False):
    if arm == "G":
        run = f"n2s{block}" if check else next_g_id(st, fault)
        st["g_used"].append(run)
        save_state(st)
        return o.run_g(run, fault)
    return o.run_hw(arm, fault, block, tag="" if attempt == 1 else "r2", check=check)


def run_list(st, items, check=False):
    for n, (fault, block, arm) in enumerate(items, 1):
        key = f"{'check-' if check else ''}{fault}-b{block}-{arm}"
        done = st["sessions"].get(key)
        if done and done.get("final"):
            continue
        hc.log(f"===== {'점검' if check else '본'} {n}/{len(items)}: {key} =====")
        attempts = list((done or {}).get("attempts") or [])
        for attempt in range(len(attempts) + 1, 3):
            res = run_one(st, fault, block, arm, attempt, check)
            attempts.append(res)
            st["sessions"][key] = {"final": False, "attempts": attempts}
            save_state(st)
            hc.log(f"{key} 시도{attempt}: {res.get('classification')}")
            if res.get("classification") != "harness_error" or check:
                break
        st["sessions"][key] = {"final": True, "attempts": attempts, "classification": attempts[-1].get("classification"),
                               "finished": hc.now_iso()}
        save_state(st)
        last = attempts[-1]
        if last.get("classification") == "blocked_by_other_work":
            hc.log("다른 작업과 충돌 - 멈춤(보고 필요)")
            return 3
        if any(x.get("restore_error") or x.get("revert_error") for x in attempts):
            hc.log("복원 실패 - 멈춤(보고 필요)")
            return 4
        if check and last.get("classification") != "valid":
            hc.log(f"기능 점검 실패({key}) - 본 실행을 시작하지 않는다")
            return 5
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["check", "main", "all"], default="all")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    seq = build_seq()
    if a.dry_run:
        for x in CHECKS:
            print("check", x)
        for i, x in enumerate(seq, 1):
            print(i, x)
        cnt = {}
        for f, _, arm in seq:
            cnt[(f, arm)] = cnt.get((f, arm), 0) + 1
        print("new sessions per cell:", dict(sorted(cnt.items())), "total", len(seq))
        return 0
    import os
    os.chdir(o.ROOT)
    st = load_state()
    if a.phase in ("check", "all"):
        rc = run_list(st, CHECKS, check=True)
        if rc:
            return rc
    if a.phase in ("main", "all"):
        return run_list(st, seq)
    return 0


if __name__ == "__main__":
    sys.exit(main())
