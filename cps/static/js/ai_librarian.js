/* AI Librarian frontend.
 * Floating FAB + bottom-right chat panel. Mode (per-book vs library-wide) is
 * determined by whether the current page has `.single[data-book-id]`.
 * Uses jQuery and the site-wide CSRF auto-injection from main.js.
 */
(function() {
    "use strict";

    var $ = window.jQuery;
    if (!$) return;

    var endpoints = {
        askBook: function(id) { return "/ai/ask/book/" + id; },
        statusBook: function(id) { return "/ai/status/book/" + id; },
        askLibrary: "/ai/ask/library",
        statusLibrary: "/ai/status/library"
    };

    var state = {
        mode: "library",
        bookId: null,
        pending: null,
        pollTimer: null,
        pollStart: null
    };

    function detectMode() {
        var el = document.querySelector(".single[data-book-id]");
        if (el) {
            var id = parseInt(el.getAttribute("data-book-id"), 10);
            if (!isNaN(id)) {
                state.mode = "book";
                state.bookId = id;
                return;
            }
        }
        state.mode = "library";
        state.bookId = null;
    }

    function setPanelTitle() {
        var $title = $("#aiLibrarianModalLabel");
        var $fabLabel = $("#ai-librarian-fab .ai-fab-label");
        if (state.mode === "book") {
            $title.text("Ask about this book");
            $fabLabel.text("Ask about this book");
        } else {
            $title.text("Librarian");
            $fabLabel.text("Librarian");
        }
    }

    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, function(c) {
            return {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c];
        });
    }

    // Minimal, safe markdown → HTML renderer. Escapes HTML first, then applies
    // a small subset: code blocks, inline code, headings, bold/italic, lists,
    // blockquotes, links, paragraphs.
    function renderMarkdown(text) {
        var placeholders = [];
        var src = String(text);

        // Extract fenced code blocks first so their contents aren't touched.
        src = src.replace(/```([a-zA-Z0-9_+-]*)\n([\s\S]*?)```/g, function(_, lang, code) {
            var i = placeholders.length;
            placeholders.push("<pre class=\"ai-code\"><code>" + escapeHtml(code) + "</code></pre>");
            return "\u0000CODE" + i + "\u0000";
        });

        src = escapeHtml(src);

        // Inline code.
        src = src.replace(/`([^`\n]+)`/g, function(_, code) { return "<code>" + code + "</code>"; });

        // Headings (# to ######).
        src = src.replace(/^######\s+(.+)$/gm, "<h6>$1</h6>");
        src = src.replace(/^#####\s+(.+)$/gm, "<h6>$1</h6>");
        src = src.replace(/^####\s+(.+)$/gm, "<h6>$1</h6>");
        src = src.replace(/^###\s+(.+)$/gm, "<h5>$1</h5>");
        src = src.replace(/^##\s+(.+)$/gm, "<h4>$1</h4>");
        src = src.replace(/^#\s+(.+)$/gm, "<h3>$1</h3>");

        // Bold + italic (***text***).
        src = src.replace(/\*\*\*([^*\n]+)\*\*\*/g, "<strong><em>$1</em></strong>");
        // Bold (**text**).
        src = src.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
        // Italic (*text*). Runs after bold, so only single-asterisk pairs remain.
        src = src.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
        // Italic with underscores (_text_) — require word boundaries.
        src = src.replace(/(^|[\s(])_([^_\n]+)_(?=[\s.,!?)]|$)/g, "$1<em>$2</em>");

        // Links [label](url).
        src = src.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function(_, label, url) {
            return "<a href=\"" + url + "\" target=\"_blank\" rel=\"noopener\">" + label + "</a>";
        });

        // Split into blocks on blank lines and handle lists / paragraphs per block.
        var blocks = src.split(/\n{2,}/);
        var html = blocks.map(function(block) {
            if (/^\u0000CODE\d+\u0000$/.test(block.trim())) return block;
            if (/^<h[1-6]>/.test(block.trim())) return block;

            var lines = block.split("\n");
            var ulMatch = lines.every(function(l) { return l.trim() === "" || /^[\s]*[-*]\s+/.test(l); });
            var hasUl = lines.some(function(l) { return /^[\s]*[-*]\s+/.test(l); });
            if (ulMatch && hasUl) {
                return "<ul>" + lines.filter(function(l) { return l.trim(); }).map(function(l) {
                    return "<li>" + l.replace(/^[\s]*[-*]\s+/, "") + "</li>";
                }).join("") + "</ul>";
            }
            var olMatch = lines.every(function(l) { return l.trim() === "" || /^[\s]*\d+\.\s+/.test(l); });
            var hasOl = lines.some(function(l) { return /^[\s]*\d+\.\s+/.test(l); });
            if (olMatch && hasOl) {
                return "<ol>" + lines.filter(function(l) { return l.trim(); }).map(function(l) {
                    return "<li>" + l.replace(/^[\s]*\d+\.\s+/, "") + "</li>";
                }).join("") + "</ol>";
            }

            var blockquoteMatch = lines.every(function(l) { return l.trim() === "" || /^>\s?/.test(l); });
            if (blockquoteMatch && lines.some(function(l) { return /^>/.test(l); })) {
                return "<blockquote>" + lines.map(function(l) { return l.replace(/^>\s?/, ""); }).join("<br>") + "</blockquote>";
            }

            if (!block.trim()) return "";
            return "<p>" + block.replace(/\n/g, "<br>") + "</p>";
        }).join("");

        // Restore code blocks.
        html = html.replace(/\u0000CODE(\d+)\u0000/g, function(_, i) { return placeholders[parseInt(i, 10)]; });
        return html;
    }

    function appendMessage(role, text) {
        var $msgs = $("#ai-messages");
        var cls = role === "user" ? "ai-msg ai-msg-user" : "ai-msg ai-msg-assistant";
        var $m = $("<div>").addClass(cls);
        $m.append($("<div>").addClass("ai-msg-role").text(role === "user" ? "You" : "Librarian"));
        var $body = $("<div>").addClass("ai-msg-body");
        if (role === "assistant") {
            $body.html(renderMarkdown(text));
        } else {
            $body.text(text);
        }
        $m.append($body);
        $msgs.append($m);
        $msgs.scrollTop($msgs[0].scrollHeight);
    }

    function showTyping() {
        hideTyping();
        var $msgs = $("#ai-messages");
        var $t = $("<div>").addClass("ai-msg ai-msg-assistant ai-typing-msg").attr("id", "ai-typing-msg");
        $t.append($("<div>").addClass("ai-msg-role").text("Librarian"));
        var $dots = $("<div>").addClass("ai-typing");
        $dots.append($("<span>")).append($("<span>")).append($("<span>"));
        $t.append($dots);
        $msgs.append($t);
        $msgs.scrollTop($msgs[0].scrollHeight);
    }

    function hideTyping() {
        $("#ai-typing-msg").remove();
    }

    function showError(msg) {
        $("#ai-error-banner").text(msg).show();
    }

    function clearBanners() {
        $("#ai-error-banner").hide().text("");
    }

    function showIndexing(text) {
        $("#ai-indexing-text").text(text || "Indexing in progress...");
        $("#ai-indexing-banner").show();
    }

    function hideIndexing() {
        $("#ai-indexing-banner").hide();
    }

    function renderSources(sources) {
        var $wrap = $("#ai-sources").empty();
        if (!sources || !sources.length) {
            $wrap.hide();
            return;
        }
        var $h = $("<div>").addClass("ai-sources-title").text("Sources");
        $wrap.append($h);
        if (state.mode === "book") {
            sources.forEach(function(s) {
                var $row = $("<div>").addClass("ai-source-chunk");
                $row.append($("<span>").addClass("ai-source-ord").text("#" + s.ordinal));
                $row.append(" ");
                $row.append($("<span>").addClass("ai-source-text").text(s.snippet || ""));
                $wrap.append($row);
            });
        } else {
            sources.forEach(function(b) {
                var $card = $("<a>").addClass("ai-source-book").attr("href", b.detail_url);
                $card.append($("<img>").addClass("ai-source-cover").attr("src", b.cover_url));
                var $meta = $("<div>").addClass("ai-source-meta");
                $meta.append($("<div>").addClass("ai-source-title").text(b.title || ""));
                $meta.append($("<div>").addClass("ai-source-author").text(b.author || ""));
                $card.append($meta);
                $wrap.append($card);
            });
        }
        $wrap.show();
    }

    function setSending(sending) {
        $("#ai-send-btn").prop("disabled", sending);
        $("#ai-question-input").prop("disabled", sending);
    }

    function stopPolling() {
        if (state.pollTimer) {
            clearTimeout(state.pollTimer);
            state.pollTimer = null;
        }
        state.pollStart = null;
    }

    function schedulePoll() {
        var elapsed = Date.now() - (state.pollStart || Date.now());
        var interval = elapsed > 30000 ? 5000 : 2000;
        state.pollTimer = setTimeout(pollStatus, interval);
    }

    function pollStatus() {
        var url = state.mode === "book"
            ? endpoints.statusBook(state.bookId)
            : endpoints.statusLibrary;
        $.ajax({
            url: url,
            method: "GET",
            success: function(data) {
                if (state.mode === "book") {
                    if (data.status === "ready") {
                        hideIndexing();
                        stopPolling();
                        if (state.pending) {
                            var q = state.pending;
                            state.pending = null;
                            submitQuestion(q);
                        }
                        return;
                    }
                    if (data.status === "failed") {
                        hideIndexing();
                        hideTyping();
                        stopPolling();
                        showError(data.error || "Indexing failed.");
                        setSending(false);
                        return;
                    }
                    showIndexing("Indexing book... (" + (data.chunk_count || 0) + " chunks)");
                } else {
                    var total = data.total || 0;
                    var indexed = data.indexed || 0;
                    if (indexed >= total && total > 0) {
                        hideIndexing();
                        stopPolling();
                        if (state.pending) {
                            var q2 = state.pending;
                            state.pending = null;
                            submitQuestion(q2);
                        }
                        return;
                    }
                    showIndexing("Building library index... " + indexed + " / " + total);
                }
                schedulePoll();
            },
            error: function(xhr) {
                hideIndexing();
                hideTyping();
                stopPolling();
                setSending(false);
                showError(extractError(xhr) || "Status check failed.");
            }
        });
    }

    function extractError(xhr) {
        try {
            var r = JSON.parse(xhr.responseText);
            if (r && r.error) return r.error;
            if (r && r.message) return r.message;
        } catch (e) {}
        return null;
    }

    function submitQuestion(q) {
        clearBanners();
        setSending(true);
        showTyping();
        var url = state.mode === "book"
            ? endpoints.askBook(state.bookId)
            : endpoints.askLibrary;
        $.ajax({
            url: url,
            method: "POST",
            contentType: "application/json",
            data: JSON.stringify({ question: q }),
            success: function(data, textStatus, xhr) {
                if (xhr.status === 202 || data.status === "indexing") {
                    state.pending = q;
                    showIndexing(data.message || "Indexing in progress...");
                    state.pollStart = Date.now();
                    schedulePoll();
                    return;
                }
                hideIndexing();
                hideTyping();
                setSending(false);
                appendMessage("assistant", data.answer || "(no answer)");
                renderSources(data.sources || []);
                $("#ai-question-input").val("").focus();
            },
            error: function(xhr) {
                hideIndexing();
                hideTyping();
                setSending(false);
                showError(extractError(xhr) || "Request failed.");
            }
        });
    }

    function openPanel() {
        detectMode();
        setPanelTitle();
        clearBanners();
        hideIndexing();
        $("#aiLibrarianModal").addClass("is-open").attr("aria-hidden", "false");
        $(document.body).addClass("ai-panel-open");
        setTimeout(function() { $("#ai-question-input").trigger("focus"); }, 120);
    }

    function closePanel() {
        $("#aiLibrarianModal").removeClass("is-open").attr("aria-hidden", "true");
        $(document.body).removeClass("ai-panel-open");
        stopPolling();
        hideTyping();
        state.pending = null;
        setSending(false);
    }

    $(function() {
        $("#ai-librarian-fab").on("click", openPanel);
        $("#ai-panel-close").on("click", closePanel);

        $("#ai-question-form").on("submit", function(e) {
            e.preventDefault();
            var q = $("#ai-question-input").val().trim();
            if (!q) return;
            appendMessage("user", q);
            $("#ai-question-input").val("");
            $("#ai-sources").hide().empty();
            submitQuestion(q);
        });

        $(document).on("keydown", function(e) {
            if (e.key === "Escape" && $("#aiLibrarianModal").hasClass("is-open")) {
                closePanel();
            }
        });

        detectMode();
        setPanelTitle();
    });
})();
