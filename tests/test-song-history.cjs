const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const sync = require('../.github/scripts/song-history.cjs');

const empty = '{"version":1,"entries":[]}\n';

function harness(t, existing = null, branchExists = Boolean(existing)) {
  const originalCwd = process.cwd();
  const originalSha = process.env.HISTORY_SHA;
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'song-history-test-'));
  process.chdir(dir);
  delete process.env.HISTORY_SHA;
  t.after(() => {
    process.chdir(originalCwd);
    if (originalSha === undefined) delete process.env.HISTORY_SHA;
    else process.env.HISTORY_SHA = originalSha;
    fs.rmSync(dir, {recursive: true, force: true});
  });
  const state = {branchExists, text: existing, sha: 'sha-original', writes: 0, outputs: {}};
  const missing = () => {throw Object.assign(new Error('Not found'), {status: 404});};
  const data = () => ({sha: state.sha, type: 'file', encoding: 'base64',
    content: Buffer.from(state.text).toString('base64')});
  const args = {
    context: {repo: {owner: 'owner', repo: 'repo'}, sha: 'source-sha', runId: 123},
    core: {setOutput: (key, value) => {state.outputs[key] = value;}},
    github: {rest: {
      git: {
        getRef: async () => state.branchExists ? {} : missing(),
        createRef: async (params) => {
          assert.equal(params.ref, 'refs/heads/codex/song-history');
          assert.equal(params.sha, 'source-sha');
          state.branchExists = true;
        },
      },
      repos: {
        getContent: async (params) => {
          assert.equal(params.ref, 'codex/song-history');
          if (state.text === null) missing();
          return {data: data()};
        },
        createOrUpdateFileContents: async (params) => {
          assert.equal(params.branch, 'codex/song-history');
          assert.equal(params.path, 'song-history.json');
          if (state.text !== null) assert.equal(params.sha, state.sha);
          state.writes++;
          state.text = Buffer.from(params.content, 'base64').toString();
          state.sha = `sha-${state.writes}`;
          return {data: {content: {sha: state.sha}}};
        },
      },
    }},
  };
  return {state, args};
}

test('first run initializes durable state and exposes SHA', async t => {
  const {state, args} = harness(t);
  await sync({...args, mode: 'restore'});
  assert.equal(fs.readFileSync('song-history.json', 'utf8'), empty);
  assert.equal(state.text, empty);
  assert.equal(state.outputs.sha, state.sha);
  assert.equal(state.writes, 1);
});

test('existing history survives across runs and successful update is committed', async t => {
  const prior = JSON.stringify({version: 1, entries: [{date: '2026-09-29', songs: [{song: '旧歌'}]}]});
  const {state, args} = harness(t, prior);
  await sync({...args, mode: 'restore'});
  assert.equal(fs.readFileSync('song-history.json', 'utf8'), prior);
  process.env.HISTORY_SHA = state.outputs.sha;
  const next = JSON.stringify({version: 1, entries: [{date: '2026-09-30', songs: [{song: '新歌'}]}]});
  fs.writeFileSync('song-history.json', next);
  await sync({...args, mode: 'save'});
  assert.equal(state.text, next);
  assert.equal(state.writes, 1);
});

test('failed generation or push leaves restored history unchanged', async t => {
  const {state, args} = harness(t, empty);
  await sync({...args, mode: 'restore'});
  process.env.HISTORY_SHA = state.outputs.sha;
  await sync({...args, mode: 'save'});
  assert.equal(state.writes, 0);
});

test('missing history on an existing branch aborts instead of resetting', async t => {
  const {state, args} = harness(t, null, true);
  await assert.rejects(sync({...args, mode: 'restore'}), /Not found/);
  assert.equal(state.writes, 0);
});

test('permission errors abort before creating new state', async t => {
  const {state, args} = harness(t);
  args.github.rest.git.getRef = async () => {throw Object.assign(new Error('Forbidden'), {status: 403});};
  await assert.rejects(sync({...args, mode: 'restore'}), /Forbidden/);
  assert.equal(state.branchExists, false);
});

test('concurrent remote updates are never overwritten', async t => {
  const {state, args} = harness(t, empty);
  await sync({...args, mode: 'restore'});
  process.env.HISTORY_SHA = state.outputs.sha;
  state.sha = 'another-run-sha';
  await assert.rejects(sync({...args, mode: 'save'}), /changed during this run/);
  assert.equal(state.writes, 0);
});

test('remote save failures propagate so workflow can flag state loss', async t => {
  const {args, state} = harness(t, empty);
  await sync({...args, mode: 'restore'});
  process.env.HISTORY_SHA = state.outputs.sha;
  fs.writeFileSync('song-history.json', '{"version":1,"entries":[{}]}');
  args.github.rest.repos.createOrUpdateFileContents = async () => {throw new Error('API unavailable');};
  await assert.rejects(sync({...args, mode: 'save'}), /API unavailable/);
});
