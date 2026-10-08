# AirCommand

A wifi auditing tool that orchestrates existing security tools (aircrack-ng, hashcat, nmap, and others) behind a unified interface, restricting active operations to networks the user is explicitly authorized to test.

## Language

**Network**:
A wifi access point observed during discovery, identified by its BSSID, SSID, and channel. Any Network may be discovered; not every Network is a Target.
_Avoid_: AP, access point (use only when referring to 802.11 hardware concepts, not this domain's tracked entity)

**Target**:
A Network that appears on the authorization allowlist and is therefore eligible to have gated actions performed against it. Authorization is per BSSID: a router that broadcasts on both Bands is two Networks, and each needs its own Target.
_Avoid_: authorized network, whitelisted network

**Discovery**:
Passively observing beacon broadcasts to identify Networks (BSSID, SSID, channel, encryption type, signal strength). Open to any Network; requires no authorization.
_Avoid_: scanning (use only for the underlying nmap/802.11 mechanism, not this domain concept)

**Band**:
One of the two radio frequency ranges Discovery can cover, 2.4 GHz or 5 GHz. A Network sits in one Band, which follows from its channel. Only Bands the adapter supports can be chosen.
_Avoid_: frequency, spectrum (those name the physical quantity, not the choice)

**Discovery session**:
The span over which the operator's view of discovered Networks accumulates. It begins when the operator first starts Discovery after launch, or starts a new session, and it continues across pausing and resuming Discovery, including resuming on a different Band.
_Avoid_: scan session, history, program session (a session is shorter than the program's lifetime)

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
