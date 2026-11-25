// Unit harness for the workflow's Code nodes. Stubs the n8n runtime surface
// actually used by the workflow (static data, $env, $json, $input, $('Node')).
const fs = require("fs");
const path = require("path");

const wf = JSON.parse(
  fs.readFileSync(path.join(__dirname, "workflows", "github-release-tracker.json"), "utf8")
);
const codeOf = (name) => {
  const node = wf.nodes.find((n) => n.name === name);
  if (!node) throw new Error("missing node: " + name);
  return node.parameters.jsCode;
};

const env = {
  GITHUB_TOKEN: "ghp_x".padEnd(40, "x"),
  GITHUB_POLL_MINUTES: "15",
  DIGEST_HOURS: "24",
  X_ENABLED: "true",
  DSH_WEBHOOK_TOKEN: "t".repeat(48),
  TRACKED_REPOS: "emiliano-go/trustsight,emiliano-go/rypipe,dbwarden-org/dbwarden",
  OPENAI_MODEL: "gemini-3.5-flash",
};

let failures = 0;
const check = (name, cond, extra) => {
  if (cond) console.log("  ok  " + name);
  else {
    failures++;
    console.log("  FAIL " + name + (extra ? " -> " + JSON.stringify(extra) : ""));
  }
};

function makeRuntime(state) {
  const nodeItems = {};
  return {
    state,
    setNode(name, items) { nodeItems[name] = items; },
    $env: env,
    $getWorkflowStaticData() { return state; },
    $input: { all: () => state.__input || [] },
    $: (name) => ({
      all: () => nodeItems[name] || [],
      first: () => (nodeItems[name] || [])[0],
      item: (nodeItems[name] || [])[0],
    }),
  };
}

function run(name, rt, json) {
  const fn = new Function("$json", "$getWorkflowStaticData", "$env", "$", "$input", codeOf(name));
  return fn(json, rt.$getWorkflowStaticData, rt.$env, rt.$, rt.$input);
}

const nowIso = new Date().toISOString();
const pending = (over = {}) => ({
  key: "k1", kind: "release", repo: "emiliano-go/trustsight", title: "v0.30.0",
  url: "https://github.com/emiliano-go/trustsight/releases/tag/v0.30.0",
  body: "Adds rule caching and faster scans.",
  draft: "TrustSight v0.30.0 ships rule caching, so scans finish faster.",
  tweetText: "TrustSight v0.30.0 ships rule caching, so scans finish faster.\n\nhttps://github.com/emiliano-go/trustsight/releases/tag/v0.30.0",
  status: "pending", attempts: 0, featureNumbers: [], ...over,
});

console.log("Plan Cycle");
{
  const st = {};
  const out = run("Plan Cycle", makeRuntime(st), {});
  check("initialises state", st.v === 1 && st.initialized === false);
  check("configured on a real GitHub token", out[0].json.configured === true);
  check("first run is due", out[0].json.githubDue === true);
}
{
  const st = { v: 1, initialized: true, lastPollAt: Date.now() - 60_000, seen: {}, offered: {}, prQueue: {}, lastDigestAt: {}, pending: {} };
  check("not due before the interval", run("Plan Cycle", makeRuntime(st), {})[0].json.githubDue === false);
  const saved = env.GITHUB_TOKEN;
  env.GITHUB_TOKEN = "PASTE_GITHUB_TOKEN";
  const inert = run("Plan Cycle", makeRuntime(st), {})[0].json;
  check("inert while the token is a placeholder", inert.configured === false && inert.githubDue === false);
  env.GITHUB_TOKEN = saved;
}

console.log("Read Queue");
{
  const st = { v: 1, pending: { k1: pending() }, dshActions: { k1: "ok" } };
  const out = run("Read Queue", makeRuntime(st), {});
  check("queued ok becomes approve", out[0].json.action === "approve" && out[0].json.source === "dsh");
  check("queue drains", Object.keys(st.dshActions).length === 0);
  check("carries the draft", out[0].json.draft === pending().draft);
}
{
  const st = { v: 1, pending: {}, dshActions: { gone: "no" } };
  check("unknown key expires", run("Read Queue", makeRuntime(st), {})[0].json.action === "expired");
}
{
  const st = { v: 1, pending: { k1: pending() }, dshActions: { k1: "ok" } };
  env.X_ENABLED = "false";
  check("approve blocked while X is off", run("Read Queue", makeRuntime(st), {})[0].json.action === "xdisabled");
  env.X_ENABLED = "true";
}
{
  const st = { v: 1, pending: {}, dshActions: {} };
  const out = run("Read Queue", makeRuntime(st), {});
  check("quiet queue emits sentinel", out.length === 1 && out[0].json.action === "none");
}

console.log("Prepare Posted");
{
  const st = { v: 1, pending: { k1: pending() } };
  const rt = makeRuntime(st);
  rt.setNode("Read Queue", [{ json: { action: "approve", key: "k1", source: "dsh" } }]);
  const out = run("Prepare Posted", rt, { data: { id: "555" } }).json;
  check("reports success", out.posted === true && out.done === true);
  check("marks pending posted", st.pending.k1.status === "posted" && st.pending.k1.postId === "555");
  check("clears last error", !st.pending.k1.lastError);
}
{
  const st = { v: 1, pending: { k1: pending() } };
  const rt = makeRuntime(st);
  rt.setNode("Read Queue", [{ json: { action: "approve", key: "k1", source: "dsh" } }]);
  const out = run("Prepare Posted", rt, { detail: "Unauthorized" }).json;
  check("reports failure", out.posted === false && out.error === "Unauthorized");
  check("keeps draft pending for another try", st.pending.k1.status === "pending");
  check("stores the error for the UI", st.pending.k1.lastError === "Unauthorized");
}

console.log("Prepare Redraft");
{
  const st = { v: 1, pending: { k1: pending() } };
  const rt = makeRuntime(st);
  rt.setNode("Build Redraft Request", [{ json: { key: "k1" } }]);
  const out = run("Prepare Redraft", rt, { choices: [{ message: { content: "Faster TrustSight scans are here — rule caching lands in v0.30.0." } }] }).json;
  check("replaces the draft", st.pending.k1.draft.startsWith("Faster TrustSight scans"));
  check("keeps the link", st.pending.k1.tweetText.includes("releases/tag/v0.30.0"));
  check("counts attempts", st.pending.k1.attempts === 1 && out.attempts === 1);
  check("sanitizes the redraft", !st.pending.k1.draft.includes("—"));
}
{
  const st = { v: 1, pending: { k1: pending() } };
  const rt = makeRuntime(st);
  rt.setNode("Build Redraft Request", [{ json: { key: "k1" } }]);
  run("Prepare Redraft", rt, { error: "no key" });
  check("falls back to the previous draft", st.pending.k1.draft === pending().draft);
}

console.log("Prepare Skip");
{
  const st = { v: 1, pending: { k1: pending() } };
  const out = run("Prepare Skip", makeRuntime(st), { action: "skip", key: "k1" }).json;
  check("marks skipped", st.pending.k1.status === "skipped" && out.done === true);
}
{
  const st = { v: 1, pending: { k1: pending() } };
  run("Prepare Skip", makeRuntime(st), { action: "expired", key: "k1" });
  check("expired leaves the draft alone", st.pending.k1.status === "pending");
}

console.log("Select Announcements");
const rel = (id, over = {}) => ({ id, tag_name: "v" + id, name: "v" + id, draft: false, prerelease: false, html_url: "https://github.com/emiliano-go/trustsight/releases/tag/v" + id, body: "Faster scans and rule caching.", ...over });
const pr = (n, over = {}) => ({ number: n, title: "Add rule caching " + n, merged_at: nowIso, html_url: "https://github.com/emiliano-go/trustsight/pull/" + n, body: "Speeds up scans.", ...over });

{
  const st = { v: 1, initialized: false, seen: {}, offered: {}, prQueue: {}, lastDigestAt: {}, pending: {} };
  const rt = makeRuntime(st);
  st.__input = [
    { kind: "release", repo: "emiliano-go/trustsight", ok: true, payload: [rel(1)] },
    { kind: "pull", repo: "emiliano-go/trustsight", ok: true, payload: [pr(1)] },
  ].map((json) => ({ json }));
  const out = run("Select Announcements", rt, {});
  check("baseline emits nothing", out.length === 0);
  check("baseline marks releases seen", Object.keys(st.seen).filter((k) => k.includes("#release:")).length === 1);
  check("baseline marks PRs seen", Object.keys(st.seen).filter((k) => k.includes("#pr:")).length === 1);
  check("baseline initialises", st.initialized === true);
}
{
  const st = { v: 1, initialized: true, seen: {}, offered: {}, prQueue: {}, lastDigestAt: { "emiliano-go/trustsight": nowIso }, pending: {} };
  const rt = makeRuntime(st);
  st.__input = [
    { kind: "release", repo: "emiliano-go/trustsight", ok: true, payload: [rel(9)] },
    { kind: "pull", repo: "emiliano-go/trustsight", ok: true, payload: [] },
  ].map((json) => ({ json }));
  const out = run("Select Announcements", rt, {});
  check("new release announced", out.length === 1 && out[0].json.kind === "release");
  check("key fits callback_data", out[0].json.key.length <= 60);
  check("does not re-offer", run("Select Announcements", rt, {}).length === 0);
}
{
  const st = { v: 1, initialized: true, seen: {}, offered: {}, prQueue: {}, lastDigestAt: { "emiliano-go/trustsight": nowIso }, pending: {} };
  const rt = makeRuntime(st);
  st.__input = [
    { kind: "release", repo: "emiliano-go/trustsight", ok: true, payload: [] },
    { kind: "pull", repo: "emiliano-go/trustsight", ok: true, payload: [pr(11), pr(12)] },
  ].map((json) => ({ json }));
  check("merged PRs queue up", run("Select Announcements", rt, {}).length === 0 && st.prQueue["emiliano-go/trustsight"].length === 2);
  // Fresh merges keep the digest waiting: the burst is still growing.
  st.lastDigestAt["emiliano-go/trustsight"] = new Date(Date.now() - 25 * 3600_000).toISOString();
  check("digest waits while PRs are still landing", run("Select Announcements", rt, {}).length === 0);
  // Once the queue goes quiet, the digest covers everything in it.
  const settledAt = new Date(Date.now() - 3 * 3600_000).toISOString();
  st.prQueue["emiliano-go/trustsight"] = [pr(11, { merged_at: settledAt }), pr(12, { merged_at: settledAt })];
  const digest = run("Select Announcements", rt, {});
  check("digest fires after the quiet window", digest.length === 1 && digest[0].json.kind === "features");
  check("digest numbers every change", digest[0].json.userPrompt.includes("1. ") && digest[0].json.userPrompt.includes("2. "));
  check("digest prompt demands full coverage", /Mention every numbered change/i.test(digest[0].json.systemPrompt));
  check("digest forbids PR framing", /never mention pull requests/i.test(digest[0].json.systemPrompt));
  check("digest carries feature numbers", digest[0].json.featureNumbers.length === 2);
}
{
  const st = { v: 1, initialized: false, seen: {}, offered: {}, prQueue: {}, lastDigestAt: {}, pending: {} };
  const rt = makeRuntime(st);
  st.__input = [{ kind: "release", repo: "emiliano-go/trustsight", ok: false, payload: [{ message: "Bad credentials" }] }].map((json) => ({ json }));
  check("failed fetch is not a baseline", run("Select Announcements", rt, {}).length === 0 && st.initialized === false);
}
{
  // A repo added later must not announce its existing history.
  const st = {
    v: 1, initialized: true, seen: {}, offered: {}, prQueue: {},
    lastDigestAt: { "emiliano-go/trustsight": nowIso },
    pending: {},
  };
  const rt = makeRuntime(st);
  st.__input = [
    { kind: "release", repo: "emiliano-go/trustsight-web", ok: true, payload: [rel(1)] },
    { kind: "pull", repo: "emiliano-go/trustsight-web", ok: true, payload: [pr(5)] },
  ].map((json) => ({ json }));
  const out = run("Select Announcements", rt, {});
  check("new repo is baselined silently", out.length === 0);
  check("new repo gets a digest clock", Boolean(st.lastDigestAt["emiliano-go/trustsight-web"]));
  check("new repo history is marked seen", Object.keys(st.seen).filter(k => k.includes("trustsight-web")).length === 2);
}
{
  const st = {
    v: 1, initialized: true, seen: {}, offered: {}, prQueue: {},
    lastDigestAt: { "emiliano-go/trustsight": new Date(Date.now() - 30 * 3600_000).toISOString() },
    pending: {},
  };
  const rt = makeRuntime(st);
  const settled = new Date(Date.now() - 3 * 3600_000).toISOString();
  st.__input = [
    { kind: "release", repo: "emiliano-go/trustsight", ok: true, payload: [] },
    { kind: "pull", repo: "emiliano-go/trustsight", ok: true, payload: [pr(21, { merged_at: settled }), pr(22, { merged_at: settled })] },
  ].map((json) => ({ json }));
  const out = run("Select Announcements", rt, {});
  check("digest links the repository", out.length === 1 && out[0].json.url === "https://github.com/emiliano-go/trustsight");
  check("digest never links a PR page", !out[0].json.url.includes("/pulls"));
}

console.log("Prepare Draft");
{
  const a = { kind: "release", key: "r1", repo: "emiliano-go/trustsight", title: "v1", url: "https://github.com/emiliano-go/trustsight/releases/tag/v1", fallbackDraft: "x".repeat(400), featureNumbers: [] };
  const rt = makeRuntime({ v: 1 });
  rt.setNode("Select Announcements", [{ json: a }]);
  const j = run("Prepare Draft", rt, { error: "no key" }).json;
  check("falls back on AI error", j.draft.length > 0);
  check("clamps to the X budget", j.tweetText.length <= 280 + 60);
  check("flags the fallback", j.tgText.includes("Template draft"));
  check("strips wrapping quotes", !run("Prepare Draft", rt, { choices: [{ message: { content: '"Quoted copy."' } }] }).json.draft.startsWith('"'));
}
{
  const a = { kind: "release", key: "r9", repo: "emiliano-go/trustsight", title: "v9", url: "https://github.com/emiliano-go/trustsight/releases/tag/v9", fallbackDraft: "x", featureNumbers: [] };
  const rt = makeRuntime({ v: 1 });
  rt.setNode("Select Announcements", [{ json: a }]);
  const d = run("Prepare Draft", rt, { choices: [{ message: { content: "Shipped **rule caching** — scans finish faster #devtools\n- no stale commits" } }] }).json.draft;
  check("no em dash", !d.includes("\u2014") && !d.includes("\u2013"));
  check("no bullets", !/^\s*[-*]\s/m.test(d));
  check("no hashtags", !/#devtools/.test(d));
  check("no bold markers", !d.includes("**"));
}

console.log("Record Pending");
{
  const st = { v: 1, pending: {}, seen: {}, offered: {}, prQueue: {}, lastDigestAt: {} };
  const rt = makeRuntime(st);
  rt.setNode("Prepare Draft", [{ json: { key: "r9", kind: "release", repo: "emiliano-go/trustsight", title: "v9", url: "u", body: "b", draft: "d", tweetText: "d\n\nu", sk: "emiliano-go/trustsight#release:9", featureNumbers: [] } }]);
  const out = run("Record Pending", rt, {}).json;
  check("stores a pending draft", out.queued === true && st.pending.r9.status === "pending");
  check("marks the release seen", st.seen["emiliano-go/trustsight#release:9"] !== undefined);
}

console.log("Manual enqueue");
{
  const st = { v: 1, pending: {} };
  const rt = makeRuntime(st);
  const req = (body) => ({ headers: { "x-totem-token": env.DSH_WEBHOOK_TOKEN }, body });
  check("rejects a short note", run("DSH Approval API", rt, req({ op: "enqueue", note: "too short" }))[0].json.statusCode === 400);
  check("rejects a bad url", run("DSH Approval API", rt, req({ op: "enqueue", note: "A manual note about the release", url: "javascript:alert(1)" }))[0].json.statusCode === 400);
  const good = run("DSH Approval API", rt, req({ op: "enqueue", note: "Shipped a small CLI that converts invoices to CSV.", url: "https://example.com/x" }))[0].json;
  check("asks for a draft", good.needsDraft === true && good.statusCode === 201);
  check("prompt forbids invented facts", /Never add facts/.test(good.systemPrompt));
  check("note reaches the prompt", good.userPrompt.includes("invoice"));

  rt.setNode("DSH Approval API", [{ json: good }]);
  const stored = run("Store Manual", rt, { choices: [{ message: { content: "New: a small CLI that turns invoices into CSV — one command, no config." } }] })[0].json;
  check("stores a manual draft", stored.statusCode === 201 && st.pending[stored.body.key].kind === "manual");
  check("appends the link", st.pending[stored.body.key].tweetText.includes("https://example.com/x"));
  check("sanitizes the manual draft", !st.pending[stored.body.key].draft.includes("\u2014"));
}
{
  const st = { v: 1, pending: {} };
  const rt = makeRuntime(st);
  rt.setNode("DSH Approval API", [{ json: { note: "A note with no URL at all", url: "" } }]);
  const stored = run("Store Manual", rt, { error: "no key" })[0].json;
  const item = st.pending[stored.body.key];
  check("falls back to the note when the model fails", item.draft.startsWith("A note with no URL"));
  check("manual post without a link has no trailing blank lines", item.tweetText === item.draft);
}

console.log("Release priority");
{
  const st = {
    v: 1, initialized: true, seen: {}, offered: {}, prQueue: {},
    lastDigestAt: { "emiliano-go/trustsight": new Date(Date.now() - 30 * 3600_000).toISOString(), "emiliano-go/other": new Date().toISOString() },
    pending: {
      d1: { key: "d1", kind: "features", repo: "emiliano-go/trustsight", status: "pending", draft: "old digest" },
      d2: { key: "d2", kind: "features", repo: "emiliano-go/other", status: "pending", draft: "keep me" },
    },
    postQueue: [],
  };
  const rt = makeRuntime(st);
  st.__input = [
    { kind: "release", repo: "emiliano-go/trustsight", ok: true, payload: [rel(9, { body: "## What changed\nFixed the probe bypasses. Bumped CI. Full changelog below." })] },
    { kind: "pull", repo: "emiliano-go/trustsight", ok: true, payload: [pr(31), pr(32)] },
  ].map((json) => ({ json }));
  const out = run("Select Announcements", rt, {});
  check("release is announced", out.length === 1 && out[0].json.kind === "release");
  check("release draft asks for a tldr", /TL;DR/i.test(out[0].json.systemPrompt));
  check("merged PRs are folded into the release", st.prQueue["emiliano-go/trustsight"].length === 0);
  check("stale digest for that repo is dropped", st.pending.d1.status === "skipped");
  check("other repos keep their digest", st.pending.d2.status === "pending");
}

console.log("Paced posting queue");
{
  const st = {
    v: 1, initialized: true, seen: {}, offered: {}, prQueue: {}, lastDigestAt: {},
    pending: { k1: { kind: "release", repo: "a/b", status: "pending", draft: "one", tweetText: "one" },
               k2: { kind: "release", repo: "a/b", status: "pending", draft: "two", tweetText: "two" } },
    dshActions: { k1: "ok", k2: "ok" },
  };
  const rt = makeRuntime(st);
  const first = run("Read Queue", rt, {});
  const released = first.filter((i) => i.json.action === "approve");
  check("one approval publishes on the first slot", released.length === 1 && released[0].json.key === "k1");
  check("the second waits in the queue", st.postQueue.length === 1 && st.postQueue[0].key === "k2");
  const second = run("Read Queue", rt, {});
  check("the next slot is not released early", !second.some((i) => i.json.action === "approve"));
  const gapBefore = st.nextPostAt - st.lastPostAt;
  check("next slot lands within the jitter window", gapBefore >= 13 * 60_000 && gapBefore <= 17 * 60_000);
  st.nextPostAt = Date.now() - 1000;
  const third = run("Read Queue", rt, {});
  const later = third.filter((i) => i.json.action === "approve");
  check("fifteen minutes later the next one goes", later.length === 1 && later[0].json.key === "k2");
  check("queue empties", st.postQueue.length === 0);
}

console.log("Sign X Post");
{
  const realRandom = Math.random;
  const realNow = Date.now;
  Math.random = () => 0.5;
  Date.now = () => 1318622958 * 1000;
  env.X_CONSUMER_KEY = "xvz1evFS4wEEPTGEFPHBog";
  env.X_CONSUMER_SECRET = "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw";
  env.X_ACCESS_TOKEN = "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb";
  env.X_ACCESS_TOKEN_SECRET = "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE";

  const out = run("Sign X Post", makeRuntime({ v: 1 }), { tweetText: "hello" }).json;
  const { signRequest } = require("./x_sign.js");
  const expected = signRequest({
    method: "POST",
    url: "https://api.x.com/2/tweets",
    consumerKey: env.X_CONSUMER_KEY,
    consumerSecret: env.X_CONSUMER_SECRET,
    token: env.X_ACCESS_TOKEN,
    tokenSecret: env.X_ACCESS_TOKEN_SECRET,
    nonce: "n" + Math.floor(0.5 * 1e16).toString(36),
    timestamp: 1318622958,
  }).authorization;

  check("builds an OAuth1 header", out.authorization.startsWith("OAuth "));
  check("signature matches the reference signer", out.authorization === expected);
  check("signs the post text", out.text === "hello");
  check("no empty oauth values", !/=\"\"/.test(out.authorization));

  Math.random = realRandom;
  Date.now = realNow;
}

console.log("Build Redraft Request");
{
  const st = { v: 1, pending: { k1: pending() } };
  const out = run("Build Redraft Request", makeRuntime(st), { key: "k1" }).json;
  check("carries the previous draft", out.userPrompt.includes(pending().draft));
  check("asks for a different angle", /different/i.test(out.systemPrompt));
}

console.log("");
if (failures) {
  console.log(failures + " FAILURE(S)");
  process.exit(1);
}
console.log("all checks passed");
