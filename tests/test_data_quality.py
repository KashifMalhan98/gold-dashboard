import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fetch_data


class DataQualityTests(unittest.TestCase):
    def test_empty_source_is_reported_missing(self):
        df = pd.DataFrame({"gold": [float("nan")], "spot": [float("nan")], "fx": [float("nan")]},
                         index=pd.date_range(pd.Timestamp.today().normalize(), periods=1))
        report = fetch_data.data_quality_report(df)
        self.assertEqual(report["critical_missing"], ["gold", "spot", "fx"])
        self.assertTrue(all(row["status"] == "MISSING" for row in report["rows"]))

    def test_recent_source_is_fresh(self):
        idx = pd.date_range(pd.Timestamp.today().normalize() - pd.Timedelta(days=2), periods=3, freq="D")
        df = pd.DataFrame({"gold": [1, 2, 3]}, index=idx)
        report = fetch_data.data_quality_report(df)
        self.assertEqual(report["rows"][0]["status"], "FRESH")
        self.assertEqual(report["rows"][0]["recent_filled"], 3)

    def test_failed_source_is_preserved(self):
        df = pd.DataFrame({"gold": [1]}, index=pd.date_range(pd.Timestamp.today().normalize(), periods=1))
        report = fetch_data.data_quality_report(df, ["FAIL gold: timeout"])
        self.assertEqual(report["failed_sources"], ["FAIL gold: timeout"])

    def test_signal_diagnostics_separate_excluded_and_missing(self):
        out = {
            "signals": [
                {"score": 1, "excluded": False},
                {"score": 0, "excluded": False},
                {"score": 0.5, "excluded": False},
                {"score": None, "excluded": False},
                {"score": 1, "excluded": True},
            ],
            "groups": [{"score": 0.7}, {"score": 0.3}, {"score": None}],
        }
        d = fetch_data.signal_diagnostics(out)
        self.assertEqual((d["available"], d["missing"], d["excluded"]), (3, 1, 1))
        self.assertEqual((d["bullish"], d["bearish"], d["neutral"]), (1, 1, 1))
        self.assertEqual(d["groups_confirmed"], 1)



if __name__ == "__main__":
    unittest.main()

class BacktestDiagnosticsTests(unittest.TestCase):
    def test_backtest_includes_redundancy_field(self):
        import numpy as np
        n = 100
        idx = pd.date_range(end=pd.Timestamp.today().normalize(), periods=n, freq="D")
        x = np.linspace(0, 1, n)
        cols = ["gold", "spot", "dxy", "gvz", "fx", "ry", "y2", "nom10", "vix", "spx", "oil", "gdx", "si", "cnh", "sge", "asia_ret", "us_ret", "cftc", "oi", "gl_etf", "in_px", "in_etf"]
        df = pd.DataFrame(index=idx)
        for col in cols:
            df[col] = 100 + np.sin(x * 8) * 3 + x * 5
        df["gold"] = 1900 + np.sin(x * 10) * 20 + x * 30
        df["spot"] = df["gold"] * 1.001
        df["be10"] = df["nom10"] - df["ry"]
        df["sh_prem"] = df["sge"] * 31.1035 / df["cnh"] - df["gold"]
        df["asia_cum"] = df["asia_ret"].fillna(0).cumsum()
        df["us_cum"] = df["us_ret"].fillna(0).cumsum()
        result = fetch_data.backtest(df, horizon=5, max_days=60, min_n=2)
        self.assertIsNotNone(result)
        self.assertIn("redundant_pairs", result)
        self.assertIsInstance(result["redundant_pairs"], list)
