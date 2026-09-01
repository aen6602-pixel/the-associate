"""DCF 전체모델 엔진 — 5시트 워크북(P&L and FCF / Debt Schedule / Depreciation Schedule /
DCF Valuation / Assumption Summary)의 데이터를 만든다.

기존 engines/dcf.py(간단 UFCF, compute_dcf 챗 tool 이 계속 사용)는 그대로 두고, 이 모듈은
그 계산을 5개년 실데이터 히스토리 + Debt/Depreciation Schedule 로 확장한 버전이다.

원칙: DART 에서 실제로 가져올 수 있는 숫자(5개년 매출·매출원가·판관비·이자비용·법인세비용·
순이익·PPE·차입금·현금·CAPEX·OCF·NWC변동, 발행주식수)는 실데이터로. 구조화 데이터가 없는
항목(개별 대출 금리, 자산별 상각내역, 상환 스케줄)만 사용자 조정 가능한 가정으로 — 가정의
기본값도 가능하면 실데이터에서 역산한다(예: 이자율 기본값 = 실제 이자비용 ÷ 평균 차입금).
"""
from __future__ import annotations

from core.schema import DataError
from providers import dart, damodaran

N_HIST = 5


# ── 가정 기본값 역산 ─────────────────────────────────────────────
def _avg_ratio(nums: list, dens: list) -> float | None:
    pairs = [(n, d) for n, d in zip(nums, dens) if n is not None and d not in (None, 0)]
    if not pairs:
        return None
    return sum(n / d for n, d in pairs) / len(pairs)


def _growth_path(hist_rev: list, user_growth, n: int) -> list[float]:
    """assumptions 에 revenue_growth 가 있으면 그대로, 없으면 최근 CAGR 기반으로 매년
    완만히 감소(참고파일과 동일한 서술 패턴: '2021-2025 CAGR 대비 보수적 적용')."""
    if user_growth is not None:
        if isinstance(user_growth, (int, float)):
            return [float(user_growth)] * n
        g = [float(x) for x in user_growth]
        return (g + [g[-1]] * n)[:n]
    valid = [v for v in hist_rev if v]
    if len(valid) >= 2 and valid[-1] > 0:
        years = len(valid) - 1
        cagr_pct = ((valid[0] / valid[-1]) ** (1 / years) - 1) * 100
    else:
        cagr_pct = 5.0
    cagr_pct = max(-5.0, min(cagr_pct, 15.0))
    step = cagr_pct / max(n, 1)
    return [round(max(1.0, cagr_pct - step * i), 2) for i in range(n)]


# ── 히스토리(5개년) ────────────────────────────────────────────────
def _history(company: str, year: int | None) -> dict:
    items = ["revenue", "cogs", "interest_expense", "tax_expense", "net_income"]
    series = {it: dart.financial_item_nyear(company, it, N_HIST, year) for it in items}
    sga_s = dart.sga_item_nyear(company, N_HIST, year)
    cf_s = dart.cf_extras_nyear(company, N_HIST, year)
    years = [p["year"] for p in series["revenue"]["series"]]  # 최신 먼저
    amt = lambda s: [p["amount"] for p in s["series"]]
    return {
        "years": years,  # 최신 먼저(예: [2025,2024,2023,2022,2021])
        "corp_name": series["revenue"]["corp_name"],
        "revenue": amt(series["revenue"]), "cogs": amt(series["cogs"]),
        "sga": amt(sga_s), "interest_expense": amt(series["interest_expense"]),
        "tax_expense": amt(series["tax_expense"]), "net_income": amt(series["net_income"]),
        "capex": amt({"series": cf_s["capex"]}), "ocf": amt({"series": cf_s["ocf"]}),
        "nwc_change": amt({"series": cf_s["nwc_change"]}), "da": amt({"series": cf_s["da"]}),
    }


def _require_usable_history(h: dict, corp_name: str) -> None:
    """5시트 모델은 **정기보고서 API 의 5개년 손익 시리즈**로만 세울 수 있다.

    비상장사는 그 API 에 재무가 없어(감사보고서 파싱은 단년 항목만 대신한다) 시리즈가
    통째로 비고, 그대로 두면 뒤쪽 계산에서 None 연산으로 알 수 없는 오류가 난다.
    여기서 이유와 대안을 말하고 멈춘다.
    """
    need = {"revenue": "매출액", "cogs": "매출원가", "net_income": "당기순이익"}
    thin = [ko for key, ko in need.items()
            if sum(1 for v in h.get(key) or [] if v is not None) < 2]
    if thin:
        raise DataError(
            f"{corp_name} 은(는) 정기보고서 5개년 손익 시리즈를 확보하지 못해 "
            f"5시트 통합 모델을 만들 수 없습니다 (부족: {', '.join(thin)}). "
            f"비상장사는 DART 에 정기보고서가 없어 흔히 발생합니다 — "
            f"'DCF 요약 (1시트)' 또는 'HTML 리포트'를 사용하세요.")


def build_full_model(company: str, assumptions: dict, year: int | None = None) -> dict:
    n_fore = int(assumptions.get("forecast_years", 5))
    wacc_pct = float(assumptions["wacc_pct"])
    gt_pct = float(assumptions["terminal_growth_pct"])
    if wacc_pct <= gt_pct:
        raise DataError(f"WACC({wacc_pct}%)이 terminal growth({gt_pct}%)보다 커야 TV가 유효합니다.")
    fcff_mode = assumptions.get("fcff_mode", "raw")
    if fcff_mode not in ("raw", "zero_floor"):
        raise DataError(f"fcff_mode는 'raw' 또는 'zero_floor'만 지원합니다: {fcff_mode}")
    ebit_margin_target = assumptions.get("ebit_margin_pct")
    capex_pct = float(assumptions.get("capex_pct", 6.0))
    nwc_pct = float(assumptions.get("nwc_pct", 3.0))

    ent = dart.resolve(company)
    h = _history(company, year)
    _require_usable_history(h, ent["corp_name"])
    end_yr = h["years"][0]
    # h 의 시리즈는 최신→과거 순, 화면 표시는 과거→최신이 자연스러우니 뒤집는다.
    hist_years = list(reversed(h["years"]))
    rev_hist = list(reversed(h["revenue"]))
    cogs_hist = list(reversed(h["cogs"]))
    sga_hist = list(reversed(h["sga"]))
    ie_hist = list(reversed(h["interest_expense"]))
    tax_hist = list(reversed(h["tax_expense"]))
    ni_hist = list(reversed(h["net_income"]))
    capex_hist = list(reversed(h["capex"]))
    ocf_hist = list(reversed(h["ocf"]))
    nwc_hist = list(reversed(h["nwc_change"]))
    da_hist = list(reversed(h["da"]))

    ppe = dart.financial_item(company, "ppe", end_yr)
    cash = dart.financial_item(company, "cash", end_yr)
    debt = dart.debt_balances(company, end_yr)
    shares = dart.shares_outstanding(company, end_yr)
    tax_v = damodaran.corporate_tax_rate("KR")

    # ── 가정 기본값(실데이터 역산) ──
    cogs_pct_default = (_avg_ratio(cogs_hist[-3:], rev_hist[-3:]) or 0.7) * 100
    sga_pct_default = (_avg_ratio(sga_hist[-3:], rev_hist[-3:]) or 0.15) * 100
    growth = _growth_path(rev_hist, assumptions.get("revenue_growth"), n_fore)
    if ebit_margin_target is not None:
        # ebit_margin_pct(기존 compute_dcf 파라미터)를 존중 — COGS:SG&A 과거 비중을
        # 유지한 채 합계(=매출-EBIT)를 목표 마진에 맞게 스케일.
        opex_target_pct = 100 - float(ebit_margin_target)
        split = cogs_pct_default / max(cogs_pct_default + sga_pct_default, 1e-9)
        cogs_pct = opex_target_pct * split
        sga_pct = opex_target_pct * (1 - split)
    else:
        cogs_pct, sga_pct = cogs_pct_default, sga_pct_default

    tax_rate_pct = float(assumptions.get("tax_rate_pct") or tax_v.value)

    short_avg = sum(v for v in ([debt["short_term"].value]) if v) or 0
    long_avg = debt["long_term"].value or 0
    implied_short_rate = _avg_ratio(
        [i for i in ie_hist[-2:]], [short_avg] * len(ie_hist[-2:])) if short_avg else None
    short_rate_pct = round((implied_short_rate or 0.055) * 100, 2) if short_avg else 0.0
    long_rate_pct = 4.5 if long_avg else 0.0

    ppe_life_default = (ppe.value / da_hist[-1]) if da_hist and da_hist[-1] else 10
    new_capex_life = max(5, min(round(ppe_life_default), 25)) if ppe_life_default else 10

    pl = _build_pl(rev_hist, cogs_hist, sga_hist, ie_hist, tax_hist, ni_hist,
                   da_hist, capex_hist, nwc_hist, ocf_hist,
                   growth, cogs_pct, sga_pct, capex_pct, nwc_pct, tax_rate_pct,
                   n_fore, hist_years, end_yr)

    debt_sched = _build_debt_schedule(debt["short_term"].value, debt["long_term"].value,
                                      short_rate_pct, long_rate_pct, end_yr, n_fore, assumptions)

    dep_sched = _build_depreciation_schedule(ppe.value, new_capex_life, pl["capex_fore"],
                                             end_yr, n_fore, assumptions)

    # 예측 FCFF는 Depreciation Schedule 확정 후에야 D&A(예측)를 알 수 있어 여기서 마무리.
    nopat_fore = [e * (1 - tax_rate_pct / 100) for e in pl["ebit_fore"]]
    fcff_fore = [n + d - c - w for n, d, c, w in
                zip(nopat_fore, dep_sched["total_da"], pl["capex_fore"], pl["nwc_fore"])]
    pl["nopat_fore"] = nopat_fore
    pl["da_fore"] = dep_sched["total_da"]
    pl["fcff_fore"] = fcff_fore
    pl["fcff_zf_fore"] = [max(f, 0) for f in fcff_fore]

    net_debt = debt["short_term"].value + debt["long_term"].value - cash.value
    dcf_raw = _dcf_scenario(pl["fcff_fore"], wacc_pct, gt_pct, net_debt, shares.value)
    dcf_zf = _dcf_scenario(pl["fcff_zf_fore"], wacc_pct, gt_pct, net_debt, shares.value)
    primary = dcf_raw if fcff_mode == "raw" else dcf_zf
    warnings = [f"[{fcff_mode}] {w}" for w in primary["warnings"]]
    dcf = {"mode": fcff_mode, "raw": dcf_raw, "zero_floor": dcf_zf, "primary": primary,
          "warnings": warnings}

    return {
        "company": ent["corp_name"], "as_of": f"FY{end_yr}",
        "hist_years": hist_years, "fore_years": [end_yr + i for i in range(1, n_fore + 1)],
        "n_fore": n_fore,
        "pl": pl, "debt": debt_sched, "depreciation": dep_sched, "dcf": dcf,
        "assumptions": {
            "cogs_pct": cogs_pct, "sga_pct": sga_pct, "growth": growth,
            "capex_pct": capex_pct, "nwc_pct": nwc_pct, "tax_rate_pct": tax_rate_pct,
            "short_rate_pct": short_rate_pct, "long_rate_pct": long_rate_pct,
            "new_capex_life": new_capex_life, "wacc_pct": wacc_pct, "gt_pct": gt_pct,
            "fcff_mode": fcff_mode,
        },
        "sources": {
            "revenue": dart.financial_item_nyear(company, "revenue", N_HIST, year),
            "ppe": ppe, "cash": cash, "shares": shares, "tax": tax_v,
            "short_debt": debt["short_term"], "long_debt": debt["long_term"],
        },
    }


def _build_pl(rev_hist, cogs_hist, sga_hist, ie_hist, tax_hist, ni_hist, da_hist,
             capex_hist, nwc_hist, ocf_hist, growth, cogs_pct, sga_pct, capex_pct,
             nwc_pct, tax_rate_pct, n_fore, hist_years, end_yr) -> dict:
    """P&L and FCF 시트 데이터. 히스토리는 실측값, 예측은 가정 기반."""
    def _fcff(rev, ebit, da, capex, nwc_chg):
        if da is None:
            return None
        nopat = ebit * (1 - tax_rate_pct / 100)
        return nopat + da - (capex or 0) - (nwc_chg or 0)

    hist_ebit = [((r or 0) - (c or 0) - (s or 0)) if r is not None else None
                for r, c, s in zip(rev_hist, cogs_hist, sga_hist)]
    # 히스토리(실적)는 사실(fact)이라 zero-floor 하지 않는다 — 스펙 L: floor는 예측 전용 시나리오.
    hist_fcff = [_fcff(r, e, d, c, w) for r, e, d, c, w in
                zip(rev_hist, hist_ebit, da_hist, capex_hist, nwc_hist)]
    hist_fcff_check = [(o - c) if (o is not None and c is not None) else None
                       for o, c in zip(ocf_hist, capex_hist)]

    rev_fore, prev = [], rev_hist[-1] or 0
    for g in growth:
        prev = prev * (1 + g / 100)
        rev_fore.append(prev)
    cogs_fore = [r * cogs_pct / 100 for r in rev_fore]
    sga_fore = [r * sga_pct / 100 for r in rev_fore]
    ebit_fore = [r - c - s for r, c, s in zip(rev_fore, cogs_fore, sga_fore)]
    capex_fore = [r * capex_pct / 100 for r in rev_fore]
    rev_prev_chain = [rev_hist[-1] or 0] + rev_fore[:-1]
    nwc_fore = [(r - p) * nwc_pct / 100 for r, p in zip(rev_fore, rev_prev_chain)]
    # D&A(예측)는 Depreciation Schedule 시트에서 산출 — 여기서는 자리만(추후 채움).
    return {
        "hist_years": hist_years, "rev_hist": rev_hist, "cogs_hist": cogs_hist,
        "sga_hist": sga_hist, "ie_hist": ie_hist, "tax_hist": tax_hist, "ni_hist": ni_hist,
        "da_hist": da_hist, "capex_hist": capex_hist, "nwc_hist": nwc_hist,
        "ocf_hist": ocf_hist, "ebit_hist": hist_ebit, "fcff_hist": hist_fcff,
        "fcff_check_hist": hist_fcff_check,
        "rev_fore": rev_fore, "cogs_fore": cogs_fore, "sga_fore": sga_fore,
        "ebit_fore": ebit_fore, "capex_fore": capex_fore, "nwc_fore": nwc_fore,
        "growth": growth,
        # fcff_fore(raw)/fcff_zf_fore(zero-floor) 는 Depreciation Schedule 확정 후
        # build_full_model 에서 덧붙인다.
        "fcff_fore": None, "fcff_zf_fore": None,
    }


def _build_debt_schedule(st0, lt0, short_rate_pct, long_rate_pct, end_yr, n_fore,
                         assumptions) -> dict:
    years = [end_yr] + [end_yr + i for i in range(1, n_fore + 1)]
    st_repay = assumptions.get("st_repay_annual", round(st0 * 0.08)) if st0 else 0
    lt_repay = assumptions.get("lt_repay_annual", round(lt0 * 0.06)) if lt0 else 0

    def _roll(bop0, repay, n):
        bop, eop, bops, eops = bop0, bop0, [], []
        for _ in range(n):
            bops.append(bop)
            e = max(0, bop - repay)
            eops.append(e)
            bop = e
        return bops, eops

    st_bops, st_eops = _roll(st0, st_repay, n_fore)
    lt_bops, lt_eops = _roll(lt0, lt_repay, n_fore)
    st_interest = [((b + e) / 2) * short_rate_pct / 100 for b, e in zip(st_bops, st_eops)]
    lt_interest = [((b + e) / 2) * long_rate_pct / 100 for b, e in zip(lt_bops, lt_eops)]
    return {
        "years": years, "short0": st0, "long0": lt0,
        "short_rate_pct": short_rate_pct, "long_rate_pct": long_rate_pct,
        "short_bop": st_bops, "short_repay": [st_repay] * n_fore, "short_eop": st_eops,
        "long_bop": lt_bops, "long_repay": [lt_repay] * n_fore, "long_eop": lt_eops,
        "short_interest": st_interest, "long_interest": lt_interest,
        "total_interest": [s + l for s, l in zip(st_interest, lt_interest)],
        "total_debt_fore": [s + l for s, l in zip(st_eops, lt_eops)],
    }


def _build_depreciation_schedule(ppe0, new_capex_life, capex_fore, end_yr, n_fore,
                                 assumptions) -> dict:
    """기존자산 2버킷(건물/기계장치, 비중·내용연수는 조정 가능한 가정 — 회사별 실제
    자산구성을 모르므로 기본값일 뿐 사용자가 직접 조정해야 함) + 신규 CAPEX vintage
    waterfall(참고파일과 동일 패턴: 발생연도는 0, 다음해부터 내용연수만큼 직선상각)."""
    years = [end_yr + i for i in range(1, n_fore + 1)]
    building_pct = assumptions.get("building_pct", 60.0)
    building_life = assumptions.get("building_life", 20)
    machinery_life = assumptions.get("machinery_life", 8)
    building_book = ppe0 * building_pct / 100
    machinery_book = ppe0 * (1 - building_pct / 100)
    building_dep_yr = building_book / building_life if building_life else 0
    machinery_dep_yr = machinery_book / machinery_life if machinery_life else 0

    building_dep, machinery_dep = [], []
    b_remaining, m_remaining = building_book, machinery_book
    for _ in range(n_fore):
        b = min(building_dep_yr, b_remaining)
        m = min(machinery_dep_yr, m_remaining)
        building_dep.append(b)
        machinery_dep.append(m)
        b_remaining -= b
        m_remaining -= m
    existing_subtotal = [b + m for b, m in zip(building_dep, machinery_dep)]

    # vintage waterfall: capex_fore[i] 는 years[i] 에 발생, years[i]엔 상각 0,
    # years[i+1] 부터 capex_fore[i]/new_capex_life 씩 상각(내용연수 끝나면 정지).
    new_capex_dep_by_vintage = {}
    for i, cpx in enumerate(capex_fore):
        per_yr = cpx / new_capex_life if new_capex_life else 0
        row = [0.0] * n_fore
        for j in range(i + 1, n_fore):
            if (j - i) <= new_capex_life:
                row[j] = per_yr
        new_capex_dep_by_vintage[years[i]] = row
    new_capex_subtotal = [sum(new_capex_dep_by_vintage[y][j] for y in new_capex_dep_by_vintage)
                          for j in range(n_fore)]
    total = [e + n for e, n in zip(existing_subtotal, new_capex_subtotal)]
    return {
        "years": years, "ppe0": ppe0, "building_pct": building_pct,
        "building_life": building_life, "machinery_life": machinery_life,
        "building_book": building_book, "machinery_book": machinery_book,
        "building_dep": building_dep, "machinery_dep": machinery_dep,
        "existing_subtotal": existing_subtotal, "new_capex_life": new_capex_life,
        "capex_by_year": dict(zip(years, capex_fore)),
        "new_capex_dep_by_vintage": new_capex_dep_by_vintage,
        "new_capex_subtotal": new_capex_subtotal, "total_da": total,
    }


def _dcf_scenario(fcff_fore, wacc_pct, gt_pct, net_debt, shares) -> dict:
    """한 FCFF 시나리오(raw 또는 zero-floor)에 대한 DCF 계산 — 스펙 N/O: Terminal FCFF≤0
    이면 Gordon Growth 결과를 정상 값처럼 내지 않고, 지분가치 음수는 숨기지 않는다."""
    wacc, gt = wacc_pct / 100, gt_pct / 100
    n = len(fcff_fore)
    warnings = []
    discount_factors = [1 / (1 + wacc) ** t for t in range(1, n + 1)]
    pv_fcff = [f * d for f, d in zip(fcff_fore, discount_factors)]
    pv_sum = sum(pv_fcff)
    terminal_fcff = fcff_fore[-1] * (1 + gt)

    valuation_reliable = True
    if terminal_fcff <= 0:
        terminal_value = pv_tv = None
        valuation_reliable = False
        warnings.append(f"Terminal FCFF({terminal_fcff:,.0f}) ≤ 0 — Gordon Growth TV 신뢰 불가. "
                        "명시적 예측기간 연장 또는 정상상태 가정 수정 필요.")
    elif wacc <= gt:
        terminal_value = pv_tv = None
        valuation_reliable = False
        warnings.append(f"WACC({wacc_pct}%) ≤ terminal g({gt_pct}%) — TV 계산 불가.")
    else:
        terminal_value = terminal_fcff / (wacc - gt)
        pv_tv = terminal_value * discount_factors[-1]

    ev = pv_sum + (pv_tv or 0)
    tv_weight_pct = (pv_tv / ev * 100) if (pv_tv and ev > 0) else None
    if tv_weight_pct is not None:
        if tv_weight_pct > 90:
            warnings.append(f"Terminal Value가 EV의 {tv_weight_pct:.0f}% — high-risk(>90%).")
        elif tv_weight_pct > 75:
            warnings.append(f"Terminal Value가 EV의 {tv_weight_pct:.0f}% — warning(>75%).")

    equity_value = ev - net_debt
    per_share_raw = equity_value / shares if shares else 0
    per_share_display = "NM" if equity_value <= 0 else round(per_share_raw)
    if equity_value <= 0:
        warnings.append(f"지분가치({equity_value:,.0f}) ≤ 0 — 주당가치는 표시상 NM(음수 원본값은 유지).")

    wacc_axis = [max(0.01, wacc_pct + d) for d in (-2, -1, 0, 1, 2, 3)]
    g_axis = [gt_pct + d for d in (-0.5, -0.25, 0, 0.25, 0.5)]
    sens_ps, sens_ev = {}, {}
    for w in wacc_axis:
        sens_ps[w], sens_ev[w] = {}, {}
        for g in g_axis:
            if w / 100 <= g / 100 or terminal_fcff <= 0:
                sens_ps[w][g], sens_ev[w][g] = None, None
                continue
            wf = [1 / (1 + w / 100) ** t for t in range(1, n + 1)]
            pv2 = sum(f * d for f, d in zip(fcff_fore, wf))
            tv2 = fcff_fore[-1] * (1 + g / 100) / (w / 100 - g / 100)
            ev2 = pv2 + tv2 * wf[-1]
            eq2 = ev2 - net_debt
            sens_ev[w][g] = ev2
            sens_ps[w][g] = eq2 / shares if shares else 0

    return {
        "wacc_pct": wacc_pct, "gt_pct": gt_pct, "net_debt": net_debt, "shares": shares,
        "discount_factors": discount_factors, "pv_fcff": pv_fcff, "pv_sum": pv_sum,
        "terminal_fcff": terminal_fcff, "terminal_value": terminal_value, "pv_tv": pv_tv,
        "tv_weight_pct": tv_weight_pct, "valuation_reliable": valuation_reliable,
        "ev": ev, "equity_value": equity_value, "per_share_raw": per_share_raw,
        "per_share": per_share_display, "wacc_axis": wacc_axis, "g_axis": g_axis,
        "sens_per_share": sens_ps, "sens_ev": sens_ev, "warnings": warnings,
    }
