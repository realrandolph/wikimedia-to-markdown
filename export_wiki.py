#!/usr/bin/env python3
"""
Crawl a MediaWiki-style wiki starting from an index URL and export a Custom-GPT-friendly corpus.

Design goals:
- One Markdown file per page (stable title + clean body text).
- No JS execution (HTML is parsed; <script>/<style> removed).
- Obey robots.txt (including Crawl-delay when present).
- Stay on the same origin and (by default) only crawl /wiki/ pages.

Usage:
  python export_wiki.py "https://ringofbrodgar.com" -o out_ring -n 2000
  python export_wiki.py "https://ringofbrodgar.com/wiki/Category:Buildings" -o out_ring
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from typing import Iterable, Optional
from urllib.parse import urljoin, urldefrag, urlparse

import requests
from bs4 import BeautifulSoup  # pip install beautifulsoup4
from urllib import robotparser


@dataclass(frozen=True)
class PageOut:
    url: str
    title: str
    md_path: str
    fetched_at: str


def norm_url(u: str) -> str:
    u, _frag = urldefrag(u)
    return u.strip()


def same_origin(a: str, b: str) -> bool:
    pa, pb = urlparse(a), urlparse(b)
    return (pa.scheme, pa.netloc) == (pb.scheme, pb.netloc)


def looks_like_wiki_article(u: str, wiki_prefix: str) -> bool:
    p = urlparse(u).path
    return p.startswith(wiki_prefix) and not any(
        bad in u for bad in ("?oldid=", "&oldid=", "action=edit", "action=history", "diff=")
    )


def safe_filename_from_url(url: str) -> str:
    # Stable, collision-resistant filename.
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", urlparse(url).path.strip("/"))[:80].strip("-") or "index"
    return f"{slug}__{h}.md"


def parse_robots(base_url: str, ua: str) -> tuple[robotparser.RobotFileParser, Optional[int]]:
    robots_url = urljoin(base_url, "/robots.txt")
    rp = robotparser.RobotFileParser()
    rp.set_url(robots_url)
    crawl_delay = None

    try:
        # robotparser can read(), but it does not reliably expose Crawl-delay, so fetch manually too.
        rp.read()
    except Exception:
        pass

    try:
        r = requests.get(robots_url, headers={"User-Agent": ua}, timeout=20)
        if r.ok:
            txt = r.text
            # Very small parser for Crawl-delay under User-agent: * (or exact UA)
            cur_agents: list[str] = []
            for raw in txt.splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r"(?i)user-agent:\s*(.+)$", line)
                if m:
                    cur_agents = [m.group(1).strip()]
                    continue
                m = re.match(r"(?i)crawl-delay:\s*(\d+)\s*$", line)
                if m and cur_agents:
                    if "*" in cur_agents or ua.lower() in (a.lower() for a in cur_agents):
                        crawl_delay = int(m.group(1))
                        break
    except Exception:
        pass

    return rp, crawl_delay


def extract_main_md(html: str, url: str) -> tuple[str, str]:
    """
    Extract title and main body from MediaWiki-like HTML and convert to lightweight Markdown.
    """
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    # Title
    title = ""
    h1 = soup.select_one("#firstHeading")
    if h1:
        title = h1.get_text(" ", strip=True)
    if not title:
        title = soup.title.get_text(" ", strip=True) if soup.title else url

    # Content root (MediaWiki)
    root = soup.select_one("#mw-content-text") or soup.select_one("#bodyContent") or soup.body
    if root is None:
        return title, ""

    # Remove common chrome
    for sel in (
        ".mw-editsection",
        ".toc",
        ".navbox",
        ".metadata",
        ".mw-jump-link",
        ".printfooter",
        "sup.reference",
        "ol.references",
    ):
        for t in root.select(sel):
            t.decompose()

    # Convert tables into simple text blocks (keep some structure, avoid huge noise)
    for table in root.find_all("table"):
        # Replace table with a plaintext approximation
        rows = []
        for tr in table.find_all("tr"):
            cells = []
            for cell in tr.find_all(["th", "td"]):
                txt = cell.get_text(" ", strip=True)
                if txt:
                    cells.append(txt)
            if cells:
                rows.append(" | ".join(cells))
        repl = soup.new_tag("div")
        if rows:
            repl.string = "\n".join(rows)
        table.replace_with(repl)

    # Headings and paragraphs to Markdown-ish text
    parts: list[str] = []

    def push(text: str) -> None:
        text = unescape(text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = text.strip()
        if text:
            parts.append(text)

    # Walk main content preserving rough structure
    for node in root.descendants:
        if getattr(node, "name", None) in ("h1", "h2", "h3", "h4", "h5"):
            lvl = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5}[node.name]
            txt = node.get_text(" ", strip=True)
            if txt:
                push(f"\n{'#' * lvl} {txt}\n")
        elif getattr(node, "name", None) in ("p", "li"):
            txt = node.get_text(" ", strip=True)
            if txt:
                push(txt)
        elif getattr(node, "name", None) == "pre":
            txt = node.get_text("\n", strip=False)
            if txt.strip():
                push("\n```text\n" + txt.strip("\n") + "\n```\n")
        elif getattr(node, "name", None) == "blockquote":
            txt = node.get_text("\n", strip=True)
            if txt:
                quoted = "\n".join(["> " + line for line in txt.splitlines() if line.strip()])
                push(quoted)

    md = "\n\n".join(parts)
    # Light cleanup for link artifacts
    md = re.sub(r"\s+\[edit\]\s*", " ", md, flags=re.I)
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return title, md


def extract_links(html: str, base_url: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue
        absu = norm_url(urljoin(base_url, href))
        links.add(absu)
    return links


def write_page(out_dir: str, page: PageOut, content: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    full = os.path.join(out_dir, page.md_path)
    with open(full, "w", encoding="utf-8") as f:
        f.write(f"---\n")
        f.write(f'title: "{page.title.replace("\"", "\'")}"\n')
        f.write(f'source_url: "{page.url}"\n')
        f.write(f'fetched_at: "{page.fetched_at}"\n')
        f.write(f"---\n\n")
        f.write(content)
        f.write("\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("start_url", help="Index/category/home page URL to begin crawling from")
    ap.add_argument("-o", "--out", default="wiki_export", help="Output folder")
    ap.add_argument("-n", "--max-pages", type=int, default=1500, help="Max pages to export")
    ap.add_argument("--user-agent", default="WikiExportBot/1.0 (respectful; contact: none)", help="HTTP User-Agent")
    ap.add_argument("--timeout", type=int, default=25, help="Request timeout seconds")
    ap.add_argument("--wiki-prefix", default="/wiki/", help="Only crawl paths starting with this prefix")
    ap.add_argument("--delay", type=float, default=None, help="Override crawl delay seconds (default: robots.txt if set)")
    ap.add_argument("--no-robots", action="store_true", help="Ignore robots.txt (not recommended)")
    args = ap.parse_args()

    start_url = norm_url(args.start_url)
    base = f"{urlparse(start_url).scheme}://{urlparse(start_url).netloc}"

    rp, robots_delay = parse_robots(base, args.user_agent)
    delay = args.delay if args.delay is not None else (robots_delay if robots_delay is not None else 1.0)

    sess = requests.Session()
    sess.headers.update({"User-Agent": args.user_agent})

    out_pages = os.path.join(args.out, "pages")
    os.makedirs(out_pages, exist_ok=True)
    manifest_path = os.path.join(args.out, "manifest.jsonl")
    seen_path = os.path.join(args.out, "seen_urls.txt")

    q = deque([start_url])
    seen: set[str] = set()
    exported = 0
    last_fetch_at = 0.0

    # Resume support
    if os.path.exists(seen_path):
        with open(seen_path, "r", encoding="utf-8") as f:
            for line in f:
                u = line.strip()
                if u:
                    seen.add(u)

    def can_fetch(u: str) -> bool:
        if not same_origin(u, base):
            return False
        if not looks_like_wiki_article(u, args.wiki_prefix):
            return False
        if args.no_robots:
            return True
        try:
            return rp.can_fetch(args.user_agent, u)
        except Exception:
            return True

    with open(manifest_path, "a", encoding="utf-8") as manifest:
        while q and exported < args.max_pages:
            url = norm_url(q.popleft())
            if url in seen:
                continue
            seen.add(url)

            if not can_fetch(url):
                continue

            # Respect delay between requests
            now = time.time()
            wait = (last_fetch_at + delay) - now
            if wait > 0:
                time.sleep(wait)

            try:
                r = sess.get(url, timeout=args.timeout)
                last_fetch_at = time.time()
                if not r.ok or "text/html" not in r.headers.get("Content-Type", ""):
                    continue

                title, md = extract_main_md(r.text, url)
                if not md.strip():
                    continue

                md_file = safe_filename_from_url(url)
                fetched_at = datetime.now(timezone.utc).isoformat()

                page = PageOut(url=url, title=title, md_path=md_file, fetched_at=fetched_at)
                write_page(out_pages, page, md)

                manifest.write(json.dumps(page.__dict__, ensure_ascii=False) + "\n")
                manifest.flush()

                exported += 1

                # Enqueue links
                for link in extract_links(r.text, url):
                    if link not in seen and can_fetch(link):
                        q.append(link)

            except requests.RequestException:
                continue

    # Persist seen URLs for resume
    os.makedirs(args.out, exist_ok=True)
    with open(seen_path, "w", encoding="utf-8") as f:
        for u in sorted(seen):
            f.write(u + "\n")

    print(f"Exported {exported} pages to: {os.path.abspath(args.out)}")
    print(f"Pages folder: {os.path.abspath(out_pages)}")
    print(f"Manifest: {os.path.abspath(manifest_path)}")
    if not args.no_robots and robots_delay is not None and args.delay is None:
        print(f"Used robots.txt Crawl-delay: {robots_delay} seconds")
    elif args.delay is not None:
        print(f"Used override delay: {delay} seconds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
