# mi_home_cli

[简体中文](README.md) · **English**

[![PyPI](https://img.shields.io/pypi/v/mi-home-cli?color=blue)](https://pypi.org/project/mi-home-cli/)
[![Python](https://img.shields.io/pypi/pyversions/mi-home-cli)](https://pypi.org/project/mi-home-cli/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Stars](https://img.shields.io/github/stars/slightc/mi_home_cli?style=social)](https://github.com/slightc/mi_home_cli/stargazers)

Control your Xiaomi Home (Mi Home / MIoT) devices from the command line.

```bash
mi device list --room "Living Room"   # see what you've got
mi set lamp on=true brightness=60     # control it
mi light lamp --ct 4000 --color warm  # ...or in plain words
mi watch purifier -o json | jq        # stream in real time, feed it to scripts
```

Protocol behavior is independently reimplemented with reference to
[XiaoMi/ha_xiaomi_home](https://github.com/XiaoMi/ha_xiaomi_home) (Xiaomi's official
Home Assistant integration). It **contains none of its source code or resources**, and
is not affiliated with Xiaomi.

---

## Features

- **No `did` to memorize**: refer to devices by name, alias, `room/name`, or model;
  refer to properties by `on`, `light.brightness`, or `2.1`; write enum values as
  plain words (`mode=sleep`).
- **spec-driven**: value ranges, enums, and read/write permissions all come from the
  device's own MIoT spec. Out-of-range values and writes to read-only properties are
  rejected *before* a request is ever sent.
- **Composable**: `-o json` gives stable structured output, `-o plain` gives you the
  bare value, and exit codes are clearly categorized.
- **Real-time push**: `mi watch` rides a cloud MQTT long connection — not polling.
- **Local (LAN) direct control**: supported devices can bypass the cloud, dropping
  latency from 1–2 s to tens of milliseconds.
- **Credentials stay local**: `~/.config/mi-home-cli/`, files mode `0600`, never
  uploaded to any third-party service.

## Install

Requires Python 3.11+. Install from PyPI with [uv](https://docs.astral.sh/uv/) or
pipx for a global `mi` command in one line:

```bash
uv tool install --with zeroconf mi-home-cli   # recommended
# or
pipx install mi-home-cli
# or
pip install mi-home-cli
```

`--with zeroconf` (with pipx: `pipx install "mi-home-cli[mdns]"`) is the optional mdns
enhancement — it improves the success rate of automatically receiving the login
callback. QR login (`mi auth login --scan`) works out of the box — nothing extra to
install.

Then:

```bash
mi --help
```

<details>
<summary><b>From source (development, or to hack on it)</b></summary>

```bash
git clone https://github.com/slightc/mi_home_cli && cd mi_home_cli
uv sync --extra mdns      # mdns is optional; it improves the success rate of
                          # automatically receiving the login callback
uv run mi --help
```

When running from source, replace `mi` with `uv run mi` below.
</details>

## Quick start

```bash
mi doctor          # health check first: ports, DNS, network, certs, clock
mi auth login      # log into your Xiaomi account in the browser
mi device sync     # pull homes, rooms, and the device list
mi device list

mi home use Home   # with multiple homes, setting a default is strongly advised
mi device alias set "Mijia Air Purifier 6" purifier

mi get purifier    # read every readable property
mi set purifier mode=sleep
mi fan purifier --speed 2
```

> Running from source (not installed from PyPI)? Replace `mi` with `uv run mi`.

### About login

Xiaomi's OAuth service only accepts a callback host of `homeassistant.local:8123`
(see the empirical findings in [design.md §3.2](docs/design.md)), so `mi auth login`
will:

1. listen on local port 8123;
2. try to point `homeassistant.local` at your machine via mDNS so the browser can
   redirect back;
3. if neither works, it still doesn't matter — after authorizing, the browser lands on
   an unreachable URL, so just **paste the whole address from the address bar back into
   the terminal** (`mi auth login --manual` goes straight down this path).

The local callback and the paste are awaited at the same time; whichever arrives first
wins. Tokens auto-renew once 70% of their lifetime is used.

**Prefer not to touch a browser? Use QR login:**

```bash
mi auth login --scan
```

A QR code is drawn right in your terminal. Scan it with the Mi Home / Xiaomi Home app
(Me → the scan button, or Settings → Xiaomi Account) and tap "Confirm login" on your
phone — no browser, no pasting. This is a pure-API reimplementation of the login page's
redirect flow (**no headless browser**); the auth code it yields is exactly the one the
browser flow produces. It works out of the box — nothing extra to install.

The terminal QR depends on your font and line spacing, so in a few terminals it may not
scan — that's fine: the command also **prints a QR image URL**. Open that image in a
browser and scan it instead (same QR), so you're never stuck.

## Core concepts

### How to refer to a device

Matched from most to least specific; the first level that hits stops the search:

```
did → alias → exact name → room/name → name substring → model
```

If more than one device matches at the same level, you get an ambiguity error listing
the candidates (exit code 4). Narrow it with `--home` / `--room` / `--model`, or just
give it an alias:

```bash
mi device alias set "Roborock Self-Cleaning Robot G10" vacuum
mi on vacuum
```

### How to write a property

Three equivalent forms, all resolved by the device's spec:

```bash
mi get lamp 2.2                  # siid.piid
mi get lamp light.brightness     # service.property
mi get lamp brightness           # bare name (only if unique within that device)
```

When a bare name exists in multiple services, the device's primary service wins — an
air purifier's `on` means the main power switch, not the screen; use `screen.on` for
the screen.

Values are just as forgiving: `on=true` / `on=on` / `on=1` are equivalent, and enum
values `mode=sleep` / `mode=Sleep` / `mode=3` are equivalent.

### Default home

With several homes, duplicate device names are common. Once a default home is set,
device resolution, `device list`, and `room list` only look at that home:

```bash
mi home use Home
mi home select           # pick one interactively (can also clear)
mi home unset            # clear the default home
mi home use --clear      # same, cancel
mi --all-homes ...       # cross-home, one-off
```

A device in another home is never operated silently across homes — instead it tells you
where it is:

```
✗ No `Downlight 8` in default home "Home", but found Downlight 8 in Lakeview Bldg 19 #301 / Entry Hallway
hint: add --all-homes to operate across homes, or switch default with `mi home use`
```

### Control channel

| Channel | Behavior |
| --- | --- |
| `cloud` (default) | goes through the Mi Home cloud; works for any device |
| `lan` | LAN direct only; if it can't, it errors and says exactly where it got stuck |
| `auto` | direct if possible; silently falls back to cloud if any step fails (including the call itself) |

```bash
mi --channel lan get purifier on
mi config set channel auto     # to default to LAN
```

The default is `cloud`: a first LAN hit costs a liveness probe plus one possibly-failing
call, and the default shouldn't pay that wait.

## Command overview

Full arguments in [docs/cli-spec.md](docs/cli-spec.md).

### Account

```bash
mi auth login [--scan] [--manual] [--no-browser] [--region cn]
mi auth login --scan         # QR login: a QR in the terminal, scan with the app
mi auth status [--check]     # login state, remaining token lifetime
mi auth exchange <code|URL>  # retry the token-exchange step with the same auth code
mi auth refresh | whoami | logout
mi profile list|use|remove|path
```

### Viewing

```bash
mi home list
mi home use [<home>] [--clear]
mi home select                 # pick the default home interactively
mi home unset                  # clear the default home
mi room list [--home Home]
mi device list [--home] [--room] [--model] [--search] [--online|--offline] [--wide]
mi device show <device>
mi device sync
mi device alias set|list|rm
mi device token <device> [--show-secrets]
```

### Capabilities

```bash
mi spec show <device|urn> [--siid N] [--writable] [--actions]
mi spec search <device> <keyword>
mi spec dump <device>            # full spec, for scripts or an LLM
mi spec cache info|clear
```

`mi spec show` is the entry point for looking up `siid.piid`, value ranges, and enums:

```
service property           id    perm  type    range           unit  desc
light   on                 2.1   rwn   bool    -               -     switch
light   brightness         2.2   rwn   uint8   1~100 step 1    %     brightness
light   color-temperature  2.3   rwn   uint16  2700~6000       K     color temp
light   mode               2.7   rwn   uint8   0=None 4=Day…    -     mode
```

### Control

```bash
mi get <device> [prop...]             # no prop = read all readable properties
mi set <device> <prop=value>...       # multiple at once, batched
mi action <device> [action] [--in v]...  # no action name = list all actions
mi on|off|toggle <device>

# semantic commands (show current state when given no options)
mi light <device> [--on|--off] [--brightness 60] [--ct 4000] [--color red] [--mode day]
mi climate <device> [--on|--off] [--mode cool] [--temp 26] [--fan 2]
mi cover <device> [--open|--close|--stop] [--position 50]
mi fan <device> [--on|--off] [--speed 2] [--mode auto] [--swing]
```

Writes print `old → new → result`, so you can roll back if you got it wrong. Add
`--verify` to read the value back after writing and trust the device's real state
(one extra round trip).

### Real time

```bash
mi watch [device...] [--prop on] [--no-events] [--no-state]
                     [--all-updates] [--exit-after N] [--duration SECONDS]
```

With no device, it watches every device in the default home. Devices periodically
re-report the same value; those are skipped by default, `--all-updates` shows them all.

```bash
# use it as an automation trigger
mi watch door-lock -o json | while read -r line; do
  echo "$line" | jq -r 'select(.kind=="event")'
done
```

### LAN

```bash
mi lan list                  # which devices in the list support direct control
mi lan discover              # broadcast scan and cache IPs
mi lan status <device>        # reachability, IP, latency
mi lan raw <device> miIO.info # send one raw miIO request (for troubleshooting)
```

### Misc

```bash
mi config list|get|set|unset|path    # profile / region / output / home / channel
mi doctor                            # environment health check
mi --install-completion              # shell completion (built into typer)
mi version
```

## Scripting

Global options go before the subcommand (`-o` may also follow the subcommand):

| Option | Meaning |
| --- | --- |
| `-o, --output` | `table` (default) / `json` / `yaml` / `plain` |
| `-p, --profile` | multi-account isolation |
| `--channel` | `cloud` (default) / `auto` / `lan` |
| `--home` / `--all-homes` | limit to or ignore the default home |
| `--dry-run` | parse and validate only, don't send |
| `--verify` | read back after writing to confirm |
| `-v, --verbose` | print request details and which channel was used |

Environment variables `MI_PROFILE` / `MI_REGION` / `MI_OUTPUT` / `MI_CHANNEL` are also
supported.

```bash
mi -o plain get lamp brightness        # prints just "60", drop it straight into $(...)
mi -o json device list | jq '.[] | select(.online)'
```

JSON / YAML output uses English field names and raw types (`"online": true`, not
`"online": "yes"`); the Chinese table headers only appear in the human-facing tier.

## For AI agents

The repo ships a [Claude Code skill](.claude/skills/mi-home/SKILL.md): running Claude
Code inside this repo loads it automatically. It teaches an agent how to locate devices,
inspect specs, branch on exit codes, and be careful when controlling real devices (ask
on ambiguity, writes have physical effects, don't poll).

To enable it globally: `cp -r .claude/skills/mi-home ~/.claude/skills/`

Exit codes:

| Code | Meaning | Code | Meaning |
| --- | --- | --- | --- |
| 0 | success | 6 | property/action not found |
| 1 | generic error | 7 | illegal value (out of range, read-only) |
| 2 | argument error | 8 | network error |
| 3 | device not found | 9 | cloud returned an error |
| 4 | ambiguous device reference | 10 | not logged in or session expired |
| 5 | device offline/unreachable | 130 | Ctrl-C interrupt |

## Where data lives

```
~/.config/mi-home-cli/
├── config.json                 # default profile / region / output / home / channel
└── profiles/<name>/
    ├── auth.json               # tokens (0600)
    ├── identity.json           # device_id and callback id used for OAuth
    ├── devices.json            # device list cache, incl. LAN tokens (0600)
    ├── aliases.json            # custom aliases
    ├── lan.json                # LAN address cache
    └── spec/                   # spec cache, 14 days
```

Set `MI_HOME_CONFIG_DIR` to change the location. A device token is equivalent to LAN
control, so it's masked in all output by default; `--show-secrets` prints it in the
clear.

## FAQ

<details>
<summary><b>On login, the browser jumps to an unreachable URL</b></summary>

That's normal. Xiaomi only accepts `homeassistant.local:8123` as the callback host, and
your machine most likely has no Home Assistant. Just paste the whole address from the
browser's address bar back into the terminal — the CLI is still waiting.
</details>

<details>
<summary><b>On macOS, <code>mi lan discover</code> finds no devices</b></summary>

macOS 14+ blocks LAN broadcasts sent from the terminal, and **drops them silently, with
no error**. Allow your terminal (Terminal / iTerm / VS Code) under
`System Settings → Privacy & Security → Local Network`.

Then check: on the same subnet as the device? AP isolation enabled on the router?
</details>

<details>
<summary><b><code>mi watch</code> reports a certificate verification failure</b></summary>

Two causes:

1. **The system root CA store is empty.** Common with uv-installed Python on macOS
   (`ssl.create_default_context()` has 0 certs). The CLI automatically falls back to
   certifi and can connect; to fix it for good, add
   `export SSL_CERT_FILE=$(python -m certifi)` to your shell config.
2. **A proxy is doing MITM.** Clash / Surge fake-IP resolves the domain to
   `198.18.x.x`. Add a direct rule for `mqtt.io.mi.com`, or point at the proxy's root
   cert with `MI_CA_BUNDLE=/path/to/ca.pem`.

`mi doctor` flags each of these separately.
</details>

<details>
<summary><b>Which devices support LAN direct control</b></summary>

Only Wi-Fi/wired devices connected straight to the router (`connect_type ∈ {0,8,12,23}`
with a token). Bluetooth Mesh, ZigBee, and anything behind a gateway can't — in one real
account, only 21 of 72 devices qualified. `mi lan list` shows which ones can.

**"Discoverable" doesn't mean "usable":** device firmware supports MIoT spec methods
differently. On the Mijia Air Purifier 6, `get_properties` over LAN works perfectly;
on an older-firmware Yeelight lamp, `miIO.info` goes through but `get_properties` returns
`user ack timeout`. `mi lan raw <device> miIO.info` distinguishes "protocol unreachable"
from "this method isn't supported by the device". The `auto` channel falls back to cloud
for such devices and records the result, so later commands don't retry in vain.
</details>

<details>
<summary><b>Is a write returning <code>code=1</code> a failure?</b></summary>

No. `0` is success, `1` is "accepted, device executing" — some devices (observed:
`dwdz.switch.sw0a01`) return `1` on every write while the operation actually succeeded.
For a definitive answer add `--verify`, which reads back once after writing.
</details>

## Development

```bash
uv sync --extra mdns
uv run pytest
```

Tests run fully offline, no Xiaomi account needed: cloud endpoints are replayed with
`httpx.MockTransport`, the LAN protocol runs the full chain against a fake device with an
independent packet-assembly implementation, and MQTT wiring is verified with a fake
client.

After editing `pyproject.toml`, run `uv lock` to update the lockfile.

## Scope

| | Status |
| --- | --- |
| OAuth login (browser / paste / QR `--scan`), token renewal, multi-account | ✅ |
| Homes / rooms / device list | ✅ |
| spec fetch & cache, property read/write, action calls | ✅ |
| Semantic commands, aliases, default home | ✅ |
| Cloud MQTT real-time push | ✅ |
| LAN direct control (miIO) | ✅ |
| Mi Home scenes / automations | ❌ needs the account-password identity; this project is OAuth2-only |
| Camera alarm / recording playback | ❌ same as above |
| Camera live stream URL | ⚠️ some models, see [docs/camera-stream.md](docs/camera-stream.md) |
| Central gateway local control | ❌ needs certificates, cn region only, high complexity |

**OAuth2-only** is a deliberate boundary: authorization happens in the browser, the CLI
only ever holds a time-limited, revocable token, and never touches your account password.
The cost is the ❌ endpoints above — they require the `serviceToken` + `ssecurity`
identity. If you need those,
[al-one/hass-xiaomi-miot](https://github.com/al-one/hass-xiaomi-miot) (Apache-2.0) covers
more ground.

Design and protocol details are in [docs/design.md](docs/design.md), and the full command
reference is in [docs/cli-spec.md](docs/cli-spec.md).

## License

[MIT](LICENSE).

## Disclaimer

This project is not affiliated with Xiaomi. The `app/v2/*` endpoints are the ones Xiaomi
provides for its Home Assistant integration — they are not a public contract and may
change at any time.

Account credentials are stored only on your machine and never uploaded to any third-party
service. This project contains no source code or resources from `ha_xiaomi_home`; it only
references its publicly observable interface behavior.
