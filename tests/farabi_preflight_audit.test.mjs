import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { analyzeRecords, readVerifiedCapture } from '../scripts/farabi_preflight_audit.mjs';

const row = record => ({ record });
const network = (type, alias, stamp, data = {}) => row({ type, clock: 'cdp_monotonic_seconds', source_time: stamp,
  data: { alias, hop: 0, host: 'gateway.farabixo.com', path_class: 'orders', ...data } });
const intent = (id = 'intent-1') => row({ type: 'ui.intent', device_wall_ms: 1000000,
  data: { intent_id: id, trusted: true } });

function fixture() {
  return [intent(),
    network('network.request', 'request-1', 10, { method: 'OPTIONS', wall_time_seconds: 1000.05 }),
    network('network.response', 'request-1', 11.5, { http_status: 204 }),
    network('network.finished', 'request-1', 11.5),
    network('network.request', 'request-2', 10, { method: 'POST', wall_time_seconds: 1000.05,
      headers: { Authorization: 'SECRET_AUTH' }, url: 'https://private.example/?token=SECRET_QUERY' }),
    network('network.response', 'request-2', 12.1, { http_status: 201,
      timing: { requestTime: 11.5, sendStart: 2, sendEnd: 3, receiveHeadersStart: 503 },
      headers: { 'set-cookie': 'SECRET_COOKIE' } }),
    network('network.finished', 'request-2', 12.12)];
}

test('reports timing candidates without claiming a causal or exchange link', () => {
  const result = analyzeRecords(fixture(), { health: 'SAVED', gaps: [], missing_prefix: true });
  const observation = result.observations[0], post = observation.candidates[0], option = post.options_candidates[0];
  assert.equal(result.capture.health, 'SAVED');
  assert.equal(result.capture.missing_prefix, true);
  assert.equal(observation.link, 'time_window_inference_not_established');
  assert.equal(observation.association_ambiguous, true);
  assert.equal(observation.one_candidate_set_in_observed_records, true);
  assert.ok(Math.abs(post.duration_ms - 2120) < 0.0001);
  assert.ok(Math.abs(post.send_start_delay_ms - 1502) < 0.0001);
  assert.equal(post.send_end_to_headers_ms, 500);
  assert.equal(option.duration_ms, 1500);
  assert.ok(Math.abs(option.options_finish_to_post_send_ms - 2) < 0.0001);
  assert.doesNotMatch(JSON.stringify(result), /SECRET_AUTH|SECRET_QUERY|SECRET_COOKIE|private\.example/);
});

test('multiple candidates, missing metadata and clock discontinuities remain explicit', () => {
  const rows = fixture();
  rows.push(network('network.request', 'request-3', 10, { method: 'OPTIONS', wall_time_seconds: 1000.06 }));
  const result = analyzeRecords(rows, { health: 'DEGRADED', gaps: [{ reason: 'gap' }] });
  assert.equal(result.capture.gap_count, 1);
  assert.equal(result.capture.missing_prefix, null);
  assert.equal(result.observations[0].one_candidate_set_in_observed_records, false);
  assert.equal(result.observations[0].association_ambiguous, true);
  assert.equal(result.observations[0].candidates[0].options_candidates.length, 2);
  rows.push(row({ type: 'clock.discontinuity' }));
  assert.deepEqual(analyzeRecords(rows).observations[0].candidates, []);
});

test('mixed clocks and invalid timing fields never manufacture durations', () => {
  const rows = fixture();
  rows.find(r => r.record.type === 'network.finished' && r.record.data.alias === 'request-2').record.clock = 'page_performance_ms';
  rows.find(r => r.record.type === 'network.response' && r.record.data.alias === 'request-2').record.data.timing.sendStart = -1;
  const post = analyzeRecords(rows).observations[0].candidates[0];
  assert.equal(post.duration_ms, null);
  assert.equal(post.send_start_delay_ms, null);
  assert.equal(post.options_candidates[0].options_finish_to_post_send_ms, null);
});

test('rejects a send timestamp that is later than its recorded response', () => {
  const rows = fixture();
  rows.find(r => r.record.type === 'network.response' && r.record.data.alias === 'request-2').record.data.timing.requestTime = 50;
  const post = analyzeRecords(rows).observations[0].candidates[0];
  assert.equal(post.send_start_delay_ms, null);
  assert.equal(post.send_end_to_headers_ms, null);
});

async function makeCapture(root) {
  const directory = path.join(root, 'capture');
  await fs.mkdir(directory);
  let previous = '0'.repeat(64), sequence = 0;
  const lines = fixture().map(({ record }) => {
    const content = { ingest_sequence: ++sequence, previous_sha256: previous, record };
    const sha256 = crypto.createHash('sha256').update(JSON.stringify(content)).digest('hex');
    previous = sha256;
    return JSON.stringify({ ...content, sha256 });
  });
  const manifest = { synthetic: true, health: 'SAVED', gaps: [], missing_prefix: true,
    journal_chain_valid: true, last_durable_ingest_sequence: sequence, final_journal_sha256: previous };
  await fs.writeFile(path.join(directory, 'events.jsonl'), `${lines.join('\n')}\n`);
  await fs.writeFile(path.join(directory, 'manifest.json'), JSON.stringify(manifest));
  const entries = [];
  for (const name of ['events.jsonl', 'manifest.json']) {
    const hash = crypto.createHash('sha256').update(await fs.readFile(path.join(directory, name))).digest('hex');
    entries.push(`${hash}  ${name}`);
  }
  await fs.writeFile(path.join(directory, 'SHA256SUMS.txt'), `${entries.join('\n')}\n`);
  return directory;
}

test('validates file checksums, journal chain and private-root containment', async () => {
  const temp = await fs.mkdtemp(path.join(os.tmpdir(), 'brs-preflight-audit-'));
  try {
    const capture = await makeCapture(temp);
    const verified = await readVerifiedCapture(capture, temp);
    assert.equal(verified.rows.length, fixture().length);
    assert.equal(verified.checksum_files_verified, 2);
    await assert.rejects(readVerifiedCapture(temp, temp), /inside the approved private root/);
    await fs.appendFile(path.join(capture, 'events.jsonl'), 'tampered');
    await assert.rejects(readVerifiedCapture(capture, temp), /checksum mismatch/);
  } finally {
    assert.ok(path.resolve(temp).startsWith(path.resolve(os.tmpdir()) + path.sep));
    assert.ok(path.basename(temp).startsWith('brs-preflight-audit-'));
    await fs.rm(temp, { recursive: true, force: true });
  }
});

test('rejects a broken journal even if its file checksum is recomputed', async () => {
  const temp = await fs.mkdtemp(path.join(os.tmpdir(), 'brs-preflight-audit-'));
  try {
    const capture = await makeCapture(temp), events = path.join(capture, 'events.jsonl');
    const raw = await fs.readFile(events, 'utf8');
    await fs.writeFile(events, raw.replace('intent-1', 'intent-9'));
    const checksumFile = path.join(capture, 'SHA256SUMS.txt');
    const oldSums = await fs.readFile(checksumFile, 'utf8');
    const newHash = crypto.createHash('sha256').update(await fs.readFile(events)).digest('hex');
    await fs.writeFile(checksumFile, oldSums.replace(/^[a-f0-9]{64}(?=  events\.jsonl)/m, newHash));
    await assert.rejects(readVerifiedCapture(capture, temp), /journal integrity failure/);
  } finally {
    assert.ok(path.resolve(temp).startsWith(path.resolve(os.tmpdir()) + path.sep));
    assert.ok(path.basename(temp).startsWith('brs-preflight-audit-'));
    await fs.rm(temp, { recursive: true, force: true });
  }
});
