"""F2 블록 2 G 보충 실행 1회 (2026-10-29 사용자 승인). orchestrate.run_g 를 그대로 쓰고 승인 인용만 오늘 것으로 바꾼다.
기존 orchestrator-state.json 은 건드리지 않고, 결과는 results/hpa_compare/supp-20261029/ 에 따로 남긴다.
같은 블록의 H(hc-f2-h-b2)·W(hc-f2-w-b2)는 2026-10-22 실행 - 이 G 는 그보다 늦은 보충 실행임을 보고에 표시한다.
사용: python experiments/hpa_compare/supp_g_20261029.py
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import orchestrate as o  # noqa: E402

RUN = sys.argv[1] if len(sys.argv) > 1 else "n2s26"     # n2s27·n2s28 = 2026-10-30 추가 반복
OUT = o.hc.RESULTS / "supp-20261029"
o.APPROVAL_QUOTE = ("user (chat 2026-10-29: '네트워크 지연 G 1회 보충 실행(11초 프로필 적용·복원 포함), 막히면 중단, 결과는 그대로 반영' 제안에 "
                    "'ppt에 뭘 더 추가하지는 않을건데 걍 몇 번 더 했다 이거정도만 넣게 승인할게 고고') - F2 블록 2 G 보충 1회") if RUN == "n2s26" else \
                   ("user (chat 2026-10-30: '아니야 실험 해도 됨' + 선택지 '우리 시스템만 2회') - F2 G 추가 반복 2회(n2s27·n2s28) 중 1회")

if __name__ == "__main__":
    os.chdir(o.ROOT)
    OUT.mkdir(parents=True, exist_ok=True)
    r = o.run_g(RUN, "f2")
    with open(OUT / f"drive-result-{RUN}.json", "x", encoding="utf-8") as fh:
        fh.write(json.dumps(r, ensure_ascii=False, indent=1, default=str) + "\n")
    print(json.dumps(r, ensure_ascii=False, indent=1, default=str))
    sys.exit(0 if r.get("classification") == "valid" else 1)
