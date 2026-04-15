# -*- coding: utf-8 -*-

#  This file is part of the Calibre-Web (https://github.com/janeczku/calibre-web)
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.

from functools import wraps

from flask import Blueprint, abort, jsonify, request, url_for
from flask_babel import gettext as _

from . import calibre_db, config, db, logger, ub
from .cw_login import current_user
from .services import ai_librarian as ai_service
from .services.worker import WorkerThread
from .tasks.ai_index import TaskIndexBook, TaskBuildLibraryIndex
from .usermanagement import user_login_required

log = logger.create()

ai_librarian = Blueprint('ai_librarian', __name__)


def get_ai_librarian_activated():
    return bool(config.config_ai_librarian_enabled)


def _enabled_or_404():
    if not config.config_ai_librarian_enabled:
        abort(404)
    if not current_user.is_authenticated:
        abort(401)


def _admin_required(f):
    @wraps(f)
    def inner(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.role_admin():
            abort(403)
        return f(*args, **kwargs)
    return inner


def _json_question():
    data = request.get_json(silent=True) or {}
    q = (data.get("question") or "").strip()
    return q


def _make_client():
    return ai_service.OllamaClient(
        base_url=config.config_ai_ollama_url,
        chat_model=config.config_ai_chat_model,
        embed_model=config.config_ai_embed_model,
    )


# ---------------------------------------------------------------------------
# Per-book routes
# ---------------------------------------------------------------------------

@ai_librarian.route("/ai/ask/book/<int:book_id>", methods=["POST"])
@user_login_required
def ask_book(book_id):
    _enabled_or_404()
    question = _json_question()
    if not question:
        return jsonify({"error": _("Please enter a question.")}), 400

    book = calibre_db.get_book(book_id)
    if book is None:
        return jsonify({"error": _("Book not found.")}), 404

    index = ub.session.query(ub.AiBookIndex).filter(ub.AiBookIndex.book_id == book_id).first()
    current_model = config.config_ai_embed_model

    needs_index = (
        index is None
        or index.status in (ub.AiBookIndex.STATUS_PENDING, ub.AiBookIndex.STATUS_FAILED)
        or index.embed_model != current_model
    )

    if needs_index and (index is None or index.status != ub.AiBookIndex.STATUS_INDEXING):
        _enqueue_book_index(book_id)
        return jsonify({
            "status": "indexing",
            "message": _("Indexing this book — please try again in a moment."),
        }), 202

    if index is None or index.status == ub.AiBookIndex.STATUS_INDEXING:
        return jsonify({
            "status": "indexing",
            "message": _("Indexing this book — please try again in a moment."),
        }), 202

    if index.status != ub.AiBookIndex.STATUS_READY:
        return jsonify({
            "status": "failed",
            "error": index.error or _("Indexing failed."),
        }), 500

    try:
        client = _make_client()
        q_vec = client.embed(question)
    except ai_service.OllamaUnreachable as e:
        return jsonify({"error": _("AI server unreachable: %(e)s", e=str(e))}), 503
    except ai_service.OllamaModelMissing as e:
        return jsonify({"error": _("Model not available on Ollama: %(e)s", e=str(e))}), 503
    except ai_service.OllamaError as e:
        return jsonify({"error": _("AI error: %(e)s", e=str(e))}), 502

    chunks = ub.session.query(ub.AiBookChunk).filter(ub.AiBookChunk.index_id == index.id).all()
    if not chunks:
        return jsonify({"error": _("No indexed content for this book.")}), 500

    rows = [(c.id, ai_service.unpack_embedding(c.embedding), (c.ordinal, c.text)) for c in chunks]
    top = ai_service.rank_rows(q_vec, rows, int(config.config_ai_top_k or 5))
    retrieved = [payload for _score, _cid, payload in top]

    title = book.title or ""
    author = ", ".join(a.name.replace("|", ",") for a in (book.authors or []))

    messages = ai_service.build_book_messages(title, author, retrieved, question)
    try:
        answer = client.chat(messages, num_ctx=int(config.config_ai_num_ctx or 8192))
    except ai_service.OllamaUnreachable as e:
        return jsonify({"error": _("AI server unreachable: %(e)s", e=str(e))}), 503
    except ai_service.OllamaModelMissing as e:
        return jsonify({"error": _("Chat model not available: %(e)s", e=str(e))}), 503
    except ai_service.OllamaError as e:
        return jsonify({"error": _("AI error: %(e)s", e=str(e))}), 502

    sources = [{"ordinal": o, "snippet": t[:200]} for (o, t) in retrieved]
    return jsonify({"status": "ok", "answer": answer, "sources": sources})


@ai_librarian.route("/ai/status/book/<int:book_id>", methods=["GET"])
@user_login_required
def status_book(book_id):
    _enabled_or_404()
    index = ub.session.query(ub.AiBookIndex).filter(ub.AiBookIndex.book_id == book_id).first()
    if index is None:
        return jsonify({"status": "missing"})
    return jsonify({
        "status": index.status,
        "chunk_count": index.chunk_count or 0,
        "error": index.error,
        "embed_model": index.embed_model,
    })


@ai_librarian.route("/ai/reindex/book/<int:book_id>", methods=["POST"])
@user_login_required
def reindex_book(book_id):
    _enabled_or_404()
    if not current_user.role_admin() and not current_user.role_edit():
        abort(403)
    index = ub.session.query(ub.AiBookIndex).filter(ub.AiBookIndex.book_id == book_id).first()
    if index is not None:
        ub.session.query(ub.AiBookChunk).filter(
            ub.AiBookChunk.index_id == index.id).delete(synchronize_session=False)
        index.status = ub.AiBookIndex.STATUS_PENDING
        index.error = None
        ub.session.commit()
    _enqueue_book_index(book_id)
    return jsonify({"status": "queued"})


def _enqueue_book_index(book_id):
    index = ub.session.query(ub.AiBookIndex).filter(ub.AiBookIndex.book_id == book_id).first()
    if index is None:
        index = ub.AiBookIndex(book_id=book_id, status=ub.AiBookIndex.STATUS_PENDING)
        ub.session.add(index)
        ub.session.commit()
    elif index.status != ub.AiBookIndex.STATUS_INDEXING:
        index.status = ub.AiBookIndex.STATUS_PENDING
        index.error = None
        ub.session.commit()
    username = current_user.name if current_user.is_authenticated else None
    WorkerThread.add(username, TaskIndexBook(book_id), hidden=False)


# ---------------------------------------------------------------------------
# Library-wide routes
# ---------------------------------------------------------------------------

@ai_librarian.route("/ai/ask/library", methods=["POST"])
@user_login_required
def ask_library():
    _enabled_or_404()
    question = _json_question()
    if not question:
        return jsonify({"error": _("Please enter a question.")}), 400

    current_model = config.config_ai_embed_model
    rows = ub.session.query(ub.AiLibraryIndex).filter(
        ub.AiLibraryIndex.embed_model == current_model).all()

    total_books = calibre_db.session.query(db.Books).count()
    if total_books > len(rows):
        _enqueue_library_index()

    if not rows:
        return jsonify({
            "status": "indexing",
            "message": _("Building library index — please try again in a moment."),
            "indexed": 0,
            "total": total_books,
        }), 202

    try:
        client = _make_client()
        q_vec = client.embed(question)
    except ai_service.OllamaUnreachable as e:
        return jsonify({"error": _("AI server unreachable: %(e)s", e=str(e))}), 503
    except ai_service.OllamaModelMissing as e:
        return jsonify({"error": _("Model not available on Ollama: %(e)s", e=str(e))}), 503
    except ai_service.OllamaError as e:
        return jsonify({"error": _("AI error: %(e)s", e=str(e))}), 502

    top_k = int(config.config_ai_top_k or 5)

    ranked_meta = ai_service.rank_rows(
        q_vec,
        ((r.book_id, ai_service.unpack_embedding(r.embedding),
          {"book_id": r.book_id, "doc_text": r.doc_text}) for r in rows),
        top_k,
    )

    # Also rank across indexed book content so library-wide questions can
    # dig into the books themselves, not just the metadata blurbs.
    content_matches = _rank_library_content(q_vec, top_k * 2, current_model)

    meta_book_ids = [book_id for _s, book_id, _p in ranked_meta]
    content_book_ids = []
    seen = set(meta_book_ids)
    for _s, _cid, payload in content_matches:
        bid = payload.get("book_id")
        if bid is None or bid in seen:
            continue
        seen.add(bid)
        content_book_ids.append(bid)
        if len(content_book_ids) >= top_k:
            break

    ordered_book_ids = meta_book_ids + content_book_ids

    # Collect content excerpts per book (preserve rank order).
    excerpts_by_book = {}
    for _s, _cid, payload in content_matches:
        bid = payload.get("book_id")
        if bid is None:
            continue
        excerpts_by_book.setdefault(bid, []).append(payload.get("text") or "")

    # Build a quick lookup for the metadata doc text we already matched.
    meta_doc_by_book = {}
    for _s, book_id, payload in ranked_meta:
        meta_doc_by_book[book_id] = payload.get("doc_text") or ""
    # Plus anything else we need metadata for (content-only hits).
    for bid in content_book_ids:
        if bid not in meta_doc_by_book:
            row = ub.session.query(ub.AiLibraryIndex).filter(
                ub.AiLibraryIndex.book_id == bid,
                ub.AiLibraryIndex.embed_model == current_model,
            ).first()
            meta_doc_by_book[bid] = row.doc_text if row else ""

    enriched = []
    for book_id in ordered_book_ids:
        book = calibre_db.get_book(book_id)
        if book is None:
            continue
        enriched.append({
            "book_id": book_id,
            "title": book.title or "",
            "author": ", ".join(a.name.replace("|", ",") for a in (book.authors or [])),
            "doc_text": meta_doc_by_book.get(book_id, ""),
            "excerpts": excerpts_by_book.get(book_id, [])[:2],
            "cover_url": url_for("web.get_cover", book_id=book_id, resolution="og"),
            "detail_url": url_for("web.show_book", book_id=book_id),
        })

    if not enriched:
        return jsonify({"error": _("No matching books found.")}), 404

    messages = ai_service.build_library_messages(enriched, question)
    try:
        answer = client.chat(messages, num_ctx=int(config.config_ai_num_ctx or 8192))
    except ai_service.OllamaUnreachable as e:
        return jsonify({"error": _("AI server unreachable: %(e)s", e=str(e))}), 503
    except ai_service.OllamaModelMissing as e:
        return jsonify({"error": _("Chat model not available: %(e)s", e=str(e))}), 503
    except ai_service.OllamaError as e:
        return jsonify({"error": _("AI error: %(e)s", e=str(e))}), 502

    sources = [{
        "book_id": b["book_id"],
        "title": b["title"],
        "author": b["author"],
        "cover_url": b["cover_url"],
        "detail_url": b["detail_url"],
        "has_excerpts": bool(b.get("excerpts")),
    } for b in enriched]
    return jsonify({"status": "ok", "answer": answer, "sources": sources})


def _rank_library_content(q_vec, top_k, current_model):
    """Rank across all ready book-content chunks under the current embed model.

    Returns a list of (score, chunk_id, payload) tuples.
    """
    ready_indexes = ub.session.query(ub.AiBookIndex).filter(
        ub.AiBookIndex.status == ub.AiBookIndex.STATUS_READY,
        ub.AiBookIndex.embed_model == current_model,
    ).all()
    if not ready_indexes:
        return []
    index_to_book = {idx.id: idx.book_id for idx in ready_indexes}
    chunks = ub.session.query(ub.AiBookChunk).filter(
        ub.AiBookChunk.index_id.in_(list(index_to_book.keys()))
    ).all()
    if not chunks:
        return []

    def _rows():
        for c in chunks:
            try:
                vec = ai_service.unpack_embedding(c.embedding)
            except Exception:
                continue
            yield (c.id, vec, {
                "book_id": index_to_book.get(c.index_id),
                "ordinal": c.ordinal,
                "text": c.text,
            })

    return ai_service.rank_rows(q_vec, _rows(), top_k)


@ai_librarian.route("/ai/status/library", methods=["GET"])
@user_login_required
def status_library():
    _enabled_or_404()
    current_model = config.config_ai_embed_model
    indexed = ub.session.query(ub.AiLibraryIndex).filter(
        ub.AiLibraryIndex.embed_model == current_model).count()
    total = calibre_db.session.query(db.Books).count()
    return jsonify({"indexed": indexed, "total": total, "stale": max(total - indexed, 0)})


@ai_librarian.route("/ai/reindex/library", methods=["POST"])
@user_login_required
@_admin_required
def reindex_library():
    _enabled_or_404()
    ub.session.query(ub.AiLibraryIndex).delete(synchronize_session=False)
    ub.session.commit()
    _enqueue_library_index()
    return jsonify({"status": "queued"})


def _enqueue_library_index():
    username = current_user.name if current_user.is_authenticated else None
    WorkerThread.add(username, TaskBuildLibraryIndex(), hidden=False)


# ---------------------------------------------------------------------------
# Admin health check
# ---------------------------------------------------------------------------

@ai_librarian.route("/ai/health", methods=["GET"])
@user_login_required
@_admin_required
def health():
    try:
        client = _make_client()
        models = client.list_models()
    except ai_service.OllamaUnreachable as e:
        return jsonify({"ok": False, "error": str(e)}), 503
    except ai_service.OllamaError as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    chat_model = config.config_ai_chat_model
    embed_model = config.config_ai_embed_model
    # Ollama model names may include a :tag — match by prefix.
    def _has(m):
        return any(name == m or name.startswith(m + ":") for name in models)
    chat_ok = _has(chat_model)
    embed_ok = _has(embed_model)
    return jsonify({
        "ok": chat_ok and embed_ok,
        "url": config.config_ai_ollama_url,
        "models": models,
        "chat_model": chat_model,
        "chat_ok": chat_ok,
        "embed_model": embed_model,
        "embed_ok": embed_ok,
    })
