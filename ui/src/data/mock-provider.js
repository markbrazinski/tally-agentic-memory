// MockProvider — explicitly synthetic public film seeds plus the generated
// log-line preview. The component reads them through the provider interface;
// none of these records or query-log lines represents live execution.
//
// ponytail: no separate interface file for one implementation — add one
// when LiveProvider (B1FE-S3) exists to conform to it. Until then this
// JSDoc is the interface: whatever object shape createMockProvider()
// returns is what Component's constructor (index.html) expects on
// window.__tallyProvider.
//
// @returns {{
//   getCases: () => object[],
//   getCredits: () => object[],
//   getPaidSeed: () => object[],
//   getNotPressedSeed: () => object[],
//   getConduct: () => object[],
//   getEvalRun: () => object,
//   getTariffCapture: () => object,
//   getHeroCaseId: () => string,
//   getCommitLog: (clockMs: number) => object[],
//   getClockMode: () => 'film'|'live',
//   getDisclosure: () => {label: string, detail: string, tone: string},
//   getInitialClock: () => number,
//   getRecordingStart: () => number,
// }}
export function createMockProvider() {
  const ts = (s) => Date.parse(s.length > 10 ? s.replace(' ', 'T') + ':00Z' : s + 'T00:00:00Z');
  const DAY = 86400000;
  const REC = ts('2026-07-03');
  const T0 = ts('2026-07-21 17:00');

  // checksum, not a hash — matches Component.h(), duplicated here because
  // commitLog() (the Law-2 fenced generator) needs it and must live in the
  // mock-only module, never in the component or a live provider.
  const h = (s) => {
    let x = 7;
    for (let i = 0; i < s.length; i++) x = (x * 31 + s.charCodeAt(i)) >>> 0;
    return x.toString(16).padStart(8, '0');
  };

  // ============ SEED (§6) — verbatim from the export ============
  const inv = [];
  const add = (o) => {
    o.aT = ts(o.arrived);
    o.cT = o.checked ? ts(o.checked) : null;
    o.sT = o.sealed ? ts(o.sealed) : null;
    o.kT = o.contest ? ts(o.contest) : null;
    o.rT = o.resolved ? ts(o.resolved) : null;
    inv.push(o);
    return o;
  };
  const pat = (id, cont, invno, days, arrived, checked, sealed) => add({
    id, container: cont, invoiceNo: invno, carrier: 'Fictional Northstar Lines', port: 'Demo Northport',
    verdict: 'over', kind: 'rate', pattern: 'PTN-0007', days, rateApplied: 350, rateRecorded: 250,
    amount: days * 350, dispute: days * 100, defect: 'Recorded $250/day · billed $350/day · ' + days + ' days',
    arrived, checked, sealed,
  });

  const hero = pat('CASE-0142', 'NOLU 8834127', 'INV-NOL-084213', 7, '2026-07-08 16:31', '2026-07-08 16:32', '2026-07-08 16:42');
  Object.assign(hero, { contest: '2026-07-21 09:14', resolved: null, kT: ts('2026-07-21 09:14'), rT: null, freeStart: '2026-07-02', chargeStart: '2026-07-04', chargeEnd: '2026-07-06', invoiced: '2026-07-07', runSecs: 4.8 });

  add({ id: 'CASE-0186', container: 'BHMU 2209481', invoiceNo: 'INV-BHM-771204', carrier: 'Fictional Bluehaven Lines', port: 'Demo Northport', verdict: 'owe0', kind: 'late', amount: 1880, dispute: 1880, defect: 'Issued day 34 of a 30-day window — §541.7', arrived: '2026-07-21 06:12', checked: '2026-07-21 06:13' });
  add({ id: 'CASE-0187', container: 'HRZU 5521763', invoiceNo: 'INV-HRZ-448291', carrier: 'Fictional Horizon Lines', port: 'Demo Southport', verdict: 'owe0', kind: 'missing', amount: 1320, dispute: 1320, defect: 'Availability date missing — field 9 of 13, §541.6', arrived: '2026-07-21 07:48', checked: '2026-07-21 07:49' });

  pat('CASE-0178', 'NOLU 7741208', 'INV-NOL-085114', 8, '2026-07-20 13:02', '2026-07-20 13:11', null);
  pat('CASE-0179', 'NOLU 7742551', 'INV-NOL-085126', 9, '2026-07-20 13:18', '2026-07-20 13:27', null);
  pat('CASE-0180', 'NOLU 7743092', 'INV-NOL-085131', 5, '2026-07-20 13:34', '2026-07-20 13:43', null);
  pat('CASE-0181', 'NOLU 7744317', 'INV-NOL-085140', 6, '2026-07-20 13:50', '2026-07-20 13:58', null);
  pat('CASE-0182', 'NOLU 7745640', 'INV-NOL-085152', 3, '2026-07-20 14:05', '2026-07-20 14:13', null);

  const vseed = [
    ['CASE-0169', 'BHMU 3320981', 'INV-BHM-770642', 'Fictional Bluehaven Lines', 1450, '2026-07-20 09:05'],
    ['CASE-0170', 'NOLU 7708113', 'INV-NOL-084877', 'Fictional Northstar Lines', 980, '2026-07-20 09:40'],
    ['CASE-0171', 'NOLU 7709224', 'INV-NOL-084892', 'Fictional Northstar Lines', 2100, '2026-07-20 10:12'],
    ['CASE-0172', 'BHMU 3321562', 'INV-BHM-770688', 'Fictional Bluehaven Lines', 760, '2026-07-20 10:44'],
    ['CASE-0173', 'NOLU 7710308', 'INV-NOL-084910', 'Fictional Northstar Lines', 1890, '2026-07-20 11:20'],
    ['CASE-0174', 'BHMU 3322047', 'INV-BHM-770701', 'Fictional Bluehaven Lines', 1240, '2026-07-20 12:02'],
    ['CASE-0175', 'NOLU 7711482', 'INV-NOL-084926', 'Fictional Northstar Lines', 830, '2026-07-21 08:21'],
    ['CASE-0176', 'BHMU 3322590', 'INV-BHM-770719', 'Fictional Bluehaven Lines', 1520, '2026-07-21 08:22'],
    ['CASE-0177', 'NOLU 7712516', 'INV-NOL-084931', 'Fictional Northstar Lines', 570, '2026-07-21 08:24'],
  ];
  vseed.forEach(v => add({ id: v[0], container: v[1], invoiceNo: v[2], carrier: v[3], port: 'Demo Northport', verdict: 'valid', kind: 'valid', amount: v[4], dispute: 0, defect: 'All four tiers pass — rate matches the recorded tariff', arrived: v[5], checked: v[5].slice(0, 14) + String(parseInt(v[5].slice(14), 10) + 1).padStart(2, '0') }));

  add({ id: 'CASE-0168', container: 'CRMU 6612940', invoiceNo: 'INV-CRM-220148', carrier: 'Fictional Crescent Lines', port: 'Demo Westhaven', verdict: 'unver', kind: 'unver', amount: 980, dispute: 0, defect: 'Tiers 1–2 passed · no recorded tariff at Demo Westhaven', arrived: '2026-07-20 13:55', checked: '2026-07-20 13:58' });

  pat('CASE-0198', 'NOLU 7746112', 'INV-NOL-085201', 4, '2026-07-21 16:59', null, null);
  pat('CASE-0199', 'NOLU 7747335', 'INV-NOL-085207', 4, '2026-07-21 16:59', null, null);
  pat('CASE-0200', 'NOLU 7748560', 'INV-NOL-085212', 6, '2026-07-21 16:59', null, null);

  const fseed = [['CASE-0154', 'NOLU 7721804', 'INV-NOL-084618', 5, '2026-07-19 10:14'], ['CASE-0155', 'NOLU 7722919', 'INV-NOL-084622', 7, '2026-07-19 10:15'], ['CASE-0156', 'NOLU 7723350', 'INV-NOL-084640', 5, '2026-07-19 09:02'], ['CASE-0157', 'NOLU 7724466', 'INV-NOL-084655', 4, '2026-07-19 09:03'], ['CASE-0158', 'NOLU 7725581', 'INV-NOL-084661', 6, '2026-07-20 11:40'], ['CASE-0159', 'NOLU 7726697', 'INV-NOL-084678', 5, '2026-07-20 11:41'], ['CASE-0160', 'NOLU 7727712', 'INV-NOL-084685', 3, '2026-07-20 08:55'], ['CASE-0161', 'NOLU 7728828', 'INV-NOL-084697', 6, '2026-07-20 08:56']];
  fseed.forEach(f => { const day = f[4].slice(0, 10); pat(f[0], f[1], f[2], f[3], day.slice(0, 8) + String(parseInt(day.slice(8), 10) - 2).padStart(2, '0') + ' 09:00', day.slice(0, 8) + String(parseInt(day.slice(8), 10) - 2).padStart(2, '0') + ' 09:05', f[4]); });

  const useed = [
    ['CASE-0118', 'HRZU 4410872', 'INV-HRZ-447210', 'Fictional Horizon Lines', 340, 940, 'Weekend scenario — illustrative, business-day arithmetic not evaluated', '2026-07-09 11:20'],
    ['CASE-0121', 'SBSU 5583021', 'INV-SBS-662311', 'Fictional Seabrook Lines', 290, 1180, 'Holiday scenario — illustrative, business-day arithmetic not evaluated', '2026-07-10 09:41'],
    ['CASE-0124', 'BHMU 3318740', 'INV-BHM-770204', 'Fictional Bluehaven Lines', 410, 1760, 'Per-diem tier scenario — synthetic illustration', '2026-07-11 14:03'],
    ['CASE-0127', 'NOLU 7702218', 'INV-NOL-084101', 'Fictional Northstar Lines', 260, 1010, 'Free-time/LFD scenario — illustrative, not evaluated', '2026-07-12 10:16'],
    ['CASE-0130', 'CRMU 6540118', 'INV-CRM-219660', 'Fictional Crescent Lines', 380, 1420, 'Availability-gap scenario — synthetic illustration', '2026-07-14 15:32'],
    ['CASE-0132', 'HRZU 4412304', 'INV-HRZ-447385', 'Fictional Horizon Lines', 310, 880, 'Rate-step scenario — synthetic illustration', '2026-07-15 09:12'],
    ['CASE-0135', 'SBSU 5584772', 'INV-SBS-662490', 'Fictional Seabrook Lines', 220, 1050, 'Duplicate-day scenario — synthetic illustration', '2026-07-16 13:48'],
    ['CASE-0138', 'BHMU 3319655', 'INV-BHM-770311', 'Fictional Bluehaven Lines', 350, 990, 'LFD scenario — illustrative, business-day arithmetic not evaluated', '2026-07-17 10:29'],
    ['CASE-0140', 'NOLU 7704431', 'INV-NOL-084177', 'Fictional Northstar Lines', 270, 830, 'Chassis-day scenario — synthetic illustration', '2026-07-18 16:07'],
  ];
  useed.forEach(u => add({ id: u[0], container: u[1], invoiceNo: u[2], carrier: u[3], port: 'Demo Northport', verdict: 'over', kind: 'small', amount: u[5], dispute: u[4], defect: u[6], arrived: u[7].slice(0, 10) + ' 08:00', checked: u[7].slice(0, 10) + ' 08:05', sealed: u[7] }));

  // No externally confirmed carrier credit was executed in recovery.
  const credits = [];
  const paidSeed = [
    { id: 'CASE-0129', container: 'NOLU 7703110', carrier: 'Fictional Northstar Lines', amount: 1050, at: '2026-07-13 10:20', note: 'Valid — all four tiers passed; rate matches the recorded tariff' },
  ];
  const notPressedSeed = [
    { id: 'CASE-0134', container: 'CRMU 6538871', carrier: 'Fictional Crescent Lines', amount: 240, at: '2026-07-16 15:02', by: 'Avery Quill (fictional)', reason: 'Customer-controlled delay — ops confirmed the hold was ours' },
  ];
  // No committed conduct harness exists, so no ratios are synthesized.
  const conduct = [];
  // No public harness result is bundled with the film. The System view must
  // stay unavailable until it receives an actual computed result.
  const evalRun = null;
  const tariffCapture = { at: ts('2026-07-04 08:00'), atS: '2026-07-04 08:00', rate: 250, carrier: 'Fictional Northstar Lines', lane: 'DEMO-NP–SP' };

  // Law 2 fence: the only place fake commit-log lines are synthesized.
  // Live mode must never call this (enforced by ComponentImpl, not here).
  function getCommitLog(clock) {
    const out = [];
    const fD = (t) => new Date(t).toISOString().slice(0, 10);
    for (let t = REC; t <= clock; t += DAY) {
      const d = fD(t);
      const push = (hm, txt, target) => {
        const at = Date.parse(d + 'T' + hm + ':00Z');
        if (at <= clock) out.push({ at, line: d + ' ' + hm + ' UTC  ' + txt, hash: h(d + hm + txt).slice(0, 7), target: !!target });
      };
      push('08:00', 'synthetic tariff snapshot — Fictional Northstar Lines · DEMO-NP-SP' + (d === '2026-07-04' ? ' · demurrage $250/day recorded' : ''), d === '2026-07-04');
      push('08:01', 'synthetic tariff snapshot — Fictional Bluehaven Lines · DEMO-NP–SP');
      push('14:36', 'synthetic terminal availability — Fictional Pier Alpha · Fictional Terminal Beta');
    }
    return out;
  }

  return {
    getCases: () => inv,
    getCredits: () => credits,
    getPaidSeed: () => paidSeed,
    getNotPressedSeed: () => notPressedSeed,
    getConduct: () => conduct,
    getEvalRun: () => evalRun,
    getTariffCapture: () => tariffCapture,
    getHeroCaseId: () => hero.id,
    getCommitLog,
    getClockMode: () => 'film',
    getDisclosure: () => ({
      label: 'SYNTHETIC FILM — NOT LIVE · FICTIONAL DATA',
      detail: 'Every entity, identifier, event, query, and outcome is synthetic. Dates are fictional scenario dates, not runtime or MVCC timestamps. Amounts outside the locked harness are illustrative inputs, not recovered money.',
      tone: 'synthetic',
    }),
    getInitialClock: () => T0,
    getRecordingStart: () => REC,
  };
}
