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
})();
