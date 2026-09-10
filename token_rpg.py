#!/usr/bin/env python3
"""Claude Code 토큰 사용량으로 굴러가는 턴제 RPG.

설계: 내 스탯은 토큰에 선형으로, 보스 능력치는 토큰의 거듭제곱근(<1)으로 커진다.
따라서 막힌 스테이지는 토큰을 더 쓰면 반드시 넘을 수 있다.
"""
import argparse, json, sys, glob, os, shutil, socket, subprocess, collections, webbrowser
from datetime import datetime, timezone

__version__ = "0.3.0"

# Claude Code가 대화 기록을 남기는 곳. 여기서 usage 필드만 읽는다.
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
TRANSCRIPTS = os.path.join(CLAUDE_DIR, "projects")
SETTINGS = os.path.join(CLAUDE_DIR, "settings.json")

def data_dir():
    """게임 데이터를 두는 곳. 설치 경로(site-packages)에는 절대 쓰지 않는다."""
    d = os.environ.get("TOKEN_RPG_HOME") or os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "token-rpg")
    os.makedirs(d, exist_ok=True)
    return d

def game_path():
    return os.path.join(data_dir(), "game.html")

def config_path():
    return os.path.join(data_dir(), "config.json")


def load_config():
    try:
        with open(config_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    tmp = config_path() + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, config_path())


def snap_dir():
    """PC별 스냅샷 폴더. 클라우드 동기화 폴더로 지정하면 여러 기기가 합산된다."""
    d = os.environ.get("TOKEN_RPG_SNAPSHOTS") or os.path.join(data_dir(), "snapshots")
    os.makedirs(d, exist_ok=True)
    return d

# --- 밸런스 조절 손잡이 (python3 build.py --balance 로 확인) ---
# 보스 능력치는 '전역 스테이지 번호'의 지수 곡선이다. 층 경계에서 난이도가
# 끊기지 않고, 한 층(=프로젝트 수)을 돌 때마다 STEP^N 배씩 벽이 높아진다.
STEP = 1.28                              # 스테이지 1칸당 보스 배율
B_HP, B_ATK, B_DEF = 115, 24, 8          # 1스테이지 보스 기준치
PT_PER_LEVEL = 2                         # 레벨업당 자유 배분 포인트
GAIN = {"atk": 1, "hp": 12, "dfn": 0.6, "crit": 0.4}   # 포인트 1점당 상승치

# 환생: 클리어 기록과 배분을 버리고 '혼'을 얻어 영구 특성을 산다.
# 특성 효과는 배율(지수), 비용도 지수 -> 층 벽을 넘으려면 환생을 거듭해야 한다.
TRAIT_MUL  = 1.12    # 특성 1레벨당 스탯 배율
COST_MUL   = 1.13    # 특성 1레벨당 비용 증가율
COST_K     = 80      # 특성 1레벨 기본 비용
SOUL_EXP   = 2.4     # 혼 획득 = 스테이지번호^SOUL_EXP
TRAIT_PT   = 4       # '각성' 1레벨당 배분 포인트
TRAIT_CRIT = 2.0     # '예지' 1레벨당 치명타 %p

# 원정(방치 수입): 클리어한 가장 깊은 스테이지를 자동 반복해 혼을 캔다.
# 초당 수확 = soulOf(최고 클리어) / IDLE_DIV. 8시간이면 환생 1회분 언저리 =
# 자는 동안 벌어두는 보조 수입이지, 환생을 대체하지는 않는다.
IDLE_DIV       = 10_000
IDLE_CAP_H     = 8          # 누적 상한(시간) — 돌아올 이유를 남긴다
IDLE_TOKEN_DIV = 1_000_000  # 이만큼 새로 쓸 때마다 생산 +1배
IDLE_TOKEN_MAX = 2          # 토큰 배율 상한(총 3배)

TIERS = [
    (0,  "\U0001f95a", "토큰 알"),
    (5,  "\U0001f423", "깨어난 파서"),
    (10, "\U0001f9d2", "견습 프롬프터"),
    (20, "\U0001f9d1‍\U0001f4bb", "코드 술사"),
    (32, "\U0001f9d9", "컨텍스트 마법사"),
    (46, "\U0001f9d9‍♂️", "대현자"),
    (60, "\U0001f409", "토큰 드래곤"),
]
BOSS_EMOJI = ["\U0001fab2", "\U0001f577️", "\U0001f40d", "\U0001f982",
              "\U0001f9df", "\U0001f479", "\U0001f47e", "\U0001f432"]


def level_of(exp):
    return min(int((exp / 50_000) ** 0.5) + 1, 99)

def exp_for(lv):
    return int(((lv - 1) ** 2) * 50_000)

def tier_of(lv):
    return [t for t in TIERS if lv >= t[0]][-1]


# ── 프로바이더 ────────────────────────────────────────────────────
# 툴마다 로그 위치와 형식이 다르다. 각 프로바이더는 "파일 하나를 읽어
# 토큰 합계를 돌려주는 함수" 하나만 제공하면 된다.

def _lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    yield line
    except OSError:
        return


def read_claude(path):
    """Claude Code: ~/.claude/projects/<프로젝트>/<세션>.jsonl
    message.usage 를 읽고 message.id 로 중복(재시도·사이드체인)을 제거한다."""
    agg, days, models, seen = collections.Counter(), set(), collections.Counter(), set()
    for line in _lines(path):
        if '"usage"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        msg = d.get("message") or {}
        u = msg.get("usage")
        if not isinstance(u, dict):
            continue
        mid = msg.get("id")
        if mid:
            if mid in seen:
                continue
            seen.add(mid)
        agg["input"] += u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
        agg["output"] += u.get("output_tokens", 0)
        agg["cache_read"] += u.get("cache_read_input_tokens", 0)
        agg["thinking"] += (u.get("output_tokens_details") or {}).get("thinking_tokens", 0)
        agg["calls"] += 1
        models[msg.get("model") or "unknown"] += 1
        ts = d.get("timestamp") or ""
        if ts:
            days.add(ts[:10])
    proj = os.path.basename(os.path.dirname(path))
    return agg, proj, days, models


def read_codex(path):
    """Codex CLI: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl

    실제 로그(cli 0.153.4)로 확인한 구조:
      {"type":"event_msg","payload":{"type":"token_count","info":{
         "total_token_usage": {input_tokens, cached_input_tokens,
                               cache_write_input_tokens, output_tokens,
                               reasoning_output_tokens, total_tokens},
         "last_token_usage":  {...같은 모양, 이번 회차 증분...}}}}

    total_token_usage 는 세션 누적이므로 파일의 마지막 값 하나만 쓴다.
    (last_token_usage 를 전부 더해도 같은 값이 나온다.)
    프로젝트 이름은 session_meta.payload.cwd 의 마지막 경로 조각.
    """
    last, days, models, cwd, n_events = None, set(), collections.Counter(), None, 0
    for line in _lines(path):
        if '"token_count"' not in line and '"session_meta"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        p = d.get("payload") or {}
        if d.get("type") == "session_meta":
            cwd = cwd or p.get("cwd") or d.get("cwd")
            m = p.get("model") or p.get("model_provider")
            if m:
                models[m] += 1
            continue
        if p.get("type") != "token_count":
            continue
        n_events += 1
        info = p.get("info") or {}
        tot = info.get("total_token_usage")
        if isinstance(tot, dict):
            last = tot
        ts = d.get("timestamp") or ""
        if ts:
            days.add(str(ts)[:10])

    agg = collections.Counter()
    if last:
        agg["input"] = last.get("input_tokens", 0) + last.get("cache_write_input_tokens", 0)
        agg["output"] = last.get("output_tokens", 0)
        agg["cache_read"] = last.get("cached_input_tokens", 0)
        agg["thinking"] = last.get("reasoning_output_tokens", 0)
        # 세부 항목이 비고 total 만 채워진 세션이 실제로 있다. total = input + output
        # 이므로 분해가 없으면 통째로 input 으로 넣는다 (EXP 는 맞고 ATK 는 과장하지 않는다).
        if not any(agg[k] for k in ("input", "output", "cache_read", "thinking")):
            agg["input"] = last.get("total_tokens", 0)
        agg["calls"] = n_events
    if not agg["input"] and not agg["output"]:
        return collections.Counter(), "codex", set(), models
    name = os.path.basename((cwd or "").rstrip("/")) or "codex"
    return agg, name, days, models


PROVIDERS = {
    "claude-code": {
        "label": "Claude Code",
        "roots": lambda: [TRANSCRIPTS],
        "glob": os.path.join("*", "*.jsonl"),
        "read": read_claude,
        "verified": True,
    },
    "codex": {
        "label": "Codex CLI",
        "roots": lambda: [os.path.expanduser("~/.codex/sessions")],
        "glob": os.path.join("**", "rollout-*.jsonl"),
        "read": read_codex,
        "verified": True,       # 실제 로그(codex cli 0.153.4)로 검증
    },
}


def provider_roots(pid, cfg=None):
    """기본 위치 + 사용자가 추가한 스캔 폴더."""
    cfg = cfg if cfg is not None else load_config()
    extra = (cfg.get("providers", {}).get(pid, {}) or {}).get("extra", [])
    out = list(PROVIDERS[pid]["roots"]())
    for p in extra:
        out.extend(sorted(glob.glob(os.path.expanduser(p))) or [os.path.expanduser(p)])
    return [p for p in out if os.path.isdir(p)]


def collect(root=None, pid="claude-code", roots=None):
    """한 프로바이더의 로그를 훑어 합계를 낸다."""
    spec = PROVIDERS[pid]
    agg = collections.Counter()
    days, projects, models = set(), collections.Counter(), collections.Counter()
    files = 0
    for r in ([root] if root else (roots if roots is not None else provider_roots(pid))):
        for path in glob.glob(os.path.join(r, spec["glob"]), recursive=True):
            a, proj, d, m = spec["read"](path)
            if not a:
                continue
            files += 1
            agg.update(a); days |= d; models.update(m)
            projects[pid + "\t" + proj] += a["input"] + a["output"]
    agg["sessions"] = files
    return agg, projects, models, days


def collect_all(cfg=None):
    """켜져 있는 프로바이더 전부를 합산한다."""
    cfg = cfg if cfg is not None else load_config()
    agg = collections.Counter()
    projects, models, days, per = collections.Counter(), collections.Counter(), set(), {}
    for pid in PROVIDERS:
        if not cfg.get("providers", {}).get(pid, {}).get("enabled", True):
            continue
        roots = provider_roots(pid, cfg)
        if not roots:
            continue
        a, p, m, d = collect(pid=pid, roots=roots)
        if not a.get("calls"):
            continue
        agg.update(a); projects.update(p); models.update(m); days |= d
        per[pid] = a["input"] + a["output"]
    return agg, projects, models, days, per


def save_snapshot(root=None, snaps=None):   # root 는 테스트용 단일 경로
    """이 PC의 집계만 작은 JSON으로 남긴다. 190MB 트랜스크립트는 옮기지 않는다."""
    snaps = snaps or snap_dir()
    os.makedirs(snaps, exist_ok=True)      # 명시로 넘긴 경로도 없으면 만든다
    if root:                                # 테스트용 단일 경로
        agg, projects, models, days = collect(root)
        per = {"claude-code": agg["input"] + agg["output"]}
    else:
        agg, projects, models, days, per = collect_all()
    snap = {"host": socket.gethostname(),
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "agg": dict(agg), "projects": dict(projects), "models": dict(models),
            "days": sorted(days), "providers": per}
    path = os.path.join(snaps, snap["host"].replace(os.sep, "_") + ".json")
    tmp = path + f".{os.getpid()}.tmp"          # 원자적 교체: 훅이 동시에 돌 수 있다
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False)
    os.replace(tmp, path)
    return path, snap


def merge(snaps=None):
    """snapshots/*.json 전부 합산. 파일당 PC 하나라 중복 없음."""
    snaps = snaps or snap_dir()
    agg, projects, models = collections.Counter(), collections.Counter(), collections.Counter()
    days, hosts, provs = set(), [], {}
    for p in sorted(glob.glob(os.path.join(snaps, "*.json"))):
        with open(p, encoding="utf-8") as f:
            snap = json.load(f)
        agg.update(snap["agg"]); projects.update(snap["projects"]); models.update(snap["models"])
        days |= set(snap.get("days", []))
        for k, v in (snap.get("providers") or {}).items():
            provs[k] = provs.get(k, 0) + v
        hosts.append((snap["host"], snap["updated"],
                      snap["agg"].get("input", 0) + snap["agg"].get("output", 0)))
    agg["days"] = len(days)
    merge.providers = provs        # 부가 정보 — 호출부가 필요할 때만 본다
    return agg, projects, models, hosts


def hero(agg):
    """토큰 종류가 스탯을 정한다 — 사용 패턴이 곧 캐릭터 빌드."""
    exp = agg["input"] + agg["output"]
    lv = level_of(exp)
    cur, nxt = exp_for(lv), exp_for(lv + 1)
    return {
        "exp": exp, "level": lv, "points": lv * PT_PER_LEVEL,
        "pct": round(100 * (exp - cur) / max(nxt - cur, 1), 1), "toNext": nxt - exp,
        "emoji": tier_of(lv)[1], "title": tier_of(lv)[2],
        "hp":   round(100 + agg["output"] / 8_000),
        "atk":  round(agg["output"] / 60_000),
        "dfn":  round(agg["cache_read"] / 30_000_000, 1),
        "crit": round(min(50, agg["thinking"] / 60_000), 1),
        "spd":  round(agg["calls"] / 400),
        "gain": GAIN, "raw": dict(agg),
    }


def dungeons(projects):
    """프로젝트 = 한 층의 스테이지 슬롯. 능력치는 boss()가 전역 번호로 정하고,
    프로젝트는 이름/이모지/혼 배수(토큰이 많을수록 혼을 더 준다)만 준다."""
    items = sorted(projects.items(), key=lambda kv: kv[1])
    home = os.path.basename(os.path.expanduser("~"))
    out = []
    for i, (key, v) in enumerate(items):
        pid, _, name = key.partition("\t")
        if not name:                     # 옛 스냅샷: 프로바이더 표기가 없다
            pid, name = "claude-code", key
        short = name.replace("-Users-" + home, "").strip("-") or "home"
        share = i / max(1, len(items) - 1)          # 토큰 적은 쪽 0.0 ~ 많은 쪽 1.0
        out.append({"slot": i + 1, "name": short, "tokens": v,
                    "prov": PROVIDERS.get(pid, {}).get("label", pid),
                    "emoji": BOSS_EMOJI[i % len(BOSS_EMOJI)],
                    "soul": round(0.7 + 0.6 * share, 2)})
    return out


def boss(g):
    """전역 스테이지 번호 g(1부터) -> 보스 능력치."""
    p = STEP ** (g - 1)
    return {"hp": round(B_HP * p), "atk": round(B_ATK * p),
            "dfn": round(B_DEF * p), "spd": round(B_DEF * p * 0.9)}


def trait_cost(lv):
    return round(COST_K * COST_MUL ** lv)


def final_stats(h, alloc=None, tr=None):
    """최종 스탯 = (기본 + 배분) x 환생 특성 배율. JS의 F()와 같은 식."""
    a, t = alloc or {}, tr or {}
    m = lambda k: TRAIT_MUL ** t.get(k, 0)
    return {
        "atk":  (h["atk"] + a.get("atk", 0) * GAIN["atk"]) * m("atk"),
        "hp":   (h["hp"] + a.get("hp", 0) * GAIN["hp"]) * m("hp"),
        "dfn":  (h["dfn"] + a.get("dfn", 0) * GAIN["dfn"]) * m("dfn"),
        "crit": min(75, h["crit"] + a.get("crit", 0) * GAIN["crit"]
                    + t.get("crit", 0) * TRAIT_CRIT),
    }


def turns_to_win(h, b, alloc=None, tr=None):
    """평균 피해 기준 결판 턴수 -> (보스를 잡는 턴, 내가 죽는 턴).
    치명타는 기대값으로 반영. JS 전투와 같은 공식, 난수만 뺐다 = 밸런스 검증용."""
    s = final_stats(h, alloc, tr)
    eff = s["atk"] * (1 + s["crit"] / 100)
    return b["hp"] / max(1, eff - b["dfn"]), s["hp"] / max(1, b["atk"] - s["dfn"])


def _splits(pts):
    for a in range(11):
        for hp in range(11 - a):
            for d in range(11 - a - hp):
                c = 10 - a - hp - d
                yield {"atk": pts*a/10, "hp": pts*hp/10, "dfn": pts*d/10, "crit": pts*c/10}


def reach(h, tr=None, cap=400):
    """이 특성으로 최적 배분을 했을 때 도달 가능한 최고 전역 스테이지."""
    tr = tr or {}
    pts = h["level"] * PT_PER_LEVEL + tr.get("pt", 0) * TRAIT_PT
    best = 0
    for alloc in _splits(pts):
        g = 1
        while g <= cap:
            kill, die = turns_to_win(h, boss(g), alloc, tr)
            if kill >= die:
                break
            g += 1
        best = max(best, g - 1)
    return best


def simulate(h, n_slots, rebirths=40):
    """환생을 거듭했을 때 몇 회차에 몇 층에 닿는지. 상수 튜닝의 근거."""
    import itertools
    souls, tr, rows = 0, {}, []
    cyc = itertools.cycle(["atk", "hp", "dfn", "atk", "hp", "dfn", "crit", "pt"])
    for r in range(rebirths + 1):
        g = reach(h, tr)
        rows.append((r, g, (g - 1) // n_slots + 1, souls, sum(tr.values())))
        souls += sum(round(i ** SOUL_EXP) for i in range(1, g + 1))
        for _ in range(5000):
            k = next(cyc)
            c = trait_cost(tr.get(k, 0))
            if c > souls:
                break
            souls -= c
            tr[k] = tr.get(k, 0) + 1
    return rows


def balance(h, ds):
    n = len(ds)
    print(f"영웅 Lv.{h['level']} HP {h['hp']} ATK {h['atk']} DEF {h['dfn']} "
          f"CRIT {h['crit']}%  (배분 {h['level']*PT_PER_LEVEL}pt, 1층 = {n}스테이지)\n")
    print("[무환생] 전역 스테이지별 판정")
    print(f"{'g':>3} {'층':>2} {'보스':20} {'HP':>8} {'ATK':>6} {'DEF':>5} {'격파턴':>7} {'생존턴':>7}  판정")
    pts = h["level"] * PT_PER_LEVEL
    for g in range(1, n * 2 + 1):
        b, d = boss(g), ds[(g - 1) % n]
        best = min(_splits(pts), key=lambda a: turns_to_win(h, b, a)[0] - turns_to_win(h, b, a)[1])
        kill, die = turns_to_win(h, b, best)
        print(f"{g:>3} {(g-1)//n+1:>2} {d['name'][:20]:20} {b['hp']:>8} {b['atk']:>6} {b['dfn']:>5}"
              f" {kill:>7.1f} {die:>7.1f}  {'승' if kill < die else '패 <- 벽'}")
        if kill >= die and g > n:
            break
    print(f"\n[환생 진행] 특성 비용 {COST_K}x{COST_MUL}^lv, 효과 x{TRAIT_MUL}/lv")
    print(f"{'환생':>4} {'스테이지':>8} {'층':>3} {'보유혼':>10} {'특성합':>6}")
    prev = None
    for r, g, fl, souls, tl in simulate(h, n):
        mark = "   <<< 새 층 진입" if prev is not None and fl > prev else ""
        if prev is None or fl != prev or r % 8 == 0:
            print(f"{r:>4} {g:>8} {fl:>3} {souls:>10} {tl:>6}{mark}")
        prev = fl


def build(root=None, out=None):    # root 는 테스트용 단일 경로
    save_snapshot(root)
    agg, projects, models, hosts = merge()
    h = hero(agg)
    data = {"hero": h, "dungeons": dungeons(projects), "hosts": hosts,
            "providers": sorted(getattr(merge, "providers", {}).items(),
                                key=lambda kv: -kv[1]),
            "models": models.most_common(),
            "k": {"step": STEP, "bhp": B_HP, "batk": B_ATK, "bdef": B_DEF,
                  "tmul": TRAIT_MUL, "cmul": COST_MUL, "ck": COST_K,
                  "soulExp": SOUL_EXP, "tpt": TRAIT_PT, "tcrit": TRAIT_CRIT,
                  "ptPerLevel": PT_PER_LEVEL,
                  "idleDiv": IDLE_DIV, "idleCapH": IDLE_CAP_H,
                  "idleTokenDiv": IDLE_TOKEN_DIV, "idleTokenMax": IDLE_TOKEN_MAX}}
    out = out or game_path()
    tmp = out + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False)))
    os.replace(tmp, out)                        # 브라우저가 반쯤 쓰인 HTML을 읽지 않게
    return out, data


TEMPLATE = r"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Token RPG</title><style>
:root{--bg:#0d1117;--fg:#e6edf3;--dim:#8b949e;--line:#30363d;--gold:#ffd166;
--hp:#f2545b;--xp:#7ee787;--on:#58a6ff;--soul:#c792ea}
*{box-sizing:border-box}body{margin:0;padding:20px 14px;background:var(--bg);color:var(--fg);
font:14px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace}
.wrap{max-width:680px;margin:0 auto}
.card{border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px;background:#161b22}
h2{font-size:12px;color:var(--dim);margin:0 0 12px;text-transform:uppercase;letter-spacing:.08em}
.dim{color:var(--dim)}.gold{color:var(--gold)}.soul{color:var(--soul)}
.hero{display:flex;gap:14px;align-items:center}
.face{font-size:56px;line-height:1}
.hero .meta{flex:1;min-width:0}.hero b{font-size:16px}
.bar{height:12px;border:1px solid var(--line);border-radius:6px;overflow:hidden;background:#0d1117;margin:5px 0}
.bar>i{display:block;height:100%;background:var(--xp);transition:width .35s}
.bar.hpb>i{background:var(--hp)}
.row{display:flex;justify-content:space-between;font-size:11px;gap:8px}
.badge{display:inline-block;border:1px solid var(--line);border-radius:20px;
padding:0 9px;font-size:11px;margin-right:5px}
.alloc{display:grid;grid-template-columns:1fr auto auto auto;gap:6px 10px;align-items:center;font-size:13px}
button{font:inherit;background:#21262d;color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:2px 9px;cursor:pointer}
button:hover:not(:disabled){border-color:var(--on)}button:disabled{opacity:.35;cursor:default}
button.big{padding:7px 14px;width:100%}
button.rb{border-color:var(--soul);color:var(--soul)}
button.on{border-color:var(--on);color:var(--on);background:#1c2531}
.pulse{animation:pulse 2s ease-in-out infinite}
@keyframes pulse{50%{opacity:.45}}
.banner{border:1px dashed var(--xp);border-radius:8px;padding:9px;margin-bottom:10px;
font-size:12px;color:var(--xp)}
.st{display:flex;align-items:center;gap:10px;padding:9px 0;border-top:1px solid var(--line)}
.st:first-of-type{border-top:0}.st .e{font-size:26px;width:32px;text-align:center}
.st .n{flex:1;min-width:0}.st .n b{font-size:13px}.st small{color:var(--dim);font-size:11px}
.lock{opacity:.4}.done .n b{color:var(--xp)}
.wall{border:1px dashed var(--soul);border-radius:8px;padding:10px;margin-top:10px;
font-size:12px;color:var(--soul)}
#fight{position:fixed;inset:0;background:#0d1117ee;display:none;align-items:center;
justify-content:center;padding:16px;z-index:9}
#fight.on{display:flex}
.arena{width:100%;max-width:460px;border:1px solid var(--line);border-radius:12px;
padding:18px;background:#161b22}
.vs{display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:10px}
.vs>div{width:46%}.vs .e{font-size:44px;text-align:center}
.hit{animation:hit .3s}@keyframes hit{50%{transform:translateX(9px) scale(.92);filter:brightness(2)}}
#log{height:132px;overflow-y:auto;font-size:12px;border-top:1px solid var(--line);
margin-top:10px;padding-top:8px}
#log div{margin:1px 0}.crit{color:var(--gold);font-weight:700}.win{color:var(--xp);font-weight:700}
.lose{color:var(--hp);font-weight:700}
</style></head><body><div class="wrap">

<div class="card hero">
  <div class="face" id="face"></div>
  <div class="meta">
    <b id="title"></b> <span class="gold" id="lv"></span>
    <div style="margin:3px 0"><span class="badge soul" id="bRebirth"></span>
      <span class="badge" id="bFloor"></span><span class="badge soul" id="bSouls"></span></div>
    <div class="bar"><i id="xpbar"></i></div>
    <div class="row"><span class="dim" id="exp"></span><span class="dim" id="tonext"></span></div>
    <div class="row" style="margin-top:4px"><span id="sheet"></span></div>
  </div>
</div>

<div class="card">
  <h2>스탯 배분 <span class="gold" id="left"></span></h2>
  <div id="mult" style="margin-bottom:10px;display:flex;gap:6px;align-items:center">
    <span class="dim" style="font-size:11px">한 번에</span></div>
  <div class="alloc" id="alloc"></div>
  <div style="margin-top:12px;display:flex;gap:8px">
    <button id="reset">전부 되돌리기</button>
    <span class="dim" style="font-size:11px;align-self:center">환생하면 초기화된다</span>
  </div>
</div>

<div class="card">
  <h2>원정 <span class="soul" id="expedRate"></span></h2>
  <div id="expedBanner"></div>
  <div id="exped"></div>
</div>

<div class="card">
  <h2>영구 특성 <span class="soul" id="soulsHave"></span></h2>
  <div class="alloc" id="traits"></div>
  <div id="rbBox" style="margin-top:14px"></div>
</div>

<div class="card"><h2 id="floorTitle">던전</h2><div id="stages"></div><div id="wall"></div></div>
<div class="card"><h2>합산된 기기</h2><div id="hosts" style="font-size:12px"></div>
  <div id="provs" style="font-size:12px;margin-top:10px"></div></div>
</div>

<div id="fight"><div class="arena">
  <div class="vs">
    <div><div class="e" id="fh"></div><div class="bar hpb"><i id="fhb"></i></div>
      <div class="row"><span class="dim">나</span><span class="dim" id="fht"></span></div></div>
    <div><div class="e" id="fb"></div><div class="bar hpb"><i id="fbb"></i></div>
      <div class="row"><span class="dim" id="fbn"></span><span class="dim" id="fbt"></span></div></div>
  </div>
  <div id="log"></div>
  <button id="close" class="big" style="margin-top:10px" disabled>전투 중…</button>
</div></div>

<script>
const $ = id => document.getElementById(id);   // id 암시적 전역은 window.close 등과 충돌한다
const D = __DATA__, H = D.hero, G = H.gain, K = D.k, SLOTS = D.dungeons, N = SLOTS.length;
const n = x => Math.round(x).toLocaleString();
// 원정은 초당 소수점 단위로 쌓인다 — 정수로 표시하면 멈춘 것처럼 보인다
const nf = x => x < 1000 ? x.toFixed(2) : n(x);
const MULTIPROV = (D.providers || []).length > 1;
const STATS  = [["atk","공격력","ATK"],["hp","체력","HP"],["dfn","방어력","DEF"],["crit","치명타","CRIT"]];
const TRAITS = [["atk","힘의 유산","ATK x"+K.tmul+"/lv"],["hp","혼의 유산","HP x"+K.tmul+"/lv"],
                ["dfn","벽의 유산","DEF x"+K.tmul+"/lv"],["crit","예지","CRIT +"+K.tcrit+"%p/lv"],
                ["pt","각성","배분 +"+K.tpt+"pt/lv"]];

let save = {alloc:{atk:0,hp:0,dfn:0,crit:0}, cleared:[], souls:0, rebirths:0,
            traits:{atk:0,hp:0,dfn:0,crit:0,pt:0},
            best:0, exped:{since:Date.now(), seenExp:0}};
try { Object.assign(save, JSON.parse(localStorage.getItem("trpg") || "{}")); } catch(e) {}
const put = () => { try { localStorage.setItem("trpg", JSON.stringify(save)); } catch(e) {} };

// 보스: 전역 스테이지 번호의 지수 곡선. 층이 바뀌어도 난이도가 끊기지 않는다.
const boss = g => { const p = Math.pow(K.step, g-1); return {
  hp: Math.round(K.bhp*p), atk: Math.round(K.batk*p),
  dfn: Math.round(K.bdef*p), spd: Math.round(K.bdef*p*0.9) }; };
const soulOf = g => Math.round(Math.pow(g, K.soulExp) * SLOTS[(g-1)%N].soul);
const costOf = lv => Math.round(K.ck * Math.pow(K.cmul, lv));

let mult = 1;                      // 1 | 10 | 100 | "max" — 배분과 특성 구입에 함께 적용
const MULTS = [1, 10, 100, "max"];
// 특성 lv..lv+cnt-1 을 한 번에 사는 총비용
const bulkCost = (k, cnt) => {
  let c = 0;
  for (let i = 0; i < cnt; i++) c += costOf(save.traits[k] + i);
  return c;
};
// 지금 혼으로 살 수 있는 특성 레벨 수 (배수 상한 안에서)
const buyable = k => {
  const cap = mult === "max" ? 1e9 : mult;
  let cnt = 0, c = 0;
  while (cnt < cap) {
    const next = c + costOf(save.traits[k] + cnt);
    if (next > save.souls) break;
    c = next; cnt++;
  }
  return cnt;
};

// ── 원정: 시간이 흐르면 알아서 쌓인다 (오프라인 포함) ──
const CAP_S = K.idleCapH * 3600;
// 마지막 수령 이후 새로 쓴 토큰이 곧 생산 배율이다 — Claude를 쓸수록 많이 캔다
const tokenMul = () => 1 + Math.min(K.idleTokenMax,
  Math.max(0, H.exp - save.exped.seenExp) / K.idleTokenDiv);
const idleRate = () => {                      // 초당 혼
  const g = save.best;          // 역대 최고 — 환생해도 원정은 여기서 계속 캔다
  return g ? soulOf(g) / K.idleDiv * tokenMul() : 0;
};
const idleSecs = () => Math.min(CAP_S, Math.max(0, (Date.now() - save.exped.since) / 1000));
const pending  = () => idleRate() * idleSecs();

const maxCleared = () => save.cleared.length ? Math.max(...save.cleared) : 0;
const markBest = g => { if (g > (save.best || 0)) save.best = g; };
const floorNow   = () => Math.floor(maxCleared() / N) + 1;
const points     = () => H.level * K.ptPerLevel + save.traits.pt * K.tpt;
const used       = () => STATS.reduce((s,[k]) => s + save.alloc[k], 0);
const left       = () => points() - used();
// 최종 스탯 = (토큰이 준 기본값 + 배분) x 환생 특성 배율
const F = k => k === "crit"
  ? Math.min(75, H.crit + save.alloc.crit*G.crit + save.traits.crit*K.tcrit)
  : (H[k] + save.alloc[k]*G[k]) * Math.pow(K.tmul, save.traits[k]);

$("face").textContent = H.emoji; $("title").textContent = H.title;
$("lv").textContent = "Lv." + H.level;
$("xpbar").style.width = H.pct + "%";
$("exp").textContent = n(H.exp) + " EXP";
$("tonext").textContent = "다음까지 " + n(H.toNext);
$("provs").innerHTML = (D.providers || []).length < 2 ? "" :
  '<div class="dim" style="margin-bottom:4px">프로바이더별</div>' +
  D.providers.map(([p,v]) =>
    `<div class="row"><span>${p}</span><span class="gold">${n(v)}</span></div>`).join("");
$("hosts").innerHTML = D.hosts.map(([h,u,v]) =>
  `<div class="row"><span>${h} <span class="dim">${u.slice(0,10)}</span></span><span class="gold">${n(v)}</span></div>`).join("");

function drawHero(){
  $("bRebirth").textContent = "환생 " + save.rebirths + "회";
  $("bFloor").textContent   = floorNow() + "층";
  $("bSouls").textContent   = "혼 " + n(save.souls);
  $("soulsHave").textContent = "보유 " + n(save.souls);
  $("sheet").innerHTML = STATS.map(([k,,s]) =>
    `<span class="dim">${s}</span> <b>${F(k).toFixed(k==="dfn"||k==="crit"?1:0)}</b>${k==="crit"?"%":""}`
  ).join(" &nbsp; ") + ` &nbsp;<span class="dim">SPD</span> <b>${H.spd}</b>`;
  $("left").textContent = "남은 " + left() + "pt";
  $("mult").innerHTML = '<span class="dim" style="font-size:11px">한 번에</span>' +
    MULTS.map(m => `<button data-mul="${m}" class="${m===mult?"on":""}">${m==="max"?"MAX":"x"+m}</button>`).join("") +
    '<span class="dim" style="font-size:11px">배분·특성 구입에 함께 적용</span>';
  $("mult").querySelectorAll("button").forEach(b => b.onclick = () => {
    mult = b.dataset.mul === "max" ? "max" : +b.dataset.mul; drawAll();
  });
  const step = (k, up) => {                 // 이번 클릭으로 실제 움직일 양
    const room = up ? left() : save.alloc[k];
    return mult === "max" ? room : Math.min(mult, room);
  };
  $("alloc").innerHTML = STATS.map(([k,ko,s]) => {
    const dn = step(k, false), up = step(k, true);
    return `<span>${ko} <span class="dim">${s} +${G[k]}/pt</span></span>
     <b class="gold">${save.alloc[k]}</b>
     <button data-m="${k}" ${dn<=0?"disabled":""}>−${dn>1?dn:""}</button>
     <button data-p="${k}" ${up<=0?"disabled":""}>+${up>1?up:""}</button>`;
  }).join("");
  $("alloc").querySelectorAll("button").forEach(b => b.onclick = () => {
    const k = b.dataset.p || b.dataset.m, up = !!b.dataset.p;
    save.alloc[k] += (up ? 1 : -1) * step(k, up); put(); drawAll();
  });
  $("traits").innerHTML = TRAITS.map(([k,ko,eff]) => {
    const cnt = buyable(k);
    const c = cnt ? bulkCost(k, cnt) : costOf(save.traits[k]);
    return `<span>${ko} <span class="dim">${eff}</span></span>
      <b class="soul">Lv.${save.traits[k]}</b>
      <span class="dim" style="font-size:11px">${n(c)}혼</span>
      <button data-t="${k}" ${cnt <= 0 ? "disabled" : ""}>구입${cnt>1?" x"+cnt:""}</button>`;
  }).join("");
  $("traits").querySelectorAll("button").forEach(b => b.onclick = () => {
    const k = b.dataset.t, cnt = buyable(k);
    if (cnt <= 0) return;
    save.souls -= bulkCost(k, cnt); save.traits[k] += cnt; put(); drawAll();
  });
  drawRebirth();
}

let rbArmed = false;   // 환생은 되돌릴 수 없다 -> 두 번 눌러야 실행 (모달 대신)
function drawRebirth(){
  const gain = save.cleared.reduce((s,g) => s + soulOf(g), 0);
  const can = save.cleared.length > 0;
  $("rbBox").innerHTML =
    `<button class="big rb" id="rbBtn" ${can?"":"disabled"}>${
       rbArmed ? `정말 환생한다 — ${floorNow()}층까지의 진행을 버린다 (다시 누르면 실행)`
               : `환생 — 혼 ${n(gain)} 획득`}</button>
     <div class="dim" style="font-size:11px;margin-top:6px">
       클리어 기록과 스탯 배분을 버리고 1층부터 다시 시작한다.
       영구 특성·혼·원정(역대 최고 ${save.best || 0}스테이지 기준)은 그대로 남는다.
       ${can?"":"먼저 스테이지를 하나 이상 클리어해라."}</div>`;
  if (can) $("rbBtn").onclick = () => {
    if (!rbArmed) { rbArmed = true; drawRebirth(); return; }
    rbArmed = false;
    save.souls += gain; save.rebirths++;
    save.cleared = []; STATS.forEach(([k]) => save.alloc[k] = 0);
    put(); drawAll(); window.scrollTo({top:0, behavior:"smooth"});
  };
}

$("reset").onclick = () => { STATS.forEach(([k]) => save.alloc[k]=0); put(); drawAll(); };

function drawStages(){
  const fl = floorNow(), base = (fl - 1) * N;
  $("floorTitle").textContent = `던전 — ${fl}층 (전역 ${base+1}~${base+N} 스테이지)`;
  $("stages").innerHTML = "";
  let blocked = null;
  SLOTS.forEach((slot, i) => {
    const g = base + i + 1, b = boss(g);
    const done = save.cleared.includes(g);
    const open = i === 0 || save.cleared.includes(g - 1);
    const el = document.createElement("div");
    el.className = "st" + (open ? "" : " lock") + (done ? " done" : "");
    el.innerHTML = `<div class="e">${slot.emoji}</div>
      <div class="n"><b>${done?"✓ ":""}${g}. ${slot.name}의 수호자</b>${
        MULTIPROV ? ` <span class="badge">${slot.prov}</span>` : ""}
      <small>HP ${n(b.hp)} · ATK ${n(b.atk)} · DEF ${n(b.dfn)} · SPD ${n(b.spd)}
      ${H.spd>=b.spd?"":"<span style='color:var(--hp)'>· 보스 선공</span>"}
      <br>격파 시 혼 ${n(soulOf(g))}</small></div>`;
    const btn = document.createElement("button");
    btn.textContent = open ? (done ? "재도전" : "도전") : "잠김";
    btn.disabled = !open;
    btn.onclick = () => fightStart(g, slot, b);
    el.appendChild(btn); $("stages").appendChild(el);
    // 평균 피해 기준으로 이길 수 없는 첫 스테이지 = 환생이 필요한 벽
    if (open && !done && blocked === null) {
      const eff = F("atk") * (1 + F("crit")/100);
      if (b.hp / Math.max(1, eff - b.dfn) >= F("hp") / Math.max(1, b.atk - F("dfn"))) blocked = g;
    }
  });
  $("wall").innerHTML = blocked === null ? "" :
    `<div class="wall">벽: ${blocked}스테이지는 지금 능력치로 넘을 수 없다.
     환생해서 영구 특성을 올려라 — 특성 배율은 배분 포인트와 달리 환생해도 사라지지 않는다.</div>`;
}

function drawExped(){
  const g = save.best, rate = idleRate(), have = pending(), secs = idleSecs();
  $("expedRate").textContent = g ? n(rate * 3600) + " 혼/시간" : "";
  if (!g) {
    $("exped").innerHTML = '<div class="dim">스테이지를 하나 클리어하면 원정대가 출발한다.</div>';
    return;
  }
  const full = secs >= CAP_S;
  const hrs = Math.floor(secs/3600), mins = Math.floor(secs/60) % 60;
  $("exped").innerHTML =
    `<div class="row" style="font-size:13px;margin-bottom:8px">
       <span>${g}스테이지를 반복 중${g > maxCleared()
           ? ' <span class="dim">(역대 최고 · 환생 전 기록)</span>' : ""}
         <span class="dim">· 토큰 배율 x${tokenMul().toFixed(2)}</span></span>
       <span class="soul ${full?"":"pulse"}">${nf(have)} 혼</span></div>
     <div class="bar"><i style="width:${100*secs/CAP_S}%;background:var(--soul)"></i></div>
     <div class="row"><span class="dim">${hrs}시간 ${mins}분 경과</span>
       <span class="dim">${full ? "가득 참 — 수령해야 다시 쌓인다" : K.idleCapH + "시간까지 누적"}</span></div>
     <button class="big" id="claim" style="margin-top:10px" ${have < 1 ? "disabled" : ""}>
       수령 — ${nf(have)} 혼</button>
     <div class="dim" style="font-size:11px;margin-top:6px">
       생산량은 클리어한 가장 깊은 스테이지와, 수령 이후 새로 쓴 토큰에 비례한다.</div>`;
  const b = $("claim");
  if (b) b.onclick = () => {
    save.souls += Math.floor(pending());
    save.exped = {since: Date.now(), seenExp: H.exp};
    $("expedBanner").innerHTML = "";
    put(); drawAll();
  };
}

const drawAll = () => { drawHero(); drawExped(); drawStages(); };

// ── 전투: 턴제 자동. 선공은 SPD, 치명타는 thinking 토큰에서 온다.
let timer = null;
function fightStart(g, slot, b){
  clearInterval(timer);
  const me  = {hp:F("hp"), max:F("hp"), atk:F("atk"), dfn:F("dfn"), crit:F("crit")};
  const foe = {hp:b.hp, max:b.hp, atk:b.atk, dfn:b.dfn};
  $("fh").textContent = H.emoji; $("fb").textContent = slot.emoji; $("fbn").textContent = slot.name;
  $("log").innerHTML = ""; $("close").disabled = true; $("close").textContent = "전투 중…";
  $("fight").classList.add("on");
  let turn = 0, myTurn = H.spd >= b.spd;
  say(`${slot.emoji} ${g}스테이지 — ${slot.name}의 수호자가 나타났다!`);
  paint(me, foe);
  timer = setInterval(() => {
    if (++turn > 200) return end(false, me, foe, g, slot, "소모전 — 화력이 부족하다");
    const [a, d, an, tag] = myTurn ? [me, foe, "나", "fb"] : [foe, me, slot.name, "fh"];
    const crit = myTurn && Math.random()*100 < me.crit;
    const dmg = Math.max(1, Math.round((a.atk - d.dfn) * (0.85 + Math.random()*0.3) * (crit ? 2 : 1)));
    d.hp -= dmg;
    say(`T${turn} ${an} → ${n(dmg)} 피해` + (crit ? " <span class='crit'>치명타!</span>" : ""), crit);
    const el = $(tag); el.classList.remove("hit"); void el.offsetWidth; el.classList.add("hit");
    paint(me, foe);
    if (foe.hp <= 0) return end(true, me, foe, g, slot);
    if (me.hp <= 0) return end(false, me, foe, g, slot,
      "토큰을 더 벌거나(build.py 재실행) 환생해서 영구 특성을 올려라");
    myTurn = !myTurn;
  }, 380);
}
function paint(me, foe){
  $("fhb").style.width = Math.max(0, 100*me.hp/me.max) + "%";
  $("fbb").style.width = Math.max(0, 100*foe.hp/foe.max) + "%";
  $("fht").textContent = n(Math.max(0, me.hp)) + "/" + n(me.max);
  $("fbt").textContent = n(Math.max(0, foe.hp)) + "/" + n(foe.max);
}
function say(html, cls){
  $("log").insertAdjacentHTML("beforeend", `<div class="${cls||""}">${html}</div>`);
  $("log").scrollTop = 1e6;
}
function end(won, me, foe, g, slot, why){
  clearInterval(timer); paint(me, foe);
  if (won) {
    say(`<span class="win">승리! ${slot.name} 정복. 혼 ${n(soulOf(g))} 예약.</span>`);
    if (!save.cleared.includes(g)) { save.cleared.push(g); }
    markBest(g); put();
    if (g % N === 0) say(`<span class="win">${g/N}층 완주 — ${g/N+1}층이 열렸다.</span>`);
    say(`<span class="dim">혼은 환생할 때 정산된다.</span>`);
  } else {
    say(`<span class="lose">패배.</span> <span class="dim">${why}</span>`);
  }
  $("close").disabled = false; $("close").textContent = "닫기";
  drawAll();
}
$("close").onclick = () => $("fight").classList.remove("on");

if (!save.best) { save.best = maxCleared(); put(); }        // 옛 저장본 이관
if (!save.exped.seenExp) { save.exped.seenExp = H.exp; put(); }

drawAll();

// 돌아왔을 때 그동안의 성과를 알려준다
(() => {
  const secs = idleSecs();
  if (secs > 300 && save.best) {
    const h = Math.floor(secs/3600), m = Math.floor(secs/60) % 60;
    $("expedBanner").innerHTML =
      `<div class="banner">원정대가 ${h}시간 ${m}분 동안 ${n(pending())} 혼을 캐왔다.</div>`;
  }
})();

// 보고 있는 동안에도 계속 쌓인다
setInterval(() => { if (!$("fight").classList.contains("on")) drawExped(); }, 1000);
</script></body></html>
"""
TEMPLATE = TEMPLATE.replace("__PT__", str(PT_PER_LEVEL))


HOOK_MARK = "token_rpg"          # 우리가 넣은 훅을 식별하는 표식


def hook_command():
    """이 파이썬으로 이 모듈을 돌린다. PATH에 의존하지 않아 훅 환경에서도 안전하다."""
    return f'"{sys.executable}" -m token_rpg build >/dev/null 2>&1 || true'


def _load_settings():
    if not os.path.exists(SETTINGS):
        return {}
    with open(SETTINGS, encoding="utf-8") as f:
        return json.load(f)


def _save_settings(cfg):
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    tmp = SETTINGS + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, SETTINGS)


def install_hook():
    """Claude Code의 Stop 훅에 등록 -> 응답이 끝날 때마다 스탯이 갱신된다."""
    if not os.path.isdir(os.path.dirname(SETTINGS)):
        print(f"Claude Code 설정 폴더가 없다: {os.path.dirname(SETTINGS)}")
        print("Claude Code를 한 번 실행한 뒤 다시 시도해라.")
        return 1
    cfg = _load_settings()
    entries = cfg.setdefault("hooks", {}).setdefault("Stop", [])
    for e in entries:                        # 이미 있으면 명령만 최신으로 고친다
        for h in e.get("hooks", []):
            if HOOK_MARK in h.get("command", ""):
                if h["command"] == hook_command():
                    print("이미 설치돼 있다.")
                    return 0
                h["command"] = hook_command()
                _backup(); _save_settings(cfg)
                print("기존 훅의 경로를 갱신했다.")
                return 0
    entries.append({"hooks": [{
        "type": "command", "command": hook_command(),
        "async": True, "timeout": 30, "statusMessage": "토큰 RPG 스탯 갱신",
    }]})
    _backup(); _save_settings(cfg)
    print(f"Stop 훅 설치 완료 -> {SETTINGS}")
    print("이제 Claude Code가 응답을 마칠 때마다 스탯이 갱신된다.")
    return 0


def _backup():
    if os.path.exists(SETTINGS):
        shutil.copy2(SETTINGS, SETTINGS + ".bak")


def uninstall_hook():
    cfg = _load_settings()
    entries = cfg.get("hooks", {}).get("Stop", [])
    before = sum(len(e.get("hooks", [])) for e in entries)
    for e in entries:
        e["hooks"] = [h for h in e.get("hooks", []) if HOOK_MARK not in h.get("command", "")]
    entries[:] = [e for e in entries if e.get("hooks")]
    if not entries:
        cfg.get("hooks", {}).pop("Stop", None)
        if not cfg.get("hooks"):
            cfg.pop("hooks", None)
    after = sum(len(e.get("hooks", [])) for e in entries)
    if before == after:
        print("설치된 훅이 없다.")
        return 0
    _backup(); _save_settings(cfg)
    print(f"훅 제거 완료 (백업: {SETTINGS}.bak)")
    return 0


def _no_data_hint():
    print("어떤 프로바이더에서도 사용 기록을 찾지 못했다. 찾아본 곳:")
    for pid, spec in PROVIDERS.items():
        for r in PROVIDERS[pid]["roots"]():
            print(f"  {spec['label']:14} {r}")
    print("\n로그가 다른 곳에 있으면:  token-rpg scan add <프로바이더> <경로>")
    print("목록 보기:                token-rpg providers")


def cmd_providers(args):
    cfg = load_config()
    print(f"{'ID':14} {'이름':14} {'상태':6} 스캔 위치")
    for pid, spec in PROVIDERS.items():
        on = cfg.get("providers", {}).get(pid, {}).get("enabled", True)
        roots = provider_roots(pid, cfg)
        state = "켬" if on else "끔"
        note = "" if spec["verified"] else "  (실제 로그 미검증)"
        where = ", ".join(roots) if roots else "— 없음 (설치 안 됨)"
        print(f"{pid:14} {spec['label']:14} {state:6} {where}{note}")
        extra = (cfg.get("providers", {}).get(pid, {}) or {}).get("extra", [])
        for e in extra:
            print(f"{'':36} 추가: {e}")
    print(f"\n설정 파일: {config_path()}")
    return 0


def _cfg_slot(cfg, pid):
    return cfg.setdefault("providers", {}).setdefault(pid, {})


def cmd_scan(args):
    if args.provider not in PROVIDERS:
        print(f"모르는 프로바이더: {args.provider}")
        print("가능한 값: " + ", ".join(PROVIDERS))
        return 1
    cfg = load_config()
    slot = _cfg_slot(cfg, args.provider)
    extra = slot.setdefault("extra", [])
    if args.action == "add":
        if args.path in extra:
            print("이미 등록돼 있다.")
            return 0
        extra.append(args.path)
        save_config(cfg)
        hits = sorted(glob.glob(os.path.expanduser(args.path)))
        print(f"추가: {args.path}")
        print(f"  일치하는 폴더 {len(hits)}개" + (f" — 첫 항목 {hits[0]}" if hits else " (지금은 없음)"))
    elif args.action == "remove":
        if args.path not in extra:
            print("등록돼 있지 않다.")
            return 1
        extra.remove(args.path)
        save_config(cfg)
        print(f"제거: {args.path}")
    return 0


def cmd_toggle(args, on):
    if args.provider not in PROVIDERS:
        print(f"모르는 프로바이더: {args.provider}")
        return 1
    cfg = load_config()
    _cfg_slot(cfg, args.provider)["enabled"] = on
    save_config(cfg)
    print(f"{PROVIDERS[args.provider]['label']} {'켬' if on else '끔'}")
    return 0


def cmd_build(args):
    agg, _, _, _, _ = collect_all()
    if not agg.get("calls") and not glob.glob(os.path.join(snap_dir(), "*.json")):
        _no_data_hint()
        return 1
    path, d = build()
    h = d["hero"]
    if not args.quiet:
        print(f"{path}")
        print(f"Lv.{h['level']} {h['title']} {h['emoji']}  HP {h['hp']} ATK {h['atk']} "
              f"DEF {h['dfn']} CRIT {h['crit']}%  배분 {h['points']}pt  던전 {len(d['dungeons'])}개")
        print(f"합산 기기 {len(d['hosts'])}대")
    return 0


def cmd_open(args):
    rc = cmd_build(args)
    if rc:
        return rc
    webbrowser.open("file://" + game_path())
    return 0


def cmd_status(args):
    """메뉴 막대 앱처럼 사람이 아닌 클라이언트가 읽을 수 있는 현재 요약.

    build를 부르지 않는다. 팝오버를 여는 것만으로 HTML을 다시 쓰거나 게임
    진행을 바꾸지 않게 하기 위해서다.
    """
    agg, projects, _models, days, per = collect_all()
    if not agg.get("calls"):
        print(json.dumps({"ok": False, "error": "no_usage"}, ensure_ascii=False))
        return 1
    h = hero(agg)
    providers = [
        {"id": pid, "name": PROVIDERS[pid]["label"], "tokens": total}
        for pid, total in sorted(per.items(), key=lambda kv: -kv[1])
    ]
    payload = {
        "ok": True,
        "version": __version__,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hero": h,
        "providers": providers,
        "projects": len(projects),
        "days": len(days),
        "gamePath": game_path(),
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="token-rpg",
        description="Claude Code 토큰 사용량으로 성장하는 턴제 RPG")
    p.add_argument("--version", action="version", version=f"token-rpg {__version__}")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("build", help="사용량을 다시 집계해 game.html 갱신 (기본)")
    sub.add_parser("open", help="갱신한 뒤 브라우저로 연다")
    sub.add_parser("status", help="현재 스탯을 JSON으로 출력 (메뉴 막대 앱용)")
    sub.add_parser("export", help="이 PC의 스냅샷만 갱신 (다른 기기와 합산용)")
    sub.add_parser("balance", help="난이도·환생 곡선 표 출력")
    sub.add_parser("selftest", help="집계·병합·밸런스 자체 검증")
    sub.add_parser("where", help="데이터 위치 출력")
    sub.add_parser("install-hook", help="Claude Code Stop 훅에 자동 갱신 등록")
    sub.add_parser("uninstall-hook", help="등록한 훅 제거")
    sub.add_parser("providers", help="프로바이더 목록과 스캔 위치")
    sc = sub.add_parser("scan", help="추가 스캔 폴더 등록/해제")
    sc.add_argument("action", choices=["add", "remove"])
    sc.add_argument("provider")
    sc.add_argument("path", help="폴더 경로. * 와일드카드 가능")
    for name, on in (("enable", True), ("disable", False)):
        q = sub.add_parser(name, help=f"프로바이더 {'켜기' if on else '끄기'}")
        q.add_argument("provider")
    p.add_argument("-q", "--quiet", action="store_true", help="출력 억제")
    a = p.parse_args(argv)

    cmd = a.cmd or "build"
    if cmd == "build":
        return cmd_build(a)
    if cmd == "open":
        return cmd_open(a)
    if cmd == "status":
        return cmd_status(a)
    if cmd == "export":
        print(save_snapshot()[0]); return 0
    if cmd == "balance":
        agg, projects, _, _ = merge()
        if not projects:
            _no_data_hint(); return 1
        balance(hero(agg), dungeons(projects)); return 0
    if cmd == "selftest":
        demo(); return 0
    if cmd == "where":
        print(f"데이터   {data_dir()}")
        print(f"게임     {game_path()}")
        print(f"스냅샷   {snap_dir()}")
        print(f"기록원본 {TRANSCRIPTS}")
        print(f"설정     {SETTINGS}")
        return 0
    if cmd == "providers":
        return cmd_providers(a)
    if cmd == "scan":
        return cmd_scan(a)
    if cmd == "enable":
        return cmd_toggle(a, True)
    if cmd == "disable":
        return cmd_toggle(a, False)
    if cmd == "install-hook":
        return install_hook()
    if cmd == "uninstall-hook":
        return uninstall_hook()
    p.print_help()
    return 1


def demo():
    assert level_of(0) == 1 and level_of(50_000) == 2 and level_of(200_000) == 3
    assert tier_of(1)[2] == "토큰 알" and tier_of(99)[2] == "토큰 드래곤"
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "proj"))
        rec = {"timestamp": "2026-01-01T00:00:00Z", "message": {"id": "a", "model": "claude-opus-5",
               "usage": {"input_tokens": 10, "output_tokens": 20, "cache_creation_input_tokens": 5,
                         "cache_read_input_tokens": 100, "output_tokens_details": {"thinking_tokens": 7}}}}
        with open(os.path.join(d, "proj", "s.jsonl"), "w") as f:
            f.write(json.dumps(rec) + "\n" + json.dumps(rec) + "\n")  # 중복 id -> 1회만
        agg, projects, _, days = collect(d)
        assert agg["calls"] == 1 and agg["input"] == 15 and agg["output"] == 20
        assert agg["thinking"] == 7 and len(days) == 1 and projects["claude-code\tproj"] == 35
        snaps = os.path.join(d, "snaps")
        _, s1 = save_snapshot(d, snaps)
        with open(os.path.join(snaps, "otherpc.json"), "w") as f:
            json.dump({**s1, "host": "otherpc"}, f)
        m, mp, _, hs = merge(snaps)
        assert m["input"] == 30 and mp["claude-code\tproj"] == 70 and m["days"] == 1 and len(hs) == 2

    # 프로바이더 리더: 각자 자기 형식을 제대로 읽는지 픽스처로 확인
    with tempfile.TemporaryDirectory() as d:
        # Codex: total_token_usage 는 세션 누적 -> 마지막 값만 센다
        cx = os.path.join(d, "2026", "09", "10")
        os.makedirs(cx)
        def _tc(inp, cached, out, reason, tot):
            return json.dumps({"timestamp": "2026-09-10T00:00:00Z", "type": "event_msg",
                "payload": {"type": "token_count", "info": {
                    "total_token_usage": {"input_tokens": inp, "cached_input_tokens": cached,
                                          "cache_write_input_tokens": 0, "output_tokens": out,
                                          "reasoning_output_tokens": reason, "total_tokens": tot}}}})
        cum = os.path.join(cx, "rollout-cum.jsonl")
        with open(cum, "w") as f:
            f.write(json.dumps({"type": "session_meta",
                                "payload": {"cwd": "/x/myproj"}}) + "\n")
            f.write(_tc(100, 60, 10, 3, 110) + "\n")
            f.write(_tc(250, 150, 25, 8, 275) + "\n")     # 누적이므로 이 값만 유효
        a1, proj, days, _ = read_codex(cum)
        assert (a1["input"], a1["output"]) == (250, 25), f"누적 처리 실패: {dict(a1)}"
        assert (a1["cache_read"], a1["thinking"]) == (150, 8), dict(a1)
        assert a1["calls"] == 2 and proj == "myproj" and days == {"2026-09-10"}

        # 세부 항목이 비고 total 만 있는 세션 (실제 로그에 존재)
        deg = os.path.join(cx, "rollout-deg.jsonl")
        with open(deg, "w") as f:
            f.write(_tc(0, 0, 0, 0, 17647) + "\n")
        a2, _, _, _ = read_codex(deg)
        assert a2["input"] == 17647 and a2["output"] == 0, dict(a2)

        # 토큰이 전혀 없는 세션은 통계에서 빠진다
        zero = os.path.join(cx, "rollout-zero.jsonl")
        with open(zero, "w") as f:
            f.write(_tc(0, 0, 0, 0, 0) + "\n")
        a3, _, _, _ = read_codex(zero)
        assert not a3, dict(a3)

        # 추가 스캔 폴더가 실제로 반영되는지 (와일드카드 포함)
        cfg = {"providers": {"codex": {"extra": [os.path.join(d, "20*", "*", "*")]}}}
        roots = provider_roots("codex", cfg)
        assert cx in roots, f"추가 스캔 폴더 미반영: {roots}"
        agg, projects, _, _ = collect(pid="codex", roots=[cx])
        assert agg["input"] == 250 + 17647 and projects["codex\tmyproj"] == 275, (dict(agg), dict(projects))
        assert agg["sessions"] == 2, agg["sessions"]      # 빈 세션은 세지 않는다

        # build/save_snapshot 이 기본 인자 탓에 한 프로바이더만 읽는 회귀를 막는다
        import inspect
        for fn in (save_snapshot, build):
            assert inspect.signature(fn).parameters["root"].default is None, \
                f"{fn.__name__}(root=...) 기본값이 None 이 아니면 프로바이더 하나만 읽는다"

        # 끈 프로바이더는 합산에서 빠진다
        off = {"providers": {"claude-code": {"enabled": False}, "codex": {"enabled": False}}}
        a3, _, _, _, per = collect_all(off)
        assert not a3.get("calls") and per == {}, (dict(a3), per)

    # 밸런스: 환생 설계가 성립하는지 검증한다.
    agg, projects, _, _ = merge()
    if not projects:
        print("ok (로컬 데이터 없음 — 밸런스 검증 생략)"); return
    h, ds = hero(agg), dungeons(projects)
    n = len(ds)

    assert [boss(g)["hp"] for g in range(1, 30)] == sorted(boss(g)["hp"] for g in range(1, 30)), \
        "보스 난이도가 단조 증가하지 않는다 (층 경계 급락)"

    # 1) 환생 없이는 얕은 곳에서 막혀야 한다.
    #    토큰이 늘면 캐릭터도 강해지므로 정확한 스테이지 수를 못박으면 안 된다
    #    (사용자마다, 그리고 같은 사용자도 시간이 지나면 달라진다).
    #    지켜야 할 성질은 "벽이 존재하고, 그 벽이 초반에 있다"이다.
    base = reach(h)
    assert base > 0, "1스테이지조차 못 깬다 — 기준 난이도가 과하다"
    assert base <= 2 * n, \
        f"무환생으로 {base}스테이지({(base-1)//n+1}층)까지 간다 — 벽이 너무 늦다"

    # 2) 환생을 거듭해야 더 깊이 간다. 여기서도 절대 층수를 못박지 않는다 —
    #    무환생으로 닿는 층(base_floor)이 사람마다 다르므로 "그 다음 층부터는
    #    환생을 여러 번 해야 한다"는 상대적 성질만 검사한다.
    base_floor = (base - 1) // n + 1
    rows = simulate(h, n, rebirths=32)
    floors = {}
    for r, g, fl, _, _ in rows:
        floors.setdefault(fl, r)
    # 난이도는 STEP^(전역 스테이지)로 연속이고, '층'은 프로젝트 수만큼 묶은 표시
    # 단위일 뿐이다. 프로바이더를 추가하면 층 크기가 변하므로 절대 횟수를 못박지
    # 않고, (a) 다음 층은 환생을 요구한다 (b) 그다음은 눈에 띄게 더 요구한다
    # 두 가지만 본다.
    nxt = floors.get(base_floor + 1, 99)
    assert nxt >= 1, f"{base_floor + 1}층을 환생 없이 간다 — 층 벽(STEP)이 너무 낮다"
    deeper = floors.get(base_floor + 2)
    if deeper is not None:
        assert deeper >= 2 * nxt, \
            f"{base_floor+2}층({deeper}회)이 {base_floor+1}층({nxt}회) 대비 가파르지 않다"
    seq = [floors[f] for f in sorted(floors) if f >= base_floor]
    assert seq == sorted(seq), f"깊은 층이 얕은 층보다 먼저 열린다: {floors}"
    assert rows[-1][1] > base, "환생을 거듭해도 도달 스테이지가 늘지 않는다"

    # 3) 원정(방치)은 보조 수입이어야 한다 — 최대 배율 8시간으로도 환생을 대체 못 함
    soul_of = lambda g: round(g ** SOUL_EXP * ds[(g - 1) % n]["soul"])
    for last in (n, 2 * n, 3 * n):
        idle8 = soul_of(last) / IDLE_DIV * IDLE_CAP_H * 3600 * (1 + IDLE_TOKEN_MAX)
        rebirth = sum(soul_of(g) for g in range(1, last + 1))
        assert idle8 < rebirth * 4, \
            f"{last//n}층 방치 수입 {idle8:.0f}이 환생 {rebirth}의 4배 이상 — IDLE_DIV 상향 필요"

    # 4) 특성 비용은 반드시 증가한다 (무한 구매 방지)
    assert trait_cost(0) < trait_cost(5) < trait_cost(20), "특성 비용이 증가하지 않는다"
    print("ok")


if __name__ == "__main__":
    sys.exit(main())
