import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import { basePath } from "./lib/base-path";
import "./index.css";

// Installable as a home-screen app (the picks page is built for a phone). The manifest and worker
// live at the served root, which is the base path when there is one — the shell's own links are
// rewritten by the server, but these are added here, so they take the prefix here.
const manifest = document.createElement("link");
manifest.rel = "manifest";
manifest.href = `${basePath}/manifest.webmanifest`;
document.head.appendChild(manifest);
if ("serviceWorker" in navigator && !import.meta.env.DEV) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register(`${basePath}/sw.js`, { scope: `${basePath}/` }).catch(() => {});
  });
}

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("Root element #root is missing from index.html");
}

createRoot(rootElement).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
