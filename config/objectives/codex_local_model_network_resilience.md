# Objective: Local + Hosted Network Resilience

Increase operational resilience and throughput for local-network and hosted model lanes by implementing:
- endpoint health/load-aware local routing,
- durable queue ingestion and offline execution continuity,
- backlog recycle controls for idle periods,
- stronger process supervision,
- safer task payload handling.

Success criteria:
1. Runner can ingest queued tasks from persistent queue files and continue when coordinator is offline.
2. Idle guard can start lanes based on queue depth and recycle backlog tasks when direct work is absent.
3. Local endpoint selection avoids repeatedly failing endpoints and distributes load across healthy candidates.
4. Dynamic token/context controls align with discovered endpoint capacity.
5. Validation gates pass.
