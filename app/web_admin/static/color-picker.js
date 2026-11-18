/**
 * Dual-Layer Color Picker Controller:
 * Synchronizes HTML5 <input type="color">, #RRGGBB text input, RGB readout,
 * preset color swatches, and Emoji badge selector with real-time Telegram & Web previews.
 */
(function () {
  "use strict";

  var HEX_RE = /^#[0-9a-fA-F]{6}$/;

  function hexToRgb(hex) {
    if (!HEX_RE.test(hex)) {
      return { r: 59, g: 130, b: 246 };
    }
    var clean = hex.slice(1);
    return {
      r: parseInt(clean.slice(0, 2), 16),
      g: parseInt(clean.slice(2, 4), 16),
      b: parseInt(clean.slice(4, 6), 16)
    };
  }

  function contrastColor(hex) {
    var rgb = hexToRgb(hex);
    var lum = (0.299 * rgb.r + 0.587 * rgb.g + 0.114 * rgb.b) / 255;
    return lum > 0.62 ? "#111827" : "#ffffff";
  }

  function bindColorPickerWidget(container) {
    var colorWheel = container.querySelector("[data-color-wheel]");
    var hexInput = container.querySelector("[data-color-hex]");
    var rgbLabel = container.querySelector("[data-color-rgb]");
    var emojiInput = container.querySelector("[data-color-emoji]");
    var titleSourceSelector = container.getAttribute("data-preview-title-source");
    var titleInput = titleSourceSelector ? document.querySelector(titleSourceSelector) : null;
    var tgPreview = container.querySelector("[data-tg-preview]");
    var webPreview = container.querySelector("[data-web-preview]");
    var swatches = container.querySelectorAll("[data-preset-hex]");
    var emojiBtns = container.querySelectorAll("[data-preset-emoji]");

    function updatePreview() {
      var hexVal = (hexInput && hexInput.value ? hexInput.value.trim() : "#3b82f6").toLowerCase();
      if (!HEX_RE.test(hexVal)) {
        if (hexInput) {
          hexInput.classList.add("border-red-500");
        }
        return;
      }
      if (hexInput) {
        hexInput.classList.remove("border-red-500");
      }
      if (colorWheel && colorWheel.value.toLowerCase() !== hexVal) {
        colorWheel.value = hexVal;
      }
      var rgb = hexToRgb(hexVal);
      if (rgbLabel) {
        rgbLabel.textContent = "rgb(" + rgb.r + ", " + rgb.g + ", " + rgb.b + ")";
      }
      var emojiVal = emojiInput && emojiInput.value ? emojiInput.value.trim() : "🔵";
      var labelVal = (titleInput && titleInput.value ? titleInput.value.trim() : "") ||
        container.getAttribute("data-default-label") ||
        "Sample Item";

      if (tgPreview) {
        tgPreview.textContent = emojiVal + " " + labelVal + " - Stock: 42";
      }
      if (webPreview) {
        webPreview.style.backgroundColor = hexVal;
        webPreview.style.color = contrastColor(hexVal);
        webPreview.style.borderColor = hexVal;
        webPreview.textContent = emojiVal + " " + labelVal;
      }

      swatches.forEach(function (btn) {
        var presetHex = (btn.getAttribute("data-preset-hex") || "").toLowerCase();
        if (presetHex === hexVal) {
          btn.classList.add("active");
        } else {
          btn.classList.remove("active");
        }
      });

      emojiBtns.forEach(function (btn) {
        var presetEmoji = btn.getAttribute("data-preset-emoji") || "";
        if (presetEmoji === emojiVal) {
          btn.classList.add("active");
        } else {
          btn.classList.remove("active");
        }
      });
    }

    if (colorWheel) {
      colorWheel.addEventListener("input", function () {
        if (hexInput) {
          hexInput.value = colorWheel.value.toLowerCase();
        }
        updatePreview();
      });
    }

    if (hexInput) {
      hexInput.addEventListener("input", function () {
        var raw = hexInput.value.trim();
        if (raw && raw[0] !== "#") {
          hexInput.value = "#" + raw;
        }
        updatePreview();
      });
    }

    if (emojiInput) {
      emojiInput.addEventListener("input", updatePreview);
    }

    if (titleInput) {
      titleInput.addEventListener("input", updatePreview);
    }

    swatches.forEach(function (btn) {
      btn.addEventListener("click", function (ev) {
        ev.preventDefault();
        var presetHex = btn.getAttribute("data-preset-hex");
        var presetEmoji = btn.getAttribute("data-preset-swatch-emoji");
        if (hexInput && presetHex) {
          hexInput.value = presetHex.toLowerCase();
        }
        if (emojiInput && presetEmoji) {
          emojiInput.value = presetEmoji;
        }
        updatePreview();
      });
    });

    emojiBtns.forEach(function (btn) {
      btn.addEventListener("click", function (ev) {
        ev.preventDefault();
        var presetEmoji = btn.getAttribute("data-preset-emoji");
        if (emojiInput && presetEmoji) {
          emojiInput.value = presetEmoji;
        }
        updatePreview();
      });
    });

    updatePreview();
  }

  document.addEventListener("DOMContentLoaded", function () {
    var widgets = document.querySelectorAll("[data-color-picker-widget]");
    widgets.forEach(bindColorPickerWidget);

    var rtlToggle = document.getElementById("rtl-toggle-btn");
    if (rtlToggle) {
      var savedDir = localStorage.getItem("shopbot_admin_dir");
      if (savedDir === "rtl" || savedDir === "ltr") {
        document.documentElement.setAttribute("dir", savedDir);
      }
      rtlToggle.addEventListener("click", function () {
        var current = document.documentElement.getAttribute("dir") || "ltr";
        var next = current === "rtl" ? "ltr" : "rtl";
        document.documentElement.setAttribute("dir", next);
        localStorage.setItem("shopbot_admin_dir", next);
      });
    }
  });
})();
