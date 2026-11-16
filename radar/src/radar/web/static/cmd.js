/* Manners for the filter panel.
 *
 * Everything here is decoration. The panel is a <details> containing a real GET
 * form, so with this file absent it still opens, still filters and still
 * submits — you just have to click «Bruk filtre» instead of clicking outside,
 * and Escape does nothing. Nothing below may become the only way to reach a
 * feature, and no state lives anywhere except the form's own controls.
 */
(function () {
  "use strict";

  var boxes = Array.prototype.slice.call(document.querySelectorAll(".filterbox"));
  var search = document.getElementById("sok");

  boxes.forEach(function (box) {
    var form = box.querySelector(".filter-panel");
    var summary = box.querySelector("summary");

    // Clicking away closes it — but only when nothing was changed, because
    // silently discarding half a set of ticked boxes is worse than leaving the
    // panel open. A touched form stays put until it is submitted or dismissed.
    var touched = false;
    form.addEventListener("change", function () { touched = true; });

    document.addEventListener("click", function (e) {
      if (!box.open || box.contains(e.target)) return;
      if (touched) return;
      box.open = false;
    });

    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape" || !box.open) return;
      box.open = false;
      touched = false;
      summary.focus();
    });

    box.addEventListener("toggle", function () {
      if (!box.open) { touched = false; return; }
      // Two panels open at once would overlap; the row only has room for one.
      boxes.forEach(function (other) { if (other !== box) other.open = false; });
      var first = form.querySelector("input:not([type=hidden])");
      if (first) first.focus();
    });

    // Enter anywhere in the panel applies the filters, which is what a form
    // does by default — but <details> swallows it on the summary, so say so.
    form.addEventListener("submit", function () { touched = false; });
  });

  // `/` and Cmd/Ctrl+K focus the search box from anywhere, with the usual guard
  // against hijacking a keystroke meant for another field.
  if (search) {
    document.addEventListener("keydown", function (e) {
      var t = e.target;
      var tag = (t.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select" || t.isContentEditable) return;
      if (e.key === "/" || ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k")) {
        e.preventDefault();
        search.focus();
        search.select();
      }
    });

    document.addEventListener("click", function (e) {
      if (!e.target.closest("[data-focus-search]")) return;
      e.preventDefault();
      search.scrollIntoView({ block: "center" });
      search.focus();
    });
  }
})();
