"""리노공업 DCF Valuation (10개년 예측, WACC=피어10개 Bottom-up Beta) 결과를 Excel 2개 파일로 추출.
일회성 산출물 생성 스크립트 — SKSQ 대화형 tool 호출 + WebSearch 조사 결과를 워크북으로 정리한다.
데이터 기준일 2026-08-12."""
from __future__ import annotations

import os

from core.schema import Provenance, SourceType
from excel.workbook import ValuationWorkbook
from excel import exporters

OUT_DIR = os.path.join(os.path.dirname(__file__), "output")
os.makedirs(OUT_DIR, exist_ok=True)


def _save(bytes_, fname):
    path = os.path.join(OUT_DIR, fname)
    with open(path, "wb") as f:
        f.write(bytes_)
    print("saved:", path)


def _p(source, url, note=None, as_of=None, source_type=SourceType.AUTHORITATIVE):
    return Provenance(source=source, source_type=source_type, source_url=url,
                      note=note, as_of=as_of)


# ── 매크로 입력 (에이전트 tool_cli 로 조회, 2026-08-12) ─────────────────
RF = 4.294          # 한국은행 ECOS 국고채(10Y), 2026-08-12
ERP_KR = 4.8691      # Damodaran, 2026-01
TAX_KR = 26.4        # Damodaran
TAX_US = 25.0
TAX_JP = 29.74

# ── WACC: 피어 10개사 Bottom-up Beta (2026-08-12 웹서치 조사, 시총은 네이버 KRX 실측) ──
PEERS = [
    dict(name="티에스이", code="131290", country="KR", beta=1.87, tax=TAX_KR,
         debt=42_800_000_000, equity=2_765_400_000_000, debt_label="차입금(유동+비유동, DART FY2025 연결)",
         beta_src="stockanalysis.com(Beta 5Y) https://stockanalysis.com/quote/kosdaq/131290/statistics/",
         debt_src="DART 사업보고서(연결) https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260318000738",
         mcap_src="네이버 금융(KRX 시세, 2026-08-12) https://m.stock.naver.com/domestic/stock/131290/total"),
    dict(name="ISC", code="095340", country="KR", beta=1.18, tax=TAX_KR,
         debt=20_000_000_000, equity=3_162_600_000_000, debt_label="교환사채(DART FY2025 연결, 차입금 0)",
         beta_src="stockanalysis.com(Beta 5Y) https://stockanalysis.com/quote/kosdaq/095340/statistics/ "
                  "(⚠️TradingView는 0.36으로 상이 — 교차검증 필요)",
         debt_src="DART 사업보고서(연결) https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260318000018",
         mcap_src="네이버 금융(KRX 시세, 2026-08-12) https://m.stock.naver.com/domestic/stock/095340/total"),
    dict(name="피엠티(구 마이크로프렌드)", code="147760", country="KR", beta=2.10, tax=TAX_KR,
         debt=35_840_000_000, equity=71_500_000_000, debt_label="차입금(유동+비유동, DART FY2025 별도)",
         beta_src="stockanalysis.com(Beta 5Y) https://stockanalysis.com/quote/kosdaq/147760/statistics/",
         debt_src="DART 사업보고서(별도) https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260316001657",
         mcap_src="네이버 금융(KRX 시세, 2026-08-12) https://m.stock.naver.com/domestic/stock/147760/total"),
    dict(name="오킨스전자", code="080580", country="KR", beta=0.72, tax=TAX_KR,
         debt=29_690_000_000, equity=334_100_000_000, debt_label="총차입금(단기+유동성장기+장기, 2026 1Q)",
         beta_src="investing.com(5Y월간 추정) https://www.investing.com/equities/okins-electronics-co-ltd",
         debt_src="ValueLine(2026 1Q) https://valueline.co.kr/finance/balancesheet/080580",
         mcap_src="네이버 금융(KRX 시세, 2026-08-12) https://m.stock.naver.com/domestic/stock/080580/total"),
    dict(name="유니테스트", code="086390", country="KR", beta=1.28, tax=TAX_KR,
         debt=50_430_000_000, equity=206_700_000_000, debt_label="총차입금(2026 1Q)",
         beta_src="investing.com(5Y월간 추정) https://www.investing.com/equities/unitest-inc",
         debt_src="ValueLine(2026 1Q) https://valueline.co.kr/finance/balancesheet/086390",
         mcap_src="네이버 금융(KRX 시세, 2026-08-12) https://m.stock.naver.com/domestic/stock/086390/total"),
    dict(name="엑시콘", code="092870", country="KR", beta=1.71, tax=TAX_KR,
         debt=16_430_000_000, equity=248_200_000_000, debt_label="총차입금(2026 1Q)",
         beta_src="investing.com(5Y월간 추정) https://www.investing.com/equities/exicon-co-ltd",
         debt_src="ValueLine(2026 1Q) https://valueline.co.kr/finance/balancesheet/092870",
         mcap_src="네이버 금융(KRX 시세, 2026-08-12) https://m.stock.naver.com/domestic/stock/092870/total"),
    dict(name="네패스", code="033640", country="KR", beta=2.24, tax=TAX_KR,
         debt=335_576_000_000, equity=422_000_000_000, debt_label="총차입금(단기+유동성장기+장기+리스, TTM 2026-03-31)",
         beta_src="stockanalysis.com(Beta 5Y) https://stockanalysis.com/quote/kosdaq/033640/statistics/ "
                  "(교차확인: investing.com 2.1)",
         debt_src="stockanalysis.com(TTM) https://stockanalysis.com/quote/kosdaq/033640/financials/balance-sheet/",
         mcap_src="네이버 금융(KRX 시세, 2026-08-12) https://m.stock.naver.com/domestic/stock/033640/total"),
    dict(name="하나마이크론", code="067310", country="KR", beta=2.07, tax=TAX_KR,
         debt=1_076_901_000_000, equity=2_357_100_000_000, debt_label="총차입금(FY2025 연말)",
         beta_src="stockanalysis.com(Beta 5Y) https://stockanalysis.com/quote/kosdaq/067310/statistics/ "
                  "(교차확인: investing.com 1.95)",
         debt_src="stockanalysis.com(FY2025) https://stockanalysis.com/quote/kosdaq/067310/financials/balance-sheet/",
         mcap_src="네이버 금융(KRX 시세, 2026-08-12) https://m.stock.naver.com/domestic/stock/067310/total"),
    dict(name="FormFactor Inc (FORM, US)", code="FORM", country="US", beta=1.25, tax=TAX_US,
         debt=11_644_000, equity=7_780_000_000, debt_label="장기부채(유동+비유동, 리스제외, SEC 10-Q FY26 2Q)",
         beta_src="stockanalysis.com(Beta 5Y) https://stockanalysis.com/stocks/form/statistics/",
         debt_src="SEC EDGAR 10-Q(2026-06-27) https://www.sec.gov/Archives/edgar/data/0001039399/000103939926000033/form-20260627.htm",
         mcap_src="stockanalysis.com(2026-07-27, $7.78B) https://stockanalysis.com/stocks/form/market-cap/"),
    dict(name="Advantest Corp (6857, JP)", code="6857", country="JP", beta=1.18, tax=TAX_JP,
         debt=88_930_000_000, equity=24_410_000_000_000, debt_label="社債(전환사채형 신주예약권부, 리스제외, 직전분기 사채0→신규발행)",
         beta_src="stockanalysis.com(Beta 5Y) https://stockanalysis.com/quote/tyo/6857/statistics/",
         debt_src="Advantest 決算短信(2026-06-30, IFRS연결) "
                  "https://advantest.com/ja/news/2026/qnpuno0000000cnr-att/J_FR_FY2026_1Q.pdf",
         mcap_src="TradingView(2026-08 초, 24.41조엔) https://www.tradingview.com/symbols/TSE-6857/"),
]

for p in PEERS:
    de = p["debt"] / p["equity"]
    p["de"] = de
    p["bu"] = p["beta"] / (1 + (1 - p["tax"] / 100) * de)

bu_list = [p["bu"] for p in PEERS]
bu_avg = sum(bu_list) / len(bu_list)
bu_sorted = sorted(bu_list)
n = len(bu_sorted)
bu_median = (bu_sorted[n // 2 - 1] + bu_sorted[n // 2]) / 2 if n % 2 == 0 else bu_sorted[n // 2]

LEENO_DEBT = 0
LEENO_CASH = 85_202_812_409
LEENO_MCAP = 5_441_500_000_000  # 네이버 금융(KRX 시세, 2026-08-12)
leeno_de = LEENO_DEBT / LEENO_MCAP  # = 0

beta_relev_median = bu_median * (1 + (1 - TAX_KR / 100) * leeno_de)
beta_relev_avg = bu_avg * (1 + (1 - TAX_KR / 100) * leeno_de)

wacc_wb = ValuationWorkbook("WACC 바텀업베타")
wb = wacc_wb
wb.title(1, "WACC — Bottom-up Beta (반도체 부품/테스트 피어 10개사) — 리노공업 (2026-08-12 기준)",
         "⚠️ 베타·개별사 부채는 웹 공개 데이터(stockanalysis.com/investing.com/DART/SEC/ValueLine 등) 조사값. "
         "출처별 산출기간·방법이 상이해 참고용(peer별 출처는 아래 표 및 '출처' 시트 참고).")

wb.section(3, "피어 테이블 (레버드 베타 → Hamada 언레버 → 평균/중앙값)")
hdr = 4
cols = ["회사", "국가", "레버드 베타(β)", "총차입금(D)", "시가총액(E)", "D/E", "법인세율(%)", "언레버드 베타(Bu)"]
for c, t in enumerate(cols, 1):
    wb.put(hdr, c, t, bold=True)
r = hdr + 1
first = r
for p in PEERS:
    wb.put(r, 1, p["name"])
    wb.put(r, 2, p["country"])
    wb.icell(r, 3, p["beta"], _p(p["beta_src"].split(" ")[0], p["beta_src"].split(" ")[-1],
                                 note=f"{p['debt_label']} 참고", source_type=SourceType.LLM_ESTIMATE),
            fmt="0.00", src_label=f"{p['name']} 베타")
    wb.icell(r, 4, round(p["debt"]), _p(p["debt_src"].split(" ")[0], p["debt_src"].split(" ")[-1],
                                        note=p["debt_label"], source_type=SourceType.LLM_ESTIMATE),
            src_label=f"{p['name']} 총차입금")
    wb.icell(r, 5, round(p["equity"]), _p(p["mcap_src"].split(" ")[0], p["mcap_src"].split(" ")[-1],
                                          note="시가총액", source_type=(
                                              SourceType.AUTHORITATIVE if p["country"] == "KR"
                                              else SourceType.LLM_ESTIMATE)),
            src_label=f"{p['name']} 시가총액")
    wb.fcell(r, 6, f"=D{r}/E{r}", fmt="0.0000")
    wb.icell(r, 7, p["tax"], _p("Damodaran", "https://pages.stern.nyu.edu/~adamodar/pc/datasets/ctryprem.xlsx",
                                as_of="2026-01", source_type=SourceType.REFERENCE), fmt="0.00")
    wb.fcell(r, 8, f"=C{r}/(1+(1-G{r}/100)*F{r})", fmt="0.0000", bold=True)
    r += 1
last = r - 1
wb.put(r, 1, "평균(Average)", bold=True)
wb.fcell(r, 8, f"=AVERAGE(H{first}:H{last})", fmt="0.0000", bold=True)
avg_row = r
r += 1
wb.put(r, 1, "중앙값(Median)", bold=True)
wb.fcell(r, 8, f"=MEDIAN(H{first}:H{last})", fmt="0.0000", bold=True)
med_row = r

r += 2
wb.section(r, "리노공업 리레버 & WACC (CAPM)")
r += 1
wb.put(r, 1, "리노공업 총차입금(D)")
b_d = wb.icell(r, 2, LEENO_DEBT, _p("DART(BS, 단기+장기차입금)", "https://dart.fss.or.kr/dsaf001/main.do",
                                    note="FY2025 별도, 차입금·사채 계정 전부 0"), src_label="리노공업 차입금")
r += 1
wb.put(r, 1, "리노공업 현금")
b_cash = wb.icell(r, 2, LEENO_CASH, _p("DART(BS, 현금및현금성자산)", "https://dart.fss.or.kr/dsaf001/main.do",
                                       note="FY2025 별도"), src_label="리노공업 현금")
r += 1
wb.put(r, 1, "순부채(D−Cash) = Net Cash")
b_netdebt = wb.fcell(r, 2, f"={b_d}-{b_cash}", bold=True)
r += 1
wb.put(r, 1, "리노공업 시가총액(E)")
b_e = wb.icell(r, 2, LEENO_MCAP, _p("네이버 금융(KRX 시세)", "https://m.stock.naver.com/domestic/stock/058470/total",
                                    as_of="2026-08-12"), src_label="리노공업 시가총액")
r += 1
wb.put(r, 1, "리노공업 D/(D+E)")
b_dv = wb.fcell(r, 2, f"={b_d}/({b_d}+{b_e})", fmt="0.0000")
r += 2
wb.put(r, 1, "리레버드 베타 (중앙값 Bu 기준)", bold=True)
b_beta_med = wb.fcell(r, 2, f"=H{med_row}*(1+(1-{TAX_KR}/100)*{b_dv})", fmt="0.0000", bold=True)
beta_med_row = r
r += 1
wb.put(r, 1, "리레버드 베타 (평균 Bu 기준, 참고)")
b_beta_avg = wb.fcell(r, 2, f"=H{avg_row}*(1+(1-{TAX_KR}/100)*{b_dv})", fmt="0.0000")
r += 2
wb.section(r, "CAPM → WACC")
r += 1
wb.put(r, 1, "무위험수익률 Rf (KR 10Y)")
b_rf = wb.icell(r, 2, RF, _p("한국은행 ECOS", "https://ecos.bok.or.kr/", as_of="2026-08-12"),
               fmt="0.000", src_label="Rf")
r += 1
wb.put(r, 1, "ERP (Korea)")
b_erp = wb.icell(r, 2, ERP_KR, _p("Damodaran", "https://pages.stern.nyu.edu/~adamodar/pc/datasets/ctryprem.xlsx",
                                  as_of="2026-01", source_type=SourceType.REFERENCE),
                 fmt="0.0000", src_label="ERP")
r += 1
wb.put(r, 1, "Ke = Rf + β×ERP (중앙값 베타)", bold=True)
b_ke = wb.fcell(r, 2, f"={b_rf}+{b_beta_med}*{b_erp}", fmt="0.00", bold=True)
r += 1
wb.put(r, 1, "▶ WACC (= Ke, D/V≈0)", bold=True)
wb.fcell(r, 2, f"={b_ke}", fmt="0.00", bold=True)
wacc_result_row = r
r += 2
wb.put(r, 1, "[참고] 평균 베타 기준 WACC")
b_ke_avg = wb.fcell(r, 2, f"={b_rf}+{b_beta_avg}*{b_erp}", fmt="0.00")

r += 2
wb.note(r, "리노공업은 차입금 0(FY2025, 별도) — 순현금 852억원. D/E≈0이라 리레버 베타=언레버드 베타와 거의 동일.")
r += 1
wb.note(r, "⚠️ ISC 베타는 stockanalysis.com 1.18 vs TradingView 0.36 로 소스간 상당한 차이 확인됨 — "
           "stockanalysis.com(5Y) 채택, 교차검증 권장.")
r += 1
wb.note(r, "Advantest는 2026-06 1Q에 처음 사채 889억엔을 신규발행(직전 0원)해 부채구조가 급변한 시점 — "
           "D/E가 구조적으로 낮아 WACC 영향은 미미하나 참고.")
r += 1
wb.note(r, "FormFactor·Advantest 시가총액은 stockanalysis.com/TradingView 웹조사값(llm_estimate) — "
           "KR 8개사는 네이버 금융(KRX 시세, authoritative) 실측.")

_save(wb.to_bytes(), "WACC_바텀업베타_리노공업_10Y.xlsx")

WACC_PCT = round(beta_relev_median * ERP_KR + RF, 2)
print(f"\n>>> WACC (median beta) = {WACC_PCT}%  [relev_beta_median={beta_relev_median:.4f}]")
print(f">>> WACC (avg beta, 참고) = {round(beta_relev_avg * ERP_KR + RF, 2)}%")

# ── DCF 전체모델 (10개년 예측, 5시트: P&L and FCF / Debt Schedule / Depreciation Schedule / ──
# ── DCF Valuation(민감도 포함) / Assumption Summary) ─────────────────────────────────────
# 주요 가정(성장률/영업이익률/CAPEX율)은 과거 5개년(2021~2025, DART 실측) 평균으로 산출:
#   매출성장률 5개년 평균 = 9.28% (YoY 4개 구간 평균: 15.08/-20.73/8.85/33.91)
#   영업이익률 5개년 평균 = 44.22% (연도별: 41.80/42.38/44.75/44.65/47.51)
#   CAPEX/매출 5개년 평균 = 13.77% (연도별: 16.10/5.66/26.80/4.18/16.09)
#   ΔNWC: 리노공업은 DART 현금흐름표에 운전자본변동 구조화 데이터가 없어(cf_extras_nyear 전부 null)
#          5개년 평균 산출 불가 → 엔진 기본값 3.0% 사용(가정으로 명시, Assumption Summary 시트에 표기)
b, fn = exporters.dcf_full_workbook(
    "리노공업",
    wacc_pct=WACC_PCT,
    net_debt=0,  # 미사용 파라미터(실제 순부채는 DART 실측 차입금-현금으로 내부 계산됨)
    revenue_growth_pct=9.28,
    ebit_margin_pct=44.22,
    da_pct=0,  # 미사용 파라미터(D&A는 Depreciation Schedule 시트에서 실측 PP&E 기반 산출)
    capex_pct=13.77,
    nwc_pct=3.0,
    terminal_growth_pct=1.0,
    forecast_years=10,
    tax_rate_pct=None,  # Damodaran 한국 법인세율(26.4%) 자동 적용
    fcff_mode="raw",
)
_save(b, fn)

print("DONE")
