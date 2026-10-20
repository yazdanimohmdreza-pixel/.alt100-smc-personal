"""Regression tests: zone confirmation must precede an actual five-minute overlap."""
import unittest
from strategy import Candle, Zone, has_confirmed_zone_touch


def candle(start, low, high, minutes):
    return Candle(start, start + minutes * 60_000 - 1,
                  (high+low)/2, high, low, (high+low)/2, 1000)


class ZoneTouchTests(unittest.TestCase):
    def setUp(self):
        # 15m candle closes at 14:59.999; the zone is NOT known before then.
        self.quarter = [candle(0, 99, 102, 15)]
        self.zone = Zone('FVG', 100, 101, 0, 'test')

    def test_trigger_on_zone_creation_bar_is_not_after_confirmation(self):
        # 5m bar from 10:00 to 14:59.999 touches zone, but is pre-confirmation.
        five = [candle(10*60_000, 100.2, 100.8, 5)]
        self.assertFalse(has_confirmed_zone_touch(five, self.quarter, self.zone))

    def test_first_new_bar_after_confirming_zone_is_eligible(self):
        five = [candle(15*60_000, 100.2, 100.8, 5)]
        self.assertTrue(has_confirmed_zone_touch(five, self.quarter, self.zone))

    def test_combining_high_of_one_and_low_of_other_cannot_fake_touch(self):
        # No individual candle intersects 100..101 despite combined extrema doing so.
        five = [candle(15*60_000, 98, 99, 5),
                candle(20*60_000, 102, 103, 5)]
        self.assertFalse(has_confirmed_zone_touch(five, self.quarter, self.zone))

    def test_old_contact_before_zone_confirmation_does_not_count(self):
        five = [candle(10*60_000, 100.3, 100.8, 5),
                candle(15*60_000, 101.5, 102.5, 5)]
        self.assertFalse(has_confirmed_zone_touch(five, self.quarter, self.zone))

    def test_only_latest_six_bars_qualify(self):
        five = [candle((15 + i*5)*60_000, 100.3, 100.8, 5) if i == 0
                else candle((15 + i*5)*60_000, 102, 103, 5) for i in range(7)]
        self.assertFalse(has_confirmed_zone_touch(five, self.quarter, self.zone))

    def test_invalid_zone_index_cannot_authorize_touch(self):
        invalid_zone = Zone('FVG', 100, 101, 2, 'test')
        five = [candle(15*60_000, 100.2, 100.8, 5)]
        self.assertFalse(has_confirmed_zone_touch(five, self.quarter, invalid_zone))


if __name__ == '__main__':
    unittest.main()
