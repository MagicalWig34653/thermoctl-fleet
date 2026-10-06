// Progressive enhancement for the fleet UI shell. Everything works without
// this file: the mobile menu is the `:target` of the hamburger link, the
// filter form has a submit button. Loaded synchronously in <head> (see
// base.html) so the `js` class is set before the first paint.
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
    var body = document.body;
    var opener = document.querySelector('[data-action="menu"]');

    function setMenu(open) {
      body.classList.toggle("menu-open", open);
      if (opener) {
        opener.setAttribute("aria-expanded", open ? "true" : "false");
      }
    }

    // The links keep their href (#sidebar / #main) for the no-JS case; with
    // JS the same click toggles a class instead of changing the URL hash.
    document.querySelectorAll('[data-action="menu"]').forEach(function (link) {
      link.addEventListener("click", function (event) {
        event.preventDefault();
        setMenu(true);
      });
    });
    document.querySelectorAll('[data-action="menu-close"]').forEach(function (link) {
      link.addEventListener("click", function (event) {
        event.preventDefault();
        setMenu(false);
      });
    });
    document.querySelectorAll("#sidebar a.nav-link, #sidebar a.account").forEach(function (link) {
      link.addEventListener("click", function () {
        setMenu(false);
      });
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && body.classList.contains("menu-open")) {
        setMenu(false);
        if (opener) {
          opener.focus();
        }
      }
    });

    // Filter forms (Wohnungen): changing a select submits the form, the
    // plain GET round trip the page already supports.
    document.querySelectorAll("form[data-autosubmit]").forEach(function (form) {
      form.querySelectorAll("select").forEach(function (select) {
        select.addEventListener("change", function () {
          form.submit();
        });
      });
    });
  });
})();
