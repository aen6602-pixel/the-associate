"""DCF 엔진 — Unlevered FCF 방식 (institutional standard).

UFCF_t = EBIT_t·(1−tax) + D&A_t − Capex_t − ΔNWC_t
EV = Σ UFCF_t/(1+WACC)^t + TV/(1+WACC)^N,   TV = UFCF_N·(1+g)/(WACC−g)
주당 지분가치 = (EV − 순부채) / 발행주식수

데이터가 주는 것: 기준매출·발행주식수(DART), 세율(Damodaran).
사용자가 주는 가정(assumption): 매출성장률·영업이익률·D&A%·Capex%·ΔNWC%·terminal g·WACC·순부채.
가정은 지어내지 않고 입력받으며, 결과 셀은 전부 수식으로 Excel에 나가 flex 된다.
"""
from __future__ import annotations

from core import runid
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
                market: str = "KR", allow_mixed: bool = False,
                terminal_tax_rate_pct: float | None = None) -> dict:
    market = (market or "KR").strip().upper()
    provider = _MARKET_PROVIDERS.get(market)
    if provider is None:
        raise DataError(f"지원하지 않는 시장: {market} (지원: {', '.join(_MARKET_PROVIDERS)})")

    if provider is dart:
        rev0 = dart.financial_item(company, "revenue", year, "annual", prefer)
        # ⚠️ 주당가치의 분모는 **유통주식수**다(자기주식 제외). 기본값 'issued'(발행주식총수
        # = 유통 + 자기주식)를 쓰면 자기주식만큼 주당가치가 낮게 나오고, 같은 답변 안의
        # 시가총액 역산(거래소 기준=유통 보통주)과 분모가 어긋난다.
        # 실측(기아 FY2025): issued 397,672,632 vs outstanding 390,413,249 — 1.86% 괴리를
        # "기준 차이가 있다"고 고지만 하고 넘어갔었다. 주당가치가 최종 산출물인데 분모가
        # 미정인 상태였다.
        sh = dart.shares_outstanding(company, year, "annual", basis="outstanding")
    else:
        rev0 = provider.financial_item(company, "revenue", year)
        sh = provider.shares_outstanding(company, year)

    # 세율은 두 개다 — 예측기간은 회사가 실제로 내는 유효세율, 계속가치는 법정 한계세율.
    # 한계세율 하나로 5년을 다 돌리면 공제·감면이 큰 기업의 FCFF 가 과소평가된다
    # (실측 지적: 한계 26.4% 를 쓰면서 산업 실효 13.91% 가 나란히 조회됐다).
    marginal_v = damodaran.corporate_tax_rate(market)
    if tax_rate_pct is not None:
        tax, tax_prov = float(tax_rate_pct), _assume("법인세율(예측기간)")
    else:
        tax, tax_prov = marginal_v.value, marginal_v.provenance
        if market.upper() == "KR":
            try:
                from engines import dcf_inputs

                eff = dcf_inputs.effective_tax_rate(company, 3, year, "annual", prefer)
                tax, tax_prov = eff.value, eff.provenance
            except Exception:  # noqa: BLE001 — 못 구하면 한계세율로 간다(그 사실은 note 에)
                pass
    if terminal_tax_rate_pct is not None:
        term_tax, term_tax_prov = float(terminal_tax_rate_pct), _assume("법인세율(계속가치)")
    else:
        term_tax, term_tax_prov = marginal_v.value, marginal_v.provenance

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

    # ── 게이트: 캡티브 금융·순수 금융회사에는 단일 FCFF DCF 를 적용하지 않는다 ──
    # 연결 IBD·운전자본·부채비중에 금융부문이 섞여 WACC 과대 + EV 과다차감의 이중 왜곡이
    # 발생한다(현대자동차 실측: 주당 −5,042,055원). 값을 내보내는 것보다 막는 게 정직하다.
    if market.upper() == "KR" and not allow_mixed:
        try:
            from engines import business_mix

            mix = business_mix.classify(company, year, "CFS", deep=False)
        except Exception:  # noqa: BLE001 — 판정 실패로 계산을 막지는 않는다
            mix = None
        if mix and not mix["single_dcf_ok"]:
            raise DataError(
                f"{mix['company']}: 단일 FCFF DCF 를 적용할 수 없습니다. {mix['reason']} "
                + ("근거: " + " / ".join(mix["evidence"]) + " " if mix["evidence"] else "")
                + ("[대안] 제조부문 세그먼트 재무로 DCF 를 돌리고 금융부문은 P/B 또는 "
                   "잔여이익으로 별도 평가해 합산하는 SOTP 를 쓰세요. 금융부문 순부채를 "
                   "제외한 제조부문 순부채를 net_debt 으로 넘기고, 그래도 단일 DCF 를 "
                   "강행하려면 allow_mixed=True 를 명시해야 합니다(그 경우 결과에 "
                   "이중 왜곡 경고가 붙습니다)."
                   if mix["kind"] == "mixed" else
                   "[대안] 순수 금융회사는 FCFF·EV 개념이 성립하지 않습니다 — P/B·잔여이익·"
                   "배당할인 또는 compute_comps 의 자기자본배수를 쓰세요."))

    # 계속가치의 기준 UFCF 는 **한계세율로 다시 계산**한다. 예측기간의 유효세율(공제·감면
    # 반영)이 영구히 이어진다고 보는 것은 근거가 없다 — 영구 구간은 법정세율에 수렴한다.
    _last = rows[-1]
    term_taxr = term_tax / 100.0
    ufcf_n = (_last["ebit"] * (1 - term_taxr) + _last["da"] - _last["capex"] - _last["dnwc"])
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
    # 우선주가 있으면 지분가치는 보통주와 우선주가 나눠 갖는다. 분모에 둘 다 넣으면
    # 결과는 "보통주 1주" 가 아니라 혼합 기준이 된다 — 삼성전자·현대차처럼 우선주 비중이
    # 큰 종목에서 이 차이가 material 하므로 숫자와 함께 반드시 알린다.
    pref = (sh.extras or {}).get("preferred_outstanding")
    if pref is not None and pref.value:
        warnings.append(
            f"우선주 {pref.value:,}주가 분모에 포함돼 있어 이 주당가치는 보통주 전용이 아니라 "
            f"보통주+우선주 혼합 기준입니다. 우선주는 통상 보통주 대비 할인 거래되므로 "
            f"보통주 목표가로 그대로 쓰지 마세요(별도 배분 필요).")
    if allow_mixed:
        warnings.append(
            "allow_mixed=True 로 금융부문 게이트를 우회했습니다 — WACC 의 부채비중과 EV 의 "
            "순부채 차감에 금융부문이 섞여 있어 주당가치를 그대로 인용해서는 안 됩니다.")

    # ── 봉인: 여기 걸리면 주당가치를 **숫자로 내보내지 않는다**(NM) ──────────
    # 지금까지는 LLM 이 사후에 걸러내는 구조였는데, 그건 프롬프트 변화에 취약하다.
    # 아래 조건은 "기계적으로는 값이 나오지만 밸류에이션으로는 의미가 없는" 상태다.
    blocking: list[str] = []
    if len(neg_years) == n:
        blocking.append(f"예측 {n}개년 UFCF 전부 음수")
    if ufcf_n <= 0:
        blocking.append("최종연도 UFCF ≤ 0 (Gordon Growth TV 무의미)")
    if ev <= 0:
        blocking.append(f"EV 음수({ev:,.0f})")
    if equity <= 0:
        blocking.append(f"지분가치 음수({equity:,.0f})")

    name = rev0.label or company
    for suffix in (" 매출액", " Revenue"):
        if suffix in name:
            name = name.split(suffix)[0]
            break

    return {
        "company": name, "market": market,
        "as_of": rev0.provenance.as_of,
        "base_revenue": rev0, "shares": sh, "tax_pct": tax, "tax_prov": tax_prov,
        "terminal_tax_pct": term_tax, "terminal_tax_prov": term_tax_prov,
        "terminal_ufcf": ufcf_n,
        "wacc_pct": float(wacc_pct), "net_debt": float(net_debt),
        "growth_pct": growth, "ebit_margin_pct": float(ebit_margin_pct),
        "da_pct": da_pct, "capex_pct": capex_pct, "nwc_pct": nwc_pct,
        "terminal_growth_pct": float(terminal_growth_pct), "forecast_years": n,
        "rows": rows, "tv": tv, "pv_tv": pv_tv, "pv_ufcf_sum": pv_sum,
        "ev": ev, "equity_value": equity, "per_share": per_share,
        "warnings": warnings, "valuation_reliable": not warnings,
        "blocking": blocking, "per_share_is_nm": bool(blocking),
        "allow_mixed": bool(allow_mixed),
    }


_CURRENCY = {"KR": "KRW", "US": "USD", "JP": "JPY", "TW": "TWD"}


def evaluate(company: str, wacc_pct: float, net_debt: float, revenue_growth,
             ebit_margin_pct: float, da_pct: float, capex_pct: float, nwc_pct: float,
             terminal_growth_pct: float, forecast_years: int = 5,
             tax_rate_pct: float | None = None, year: int | None = None,
             market: str = "KR", allow_mixed: bool = False,
             terminal_tax_rate_pct: float | None = None) -> Value:
    d = build_model(company, wacc_pct, net_debt, revenue_growth, ebit_margin_pct,
                    da_pct, capex_pct, nwc_pct, terminal_growth_pct, forecast_years,
                    tax_rate_pct, year, market=market, allow_mixed=allow_mixed,
                    terminal_tax_rate_pct=terminal_tax_rate_pct)
    # 재현성 각인 — 같은 입력이면 같은 run_id 가 나오므로 재실행이 곧 검증이 된다.
    # (에이전트가 "재현 가능한 compute_dcf 결과가 없어 철회한다" 고 말한 사례 대응)
    run = runid.stamp("dcf", {
        "company": company, "market": market, "wacc_pct": wacc_pct,
        "net_debt": net_debt, "revenue_growth": revenue_growth,
        "ebit_margin_pct": ebit_margin_pct, "da_pct": da_pct, "capex_pct": capex_pct,
        "nwc_pct": nwc_pct, "terminal_growth_pct": terminal_growth_pct,
        "forecast_years": forecast_years, "tax_rate_pct": tax_rate_pct,
        "year": year, "allow_mixed": allow_mixed,
        "terminal_tax_rate_pct": terminal_tax_rate_pct})
    d["run"] = run
    f = lambda x: f"{x:,.0f}"
    cur = _CURRENCY.get(d["market"], d["market"])

    # ── 기준일(as-of) 정렬 요약 (P1-3) ─────────────────────────────────
    # 하나의 산출물에 FY 재무 / 최근 시세 / Damodaran 연간 데이터셋이 섞인다. 예전에는
    # 이 불일치가 note 맨 뒤에 묻혀 있었는데, 기준일은 결과 해석을 바꾸는 정보라 앞에 둔다.
    _asof = [("기준매출", d["base_revenue"].provenance.as_of),
             ("발행주식수", d["shares"].provenance.as_of),
             ("세율", d["tax_prov"].as_of if d.get("tax_prov") else None)]
    _asof = [(k, v) for k, v in _asof if v]
    _mixed = len({v for _, v in _asof}) > 1
    asof_line = ("[기준일] " + ", ".join(f"{k} {v}" for k, v in _asof)
                 + (" ⚠️ 기준일이 서로 다릅니다 — WACC·순부채의 기준일까지 포함해 "
                    "Valuation Date 를 하나로 정하고 이탈 항목을 보고서 상단에 밝히세요."
                    if _mixed else "") + " ")

    note = (
        asof_line +
        f"[DCF · UFCF · ⚠️ 성장·마진 등은 사용자 가정] 기준매출 {f(d['base_revenue'].value)}"
        f"({d['base_revenue'].provenance.source}, {d['as_of']}), "
        f"유통주식 {d['shares'].value:,}주(자기주식 제외). "
        f"WACC {d['wacc_pct']}%, terminal g {d['terminal_growth_pct']}%, "
        f"세율 예측기간 {d['tax_pct']}%({d['tax_prov'].source}) · "
        f"계속가치 {d['terminal_tax_pct']}%(한계세율). 예측 {d['forecast_years']}년 → PV(UFCF) {f(d['pv_ufcf_sum'])} + PV(TV) {f(d['pv_tv'])} "
        f"= EV {f(d['ev'])}. 순부채 {f(d['net_debt'])} 차감 → 지분가치 {f(d['equity_value'])} "
        f"→ 주당 {f(d['per_share'])}{cur}."
    )
    if d["warnings"]:
        note += " ⚠️ [검증 경고] " + " | ".join(d["warnings"])
    # 재현 각인은 맨 뒤 — 앞자리는 기준일·NM 처럼 해석을 바꾸는 정보의 몫이다.
    note += f" [{runid.line(run)}]"
    if d["per_share_is_nm"]:
        note = ("⛔ [산출 불가 · NM] " + ", ".join(d["blocking"])
                + " → 이 입력 조합의 주당가치는 밸류에이션으로 해석할 수 없어 값을 반환하지 "
                  "않습니다(EV·지분가치는 진단용으로만 extras 에 남깁니다). " + note)
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
        value=None if d["per_share_is_nm"] else round(d["per_share"]),
        unit=f"{cur}/주",
        label=(f"{d['company']} DCF 주당가치 (산출 불가·NM)" if d["per_share_is_nm"]
               else f"{d['company']} DCF 주당가치"),
        provenance=Provenance(source="계산엔진(engines.dcf)", source_type=SourceType.COMPUTED,
                              source_url="(computed: DART 매출/주식수 + 사용자 가정 + WACC)",
                              as_of=d["as_of"], note=note),
        extras={"enterprise_value": ev_value, "equity_value": equity_value},
    )
