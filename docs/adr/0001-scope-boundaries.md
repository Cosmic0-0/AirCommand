# Scope boundaries: cut WEP cracking, WPS attacks, and evil-twin creation

AirCommand's initial candidate feature list (discovery, WPA capture/crack, deauth-assisted capture, WEP cracking, WPS attacks, rogue-AP/evil-twin) read as a general-purpose offensive wifi toolkit once laid out as a whole, which is not what this project is — it's a tool for auditing networks the user personally owns and administers. We decided to permanently cut WEP cracking, WPS attacks (PIN brute-force / Pixie Dust), and evil-twin *creation* from the roadmap rather than leave them as deprioritized stretch goals. Passive discovery, WPA/WPA2/WPA3 handshake capture + offline cracking, deauth (narrowly scoped as a support mechanism for handshake capture only, audit-logged), and rogue-AP/evil-twin *detection* (passively flagging another AP impersonating a Target's SSID/BSSID) remain in scope.

## Why

Each dropped capability provides little to no legitimate audit value against a network the user already owns and administers — an evil twin of your own network teaches you nothing you don't already know, since you already have the real credentials. Its only real use case is against a network you don't control, which is exactly what AirCommand is designed to exclude. WPS attacks are, by reputation, the technique most associated with unauthorized wifi access. WEP is obsolete; nothing worth auditing runs it. Carrying these in the roadmap costs real dual-use-risk weight for near-zero benefit to the stated purpose.

## Considered Options

- **Keep all candidate capabilities, rely on allowlist gating alone.** Rejected — the allowlist controls what execution is *allowed to do*, not how the project *reads* when its full capability list is described, which is what raised the concern in the first place.
- **Keep everything, add extra confirmation friction to the riskier features.** Rejected for WEP/WPS/evil-twin-creation specifically — the capabilities would still exist in the codebase and roadmap; friction doesn't address a scope problem. (This approach is still used for deauth, which stayed in scope.)

## Consequences

AirCommand cannot be used to audit WPS-vulnerable routers (e.g. Pixie Dust). If that becomes a real need later, it should be reopened as its own deliberate decision, not quietly reintroduced.
