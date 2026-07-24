// Chooses the data provider. Default = live (real backend). Force the mock with
// ?provider=mock in the URL or VITE_PROVIDER=mock — used for offline design/dev.
// The live provider never silently falls back to the mock.
import { createLiveProvider } from "./liveProvider.js";
import { createMockProvider } from "./mockProvider.js";

export function selectProvider() {
  let choice = import.meta.env.VITE_PROVIDER || "live";
  if (typeof window !== "undefined") {
    const q = new URLSearchParams(window.location.search).get("provider");
    if (q) choice = q;
  }
  return choice === "mock" ? createMockProvider() : createLiveProvider();
}
