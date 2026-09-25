#!/usr/bin/env python3
"""Phase 8 결과 슬라이드 - Cobalt Grid 스타일 시안(2장만: 탐지경로/탐지시점).
§131 확정 분석 집합(core 45 + auxiliary 5)만 사용 - 결과 JSON·state·hash·
모델·SLO·스키마 수정 없음, 클러스터 접속 없음(로컬 파일만 읽는다).

재사용: load_authoritative()/verify_against_section_131()/cell()/
SCENARIOS/ARMS/SCENARIO_LABEL/ARM_LABEL은 experiments/generate_phase8_
figures.py(§132-134에서 이미 검증된 스크립트)에서 그대로 import한다 -
계산 로직을 새로 만들지 않는다. 이 스크립트는 그 위에 Cobalt Grid
디자인 시스템(크림 #F0EBDE + 코발트 #1F2BE0 2색, Newsreader류 큰 제목,
1.5px/1px 헤어라인 구조, 정밀 edge/grid 치수 - frontend-slides 스킬
bold-template-pack/templates/cobalt-grid/design.md 원문 수치를 1920x1080
고정 캔버스 좌표로 환산해 사용)으로 HTML만 새로 렌더링한다.

**의도적 이탈(사용자 명시 지시)**: design.md는 그래프-용지 격자가
"모든 슬라이드 뒤에 항상 있어야 한다(비활성화 불가)"고 규정하지만,
이번 지시("데이터 영역 뒤에는 격자를 깔지 마세요")에 따라 이 2개
결과 슬라이드에서는 격자 배경을 전부 생략했다 - 크림+코발트 2색,
헤어라인, topbar 룰 등 나머지 구조는 그대로 따른다.

재현: `python generate_cobalt_slides.py` (experiments/ 안에서 실행).
산출물: docs/design/phase8-figures/cobalt/slide1-detection-path.html,
slide2-detection-timing.html (편집 가능한 생성 소스) - PNG/PDF는 별도
브라우저 스크린샷·export-pdf.sh 단계에서 생성한다(이 스크립트는
HTML만 만듦).
"""
import json
import statistics
from pathlib import Path

from generate_phase8_figures import (
    load_authoritative,
    verify_against_section_131,
    cell,
    SCENARIOS,
    ARMS,
    SCENARIO_LABEL,
    ARM_LABEL,
)

OUT_DIR = Path("../docs/design/phase8-figures/cobalt")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Cobalt Grid 팔레트(design.md 원문 그대로, 새 색 추가 없음 - 엄격한 2색)
PAPER = "#F0EBDE"
INK = "#1F2BE0"
INK_FAINT = "rgba(31,43,224,0.18)"
INK_SOFT = "#5560E5"

ARM_SHAPE_GLYPH = {"native": "●", "fixed_threshold": "■", "proposed": "▲"}

FONT_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+KR:wght@700&'
    'family=Noto+Sans+KR:wght@400;500;700;900&family=DM+Mono:wght@400;500&display=swap" '
    'rel="stylesheet">'
)

BASE_CSS = """
:root{
  --paper:#F0EBDE; --ink:#1F2BE0; --ink-faint:rgba(31,43,224,0.18); --ink-soft:#5560E5;
  --edge:80px; --pad-top:120px; --pad-bottom:96px;
}
*{box-sizing:border-box;}
html,body{margin:0;padding:0;background:#000;}
.deck-viewport{position:fixed;inset:0;overflow:hidden;background:#000;}
.deck-stage{position:absolute;left:0;top:0;width:1920px;height:1080px;overflow:hidden;background:var(--paper);}
.slide{position:absolute;inset:0;width:1920px;height:1080px;overflow:hidden;background:var(--paper);
  font-family:"Noto Sans KR",sans-serif; color:var(--ink);}
img,video,canvas,svg{max-width:100%;max-height:100%;}
@media (prefers-reduced-motion: reduce){*,*::before,*::after{transition-duration:.01ms!important;}}

.hairline-top, .hairline-bottom{position:absolute;left:var(--edge);right:var(--edge);height:1.5px;background:var(--ink);}
.hairline-top{top:56px;}
.hairline-bottom{bottom:44px;}
.pagenum{position:absolute;right:var(--edge);bottom:64px;font-family:"DM Mono",monospace;font-size:15px;color:var(--ink);letter-spacing:.06em;}
.navhint{position:absolute;left:var(--edge);bottom:64px;font-family:"DM Mono",monospace;font-size:13px;color:var(--ink);letter-spacing:.08em;opacity:.45;}

.frame{position:absolute;left:var(--edge);right:var(--edge);top:var(--pad-top);bottom:var(--pad-bottom);}

.topbar{display:flex;align-items:flex-start;justify-content:space-between;border-bottom:1.5px solid var(--ink);padding-bottom:22px;height:96px;}
.topbar h1{font-family:"Noto Serif KR",serif;font-weight:700;font-size:52px;line-height:1.15;margin:0;letter-spacing:-0.01em;max-width:1440px;}
.labtag{font-family:"DM Mono",monospace;font-size:15px;letter-spacing:.05em;white-space:nowrap;padding-left:24px;padding-top:10px;}
.content-area{position:absolute;left:0;right:0;top:168px;}

.footnote{position:absolute;left:0;right:0;bottom:0;font-family:"Noto Sans KR",sans-serif;font-weight:400;font-size:17px;line-height:1.5;color:var(--ink);opacity:.82;}
"""


def esc(s):
    return str(s)


def slide_shell(body_html, title_html, labtag, footnote_html, pagenum, navhint):
    return f"""
<div class="frame">
  <div class="topbar">
    <h1>{title_html}</h1>
    <div class="labtag">{labtag}</div>
  </div>
  {body_html}
  <div class="footnote">{footnote_html}</div>
</div>
<div class="hairline-top"></div>
<div class="hairline-bottom"></div>
<div class="pagenum">{pagenum}</div>
<div class="navhint">{navhint}</div>
"""


# ==================================================================
# 슬라이드 1 - 탐지 경로 3x3
# ==================================================================
def build_slide1(out_core):
    cols = ARMS
    rows = SCENARIOS
    ROW_LABEL_W = 260
    col_w = (1760 - ROW_LABEL_W) / 3  # frame width(1760) - 행 라벨 거터, 3열 균등 분할

    col_headers = ""
    for i, arm in enumerate(cols):
        x = i * col_w
        col_headers += f"""
        <div style="position:absolute; left:{x:.1f}px; top:0; width:{col_w:.1f}px; height:64px;
                    display:flex; align-items:center; gap:14px; border-bottom:1.5px solid var(--ink);">
          <span style="font-size:26px;">{ARM_SHAPE_GLYPH[arm]}</span>
          <span style="font-family:'Noto Sans KR',sans-serif; font-weight:700; font-size:26px;">{ARM_LABEL[arm]}</span>
        </div>"""

    row_h = (690 - 64) / 3
    cells_html = ""
    manifest = {}
    for r, scn in enumerate(rows):
        y = 64 + r * row_h
        cells_html += f"""
        <div style="position:absolute; left:-260px; top:{y:.1f}px; width:240px; height:{row_h:.1f}px;
                    display:flex; align-items:center; justify-content:flex-end; text-align:right;
                    font-family:'Noto Sans KR',sans-serif; font-weight:700; font-size:24px; padding-right:20px;">
          {SCENARIO_LABEL[scn].split('(')[0]}
        </div>"""
        for c, arm in enumerate(cols):
            x = c * col_w
            rows_data = cell(out_core, scn, arm)
            manifest[f"{scn}/{arm}"] = [rr["run_id"] for rr in rows_data]
            det_src = {}
            for rr in rows_data:
                k = rr["detection_source"] or "없음"
                det_src[k] = det_src.get(k, 0) + 1
            n_prevented = sum(1 for rr in rows_data if rr["outcome"] == "prevented")

            if arm == "native":
                main_txt, sub_txt = "탐지 없음", "0/5 · 개입 없음"
            else:
                label_map = {"predictive": "예측 탐지", "reactive": "반응형 탐지", "없음": "탐지 없음"}
                order = ["predictive", "reactive", "없음"]
                parts = [f"{label_map[k]} {det_src[k]}/5" for k in order if k in det_src]
                main_txt = parts[0]
                sub_txt = " · ".join(parts[1:]) if len(parts) > 1 else ""

            prevented_html = (
                f'<div style="font-family:\'DM Mono\',monospace; font-size:16px; margin-top:14px; opacity:.75;">△ SLO 위반 없음 {n_prevented}/5</div>'
                if n_prevented else ""
            )
            border_r = "border-right:1px solid var(--ink-faint);" if c < 2 else ""
            border_b = "border-bottom:1px solid var(--ink-faint);" if r < 2 else ""
            cells_html += f"""
            <div style="position:absolute; left:{x:.1f}px; top:{y:.1f}px; width:{col_w:.1f}px; height:{row_h:.1f}px;
                        {border_r} {border_b} display:flex; flex-direction:column; align-items:center; justify-content:center; text-align:center;">
              <div style="font-family:'Noto Sans KR',sans-serif; font-weight:900; font-size:34px;">{main_txt}</div>
              {f'<div style="font-family:\'Noto Sans KR\',sans-serif; font-weight:400; font-size:18px; margin-top:10px; opacity:.7;">{sub_txt}</div>' if sub_txt else ''}
              {prevented_html}
            </div>"""

    body = f"""
    <div class="content-area">
      <div style="position:relative; margin-left:260px; height:690px;">
        {col_headers}
        {cells_html}
      </div>
    </div>
    """
    title = "탐지 경로는 시나리오와 대응 방식마다 다르다"
    labtag = "PHASE 8 · CORE n=45 · §131"
    footnote = "무개입=native · 고정 임계치=fixed_threshold · Isolation Forest=proposed(예측 모델) · SLO 위반 없음=능동 예방 의미 아님 · 탐지 경로는 detection_source 기준(detector_process와 다를 수 있음)"
    html = slide_shell(body, title, labtag, footnote, "01", "PHASE 8 RESULTS — DRAFT")
    return html, manifest


# ==================================================================
# 슬라이드 2 - 탐지 시점
# ==================================================================
def build_slide2(out_core):
    import random

    rows_ordered = [(s, a) for s in SCENARIOS for a in ["fixed_threshold", "proposed"]]
    per_row = {}
    all_leads = []
    manifest = {"cells": {}}
    for scenario, arm in rows_ordered:
        rows_data = cell(out_core, scenario, arm)
        plotted = [(r, r["detection_lead_sec"]) for r in rows_data if r["detection_lead_sec"] is not None]
        excluded = [r for r in rows_data if r["detection_lead_sec"] is None]
        per_row[(scenario, arm)] = (plotted, excluded)
        all_leads.extend(v for _, v in plotted)
        manifest["cells"][f"{scenario}/{arm}"] = {
            "plotted_run_ids": [r["run_id"] for r, _ in plotted],
            "excluded_run_ids": [r["run_id"] for r in excluded],
        }

    FRAME_W = 1760  # frame 폭(1920 - edge 80*2)과 반드시 일치시켜야 함
    LEFT_GUTTER = 380   # 시나리오·방식 행 라벨
    RIGHT_GUTTER = 320  # n= / 제외 건수 표기
    CHART_W = FRAME_W - LEFT_GUTTER - RIGHT_GUTTER  # = 1060px - 나머지 전부 이 값에서 파생

    xmin, xmax = min(all_leads), max(all_leads)
    span = xmax - xmin
    plot_left, plot_right = 0, CHART_W
    domain_lo = xmin - span * 0.08
    domain_hi = xmax + span * 0.14

    def xpix(v):
        return plot_left + (v - domain_lo) / (domain_hi - domain_lo) * (plot_right - plot_left)

    n_rows = len(rows_ordered)
    row_area_top, row_area_bot = 20, 540
    row_h = (row_area_bot - row_area_top) / n_rows

    zero_x = xpix(0)

    svg_parts = []
    svg_parts.append(f'<line x1="{zero_x:.1f}" y1="0" x2="{zero_x:.1f}" y2="{row_area_bot}" stroke="{INK}" stroke-width="2.5"/>')

    rng = random.Random(20260925)
    row_labels_html = ""
    right_notes_html = ""
    for i, (scenario, arm) in enumerate(rows_ordered):
        y_center = row_area_top + i * row_h + row_h / 2
        plotted, excluded = per_row[(scenario, arm)]
        if i > 0:
            svg_parts.append(f'<line x1="0" y1="{row_area_top + i*row_h:.1f}" x2="{plot_right}" y2="{row_area_top + i*row_h:.1f}" stroke="{INK_FAINT}" stroke-width="1"/>')
        for r, v in plotted:
            jitter = rng.uniform(-row_h * 0.22, row_h * 0.22)
            px = xpix(v)
            py = y_center + jitter
            filled = v < 0  # 위반 후(음수)=solid, 위반 전(양수)=hollow (2색 체계 - fill 상태로 부호 구분)
            fill = INK if filled else PAPER
            if arm == "fixed_threshold":
                svg_parts.append(f'<rect x="{px-9:.1f}" y="{py-9:.1f}" width="18" height="18" fill="{fill}" stroke="{INK}" stroke-width="2.2"/>')
            else:
                pts = f"{px:.1f},{py-11:.1f} {px-10:.1f},{py+8:.1f} {px+10:.1f},{py+8:.1f}"
                svg_parts.append(f'<polygon points="{pts}" fill="{fill}" stroke="{INK}" stroke-width="2.2"/>')

        label_kr = f"{SCENARIO_LABEL[scenario].split('(')[0]} · {ARM_LABEL[arm]}"
        row_labels_html += f"""
        <div style="position:absolute; left:{-LEFT_GUTTER}px; top:{y_center-18:.1f}px; width:{LEFT_GUTTER-20}px; height:36px;
                    display:flex; align-items:center; justify-content:flex-end; text-align:right;
                    font-family:'Noto Sans KR',sans-serif; font-weight:500; font-size:20px;">{label_kr}</div>"""
        n_note = f"n={len(plotted)}/5"
        if excluded:
            n_note += f" · 위반없음/미탐지 {len(excluded)}건"
        right_notes_html += f"""
        <div style="position:absolute; left:{plot_right+24}px; top:{y_center-14:.1f}px; width:{RIGHT_GUTTER-24}px; height:28px;
                    font-family:'DM Mono',monospace; font-size:15px; opacity:.8;">{n_note}</div>"""

    # 축 눈금 (DM Mono)
    ticks_html = ""
    import math
    tick_step = 100 if span > 250 else 50
    t = math.ceil(domain_lo / tick_step) * tick_step
    while t <= domain_hi:
        tx = xpix(t)
        ticks_html += f'<line x1="{tx:.1f}" y1="{row_area_bot}" x2="{tx:.1f}" y2="{row_area_bot+10}" stroke="{INK}" stroke-width="1.5"/>'
        ticks_html += f'<text x="{tx:.1f}" y="{row_area_bot+34}" font-family="DM Mono" font-size="15" fill="{INK}" text-anchor="middle">{t:+d}</text>'
        t += tick_step

    # 데이터 범위가 비대칭(양수 쪽이 훨씬 좁음)이라 구간 중점을 쓰면 0선에
    # 라벨이 붙어버린다 - 0선 기준 고정 픽셀 오프셋으로 최소 이격을 보장한다.
    zone_label_y = row_area_top - 8
    ZONE_LABEL_OFFSET = 150
    zone_labels_svg = (
        f'<text x="{max(zero_x-ZONE_LABEL_OFFSET, 90):.1f}" y="{zone_label_y}" font-family="Noto Sans KR" font-weight="700" font-size="19" fill="{INK}" text-anchor="middle">◀ 위반 후 탐지</text>'
        f'<text x="{min(zero_x+ZONE_LABEL_OFFSET, CHART_W-90):.1f}" y="{zone_label_y}" font-family="Noto Sans KR" font-weight="700" font-size="19" fill="{INK}" text-anchor="middle">위반 전 탐지 ▶</text>'
    )

    WRAPPER_H = row_area_bot + 120  # 축 캡션(+36) + 범례(+68) + 여유 - content-area 하단 예산(696) 안쪽

    body = f"""
    <div class="content-area">
    <div style="position:relative; margin-left:{LEFT_GUTTER}px; margin-right:{RIGHT_GUTTER}px; height:{WRAPPER_H}px;">
      <svg width="{CHART_W}" height="{row_area_bot}" viewBox="0 0 {CHART_W} {row_area_bot}" style="position:absolute; left:0; top:0; overflow:visible;">
        {zone_labels_svg}
        {''.join(svg_parts)}
        {ticks_html}
      </svg>
      {row_labels_html}
      {right_notes_html}
      <div style="position:absolute; left:0; top:{row_area_bot+36}px; width:{CHART_W}px; text-align:center;
                  font-family:'Noto Sans KR',sans-serif; font-weight:400; font-size:18px;">
        SLO 위반 시각 대비 탐지 시점(초)
      </div>
      <div style="position:absolute; left:0; top:{row_area_bot+68}px; width:{CHART_W}px; display:flex; justify-content:center; gap:28px;
                  font-family:'Noto Sans KR',sans-serif; font-size:14px;">
        <span>■ 고정 임계치</span><span>▲ Isolation Forest</span>
        <span style="opacity:.6;">(채움=위반 후, 빈 도형=위반 전)</span>
      </div>
    </div>
    </div>
    """
    title = "선제 탐지는 부하 램프 일부에서만 관측됨"
    labtag = "n=45 중 탐지·위반 평가 가능 subset · §131"
    footnote = "무개입(native)은 대응 자체가 없어 제외(15건) · 예측 탐지라도 음수면 위반 후 탐지 · 위반 없음/미탐지는 0으로 표기하지 않고 건수로 별도 표기"
    html = slide_shell(body, title, labtag, footnote, "02", "PHASE 8 RESULTS — DRAFT")
    return html, manifest


STAGE_SCRIPT = """
<script>
// 고정 1920x1080 스테이지를 실제 브라우저 창 크기에 맞춰 균일 스케일(html-template.md 표준 패턴)
(function(){
  var stage = document.querySelector('.deck-stage');
  function scale(){
    var factor = Math.min(window.innerWidth/1920, window.innerHeight/1080);
    var x = (window.innerWidth - 1920*factor)/2;
    var y = (window.innerHeight - 1080*factor)/2;
    stage.style.transform = 'translate('+x+'px,'+y+'px) scale('+factor+')';
  }
  scale();
  window.addEventListener('resize', scale);
})();
</script>
"""


def page(title, slide_html):
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
{FONT_LINK}
<style>{BASE_CSS}
.deck-stage{{transform-origin:0 0;}}
</style>
</head>
<body>
<div class="deck-viewport">
  <div class="deck-stage">
    <div class="slide active">
      {slide_html}
    </div>
  </div>
</div>
{STAGE_SCRIPT}
</body>
</html>"""


def main():
    core_run_ids, out_core, aux_run_ids, out_aux = load_authoritative()
    verify_against_section_131(out_core, out_aux)  # 불일치 시 AssertionError로 즉시 중단

    s1_html, s1_manifest = build_slide1(out_core)
    s2_html, s2_manifest = build_slide2(out_core)

    (OUT_DIR / "slide1-detection-path.html").write_text(page("탐지 경로", s1_html), encoding="utf-8")
    (OUT_DIR / "slide2-detection-timing.html").write_text(page("탐지 시점", s2_html), encoding="utf-8")

    manifest = {"core_n": len(core_run_ids), "slide1": s1_manifest, "slide2": s2_manifest}
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Cobalt Grid draft slides generated ->", OUT_DIR.resolve())


if __name__ == "__main__":
    main()
