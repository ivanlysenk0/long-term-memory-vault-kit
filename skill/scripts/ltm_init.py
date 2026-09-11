#!/usr/bin/env python3
"""
ltm_init.py - інтерактивне розгортання довготривалої пам'яті агентів.

Створює локальний vault за еталонною структурою, ставить скрипт-лікар,
підключає Claude Code і AMP через файли правил. Без git, без хмари:
пам'ять живе лише на цій машині.

Працює на Ubuntu, macOS і Windows. Залежностей немає, лише stdlib.

Використання:
    python3 ltm_init.py                    інтерактивно, поставить питання
    python3 ltm_init.py --check            лише діагностика, нічого не змінювати
    python3 ltm_init.py --update           оновити скрипти в наявній пам'яті
    python3 ltm_init.py --migrate          оновити структуру і правила на місці
    python3 ltm_init.py --version          яка версія скіла і яка в пам'яті
    python3 ltm_init.py --path DIR --projects a,b --yes    без питань

Скрипт ідемпотентний: повторний запуск не перезаписує готові файли,
а лише дописує те, чого бракує. Саме тому оновлення виконуваних скриптів
винесене в окремий режим `--update`: установка їх навмисно не чіпає.
"""

from __future__ import annotations

__version__ = "1.1.0"

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Провайдер -> имя файла правил, который читает его агент.
# Спрашиваем у пользователя, не угадываем: у человека может стоять несколько сразу.
PROVIDERS = {
    "claude": {"file": "CLAUDE.md", "label": "Claude Code"},
    "amp": {"file": "AGENTS.md", "label": "AMP Code"},
    "gemini": {"file": "GEMINI.md", "label": "Gemini CLI"},
}
ALL_RULE_FILES = [v["file"] for v in PROVIDERS.values()]

SUBDIRS = ["00-home", "atlas", "knowledge", "sessions", "Raw", "scripts", "tasks"]
KNOWLEDGE_CATS = ["decisions", "patterns", "debugging", "integrations", "analyses", "research"]
TODAY = datetime.now().strftime("%Y-%m-%d")

created: list[str] = []
skipped: list[str] = []
# Сухий режим: write_once нічого не пише, лише збирає перелік у planned.
# Потрібен для `--migrate --dry-run`: показати людині, що саме зміниться,
# ДО того, як щось торкнеться її пам'яті.
DRY_RUN = False
planned: list[str] = []
# Що саме ми зробили в чужих проєктах. Потрібно для чесного видалення:
# створений нами файл прибирається цілком, чужий лише звільняється від блока.
link_records: list[dict] = []


def fm(title: str, project: str, ftype: str, tags: list[str]) -> str:
    tag_lines = "\n".join(f"  - {t}" for t in tags)
    return (
        f"---\ntitle: \"{title}\"\ndate: {TODAY}\nproject: {project}\n"
        f"agent: ltm-init\ntype: {ftype}\ntags:\n{tag_lines}\n"
        f"status: active\nsources: 0\n---\n\n"
    )


def write_once(path: Path, content: str) -> None:
    """Ніколи не затирати наявний файл: пам'ять дорожча за шаблон."""
    if path.exists():
        skipped.append(str(path))
        return
    if DRY_RUN:
        planned.append(str(path))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    created.append(str(path))


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        val = ""
    return val or default


def ask_yes(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    try:
        val = input(f"{prompt} ({d}): ").strip().lower()
    except EOFError:
        val = ""
    if not val:
        return default
    return val in ("y", "yes", "т", "так", "д", "да")


def default_vault_path() -> Path:
    # Без проміжної теки: пам'ять кладеться прямо в домашній каталог.
    # Раніше тут було home/memory/long-term-memory-vault, і на Windows
    # користувач отримував зайвий рівень вкладеності.
    return Path.home() / "long-term-memory-vault"


# ------------------------------------------------------------- диагностика

def diagnose(vault: Path) -> None:
    print("=== Діагностика ===")
    print(f"ОС: {sys.platform}   Python: {sys.version.split()[0]}")
    print(f"Цільовий vault: {vault}")
    if vault.exists():
        md = [p for p in vault.rglob("*.md") if ".git" not in p.parts]
        projects = sorted(d.name for d in vault.iterdir()
                          if d.is_dir() and not d.name.startswith(".") and (d / "00-home").is_dir())
        print(f"  vault уже існує: {len(md)} файлів .md")
        print(f"  проєктів за шаблоном: {len(projects)}")
        if projects:
            print(f"  перелік: {', '.join(projects)}")
        loose = [d.name for d in vault.iterdir()
                 if d.is_dir() and not d.name.startswith(".") and not (d / "00-home").is_dir()]
        if loose:
            print(f"  теки без 00-home (не за шаблоном): {', '.join(loose)}")
        print(f"  master-index.md: {'є' if (vault / '00-global-home' / 'master-index.md').is_file() else 'НЕМАЄ'}")
        print(f"  лікар: {'є' if (vault / 'scripts' / 'ltm_doctor.py').is_file() else 'НЕМАЄ'}")
    else:
        print("  vault поки немає, буде створений з нуля")

    claude = Path.home() / ".claude"
    print(f"Claude Code: {'знайдено ~/.claude' if claude.is_dir() else 'теку ~/.claude не знайдено'}")
    print(f"git у PATH: {'так' if shutil.which('git') else 'ні'} (для локальної пам'яті не обов'язковий)")
    print()


# ------------------------------------------------------------- генерация

def make_project(vault: Path, project: str) -> None:
    pdir = vault / project
    for sub in SUBDIRS:
        (pdir / sub).mkdir(parents=True, exist_ok=True)
    for cat in KNOWLEDGE_CATS:
        (pdir / "knowledge" / cat).mkdir(parents=True, exist_ok=True)
        write_once(pdir / "knowledge" / cat / ".gitkeep", "")

    write_once(pdir / "00-home" / "index.md",
        fm(f"{project}: індекс", project, "index", ["navigation", "index"]) +
        f"# {project}\n\nВхідна точка проєкту. Агент починає читання звідси.\n\n"
        "## Що це за проєкт\n\nОписати одним абзацом: що робимо, навіщо, для кого.\n\n"
        "## Навігація\n\n"
        "- [[current-priorities]]: що робимо просто зараз\n"
        "- [[hot]]: гарячі факти і відкриті питання\n"
        "- [[log]]: хронологічний журнал\n\n"
        "## Знання\n\nСюди додаємо посилання на кожну нову сторінку в knowledge/ з однорядковим описом.\n")

    write_once(pdir / "00-home" / "current-priorities.md",
        fm(f"{project}: пріоритети", project, "index", ["priorities"]) +
        "# Поточні пріоритети\n\n1. \n2. \n3. \n\n"
        "Посилання назад: [[index]]\n")

    write_once(pdir / "00-home" / "hot.md",
        fm(f"{project}: гаряче", project, "index", ["hot"]) +
        "# Гаряче\n\nФакти, які потрібні часто, і відкриті питання.\n\n"
        "Посилання назад: [[index]]\n")

    write_once(pdir / "log.md",
        fm(f"{project}: журнал", project, "index", ["log"]) +
        f"# Журнал\n\n## {TODAY} init | створено проєкт у пам'яті\n")

    write_once(pdir / "Raw" / "README.md",
        "# Raw\n\nСюди кладе матеріали лише людина. Агент читає і ніколи не змінює.\n"
        "Лише текст: транскрипції, витяги, метадані. Відео й аудіо сюди не кладемо.\n")

    # Слой Schema проекта. Без него правила расползаются по агентским файлам
    # и через месяц расходятся между собой.
    write_once(pdir / "00-home" / "operations.md",
        fm(f"Vault Operations: {project}", project, "operations",
           ["workflow", "ingest", "query", "lint"]) +
        f"""# Vault Operations: {project}

Правила роботи агента з пам'яттю цього проєкту. Спільні правила для всіх проєктів:
[[00-global-home/00-home/operations|глобальний operations.md]].

## Шари

| Шар | Де | Хто пише |
|------|-----|-----------|
| Raw | `Raw/` | лише людина, агент читає і ніколи не змінює |
| Wiki | `knowledge/`, `atlas/`, `00-home/` | агент |
| Schema | цей файл | людина |

## Ingest

1. Первинний матеріал у `Raw/` з посиланням на джерело і датою.
2. Перевірити індекс: якщо сторінка за темою вже є, оновлюємо її, а не створюємо другу.
3. До створення чи суттєвої правки показати власнику зміни і отримати схвалення.
4. Оновити `00-home/index.md` і `log.md`, прогнати лікаря.

## Query

1. Почати з [[{project}/00-home/index|індексу проєкту]].
2. Читати knowledge-сторінки та їхні первинні джерела.
3. У відповіді називати файли, з яких узяті факти.

## Межі проєкту

Опиши тут, що належить до проєкту, а що ні. Порожній розділ означає,
що межі не задані і агент тягтиме сюди чуже.

## Lint

`python3 scripts/ltm_doctor.py` з кореня пам'яті. Нова knowledge-сторінка мусить
отримати вхідне посилання з `index.md`, інакше вона orphan і агент до неї не дійде.
""")

    write_once(pdir / "00-home" / "pending-concepts.md",
        fm(f"{project}: черга на збирання", project, "index", ["pending"]) +
        "# Черга на збирання\n\nSession-логи, з яких ще не вийняті концепти в `knowledge/`.\n"
        "Заповнює команда `ltm_doctor.py --scout`.\n")


def make_global_home(vault: Path, projects: list[str], providers: list[str] | None = None) -> None:
    providers = providers or ["claude", "amp"]
    gh = vault / "00-global-home"
    for sub in ["00-home", "knowledge", "Raw", "sessions", "tasks"]:
        (gh / sub).mkdir(parents=True, exist_ok=True)

    mi = gh / "master-index.md"
    if mi.is_file():
        # Повторный запуск: не перезаписываем индекс, а дописываем недостающие проекты.
        text = mi.read_text(encoding="utf-8")
        missing = [p for p in projects if f"[[{p}/00-home/index" not in text]
        if missing:
            add = "\n".join(f"| [[{p}/00-home/index\\|{p}]] | Active | [[{p}/00-home/index]] |" for p in missing)
            lines = text.rstrip().splitlines()
            last_row = max((i for i, l in enumerate(lines) if l.startswith("| [[")), default=len(lines) - 1)
            lines[last_row + 1:last_row + 1] = add.splitlines()
            if DRY_RUN:
                planned.append(f"{mi} (+{len(missing)} проєктів)")
            else:
                mi.write_text("\n".join(lines) + "\n", encoding="utf-8")
                created.append(f"{mi} (+{len(missing)} проєктів)")
    # Раніше тут стояв `return`, і при наявному master-index функція виходила
    # достроково: `log.md`, `00-home/operations.md` і `00-home/index.md` так
    # і не з'являлися в старих установках. Виходу немає, бо `write_once`
    # наявні файли й так не чіпає.

    rows = "\n".join(
        ["| [[00-global-home/00-home/index\\|Знання спільного рівня]] | Active | [[00-global-home/00-home/index]] |"]
        + [f"| [[{p}/00-home/index\\|{p}]] | Active | [[{p}/00-home/index]] |" for p in projects])
    rule_lines = "\n".join(
        f"- `{PROVIDERS[k]['file']}`: правила для {PROVIDERS[k]['label']}" for k in providers)
    write_once(mi,
        fm("Master Index", "global", "index", ["navigation", "index"]) +
        "# Master Index\n\nТочка входу в довготривалу пам'ять. Будь-який запит починається звідси.\n\n"
        "## Перш ніж працювати з пам'яттю\n\n"
        "Прочитати правила роботи з нею:\n\n"
        "- [[00-global-home/00-home/operations|Спільні правила]]: шари, Query, Query -> Save, формат, вікі-посилання\n"
        "- `<проєкт>/00-home/operations.md`: правила конкретного проєкту, вони в проєктів різні\n\n"
        "Короткі вказівники для агента, відкритого просто в корені пам'яті:\n\n"
        + rule_lines + "\n\n"
        "Вміст файлів однаковий, різняться лише назви: різні агенти читають різні назви.\n\n"
        "## Проєкти\n\n| Проєкт | Статус | Вхідна точка |\n|--------|--------|---------------|\n"
        + rows + "\n\n"
        "## Авторство записів\n\n"
        "Поле `agent` у шапці файла відповідає на питання «хто записав», поле `date` на питання «коли».\n"
        "Пам'ять зараз локальна, але ці поля обов'язкові вже зараз: без них перехід\n"
        "на спільну пам'ять кількох людей потребуватиме переписування всіх файлів.\n")

    write_once(gh / "log.md",
        fm("Глобальний журнал", "global", "index", ["log"]) +
        f"# Глобальний журнал\n\n## {TODAY} init | пам'ять розгорнуто локально\n")

    # Глобальный operations: правила ЖИВУТ ВНУТРИ памяти, а не в файле настройки
    # машины. Иначе при копировании памяти на другую машину правила не поедут.
    write_once(gh / "00-home" / "operations.md",
        fm("Vault Operations: глобальні правила", "global", "operations",
           ["workflow", "ingest", "query", "lint"]) +
        """# Vault Operations: глобальні правила

Спільні правила роботи з пам'яттю. Правила конкретного проєкту лежать у
`<проєкт>/00-home/operations.md` і мають пріоритет, якщо щось уточнюють.

Файл живе всередині пам'яті навмисно: при копіюванні пам'яті на іншу машину
правила їдуть разом із нею, і агенту не потрібен зовнішній файл налаштувань.

## Шари

| Шар | Де | Хто пише |
|------|-----|-----------|
| Raw | `<проєкт>/Raw/` | лише людина |
| Wiki | `knowledge/`, `atlas/`, `00-home/` | агент |
| Schema | файли правил у корені пам'яті та `operations.md` | людина |

## Query

1. `00-global-home/master-index.md`
2. `<проєкт>/00-home/index.md`
3. `<проєкт>/00-home/operations.md`, правила проєктів відрізняються
4. `knowledge/`, потім `log.md` і свіжі `sessions/`

За один запит читати від 10 до 50 файлів. У відповіді називати файли-джерела.

## Query -> Save

Зберігати, якщо в сесії з'явилося: новий зв'язок між сутностями, синтез із різних
джерел, порівняння підходів з обґрунтуванням, root cause бага, архітектурний висновок.
Не зберігати просту видачу факту і коротке уточнення.

Без концепт-сторінки знання лишиться тільки в session-лозі, а структурний lint
session-логи не перевіряє. Session-логи дешеві, концепт-сторінки дорогі.

## Обов'язковий формат

Кожен .md починається з YAML: `title`, `date`, `project`, `agent`, `type`, `tags`, `status`.

## Вікі-посилання

- У кожної концепт-сторінки щонайменше одне вхідне посилання, інакше вона orphan.
- Посилання двосторонні: якщо A посилається на B, то B посилається на A.
- Шлях пишеться від кореня пам'яті. Відносні шляхи з `../` Obsidian не розуміє.
- Регістр назви проєкту важливий: посилання на `my-proj/log` при каталозі `My_Proj`
  веде в нікуди, хоча виглядає робочим.
- Session-логи в граф не лінкуються.

## Один документ на одну тему

Сторінка за темою одна. Застаріла позначається `status: superseded` з посиланням
на заміну, а не дублюється другим файлом з іншим станом.

## Первинні матеріали: лише текст

Відео й аудіо в пам'ять не кладемо: якщо пам'ять під git, бінарник лишиться
в історії назавжди. Транскрибуємо в тимчасовій теці, зберігаємо текст.

## Перевірка здоров'я

`python3 scripts/ltm_doctor.py` з кореня пам'яті. ПОМИЛКА лагодиться одразу,
УВАГА розбирається по ходу роботи.

## Наприкінці сесії

Створити `<проєкт>/sessions/YYYY-MM-DD_HHMM_<агент>_<тема>.md`, оновити
`current-priorities.md` і `hot.md`, дописати в `log.md`.
""")

    # Индекс глобального проекта. Без него знания общего уровня начинают
    # перечисляться прямо в master-index, и он раздувается.
    write_once(gh / "00-home" / "index.md",
        fm("00-global-home: індекс", "global", "index", ["navigation", "index"]) +
        """# 00-global-home: індекс

> **Правила роботи з пам'яттю: [[operations]].** Прочитати до запису в пам'ять.

Точка входу в знання спільного рівня: те, що застосовне до кількох проєктів одразу
і не належить жодному з них. Навігація між проєктами живе окремо,
у [[00-global-home/master-index|Master Index]].

## Операційний кеш

- [[operations]]: спільні правила роботи з пам'яттю
- [[00-global-home/log|log.md]]: хронологічний журнал

## Знання

Розділи відповідають підтекам `knowledge/`. Кожна сторінка вказується один раз,
з однорядковим описом. Списків «обраного» за важливістю тут бути не повинно:
розділи організовані за категоріями вмісту.
""")


def make_rules(vault: Path, projects: list[str]) -> str:
    plist = "\n".join(f"- `{p}/`" for p in projects)
    return f"""# Правила довготривалої пам'яті

Цей файл агент читає на початку кожної сесії. Він описує, як влаштована
пам'ять і що агент зобов'язаний з нею робити.

## Де пам'ять і з чого починати

Корінь: цей каталог. Пам'ять локальна, синхронізації немає.
Єдина копія живе на цій машині, тому бекап це відповідальність власника.

**Точка входу в пам'ять це `00-global-home/master-index.md`, а не цей файл.**
Цей файл описує, ЯК працювати з пам'яттю. Master-index описує, ЩО в ній є.
Порядок такий: прочитати ці правила, потім іти в master-index і далі за посиланнями.

## Структура

```
<vault>/
├── 00-global-home/       глобальна навігація
│   └── master-index.md   canonical entry point, читання починається тут
├── <project>/
│   ├── 00-home/          index.md, current-priorities.md, hot.md, pending-concepts.md
│   ├── atlas/            архітектура, стек, БД, деплой
│   ├── knowledge/        decisions, patterns, debugging, integrations, analyses, research
│   ├── sessions/         логи сесій YYYY-MM-DD_HHMM_agent_topic.md
│   ├── Raw/              джерела, пише лише людина
│   ├── scripts/          локальні утиліти проєкту
│   └── log.md            append-only журнал
└── scripts/ltm_doctor.py перевірка здоров'я пам'яті
```

Проєкти зараз:
{plist}

## Шари і права

- `Raw/` пише лише людина. Агент читає, ніколи не змінює.
- `atlas/`, `knowledge/`, `00-home/` веде агент.
- Цей файл правил пише людина.
- Session-логи це сировина, а не канон. Канон живе в `knowledge/`.

## Формат файла

Кожен .md починається з YAML frontmatter:

```yaml
---
title: "Опис"
date: YYYY-MM-DD
project: <project>
agent: claude-code
type: <atlas|integration|decision|debugging|pattern|business|analysis|session>
tags: [tag1, tag2]
status: active
sources: 0
---
```

## Операція Query, відповідь на питання

1. `00-global-home/master-index.md`
2. `<project>/00-home/index.md`
3. `<project>/knowledge/decisions/`
4. `<project>/knowledge/patterns/`
5. `<project>/00-home/current-priorities.md`
6. `<project>/00-home/hot.md`
7. `<project>/log.md`
8. свіжі файли в `<project>/sessions/`

Правило 10-50: за один запит читати від 10 до 50 файлів, не більше.
У відповіді називати файли, з яких узяті факти.
Не відповідати «нічого не знайдено», доки цей шлях не пройдено до кінця.

## Операція Save, що зберігати обов'язково

- нові зв'язки між сутностями
- синтез ідей із різних джерел
- порівняння підходів і обґрунтування вибору
- баг-фікс і його root cause
- архітектурний висновок або новий патерн
- оновлення поведінки вже описаної концепції

Не зберігати: просту видачу факту, коротке уточнення, те, що вже цілком є на сторінці.

## При створенні нової сторінки в knowledge/

1. додати посилання в `<project>/00-home/index.md` з однорядковим описом
2. дописати рядок у `<project>/log.md`
3. проставити двосторонні вікі-посилання зі спорідненими сторінками
4. наприкінці сторінки секція `## Джерела` з посиланням на session-файл
5. збільшити `sources:` у frontmatter

Правило вікі-посилань: у кожної концепт-сторінки щонайменше одне вхідне посилання,
інакше вона orphan і лікар її позначить. Session-логи не лінкуються.

## Наприкінці сесії

- створити `<project>/sessions/YYYY-MM-DD_HHMM_<agent>_<topic>.md`
- оновити `current-priorities.md` і `hot.md`
- дописати запис у `log.md`
- запустити `python3 scripts/ltm_doctor.py` і полагодити те, що він показав як ПОМИЛКА

## Перевірка здоров'я

```
python3 scripts/ltm_doctor.py           повна перевірка
python3 scripts/ltm_doctor.py --scout   позначити сесії, які час збирати
python3 scripts/ltm_doctor.py --all     скаут, потім перевірка
python3 scripts/ltm_doctor.py --json    машинний вивід для агента
```

ПОМИЛКА лагодиться одразу. УВАГА розбирається в міру роботи над проєктом.

## Колективна пам'ять: правила діють уже зараз

Зараз пам'ять локальна, копія одна, на цій машині. У майбутньому її можуть об'єднати
на кількох людей. Транспорт об'єднання поки не обраний. Щоб перехід не потребував
переписування всіх файлів, ці правила діють із першого дня:

- **Авторство обов'язкове.** Поле `agent` у шапці відповідає «хто записав», `date` відповідає
  «коли». Без них у спільній пам'яті неможливо зрозуміти, чий це запис і чи актуальний він
- **Один документ на одну тему.** Якщо за темою вже є сторінка, оновлюємо її.
  Другий файл за тією ж темою не створюємо. У спільній пам'яті два файли про одне це
  гарантовано хибна відповідь агента
- **Застаріле не видаляємо.** Ставимо `status: superseded` у шапці і посилання на заміну
  в першому рядку тіла. Видалена сторінка виглядає як така, що ніколи не існувала,
  а в колеги вона може бути ще відкрита
- **`log.md` лише дописується.** Рядки не переписуємо і не видаляємо. Це єдиний
  спосіб зрозуміти, хто і що змінював, коли тих, хто пише, стане кілька
- **Особисте відокремлене від спільного.** Те, що не має поїхати до колег, тримати в проєкті
  з явною позначкою в `index.md`, а не впереміш
- **Правки атомарні.** Одна тема, один файл, за раз. Великі переписування всього проєкту
  у спільній пам'яті перетворюються на нерозв'язний конфлікт

Коли транспорт оберуть (спільний git, мережева тека чи сервер), ці правила не змінюються,
додасться лише процедура синхронізації.

## Стиль

Довгі тире не використовувати ніде: ні у файлах пам'яті, ні у відповідях.
Замінювати комою, двокрапкою, дужками або крапкою.
"""


MANIFEST = ".ltm-install-manifest.json"


def write_manifest(vault: Path, projects: list[str], providers: list[str]) -> None:
    """Записати, що саме створила установка.

    Без цього видалення діє за здогадом: воно не може відрізнити `CLAUDE.md`,
    який ми створили, від того, що людина писала сама півроку тому.
    Манифест доповнюється, а не переписується: друга установка в ті самі
    проєкти не повинна стирати пам'ять про першу.
    """
    p = vault / MANIFEST
    data = {"version": 1, "installs": []}
    if p.is_file():
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(old, dict) and isinstance(old.get("installs"), list):
                data = old
        except (ValueError, OSError):
            pass

    known = {(r["path"], r["action"]) for r in data.get("project_links", [])}
    merged = list(data.get("project_links", []))
    for rec in link_records:
        if (rec["path"], rec["action"]) not in known:
            merged.append(rec)

    data["vault"] = str(vault)
    data["project_links"] = merged
    data["installs"].append({
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "projects": projects,
        "providers": providers,
        "created": len(created),
    })
    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"  УВАГА: манифест не записано ({e}). Видалення діятиме за ознаками.")


def install_doctor(vault: Path) -> None:
    # Планувальник кладемо поруч із лікарем: без нього регулярна перевірка
    # лишається порадою в тексті, яку ніхто не виконає.
    for helper in ("ltm_schedule.py", "ltm_seed.py", "ltm_uninstall.py"):
        h_src = Path(__file__).resolve().parent / helper
        if not h_src.is_file():
            continue
        h_dst = vault / "scripts" / helper
        h_dst.parent.mkdir(parents=True, exist_ok=True)
        if h_dst.exists():
            skipped.append(str(h_dst))
        else:
            shutil.copy2(h_src, h_dst)
            created.append(str(h_dst))
            if os.name != "nt":
                try:
                    h_dst.chmod(0o755)
                except OSError:
                    pass

    src = Path(__file__).resolve().parent / "ltm_doctor.py"
    dst = vault / "scripts" / "ltm_doctor.py"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not src.is_file():
        print(f"  УВАГА: поруч немає ltm_doctor.py, скопіюй його в {dst} вручну")
        return
    if dst.exists():
        skipped.append(str(dst))
    else:
        shutil.copy2(src, dst)
        created.append(str(dst))
    if os.name != "nt":
        dst.chmod(0o755)

    # Windows: батник, чтобы доктор запускался двойным кликом и из cmd.
    if os.name == "nt":
        bat = vault / "scripts" / "ltm_doctor.bat"
        write_once(bat, f'@echo off\r\nchcp 65001 >nul\r\npython "%~dp0ltm_doctor.py" %*\r\n')
    else:
        sh = vault / "scripts" / "ltm_doctor.sh"
        write_once(sh, '#!/usr/bin/env bash\nexec python3 "$(dirname "$0")/ltm_doctor.py" "$@"\n')
        if sh.exists():
            sh.chmod(0o755)


VAULT_SCRIPTS = ("ltm_doctor.py", "ltm_schedule.py", "ltm_seed.py", "ltm_uninstall.py")


def rules_hash(text: str) -> str:
    """Відбиток згенерованого файлу правил.

    Потрібен міграції, щоб відрізнити файл, якого людина не чіпала, від того,
    який вона правила руками. Перший можна оновити мовчки, другий чіпати не можна.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def stamp_rules(vault: Path, providers: list[str], text: str) -> None:
    p = vault / MANIFEST
    data: dict = {"version": 1, "installs": []}
    if p.is_file():
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                data = old
        except (ValueError, OSError):
            pass
    h = rules_hash(text)
    rules = data.get("rules_hash")
    if not isinstance(rules, dict):
        rules = {}
    for prov in providers:
        rules[PROVIDERS[prov]["file"]] = h
    data["rules_hash"] = rules
    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def stamp_version(vault: Path) -> None:
    """Записати в манифест, якої версії скрипти зараз лежать у пам'яті.

    Без цього на питання «яка версія стоїть у користувача» відповісти нічим,
    і лікар не може сказати «твої скрипти старіші за скіл».
    """
    p = vault / MANIFEST
    data: dict = {"version": 1, "installs": []}
    if p.is_file():
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                data = old
        except (ValueError, OSError):
            pass
    data["scripts_version"] = __version__
    data["scripts_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"  УВАГА: версію не записано в манифест ({e})")


def installed_version(vault: Path) -> str | None:
    p = vault / MANIFEST
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    v = data.get("scripts_version") if isinstance(data, dict) else None
    return v if isinstance(v, str) else None


def update_scripts(vault: Path, auto_yes: bool = False) -> int:
    """Оновити скрипти всередині пам'яті до версії скіла.

    Це окрема дія, а не частина установки. Установка навмисно нічого не
    затирає (`write_once`), і саме через це скрипти в пам'яті лишалися
    вічно старими: `install_doctor` бачив наявний файл і пропускав його.
    Тут перезапис навмисний і стосується ЛИШЕ виконуваних файлів у
    `<vault>/scripts/`. Структура, knowledge, правила і будь-який текст
    пам'яті не чіпаються взагалі.
    """
    src_dir = Path(__file__).resolve().parent
    dst_dir = vault / "scripts"

    if not vault.is_dir():
        print(f"Пам'яті за шляхом {vault} немає. Оновлювати нічого.")
        print("Спершу встановлення: python3 ltm_init.py")
        return 1

    have = installed_version(vault)
    print(f"Пам'ять: {vault}")
    print(f"Версія скрипта-скіла: {__version__}")
    print(f"Версія в пам'яті: {have or 'невідома, манифест без позначки'}")

    plan: list[tuple[Path, Path, str]] = []
    for name in VAULT_SCRIPTS:
        src = src_dir / name
        dst = dst_dir / name
        if not src.is_file():
            print(f"  УВАГА: поруч зі скіла немає {name}, пропускаю")
            continue
        if not dst.is_file():
            plan.append((src, dst, "додати, файла ще немає"))
        elif src.read_bytes() != dst.read_bytes():
            plan.append((src, dst, "оновити, вміст відрізняється"))

    if not plan:
        print("\nУсі скрипти в пам'яті вже збігаються зі скілом. Нічого робити.")
        stamp_version(vault)
        return 0

    print(f"\nБуде перезаписано файлів: {len(plan)}")
    for _, dst, why in plan:
        print(f"  {dst}  ({why})")
    print("\nВміст пам'яті не змінюється: лише ці виконувані файли.")

    if not auto_yes and not ask_yes("Оновлюємо?"):
        print("Скасовано.")
        return 0

    dst_dir.mkdir(parents=True, exist_ok=True)
    for src, dst, _ in plan:
        shutil.copy2(src, dst)
        if os.name != "nt":
            try:
                dst.chmod(0o755)
            except OSError:
                pass
        print(f"  оновлено: {dst}")

    stamp_version(vault)
    print(f"\nГотово. Версія в пам'яті тепер {__version__}.")

    doctor = dst_dir / "ltm_doctor.py"
    if doctor.is_file():
        print("Перевірка пам'яті після оновлення:")
        # Без flush дочірній процес пише у дескриптор напряму і його вивід
        # опиняється в конвеєрі раніше за наші рядки: виглядає так, ніби
        # лікар відпрацював до оновлення.
        sys.stdout.flush()
        try:
            subprocess.call([sys.executable, str(doctor), "--vault", str(vault), "--quiet"])
        except OSError as e:
            print(f"  не вдалося запустити лікаря: {e}")
    return 0


def migrate_vault(vault: Path, dry_run: bool = False, auto_yes: bool = False) -> int:
    """Оновити структуру і правила наявної пам'яті до поточної версії скіла.

    Навіщо окремо від `--update`. Той режим оновлює виконувані скрипти, тобто
    інструменти. Цей оновлює те, за чим працює агент: файли правил у корені
    пам'яті і структурні файли, яких у старих установках просто не було
    (`00-global-home/00-home/index.md`, `operations.md`, `pending-concepts.md`).
    Правила визначають, ЯК агент поводиться з пам'яттю, і саме вони роками
    лишалися версії першої установки: `write_once` мовчки пропускає наявний файл.

    Записи користувача не чіпаються ніколи. Додаються лише відсутні файли,
    а файл правил оновлюється тільки якщо людина його не редагувала: це
    перевіряється відбитком у манифесті, а не здогадом.
    """
    global DRY_RUN
    if not vault.is_dir():
        print(f"Пам'яті за шляхом {vault} немає. Мігрувати нічого.")
        return 1
    if not (vault / "00-global-home").is_dir() and not (vault / ".ltm-vault").is_file():
        print(f"За шляхом {vault} не схоже на нашу пам'ять: немає ні 00-global-home, ні .ltm-vault.")
        print("Перевір шлях: ltm_init.py --migrate --path <vault>")
        return 1

    print(f"Пам'ять: {vault}")
    print(f"Версія скіла: {__version__}\n")

    projects = sorted(d.name for d in vault.iterdir()
                      if d.is_dir() and not d.name.startswith(".")
                      and d.name not in ("scripts", "Clippings", "00-global-home")
                      and (d / "00-home").is_dir())
    providers = [p for p in PROVIDERS if (vault / PROVIDERS[p]["file"]).is_file()]
    if not providers:
        providers = ["claude"]
    print(f"Проєктів знайдено: {len(projects)}   Файли правил: "
          f"{', '.join(PROVIDERS[p]['file'] for p in providers)}\n")

    # Прохід у сухому режимі: генеруємо все те саме, що й установка, але
    # write_once лише збирає перелік. Так ми дізнаємось, чого бракує,
    # не торкнувшись жодного файлу.
    planned.clear()
    skipped.clear()
    DRY_RUN = True
    try:
        make_global_home(vault, projects, providers)
        for p in projects:
            make_project(vault, p)
    finally:
        DRY_RUN = False
    missing = sorted(set(planned))

    # Файли правил розбираємо окремо: їх не можна просто дописати.
    rules_text = make_rules(vault, projects)
    want = rules_hash(rules_text)
    stamps = {}
    mp = vault / MANIFEST
    if mp.is_file():
        try:
            data = json.loads(mp.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("rules_hash"), dict):
                stamps = data["rules_hash"]
        except (ValueError, OSError):
            pass

    fresh: list[Path] = []      # можна оновити мовчки, людина не редагувала
    edited: list[Path] = []     # редагована або невідома: чіпати не можна
    pending: list[Path] = []    # .new уже лежить поруч, людина ще не розібрала
    for prov in providers:
        f = vault / PROVIDERS[prov]["file"]
        if not f.is_file():
            continue
        cur = rules_hash(f.read_text(encoding="utf-8", errors="replace"))
        if cur == want:
            continue                      # уже актуальні
        known = stamps.get(PROVIDERS[prov]["file"])
        if known and known == cur:
            fresh.append(f)
            continue
        # Якщо .new з тим самим вмістом уже лежить поруч, не пропонуємо
        # його вкотре: інакше людина бачить те саме попередження вічно.
        new = f.with_suffix(f.suffix + ".new")
        if new.is_file() and rules_hash(new.read_text(encoding="utf-8", errors="replace")) == want:
            pending.append(f)
        else:
            edited.append(f)

    if not missing and not fresh and not edited:
        if pending:
            print("Структура актуальна. Лишилось розібрати вручну:")
            for f in pending:
                print(f"  {f.name}.new  поруч із вашим {f.name}")
            print("\nПорівняй їх, перенеси потрібне у свій файл і видали .new.")
            return 0
        print("Структура і правила вже відповідають поточній версії. Мігрувати нічого.")
        return 0

    print("--- Що зміниться ---")
    if missing:
        print(f"\nДодати відсутні файли: {len(missing)}")
        for m in missing:
            print(f"  + {Path(m).relative_to(vault)}")
    if fresh:
        print(f"\nОновити правила (ви їх не редагували): {len(fresh)}")
        for f in fresh:
            print(f"  ~ {f.name}")
    if edited:
        print(f"\nПравила, які ви змінювали руками: {len(edited)}")
        for f in edited:
            print(f"  ! {f.name}: НЕ чіпаю, нову версію покладу поруч як {f.name}.new")
    print("\nЗаписи в knowledge, sessions, Raw і будь-який ваш текст не чіпаються.")

    if dry_run:
        print("\nСухий режим: нічого не змінено.")
        return 0
    if not auto_yes and not ask_yes("\nЗастосувати?"):
        print("Скасовано.")
        return 0

    created.clear()
    make_global_home(vault, projects, providers)
    for p in projects:
        make_project(vault, p)

    for f in fresh:
        f.write_text(rules_text, encoding="utf-8")
        print(f"  оновлено: {f.name}")
    for f in edited:
        new = f.with_suffix(f.suffix + ".new")
        new.write_text(rules_text, encoding="utf-8")
        print(f"  покладено поруч: {new.name} (порівняй і перенеси потрібне вручну)")

    if fresh:
        provs = [p for p in providers if (vault / PROVIDERS[p]["file"]) in fresh]
        stamp_rules(vault, provs, rules_text)
    stamp_version(vault)

    print(f"\nДодано файлів: {len(created)}")
    print("Готово. Перевірка пам'яті:")
    sys.stdout.flush()
    doctor = vault / "scripts" / "ltm_doctor.py"
    if doctor.is_file():
        try:
            subprocess.call([sys.executable, str(doctor), "--vault", str(vault), "--quiet"])
        except OSError as e:
            print(f"  не вдалося запустити лікаря: {e}")
    return 0


MEMORY_BLOCK_START = "<!-- ltm:start -->"
MEMORY_BLOCK_END = "<!-- ltm:end -->"


def memory_block(vault: Path, provider: str = "claude") -> str:
    """Блок, який вставляється в правила робочого проєкту.

    Обгорнутий маркерами, щоб повторний запуск оновлював його, а не плодив копії."""
    rules_file = PROVIDERS.get(provider, PROVIDERS["claude"])["file"]
    return (
        f"{MEMORY_BLOCK_START}\n"
        "## Довготривала пам'ять\n\n"
        "Це НЕ пам'ять цього проєкту і не файл MEMORY.md агента. Це окреме файлове\n"
        "сховище знань, спільне для всіх проєктів на цій машині.\n\n"
        f"Каталог пам'яті: `{vault}`\n\n"
        "Порядок звернення до неї:\n"
        f"1. правила роботи з пам'яттю: `{vault / rules_file}`\n"
        f"2. точка входу в саму пам'ять: `{vault / '00-global-home' / 'master-index.md'}`\n"
        "3. далі за посиланнями з master-index: `<проєкт>/00-home/index.md`,\n"
        "   `knowledge/decisions/`, `knowledge/patterns/`, `current-priorities.md`, `hot.md`,\n"
        "   свіжі файли в `<проєкт>/sessions/`\n\n"
        "Коли звертатися: питання про раніше ухвалені рішення, архітектуру, причини бага,\n"
        "про те, що вже обговорювалося. Не відповідати «не знаю», не пройшовши цей шлях.\n\n"
        "Що записувати: нові зв'язки, синтез із різних джерел, порівняння підходів,\n"
        "root cause бага, архітектурний висновок. Не записувати просту видачу факту.\n\n"
        "Наприкінці сесії: лог у `<проєкт>/sessions/`, оновити `log.md`.\n"
        "Перевірка здоров'я: `python3 " + str(vault / "scripts" / "ltm_doctor.py") + "`\n"
        f"{MEMORY_BLOCK_END}\n"
    )


def link_project(project_dir: Path, vault: Path, providers: list[str]) -> list[str]:
    """Прописати посилання на пам'ять у файлах правил робочого проєкту.

    Файл створюється, лише якщо провайдера обрав користувач: зайвий GEMINI.md
    у проєкті людини, яка Gemini не користується, це сміття."""
    done = []
    for prov in providers:
        fname = PROVIDERS[prov]["file"]
        block = memory_block(vault, prov)
        target = project_dir / fname
        if target.is_file():
            text = target.read_text(encoding="utf-8", errors="replace")
            if MEMORY_BLOCK_START in text:
                # Блок уже есть: обновляем его содержимое, остальной файл не трогаем.
                start = text.index(MEMORY_BLOCK_START)
                end = text.index(MEMORY_BLOCK_END) + len(MEMORY_BLOCK_END) + 1
                new = text[:start] + block + text[end:]
                if new != text:
                    target.write_text(new, encoding="utf-8")
                    done.append(f"{target} (оновлено блок)")
                link_records.append({"path": str(target), "action": "block_updated"})
            else:
                with target.open("a", encoding="utf-8") as fh:
                    fh.write("\n\n" + block)
                done.append(f"{target} (дописано блок)")
                link_records.append({"path": str(target), "action": "block_appended"})
        else:
            target.write_text(f"# {project_dir.name}\n\n" + block, encoding="utf-8")
            done.append(f"{target} (створено)")
            link_records.append({"path": str(target), "action": "created"})
    return done


def adopt_existing(vault: Path, providers: list[str]) -> list[str]:
    """Прийняти чужу пам'ять: не ламати структуру, лише додати те, чого бракує."""
    notes = []
    projects = sorted(d.name for d in vault.iterdir()
                      if d.is_dir() and not d.name.startswith(".")
                      and d.name not in ("scripts", "Clippings"))
    if not (vault / "00-global-home" / "master-index.md").is_file():
        make_global_home(vault, projects, providers)
        notes.append("створено 00-global-home/master-index.md, точки входу не було")
    else:
        # Точка входа есть, но может не вести к правилам. Дописываем ссылку, текст не трогаем.
        mi = vault / "00-global-home" / "master-index.md"
        txt = mi.read_text(encoding="utf-8", errors="replace")
        missing = [PROVIDERS[p]["file"] for p in providers if PROVIDERS[p]["file"] not in txt]
        if missing:
            with mi.open("a", encoding="utf-8") as fh:
                fh.write("\n## Правила роботи з пам'яттю\n\n")
                for f in missing:
                    fh.write(f"- `{f}` у корені пам'яті\n")
            notes.append(f"у master-index дописано посилання на правила: {', '.join(missing)}")
    for p in projects:
        pdir = vault / p
        if not (pdir / "00-home").is_dir() and (pdir / "sessions").is_dir():
            notes.append(f"проєкт {p}: немає 00-home, структура відрізняється від еталона, лишено як є")
    for prov in providers:
        f = PROVIDERS[prov]["file"]
        if not (vault / f).is_file():
            notes.append(f"додано {f} з правилами")
    return notes


def discover_projects(vault: Path, max_depth: int = 3) -> list[dict]:
    """Знайти робочі проєкти користувача, щоб запропонувати їх переліком."""
    markers = [".git", "package.json", "pyproject.toml", "requirements.txt", "go.mod",
               "Cargo.toml", "pom.xml", "build.gradle", "composer.json", "Gemfile",
               "CMakeLists.txt", "Makefile", "docker-compose.yml"]
    skip = {".cache", ".local", "Library", "AppData", "node_modules", ".npm", ".nvm",
            "snap", "Applications", ".Trash", ".git", "venv", ".venv", "OrbStack"}
    found, seen = [], set()

    def walk(d: Path, depth: int):
        if depth > max_depth or d.name in skip or d.name.startswith("."):
            return
        try:
            entries = list(d.iterdir())
        except (PermissionError, OSError):
            return
        names = {e.name for e in entries}
        if set(markers) & names:
            key = str(d.resolve())
            if key not in seen and key != str(vault.resolve()):
                seen.add(key)
                found.append({
                    "path": str(d),
                    "rules": sorted(set(ALL_RULE_FILES) & names),
                    "markers": sorted(set(markers) & names)[:3],
                })
            return
        for e in entries:
            if e.is_dir() and not e.is_symlink():
                walk(e, depth + 1)

    for root in [Path.home(), Path.home() / "projects", Path.home() / "dev",
                 Path.home() / "work", Path.home() / "src", Path.home() / "Documents"]:
        if root.is_dir():
            walk(root, 1)
    return sorted(found, key=lambda x: x["path"])


def choose_project_names(vault: Path, auto: bool = False) -> list[str]:
    """Визначити, які проєкти завести в пам'яті.

    Три гілки, бо ситуації різні:
      1. на диску знайшлися робочі проєкти -> запропонувати їх переліком
      2. користувач не хоче жодного зі знайдених -> спитати імена
      3. проєктів немає взагалі -> спитати імена

    Імен за замовчуванням не вигадуємо: тека, яку людина не просила,
    в каноні не існує і тільки заважає.
    """
    found = discover_projects(vault)

    if auto:
        # Неінтерактивний режим: беремо імена знайдених проєктів, бо це
        # єдине, що можна вивести з машини, не питаючи людину.
        return [Path(p["path"]).name for p in found] if found else []

    if found:
        print(f"\nНа цій машині знайдено робочих проєктів: {len(found)}")
        for i, p in enumerate(found, 1):
            print(f"  {i}. {Path(p['path']).name}")
            print(f"     {p['path']}  [{', '.join(p['markers'])}]")
        print("\nДля яких із них завести розділи в пам'яті?")
        print("  номери через кому, 'всі', або 'жоден' щоб назвати проєкти вручну")
        raw = ask("Вибір", "всі").strip().lower()

        if raw in ("всі", "все", "all", "*"):
            return [Path(p["path"]).name for p in found]
        if raw not in ("жоден", "жодного", "none", "-", "0"):
            picked = []
            for tok in raw.split(","):
                tok = tok.strip()
                if tok.isdigit() and 1 <= int(tok) <= len(found):
                    picked.append(Path(found[int(tok) - 1]["path"]).name)
            if picked:
                return picked
        print("\nГаразд, назвемо проєкти вручну.")
    else:
        print("\nРобочих проєктів на цій машині не знайдено.")
        print("Це нормально: розділи пам'яті можна завести під будь-які теми.")

    print("Назви стануть іменами тек у пам'яті, тому краще короткі й латиницею.")
    print("Приклади: mobile-app, api-testing, research")
    raw = ask("Назви проєктів через кому", "").strip()
    names = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        # Ім'я стає шляхом, тому прибираємо все, що ламає файлову систему.
        safe = re.sub(r'[<>:"/\\|?*]', "-", tok).strip(". ")
        if safe:
            names.append(safe)
    return names


def choose_projects(vault: Path) -> list[str]:
    """Показати знайдені проєкти і дати обрати. Без вибору не чіпаємо нічого."""
    print("\nШукаю робочі проєкти...")
    projects = discover_projects(vault)
    if not projects:
        raw = ask("Проєктів не знайдено. Вкажи шляхи вручну через кому (порожньо = пропустити)", "")
        return [t.strip() for t in raw.split(",") if t.strip()]

    print(f"\nЗнайдено проєктів: {len(projects)}")
    for i, p in enumerate(projects, 1):
        rules = f"є {', '.join(p['rules'])}" if p["rules"] else "файлів правил немає"
        print(f"  {i}. {p['path']}")
        print(f"     [{', '.join(p['markers'])}]  {rules}")

    print("\nУ які проєкти прописати довготривалу пам'ять?")
    print("  номери через кому, 'всі', 'з-правилами' (лише де вже є файли правил),")
    print("  або порожньо, щоб не чіпати нічого")
    raw = ask("Вибір", "").strip().lower()
    if not raw:
        return []
    if raw in ("всі", "все", "all", "*"):
        return [p["path"] for p in projects]
    if raw in ("з-правилами", "з правилами", "rules"):
        return [p["path"] for p in projects if p["rules"]]
    out = []
    for tok in raw.split(","):
        tok = tok.strip()
        if tok.isdigit() and 1 <= int(tok) <= len(projects):
            out.append(projects[int(tok) - 1]["path"])
    return out


def verify(vault: Path, linked: list[str], providers: list[str]) -> bool:
    """Самоперевірка: агент зобов'язаний переконатися, що встановлення справді працює."""
    print("\n=== Самоперевірка ===")
    ok = True

    mi = vault / "00-global-home" / "master-index.md"
    checks = [
        ("каталог пам'яті", vault.is_dir()),
        ("точка входу master-index.md", mi.is_file()),
    ]
    for prov in providers:
        f = PROVIDERS[prov]["file"]
        checks.append((f"правила {f} ({PROVIDERS[prov]['label']})", (vault / f).is_file()))
    # Точка входа обязана вести к правилам, иначе агент их не найдёт.
    if mi.is_file():
        txt = mi.read_text(encoding="utf-8", errors="replace")
        checks.append(("master-index посилається на файли правил",
                       all(PROVIDERS[p]["file"] in txt for p in providers)))
    checks += [
        ("лікар ltm_doctor.py", (vault / "scripts" / "ltm_doctor.py").is_file()),
        ("маркер шляху .ltm-vault", (vault / ".ltm-vault").is_file()),
    ]
    for label, res in checks:
        print(f"  {'ok  ' if res else 'ЗБІЙ'} {label}")
        ok = ok and res

    doctor = vault / "scripts" / "ltm_doctor.py"
    if doctor.is_file():
        try:
            r = subprocess.run([sys.executable, str(doctor), "--vault", str(vault), "--quiet"],
                               capture_output=True, text=True, timeout=180)
            passed = r.returncode == 0
            print(f"  {'ok  ' if passed else 'ЗБІЙ'} лікар запускається (код {r.returncode})")
            tail = [l for l in r.stdout.strip().splitlines() if l.strip()][-2:]
            for line in tail:
                print(f"       {line}")
            ok = ok and passed
        except (subprocess.SubprocessError, OSError) as e:
            print(f"  ЗБІЙ лікар не запустився: {e}")
            ok = False

    if linked:
        print(f"  ok   правила підключені в проєктах: {len(linked)}")
        for l in linked:
            print(f"       {l}")

    print("\n  ПІДСУМОК: " + ("встановлення робоче" if ok else "є збої, дивись рядки ЗБІЙ вище"))
    return ok


def print_next_steps(vault: Path, providers: list[str] | None = None) -> None:
    providers = providers or ["claude"]
    doctor = vault / "scripts" / "ltm_doctor.py"
    mi = vault / "00-global-home" / "master-index.md"
    print("\n=== Що далі ===")
    print("1. Перевірити пам'ять просто зараз:")
    print(f"   python3 \"{doctor}\"")
    print("2. Точка входу в пам'ять (її агент відкриває першою):")
    print(f"   {mi}")
    print("3. Файли правил у корені пам'яті:")
    for prov in providers:
        print(f"   {vault / PROVIDERS[prov]['file']}  ->  {PROVIDERS[prov]['label']}")
    print("4. Глобальне підключення, якщо потрібно поза обраними проєктами:")
    for prov in providers:
        home_cfg = {"claude": "~/.claude/CLAUDE.md", "amp": "~/.config/amp/AGENTS.md",
                    "gemini": "~/.gemini/GEMINI.md"}.get(prov, "")
        if home_cfg:
            print(f"   {home_cfg}: дописати посилання на {mi}")
    sched = vault / "scripts" / "ltm_schedule.py"
    print("5. Регулярна перевірка здоров'я пам'яті:")
    if sched.is_file():
        print(f"   python3 \"{sched}\"")
        print("   Поставить лікаря на розклад: будні, 12:00. Спитає підтвердження.")
    else:
        print(f"   python3 \"{doctor}\" --all --quiet")
    print("6. Бекап: пам'ять локальна, копія одна. Тримати копію поза цією машиною.")


def obsidian_installed() -> bool:
    """Чи є Obsidian на машині. Перевіряємо і PATH, і типові шляхи."""
    if shutil.which("obsidian"):
        return True
    candidates = []
    if sys.platform == "darwin":
        candidates = [Path("/Applications/Obsidian.app"),
                      Path.home() / "Applications" / "Obsidian.app"]
    elif os.name == "nt":
        la = os.environ.get("LOCALAPPDATA", "")
        pf = os.environ.get("PROGRAMFILES", "")
        candidates = [Path(la) / "Obsidian" / "Obsidian.exe",
                      Path(la) / "Programs" / "Obsidian" / "Obsidian.exe",
                      Path(pf) / "Obsidian" / "Obsidian.exe"]
    else:
        candidates = [Path("/var/lib/flatpak/exports/bin/md.obsidian.Obsidian"),
                      Path("/snap/bin/obsidian")]
    return any(c.exists() for c in candidates if str(c))


def obsidian_install_cmd() -> list[str] | None:
    """Команда встановлення під поточну систему, або None якщо нема чим."""
    if os.name == "nt":
        if shutil.which("winget"):
            return ["winget", "install", "--id", "Obsidian.Obsidian",
                    "-e", "--accept-package-agreements", "--accept-source-agreements"]
        if shutil.which("choco"):
            return ["choco", "install", "obsidian", "-y"]
        return None
    if sys.platform == "darwin":
        if shutil.which("brew"):
            return ["brew", "install", "--cask", "obsidian"]
        return None
    if shutil.which("flatpak"):
        return ["flatpak", "install", "-y", "flathub", "md.obsidian.Obsidian"]
    if shutil.which("snap"):
        return ["sudo", "snap", "install", "obsidian", "--classic"]
    return None


def offer_obsidian(auto_yes: bool = False) -> None:
    """Запропонувати встановити Obsidian і справді це зробити.

    Раніше скрипт лише друкував команду встановлення, і людина думала,
    що програму встановлено. Обіцянка без дії гірша за мовчання.
    """
    if obsidian_installed():
        return
    cmd = obsidian_install_cmd()

    print("\n=== Obsidian ===")
    print("Пам'ять це звичайні markdown-файли, вони працюють і без Obsidian.")
    print("Але саме він показує зв'язки між сторінками графом і робить")
    print("вікі-посилання клікабельними, тому читати пам'ять зручніше.")

    if not cmd:
        print("\nАвтоматично встановити не можу: немає пакетного менеджера.")
        print("Завантаж вручну: https://obsidian.md/download")
        return

    if not auto_yes:
        print(f"\nКоманда: {' '.join(cmd)}")
        try:
            a = input("Встановити Obsidian зараз? [т/Н]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if a not in ("т", "так", "y", "yes"):
            print("Пропущено. Встановити пізніше: " + " ".join(cmd))
            return

    print("Встановлюю, це може зайняти кілька хвилин...")
    try:
        r = subprocess.run(cmd, timeout=600)
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"Не вдалося: {e}")
        print("Завантаж вручну: https://obsidian.md/download")
        return
    if r.returncode == 0 and obsidian_installed():
        print("Obsidian встановлено.")
    elif r.returncode == 0:
        print("Команда завершилась успішно, але програму не видно в системі.")
        print("Можливо, потрібен перезапуск термінала.")
    else:
        print(f"Встановлення не вдалося, код {r.returncode}.")
        print("Завантаж вручну: https://obsidian.md/download")


def offer_seed(vault: Path, seed_file: str | None = None, auto_yes: bool = False) -> None:
    """Запропонувати наповнити пам'ять готовим вмістом.

    Питання ставимо ЗАВЖДИ, навіть якщо файла seed поруч немає: людина має
    знати, що така можливість існує, і де взяти файл.
    """
    seeder = vault / "scripts" / "ltm_seed.py"
    if not seeder.is_file():
        return

    print("\n=== Наповнення пам'яті ===")
    print("Пам'ять можна почати з чистого аркуша або наповнити готовим вмістом:")
    print("рішеннями, патернами й розборами, які вже хтось зібрав.")
    print("Готовий вміст лежить у зашифрованому файлі, пароль дає його власник.")
    print("\nВаші власні файли при цьому не перезаписуються: якщо сторінка вже є,")
    print("версія з набору лягає поруч як *.seed.md, а рішення лишається за вами.")

    candidate = seed_file
    if not candidate and not auto_yes:
        try:
            a = input("\nНаповнити пам'ять готовим вмістом? [т/Н]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if a not in ("т", "так", "y", "yes"):
            print("Гаразд, пам'ять лишається чистою. Наповнити пізніше:")
            print(f"  python3 \"{seeder}\" --unpack <файл> --vault \"{vault}\"")
            return
        try:
            candidate = input("Шлях до файлу набору: ").strip()
        except (EOFError, KeyboardInterrupt):
            return

    if not candidate:
        return
    src = Path(candidate).expanduser()
    if not src.is_file():
        print(f"Файл не знайдено: {src}")
        print(f"Наповнити пізніше: python3 \"{seeder}\" --unpack <файл> --vault \"{vault}\"")
        return

    # Спершу показуємо, що саме прийде, і лише потім пишемо.
    subprocess.run([sys.executable or "python3", str(seeder), "--unpack", str(src),
                    "--vault", str(vault), "--dry-run"])
    if not auto_yes:
        try:
            a = input("\nЗастосувати? [Т/н]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if a and a not in ("т", "так", "y", "yes"):
            print("Скасовано, нічого не записано.")
            return
    subprocess.run([sys.executable or "python3", str(seeder), "--unpack", str(src),
                    "--vault", str(vault)])


def offer_schedule(vault: Path, auto_yes: bool = False) -> None:
    """Запропонувати розклад одразу після встановлення.

    Окремим кроком і тільки за згодою: запис у cron, launchd чи Планувальник
    Windows це зміна в системі користувача, а не всередині пам'яті.
    """
    sched = vault / "scripts" / "ltm_schedule.py"
    if not sched.is_file():
        return
    print("\n=== Регулярна перевірка ===")
    print("Пам'ять псується тихо: биті посилання й сторінки-сироти не помітні,")
    print("поки агент не почне відповідати неправильно. Перевірка ловить це заздалегідь.")
    if not auto_yes:
        try:
            a = input("Поставити перевірку на розклад: будні, 12:00? [Т/н]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if a and a not in ("т", "так", "y", "yes"):
            print("Пропущено. Поставити згодом:")
            print(f"  python3 \"{sched}\"")
            return
    r = subprocess.run([sys.executable or "python3", str(sched),
                        "--path", str(vault), "--yes"],
                       capture_output=False)
    if r.returncode != 0:
        print("Розклад не поставлено. Спробуй вручну:")
        print(f"  python3 \"{sched}\"")


def run_uninstall(extra: list[str]) -> int:
    """Передати роботу ltm_uninstall.py.

    Логіку видалення тримаємо в одному місці: копія тут швидко розійдеться
    з оригіналом, і відкат почне лишати сліди саме тоді, коли на нього
    покладаються найбільше.
    """
    here = Path(__file__).resolve().parent
    script = here / "ltm_uninstall.py"
    if not script.is_file():
        # Скрипт міг лишитися лише всередині вже встановленої пам'яті.
        for base in (Path.home(), Path.home() / "Documents"):
            for cand in base.glob("*/scripts/ltm_uninstall.py"):
                script = cand
                break
    if not script.is_file():
        print("Поруч немає ltm_uninstall.py. Візьми його з репозиторію скіла.")
        return 1
    return subprocess.call([sys.executable, str(script)] + extra)


def main() -> int:
    ap = argparse.ArgumentParser(description="Розгорнути довготривалу пам'ять агентів")
    ap.add_argument("--path", help="шлях до vault")
    ap.add_argument("--projects", help="перелік проєктів через кому")
    ap.add_argument("--check", action="store_true", help="лише діагностика")
    ap.add_argument("--yes", action="store_true", help="без питань")
    ap.add_argument("--adopt", action="store_true",
                    help="прийняти наявну пам'ять за цим шляхом, а не створювати нову")
    ap.add_argument("--link", metavar="DIRS",
                    help="підключити пам'ять до робочих проєктів: шляхи через кому")
    ap.add_argument("--no-verify", action="store_true", help="пропустити самоперевірку")
    ap.add_argument("--no-obsidian", action="store_true",
                    help="не пропонувати встановлення Obsidian")
    ap.add_argument("--seed", metavar="FILE",
                    help="наповнити пам'ять готовим вмістом із зашифрованого файлу")
    ap.add_argument("--no-seed", action="store_true",
                    help="не питати про наповнення готовим вмістом")
    ap.add_argument("--schedule", action="store_true",
                    help="одразу поставити перевірку на розклад: будні, 12:00")
    ap.add_argument("--no-schedule", action="store_true",
                    help="не пропонувати розклад")
    ap.add_argument("--providers", metavar="LIST",
                    help="агенти через кому: claude, amp, gemini. За замовчуванням спитає")
    ap.add_argument("--uninstall", action="store_true",
                    help="прибрати пам'ять і всі сліди установки")
    ap.add_argument("--update", action="store_true",
                    help="оновити скрипти всередині пам'яті до версії скіла")
    ap.add_argument("--migrate", action="store_true",
                    help="оновити структуру і правила наявної пам'яті, записи не чіпає")
    ap.add_argument("--dry-run", action="store_true",
                    help="з --migrate: лише показати, що змінилося б")
    ap.add_argument("--version", action="store_true",
                    help="показати версію скіла і версію скриптів у пам'яті")
    args = ap.parse_args()

    if args.version:
        v = Path(args.path).expanduser() if args.path else default_vault_path()
        print(f"ltm_init.py {__version__}")
        print(f"пам'ять: {v}")
        print(f"версія скриптів у пам'яті: {installed_version(v) or 'невідома'}")
        return 0

    print("Довготривала пам'ять агентів\n")

    # Розвилка на старті. Друга гілка потрібна насамперед для тестування:
    # без відкату скіл перевіряється на машині рівно один раз, і друга спроба
    # вже йде поверх залишків першої.
    if args.uninstall:
        return run_uninstall([])
    if args.update:
        v = Path(args.path).expanduser() if args.path else default_vault_path()
        return update_scripts(v, auto_yes=args.yes)
    if args.migrate:
        v = Path(args.path).expanduser() if args.path else default_vault_path()
        return migrate_vault(v, dry_run=args.dry_run, auto_yes=args.yes)
    if not args.yes and not args.check and not args.path and not args.adopt:
        print("Що робимо?")
        print("  1. Встановити пам'ять")
        print("  2. Оновити скрипти наявної пам'яті до версії скіла")
        print("  3. Оновити структуру і правила наявної пам'яті")
        print("  4. Видалити все, що поставив цей скіл")
        choice = ask("Номер", "1").strip()
        if choice == "4":
            return run_uninstall([])
        if choice == "3":
            v = Path(ask("Де лежить пам'ять", str(default_vault_path()))).expanduser()
            # Спершу завжди сухий прогін: людина мусить побачити перелік
            # до того, як щось торкнеться її пам'яті.
            migrate_vault(v, dry_run=True)
            if not ask_yes("\nЗастосувати ці зміни?"):
                print("Скасовано.")
                return 0
            return migrate_vault(v, auto_yes=True)
        if choice == "2":
            v = Path(ask("Де лежить пам'ять", str(default_vault_path()))).expanduser()
            return update_scripts(v)
        print()

    vault = Path(args.path).expanduser() if args.path else default_vault_path()
    if not args.path and not args.yes and not args.check:
        vault = Path(ask("Куди покласти пам'ять", str(default_vault_path()))).expanduser()

    diagnose(vault)
    if args.check:
        return 0

    # Провайдера НЕ угадываем: у человека может стоять несколько агентов сразу,
    # и от выбора зависит и что кладём в память, и какие файлы искать в его проектах.
    if args.providers:
        providers = [p.strip().lower() for p in args.providers.split(",")
                     if p.strip().lower() in PROVIDERS]
    elif args.yes:
        # Раніше тут було ["claude", "amp"], і GEMINI.md мовчки не створювався.
        providers = list(PROVIDERS)
    else:
        print("Яким агентом ти користуєшся? Можна кілька.")
        for i, (k, v) in enumerate(PROVIDERS.items(), 1):
            print(f"  {i}. {v['label']}  ->  {v['file']}")
        raw = ask("Номери через кому, або 'всі'", "1")
        if raw.strip().lower() in ("всі", "все", "all", "*"):
            providers = list(PROVIDERS)
        else:
            keys = list(PROVIDERS)
            providers = []
            for tok in raw.split(","):
                tok = tok.strip()
                if tok.isdigit() and 1 <= int(tok) <= len(keys):
                    providers.append(keys[int(tok) - 1])
                elif tok.lower() in PROVIDERS:
                    providers.append(tok.lower())
    if not providers:
        providers = ["claude"]
    print(f"Файли правил будуть: {', '.join(PROVIDERS[p]['file'] for p in providers)}")

    if args.projects:
        projects = [p.strip() for p in args.projects.split(",") if p.strip()]
    else:
        # Ніяких імен за замовчуванням: раніше тут з'являлася тека "work",
        # якої немає в каноні і яку користувач не просив.
        projects = choose_project_names(vault, auto=args.yes)
    if not projects:
        print("Без жодного проєкту пам'ять не має сенсу. Скасовано.")
        return 1

    if not args.yes:
        print(f"\nСтворю пам'ять у {vault}")
        print(f"Проєкти: {', '.join(projects)}")
        if not ask_yes("Продовжуємо?"):
            print("Скасовано.")
            return 0

    vault.mkdir(parents=True, exist_ok=True)
    (vault / "scripts").mkdir(exist_ok=True)

    if args.adopt:
        # Чужая память уже устроена как-то. Не переделываем её под эталон,
        # а только дописываем то, без чего агент не сможет работать.
        print("\nРежим adopt: наявна пам'ять приймається як є.")
        for note in adopt_existing(vault, providers):
            print(f"  {note}")
    else:
        make_global_home(vault, projects, providers)
        for p in projects:
            make_project(vault, p)

    rules = make_rules(vault, projects)
    # Один текст под разными именами: каждый агент читает своё имя файла.
    for prov in providers:
        write_once(vault / PROVIDERS[prov]["file"], rules)
    stamp_rules(vault, providers, rules)
    write_once(vault / "README.md",
        fm("Довготривала пам'ять", "global", "meta", ["vault", "memory"]) +
        "# Довготривала пам'ять агентів\n\n"
        "Локальна файлова пам'ять для Claude Code і AMP. Синхронізації немає.\n\n"
        "Почати читання: [[00-global-home/master-index]]\n"
        "Правила: `AGENTS.md`\n"
        "Перевірка здоров'я: `python3 scripts/ltm_doctor.py`\n")
    write_once(vault / ".ltm-vault", str(vault))

    install_doctor(vault)

    linked: list[str] = []
    link_targets: list[str] = []
    if args.link:
        link_targets = [t.strip() for t in args.link.split(",") if t.strip()]
    elif args.yes:
        # Раніше в цій гілці підключення пропускалося зовсім, і в робочих
        # проєктах не з'являлося правил: агент відкривав проєкт і не знав,
        # що пам'ять узагалі існує. Тепер підключаємо знайдені проєкти.
        link_targets = [p["path"] for p in discover_projects(vault)]
        if link_targets:
            print(f"\nПідключаю пам'ять до знайдених проєктів: {len(link_targets)}")
    else:
        link_targets = choose_projects(vault)

    for t in link_targets:
        pdir = Path(t).expanduser()
        if not pdir.is_dir():
            print(f"  пропущено, каталогу немає: {pdir}")
            continue
        linked += link_project(pdir, vault, providers)

    # Підключення це половина справи: якщо файл правил не з'явився,
    # агент у проєкті пам'ять не побачить. Перевіряємо явно.
    if link_targets and not linked:
        print("  УВАГА: жодного файлу правил не створено в проєктах.")
        print("  Підключи вручну: ltm_init.py --link <шлях> --providers claude")

    write_manifest(vault, projects, providers)
    stamp_version(vault)

    print(f"\nСтворено файлів і тек: {len(created)}")
    if skipped:
        print(f"Пропущено (уже було): {len(skipped)}")

    ok = True
    if not args.no_verify:
        ok = verify(vault, linked, providers)

    # Розклад пропонуємо лише коли встановлення справді робоче: ставити
    # перевірку на зламану пам'ять безглуздо.
    if ok and not args.check and not args.no_obsidian and not args.yes:
        offer_obsidian()

    # Наповнення пропонуємо ДО розкладу: спершу вміст, потім догляд за ним.
    if ok and not args.check and not args.no_seed:
        offer_seed(vault, seed_file=args.seed, auto_yes=bool(args.seed) and args.yes)

    if ok and not args.check:
        if args.schedule:
            offer_schedule(vault, auto_yes=True)
        elif not args.yes and not args.no_schedule:
            offer_schedule(vault, auto_yes=False)

    print_next_steps(vault, providers)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
