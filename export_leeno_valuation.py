"""리노공업 밸류에이션(상증법/WACC 바텀업베타/DCF/Comps) 결과를 Excel 4개 파일로 추출.
일회성 산출물 생성 스크립트 — SKSQ 대화형 tool 호출 결과를 워크북으로 정리한다."""
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


# ── 1) 상증법 ──────────────────────────────────────────────────────────
b, fn = exporters.sangjeung_workbook("리노공업")
_save(b, fn)

# ── 2) WACC (Bottom-up Beta, 피어 10개사) ───────────────────────────────
RF = 4.301        # 한국은행 ECOS 국고채(10Y), 2026-08-11
ERP_KR = 4.8691    # Damodaran, 2026-01
TAX_KR = 26.4
TAX_US = 25.0
TAX_JP = 29.74

PEERS = [
    dict(name="티에스이", code="131290", country="KR", beta=1.24, tax=TAX_KR,
         debt=42_800_000_000, equity=2_704_500_000_000, debt_label="총차입금(단기+유동성장기+장기)",
         beta_src="WiseReport(52주베타,주간수익률) https://comp.wisereport.co.kr/company/c1010001.aspx?cmp_cd=131290",
         debt_src="ValueLine(26.1Q) https://valueline.co.kr/finance/balancesheet/131290"),
    dict(name="ISC", code="095340", country="KR", beta=1.04, tax=TAX_KR,
         debt=85_159_590_000, equity=3_040_000_000_000, debt_label="부채총계(총차입금 확인 실패→대체)",
         beta_src="Investing.com(기간 미명시) https://www.investing.com/equities/isc-co-ltd",
         debt_src="Investing.com(FY2024) https://www.investing.com/equities/isc-co-ltd-balance-sheet"),
    dict(name="피엠티(구 마이크로프렌드)", code="147760", country="KR", beta=1.97, tax=TAX_KR,
         debt=44_591_770_000, equity=69_600_000_000, debt_label="부채총계(총차입금 확인 실패→대체)",
         beta_src="Investing.com(기간 미명시) https://kr.investing.com/equities/micro-friend-inc",
         debt_src="Investing.com(FY2025.12) https://kr.investing.com/equities/micro-friend-inc-balance-sheet"),
    dict(name="오킨스전자", code="080580", country="KR", beta=1.77, tax=TAX_KR,
         debt=33_717_000_000, equity=333_900_000_000, debt_label="총차입금(단기+장기+기타)",
         beta_src="WiseReport(52주베타) https://comp.wisereport.co.kr/company/c1010001.aspx?cmp_cd=080580",
         debt_src="stockanalysis.com https://stockanalysis.com/quote/kosdaq/080580/financials/balance-sheet/"),
    dict(name="유니테스트", code="086390", country="KR", beta=1.21, tax=TAX_KR,
         debt=4_442_240_000, equity=201_400_000_000, debt_label="Total Debt",
         beta_src="WiseReport(52주베타,주간수익률) https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd=086390",
         debt_src="Investing.com(FY2025) https://www.investing.com/equities/unitest-inc-balance-sheet"),
    dict(name="엑시콘", code="092870", country="KR", beta=2.27, tax=TAX_KR,
         debt=1_874_700_000, equity=235_300_000_000, debt_label="총차입금(전액 장기)",
         beta_src="WiseReport(52주베타,주간수익률) https://comp.wisereport.co.kr/company/c1010001.aspx?cmp_cd=092870",
         debt_src="Investing.com(FY2025.12) https://www.investing.com/equities/exicon-co-ltd-balance-sheet"),
    dict(name="네패스", code="033640", country="KR", beta=1.27, tax=TAX_KR,
         debt=214_428_750_000, equity=403_500_000_000, debt_label="Total Debt",
         beta_src="WiseReport(52주베타) https://comp.wisereport.co.kr/company/c1010001.aspx?cmp_cd=033640",
         debt_src="Investing.com(FY2024.12) https://www.investing.com/equities/nepes-corp-balance-sheet"),
    dict(name="하나마이크론", code="067310", country="KR", beta=2.07, tax=TAX_KR,
         debt=1_076_901_000_000, equity=2_100_000_000_000, debt_label="총차입금(FY2025)",
         beta_src="stockanalysis.com(5Y,월간추정) https://stockanalysis.com/quote/kosdaq/067310/statistics/",
         debt_src="stockanalysis.com https://stockanalysis.com/quote/kosdaq/067310/financials/balance-sheet/"),
    dict(name="FormFactor Inc (FORM, US)", code="FORM", country="US", beta=1.25, tax=TAX_US,
         debt=32_360_000, equity=9_460_000_000, debt_label="Total Debt(리스포함, FY2025)",
         beta_src="stockanalysis.com(5Y) https://stockanalysis.com/stocks/form/statistics/",
         debt_src="stockanalysis.com(FY2025) https://stockanalysis.com/stocks/form/financials/balance-sheet/"),
    dict(name="Advantest Corp (6857, JP)", code="6857", country="JP", beta=1.18, tax=TAX_JP,
         debt=20_192_000_000, equity=24_800_000_000_000, debt_label="Total Debt(FY2026.3)",
         beta_src="stockanalysis.com(5Y 표기,방법 불명확) https://stockanalysis.com/quote/tyo/6857/statistics/",
         debt_src="stockanalysis.com(FY2026.3) https://stockanalysis.com/quote/tyo/6857/financials/balance-sheet/"),
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
LEENO_MCAP = 5_411_000_000_000
leeno_de = LEENO_DEBT / LEENO_MCAP  # = 0

beta_relev_median = bu_median * (1 + (1 - TAX_KR / 100) * leeno_de)
beta_relev_avg = bu_avg * (1 + (1 - TAX_KR / 100) * leeno_de)

wacc = ValuationWorkbook("WACC 바텀업베타")
wb = wacc
wb.title(1, "WACC — Bottom-up Beta (반도체 부품/테스트 피어 10개사) — 리노공업",
         "⚠️ 베타·개별사 부채는 웹 공개 데이터(Investing.com/WiseReport/stockanalysis.com 등) 조사값. "
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
    wb.icell(r, 5, round(p["equity"]), _p("시가총액(웹 조사)", p["debt_src"].split(" ")[-1],
                                          source_type=SourceType.LLM_ESTIMATE),
            src_label=f"{p['name']} 시가총액")
    wb.fcell(r, 6, f"=D{r}/E{r}", fmt="0.0000")
    wb.icell(r, 7, p["tax"], _p("Damodaran", "https://pages.stern.nyu.edu/~adamodar/pc/datasets/ctryprem.xlsx",
                                source_type=SourceType.REFERENCE), fmt="0.00")
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
b_d = wb.icell(r, 2, LEENO_DEBT, _p("DART(BS, 단기+장기차입금)", "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260318000182",
                                    note="FY2025 별도, 차입금·사채 계정 전부 0"), src_label="리노공업 차입금")
r += 1
wb.put(r, 1, "리노공업 현금")
b_cash = wb.icell(r, 2, LEENO_CASH, _p("DART(BS, 현금및현금성자산)", "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260318000182",
                                       note="FY2025 별도"), src_label="리노공업 현금")
r += 1
wb.put(r, 1, "순부채(D−Cash) = Net Cash")
b_netdebt = wb.fcell(r, 2, f"={b_d}-{b_cash}", bold=True)
r += 1
wb.put(r, 1, "리노공업 시가총액(E)")
b_e = wb.icell(r, 2, LEENO_MCAP, _p("네이버 금융(KRX 시세)", "https://m.stock.naver.com/domestic/stock/058470/total"),
              src_label="리노공업 시가총액")
r += 1
wb.put(r, 1, "리노공업 D/(D+E)")
b_dv = wb.fcell(r, 2, f"={b_d}/({b_d}+{b_e})", fmt="0.0000")
r += 2
wb.put(r, 1, "리레버드 베타 (중앙값 Bu 기준)", bold=True)
b_beta_med = wb.fcell(r, 2, f"=H{med_row}*(1+(1-26.4/100)*{b_dv})", fmt="0.0000", bold=True)
beta_med_row = r
r += 1
wb.put(r, 1, "리레버드 베타 (평균 Bu 기준, 참고)")
b_beta_avg = wb.fcell(r, 2, f"=H{avg_row}*(1+(1-26.4/100)*{b_dv})", fmt="0.0000")
r += 2
wb.section(r, "CAPM → WACC")
r += 1
wb.put(r, 1, "무위험수익률 Rf (KR 10Y)")
b_rf = wb.icell(r, 2, RF, _p("한국은행 ECOS", "https://ecos.bok.or.kr/", as_of="2026-08-11"),
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
r += 2
wb.put(r, 1, "[참고] 평균 베타 기준 WACC")
b_ke_avg = wb.fcell(r, 2, f"={b_rf}+{b_beta_avg}*{b_erp}", fmt="0.00")

r += 2
wb.note(r, "리노공업은 차입금 0(FY2025, 별도) — 순현금 852억원. D/E≈0이라 리레버 베타=언레버드 베타와 거의 동일.")
r += 1
wb.note(r, "피어 베타는 소스마다 산출기간·방법(주간/월간, 1년/52주/5년)이 달라 값이 상당히 갈림"
           "(예: 오킨스전자 0.72~1.77, 엑시콘 1.71~2.27, 네패스 1.27~2.1). 중앙값을 1차 채택, 평균은 참고용.")
r += 1
wb.note(r, "ISC·피엠티는 개별 이자부담부채 확인 실패 → 부채총계로 대체(레버리지 과대추정 가능성 있음).")

_save(wb.to_bytes(), "WACC_바텀업베타_리노공업.xlsx")

# ── 3) DCF ───────────────────────────────────────────────────────────
b, fn = exporters.dcf_workbook(
    "리노공업", wacc_pct=10.32, net_debt=-85_202_812_409,
    revenue_growth=7.35, ebit_margin_pct=45.65, da_pct=4.60,
    capex_pct=15.69, nwc_pct=34.87, terminal_growth_pct=1.0,
    market="KR",
)
_save(b, fn)

# ── 4) Comps ───────────────────────────────────────────────────────────
b, fn = exporters.comps_workbook(
    "리노공업",
    ["티에스이", "ISC", "피엠티", "오킨스전자", "유니테스트", "엑시콘", "네패스", "하나마이크론"],
)
_save(b, fn)

print("DONE")
