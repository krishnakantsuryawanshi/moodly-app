function setInstallHint(message) {
  document.querySelectorAll("[data-install-hint]").forEach((node) => {
    node.textContent = message;
  });
}

function setInstallSize(message) {
  document.querySelectorAll("[data-install-size]").forEach((node) => {
    node.textContent = message;
  });
}

async function clearLegacyPwaState() {
  if ("serviceWorker" in navigator) {
    try {
      const registrations = await navigator.serviceWorker.getRegistrations();
      await Promise.all(registrations.map((registration) => registration.unregister()));
    } catch (_) {
      // Ignore cleanup failures.
    }
  }

  if ("caches" in window) {
    try {
      const keys = await caches.keys();
      await Promise.all(keys.map((key) => caches.delete(key)));
    } catch (_) {
      // Ignore cleanup failures.
    }
  }
}

window.addEventListener("load", () => {
  clearLegacyPwaState();
  setInstallSize("App size: browser mode");
  setInstallHint("Install mode is disabled right now to keep login and signup stable.");

  document.querySelectorAll("[data-install-app]").forEach((button) => {
    button.hidden = true;
  });
});
