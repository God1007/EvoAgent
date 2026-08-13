import unittest

from evoagent.metrics import Metrics


class CounterAndGaugeTests(unittest.TestCase):
    def test_counter_accumulates(self):
        m = Metrics()
        m.inc("widgets_total")
        m.inc("widgets_total", 4)
        self.assertIn("evoagent_widgets_total 5.0", m.prometheus())

    def test_gauge_set_and_add(self):
        m = Metrics()
        m.set_gauge("in_flight", 3)
        m.add_gauge("in_flight", 1)
        m.add_gauge("in_flight", -2)
        out = m.prometheus()
        self.assertIn("# TYPE evoagent_in_flight gauge", out)
        self.assertIn("evoagent_in_flight 2", out)

    def test_gauge_source_sampled_at_scrape(self):
        m = Metrics()
        value = {"n": 7}
        m.register_gauge_source("queue_depth", lambda: value["n"])
        self.assertIn("evoagent_queue_depth 7", m.prometheus())
        value["n"] = 9
        self.assertIn("evoagent_queue_depth 9", m.prometheus())

    def test_broken_gauge_source_does_not_break_scrape(self):
        m = Metrics()

        def boom():
            raise RuntimeError("probe failed")

        m.register_gauge_source("bad", boom)
        m.inc("ok_total")
        out = m.prometheus()
        self.assertIn("evoagent_ok_total", out)
        self.assertNotIn("evoagent_bad", out)


class HistogramTests(unittest.TestCase):
    def test_bucket_counts_are_cumulative(self):
        m = Metrics(buckets=(0.1, 0.5, 1.0))
        for value in (0.05, 0.2, 0.2, 2.0):
            m.observe("lat", value)
        out = m.prometheus()
        self.assertIn("# TYPE evoagent_lat histogram", out)
        self.assertIn('evoagent_lat_bucket{le="0.1"} 1', out)
        self.assertIn('evoagent_lat_bucket{le="0.5"} 3', out)
        self.assertIn('evoagent_lat_bucket{le="1"} 3', out)
        self.assertIn('evoagent_lat_bucket{le="+Inf"} 4', out)
        self.assertIn("evoagent_lat_count 4", out)

    def test_sum_tracks_total(self):
        m = Metrics(buckets=(1.0,))
        m.observe("lat", 0.25)
        m.observe("lat", 0.75)
        self.assertIn("evoagent_lat_sum 1.0", m.prometheus())

    def test_latency_context_manager_records_one_sample(self):
        m = Metrics()
        with m.latency("http_request_GET"):
            pass
        self.assertIn("evoagent_http_request_GET_count 1", m.prometheus())


if __name__ == "__main__":
    unittest.main()
