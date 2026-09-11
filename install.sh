#!/usr/bin/env bash
# Встановлення скіла довготривалої пам'яті для агента.
# Працює на macOS, Ubuntu, Debian, Fedora, Arch та інших Unix-системах.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/skill"
NAME="ltm-vault"

say()  { printf '%s\n' "$*"; }
fail() { printf 'ПОМИЛКА: %s\n' "$*" >&2; exit 1; }

# --- Python -----------------------------------------------------------------
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  fi
done
[ -n "$PY" ] || fail "потрібен Python 3.8 або новіший.
  Ubuntu/Debian: sudo apt install python3
  Fedora:        sudo dnf install python3
  Arch:          sudo pacman -S python
  macOS:         brew install python3"

say "Python: $("$PY" --version 2>&1)"
[ -d "$SRC" ] || fail "не знайдено каталог skill/ поруч зі скриптом"

# --- куди ставити ------------------------------------------------------------
# Кожен агент шукає скіли у власному каталозі.
declare -a TARGETS=()
add_target() { TARGETS+=("$1|$2"); }

detect() {
  # Кожен рядок завершується `|| true`: без цього останній невдалий `[ -d ]`
  # поверне 1, і через `set -e` скрипт мовчки завершиться, нічого не сказавши.
  [ -d "$HOME/.claude" ]      && add_target "Claude Code" "$HOME/.claude/skills"      || true
  [ -d "$HOME/.config/amp" ]  && add_target "AMP Code"    "$HOME/.config/amp/skills"  || true
  [ -d "$HOME/.gemini" ]      && add_target "Gemini CLI"  "$HOME/.gemini/skills"      || true
}

case "${1:-}" in
  --claude) add_target "Claude Code" "$HOME/.claude/skills" ;;
  --amp)    add_target "AMP Code"    "$HOME/.config/amp/skills" ;;
  --gemini) add_target "Gemini CLI"  "$HOME/.gemini/skills" ;;
  --all)
    add_target "Claude Code" "$HOME/.claude/skills"
    add_target "AMP Code"    "$HOME/.config/amp/skills"
    add_target "Gemini CLI"  "$HOME/.gemini/skills" ;;
  --help|-h)
    cat <<'EOF'
Використання: ./install.sh [опція]

  (без опцій)  знайти встановлених агентів і поставити скіл усім
  --claude     тільки Claude Code
  --amp        тільки AMP Code
  --gemini     тільки Gemini CLI
  --all        усім трьом, навіть якщо агент ще не встановлено
  --help       ця довідка
EOF
    exit 0 ;;
  "") detect ;;
  *)  fail "невідома опція: $1 (спробуй --help)" ;;
esac

if [ ${#TARGETS[@]} -eq 0 ]; then
  say "Не знайдено жодного встановленого агента."
  say "Постав примусово: ./install.sh --all"
  exit 1
fi

# --- установка ---------------------------------------------------------------
for t in "${TARGETS[@]}"; do
  label="${t%%|*}"
  dir="${t##*|}/$NAME"
  mkdir -p "$dir"
  cp -R "$SRC/." "$dir/"
  # __pycache__ лишається від запусків у репозиторії і їде до користувача.
  find "$dir" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  chmod +x "$dir"/scripts/*.py 2>/dev/null || true
  say "встановлено: $label -> $dir"
done

# --- перевірка ---------------------------------------------------------------
first="${TARGETS[0]##*|}/$NAME"
"$PY" - "$first" <<'PY'
import sys, pathlib
d = pathlib.Path(sys.argv[1])
need = ["SKILL.md", "scripts/ltm_detect.py", "scripts/ltm_init.py",
        "scripts/ltm_doctor.py", "scripts/ltm_schedule.py", "scripts/ltm_seed.py",
        "scripts/ltm_uninstall.py", "scripts/ltm_version.py", "scripts/ltm_blocks.py",
        "scripts/ltm_paths.py", "scripts/ltm_hooks.py", "scripts/ltm_session_start.py",
        "scripts/ltm_precompact.py"]
missing = [n for n in need if not (d / n).is_file()]
if missing:
    print("ПОМИЛКА: не вистачає файлів: " + ", ".join(missing)); sys.exit(1)
print("перевірка: усі файли на місці")
PY

say ""
say "Готово. Далі:"
say "  1. Перезапусти агента, щоб він побачив новий скіл."
say "  2. Попроси його: «розгорни довготривалу пам'ять»."
say "  3. Або запусти розвідку вручну:"
say "     $PY \"$first/scripts/ltm_detect.py\""
