#!/usr/bin/env python3
"""Собирает локальное зеркало главной страницы https://multibrand.tech/ из архива.

Источник — снимок Wayback Machine от 16.09.2025 (та же дата, что у копии на web-arhive.ru;
сам web-arhive.ru отдаёт содержимое только через JS, поэтому для выгрузки кода не годится).

Скрипт идемпотентный: уже скачанные файлы повторно не запрашиваются, поэтому сборку можно
запускать несколько раз подряд — каждый запуск докачивает остаток.

  1. читает архивный HTML (archive/wayback.html);
  2. собирает ссылки на ассеты из атрибутов HTML, из url() в CSS и из import в JS;
  3. качает их в multibrand/ с сохранением путей (curl, вежливая очередь);
  4. в конце переписывает ссылки: на скачанные файлы — относительные, на остальное — архивные.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse

TIMESTAMP = "20250916231958"
ORIGIN = "https://multibrand.tech"
OUT_DIR = "multibrand"
ARCHIVE_BASE = f"https://web.archive.org/web/{TIMESTAMP}/https://multibrand.tech/"
RAW_HTML = "archive/wayback.html"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

ASSET_EXT = {
    ".css", ".js", ".mjs", ".json", ".map",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".svg", ".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif", ".ico",
    ".webmanifest", ".mp4", ".webm", ".txt", ".xml",
}

DELAY = 0.3
MAX_FILES_PER_RUN = 0
MAX_BYTES = 90 * 1024 * 1024
ROUNDS = 2

failed: list[str] = []
downloaded: list[str] = []
total_bytes = 0


def archive_url(path: str, raw: bool = True) -> str:
    mode = "id_" if raw else ""
    return f"https://web.archive.org/web/{TIMESTAMP}{mode}/{ORIGIN}{path}"


def page_url(path: str) -> str:
    return archive_url(path, raw=False)


def normalize(url: str, base_dir: str) -> str | None:
    """Возвращает корневой путь сайта ('/a/b.css') или None для внешних ссылок."""
    if not url:
        return None
    url = url.strip()
    if url.startswith(("data:", "mailto:", "tel:", "javascript:", "#", "//")):
        return None
    if url.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(url)
        if parsed.netloc and parsed.netloc not in ("multibrand.tech", "www.multibrand.tech"):
            return None
        url = parsed.path
    url = url.split("#")[0].split("?")[0]
    if not url:
        return None
    if url.startswith("/"):
        return url
    return os.path.normpath(os.path.join(base_dir, url))


def is_asset(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in ASSET_EXT


def local_path(path: str) -> str:
    return os.path.join(OUT_DIR, path.lstrip("/"))


def have(path: str) -> bool:
    return os.path.exists(local_path(path))


def rel_from(file_path: str, target: str) -> str:
    from_dir = os.path.dirname(file_path) or "/"
    return os.path.relpath(target, from_dir).replace(os.sep, "/")


# ---------------------------------------------------------------- скачивание


def fetch(path: str) -> bytes | None:
    global total_bytes
    if total_bytes > MAX_BYTES:
        return None
    code = ""
    for attempt in range(2):
        tmp_name = tempfile.mktemp()
        try:
            proc = subprocess.run(
                [
                    "curl", "-sL", "--compressed", "--max-time", "20",
                    "-A", UA, "-o", tmp_name, "-w", "%{http_code}", archive_url(path),
                ],
                capture_output=True,
                text=True,
            )
            code = (proc.stdout or "").strip()
            if code == "200":
                with open(tmp_name, "rb") as fh:
                    data = fh.read()
                total_bytes += len(data)
                return data
            if code in ("404", "410"):
                failed.append(f"{path} (HTTP {code})")
                return None
        except Exception as exc:  # noqa: BLE001
            code = type(exc).__name__
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        time.sleep(4 * (attempt + 1))
    failed.append(f"{path} (HTTP {code or 'timeout'})")
    return None


def save(path: str, data: bytes) -> None:
    dest = local_path(path)
    os.makedirs(os.path.dirname(dest) or OUT_DIR, exist_ok=True)
    with open(dest, "wb") as fh:
        fh.write(data)
    downloaded.append(path)


# ---------------------------------------------------------------- ссылки


CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)([^'\")]+)\1\s*\)")
CSS_IMPORT_RE = re.compile(r"@import\s+(?:url\(\s*)?(['\"])([^'\"]+)\1")
JS_IMPORT_RE = re.compile(r"""(?:import\s*(?:\(\s*)?|from\s*)\s*['"]([^'"]+)['"]""")
JS_ASSET_RE = re.compile(
    r"""['"](/[A-Za-z0-9_./\-]+\.(?:js|css|json|woff2?|svg|png|webp|jpg|avif))['"]"""
)
HTML_ATTR_RE = re.compile(r"""(\b(?:src|href|data-src|poster)\s*=\s*)(["'])(.*?)\2""")


def refs_from_html(html: str) -> set[str]:
    found: set[str] = set()
    for match in HTML_ATTR_RE.finditer(html):
        target = normalize(match.group(3), "/")
        if target and is_asset(target):
            found.add(target)
    return found


def refs_from_css(path: str, text: str) -> set[str]:
    found: set[str] = set()
    for _, url in CSS_URL_RE.findall(text):
        target = normalize(url, os.path.dirname(path))
        if target:
            found.add(target)
    for _, url in CSS_IMPORT_RE.findall(text):
        target = normalize(url, os.path.dirname(path))
        if target:
            found.add(target)
    return found


def refs_from_js(path: str, text: str) -> set[str]:
    found: set[str] = set()
    for spec in JS_IMPORT_RE.findall(text):
        target = normalize(spec, os.path.dirname(path))
        if target:
            found.add(target)
    for spec in JS_ASSET_RE.findall(text):
        found.add(spec)
    return found


def refs_from_manifest(path: str, text: str) -> set[str]:
    found: set[str] = set()
    try:
        manifest = json.loads(text)
    except Exception:  # noqa: BLE001
        return found
    for icon in manifest.get("icons", []):
        target = normalize(icon.get("src", ""), os.path.dirname(path))
        if target:
            found.add(target)
    return found


def scan_local(path: str) -> set[str]:
    """Ссылки внутри уже скачанного файла."""
    ext = os.path.splitext(path)[1].lower()
    try:
        text = open(local_path(path), encoding="utf-8").read()
    except (UnicodeDecodeError, OSError):
        return set()
    if ext == ".css":
        return refs_from_css(path, text)
    if ext in (".js", ".mjs"):
        return refs_from_js(path, text)
    if ext == ".webmanifest":
        return refs_from_manifest(path, text)
    return set()


# ---------------------------------------------------------------- переписывание


def rewrite_css_file(path: str) -> None:
    """url() в CSS: на скачанный файл — относительная ссылка, иначе — архивный адрес."""
    file_path = local_path(path)
    try:
        text = open(file_path, encoding="utf-8").read()
    except (UnicodeDecodeError, OSError):
        return

    def repl(match: re.Match[str]) -> str:
        quote, url = match.group(1), match.group(2)
        target = normalize(url, os.path.dirname(path))
        if not target:
            return match.group(0)
        final = rel_from(path, target) if have(target) else archive_url(target)
        return f"url({quote}{final}{quote})"

    new_text = CSS_URL_RE.sub(repl, text)

    def repl_import(match: re.Match[str]) -> str:
        quote, url = match.group(1), match.group(2)
        target = normalize(url, os.path.dirname(path))
        if not target:
            return match.group(0)
        final = rel_from(path, target) if have(target) else archive_url(target)
        return f"@import {quote}{final}{quote}"

    new_text = CSS_IMPORT_RE.sub(repl_import, new_text)
    if new_text != text:
        with open(file_path, "w", encoding="utf-8") as fh:
            fh.write(new_text)


def rewrite_html(html: str) -> str:
    def repl(match: re.Match[str]) -> str:
        prefix, quote, value = match.group(1), match.group(2), match.group(3)
        target = normalize(value, "/")
        if not target:
            return match.group(0)
        if is_asset(target) and have(target):
            return f"{prefix}{quote}{target.lstrip('/')}{quote}"
        return f"{prefix}{quote}{page_url(target)}{quote}"

    html = HTML_ATTR_RE.sub(repl, html)

    # Nuxt строит адреса ассетов из app.baseURL + buildAssetsDir. В зеркале страница лежит
    # в подпапке, поэтому базой делаем архивный адрес — тогда догружаемые скрипты и картинки
    # продолжают приходить из архива, а не с корня нашего сайта.
    html = html.replace(
        'app:{baseURL:"/",buildAssetsDir:"/_nuxt/"',
        'app:{baseURL:"' + ARCHIVE_BASE + '",buildAssetsDir:"/_nuxt/"',
    )

    # Страховка для архивной копии: если гидратация Nuxt не поднялась (часть чанков
    # в архиве не сохранилась), картинки всё равно появятся — берём адрес из data-src.
    fallback = (
        "<script>"
        "window.addEventListener('DOMContentLoaded',function(){"
        "setTimeout(function(){"
        "document.querySelectorAll('img[data-src]').forEach(function(img){"
        "if(!img.getAttribute('src'))img.setAttribute('src',img.getAttribute('data-src'));"
        "});"
        "},1500);"
        "});"
        "</script>"
    )
    if "</body>" in html:
        html = html.replace("</body>", fallback + "</body>")
    return html


# ---------------------------------------------------------------- main


def main() -> int:
    raw = open(RAW_HTML, encoding="utf-8", errors="replace").read()
    os.makedirs(OUT_DIR, exist_ok=True)

    pending = refs_from_html(raw)
    seen: set[str] = set()
    attempts = 0

    for round_no in range(1, ROUNDS + 1):
        queue = [p for p in sorted(pending) if p not in seen and is_asset(p)]
        queue = queue[: max(0, MAX_FILES_PER_RUN - attempts)]
        if not queue:
            break
        print(f"[round {round_no}] в очереди {len(queue)} файлов", flush=True)

        for index, path in enumerate(queue, 1):
            seen.add(path)
            if have(path):
                continue
            attempts += 1
            data = fetch(path)
            if data is not None:
                save(path, data)
            if index % 10 == 0:
                print(
                    f"[round {round_no}] {index}/{len(queue)}: скачано {len(downloaded)}, "
                    f"ошибок {len(failed)}",
                    flush=True,
                )
            time.sleep(DELAY)

        extra: set[str] = set()
        for path in list(downloaded):
            extra |= scan_local(path)
        new_refs = {p for p in extra if p not in seen and is_asset(p)}
        print(f"[round {round_no}] итог: файлов {len(downloaded)}, новых ссылок {len(new_refs)}", flush=True)
        if not new_refs:
            break
        pending |= new_refs

    # переписываем ссылки внутри CSS и в самом HTML
    for path in downloaded:
        if os.path.splitext(path)[1].lower() == ".css":
            rewrite_css_file(path)

    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(rewrite_html(raw))

    total_files = sum(len(files) for _, _, files in os.walk(OUT_DIR))
    report = [
        "Зеркало multibrand.tech — отчёт сборки",
        f"Источник: {ORIGIN} (Wayback Machine, снимок {TIMESTAMP})",
        "",
        f"Файлов в зеркале всего: {total_files}",
        f"Скачано в этом запуске: {len(downloaded)} ({total_bytes / 1024 / 1024:.1f} МБ)",
        f"Не удалось скачать в этом запуске: {len(failed)}",
        "",
    ]
    report += [f"- {item}" for item in sorted(failed)[:60]]
    with open(os.path.join(OUT_DIR, "_mirror-report.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(report) + "\n")

    print("\n".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
