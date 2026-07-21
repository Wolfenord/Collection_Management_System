// Per-device column visibility for the items table. The server always renders
// every column; this hides the ones the user unticked. Choice is remembered in
// localStorage per collection. New columns default to visible (we only store
// the *hidden* set), so adding a field later doesn't silently hide it.
(function () {
    document.addEventListener('DOMContentLoaded', function () {
        const menu = document.getElementById('columnMenu');
        const table = document.getElementById('itemsTable');
        if (!menu || !table) return;

        const storeKey = 'cms:cols:' + menu.getAttribute('data-collection');
        const checks = Array.from(menu.querySelectorAll('[data-col-toggle]'));

        function loadHidden() {
            try { return new Set(JSON.parse(localStorage.getItem(storeKey) || '[]')); }
            catch (e) { return new Set(); }
        }
        function saveHidden(set) {
            try { localStorage.setItem(storeKey, JSON.stringify(Array.from(set))); }
            catch (e) { /* storage may be unavailable */ }
        }

        function applyColumn(key, visible) {
            table.querySelectorAll('[data-col="' + CSS.escape(key) + '"]').forEach(function (el) {
                el.classList.toggle('d-none', !visible);
            });
        }

        function apply() {
            const hidden = loadHidden();
            checks.forEach(function (chk) {
                const key = chk.getAttribute('data-col-toggle');
                const visible = !hidden.has(key);
                chk.checked = visible;
                applyColumn(key, visible);
            });
        }

        checks.forEach(function (chk) {
            chk.addEventListener('change', function () {
                const key = chk.getAttribute('data-col-toggle');
                const hidden = loadHidden();
                if (chk.checked) { hidden.delete(key); } else { hidden.add(key); }
                saveHidden(hidden);
                applyColumn(key, chk.checked);
            });
        });

        const reset = menu.querySelector('[data-col-reset]');
        if (reset) {
            reset.addEventListener('click', function () {
                saveHidden(new Set());
                apply();
            });
        }

        apply();
    });
})();
