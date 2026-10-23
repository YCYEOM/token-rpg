// 홍보 영상 시나리오 (~50초). 실제 게임 화면을 실제 규칙으로 조작한다. 자막·타이틀만 덧씌운다.
// v0.20.7 화면 기준 — 탭(전투·성장·일과·수집), 환생은 성장 탭, 반복 칸의 ◆·★ 표시.
const sleep = ms => new Promise(r => setTimeout(r, ms));
const ov = document.createElement("div"), card = document.createElement("div");
ov.style.cssText = "position:fixed;left:0;right:0;bottom:0;z-index:99;pointer-events:none;opacity:0;transition:opacity .35s";
card.style.cssText = "position:fixed;inset:0;z-index:100;display:none;flex-direction:column;align-items:center;" +
  "justify-content:center;text-align:center;background:#0d1117;opacity:0;transition:opacity .6s;padding:24px";
document.body.append(ov, card);

const caption = async html => {
  ov.innerHTML = `<div style="margin:0 12px 16px;padding:12px 14px;border:1px solid var(--gold);border-radius:12px;
    background:#0d1117f2;font-size:16px;font-weight:700;line-height:1.45;box-shadow:0 6px 24px #000a">${html}</div>`;
  ov.style.opacity = 1; await sleep(350);
};
const hideCap = async () => { ov.style.opacity = 0; await sleep(350); };
const showCard = async html => { card.innerHTML = html; card.style.display = "flex"; await sleep(30); card.style.opacity = 1; await sleep(600); };
const hideCard = async () => { card.style.opacity = 0; await sleep(650); card.style.display = "none"; };
const tap = async (el, after = 350) => {           // 누르는 곳을 잠깐 금색으로 — 무엇을 눌렀는지 보이게
  el.style.boxShadow = "0 0 0 3px var(--gold)"; await sleep(220); el.click(); el.style.boxShadow = ""; await sleep(after);
};
// 탭 막대가 위에 붙어 있어서 그만큼 띄워서 내린다
const scrollToEl = async el => {
  window.scrollTo({top: el.getBoundingClientRect().top + window.scrollY - 64, behavior: "smooth"}); await sleep(900);
};
const scrollToK = k => scrollToEl(document.querySelector(`details[data-k=${k}]`));
const tab = async t => { await tap(document.querySelector(`button[data-tab=${t}]`), 250); };

// 1) 타이틀
await showCard(`<div style="font-size:70px">🧙</div>
  <div style="font-size:42px;font-weight:800;letter-spacing:.03em;margin-top:6px">TOKEN RPG</div>
  <div style="font-size:18px;color:var(--dim);margin-top:12px">AI 코딩에 쓴 토큰이 캐릭터가 된다</div>`);
await sleep(2200); await hideCard();

// 2) 토큰 = 스탯 → 성장 탭에서 배분
await caption("출력 토큰 → <span class=gold>ATK</span> · 캐시 → <span class=gold>DEF</span> · thinking → <span class=gold>CRIT</span>");
await sleep(2200);
await tab("grow");
await scrollToK("alloc");
await caption("쓴 만큼 레벨이 오르고, 포인트를 스탯에 배분한다");
await tap(document.querySelector('#alloc button[data-k=atk][data-d="10"]'), 450);
for (let i = 0; i < 3; i++) await tap(document.querySelector('#alloc button[data-k=crit][data-d="1"]'), 200);
await sleep(700); await hideCap();

// 3) 전투
await tab("fight");
window.scrollTo({top: 0, behavior: "smooth"}); await sleep(500);
await scrollToK("dungeon");
await caption("🐛 버그 벌레부터 👑 루트 권한의 군주까지 — 개발자의 적 75종");
await sleep(1000);
// 7스테이지 = 보스가 선공하는 첫 벽 — 턴이 길어 전투가 화면에 오래 남는다
await tap(document.querySelectorAll("#stages .st")[6].querySelector("button"), 100);
await caption("턴제 자동 전투 — 치명타는 thinking 토큰에서 나온다");
while ($("close").disabled) await sleep(150);
await sleep(1300);
await hideCap(); await tap($("close"), 300);

// 4) 유물 (연출: 이번 격파에서 전설이 나온 장면)
save.relics[relicKey(SLOTS[6])] = {r: 3, a: SLOTS[6].affix, g: 7};
relicNews = `전설 유물 획득! ${SLOTS[6].emoji} ${SLOTS[6].name} (1층) — ${AFFIX[SLOTS[6].affix][0]} +24${AFFIX[SLOTS[6].affix][2]}`;
put(); drawAll();
await tab("collect");
window.scrollTo({top: 0, behavior: "smooth"}); await sleep(600);
await scrollToK("relics");
await caption("보스가 떨어뜨리는 유물 — 깊은 층에서 나올수록 세다");
await sleep(2600); await hideCap();

// 5) 환생 -> 영구 특성 (성장 탭)
await tab("grow");
window.scrollTo({top: 0, behavior: "smooth"}); await sleep(600);
await scrollToK("rebirth");
await caption("벽에 막히면 환생 — 혼을 모아 영구 특성을 산다");
save.auto = true;                                    // 환생 후 배분을 그대로 다시 건다
await tap($("rbBtn"), 700); await tap($("rbBtn"), 1100);
save.souls += 40_000; drawAll();
await scrollToK("traits");
// 한 특성에 '최대' 를 누르면 혼을 다 써 버린다 — 넷을 번갈아 한 레벨씩 산다
for (let i = 0; i < 4; i++)
  for (const k of ["atk", "hp", "dfn", "cdmg"])
    await tap(document.querySelector(`#traits button[data-t=${k}][data-c="1"]`), 120);
await sleep(800); await hideCap();

// 6) 자동 도전 — 반복 칸에 아직 모을 것이 찍힌다
await tab("fight");
window.scrollTo({top: 0, behavior: "smooth"}); await sleep(500);
save.farm = 0; autoReset(); drawAll();
await scrollToK("dungeon");
await caption("자동 도전 — 알아서 오르고, 막히면 그 자리에서 유물을 캔다");
for (let i = 0; i < 16; i++) { autoStep(); await sleep(250); }
picksOpen = true; autoKey = ""; drawAuto();
await caption("◆ 더 센 유물 · <span class=gold>★</span> 황금 개체 — 아직 못 모은 곳이 칸에 바로 보인다");
for (let i = 0; i < 10; i++) { autoStep(); await sleep(250); }
await sleep(500); await hideCap();

// 7) 일과 — 실제 사용량으로 차는 미션
await tab("daily");
window.scrollTo({top: 0, behavior: "smooth"}); await sleep(600);
await caption("미션 · 주간 레이드 · 슬롯 · 원정 — AI를 쓴 만큼 채워진다");
await sleep(2600); await hideCap();

// 8) 엔딩
await showCard(`<div style="font-size:52px">🎮</div>
  <div style="font-size:36px;font-weight:800;letter-spacing:.03em;margin-top:6px">TOKEN RPG</div>
  <div style="font-size:16px;color:var(--dim);margin-top:12px;line-height:1.7">Claude Code · Codex · Gemini CLI<br>
    쓴 만큼 강해진다 · macOS 메뉴 막대 · 윈도우 트레이</div>
  <div style="margin-top:24px;font:600 19px ui-monospace,monospace;padding:11px 18px;border:1px solid var(--line);
    border-radius:10px;background:#161b22">pipx install token-rpg</div>
  <div style="margin-top:14px;font-size:15px;color:var(--on)">ycyeom.github.io/token-rpg</div>`);
await sleep(3600);
return `끝: 환생 ${save.rebirths}회, 최고 ${maxCleared()}스테이지`;
