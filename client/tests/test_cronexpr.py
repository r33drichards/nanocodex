"""The dependency-free crontab parser: field syntax, vixie dom/dow OR
semantics, @aliases, and next_after boundary behavior."""

import unittest
from datetime import datetime

from nanocodex_client.agui.cronexpr import parse


def nxt(expr: str, after: str) -> str:
    return parse(expr).next_after(datetime.fromisoformat(after)).isoformat(timespec="minutes")


class CronExprTest(unittest.TestCase):
    def test_every_minute_advances_one_minute(self):
        self.assertEqual(nxt("* * * * *", "2026-07-06T10:07:31"), "2026-07-06T10:08")

    def test_step_minutes(self):
        self.assertEqual(nxt("*/15 * * * *", "2026-07-06T10:07:00"), "2026-07-06T10:15")
        self.assertEqual(nxt("*/15 * * * *", "2026-07-06T10:45:00"), "2026-07-06T11:00")

    def test_fixed_time_rolls_to_next_day(self):
        self.assertEqual(nxt("30 9 * * *", "2026-07-06T09:30:00"), "2026-07-07T09:30")
        self.assertEqual(nxt("30 9 * * *", "2026-07-06T08:00:00"), "2026-07-06T09:30")

    def test_day_name_and_dow(self):
        # 2026-07-06 is a Monday; next Monday 09:00 is a week out once past 9.
        self.assertEqual(nxt("0 9 * * mon", "2026-07-06T10:00:00"), "2026-07-13T09:00")
        self.assertEqual(nxt("0 9 * * 1", "2026-07-06T08:00:00"), "2026-07-06T09:00")

    def test_sunday_is_0_and_7(self):
        self.assertEqual(nxt("0 0 * * 7", "2026-07-06T10:00:00"),
                         nxt("0 0 * * 0", "2026-07-06T10:00:00"))

    def test_month_names_and_ranges(self):
        self.assertEqual(nxt("0 0 1 jan *", "2026-07-06T10:00:00"), "2027-01-01T00:00")
        self.assertEqual(nxt("0 12 * * mon-fri", "2026-07-10T13:00:00"),  # Friday 13:00
                         "2026-07-13T12:00")                              # -> Monday

    def test_vixie_dom_dow_or(self):
        # Both restricted: the 13th OR a Friday, whichever comes first.
        self.assertEqual(nxt("0 0 13 * fri", "2026-07-06T10:00:00"), "2026-07-10T00:00")
        self.assertEqual(nxt("0 0 13 * fri", "2026-07-10T10:00:00"), "2026-07-13T00:00")

    def test_leap_day(self):
        self.assertEqual(nxt("0 0 29 2 *", "2026-07-06T10:00:00"), "2028-02-29T00:00")

    def test_aliases(self):
        self.assertEqual(nxt("@daily", "2026-07-06T10:00:00"), "2026-07-07T00:00")
        self.assertEqual(nxt("@hourly", "2026-07-06T10:07:00"), "2026-07-06T11:00")
        self.assertEqual(nxt("@monthly", "2026-07-06T10:00:00"), "2026-08-01T00:00")

    def test_lists_and_range_steps(self):
        self.assertEqual(nxt("0,30 8-10/2 * * *", "2026-07-06T08:31:00"), "2026-07-06T10:00")

    def test_invalid_expressions_raise(self):
        for bad in ("", "* * * *", "61 * * * *", "* 24 * * *", "* * 0 * *",
                    "* * * 13 *", "* * * * 8", "a * * * *", "*/0 * * * *",
                    "5-1 * * * *", "@fortnightly"):
            with self.assertRaises(ValueError, msg=bad):
                parse(bad)


if __name__ == "__main__":
    unittest.main()
