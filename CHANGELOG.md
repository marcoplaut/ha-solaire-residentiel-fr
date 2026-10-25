# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.1] - 2026-10-25

### Fixed

- Corrupted hours published by the portal (meter index instead of the energy of the hour, or values
  in Wh) no longer end up in the statistics and "today" sensors: impossible values are rejected and
  replaced using the portal's daily total. After updating, run the action
  `edf_enr.reimport_statistics` to repair statistics imported by 0.1.0.

## [0.1.0] - 2026-10-25

First release.

### Added

- Config flow with email/password sign-in to the customer area of the residential solar offer, installation
  selection for multi-installation accounts, re-authentication and reconfiguration.
- Hourly energy imported as external statistics `edf_enr:<pv>_<series>` (solar production, grid import,
  grid injection, self consumption, home consumption) for the Energy dashboard, with history back-fill
  and re-import of the last 3 days on every refresh.
- "Today" display sensors, installed peak power and monitoring gateway diagnostics.
- Action `edf_enr.reimport_statistics`.
- Diagnostics download (redacted).
- English and French translations.
