#!/usr/bin/env python3
"""Phase 8 최종 결과 시각화 - 10월 발표용 16:9 슬라이드 재디자인.
§131 확정 분석 집합(core 45 + auxiliary 5), 수치·분모는 §131과 완전히
동일 - 이 스크립트는 결과 JSON·state·hash·모델·SLO 정의·판정 기준·
기존 코드(collect_metrics.py 등)를 전혀 수정하지 않는다(읽기 전용
소비자). 클러스터 접속 없음.

재현 방법: `python generate_phase8_figures.py` (experiments/ 안에서
실행, KUBECONFIG 불필요). 실행 시작 시 §131 문서에 기재된 수치 일부를
하드코딩한 기준값과 자동 대조(verify_against_section_131()) - 불일치가
있으면 AssertionError로 즉시 중단하고 그림을 생성하지 않는다.

산출물(docs/design/phase8-figures/, 전부 PNG 16:9@150dpi + SVG 벡터):
- fig1-detection-path            : 탐지 경로 매트릭스(3x3)
- fig2-detection-timing          : 탐지 시점(native 행 제거, 6행만)
- fig3a/b/c-recovery-*           : 시나리오별 회복시간(독립 슬라이드 3장)
- fig4a-aux-summary              : auxiliary 한눈에 요약(메인)
- fig4b-aux-detail                : auxiliary 반복별 실측값(보조 표)
- manifest.json                  : 그림별 run_id·계산식·유효 n·제외 사유

디자인 기준(발표용): 슬라이드당 메시지 하나 - 방법론·긴 주의문은 짧은
각주 한 줄로 축약(자세한 설명은 문서 §133/발표자 노트로 분리). 16:9
(13.333x7.5in) 실크기 렌더링, 큰 폰트, 옅은 가로 기준선만(격자 최소화),
얇은 테두리, 그림자·과도한 범례 없음. arm 색은 전 그림 공통
(native=회색/fixed_threshold=파랑/proposed=빨강) + 도형(원/사각/삼각)
병행. 중앙값은 굵게 강조하되 개별 n개 관측값·실제 n은 항상 함께 표시,
축 절단·중복점 삭제 없음.
"""
import json
import statistics
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

import collect_metrics as cm

plt.rcParams["font.family"] = "Malgun Gothic"
plt.rcParams["axes.unicode_minus"] = False

RESULTS_DIR = Path("results")
OUT_DIR = Path("../docs/design/phase8-figures")
OUT_DIR.mkdir(parents=True, exist_ok=True)

SCENARIOS = ["load_ramp", "pod_kill", "network_degrade"]
ARMS = ["native", "fixed_threshold", "proposed"]
SCENARIO_LABEL = {"load_ramp": "부하 램프(load_ramp)", "pod_kill": "Pod 강제종료(pod_kill)", "network_degrade": "네트워크 열화(network_degrade)"}
# 발표 화면에는 평이한 한글 용어만 노출한다(native/fixed_threshold/proposed
# 같은 코드 값은 각주·발표자 노트로만 이동, §133 피드백 반영) - ARM_CODE_LABEL
# 은 발표자 노트·매니페스트 등 "정확한 필드명이 필요한 곳"에서만 쓴다.
ARM_LABEL = {"native": "무개입", "fixed_threshold": "고정 임계치", "proposed": "Isolation Forest"}
ARM_CODE_LABEL = {"native": "native", "fixed_threshold": "fixed_threshold", "proposed": "proposed"}
ARM_MARKER = {"native": "o", "fixed_threshold": "s", "proposed": "^"}
ARM_COLOR = {"native": "#8c8c8c", "fixed_threshold": "#4C72B0", "proposed": "#C44E52"}
DETSRC_LABEL = {"predictive": "예측 탐지", "reactive": "반응형 탐지", "없음": "탐지 없음", None: "탐지 없음"}

# 16:9 PowerPoint 위젯 표준 인치 크기(13.333 x 7.5) - "실제 PPT 크기로 렌더링"
SLIDE_W, SLIDE_H = 13.333, 7.5
DPI = 150

TITLE_FS = 27
FOOT_FS = 12.5
AXIS_FS = 18
TICK_FS = 16
DIRECT_FS = 16
N_FS = 16

manifest = {}


def new_slide():
    fig = plt.figure(figsize=(SLIDE_W, SLIDE_H))
    return fig


def clean_ax(ax):
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_linewidth(1.0)
        ax.spines[spine].set_color("#333333")
    ax.tick_params(labelsize=TICK_FS, length=0)


def footnote(fig, text):
    """슬라이드용 짧은 각주 한 줄 - 긴 방법론 설명은 발표자 노트(§133)로 분리."""
    fig.text(0.5, 0.015, text, ha="center", va="bottom", fontsize=FOOT_FS, color="#555555")


def load_authoritative():
    v1 = json.loads((RESULTS_DIR / "official-experiment-state.json").read_text(encoding="utf-8"))
    v2 = json.loads((RESULTS_DIR / "official-experiment-state-v2.json").read_text(encoding="utf-8"))

    core_run_ids, core_paths = [], []
    for rid, t in v2["trials"].items():
        if t["scenario"] in ("load_ramp", "pod_kill") and t["status"] == "completed":
            core_run_ids.append(rid)
            core_paths.append(t["result_path"])
    for rid, t in v1["trials"].items():
        if t["scenario"] == "network_degrade" and t["status"] == "completed":
            core_run_ids.append(rid)
            core_paths.append(t["result_path"])

    aux_run_ids, aux_paths = [], []
    for rid, t in v1["trials"].items():
        if t["analysis_group"] == "auxiliary_negative_control" and t["status"] == "completed":
            aux_run_ids.append(rid)
            aux_paths.append(t["result_path"])

    assert len(core_run_ids) == 45, f"core count={len(core_run_ids)} (기대 45)"
    assert len(aux_run_ids) == 5, f"aux count={len(aux_run_ids)} (기대 5)"

    rows_core = [json.loads(Path(p).read_text(encoding="utf-8")) for p in core_paths]
    rows_aux = [json.loads(Path(p).read_text(encoding="utf-8")) for p in aux_paths]
    out_core, _ = cm.build_comparison(rows_core)
    out_aux, _ = cm.build_comparison(rows_aux)
    return core_run_ids, out_core, aux_run_ids, out_aux


def cell(rows, scenario, arm):
    return [r for r in rows if r["scenario"] == scenario and r["arm"] == arm]


# ------------------------------------------------------ §131 자동 대조
def verify_against_section_131(out_core, out_aux):
    """§131.2/§131.3/§131.4에 기재된 수치를 하드코딩 기준값으로 삼아
    이번 실행의 재계산 결과와 대조한다 - 불일치 시 AssertionError로
    즉시 중단(그림을 만들지 않음). §131 원문 자체는 이 스크립트가
    수정하지 않는다."""

    def med_range(vals):
        vals = [v for v in vals if v is not None]
        return (round(statistics.median(vals), 2), round(min(vals), 2), round(max(vals), 2), len(vals))

    checks = [
        ("load_ramp/native recovery_sec", [r["recovery_sec"] for r in cell(out_core, "load_ramp", "native")], (221.68, 168.58, 321.69, 4)),
        ("load_ramp/fixed_threshold recovery_sec", [r["recovery_sec"] for r in cell(out_core, "load_ramp", "fixed_threshold")], (226.17, 40.99, 259.56, 4)),
        ("load_ramp/proposed recovery_sec", [r["recovery_sec"] for r in cell(out_core, "load_ramp", "proposed")], (124.57, 1.10, 235.87, 4)),
        ("pod_kill/native recovery_sec", [r["recovery_sec"] for r in cell(out_core, "pod_kill", "native")], (257.27, 195.22, 328.23, 5)),
        ("pod_kill/proposed detection_lead_sec", [r["detection_lead_sec"] for r in cell(out_core, "pod_kill", "proposed")], (-51.14, -52.65, -44.57, 5)),
        ("network_degrade/fixed_threshold recovery_sec", [r["recovery_sec"] for r in cell(out_core, "network_degrade", "fixed_threshold")], (382.56, 359.66, 391.59, 5)),
    ]
    for name, vals, expected in checks:
        got = med_range(vals)
        assert got == expected, f"§131 불일치: {name} 기대={expected} 실측={got}"

    nd_proposed = cell(out_core, "network_degrade", "proposed")
    src_counts = {}
    for r in nd_proposed:
        src_counts[r["detection_source"]] = src_counts.get(r["detection_source"], 0) + 1
    assert src_counts == {"reactive": 4, "predictive": 1}, f"§131 불일치: network_degrade/proposed detection_source={src_counts}"
    tr_counts = {}
    for r in nd_proposed:
        tr_counts[r["target_replaced"]] = tr_counts.get(r["target_replaced"], 0) + 1
    assert tr_counts == {False: 3, True: 2}, f"§131 불일치: network_degrade/proposed target_replaced={tr_counts}"

    lr_proposed_leads = sorted(r["detection_lead_sec"] for r in cell(out_core, "load_ramp", "proposed") if r["detection_lead_sec"] is not None)
    expected_leads = sorted([-19.186265, 61.25934, -194.454602, 35.798788])
    assert all(abs(a - b) < 0.01 for a, b in zip(lr_proposed_leads, expected_leads)), f"§131 불일치: load_ramp/proposed leads={lr_proposed_leads}"

    aux_by_rep = {int(r["run_id"].split("-native-")[1].split("-")[0]): r for r in out_aux}
    assert all(aux_by_rep[i]["t_slo"] is None for i in range(1, 6)), "§131 불일치: aux t_slo 5/5 None 아님"

    ws_rise_expected = {1: 965.45, 2: 958.06, 3: 957.66, 4: 914.13, 5: 957.66}
    recovered_expected = {1: False, 2: True, 3: True, 4: True, 5: True}

    print("[검증] §131 수치 자동 대조 PASS - 6개 timing 셀 + detection_source/target_replaced + load_ramp/proposed lead 4값 + aux t_slo 5건 전부 일치")
    return ws_rise_expected, recovered_expected


# ---------------------------------------------------------------- 슬라이드1
def fig1_detection_path(out_core):
    fig = new_slide()
    ax = fig.add_axes((0.18, 0.12, 0.79, 0.66))
    ax.set_xlim(0, 3)
    ax.set_ylim(0, 3.42)  # 3.0이면 열 헤더 밴드가 잘림(§133 피드백) - 헤더용 여백 확보
    ax.axis("off")

    fig.text(0.5, 0.94, "탐지 경로는 시나리오와 대응 방식마다 다르다", ha="center", fontsize=TITLE_FS, fontweight="bold")

    fig1_manifest = {"run_ids": {}, "formula": "detection_source(실제 판단 근거) 개수, detector_process(실행된 daemon)와는 다른 값 - build_comparison() 출력 그대로"}

    col_x = {arm: i + 0.5 for i, arm in enumerate(ARMS)}
    row_y = {scn: 2.5 - i for i, scn in enumerate(SCENARIOS)}

    header_y0, header_h = 3.06, 0.28
    for arm in ARMS:
        ax.add_patch(mpatches.Rectangle((col_x[arm] - 0.46, header_y0), 0.92, header_h, facecolor=ARM_COLOR[arm], edgecolor="none"))
        ax.text(col_x[arm], header_y0 + header_h / 2, ARM_LABEL[arm], ha="center", va="center", fontsize=DIRECT_FS + 1, color="white", fontweight="bold")

    for scn in SCENARIOS:
        ax.text(-0.12, row_y[scn], SCENARIO_LABEL[scn].split("(")[0], ha="right", va="center", fontsize=DIRECT_FS, fontweight="bold")

    for scn in SCENARIOS:
        for arm in ARMS:
            rows = cell(out_core, scn, arm)
            fig1_manifest["run_ids"][f"{scn}/{arm}"] = [r["run_id"] for r in rows]
            det_src = {}
            for r in rows:
                k = r["detection_source"] or "없음"
                det_src[k] = det_src.get(k, 0) + 1
            n_prevented = sum(1 for r in rows if r["outcome"] == "prevented")

            x, y = col_x[arm], row_y[scn]
            ax.add_patch(mpatches.Rectangle((x - 0.46, y - 0.42), 0.92, 0.84, facecolor="#F5F5F5", edgecolor="#999999", linewidth=1.0))

            if arm == "native":
                main_txt, sub_txt = "탐지 없음", "(0/5, 개입 없음)"
            else:
                order = ["predictive", "reactive", "없음"]
                parts = [f"{DETSRC_LABEL[k]} {det_src[k]}/5" for k in order if k in det_src]
                if len(parts) == 1:
                    main_txt, sub_txt = parts[0], ""
                else:
                    main_txt, sub_txt = parts[0], " · ".join(parts[1:])
            ax.text(x, y + 0.10, main_txt, ha="center", va="center", fontsize=DIRECT_FS + 1, fontweight="bold")
            if sub_txt:
                ax.text(x, y - 0.20, sub_txt, ha="center", va="center", fontsize=DIRECT_FS - 3, color="#444444")
            if n_prevented:
                ax.text(x, y - 0.36, f"△ SLO 위반 없음 {n_prevented}/5", ha="center", va="center", fontsize=DIRECT_FS - 4, color="#B05A00")

    footnote(fig, "무개입=native · 고정 임계치=fixed_threshold · Isolation Forest=proposed(예측 모델) · 위반 없음=능동 예방 의미 아님")
    fig.savefig(OUT_DIR / "fig1-detection-path.png", dpi=DPI)
    fig.savefig(OUT_DIR / "fig1-detection-path.svg")
    plt.close(fig)
    manifest["fig1"] = fig1_manifest


# ---------------------------------------------------------------- 슬라이드2
def fig2_detection_timing(out_core):
    fig = new_slide()
    ax = fig.add_axes((0.24, 0.26, 0.68, 0.52))

    fig2_manifest = {
        "formula": "detection_lead_sec = t_detection - t_slo (초). native 행은 detector 자체가 없어 항상 제외(별도 표시 없이 애초에 그리지 않음). "
                   "None(SLO 미위반 또는 미탐지)은 점으로 표시하지 않고 행 옆 개수로 표기.",
        "cells": {},
    }

    rows_ordered = [(s, a) for s in SCENARIOS for a in ["fixed_threshold", "proposed"]]
    ylabels = [f"{SCENARIO_LABEL[s].split('(')[0]}\n{ARM_LABEL[a]}" for s, a in rows_ordered]
    y_positions = np.arange(len(rows_ordered))[::-1]

    all_leads = []
    per_row = {}
    for y, (scenario, arm) in zip(y_positions, rows_ordered):
        rows = cell(out_core, scenario, arm)
        plotted = [(r, r["detection_lead_sec"]) for r in rows if r["detection_lead_sec"] is not None]
        excluded = [r for r in rows if r["detection_lead_sec"] is None]
        per_row[(scenario, arm)] = (y, plotted, excluded)
        all_leads.extend(v for _, v in plotted)
        fig2_manifest["cells"][f"{scenario}/{arm}"] = {
            "plotted_run_ids": [r["run_id"] for r, _ in plotted],
            "excluded_run_ids": [r["run_id"] for r in excluded],
            "n_plotted": len(plotted),
        }
    fig2_manifest["cells"]["native(전체 시나리오)"] = {"excluded_reason": "무개입 - detector 없음, 3개 시나리오 15건 전부 제외", "n_plotted": 0}

    xmin_data, xmax_data = min(all_leads), max(all_leads)
    span = xmax_data - xmin_data
    xlim_lo = xmin_data - span * 0.06
    xlim_hi = xmax_data + span * 0.55  # n-라벨을 위한 오른쪽 여백
    ax.set_xlim(xlim_lo, xlim_hi)
    ax.set_ylim(-0.7, len(rows_ordered) + 0.9)
    ax.axvspan(xlim_lo, 0, color="#4C72B0", alpha=0.06, zorder=0)
    ax.axvspan(0, xlim_hi, color="#C44E52", alpha=0.06, zorder=0)
    ax.axvline(0, color="black", linewidth=2.4, zorder=2)
    top_y = len(rows_ordered) + 0.55
    ax.text(xlim_lo + (0 - xlim_lo) * 0.42, top_y, "◀ 위반 후 탐지", ha="center", va="center", fontsize=DIRECT_FS - 1, color="#2A4D80", fontweight="bold")
    ax.text(0 + (xmax_data - 0) * 0.55, top_y, "위반 전 탐지 ▶", ha="center", va="center", fontsize=DIRECT_FS - 1, color="#8C2A30", fontweight="bold")

    rng = np.random.default_rng(20260925)  # 결정론적 jitter - 재실행해도 항상 동일
    for (scenario, arm), (y, plotted, excluded) in per_row.items():
        jitter = rng.uniform(-0.16, 0.16, size=len(plotted))
        for (r, v), j in zip(plotted, jitter):
            face = "#C44E52" if v > 0 else "#4C72B0"
            ax.scatter(v, y + j, marker=ARM_MARKER[arm], s=180, facecolor=face, edgecolor="black", linewidth=0.8, zorder=3)
        label = f"n={len(plotted)}/5"
        if excluded:
            label += f" (위반없음/미탐지 {len(excluded)}건)"
        ax.text(xmax_data + span * 0.05, y, label, ha="left", va="center", fontsize=N_FS - 2, color="#333333")

    ax.set_yticks(y_positions)
    ax.set_yticklabels(ylabels, fontsize=TICK_FS)
    ax.set_xlabel("SLO 위반 시각 대비 탐지 시점(초)", fontsize=AXIS_FS)
    clean_ax(ax)
    ax.spines["left"].set_visible(False)
    ax.tick_params(axis="y", length=0)
    ax.grid(axis="x", alpha=0.25)

    fig.text(0.5, 0.94, "위반 전(선제) 탐지는 부하 램프 일부 반복에서만 관측됨", ha="center", fontsize=TITLE_FS, fontweight="bold")
    fig.text(0.16, 0.11, f"도형  ■ {ARM_LABEL['fixed_threshold']}   ▲ {ARM_LABEL['proposed']}", ha="left", fontsize=DIRECT_FS - 2, color="#333333")

    footnote(fig, "무개입(native)은 대응 자체가 없어 제외(15건) · 예측 탐지라도 음수면 위반 후 탐지")
    fig.savefig(OUT_DIR / "fig2-detection-timing.png", dpi=DPI)
    fig.savefig(OUT_DIR / "fig2-detection-timing.svg")
    plt.close(fig)
    manifest["fig2"] = fig2_manifest


# ------------------------------------------------------------ 슬라이드3a/b/c
def fig3_recovery_time_single(out_core, scenario, tag):
    fig = new_slide()
    ax = fig.add_axes((0.14, 0.24, 0.78, 0.54))

    cells = {}
    for i, arm in enumerate(ARMS):
        rows = cell(out_core, scenario, arm)
        vals = [r["recovery_sec"] for r in rows if r["recovery_sec"] is not None]
        excluded = [r["run_id"] for r in rows if r["recovery_sec"] is None]
        cells[arm] = {"run_ids_used": [r["run_id"] for r in rows if r["recovery_sec"] is not None],
                       "excluded_run_ids": excluded, "n": len(vals),
                       "median": round(statistics.median(vals), 2) if vals else None}

        rng = np.random.default_rng(abs(hash((scenario, arm, "fig3"))) % (2**32))
        xj = i + rng.uniform(-0.14, 0.14, size=len(vals))
        ax.scatter(xj, vals, marker=ARM_MARKER[arm], s=170, facecolor=ARM_COLOR[arm], edgecolor="black", linewidth=0.8, zorder=3, alpha=0.92)
        if vals:
            med = statistics.median(vals)
            ax.plot([i - 0.26, i + 0.26], [med, med], color="black", linewidth=4.2, zorder=4, solid_capstyle="round")
            ax.text(i + 0.32, med, f"중앙값 {med:.0f}s", va="center", ha="left", fontsize=DIRECT_FS - 2, fontweight="bold")
        n_note = f"n={len(vals)}/5"
        if excluded:
            n_note += f" ({len(excluded)}건 SLO 위반 없음 제외)"
        ax.text(i, -0.09, n_note, transform=ax.get_xaxis_transform(), ha="center", va="top", fontsize=N_FS - 1, color="#333333")

    ax.set_xticks(range(len(ARMS)))
    ax.set_xticklabels([ARM_LABEL[a] for a in ARMS], fontsize=AXIS_FS)
    ax.set_ylabel("회복시간(초)", fontsize=AXIS_FS)
    ax.set_ylim(bottom=0)
    clean_ax(ax)
    ax.grid(axis="y", alpha=0.25)

    scn_kr = SCENARIO_LABEL[scenario].split("(")[0]
    fig.text(0.5, 0.92, f"{scn_kr} - 관측된 회복시간 중앙값 (반복 5회, 우열 판정 아님)", ha="center", fontsize=TITLE_FS - 1, fontweight="bold")

    footnote(fig, "n=5(그룹당) 소표본, 사전 등록 검정 없음 - 우월성 판단 근거 아님(그룹별 노출 시간도 동일하지 않을 수 있음)")
    fig.savefig(OUT_DIR / f"fig3{tag}-recovery-{scenario}.png", dpi=DPI)
    fig.savefig(OUT_DIR / f"fig3{tag}-recovery-{scenario}.svg")
    plt.close(fig)
    manifest[f"fig3{tag}_{scenario}"] = {"formula": "recovery_sec = t_recovery - t_slo (초)", "cells": cells}


# ---------------------------------------------------------------- 슬라이드4a
def fig4a_aux_summary(ws_rise_expected):
    fig = new_slide()
    ax = fig.add_axes((0.03, 0.12, 0.94, 0.62))
    ax.set_xlim(0, 3)
    ax.set_ylim(0, 1)
    ax.axis("off")

    fig.text(0.5, 0.92, "메모리 압박 보조실험 - 무개입 상태 안전성 요약 (핵심 비교와 별도 집계)", ha="center", fontsize=TITLE_FS - 1, fontweight="bold")

    blocks = [
        ("SLO 위반", "0/5", "#2E7D32", "지속적인 위반 없음"),
        ("안전 조건", "5/5", "#2E7D32", "재시작·메모리 부족·노드 상태 전부 정상"),
        ("메모리 사용량\n복귀", "4/5", "#B05A00", "1/5(반복1) 미충족 - 원본값 그대로 유지"),
    ]
    for i, (label, big, color, sub) in enumerate(blocks):
        cx = 0.5 + i
        ax.add_patch(mpatches.Rectangle((cx - 0.44, 0.08), 0.88, 0.84, facecolor="#FAFAFA", edgecolor=color, linewidth=2.4))
        ax.text(cx, 0.72, label, ha="center", va="center", fontsize=DIRECT_FS + 2, fontweight="bold")
        ax.text(cx, 0.42, big, ha="center", va="center", fontsize=52, fontweight="bold", color=color)
        ax.text(cx, 0.18, sub, ha="center", va="center", fontsize=DIRECT_FS - 3, color="#444444", wrap=True)

    footnote(fig, "n=5, 무개입(native) 단일 방식 - 무탐지·무조치는 탐지 성능의 근거 아님 · 반복별 실측값은 Q&A 부록(4-B) 참고")
    fig.savefig(OUT_DIR / "fig4a-aux-summary.png", dpi=DPI)
    fig.savefig(OUT_DIR / "fig4a-aux-summary.svg")
    plt.close(fig)


def fig4b_aux_detail(out_aux, ws_rise_expected, recovered_expected):
    fig = new_slide()
    ax = fig.add_axes((0.06, 0.18, 0.88, 0.58))
    ax.axis("off")

    table_rows = []
    for rep in range(1, 6):
        table_rows.append([f"{rep}", "위반 없음", "없음 / 없음",
                            f"{ws_rise_expected[rep]:.2f}",
                            "충족" if recovered_expected[rep] else "미충족(반복1)"])
    header = ["반복", "SLO 위반", "탐지 / 조치", "메모리 상승(MiB)", "±150MiB 복귀"]

    tbl = ax.table(cellText=table_rows, colLabels=header, loc="center", cellLoc="center")
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(15)
    tbl.scale(1, 2.6)
    for (r, c), cell_ in tbl.get_celld().items():
        cell_.set_edgecolor("#BBBBBB")
        if r == 0:
            cell_.set_facecolor("#4C72B0")
            cell_.set_text_props(color="white", fontweight="bold")
        elif c == 4 and "미충족" in table_rows[r - 1][4]:
            cell_.set_facecolor("#FDE2D5")

    fig.text(0.5, 0.92, "메모리 압박 보조실험 - 반복별 실측값 (Q&A 부록, 메인 미사용)", ha="center", fontsize=TITLE_FS - 1, fontweight="bold")
    footnote(fig, "메모리 상승값은 §131에서 확정한 기준값을 그대로 인용(재계산 아님)")
    fig.savefig(OUT_DIR / "fig4b-aux-detail.png", dpi=DPI)
    fig.savefig(OUT_DIR / "fig4b-aux-detail.svg")
    plt.close(fig)
    manifest["fig4"] = {"ws_rise": ws_rise_expected, "recovered": recovered_expected}


def main():
    core_run_ids, out_core, aux_run_ids, out_aux = load_authoritative()
    ws_rise_expected, recovered_expected = verify_against_section_131(out_core, out_aux)

    manifest["core_run_ids"] = core_run_ids
    manifest["aux_run_ids"] = aux_run_ids
    manifest["core_n"] = len(core_run_ids)
    manifest["aux_n"] = len(aux_run_ids)
    manifest["display_label_to_code"] = {ARM_LABEL[a]: ARM_CODE_LABEL[a] for a in ARMS}  # 발표 라벨->코드 arm 값 대응(§133 피드백)

    fig1_detection_path(out_core)
    fig2_detection_timing(out_core)
    fig3_recovery_time_single(out_core, "load_ramp", "a")
    fig3_recovery_time_single(out_core, "pod_kill", "b")
    fig3_recovery_time_single(out_core, "network_degrade", "c")
    fig4a_aux_summary(ws_rise_expected)
    fig4b_aux_detail(out_aux, ws_rise_expected, recovered_expected)

    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("모든 슬라이드 생성 완료 ->", OUT_DIR.resolve())
    print("core run_ids:", len(core_run_ids), "| aux run_ids:", len(aux_run_ids))


if __name__ == "__main__":
    main()
