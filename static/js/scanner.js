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
        let cameras = [];
        let currentCamId = null;
        // Remember the chosen camera across sessions (e.g. the macro lens).
        const CAM_KEY = 'cms.scanCameraId';

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
        // preference, and many phones then only focus at a distance. We switch
        // to continuous autofocus where supported, and — because most phone main
        // lenses can't focus close enough for a barcode (so it has to be held
        // further away, where it's too small to decode) — expose a zoom slider
        // the reader can adjust to enlarge the code. Everything is
        // feature-detected; unsupported capabilities are simply skipped.
        function optimizeFocus(video) {
            const stream = video && video.srcObject;
            const track = stream && stream.getVideoTracks && stream.getVideoTracks()[0];
            if (!track || !track.getCapabilities || !track.applyConstraints) return;
            const caps = track.getCapabilities();

            if (caps.focusMode && caps.focusMode.indexOf('continuous') !== -1) {
                track.applyConstraints({ advanced: [{ focusMode: 'continuous' }] })
                    .catch(function () { /* best effort */ });
            }

            if (caps.zoom && caps.zoom.max > (caps.zoom.min || 1)) {
                buildZoomSlider(track, caps.zoom);
            }
        }

        // Insert a zoom slider into the scan modal, wired to the live track.
        // A fixed zoom value is always wrong for some phone/distance combo; a
        // slider lets the user find the point where the barcode is both in
        // focus (held at the lens' focus distance) and large enough to decode.
        function buildZoomSlider(track, zoomCaps) {
            removeZoomSlider();
            const region = document.getElementById('scanRegion');
            if (!region) return;

            const min = zoomCaps.min || 1;
            const max = zoomCaps.max;
            const step = zoomCaps.step || 0.1;
            // Start slightly zoomed so a comfortably-held code is already large.
            const startZoom = Math.min(max, Math.max(min, 2));

            const wrap = document.createElement('div');
            wrap.id = 'scanZoom';
            wrap.className = 'd-flex align-items-center gap-2 mt-2';

            const label = document.createElement('label');
            label.className = 'small text-nowrap mb-0';
            label.innerHTML = '<i class="bi bi-zoom-in"></i> ' + gettext('Zoom');

            const slider = document.createElement('input');
            slider.type = 'range';
            slider.className = 'form-range';
            slider.min = min;
            slider.max = max;
            slider.step = step;
            slider.value = startZoom;
            slider.setAttribute('aria-label', gettext('Zoom'));
            slider.addEventListener('input', function () {
                track.applyConstraints({ advanced: [{ zoom: parseFloat(slider.value) }] })
                    .catch(function () { /* best effort */ });
            });

            wrap.appendChild(label);
            wrap.appendChild(slider);
            region.parentNode.insertBefore(wrap, region.nextSibling);

            track.applyConstraints({ advanced: [{ zoom: startZoom }] })
                .catch(function () { /* best effort */ });
        }

        function removeZoomSlider() {
            const existing = document.getElementById('scanZoom');
            if (existing) existing.remove();
        }

        // Pick the remembered camera if it is still present, else the rear
        // camera (usually the last entry in the list).
        function preferredCamId(list) {
            let stored = null;
            try { stored = localStorage.getItem(CAM_KEY); } catch (e) { /* ignore */ }
            if (stored && list.some(function (c) { return c.id === stored; })) return stored;
            return list[list.length - 1].id;
        }

        function startStream(camId) {
            return scanner.start(
                camId, { fps: 10, qrbox: { width: 260, height: 160 } }, onDecode, function () {}
            ).then(function () {
                optimizeFocus(document.querySelector('#scanRegion video'));
            });
        }

        // Switch to another lens without closing the modal. The ultra-wide /
        // macro camera focuses much closer, so barcodes can be held right up to
        // the phone — where the main lens only produces a blur.
        function switchCamera(camId) {
            currentCamId = camId;
            try { localStorage.setItem(CAM_KEY, camId); } catch (e) { /* ignore */ }
            if (!scanner) return;
            removeZoomSlider();
            scanner.stop().then(function () {
                return startStream(camId);
            }).catch(function () { /* best effort */ });
        }

        function buildCameraSelect() {
            removeCameraSelect();
            if (cameras.length < 2) return;  // nothing to choose from
            const region = document.getElementById('scanRegion');
            if (!region) return;

            const wrap = document.createElement('div');
            wrap.id = 'scanCamera';
            wrap.className = 'mb-2';

            const select = document.createElement('select');
            select.className = 'form-select form-select-sm';
            select.setAttribute('aria-label', gettext('Kamera wählen'));
            cameras.forEach(function (cam, i) {
                const opt = document.createElement('option');
                opt.value = cam.id;
                opt.textContent = cam.label || (gettext('Kamera') + ' ' + (i + 1));
                if (cam.id === currentCamId) opt.selected = true;
                select.appendChild(opt);
            });
            select.addEventListener('change', function () { switchCamera(select.value); });

            const hint = document.createElement('div');
            hint.className = 'form-text small mt-1';
            hint.textContent = gettext(
                'Für Nahaufnahmen eine andere Kamera wählen (Ultraweitwinkel/Makro stellt näher scharf).'
            );

            wrap.appendChild(select);
            wrap.appendChild(hint);
            region.parentNode.insertBefore(wrap, region);  // above the video
        }

        function removeCameraSelect() {
            const existing = document.getElementById('scanCamera');
            if (existing) existing.remove();
        }

        function start() {
            errorBox.classList.add('d-none');
            scanner = new window.Html5Qrcode('scanRegion', {
                formatsToSupport: formatsFor(targetInput && targetInput.getAttribute('data-scan')),
            });
            window.Html5Qrcode.getCameras().then(function (list) {
                if (!list || !list.length) throw new Error(gettext('Keine Kamera gefunden.'));
                cameras = list;
                currentCamId = preferredCamId(list);
                buildCameraSelect();
                return startStream(currentCamId);
            }).catch(function (err) {
                errorBox.textContent = gettext('Kamera nicht verfügbar:') + ' ' +
                    (err && err.message ? err.message : err) + ' – ' +
                    gettext('du kannst den Code auch manuell eingeben.');
                errorBox.classList.remove('d-none');
            });
        }

        function stop() {
            removeZoomSlider();
            removeCameraSelect();
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
