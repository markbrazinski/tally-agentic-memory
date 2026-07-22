# Independent Gate 0 verification (sanitized)

An independent verifier evaluated the private evidence snapshot and returned
`PASS WITH LIMITATIONS`; every Gate 0 acceptance criterion passed.

Verified independently:

- complete schema and application-state export existed privately;
- 51 current object versions were exact-version/hash checked, including at
  least three samples across different days;
- isolated replay produced 51 recordings and 51 tariff snapshots with zero
  semantic differences;
- the second replay was idempotent;
- scheduled capture and verifier jobs were enabled;
- cluster plan and billing state were documented privately;
- the public artifact set contains no substitute for the retained private
  evidence.

Accepted limitations:

- superseded versions were not enumerated due to missing list-version
  permission;
- billing values were operator-recorded;
- delete protection/object lock was not enabled;
- only capture-derived application rows are reconstructed by replay.

## Public-safe branch verification

A second independent verifier reviewed the orphan publication candidate. It
confirmed the branch had no parent, executed 21 targeted Gate 0 tests and the
full suites, and compared the candidate against private evidence values without
printing them. Two reused private-export UUIDs were found, replaced with
explicit fictional fixture UUIDs, and rescanned successfully.

Final independent publication checks:

- Python: 245 passed, one deprecation warning;
- UI: 15 passed;
- private-export UUID matches: zero;
- generic credential classes: zero;
- exact account IDs, ARNs, bucket, object keys, Version IDs, production hashes,
  source URLs, carrier names/SCACs, connection metadata, cluster name/ID, and
  AWS resource-name matches: zero;
- apparent remaining broad-token matches were field-classified as generic
  status/metric terminology, not prohibited values.

Publication classification: `PASS` subject only to the already accepted Gate 0
recoverability limitations above.
