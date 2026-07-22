import { createLiveProvider } from "./live-provider.js";
import { createMockProvider } from "./mock-provider.js";

function createUnavailableProvider(reason) {
  const now = Date.now();
  return {
    getCases: () => [],
    getCredits: () => [],
    getPaidSeed: () => [],
    getNotPressedSeed: () => [],
    getConduct: () => [],
    getEvalRun: () => null,
    getTariffCapture: () => ({ at: null, atS: null, rate: null, carrier: null, lane: null }),
    getHeroCaseId: () => null,
    getCommitLog: () => {
      throw new Error("UnavailableProvider.getCommitLog: no synthetic log may be rendered");
    },
    // The existing component's live fences also provide the safe empty-state
    // behavior for an unavailable live request. This provider performs no I/O.
    getClockMode: () => "live",
    getInitialClock: () => now,
    getRecordingStart: () => now,
    getDisclosure: () => ({
      label: "LIVE UNAVAILABLE — NO MOCK FALLBACK",
      detail: reason,
      tone: "error",
    }),
  };
}

export function selectProvider(
  { wantsFilm = false } = {},
  { mockFactory = createMockProvider, liveFactory = createLiveProvider } = {},
) {
  if (wantsFilm) return mockFactory();
  return liveFactory();
}

export { createUnavailableProvider };
