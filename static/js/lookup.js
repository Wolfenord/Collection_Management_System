// Auto-fill item fields from the external databases (DNB, Google Books, Open
// Library, MusicBrainz, …). Driven by #autofill (data-lookup-url = code lookup,
// data-search-url = text search, data-query-key, data-provider, data-exclude)
// rendered by item_form.html whenever at least one field is mapped to a lookup
// attribute.
//
// There is exactly ONE search entry point: the top search bar (#autofillSearch).
// It accepts free text (title, name, keywords) AND codes (ISBN/EAN). A code is
// detected automatically and routed to the code lookup; everything else runs a
// text search and offers candidate records to pick from. Individual fields are
// no longer searchable — but they can still be scanned (scanner.js/data-scan),
// e.g. to put an ISBN straight into its field when the search found nothing.
(function () {
    const gettext = window.gettext || function (s) { return s; };
    const interpolate = window.interpolate || function (fmt, obj) {
        return fmt.replace(/%\((\w+)\)s/g, function (m, k) { return String(obj[k]); });
    };

    document.addEventListener('DOMContentLoaded', function () {
        const cfg = document.getElementById('autofill');
        if (!cfg) return;

        const lookupUrl = cfg.getAttribute('data-lookup-url');   // code lookup (may be '')
        const searchUrl = cfg.getAttribute('data-search-url');   // text search (may be '')
        const provider = cfg.getAttribute('data-provider') || gettext('den externen Datenbanken');
        const exclude = cfg.getAttribute('data-exclude') || '';
        const form = cfg.closest('form') || document;

        const searchInput = document.getElementById('autofillSearchInput');
        const searchBtn = document.getElementById('autofillSearchBtn');
        const list = document.getElementById('autofillSearchResults');
        const status = document.getElementById('autofillStatus');
        if (!searchInput || !searchBtn || !list || !status) return;

        function setStatus(html, cls) {
            status.className = 'form-text mt-1 ' + (cls || '');
            status.innerHTML = html;
        }

        // A query counts as a code when it is (almost) all digits and has an
        // ISBN/EAN/UPC-typical length once separators are stripped.
        function asCode(query) {
            const digits = (query || '').replace(/[^0-9Xx]/g, '');
            const cleaned = (query || '').replace(/[\s-]/g, '');
            if (cleaned.length && digits.length / cleaned.length >= 0.9 &&
                (digits.length === 8 || digits.length === 10 ||
                 digits.length === 12 || digits.length === 13)) {
                return digits;
            }
            return '';
        }

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

        function applyFields(fields, covers) {
            let count = 0;
            Object.keys(fields || {}).forEach(function (key) {
                if (fillField(key, fields[key])) count++;
            });
            Object.keys(covers || {}).forEach(function (key) { showCover(key, covers[key]); });
            return count;
        }

        function applyResult(result) {
            const count = applyFields(result.fields, result.covers);
            list.innerHTML = '';
            setStatus('<i class="bi bi-check-circle text-success"></i> ' +
                interpolate(gettext('Vorschlag übernommen – %(count)s Feld(er) befüllt. Bitte prüfen und speichern.'),
                    { count: count }, true), 'text-success');
        }

        function renderResults(results) {
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
                applyBtn.addEventListener('click', function () { applyResult(result); });
                row.appendChild(applyBtn);
                list.appendChild(row);
            });
        }

        function spinner() {
            list.innerHTML = '<div class="list-group-item small text-muted">' +
                '<span class="spinner-border spinner-border-sm"></span> ' +
                interpolate(gettext('Suche in %(provider)s …'), { provider: provider }, true) + '</div>';
        }

        // --- Text search: candidate records to pick from --------------------
        function runTextSearch(query) {
            if (!searchUrl) { return runCodeLookup(query); }
            spinner();
            setStatus('', '');
            fetch(searchUrl + '?q=' + encodeURIComponent(query), {
                headers: { 'X-Requested-With': 'XMLHttpRequest' },
            })
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    if (!data.ok) {
                        list.innerHTML = '';
                        setStatus('<i class="bi bi-exclamation-circle"></i> ' + (data.error || gettext('Fehler bei der Suche.')), 'text-danger');
                        return;
                    }
                    renderResults(data.results || []);
                })
                .catch(function () {
                    list.innerHTML = '<div class="list-group-item small text-danger">' + gettext('Suche nicht möglich (Netzwerk?).') + '</div>';
                });
        }

        // --- Code lookup: fills the mapped fields directly ------------------
        function runCodeLookup(code) {
            if (!lookupUrl) { return runTextSearch(code); }
            list.innerHTML = '';
            setStatus('<span class="spinner-border spinner-border-sm"></span> ' +
                interpolate(gettext('Suche in %(provider)s …'), { provider: provider }, true), 'text-muted');
            fetch(lookupUrl + '?q=' + encodeURIComponent(code) + (exclude ? '&exclude=' + encodeURIComponent(exclude) : ''), {
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
                                { provider: provider, query: code }, true), 'text-warning');
                        showDuplicate(data.duplicate);
                        return;
                    }
                    const count = applyFields(data.fields, data.covers);
                    setStatus('<i class="bi bi-check-circle text-success"></i> ' +
                        interpolate(gettext('%(count)s Feld(er) automatisch befüllt (Quellen: %(sources)s). Bitte prüfen und speichern.'),
                            { count: count, sources: data.provider }, true), 'text-success');
                    showDuplicate(data.duplicate);
                })
                .catch(function () {
                    setStatus('<i class="bi bi-exclamation-circle"></i> ' + gettext('Suche nicht möglich (Netzwerk?).'), 'text-danger');
                });
        }

        function run() {
            const query = (searchInput.value || '').trim();
            if (!query) { searchInput.focus(); return; }
            const code = asCode(query);
            if (code && lookupUrl) { runCodeLookup(code); }
            else { runTextSearch(query); }
        }

        searchBtn.addEventListener('click', run);
        searchInput.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { e.preventDefault(); run(); }
        });
        // A scan into the top bar (scanner.js dispatches cms:scanned) auto-runs.
        searchInput.addEventListener('cms:scanned', run);

        setStatus('<i class="bi bi-magic"></i> ' +
            interpolate(gettext('Tipp: Titel/Name oder Code (ISBN/EAN) eingeben oder scannen – die Felder werden automatisch aus %(provider)s befüllt.'),
                { provider: provider }, true), 'text-muted');
    });
})();
