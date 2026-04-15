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

async function copyText(value) {
  if (!value) return;

  try {
    await navigator.clipboard.writeText(value);
    window.alert("Copied.");
  } catch (_) {
    window.prompt("Copy this text:", value);
  }
}

async function shareUrl(url, title) {
  if (!url) return;

  if (navigator.share) {
    try {
      await navigator.share({ title: title || "Moodly", url });
      return;
    } catch (_) {
      // Fall back to copy if share is dismissed or unavailable.
    }
  }

  await copyText(url);
}

document.addEventListener("click", (event) => {
  const copyTrigger = event.target.closest("[data-copy-text]");
  if (copyTrigger) {
    copyText(copyTrigger.getAttribute("data-copy-text"));
    return;
  }

  const shareTrigger = event.target.closest("[data-share-url]");
  if (shareTrigger) {
    shareUrl(
      shareTrigger.getAttribute("data-share-url"),
      shareTrigger.getAttribute("data-share-title"),
    );
  }
});
