"""상증법 보충적 평가 엔진 (비상장주식, 상증세법 §63·시행령 §54).

1주당 평가액 = Max[ (순손익가치 × w1 + 순자산가치 × w2) / (w1+w2),  순자산가치 × 80% ]
  · 일반법인 3:2, 부동산과다법인 2:3
순손익가치 = (3개년 가중평균 1주당 순손익 ÷ 환원율 10%),  가중치 직전3·직전전2·직전전전1 ÷6
순자산가치 = 순자산가액 ÷ 발행주식총수

⚠️ 근사: 법상 '순손익액(각 사업연도 소득, 세무조정)'·'순자산가액(시가평가)' 대신
공개 재무제표의 당기순이익·자본총계를 사용한다(별도재무제표 기준). 세무조정·자산 시가평가·
영업권 가산 미반영. 상장사는 실제로는 시가(평가기준일 전후 2개월 종가평균)로 평가됨.
"""
from __future__ import annotations

from core.schema import Provenance, Value, DataError, SourceType
from providers import dart

CAP_RATE = 0.10  # 순손익가치환원율 (상증세법 시행규칙 §17-3①)
PREFER = "OFS"   # 상증법은 별도(법인 자체) 기준

# 부동산과다보유법인: 자산총액 중 부동산 등의 비율이 이 값 이상 → 가중치 2:3 (상증령 §54①)
REAL_ESTATE_HEAVY_RATIO = 0.50
# 순자산가치만으로 평가하는 사유 중 '부동산·주식 등의 비중' 기준 (상증령 §54④)
NAV_ONLY_ASSET_RATIO = 0.80
# 최대주주 할증률 (상증법 §63③). 중소기업 등은 제외.
CONTROL_PREMIUM = 0.20

# 재무상태표에서 '부동산 등' 으로 볼 계정. 유형자산 전체를 쓰면 기계장치까지 들어가 판정이
# 틀리므로(제조업이 전부 부동산과다보유로 잡힌다) 토지·건물·투자부동산 계열만 센다.
REAL_ESTATE_TERMS = ("토지", "건물", "투자부동산", "구축물", "부동산")
REAL_ESTATE_EXCLUDE = ("건설중인자산",)   # 진행 중 공사는 부동산 '보유' 로 보기 어렵다
# 주식 등(상증령 §54④의 '주식등') 계열
STOCK_TERMS = ("공동기업및관계기업투자", "관계기업투자", "종속기업투자", "지분증권",
               "매도가능금융자산", "당기손익-공정가치측정금융자산")


def _asset_mix(company: str, year: int | None, report: str) -> dict:
    """재무상태표에서 부동산·주식 비중을 센다 → {ratio_real_estate, ratio_stock, ...}.

    ⚠️ 근사다. 법상 '부동산 등' 은 시가평가 기준이고 부동산에 관한 권리까지 포함하지만,
    공개 재무제표에서는 장부가액 계정만 볼 수 있다. 그래서 판정 결과를 자동 확정하지 않고
    호출부가 사용자에게 확인할 수 있도록 근거 계정을 함께 돌려준다.
    """
    from providers import dart as _dart

    ent = _dart.resolve(company)
    reprt = _dart.REPRT.get(report, "11011")
    yr = year if year is not None else _dart._latest_year(ent["corp_code"], reprt, PREFER)
    rows, fs_label = _dart._statement_rows(ent["corp_code"], yr, reprt, PREFER)

    total = None
    re_rows: list[tuple[str, int]] = []
    st_rows: list[tuple[str, int]] = []
    for r in rows:
        if r.get("sj_div") != "BS":
            continue
        raw = r.get("account_nm") or ""
        nm = raw.replace(" ", "")
        amt = _dart._to_int(r.get("thstrm_amount"))
        if nm == "자산총계" and amt:
            total = amt
            continue
        if not amt or amt <= 0:
            continue
        if any(t in nm for t in REAL_ESTATE_TERMS) and not any(
                t in nm for t in REAL_ESTATE_EXCLUDE):
            re_rows.append((raw, amt))
        if any(t in nm for t in STOCK_TERMS):
            st_rows.append((raw, amt))
    re_sum = sum(a for _, a in re_rows)
    st_sum = sum(a for _, a in st_rows)
    return {
        "year": yr, "fs_label": fs_label, "total_assets": total,
        "real_estate": re_sum, "stock": st_sum,
        "real_estate_rows": re_rows, "stock_rows": st_rows,
        "ratio_real_estate": (re_sum / total) if total else None,
        "ratio_stock": (st_sum / total) if total else None,
        "ratio_combined": ((re_sum + st_sum) / total) if total else None,
    }


def build_model(company: str, year: int | None = None,
                real_estate_heavy: bool | None = None, report: str = "annual",
                nav_only: bool | None = None, largest_shareholder: bool = False,
                sme: bool = False) -> dict:
    """상증법 계산에 필요한 입력·가정·결과를 구조화해 반환 (Value/note/Excel 공용).

    real_estate_heavy: None(기본)이면 재무상태표의 부동산 비중으로 자동 판정(상증령 §54①,
      50% 이상이면 가중치 2:3). 명시하면 그 값을 쓴다.
    nav_only: None(기본)이면 자동 판정 — 3개년 순손익 부족(사업개시 3년 미만) 또는
      부동산·주식 등이 자산의 80% 이상(상증령 §54④)이면 순자산가치만으로 평가한다.
    largest_shareholder: 최대주주 지분이면 20% 할증(상증법 §63③). sme=True 면 할증 제외.
    """
    eq = dart.financial_item(company, "total_equity", year, report, PREFER)
    sh = dart.shares_outstanding(company, year, report)
    shares = sh.value

    mix, mix_err = None, None
    try:
        mix = _asset_mix(company, year, report)
    except Exception as e:  # noqa: BLE001 — 자산구성 판정 실패가 평가를 막지는 않는다
        mix_err = str(e)

    # 순손익 3개년 — 부족하면 예외를 던지지 말고 순자산가치만으로 평가한다(법이 그렇게 정한다)
    series: list[dict] = []
    ni_err = None
    try:
        ni = dart.financial_item_multiyear(company, "net_income", year, report, PREFER)
        series = ni["series"]
    except DataError as e:
        ni, ni_err = None, str(e)
    short_history = len(series) < 3

    # 부동산과다보유 자동판정 (상증령 §54①)
    re_auto = None
    if mix and mix["ratio_real_estate"] is not None:
        re_auto = mix["ratio_real_estate"] >= REAL_ESTATE_HEAVY_RATIO
    re_heavy = re_auto if real_estate_heavy is None else bool(real_estate_heavy)
    if re_heavy is None:
        re_heavy = False

    # 순자산가치 100% 사유 (상증령 §54④ 일부 + 사업개시 3년 미만)
    nav_reasons: list[str] = []
    if short_history:
        nav_reasons.append(
            f"3개년 순손익 확보 불가({len(series)}개년) — 사업개시 3년 미만 등에 해당"
            + (f" [{ni_err}]" if ni_err else ""))
    if mix and mix["ratio_combined"] is not None and mix["ratio_combined"] >= NAV_ONLY_ASSET_RATIO:
        nav_reasons.append(
            f"부동산·주식 등이 자산총계의 {mix['ratio_combined'] * 100:.0f}% "
            f"(≥{NAV_ONLY_ASSET_RATIO * 100:.0f}%) — 상증령 §54④ 순자산가치 단독평가 사유")
    nav_only_final = bool(nav_reasons) if nav_only is None else bool(nav_only)

    w1, w2 = (2, 3) if re_heavy else (3, 2)
    nav_per_share = eq.value / shares

    if nav_only_final:
        weighted_ni = ni_per_share = 0.0
        income_value = 0.0
        blended = nav_per_share
        floor = nav_per_share * 0.8
        value = nav_per_share
    else:
        n0, n1, n2 = series[0]["amount"], series[1]["amount"], series[2]["amount"]
        weighted_ni = (n0 * 3 + n1 * 2 + n2 * 1) / 6.0
        ni_per_share = weighted_ni / shares
        income_value = max(0.0, ni_per_share / CAP_RATE)   # 음수 순손익가치 → 0
        blended = (income_value * w1 + nav_per_share * w2) / (w1 + w2)
        floor = nav_per_share * 0.8
        value = max(blended, floor)

    # 최대주주 할증 (상증법 §63③). 중소기업 등은 제외.
    premium_applied = bool(largest_shareholder) and not sme
    base_value = value
    if premium_applied:
        value = value * (1 + CONTROL_PREMIUM)

    ni_prov = Provenance(
        source="DART (금융감독원)", source_type=SourceType.AUTHORITATIVE,
        source_url=(f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={ni['rcept']}" if ni
                    else "https://dart.fss.or.kr"),
        as_of=eq.provenance.as_of, filing_date=ni["filing_date"] if ni else None,
        note=(f"{ni['fs_label']} 당기순이익 {len(series)}개년" if ni
              else f"순손익 조회 실패 — 순자산가치 단독평가. {ni_err}"),
    )
    label_from_eq = (eq.label or company).split(" 자본총계")[0]
    return {
        "company": ni["corp_name"] if ni else label_from_eq,
        "stock_code": ni["stock_code"] if ni else None,
        "as_of": eq.provenance.as_of,
        "fs_label": ni["fs_label"] if ni else "별도(OFS)",
        "ni_series": series, "ni_prov": ni_prov,
        "equity": eq, "shares": sh,
        "cap_rate": CAP_RATE, "w_income": w1, "w_asset": w2, "floor_pct": 0.8,
        "real_estate_heavy": re_heavy,
        "real_estate_heavy_auto": re_auto,
        "real_estate_heavy_explicit": real_estate_heavy is not None,
        "nav_only": nav_only_final, "nav_only_reasons": nav_reasons,
        "nav_only_explicit": nav_only is not None,
        "asset_mix": mix, "asset_mix_error": mix_err,
        "largest_shareholder": bool(largest_shareholder), "sme": bool(sme),
        "control_premium_pct": CONTROL_PREMIUM * 100 if premium_applied else 0.0,
        "results": {
            "weighted_ni": weighted_ni, "ni_per_share": ni_per_share,
            "income_value": income_value, "nav_per_share": nav_per_share,
            "blended": blended, "floor": floor,
            "value_before_premium": base_value, "value": value,
            "floored": (not nav_only_final) and base_value == floor and blended < floor,
        },
    }


def evaluate(company: str, year: int | None = None,
             real_estate_heavy: bool | None = None, report: str = "annual",
             nav_only: bool | None = None, largest_shareholder: bool = False,
             sme: bool = False) -> Value:
    """상증법 보충적 평가액(1주당). 결과 단위 KRW/주, computed 등급."""
    m = build_model(company, year, real_estate_heavy, report, nav_only,
                    largest_shareholder, sme)
    s, r = m["ni_series"], m["results"]
    fmt = lambda x: f"{x:,.0f}"

    # 법령 판정 근거를 앞세운다 — 가중치·단독평가 여부가 값을 가장 크게 바꾸는 선택이다.
    judge = []
    if m["nav_only"]:
        judge.append("순자산가치 단독평가"
                     + (f"(사유: {'; '.join(m['nav_only_reasons'])})" if m["nav_only_reasons"]
                        else "(사용자 지정)"))
    judge.append(f"가중치 {m['w_income']}:{m['w_asset']}"
                 + (" · 부동산과다보유법인(상증령 §54①)" if m["real_estate_heavy"] else "")
                 + (" [사용자 지정]" if m["real_estate_heavy_explicit"] else
                    " [자동판정]" if m["real_estate_heavy_auto"] is not None else ""))
    mix = m["asset_mix"]
    if mix and mix["ratio_real_estate"] is not None:
        judge.append(f"자산구성: 부동산 등 {mix['ratio_real_estate'] * 100:.0f}%"
                     + (f", 주식 등 {mix['ratio_stock'] * 100:.0f}%"
                        if mix["ratio_stock"] is not None else ""))
    if m["control_premium_pct"]:
        judge.append(f"최대주주 할증 {m['control_premium_pct']:.0f}%(상증법 §63③) 적용 — "
                     f"할증 전 {fmt(r['value_before_premium'])}원/주")
    elif m["largest_shareholder"] and m["sme"]:
        judge.append("최대주주 지분이나 중소기업이라 할증 제외(상증법 §63③ 단서)")

    income_txt = ("순자산가치 단독평가라 순손익가치를 쓰지 않음. " if m["nav_only"] else
                  f"순손익: {s[0]['period']} {fmt(s[0]['amount'])} / {s[1]['period']} "
                  f"{fmt(s[1]['amount'])} / {s[2]['period']} {fmt(s[2]['amount'])} "
                  f"→ 가중평균 {fmt(r['weighted_ni'])} → 1주 {fmt(r['ni_per_share'])} "
                  f"÷ {m['cap_rate']:.0%} = 순손익가치 {fmt(r['income_value'])}원/주. ")
    note = (
        f"[상증법 보충적평가 · {m['fs_label']} 기준 · ⚠️세무조정·시가평가 미반영 근사]  "
        f"[법령판정] {' / '.join(judge)}.  "
        f"발행주식총수 {m['shares'].value:,}주(DART). "
        + income_txt
        + f"자본총계 {fmt(m['equity'].value)}(DART) → 순자산가치 {fmt(r['nav_per_share'])}원/주. "
        + ("" if m["nav_only"] else
           f"가중 {m['w_income']}:{m['w_asset']} → {fmt(r['blended'])}, "
           f"하한(순자산×80%) {fmt(r['floor'])} → ")
        + f"평가액 {fmt(r['value'])}원/주"
        + (" (하한 적용)" if r["floored"] else "") + ". "
        + "미반영 한계: 각 사업연도 소득 기반 순손익액 재계산(상증령 §56①의 가산·차감), "
          "순자산가액의 자산별 보충적 평가와 영업권 가산(상증령 §59②), 부동산의 시가평가. "
          "자산구성 판정은 장부가액 계정 기준 근사이므로 실제 세무 판정과 다를 수 있습니다."
    )
    return Value(
        value=round(r["value"]), unit="KRW/주",
        label=f"{m['company']} 상증법 1주 평가액 ({m['as_of']})",
        provenance=Provenance(
            source="계산엔진(engines.sangjeung)", source_type=SourceType.COMPUTED,
            source_url="(computed: DART 순손익 3개년 + 자본총계 + 발행주식총수)",
            as_of=m["as_of"], note=note,
        ),
    )
