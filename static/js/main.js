(function () {
  'use strict';

  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  var csrfToken = csrfMeta ? csrfMeta.getAttribute('content') : '';
  var progressEl = document.getElementById('htmx-progress');
  var pageLoader = document.getElementById('jm-loader');
  var pendingGlobalProgress = 0;

  function hidePageLoader() {
    if (!pageLoader || pageLoader.classList.contains('is-done')) {
      return;
    }
    pageLoader.classList.add('is-done');
    window.setTimeout(function () {
      if (pageLoader) {
        pageLoader.setAttribute('hidden', '');
      }
    }, 450);
  }

  function triggeringElementHasIndicator(elt) {
    if (!elt) {
      return false;
    }
    if (elt.getAttribute && elt.getAttribute('hx-indicator')) {
      return true;
    }
    return !!elt.closest('[hx-indicator]');
  }

  function showGlobalProgress() {
    if (!progressEl) {
      return;
    }
    progressEl.hidden = false;
    progressEl.setAttribute('aria-hidden', 'false');
    progressEl.classList.add('is-active');
  }

  function hideGlobalProgress() {
    if (!progressEl || pendingGlobalProgress > 0) {
      return;
    }
    progressEl.classList.remove('is-active');
    progressEl.hidden = true;
    progressEl.setAttribute('aria-hidden', 'true');
  }

  function showHtmxToast() {
    var root = document.getElementById('htmx-toast-root');
    if (!root) {
      return;
    }
    root.hidden = false;
    root.innerHTML =
      '<div class="htmx-toast" role="alert">' +
      '<span class="htmx-toast__message">Something went wrong, please try again.</span>' +
      '<button type="button" class="htmx-toast__close btn-ghost" aria-label="Dismiss">&times;</button>' +
      '</div>';
    var closeBtn = root.querySelector('.htmx-toast__close');
    if (closeBtn) {
      closeBtn.addEventListener('click', function () {
        root.hidden = true;
        root.innerHTML = '';
      });
    }
    window.setTimeout(function () {
      if (!root.hidden) {
        root.hidden = true;
        root.innerHTML = '';
      }
    }, 6000);
  }

  document.body.addEventListener('htmx:configRequest', function (event) {
    if (csrfToken) {
      event.detail.headers['X-CSRFToken'] = csrfToken;
    }
  });

  document.body.addEventListener('htmx:beforeRequest', function (event) {
    if (triggeringElementHasIndicator(event.detail.elt)) {
      return;
    }
    pendingGlobalProgress += 1;
    showGlobalProgress();
  });

  document.body.addEventListener('htmx:afterRequest', function (event) {
    if (triggeringElementHasIndicator(event.detail.elt)) {
      return;
    }
    pendingGlobalProgress = Math.max(0, pendingGlobalProgress - 1);
    hideGlobalProgress();
  });

  document.body.addEventListener('htmx:responseError', function (event) {
    console.error('[HTMX] responseError', event.detail);
    showHtmxToast();
  });

  document.body.addEventListener('htmx:sendError', function (event) {
    console.error('[HTMX] sendError', event.detail);
    showHtmxToast();
  });

  window.addEventListener('load', function () {
    window.setTimeout(hidePageLoader, 320);
  });
  // Fallback if load already fired or assets cached
  if (document.readyState === 'complete') {
    window.setTimeout(hidePageLoader, 320);
  } else {
    document.addEventListener('DOMContentLoaded', function () {
      window.setTimeout(function () {
        if (document.readyState === 'complete') {
          hidePageLoader();
        }
      }, 1200);
    });
  }

  function loadCartDrawerIfNeeded() {
    var body = document.getElementById('cart-drawer-body');
    if (!body || body.dataset.drawerHydrated === 'true' || !window.htmx) {
      return;
    }
    var url = body.getAttribute('hx-get');
    if (!url) {
      return;
    }
    htmx.ajax('GET', url, { target: '#cart-drawer-body', swap: 'innerHTML' });
    body.dataset.drawerHydrated = 'true';
  }

  var cartOffcanvas = document.getElementById('cartOffcanvas');
  if (cartOffcanvas) {
    cartOffcanvas.addEventListener('shown.bs.offcanvas', function () {
      loadCartDrawerIfNeeded();
    });
  }

  document.body.addEventListener('htmx:afterSwap', function (event) {
    if (event.detail.target && event.detail.target.id === 'cart-drawer-body') {
      event.detail.target.dataset.drawerHydrated = 'true';
    }
    if (event.detail.target && event.detail.target.id === 'search-suggestions') {
      initSearchSuggestionsKeyboard();
      updateSearchDropdownState();
    }
  });

  function updateSearchDropdownState() {
    var input = document.getElementById('site-search-input');
    var panel = document.getElementById('search-suggestions-dropdown');
    var results = document.getElementById('search-suggestions');
    if (!input || !panel || !results) {
      return;
    }
    var isOpen = results.innerHTML.trim().length > 0 || panel.querySelector('.htmx-request');
    input.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
  }

  function closeSearchSuggestions() {
    var input = document.getElementById('site-search-input');
    var results = document.getElementById('search-suggestions');
    if (!input || !results) {
      return;
    }
    results.innerHTML = '';
    input.setAttribute('aria-expanded', 'false');
    input.focus();
  }

  function initSearchSuggestionsKeyboard() {
    var input = document.getElementById('site-search-input');
    var results = document.getElementById('search-suggestions');
    if (!input || !results) {
      return;
    }
    var links = results.querySelectorAll('.search-suggestion-link');
    links.forEach(function (link, index) {
      link.setAttribute('data-suggestion-index', String(index));
    });
    if (links.length) {
      input.dataset.activeSuggestion = '0';
      links[0].classList.add('is-active');
    } else {
      delete input.dataset.activeSuggestion;
    }
  }

  function setActiveSearchSuggestion(input, links, index) {
    links.forEach(function (link) { link.classList.remove('is-active'); });
    if (index < 0 || index >= links.length) {
      delete input.dataset.activeSuggestion;
      return;
    }
    input.dataset.activeSuggestion = String(index);
    links[index].classList.add('is-active');
    links[index].scrollIntoView({ block: 'nearest' });
  }

  var searchInput = document.getElementById('site-search-input');
  if (searchInput) {
    searchInput.addEventListener('keydown', function (event) {
      var results = document.getElementById('search-suggestions');
      if (!results) {
        return;
      }
      var links = results.querySelectorAll('.search-suggestion-link');
      var activeIndex = parseInt(searchInput.dataset.activeSuggestion || '-1', 10);

      if (event.key === 'Escape') {
        event.preventDefault();
        closeSearchSuggestions();
        return;
      }

      if (!links.length) {
        return;
      }

      if (event.key === 'ArrowDown') {
        event.preventDefault();
        var next = activeIndex < links.length - 1 ? activeIndex + 1 : 0;
        setActiveSearchSuggestion(searchInput, links, next);
      } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        var prev = activeIndex > 0 ? activeIndex - 1 : links.length - 1;
        setActiveSearchSuggestion(searchInput, links, prev);
      } else if (event.key === 'Enter' && activeIndex >= 0 && links[activeIndex]) {
        event.preventDefault();
        window.location.href = links[activeIndex].href;
      }
    });

    searchInput.addEventListener('input', function () {
      if (!searchInput.value.trim()) {
        closeSearchSuggestions();
      }
    });

    searchInput.addEventListener('blur', function () {
      window.setTimeout(function () {
        var results = document.getElementById('search-suggestions');
        if (!results || document.activeElement === searchInput) {
          return;
        }
        if (!results.contains(document.activeElement)) {
          results.innerHTML = '';
          searchInput.setAttribute('aria-expanded', 'false');
        }
      }, 150);
    });
  }

  document.body.addEventListener('htmx:beforeRequest', function (event) {
    if (event.detail.elt && event.detail.elt.id === 'site-search-input') {
      var results = document.getElementById('search-suggestions');
      if (results) {
        results.innerHTML = '';
      }
      updateSearchDropdownState();
    }
  });

  document.body.addEventListener('htmx:afterRequest', function (event) {
    if (event.detail.elt && event.detail.elt.id === 'site-search-input') {
      updateSearchDropdownState();
    }
  });

  function reinitPageScripts() {
    document.querySelectorAll('.thumb-btn').forEach(function (btn) {
      if (btn.dataset.boundThumb) return;
      btn.dataset.boundThumb = '1';
      btn.addEventListener('click', function () {
        var main = document.getElementById('main-pdp-image');
        if (main) main.src = this.getAttribute('data-full');
        document.querySelectorAll('.thumb-btn').forEach(function (b) {
          b.classList.remove('active', 'is-active');
        });
        this.classList.add('active', 'is-active');
      });
    });
  }

  function applyDocumentLocale(detail) {
    if (!detail || !detail.lang) {
      return;
    }
    document.documentElement.lang = detail.lang;
    document.documentElement.dir = detail.dir || 'ltr';
    var rtlHref = document.body.getAttribute('data-rtl-stylesheet');
    var rtlLink = document.getElementById('rtl-stylesheet');
    if (detail.dir === 'rtl') {
      if (!rtlLink && rtlHref) {
        rtlLink = document.createElement('link');
        rtlLink.id = 'rtl-stylesheet';
        rtlLink.rel = 'stylesheet';
        rtlLink.href = rtlHref;
        document.head.appendChild(rtlLink);
      }
    } else if (rtlLink) {
      rtlLink.remove();
    }
  }

  document.body.addEventListener('preferencesUpdated', function (event) {
    applyDocumentLocale(event.detail);
  });

  document.body.addEventListener('htmx:beforeRequest', function (event) {
    if (event.detail.elt && event.detail.elt.classList.contains('preference-switcher')) {
      sessionStorage.setItem('flowardScrollY', String(window.scrollY));
    }
  });

  document.body.addEventListener('htmx:afterSettle', function (event) {
    if (event.detail.target && event.detail.target.id === 'floward-app-shell') {
      var saved = sessionStorage.getItem('flowardScrollY');
      if (saved !== null) {
        window.scrollTo(0, parseInt(saved, 10));
        sessionStorage.removeItem('flowardScrollY');
      }
      reinitPageScripts();
    }
    if (event.detail.target && event.detail.target.id === 'product-grid') {
      event.detail.target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      reinitPageScripts();
    }
  });

  reinitPageScripts();


  /*
   * Attention coordinator — search nudge pulses on a fixed schedule.
   * Timing plan (page load = 0):
   *   Search preferred: 15s, 55s, 95s (max 3, then stop / stop on search use)
   *   Each effect lasts 8s; after any effect, channel locked for 8s + 30s gap
   *   If a preferred time is busy, it waits until the channel is free
   */
  (function initAttentionEffects() {
    var SHOW_MS = 8000;
    var MIN_GAP_MS = 30000;
    var SEARCH_FIRST_MS = 15000;
    var SEARCH_GAP_MS = 40000;
    var SEARCH_MAX = 3;

    var freeAt = 0;
    var queue = Promise.resolve();
    var searchStopped = false;
    var timers = [];

    var openBtn = document.getElementById('mobile-search-open');
    var nudge = openBtn ? openBtn.closest('.jm-search-nudge') : null;
    var overlay = document.getElementById('mobile-search-overlay');
    var input = document.getElementById('mobile-search-input');
    var clearBtn = document.getElementById('mobile-search-clear');
    var results = document.getElementById('mobile-search-results');

    var NUDGE_STORAGE_DONE = 'jm_search_nudge_done';
    var NUDGE_STORAGE_COUNT = 'jm_search_nudge_count';

    function readSearchCount() {
      try {
        return parseInt(localStorage.getItem(NUDGE_STORAGE_COUNT) || '0', 10) || 0;
      } catch (e) {
        return 0;
      }
    }

    function writeSearchCount(count) {
      try {
        localStorage.setItem(NUDGE_STORAGE_COUNT, String(count));
      } catch (e) {}
    }

    function isSearchDone() {
      try {
        return localStorage.getItem(NUDGE_STORAGE_DONE) === '1'
          || localStorage.getItem('jm_search_nudge_seen') === '1';
      } catch (e) {
        return false;
      }
    }

    function markSearchDone() {
      searchStopped = true;
      if (nudge) {
        nudge.classList.remove('is-active');
        nudge.classList.add('is-done');
      }
      try {
        localStorage.setItem(NUDGE_STORAGE_DONE, '1');
      } catch (e) {}
    }

    function schedule(fn, delay) {
      var id = window.setTimeout(fn, Math.max(0, delay));
      timers.push(id);
      return id;
    }

    function runExclusive(activate, deactivate) {
      queue = queue.then(function () {
        return new Promise(function (resolve) {
          function start() {
            var now = Date.now();
            var wait = Math.max(0, freeAt - now);
            schedule(function () {
              freeAt = Date.now() + SHOW_MS + MIN_GAP_MS;
              activate();
              schedule(function () {
                deactivate();
                resolve();
              }, SHOW_MS);
            }, wait);
          }
          start();
        });
      });
      return queue;
    }

    function requestSearchPulse() {
      if (searchStopped || !nudge || isSearchDone()) return;
      var count = readSearchCount();
      if (count >= SEARCH_MAX) {
        markSearchDone();
        return;
      }
      runExclusive(
        function () {
          if (searchStopped || isSearchDone()) return;
          var next = readSearchCount() + 1;
          writeSearchCount(next);
          nudge.classList.add('is-active');
          if (next >= SEARCH_MAX) {
            schedule(markSearchDone, SHOW_MS + 40);
          }
        },
        function () {
          if (nudge) nudge.classList.remove('is-active');
        }
      );
    }

    function scheduleSearchPulses() {
      if (!nudge || isSearchDone()) {
        if (nudge) nudge.classList.add('is-done');
        searchStopped = true;
        return;
      }
      var count = readSearchCount();
      if (count >= SEARCH_MAX) {
        markSearchDone();
        return;
      }
      var remaining = SEARCH_MAX - count;
      for (var i = 0; i < remaining; i += 1) {
        schedule(requestSearchPulse, SEARCH_FIRST_MS + (i * SEARCH_GAP_MS));
      }
    }

    // Mobile search open/close wiring (kept with attention effects)
    if (overlay && openBtn && input) {
      function openSearch() {
        overlay.classList.add('is-open');
        overlay.setAttribute('aria-hidden', 'false');
        document.body.classList.add('mobile-search-open');
        openBtn.setAttribute('aria-expanded', 'true');
        markSearchDone();
        window.setTimeout(function () {
          input.focus({ preventScroll: true });
        }, 120);
      }

      function closeSearch() {
        overlay.classList.remove('is-open');
        overlay.setAttribute('aria-hidden', 'true');
        document.body.classList.remove('mobile-search-open');
        openBtn.setAttribute('aria-expanded', 'false');
        input.blur();
      }

      function syncClearButton() {
        if (!clearBtn) return;
        clearBtn.hidden = !input.value.trim();
      }

      openBtn.addEventListener('click', openSearch);
      overlay.querySelectorAll('[data-search-dismiss]').forEach(function (el) {
        el.addEventListener('click', closeSearch);
      });
      document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape' && overlay.classList.contains('is-open')) {
          closeSearch();
        }
      });
      input.addEventListener('input', function () {
        syncClearButton();
        if (!input.value.trim() && results) results.innerHTML = '';
      });
      if (clearBtn) {
        clearBtn.addEventListener('click', function () {
          input.value = '';
          if (results) results.innerHTML = '';
          syncClearButton();
          input.focus({ preventScroll: true });
        });
      }
      document.body.addEventListener('htmx:afterSwap', function (event) {
        if (event.detail.target && event.detail.target.id === 'mobile-search-results') {
          syncClearButton();
        }
      });
    }

    scheduleSearchPulses();
  })();

  document.querySelectorAll('.product-rail-scroll, .chip-scroll').forEach(function (rail) {
    var isDown = false;
    var startX;
    var scrollLeft;
    rail.addEventListener('mousedown', function (e) {
      isDown = true;
      startX = e.pageX - rail.offsetLeft;
      scrollLeft = rail.scrollLeft;
      rail.style.cursor = 'grabbing';
    });
    rail.addEventListener('mouseleave', function () { isDown = false; rail.style.cursor = ''; });
    rail.addEventListener('mouseup', function () { isDown = false; rail.style.cursor = ''; });
    rail.addEventListener('mousemove', function (e) {
      if (!isDown) return;
      e.preventDefault();
      var x = e.pageX - rail.offsetLeft;
      rail.scrollLeft = scrollLeft - (x - startX) * 1.5;
    });
  });
})();

// Featured brands rail: arrow scrolling + auto-hide disabled state
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.brand-rail').forEach((rail) => {
    const track = rail.querySelector('[data-brand-track]');
    const prevBtn = rail.querySelector('[data-brand-scroll="prev"]');
    const nextBtn = rail.querySelector('[data-brand-scroll="next"]');
    if (!track || !prevBtn || !nextBtn) return;

    const scrollByCard = (dir) => {
      const card = track.querySelector('.brand-card');
      const gap = 20;
      const distance = card ? card.offsetWidth + gap : 220;
      track.scrollBy({ left: dir * distance * 2, behavior: 'smooth' });
    };

    prevBtn.addEventListener('click', () => scrollByCard(-1));
    nextBtn.addEventListener('click', () => scrollByCard(1));

    const updateArrowState = () => {
      const maxScroll = track.scrollWidth - track.clientWidth - 1;
      prevBtn.disabled = track.scrollLeft <= 0;
      nextBtn.disabled = track.scrollLeft >= maxScroll || maxScroll <= 0;
    };

    track.addEventListener('scroll', updateArrowState, { passive: true });
    window.addEventListener('resize', updateArrowState);
    updateArrowState();
  });
});
/* Desert Star: product rail arrows, quick view, wishlist, search category */
(function () {
  'use strict';

  function initRailArrows(root) {
    (root || document).querySelectorAll('.jm-featured__rail').forEach(function (rail) {
      if (rail.dataset.jmRailReady === '1') return;
      var track = rail.querySelector('[data-rail-track]');
      var prevBtn = rail.querySelector('[data-rail-scroll="prev"]');
      var nextBtn = rail.querySelector('[data-rail-scroll="next"]');
      if (!track || !prevBtn || !nextBtn) return;
      rail.dataset.jmRailReady = '1';

      function scrollByDir(dir) {
        var styles = window.getComputedStyle(track);
        var gap = parseFloat(styles.columnGap || styles.gap) || 16;
        var item = track.querySelector('.product-rail-item');
        var colWidth = item ? item.getBoundingClientRect().width : 240;
        track.scrollBy({ left: dir * (colWidth + gap) * 2, behavior: 'smooth' });
      }

      function updateState() {
        var maxScroll = track.scrollWidth - track.clientWidth - 1;
        prevBtn.disabled = track.scrollLeft <= 0;
        nextBtn.disabled = track.scrollLeft >= maxScroll || maxScroll <= 0;
      }

      prevBtn.addEventListener('click', function () { scrollByDir(-1); });
      nextBtn.addEventListener('click', function () { scrollByDir(1); });
      track.addEventListener('scroll', updateState, { passive: true });
      window.addEventListener('resize', updateState);
      updateState();
    });
  }

  function openQuickView(card) {
    var modalEl = document.getElementById('jmQuickViewModal');
    if (!modalEl || !window.bootstrap) return;
    var img = document.getElementById('jm-qv-image');
    var title = document.getElementById('jmQuickViewTitle');
    var price = document.getElementById('jm-qv-price');
    var stock = document.getElementById('jm-qv-stock');
    var badge = document.getElementById('jm-qv-badge');
    var pdp = document.getElementById('jm-qv-pdp');
    var productId = document.getElementById('jm-qv-product-id');

    if (img) {
      img.src = card.dataset.productImage || '';
      img.alt = card.dataset.productName || '';
    }
    if (title) title.textContent = card.dataset.productName || '';
    if (price) price.textContent = card.dataset.productPrice || '';
    if (stock) stock.textContent = card.dataset.productStock || '';
    if (badge) {
      if (card.dataset.productBadge) {
        badge.hidden = false;
        badge.textContent = card.dataset.productBadge;
      } else {
        badge.hidden = true;
      }
    }
    if (pdp) pdp.href = card.dataset.productUrl || '#';
    if (productId) productId.value = card.dataset.productId || '';

    var inCart = card.dataset.inCart === 'true';
    var qvCartForm = document.getElementById('jm-qv-cart');
    var qvViewCart = document.getElementById('jm-qv-view-cart');
    if (qvCartForm && qvViewCart) {
      if (inCart) {
        qvCartForm.classList.add('d-none');
        qvViewCart.classList.remove('d-none');
      } else {
        qvCartForm.classList.remove('d-none');
        qvViewCart.classList.add('d-none');
      }
    }

    bootstrap.Modal.getOrCreateInstance(modalEl).show();
  }

  function setWishFilled(svg, filled) {
    if (!svg) return;
    svg.setAttribute('fill', filled ? 'currentColor' : 'none');
  }

  function syncWishlistChrome(detail) {
    var added = !!(detail && detail.added);
    var productId = detail && detail.product_id != null ? String(detail.product_id) : '';
    if (!productId) return;

    document.querySelectorAll('.jm-product-card__wish').forEach(function (btn) {
      var form = btn.closest('form');
      var input = form ? form.querySelector('input[name="product_id"]') : null;
      if (!input || String(input.value) !== productId) return;
      btn.classList.toggle('is-active', added);
      btn.setAttribute('aria-pressed', added ? 'true' : 'false');
      setWishFilled(btn.querySelector('svg'), added);
    });

    document.querySelectorAll('[data-jm-wish-btn]').forEach(function (btn) {
      if (String(btn.getAttribute('data-product-id') || '') !== productId) return;
      btn.classList.toggle('is-active', added);
      btn.setAttribute('aria-pressed', added ? 'true' : 'false');
      setWishFilled(btn.querySelector('svg'), added);
    });
  }

  function initSearchCategoryDropdown() {
    var root = document.querySelector('.jm-search__cat');
    if (!root || root.dataset.jmCatReady === '1') return;
    root.dataset.jmCatReady = '1';

    var hidden = document.getElementById('jm-search-category');
    var label = root.querySelector('.jm-search__cat-label');
    var items = root.querySelectorAll('[data-jm-search-cat]');
    var toggle = root.querySelector('[data-bs-toggle="dropdown"]');

    items.forEach(function (item) {
      item.addEventListener('click', function (event) {
        event.preventDefault();
        var value = item.getAttribute('value') || '';
        var text = (item.textContent || '').trim();
        if (hidden) hidden.value = value;
        if (label) label.textContent = text;
        items.forEach(function (el) {
          var active = el === item;
          el.classList.toggle('is-active', active);
          el.setAttribute('aria-selected', active ? 'true' : 'false');
        });
        if (toggle && window.bootstrap && bootstrap.Dropdown) {
          var instance = bootstrap.Dropdown.getInstance(toggle);
          if (instance) instance.hide();
        }
      });
    });
  }

  document.addEventListener('click', function (event) {
    var qvBtn = event.target.closest('[data-jm-quick-view]');
    if (qvBtn) {
      var card = qvBtn.closest('.jm-product-card');
      if (card) openQuickView(card);
    }
  });

  document.addEventListener('DOMContentLoaded', function () {
    initRailArrows(document);
    initSearchCategoryDropdown();
  });

  document.body.addEventListener('htmx:afterSwap', function () {
    initRailArrows(document);
  });

  document.body.addEventListener('wishlistUpdated', function (event) {
    var detail = event.detail || {};
    syncWishlistChrome(detail);

    if (detail.added === false && window.location.pathname.indexOf('/wishlist') !== -1) {
      var productId = detail.product_id;
      if (productId) {
        document.querySelectorAll('.jm-product-card__wish').forEach(function (btn) {
          var form = btn.closest('form');
          var input = form ? form.querySelector('input[name="product_id"]') : null;
          if (input && String(input.value) === String(productId)) {
            var gridItem = btn.closest('.product-grid-item');
            if (gridItem) {
              gridItem.style.transition = 'opacity 0.3s ease';
              gridItem.style.opacity = '0';
              setTimeout(function() {
                gridItem.remove();
                var gridInner = document.querySelector('.product-grid-inner');
                if (gridInner && gridInner.children.length === 0) {
                  window.location.reload();
                }
              }, 300);
            }
          }
        });
      }
    }
  });

  function setProductCardCartState(productId, inCart) {
    document.querySelectorAll('.jm-product-card[data-product-id="' + productId + '"]').forEach(function (card) {
      card.dataset.inCart = inCart ? 'true' : 'false';
      var form = card.querySelector('form.jm-product-card__cart');
      var viewCartLink = card.querySelector('.jm-dynamic-view-cart');

      if (inCart) {
        if (form && !form.classList.contains('d-none')) {
          var newLink = document.createElement('a');
          var cartUrl = '/cart/';
          var existingLink = document.querySelector('a[href*="/cart/"]');
          if (existingLink) cartUrl = existingLink.getAttribute('href');

          newLink.href = cartUrl;
          newLink.className = 'btn btn-outline-floward jm-product-card__atc jm-product-card__atc--in-cart mt-auto jm-dynamic-view-cart';
          newLink.innerHTML = '<span>View Cart</span>';

          form.classList.add('d-none');
          if (viewCartLink) viewCartLink.replaceWith(newLink);
          else form.parentNode.insertBefore(newLink, form.nextSibling);
        }
      } else {
        if (viewCartLink) viewCartLink.remove();
        if (form) form.classList.remove('d-none');
      }
    });

    var qvProductId = document.getElementById('jm-qv-product-id');
    if (qvProductId && qvProductId.value === productId) {
      var qvCartForm = document.getElementById('jm-qv-cart');
      var qvViewCart = document.getElementById('jm-qv-view-cart');
      if (qvCartForm && qvViewCart) {
        if (inCart) {
          qvCartForm.classList.add('d-none');
          qvViewCart.classList.remove('d-none');
        } else {
          qvCartForm.classList.remove('d-none');
          qvViewCart.classList.add('d-none');
        }
      }
    }
  }

  document.body.addEventListener('cartItemAdded', function (event) {
    var pid = (event.detail && event.detail.product_id) ? String(event.detail.product_id) : '';
    if (pid) setProductCardCartState(pid, true);
  });

  document.body.addEventListener('cartItemRemoved', function (event) {
    var pid = (event.detail && event.detail.product_id) ? String(event.detail.product_id) : '';
    if (pid) setProductCardCartState(pid, false);
  });

  var lastSync = 0;
  function syncCartStatus() {
    if (Date.now() - lastSync < 300) return;
    lastSync = Date.now();

    fetch('/cart/status/', { headers: { 'X-Requested-With': 'XMLHttpRequest' }, cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || !Array.isArray(data.cart_product_ids)) return;
        var cartSet = new Set(data.cart_product_ids.map(String));
        document.querySelectorAll('.jm-product-card').forEach(function (card) {
          if (card.dataset.productId) setProductCardCartState(String(card.dataset.productId), cartSet.has(String(card.dataset.productId)));
        });

        var badge = document.getElementById('cart-count-badge');
        if (badge && data.cart_count !== undefined) {
          badge.innerHTML = data.cart_count > 0 ? '<span class="badge-count">' + data.cart_count + '</span>' : '';
        }

        var pdpForm = document.getElementById('buy-form');
        if (pdpForm) {
          var pid = (pdpForm.querySelector('input[name="product_id"]') || {}).value;
          var vid = (pdpForm.querySelector('input[name="variant_id"]') || {}).value;
          if (pid) {
            var itemKey = vid ? (pid + '_' + vid) : pid;
            var inPdp = Array.isArray(data.cart_item_keys) ? data.cart_item_keys.indexOf(itemKey) !== -1 : cartSet.has(pid);
            var addGrp = document.getElementById('pdp-add-to-cart-group');
            var viewGrp = document.getElementById('pdp-view-cart-group');
            var stickyAdd = document.getElementById('sticky-buy-form');
            var stickyView = document.getElementById('pdp-sticky-view-cart-btn');
            if (addGrp) addGrp.classList.toggle('d-none', inPdp);
            if (viewGrp) viewGrp.classList.toggle('d-none', !inPdp);
            if (stickyAdd) stickyAdd.classList.toggle('d-none', inPdp);
            if (stickyView) stickyView.classList.toggle('d-none', !inPdp);
          }
        }
      }).catch(function () {});
  }

  window.addEventListener('pageshow', function (e) { syncCartStatus(); if (e.persisted) window.location.reload(); });
  window.addEventListener('focus', syncCartStatus);
  document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'visible') syncCartStatus(); });
})();

/* Desert Star: sticky header offset + mobile trust auto-slide */
(function () {
  'use strict';

  function syncHeaderStickyOffset() {
    var header = document.querySelector('.site-header.jm-header');
    if (!header) return;
    var height = Math.ceil(header.getBoundingClientRect().height);
    if (height > 0) {
      document.documentElement.style.setProperty('--jm-header-sticky-offset', height + 'px');
    }
  }

  function initTrustAutoSlide(root) {
    var track = (root || document).querySelector('[data-jm-trust-track]');
    if (!track || track.dataset.jmTrustReady === '1') return;
    track.dataset.jmTrustReady = '1';

    var timer = null;
    var paused = false;
    var mq = window.matchMedia('(max-width: 991.98px)');

    function step() {
      if (paused || !mq.matches) return;
      var item = track.querySelector('.jm-trust__item');
      if (!item) return;
      var gap = 8;
      var distance = item.getBoundingClientRect().width + gap;
      var maxScroll = track.scrollWidth - track.clientWidth - 2;
      if (maxScroll <= 0) return;
      if (track.scrollLeft >= maxScroll) {
        track.scrollTo({ left: 0, behavior: 'smooth' });
      } else {
        track.scrollBy({ left: distance, behavior: 'smooth' });
      }
    }

    function start() {
      stop();
      if (!mq.matches) return;
      timer = window.setInterval(step, 3200);
    }

    function stop() {
      if (timer) {
        window.clearInterval(timer);
        timer = null;
      }
    }

    ['pointerdown', 'touchstart', 'mouseenter', 'focusin'].forEach(function (evt) {
      track.addEventListener(evt, function () { paused = true; }, { passive: true });
    });
    ['pointerup', 'touchend', 'mouseleave', 'focusout'].forEach(function (evt) {
      track.addEventListener(evt, function () { paused = false; }, { passive: true });
    });

    if (typeof mq.addEventListener === 'function') {
      mq.addEventListener('change', start);
    } else if (typeof mq.addListener === 'function') {
      mq.addListener(start);
    }

    start();
  }

  document.addEventListener('DOMContentLoaded', function () {
    syncHeaderStickyOffset();
    initTrustAutoSlide(document);
  });

  window.addEventListener('resize', syncHeaderStickyOffset);
  window.addEventListener('load', syncHeaderStickyOffset);

  document.addEventListener('click', function (event) {
    var link = event.target.closest('[data-jm-scroll-target]');
    if (!link) return;
    var id = link.getAttribute('data-jm-scroll-target');
    var target = id ? document.getElementById(id) : null;
    if (!target) return;
    event.preventDefault();
    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    if (history.replaceState) {
      history.replaceState(null, '', '#' + id);
    }
  });
})();
