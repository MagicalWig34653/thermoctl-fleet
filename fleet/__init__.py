"""Fleet service: the cloud side (docs/specification.md, section 2).

Receives heartbeats and events, hands out commands over an SSE stream, holds
desired states. Sees **no** room temperatures, setpoints, or tenant data (section
6) and cannot control the heating (section 1) -- that is not an implementation
detail, it is the scope this repository implements.
"""
