# Solaire Résidentiel FR for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![Validate](https://github.com/marcoplaut/ha-solaire-residentiel-fr/actions/workflows/validate.yml/badge.svg)](https://github.com/marcoplaut/ha-solaire-residentiel-fr/actions/workflows/validate.yml)
[![Tests](https://github.com/marcoplaut/ha-solaire-residentiel-fr/actions/workflows/tests.yml/badge.svg)](https://github.com/marcoplaut/ha-solaire-residentiel-fr/actions/workflows/tests.yml)
[![Lint](https://github.com/marcoplaut/ha-solaire-residentiel-fr/actions/workflows/lint.yml/badge.svg)](https://github.com/marcoplaut/ha-solaire-residentiel-fr/actions/workflows/lint.yml)

Home Assistant integration for home solar installations sold and monitored through the
**residential solar offer of France's leading energy utility**. It reads your installation's data
from the offer's customer area and imports your hourly solar production, grid import, grid injection
and self consumption into Home Assistant long-term statistics, ready for the **Energy dashboard**.

> [!WARNING]
> **Unofficial project, not affiliated with, endorsed by or supported by the energy provider.**
> It relies on the private API of the customer area web site, which **may change or break without
> notice**. Your email and password are stored only in the local Home Assistant config entry and are
> sent only to the provider's own sign-in service and customer area API. Use at your own risk.

## Features

- Hourly energy statistics for the Energy dashboard: solar production, grid import, grid injection,
  self consumption and home consumption.
- Full history back-fill on first setup, and automatic correction of late or revised measurements.
- Protection against the corrupted hours the portal sometimes publishes (see
  [Data quality](#data-quality)).
- "Today" sensors for display (energy and self consumption / self sufficiency rates).
- Monitoring gateway diagnostics (last report, status) and installed peak power.
- Several installations per account, one config entry per installation.
- Re-authentication and reconfiguration from the UI; downloadable (redacted) diagnostics.
- English and French translations.

## Requirements

- Home Assistant **2026.10** or newer (the recorder integration is used).
- An account on the **customer area of the residential solar offer** (the web site where you follow
  your production), for an installation with a monitoring gateway.

## Installation

### Marketplace (Home Assistant 2026.11 and later)

1. Open **Settings > Marketplace**, add `https://github.com/marcoplaut/ha-solaire-residentiel-fr` as a custom repository
   (category **Integration**).
2. Download **Solaire Résidentiel FR**, then restart Home Assistant.

### HACS (Home Assistant 2026.10)

1. In HACS, open the menu (three dots) > **Custom repositories**.
2. Add `https://github.com/marcoplaut/ha-solaire-residentiel-fr` with category **Integration**.
3. Search for "Solaire Résidentiel FR", download it, then restart Home Assistant.

### Manual

Copy the `custom_components/edf_enr` folder of this repository into the `custom_components` folder of
your Home Assistant configuration, then restart Home Assistant.

## Configuration

1. Go to **Settings > Devices & services > Add integration** and search for
   **Solaire Résidentiel FR**.
2. Enter the email and password of your customer area account.
3. If the account has several installations, choose one. Repeat the setup to add the others; each
   installation is its own entry.

If the credentials stop working (for example after a password change), Home Assistant asks you to
**re-authenticate**. To change the stored credentials, use **Reconfigure** in the entry menu.

## Energy dashboard setup

**Use the external statistics, not the sensors.** The integration writes hourly statistics with the
id `edf_enr:<pv>_<series>`, where `<pv>` is your project number in lowercase and `<series>` is one of:

| Statistic id (example for PV0000042) | Meaning |
|---|---|
| `edf_enr:pv0000042_solar_production` | Solar production |
| `edf_enr:pv0000042_grid_import` | Energy drawn from the grid |
| `edf_enr:pv0000042_grid_injection` | Energy fed back to the grid |
| `edf_enr:pv0000042_self_consumption` | Solar energy consumed on site |
| `edf_enr:pv0000042_home_consumption` | Total home consumption |

Why: the portal publishes measurements with a delay and sometimes revises them. A regular sensor
would attribute late energy to the wrong hour. Statistics are written for the exact hour they belong
to and rewritten when the portal revises them. For the same reason, the "today" sensors are
display-only and **intentionally have no `state_class`**, so they cannot be picked up (and double
counted) by the Energy dashboard.

Go to **Settings > Dashboards > Energy** and pick the statistics (the picker lists them by name, or
type the id):

| Energy dashboard field | Statistic |
|---|---|
| Solar panels > Solar production | `edf_enr:<pv>_solar_production` |
| Electricity grid > Grid consumption | `edf_enr:<pv>_grid_import` |
| Electricity grid > Return to grid | `edf_enr:<pv>_grid_injection` |

`self_consumption` and `home_consumption` are provided for statistics graphs and custom cards; the
dashboard derives them from the three above.

Good to know:

- The first import **back-fills the whole history in the background**; it can take a minute or two.
  Statistics appear progressively.
- On every refresh the last **3 days** are re-imported, so revisions show up within 30 minutes.
- Data is **hourly**.

## Entities

Each installation creates a service device, plus a "Monitoring gateway" device when the portal
reports one.

| Entity | Notes |
|---|---|
| Solar production today | kWh, display only |
| Grid import today | kWh, display only |
| Grid injection today | kWh, display only |
| Self consumption today | kWh, display only |
| Home consumption today | kWh, display only |
| Self consumption rate today | %, share of the production consumed on site |
| Self sufficiency rate today | %, share of the consumption covered by the production |
| Installed peak power | kW, diagnostic, only if declared in the contract |
| Last report | Timestamp, diagnostic, gateway |
| Status | Diagnostic, gateway, disabled by default |

### Action `edf_enr.reimport_statistics`

Deletes the imported statistics of an installation and imports the whole history again in the
background. Parameter: `config_entry_id` (the installation). Useful if statistics look wrong; it fails
if an import is already running.

## Data updates

The integration polls the portal every **30 minutes**. The portal exposes 10-minute buckets with a
short delay, so polling faster would only load an unofficial API without giving fresher data.

## Data quality

The portal occasionally publishes corrupted hours, typically right after a monitoring gateway
reset: a meter index instead of the energy of the hour (millions of kWh), then a few hours in Wh
instead of kWh. Stored as is, a single such hour would ruin the Energy dashboard totals for good.

Any hourly value above what the installation can physically measure (1.5 × its peak power for
production and self consumption, 36 kW — the highest residential grid connection — for grid
import) is rejected. The integration then reads the portal's daily total of that day, which stays
right, and spreads what it has in excess of the accepted hours over the rejected ones. The energy
of the day is preserved; only its distribution within those hours is approximated.

## Known limitations

- **No live power.** Instantaneous power is only offered by more advanced monitoring gateways and is
  not supported yet.
- Statistics are **hourly**, even though the portal has finer data.
- History depth depends on what the portal returns for your installation.
- The portal's refresh tokens last 24 hours, so the integration performs a **full sign-in about
  once a day** (using the stored credentials).
- The portal works in Paris wall-clock time. On the autumn daylight saving change it reports the
  repeated hour as a single bucket, so that energy lands in the first of the two hours and the
  second one shows 0. Daily totals are not affected.
- The private API can change at any time.

## Troubleshooting

Enable debug logging in `configuration.yaml`, restart, and reproduce the problem:

```yaml
logger:
  default: warning
  logs:
    custom_components.edf_enr: debug
```

You can also enable it from the integration page ("Enable debug logging"). When opening an issue,
attach the **diagnostics** (Settings > Devices & services > Solaire Résidentiel FR > three-dot menu >
Download diagnostics). They are redacted (credentials, tokens, identifiers, addresses), but review
them before posting. Never post your password.

## Removal

1. Delete the entry in **Settings > Devices & services > Solaire Résidentiel FR**.
2. Optionally remove it from the Marketplace or HACS (or delete `custom_components/edf_enr`) and restart.

Removing the entry **also deletes its imported statistics**, so the Energy dashboard history from
this installation is lost.

## Development

```bash
uv venv --python 3.14
source .venv/bin/activate
uv pip install -r requirements_dev.txt   # or: pip install -r requirements_dev.txt

pytest                                   # the whole test suite
pytest --cov --cov-report=term           # with coverage (must stay >= 95 %)
pytest tests/test_e2e_simulator.py       # end-to-end tests only
ruff check . && ruff format --check .
mypy
```

### Test layers

| Layer | Files | What it covers |
|---|---|---|
| Unit | `tests/test_api_models.py`, `tests/test_api_client.py`, pure helpers in `tests/test_statistics.py` | Parsing, Paris-time conversion and DST, the B2C login steps, token refresh/renewal, HTTP error mapping (HTTP mocked with `aioresponses`) |
| Integration | `tests/test_config_flow.py`, `test_init.py`, `test_coordinator.py`, `test_sensor.py`, `test_services.py`, `test_diagnostics.py`, `test_statistics.py` | A real Home Assistant instance with an in-memory recorder and a mocked API client: flows, setup/unload, reauth, availability, entities (snapshots), diagnostics redaction, statistics written to the recorder (back-fill, revisions, gaps, sum continuity) |
| End to end | `tests/test_e2e_simulator.py` + `tests/simulator/` | Home Assistant + the real client + a local HTTP simulator of the portal (B2C cookies, CSRF, PKCE, rotating tokens, Paris-time buckets, placeholders, failure injection): full journey through the UI flow, token expiry, revocation, password change and reauth, outages and rate limiting, late and revised data, DST |

Never commit real portal responses, captures or credentials. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE). Product and company names belong to their respective owners.
