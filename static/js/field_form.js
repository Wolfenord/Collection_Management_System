// Structured options editor for list (choice) field types.
//
// The Django form keeps the options in a hidden CharField (`choices_text`, one
// option per line — parsed server-side in FieldDefinitionForm.clean()). This
// script builds a chip editor on top of that hidden field and only reveals it
// when the selected field type is a list type (choice / multichoice). No more
// free-text typing into a textarea.
(function () {
    const gettext = window.gettext || function (s) { return s; };

    document.addEventListener('DOMContentLoaded', function () {
        const editor = document.getElementById('choicesEditor');
        if (!editor) return;

        const store = document.getElementById(editor.getAttribute('data-store'));
        const typeSelect = document.getElementById(editor.getAttribute('data-type-select'));
        const listTypes = (editor.getAttribute('data-list-types') || '').split(',');
        const input = document.getElementById('choicesInput');
        const addBtn = document.getElementById('choicesAdd');
        const chips = document.getElementById('choicesChips');
        if (!store || !typeSelect || !input || !addBtn || !chips) return;

        function parse() {
            return (store.value || '').split('\n')
                .map(function (s) { return s.trim(); })
                .filter(function (s) { return s.length; });
        }

        let options = parse();

        function persist() {
            store.value = options.join('\n');
        }

        function render() {
            chips.innerHTML = '';
            options.forEach(function (opt, index) {
                const chip = document.createElement('span');
                chip.className = 'badge text-bg-light border d-inline-flex align-items-center gap-1 py-2';
                const text = document.createElement('span');
                text.textContent = opt;
                chip.appendChild(text);
                const rm = document.createElement('button');
                rm.type = 'button';
                rm.className = 'btn-close btn-close-sm';
                rm.style.fontSize = '.6rem';
                rm.setAttribute('aria-label', gettext('Entfernen'));
                rm.addEventListener('click', function () {
                    options.splice(index, 1);
                    persist();
                    render();
                });
                chip.appendChild(rm);
                chips.appendChild(chip);
            });
            if (!options.length) {
                const empty = document.createElement('span');
                empty.className = 'text-muted small';
                empty.textContent = gettext('Noch keine Optionen.');
                chips.appendChild(empty);
            }
        }

        function addOption() {
            const value = (input.value || '').trim();
            if (!value) return;
            if (options.indexOf(value) === -1) {
                options.push(value);
                persist();
                render();
            }
            input.value = '';
            input.focus();
        }

        addBtn.addEventListener('click', addOption);
        input.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { e.preventDefault(); addOption(); }
        });

        function toggle() {
            const isList = listTypes.indexOf(typeSelect.value) !== -1;
            editor.classList.toggle('d-none', !isList);
        }
        typeSelect.addEventListener('change', toggle);

        render();
        toggle();
    });
})();
