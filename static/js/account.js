const phoneInput = document.querySelector("[data-phone-input]");

if (phoneInput) {
    const invalidPhoneCharacters = /[^0-9+().\-\s]/g;

    phoneInput.addEventListener("beforeinput", (event) => {
        if (event.data && invalidPhoneCharacters.test(event.data)) {
            event.preventDefault();
        }
        invalidPhoneCharacters.lastIndex = 0;
    });

    phoneInput.addEventListener("input", () => {
        phoneInput.value = phoneInput.value.replace(invalidPhoneCharacters, "");
    });
}

const accountForm = document.querySelector(".account-form");
const provinceSelect = accountForm?.querySelector("[data-province-select]");
const wardSelect = accountForm?.querySelector("[data-ward-select]");
const addressStatus = accountForm?.querySelector("[data-address-status]");
const profileSubmit = accountForm?.querySelector("[data-profile-submit]");
let wardsRequest;

function updateSubmitState() {
    profileSubmit.disabled = wardSelect.disabled || !accountForm.checkValidity();
}

function replaceWardOptions(options, placeholder) {
    wardSelect.replaceChildren(new Option(placeholder, ""));
    for (const ward of options) {
        wardSelect.add(new Option(ward.name, ward.code));
    }
}

if (accountForm && provinceSelect && wardSelect && addressStatus && profileSubmit) {
    accountForm.addEventListener("input", updateSubmitState);
    accountForm.addEventListener("change", updateSubmitState);
    updateSubmitState();

    provinceSelect.addEventListener("change", async () => {
        wardsRequest?.abort();
        wardsRequest = new AbortController();
        addressStatus.textContent = "";

        if (!provinceSelect.value) {
            replaceWardOptions([], accountForm.dataset.wardPlaceholder);
            wardSelect.disabled = true;
            updateSubmitState();
            return;
        }

        wardSelect.disabled = true;
        replaceWardOptions([], accountForm.dataset.wardLoading);
        updateSubmitState();

        try {
            const url = new URL(accountForm.dataset.wardsUrl, window.location.origin);
            url.searchParams.set("province", provinceSelect.value);
            const response = await fetch(url, {
                headers: { Accept: "application/json" },
                signal: wardsRequest.signal,
            });
            if (!response.ok) {
                throw new Error("Unable to load wards");
            }

            const payload = await response.json();
            replaceWardOptions(payload.data, accountForm.dataset.wardPlaceholder);
            wardSelect.disabled = false;
            updateSubmitState();
        } catch (error) {
            if (error.name === "AbortError") {
                return;
            }
            replaceWardOptions([], accountForm.dataset.wardPlaceholder);
            addressStatus.textContent = accountForm.dataset.wardError;
            updateSubmitState();
        }
    });
}
