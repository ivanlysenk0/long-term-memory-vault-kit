"""Пути и служебные каталоги под конкретную ОС.

Почему отдельный модуль. Хуки запускаются на macOS, Linux и Windows, и на
каждой системе «куда класть логи» и «где искать память» решается по-разному.
Держать три набора правил внутри каждого хука значит гарантированно забыть
поправить один из них.

Главный принцип: ничего не выдумывать. Сначала спрашиваем систему и явные
настройки, и только если ответа нет, берём типовое место для этой ОС.
"""

import os
import sys
from pathlib import Path

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"


def state_dir():
    """Каталог для служебного состояния: дампы, маркеры тревоги.

    Это НЕ память и не знание. Это состояние машины, поэтому оно живёт
    отдельно от хранилища и не попадает в git.
    """
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "claude-ltm"
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / "claude-ltm"
    # Linux: уважаем XDG, как принято в системе, а не кладём точку в $HOME.
    base = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return Path(base) / "claude-ltm"


def log_dir():
    """Каталог для логов хуков."""
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "claude-ltm" / "logs"
    if IS_MAC:
        return Path.home() / "Library" / "Logs"
    base = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return Path(base) / "claude-ltm" / "logs"


def find_vault():
    """Где лежит долговременная память.

    Порядок намеренный, от самого явного к самому общему:

    1. `LTM_VAULT` - человек сказал прямо, спорить не с чем;
    2. файл-указатель `~/.ltm-vault` - его пишет установщик скила;
    3. типовые места для этой ОС, включая облачные каталоги.

    Возвращаем None, если ничего не нашли: хук должен молча выключиться,
    а не угадывать путь и лезть в случайную папку.
    """
    env = os.environ.get("LTM_VAULT")
    if env and Path(env).is_dir():
        return Path(env)

    pointer = Path.home() / ".ltm-vault"
    if pointer.is_file():
        try:
            p = Path(pointer.read_text(encoding="utf-8").strip())
            if p.is_dir():
                return p
        except OSError:
            pass

    name = "long-term-memory-vault"
    candidates = [Path.home() / name, Path.home() / "Documents" / name]

    if IS_MAC:
        candidates += [
            Path.home() / "Library" / "Mobile Documents"
            / "iCloud~md~obsidian" / "Documents" / name,
            Path.home() / "Library" / "Mobile Documents"
            / "com~apple~CloudDocs" / name,
        ]
    elif IS_WIN:
        candidates += [
            Path.home() / "iCloudDrive" / "iCloud~md~obsidian" / name,
            Path.home() / "OneDrive" / "Documents" / name,
            Path.home() / "OneDrive" / name,
        ]
    else:
        candidates += [
            Path.home() / ".local" / "share" / name,
            Path.home() / "vault" / name,
        ]

    # Общие облачные каталоги: на всех трёх системах называются одинаково.
    for cloud in ("Dropbox", "Google Drive", "OneDrive", "Yandex.Disk"):
        candidates.append(Path.home() / cloud / name)

    def looks_like_vault(d):
        """Признак настоящего хранилища, а не одноимённой папки.

        Проверяем содержимое, а не имя: папка может называться как угодно,
        но у памяти всегда есть маркер установщика или точка входа.
        """
        try:
            return d.is_dir() and (
                (d / ".ltm-vault").exists()
                or (d / "index.md").is_file()
                or (d / "00-global-home" / "master-index.md").is_file()
            )
        except OSError:
            return False

    for c in candidates:
        if looks_like_vault(c):
            return c

    # Память может лежать где угодно и называться как угодно: `~/projects/notes`,
    # `~/work/brain`. Перебирать имена бесполезно, поэтому обходим домашнюю
    # папку и смотрим СОДЕРЖИМОЕ каталогов.
    #
    # Глубина ограничена двумя уровнями, а служебные и тяжёлые каталоги
    # пропускаются: хук стартует вместе с сессией, и обход всего диска
    # превратил бы открытие проекта в ожидание.
    skip = {
        "Library", "AppData", "Applications", ".Trash", ".cache", ".local",
        "node_modules", ".npm", ".nvm", ".git", "venv", ".venv", "snap",
        "Photos Library.photoslibrary", "Music", "Movies", "Pictures",
        ".docker", ".cargo", ".rustup", "go", ".m2", ".gradle",
    }

    def scan(root, depth):
        if depth > 2:
            return None
        try:
            entries = sorted(root.iterdir())
        except OSError:
            return None
        for e in entries:
            if not e.is_dir() or e.is_symlink():
                continue
            if e.name in skip or (e.name.startswith(".") and e.name != ".ltm"):
                continue
            if looks_like_vault(e):
                return e
            found = scan(e, depth + 1)
            if found:
                return found
        return None

    return scan(Path.home(), 0)
