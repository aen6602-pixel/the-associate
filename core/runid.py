"""산출물 재현성 — run_id · 입력 스냅샷 해시 · 엔진 버전.

왜 필요한가: 에이전트가 스스로 "재현 가능한 compute_dcf 결과가 없어 철회한다"고 말한 적이
있다(실측). 같은 답변 안에서 어떤 계산이 어떤 입력으로 돌았는지 가리킬 방법이 없으면
모델도, 사람도, 감사도 그 숫자를 다시 만들어낼 수 없다.

각 엔진 실행에 다음을 붙인다.
  run_id       짧은 식별자. 답변 본문·엑셀·HTML 에 각인해 "이 표는 그 실행 결과" 를 잇는다.
  inputs_hash  입력 스냅샷의 해시. 같은 입력이면 같은 해시 → 재실행 결과와 대조할 수 있다.
  engine       엔진 이름 + 버전. 로직이 바뀌면 버전을 올려 과거 산출물과 구분한다.

run_id 는 입력 해시에서 유도한다(난수가 아니다). 같은 입력을 두 번 돌리면 같은 run_id 가
나와서, "이 숫자 어디서 나왔냐" 에 답할 때 재현 자체가 증명이 된다.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

# 계산 로직이 바뀌면 올린다. 과거 산출물과 숫자가 달라질 수 있음을 이 값으로 구분한다.
ENGINE_VERSIONS = {
    "dcf": "2.0",          # 유통주식수 분모 · NM 봉인 · 산업 게이트 · 시장 대조
    "comps": "2.0",        # 크로스보더 · 공통 거래일 · LTM
    "sangjeung": "1.1",    # 부동산과다보유 · 순자산 100% · 최대주주 할증
    "dcf_full": "1.1",
    "reverse_dcf": "1.0",   # 목표가 → 필요가정 역산 진단
    "scenarios": "1.0",    # Base/Bull/Bear 동시 산출
}


def _canonical(obj: Any) -> Any:
    """해시 안정성을 위해 정규화 — float 는 유효자리를 잘라 부동소수점 잡음을 없앤다.

    같은 입력을 두 번 넘겼는데 8.700000000000001 vs 8.7 때문에 해시가 갈리면
    재현성 표시가 오히려 거짓말이 된다.
    """
    if isinstance(obj, float):
        return round(obj, 10)
    if isinstance(obj, dict):
        return {k: _canonical(v) for k, v in sorted(obj.items()) if v is not None}
    if isinstance(obj, (list, tuple)):
        return [_canonical(v) for v in obj]
    return obj


def stamp(engine: str, inputs: dict) -> dict:
    """→ {run_id, inputs_hash, engine, engine_version}."""
    payload = json.dumps({"engine": engine, "inputs": _canonical(inputs)},
                         ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    version = ENGINE_VERSIONS.get(engine, "0")
    # 버전을 run_id 에 섞어, 로직이 바뀌면 같은 입력이라도 다른 run_id 가 되게 한다.
    rid = hashlib.sha256(f"{digest}|{version}".encode("utf-8")).hexdigest()[:12]
    return {"run_id": rid, "inputs_hash": digest[:16],
            "engine": engine, "engine_version": version}


def line(stamped: dict) -> str:
    """답변·엑셀·HTML 에 한 줄로 박을 표기."""
    return (f"run {stamped['run_id']} · {stamped['engine']} v{stamped['engine_version']} "
            f"· inputs {stamped['inputs_hash']}")
