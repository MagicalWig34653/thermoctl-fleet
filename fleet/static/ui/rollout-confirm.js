/* Confirm rollout actions while keeping the original forms usable without JS. */
(() => {
    const dialog = document.getElementById('rollout-confirm-dialog');
    if (!dialog || typeof dialog.showModal !== 'function') return;

    let pendingForm = null;
    let confirmedForm = null;
    document.querySelectorAll('form[data-rollout-confirm]').forEach((form) => {
        form.addEventListener('submit', (event) => {
            if (confirmedForm === form) {
                confirmedForm = null;
                return;
            }
            event.preventDefault();
            pendingForm = form;
            const action = form.dataset.rolloutConfirm;
            document.getElementById('rollout-dialog-title').textContent =
                action === 'Abbrechen' ? 'Rollout abbrechen?' : 'Rollout fortsetzen?';
            document.getElementById('rollout-dialog-message').textContent =
                action === 'Abbrechen'
                    ? 'Der Rollout wird dauerhaft abgebrochen. Bereits aktualisierte Wohnungen behalten ihren Stand.'
                    : 'Der Rollout setzt die Verteilung an den wartenden Wohnungen fort.';
            dialog.showModal();
        });
    });
    dialog.querySelector('[data-dialog-close]').addEventListener('click', () => dialog.close());
    dialog.querySelector('[data-dialog-submit]').addEventListener('click', () => {
        const form = pendingForm;
        dialog.close();
        pendingForm = null;
        if (!form) return;
        confirmedForm = form;
        form.requestSubmit();
    });
    dialog.addEventListener('close', () => { pendingForm = null; });
})();
