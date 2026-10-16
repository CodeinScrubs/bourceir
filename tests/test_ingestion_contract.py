"""Corrupt quantities and conflicting provider ownership cannot silently enter features."""

import numpy as np
import pandas as pd
import pytest

from core.client_quality import validated_client_data
from tests.test_client_quality import coherent_history


@pytest.mark.parametrize('column', ['volume', 'trade_count'])
@pytest.mark.parametrize('bad', [1.5, -1, True, np.bool_(False), float('inf'), '12', 2**63])
def test_invalid_integer_quantity_rejects_the_entire_batch(db, column, bad):
    prices, _ = coherent_history()
    sid = db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='1', instrument_type_code=300))
    prices = prices.head(2).copy()
    prices[column] = prices[column].astype(object)
    prices.loc[1, column] = bad
    with pytest.raises(ValueError):
        db.upsert_daily_prices_df(sid, prices)
    assert db.conn.execute('SELECT COUNT(*) FROM daily_prices').fetchone()[0] == 0


def test_integer_valued_float_and_unknown_are_preserved(db):
    prices, _ = coherent_history()
    sid = db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='1', instrument_type_code=300))
    prices = prices.head(2).copy()
    prices['volume'] = pd.Series([1000000., np.nan])
    prices['trade_count'] = pd.Series([100., pd.NA], dtype=object)
    assert db.upsert_daily_prices_df(sid, prices) == 2
    stored = db.conn.execute('SELECT volume, trade_count FROM daily_prices ORDER BY date').fetchall()
    assert tuple(stored[0]) == (1000000, 100) and tuple(stored[1]) == (None, None)


def test_foreign_client_source_is_not_used_or_overwritten(db):
    prices, clients = coherent_history()
    sid = db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='1', instrument_type_code=300))
    db.upsert_client_types_df(sid, clients.tail(1))
    with db.conn:
        db.conn.execute("UPDATE client_types_daily SET source='unapproved-provider'")
    assert db.get_client_types_df('آزمون').empty
    foreign = db.get_client_types_df('آزمون', source='unapproved-provider')
    assert len(foreign) == 1
    clean = validated_client_data(prices, db.get_client_types_df('آزمون'))
    assert clean.buy_i_value.isna().all()
    # New dates preceding the conflict must roll back as well.
    with pytest.raises(ValueError, match='source conflict'):
        db.upsert_client_types_df(sid, clients)
    after = db.conn.execute('SELECT source, buy_i_value FROM client_types_daily').fetchall()
    assert len(after) == 1 and tuple(after[0]) == ('unapproved-provider', foreign.buy_i_value.iloc[0])


def test_same_source_client_refresh_and_date_window_still_work(db):
    _, clients = coherent_history()
    sid = db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='1', instrument_type_code=300))
    db.upsert_client_types_df(sid, clients)
    refreshed = clients.copy()
    refreshed['buy_i_value'] = 0.
    assert db.upsert_client_types_df(sid, refreshed) == 25
    frame = db.get_client_types_df('آزمون', start_date=clients.date.iloc[2], end_date=clients.date.iloc[5])
    assert len(frame) == 4 and frame.buy_i_value.eq(0).all()
