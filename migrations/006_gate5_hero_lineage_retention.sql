-- Gate 5 recovery: retain every table touched by the executed hero lineage
-- for at least the same 90-day window as the temporal replay join.
--
-- These settings preserve future MVCC versions only. They do not and cannot
-- restore the expired original hero lineage.

ALTER TABLE tenants CONFIGURE ZONE USING gc.ttlseconds = 7776000;
ALTER TABLE users CONFIGURE ZONE USING gc.ttlseconds = 7776000;
ALTER TABLE carriers CONFIGURE ZONE USING gc.ttlseconds = 7776000;
ALTER TABLE invoices CONFIGURE ZONE USING gc.ttlseconds = 7776000;
ALTER TABLE clerk_runs CONFIGURE ZONE USING gc.ttlseconds = 7776000;
ALTER TABLE findings CONFIGURE ZONE USING gc.ttlseconds = 7776000;
ALTER TABLE ledger_events CONFIGURE ZONE USING gc.ttlseconds = 7776000;
ALTER TABLE contests CONFIGURE ZONE USING gc.ttlseconds = 7776000;
ALTER TABLE query_log CONFIGURE ZONE USING gc.ttlseconds = 7776000;
