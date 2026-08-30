/*
 * Dashboard behaviour: toasts, theme, live-update indicator.
 *
 * The toast builder is a security fix, not a style choice
 * (docs/CODE-AUDIT.md A11). The previous version concatenated the message into
 * `el.innerHTML`, and messages carry YouTube-supplied video titles verbatim.
 * `<script>` inserted that way is inert per spec, but `<img src=x onerror=...>`
 * and `<svg onload=...>` are not, so any uploader could run script in this
 * origin the moment an operator pressed Download on their video.
 *
 * The fix is structural: the toast is assembled with createElement and the
 * message is written with textContent, which cannot parse markup at all. There
 * is no escaping to get right and no way for a future edit to reintroduce the
 * hole without deleting this comment.
 */
(function () {
    'use strict';

    // ---------------------------------------------------------------- toasts

    function showToast(detail) {
        var level = (detail && detail.level) || 'primary';
        var message = (detail && detail.message) || '';

        var toast = document.createElement('div');
        toast.className = 'toast align-items-center text-bg-' + levelClass(level) + ' border-0';
        toast.setAttribute('role', 'alert');
        toast.setAttribute('aria-live', 'assertive');
        toast.setAttribute('aria-atomic', 'true');

        var row = document.createElement('div');
        row.className = 'd-flex';

        var body = document.createElement('div');
        body.className = 'toast-body';
        // The whole point: markup in `message` becomes visible text, never nodes.
        body.textContent = message;

        var close = document.createElement('button');
        close.type = 'button';
        close.className = 'btn-close btn-close-white me-2 m-auto';
        close.setAttribute('data-bs-dismiss', 'toast');
        close.setAttribute('aria-label', 'Close');

        row.appendChild(body);
        row.appendChild(close);
        toast.appendChild(row);

        var container = document.getElementById('toast-container');
        if (!container) { return; }
        container.appendChild(toast);

        toast.addEventListener('hidden.bs.toast', function () { toast.remove(); });

        if (window.bootstrap && window.bootstrap.Toast) {
            var instance = new bootstrap.Toast(toast, { delay: 4500, autohide: true });
            instance.show();
            // Backstop: on touch devices Bootstrap's autohide timer can be paused
            // by sticky hover, leaving a toast on screen indefinitely.
            setTimeout(function () {
                try { instance.hide(); } catch (e) { toast.remove(); }
            }, 6000);
        } else {
            toast.classList.add('show');
            setTimeout(function () { toast.remove(); }, 5000);
        }
    }

    // Only these reach a CSS class name, so a hostile `level` cannot smuggle
    // one in alongside the message.
    var LEVELS = ['primary', 'secondary', 'success', 'danger', 'warning', 'info'];
    function levelClass(level) {
        return LEVELS.indexOf(level) === -1 ? 'primary' : level;
    }

    // htmx turns the {"notify": {...}} HX-Trigger header into this DOM event.
    document.body.addEventListener('notify', function (event) {
        showToast(event.detail);
    });

    document.body.addEventListener('closeSettings', function () {
        var modal = document.getElementById('settingsModal');
        if (modal && window.bootstrap) {
            var instance = bootstrap.Modal.getInstance(modal);
            if (instance) { instance.hide(); }
        }
    });

    // ----------------------------------------------------------------- theme

    var toggle = document.getElementById('theme-toggle');
    if (toggle) {
        toggle.addEventListener('click', function () {
            var root = document.documentElement;
            var next = root.getAttribute('data-bs-theme') === 'dark' ? 'light' : 'dark';
            root.setAttribute('data-bs-theme', next);
            try { localStorage.setItem('mm-theme', next); } catch (e) { /* private mode */ }
        });
    }

    // ------------------------------------------------------- live indicator

    function setLive(connected) {
        var dot = document.getElementById('sse-dot');
        if (dot) { dot.classList.toggle('connected', connected); }
        var label = document.getElementById('sse-label');
        if (label) { label.textContent = connected ? 'Live' : 'Reconnecting…'; }
    }

    // The stream hangs up on purpose every SSE_MAX_STREAM_SECONDS so its server
    // thread returns to the pool; EventSource reconnects on its own after the
    // `retry:` delay. Wait a moment before reporting a drop, so that scheduled
    // recycle does not flash "Reconnecting…" at an operator every few minutes.
    var dropTimer = null;
    document.body.addEventListener('htmx:sseOpen', function () {
        clearTimeout(dropTimer);
        setLive(true);
    });
    document.body.addEventListener('htmx:sseError', function () {
        clearTimeout(dropTimer);
        dropTimer = setTimeout(function () { setLive(false); }, 4000);
    });

    // ------------------------------------------------- pick one provider
    //
    // Tapping Identify runs the whole chain. Holding it — or right-clicking,
    // or pressing the keyboard menu key — opens a menu to run exactly one.
    //
    // Three openers rather than one because a long-press alone would be
    // unreachable with a mouse and invisible to a keyboard. Right-click and the
    // menu key both arrive as `contextmenu`, so that single handler covers
    // both; touch gets the timer below.
    //
    // The menu is delegated from document: #tracks-panel is replaced wholesale
    // on every SSE update, so a listener bound to a button would die with it.

    var LONG_PRESS_MS = 450;
    var menu = document.getElementById('provider-menu');
    var menuLabel = document.getElementById('provider-menu-label');
    var openFor = null;          // the button the menu currently belongs to
    var pressTimer = null;
    var pressButton = null;
    var suppressClick = false;   // a long-press must not also fire the tap

    function closeMenu() {
        if (!menu) { return; }
        menu.hidden = true;
        if (openFor) { openFor.setAttribute('aria-expanded', 'false'); }
        openFor = null;
    }

    function openMenu(button, x, y) {
        if (!menu || !button) { return; }
        openFor = button;
        button.setAttribute('aria-expanded', 'true');
        menuLabel.textContent = button.getAttribute('data-track-label') || 'this track';
        menu.hidden = false;

        // Placed after unhiding so the measured size is the real one, and
        // clamped so a row near the right or bottom edge still shows it whole.
        var box = menu.getBoundingClientRect();
        var left = Math.max(8, Math.min(x, window.innerWidth - box.width - 8));
        var top = y;
        if (top + box.height > window.innerHeight - 8) {
            top = Math.max(8, y - box.height);
        }
        menu.style.left = (left + window.scrollX) + 'px';
        menu.style.top = (top + window.scrollY) + 'px';

        var first = menu.querySelector('.provider-menu-item');
        if (first) { first.focus(); }
    }

    function identifyButton(target) {
        return target && target.closest ? target.closest('.js-identify') : null;
    }

    document.addEventListener('contextmenu', function (event) {
        var button = identifyButton(event.target);
        if (!button) { return; }
        event.preventDefault();
        openMenu(button, event.clientX, event.clientY);
    });

    document.addEventListener('pointerdown', function (event) {
        var button = identifyButton(event.target);
        if (!button) {
            if (menu && !menu.hidden && !event.target.closest('#provider-menu')) {
                closeMenu();
            }
            return;
        }
        if (event.pointerType === 'mouse') { return; }  // mouse uses right-click
        pressButton = button;
        clearTimeout(pressTimer);
        pressTimer = setTimeout(function () {
            pressTimer = null;
            suppressClick = true;
            var box = button.getBoundingClientRect();
            openMenu(button, box.left, box.bottom + 4);
        }, LONG_PRESS_MS);
    });

    function cancelPress() {
        clearTimeout(pressTimer);
        pressTimer = null;
        pressButton = null;
    }
    document.addEventListener('pointerup', cancelPress);
    document.addEventListener('pointercancel', cancelPress);
    document.addEventListener('pointermove', function (event) {
        // A scroll that began on the button is not a long-press.
        if (pressButton && event.pointerType !== 'mouse') { cancelPress(); }
    });
    window.addEventListener('scroll', closeMenu, { passive: true });
    window.addEventListener('resize', closeMenu);

    // The click that follows a long-press would otherwise run the whole chain
    // as well as opening the menu. Captured so it never reaches htmx.
    document.addEventListener('click', function (event) {
        if (suppressClick && identifyButton(event.target)) {
            event.preventDefault();
            event.stopPropagation();
            suppressClick = false;
        }
    }, true);

    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape' && menu && !menu.hidden) {
            var button = openFor;
            closeMenu();
            if (button) { button.focus(); }
        }
    });

    if (menu) {
        menu.addEventListener('click', function (event) {
            var item = event.target.closest('.provider-menu-item');
            if (!item || !openFor) { return; }
            var button = openFor;

            // "show close matches" is not a provider: it queues a search and
            // opens the chooser, which fills itself in on the next SSE update.
            if (item.hasAttribute('data-suggest')) {
                var trackId = button.getAttribute('data-track-id');
                closeMenu();
                window.htmx.ajax('POST', '/actions/track/' + trackId + '/suggest/',
                                 {source: document.body, swap: 'none'});
                openSuggestions(trackId);
                return;
            }

            var url = button.getAttribute('data-identify-url');
            var provider = item.getAttribute('data-provider') || '';
            closeMenu();
            // `source: document.body` is what carries the CSRF token: the
            // header lives in body's hx-headers, and an element outside the
            // htmx tree would post without it and get a 403.
            window.htmx.ajax('POST', url, {
                source: document.body,
                swap: 'none',
                values: provider ? { provider: provider } : {}
            });
            button.focus();
        });
    }

    // ------------------------------------------------------ close matches
    //
    // The panel refetches itself on `sse:update`, so the job filling in the
    // candidates is what makes it populate — no polling, and the same signal
    // the rest of the page already listens to.

    var suggestions = document.getElementById('suggestion-panel');

    function closeSuggestions() {
        if (!suggestions) { return; }
        suggestions.hidden = true;
        // replaceChildren, not an assignment to innerHTML: ToastMarkupTests
        // greps this whole file for that pattern, and the guard is the only
        // thing standing between a future edit and the stored-XSS hole (A11).
        suggestions.replaceChildren();
    }

    function openSuggestions(trackId) {
        if (!suggestions || !trackId) { return; }
        // Fetch once. The fragment that comes back carries its own refresh
        // trigger while the search is running and drops it once there are
        // results — setting hx-trigger from here is what stranded the spinner
        // when the attribute did not take.
        suggestions.hidden = false;
        window.htmx.ajax('GET', '/fragments/track/' + trackId + '/suggestions/',
                         {target: suggestions, swap: 'innerHTML'});
    }

    if (suggestions) {
        // Delegated: the contents are replaced on every update, so a listener
        // bound to a button inside would not survive the first refresh.
        suggestions.addEventListener('click', function (event) {
            if (event.target.closest('[data-close-suggestions]')) {
                // A Use button posts through htmx first; closing here only
                // hides a panel whose choice has already been sent.
                setTimeout(closeSuggestions, 0);
            }
        });
    }
    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape' && suggestions && !suggestions.hidden) {
            closeSuggestions();
        }
    });
})();
