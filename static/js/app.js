// Small site-wide helpers.
document.addEventListener('DOMContentLoaded', function () {
    // Copy-to-clipboard buttons: <button data-copy="https://…">
    document.querySelectorAll('[data-copy]').forEach(function (btn) {
        btn.addEventListener('click', function () {
            const text = btn.getAttribute('data-copy');
            navigator.clipboard.writeText(text).then(function () {
                const original = btn.innerHTML;
                btn.innerHTML = '<i class="bi bi-check-lg"></i> Kopiert!';
                setTimeout(function () { btn.innerHTML = original; }, 1500);
            });
        });
    });

    // Enable Bootstrap tooltips (used for help "?" hints).
    if (window.bootstrap) {
        document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(function (el) {
            new window.bootstrap.Tooltip(el);
        });
    }

    // Guided tour (Intro.js). Driven by data-intro / data-step attributes.
    function startTour() {
        if (!window.introJs) return;
        window.introJs().setOptions({
            nextLabel: 'Weiter', prevLabel: 'Zurück', doneLabel: 'Fertig',
            skipLabel: '✕', tooltipClass: 'cms-tour',
        }).start();
    }

    const tourBtn = document.getElementById('startTour');
    if (tourBtn) {
        tourBtn.addEventListener('click', function (e) {
            e.preventDefault();
            startTour();
        });
    }

    // Auto-start the tour once per page type for first-time visitors.
    const auto = document.querySelector('[data-tour-auto]');
    if (auto && window.introJs && document.querySelector('[data-intro]')) {
        const key = 'cmsTourSeen_' + auto.getAttribute('data-tour-auto');
        try {
            if (!localStorage.getItem(key)) {
                localStorage.setItem(key, '1');
                startTour();
            }
        } catch (err) { /* localStorage unavailable: skip auto-start */ }
    }
});
