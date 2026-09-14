#!/usr/bin/env python3
"""윈도우 알림 영역(트레이) 앱 — macOS 메뉴 막대 앱(macos/TokenRPGMenuBar.swift)의 짝.

같은 일을 한다: 저장 서버를 띄우고, 5분마다 사용량을 다시 집계하고, 아이콘을 누르면
게임을 연다. 다른 점은 플랫폼이 정한다 — 윈도우 트레이 아이콘은 옆에 글자를 못 달아서
레벨과 배지가 툴팁 첫 줄로 간다.

    token-rpg-tray                 # 콘솔 없이 상주 (gui-scripts 라 pythonw 로 돈다)
    token-rpg-tray --startup on    # 로그인할 때 자동 실행
    token-rpg-tray --startup off

의존성은 없다. 트레이는 ctypes 로 Shell_NotifyIconW 를 직접 부른다.
"""
import ctypes
import json
import os
import subprocess
import sys
import threading
import webbrowser
from ctypes import wintypes

import token_rpg

APP_NAME = "Token RPG"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
REFRESH_SEC = 300                      # 맥 앱과 같은 주기

# --- win32 상수 ---
WM_DESTROY, WM_COMMAND, WM_TIMER = 0x0002, 0x0111, 0x0113
WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205
WM_TRAY, WM_UPDATE = 0x0400 + 20, 0x0400 + 21     # WM_USER+n: 우리가 정한 메시지
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04
IDI_APPLICATION = 32512
TPM_RIGHTBUTTON, MF_STRING, MF_SEPARATOR = 0x0002, 0x0000, 0x0800
SW_HIDE, CREATE_NO_WINDOW = 0, 0x08000000
ID_OPEN, ID_REFRESH, ID_STARTUP, ID_QUIT = 1, 2, 3, 4

# win32 타입은 윈도우에만 있다. 나머지(human·tooltip·seen_level)는
# 어느 OS 에서나 import 되어야 검증할 수 있어서 이 블록만 가둔다.
if os.name == "nt":
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)


    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_byte * 8)]


    class NOTIFYICONDATAW(ctypes.Structure):
        """cbSize 로 버전을 알린다 — 최신 전체 구조체를 그대로 쓴다."""
        _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                    ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                    ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                    ("szTip", wintypes.WCHAR * 128),
                    ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                    ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                    ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                    ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON)]


    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


def _api():
    """user32·shell32·kernel32 에 argtypes 를 달아 돌려준다.
    64비트에서 핸들이 int 로 잘리지 않게 restype 을 반드시 지정한다."""
    u, s, k = ctypes.windll.user32, ctypes.windll.shell32, ctypes.windll.kernel32
    u.CreateWindowExW.restype = wintypes.HWND
    u.DefWindowProcW.restype = LRESULT
    u.LoadIconW.restype = wintypes.HICON
    u.CreatePopupMenu.restype = wintypes.HMENU
    u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                              wintypes.UINT, wintypes.UINT]
    s.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    k.GetConsoleWindow.restype = wintypes.HWND
    return u, s, k


# --- 게임 쪽과 주고받기 (전부 token_rpg 를 그대로 쓴다) ---

def _run(args):
    """이 파이썬으로 token_rpg 를 돌린다. 콘솔 창이 깜빡이지 않게 감춘다."""
    return subprocess.run([sys.executable, "-m", "token_rpg"] + args,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=CREATE_NO_WINDOW)


def fetch_status():
    _run(["-q", "build"])
    r = _run(["status"])
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def human(v):
    """게임 화면과 같은 K·M·B 단위."""
    v = float(v)
    for lim, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= lim:
            return f"{v / lim:.1f}".rstrip("0").rstrip(".") + suf
    return str(round(v))


def _seen_path():
    return os.path.join(token_rpg.data_dir(), "tray.json")


def seen_level(level=None):
    """맥 앱의 UserDefaults(seenLevel) 자리. 레벨 업 배지를 언제 끌지 기억한다."""
    p = _seen_path()
    try:
        with open(p, encoding="utf-8") as f:
            cur = json.load(f).get("seenLevel", 0)
    except (OSError, ValueError):
        cur = 0
    if level is not None and level != cur:
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"seenLevel": level}, f)
        except OSError:
            pass
    return cur


def tooltip(status):
    """맥 앱의 아이콘 글자 + 툴팁을 합친 것. 트레이 툴팁은 128자가 상한이다."""
    if not status or not status.get("ok"):
        return f"{APP_NAME} — 아직 사용 기록이 없다"
    h = status.get("hero") or {}
    level, pct = h.get("level", 1), h.get("pct", 0)
    up = level > seen_level()
    badge = (" ⬆" if up else "") + (" ⛏" if status.get("expedFull") else "")
    n = human
    lines = [f"Lv.{level} {int(pct)}%{badge}",
             f"{h.get('emoji', '')} {h.get('title', '')}",
             f"오늘 {n(status.get('today', 0))} 토큰",
             f"다음 레벨까지 {n(h.get('toNext', 0))} EXP"]
    if up:
        lines.append("⬆ 열어서 새 포인트를 배분해라")
    if status.get("expedFull"):
        lines.append("⛏ 원정이 가득 찼다")
    return "\n".join(lines)[:127]


def edge_path():
    """msedge 는 PATH 에 없다(레지스트리 App Paths 로만 풀린다) — 설치 경로를 직접 본다."""
    for var in ("PROGRAMFILES(X86)", "PROGRAMFILES"):
        base = os.environ.get(var)
        exe = base and os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
        if exe and os.path.exists(exe):
            return exe
    return None


def open_game():
    """맥은 팝오버, 윈도우는 창. Edge 앱 모드면 주소창 없이 팝오버에 가깝다."""
    url = f"http://127.0.0.1:{token_rpg.SERVE_PORT}/"
    exe = edge_path()
    if not exe:
        webbrowser.open(url)
        return
    subprocess.Popen([exe, f"--app={url}", "--window-size=460,860"],
                     creationflags=CREATE_NO_WINDOW)


def startup(on):
    """로그인 시 자동 실행. .lnk 는 COM 이 필요해서 레지스트리 Run 키를 쓴다."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_ALL_ACCESS) as k:
        if on:
            exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            exe = exe if os.path.exists(exe) else sys.executable
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ,
                              f'"{exe}" -m token_rpg_tray')
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


def startup_on():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


# --- 트레이 ---

class Tray:
    def __init__(self):
        self.u, self.s, self.k = _api()
        self.status = None
        self.proc = None
        self.data = NOTIFYICONDATAW()
        self.wndproc = WNDPROC(self._on_message)     # GC 되면 콜백이 죽는다 — 꼭 붙들어 둔다

    def run(self):
        self.u.ShowWindow(self.k.GetConsoleWindow(), SW_HIDE)   # 콘솔로 떠도 창을 숨긴다
        self.proc = subprocess.Popen([sys.executable, "-m", "token_rpg", "serve"],
                                     creationflags=CREATE_NO_WINDOW)
        hinst = self.k.GetModuleHandleW(None)
        cls = WNDCLASSW(style=0, lpfnWndProc=self.wndproc, cbClsExtra=0, cbWndExtra=0,
                        hInstance=hinst, hIcon=0, hCursor=0, hbrBackground=0,
                        lpszMenuName=None, lpszClassName="TokenRPGTray")
        self.u.RegisterClassW(ctypes.byref(cls))
        self.hwnd = self.u.CreateWindowExW(0, "TokenRPGTray", APP_NAME, 0,
                                           0, 0, 0, 0, None, None, hinst, None)
        self.data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        self.data.hWnd = self.hwnd
        self.data.uID = 1
        self.data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        self.data.uCallbackMessage = WM_TRAY
        # ponytail: 시스템 기본 아이콘. 전용 .ico 는 게임패드 그림이 필요해질 때 넣는다
        self.data.hIcon = self.u.LoadIconW(None, IDI_APPLICATION)
        self.data.szTip = f"{APP_NAME} — 집계 중…"
        self.s.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self.data))
        self.u.SetTimer(self.hwnd, 1, REFRESH_SEC * 1000, None)
        self.refresh()

        msg = wintypes.MSG()
        while self.u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            self.u.TranslateMessage(ctypes.byref(msg))
            self.u.DispatchMessageW(ctypes.byref(msg))
        return 0

    def refresh(self):
        """집계는 몇 초 걸린다 — 메시지 루프를 막지 않게 딴 실 에서 돌리고 결과만 돌려보낸다."""
        def work():
            self.status = fetch_status()
            self.u.PostMessageW(self.hwnd, WM_UPDATE, 0, 0)
        threading.Thread(target=work, daemon=True).start()

    def show(self):
        self.data.szTip = tooltip(self.status)
        self.s.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.data))

    def menu(self):
        m = self.u.CreatePopupMenu()
        self.u.AppendMenuW(m, MF_STRING, ID_OPEN, "열기")
        self.u.AppendMenuW(m, MF_STRING, ID_REFRESH, "지금 갱신")
        self.u.AppendMenuW(m, MF_SEPARATOR, 0, None)
        self.u.AppendMenuW(m, MF_STRING, ID_STARTUP,
                           "로그인 시 자동 실행 " + ("해제" if startup_on() else "등록"))
        self.u.AppendMenuW(m, MF_STRING, ID_QUIT, "종료")
        pt = wintypes.POINT()
        self.u.GetCursorPos(ctypes.byref(pt))
        self.u.SetForegroundWindow(self.hwnd)      # 없으면 메뉴가 딴 데를 눌러도 안 닫힌다
        self.u.TrackPopupMenu(m, TPM_RIGHTBUTTON, pt.x, pt.y, 0, self.hwnd, None)
        self.u.DestroyMenu(m)

    def _on_message(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY and (lparam & 0xFFFF) == WM_LBUTTONUP:
            if self.status and (self.status.get("hero") or {}).get("level"):
                seen_level(self.status["hero"]["level"])     # 열었으면 ⬆ 는 확인한 것
                self.show()
            open_game()
        elif msg == WM_TRAY and (lparam & 0xFFFF) == WM_RBUTTONUP:
            self.menu()
        elif msg == WM_UPDATE:
            self.show()
        elif msg == WM_TIMER:
            self.refresh()
        elif msg == WM_COMMAND:
            cmd = wparam & 0xFFFF
            if cmd == ID_OPEN:
                open_game()
            elif cmd == ID_REFRESH:
                self.refresh()
            elif cmd == ID_STARTUP:
                startup(not startup_on())
            elif cmd == ID_QUIT:
                self.u.DestroyWindow(hwnd)
        elif msg == WM_DESTROY:
            self.s.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.data))
            if self.proc:
                self.proc.terminate()
            self.u.PostQuitMessage(0)
        else:
            return self.u.DefWindowProcW(hwnd, msg, wparam, lparam)
        return 0


def selftest():
    """어느 OS 에서나 도는 만큼은 돌린다. 윈도우면 창·구조체까지 실제로 만들어 본다."""
    assert human(950) == "950" and human(12345) == "12.3K" and human(2_400_000_000) == "2.4B"
    t = tooltip({"ok": True, "today": 1_500_000, "expedFull": True,
                 "hero": {"level": 99, "pct": 50.0, "toNext": 1234,
                          "emoji": "\U0001f409", "title": "토큰 드래곤"}})
    assert t.startswith("Lv.99 50%") and "⛏" in t, t
    assert len(t) <= 127, "툴팁이 트레이 상한(128자)을 넘는다"
    assert "기록이 없다" in tooltip(None)
    if os.name == "nt":
        # 여기서 틀리면(핸들 절단·구조체 정렬) 트레이가 조용히 안 뜬다
        u, _s, k = _api()
        hinst = k.GetModuleHandleW(None)
        keep = WNDPROC(lambda h, m, w, l: u.DefWindowProcW(h, m, w, l))
        cls = WNDCLASSW(style=0, lpfnWndProc=keep, cbClsExtra=0, cbWndExtra=0,
                        hInstance=hinst, hIcon=0, hCursor=0, hbrBackground=0,
                        lpszMenuName=None, lpszClassName="TokenRPGTraySelftest")
        assert u.RegisterClassW(ctypes.byref(cls)), ctypes.GetLastError()
        hwnd = u.CreateWindowExW(0, "TokenRPGTraySelftest", APP_NAME, 0,
                                 0, 0, 0, 0, None, None, hinst, None)
        assert hwnd, ctypes.GetLastError()
        d = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=hwnd, uID=1)
        assert d.cbSize > 900, d.cbSize          # 필드가 빠지면 Shell_NotifyIconW 가 거부한다
        u.DestroyWindow(hwnd)
    print("ok")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--selftest"]:
        return selftest()
    if os.name != "nt":
        print("트레이 앱은 윈도우 전용이다. macOS 는 메뉴 막대 앱(Token RPG.app)을 쓴다.")
        print("둘 다 아니면: token-rpg open")
        return 1
    if argv[:1] == ["--startup"]:
        on = argv[1:2] == ["on"]
        startup(on)
        print(f"로그인 시 자동 실행 {'등록' if on else '해제'}")
        return 0
    return Tray().run()


if __name__ == "__main__":
    sys.exit(main())
