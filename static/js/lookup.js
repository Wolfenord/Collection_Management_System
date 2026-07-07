// Auto-fill item fields from the external databases (DNB, Google Books, Open
// Library — always all of them). Driven by #autofill (data-lookup-url,
// data-search-url, data-query-key, data-suggest-fields, data-provider)
// rendered by item_form.html whenever at least one field is mapped to a
// lookup attribute.
//
// Three entry points, all filling the same mapped fields:
//   1. Code lookup: scan/enter into the query field (e.g. ISBN)
//      -> GET <lookup-url>?q=<value> -> fill matching inputs directly.
//   2. Field suggestions: type into any mapped text field (title, author, …)
//      and hit its search button -> GET <search-url>?q=<value> -> pick one of
//      the candidate records.
//   3. Generic search box (title, name, keywords) -> same candidate list.
// Everything is keyed by the collection's own field keys, so it stays dynamic.
(function () {
    // Translations come from Django's JavaScriptCatalog (loaded in base.html);
    // identity fallbacks keep the page working if the catalogue is missing.
    const gettext = window.gettext || function (s) { return s; };
    const interpolate = window.interpolate || function (fmt, obj) {
        return fmt.replace(/%\((\w+)\)s/g, function (m, k) { return String(obj[k]); });
    };

    document.addEventListener('DOMContentLoaded', function () {
        const cfg = document.getElementById('autofill');
        if (!cfg) return;

        const url = cfg.getAttribute('data-lookup-url');
        const searchUrl = cfg.getAttribute('data-search-url');
        const queryKey = cfg.getAttribute('data-query-key');
        const provider = cfg.getAttribute('data-provider') || gettext('den externen Datenbanken');
        const exclude = cfg.getAttribute('data-exclude') || '';
        let suggestFields = {};
        try { suggestFields = JSON.parse(cfg.getAttribute('data-suggest-fields') || '{}'); } catch (e) { /* ignore */ }
        const form = cfg.closest('form') || document;
        const queryInput = queryKey ? form.querySelector('[name="' + queryKey + '"]') : null;

        // Status line: under the query field when there is one, else under the
        // generic search box (attached later).
        const status = document.createElement('div');
        status.className = 'form-text mt-1';

        function setStatus(html, cls) {
            status.className = 'form-text mt-1 ' + (cls || '');
            status.innerHTML = html;
        }

        let lastQuery = null;

        function fillField(key, value) {
            const el = form.querySelector('[name="' + key + '"]');
            if (!el) return false;
            if (el.type === 'file') return false; // can't set file inputs from JS
            if (el.tagName === 'SELECT') {
                const opt = Array.from(el.options).find(function (o) { return o.value === String(value); });
                if (opt) { el.value = opt.value; } else { return false; }
            } else if (el.type === 'checkbox') {
                el.checked = Boolean(value);
            } else {
                el.value = value;
            }
            el.dispatchEvent(new Event('change', { bubbles: true }));
            // Brief visual confirmation.
            el.classList.add('border-success');
            setTimeout(function () { el.classList.remove('border-success'); }, 1500);
            return true;
        }

        function showCover(key, coverUrl) {
            const el = form.querySelector('[name="' + key + '"]');
            if (!el) return;
            const host = el.closest('.mb-3') || el.parentNode;
            let prev = host.querySelector('.autofill-cover');
            if (!prev) {
                prev = document.createElement('div');
                prev.className = 'autofill-cover mt-2 small';
                host.appendChild(prev);
            }
            // Hidden field -> the server downloads the cover on save (unless the
            // user uploads/keeps an own file, which always wins).
            let hidden = form.querySelector('input[name="' + key + '__cover_url"]');
            if (!hidden) {
                hidden = document.createElement('input');
                hidden.type = 'hidden';
                hidden.name = key + '__cover_url';
                host.appendChild(hidden);
            }
            hidden.value = coverUrl;
            prev.innerHTML =
                '<img src="' + coverUrl + '" alt="Cover" style="height:80px" class="rounded border me-2">' +
                '<span class="text-success"><i class="bi bi-check-circle"></i> ' + gettext('Bild wird beim Speichern übernommen.') + '</span> ' +
                '<a href="#" class="autofill-cover-skip">' + gettext('Nicht übernehmen') + '</a>';
            prev.querySelector('.autofill-cover-skip').addEventListener('click', function (e) {
                e.preventDefault();
                hidden.value = '';
                prev.innerHTML = '<span class="text-muted">' + gettext('Bild wird nicht übernommen.') + '</span> ' +
                    '<a href="' + coverUrl + '" target="_blank" rel="noopener">' + gettext('Bild öffnen') + '</a>';
            });
        }

        function showDuplicate(duplicate) {
            if (!duplicate) return;
            status.innerHTML += ' <span class="text-warning"><i class="bi bi-exclamation-triangle"></i> ' +
                interpolate(gettext('Achtung: „%(name)s“ mit diesem Code existiert bereits – <a href="%(url)s">öffnen</a>.'),
                    { name: duplicate.name, url: duplicate.url }, true) + '</span>';
        }

        function applyResult(result, list) {
            // The code field (e.g. ISBN) gets filled too; park it in lastQuery
            // so its change listener doesn't fire a redundant code lookup.
            if (queryKey && result.fields && result.fields[queryKey]) {
                lastQuery = String(result.fields[queryKey]).trim();
            }
            let count = 0;
            Object.keys(result.fields || {}).forEach(function (key) {
                if (fillField(key, result.fields[key])) count++;
            });
            Object.keys(result.covers || {}).forEach(function (key) { showCover(key, result.covers[key]); });
            if (list) list.innerHTML = '';
            setStatus('<i class="bi bi-check-circle text-success"></i> ' +
                interpolate(gettext('Vorschlag übernommen – %(count)s Feld(er) befüllt. Bitte prüfen und speichern.'),
                    { count: count }, true), 'text-success');
        }

        function renderResults(list, results) {
            list.innerHTML = '';
            if (!results.length) {
                const empty = document.createElement('div');
                empty.className = 'list-group-item small text-muted';
                empty.textContent = interpolate(gettext('Keine Treffer in %(provider)s.'), { provider: provider }, true);
                list.appendChild(empty);
                return;
            }
            results.forEach(function (result) {
                const row = document.createElement('div');
                row.className = 'list-group-item d-flex align-items-center gap-2 flex-wrap';
                if (result.cover) {
                    const img = document.createElement('img');
                    img.src = result.cover;
                    img.alt = '';
                    img.style.height = '48px';
                    img.className = 'rounded border';
                    img.addEventListener('error', function () { img.remove(); });
                    row.appendChild(img);
                }
                const text = document.createElement('div');
                text.className = 'flex-grow-1 small';
                text.textContent = result.label;
                row.appendChild(text);
                const applyBtn = document.createElement('button');
                applyBtn.type = 'button';
                applyBtn.className = 'btn btn-sm btn-primary';
                applyBtn.innerHTML = '<i class="bi bi-link-45deg"></i> ' + gettext('Übernehmen');
                applyBtn.addEventListener('click', function () { applyResult(result, list); });
                row.appendChild(applyBtn);
                list.appendChild(row);
            });
        }

        function runSearch(query, list) {
            const q = (query || '').trim();
            if (!q || !searchUrl) return;
            list.innerHTML = '<div class="list-group-item small text-muted">' +
                '<span class="spinner-border spinner-border-sm"></span> ' +
                interpolate(gettext('Suche in %(provider)s …'), { provider: provider }, true) + '</div>';
            fetch(searchUrl + '?q=' + encodeURIComponent(q), {
                headers: { 'X-Requested-With': 'XMLHttpRequest' },
            })
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    if (!data.ok) {
                        list.innerHTML = '';
                        setStatus('<i class="bi bi-exclamation-circle"></i> ' + (data.error || gettext('Fehler bei der Suche.')), 'text-danger');
                        return;
                    }
                    renderResults(list, data.results || []);
                })
                .catch(function () {
                    list.innerHTML = '<div class="list-group-item small text-danger">' + gettext('Suche nicht möglich (Netzwerk?).') + '</div>';
                });
        }

        // Wrap an input in an input-group (reusing one scanner.js may have
        // created) and append a button to it.
        function addGroupButton(input, btn) {
            let group = input.parentNode;
            if (!group.classList || !group.classList.contains('input-group')) {
                group = document.createElement('div');
                group.className = 'input-group';
                input.parentNode.insertBefore(group, input);
                group.appendChild(input);
            }
            group.appendChild(btn);
            return group;
        }

        // --- 1. Code lookup (scan/enter ISBN or another code) ---------------
        if (url && queryInput) {
            (queryInput.closest('.mb-3') || queryInput.parentNode).appendChild(status);

            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'btn btn-outline-primary';
            btn.title = interpolate(gettext('Code in allen Datenbanken (%(provider)s) suchen und Felder befüllen'),
                { provider: provider }, true);
            btn.innerHTML = '<i class="bi bi-search"></i> ' + gettext('Suchen');
            addGroupButton(queryInput, btn);

            function lookup() {
                const q = (queryInput.value || '').trim();
                if (!q || q === lastQuery) return;
                lastQuery = q;
                setStatus('<span class="spinner-border spinner-border-sm"></span> ' +
                    interpolate(gettext('Suche in %(provider)s …'), { provider: provider }, true), 'text-muted');
                fetch(url + '?q=' + encodeURIComponent(q) + (exclude ? '&exclude=' + encodeURIComponent(exclude) : ''), {
                    headers: { 'X-Requested-With': 'XMLHttpRequest' },
                })
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        if (!data.ok) {
                            setStatus('<i class="bi bi-exclamation-circle"></i> ' + (data.error || gettext('Fehler bei der Suche.')), 'text-danger');
                            return;
                        }
                        if (!data.found) {
                            setStatus('<i class="bi bi-info-circle"></i> ' +
                                interpolate(gettext('Kein Treffer in %(provider)s für „%(query)s“.'),
                                    { provider: provider, query: q }, true), 'text-warning');
                            showDuplicate(data.duplicate);
                            return;
                        }
                        let count = 0;
                        Object.keys(data.fields || {}).forEach(function (key) {
                            if (fillField(key, data.fields[key])) count++;
                        });
                        Object.keys(data.covers || {}).forEach(function (key) { showCover(key, data.covers[key]); });
                        setStatus('<i class="bi bi-check-circle text-success"></i> ' +
                            interpolate(gettext('%(count)s Feld(er) automatisch befüllt (Quellen: %(sources)s). Bitte prüfen und speichern.'),
                                { count: count, sources: data.provider }, true), 'text-success');
                        showDuplicate(data.duplicate);
                    })
                    .catch(function () {
                        setStatus('<i class="bi bi-exclamation-circle"></i> ' + gettext('Suche nicht möglich (Netzwerk?).'), 'text-danger');
                        lastQuery = null;
                    });
            }

            btn.addEventListener('click', function () { lastQuery = null; lookup(); });
            // Auto-trigger after a camera scan; also on manual entry (Enter / blur).
            queryInput.addEventListener('cms:scanned', lookup);
            queryInput.addEventListener('change', lookup);
            queryInput.addEventListener('keydown', function (e) {
                if (e.key === 'Enter') { e.preventDefault(); lastQuery = null; lookup(); }
            });

            setStatus('<i class="bi bi-magic"></i> ' +
                interpolate(gettext('Tipp: Code scannen oder eingeben – die Felder werden automatisch aus %(provider)s befüllt.'),
                    { provider: provider }, true), 'text-muted');
        }

        // --- 2. Suggestions from any mapped text field -----------------------
        // Each mapped field (title, author, publisher, …) gets a search button:
        // it looks the typed value up in all databases and offers the matching
        // records to pick from.
        if (searchUrl) {
            Object.keys(suggestFields).forEach(function (key) {
                const input = form.querySelector('[name="' + key + '"]');
                if (!input || input === queryInput) return;
                if (input.tagName !== 'INPUT' && input.tagName !== 'TEXTAREA') return;

                const list = document.createElement('div');
                list.className = 'list-group mt-2';

                const btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'btn btn-outline-secondary';
                btn.title = interpolate(gettext('Mit diesem Wert in allen Datenbanken (%(provider)s) suchen und einen Vorschlag übernehmen'),
                    { provider: provider }, true);
                btn.innerHTML = '<i class="bi bi-binoculars"></i>';
                btn.addEventListener('click', function () { runSearch(input.value, list); });

                if (input.tagName === 'TEXTAREA') {
                    // No input-group for textareas: place the button underneath.
                    const holder = document.createElement('div');
                    holder.className = 'mt-1';
                    btn.classList.add('btn-sm');
                    btn.innerHTML = '<i class="bi bi-binoculars"></i> ' + gettext('Vorschläge suchen');
                    holder.appendChild(btn);
                    input.after(holder);
                    holder.after(list);
                } else {
                    const group = addGroupButton(input, btn);
                    group.after(list);
                    input.addEventListener('keydown', function (e) {
                        if (e.key === 'Enter') { e.preventDefault(); runSearch(input.value, list); }
                    });
                }
            });
        }

        // --- 3. Generic search box (works without any code) ------------------
        if (searchUrl) {
            const wrap = document.createElement('div');
            wrap.className = 'mb-3';
            const label = document.createElement('label');
            label.className = 'form-label small text-muted';
                label.textContent = interpolate(gettext('In allen Datenbanken (%(provider)s) suchen und einen Vorschlag übernehmen:'),
                { provider: provider }, true);
            const searchGroup = document.createElement('div');
            searchGroup.className = 'input-group';
            const searchInput = document.createElement('input');
            searchInput.type = 'text';
            searchInput.className = 'form-control';
            searchInput.placeholder = gettext('Titel, Name, Stichwörter …');
            const searchBtn = document.createElement('button');
            searchBtn.type = 'button';
            searchBtn.className = 'btn btn-outline-primary';
            searchBtn.innerHTML = '<i class="bi bi-binoculars"></i> ' + gettext('Suchen');
            const list = document.createElement('div');
            list.className = 'list-group mt-2';
            searchGroup.appendChild(searchInput);
            searchGroup.appendChild(searchBtn);
            wrap.appendChild(label);
            wrap.appendChild(searchGroup);
            wrap.appendChild(list);
            cfg.after(wrap);
            if (!status.parentNode) wrap.appendChild(status);

            searchBtn.addEventListener('click', function () { runSearch(searchInput.value, list); });
            searchInput.addEventListener('keydown', function (e) {
                if (e.key === 'Enter') { e.preventDefault(); runSearch(searchInput.value, list); }
            });
        }
    });
})();
