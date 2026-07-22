// Data-provider interface (B1FE-S3). Promoted from a JSDoc-only comment on
// mock-provider.js (B1FE-S2) now that a second implementation exists —
// per that session's own note: "add one when LiveProvider exists."
//
// @typedef {Object} DataProvider
// @property {() => object[]} getCases
// @property {() => object[]} getCredits
// @property {() => object[]} getPaidSeed
// @property {() => object[]} getNotPressedSeed
// @property {() => object[]} getConduct
// @property {() => object} getEvalRun
// @property {() => object} getTariffCapture
// @property {() => string} getHeroCaseId
// @property {(clockMs: number) => object[]} getCommitLog - LAW 2 FENCE: only
//   MockProvider may implement this with real output. LiveProvider's
//   implementation throws (see live-provider.js) — live mode must never
//   render a synthesized log line, only feed-delivered ones.
// @property {() => 'film'|'live'} getClockMode
// @property {() => {label: string, detail: string, tone: string}} getDisclosure
// @property {() => number} getInitialClock
// @property {() => number} getRecordingStart
//
// @property {(onChange: () => void) => void} onDataChange - registers a
//   callback the provider invokes after its backing data changes (e.g. a
//   fetch resolves, a feed event arrives). MockProvider's data never
//   changes on its own, so its onChange is never called — Component must
//   not assume onDataChange fires at all. This is the mechanism that
//   replaces "await load() before constructing state": Component mounts
//   and renders immediately with whatever getCases()/etc. return right
//   now (empty for a fresh LiveProvider), then re-renders when notified.
//
// The logged-out public provider adds only replayCase for its already-loaded,
// server-selected projection. It deliberately has no generic case lookup,
// mutation, bearer-authentication, or feed capability.
//
// @property {(localHeroHandle: string) => Promise<object>} replayCase
export {};
