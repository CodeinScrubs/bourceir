import assert from 'node:assert/strict';
import test from 'node:test';
import {numeric, portfolioHeaders, verifySnapshot} from '../scripts/farabi_capture.mjs';

// Synthetic fixtures remain under tests and never enter the market/account databases.
function snapshot() {
  return {expected_positions:2, headers:[...portfolioHeaders],
    rows:[['A','1','1','100','1','(0%) 1','100','(+0%) +0'],
          ['B','2','2','200','2','(0%) 2','200','(+0%) +0']],
    summary:{buy_total_rial:300, portfolio_value_rial:300, broker_pnl_rial:0, net_assets_rial:310, cash_rial:10}};
}

test('numeric fields support Persian/Arabic digits and reject unknown or unsafe values', () => {
  assert.equal(numeric('−۱۲٬۳۴۵'), -12345);
  assert.equal(numeric('١٢٣.٥'), 123.5);
  for (const value of ['∞','',null,'12 ریال','9e8','9007199254740992']) assert.throws(() => numeric(value));
});

test('capture requires all unique rows and independent control totals', () => {
  assert.equal(verifySnapshot(snapshot()).rows.length,2);
  for (const mutate of [s => s.rows.pop(), s => s.rows[1]=s.rows[0],
    s => s.headers.reverse(), s => s.summary.portfolio_value_rial+=3,
    s => s.summary.net_assets_rial+=3, s => s.summary.broker_pnl_rial+=3,
    s => s.expected_positions=0]) {
    const source=snapshot(); mutate(source); assert.throws(() => verifySnapshot(source));
  }
  const rounded=snapshot(); rounded.summary.portfolio_value_rial-=1; rounded.summary.net_assets_rial-=1;
  assert.doesNotThrow(() => verifySnapshot(rounded));
});
