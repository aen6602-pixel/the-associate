"""DCF 입력 자동 도출 엔진 — 공시에서 나오는 값은 사용자에게 묻지 않는다.

지금까지 `compute_dcf` 는 순부채·D&A%·CAPEX%·ΔNWC%·terminal g·WACC 를 전부 사용자 가정으로
받았다. 그런데 이 중 대부분은 **DART 공시에 이미 있는 값**이다. 이 엔진이 그것들을 뽑아
`compute_dcf` 가 바로 쓸 수 있는 형태로 만들어 준다.

원칙은 그대로다 — **숫자는 LLM 이 만들지 않는다.** 여기서 나오는 값은 전부
  · 공시 원본(authoritative) 또는
  · 공시 원본으로 계산한 파생값(computed)
이고, 데이터가 없으면 조용히 0/평균으로 때우지 않고 `None` + 이유를 남긴다.

`engines.dcf` 의 파라미터 규약에 맞춘다:
  da_pct    = D&A / 매출
  capex_pct = CAPEX / 매출
  nwc_pct   = ΔNWC / Δ매출        (dcf.build_model 이 `(rev - prev) * nwc` 로 쓴다)
  ΔNWC 부호: 현금흐름표의 '영업활동으로 인한 자산부채의 변동' 은 **현금 영향**이라
            운전자본이 늘면 음수로 찍힌다(삼성전자 FY2025 −9.6조 실측) → ΔNWC = −(그 값).
"""
from __future__ import annotations

from statistics import mean

from core.schema import DataError, Provenance, SourceType, Value
from providers import damodaran, dart, dart_audit, ecos, fred


def _audit(company: str, year: int | None = None) -> dict:
    """비상장(외감법인) 폴백 — 감사보고서 원문 표에서 DCF 입력을 뽑는다.

    상장사는 DART 정형 API 로 되지만 외감법인은 그 API 에 데이터가 없다(013 오류).
    호출부는 1차 경로가 DataError 를 낼 때만 이걸 쓴다."""
    ent = dart.resolve(company)
    d = dart_audit.dcf_inputs(ent["corp_code"], year)
    d["_name"] = ent["corp_name"]
    return d


def _audit_prov(d: dict, field: str, note: str) -> Provenance:
    return Provenance(
        source="DART 감사보고서(외감법인 원문)", source_type=SourceType.AUTHORITATIVE,
        source_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={d['_rcept']}",
        original_field=field, as_of=f"FY{d['_year']}", note=note)


def _computed(value, unit: str, label: str, note: str, as_of: str | None = None,
              extras: dict | None = None) -> Value:
    return Value(
        value=value, unit=unit, label=label,
        provenance=Provenance(source="계산엔진(engines.dcf_inputs)",
                              source_type=SourceType.COMPUTED,
                              source_url="(computed from DART 공시)",
                              as_of=as_of, note=note),
        extras=extras or {},
    )


# ── 순부채 ────────────────────────────────────────────────────────
def net_debt(company: str, year: int | None = None, include_lease: bool = True,
             report: str = "annual", prefer: str = "CFS") -> Value:
    """순부채 = 이자발생부채(IBD) − 현금및현금성자산.

    IBD = 단기차입금(유동성장기부채 포함) + 장기차입금·사채 (+ 리스부채, IFRS 16).
    리스부채를 포함하는 게 기본값이다 — D&A 에 사용권자산상각비가 들어가 있으므로
    분자·분모의 처리를 일치시킨다. 음수면 순현금(net cash) 상태다."""
    try:
        db = dart.debt_balances(company, year, report, prefer)
        cash = dart.financial_item(company, "cash", year, report, prefer)
    except DataError:
        return _net_debt_from_audit(company, year, include_lease)

    st, lt = db["short_term"].value, db["long_term"].value
    lease = db["lease"].value if include_lease else 0
    ibd = st + lt + lease
    nd = ibd - cash.value

    f = lambda x: f"{x:,.0f}"  # noqa: E731
    lease_txt = f" + 리스부채 {f(lease)}" if include_lease and lease else ""
    note = (f"IBD {f(ibd)} (단기 {f(st)} + 장기 {f(lt)}{lease_txt}) "
            f"− 현금및현금성자산 {f(cash.value)} = 순부채 {f(nd)}"
            f"{' → 순현금 상태' if nd < 0 else ''}"
            + ("" if include_lease else " (리스부채 제외)"))
    return _computed(nd, "KRW", f"{db['short_term'].label.split(' 단기')[0]} 순부채",
                     note, cash.provenance.as_of,
                     extras={"interest_bearing_debt": _computed(
                         ibd, "KRW", "이자발생부채(IBD)",
                         f"단기 {f(st)} + 장기 {f(lt)}{lease_txt}", cash.provenance.as_of),
                             "cash": cash})


def _net_debt_from_audit(company: str, year: int | None, include_lease: bool) -> Value:
    d = _audit(company, year)
    if "cash" not in d:
        raise DataError(
            f"{d['_name']} 감사보고서에서 현금및현금성자산을 찾지 못해 순부채를 계산할 수 없습니다.")
    st = d.get("short_term_debt", {}).get("amount", 0)
    lt = d.get("long_term_debt", {}).get("amount", 0)
    lease = d.get("lease_liability", {}).get("amount", 0) if include_lease else 0
    cash = d["cash"]["amount"]
    ibd = st + lt + lease
    nd = ibd - cash
    f = lambda x: f"{x:,.0f}"  # noqa: E731
    note = (f"[비상장·감사보고서] IBD {f(ibd)} (단기 {f(st)} + 장기 {f(lt)}"
            + (f" + 리스 {f(lease)}" if lease else "")
            + f") − 현금 {f(cash)} = 순부채 {f(nd)}"
            + (" → 순현금 상태" if nd < 0 else "")
            + ". 감사보고서 본표에서 못 찾은 차입금 항목은 0 으로 잡히므로 과소계상 가능 — "
              "note 의 구성요소를 확인할 것.")
    return Value(nd, "KRW", label=f"{d['_name']} 순부채",
                 provenance=_audit_prov(d, "재무상태표/차입금·현금", note),
                 extras={"cash": Value(cash, "KRW", label=f"{d['_name']} 현금및현금성자산",
                                       provenance=_audit_prov(d, "재무상태표/현금및현금성자산",
                                                              "감사보고서 본표"))})


# ── 5개년 실적 비율 ───────────────────────────────────────────────
def _series_map(payload: dict, key: str = "series") -> dict[int, int | None]:
    return {p["year"]: p["amount"] for p in payload[key]}


def historical_ratios(company: str, n: int = 5, year: int | None = None,
                      report: str = "annual", prefer: str = "CFS") -> dict:
    """최근 n개년 실적에서 DCF 가정의 출발점이 되는 비율들을 계산한다.

    돌려주는 dict 의 각 값은 Value(단위 %) 또는 None(데이터 없음).
    각 Value 의 extras 에 연도별 값이 들어가므로 사용자가 눈으로 검증할 수 있다.
    """
    rev = dart.financial_item_nyear(company, "revenue", n, year, report, prefer)
    ebit = dart.financial_item_nyear(company, "operating_income", n, year, report, prefer)
    cf = dart.cf_extras_nyear(company, n, year, report, prefer)

    revs = _series_map(rev)
    ebits = _series_map(ebit)
    capex = {p["year"]: p["amount"] for p in cf["capex"]}
    capex_int = {p["year"]: p["amount"] for p in cf.get("capex_intangible", [])}
    das = {p["year"]: p["amount"] for p in cf["da"]}
    nwcs = {p["year"]: p["amount"] for p in cf["nwc_change"]}

    years = sorted(revs, reverse=True)          # 최신 → 과거
    asc = list(reversed(years))                 # 과거 → 최신
    name = rev["corp_name"]
    as_of = f"FY{years[0]}" if years else None

    # D&A 가 현금흐름표에 없으면(삼성전자·SK하이닉스류) 최신연도만 주석에서 보완한다.
    da_note_extra = ""
    if all(das.get(y) is None for y in years):
        try:
            v = dart.da_best(company, years[0] if years else None, report, prefer)
            das[years[0]] = v.value
            da_note_extra = (" D&A 는 현금흐름표에 분리 공시되지 않아 최신연도만 "
                             "비용의 성격별 분류 주석에서 추출(연도별 평균 아님).")
        except DataError:
            pass

    def _ratio_of_revenue(amounts: dict, label: str, unit_label: str,
                          extra_note: str = "") -> Value | None:
        pairs = [(y, abs(amounts[y]) / revs[y]) for y in years
                 if amounts.get(y) is not None and revs.get(y)]
        if not pairs:
            return None
        avg = mean(r for _, r in pairs) * 100
        detail = ", ".join(f"{y} {r * 100:.2f}%" for y, r in pairs)
        return _computed(round(avg, 2), "%", f"{name} {label}",
                         f"{len(pairs)}개년 산술평균 ({unit_label}). 연도별: {detail}."
                         f"{extra_note}", as_of)

    # EBIT 마진
    ebit_pairs = [(y, ebits[y] / revs[y]) for y in years
                  if ebits.get(y) is not None and revs.get(y)]
    ebit_margin = None
    if ebit_pairs:
        ebit_margin = _computed(
            round(mean(r for _, r in ebit_pairs) * 100, 2), "%", f"{name} 영업이익률(5개년 평균)",
            f"{len(ebit_pairs)}개년 산술평균. 연도별: "
            + ", ".join(f"{y} {r * 100:.2f}%" for y, r in ebit_pairs) + ".", as_of)

    # 매출성장률 — 산술평균과 CAGR 둘 다 제공(정의에 따라 달라지므로 명시)
    growth = None
    yoy = [(asc[i], revs[asc[i]] / revs[asc[i - 1]] - 1)
           for i in range(1, len(asc))
           if revs.get(asc[i]) and revs.get(asc[i - 1])]
    if yoy:
        arith = mean(g for _, g in yoy) * 100
        first, last = revs[asc[0]], revs[asc[-1]]
        periods = len(asc) - 1
        cagr = ((last / first) ** (1 / periods) - 1) * 100 if first and periods else None
        growth = _computed(
            round(arith, 2), "%", f"{name} 매출성장률(연평균)",
            f"YoY 산술평균 {arith:.2f}%"
            + (f" · CAGR({asc[0]}→{asc[-1]}) {cagr:.2f}%" if cagr is not None else "")
            + ". 연도별: " + ", ".join(f"{y} {g * 100:+.2f}%" for y, g in yoy) + ".", as_of,
            extras={"cagr": _computed(round(cagr, 2), "%", f"{name} 매출 CAGR",
                                      f"{asc[0]}→{asc[-1]} {periods}년", as_of)}
            if cagr is not None else None)

    # CAPEX — 유형 + 무형(무형이 있으면 합산: D&A 에 무형자산상각비가 들어가므로 대응)
    capex_total = {}
    for y in years:
        t = capex.get(y)
        if t is None:
            continue
        capex_total[y] = abs(t) + abs(capex_int.get(y) or 0)
    capex_pct = _ratio_of_revenue(capex_total, "CAPEX/매출(5개년 평균)",
                                  "유형자산 취득 + 무형자산 취득")

    da_pct = _ratio_of_revenue(das, "D&A/매출(5개년 평균)",
                               "감가상각비+무형자산상각비", da_note_extra)

    # ΔNWC / Δ매출 — 부호 반전 필요(CF 는 현금영향)
    nwc_pct = None
    nwc_pairs = []
    for i in range(1, len(asc)):
        y, prev = asc[i], asc[i - 1]
        if nwcs.get(y) is None or not revs.get(y) or not revs.get(prev):
            continue
        d_rev = revs[y] - revs[prev]
        if d_rev == 0:
            continue
        nwc_pairs.append((y, (-nwcs[y]) / d_rev))
    nwc_basis = ("현금흐름표 '영업활동으로 인한 자산부채의 변동'은 현금영향이라 부호를 반전해 "
                 "ΔNWC 로 환산")

    # 폴백: 현금흐름표에 '자산부채의 변동' 합계가 없는 회사(SK하이닉스·네이버 실측)는
    # 재무상태표에서 NWC = 매출채권 + 재고자산 − 매입채무 를 만들어 연도별 증감으로 계산한다.
    if not nwc_pairs:
        wc: dict[int, int] = {}
        try:
            ar = _series_map(dart.financial_item_nyear(
                company, "trade_receivables", n, year, report, prefer))
            inv = _series_map(dart.financial_item_nyear(
                company, "inventories", n, year, report, prefer))
            ap = _series_map(dart.financial_item_nyear(
                company, "trade_payables", n, year, report, prefer))
            for y in years:
                if ar.get(y) is None or inv.get(y) is None or ap.get(y) is None:
                    continue
                wc[y] = ar[y] + inv[y] - ap[y]
        except DataError:
            wc = {}
        for i in range(1, len(asc)):
            y, prev = asc[i], asc[i - 1]
            if y not in wc or prev not in wc or not revs.get(y) or not revs.get(prev):
                continue
            d_rev = revs[y] - revs[prev]
            if d_rev == 0:
                continue
            nwc_pairs.append((y, (wc[y] - wc[prev]) / d_rev))
        if nwc_pairs:
            nwc_basis = ("현금흐름표에 자산부채 변동 합계가 없어 재무상태표에서 "
                         "NWC = 매출채권 + 재고자산 − 매입채무 의 연도별 증감으로 산출")

    if nwc_pairs:
        nwc_pct = _computed(
            round(mean(r for _, r in nwc_pairs) * 100, 2), "%",
            f"{name} ΔNWC/Δ매출({len(nwc_pairs)}개년 평균)",
            f"{len(nwc_pairs)}개년 산술평균. {nwc_basis}. 연도별: "
            + ", ".join(f"{y} {r * 100:+.1f}%" for y, r in nwc_pairs)
            + ". 매출 감소 연도가 섞이면 부호가 뒤집혀 평균이 왜곡될 수 있으니 확인 필요.", as_of)

    missing = [k for k, v in (("ebit_margin_pct", ebit_margin), ("revenue_growth_pct", growth),
                              ("da_pct", da_pct), ("capex_pct", capex_pct),
                              ("nwc_pct", nwc_pct)) if v is None]
    return {
        "company": name, "years": years, "as_of": as_of,
        "ebit_margin_pct": ebit_margin, "revenue_growth_pct": growth,
        "da_pct": da_pct, "capex_pct": capex_pct, "nwc_pct": nwc_pct,
        "missing": missing,
    }


# ── 세전 타인자본비용 ─────────────────────────────────────────────
def cost_of_debt(company: str, year: int | None = None, include_lease: bool = True,
                 report: str = "annual", prefer: str = "CFS") -> Value:
    """Kd(세전) = 이자비용 / 이자발생부채.

    손익의 '금융비용'(ifrs-full_FinanceCosts)은 환차손·파생손실을 포함해 쓸 수 없다
    (삼성전자 FY2025 실측: 금융비용 11.7조/차입금 24.1조 → 48.8% 라는 비상식적 값).
    현금흐름표의 이자 전용 계정(이자비용 조정 또는 이자의 지급)을 쓴다.
    기말 잔액 기준이라 차입금이 급변한 해에는 왜곡될 수 있어 note 에 밝힌다."""
    try:
        cf = dart.cf_extras(company, year, report, prefer)
        db = dart.debt_balances(company, year, report, prefer)
    except DataError:
        return _cost_of_debt_from_audit(company, year, include_lease)

    interest = cf.get("interest")
    if interest is None:
        raise DataError(
            f"{company} 의 이자비용을 현금흐름표에서 찾지 못했습니다. Kd 를 직접 지정하거나 "
            f"산업평균(get_industry_benchmarks)을 쓰세요.")

    lease = db["lease"].value if include_lease else 0
    ibd = db["short_term"].value + db["long_term"].value + lease
    if ibd <= 0:
        raise DataError(
            f"{company} 는 이자발생부채가 0 입니다(무차입 경영) → 이자비용/차입금으로 Kd 를 "
            f"계산할 수 없습니다. 산업평균 Kd 또는 무위험수익률+스프레드를 쓰세요.")

    kd = interest.value / ibd * 100
    f = lambda x: f"{x:,.0f}"  # noqa: E731
    note = (f"이자비용 {f(interest.value)} ÷ IBD {f(ibd)} = {kd:.2f}%. "
            f"{interest.provenance.original_field}. 기말 잔액 기준(기중 평균 아님) — "
            f"차입금이 급변한 해는 왜곡 가능.")
    if kd > 15 or kd < 0.3:
        note += f" ⚠️ {kd:.2f}% 는 통상 범위(0.3~15%)를 벗어남 — 검토 필요."
    return _computed(round(kd, 2), "%", f"{interest.label.split(' 이자')[0]} 세전 타인자본비용(Kd)",
                     note, interest.provenance.as_of, extras={"interest_expense": interest})


def _cost_of_debt_from_audit(company: str, year: int | None, include_lease: bool) -> Value:
    d = _audit(company, year)
    if "interest_paid" not in d:
        raise DataError(
            f"{d['_name']} 감사보고서 현금흐름표에서 '이자의 지급' 을 찾지 못했습니다. "
            f"Kd 를 직접 지정하거나 산업평균(get_industry_benchmarks)을 쓰세요.")
    st = d.get("short_term_debt", {}).get("amount", 0)
    lt = d.get("long_term_debt", {}).get("amount", 0)
    lease = d.get("lease_liability", {}).get("amount", 0) if include_lease else 0
    ibd = st + lt + lease
    if ibd <= 0:
        raise DataError(f"{d['_name']} 는 감사보고서상 이자발생부채가 0 이라 Kd 를 계산할 수 "
                        f"없습니다. 산업평균 Kd 를 쓰세요.")
    interest = d["interest_paid"]["amount"]
    kd = interest / ibd * 100
    f = lambda x: f"{x:,.0f}"  # noqa: E731
    note = (f"[비상장·감사보고서] 이자의 지급 {f(interest)} ÷ IBD {f(ibd)} = {kd:.2f}% "
            f"(현금주의, 기말 잔액 기준).")
    if kd > 15 or kd < 0.3:
        note += f" ⚠️ {kd:.2f}% 는 통상 범위(0.3~15%)를 벗어남 — 검토 필요."
    return Value(round(kd, 2), "%", label=f"{d['_name']} 세전 타인자본비용(Kd)",
                 provenance=_audit_prov(d, "현금흐름표/이자의 지급", note))


# ── 영구성장률 ────────────────────────────────────────────────────
def terminal_growth(country: str = "KR", tenor: str = "10Y") -> Value:
    """영구성장률 g. Damodaran 원칙: **g 는 무위험수익률을 넘을 수 없다**
    (영구히 경제보다 빨리 크는 기업은 없다 → 국채수익률이 명목 경제성장률의 상한 대용).
    그래서 g = 해당 국가 10년 국채수익률로 잡고, 원본 Rf 를 extras 로 함께 돌려준다.
    더 보수적으로 가려면 이 값보다 낮게 직접 지정하면 된다."""
    c = (country or "KR").strip().upper()
    rf = ecos.risk_free_rate(tenor) if c == "KR" else fred.risk_free_rate(tenor)
    if c not in ("KR", "US"):
        raise DataError(f"영구성장률 산정 미지원 국가: {country} (현재 KR, US)")
    return _computed(
        rf.value, "%", f"영구성장률 g ({c})",
        f"Damodaran 원칙(g ≤ 무위험수익률)에 따라 {c} 국채 {tenor} 수익률 {rf.value}% 를 g 의 "
        f"상한으로 채택 ({rf.provenance.source}, {rf.provenance.as_of}). 영구히 명목 경제성장률을 "
        f"초과하는 성장은 불가능하다는 전제. 더 보수적으로 보려면 이보다 낮게 지정.",
        rf.provenance.as_of, extras={"risk_free_rate": rf})
