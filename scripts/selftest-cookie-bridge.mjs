import assert from 'node:assert/strict';

const duplicate = {
  name: 'auth_token',
  domain: '.kick.com',
  path: '/',
  storeId: '0',
  value: 'token',
};
globalThis.chrome = {
  cookies: {
    getAll: async (details) => details.partitionKey ? [duplicate, duplicate] : [duplicate],
  },
};

const {
  default: getAllCookies,
  createDebouncedPush,
} = await import('../vendor/cookie-extension/src/modules/get_all_cookies.mjs').then(async (merge) => ({
  default: merge.default,
  ...(await import('../vendor/cookie-extension/src/modules/cookie_bridge.mjs')),
}));

const merged = await getAllCookies({ storeId: '0', partitionKey: { topLevelSite: 'https://kick.com' } });
assert.equal(merged.length, 1, 'duplicate cookie rows must be collapsed');

let releasePost;
const posts = [];
const schedule = createDebouncedPush({
  collect: async () => [duplicate],
  post: async (payload) => {
    posts.push(payload);
    await new Promise((resolve) => { releasePost = resolve; });
  },
  delayMs: 0,
});
schedule();
await new Promise((resolve) => setTimeout(resolve, 10));
assert.equal(posts.length, 1, 'first scheduled push must run');
schedule();
await new Promise((resolve) => setTimeout(resolve, 10));
assert.equal(posts.length, 1, 'in-flight push must not overlap');
releasePost();
await new Promise((resolve) => setTimeout(resolve, 10));
assert.equal(posts.length, 2, 'changes during an in-flight push must be retried');
releasePost();
console.log('cookie bridge self-checks passed');
