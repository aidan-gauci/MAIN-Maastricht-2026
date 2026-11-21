"""Figures that are a forecast to act on are never read out; ordinary reported figures still are.

Every sentence here is invented for the test.
"""
import unittest

from backend.app.pipeline import is_advice_figure


def fact(measure, evidence, translations=None):
    return {"measure": measure, "evidence": evidence, "measure_translations": translations or {}}


class AdviceFigureTest(unittest.TestCase):
    def test_forecasts_to_act_on_are_withheld(self):
        for measure, evidence in (
                ("Upside potential", "The new target implies about 18% upside potential for Zorbo."),
                ("Upside", "The strategist sees roughly 15% upside for the index."),
                ("Downside risk", "Analysts see 9% downside for Quarx shares."),
                ("Fair value", "Zorbo trades 12% below its fair value estimate."),
                ("Undervaluation", "The shares look undervalued by 20 percent."),
                ("Year-end index target", "The bank raised its year-end target for the S&P to 7,900."),
                ("Expected return", "The fund's expected return is 7 percent a year."),
                ("Average broker recommendation", "Zorbo has an average brokerage recommendation of 1.4."),
                ("Earnings ESP", "Zorbo has an Earnings ESP of 3.1 percent."),
                ("200-day moving average", "The stock sits 4% above its 200-day moving average."),
                ("Breakout level", "A breakout above 120 dollars would open more gains."),
                ("Buy zone", "Traders flag a buy zone near 88 dollars."),
                ("Kurspotenzial", "Das Kurspotenzial liegt bei 14 Prozent."),
                ("Opwaarts potentieel", "Het opwaarts potentieel bedraagt 11 procent.")):
            with self.subTest(measure=measure):
                self.assertTrue(is_advice_figure(fact(measure, evidence)))

    def test_a_translated_name_is_checked_too(self):
        self.assertTrue(is_advice_figure(fact("Price gap", "The gap is 14 percent.", {"de": "Kurspotenzial"})))

    def test_reported_figures_are_still_read(self):
        for measure, evidence in (
                ("Average selling price increase", "The average selling price rose 28 percent."),
                ("Approval rating", "The president's approval rating fell to 32 percent."),
                ("Deal value", "The deal is worth 3.79 billion pounds."),
                ("Quarterly revenue", "Revenue rose 12% to 4.1 billion dollars in the quarter."),
                ("Bullish positions liquidated", "Bullish positions worth 1.31 billion dollars were liquidated."),
                ("Policy rate", "The central bank kept its policy rate at 2 percent."),
                ("Verkopen", "De verkopen stegen met 5 procent.")):
            with self.subTest(measure=measure):
                self.assertFalse(is_advice_figure(fact(measure, evidence)))


if __name__ == "__main__":
    unittest.main()
