"""Provenance and integrity of the vendored browser age implementation
(P5.5b, owner decision 2026-09-28: "Vendor a JS age implementation into
the repo (e.g. the `age-encryption` npm package / typage), a pinned exact
version, served by the fleet as a static file, with its sha256 recorded
and verified by a test; no CDN at runtime").

**What is vendored, and how it was built** (so a future version bump is a
reviewed, reproducible diff, not "npm update and hope"):

`fleet/static/ui/vendor/age-encryption.vendor.js` is a self-contained,
minified browser bundle built from the npm package
[`age-encryption`](https://www.npmjs.com/package/age-encryption) (the
[typage](https://github.com/FiloSottile/typage) project, the reference
implementation this project's own README already points landlords to for
`age -d ...`), **pinned to exactly `AGE_VENDOR_PACKAGE_VERSION` below**.

Built with:

```
npm install --no-save age-encryption@0.3.1 esbuild
```

`entry.js` (this project's own file, not part of the upstream package --
the *only* piece of upstream surface re-exported to the page, see this
module's own reasoning for why the exported functions should stay
minimal):

```js
import { Encrypter } from "age-encryption";

async function encryptToRecipient(plaintext, recipient) {
  const encrypter = new Encrypter();
  encrypter.addRecipient(recipient);
  return await encrypter.encrypt(plaintext);
}

window.thermoctlAge = { encryptToRecipient };
```

Bundled with:

```
esbuild entry.js --bundle --minify --format=iife --platform=browser \\
  --target=es2020 --outfile=age-encryption.vendor.js
```

This produces one self-contained file with **no remaining `import`
statements and no references to any external URL**
(`tests/test_restore_vendor.py
::test_vendored_age_js_contains_no_external_url_references`) -- "no CDN
at runtime" is therefore not merely a claim in this docstring, it is a
property the bundle's own bytes are checked against. The bundle exposes
exactly one global, `window.thermoctlAge.encryptToRecipient(plaintext:
Uint8Array, recipient: string) -> Promise<Uint8Array>`, consumed by
`fleet/static/ui/restore_form.js` -- nothing else from the underlying
package (passphrase encryption, WebAuthn identities, armor/PEM framing)
is re-exported, so this project's own surface against a future upstream
change stays as small as the one function it actually needs.

**Integrity, checked by a test, not only recorded here:**
`AGE_VENDOR_JS_SHA256` is this exact file's SHA-256 hex digest --
`tests/test_restore_vendor.py
::test_vendored_age_js_matches_the_recorded_sha256` fails the moment
either one changes without the other, so nobody can silently edit or
replace the vendored file without the test suite noticing. **Upgrading
the pinned version** means re-running the build above with a new
`@version`, replacing the vendored file, and updating both constants
below to match -- always a reviewed two-file diff together.
"""

from __future__ import annotations

from pathlib import Path

AGE_VENDOR_PACKAGE_NAME = "age-encryption"
AGE_VENDOR_PACKAGE_VERSION = "0.3.1"

AGE_VENDOR_JS_PATH = (
    Path(__file__).parent / "static" / "ui" / "vendor" / "age-encryption.vendor.js"
)

# SHA-256 hex digest of `AGE_VENDOR_JS_PATH`'s exact current bytes -- see
# the module docstring for how it was produced and why this is checked by
# a test, not merely documented here.
AGE_VENDOR_JS_SHA256 = "73faba0770247c381c1f1e9e1ce0896a2788cd0d0e8443df38227be8fb6f5c23"
