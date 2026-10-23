// 녹화 전 상태 (녹화에 안 나온다): 1층 6스테이지까지 깬 첫 판, 환생 기운 3개, 배분 일부
// 초월은 한 번 마친 것으로 둔다 — 토큰이 초월 비용을 넘으면 Lv.99 초월 안내가 화면을 가린다
save = Object.assign(fresh(), {cleared: [1, 2, 3, 4, 5, 6], best: 6, rbExp: H.exp - 3 * K.rbExp,
  trans: Math.floor(H.exp / TRANS_EXP)});
fill();
Object.assign(save.alloc, {atk: 26, hp: 16, dfn: 6, crit: 2, cdmg: 0});
put(); drawAll();
// 기기 이름이 나오는 '합산된 기기'·저장 코드와 긴 도감·업적은 접는다
for (const d of document.querySelectorAll("details[data-k]"))
  d.open = !["hosts", "code", "dex", "ach", "exped", "slot"].includes(d.dataset.k);
showTab("fight");
window.scrollTo(0, 0);
return "ready";
