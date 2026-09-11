"""Тексти блоку пам'яті для файлів правил робочого проєкту.

Навіщо окремий модуль. Блок це головний текст усього скіла: саме його читає
агент, відкритий у робочому проєкті. Агент не відкривається всередині сховища,
тому правила, що лежать у корені пам'яті, він не побачить ніколи.

Склад блоку відповідає канону Карпати: три шари (Raw, Wiki, Schema) і три
операції (Ingest, Query, Lint). Еталон, з якого знято структуру, це реальний
`CLAUDE.md` користувача, доведений до канону вручну.

Мова блоку визначається по наявному файлу правил проєкту, а не питається.
"""

from pathlib import Path

MEMORY_BLOCK_START = "<!-- ltm:start -->"
MEMORY_BLOCK_END = "<!-- ltm:end -->"
DEFAULT_BLOCK_LANG = "uk"


def _uk(vault: Path, p: str, entry: str) -> str:
    v = str(vault)
    return f"""{MEMORY_BLOCK_START}
## Довготривала пам'ять

Це НЕ пам'ять цього проєкту і не файл MEMORY.md агента. Це окреме файлове
сховище знань за методом Карпати, спільне для всіх проєктів на цій машині.

Сховище: `{v}`
Каталог цього проєкту в сховищі: `{p}/`

### Шари

| Шар | Де | Хто пише |
|------|-----|----------|
| Raw | `{p}/Raw/` | тільки людина. Агент читає і НІКОЛИ не змінює |
| Wiki | `{p}/knowledge/`, `{p}/00-home/` | агент |
| Schema | цей блок і `{p}/00-home/operations.md` | людина |

### На початку сесії читай

1. `{entry}`
2. `{p}/00-home/index.md`
3. `{p}/00-home/current-priorities.md`
4. `{p}/00-home/hot.md`
5. останні 3 файли в `{p}/sessions/`

### Операція Query, відповідь на питання

Будь-яка відповідь по проєкту починається з `{p}/00-home/index.md`. Це canonical
entry point, а не здогад по пам'яті. Далі йти за вікі-посиланнями вглиб.
За один запит читати від 10 до 50 файлів. У відповіді називати файли-джерела.

### Операція Ingest, переробка сировини

Тригер: людина каже «ingest файл». Порядок:

1. прочитати файл із `{p}/Raw/`
2. видати тези і план правок, **зупинитися і чекати підтвердження**
3. після згоди: сторінка-саммарі в `{p}/knowledge/`, далі нові й оновлені
   концепт-сторінки з двосторонніми вікі-посиланнями
4. оновити `{p}/00-home/index.md`
5. дописати в `{p}/log.md` рядок `## YYYY-MM-DD ingest | <джерело>`

### Операція Lint, перевірка здоров'я

Тригер: «lint» або раз на тиждень. Шукати: сироти без вхідних посилань, биті
посилання, заглушки коротші за 200 слів, застарілі твердження, відсутні
концепт-сторінки, односторонні посилання.

### Query -> Save, що зберігати обов'язково

Новий зв'язок між сутностями, синтез із різних джерел, порівняння підходів
з обґрунтуванням, root cause бага, архітектурний висновок, зміна поведінки
вже описаної концепції.

Не зберігати: просту видачу факту, коротке уточнення, те, що вже є на сторінці.

Без концепт-сторінки знання лишиться тільки в session-лозі, а структурний lint
session-логи не перевіряє. Session-логи дешеві, концепт-сторінки дорогі.

### При створенні будь-якої нової сторінки в knowledge/

1. **одразу** дописати рядок у `{p}/00-home/index.md` з однорядковим описом.
   Саме одразу, а не наприкінці сесії: сторінка без рядка в індексі
   не знаходиться і фактично втрачена
2. дописати запис у `{p}/log.md`
3. проставити двосторонні вікі-посилання зі спорідненими сторінками
4. у кінці сторінки секція `## Джерела` з посиланням на session-файл
5. збільшити `sources: N` у frontmatter на число доданих джерел

### Формат файла

Кожен `.md` у сховищі починається з YAML frontmatter:

```yaml
---
title: "Опис"
date: YYYY-MM-DD
project: {p}
agent: <claude-code|codex|gemini>
type: <atlas|integration|decision|debugging|pattern|business|analysis|session>
tags: [tag1, tag2]
status: active
sources: 0
---
```

### Вікі-посилання

- шлях пишеться **від кореня сховища**: `[[{p}/knowledge/decisions/назва]]`.
  Такий шлях не залежить від того, де лежить файл-джерело
- відносні шляхи з `../` заборонені: Obsidian їх не розуміє, посилання
  виглядає робочим, але веде в нікуди
- регістр і роздільник важливі: посилання на `my-proj/log` при каталозі
  `My_Proj` не розв'яжеться
- у кожної концепт-сторінки щонайменше одне вхідне посилання, інакше сирота
- посилання двосторонні: якщо A посилається на B, то B посилається на A
- session-логи в граф не лінкуються

### Збереження сесії

Запускається, коли людина каже «збережи сесію», або коли ти сам бачиш, що
тема завершена. Не чекай кінця розмови: людина часто не закриває сесію
взагалі, і тоді робота зникне.

1. створити `{p}/sessions/YYYY-MM-DD_HHMM_<agent>_<topic>.md`
2. оновити `{p}/00-home/current-priorities.md` і `hot.md`
3. дописати запис у `{p}/log.md`
4. у session-файлі секція `## Створено / змінено в цій сесії` з посиланнями
   тільки на файли всередині сховища
5. пройти чеклист Query -> Save вище і, якщо є що зберігати,
   **запропонувати людині** створити концепт-сторінку. Саме запропонувати:
   рішення про те, що гідне knowledge, залишається за людиною.

### Первинні матеріали

У `Raw/` кладемо лише текст: транскрипції, виписки, метадані. Відео й аудіо
не кладемо: якщо сховище під git, бінарник лишиться в історії назавжди.
{MEMORY_BLOCK_END}
"""


def _ru(vault: Path, p: str, entry: str) -> str:
    v = str(vault)
    return f"""{MEMORY_BLOCK_START}
## Долговременная память

Это НЕ память этого проекта и не файл MEMORY.md агента. Это отдельное файловое
хранилище знаний по методу Карпати, общее для всех проектов на этой машине.

Хранилище: `{v}`
Каталог этого проекта в хранилище: `{p}/`

### Слои

| Слой | Где | Кто пишет |
|------|-----|-----------|
| Raw | `{p}/Raw/` | только человек. Агент читает и НИКОГДА не меняет |
| Wiki | `{p}/knowledge/`, `{p}/00-home/` | агент |
| Schema | этот блок и `{p}/00-home/operations.md` | человек |

### В начале сессии читай

1. `{entry}`
2. `{p}/00-home/index.md`
3. `{p}/00-home/current-priorities.md`
4. `{p}/00-home/hot.md`
5. последние 3 файла в `{p}/sessions/`

### Операция Query, ответ на вопрос

Любой ответ по проекту начинается с `{p}/00-home/index.md`. Это canonical entry
point, а не догадка по памяти. Дальше идти по вики-ссылкам вглубь.
За один запрос читать от 10 до 50 файлов. В ответе называть файлы-источники.

### Операция Ingest, переработка сырья

Триггер: человек говорит «ingest файл». Порядок:

1. прочитать файл из `{p}/Raw/`
2. выдать тезисы и план правок, **остановиться и ждать подтверждения**
3. после согласия: страница-саммари в `{p}/knowledge/`, затем новые и обновлённые
   концепт-страницы с двусторонними вики-ссылками
4. обновить `{p}/00-home/index.md`
5. дописать в `{p}/log.md` строку `## YYYY-MM-DD ingest | <источник>`

### Операция Lint, проверка здоровья

Триггер: «lint» или раз в неделю. Искать: сироты без входящих ссылок, битые
ссылки, заглушки короче 200 слов, устаревшие утверждения, отсутствующие
концепт-страницы, односторонние ссылки.

### Query -> Save, что сохранять обязательно

Новая связь между сущностями, синтез из разных источников, сравнение подходов
с обоснованием, root cause бага, архитектурный вывод, изменение поведения уже
описанной концепции.

Не сохранять: простую выдачу факта, короткое уточнение, то, что уже есть
на существующей странице.

Без концепт-страницы знание останется только в session-логе, а структурный lint
session-логи не проверяет. Session-логи дешёвые, концепт-страницы дорогие.

### При создании любой новой страницы в knowledge/

1. **сразу** дописать строку в `{p}/00-home/index.md` с однострочным описанием.
   Именно сразу, а не в конце сессии: страница без строки в индексе
   не находится и фактически потеряна
2. дописать запись в `{p}/log.md`
3. проставить двусторонние вики-ссылки со связанными страницами
4. в конце страницы секция `## Источники` со ссылкой на session-файл
5. увеличить `sources: N` в frontmatter на число добавленных источников

### Формат файла

Каждый `.md` в хранилище начинается с YAML frontmatter:

```yaml
---
title: "Описание"
date: YYYY-MM-DD
project: {p}
agent: <claude-code|codex|gemini>
type: <atlas|integration|decision|debugging|pattern|business|analysis|session>
tags: [tag1, tag2]
status: active
sources: 0
---
```

### Вики-ссылки

- путь пишется **от корня хранилища**: `[[{p}/knowledge/decisions/название]]`.
  Такой путь не зависит от того, где лежит файл-источник
- относительные пути с `../` запрещены: Obsidian их не понимает, ссылка
  выглядит рабочей, но ведёт в никуда
- регистр и разделитель важны: ссылка на `my-proj/log` при каталоге
  `My_Proj` не разрешится
- у каждой концепт-страницы минимум одна входящая ссылка, иначе сирота
- ссылки двусторонние: если A ссылается на B, то B ссылается на A
- session-логи в граф не линкуются

### Сохранение сессии

Запускается, когда человек говорит «сохрани сессию», или когда ты сам видишь,
что тема завершена. Не жди конца разговора: человек часто не закрывает сессию
вообще, и тогда работа пропадёт.

1. создать `{p}/sessions/YYYY-MM-DD_HHMM_<agent>_<topic>.md`
2. обновить `{p}/00-home/current-priorities.md` и `hot.md`
3. дописать запись в `{p}/log.md`
4. в session-файле секция `## Создано / изменено в этой сессии` со ссылками
   только на файлы внутри хранилища
5. пройти чеклист Query -> Save выше и, если есть что сохранять,
   **предложить человеку** создать концепт-страницу. Именно предложить:
   решение о том, что достойно knowledge, остаётся за человеком.

### Первичные материалы

В `Raw/` кладём только текст: транскрипции, выдержки, метаданные. Видео и аудио
не кладём: если хранилище под git, бинарник останется в истории навсегда.
{MEMORY_BLOCK_END}
"""


def _en(vault: Path, p: str, entry: str) -> str:
    v = str(vault)
    return f"""{MEMORY_BLOCK_START}
## Long-term memory

This is NOT this project's memory and not the agent's MEMORY.md file. It is a
separate file-based knowledge store following Karpathy's method, shared by every
project on this machine.

Store: `{v}`
This project's directory inside the store: `{p}/`

### Layers

| Layer | Where | Who writes |
|-------|-------|------------|
| Raw | `{p}/Raw/` | the human only. The agent reads and NEVER modifies |
| Wiki | `{p}/knowledge/`, `{p}/00-home/` | the agent |
| Schema | this block and `{p}/00-home/operations.md` | the human |

### At the start of a session read

1. `{entry}`
2. `{p}/00-home/index.md`
3. `{p}/00-home/current-priorities.md`
4. `{p}/00-home/hot.md`
5. the last 3 files in `{p}/sessions/`

### Query operation, answering a question

Every answer about this project starts at `{p}/00-home/index.md`. That is the
canonical entry point, not a guess from memory. Then follow wiki links deeper.
Read between 10 and 50 files per request. Name your source files in the answer.

### Ingest operation, turning raw material into knowledge

Trigger: the human says "ingest this file". Order:

1. read the file from `{p}/Raw/`
2. produce the key points and a plan of edits, then **stop and wait for approval**
3. once approved: a summary page in `{p}/knowledge/`, then new and updated
   concept pages with two-way wiki links
4. update `{p}/00-home/index.md`
5. append a line to `{p}/log.md`: `## YYYY-MM-DD ingest | <source>`

### Lint operation, health check

Trigger: "lint" or once a week. Look for: orphans with no inbound links, broken
links, stubs under 200 words, stale claims, missing concept pages, one-way links.

### Query -> Save, what must be saved

A new connection between entities, a synthesis across sources, a comparison of
approaches with reasoning, the root cause of a bug, an architectural conclusion,
a change in the behaviour of an already documented concept.

Do not save: a plain fact lookup, a one-line clarification, anything already
covered by an existing page.

Without a concept page the knowledge stays in a session log only, and the
structural lint does not check session logs. Session logs are cheap, concept
pages are expensive.

### When creating any new page in knowledge/

1. **immediately** add a line to `{p}/00-home/index.md` with a one-line summary.
   Immediately, not at the end of the session: a page with no line in the index
   cannot be found and is effectively lost
2. append an entry to `{p}/log.md`
3. add two-way wiki links to related pages
4. end the page with a `## Sources` section linking the session file
5. increase `sources: N` in the frontmatter by the number of sources added

### File format

Every `.md` in the store starts with YAML frontmatter:

```yaml
---
title: "Description"
date: YYYY-MM-DD
project: {p}
agent: <claude-code|codex|gemini>
type: <atlas|integration|decision|debugging|pattern|business|analysis|session>
tags: [tag1, tag2]
status: active
sources: 0
---
```

### Wiki links

- write the path **from the store root**: `[[{p}/knowledge/decisions/name]]`.
  Such a path does not depend on where the linking file lives
- relative paths with `../` are forbidden: Obsidian does not understand them,
  the link looks fine but resolves to nothing
- case and separator matter: a link to `my-proj/log` will not resolve when the
  directory is called `My_Proj`
- every concept page needs at least one inbound link, otherwise it is an orphan
- links are two-way: if A links to B, then B links back to A
- session logs are not part of the graph and are not linked

### Saving a session

Triggered when the person says "save the session", or when you see the topic
is finished. Do not wait for the conversation to end: people often never close
a session, and the work is lost.

1. create `{p}/sessions/YYYY-MM-DD_HHMM_<agent>_<topic>.md`
2. update `{p}/00-home/current-priorities.md` and `hot.md`
3. append an entry to `{p}/log.md`
4. in the session file add a `## Created / changed in this session` section
   linking only files inside the store
5. walk the Query -> Save checklist above and, if there is something worth
   keeping, **offer the person** to create a concept page. Offer, not decide:
   what deserves knowledge is the person's call.

### Primary material

Put text only into `Raw/`: transcripts, excerpts, metadata. No video or audio:
if the store is under git, a binary stays in history forever.
{MEMORY_BLOCK_END}
"""


BLOCK_TEXT = {"uk": _uk, "ru": _ru, "en": _en}
