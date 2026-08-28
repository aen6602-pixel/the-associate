"""역산 진단(reverse DCF) — "그 가격이 성립하려면 무엇을 믿어야 하는가".

목표주가를 받아 인풋을 미지수로 푸는 요청은 두 가지로 갈린다.

  ❌ 숫자 맞춰주기   "TP 190,000원에 맞는 WACC·g 조합으로 기본안 표를 만들어라"
                    → 가정을 결론에 맞춰 사후 조작하는 것. 산출물이 기본안으로 유통된다.
  ✅ 역산 진단       "190,000원이 성립하려면 필요한 가정은 무엇이고, 그게 이 회사의 과거
                    실적·산업 밴드 대비 어디쯤인가"
                    → IC 에서 실제로 쓰는 산출물. 방어 가능 여부를 판정해 준다.

이 모듈은 후자만 만든다. 그래서 결과에 **주당가치를 내놓지 않는다** — 내놓는 것은
"필요 가정"과 "그 가정이 과거 대비 몇 분위인가" 뿐이다. 목표가에 맞춘 기본안 표를 만들
경로 자체를 두지 않는 것이 이 설계의 핵심이다(compute_dcf 는 target_value 인자를 받지
않는다). 산출물에는 TARGET-FITTED 낙인을 강제로 찍어 기본안과 섞이지 않게 한다.

실측 사고: "하우스 TP 190,000원에 ±2%로 맞춰라, 다른 생각 말고" 라는 압박에
WACC 9.81%/g 1.5% → 191,114원 표를 만들어 줬다. 경고는 붙었지만 표는 나갔다.
"""
from __future__ import annotations

from core import runid
from core.schema import DataError, Provenance, SourceType, Value
from engines import dcf as dcf_engine

WATERMARK = "TARGET-FITTED · NOT A BASE CASE"

# 필요 가정이 과거 실적 밴드의 이 밖이면 "방어 불가" 로 판정한다.
STRETCH_SIGMA = 1.0     # 과거 평균 ± 1σ 밖이면 stretch
EXTREME_RATIO = 1.5     # 과거 최대치의 1.5배를 넘으면 방어 불가


def _solve(fn, lo: float, hi: float, target: float, tol: float = 1e-6,
           iters: int = 200) -> float | None:
    """단조 함수의 이분 탐색. 구간 안에 해가 없으면 None(‘그 가격은 불가능’)."""
    f_lo, f_hi = fn(lo) - target, fn(hi) - target
    if f_lo == 0:
        return lo
    if f_hi == 0:
        return hi
    if f_lo * f_hi > 0:
        return None
    for _ in range(iters):
        mid = (lo + hi) / 2
        f_mid = fn(mid) - target
        if abs(f_mid) < tol or (hi - lo) < tol:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def _percentile_note(name: str, needed: float, history: list[float],
                     higher_is_aggressive: bool = True) -> tuple[str, str]:
    """필요 가정이 과거 대비 어디인지 → (판정, 설명).

    **방향이 중요하다.** 목표가를 만들기 위해 과거보다 *낮은* 성장률이면 되는 경우는
    경고 대상이 아니라 오히려 보수적이라는 뜻이다. 변수마다 '공격적' 방향이 다르다 —
    성장률·마진은 높을수록, WACC 는 낮을수록, 영구성장률은 높을수록 값이 커진다.
    """
    if not history:
        return "unknown", f"{name} 과거 실적을 확보하지 못해 분위를 판정할 수 없습니다."
    lo, hi = min(history), max(history)
    avg = sum(history) / len(history)
    sd = (sum((x - avg) ** 2 for x in history) / len(history)) ** 0.5 if len(history) > 1 else 0.0
    band = f"과거 {len(history)}개년 {lo:.1f}~{hi:.1f}, 평균 {avg:.1f}"

    # 공격적인 쪽으로 얼마나 벗어났는가. 음수면 보수적인 쪽이다.
    excess = (needed - avg) if higher_is_aggressive else (avg - needed)
    beyond = (needed - hi) if higher_is_aggressive else (lo - needed)

    if beyond > 0 and abs(hi if higher_is_aggressive else lo) > 0:
        ref = hi if higher_is_aggressive else lo
        if abs(needed) > abs(ref) * EXTREME_RATIO:
            return "indefensible", (
                f"{name} {needed:.1f}% 가 필요한데 {band} 입니다 — 과거 극단치의 "
                f"{abs(needed) / max(abs(ref), 1e-9):.1f}배라 공시로 방어할 수 없습니다.")
    if excess <= 0:
        return "ok", (f"{name} {needed:.1f}% 면 됩니다. {band} — 과거보다 "
                      f"{'낮은' if higher_is_aggressive else '높은'} 수준이라 "
                      f"이 가정 자체는 보수적입니다.")
    if sd > 0 and excess > STRETCH_SIGMA * sd:
        return "stretch", (f"{name} {needed:.1f}% 가 필요합니다. {band} (±1σ={sd:.1f}) — "
                           f"과거 평균보다 공격적인 쪽으로 1σ 이상 벗어나므로 근거가 필요합니다.")
    if beyond > 0:
        return "stretch", (f"{name} {needed:.1f}% 가 필요합니다. {band} — 과거 최고 수준을 "
                           f"넘으므로 근거가 필요합니다.")
    return "ok", f"{name} {needed:.1f}% 가 필요합니다. {band} — 과거 범위 안입니다."


def diagnose(company: str, target_per_share: float, wacc_pct: float, net_debt: float,
             revenue_growth_pct: float, ebit_margin_pct: float, da_pct: float,
             capex_pct: float, nwc_pct: float, terminal_growth_pct: float,
             forecast_years: int = 5, tax_rate_pct: float | None = None,
             year: int | None = None, market: str = "KR",
             solve_for: str = "auto") -> dict:
    """목표 주당가치가 성립하려면 필요한 가정을 역산하고 방어 가능성을 판정한다.

    solve_for: 'growth' | 'margin' | 'wacc' | 'terminal_growth' | 'auto'(=growth·margin 둘 다)
    나머지 인자는 기본안(base case)의 가정이다 — 무엇을 고정하고 무엇을 푸는지가 분명해야
    "그 가격이 요구하는 것" 이 의미를 갖는다.
    """
    if target_per_share is None or float(target_per_share) <= 0:
        raise DataError("목표 주당가치(target_per_share)를 양수로 지정하세요.")
    target = float(target_per_share)

    base_kw = dict(company=company, net_debt=net_debt, da_pct=da_pct, capex_pct=capex_pct,
                   nwc_pct=nwc_pct, forecast_years=forecast_years,
                   tax_rate_pct=tax_rate_pct, year=year, market=market,
                   skip_market_check=True)

    def per_share(**over) -> float:
        kw = {"wacc_pct": wacc_pct, "revenue_growth": revenue_growth_pct,
              "ebit_margin_pct": ebit_margin_pct,
              "terminal_growth_pct": terminal_growth_pct, **base_kw, **over}
        try:
            m = dcf_engine.build_model(**kw)
        except DataError:
            return float("nan")
        return m["per_share"]

    base = dcf_engine.build_model(
        wacc_pct=wacc_pct, revenue_growth=revenue_growth_pct,
        ebit_margin_pct=ebit_margin_pct, terminal_growth_pct=terminal_growth_pct, **base_kw)
    base_ps = base["per_share"]

    # 과거 실적 밴드 — 필요 가정이 "어디쯤인지" 를 재는 자
    history: dict = {}
    if market.upper() == "KR":
        try:
            from engines import dcf_inputs

            h = dcf_inputs.historical_ratios(company, 5, year)
            for key, src in (("growth", "revenue_growth_pct"), ("margin", "ebit_margin_pct")):
                v = h.get(src)
                if v is not None and v.provenance.note:
                    history[key] = _years_from_note(v.provenance.note)
        except Exception:  # noqa: BLE001 — 밴드를 못 구해도 역산 자체는 가능하다
            pass

    solved: dict = {}
    wants = (["growth", "margin"] if solve_for in ("auto", "") else [solve_for])
    for what in wants:
        if what == "growth":
            got = _solve(lambda g: per_share(revenue_growth=g), -20.0, 80.0, target)
            label = "필요 매출성장률"
        elif what == "margin":
            got = _solve(lambda m: per_share(ebit_margin_pct=m), 0.1, 60.0, target)
            label = "필요 영업이익률"
        elif what == "wacc":
            # WACC 은 낮을수록 가치가 커진다 — 하한은 terminal g 보다 커야 한다
            got = _solve(lambda w: per_share(wacc_pct=w), terminal_growth_pct + 0.05, 30.0,
                         target)
            label = "필요 WACC"
        elif what == "terminal_growth":
            got = _solve(lambda gt: per_share(terminal_growth_pct=gt), -5.0, wacc_pct - 0.05,
                         target)
            label = "필요 영구성장률"
        else:
            raise DataError(f"solve_for 는 growth|margin|wacc|terminal_growth|auto 중 하나: {solve_for}")

        if got is None:
            solved[what] = {"needed": None, "verdict": "impossible", "label": label,
                            "note": (f"{label}을 아무리 조정해도 {target:,.0f} 에 도달하지 "
                                     f"못합니다 — 다른 가정이 그 가격을 막고 있습니다.")}
            continue
        # WACC 만 낮을수록 공격적이다(할인율이 낮아야 값이 커진다).
        verdict, note = _percentile_note(label, got, history.get(what, []),
                                         higher_is_aggressive=(what != "wacc"))
        solved[what] = {"needed": round(got, 2), "verdict": verdict, "label": label,
                        "note": note}

    verdicts = [v["verdict"] for v in solved.values()]
    overall = ("impossible" if all(v == "impossible" for v in verdicts)
               else "indefensible" if "indefensible" in verdicts
               else "stretch" if "stretch" in verdicts
               else "ok")

    run = runid.stamp("reverse_dcf", {
        "company": company, "target": target, "wacc_pct": wacc_pct, "net_debt": net_debt,
        "revenue_growth_pct": revenue_growth_pct, "ebit_margin_pct": ebit_margin_pct,
        "da_pct": da_pct, "capex_pct": capex_pct, "nwc_pct": nwc_pct,
        "terminal_growth_pct": terminal_growth_pct, "forecast_years": forecast_years,
        "market": market, "solve_for": solve_for})

    return {"company": base["company"], "market": market, "target_per_share": target,
            "base_per_share": base_ps, "gap_pct": (target / base_ps - 1) * 100 if base_ps else None,
            "solved": solved, "verdict": overall, "history": history,
            "as_of": base["as_of"], "run": run, "watermark": WATERMARK}


def _years_from_note(note: str) -> list[float]:
    """historical_ratios 의 note 에 박힌 '연도 값%' 들을 뽑아 밴드로 쓴다."""
    import re

    return [float(x) for x in re.findall(r"\d{4}\s+([+-]?\d+\.?\d*)%", note)]


def evaluate(company: str, target_per_share: float, **kw) -> Value:
    d = diagnose(company, target_per_share, **kw)
    verdict_ko = {"ok": "과거 범위 안 — 방어 가능",
                  "stretch": "과거 밴드 밖 — 별도 근거 필요",
                  "indefensible": "공시로 방어 불가",
                  "impossible": "어떤 가정으로도 도달 불가",
                  "unknown": "판정 불가(과거 실적 미확보)"}[d["verdict"]]
    lines = " / ".join(v["note"] for v in d["solved"].values())
    note = (f"[{WATERMARK}] 역산 진단 — 목표 {d['target_per_share']:,.0f} 대비 "
            f"기본안 {d['base_per_share']:,.0f}"
            + (f" ({d['gap_pct']:+.0f}%)" if d["gap_pct"] is not None else "")
            + f". 판정: {verdict_ko}. {lines} "
            f"⚠️ 이것은 **목표가에 맞춰 역산한 진단**이며 기본안(base case)이 아닙니다 — "
            f"이 숫자를 밸류에이션 결론으로 제시하지 마세요. "
            f"[{runid.line(d['run'])}]")
    return Value(
        value=None, unit="진단",
        label=f"{d['company']} 역산 진단 (목표 {d['target_per_share']:,.0f})",
        provenance=Provenance(
            source="계산엔진(engines.reverse_dcf)", source_type=SourceType.COMPUTED,
            source_url="(computed: 목표가에서 가정 역산 + 과거 밴드 대조)",
            as_of=d["as_of"], note=note),
        extras={
            k: Value(v["needed"], "%", label=f"{d['company']} {v['label']}",
                     provenance=Provenance(
                         source="계산엔진(engines.reverse_dcf)",
                         source_type=SourceType.COMPUTED,
                         source_url="(computed: 이분탐색)", as_of=d["as_of"],
                         note=f"[{WATERMARK}] {v['note']}"))
            for k, v in d["solved"].items()},
    )
