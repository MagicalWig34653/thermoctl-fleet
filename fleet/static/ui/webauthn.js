// Passkey (WebAuthn) support for the login form and the account page (P6.2).
// Same-origin, served under /ui/static -- CSP `script-src 'self'` allows it
// with no inline script and no CDN (see fleet/ui_routes.py's
// `install_security_headers`). This file only ever talks to this app's own
// /ui/login/webauthn/* and /ui/account/webauthn/* endpoints, and only ever
// calls the browser's own `navigator.credentials` API -- it never
// implements a cryptographic primitive itself (all verification happens
// server-side in fleet.webauthn_auth, via the real `webauthn` library).
(function () {
  "use strict";

  function base64urlToBytes(value) {
    const padded = value.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((value.length + 3) % 4);
    const binary = window.atob(padded);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) {
      bytes[i] = binary.charCodeAt(i);
    }
    return bytes;
  }

  function bytesToBase64url(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = "";
    for (let i = 0; i < bytes.length; i += 1) {
      binary += String.fromCharCode(bytes[i]);
    }
    return window.btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function optionsToPublicKey(options, isRegistration) {
    const publicKey = Object.assign({}, options);
    publicKey.challenge = base64urlToBytes(options.challenge);
    if (isRegistration && options.user) {
      publicKey.user = Object.assign({}, options.user, { id: base64urlToBytes(options.user.id) });
    }
    if (options.excludeCredentials) {
      publicKey.excludeCredentials = options.excludeCredentials.map(function (descriptor) {
        return Object.assign({}, descriptor, { id: base64urlToBytes(descriptor.id) });
      });
    }
    if (options.allowCredentials) {
      publicKey.allowCredentials = options.allowCredentials.map(function (descriptor) {
        return Object.assign({}, descriptor, { id: base64urlToBytes(descriptor.id) });
      });
    }
    return publicKey;
  }

  function registrationCredentialToJson(credential) {
    return JSON.stringify({
      id: credential.id,
      rawId: bytesToBase64url(credential.rawId),
      type: credential.type,
      response: {
        clientDataJSON: bytesToBase64url(credential.response.clientDataJSON),
        attestationObject: bytesToBase64url(credential.response.attestationObject),
      },
      clientExtensionResults: {},
    });
  }

  function assertionCredentialToJson(credential) {
    const response = {
      clientDataJSON: bytesToBase64url(credential.response.clientDataJSON),
      authenticatorData: bytesToBase64url(credential.response.authenticatorData),
      signature: bytesToBase64url(credential.response.signature),
    };
    if (credential.response.userHandle) {
      response.userHandle = bytesToBase64url(credential.response.userHandle);
    }
    return JSON.stringify({
      id: credential.id,
      rawId: bytesToBase64url(credential.rawId),
      type: credential.type,
      response: response,
      clientExtensionResults: {},
    });
  }

  function postForm(url, fields) {
    const body = new URLSearchParams(fields);
    return window.fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body.toString(),
    });
  }

  function initLoginButton() {
    const button = document.getElementById("webauthn-login-button");
    const form = document.getElementById("login-form");
    if (!button || !form) {
      return;
    }
    const errorBox = document.getElementById("webauthn-error");
    button.addEventListener("click", function () {
      if (errorBox) {
        errorBox.textContent = "";
      }
      const username = document.getElementById("username").value;
      const preCsrf = form.querySelector('input[name="pre_csrf"]').value;
      postForm("/ui/login/webauthn/begin", { username: username, pre_csrf: preCsrf })
        .then(function (response) {
          if (!response.ok) {
            throw new Error("begin failed");
          }
          return response.json();
        })
        .then(function (options) {
          const challengeId = options.fleetChallengeId;
          return navigator.credentials
            .get({ publicKey: optionsToPublicKey(options, false) })
            .then(function (credential) {
              document.getElementById("webauthn-assertion").value =
                assertionCredentialToJson(credential);
              document.getElementById("webauthn-challenge-id").value = String(challengeId);
              form.submit();
            });
        })
        .catch(function () {
          if (errorBox) {
            errorBox.textContent = "Anmeldung mit Passkey nicht möglich.";
          }
        });
    });
  }

  function initRegisterForm() {
    const form = document.getElementById("webauthn-register-reauth-form");
    if (!form) {
      return;
    }
    const errorBox = document.getElementById("webauthn-register-error");
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (errorBox) {
        errorBox.textContent = "";
      }
      const csrfToken = document.getElementById("csrf_token").value;
      const totpCode = document.getElementById("reauth_totp_code").value;
      const label = document.getElementById("passkey_label").value || "Passkey";

      postForm("/ui/account/webauthn/register/begin", {
        csrf_token: csrfToken,
        totp_code: totpCode,
      })
        .then(function (response) {
          if (!response.ok) {
            throw new Error("begin failed");
          }
          return response.json();
        })
        .then(function (options) {
          const challengeId = options.fleetChallengeId;
          return navigator.credentials
            .create({ publicKey: optionsToPublicKey(options, true) })
            .then(function (credential) {
              return postForm("/ui/account/webauthn/register/complete", {
                csrf_token: csrfToken,
                challenge_id: String(challengeId),
                credential_json: registrationCredentialToJson(credential),
                label: label,
              });
            });
        })
        .then(function (response) {
          if (!response || !response.ok) {
            throw new Error("register failed");
          }
          window.location.reload();
        })
        .catch(function () {
          if (errorBox) {
            errorBox.textContent = "Registrierung des Passkeys fehlgeschlagen.";
          }
        });
    });
  }

  function init() {
    initLoginButton();
    initRegisterForm();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
