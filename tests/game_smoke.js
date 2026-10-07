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
  boss, slotOf, catchRare, dexKey, sideSoul, idleRate, slotPay, raidHp, raidPay, raidClaim, weekSum, raidWk, F, expFor, levelOf, lvNow, costOf, soulOf, pointsOf, beatable, turnDmg, drawAll,
  encodeSave, decodeSave, validSave, left, points, picks, missions, transOf, maxCleared };`;

const [Y, M, DAY] = fx.today.split("-").map(Number);
const NOON = new Date(Y, M - 1, DAY, 12).getTime();     // 현지 정오 — 어느 시간대에서도 같은 날짜가 나온다

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
    ok(JSON.stringify(t.boss(n)) === JSON.stringify(want),
       `보스 ${n}: 화면 ${JSON.stringify(t.boss(n))} · 파이썬 ${JSON.stringify(want)}`);
  for (const [n, want] of e.slots) {             // 층 테마 — 이름·이모지·테마와 칸 고정값
    const s = t.slotOf(n);
    for (const k of Object.keys(want))
      ok(s[k] === want[k], `${n}스테이지 칸의 ${k}: 화면 ${s[k]} · 파이썬 ${want[k]}`);
  }
  e.exp.forEach((want, i) => ok(t.expFor(i + 1) === want, `Lv.${i + 1} EXP: 화면 ${t.expFor(i + 1)} · 파이썬 ${want}`));
  for (const [x, want] of e.levels) ok(t.levelOf(x) === want, `EXP ${x}: 화면 Lv.${t.levelOf(x)} · 파이썬 Lv.${want}`);
  e.cost.forEach((want, lv) => ok(t.costOf(lv) === want, `특성 ${lv}레벨 비용: 화면 ${t.costOf(lv)} · 파이썬 ${want}`));
  // 혼은 환생·유물·수확 배수가 없는 새 저장에서 잰다
  for (const [n, want] of e.soul) ok(t.soulOf(n) === want, `혼 ${n}스테이지: 화면 ${t.soulOf(n)} · 파이썬 ${want}`);
  for (const [n, want] of e.side) ok(t.sideSoul(n) === want, `보조 수입 혼 ${n}스테이지: 화면 ${t.sideSoul(n)} · 파이썬 ${want}`);
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

// ── 새 설치에서 한 바퀴: 배분 → 전투 → 슬롯 → 혼 사냥 → 미션 → 원정 → 환생 → 자동 도전 → 특성
async function playthrough() {
  const g = await boot("한 바퀴"), {t, server} = g;
  ok(t.save.rbExp === t.H.exp, "새 저장의 환생 기운 기준이 지금 EXP 가 아니다");
  ok(server.puts >= 1, "첫 화면에서 저장을 한 번도 안 썼다");
  parity(g);

  await g.click(g.$("updBtn"));
  ok(g.$("updMsg").text === "올렸다", "업데이트 버튼이 결과를 안 적는다");

  await g.click(g.btn("alloc", {k: "atk", d: "max"}));
  ok(t.left() === 0 && t.save.alloc.atk === t.points(), "'최대' 가 남은 포인트를 다 안 넣는다");
  await g.click(g.btn("alloc", {k: "atk", d: "-1"}));
  await g.click(g.btn("alloc", {k: "spd", d: "1"}));
  ok(t.left() === 0 && t.save.alloc.spd === 1, "−1 · +1 이 한 점씩 안 옮긴다");

  ok(/1층 코드 버그/.test(g.$("floorTitle").text), "층 제목에 테마가 없다: " + g.$("floorTitle").text);
  ok(/2층 첫 보스/.test(g.$("raid").html) && !g.$("raid").querySelectorAll("button").length, "레이드가 2층 전에 열려 있다");
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

  await g.click(g.$("miniBtn"));
  for (let shot = 0; shot < t.K.miniShots; shot++) {
    await g.advance(300 + 137 * shot);
    await g.click(g.$("miniBtn"));
    g.clean("혼 사냥 " + (shot + 1) + "발");
    await g.advance(700);
  }
  ok(t.save.mini.n === 1, "혼 사냥 한 판이 안 빠졌다");
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
  ok(JSON.stringify(t.save.alloc) === alloc, "자동 도전을 켠 채 환생했는데 배분이 풀렸다");
  g.clean("환생");

  await g.advance(6000);
  ok(t.save.cleared.length >= 3, "자동 도전이 안 오른다: " + JSON.stringify(t.save.cleared));
  await g.click(g.$("pickAll"));
  ok(t.picks().length === t.save.best, "'전체 반복' 이 다 고르지 않는다");
  await g.click(g.$("picks"), {target: g.$("auto").all().find(e => e.dataset.g === "1")});
  ok(!t.picks().includes(1), "고른 칸을 다시 눌러도 안 빠진다");
  await g.click(g.$("pickAll")); await g.click(g.$("pickAll"));
  ok(!t.picks().length, "'전체 해제' 가 비우지 않는다");
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
    mini: {day: fx.today, n: 3}, slot: {day: fx.today, n: 10}}});
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
  ok(t.missions().every(m => m.souls > 0 && m.souls <= side * t.K.weekBudget * 2), "미션이 보조 수입용 혼을 안 쓴다");
  ok(/4층 회사/.test(g.$("floorTitle").text), "4층 제목이 테마를 안 따른다: " + g.$("floorTitle").text);
  ok(g.$("stages").kids[0].html.includes(t.slotOf(46).name) && t.slotOf(46).name === "급한 건 모기",
     "4층 첫 칸이 테마 보스가 아니다");
  ok(g.$("auto").html.includes(t.slotOf(17).name), "반복 고르기 칸이 2층 테마 이름을 안 쓴다");
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
  for (let i = 0; i < 4; i++) {
    const had = t.save.souls;
    await g.click(g.btn("raid", {r: String(i)}));
    ok(t.save.raid.got.includes(i) && t.save.souls >= had + pay, `레이드 ${i + 1}단계 보상이 안 들어왔다`);
    ok(i === 3 || t.save.souls === had + pay, `레이드 ${i + 1}단계가 적힌 것보다 많이 줬다`);
  }
  ok(/격파 — 혼/.test(g.$("raid").html) && /유물/.test(g.$("raid").html), "격파 알림에 유물 굴림이 없다");
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
  for (const run of [playthrough, oldSave, deepSave, transcend, zeroHero, fileMode]) await run();
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
