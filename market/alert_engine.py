class PriceAlertEngine:
    def __init__(
        self,
        reference_price,
        upper_distance=400,
        lower_distance=400,
        repeat_seconds=120,
        enabled=True,
    ):
        if upper_distance <= 0:
            raise ValueError("upper_distance must be greater than 0")

        if lower_distance <= 0:
            raise ValueError("lower_distance must be greater than 0")

        if repeat_seconds <= 0:
            raise ValueError("repeat_seconds must be greater than 0")

        self.reference_price = float(reference_price)

        self.upper_distance = float(upper_distance)
        self.lower_distance = float(lower_distance)

        self.repeat_seconds = int(repeat_seconds)

        self.enabled = enabled

        # 当前正在触发的方向：
        # None / UP / DOWN
        self.current_direction = None

        # 当前这一轮报警是否已经由用户确认
        self.acknowledged = False

        # 上一次真正发出报警的时间
        self.last_alert_ts = None


    @property
    def upper_trigger_price(self):
        return (
            self.reference_price
            + self.upper_distance
        )


    @property
    def lower_trigger_price(self):
        return (
            self.reference_price
            - self.lower_distance
        )


    def _get_direction(self, current_price):
        current_price = float(current_price)

        if current_price > self.upper_trigger_price:
            return "UP"

        if current_price < self.lower_trigger_price:
            return "DOWN"

        return None


    def _no_alert(self, direction=None):
        return {
            "should_alert": False,
            "direction": direction,
            "reason": None,
            "reference_price": self.reference_price,
            "upper_trigger_price": self.upper_trigger_price,
            "lower_trigger_price": self.lower_trigger_price,
            "acknowledged": self.acknowledged,
        }


    def _create_alert(
        self,
        direction,
        reason,
        current_price,
        now_ts,
    ):
        self.last_alert_ts = now_ts

        return {
            "should_alert": True,
            "direction": direction,
            "reason": reason,
            "current_price": float(current_price),
            "reference_price": self.reference_price,
            "upper_trigger_price": self.upper_trigger_price,
            "lower_trigger_price": self.lower_trigger_price,
            "acknowledged": self.acknowledged,
        }


    def check_price(
        self,
        current_price,
        now_ts,
    ):
        if not self.enabled:
            return self._no_alert()

        direction = self._get_direction(
            current_price
        )

        # -------------------------
        # 价格重新回到区间
        # -------------------------
        if direction is None:

            self.current_direction = None
            self.acknowledged = False
            self.last_alert_ts = None

            return self._no_alert()

        # -------------------------
        # 第一次越界
        # 或从另一方向直接越界
        # -------------------------
        if direction != self.current_direction:

            self.current_direction = direction
            self.acknowledged = False

            return self._create_alert(
                direction=direction,
                reason="CROSS",
                current_price=current_price,
                now_ts=now_ts,
            )

        # -------------------------
        # 用户已经确认本轮报警
        # -------------------------
        if self.acknowledged:
            return self._no_alert(
                direction=direction
            )

        # -------------------------
        # 仍然越界
        # 检查是否到了重复提醒时间
        # -------------------------
        if self.last_alert_ts is None:

            return self._create_alert(
                direction=direction,
                reason="CROSS",
                current_price=current_price,
                now_ts=now_ts,
            )

        elapsed = (
            now_ts
            - self.last_alert_ts
        )

        if elapsed >= self.repeat_seconds:

            return self._create_alert(
                direction=direction,
                reason="REPEAT",
                current_price=current_price,
                now_ts=now_ts,
            )

        return self._no_alert(
            direction=direction
        )


    def acknowledge(self):
        """
        用户点击“已知晓”。

        当前这一轮越界停止重复响铃。
        只有价格重新回到正常区间以后，
        才会重新武装。
        """

        if self.current_direction is not None:
            self.acknowledged = True

        return {
            "acknowledged": self.acknowledged,
            "direction": self.current_direction,
        }


    def update_config(
        self,
        reference_price=None,
        upper_distance=None,
        lower_distance=None,
        repeat_seconds=None,
        enabled=None,
    ):
        """
        动态修改报警参数。

        ±400 只是默认值，
        所有参数都可以修改。
        """

        if reference_price is not None:
            self.reference_price = float(
                reference_price
            )

        if upper_distance is not None:

            if upper_distance <= 0:
                raise ValueError(
                    "upper_distance must be greater than 0"
                )

            self.upper_distance = float(
                upper_distance
            )

        if lower_distance is not None:

            if lower_distance <= 0:
                raise ValueError(
                    "lower_distance must be greater than 0"
                )

            self.lower_distance = float(
                lower_distance
            )

        if repeat_seconds is not None:

            if repeat_seconds <= 0:
                raise ValueError(
                    "repeat_seconds must be greater than 0"
                )

            self.repeat_seconds = int(
                repeat_seconds
            )

        if enabled is not None:
            self.enabled = bool(enabled)

        # 修改配置后重新武装
        self.current_direction = None
        self.acknowledged = False
        self.last_alert_ts = None

        return self.get_status()


    def get_status(self):
        return {
            "enabled": self.enabled,
            "reference_price": self.reference_price,
            "upper_distance": self.upper_distance,
            "lower_distance": self.lower_distance,
            "upper_trigger_price": self.upper_trigger_price,
            "lower_trigger_price": self.lower_trigger_price,
            "repeat_seconds": self.repeat_seconds,
            "current_direction": self.current_direction,
            "acknowledged": self.acknowledged,
            "last_alert_ts": self.last_alert_ts,
        }
