"""Atomic prospective attention recording; intended for a separate public-market DB."""

import json
from contextlib import contextmanager

from core.opportunity_radar import VERSION, attention_events, digest, scope_rows, stamp


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


@contextmanager
def read_snapshot(connection):
    """Pin related reads; leave an existing caller-owned transaction untouched.

    SQLite's read-only connection mode does not itself begin a transaction. These
    scopes contain reads only; their own snapshot is released without a commit.
    Nested scopes share the outer snapshot instead of ending it prematurely.
    """
    owned = not connection.in_transaction
    if owned:
        connection.execute('BEGIN')
    try:
        yield
    finally:
        if owned:
            connection.rollback()


class RadarRepository:
    def __init__(self, db):
        self.db = db

    def current(self):
        row = self.db.conn.execute('SELECT * FROM radar_runs ORDER BY rowid DESC LIMIT 1').fetchone()
        return dict(row) if row else None

    def retained_scope(self):
        """Preserve observed identities across upgrades, including earlier narrowed runs.

        Chain verification authenticates internal consistency, not exchange membership.
        Do not carry prices, notices or actionable episodes into the new run.
        """
        identities, sources = [], []
        for run in self.db.conn.execute('SELECT * FROM radar_runs ORDER BY rowid'):
            receipt = self.verify(run['id'])
            protocol = json.loads(run['protocol_json'])
            identities = scope_rows([*identities, *protocol.get('initial_scope', [])])
            for cycle in self.db.conn.execute('SELECT report_json FROM radar_cycles WHERE run_id=? ORDER BY id',
                                               (run['id'],)):
                identities = scope_rows([*identities, *json.loads(cycle['report_json'])['scope']])
            sources.append(receipt)
        return dict(identities=identities, source_runs=sources)

    def activate(self, protocol):
        if protocol.get('version') != VERSION or protocol.get('accepted_for_signals') is not False:
            raise ValueError('A factual radar protocol is required')
        stamp(protocol['registered_at'])
        with self.db.conn:
            self.db.conn.execute('INSERT INTO radar_runs VALUES (?,?,?,?)',
                (protocol['id'], protocol['registered_at'], encode(protocol), digest(protocol)))
        return protocol['id']

    def _episodes(self, run_id):
        return {row['episode_key']: json.loads(row['payload_json']) for row in self.db.conn.execute(
            'SELECT * FROM radar_episodes WHERE run_id=? ORDER BY episode_key', (run_id,))}

    def append(self, run_id, report, *, before_commit=None):
        if before_commit is not None and not callable(before_commit):
            raise ValueError('Radar precommit guard must be callable')
        if self.db.conn.in_transaction:
            raise ValueError('Radar recording owns its transaction')
        self.db.conn.execute('BEGIN IMMEDIATE')
        try:
            run = self.db.conn.execute('SELECT * FROM radar_runs WHERE id=?', (run_id,)).fetchone()
            if run is None:
                raise ValueError('Unknown radar protocol')
            protocol = json.loads(run['protocol_json'])
            if (digest(protocol) != run['protocol_sha256'] or report['version'] != VERSION
                    or report.get('run_id') != run_id or report.get('accepted_for_signals') is not False):
                raise ValueError('Radar protocol fingerprint differs')
            at = stamp(report['observed_at'])
            previous = self.db.conn.execute('SELECT * FROM radar_cycles WHERE run_id=? ORDER BY id DESC LIMIT 1', (run_id,)).fetchone()
            if at < stamp(previous['recorded_at'] if previous else run['registered_at']):
                raise ValueError('Clock rollback: radar observation time is not monotonic')
            episodes = self._episodes(run_id)
            if previous and digest(episodes) != previous['episodes_sha256']:
                raise ValueError('Radar episode cache differs from the committed chain')
            incoming = report.get('notices', [])
            known = []
            for notice in incoming:
                if self.db.conn.execute('SELECT 1 FROM radar_notice_versions WHERE run_id=? AND notice_key=?',
                                        (run_id, notice['notice_key'])).fetchone():
                    known.append(notice['notice_key'])
            state, events = attention_events(dict(episodes=episodes, notice_keys=known),
                report['rows'], incoming, registered_at=run['registered_at'], observed_at=report['observed_at'],
                price_scan=report['price_scan'])
            if previous:
                old_scope = {row['ins_code'] for row in json.loads(previous['report_json'])['scope']}
                events.extend(dict(kind='new_scope_identity', symbol=row['symbol_fa'], ins_code=row['ins_code'],
                    first_seen_at=report['observed_at'], baseline=False) for row in report['scope']
                    if row['ins_code'] not in old_scope)
            sealed = dict(report, events=events)
            report_hash = digest(sealed)
            before = previous['chain_sha256'] if previous else run['protocol_sha256']
            chain = digest(dict(previous=before, report=report_hash, run_id=run_id, recorded_at=report['observed_at']))
            cursor = self.db.conn.execute('INSERT INTO radar_cycles(run_id,recorded_at,report_json,report_sha256,'
                'previous_sha256,chain_sha256,episodes_sha256) VALUES(?,?,?,?,?,?,?)',
                (run_id, report['observed_at'], encode(sealed), report_hash, before, chain, digest(state['episodes'])))
            cycle_id = cursor.lastrowid
            for key, value in state['episodes'].items():
                self.db.conn.execute('INSERT INTO radar_episodes VALUES (?,?,?) ON CONFLICT(run_id,episode_key) '
                    'DO UPDATE SET payload_json=excluded.payload_json', (run_id, key, encode(value)))
            for notice in incoming:
                self.db.conn.execute('INSERT OR IGNORE INTO radar_notice_versions VALUES (?,?,?,?)',
                    (run_id, notice['notice_key'], report['observed_at'], encode(notice)))
            for event in events:
                self.db.conn.execute('INSERT INTO radar_attention_events(run_id,cycle_id,recorded_at,kind,payload_json) VALUES(?,?,?,?,?)',
                    (run_id, cycle_id, report['observed_at'], event['kind'], encode(event)))
            if before_commit is not None:
                before_commit()
            self.db.conn.commit()
            return dict(cycle_id=cycle_id, chain_sha256=chain, new_attention=sum(e['kind'] in
                ('new_attention', 'new_notice', 'new_scope_identity', 'attention_observed_after_gap') and not e.get('baseline', False) for e in events),
                events=events)
        except Exception:
            self.db.conn.rollback()
            raise

    def latest(self, run_id):
        with read_snapshot(self.db.conn):
            row = self.db.conn.execute('SELECT * FROM radar_cycles WHERE run_id=? ORDER BY id DESC LIMIT 1', (run_id,)).fetchone()
            if row is None:
                return None
            report = json.loads(row['report_json'])
            if digest(report) != row['report_sha256']:
                raise ValueError('Radar cycle content differs from its hash')
            episodes = self._episodes(run_id)
            if digest(episodes) != row['episodes_sha256']:
                raise ValueError('Radar episode cache differs from the committed chain')
            return dict(report=report, episodes=list(episodes.values()), cycle_id=row['id'])

    def events(self, run_id, *, limit=500):
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError('A bounded event view is required')
        rows = self.db.conn.execute('SELECT * FROM radar_attention_events WHERE run_id=? ORDER BY id DESC LIMIT ?', (run_id,limit))
        return [dict(id=r['id'], recorded_at=r['recorded_at'], **json.loads(r['payload_json'])) for r in rows]

    def verify(self, run_id):
        with read_snapshot(self.db.conn):
            run = self.db.conn.execute('SELECT * FROM radar_runs WHERE id=?', (run_id,)).fetchone()
            if run is None or digest(json.loads(run['protocol_json'])) != run['protocol_sha256']:
                raise ValueError('Radar protocol fingerprint differs')
            previous, count = run['protocol_sha256'], 0
            for row in self.db.conn.execute('SELECT * FROM radar_cycles WHERE run_id=? ORDER BY id', (run_id,)):
                report = json.loads(row['report_json'])
                expected = digest(dict(previous=previous, report=digest(report), run_id=run_id, recorded_at=row['recorded_at']))
                if row['previous_sha256'] != previous or row['report_sha256'] != digest(report) or row['chain_sha256'] != expected:
                    raise ValueError('Radar cycle chain differs')
                stored=[json.loads(event['payload_json']) for event in self.db.conn.execute(
                    'SELECT payload_json FROM radar_attention_events WHERE run_id=? AND cycle_id=? ORDER BY id',
                    (run_id,row['id']))]
                if stored!=report['events']:
                    raise ValueError('Radar event ledger differs from cycle')
                previous, count = expected, count+1
            self.latest(run_id)
            return dict(run_id=run_id, verified_cycles=count, chain_sha256=previous, externally_timestamped=False,
                        accepted_for_signals=False)
