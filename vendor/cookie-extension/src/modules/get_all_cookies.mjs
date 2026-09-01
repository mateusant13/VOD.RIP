/**
 * Get all cookies that match the given criteria.
 * @param {chrome.cookies.GetAllDetails} details
 * @returns {Promise<chrome.cookies.Cookie[]>}
 */
export default async function getAllCookies(details) {
  details.storeId ??= await getCurrentCookieStoreId();
  const { partitionKey, ...detailsWithoutPartitionKey } = details;
  let cookiesWithPartitionKey = [];
  if (partitionKey) {
    try {
      cookiesWithPartitionKey = await chrome.cookies.getAll(details);
    } catch (err) {
      const message = String(err?.message || err);
      if (!/partition[\s_-]*key/i.test(message) || !/(unsupported|not supported|unknown|invalid)/i.test(message)) {
        throw err;
      }
    }
  }
  const cookies = await chrome.cookies.getAll(detailsWithoutPartitionKey);
  const unique = new Map();
  for (const cookie of [...cookies, ...cookiesWithPartitionKey]) {
    const partition = JSON.stringify(cookie.partitionKey || null);
    const key = [cookie.name, cookie.domain, cookie.path, cookie.storeId, partition].join('\u0000');
    if (!unique.has(key)) unique.set(key, cookie);
  }
  return [...unique.values()];
}

/**
 * Get the current cookie store ID.
 * @returns {Promise<string | undefined>}
 */
const getCurrentCookieStoreId = async () => {
  // If the extension is in split incognito mode, return undefined to choose the default store.
  if (chrome.runtime.getManifest().incognito === 'split') return undefined;

  // Firefox supports the `tab.cookieStoreId` property.
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab.cookieStoreId) return tab.cookieStoreId;

  // Chrome does not support the `tab.cookieStoreId` property.
  const stores = await chrome.cookies.getAllCookieStores();
  return stores.find((store) => store.tabIds.includes(tab.id))?.id;
};
