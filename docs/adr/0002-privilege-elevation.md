# Privilege elevation: cached sudo credential at launch, not setcap or per-action prompts

AirCommand needs root for monitor mode, packet injection, and raw-socket operations (airodump-ng, aireplay-ng, nmap SYN scans), but running the whole GUI as root was rejected — bugs in AirCommand's own code (or a bad pip dependency) would then have full system control, whereas mature, already-audited tools like aircrack-ng and nmap are a much smaller trust surface. AirCommand runs as the user's normal account. At launch it prompts once for a sudo password, then runs a background keepalive that touches `sudo` every 1-2 minutes to keep the credential cache warm for the life of the session; every privileged subprocess call rides on that cached credential silently, with no further prompts. When the app closes, the keepalive stops and the cached credential expires naturally shortly after — nothing privileged persists once AirCommand isn't running.

## Why

This gets one authentication per session (not one per privileged action, which is what `pkexec` or bare `sudo` per-command would force) while avoiding `setcap`'s downside: `setcap cap_net_raw,cap_net_admin+eip` on a tool binary is a standing, system-wide grant — any user or process on the machine gets that binary's elevated powers permanently, whether AirCommand is running or not, and the flag has to be reapplied after every tool update/reinstall. A single password prompt at launch, refreshed only while the app is open, keeps the elevated window scoped to "AirCommand is actually running" rather than "forever, for anyone."

## Considered Options

- **Run the whole GUI as root.** User's initial instinct ("anyone using this tool should be an admin anyway"). Rejected — the risk isn't the mature CLI tools, it's AirCommand's own code and dependency tree (including AI-assisted/smaller-model-written code), a much less trusted surface to hand root to wholesale.
- **`setcap` on the tool binaries (aircrack-ng suite, nmap), once at install time.** No prompts ever, but the grant is permanent and file-scoped, not app-scoped — any user or process invoking that binary directly gets the same power, indefinitely, regardless of whether AirCommand is open.
- **`pkexec` per privileged action.** Tightest per-action scoping, but a graphical auth popup on every single privileged call breaks up the GUI workflow badly.

## Consequences

AirCommand needs a background keepalive mechanism (a thread or subprocess quietly re-touching `sudo` every 1-2 minutes) for the life of the app session — a real component to build and test, not just a config flag. The user's account needs normal sudo rights (no passwordless/NOPASSWD sudoers entry — the one prompt at launch is the point). If the keepalive dies independently of the main app, privileged calls start failing mid-session; this should surface clearly in the GUI rather than fail silently.
