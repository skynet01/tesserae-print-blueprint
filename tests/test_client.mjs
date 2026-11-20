import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs/promises';

const source = await fs.readFile(new URL('../print_blueprint/client.js', import.meta.url), 'utf8');
const {default: render} = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

test('real AI render waits up to exactly 15 seconds and uses the finished sheet', async () => {
  const old = {fetch: globalThis.fetch, window: globalThis.window, setTimeout, clearTimeout};
  let delay, cleared;
  globalThis.window = {TESSERAE_URL_PREFIX: '/ingress'};
  globalThis.setTimeout = (fn, ms) => { delay = ms; return 123; };
  globalThis.clearTimeout = id => { cleared = id; };
  globalThis.fetch = async (url, opts) => {
    assert.equal(url, '/ingress/plugins/print_blueprint/ready/key');
    assert.equal(opts.cache, 'no-store');
    assert.ok(opts.signal);
    return {ok: true, status: 200, json: async () => ({image: '/plugins/print_blueprint/image/ai.png'})};
  };
  try {
    const shadow = {innerHTML: ''};
    await render(shadow, {data: {image: '/fallback.png', ai_wait: '/plugins/print_blueprint/ready/key'}});
    assert.equal(delay, 15_000);
    assert.equal(cleared, 123);
    assert.match(shadow.innerHTML, /image\/ai\.png/);
    assert.doesNotMatch(shadow.innerHTML, /fallback\.png/);
  } finally { Object.assign(globalThis, old); }
});

test('a preview never waits, even with wait data from the server', async () => {
  const old = globalThis.fetch;
  globalThis.fetch = () => assert.fail('preview must not request a wait');
  try {
    const shadow = {innerHTML: ''};
    await render(shadow, {preview: true, data: {image: '/fallback.png', ai_wait: '/ready/key'}});
    assert.match(shadow.innerHTML, /fallback\.png/);
  } finally { globalThis.fetch = old; }
});

test('unavailable AI or a timed-out request keeps the fallback and resolves composition', async () => {
  const old = {fetch: globalThis.fetch, window: globalThis.window, setTimeout, clearTimeout};
  globalThis.window = {};
  let abort;
  globalThis.setTimeout = fn => { abort = fn; return 123; };
  globalThis.clearTimeout = () => {};
  try {
    for (const mode of ['not-ready', 'error', 'timeout']) {
      globalThis.fetch = async (_url, {signal}) => {
        if (mode === 'error') throw Error('offline');
        if (mode === 'timeout') {
          return new Promise((resolve, reject) => {
            signal.addEventListener('abort', () => reject(Error('aborted')));
            abort();
          });
        }
        return {ok: true, status: 204};
      };
      const shadow = {innerHTML: ''};
      await render(shadow, {data: {image: '/fallback.png', ai_wait: '/ready/key'}});
      assert.match(shadow.innerHTML, /fallback\.png/);
    }
  } finally { Object.assign(globalThis, old); }
});
