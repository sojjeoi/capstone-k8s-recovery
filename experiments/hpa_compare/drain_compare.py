"""구성별 드레인 검증 비교표·그림 (2026-10-23). 실행: python drain_compare.py "F 기존=<폴더>" "D 개선=<폴더>" ...
각 폴더에서 drain-report.json 과 drain-timeline.png 를 읽는다(drain_report.py 를 먼저 실행). 출력: analysis/drain-compare-20261023/{comparison.csv, comparison.json, comparison.png}
추가 Pod-초 = 구 Pod 유지 시간(선택자 변경 → Pod 사라짐)의 첫 번째 인자(기준) 대비 차이. 구 Pod 하나가 요청 2 CPU·4Gi 를 예약하므로 CPU-초·GiB-초는 거기에 비례한다.
"""
import csv
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hc_common as hc  # noqa: E402

OUT = hc.RESULTS / "analysis" / "drain-compare-20261023"
OUT.mkdir(parents=True, exist_ok=True)
runs = [a.split("=", 1) for a in sys.argv[1:]]
rows, base = [], None
for label, d in runs:
    d = Path(d)
    r = json.loads((d / "drain-report.json").read_text(encoding="utf-8"))
    st = r["streams"]
    keys = list(st)
    ret = r.get("old_pod_retention_after_selector_sec")
    base = ret if base is None else base
    fx, ka = r["fixed_new_connection"], r["keepalive"]
    rows.append({"구성": label, "스트리밍 n": st["n"], "정상 완료(finish_reason)": st[keys[0]], "절단([DONE]만)": st[keys[1]], "중단(끊김·오류)": st[keys[2]],
                 "요청별": "; ".join(f"{x['max_tokens']}토큰:{x['outcome']}({x['n_chunks']}청크)" for x in st["per_request"]),
                 "구 Pod 유지(선택자 변경→사라짐, 초)": ret, "기준 대비 추가 Pod-초": None if ret is None or base is None else round(ret - base, 1),
                 "종료 신호→사라짐(초)": r.get("sigterm_to_gone_sec"),
                 "새 연결 요청(선택자 이후) 새 Pod/구 Pod/미확인": "/".join(str(fx["after_selector_served_by"][k]) for k in ("new", "old", "unknown")),
                 "새 연결 실패": fx["after_selector_failed"], "새 연결 0.648초 초과": fx["over_0.648s_after_selector"],
                 "keepalive(선택자 이후) 새 Pod/구 Pod": "/".join(str(ka["after_selector_served_by"][k]) for k in ("new", "old")),
                 "keepalive 실패(500 등)": ka["after_selector_failed"], "keepalive 0.648초 초과": ka["over_0.648s_after_selector"],
                 "장애 주입 정리": r.get("injector_cleanup")})
(OUT / "comparison.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
with open(OUT / "comparison.csv", "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
for r in rows:
    print(json.dumps(r, ensure_ascii=False))

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.image as mpimg  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
matplotlib.rcParams["font.family"] = "Malgun Gothic"
fig, axs = plt.subplots(len(runs), 1, figsize=(11, 6.2 * len(runs)))
axs = [axs] if len(runs) == 1 else list(axs)
for ax, (label, d) in zip(axs, runs):
    ax.imshow(mpimg.imread(Path(d) / "drain-timeline.png"))
    ax.set_title(label, fontsize=13)
    ax.axis("off")
fig.tight_layout()
fig.savefig(OUT / "comparison.png", dpi=110)
