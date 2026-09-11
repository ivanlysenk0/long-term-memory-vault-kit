#!/usr/bin/env python3
"""Claude Code PreCompact hook: сохранить сессию ДО сжатия контекста.

Что делает. Срабатывает в момент, когда Claude Code собирается сжать историю
разговора. Забирает транскрипт сессии, вытаскивает последние сообщения
и кладёт их дампом в отдельный каталог вне памяти.

Почему это нужно. При сжатии из контекста выкидываются детали и остаётся
краткое резюме. Инженерные решения, причины багов и обоснования выбора,
прозвучавшие в начале длинной сессии, исчезают бесследно: агент их уже
не помнит, а в память записать не успел.

Почему дамп кладётся НЕ в память. Сырой транскрипт это не знание, а сырьё.
Слой `Raw/` по правилам пишет только человек, а `knowledge/` требует
осмысленной страницы. Поэтому дамп ждёт в служебном каталоге, а хук
SessionStart в следующей сессии напомнит агенту его оформить.

Совместимость: Python 3.9 (системный на macOS).
Хук всегда завершается кодом 0 и НИКОГДА не блокирует сжатие: заблокировать
компактинг означает подвесить работу человека.
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# Пути под конкретную ОС живут в отдельном модуле: macOS, Linux и Windows
# держат логи и служебное состояние в разных местах, и хардкод одного из них
# означает, что на двух других системах хук молча не работает.
try:
    from ltm_paths import find_vault, state_dir, log_dir
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ltm_paths import find_vault, state_dir, log_dir

VAULT = find_vault()
PENDING = state_dir() / "pending"
LOG = log_dir() / "claude-ltm-hooks.log"

SERVICE = {"00-global-home", "scripts", "Clippings", ".git", ".obsidian"}
KEEP_MESSAGES = 40      # сколько последних сообщений забрать
MAX_CHARS = 60000       # потолок размера дампа
KEEP_DUMPS = 20         # сколько дампов хранить, старые удаляются


def log(msg):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write("[%s] precompact: %s\n"
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
    """Последние сообщения из транскрипта в читаемом виде.

    Транскрипт это JSONL: одна запись на строку. Формат может меняться между
    версиями, поэтому разбор намеренно терпимый: неизвестную запись пропускаем,
    а не падаем на ней.
    """
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


def prune():
    """Старые дампы удаляются: каталог не должен расти бесконечно."""
    try:
        files = sorted(PENDING.glob("*.md"))
        for f in files[:-KEEP_DUMPS]:
            f.unlink()
    except OSError:
        pass


def main():
    cwd = os.getcwd()
    transcript = ""
    trigger = "auto"
    try:
        raw = sys.stdin.read()
        if raw.strip():
            data = json.loads(raw)
            cwd = data.get("cwd") or cwd
            transcript = data.get("transcript_path") or ""
            trigger = data.get("trigger") or trigger
    except (ValueError, OSError):
        pass

    project = find_project(cwd)
    if not project:
        log("проект вне памяти (%s), пропуск" % cwd)
        return 0

    msgs = extract(transcript)
    if not msgs:
        log("транскрипт пуст или не прочитан: %s" % transcript)
        return 0

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    out = PENDING / ("%s__%s.md" % (project, stamp))

    body = []
    body.append("# Дамп сессии до сжатия контекста")
    body.append("")
    body.append("- проект: %s" % project)
    body.append("- рабочий каталог: %s" % cwd)
    body.append("- когда: %s" % datetime.now().strftime("%Y-%m-%d %H:%M"))
    body.append("- причина сжатия: %s" % trigger)
    body.append("- сообщений сохранено: %d" % len(msgs))
    body.append("")
    body.append("Это СЫРЬЁ, а не знание. Оформить по операции Query -> Save:")
    body.append("страница в %s/knowledge/<category>/, строка в" % project)
    body.append("%s/00-home/index.md, запись в %s/log.md." % (project, project))
    body.append("После оформления этот файл удалить.")
    body.append("")
    body.append("---")
    body.append("")
    for role, text in msgs:
        who = "Человек" if role == "user" else "Агент"
        body.append("## %s" % who)
        body.append("")
        body.append(text)
        body.append("")

    text = "\n".join(body)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n\n[дамп обрезан по размеру]\n"

    try:
        PENDING.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        log("сохранено %d сообщений -> %s" % (len(msgs), out))
        prune()
    except OSError as e:
        log("FAIL запись дампа: %s" % e)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log("FAIL необработанное: %s" % e)
        # Блокировать сжатие нельзя ни при каких обстоятельствах.
        sys.exit(0)
