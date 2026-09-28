// Restore form (P5.5b, owner decision 2026-09-28): the landlord types the
// decryption key into a field with **no `name` attribute** -- it is never
// submitted as part of the ordinary form POST, not even with JavaScript
// disabled (a browser never includes a nameless field in a form
// submission; this is not something this script has to enforce, it is a
// property of HTML forms themselves, which is exactly why the field is
// built that way in `fleet/templates/ui/apartment.html` rather than this
// script hiding/removing a named one at runtime).
//
// Without JavaScript, this form does nothing at all: there is no
// server-side fallback that would accept a plaintext key (deliberately --
// see `fleet.ui_routes.apartment_restore_create`, which only ever reads
// `key_block`, never a plaintext-key field, because no such field is ever
// submitted in the first place).
//
// With JavaScript: on submit, this script reads the key field's value
// directly from the DOM (never via form submission), encrypts it locally
// with the vendored age implementation
// (`fleet/static/ui/vendor/age-encryption.vendor.js`, loaded by the page
// before this script) to the device's own recipient (embedded in the page
// as a `data-recipient` attribute -- a public value, safe to render
// server-side), writes the resulting ciphertext into a hidden, *named*
// field (`key_block_b64`), clears the plaintext field, and only then lets
// the form submit normally (CSRF token and all, same as every other `/ui`
// POST in this codebase).
(function () {
  "use strict";

  function base64FromBytes(bytes) {
    let binary = "";
    for (let i = 0; i < bytes.length; i += 1) {
      binary += String.fromCharCode(bytes[i]);
    }
    return window.btoa(binary);
  }

  function init() {
    const form = document.getElementById("restore-form");
    if (!form) {
      return;
    }
    const keyField = document.getElementById("restore-key-plaintext");
    const hiddenField = document.getElementById("restore-key-block");
    const errorBox = document.getElementById("restore-error");
    const recipient = form.getAttribute("data-recipient");

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (errorBox) {
        errorBox.textContent = "";
      }
      const plaintext = keyField ? keyField.value : "";
      if (!plaintext) {
        if (errorBox) {
          errorBox.textContent = "Bitte den Schlüssel eingeben.";
        }
        return;
      }
      if (!window.thermoctlAge || !recipient) {
        if (errorBox) {
          errorBox.textContent =
            "Verschlüsselung im Browser nicht verfügbar -- bitte Seite neu laden.";
        }
        return;
      }
      const plaintextBytes = new TextEncoder().encode(plaintext);
      window.thermoctlAge
        .encryptToRecipient(plaintextBytes, recipient)
        .then(function (ciphertext) {
          hiddenField.value = base64FromBytes(new Uint8Array(ciphertext));
          // The plaintext never travels any further than this point --
          // cleared from the DOM immediately after it has done its one
          // job (being encrypted), before the form is ever submitted.
          if (keyField) {
            keyField.value = "";
          }
          form.submit();
        })
        .catch(function () {
          if (errorBox) {
            errorBox.textContent =
              "Verschlüsselung fehlgeschlagen -- bitte den Schlüssel prüfen.";
          }
        });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
