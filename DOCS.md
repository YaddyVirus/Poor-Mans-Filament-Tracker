# Poor Man's Filament Tracker — Usage

## How print data is collected

**Direct printer connection (default when `PRINTER_*` is set):** the app
connects to the printer's MQTT (TLS :8883) for status, progress, and the
external-spool filament setting, and fetches each job's 3MF over FTPS
(:990) to read the sliced filament weight — the MQTT stream doesn't carry
weight. If the fetch fails, the print is recorded at **0 g** (flagged
"0 g?" in History) so you can fill in the grams afterwards. Home Assistant
being down never interrupts tracking.

**Via ha-bambulab (fallback):** with no printer credentials, the app
watches the integration's sensors over the HA websocket instead,
auto-discovering entities ending in `_print_status`, `_print_weight`,
`_print_progress`, `_task_name`, and `_external_spool`.

## Automatic spool detection

The external spool has no RFID — the printer only knows what *you* set
on its filament screen (Load → Edit) when loading. The app matches that
setting (material + nearest color) against your library and loads the
right spool automatically.

**Twin spools** (same brand/material/color) can't be told apart: the app
keeps the **last loaded** spool, shows a verify card in the UI, and raises
an HA persistent notification. One click confirms which twin is loaded;
the answer is remembered until the situation changes.

## Controlling the loaded spool from Home Assistant

With `HA_URL`/`HA_TOKEN` configured, the app creates and maintains an
**`input_select.loaded_filament_spool`** helper:

- Options mirror your (non-archived) spools; twins get an `[id]` suffix.
- Selecting an option in HA loads that spool in the tracker.
- Loading a spool in the app (or via auto-detect) updates the selection.

Add it to any dashboard as an entities card or a dropdown row. Spool
management (add/edit/archive/history) lives in the app UI.

The app also publishes **`sensor.filament_tracker_remaining`** (grams
left, with brand/material/color attributes):

```yaml
automation:
  - alias: Low filament warning
    trigger:
      - platform: numeric_state
        entity_id: sensor.filament_tracker_remaining
        below: 100
    action:
      - service: notify.mobile_app_your_phone
        data:
          message: "Filament low: {{ states('sensor.filament_tracker_remaining') }} g left"
```

## Cost tracking

Give a spool a price (Add/Edit spool → **Cost**) and the app derives the
rest: a print's cost is `grams used ÷ spool weight × spool price`, shown
next to each entry in History, and every spool card shows the total
spent on it so far the same way.

Things worth knowing before trusting the numbers:

- **Costs are computed live from the spool's current price**, not
  snapshotted when the print happened. Correcting a typo in the price
  retroactively fixes the whole history — but changing it to a genuinely
  different price rewrites history too. There is no per-print price
  ledger.
- Clearing a spool's Cost field removes the price entirely; its prints
  simply stop showing costs.
- Manual weight adjustments show a matching +/− cost, consistent with
  how their grams are displayed.
- The **currency dropdown in the header** is stored server-side, so it
  follows the app rather than the browser. It changes the symbol shown
  everywhere and nothing else — no conversion is applied when switching.

## Fixing mistakes

- **Since last filament change** lists every print recorded since the
  current spool was loaded. Swapped filament and forgot to tell the app?
  **Move all** shifts those prints to the right spool in one go.
- Any print in **History** can be edited (✎): change grams (e.g. a 0 g
  record) or reassign to another spool — remaining weights recalculate.
- Manual corrections to a spool's remaining weight are logged as `adjust`
  entries so history stays honest.

## Good to know

- Print weight is the **slicer's estimate** from the 3MF (printers have no
  scale); failed prints are prorated by progress. Weigh spools occasionally
  and correct drift via **Edit → Remaining**.
- The brand/color catalog is curated at build time from manufacturer color
  pages. Numakers PLA+/PLA Metallic and Bambu Lab hexes are official
  published values; other entries are close approximations. Pick **Other**
  for anything not listed — the manual color picker always works.
- Data lives in the `/data` volume (`filament.db`) and survives container
  rebuilds.
- The UI follows your system light/dark preference; override with the
  ☀️/🌙 toggle (persisted per browser).
