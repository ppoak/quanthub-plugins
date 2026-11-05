#!/usr/bin/env python3
"""
Download a BigQuant wiki report PDF from a wiki page, viewer URL, or PDF URL.
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

WEB_HOST = "https://bigquant.com"
UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.3 "
    "Mobile/15E148 Safari/604.1"
)
CHUNK_SIZE = 256 * 1024


class BigQuantError(RuntimeError):
    pass


def project_root(value: str | Path | None = None) -> Path:
    return Path(value or Path.cwd()).expanduser().resolve()


def load_dotenv(path: Path) -> dict[str, str]:
    """Read project values without changing process-level overrides."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            continue
        value = value.strip()
        if value and value[0] in "\"'":
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end >= 0 else value[1:]
        else:
            value = value.split(" #", maxsplit=1)[0].strip()
        values[key.strip()] = value
    return values


def resolve_output_dir(value: str, *, root: Path | None = None) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = project_root(root) / path
    return path.resolve()


def http_request(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 60,
    raise_http_error: bool = True,
) -> tuple[bytes, dict[str, str], str, int]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Accept-Language": "zh-CN,zh-Hans;q=0.9",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), dict(resp.headers), resp.geturl(), resp.status
    except urllib.error.HTTPError as exc:
        if not raise_http_error:
            return exc.read(), dict(exc.headers), exc.geturl(), exc.code
        body = exc.read().decode("utf-8", "replace")
        raise BigQuantError(f"HTTP {exc.code} for {url}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise BigQuantError(f"Could not fetch {url}: {exc}") from exc


def absolute_url(value: str, base_url: str = WEB_HOST) -> str:
    value = html.unescape(value.strip())
    return urllib.parse.urljoin(base_url, value)


def normalize_pdf_url(value: str, base_url: str = WEB_HOST) -> str | None:
    if not value:
        return None
    unescaped = html.unescape(value)
    parsed = urllib.parse.urlparse(unescaped)
    query = urllib.parse.parse_qs(parsed.query)
    if parsed.path.endswith("/wiki/pdfjs/web/viewer.html") and query.get("file"):
        return absolute_url(query["file"][0], WEB_HOST)
    if parsed.path.lower().endswith(".pdf"):
        return absolute_url(unescaped, base_url)
    return None


def extract_title(page_html: str, fallback: str) -> str:
    patterns = [
        r"<h1[^>]*>(.*?)</h1>",
        r"<title[^>]*>(.*?)</title>",
    ]
    for pattern in patterns:
        match = re.search(pattern, page_html, re.I | re.S)
        if match:
            title = strip_tags(html.unescape(match.group(1))).strip()
            title = re.sub(r"\s+-\s+BigQuant.*$", "", title).strip()
            if title:
                return title
    return fallback


def strip_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value)


def extract_pdf_url(page_url: str) -> tuple[str, str]:
    direct_pdf = normalize_pdf_url(page_url)
    if direct_pdf:
        return direct_pdf, report_key_from_url(direct_pdf)

    body, _, final_url, _ = http_request(
        page_url,
        headers={
            "Referer": f"{WEB_HOST}/",
        },
    )
    page_html = body.decode("utf-8", "replace")

    candidates: list[str] = []
    for match in re.finditer(
        r"""(?:href|src)=["']([^"']+\.pdf(?:\?[^"']*)?)["']""", page_html, re.I
    ):
        candidates.append(match.group(1))
    for match in re.finditer(
        r"""["']([^"']*/wiki/static/upload/[^"']+\.pdf(?:\?[^"']*)?)["']""",
        page_html,
        re.I,
    ):
        candidates.append(match.group(1))
    for match in re.finditer(
        r"""viewer\.html\?file=([^"'&<>\s]+\.pdf)""", page_html, re.I
    ):
        candidates.append(match.group(1))

    for candidate in candidates:
        pdf_url = normalize_pdf_url(candidate, final_url)
        if pdf_url:
            return pdf_url, extract_title(page_html, report_key_from_url(pdf_url))

    raise BigQuantError("Could not find a PDF link on the BigQuant wiki page.")


def report_key_from_url(url: str) -> str:
    name = Path(urllib.parse.urlparse(url).path).stem
    return name or "bigquant-report"


def output_name(title: str, pdf_url: str) -> str:
    suffix = report_key_from_url(pdf_url)
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", title).strip(" ._")
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned[:90] or suffix
    if suffix and suffix not in cleaned:
        return f"{cleaned}-{suffix}.pdf"
    return f"{cleaned}.pdf"


def content_range_total(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"bytes\s+\d+-\d+/(\d+)", value)
    if not match:
        return None
    return int(match.group(1))


def download_pdf(pdf_url: str, dest: Path, *, referer: str | None = None) -> None:
    headers = {
        "Referer": referer
        or f"{WEB_HOST}/wiki/pdfjs/web/viewer.html?file={urllib.parse.urlparse(pdf_url).path}",
    }
    body, resp_headers, final_url, status = http_request(
        pdf_url,
        headers={**headers, "Range": f"bytes=0-{CHUNK_SIZE - 1}"},
        timeout=120,
        raise_http_error=False,
    )
    ctype = resp_headers.get("Content-Type", "")
    if status >= 400:
        message = body.decode("utf-8", "replace")
        raise BigQuantError(f"HTTP {status} for {pdf_url}: {message[:500]}")
    if status == 206:
        total = content_range_total(resp_headers.get("Content-Range"))
        if not total:
            raise BigQuantError(
                "Server returned a partial PDF without Content-Range total."
            )
        body = download_pdf_ranges(final_url, total, headers=headers, first_chunk=body)

    if not body.startswith(b"%PDF") and "pdf" not in ctype.lower():
        raise BigQuantError(
            f"Downloaded content is not a PDF from {final_url}; "
            f"content-type={ctype!r}, first bytes={body[:80]!r}"
        )
    dest.write_bytes(body)


def download_pdf_ranges(
    pdf_url: str,
    total: int,
    *,
    headers: dict[str, str],
    first_chunk: bytes | None = None,
) -> bytes:
    chunks = [first_chunk or b""]
    start = len(chunks[0])
    while start < total:
        end = min(start + CHUNK_SIZE - 1, total - 1)
        chunk, _, _, status = http_request(
            pdf_url,
            headers={**headers, "Range": f"bytes={start}-{end}"},
            timeout=120,
        )
        if status != 206:
            raise BigQuantError(
                f"Expected HTTP 206 for range {start}-{end}, got {status}."
            )
        chunks.append(chunk)
        start += len(chunk)
        if not chunk:
            raise BigQuantError(f"Empty range response at offset {start}.")
    return b"".join(chunks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "url", help="BigQuant wiki page, PDF.js viewer URL, or PDF URL."
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        help="Output directory; defaults to REPORT_DOWNLOAD_DIR from .env.",
    )
    parser.add_argument("--output-name", help="Override the output PDF file name.")
    parser.add_argument("--project-root", default=".", help="Data project root.")
    args = parser.parse_args(argv)
    root = project_root(args.project_root)
    if not root.is_dir():
        parser.error(f"project root does not exist: {root}")
    settings = load_dotenv(root / ".env")
    output_dir = (
        args.output_dir
        or os.environ.get("REPORT_DOWNLOAD_DIR")
        or settings.get("REPORT_DOWNLOAD_DIR")
        or "references/report-download"
    )

    pdf_url, title = extract_pdf_url(args.url)
    dest_dir = resolve_output_dir(output_dir, root=root)
    dest_dir.mkdir(parents=True, exist_ok=True)
    file_name = args.output_name or output_name(title, pdf_url)
    if not file_name.lower().endswith(".pdf"):
        file_name += ".pdf"
    file_path = dest_dir / file_name

    download_pdf(pdf_url, file_path, referer=args.url)
    print(f"pdf_url={pdf_url}")
    print(file_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BigQuantError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
