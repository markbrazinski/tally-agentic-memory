CREATE TABLE public.smoke (
	"tenant" UUID NOT NULL,
	id UUID NOT NULL DEFAULT gen_random_uuid(),
	v STRING NULL,
	ts TIMESTAMPTZ NULL DEFAULT now():::TIMESTAMPTZ,
	emb VECTOR(1024) NULL,
	CONSTRAINT smoke_pkey PRIMARY KEY ("tenant" ASC, id ASC),
	VECTOR INDEX smoke_vec ("tenant", emb vector_l2_ops)
);
CREATE TABLE public.tenants (
	id UUID NOT NULL DEFAULT gen_random_uuid(),
	name STRING NOT NULL,
	created_at TIMESTAMPTZ NOT NULL DEFAULT now():::TIMESTAMPTZ,
	CONSTRAINT tenants_pkey PRIMARY KEY (id ASC)
);
CREATE TABLE public.carriers (
	tenant_id UUID NOT NULL,
	id UUID NOT NULL DEFAULT gen_random_uuid(),
	scac STRING NOT NULL,
	name STRING NOT NULL,
	date_format_hint STRING NULL,
	free_time_basis_default STRING NULL,
	lanes JSONB NOT NULL DEFAULT '[]':::JSONB,
	tariff_source_url STRING NULL,
	created_at TIMESTAMPTZ NOT NULL DEFAULT now():::TIMESTAMPTZ,
	CONSTRAINT carriers_pkey PRIMARY KEY (tenant_id ASC, id ASC),
	UNIQUE INDEX carriers_scac_idx (tenant_id ASC, scac ASC)
);
CREATE TABLE public.recordings (
	tenant_id UUID NOT NULL,
	id UUID NOT NULL DEFAULT gen_random_uuid(),
	run_date DATE NOT NULL,
	target STRING NOT NULL,
	carrier_id UUID NULL,
	lane STRING NULL,
	terminal_code STRING NULL,
	status STRING NOT NULL,
	rows_written INT8 NOT NULL DEFAULT 0:::INT8,
	s3_key STRING NULL,
	error STRING NULL,
	started_at TIMESTAMPTZ NOT NULL,
	committed_at TIMESTAMPTZ NULL,
	invocation STRING NOT NULL DEFAULT 'manual':::STRING,
	CONSTRAINT recordings_pkey PRIMARY KEY (tenant_id ASC, id ASC),
	UNIQUE INDEX recordings_day_idx (tenant_id ASC, run_date ASC, target ASC, carrier_id ASC, (COALESCE(lane, '':::STRING)) ASC, (COALESCE(terminal_code, '':::STRING)) ASC)
);
CREATE TABLE public.tariff_snapshots (
	tenant_id UUID NOT NULL,
	id UUID NOT NULL DEFAULT gen_random_uuid(),
	carrier_id UUID NOT NULL,
	lane STRING NOT NULL,
	version_label STRING NOT NULL,
	effective_date DATE NOT NULL,
	captured_at TIMESTAMPTZ NOT NULL,
	source_url STRING NOT NULL,
	s3_key STRING NOT NULL,
	doc_sha256 STRING NOT NULL,
	doc_text STRING NOT NULL,
	headline_rate DECIMAL(12,2) NULL,
	recording_id UUID NULL,
	committed_at TIMESTAMPTZ NOT NULL DEFAULT now():::TIMESTAMPTZ,
	embedding VECTOR(1024) NULL,
	CONSTRAINT tariff_snapshots_pkey PRIMARY KEY (tenant_id ASC, id ASC),
	UNIQUE INDEX tariff_snap_day_idx (tenant_id ASC, carrier_id ASC, lane ASC, captured_at ASC),
	INDEX tariff_snap_lookup_idx (tenant_id ASC, carrier_id ASC, lane ASC, effective_date DESC)
);
CREATE TABLE public.terminal_snapshots (
	tenant_id UUID NOT NULL,
	id UUID NOT NULL DEFAULT gen_random_uuid(),
	terminal_code STRING NOT NULL,
	captured_at TIMESTAMPTZ NOT NULL,
	gate_status JSONB NOT NULL DEFAULT '{}':::JSONB,
	appointment_availability JSONB NOT NULL DEFAULT '{}':::JSONB,
	empty_return_restrictions JSONB NOT NULL DEFAULT '{}':::JSONB,
	source STRING NOT NULL,
	s3_key STRING NULL,
	sha256 STRING NOT NULL,
	recording_id UUID NULL,
	committed_at TIMESTAMPTZ NOT NULL DEFAULT now():::TIMESTAMPTZ,
	CONSTRAINT terminal_snapshots_pkey PRIMARY KEY (tenant_id ASC, id ASC),
	UNIQUE INDEX terminal_snap_day_idx (tenant_id ASC, terminal_code ASC, captured_at ASC)
);
CREATE TABLE public.schema_migrations (
	filename STRING NOT NULL,
	applied_at TIMESTAMPTZ NOT NULL DEFAULT now():::TIMESTAMPTZ,
	CONSTRAINT schema_migrations_pkey PRIMARY KEY (filename ASC)
);
CREATE TABLE public.users (
	tenant_id UUID NOT NULL,
	id UUID NOT NULL DEFAULT gen_random_uuid(),
	email STRING NOT NULL,
	display_name STRING NOT NULL,
	title STRING NULL,
	"role" STRING NOT NULL DEFAULT 'viewer':::STRING,
	created_at TIMESTAMPTZ NOT NULL DEFAULT now():::TIMESTAMPTZ,
	CONSTRAINT users_pkey PRIMARY KEY (tenant_id ASC, id ASC),
	UNIQUE INDEX users_email_idx (tenant_id ASC, email ASC)
);
CREATE TABLE public.tariff_clauses (
	tenant_id UUID NOT NULL,
	id UUID NOT NULL DEFAULT gen_random_uuid(),
	carrier_id UUID NOT NULL,
	snapshot_id UUID NOT NULL,
	clause_ref STRING NOT NULL,
	clause_kind STRING NOT NULL,
	clause_text STRING NOT NULL,
	rate_amount DECIMAL(12,2) NULL,
	free_time_basis STRING NULL,
	sha256 STRING NOT NULL,
	embedding VECTOR(1024) NOT NULL,
	committed_at TIMESTAMPTZ NOT NULL DEFAULT now():::TIMESTAMPTZ,
	CONSTRAINT tariff_clauses_pkey PRIMARY KEY (tenant_id ASC, id ASC)
);
CREATE TABLE public.containers (
	tenant_id UUID NOT NULL,
	id UUID NOT NULL DEFAULT gen_random_uuid(),
	container_no STRING NOT NULL,
	carrier_id UUID NOT NULL,
	lane STRING NOT NULL,
	meta JSONB NOT NULL DEFAULT '{}':::JSONB,
	created_at TIMESTAMPTZ NOT NULL DEFAULT now():::TIMESTAMPTZ,
	CONSTRAINT containers_pkey PRIMARY KEY (tenant_id ASC, id ASC),
	UNIQUE INDEX containers_no_idx (tenant_id ASC, container_no ASC)
);
CREATE TABLE public.container_events (
	tenant_id UUID NOT NULL,
	id UUID NOT NULL DEFAULT gen_random_uuid(),
	container_id UUID NOT NULL,
	event_type STRING NOT NULL,
	occurred_at TIMESTAMPTZ NOT NULL,
	captured_at TIMESTAMPTZ NOT NULL,
	source STRING NOT NULL,
	details JSONB NOT NULL DEFAULT '{}':::JSONB,
	committed_at TIMESTAMPTZ NOT NULL DEFAULT now():::TIMESTAMPTZ,
	CONSTRAINT container_events_pkey PRIMARY KEY (tenant_id ASC, id ASC),
	INDEX ce_timeline_idx (tenant_id ASC, container_id ASC, occurred_at ASC)
);
CREATE TABLE public.invoices (
	tenant_id UUID NOT NULL,
	id UUID NOT NULL DEFAULT gen_random_uuid(),
	carrier_id UUID NOT NULL,
	container_id UUID NULL,
	invoice_no STRING NULL,
	received_at TIMESTAMPTZ NOT NULL,
	s3_key STRING NOT NULL,
	sha256 STRING NOT NULL,
	page_count INT8 NULL,
	is_image_only BOOL NOT NULL DEFAULT false,
	raw_text STRING NULL,
	extracted JSONB NULL,
	extraction_model STRING NULL,
	amount DECIMAL(12,2) NULL,
	currency STRING NULL DEFAULT 'USD':::STRING,
	invoice_date DATE NULL,
	status STRING NOT NULL DEFAULT 'RECEIVED':::STRING,
	created_at TIMESTAMPTZ NOT NULL DEFAULT now():::TIMESTAMPTZ,
	CONSTRAINT invoices_pkey PRIMARY KEY (tenant_id ASC, id ASC),
	UNIQUE INDEX invoices_sha256_idx (tenant_id ASC, sha256 ASC),
	INDEX invoices_status_idx (tenant_id ASC, status ASC, received_at ASC)
);
CREATE TABLE public.clerk_runs (
	tenant_id UUID NOT NULL,
	id UUID NOT NULL DEFAULT gen_random_uuid(),
	invoice_id UUID NOT NULL,
	status STRING NOT NULL DEFAULT 'QUEUED':::STRING,
	current_step INT8 NOT NULL DEFAULT 0:::INT8,
	steps JSONB NOT NULL DEFAULT '[]':::JSONB,
	error STRING NULL,
	started_at TIMESTAMPTZ NULL,
	finished_at TIMESTAMPTZ NULL,
	created_at TIMESTAMPTZ NOT NULL DEFAULT now():::TIMESTAMPTZ,
	CONSTRAINT clerk_runs_pkey PRIMARY KEY (tenant_id ASC, id ASC),
	INDEX clerk_runs_invoice_idx (tenant_id ASC, invoice_id ASC, created_at DESC)
);
CREATE TABLE public.findings (
	tenant_id UUID NOT NULL,
	id UUID NOT NULL DEFAULT gen_random_uuid(),
	invoice_id UUID NOT NULL,
	clerk_run_id UUID NOT NULL,
	verdict STRING NOT NULL,
	cited_rule STRING NULL,
	field_results JSONB NOT NULL,
	window_result JSONB NOT NULL,
	tariff_result JSONB NOT NULL,
	timeline_event_count INT8 NOT NULL DEFAULT 0:::INT8,
	summary STRING NOT NULL,
	amount_disputed DECIMAL(12,2) NULL,
	created_at TIMESTAMPTZ NOT NULL DEFAULT now():::TIMESTAMPTZ,
	CONSTRAINT findings_pkey PRIMARY KEY (tenant_id ASC, id ASC),
	UNIQUE INDEX findings_invoice_idx (tenant_id ASC, invoice_id ASC, clerk_run_id ASC)
);
CREATE TABLE public.cases (
	tenant_id UUID NOT NULL,
	id UUID NOT NULL DEFAULT gen_random_uuid(),
	invoice_id UUID NOT NULL,
	finding_id UUID NOT NULL,
	carrier_id UUID NOT NULL,
	state STRING NOT NULL DEFAULT 'ANALYZED':::STRING,
	pin_date DATE NOT NULL,
	draft_dispute STRING NOT NULL,
	amount DECIMAL(12,2) NOT NULL,
	sealed_at_display TIMESTAMPTZ NULL,
	sealed_txn_ts DECIMAL NULL,
	sealed_by UUID NULL,
	evidence_hash STRING NULL,
	created_at TIMESTAMPTZ NOT NULL DEFAULT now():::TIMESTAMPTZ,
	updated_at TIMESTAMPTZ NOT NULL DEFAULT now():::TIMESTAMPTZ,
	decision_reason STRING NULL,
	CONSTRAINT cases_pkey PRIMARY KEY (tenant_id ASC, id ASC),
	UNIQUE INDEX cases_invoice_idx (tenant_id ASC, invoice_id ASC),
	INDEX cases_record_idx (tenant_id ASC, pin_date ASC),
	INDEX cases_state_idx (tenant_id ASC, state ASC)
);
CREATE TABLE public.case_evidence (
	tenant_id UUID NOT NULL,
	id UUID NOT NULL DEFAULT gen_random_uuid(),
	case_id UUID NOT NULL,
	kind STRING NOT NULL,
	source_table STRING NOT NULL,
	source_id UUID NOT NULL,
	content JSONB NOT NULL,
	content_sha256 STRING NOT NULL,
	embedding_sha256 STRING NULL,
	captured_at_display TIMESTAMPTZ NULL,
	sealed BOOL NOT NULL DEFAULT false,
	created_at TIMESTAMPTZ NOT NULL DEFAULT now():::TIMESTAMPTZ,
	CONSTRAINT case_evidence_pkey PRIMARY KEY (tenant_id ASC, id ASC),
	INDEX ev_case_idx (tenant_id ASC, case_id ASC)
);
CREATE TABLE public.contests (
	tenant_id UUID NOT NULL,
	id UUID NOT NULL DEFAULT gen_random_uuid(),
	case_id UUID NOT NULL,
	carrier_id UUID NOT NULL,
	received_at TIMESTAMPTZ NOT NULL,
	sender STRING NOT NULL,
	claim_text STRING NOT NULL,
	claimed_rate DECIMAL(12,2) NULL,
	s3_key STRING NULL,
	status STRING NOT NULL DEFAULT 'OPEN':::STRING,
	rebuttal_text STRING NULL,
	rebuttal_sent_at TIMESTAMPTZ NULL,
	resolved_at TIMESTAMPTZ NULL,
	created_at TIMESTAMPTZ NOT NULL DEFAULT now():::TIMESTAMPTZ,
	CONSTRAINT contests_pkey PRIMARY KEY (tenant_id ASC, id ASC),
	INDEX contests_open_idx (tenant_id ASC, status ASC, received_at ASC)
);
CREATE TABLE public.ledger_events (
	tenant_id UUID NOT NULL,
	id UUID NOT NULL DEFAULT gen_random_uuid(),
	case_id UUID NOT NULL,
	carrier_id UUID NOT NULL,
	kind STRING NOT NULL,
	amount DECIMAL(12,2) NULL,
	occurred_on DATE NOT NULL,
	details JSONB NOT NULL DEFAULT '{}':::JSONB,
	created_at TIMESTAMPTZ NOT NULL DEFAULT now():::TIMESTAMPTZ,
	CONSTRAINT ledger_events_pkey PRIMARY KEY (tenant_id ASC, id ASC),
	INDEX ledger_day_idx (tenant_id ASC, occurred_on ASC),
	INDEX ledger_carrier_idx (tenant_id ASC, carrier_id ASC, kind ASC)
);
CREATE TABLE public.query_log (
	tenant_id UUID NOT NULL,
	id UUID NOT NULL DEFAULT gen_random_uuid(),
	ts TIMESTAMPTZ NOT NULL DEFAULT now():::TIMESTAMPTZ,
	kind STRING NOT NULL,
	tag STRING NOT NULL,
	sql_text STRING NULL,
	elapsed_ms INT8 NULL,
	row_count INT8 NULL,
	render_source STRING NOT NULL DEFAULT 'live':::STRING,
	actor STRING NULL,
	ok BOOL NOT NULL DEFAULT true,
	error STRING NULL,
	CONSTRAINT query_log_pkey PRIMARY KEY (tenant_id ASC, id ASC),
	INDEX query_log_ts_idx (tenant_id ASC, ts DESC)
) WITH (ttl = 'on', ttl_expiration_expression = e'ts + INTERVAL \'30 days\'', ttl_job_cron = '@daily');
CREATE TABLE public.eval_runs (
	tenant_id UUID NOT NULL,
	id UUID NOT NULL DEFAULT gen_random_uuid(),
	git_sha STRING NOT NULL,
	started_at TIMESTAMPTZ NOT NULL,
	finished_at TIMESTAMPTZ NULL,
	invoices_total INT8 NOT NULL DEFAULT 0:::INT8,
	invoices_passed INT8 NOT NULL DEFAULT 0:::INT8,
	assertions_total INT8 NOT NULL DEFAULT 0:::INT8,
	assertions_passed INT8 NOT NULL DEFAULT 0:::INT8,
	report_s3_key STRING NULL,
	CONSTRAINT eval_runs_pkey PRIMARY KEY (tenant_id ASC, id ASC)
);
CREATE TABLE public.eval_results (
	tenant_id UUID NOT NULL,
	id UUID NOT NULL DEFAULT gen_random_uuid(),
	run_id UUID NOT NULL,
	invoice_id UUID NOT NULL,
	archetype STRING NOT NULL,
	assertion STRING NOT NULL,
	expected STRING NOT NULL,
	actual STRING NOT NULL,
	passed BOOL NOT NULL,
	CONSTRAINT eval_results_pkey PRIMARY KEY (tenant_id ASC, id ASC),
	INDEX eval_results_run_idx (tenant_id ASC, run_id ASC, passed ASC)
);
ALTER TABLE public.carriers ADD CONSTRAINT carriers_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
ALTER TABLE public.recordings ADD CONSTRAINT recordings_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
ALTER TABLE public.tariff_snapshots ADD CONSTRAINT tariff_snap_fk FOREIGN KEY (tenant_id, carrier_id) REFERENCES public.carriers(tenant_id, id);
ALTER TABLE public.tariff_snapshots ADD CONSTRAINT tariff_snapshots_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
ALTER TABLE public.terminal_snapshots ADD CONSTRAINT terminal_snapshots_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
ALTER TABLE public.users ADD CONSTRAINT users_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
ALTER TABLE public.tariff_clauses ADD CONSTRAINT clause_snap_fk FOREIGN KEY (tenant_id, snapshot_id) REFERENCES public.tariff_snapshots(tenant_id, id);
ALTER TABLE public.tariff_clauses ADD CONSTRAINT tariff_clauses_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
ALTER TABLE public.containers ADD CONSTRAINT containers_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
ALTER TABLE public.container_events ADD CONSTRAINT ce_container_fk FOREIGN KEY (tenant_id, container_id) REFERENCES public.containers(tenant_id, id);
ALTER TABLE public.container_events ADD CONSTRAINT container_events_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
ALTER TABLE public.invoices ADD CONSTRAINT invoices_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
ALTER TABLE public.clerk_runs ADD CONSTRAINT clerk_runs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
ALTER TABLE public.findings ADD CONSTRAINT findings_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
ALTER TABLE public.cases ADD CONSTRAINT cases_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
ALTER TABLE public.case_evidence ADD CONSTRAINT ev_case_fk FOREIGN KEY (tenant_id, case_id) REFERENCES public.cases(tenant_id, id);
ALTER TABLE public.case_evidence ADD CONSTRAINT case_evidence_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
ALTER TABLE public.contests ADD CONSTRAINT contest_case_fk FOREIGN KEY (tenant_id, case_id) REFERENCES public.cases(tenant_id, id);
ALTER TABLE public.contests ADD CONSTRAINT contests_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
ALTER TABLE public.ledger_events ADD CONSTRAINT ledger_events_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
-- Validate foreign key constraints. These can fail if there was unvalidated data during the SHOW CREATE ALL TABLES
ALTER TABLE public.carriers VALIDATE CONSTRAINT carriers_tenant_id_fkey;
ALTER TABLE public.recordings VALIDATE CONSTRAINT recordings_tenant_id_fkey;
ALTER TABLE public.tariff_snapshots VALIDATE CONSTRAINT tariff_snap_fk;
ALTER TABLE public.tariff_snapshots VALIDATE CONSTRAINT tariff_snapshots_tenant_id_fkey;
ALTER TABLE public.terminal_snapshots VALIDATE CONSTRAINT terminal_snapshots_tenant_id_fkey;
ALTER TABLE public.users VALIDATE CONSTRAINT users_tenant_id_fkey;
ALTER TABLE public.tariff_clauses VALIDATE CONSTRAINT clause_snap_fk;
ALTER TABLE public.tariff_clauses VALIDATE CONSTRAINT tariff_clauses_tenant_id_fkey;
ALTER TABLE public.containers VALIDATE CONSTRAINT containers_tenant_id_fkey;
ALTER TABLE public.container_events VALIDATE CONSTRAINT ce_container_fk;
ALTER TABLE public.container_events VALIDATE CONSTRAINT container_events_tenant_id_fkey;
ALTER TABLE public.invoices VALIDATE CONSTRAINT invoices_tenant_id_fkey;
ALTER TABLE public.clerk_runs VALIDATE CONSTRAINT clerk_runs_tenant_id_fkey;
ALTER TABLE public.findings VALIDATE CONSTRAINT findings_tenant_id_fkey;
ALTER TABLE public.cases VALIDATE CONSTRAINT cases_tenant_id_fkey;
ALTER TABLE public.case_evidence VALIDATE CONSTRAINT ev_case_fk;
ALTER TABLE public.case_evidence VALIDATE CONSTRAINT case_evidence_tenant_id_fkey;
ALTER TABLE public.contests VALIDATE CONSTRAINT contest_case_fk;
ALTER TABLE public.contests VALIDATE CONSTRAINT contests_tenant_id_fkey;
ALTER TABLE public.ledger_events VALIDATE CONSTRAINT ledger_events_tenant_id_fkey;
