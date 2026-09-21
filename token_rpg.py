#!/usr/bin/env python3
"""Claude Code 토큰 사용량으로 굴러가는 턴제 RPG.

설계: 내 스탯은 토큰에 선형으로, 보스 능력치는 토큰의 거듭제곱근(<1)으로 커진다.
따라서 막힌 스테이지는 토큰을 더 쓰면 반드시 넘을 수 있다.
"""
import argparse, base64, collections, glob, hashlib, hmac, json, os, shutil, socket, subprocess, sys, webbrowser
import http.server, threading, time, urllib.request
from datetime import datetime, timedelta, timezone

__version__ = "0.13.0"

# Claude Code가 대화 기록을 남기는 곳. 여기서 usage 필드만 읽는다.
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
TRANSCRIPTS = os.path.join(CLAUDE_DIR, "projects")
SETTINGS = os.path.join(CLAUDE_DIR, "settings.json")

def data_dir():
    """게임 데이터를 두는 곳. 설치 경로(site-packages)에는 절대 쓰지 않는다."""
    base = (os.environ.get("XDG_DATA_HOME")
            or (os.environ.get("LOCALAPPDATA") if os.name == "nt" else None)
            or os.path.expanduser("~/.local/share"))
    d = os.environ.get("TOKEN_RPG_HOME") or os.path.join(base, "token-rpg")
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


# --- 봉인: 진행 저장과 스냅샷은 base64(JSON).HMAC 으로 적는다 ---
# 텍스트 편집기로 열어 혼·환생·토큰 숫자만 고치는 짓을 막는다. 파일은 한 줄 난수처럼 보인다.
# ponytail: 열쇠가 이 파일 안에 있어 소스를 읽으면 위조할 수 있다. 로컬 단인용 게임이라
# 진짜로 막으려면 저장을 서버가 들고 있어야 한다 — 막는 대상은 '파일 열어서 0 하나 더 붙이기'다.
_SEAL_KEY = b"token-rpg/seal/v1"


def _mac(b):
    return hmac.new(_SEAL_KEY, b.encode(), hashlib.sha256).hexdigest()[:16]


def seal(obj):
    b = base64.b64encode(json.dumps(obj, ensure_ascii=False).encode()).decode()
    return b + "." + _mac(b)


def unseal(text):
    """봉인을 푼다. 봉인이 아니거나 고쳐졌으면 None — 옛 평문 파일과 구분해야 해서 예외를 안 쓴다."""
    b, dot, mac = text.strip().partition(".")
    if not dot or not hmac.compare_digest(mac, _mac(b)):
        return None
    try:
        return json.loads(base64.b64decode(b))
    except ValueError:          # binascii.Error 도 ValueError 다
        return None


def _sealed_era(cfg=None):
    """봉인을 쓰기 시작했는가. 한 번 켜지면 평문 파일은 더 안 받는다 —
    계속 받아 주면 평문으로 써넣는 것만으로 봉인을 비켜갈 수 있다."""
    return bool((cfg if cfg is not None else load_config()).get("sealed"))


def _load_sealed(path, strict=True):
    """봉인된 파일을 읽는다. 봉인이 붙어 있는데 서명이 깨졌으면 언제나 거부한다.
    strict 가 거짓이면 평문 JSON 도 받는다 — 스냅샷이 그렇다. 한 PC 안에서도
    설치본이 여러 개라(uv tool·pip·앱 번들) 옛 사본의 훅이 평문으로 덮어쓰는데,
    그걸 거부하면 그 PC 토큰이 통째로 0 이 된다. 스냅샷은 매 훅마다 진짜 로그에서
    다시 만들어지니 잃을 게 없지만, 진행 저장(game.save)은 재생성이 안 돼 strict 로 지킨다."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if text.lstrip().startswith("{"):           # 옛 버전·다른 사본이 남긴 평문
        if strict:
            raise ValueError("봉인이 없다")
        return json.loads(text)
    d = unseal(text)
    if d is None:
        raise ValueError("봉인이 맞지 않는다")
    return d


def _dump_sealed(path, obj):
    tmp = path + f".{os.getpid()}.tmp"       # 원자적 교체: 훅이 동시에 돌 수 있다
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(seal(obj))
    os.replace(tmp, path)


def start_sealing(snaps=None):
    """설치당 한 번: 남아 있던 평문 진행 저장을 봉인해 두고 봉인 시대를 연다.
    이관을 안 하면 업데이트하는 순간 진행(혼·환생·배분)이 통째로 버려진다."""
    cfg = load_config()
    if cfg.get("sealed"):
        return
    try:
        path = save_path(snaps)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if text.lstrip().startswith("{"):
            _dump_sealed(path, json.loads(text))
    except (OSError, ValueError):
        pass                                    # 없거나 깨진 파일은 그대로 둔다
    cfg["sealed"] = True
    save_config(cfg)


def since_ts(cfg=None):
    """이 설치가 언제부터 센 것인지. 처음 돌 때 정한다 — 쌓여 있던 로그로 레벨이
    순간에 치솟으면 성장이 통째로 사라지기 때문이다. 0 이면 전체 기록을 센다."""
    cfg = cfg if cfg is not None else load_config()
    if isinstance(cfg.get("since"), (int, float)):
        return cfg["since"]
    # 이미 쓰던 설치(스냅샷이나 저장이 있다)는 과거를 지킨다. 새 설치만 지금부터.
    used = glob.glob(os.path.join(snap_dir(), "*.json")) or os.path.exists(save_path())
    cfg["since"] = 0 if used else time.time()
    save_config(cfg)
    return cfg["since"]


def snap_dir():
    """PC별 스냅샷 폴더. 클라우드 동기화 폴더로 지정하면 여러 기기가 합산된다."""
    d = os.environ.get("TOKEN_RPG_SNAPSHOTS") or os.path.join(data_dir(), "snapshots")
    os.makedirs(d, exist_ok=True)
    return d


def save_path(snaps=None):
    """게임 진행 저장 파일 하나. 스냅샷 폴더에 두므로 그 폴더를 동기화 폴더로 지정하면
    기기끼리도 같은 저장을 쓴다. 확장자가 .json 이 아니라 스냅샷 합산에 섞이지 않는다."""
    return os.path.join(snaps or snap_dir(), "game.save")


def read_save(snaps=None):
    """{"rev": n, "save": {...}}. 파일이 없으면 rev 0.
    깨졌거나 봉인이 맞지 않는 파일은 ValueError 를 올린다 — 동기화 중인 반쪽 파일을 새 저장으로 덮어 진행을 날리지 않게."""
    try:
        d = _load_sealed(save_path(snaps), strict=_sealed_era())
    except FileNotFoundError:
        return {"rev": 0}
    if not isinstance(d, dict) or type(d.get("rev")) is not int:
        raise ValueError("저장 파일 형식이 아니다")
    return d


_save_lock = threading.Lock()


def write_save(base, save, snaps=None):
    """낙관적 잠금: 클라이언트가 읽었던 rev 와 지금 파일의 rev 가 같을 때만 쓴다.
    다르면 (False, 현재 저장)을 돌려준다 — 오래된 창이 새 진행을 덮어쓰지 못한다.
    ponytail: 한 프로세스 안의 잠금뿐. 두 기기가 같은 순간에 쓰면 동기화 서비스의 충돌 사본에 맡긴다."""
    with _save_lock:
        cur = read_save(snaps)
        if cur["rev"] != base:
            return False, cur
        new = {"rev": base + 1, "save": save}
        _dump_sealed(save_path(snaps), new)
        return True, new

# --- 밸런스 조절 손잡이 (python3 build.py --balance 로 확인) ---
# 보스 능력치는 '전역 스테이지 번호'의 지수 곡선이다. 층 경계에서 난이도가
# 끊기지 않고, 한 층(=고정 보스 15종)을 돌 때마다 STEP^N 배씩 벽이 높아진다.
STEP = 1.28                              # 스테이지 1칸당 보스 배율
B_HP, B_ATK, B_DEF = 115, 24, 8          # 1스테이지 보스 기준치
# 극초반 벽 낮추기: 토큰이 0 인 Lv.1 은 ATK 도 0 이라 DEF 8 인 1스테이지 보스에게 데미지가
# 한 점도 안 들어갔다. 1스테이지를 이기는 데 누적 200만 토큰이 필요했고, 그전까지 게임이
# 시작조차 안 됐다. EARLY_G 까지는 배율을 눌러 두고 거기서 원래 곡선에 합류시킨다.
EARLY_G   = 6                            # 여기부터는 원래 곡선 그대로
EARLY_MUL = 0.12                         # 1스테이지 배율 (EARLY_G 에서 1.0 으로 복귀)
PT_PER_LEVEL = 2                         # 레벨업당 자유 배분 포인트
# 포인트 1점당 상승치 (cdmg 는 %p). SPD 3 은 보스 SPD 와 같은 자리수에 놓으려고 고른 값이다 —
# 토큰만으로 얻는 SPD(호출수/400)는 13스테이지 보스의 1/9 이라, 배분 없이 신속의 유산만으로
# 뒤집으려면 20레벨(혼 6,475)이 든다. 같은 혼이면 힘의 유산 ATK x9.65 다. 그건 선택이 아니다.
GAIN = {"atk": 1, "hp": 12, "dfn": 0.6, "crit": 0.4, "cdmg": 1, "spd": 3}
CRIT_CAP  = 100      # 치명타율 상한(%). 넘친 %p 는 치명타 피해 %p 로 옮겨 간다 — 올려도 헛되지 않게
CDMG_BASE = 200      # 치명타 피해 기본값(%) = 예전 고정 2배
# '파괴의 유산'은 치명타 피해에 TRAIT_MUL 을 곱한다. 전에는 레벨당 +5%p 가산이었는데,
# 보스가 스테이지마다 x1.28 로 곱해지는 곡선이라 가산은 따라가지 못했다 — 혼 23,455 를
# 부어도 도달 스테이지가 9 에서 9 로 그대로였다(같은 혼이면 힘의 유산이 16). 곱으로 바꾸면
# 힘의 유산보다 한 칸 뒤에 서고, CRIT 이 높을수록 좁혀진다 = 빌드가 고르는 선택이 된다.
# demo() 의 _check_cdmg 가 이 성질을 붙들고 있다.

# 환생: 클리어 기록과 배분을 버리고 '혼'을 얻어 영구 특성을 산다.
# 특성 효과는 배율(지수), 비용도 지수 -> 층 벽을 넘으려면 환생을 거듭해야 한다.
TRAIT_MUL  = 1.12    # 특성 1레벨당 스탯 배율
COST_MUL   = 1.13    # 특성 1레벨당 비용 증가율
COST_K     = 80      # 특성 1레벨 기본 비용
SOUL_EXP   = 2.4     # 혼 획득 = 스테이지번호^SOUL_EXP
TRAIT_PT   = 4       # '각성' 1레벨당 배분 포인트
# '수확' 1레벨당 혼 획득 +%p. 3 이면 예지를 뺀 만큼(3층 27회)이 원래 속도(23회)로 돌아온다.
# 더 올리면 층 벽이 무너진다 — 6 에서 19회, 10 에서 17회. simulate() 로 잰 값이다.
TRAIT_SOUL = 3
# '신속' 은 다른 유산과 같은 배율(TRAIT_MUL). SPD 는 선공만 정하고 평균 피해 모델에는
# 안 들어가서 reach()·simulate() 이 값을 못 재는데, 효과가 '한 대 먼저'로 묶여 있어 괜찮다.
# TRAIT_CRIT 은 없앴다 — CRIT 은 상한 100%가 있어 영구 특성으로 두면 되팔 수 없는 함정이 된다.
# 치명타는 토큰(thinking)·배분 포인트·유물로만 오른다.

# 환생 기운: 환생 1회에 마지막 환생 이후 새로 쓴 토큰(EXP)이 이만큼 필요하다.
# 진행 속도를 실제 사용량에 묶는다 — 하루 150~250만 쓰면 하루 2회 안팎, 1층(환생 5~6회)에 2~3일.
# 게임을 시작하기 전에 쓴 토큰은 세지 않는다 (높은 레벨로 시작해도 몰아서 환생 못 함).
REBIRTH_EXP = 1_000_000
REBIRTH_CAP = 3      # 기운은 3회분까지만 쌓인다 — 며칠 쉬고 와도 한 번에 몰아치지 못하게
REBIRTH_SOUL = 0.02  # 환생 1회마다 얻는 혼 +2% (선형 누적). 옛 축복(판마다 ATK+30%·혼+50% 등)을 대신한다

# 미션 보상 예산. 단위는 '최고 스테이지 보스 격파 몇 번분'이고, 그날의 미션들이 가중치로
# 나눠 갖는다. 미션을 쪼개도 총합이 그대로라 진행 속도가 빨라지지 않는다.
# 환생이 주 수입원이어야 하므로 demo() 에서 환생 수입과 비교해 상한을 지킨다.
DAY_BUDGET, WEEK_BUDGET = 5, 20

# 미니게임(혼 사냥): 손으로 직접 벌어가는 칸. 단위는 미션과 같다(최고 스테이지 보스 격파 몇 번분).
# 하루 예산이 고정이라 아무리 잘해도 진행 속도가 흔들리지 않는다 — 실력은 '얼마나 빨리 하루치를
# 채우느냐'만 바꾼다. MINI_BEST 는 과녁 한가운데(정확)일 때 배수다.
MINI_TRIES, MINI_BUDGET, MINI_BEST = 3, 2, 1.5
MINI_SHOTS = 5       # 한 판에 쏘는 횟수. 발마다 표식이 빨라진다 — 한 번 눌러 끝나면 게임이 아니다

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
# 고정 보스 15종 = 한 층 15스테이지 (1층을 다 깨는 데 환생 5~6회 ≈ 2~3일).
# 층마다 같은 순서로 다시 나오고, 능력치는 boss()가 전역 스테이지 번호로 정한다.
# 세 번째 칸은 그 보스가 떨어뜨리는 유물의 능력치다. 고정이라 "어디를 먼저 깰까"가 선택이 된다.
# 5종을 3개씩 나누고 층 앞뒤로 흩어 놔, 어떤 빌드를 노리든 초반에 목표가 하나는 있게 한다.
BOSSES = [("버그 벌레", "🐛", "atk"), ("무한 루프 뱀", "🐍", "crit"), ("널 포인터 박쥐", "🦇", "dfn"),
          ("레거시 전갈", "🦂", "hp"), ("좀비 프로세스", "🧟", "soul"), ("메모리 누수 슬라임", "🦠", "hp"),
          ("머지 충돌 도깨비", "👹", "crit"), ("스파게티 크라켄", "🐙", "atk"), ("데드락 골렘", "🗿", "dfn"),
          ("레이스 컨디션 유령", "👻", "crit"), ("의존성 지옥 악마", "😈", "soul"),
          ("스택 오버플로 히드라", "🐉", "atk"), ("기술 부채 리치", "💀", "dfn"),
          ("프로덕션 장애 드래곤", "🐲", "hp"), ("토큰 한도의 군주", "👑", "soul")]


# 레벨 곡선 = LV_EXP * x^3/(x+LV_EASE), x = lv-1. 예전 제곱 곡선(LV_EXP 5만)을 저렙 쪽만
# 완만하게 눌러 놓은 모양이다 — Lv.2 가 9배 빠르고, Lv.32 에서 1.18배, Lv.70 위로는 사실상 같다.
# LV_EXP 54,600 은 Lv.99 총량(=초월 비용)을 예전 4.8억에 맞춰 고른 값이다. 후반과 초월은 그대로 두고
# 극초반만 손본다. level_of 는 이 식의 역함수를 닫힌 꼴로 못 구해 훑어서 찾는다(최대 99칸).
LV_EXP  = 54_600                         # 레벨 1칸의 EXP 기울기
LV_EASE = 9                              # 저렙 완만화. 0 이면 예전 제곱 곡선 그대로
MAX_LV  = 99                             # 여기서 막힌다 — 넘기려면 초월한다


def exp_for(lv):
    x = lv - 1
    return int(LV_EXP * x ** 3 / (x + LV_EASE))

def level_of(exp):
    exp, lv = max(exp, 0), 1
    while lv < MAX_LV and exp_for(lv + 1) <= exp:
        lv += 1
    return lv

TRANS_EXP = exp_for(MAX_LV)              # 초월 한 번에 들어가는 EXP

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


def _day(ts):
    """ISO 타임스탬프 -> 로컬 날짜. '오늘' 미션이 UTC 가 아니라 내 시간 자정에 바뀌도록."""
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone().date().isoformat()
    except ValueError:
        return str(ts)[:10]


def read_claude(path):
    """Claude Code: ~/.claude/projects/<프로젝트>/<세션>.jsonl
    message.usage 를 읽고 message.id 로 중복(재시도·사이드체인)을 제거한다."""
    agg, days, models, seen = collections.Counter(), collections.Counter(), collections.Counter(), set()
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
        if ts:                          # 날짜별 사용량 (미션용) — 자정을 넘긴 세션도 날짜대로 나뉜다
            days[_day(ts)] += (u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                               + u.get("output_tokens", 0))
    proj = os.path.basename(os.path.dirname(path))
    return agg, proj, days, models


def _codex_fresh(t):
    """이번 세션에서 **새로 처리한** 입력 토큰. Codex 의 input_tokens 는 캐시 재사용분을
    품고 있다(cached_input_tokens 가 그 부분집합이고, input + output == total 이 성립한다).
    Claude 는 input_tokens 가 캐시를 뺀 값이고 cache_read 가 따로라, 그대로 더하면 Codex 만
    같은 일을 하고도 EXP 를 몇 배로 받는다 — 실제 로그에서 input 의 67~97% 가 캐시였다.
    cache_write 는 실측에서 늘 0 이고 input 밖이라는 증거도 없어 따로 더하지 않는다."""
    return max(0, t.get("input_tokens", 0) - t.get("cached_input_tokens", 0))


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
    last, days, models, cwd, n_events = None, collections.Counter(), collections.Counter(), None, 0
    seen_v = 0
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
            # 누적값이 늘어난 만큼을 그 이벤트의 날짜에 준다 -> 날짜별 사용량 (미션용)
            v = _codex_fresh(tot) + tot.get("output_tokens", 0) or tot.get("total_tokens", 0)
            ts = d.get("timestamp") or ""
            if ts and v > seen_v:
                days[_day(ts)] += v - seen_v
            seen_v = max(seen_v, v)

    agg = collections.Counter()
    if last:
        agg["input"] = _codex_fresh(last)
        agg["output"] = last.get("output_tokens", 0)
        agg["cache_read"] = last.get("cached_input_tokens", 0)
        agg["thinking"] = last.get("reasoning_output_tokens", 0)
        # 세부 항목이 비고 total 만 채워진 세션이 실제로 있다. total = input + output
        # 이므로 분해가 없으면 통째로 input 으로 넣는다 (EXP 는 맞고 ATK 는 과장하지 않는다).
        if not any(agg[k] for k in ("input", "output", "cache_read", "thinking")):
            agg["input"] = last.get("total_tokens", 0)
        agg["calls"] = n_events
    if not agg["input"] and not agg["output"]:
        return collections.Counter(), "codex", collections.Counter(), models
    name = os.path.basename((cwd or "").rstrip("/")) or "codex"
    return agg, name, days, models


def read_gemini(path):
    """Gemini CLI: ~/.gemini/tmp/<프로젝트>/chats/session-*.jsonl

    실제 로그(cli 0.59.0)로 확인한 구조: 메시지 한 줄마다
      {"id", "timestamp", "type": "gemini", "model",
       "tokens": {input, output, cached, thoughts, tool, total}}
    tokens 는 답변 한 번의 사용량(누적 아님)이라 전부 더한다. 같은 id 가
    다시 기록되면 마지막 값만 쓴다. total = input + output + thoughts 이고
    input 은 cached 를 포함한다 -> Claude 기준(입력=캐시 제외, 출력=생각 포함)에 맞춘다.
    프로젝트 이름은 <프로젝트>/.project_root 에 적힌 경로의 마지막 조각.
    """
    msgs = {}
    for line in _lines(path):
        if '"tokens"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d.get("tokens"), dict):
            msgs[d.get("id") or len(msgs)] = d
    agg, days, models = collections.Counter(), collections.Counter(), collections.Counter()
    for d in msgs.values():
        t = d["tokens"]
        cached = t.get("cached", 0)
        agg["input"] += max(0, t.get("input", 0) - cached) + t.get("tool", 0)
        agg["output"] += t.get("output", 0) + t.get("thoughts", 0)
        agg["cache_read"] += cached
        agg["thinking"] += t.get("thoughts", 0)
        agg["calls"] += 1
        models[d.get("model") or "unknown"] += 1
        if d.get("timestamp"):
            days[_day(d["timestamp"])] += (max(0, t.get("input", 0) - cached) + t.get("tool", 0)
                                           + t.get("output", 0) + t.get("thoughts", 0))
    if not agg["input"] and not agg["output"]:
        return collections.Counter(), "gemini", collections.Counter(), models
    proj_dir = os.path.dirname(os.path.dirname(path))
    try:
        with open(os.path.join(proj_dir, ".project_root"), encoding="utf-8") as f:
            name = os.path.basename(f.read().strip().rstrip("/"))
    except OSError:
        name = os.path.basename(proj_dir)
    return agg, name or "gemini", days, models


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
    "gemini": {
        "label": "Gemini CLI",
        "roots": lambda: [os.path.expanduser("~/.gemini/tmp")],
        "glob": os.path.join("*", "chats", "session-*.jsonl"),
        "read": read_gemini,
        "verified": True,       # 실제 로그(gemini cli 0.59.0)로 검증
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


def collect(root=None, pid="claude-code", roots=None, since=0):
    """한 프로바이더의 로그를 훑어 합계를 낸다.
    since 이전에 끝난 세션 파일은 건너뛴다 — 세션 단위라 기준을 걸친 파일 하나는 통째로 센다."""
    spec = PROVIDERS[pid]
    agg = collections.Counter()
    days, projects, models = collections.Counter(), collections.Counter(), collections.Counter()  # days: 날짜 -> 토큰
    files = 0
    for r in ([root] if root else (roots if roots is not None else provider_roots(pid))):
        for path in glob.glob(os.path.join(r, spec["glob"]), recursive=True):
            try:
                if since and os.path.getmtime(path) < since:
                    continue
            except OSError:
                continue
            a, proj, d, m = spec["read"](path)
            if not a:
                continue
            files += 1
            agg.update(a); days.update(d); models.update(m)
            projects[pid + "\t" + proj] += a["input"] + a["output"]
    agg["sessions"] = files
    return agg, projects, models, days


def collect_all(cfg=None):
    """켜져 있는 프로바이더 전부를 합산한다. days = {날짜: {프로바이더: 토큰}}"""
    cfg = cfg if cfg is not None else load_config()
    agg = collections.Counter()
    projects, models, days, per = collections.Counter(), collections.Counter(), {}, {}
    for pid in PROVIDERS:
        if not cfg.get("providers", {}).get(pid, {}).get("enabled", True):
            continue
        roots = provider_roots(pid, cfg)
        if not roots:
            continue
        a, p, m, d = collect(pid=pid, roots=roots, since=since_ts(cfg))
        if not a.get("calls"):
            continue
        agg.update(a); projects.update(p); models.update(m)
        for day, v in d.items():
            days.setdefault(day, {})[pid] = v
        per[pid] = a["input"] + a["output"]
    return agg, projects, models, days, per


def save_snapshot(root=None, snaps=None):   # root 는 테스트용 단일 경로
    """이 PC의 집계만 작은 JSON으로 남긴다. 190MB 트랜스크립트는 옮기지 않는다."""
    snaps = snaps or snap_dir()
    os.makedirs(snaps, exist_ok=True)      # 명시로 넘긴 경로도 없으면 만든다
    start_sealing(snaps)                   # 옛 평문 파일 이관 — 설치당 한 번만 돈다
    if root:                                # 테스트용 단일 경로
        agg, projects, models, days = collect(root)
        per = {"claude-code": agg["input"] + agg["output"]}
        days = {k: {"claude-code": v} for k, v in days.items()}
    else:
        agg, projects, models, days, per = collect_all()
    recent = (datetime.now() - timedelta(days=60)).date().isoformat()   # 스냅샷을 작게 유지
    snap = {"host": socket.gethostname(),
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "agg": dict(agg), "projects": dict(projects), "models": dict(models),
            "days": sorted(days), "providers": per,
            "daily": {k: v for k, v in days.items() if k >= recent}}
    path = os.path.join(snaps, snap["host"].replace(os.sep, "_") + ".json")
    try:                                    # 값이 그대로면 updated 도 그대로 둔다
        old = _load_sealed(path, strict=False)
        if {k: v for k, v in old.items() if k != "updated"} == \
           {k: v for k, v in snap.items() if k != "updated"}:
            return path, old
    except (OSError, ValueError):
        pass
    _dump_sealed(path, snap)
    return path, snap


def merge(snaps=None):
    """snapshots/*.json 전부 합산. 파일당 PC 하나라 중복 없음."""
    snaps = snaps or snap_dir()
    agg, projects, models = collections.Counter(), collections.Counter(), collections.Counter()
    days, hosts, provs = set(), [], {}
    daily = collections.defaultdict(collections.Counter)      # 날짜 -> 프로바이더 -> 토큰
    for p in sorted(glob.glob(os.path.join(snaps, "*.json"))):
        try:
            snap = _load_sealed(p, strict=False)
        except (OSError, ValueError) as e:      # 봉인이 붙어 있는데 서명이 깨진 것만 뺀다
            print(f"스냅샷 건너뜀 {os.path.basename(p)}: {e}", file=sys.stderr)
            continue
        agg.update(snap["agg"]); projects.update(snap["projects"]); models.update(snap["models"])
        days |= set(snap.get("days", []))
        for day, per in (snap.get("daily") or {}).items():
            daily[day].update(per)
        for k, v in (snap.get("providers") or {}).items():
            provs[k] = provs.get(k, 0) + v
        hosts.append((snap["host"], snap["updated"],
                      snap["agg"].get("input", 0) + snap["agg"].get("output", 0)))
    agg["days"] = len(days)
    merge.providers = provs        # 부가 정보 — 호출부가 필요할 때만 본다
    merge.daily = {d: dict(c) for d, c in sorted(daily.items())}
    return agg, projects, models, hosts


def hero(agg, trans=0):
    """토큰 종류가 스탯을 정한다 — 사용 패턴이 곧 캐릭터 빌드.

    trans 는 초월 횟수. 레벨만 그만큼 되돌리고, 준 배분 포인트는 적립해 둔다
    (JS 의 lvNow()·pointsOf() 와 같은 식이어야 메뉴 막대 배지가 어긋나지 않는다)."""
    total = agg["input"] + agg["output"]
    trans = max(0, min(int(trans or 0), total // TRANS_EXP))
    exp = total - trans * TRANS_EXP
    lv = level_of(exp)
    cur, nxt = exp_for(lv), exp_for(lv + 1)
    return {
        "exp": exp, "total": total, "trans": trans,
        "level": lv, "points": (trans * MAX_LV + lv) * PT_PER_LEVEL,
        "pct": round(100 * (exp - cur) / max(nxt - cur, 1), 1), "toNext": nxt - exp,
        "canTrans": lv >= MAX_LV,
        "emoji": tier_of(lv)[1], "title": tier_of(lv)[2],
        "hp":   round(100 + agg["output"] / 8_000),
        "atk":  round(agg["output"] / 60_000),
        "dfn":  round(agg["cache_read"] / 30_000_000, 1),
        "crit": round(min(50, agg["thinking"] / 60_000), 1),
        "spd":  round(agg["calls"] / 400),
        "gain": GAIN, "raw": dict(agg),
    }


def dungeons():
    """한 층의 스테이지 슬롯 = 고정 보스. 누구 PC 에서든 같은 던전이다 (프로젝트 이름을 드러내지 않는다).
    능력치는 boss()가 정하고, 여기선 이름·이모지·혼 배수(층 뒤쪽 보스일수록 0.7 -> 1.3)만 준다."""
    n = len(BOSSES)
    return [{"slot": i + 1, "name": name, "emoji": emoji, "affix": affix,
             "soul": round(0.7 + 0.6 * i / (n - 1), 2)}
            for i, (name, emoji, affix) in enumerate(BOSSES)]


def boss(g):
    """전역 스테이지 번호 g(1부터) -> 보스 능력치. JS 의 boss() 와 같은 식이어야 한다."""
    p = STEP ** (g - 1)
    if g < EARLY_G:                       # 초반 램프 — 지수 보간이라 단조 증가가 유지된다
        p *= EARLY_MUL ** ((EARLY_G - g) / (EARLY_G - 1))
    return {"hp": round(B_HP * p), "atk": round(B_ATK * p),
            "dfn": round(B_DEF * p), "spd": round(B_DEF * p * 0.9)}


def trait_cost(lv):
    return round(COST_K * COST_MUL ** lv)


def final_stats(h, alloc=None, tr=None):
    """최종 스탯 = (기본 + 배분) x 환생 특성 배율. JS의 F()와 같은 식."""
    a, t = alloc or {}, tr or {}
    m = lambda k: TRAIT_MUL ** t.get(k, 0)
    crit = h["crit"] + a.get("crit", 0) * GAIN["crit"]
    return {
        "atk":  (h["atk"] + a.get("atk", 0) * GAIN["atk"]) * m("atk"),
        "hp":   (h["hp"] + a.get("hp", 0) * GAIN["hp"]) * m("hp"),
        "dfn":  (h["dfn"] + a.get("dfn", 0) * GAIN["dfn"]) * m("dfn"),
        "crit": min(CRIT_CAP, crit),
        "cdmg": (CDMG_BASE + a.get("cdmg", 0) * GAIN["cdmg"]
                 + max(0, crit - CRIT_CAP)) * m("cdmg"),
    }


def turns_to_win(h, b, alloc=None, tr=None):
    """평균 피해 기준 결판 턴수 -> (보스를 잡는 턴, 내가 죽는 턴).
    치명타는 기대값으로 반영. JS 전투와 같은 공식, 난수만 뺐다 = 밸런스 검증용."""
    s = final_stats(h, alloc, tr)
    eff = s["atk"] * (1 + s["crit"] / 100 * (s["cdmg"] / 100 - 1))
    return b["hp"] / max(1, eff - b["dfn"]), s["hp"] / max(1, b["atk"] - s["dfn"])


def _splits(pts):
    for a in range(11):
        for hp in range(11 - a):
            for d in range(11 - a - hp):
                for c in range(11 - a - hp - d):
                    x = 10 - a - hp - d - c
                    # SPD 는 빼 둔다 — turns_to_win 이 선공을 모델에 안 넣어서 값을 못 재고,
                    # '배분을 SPD 에 쓰지 않는다'가 층 벽 높이로는 안전한 쪽 가정이다.
                    yield {"atk": pts*a/10, "hp": pts*hp/10, "dfn": pts*d/10,
                           "crit": pts*c/10, "cdmg": pts*x/10}


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
    # 신속은 빠졌다 — 평균 피해 모델이 SPD 를 안 보므로 넣어도 0 으로 값이 매겨진다
    cyc = itertools.cycle(["atk", "hp", "dfn", "atk", "hp", "dfn", "cdmg", "cdmg", "pt", "soul"])
    for r in range(rebirths + 1):
        g = reach(h, tr)
        rows.append((r, g, (g - 1) // n_slots + 1, souls, sum(tr.values())))
        souls += round(sum(round(i ** SOUL_EXP * (1 + REBIRTH_SOUL * r)) for i in range(1, g + 1))
                       * (1 + TRAIT_SOUL / 100 * tr.get("soul", 0)))
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
    print(f"환생 1회 = 새 토큰 {REBIRTH_EXP:,} (최대 {REBIRTH_CAP}회분 적립)")
    print(f"{'환생':>4} {'스테이지':>8} {'층':>3} {'보유혼':>10} {'특성합':>6} {'필요토큰':>12}")
    prev = None
    for r, g, fl, souls, tl in simulate(h, n):
        mark = "   <<< 새 층 진입" if prev is not None and fl > prev else ""
        if prev is None or fl != prev or r % 8 == 0:
            print(f"{r:>4} {g:>8} {fl:>3} {souls:>10} {tl:>6} {r * REBIRTH_EXP:>12,}{mark}")
        prev = fl


def build(root=None, out=None):    # root 는 테스트용 단일 경로
    save_snapshot(root)
    agg, projects, models, hosts = merge()
    h = hero(agg)
    data = {"hero": h, "dungeons": dungeons(), "hosts": hosts,
            "providers": sorted(getattr(merge, "providers", {}).items(),
                                key=lambda kv: -kv[1]),
            "models": models.most_common(),
            "daily": getattr(merge, "daily", {}),
            "k": {"step": STEP, "bhp": B_HP, "batk": B_ATK, "bdef": B_DEF,
                  "earlyG": EARLY_G, "earlyMul": EARLY_MUL, "lvEase": LV_EASE,
                  "tmul": TRAIT_MUL, "cmul": COST_MUL, "ck": COST_K,
                  "soulExp": SOUL_EXP, "tpt": TRAIT_PT, "tsoul": TRAIT_SOUL,
                  "critCap": CRIT_CAP, "cdmgBase": CDMG_BASE,
                  "ptPerLevel": PT_PER_LEVEL, "rbExp": REBIRTH_EXP, "rbCap": REBIRTH_CAP,
                  "maxLv": MAX_LV, "lvExp": LV_EXP, "tiers": TIERS,
                  "rbSoul": REBIRTH_SOUL,
                  "dayBudget": DAY_BUDGET, "weekBudget": WEEK_BUDGET,
                  "idleDiv": IDLE_DIV, "idleCapH": IDLE_CAP_H,
                  "idleTokenDiv": IDLE_TOKEN_DIV, "idleTokenMax": IDLE_TOKEN_MAX,
                  "miniTries": MINI_TRIES, "miniBudget": MINI_BUDGET, "miniBest": MINI_BEST,
                  "miniShots": MINI_SHOTS}}
    out = out or game_path()
    html = TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False))
    try:                                        # 내용이 같으면 건드리지 않는다 — mtime 이 바뀌면
        with open(out, encoding="utf-8") as f:  # 메뉴 막대 앱이 페이지를 다시 읽어 화면 상태가 날아간다
            if f.read() == html:
                return out, data
    except OSError:
        pass
    tmp = out + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, out)                        # 브라우저가 반쯤 쓰인 HTML을 읽지 않게
    return out, data


TEMPLATE = r"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Token RPG</title><style>
:root{--bg:#0d1117;--fg:#e6edf3;--dim:#8b949e;--line:#30363d;--gold:#ffd166;
--hp:#f2545b;--xp:#7ee787;--on:#58a6ff;--soul:#c792ea;--myth:#ff5edb}
*{box-sizing:border-box}body{margin:0;padding:20px 14px;background:var(--bg);color:var(--fg);
font:14px/1.55 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR",sans-serif;
font-variant-numeric:tabular-nums}
.wrap{max-width:680px;margin:0 auto}
.card{border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px;background:#161b22}
h2{font-size:12px;color:var(--dim);margin:0 0 12px;letter-spacing:.02em}
.dim{color:var(--dim)}.gold{color:var(--gold)}.soul{color:var(--soul)}
.hero{display:flex;gap:14px;align-items:center}
.face{font-size:56px;line-height:1}
.hero .meta{flex:1;min-width:0}.hero b{font-size:16px}
.bar{height:12px;border:1px solid var(--line);border-radius:6px;overflow:hidden;background:#0d1117;margin:5px 0}
.bar>i{display:block;height:100%;background:var(--xp);transition:width .35s}
.bar.hpb>i{background:var(--hp)}
.row{display:flex;justify-content:space-between;font-size:11px;gap:8px}
.sheet{display:grid;grid-template-columns:repeat(3,auto);justify-content:start;gap:2px 14px;
font-size:12px;margin-top:6px}.sheet>span{white-space:nowrap}
.badge{display:inline-block;border:1px solid var(--line);border-radius:20px;
padding:0 9px;font-size:11px;margin-right:5px}
.alloc{display:grid;gap:6px;align-items:center;font-size:13px}
#alloc{grid-template-columns:1fr auto auto auto auto auto auto}#traits{grid-template-columns:1fr auto auto auto auto}
.alloc small{display:block;color:var(--dim);font-size:11px;white-space:nowrap}
.alloc>b{min-width:34px;text-align:right}.alloc button{white-space:nowrap;padding:2px 7px}
details.card>summary{list-style:none;cursor:pointer;display:flex;align-items:baseline;gap:6px;user-select:none}
details.card>summary::-webkit-details-marker{display:none}
details.card>summary::before{content:"▸";display:inline-block;color:var(--dim);font-size:10px;transition:transform .15s}
details.card[open]>summary::before{transform:rotate(90deg)}
details.card>summary h2{margin:0;flex:1}details.card[open]>summary{margin-bottom:12px}
button,select{font:inherit;background:#21262d;color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:2px 9px;cursor:pointer}
button:hover:not(:disabled){border-color:var(--on)}button:disabled{opacity:.35;cursor:default}
button.big{padding:7px 14px;width:100%}
button.rb{border-color:var(--soul);color:var(--soul)}
button.on{border-color:var(--on);color:var(--on);background:#1c2531}
button.chip{padding:1px 6px;font-size:11px}
#picks{flex-basis:100%;display:flex;flex-wrap:wrap;gap:4px;max-height:76px;overflow-y:auto}
.pulse{animation:pulse 2s ease-in-out infinite}
@keyframes pulse{50%{opacity:.45}}
.banner{border:1px dashed var(--xp);border-radius:8px;padding:9px;margin-bottom:10px;
font-size:12px;color:var(--xp)}
.st{display:flex;align-items:center;gap:10px;padding:9px 0;border-top:1px solid var(--line)}
.st:first-of-type{border-top:0}.st .e{font-size:26px;width:32px;text-align:center}
.st .n{flex:1;min-width:0}.st .n b{font-size:13px}.st small{color:var(--dim);font-size:11px}
.st .n b.nm{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
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
.thud{animation:thud .35s ease-out}
@keyframes thud{0%{transform:scaleY(2.2);filter:brightness(3)}60%{transform:scaleY(1);filter:brightness(1.6)}}
.miss{animation:miss .35s ease-out}
@keyframes miss{0%,60%{opacity:.25}30%{opacity:1}}
#log{height:132px;overflow-y:auto;font-size:12px;border-top:1px solid var(--line);
margin-top:10px;padding-top:8px}
#log div{margin:1px 0}.crit{color:var(--gold);font-weight:700}.win{color:var(--xp);font-weight:700}
.lose{color:var(--hp);font-weight:700}
</style></head><body><div class="wrap">
<div id="updBanner"></div>

<div class="card hero">
  <div class="face" id="face"></div>
  <div class="meta">
    <b id="title"></b> <span class="gold" id="lv"></span>
    <div style="margin:3px 0"><span class="badge soul" id="bRebirth"></span>
      <span class="badge" id="bFloor"></span><span class="badge soul" id="bSouls"></span></div>
    <div class="bar"><i id="xpbar"></i></div>
    <div class="row"><span class="dim" id="exp"></span><span class="dim" id="tonext"></span></div>
    <div id="transBox"></div>
    <div class="sheet" id="sheet"></div>
    <div id="relicIn" style="margin-top:6px;font-size:11px"></div>
    <div id="rbBonus" style="margin-top:6px;font-size:12px"></div>
  </div>
</div>

<details class="card" data-k="alloc" open>
  <summary><h2>스탯 배분 <span class="gold" id="left"></span></h2></summary>
  <div class="alloc" id="alloc"></div>
  <div style="margin-top:12px;display:flex;gap:8px">
    <button id="reset">전부 되돌리기</button>
    <span class="dim" style="font-size:11px;align-self:center">환생하면 초기화된다</span>
  </div>
</details>

<details class="card" data-k="missions" open>
  <summary><h2>미션 <span class="gold" id="streak"></span></h2></summary>
  <div id="missions"></div>
</details>

<details class="card" data-k="mini" open>
  <summary><h2>혼 사냥 <span class="soul" id="miniLeft"></span></h2></summary>
  <div id="mini"></div>
</details>

<details class="card" data-k="exped" open>
  <summary><h2>원정 <span class="soul" id="expedRate"></span></h2></summary>
  <div id="expedBanner"></div>
  <div id="exped"></div>
</details>

<details class="card" data-k="traits" open>
  <summary><h2>영구 특성 <span class="soul" id="soulsHave"></span></h2></summary>
  <div class="alloc" id="traits"></div>
  <div id="rbBox" style="margin-top:14px"></div>
</details>

<details class="card" data-k="relics" open><summary><h2>유물 도감 <span class="gold" id="relicCount"></span></h2></summary>
  <div id="relicNews"></div><div id="relicOdds"></div><div id="relics"></div></details>

<details class="card" data-k="dungeon" open><summary><h2 id="floorTitle">던전</h2></summary>
  <div id="auto" style="margin-bottom:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap"></div>
  <div id="stages"></div><div id="wall"></div></details>
<details class="card" data-k="hosts" open><summary><h2>합산된 기기</h2></summary><div id="hosts" style="font-size:12px"></div>
  <div id="provs" style="font-size:12px;margin-top:10px"></div></details>
<details class="card" data-k="code" open><summary><h2>저장 코드 — 백업·옮기기</h2></summary>
  <div class="dim" style="font-size:11px;margin-bottom:6px">메뉴 막대 앱과 token-rpg open 은 저장 파일 하나를 같이 쓴다.
    예전처럼 파일로 연 브라우저 저장을 옮기거나 백업할 때 쓴다. 고친 코드는 거부된다.</div>
  <textarea id="saveBox" rows="3" spellcheck="false" style="width:100%;background:#0d1117;
    color:var(--fg);border:1px solid var(--line);border-radius:6px;font:11px ui-monospace,monospace"></textarea>
  <div style="margin-top:6px;display:flex;gap:8px;align-items:center">
    <button id="saveShow">현재 저장 표시</button><button id="saveLoad">가져오기</button>
    <span class="dim" id="saveMsg" style="font-size:11px"></span></div></details>
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
// 1,000 이상은 1.25K · 12.5M · 3.4B 처럼 줄인다 — 커진 숫자가 좁은 팝오버에서 밀리지 않게
const compact = new Intl.NumberFormat("en", {notation: "compact", maximumSignificantDigits: 3});
const n = x => Math.abs(x) < 1000 ? String(Math.round(x)) : compact.format(x);
// 스탯은 1,000 아래에서 소수 한 자리가 뜻이 있다(DEF 281.8) — 그 위는 n() 으로 줄인다
const ns = (x, d) => Math.abs(x) < 1000 ? x.toFixed(d) : n(x);
// 원정은 초당 소수점 단위로 쌓인다 — 정수로 표시하면 멈춘 것처럼 보인다
const nf = x => x < 1000 ? x.toFixed(2) : n(x);
const MULTIPROV = (D.providers || []).length > 1;
const STATS  = [["atk","공격력","ATK"],["hp","체력","HP"],["dfn","방어력","DEF"],["crit","치명타","CRIT"],
                ["cdmg","치명타 피해","CDMG"],["spd","속도","SPD"]];
const TRAITS = [["atk","힘의 유산","ATK x"+K.tmul+"/lv"],["hp","혼의 유산","HP x"+K.tmul+"/lv"],
                ["dfn","벽의 유산","DEF x"+K.tmul+"/lv"],
                ["cdmg","파괴의 유산","CDMG x"+K.tmul+"/lv"],
                ["spd","신속의 유산","SPD x"+K.tmul+"/lv"],["soul","수확","얻는 혼 +"+K.tsoul+"%/lv"],
                ["pt","각성","스탯 배분 +"+K.tpt+"pt/lv"]];

const fresh = () => ({alloc:{atk:0,hp:0,dfn:0,crit:0,cdmg:0,spd:0}, cleared:[], souls:0, rebirths:0,
            traits:{atk:0,hp:0,dfn:0,crit:0,cdmg:0,spd:0,soul:0,pt:0},
            best:0, exped:{since:Date.now(), seenExp:0}, auto:false, claimed:{}, relics:{}, rbExp:null, farm:0,
            trans:0, mini:{day:"", n:0}});

// ── 초월: Lv.99 에서 레벨을 1로 되돌리고 그 위로 다시 올린다.
// EXP 는 쓴 토큰이라 줄지 않는다 -> 초월 횟수만큼 덜어내고 다시 센다.
// 이미 받은 배분 포인트는 적립해 둔다 — 99 에서 토큰이 아무것도 안 주던 벽을 없애는 게 목적이라
// 초월이 손해가 되면 아무도 안 누른다.
// 레벨 곡선·보스 곡선은 파이썬 exp_for()/level_of()/boss() 와 같은 식이어야 한다 —
// 한쪽만 고치면 화면과 메뉴 막대 배지가 어긋난다 (selftest 의 _check_trans 가 붙들고 있다)
const expFor  = lv => Math.floor(K.lvExp * Math.pow(lv-1, 3) / (lv - 1 + K.lvEase));
const levelOf = e => { let lv = 1, x = Math.max(e, 0);
                       while (lv < K.maxLv && expFor(lv + 1) <= x) lv++; return lv; };
const TRANS_EXP = expFor(K.maxLv);
const tierOf  = lv => K.tiers.filter(t => lv >= t[0]).pop();
const transOf = s => Math.max(0, Math.min(s.trans|0, Math.floor(H.exp / TRANS_EXP)));
const expNow  = () => Math.max(0, H.exp - transOf(save) * TRANS_EXP);
const lvNow   = () => levelOf(expNow());
const canTrans = () => lvNow() >= K.maxLv;
// 레벨이 준 배분 포인트 — 초월로 되돌린 몫은 적립분으로 남는다
const pointsOf = s => (transOf(s) * K.maxLv + levelOf(Math.max(0, H.exp - transOf(s) * TRANS_EXP)))
                      * K.ptPerLevel + (s.traits.pt|0) * K.tpt;
let save = fresh();
// 저장: token-rpg 서버(http)로 열면 서버의 파일 하나를 브라우저·메뉴 막대 앱·다른 기기가 같이 쓴다.
// file:// 로 열면(서버 없음) 예전처럼 이 브라우저의 localStorage 에 둔다.
const SERVED = location.protocol.startsWith("http");
let rev = 0, sync = Promise.resolve();          // 쓰기를 한 줄로 세워 rev 가 꼬이지 않게 한다
const note = t => { $("expedBanner").innerHTML = `<div class="banner">${t}</div>`; };
const adopt = d => { rev = d.rev; save = Object.assign(fresh(), d.save || {}); fill(); };
// 옛 저장에는 새로 생긴 스탯 칸(cdmg 등)이 없다 — 비어 있으면 계산이 전부 NaN 이 된다
function fill(){ const f = fresh(); save.alloc = {...f.alloc, ...save.alloc}; save.traits = {...f.traits, ...save.traits};
                 delete save.bless; delete save.blessOffer;     // 없어진 축복 칸은 저장에서 치운다
  // 예전 유물(프로바이더|프로젝트 키)은 빈 고정 보스 칸에 차례로 옮긴다 — 모은 유물을 잃지 않게
  const rs = save.relics || {};
  for (const k of Object.keys(rs).filter(k => k.includes("|"))) {
    const free = SLOTS.find(s => !rs[relicKey(s)]);
    if (free) rs[relicKey(free)] = rs[k];
    delete rs[k];
  }
  // 유물 능력치가 보스 고정으로 바뀌었다 — 모은 등급은 그대로 두고 능력치만 맞춰 준다
  for (const s of SLOTS) {
    const r = rs[relicKey(s)];
    if (r && r.a !== s.affix) r.a = s.affix;
  }
  // 없어진 예지에 쓴 혼은 전액 돌려준다 — 되팔 수 없는 특성이었으니 떠안기지 않는다
  const lv = save.traits.crit || 0;
  if (lv > 0) {
    let back = 0;
    for (let i = 0; i < lv; i++) back += costOf(i);
    save.souls += back;
    save.traits.crit = 0;
    refundMsg = `영구 특성 '예지'를 없앴다 (CRIT 상한 100%에 막히는데 되팔 수 없었다).
                 쓴 혼 ${n(back)}을 전액 돌려줬다 — 다른 특성에 다시 넣어라.`;
  }
}
const pull = async () => {
  const r = await fetch("save", {cache: "no-store"});
  if (!r.ok) throw new Error("save " + r.status);
  adopt(await r.json());
};
const put = () => {
  if (!SERVED) { try { localStorage.setItem("trpg", JSON.stringify(save)); } catch(e) {} return; }
  sync = sync.then(async () => {
    const r = await fetch("save", {method: "PUT", headers: {"Content-Type": "application/json"},
                                   body: JSON.stringify({base: rev, save})});
    if (r.ok) { rev = (await r.json()).rev; return; }
    if (r.status === 409) {            // 다른 창·기기가 먼저 진행했다 -> 그쪽 저장을 따른다
      adopt(await r.json()); drawAll();
      note("다른 창에서 진행된 저장을 불러왔다. 방금 조작은 반영되지 않았다.");
      return;
    }
    throw new Error("save " + r.status);
  }).catch(() => note("저장하지 못했다 — 게임 서버가 꺼져 있다. 메뉴 막대 앱이나 token-rpg open 으로 다시 열어라."));
};

// 보스: 전역 스테이지 번호의 지수 곡선. 층이 바뀌어도 난이도가 끊기지 않는다.
const boss = g => { let p = Math.pow(K.step, g-1);
  if (g < K.earlyG) p *= Math.pow(K.earlyMul, (K.earlyG - g) / (K.earlyG - 1));
  return {
  hp: Math.round(K.bhp*p), atk: Math.round(K.batk*p),
  dfn: Math.round(K.bdef*p), spd: Math.round(K.bdef*p*0.9) }; };
const soulOf = g => Math.round(Math.pow(g, K.soulExp) * SLOTS[(g-1)%N].soul
                                * (1 + relicSum("soul")/100) * (1 + K.rbSoul * save.rebirths)
                                * (1 + K.tsoul/100 * save.traits.soul));
const costOf = lv => Math.round(K.ck * Math.pow(K.cmul, lv));
// 환생 기운 = 마지막 환생 이후 새로 쓴 토큰 / K.rbExp. 상한(K.rbCap회분)을 넘은 몫은 버린다
const rbBase = () => Math.max(save.rbExp ?? H.exp, H.exp - K.rbCap * K.rbExp);
const rbCharges = () => Math.floor((H.exp - rbBase()) / K.rbExp);
// 옛 저장(기운 없음)이나 지금보다 큰 값(프로바이더를 끈 경우)은 지금부터 센다 — 소급하지 않는다
const fixRb = () => { if (typeof save.rbExp !== "number" || save.rbExp > H.exp) save.rbExp = H.exp; };

// 특성 lv..lv+cnt-1 을 한 번에 사는 총비용
const bulkCost = (k, cnt) => {
  let c = 0;
  for (let i = 0; i < cnt; i++) c += costOf(save.traits[k] + i);
  return c;
};
// 지금 혼으로 살 수 있는 특성 레벨 수 (cap 까지)
const buyable = (k, cap) => {
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
const points     = () => pointsOf(save);
const used       = () => STATS.reduce((s,[k]) => s + save.alloc[k], 0);
const left       = () => points() - used();
// 최종 스탯 = (토큰이 준 기본값 + 배분) x 환생 특성 배율
// 유물(영구)이 마지막에 곱해진다
// 치명타율은 K.critCap 까지, 넘친 %p 는 치명타 피해로 간다
// 파괴의 유산은 CDMG 에 배율로 곱한다 — 가산이던 시절엔 혼을 부어도 깊이가 안 늘었다.
// 영구 특성에서는 CRIT 을 뺐다 — 상한 100%가 있어 되팔 수 없는 함정이었다.
// CRIT 은 토큰(thinking)·배분 포인트·유물로만 오른다. 넘친 %p 가 CDMG 로 가는 건 그대로다.
const spdNow = () => Math.round(F("spd"));
const critRaw = () => H.crit + save.alloc.crit*G.crit + relicSum("crit");
const F = k => k === "crit" ? Math.min(K.critCap, critRaw())
  : k === "cdmg" ? (K.cdmgBase + save.alloc.cdmg*G.cdmg + Math.max(0, critRaw() - K.critCap))
                   * Math.pow(K.tmul, save.traits.cdmg)
  : (H[k] + save.alloc[k]*G[k]) * Math.pow(K.tmul, save.traits[k])
    * (1 + relicSum(k)/100);

// 카드 접기 — 접어 둔 카드는 이 브라우저(창)에 기억한다. 저장 파일과는 무관
const FOLD = "trpg.fold";
let folded = [];
try { folded = JSON.parse(localStorage.getItem(FOLD)) || []; } catch(e) {}
document.querySelectorAll("details[data-k]").forEach(d => {
  d.open = !folded.includes(d.dataset.k);
  d.ontoggle = () => {
    folded = [...document.querySelectorAll("details[data-k]:not([open])")].map(x => x.dataset.k);
    try { localStorage.setItem(FOLD, JSON.stringify(folded)); } catch(e) {}
  };
});

$("provs").innerHTML = (D.providers || []).length < 2 ? "" :
  '<div class="dim" style="margin-bottom:4px">프로바이더별</div>' +
  D.providers.map(([p,v]) =>
    `<div class="row"><span>${p}</span><span class="gold">${n(v)}</span></div>`).join("");
$("hosts").innerHTML = D.hosts.map(([h,u,v]) =>
  `<div class="row"><span>${h} <span class="dim">${u.slice(0,10)}</span></span><span class="gold">${n(v)}</span></div>`).join("");

// 치명타율이 상한이면 배분·특성 줄에 알린다 — 더 올려도 치명타 피해로 간다
const capNote = k => k === "crit" && critRaw() >= K.critCap
  ? `<small style="color:var(--gold);white-space:normal">${K.critCap}% — 넘친 %p는 치명타 피해로</small>` : "";
function drawHero(){
  const lv = lvNow(), e = expNow(), cur = expFor(lv), nxt = expFor(lv + 1), t = tierOf(lv);
  $("face").textContent = t[1]; $("title").textContent = t[2];
  $("lv").innerHTML = "Lv." + lv + (transOf(save) ? ` <span class="soul">초월 ${transOf(save)}</span>` : "");
  $("xpbar").style.width = (lv >= K.maxLv ? 100 : 100 * (e - cur) / Math.max(nxt - cur, 1)) + "%";
  $("exp").textContent = n(e) + " EXP" + (transOf(save) ? ` (누적 ${n(H.exp)})` : "");
  $("tonext").textContent = lv >= K.maxLv ? "초월할 수 있다" : "다음까지 " + n(nxt - e);
  drawTrans();
  $("bRebirth").textContent = "환생 " + save.rebirths + "회";
  $("bFloor").textContent   = floorNow() + "층";
  $("bSouls").textContent   = "혼 " + n(save.souls);
  $("soulsHave").textContent = "보유 " + n(save.souls);
  $("sheet").innerHTML = STATS.map(([k,,s]) =>
    `<span><span class="dim">${s}</span> <b>${ns(F(k), k==="dfn"||k==="crit"?1:0)}${k==="crit"||k==="cdmg"?"%":""}</b></span>`
  ).join("");
  // 유물 몫은 한 줄을 따로 쓴다 — 수치 옆에 붙이면 칸이 넘치고 '더 더해진다'로 읽힌다
  const rin = STATS.map(([k,,s]) => [s, relicSum(k), k === "crit" ? "%p" : "%"])
    .filter(([, v]) => v).map(([s, v, u]) => `${s} +${+v.toFixed(1)}${u}`).join(" · ");
  $("relicIn").innerHTML = rin
    ? `<span class="dim">위 수치에 유물 포함 —</span> <span class="gold">${rin}</span>` : "";
  // 집계가 바뀌어 EXP 가 줄면(프로바이더 보정 등) 이미 쓴 포인트가 준 것보다 많아질 수 있다.
  // 넣는 버튼은 이미 막히니 남은 건 읽히게만 해 준다 — "남은 -6pt" 는 무슨 뜻인지 알 수 없다.
  $("left").textContent = left() < 0 ? `${-left()}pt 초과 — 빼야 한다` : "남은 " + left() + "pt";
  // 줄마다 최소(그 줄 전부 회수) · −1 · +1 · +10 · 최대(남은 전부)
  // — 넣는 쪽만 한 번에 되고 빼는 쪽은 한 점씩이라 되돌리기가 번거로웠다
  const lf = left();
  $("alloc").innerHTML = STATS.map(([k,ko,s]) =>
    `<span>${ko}<small>${s} +${G[k]}${k==="cdmg"?"%p":""}/pt</small>${capNote(k)}</span>
     <b class="gold">${save.alloc[k]}</b>
     <button data-k="${k}" data-d="min" ${save.alloc[k]<=0?"disabled":""}>최소</button>
     <button data-k="${k}" data-d="-1" ${save.alloc[k]<=0?"disabled":""}>−</button>
     <button data-k="${k}" data-d="1" ${lf<=0?"disabled":""}>+1</button>
     <button data-k="${k}" data-d="10" ${lf<=0?"disabled":""}>+10</button>
     <button data-k="${k}" data-d="max" ${lf<=0?"disabled":""}>최대</button>`
  ).join("");
  $("alloc").querySelectorAll("button").forEach(b => b.onclick = () => {
    const k = b.dataset.k, d = b.dataset.d, room = left();
    save.alloc[k] += d === "min" ? -save.alloc[k]
                   : d === "-1"  ? -Math.min(1, save.alloc[k])
                   : d === "max" ? room : Math.min(+d, room);
    put(); drawAll();
  });
  // 줄마다 구입(1레벨) · 최대(혼이 되는 만큼)
  $("traits").innerHTML = TRAITS.map(([k,ko,eff]) => {
    const all = buyable(k, 1e9);
    return `<span>${ko}<small>${eff}</small>${capNote(k)}</span>
      <b class="soul">Lv.${save.traits[k]}</b>
      <span class="dim" style="font-size:11px;text-align:right;white-space:nowrap">${n(costOf(save.traits[k]))}혼</span>
      <button data-t="${k}" data-c="1" ${all<=0?"disabled":""}>구입</button>
      <button data-t="${k}" data-c="max" ${all<=0?"disabled":""}
        title="${all?n(bulkCost(k, all))+"혼":""}">최대${all>1?" x"+all:""}</button>`;
  }).join("");
  $("traits").querySelectorAll("button").forEach(b => b.onclick = () => {
    const k = b.dataset.t, cnt = buyable(k, b.dataset.c === "max" ? 1e9 : 1);
    if (cnt <= 0) return;
    save.souls -= bulkCost(k, cnt); save.traits[k] += cnt; put(); drawAll();
  });
  drawRebirth();
}

let rbArmed = false;   // 환생은 되돌릴 수 없다 -> 두 번 눌러야 실행 (모달 대신)
function drawRebirth(){
  const gain = save.cleared.reduce((s,g) => s + soulOf(g), 0);
  const ch = rbCharges(), can = save.cleared.length > 0 && ch > 0;
  const toNext = K.rbExp - (H.exp - rbBase()) % K.rbExp;
  $("rbBox").innerHTML =
    `<button class="big rb" id="rbBtn" ${can?"":"disabled"}>${
       rbArmed ? `정말 환생한다 — ${floorNow()}층까지의 진행을 버린다 (다시 누르면 실행)`
               : `환생 — 혼 ${n(gain)} 획득 · 기운 ${ch}/${K.rbCap}`}</button>
     <div class="bar" style="margin-top:8px"><i style="width:${ch >= K.rbCap ? 100 : 100 - toNext / K.rbExp * 100}%;background:var(--soul)"></i></div>
     <div class="row"><span class="soul">환생 기운 ${ch}/${K.rbCap}</span>
       <span class="dim">${ch >= K.rbCap ? "가득 참" : "다음 기운까지 토큰 " + n(toNext)}</span></div>
     <div class="dim" style="font-size:11px;margin-top:6px">
       환생 1회에 기운 하나 — AI 툴로 새로 쓴 토큰 ${n(K.rbExp)}마다 하나씩, ${K.rbCap}개까지 쌓인다.
       클리어 기록${save.auto ? "" : "과 스탯 배분"}을 버리고 1층부터 다시 시작한다.
       영구 특성·혼·유물·원정(역대 최고 ${save.best || 0}스테이지 기준)은 그대로 남는다.
       환생할 때마다 얻는 혼이 영구히 +${K.rbSoul * 100}%씩 는다 (지금 +${Math.round(K.rbSoul * save.rebirths * 100)}%).
       ${!save.cleared.length ? "먼저 스테이지를 하나 이상 클리어해라." : ch ? "" : "기운이 없다 — 토큰을 더 쓰면 찬다."}</div>`;
  if (can) $("rbBtn").onclick = () => {
    if (!rbArmed) { rbArmed = true; drawRebirth(); return; }
    rbArmed = false;
    const prev = {...save.alloc};
    save.rbExp = rbBase() + K.rbExp;                        // 기운 하나 소모
    save.souls += gain; save.rebirths++;
    save.cleared = []; STATS.forEach(([k]) => save.alloc[k] = 0);
    // 자동 도전 중이면 직전 배분을 다시 건다. 포인트는 레벨·각성에서 오므로 환생해도 줄지 않는다.
    if (save.auto) STATS.forEach(([k]) => save.alloc[k] = prev[k]);
    put(); drawAll(); window.scrollTo({top:0, behavior:"smooth"});
  };
}

function drawTrans(){
  const t = transOf(save);
  if (!canTrans() && !t) { $("transBox").innerHTML = ""; return; }   // 아직 볼 일이 없다
  $("transBox").innerHTML = canTrans()
    ? `<button class="big" id="transBtn" style="margin-top:10px">초월 — 배분 ${
         K.maxLv * K.ptPerLevel}pt 적립하고 Lv.1 부터 다시</button>
       <div class="dim" style="font-size:11px;margin-top:6px">
         Lv.${K.maxLv} 위로는 토큰을 더 써도 배분 포인트가 늘지 않는다. 초월하면 레벨과 칭호만
         처음으로 돌아가고, 지금까지 레벨이 준 포인트는 그대로 남은 채 다시 올릴 수 있다.
         스탯 배분·혼·유물·클리어 기록은 건드리지 않는다.</div>`
    : `<div class="dim" style="font-size:11px;margin-top:6px">초월 ${t}회 —
         적립된 배분 ${n(t * K.maxLv * K.ptPerLevel)}pt</div>`;
  const b = $("transBtn");
  if (b) b.onclick = () => {
    save.trans = transOf(save) + 1;
    put(); drawAll(); window.scrollTo({top:0, behavior:"smooth"});
    note(`초월 ${save.trans}회 — 배분 ${n(save.trans * K.maxLv * K.ptPerLevel)}pt 가 적립됐다.`);
  };
}

$("reset").onclick = () => { STATS.forEach(([k]) => save.alloc[k]=0); put(); drawAll(); };

// 저장 옮기기: 브라우저 <-> 메뉴 막대 앱. 덮어쓰기는 되돌릴 수 없어 두 번 눌러야 실행
// 코드 = base64(JSON).체크섬 — 숫자를 손으로 고친 코드를 거른다.
// ponytail: 솔트가 페이지 안에 있어 작정하면 위조할 수 있고, 로컬 게임이라 개발자도구로도 고칠 수 있다.
// 막는 대상은 '붙여넣기 전에 숫자 살짝 고치기'. 진짜 막으려면 저장을 서버가 들고 있어야 한다.
const SALT = "token-rpg/save/v1";
const sum = s => { let h = 0x811c9dc5;
  for (const c of SALT + s) { h ^= c.codePointAt(0); h = Math.imul(h, 16777619) >>> 0; }
  return h.toString(36); };
const encodeSave = s => { const j = JSON.stringify(s); return btoa(j) + "." + sum(j); };
const decodeSave = code => {
  try { const [b, h] = code.trim().split("."), j = atob(b);
        return sum(j) === h ? JSON.parse(j) : null; } catch(e) { return null; }
};
// 체크섬이 맞아도 규칙상 불가능한 값은 거른다: 음수·소수, 레벨이 준 것보다 많은 배분
const validSave = s => !!s && Array.isArray(s.cleared) && !!s.traits && !!s.alloc && !!s.exped
  && [s.souls, s.rebirths, s.best, s.trans ?? 0, (s.mini || {}).n || 0, ...s.cleared,
      ...Object.values(s.traits), ...Object.values(s.alloc)]
       .every(v => Number.isInteger(v) && v >= 0)
  && (s.trans ?? 0) * TRANS_EXP <= H.exp                 // 쓴 토큰보다 많이 초월할 수는 없다
  && STATS.reduce((t, [k]) => t + (s.alloc[k] || 0), 0) <= pointsOf(s);

let loadArmed = false;
$("saveShow").onclick = () => {
  $("saveBox").value = encodeSave(save); $("saveBox").select();
  $("saveMsg").textContent = "⌘C 로 복사";
};
$("saveLoad").onclick = () => {
  const s = decodeSave($("saveBox").value);
  if (!validSave(s)) {
    loadArmed = false; $("saveMsg").textContent = "저장 코드가 아니거나 수정됐다"; return;
  }
  if (!loadArmed) { loadArmed = true; $("saveMsg").textContent = "지금 저장을 덮어쓴다 — 다시 누르면 실행"; return; }
  loadArmed = false;
  Object.assign(save, s); fill(); fixRb(); put(); drawAll();
  $("saveMsg").textContent = `가져왔다 — 환생 ${save.rebirths}회, 혼 ${n(save.souls)}`;
};

// 평균 피해로 따져 이길 수 있는가 — 벽 표시와 자동 도전이 같은 기준을 쓴다
const beatable = b => {
  const eff = F("atk") * (1 + F("crit")/100 * (F("cdmg")/100 - 1));
  return b.hp / Math.max(1, eff - b.dfn) < F("hp") / Math.max(1, b.atk - F("dfn"));
};

// ── 자동 도전: 첫 환생 후 해금. 1초에 한 판씩 연출 없이 같은 규칙으로 싸운다.
// 목표(save.farm: 0 = 끝까지, g = g스테이지)까지 한 칸씩 오르고, 더 못 오르면 멈추지 않고
// 목표(또는 이번 판 가장 깊은 곳)를 반복한다 — 유물 파밍. 환생해도 켜 둔 채면 다시 오른다.
let autoMsg = "", autoFailSig = "", autoKey = "", farmW = 0, farmL = 0, farmI = 0;
// 능력치가 그대로면 진 스테이지로 다시 오르지 않는다 (치명타 운으로 벽을 넘는 반복 방지)
const statSig = () => STATS.map(([k]) => F(k)).join();
const autoReset = () => { autoFailSig = ""; autoMsg = ""; farmW = farmL = farmI = 0; };
// 반복할 스테이지들. 옛 저장은 숫자 하나였다 — 읽을 때 배열로 맞춘다.
// 비어 있으면 예전처럼 끝까지 오르고 이길 수 있는 가장 깊은 곳을 돈다.
const picks = () => (Array.isArray(save.farm) ? save.farm : save.farm > 0 ? [save.farm] : [])
  .filter(g => Number.isInteger(g) && g >= 1 && g <= save.best).sort((a, b) => a - b);
const setPicks = ps => { save.farm = [...new Set(ps)].sort((a, b) => a - b); autoReset(); put(); drawAuto(); };
const autoSay = m => { if (m !== autoMsg) { autoMsg = m; drawAuto(); } };
function drawAuto(){
  if (!save.rebirths) {
    autoKey = "";
    $("auto").innerHTML = '<span class="dim" style="font-size:11px">자동 도전 — 첫 환생 후 해금</span>';
    return;
  }
  // 버튼·칸은 바뀔 때만 다시 그린다 — 매초 갈아엎으면 누르는 중에 사라진다
  const ps = picks(), on = new Set(ps), best = save.best || 0;
  const key = `${save.auto}|${ps.join()}|${best}`;
  if (key !== autoKey || !$("autoMsg")) {
    autoKey = key;
    // 스테이지마다 유물 속성이 다르다 — 여러 곳을 골라 돌려야 속성이 골고루 모인다
    const chips = [];
    for (let g = 1; g <= best; g++)
      chips.push(`<button class="chip${on.has(g) ? " on" : ""}" data-g="${g}"
        title="${g}. ${SLOTS[(g-1)%N].name}">${SLOTS[(g-1)%N].emoji}${g}</button>`);
    $("auto").innerHTML = `<button id="autoBtn" class="${save.auto?"on":""}">자동 ${save.auto?"켜짐":"꺼짐"}</button>
      <button id="pickAll">${ps.length === best && best ? "전체 해제" : "전체 반복"}</button>
      <span class="dim" style="font-size:11px">${ps.length
        ? `고른 ${ps.length}곳을 차례로 반복` : "끝까지 오르고 가장 깊은 곳 반복"}</span>
      <div id="picks">${chips.join("")}</div>
      <span class="dim" id="autoMsg" style="font-size:11px;flex-basis:100%"></span>`;
    $("autoBtn").onclick = () => { save.auto = !save.auto; autoReset(); put(); drawAuto(); };
    $("pickAll").onclick = () => setPicks(ps.length === best
      ? [] : Array.from({length: best}, (_, i) => i + 1));
    $("picks").onclick = e => {                       // 칸 하나하나가 아니라 묶어서 듣는다
      const g = +(e.target.dataset || {}).g;
      if (g) setPicks(on.has(g) ? ps.filter(x => x !== g) : [...ps, g]);
    };
  }
  $("autoMsg").textContent = save.auto ? autoMsg : "";
}
function autoStep(){
  if (!save.auto || !save.rebirths) return;
  const ps = picks();
  // 고른 곳이 있으면 가장 깊은 것까지만 오른다 — 하나만 고른 건 예전 동작 그대로다
  const tgt = ps.length ? ps[ps.length - 1] : Infinity;
  const top = maxCleared(), next = top + 1;
  // 1) 목표까지 한 칸씩 오른다
  if (next <= tgt && beatable(boss(next)) && autoFailSig !== statSig() + "@" + next) {
    if (quickFight(boss(next))) {
      winStage(next); autoMsg = `${next}. ${SLOTS[(next-1)%N].name} 격파`; drawAll();
    } else {
      autoFailSig = statSig() + "@" + next;
      autoSay(`${next}스테이지 패배 — 능력치가 바뀌면 다시 오른다`);
    }
    return;
  }
  // 2) 더 못 오르면 반복. 이긴 판은 저장할 게 없으니 유물이 나왔을 때만 쓴다
  let g;
  if (ps.length) {
    // 고른 곳을 한 판씩 돌아가며 — 아직 못 깼거나 못 이기는 곳은 건너뛴다
    const ok = ps.filter(x => x <= top && beatable(boss(x)));
    if (!ok.length) return autoSay(`고른 ${ps.length}곳을 아직 못 이긴다 — 능력치를 올려라`);
    g = ok[farmI++ % ok.length];
  } else {
    g = top;
    // 끝까지 모드: 이길 수 있는 가장 깊은 곳을 반복
    while (g > 1 && !beatable(boss(g))) g--;
  }
  if (g < 1) return autoSay(`${next}스테이지 앞에서 대기 — 능력치를 올려라`);
  if (quickFight(boss(g))) {
    farmW++;
    const drop = rollRelic(g, SLOTS[(g-1)%N], false);
    if (drop) { relicNews = drop; put(); drawAll(); }
  } else farmL++;
  autoSay(`${g}스테이지 반복 중${ps.length > 1 ? ` (고른 ${ps.length}곳 순회)` : ""}`
    + ` · 승 ${farmW} 패 ${farmL}${next <= tgt ? ` · ${next}스테이지는 아직 무리` : ""}`);
}

function drawStages(){
  const fl = floorNow(), base = (fl - 1) * N;
  $("floorTitle").textContent = `던전 — ${fl}층 (전역 ${base+1}~${base+N} 스테이지)`;
  drawAuto();
  $("stages").innerHTML = "";
  let blocked = null;
  SLOTS.forEach((slot, i) => {
    const g = base + i + 1, b = boss(g);
    const done = save.cleared.includes(g);
    const open = i === 0 || save.cleared.includes(g - 1);
    const el = document.createElement("div");
    el.className = "st" + (open ? "" : " lock") + (done ? " done" : "");
    // 이름은 한 줄 말줄임, 수치는 "HP 115" 단위로 묶어 숫자만 다음 줄로 밀리지 않게
    const seg = (k, v) => `<span style="white-space:nowrap">${k} ${n(v)}</span>`;
    el.innerHTML = `<div class="e">${slot.emoji}</div>
      <div class="n"><b class="nm" title="${slot.name}">${done?"✓ ":""}${g}. ${slot.name}</b>
      <small>${
        [seg("HP", b.hp), seg("ATK", b.atk), seg("DEF", b.dfn), seg("SPD", b.spd)].join(" · ")}${
        spdNow()>=b.spd ? "" : " · <span style='color:var(--hp);white-space:nowrap'>보스 선공</span>"}
      <br>격파 시 혼 ${n(soulOf(g))} · 유물 ${AFFIX[slot.affix][0]}</small></div>`;
    const btn = document.createElement("button");
    btn.textContent = open ? (done ? "재도전" : "도전") : "잠김";
    btn.disabled = !open;
    btn.onclick = () => fightStart(g, slot, b);
    el.appendChild(btn); $("stages").appendChild(el);
    // 평균 피해 기준으로 이길 수 없는 첫 스테이지 = 환생이 필요한 벽
    if (open && !done && blocked === null && !beatable(b)) blocked = g;
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
    `<div class="row" style="font-size:13px">
       <span>${g}스테이지 반복 중</span>
       <span class="soul ${full?"":"pulse"}" style="white-space:nowrap">${nf(have)} 혼</span></div>
     <div class="dim" style="font-size:11px;margin-bottom:8px">${g > maxCleared()
         ? "역대 최고(환생 전) 기록 기준 · " : ""}토큰 배율 x${tokenMul().toFixed(2)}</div>
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

// ── 미션: 게임 안 행동이 아니라 실제 사용량(D.daily = 날짜 -> 프로바이더 -> 토큰)으로 채워진다.
// 저장을 고쳐도 진행도는 못 바꾼다 — 로그에서 build 가 계산한 값이기 때문.
const ymd = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
const daysBack = k => { const d = new Date(); d.setDate(d.getDate() - k); return ymd(d); };
const dayTok = day => Object.values((D.daily || {})[day] || {}).reduce((a, b) => a + b, 0);
// 오늘 아직 안 썼으면 어제까지 이어진 연속일을 센다 (오늘 쓰면 이어진다)
const streak = () => { let s = 0; for (let k = dayTok(daysBack(0)) ? 0 : 1; dayTok(daysBack(k)); k++) s++; return s; };
const streakMul = () => 1 + Math.min(streak(), 7) * 0.1;
const nice = x => { const p = Math.pow(10, Math.floor(Math.log10(x)) - 1); return Math.round(x / p) * p; };
function missions(){
  // 목표 = 지난 14일 중 쓴 날 평균의 절반 -> 사용량이 적은 사람도 많은 사람도 매일 닿을 수 있다
  const past = Array.from({length: 14}, (_, i) => dayTok(daysBack(i + 1))).filter(Boolean);
  const T = nice(Math.max(50000, (past.length ? past.reduce((a, b) => a + b) / past.length : 200000) * 0.5));
  const today = daysBack(0), dow = (new Date().getDay() + 6) % 7;          // 월요일 = 0
  const week = Array.from({length: dow + 1}, (_, i) => daysBack(dow - i)), wk = daysBack(dow);
  const weekTok = week.reduce((s, d) => s + dayTok(d), 0);
  const reward = m => Math.round(Math.max(50, soulOf(Math.max(1, save.best))) * m * streakMul());
  const tools = Object.keys((D.daily || {})[today] || {}).length;
  // 예산을 가중치로 나눠 갖는다 — 미션을 늘려도 하루 수입 총합은 K.dayBudget 그대로다
  const share = (budget, list) => {
    const sum = list.reduce((s, m) => s + m.w, 0);
    return list.map(m => ({key: m.key, name: m.name, cur: m.cur, goal: m.goal,
                           souls: reward(budget * m.w / sum)}));
  };
  // 쉬운 단은 적게, 먼 단은 많이 — 예전엔 2단만 깨면 하루치를 다 받았다
  const day = [
    {key: "d1:" + today, w: 1,   name: `오늘 토큰 ${n(T)} 쓰기`,     cur: dayTok(today), goal: T},
    {key: "d2:" + today, w: 1.5, name: `오늘 토큰 ${n(T * 2)} 쓰기`, cur: dayTok(today), goal: T * 2},
    {key: "d3:" + today, w: 2.5, name: `오늘 토큰 ${n(T * 3)} 쓰기`, cur: dayTok(today), goal: T * 3},
  ];
  if (MULTIPROV)      // 툴을 여러 개 쓰는 사람만 — 예산은 위 세 단과 나눠 쓴다
    day.push({key: "d4:" + today, w: 1, name: "오늘 AI 툴 2개 이상 쓰기", cur: tools, goal: 2});
  return share(K.dayBudget, day).concat(share(K.weekBudget, [
    {key: "w1:" + wk, w: 1, name: "이번 주 5일 사용", cur: week.filter(dayTok).length, goal: 5},
    {key: "w2:" + wk, w: 1, name: `이번 주 토큰 ${n(T * 5)} 쓰기`, cur: weekTok, goal: T * 5},
  ]));
}
function drawMissions(){
  const s = streak();
  $("streak").textContent = s ? `연속 ${s}일 · 보상 x${streakMul().toFixed(1)}` : "";
  const got = k => !!(save.claimed || {})[k];
  const row = m => {
    const done = m.cur >= m.goal;
    return `<div class="st"><div class="n"><b>${got(m.key) ? "✓ " : ""}${m.name}</b>
      <div class="bar"><i style="width:${Math.min(100, 100 * m.cur / m.goal)}%;background:var(--gold)"></i></div>
      <small>${n(Math.min(m.cur, m.goal))} / ${n(m.goal)} · 보상 혼 ${n(m.souls)}</small></div>
      <button data-k="${m.key}" ${done && !got(m.key) ? "" : "disabled"}>${
        got(m.key) ? "받음" : done ? "받기" : "진행 중"}</button></div>`;
  };
  // 주기가 다르면 따로 묶어 둬야 뭐가 언제 초기화되는지 보인다 (키 앞글자가 d=일일·w=주간)
  const ms = missions();
  const group = (title, when, list) => !list.length ? "" :
    `<div class="dim" style="font-size:11px;margin:10px 0 2px">${title}
       <span style="opacity:.7">· ${when} 새로 바뀐다</span></div>` + list.map(row).join("");
  $("missions").innerHTML =
    group("일일 미션", "매일 자정에", ms.filter(m => m.key[0] === "d")) +
    group("주간 미션", "월요일에", ms.filter(m => m.key[0] === "w")) +
    `<div class="dim" style="font-size:11px;margin-top:6px">실제 사용량으로 채워진다.
    Claude Code 응답마다, 메뉴 막대 앱은 5분마다 갱신. 연속 사용일마다 보상 +10% (최대 7일).</div>`;
  $("missions").querySelectorAll("button[data-k]").forEach(b => b.onclick = () => {
    const m = missions().find(x => x.key === b.dataset.k);
    if (!m || m.cur < m.goal || got(m.key)) return;
    // 2주 지난 수령 기록은 버린다 — 저장이 끝없이 커지지 않게
    save.claimed = Object.fromEntries(Object.entries(save.claimed || {})
      .filter(([k]) => k.split(":")[1] >= daysBack(14)));
    save.claimed[m.key] = 1; save.souls += m.souls; put(); drawAll();
  });
}

// ── 혼 사냥: 하루 K.miniTries 판, 한 판은 K.miniShots 발. 왕복하는 표식을 과녁에서 멈춘다.
// 게임 안의 행동으로 혼을 버는 유일한 칸이라 예산을 미션과 같은 단위로 묶어 뒀다 —
// 아무리 잘해도 하루 K.miniBudget x K.miniBest 격파분이 끝이다. 실력은 수입의 상한이 아니라
// 그 상한을 채우는 속도만 바꾼다. (demo() 의 밸런스 검사가 이 상한을 붙들고 있다)
const MINI_P0 = 2400;           // 첫 발 왕복(ms)
// 맞힌 발 하나당 이만큼 빨라진다. '발 수'가 아니라 '명중 수'에 걸어 둔다 —
// 빗나가면 속도가 그대로라, 못 맞히는 사람에게 벌로 더 어려워지지 않는다.
const MINI_RAMP = 0.85;
const MINI_HALF = 9;            // 과녁 반폭(%)
const MINI_EYE = 0.2;           // 이 안쪽이 한가운데(x K.miniBest). 화면의 금색 띠와 같은 값이어야 한다
const MINI_HOLD = 650;          // 맞은 자리를 보여주는 시간(ms) — 바로 다음 발이 뛰면 맞췄는지 알 수 없다
const miniDay = () => { const m = save.mini || {}; return m.day === daysBack(0) ? m : {day: daysBack(0), n: 0}; };
const miniLeft = () => Math.max(0, K.miniTries - (miniDay().n | 0));
const miniP = () => MINI_P0 * Math.pow(MINI_RAMP, miniHits);
const miniSpd = () => MINI_P0 / miniP();        // 지금 속도 배수 (표시용)
// 표식 위치: 이번 발이 시작된 때부터의 삼각파. 프레임을 건너뛰어도 위치가 어긋나지 않는다
const miniPos = () => { const x = ((Date.now() - miniT0) % miniP()) / miniP() * 2; return 100 * (x < 1 ? x : 2 - x); };
const miniAcc = d => d > MINI_HALF ? 0 : d < MINI_HALF * MINI_EYE ? K.miniBest : 1 - d / MINI_HALF;
let miniRun = false, miniHold = false, miniT0 = 0, miniZone = 50;
let miniShot = 0, miniHits = 0, miniEarned = 0, miniMsg = "", miniMark = null;

const miniAim = () => { miniZone = MINI_HALF + Math.random() * (100 - 2 * MINI_HALF); miniT0 = Date.now(); };
const miniStep = () => { if (!miniRun || miniHold) return;
  const pin = $("miniPin"); if (pin) pin.style.left = miniPos() + "%";
  requestAnimationFrame(miniStep); };

function miniStart(){
  if (miniRun || miniHold || !save.best || miniLeft() <= 0) return;
  // 판을 시작하는 순간 1회가 빠진다 — 불리한 발을 새로고침으로 무를 수 없게
  save.mini = {day: daysBack(0), n: (miniDay().n | 0) + 1};
  miniShot = 0; miniHits = 0; miniEarned = 0; miniMsg = ""; miniMark = null; miniRun = true;
  miniAim(); put(); drawAll(); miniStep();
}
function miniStop(){
  if (!miniRun || miniHold) return;
  const at = miniPos(), acc = miniAcc(Math.abs(at - miniZone));
  // 혼은 발마다 바로 들어온다 — 스테이지 격파와 달리 환생 정산을 기다리지 않는다
  const pay = Math.round(soulOf(Math.max(1, save.best))
                         * K.miniBudget / (K.miniTries * K.miniShots) * acc);
  save.souls += pay; miniEarned += pay;
  // 멈춘 자리를 그대로 남겨 둔다 — 과녁과 얼마나 어긋났는지 눈으로 봐야 다음 발이 는다
  miniMark = {at, acc};
  const off = Math.abs(at - miniZone).toFixed(1);
  const hit = acc >= K.miniBest ? `<span class="crit">한가운데! +${n(pay)} 혼</span>`
            : acc ? `<span class="win">명중 +${n(pay)} 혼</span> <span class="dim">· ${off}%p 빗나감 (x${acc.toFixed(2)})</span>`
                  : `<span class="lose">빗나갔다</span> <span class="dim">· ${off}%p</span>`;
  miniHold = true;
  if (acc) miniHits++;                     // 맞힐수록 다음 발이 빨라진다
  const last = ++miniShot >= K.miniShots;
  miniMsg = last ? `${hit} · <span class="soul">이번 판 혼 ${n(miniEarned)}</span>`
         : acc  ? `${hit} <span class="dim">· 다음 발 속도 x${miniSpd().toFixed(2)}</span>`
                : `${hit} <span class="dim">· 속도는 그대로다 (x${miniSpd().toFixed(2)})</span>`;
  if (last) miniRun = false;
  put(); drawAll();
  setTimeout(() => {                       // 결과를 보여준 뒤 다음 발
    miniHold = false;
    if (!last) { miniMark = null; miniAim(); drawMini(); miniStep(); } else { drawMini(); }
  }, MINI_HOLD);
}
function drawMini(){
  const rest = miniLeft();
  $("miniLeft").textContent = save.best
    ? (miniRun ? `${Math.min(miniShot + 1, K.miniShots)}/${K.miniShots}발 · 명중 ${miniHits} · 속도 x${miniSpd().toFixed(2)} · 혼 ${n(miniEarned)}`
               : `오늘 ${rest}/${K.miniTries}판`)
    : "";
  if (!save.best) {
    $("mini").innerHTML = '<div class="dim">스테이지를 하나 클리어하면 사냥터가 열린다.</div>';
    return;
  }
  const full = Math.round(soulOf(save.best) * K.miniBudget / K.miniTries * K.miniBest);
  const m = miniMark;
  // 멈춘 표식: 한가운데 금색 · 명중 초록 · 빗나감 빨강. 흐르는 표식은 늘 금색 실선
  const mc = !m ? "" : m.acc >= K.miniBest ? "var(--gold)" : m.acc ? "var(--xp)" : "var(--hp)";
  $("mini").innerHTML =
    `<div class="bar" style="height:22px;position:relative;margin:2px 0 8px;overflow:visible">
       <i style="position:absolute;left:${miniZone - MINI_HALF}%;width:${2 * MINI_HALF}%;height:100%;
          background:var(--soul);opacity:${m && m.acc ? .75 : .35}"></i>
       <i style="position:absolute;left:${miniZone - MINI_HALF * MINI_EYE}%;
          width:${2 * MINI_HALF * MINI_EYE}%;height:100%;background:var(--gold);
          opacity:${m && m.acc >= K.miniBest ? 1 : .6}"></i>
       ${m ? `<i class="${m.acc ? "thud" : "miss"}" style="position:absolute;left:${m.at}%;
          width:5px;height:100%;background:${mc};box-shadow:0 0 8px ${mc}"></i>` : ""}
       <i id="miniPin" style="position:absolute;left:${miniRun && !miniHold ? miniPos() : 0}%;
          width:3px;height:100%;background:var(--fg);
          visibility:${miniRun && !miniHold ? "visible" : "hidden"}"></i></div>
     <button class="big" id="miniBtn" ${(rest <= 0 && !miniRun) || miniHold ? "disabled" : ""}>${
       miniRun ? `멈춰라! (${Math.min(miniShot + 1, K.miniShots)}/${K.miniShots})`
               : rest > 0 ? `사냥 시작 — ${K.miniShots}발` : "오늘 몫을 다 캤다 — 자정에 다시"}</button>
     <div style="margin-top:8px;font-size:13px;min-height:20px">${miniMsg}</div>
     <div class="dim" style="font-size:11px">
       <span class="gold">금색 띠</span>가 한가운데 — 거기서 멈추면 x${K.miniBest},
       <span class="soul">보라 띠</span> 안이면 중심에 가까울수록 더 준다. 밖이면 0이다.
       한 판 ${K.miniShots}발. <b>맞힐수록 표식이 빨라지고</b>, 빗나가면 속도는 그대로다 —
       다 맞히면 최대 혼 ${n(full)}.
       보상은 역대 최고 스테이지(${save.best})를 따라 오른다.
       하루 ${K.miniTries}판, 시작하면 한 판이 빠진다. 자정에 초기화된다.</div>`;
  const b = $("miniBtn");
  if (b) b.onclick = () => miniRun ? miniStop() : miniStart();
}

// ── 유물: 보스가 가끔 떨어뜨린다. 보스마다 하나, 더 높은 등급이 나오면 교체.
// 환생해도 남는 두 번째 영구 성장 축. 등급이 오를 때마다 효과가 두 배.
// 합 100%. 등급이 오를 때마다 효과가 두 배라 신화는 일반의 16배다 — 그래서 0.4% 다
const RARITY = [["일반", 60, "var(--dim)"], ["희귀", 28, "var(--on)"], ["영웅", 10, "var(--soul)"],
                ["전설", 1.6, "var(--gold)"], ["신화", 0.4, "var(--myth)"]];
const AFFIX = {atk: ["ATK", 3, "%"], hp: ["HP", 3, "%"], dfn: ["DEF", 3, "%"], crit: ["CRIT", 0.5, "%p"], soul: ["혼", 3, "%"]};
const relicKey = slot => "boss" + slot.slot;                    // 고정 보스라 칸 번호로 충분하다
const relicOk = r => !!r && !!RARITY[r.r] && !!AFFIX[r.a];      // 고친 저장의 이상한 값은 무시
const relicVal = r => AFFIX[r.a][1] * Math.pow(2, r.r);
const relicSum = a => Object.values(save.relics || {}).filter(relicOk)
  .reduce((s, r) => s + (r.a === a ? relicVal(r) : 0), 0);
let relicNews = "", refundMsg = "";
// 역대 첫 격파 30%, 재격파 1% — 자동 반복이 1초에 한 판이라 재격파 확률이 높으면 금방 다 모인다.
// 신화(0.4%)까지 생겨 도감 끝이 멀어진 만큼 0.5% 에서 올렸다.
const RELIC_FIRST = 0.3, RELIC_AGAIN = 0.01;
// 중복 유물 -> 혼 환산 나누기. 재격파 확률을 2배로 올린 만큼 1/10 에서 1/20 으로 낮춘다 —
// 도감만 두 배로 빨리 차고, 켜 두기만 해서 버는 혼(= 환생 기운 우회)은 그대로다.
// demo() 가 RELIC_AGAIN / RELIC_DUP 를 붙들고 있다.
const RELIC_DUP = 20;
function rollRelic(g, slot, first){
  if (Math.random() >= (first ? RELIC_FIRST : RELIC_AGAIN)) return "";
  let x = Math.random() * 100, r = 0;
  while (r < RARITY.length - 1 && x >= RARITY[r][1]) { x -= RARITY[r][1]; r++; }
  const a = slot.affix;                           // 능력치는 보스가 정한다 — 무작위는 등급뿐
  save.relics = save.relics || {};
  const k = relicKey(slot), old = save.relics[k];
  if (relicOk(old) && old.r >= r) {               // 같거나 낮은 등급 중복 -> 혼으로
    // 반복 파밍 중복은 1/10 — 켜 두기만 해도 혼이 쏟아져 환생 기운(토큰) 제한을 우회하지 않게
    const s = first ? soulOf(g) : Math.round(soulOf(g) / RELIC_DUP); save.souls += s;
    // 획득 메시지처럼 보스 이름을 밝힌다 — 능력치가 보스마다 고정이라 어디서 나왔는지가 정보다
    return `${slot.name} — ${RARITY[r][0]} 유물 중복, 혼 ${n(s)}로 바꿨다`;
  }
  save.relics[k] = {r, a};
  return `${RARITY[r][0]} 유물 획득! ${slot.name} — ${AFFIX[a][0]} +${relicVal({r, a})}${AFFIX[a][2]}`;
}
function drawRelics(){
  const rs = save.relics || {};
  $("relicCount").textContent = `${SLOTS.filter(s => relicOk(rs[relicKey(s)])).length}/${N}`;
  const sum = Object.keys(AFFIX).map(a => [a, relicSum(a)]).filter(([, v]) => v)
    .map(([a, v]) => `${AFFIX[a][0]} +${+v.toFixed(1)}${AFFIX[a][2]}`).join(" · ");
  $("relicNews").innerHTML =
    (relicNews ? `<div class="banner" style="color:var(--gold);border-color:var(--gold)">${relicNews}</div>` : "") +
    `<div class="dim" style="font-size:11px;margin-bottom:6px">보스를 이기면 가끔 그 보스의 유물이 나온다
     (역대 첫 격파 ${RELIC_FIRST*100}%, 재격파 ${RELIC_AGAIN*100}%). 환생해도 남는다.${sum ? " 합계: " + sum : ""}</div>`;
  $("relics").innerHTML = SLOTS.map(s => {
    const r = rs[relicKey(s)];
    // 긴 이름은 한 줄에서 말줄임 — 오른쪽 등급이 줄바꿈되지 않게
    return `<div class="row" style="font-size:12px;padding:2px 0"><span style="flex:1;min-width:0;overflow:hidden;
        text-overflow:ellipsis;white-space:nowrap" title="${s.name}">${s.emoji} ${s.name}</span>${relicOk(r)
      ? `<span style="color:${RARITY[r.r][2]};white-space:nowrap">${RARITY[r.r][0]} · ${AFFIX[r.a][0]} +${relicVal(r)}${AFFIX[r.a][2]}</span>`
      : `<span class="dim" style="white-space:nowrap">? · ${AFFIX[s.affix][0]}</span>`}</div>`;
  }).join("");
}

// 등급·확률표 — 한 번만 그린다 (drawRelics 가 매 판 다시 그리면 열어 둔 표가 닫힌다).
// 접어 두면 못 찾으니 펼친 채로 시작한다 — 접으면 그대로 접혀 있다.
{
  const pct = v => +v.toPrecision(2) + "%";
  const cell = (s, c) => `<span${c ? ` style="color:${c}"` : ""}>${s}</span>`;
  $("relicOdds").innerHTML = `<details open style="margin-bottom:8px;font-size:11px">
    <summary style="cursor:pointer">등급·확률표 — 어느 보스를 파밍할지 정할 때 본다</summary>
    <div style="display:grid;grid-template-columns:repeat(6,auto);gap:2px 12px;justify-content:start;margin-top:6px;white-space:nowrap">
      ${["등급", "등급 확률", "첫 격파", "재격파", "스탯·혼", "CRIT"].map(h => cell(h, "var(--dim)")).join("")}
      ${RARITY.map(([nm, p, c], r) => [cell(nm, c), cell(p + "%"), cell(pct(RELIC_FIRST * p)),
          cell(pct(RELIC_AGAIN * p)), cell("+" + AFFIX.atk[1] * 2 ** r + "%"),
          cell("+" + AFFIX.crit[1] * 2 ** r + "%p")].join("")).join("")}
    </div>
    <div class="dim" style="margin-top:4px">첫 격파·재격파 = 한 판 이겼을 때 그 등급이 나올 확률.
      어떤 능력치가 붙을지는 보스마다 고정이다 — 무작위인 것은 등급뿐이다.
      보스마다 유물은 하나 — 더 높은 등급이 나와야 바뀌고, 같거나 낮으면 혼이 된다.</div></details>`;
}

// ── 환생 보너스: 환생할 때마다 얻는 혼(환생·원정·미션·유물 중복)이 K.rbSoul 씩 영구히 는다
function drawRbBonus(){
  // 혼 배수는 셋(환생·유물·수확)이 곱해진다. 따로 두면 산 특성이 얼마나 일하는지 볼 데가 없다.
  const parts = [];
  let mul = 1;
  const add = (label, pct) => { if (pct > 0) { parts.push(`${label} +${+pct.toFixed(1)}%`); mul *= 1 + pct/100; } };
  add("환생", K.rbSoul * save.rebirths * 100);
  add("유물", relicSum("soul"));
  add("수확", K.tsoul * save.traits.soul);
  $("rbBonus").innerHTML = parts.length
    ? `<span class="dim">얻는 혼</span> <span class="soul">x${mul.toFixed(2)}</span>
       <span class="dim">(${parts.join(" · ")})</span>` : "";
}

const drawAll = () => { drawHero(); drawRbBonus(); drawMissions(); drawMini(); drawExped(); drawStages(); drawRelics(); };

// ── 전투: 턴제 자동. 선공은 SPD, 치명타는 thinking 토큰에서 온다.
const dmgOf = (a, d, crit) =>
  Math.max(1, Math.round((a.atk - d.dfn) * (0.85 + Math.random()*0.3) * (crit ? a.cdmg / 100 : 1)));
const newFighters = b => [{hp:F("hp"), max:F("hp"), atk:F("atk"), dfn:F("dfn"), crit:F("crit"), cdmg:F("cdmg")},
                          {hp:b.hp, max:b.hp, atk:b.atk, dfn:b.dfn}];
// 승리 처리 (수동·자동 공통). 유물이 나오면 그 안내 문구를 돌려준다.
const winStage = g => {
  const first = g > (save.best || 0);             // 역대 처음 깬 스테이지
  if (!save.cleared.includes(g)) save.cleared.push(g);
  markBest(g);
  const drop = rollRelic(g, SLOTS[(g-1)%N], first);
  if (drop) relicNews = drop;
  put();
  return drop;
};

// 연출 없는 즉시 전투 (자동 도전용). 규칙은 fightStart 와 같다.
function quickFight(b){
  const [me, foe] = newFighters(b);
  let myTurn = spdNow() >= b.spd;
  for (let turn = 1; turn <= 200; turn++) {
    const [a, d] = myTurn ? [me, foe] : [foe, me];
    d.hp -= dmgOf(a, d, myTurn && Math.random()*100 < me.crit);
    if (foe.hp <= 0) return true;
    if (me.hp <= 0) return false;
    myTurn = !myTurn;
  }
  return false;
}

let timer = null;
function fightStart(g, slot, b){
  clearInterval(timer);
  const [me, foe] = newFighters(b);
  $("fh").textContent = tierOf(lvNow())[1]; $("fb").textContent = slot.emoji; $("fbn").textContent = slot.name;
  $("log").innerHTML = ""; $("close").disabled = true; $("close").textContent = "전투 중…";
  $("fight").classList.add("on");
  let turn = 0, myTurn = spdNow() >= b.spd;
  say(`${slot.emoji} ${g}스테이지 — ${slot.name} 등장!`);
  paint(me, foe);
  timer = setInterval(() => {
    if (++turn > 200) return end(false, me, foe, g, slot, "소모전 — 화력이 부족하다");
    const [a, d, an, tag] = myTurn ? [me, foe, "나", "fb"] : [foe, me, slot.name, "fh"];
    const crit = myTurn && Math.random()*100 < me.crit;
    const dmg = dmgOf(a, d, crit);
    d.hp -= dmg;
    say(`T${turn} ${an} → ${n(dmg)} 피해` + (crit ? " <span class='crit'>치명타!</span>" : ""), crit);
    const el = $(tag); el.classList.remove("hit"); void el.offsetWidth; el.classList.add("hit");
    paint(me, foe);
    if (foe.hp <= 0) return end(true, me, foe, g, slot);
    if (me.hp <= 0) return end(false, me, foe, g, slot,
      "토큰을 더 쓰거나 환생해서 영구 특성을 올려라");
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
    const drop = winStage(g);
    if (drop) say(`<span class="gold">${drop}</span>`);
    if (g % N === 0) say(`<span class="win">${g/N}층 완주 — ${g/N+1}층이 열렸다.</span>`);
    say(`<span class="dim">혼은 환생할 때 정산된다.</span>`);
  } else {
    say(`<span class="lose">패배.</span> <span class="dim">${why}</span>`);
  }
  $("close").disabled = false; $("close").textContent = "닫기";
  drawAll();
}
$("close").onclick = () => $("fight").classList.remove("on");

(async () => {
  if (SERVED) {
    // 실패해도 rev 0 으로 남아, 이후 쓰기는 서버에서 409 로 막히고 서버 저장을 불러온다 — 진행을 덮지 않는다
    try { await pull(); } catch(e) { note("저장을 불러오지 못했다 — 게임 서버를 확인하고 새로고침해라."); }
  } else {
    try { Object.assign(save, JSON.parse(localStorage.getItem("trpg") || "{}")); } catch(e) {}
    fill();
  }
  if (!save.best && maxCleared()) { save.best = maxCleared(); put(); }   // 옛 저장본 이관
  if (!save.exped.seenExp) { save.exped.seenExp = H.exp; put(); }
  if (typeof save.rbExp !== "number" || save.rbExp > H.exp) { fixRb(); put(); }
  if (refundMsg) { put(); note(refundMsg); }     // 환불을 바로 저장에 남긴다
  drawAll();

  // 돌아왔을 때 그동안의 성과를 알려준다
  const secs = idleSecs();
  if (secs > 300 && save.best) {
    const h = Math.floor(secs/3600), m = Math.floor(secs/60) % 60;
    $("expedBanner").innerHTML =
      `<div class="banner">원정대가 ${h}시간 ${m}분 동안 ${n(pending())} 혼을 캐왔다.</div>`;
  }
})();

// 새 버전 알림. 확인은 서버(파이썬)가 하고 결과를 6시간 재사용한다 — 페이지가 직접 바깥으로 나가지 않는다.
if (SERVED) fetch("update", {cache: "no-store"}).then(r => r.json()).then(u => {
  if (!u || !u.newer) return;
  // 어느 설치 방식이든 서버가 알아서 올린다 — 앱은 DMG 를 직접 받아 번들을 갈아끼운다.
  // 링크(target=_blank)로 두면 메뉴 막대 팝오버(WKWebView)가 새 창을 못 띄워 눌러도 무반응이다.
  const isApp = u.kind === "app";
  $("updBanner").innerHTML = `<div class="banner" style="color:var(--gold);border-color:var(--gold);
    display:flex;align-items:center;gap:10px;justify-content:space-between;flex-wrap:wrap">
    <span id="updMsg">새 버전 ${u.latest} — 지금은 ${u.current}</span>
    <button id="updBtn">업데이트</button></div>`;
  const b = $("updBtn");
  if (b) b.onclick = async () => {
    b.disabled = true; b.textContent = isApp ? "받는 중…" : "업데이트 중…";
    try {
      const r = await fetch("update", {method: "POST", cache: "no-store",
        headers: {"Content-Type": "application/json"}, body: "{}"});
      const d = await r.json();
      $("updMsg").textContent = d.msg;
      b.textContent = d.ok ? (isApp ? "다시 뜬다" : "완료") : "실패";
      if (!d.ok) b.disabled = false;              // 다시 눌러볼 수 있게
    } catch (e) {
      $("updMsg").textContent = isApp ? `${u.url} 에서 DMG 를 받아라` : "업데이트하지 못했다 — " + u.cmd;
      b.textContent = "실패"; b.disabled = false;
    }
  };
}).catch(() => {});

// 보고 있는 동안에도 계속 쌓인다
setInterval(() => { if (!$("fight").classList.contains("on")) { drawExped(); autoStep(); } }, 1000);

// 다른 창에서 진행했을 수 있다 -> 이 창으로 돌아올 때 최신 저장을 받는다 (전투 중에는 건드리지 않는다)
window.addEventListener("focus", () => {
  if (!SERVED || $("fight").classList.contains("on")) return;
  sync = sync.then(pull).then(drawAll).catch(() => {});
});
</script></body></html>
"""
TEMPLATE = TEMPLATE.replace("__PT__", str(PT_PER_LEVEL))


HOOK_MARK = "token_rpg"          # 우리가 넣은 훅을 식별하는 표식


def hook_command():
    """이 파이썬으로 이 모듈을 돌린다. PATH에 의존하지 않아 훅 환경에서도 안전하다."""
    if os.name == "nt":                  # 훅은 cmd.exe 가 돌린다 — /dev/null 도 true 도 없다
        return f'"{sys.executable}" -m token_rpg build --quiet >NUL 2>&1 || exit /b 0'
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


REPO = "YCYEOM/token-rpg"
PKG = "token-rpg"              # PyPI 배포판 이름
UPDATE_TTL = 60              # 확인 결과를 이만큼만 재사용한다. 0 으로 두면 팝오버를 여닫을 때마다
                             # GitHub 을 두드리는데, 인증 없이는 시간당 60번이 상한이다.


def _ver(v):
    """'v0.4.0' -> (0, 4, 0). 비교용이라 숫자가 아닌 꼬리는 버린다."""
    out = []
    for part in str(v).lstrip("vV").split(".")[:3]:
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out + [0] * (3 - len(out)))


def install_kind():
    """어떻게 깔린 건지. 업그레이드 방법이 저마다 다르고, 앱 번들은 아예 못 고친다."""
    here = os.path.abspath(__file__)
    if ".app/Contents/" in here:
        return "app"                      # 메뉴 막대 앱 안의 사본 — DMG 를 다시 받아야 한다
    for mark, kind in (("/uv/tools/", "uv"), ("/pipx/venvs/", "pipx")):
        if mark in here.replace(os.sep, "/"):
            return kind
    return "pip"


# PyPI 에 올라간 뒤로는 셋 다 배포판을 받는다 — 릴리스 태그로 새 버전을 알리면서
# 설치는 main 최신 커밋에서 하던 엇갈림이 없어진다.
UPDATE_CMD = {
    "uv":   [["uv", "tool", "install", "--force", "--no-cache", PKG]],
    "pipx": [["pipx", "install", "--force", PKG]],
    "pip":  [[sys.executable, "-m", "pip", "install", "--upgrade", PKG]],
}


def update_check(cfg=None, force=False):
    """{current, latest, url, kind, cmd, newer}. 네트워크가 막히면 latest 가 없다."""
    cfg = cfg if cfg is not None else load_config()
    seen = cfg.get("update") or {}
    fresh = not force and time.time() - (seen.get("at") or 0) < UPDATE_TTL
    if not fresh and cfg.get("updateCheck", True):
        try:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{REPO}/releases/latest",
                headers={"Accept": "application/vnd.github+json",
                         "User-Agent": f"token-rpg/{__version__}"})
            with urllib.request.urlopen(req, timeout=3) as r:
                seen = {"at": time.time(), "tag": json.load(r).get("tag_name") or ""}
            cfg["update"] = seen
            save_config(cfg)
        except Exception:                 # 오프라인·차단·API 제한 — 조용히 넘어간다
            # 실패도 시각을 남긴다. 안 남기면 다음 호출이 곧바로 또 두드려, 제한에 걸린 뒤
            # 팝오버를 열 때마다 헛물을 켠다. 아는 태그는 그대로 들고 간다.
            cfg["update"] = seen = {**seen, "at": time.time()}
            save_config(cfg)
    kind = install_kind()
    tag = seen.get("tag") or ""
    cmd = UPDATE_CMD.get(kind, [])
    return {"current": __version__, "latest": tag.lstrip("vV"), "kind": kind,
            "url": f"https://github.com/{REPO}/releases/latest",
            "cmd": " ".join(cmd[0]) if cmd else "",
            "newer": bool(tag) and _ver(tag) > _ver(__version__)}


def app_bundle(path=None):
    """이 사본을 담고 있는 .app 경로. 앱 안에서 도는 게 아니면 None."""
    here = os.path.abspath(path or __file__)
    i = here.find(".app" + os.sep + "Contents" + os.sep)
    return here[:i + 4] if i > 0 else None


def _dmg_url():
    """최신 릴리스에 붙은 DMG 주소. CI(macos-dmg.yml)가 릴리스마다 올려 둔다."""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/releases/latest",
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": f"token-rpg/{__version__}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        for a in json.load(r).get("assets") or []:
            if str(a.get("name", "")).endswith(".dmg"):
                return a.get("browser_download_url")
    return None


# 앱은 자기가 도는 동안 자기를 갈아치울 수 없다 -> 앱을 닫고, 바꾸고, 다시 띄우는 일을
# 분리된 셸에 맡긴다. 새 번들을 옆에 펼친 뒤 이름만 바꾸므로 중간에 죽어도 옛 앱이 남는다.
_SWAP_SH = r"""
APP="$1"; DMG="$2"; PID="$3"
kill -TERM "$PID" 2>/dev/null
n=0; while kill -0 "$PID" 2>/dev/null && [ "$n" -lt 80 ]; do sleep 0.25; n=$((n + 1)); done
MNT=$(hdiutil attach "$DMG" -nobrowse -readonly | grep -o '/Volumes/.*' | tail -1)
NEW=$(find "$MNT" -maxdepth 1 -name '*.app' | head -1)
if [ -n "$NEW" ]; then
  rm -rf "$APP.new" "$APP.old"
  if ditto "$NEW" "$APP.new"; then
    mv "$APP" "$APP.old" && mv "$APP.new" "$APP" || mv "$APP.old" "$APP"
  fi
  rm -rf "$APP.new" "$APP.old"
fi
[ -n "$MNT" ] && hdiutil detach "$MNT" -quiet
rm -f "$DMG"
open "$APP"
"""


def update_app(bundle):
    """DMG 를 직접 받아 번들을 갈아끼운다. 브라우저를 거치지 않으니 quarantine 딱지도
    안 붙는다 — 그 딱지는 서명 유무가 아니라 받은 프로그램(브라우저)이 찍는 것이다."""
    if not os.access(os.path.dirname(bundle), os.W_OK) or not os.access(bundle, os.W_OK):
        return False, f"{bundle} 를 고칠 권한이 없다 — 직접 받아 덮어써라"
    try:
        url = _dmg_url()
        if not url:
            return False, "이번 릴리스에는 DMG 가 없다"
        dmg = os.path.join(data_dir(), "update.dmg")
        with urllib.request.urlopen(url, timeout=120) as r, open(dmg, "wb") as f:
            shutil.copyfileobj(r, f)
    except Exception as e:                            # 네트워크·디스크 — 옛 앱은 그대로다
        return False, f"DMG 를 받지 못했다 — {e}"
    # 부모가 곧 메뉴 막대 앱이다(앱이 serve 를 띄운다). 스크립트는 우리가 죽어도 살아야 하므로
    # 새 세션으로 떼어 놓는다 — 앱을 닫는 순간 이 파이썬도 같이 죽는다.
    subprocess.Popen(["/bin/sh", "-c", _SWAP_SH, "swap", bundle, dmg, str(os.getppid())],
                     start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True, "새 버전을 받았다 — 앱이 잠깐 닫혔다 다시 뜬다"


_real_update_app = update_app


def update_run():
    """감지한 방식으로 업그레이드한다. 지금 도는 프로세스를 갈아치우므로 따로 띄우고 기다린다."""
    kind = install_kind()
    bundle = app_bundle()
    if kind == "app" and bundle:
        ok, msg = update_app(bundle)
        if ok:
            return ok, msg
        # 자동으로 못 바꿨을 때만 사람 손에 넘긴다 — 주소는 알려 준다.
        url = f"https://github.com/{REPO}/releases/latest"
        try:
            webbrowser.open(url)
        except Exception:                            # 브라우저도 못 띄웠다
            return False, f"{msg} ({url})"
        return False, f"{msg} — 릴리스 페이지를 열었다"
    cmd = UPDATE_CMD.get(kind)
    if not cmd:
        return False, "이 설치 방식은 자동 업그레이드를 지원하지 않는다"
    try:
        r = subprocess.run(cmd[0], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    if r.returncode:
        return False, (r.stderr or r.stdout or "").strip()[-300:]
    return True, "업데이트 완료 — 앱이나 창을 다시 열면 새 버전이다"


def cmd_update(args):
    info = update_check(force=True)
    if not info["latest"]:
        print(f"현재 {info['current']} — 최신 버전을 확인하지 못했다 (네트워크·릴리스 없음)")
        return 1
    if not info["newer"]:
        print(f"최신이다 ({info['current']})")
        return 0
    print(f"새 버전 {info['latest']} (지금 {info['current']})")
    if info["kind"] == "app":
        print(f"메뉴 막대 앱은 DMG 를 다시 받아야 한다: {info['url']}")
        return 0
    ok, msg = update_run()
    print(msg)
    return 0 if ok else 1


def _no_data_hint():
    # 기준이 걸려 있으면 그게 진짜 이유다 — 경로부터 늘어놓으면 엉뚱한 데를 뒤지게 된다
    ts = since_ts()
    if ts:
        print(f"{datetime.fromtimestamp(ts).date()} 이후로는 사용 기록이 없다 (설치 시점부터 센다).")
        print("AI 코딩을 한 번 하면 캐릭터가 생긴다.")
        print("예전 기록까지 세려면:     token-rpg since all\n")
    print("어떤 프로바이더에서도 사용 기록을 찾지 못했다. 찾아본 곳:")
    for pid, spec in PROVIDERS.items():
        for r in PROVIDERS[pid]["roots"]():
            print(f"  {spec['label']:14} {r}")
    print("\n로그가 다른 곳에 있으면:  token-rpg scan add <프로바이더> <경로>")
    print("목록 보기:                token-rpg providers")


def cmd_since(args):
    """언제부터 센 토큰을 캐릭터로 칠지. 인자가 없으면 지금 기준만 보여준다."""
    cfg = load_config()
    if args.when:
        if args.when == "all":
            cfg["since"] = 0
        elif args.when == "now":
            cfg["since"] = time.time()
        else:
            try:
                cfg["since"] = datetime.strptime(args.when, "%Y-%m-%d").timestamp()
            except ValueError:
                print("YYYY-MM-DD 또는 all(전체) · now(지금부터)")
                return 1
        save_config(cfg)
    ts = since_ts(cfg)
    print("전체 기록을 센다" if not ts
          else f"{datetime.fromtimestamp(ts).date()} 이후 기록만 센다")
    if ts:
        print("바꾸려면: token-rpg since all | now | YYYY-MM-DD")
    return 0


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
    # 갱신 버튼은 "지금 것으로 맞춰라"는 뜻이다 — 6시간 캐시가 남아 새 릴리스가 안 보이면
    # 눌러도 아무 일이 없다. 결과는 config 에 떨어져 서버의 GET /update 가 곧바로 읽는다.
    if getattr(args, "check_update", False):
        update_check(force=True)
    agg, _, _, _, _ = collect_all()
    if not agg.get("calls") and not glob.glob(os.path.join(snap_dir(), "*.json")):
        _no_data_hint()
        return 1
    path, d = build()
    h = d["hero"]
    if not getattr(args, "quiet", False):
        print(f"{path}")
        print(f"Lv.{h['level']} {h['title']} {h['emoji']}  HP {h['hp']} ATK {h['atk']} "
              f"DEF {h['dfn']} CRIT {h['crit']}%  배분 {h['points']}pt  던전 {len(d['dungeons'])}개")
        print(f"합산 기기 {len(d['hosts'])}대")
    return 0


SERVE_PORT = 8765          # 메뉴 막대 앱(macos/TokenRPGMenuBar.swift)과 같은 값


class _GameHandler(http.server.BaseHTTPRequestHandler):
    """127.0.0.1 전용. 게임 페이지와 저장 파일 하나만 다룬다."""
    snaps = None                                   # 테스트에서 저장 폴더를 바꿀 때만

    def _host_ok(self):
        # DNS 리바인딩 방어: 외부 도메인이 127.0.0.1 로 풀려 들어온 요청을 거른다
        port = self.server.server_address[1]
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "host"})
        if self.path in ("/", "/game.html"):
            try:
                with open(game_path(), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                return self._send(404, {"error": "no_game"})
        if self.path == "/save":
            try:
                return self._send(200, read_save(self.snaps))
            except (OSError, ValueError):
                return self._send(500, {"error": "save_unreadable"})
        if self.path == "/health":
            return self._send(200, {"app": "token-rpg", "version": __version__})
        if self.path == "/update":
            return self._send(200, update_check())
        self._send(404, {"error": "not_found"})

    def do_POST(self):
        # 업그레이드 실행. PUT 과 같은 방어 — 127.0.0.1 호스트 검사 + JSON 강제로
        # 다른 사이트가 보내는 요청은 사전 요청(OPTIONS)에서 막힌다.
        if not self._host_ok() or self.path != "/update":
            return self._send(403, {"error": "forbidden"})
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self._send(415, {"error": "json_only"})
        ok, msg = update_run()
        self._send(200 if ok else 500, {"ok": ok, "msg": msg})

    def do_PUT(self):
        # 다른 사이트가 보낸 JSON PUT 은 사전 요청(OPTIONS)에서 막힌다 — OPTIONS 는 받지 않는다
        if not self._host_ok() or self.path != "/save":
            return self._send(403, {"error": "forbidden"})
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self._send(415, {"error": "json_only"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if not 0 < n <= 65536:
            return self._send(413, {"error": "size"})
        try:
            body = json.loads(self.rfile.read(n))
            base, save = body["base"], body["save"]
        except (ValueError, KeyError, TypeError):
            return self._send(400, {"error": "bad_request"})
        if type(base) is not int or not isinstance(save, dict):
            return self._send(400, {"error": "bad_request"})
        try:
            ok, cur = write_save(base, save, self.snaps)
        except (OSError, ValueError):
            return self._send(500, {"error": "save_unwritable"})
        if ok:
            return self._send(200, {"rev": cur["rev"]})
        self._send(409, cur)

    def log_message(self, *args):
        pass


def make_server(port=SERVE_PORT):
    return http.server.ThreadingHTTPServer(("127.0.0.1", port), _GameHandler)


def server_running(port=SERVE_PORT):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5) as r:
            return json.load(r).get("app") == "token-rpg"
    except (OSError, ValueError):
        return False


def cmd_open(args):
    rc = cmd_build(args)
    if rc:
        return rc
    url = f"http://127.0.0.1:{SERVE_PORT}/"
    if server_running():                 # 메뉴 막대 앱이 이미 띄워 둠
        webbrowser.open(url)
        return 0
    try:
        srv = make_server()
    except OSError:
        print(f"포트 {SERVE_PORT} 를 다른 프로그램이 쓰고 있다.")
        return 1
    webbrowser.open(url)                 # 포트를 먼저 열어 둬서 브라우저가 연결에 실패하지 않는다
    print(f"{url}  (저장: {save_path()})\n끝내려면 Ctrl+C")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_serve(args):
    """메뉴 막대 앱이 띄우는 서버. 앱이 끝날 때 함께 끝난다."""
    srv = None
    for _ in range(10):                 # 앱을 다시 켜면 이전 서버가 막 내려가는 중일 수 있다
        try:
            srv = make_server()
            break
        except OSError:
            time.sleep(0.5)
    if srv is None:
        return 0 if server_running() else 1     # token-rpg open 이 띄운 우리 서버면 그걸 쓴다
    parent = os.getppid()
    def watch():                        # 앱이 강제 종료돼도(applicationWillTerminate 없음) 고아로 남지 않게
        while os.getppid() == parent:
            time.sleep(2)
        os._exit(0)
    threading.Thread(target=watch, daemon=True).start()
    srv.serve_forever()
    return 0


def _exped_full(snaps=None):
    """메뉴 막대 배지용: 원정이 가득 찼는가. JS 와 같은 규칙 — 깬 스테이지가 있어야 돌고,
    마지막 수령 뒤 IDLE_CAP_H 시간이 지나면 가득."""
    try:
        sv = read_save(snaps).get("save") or {}
    except (OSError, ValueError):
        return False
    since = (sv.get("exped") or {}).get("since")
    return (bool(sv.get("best")) and isinstance(since, (int, float))
            and time.time() * 1000 - since >= IDLE_CAP_H * 3600 * 1000)


def _saved_trans(snaps=None):
    """저장에 적힌 초월 횟수. 못 읽으면 0 — 배지가 조금 어긋날 뿐 진행에는 영향이 없다."""
    try:
        return int((read_save(snaps).get("save") or {}).get("trans") or 0)
    except (OSError, ValueError, TypeError):
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
    h = hero(agg, _saved_trans())
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
        "today": sum(days.get(datetime.now().date().isoformat(), {}).values()),
        "expedFull": _exped_full(),
        "gamePath": game_path(),
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="token-rpg",
        description="Claude Code 토큰 사용량으로 성장하는 턴제 RPG")
    p.add_argument("--version", action="version", version=f"token-rpg {__version__}")
    # build 용 플래그. 최상위에만 두면 "build --quiet" 가 argparse 오류로 죽는다 — 윈도우
    # Stop 훅이 그 형태였다. SUPPRESS 라 서브파서 사본이 앞에서 받은 값을 덮지 않는다.
    common = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    common.add_argument("-q", "--quiet", action="store_true", help="출력 억제")
    common.add_argument("--check-update", action="store_true",
                        help="캐시를 무시하고 새 버전을 다시 확인한다 (메뉴 막대 갱신 버튼이 쓴다)")
    p.add_argument("-q", "--quiet", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--check-update", action="store_true", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("build", parents=[common],
                   help="사용량을 다시 집계해 game.html 갱신 (기본)")
    sub.add_parser("open", help="갱신한 뒤 브라우저로 연다 (저장 서버 포함, Ctrl+C 로 종료)")
    sub.add_parser("serve", help="게임과 저장을 127.0.0.1 로 제공 (메뉴 막대 앱용)")
    sub.add_parser("status", help="현재 스탯을 JSON으로 출력 (메뉴 막대 앱용)")
    sub.add_parser("export", help="이 PC의 스냅샷만 갱신 (다른 기기와 합산용)")
    sub.add_parser("balance", help="난이도·환생 곡선 표 출력")
    sub.add_parser("selftest", help="집계·병합·밸런스 자체 검증")
    sub.add_parser("where", help="데이터 위치 출력")
    sub.add_parser("update", help="새 버전 확인 후 업그레이드")
    sn = sub.add_parser("since", help="언제부터의 토큰을 셀지 (기본: 설치 시점)")
    sn.add_argument("when", nargs="?", help="all · now · YYYY-MM-DD")
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
    return p


def main(argv=None):
    if os.name == "nt":                  # 리다이렉트되면 기본 인코딩이 cp949 -> 이모지에서 죽는다
        for st in (sys.stdout, sys.stderr):
            try:
                st.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass
    a = build_parser().parse_args(argv)

    cmd = a.cmd or "build"
    if cmd == "build":
        return cmd_build(a)
    if cmd == "open":
        return cmd_open(a)
    if cmd == "serve":
        return cmd_serve(a)
    if cmd == "status":
        return cmd_status(a)
    if cmd == "export":
        print(save_snapshot()[0]); return 0
    if cmd == "balance":
        agg, projects, _, _ = merge()
        if not projects:
            _no_data_hint(); return 1
        balance(hero(agg), dungeons()); return 0
    if cmd == "since":
        return cmd_since(a)
    if cmd == "update":
        return cmd_update(a)
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


def _check_trans():
    """초월: 레벨만 되돌리고 준 배분 포인트는 적립한다.

    같은 식이 JS 쪽 lvNow()·pointsOf() 에도 있다. 한쪽만 고치면 화면과 메뉴 막대
    배지가 어긋나므로 여기서 표를 박아 둔다."""
    T = TRANS_EXP
    table = [                     # (누적토큰, 초월, 기대 레벨, 기대 배분pt)
        (0, 0, 1, 2), (T - 1, 0, MAX_LV - 1, 196), (T, 0, MAX_LV, 198),
        (T, 1, 1, 200), (2 * T, 1, MAX_LV, 396), (2 * T, 2, 1, 398),
        (T, 5, 1, 200), (0, 3, 1, 2),             # 쓴 토큰보다 많이 초월할 수는 없다
        (3 * T + 123_456, 2, MAX_LV, 594),
    ]
    for total, tr, lv, pt in table:
        h = hero({"input": total, "output": 0, "cache_read": 0, "thinking": 0, "calls": 0}, tr)
        assert (h["level"], h["points"]) == (lv, pt), (total, tr, h["level"], h["points"], lv, pt)
    assert hero({"input": T, "output": 0, "cache_read": 0, "thinking": 0, "calls": 0})["canTrans"]


def _check_early():
    """극초반: 토큰 한 톨 없는 Lv.1 이 1스테이지를 이겨야 한다. 안 그러면 게임이 시작조차
    안 된다 — 예전엔 1스테이지 보스를 잡는 데 누적 200만 토큰이 필요했다."""
    zero = collections.Counter({"input": 0, "output": 0, "cache_read": 0, "thinking": 0,
                                "calls": 0, "sessions": 0, "days": 0})
    h = hero(zero)
    assert h["level"] == 1 and h["atk"] == 0, h
    assert reach(h) >= 1, "무토큰 Lv.1 이 1스테이지도 못 깬다 — 시작 벽이 너무 높다"

    # 램프는 EARLY_G 에서 원래 곡선에 정확히 합류하고, 그 전까지 단조 증가여야 한다
    assert boss(EARLY_G)["hp"] == round(B_HP * STEP ** (EARLY_G - 1)), boss(EARLY_G)
    hps = [boss(g)["hp"] for g in range(1, EARLY_G + 3)]
    assert hps == sorted(hps) and hps[0] < B_HP, hps

    # 램프가 후반까지 새지 않는다 — 초반 몇 칸을 쉽게 하려고 전체를 무르게 만들면 곤란하다
    h10 = hero(collections.Counter({"input": 7_000_000, "output": 3_000_000,
                                    "cache_read": 0, "thinking": 0, "calls": 0}))
    assert reach(h10) <= 2 * len(BOSSES), f"1천만 토큰으로 {reach(h10)}스테이지 — 벽이 사라졌다"


def _check_update_app():
    """앱은 DMG 를 직접 받아 번들을 갈아끼운다. 브라우저를 거치지 않으니 quarantine 도 안 붙는다
    (그 딱지는 서명이 아니라 받은 프로그램이 찍는다). 못 바꿀 때만 릴리스 페이지로 넘긴다."""
    import tempfile
    real_kind, real_bundle, real_open = install_kind, app_bundle, webbrowser.open
    opened, swapped = [], []
    try:
        globals()["install_kind"] = lambda: "app"
        webbrowser.open = lambda u: opened.append(u) or True

        with tempfile.TemporaryDirectory() as d:
            bundle = os.path.join(d, "Token RPG.app")
            os.makedirs(os.path.join(bundle, "Contents", "Resources"))
            globals()["app_bundle"] = lambda: bundle

            # 받아서 갈아끼운다 — 셸에 넘기는 인자가 번들·DMG·앱 PID 순이어야 한다
            globals()["update_app"] = lambda b: swapped.append(b) or (True, "새 버전을 받았다")
            ok, msg = update_run()
            assert ok and swapped == [bundle] and not opened, (ok, msg, swapped, opened)

            # 못 바꿨으면 실패로 알리고 릴리스 페이지를 대신 연다
            globals()["update_app"] = lambda b: (False, "권한이 없다")
            ok, msg = update_run()
            assert not ok and "권한이 없다" in msg, msg
            assert opened == [f"https://github.com/{REPO}/releases/latest"], opened

            # 번들 경로는 .app 에서 끊는다 — Contents 안쪽까지 넘기면 그걸 통째로 지우게 된다
            inside = os.path.join(bundle, "Contents", "Resources", "token_rpg.py")
            assert real_bundle(inside) == bundle, real_bundle(inside)
            assert real_bundle("/usr/local/bin/token_rpg.py") is None

        # 쓸 수 없는 번들은 건드리지 않는다
        ok, msg = update_app("/nonexistent/Token RPG.app")
        assert not ok and "권한" in msg, msg
    finally:
        globals()["install_kind"] = real_kind
        globals()["app_bundle"] = real_bundle
        globals()["update_app"] = _real_update_app
        webbrowser.open = real_open


def _check_cli_flags():
    """build 용 플래그는 서브커맨드 앞뒤 어디든 먹어야 한다. 최상위에만 달아 뒀을 때
    윈도우 Stop 훅의 `token_rpg build --quiet` 가 argparse 오류로 조용히 죽고 있었다."""
    import contextlib, io
    for argv in (["build", "--quiet"], ["--quiet", "build"], ["build", "-q"],
                 ["build", "--check-update"], ["--check-update", "build"]):
        with contextlib.redirect_stderr(io.StringIO()):
            a = build_parser().parse_args(argv)          # SystemExit 면 파서가 거부한 것
        assert a.cmd == "build", (argv, a)
        assert getattr(a, "quiet", False) or getattr(a, "check_update", False), (argv, a)
    assert "build --quiet" in hook_command() or os.name != "nt"


def demo():
    """진짜 설치를 건드리지 않게 임시 데이터 폴더에서 돈다 — 봉인 플래그가 사용자 config 로
    새면 다음 실행에서 옛 평문 파일을 못 받는다."""
    import tempfile
    _demo.real = merge()                  # 밸런스 검증용 — 진짜 스냅샷은 지금 읽어 둔다
    keep = dict(os.environ)
    with tempfile.TemporaryDirectory() as home:
        os.environ["TOKEN_RPG_HOME"] = home
        os.environ.pop("TOKEN_RPG_SNAPSHOTS", None)
        try:
            _demo()
        finally:
            os.environ.clear(); os.environ.update(keep)


def _demo():
    _check_trans()
    _check_update_app()
    _check_cli_flags()
    assert level_of(0) == 1 and level_of(exp_for(2)) == 2 and level_of(exp_for(2) - 1) == 1
    assert [level_of(e) for e in (5_460, 268_800, 2_211_300)] == [2, 5, 10]   # 저렙 완만 구간
    assert exp_for(MAX_LV) == TRANS_EXP and abs(TRANS_EXP / 480_200_000 - 1) < 0.01, \
        "초월 비용이 예전 4.8억에서 벗어났다 — 저렙만 손보기로 한 약속이 깨진다"
    assert all(exp_for(lv) < exp_for(lv + 1) for lv in range(1, MAX_LV)), "레벨 곡선이 뒤집혔다"
    assert tier_of(1)[2] == "토큰 알" and tier_of(99)[2] == "토큰 드래곤"
    _check_early()
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "proj"))
        rec = {"timestamp": datetime.now(timezone.utc).isoformat(),   # 스냅샷은 최근 60일만 남긴다
               "message": {"id": "a", "model": "claude-opus-5",
               "usage": {"input_tokens": 10, "output_tokens": 20, "cache_creation_input_tokens": 5,
                         "cache_read_input_tokens": 100, "output_tokens_details": {"thinking_tokens": 7}}}}
        with open(os.path.join(d, "proj", "s.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n" + json.dumps(rec) + "\n")  # 중복 id -> 1회만
        agg, projects, _, days = collect(d)
        assert agg["calls"] == 1 and agg["input"] == 15 and agg["output"] == 20
        assert agg["thinking"] == 7 and len(days) == 1 and projects["claude-code\tproj"] == 35
        assert sum(days.values()) == 35, dict(days)             # 날짜별 합 = EXP 기여
        snaps = os.path.join(d, "snaps")
        _, s1 = save_snapshot(d, snaps)
        _dump_sealed(os.path.join(snaps, "otherpc.json"), {**s1, "host": "otherpc"})
        m, mp, _, hs = merge(snaps)
        assert m["input"] == 30 and mp["claude-code\tproj"] == 70 and m["days"] == 1 and len(hs) == 2
        assert [sum(v.values()) for v in merge.daily.values()] == [70], merge.daily   # 기기 합산

        # 봉인: 서명을 고치면 그 PC 집계는 세지 않는다
        with open(os.path.join(snaps, "otherpc.json"), encoding="utf-8") as f:
            sealed = f.read()
        assert '"host"' not in sealed and unseal(sealed)["host"] == "otherpc", sealed[:60]
        with open(os.path.join(snaps, "otherpc.json"), "w", encoding="utf-8") as f:
            f.write(sealed[:-1] + ("0" if sealed[-1] != "0" else "1"))
        import contextlib, io
        with contextlib.redirect_stderr(io.StringIO()):         # 건너뜀 안내는 여기선 정상
            m2, _, _, hs2 = merge(snaps)
        assert m2["input"] == 15 and len(hs2) == 1, (dict(m2), hs2)

        # 평문 스냅샷은 받아 준다 — 한 PC 안에서도 옛 설치본의 훅이 평문으로 덮어쓴다.
        # 여기서 거부하면 그 PC 토큰이 0 이 된다 (v0.10.0 이 실제로 그랬다).
        with open(os.path.join(snaps, "otherpc.json"), "w", encoding="utf-8") as f:
            json.dump({**s1, "host": "otherpc"}, f)
        m3, _, _, hs3 = merge(snaps)
        assert m3["input"] == 30 and len(hs3) == 2, (dict(m3), hs3)

    # 저장 서버: 오래된 rev 의 쓰기·깨진 파일 덮어쓰기·다른 Host·JSON 아닌 쓰기는 거부한다
    with tempfile.TemporaryDirectory() as d:
        ok, cur = write_save(0, {"souls": 1}, d)
        assert ok and cur["rev"] == 1
        with open(save_path(d), encoding="utf-8") as f:     # 봉인: 눈으로 고칠 수 없는 한 줄
            blob = f.read()
        assert "souls" not in blob and unseal(blob) == cur, blob[:60]
        start_sealing(d)                                    # 봉인 시대 — 평문 저장은 이제 거부
        with open(save_path(d), "w", encoding="utf-8") as f:
            json.dump({"rev": 1, "save": {"souls": 10 ** 9}}, f)
        try:
            read_save(d)
            raise AssertionError("평문으로 써넣은 저장이 먹혔다")
        except ValueError:
            pass
        _dump_sealed(save_path(d), cur)                     # 원상복구
        ok, cur = write_save(0, {"souls": 999}, d)                 # 오래된 창
        assert not ok and cur["save"] == {"souls": 1}, cur
        with open(save_path(d), "w", encoding="utf-8") as f:
            f.write("{반쪽")
        try:
            write_save(1, {}, d)
            raise AssertionError("깨진 저장 파일을 덮어썼다")
        except ValueError:
            pass
        os.remove(save_path(d))

        # 초월 횟수는 저장에서 읽는다 — 못 읽으면 0 이어야 한다(배지가 어긋날 뿐 진행은 무사)
        assert _saved_trans(d) == 0
        write_save(0, {"trans": 2}, d)
        assert _saved_trans(d) == 2
        os.remove(save_path(d))

        # 메뉴 막대 ⛏ 배지: 깬 스테이지가 있고 마지막 수령 뒤 IDLE_CAP_H 가 지났을 때만
        assert not _exped_full(d)
        write_save(0, {"best": 5, "exped": {"since": time.time() * 1000}}, d)
        assert not _exped_full(d)
        write_save(1, {"best": 5, "exped": {"since": (time.time() - IDLE_CAP_H * 3600 - 60) * 1000}}, d)
        assert _exped_full(d)
        os.remove(save_path(d))

        import http.client
        class _H(_GameHandler):
            snaps = d
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        def req(method, body=None, host=None, ctype="application/json"):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request(method, "/save", body=None if body is None else json.dumps(body),
                      headers={"Host": host or f"127.0.0.1:{port}", "Content-Type": ctype})
            r = c.getresponse()
            return r.status, json.loads(r.read())
        try:
            assert req("GET") == (200, {"rev": 0})
            assert req("PUT", {"base": 0, "save": {"souls": 5}}) == (200, {"rev": 1})
            assert req("PUT", {"base": 0, "save": {"souls": 9}})[0] == 409
            assert req("PUT", {"base": 1, "save": {"souls": 7}}, host="evil.example")[0] == 403
            assert req("PUT", {"base": 1, "save": {"souls": 7}}, ctype="text/plain")[0] == 415
            assert req("PUT", {"base": 1, "save": [1]})[0] == 400
            assert req("GET")[1] == {"rev": 1, "save": {"souls": 5}}
        finally:
            srv.shutdown(); srv.server_close()

    # 프로바이더 리더: 각자 자기 형식을 제대로 읽는지 픽스처로 확인
    with tempfile.TemporaryDirectory() as d:
        # Codex: total_token_usage 는 세션 누적 -> 마지막 값만 센다
        cx = os.path.join(d, "2026", "09", "10")
        os.makedirs(cx)
        def _tc(inp, cached, out, reason, tot):
            return json.dumps({"timestamp": "2026-09-10T12:00:00Z", "type": "event_msg",
                "payload": {"type": "token_count", "info": {
                    "total_token_usage": {"input_tokens": inp, "cached_input_tokens": cached,
                                          "cache_write_input_tokens": 0, "output_tokens": out,
                                          "reasoning_output_tokens": reason, "total_tokens": tot}}}})
        cum = os.path.join(cx, "rollout-cum.jsonl")
        with open(cum, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "session_meta",
                                "payload": {"cwd": "/x/myproj"}}) + "\n")
            f.write(_tc(100, 60, 10, 3, 110) + "\n")
            f.write(_tc(250, 150, 25, 8, 275) + "\n")     # 누적이므로 이 값만 유효
        a1, proj, days, _ = read_codex(cum)
        # 250 중 150 이 캐시 재사용 -> 새로 처리한 입력은 100. Claude 와 같은 뜻이 된다.
        assert (a1["input"], a1["output"]) == (100, 25), f"누적·캐시 처리 실패: {dict(a1)}"
        assert (a1["cache_read"], a1["thinking"]) == (150, 8), dict(a1)
        assert a1["calls"] == 2 and proj == "myproj" and dict(days) == {"2026-09-10": 125}, dict(days)

        # 캐시가 input 을 거의 다 차지해도 EXP 가 부풀지 않는다 (실제 로그의 97% 재사용 재현)
        hot = os.path.join(cx, "rollout-hot.jsonl")
        with open(hot, "w", encoding="utf-8") as f:
            f.write(_tc(7_573_626, 7_372_032, 19_645, 4_538, 7_679_071) + "\n")
        ah, _, _, _ = read_codex(hot)
        assert ah["input"] == 201_594, dict(ah)          # 7.57M 이 아니라 0.20M
        assert ah["cache_read"] == 7_372_032, dict(ah)   # 캐시는 DEF 로만 간다

        # 세부 항목이 비고 total 만 있는 세션 (실제 로그에 존재)
        deg = os.path.join(cx, "rollout-deg.jsonl")
        with open(deg, "w", encoding="utf-8") as f:
            f.write(_tc(0, 0, 0, 0, 17647) + "\n")
        a2, _, _, _ = read_codex(deg)
        assert a2["input"] == 17647 and a2["output"] == 0, dict(a2)

        # 토큰이 전혀 없는 세션은 통계에서 빠진다
        zero = os.path.join(cx, "rollout-zero.jsonl")
        with open(zero, "w", encoding="utf-8") as f:
            f.write(_tc(0, 0, 0, 0, 0) + "\n")
        a3, _, _, _ = read_codex(zero)
        assert not a3, dict(a3)

        # 추가 스캔 폴더가 실제로 반영되는지 (와일드카드 포함)
        cfg = {"providers": {"codex": {"extra": [os.path.join(d, "20*", "*", "*")]}}}
        roots = provider_roots("codex", cfg)
        assert cx in roots, f"추가 스캔 폴더 미반영: {roots}"
        agg, projects, _, _ = collect(pid="codex", roots=[cx])
        assert agg["input"] == 100 + 17647 + 201_594 and projects["codex\tmyproj"] == 125, (dict(agg), dict(projects))
        assert agg["sessions"] == 3, agg["sessions"]      # 빈 세션은 세지 않는다

        # build/save_snapshot 이 기본 인자 탓에 한 프로바이더만 읽는 회귀를 막는다
        import inspect
        for fn in (save_snapshot, build):
            assert inspect.signature(fn).parameters["root"].default is None, \
                f"{fn.__name__}(root=...) 기본값이 None 이 아니면 프로바이더 하나만 읽는다"

        # Gemini: 답변마다 따로 기록 -> 전부 더하되 같은 id 는 한 번만
        gt = os.path.join(d, "gtmp")
        gs = os.path.join(gt, "someproj", "chats")
        os.makedirs(gs)
        with open(os.path.join(gt, "someproj", ".project_root"), "w", encoding="utf-8") as f:
            f.write("/x/gproj\n")
        def _gm(mid, inp, out, cached, thoughts, tool):
            return json.dumps({"id": mid, "timestamp": "2026-09-10T08:44:00Z", "type": "gemini",
                "model": "gemini-3.5-flash", "tokens": {"input": inp, "output": out,
                "cached": cached, "thoughts": thoughts, "tool": tool,
                "total": inp + out + thoughts}})
        with open(os.path.join(gs, "session-a.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"sessionId": "s", "kind": "main"}) + "\n")
            f.write(json.dumps({"id": "u1", "type": "user", "content": "hi"}) + "\n")
            f.write(_gm("g1", 100, 10, 40, 5, 2) + "\n")
            f.write(_gm("g1", 100, 10, 40, 5, 2) + "\n")   # 같은 id 재기록 -> 한 번만
            f.write(_gm("g2", 50, 4, 0, 0, 0) + "\n")
        ag, gproj, gdays, _ = read_gemini(os.path.join(gs, "session-a.jsonl"))
        assert (ag["input"], ag["output"]) == (62 + 50, 15 + 4), f"Gemini 합산 실패: {dict(ag)}"
        assert (ag["cache_read"], ag["thinking"], ag["calls"]) == (40, 5, 2), dict(ag)
        assert gproj == "gproj" and dict(gdays) == {"2026-09-10": 131}, (gproj, dict(gdays))
        agg, projects, _, _ = collect(pid="gemini", roots=[gt])
        assert projects["gemini\tgproj"] == 131 and agg["sessions"] == 1, (dict(agg), dict(projects))

        # 끈 프로바이더는 합산에서 빠진다
        off = {"providers": {p: {"enabled": False} for p in PROVIDERS}}
        a3, _, _, _, per = collect_all(off)
        assert not a3.get("calls") and per == {}, (dict(a3), per)

    # 밸런스: 환생 설계가 성립하는지 검증한다. 임시 폴더에는 스냅샷이 없어 진짜 폴더 것을 쓴다.
    agg, projects, _, _ = _demo.real
    if not projects:
        print("ok (로컬 데이터 없음 — 밸런스 검증 생략)"); return
    h, ds = hero(agg), dungeons()
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
    # 난이도는 STEP^(전역 스테이지)로 연속이고, '층'은 고정 보스 수만큼 묶은 표시
    # 단위일 뿐이다. 캐릭터가 토큰 따라 계속 변하므로 절대 횟수를 못박지 않고,
    # (a) 다음 층은 환생을 요구한다 (b) 그다음은 눈에 띄게 더 요구한다 두 가지만 본다.
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
        # 미션도 보조 수입이어야 한다. 한 주치 미션 vs 한 주치 환생(하루 2회 = 토큰 200만/일).
        # 1.7 = 연속 7일 최대 배율(streakMul). 미션을 쪼개도 예산이 고정이라 여기가 안 움직인다.
        # 혼 사냥은 연속 배율(streakMul)을 안 받고, 대신 정확도 최대 MINI_BEST 배까지 간다
        wk_mission = ((DAY_BUDGET * 7 + WEEK_BUDGET) * 1.7
                      + MINI_BUDGET * MINI_BEST * 7) * soul_of(last)
        wk_rebirth = 14 * rebirth
        assert wk_mission < wk_rebirth * 2, (
            f"{last//n}층 미션 주간 수입 {wk_mission:.0f}이 환생 {wk_rebirth}의 2배 이상 "
            f"— DAY_BUDGET·WEEK_BUDGET 하향 필요")

    # 3-1) 유물 등급표는 합이 100% 여야 한다 — 어긋나면 마지막 등급이 나머지를 통째로 먹는다
    import re
    odds = [float(x) for x in re.findall(r'"[^"]+", ([\d.]+),', 
                  re.search(r"const RARITY = \[(.+?)\];", TEMPLATE, re.S).group(1))]
    assert len(odds) >= 4 and abs(sum(odds) - 100) < 1e-9, f"유물 등급 확률 합이 {sum(odds)}% 다: {odds}"
    # 재격파 파밍으로 버는 혼(중복 환산)이 늘면 환생 기운(토큰) 제한을 우회한다.
    # 확률을 올리려면 환산 나누기도 같이 올려야 한다 — 곱이 옛 값(0.5% x 1/10)을 넘으면 안 된다.
    again = float(re.search(r"RELIC_AGAIN = ([\d.]+)", TEMPLATE).group(1))
    dup = float(re.search(r"RELIC_DUP = ([\d.]+)", TEMPLATE).group(1))
    assert again / dup <= 0.005 / 10 + 1e-12, \
        f"재격파 파밍 혼 수입이 늘었다 ({again}/{dup}) — RELIC_DUP 를 같이 올려라"

    # 3-2) 파괴의 유산이 혼을 깊이로 바꾸는가. 가산이던 시절엔 어떤 레벨을 사도 도달이
    #      그대로여서, 사면 손해인 특성이었다. 절대 스테이지는 사용자마다 다르니
    #      (a) 사면 더 깊이 간다 (b) 힘의 유산과 같은 줄에 선다 두 가지만 본다.
    base_r = reach(h)
    atk20, cdmg20 = reach(h, {"atk": 20}), reach(h, {"cdmg": 20})
    assert cdmg20 > base_r, \
        f"파괴의 유산 20레벨이 도달 스테이지를 못 늘린다 ({base_r} -> {cdmg20}) — 혼을 버리는 특성이다"
    assert cdmg20 >= atk20 - 3, \
        f"파괴({cdmg20})가 힘({atk20})보다 세 칸 넘게 뒤진다 — 선택이 아니라 들러리다"
    assert cdmg20 <= atk20, \
        f"파괴({cdmg20})가 힘({atk20})을 넘어섰다 — 치명타 빌드만 정답이 된다"

    # 4) 특성 비용은 반드시 증가한다 (무한 구매 방지)
    assert trait_cost(0) < trait_cost(5) < trait_cost(20), "특성 비용이 증가하지 않는다"

    # 5) 설치 시점 기준 — 쌓여 있던 로그로 레벨이 치솟지 않아야 하고,
    #    쓰던 설치는 과거를 잃지 않아야 한다 (둘 다 틀리면 캐릭터가 통째로 바뀐다)
    keep_env = dict(os.environ)
    with tempfile.TemporaryDirectory() as t:
        try:
            os.environ["TOKEN_RPG_HOME"] = t
            os.environ.pop("TOKEN_RPG_SNAPSHOTS", None)
            assert since_ts({}) > 0, "새 설치인데 전체 기록을 센다"
            os.remove(config_path())
            with open(os.path.join(snap_dir(), "pc.json"), "w", encoding="utf-8") as f:
                f.write("{}")
            assert since_ts({}) == 0, "쓰던 설치인데 과거를 잘라낸다"

            logs = os.path.join(t, "logs")
            os.makedirs(os.path.join(logs, "proj"))
            for name in ("old.jsonl", "new.jsonl"):
                with open(os.path.join(logs, "proj", name), "w", encoding="utf-8") as f:
                    f.write(json.dumps(rec) + "\n")
            os.utime(os.path.join(logs, "proj", "old.jsonl"), (time.time() - 86400 * 30,) * 2)
            a_all, _, _, _ = collect(roots=[logs])
            a_cut, _, _, _ = collect(roots=[logs], since=time.time() - 86400)
            assert a_all["calls"] == 2 and a_cut["calls"] == 1, (dict(a_all), dict(a_cut))
        finally:
            os.environ.clear(); os.environ.update(keep_env)

    # 6) 값이 그대로면 game.html 을 다시 쓰지 않는다. mtime 이 바뀌면 메뉴 막대 앱이
    #    페이지를 리로드하고, 그러면 화면 상태(자동 도전 승/패)가 0 으로 돌아간다.
    keep_env = dict(os.environ)
    with tempfile.TemporaryDirectory() as t:
        try:
            os.environ["TOKEN_RPG_HOME"] = t
            os.environ.pop("TOKEN_RPG_SNAPSHOTS", None)
            os.makedirs(os.path.join(t, "proj"))
            with open(os.path.join(t, "proj", "s.jsonl"), "w", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            out, _ = build(root=t)
            m1 = os.path.getmtime(out)
            build(root=t)
            assert os.path.getmtime(out) == m1, "사용량이 그대로인데 game.html 을 다시 썼다"
            with open(os.path.join(t, "proj", "s2.jsonl"), "w", encoding="utf-8") as f:
                f.write(json.dumps({**rec, "message": {**rec["message"], "id": "b"}}) + "\n")
            build(root=t)
            assert os.path.getmtime(out) != m1, "사용량이 늘었는데 game.html 이 그대로다"
        finally:
            os.environ.clear(); os.environ.update(keep_env)

    # 7) 윈도우 분기 — 맥·리눅스에서도 os.name 만 바꿔 끼워 검증한다
    import tempfile
    real_name, env = os.name, dict(os.environ)
    try:
        os.name = "nt"
        with tempfile.TemporaryDirectory() as t:
            os.environ.pop("XDG_DATA_HOME", None); os.environ.pop("TOKEN_RPG_HOME", None)
            os.environ["LOCALAPPDATA"] = t
            assert data_dir() == os.path.join(t, "token-rpg"), data_dir()
        cmd = hook_command()
        assert "/dev/null" not in cmd and "true" not in cmd and ">NUL" in cmd, cmd
        assert HOOK_MARK in cmd, cmd          # 재설치·제거가 이 표식으로 훅을 찾는다
    finally:
        os.name = real_name
        os.environ.clear(); os.environ.update(env)
    assert "/dev/null" in hook_command()      # 원래 플랫폼 분기로 되돌아왔다

    print("ok")


if __name__ == "__main__":
    sys.exit(main())
