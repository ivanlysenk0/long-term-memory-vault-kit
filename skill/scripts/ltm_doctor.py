#!/usr/bin/env python3
"""
ltm_doctor.py - перевірка здоров'я довготривалої пам'яті агентів (LTM vault).

Кросплатформений порт macOS-скриптів lint.sh + concept_scout.sh.
Працює однаково на Ubuntu, macOS і Windows. Залежностей немає, лише stdlib.

Використання:
    python3 ltm_doctor.py                 перевірити vault (шлях береться з оточення)
    python3 ltm_doctor.py --vault PATH    вказати шлях явно
    python3 ltm_doctor.py --scout         лише сканер кандидатів у knowledge/
    python3 ltm_doctor.py --all           scout, потім повна перевірка
    python3 ltm_doctor.py --json          машинний вивід для агента
    python3 ltm_doctor.py --quiet         лише підсумок

Шлях до vault шукається в такому порядку: --vault, змінна LTM_VAULT,
файл .ltm-vault поруч зі скриптом, поточна тека.

Код повернення: 0 якщо помилок немає (попередження припустимі), 1 якщо є помилки.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------- конфигурация

REQUIRED_FIELDS = ["title", "date", "project", "agent", "type", "tags", "status"]

# Каталоги вне графа знаний: источники и вырезки агент не проверяет.
SKIP_DIR_PARTS = {"Raw", "Clippings", ".git", ".obsidian", "node_modules", ".trash"}

# Файлы, которым frontmatter не нужен.
# Файлы правил и служебные корневые файлы: пишет человек, в графе знаний не участвуют.
SKIP_NAMES = {"BOARD.md", "AGENTS.md", "CLAUDE.md", "GEMINI.md", ".ltm-vault"}

# Служебные страницы, для которых orphan это норма.
HUB_NAMES = {
    "index.md", "log.md", "current-priorities.md", "hot.md", "BOARD.md",
    "master-index.md", "operations.md", "pending-concepts.md", "README.md",
    "AGENTS.md", "CLAUDE.md", "GEMINI.md",
}

# Одинаковые имена в разных проектах это норма для типовых страниц.
GENERIC_CONCEPT_NAMES = {
    "architecture.md", "database.md", "deploy.md", "stack.md",
    "integrations.md", "debugging.md",
}

STUB_WORDS = 200
SCOUT_THRESHOLD = 3

SESSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}_[a-z0-9-]+_.+\.md$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")

LINK_PLACEHOLDERS = {"ім'я-файлу", "имя-файла"}

TRIGGERS = [
    r"глибокий аналіз", r"детальн\w+ розбір", r"архітектур\w+", r"патерн",
    r"root cause", r"корінь причини", r"прийнят\w+ рішення", r"порівняння",
    r"висновок", r"висновки", r"еталон\w*", r"канонічн\w+", r"Карпаті", r"Karpathy",
    r"deep analysis", r"architecture", r"pattern", r"decision", r"comparison",
    r"trade-off", r"tradeoff", r"post-mortem", r"lessons learned",
]
TRIGGER_RES = [(t, re.compile(t, re.IGNORECASE)) for t in TRIGGERS]

# ------------------------------------------------------------------ утилиты

def supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if os.name == "nt":
        return os.environ.get("WT_SESSION") or os.environ.get("TERM")
    return True


class Palette:
    def __init__(self, on: bool):
        self.red = "\033[0;31m" if on else ""
        self.yellow = "\033[1;33m" if on else ""
        self.green = "\033[0;32m" if on else ""
        self.dim = "\033[2m" if on else ""
        self.off = "\033[0m" if on else ""


def read_text(path: Path) -> str:
    # Vault пишут разные агенты и ОС, поэтому кодировку не угадываем, а терпим.
    for enc in ("utf-8", "utf-8-sig", "cp1251"):
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, ValueError):
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def clean_link(link: str) -> str:
    """Із [[шлях\\|підпис#якір]] дістати чистий шлях.

    Obsidian вимагає екранувати | всередині таблиць, тому backslash знімаємо."""
    return link.replace("\\|", "|").split("|")[0].split("#")[0].strip()


def split_frontmatter(text: str):
    """Повернути (dict полів, тіло). Якщо frontmatter немає, повернути (None, text)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None, text
    fields: dict[str, str] = {}
    current_key = None
    for raw in lines[1:end]:
        if not raw.strip():
            continue
        if raw[0] in " \t-" and current_key:
            continue  # продолжение списка, например tags
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", raw)
        if m:
            current_key = m.group(1)
            fields[current_key] = m.group(2).strip()
    return fields, "\n".join(lines[end + 1:])


def find_vault(explicit: str | None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("LTM_VAULT")
    if env:
        candidates.append(Path(env))
    marker = Path(__file__).resolve().parent / ".ltm-vault"
    if marker.is_file():
        candidates.append(Path(read_text(marker).strip()))
    candidates.append(Path.cwd())
    for c in candidates:
        c = c.expanduser()
        if c.is_dir():
            return c.resolve()
    raise SystemExit(
        "Не знайшов vault. Вкажи шлях: --vault /path/to/vault, "
        "або задай змінну LTM_VAULT, або поклади шлях у файл .ltm-vault поруч зі скриптом."
    )


def collect_md(vault: Path) -> list[Path]:
    out = []
    for p in vault.rglob("*.md"):
        if SKIP_DIR_PARTS & set(p.relative_to(vault).parts[:-1]):
            continue
        if p.is_symlink():
            continue
        out.append(p)
    return sorted(out)


# ------------------------------------------------------------------ проверки

class Report:
    def __init__(self):
        self.errors: list[dict] = []
        self.warnings: list[dict] = []
        self.stats: dict = {}

    def error(self, check: str, msg: str, file: str = ""):
        self.errors.append({"check": check, "message": msg, "file": file})

    def warn(self, check: str, msg: str, file: str = ""):
        self.warnings.append({"check": check, "message": msg, "file": file})


def run_checks(vault: Path, files: list[Path], rep: Report) -> None:
    rel = lambda p: str(p.relative_to(vault)).replace("\\", "/")

    # Индексы строим один раз: иначе проверка ссылок и orphan-страниц
    # получится квадратичной и на большом vault будет ползти минутами.
    stems_all: set[str] = set()
    dirs_all: set[str] = set()
    for p in vault.rglob("*"):
        parts = set(p.relative_to(vault).parts)
        if SKIP_DIR_PARTS & parts and ".git" in parts:
            continue
        if p.is_dir():
            dirs_all.add(p.name)
        elif p.suffix == ".md":
            stems_all.add(p.stem)
    rel_paths = {rel(p)[:-3] for p in files}

    inbound: Counter = Counter()
    parsed: dict[Path, tuple[dict | None, str]] = {}

    for f in files:
        text = read_text(f)
        fm, body = split_frontmatter(text)
        parsed[f] = (fm, body)

        for link in WIKILINK_RE.findall(text):
            target = clean_link(link)
            if target:
                inbound[Path(target).name] += 1

    # 1 и 2: frontmatter и обязательные поля
    no_fm = 0
    missing_fields = 0
    for f in files:
        if f.name in SKIP_NAMES:
            continue
        fm, _ = parsed[f]
        if fm is None:
            rep.error("frontmatter", "немає YAML frontmatter", rel(f))
            no_fm += 1
            continue
        for field in REQUIRED_FIELDS:
            if field not in fm:
                rep.error("fields", f"немає поля '{field}'", rel(f))
                missing_fields += 1

    # 3: формат даты
    bad_dates = 0
    for f in files:
        fm, _ = parsed[f]
        if not fm:
            continue
        d = fm.get("date")
        if d and not DATE_RE.match(d.strip().strip('"').strip("'")):
            rep.error("date", f"дата не у форматі YYYY-MM-DD: '{d}'", rel(f))
            bad_dates += 1

    # 4: битые вики-ссылки
    # log.md и sessions/ это append-only история: они законно ссылаются на то,
    # что существовало на момент записи. Переписывать журнал нельзя, значит и
    # ругаться на его ссылки бессмысленно.
    broken = 0
    for f in files:
        if f.name == "log.md" or "sessions" in f.relative_to(vault).parts:
            continue
        text = read_text(f)
        for link in WIKILINK_RE.findall(text):
            target = clean_link(link)
            if not target or target in LINK_PLACEHOLDERS:
                continue
            name = Path(target).name
            # Ссылка с путём проверяется БУКВАЛЬНО. Поиск по короткому имени тут
            # недопустим: [[my-proj/log]] нашёл бы чужой log.md и выглядел живым,
            # хотя каталог называется My_Proj и переход в Obsidian сломан.
            if "/" in target:
                if target in rel_paths or (vault / target).exists() or (vault / f"{target}.md").exists():
                    continue
                rep.warn("links", f"розірване посилання [[{target}]]", rel(f))
                broken += 1
                continue
            # Короткая ссылка без пути: Obsidian ищет файл по имени во всём хранилище.
            if name in stems_all or name in dirs_all or (vault / target).exists():
                continue
            rep.warn("links", f"розірване посилання [[{target}]]", rel(f))
            broken += 1

    # 4б: ссылки, которые ВЫГЛЯДЯТ рабочими, но ведут не туда.
    # Два класса, оба не ловятся обычной проверкой существования файла:
    #   - неверный регистр или разделитель в имени проекта: [[my-proj/log]] при папке My_Proj
    #   - относительный путь: [[../knowledge/x]], Obsidian их не понимает
    project_names = {d.name for d in vault.iterdir()
                     if d.is_dir() and not d.name.startswith(".")}
    lower_map = {p.lower().replace("-", "_"): p for p in project_names}
    wrong_case = relative_links = 0
    for f in files:
        if f.name == "log.md" or "sessions" in f.relative_to(vault).parts:
            continue
        for link in WIKILINK_RE.findall(read_text(f)):
            target = clean_link(link)
            if not target or "/" not in target:
                continue
            if target.startswith("../") or target.startswith("./"):
                rep.warn("links", f"відносний шлях у посиланні [[{target}]]: "
                                  "Obsidian їх не розуміє, писати шлях від кореня пам'яті", rel(f))
                relative_links += 1
                continue
            first = target.split("/")[0]
            if first in project_names:
                continue
            real = lower_map.get(first.lower().replace("-", "_"))
            if real:
                rep.warn("links", f"хибна назва проєкту в посиланні [[{target}]]: "
                                  f"каталог зветься '{real}', а не '{first}'", rel(f))
                wrong_case += 1

    rep.stats_extra = {"wrong_case_links": wrong_case, "relative_links": relative_links}
    broken += wrong_case + relative_links

    # 5: дубли имён концепт-страниц
    dupes = defaultdict(list)
    for f in files:
        parts = set(f.relative_to(vault).parts)
        if not ({"knowledge", "atlas"} & parts):
            continue
        if f.name in GENERIC_CONCEPT_NAMES or f.name in HUB_NAMES:
            continue
        dupes[f.name].append(rel(f))
    dupe_count = 0
    for name, paths in dupes.items():
        if len(paths) > 1:
            rep.warn("duplicates", f"дубль назви концепт-сторінки: {name} ({len(paths)} шт)",
                     "; ".join(paths))
            dupe_count += 1

    # 6: имена session-файлов
    bad_sessions = 0
    for f in files:
        if f.parent.name != "sessions":
            continue
        if not SESSION_RE.match(f.name):
            rep.warn("sessions", "назва не у форматі YYYY-MM-DD_HHMM_agent_topic.md", rel(f))
            bad_sessions += 1

    # 7: orphan-страницы
    orphans = 0
    for f in files:
        if f.name in HUB_NAMES or "sessions" in f.relative_to(vault).parts:
            continue
        if inbound.get(f.stem, 0) == 0:
            rep.warn("orphans", "немає вхідних вікі-посилань", rel(f))
            orphans += 1

    # 8: страницы-заглушки
    stubs = 0
    for f in files:
        if f.name in HUB_NAMES or "sessions" in f.relative_to(vault).parts:
            continue
        _, body = parsed[f]
        words = len(body.split())
        if 0 < words < STUB_WORDS:
            rep.warn("stubs", f"заготовка, {words} слів", rel(f))
            stubs += 1

    # 9: неоткомпилированные кандидаты
    pending = 0
    for pf in vault.rglob("pending-concepts.md"):
        if ".git" in pf.parts:
            continue
        text = read_text(pf)
        blocks = re.split(r"\n(?=### )", text)
        for b in blocks:
            if "Status: pending" not in b:
                continue
            m = WIKILINK_RE.search(b)
            session = m.group(1).split("|")[-1] if m else "?"
            rep.warn("pending", f"не зібрано в knowledge/: {session}", rel(pf))
            pending += 1

    rep.stats = {
        "files": len(files),
        "no_frontmatter": no_fm,
        "missing_fields": missing_fields,
        "bad_dates": bad_dates,
        "broken_links": broken,
        "duplicate_names": dupe_count,
        "bad_session_names": bad_sessions,
        "orphans": orphans,
        "stubs": stubs,
        "pending_concepts": pending,
    }


# -------------------------------------------------------------------- scout

def run_scout(vault: Path, recent_hours: int | None, verbose: bool = True) -> list[dict]:
    """Позначити session-логи, які час збирати в knowledge/."""
    queued = []
    cutoff = datetime.now() - timedelta(hours=recent_hours) if recent_hours else None

    for sessions_dir in sorted(vault.glob("*/sessions")):
        project_dir = sessions_dir.parent
        project = project_dir.name
        pending = project_dir / "00-home" / "pending-concepts.md"
        for sf in sorted(sessions_dir.glob("*.md")):
            if cutoff and datetime.fromtimestamp(sf.stat().st_mtime) < cutoff:
                continue
            if pending.is_file() and sf.stem in read_text(pending):
                continue
            _, body = split_frontmatter(read_text(sf))
            if not body.strip():
                continue
            matched = [t for t, rx in TRIGGER_RES if rx.search(body)]
            if len(matched) < SCOUT_THRESHOLD:
                continue
            _append_pending(pending, project, sf, vault, matched)
            queued.append({"project": project, "session": sf.stem, "keywords": len(matched)})
            if verbose:
                print(f"QUEUED: {sf.stem} ({len(matched)} тригерів) - {project}")
    return queued


def _append_pending(pending: Path, project: str, session: Path, vault: Path, matched: list[str]) -> None:
    if not pending.is_file():
        pending.parent.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        pending.write_text(
            "---\n"
            'title: "Черга session-логів на збирання в knowledge/"\n'
            f"date: {today}\nproject: {project}\nagent: ltm-doctor\ntype: pattern\n"
            "tags:\n  - karpathy\n  - automation\n  - concept-scout\n"
            "status: active\nsources: 0\n---\n\n"
            "# Pending concepts queue\n\n"
            "> Заповнює ltm_doctor --scout. Обробляє агент.\n\n"
            "## Active queue\n",
            encoding="utf-8",
        )
    relp = str(session.relative_to(vault)).replace("\\", "/")[:-3]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    with pending.open("a", encoding="utf-8") as fh:
        fh.write(
            f"\n### {ts}\n"
            f"- Session: [[{relp}|{session.stem}]]\n"
            f"- Trigger keywords matched: {len(matched)}\n"
            f"- Keywords: {', '.join(matched)}\n"
            f"- Status: pending\n"
            f"- Suggested action: витягти концепт(и) у knowledge/<category>/, оновити index.md і log.md\n"
        )


# --------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Лікар довготривалої пам'яті агентів")
    ap.add_argument("--vault", help="шлях до vault")
    ap.add_argument("--scout", action="store_true", help="лише сканер кандидатів")
    ap.add_argument("--all", action="store_true", help="scout, потім перевірка")
    ap.add_argument("--recent", type=int, metavar="HOURS", help="scout лише по свіжих сесіях")
    ap.add_argument("--json", action="store_true", help="машинний вивід")
    ap.add_argument("--quiet", action="store_true", help="лише підсумок")
    args = ap.parse_args()

    vault = find_vault(args.vault)
    c = Palette(supports_color() and not args.json)

    if args.scout or args.all:
        if not args.json:
            print(f"--- scout: {vault} ---")
        queued = run_scout(vault, args.recent, verbose=not args.json)
        if args.scout:
            if args.json:
                print(json.dumps({"vault": str(vault), "queued": queued}, ensure_ascii=False, indent=2))
            else:
                print(f"Поставлено в чергу: {len(queued)}")
            return 0

    files = collect_md(vault)
    rep = Report()
    run_checks(vault, files, rep)

    if args.json:
        print(json.dumps({
            "vault": str(vault),
            "stats": rep.stats,
            "errors": rep.errors,
            "warnings": rep.warnings,
        }, ensure_ascii=False, indent=2))
        return 1 if rep.errors else 0

    print(f"=== Перевірка пам'яті: {vault} ===")
    print(f"Файлів .md у графі: {rep.stats['files']}\n")

    if not args.quiet:
        for item in rep.errors:
            print(f"{c.red}ПОМИЛКА{c.off} [{item['check']}] {item['message']}  {c.dim}{item['file']}{c.off}")
        for item in rep.warnings:
            print(f"{c.yellow}УВАГА{c.off} [{item['check']}] {item['message']}  {c.dim}{item['file']}{c.off}")
        if rep.errors or rep.warnings:
            print()

    labels = [
        ("no_frontmatter", "без frontmatter"),
        ("missing_fields", "немає обов'язкових полів"),
        ("bad_dates", "хибна дата"),
        ("broken_links", "розірвані посилання"),
        ("duplicate_names", "дублі назв"),
        ("bad_session_names", "назви сесій не за форматом"),
        ("orphans", "orphan-сторінки"),
        ("stubs", "заготовки"),
        ("pending_concepts", "чекають на збирання"),
    ]
    print("--- Підсумок ---")
    for key, label in labels:
        print(f"  {label}: {rep.stats[key]}")
    print(f"\nПомилок: {c.red}{len(rep.errors)}{c.off}   Попереджень: {c.yellow}{len(rep.warnings)}{c.off}")

    if not rep.errors and not rep.warnings:
        print(f"{c.green}Пам'ять здорова.{c.off}")
        return 0
    if not rep.errors:
        print(f"{c.yellow}Помилок немає, є попередження.{c.off}")
        return 0
    print(f"{c.red}Є помилки, пам'ять потребує правок.{c.off}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
