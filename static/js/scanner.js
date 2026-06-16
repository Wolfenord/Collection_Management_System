// Camera scanning for inputs marked with data-scan ("isbn" | "barcode").
// Injects a camera button next to each such input; scanning fills the input.
// Uses html5-qrcode (loaded on the item form page). Inputs still work manually
// if the library or a camera is unavailable.
(function () {
    document.addEventListener('DOMContentLoaded', function () {
        const inputs = document.querySelectorAll('input[data-scan]');
        if (!inputs.length || !window.Html5Qrcode) return;

        const modalEl = document.getElementById('scanModal');
        const modal = window.bootstrap ? new window.bootstrap.Modal(modalEl) : null;
        const errorBox = document.getElementById('scanError');
        const F = window.Html5QrcodeSupportedFormats;
        let scanner = null;
        let targetInput = null;

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
            btn.title = 'Mit Kamera scannen';
            btn.innerHTML = '<i class="bi bi-camera"></i>';
            btn.addEventListener('click', function () {
                targetInput = input;
                if (modal) modal.show();
            });
            group.appendChild(btn);
        });

        function onDecode(text) {
            if (targetInput) targetInput.value = text;
            if (modal) modal.hide();
        }

        function start() {
            errorBox.classList.add('d-none');
            scanner = new window.Html5Qrcode('scanRegion', {
                formatsToSupport: formatsFor(targetInput && targetInput.getAttribute('data-scan')),
            });
            window.Html5Qrcode.getCameras().then(function (cameras) {
                if (!cameras || !cameras.length) throw new Error('Keine Kamera gefunden.');
                // Prefer the rear camera (usually the last entry).
                const camId = cameras[cameras.length - 1].id;
                return scanner.start(camId, { fps: 10, qrbox: { width: 260, height: 160 } }, onDecode, function () {});
            }).catch(function (err) {
                errorBox.textContent = 'Kamera nicht verfügbar: ' + (err && err.message ? err.message : err) +
                    ' – du kannst den Code auch manuell eingeben.';
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
