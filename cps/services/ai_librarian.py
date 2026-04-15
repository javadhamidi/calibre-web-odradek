# -*- coding: utf-8 -*-

#  This file is part of the Calibre-Web (https://github.com/janeczku/calibre-web)
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.

"""Pure-logic helpers for the AI Librarian feature.

Intentionally has no Flask dependency so it is trivially unit-testable and
also so importing the module cannot itself fail for a non-AI reason.
"""

import math
import os
import re
import struct
import zipfile

import requests

from .. import logger

log = logger.create()


CHUNK_SIZE = 1200
CHUNK_OVERLAP = 200
CHUNK_HARD_CAP = 2000
TOTAL_CONTEXT_CHAR_BUDGET = 8000
SUPPORTED_FORMATS = ("epub", "pdf", "txt")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class OllamaError(Exception):
    """Base class for Ollama errors surfaced to the user."""


class OllamaUnreachable(OllamaError):
    pass


class OllamaModelMissing(OllamaError):
    pass


# ---------------------------------------------------------------------------
# Ollama client
# ---------------------------------------------------------------------------

class OllamaClient(object):
    def __init__(self, base_url, chat_model, embed_model, timeout=180):
        self.base_url = (base_url or "").rstrip("/")
        self.chat_model = chat_model
        self.embed_model = embed_model
        self.timeout = timeout

    def _post(self, path, payload):
        url = self.base_url + path
        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
        except requests.exceptions.ConnectionError as e:
            raise OllamaUnreachable("Cannot reach Ollama at %s: %s" % (self.base_url, e))
        except requests.exceptions.Timeout:
            raise OllamaUnreachable("Ollama request timed out")
        if resp.status_code == 404:
            body = (resp.text or "").lower()
            if "model" in body and ("not found" in body or "pull" in body):
                raise OllamaModelMissing(resp.text.strip())
            raise OllamaError("Ollama 404: " + resp.text)
        if resp.status_code >= 400:
            raise OllamaError("Ollama %d: %s" % (resp.status_code, resp.text))
        try:
            return resp.json()
        except ValueError:
            raise OllamaError("Ollama returned non-JSON response")

    def _get(self, path):
        url = self.base_url + path
        try:
            resp = requests.get(url, timeout=self.timeout)
        except requests.exceptions.ConnectionError as e:
            raise OllamaUnreachable("Cannot reach Ollama at %s: %s" % (self.base_url, e))
        except requests.exceptions.Timeout:
            raise OllamaUnreachable("Ollama request timed out")
        if resp.status_code >= 400:
            raise OllamaError("Ollama %d: %s" % (resp.status_code, resp.text))
        try:
            return resp.json()
        except ValueError:
            raise OllamaError("Ollama returned non-JSON response")

    def ping(self):
        self._get("/api/tags")
        return True

    def list_models(self):
        data = self._get("/api/tags")
        return [m.get("name", "") for m in (data.get("models") or [])]

    def embed(self, text):
        payload = {"model": self.embed_model, "prompt": text}
        data = self._post("/api/embeddings", payload)
        vec = data.get("embedding")
        if not vec:
            raise OllamaError("Ollama embeddings response missing 'embedding'")
        return [float(x) for x in vec]

    def chat(self, messages, num_ctx=8192):
        payload = {
            "model": self.chat_model,
            "messages": messages,
            "stream": False,
            "options": {"num_ctx": int(num_ctx)},
        }
        data = self._post("/api/chat", payload)
        msg = data.get("message") or {}
        content = msg.get("content") or ""
        return content.strip()


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def extract_text(path, fmt):
    """Return extracted plain text for a supported format, or None."""
    if not path or not os.path.exists(path):
        return None
    fmt = (fmt or "").lower()
    try:
        if fmt == "epub":
            return _extract_epub(path)
        if fmt == "pdf":
            return _extract_pdf(path)
        if fmt == "txt":
            return _extract_txt(path)
    except Exception as e:
        log.warning("AI librarian extraction failed for %s (%s): %s", path, fmt, e)
        return None
    return None


_NS = {
    "container": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
}


def _extract_epub(path):
    from lxml import etree, html as lxml_html

    out_parts = []
    with zipfile.ZipFile(path) as zf:
        try:
            container = etree.fromstring(zf.read("META-INF/container.xml"))
        except KeyError:
            return None
        rootfile_el = container.find(".//container:rootfile", _NS)
        if rootfile_el is None:
            return None
        opf_path = rootfile_el.get("full-path")
        if not opf_path:
            return None
        opf_dir = os.path.dirname(opf_path)
        try:
            opf = etree.fromstring(zf.read(opf_path))
        except KeyError:
            return None

        manifest = {}
        for item in opf.findall(".//opf:manifest/opf:item", _NS):
            manifest[item.get("id")] = item.get("href")

        spine_ids = [itemref.get("idref")
                     for itemref in opf.findall(".//opf:spine/opf:itemref", _NS)]

        for idref in spine_ids:
            href = manifest.get(idref)
            if not href:
                continue
            item_path = os.path.normpath(os.path.join(opf_dir, href)) if opf_dir else href
            item_path = item_path.replace(os.sep, "/")
            try:
                raw = zf.read(item_path)
            except KeyError:
                continue
            try:
                doc = lxml_html.fromstring(raw)
            except Exception:
                continue
            text = doc.text_content()
            if text:
                out_parts.append(text)
    joined = "\n\n".join(out_parts)
    return _collapse_whitespace(joined)


def _extract_pdf(path):
    try:
        from pypdf import PdfReader
    except ImportError:
        from PyPDF2 import PdfReader  # type: ignore
    reader = PdfReader(path)
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception as e:
            log.debug("pypdf page extract failed: %s", e)
            continue
    joined = "\n\n".join(p for p in pages if p)
    return _collapse_whitespace(joined)


def _extract_txt(path):
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return _collapse_whitespace(raw.decode(enc))
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


_WS_RE = re.compile(r"[ \t\r\f\v]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")


def _collapse_whitespace(text):
    if not text:
        return ""
    text = text.replace("\u00a0", " ")
    text = _WS_RE.sub(" ", text)
    text = _MULTI_NL_RE.sub("\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def chunk_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP, hard_cap=CHUNK_HARD_CAP):
    if not text:
        return []
    text = text.strip()
    if len(text) <= size:
        return [text]

    chunks = []
    n = len(text)
    start = 0
    step = max(size - overlap, 1)
    while start < n and len(chunks) < hard_cap:
        end = min(start + size, n)
        if end < n:
            # prefer a paragraph or sentence boundary near the end
            window = text[max(end - 200, start):end]
            para_idx = window.rfind("\n\n")
            if para_idx != -1:
                end = max(end - 200, start) + para_idx
            else:
                sent_idx = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
                if sent_idx != -1:
                    end = max(end - 200, start) + sent_idx + 1
        piece = text[start:end].strip()
        if len(piece) >= 50:
            chunks.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + step)
    return chunks


# ---------------------------------------------------------------------------
# Embedding serialization + similarity
# ---------------------------------------------------------------------------

def pack_embedding(vec):
    return struct.pack("<%df" % len(vec), *vec)


def unpack_embedding(blob):
    n = len(blob) // 4
    return list(struct.unpack("<%df" % n, blob))


def cosine(a, b):
    if not a or not b:
        return 0.0
    # iterate once; avoid calling len twice
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def rank_rows(q_vec, rows, top_k):
    """rows: iterable of (id, vec, payload). Returns top_k by cosine desc."""
    scored = []
    for row_id, vec, payload in rows:
        s = cosine(q_vec, vec)
        scored.append((s, row_id, payload))
    scored.sort(key=lambda t: t[0], reverse=True)
    return scored[:top_k]


# ---------------------------------------------------------------------------
# Library-wide metadata doc
# ---------------------------------------------------------------------------

def build_library_doc(title, authors, series, series_index, tags, publisher, pubdate, language, comment):
    """Build the short synthetic document we embed for library-wide RAG.

    All args are plain strings/None except tags and authors which may be lists.
    """
    parts = []
    if title:
        parts.append("Title: %s" % title)
    if authors:
        if isinstance(authors, (list, tuple)):
            parts.append("Author: %s" % ", ".join(authors))
        else:
            parts.append("Author: %s" % authors)
    if series:
        if series_index:
            parts.append("Series: %s of %s" % (series_index, series))
        else:
            parts.append("Series: %s" % series)
    if tags:
        if isinstance(tags, (list, tuple)):
            parts.append("Tags: %s" % ", ".join(tags))
        else:
            parts.append("Tags: %s" % tags)
    if publisher:
        parts.append("Publisher: %s" % publisher)
    if pubdate:
        parts.append("Published: %s" % pubdate)
    if language:
        parts.append("Language: %s" % language)
    if comment:
        # plain-text preview of the description; strip html
        parts.append("Description: %s" % _strip_html(comment))
    return ". ".join(parts)


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text):
    if not text:
        return ""
    return _collapse_whitespace(_TAG_RE.sub(" ", text))


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

BOOK_SYSTEM_PROMPT = (
    "You are an AI librarian. Answer the user's question about the book "
    "'{title}' by {author} using only the excerpts provided below. "
    "If the excerpts do not contain the answer, say so honestly. "
    "Keep answers concise and cite relevant details from the excerpts."
)

LIBRARY_SYSTEM_PROMPT = (
    "You are an AI librarian helping a reader browse their personal library. "
    "The user will ask a question about what is in the library. Each candidate "
    "book is given to you as a metadata summary, optionally followed by one or "
    "more excerpts from the book's full text. Use only the material provided "
    "below; do not invent titles. Recommend specific books from the list when "
    "relevant, explain why briefly, and quote or reference excerpt content when "
    "it helps answer the question."
)


def build_book_messages(title, author, retrieved_chunks, question):
    """retrieved_chunks: list of (ordinal, text)."""
    budget = TOTAL_CONTEXT_CHAR_BUDGET
    parts = []
    for ordinal, text in retrieved_chunks:
        snippet = text.strip()
        if len(snippet) > budget:
            snippet = snippet[:budget]
        parts.append("[excerpt %d]\n%s" % (ordinal, snippet))
        budget -= len(snippet)
        if budget <= 0:
            break
    context = "\n\n".join(parts) if parts else "(no excerpts available)"
    system = BOOK_SYSTEM_PROMPT.format(title=title or "Unknown", author=author or "Unknown")
    user = "Excerpts:\n\n%s\n\nQuestion: %s" % (context, question)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_library_messages(retrieved_books, question):
    """retrieved_books: list of dicts with title, author, doc_text, book_id,
    and optionally an 'excerpts' list of content snippets pulled from the
    book's full text."""
    budget = TOTAL_CONTEXT_CHAR_BUDGET
    parts = []
    for b in retrieved_books:
        block_lines = []
        header = "[book %s] %s" % (b.get("book_id"), (b.get("doc_text") or "").strip())
        if len(header) > budget:
            header = header[:budget]
        block_lines.append(header)
        budget -= len(header)
        for ex in (b.get("excerpts") or []):
            if budget <= 0:
                break
            snippet = (ex or "").strip()
            if not snippet:
                continue
            if len(snippet) > 1200:
                snippet = snippet[:1200]
            if len(snippet) > budget:
                snippet = snippet[:budget]
            block_lines.append("Excerpt from the book: %s" % snippet)
            budget -= len(snippet)
        parts.append("\n".join(block_lines))
        if budget <= 0:
            break
    context = "\n\n".join(parts) if parts else "(no books available yet)"
    user = "Candidate books from the library:\n\n%s\n\nQuestion: %s" % (context, question)
    return [
        {"role": "system", "content": LIBRARY_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# Format preference
# ---------------------------------------------------------------------------

def pick_format(available_formats):
    """Given a list/set of format strings (any case), return preferred one or None."""
    if not available_formats:
        return None
    lowered = {f.lower() for f in available_formats}
    for pref in SUPPORTED_FORMATS:
        if pref in lowered:
            return pref
    return None
