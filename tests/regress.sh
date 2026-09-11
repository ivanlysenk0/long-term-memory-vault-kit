#!/usr/bin/env bash
# Регресс: проверить, что все сценарии обновления работают в обоих репозиториях.
set -u
PASS=0; FAIL=0
ok(){ echo "  OK   $1"; PASS=$((PASS+1)); }
no(){ echo "  FAIL $1"; FAIL=$((FAIL+1)); }

for REPO in this; do
  echo "=============== $REPO ==============="
  S="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"/skill/scripts
  B=/tmp/reg_$REPO; rm -rf $B; mkdir -p $B; export HOME=$B

  # 1. чистая установка новой версией
  python3 $S/ltm_init.py --path $B/v1 --projects a,b --providers claude --yes --no-seed --no-schedule --no-verify >/dev/null 2>&1
  [ -f $B/v1/.ltm-install-manifest.json ] && ok "чистая установка" || no "чистая установка"
  grep -q scripts_version $B/v1/.ltm-install-manifest.json && ok "штамп версии" || no "штамп версии"
  grep -q rules_hash $B/v1/.ltm-install-manifest.json && ok "штамп правил" || no "штамп правил"

  # 2. migrate сразу после установки = нечего делать
  python3 $S/ltm_init.py --migrate --path $B/v1 --dry-run 2>&1 | grep -qiE "нічого|Nothing to migrate" && ok "migrate идемпотентен" || no "migrate идемпотентен"

  # 3. update сразу после установки = нечего делать
  python3 $S/ltm_init.py --update --path $B/v1 --yes 2>&1 | grep -qiE "збігаються|already match" && ok "update идемпотентен" || no "update идемпотентен"

  # 4. записи не трогаются при migrate
  printf -- '---\ntitle: "t"\ndate: 2026-01-01\nproject: a\nagent: x\ntype: decision\ntags:\n  - t\nstatus: active\nsources: 0\n---\n\nмои данные\n' > $B/v1/a/knowledge/decisions/mine.md
  H1=$(md5sum $B/v1/a/knowledge/decisions/mine.md | cut -d' ' -f1)
  python3 $S/ltm_init.py --migrate --path $B/v1 --yes >/dev/null 2>&1
  H2=$(md5sum $B/v1/a/knowledge/decisions/mine.md | cut -d' ' -f1)
  [ "$H1" = "$H2" ] && ok "записи целы" || no "записи целы"

  # 5. правленые правила получают .new и не затираются
  printf '\n## моё\nне трогай\n' >> $B/v1/CLAUDE.md
  H3=$(md5sum $B/v1/CLAUDE.md | cut -d' ' -f1)
  python3 $S/ltm_init.py --migrate --path $B/v1 --yes >/dev/null 2>&1
  H4=$(md5sum $B/v1/CLAUDE.md | cut -d' ' -f1)
  [ "$H3" = "$H4" ] && ok "правленые правила целы" || no "правленые правила целы"
  [ -f $B/v1/CLAUDE.md.new ] && ok ".new создан" || no ".new создан"

  # 6. повторный прогон не предлагает то же самое снова
  python3 $S/ltm_init.py --migrate --path $B/v1 --dry-run 2>&1 | grep -qiE "розібрати вручну|resolve by hand" && ok "не спамит повторно" || no "не спамит повторно"

  # 7. ltm_version коды возврата
  python3 $S/ltm_version.py --vault $B/v1 --offline >/dev/null 2>&1
  [ $? -eq 0 ] && ok "version: код 0 когда всё совпало" || no "version: код 0"
  python3 - "$B/v1" <<'PY'
import json,pathlib,sys
p=pathlib.Path(sys.argv[1])/'.ltm-install-manifest.json'
d=json.loads(p.read_text()); d['scripts_version']='0.1.0'
p.write_text(json.dumps(d,ensure_ascii=False,indent=2))
PY
  python3 $S/ltm_version.py --vault $B/v1 --offline >/dev/null 2>&1
  [ $? -eq 2 ] && ok "version: код 2 при расхождении" || no "version: код 2"

  # 8. несуществующие пути
  python3 $S/ltm_init.py --migrate --path /tmp/nope_$$ --dry-run >/dev/null 2>&1
  [ $? -eq 1 ] && ok "migrate: код 1 на пустом пути" || no "migrate: код 1"
  python3 $S/ltm_init.py --update --path /tmp/nope_$$ --yes >/dev/null 2>&1
  [ $? -eq 1 ] && ok "update: код 1 на пустом пути" || no "update: код 1"

  # 9. не наша папка
  mkdir -p $B/notours && python3 $S/ltm_init.py --migrate --path $B/notours --dry-run >/dev/null 2>&1
  [ $? -eq 1 ] && ok "migrate отказывается от чужой папки" || no "migrate отказывается от чужой папки"

  # 10. uninstall жив
  python3 $B/v1/scripts/ltm_uninstall.py --survey >/dev/null 2>&1 && ok "uninstall работает" || no "uninstall работает"
done
echo
echo "ИТОГО: PASS=$PASS FAIL=$FAIL"
