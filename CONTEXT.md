# AirCommand

A wifi auditing tool that orchestrates existing security tools (aircrack-ng, hashcat, nmap, and others) behind a unified interface, restricting active operations to networks the user is explicitly authorized to test.

## Language

**Network**:
A wifi access point observed during discovery, identified by its BSSID, SSID, and channel. Any Network may be discovered; not every Network is a Target.
_Avoid_: AP, access point (use only when referring to 802.11 hardware concepts, not this domain's tracked entity)

**Target**:
A Network that appears on the authorization allowlist and is therefore eligible to have gated actions performed against it.
_Avoid_: authorized network, whitelisted network

**Discovery**:
Passively observing beacon broadcasts to identify Networks (BSSID, SSID, channel, encryption type, signal strength). Open to any Network; requires no authorization.
_Avoid_: scanning (use only for the underlying nmap/802.11 mechanism, not this domain concept)

**Action**:
Any operation gated to Targets only: capturing traffic tied to a Network (passive or active), transmitting frames at a Network (e.g. deauth), or probing hosts on a Target's subnet (Enumerate). Requires the Network to be a Target.
_Avoid_: attack (too narrow — Action also covers passive capture, which isn't an attack)

**Capture**:
An Action that records 802.11 traffic tied to a Target, such as a Handshake. Always gated, regardless of whether it involved transmitting (e.g. deauth) or was purely passive.

**Handshake**:
The WPA/WPA2/WPA3 authentication exchange captured from a Target, used as input to offline cracking. Cracking inherits its authorization from the Target the Handshake was captured from — it is not separately gated.

**Enumerate**:
An Action that probes hosts on a Target's subnet for open ports and services, via nmap. Depends on the operator having already associated the adapter to the Target's network through their OS's normal wifi settings — AirCommand does not manage that association itself.
_Avoid_: scan, port scan (use only for the underlying nmap mechanism, not this domain concept — same reasoning as Discovery's own _Avoid_ note)
