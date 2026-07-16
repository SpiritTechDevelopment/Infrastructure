# Privacy disclosure

## Current state

Operational connection logging is **enabled** in the Loki `ops` tenant with 30-day
retention. Xray and nginx access output may include IP addresses, destinations and other
connection metadata depending on upstream log format. Per-user traffic accounting stores
a backend-defined pseudonymous identifier and aggregate uplink/downlink volume.

The repository does not create a separate customer-activity datastore and does not put
runtime user UUIDs or API request payloads into logs intentionally. Operators must review
local legal/disclosure requirements before customer use and reduce retention or masking
when the operational test phase ends.
