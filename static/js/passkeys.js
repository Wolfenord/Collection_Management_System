// Passkeys (WebAuthn): register a passkey on the profile page and sign in
// without a password on the login page. Server endpoints live in
// accounts/passkeys.py; options/answers travel as JSON with base64url fields.
(function () {
    // Translations from Django's JavaScriptCatalog (loaded in base.html).
    const gettext = window.gettext || function (s) { return s; };

    function b64urlToBuf(value) {
        const pad = '='.repeat((4 - (value.length % 4)) % 4);
        const raw = atob(value.replace(/-/g, '+').replace(/_/g, '/') + pad);
        const buf = new Uint8Array(raw.length);
        for (let i = 0; i < raw.length; i++) buf[i] = raw.charCodeAt(i);
        return buf.buffer;
    }

    function bufToB64url(buf) {
        const bytes = new Uint8Array(buf);
        let raw = '';
        bytes.forEach(function (b) { raw += String.fromCharCode(b); });
        return btoa(raw).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    }

    function csrf() {
        const m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
        return m ? m[1] : '';
    }

    function postJson(url, body) {
        return fetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrf(),
                'X-Requested-With': 'XMLHttpRequest',
            },
            body: body ? JSON.stringify(body) : '{}',
        }).then(function (r) {
            return r.json().then(function (data) {
                if (!r.ok || data.ok === false) {
                    throw new Error(data.error || gettext('Passkey-Vorgang fehlgeschlagen.'));
                }
                return data;
            });
        });
    }

    function supported() {
        return Boolean(window.PublicKeyCredential && navigator.credentials);
    }

    function showStatus(el, message, ok) {
        el.textContent = message;
        el.className = 'form-text mt-2 ' + (ok ? 'text-success' : 'text-danger');
        el.classList.remove('d-none');
    }

    // --- Registration (profile page) ------------------------------------
    const addBtn = document.getElementById('passkeyAdd');
    if (addBtn) {
        const status = document.getElementById('passkeyStatus');
        const labelInput = document.getElementById('passkeyLabel');
        if (!supported()) {
            addBtn.disabled = true;
            showStatus(status, gettext('Passkeys werden von diesem Browser nicht unterstützt oder die Seite läuft nicht über HTTPS.'), false);
        } else {
            addBtn.addEventListener('click', function () {
                addBtn.disabled = true;
                postJson(addBtn.getAttribute('data-begin-url'))
                    .then(function (options) {
                        options.challenge = b64urlToBuf(options.challenge);
                        options.user.id = b64urlToBuf(options.user.id);
                        (options.excludeCredentials || []).forEach(function (cred) {
                            cred.id = b64urlToBuf(cred.id);
                        });
                        return navigator.credentials.create({ publicKey: options });
                    })
                    .then(function (credential) {
                        return postJson(addBtn.getAttribute('data-complete-url'), {
                            label: labelInput ? labelInput.value : '',
                            credential: {
                                id: credential.id,
                                rawId: bufToB64url(credential.rawId),
                                type: credential.type,
                                response: {
                                    clientDataJSON: bufToB64url(credential.response.clientDataJSON),
                                    attestationObject: bufToB64url(credential.response.attestationObject),
                                },
                                clientExtensionResults: credential.getClientExtensionResults(),
                            },
                        });
                    })
                    .then(function () { window.location.reload(); })
                    .catch(function (err) {
                        addBtn.disabled = false;
                        showStatus(status, err && err.message ? err.message
                            : gettext('Passkey-Registrierung fehlgeschlagen.'), false);
                    });
            });
        }
    }

    // --- Login (login page) ----------------------------------------------
    const loginBtn = document.getElementById('passkeyLogin');
    if (loginBtn) {
        const status = document.getElementById('passkeyLoginStatus');
        if (!supported()) {
            loginBtn.classList.add('d-none');
        } else {
            loginBtn.addEventListener('click', function () {
                loginBtn.disabled = true;
                postJson(loginBtn.getAttribute('data-begin-url'))
                    .then(function (options) {
                        options.challenge = b64urlToBuf(options.challenge);
                        (options.allowCredentials || []).forEach(function (cred) {
                            cred.id = b64urlToBuf(cred.id);
                        });
                        return navigator.credentials.get({ publicKey: options });
                    })
                    .then(function (assertion) {
                        return postJson(loginBtn.getAttribute('data-complete-url'), {
                            next: loginBtn.getAttribute('data-next') || '',
                            credential: {
                                id: assertion.id,
                                rawId: bufToB64url(assertion.rawId),
                                type: assertion.type,
                                response: {
                                    clientDataJSON: bufToB64url(assertion.response.clientDataJSON),
                                    authenticatorData: bufToB64url(assertion.response.authenticatorData),
                                    signature: bufToB64url(assertion.response.signature),
                                    userHandle: assertion.response.userHandle
                                        ? bufToB64url(assertion.response.userHandle) : null,
                                },
                                clientExtensionResults: assertion.getClientExtensionResults(),
                            },
                        });
                    })
                    .then(function (data) { window.location.href = data.redirect; })
                    .catch(function (err) {
                        loginBtn.disabled = false;
                        showStatus(status, err && err.message ? err.message
                            : gettext('Passkey-Anmeldung fehlgeschlagen.'), false);
                    });
            });
        }
    }
})();
