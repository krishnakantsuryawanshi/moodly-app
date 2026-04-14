let deferredInstallPrompt = null;

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {
      // Ignore registration failures in unsupported or restricted environments.
    });
  });
}

window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  deferredInstallPrompt = event;
  document.querySelectorAll("[data-install-app]").forEach((button) => {
    button.hidden = false;
    button.disabled = false;
  });
});

window.addEventListener("appinstalled", () => {
  deferredInstallPrompt = null;
  document.querySelectorAll("[data-install-app]").forEach((button) => {
    button.textContent = "Installed";
    button.disabled = true;
  });
});

window.addEventListener("load", () => {
  document.querySelectorAll("[data-install-app]").forEach((button) => {
    button.hidden = false;
    button.addEventListener("click", async () => {
      if (deferredInstallPrompt) {
        deferredInstallPrompt.prompt();
        await deferredInstallPrompt.userChoice.catch(() => null);
        deferredInstallPrompt = null;
        return;
      }

      const isAppleDevice = /iphone|ipad|mac/i.test(window.navigator.userAgent);
      window.alert(
        isAppleDevice
          ? "Browser menu kholkar 'Add to Home Screen' use karo."
          : "Agar install prompt na aaye to browser menu se 'Install app' ya 'Create shortcut' use karo."
      );
    });
  });
});
