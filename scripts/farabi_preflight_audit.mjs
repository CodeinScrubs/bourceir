/** Read-only analysis of already-redacted passive Farabi timing journals. */
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const number = value => typeof value === 'number' && Number.isFinite(value) ? value : null;
const validAlias = value => typeof value === 'string' && /^request-\d+$/.test(value);
const hosts = new Set(['gateway.farabixo.com', 'farabixo.irfarabi.com']);
const deltaMs = (end, start) => end !== null && start !== null && end >= start ? (end - start) * 1000 : null;

/** All click/request and OPTIONS/POST links remain time-window candidates. */
export function analyzeRecords(rows, manifest = {}) {
  const records = rows.map(row => row.record).filter(Boolean);
  const discontinuity = records.some(record => record.type === 'clock.discontinuity');
  const intents = records.filter(record => record.type === 'ui.intent').map(record => ({
    id: /^intent-\d+$/.test(record.data?.intent_id || '') ? record.data.intent_id : null,
    observer_wall_ms: number(record.device_wall_ms),
    trusted: typeof record.data?.trusted === 'boolean' ? record.data.trusted : null,
  }));
  const byHop = new Map();
  for (const record of records) {
    const data = record.data || {};
    if (!['network.request', 'network.response', 'network.finished', 'network.failed'].includes(record.type) ||
        !validAlias(data.alias) || !Number.isSafeInteger(data.hop) || data.hop < 0) continue;
    const key = `${data.alias}:${data.hop}`;
    const item = byHop.get(key) || { alias: data.alias, hop: data.hop };
    const stamp = record.clock === 'cdp_monotonic_seconds' ? number(record.source_time) : null;
    if (record.type === 'network.request') {
      item.host = hosts.has(data.host) ? data.host : manifest.synthetic === true && data.host === '127.0.0.1' ? data.host : null;
      item.path_class = data.path_class === 'orders' ? 'orders' : null;
      item.method = ['OPTIONS', 'POST'].includes(data.method) ? data.method : null;
      item.wall_seconds = number(data.wall_time_seconds);
      item.start = stamp;
    } else if (record.type === 'network.response') {
      item.status = number(data.http_status);
      item.response_at = stamp;
      item.timing = data.timing || {};
    } else {
      item.end = stamp;
      item.outcome = record.type === 'network.finished' ? 'finished' : 'failed';
    }
    byHop.set(key, item);
  }
  const requests = [...byHop.values()].filter(item => item.host && item.path_class && item.method).map(item => {
    const timing = item.timing || {};
    const responseAt = item.response_at ?? null;
    const base = responseAt !== null ? number(timing.requestTime) : null;
    const sendStart = number(timing.sendStart);
    const sendEnd = number(timing.sendEnd);
    const headersStart = number(timing.receiveHeadersStart);
    const candidateSendAt = base !== null && sendStart !== null && sendStart >= 0 ? base + sendStart / 1000 : null;
    const sendAt = candidateSendAt !== null && item.start !== null && item.start !== undefined &&
      responseAt !== null && candidateSendAt >= item.start && candidateSendAt <= responseAt ? candidateSendAt : null;
    return {
      alias: item.alias, hop: item.hop, host: item.host, method: item.method,
      wall_seconds: item.wall_seconds ?? null, start: item.start ?? null, end: item.end ?? null,
      send_at: sendAt, status: item.status ?? null, outcome: item.outcome ?? 'incomplete',
      duration_ms: deltaMs(item.end ?? null, item.start ?? null),
      send_start_delay_ms: deltaMs(sendAt, item.start ?? null),
      send_end_to_headers_ms: base !== null && responseAt !== null && sendEnd !== null && headersStart !== null &&
        sendEnd >= 0 && headersStart >= sendEnd && base + headersStart / 1000 <= responseAt
        ? headersStart - sendEnd : null,
    };
  });
  const posts = requests.filter(item => item.method === 'POST');
  const options = requests.filter(item => item.method === 'OPTIONS');
  const observations = intents.map(intent => {
    // Browser wall clocks are not independently synchronized to exchange time.
    const postCandidates = discontinuity || intent.observer_wall_ms === null ? [] : posts.filter(post =>
      post.wall_seconds !== null && post.wall_seconds * 1000 - intent.observer_wall_ms >= -1000 &&
      post.wall_seconds * 1000 - intent.observer_wall_ms <= 5000);
    const candidates = postCandidates.map(post => {
      const optionCandidates = options.filter(option => option.host === post.host && option.wall_seconds !== null &&
        post.wall_seconds !== null && Math.abs(option.wall_seconds - post.wall_seconds) <= 0.5).map(option => ({
        alias: option.alias, status: option.status, duration_ms: option.duration_ms,
        send_start_delay_ms: option.send_start_delay_ms, send_end_to_headers_ms: option.send_end_to_headers_ms,
        options_finish_to_post_send_ms: option.end !== null && post.send_at !== null
          ? (post.send_at - option.end) * 1000 : null,
      }));
      return { alias: post.alias, status: post.status, duration_ms: post.duration_ms,
        send_start_delay_ms: post.send_start_delay_ms, send_end_to_headers_ms: post.send_end_to_headers_ms,
        options_candidates: optionCandidates };
    });
    return { intent_id: intent.id, observer_wall_ms: intent.observer_wall_ms, trusted: intent.trusted,
      link: 'time_window_inference_not_established', association_ambiguous: true,
      one_candidate_set_in_observed_records: intents.length === 1 && candidates.length === 1 && candidates[0].options_candidates.length === 1,
      candidates };
  });
  return {
    capture: { health: ['SAVED', 'DEGRADED'].includes(manifest.health) ? manifest.health : 'UNKNOWN',
      gap_count: Array.isArray(manifest.gaps) ? manifest.gaps.length : null,
      missing_prefix: typeof manifest.missing_prefix === 'boolean' ? manifest.missing_prefix : null,
      clock_discontinuity_observed: discontinuity },
    candidate_window_ms: { post_relative_to_click_observer_min: -1000, post_relative_to_click_observer_max: 5000,
      options_relative_to_post_absolute_max: 500 },
    observations,
    limitations: ['Request and OPTIONS links are candidates, not proven causality.',
      'Candidate windows cover observed same-host and path-class records only; no match does not prove absence.',
      'Wall clocks are not independent exchange-clock measurements.',
      'HTTP status and response duration do not establish exchange acceptance or queue rank.'],
  };
}

/** Verify checksums and the original JS hash chain without changing the capture. */
export async function readVerifiedCapture(directory, allowedRoot) {
  const root = await fs.realpath(allowedRoot);
  const session = await fs.realpath(directory);
  const relative = path.relative(root, session);
  if (!relative || relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative))
    throw new Error('Capture must be inside the approved private root.');
  const checksumPath = await fs.realpath(path.join(session, 'SHA256SUMS.txt'));
  if (path.dirname(checksumPath) !== session) throw new Error('Capture checksum manifest escapes its directory.');
  const checksumText = await fs.readFile(checksumPath, 'utf8');
  const checksums = new Map();
  for (const line of checksumText.split(/\r?\n/).filter(Boolean)) {
    const match = /^([a-f0-9]{64})  ([A-Za-z0-9._-]+)$/.exec(line);
    if (!match || checksums.has(match[2])) throw new Error('Invalid capture checksum manifest.');
    checksums.set(match[2], match[1]);
  }
  for (const name of ['events.jsonl', 'manifest.json']) if (!checksums.has(name))
    throw new Error('Capture is missing required checksums.');
  const verifiedFiles = new Map();
  for (const [name, expected] of checksums) {
    const file = path.join(session, name);
    const actualPath = await fs.realpath(file);
    if (path.dirname(actualPath) !== session) throw new Error('Capture file escapes its directory.');
    const contents = await fs.readFile(actualPath);
    const digest = crypto.createHash('sha256').update(contents).digest('hex');
    if (digest !== expected) throw new Error('Capture checksum mismatch.');
    if (name === 'events.jsonl' || name === 'manifest.json') verifiedFiles.set(name, contents);
  }
  const manifest = JSON.parse(verifiedFiles.get('manifest.json').toString('utf8'));
  const text = verifiedFiles.get('events.jsonl').toString('utf8');
  const lines = text.trim() ? text.trim().split('\n') : [];
  let previous = '0'.repeat(64), sequence = 0;
  const rows = lines.map(line => {
    const row = JSON.parse(line), { sha256, ...content } = row;
    if (content.ingest_sequence !== ++sequence || content.previous_sha256 !== previous ||
        crypto.createHash('sha256').update(JSON.stringify(content)).digest('hex') !== sha256)
      throw new Error('Capture journal integrity failure.');
    previous = sha256;
    return row;
  });
  if (manifest.journal_chain_valid !== true || manifest.last_durable_ingest_sequence !== sequence ||
      manifest.final_journal_sha256 !== previous) throw new Error('Capture manifest does not match its journal.');
  return { manifest, rows, checksum_files_verified: checksums.size };
}

async function main(args) {
  if (!args.length) throw new Error('Supply one or more private capture directories.');
  const workspace = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const root = path.join(workspace, 'data', 'private', 'farabi-timing');
  const results = [];
  for (const directory of args) {
    const capture = await readVerifiedCapture(path.resolve(directory), root);
    results.push({ session: path.basename(directory), checksum_files_verified: capture.checksum_files_verified,
      journal_chain_verified: true, ...analyzeRecords(capture.rows, capture.manifest) });
  }
  process.stdout.write(`${JSON.stringify({ schema_version: 1, sessions: results }, null, 2)}\n`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main(process.argv.slice(2)).catch(error => { process.stderr.write(`${error.message}\n`); process.exitCode = 1; });
}
