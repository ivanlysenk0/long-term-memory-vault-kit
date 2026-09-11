#!/usr/bin/env python3
"""Повне видалення довготривалої пам'яті та всіх слідів установки.

Потрібне насамперед для тестування: без відкату скіл перевіряється на машині
рівно один раз, і друга спроба вже йде поверх залишків першої.

Скрипт прибирає п'ять видів слідів:
  1. саму пам'ять (тека з маркером `.ltm-vault`)
  2. блоки `<!-- ltm:start -->` у файлах правил робочих проєктів
  3. файли правил, які створив саме установник, а не людина
  4. запис у планувальнику: cron, launchd або schtasks
  5. скіл у каталогах агентів (~/.claude/skills та інші)

Головний принцип: чуже не чіпаємо. Якщо `CLAUDE.md` у проєкті існував до нас,
з нього вирізається лише наш блок, а файл лишається. Що саме створив
установник, відомо з манифесту `.ltm-install-manifest.json` усередині пам'яті.
Якщо манифесту немає (стара установка), скрипт переходить на пошук за
ознаками і чесно каже, що діє за здогадом.
"""

from __future__ import annotations

__version__ = "1.1.0"

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

MARKER_FILE = ".ltm-vault"
MANIFEST = ".ltm-install-manifest.json"
BLOCK_START = "<!-- ltm:start -->"
BLOCK_END = "<!-- ltm:end -->"

CRON_MARKER = "ltm-vault-doctor"
LAUNCHD_LABEL = "md.ltm.vault.doctor"
SCHTASKS_NAME = "LTM Vault Doctor"

RULE_FILES = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")
SKILL_NAME = "ltm-vault"

# Каталоги, куди install.sh / install.ps1 кладуть скіл.
def skill_dirs() -> list[Path]:
    home = Path.home()
    out = [
        home / ".claude" / "skills" / SKILL_NAME,
        home / ".gemini" / "skills" / SKILL_NAME,
        home / ".config" / "amp" / "skills" / SKILL_NAME,
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        out.append(Path(appdata) / "amp" / "skills" / SKILL_NAME)
    return out


# Теки, у які немає сенсу заходити під час пошуку: там пам'яті не буває,
# а часу вони з'їдають більше за все інше разом узяте.
SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".cache",
    "Library", "AppData", ".Trash", ".local", "site-packages", ".npm",
    "Applications", "snap", ".rustup", ".cargo", "go",
}


def system() -> str:
    s = platform.system().lower()
    if s.startswith("win"):
        return "windows"
    if s == "darwin":
        return "macos"
    return "linux"


def ask_yes(prompt: str, default: bool = False) -> bool:
    d = "т/Н" if not default else "Т/н"
    try:
        raw = input(f"{prompt} [{d}]: ").strip().lower()
    except EOFError:
        return default
    if not raw:
        return default
    return raw in ("т", "так", "y", "yes", "д", "да")


# --- пошук слідів ------------------------------------------------------------

def find_vaults(explicit: str | None, deep: bool = False) -> list[Path]:
    """Знайти пам'ять за маркером `.ltm-vault`."""
    if explicit:
        p = Path(explicit).expanduser()
        return [p] if p.is_dir() else []

    home = Path.home()
    found: list[Path] = []
    seen: set[Path] = set()

    def add(p: Path) -> None:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            found.append(p)

    # Швидкий шлях: типові місця на глибині 1-2.
    for base in (home, home / "Documents", home / "memory", home / "Documents" / "memory"):
        if not base.is_dir():
            continue
        try:
            for marker in base.glob(f"*/{MARKER_FILE}"):
                add(marker.parent)
            for marker in base.glob(f"*/*/{MARKER_FILE}"):
                add(marker.parent)
        except OSError:
            continue

    if deep:
        # Глибокий пошук потрібен, коли людина поклала пам'ять кудись убік.
        for root, dirs, files in os.walk(home):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")
                       or d == ".obsidian" and False]
            if MARKER_FILE in files:
                add(Path(root))
                dirs[:] = []  # усередину пам'яті спускатися нема сенсу
    return found


def read_manifest(vault: Path) -> dict | None:
    p = vault / MANIFEST
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def find_linked_projects(deep: bool = False) -> list[Path]:
    """Знайти файли правил, у яких є наш блок.

    Шукаємо саме за маркером блока, а не за іменем файлу: `CLAUDE.md` є
    у безлічі проєктів, і жоден із них видаляти не можна.
    """
    home = Path.home()
    hits: list[Path] = []
    max_depth = 6 if deep else 3

    for root, dirs, files in os.walk(home):
        rel_depth = len(Path(root).relative_to(home).parts)
        if rel_depth >= max_depth:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name not in RULE_FILES:
                continue
            p = Path(root) / name
            try:
                if BLOCK_START in p.read_text(encoding="utf-8", errors="replace"):
                    hits.append(p)
            except OSError:
                continue
    return hits


def scheduler_state() -> tuple[bool, str]:
    """Чи стоїть запис у планувальнику цієї системи."""
    s = system()
    if s == "linux":
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        text = r.stdout if r.returncode == 0 else ""
        return CRON_MARKER in text, "crontab"
    if s == "macos":
        p = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        return p.is_file(), "launchd"
    r = subprocess.run(["schtasks", "/Query", "/TN", SCHTASKS_NAME],
                       capture_output=True, text=True)
    return r.returncode == 0, "schtasks"


# --- видалення ---------------------------------------------------------------

def remove_scheduler(dry: bool) -> list[str]:
    done: list[str] = []
    present, kind = scheduler_state()
    if not present:
        return done
    if dry:
        return [f"прибрати запис у {kind}"]

    s = system()
    if s == "linux":
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        existing = r.stdout if r.returncode == 0 else ""
        kept = [l for l in existing.splitlines() if CRON_MARKER not in l]
        body = "\n".join(kept).strip()
        body = body + "\n" if body else ""
        w = subprocess.run(["crontab", "-"], input=body, capture_output=True, text=True)
        done.append("crontab: запис прибрано" if w.returncode == 0
                    else f"crontab: помилка, {w.stderr.strip()}")
    elif s == "macos":
        p = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"],
                       capture_output=True, text=True)
        try:
            p.unlink()
            done.append("launchd: завдання прибрано")
        except OSError as e:
            done.append(f"launchd: помилка, {e}")
    else:
        r = subprocess.run(["schtasks", "/Delete", "/TN", SCHTASKS_NAME, "/F"],
                           capture_output=True, text=True)
        done.append("schtasks: завдання прибрано" if r.returncode == 0
                    else f"schtasks: помилка, {r.stderr.strip()}")
    return done


def strip_block(path: Path, dry: bool) -> str | None:
    """Вирізати наш блок із чужого файлу правил, сам файл лишити."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"{path}: не прочитано, {e}"
    if BLOCK_START not in text:
        return None
    start = text.index(BLOCK_START)
    try:
        end = text.index(BLOCK_END) + len(BLOCK_END)
    except ValueError:
        # Кінцевого маркера немає: файл правили руками. Не вгадуємо межу.
        return f"{path}: блок пошкоджений, приберіть вручну"
    new = (text[:start].rstrip() + "\n" + text[end:].lstrip()).strip()
    new = new + "\n" if new else ""
    if dry:
        return f"вирізати блок: {path}"
    try:
        if new.strip():
            path.write_text(new, encoding="utf-8")
            return f"блок вирізано: {path}"
        # Від файлу лишився самий заголовок: він був наш і без блока порожній.
        path.unlink()
        return f"файл спорожнів і видалений: {path}"
    except OSError as e:
        return f"{path}: не записано, {e}"


def remove_path(p: Path, dry: bool) -> str:
    if dry:
        return f"видалити: {p}"
    try:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        return f"видалено: {p}"
    except OSError as e:
        return f"НЕ видалено {p}: {e}"


def count_files(p: Path) -> int:
    n = 0
    for _, _, files in os.walk(p):
        n += len(files)
    return n


# --- звіт --------------------------------------------------------------------

def survey(args) -> dict:
    vaults = find_vaults(args.path, deep=args.deep)
    manifests = {str(v): read_manifest(v) for v in vaults}

    # Проєкти беремо з манифесту, якщо він є: це точне знання замість пошуку.
    linked: list[Path] = []
    from_manifest = False
    for v in vaults:
        m = manifests.get(str(v))
        if m and m.get("project_links"):
            from_manifest = True
            for item in m["project_links"]:
                p = Path(item["path"])
                if p.is_file():
                    linked.append(p)
    if not from_manifest:
        linked = find_linked_projects(deep=args.deep)

    skills = [d for d in skill_dirs() if d.is_dir()]
    sched, sched_kind = scheduler_state()
    return {
        "vaults": vaults,
        "manifests": manifests,
        "linked": sorted(set(linked)),
        "linked_from_manifest": from_manifest,
        "skills": skills,
        "scheduler": sched,
        "scheduler_kind": sched_kind,
    }


def print_survey(s: dict) -> bool:
    found_any = False
    print("Що знайдено на цій машині\n")

    if s["vaults"]:
        found_any = True
        print("Пам'ять:")
        for v in s["vaults"]:
            m = s["manifests"].get(str(v))
            mark = "манифест є" if m else "манифесту немає, дію за ознаками"
            print(f"  {v}   файлів: {count_files(v)}   ({mark})")
    else:
        print("Пам'ять: не знайдено")

    if s["linked"]:
        found_any = True
        src = "з манифесту" if s["linked_from_manifest"] else "знайдено пошуком"
        print(f"\nФайли правил із нашим блоком ({src}):")
        for p in s["linked"]:
            print(f"  {p}")
    else:
        print("\nФайли правил із нашим блоком: не знайдено")

    if s["skills"]:
        found_any = True
        print("\nСкіл у каталогах агентів:")
        for d in s["skills"]:
            print(f"  {d}")
    else:
        print("\nСкіл у каталогах агентів: не знайдено")

    print(f"\nПланувальник ({s['scheduler_kind']}): "
          + ("запис є" if s["scheduler"] else "запису немає"))
    if s["scheduler"]:
        found_any = True
    return found_any


def do_remove(s: dict, args) -> int:
    dry = args.dry_run
    actions: list[str] = []

    actions += remove_scheduler(dry)

    for p in s["linked"]:
        # Файл, який створив саме установник, видаляємо цілком.
        # Чужий файл лишається, з нього йде тільки блок.
        ours = False
        for v in s["vaults"]:
            m = s["manifests"].get(str(v)) or {}
            for item in m.get("project_links", []):
                if Path(item["path"]) == p and item.get("action") == "created":
                    ours = True
        if ours:
            actions.append(remove_path(p, dry))
        else:
            r = strip_block(p, dry)
            if r:
                actions.append(r)

    for d in s["skills"]:
        actions.append(remove_path(d, dry))

    if not args.keep_vault:
        for v in s["vaults"]:
            actions.append(remove_path(v, dry))

    print()
    for a in actions:
        print(f"  {a}")
    if not actions:
        print("  нічого прибирати")

    if dry:
        print("\nЦе був --dry-run, нічого не змінено.")
        return 0

    # Перевірка після роботи: звіт не заміняє факт.
    left = survey(args)
    rest = (len(left["vaults"]) if not args.keep_vault else 0) + len(left["linked"]) \
        + len(left["skills"]) + (1 if left["scheduler"] else 0)
    if rest:
        print(f"\nУВАГА: лишилося слідів: {rest}. Запусти ще раз із --deep.")
        return 1
    print("\nЧисто: слідів установки не лишилося.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Видалити довготривалу пам'ять і всі сліди установки")
    ap.add_argument("--path", help="шлях до пам'яті, якщо пошук її не бачить")
    ap.add_argument("--survey", action="store_true",
                    help="лише показати знайдене, нічого не чіпати")
    ap.add_argument("--dry-run", action="store_true",
                    help="показати, що буде видалено, без видалення")
    ap.add_argument("--deep", action="store_true",
                    help="глибокий пошук по всій домашній теці, повільніше")
    ap.add_argument("--keep-vault", action="store_true",
                    help="прибрати інтеграції, але саму пам'ять лишити")
    ap.add_argument("--yes", action="store_true",
                    help="без підтвердження, для автотестів")
    args = ap.parse_args()

    print("Видалення довготривалої пам'яті\n")
    s = survey(args)
    found = print_survey(s)

    if args.survey:
        return 0
    if not found:
        print("\nВидаляти нічого.")
        return 0
    if args.dry_run:
        return do_remove(s, args)

    total_files = sum(count_files(v) for v in s["vaults"]) if not args.keep_vault else 0
    print("\n" + "=" * 60)
    print("Це незворотна дія. Пам'ять локальна, копії немає.")
    if total_files:
        print(f"Буде видалено файлів пам'яті: {total_files}")
    print("=" * 60)

    if not args.yes:
        # Свідоме введення слова, а не 'т': видалення пам'яті надто дороге,
        # щоб спрацювати від випадкового натискання.
        try:
            word = input("Щоб підтвердити, надрукуй ВИДАЛИТИ: ").strip()
        except EOFError:
            word = ""
        if word != "ВИДАЛИТИ":
            print("Скасовано, нічого не змінено.")
            return 0

    return do_remove(s, args)


if __name__ == "__main__":
    sys.exit(main())
