for (const input of document.querySelectorAll("input[data-phone-paste]")) {
    input.addEventListener("paste", (event) => {
        const text = event.clipboardData?.getData("text/plain") ?? "";
        if (!/[\r\n]/.test(text)) return;

        const { value, selectionStart, selectionEnd } = input;
        if (input.dataset.phonePaste === "login") {
            const next = value.slice(0, selectionStart) + text + value.slice(selectionEnd);
            if (next.includes("@")) return;
        }

        // Single-line inputs discard newlines. Keep them as spaces so validation rejects them.
        event.preventDefault();
        input.setRangeText(text.replace(/[\r\n]/g, " "), selectionStart, selectionEnd, "end");
    });
}
