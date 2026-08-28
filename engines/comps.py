"""Trading comps 엔진 — 크로스보더 EV·자기자본 배수 표.

설계 원칙 (직전 버전이 왜 실패했는지에서 나왔다):

1. **표를 통째로 포기하지 않는다.** 한 회사의 한 항목을 못 구했다고 4개사 표 전체를
   "산출 불가" 로 끝내면 안 된다. 셀 단위로 `None` + 사유를 남기고 나머지는 계산한다.
   (실측 실패: TSMC 순부채 자동조회가 없다는 이유로 삼성전자·SK하이닉스 배수까지 포기)
2. **기준을 통일하고, 통일 못 한 것은 표에 적는다.**
   - 분자: 4개 시장 **공통 거래일** 종가 × 유통 보통주식수
   - 분모: 각 시장 LTM. LTM 이 안 되면 연간을 쓰고 `basis` 에 표시
   - 통화: 배수는 통화중립이라 환산 없이 비교하고, 절대금액만 표시통화로 환산
3. **타깃은 선택**이다. "4개사 비교표를 만들어라" 는 타깃 평가가 아니라 표 자체가 산출물이다.
4. 배수의 분모가 해석 불가면(적자 순이익의 P/E, 0 이하 EBITDA 의 EV/EBITDA) `NM` 으로 두고
   중앙값 계산에서 제외한다.
"""
from __future__ import annotations

import statistics

from core import runid
from core.schema import Provenance, Value, DataError, SourceType
from engines import market_data as md
from providers import fx

# 배수 정의 — (키, 표시명, 분자, 분모, 소수자리)
MULTIPLES = (
    ("ev_ebitda", "EV/EBITDA", "ev", "ebitda", 2),
    ("ev_ebit", "EV/EBIT", "ev", "ebit", 2),
    ("ev_revenue", "EV/Revenue", "ev", "revenue", 2),
    ("per", "P/E", "market_cap", "net_income", 2),
    ("pbr", "P/B", "market_cap", "equity", 2),
)


def _parse_spec(item: str, default_market: str) -> tuple[str, str]:
    """'MU:US' / 'US:MU' / '삼성전자' → (회사, 시장)."""
    s = (item or "").strip()
    if not s:
        raise DataError("빈 회사명이 목록에 있습니다")
    if ":" in s:
        a, b = (x.strip() for x in s.split(":", 1))
        if a.upper() in md.MARKETS:
            return b, a.upper()
        if b.upper() in md.MARKETS:
            return a, b.upper()
        raise DataError(f"'{s}' 의 시장 코드를 알 수 없습니다 (지원: {', '.join(md.MARKETS)})")
    return s, default_market


def _row(company: str, market: str, as_of: str, use_ltm: bool) -> dict:
    """한 회사의 원자료 + 배수. 실패한 항목만 None 으로 남기고 사유를 모은다."""
    spec = md.resolve(company, market)
    row: dict = {"input": company, "name": spec["name"], "market": spec["market"],
                 "currency": spec["currency"], "symbol": spec["symbol"],
                 "values": {}, "basis": {}, "missing": {}, "nm": {}}

    def take(key: str, fn):
        try:
            got = fn()
        except Exception as e:  # noqa: BLE001 — 셀 단위 실패는 표를 죽이지 않는다
            row["missing"][key] = str(e)
            return None
        if isinstance(got, tuple):
            v, basis = got
            row["basis"][key] = basis
        else:
            v = got
        row["values"][key] = v
        return v

    mc = take("market_cap", lambda: md.market_cap(spec, as_of))
    nd = take("net_debt", lambda: md.net_debt(spec))
    if use_ltm and md.supports_ltm(spec["market"]):
        ebit = take("ebit", lambda: md.ltm(spec, "operating_income"))
        da = take("da", lambda: md.ltm(spec, "da"))
        ni = take("net_income", lambda: md.ltm(spec, "net_income"))
        rev = take("revenue", lambda: md.ltm(spec, "revenue"))
    else:
        ebit = take("ebit", lambda: (md.point(spec, "operating_income"), "FY"))
        da = take("da", lambda: (md.point(spec, "da"), "FY"))
        ni = take("net_income", lambda: (md.point(spec, "net_income"), "FY"))
        rev = take("revenue", lambda: (md.point(spec, "revenue"), "FY"))
    eq = take("equity", lambda: md.point(spec, "total_equity"))

    # 파생값
    if ebit is not None and da is not None:
        row["derived_ebitda"] = ebit.value + da.value
        row["basis"]["ebitda"] = (
            row["basis"].get("ebit", "?") if row["basis"].get("ebit") == row["basis"].get("da")
            else f"EBIT={row['basis'].get('ebit')}/D&A={row['basis'].get('da')} 혼용")
    else:
        row["derived_ebitda"] = None
        row["missing"].setdefault("ebitda", "EBIT 또는 D&A 미확보로 EBITDA 산출 불가")
    if mc is not None and nd is not None:
        row["derived_ev"] = mc.value + nd.value
    else:
        row["derived_ev"] = None
        row["missing"].setdefault("ev", "시가총액 또는 순부채 미확보로 EV 산출 불가")

    nums = {"ev": row["derived_ev"], "market_cap": mc.value if mc else None}
    dens = {"ebitda": row["derived_ebitda"],
            "ebit": ebit.value if ebit else None,
            "revenue": rev.value if rev else None,
            "net_income": ni.value if ni else None,
            "equity": eq.value if eq else None}
    row["multiples"] = {}
    for key, label, num_k, den_k, _ in MULTIPLES:
        num, den = nums.get(num_k), dens.get(den_k)
        if num is None or den is None:
            row["multiples"][key] = None
            continue
        if den <= 0:
            # 분모가 해석 불가 → NM. 음수 배수를 표에 올리면 median 이 오염된다.
            row["multiples"][key] = None
            row["nm"][key] = f"{den_k} {den:,.0f} ≤ 0 → {label} 해석 불가(NM)"
            continue
        row["multiples"][key] = num / den
    return row


def _stats(rows: list[dict], key: str) -> dict | None:
    vals = sorted(r["multiples"].get(key) for r in rows
                  if r["multiples"].get(key) is not None)
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    out = {"n": len(vals), "min": vals[0], "max": vals[-1],
           "median": statistics.median(vals), "mean": statistics.fmean(vals)}
    if len(vals) >= 4:
        q = statistics.quantiles(vals, n=4, method="inclusive")
        out["q1"], out["q3"] = q[0], q[2]
    return out


def build_model(companies: list | str, target: str | None = None, market: str = "KR",
                as_of: str | None = None, basis: str = "LTM",
                display_currency: str = "USD") -> dict:
    """비교표 모델.

    companies: 비교기업 목록. 항목마다 'MU:US' 처럼 시장을 붙일 수 있고, 안 붙이면 `market`.
    target: 선택. 지정하면 median 배수를 적용해 내재 주당가치까지 계산한다.
            **비교표 자체가 산출물인 요청(4개사 표)에서는 생략한다.**
    basis: 'LTM'(기본) 또는 'FY'. LTM 을 지원하지 않는 시장은 자동으로 FY 가 되고 표에 표시된다.
    display_currency: 절대금액(시총·EV·EBITDA) 환산 통화. 배수는 통화중립이라 환산하지 않는다.
    """
    if isinstance(companies, str):
        companies = [companies]
    if not companies:
        raise DataError("비교기업 목록이 비어 있습니다")
    default_market = md.normalize_market(market)
    use_ltm = (basis or "LTM").strip().upper() != "FY"

    parsed = [_parse_spec(c, default_market) for c in companies]
    specs, resolve_errors = [], []
    for name, mkt in parsed:
        try:
            specs.append(md.resolve(name, mkt))
        except Exception as e:  # noqa: BLE001
            resolve_errors.append(f"{name}({mkt}): {e}")
    if not specs:
        raise DataError("비교기업을 하나도 식별하지 못했습니다. " + " / ".join(resolve_errors))

    # 분자 기준일: 공통 거래일 (지정이 있으면 그 날짜 이하)
    common, latest_by_name = md.common_trading_date(specs)
    price_date = min(common, as_of.replace("-", "")) if as_of else common

    rows = []
    for (name, mkt) in parsed:
        try:
            rows.append(_row(name, mkt, price_date, use_ltm))
        except Exception as e:  # noqa: BLE001
            resolve_errors.append(f"{name}({mkt}): {e}")
    if not rows:
        raise DataError("비교기업 행을 하나도 만들지 못했습니다. " + " / ".join(resolve_errors))

    stats = {key: _stats(rows, key) for key, *_ in MULTIPLES}

    # 절대금액 표시통화 환산 (배수는 환산하지 않는다)
    fx_rates, fx_errors = {}, {}
    disp = (display_currency or "USD").strip().upper()
    for r in rows:
        cur = r["currency"]
        if cur == disp or cur in fx_rates:
            continue
        try:
            fx_rates[cur] = fx.fx_rate(cur, disp, f"{price_date[:4]}-{price_date[4:6]}-{price_date[6:8]}")
        except Exception as e:  # noqa: BLE001
            fx_errors[cur] = str(e)
    for r in rows:
        rate = 1.0 if r["currency"] == disp else (
            fx_rates[r["currency"]].value if r["currency"] in fx_rates else None)
        r["fx_rate"] = rate
        r["display"] = {}
        if rate is None:
            continue
        mc = r["values"].get("market_cap")
        r["display"]["market_cap"] = mc.value * rate if mc else None
        r["display"]["ev"] = r["derived_ev"] * rate if r["derived_ev"] is not None else None
        r["display"]["ebitda"] = (r["derived_ebitda"] * rate
                                  if r["derived_ebitda"] is not None else None)
        nd = r["values"].get("net_debt")
        r["display"]["net_debt"] = nd.value * rate if nd else None

    # median 대비 프리미엄/디스카운트
    for r in rows:
        r["vs_median"] = {}
        for key, *_ in MULTIPLES:
            m, s = r["multiples"].get(key), stats.get(key)
            r["vs_median"][key] = ((m / s["median"] - 1) * 100
                                   if m is not None and s and s["median"] else None)

    model = {
        "rows": rows, "stats": stats, "errors": resolve_errors,
        "price_date": price_date, "latest_trading_date": latest_by_name,
        "basis_requested": "LTM" if use_ltm else "FY",
        "display_currency": disp, "fx": fx_rates, "fx_errors": fx_errors,
        "target": None,
    }
    model["warnings"] = _warnings(model)
    if target:
        model["target"] = _apply_to_target(target, default_market, stats, price_date, use_ltm)
    return model


def _warnings(model: dict) -> list[str]:
    """검증 경고 — 표에 반드시 같이 나가야 하는 기준 불일치."""
    out = []
    rows = model["rows"]
    bases = {r["name"]: r["basis"].get("ebitda") or r["basis"].get("ebit") for r in rows}
    mixed = {n: b for n, b in bases.items() if b and b != "LTM"}
    if mixed and any(b == "LTM" for b in bases.values()):
        out.append("기준기간 혼용: " + ", ".join(f"{n}={b}" for n, b in mixed.items())
                   + " (나머지는 LTM) → EV/EBITDA 를 나란히 비교할 때 이 차이를 감안할 것")
    fy_ends = {r["name"]: (r["values"].get("ebit").provenance.as_of if r["values"].get("ebit") else None)
               for r in rows}
    if len({v for v in fy_ends.values() if v}) > 1:
        out.append("기준기간 종료시점 상이(결산월 차이): "
                   + ", ".join(f"{n}={v}" for n, v in fy_ends.items() if v)
                   + " → LTM 창이 회사마다 몇 달씩 다르므로 사이클 업종에서는 배수가 그만큼 갈린다")
    nd_defs = [r["name"] for r in rows if r["market"] == "TW"]
    if nd_defs and any(r["market"] in ("KR", "US") for r in rows):
        out.append(f"순부채 정의 차이: {', '.join(nd_defs)}(대만)은 공시에 리스부채 계정이 "
                   f"없어 IFRS 16 리스부채가 제외됨 — 한국·미국은 포함")
    markets = {r["market"] for r in rows}
    if "US" in markets and markets - {"US"}:
        out.append("회계기준 차이: 미국(US GAAP) vs 한국·대만·일본(IFRS) — 금융자산 분류, "
                   "D&A 표시, 리스 처리가 달라 EBITDA 정의가 완전히 동일하지 않음")
    if model["fx_errors"]:
        out.append("환산 실패 통화: " + ", ".join(f"{k}({v})" for k, v in model["fx_errors"].items()))
    latest = model["latest_trading_date"]
    if len(set(latest.values())) > 1:
        out.append(f"거래일 정렬: 각 종목 최신 거래일이 달라("
                   + ", ".join(f"{n}={d}" for n, d in latest.items())
                   + f") 공통 기준일 {model['price_date']} 의 종가로 통일함")
    return out


def _apply_to_target(target: str, default_market: str, stats: dict,
                     price_date: str, use_ltm: bool) -> dict:
    """median 배수를 타깃 재무에 적용 → 내재 EV·지분가치·주당가치."""
    name, mkt = _parse_spec(target, default_market)
    spec = md.resolve(name, mkt)
    out: dict = {"name": spec["name"], "market": spec["market"], "currency": spec["currency"],
                 "values": {}, "basis": {}, "missing": {}, "implied": {}}

    def take(key, fn):
        try:
            got = fn()
        except Exception as e:  # noqa: BLE001
            out["missing"][key] = str(e)
            return None
        if isinstance(got, tuple):
            v, b = got
            out["basis"][key] = b
        else:
            v = got
        out["values"][key] = v
        return v

    if use_ltm and md.supports_ltm(spec["market"]):
        ebit = take("ebit", lambda: md.ltm(spec, "operating_income"))
        da = take("da", lambda: md.ltm(spec, "da"))
        ni = take("net_income", lambda: md.ltm(spec, "net_income"))
        rev = take("revenue", lambda: md.ltm(spec, "revenue"))
    else:
        ebit = take("ebit", lambda: (md.point(spec, "operating_income"), "FY"))
        da = take("da", lambda: (md.point(spec, "da"), "FY"))
        ni = take("net_income", lambda: (md.point(spec, "net_income"), "FY"))
        rev = take("revenue", lambda: (md.point(spec, "revenue"), "FY"))
    eq = take("equity", lambda: md.point(spec, "total_equity"))
    nd = take("net_debt", lambda: md.net_debt(spec))
    sh = take("shares", lambda: md.shares(spec))

    ebitda = (ebit.value + da.value) if (ebit and da) else None
    out["ebitda"] = ebitda
    shares = sh.value if sh else None

    def ev_based(stat_key, denom):
        s = stats.get(stat_key)
        if not s or denom is None or denom <= 0 or nd is None:
            return None
        ev = s["median"] * denom
        eqv = ev - nd.value
        return {"multiple": s["median"], "denominator": denom, "ev": ev, "equity_value": eqv,
                "per_share": (eqv / shares) if shares else None}

    def eq_based(stat_key, denom):
        s = stats.get(stat_key)
        if not s or denom is None or denom <= 0:
            return None
        eqv = s["median"] * denom
        return {"multiple": s["median"], "denominator": denom, "ev": None, "equity_value": eqv,
                "per_share": (eqv / shares) if shares else None}

    out["implied"] = {
        "ev_ebitda": ev_based("ev_ebitda", ebitda),
        "ev_ebit": ev_based("ev_ebit", ebit.value if ebit else None),
        "ev_revenue": ev_based("ev_revenue", rev.value if rev else None),
        "per": eq_based("per", ni.value if ni else None),
        "pbr": eq_based("pbr", eq.value if eq else None),
    }
    return out


# ── LLM 도구용 요약 Value ──────────────────────────────────────────
def evaluate(companies: list | str, target: str | None = None, market: str = "KR",
             as_of: str | None = None, basis: str = "LTM",
             display_currency: str = "USD") -> Value:
    m = build_model(companies, target, market, as_of, basis, display_currency)
    run = runid.stamp("comps", {"companies": companies, "target": target,
                                 "market": market, "as_of": as_of, "basis": basis,
                                 "display_currency": display_currency})
    m["run"] = run
    lines = []
    for r in m["rows"]:
        parts = []
        for key, label, *_ in MULTIPLES:
            v = r["multiples"].get(key)
            if v is not None:
                parts.append(f"{label} {v:.2f}x")
            elif key in r["nm"]:
                parts.append(f"{label} NM")
        b = r["basis"].get("ebitda") or r["basis"].get("ebit") or "?"
        lines.append(f"{r['name']}[{r['market']}, {b}] " + ", ".join(parts) or r["name"])
    med = ", ".join(f"{label} {m['stats'][key]['median']:.2f}x (n={m['stats'][key]['n']})"
                    for key, label, *_ in MULTIPLES if m["stats"].get(key))
    note = (f"[Trading comps · 기준일 {m['price_date']} 종가, 분모 {m['basis_requested']}] "
            + " / ".join(lines) + f". median: {med}."
            + (f" 표시통화 {m['display_currency']}." if m["fx"] else ""))
    if m["target"]:
        t = m["target"]
        ps = {k: v["per_share"] for k, v in t["implied"].items() if v and v["per_share"]}
        if ps:
            note += (f" 타깃 {t['name']} 내재 주당가치: "
                     + ", ".join(f"{k} 기준 {v:,.0f}" for k, v in ps.items())
                     + f" {t['currency']}/주.")
    miss = {f"{r['name']}.{k}" for r in m["rows"] for k in r["missing"]}
    if miss:
        note += f" 미확보 항목: {', '.join(sorted(miss))}."
    if m["errors"]:
        note += f" 제외: {'; '.join(m['errors'])}."
    if m["warnings"]:
        note += " ⚠️ " + " ⚠️ ".join(m["warnings"])
    note += f" [{runid.line(run)}]"

    primary = None
    if m["target"]:
        for k in ("ev_ebitda", "per", "pbr"):
            got = m["target"]["implied"].get(k)
            if got and got["per_share"]:
                primary = got["per_share"]
                break
    unit = f"{m['target']['currency']}/주" if m["target"] else "개 비교기업"
    return Value(
        value=round(primary) if primary else len(m["rows"]), unit=unit,
        label=(f"{m['target']['name']} comps 내재 주당가치" if m["target"]
               else f"Trading comps 비교표 ({len(m['rows'])}개사, {m['price_date']} 기준)"),
        provenance=Provenance(
            source="계산엔진(engines.comps)", source_type=SourceType.COMPUTED,
            source_url="(computed: 시장별 공시재무 + 거래소 시세)",
            as_of=m["price_date"], note=note),
        extras=_extras(m),
    )


def _extras(m: dict) -> dict:
    """UI·LLM 이 셀 단위로 인용할 수 있게 주요 Value 를 평평하게 펼친다."""
    out: dict = {}
    for r in m["rows"]:
        for k, v in r["values"].items():
            if isinstance(v, Value):
                out[f"{r['name']}.{k}"] = v
    for cur, v in m["fx"].items():
        out[f"FX.{cur}"] = v
    return out
