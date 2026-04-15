/* Foliate-js reader integration for Calibre-Web */
/* global calibre, themes, $, screenfull */

const FOLIATE_BASE = calibre.foliateBase;

// Dynamic import of the foliate-js view module
const { View } = await import(FOLIATE_BASE + "view.js");

// --- State ---
let view;
let currentTheme = "lightTheme";
let currentFontSize = 100;
let currentFont = "default";
let currentLayout = "paginated";
let currentFraction = 0;
let isDraggingSlider = false;
let currentCfi = null;
let isBookmarked = false;
let bookmarkedCfi = null;

// --- DOM references ---
const container = document.getElementById("foliate-container");
const loader = document.getElementById("loader");
const progressEl = document.getElementById("progress");
const chapterDisplay = document.getElementById("chapter-display");
const chapterTitle = document.getElementById("chapter-title");
const bookTitle = document.getElementById("book-title");
const prevBtn = document.getElementById("prev");
const nextBtn = document.getElementById("next");
const progressSlider = document.getElementById("progress-slider");

// --- Helpers ---
function buildBookStyles() {
    const theme = themes[currentTheme] || themes.lightTheme;
    let css = "";
    css += `html, body { background: ${theme.bgColor} !important; color: ${theme.fgColor} !important; }`;
    css += `a { color: ${theme.linkColor} !important; }`;
    if (currentFont !== "default") {
        css += `html, body { font-family: ${currentFont} !important; }`;
    }
    if (currentFontSize !== 100) {
        css += `html { font-size: ${currentFontSize}% !important; }`;
    }
    return css;
}

function applyPageTheme() {
    const theme = themes[currentTheme] || themes.lightTheme;

    // Apply body theme class for CSS-driven UI theming
    document.body.className = document.body.className.replace(/\btheme-\S+/g, "").trim();
    if (currentTheme === "darkTheme") document.body.classList.add("theme-dark");
    else if (currentTheme === "sepiaTheme") document.body.classList.add("theme-sepia");
    else if (currentTheme === "blackTheme") document.body.classList.add("theme-black");
    // lightTheme = no theme class (default CSS)
}

function applyStyles() {
    if (view && view.renderer) {
        view.renderer.setStyles(buildBookStyles());
    }
    applyPageTheme();
}

function applyLayout() {
    const isScrolled = currentLayout === "scrolled";
    document.body.classList.toggle("layout-scrolled", isScrolled);

    // In scroll mode, the paginator's internal container handles scrolling.
    // Our outer container should not clip the scrollbar.
    if (container) {
        container.style.overflow = isScrolled ? "visible" : "hidden";
    }

    // Hide arrows in scroll mode
    if (prevBtn) prevBtn.style.display = isScrolled ? "none" : "";
    if (nextBtn) nextBtn.style.display = isScrolled ? "none" : "";

    // Renderer attributes
    if (view && view.renderer) {
        view.renderer.setAttribute("flow", currentLayout);
        view.renderer.setAttribute("max-column-count", isScrolled ? "1" : "2");
        view.renderer.setAttribute("max-inline-size", isScrolled ? "960px" : "720px");
    }
}

function savePosition(cfi) {
    if (!cfi) return;
    const key = "calibre.foliate.position." + calibre.bookUrl;
    try {
        localStorage.setItem(key, JSON.stringify({ cfi: cfi }));
    } catch (e) { /* ignore */ }
}

function loadPosition() {
    const key = "calibre.foliate.position." + calibre.bookUrl;
    try {
        const saved = localStorage.getItem(key);
        if (saved) {
            const obj = JSON.parse(saved);
            return obj.cfi || null;
        }
    } catch (e) { /* ignore */ }
    return null;
}

// --- TOC ---
function buildTOC(toc, parentEl) {
    if (!toc || !toc.length) return;
    const ul = document.createElement("ul");
    ul.className = "toc-list";
    for (const item of toc) {
        const li = document.createElement("li");
        const a = document.createElement("a");
        a.textContent = item.label || "Untitled";
        a.href = "#";
        a.addEventListener("click", (e) => {
            e.preventDefault();
            view.goTo(item.href);
            closeSidebar();
        });
        li.appendChild(a);
        if (item.subitems && item.subitems.length) {
            buildTOC(item.subitems, li);
        }
        ul.appendChild(li);
    }
    parentEl.appendChild(ul);
}

// --- Bookmarks ---
function saveBookmarkToServer(cfi) {
    if (!calibre.useBookmarks) return;
    const csrftoken = $("input[name='csrf_token']").val();
    $.ajax(calibre.bookmarkUrl, {
        method: "post",
        data: { bookmark: cfi || "" },
        headers: { "X-CSRFToken": csrftoken },
    }).fail(function (_xhr, _status, error) {
        console.error("Bookmark save failed:", error);
    });
}

function updateBookmarkIcon() {
    const bookmarkIcon = document.getElementById("bookmark-icon");
    if (bookmarkIcon) {
        bookmarkIcon.setAttribute("fill", isBookmarked ? "currentColor" : "none");
    }
}

function addBookmarkToSidebar(listEl, cfi, label) {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = "#";
    a.textContent = label || "Bookmark";
    a.addEventListener("click", (ev) => {
        ev.preventDefault();
        if (view) view.goTo(cfi);
        closeSidebar();
    });
    li.appendChild(a);
    listEl.appendChild(li);
}

// --- Sidebar ---
const sidebar = document.getElementById("sidebar");
const backdrop = document.getElementById("sidebar-backdrop");
let sidebarOpen = false;

function openSidebar() {
    sidebarOpen = true;
    sidebar.classList.add("open");
    backdrop.classList.add("visible");
}

function closeSidebar() {
    sidebarOpen = false;
    sidebar.classList.remove("open");
    backdrop.classList.remove("visible");
}

// --- Settings dropdown ---
const settingsDropdown = document.getElementById("settings-dropdown");
let settingsOpen = false;

function toggleSettings() {
    settingsOpen = !settingsOpen;
    settingsDropdown.classList.toggle("open", settingsOpen);
}

function closeSettings() {
    settingsOpen = false;
    settingsDropdown.classList.remove("open");
}

// Close settings when clicking outside
document.addEventListener("click", (e) => {
    if (settingsOpen && !settingsDropdown.contains(e.target) && e.target.id !== "setting" && !e.target.closest("#setting")) {
        closeSettings();
    }
});

// --- Initialize Reader ---
async function initReader() {
    // Create the foliate-view element
    view = document.createElement("foliate-view");
    view.style.width = "100%";
    view.style.height = "100%";
    container.appendChild(view);

    // Restore settings from localStorage
    try {
        currentTheme = localStorage.getItem("calibre.foliate.theme") || "lightTheme";
        const savedFontSize = localStorage.getItem("calibre.foliate.fontSize");
        if (savedFontSize) currentFontSize = parseInt(savedFontSize, 10);
        currentFont = localStorage.getItem("calibre.foliate.font") || "default";
        currentLayout = localStorage.getItem("calibre.foliate.layout") || "paginated";
    } catch (e) { /* ignore */ }

    // Restore UI state for settings panel
    restoreSettingsUI();

    // Open the book
    try {
        await view.open(calibre.bookUrl);
    } catch (e) {
        console.error("Failed to open book:", e);
        if (loader) loader.innerHTML = "<p style='color:red;text-align:center;padding:20px;'>Failed to load book.</p>";
        return;
    }

    // Attach 'load' listener BEFORE view.init() so the initial section's load
    // fires our wheel/keyboard bindings. Foliate uses closed shadow DOM, so
    // this is the only way to reach the book iframe's document.
    view.addEventListener("load", (e) => {
        applyStyles();
        const doc = e.detail && e.detail.doc;
        if (doc) {
            bindWheelToDoc(doc);
            doc.addEventListener("keydown", (ev) => {
                const t = ev.target;
                if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA")) return;
                const isScrolled = currentLayout === "scrolled";
                switch (ev.key) {
                case "ArrowLeft": case "h": ev.preventDefault(); view.goLeft(); break;
                case "ArrowRight": case "l": ev.preventDefault(); view.goRight(); break;
                case "ArrowUp": ev.preventDefault(); if (isScrolled) view.prev(200); else view.prev(); break;
                case "ArrowDown": ev.preventDefault(); if (isScrolled) view.next(200); else view.next(); break;
                case "PageUp": ev.preventDefault(); view.prev(); break;
                case "PageDown": ev.preventDefault(); view.next(); break;
                case " ": if (isScrolled) { ev.preventDefault(); if (ev.shiftKey) view.prev(); else view.next(); } break;
                }
            });
        }
    });

    // Wheel navigation in paginated mode
    let wheelCooldown = false;
    function handleWheel(ev) {
        if (currentLayout === "scrolled") return;
        const delta = Math.abs(ev.deltaY) > Math.abs(ev.deltaX) ? ev.deltaY : ev.deltaX;
        if (Math.abs(delta) < 5) return;
        ev.preventDefault();
        if (wheelCooldown) return;
        wheelCooldown = true;
        setTimeout(() => { wheelCooldown = false; }, 120);
        if (delta > 0) view.goRight();
        else view.goLeft();
    }
    const boundDocs = new WeakSet();
    function bindWheelToDoc(doc) {
        if (!doc || boundDocs.has(doc)) return;
        boundDocs.add(doc);
        doc.addEventListener("wheel", handleWheel, { passive: false, capture: true });
    }
    document.addEventListener("wheel", handleWheel, { passive: false, capture: true });

    // Set renderer attributes for layout
    if (view.renderer) {
        view.renderer.setAttribute("margin", "48px");
        view.renderer.setAttribute("gap", "5%");
    }
    applyLayout();

    // Set book title in titlebar
    if (bookTitle && view.book && view.book.metadata) {
        bookTitle.textContent = view.book.metadata.title || "";
    }

    // Build TOC
    const tocView = document.getElementById("tocView");
    if (tocView && view.book && view.book.toc) {
        buildTOC(view.book.toc, tocView);
    }

    // Default: Contents tab is active (per HTML), so hide the Bookmarks
    // panel initially — otherwise both panels stack in the sidebar.
    const bookmarksViewEl = document.getElementById("bookmarksView");
    if (bookmarksViewEl) bookmarksViewEl.style.display = "none";

    // Apply initial styles
    applyStyles();

    // Navigate to URL hash target, saved position, or server bookmark
    const hashTarget = (() => {
        const h = window.location.hash;
        if (!h || h.length <= 1) return null;
        return decodeURIComponent(h.slice(1));
    })();
    const savedCfi = loadPosition();
    const serverBookmark = calibre.bookmark || null;
    const initialTarget = hashTarget || savedCfi || serverBookmark;

    if (initialTarget) {
        try {
            await view.init({ lastLocation: initialTarget });
        } catch (e) {
            console.warn("Could not restore position:", e);
            await view.init({ showTextStart: true });
        }
    } else {
        await view.init({ showTextStart: true });
    }

    // Hide loader
    if (loader) loader.style.display = "none";

    // --- Events ---
    view.addEventListener("relocate", (e) => {
        const detail = e.detail;

        // Progress fraction
        const fraction = detail.fraction || 0;
        currentFraction = fraction;
        const pct = Math.round(fraction * 100);
        if (progressEl) progressEl.textContent = pct + "%";

        // Update slider (only if not being dragged)
        if (progressSlider && !isDraggingSlider) {
            progressSlider.value = Math.round(fraction * 1000);
        }

        // Chapter title in bottom bar and top bar
        if (detail.tocItem) {
            const label = detail.tocItem.label || "";
            if (chapterTitle) chapterTitle.textContent = label;
            if (chapterDisplay) chapterDisplay.textContent = label;
        }

        // Save position
        if (detail.cfi) {
            currentCfi = detail.cfi;
            savePosition(detail.cfi);

            // Reflect current position in URL hash so pages can be linked to
            try {
                const newHash = "#" + encodeURIComponent(detail.cfi);
                if (window.location.hash !== newHash) {
                    history.replaceState(null, "", newHash);
                }
            } catch (err) { /* ignore */ }

            // Highlight bookmark icon only when current position matches the saved bookmark
            const nowBookmarked = !!(bookmarkedCfi && detail.cfi === bookmarkedCfi);
            if (nowBookmarked !== isBookmarked) {
                isBookmarked = nowBookmarked;
                updateBookmarkIcon();
            }
        }
    });

    // --- Navigation ---
    if (prevBtn) {
        prevBtn.addEventListener("click", () => view.goLeft());
    }
    if (nextBtn) {
        nextBtn.addEventListener("click", () => view.goRight());
    }

    // Keyboard navigation
    document.addEventListener("keydown", (e) => {
        if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
        const isScrolled = currentLayout === "scrolled";
        switch (e.key) {
        case "ArrowLeft":
        case "h":
            e.preventDefault();
            view.goLeft();
            break;
        case "ArrowRight":
        case "l":
            e.preventDefault();
            view.goRight();
            break;
        case "ArrowUp":
            e.preventDefault();
            // In scroll mode, scroll by a smaller amount for smoother feel
            if (isScrolled) view.prev(200);
            else view.prev();
            break;
        case "ArrowDown":
            e.preventDefault();
            if (isScrolled) view.next(200);
            else view.next();
            break;
        case "PageUp":
            e.preventDefault();
            view.prev();
            break;
        case "PageDown":
            e.preventDefault();
            view.next();
            break;
        case " ":
            // Spacebar scrolls in scroll mode
            if (isScrolled) {
                e.preventDefault();
                if (e.shiftKey) view.prev();
                else view.next();
            }
            break;
        case "Escape":
            closeSettings();
            closeSidebar();
            break;
        }
    });

    // --- Progress slider interaction ---
    if (progressSlider) {
        progressSlider.addEventListener("input", () => {
            isDraggingSlider = true;
            const frac = parseInt(progressSlider.value, 10) / 1000;
            if (progressEl) progressEl.textContent = Math.round(frac * 100) + "%";
        });

        progressSlider.addEventListener("change", () => {
            const frac = parseInt(progressSlider.value, 10) / 1000;
            isDraggingSlider = false;
            if (view && view.goToFraction) {
                view.goToFraction(frac);
            }
        });
    }
}

// --- Restore settings UI state ---
function restoreSettingsUI() {
    // Theme swatch
    document.querySelectorAll(".theme-swatch").forEach(s => s.classList.remove("active"));
    const activeSwatch = document.querySelector(`.theme-swatch[data-theme="${currentTheme}"]`);
    if (activeSwatch) activeSwatch.classList.add("active");

    // Font size
    const fontSizeDisplay = document.getElementById("fontSizeDisplay");
    if (fontSizeDisplay) fontSizeDisplay.textContent = currentFontSize + "%";

    // Font
    document.querySelectorAll(".font-option").forEach(b => b.classList.remove("active"));
    const activeFont = document.querySelector(`.font-option[data-font="${currentFont}"]`);
    if (activeFont) activeFont.classList.add("active");

    // Layout
    document.querySelectorAll(".layout-option").forEach(b => b.classList.remove("active"));
    const activeLayout = document.querySelector(`.layout-option[data-layout="${currentLayout}"]`);
    if (activeLayout) activeLayout.classList.add("active");

    // Apply page theme immediately
    applyPageTheme();
}

// --- UI Chrome ---
(function initUI() {
    // Sidebar toggle (hamburger in toolbar)
    const slider = document.getElementById("slider");
    if (slider) {
        slider.addEventListener("click", (e) => {
            e.preventDefault();
            if (sidebarOpen) closeSidebar();
            else openSidebar();
        });
    }

    // Sidebar close button
    const sidebarClose = document.getElementById("sidebar-close");
    if (sidebarClose) {
        sidebarClose.addEventListener("click", (e) => {
            e.preventDefault();
            closeSidebar();
        });
    }

    // Backdrop click closes sidebar
    if (backdrop) {
        backdrop.addEventListener("click", closeSidebar);
    }

    // Sidebar view toggles (TOC / Bookmarks)
    document.querySelectorAll("#panels .show_view").forEach(btn => {
        btn.addEventListener("click", (e) => {
            e.preventDefault();
            const targetView = btn.dataset.view;

            // Activate the clicked tab
            document.querySelectorAll("#panels .show_view").forEach(b => b.classList.remove("active"));
            btn.classList.add("active");

            // Show the target view, hide others
            document.querySelectorAll("#sidebar .view").forEach(v => v.style.display = "none");
            const viewEl = document.getElementById(targetView.toLowerCase() + "View");
            if (viewEl) viewEl.style.display = "block";

            // Open sidebar if not already open
            if (!sidebarOpen) openSidebar();
        });
    });

    // Settings button
    const settingBtn = document.getElementById("setting");
    if (settingBtn) {
        settingBtn.addEventListener("click", (e) => {
            e.preventDefault();
            e.stopPropagation();
            toggleSettings();
        });
    }

    // Theme swatches
    document.querySelectorAll(".theme-swatch").forEach(btn => {
        btn.addEventListener("click", () => {
            const themeId = btn.dataset.theme;
            document.querySelectorAll(".theme-swatch").forEach(s => s.classList.remove("active"));
            btn.classList.add("active");

            currentTheme = themeId;
            localStorage.setItem("calibre.foliate.theme", themeId);
            applyStyles();
        });
    });

    // Font size controls
    const fontSizeDisplay = document.getElementById("fontSizeDisplay");
    const fontSizeDecrease = document.getElementById("fontSizeDecrease");
    const fontSizeIncrease = document.getElementById("fontSizeIncrease");

    function updateFontSizeDisplay() {
        if (fontSizeDisplay) fontSizeDisplay.textContent = currentFontSize + "%";
    }

    if (fontSizeDecrease) {
        fontSizeDecrease.addEventListener("click", () => {
            currentFontSize = Math.max(50, currentFontSize - 5);
            localStorage.setItem("calibre.foliate.fontSize", currentFontSize);
            updateFontSizeDisplay();
            applyStyles();
        });
    }

    if (fontSizeIncrease) {
        fontSizeIncrease.addEventListener("click", () => {
            currentFontSize = Math.min(300, currentFontSize + 5);
            localStorage.setItem("calibre.foliate.fontSize", currentFontSize);
            updateFontSizeDisplay();
            applyStyles();
        });
    }

    // Font family buttons
    document.querySelectorAll(".font-option").forEach(btn => {
        btn.addEventListener("click", () => {
            const fontId = btn.dataset.font;
            document.querySelectorAll(".font-option").forEach(b => b.classList.remove("active"));
            btn.classList.add("active");

            currentFont = fontId;
            localStorage.setItem("calibre.foliate.font", fontId);
            applyStyles();
        });
    });

    // Layout buttons
    document.querySelectorAll(".layout-option").forEach(btn => {
        btn.addEventListener("click", () => {
            const layoutId = btn.dataset.layout;
            document.querySelectorAll(".layout-option").forEach(b => b.classList.remove("active"));
            btn.classList.add("active");

            currentLayout = layoutId;
            localStorage.setItem("calibre.foliate.layout", layoutId);
            applyLayout();
        });
    });

    // Bookmark button
    const bookmarkBtn = document.getElementById("bookmark");
    const bookmarksList = document.getElementById("bookmarks");

    if (bookmarkBtn) {
        if (!calibre.useBookmarks) {
            bookmarkBtn.style.display = "none";
            const showBookmarks = document.getElementById("show-Bookmarks");
            if (showBookmarks) showBookmarks.style.display = "none";
        } else {
            // If server already has a bookmark, remember it (icon state
            // is set by the relocate handler based on current position)
            if (calibre.bookmark) {
                bookmarkedCfi = calibre.bookmark;
                // Add the existing bookmark to the sidebar list
                if (bookmarksList) {
                    addBookmarkToSidebar(bookmarksList, calibre.bookmark, "Saved position");
                }
            }

            bookmarkBtn.addEventListener("click", (e) => {
                e.preventDefault();
                if (!currentCfi && !(view && view.lastLocation)) return;
                const cfi = currentCfi || (view.lastLocation ? view.lastLocation.cfi : null);
                if (!cfi) return;

                if (isBookmarked) {
                    // Remove bookmark
                    isBookmarked = false;
                    bookmarkedCfi = null;
                    saveBookmarkToServer(""); // empty string deletes it
                    updateBookmarkIcon();
                    // Clear sidebar list
                    if (bookmarksList) bookmarksList.innerHTML = "";
                } else {
                    // Add bookmark
                    isBookmarked = true;
                    bookmarkedCfi = cfi;
                    saveBookmarkToServer(cfi);
                    updateBookmarkIcon();
                    // Add to sidebar list
                    if (bookmarksList) {
                        bookmarksList.innerHTML = "";
                        const label = (view && view.lastLocation && view.lastLocation.tocItem)
                            ? view.lastLocation.tocItem.label
                            : "Bookmark";
                        addBookmarkToSidebar(bookmarksList, cfi, label);
                    }
                }
            });
        }
    }

    // Fullscreen
    const fullscreenBtn = document.getElementById("fullscreen");
    if (fullscreenBtn && typeof screenfull !== "undefined" && screenfull.isEnabled) {
        fullscreenBtn.addEventListener("click", (e) => {
            e.preventDefault();
            screenfull.toggle();
        });
    }
})();

// --- Start ---
initReader().catch(e => console.error("Reader init failed:", e));
