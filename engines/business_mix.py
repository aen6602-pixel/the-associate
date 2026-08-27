"""사업부문 구성 판정 — 이 회사에 단일 FCFF DCF 를 적용할 수 있는가.

왜 필요한가. 이 앱의 자동 가정 도출은 **집계 계정을 그 구성을 확인하지 않고 신뢰**한다.
삼성전자에서는 그 집계가 곧 영업 실체지만, 캡티브 금융(할부금융·리스·캐피탈)을 연결에 담은
회사에서는 같은 이름의 계정에 전혀 다른 것이 들어간다. 실측(2026-08, 현대자동차):

  · get_net_debt        → 111.99조 (IBD 131.01조 − 현금 19.01조)
                          IBD 에 금융부문 차입금이 포함 → EV 에서 과다 차감
  · compute_wacc_auto   → D/(D+E) 0.631 → WACC 5.9%
                          제조 실체의 시장가 레버리지는 한 자릿수 퍼센트대
  · get_dcf_assumptions → ΔNWC/Δ매출 161.51%
                          현금흐름표 '자산부채의 변동' 집계에 금융업채권 증감 포함
  → 5개 예측연도 UFCF 전부 음수, EV 음수, 주당 −5,042,055원

세 곳에서 터졌지만 원인은 하나다. 그래서 판정을 이 모듈로 모으고 세 소비처가 함께 쓴다.

판정은 boolean 이 아니라 **3분류**다. 실측(2026-08-27, FY2025 연결)으로 확인한 근거:

  industrial  삼성전자 — 매출액 333.6조 / 재고자산 있음 / 금융업 계정 0건
              기아     — 매출액 114.1조 / 재고자산 있음 / 금융업 계정 0건
              → 단일 FCFF DCF 적용 가능
  mixed       현대자동차 — 매출액 186.3조(금융업수익 포함) / 재고자산 있음 /
              금융업채권 134.3조(유동 78.5 + 비유동 55.8) + 운용리스자산 53.9조
              = 총자산 368.8조의 51% → 캡티브 금융 보유
              → 단일 FCFF DCF 부적합, SOTP(제조 DCF + 금융 P/B) 필요
  financial   삼성카드 — **재고자산 행 자체가 없고** IS 가 이자수익 2.03조 /
              수수료수익 1.81조 / 순이자손익 1.43조 로 구성
              → FCFF·EV 개념이 성립하지 않음(부채가 조달수단이자 영업). 지분 기준
                평가(P/B·잔여이익·배당할인)로만 평가

'금융부문 있음' 은 차단이 아니라 **경로 변경**이다 — 좁은 정의의 운전자본을 쓰게 하고,
순부채·부채비중을 오염원으로 표시하고, 단일 DCF 를 막아 SOTP 로 보낸다.
"""
from __future__ import annotations

import re
from core.cache import TTL_FRESH, ttl_cache

from core.schema import DataError, Provenance, SourceType, Value
from providers import dart

# ── 금융업 고유 자산 계정 (재무상태표) ────────────────────────────
# '금융자산' 은 제조업에도 흔하므로 넣지 않는다 — 캡티브 금융의 지표는 **여신 자산**이다.
# 실측: 현대자동차의 실제 계정명은 "금융업채권" 이다. 처음에 "금융채권" 으로 적었더니
# "금융채권" ⊄ "금융업채권" 이라 매칭이 안 됐다 — 계정명은 추측하지 말고 실측으로 넣는다.
FINANCE_ASSET_TERMS = (
    "금융업채권", "금융채권", "할부금융", "할부매출채권", "리스채권", "카드채권",
    "대출채권", "운용리스자산", "금융리스채권", "미수금융수익",
)

# 순수 금융회사의 손익 구성. 제조업 손익(매출액·매출원가)이 없고 이쪽만 있으면 금융회사다.
FINANCE_REVENUE_TERMS = (
    "이자수익", "순이자손익", "수수료수익", "금융업수익", "보험료수익", "리스수익",
    "카드수익", "배당금수익및이자수익",
)
# ⚠️ 주 매출 계정은 **정확일치**로 본다. 부분일치로 하면 삼성카드의 "기타영업수익"(923억)이
# "영업수익" 에 걸려 순수 금융회사가 제조업으로 분류된다(실측으로 확인한 오분류).
OPERATING_REVENUE_TERMS = ("매출액", "매출원가", "매출총이익", "영업수익", "수익(매출액)",
                           "영업수익(매출액)")
# 정확일치가 표기 차이로 빠져나갈 수 있으니 금액 비교를 안전망으로 둔다 — 금융수익이 제조
# 매출의 이 배수 이상이면 금융회사로 본다(삼성카드 실측: 5.49조 vs 0.09조 = 61배).
FINANCE_REVENUE_DOMINANCE = 2.0

# 금융부문 차입금 식별용. 계정명만으로는 완벽히 못 가르므로, 분해가 안 되면 추정하지 않고
# confident=False 로 돌려준다.
FINANCE_DEBT_TERMS = ("금융부문", "할부금융", "여신", "캐피탈", "카드")

# 금융업 자산이 총자산의 이 비율을 넘으면 **단독으로 결정적**이다. 2-of-3 만 쓰면
# 현대자동차(신호 1개, 그러나 금융업 자산이 총자산의 51%)를 놓친다.
MATERIAL_FINANCE_ASSET_RATIO = 0.10

_SECTION_RE = re.compile(r"\(\s*(금융업|여신전문금융업|보험업|카드업)\s*\)")
_SUBSIDIARY_RE = re.compile(
    r"(캐피탈|카드|할부금융|여신전문|파이낸스|파이낸셜|Capital|Card|Finance|Financial)", re.I)


def _norm(s: str | None) -> str:
    return (s or "").replace(" ", "")


def _statement_signals(company: str, year: int | None, prefer: str) -> dict:
    """재무제표 한 번 조회로 자산·손익 신호를 전부 뽑는다."""
    ent = dart.resolve(company)
    yr = year if year is not None else dart._latest_year(
        ent["corp_code"], dart.REPRT["annual"], prefer)
    rows, fs_label = dart._statement_rows(ent["corp_code"], yr, dart.REPRT["annual"], prefer)

    finance_assets, total_assets, has_inventory = [], None, False
    finance_revenue, operating_revenue = [], []
    for r in rows:
        nm_raw = r.get("account_nm") or ""
        nm = _norm(nm_raw)
        amt = dart._to_int(r.get("thstrm_amount"))
        sj = r.get("sj_div")
        if sj == "BS":
            if nm == "자산총계" and amt:
                total_assets = amt
            if "재고자산" in nm:
                has_inventory = True
            if amt and any(t in nm for t in FINANCE_ASSET_TERMS):
                finance_assets.append({"account": nm_raw, "amount": amt})
        elif sj in ("IS", "CIS") and amt:
            if any(t in nm for t in FINANCE_REVENUE_TERMS):
                finance_revenue.append({"account": nm_raw, "amount": amt})
            if nm in OPERATING_REVENUE_TERMS:      # 정확일치 — 위 주석 참고
                operating_revenue.append({"account": nm_raw, "amount": amt})
    return {
        "corp_name": ent["corp_name"], "year": yr, "fs_label": fs_label,
        "finance_assets": finance_assets, "total_assets": total_assets,
        "has_inventory": has_inventory,
        "finance_revenue": finance_revenue, "operating_revenue": operating_revenue,
    }


def _report_signals(company: str, year: int | None) -> dict:
    """사업보고서 원문에서 금융업 섹션·금융 종속기업 힌트를 찾는다.

    원문 파싱은 비싸고 실패할 수 있으므로 실패는 '신호 없음' 으로 처리한다 — 예외를 올리면
    판정 자체가 죽어서 게이트가 무력화된다.
    """
    out: dict = {"section": None, "subsidiaries": []}
    try:
        ent = dart.resolve(company)
        yr = year or dart._latest_year(ent["corp_code"], dart.REPRT["annual"], "CFS")
        rows = dart.list_filings(ent["corp_code"], f"{yr + 1}0101", f"{yr + 1}1231", "사업보고서")
        if not rows:
            return out
        rcept = rows[0].get("rcept_no")
        doc = dart.filing_text(rcept, "금융업", context_chars=120, max_matches=6)
        for ex in doc.get("excerpts") or []:
            m = _SECTION_RE.search(ex)
            if m:
                out["section"] = m.group(0)
                break
        doc2 = dart.filing_text(rcept, "캐피탈", context_chars=80, max_matches=6)
        for ex in doc2.get("excerpts") or []:
            m = _SUBSIDIARY_RE.search(ex)
            if m:
                out["subsidiaries"].append(m.group(0))
    except Exception:  # noqa: BLE001
        return out
    return out


@ttl_cache(TTL_FRESH, maxsize=128)
def _cached(company: str, year: int | None, prefer: str, deep: bool) -> tuple:
    try:
        st = _statement_signals(company, year, prefer)
    except DataError as e:
        raise DataError(f"{company} 사업부문 판정 실패(재무제표 조회 불가): {e}") from e
    rep = _report_signals(company, year) if deep else {"section": None, "subsidiaries": []}
    return (
        st["corp_name"], st["year"], st["total_assets"], st["has_inventory"],
        tuple((a["account"], a["amount"]) for a in st["finance_assets"]),
        tuple((a["account"], a["amount"]) for a in st["finance_revenue"]),
        tuple((a["account"], a["amount"]) for a in st["operating_revenue"]),
        rep["section"], tuple(rep["subsidiaries"]),
    )


def classify(company: str, year: int | None = None, prefer: str = "CFS",
             deep: bool = True) -> dict:
    """→ {kind: 'industrial'|'mixed'|'financial', ...}.

    deep=False 면 사업보고서 원문 파싱(느림)을 건너뛰고 재무제표 신호만 본다. 재무제표 신호만
    으로도 현대자동차·삼성카드는 잡힌다(중요성 비율과 손익 구성이 결정적이라서).
    """
    (name, yr, total_assets, has_inventory, fa_t, fr_t, orv_t,
     section, subs) = _cached(company, year, prefer, deep)
    finance_assets = [{"account": a, "amount": v} for a, v in fa_t]
    fa_total = sum(a["amount"] for a in finance_assets)
    ratio = (fa_total / total_assets) if (total_assets and fa_total) else 0.0

    checks = {
        "balance_sheet_finance_assets": bool(finance_assets),
        "report_finance_section": section is not None,
        "finance_subsidiaries": bool(subs),
    }
    hits = sum(1 for v in checks.values() if v)

    # 순수 금융회사: 재고자산이 없고, 손익이 금융수익으로 지배된다.
    fin_rev = sum(v for _, v in fr_t)
    op_rev = sum(v for _, v in orv_t)
    is_financial = bool(fr_t) and not has_inventory and (
        not orv_t or fin_rev >= FINANCE_REVENUE_DOMINANCE * op_rev)
    material = ratio >= MATERIAL_FINANCE_ASSET_RATIO
    kind = "financial" if is_financial else ("mixed" if (material or hits >= 2) else "industrial")

    evidence = []
    if finance_assets:
        top = sorted(finance_assets, key=lambda a: -a["amount"])[:3]
        evidence.append(
            "재무상태표 금융업 자산: "
            + ", ".join(f"{a['account']} {a['amount']:,}" for a in top)
            + (f" (합계 {fa_total:,} = 총자산의 {ratio * 100:.0f}%)" if total_assets else ""))
    if is_financial:
        top = sorted([{"account": a, "amount": v} for a, v in fr_t],
                     key=lambda a: -a["amount"])[:3]
        evidence.append("손익이 금융수익으로 구성(제조 매출·재고자산 없음): "
                        + ", ".join(f"{a['account']} {a['amount']:,}" for a in top))
    if section:
        evidence.append(f"사업보고서 사업의 내용에 {section} 섹션 존재")
    if subs:
        evidence.append(f"금융 종속기업 추정: {', '.join(sorted(set(subs))[:3])}")

    return {
        "company": name, "year": yr, "kind": kind,
        "has_finance_arm": kind in ("mixed", "financial"),
        "single_dcf_ok": kind == "industrial",
        "hits": hits, "checks": checks,
        "finance_asset_total": fa_total, "finance_asset_ratio": round(ratio, 4),
        "total_assets": total_assets, "has_inventory": has_inventory,
        "finance_assets": finance_assets,
        "evidence": evidence,
        "reason": _reason(kind, ratio, hits, is_financial),
    }


def _reason(kind: str, ratio: float, hits: int, is_financial: bool) -> str:
    if kind == "financial":
        return ("순수 금융회사 — 부채가 조달수단이자 영업이라 FCFF·EV 개념이 성립하지 않는다. "
                "단일 DCF 대신 지분 기준 평가(P/B·잔여이익·배당할인)를 쓴다.")
    if kind == "mixed":
        why = (f"금융업 자산이 총자산의 {ratio * 100:.0f}% (기준 "
               f"{MATERIAL_FINANCE_ASSET_RATIO * 100:.0f}% 초과)"
               if ratio >= MATERIAL_FINANCE_ASSET_RATIO else f"금융업 신호 {hits}/3 (2개 이상)")
        return (f"캡티브 금융 보유 — {why}. 연결 IBD·운전자본·부채비중에 금융부문이 섞여 "
                f"단일 FCFF DCF 는 이중 왜곡(WACC 과대 + EV 과다차감)을 낸다. "
                f"SOTP(제조부문 DCF + 금융부문 P/B 또는 잔여이익)로 평가한다.")
    return "제조·서비스 단일 실체 — 금융업 자산·손익 신호가 없어 단일 FCFF DCF 적용 가능."


# 예전 이름 호환 (boolean 판정만 필요한 호출부용)
def detect(company: str, year: int | None = None, prefer: str = "CFS",
           deep: bool = True) -> dict:
    return classify(company, year, prefer, deep)


def gate_value(company: str, year: int | None = None) -> Value:
    """LLM 도구용 — 판정 결과를 Value 로."""
    d = classify(company, year)
    labels = {"industrial": "제조·서비스 단일 실체", "mixed": "캡티브 금융 보유(제조+금융)",
              "financial": "순수 금융회사"}
    note = (f"{labels[d['kind']]}. {d['reason']} "
            + ("근거: " + " / ".join(d["evidence"]) if d["evidence"] else "금융업 신호 없음."))
    return Value(
        value=d["kind"], unit="분류",
        label=f"{d['company']} 사업부문 판정 (FY{d['year']})",
        provenance=Provenance(
            source="계산엔진(engines.business_mix)", source_type=SourceType.COMPUTED,
            source_url="(computed: DART 재무제표 계정 구성 + 사업보고서 원문 신호)",
            original_field="금융업 자산비율 · 손익 구성 · 사업의 내용 섹션 · 종속기업",
            as_of=f"FY{d['year']}", note=note),
        extras={},
    )


def split_finance_debt(company: str, year: int | None = None, report: str = "annual",
                       prefer: str = "CFS") -> dict:
    """차입금을 제조/금융으로 분해 시도 → {total, finance, industrial, basis, confident}.

    계정명에 금융부문 힌트가 없으면 분해가 불가능하다. 그때 **추정으로 쪼개지 않는다** —
    confident=False 로 돌려주고 호출부가 "분해 불가" 를 사용자에게 알리게 한다.
    (실측: 현대자동차의 사채 106.9조는 계정명만으로 제조/금융을 가릴 수 없다. 세그먼트
     주석이 필요하며, 그때까지는 단일 DCF 를 막는 것이 유일하게 정직한 처리다.)
    """
    ent = dart.resolve(company)
    reprt = dart.REPRT.get(report, "11011")
    yr = year if year is not None else dart._latest_year(ent["corp_code"], reprt, prefer)
    rows, fs_label = dart._statement_rows(ent["corp_code"], yr, reprt, prefer)
    total = fin = 0
    fin_rows: list[str] = []
    for r in rows:
        if r.get("sj_div") != "BS":
            continue
        nm = _norm(r.get("account_nm"))
        if not any(t in nm for t in ("차입금", "사채", "리스부채")):
            continue
        amt = dart._to_int(r.get("thstrm_amount"))
        if amt is None:
            continue
        total += amt
        if any(t in nm for t in FINANCE_DEBT_TERMS):
            fin += amt
            fin_rows.append(f"{r.get('account_nm')} {amt:,}")
    return {
        "company": ent["corp_name"], "year": yr, "fs_label": fs_label,
        "total": total, "finance": fin, "industrial": total - fin,
        "finance_rows": fin_rows,
        "confident": bool(fin_rows),
        "basis": ("계정명에 금융부문 표기가 있는 차입금만 금융으로 분류"
                  if fin_rows else
                  "계정명으로 금융부문 차입금을 분리할 수 없음 — 세그먼트 주석이 필요하다"),
    }
