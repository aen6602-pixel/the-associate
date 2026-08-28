"""가정 세트의 **내부 정합성** 검사 — 개별로는 근거가 있어도 조합이 성립하지 않을 수 있다.

실측 사고: 팬데믹 구간 평균 성장률 16.23% 와 부진기 포함 평균 마진 8.50% 를 한 세트로 묶어
5년을 깔았다. 각 드라이버는 저마다 "공시 기반" 이지만 서로 다른 국면에서 뽑은 값이라
동시에 성립할 수 없다.

여기서 보는 것은 두 종류다.

1. **절대 기준** — 각 가정이 그 자체로 말이 되는가
   · 성장률이 명목 GDP·산업 성장 대비 과도한가
   · 마진이 과거 밴드를 벗어나는가
   · g 가 장기 인플레+실질성장 범위 안인가 (그리고 Rf 를 넘지 않는가)
   · CAPEX/매출이 D&A/매출보다 지속적으로 낮아 자산이 소멸하는 구조는 아닌가
2. **상호 정합** — 가정들이 서로 맞는가
   · 성장은 높은데 재투자가 없다(성장에는 자본이 든다)
   · 마진은 과거 최고인데 성장도 과거 최고다(둘 다 최선을 가정)

⚠️ 이 모듈은 **막지 않는다.** 판단이 필요한 영역이라 자동으로 값을 바꾸거나 산출을 봉인하면
분석가의 판단을 대신하게 된다. 대신 "무엇이 서로 안 맞는지" 를 구체적으로 적어 올린다.
"""
from __future__ import annotations

# 절대 기준 — 넘으면 근거를 요구한다.
GROWTH_VS_GDP_MULT = 3.0     # 명목 GDP 성장의 몇 배까지 봐줄 것인가(5년 지속 기준)
KR_NOMINAL_GDP_GROWTH = 3.5  # 한국 명목 GDP 성장률 대략치(%). 국가별로 넘겨 덮을 수 있다.
TERMINAL_G_MAX = 4.0         # 영구성장률 상한(장기 인플레 + 실질성장)
CAPEX_DA_MIN_RATIO = 0.8     # CAPEX/D&A 가 이보다 낮으면 자산이 줄어드는 구조


def _band(history: list[float]) -> tuple[float, float, float] | None:
    if not history:
        return None
    return min(history), max(history), sum(history) / len(history)


def check(*, revenue_growth_pct: float, ebit_margin_pct: float, da_pct: float,
          capex_pct: float, terminal_growth_pct: float, wacc_pct: float,
          growth_history: list[float] | None = None,
          margin_history: list[float] | None = None,
          risk_free_pct: float | None = None,
          nominal_gdp_growth_pct: float | None = None,
          forecast_years: int = 5) -> dict:
    """→ {flags: [...], metrics: {...}}. 막지 않고 알린다."""
    flags: list[str] = []
    metrics: dict = {}
    gdp = nominal_gdp_growth_pct if nominal_gdp_growth_pct is not None else KR_NOMINAL_GDP_GROWTH

    # ── 절대 기준 ───────────────────────────────────────────────────
    metrics["growth_vs_gdp"] = revenue_growth_pct / gdp if gdp else None
    if gdp and revenue_growth_pct > gdp * GROWTH_VS_GDP_MULT:
        flags.append(
            f"매출성장률 {revenue_growth_pct:.1f}% 를 {forecast_years}년 유지하면 명목 GDP "
            f"성장({gdp:.1f}%)의 {revenue_growth_pct / gdp:.1f}배입니다 — 그 기간 내내 시장에서 "
            f"점유율을 계속 뺏는다는 뜻이니 근거가 필요합니다.")

    if terminal_growth_pct > TERMINAL_G_MAX:
        flags.append(
            f"영구성장률 {terminal_growth_pct:.1f}% 가 장기 인플레+실질성장 범위"
            f"({TERMINAL_G_MAX:.1f}%)를 넘습니다 — 영구히 경제보다 빨리 크는 기업은 없습니다.")
    if risk_free_pct is not None and terminal_growth_pct > risk_free_pct:
        flags.append(
            f"영구성장률 {terminal_growth_pct:.1f}% 가 무위험수익률 {risk_free_pct:.1f}% 를 "
            f"넘습니다 — 국채수익률이 명목 성장률의 상한 대용이라는 전제와 어긋납니다.")

    if da_pct > 0:
        ratio = capex_pct / da_pct
        metrics["capex_to_da"] = ratio
        if ratio < CAPEX_DA_MIN_RATIO:
            flags.append(
                f"CAPEX/매출 {capex_pct:.2f}% 가 D&A/매출 {da_pct:.2f}% 의 {ratio:.2f}배입니다 "
                f"— 감가상각만큼도 재투자하지 않는 상태가 {forecast_years}년 이어지면 자산이 "
                f"소멸합니다. 성장 가정과 함께 두면 특히 앞뒤가 안 맞습니다.")

    # ── 과거 밴드 대비 ──────────────────────────────────────────────
    gb, mb = _band(growth_history or []), _band(margin_history or [])
    if gb:
        lo, hi, avg = gb
        metrics["growth_band"] = (lo, hi, avg)
        if revenue_growth_pct > hi:
            flags.append(f"매출성장률 {revenue_growth_pct:.1f}% 가 과거 최고 {hi:.1f}% 를 "
                         f"넘습니다(과거 {lo:.1f}~{hi:.1f}%).")
    if mb:
        lo, hi, avg = mb
        metrics["margin_band"] = (lo, hi, avg)
        if ebit_margin_pct > hi:
            flags.append(f"영업이익률 {ebit_margin_pct:.1f}% 가 과거 최고 {hi:.1f}% 를 "
                         f"넘습니다(과거 {lo:.1f}~{hi:.1f}%).")

    # ── 상호 정합 ───────────────────────────────────────────────────
    if gb and mb:
        # 둘 다 과거 상위 구간이면 "모든 것이 동시에 잘 된다" 는 가정이다.
        g_hi = revenue_growth_pct >= gb[2] and revenue_growth_pct > gb[0]
        m_hi = ebit_margin_pct >= mb[2] and ebit_margin_pct > mb[0]
        if g_hi and m_hi and (revenue_growth_pct > gb[1] * 0.9 or ebit_margin_pct > mb[1] * 0.9):
            flags.append(
                f"성장률({revenue_growth_pct:.1f}%)과 마진({ebit_margin_pct:.1f}%)을 둘 다 과거 "
                f"상위 구간으로 잡았습니다 — 성장과 수익성이 동시에 최선인 국면을 "
                f"{forecast_years}년 가정하는 셈이라, 서로 다른 국면에서 뽑은 값을 한 세트로 "
                f"묶은 것은 아닌지 확인하세요(팬데믹 성장률 + 부진기 마진 같은 조합).")

    # 성장에는 자본이 든다 — 높은 성장인데 순재투자가 거의 없으면 앞뒤가 안 맞는다.
    net_reinvest = capex_pct - da_pct
    metrics["net_reinvest_pct"] = net_reinvest
    if revenue_growth_pct > gdp and net_reinvest <= 0:
        flags.append(
            f"매출을 매년 {revenue_growth_pct:.1f}% 늘리면서 순재투자(CAPEX−D&A)가 "
            f"{net_reinvest:+.2f}%p 입니다 — 자본 투입 없이 성장한다는 가정이라 근거가 필요합니다.")

    if wacc_pct <= terminal_growth_pct:
        flags.append(f"WACC {wacc_pct:.2f}% ≤ g {terminal_growth_pct:.2f}% — TV 가 성립하지 않습니다.")

    return {"flags": flags, "metrics": metrics,
            "verdict": "disclose" if flags else "ok"}


def summary(result: dict) -> str:
    if not result["flags"]:
        return ""
    return ("⚠️ [가정 정합성] " + " ".join(result["flags"]) + " ")
