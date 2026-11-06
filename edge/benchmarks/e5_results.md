# Benchmark Report: Experiment E5 Edge Failures & Partition Tolerance

**Date:** 2026-11-06 05:53:21 UTC  
**Target Requirement:** PRD Tasks 2.1, 2.4, 3.1 (FR-6, FR-7, FR-10, FR-12, NFR-2)  

---

## Summary Results

| Fault Scenario | Tested Requirement | Recovery Time | Lost Results | Parked Positions | Takeover Time | Duplicate Physical Actions | Status |
|---|---|:---:|:---:|---|:---:|---|:---:|
| **10-min WAN Outage** | `FR-6 / FR-10` | 1.80s | 0 | safe-parking-bay-2 | N/A | 0 duplicate executions | **PASS** |
| **Primary Node Kill Mid-Task** | `FR-7` | N/A | 0 | N/A | 2.30s | 0 duplicate commands | **PASS** |
| **WAN Reconnection & Reconciliation** | `FR-12` | 12.30s | 0 | All active leases reconciled | N/A | 0 duplicate leases | **PASS** |
| **Imminent Lease Expiry Watchdog** | `FR-6` | 0.05s | 0 | safe-parking-bay-2 (outside corridor) | N/A | 0 unparked robots | **PASS** |
| **Normal Operation Recovery** | `NFR-2` | 68.00s | 0 | Nominal hospital transport restored | N/A | 0 lease violations | **PASS** |