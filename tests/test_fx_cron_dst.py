"""D-06 — the daily FX cron fires at 16:00 Europe/Kyiv and survives the
last-Sunday-of-October DST boundary without drift.

This test exercises APScheduler's CronTrigger directly (no app, no DB), so it
does NOT need the not-yet-built fx_tick wiring — it locks the trigger config
the wiring must use. It is NOT xfail: CronTrigger + ZoneInfo exist today.

In 2026 Ukraine ends DST on Sunday 2026-10-25 (clocks 04:00->03:00 EEST->EET).
We assert the next fire after a point just before that day's 16:00 lands on
2026-10-25 16:00 Kyiv local time, regardless of the offset change.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

KYIV = ZoneInfo("Europe/Kyiv")


def test_cron_fires_at_1600_kyiv_across_october_dst():
    trigger = CronTrigger(hour=16, minute=0, timezone=KYIV)

    # A moment on the DST-changeover morning, before 16:00 Kyiv.
    previous = datetime(2026, 10, 25, 9, 0, tzinfo=KYIV)
    nxt = trigger.get_next_fire_time(None, previous)

    assert nxt is not None
    # Wall-clock 16:00 on the changeover day.
    assert nxt.year == 2026
    assert nxt.month == 10
    assert nxt.day == 25
    assert nxt.hour == 16
    assert nxt.minute == 0
    # Kyiv has switched to EET (UTC+2) by 16:00 that day.
    assert nxt.utcoffset().total_seconds() == 2 * 3600


def test_cron_next_fire_is_1600_kyiv_on_a_normal_day():
    trigger = CronTrigger(hour=16, minute=0, timezone=KYIV)
    previous = datetime(2026, 5, 8, 9, 0, tzinfo=KYIV)
    nxt = trigger.get_next_fire_time(None, previous)

    assert nxt is not None
    assert (nxt.month, nxt.day, nxt.hour, nxt.minute) == (5, 8, 16, 0)
    # Summer: Kyiv is EEST (UTC+3).
    assert nxt.utcoffset().total_seconds() == 3 * 3600
