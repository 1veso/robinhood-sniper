"""Real TypeSafe wire precision must not weaken either desk's exit cutoff."""
import unittest
from copy import deepcopy
from sniper import jev
from sniper.nitrogen import judge


class JevWireRoundingTests(unittest.TestCase):
    def test_observed_high_risk_rounding_keeps_reported_value(self):
        probabilities={str(i): float(i == 4) for i in range(5)}
        self.assertEqual(jev.validate_score(3.99, probabilities), 3.99)
        self.assertEqual(jev.conservative_score({'score':3.99,'probabilities':probabilities}), 4)

    def test_larger_and_unexplained_high_precision_mismatch_rejected(self):
        probabilities={str(i): float(i == 4) for i in range(5)}
        for score in (3.98, 3.99001):
            with self.assertRaises(ValueError):jev.validate_score(score, probabilities)

    def test_rounded_value_cannot_cross_either_exit_cutoff(self):
        from test_jev import response
        for module in (jev, judge):
            raw=response(score=2)
            raw['answers']['exit_risk']['legend']={str(i): x for i,x in enumerate(module.LEVELS)}
            raw['answers']['exit_risk']['probabilities']={'0':0.,'1':0.,'2':.99,'3':.01,'4':0.}
            self.assertEqual(module.gate(deepcopy(raw))[0], 'PASS')
            raw['answers']['exit_risk']['score']=2.01
            raw['answers']['exit_risk']['probabilities']={'0':0.,'1':0.,'2':1.,'3':0.,'4':0.}
            self.assertEqual(module.gate(deepcopy(raw))[0], 'PASS')


if __name__ == '__main__':unittest.main()
