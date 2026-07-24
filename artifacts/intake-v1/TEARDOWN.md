# Intake v1 isolated teardown

This checklist applies only to the isolated Intake v1 deployment recorded in
the operator's private execution record. It does not contain live
infrastructure identifiers, credentials, object bindings, or database
connection details.

## Safety boundary

- Resolve every exact target from the retained private provisioning record.
- Confirm each target carries the isolated Intake v1 environment tag before
  changing it.
- Stop if a target is shared with the existing hero, judged routing, or any
  retained lineage.
- Obtain explicit destructive-operation approval immediately before deletion.
- Capture a private before/after readback for every target class.
- Do not treat an absent list result as proof when the read itself failed.

## Ordered teardown

1. Stop and remove the isolated application service after its required
   execution evidence has been retained.
2. Remove the isolated container-image repository after confirming that no
   retained service references it.
3. Remove only the isolated runtime configuration parameters.
4. Detach inline policies and remove only the isolated runtime and image-access
   roles.
5. Remove only the isolated CockroachDB database after preserving the
   public-safe aggregate execution result and the required private audit
   record.
6. Remove all versions and delete markers beneath only the isolated object
   prefix. Confirm that no retained source binding outside Intake v1 references
   those objects.

## Required readback

Record the following privately, without copying identifiers into this
repository:

- application service absent;
- container-image repository absent;
- isolated configuration parameter set empty;
- isolated IAM roles absent;
- isolated CockroachDB database absent;
- isolated object prefix has zero current versions and zero delete markers;
- existing hero service, database, lineage, object set, and judged routing
  remain present and unchanged.

## Current state

The isolated application service is **PAUSED** after evidence capture. Resource
deletion is **NOT EXECUTED**. This document is a controlled checklist, not proof
of deletion. Every destructive step remains separately approval-gated.
