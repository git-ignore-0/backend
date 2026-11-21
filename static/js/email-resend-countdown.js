const resendButton = document.querySelector("[data-email-resend-seconds]");

if (resendButton) {
    const countdown = resendButton.querySelector("[data-email-resend-countdown]");
    const deadline = performance.now() + Number(resendButton.dataset.emailResendSeconds) * 1000;

    const timer = setInterval(() => {
        const remaining = Math.ceil((deadline - performance.now()) / 1000);
        if (remaining <= 0) {
            clearInterval(timer);
            countdown.remove();
            resendButton.disabled = false;
            return;
        }
        countdown.textContent = `(${remaining}s)`;
    }, 250);
}
