// 게임 화면(JS)을 node 에서 실제로 돌려 본다. `token_rpg.py selftest` 가 부른다.
//
// selftest 는 파이썬 식만 계산하고 화면 쪽은 문자열이 있는지만 봤다. 그 틈으로
// v0.14.4(선언 순서 — 화면 전체 공백)와 v0.14.5(없는 상수 — NaN 레벨)가 나갔다.
// 여기서는 가짜 DOM 위에 스크립트를 통째로 올리고 버튼을 눌러 한 바퀴 돈다.
//   1) 어느 화면에서도 예외가 안 난다
//   2) 그린 글자에 NaN·undefined 가 없다
//   3) 파이썬과 두 벌인 식(보스·레벨·스탯·벽)이 같은 값을 낸다
//
// 브라우저가 아니다. 배치·CSS·터치는 못 본다 — 잡는 것은 로직과 문구다.
"use strict";
const fs = require("fs"), vm = require("vm");

const fx = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const flush = () => new Promise(r => setImmediate(r));
const ok = (cond, msg) => { if (!cond) throw new Error(msg); };
const same = (a, b) => Math.abs(a - b) <= 1e-9 * Math.max(1, Math.abs(a), Math.abs(b));

// ── 가짜 DOM. 게임이 쓰는 만큼만 흉내 낸다 — 모르는 선택자는 조용히 넘기지 않고 던진다.
const TAG = /<([a-zA-Z][\w-]*)((?:\s+[\w-]+(?:\s*=\s*(?:"[^"]*"|'[^']*'))?)*)\s*\/?>/g;
const ATTR = /([\w-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'))?/g;

class El {
  constructor(doc, tag, attrs = {}) {
    this.doc = doc; this.tag = tag.toLowerCase(); this.kids = [];
    this.html = ""; this.text = ""; this.value = ""; this.style = {}; this.dataset = {};
    this.id = attrs.id || "";
    this.disabled = "disabled" in attrs; this.open = "open" in attrs;
    const cls = new Set((attrs.class || "").split(/\s+/).filter(Boolean));
    this.classList = {add: c => cls.add(c), remove: c => cls.delete(c), contains: c => cls.has(c)};
    this._cls = cls;
    for (const [k, v] of Object.entries(attrs))
      if (k.startsWith("data-")) this.dataset[k.slice(5)] = v;
  }
  set className(v) { this._cls.clear(); String(v).split(/\s+/).filter(Boolean).forEach(c => this._cls.add(c)); }
  get className() { return [...this._cls].join(" "); }
  all() { return this.kids.flatMap(k => [k, ...k.all()]); }
  _drop() {
    for (const e of this.all()) if (e.id && this.doc.ids.get(e.id) === e) this.doc.ids.delete(e.id);
    this.kids = [];
  }
  _take(list) {
    for (const e of list) for (const x of [e, ...e.all()]) if (x.id) this.doc.ids.set(x.id, x);
    this.kids.push(...list);
  }
  set innerHTML(h) { this._drop(); this.html = String(h); this.text = ""; this._take(parse(this.doc, this.html)); }
  get innerHTML() { return this.html; }
  set textContent(v) { this._drop(); this.html = ""; this.text = String(v); }
  get textContent() { return this.text; }
  insertAdjacentHTML(pos, h) { this.html += String(h); this._take(parse(this.doc, String(h))); }
  appendChild(e) { this._take([e]); return e; }
  querySelectorAll(sel) {
    const m = /^(\w+)(?:\[([\w-]+)\])?(:not\(\[open\]\))?$/.exec(sel);
    if (!m) throw new Error(`가짜 DOM 이 모르는 선택자: ${sel} — tests/game_smoke.js 에 더해라`);
    const key = m[2] && m[2].replace(/^data-/, "");
    return this.all().filter(e => e.tag === m[1] && (!m[2] || key in e.dataset) && (!m[3] || !e.open));
  }
  select() {}
  get offsetWidth() { return 0; }
}
function parse(doc, html) {
  const out = [];
  for (const m of html.matchAll(TAG)) {
    const attrs = {};
    for (const a of m[2].matchAll(ATTR)) attrs[a[1]] = a[2] ?? a[3] ?? "";
    out.push(new El(doc, m[1], attrs));
  }
  return out;
}

// 결정적 난수 — 같은 입력이면 같은 판이 나와야 실패를 다시 볼 수 있다
const rng = seed => () => {
  seed = (seed + 0x6d2b79f5) | 0;
  let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
  t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
};

const PRELUDE = `(() => { const R = Date, now = __now;
  globalThis.Date = class extends R {
    constructor(...a) { if (a.length) super(...a); else super(now()); }
    static now() { return now(); } };
  Math.random = __rand; })();`;
// 스크립트 안쪽 이름을 밖으로 꺼낸다. 게임에서 이름을 바꾸면 여기서 ReferenceError 가 난다 — 같이 고친다.
const TAIL = `
;globalThis.__t = { get save() { return save; }, set save(v) { save = v; }, K, H, fresh,
  n, boss, slotOf, catchRare, dexKey, sideSoul, idleRate, slotPay, raidHp, raidPay, raidClaim, raidRelic, weekSum, raidWk, critMax, spdMax, spdVs, spdNow, goesFirst, xtra, hitsOf, ACH, achCur, achSoul, winStage, catchRare, mutOf, mutPow, mutId, gated, gateLv, fill, buyRes, shardCost, rollShard, newFighters, F, expFor, levelOf, lvNow, costOf, soulOf, pointsOf, beatable, turnDmg, drawAll,
  encodeSave, decodeSave, validSave, left, points, picks, relicVal, wantPicks, relicLeft, goldLeft, missions, transOf, maxCleared };`;

const [Y, M, DAY] = fx.today.split("-").map(Number);
const NOON = new Date(Y, M - 1, DAY, 12).getTime();     // 현지 정오 — 어느 시간대에서도 같은 날짜가 나온다

// 탭 칸(div[data-pane])을 찾는다
const doc_pane = (g, k) => g.doc.querySelectorAll("div[data-pane]").find(p => p.dataset.pane === k);
// 가짜 DOM 은 페이지 뼈대를 납작하게 읽는다 — 탭 버튼은 nav 의 자식이 아니라 문서에서 찾는다
const tab_btn = (g, k) => g.doc.querySelectorAll("button[data-tab]").find(b => b.dataset.tab === k);
async function boot(name, {page = "base", served = true, save, rev = 0, local = {}, seed = 1} = {}) {
  const html = fx.pages[page];
  const cut = html.indexOf("<script>"), end = html.lastIndexOf("</script>");
  const doc = {ids: new Map()};
  doc.root = new El(doc, "body");
  doc.root._take(parse(doc, html.slice(html.indexOf("<body"), cut)));
  doc.getElementById = id => doc.ids.get(id) || null;
  doc.querySelectorAll = sel => doc.root.querySelectorAll(sel);
  doc.createElement = tag => new El(doc, tag);

  let now = NOON, seq = 0, rand = rng(seed);
  const dice = rand;
  const timers = new Map();
  const later = (fn, ms, every) => { timers.set(++seq, {fn, at: now + ms, every}); return seq; };
  const server = {rev, save, puts: 0};
  const res = (status, body) => ({ok: status >= 200 && status < 300, status, json: async () => body});
  const fetch = async (url, opt = {}) => {
    ok(served, "파일로 연 화면이 서버를 불렀다: " + url);
    const method = opt.method || "GET";
    if (url === "save" && method === "GET")
      return res(200, server.save ? {rev: server.rev, save: server.save} : {rev: server.rev});
    if (url === "save" && method === "PUT") {
      const b = JSON.parse(opt.body);
      if (b.base !== server.rev) return res(409, {rev: server.rev, save: server.save});
      server.rev++; server.save = b.save; server.puts++;
      return res(200, {rev: server.rev});
    }
    if (url === "update")
      return method === "POST" ? res(200, {ok: true, msg: "올렸다"})
        : res(200, {newer: true, latest: "9.9.9", current: "0.0.1", kind: "pip", cmd: "pip", url: "u"});
    throw new Error("모르는 요청: " + method + " " + url);
  };
  const store = new Map(Object.entries(local));
  const win = {listeners: {}, scrollTo() {}, close() {},
               addEventListener(t, f) { win.listeners[t] = f; }};
  const box = {
    document: doc, window: win, location: {protocol: served ? "http:" : "file:"}, fetch, console,
    localStorage: {getItem: k => store.has(k) ? store.get(k) : null, setItem: (k, v) => store.set(k, String(v))},
    setInterval: (fn, ms) => later(fn, ms, ms), setTimeout: (fn, ms = 0) => later(fn, ms, 0),
    clearInterval: id => timers.delete(id), clearTimeout: id => timers.delete(id),
    requestAnimationFrame: fn => later(fn, 16, 0),
    btoa, atob, __now: () => now, __rand: () => rand(),
  };
  vm.createContext(box);
  vm.runInContext(PRELUDE, box);
  // 줄 번호를 token_rpg.py 에 맞춘다 — 실패한 줄을 그대로 찾아갈 수 있다
  vm.runInContext(html.slice(cut + 8, end) + TAIL, box, {filename: "token_rpg.py", lineOffset: fx.scriptLine - 1});
  await flush();

  const g = {name, doc, server, win, store, t: box.__t, $: id => {
    const e = doc.getElementById(id); ok(e, `[${name}] #${id} 가 화면에 없다`); return e; }};
  g.has = id => !!doc.getElementById(id);
  g.rand = fn => { rand = fn || dice; };          // 난수를 잠깐 바꿔 끼운다 (황금 개체를 부를 때)
  g.advance = async ms => {                      // 시간을 흘려 그사이 타이머를 차례로 돌린다
    const stop = now + ms;
    for (;;) {
      let id = 0, next = null;
      for (const [i, t] of timers) if (t.at <= stop && (!next || t.at < next.at)) { id = i; next = t; }
      if (!next) break;
      now = Math.max(now, next.at);
      if (next.every) next.at += next.every; else timers.delete(id);
      next.fn(); await flush();
    }
    now = stop; await flush();
  };
  g.jump = ms => { now += ms; for (const t of timers.values()) t.at += ms; };   // 타이머는 안 돌리고 시계만
  g.click = async (e, ev) => {
    ok(e && typeof e.onclick === "function", `[${name}] 누를 수 없는 것을 눌렀다: ${e && (e.id || e.tag)}`);
    ok(!e.disabled, `[${name}] 잠긴 버튼을 눌렀다: ${e.id || e.html || e.text}`);
    e.onclick(ev || {target: e}); await flush();
  };
  g.btn = (box, want) => {
    const b = g.$(box).querySelectorAll("button")
      .find(b => Object.entries(want).every(([k, v]) => b.dataset[k] === v));
    ok(b, `[${name}] #${box} 에 ${JSON.stringify(want)} 버튼이 없다`); return b;
  };
  // 그린 글자 전부를 훑는다. NaN·undefined 는 계산이 어딘가에서 끊겼다는 뜻이다
  g.clean = where => {
    for (const e of [doc.root, ...doc.root.all()])
      for (const s of [e.html, e.text, e.value, ...Object.values(e.style)]) {
        const m = /NaN|undefined|Infinity|\[object /.exec(String(s));
        ok(!m, `[${name}] ${where}: 화면에 ${m && m[0]} — ${String(s).slice(Math.max(0, (m ? m.index : 0) - 80), (m ? m.index : 0) + 40)}`);
      }
    const banner = g.$("expedBanner").html;
    ok(!/못했다/.test(banner), `[${name}] ${where}: 저장 실패 알림이 떴다 — ${banner}`);
  };
  g.fight = async stage => {                     // 스테이지 버튼을 눌러 전투가 끝날 때까지 본다
    const row = g.$("stages").kids[stage];
    await g.click(row.kids.find(k => k.tag === "button"));
    ok(g.$("fight").classList.contains("on"), `[${name}] 전투 창이 안 열렸다`);
    for (let i = 0; i < 420 && g.$("close").disabled; i++) await g.advance(380);
    ok(!g.$("close").disabled, `[${name}] 전투가 끝나지 않는다`);
    g.clean("전투");
    await g.click(g.$("close"));
    ok(!g.$("fight").classList.contains("on"), `[${name}] 전투 창이 안 닫혔다`);
  };
  g.clean("첫 화면");
  return g;
}

// ── 파이썬과 두 벌인 식. 한쪽만 고치면 화면과 밸런스 표·메뉴 막대 배지가 어긋난다
function parity(g) {
  const {t} = g, e = fx.expect;
  for (const [n, want] of e.boss)
    // 거듭제곱은 파이썬(C pow)과 JS(Math.pow)가 마지막 자리에서 다를 수 있다 — 10^15 에서 1 차이. 상대 1e-12 까지 같다고 본다
    ok(["hp", "atk", "dfn", "spd"].every(k => Math.abs(t.boss(n)[k] - want[k]) <= 1e-12 * Math.max(1, Math.abs(want[k]))) && t.boss(n).g === n,
       `보스 ${n}: 화면 ${JSON.stringify(t.boss(n))} · 파이썬 ${JSON.stringify(want)}`);
  for (const [n, want] of e.slots) {             // 층 테마 — 이름·이모지·테마와 칸 고정값
    const s = t.slotOf(n);
    for (const k of Object.keys(want))
      ok(s[k] === want[k], `${n}스테이지 칸의 ${k}: 화면 ${s[k]} · 파이썬 ${want[k]}`);
  }
  // 큰 수의 단위: T 다음에도 단위가 있어야 한다. 없으면 12,500T 처럼 끝없이 길어져 칸을 넘는다
  for (const [x, want] of [[0, "0"], [999, "999"], [999.6, "1000"], [1000, "1K"], [1250, "1.25K"], [12500, "12.5K"], [125000, "125K"],
                           [999499, "999K"], [999500, "1M"], [1.25e12, "1.25T"], [9.99e14, "999T"], [9.996e14, "1e15"], [1e15, "1e15"], [1.25e16, "1.25e16"],
                           [3.4e18, "3.4e18"], [9.999e20, "1e21"], [5e24, "5e24"], [7.77e27, "7.77e27"], [1e30, "1e30"], [9.99e35, "9.99e35"],
                           [1e36, "1e36"], [4.56e40, "4.56e40"], [-2500, "-2.5K"], [-3e17, "-3e17"], [1e3 - 1e-9, "1000"]])
    ok(t.n(x) === want, `숫자 줄이기 ${x}: ${t.n(x)} — ${want} 이어야 한다`);
  for (let p = 3; p < 99; p += 0.37) ok(t.n(Math.pow(10, p)).length <= 7, `10^${p.toFixed(2)} 이 ${t.n(Math.pow(10, p))} 로 길다`);
  e.exp.forEach((want, i) => ok(t.expFor(i + 1) === want, `Lv.${i + 1} EXP: 화면 ${t.expFor(i + 1)} · 파이썬 ${want}`));
  for (const [x, want] of e.levels) ok(t.levelOf(x) === want, `EXP ${x}: 화면 Lv.${t.levelOf(x)} · 파이썬 Lv.${want}`);
  e.cost.forEach((want, lv) => ok(t.costOf(lv) === want, `특성 ${lv}레벨 비용: 화면 ${t.costOf(lv)} · 파이썬 ${want}`));
  // 혼은 환생·유물·수확·업적 배수가 없는 새 저장에서 잰다 (픽스처의 사용 기록으로 '3일 연속' 업적이 이미 달성돼 있다)
  const keepAch = t.save.ach; t.save.ach = {};
  for (const [n, want] of e.soul) ok(t.soulOf(n) === want, `혼 ${n}스테이지: 화면 ${t.soulOf(n)} · 파이썬 ${want}`);
  for (const [n, want] of e.side) ok(t.sideSoul(n) === want, `보조 수입 혼 ${n}스테이지: 화면 ${t.sideSoul(n)} · 파이썬 ${want}`);
  t.save.ach = keepAch;
  ok(t.lvNow() === e.hero.level && t.points() === e.hero.points,
     `레벨·배분: 화면 Lv.${t.lvNow()} ${t.points()}pt · 파이썬 Lv.${e.hero.level} ${e.hero.points}pt`);
  const keep = t.save;
  for (const b of e.builds) {
    t.save = Object.assign(t.fresh(), {alloc: {...t.fresh().alloc, ...b.alloc}, traits: {...t.fresh().traits, ...b.traits}});
    for (const [k, want] of Object.entries(b.stats))
      ok(same(t.F(k), want), `스탯 ${k} (${JSON.stringify(b.alloc)} ${JSON.stringify(b.traits)}): 화면 ${t.F(k)} · 파이썬 ${want}`);
    let wall = 1;
    while (wall < 400 && t.beatable(t.boss(wall))) wall++;
    ok(wall === b.wall, `벽 (${JSON.stringify(b.alloc)} ${JSON.stringify(b.traits)}): 화면 ${wall} · 파이썬 ${b.wall}`);
  }
  t.save = keep;
}

// ── 새 설치에서 한 바퀴: 배분 → 전투 → 슬롯 → 미션 → 원정 → 환생 → 자동 도전 → 특성
async function playthrough() {
  const g = await boot("한 바퀴"), {t, server} = g;
  ok(t.save.rbExp === t.H.exp, "새 저장의 환생 기운 기준이 지금 EXP 가 아니다");
  ok(server.puts >= 1, "첫 화면에서 저장을 한 번도 안 썼다");
  parity(g);

  await g.click(g.$("updBtn"));
  ok(g.$("updMsg").text === "올렸다", "업데이트 버튼이 결과를 안 적는다");

  // 탭: 처음엔 전투 탭. 배분 점수가 남으면 성장 탭에 점이 뜨고, 다 쓰면 사라진다
  const pane = k => g.$("tabs") && doc_pane(g, k);
  ok(!pane("fight").classList.contains("off") && pane("grow").classList.contains("off"), "처음 탭이 전투가 아니다");
  ok(t.left() > 0 && g.$("dot-grow").classList.contains("on"), "배분 점수가 남았는데 성장 탭에 점이 없다");
  await g.click(tab_btn(g, "grow"));
  ok(pane("fight").classList.contains("off") && !pane("grow").classList.contains("off") && tab_btn(g, "grow").classList.contains("on"),
     "성장 탭을 눌러도 안 바뀐다");
  await g.click(g.btn("alloc", {k: "atk", d: "max"}));
  ok(t.left() === 0 && t.save.alloc.atk === t.points(), "'최대' 가 남은 포인트를 다 안 넣는다");
  ok(!g.$("dot-grow").classList.contains("on"), "배분을 다 했는데 성장 탭의 점이 안 꺼진다");
  await g.click(tab_btn(g, "fight"));
  await g.click(g.btn("alloc", {k: "atk", d: "-1"}));
  await g.click(g.btn("alloc", {k: "hp", d: "1"}));
  ok(t.left() === 0 && t.save.alloc.hp === 1, "−1 · +1 이 한 점씩 안 옮긴다");

  ok(/1층 코드 버그/.test(g.$("floorTitle").text), "층 제목에 테마가 없다: " + g.$("floorTitle").text);
  ok(/2층 첫 보스/.test(g.$("raid").html) && !g.$("raid").querySelectorAll("button").length, "레이드가 2층 전에 열려 있다");
  ok(/5층을 다 깨면/.test(g.$("mutHelp").html) && !g.$("muts").html && !g.$("mutNote").html, "변종 층이 열리기 전인데 변이 내성이 보인다");
  await g.fight(0);
  ok(t.save.cleared.includes(1) && t.save.best === 1, "1스테이지를 이겼는데 기록이 안 남았다");

  // 도감: 이긴 보스는 윗줄에 찍힌다
  ok(t.save.dex["0:1"] === 1 && /^1\/75 · 황금 0\/75$/.test(g.$("dexCount").text), "이긴 보스가 도감에 안 찍혔다: " + g.$("dexCount").text);
  // 황금 개체: 난수를 0 으로 눌러 부른다. 이름과 이모지만 다르고, 이기면 아랫줄에 찍히고 혼을 준다
  g.rand(() => 0);
  let had = t.save.souls;
  const pay = t.sideSoul(2) * t.K.rareMul;
  await g.fight(1);
  g.rand();
  ok(g.$("fbn").text === "황금 무한 루프 뱀" && g.$("fb").text.endsWith("✨"), "황금 개체가 이름·이모지를 안 바꿨다: " + g.$("fbn").text);
  ok(t.save.dex["0:2"] === 2 && t.save.souls === had + pay, `황금 포획이 도감·혼에 안 들어갔다 (${had} + ${pay} -> ${t.save.souls})`);
  ok(/황금 무한 루프 뱀 포획/.test(g.$("dexNews").html) && /도감 \+0\.5%/.test(g.$("rbBonus").html), "포획 알림이나 혼 배수 표시가 없다");
  had = t.save.souls;
  ok(/이미 도감에/.test(t.catchRare(2)) && t.save.souls === had, "이미 잡은 황금이 혼을 또 준다 — 켜 두기만 해도 혼이 쌓인다");
  // 한 층의 황금 15종을 다 채우면 배분 포인트가 는다
  const pts = t.points(), keep = t.save.dex;
  t.save.dex = {...keep, ...Object.fromEntries(Array.from({length: 15}, (_, i) => ["0:" + (i + 1), 2]))};
  ok(t.points() === pts + t.K.dexPt, "한 층 완성이 배분 포인트를 안 준다");
  t.drawAll(); g.clean("도감 한 층 완성");
  ok(/배분 \+4pt/.test(g.$("dex").html), "완성한 층에 보상 표시가 없다");
  t.save.dex = keep; t.drawAll();
  ok(t.points() === pts, "도감을 되돌렸는데 포인트가 남았다");

  await g.click(g.$("slotBtn"));
  await g.advance(100);                           // 버튼은 첫 틱에 잠긴다
  ok(g.$("slotBtn").disabled, "도는 중에 슬롯 버튼이 열려 있다");
  await g.advance(1500);
  ok(t.save.slot.n === 1 && !g.$("slotBtn").disabled, "슬롯 한 판이 안 끝났다");
  g.clean("슬롯");

  // 혼 사냥은 v0.18.5 에 없앴다 — 그 몫(하루 3 격파분)이 슬롯으로 갔다
  ok(!g.has("mini") && !g.has("miniBtn") && t.K.slotBudget === 4 && !("mini" in t.save), "혼 사냥이 남아 있거나 슬롯 예산이 안 늘었다");
  ok(Number.isInteger(t.save.souls) && t.save.souls >= 0, "혼이 정수가 아니다: " + t.save.souls);

  const due = t.missions().filter(m => m.cur >= m.goal);
  ok(due.length >= 2 && due.length < t.missions().length, "미션 픽스처가 '일부만 달성' 이 아니다");
  let before = t.save.souls;
  for (const m of due) await g.click(g.btn("missions", {k: m.key}));
  ok(due.every(m => t.save.claimed[m.key]) && t.save.souls === before + due.reduce((s, m) => s + m.souls, 0),
     "미션 보상이 적힌 만큼 안 들어왔다");
  ok(g.$("missions").querySelectorAll("button[data-k]").every(b => b.disabled), "받은 미션을 또 받을 수 있다");

  g.jump(9 * 3600 * 1000);                        // 상한(8시간)을 넘겨 가득 채운다
  t.drawAll(); before = t.save.souls;
  ok(/가득 참/.test(g.$("exped").html), "원정이 상한에서 가득 찼다고 안 알린다");
  await g.click(g.$("claim"));
  ok(t.save.souls > before && t.save.exped.seenExp === t.H.exp, "원정 수령이 혼을 안 준다");

  t.save.rbExp = t.H.exp - 2 * t.K.rbExp;         // 기운 두 개 — 토큰을 200만 더 쓴 셈
  t.drawAll(); before = t.save.souls;
  const alloc = JSON.stringify(t.save.alloc);
  await g.click(g.$("rbBtn"));
  ok(t.save.rebirths === 0, "환생이 한 번 눌러 실행됐다");
  await g.click(g.$("rbBtn"));
  ok(t.save.rebirths === 1 && !t.save.cleared.length && t.save.souls > before, "환생이 정산을 안 했다");
  ok(JSON.stringify(t.save.alloc) === alloc, "환생했는데 배분이 풀렸다");
  g.clean("환생");

  await g.advance(6000);
  ok(t.save.cleared.length >= 3, "자동 도전이 안 오른다: " + JSON.stringify(t.save.cleared));
  await g.click(g.$("pickAll"));
  ok(t.picks().length === t.save.best, "'전체 반복' 이 다 고르지 않는다");
  await g.click(g.$("picks"), {target: g.$("auto").all().find(e => e.dataset.g === "1")});
  ok(!t.picks().includes(1), "고른 칸을 다시 눌러도 안 빠진다");
  await g.click(g.$("pickAll")); await g.click(g.$("pickAll"));
  ok(!t.picks().length, "'전체 해제' 가 비우지 않는다");
  // 층 머리줄: 누르면 그 층에서 깬 스테이지를 통째로 고른다. 1층은 깬 데까지만이다
  const head = f => g.$("auto").all().find(e => e.dataset.f === String(f));
  await g.click(g.$("picks"), {target: head(1)});
  ok(t.picks().length === t.save.best && t.save.best < 15 && /전부 고름/.test(g.$("auto").html), "층 머리줄이 깬 스테이지를 다 고르지 않는다: " + t.picks());
  await g.click(g.$("picks"), {target: head(1)});
  ok(!t.picks().length, "층 머리줄을 다시 눌러도 안 빠진다");
  // 못 모은 곳만: 유물을 더 올릴 수 있거나 황금이 없는 칸마다 하나씩. 칸에는 ◆·★ 가 찍힌다
  // 유물 깊이 (v0.20.4): 떨어진 층마다 +25%. 13층 신화 ATK = 48 x 4. 혼 유물은 깊이와 상관없다. g 없는 옛 유물은 1층
  ok(t.relicVal({r: 4, a: "atk", g: 181}) === 192 && t.relicVal({r: 4, a: "soul", g: 181}) === 48 && t.relicVal({r: 4, a: "atk"}) === 48,
     "유물 깊이 배율이 틀렸다: " + [t.relicVal({r: 4, a: "atk", g: 181}), t.relicVal({r: 4, a: "soul", g: 181}), t.relicVal({r: 4, a: "atk"})]);
  const want = t.wantPicks();
  ok(want.length > 0 && want.every(x => t.relicLeft(x) || t.goldLeft(x)), "'못 모은 곳만' 이 다 모은 칸을 고른다: " + want);
  await g.click(g.$("pickWant"));
  ok(t.picks().join() === want.join(), "'못 모은 곳만' 버튼이 그 칸들을 안 고른다");
  ok(/★/.test(g.$("auto").html), "반복 칸에 황금 미포획 표시가 없다");
  ok(/유물 강화 \d+|황금 \d+/.test(g.$("auto").html), "모으기 버튼이 무엇을 모으는지 안 적는다");
  await g.click(g.$("pickAll")); await g.click(g.$("pickAll"));
  await g.advance(3000);
  g.clean("자동 도전");
  // 이번 판에 나온 유물은 떨어뜨린 스테이지를 적어 둔다 — 도감이 그 보스 이름을 보여 준다
  const got = Object.entries(t.save.relics);
  ok(got.length >= 1, "픽스처에서 유물이 하나도 안 나왔다 — 난수 씨앗을 바꿔라");
  for (const [k, r] of got) {
    ok(Number.isInteger(r.g) && k === "boss" + t.slotOf(r.g).slot, `유물 ${k} 에 떨어뜨린 스테이지가 없다: ${JSON.stringify(r)}`);
    ok(g.$("relics").html.includes(t.slotOf(r.g).name), `도감에 ${t.slotOf(r.g).name} 이 없다`);
  }

  t.save.souls += 1e6; t.drawAll();
  for (const k of Object.keys(t.fresh().traits).filter(k => k !== "crit")) {
    const lv = t.save.traits[k], had = t.save.souls;
    await g.click(g.btn("traits", {t: k, c: "1"}));
    ok(t.save.traits[k] === lv + 1 && t.save.souls === had - t.costOf(lv), `특성 ${k} 구입이 어긋났다`);
  }
  await g.click(g.btn("traits", {t: "atk", c: "max"}));
  ok(t.save.souls >= 0 && t.save.souls < t.costOf(t.save.traits.atk), "'최대' 구입이 혼을 남겼다");
  g.clean("특성");
  await g.advance(5000);

  await g.click(g.$("saveShow"));
  const code = g.$("saveBox").value;
  ok(t.validSave(t.decodeSave(code)), "방금 뽑은 저장 코드를 스스로 거부한다");
  ok(t.decodeSave(code.replace(/\.\w+$/, ".zzzz")) === null, "체크섬이 틀린 저장 코드를 받는다");
  ok(!t.validSave({...t.decodeSave(code), dex: {"0:1": 3}}), "도감에 없는 값(3)이 든 저장을 받는다");
  await g.click(g.$("saveLoad")); await g.click(g.$("saveLoad"));
  ok(/^가져왔다/.test(g.$("saveMsg").text), "저장 코드 가져오기가 안 끝났다: " + g.$("saveMsg").text);

  await g.click(g.$("reset"));
  ok(t.left() === t.points(), "배분 초기화가 포인트를 다 안 돌려준다");
  ok(JSON.stringify(server.save) === JSON.stringify(t.save), "화면의 저장과 서버의 저장이 다르다");
  g.clean("끝");

  // 다른 창이 먼저 저장했다 — 내 쓰기는 거부되고 그쪽 저장을 따른다
  server.rev++; server.save = {...server.save, souls: 777};
  await g.click(g.btn("alloc", {k: "hp", d: "1"}));
  ok(t.save.souls === 777 && t.save.alloc.hp === 0 && /다른 창/.test(g.$("expedBanner").html),
     "오래된 창이 최신 저장을 덮었다");
  server.save = {...server.save, souls: 888};
  g.win.listeners.focus(); await flush();
  ok(t.save.souls === 888, "창으로 돌아왔는데 최신 저장을 안 받는다");
  g.clean("충돌 뒤");
}

// ── 옛 저장: 칸이 모자라고(cdmg·spd 없음), 없어진 특성(예지)과 옛 유물 키가 남아 있다
async function oldSave() {
  const g = await boot("옛 저장", {rev: 5, seed: 2, save: {
    alloc: {atk: 5, hp: 0, dfn: 0, crit: 0}, cleared: [1, 2, 3], souls: 10, rebirths: 2,
    traits: {atk: 1, hp: 0, dfn: 0, crit: 3}, farm: 2, bless: {x: 1},
    exped: {since: NOON - 2 * 3600 * 1000, seenExp: 0}, relics: {"claude-code|proj": {r: 1, a: "hp"}}}});
  const {t} = g;
  ok(t.save.traits.crit === 0 && t.save.souls === 10 + t.costOf(0) + t.costOf(1) + t.costOf(2),
     "없앤 특성 '예지' 에 쓴 혼을 전액 돌려주지 않았다");
  ok(t.save.best === 3, "옛 저장의 최고 기록을 옮기지 않았다");
  ok(t.save.relics.boss1 && t.save.relics.boss1.a === "atk" && !t.save.relics["claude-code|proj"],
     "옛 유물을 고정 보스 칸으로 옮기지 않았다");
  ok(JSON.stringify(t.picks()) === "[2]", "숫자 하나이던 반복 스테이지를 못 읽는다");
  ok(g.$("relics").html.includes("버그 벌레"), "스테이지가 안 적힌 옛 유물을 1층 이름으로 못 적는다");
  await g.advance(5000);
  g.clean("자동 도전");
}

// ── 깊은 저장: 숫자가 억·조 단위이고 한 턴에 수천 번 친다
async function deepSave() {
  const relics = {};
  fx.affix.forEach((a, i) => { relics["boss" + (i + 1)] = {r: 4, a}; });
  const g = await boot("깊은 저장", {rev: 9, seed: 3, save: {
    alloc: {atk: 20, hp: 10, dfn: 10, crit: 5, cdmg: 5, spd: 5}, souls: 123456789012, rebirths: 40,
    cleared: Array.from({length: 47}, (_, i) => i + 1), best: 120, auto: true, farm: [3, 17, 46],
    traits: {atk: 70, hp: 70, dfn: 60, crit: 0, cdmg: 50, spd: 90, soul: 30, pt: 20},
    exped: {since: NOON - 30 * 3600 * 1000, seenExp: 1}, relics, rbExp: 1,
    mini: {day: fx.today, n: 3}, slot: {day: fx.today, n: 10}}});   // mini = 없어진 혼 사냥 칸 (치워야 한다)
  const {t} = g;
  const [dmg] = t.turnDmg({atk: t.F("atk"), cdmg: t.F("cdmg")}, {dfn: 5}, 5000, t.F("crit"));
  ok(Number.isFinite(dmg) && dmg > 0, "타격이 많을 때 피해가 숫자가 아니다: " + dmg);
  // 도감이 없던 저장: 이미 깬 스테이지(최고 120)의 보스 75종은 본 것으로 찍는다
  ok(Object.keys(t.save.dex).length === 75 && /^75\/75 · 황금 0\/75$/.test(g.$("dexCount").text),
     "옛 저장의 도감을 채우지 않았다: " + g.$("dexCount").text);
  // 후반 배율은 환생 정산에만 붙는다. 최고 120스테이지면 정산용 혼이 보조 수입용의 천 배가 넘는다 —
  // 보조 수입이 정산용 혼을 쓰면 원정·미션·슬롯이 환생보다 많이 번다 (v0.14.11 이 그랬다)
  const side = t.sideSoul(120);
  ok(t.soulOf(120) > side * 2 && side > Math.pow(120, t.K.soulExp) * 20,
     "보조 수입용 혼이 환생만큼 못 자랐거나(후반에 죽는다) 정산용만큼 자랐다(환생을 넘어선다)");
  ok(t.idleRate() > 0 && t.idleRate() <= side / t.K.idleDiv * (1 + t.K.idleTokenMax) * 1.0001, "원정이 보조 수입용 혼을 안 쓴다");
  ok(t.slotPay(1) === Math.round(side * t.K.slotBudget / (t.K.slotTries * t.K.slotAvg)), "슬롯이 보조 수입용 혼을 안 쓴다");
  ok(!("mini" in t.save), "옛 저장의 혼 사냥 칸을 안 치웠다");
  ok(t.missions().every(m => m.souls > 0 && m.souls <= side * t.K.weekBudget * 2), "미션이 보조 수입용 혼을 안 쓴다");
  // 유물 도감은 지금 층(4층)의 보스 이름과 번호로 적는다 — 던전 목록과 짝이 맞아야 한다
  for (let i = 1; i <= 15; i++)
    ok(g.$("relics").html.includes(`${45 + i}. ${t.slotOf(45 + i).name}`), `유물 도감에 ${45 + i}. ${t.slotOf(45 + i).name} 이 없다`);
  ok(!g.$("relics").html.includes("버그 벌레") && /지금 4층/.test(g.$("relicNews").html), "4층인데 유물 도감이 1층 이름을 쓴다");
  // 층별 반복: 고른 것이 [3, 17, 46] 인 저장에서 2층 머리줄을 누르면 16~30 이 다 들어가고 다른 층은 그대로다
  const head = f => g.$("auto").all().find(e => e.dataset.f === String(f));
  ok(g.$("auto").all().filter(e => e.dataset.f).length === 8 && /2층 인프라 장애/.test(g.$("auto").html), "층 머리줄이 여덟 층에 하나씩이 아니다");
  await g.click(g.$("picks"), {target: head(2)});
  ok(t.picks().join() === [3, ...Array.from({length: 15}, (_, i) => 16 + i), 46].join(), "2층 머리줄이 16~30 을 고르지 않았다: " + t.picks());
  await g.click(g.$("picks"), {target: head(2)});
  ok(t.picks().join() === "3,46", "2층을 뺐는데 다른 층의 선택이 바뀌었다: " + t.picks());
  // 층마다 접고 편다 — 처음에는 가장 깊은 층(8층)만 펼쳐져 있다. 접힌 층도 머리줄로는 고를 수 있다(위에서 2층을 접힌 채 골랐다)
  const chip = n => g.$("auto").all().find(e => e.dataset.g === String(n));
  const fold = f => g.$("auto").all().find(e => e.dataset.x === String(f));
  ok(!chip(17) && chip(106) && fold(2) && fold(8), "처음부터 가장 깊은 층만 펼쳐져 있지 않다");
  await g.click(g.$("picks"), {target: fold(2)});
  ok(chip(17) && chip(30) && !chip(31), "2층을 펼쳤는데 칸이 안 보인다");
  await g.click(g.$("picks"), {target: chip(17)});
  ok(t.picks().join() === "3,17,46", "칸 하나 고르기가 안 된다");
  ok(chip(17), "칸을 골랐더니 펼친 층이 접혔다");
  await g.click(g.$("picks"), {target: fold(2)});
  ok(!chip(17) && t.picks().join() === "3,17,46", "2층을 접었는데 칸이 남았거나 선택이 바뀌었다");
  // 도감은 5층에서 끝나고 변종은 같은 칸을 쓴다 — 화면에 적혀 있어야 하고, 지금 층이 어느 줄인지 보여야 한다
  ok(/도감은 5층까지, 보스 75종이 끝이다/.test(g.$("dex").html) && /6층은 1층 줄/.test(g.$("dex").html), "도감이 어디서 끝나는지 적혀 있지 않다");
  ok(/4층 회사 <span class="gold">← 지금 4층<\/span>/.test(g.$("dex").html), "지금 층이 도감의 어느 줄인지 안 보인다");
  const run = t.save.cleared;
  t.save.cleared = Array.from({length: 80}, (_, i) => i + 1); t.drawAll();       // 6층 = 변종 코드 버그
  ok(/1층 코드 버그 <span class="gold">← 지금 6층 \(변종\)<\/span>/.test(g.$("dex").html) && /6층 변종 코드 버그/.test(g.$("floorTitle").text),
     "6층(변종)이 도감의 1층 줄을 쓴다는 표시가 없다");
  g.clean("6층");
  t.save.cleared = run; t.drawAll();
  ok(/4층 회사/.test(g.$("floorTitle").text), "4층 제목이 테마를 안 따른다: " + g.$("floorTitle").text);
  ok(g.$("stages").kids[0].html.includes(t.slotOf(46).name) && t.slotOf(46).name === "급한 건 모기",
     "4층 첫 칸이 테마 보스가 아니다");
  ok(g.$("auto").html.includes(t.slotOf(106).name) && /2층 인프라 장애/.test(g.$("auto").html), "반복 고르기 칸이 층 테마 이름을 안 쓴다");
  await raid(g);
  await g.advance(8000);
  g.clean("자동 도전");
  await g.fight(2);
  ok(g.$("fbn").text === t.slotOf(48).name, "전투 창의 보스 이름이 테마를 안 따른다: " + g.$("fbn").text);
  await g.click(g.$("slotBtn")); await g.advance(1500);
  ok(t.save.slot.n === 10 && /연습판/.test(g.$("slot").html), "하루 몫을 다 쓴 슬롯이 연습판으로 안 돈다");
  g.clean("연습판");
}

// ── 주간 레이드: 이번 주에 쓴 토큰이 보스 HP 를 깎는다. 픽스처는 목요일이고, 지난주(월~토 6일 x 200만)가
// 유일하게 쓴 주라 HP 가 1,200만이다. 이번 주는 월~수 200만씩 + 오늘 800만 = 1,400만 — 격파다.
// 이번 주와 지난주의 합이 다르다. 같으면 HP 에 이번 주가 섞여도 값이 같아 못 잡는다.
async function raid(g) {
  const {t} = g;
  ok(t.raidWk() === "2026-01-12" && t.raidHp() === 12e6 && t.weekSum(0) === 14e6 && t.weekSum(1) === 12e6,
     `레이드 계산이 어긋났다: ${t.raidWk()} HP ${t.raidHp()} 이번 주 ${t.weekSum(0)} 지난주 ${t.weekSum(1)}`);
  ok(g.$("raidHead").text === "격파", "HP 를 다 깎았는데 격파로 안 뜬다: " + g.$("raidHead").text);
  const pay = t.raidPay();
  ok(pay === Math.round(t.sideSoul(120) * t.K.raidBudget / 4), "레이드 보상이 보조 수입용 혼의 예산 1/4 이 아니다");
  // 격파 보상 (v0.20.5): 가장 약한 유물 하나를 등급 +1 또는 '지금 층으로 옮기기' 중 더 센 쪽으로 강화한다.
  // 다른 14칸은 가장 깊은 층 신화로 채워 두고, 3번 칸만 3층 희귀로 약하게 둔다
  const all = t.save.relics, deep = sl => Math.floor(Math.max(0, t.save.best - sl) / 15) * 15 + sl;
  const maxed = Object.fromEntries(Object.entries(all).map(([k, r]) => [k, {...r, r: 4, g: deep(+k.slice(4))}]));
  t.save.relics = {...maxed, boss3: {r: 1, a: "dfn", g: 33}};
  t.drawAll();
  const card = g.$("raid").html;
  ok(/격파하면 강화할 유물/.test(card) && /48\. 문서 없음 안개/.test(card) && /지금: 희귀 · 3층 · DEF \+9% → 강화 후: <b>희귀 · 8층 · DEF \+16\.5%<\/b>/.test(card)
     && /어떻게: 이 유물을 얻을 수 있는 가장 깊은 층\(8층\)으로 옮긴다 — 등급은 그대로/.test(card) && /왜: 15칸 중 가장 약하다 — 이 칸 최고치\(신화 · 8층 · DEF \+132%\)의 7%/.test(card),
     "레이드 카드에 강화 대상·전후·이유가 안 보인다: " + card.slice(-700));
  for (let i = 0; i < 4; i++) {
    const had = t.save.souls;
    await g.click(g.btn("raid", {r: String(i)}));
    ok(t.save.raid.got.includes(i) && t.save.souls === had + pay, `레이드 ${i + 1}단계 보상이 적힌 것과 다르다`);
  }
  // 3층 희귀 DEF(+9%) -> 먼저 가장 깊은 층(8층)으로 옮긴다: 희귀 +16.5% (v0.20.6)
  ok(t.save.relics.boss3.r === 1 && t.save.relics.boss3.a === "dfn" && t.save.relics.boss3.g === deep(3),
     "격파했는데 가장 약한 유물이 강화되지 않았다: " + JSON.stringify(t.save.relics.boss3));
  ok(Object.entries(t.save.relics).filter(([k]) => k !== "boss3").every(([k, r]) => r.r === 4 && r.g === deep(+k.slice(4))), "다른 유물을 건드렸다");
  ok(/격파 — 혼/.test(g.$("raid").html) && /가장 약한 유물 강화 — .*48\. 문서 없음 안개[\s\S]*지금: 희귀 · 3층 · DEF \+9% → 강화 후: <b>희귀 · 8층 · DEF \+16\.5%/.test(g.$("raid").html) && !/격파하면 강화할 유물/.test(g.$("raid").html), "격파 알림에 강화가 없다: " + g.$("raid").html.slice(0, 300));
  // 얕은 층 신화는 등급을 못 올리니 지금 층으로 옮긴다 — 깊은 층 유물에 밀려 사라지지 않게
  t.save.relics = {...maxed, boss3: {r: 4, a: "dfn", g: 3}};
  ok(/지금: 신화 · 1층 · DEF \+48% → 강화 후: <b>신화 · 8층 · DEF \+132%<\/b>[\s\S]*가장 깊은 층\(8층\)으로 옮긴다 — 등급은 그대로/.test(t.raidRelic()) && t.save.relics.boss3.r === 4 && t.save.relics.boss3.g === deep(3), "얕은 신화를 지금 층으로 옮기지 않는다: " + JSON.stringify(t.save.relics.boss3));
  // 이미 가장 깊은 층이면 등급 +1
  t.save.relics = {...maxed, boss3: {r: 1, a: "dfn", g: deep(3)}};
  ok(/희귀 · 8층 · DEF \+16\.5% → 강화 후: <b>영웅 · 8층 · DEF \+33%<\/b>[\s\S]*등급 \+1 — 이미 가장 깊은 층\(8층\)이다/.test(t.raidRelic()) && t.save.relics.boss3.r === 2,
     "가장 깊은 층 유물의 등급을 안 올린다: " + JSON.stringify(t.save.relics.boss3));
  // 가장 깊은 층(8층)과 한 층 차이(7층)면 옮기지 않고 등급 +1. 이미 신화면 한 층이라도 옮긴다
  t.save.relics = {...maxed, boss3: {r: 1, a: "dfn", g: 93}};
  ok(/희귀 · 7층 · DEF \+15% → 강화 후: <b>영웅 · 7층 · DEF \+30%<\/b>[\s\S]*한 층 차이라/.test(t.raidRelic()) && t.save.relics.boss3.g === 93,
     "한 층 차이인데 등급 대신 옮겼다: " + JSON.stringify(t.save.relics.boss3));
  t.save.relics = {...maxed, boss3: {r: 4, a: "dfn", g: 93}};
  ok(/신화 · 7층 · DEF \+120% → 강화 후: <b>신화 · 8층/.test(t.raidRelic()) && t.save.relics.boss3.g === deep(3), "한 층 차이 신화를 안 옮긴다");
  // 빈 칸이 가장 약하다 — 유물이 없으면 하나를 준다. 전부 가장 깊은 층 신화면 혼을 준다
  t.save.relics = {};
  ok(/유물/.test(t.raidRelic()) && Object.keys(t.save.relics).length === 1, "유물이 하나도 없을 때 하나를 주지 않는다");
  t.save.relics = maxed;
  const full = t.save.souls;
  ok(/모두 가장 깊은 층 신화/.test(t.raidRelic()) && t.save.souls === full + t.sideSoul(120), "다 찼을 때 혼을 주지 않는다");
  ok(JSON.stringify(t.save.relics) === JSON.stringify(maxed), "다 찼는데 유물이 바뀌었다");
  t.save.relics = all;
  t.drawAll();
  ok(g.$("raid").querySelectorAll("button[data-r]").every(b => b.disabled), "받은 레이드 보상을 또 받을 수 있다");
  ok(t.save.raid.wk === "2026-01-12" && t.save.raid.got.join() === "0,1,2,3", "받은 단계를 저장하지 않았다");
  const twice = t.save.souls;
  t.raidClaim(0); t.raidClaim(3);
  ok(t.save.souls === twice && t.save.raid.got.length === 4, "받은 레이드 단계를 다시 받을 수 있다");
  // 주가 바뀌면 다음 보스로 넘어간다 — 지난주에 받은 단계는 이번 주와 무관하다
  const boss = g.$("raid").kids[1] ? g.$("raid").html : "";
  g.jump(7 * 86400 * 1000); t.drawAll();
  ok(t.raidWk() === "2026-01-19" && g.$("raid").querySelectorAll("button[data-r]").every(b => b.dataset.r === "0" || b.disabled),
     "주가 바뀌었는데 레이드가 안 넘어갔다");
  ok(g.$("raid").html !== boss && g.$("raidHead").text !== "격파", "새 주의 보스가 이미 잡혀 있다");
  ok(!/받음/.test(g.$("raid").html) && !/✓/.test(g.$("raid").html), "지난주에 받은 단계가 새 주에도 받은 것으로 뜬다");
  g.clean("레이드 다음 주");
  g.jump(-7 * 86400 * 1000); t.drawAll();
  ok(!t.validSave({...t.save, raid: {wk: "x", got: [7]}}), "레이드에 없는 단계(7)가 든 저장을 받는다");
  g.clean("레이드");
}

// ── 업적: 저장에 있는 기록으로 바로 달성되고, 셈은 이긴 판에서 오르고, 칭호를 달 수 있다
async function achievements() {
  const g = await boot("업적", {rev: 3, seed: 11, save: {
    alloc: {atk: 20, hp: 10, dfn: 10, crit: 5, cdmg: 5, spd: 5}, souls: 5, rebirths: 12, auto: false,
    cleared: Array.from({length: 92}, (_, i) => i + 1), best: 92,
    traits: {atk: 80, hp: 80, dfn: 70, crit: 0, cdmg: 60, spd: 0, soul: 0, pt: 10},
    exped: {since: NOON, seenExp: 1}, relics: {}, rbExp: 1, st: {kills: 998, losses: 99}}});
  const {t} = g, got = () => t.save.ach || {};
  // 92스테이지 = 7층, 관문 하나(90), 환생 12번 — 업데이트하자마자 달성. 10층·환생 50번은 아직
  ok(got().floor3 && got().floor6 && !got().floor10 && got().reb10 && !got().reb50 && got().gate1 && !got().gate5,
     "저장에 있는 기록으로 업적이 바로 달성되지 않는다: " + JSON.stringify(got()));
  ok(t.ACH.length * t.K.achSoul <= 10, "업적을 다 모은 혼 보너스가 +10% 를 넘는다");
  const base = Object.keys(got()).length;
  ok(Math.abs(t.achSoul() - base * t.K.achSoul) < 1e-9 && /업적 \+/.test(g.$("rbBonus").html), "업적 혼 보너스가 얻는 혼에 안 붙는다");
  // 이긴 판이 쌓여 1,000마리 — 두 판이면 달성된다. 패배 100번은 숨은 업적이라 그전에는 ??? 로 보인다
  ok(/숨은 업적/.test(g.$("ach").html) && !/넘어져도 일어서는 자/.test(g.$("ach").html), "숨은 업적이 미리 보인다");
  t.winStage(5); t.winStage(5); t.save.st.losses = 100; t.drawAll();
  ok(got().kill1k && got().lose100 && /넘어져도 일어서는 자/.test(g.$("ach").html) && /업적 달성/.test(g.$("achNews").html),
     "처치 1,000마리·패배 100번이 달성되지 않았다: " + JSON.stringify(t.save.st));
  // 칭호를 달고 뗀다
  const tb = () => g.btn("ach", {t: "floor6"});
  await g.click(tb());
  ok(t.save.title === "floor6" && /변종 사냥꾼/.test(g.$("achTitle").text), "칭호가 이름 위에 안 붙는다: " + g.$("achTitle").text);
  await g.click(tb());
  ok(t.save.title === "" && !g.$("achTitle").text, "칭호를 뗄 수 없다");
  // 하루에 황금 둘
  t.catchRare(1); t.catchRare(2); t.drawAll();
  ok(got().gold1 && got().gold2, "하루에 황금 둘 업적이 안 된다: " + JSON.stringify(t.save.st));
  g.clean("업적");
}

// ── 변이와 변이 조각: 변종 층(6층~)의 보스는 능력치 하나에 배수가 걸린다. 조각으로 내성을 올리면 약해진다
async function mutations() {
  const g = await boot("변이", {rev: 3, seed: 7, save: {
    alloc: {atk: 20, hp: 10, dfn: 10, crit: 5, cdmg: 5, spd: 5}, souls: 5, rebirths: 30, auto: false,
    cleared: Array.from({length: 76}, (_, i) => i + 1), best: 76,
    traits: {atk: 80, hp: 80, dfn: 70, crit: 0, cdmg: 60, spd: 0, soul: 20, pt: 10},
    exped: {since: NOON, seenExp: 1}, relics: {}, rbExp: 1, shards: 200}});
  const {t} = g;
  // 파이썬과 같은 변이 · 같은 배수 (내성 0 과 내성 5)
  for (const [n, id, lap, p0] of fx.expect.mut) {
    const m = t.mutOf(n);
    ok((m ? m.id : null) === id && (m ? m.lap : 0) === lap && same(t.mutPow(n), p0), `변이 ${n}스테이지: 화면 ${m && m.id}/${m && m.lap}/${t.mutPow(n)} · 파이썬 ${id}/${lap}/${p0}`);
  }
  ok(!t.mutOf(75) && !t.mutId(75) && t.mutPow(75) === 1, "5층에 변이가 붙었다");
  ok(/변이 <b>철갑<\/b> — 내가 주는 피해 ÷5/.test(g.$("mutNote").html) && /조각 200/.test(g.$("mutHead").text), "6층의 변이가 던전 카드에 안 보인다: " + g.$("mutNote").html);
  // 전투에 실제로 걸린다: 철갑은 내가 주는 피해를 나눈다
  ok(t.newFighters(t.boss(77))[0].div === 5 && !t.newFighters(t.boss(75))[0].div, "철갑이 전투에 안 걸렸다");
  // 벽이 파이썬과 같은 자리에 선다 — 내성이 오르면 벽이 뒤로 밀린다 (beatable 과 turns_to_win 이 같은 규칙)
  const keepSave = JSON.stringify(t.save);
  for (const build of fx.expect.mutWall) {         // 벽이 6층(철갑)에 서는 빌드와 8층(거대화)에 서는 빌드
    const walls = [];
    for (const [res, want] of build.cases) {
      t.save.alloc = {...build.alloc}; t.save.traits = {...t.fresh().traits, ...build.traits}; t.save.res = res;
      let wall = 1;
      while (wall < 400 && t.beatable(t.boss(wall))) wall++;
      ok(wall === want, `변이 벽 (내성 ${JSON.stringify(res)}): 화면 ${wall} · 파이썬 ${want}`);
      walls.push(wall);
    }
    ok(walls[0] < walls[2] && walls[2] <= walls[3], "내성을 올려도 벽이 안 밀린다: " + walls);
  }
  t.save = JSON.parse(keepSave); t.drawAll();
  // 치명타 배분은 100% 가 되는 데까지만 — 넘친 %p 를 치명타 피해로 바꾸는 건 1점 0.4%p 라 손해였다
  t.save.alloc = {atk: 0, hp: 0, dfn: 0, crit: 0, cdmg: 0, spd: 0}; t.save.traits.pt = 100; t.drawAll();   // 각성으로 점수를 넉넉히
  const cm = t.critMax(), critBtn = d => g.btn("alloc", {k: "crit", d});
  await g.click(critBtn("max"));
  ok(cm > 0 && t.save.alloc.crit === cm && t.F("crit") === 100, `치명타 최대가 100% 에 안 맞는다: ${t.save.alloc.crit}/${cm} → ${t.F("crit")}`);
  t.save.alloc.crit = cm - 1; t.drawAll();
  ok(t.F("crit") < 100 && !critBtn("1").disabled, "100% 한 점 앞에서 더 못 넣는다");
  await g.click(critBtn("10"));
  ok(t.save.alloc.crit === cm && critBtn("1").disabled && critBtn("max").disabled && !g.btn("alloc", {k: "atk", d: "1"}).disabled,
     `+10 이 100% 를 넘겨 넣었거나 다른 줄까지 막혔다: ${t.save.alloc.crit}/${cm}`);
  ok(/더 넣을 수 없다/.test(g.$("alloc").html), "치명타 100% 안내가 없다");
  // 속도: 보스 SPD 는 스테이지당 K.bspd 씩, 한 턴 최대 2타 (v0.19.2)
  ok(t.boss(100).spd === 100 * t.K.bspd && t.boss(200).spd === 2 * t.boss(100).spd, "보스 SPD 가 덧셈으로 안 오른다");
  t.save.alloc.spd = 0; t.save.traits.spd = 0; t.drawAll();
  { const b = t.boss(10), want = Math.ceil((2 * b.spd - t.H.spd) / 3);
    t.save.alloc = {atk: 0, hp: 0, dfn: 0, crit: 0, cdmg: 0, spd: want + 50}; t.drawAll();
    ok(t.xtra(b) === 1 && t.hitsOf(b) === 2, `보스보다 훨씬 빨라도 2타가 아니다: ${t.xtra(b)} ${t.hitsOf(b)}`);
    t.save.alloc.spd = 0; t.drawAll();
    ok(/2타 확률/.test(g.$("alloc").html), "속도 줄에 2타 확률 안내가 없다"); }
  // 속도 배분은 다음 보스 기준 늘 2타가 되는 데까지만 (치명타 100% 와 같은 방식)
  { t.save.alloc = {atk: 0, hp: 0, dfn: 0, crit: 0, cdmg: 0, spd: 0}; t.save.traits.pt = 100; t.drawAll();
    const sm = t.spdMax(), b = t.boss((t.save.best || 0) + 1), spdBtn = d => g.btn("alloc", {k: "spd", d});
    await g.click(spdBtn("max"));
    ok(sm > 0 && t.save.alloc.spd === sm && t.xtra(b) === 1, `속도 최대가 늘 2타에 안 맞는다: ${t.save.alloc.spd}/${sm} → ${t.xtra(b)}`);
    t.save.alloc.spd = sm - 1; t.drawAll();
    ok(t.xtra(b) < 1 && !spdBtn("1").disabled, "늘 2타 한 점 앞에서 더 못 넣는다");
    await g.click(spdBtn("10"));
    ok(t.save.alloc.spd === sm && spdBtn("1").disabled && spdBtn("max").disabled && !g.btn("alloc", {k: "atk", d: "1"}).disabled,
       `+10 이 늘 2타를 넘겨 넣었거나 다른 줄까지 막혔다: ${t.save.alloc.spd}/${sm}`);
    ok(/늘 2타 — 더 넣을 수 없다/.test(g.$("alloc").html), "속도 늘 2타 안내가 없다");
    t.save.alloc.spd = sm + 30; t.drawAll();
    ok(t.save.alloc.spd === sm + 30, "이미 넣은 속도 점수를 빼 버린다");
    // 상한은 배분에만 — 늘 2타여도 신속의 유산은 산다
    t.save.alloc.spd = sm; t.save.souls = 1e12; t.drawAll();
    const lv = t.save.traits.spd, tb = g.$("traits").querySelectorAll("button").find(x => x.dataset.t === "spd" && x.dataset.c === "1");
    ok(!/더 넣을 수 없다|늘 2타/.test(g.$("traits").html), "특성 줄에 속도 상한 안내가 붙었다");
    ok(tb && !tb.disabled, "늘 2타라고 신속의 유산 구입이 막혔다");
    await g.click(tb);
    ok(t.save.traits.spd === lv + 1, `신속의 유산을 못 샀다: ${lv} → ${t.save.traits.spd}`); }
  // 유물로 치명타가 오르면 남는 점수는 돌려준다
  t.save.alloc.crit = cm + 40; t.drawAll();
  ok(t.save.alloc.crit === cm, `넘친 치명타 배분을 돌려주지 않는다: ${t.save.alloc.crit}/${cm}`);
  t.save = JSON.parse(keepSave); t.drawAll();
  // 관문: 변종 층의 마지막 보스는 그 층의 변이를 다 지워야 싸울 수 있다 — 파이썬 gated() 와 같은 자리
  for (const [n, res, want] of fx.expect.gate) {
    t.save.res = res;
    ok(t.gated(n) === want && (!want || !t.beatable(t.boss(n))), `관문 ${n}스테이지 (내성 ${JSON.stringify(res)}): 화면 ${t.gated(n)} · 파이썬 ${want}`);
  }
  // 능력치가 아무리 높아도 관문은 못 넘고, 내성을 채우면 넘는다. 화면은 '벽' 이 아니라 '관문' 이라고 적는다
  t.save.traits = {...t.save.traits, atk: 400, hp: 400, dfn: 400, cdmg: 300};
  t.save.cleared = Array.from({length: 89}, (_, i) => i + 1); t.save.best = 89; t.save.res = {armor: 9}; t.drawAll();
  ok(t.beatable(t.boss(89)) && !t.beatable(t.boss(90)), "내성 9레벨로 6층 보스를 넘는다");
  ok(/관문: 90스테이지/.test(g.$("wall").html) && /내성\s+Lv\.10/.test(g.$("wall").html) && /지금 Lv\.9/.test(g.$("wall").html), "관문 안내가 없다: " + g.$("wall").html);
  const gateBtn = g.$("stages").kids[14].kids.find(k => k.tag === "button");
  ok(gateBtn && gateBtn.disabled && gateBtn.textContent === "내성 부족", "관문 보스에 도전할 수 있다: " + (gateBtn && gateBtn.textContent));
  t.save.res = {armor: 10}; t.drawAll();
  ok(t.beatable(t.boss(90)) && !/관문/.test(g.$("wall").html), "내성 10레벨인데 관문이 안 열린다");
  // 관문이 없던 때 넘은 층은 내성을 찍어 준다 — 7층(104)까지 간 저장은 6층 관문 몫만 받는다. 두 겹(11층~)은 20레벨
  t.save.res = {curse: 3}; t.save.best = 104; t.fill();
  ok(t.save.res.armor === 10 && t.save.res.curse === 3 && !t.save.res.fury, "넘은 관문의 내성을 안 찍어 준다: " + JSON.stringify(t.save.res));
  t.save.best = 170; t.fill();
  ok(t.save.res.armor === 20 && t.save.res.curse === 10 && t.save.res.numb === 10 && !t.gated(165) && t.gated(180), "두 겹째 관문의 내성이 어긋났다: " + JSON.stringify(t.save.res));
  t.drawAll();
  // 지운 겹은 안 적는다 — 지금 지우는 겹만. 철갑은 두 겹 다(11층까지), 저주는 둘째 겹(12층)을 지우는 중
  const mh = g.$("muts").html;
  ok(/11층까지 다 지웠다/.test(mh) && /12층 — 내 SPD ÷8/.test(mh) && !/첫 겹|÷1</.test(mh), "내성 줄이 지운 겹까지 적는다: " + mh.slice(0, 400));
  t.save = JSON.parse(keepSave); t.drawAll();
  // 내성: 조각으로 한 레벨씩. 값은 12, 14, 16… 이고 배수가 줄어든다
  const btn = id => g.btn("muts", {m: id});
  await g.click(btn("armor"));
  ok(t.save.res.armor === 1 && t.save.shards === 191 && same(t.mutPow(77), Math.pow(5, 0.9)), `내성 구입이 어긋났다: ${JSON.stringify(t.save.res)} 조각 ${t.save.shards}`);
  for (let i = 0; i < 2; i++) await g.click(btn("armor"));
  ok(t.save.res.armor === 3 && t.save.shards === 191 - 10 - 11, `내성 값이 어긋났다: ${t.save.shards}`);
  t.save.shards = 11; t.drawAll();
  ok(btn("armor").disabled, "조각 11개로 12개짜리를 살 수 있다");
  t.buyRes("armor", 1);
  ok(t.save.res.armor === 3 && t.save.shards === 11, "조각이 모자란데 내성이 올랐다");
  // 다 올리면 사라진다 — 덤은 없다. 닿은 깊이(한 겹)보다 더는 못 올린다
  t.save.shards = 5000; t.drawAll();
  for (let i = 0; i < 7; i++) await g.click(btn("armor"));
  ok(t.save.res.armor === 10 && t.mutPow(77) === 1 && !t.mutId(77) && btn("armor").disabled, "내성 10레벨인데 변이가 남았다");
  ok(!t.newFighters(t.boss(77))[0].div && /내성으로 지웠다/.test(g.$("mutNote").html), "지운 변이가 전투에 남아 있다");
  t.buyRes("armor", 5);
  ok(t.save.res.armor === 10, "닿지 않은 겹의 내성을 살 수 있다");
  // 내성이 넘쳐도 덤은 없다 — 배수가 1 아래로 내려가면 켜 두기만 해서 기준보다 빨라진다
  t.save.res = {...t.save.res, armor: 25};
  ok(t.mutPow(77) === 1 && !t.newFighters(t.boss(77))[0].div, "내성이 넘치면 변이가 덤이 된다: " + t.mutPow(77));
  t.save.res = {...t.save.res, armor: 10};
  ok(same(t.mutPow(151), 5), "11층(두 겹)의 철갑이 내성 10레벨에서 5배가 아니다: " + t.mutPow(151));
  // 다른 변이도 전투에 걸린다
  const f = n => t.newFighters(t.boss(n));
  const plain = t.newFighters(t.boss(75));
  ok(same(f(91)[0].max, plain[0].max), "저주가 아직 내 HP 를 나눈다");
  ok(same(t.spdVs(t.boss(91)), t.spdNow() / 8) && same(t.spdVs(t.boss(90)), t.spdNow()), "저주가 내 SPD 를 안 나눈다");
  { const b = t.boss(91), keep = t.save.alloc.spd;    // 저주로 보스보다 느려지면 선공을 빼앗긴다
    t.save.alloc.spd = 0; while (t.spdNow() < b.spd * 1.5) t.save.alloc.spd += 10;
    ok(t.spdNow() >= b.spd && !t.goesFirst(b), "저주로 느려졌는데 내가 먼저 친다");
    t.save.alloc.spd = keep; }
  ok(same(f(106)[1].max, t.boss(106).hp * 5) && same(f(106)[1].atk, t.boss(106).atk), "거대화가 보스 HP 를 안 올린다");
  ok(same(f(121)[0].crit, plain[0].crit / 5) && same(f(121)[0].dfn, plain[0].dfn), "실명이 치명타 확률을 안 나눈다");
  ok(same(f(136)[0].cdmg, Math.max(100, plain[0].cdmg / 5)), "무감각이 치명타 피해를 안 나눈다");
  // 조각은 변종 층에서만 나온다
  g.rand(() => 0);
  const had = t.save.shards;
  ok(!t.rollShard(75) && t.save.shards === had && t.rollShard(76) && t.save.shards === had + 1, "조각이 변종 층 밖에서 나오거나 변종 층에서 안 나온다");
  g.rand();
  ok(!t.validSave({...t.save, shards: -1}) && !t.validSave({...t.save, res: {armor: 1.5}}), "조각·내성에 이상한 값이 든 저장을 받는다");
  // 변종 층에서 수동 전투를 한 판 — 변이가 걸린 채로 끝까지 돈다
  await g.fight(1);
  g.clean("변이 전투");
}

// ── 초월: Lv.99 에서 레벨만 되돌린다. 식이 파이썬 hero() 와 같아야 메뉴 막대 배지가 안 어긋난다
async function transcend() {
  const g = await boot("초월", {page: "maxed", seed: 4}), {t} = g;
  for (const want of fx.expect.trans) {
    ok(t.transOf(t.save) === want.trans && t.lvNow() === want.level && t.points() === want.points,
       `초월 ${want.trans}회: 화면 Lv.${t.lvNow()} ${t.points()}pt · 파이썬 Lv.${want.level} ${want.points}pt`);
    g.clean("초월 " + want.trans + "회");
    if (g.has("transBtn")) await g.click(g.$("transBtn"));
  }
}

// ── 토큰 0 인 갓 깐 설치. 1스테이지는 깨야 게임이 시작된다 (v0.11.0 의 약속)
async function zeroHero() {
  const g = await boot("토큰 0", {page: "zero", seed: 5}), {t} = g;
  ok(t.lvNow() === 1 && t.F("atk") === 0, "토큰 0 영웅이 Lv.1 ATK 0 이 아니다");
  await g.click(g.btn("alloc", {k: "atk", d: "max"}));
  await g.fight(0);
  ok(t.save.cleared.includes(1), "토큰 0 으로 1스테이지를 못 깬다");
  g.clean("끝");
}

// ── 서버 없이 파일로 연 화면: 저장은 이 브라우저에 둔다
async function fileMode() {
  const g = await boot("파일로 열기", {served: false, seed: 6, local: {
    "trpg": JSON.stringify({souls: 42, cleared: [1], best: 1}), "trpg.fold": JSON.stringify(["hosts"])}});
  const {t} = g;
  ok(t.save.souls === 42, "브라우저에 둔 저장을 안 읽는다");
  const cards = g.doc.querySelectorAll("details[data-k]");
  ok(cards.length >= 5 && cards.every(d => d.open === (d.dataset.k !== "hosts")), "접어 둔 카드를 기억하지 못한다");
  await g.click(g.btn("alloc", {k: "hp", d: "1"}));
  ok(JSON.parse(g.store.get("trpg")).alloc.hp === 1, "브라우저 저장에 안 적었다");
  g.clean("끝");
}

(async () => {
  process.on("unhandledRejection", e => { throw e; });
  for (const run of [playthrough, oldSave, deepSave, mutations, achievements, transcend, zeroHero, fileMode]) await run();
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
