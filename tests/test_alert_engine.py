import unittest


class TestAlertEngine(unittest.TestCase):

    def test_upper_price_crossing_triggers_alert(self):
        from market.alert_engine import PriceAlertEngine

        engine = PriceAlertEngine(
            reference_price=77600,
            upper_distance=400,
            lower_distance=400,
            repeat_seconds=120,
        )

        result = engine.check_price(
            current_price=78001,
            now_ts=1000,
        )

        self.assertTrue(result["should_alert"])
        self.assertEqual(result["direction"], "UP")


    def test_lower_price_crossing_triggers_alert(self):
        from market.alert_engine import PriceAlertEngine

        engine = PriceAlertEngine(
            reference_price=77600,
            upper_distance=400,
            lower_distance=400,
            repeat_seconds=120,
        )

        result = engine.check_price(
            current_price=77199,
            now_ts=1000,
        )

        self.assertTrue(result["should_alert"])
        self.assertEqual(result["direction"], "DOWN")


    def test_no_repeat_before_interval(self):
        from market.alert_engine import PriceAlertEngine

        engine = PriceAlertEngine(
            reference_price=77600,
            upper_distance=400,
            lower_distance=400,
            repeat_seconds=120,
        )

        first = engine.check_price(
            current_price=78001,
            now_ts=1000,
        )

        second = engine.check_price(
            current_price=78020,
            now_ts=1060,
        )

        self.assertTrue(first["should_alert"])
        self.assertFalse(second["should_alert"])


    def test_repeat_after_interval_if_unacknowledged(self):
        from market.alert_engine import PriceAlertEngine

        engine = PriceAlertEngine(
            reference_price=77600,
            upper_distance=400,
            lower_distance=400,
            repeat_seconds=120,
        )

        engine.check_price(
            current_price=78001,
            now_ts=1000,
        )

        result = engine.check_price(
            current_price=78020,
            now_ts=1121,
        )

        self.assertTrue(result["should_alert"])
        self.assertEqual(result["reason"], "REPEAT")


    def test_acknowledge_stops_repeat_for_current_crossing(self):
        from market.alert_engine import PriceAlertEngine

        engine = PriceAlertEngine(
            reference_price=77600,
            upper_distance=400,
            lower_distance=400,
            repeat_seconds=120,
        )

        engine.check_price(
            current_price=78001,
            now_ts=1000,
        )

        engine.acknowledge()

        result = engine.check_price(
            current_price=78100,
            now_ts=1300,
        )

        self.assertFalse(result["should_alert"])


    def test_returning_inside_rearms_alert(self):
        from market.alert_engine import PriceAlertEngine

        engine = PriceAlertEngine(
            reference_price=77600,
            upper_distance=400,
            lower_distance=400,
            repeat_seconds=120,
        )

        engine.check_price(
            current_price=78001,
            now_ts=1000,
        )

        engine.acknowledge()

        engine.check_price(
            current_price=77900,
            now_ts=1100,
        )

        result = engine.check_price(
            current_price=78010,
            now_ts=1200,
        )

        self.assertTrue(result["should_alert"])
        self.assertEqual(result["reason"], "CROSS")


    def test_distances_are_configurable(self):
        from market.alert_engine import PriceAlertEngine

        engine = PriceAlertEngine(
            reference_price=77600,
            upper_distance=250,
            lower_distance=600,
            repeat_seconds=120,
        )

        upper = engine.check_price(
            current_price=77851,
            now_ts=1000,
        )

        self.assertTrue(upper["should_alert"])
        self.assertEqual(engine.upper_trigger_price, 77850)

        engine.check_price(
            current_price=77600,
            now_ts=1100,
        )

        lower = engine.check_price(
            current_price=76999,
            now_ts=1200,
        )

        self.assertTrue(lower["should_alert"])
        self.assertEqual(engine.lower_trigger_price, 77000)


if __name__ == "__main__":
    unittest.main()
