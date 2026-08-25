"""DCF 엔진 — Unlevered FCF 방식 (institutional standard).

UFCF_t = EBIT_t·(1−tax) + D&A_t − Capex_t − ΔNWC_t
EV = Σ UFCF_t/(1+WACC)^t + TV/(1+WACC)^N,   TV = UFCF_N·(1+g)/(WACC−g)
주당 지분가치 = (EV − 순부채) / 발행주식수

데이터가 주는 것: 기준매출·발행주식수(DART), 세율(Damodaran).
사용자가 주는 가정(assumption): 매출성장률·영업이익률·D&A%·Capex%·ΔNWC%·terminal g·WACC·순부채.
가정은 지어내지 않고 입력받으며, 결과 셀은 전부 수식으로 Excel에 나가 flex 된다.
"""
from __future__ import annotations

from core.schema import Provenance, Value, DataError, SourceType
from providers import dart, sec, edinet, finmind, damodaran

# 시장 코드 → provider 모듈. DART만 report/prefer(IS vs CIS 선택)를 추가로 받는다.
_MARKET_PROVIDERS = {"KR": dart, "US": sec, "JP": edinet, "TW": finmind}


def _assume(desc: str) -> Provenance:
    return Provenance(source="사용자 가정(입력)", source_type=SourceType.ASSUMPTION,
                      source_url="(assumption)", note=desc)


def build_model(company: str, wacc_pct: float, net_debt: float,
                revenue_growth, ebit_margin_pct: float,
                da_pct: float, capex_pct: float, nwc_pct: float,
                terminal_growth_pct: float, forecast_years: int = 5,
                tax_rate_pct: float | None = None,
                year: int | None = None, prefer: str = "CFS",
                market: str = "KR") -> dict:
    market = (market or "KR").strip().upper()
    provider = _MARKET_PROVIDERS.get(market)
    if provider is None:
        raise DataError(f"지원하지 않는 시장: {market} (지원: {', '.join(_MARKET_PROVIDERS)})")

    if provider is dart:
        rev0 = dart.financial_item(company, "revenue", year, "annual", prefer)
        sh = dart.shares_outstanding(company, year, "annual")
    else:
        rev0 = provider.financial_item(company, "revenue", year)
        sh = provider.shares_outstanding(company, year)

    if tax_rate_pct is None:
        tax_v = damodaran.corporate_tax_rate(market)
        tax, tax_prov = tax_v.value, tax_v.provenance
    else:
        tax, tax_prov = float(tax_rate_pct), _assume("법인세율")

    if isinstance(revenue_growth, (int, float)):
        growth = [float(revenue_growth)] * int(forecast_years)
    else:
        growth = [float(g) for g in revenue_growth]
    n = len(growth)
    if n < 1:
        raise DataError("매출성장률(revenue_growth)을 1개 이상 입력하세요.")

    wacc = float(wacc_pct) / 100.0
    gt = float(terminal_growth_pct) / 100.0
    if wacc <= gt:
        raise DataError(f"WACC({wacc_pct}%)이 terminal growth({terminal_growth_pct}%)보다 커야 TV가 유효합니다.")

    m = float(ebit_margin_pct) / 100.0
    da, capex, nwc, taxr = da_pct / 100.0, capex_pct / 100.0, nwc_pct / 100.0, tax / 100.0

    base_rev, shares = rev0.value, sh.value
    rows, prev, pv_sum = [], base_rev, 0.0
    for t in range(1, n + 1):
        rev = prev * (1 + growth[t - 1] / 100.0)
        ebit = rev * m
        nopat = ebit * (1 - taxr)
        d_a, cap = rev * da, rev * capex
        dnwc = (rev - prev) * nwc
        ufcf = nopat + d_a - cap - dnwc
        df = 1 / ((1 + wacc) ** t)
        pv = ufcf * df
        pv_sum += pv
        rows.append({"t": t, "rev": rev, "ebit": ebit, "nopat": nopat, "da": d_a,
                     "capex": cap, "dnwc": dnwc, "ufcf": ufcf, "df": df, "pv": pv})
        prev = rev

    ufcf_n = rows[-1]["ufcf"]
    tv = ufcf_n * (1 + gt) / (wacc - gt)
    pv_tv = tv / ((1 + wacc) ** n)
    ev = pv_sum + pv_tv
    equity = ev - float(net_debt)
    per_share = equity / shares

    # ── 검증: 기계적으로는 값이 나오지만 밸류에이션으로 의미가 없는 조합을 경고한다 ──
    # (실측 사례: SK하이닉스에 5개년 평균 CAPEX/매출 31% + EBIT마진 14% 를 넣으면 전 연도
    #  UFCF 가 음수 → EV −66조. 숫자를 그냥 내보내면 '가치가 음수' 로 오독된다.)
    warnings: list[str] = []
    neg_years = [r["t"] for r in rows if r["ufcf"] < 0]
    if len(neg_years) == n:
        warnings.append(
            f"예측 {n}개년 전부 UFCF 가 음수 — 재투자(CAPEX {capex_pct}%)가 세후영업이익+D&A 를 "
            f"초과하는 입력입니다. 경기민감 업종에 과거 평균을 그대로 쓰면 흔히 발생하며, "
            f"이 상태의 EV/주당가치는 밸류에이션으로 해석할 수 없습니다.")
    elif neg_years:
        warnings.append(f"UFCF 가 음수인 예측연도: {neg_years} — 재투자 가정 확인 필요.")
    if ufcf_n <= 0:
        warnings.append(
            f"최종연도 UFCF 가 {ufcf_n:,.0f} (≤0) 이라 Gordon Growth 로 만든 TV 가 무의미합니다.")
    if ev <= 0:
        warnings.append(f"EV 가 음수({ev:,.0f})입니다 — 입력 가정을 재검토해야 합니다.")
    if equity <= 0:
        warnings.append(f"지분가치가 음수({equity:,.0f})입니다 — 주당가치는 의미가 없습니다(NM).")
    if ev > 0 and pv_tv / ev > 0.9:
        warnings.append(f"EV 의 {pv_tv / ev * 100:.0f}% 가 Terminal Value 입니다 "
                        f"(90% 초과) — 예측기간 가정보다 g·WACC 에 결과가 지배됩니다.")

    name = rev0.label or company
    for suffix in (" 매출액", " Revenue"):
        if suffix in name:
            name = name.split(suffix)[0]
            break

    return {
        "company": name, "market": market,
        "as_of": rev0.provenance.as_of,
        "base_revenue": rev0, "shares": sh, "tax_pct": tax, "tax_prov": tax_prov,
        "wacc_pct": float(wacc_pct), "net_debt": float(net_debt),
        "growth_pct": growth, "ebit_margin_pct": float(ebit_margin_pct),
        "da_pct": da_pct, "capex_pct": capex_pct, "nwc_pct": nwc_pct,
        "terminal_growth_pct": float(terminal_growth_pct), "forecast_years": n,
        "rows": rows, "tv": tv, "pv_tv": pv_tv, "pv_ufcf_sum": pv_sum,
        "ev": ev, "equity_value": equity, "per_share": per_share,
        "warnings": warnings, "valuation_reliable": not warnings,
    }


_CURRENCY = {"KR": "KRW", "US": "USD", "JP": "JPY", "TW": "TWD"}


def evaluate(company: str, wacc_pct: float, net_debt: float, revenue_growth,
             ebit_margin_pct: float, da_pct: float, capex_pct: float, nwc_pct: float,
             terminal_growth_pct: float, forecast_years: int = 5,
             tax_rate_pct: float | None = None, year: int | None = None,
             market: str = "KR") -> Value:
    d = build_model(company, wacc_pct, net_debt, revenue_growth, ebit_margin_pct,
                    da_pct, capex_pct, nwc_pct, terminal_growth_pct, forecast_years,
                    tax_rate_pct, year, market=market)
    f = lambda x: f"{x:,.0f}"
    cur = _CURRENCY.get(d["market"], d["market"])
    note = (
        f"[DCF · UFCF · ⚠️ 성장·마진 등은 사용자 가정] 기준매출 {f(d['base_revenue'].value)}"
        f"({d['base_revenue'].provenance.source}, {d['as_of']}), "
        f"발행주식 {d['shares'].value:,}주. WACC {d['wacc_pct']}%, terminal g {d['terminal_growth_pct']}%, "
        f"세율 {d['tax_pct']}%. 예측 {d['forecast_years']}년 → PV(UFCF) {f(d['pv_ufcf_sum'])} + PV(TV) {f(d['pv_tv'])} "
        f"= EV {f(d['ev'])}. 순부채 {f(d['net_debt'])} 차감 → 지분가치 {f(d['equity_value'])} "
        f"→ 주당 {f(d['per_share'])}{cur}."
    )
    if d["warnings"]:
        note += " ⚠️ [검증 경고] " + " | ".join(d["warnings"])
    def _computed(label: str, val: float, src_url: str, extra_note: str) -> Value:
        return Value(
            value=round(val), unit=cur, label=f"{d['company']} {label}",
            provenance=Provenance(source="계산엔진(engines.dcf)", source_type=SourceType.COMPUTED,
                                  source_url=src_url, as_of=d["as_of"], note=extra_note),
        )

    ev_value = _computed("DCF Enterprise Value", d["ev"],
                         "(computed: PV(UFCF) 합 + PV(Terminal Value))",
                         f"PV(UFCF) {f(d['pv_ufcf_sum'])} + PV(TV) {f(d['pv_tv'])} = EV {f(d['ev'])}")
    equity_value = _computed("DCF Equity Value", d["equity_value"],
                             "(computed: EV − 순부채)",
                             f"EV {f(d['ev'])} − 순부채 {f(d['net_debt'])} = 지분가치 {f(d['equity_value'])}")

    return Value(
        value=round(d["per_share"]), unit=f"{cur}/주",
        label=f"{d['company']} DCF 주당가치",
        provenance=Provenance(source="계산엔진(engines.dcf)", source_type=SourceType.COMPUTED,
                              source_url="(computed: DART 매출/주식수 + 사용자 가정 + WACC)",
                              as_of=d["as_of"], note=note),
        extras={"enterprise_value": ev_value, "equity_value": equity_value},
    )
