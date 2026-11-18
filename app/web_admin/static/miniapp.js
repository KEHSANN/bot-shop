/**
 * Telegram Mini App Shop Controller:
 * Authenticates via X-Telegram-Init-Data, renders dynamic CSS Hex colors from DB,
 * supports 12 languages with RTL/LTR switching, enforces region-aware deposit display,
 * and performs atomic product purchases.
 */
(function () {
  "use strict";

  var state = {
    initData: "",
    langCode: document.documentElement.getAttribute("data-initial-lang") || "en",
    isRtl: document.documentElement.getAttribute("dir") === "rtl",
    supportsFiatIrr: false,
    user: null,
    categories: [],
    products: [],
    paymentMethods: { fiat_enabled: false, crypto_wallets: [], fiat_methods: [] },
    translations: {},
    selectedCategoryId: null,
    activeProduct: null
  };

  function getInitData() {
    if (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData) {
      window.Telegram.WebApp.ready();
      window.Telegram.WebApp.expand();
      return window.Telegram.WebApp.initData;
    }
    var params = new URLSearchParams(window.location.search);
    return params.get("init_data") || "";
  }

  function contrastText(hex) {
    if (!hex || !/^#[0-9a-fA-F]{6}$/.test(hex)) {
      return "#ffffff";
    }
    var r = parseInt(hex.slice(1, 3), 16);
    var g = parseInt(hex.slice(3, 5), 16);
    var b = parseInt(hex.slice(5, 7), 16);
    var lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
    return lum > 0.62 ? "#111827" : "#ffffff";
  }

  function localizeMap(mapObj, fallback) {
    if (!mapObj || typeof mapObj !== "object") {
      return fallback || "";
    }
    return mapObj[state.langCode] || mapObj.en || fallback || "";
  }

  function setStatusBanner(message, isError) {
    var banner = document.getElementById("miniapp-status");
    if (!banner) return;
    if (!message) {
      banner.classList.add("hidden");
      banner.textContent = "";
      return;
    }
    banner.textContent = message;
    banner.className = isError
      ? "mb-4 rounded-lg bg-red-900/60 border border-red-500/50 px-4 py-3 text-sm text-red-100"
      : "mb-4 rounded-lg bg-emerald-900/60 border border-emerald-500/50 px-4 py-3 text-sm text-emerald-100";
  }

  function renderHeader() {
    document.documentElement.setAttribute("dir", state.isRtl ? "rtl" : "ltr");
    var usdEl = document.getElementById("miniapp-balance-usd");
    var irtWrap = document.getElementById("miniapp-balance-irt-wrap");
    var irtEl = document.getElementById("miniapp-balance-irt");
    var langSelect = document.getElementById("miniapp-lang-select");

    if (usdEl && state.user) {
      usdEl.textContent = "$" + state.user.balance_usd;
    }
    if (irtWrap && irtEl) {
      if (state.langCode === "fa" && state.user) {
        irtWrap.classList.remove("hidden");
        irtEl.textContent = state.user.balance_irt + " IRT";
      } else {
        irtWrap.classList.add("hidden");
      }
    }
    if (langSelect && langSelect.value !== state.langCode) {
      langSelect.value = state.langCode;
    }
  }

  function renderCategories() {
    var container = document.getElementById("miniapp-categories");
    if (!container) return;
    container.innerHTML = "";

    var allBtn = document.createElement("button");
    allBtn.type = "button";
    allBtn.className = "px-3.5 py-2 rounded-xl text-xs font-semibold transition border " +
      (state.selectedCategoryId === null
        ? "bg-blue-600 text-white border-blue-500"
        : "bg-slate-800 text-slate-300 border-slate-700");
    allBtn.textContent = "🛍 All";
    allBtn.addEventListener("click", function () {
      state.selectedCategoryId = null;
      renderCategories();
      renderProducts();
    });
    container.appendChild(allBtn);

    state.categories.forEach(function (cat) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "px-3.5 py-2 rounded-xl text-xs font-semibold transition border";
      var hex = cat.color_hex || "#3b82f6";
      if (state.selectedCategoryId === cat.id) {
        btn.style.backgroundColor = hex;
        btn.style.color = contrastText(hex);
        btn.style.borderColor = hex;
      } else {
        btn.className += " bg-slate-800/90 text-slate-200 border-slate-700";
        btn.style.borderLeftWidth = "4px";
        btn.style.borderLeftColor = hex;
      }
      btn.textContent = (cat.color_emoji || "🔵") + " " + localizeMap(cat.names_json, cat.slug);
      btn.addEventListener("click", function () {
        state.selectedCategoryId = cat.id;
        renderCategories();
        renderProducts();
      });
      container.appendChild(btn);
    });
  }

  function renderProducts() {
    var grid = document.getElementById("miniapp-products");
    if (!grid) return;
    grid.innerHTML = "";

    var filtered = state.products.filter(function (prod) {
      return state.selectedCategoryId === null || prod.category_id === state.selectedCategoryId;
    });

    if (filtered.length === 0) {
      var empty = document.createElement("div");
      empty.className = "col-span-full text-center py-10 text-slate-400 text-sm";
      empty.textContent = state.translations["shop.no_products"] || "No products available.";
      grid.appendChild(empty);
      return;
    }

    filtered.forEach(function (prod) {
      var hex = prod.color_hex || "#22c55e";
      var card = document.createElement("div");
      card.className = "rounded-2xl bg-slate-800/90 border border-slate-700/80 p-4 flex flex-col justify-between shadow-lg";
      card.style.borderTopWidth = "4px";
      card.style.borderTopColor = hex;

      var top = document.createElement("div");
      var badgeRow = document.createElement("div");
      badgeRow.className = "flex items-center justify-between gap-2 mb-2";

      var badge = document.createElement("span");
      badge.className = "inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-bold";
      badge.style.backgroundColor = hex;
      badge.style.color = contrastText(hex);
      badge.textContent = (prod.color_emoji || "🟢") + (prod.is_pinned ? " ★ Featured" : " " + prod.delivery_type.toUpperCase());

      var stockSpan = document.createElement("span");
      stockSpan.className = "text-xs text-slate-400 font-mono";
      stockSpan.textContent = prod.manual_stock_count === -1 ? "∞ Stock" : "Stock: " + prod.available_stock;

      badgeRow.appendChild(badge);
      badgeRow.appendChild(stockSpan);

      var title = document.createElement("h3");
      title.className = "font-bold text-base text-white mb-1";
      title.textContent = localizeMap(prod.titles_json, prod.slug);

      var desc = document.createElement("p");
      desc.className = "text-xs text-slate-300 mb-3 line-clamp-2";
      desc.textContent = localizeMap(prod.descriptions_json, "");

      top.appendChild(badgeRow);
      top.appendChild(title);
      top.appendChild(desc);

      var bottom = document.createElement("div");
      bottom.className = "pt-3 border-t border-slate-700/60 flex items-center justify-between gap-2";

      var priceBox = document.createElement("div");
      var usdPrice = document.createElement("div");
      usdPrice.className = "text-sm font-bold text-emerald-400 font-mono";
      usdPrice.textContent = "$" + prod.price_usd;
      priceBox.appendChild(usdPrice);

      if (state.langCode === "fa" && prod.price_irt) {
        var irtPrice = document.createElement("div");
        irtPrice.className = "text-xs text-amber-300 font-mono";
        irtPrice.textContent = prod.price_irt + " تومان";
        priceBox.appendChild(irtPrice);
      }

      var buyBtn = document.createElement("button");
      buyBtn.type = "button";
      buyBtn.className = "px-3.5 py-2 rounded-xl text-xs font-bold transition shadow";
      buyBtn.style.backgroundColor = hex;
      buyBtn.style.color = contrastText(hex);
      buyBtn.textContent = (prod.color_emoji || "🛒") + " Buy";
      buyBtn.addEventListener("click", function () {
        openPurchaseModal(prod);
      });

      bottom.appendChild(priceBox);
      bottom.appendChild(buyBtn);

      card.appendChild(top);
      card.appendChild(bottom);
      grid.appendChild(card);
    });
  }

  function renderDepositMethods() {
    var cryptoList = document.getElementById("miniapp-crypto-list");
    var fiatSection = document.getElementById("miniapp-fiat-section");
    var fiatList = document.getElementById("miniapp-fiat-list");
    if (!cryptoList || !fiatSection || !fiatList) return;

    cryptoList.innerHTML = "";
    fiatList.innerHTML = "";

    var wallets = (state.paymentMethods && state.paymentMethods.crypto_wallets) || [];
    wallets.forEach(function (w) {
      var item = document.createElement("div");
      item.className = "rounded-xl bg-slate-900/80 border border-slate-700 p-3 text-xs";
      var hdr = document.createElement("div");
      hdr.className = "font-bold text-blue-300 mb-1";
      hdr.textContent = w.symbol + " (" + w.network + ") - Min $" + w.min_deposit_usd;
      var addr = document.createElement("code");
      addr.className = "block bg-slate-950 p-2 rounded text-slate-200 break-all select-all";
      addr.textContent = w.wallet_address;
      item.appendChild(hdr);
      item.appendChild(addr);
      cryptoList.appendChild(item);
    });

    if (state.langCode === "fa" && state.paymentMethods && state.paymentMethods.fiat_enabled) {
      fiatSection.classList.remove("hidden");
      var fiats = state.paymentMethods.fiat_methods || [];
      fiats.forEach(function (m) {
        var fItem = document.createElement("div");
        fItem.className = "rounded-xl bg-slate-900/80 border border-emerald-700/60 p-3 text-xs";
        var fHdr = document.createElement("div");
        fHdr.className = "font-bold text-emerald-300 mb-1";
        fHdr.textContent = m.title + " (" + m.method_type + ")";
        var fBody = document.createElement("div");
        fBody.className = "text-slate-300 font-mono";
        fBody.textContent = (m.card_number || m.gateway_provider || "") + (m.cardholder_name ? " - " + m.cardholder_name : "");
        fItem.appendChild(fHdr);
        fItem.appendChild(fBody);
        fiatList.appendChild(fItem);
      });
    } else {
      fiatSection.classList.add("hidden");
    }
  }

  function updateModalTotalPreview() {
    var prod = state.activeProduct;
    var qtyEl = document.getElementById("modal-qty");
    var curEl = document.getElementById("modal-currency");
    var totalEl = document.getElementById("modal-total-preview");
    if (!prod || !qtyEl || !curEl || !totalEl) return;
    var qty = Math.max(1, parseInt(qtyEl.value || "1", 10) || 1);
    var currency = curEl.value || "USD";
    var unitPrice = currency === "IRT" ? String(prod.price_irt || "0") : String(prod.price_usd || "0");
    var tiers = Array.isArray(prod.bulk_tiers) ? prod.bulk_tiers : [];
    var bestMin = 0;
    tiers.forEach(function (t) {
      if (t.min_qty <= qty && t.min_qty > bestMin) {
        if (currency === "IRT" && t.unit_price_irt !== null && t.unit_price_irt !== undefined) {
          bestMin = t.min_qty;
          unitPrice = String(t.unit_price_irt);
        } else if (currency === "USD") {
          bestMin = t.min_qty;
          unitPrice = String(t.unit_price_usd);
        }
      }
    });
    totalEl.textContent = "Unit: " + (currency === "IRT" ? unitPrice + " IRT" : "$" + unitPrice) + " × " + qty;
  }

  function renderModalBulkTiers(prod) {
    var tiersEl = document.getElementById("modal-bulk-tiers");
    if (!tiersEl) return;
    tiersEl.innerHTML = "";
    var tiers = Array.isArray(prod.bulk_tiers) ? prod.bulk_tiers : [];
    if (tiers.length === 0) {
      tiersEl.classList.add("hidden");
      return;
    }
    var heading = document.createElement("div");
    heading.className = "font-bold text-slate-200 mb-1";
    heading.textContent = "🏷 Bulk Discounts";
    tiersEl.appendChild(heading);
    tiers.forEach(function (t) {
      var line = document.createElement("div");
      line.className = "text-slate-300 font-mono";
      var txt = "• " + t.min_qty + "+ : $" + t.unit_price_usd;
      if (state.langCode === "fa" && t.unit_price_irt) {
        txt += " / " + t.unit_price_irt + " IRT";
      }
      line.textContent = txt;
      tiersEl.appendChild(line);
    });
    tiersEl.classList.remove("hidden");
  }

  async function openPurchaseModal(prod) {
    state.activeProduct = prod;
    var modal = document.getElementById("miniapp-purchase-modal");
    var titleEl = document.getElementById("modal-prod-title");
    var rulesEl = document.getElementById("modal-prod-rules");
    var currencySelect = document.getElementById("modal-currency");
    var manualWrap = document.getElementById("modal-manual-wrap");
    var manualLabel = document.getElementById("modal-manual-label");
    var submitBtn = document.getElementById("modal-submit-btn");
    if (!modal || !titleEl) return;

    titleEl.textContent = (prod.color_emoji || "🟢") + " " + localizeMap(prod.titles_json, prod.slug);
    if (rulesEl) {
      rulesEl.textContent = localizeMap(prod.rules_json, "");
    }
    if (submitBtn && prod.color_hex) {
      submitBtn.style.backgroundColor = prod.color_hex;
      submitBtn.style.color = contrastText(prod.color_hex);
    }
    if (currencySelect) {
      currencySelect.innerHTML = "";
      var optUsd = document.createElement("option");
      optUsd.value = "USD";
      optUsd.textContent = "USD ($" + prod.price_usd + ")";
      currencySelect.appendChild(optUsd);
      if (state.langCode === "fa" && prod.price_irt) {
        var optIrt = document.createElement("option");
        optIrt.value = "IRT";
        optIrt.textContent = "Toman (" + prod.price_irt + " IRT)";
        currencySelect.appendChild(optIrt);
      }
    }
    if (manualWrap) {
      if (prod.delivery_type === "manual") {
        manualWrap.classList.remove("hidden");
        if (manualLabel) {
          manualLabel.textContent = localizeMap(prod.manual_prompts_json, "Activation Input (Email / Link / ID)");
        }
      } else {
        manualWrap.classList.add("hidden");
      }
    }
    renderModalBulkTiers(prod);
    updateModalTotalPreview();
    modal.classList.remove("hidden");

    if (state.initData) {
      try {
        var detailResp = await apiFetch("/miniapp/api/products/" + prod.id);
        if (detailResp.ok) {
          var detailData = await detailResp.json();
          if (detailData && detailData.product && state.activeProduct && state.activeProduct.id === prod.id) {
            state.activeProduct = detailData.product;
            renderModalBulkTiers(state.activeProduct);
            updateModalTotalPreview();
          }
        }
      } catch (_err) {
        /* ignore transient detail fetch error */
      }
    }
  }

  async function loadOrders() {
    var listEl = document.getElementById("miniapp-orders-list");
    if (!listEl || !state.initData) return;
    try {
      var resp = await apiFetch("/miniapp/api/orders");
      if (!resp.ok) return;
      var data = await resp.json();
      var orders = data.orders || [];
      listEl.innerHTML = "";
      if (orders.length === 0) {
        var empty = document.createElement("div");
        empty.className = "text-slate-400";
        empty.textContent = state.translations["shop.no_orders"] || "No orders placed yet.";
        listEl.appendChild(empty);
        return;
      }
      orders.forEach(function (o) {
        var row = document.createElement("div");
        row.className = "rounded-xl bg-slate-950 border border-slate-800 p-3 space-y-1";
        var top = document.createElement("div");
        top.className = "flex justify-between font-mono font-bold";
        var num = document.createElement("span");
        num.className = "text-blue-300";
        num.textContent = o.order_number + " (×" + o.quantity + ")";
        var st = document.createElement("span");
        st.className = "text-emerald-400 uppercase";
        st.textContent = o.status + " · " + o.total_amount + " " + o.currency;
        top.appendChild(num);
        top.appendChild(st);
        row.appendChild(top);
        if (Array.isArray(o.items) && o.items.length > 0) {
          o.items.forEach(function (it) {
            if (it.decrypted_payload) {
              var code = document.createElement("pre");
              code.className = "bg-slate-900 rounded p-2 text-slate-200 overflow-x-auto select-all";
              code.textContent = it.decrypted_payload;
              row.appendChild(code);
            }
          });
        }
        listEl.appendChild(row);
      });
    } catch (_err) {
      /* ignore order fetch error */
    }
  }

  async function apiFetch(url, options) {
    var opts = options || {};
    opts.headers = opts.headers || {};
    if (state.initData) {
      opts.headers["X-Telegram-Init-Data"] = state.initData;
    }
    return fetch(url, opts);
  }

  async function loadBootstrap(langOverride) {
    state.initData = getInitData();
    if (!state.initData) {
      return;
    }
    var url = "/miniapp/api/bootstrap";
    if (langOverride) {
      url += "?lang=" + encodeURIComponent(langOverride);
    }
    try {
      var resp = await apiFetch(url);
      var data = await resp.json();
      if (!resp.ok) {
        setStatusBanner(data.detail || "Authentication failed", true);
        return;
      }
      state.user = data.user;
      state.langCode = data.lang_code;
      state.isRtl = Boolean(data.is_rtl);
      state.supportsFiatIrr = Boolean(data.supports_fiat_irr);
      state.categories = data.categories || [];
      state.products = data.products || [];
      state.paymentMethods = data.payment_methods || { fiat_enabled: false, crypto_wallets: [], fiat_methods: [] };
      state.translations = data.translations || {};
      renderHeader();
      renderCategories();
      renderProducts();
      renderDepositMethods();
      loadOrders();
    } catch (err) {
      setStatusBanner("Failed to connect to Mini App API", true);
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    var langSelect = document.getElementById("miniapp-lang-select");
    if (langSelect) {
      langSelect.addEventListener("change", async function () {
        var nextLang = langSelect.value;
        if (!state.initData) {
          window.location.search = "?lang=" + encodeURIComponent(nextLang);
          return;
        }
        var resp = await apiFetch("/miniapp/api/language", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ lang_code: nextLang })
        });
        if (resp.ok) {
          await loadBootstrap(nextLang);
        }
      });
    }

    var closeBtn = document.getElementById("modal-close-btn");
    if (closeBtn) {
      closeBtn.addEventListener("click", function () {
        var modal = document.getElementById("miniapp-purchase-modal");
        if (modal) modal.classList.add("hidden");
      });
    }

    var qtyInput = document.getElementById("modal-qty");
    if (qtyInput) qtyInput.addEventListener("input", updateModalTotalPreview);
    var curSelect = document.getElementById("modal-currency");
    if (curSelect) curSelect.addEventListener("change", updateModalTotalPreview);
    var refreshOrdersBtn = document.getElementById("miniapp-refresh-orders");
    if (refreshOrdersBtn) refreshOrdersBtn.addEventListener("click", loadOrders);

    var purchaseForm = document.getElementById("miniapp-purchase-form");
    if (purchaseForm) {
      purchaseForm.addEventListener("submit", async function (ev) {
        ev.preventDefault();
        if (!state.activeProduct) return;
        var qty = parseInt(document.getElementById("modal-qty").value || "1", 10);
        var currency = document.getElementById("modal-currency").value || "USD";
        var manualInput = document.getElementById("modal-manual-input").value || "";

        var resp = await apiFetch("/miniapp/api/purchase", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            product_id: state.activeProduct.id,
            quantity: qty,
            currency: currency,
            fulfillment_input: manualInput
          })
        });
        var data = await resp.json();
        var modal = document.getElementById("miniapp-purchase-modal");
        if (modal) modal.classList.add("hidden");

        if (!resp.ok) {
          setStatusBanner(data.detail || "Purchase failed", true);
          return;
        }
        var orderNum = data.purchase && data.purchase.order ? data.purchase.order.order_number : "";
        var items = (data.purchase && data.purchase.delivered_items) || [];
        var summary = "Order " + orderNum + " completed!";
        if (items.length > 0) {
          summary += " Delivered: " + items.join(" | ");
        }
        setStatusBanner(summary, false);
        await loadBootstrap(state.langCode);
      });
    }

    loadBootstrap();
  });
})();
