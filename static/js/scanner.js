// Camera scanning for inputs marked with data-scan ("isbn" | "barcode") and for
// the scan-to-find button (#scanFind, data-find-url): scanning an item label /
// ISBN there navigates to the find view, which opens the matching item.
// Uses html5-qrcode (loaded where needed). Inputs still work manually
// if the library or a camera is unavailable.
(function () {
    // Translations from Django's JavaScriptCatalog (loaded in base.html).
    const gettext = window.gettext || function (s) { return s; };

    document.addEventListener('DOMContentLoaded', function () {
        const inputs = document.querySelectorAll('input[data-scan]');
        const findBtn = document.getElementById('scanFind');
        if ((!inputs.length && !findBtn) || !window.Html5Qrcode) return;

        const modalEl = document.getElementById('scanModal');
        const modal = window.bootstrap ? new window.bootstrap.Modal(modalEl) : null;
        const errorBox = document.getElementById('scanError');
        const F = window.Html5QrcodeSupportedFormats;
        let scanner = null;
        let targetInput = null;
        let findUrl = null;

        if (findBtn) {
            findBtn.addEventListener('click', function () {
                targetInput = null;
                findUrl = findBtn.getAttribute('data-find-url');
                if (modal) modal.show();
            });
        }

        function formatsFor(kind) {
            if (kind === 'isbn') return [F.EAN_13, F.EAN_8];
            return [F.CODE_128, F.CODE_39, F.EAN_13, F.EAN_8, F.UPC_A, F.UPC_E, F.QR_CODE];
        }

        // Wrap each input in a Bootstrap input-group and add the camera button.
        inputs.forEach(function (input) {
            const group = document.createElement('div');
            group.className = 'input-group';
            input.parentNode.insertBefore(group, input);
            group.appendChild(input);

            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'btn btn-outline-secondary';
            btn.title = gettext('Mit Kamera scannen');
            btn.innerHTML = '<i class="bi bi-camera"></i>';
            btn.addEventListener('click', function () {
                targetInput = input;
                findUrl = null;
                if (modal) modal.show();
            });
            group.appendChild(btn);
        });

        function onDecode(text) {
            if (findUrl) {
                window.location.href = findUrl + '?code=' + encodeURIComponent(text);
                return;
            }
            if (targetInput) {
                targetInput.value = text;
                // Notify listeners (e.g. lookup.js auto-fill) that a code was scanned.
                targetInput.dispatchEvent(new Event('change', { bubbles: true }));
                targetInput.dispatchEvent(new CustomEvent('cms:scanned', { bubbles: true, detail: text }));
            }
            if (modal) modal.hide();
        }

        // Close-up sharpness: browsers start the camera without any focus
        // preference, and many phones then only focus at a distance. Where the
        // track supports it, switch to continuous autofocus and add a slight
        // zoom (helps main lenses whose minimum focus distance is too far for
        // a barcode held close — e.g. iPhones, which expose zoom but not
        // focusMode). Everything is feature-detected; unsupported = no-op.
        function optimizeFocus(video) {
            const stream = video && video.srcObject;
            const track = stream && stream.getVideoTracks && stream.getVideoTracks()[0];
            if (!track || !track.getCapabilities || !track.applyConstraints) return;
            const caps = track.getCapabilities();
            const advanced = [];
            const hasContinuousFocus =
                caps.focusMode && caps.focusMode.indexOf('continuous') !== -1;
            if (hasContinuousFocus) {
                advanced.push({ focusMode: 'continuous' });
            }
            // Only zoom as a fallback when continuous autofocus is NOT available
            // (e.g. iOS, whose main lens can't focus close). Where autofocus works
            // (most Android), a forced zoom just crops/pixelates the view and makes
            // it hard to frame a barcode held close.
            if (!hasContinuousFocus && caps.zoom && caps.zoom.max >= 2) {
                advanced.push({ zoom: Math.min(2, caps.zoom.max) });
            }
            if (advanced.length) {
                track.applyConstraints({ advanced: advanced }).catch(function () { /* best effort */ });
            }
        }

        function start() {
            errorBox.classList.add('d-none');
            scanner = new window.Html5Qrcode('scanRegion', {
                formatsToSupport: formatsFor(targetInput && targetInput.getAttribute('data-scan')),
            });
            window.Html5Qrcode.getCameras().then(function (cameras) {
                if (!cameras || !cameras.length) throw new Error(gettext('Keine Kamera gefunden.'));
                // Prefer the rear camera (usually the last entry).
                const camId = cameras[cameras.length - 1].id;
                return scanner.start(camId, { fps: 10, qrbox: { width: 260, height: 160 } }, onDecode, function () {});
            }).then(function () {
                optimizeFocus(document.querySelector('#scanRegion video'));
            }).catch(function (err) {
                errorBox.textContent = gettext('Kamera nicht verfügbar:') + ' ' +
                    (err && err.message ? err.message : err) + ' – ' +
                    gettext('du kannst den Code auch manuell eingeben.');
                errorBox.classList.remove('d-none');
            });
        }

        function stop() {
            if (scanner) {
                scanner.stop().then(function () { scanner.clear(); }).catch(function () {});
                scanner = null;
            }
        }

        if (modalEl) {
            modalEl.addEventListener('shown.bs.modal', start);
            modalEl.addEventListener('hidden.bs.modal', stop);
        }
    });
})();
