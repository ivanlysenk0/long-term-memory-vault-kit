#!/usr/bin/env python3
"""Claude Code SessionStart hook: подъём контекста из долговременной памяти.

Что делает. При старте сессии определяет по рабочему каталогу, какой это проект,
находит его каталог в памяти и печатает в stdout сжатую выжимку: приоритеты,
горячие темы, последние сессии. Всё, что хук пишет в stdout, Claude Code
добавляет в контекст агента.

Почему это нужно. Без него каждая сессия начинается с нуля: агент не знает,
на чём остановились, и либо переспрашивает, либо лезет читать память руками,
тратя 5-10 минут и токены на сканирование.

Ограничение объёма принципиально. Этот текст уходит в контекст КАЖДОЙ сессии,
поэтому выжимка жёстко подрезана. Задача хука - дать агенту зацепки и пути,
а не пересказать память целиком: дальше он дочитает сам по операции Query.

Совместимость: Python 3.9 (системный на macOS). Никаких `str | None`.
Хук всегда завершается кодом 0: упавший хук не должен ломать сессию.
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
    from ltm_paths import find_vault, state_dir, log_dir, IS_MAC
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ltm_paths import find_vault, state_dir, log_dir, IS_MAC

VAULT = find_vault()
PENDING = state_dir() / "pending"
SYNC_ALERT = state_dir() / "sync-alert.txt"
LOG = log_dir() / "claude-ltm-hooks.log"

SERVICE = {"00-global-home", "scripts", "Clippings", ".git", ".obsidian"}


def log(msg):
    """Лог обязателен, а не «на всякий случай».

    Без него невозможно ответить на главный вопрос эксплуатации: хук не
    сработал или сработал и промолчал? Эти два случая чинятся по-разному,
    а внешне выглядят одинаково.
    """
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write("[%s] session_start: %s\n"
                     % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass
MAX_LINES = 14          # сколько строк максимум из одного файла
MAX_SESSIONS = 3        # сколько последних сессий перечислить


def norm(s):
    return re.sub(r"[-_\s.]+", "", s).lower()


def find_project(cwd):
    """Каталог проекта в памяти, соответствующий рабочему каталогу сессии.

    VAULT может быть None, если память на этой машине не развёрнута:
    вызывающий код обязан это учитывать.

    Имена не обязаны совпадать: на диске `Sky-Kids-SMM-bot`, в памяти
    `sky-kids-smm-bot`. Сначала точное совпадение, потом без учёта регистра
    и разделителей. Не нашли - возвращаем None и молчим: сессия в случайной
    папке не должна получать чужой контекст.
    """
    if VAULT is None or not VAULT.is_dir():
        return None
    try:
        dirs = [d.name for d in VAULT.iterdir()
                if d.is_dir() and not d.name.startswith(".") and d.name not in SERVICE]
    except OSError:
        return None

    # Проверяем сам каталог и родителей: сессия может быть открыта в подпапке.
    for part in [Path(cwd).name] + [p.name for p in Path(cwd).parents]:
        if not part:
            continue
        if part in dirs:
            return part
        hits = [d for d in dirs if norm(d) == norm(part)]
        if len(hits) == 1:
            return hits[0]
    return None


def pull_vault():
    """Подтянуть свежую память с GitHub в начале сессии.

    Без этого агент работает с тем, что лежит локально. Человек правил память
    с другой машины или с сервера, а здесь её ещё нет, и агент уверенно
    отвечает по устаревшим данным.

    `--ff-only` намеренно: если локально есть свои коммиты, безопасного
    быстрого слияния не выйдет. Тогда не трогаем ничего и не устраиваем
    merge-конфликт на старте сессии - это сделает Stop-хук в конце.
    """
    if VAULT is None:
        return
    try:
        import subprocess
        r = subprocess.run(
            ["git", "-C", str(VAULT), "pull", "--ff-only", "-q",
             "origin", "main:refs/remotes/origin/main"],
            capture_output=True, text=True, timeout=25)
        log("git pull: %s" % ("ok" if r.returncode == 0 else "пропущен, код %d" % r.returncode))
    except Exception as e:
        log("git pull не выполнен: %s" % e)


def ensure_downloaded(paths):
    """Скачать файлы, выгруженные в облако (macOS iCloud).

    iCloud вытесняет редко используемые файлы, оставляя на диске заглушку
    `.<имя>.icloud`. Обычное чтение такого файла вернёт пустоту, и хук молча
    отдаст агенту выжимку без половины данных - худший вид сбоя, потому что
    выглядит как «в памяти ничего нет».
    """
    if not IS_MAC:
        return
    import subprocess
    for p in paths:
        stub = p.parent / ("." + p.name + ".icloud")
        if p.is_file() or not stub.exists():
            continue
        try:
            subprocess.run(["brctl", "download", str(p)],
                           capture_output=True, timeout=20)
            log("iCloud: запрошена загрузка %s" % p.name)
        except Exception:
            pass


def head(path, limit=MAX_LINES):
    """Содержательные строки файла без YAML-шапки и пустот."""
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    # Срезаем frontmatter: агенту в выжимке нужны факты, а не метаданные.
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    out = []
    for line in text.splitlines():
        s = line.rstrip()
        if not s.strip():
            continue
        out.append(s)
        if len(out) >= limit:
            out.append("   ...")
            break
    return out


def main():
    # Вход приходит на stdin. Нет входа - не повод падать.
    cwd = os.getcwd()
    source = "?"
    raw = ""
    candidates = []
    try:
        raw = sys.stdin.read()
        # Сырой payload последнего запуска. Нужен ровно для одного вопроса:
        # какие поля реально присылает конкретная среда (терминал, десктоп,
        # IDE). Гадать по документации тут бессмысленно, форматы различаются.
        try:
            dbg = LOG.parent / "claude-ltm-last-payload.json"
            dbg.parent.mkdir(parents=True, exist_ok=True)
            dbg.write_text(raw[:8000], encoding="utf-8")
        except OSError:
            pass
        if raw.strip():
            data = json.loads(raw)
            # `cwd` не единственный источник правды. В десктопе и IDE сессия
            # нередко стартует из домашней папки, а реальный проект приходит
            # отдельным полем. Поэтому собираем все кандидаты и берём первый,
            # который удаётся сопоставить с каталогом памяти.
            for key in ("workspace", "project_dir", "projectDir", "workspaceRoot",
                        "root_dir", "cwd"):
                v = data.get(key)
                if isinstance(v, str) and v.strip():
                    candidates.append(v)
            extra = data.get("additional_directories") or data.get("additionalDirectories")
            if isinstance(extra, list):
                candidates.extend(x for x in extra if isinstance(x, str))
            if candidates:
                cwd = candidates[0]
            # `source` различает startup, resume, clear и compact. Важно:
            # возобновлённая сессия это не то же самое, что новая.
            source = data.get("source") or data.get("matcher") or "?"
    except (ValueError, OSError):
        pass

    log("старт, cwd=%s, source=%s" % (cwd, source))

    if VAULT is None or not VAULT.is_dir():
        # Память не найдена: возможно, скил тут не ставили. Молчим,
        # чужую сессию это касаться не должно.
        log("память не найдена, выключаюсь")
        return 0

    # Сбой синхронизации сообщается ДО проверки проекта и независимо от него:
    # память не уехала в GitHub целиком, а не только для текущего проекта.
    alert = ""
    if SYNC_ALERT.is_file():
        # Маркер говорит лишь о том, что сбой БЫЛ. Он не говорит, есть ли
        # проблема сейчас. Stop-хук снимает тревогу только в конце следующей
        # сессии, поэтому между сбоем и снятием человек получает предупреждение
        # о проблеме, которой уже нет.
        #
        # Проверяем факт, а не запись в файле: если незапушенных коммитов ноль
        # и рабочее дерево чистое, память в GitHub уехала, и пугать нечем.
        stale = False
        try:
            import subprocess
            common = ["git", "-C", str(VAULT)]
            subprocess.run(common + ["fetch", "origin", "main", "-q"],
                           capture_output=True, timeout=25)
            ahead = subprocess.run(common + ["rev-list", "--count", "origin/main..HEAD"],
                                   capture_output=True, text=True, timeout=15)
            dirty = subprocess.run(common + ["status", "--porcelain"],
                                   capture_output=True, text=True, timeout=15)
            if (ahead.returncode == 0 and ahead.stdout.strip() == "0"
                    and dirty.returncode == 0 and not dirty.stdout.strip()):
                stale = True
        except Exception:
            # Проверить не смогли - считаем тревогу настоящей. Ложное
            # предупреждение дешевле пропущенной потери памяти.
            stale = False

        if stale:
            try:
                SYNC_ALERT.unlink()
            except OSError:
                pass
            log("маркер сбоя снят: память синхронизирована, тревога устарела")
            rows = []
        else:
            try:
                rows = [r for r in SYNC_ALERT.read_text(encoding="utf-8",
                                                        errors="replace").splitlines() if r.strip()]
            except OSError:
                rows = []
        if rows:
            alert = ("## ВНИМАНИЕ: долговременная память не синхронизирована\n\n"
                     "Последняя попытка отправить память в GitHub не прошла:\n"
                     + "\n".join("- " + r for r in rows[-3:]) + "\n\n"
                     "Скажи об этом человеку в первом же ответе. Подробности:\n"
                     + str(log_dir() / "claude-vault-sync.log") + "\n")
            log("есть маркер сбоя синхронизации, записей %d" % len(rows))

    project = None
    # Перебираем все кандидаты, а не только первый: в одном из полей проект
    # обычно находится, даже если `cwd` указывает на домашнюю папку.
    for cand in (candidates or [cwd]):
        project = find_project(cand)
        if project:
            if cand != cwd:
                log("проект найден не по cwd, а по %s" % cand)
            break

    # Сессия стартовала из домашней папки. Раньше хук тут просто молчал, и
    # человек оставался без памяти вообще. Но домашняя папка это не «чужой
    # проект», это «проект ещё не выбран»: дальше человек откроет файлы или
    # назовёт задачу. Поэтому даём короткую карту памяти вместо тишины.
    if not project and Path(cwd).resolve() == Path.home().resolve():
        try:
            names = sorted(d.name for d in VAULT.iterdir()
                           if d.is_dir() and not d.name.startswith(".")
                           and d.name not in SERVICE)
        except OSError:
            names = []
        if names:
            out = [alert] if alert else []
            out.append("## Долговременная память подключена")
            out.append("")
            out.append("Сессия открыта в домашней папке, проект не определён.")
            out.append("Память: " + str(VAULT))
            out.append("")
            out.append("Проекты в памяти: " + ", ".join(names))
            out.append("")
            out.append("Как только станет понятно, о каком проекте речь, начинай")
            out.append("с <проект>/00-home/index.md и дальше по вики-ссылкам.")
            text = "\n".join(out) + "\n"
            sys.stdout.write(text)
            log("домашняя папка: выдана карта памяти, %d символов" % len(text))
            return 0

    if not project:
        # Проект не из памяти: выжимку не даём, но о сбое синхронизации
        # сообщаем в любом случае - он касается всей памяти.
        if alert:
            sys.stdout.write(alert)
            log("выдано только предупреждение о синхронизации")
        else:
            log("проект не найден в памяти, молчу")
        return 0

    pdir = VAULT / project
    home = pdir / "00-home"

    # Порядок важен: сначала подтянуть свежее с GitHub, потом убедиться, что
    # файлы физически на диске, и только потом читать.
    pull_vault()
    ensure_downloaded([home / "current-priorities.md", home / "hot.md",
                       home / "index.md", pdir / "log.md"])

    lines = []

    if alert:
        lines.append(alert)

    # Отдельная ветка для запуска сразу после сжатия контекста.
    #
    # Это замыкает цикл: PreCompact сохранил дамп ДО сжатия, а здесь мы
    # возвращаем его агенту сразу ПОСЛЕ. Без этой ветки дамп пролежал бы
    # до следующей сессии, а агент продолжал бы работу, не помня деталей,
    # которые обсуждались десять минут назад.
    #
    # Документация Claude Code рекомендует именно этот путь: SessionStart
    # с матчером `compact`, а не отдельный хук на PostCompact.
    if source == "compact":
        fresh = []
        if PENDING.is_dir():
            try:
                fresh = sorted(PENDING.glob(project + "__*.md"))[-1:]
            except OSError:
                fresh = []
        lines.append("## Контекст только что сжимался")
        lines.append("")
        if fresh:
            lines.append("Детали начала сессии могли потеряться при сжатии.")
            lines.append("Полный дамп разговора ДО сжатия сохранён здесь:")
            lines.append(str(fresh[0]))
            lines.append("")
            lines.append("Если для текущей задачи нужны подробности, которых")
            lines.append("больше нет в контексте, прочитай этот файл.")
        else:
            lines.append("Дамп не найден. Если потерялись важные детали,")
            lines.append("скажи об этом человеку, а не догадывайся.")
        lines.append("")

    lines.append("## Долговременная память: состояние проекта " + project)
    lines.append("")
    lines.append("Память: " + str(VAULT))
    lines.append("Каталог проекта: " + project + "/")
    lines.append("")

    prio = head(home / "current-priorities.md")
    if prio:
        lines.append("### Текущие приоритеты")
        lines.extend(prio)
        lines.append("")

    hot = head(home / "hot.md", 10)
    if hot:
        lines.append("### Горячее")
        lines.extend(hot)
        lines.append("")

    # Хвост журнала. Без него выжимка врёт свежестью: `current-priorities.md`
    # и `hot.md` обновляются агентом вручную и легко отстают на месяцы, тогда
    # как `log.md` пишется при каждой значимой правке памяти.
    #
    # Реальный случай: приоритеты отдавались от 26 апреля, а работа за
    # 9 сентября лежала только в журнале, и агент шёл читать его командой.
    log_rows = []
    lf = pdir / "log.md"
    if lf.is_file():
        try:
            raw = lf.read_text(encoding="utf-8", errors="replace")
            if raw.startswith("---"):
                end = raw.find("\n---", 3)
                if end != -1:
                    raw = raw[end + 4:]
            # Записи журнала начинаются с даты, всё остальное это шапки и пустоты.
            rows = [r.strip() for r in raw.splitlines()
                    if re.match(r"^#*\s*20\d\d-\d\d-\d\d", r.strip())]
            log_rows = rows[-5:]
        except OSError:
            log_rows = []

    if log_rows:
        lines.append("### Последние записи журнала")
        for r in log_rows:
            # Записи бывают длинными абзацами: в выжимке нужен факт, а не текст
            # целиком. За полным текстом агент сходит в log.md сам.
            r = r.lstrip("#").strip()
            lines.append("- " + (r[:240] + " ..." if len(r) > 240 else r))
        lines.append("")

    sdir = pdir / "sessions"
    if sdir.is_dir():
        try:
            recent = sorted((f for f in sdir.glob("*.md")),
                            key=lambda f: f.name, reverse=True)[:MAX_SESSIONS]
        except OSError:
            recent = []
        if recent:
            lines.append("### Последние сессии")
            for f in recent:
                title = ""
                for ln in head(f, 3):
                    if ln.startswith("# "):
                        title = ln[2:].strip()
                        break
                lines.append("- " + f.stem + (": " + title if title else ""))
            lines.append("")

    # Несохранённые дампы от PreCompact. Это главное, ради чего связка работает:
    # контекст сжался, детали могли пропасть, и агент обязан узнать об этом.
    if PENDING.is_dir():
        try:
            pend = sorted(PENDING.glob(project + "__*.md"))
        except OSError:
            pend = []
        if pend:
            lines.append("### ВНИМАНИЕ: есть несохранённые сессии")
            lines.append("Контекст прошлой сессии сжимался, дамп сохранён до сжатия:")
            for f in pend[-3:]:
                lines.append("- " + str(f))
            lines.append("")
            lines.append("Предложи человеку оформить их по операции Query -> Save:")
            lines.append("страница в " + project + "/knowledge/<category>/, строка в")
            lines.append(project + "/00-home/index.md, запись в " + project + "/log.md.")
            lines.append("После оформления дамп удалить.")
            lines.append("")

    # Очередь кандидатов на компиляцию в knowledge. Её наполняет скрипт
    # concept_scout: он ищет в session-логах признаки разбора (решение,
    # паттерн, root cause) и помечает такие логи как pending.
    #
    # Сам скрипт ничего не компилирует - это работа агента. Но если про
    # очередь никто не напомнит, записи так и лежат: session-лог дешёвый,
    # а концепт-страница дорогая, и без напоминания знание остаётся
    # похороненным в логе.
    pending_file = home / "pending-concepts.md"
    if pending_file.is_file():
        try:
            ptxt = pending_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            ptxt = ""
        # Считаем только необработанные: у скомпилированных статус compiled.
        waiting = len(re.findall(r"(?im)^-\s*Status:\s*pending\s*$", ptxt))
        if waiting:
            lines.append("### Ожидают компиляции в knowledge: %d" % waiting)
            lines.append("")
            lines.append("В " + project + "/00-home/pending-concepts.md есть записи")
            lines.append("со статусом pending: это session-логи, в которых нашлись")
            lines.append("признаки разбора, но концепт-страница ещё не создана.")
            lines.append("")
            lines.append("Предложи человеку скомпилировать их: прочитать session,")
            lines.append("создать страницу в " + project + "/knowledge/<category>/,")
            lines.append("обновить index.md и log.md, сменить статус на compiled.")
            lines.append("")
            log("ожидают компиляции: %d" % waiting)

    # Без этого блока хук бесполезен наполовину. Правила проекта велят агенту
    # в начале сессии прочитать index.md, current-priorities.md, hot.md и
    # последние сессии. Агент выполняет инструкцию буквально и читает их
    # командами - хотя всё это уже лежит выше, выданное хуком. Получается
    # двойная работа: лишние вызовы инструментов и задержка ответа.
    # Поэтому явно говорим, что стартовое чтение уже выполнено.
    lines.append("### Стартовое чтение уже выполнено")
    lines.append("")
    lines.append("Выжимка выше получена хуком SessionStart из файлов:")
    lines.append("- " + project + "/00-home/current-priorities.md")
    lines.append("- " + project + "/00-home/hot.md")
    lines.append("- " + project + "/log.md (последние записи)")
    lines.append("- последние сессии в " + project + "/sessions/")
    lines.append("")
    lines.append("НЕ перечитывай их командами в начале сессии: данные актуальны")
    lines.append("на эту минуту. Правило «в начале сессии читай» уже выполнено.")
    lines.append("")
    lines.append("Читай файлы памяти только когда нужны детали, которых нет выше:")
    lines.append("конкретная страница knowledge, полный log.md, старая сессия.")
    lines.append("")
    lines.append("### Порядок работы")
    lines.append("Вглубь иди с " + project + "/00-home/index.md по вики-ссылкам.")
    lines.append("Не отвечай «не знаю», не пройдя этот путь.")

    out = "\n".join(lines) + "\n"
    sys.stdout.write(out)
    log("ВЫДАНО %d символов для проекта %s" % (len(out), project))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Хук не имеет права ронять сессию. Любая ошибка - тихий выход.
        sys.exit(0)
