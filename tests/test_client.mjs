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

test('explicit cell look is sent to the renderer; Use dashboard keeps Paper', async () => {
  const old = {fetch: globalThis.fetch, window: globalThis.window, document: globalThis.document,
               getComputedStyle: globalThis.getComputedStyle};
  let explicit = true, calls = 0;
  globalThis.window = {};
  const tokens = {'--surface': '#141310', '--text-primary': '#f1eee4', '--text-secondary': '#b9b4a6',
                  '--accent-4': '#69b7b0', '--font-family': 'Inter'};
  globalThis.getComputedStyle = () => ({getPropertyValue: name => tokens[name] || ''});
  globalThis.document = {createElement: () => ({getContext: () => ({
    fillStyle: '', fillRect() {}, getImageData() {
      const h = this.fillStyle.replace('#', '');
      return {data: [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16)).concat(255)};
    },
  })})};
  const cell = {getAttribute: name => explicit && name === 'data-theme' ? 'dark' : null,
                style: {getPropertyValue: () => ''}};
  const shadow = {innerHTML: '', host: {closest: () => cell}};
  globalThis.fetch = async url => {
    calls++;
    const query = new URL(url, 'http://local').searchParams;
    assert.equal(JSON.parse(query.get('look')).bg, '#141310');
    assert.equal(JSON.parse(query.get('look')).font, 'Inter');
    assert.equal(query.get('preview'), '1');
    return {ok: true, json: async () => ({image: '/styled.png', colors: {bg:'#141310', ink:'#f1eee4'}})};
  };
  try {
    const ctx = {data: {image:'/paper.png', look_preview:true}, cell:{options:{background:'blue'}}};
    await render(shadow, ctx);
    assert.match(shadow.innerHTML, /styled\.png/);
    explicit = false;
    await render(shadow, ctx);
    assert.match(shadow.innerHTML, /paper\.png/);
    assert.equal(calls, 1);
  } finally {Object.assign(globalThis, old);}
});
