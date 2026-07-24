import React from "react";
import { createRoot } from "react-dom/client";
import { selectProvider } from "./api/provider.js";
import Workbench from "./Workbench.jsx";
import "./index.css";

/* Reviewer props mirror the design's tweaks:
 *   scene: "live" | "recommendation" | "sendGate" | "sendBlocked" | "sent" | "insufficient" | "completed"
 *   reducedMotion: boolean
 *   internal: boolean (reveals ANNOTATIONS / REPLAY / HANDOFF)
 *
 * provider: the data source. Default = live (real backend via same-origin /api).
 * Force the offline demo with ?provider=mock. */
const provider = selectProvider();

createRoot(document.getElementById("root")).render(
  <Workbench scene="live" reducedMotion={false} internal={false} provider={provider} />
);
