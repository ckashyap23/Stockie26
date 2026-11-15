"""Streak-only continuation CALL; EOD setup and separate next-open guards."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .constants import CALL, FLAT

NAME = 'StreakContinuationCall'
TRADE_POLICY = dict(entry='NEXT_OPEN', target_pct=0.006, stop_pct=0.004,
                    time_exit_ist='15:10', size_multiplier=0.5)


def add_streak_history_context(df: pd.DataFrame, calendar: pd.DataFrame | None = None) -> pd.DataFrame:
    """Reset row-based histories when known exchange sessions have no features.

    A missing calendar year is not guessed; diagnostics disclose that limitation.
    """
    if calendar is None:
        from technical_analysis_service.common.config import get_settings
        from technical_analysis_service.data_manager.db.client_factory import get_database_client
        db=get_database_client(get_settings()); db.connect()
        try:
            with db.conn.cursor() as cur:
                cur.execute('SELECT calendar_date,is_trading_day FROM "TradingCalendar" WHERE exchange=%s ORDER BY calendar_date',('NSE',))
                calendar=pd.DataFrame(cur.fetchall(),columns=['calendar_date','is_trading_day'])
        finally: db.close()
    days=pd.to_datetime(calendar.loc[calendar.is_trading_day,'calendar_date']).dt.strftime('%Y-%m-%d').tolist()
    previous={day:days[i-1] for i,day in enumerate(days) if i}
    result=df.copy()
    dates=pd.to_datetime(result.signal_date).dt.strftime('%Y-%m-%d')
    result['history_break']=[day in previous and previous[day]!=prior for day,prior in zip(dates,dates.shift(1))]
    if len(result): result.loc[result.index[0],'history_break']=False
    return result


def streak_continuation_details(df: pd.DataFrame) -> pd.DataFrame:
    """Input rows must be chronological. VIX is D close-to-close, in fractions.

    Optional history_break marks missing exchange sessions; rolling observations
    across a break are unknown. Unknown required data yields a nullable signal,
    never a silent pass/fail. Gap lives on D but describes execution session D+1.
    """
    if 'signal_date' in df and not pd.to_datetime(df.signal_date).is_monotonic_increasing:
        raise ValueError('StreakContinuationCall requires chronological signal dates')
    def number(name):
        return pd.to_numeric(df.get(name, pd.Series(np.nan, index=df.index)), errors='coerce')
    close, opening = number('close_1515'), number('open_915')
    high, low = number('high_day'), number('low_day')
    breadth = number('breadth_advance_decline_ratio')
    gap, vix = number('gap_open_atr'), number('vix_chg_pct')
    ret3, efficiency = number('ret_3d'), number('trend_efficiency_5d')
    history_break = df.get('history_break', pd.Series(False, index=df.index)).fillna(True).astype(bool)
    up = (close > opening) & close.notna() & opening.notna()
    streak = []; run = 0
    for rising, broken in zip(up, history_break):
        if broken: run = 0
        run = run + 1 if rising else 0
        streak.append(run)
    streak = pd.Series(streak, index=df.index)
    history3_ok = ~history_break.rolling(2, min_periods=1).max().astype(bool)
    breadth3 = breadth.rolling(3, min_periods=3).mean().where(history3_ok)
    streak_trigger = (streak >= 3) & (ret3 >= 0.01)
    position = ((close-low)/(high-low).where(high > low)).where(close.between(low,high))
    breadth_ok = (breadth >= 1.5) & (breadth3 >= 1.5)
    quality_ok = (position >= 0.7) & (efficiency >= 0.6)
    setup = streak_trigger & breadth_ok & quality_ok
    trigger_unknown = ~streak_trigger & (ret3.isna() |
        close.rolling(3,min_periods=3).count().lt(3) |
        opening.rolling(3,min_periods=3).count().lt(3) | ~history3_ok)
    setup_missing = (close.isna() | breadth.isna() | breadth3.isna() |
                     position.isna() | efficiency.isna() | trigger_unknown)
    gap_missing = gap.isna()
    vix_missing = vix.isna()
    guarded = gap.lt(-0.5) | gap.gt(1.5) | vix.gt(0.10)
    ungraded = gap_missing | vix_missing | setup_missing
    signal = pd.Series(np.where(setup & ~guarded, CALL, FLAT), index=df.index, dtype=object)
    signal.loc[ungraded] = pd.NA
    status = pd.Series(np.select([gap_missing, vix_missing, setup_missing, setup & guarded, setup],
        ['UNGRADED_MISSING_GAP','UNGRADED_MISSING_VIX','UNGRADED_SETUP_INPUT',
         'GUARD_VETO','CALL'], default='NO_SETUP'), index=df.index)
    return pd.DataFrame(dict(streak=streak, ret_3d=ret3, streak_trigger=streak_trigger,
        breadth_ratio=breadth, breadth_mean_3d=breadth3,
        breadth_ok=breadth_ok, close_range_position=position, efficiency_5d=efficiency,
        quality_ok=quality_ok, eod_setup=setup, gap_open_atr=gap,
        signal_day_vix_chg_pct=vix, gap_down_veto=gap.lt(-0.5), gap_up_veto=gap.gt(1.5),
        vix_veto=vix.gt(0.10), evaluation_eligible=~ungraded, status=status, prediction=signal))


def streak_continuation_call(df: pd.DataFrame) -> dict[str, pd.Series]:
    return {'strategy_StreakContinuationCall_signal': streak_continuation_details(df).prediction}


def research_conflict_signals(call: pd.Series, exhaustion_put: pd.Series) -> pd.DataFrame:
    """Research overlay only: confirmed continuation CALL suppresses exhaustion PUT."""
    conflict = call.eq(CALL).fillna(False) & exhaustion_put.eq('PUT').fillna(False)
    return pd.DataFrame({'continuation_call':call, 'exhaustion_put_original':exhaustion_put,
        'conflict':conflict, 'exhaustion_put_research_overlay':exhaustion_put.where(~conflict, FLAT)})
