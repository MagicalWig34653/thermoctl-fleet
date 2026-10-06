// Login page: the two steps of the design draft ("1 Anmelden", "2 Bestätigen")
// are two panes of ONE form that is posted exactly once to POST /ui/login.
//
// Security notes:
//  * This script never looks at what the password is and never talks to the
//    server on step 1 -- the "Weiter" button only checks that the required
//    fields are not empty (browser validity), so it cannot be used as an
//    oracle for credentials. The server decides after step 2, with one
//    generic failure message.
//  * The code input is a single real <input name="totp_code"> (accessible,
//    inputmode=numeric, autocomplete=one-time-code, paste works); the six
//    boxes are decoration mirroring its value.
//  * Without this script every field is visible and the plain "Anmelden"
//    button submits the form (see login.html / auth.css).
(function () {
  "use strict";
  document.documentElement.classList.add("js");

  function ready(fn) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", fn);
    } else {
      fn();
    }
  }

  ready(function () {
    var flow = document.getElementById("auth-flow");
    var form = document.getElementById("login-form");
    if (!flow || !form) {
      return;
    }
    var username = document.getElementById("username");
    var password = document.getElementById("password");
    var code = document.getElementById("totp_code");
    var codeArea = document.getElementById("code-area");
    var help = document.getElementById("code-help");
    var steps = document.getElementById("auth-steps");
    var stepOne = document.getElementById("step-1");
    var stepTwo = document.getElementById("step-2");
    var nextButton = document.getElementById("step-next");
    var backButton = document.getElementById("step-back");
    var submitButton = document.getElementById("login-submit");
    var reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    var defaultHelp = help ? help.textContent : "";
    var step = 1;
    var autoTimer = null;
    var busy = false;

    // -- steps ----------------------------------------------------------

    function setStep(next, focus) {
      step = next;
      flow.setAttribute("data-step", String(next));
      steps.setAttribute("aria-label", "Anmeldung, Schritt " + next + " von 2");
      stepOne.className = "step " + (next === 1 ? "active" : "done");
      stepTwo.className = "step" + (next === 2 ? " active" : "");
      stepOne.querySelector(".step-number").textContent = next === 1 ? "1" : "✓";
      if (next === 1) {
        stepOne.setAttribute("aria-current", "step");
        stepTwo.removeAttribute("aria-current");
      } else {
        stepTwo.setAttribute("aria-current", "step");
        stepOne.removeAttribute("aria-current");
      }
      if (focus) {
        (next === 2 ? code : username.value ? password : username).focus();
      }
    }

    function goNext() {
      // Only "are the required fields filled in" -- never whether they are
      // correct, and nothing is sent anywhere.
      var fields = [username, password];
      for (var i = 0; i < fields.length; i += 1) {
        if (!fields[i].checkValidity()) {
          fields[i].reportValidity();
          return;
        }
      }
      setStep(2, true);
      syncCode();
    }

    nextButton.addEventListener("click", goNext);
    backButton.addEventListener("click", function () {
      setStep(1, true);
    });

    // Enter in a step-1 field must lead to step 2, not submit half a login.
    form.addEventListener("submit", function (event) {
      if (step === 1) {
        event.preventDefault();
        goNext();
      }
    });

    // -- password reveal --------------------------------------------------

    var reveal = document.getElementById("reveal");
    if (reveal) {
      reveal.addEventListener("click", function (event) {
        var show = password.type === "password";
        password.type = show ? "text" : "password";
        event.currentTarget.setAttribute("aria-pressed", String(show));
        event.currentTarget.setAttribute("aria-label", show ? "Passwort verbergen" : "Passwort anzeigen");
      });
    }

    // -- six-box code input ----------------------------------------------

    function syncCode(animate) {
      var boxes = document.querySelectorAll(".code-box");
      boxes.forEach(function (box, index) {
        var digit = code.value[index] || "";
        var changed = box.textContent !== digit;
        box.textContent = digit;
        box.classList.toggle("filled", index < code.value.length);
        var caret = code.selectionStart === null ? code.value.length : code.selectionStart;
        box.classList.toggle("current", index === Math.min(caret, 5));
        if (animate && changed && digit && !reducedMotion.matches && box.animate) {
          box.getAnimations().forEach(function (animation) {
            animation.cancel();
          });
          box.animate(
            [
              { transform: "translateY(2px) scale(.95)" },
              { transform: "translateY(-2px) scale(1.025)", offset: 0.55 },
              { transform: "translateY(0) scale(1)" }
            ],
            { duration: 220, easing: "ease-out" }
          );
        }
      });
    }

    function digitsOnly(value) {
      return value.replace(/\D/g, "").slice(0, 6);
    }

    function setBusy(value) {
      busy = value;
      code.readOnly = value;
      form.setAttribute("aria-busy", String(value));
      codeArea.classList.toggle("checking", value);
      if (value) {
        help.textContent = "Code wird geprüft …";
        submitButton.querySelector("span").textContent = "Code wird geprüft …";
      } else {
        help.textContent = defaultHelp;
        submitButton.querySelector("span").textContent = "Bestätigen & anmelden";
      }
    }

    function scheduleSubmit() {
      clearTimeout(autoTimer);
      if (step === 2 && code.value.length === 6 && !busy) {
        autoTimer = setTimeout(function () {
          if (step === 2 && code.value.length === 6 && !busy) {
            setBusy(true);
            form.requestSubmit(submitButton);
          }
        }, 220);
      }
    }

    function onChange(animate) {
      syncCode(animate);
      scheduleSubmit();
    }

    code.addEventListener("input", function (event) {
      if (event.isComposing) {
        return;
      }
      var caret = code.selectionStart;
      var raw = code.value;
      code.value = digitsOnly(raw);
      if (caret !== null && raw === code.value) {
        code.setSelectionRange(caret, caret);
      }
      onChange(true);
    });
    code.addEventListener("compositionend", function () {
      code.value = digitsOnly(code.value);
      onChange(true);
    });
    code.addEventListener("paste", function (event) {
      if (busy) {
        event.preventDefault();
        return;
      }
      var pasted = event.clipboardData ? event.clipboardData.getData("text") : undefined;
      if (pasted === undefined) {
        return;
      }
      event.preventDefault();
      var digits = pasted.replace(/\D/g, "");
      if (digits.length >= 6) {
        code.value = digits.slice(0, 6);
        code.setSelectionRange(6, 6);
      } else {
        code.setRangeText(digits, code.selectionStart, code.selectionEnd, "end");
        code.value = code.value.slice(0, 6);
      }
      onChange(true);
    });
    ["focus", "click", "keyup", "select"].forEach(function (name) {
      code.addEventListener(name, function () {
        syncCode(false);
      });
    });

    // A passkey attempt must not race an automatic code submit.
    var passkey = document.getElementById("webauthn-login-button");
    if (passkey) {
      passkey.addEventListener("click", function () {
        clearTimeout(autoTimer);
      });
    }

    window.addEventListener("pagehide", function () {
      clearTimeout(autoTimer);
    });
    // Back/forward cache: come back to a usable step 2, not a stuck "busy" one.
    window.addEventListener("pageshow", function (event) {
      if (event.persisted) {
        setBusy(false);
        code.value = "";
        syncCode(false);
      }
    });

    setStep(1, false);
    syncCode(false);
  });
})();
