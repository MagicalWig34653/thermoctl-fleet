// "Technik" tab copy affordance (UI-redesign stage 2) -- attaches a click
// handler to every `[data-copy]` button, copying its full, untouched
// value (the digest `fleet/ui_routes.py`'s `shortdigest` filter only
// shortened for *display*) to the clipboard. Loaded only on pages that
// actually render such a button (see apartment.html's own `{% block
// scripts %}`) -- same-origin, no third-party script, consistent with
// this application's CSP (`script-src 'self'`).
//
// Progressive enhancement only: every digest this button sits next to is
// already fully present in the DOM (the shortened text plus a `title`
// attribute carrying the full value), so a browser with JavaScript
// disabled, or a failed clipboard permission, loses only the one-click
// convenience, never the information itself.
(function () {
    "use strict";

    document.querySelectorAll("button.copy-button[data-copy]").forEach(function (button) {
        button.addEventListener("click", function () {
            var value = button.getAttribute("data-copy") || "";
            var original = button.textContent;
            function flash(text) {
                button.textContent = text;
                window.setTimeout(function () {
                    button.textContent = original;
                }, 1500);
            }
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(value).then(
                    function () { flash("Kopiert"); },
                    function () { flash("Fehlgeschlagen"); }
                );
            }
        });
    });
})();
