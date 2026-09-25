#!/usr/bin/env python3
"""Phase 8 최종 결과 시각화 - §131 확정 분석 집합(core 45 + auxiliary 5)
전용, 오프라인 재생성 스크립트(클러스터 접속 없음).

재현 방법: `python generate_phase8_figures.py` (experiments/ 안에서 실행,
KUBECONFIG 불필요 - official-experiment-state*.json과 results/ 아래
trial-*.json만 읽는다). 산출물은 docs/design/phase8-figures/ 아래
PNG+SVG 쌍 4세트 + manifest.json(그림별 run_id 목록·필터·계산식·유효
n·제외 사유) - 이 스크립트는 결과 JSON·state·hash·코드(collect_metrics.py
등)를 전혀 수정하지 않는다(읽기 전용 소비자).

권위 있는 분석 집합(§129.1/§131 확정, 이 스크립트가 그대로 재사용):
- core 45건: load_ramp/pod_kill=official-experiment-state-v2.json,
  network_degrade=official-experiment-state.json. analysis_group=
  core_fault_comparison, status=completed만. mainexp-v1의 load_ramp/
  pod_kill 선행 자료(precursor, §109.2)는 official-experiment-state*.json
  의 result_path로 파일 목록을 직접 구성해 원천적으로 미포함(디렉터리
  전체 글롭 사용 안 함 - §129.5의 오염 경로를 타지 않음).
- auxiliary 5건: official-experiment-state.json의 analysis_group=
  auxiliary_negative_control, status=completed.
"""
import json
import statistics
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

import collect_metrics as cm

# 한글 렌더링 - Windows 기본 내장 폰트, 별도 설치 불필요(재현성)
plt.rcParams["font.family"] = "Malgun Gothic"
plt.rcParams["axes.unicode_minus"] = False

RESULTS_DIR = Path("results")
OUT_DIR = Path("../docs/design/phase8-figures")
OUT_DIR.mkdir(parents=True, exist_ok=True)

SCENARIOS = ["load_ramp", "pod_kill", "network_degrade"]
ARMS = ["native", "fixed_threshold", "proposed"]
SCENARIO_LABEL = {"load_ramp": "부하 램프", "pod_kill": "Pod 강제종료", "network_degrade": "네트워크 열화"}
ARM_LABEL = {"native": "native", "fixed_threshold": "fixed_threshold", "proposed": "proposed"}
ARM_MARKER = {"native": "o", "fixed_threshold": "s", "proposed": "^"}
ARM_COLOR = {"native": "#8c8c8c", "fixed_threshold": "#4C72B0", "proposed": "#C44E52"}

manifest = {}


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
    out_core, issues_core = cm.build_comparison(rows_core)
    out_aux, issues_aux = cm.build_comparison(rows_aux)
    return core_run_ids, out_core, aux_run_ids, out_aux


def cell(rows, scenario, arm):
    return [r for r in rows if r["scenario"] == scenario and r["arm"] == arm]


# ---------------------------------------------------------------- Figure 1
def fig1_outcome_detection_path(out_core):
    fig, axes = plt.subplots(1, 3, figsize=(15, 6.6), sharey=True)
    outcomes = ["prevented", "recovered"]  # timeout/invalid_run: 0/45 (실측 확인)
    outcome_label = {"prevented": "prevented(SLO 위반 없음)", "recovered": "recovered(위반 후 정상화)"}
    outcome_hatch = {"prevented": "..", "recovered": ""}
    outcome_color = {"prevented": "#DD8452", "recovered": "#55A868"}

    fig1_manifest = {"run_ids": {}, "formula": "outcome 값 개수(5건 중), detection_source 개수(collect_metrics.build_comparison() 출력 그대로)"}

    for ax, scenario in zip(axes, SCENARIOS):
        x = np.arange(len(ARMS))
        bottoms = np.zeros(len(ARMS))
        for oc in outcomes:
            heights = []
            for arm in ARMS:
                rows = cell(out_core, scenario, arm)
                heights.append(sum(1 for r in rows if r["outcome"] == oc))
            ax.bar(x, heights, bottom=bottoms, color=outcome_color[oc], hatch=outcome_hatch[oc],
                   edgecolor="black", linewidth=0.6, label=outcome_label[oc] if scenario == SCENARIOS[0] else None)
            bottoms += np.array(heights)

        for i, arm in enumerate(ARMS):
            rows = cell(out_core, scenario, arm)
            fig1_manifest["run_ids"][f"{scenario}/{arm}"] = [r["run_id"] for r in rows]
            det_src = {}
            for r in rows:
                key = r["detection_source"] or "탐지없음"
                det_src[key] = det_src.get(key, 0) + 1
            if arm == "native":
                label = "탐지:\n없음(0/5)"
            else:
                parts = []
                for k in ("predictive", "reactive", "탐지없음"):
                    if k in det_src:
                        kk = {"predictive": "predictive", "reactive": "reactive(fallback)", "탐지없음": "탐지없음"}[k]
                        parts.append(f"{kk} {det_src[k]}/5")
                label = "탐지:\n" + "\n".join(parts)
            ax.annotate(label, xy=(i, 5.2), ha="center", va="bottom", fontsize=7.6, linespacing=1.4)

        ax.set_xticks(x)
        ax.set_xticklabels([ARM_LABEL[a] for a in ARMS], fontsize=10)
        ax.set_ylim(0, 8.3)
        ax.set_title(SCENARIO_LABEL[scenario], fontsize=12, fontweight="bold")
        ax.set_yticks(range(0, 6))
        ax.grid(axis="y", alpha=0.3)

    axes[0].set_ylabel("trial 수 (n=5/arm)")
    fig.suptitle("그림1. 시나리오×arm별 outcome 분포와 실제 탐지 경로 (core 45건)", fontsize=13, fontweight="bold", y=0.99)
    fig.text(0.5, 0.905,
              "주: 'prevented'는 arm이 능동적으로 예방했다는 뜻이 아니라 이 반복에서 t_slo(지속 SLO 위반)가 관측되지 않았다는 뜻 - "
              "native의 prevented(load_ramp rep1)는 무개입 상태에서 위반이 없었던 경우",
              ha="center", fontsize=8, style="italic")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=2, fontsize=9, bbox_to_anchor=(0.5, -0.02))
    fig.tight_layout(rect=(0, 0.05, 1, 0.87))
    fig.savefig(OUT_DIR / "fig1-outcome-detection-path.png", dpi=200)
    fig.savefig(OUT_DIR / "fig1-outcome-detection-path.svg")
    plt.close(fig)
    manifest["fig1"] = fig1_manifest


# ---------------------------------------------------------------- Figure 2
def fig2_detection_timing(out_core):
    fig, ax = plt.subplots(figsize=(11, 6.5))
    fig2_manifest = {
        "formula": "detection_lead_sec = t_detection - t_slo (초, collect_metrics._seconds_between). "
                   "None(SLO 미위반 또는 미탐지)은 점으로 표시하지 않고 행 옆 개수로 별도 표기.",
        "cells": {},
    }

    rows_ordered = [(s, a) for s in SCENARIOS for a in ARMS]
    ylabels = [f"{SCENARIO_LABEL[s]}/{ARM_LABEL[a]}" for s, a in rows_ordered]
    y_positions = np.arange(len(rows_ordered))[::-1]

    all_leads = []
    per_row_plotted = {}
    for y, (scenario, arm) in zip(y_positions, rows_ordered):
        rows = cell(out_core, scenario, arm)
        plotted = [(r, r["detection_lead_sec"]) for r in rows if r["detection_lead_sec"] is not None]
        excluded = [r for r in rows if r["detection_lead_sec"] is None]
        per_row_plotted[(scenario, arm)] = (y, plotted, excluded)
        all_leads.extend(v for _, v in plotted)
        reason = "무개입(detector 없음)" if arm == "native" else "SLO 미위반 또는 미탐지"
        fig2_manifest["cells"][f"{scenario}/{arm}"] = {
            "plotted_run_ids": [r["run_id"] for r, _ in plotted],
            "excluded_run_ids": [r["run_id"] for r in excluded],
            "excluded_reason": reason if excluded else None,
            "n_plotted": len(plotted),
        }

    xmax = max(abs(v) for v in all_leads) * 1.15
    ax.set_xlim(-xmax, xmax + xmax * 0.28)
    ax.axvline(0, color="black", linewidth=1.4)
    ax.text(0, len(rows_ordered) - 0.35, "SLO 위반 시각(t_slo)=0", ha="center", fontsize=9, fontweight="bold")

    rng = np.random.default_rng(20260925)
    for (scenario, arm), (y, plotted, excluded) in per_row_plotted.items():
        jitter = rng.uniform(-0.13, 0.13, size=len(plotted))
        for (r, v), j in zip(plotted, jitter):
            face = "#C44E52" if v > 0 else "#4C72B0"
            ax.scatter(v, y + j, marker=ARM_MARKER[arm], s=75, facecolor=face, edgecolor="black", linewidth=0.6, zorder=3)
        n_excl = len(excluded)
        label = f"n={len(plotted)}/5"
        if n_excl:
            label += f" (미표시 {n_excl}건)"
        ax.text(xmax + xmax * 0.03, y, label, ha="left", va="center", fontsize=7.8, color="#444444")

    ax.set_yticks(y_positions)
    ax.set_yticklabels(ylabels, fontsize=9.5)
    ax.set_xlabel("detection_lead_sec (초) — 왼쪽(음수)=SLO 위반 '후' 탐지, 오른쪽(양수)=위반 '전' 탐지")
    ax.set_title("그림2. 탐지 시점 - 개별 trial (탐지·위반이 모두 관측된 trial만 표시)", fontsize=12, fontweight="bold")
    fig.text(0.5, 0.955,
              "주: 점의 좌우 위치는 detection_lead_sec 실측 부호로만 결정 - detection_source=predictive라는 사실만으로 '선제 탐지'로 표시하지 않음. "
              "위반 없음/미탐지 trial은 0으로 대체하지 않고 우측에 개수로 표기.",
              ha="center", fontsize=8, style="italic")
    ax.grid(axis="x", alpha=0.3)

    legend_elems = [
        plt.Line2D([0], [0], marker="o", color="w", markerfacecolor="#C44E52", markeredgecolor="black", markersize=9, label="양수(위반 전 탐지)"),
        plt.Line2D([0], [0], marker="o", color="w", markerfacecolor="#4C72B0", markeredgecolor="black", markersize=9, label="음수(위반 후 탐지)"),
        plt.Line2D([0], [0], marker="o", color="w", markerfacecolor="gray", markeredgecolor="black", markersize=9, label="native(도형)"),
        plt.Line2D([0], [0], marker="s", color="w", markerfacecolor="gray", markeredgecolor="black", markersize=9, label="fixed_threshold(도형)"),
        plt.Line2D([0], [0], marker="^", color="w", markerfacecolor="gray", markeredgecolor="black", markersize=9, label="proposed(도형)"),
    ]
    ax.legend(handles=legend_elems, loc="lower left", fontsize=8.3, ncol=2)
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(OUT_DIR / "fig2-detection-lead-time.png", dpi=200)
    fig.savefig(OUT_DIR / "fig2-detection-lead-time.svg")
    plt.close(fig)
    manifest["fig2"] = fig2_manifest


# ---------------------------------------------------------------- Figure 3
def fig3_recovery_time(out_core):
    fig, axes = plt.subplots(1, 3, figsize=(15, 6.6), sharey=False)
    fig3_manifest = {
        "formula": "recovery_sec = t_recovery - t_slo (초). outcome=prevented(t_slo 없음) trial은 0이 아니라 자연 제외 - 각 median 아래 유효 n 표기.",
        "cells": {},
    }

    for ax, scenario in zip(axes, SCENARIOS):
        for i, arm in enumerate(ARMS):
            rows = cell(out_core, scenario, arm)
            vals = [r["recovery_sec"] for r in rows if r["recovery_sec"] is not None]
            excluded = [r["run_id"] for r in rows if r["recovery_sec"] is None]
            fig3_manifest["cells"][f"{scenario}/{arm}"] = {
                "run_ids_used": [r["run_id"] for r in rows if r["recovery_sec"] is not None],
                "excluded_run_ids": excluded, "n": len(vals),
                "median": round(statistics.median(vals), 2) if vals else None,
            }
            rng = np.random.default_rng(abs(hash((scenario, arm, "r3"))) % (2**32))
            xj = i + rng.uniform(-0.12, 0.12, size=len(vals))
            ax.scatter(xj, vals, marker=ARM_MARKER[arm], s=55, facecolor=ARM_COLOR[arm], edgecolor="black", linewidth=0.5, zorder=3, alpha=0.9)
            if vals:
                med = statistics.median(vals)
                ax.plot([i - 0.22, i + 0.22], [med, med], color="black", linewidth=2.4, zorder=4)
            ax.text(i, -0.09, f"n={len(vals)}/5", transform=ax.get_xaxis_transform(), ha="center", va="top", fontsize=8.2, color="#444444")

        ax.set_xticks(range(len(ARMS)))
        ax.set_xticklabels([ARM_LABEL[a] for a in ARMS], fontsize=10)
        ax.set_title(SCENARIO_LABEL[scenario], fontsize=12, fontweight="bold")
        ax.grid(axis="y", alpha=0.3)
        ax.set_ylim(bottom=0)

    axes[0].set_ylabel("recovery_sec (초)")
    fig.suptitle("그림3. 시나리오별 arm 회복시간 - 개별 trial(점)과 중앙값(굵은 선)", fontsize=13, fontweight="bold", y=0.99)
    fig.text(0.5, 0.905,
             "주: 'prevented'(위반 없음) trial은 회복시간 0이 아니라 분석에서 자연 제외 - 각 중앙값의 분모 n을 하단에 표기. "
             "arm별 stage 노출은 promotion 시점에 따라 달라질 수 있어 '동일 주입량을 받았다'는 해석을 하지 않음.",
             ha="center", fontsize=8, style="italic")
    fig.tight_layout(rect=(0, 0.05, 1, 0.87))
    fig.savefig(OUT_DIR / "fig3-recovery-time.png", dpi=200)
    fig.savefig(OUT_DIR / "fig3-recovery-time.svg")
    plt.close(fig)
    manifest["fig3"] = fig3_manifest


# ---------------------------------------------------------------- Figure 4
def fig4_aux_summary(out_aux, aux_run_ids):
    fig, ax = plt.subplots(figsize=(10, 5.4))
    ax.axis("off")

    # §130.3/§131.3에서 canonical baseline(prepare_ok.baseline_working_set_bytes)으로
    # 확정된 값 그대로 재인용(재계산 아님) - rep1=965.45MiB(정정값), rep2-5는 §128.2와 동일
    ws_rise = {1: 965.45, 2: 958.06, 3: 957.66, 4: 914.13, 5: 957.66}
    recovered = {1: False, 2: True, 3: True, 4: True, 5: True}

    table_rows = []
    for rep in range(1, 6):
        table_rows.append([
            f"{rep}", "None(위반 없음)", "False / none",
            f"{ws_rise[rep]:.2f}", ("충족" if recovered[rep] else "미충족(rep1)"),
        ])
    header = ["rep", "t_slo", "detected/action", "ws 상승(MiB)", "±150MiB 복귀"]

    tbl = ax.table(cellText=table_rows, colLabels=header, loc="upper center", cellLoc="center", bbox=[0.05, 0.42, 0.9, 0.5])
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(10.5)
    for (r, c), cell_ in tbl.get_celld().items():
        if r == 0:
            cell_.set_facecolor("#4C72B0")
            cell_.set_text_props(color="white", fontweight="bold")
        elif c == 4 and r >= 1 and "미충족" in table_rows[r - 1][4]:
            cell_.set_facecolor("#FDE2D5")

    summary_lines = [
        "n = 5/5 (memory_pressure_negative_control_v1, native 단일 arm - 다른 arm 실행 없음, §97/§98)",
        "t_slo = None 5/5  (지속 SLO 위반 0/5)",
        "restart/OOM 0/5 변화, target UID 5/5 불변, Node MemAvailable 최솟값 range[4.924, 4.957]GiB (PASS 바 4GiB 이상)",
        "baseline ±150MiB 복귀: 4/5 충족, 1/5(rep1) 미충족 - 원본 recovered=False 유지, PASS로 재분류하지 않음",
    ]
    ax.text(0.05, 0.30, "\n".join(summary_lines), transform=ax.transAxes, fontsize=10, va="top")
    ax.text(0.05, 0.03,
            "주의: native는 detector 자체가 없는 arm(개입 없음) - 이 표의 무탐지·무조치는 detector 성능의 근거가 아니라\n"
            "이 auxiliary 시나리오가 설계대로 '무개입 상태에서 안전하게 SLO를 위반하지 않았는지'만 확인한다(§94.1).",
            transform=ax.transAxes, fontsize=8.3, style="italic", color="#553300")
    ax.set_title("그림4. Memory auxiliary(negative control) 요약 - core와 분리, 5건", fontsize=12.5, fontweight="bold")

    fig.tight_layout()
    fig.savefig(OUT_DIR / "fig4-auxiliary-summary.png", dpi=200)
    fig.savefig(OUT_DIR / "fig4-auxiliary-summary.svg")
    plt.close(fig)
    manifest["fig4"] = {
        "run_ids": aux_run_ids,
        "working_set_rise_source": "§130.3/§131.3 canonical baseline(prepare_ok.baseline_working_set_bytes) 정정값 재인용(재계산 아님)",
    }


def main():
    core_run_ids, out_core, aux_run_ids, out_aux = load_authoritative()
    manifest["core_run_ids"] = core_run_ids
    manifest["aux_run_ids"] = aux_run_ids
    manifest["core_n"] = len(core_run_ids)
    manifest["aux_n"] = len(aux_run_ids)

    fig1_outcome_detection_path(out_core)
    fig2_detection_timing(out_core)
    fig3_recovery_time(out_core)
    fig4_aux_summary(out_aux, aux_run_ids)

    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("모든 그림 생성 완료 ->", OUT_DIR.resolve())
    print("core run_ids:", len(core_run_ids), "| aux run_ids:", len(aux_run_ids))


if __name__ == "__main__":
    main()
