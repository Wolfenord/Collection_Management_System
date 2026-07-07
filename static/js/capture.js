// Camera photo capture for file inputs marked with data-capture ("image" | "file").
// Injects a camera button next to each such input; a modal shows a live preview
// and the captured photo is placed into the file input as if it were selected.
// Normal file selection keeps working; without camera support no button is added.
(function () {
    // Translations from Django's JavaScriptCatalog (loaded in base.html).
    const gettext = window.gettext || function (s) { return s; };

    document.addEventListener('DOMContentLoaded', function () {
        const inputs = document.querySelectorAll('input[type="file"][data-capture]');
        const modalEl = document.getElementById('captureModal');
        if (!inputs.length || !modalEl) return;
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.DataTransfer) return;

        const modal = window.bootstrap ? new window.bootstrap.Modal(modalEl) : null;
        const video = document.getElementById('captureVideo');
        const shotBtn = document.getElementById('captureShot');
        const errorBox = document.getElementById('captureError');
        let stream = null;
        let targetInput = null;

        // Wrap each input in a Bootstrap input-group and add the camera button.
        inputs.forEach(function (input) {
            const group = document.createElement('div');
            group.className = 'input-group';
            input.parentNode.insertBefore(group, input);
            group.appendChild(input);

            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'btn btn-outline-secondary';
            btn.title = gettext('Mit Kamera aufnehmen');
            btn.innerHTML = '<i class="bi bi-camera"></i>';
            btn.addEventListener('click', function () {
                targetInput = input;
                if (modal) modal.show();
            });
            group.appendChild(btn);
        });

        function start() {
            errorBox.classList.add('d-none');
            shotBtn.disabled = true;
            navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' }, audio: false })
                .then(function (s) {
                    stream = s;
                    video.srcObject = s;
                    shotBtn.disabled = false;
                })
                .catch(function (err) {
                    errorBox.textContent = gettext('Kamera nicht verfügbar:') + ' ' +
                        (err && err.message ? err.message : err) + ' – ' +
                        gettext('du kannst stattdessen eine Datei auswählen.');
                    errorBox.classList.remove('d-none');
                });
        }

        function stop() {
            if (stream) {
                stream.getTracks().forEach(function (t) { t.stop(); });
                stream = null;
            }
            video.srcObject = null;
        }

        function takePhoto() {
            if (!stream || !targetInput) return;
            const w = video.videoWidth || 1280;
            const h = video.videoHeight || 720;
            // Downscale to keep uploads small (receipts/photos don't need more).
            const MAX_DIM = 1600;
            const scale = Math.min(1, MAX_DIM / Math.max(w, h));
            const canvas = document.createElement('canvas');
            canvas.width = Math.round(w * scale);
            canvas.height = Math.round(h * scale);
            canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
            canvas.toBlob(function (blob) {
                if (!blob) return;
                const file = new File([blob], 'kamera-' + Date.now() + '.jpg', { type: 'image/jpeg' });
                const dt = new DataTransfer();
                dt.items.add(file);
                targetInput.files = dt.files;
                targetInput.dispatchEvent(new Event('change', { bubbles: true }));
                if (modal) modal.hide();
            }, 'image/jpeg', 0.92);
        }

        shotBtn.addEventListener('click', takePhoto);
        modalEl.addEventListener('shown.bs.modal', start);
        modalEl.addEventListener('hidden.bs.modal', stop);
    });
})();
