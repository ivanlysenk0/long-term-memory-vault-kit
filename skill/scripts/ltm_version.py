#!/usr/bin/env python3
"""
ltm_version.py - показати три версії одразу: скіл, пам'ять, репозиторій.

Навіщо. Скіл живе в трьох місцях, і вони розходяться непомітно:
файли скіла в каталозі агента, скрипти всередині пам'яті, свіжий код у GitHub.
Агент запускає цей скрипт ПЕРЕД роботою з пам'яттю, щоб не працювати
за застарілими правилами і не мовчати про це.

Використання:
    python3 ltm_version.py                   людський вивід
    python3 ltm_version.py --json            машинний вивід для агента
    python3 ltm_version.py --vault PATH      явний шлях до пам'яті
    python3 ltm_version.py --offline         не ходити в мережу
    python3 ltm_version.py --timeout 3       інший таймаут, за замовчуванням 5 с

Код повернення:
    0  усе збігається, або розходження лише в мережевій версії
    2  локальні версії розійшлися: час оновлювати
    0  мережа недоступна. Це НЕ помилка: працювати офлайн можна

Мережа необов'язкова. Немає інтернету, немає проблеми: скрипт мовчки
працює за локальними версіями і каже про це прямо.
"""

from __future__ import annotations

__version__ = "1.1.0"

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

MANIFEST = ".ltm-install-manifest.json"

# Два офіційні репозиторії. Перевіряємо обидва: людина могла поставити будь-який.
REMOTES = {
    "uk": "https://raw.githubusercontent.com/ivanlysenk0/long-term-memory-vault-kit/main/VERSION",
    "en": "https://raw.githubusercontent.com/ivanlysenk0/long-term-memory-for-agents/main/VERSION",
}


def skill_version() -> str:
    """Версія скіла, який зараз лежить поруч із цим скриптом."""
    # Спершу файл VERSION у корені репозиторію, якщо скрипт запущено з клона.
    here = Path(__file__).resolve().parent
    for cand in (here.parent.parent / "VERSION", here.parent / "VERSION"):
        if cand.is_file():
            try:
                v = cand.read_text(encoding="utf-8").strip()
                if v:
                    return v
            except OSError:
                pass
    # Інакше беремо з сусіднього ltm_init.py: він завжди поруч.
    init = here / "ltm_init.py"
    if init.is_file():
        try:
            for line in init.read_text(encoding="utf-8").splitlines()[:40]:
                if line.startswith("__version__"):
                    return line.split('"')[1]
        except (OSError, IndexError):
            pass
    return __version__


def find_vault(explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_dir() else None
    env = os.environ.get("LTM_VAULT")
    if env and Path(env).expanduser().is_dir():
        return Path(env).expanduser()
    for c in (Path.home() / "long-term-memory-vault",
              Path.home() / "memory" / "long-term-memory-vault"):
        if c.is_dir():
            return c
    return None


def vault_version(vault: Path | None) -> str | None:
    if not vault:
        return None
    p = vault / MANIFEST
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    v = data.get("scripts_version") if isinstance(data, dict) else None
    return v if isinstance(v, str) else None


def remote_version(timeout: float = 5.0) -> tuple[str | None, str | None]:
    """Свіжа версія з GitHub. Повертає (версія, причина невдачі).

    Мережа тут необов'язкова, тому будь-яка помилка це не виняток,
    а просто «невідомо». Агент мусить продовжити роботу, а не впасти.
    """
    last = None
    for name, url in REMOTES.items():
        try:
            with urlopen(url, timeout=timeout) as r:
                v = r.read(64).decode("utf-8", "replace").strip()
                if v:
                    return v, None
        except (URLError, HTTPError, OSError, ValueError) as e:
            last = f"{name}: {e.__class__.__name__}"
    return None, last or "невідома причина"


def parse(v: str | None) -> tuple:
    """Версія як кортеж чисел, щоб порівнювати 1.10.0 і 1.9.0 правильно."""
    if not v:
        return ()
    out = []
    for part in v.split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Версії скіла, пам'яті та репозиторію")
    ap.add_argument("--vault", help="шлях до пам'яті")
    ap.add_argument("--json", action="store_true", help="машинний вивід")
    ap.add_argument("--offline", action="store_true", help="не ходити в мережу")
    ap.add_argument("--timeout", type=float, default=5.0, help="таймаут мережі, секунд")
    args = ap.parse_args()

    vault = find_vault(args.vault)
    skill = skill_version()
    inmem = vault_version(vault)
    remote, why = (None, "вимкнено прапорцем --offline") if args.offline \
        else remote_version(args.timeout)

    local_drift = bool(vault) and inmem != skill
    remote_drift = bool(remote) and parse(remote) > parse(skill)

    # Що робити: одна дія, а не перелік можливостей.
    if remote_drift:
        action = ("У репозиторії є новіша версія. Спершу онови сам скіл: "
                  "git pull і ./install.sh, потім --update і --migrate.")
    elif local_drift:
        action = (f"Скрипти в пам'яті відстали. Онови: "
                  f"ltm_init.py --update --path {vault} , потім "
                  f"ltm_init.py --migrate --path {vault} --dry-run")
    else:
        action = "Усе актуальне, можна працювати."

    if args.json:
        print(json.dumps({
            "skill": skill,
            "vault": str(vault) if vault else None,
            "vault_version": inmem,
            "remote": remote,
            "remote_error": why,
            "local_drift": local_drift,
            "remote_drift": remote_drift,
            "action": action,
        }, ensure_ascii=False, indent=2))
        return 2 if local_drift or remote_drift else 0

    print("=== Версії ===")
    print(f"  скіл на диску:      {skill}")
    print(f"  пам'ять:            {inmem or 'позначки немає'}"
          + (f"   ({vault})" if vault else "   (пам'ять не знайдено)"))
    if remote:
        print(f"  репозиторій:        {remote}")
    else:
        print(f"  репозиторій:        невідомо ({why})")
        print("  мережа необов'язкова: локальна перевірка все одно працює")
    print()
    print(action)
    return 2 if local_drift or remote_drift else 0


if __name__ == "__main__":
    sys.exit(main())
