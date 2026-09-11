/**
 * Долговременная память для Amp.
 *
 * Поднимает память в контекст на старте разговора, помечает кандидатов в
 * knowledge и синхронизирует хранилище после каждого хода агента.
 *
 * Устройство отличается от Claude Code, и вот почему.
 *
 * 1. Выжимку отдаёт `agent.start`, а не `session.start`.
 *    В типах Amp у `session.start` нет возвращаемого значения: это событие
 *    «сделай и забудь». Вернуть текст в контекст умеет только `agent.start`
 *    через `{ message: { content } }`. Поэтому `session.start` готовит
 *    выжимку и синхронизирует хранилище, а отдаёт её первый ход агента.
 *
 * 2. Аналога `PreCompact` нет и не нужно.
 *    В Amp нет события сжатия контекста, зато обработчики получают полную
 *    историю потока. Спасать разговор перед сжатием, как в Claude Code, не
 *    требуется.
 *
 * 3. Аналога `SessionEnd` нет в самом Amp.
 *    Документация говорит прямо: `There is no session.end event`. Это
 *    совпадает с методом Карпати, где сессию сохраняет человек командой.
 *
 * Скрипты вызываются через `amp.$` - встроенный шелл Bun. Он одинаково
 * работает на macOS, Linux и Windows, поэтому отдельной ветки под Windows
 * здесь нет: разница только в имени интерпретатора Python.
 */
import type { PluginAPI } from '@ampcode/plugin'

export const description =
  'Долговременная память: поднимает состояние проекта в контекст, помечает кандидатов в knowledge, синхронизирует хранилище.'

/** Выжимка на поток. Ключ - идентификатор потока. */
const pending = new Map<string, string>()
/** Потоки, которым выжимку уже отдали: второй раз не повторяем. */
const served = new Set<string>()

/** Сколько ждать скрипты, миллисекунды. */
const TIMEOUT_MS = 25000

export default function (amp: PluginAPI) {
  const dir = new URL('.', import.meta.url).pathname
  const scripts = `${dir}scripts`

  /**
   * Найти рабочий Python.
   *
   * На macOS и Linux это `python3`. На Windows установщик с python.org
   * кладёт `python`, а из Microsoft Store приходит заглушка `python3`,
   * которая открывает магазин вместо запуска. Поэтому порядок проверки
   * именно такой, и результат запоминается на время жизни плагина.
   */
  let python: string | null = null
  const findPython = async (): Promise<string | null> => {
    if (python) return python
    for (const candidate of ['python3', 'python']) {
      try {
        const probe = await amp.$`${candidate} -c "print(1)"`.quiet().nothrow()
        if (probe.exitCode === 0) {
          python = candidate
          return python
        }
      } catch {
        // Кандидата нет в PATH, пробуем следующего.
      }
    }
    return null
  }

  /**
   * Запустить скрипт памяти, передав ему рабочий каталог через stdin.
   *
   * Скрипты ждут на входе тот же JSON, что и хуки Claude Code: так один и тот
   * же код обслуживает оба агента без развилок внутри.
   */
  const run = async (script: string, cwd: string): Promise<string> => {
    const exe = await findPython()
    if (!exe) {
      amp.logger.log('память: не найден Python, пропускаю')
      return ''
    }
    const payload = JSON.stringify({ cwd, source: 'amp' })
    try {
      const result = await Promise.race([
        amp.$`echo ${payload} | ${exe} ${`${scripts}/${script}`}`.quiet().nothrow(),
        new Promise<null>((resolve) => setTimeout(() => resolve(null), TIMEOUT_MS)),
      ])
      if (result === null) {
        amp.logger.log(`память: ${script} не ответил за ${TIMEOUT_MS / 1000} с`)
        return ''
      }
      if (result.exitCode !== 0) {
        amp.logger.log(`память: ${script} завершился с кодом ${result.exitCode}`)
      }
      return result.stdout.toString().trim()
    } catch (err) {
      amp.logger.log(`память: ошибка запуска ${script}: ${err}`)
      return ''
    }
  }

  /** Корень проекта. Amp отдаёт его как URI, скриптам нужен обычный путь. */
  const projectDir = (): string => {
    const root = amp.system.workspaceRoot
    if (!root) return ''
    try {
      return amp.helpers.filePathFromURI(root)
    } catch {
      return ''
    }
  }

  // Начало разговора: синхронизируем хранилище и готовим выжимку.
  // Отдать её отсюда нельзя - событие не принимает возвращаемое значение.
  amp.on('session.start', async (event) => {
    const cwd = projectDir()
    if (!cwd) return
    const summary = await run('ltm_session_start.py', cwd)
    if (summary) {
      pending.set(event.thread.id, summary)
      amp.logger.log(`память: выжимка готова, ${summary.length} символов`)
    }
  })

  // Первый ход агента: кладём выжимку в контекст.
  // display: false - это служебный текст для агента, человеку он не нужен.
  amp.on('agent.start', (event) => {
    const id = event.thread.id
    if (served.has(id)) return {}
    const summary = pending.get(id)
    if (!summary) return {}
    served.add(id)
    pending.delete(id)
    return { message: { content: summary, display: false } }
  })

  // Конец хода: помечаем кандидатов в knowledge и синхронизируем хранилище.
  // Скрипт сам проверяет, есть ли изменения: при чистом дереве он молча
  // выходит, не трогая сеть.
  amp.on('agent.end', async () => {
    const cwd = projectDir()
    if (!cwd) return
    await run('ltm_sync.py', cwd)
  })

  amp.registerCommand(
    'ltm-vault.status',
    {
      title: 'Состояние долговременной памяти',
      category: 'Долговременная память',
      description: 'Показать, где найдено хранилище и что ждёт компиляции.',
    },
    async (ctx) => {
      const cwd = projectDir()
      const out = await run('ltm_doctor.py', cwd)
      await ctx.ui.notify(out || 'Проверка ничего не вернула, смотри журнал плагина.')
    },
  )

  amp.registerCommand(
    'ltm-vault.sync',
    {
      title: 'Синхронизировать память сейчас',
      category: 'Долговременная память',
      description: 'Пометить кандидатов в knowledge, закоммитить и отправить изменения.',
    },
    async (ctx) => {
      const cwd = projectDir()
      await run('ltm_sync.py', cwd)
      await ctx.ui.notify('Память синхронизирована.')
    },
  )

  amp.onDispose(() => {
    pending.clear()
    served.clear()
  })
}
