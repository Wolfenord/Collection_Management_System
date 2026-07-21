// Inline editing of the items table: turn on "Schnell bearbeiten", then click an
// editable cell to change its value right there — saved via AJAX, no detail page.
// File/image and multi-select cells are not editable inline (they have no
// data-editable attribute); use the full form for those.
(function () {
    const gettext = window.gettext || function (s) { return s; };

    function csrf() {
        const m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
        return m ? decodeURIComponent(m[1]) : '';
    }

    document.addEventListener('DOMContentLoaded', function () {
        const toggle = document.getElementById('inlineToggle');
        const table = document.getElementById('itemsTable');
        if (!toggle || !table) return;

        let editing = false;
        let openCell = null;

        toggle.addEventListener('click', function () {
            editing = !editing;
            table.classList.toggle('inline-mode', editing);
            toggle.classList.toggle('active', editing);
            toggle.classList.toggle('btn-outline-secondary', !editing);
            toggle.classList.toggle('btn-primary', editing);
            if (!editing && openCell) closeEditor(openCell, false);
        });

        function buildEditor(td) {
            const type = td.getAttribute('data-type');
            const raw = td.getAttribute('data-raw') || '';
            let el;
            if (type === 'boolean') {
                el = document.createElement('select');
                el.className = 'form-select form-select-sm';
                [['', '—'], ['true', gettext('Ja')], ['false', gettext('Nein')]].forEach(function (o) {
                    const opt = document.createElement('option');
                    opt.value = o[0]; opt.textContent = o[1];
                    if ((raw === 'True' && o[0] === 'true') || (raw === 'False' && o[0] === 'false')) opt.selected = true;
                    el.appendChild(opt);
                });
            } else if (type === 'choice') {
                el = document.createElement('select');
                el.className = 'form-select form-select-sm';
                const blank = document.createElement('option');
                blank.value = ''; blank.textContent = '—';
                el.appendChild(blank);
                (td.getAttribute('data-choices') || '').split('||').forEach(function (c) {
                    if (!c) return;
                    const opt = document.createElement('option');
                    opt.value = c; opt.textContent = c;
                    if (c === raw) opt.selected = true;
                    el.appendChild(opt);
                });
            } else if (type === 'textarea') {
                el = document.createElement('textarea');
                el.className = 'form-control form-control-sm';
                el.rows = 2;
                el.value = raw;
            } else {
                el = document.createElement('input');
                el.className = 'form-control form-control-sm';
                el.type = ({ number: 'number', year: 'number', decimal: 'number', price: 'number',
                    date: 'date', time: 'time', datetime: 'datetime-local', email: 'email',
                    url: 'url' })[type] || 'text';
                if (type === 'decimal' || type === 'price') el.step = 'any';
                el.value = raw;
            }
            el.classList.add('cell-editor');
            return el;
        }

        function openEditor(td) {
            if (openCell) closeEditor(openCell, false);
            openCell = td;
            const display = td.querySelector('.cell-display');
            const editor = buildEditor(td);
            if (display) display.classList.add('d-none');
            td.appendChild(editor);
            editor.focus();
            if (editor.select) { try { editor.select(); } catch (e) { /* selects */ } }

            const isSelect = editor.tagName === 'SELECT';
            editor.addEventListener('keydown', function (e) {
                if (e.key === 'Enter' && editor.tagName !== 'TEXTAREA') { e.preventDefault(); save(td, editor); }
                if (e.key === 'Escape') { e.preventDefault(); closeEditor(td, false); }
            });
            if (isSelect) {
                editor.addEventListener('change', function () { save(td, editor); });
            } else {
                editor.addEventListener('blur', function () { save(td, editor); });
            }
        }

        function closeEditor(td, keepDisplayHidden) {
            const editor = td.querySelector('.cell-editor');
            if (editor) editor.remove();
            const display = td.querySelector('.cell-display');
            if (display && !keepDisplayHidden) display.classList.remove('d-none');
            if (openCell === td) openCell = null;
        }

        function save(td, editor) {
            const url = td.closest('tr').getAttribute('data-inline-url');
            const key = td.getAttribute('data-key');
            const value = editor.value;
            // No change → just close.
            if (value === (td.getAttribute('data-raw') || '') ||
                (value === '' && !td.getAttribute('data-raw'))) {
                closeEditor(td, false);
                return;
            }
            editor.disabled = true;
            const body = new URLSearchParams({ field_key: key, value: value });
            fetch(url, {
                method: 'POST',
                headers: { 'X-CSRFToken': csrf(), 'Content-Type': 'application/x-www-form-urlencoded',
                    'X-Requested-With': 'XMLHttpRequest' },
                body: body.toString(),
            })
                .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
                .then(function (res) {
                    if (!res.ok || !res.d.ok) {
                        editor.disabled = false;
                        editor.classList.add('is-invalid');
                        editor.title = res.d.error || gettext('Speichern fehlgeschlagen.');
                        return;
                    }
                    const display = td.querySelector('.cell-display');
                    if (display) {
                        if (res.d.empty) { display.innerHTML = '<span class="text-muted">–</span>'; }
                        else { display.textContent = res.d.display; }
                    }
                    td.setAttribute('data-raw', editor.tagName === 'SELECT' ? value : value);
                    closeEditor(td, false);
                    td.classList.add('table-success');
                    setTimeout(function () { td.classList.remove('table-success'); }, 1200);
                })
                .catch(function () {
                    editor.disabled = false;
                    editor.classList.add('is-invalid');
                    editor.title = gettext('Netzwerkfehler.');
                });
        }

        table.addEventListener('click', function (e) {
            if (!editing) return;
            const td = e.target.closest('td[data-editable]');
            if (!td || td === openCell) return;
            // Don't hijack clicks on links inside a cell.
            if (e.target.closest('a')) return;
            e.preventDefault();
            openEditor(td);
        });
    });
})();
