(function () {
  var variantGroup = document.getElementById('variant-select-group');
  var variantRadios = document.querySelectorAll('.variant-radio');
  if (variantGroup && variantRadios.length > 0) {
    variantRadios.forEach(function (radio) {
      radio.addEventListener('change', function () {
        var url = variantGroup.getAttribute('data-price-url');
        var vid = this.value;

        document.querySelectorAll('.pdp-variant-id-input').forEach(function (input) {
          input.value = vid;
        });

        var qtyInput = document.getElementById('pdp-qty');
        var qty = qtyInput ? qtyInput.value : '1';
        var queryString = '?quantity=' + qty + (vid ? '&variant_id=' + vid : '');

        fetch(url + queryString)
          .then(function (r) { return r.json(); })
          .then(function (data) {
            var el = document.getElementById('pdp-price');
            var elSticky = document.getElementById('pdp-sticky-price');
            var retailContainer = document.getElementById('pdp-retail-price-container');
            var symbol = variantGroup.getAttribute('data-currency-symbol') || '';
            var elPriceValue = document.getElementById('pdp-price-value');

            if (elPriceValue) {
              elPriceValue.textContent = symbol + ' ' + parseFloat(data.price).toFixed(2).replace(/\.00$/, '');
            }

            var elMrp = document.getElementById('pdp-price-mrp');
            if (elMrp) {
              if (data.is_flash_sale !== 'true' && data.has_mrp_discount === 'true' && data.mrp) {
                elMrp.textContent = symbol + ' ' + parseFloat(data.mrp).toFixed(2).replace(/\.00$/, '');
                elMrp.style.display = 'inline';
              } else {
                elMrp.style.display = 'none';
              }
            }

            if (retailContainer) {
              if (data.is_tier_active === 'true') {
                retailContainer.classList.remove("d-none");
              } else {
                retailContainer.classList.add("d-none");
              }
            }
            if (elSticky) elSticky.textContent = symbol + ' ' + parseFloat(data.price).toFixed(2).replace(/\.00$/, '');
            
            var stickyVariantName = document.getElementById('pdp-sticky-variant-name');
            if (stickyVariantName) {
              var checkedLabel = document.querySelector('label[for="variant_' + vid + '"]');
              if (checkedLabel) {
                 var nameText = checkedLabel.childNodes[0].textContent.trim();
                 stickyVariantName.textContent = nameText;
              }
            }

            var elRetail = document.getElementById('pdp-retail-price');
            if (elRetail && data.retail_price) {
              elRetail.textContent = symbol + ' ' + parseFloat(data.retail_price).toFixed(2).replace(/\.00$/, '');
            }
            var elRetailVal = document.getElementById('pdp-retail-price-val');
            if (elRetailVal && data.retail_price) {
              elRetailVal.value = data.retail_price;
            }

            if (data.is_in_cart !== undefined) {
              var addGroup = document.getElementById('pdp-add-to-cart-group');
              var viewGroup = document.getElementById('pdp-view-cart-group');
              var stickyAdd = document.getElementById('sticky-buy-form');
              var stickyView = document.getElementById('pdp-sticky-view-cart-btn');

              if (data.is_in_cart) {
                if (addGroup) addGroup.classList.add('d-none');
                if (viewGroup) viewGroup.classList.remove('d-none');
                if (stickyAdd) stickyAdd.classList.add('d-none');
                if (stickyView) stickyView.classList.remove('d-none');
              } else {
                if (addGroup) addGroup.classList.remove('d-none');
                if (viewGroup) viewGroup.classList.add('d-none');
                if (stickyAdd) stickyAdd.classList.remove('d-none');
                if (stickyView) stickyView.classList.add('d-none');
              }
            }

            if (data.is_in_stock !== undefined) {
              var atcBtn = document.querySelector('#buy-form button[type="submit"]');
              var bnForm = document.getElementById('buy-now-form');
              var qtyStepper = document.querySelector('.jm-pdp-qty');
              var stickyAtcBtn = document.querySelector('#sticky-buy-form button[type="submit"]');
              var stickyBnForm = document.getElementById('sticky-buy-now-form');

              if (data.is_in_stock) {
                if (atcBtn) {
                  atcBtn.type = 'submit';
                  atcBtn.classList.remove('disabled');
                  atcBtn.style.cursor = 'pointer';
                  atcBtn.style.opacity = '1';
                  atcBtn.style.pointerEvents = 'auto';
                  atcBtn.onclick = null;
                  atcBtn.textContent = 'Add to Cart';
                }
                if (stickyAtcBtn) {
                  stickyAtcBtn.type = 'submit';
                  stickyAtcBtn.classList.remove('disabled');
                  stickyAtcBtn.style.cursor = 'pointer';
                  stickyAtcBtn.style.opacity = '1';
                  stickyAtcBtn.style.pointerEvents = 'auto';
                  stickyAtcBtn.onclick = null;
                  stickyAtcBtn.textContent = 'Add';
                }
                if (bnForm) bnForm.classList.remove('d-none');
                if (stickyBnForm) stickyBnForm.classList.remove('d-none');
                if (qtyStepper) qtyStepper.style.opacity = '1';
              } else {
                if (atcBtn) {
                  atcBtn.type = 'button';
                  atcBtn.classList.add('disabled');
                  atcBtn.style.cursor = 'not-allowed';
                  atcBtn.style.opacity = '0.6';
                  atcBtn.style.pointerEvents = 'auto';
                  atcBtn.onclick = function (e) { e.preventDefault(); return false; };
                  atcBtn.textContent = 'Sold out';
                }
                if (stickyAtcBtn) {
                  stickyAtcBtn.type = 'button';
                  stickyAtcBtn.classList.add('disabled');
                  stickyAtcBtn.style.cursor = 'not-allowed';
                  stickyAtcBtn.style.opacity = '0.6';
                  stickyAtcBtn.style.pointerEvents = 'auto';
                  stickyAtcBtn.onclick = function (e) { e.preventDefault(); return false; };
                  stickyAtcBtn.textContent = 'Sold out';
                }
                if (bnForm) bnForm.classList.add('d-none');
                if (stickyBnForm) stickyBnForm.classList.add('d-none');
                if (qtyStepper) qtyStepper.style.opacity = '0.5';
              }

              var stockText = document.getElementById('pdp-stock-text');
              if (stockText) {
                if (data.is_in_stock) {
                  stockText.classList.remove('is-out');
                  if (data.stock_quantity !== undefined && data.low_stock_threshold !== undefined && data.stock_quantity <= data.low_stock_threshold) {
                    stockText.classList.remove('is-in');
                    stockText.classList.add('is-low');
                    stockText.innerHTML = 'Only ' + data.stock_quantity + ' left';
                  } else {
                    stockText.classList.remove('is-low');
                    stockText.classList.add('is-in');
                    stockText.innerHTML = 'In stock' + (data.stock_quantity ? ' &bull; ' + data.stock_quantity : '');
                  }
                } else {
                  stockText.classList.remove('is-in');
                  stockText.classList.remove('is-low');
                  stockText.classList.add('is-out');
                  stockText.textContent = 'Out of stock';
                }
              }
            }
          });
      });
    });

    var checkedRadio = document.querySelector('.variant-radio:checked');
    if (checkedRadio) {
      checkedRadio.dispatchEvent(new Event('change'));
    }
  }

  var citySelect = document.getElementById('delivery-city');
  if (citySelect) {
    citySelect.addEventListener('change', function () {
      if (!this.value) {
        return;
      }
      var url = this.getAttribute('data-estimate-url') + '?city=' + encodeURIComponent(this.value);
      fetch(url)
        .then(function (r) { return r.json(); })
        .then(function (data) {
          var el = document.getElementById('delivery-estimate-text');
          if (el) el.textContent = 'Delivery: ' + data.label + ' to ' + data.city;
        });
    });
  }
})();
