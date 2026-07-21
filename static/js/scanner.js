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

        // Let the device manage the camera: we ask for the rear-facing camera
        // (facingMode "environment") and otherwise leave lens choice, zoom and
        // exposure to the platform's own logic — like the phone's native camera
        // app. The only nudge is enabling continuous autofocus where the browser
        // exposes it (some phones otherwise lock focus and never sharpen on a
        // close barcode). Fully automatic lens switching (wide↔macro by distance)
        // is an OS feature not available to web pages, so it can't be replicated
        // here. Everything below is best-effort and feature-detected.
        function enableAutofocus(video) {
            const stream = video && video.srcObject;
            const track = stream && stream.getVideoTracks && stream.getVideoTracks()[0];
            if (!track || !track.getCapabilities || !track.applyConstraints) return;
            const caps = track.getCapabilities();
            if (caps.focusMode && caps.focusMode.indexOf('continuous') !== -1) {
                track.applyConstraints({ advanced: [{ focusMode: 'continuous' }] })
                    .catch(function () { /* best effort */ });
            }
        }

        function start() {
            errorBox.classList.add('d-none');
            scanner = new window.Html5Qrcode('scanRegion', {
                formatsToSupport: formatsFor(targetInput && targetInput.getAttribute('data-scan')),
            });
            // Hand camera selection to the browser/OS via facingMode instead of
            // pinning a deviceId or forcing a zoom, so the device decides.
            scanner.start(
                { facingMode: 'environment' },
                { fps: 10, qrbox: { width: 260, height: 160 } },
                onDecode,
                function () {}
            ).then(function () {
                enableAutofocus(document.querySelector('#scanRegion video'));
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
