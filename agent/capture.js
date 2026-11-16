// Walk the bookmarks list and return the raw GraphQL bodies for each page.
// Python does all parsing, so this file stays a transport: open, capture, scroll, hand back.
//
// ARGS is replaced by agent/sync.py before this file runs:
//   { maxPages, startUrl, settleMs, scrollPx, scrollWaitMs }
const ARGS = __ARGS__;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const notes = [];

await page.goto(ARGS.startUrl, { waitUntil: "domcontentloaded", timeout: 45000 });
await page.waitForTimeout(ARGS.settleMs);

// A signed-out session lands on the login flow: only the person can fix that.
const landed = await page.url();
if (/\/i\/flow\/login|\/i\/login|^https:\/\/x\.com\/?$/.test(landed)) {
  await handoff("Sign in to X in this tab, then press Continue.", { timeoutMs: 300000 });
  await page.goto(ARGS.startUrl, { waitUntil: "domcontentloaded", timeout: 45000 });
  await page.waitForTimeout(ARGS.settleMs);
}

const start = await network.start({
  urlFilter: "/graphql/",
  resourceTypes: ["XHR", "Fetch"],
  bodies: true,
});

// Reload inside the capture so the first page counts too.
await page.reload({ waitUntil: "domcontentloaded", timeout: 45000 });
await page.waitForTimeout(ARGS.settleMs);

for (let index = 0; index < ARGS.maxPages; index += 1) {
  await page.mouse.wheel(0, ARGS.scrollPx);
  await page.waitForTimeout(ARGS.scrollWaitMs);
  const status = await network.status();
  notes.push({ step: index, entries: status.entryCount, responses: status.responseCount });
}

const capture = await network.stop();
const exchanges = (capture && capture.exchanges) || [];
const pages = [];

// The execute result is truncated near 100 KB, so page bodies go to files in the temp dir
// (files.roots) and only the paths come back here.
let fileIndex = 0;
for (const exchange of exchanges) {
  const url = (exchange.request && exchange.request.url) || "";
  if (!/\/Bookmarks\?/.test(url)) continue;
  const body = (exchange.response && exchange.response.body) || {};
  if (typeof body.text !== "string") continue;
  const name = `tb-page-${fileIndex}-${Date.now()}.json`;
  await files.write(name, body.text);
  pages.push({
    path: files.path(name),
    url,
    status: (exchange.response && exchange.response.status) || 0,
    bytes: body.text.length,
  });
  fileIndex += 1;
}

// Leave the tab open: the next run may want to continue here.
return {
  pageUrl: await page.url(),
  pageCount: pages.length,
  pages,
  notes,
  dropped: (capture && capture.droppedEntryCount) || 0,
  truncated: (capture && capture.truncatedBodyCount) || 0,
};
