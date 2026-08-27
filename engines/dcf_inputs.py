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

from statistics import mean, median

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
    # 캡티브 금융 보유사의 연결 IBD 에는 금융부문 조달이 전부 들어 있다. 이 값을 EV 에서
    # 그대로 차감하면 과다차감이 되므로(현대자동차 실측: IBD 131조 중 상당액이 금융부문)
    # 값을 감추지 않고 **오염 사실을 같이 실어 보낸다**.
    note += _finance_arm_note(company, year, prefer)
    return _computed(nd, "KRW", f"{db['short_term'].label.split(' 단기')[0]} 순부채",
                     note, cash.provenance.as_of,
                     extras={"interest_bearing_debt": _computed(
                         ibd, "KRW", "이자발생부채(IBD)",
                         f"단기 {f(st)} + 장기 {f(lt)}{lease_txt}", cash.provenance.as_of),
                             "cash": cash})


def _finance_arm_note(company: str, year: int | None, prefer: str) -> str:
    """금융부문 보유 판정 결과를 note 문구로. 판정 실패는 빈 문자열(계산을 막지 않는다)."""
    try:
        from engines import business_mix

        d = business_mix.classify(company, year, prefer, deep=False)
    except Exception:  # noqa: BLE001
        return ""
    if d["single_dcf_ok"]:
        return ""
    split = None
    try:
        split = business_mix.split_finance_debt(company, year, "annual", prefer)
    except Exception:  # noqa: BLE001
        pass
    msg = (f" ⚠️ [금융부문 오염] {d['reason']} 이 순부채는 **연결 기준**이라 금융부문 조달이 "
           f"포함돼 있고, 제조부문 EV 에서 그대로 차감하면 과다차감이 됩니다.")
    if split and split["confident"]:
        msg += (f" 계정명 기준 분해: 금융 {split['finance']:,} / 제조 "
                f"{split['industrial']:,} (근거 {'; '.join(split['finance_rows'][:3])}).")
    elif split:
        msg += f" 차입금 분해 불가 — {split['basis']}. 세그먼트 재무가 필요합니다."
    return msg


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


# ΔNWC/Δ매출 안전장치 상수
NWC_MIN_REV_CHANGE = 0.02   # |Δ매출|/매출 이 2% 미만인 연도는 비율 계산에서 제외
NWC_SANITY_LIMIT = 30.0     # |ΔNWC/Δ매출| 이 30% 를 넘으면 자동 채택 금지


def historical_ratios(company: str, n: int = 5, year: int | None = None,
                      report: str = "annual", prefer: str = "CFS",
                      narrow_nwc: bool | None = None) -> dict:
    """최근 n개년 실적에서 DCF 가정의 출발점이 되는 비율들을 계산한다.

    돌려주는 dict 의 각 값은 Value(단위 %) 또는 None(데이터 없음).
    각 Value 의 extras 에 연도별 값이 들어가므로 사용자가 눈으로 검증할 수 있다.

    narrow_nwc: 운전자본을 **좁은 정의**(매출채권+재고-매입채무)로 강제한다.
      None(기본)이면 engines.business_mix 로 판정해 금융부문이 섞인 회사에 자동 적용한다.
      현금흐름표의 '자산부채의 변동' 집계에는 금융업채권·할부금융자산 증감이 함께 들어 있어
      캡티브 금융 보유사에서 ΔNWC 가 폭발한다(현대자동차 실측 161.51%).
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

    # ── ΔNWC / Δ매출 ──────────────────────────────────────────────
    # 안전장치 3겹: (1) 좁은 정의 강제(금융부문 보유사) (2) Δ매출 분모 하한 (3) 상한 게이트.
    # 실측 실패(현대자동차): 현금흐름표 '자산부채의 변동' 집계에 금융업채권 증감이 포함돼
    # ΔNWC/Δ매출 161.51% -> 5개년 UFCF 전부 음수 -> 주당 -5,042,055원.
    if narrow_nwc is None:
        try:
            from engines import business_mix

            narrow_nwc = not business_mix.classify(company, year, prefer,
                                                   deep=False)["single_dcf_ok"]
        except Exception:  # noqa: BLE001 — 판정 실패는 계산을 막지 않는다(기존 경로 유지)
            narrow_nwc = False

    def _too_small(y: int, prev: int) -> bool:
        """Δ매출이 매출 대비 너무 작은 연도는 비율이 폭발한다 -> 제외."""
        return abs(revs[y] - revs[prev]) < abs(revs[y]) * NWC_MIN_REV_CHANGE

    nwc_pct = None
    nwc_pairs: list[tuple[int, float]] = []
    nwc_skipped: list[str] = []
    nwc_basis = ""

    if not narrow_nwc:
        for i in range(1, len(asc)):
            y, prev = asc[i], asc[i - 1]
            if nwcs.get(y) is None or not revs.get(y) or not revs.get(prev):
                continue
            d_rev = revs[y] - revs[prev]
            if d_rev == 0:
                continue
            if _too_small(y, prev):
                nwc_skipped.append(f"{y}(Δ매출 {d_rev / revs[y] * 100:+.1f}%)")
                continue
            nwc_pairs.append((y, (-nwcs[y]) / d_rev))
        if nwc_pairs:
            nwc_basis = ("현금흐름표 '영업활동으로 인한 자산부채의 변동'은 현금영향이라 부호를 "
                         "반전해 ΔNWC 로 환산")

    # 좁은 정의 경로 — 금융부문 보유사에는 **강제**, 그 외에는 CF 에 집계가 없을 때 폴백.
    # (SK하이닉스·네이버 실측: CF 에 '자산부채의 변동' 합계 행이 없다)
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
            if _too_small(y, prev):
                nwc_skipped.append(f"{y}(Δ매출 {d_rev / revs[y] * 100:+.1f}%)")
                continue
            nwc_pairs.append((y, (wc[y] - wc[prev]) / d_rev))
        if nwc_pairs:
            nwc_basis = ("영업 운전자본 = 매출채권 + 재고자산 - 매입채무 의 연도별 증감"
                         + (" (금융부문 보유사 -> 금융성 자산·부채를 배제한 좁은 정의를 강제)"
                            if narrow_nwc else
                            " (현금흐름표에 자산부채 변동 합계가 없어 재무상태표에서 산출)"))

    nwc_flag = None
    if nwc_pairs:
        # 산술평균은 이상치 하나에 무방비다(3개년이면 특히) -> median.
        med = median([r for _, r in nwc_pairs]) * 100
        if abs(med) > NWC_SANITY_LIMIT:
            nwc_flag = (f"|ΔNWC/Δ매출| {abs(med):.1f}% 가 통상 범위"
                        f"({NWC_SANITY_LIMIT:.0f}%)를 넘습니다 — 자동 채택하지 말고 "
                        f"사용자에게 확인하세요.")
        nwc_pct = _computed(
            round(med, 2), "%",
            f"{name} ΔNWC/Δ매출({len(nwc_pairs)}개년 중앙값)",
            f"{len(nwc_pairs)}개년 중앙값(산술평균은 이상치에 취약해 median 사용). {nwc_basis}. "
            f"연도별: " + ", ".join(f"{y} {r * 100:+.1f}%" for y, r in nwc_pairs)
            + (f". 제외된 연도: {', '.join(nwc_skipped)} — Δ매출이 매출의 "
               f"{NWC_MIN_REV_CHANGE * 100:.0f}% 미만이면 비율이 폭발해 제외"
               if nwc_skipped else "")
            + ". 매출 감소 연도가 섞이면 부호가 뒤집힐 수 있으니 연도별 값을 확인할 것."
            + (f" ⚠️ {nwc_flag}" if nwc_flag else ""), as_of)

    missing = [k for k, v in (("ebit_margin_pct", ebit_margin), ("revenue_growth_pct", growth),
                              ("da_pct", da_pct), ("capex_pct", capex_pct),
                              ("nwc_pct", nwc_pct)) if v is None]
    return {
        "company": name, "years": years, "as_of": as_of,
        "ebit_margin_pct": ebit_margin, "revenue_growth_pct": growth,
        "da_pct": da_pct, "capex_pct": capex_pct, "nwc_pct": nwc_pct,
        "missing": missing,
        "nwc_narrow": bool(narrow_nwc), "nwc_basis": nwc_basis,
        "nwc_skipped_years": nwc_skipped, "nwc_needs_confirmation": nwc_flag,
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
            f"차입금이 급변한 해는 왜곡 가능. "
            f"⚠️ 이것은 **실효(과거 가중평균) 조달금리**이고 신규 조달비용이 아니다 — "
            f"WACC 에는 market_cost_of_debt(등급별 회사채 유통수익률)를 쓰고, 이 값은 "
            f"교차검증에 쓴다.")
    if kd > 15 or kd < 0.3:
        note += f" ⚠️ {kd:.2f}% 는 통상 범위(0.3~15%)를 벗어남 — 검토 필요."
    return _computed(round(kd, 2), "%", f"{interest.label.split(' 이자')[0]} 세전 타인자본비용(Kd)",
                     note, interest.provenance.as_of, extras={"interest_expense": interest})


# 이자보상배율 → 한국은행 고시 등급 구간. 고시가 AA-/BBB- 두 구간뿐이라 두 갈래로만
# 나눈다(없는 정밀도를 만들지 않는다). 경계 5배는 실무에서 투자적격/취약 구분에 널리 쓰는 값.
COVERAGE_INVESTMENT_GRADE = 5.0


def market_cost_of_debt(company: str, year: int | None = None, country: str = "KR",
                        rating: str | None = None, report: str = "annual",
                        prefer: str = "CFS") -> Value:
    """**시장** 세전 Kd = 해당 등급 회사채 유통수익률 (신규 조달금리).

    실효 Kd(cost_of_debt: 이자비용÷차입금)는 과거 조달금리의 가중평균이라 WACC 의 신규
    조달비용으로는 맞지 않다. 실측(SK하이닉스): 실효 Kd 3.79% < 무위험수익률 4.288% 라는
    역전이 나왔고, 이는 계산 오류가 아니라 "만기가 남은 저금리 조달분이 살아있다" 는 사실의
    정확한 반영이다 — 그러나 그 값을 신규 조달비용으로 쓰면 신용스프레드가 음수가 된다.

    등급은 rating 으로 직접 지정하거나, 미지정 시 이자보상배율(EBIT ÷ 이자비용)로 구간을
    고른다. 실효 Kd 와의 괴리는 note 에 남긴다.
    """
    c = (country or "KR").strip().upper()
    if c != "KR":
        raise DataError(
            f"시장 Kd 는 현재 한국(ECOS 등급별 회사채)만 지원합니다 — {c} 는 "
            f"get_industry_benchmarks 의 산업평균 Kd 를 쓰거나 값을 직접 지정하세요.")

    coverage = None
    cov_note = ""
    if rating is None:
        try:
            ebit = dart.financial_item(company, "operating_income", year, report, prefer)
            cf = dart.cf_extras(company, year, report, prefer)
            interest = cf.get("interest")
            if interest is not None and interest.value:
                coverage = ebit.value / interest.value
        except DataError:
            coverage = None
        if coverage is None:
            rating = "AA-"
            cov_note = ("이자보상배율을 계산할 수 없어 AA- 구간을 가정했습니다 — 등급을 알면 "
                        "rating 으로 지정하세요. ")
        else:
            rating = "AA-" if coverage >= COVERAGE_INVESTMENT_GRADE else "BBB-"
            cov_note = (f"이자보상배율(EBIT÷이자비용) {coverage:.1f}배 → "
                        f"{'AA-' if coverage >= COVERAGE_INVESTMENT_GRADE else 'BBB-'} 구간"
                        f"(경계 {COVERAGE_INVESTMENT_GRADE:.0f}배). ")

    y = ecos.corporate_bond_yield(rating)
    rf = ecos.risk_free_rate("3Y")
    spread = y.value - rf.value

    effective = None
    eff_note = ""
    try:
        effective = cost_of_debt(company, year, True, report, prefer)
    except DataError:
        pass
    if effective is not None:
        gap = effective.value - y.value
        eff_note = (f"실효 Kd(이자비용÷차입금) {effective.value}% 와의 차이 {gap:+.2f}%p. ")
        if effective.value < rf.value:
            eff_note += (f"⚠️ 실효 Kd 가 무위험수익률({rf.value}%)보다 낮습니다 — 과거 저금리 "
                         f"조달분 때문이며 신용스프레드가 음수라는 뜻이 아닙니다. WACC 에는 "
                         f"이 시장 Kd 를 쓰는 것이 맞습니다. ")
        elif abs(gap) > 2.0:
            eff_note += ("⚠️ 두 값의 괴리가 2%p 를 넘습니다 — 조달구조가 최근 크게 바뀌었을 "
                         "수 있으니 어느 쪽을 쓸지 명시적으로 판단하세요. ")

    return _computed(
        round(y.value, 2), "%", f"{company} 세전 타인자본비용(Kd, 시장 · {rating})",
        f"{y.label} {y.value}% (한국은행 ECOS, {y.provenance.as_of}) = 신규 조달금리. "
        f"{cov_note}국고채 3년 {rf.value}% 대비 신용스프레드 {spread:+.2f}%p. {eff_note}"
        f"한국은행 고시 등급이 AA-/BBB- 두 구간뿐이라 그 사이 등급은 근사입니다.",
        y.provenance.as_of,
        extras={k: v for k, v in {
            "corporate_bond_yield": y, "risk_free_rate": rf,
            "effective_cost_of_debt": effective}.items() if v is not None})


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
# 영구성장률의 실무 권장 범위. 국채수익률은 g 의 **상한**이지 g 자체가 아니다 —
# 이름이 terminal_growth 였던 탓에 LLM 이 상한값을 g 로 그대로 써서 TV 가 폭발했다
# (실측: 국고채 4.288% 를 g 로 사용 → WACC−g 스프레드 0.2%p, TV 비중 92%).
TERMINAL_G_SUGGESTED = {"KR": 2.0, "US": 2.5, "JP": 1.0, "TW": 2.0}


def terminal_growth_cap(country: str = "KR", tenor: str = "10Y") -> Value:
    """영구성장률 g 의 **상한**과 권장 기본값을 함께 돌려준다.

    Damodaran 원칙: g ≤ 무위험수익률(영구히 경제보다 빨리 크는 기업은 없고, 국채수익률이
    명목 경제성장률의 상한 대용). 그러나 **상한을 g 로 그대로 쓰면 안 된다** — value 에는
    권장 g(장기 인플레이션 + 실질성장 수준)를 담고, 상한은 extras.cap 으로 준다.
    """
    c = (country or "KR").strip().upper()
    if c not in ("KR", "US"):
        raise DataError(f"영구성장률 산정 미지원 국가: {country} (현재 KR, US)")
    rf = ecos.risk_free_rate(tenor) if c == "KR" else fred.risk_free_rate(tenor)
    cap = rf.value
    suggested = min(TERMINAL_G_SUGGESTED.get(c, 2.0), cap)
    cap_v = _computed(
        cap, "%", f"영구성장률 g 상한 ({c})",
        f"Damodaran 원칙(g ≤ 무위험수익률): {c} 국채 {tenor} {cap}% "
        f"({rf.provenance.source}, {rf.provenance.as_of})", rf.provenance.as_of,
        extras={"risk_free_rate": rf})
    return _computed(
        suggested, "%", f"영구성장률 g 권장값 ({c})",
        f"권장 g {suggested}% = 장기 물가+실질성장 수준. 상한은 국채 {tenor} {cap}% 이지만 "
        f"**상한을 g 로 쓰면 안 된다** — WACC−g 스프레드가 좁아져 TV 가 EV 를 지배하고, "
        f"국채수익률 수준의 영구성장은 그 자체로 정당화되지 않는다. 상한 근처를 쓰려면 "
        f"근거를 별도로 제시할 것.", rf.provenance.as_of,
        extras={"cap": cap_v, "risk_free_rate": rf})


# 예전 이름 호환 — 상한이 아니라 권장값을 돌려주도록 의미가 바뀌었다.
def terminal_growth(country: str = "KR", tenor: str = "10Y") -> Value:
    return terminal_growth_cap(country, tenor)
