#!/usr/bin/env python3
"""Наповнення пам'яті готовим вмістом (seed).

Дві ролі:
  - власник: `--pack` збирає вміст пам'яті в зашифрований файл
  - колега:  `--unpack` розшифровує його і ЗЛИВАЄ з наявною пам'яттю

Головне правило злиття: нічого не перезаписуємо. Якщо файл уже є, seed-версія
кладеться поруч із суфіксом `.seed.md`, а рішення лишається за людиною.
Пам'ять колеги дорожча за будь-який шаблон.

Шифрування: scrypt для ключа з пароля, AES-256-GCM для даних.
GCM обраний свідомо: він ловить підміну файлу, а не лише приховує вміст.
"""

from __future__ import annotations

__version__ = "1.1.0"

import argparse
import getpass
import hashlib
import io
import json
import os
import secrets
import sys
import tarfile
from pathlib import Path

MAGIC = b"LTMSEED1"
SALT_LEN = 16
NONCE_LEN = 12
# Параметри scrypt: підбір пароля дорогий, розпакування у колеги ~1 секунда.
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1

# Те, що ніколи не потрапляє в seed. .git тримає всю історію, включно
# з тим, що колись видаляли: у чужі руки він їхати не повинен.
EXCLUDE_DIRS = {".git", ".obsidian", "__pycache__", ".venv", "venv", "node_modules", ".idea"}
EXCLUDE_FILES = {".DS_Store", "Thumbs.db", ".ltm-vault"}
EXCLUDE_SUFFIX = {".pyc", ".log", ".tmp", ".swp"}


# --- криптографія ------------------------------------------------------------

def _aesgcm(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        print("Потрібна бібліотека cryptography. Встанови її:")
        print("  python3 -m pip install cryptography")
        sys.exit(2)
    return AESGCM(key)


def derive_key(password: str, salt: bytes) -> bytes:
    # hashlib.scrypt тягне OpenSSL, а він має власний ліміт пам'яті і падає
    # на N=2**15 з "memory limit exceeded". Реалізація з cryptography цього
    # ліміту не має, тому вона основна, а hashlib лишається запасним шляхом.
    try:
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
        return Scrypt(salt=salt, length=32, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P).derive(
            password.encode("utf-8"))
    except ImportError:
        pass
    try:
        return hashlib.scrypt(password.encode("utf-8"), salt=salt,
                              n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    except ValueError:
        # Останній рубіж: PBKDF2 є всюди й ніколи не впирається в ліміт.
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                                   600_000, dklen=32)


def encrypt(data: bytes, password: str) -> bytes:
    salt = secrets.token_bytes(SALT_LEN)
    nonce = secrets.token_bytes(NONCE_LEN)
    key = derive_key(password, salt)
    return MAGIC + salt + nonce + _aesgcm(key).encrypt(nonce, data, MAGIC)


def decrypt(blob: bytes, password: str) -> bytes:
    if not blob.startswith(MAGIC):
        raise ValueError("це не файл seed або він пошкоджений")
    off = len(MAGIC)
    salt = blob[off:off + SALT_LEN]
    nonce = blob[off + SALT_LEN:off + SALT_LEN + NONCE_LEN]
    body = blob[off + SALT_LEN + NONCE_LEN:]
    key = derive_key(password, salt)
    try:
        return _aesgcm(key).decrypt(nonce, body, MAGIC)
    except Exception:
        raise ValueError("не вдалося розшифрувати: невірний пароль або файл змінено")


# --- збирання ----------------------------------------------------------------

def collect(vault: Path, projects: list[str] | None) -> tuple[bytes, dict]:
    """Запакувати вміст пам'яті в tar.gz у пам'яті."""
    buf = io.BytesIO()
    stats = {"projects": {}, "files": 0}

    def keep(p: Path) -> bool:
        if any(part in EXCLUDE_DIRS for part in p.parts):
            return False
        if p.name in EXCLUDE_FILES or p.suffix in EXCLUDE_SUFFIX:
            return False
        return True

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for item in sorted(vault.rglob("*")):
            if not item.is_file() or not keep(item.relative_to(vault)):
                continue
            rel = item.relative_to(vault)
            top = rel.parts[0]
            # Якщо перелік проєктів заданий, беремо лише їх плюс глобальне.
            if projects and top not in projects and top != "00-global-home":
                continue
            tar.add(item, arcname=str(rel))
            stats["files"] += 1
            stats["projects"][top] = stats["projects"].get(top, 0) + 1
    return buf.getvalue(), stats


# --- злиття ------------------------------------------------------------------

def merge(archive: bytes, vault: Path, dry_run: bool = False) -> dict:
    """Розпакувати seed у пам'ять, не перезаписуючи наявні файли.

    Файли лягають за своїми шляхами всередині пам'яті, тому проєктні сторінки
    потрапляють саме у свої проєкти. Якщо проєкту в колеги ще немає, він
    створюється; якщо є, вміст додається поруч із наявним.
    """
    report = {"added": [], "conflicts": [], "projects": set()}
    buf = io.BytesIO(archive)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            rel = Path(member.name)
            # Захист від виходу за межі пам'яті: архів міг бути підроблений.
            if rel.is_absolute() or ".." in rel.parts:
                continue
            dst = vault / rel
            if rel.parts:
                report["projects"].add(rel.parts[0])

            if dst.exists():
                old = dst.read_bytes()
                new = tar.extractfile(member).read()
                if old == new:
                    continue
                # Не чіпаємо чуже: кладемо поруч, рішення за людиною.
                side = dst.parent / (dst.stem + ".seed" + dst.suffix)
                report["conflicts"].append(str(rel))
                if not dry_run:
                    side.parent.mkdir(parents=True, exist_ok=True)
                    side.write_bytes(new)
                continue

            report["added"].append(str(rel))
            if not dry_run:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(tar.extractfile(member).read())

    report["projects"] = sorted(report["projects"])
    return report


def find_vault(explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_dir() else None
    for base in [Path.home(), Path.home() / "Documents"]:
        try:
            for marker in base.glob("*/.ltm-vault"):
                return marker.parent
        except OSError:
            continue
    return None


def ask_password(confirm: bool = False) -> str:
    p = getpass.getpass("Пароль: ")
    if not p:
        print("Порожній пароль не приймається.")
        sys.exit(1)
    if confirm:
        again = getpass.getpass("Ще раз: ")
        if p != again:
            print("Паролі не збігаються.")
            sys.exit(1)
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description="Наповнення пам'яті готовим вмістом")
    ap.add_argument("--pack", metavar="OUT", help="зібрати seed із пам'яті у файл")
    ap.add_argument("--unpack", metavar="FILE", help="влити seed у пам'ять")
    ap.add_argument("--vault", help="шлях до пам'яті")
    ap.add_argument("--projects", help="лише ці проєкти, через кому (для --pack)")
    ap.add_argument("--dry-run", action="store_true", help="показати, що буде, без запису")
    ap.add_argument("--password-file", help="файл із паролем, щоб не вводити руками")
    a = ap.parse_args()

    if not a.pack and not a.unpack:
        ap.print_help()
        return 1

    vault = find_vault(a.vault)
    if not vault:
        print("Не знайдено пам'ять. Вкажи шлях: --vault /шлях")
        return 1

    def get_pw(confirm: bool) -> str:
        if a.password_file:
            return Path(a.password_file).expanduser().read_text(encoding="utf-8").strip()
        return ask_password(confirm)

    if a.pack:
        projects = [p.strip() for p in a.projects.split(",")] if a.projects else None
        data, stats = collect(vault, projects)
        print(f"Зібрано файлів: {stats['files']}")
        for proj, n in sorted(stats["projects"].items()):
            print(f"  {proj}: {n}")
        blob = encrypt(data, get_pw(confirm=True))
        out = Path(a.pack).expanduser()
        out.write_bytes(blob)
        print(f"\nЗашифрований seed: {out}  ({len(blob) / 1024:.1f} КБ)")
        print("Пароль передавай окремим каналом, не разом із файлом.")
        return 0

    src = Path(a.unpack).expanduser()
    if not src.is_file():
        print(f"Файл не знайдено: {src}")
        return 1
    try:
        data = decrypt(src.read_bytes(), get_pw(confirm=False))
    except ValueError as e:
        print(f"ПОМИЛКА: {e}")
        return 1

    rep = merge(data, vault, dry_run=a.dry_run)
    print(f"\nПам'ять: {vault}")
    print(f"Проєкти в seed: {', '.join(rep['projects'])}")
    print(f"Додано файлів: {len(rep['added'])}")
    if rep["conflicts"]:
        print(f"Уже існували, тому покладено поруч як *.seed.md: {len(rep['conflicts'])}")
        for c in rep["conflicts"][:10]:
            print(f"  {c}")
        if len(rep["conflicts"]) > 10:
            print(f"  ... і ще {len(rep['conflicts']) - 10}")
        print("Перевір їх і виріши, що лишити. Твої файли не змінені.")
    if a.dry_run:
        print("\nЦе був --dry-run, нічого не записано.")
    else:
        print("\nПеревір пам'ять: python3 scripts/ltm_doctor.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
