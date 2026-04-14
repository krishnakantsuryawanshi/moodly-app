const INSTALL_ASSET_PATHS = [
  "/",
  "/login",
  "/register",
  "/static/style.css",
  "/static/app.js",
  "/static/sw.js",
  "/static/manifest.json",
  "/static/uploads/moodly-app-icon-192.png",
  "/static/uploads/moodly-app-icon-512.png",
];

let deferredInstallPrompt = null;

function formatMegabytes(totalBytes) {
  return `${(totalBytes / (1024 * 1024)).toFixed(2)} MB`;
}

function isIosDevice() {
  return /iphone|ipad|ipod/i.test(window.navigator.userAgent);
}

function isAndroidDevice() {
  return /android/i.test(window.navigator.userAgent);
}

function isStandaloneMode() {
  return window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
}

function setInstallButtonState({ label, disabled = false }) {
  document.querySelectorAll("[data-install-app]").forEach((button) => {
    button.hidden = false;
    button.disabled = disabled;
    button.textContent = label;
  });
}

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

async function updateInstallSize() {
  try {
    const responses = await Promise.all(
      INSTALL_ASSET_PATHS.map((path) => fetch(path, { cache: "no-store" }))
    );

    const blobs = await Promise.all(
      responses.map(async (response) => {
        if (!response.ok) {
          return 0;
        }
        const blob = await response.blob();
        return blob.size;
      })
    );

    const totalBytes = blobs.reduce((sum, size) => sum + size, 0);
    setInstallSize(`App size: approx ${formatMegabytes(totalBytes)}`);
  } catch (_) {
    setInstallSize("App size: lightweight web app");
  }
}

function updateInstallInstructions() {
  if (isStandaloneMode()) {
    setInstallButtonState({ label: "Installed", disabled: true });
    setInstallHint("Moodly is already installed on this device.");
    return;
  }

  if (deferredInstallPrompt) {
    setInstallButtonState({ label: "Install App" });
    setInstallHint("Tap install to add Moodly directly to your home screen or desktop.");
    return;
  }

  if (isIosDevice()) {
    setInstallButtonState({ label: "How to Install" });
    setInstallHint("On iPhone/iPad, open Share and tap Add to Home Screen.");
    return;
  }

  if (isAndroidDevice()) {
    setInstallButtonState({ label: "Install from Browser" });
    setInstallHint("If the popup does not appear, open the browser menu and tap Install app.");
    return;
  }

  setInstallButtonState({ label: "Install from Browser" });
  setInstallHint("If the popup does not appear, use the browser menu and choose Install app or Create shortcut.");
}

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
  updateInstallInstructions();
});

window.addEventListener("appinstalled", () => {
  deferredInstallPrompt = null;
  setInstallButtonState({ label: "Installed", disabled: true });
  setInstallHint("Moodly has been added to this device.");
});

window.addEventListener("load", () => {
  updateInstallSize();
  updateInstallInstructions();

  document.querySelectorAll("[data-install-app]").forEach((button) => {
    button.hidden = false;
    button.addEventListener("click", async () => {
      if (deferredInstallPrompt) {
        deferredInstallPrompt.prompt();
        await deferredInstallPrompt.userChoice.catch(() => null);
        deferredInstallPrompt = null;
        updateInstallInstructions();
        return;
      }

      if (isIosDevice()) {
        window.alert("iPhone/iPad me Share button kholo aur 'Add to Home Screen' tap karo.");
        return;
      }

      if (isAndroidDevice()) {
        window.alert("Chrome ya Edge ke menu me jaakar 'Install app' ya 'Add to Home screen' use karo.");
        return;
      }

      window.alert("Browser menu se 'Install app' ya 'Create shortcut' use karo.");
    });
  });
});
