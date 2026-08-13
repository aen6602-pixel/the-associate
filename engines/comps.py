"""Trading comps 엔진 — peer 상대가치로 타깃 지분가치 추정.

peer 배수: PER = 시가총액 / 당기순이익,  PBR = 시가총액 / 자본총계   (자기자본 배수)
타깃 적용: 지분가치(이익) = 타깃 당기순이익 × median(PER),
           지분가치(장부) = 타깃 자본총계 × median(PBR)  → ÷ 발행주식수 = 주당가치

시가총액=네이버(KRX 시세), 당기순이익·자본총계=DART. peer 는 상장사여야 시가총액이 있다.
EV 배수(EV/EBITDA 등)는 순부채 소스 확보 후 추가 예정.
"""
from __future__ import annotations

import statistics

from core.schema import Provenance, Value, DataError, SourceType
from providers import dart, naver


def _peer_multiples(name: str, year: int | None) -> dict:
    ent = dart.resolve(name)
    if not ent["stock_code"]:
        raise DataError(f"{ent['corp_name']}: 비상장(시가총액 없음) → peer 제외")
    mc = naver.market_cap(ent["stock_code"], ent["corp_name"]).value
    ni = dart.financial_item(name, "net_income", year).value
    eq = dart.financial_item(name, "total_equity", year).value
    return {
        "name": ent["corp_name"], "stock_code": ent["stock_code"],
        "market_cap": mc, "net_income": ni, "equity": eq,
        "per": (mc / ni) if ni and ni > 0 else None,
        "pbr": (mc / eq) if eq and eq > 0 else None,
    }


def build_model(target: str, peers: list[str], year: int | None = None) -> dict:
    peer_rows, errors = [], []
    for p in peers:
        try:
            peer_rows.append(_peer_multiples(p, year))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{p}: {e}")
    pers = [r["per"] for r in peer_rows if r["per"]]
    pbrs = [r["pbr"] for r in peer_rows if r["pbr"]]
    if not pers and not pbrs:
        raise DataError("유효한 peer 배수를 하나도 구하지 못했습니다. " + " / ".join(errors))

    med_per = statistics.median(pers) if pers else None
    med_pbr = statistics.median(pbrs) if pbrs else None

    t_ent = dart.resolve(target)
    ni_t = dart.financial_item(target, "net_income", year)
    eq_t = dart.financial_item(target, "total_equity", year)
    sh_t = dart.shares_outstanding(target, year)
    shares = sh_t.value

    imp_per_ps = (ni_t.value * med_per / shares) if med_per and ni_t.value > 0 else None
    imp_pbr_ps = (eq_t.value * med_pbr / shares) if med_pbr else None

    return {
        "target": t_ent["corp_name"], "target_stock": t_ent["stock_code"],
        "as_of": ni_t.provenance.as_of,
        "peers": peer_rows, "errors": errors,
        "median_per": med_per, "median_pbr": med_pbr,
        "ni_t": ni_t, "eq_t": eq_t, "shares_t": sh_t,
        "implied_per_ps": imp_per_ps, "implied_pbr_ps": imp_pbr_ps,
    }


def evaluate(target: str, peers: list[str], year: int | None = None) -> Value:
    m = build_model(target, peers, year)
    f = lambda x: f"{x:,.0f}" if x is not None else "n/a"
    peer_desc = ", ".join(
        f"{r['name']} PER {r['per']:.1f}x/PBR {r['pbr']:.2f}x"
        if r["per"] and r["pbr"] else f"{r['name']}(일부 n/a)"
        for r in m["peers"]
    )
    note = (
        f"[Trading comps · 자기자본배수] peer: {peer_desc}. "
        f"median PER {m['median_per']:.1f}x, median PBR {m['median_pbr']:.2f}x. "
        f"타깃 {m['target']} (당기순이익 {f(m['ni_t'].value)}, 자본총계 {f(m['eq_t'].value)}, "
        f"주식수 {m['shares_t'].value:,}) → 주당가치: PER기준 {f(m['implied_per_ps'])}원 / "
        f"PBR기준 {f(m['implied_pbr_ps'])}원."
        + (f" (제외 peer: {'; '.join(m['errors'])})" if m["errors"] else "")
    )
    primary = m["implied_per_ps"] or m["implied_pbr_ps"]
    return Value(
        value=round(primary) if primary else None, unit="KRW/주",
        label=f"{m['target']} comps 주당가치(PER기준)",
        provenance=Provenance(source="계산엔진(engines.comps)", source_type=SourceType.COMPUTED,
                              source_url="(computed: 네이버 시총 + DART 순이익/자본)",
                              as_of=m["as_of"], note=note),
    )
