#!/usr/bin/env python3
"""Claude Code 토큰 사용량으로 굴러가는 턴제 RPG.

설계: 내 스탯은 토큰에 선형으로, 보스 능력치는 토큰의 거듭제곱근(<1)으로 커진다.
따라서 막힌 스테이지는 토큰을 더 쓰면 반드시 넘을 수 있다.
"""
import argparse, base64, collections, glob, hashlib, hmac, json, math, os, shutil, socket, subprocess, sys, webbrowser
import http.server, threading, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

__version__ = "0.19.8"

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

# --- 밸런스 조절 손잡이 (token-rpg balance 로 확인) ---
# 보스 능력치는 '전역 스테이지 번호'의 지수 곡선이다. 층 경계에서 난이도가
# 끊기지 않고, 한 층(=고정 보스 15종)을 돌 때마다 STEP^N 배씩 벽이 높아진다.
STEP = 1.28                              # 스테이지 1칸당 보스 배율
B_HP, B_ATK, B_DEF = 115, 24, 8          # 1스테이지 보스 기준치
# 극초반 벽 낮추기: 토큰이 0 인 Lv.1 은 ATK 도 0 이라 DEF 8 인 1스테이지 보스에게 데미지가
# 한 점도 안 들어갔다. 1스테이지를 이기는 데 누적 200만 토큰이 필요했고, 그전까지 게임이
# 시작조차 안 됐다. EARLY_G 까지는 배율을 눌러 두고 거기서 원래 곡선에 합류시킨다.
EARLY_G   = 6                            # 여기부터는 원래 곡선 그대로
EARLY_MUL = 0.12                         # 1스테이지 배율 (EARLY_G 에서 1.0 으로 복귀)
# v0.19.8: EARLY_G 부터 보스 ATK 는 x1.24, HP 는 x1.325 씩 오른다(DEF 는 STEP 그대로).
# 셋이 다 x1.28 이던 때는 6층부터 보스 한 대에 누구나 죽어, 먼저 쳐서 한 턴에 잡느냐로만 갈렸다.
# HP·DEF 배분과 생존·벽의 유산이 아무 일도 안 했다. 보스 공격을 늦추고 HP 를 키우니 싸움이 길어지고
# 버티는 빌드가 화력 몰빵과 나란히 선다. 값은 테스트 캐릭터 다섯으로 계산해 TypeSafe 판정과 함께 골랐다
# (특성 배율 조정과 함께 진행은 캐릭터 중앙값 -14~+10%, 벽의 유산 +20 이 힘의 유산 +20 과 같은 칸을 민다). `.studio/bal_grid.py`
B_ATK_STEP, B_HP_STEP = 1.24, 1.325
PT_PER_LEVEL = 2                         # 레벨업당 자유 배분 포인트
# 포인트 1점당 상승치 (cdmg 는 %p). SPD 3 은 보스 SPD(스테이지당 +BOSS_SPD)와 같은 걸음이다 —
# 1점이 보스 한 스테이지분이다.
GAIN = {"atk": 1, "hp": 12, "dfn": 0.6, "crit": 0.4, "cdmg": 1, "spd": 3}
# 보스보다 1~2배 빠르면 그 비율만큼 확률로 2타, 2배부터 늘 2타 (BOSS_SPD 아래 주석).
# v0.19.1 까지는 천장이 없고 보스 SPD 가 STEP 배로 올라, 깊은 층에서 아무도 SPD 에 배분하지 않았다.
CRIT_CAP  = 100      # 치명타율 상한(%). 넘친 %p 는 치명타 피해 %p 로 옮겨 간다 — 올려도 헛되지 않게
CDMG_BASE = 200      # 치명타 피해 기본값(%) = 예전 고정 2배
# '파괴의 유산'은 치명타 피해에 TRAIT_MUL 을 곱한다. 전에는 레벨당 +5%p 가산이었는데,
# 보스가 스테이지마다 x1.28 로 곱해지는 곡선이라 가산은 따라가지 못했다 — 혼 23,455 를
# 부어도 도달 스테이지가 9 에서 9 로 그대로였다(같은 혼이면 힘의 유산이 16). 곱으로 바꾸면
# 힘의 유산보다 한 칸 뒤에 서고, CRIT 이 높을수록 좁혀진다 = 빌드가 고르는 선택이 된다.
# demo() 의 _check_cdmg 가 이 성질을 붙들고 있다.

# 환생: 클리어 기록과 배분을 버리고 '혼'을 얻어 영구 특성을 산다.
# 특성 효과는 배율(지수), 비용도 지수 -> 층 벽을 넘으려면 환생을 거듭해야 한다.
TRAIT_MUL  = 1.135   # 특성 1레벨당 스탯 배율 (기본값). v0.19.8 에서 1.12 -> 1.135 (아래 TRAIT_MULS 참고)
# 배율은 그 스탯이 '무엇과 겨루는지'에 맞춘다. 전부 x1.12 이던 시절엔 벽과 신속이 죽어 있었다 —
# 혼을 얼마를 부어도 도달 스테이지가 그대로라, 사면 손해인 특성이었다.
#   힘·파괴  피해에 곱으로 붙는다. x1.12 로 충분하다.
#   벽       보스 ATK 와 1:1 로 맞선다. 보스 ATK 는 스테이지마다 STEP 이므로 벽도 STEP —
#            벽 1레벨이 보스 한 스테이지분이다. x1.12 면 60레벨을 사도 도달이 15 에서 멈췄다.
#   신속     배율이 아니라 덧셈(SPD_TRAIT). 2타 천장이라 힘과 나란히 서지 않는다 — 싸게 배분 점수를 아껴 준다.
#   혼(HP)   피해를 견디는 쪽. 벽과 함께 크므로 그 사이 값이면 된다.
# 다섯이 도달 스테이지에서 서로 두 칸 안에 서고 힘을 넘지 않는 값이다 (demo() 가 붙든다).
# v0.19.8: 보스 ATK 가 x1.24 로 늦춰지며 벽(x1.28)이 힘의 두 배 칸을 밀었다. 사용자 조건은 '특성이 한쪽으로
# 지나치게 기울면 안 된다'. 벽 1.25 · 힘/파괴 1.135 에서 +20레벨 효과가 공격·방어 같아진다(비율 중앙 1.0).
# 후보는 코드로 계산하고 TypeSafe 판정으로 골랐다(`.studio/trait_tune.py`, `trait_ts.py`).
TRAIT_MULS = {"dfn": 1.25, "hp": 1.16}
# 속도 (v0.19.2): 보스 SPD 는 스테이지마다 BOSS_SPD 씩만 오른다(다른 능력치는 STEP 배). 그래서 배분으로 따라잡을 수 있다.
# 보스보다 1~2배 빠르면 그 비율만큼 확률로 2타, 2배부터 늘 2타 — 3타는 없다(HITS_MAX).
# 신속의 유산은 레벨당 SPD +SPD_TRAIT(덧셈). 곱셈(1.2/lv)이면 2타가 금방 공짜가 된다.
# 값은 .studio/spd_linear.py · spd_linear_typesafe.py 로 골랐다 (k=3: 초반·후반 모두 배분에 속도가 들어가고 층 진입은 3회 안에서 움직인다).
BOSS_SPD, HITS_MAX, SPD_TRAIT = 3, 2, 0.05
tmul_of = lambda k: TRAIT_MULS.get(k, TRAIT_MUL)
COST_MUL   = 1.13    # 특성 1레벨당 비용 증가율
COST_K     = 80      # 특성 1레벨 기본 비용
SOUL_EXP   = 2.4     # 혼 획득 = 스테이지번호^SOUL_EXP (SOUL_LATE_G 까지)
# 후반 혼 배율: SOUL_LATE_G 를 넘은 스테이지마다 혼이 SOUL_LATE_MUL 배씩 는다 (4층부터).
# 특성 비용은 스테이지마다 약 x1.3 씩 지수로 오르는데 혼은 번호의 2.4제곱(다항식)이라,
# 5층부터 층마다 환생이 두 배씩 들었다 — 층당 7 · 16 · 37 · 75회 (하루 2회면 8층에 37일).
# 혼도 지수로 따라가게 하면 10층까지 층당 5~8회로 고르게 가고 그 뒤로 천천히 느려진다.
# 1.10 은 잰 값이다: 1.08 이면 10층이 22회로 벽이 일찍 다시 서고, 1.12 부터는 뒤로 갈수록
# 빨라져 100회 안에 끝없이 오른다(폭주). demo() 가 '느려지지도 폭주하지도 않는다' 를 붙든다.
# 1~3층(45스테이지까지)은 그대로다 — 거기는 이미 조율해 둔 구간이다.
SOUL_LATE_G   = 45
# v0.18.1 에서 1.10 을 1.09 로 낮췄다. 보조 수입을 후반에도 살린 뒤로(v0.16.0) 실제 속도가 환생 한 번에
# 3~5스테이지였다 — 처음 고른 목표(2~3)보다 빨랐다. 실제 저장(환생 26회, 90스테이지)에서 앞으로 10회를
# 재면 1.10 이 환생당 4.2, 1.095 가 3.5, 1.09 가 3.0, 1.08 이 2.3 이다. 사용자가 1.09 를 골랐다.
# 대가는 뒤쪽 층이다 — 환생 혼만 센 모델에서 9층이 14~15회가 된다(1.10 은 10회). 그래서 속도 검사의
# 층당 상한을 12회에서 16회로 올렸다. 1.08 은 8층이 16~17회이고 80회 안에 9층에 못 닿는다 — 벽이 다시 선다.
SOUL_LATE_MUL = 1.09
TRAIT_PT   = 4       # '각성' 1레벨당 배분 포인트
# '수확' 1레벨당 혼 획득 +%p. 더 올리면 깊은 층이 일찍 열린다 — simulate() 로 잰 값(v0.14.11,
# 개발자 영웅 Lv.31): 3 이면 5층 19회 · 8층 40회, 6 이면 16회 · 31회, 10 이면 14회 · 25회.
# 3층까지는 어느 값이든 7~8회로 같다. 처음 고를 때의 숫자(3층 23회)는 옛 상수에서 잰 것이다.
TRAIT_SOUL = 3
# '신속' 은 SPD_TRAIT 을 쓴다(위 참고). turns_to_win 이 2타 확률을 재므로 reach() 도 SPD 배분을 고른다.
# demo() 가 '신속 도달 <= 힘 도달' 을 검사한다.
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

# 슬롯: 실력이 없는 칸이라 하루 판수만 정해 주고 나머지는 운에 맡긴다. 단위는 위와 같다.
# SLOT_AVG 는 SLOT_SYM 문양표에서 나오는 한 판 평균 배당이고, 지급을 이 값으로 나눠 둔다 —
# 그래야 표를 어떻게 흔들어도 하루 기대 수입이 SLOT_BUDGET 에 붙박이고 변동성만 남는다.
# (selftest 가 문양표에서 다시 계산해 이 값을 붙들고 있다)
# 예산은 4 격파분이다. 혼 사냥(하루 2 격파분 x 한가운데 1.5배 = 3)을 v0.18.5 에 없애며 그 몫을 여기로
# 옮겼다 — 보조 수입 총량은 그대로라 selftest 의 1층 상한(주간 미션 수입 < 환생 x2)도 그대로 지킨다.
SLOT_TRIES, SLOT_BUDGET, SLOT_AVG = 10, 4, 2.42
# 하루 몫을 다 쓴 뒤에도 돌리는 것 자체는 막지 않는다(연습판). 혼만 안 들어간다 —
# 못 누르게 막아 두면 하루 열 판이 끝인 칸이 되고, 돌리는 재미가 예산과 묶일 이유는 없다.

# 희귀 개체와 보스 도감 (v0.16.0): 싸울 때마다 RARE_RATE 확률로 그 보스의 '황금' 개체가 대신 나온다.
# 능력치는 같다. 잡으면 도감에 찍히고, 처음 잡을 때만 그 스테이지 혼의 RARE_MUL 배를 준다.
#
# 확률이 0.1% 인 이유: 자동 반복은 1초에 한 판이다. 계획 초안의 2% 면 50초에 한 번이라 한 층의
# 15종이 십여 분에 다 찬다 — 희귀가 아니다. 0.1% 면 켜 둔 채 17분에 한 번이다. 한 층 15종은
# 한 칸씩 골라 가며 잡으면 네 시간쯤, 15칸을 한꺼번에 돌리면 열네 시간쯤 든다.
# 다시 잡은 황금은 혼을 주지 않는다. 주면 켜 두기만 해도 혼이 쌓여 환생 기운(토큰) 제한을 돈다 —
# 유물 중복 환산(RELIC_AGAIN / RELIC_DUP)을 묶어 둔 것과 같은 이유다. 혼 보상은 칸마다 한 번이다.
#
# 배수가 1 인 이유: 처음엔 계획 초안대로 5 를 넣었다. 재 보니 처음 한 번만 줘도 5층·6층이 환생
# 2~3회에 열렸다(기준은 4~12회). 한 층에 황금이 15칸이라 환생 한 번 사이에 환생 다섯 번치 혼이
# 들어왔다. 1 이면 5~9층이 층당 5~7회로 기준 안이고, 환생 사이에 들어오는 황금 혼이 환생 한 번치를
# 넘지 않는다. demo() 의 _check_dex_pace 가 '하루 종일 켜 두고 값이 큰 칸부터 잡는' 경우로 붙든다.
RARE_RATE = 0.001
RARE_MUL  = 1
# 도감 보너스(영구): 황금 한 칸마다 얻는 혼 +DEX_SOUL %, 한 층의 황금 15종을 다 채우면 배분 +DEX_PT pt.
# 다 모으면 혼 +37.5% · 배분 +20pt. 수확 12레벨, 각성 5레벨어치다 — 혼이 아니라 시간으로 사는 성장이다.
DEX_SOUL = 0.5
DEX_PT   = 4

# 업적 (v0.19.0): 달성하면 칭호 하나와 얻는 혼 +ACH_SOUL % (영구). 전부 모아도 +10% 안이다 —
# 그 이상은 층 진입이 15% 넘게 당겨져 TypeSafe 에서 떨어졌다 (.studio/ach_balance.py).
# (id, 분류, 무엇을 세나, 목표, 칭호, 설명, 숨김)
ACH_SOUL = 0.3
ACHS = [
    ("floor3",  "모험", "floor", 3,      "던전 탐험가",       "3층 도달", False),
    ("floor6",  "모험", "floor", 6,      "변종 사냥꾼",       "6층 도달", False),
    ("floor10", "모험", "floor", 10,     "심연을 걷는 자",    "10층 도달", False),
    ("reb10",   "모험", "reb",   10,     "다시 태어난 자",    "환생 10번", False),
    ("reb50",   "모험", "reb",   50,     "윤회의 여행자",     "환생 50번", False),
    ("reb200",  "모험", "reb",   200,    "영원의 순례자",     "환생 200번", False),
    ("trans1",  "모험", "trans", 1,      "한계를 넘은 자",    "초월 1번", False),
    ("kill1k",  "전투", "kill",  1000,   "견습 전사",         "보스 1,000마리 처치", False),
    ("kill10k", "전투", "kill",  10000,  "베테랑",            "보스 10,000마리 처치", False),
    ("kill100k","전투", "kill",  100000, "전장의 전설",       "보스 100,000마리 처치", False),
    ("gate1",   "전투", "gate",  1,      "관문 파괴자",       "변이 관문 1개 돌파", False),
    ("gate5",   "전투", "gate",  5,      "변이를 이긴 자",    "변이 관문 5개 돌파", False),
    ("gate10",  "전투", "gate",  10,     "불굴의 정복자",     "변이 관문 10개 돌파", False),
    ("dex15",   "수집", "dex",   15,     "초보 수집가",       "보스 도감 15종", False),
    ("dex45",   "수집", "dex",   45,     "열성 수집가",       "보스 도감 45종", False),
    ("dex75",   "수집", "dex",   75,     "도감의 주인",       "보스 도감 75종", False),
    ("gold1",   "수집", "gold",  1,      "황금을 본 자",      "황금 개체 1마리", False),
    ("gold15",  "수집", "gold",  15,     "황금 수집가",       "황금 개체 15마리", False),
    ("gold75",  "수집", "gold",  75,     "황금 사냥꾼",       "황금 개체 75마리", False),
    ("relic5",  "수집", "relic", 5,      "유물 수집가",       "유물 5칸", False),
    ("relic15", "수집", "relic", 15,     "유물 감정사",       "유물 15칸", False),
    ("myth1",   "수집", "myth",  1,      "신화의 주인",       "신화 유물 1개", False),
    ("streak3", "꾸준함", "streak", 3,   "꾸준한 모험가",     "3일 연속 사용", False),
    ("streak7", "꾸준함", "streak", 7,   "성실한 모험가",     "7일 연속 사용", False),
    ("streak30","꾸준함", "streak", 30,  "한결같은 모험가",   "30일 연속 사용", False),
    ("raid1",   "꾸준함", "raid",  1,    "거인 사냥꾼",       "레이드 격파 1번", False),
    ("raid4",   "꾸준함", "raidrun", 4,  "주간 정복자",       "레이드 4주 연속 격파", False),
    ("raid12",  "꾸준함", "raid",  12,   "레이드의 왕",       "레이드 격파 12번", False),
    ("jackpot", "행운", "jackpot", 1,    "행운의 여신이 본 자", "슬롯 💎 세 줄", False),
    ("gold2",   "행운", "gold2", 1,      "겹경사",            "하루에 황금 개체 둘", False),
    ("night",   "숨은 업적", "night", 1, "잠들지 않는 자",    "새벽 3~5시에 보스 처치", True),
    ("lose100", "숨은 업적", "lose", 100, "넘어져도 일어서는 자", "패배 100번", True),
]

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
#
# 층 테마 (v0.15.0): 층마다 보스 15종의 이름과 이모지가 다르다. 예전엔 2층부터 같은 15종이
# 능력치만 커져서 다시 나왔다 — 5층이면 같은 보스를 다섯 번째 보는 셈이었다.
# 바뀌는 것은 이름과 이모지뿐이다. 능력치(boss)·혼 배수·유물 능력치는 칸 번호를 따르므로
# 밸런스와 저장 형식은 그대로다. 테마가 끝나면 처음으로 돌아가 이름 앞에 '변종'을 붙인다.
AFFIXES = ["atk", "crit", "dfn", "hp", "soul", "hp", "crit", "atk", "dfn", "crit", "soul", "atk", "dfn", "hp", "soul"]
THEMES = [
    ("코드 버그", [
        ("버그 벌레", "🐛"), ("무한 루프 뱀", "🐍"), ("널 포인터 박쥐", "🦇"), ("레거시 전갈", "🦂"),
        ("좀비 프로세스", "🧟"), ("메모리 누수 슬라임", "🦠"), ("머지 충돌 도깨비", "👹"),
        ("스파게티 크라켄", "🐙"), ("데드락 골렘", "🗿"), ("레이스 컨디션 유령", "👻"),
        ("의존성 지옥 악마", "😈"), ("스택 오버플로 히드라", "🐉"), ("기술 부채 리치", "💀"),
        ("프로덕션 장애 드래곤", "🐲"), ("토큰 한도의 군주", "👑")]),
    ("인프라 장애", [
        ("타임아웃 거미", "🕷️"), ("재시도 폭풍", "🌪️"), ("DNS 망령", "👻"), ("디스크 포화 두꺼비", "🐸"),
        ("고아 컨테이너", "📦"), ("로그 홍수 해파리", "🪼"), ("인증서 만료 사신", "☠️"),
        ("OOM 킬러", "🪓"), ("방화벽 석상", "🧱"), ("캐시 미스 도깨비불", "🔥"),
        ("요금 폭탄 악마", "😈"), ("롤백 실패 켄타우로스", "🐎"), ("네트워크 분단 거인", "🗿"),
        ("리전 다운 레비아탄", "🐋"), ("온콜의 군주", "👑")]),
    ("AI", [
        ("오타 프롬프트 임프", "👺"), ("무한 재생성 앵무새", "🦜"), ("거절 응답 달팽이", "🐌"),
        ("컨텍스트 오버플로 거인", "🦣"), ("버려진 에이전트", "🤖"), ("과적합 슬라임", "🦠"),
        ("환각 키메라", "🦄"), ("도구 호출 폭주 문어", "🐙"), ("가드레일 골렘", "🗿"),
        ("온도 2.0 광대", "🤡"), ("토큰 흡혈귀", "🧛"), ("프롬프트 인젝션 사기꾼", "🎭"),
        ("레이트 리밋 문지기", "🚧"), ("모델 붕괴 드래곤", "🐲"), ("정렬 실패의 군주", "👑")]),
    ("회사", [
        ("급한 건 모기", "🦟"), ("핑퐁 메일 박쥐", "🦇"), ("문서 없음 안개", "🌫️"), ("야근 좀비", "🧟"),
        ("퇴사자의 코드", "🪦"), ("범위 팽창 슬라임", "🫧"), ("기획 변경 폭풍", "⛈️"),
        ("긴급 핫픽스 전갈", "🦂"), ("결재선 미궁", "🏛️"), ("금요일 배포 유령", "👻"),
        ("예산 삭감 악마", "😈"), ("무한 회의 히드라", "🐉"), ("레거시 수호자 리치", "💀"),
        ("마감 전야 드래곤", "🐲"), ("분기 실적의 군주", "👑")]),
    ("보안", [
        ("피싱 낚시꾼", "🎣"), ("무차별 대입 딱따구리", "🐦"), ("평문 비밀번호 쥐", "🐀"),
        ("패치 안 된 서버", "🏚️"), ("유출된 키", "🗝️"), ("버퍼 넘침 슬라임", "🦠"),
        ("SQL 주입 뱀", "🐍"), ("XSS 거미", "🕷️"), ("권한 상승 기사", "♞"),
        ("사이드 채널 유령", "👻"), ("랜섬웨어 악마", "😈"), ("공급망 오염 히드라", "🐉"),
        ("봇넷 군단", "🐜"), ("제로데이 드래곤", "🐲"), ("루트 권한의 군주", "👑")]),
]
# 주간 레이드 (v0.17.0): 탑과 별개로 매주 월요일에 보스 하나가 선다. HP 는 이번 주에 실제로 쓴 토큰으로만
# 깎인다 — 스탯과 전투는 관계없다. 탑이 막힌 주에도 할 일이 남는다. 25·50·75·100% 를 깎을 때마다 보상을 준다.
#
# RAID_BUDGET: 네 단계가 나눠 갖는 한 주 예산(단위는 미션과 같다 — 최고 스테이지 보스 몇 판치, 보조 수입용 혼).
#   연속 사용 배율(streakMul)은 안 붙인다 — 레이드가 이미 한 주의 사용량을 잰다.
# RAID_MIN_BEST: 2층 첫 보스를 깨야 열린다. 1층에서는 주간 보조 수입이 이미 상한(환생의 2배)에 닿아 있어
#   레이드 몫을 얹을 자리가 없다. 16스테이지부터는 상한 안이다 (demo() 가 16스테이지부터 전부 본다).
# RAID_MIN_HP: 갓 깔았거나 쉬던 주에도 보스가 서도록 두는 HP 하한.
# 격파 보상은 가진 유물 하나를 한 등급 올리는 것이다(v0.17.1). 등급을 새로 굴리던 v0.17.0 은 열에 여섯이
# 일반이라 유물을 모은 뒤로는 빈손이었다. 천장(신화)은 그대로고, 15칸 전부 신화까지 많아야 60주다.
# 자동 반복으로도 신화는 나온다(한 보스에 7시간쯤) — 레이드는 켜 두지 않는 사람의 길이다.
# 보스 HP 는 지난 4주의 주간 사용량 평균이다 (쓴 주만 센다) — 평소대로 쓰면 주말쯤 잡는다.
RAID_BUDGET   = 8
RAID_MIN_BEST = len(AFFIXES) + 1
RAID_MIN_HP   = 1_000_000
RAID_BOSSES = [("월요일의 거신", "🗿"), ("분기 마감의 용", "🐲"), ("전사 장애의 크라켄", "🦑"),
               ("대이동의 골렘", "🏗️"), ("감사 시즌의 스핑크스", "🦁"), ("연말 정산의 불사조", "🦅")]
# 변이와 변이 조각 (v0.18.0): 변종 층(6층~)의 보스는 층마다 특수한 능력(변이) 하나를 갖는다.
# 반복 전투로 변이 조각을 모아 그 변이의 내성을 올리면 능력이 약해지고, 다 올리면 사라진다.
# (id, 이름, 이모지, 세기, 설명틀) — 6층부터 차례로 붙고 다섯 층마다 되풀이한다.
# 세기는 능력치 하나에 곱하는 배수다. 되풀이할 때마다(11층~, 16층~) 한 번씩 더 곱한다.
MUTS = [
    # v0.19.8: 세기를 3 -> 5(저주 4 -> 8)로 올렸다. 보스 HP 가 스테이지마다 더 가파르게 오르며 같은 배수가
    # 덜 깊은 칸에 해당하게 돼, 그대로면 한 겹이 1~2스테이지밖에 안 막았다. 지금 값이 2~5스테이지다.
    ("armor",  "철갑",   "🛡️", 5.0,  "내가 주는 피해 ÷{0}"),
    ("curse",  "저주",   "☠️", 8.0,  "내 SPD ÷{0}"),
    # v0.19.7: 거대화·실명은 예전 광폭(보스 ATK)·관통(내 DEF) 자리다. 6층부터는 서로 한 방이라 보스 공격을
    # 키우거나 내 방어를 깎아도 결과가 같았다(벽을 0스테이지 당겼다). id 는 그대로 둬 모은 내성을 잇는다.
    ("fury",   "거대화", "🦣", 5.0,  "보스 HP x{0}"),
    ("pierce", "실명",   "🕶️", 5.0,  "내 치명타 확률 ÷{0}"),
    ("numb",   "무감각", "🧊", 5.0,  "내 치명타 피해 ÷{0}"),
]
MUT_LV = 10          # 내성 10레벨이면 그 변이 한 겹이 사라진다. 되풀이 층(11층~)의 두 겹째는 11~20레벨이 지운다
SHARD_RATE = 0.02    # 변종 층의 보스를 반복 전투로 이길 때 조각이 나올 확률
SHARD_K, SHARD_STEP = 9, 1       # 내성 L -> L+1 의 값 = SHARD_K + SHARD_STEP * L. 층 보스의 관문이 생기며 낮췄다(30, 15).
                                  # 12+2 는 하루 15분 켜는 사람을 두 배 가까이 늦춰 TypeSafe 에서 떨어졌다 (.studio/gate_typesafe.py)


def shard_cost(lv):
    return SHARD_K + SHARD_STEP * lv


def mut_of(g):
    """g 스테이지의 변이 -> (id, 겹). 변종 층이 아니면 None. 겹은 6~10층이 1, 11~15층이 2다."""
    fl = (g - 1) // len(AFFIXES) - len(THEMES)
    return None if fl < 0 or not MUTS else (MUTS[fl % len(MUTS)][0], fl // len(MUTS) + 1)


def mut_power(g, res=None):
    """g 스테이지의 변이가 지금 내성으로 몇 배짜리인가 (1 이면 사라졌다). JS 의 mutPow() 와 같은 식.
    세기^(겹 - 내성/MUT_LV), 1 아래로는 안 내려간다 — 내성이 남아도 덤은 없다."""
    m = mut_of(g)
    if not m:
        return None, 1.0
    base = next(x[3] for x in MUTS if x[0] == m[0])
    return m[0], max(1.0, base ** (m[1] - (res or {}).get(m[0], 0) / MUT_LV))


def gated(g, res=None):
    """변종 층의 마지막 보스는 관문이다 — 그 층의 변이를 내성으로 다 지워야(겹마다 MUT_LV 레벨) 싸울 수 있다.
    JS 의 gated() 와 같은 식. reach() 같은 속도 모델은 관문을 세지 않는다: 관문은 환생이 아니라 반복 전투
    시간을 받는다 (그 시간은 _check_muts 가 붙든다)."""
    m = mut_of(g)
    return bool(m) and g % len(AFFIXES) == 0 and (res or {}).get(m[0], 0) < MUT_LV * m[1]


# 1층 보스 (이름, 이모지, 유물 능력치). 칸별 혼 배수·유물 능력치의 기준이라 그대로 둔다.
BOSSES = [(name, emoji, affix) for (name, emoji), affix in zip(THEMES[0][1], AFFIXES)]


def slot_of(g):
    """전역 스테이지 번호 -> 그 칸의 보스(이름·이모지·테마)와 칸 고정값(유물 능력치). JS 의 slotOf() 와 같은 식."""
    n = len(AFFIXES)
    i, fl = (g - 1) % n, (g - 1) // n
    theme, bosses = THEMES[fl % len(THEMES)]
    lap = fl // len(THEMES)                     # 테마를 몇 바퀴 돌았나
    pre = "" if lap == 0 else "변종 " if lap == 1 else f"변종{lap} "
    return {"slot": i + 1, "name": pre + bosses[i][0], "emoji": bosses[i][1],
            "affix": AFFIXES[i], "theme": pre + theme}


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


def ledger_path():
    return os.path.join(data_dir(), "ledger.json")


def collect(root=None, pid="claude-code", roots=None, since=0, ledger=None):
    """한 프로바이더의 로그를 훑어 합계를 낸다.
    since 이전에 끝난 세션 파일은 건너뛴다 — 세션 단위라 기준을 걸친 파일 하나는 통째로 센다.
    ledger({경로: {mt, sz, r}})를 넘기면 파일별 합계를 거기 적어 두고, 디스크에서 사라진
    파일도 적힌 값으로 센다 — Claude Code 는 30일 지난 로그를 지워서(cleanupPeriodDays)
    다시 훑기만 하면 누적이 30일 창으로 줄어든다."""
    spec = PROVIDERS[pid]
    agg = collections.Counter()
    days, projects, models = collections.Counter(), collections.Counter(), collections.Counter()  # days: 날짜 -> 토큰
    files = 0
    found = {}
    for r in ([root] if root else (roots if roots is not None else provider_roots(pid))):
        for path in glob.glob(os.path.join(r, spec["glob"]), recursive=True):
            try:
                st = os.stat(path)
            except OSError:
                continue
            e = (ledger or {}).get(pid + "\t" + path)
            if not (e and e["mt"] == st.st_mtime and e["sz"] == st.st_size):
                e = {"mt": st.st_mtime, "sz": st.st_size, "r": spec["read"](path)}
            found[path] = e
    if ledger is not None:
        prefix = pid + "\t"
        for path, e in ledger.items():          # 지워진 로그 — 마지막으로 본 값을 그대로 센다
            if path.startswith(prefix):
                found.setdefault(path[len(prefix):], e)
        for path, e in found.items():
            ledger[prefix + path] = e
    for path, e in found.items():
        if since and e["mt"] < since:
            continue
        a, proj, d, m = e["r"]
        if not a:
            continue
        a = collections.Counter(a)
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
    try:
        ledger = _load_sealed(ledger_path())
    except OSError:
        ledger = {}
    except ValueError:                          # 깨진 장부는 덮어쓰지 않는다 — 지워진 로그의 유일한 기록이다
        ledger = None
    for pid in PROVIDERS:
        if not cfg.get("providers", {}).get(pid, {}).get("enabled", True):
            continue
        roots = provider_roots(pid, cfg)
        if not roots:
            continue
        a, p, m, d = collect(pid=pid, roots=roots, since=since_ts(cfg), ledger=ledger)
        if not a.get("calls"):
            continue
        agg.update(a); projects.update(p); models.update(m)
        for day, v in d.items():
            days.setdefault(day, {})[pid] = v
        per[pid] = a["input"] + a["output"]
    if ledger is not None:
        _dump_sealed(ledger_path(), ledger)
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
    if g >= EARLY_G:
        p0 = STEP ** (EARLY_G - 1)
        return {"hp": round(B_HP * p0 * B_HP_STEP ** (g - EARLY_G)), "atk": round(B_ATK * p0 * B_ATK_STEP ** (g - EARLY_G)),
                "dfn": round(B_DEF * p), "spd": max(1, round(BOSS_SPD * g))}
    return {"hp": round(B_HP * p), "atk": round(B_ATK * p),
            "dfn": round(B_DEF * p), "spd": max(1, round(BOSS_SPD * g))}


def mitigate(atk, dfn):
    """DEF 가 깎고 남은 피해. JS 의 dmgOf 와 같은 식이어야 한다.

    예전엔 그냥 빼기(atk - dfn)였다. 보스 ATK 가 49스테이지에 336만인데 내 DEF 는 아무리
    올려도 6만이라 피해가 1.88% 줄었고, 벽의 유산은 0레벨이든 70레벨이든 도달이 똑같았다.
    비율로 깎으면 DEF 가 보스 ATK 와 같은 자리수에 서는 만큼 값을 한다 — 같으면 절반이다.
    뚫고 못 뚫고의 벼랑도 사라진다(예전엔 못 뚫으면 타격당 1 피해였다).
    """
    return max(1.0, atk * atk / (atk + dfn)) if atk > 0 else 1.0   # 한 대의 최소 피해는 1 (JS 와 같다)


def spd_extra(spd, bspd):
    """보스보다 빠른 만큼 늘어나는 추가 타격 수. JS 의 xtra() 와 같은 식이어야 한다."""
    return min(HITS_MAX - 1.0, max(0.0, spd / max(1, bspd) - 1))


def soul_base(g):
    """g 스테이지 보스가 주는 혼의 기준값 (보스 칸 배수·환생·유물·수확 배수를 곱하기 전).
    JS 의 soulOf() 와 같은 식이어야 한다."""
    return g ** SOUL_EXP * SOUL_LATE_MUL ** max(0, g - SOUL_LATE_G)


_SIDE = ([0.0], [0.0])       # 환생 정산 혼의 누적합(배율 포함) · 배율 없는 혼의 누적합


def side_scale(g):
    """g 스테이지까지 깼을 때 환생 한 번이 버는 혼이 후반 배율 덕에 몇 배가 됐는가. SOUL_LATE_G 까지는 1 이다."""
    late, flat = _SIDE
    if len(late) <= g:
        ds = dungeons()
        for i in range(len(late), g + 1):
            m = ds[(i - 1) % len(ds)]["soul"]
            late.append(late[-1] + soul_base(i) * m)
            flat.append(flat[-1] + i ** SOUL_EXP * m)
    return late[g] / flat[g]


def side_base(g):
    """보조 수입(원정·미션·슬롯·유물 중복·황금 개체)이 쓰는 혼의 기준값.
    JS 의 sideSoul() 과 같은 식이어야 한다.

    보조 수입은 '최고 스테이지 보스 몇 판치' 로 정해져 있다(원정 하루 26판치, 미션 13판치…).
    그 한 판의 값을 무엇으로 잡느냐가 후반을 가른다. 세 가지를 재 봤다 (v0.16.0, 들어오는 혼을 전부 넣은 모델).

      후반 배율을 그대로 붙인다 (v0.14.11)  환생이 버는 혼은 1스테이지부터의 합이라, 배율이 붙으면 최고 스테이지
                                           22판치에서 8판치로 줄어든다. 보조 수입은 판수가 그대로라 환생의
                                           0.48배에서 1.4~2.0배가 됐고 5~9층이 층당 3~4회에 열렸다.
      배율을 안 붙인다                     속도는 맞지만 보조 수입이 죽는다 — 6층에서 환생의 10%, 8층부터 1% 아래.
      환생이 커진 만큼만 키운다 (이 식)     side_scale 이 그 배수다. 보조 수입이 환생 대비 예전 비율을 그대로 지킨다 —
                                           깊은 층에서도 환생 사이 수입의 20~40% 로 남는다.

    놀 거리가 후반에 버려지면 안 된다는 것이 기준이다. 대신 6~9층이 층당 3~5회로 조금 빨라진다."""
    return g ** SOUL_EXP * side_scale(g)


def trait_cost(lv):
    return round(COST_K * COST_MUL ** lv)


def final_stats(h, alloc=None, tr=None):
    """최종 스탯 = (기본 + 배분) x 환생 특성 배율. JS의 F()와 같은 식."""
    a, t = alloc or {}, tr or {}
    m = lambda k: (1 + SPD_TRAIT * t.get(k, 0)) if k == "spd" else tmul_of(k) ** t.get(k, 0)
    crit = h["crit"] + a.get("crit", 0) * GAIN["crit"]
    return {
        "atk":  (h["atk"] + a.get("atk", 0) * GAIN["atk"]) * m("atk"),
        "hp":   (h["hp"] + a.get("hp", 0) * GAIN["hp"]) * m("hp"),
        "dfn":  (h["dfn"] + a.get("dfn", 0) * GAIN["dfn"]) * m("dfn"),
        "crit": min(CRIT_CAP, crit),
        "cdmg": (CDMG_BASE + a.get("cdmg", 0) * GAIN["cdmg"]
                 + max(0, crit - CRIT_CAP)) * m("cdmg"),
        "spd":  (h["spd"] + a.get("spd", 0) * GAIN["spd"]) * m("spd"),
    }


def turns_to_win(h, b, alloc=None, tr=None, mut=None):
    """평균 피해 기준 결판 턴수 -> (보스를 잡는 턴, 내가 죽는 턴).
    치명타는 기대값으로 반영. JS 전투와 같은 공식, 난수만 뺐다 = 밸런스 검증용.
    mut = (변이 id, 배수) — mut_power() 가 준다. JS 의 mutate() 와 같은 규칙이어야 한다."""
    s = final_stats(h, alloc, tr)
    mid, k = mut or (None, 1.0)
    cdmg = max(100.0, s["cdmg"] / k) if mid == "numb" else s["cdmg"]
    crit = s["crit"] / k if mid == "pierce" else s["crit"]      # 실명: 치명타 확률 ÷k
    eff = s["atk"] * (1 + crit / 100 * (cdmg / 100 - 1))
    spd = s["spd"] / (k if mid == "curse" else 1)   # 저주는 SPD 를 깎는다 — 2타와 선공을 빼앗는다
    hit = 1 + spd_extra(spd, b["spd"])               # 추가타는 '타격 수'를 늘린다
    kill = b["hp"] * (k if mid == "fury" else 1) / (mitigate(eff, b["dfn"]) / (k if mid == "armor" else 1) * hit)
    die = s["hp"] / mitigate(b["atk"], s["dfn"])
    return kill, die


def wins(h, b, alloc=None, tr=None, mut=None):
    """실제 전투(quickFight)와 같은 판정. 턴은 정수다 — 내가 n_h 턴, 보스가 n_b 대를 쳐야 끝난다.
    내 SPD(저주 반영)가 보스 이상이면 내가 먼저 친다: n_h <= n_b 면 이긴다. 보스가 먼저면 n_h < n_b.
    예전 판정(격파 턴 < 생존 턴, 소수)은 선공을 세지 않았다. 깊은 층은 서로 한 방이라 먼저 치는 쪽이
    이기는데, 그 판정으로는 자동 도전이 수동으로 이기는 곳 앞에서 멈췄다(v0.19.6). JS 의 beatable() 과 같은 식."""
    kill, die = turns_to_win(h, b, alloc, tr, mut)
    mid, k = mut or (None, 1.0)
    first = final_stats(h, alloc, tr)["spd"] / (k if mid == "curse" else 1) >= b["spd"]
    nh, nb = max(1, math.ceil(kill - 1e-9)), max(1, math.ceil(die - 1e-9))   # 누구든 한 대는 쳐야 끝난다
    return nh <= nb if first else nh < nb


def _splits(pts):
    # SPD 도 넣는다 — 추가타가 생긴 뒤로는 turns_to_win 이 값을 잴 수 있다.
    for a in range(11):
        for hp in range(11 - a):
            for d in range(11 - a - hp):
                for c in range(11 - a - hp - d):
                    for x in range(11 - a - hp - d - c):
                        sp = 10 - a - hp - d - c - x
                        yield {"atk": pts*a/10, "hp": pts*hp/10, "dfn": pts*d/10,
                               "crit": pts*c/10, "cdmg": pts*x/10, "spd": pts*sp/10}


def reach(h, tr=None, cap=400, res=None, start=0):
    """이 특성으로 최적 배분을 했을 때 도달 가능한 최고 전역 스테이지.
    res = 변이 내성 {id: 레벨}. start = 이미 닿은 곳(거기서부터 본다 — 내성이 오르는 모델에서 쓴다)."""
    tr = tr or {}
    pts = h["level"] * PT_PER_LEVEL + tr.get("pt", 0) * TRAIT_PT
    # 보스는 스테이지마다 단조로 강해진다. 그래서 best+1 을 못 넘는 배분은 best 를 못 늘린다 —
    # 배분마다 1스테이지부터 다시 오르지 않고 지금 최고 다음 칸만 본다. 값은 같고 14배 빠르다.
    # 이게 느리면 selftest 가 고정 영웅 여럿으로 밸런스를 볼 수 없다.
    # 변이는 배수라 층 경계에서 난이도가 뛰거나 떨어질 수 있다. 배분은 판마다 다시 할 수 있으므로
    # '어느 배분으로든 다음 칸을 넘으면 오른다' — 한 바퀴에 진전이 없을 때까지 배분을 다시 훑는다.
    best, grew = start, True
    while grew and best < cap:
        grew = False
        for alloc in _splits(pts):
            while best < cap:
                if not wins(h, boss(best + 1), alloc, tr, mut_power(best + 1, res)):
                    break
                best += 1
                grew = True
    return best


# 환생 시뮬레이션이 특성을 사는 순서. v0.19.6 부터 신속을 한 바퀴에 한 번 산다 — 승패 판정이 선공을 세자
# 신속이 값을 하게 됐다(먼저 쳐서 한 턴에 잡으면 이긴다). 한 번 섞으면 보통 캐릭터의 6층이 30회에서 26회로
# 당겨지고, 두 번 섞으면 다시 늦어진다. 그래서 한 번이 기준이다.
# selftest 가 '신속을 더 섞어도 층 진입이 2회 넘게 당겨지지 않는다' 를 붙든다. 깨지면 이 순서를 다시 본다.
SIM_ORDER = ["atk", "hp", "dfn", "atk", "hp", "dfn", "cdmg", "cdmg", "pt", "soul", "spd"]


def simulate(h, n_slots, rebirths=40, order=None):
    """환생을 거듭했을 때 몇 회차에 몇 층에 닿는지. 상수 튜닝의 근거.
    영웅은 그대로 둔다 — 환생에 드는 토큰만큼 자라는 것은 세지 않으므로 실제보다 느리게 나온다."""
    import itertools
    souls, tr, rows = 0, {}, []
    cyc = itertools.cycle(order or SIM_ORDER)
    for r in range(rebirths + 1):
        g = reach(h, tr)
        rows.append((r, g, (g - 1) // n_slots + 1, souls, sum(tr.values())))
        souls += round(sum(round(soul_base(i) * (1 + REBIRTH_SOUL * r)) for i in range(1, g + 1))
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
        b, d = boss(g), slot_of(g)
        best = min(_splits(pts), key=lambda a: turns_to_win(h, b, a)[0] - turns_to_win(h, b, a)[1])
        best = next((a for a in _splits(pts) if wins(h, b, a)), best)
        kill, die = turns_to_win(h, b, best)
        ok = wins(h, b, best)
        print(f"{g:>3} {(g-1)//n+1:>2} {d['name'][:20]:20} {b['hp']:>8} {b['atk']:>6} {b['dfn']:>5}"
              f" {kill:>7.1f} {die:>7.1f}  {'승' if ok else '패 <- 벽'}")
        if not ok and g > n:
            break
    print(f"\n[환생 진행] 특성 비용 {COST_K}x{COST_MUL}^lv, 효과 x{TRAIT_MUL}/lv"
          f" ({' · '.join(f'{k} x{v}' for k, v in TRAIT_MULS.items())})")
    print(f"환생 1회 = 새 토큰 {REBIRTH_EXP:,} (최대 {REBIRTH_CAP}회분 적립)")
    print(f"{'환생':>4} {'스테이지':>8} {'층':>3} {'보유혼':>10} {'특성합':>6} {'필요토큰':>12}")
    prev = None
    for r, g, fl, souls, tl in simulate(h, n):
        mark = "   <<< 새 층 진입" if prev is not None and fl > prev else ""
        if prev is None or fl != prev or r % 8 == 0:
            print(f"{r:>4} {g:>8} {fl:>3} {souls:>10} {tl:>6} {r * REBIRTH_EXP:>12,}{mark}")
        prev = fl


def game_data(agg, hosts=(), providers=(), models=(), daily=None):
    """게임 화면에 심는 데이터. build() 와 selftest 의 화면 검증이 같은 길로 만든다 —
    따로 만들면 검증이 보는 화면과 실제 화면이 갈라진다."""
    return {"hero": hero(agg), "dungeons": dungeons(), "themes": THEMES, "raid": RAID_BOSSES, "muts": MUTS, "achs": ACHS,
            "hosts": list(hosts),
            "providers": list(providers), "models": list(models), "daily": daily or {},
            "k": {"step": STEP, "bhp": B_HP, "atkStep": B_ATK_STEP, "hpStep": B_HP_STEP, "batk": B_ATK, "bdef": B_DEF, "bspd": BOSS_SPD, "hitsMax": HITS_MAX, "spdTrait": SPD_TRAIT,
                  "earlyG": EARLY_G, "earlyMul": EARLY_MUL, "lvEase": LV_EASE,
                  "tmul": TRAIT_MUL, "cmul": COST_MUL, "ck": COST_K,
                  "soulExp": SOUL_EXP, "soulLateG": SOUL_LATE_G, "soulLateMul": SOUL_LATE_MUL, "tpt": TRAIT_PT, "tsoul": TRAIT_SOUL,
                  "critCap": CRIT_CAP, "cdmgBase": CDMG_BASE,
                  "tmuls": TRAIT_MULS,
                  "ptPerLevel": PT_PER_LEVEL, "rbExp": REBIRTH_EXP, "rbCap": REBIRTH_CAP,
                  "maxLv": MAX_LV, "lvExp": LV_EXP, "tiers": TIERS,
                  "rbSoul": REBIRTH_SOUL,
                  "dayBudget": DAY_BUDGET, "weekBudget": WEEK_BUDGET,
                  "idleDiv": IDLE_DIV, "idleCapH": IDLE_CAP_H,
                  "idleTokenDiv": IDLE_TOKEN_DIV, "idleTokenMax": IDLE_TOKEN_MAX,
                  "rareRate": RARE_RATE, "rareMul": RARE_MUL, "dexSoul": DEX_SOUL, "dexPt": DEX_PT, "achSoul": ACH_SOUL,
                  "raidBudget": RAID_BUDGET, "raidMinBest": RAID_MIN_BEST, "raidMinHp": RAID_MIN_HP,
                  "mutLv": MUT_LV, "shardRate": SHARD_RATE, "shardK": SHARD_K, "shardStep": SHARD_STEP,
                  "slotTries": SLOT_TRIES, "slotBudget": SLOT_BUDGET, "slotAvg": SLOT_AVG}}


def render(data):
    return TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False))


def build(root=None, out=None):    # root 는 테스트용 단일 경로
    save_snapshot(root)
    agg, projects, models, hosts = merge()
    data = game_data(agg, hosts,
                     sorted(getattr(merge, "providers", {}).items(), key=lambda kv: -kv[1]),
                     models.most_common(), getattr(merge, "daily", {}))
    out = out or game_path()
    html = render(data)
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
.sheet{display:grid;grid-template-columns:repeat(3,minmax(0,auto));justify-content:space-between;gap:2px 6px;
font-size:11px;margin-top:6px;max-width:400px}.sheet>span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sheet b{font-size:13px}
.badge{display:inline-block;border:1px solid var(--line);border-radius:20px;
padding:0 9px;font-size:11px;margin-right:5px}
.alloc{display:grid;gap:6px;align-items:center;font-size:13px}
#alloc{grid-template-columns:minmax(0,1fr) auto auto auto auto auto auto}#traits{grid-template-columns:minmax(0,1fr) auto auto auto auto}
.alloc small{display:block;color:var(--dim);font-size:11px;white-space:normal;overflow-wrap:anywhere}
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
button.chip{padding:1px 2px;font-size:11px;white-space:nowrap;font-variant-numeric:tabular-nums}
#picksBox{flex-basis:100%}
#picksBox summary{font-size:12px;cursor:pointer;padding:6px 10px;background:#21262d;
border:1px solid var(--line);border-radius:6px}#picksBox summary:hover{border-color:var(--on)}
#picks{display:grid;grid-template-columns:repeat(auto-fill,minmax(46px,1fr));gap:4px;max-height:34vh;overflow-y:auto;padding-top:4px}
#picks .flrow{grid-column:1/-1;display:flex;gap:4px;margin-top:4px}#picks .flrow:first-child{margin-top:0}
#picks button.fl{flex:1;min-width:0;text-align:left;font-size:11px;padding:3px 8px;
display:flex;justify-content:space-between;gap:8px}
#picks button.fx{font-size:11px;padding:3px 0;width:26px;flex:none}
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
.vs .row span{white-space:nowrap}.vs .row span:first-child{min-width:0;overflow:hidden;text-overflow:ellipsis}
.hit{animation:hit .3s}@keyframes hit{50%{transform:translateX(9px) scale(.92);filter:brightness(2)}}
.thud{animation:thud .35s ease-out}
@keyframes thud{0%{transform:scaleY(2.2);filter:brightness(3)}60%{transform:scaleY(1);filter:brightness(1.6)}}
.miss{animation:miss .35s ease-out}
@keyframes miss{0%,60%{opacity:.25}30%{opacity:1}}
.dex{display:grid;grid-template-columns:repeat(15,1fr);gap:2px;margin-top:2px}
.dx{text-align:center;font-size:14px;line-height:22px;height:24px;border:1px solid var(--line);border-radius:4px;
background:#0d1117;overflow:hidden}.dx.no{color:var(--dim);font-size:11px;opacity:.45}
.dx.r{border-color:var(--gold);box-shadow:inset 0 0 7px #ffd16666}
.reels{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin:2px 0 10px}
.reels .cell{height:64px;display:flex;align-items:center;justify-content:center;font-size:34px;
border:1px solid var(--line);border-radius:8px;background:#0d1117}
.reels .lit{border-color:var(--gold);box-shadow:0 0 10px var(--gold);animation:thud .35s ease-out}
.reels .spin{filter:blur(1.6px);opacity:.75}
#log{height:132px;overflow-y:auto;font-size:12px;border-top:1px solid var(--line);
margin-top:10px;padding-top:8px}
#log div{margin:1px 0}.crit{color:var(--gold);font-weight:700}.win{color:var(--xp);font-weight:700}
.lose{color:var(--hp);font-weight:700}
</style></head><body><div class="wrap">
<div id="updBanner"></div>

<div class="card hero">
  <div class="face" id="face"></div>
  <div class="meta">
    <div class="gold" id="achTitle" style="font-size:11px;min-height:0"></div>
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
  </div>
</details>

<details class="card" data-k="missions" open>
  <summary><h2>미션 <span class="gold" id="streak"></span></h2></summary>
  <div id="missions"></div>
</details>

<details class="card" data-k="raid" open>
  <summary><h2>주간 레이드 <span class="gold" id="raidHead"></span></h2></summary>
  <div id="raid"></div>
</details>

<details class="card" data-k="slot" open>
  <summary><h2>슬롯 <span class="gold" id="slotHead"></span></h2></summary>
  <div id="slot"></div>
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

<details class="card" data-k="dex" open><summary><h2>보스 도감 <span class="gold" id="dexCount"></span></h2></summary>
  <div id="dexNews"></div><div id="dex"></div></details>

<details class="card" data-k="ach" open><summary><h2>업적 <span class="gold" id="achHead"></span></h2></summary>
  <div id="achNews"></div><div id="ach"></div></details>

<details class="card" data-k="mut" open><summary><h2>변이 내성 <span class="gold" id="mutHead"></span></h2></summary>
  <div id="mutNews"></div><div class="alloc" id="muts" style="grid-template-columns:minmax(0,1fr) auto auto auto"></div>
  <div id="mutHelp"></div></details>

<details class="card" data-k="dungeon" open><summary><h2 id="floorTitle">던전</h2></summary>
  <div id="mutNote"></div>
  <div id="auto" style="margin-bottom:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap"></div>
  <div id="stages"></div><div id="wall"></div></details>
<details class="card" data-k="hosts" open><summary><h2>합산된 기기</h2></summary><div id="hosts" style="font-size:12px"></div>
  <div id="provs" style="font-size:12px;margin-top:10px"></div></details>
<details class="card" data-k="code" open><summary><h2>저장 코드 — 백업·옮기기</h2></summary>
  <div class="dim" style="font-size:11px;margin-bottom:6px">백업하거나 다른 곳으로 옮길 때 쓴다. 고친 코드는 받지 않는다.</div>
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
// 층 테마: 층마다 보스의 이름과 이모지가 다르다. 능력치·혼 배수·유물 능력치는 칸(SLOTS)을 따른다.
// 테마가 끝나면 처음으로 돌아가 '변종'을 붙인다. 파이썬 slot_of() 와 같은 식이어야 한다.
const THEMES = D.themes;
const slotOf = g => { const i = (g-1)%N, fl = Math.floor((g-1)/N), t = THEMES[fl % THEMES.length];
  const lap = Math.floor(fl / THEMES.length), pre = !lap ? "" : lap === 1 ? "변종 " : `변종${lap} `;
  return {...SLOTS[i], name: pre + t[1][i][0], emoji: t[1][i][1], theme: pre + t[0], ti: fl % THEMES.length}; };
// ── 보스 도감: 칸 = "테마번호:칸번호", 값 1 = 이겨 봤다 · 2 = 황금 개체를 잡았다.
// 변종(테마 두 바퀴째)은 같은 칸을 쓴다 — 도감은 보스 75종이 끝이다.
const dexKey = g => { const s = slotOf(g); return s.ti + ":" + s.slot; };
const dexOf = s => (s && s.dex && typeof s.dex === "object") ? s.dex : {};
const dexRare = (s, ti) => SLOTS.filter(x => dexOf(s)[ti + ":" + x.slot] === 2).length;
const dexRareAll = s => Object.values(dexOf(s)).filter(v => v === 2).length;
const dexSoul = () => K.dexSoul * dexRareAll(save);                       // 얻는 혼 +%
// ── 업적: 달성하면 칭호와 얻는 혼 +K.achSoul %. 저장에 이미 있는 기록(층·환생·도감·유물)은 바로 센다
const ACH = D.achs;                                // [id, 분류, 무엇을 세나, 목표, 칭호, 설명, 숨김]
const achOf = s => (s && s.ach && typeof s.ach === "object") ? s.ach : {};
const achSoul = () => K.achSoul * ACH.filter(a => achOf(save)[a[0]]).length;
const stOf = () => (save.st && typeof save.st === "object") ? save.st : (save.st = {});
const tally = (k, d = 1) => { const t = stOf(); t[k] = (t[k] | 0) + d; };
const dexPts = s => THEMES.filter((_, ti) => dexRare(s, ti) === N).length * K.dexPt;
const rollRare = () => Math.random() < K.rareRate;
const rareOf = slot => ({...slot, name: "황금 " + slot.name, emoji: slot.emoji + "✨"});
// 특성 배율은 스탯마다 다르다 (파이썬 tmul_of). TRAITS 가 로드 시점에 부르므로 여기 있어야 한다 —
// 아래에 두면 TDZ 로 스크립트 전체가 죽는다. node --check 는 문법만 보므로 이걸 못 잡는다.
const TM = k => K.tmuls[k] || K.tmul;
// 1,000 이상은 1.25K · 12.5M · 3.4B 처럼 줄인다 — 커진 숫자가 좁은 팝오버에서 밀리지 않게
// 단위는 직접 붙인다. 예전엔 Intl 의 compact 를 썼는데 T(조) 다음 단위가 없어서 1000T · 12,500T ·
// 3,400,000T 로 끝없이 길어졌다 — 보스 HP 가 120스테이지쯤에 거기 닿는다. 유효숫자는 셋이다.
// K M B T 까지만 글자고, 그 위(10^15~)는 승수로 적는다 — 1.25e16. Qa · Qi · Sx 같은 약자도 넣어 봤는데
// 뜻을 외워야 읽힌다. 승수는 숫자 하나만 견주면 된다 (e18 이 e15 보다 크다).
const UNITS = ["K", "M", "B", "T"];
const n = x => { const a = Math.abs(x);
  if (!(a >= 1000) || !isFinite(a)) return String(Math.round(x));
  let u = Math.floor(Math.log10(a) / 3), v = +(a / Math.pow(1000, u)).toPrecision(3);
  if (v >= 1000) { u++; v = +(a / Math.pow(1000, u)).toPrecision(3); }      // 999.5K 는 1M 이다
  if (v < 1) { u--; v = +(a / Math.pow(1000, u)).toPrecision(3); }          // log10 의 끝자리 오차
  const sign = x < 0 ? "-" : "";
  if (u > UNITS.length) { const [m, e] = a.toExponential(2).split("e"); return sign + +m + "e" + +e; }   // 반올림 넘침(9.996e14 -> 1e15)은 toExponential 이 맡는다
  return sign + v + UNITS[u - 1]; };
// 스탯은 1,000 아래에서 소수 한 자리가 뜻이 있다(DEF 281.8) — 그 위는 n() 으로 줄인다
const ns = (x, d) => Math.abs(x) < 1000 ? String(+x.toFixed(d)) : n(x);      // 100.0 은 100 으로 — 좁은 칸에서 한 글자가 아쉽다
// 원정은 초당 소수점 단위로 쌓인다 — 정수로 표시하면 멈춘 것처럼 보인다
const nf = x => x < 1000 ? x.toFixed(2) : n(x);
const MULTIPROV = (D.providers || []).length > 1;
const STATS  = [["atk","공격력","ATK"],["hp","체력","HP"],["dfn","방어력","DEF"],["crit","치명타","CRIT"],
                ["cdmg","치명타 피해","CDMG"],["spd","속도","SPD"]];
const TRAITS = [["atk","힘의 유산","ATK x"+TM("atk")+"/lv"],["hp","혼의 유산","HP x"+TM("hp")+"/lv"],
                ["dfn","벽의 유산","DEF x"+TM("dfn")+"/lv"],
                ["cdmg","파괴의 유산","CDMG x"+TM("cdmg")+"/lv"],
                ["spd","신속의 유산","SPD +"+Math.round(K.spdTrait*100)+"%/lv"],["soul","수확","얻는 혼 +"+K.tsoul+"%/lv"],
                ["pt","각성","스탯 배분 +"+K.tpt+"pt/lv"]];

const fresh = () => ({alloc:{atk:0,hp:0,dfn:0,crit:0,cdmg:0,spd:0}, cleared:[], souls:0, rebirths:0,
            traits:{atk:0,hp:0,dfn:0,crit:0,cdmg:0,spd:0,soul:0,pt:0},
            best:0, exped:{since:Date.now(), seenExp:0}, auto:true, claimed:{}, relics:{}, rbExp:null, farm:0,
            trans:0, dex:{}, raid:{wk:"", got:[]}, shards:0, res:{}, ach:{}, st:{}, title:""});

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
                      * K.ptPerLevel + (s.traits.pt|0) * K.tpt + dexPts(s);
let save = fresh();
// 저장: token-rpg 서버(http)로 열면 서버의 파일 하나를 브라우저·메뉴 막대 앱·다른 기기가 같이 쓴다.
// file:// 로 열면(서버 없음) 예전처럼 이 브라우저의 localStorage 에 둔다.
const SERVED = location.protocol.startsWith("http");
let rev = 0, sync = Promise.resolve();          // 쓰기를 한 줄로 세워 rev 가 꼬이지 않게 한다
const note = t => { $("expedBanner").innerHTML = `<div class="banner">${t}</div>`; };
const adopt = d => { rev = d.rev; save = Object.assign(fresh(), d.save || {}); fill(); };
// 옛 저장에는 새로 생긴 스탯 칸(cdmg 등)이 없다 — 비어 있으면 계산이 전부 NaN 이 된다
function fill(){ const f = fresh(); save.alloc = {...f.alloc, ...save.alloc}; save.traits = {...f.traits, ...save.traits};
                 delete save.bless; delete save.blessOffer; delete save.mini;   // 없어진 칸(축복·혼 사냥)은 저장에서 치운다
  // 도감이 없던 저장: 이미 깬 스테이지의 보스는 본 것으로 찍어 준다 (황금은 지금부터 잡는다)
  save.dex = {...dexOf(save)};
  const seen = Math.min(Math.max(save.best || 0, ...(save.cleared || [])), N * THEMES.length);
  for (let g = 1; g <= seen; g++) if (!save.dex[dexKey(g)]) save.dex[dexKey(g)] = 1;
  // 관문이 없던 때 이미 넘은 층 보스: 그만큼의 내성을 찍어 준다 — 업데이트를 받았더니 뒤로 밀리면 안 된다.
  // 관문이 생긴 뒤로는 내성 없이 최고 기록이 관문을 넘을 수 없으니, 몇 번을 돌려도 같은 결과다
  save.res = {...(save.res || {})};
  for (let g = N * (THEMES.length + 1); g <= (save.best || 0); g += N) {
    const m = mutOf(g); if (m && resOf(m.id) < gateLv(g)) save.res[m.id] = gateLv(g);
  }
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
  const p0 = Math.pow(K.step, K.earlyG - 1), late = g >= K.earlyG;   // 파이썬 boss() 와 같은 식
  return {
  hp: Math.round(late ? K.bhp*p0*Math.pow(K.hpStep, g - K.earlyG) : K.bhp*p),
  atk: Math.round(late ? K.batk*p0*Math.pow(K.atkStep, g - K.earlyG) : K.batk*p),
  dfn: Math.round(K.bdef*p), spd: Math.max(1, Math.round(K.bspd*g)), g }; };
// ── 변이: 변종 층(6층~)의 보스는 층마다 특수한 능력 하나를 갖는다. 능력치 하나에 배수가 걸린다.
// 변이 조각으로 내성을 올리면 배수가 줄고, K.mutLv 레벨이면 한 겹이 사라진다. 11층부터는 두 겹이다.
// 파이썬 mut_of() · mut_power() · turns_to_win() 과 같은 규칙이어야 한다.
const MUTS = D.muts;                             // [id, 이름, 이모지, 세기, 설명틀]
const mutOf = g => { const fl = Math.floor((g - 1) / N) - THEMES.length;
  if (!(fl >= 0) || !MUTS.length) return null;
  const m = MUTS[fl % MUTS.length];
  return {id: m[0], name: m[1], emoji: m[2], base: m[3], tmpl: m[4], lap: Math.floor(fl / MUTS.length) + 1}; };
const resOf = id => (save.res || {})[id] | 0;
// 지금 내성으로 몇 배짜리인가 (1 이면 사라졌다). 내성이 남아도 덤은 없다
const mutPow = g => { const m = mutOf(g); return m ? Math.max(1, Math.pow(m.base, m.lap - resOf(m.id) / K.mutLv)) : 1; };
const mutId = g => mutPow(g) > 1 ? mutOf(g).id : "";      // 아직 살아 있는 변이의 id
// 관문: 변종 층의 마지막 보스는 그 층의 변이를 다 지워야(겹마다 K.mutLv 레벨) 싸울 수 있다. 파이썬 gated() 와 같은 식
const gateLv = g => { const m = mutOf(g); return m && g % N === 0 ? m.lap * K.mutLv : 0; };
const gated = g => resOf((mutOf(g) || {}).id) < gateLv(g);
const mutText = (m, k) => m.tmpl.replace("{0}", +k.toFixed(2));
const shardCost = lv => K.shardK + K.shardStep * lv;
// K.soulLateG 를 넘은 스테이지마다 K.soulLateMul 배 — 파이썬 soul_base() 와 같은 식이어야 한다
const lateMul = g => Math.pow(K.soulLateMul, Math.max(0, g - K.soulLateG));
// 환생 한 번이 버는 혼이 후반 배율 덕에 몇 배가 됐는가 (K.soulLateG 까지는 1). 파이썬 side_scale() 과 같은 식.
const sideScale = (() => { const late = [0], flat = [0];
  return g => { for (let i = late.length; i <= g; i++) { const m = SLOTS[(i-1)%N].soul, p = Math.pow(i, K.soulExp);
                  late.push(late[i-1] + p * lateMul(i) * m); flat.push(flat[i-1] + p * m); }
                return late[g] / flat[g]; }; })();
const soulRaw = (g, late) => Math.round(Math.pow(g, K.soulExp) * (late ? lateMul(g) : sideScale(g))
                                * SLOTS[(g-1)%N].soul
                                * (1 + relicSum("soul")/100) * (1 + K.rbSoul * save.rebirths)
                                * (1 + K.tsoul/100 * save.traits.soul) * (1 + dexSoul()/100) * (1 + achSoul()/100));
// 환생할 때 정산되는 혼. 후반 배율은 여기에만 붙는다.
const soulOf = g => soulRaw(g, true);
// 보조 수입(원정·미션·슬롯·유물 중복·황금 개체)이 쓰는 혼. 파이썬 side_base().
// 후반 배율을 그대로 붙이면(v0.14.11) 보조 수입이 환생보다 많이 벌고, 안 붙이면 깊은 층에서 죽는다.
// 환생이 커진 만큼만(sideScale) 키운다 — 깊은 층에서도 환생 대비 예전 비율로 남는다.
const sideSoul = g => soulRaw(g, false);
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
  return g ? sideSoul(g) / K.idleDiv * tokenMul() : 0;
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
// 치명타 배분은 치명타율이 100% 가 되는 데까지만 받는다. 예전엔 더 넣을 수 있었고 넘친 %p 가 치명타 피해로
// 갔는데, 1점이 0.4%p 라 치명타 피해에 바로 넣는 것(1%p)보다 손해였다. 상한은 토큰·유물에 따라 움직인다 —
// 유물로 치명타가 오르면 남는 점수는 배분으로 돌려준다 (fixCrit). 토큰·유물만으로 넘친 몫은 예전처럼 치명타 피해로 간다.
const critMax = () => Math.max(0, Math.ceil((K.critCap - H.crit - relicSum("crit")) / G.crit - 1e-9));
const fixCrit = () => { if (save.alloc.crit > critMax()) { save.alloc.crit = critMax(); return true; } return false; };
const F = k => k === "crit" ? Math.min(K.critCap, critRaw())
  : k === "cdmg" ? (K.cdmgBase + save.alloc.cdmg*G.cdmg + Math.max(0, critRaw() - K.critCap))
                   * Math.pow(TM("cdmg"), save.traits.cdmg)
  : (H[k] + save.alloc[k]*G[k]) * (k === "spd" ? 1 + K.spdTrait*save.traits.spd : Math.pow(TM(k), save.traits[k]))
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
// 이 줄에 더 넣을 수 있는 점수 — 치명타는 100% 가 되는 데까지만
// 속도는 다음 보스(최고 기록 + 1) 기준 늘 2타가 되는 데까지만 받는다. 더 넣은 점수는 버리는 점수였다.
// 보스 SPD 는 스테이지마다 오르니 상한도 따라 오른다. 이미 넣어 둔 점수는 빼지 않는다 — 더 깊은 보스에선 쓸모가 있다.
const spdAt = a => Math.round((H.spd + a*G.spd) * (1 + K.spdTrait*save.traits.spd) * (1 + relicSum("spd")/100));
const spdMax = () => {
  const g = (save.best || 0) + 1;                // 저주가 붙은 보스면 깎이는 만큼 더 받는다
  const t = K.hitsMax * boss(g).spd * (mutId(g) === "curse" ? mutPow(g) : 1);
  let a = Math.max(0, Math.ceil((t / ((1 + K.spdTrait*save.traits.spd) * (1 + relicSum("spd")/100)) - H.spd) / G.spd));
  while (a > 0 && spdAt(a - 1) >= t) a--;
  while (spdAt(a) < t) a++;
  return a;
};
const roomOf = k => Math.max(0, k === "crit" ? Math.min(left(), critMax() - save.alloc.crit)
  : k === "spd" ? Math.min(left(), spdMax() - save.alloc.spd) : left());
// 상한은 스탯 배분에만 있다. 영구 특성(신속의 유산)은 늘 살 수 있으니 특성 줄에는 이 안내를 달지 않는다.
const capNote = k => k === "crit" && critRaw() >= K.critCap
  ? `<small style="color:var(--gold);white-space:normal">${K.critCap}% — ${
      H.crit + relicSum("crit") >= K.critCap ? "토큰·유물만으로 찼다, 넘친 %p는 치명타 피해로" : "더 넣을 수 없다"}</small>`
  : k === "spd" ? spdNote() : "";
// 다음 보스 기준 2타 확률과, 늘 2타까지 남은 점수. 2타가 천장이라 그 위로 넣은 점수는 버린다.
function spdNote(){
  const g = (save.best || 0) + 1, b = boss(g), p = Math.round(xtra(b) * 100);
  const need = Math.max(0, spdMax() - save.alloc.spd);
  if (!need) return `<small style="color:var(--gold);white-space:normal">${g}스테이지 보스 기준 늘 2타 — ${
    spdMax() ? "더 넣을 수 없다" : "토큰·특성·유물만으로 찼다"}</small>`;
  return `<small style="color:var(--dim);white-space:normal">${g}스테이지 보스 SPD ${n(b.spd)} · 2타 확률 ${p}%`
    + ` (늘 2타까지 ${n(need)}pt)</small>`;
}
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
     <button data-k="${k}" data-d="1" ${roomOf(k)<=0?"disabled":""}>+1</button>
     <button data-k="${k}" data-d="10" ${roomOf(k)<=0?"disabled":""}>+10</button>
     <button data-k="${k}" data-d="max" ${roomOf(k)<=0?"disabled":""}>최대</button>`
  ).join("");
  $("alloc").querySelectorAll("button").forEach(b => b.onclick = () => {
    const k = b.dataset.k, d = b.dataset.d, room = roomOf(k);
    save.alloc[k] += d === "min" ? -save.alloc[k]
                   : d === "-1"  ? -Math.min(1, save.alloc[k])
                   : d === "max" ? room : Math.min(+d, room);
    put(); drawAll();
  });
  // 줄마다 구입(1레벨) · 최대(혼이 되는 만큼)
  $("traits").innerHTML = TRAITS.map(([k,ko,eff]) => {
    const all = buyable(k, 1e9);
    return `<span>${ko}<small>${eff}</small>${k === "spd" ? "" : capNote(k)}</span>
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
       토큰 ${n(K.rbExp)}마다 기운이 하나 찬다(최대 ${K.rbCap}개). 1층부터 다시 시작하고, 특성·혼·유물은 남는다.
       환생할수록 얻는 혼이 늘어난다 (지금 +${Math.round(K.rbSoul * save.rebirths * 100)}%).
       ${!save.cleared.length ? "먼저 스테이지를 하나 깨라." : ch ? "" : "기운이 없다 — 토큰을 더 쓰면 찬다."}</div>`;
  if (can) $("rbBtn").onclick = () => {
    if (!rbArmed) { rbArmed = true; drawRebirth(); return; }
    rbArmed = false;
    save.rbExp = rbBase() + K.rbExp;                        // 기운 하나 소모
    save.souls += gain; save.rebirths++;
    save.cleared = [];   // 배분은 그대로 둔다. 포인트는 레벨·각성에서 오므로 환생해도 줄지 않는다
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
         레벨만 1로 돌아가고 받은 배분 포인트는 남는다. 다시 레벨을 올리며 포인트를 더 받는다.</div>`
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
  && [s.souls, s.rebirths, s.best, s.trans ?? 0, s.shards ?? 0, ...Object.values(s.res || {}), ...s.cleared,
      ...Object.values(s.traits), ...Object.values(s.alloc)]
       .every(v => Number.isInteger(v) && v >= 0)
  && Object.values(s.dex || {}).every(v => v === 1 || v === 2)
  && ((s.raid || {}).got || []).every(v => [0, 1, 2, 3].includes(v))
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

// 추가타 확률 — 보스보다 빠른 만큼 한 번 더 친다. 파이썬 spd_extra() 와 같은 식이어야 한다.
// 저주는 이 보스 앞에서 내 SPD 를 깎는다 — 2타 확률과 선공이 모두 이 값을 쓴다. 파이썬 turns_to_win() 과 같은 식
const spdVs = b => spdNow() / (b.g && mutId(b.g) === "curse" ? mutPow(b.g) : 1);
const goesFirst = b => spdVs(b) >= b.spd;          // 내가 먼저 친다
const xtra = b => Math.min(K.hitsMax - 1, Math.max(0, spdVs(b) / Math.max(1, b.spd) - 1));
// 한 턴에 치는 횟수. 보스보다 N 배 빠르면 N 번, 소수점은 확률로 한 번 더.
const hitsOf = b => { const x = 1 + xtra(b); return Math.floor(x) + (Math.random() < x % 1 ? 1 : 0); };
// 한 턴 피해와 치명타 여부. 지금은 2타가 천장이지만, 타격이 많아도 멎지 않게 그대로 둔다 —
// HIT_ROLL 을 넘으면 한 대를 굴려 기댓값에 곱한다. 안 그러면 자동 도전이 화면을 멎게 한다.
const HIT_ROLL = 50;
function turnDmg(a, d, hits, critPct){
  if (hits <= HIT_ROLL) {
    let sum = 0, crit = false;
    for (let i = 0; i < hits; i++) {
      const c = Math.random()*100 < critPct;
      crit = crit || c; sum += dmgOf(a, d, c);
    }
    return [sum, crit];
  }
  const one = dmgOf(a, d, false) * (1 + critPct/100 * (a.cdmg/100 - 1));
  return [Math.round(hits * one), critPct > 0];
}

// 이길 수 있는가 — 벽 표시와 자동 도전이 같은 기준을 쓴다. 실제 전투처럼 턴을 정수로 세고 선공을 따진다:
// 내가 먼저 치면 '내가 잡는 턴 <= 보스가 나를 잡는 대수', 보스가 먼저면 '<'. 피해는 평균값이다.
// 예전엔 소수 턴끼리 비교해 선공을 세지 않았다 — 서로 한 방인 깊은 층에서 수동으로는 이기는 곳 앞에서
// 자동 도전이 멈췄다(v0.19.6). 파이썬 wins() 와 같은 식이어야 한다.
const beatable = b => {
  if (gated(b.g)) return false;                  // 관문 — 내성이 모자라면 능력치와 상관없이 못 넘는다
  const id = mutId(b.g), k = mutPow(b.g);        // 변이 — 전투(mutate)와 같은 규칙
  const cd = id === "numb" ? Math.max(100, F("cdmg") / k) : F("cdmg");
  const cr = id === "pierce" ? F("crit") / k : F("crit");
  const eff = F("atk") * (1 + cr/100 * (cd/100 - 1));
  const mit = (atk, dfn) => atk * atk / (atk + dfn);
  const kill = b.hp * (id === "fury" ? k : 1) / (mit(eff, b.dfn) / (id === "armor" ? k : 1) * (1 + xtra(b)));
  const die = F("hp") / mit(b.atk, F("dfn"));
  const nh = Math.max(1, Math.ceil(kill - 1e-9)), nb = Math.max(1, Math.ceil(die - 1e-9));   // 누구든 한 대는 쳐야 끝난다
  return goesFirst(b) ? nh <= nb : nh < nb;
};

// ── 자동 도전: 첫 환생 후 해금. 1초에 한 판씩 연출 없이 같은 규칙으로 싸운다.
// 목표(save.farm: 0 = 끝까지, g = g스테이지)까지 한 칸씩 오르고, 더 못 오르면 멈추지 않고
// 목표(또는 이번 판 가장 깊은 곳)를 반복한다 — 유물 파밍. 환생해도 켜 둔 채면 다시 오른다.
let autoMsg = "", autoFailSig = "", autoKey = "", farmW = 0, farmL = 0, farmI = 0;
let picksOpen = false;   // 다시 그려도 펼친 상태는 유지한다 — 고르는 중에 접히면 못 쓴다
// 층마다 칸을 펼치고 접는다. 층이 늘면 칸이 수백 개라 원하는 층을 찾기 어려웠다.
// 처음에는 가장 깊은 층만 펼친다. 누른 층은 이 창을 닫을 때까지 기억한다(저장에는 안 쓴다).
let floorsOpen = null;
// 능력치가 그대로면 진 스테이지로 다시 오르지 않는다 (치명타 운으로 벽을 넘는 반복 방지)
const statSig = () => STATS.map(([k]) => F(k)).join();
// 밀린 전투를 몰아 칠 때(quiet)는 저장·다시 그리기를 끝에 한 번만 한다
let quiet = false, dirty = false;
const aput = () => { if (quiet) dirty = true; else put(); };
const adraw = () => { if (!quiet) drawAll(); };
const autoReset = () => { autoFailSig = ""; autoMsg = ""; farmW = farmL = farmI = 0; };
// 반복할 스테이지들. 옛 저장은 숫자 하나였다 — 읽을 때 배열로 맞춘다.
// 비어 있으면 예전처럼 끝까지 오르고 이길 수 있는 가장 깊은 곳을 돈다.
const picks = () => (Array.isArray(save.farm) ? save.farm : save.farm > 0 ? [save.farm] : [])
  .filter(g => Number.isInteger(g) && g >= 1 && g <= save.best).sort((a, b) => a - b);
const setPicks = ps => { save.farm = [...new Set(ps)].sort((a, b) => a - b); autoReset(); put(); drawAuto(); };
const autoSay = m => { if (m !== autoMsg) { autoMsg = m; drawAuto(); } };
// f 층에서 역대 깬 스테이지들 (반복으로 고를 수 있는 칸). 마지막 층은 깬 데까지만이다
const floorStages = f => { const out = [];
  for (let g = (f - 1) * N + 1; g <= Math.min(f * N, save.best || 0); g++) out.push(g);
  return out; };
function drawAuto(){
  if (!save.rebirths) {
    autoKey = "";
    $("auto").innerHTML = '<span class="dim" style="font-size:11px">자동 도전 — 첫 환생 후 해금</span>';
    return;
  }
  // 버튼·칸은 바뀔 때만 다시 그린다 — 매초 갈아엎으면 누르는 중에 사라진다
  const ps = picks(), on = new Set(ps), best = save.best || 0;
  const top = Math.max(1, Math.ceil(best / N));
  if (!floorsOpen) floorsOpen = new Set([top]);
  const key = `${save.auto}|${ps.join()}|${best}|${[...floorsOpen].join()}`;
  if (key !== autoKey || !$("autoMsg")) {
    autoKey = key;
    // 스테이지마다 유물 속성이 다르다 — 여러 곳을 골라 돌려야 속성이 골고루 모인다
    // 층마다 머리줄을 둔다 — 누르면 그 층에서 깬 스테이지를 통째로 고르거나 뺀다.
    // 칸을 하나씩만 고를 수 있던 때는 한 층을 돌리려면 열다섯 번을 눌러야 했다.
    const chips = [];
    for (let g = 1; g <= best; g++) {
      const f = Math.floor((g - 1) / N) + 1, open = floorsOpen.has(f);
      if ((g - 1) % N === 0) {
        const all = floorStages(f), cnt = all.filter(x => on.has(x)).length;
        chips.push(`<div class="flrow"><button class="fx" data-x="${f}" title="${open ? "접기" : "펼치기"}">${open ? "▾" : "▸"}</button>
          <button class="fl${cnt === all.length ? " on" : ""}" data-f="${f}"
          title="이 층을 통째로 고르거나 뺀다">${f}층 ${slotOf(g).theme}
          <span>${cnt === all.length ? "전부 고름 — 눌러서 빼기" : `${cnt}/${all.length} — 눌러서 층 전체 고르기`}</span></button></div>`);
      }
      if (open) chips.push(`<button class="chip${on.has(g) ? " on" : ""}" data-g="${g}"
        title="${g}. ${slotOf(g).name}">${slotOf(g).emoji}${g}</button>`);
    }
    $("auto").innerHTML = `<button id="autoBtn" class="${save.auto?"on":""}">자동 ${save.auto?"켜짐":"꺼짐"}</button>
      <button id="pickAll">${ps.length === best && best ? "전체 해제" : "전체 반복"}</button>
      <details id="picksBox" ${picksOpen ? "open" : ""}><summary>${ps.length
        ? `고른 ${ps.length}곳을 차례로 반복` : "끝까지 오르고 가장 깊은 곳 반복"} — 눌러서 고르기</summary>
        <div id="picks">${chips.join("")}</div></details>
      <span class="dim" id="autoMsg" style="font-size:11px;flex-basis:100%"></span>`;
    $("autoBtn").onclick = () => { save.auto = !save.auto; autoReset(); put(); drawAuto(); };
    $("pickAll").onclick = () => setPicks(ps.length === best
      ? [] : Array.from({length: best}, (_, i) => i + 1));
    $("picksBox").ontoggle = e => { picksOpen = e.target.open; };
    $("picks").onclick = e => {                       // 칸 하나하나가 아니라 묶어서 듣는다
      // 머리줄 안의 글자(span)를 눌러도 머리줄로 친다
      const d = (e.target.closest ? e.target.closest("button") || e.target : e.target).dataset || {};
      const g = +d.g, f = +d.f, x = +d.x;
      if (x) { floorsOpen.has(x) ? floorsOpen.delete(x) : floorsOpen.add(x); drawAuto(); }
      else if (g) setPicks(on.has(g) ? ps.filter(x => x !== g) : [...ps, g]);
      else if (f) {                                   // 층 머리줄: 다 골라져 있으면 빼고, 아니면 채운다
        const all = floorStages(f);
        setPicks(all.every(x => on.has(x)) ? ps.filter(x => !all.includes(x)) : [...ps, ...all]);
      }
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
    const rare = rollRare();
    if (quickFight(boss(next))) {
      winStage(next); autoMsg = `${next}. ${slotOf(next).name} 격파`;
      if (rare) { dexNews = catchRare(next); aput(); }
      if (rollShard(next)) aput();
      adraw();
    } else {
      autoFailSig = statSig() + "@" + next; tally("losses");
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
  const rare = rollRare();                       // 황금 개체 — 이겨야 잡는다
  if (quickFight(boss(g))) {
    farmW++; winTally();
    const drop = rollRelic(g, slotOf(g), false);
    if (drop) relicNews = drop;
    if (rare) dexNews = catchRare(g);
    const shard = rollShard(g);
    if (drop || rare || shard) { aput(); adraw(); }
  } else { farmL++; tally("losses"); }
  autoSay(`${g}스테이지 반복 중${ps.length > 1 ? ` (고른 ${ps.length}곳 순회)` : ""}`
    + ` · 승 ${farmW} 패 ${farmL}${next <= tgt ? ` · ${next}스테이지는 아직 무리` : ""}`);
}

function drawStages(){
  const fl = floorNow(), base = (fl - 1) * N;
  $("floorTitle").textContent = `던전 — ${fl}층 ${slotOf(base+1).theme} (전역 ${base+1}~${base+N} 스테이지)`;
  drawAuto();
  $("stages").innerHTML = "";
  let blocked = null;
  SLOTS.forEach((_, i) => {
    const g = base + i + 1, b = boss(g), slot = slotOf(g);
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
        !goesFirst(b) ? " · <span style='color:var(--hp);white-space:nowrap'>보스 선공</span>"
        : xtra(b) ? ` · <span style="color:var(--on);white-space:nowrap">한 턴에 ${(1 + xtra(b)).toFixed(1)}회</span>` : ""}
      <br>격파 시 혼 ${n(soulOf(g))} · 유물 ${AFFIX[slot.affix][0]}</small></div>`;
    const btn = document.createElement("button");
    const shut = gated(g);                       // 관문 — 내성을 채우기 전에는 도전도 못 한다
    btn.textContent = !open ? "잠김" : shut ? "내성 부족" : done ? "재도전" : "도전";
    btn.disabled = !open || shut;
    btn.onclick = () => fightStart(g, slot, b);
    el.appendChild(btn); $("stages").appendChild(el);
    // 평균 피해 기준으로 이길 수 없는 첫 스테이지 = 환생이 필요한 벽
    if (open && !done && blocked === null && !beatable(b)) blocked = g;
  });
  const gm = blocked !== null && gated(blocked) ? mutOf(blocked) : null;
  $("wall").innerHTML = blocked === null ? "" : gm ?
    `<div class="wall">관문: ${blocked}스테이지는 ${gm.emoji} ${gm.name} 내성 Lv.${gateLv(blocked)} 이 필요하다
     (지금 Lv.${resOf(gm.id)}). 반복 전투로 조각을 모아라.</div>` :
    `<div class="wall">벽: ${blocked}스테이지는 지금 능력치로 못 넘는다. 환생해서 특성을 올려라.</div>`;
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
       깊이 깰수록, 토큰을 많이 쓸수록 많이 캔다.</div>`;
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
  const reward = m => Math.round(Math.max(50, sideSoul(Math.max(1, save.best))) * m * streakMul());
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
    `<div class="dim" style="font-size:11px;margin-top:6px">AI를 쓴 만큼 채워진다. 매일 쓰면 보상이 늘어난다(최대 7일).</div>`;
  $("missions").querySelectorAll("button[data-k]").forEach(b => b.onclick = () => {
    const m = missions().find(x => x.key === b.dataset.k);
    if (!m || m.cur < m.goal || got(m.key)) return;
    // 2주 지난 수령 기록은 버린다 — 저장이 끝없이 커지지 않게
    save.claimed = Object.fromEntries(Object.entries(save.claimed || {})
      .filter(([k]) => k.split(":")[1] >= daysBack(14)));
    save.claimed[m.key] = 1; save.souls += m.souls; put(); drawAll();
  });
}

// ── 주간 레이드: 탑과 별개로 매주 월요일에 보스 하나가 선다. HP 는 이번 주에 실제로 쓴 토큰으로만 깎인다.
// 스탯과 전투는 관계없다 — 탑이 막힌 주에도 할 일이 남는다. 진행도는 로그에서 온 값이라 저장을 고쳐도 못 바꾼다.
// 저장하는 것은 이번 주에 받은 단계뿐이다 (save.raid = {wk: 월요일 날짜, got: [0..3]}).
const RAID = D.raid;
const raidDow = () => (new Date().getDay() + 6) % 7;                       // 월요일 = 0
const raidWk = () => daysBack(raidDow());                                  // 이번 주 월요일 날짜
const raidIdx = wk => Math.floor(Date.parse(wk + "T00:00:00Z") / 6048e5);  // 주 번호 — 주마다 1 씩 는다
// k 주 전(0 = 이번 주)의 월~일에 쓴 토큰. 이번 주는 오늘까지다
const weekSum = k => { let s = 0;
  for (let i = 0; i < 7; i++) { const back = raidDow() + 7 * k - i; if (back >= 0) s += dayTok(daysBack(back)); }
  return s; };
// 보스 HP = 지난 4주의 주간 사용량 평균(쓴 주만). 평소대로 쓰면 주말쯤 잡는다. 이번 주는 넣지 않으니 주중에 안 변한다
const raidHp = () => { const ws = [1, 2, 3, 4].map(weekSum).filter(Boolean);
  return Math.max(K.raidMinHp, ws.length ? nice(ws.reduce((a, b) => a + b) / ws.length) : 0); };
const raidBoss = () => RAID[raidIdx(raidWk()) % RAID.length];
const raidOpen = () => (save.best || 0) >= K.raidMinBest;
const raidGot = () => { const r = save.raid || {}; return r.wk === raidWk() && Array.isArray(r.got) ? r.got : []; };
// 네 단계가 한 주 예산(K.raidBudget)을 똑같이 나눈다. 연속 사용 배율은 없다 — 레이드가 이미 한 주의 사용량을 잰다
const raidPay = () => Math.round(Math.max(50, sideSoul(Math.max(1, save.best))) * K.raidBudget / 4);
let raidNews = "";
function raidClaim(i){
  const got = raidGot();
  if (!raidOpen() || got.includes(i) || weekSum(0) < raidHp() * (i + 1) / 4) return;
  save.raid = {wk: raidWk(), got: [...got, i].sort()};
  const pay = raidPay();
  save.souls += pay;
  raidNews = `${(i + 1) * 25}% 돌파 — 혼 +${n(pay)}`;
  if (i === 3) {
    const t = stOf(), wi = raidIdx(raidWk());
    t.raids = (t.raids | 0) + 1;
    t.raidRun = t.raidLast === wi - 1 ? (t.raidRun | 0) + 1 : 1; t.raidLast = wi;
    t.raidRunMax = Math.max(t.raidRunMax | 0, t.raidRun);
    const drop = raidRelic();
    relicNews = drop;
    raidNews = `${raidBoss()[0]} 격파 — 혼 +${n(pay)} · ${drop}`;
  }
  put(); drawAll();
}
// 격파 보상: 가진 유물 하나를 한 등급 올린다. 처음엔 등급을 새로 굴렸는데(v0.17.0), 열에 여섯은 일반이라
// 유물을 모은 뒤로는 거의 혼으로 바뀌었다 — 한 주를 채워 잡은 보상이 빈손이었다.
// 올릴 것이 없으면 버려지지 않게 다른 것을 준다: 유물이 하나도 없으면 하나를 주고, 전부 신화면 혼을 준다.
// 등급의 천장(신화)은 그대로다. 빨라지는 것은 거기까지 가는 길뿐이다 — 15칸 전부 신화까지 많아야 60주.
function raidRelic(){
  const rs = save.relics = save.relics || {};
  const owned = SLOTS.filter(s => relicOk(rs[relicKey(s)]));
  const up = owned.filter(s => rs[relicKey(s)].r < RARITY.length - 1);
  if (up.length) {
    const slot = up[Math.floor(Math.random() * up.length)], k = relicKey(slot), old = rs[k];
    rs[k] = {...old, r: old.r + 1};
    const from = (floorNow() - 1) * N + slot.slot;      // 도감과 같은 이름 — 지금 층에서 그 칸을 지키는 보스
    return `${from}. ${slotOf(from).name}의 유물 승급: ${RARITY[old.r][0]} → ${RARITY[old.r + 1][0]} — `
         + `${AFFIX[old.a][0]} +${relicVal(rs[k])}${AFFIX[old.a][2]}`;
  }
  if (!owned.length) {                           // 올릴 유물이 없다 — 이번 주 칸의 유물을 하나 준다
    const slot = SLOTS[raidIdx(raidWk()) % N];
    const g = Math.floor(Math.max(0, save.best - slot.slot) / N) * N + slot.slot;
    return rollRelic(g, slotOf(g), false, true);
  }
  const pay = sideSoul(Math.max(1, save.best));  // 전부 신화다 — 최고 스테이지 한 판치 혼
  save.souls += pay;
  return `유물이 모두 ${RARITY[RARITY.length - 1][0]}다 — 혼 +${n(pay)}`;
}
function drawRaid(){
  if (!raidOpen()) {
    $("raidHead").textContent = "";
    $("raid").innerHTML = `<div class="dim">${K.raidMinBest}스테이지(2층 첫 보스)를 깨면 레이드가 열린다.</div>`;
    return;
  }
  const [name, emoji] = raidBoss(), hp = raidHp(), dealt = weekSum(0), got = raidGot();
  const left = Math.max(0, hp - dealt);
  $("raidHead").textContent = left ? `${Math.floor(100 * dealt / hp)}%` : "격파";
  $("raid").innerHTML =
    (raidNews ? `<div class="banner" style="color:var(--gold);border-color:var(--gold)">${raidNews}</div>` : "") +
    `<div class="st" style="border-top:0"><div class="e">${emoji}</div>
       <div class="n"><b class="nm" title="${name}">${left ? "" : "✓ "}${name}</b>
       <div class="bar hpb"><i style="width:${100 * left / hp}%"></i></div>
       <small>남은 HP ${n(left)} / ${n(hp)} · 이번 주 쓴 토큰 ${n(dealt)}</small></div></div>` +
    [0, 1, 2, 3].map(i => {
      const need = hp * (i + 1) / 4, done = dealt >= need, had = got.includes(i);
      return `<div class="st"><div class="n"><b>${had ? "✓ " : ""}${(i + 1) * 25}% ${i === 3 ? "격파" : "돌파"}</b>
        <small style="display:block">토큰 ${n(need)} · 보상 혼 ${n(raidPay())}${i === 3 ? " · 유물 한 등급 승급" : ""}</small></div>
        <button data-r="${i}" ${done && !had ? "" : "disabled"}>${had ? "받음" : done ? "받기" : "진행 중"}</button></div>`;
    }).join("") +
    `<div class="dim" style="font-size:11px;margin-top:6px">매주 월요일에 새 보스가 나온다. 이번 주에 쓴 토큰만큼 HP가 깎인다.
     평소대로 쓰면 주말쯤 잡는다. 격파하면 유물 하나가 한 등급 오른다. 받지 않은 보상은 다음 주에 사라진다.</div>`;
  $("raid").querySelectorAll("button[data-r]").forEach(b => b.onclick = () => raidClaim(+b.dataset.r));
}

// ── 슬롯: 3x3, 가로 셋 + 세로 셋 + 대각 둘 = 여덟 줄. 하루 K.slotTries 판을 그냥 준다.
// 운으로 버는 칸이다 — 잘 하고 못 하고가 없으니
// 하루 기대 수입을 K.slotBudget 격파분에 못박고(slotPay 가 평균 배당으로 나눈다) 변동성만 남긴다.
// 스테이지 클리어마다 한 장씩 주는 안은 접었다 — 원정 자동반복이 1초에 한 판이라 티켓이 무한이 되고,
// '처음 깬 스테이지만' 으로 막으면 이번엔 하루 한두 장이라 열어볼 이유가 안 된다.
const SLOT_SYM = [["🪙", 30, 4], ["🐛", 23, 6], ["🐍", 18, 9], ["🦇", 13, 15],
                  ["🗿", 9, 30], ["🐉", 4, 90], ["💎", 3, 300]];  // 문양, 가중치(합 100), 석 줄 배당
// 줄마다 [칸, 행] 셋. 세로는 한 칸에서 세 행을 쓰므로 '칸마다 행 하나' 로는 적을 수 없다.
// 실제 슬롯은 세로를 라인으로 안 세지만(릴 하나가 보여주는 창일 뿐이다) 3x3 격자를 보면
// 누구나 세로도 센다 — 화면이 말하는 것과 규칙이 어긋나면 그건 규칙이 틀린 것이다.
const SLOT_LINE = [
  [[0, 0], [1, 0], [2, 0]], [[0, 1], [1, 1], [2, 1]], [[0, 2], [1, 2], [2, 2]],   // 가로 셋
  [[0, 0], [0, 1], [0, 2]], [[1, 0], [1, 1], [1, 2]], [[2, 0], [2, 1], [2, 2]],   // 세로 셋
  [[0, 0], [1, 1], [2, 2]], [[0, 2], [1, 1], [2, 0]]];                            // 대각 둘
const SLOT_TICK = 70, SLOT_STOP = [700, 1050, 1400];  // 칸은 왼쪽부터 차례로 선다 — 한꺼번에 서면 밋밋하다
const slotRoll = () => { let x = Math.random() * 100, i = 0;
  while (i < SLOT_SYM.length - 1 && x >= SLOT_SYM[i][1]) { x -= SLOT_SYM[i][1]; i++; }
  return i; };
let slotG = [[0, 1, 2], [3, 1, 0], [2, 0, 4]], slotDone = 3, slotWin = [], slotMsg = "";
let slotEarned = 0;                             // 오늘 이 칸에서 번 혼 — 화면에만 쓰고 저장하지 않는다
let slotDry = false;                            // 이번 판이 연습판인가(오늘 몫을 다 쓴 뒤)
const slotLit = (c, r) => slotWin.some(L => L.some(x => x[0] === c && x[1] === r));
// 남은 판수는 날짜 + 쓴 횟수로 저장한다. 자정에 저절로 초기화된다
const slotDay = () => { const t = save.slot || {}; return t.day === daysBack(0) ? t : {day: daysBack(0), n: 0}; };
const slotLeft = () => Math.max(0, K.slotTries - (slotDay().n | 0));
// 한 판 지급 = 하루 예산 / (판수 x 평균 배당) x 이번 배당. 운이 나빠도 좋아도 하루 기대값은 그대로다
const slotPay = mul => Math.round(sideSoul(Math.max(1, save.best)) * K.slotBudget
                                  / (K.slotTries * K.slotAvg) * mul);

function slotScore(){
  let sum = 0;
  slotWin = SLOT_LINE.filter(L => {
    const s = slotG[L[0][0]][L[0][1]];
    if (!L.every(x => slotG[x[0]][x[1]] === s)) return false;
    sum += SLOT_SYM[s][2]; return true;
  });
  const pay = sum && !slotDry ? slotPay(sum) : 0;
  if (!slotDry && slotWin.some(L => SLOT_SYM[slotG[L[0][0]][L[0][1]]][0] === "💎")) stOf().jackpot = 1;
  save.souls += pay; slotEarned += pay;         // 판마다 바로 들어온다
  slotMsg = sum
    ? `<span class="${sum >= 90 ? "crit" : "win"}">${slotWin.length}줄 · 배당 x${sum}</span>`
      + (slotDry ? ` <span class="dim">· 연습판이라 혼은 안 들어온다</span>`
                 : ` <span class="soul">+${n(pay)} 혼</span>`)
    : `<span class="dim">꽝 — 다시</span>`;
  put(); drawAll();
}
function slotSpin(){
  if (slotDone < 3 || !save.best) return;
  slotDry = slotLeft() <= 0;                    // 오늘 몫을 다 썼으면 돌아가되 혼은 안 준다
  // 돌리는 순간 한 판이 빠진다 — 결과를 보고 새로고침으로 무를 수 없게
  if (!slotDry) save.slot = {day: daysBack(0), n: (slotDay().n | 0) + 1};
  slotDone = 0; slotWin = []; slotMsg = ""; put();
  const t0 = Date.now();
  const iv = setInterval(() => {
    slotDone = SLOT_STOP.filter(t => Date.now() - t0 >= t).length;
    for (let c = slotDone; c < 3; c++) slotG[c] = [slotRoll(), slotRoll(), slotRoll()];
    if (slotDone >= 3) { clearInterval(iv); slotScore(); }
    drawSlot();
  }, SLOT_TICK);
}
function drawSlot(){
  const rest = slotLeft();
  $("slotHead").textContent = save.best
    ? (rest > 0 ? `오늘 ${rest}/${K.slotTries}판` : "오늘 몫 소진 — 연습판")
      + (slotEarned ? ` · 혼 ${n(slotEarned)}` : "")
    : "";
  if (!save.best) {
    $("slot").innerHTML = '<div class="dim">스테이지를 하나 클리어하면 슬롯이 열린다.</div>';
    return;
  }
  $("slot").innerHTML =
    `<div class="reels">${[0, 1, 2].map(r => [0, 1, 2].map(c =>
       `<div class="cell${slotLit(c, r) ? " lit" : ""}${c >= slotDone ? " spin" : ""}"
        >${SLOT_SYM[slotG[c][r]][0]}</div>`).join("")).join("")}</div>
     <button class="big" id="slotBtn" ${slotDone < 3 ? "disabled" : ""}>${
       slotDone < 3 ? "도는 중…" : rest > 0 ? `돌려라 — ${rest}판 남음`
                                            : "연습으로 돌려라 — 혼은 안 들어온다"}</button>
     <div style="margin-top:8px;font-size:13px;min-height:20px">${slotMsg}</div>
     <div class="dim" style="font-size:11px">
       가로·세로·대각선 여덟 줄 중 한 줄에 같은 문양이 셋이면 당첨이다.
       ${SLOT_SYM.map(x => `${x[0]} x${x[2]}`).join(" · ")} (x1 = 혼 ${n(slotPay(1))})<br>
       하루 ${K.slotTries}판, 자정에 다시 찬다. 다 쓴 뒤에는 혼 없이 연습판으로 돌릴 수 있다.</div>`;
  const b = $("slotBtn");
  if (b) b.onclick = slotSpin;
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
function rollRelic(g, slot, first, sure){       // sure = 확률 문을 건너뛴다 (레이드 격파 보상)
  if (!sure && Math.random() >= (first ? RELIC_FIRST : RELIC_AGAIN)) return "";
  let x = Math.random() * 100, r = 0;
  while (r < RARITY.length - 1 && x >= RARITY[r][1]) { x -= RARITY[r][1]; r++; }
  const a = slot.affix;                           // 능력치는 보스가 정한다 — 무작위는 등급뿐
  save.relics = save.relics || {};
  const k = relicKey(slot), old = save.relics[k];
  if (relicOk(old) && old.r >= r) {               // 같거나 낮은 등급 중복 -> 혼으로
    // 반복 파밍 중복은 1/RELIC_DUP — 켜 두기만 해도 혼이 쏟아져 환생 기운(토큰) 제한을 우회하지 않게
    const s = first ? sideSoul(g) : Math.round(sideSoul(g) / RELIC_DUP); save.souls += s;
    // 획득 메시지처럼 보스 이름을 밝힌다 — 능력치가 보스마다 고정이라 어디서 나왔는지가 정보다
    return `${slot.name} — ${RARITY[r][0]} 유물 중복, 혼 ${n(s)}로 바꿨다`;
  }
  save.relics[k] = {r, a, g};                     // g = 떨어뜨린 스테이지. 도감이 그 보스 이름을 보여 준다
  return `${RARITY[r][0]} 유물 획득! ${slot.name} — ${AFFIX[a][0]} +${relicVal({r, a})}${AFFIX[a][2]}`;
}
function drawRelics(){
  const rs = save.relics || {};
  $("relicCount").textContent = `${SLOTS.filter(s => relicOk(rs[relicKey(s)])).length}/${N}`;
  const sum = Object.keys(AFFIX).map(a => [a, relicSum(a)]).filter(([, v]) => v)
    .map(([a, v]) => `${AFFIX[a][0]} +${+v.toFixed(1)}${AFFIX[a][2]}`).join(" · ");
  $("relicNews").innerHTML =
    (relicNews ? `<div class="banner" style="color:var(--gold);border-color:var(--gold)">${relicNews}</div>` : "") +
    `<div class="dim" style="font-size:11px;margin-bottom:6px">보스를 이기면 가끔 유물이 나온다
     (처음 이길 때 ${RELIC_FIRST*100}%, 다시 이길 때 ${RELIC_AGAIN*100}%).${sum ? " 합계: " + sum : ""}
     <br>이름은 <b>지금 ${floorNow()}층</b> 보스 기준이다. 다른 층에서도 같은 자리 보스가 같은 유물을 준다.</div>`;
  $("relics").innerHTML = SLOTS.map(slot => {
    const r = rs[relicKey(slot)];
    // 유물은 칸마다 하나인데 보스 이름은 층마다 다르다. 늘 지금 층에서 그 칸을 지키는 보스로 적는다 —
    // 던전 목록과 같은 번호·같은 이름이라, 이 유물을 올리려면 어디를 깨야 하는지가 바로 보인다.
    // 처음엔 '떨어뜨린 보스' 이름으로 적었다(v0.15.0). 5층에 있는데 도감은 1층 이름이라 짝을 찾을 수 없었다.
    const from = (floorNow() - 1) * N + slot.slot;
    const s = slotOf(from);
    // 긴 이름은 한 줄에서 말줄임 — 오른쪽 등급이 줄바꿈되지 않게
    return `<div class="row" style="font-size:12px;padding:2px 0"><span style="flex:1;min-width:0;overflow:hidden;
        text-overflow:ellipsis;white-space:nowrap" title="${s.name}">${s.emoji} ${from}. ${s.name}</span>${relicOk(r)
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
    <div class="dim" style="margin-top:4px">능력치는 보스마다 정해져 있고 등급만 무작위다.
      더 높은 등급이 나와야 바뀌고, 같거나 낮으면 혼이 된다.</div></details>`;
}

// ── 황금 개체를 잡았다. 처음이면 도감에 찍고 혼을 준다. 이미 잡은 칸은 혼을 주지 않는다 —
// 자동 반복이 1초에 한 판이라, 주면 켜 두기만 해도 혼이 쌓여 환생 기운(토큰) 제한을 돈다.
let dexNews = "", fightRare = false;
function catchRare(g){
  const k = dexKey(g), s = rareOf(slotOf(g));
  if (dexOf(save)[k] === 2) return `${s.emoji} ${s.name} — 이미 도감에 있다`;
  const pay = sideSoul(g) * K.rareMul;           // 찍기 전 배수로 센다 — 자기 보너스를 자기가 받지 않는다
  save.dex = {...dexOf(save), [k]: 2};
  save.souls += pay;
  const gd = stOf().gd || {}; stOf().gd = {day: daysBack(0), n: (gd.day === daysBack(0) ? gd.n | 0 : 0) + 1};
  if (stOf().gd.n >= 2) stOf().gold2 = 1;
  return `${s.emoji} ${s.name} 포획! 도감에 올렸다 — 혼 +${n(pay)} · 얻는 혼 +${K.dexSoul}%`
    + (dexRare(save, slotOf(g).ti) === N ? ` · ${THEMES[slotOf(g).ti][0]} 황금 ${N}종 완성, 배분 +${K.dexPt}pt` : "");
}
function drawDex(){
  const d = dexOf(save), all = N * THEMES.length;
  const seen = Object.values(d).filter(v => v === 1 || v === 2).length, rare = dexRareAll(save);
  $("dexCount").textContent = `${seen}/${all} · 황금 ${rare}/${all}`;
  $("dexNews").innerHTML = dexNews
    ? `<div class="banner" style="color:var(--gold);border-color:var(--gold)">${dexNews}</div>` : "";
  $("dex").innerHTML = THEMES.map((t, ti) => {
    const got = dexRare(save, ti);
    // 윗줄 = 이겨 본 보스, 아랫줄 = 잡은 황금 개체. 못 본 칸은 물음표로 가린다
    const line = want => SLOTS.map(s => {
      const has = (d[ti + ":" + s.slot] || 0) >= want, b = t[1][s.slot - 1];
      return `<span class="dx${has ? (want === 2 ? " r" : "") : " no"}" title="${
        has ? (want === 2 ? "황금 " : "") + b[0] : "아직 못 봤다"}">${has ? b[1] : "?"}</span>`;
    }).join("");
    // 지금 있는 층이 어느 줄에 찍히는지 적는다. 6층부터는 변종이라 1~5층 줄을 같이 쓴다 —
    // 이걸 안 적어 두니 도감이 5층에서 끝나는 것도, 6층 보스가 어디 찍히는지도 알 길이 없었다.
    const here = (floorNow() - 1) % THEMES.length === ti
      ? ` <span class="gold">← 지금 ${floorNow()}층${floorNow() > THEMES.length ? " (변종)" : ""}</span>` : "";
    return `<div class="row" style="margin-top:10px;font-size:12px"><span>${ti + 1}층 ${t[0]}${here}</span>
        <span class="${got === N ? "gold" : "dim"}">황금 ${got}/${N}${got === N ? ` · 배분 +${K.dexPt}pt` : ""}</span></div>
      <div class="dex">${line(1)}</div><div class="dex">${line(2)}</div>`;
  }).join("") + `<div class="dim" style="font-size:11px;margin-top:10px">
    <b>도감은 ${THEMES.length}층까지, 보스 ${all}종이 끝이다.</b> 변종은 같은 줄에 찍힌다(${THEMES.length + 1}층은 1층 줄).<br>
    보스를 이기면 윗줄, <span class="gold">황금 개체</span>(${+(K.rareRate * 100).toFixed(2)}% 확률)를 잡으면 아랫줄에 찍힌다.
    황금 하나마다 혼 +${K.dexSoul}% (지금 +${+dexSoul().toFixed(1)}%), 한 층을 다 채우면 배분 +${K.dexPt}pt.</div>`;
}

// ── 변이 내성: 조각으로 변이를 한 레벨씩 지운다. 한 겹에 K.mutLv 레벨이고, 닿은 깊이만큼만 올릴 수 있다
// (6~10층에 닿았으면 한 겹, 11층부터 두 겹). 변종 층이 열리기 전에는 안내만 한다.
const mutLaps = () => Math.max(0, Math.ceil((Math.floor(((save.best || 0)) / N) + 1 - THEMES.length) / MUTS.length));
const mutCap = () => mutLaps() * K.mutLv;        // 지금 올릴 수 있는 내성의 끝
function buyRes(id, cnt){
  for (let i = 0; i < cnt; i++) {
    const lv = resOf(id), c = shardCost(lv);
    if (lv >= mutCap() || (save.shards | 0) < c) break;
    save.shards -= c; save.res = {...(save.res || {}), [id]: lv + 1};
  }
  put(); drawAll();
}
function drawMuts(){
  const here = floorNow(), cur = mutOf((here - 1) * N + 1);
  // 던전 카드 맨 위: 지금 층의 변이. 사라졌으면 사라졌다고 적는다
  $("mutNote").innerHTML = !cur ? "" : mutPow((here - 1) * N + 1) > 1
    ? `<div class="wall" style="margin:0 0 8px">${cur.emoji} 변이 <b>${cur.name}</b> — ${mutText(cur, mutPow((here - 1) * N + 1))}.
       조각으로 내성을 올리면 약해진다.</div>`
    : `<div class="banner" style="margin:0 0 8px">${cur.emoji} 변이 ${cur.name} — 내성으로 지웠다.</div>`;
  $("mutHead").textContent = mutLaps() || (save.shards | 0) ? `조각 ${n(save.shards | 0)}` : "";
  $("mutNews").innerHTML = shardNews ? `<div class="banner">${shardNews}</div>` : "";
  if (!mutLaps()) {
    $("muts").innerHTML = "";
    $("mutHelp").innerHTML = `<div class="dim">${THEMES.length}층을 다 깨면(${N * THEMES.length}스테이지) 변종 층이 열린다.
      변종 보스는 특수한 능력(변이)을 갖는다. 반복 전투에서 나오는 조각으로 지운다.</div>`;
    return;
  }
  const cap = mutCap();
  $("muts").innerHTML = MUTS.map((m, i) => {
    const lv = resOf(m[0]), c = shardCost(lv), full = lv >= cap, can = !full && (save.shards | 0) >= c;
    // 지금 지우고 있는 겹만 적는다 — 이미 지운 겹은 볼 일이 없다
    const layer = Math.min(mutLaps(), Math.floor(lv / K.mutLv) + 1);
    const fl = THEMES.length + 1 + i + MUTS.length * (layer - 1);       // 그 겹이 붙는 층
    return `<span>${m[2]} ${m[1]} <small>${full ? `${fl}층까지 다 지웠다`
        : `${fl}층 — ${m[4].replace("{0}", +Math.pow(m[3], layer - lv / K.mutLv).toFixed(2))}`}</small></span>
      <b class="soul">Lv.${lv}/${cap}</b>
      <span class="dim" style="font-size:11px;text-align:right;white-space:nowrap">${full ? "" : n(c) + "조각"}</span>
      <button data-m="${m[0]}" ${can ? "" : "disabled"}>${full ? "끝" : "내성 +1"}</button>`;
  }).join("");
  $("muts").querySelectorAll("button[data-m]").forEach(b => b.onclick = () => buyRes(b.dataset.m, 1));
  $("mutHelp").innerHTML = `<div class="dim" style="font-size:11px;margin-top:8px">
    변종 보스를 이기면 ${+(K.shardRate * 100).toFixed(1)}% 확률로 조각이 나온다. 내성 ${K.mutLv}레벨마다 변이가 한 겹 사라진다.
    <b>층의 마지막 보스는 그 층의 변이를 다 지워야 싸울 수 있다.</b></div>`;
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
  add("도감", dexSoul());
  add("업적", achSoul());
  $("rbBonus").innerHTML = parts.length
    ? `<span class="dim">얻는 혼</span> <span class="soul">x${mul.toFixed(2)}</span>
       <span class="dim">(${parts.join(" · ")})</span>` : "";
}

// 업적마다 지금 값. 이미 저장에 있는 기록으로 셀 수 있는 것은 업데이트하자마자 달성된다
const achCur = kind => {
  const t = stOf(), rs = save.relics || {};
  switch (kind) {
    case "floor": return Math.ceil((save.best || 0) / N);
    case "reb": return save.rebirths | 0;
    case "trans": return transOf(save);
    case "kill": return t.kills | 0;
    case "gate": return Math.max(0, Math.floor((save.best || 0) / N) - THEMES.length);
    case "dex": return Object.values(dexOf(save)).filter(v => v === 1 || v === 2).length;
    case "gold": return dexRareAll(save);
    case "relic": return SLOTS.filter(sl => relicOk(rs[relicKey(sl)])).length;
    case "myth": return SLOTS.some(sl => relicOk(rs[relicKey(sl)]) && rs[relicKey(sl)].r === RARITY.length - 1) ? 1 : 0;
    case "streak": return Math.max(t.streakMax | 0, streak());
    case "raid": return t.raids | 0;
    case "raidrun": return t.raidRunMax | 0;
    case "jackpot": return t.jackpot | 0;
    case "gold2": return t.gold2 | 0;
    case "night": return t.night | 0;
    case "lose": return t.losses | 0;
  }
  return 0;
};
let achNews = "";
function checkAch(){
  const got = achOf(save), now = [];
  stOf().streakMax = Math.max(stOf().streakMax | 0, streak());
  for (const [id, , kind, goal, title] of ACH)
    if (!got[id] && achCur(kind) >= goal) { got[id] = daysBack(0); now.push(title); }
  if (!now.length) return false;
  save.ach = got;
  achNews = `업적 달성 — 칭호 ${now.map(x => `「${x}」`).join(" ")} · 얻는 혼 +${+(K.achSoul * now.length).toFixed(1)}%`;
  return true;
}
function drawAch(){
  const got = achOf(save), done = ACH.filter(a => got[a[0]]).length;
  $("achHead").textContent = `${done}/${ACH.length} · 혼 +${+achSoul().toFixed(1)}%`;
  const ti = ACH.find(a => a[0] === save.title && got[a[0]]);
  $("achTitle").textContent = ti ? `「${ti[4]}」` : "";
  $("achNews").innerHTML = achNews ? `<div class="banner" style="color:var(--gold);border-color:var(--gold)">${achNews}</div>` : "";
  const groups = [...new Set(ACH.map(a => a[1]))];
  $("ach").innerHTML = groups.map(gr => `<div class="dim" style="font-size:11px;margin:10px 0 2px">${gr}</div>` +
    ACH.filter(a => a[1] === gr).map(([id, , kind, goal, title, desc, hidden]) => {
      const ok = !!got[id], cur = Math.min(achCur(kind), goal);
      if (hidden && !ok) return `<div class="row" style="font-size:12px;padding:3px 0"><span class="dim">🔒 ??? — 숨은 업적</span></div>`;
      return `<div class="row" style="font-size:12px;padding:3px 0;align-items:center;gap:8px">
        <span style="flex:1;min-width:0">${ok ? "🏅" : "▫️"} <b${ok ? ' class="gold"' : ""}>${title}</b>
          <small class="dim" style="display:block">${desc}${ok ? ` · ${got[id]}` : goal > 1 ? ` · ${n(cur)}/${n(goal)}` : ""}</small></span>
        ${ok ? `<button data-t="${id}" class="${save.title === id ? "on" : ""}">${save.title === id ? "칭호 해제" : "칭호로"}</button>` : ""}</div>`;
    }).join("")).join("") +
    `<div class="dim" style="font-size:11px;margin-top:8px">업적 하나마다 얻는 혼 +${K.achSoul}%. 칭호는 하나 골라 이름 위에 단다.</div>`;
  $("ach").querySelectorAll("button[data-t]").forEach(b => b.onclick = () => {
    save.title = save.title === b.dataset.t ? "" : b.dataset.t; put(); drawAch();
  });
}

const drawAll = () => { if (fixCrit()) put(); if (checkAch()) put(); drawHero(); drawRbBonus(); drawMissions(); drawRaid(); drawSlot(); drawExped(); drawStages(); drawRelics(); drawDex(); drawAch(); drawMuts(); };

// ── 전투: 턴제 자동. 선공은 SPD, 치명타는 thinking 토큰에서 온다.
// DEF 는 비율로 깎는다 — 같은 자리수면 절반이다. 빼기이던 시절엔 보스 ATK 가 백만 단위라
// 천 단위 DEF 가 아무 일도 못 했고, 뚫고 못 뚫고의 벼랑만 있었다. 파이썬 mitigate() 와 같은 식.
const dmgOf = (a, d, crit) => {
  const p = a.atk * (0.85 + Math.random()*0.3) * (crit ? a.cdmg / 100 : 1);
  return Math.max(1, Math.round(p * p / (p + d.dfn) / (a.div || 1)));      // div = 철갑 변이
};
const newFighters = b => mutate([{hp:F("hp"), max:F("hp"), atk:F("atk"), dfn:F("dfn"), crit:F("crit"), cdmg:F("cdmg")},
                                  {hp:b.hp, max:b.hp, atk:b.atk, dfn:b.dfn}], b.g);
// 변이를 두 싸움꾼에게 건다. beatable() 과 파이썬 turns_to_win() 이 같은 규칙을 쓴다
function mutate(pair, g){
  const [me, foe] = pair, id = mutId(g), k = mutPow(g);
  if (id === "armor") me.div = k;                           // 내가 주는 피해 ÷k
  // 저주(내 SPD ÷k)는 spdVs() 가 맡는다 — 선공과 2타가 그 값을 쓴다
  if (id === "fury") { foe.hp *= k; foe.max *= k; }         // 거대화: 보스 HP xk
  if (id === "pierce") me.crit /= k;                        // 실명: 내 치명타 확률 ÷k
  if (id === "numb") me.cdmg = Math.max(100, me.cdmg / k);  // 내 치명타 피해 ÷k
  return pair;
}
// 변이 조각: 변종 층의 보스를 이기면 가끔 나온다. 반복 전투가 주된 길이다 — 켜 둔 시간이 내성이 된다.
// 혼이 아니라 '변이를 지우는 힘' 만 사므로, 다 모아도 변이가 없던 때보다 빨라지지 않는다 (selftest 가 붙든다).
let shardNews = "";
const rollShard = g => { if (!mutOf(g) || Math.random() >= K.shardRate) return false;
  save.shards = (save.shards | 0) + 1; return true; };
// 승리 처리 (수동·자동 공통). 유물이 나오면 그 안내 문구를 돌려준다.
const winTally = () => { tally("kills"); const h = new Date().getHours(); if (h >= 3 && h < 5) stOf().night = 1; };
const winStage = g => {
  winTally();
  const first = g > (save.best || 0);             // 역대 처음 깬 스테이지
  if (!save.cleared.includes(g)) save.cleared.push(g);
  markBest(g);
  if (!dexOf(save)[dexKey(g)]) save.dex = {...dexOf(save), [dexKey(g)]: 1};     // 도감 윗줄
  const drop = rollRelic(g, slotOf(g), first);
  if (drop) relicNews = drop;
  put();
  return drop;
};

// 연출 없는 즉시 전투 (자동 도전용). 규칙은 fightStart 와 같다.
function quickFight(b){
  const [me, foe] = newFighters(b);
  let myTurn = goesFirst(b);
  for (let turn = 1; turn <= 200; turn++) {
    if (myTurn) foe.hp -= turnDmg(me, foe, hitsOf(b), me.crit)[0];
    else me.hp -= dmgOf(foe, me, false);
    if (foe.hp <= 0) return true;
    if (me.hp <= 0) return false;
    myTurn = !myTurn;
  }
  return false;
}

let timer = null, fighting = false;
function fightStart(g, slot, b){
  if (gated(g)) return;
  clearInterval(timer); fighting = true;
  fightRare = rollRare();                        // 황금 개체가 대신 나왔다 — 이름과 이모지만 다르다
  if (fightRare) slot = rareOf(slot);
  const [me, foe] = newFighters(b);
  $("fh").textContent = tierOf(lvNow())[1]; $("fb").textContent = slot.emoji; $("fbn").textContent = slot.name;
  $("log").innerHTML = ""; $("close").disabled = true; $("close").textContent = "전투 중…";
  $("fight").classList.add("on");
  let turn = 0, myTurn = goesFirst(b);
  say(`${slot.emoji} ${g}스테이지 — ${slot.name} 등장!`
      + (xtra(b) ? ` <span class="dim">한 턴에 ${(1 + xtra(b)).toFixed(1)}회</span>` : ""));
  if (fightRare) say(`<span class="gold">황금 개체다 — 이기면 도감에 오른다.</span>`);
  if (mutId(g)) say(`<span class="lose">${mutOf(g).emoji} 변이 ${mutOf(g).name} — ${mutText(mutOf(g), mutPow(g))}</span>`);
  paint(me, foe);
  timer = setInterval(() => {
    if (++turn > 200) return end(false, me, foe, g, slot, "소모전 — 화력이 부족하다");
    const [a, d, an, tag] = myTurn ? [me, foe, "나", "fb"] : [foe, me, slot.name, "fh"];
    // 내 턴은 타격 수만큼 몰아 친다 — 한 줄에 합쳐 적는다. 스무 번을 스무 줄로 쓰면 못 읽는다.
    const hits = myTurn ? hitsOf(b) : 1;
    const [dmg, crit] = turnDmg(a, d, hits, myTurn ? me.crit : 0);
    d.hp -= dmg;
    say(`T${turn} ${an}${hits > 1 ? ` <span class='gold'>x${n(hits)}타</span>` : ""} → ${n(dmg)} 피해`
        + (crit ? " <span class='crit'>치명타!</span>" : ""), crit);
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
  clearInterval(timer); fighting = false; paint(me, foe);
  if (won) {
    say(`<span class="win">승리! ${slot.name} 정복. 혼 ${n(soulOf(g))} 예약.</span>`);
    const drop = winStage(g);
    if (drop) say(`<span class="gold">${drop}</span>`);
    if (fightRare) { dexNews = catchRare(g); put(); say(`<span class="gold">${dexNews}</span>`); }
    if (rollShard(g)) { put(); say(`<span class="soul">변이 조각 +1 (보유 ${n(save.shards)})</span>`); }
    if (g % N === 0) say(`<span class="win">${g/N}층 완주 — ${g/N+1}층 ${slotOf(g+1).theme} 이 열렸다.</span>`);
    say(`<span class="dim">혼은 환생할 때 정산된다.</span>`);
  } else {
    say(`<span class="lose">패배.</span> <span class="dim">${why}</span>`); tally("losses"); put();
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

// 새 버전 알림. 확인은 서버(파이썬)가 하고 결과를 UPDATE_TTL(60초) 동안 재사용한다 — 페이지가 직접 바깥으로 나가지 않는다.
if (SERVED) fetch("update", {cache: "no-store"}).then(r => r.json()).then(u => {
  if (!u || !u.newer) return;
  // 어느 설치 방식이든 서버가 알아서 올린다 — 앱은 DMG 를 직접 받아 번들을 갈아끼운다.
  // 링크(target=_blank)로 두면 메뉴 막대 팝오버(WKWebView)가 새 창을 못 띄워 눌러도 무반응이다.
  const isApp = u.kind === "app";
  $("updBanner").innerHTML = `<div class="banner" style="color:var(--gold);border-color:var(--gold);
    display:flex;align-items:center;gap:10px;justify-content:space-between;flex-wrap:wrap">
    <span id="updMsg">새 버전 ${u.latest} — 지금은 ${u.current}${
      u.failed ? `<br><small style="color:var(--hp)">지난 업데이트 실패: ${u.failed}</small>` : ""}</span>
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

// 자동 전투는 1초에 한 판이다. 메뉴 막대 창을 닫거나 Mac 이 잠들면 macOS 가 이 타이머를 멈춘다.
// 그래서 지난 판 이후 흐른 시간만큼 밀린 판을 친다 — 한 번에 AUTO_BATCH 판씩, 원정대와 같은 상한까지.
// 수동 전투가 연출 중일 때만 쉬고, 그동안 밀린 판도 끝나면 따라잡는다.
const AUTO_BATCH = 600;
let autoAt = Date.now();
setInterval(() => {
  const now = Date.now();
  if (!save.auto || !save.rebirths) autoAt = now;              // 꺼 둔 시간은 쌓지 않는다
  autoAt = Math.max(autoAt, now - CAP_S * 1000);
  if (!fighting) {
    const k = Math.min(AUTO_BATCH, Math.floor((now - autoAt) / 1000));
    if (k > 1) {
      quiet = true;
      try { for (let i = 0; i < k; i++) autoStep(); } finally { quiet = false; }
      if (dirty) { dirty = false; put(); }
      drawAll();
      const left = Math.floor((now - autoAt) / 1000) - k;
      if (left > 0) autoSay(`밀린 전투 ${n(left)}판 따라잡는 중`);
    } else if (k === 1) autoStep();
    autoAt += k * 1000;
  }
  if (!$("fight").classList.contains("on")) drawExped();
}, 1000);

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
UPDATE_TTL = 60              # 초. 확인 결과를 이만큼만 재사용한다. 0 으로 두면 팝오버를 여닫을 때마다
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


def _latest_tag():
    """최신 릴리스의 태그. 릴리스 페이지(/releases/latest)가 /releases/tag/vX.Y.Z 로 넘기는 것을 읽는다.

    예전엔 api.github.com 에 물었다. 로그인 없이는 한 IP 에서 시간당 60번이라, 사무실처럼 여럿이 한 IP 를
    쓰면 금방 막혔다 — 알림은 예전에 본 태그로 뜨는데 버튼을 누르면 DMG 주소를 못 얻어 실패했다.
    웹 주소에는 그 제한이 없다. 막히면 API 로 한 번 더 본다."""
    ua = {"User-Agent": f"token-rpg/{__version__}"}
    try:
        req = urllib.request.Request(f"https://github.com/{REPO}/releases/latest", headers=ua, method="HEAD")
        with urllib.request.urlopen(req, timeout=4) as r:
            url = r.geturl()
        if "/releases/tag/" in url:
            return urllib.parse.unquote(url.rsplit("/releases/tag/", 1)[1].split("?")[0].strip("/"))
    except Exception:
        pass
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                 headers={**ua, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.load(r).get("tag_name") or ""


def update_check(cfg=None, force=False):
    """{current, latest, url, kind, cmd, newer}. 네트워크가 막히면 latest 가 없다."""
    cfg = cfg if cfg is not None else load_config()
    seen = cfg.get("update") or {}
    fresh = not force and time.time() - (seen.get("at") or 0) < UPDATE_TTL
    if not fresh and cfg.get("updateCheck", True):
        try:
            seen = {"at": time.time(), "tag": _latest_tag()}
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
            "failed": update_failed(),
            "url": f"https://github.com/{REPO}/releases/latest",
            "cmd": " ".join(cmd[0]) if cmd else "",
            "newer": bool(tag) and _ver(tag) > _ver(__version__)}


def app_bundle(path=None):
    """이 사본을 담고 있는 .app 경로. 앱 안에서 도는 게 아니면 None."""
    here = os.path.abspath(path or __file__)
    i = here.find(".app" + os.sep + "Contents" + os.sep)
    return here[:i + 4] if i > 0 else None


DMG_NAME = "Token-RPG-macOS.dmg"      # scripts/build-macos-app.sh 가 만드는 이름과 같아야 한다


def _dmg_url():
    """최신 릴리스에 붙은 DMG 주소. CI(macos-dmg.yml)가 릴리스마다 같은 이름으로 올려 둔다.
    GitHub 이 /releases/latest/download/<이름> 을 최신 릴리스의 파일로 넘겨 준다 — API 를 안 거친다."""
    return f"https://github.com/{REPO}/releases/latest/download/{DMG_NAME}"


def _update_result():
    return os.path.join(data_dir(), "update.result")


def update_failed():
    """지난 업데이트가 실패했으면 그 이유, 아니면 "". 갈아끼우는 셸은 앱이 닫힌 뒤에 돌아서 화면에
    아무 말도 못 한다 — 결과를 파일에 적어 두고 다음에 열 때 읽는다. 성공 기록은 읽으면서 치운다."""
    try:
        with open(_update_result(), encoding="utf-8") as f:
            line = f.read().strip()
    except OSError:
        return ""
    if line.startswith("fail "):
        return line[5:]
    try:
        os.remove(_update_result())
    except OSError:
        pass
    return ""


# 앱은 자기가 도는 동안 자기를 갈아치울 수 없다 -> 앱을 닫고, 바꾸고, 다시 띄우는 일을
# 분리된 셸에 맡긴다. 새 번들을 옆에 펼친 뒤 이름만 바꾸므로 중간에 죽어도 옛 앱이 남는다.
# macOS 는 브라우저로 받은 앱에 '인터넷에서 받음' 딱지(com.apple.quarantine)를 붙인다. 딱지 붙은 앱이
# 받은 DMG 에도 딱지가 옮겨 붙고, 거기서 꺼낸 새 앱도 물려받는다 — 서명·공증이 없는 앱이라 macOS 가
# 새 앱 실행을 막는다. 버튼을 누르면 앱이 닫히고 다시 안 떴다. 그래서 갈아끼우기 전에 딱지를 지운다.
# 우리가 릴리스에서 직접 받은 파일이라 지워도 된다. 과정은 LOG 에, 결과는 RES 에 한 줄로 남긴다.
_SWAP_SH = r"""
APP="$1"; DMG="$2"; PID="$3"; RES="$4"; LOG="$5"; OPEN="${TOKEN_RPG_OPEN:-open}"
exec >"$LOG" 2>&1
echo "swap $(date) app=$APP pid=$PID"
fail() { echo "fail $1" > "$RES"; echo "FAIL $1"; }
kill -TERM "$PID" 2>/dev/null
n=0; while kill -0 "$PID" 2>/dev/null && [ "$n" -lt 80 ]; do sleep 0.25; n=$((n + 1)); done
/usr/bin/xattr -d com.apple.quarantine "$DMG" 2>/dev/null
MNT=$(hdiutil attach "$DMG" -nobrowse -readonly | grep -o '/Volumes/.*' | tail -1)
NEW=$(find "$MNT" -maxdepth 1 -name '*.app' | head -1)
if [ -z "$NEW" ]; then
  fail "받은 DMG 를 열지 못했다"
else
  rm -rf "$APP.new" "$APP.old"
  if ! ditto "$NEW" "$APP.new"; then
    fail "새 앱을 풀지 못했다 (디스크·권한)"
  else
    /usr/bin/xattr -dr com.apple.quarantine "$APP.new" 2>/dev/null
    if mv "$APP" "$APP.old" && mv "$APP.new" "$APP"; then
      echo "ok" > "$RES"; echo "OK"
    else
      [ -e "$APP" ] || mv "$APP.old" "$APP"
      fail "앱을 바꾸지 못했다 — macOS 가 막았다 (시스템 설정 > 개인정보 보호 및 보안 > 앱 관리)"
    fi
  fi
  rm -rf "$APP.new" "$APP.old"
fi
[ -n "$MNT" ] && hdiutil detach "$MNT" -quiet
rm -f "$DMG"
"$OPEN" "$APP"
"""


def update_app(bundle):
    """DMG 를 직접 받아 번들을 갈아끼운다. 브라우저를 거치지 않으니 quarantine 딱지도
    안 붙는다 — 그 딱지는 서명 유무가 아니라 받은 프로그램(브라우저)이 찍는 것이다."""
    # DMG 안에서 바로 열었거나 다운로드 폴더에서 열면 macOS 가 앱을 읽기 전용 임시 위치에서 돌린다
    if "/AppTranslocation/" in bundle or bundle.startswith("/Volumes/"):
        return False, "앱이 설치되지 않은 채 돌고 있다 — Token RPG 를 Applications 폴더로 옮긴 뒤 다시 열어라"
    if not os.access(os.path.dirname(bundle), os.W_OK) or not os.access(bundle, os.W_OK):
        return False, f"{bundle} 를 고칠 권한이 없다 — 직접 받아 덮어써라"
    try:
        dmg = os.path.join(data_dir(), "update.dmg")
        req = urllib.request.Request(_dmg_url(), headers={"User-Agent": f"token-rpg/{__version__}"})
        with urllib.request.urlopen(req, timeout=120) as r, open(dmg, "wb") as f:
            shutil.copyfileobj(r, f)
    except urllib.error.HTTPError as e:               # 릴리스를 발행하고 DMG 가 붙기까지 1분쯤 걸린다
        return False, ("새 버전의 DMG 가 아직 안 올라왔다 — 1분 뒤에 다시 눌러라" if e.code == 404
                       else f"DMG 를 받지 못했다 — HTTP {e.code}")
    except Exception as e:                            # 네트워크·디스크 — 옛 앱은 그대로다
        return False, f"DMG 를 받지 못했다 — {e}"
    try:
        os.remove(_update_result())
    except OSError:
        pass
    # 부모가 곧 메뉴 막대 앱이다(앱이 serve 를 띄운다). 스크립트는 우리가 죽어도 살아야 하므로
    # 새 세션으로 떼어 놓는다 — 앱을 닫는 순간 이 파이썬도 같이 죽는다.
    subprocess.Popen(["/bin/sh", "-c", _SWAP_SH, "swap", bundle, dmg, str(os.getppid()),
                      _update_result(), os.path.join(data_dir(), "update.log")],
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
    # 갱신 버튼은 "지금 것으로 맞춰라"는 뜻이다 — 확인 캐시(UPDATE_TTL)가 남아 새 릴리스가 안 보이면
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
    sub.add_parser("selftest", help="집계·병합·밸런스·게임 화면 자체 검증")
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
        ok, msg = _real_update_app("/nonexistent/Token RPG.app")     # update_app 은 위에서 바꿔 끼운 가짜다
        assert not ok and "권한" in msg, msg
        # 설치하지 않고 연 앱(DMG 안 · 임시 위치)은 옮기라고 알린다
        for where in ("/Volumes/Token RPG/Token RPG.app", "/private/var/folders/x/T/AppTranslocation/A1/d/Token RPG.app"):
            ok, msg = _real_update_app(where)
            assert not ok and "Applications" in msg, msg
        assert _dmg_url() == f"https://github.com/{REPO}/releases/latest/download/{DMG_NAME}"
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "build-macos-app.sh"),
                  encoding="utf-8") as f:
            assert DMG_NAME in f.read(), "빌드 스크립트가 만드는 DMG 이름이 DMG_NAME 과 다르다 — 업데이트가 404 를 받는다"
        if sys.platform == "darwin" and shutil.which("hdiutil"):
            _check_swap()
    finally:
        globals()["install_kind"] = real_kind
        globals()["app_bundle"] = real_bundle
        globals()["update_app"] = _real_update_app
        webbrowser.open = real_open


def _check_swap():
    """갈아끼우는 셸을 진짜 DMG 로 돌려 본다. 딱지(quarantine) 붙은 DMG 에서 꺼낸 앱에 딱지가 남으면
    macOS 가 새 앱을 안 띄운다 — 브라우저로 받아 설치한 사람은 업데이트 버튼이 앱을 없애 버렸다."""
    import tempfile
    q = "com.apple.quarantine"
    xa = lambda *a: subprocess.run(["/usr/bin/xattr", *a], capture_output=True, text=True)
    with tempfile.TemporaryDirectory() as d:
        app, src = os.path.join(d, "inst", "Token RPG.app"), os.path.join(d, "src", "Token RPG.app")
        for root, ver in ((app, "old"), (src, "new")):
            os.makedirs(os.path.join(root, "Contents", "Resources"))
            with open(os.path.join(root, "Contents", "Resources", "v.txt"), "w") as f:
                f.write(ver)
        dmg, res, log, opened = (os.path.join(d, x) for x in ("u.dmg", "res", "log", "opened"))
        r = subprocess.run(["hdiutil", "create", "-volname", "TRPG selftest", "-srcfolder", os.path.dirname(src),
                            "-ov", "-format", "UDZO", dmg], capture_output=True, text=True)
        if r.returncode:                                  # 디스크 이미지를 못 만드는 환경 — 건너뛴다
            return
        xa("-w", q, "0083;00000000;Safari;", dmg)
        xa("-w", q, "0083;00000000;Safari;", app)
        opener = os.path.join(d, "open.sh")
        with open(opener, "w") as f:
            f.write(f'#!/bin/sh\necho "$1" > "{opened}"\n')
        os.chmod(opener, 0o755)
        sleeper = subprocess.Popen(["sleep", "30"])
        try:
            swap = subprocess.Popen(["/bin/sh", "-c", _SWAP_SH, "swap", app, dmg, str(sleeper.pid), res, log],
                                    env={**os.environ, "TOKEN_RPG_OPEN": opener})
            sleeper.wait(timeout=20)          # 셸이 앱(여기서는 sleep)을 닫는다 — 거둬 줘야 죽은 것으로 보인다
            swap.wait(timeout=120)
        finally:
            sleeper.kill(); sleeper.wait()
        note = open(log, encoding="utf-8").read() if os.path.exists(log) else ""
        assert open(res).read().strip() == "ok", "갈아끼우기 실패\n" + note
        assert open(os.path.join(app, "Contents", "Resources", "v.txt")).read() == "new", "옛 앱이 그대로다\n" + note
        assert q not in xa("-lr", app).stdout, "새 앱에 '인터넷에서 받음' 딱지가 남았다 — macOS 가 실행을 막는다"
        assert open(opened).read().strip() == app and not os.path.exists(dmg), "앱을 다시 띄우지 않았거나 DMG 가 남았다"
        assert not os.path.exists(app + ".old") and not os.path.exists(app + ".new"), "찌꺼기가 남았다"
        # 못 여는 DMG 면 옛 앱을 그대로 두고 이유를 남긴다 — 다음에 열 때 화면이 읽는다
        with open(dmg, "w") as f:
            f.write("not a dmg")
        subprocess.run(["/bin/sh", "-c", _SWAP_SH, "swap", app, dmg, "999999", res, log],
                       env={**os.environ, "TOKEN_RPG_OPEN": opener}, timeout=120)
        assert open(res).read().startswith("fail ") and os.path.isdir(app), open(res).read()


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


def _check_themes():
    """층 테마: 이름과 이모지만 바뀐다. 칸 수가 어긋나면 어느 층에서 화면이 죽고, 이름이 겹치면
    유물 도감과 전투 로그에서 어느 층 보스인지 알 수 없다."""
    n = len(AFFIXES)
    assert n == 15 and all(AFFIXES.count(a) == 3 for a in set(AFFIXES)), "유물 능력치 5종이 3칸씩이 아니다"
    names = [b[0] for _, bosses in THEMES for b in bosses]
    for theme, bosses in THEMES:
        assert len(bosses) == n, f"테마 '{theme}' 의 보스가 {len(bosses)}종이다 — {n}종이어야 한다"
        assert all(name.strip() and emoji.strip() for name, emoji in bosses), f"테마 '{theme}' 에 빈 이름·이모지가 있다"
    assert len(set(names)) == len(names), f"보스 이름이 겹친다: {sorted(x for x in set(names) if names.count(x) > 1)}"
    assert len({t for t, _ in THEMES}) == len(THEMES), "테마 이름이 겹친다"
    assert [(b["name"], b["emoji"], b["affix"]) for b in dungeons()] == BOSSES, "1층이 THEMES[0] 과 다르다"
    # 테마가 바뀌어도 칸 고정값은 그대로다 — 유물 능력치가 층마다 달라지면 저장의 유물 키가 틀어진다
    for g in range(1, n * (2 * len(THEMES) + 1) + 1):
        s = slot_of(g)
        assert s["slot"] == (g - 1) % n + 1 and s["affix"] == AFFIXES[(g - 1) % n], (g, s)
    assert slot_of(1)["name"] == "버그 벌레" and slot_of(n + 1)["theme"] == THEMES[1][0]
    lap = n * len(THEMES)
    assert slot_of(lap + 1)["name"] == "변종 버그 벌레" and slot_of(2 * lap + 1)["name"] == "변종2 버그 벌레"
    # 도감: 다시 잡은 황금이 혼을 주면 켜 두기만 해도 혼이 쌓인다 (유물 중복을 묶어 둔 것과 같은 이유).
    # 화면 검증이 '두 번째 포획은 혼 0' 을 실제로 돌려 본다. 여기서는 숫자의 테두리만 본다.
    assert 0 < RARE_RATE <= 0.002, f"황금 확률 {RARE_RATE} — 자동 반복이 1초에 한 판이라 0.2% 를 넘으면 한 층이 두 시간 안에 찬다"
    assert DEX_SOUL * n * len(THEMES) <= 50, "도감을 다 채운 혼 보너스가 +50% 를 넘는다 — 수확 특성을 밀어낸다"
    assert DEX_PT <= TRAIT_PT, "한 층 완성 보상이 각성 1레벨보다 크다"
    assert ACH_SOUL * len(ACHS) <= 10, "업적을 다 모은 혼 보너스가 +10% 를 넘는다 — 층 진입이 15% 넘게 당겨진다"
    assert len({a[0] for a in ACHS}) == len(ACHS) and len({a[4] for a in ACHS}) == len(ACHS), "업적 id 나 칭호가 겹친다"
    assert RARE_MUL <= 1, "황금 한 마리가 보스 한 판보다 많이 준다 — 한 층 15칸이라 환생 사이 수입이 환생을 넘는다"
    assert set(boss(1)) == {"hp", "atk", "dfn", "spd"}, "보스 능력치에 테마가 섞였다 — 밸런스는 스테이지 번호만 본다"


def _check_muts():
    """변이와 변이 조각. 지켜야 할 것은 넷이다.

    (1) 변이는 언제나 불이익이다 — 내성을 아무리 올려도 변이가 없던 때보다 쉬워지지 않는다. 조각은 반복 전투
        (토큰이 안 드는 시간)로 벌기 때문에, 덤이 생기면 켜 두기만 해서 환생 기운 제한을 돈다.
    (2) 다섯 변이의 무게가 비슷하다 — 하나만 가벼우면 그 층은 조각을 캘 이유가 없고, 하나만 무거우면 그 층이 벽이다.
    (3) 변종 층의 마지막 보스만 관문이다 — 그 층의 변이를 다 지워야 넘는다. 나머지 14스테이지는 조각 없이 간다
        (_check_balance 의 속도 검사는 관문을 세지 않고 내성 없이 돈다).
    (4) 관문 하나는 '몇 시간' 의 일이다 — 몇 분이면 장애물이 아니고, 꼭 넘어야 하는 것이 며칠이면 못 한다."""
    n, nt = len(AFFIXES), len(THEMES)
    assert all(mut_of(g) is None for g in range(1, n * nt + 1)), "1~5층에 변이가 붙었다"
    assert [mut_of(n * (nt + i) + 1) for i in range(len(MUTS) + 1)] == \
        [(m[0], 1) for m in MUTS] + [(MUTS[0][0], 2)], "변이가 6층부터 차례로 붙고 11층에서 두 겹이 되지 않는다"
    assert len({m[0] for m in MUTS}) == len(MUTS) and all(m[3] > 1 and "{0}" in m[4] for m in MUTS), "변이 표가 어긋났다"
    h = hero(BALANCE_HEROES[1][1])
    alloc = {"atk": 20, "hp": 10, "dfn": 10, "crit": 5, "cdmg": 5, "spd": 5}
    tr = {"atk": 100, "hp": 100, "dfn": 88, "cdmg": 75}
    for i, m in enumerate(MUTS):
        g = n * (nt + i) + 5                     # 이 변이가 붙는 층 — 배수는 여기서 읽는다
        b = boss(80)                             # 무게는 이 빌드가 막히는 깊이(80스테이지 언저리)에서 잰다.
        al = alloc
        if m[0] == "curse":                      # 저주는 SPD 를 깎는다 — 보스의 두 배로 빠른(늘 2타) 빌드에서 잰다
            need = 2 * b["spd"] / (1 + SPD_TRAIT * tr.get("spd", 0)) - h["spd"]
            al = {**alloc, "spd": math.ceil(need / GAIN["spd"])}
            assert final_stats(h, al, tr)["spd"] >= 2 * b["spd"]
        if m[0] == "pierce":                     # 실명은 치명타 확률을 깎는다 — 치명타 100% 빌드에서 잰다
            al = {**alloc, "crit": math.ceil((CRIT_CAP - h["crit"]) / GAIN["crit"])}
        k0, d0 = turns_to_win(h, b, al, tr)      # 빌드보다 한참 깊은 데서 재면 DEF 가 이미 무의미해 관통이 0 으로 나온다
        seen = []
        for lv in (0, 3, MUT_LV, MUT_LV + 5, 3 * MUT_LV):
            mid, power = mut_power(g, {m[0]: lv})
            assert power >= 1, f"{m[1]} 내성 {lv}레벨: 배수 {power} — 1 아래면 변이가 덤이 된다"
            k, d = turns_to_win(h, b, al, tr, (mid, power))
            assert k >= k0 - 1e-9 and d <= d0 + 1e-9, f"{m[1]} 내성 {lv}레벨에서 변이가 없을 때보다 쉽다"
            seen.append((d / k) / (d0 / k0))
        assert seen == sorted(seen) and abs(seen[2] - 1) < 1e-9 and abs(seen[-1] - 1) < 1e-9, \
            f"{m[1]}: 내성을 올릴수록 약해지다가 {MUT_LV}레벨에서 사라져야 한다 — {seen}"
        # 철갑·거대화는 정확히 1/k, 무감각·실명은 치명타에 기댄 만큼, 저주는 2타를 잃어 1/2 이다
        # v0.19.8 에서 세기를 5 로 올려 철갑·거대화는 정확히 1/5 다. 아래로 1/7 까지 둔다
        assert 0.14 <= seen[0] <= 0.6, f"{m[1]}의 무게가 {seen[0]:.2f} — 다섯 변이는 여유를 1/7~3/5 로 줄여야 한다"
    assert mut_power(n * (nt + len(MUTS)) + 1, {MUTS[0][0]: MUT_LV})[1] == MUTS[0][3], "두 겹째는 내성 10레벨로 한 겹만 지워져야 한다"
    for lap, (lo, hi) in enumerate(((1.5, 3), (2.5, 5)), 1):
        hours = sum(shard_cost(l) for l in range((lap - 1) * MUT_LV, lap * MUT_LV)) / SHARD_RATE / 3600
        assert lo <= hours <= hi, f"{lap}겹째 관문이 반복 전투 {hours:.1f}시간이다 — {lo}~{hi}시간이어야 한다"
    gates = [g for g in range(1, n * (nt + 2 * len(MUTS)) + 1) if gated(g)]
    assert gates == list(range(n * (nt + 1), n * (nt + 2 * len(MUTS)) + 1, n)), f"관문이 변종 층의 마지막 보스가 아니다 — {gates}"
    for g in gates:
        mid, lap = mut_of(g)
        assert gated(g, {mid: lap * MUT_LV - 1}) and not gated(g, {mid: lap * MUT_LV}), f"{g}스테이지 관문이 내성 {lap * MUT_LV}레벨에서 안 열린다"
        assert mut_power(g, {mid: lap * MUT_LV})[1] == 1, "관문이 열렸는데 변이가 남았다"
    assert all(shard_cost(l) < shard_cost(l + 1) for l in range(3 * MUT_LV)), "내성 값이 오르지 않는다"


def _mut_walls(h):
    """변이가 걸린 층에서 '어디서 막히는가' 를 화면과 맞대 볼 값. 내성에 따라 벽이 달라져야 한다.
    두 빌드를 쓴다 — 벽이 6층(철갑)에 서는 것과 8층(거대화)에 서는 것."""
    alloc = {"atk": 20, "hp": 10, "dfn": 10, "crit": 5, "cdmg": 5, "spd": 5}
    out = []
    for tr in ({"atk": 100, "hp": 100, "dfn": 88, "cdmg": 75, "soul": 20, "pt": 10},
               {"atk": 140, "hp": 140, "dfn": 120, "cdmg": 105, "soul": 20, "pt": 10}):
        cases = []
        for res in ({}, {"armor": 4}, {m[0]: MUT_LV for m in MUTS}, {m[0]: 2 * MUT_LV for m in MUTS}):
            g = 1
            while g < 400:
                if not wins(h, boss(g), alloc, tr, mut_power(g, res)) or gated(g, res):
                    break
                g += 1
            cases.append([res, g])
        out.append({"alloc": alloc, "traits": tr, "cases": cases})
    return out


def _check_js():
    """화면 쪽(JS)을 node 로 실제로 돌린다 — tests/game_smoke.js 가 가짜 DOM 위에서 버튼을 눌러 한 바퀴 돈다.

    예전 selftest 는 파이썬 식만 계산하고 화면은 문자열이 있는지만 봤다. 그 틈으로 v0.14.4(선언
    순서, 화면 전체 공백)와 v0.14.5(없는 상수, NaN 레벨)가 나갔다. 여기서 보는 것은 셋이다 —
    예외가 안 난다, 그린 글자에 NaN 이 없다, 파이썬과 두 벌인 식이 같은 값을 낸다.

    깔린 사본에는 tests/ 가 없으니 조용히 넘어간다. 저장소에서 돌리는데 node 가 없으면
    알리고 넘어가되, CI 에서는 실패시킨다 — 거기서 빠지면 아무도 모른다."""
    import shutil, subprocess, tempfile
    here = os.path.dirname(os.path.abspath(__file__))
    harness = os.path.join(here, "tests", "game_smoke.js")
    if not os.path.exists(harness):
        return
    node = shutil.which("node")
    if not node:
        assert not os.environ.get("CI"), "node 가 없어 화면 검증을 못 돌린다"
        print("node 가 없어 화면(JS) 검증을 건너뛴다", file=sys.stderr)
        return

    today = "2026-01-15"                               # 목요일. 주간 '5일 사용' 은 아직 못 채운 날이다
    day0 = datetime(2026, 1, 15)
    daily = {(day0 - timedelta(days=k)).strftime("%Y-%m-%d"): {"claude-code": 1_500_000, "codex": 500_000}
             for k in range(1, 10)}
    daily[today] = {"claude-code": 7_000_000, "codex": 1_000_000}    # 이번 주 합이 지난주와 달라야 레이드 HP 검증이 선다
    agg = {"input": 20_000_000, "output": 6_000_000, "cache_read": 900_000_000,
           "thinking": 1_200_000, "calls": 8_000}
    top = {**agg, "input": 2 * TRANS_EXP + 5_000_000}  # 두 번 초월하고도 남는다
    zero = dict.fromkeys(agg, 0)
    hosts = [["test-pc", "2026-01-15T00:00:00+00:00", 26_000_000]]
    provs = [["claude-code", 20_000_000], ["codex", 6_000_000]]
    page = lambda a, d=None: render(game_data(a, hosts, provs, [], d))

    h = hero(agg)
    builds = []
    for alloc, tr in (({}, {}), ({"atk": 40, "crit": 20}, {}),
                      ({"atk": 20, "hp": 10, "dfn": 10, "spd": 20},
                       {"atk": 10, "hp": 8, "dfn": 6, "cdmg": 5, "spd": 12}),
                      ({"crit": 250, "cdmg": 30}, {"cdmg": 20, "atk": 30, "hp": 30, "dfn": 30}),
                      ({"spd": 60}, {"spd": 40, "hp": 20})):
        g = 1
        while g < 400:
            if not wins(h, boss(g), alloc, tr):
                break
            g += 1
        builds.append({"alloc": alloc, "traits": tr, "stats": final_stats(h, alloc, tr), "wall": g})
    with open(os.path.abspath(__file__), encoding="utf-8") as f:
        script_line = f.read().split("\n<script>\n")[0].count("\n") + 2
    fixture = {
        "today": today, "scriptLine": script_line, "affix": [b[2] for b in BOSSES],
        "pages": {"base": page(agg, daily), "maxed": page(top), "zero": page(zero)},
        "expect": {
            "boss": [[g, boss(g)] for g in list(range(1, 61)) + [100, 150, 250]],
            "exp": [exp_for(lv) for lv in range(1, MAX_LV + 2)],
            "levels": [[e, level_of(e)] for e in (0, 5_459, 5_460, 268_800, 2_211_300, 26_000_000,
                                                  TRANS_EXP - 1, TRANS_EXP, TRANS_EXP * 3)],
            "cost": [trait_cost(lv) for lv in range(0, 121)],
            "slots": [[g, slot_of(g)] for g in list(range(1, 77)) + [90, 150, 151, 165, 226, 300]],
            "mutWall": _mut_walls(h),
            "gate": [[g, res, gated(g, res)] for g in (75, 89, 90, 105, 150, 164, 165, 240)
                     for res in ({}, {"armor": 9}, {"armor": 10, "curse": 10, "numb": 10}, {"armor": 20})],
            "mut": [[g, (mut_of(g) or (None, 0))[0], (mut_of(g) or (None, 0))[1], mut_power(g)[1]]
                    for g in (1, 75, 76, 90, 91, 106, 121, 136, 150, 151, 166, 226, 301)],
            "side": [[g, round(side_base(g) * dungeons()[(g - 1) % len(BOSSES)]["soul"])]
                     for g in list(range(1, 91)) + [120, 150, 200]],
            "soul": [[g, round(soul_base(g) * dungeons()[(g - 1) % len(BOSSES)]["soul"])]
                     for g in list(range(1, 91)) + [120, 150, 200]],
            "hero": {"level": h["level"], "points": h["points"]},
            "builds": builds,
            "trans": [{"trans": n, "level": hero(top, n)["level"], "points": hero(top, n)["points"]}
                      for n in range(3)],
        },
    }
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "fixture.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(fixture, f, ensure_ascii=False)
        r = subprocess.run([node, harness, path], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
    assert r.returncode == 0, "화면(JS) 검증 실패\n" + (r.stderr or r.stdout)


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
    _check_themes()
    _check_muts()
    _check_js()
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

    # 밸런스: 환생 설계가 성립하는지 검증한다. 고정 영웅 여럿으로 늘 돌리고, 이 PC 에 기록이
    # 있으면 그 영웅으로도 한 번 더 본다 (임시 폴더에는 스냅샷이 없어 진짜 폴더 것을 쓴다).
    # 예전엔 이 PC 의 영웅 하나로만 봤다. CI 에는 기록이 없어 통째로 건너뛰었고, 그 뒤에 있던
    # 검사(확률표·설치 시점·game.html 재작성·윈도우 분기)까지 같이 빠졌다.
    ds = dungeons()
    n = len(ds)
    for g in range(1, 3 * n):
        b0, b1 = boss(g), boss(g + 1)
        assert all(b0[k] <= b1[k] for k in b0), \
            f"보스 난이도가 단조 증가하지 않는다 ({g} -> {g + 1}스테이지) — reach() 가 이 성질에 기댄다"
    agg, projects, _, _ = _demo.real
    who = ([(name, hero(a), strict) for name, a, strict in BALANCE_HEROES]
           + ([("이 PC", hero(agg), True)] if projects else []))      # 내 영웅은 예전처럼 엄격하게
    for name, h, strict in who:
        try:
            _check_balance(h, ds, strict)
        except AssertionError as e:
            raise AssertionError(f"[{name} · Lv.{h['level']} ATK {h['atk']} HP {h['hp']} DEF {h['dfn']} "
                                 f"CRIT {h['crit']} SPD {h['spd']}] {e}") from None

    # 3) 원정(방치)은 보조 수입이어야 한다 — 최대 배율 8시간으로도 환생을 대체 못 함
    soul_of = lambda g: round(soul_base(g) * ds[(g - 1) % n]["soul"])      # 환생 정산
    side_of = lambda g: round(side_base(g) * ds[(g - 1) % n]["soul"])      # 보조 수입 (후반 배율 없음)
    for last in (n, 2 * n, 3 * n, 5 * n, 8 * n, 12 * n):      # 후반 혼 배율 구간까지 본다
        idle8 = side_of(last) / IDLE_DIV * IDLE_CAP_H * 3600 * (1 + IDLE_TOKEN_MAX)
        rebirth = sum(soul_of(g) for g in range(1, last + 1))
        assert idle8 < rebirth * 4, \
            f"{last//n}층 방치 수입 {idle8:.0f}이 환생 {rebirth}의 4배 이상 — IDLE_DIV 상향 필요"
        # 미션도 보조 수입이어야 한다. 한 주치 미션 vs 한 주치 환생(하루 2회 = 토큰 200만/일).
        # 1.7 = 연속 7일 최대 배율(streakMul). 미션을 쪼개도 예산이 고정이라 여기가 안 움직인다.
        # 슬롯은 운이라 평균으로 센다 — 지급을 평균 배당으로 나눠 둬서 하루 기대값이 SLOT_BUDGET 이다
        wk_mission = ((DAY_BUDGET * 7 + WEEK_BUDGET) * 1.7
                      + SLOT_BUDGET * 7) * side_of(last)
        wk_rebirth = 14 * rebirth
        assert wk_mission < wk_rebirth * 2, (
            f"{last//n}층 미션 주간 수입 {wk_mission:.0f}이 환생 {wk_rebirth}의 2배 이상 "
            f"— DAY_BUDGET·WEEK_BUDGET 하향 필요")
    # 3-0) 레이드가 열린 뒤(RAID_MIN_BEST~)에는 레이드 몫까지 얹어도 주간 보조 수입이 상한 안이어야 한다.
    #       위 검사는 층 끝만 본다. 여기는 스테이지를 하나도 건너뛰지 않는다 — 문턱을 한 칸 잘못 잡으면
    #       그 칸에서만 상한을 넘는다 (15스테이지에서는 레이드를 얹으면 넘는다).
    week = (DAY_BUDGET * 7 + WEEK_BUDGET) * 1.7 + SLOT_BUDGET * 7 + RAID_BUDGET
    paid = sum(soul_of(g) for g in range(1, RAID_MIN_BEST))
    for g in range(RAID_MIN_BEST, 12 * n + 1):
        paid += soul_of(g)
        assert week * side_of(g) < 14 * paid * 2, \
            f"{g}스테이지: 레이드까지 얹은 주간 보조 수입이 환생의 2배를 넘는다 — RAID_BUDGET 을 낮추거나 RAID_MIN_BEST 를 올려라"
    assert len(RAID_BOSSES) >= 4 and len({b[0] for b in RAID_BOSSES}) == len(RAID_BOSSES), "레이드 보스가 모자라거나 겹친다"
    assert not {b[0] for b in RAID_BOSSES} & {b[0] for _, bs in THEMES for b in bs}, "레이드 보스 이름이 탑 보스와 겹친다"
    _demo_rest(rec)


# 밸런스 검증에 쓰는 고정 영웅. 사용 패턴이 곧 빌드라서 양만이 아니라 모양도 흩어 놨다 —
# 한 사람의 로그로만 보면 그 사람 빌드에서만 성립하는 균형을 놓친다.
#
# 세 번째 칸은 '엄격' 이다. 엄격한 기준(특성은 힘을 넘지 않는다 · 20레벨이면 도달이 는다 ·
# 다음다음 층은 환생이 두 배 든다)은 개발자 영웅 하나에 맞춰 조율한 값이라, 재 보니 아래 넷에서는
# 성립하지 않는다. 그 넷은 느슨한 기준(_check_balance 의 strict=False)으로만 본다 —
# 느슨한 기준은 지금 값을 잰 것이지 목표가 아니다. 더 벌어지면 실패한다.
#   첫 주      환생만으로는 4스테이지에서 안 움직인다(32회, 특성 20레벨). 파괴 20레벨이 도달 +0
#   많이 씀    4층이 5회, 3층이 3회 (두 배 미만). 벽 60레벨 31 > 힘 30
#   초월 직전  4층이 3회, 3층이 2회. 혼 20레벨 25 > 힘 24
#   캐시만     3층이 11회, 2층이 6회. 벽 20레벨 13 > 힘 12. 파괴 20레벨이 도달 +0.
#              호출 수가 준 SPD 만으로 무환생 벽이 7 에서 9 로 밀린다 (기준은 한 칸)
# 고치면(상수를 조율하면) 여기 True 로 바꿔 엄격한 쪽으로 올린다.
_agg = lambda i, o, c, t, n: {"input": i, "output": o, "cache_read": c, "thinking": t, "calls": n}
BALANCE_HEROES = [
    ("첫 주",     _agg(1_500_000, 400_000, 40_000_000, 100_000, 600), False),
    ("보통",      _agg(20_000_000, 6_000_000, 900_000_000, 1_200_000, 8_000), True),
    ("많이 씀",   _agg(150_000_000, 40_000_000, 6_000_000_000, 6_000_000, 60_000), False),
    ("초월 직전", _agg(400_000_000, 80_000_000, 15_000_000_000, 20_000_000, 150_000), False),
    ("출력만",    _agg(5_000_000, 30_000_000, 0, 0, 2_000), True),
    ("캐시만",    _agg(30_000_000, 500_000, 20_000_000_000, 0, 90_000), False),
]


# 환생 한 번 사이(반나절 — 하루 2회 환생)에 들어오는 보조 수입의 최대치. 단위는 역대 최고 스테이지 보스 한 판의 혼.
# 원정 12시간 x 최대 배율, 미션 연속 7일(x1.7), 슬롯 평균.
SIDE_MAX = (12 * 3600 / IDLE_DIV * (1 + IDLE_TOKEN_MAX) + (DAY_BUDGET / 2 + WEEK_BUDGET / 14) * 1.7
            + SLOT_BUDGET / 2 + RAID_BUDGET / 14)


def _full_floors(h, n, side=SIDE_MAX, fights=43_200, rebirths=80):
    """들어오는 혼을 전부 넣은 속도 모델 -> (층 진입 회차, 층마다 보조 수입 / 환생 수입의 최댓값).

    simulate() 는 환생 혼만 센다. v0.14.11 의 후반 배율을 그 모델로만 봤다가, 보조 수입이 환생을
    넘어서는 것을 놓쳤다. 여기서는 환생 + 보조 수입(side) + 황금 개체를 다 넣는다.
    보조 수입과 황금 개체는 가장 유리한 쪽으로 잡는다 — 수입은 최대치, 황금은 하루 종일 켜 두고
    혼이 큰 칸부터 한 칸씩(평균 1/RARE_RATE 판) 잡으며 늘 이긴다.
    화면(JS)의 soulOf · sideSoul · catchRare · dexSoul · dexPts 와 같은 규칙이어야 한다."""
    import itertools
    ds, nt = dungeons(), len(THEMES)
    soul = lambda s: soul_base(s) * ds[(s - 1) % n]["soul"]          # 환생 정산
    sside = lambda s: side_base(s) * ds[(s - 1) % n]["soul"]         # 보조 수입
    souls, tr, caught, floors, best, share = 0.0, {}, set(), {}, 0, {}
    cyc = itertools.cycle(SIM_ORDER)
    for r in range(rebirths + 1):
        done = sum(all((t, s) in caught for s in range(1, n + 1)) for t in range(nt))
        # 한 층 완성 보상(DEX_PT)은 각성과 같은 배분 포인트다 — reach 에는 포인트로 넣는다
        g = reach(h, {**tr, "pt": tr.get("pt", 0) + done * DEX_PT / TRAIT_PT})
        best = max(best, g)
        floors.setdefault((g - 1) // n + 1, r)
        mult = ((1 + REBIRTH_SOUL * r) * (1 + TRAIT_SOUL / 100 * tr.get("soul", 0))
                * (1 + DEX_SOUL / 100 * len(caught)) * (1 + ACH_SOUL / 100 * len(ACHS)))
        reb = sum(soul(i) for i in range(1, g + 1)) * mult
        extra = side * sside(best) * mult
        cand = []
        for t in range(nt):
            for s in range(1, n + 1):
                base = t * n + s
                if (t, s) not in caught and base <= g:
                    deep = base + (g - base) // (n * nt) * (n * nt)     # 같은 칸의 가장 깊은 스테이지에서 잡는다
                    cand.append((sside(deep), t, s))
        for val, t, s in sorted(cand, reverse=True)[:int(fights * RARE_RATE)]:
            extra += val * RARE_MUL * mult
            caught.add((t, s))
        souls += reb + extra
        fl = (g - 1) // n + 1
        share[fl] = max(share.get(fl, 0), extra / reb)
        for _ in range(5000):
            k = next(cyc)
            c = trait_cost(tr.get(k, 0))
            if c > souls:
                break
            souls -= c
            tr[k] = tr.get(k, 0) + 1
    return floors, share


def _check_full_pace(h, n):
    """들어오는 혼을 전부 넣어도 속도와 수입 구조가 무너지지 않는다 (가장 유리한 플레이어 기준).

    보조 수입은 최대치, 황금 개체는 하루 종일 켜 두고 몰아 잡는 경우다. 환생 혼만 세는 위 검사보다
    당연히 빠르다 — 그래서 바닥을 4회가 아니라 3회로 둔다. 지금 값에서 엄격한 영웅 셋의 6~9층은
    층당 3~5회다(출력만 쓰는 영웅의 6·7층이 3회로 바닥)."""
    fl, share = _full_floors(h, n)
    assert max(fl) >= 10, f"전부 넣은 모델이 환생 80회에 {max(fl)}층이다"
    for f in range(6, 10):
        need = fl[f] - fl[f - 1]
        # 하한 2: v0.19.6 에서 승패가 선공을 세며 깊은 층이 빨라졌다(출력만 7층 2회). 위 환생 표와 같은 이유다.
        assert 2 <= need <= 12, \
            f"보조 수입과 황금 개체를 다 챙기면 {f}층이 환생 {need}회에 열린다 — 보조 수입이 과하다"
    # 깊은 층(7~10층)에서 보조 수입은 환생보다 작되 죽지 않아야 한다.
    #   1 을 넘으면 환생이 주 수입이 아니다 — v0.14.11 은 후반 배율을 그대로 붙여 2.0 까지 갔다.
    #   0.3 아래면 놀 거리가 버려진다 — 배율을 아예 안 붙이면 7층에서 0.02 였다.
    # 1~6층은 원래 보조 수입이 환생과 맞먹거나 더 크다(초반 디딤돌) — 여기서 보지 않는다.
    for f in range(7, 11):
        assert share[f] < 1, f"{f}층에서 환생 사이의 보조 수입이 환생 수입의 {share[f]:.2f}배다 — 환생이 주 수입이 아니다"
        assert share[f] >= 0.3, \
            f"{f}층에서 보조 수입을 다 챙겨도 환생의 {share[f]:.0%}다 — 원정·미션·혼 사냥·슬롯이 후반에 버려진다"


def _check_balance(h, ds, strict=True):
    """영웅 하나에 대해 환생 설계가 성립하는가. 절대 스테이지는 영웅마다 다르니 상대적 성질만 본다.
    strict 가 아니면 느슨한 기준만 본다 (BALANCE_HEROES 주석 참고)."""
    n = len(ds)

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
        assert deeper > nxt, f"{base_floor+2}층({deeper}회)이 {base_floor+1}층({nxt}회)과 같이 열린다"
        assert not strict or deeper >= 2 * nxt, \
            f"{base_floor+2}층({deeper}회)이 {base_floor+1}층({nxt}회) 대비 가파르지 않다"
    seq = [floors[f] for f in sorted(floors) if f >= base_floor]
    assert seq == sorted(seq), f"깊은 층이 얕은 층보다 먼저 열린다: {floors}"
    assert not strict or rows[-1][1] > base, "환생을 거듭해도 도달 스테이지가 늘지 않는다"
    # 2-1) 후반 속도: 5층부터 층마다 드는 환생 횟수가 고르게 가야 한다 (SOUL_LATE_MUL).
    #      혼 배율이 없던 시절엔 층마다 두 배씩 들었다(5층 9회 · 6층 21회 · 7층 52회).
    #      너무 올리면 반대로 뒤로 갈수록 빨라진다 — 1.12 에서 층당 3회까지 떨어지고 끝없이 오른다.
    #      지금 값(1.09)에서 엄격한 영웅 셋이 5~9층에 층당 5~15회다(변이 포함, 내성 0). 그 밖으로 나가면 실패한다.
    #      상한 16 은 v0.18.1 에서 12 를 올린 것이다 — 사용자가 지금 구간의 속도를 줄이려고 뒤쪽 층을 내줬다.
    if strict:
        late = {}
        for r, g, fl, _, _ in simulate(h, n, rebirths=80):
            late.setdefault(fl, r)
        assert max(late) >= 9, f"환생 80회에 {max(late)}층 — 후반이 다시 막혔다 (SOUL_LATE_MUL 이 낮다)"
        for fl in range(5, 10):
            need = late[fl] - late[fl - 1]
            assert need <= 16, f"{fl}층에 환생 {need}회가 든다 — 후반이 다시 느려졌다 (SOUL_LATE_MUL 이 낮다)"
            # 하한 3: v0.19.6 에서 승패가 선공을 세며 깊은 층이 빨라졌다(출력만 9층 3회). 보스를 키워 되돌리지
            # 않기로 했다 — 수동으로는 이미 그만큼 갔고, 자동이 거기에 맞춘 것이다.
            assert need >= 3, f"{fl}층이 환생 {need}회에 열린다 — 후반이 폭주한다 (SOUL_LATE_MUL 이 높다)"

        _check_full_pace(h, n)

    # 위 회차는 SIM_ORDER(신속 한 번)로 잰 값이다. 신속을 더 사도 그 회차보다 빨라지면 안 된다 —
    # 빨라지면 표가 실제로 갈 수 있는 길보다 느리다는 뜻이고, 순서를 다시 봐야 한다. 늦어지는 건 괜찮다.
    mixed = {}
    for r, g, fl, _, _ in simulate(h, n, rebirths=32, order=SIM_ORDER + ["spd"]):
        mixed.setdefault(fl, r)
    for fl in set(floors) | set(mixed):
        a, b = floors.get(fl, 33), mixed.get(fl, 33)        # 33 = 32회 안에 못 닿았다
        assert b >= a - 2, \
            f"신속을 더 섞어 사면 {fl}층 진입이 {a}회에서 {b}회로 바뀐다 — 환생 표(SIM_ORDER)가 낡았다"

    # 3-2) 파괴의 유산이 혼을 깊이로 바꾸는가. 가산이던 시절엔 어떤 레벨을 사도 도달이
    #      그대로여서, 사면 손해인 특성이었다. 절대 스테이지는 사용자마다 다르니
    #      (a) 사면 더 깊이 간다 (b) 힘의 유산과 같은 줄에 선다 두 가지만 본다.
    # 다섯 특성 전부 '사면 더 깊이 가고, 힘의 유산과 세 칸 안에 서되 넘지는 않는다'.
    # 예전엔 파괴에만 이 기준을 뒀고, 그사이 벽은 60레벨을 사도 도달이 15 에서 멈춰 있었다.
    # 느슨한 기준(strict 가 아닐 때): 60레벨이면 도달이 늘고, 힘을 넘더라도 한 칸까지다.
    base_r = base
    atk20, atk60 = reach(h, {"atk": 20}), reach(h, {"atk": 60})
    assert atk20 > base_r, f"힘의 유산 20레벨이 도달을 못 늘린다 ({base_r} -> {atk20})"
    slack = 0 if strict else 1
    # 신속은 뺀다 — 2타 천장(v0.19.2)이라 레벨당으로는 힘과 나란히 설 수 없다. 대신 값이 아주 싸서
    # (힘이 수천만 혼일 때 신속은 수백) 배분 점수를 아껴 주는 몫을 한다. 아래에서 '넘지 않는다'만 본다.
    for k, ko in (("hp", "혼"), ("dfn", "벽"), ("cdmg", "파괴")):
        near, far = reach(h, {k: 20}), reach(h, {k: 60})
        assert (near if strict else far) > base_r, \
            f"{ko}의 유산 {20 if strict else 60}레벨이 도달을 못 늘린다 ({base_r} -> {near if strict else far})" \
            f" — 혼을 버리는 특성이다"
        # 다섯 칸: v0.19.2 에서 2타가 쉬워지며 힘 20레벨이 한 칸 올랐고(출력만: 벽 16 · 힘 19 -> 20),
        # v0.19.6 에서 승패가 선공을 세며 또 한 칸 올랐다(힘 21). 먼저 쳐서 잡는 싸움이라 공격 쪽이 앞선다. 벽은 그대로다.
        assert near >= atk20 - 5, \
            f"{ko}({near})가 힘({atk20})보다 다섯 칸 넘게 뒤진다 — 선택이 아니라 들러리다"
        assert near <= atk20 + slack and far <= atk60 + slack, \
            f"{ko}({near}/{far})가 힘({atk20}/{atk60})을 넘어섰다 — 그 빌드만 정답이 된다"

    _sx = spd_extra
    try:
        globals()["spd_extra"] = lambda spd, bspd: 0.0
        flat = reach(h)                                             # 추가타가 없던 시절의 깊이
    finally:
        globals()["spd_extra"] = _sx
    assert base <= flat + 1 + slack, \
        f"추가타가 벽을 {flat} -> {base} 스테이지로 밀었다 — 환생 없이 너무 깊이 간다"
    # 신속의 유산은 사면 더 깊이 가야 하지만 힘의 유산을 넘어서는 안 된다 (파괴의 유산과 같은 기준).
    # 타격 수에 상한이 없으니 TRAIT_MULS["spd"] 를 올리면 넘길 수 있다 — 그래서 여기서 붙든다.
    spd20, spd60 = reach(h, {"spd": 20}), reach(h, {"spd": 60})
    assert spd20 <= atk20 + slack and spd60 <= atk60 + slack, \
        f"신속({spd20}/{spd60})이 힘({atk20}/{atk60})을 넘어섰다 — 속도 빌드만 정답이 된다"


def _demo_rest(rec):
    """영웅과 무관한 나머지 검사. 예전엔 밸런스 검증 뒤에 붙어 있어서, 이 PC 에 기록이 없으면
    (= CI) 같이 건너뛰었다."""
    import tempfile
    # 3-1) 유물 등급표는 합이 100% 여야 한다 — 어긋나면 마지막 등급이 나머지를 통째로 먹는다
    import re
    odds = [float(x) for x in re.findall(r'"[^"]+", ([\d.]+),', 
                  re.search(r"const RARITY = \[(.+?)\];", TEMPLATE, re.S).group(1))]
    assert len(odds) >= 4 and abs(sum(odds) - 100) < 1e-9, f"유물 등급 확률 합이 {sum(odds)}% 다: {odds}"
    # 3-2) 슬롯 문양표도 합이 100% 여야 한다 — 어긋나면 마지막 문양이 나머지를 통째로 먹는다
    sw = [float(x) for x in re.findall(r'", (\d+), \d+\]',
                 re.search(r"const SLOT_SYM = \[(.+?)\];", TEMPLATE, re.S).group(1))]
    assert len(sw) >= 5 and abs(sum(sw) - 100) < 1e-9, f"슬롯 문양 확률 합이 {sum(sw)}% 다: {sw}"
    # SLOT_AVG 는 문양표에서 나오는 값이라 표를 고치면 같이 움직여야 한다 — 어긋나면
    # 하루 기대 수입이 SLOT_BUDGET 에서 벗어난다(지급을 이 값으로 나누기 때문).
    pays = [int(x) for x in re.findall(r'", \d+, (\d+)\]',
                 re.search(r"const SLOT_SYM = \[(.+?)\];", TEMPLATE, re.S).group(1))]
    lines = len(re.findall(r"\[\[\d, \d\]",
                re.search(r"const SLOT_LINE = \[(.+?)\];", TEMPLATE, re.S).group(1)))
    avg = lines * sum((w / 100) ** 3 * p for w, p in zip(sw, pays))
    assert abs(avg - SLOT_AVG) < 0.01, f"슬롯 평균 배당은 x{avg:.3f} 인데 SLOT_AVG 는 {SLOT_AVG} 다"
    # 재격파 파밍으로 버는 혼(중복 환산)이 늘면 환생 기운(토큰) 제한을 우회한다.
    # 확률을 올리려면 환산 나누기도 같이 올려야 한다 — 곱이 옛 값(0.5% x 1/10)을 넘으면 안 된다.
    again = float(re.search(r"RELIC_AGAIN = ([\d.]+)", TEMPLATE).group(1))
    dup = float(re.search(r"RELIC_DUP = ([\d.]+)", TEMPLATE).group(1))
    assert again / dup <= 0.005 / 10 + 1e-12, \
        f"재격파 파밍 혼 수입이 늘었다 ({again}/{dup}) — RELIC_DUP 를 같이 올려라"

    # 3-3) 속도: 보스보다 빠른 만큼 추가타가 붙는다. 예전엔 선공만 정해서, 보스 SPD 를
    #      넘긴 1점부터 값이 0 이었다 — 아무도 SPD 에 배분하지 않았다.
    #      (a) 식의 경계 (b) 평균 피해 모델이 실제로 본다 (c) 그래도 층 벽을 밀지 않는다.
    assert spd_extra(50, 100) == 0 and spd_extra(100, 100) == 0, "느린데 추가타가 붙는다"
    assert spd_extra(150, 100) == 0.5 and spd_extra(200, 100) == 1, "1~2배 구간이 2타 확률이 아니다"
    # 천장은 2타다 (v0.19.2). 천장이 없으면 보스 SPD 가 덧셈으로 크는 지금 속도만 정답이 된다
    assert spd_extra(10 ** 6, 1) == HITS_MAX - 1, "추가 타격 수가 천장을 넘는다"
    # 보스 SPD 는 덧셈으로 오른다 — 배분(1점에 GAIN['spd'])으로 따라잡을 수 있어야 한다
    assert boss(100)["spd"] == 100 * BOSS_SPD and boss(200)["spd"] == 2 * boss(100)["spd"], "보스 SPD 가 덧셈으로 안 오른다"
    # v0.19.8: 벽은 보스 ATK(x1.24) 와 힘(x1.135) 사이에 둔다 — 보스 ATK 보다 느리면 벽이 죽고, 1.28 이면 힘의 두 배를 민다
    assert B_ATK_STEP <= tmul_of("dfn") < STEP and tmul_of("atk") < tmul_of("dfn"), "벽 배율이 보스 ATK 성장과 힘 사이를 벗어났다"
    # DEF 는 비율로 깎는다. 같은 자리수면 절반, 아무리 높아도 0 이 되지 않는다.
    assert abs(mitigate(100, 100) - 50) < 1e-9, mitigate(100, 100)
    assert mitigate(1000, 0) == 1000 and 0 < mitigate(1000, 10 ** 9) <= 1, "DEF 가 피해를 전부 지운다"
    assert mitigate(3_359_617, 63_256) < 3_359_617 * 0.99, "백만 단위 ATK 앞에서 DEF 가 아무 일도 못 한다"
    hh = {"level": 1, "atk": 100, "hp": 1000, "dfn": 0, "crit": 0, "spd": 20}
    quick = {"hp": 100, "atk": 10, "dfn": 0, "spd": 10}             # 내가 두 배 빠르다
    assert turns_to_win(hh, quick)[0] < turns_to_win(hh, {**quick, "spd": 10 ** 9})[0], \
        "추가타가 평균 피해 모델에 안 들어갔다 — reach() 와 자동 도전이 SPD 를 0 으로 본다"
    # 보조 수입은 환생이 커진 만큼만 커진다: 환생 한 번이 '보조 수입용 혼' 몇 판치인지가 배율 없던 시절과 같다.
    # 이 식이 어긋나면 후반에 보조 수입이 환생을 넘어서거나(v0.14.11) 죽는다.
    ds = dungeons()
    n = len(ds)
    for g in (10, 45, 46, 60, 90, 150, 240):
        m = lambda i: ds[(i - 1) % n]["soul"]
        now = sum(soul_base(i) * m(i) for i in range(1, g + 1)) / (side_base(g) * m(g))
        old = sum(i ** SOUL_EXP * m(i) for i in range(1, g + 1)) / (g ** SOUL_EXP * m(g))
        assert abs(now / old - 1) < 1e-9, f"{g}스테이지: 환생 한 번이 보조 수입 {now:.2f}판치다 — 배율 없던 시절은 {old:.2f}판치"
    assert side_scale(SOUL_LATE_G) == 1 and side_scale(SOUL_LATE_G + 30) > 2, "보조 수입이 후반에 환생을 따라 자라지 않는다"
    # 보조 수입은 sideSoul 을 쓴다. soulOf(후반 배율 포함)를 쓰면 보조 수입이 환생을 넘어선다 (v0.14.11)
    for pat in ("soulOf(Math.max(1, save.best))", "soulOf(save.best)", "soulOf(g) / K.idleDiv", "soulOf(g) / RELIC_DUP",
                "soulOf(g) * K.rareMul"):
        assert pat not in TEMPLATE, f"보조 수입이 환생 정산용 혼({pat})을 쓴다 — sideSoul 이어야 한다"
    for tag in ("hitsOf", "K.tmuls", "p * p / (p + d.dfn)"):
        assert tag in TEMPLATE, f"게임 쪽이 {tag} 를 안 읽는다 — 화면과 모델이 어긋난다"
    # 로드 시점에 불리는 헬퍼는 선언이 먼저 와야 한다. 뒤에 있으면 TDZ 로 스크립트가 통째로
    # 죽어 화면이 빈칸만 남는다 — 실제로 한 번 냈고, node --check 는 문법만 봐서 못 잡는다.
    assert TEMPLATE.index("const TM = ") < TEMPLATE.index("const TRAITS = "), \
        "TM 이 TRAITS 보다 뒤에 있다 — 로드 시점 TDZ 로 게임 화면이 통째로 빈다"

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
            # Claude Code 가 30일 지난 로그를 지워도 장부에 적힌 값으로 계속 센다
            led = {}
            collect(roots=[logs], ledger=led)
            os.remove(os.path.join(logs, "proj", "old.jsonl"))
            a_led, _, _, _ = collect(roots=[logs], ledger=json.loads(json.dumps(led)))
            assert a_led["calls"] == 2, "지워진 로그의 토큰이 누적에서 빠졌다"
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
    # 원래 플랫폼 분기로 되돌아왔다. 진짜 윈도우에서는 되돌아온 곳도 윈도우 분기다 —
    # 이 검사는 예전에 CI 에서 한 번도 돌지 않아(밸런스 검증 뒤라 같이 빠졌다) 그걸 몰랐다.
    assert ("/dev/null" in hook_command()) == (os.name != "nt"), hook_command()

    print("ok")


if __name__ == "__main__":
    sys.exit(main())
