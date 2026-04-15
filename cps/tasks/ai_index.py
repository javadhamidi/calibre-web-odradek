# -*- coding: utf-8 -*-

#   This file is part of the Calibre-Web (https://github.com/janeczku/calibre-web)
#
#   This program is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.

import os
import threading

from flask_babel import lazy_gettext as N_

from cps import config, db, logger, ub, app
from cps.services import ai_librarian as ai_service
from cps.services.worker import CalibreTask, WorkerThread, STAT_CANCELLED, STAT_ENDED


_book_locks_guard = threading.Lock()
_book_locks = {}


def _get_book_lock(book_id):
    with _book_locks_guard:
        lock = _book_locks.get(book_id)
        if lock is None:
            lock = threading.Lock()
            _book_locks[book_id] = lock
        return lock


def _make_client():
    return ai_service.OllamaClient(
        base_url=config.config_ai_ollama_url,
        chat_model=config.config_ai_chat_model,
        embed_model=config.config_ai_embed_model,
    )


class TaskIndexBook(CalibreTask):
    def __init__(self, book_id, task_message=''):
        super(TaskIndexBook, self).__init__(task_message or ('Indexing book %d for AI Librarian' % book_id))
        self.log = logger.create()
        self.book_id = book_id
        self.app_db_session = ub.get_new_session_instance()

    def run(self, worker_thread):
        lock = _get_book_lock(self.book_id)
        if not lock.acquire(blocking=False):
            self.log.debug("AI index: lock held for book %s, skipping", self.book_id)
            self._handleSuccess()
            self.app_db_session.remove()
            return
        try:
            self._run_locked()
        finally:
            lock.release()
            self.app_db_session.remove()

    def _run_locked(self):
        if not config.config_ai_librarian_enabled:
            self._mark_failed("AI Librarian disabled")
            return

        index = self._get_or_create_index()
        if self.stat == STAT_CANCELLED or self.stat == STAT_ENDED:
            return

        self.message = 'Locating book file'
        file_path, fmt = self._resolve_book_file()
        if not file_path:
            self._mark_failed("No supported format (epub/pdf/txt) available for this book")
            return

        self.message = 'Extracting text'
        text = ai_service.extract_text(file_path, fmt)
        if not text or len(text) < 500:
            self._mark_failed("No extractable text (possibly a scanned PDF, DRM, or empty file)")
            return

        chunks = ai_service.chunk_text(text)
        if not chunks:
            self._mark_failed("No usable chunks after splitting text")
            return

        # Clear any stale chunks from a previous run
        self.app_db_session.query(ub.AiBookChunk).filter(
            ub.AiBookChunk.index_id == index.id).delete(synchronize_session=False)
        index.status = ub.AiBookIndex.STATUS_INDEXING
        index.format = fmt
        index.embed_model = config.config_ai_embed_model
        index.embed_dim = 0
        index.chunk_count = 0
        index.error = None
        self.app_db_session.commit()

        try:
            client = _make_client()
        except Exception as e:
            self._mark_failed("Ollama client init failed: %s" % e)
            return

        total = len(chunks)
        embed_dim = None
        for i, chunk in enumerate(chunks):
            if self.stat == STAT_CANCELLED or self.stat == STAT_ENDED:
                self._mark_failed("Cancelled")
                return
            if not config.config_ai_librarian_enabled:
                self._mark_failed("AI Librarian disabled mid-task")
                return
            try:
                vec = client.embed(chunk)
            except ai_service.OllamaError as e:
                self._mark_failed(str(e))
                return
            if embed_dim is None:
                embed_dim = len(vec)
            row = ub.AiBookChunk(
                index_id=index.id,
                ordinal=i,
                text=chunk,
                embedding=ai_service.pack_embedding(vec),
            )
            self.app_db_session.add(row)
            self.progress = (i + 1) / float(total)
            self.message = 'Embedding chunk %d / %d' % (i + 1, total)

        index.status = ub.AiBookIndex.STATUS_READY
        index.embed_dim = embed_dim or 0
        index.chunk_count = total
        index.error = None
        self.app_db_session.commit()
        self._handleSuccess()

    def _get_or_create_index(self):
        index = self.app_db_session.query(ub.AiBookIndex).filter(
            ub.AiBookIndex.book_id == self.book_id).first()
        if index is None:
            index = ub.AiBookIndex(
                book_id=self.book_id,
                status=ub.AiBookIndex.STATUS_INDEXING,
            )
            self.app_db_session.add(index)
            self.app_db_session.commit()
        else:
            index.status = ub.AiBookIndex.STATUS_INDEXING
            self.app_db_session.commit()
        return index

    def _resolve_book_file(self):
        with app.app_context():
            calibre_db = db.CalibreDB(app)
            book = calibre_db.session.query(db.Books).filter(db.Books.id == self.book_id).first()
            if not book:
                return None, None
            available = [d.format.lower() for d in (book.data or [])]
            fmt = ai_service.pick_format(available)
            if not fmt:
                return None, None
            data = None
            for d in book.data:
                if d.format.lower() == fmt:
                    data = d
                    break
            if data is None:
                return None, None
            file_path = os.path.join(config.config_calibre_dir, book.path, data.name + "." + fmt)
        return file_path, fmt

    def _mark_failed(self, reason):
        try:
            index = self.app_db_session.query(ub.AiBookIndex).filter(
                ub.AiBookIndex.book_id == self.book_id).first()
            if index is not None:
                index.status = ub.AiBookIndex.STATUS_FAILED
                index.error = (reason or "")[:500]
                self.app_db_session.commit()
        except Exception as e:
            self.log.warning("AI index: failed to persist failure state: %s", e)
        self._handleError(reason)

    @property
    def name(self):
        return str(N_('AI Book Index'))

    @property
    def is_cancellable(self):
        return True


class TaskBuildLibraryIndex(CalibreTask):
    def __init__(self, task_message=''):
        super(TaskBuildLibraryIndex, self).__init__(
            task_message or 'Building AI Librarian library index')
        self.log = logger.create()
        self.app_db_session = ub.get_new_session_instance()

    def run(self, worker_thread):
        try:
            self._run()
        finally:
            self.app_db_session.remove()

    def _run(self):
        if not config.config_ai_librarian_enabled:
            self._handleError("AI Librarian disabled")
            return

        current_model = config.config_ai_embed_model

        self.message = 'Scanning library'
        with app.app_context():
            calibre_db = db.CalibreDB(app)
            books = calibre_db.session.query(db.Books).all()
            # Snapshot all the metadata we need while the session is live.
            pending = []
            for book in books:
                existing = self.app_db_session.query(ub.AiLibraryIndex).filter(
                    ub.AiLibraryIndex.book_id == book.id).first()
                if existing and existing.embed_model == current_model:
                    continue
                pending.append({
                    "book_id": book.id,
                    "title": book.title or "",
                    "authors": [a.name.replace("|", ",") for a in (book.authors or [])],
                    "series": book.series[0].name if book.series else "",
                    "series_index": book.series_index or "",
                    "tags": [t.name for t in (book.tags or [])],
                    "publisher": book.publishers[0].name if book.publishers else "",
                    "pubdate": str(book.pubdate) if getattr(book, "pubdate", None) else "",
                    "language": book.languages[0].lang_code if book.languages else "",
                    "comment": book.comments[0].text if book.comments else "",
                })

        total = len(pending)
        if total == 0:
            self.message = 'Library index up to date'
            self._handleSuccess()
            return

        try:
            client = _make_client()
        except Exception as e:
            self._handleError("Ollama client init failed: %s" % e)
            return

        for i, item in enumerate(pending):
            if self.stat == STAT_CANCELLED or self.stat == STAT_ENDED:
                break
            if not config.config_ai_librarian_enabled:
                break
            doc = ai_service.build_library_doc(
                item["title"], item["authors"], item["series"], item["series_index"],
                item["tags"], item["publisher"], item["pubdate"], item["language"], item["comment"],
            )
            if not doc:
                continue
            try:
                vec = client.embed(doc)
            except ai_service.OllamaError as e:
                self._handleError(str(e))
                return

            row = self.app_db_session.query(ub.AiLibraryIndex).filter(
                ub.AiLibraryIndex.book_id == item["book_id"]).first()
            if row is None:
                row = ub.AiLibraryIndex(book_id=item["book_id"])
                self.app_db_session.add(row)
            row.embed_model = current_model
            row.embed_dim = len(vec)
            row.doc_text = doc
            row.embedding = ai_service.pack_embedding(vec)
            self.app_db_session.commit()

            self.progress = (i + 1) / float(total)
            self.message = 'Indexed %d / %d books' % (i + 1, total)

        self._enqueue_content_indexing()
        self._handleSuccess()

    def _enqueue_content_indexing(self):
        """Queue per-book content indexing for any book whose content is not
        already ready under the current embed model. Runs after metadata is
        done so library-wide queries can grow richer over time."""
        if not config.config_ai_librarian_enabled:
            return
        current_model = config.config_ai_embed_model
        with app.app_context():
            calibre_db = db.CalibreDB(app)
            book_ids = [b.id for b in calibre_db.session.query(db.Books.id).all()]
        for book_id in book_ids:
            if self.stat == STAT_CANCELLED or self.stat == STAT_ENDED:
                return
            existing = self.app_db_session.query(ub.AiBookIndex).filter(
                ub.AiBookIndex.book_id == book_id).first()
            if existing and existing.status == ub.AiBookIndex.STATUS_READY \
                    and existing.embed_model == current_model:
                continue
            if existing and existing.status == ub.AiBookIndex.STATUS_INDEXING:
                continue
            try:
                WorkerThread.add(None, TaskIndexBook(book_id), hidden=False)
            except Exception as e:
                self.log.warning("AI librarian: failed to enqueue content index for book %s: %s",
                                 book_id, e)

    @property
    def name(self):
        return str(N_('AI Library Index'))

    @property
    def is_cancellable(self):
        return True
