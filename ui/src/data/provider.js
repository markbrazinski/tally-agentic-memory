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
// Methods below this line exist only on LiveProvider — MockProvider has no
// backend to fetch from or seal against, so it doesn't implement them.
// Component code must check `typeof this.provider.fetchCase === 'function'`
// (or branch on getClockMode() === 'live') before calling them.
//
// @property {(id: string) => Promise<object>} fetchCase - GET /cases/{id}
// @property {(id: string) => Promise<object>} replayCase - GET /cases/{id}/replay
// @property {(caseId: string) => Promise<object>} approveCase - POST /cases/{id}/approve
// @property {(onEvent: (frame: {event: string, ts: string, payload: object}) => void) => () => void} subscribeFeed -
//   opens the WS feed, calls onEvent per frame, returns an unsubscribe function
export {};
