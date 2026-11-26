#!/usr/bin/env python3
"""Builds github-release-tracker.json for n8n 2.x.

Single workflow, single 2-minute schedule trigger:
  1. polls Telegram getUpdates (long polling is unnecessary at this cadence)
  2. handles Approve / Re-draft / Skip callbacks against in-workflow state
  3. every GITHUB_POLL_MINUTES, polls GitHub releases + merged PRs
  4. drafts an X post (OpenAI with template fallback) and sends it to Telegram
     with inline approval buttons

Merged PRs are collected into a queue and surfaced on a digest cadence
(DIGEST_HOURS) as "features shipped", never as pull requests.
"""
import json
import pathlib

OUT = pathlib.Path(__file__).parent / "workflows" / "github-release-tracker.json"

# ---------------------------------------------------------------- code blocks

PLAN = r"""
const st = $getWorkflowStaticData('global');
if (st.v !== 1) {
  st.v = 1;
  st.initialized = false;
  st.lastPollAt = 0;
  st.tgOffset = 0;
  st.seen = {};
  st.offered = {};
  st.prQueue = {};
  st.lastDigestAt = {};
  st.pending = {};
}
const gh = String($env.GITHUB_TOKEN || '');
const configured = gh.length > 20 && !gh.includes('PASTE');
const now = Date.now();
const minutes = Math.max(5, Number($env.GITHUB_POLL_MINUTES || 15));
const due = !st.lastPollAt || (now - st.lastPollAt) >= minutes * 60000;
if (configured && due) st.lastPollAt = now;
return [{ json: { configured, githubDue: configured && due, tgOffset: st.tgOffset || 0, now } }];
"""

QUEUE = r"""
const st = $getWorkflowStaticData('global');
const xEnabled = String($env.X_ENABLED || '').toLowerCase() === 'true';
const now = Date.now();
const queued = st.dshActions || {};
const out = [];
st.postQueue = Array.isArray(st.postQueue) ? st.postQueue : [];
for (const key of Object.keys(queued)) {
  const act = queued[key];
  const p = st.pending ? st.pending[key] : null;
  let action = act === 'ok' ? (xEnabled ? 'approve' : 'xdisabled') : act === 're' ? 'redraft' : 'skip';
  if (!p || p.status !== 'pending') action = 'expired';
  const item = { action, key, source: 'dsh' };
  if (p) {
    item.repo = p.repo;
    item.kind = p.kind;
    item.title = p.title;
    item.url = p.url;
    item.body = p.body;
    item.draft = p.draft;
    item.tweetText = p.tweetText;
    item.attempts = p.attempts || 0;
  }
  // Approvals wait in a paced queue instead of publishing on the next cycle.
  if (action === 'approve') {
    if (!st.postQueue.some((q) => q.key === key)) st.postQueue.push({ key, at: now });
    continue;
  }
  out.push(item);
}
st.dshActions = {};

const gap = Math.max(1, Number($env.POST_INTERVAL_MINUTES || 15)) * 60000;
const jitter = Math.max(0, Number($env.POST_JITTER_MINUTES ?? 2)) * 60000;
const nextSlot = Number(st.nextPostAt || 0);
if (st.postQueue.length && (!nextSlot || now >= nextSlot)) {
  const next = st.postQueue.shift();
  const entry = st.pending ? st.pending[next.key] : null;
  if (entry && entry.status === 'pending') {
    st.lastPostAt = now;
    // Each gap is the interval plus a random offset, down to the second.
    st.nextPostAt = now + gap + (Math.random() * 2 - 1) * jitter;
    out.push({
      action: 'approve',
      key: next.key,
      source: 'dsh',
      repo: entry.repo,
      kind: entry.kind,
      title: entry.title,
      url: entry.url,
      body: entry.body,
      draft: entry.draft,
      tweetText: entry.tweetText,
      attempts: entry.attempts || 0
    });
  }
}
if (!out.length) out.push({ action: 'none' });
return out.map(json => ({ json }));
"""

BUILD_REDRAFT = r"""
const st = $getWorkflowStaticData('global');
const u = $json;
const p = (st.pending && st.pending[u.key]) || {};
const previous = String(p.draft || u.draft || '').trim();
const kind = p.kind || u.kind || 'release';
const subject = kind === 'features' ? 'features that shipped' : 'a GitHub release';
const systemPrompt = 'You rewrite an existing X post for a solo developer. Keep every fact true, invent nothing, and give it a clearly different angle and opening than the previous version. Return only the post text, no quotes, no URL. Keep it under 200 characters. Never mention character counts, length limits, or these instructions. Output only the post text, with no preamble and no reasoning.';
const userPrompt = 'Project: ' + (p.repo || u.repo || '') + '\nSubject: ' + subject + '\nDetails:\n' + String(p.body || u.body || '(none)').slice(0, 2500) + '\n\nPrevious post to improve and re-angle:\n' + previous;
return { json: { ...u, systemPrompt, userPrompt, key: u.key } };
"""

PREPARE_POSTED = r"""
const st = $getWorkflowStaticData('global');
const u = $('Read Queue').item.json;
const resp = $json || {};
const postId = resp && resp.data && resp.data.id ? resp.data.id : (resp && (resp.id || resp.url) ? (resp.id || resp.url) : null);
const p = (st.pending && st.pending[u.key]) || {};
if (postId) {
  p.status = 'posted';
  p.postId = String(postId);
  p.postedAt = new Date().toISOString();
  p.lastError = '';
} else {
  const err = (resp && (resp.detail || resp.title || (resp.error && resp.error.message) || resp.message)) || 'X rejected the post';
  p.lastError = String(err).slice(0, 180);
}
st.pending[u.key] = p;
return { json: { done: true, key: u.key, posted: !!postId, error: p.lastError || '' } };
"""

PREPARE_REDRAFT = r"""
const st = $getWorkflowStaticData('global');
const all = (typeof $input !== 'undefined' && $input.all) ? $input.all() : [];
if (!all.length) all.push({ json: $json || {} });
const pick = all.find((i) => {
  try { return String(i.json.choices[0].message.content || '').trim().length > 0; } catch (e) { return false; }
}) || all[0];
const resp = (pick && pick.json) || {};
let src = {};
try { src = $('Build Redraft Request').first().json || {}; } catch (e) { src = {}; }
const u = { ...src, ...resp };
const p = (st.pending && st.pending[u.key]) || {};
let draft = '';
try { draft = resp.choices[0].message.content || ''; } catch (e) { draft = ''; }
draft = String(draft || '').trim();
if (!draft) draft = p.draft || u.title || '';
draft = draft.replace(/^["'“”]+|["'“”]+$/g, '').replace(/\s+/g, ' ').trim();
draft = draft.replace(/^\d{2,4}\s*characters?\b[^.!?]{0,20}[.!:]?\s*/i, '');
draft = draft.replace(/^(?:sure|okay|alright|let'?s|here'?s|here is)\b[^.!?]{0,80}[.!:]\s*/i, '');
draft = draft.replace(/^["'“”]+|["'“”]+$/g, '').trim();
draft = draft
  .replace(/^\s*[-*•]\s+/gm, '')
  .replace(/[\r\n]+/g, ' ')
  .replace(/\s*[—–]\s*/g, ', ')
  .replace(/(^|\s)#[A-Za-z0-9_]+/g, '')
  .replace(/\*\*|__/g, '')
  .replace(/,\s*,/g, ',')
  .replace(/\s+([,.;:!?])/g, '$1')
  .replace(/\s{2,}/g, ' ')
  .trim();
const max = 253;
if (draft.length > max) {
  const window = draft.slice(0, max);
  const stop = Math.max(window.lastIndexOf('. '), window.lastIndexOf('! '), window.lastIndexOf('? '));
  draft = stop > 80 ? window.slice(0, stop + 1) : window.replace(/[\s,;:]+\S*$/, '').trimEnd();
}
p.draft = draft;
p.tweetText = draft + '\n\n' + String(p.url || u.url || '');
p.attempts = (p.attempts || 0) + 1;
p.lastError = '';
st.pending[u.key] = p;
return { json: { done: true, key: u.key, attempts: p.attempts } };
"""

PREPARE_SKIP = r"""
const st = $getWorkflowStaticData('global');
const u = $json;
const p = (st.pending && st.pending[u.key]) || null;
if (p && u.action === 'skip') {
  p.status = 'skipped';
  p.skippedAt = new Date().toISOString();
  st.pending[u.key] = p;
}
return { json: { done: true, key: u.key, action: u.action } };
"""

DSH_API = r"""
const st = $getWorkflowStaticData('global');
const req = $json || {};
const headers = req.headers || {};
const body = req.body || {};
const query = req.query || {};
const token = String($env.DSH_WEBHOOK_TOKEN || '');
const given = String(headers['x-totem-token'] || headers['X-Totem-Token'] || body.token || query.token || '');
const reply = (statusCode, payload) => [{ json: { statusCode, body: payload } }];

if (!token || given !== token) return reply(401, { error: 'unauthorized' });

st.pending = st.pending || {};
const op = String(body.op || query.op || 'list');

if (op === 'list') {
  const queueKeys = new Set((st.postQueue || []).map((q) => q.key));
  const pending = Object.values(st.pending)
    .filter((p) => p && p.status === 'pending')
    .sort((a, b) => String(a.createdAt).localeCompare(String(b.createdAt)))
    .map((p) => ({
      key: p.key,
      kind: p.kind,
      repo: p.repo,
      title: p.title,
      draft: p.draft,
      tweetText: p.tweetText,
      url: p.url,
      createdAt: p.createdAt,
      attempts: p.attempts || 0,
      queued: queueKeys.has(p.key),
      lastError: p.lastError || ''
    }));
  return reply(200, { ok: true, count: pending.length, pending });
}

if (op === 'enqueue') {
  const note = String(body.note || '').trim();
  const url = String(body.url || '').trim();
  if (note.length < 12) return reply(400, { error: 'note is too short' });
  if (note.length > 2000) return reply(400, { error: 'note is too long' });
  const okUrl = url === '' || url.startsWith('http://') || url.startsWith('https://');
  if (!okUrl) return reply(400, { error: 'url must start with http' });
  return [{ json: {
    needsDraft: true,
    statusCode: 201,
    note,
    url,
    systemPrompt: 'You write the X post for a developer shipping open source. The developer gives you a short note about something they made. Rules: one or two sentences, under 220 characters, prose only. No em dashes or en dashes. No bullet points, lists, numbering, bold, or hashtags. Do not open with a question, and never use excited, thrilled, proud, announce, or seamlessly. Never write not just X but Y. No closing line that restates the point. Never add facts, numbers, or claims that are not in the note. Sound like the person who wrote the code. Return only the post text, no quotes, no URL.',
    userPrompt: 'What shipped:\n' + note
  } }];
}

if (op === 'act') {
  const key = String(body.key || '');
  const action = String(body.action || '');
  if (!['ok', 're', 'no'].includes(action)) return reply(400, { error: 'action must be ok, re or no' });
  const p = st.pending[key];
  if (!p) return reply(404, { error: 'unknown key' });
  if (p.status !== 'pending') return reply(409, { error: 'already ' + p.status });
  st.dshActions = st.dshActions || {};
  st.dshActions[key] = action;
  return reply(202, { ok: true, queued: true, key, action });
}

return reply(400, { error: 'unknown op' });
"""

STORE_MANUAL = r"""
const st = $getWorkflowStaticData('global');
const api = $('DSH Approval API').item.json;
const resp = $json || {};
let draft = '';
try { draft = resp.choices[0].message.content || ''; } catch (e) { draft = ''; }
draft = String(draft || '').trim();
if (!draft) draft = String(api.note || '').split(/(?<=[.!?])\s/)[0] || String(api.note || '');
draft = draft.replace(/^["'“”]+|["'“”]+$/g, '').replace(/\s+/g, ' ').trim();
draft = draft.replace(/^\d{2,4}\s*characters?\b[^.!?]{0,20}[.!:]?\s*/i, '');
draft = draft.replace(/^(?:sure|okay|alright|let'?s|here'?s|here is)\b[^.!?]{0,80}[.!:]\s*/i, '');
draft = draft.replace(/^["'“”]+|["'“”]+$/g, '').trim();
draft = draft
  .replace(/^\s*[-*•]\s+/gm, '')
  .replace(/[\r\n]+/g, ' ')
  .replace(/\s*[—–]\s*/g, ', ')
  .replace(/(^|\s)#[A-Za-z0-9_]+/g, '')
  .replace(/\*\*|__/g, '')
  .replace(/,\s*,/g, ',')
  .replace(/\s+([,.;:!?])/g, '$1')
  .replace(/\s{2,}/g, ' ')
  .trim();
const max = 253;
if (draft.length > max) draft = draft.slice(0, max - 1).replace(/\s+\S*$/, '').trimEnd() + '…';

function hash(s) {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  return (h >>> 0).toString(36);
}

const key = 'm' + hash(String(api.note) + '|' + Date.now());
const url = String(api.url || '');
st.pending = st.pending || {};
st.pending[key] = {
  key,
  kind: 'manual',
  repo: 'manual',
  title: 'Manual post',
  url,
  body: String(api.note || ''),
  draft,
  tweetText: url ? draft + '\n\n' + url : draft,
  status: 'pending',
  attempts: 0,
  createdAt: new Date().toISOString(),
  sk: 'manual#' + key,
  featureNumbers: []
};
return [{ json: { statusCode: 201, body: { ok: true, key, draft, usedModel: !!resp.choices } } }];
"""

TRACKED = r"""
const raw = String($env.TRACKED_REPOS || 'emiliano-go/trustsight,emiliano-go/trustsight-feed,emiliano-go/trustsight-harness,emiliano-go/trustsight-web,emiliano-go/trustsight-triage-agent,emiliano-go/trustsight-lab,emiliano-go/rypipe,dbwarden-org/dbwarden');
const seen = new Set();
const items = [];
for (const part of raw.split(',')) {
  const full = part.trim();
  if (!full || !full.includes('/')) continue;
  if (seen.has(full)) continue;
  seen.add(full);
  const bits = full.split('/');
  items.push({ json: { owner: bits[0], repo: bits[1], full } });
}
return items;
"""

SELECT = r"""
const st = $getWorkflowStaticData('global');
const now = Date.now();
const digestMs = Math.max(1, Number($env.DIGEST_HOURS || 2)) * 3600000;

st.seen = st.seen || {};
st.offered = st.offered || {};
st.prQueue = st.prQueue || {};
st.lastDigestAt = st.lastDigestAt || {};

const items = $input.all().map(i => i.json);
const byRepo = new Map();
for (const it of items) {
  if (!it || !it.repo) continue;
  if (!byRepo.has(it.repo)) byRepo.set(it.repo, { releases: [], pulls: [], okRelease: false, okPull: false });
  const b = byRepo.get(it.repo);
  if (it.kind === 'release') {
    if (it.ok === true) b.okRelease = true;
    if (Array.isArray(it.payload)) {
      for (const x of it.payload) if (x && (x.id || x.tag_name)) b.releases.push(x);
    }
  } else if (it.kind === 'pull') {
    if (it.ok === true) b.okPull = true;
    if (Array.isArray(it.payload)) {
      for (const x of it.payload) if (x && x.number) b.pulls.push(x);
    }
  }
}
if (!byRepo.size) return [];

const baseline = !st.initialized;
const complete = byRepo.size > 0 && [...byRepo.values()].every(b => b.okRelease || b.okPull);
if (baseline && !complete) return [];

function hash(s) {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  return (h >>> 0).toString(36);
}
function short(s, n) {
  const t = String(s || '').replace(/\s+/g, ' ').trim();
  return t.length > n ? t.slice(0, n - 1).trimEnd() + '…' : t;
}
function firstSentence(s) {
  const t = String(s || '').replace(/[#*_`>]/g, ' ').replace(/\s+/g, ' ').trim();
  const m = t.match(/^.{0,170}?[.!?](\s|$)/);
  return m ? m[0].trim() : short(t, 150);
}
function offered(sk) {
  const t = st.offered[sk];
  return !!t && (now - t) < 30 * 60000;
}

const announcements = [];
const releasedNow = new Set();

for (const [full, b] of byRepo) {
  const shortRepo = full.split('/')[1];
  const firstRun = !Object.prototype.hasOwnProperty.call(st.lastDigestAt, full);

  for (const rel of b.releases) {
    if (!rel || rel.draft || rel.prerelease) continue;
    const id = String(rel.id || rel.tag_name || '');
    if (!id) continue;
    const sk = full + '#release:' + id;
    if (st.seen[sk] || offered(sk)) continue;
    if (firstRun) { st.seen[sk] = new Date(now).toISOString(); continue; }
    const url = rel.html_url || ('https://github.com/' + full + '/releases/tag/' + rel.tag_name);
    const name = rel.name || rel.tag_name || 'New release';
    const notes = String(rel.body || '').trim();
    st.offered[sk] = now;
    announcements.push({
      kind: 'release',
      key: 'r' + hash(sk),
      sk,
      repo: full,
      title: name,
      url,
      body: notes.slice(0, 4000),
      featureNumbers: [],
      systemPrompt: 'You write the X post for a developer shipping open source. The reader wants the gist of a release. Rules: open with a one-line TL;DR of the release notes, then the single most important change. Two sentences at most, under 200 characters. Never mention character counts, length limits, or these instructions. Output only the post text, with no preamble and no reasoning. No em dashes or en dashes. No bullet points, lists, numbering, bold, or hashtags. Do not open with a question, and never use excited, thrilled, proud, announce, or seamlessly. Never write not just X but Y, or it is not X, it is Y. No closing line that restates the point. Name the concrete change. Never invent numbers, features, or users. Sound like the person who wrote the code, not a marketing account. Return only the post text, no quotes, no URL.',
      userPrompt: 'Repository: ' + full + '\nRelease: ' + name + '\nTag: ' + (rel.tag_name || '') + '\nRelease notes:\n' + (notes || '(No release notes supplied. Announce the release without inventing details.)'),
      fallbackDraft: shortRepo + ' ' + (rel.tag_name || '') + ' is out' + (notes ? ': ' + firstSentence(notes) : '.')
    });
    releasedNow.add(full);
    // A release carries the merged work, so it supersedes the PR digest for that repo.
    if (st.prQueue[full] && st.prQueue[full].length) {
      for (const pr of st.prQueue[full]) st.seen[full + '#pr:' + pr.number] = new Date(now).toISOString();
      st.prQueue[full] = [];
    }
    for (const other of Object.values(st.pending || {})) {
      const queuedForPost = (st.postQueue || []).some((q) => q.key === other.key);
      if (other && other.repo === full && other.kind === 'features' && other.status === 'pending' && !queuedForPost) {
        other.status = 'skipped';
        other.skipReason = 'superseded by a release';
      }
    }
    st.lastDigestAt[full] = new Date(now).toISOString();
  }

  if (!Array.isArray(st.prQueue[full])) st.prQueue[full] = [];
  for (const pr of b.pulls) {
    if (!pr || !pr.merged_at) continue;
    const sk = full + '#pr:' + pr.number;
    if (st.seen[sk]) continue;
    st.seen[sk] = new Date(now).toISOString();
    if (firstRun || releasedNow.has(full)) continue;
    st.prQueue[full].push({
      number: pr.number,
      title: String(pr.title || ''),
      url: pr.html_url,
      mergedAt: pr.merged_at,
      body: String(pr.body || '').slice(0, 600)
    });
  }

  if (firstRun) { st.lastDigestAt[full] = new Date(now).toISOString(); continue; }

  const q = st.prQueue[full];
  const last = Date.parse(st.lastDigestAt[full] || 0) || 0;
  const newest = q.reduce((m, x) => Math.max(m, Date.parse(x.mergedAt) || 0), 0);
  const quietMs = Math.max(0, Number($env.DIGEST_QUIET_MINUTES || 30)) * 60000;
  const settled = (now - newest) >= quietMs;
  const dueByTime = now - last >= digestMs;
  const starved = now - last >= digestMs * 2;
  if (q.length && (starved || (dueByTime && settled) || q.length >= 8)) {
    const nums = q.map(x => x.number);
    const sk = full + '#features:' + nums[0] + '-' + nums[nums.length - 1];
    if (st.seen[sk] || offered(sk)) continue;
    st.offered[sk] = now;
    const bullets = q.map((x, i) => (i + 1) + '. ' + x.title + (x.body ? ' — ' + short(x.body, 110) : '')).join('\n');
    const liveDigest = Object.values(st.pending || {}).some((p) => p && p.repo === full && p.kind === 'features' && p.status === 'pending');
    if (liveDigest) continue;
    announcements.push({
      kind: 'features',
      key: 'f' + hash(sk),
      sk,
      repo: full,
      title: 'New in ' + shortRepo,
      url: 'https://github.com/' + full,
      body: bullets,
      featureNumbers: nums.slice(),
      systemPrompt: 'You write the X post for a developer shipping open source, from a list of merged changes. Describe what shipped as capabilities. Rules: one or two sentences, under 230 characters, prose only. Mention every numbered change in the list with at least a few words each; compress related ones into one clause and never drop an item. No em dashes or en dashes. No bullet points, lists, numbering, bold, or hashtags. Never mention pull requests, merges, commits, branches, or process. Do not open with a question, and never use excited, thrilled, proud, announce, or seamlessly. Never write not just X but Y. No closing line that restates the point. Never invent details. Sound like the person who wrote the code. Return only the post text, no quotes, no URL.',
      userPrompt: 'Project: ' + full + '\nShipped:\n' + bullets,
      fallbackDraft: 'New in ' + shortRepo + ': ' + q.slice(0, 4).map(x => short(x.title, 55)).join(' · ')
    });
  }
}

if (baseline) st.initialized = true;

const seenKeys = Object.keys(st.seen);
if (seenKeys.length > 4000) {
  const sorted = seenKeys.sort((a, b) => (st.seen[a] < st.seen[b] ? 1 : -1));
  const keep = {};
  for (const k of sorted.slice(0, 3000)) keep[k] = st.seen[k];
  st.seen = keep;
}
const offeredKeys = Object.keys(st.offered);
if (offeredKeys.length > 500) {
  const keep = {};
  for (const k of offeredKeys) if (offered(k)) keep[k] = st.offered[k];
  st.offered = keep;
}
return announcements.map(json => ({ json }));
"""

TAG_RELEASE = r"""
const repo = $('Tracked Repos').item.json.full;
const arr = Array.isArray($json) ? $json : [$json];
const bad = !Array.isArray($json) && $json && typeof $json === 'object' && ($json.message || $json.documentation_url);
return { json: { kind: 'release', repo, ok: !bad, payload: arr } };
"""

TAG_PULL = r"""
const repo = $('Tracked Repos').item.json.full;
const arr = Array.isArray($json) ? $json : [$json];
const bad = !Array.isArray($json) && $json && typeof $json === 'object' && ($json.message || $json.documentation_url);
return { json: { kind: 'pull', repo, ok: !bad, payload: arr } };
"""

MODEL_FANOUT = r"""
const primary = String($env.OPENAI_MODEL || 'gemini-3.5-flash');
const list = [primary].concat(
  String($env.OPENAI_FALLBACK_MODELS || '')
    .split(',').map((m) => m.trim()).filter((m) => m.length > 0),
);
const route = (spec) => {
  if (spec.startsWith('openrouter/')) {
    const base = String($env.OPENROUTER_BASE_URL || 'https://openrouter.ai/api/v1').replace(/\/+$/, '');
    return { model: spec.slice('openrouter/'.length), endpoint: base + '/chat/completions', apiKey: String($env.OPENROUTER_API_KEY || ''), extra: { reasoning: { effort: 'low' } } };
  }
  const base = String($env.OPENAI_BASE_URL || 'https://api.openai.com/v1').replace(/\/+$/, '');
  return { model: spec, endpoint: base + '/chat/completions', apiKey: String($env.OPENAI_API_KEY || ''), extra: {} };
};
const seen = new Set();
const targets = [];
for (const spec of list) {
  if (seen.has(spec)) continue;
  seen.add(spec);
  targets.push(route(spec));
}
const out = [];
for (const item of $input.all()) {
  const j = item.json;
  for (const t of targets) out.push({ json: { ...j, ...t, __a: j, __models: targets.length } });
}
return out;
"""

PREPARE_DRAFT = r"""
const all = (typeof $input !== 'undefined' && $input.all) ? $input.all() : [];
if (!all.length) all.push({ json: $json || {} });
const pick = all.find((i) => {
  try { return String(i.json.choices[0].message.content || '').trim().length > 0; } catch (e) { return false; }
}) || all[0];
const resp = (pick && pick.json) || {};
const a = resp.__a || $('Select Announcements').first().json;
let draft = '';
try { draft = resp.choices[0].message.content || ''; } catch (e) { draft = ''; }
const aiUsed = String(draft || '').trim().length > 0;
draft = String(draft || '').trim();
if (!draft) draft = a.fallbackDraft || a.title || '';
draft = draft.replace(/^["'“”]+|["'“”]+$/g, '').replace(/\s+/g, ' ').trim();
draft = draft.replace(/^\d{2,4}\s*characters?\b[^.!?]{0,20}[.!:]?\s*/i, '');
draft = draft.replace(/^(?:sure|okay|alright|let'?s|here'?s|here is)\b[^.!?]{0,80}[.!:]\s*/i, '');
draft = draft.replace(/^["'“”]+|["'“”]+$/g, '').trim();
// Humanizer pass: flatten a list the model slipped through, drop em/en dashes
// and hashtags, then tidy the punctuation that replacement can leave behind.
draft = draft
  .replace(/^\s*[-*•]\s+/gm, '')
  .replace(/[\r\n]+/g, ' ')
  .replace(/\s*[—–]\s*/g, ', ')
  .replace(/(^|\s)#[A-Za-z0-9_]+/g, '')
  .replace(/\*\*|__/g, '')
  .replace(/,\s*,/g, ',')
  .replace(/\s+([,.;:!?])/g, '$1')
  .replace(/\s{2,}/g, ' ')
  .trim();
const max = 253;
if (draft.length > max) draft = draft.slice(0, max - 1).replace(/\s+\S*$/, '').trimEnd() + '…';
const tweetText = draft + '\n\n' + a.url;
const label = a.kind === 'features' ? '✨ Features' : '🚀 Release';
return { json: {
  ...a,
  draft,
  tweetText,
  tgText: label + ' — ' + a.repo + '\n\n' + tweetText + '\n\nApprove to publish this on X.' +
    (aiUsed ? '' : '\n\n⚠️ Template draft — the model call failed. Check OPENAI_API_KEY and OPENAI_MODEL.'),
  tgButtons: [[
    { text: '✅ Post to X', callback_data: 'ok:' + a.key },
    { text: '🔁 Re-draft', callback_data: 're:' + a.key },
    { text: '🗑 Skip', callback_data: 'no:' + a.key }
  ]]
} };
"""

RECORD = r"""
const st = $getWorkflowStaticData('global');
const a = $('Prepare Draft').item.json;
st.pending = st.pending || {};
st.pending[a.key] = {
  key: a.key,
  kind: a.kind,
  repo: a.repo,
  title: a.title,
  url: a.url,
  body: a.body,
  draft: a.draft,
  tweetText: a.tweetText,
  status: 'pending',
  attempts: 0,
  createdAt: new Date().toISOString(),
  sk: a.sk,
  featureNumbers: a.featureNumbers || []
};
st.seen[a.sk] = new Date().toISOString();
delete st.offered[a.sk];
if (a.kind === 'features' && a.featureNumbers && a.featureNumbers.length) {
  const done = new Set(a.featureNumbers);
  st.prQueue[a.repo] = (st.prQueue[a.repo] || []).filter(x => !done.has(x.number));
  st.lastDigestAt[a.repo] = new Date().toISOString();
}
return { json: { queued: true, key: a.key } };
"""

FINALIZE = r"""
const due = $('Plan Cycle').first().json.githubDue;
return [{ json: { done: true, githubDue: due, at: new Date().toISOString() } }];
"""

# --------------------------------------------------------------------- helpers

_counter = [0]


def nid():
    _counter[0] += 1
    return "n%02d" % _counter[0]


def node(name, ntype, version, params, pos, *, on_error=None, credentials=None, retry=None):
    n = {
        "parameters": params,
        "id": nid(),
        "name": name,
        "type": ntype,
        "typeVersion": version,
        "position": pos,
    }
    if on_error:
        n["onError"] = on_error
    if retry:
        n["retryOnFail"] = True
        n["maxTries"] = retry[0]
        n["waitBetweenTries"] = retry[1]
    if credentials:
        n["credentials"] = credentials
    return n


def code(name, js, pos, *, each=False, on_error=None):
    return node(
        name,
        "n8n-nodes-base.code",
        2,
        {"mode": "runOnceForEachItem" if each else "runOnceForAllItems", "jsCode": js},
        pos,
        on_error=on_error,
    )


def http(name, url, pos, *, method="GET", headers=None, body=None, auth=None,
         on_error=None, timeout=60000, retry=None):
    p = {"method": method, "url": url, "options": {"timeout": timeout}}
    if headers:
        p["sendHeaders"] = True
        p["headerParameters"] = {"parameters": [{"name": k, "value": v} for k, v in headers]}
    if body is not None:
        p["sendBody"] = True
        p["contentType"] = "json"
        p["specifyBody"] = "json"
        p["jsonBody"] = body
    if auth == "oauth1":
        p["authentication"] = "genericCredentialType"
        p["genericAuthType"] = "oAuth1Api"
    else:
        p["authentication"] = "none"
    return node(name, "n8n-nodes-base.httpRequest", 4.2, p, pos, on_error=on_error, retry=retry)


def if_node(name, left, op, right, pos, op_type="boolean"):
    return node(
        name,
        "n8n-nodes-base.if",
        2.2,
        {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose"},
                "conditions": [{
                    "id": nid(),
                    "leftValue": left,
                    "rightValue": right,
                    "operator": {"type": op_type, "operation": op},
                }],
                "combinator": "and",
            },
            "options": {},
        },
        pos,
    )


def switch_rules(name, field, cases, pos):
    rules = []
    for value in cases:
        rules.append({
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
                "conditions": [{
                    "id": nid(),
                    "leftValue": "={{ " + field + " }}",
                    "rightValue": value,
                    "operator": {"type": "string", "operation": "equals"},
                }],
                "combinator": "and",
            },
        })
    return node(
        name,
        "n8n-nodes-base.switch",
        3.2,
        {"rules": {"values": rules}, "options": {}},
        pos,
    )


GH_HEADERS = [
    ("Accept", "application/vnd.github+json"),
    ("Authorization", "=Bearer {{ $env.GITHUB_TOKEN }}"),
    ("X-GitHub-Api-Version", "2022-11-28"),
    ("User-Agent", "n8n-github-tracker"),
]

TG = "=https://api.telegram.org/bot{{ $env.TELEGRAM_BOT_TOKEN }}/"
OPENAI_URL = "={{ $json.endpoint || ((($env.OPENAI_BASE_URL || 'https://api.openai.com/v1').replace(/\\/+$/, '')) + '/chat/completions') }}"
OPENAI_BODY = (
    "={{ JSON.stringify(Object.assign({ model: $json.model || $env.OPENAI_MODEL || 'gpt-4o-mini', temperature: 0.7, max_tokens: 3000, "
    "messages: [{ role: 'system', content: $json.systemPrompt }, "
    "{ role: 'user', content: $json.userPrompt }] }, $json.extra || {})) }}"
)
OPENAI_HEADERS = [
    ("Authorization", "=Bearer {{ $json.apiKey || $env.OPENAI_API_KEY }}"),
    ("Content-Type", "application/json"),
]

SIGN_LIB = (pathlib.Path(__file__).with_name("x_sign.js").read_text().split("module.exports")[0]).strip()

SIGN_X = SIGN_LIB + """

const j = $json;
const nonce = 'n' + Math.floor(Math.random() * 1e16).toString(36);
const timestamp = Math.floor(Date.now() / 1000);
const signed = signRequest({
  method: 'POST',
  url: 'https://api.x.com/2/tweets',
  consumerKey: String($env.X_CONSUMER_KEY || ''),
  consumerSecret: String($env.X_CONSUMER_SECRET || ''),
  token: String($env.X_ACCESS_TOKEN || ''),
  tokenSecret: String($env.X_ACCESS_TOKEN_SECRET || ''),
  nonce,
  timestamp
});
return { json: { text: j.tweetText, authorization: signed.authorization } };
"""

# ---------------------------------------------------------------------- nodes

nodes = []

nodes.append(node(
    "DSH Approval Webhook",
    "n8n-nodes-base.webhook",
    2,
    {"httpMethod": "POST", "path": "totem-approvals", "responseMode": "responseNode", "options": {}},
    [-560, 760],
))
nodes[-1]["webhookId"] = "a1b2c3d4-totem-approvals"
nodes.append(code("DSH Approval API", DSH_API, [-320, 760]))
nodes.append(if_node("Needs Draft?", "={{ $json.needsDraft }}", "true", "", [-100, 760]))
nodes.append(http(
    "AI: Draft Manual",
    OPENAI_URL,
    [120, 900],
    method="POST",
    headers=OPENAI_HEADERS,
    body=OPENAI_BODY,
    on_error="continueRegularOutput",
    retry=(3, 3000),
))
nodes.append(code("Store Manual", STORE_MANUAL, [340, 900]))
nodes.append(node(
    "Respond to Webhook",
    "n8n-nodes-base.respondToWebhook",
    1.1,
    {"respondWith": "json", "responseBody": "={{ JSON.stringify($json.body) }}",
     "options": {"responseCode": "={{ $json.statusCode }}"}},
    [-80, 760],
))
nodes.append(node(
    "Every 2 Minutes",
    "n8n-nodes-base.scheduleTrigger",
    1.2,
    {"rule": {"interval": [{"field": "minutes", "minutesInterval": 2}]}},
    [-560, 300],
))
nodes.append(code("Plan Cycle", PLAN, [-340, 300]))
nodes.append(code("Read Queue", QUEUE, [120, 300]))
nodes.append(if_node("Has Actions?", "={{ $json.action }}", "notEquals", "none", [340, 300], op_type="string"))
nodes.append(node(
    "Route Action",
    "n8n-nodes-base.switch",
    3.2,
    {
        "rules": {"values": [
            {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
                            "conditions": [{"id": nid(), "leftValue": "={{ $json.action }}", "rightValue": "approve",
                                            "operator": {"type": "string", "operation": "equals"}}],
                            "combinator": "and"}},
            {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
                            "conditions": [{"id": nid(), "leftValue": "={{ $json.action }}", "rightValue": "redraft",
                                            "operator": {"type": "string", "operation": "equals"}}],
                            "combinator": "and"}},
            {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
                            "conditions": [{"id": nid(), "leftValue": "={{ $json.action }}", "rightValue": "skip",
                                            "operator": {"type": "string", "operation": "equals"}}],
                            "combinator": "and"}},
            {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
                            "conditions": [{"id": nid(), "leftValue": "={{ $json.action }}", "rightValue": "expired",
                                            "operator": {"type": "string", "operation": "equals"}}],
                            "combinator": "and"}},
            {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
                            "conditions": [{"id": nid(), "leftValue": "={{ $json.action }}", "rightValue": "xdisabled",
                                            "operator": {"type": "string", "operation": "equals"}}],
                            "combinator": "and"}}
        ]},
        "options": {},
    },
    [780, 300],
))

nodes.append(code("Sign X Post", SIGN_X, [1000, 80], each=True))
nodes.append(http(
    "Post to X",
    "={{ ($env.POSTER_URL || 'http://192.168.31.200:8099') + '/post' }}",
    [1220, 80],
    method="POST",
    headers=[("Content-Type", "application/json"), ("Authorization", "=Bearer {{ $env.POSTER_TOKEN }}")],
    body="={{ JSON.stringify({ text: $json.text }) }}",
    on_error="continueRegularOutput",
    timeout=180000,
))
nodes.append(code("Prepare Posted", PREPARE_POSTED, [1440, 80], each=True))

nodes.append(code("Build Redraft Request", BUILD_REDRAFT, [1000, 300], each=True))
nodes.append(code("Redraft Fanout", MODEL_FANOUT, [1100, 300]))
nodes.append(http(
    "Draft Again",
    OPENAI_URL,
    [1220, 300],
    method="POST",
    headers=OPENAI_HEADERS,
    body=OPENAI_BODY,
    on_error="continueRegularOutput",
    retry=(2, 3000),
))
nodes.append(code("Prepare Redraft", PREPARE_REDRAFT, [1440, 300]))

nodes.append(code("Prepare Skip", PREPARE_SKIP, [1000, 520], each=True))

nodes.append(code("Finalize Update", FINALIZE, [1660, 300]))

nodes.append(code("No Updates", FINALIZE, [780, 560]))
nodes.append(if_node(
    "GitHub Due?",
    "={{ $json.githubDue }}",
    "true",
    "",
    [2100, 380],
))
nodes.append(code("Tracked Repos", TRACKED, [2320, 380]))
nodes.append(http(
    "GitHub: Releases",
    "=https://api.github.com/repos/{{ $json.owner }}/{{ $json.repo }}/releases?per_page=10",
    [2540, 260],
    headers=GH_HEADERS,
    on_error="continueRegularOutput",
    retry=(2, 2000),
))
nodes.append(http(
    "GitHub: Merged PRs",
    "=https://api.github.com/repos/{{ $json.owner }}/{{ $json.repo }}/pulls?state=closed&sort=updated&direction=desc&per_page=30",
    [2540, 500],
    headers=GH_HEADERS,
    on_error="continueRegularOutput",
    retry=(2, 2000),
))
nodes.append(code("Tag Releases", TAG_RELEASE, [2760, 260], each=True))
nodes.append(code("Tag Pulls", TAG_PULL, [2760, 500], each=True))
nodes.append(node("Merge GitHub Data", "n8n-nodes-base.merge", 3,
                  {"mode": "append", "numberInputs": 2, "options": {}}, [2920, 380]))
nodes.append(code("Select Announcements", SELECT, [3080, 380]))
nodes.append(http(
    "AI: Draft Post",
    OPENAI_URL,
    [3300, 380],
    method="POST",
    headers=OPENAI_HEADERS,
    body=OPENAI_BODY,
    on_error="continueRegularOutput",
    retry=(5, 8000),
))
nodes.append(code("Model Fanout", MODEL_FANOUT, [3080, 380]))
nodes.append(code("Prepare Draft", PREPARE_DRAFT, [3520, 380]))

nodes.append(code("Record Pending", RECORD, [3960, 380], each=True))

# ----------------------------------------------------------------- connections


def edge(target, index=0):
    return {"node": target, "type": "main", "index": index}


connections = {
    "DSH Approval Webhook": {"main": [[edge("DSH Approval API")]]},
    "DSH Approval API": {"main": [[edge("Needs Draft?")]]},
    "Needs Draft?": {"main": [[edge("AI: Draft Manual")], [edge("Respond to Webhook")]]},
    "AI: Draft Manual": {"main": [[edge("Store Manual")]]},
    "Store Manual": {"main": [[edge("Respond to Webhook")]]},
    "Every 2 Minutes": {"main": [[edge("Plan Cycle")]]},
    "Plan Cycle": {"main": [[edge("Read Queue")]]},
    "Read Queue": {"main": [[edge("Has Actions?")]]},
    "Has Actions?": {"main": [[edge("Route Action")], [edge("No Updates")]]},
    "Route Action": {"main": [
        [edge("Sign X Post")],
        [edge("Build Redraft Request")],
        [edge("Prepare Skip")],
        [edge("Prepare Skip")],
        [edge("Prepare Skip")],
    ]},
    "Sign X Post": {"main": [[edge("Post to X")]]},
    "Post to X": {"main": [[edge("Prepare Posted")]]},
    "Prepare Posted": {"main": [[edge("Finalize Update")]]},
    "Build Redraft Request": {"main": [[edge("Redraft Fanout")]]},
    "Redraft Fanout": {"main": [[edge("Draft Again")]]},
    "Draft Again": {"main": [[edge("Prepare Redraft")]]},
    "Prepare Redraft": {"main": [[edge("Finalize Update")]]},
    "Prepare Skip": {"main": [[edge("Finalize Update")]]},

    "Finalize Update": {"main": [[edge("GitHub Due?")]]},
    "No Updates": {"main": [[edge("GitHub Due?")]]},
    "GitHub Due?": {"main": [[edge("Tracked Repos")], []]},
    "Tracked Repos": {"main": [[edge("GitHub: Releases"), edge("GitHub: Merged PRs")]]},
    "GitHub: Releases": {"main": [[edge("Tag Releases")]]},
    "GitHub: Merged PRs": {"main": [[edge("Tag Pulls")]]},
    "Tag Releases": {"main": [[edge("Merge GitHub Data")]]},
    "Tag Pulls": {"main": [[{"node": "Merge GitHub Data", "type": "main", "index": 1}]]},
    "Merge GitHub Data": {"main": [[edge("Select Announcements")]]},
    "Select Announcements": {"main": [[edge("Model Fanout")]]},
    "Model Fanout": {"main": [[edge("AI: Draft Post")]]},
    "AI: Draft Post": {"main": [[edge("Prepare Draft")]]},
    "Prepare Draft": {"main": [[edge("Record Pending")]]},
}

workflow = {
    "id": "ghReleaseTrack01",
    "name": "GitHub Releases & Features → X (Telegram approval)",
    "nodes": nodes,
    "pinData": {},
    "connections": connections,
    "active": False,
    "settings": {"executionOrder": "v1", "timezone": "America/Montevideo"},
    "versionId": "7f3c1d20-0000-4000-8000-000000000001",
    "meta": {"templateCredsSetupCompleted": False},
    "tags": [],
}

# sanity checks
names = {n["name"] for n in nodes}
assert len(names) == len(nodes), "duplicate node names"
for src, ports in connections.items():
    assert src in names, src
    for port in ports["main"]:
        for e in port:
            assert e["node"] in names, e["node"]

OUT.write_text(json.dumps(workflow, indent=2, ensure_ascii=False) + "\n")
print("wrote", OUT, len(nodes), "nodes")
