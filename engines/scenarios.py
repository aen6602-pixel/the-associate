"""시나리오 3종(Base/Bull/Bear) 동시 산출 + 확률 가중.

왜 필요한가 — 엔진이 단일 값 하나만 내면 "그래서 얼마냐" 에 점 추정으로 답하게 된다.
IC 자료는 점이 아니라 범위로 움직이고, 무엇이 그 범위를 만드는지(어떤 가정이 얼마나
민감한지)가 결론만큼 중요하다.

설계 원칙 둘.

1. **시나리오는 가정의 델타로 정의한다.** 세 벌의 완전한 가정 세트를 받으면 사용자가
   시나리오 간에 무엇을 바꿨는지 추적할 수 없고, 실수로 여러 항목이 동시에 바뀐다.
   여기서는 base 하나 + 성장·마진 델타만 받아, "무엇을 얼마나 다르게 봤는지" 가 명시된다.
2. **확률 가중값을 결론으로 앞세우지 않는다.** 가중평균은 세 시나리오를 하나의 점으로
   다시 뭉개는 것이라, 범위를 보여주려던 목적과 어긋난다. 참고값으로만 낸다.
"""
from __future__ import annotations

from core import runid
from core.schema import DataError, Provenance, SourceType, Value
from engines import dcf as dcf_engine

DEFAULT_PROBS = (0.25, 0.50, 0.25)   # bear / base / bull
LABELS = {"bear": "Bear", "base": "Base", "bull": "Bull"}


def _shift(value, delta: float):
    """스칼라든 연도별 벡터든 같은 규약으로 델타를 더한다."""
    if isinstance(value, (int, float)):
        return float(value) + delta
    return [float(x) + delta for x in value]


def build(company: str, wacc_pct: float, net_debt: float, revenue_growth,
          ebit_margin_pct, da_pct: float, capex_pct: float, nwc_pct: float,
          terminal_growth_pct: float, forecast_years: int = 5,
          tax_rate_pct: float | None = None, year: int | None = None,
          market: str = "KR", allow_mixed: bool = False,
          bull_growth_delta_pct: float = 2.0, bull_margin_delta_pct: float = 1.0,
          bear_growth_delta_pct: float = -2.0, bear_margin_delta_pct: float = -1.0,
          bull_terminal_growth_delta_pct: float = 0.0,
          bear_terminal_growth_delta_pct: float = 0.0,
          probabilities: list | None = None) -> dict:
    probs = tuple(float(x) for x in (probabilities or DEFAULT_PROBS))
    if len(probs) != 3:
        raise DataError("probabilities 는 [bear, base, bull] 3개여야 합니다.")
    total = sum(probs)
    if total <= 0:
        raise DataError("probabilities 합이 0 보다 커야 합니다.")
    probs = tuple(p / total for p in probs)   # 합이 1 이 아니어도 정규화해 받아준다

    deltas = {
        "bear": (bear_growth_delta_pct, bear_margin_delta_pct,
                 bear_terminal_growth_delta_pct),
        "base": (0.0, 0.0, 0.0),
        "bull": (bull_growth_delta_pct, bull_margin_delta_pct,
                 bull_terminal_growth_delta_pct),
    }

    cases: dict = {}
    for name, (dg, dm, dgt) in deltas.items():
        gt = float(terminal_growth_pct) + dgt
        if gt >= float(wacc_pct):
            raise DataError(
                f"{LABELS[name]}: 영구성장률 {gt:.2f}% 가 WACC {wacc_pct:.2f}% 이상이라 "
                f"TV 가 성립하지 않습니다. terminal_growth 델타를 줄이세요.")
        # 시나리오마다 시장 대조를 다시 돌리면 같은 시세를 3번 조회한다 → base 만 켠다.
        m = dcf_engine.build_model(
            company, wacc_pct, net_debt, _shift(revenue_growth, dg),
            _shift(ebit_margin_pct, dm), da_pct, capex_pct, nwc_pct, gt,
            forecast_years, tax_rate_pct, year, market=market, allow_mixed=allow_mixed,
            skip_market_check=(name != "base"))
        cases[name] = m

    base = cases["base"]
    valid = {k: v for k, v in cases.items() if not v["per_share_is_nm"]}
    weighted = None
    if len(valid) == 3:
        weighted = sum(cases[k]["per_share"] * p
                       for k, p in zip(("bear", "base", "bull"), probs))

    run = runid.stamp("scenarios", {
        "company": company, "wacc_pct": wacc_pct, "net_debt": net_debt,
        "revenue_growth": revenue_growth, "ebit_margin_pct": ebit_margin_pct,
        "da_pct": da_pct, "capex_pct": capex_pct, "nwc_pct": nwc_pct,
        "terminal_growth_pct": terminal_growth_pct, "forecast_years": forecast_years,
        "market": market, "deltas": deltas, "probabilities": probs})

    return {"company": base["company"], "market": market, "cases": cases,
            "probabilities": dict(zip(("bear", "base", "bull"), probs)),
            "weighted_per_share": weighted, "deltas": deltas,
            "as_of": base["as_of"], "currency": dcf_engine._CURRENCY.get(market, market),
            "run": run}


def evaluate(company: str, **kw) -> Value:
    d = build(company, **kw)
    cur = d["currency"]
    f = lambda x: f"{x:,.0f}"  # noqa: E731

    parts, extras = [], {}
    for name in ("bear", "base", "bull"):
        m = d["cases"][name]
        dg, dm, dgt = d["deltas"][name]
        tag = LABELS[name]
        delta_txt = (f"성장 {dg:+g}%p, 마진 {dm:+g}%p"
                     + (f", g {dgt:+g}%p" if dgt else "")) if name != "base" else "기준"
        if m["per_share_is_nm"]:
            parts.append(f"{tag}({delta_txt}): 산출 불가(NM — {', '.join(m['blocking'])})")
            continue
        parts.append(f"{tag}({delta_txt}) {f(m['per_share'])}{cur}/주")
        extras[f"{name}_per_share"] = Value(
            round(m["per_share"]), f"{cur}/주", label=f"{d['company']} {tag} 주당가치",
            provenance=Provenance(
                source="계산엔진(engines.scenarios)", source_type=SourceType.COMPUTED,
                source_url="(computed: DCF × 시나리오 델타)", as_of=d["as_of"],
                note=(f"{tag}: {delta_txt}. EV {f(m['ev'])}, 지분가치 {f(m['equity_value'])}. "
                      + (" | ".join(m["warnings"]) if m["warnings"] else "검증 경고 없음"))))

    spread = ""
    valid = [d["cases"][k]["per_share"] for k in ("bear", "base", "bull")
             if not d["cases"][k]["per_share_is_nm"]]
    if len(valid) >= 2:
        lo, hi = min(valid), max(valid)
        spread = f" 범위 {f(lo)}~{f(hi)}{cur}/주"
        if lo > 0:
            spread += f" (Bear 대비 Bull {hi / lo - 1:+.0%})"
        spread += "."

    weighted_txt = ""
    if d["weighted_per_share"] is not None:
        p = d["probabilities"]
        weighted_txt = (f" 확률가중 {f(d['weighted_per_share'])}{cur}/주 "
                        f"(bear {p['bear']:.0%}/base {p['base']:.0%}/bull {p['bull']:.0%}) — "
                        f"⚠️ 이 값은 참고용이다. 세 시나리오를 하나의 점으로 다시 뭉개는 것이라 "
                        f"범위를 보여주려던 목적과 어긋나므로, 결론은 범위로 제시하라.")

    base_m = d["cases"]["base"]
    reality = dcf_engine.reality_check.summary_line(base_m["reality"], cur)
    note = (reality + "[시나리오 3종] " + " / ".join(parts) + "."
            + spread + weighted_txt
            + f" [{runid.line(d['run'])}]")

    return Value(
        value=None if base_m["per_share_is_nm"] else round(base_m["per_share"]),
        unit=f"{cur}/주", label=f"{d['company']} 시나리오 밸류에이션 (Base 기준)",
        provenance=Provenance(
            source="계산엔진(engines.scenarios)", source_type=SourceType.COMPUTED,
            source_url="(computed: Base/Bull/Bear DCF)", as_of=d["as_of"], note=note),
        extras=extras,
    )
