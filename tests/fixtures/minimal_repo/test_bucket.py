import unittest

from calculator import add


class BucketTests(unittest.TestCase):
    def test_add_remains_available(self) -> None:
        self.assertEqual(add(10, 7), 17)
