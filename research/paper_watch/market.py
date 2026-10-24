"""Forex weekly calendar; NY 17:00 boundary follows IANA DST rules.
Holidays are not inferred: unexplained missing active quotes remain errors.
"""
import pandas as pd
from .engine import utc,BAR

def is_open(time):
    local=utc(time).tz_convert('America/New_York')
    return local.dayofweek<4 or (local.dayofweek==4 and local.hour<17) or (local.dayofweek==6 and local.hour>=17)

def expected_closed(next_unseen,end):
    end=utc(end);start=utc(next_unseen)
    grid=pd.date_range(start,end,freq='5min',inclusive='left') if start<end else []
    return not is_open(end) and not any(is_open(t) for t in grid)
