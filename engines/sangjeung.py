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

CAP_RATE = 0.10  # 순손익가치환원율 (상증세법 시행규칙)
PREFER = "OFS"   # 상증법은 별도(법인 자체) 기준


def build_model(company: str, year: int | None = None,
                real_estate_heavy: bool = False, report: str = "annual") -> dict:
    """상증법 계산에 필요한 입력·가정·결과를 구조화해 반환 (Value/note/Excel 공용)."""
    ni = dart.financial_item_multiyear(company, "net_income", year, report, PREFER)
    series = ni["series"]
    if len(series) < 3:
        raise DataError(
            f"{ni['corp_name']}: 3개년 순손익 데이터가 부족합니다({len(series)}개 확인). "
            "상증법 순손익가치 산정 불가(사업개시 3년 미만 등은 순자산가치만으로 평가)."
        )
    eq = dart.financial_item(company, "total_equity", year, report, PREFER)
    sh = dart.shares_outstanding(company, year, report)

    n0, n1, n2 = series[0]["amount"], series[1]["amount"], series[2]["amount"]
    shares = sh.value
    w1, w2 = (2, 3) if real_estate_heavy else (3, 2)

    weighted_ni = (n0 * 3 + n1 * 2 + n2 * 1) / 6.0
    ni_per_share = weighted_ni / shares
    income_value = max(0.0, ni_per_share / CAP_RATE)   # 음수 순손익가치 → 0
    nav_per_share = eq.value / shares
    blended = (income_value * w1 + nav_per_share * w2) / (w1 + w2)
    floor = nav_per_share * 0.8
    value = max(blended, floor)

    ni_prov = Provenance(
        source="DART (금융감독원)", source_type=SourceType.AUTHORITATIVE,
        source_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={ni['rcept']}",
        as_of=eq.provenance.as_of, filing_date=ni["filing_date"],
        note=f"{ni['fs_label']} 당기순이익 3개년",
    )
    return {
        "company": ni["corp_name"], "stock_code": ni["stock_code"],
        "as_of": eq.provenance.as_of, "fs_label": ni["fs_label"],
        "ni_series": series, "ni_prov": ni_prov,
        "equity": eq, "shares": sh,
        "cap_rate": CAP_RATE, "w_income": w1, "w_asset": w2, "floor_pct": 0.8,
        "real_estate_heavy": real_estate_heavy,
        "results": {
            "weighted_ni": weighted_ni, "ni_per_share": ni_per_share,
            "income_value": income_value, "nav_per_share": nav_per_share,
            "blended": blended, "floor": floor, "value": value,
            "floored": value == floor and blended < floor,
        },
    }


def evaluate(company: str, year: int | None = None,
             real_estate_heavy: bool = False, report: str = "annual") -> Value:
    """상증법 보충적 평가액(1주당). 결과 단위 KRW/주, computed 등급."""
    m = build_model(company, year, real_estate_heavy, report)
    s, r = m["ni_series"], m["results"]
    fmt = lambda x: f"{x:,.0f}"
    note = (
        f"[상증법 보충적평가 · {m['fs_label']} 기준 · ⚠️세무조정·시가평가 미반영 근사]  "
        f"발행주식총수 {m['shares'].value:,}주(DART). "
        f"순손익: {s[0]['period']} {fmt(s[0]['amount'])} / {s[1]['period']} {fmt(s[1]['amount'])} / "
        f"{s[2]['period']} {fmt(s[2]['amount'])} → 가중평균 {fmt(r['weighted_ni'])} "
        f"→ 1주 {fmt(r['ni_per_share'])} ÷ {m['cap_rate']:.0%} = 순손익가치 {fmt(r['income_value'])}원/주. "
        f"자본총계 {fmt(m['equity'].value)}(DART) → 순자산가치 {fmt(r['nav_per_share'])}원/주. "
        f"가중 {m['w_income']}:{m['w_asset']} → {fmt(r['blended'])}, "
        f"하한(순자산×80%) {fmt(r['floor'])} → 평가액 {fmt(r['value'])}원/주"
        + (" (하한 적용)" if r["floored"] else "") + "."
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
