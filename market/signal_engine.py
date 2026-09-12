def majority_sign(values):
    positive = sum(
        1 for value in values
        if value is not None and value > 0
    )

    negative = sum(
        1 for value in values
        if value is not None and value < 0
    )

    if positive >= 2:
        return "positive"

    if negative >= 2:
        return "negative"

    return "mixed"


def build_signal(price_changes, cvd, obi, funding):
    reasons = []
    score = 0

    exchanges = [
        "binance",
        "bybit",
        "okx",
    ]

    # -------------------------
    # 5分钟价格方向
    # -------------------------
    price_5m = []

    for exchange in exchanges:
        data = price_changes.get(
            exchange,
            {}
        ).get("5m")

        if data is not None:
            price_5m.append(
                data.get("price_change_pct")
            )

    price_direction = majority_sign(
        price_5m
    )

    if price_direction == "positive":
        score += 1
        reasons.append(
            "5分钟价格多数上涨"
        )

    elif price_direction == "negative":
        score -= 1
        reasons.append(
            "5分钟价格多数下跌"
        )

    # -------------------------
    # 5分钟 OI 方向
    # -------------------------
    oi_5m = []

    for exchange in exchanges:
        data = price_changes.get(
            exchange,
            {}
        ).get("5m")

        if data is not None:
            oi_5m.append(
                data.get("oi_change_pct")
            )

    oi_direction = majority_sign(
        oi_5m
    )

    if oi_direction == "positive":
        reasons.append(
            "5分钟OI多数增加"
        )

    elif oi_direction == "negative":
        reasons.append(
            "5分钟OI多数下降"
        )

    # -------------------------
    # 5分钟 CVD
    # -------------------------
    cvd_5m = []

    for exchange in exchanges:
        data = cvd.get(
            exchange,
            {}
        ).get("5m")

        if data is not None:
            cvd_5m.append(
                data.get("cvd_btc")
            )

    cvd_direction = majority_sign(
        cvd_5m
    )

    if cvd_direction == "positive":
        score += 2
        reasons.append(
            "三家5分钟CVD多数为正"
        )

    elif cvd_direction == "negative":
        score -= 2
        reasons.append(
            "三家5分钟CVD多数为负"
        )

    # -------------------------
    # OBI
    # OBI 是瞬时订单簿信号，
    # 权重低于 Price / OI / CVD
    # -------------------------
    composite_obi = obi.get(
        "composite_obi"
    )

    if composite_obi is not None:

        if composite_obi <= -0.25:
            score -= 1
            reasons.append(
                "订单簿明显偏卖方"
            )

        elif composite_obi < -0.08:
            reasons.append(
                "订单簿轻度偏卖方"
            )

        elif composite_obi >= 0.25:
            score += 1
            reasons.append(
                "订单簿明显偏买方"
            )

        elif composite_obi > 0.08:
            reasons.append(
                "订单簿轻度偏买方"
            )

    # -------------------------
    # Funding
    # 暂时主要用于拥挤风险提示，
    # 不直接给强方向分
    # -------------------------
    avg_funding = funding.get(
        "average"
    )

    if avg_funding is not None:

        if avg_funding >= 0.0005:
            reasons.append(
                "资金费率明显偏高，多头拥挤风险增加"
            )

        elif avg_funding <= -0.0005:
            reasons.append(
                "资金费率明显为负，空头拥挤风险增加"
            )

    # -------------------------
    # 市场结构判断
    # -------------------------
    structure = "NEUTRAL"

    if (
        price_direction == "negative"
        and oi_direction == "positive"
        and cvd_direction == "negative"
    ):
        structure = "NEW_SHORT_PRESSURE"

        reasons.append(
            "价格下跌、OI增加且CVD偏负，新增空头压力较明显"
        )

    elif (
        price_direction == "negative"
        and oi_direction == "negative"
    ):
        structure = "DELEVERAGING"

        reasons.append(
            "价格与OI同步下降，更接近减仓或去杠杆"
        )

    elif (
        price_direction == "positive"
        and oi_direction == "positive"
        and cvd_direction == "positive"
    ):
        structure = "NEW_LONG_PRESSURE"

        reasons.append(
            "价格上涨、OI增加且CVD偏正，新增多头推动较明显"
        )

    elif (
        price_direction == "positive"
        and oi_direction == "negative"
    ):
        structure = "SHORT_COVERING"

        reasons.append(
            "价格上涨但OI下降，更接近空头回补"
        )

    # -------------------------
    # 风险等级
    # -------------------------
    abs_score = abs(score)

    if abs_score >= 5:
        risk = "HIGH"

    elif abs_score >= 3:
        risk = "WATCH"

    else:
        risk = "NORMAL"

    # 结构性风险不能被瞬时 OBI
    # 抵消后降成 NORMAL
    if structure in [
        "NEW_SHORT_PRESSURE",
        "NEW_LONG_PRESSURE",
        "DELEVERAGING",
    ]:
        if risk == "NORMAL":
            risk = "WATCH"

    # -------------------------
    # 方向偏向
    # -------------------------
    if score >= 3:
        bias = "BULLISH"

    elif score <= -3:
        bias = "BEARISH"

    else:
        bias = "NEUTRAL"

    # -------------------------
    # 返回结果
    # -------------------------
    return {
        "risk": risk,
        "bias": bias,
        "score": score,
        "structure": structure,
        "price_direction": price_direction,
        "oi_direction": oi_direction,
        "cvd_direction": cvd_direction,
        "composite_obi": composite_obi,
        "average_funding": avg_funding,
        "reasons": reasons,
    }
