#!/usr/bin/env python3
"""Claude Code SessionEnd hook: не дать сессии пропасть бесследно.

Зачем нужен отдельно от PreCompact. PreCompact срабатывает только когда
контекст переполнился. Но большинство разговоров заканчивается раньше:
человек обсудил тему, получил решение и закрыл окно. Контекст израсходован
наполовину, сжатия не было, дампа нет, и всё сказанное исчезает.

Что делает. Сохраняет дамп разговора и, если в нём есть признаки разбора
(решение, причина бага, выбор подхода), ставит запись в очередь
`pending-concepts.md` того же проекта. В следующей сессии хук SessionStart
увидит очередь и предложит человеку скомпилировать её в knowledge.

Чего НЕ делает намеренно. Не пишет страницы в knowledge сам. Осмысленную
страницу может написать только агент, понимающий содержание разговора;
скрипт на такое не способен и выдал бы мусор, который потом никто не чистит.
Скрипт лишь помечает: здесь есть что забрать.

Совместимость: Python 3.9. Завершается кодом 0 всегда.
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    from ltm_paths import find_vault, state_dir, log_dir
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ltm_paths import find_vault, state_dir, log_dir

VAULT = find_vault()
PENDING = state_dir() / "pending"
LOG = log_dir() / "claude-ltm-hooks.log"

SERVICE = {"00-global-home", "scripts", "Clippings", ".git", ".obsidian"}
KEEP_MESSAGES = 60
MAX_CHARS = 80000
MIN_MESSAGES = 6        # ниже этого порога разговор считаем незначительным

# Признаки того, что в разговоре был разбор, а не текучка. Набор намеренно
# совпадает по смыслу с concept_scout: механизм тот же, точка входа другая.
TRIGGERS = [
    r"принят\w* решение", r"прийнят\w* рішення", r"decision",
    r"root cause", r"корень причины", r"корінь причини",
    r"паттерн", r"патерн", r"pattern",
    r"архитектур\w+", r"архітектур\w+", r"architecture",
    r"сравнени\w+", r"порівнянн\w+", r"comparison", r"trade-?off",
    r"вывод\w*", r"висновк\w*", r"lessons learned", r"post-?mortem",
    r"почему\s+(?:не\s+)?работа", r"чому\s+(?:не\s+)?працю",
    r"исправл\w+", r"виправл\w+", r"пофикс\w+", r"починил\w*",
]


def log(msg):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write("[%s] session_end: %s\n"
                     % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def norm(s):
    return re.sub(r"[-_\s.]+", "", s).lower()


def find_project(cwd):
    if VAULT is None or not VAULT.is_dir():
        return None
    try:
        dirs = [d.name for d in VAULT.iterdir()
                if d.is_dir() and not d.name.startswith(".") and d.name not in SERVICE]
    except OSError:
        return None
    for part in [Path(cwd).name] + [p.name for p in Path(cwd).parents]:
        if not part:
            continue
        if part in dirs:
            return part
        hits = [d for d in dirs if norm(d) == norm(part)]
        if len(hits) == 1:
            return hits[0]
    return None


def extract(transcript_path):
    p = Path(transcript_path)
    if not p.is_file():
        return []
    try:
        raw = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    msgs = []
    for line in raw:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        msg = rec.get("message") or rec
        role = msg.get("role") or rec.get("type") or ""
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content")
        parts = []
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text" and item.get("text"):
                        parts.append(item["text"])
                    elif item.get("type") == "tool_use":
                        parts.append("[инструмент: %s]" % item.get("name", "?"))
                elif isinstance(item, str):
                    parts.append(item)
        text = "\n".join(x for x in parts if x).strip()
        if text:
            msgs.append((role, text))
    return msgs[-KEEP_MESSAGES:]


# Человекочитаемая метка для каждого шаблона: в очередь должно попадать
# слово, понятное человеку, а не сырой регэксп вроде `принят\w* решение`.
TRIGGER_LABELS = {
    r"принят\w* решение": "решение", r"прийнят\w* рішення": "рішення",
    r"decision": "decision",
    r"root cause": "root cause", r"корень причины": "корень причины",
    r"корінь причини": "корінь причини",
    r"паттерн": "паттерн", r"патерн": "патерн", r"pattern": "pattern",
    r"архитектур\w+": "архитектура", r"архітектур\w+": "архітектура",
    r"architecture": "architecture",
    r"сравнени\w+": "сравнение", r"порівнянн\w+": "порівняння",
    r"comparison": "comparison", r"trade-?off": "trade-off",
    r"вывод\w*": "вывод", r"висновк\w*": "висновок",
    r"lessons learned": "lessons learned", r"post-?mortem": "post-mortem",
    r"почему\s+(?:не\s+)?работа": "почему не работает",
    r"чому\s+(?:не\s+)?працю": "чому не працює",
    r"исправл\w+": "исправление", r"виправл\w+": "виправлення",
    r"пофикс\w+": "фикс", r"починил\w*": "починка",
}


def matched_triggers(text):
    low = text.lower()
    hits = [TRIGGER_LABELS.get(pat, pat) for pat in TRIGGERS if re.search(pat, low)]
    return sorted(set(hits))


def queue_concept(project, dump_path, hits, msg_count):
    """Поставить запись в очередь кандидатов проекта.

    Пишем в тот же файл и в том же формате, что и concept_scout, чтобы
    существующий порядок работы не раздваивался: агент читает одну очередь,
    независимо от того, кто её наполнил.
    """
    pf = VAULT / project / "00-home" / "pending-concepts.md"
    if not pf.is_file():
        return False
    try:
        entry = [
            "",
            "### %s" % datetime.now().strftime("%Y-%m-%d %H:%M"),
            "- Source: session end (hook), сообщений: %d" % msg_count,
            "- Dump: `%s`" % dump_path,
            "- Trigger keywords matched: %d" % len(hits),
            "- Keywords: %s" % ",".join(hits),
            "- Status: pending",
            "- Suggested action: агенту прочитать дамп, извлечь концепт(ы) в "
            "`knowledge/<category>/`, оформить session-файл в `sessions/`, "
            "обновить `index.md` и `log.md`",
            "",
        ]
        with pf.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(entry))
        return True
    except OSError:
        return False


def main():
    cwd = os.getcwd()
    transcript = ""
    reason = "other"
    try:
        raw = sys.stdin.read()
        if raw.strip():
            data = json.loads(raw)
            cwd = data.get("cwd") or cwd
            transcript = data.get("transcript_path") or ""
            reason = data.get("reason") or data.get("matcher") or reason
    except (ValueError, OSError):
        pass

    if VAULT is None:
        return 0

    project = find_project(cwd)
    if not project:
        log("проект вне памяти, пропуск")
        return 0

    msgs = extract(transcript)
    if len(msgs) < MIN_MESSAGES:
        # Короткий обмен репликами сохранять незачем: очередь кандидатов
        # быстро превратится в свалку, и человек перестанет её читать.
        log("сессия короткая (%d сообщений), пропуск" % len(msgs))
        return 0

    full = "\n".join(t for _, t in msgs)
    hits = matched_triggers(full)

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    out = PENDING / ("%s__end_%s.md" % (project, stamp))

    body = [
        "# Дамп сессии (завершение)",
        "",
        "- проект: %s" % project,
        "- рабочий каталог: %s" % cwd,
        "- когда: %s" % datetime.now().strftime("%Y-%m-%d %H:%M"),
        "- причина завершения: %s" % reason,
        "- сообщений: %d" % len(msgs),
        "- признаки разбора: %s" % (", ".join(hits) if hits else "не найдены"),
        "",
        "Это СЫРЬЁ. Оформить по операции Query -> Save:",
        "session-файл в %s/sessions/, при наличии концепта - страница" % project,
        "в %s/knowledge/<category>/, затем index.md и log.md." % project,
        "",
        "---",
        "",
    ]
    for role, text in msgs:
        body.append("## %s" % ("Человек" if role == "user" else "Агент"))
        body.append("")
        body.append(text)
        body.append("")

    text = "\n".join(body)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n\n[дамп обрезан по размеру]\n"

    try:
        PENDING.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        log("дамп сохранён: %d сообщений -> %s" % (len(msgs), out.name))
    except OSError as e:
        log("FAIL запись дампа: %s" % e)
        return 0

    if hits:
        if queue_concept(project, out, hits, len(msgs)):
            log("в очередь pending-concepts добавлена запись, совпадений: %d" % len(hits))
        else:
            log("очередь pending-concepts недоступна у проекта %s" % project)
    else:
        log("признаков разбора нет, в очередь не ставлю")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log("FAIL необработанное: %s" % e)
        sys.exit(0)
