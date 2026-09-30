import unittest
from stats import average


class StatsTests(unittest.TestCase):
    def test_average(self):
        self.assertEqual(average([1, 2, 3]), 2)

    def test_negative_average(self):
        self.assertEqual(average([-4, 2]), -1)


if __name__ == "__main__":
    unittest.main()
