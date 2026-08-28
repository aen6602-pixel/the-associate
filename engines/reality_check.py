"""DCF 결과를 시장·구조 기준으로 대조하는 필수 검증 블록.

왜 엔진에 넣는가 — 예전에는 이 대조가 "요청하면 해주는 것" 이었다. 실측 사고: 주당
415,204원을 시가 126,800원(+227%)과 **비교도 하지 않고** 내놨고, 지적을 받고서야 comps 를
돌렸다. 대체하려는 대상이 바로 그 지적을 하는 사람이므로, 지적이 없어도 붙어야 한다.

여기서 보는 것은 넷이다.
  1) 시장 대비 프리미엄/디스카운트 — "시장이 완전히 틀렸다" 는 주장을 하고 있는지
  2) 내재 배수(진입·청산) — DCF 가 암묵적으로 어떤 배수를 사고파는 것으로 가정하는지
  3) TV 비중 — 결론이 예측기간이 아니라 g·WACC 에 지배되는지
  4) 증분 ROIC vs WACC — **성장이 가치를 만드는 구조가 성립하는지**

4번이 가장 중요하다. 재투자를 하는데 증분 ROIC 가 WACC 보다 낮으면 성장할수록 가치가
줄어야 한다. 그런데 모델이 성장으로 가치를 만들어내고 있다면 그건 취향 차이가 아니라
모델이 틀린 것이다.

판정은 두 단계로만 나눈다.
  disclose  결론보다 **먼저** 보여줘야 하는 상태. 산출을 막지는 않는다.
  ok        특이사항 없음.
시가와 크게 다르다는 이유로 산출을 막지는 않는다 — 역발상 분석이 곧 결론인 경우가 있고,
그걸 봉인하면 도구가 시장 추종만 하게 된다. 수학적으로 무의미한 상태(UFCF·EV·지분가치
음수)의 봉인은 dcf 엔진이 따로 담당한다.
"""
from __future__ import annotations

from core.schema import DataError

# 임계치 — 넘으면 "먼저 보여줘야 하는" 상태가 된다.
PREMIUM_LIMIT = 50.0      # 시가 대비 ±% (역발상 자체는 정상이지만 근거를 앞세워야 한다)
TV_SHARE_LIMIT = 80.0     # TV 가 EV 에서 차지하는 %
EXIT_ENTRY_RATIO = 1.5    # 청산배수 / 진입배수. 1.5 배면 "비싸게 팔고 나온다" 는 가정이다
MIN_REINVEST_RATIO = 0.02 # 재투자가 매출의 이 미만이면 증분 ROIC 를 논하지 않는다


def _incremental_roic(rows: list[dict], tax_pct: float) -> dict | None:
    """증분 ROIC = Σ ΔNOPAT / Σ 순재투자.  순재투자 = CAPEX − D&A + ΔNWC.

    감가상각만큼의 CAPEX 는 기존 자산 유지분이라 '새로 투입한 자본' 이 아니다. 그래서
    순재투자로 본다. 재투자가 사실상 없으면(유지보수 수준) 증분 ROIC 는 정의되지 않는다.
    """
    if len(rows) < 2:
        return None
    reinvest = sum(r["capex"] - r["da"] + r["dnwc"] for r in rows[1:])
    d_nopat = rows[-1]["nopat"] - rows[0]["nopat"]
    rev_scale = sum(r["rev"] for r in rows[1:])
    if rev_scale <= 0 or reinvest <= rev_scale * MIN_REINVEST_RATIO:
        return None
    return {"reinvest": reinvest, "d_nopat": d_nopat,
            "roic_pct": d_nopat / reinvest * 100.0}


def evaluate(model: dict, market_value: float | None = None,
             price: float | None = None, price_as_of: str | None = None) -> dict:
    """DCF build_model 결과 + (선택) 시가총액 → 검증 블록.

    market_value 를 못 구하면(비상장 등) 시장 대조 항목만 비우고 구조 검사는 그대로 한다 —
    "시가가 없어서 검증을 통째로 건너뛰었다" 가 되면 안 된다.
    """
    rows = model["rows"]
    ev, equity, wacc = model["ev"], model["equity_value"], model["wacc_pct"]
    pv_tv, per_share = model["pv_tv"], model["per_share"]

    flags: list[str] = []
    metrics: dict = {}

    # ── 1) 시장 대비 ────────────────────────────────────────────────
    if market_value:
        prem = (equity / market_value - 1) * 100.0
        metrics["market_cap"] = market_value
        metrics["premium_pct"] = prem
        if abs(prem) > PREMIUM_LIMIT:
            flags.append(
                f"시가총액 대비 {prem:+.0f}% (지분가치 {equity:,.0f} vs 시가총액 "
                f"{market_value:,.0f}) — 시장 가격과 {abs(prem):.0f}% 다른 결론이므로, "
                f"무엇을 시장과 다르게 보는지를 결론보다 먼저 밝혀야 합니다.")
    if price and per_share:
        metrics["price"] = price
        metrics["price_as_of"] = price_as_of
        metrics["per_share_premium_pct"] = (per_share / price - 1) * 100.0

    # ── 2) 내재 배수(진입 · 청산) ───────────────────────────────────
    # EV 가 음수면 배수는 계산되더라도 해석이 불가능하다(-13.1x 같은 값이 나온다).
    # 숫자를 내보내는 것보다 NM 으로 두는 게 정직하다 — 봉인은 dcf 엔진이 따로 한다.
    ebitda_1 = rows[0]["ebit"] + rows[0]["da"]
    ebitda_n = rows[-1]["ebit"] + rows[-1]["da"]
    if ev <= 0:
        metrics["multiples"] = "NM (EV ≤ 0)"
        ebitda_1 = ebitda_n = 0
    if ebitda_1 > 0:
        metrics["implied_entry_ev_ebitda"] = ev / ebitda_1
        if market_value is not None:
            metrics["market_entry_ev_ebitda"] = (market_value + model["net_debt"]) / ebitda_1
    if ebitda_n > 0:
        exit_mult = model["tv"] / ebitda_n
        metrics["implied_exit_ev_ebitda"] = exit_mult
        entry = metrics.get("implied_entry_ev_ebitda")
        if entry and entry > 0 and exit_mult / entry > EXIT_ENTRY_RATIO:
            flags.append(
                f"내재 청산배수 EV/EBITDA {exit_mult:.1f}x 가 진입배수 {entry:.1f}x 의 "
                f"{exit_mult / entry:.1f}배입니다 — 지금보다 비싸게 팔고 나온다는 가정이 "
                f"결과를 만들고 있습니다. g 를 낮추거나 exit multiple 방식과 대조하세요.")

    # ── 3) TV 비중 ──────────────────────────────────────────────────
    if ev > 0:
        tv_share = pv_tv / ev * 100.0
        metrics["tv_share_pct"] = tv_share
        if tv_share > TV_SHARE_LIMIT:
            flags.append(
                f"EV 의 {tv_share:.0f}% 가 Terminal Value 입니다 — 예측기간 가정이 아니라 "
                f"g·WACC 가 결론을 정하고 있습니다.")

    # ── 4) 증분 ROIC vs WACC ────────────────────────────────────────
    ir = _incremental_roic(rows, model["tax_pct"])
    if ir:
        metrics["incremental_roic_pct"] = ir["roic_pct"]
        metrics["roic_wacc_spread_pct"] = ir["roic_pct"] - wacc
        if ir["roic_pct"] < wacc:
            flags.append(
                f"증분 ROIC {ir['roic_pct']:.1f}% < WACC {wacc}% — 재투자 "
                f"{ir['reinvest']:,.0f} 를 넣어 얻는 이익 증가분이 자본비용에 못 미칩니다. "
                f"이 구조에서는 성장이 가치를 **깎아야** 하는데 모델은 성장으로 가치를 "
                f"만들고 있습니다. 마진·CAPEX·성장률 조합이 서로 맞는지 다시 보세요.")

    return {"verdict": "disclose" if flags else "ok", "flags": flags, "metrics": metrics}


def summary_line(check: dict, currency: str = "") -> str:
    """검증 블록을 note 앞머리에 넣을 한 줄로."""
    m = check["metrics"]
    bits = []
    if "premium_pct" in m:
        bits.append(f"시가 대비 {m['premium_pct']:+.0f}%")
    if "implied_entry_ev_ebitda" in m:
        bits.append(f"내재 진입 EV/EBITDA {m['implied_entry_ev_ebitda']:.1f}x")
    if "implied_exit_ev_ebitda" in m:
        bits.append(f"청산 {m['implied_exit_ev_ebitda']:.1f}x")
    if "tv_share_pct" in m:
        bits.append(f"TV {m['tv_share_pct']:.0f}%")
    if "incremental_roic_pct" in m:
        bits.append(f"증분ROIC {m['incremental_roic_pct']:.1f}%")
    head = "[시장·구조 대조] " + (", ".join(bits) if bits else "대조 가능한 시장가 없음")
    if check["flags"]:
        head = "⚠️ " + head + " → " + " ".join(check["flags"])
    return head + " "


def market_reference(company: str, market: str) -> dict:
    """시가총액·종가를 한 번에. 실패해도 예외를 올리지 않고 사유를 담아 돌려준다."""
    try:
        from engines import market_data

        spec = market_data.resolve(company, market)
        mc = market_data.market_cap(spec)
        px = (mc.extras or {}).get("price")
        return {"market_cap": mc.value, "price": px.value if px else None,
                "as_of": mc.provenance.as_of, "error": None}
    except (DataError, Exception) as e:  # noqa: BLE001 — 대조 실패가 DCF 를 막지는 않는다
        return {"market_cap": None, "price": None, "as_of": None, "error": str(e)}
