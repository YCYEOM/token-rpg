# Token RPG

Claude Code에 쓴 토큰이 그대로 캐릭터가 되는 턴제 RPG.

실제로 코딩한 만큼 강해진다. 출력 토큰은 공격력, 캐시 재사용은 방어력,
thinking 토큰은 치명타율이 된다. 프로젝트 하나하나가 던전 보스다.

```
$ token-rpg open
Lv.28 코드 술사 🧑‍💻  HP 775 ATK 90 DEF 40.0 CRIT 30.5%  던전 8개
```

## 설치

```bash
pipx install token-rpg      # 권장 (또는: uv tool install token-rpg)
token-rpg open              # 집계 후 브라우저로 열기
```

의존성은 없다. 표준 라이브러리만 쓴다.

## 자동 갱신

```bash
token-rpg install-hook      # Claude Code Stop 훅에 등록
```

이후 Claude Code가 응답을 마칠 때마다 스탯이 자동 갱신된다(백그라운드, 1초 미만).
`token-rpg uninstall-hook`으로 되돌린다. `~/.claude/settings.json`을 고치기 전에
항상 `.bak` 백업을 남긴다.

## 게임 구조

**스탯** — 토큰 종류가 캐릭터 빌드를 정한다. 사용 패턴이 곧 개성이다.

| 스탯 | 출처 |
|---|---|
| ATK | 출력 토큰 |
| DEF | 캐시 읽기 (재사용 효율이 곧 방어력) |
| CRIT | thinking 토큰 |
| SPD | API 호출 수 |
| EXP | 입력 + 캐시 생성 + 출력 |

**던전** — 프로젝트 하나가 스테이지 하나. 8개를 돌면 1층이고, 층마다 보스가
지수로 강해진다. 내 스탯은 토큰에 선형으로 크고 보스는 거듭제곱근으로 커서,
막힌 곳은 토큰을 더 쓰면 반드시 넘을 수 있다.

**환생** — 클리어 기록과 스탯 배분을 버리고 혼을 얻는다. 혼으로 사는 영구 특성은
배율(×1.12/레벨)이라, 배분 포인트로는 따라갈 수 없는 층 벽을 넘게 해준다.
1층은 환생 없이, 2층은 1회, 3층은 9회, 4층은 17회쯤 걸린다.

**원정** — 역대 최고로 깊이 간 스테이지를 자동 반복해 혼을 캔다. 최대 8시간까지
누적되고, 수령 이후 새로 쓴 토큰이 생산 배율이 된다(최대 3배).
**환생해도 멈추지 않는다** — 기준이 이번 회차 진행이 아니라 역대 최고 기록이고,
쌓여 있던 미수령분도 그대로 남는다.

## 여러 PC 합산

각 PC에서 `token-rpg export`를 돌리면 4KB짜리 스냅샷만 남는다.
스냅샷 폴더를 클라우드 동기화 폴더로 지정하면 모든 기기의 사용량이 합산된다.

```bash
export TOKEN_RPG_SNAPSHOTS=~/Dropbox/token-rpg
```

## 명령

| 명령 | 하는 일 |
|---|---|
| `token-rpg` / `build` | 사용량 재집계 후 `game.html` 갱신 |
| `open` | 갱신 후 브라우저로 열기 |
| `export` | 이 PC의 스냅샷만 갱신 |
| `balance` | 난이도·환생 곡선 표 출력 |
| `selftest` | 집계·병합·밸런스 자체 검증 |
| `where` | 데이터 위치 출력 |
| `install-hook` / `uninstall-hook` | 자동 갱신 등록/해제 |

## 데이터

`~/.claude/projects/**/*.jsonl`의 `usage` 필드만 읽는다. **읽기 전용이고,
아무것도 밖으로 보내지 않는다.** 게임 데이터는
`~/.local/share/token-rpg/`(또는 `$XDG_DATA_HOME`)에 저장되고, 진행 상황은
브라우저 localStorage에 있다.

## 다른 AI 툴

현재는 Claude Code 전용이다. Cursor와 GitHub Copilot은 토큰 사용량을 로컬에
남기지 않고 서버에서 집계하므로 읽어올 값이 없다.

다만 스냅샷 JSON이 사실상 공개 인터페이스라, 사용량을 로컬에 남기는 툴이라면
이 모양으로 뱉는 스크립트를 붙여 합산할 수 있다:

```json
{"host": "내PC", "updated": "2026-09-10T00:00:00+00:00",
 "agg": {"input": 0, "output": 0, "cache_read": 0, "thinking": 0, "calls": 0},
 "projects": {"프로젝트명": 0}, "days": ["2026-09-10"]}
```

## 라이선스

MIT
