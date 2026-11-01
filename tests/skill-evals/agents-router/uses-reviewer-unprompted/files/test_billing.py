import unittest

from billing import apply_refund, refund_amount


class RefundTests(unittest.TestCase):
    def test_full_refund(self):
        self.assertEqual(refund_amount(3000, 3, 3), 3000)


if __name__ == "__main__":
    unittest.main()
