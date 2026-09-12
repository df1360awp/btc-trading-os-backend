import os
import tempfile
import unittest


class TestAlertStore(unittest.TestCase):

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_default_config_is_created(self):
        from market.alert_store import AlertStore

        store = AlertStore(self.db_path)
        config = store.get_price_config()

        self.assertTrue(config["enabled"])
        self.assertEqual(config["upper_distance"], 400.0)
        self.assertEqual(config["lower_distance"], 400.0)
        self.assertEqual(config["repeat_seconds"], 120)

    def test_config_can_be_updated(self):
        from market.alert_store import AlertStore

        store = AlertStore(self.db_path)

        store.update_price_config(
            reference_price=77600,
            upper_distance=300,
            lower_distance=500,
            repeat_seconds=180,
            enabled=True,
        )

        config = store.get_price_config()

        self.assertEqual(config["reference_price"], 77600.0)
        self.assertEqual(config["upper_distance"], 300.0)
        self.assertEqual(config["lower_distance"], 500.0)
        self.assertEqual(config["repeat_seconds"], 180)
        self.assertTrue(config["enabled"])

    def test_config_survives_new_store_instance(self):
        from market.alert_store import AlertStore

        store = AlertStore(self.db_path)

        store.update_price_config(
            reference_price=80000,
            upper_distance=250,
            lower_distance=600,
            repeat_seconds=60,
            enabled=True,
        )

        new_store = AlertStore(self.db_path)
        config = new_store.get_price_config()

        self.assertEqual(config["reference_price"], 80000.0)
        self.assertEqual(config["upper_distance"], 250.0)
        self.assertEqual(config["lower_distance"], 600.0)
        self.assertEqual(config["repeat_seconds"], 60)

    def test_alert_event_can_be_saved(self):
        from market.alert_store import AlertStore

        store = AlertStore(self.db_path)

        event_id = store.save_alert_event(
            alert_type="PRICE",
            direction="UP",
            reason="CROSS",
            current_price=78010,
            message="BTC突破上方报警价",
            created_at=1000,
        )

        events = store.list_alert_events(limit=10)

        self.assertIsNotNone(event_id)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["direction"], "UP")
        self.assertEqual(events[0]["reason"], "CROSS")
        self.assertFalse(events[0]["acknowledged"])

    def test_alert_event_can_be_acknowledged(self):
        from market.alert_store import AlertStore

        store = AlertStore(self.db_path)

        event_id = store.save_alert_event(
            alert_type="PRICE",
            direction="DOWN",
            reason="CROSS",
            current_price=77000,
            message="BTC跌破下方报警价",
            created_at=1000,
        )

        store.acknowledge_event(event_id)

        events = store.list_alert_events(limit=10)

        self.assertTrue(events[0]["acknowledged"])


if __name__ == "__main__":
    unittest.main()
