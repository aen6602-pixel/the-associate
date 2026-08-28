"""세션 사실 원장 — 한 대화에서 이미 검증한 값을 다음 턴이 다시 쓰게 한다.

실측 사고: 원문에서 비지배지분 9,144백만원을 찾아 인용해놓고, 재계산 턴에서 "이번
세션에서 도구로 검증된 값이 아니다" 며 철회하고 미차감으로 남겼다. 같은 대화 안에서
같은 항목이 턴마다 다른 값이 되기도 했다(실효세율 11.61% → 7.00%).

원장을 **따로 저장하지 않는다.** 모든 도구 호출은 이미 세션 messages 의 `trace` 에
입력·결과·출처가 통째로 남아 있으므로, 그걸 읽어 사실 목록을 재구성한다. 별도 저장소를
두면 그것과 trace 가 어긋나는 순간 어느 쪽이 진실인지 알 수 없게 된다.

키는 (회사, 항목, 기준기간) 이다. 같은 키가 여러 번 나오면 **가장 신뢰도 높은 것**을
남기고, 동률이면 최근 것을 남긴다 — 그래야 "원문 파싱 → 나중에 XBRL 로 재확인" 이
승격으로 이어진다.
"""
from __future__ import annotations

from core.schema import SourceType

# 원장에 올릴 도구. 계산 결과(compute_*)는 가정에 따라 달라지므로 '사실' 이 아니다 —
# 여기 올리면 가정이 바뀐 뒤에도 옛 결론이 사실처럼 재사용된다.
_FACT_TOOLS = {
    "get_financial_item": "재무항목",
    "get_financial_item_us": "재무항목",
    "get_financial_item_jp": "재무항목",
    "get_financial_item_tw": "재무항목",
    "get_financial_history": "재무시계열",
    "get_net_debt": "순부채",
    "get_market_cap": "시가총액",
    "get_ebitda": "EBITDA",
    "get_effective_tax_rate": "유효세율",
    "get_cost_of_debt": "타인자본비용",
    "get_beta": "베타",
    "get_shares_outstanding": "주식수",
    "get_risk_free_rate": "무위험수익률",
    "get_equity_risk_premium": "ERP",
    "get_corporate_tax_rate": "법정세율",
    "get_fx_rate": "환율",
    "get_business_mix": "사업부문 판정",
}

# 원문에서 읽은 값도 사실이다(문서 ID·인용이 있으므로). LLM 추정만 제외한다.
_MIN_RANK = SourceType.RANK[SourceType.ASSUMPTION]


def _key(name: str, inp: dict, value: dict) -> tuple:
    company = str(inp.get("company") or inp.get("target") or inp.get("corp")
                  or inp.get("base") or "").strip().lower()
    item = str(inp.get("item") or _FACT_TOOLS.get(name, name)).strip()
    prov = value.get("provenance") or {}
    period = str(inp.get("year") or prov.get("as_of") or "").strip()
    return (company, item, period)


def build(messages: list[dict], limit: int = 40) -> list[dict]:
    """세션 messages → 사실 목록(최근 우선). 실패한 호출과 추정값은 제외한다."""
    best: dict[tuple, dict] = {}
    order = 0
    for msg in messages or []:
        for call in msg.get("trace") or []:
            name = call.get("name")
            if name not in _FACT_TOOLS:
                continue
            res = call.get("result") or {}
            if not res.get("ok"):
                continue
            val = res.get("value") or {}
            if val.get("value") is None:
                continue
            prov = val.get("provenance") or {}
            rank = SourceType.RANK.get(prov.get("source_type"), 0)
            if rank < _MIN_RANK:
                continue
            order += 1
            k = _key(name, call.get("input") or {}, val)
            if not k[0] and not k[2]:
                continue        # 회사도 기간도 없으면 재사용 키가 성립하지 않는다
            fact = {
                "key": k, "tool": name, "kind": _FACT_TOOLS[name],
                "label": val.get("label") or "", "value": val.get("value"),
                "unit": val.get("unit") or "", "as_of": prov.get("as_of"),
                "source": prov.get("source"), "source_type": prov.get("source_type"),
                "source_url": prov.get("source_url"), "rank": rank, "order": order,
            }
            prev = best.get(k)
            # 더 신뢰도 높은 값이 오면 승격, 같으면 최근 것으로 갱신.
            if prev is None or (fact["rank"], fact["order"]) >= (prev["rank"], prev["order"]):
                best[k] = fact
    facts = sorted(best.values(), key=lambda f: -f["order"])
    return facts[:limit]


def conflicts(facts: list[dict]) -> list[str]:
    """같은 (회사, 항목) 인데 기간이 다른 값이 섞여 있으면 알린다.

    기간이 다른 것 자체는 정상이지만, 한 답변 안에서 섞이면 기준 혼용이 된다.
    """
    by_pair: dict[tuple, list[dict]] = {}
    for f in facts:
        by_pair.setdefault((f["key"][0], f["key"][1]), []).append(f)
    out = []
    for (company, item), group in by_pair.items():
        periods = {g["as_of"] for g in group if g["as_of"]}
        if len(periods) > 1:
            out.append(f"{company or '?'} {item}: 기준기간이 여러 개 "
                       f"({', '.join(sorted(str(p) for p in periods))})")
    return out


def _fmt(v) -> str:
    if isinstance(v, (int, float)):
        return f"{v:,.4g}" if abs(v) < 1000 else f"{v:,.0f}"
    return str(v)


def render(facts: list[dict], conflict_lines: list[str] | None = None) -> str:
    """시스템 프롬프트에 붙일 블록. 값이 없으면 빈 문자열."""
    if not facts:
        return ""
    lines = []
    for f in facts:
        src = "원문" if f["source_type"] == SourceType.PARSED_AUTHORITATIVE else "공시/API"
        lines.append(f"- {f['label'] or f['kind']}: {_fmt(f['value'])} {f['unit']}"
                     + (f" ({f['as_of']})" if f["as_of"] else "")
                     + f" [{src}·{f['source']}]")
    block = ("\n## 이 세션에서 이미 검증된 값 (사실 원장)\n"
             "아래는 이 대화의 앞선 턴에서 도구로 확인한 값이다. **같은 항목이 다시 필요하면 "
             "그대로 재사용하라** — 도구를 다시 부르는 것은 괜찮지만, 값이 달라지면 왜 달라졌는지 "
             "먼저 밝혀야 하고, 근거 없이 앞 턴의 값을 철회하거나 다른 숫자로 바꾸지 마라.\n"
             + "\n".join(lines))
    if conflict_lines:
        block += ("\n⚠️ 기준기간이 섞인 항목: " + " / ".join(conflict_lines)
                  + " — 한 산출물 안에서 섞어 쓰지 말고 어느 기준인지 밝혀라.")
    return block + "\n"


def block_for(messages: list[dict], limit: int = 40) -> str:
    facts = build(messages, limit)
    return render(facts, conflicts(facts))
