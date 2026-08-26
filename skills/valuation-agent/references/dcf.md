# DCF

## 적용 판단

영업 현금흐름을 합리적으로 예측할 수 있고 자본구조와 비영업 항목을 분리할 수 있을 때 FCFF 기반 DCF를 사용한다. 초기 또는 구조조정 기업처럼 현금흐름의 변동성이 큰 경우 시나리오와 다른 방법의 교차검증을 우선 검토한다.

## 기준 계산

일반 구조는 다음과 같다.

```text
FCFF = EBIT x (1 - tax rate) + D&A - CAPEX - change in NWC
Terminal Value = FCFF(n+1) / (WACC - g)
Enterprise Value = PV(forecast FCFF) + PV(Terminal Value)
Equity Value = Enterprise Value - debt and debt-like claims + cash and non-operating assets +/- other claims
Per-share Value = Equity Value / relevant diluted shares
```

공식은 회사와 목적에 맞게 조정하되 변경을 명시한다. 세율, 리스부채, 연금, 소수주주지분, 관계기업, 비영업자산 및 희석 주식의 처리를 일관되게 적용한다.

## 자동 기준안

과거 실적과 현재 시장 자료에서 다음을 먼저 조사한다.

- 매출 성장과 영업이익률
- 세율
- D&A, CAPEX 및 운전자본 driver
- 순부채와 debt-like 항목
- 무위험수익률, ERP, 베타, 타인자본비용 및 자본구조
- 장기 성장률의 외부 기준

과거 평균을 미래 가정으로 자동 채택하지 않는다. 정상화가 필요한 일회성, 경기순환, 증설, 인수 또는 회계 변경을 식별한다.

## material 검토

결과에 유의한 영향을 주는 항목만 사용자 결정으로 올린다.

- 예측기간과 성장 경로
- 정상 영업이익률
- CAPEX와 감가상각의 정상화
- 운전자본 투자
- WACC 구성요소
- 영구성장률 또는 exit multiple
- 비영업 항목과 순부채 bridge
- 시나리오 또는 민감도 범위

## 검증

- 영구성장법에서는 `g < WACC`인지 확인한다.
- 예측 FCFF의 부호와 변동을 영업 driver로 설명한다.
- Terminal Value의 EV 비중이 크면 결론의 민감도를 전면에 표시한다.
- WACC와 현금흐름의 명목/실질, 세전/세후, 통화를 맞춘다.
- Enterprise Value에서 Equity Value로의 bridge를 항목별로 재현한다.
- 주당가치에는 목적에 맞는 희석 주식 수를 사용한다.
- WACC, g, 마진 또는 성장률 중 material driver의 민감도를 제시한다.

음수 FCFF 자체는 오류가 아니다. 음수 Enterprise Value 또는 0 이하의 Equity Value가 나오면 계산을 숨기지 말고 가정, bridge 및 경제적 의미를 재검토한다. 주당가치가 의사결정에 유용하지 않으면 `NM`으로 표시하고 이유를 설명한다.

