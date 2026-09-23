// UI audit: loads every page in a headless DOM (jsdom) and reports JS errors, colorful emoji left in the
// rendered text/tooltips/dropdowns, and "[object Object]/undefined/NaN" leaking into the page.
//
//   docker run --rm --network host -v "$PWD/tests":/w -w /w node:20-alpine sh -c \
//     'npm i --silent jsdom@22 >/dev/null 2>&1; node ui_audit.js http://172.17.0.1:8880'
//
// Run it against a running deployer (read-only: it only loads pages). The single-color shield and rocket
// emoji are allowed; everything else should be a Font Awesome icon (see sensorIcon()/plain() in app.js).
const { JSDOM, ResourceLoader, VirtualConsole } = require("jsdom");
const base = process.argv[2];
class SameOrigin extends ResourceLoader { fetch(url, o) { return url.startsWith(base) ? super.fetch(url, o) : Promise.resolve(Buffer.from("")); } }
const EMO = /\p{Extended_Pictographic}️?/gu;
const KEEP = new Set(["\u{1F6E1}️", "\u{1F6E1}", "\u{1F680}"]);
async function audit(path, wait) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", e => errors.push("jsdomError: " + String(e.detail || e.message).slice(0, 160)));
  vc.on("error", (...a) => errors.push("console.error: " + a.map(String).join(" ").slice(0, 160)));
  const dom = await JSDOM.fromURL(base + path, { runScripts: "dangerously", resources: new SameOrigin(), pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) { w.fetch = (u, o) => fetch(new URL(u, base).href, o); w.addEventListener("error", e => errors.push("window error: " + e.message)); w.addEventListener("unhandledrejection", e => errors.push("unhandled: " + String(e.reason).slice(0,120))); w.confirm = () => false; } });
  await new Promise(r => setTimeout(r, wait));
  const d = dom.window.document;
  // element.textContent walks <script>/<style> children too (unlike rendered text), which would flag
  // source code (e.g. the confetti emoji array literal) as if it were on-page content. Strip those first.
  const bodyClone = d.body.cloneNode(true);
  bodyClone.querySelectorAll("script,style").forEach(e => e.remove());
  const text = (bodyClone.textContent || "").replace(/\s+/g, " ");
  const emoji = [...new Set((text.match(EMO) || []).filter(m => !KEEP.has(m)))];
  // emoji hidden in attributes (title, data-tip, option labels)
  const attrs = [...d.querySelectorAll("[title],[data-tip],option")].map(e => (e.getAttribute("title") || "") + (e.getAttribute("data-tip") || "") + (e.tagName === "OPTION" ? e.textContent : "")).join(" ");
  const attrEmoji = [...new Set((attrs.match(EMO) || []).filter(m => !KEEP.has(m)))];
  const extra = {};
  const cards = d.querySelectorAll(".sensor-select-card"); if (cards.length) extra.sensorCards = cards.length;
  const grey = d.querySelectorAll(".sensor-select-card.cursor-not-allowed"); if (cards.length) extra.greyedOut = grey.length;
  const obj = /\[object Object\]|undefined|NaN/.exec(text); extra.badText = obj ? obj[0] : "none";
  console.log(`${path.padEnd(22)} errors=${errors.length} emoji-in-text=[${emoji.join("")}] emoji-in-attrs=[${attrEmoji.join("")}] ${JSON.stringify(extra)}`);
  errors.slice(0, 3).forEach(e => console.log("     ", e));
  dom.window.close();
}
(async () => { for (const p of ["/", "/deploy", "/fleet", "/campaigns", "/edl", "/tasks", "/admin", "/static/help.html"]) await audit(p, 7000); process.exit(0); })();
