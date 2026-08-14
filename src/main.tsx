import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App";
import { initUiScale } from "./uiScale";

initUiScale();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>
);

// First frame is mounted — fade out the inline pre-React loader (index.html)
// and drop it from the DOM once the transition ends.
const loader = document.getElementById("vodrip-loader");
if (loader) {
  requestAnimationFrame(() => loader.classList.add("vodrip-loader--done"));
  loader.addEventListener(
    "transitionend",
    () => loader.remove(),
    { once: true }
  );
  // Fallback: transitionend never fires if the tab is backgrounded mid-fade.
  setTimeout(() => loader.remove(), 600);
}
